#!/bin/sh
# Isolation matrix against a running gateway (MLOPS-02 evidence). Exit 0 only if every check holds.
#
#   GATEWAY_URL=https://mlflow.<domain> TOKEN_A=... TOKEN_B=... scripts/gateway-matrix.sh
#
# Required: TOKEN_A (write scope, context A) and TOKEN_B (write scope, a DIFFERENT context).
# Optional: TOKEN_A_READ (read-only scope of context A) to prove it cannot write;
#           TOKEN_REVOKE + REVOKE_COMMAND to prove revocation (see below).
#
# What it proves, with real objects instead of guessed IDs:
#   1. A creates an experiment, a run, an artifact (PUT) and a registered model; A reads each (200).
#   2. B asks for the SAME ids and gets 403 or 404; a 200 (or any B write that succeeds) is a FAIL.
#   3. A control with ids that do not exist answers 404 for both, so the 403/404 above is not a
#      broken endpoint or a typo.
#   4. Everything it created is deleted at the end, also when a check fails or the script is
#      interrupted (MLflow keeps soft-deleted experiments/runs; the names are unique per run).
#   5. Revocation, only when TOKEN_REVOKE is a valid token of A and REVOKE_COMMAND is a shell
#      command that revokes exactly that token in InvoiceOps: the token must stop working within
#      REVOKE_WAIT_SECONDS (default 40; the gateway caches an answer for at most 30 s).
#
# Secrets are read from the environment and handed to curl through a config on stdin, so they
# never appear in argv. Needs curl and python3 (JSON parsing only).
set -eu

: "${GATEWAY_URL:?GATEWAY_URL must be set}"
: "${TOKEN_A:?TOKEN_A must be set (write scope, context A)}"
: "${TOKEN_B:?TOKEN_B must be set (write scope, another context)}"
TOKEN_A_READ=${TOKEN_A_READ:-}
TOKEN_REVOKE=${TOKEN_REVOKE:-}
REVOKE_COMMAND=${REVOKE_COMMAND:-}
REVOKE_WAIT_SECONDS=${REVOKE_WAIT_SECONDS:-40}
GATEWAY_URL=${GATEWAY_URL%/}

API=/api/2.0/mlflow
ARTIFACTS=/api/2.0/mlflow-artifacts/artifacts
MARKER="matrix-artifact-$$"
NAME="matrix-$$-$(date +%s)"
work=$(mktemp -d)
fail=0
STATUS=000
exp_id=""
run_id=""
artifact_path=""
model_name=""
cleaned=0

# request TOKEN METHOD PATH [JSON-BODY | @FILE]; sets STATUS, body in $work/body
request() {
  token=$1
  method=$2
  path=$3
  payload=${4:-}
  : >"$work/body"
  {
    printf 'url = "%s%s"\nrequest = "%s"\nsilent\nshow-error\n' "$GATEWAY_URL" "$path" "$method"
    printf 'output = "%s/body"\nwrite-out = "%%{http_code}"\nmax-time = 60\n' "$work"
    [ -z "$token" ] || printf 'header = "Authorization: Bearer %s"\n' "$token"
    case $payload in
      '') ;;
      @*)
        printf 'header = "Content-Type: application/octet-stream"\n'
        printf 'data-binary = "@%s"\n' "${payload#@}"
        ;;
      *)
        printf '%s' "$payload" >"$work/request.json"
        printf 'header = "Content-Type: application/json"\ndata-binary = "@%s/request.json"\n' "$work"
        ;;
    esac
  } | curl --config - >"$work/status" 2>"$work/curl.err" || true
  STATUS=$(cat "$work/status")
  [ -n "$STATUS" ] || STATUS=000
}

jget() { # file dotted.path -> value or empty
  python3 - "$1" "$2" <<'PY'
import json, sys
try:
    node = json.load(open(sys.argv[1]))
    for part in sys.argv[2].split("."):
        node = node[part]
    print(node if not isinstance(node, (dict, list)) else json.dumps(node))
except Exception:
    pass
PY
}

ok() { printf 'ok   %s %s\n' "$1" "$2"; }
bad() {
  printf 'FAIL %s (want %s) %s\n' "$1" "$2" "$3"
  fail=1
}

expect() { # WANT-STATUS DESCRIPTION  (uses the last request)
  if [ "$STATUS" = "$1" ]; then ok "$STATUS" "$2"; else bad "$STATUS" "$1" "$2"; fi
}

expect_one_of() { # "403 404" DESCRIPTION (a 200 is never in the list)
  for want in $1; do
    if [ "$STATUS" = "$want" ]; then
      ok "$STATUS" "$2"
      return
    fi
  done
  bad "$STATUS" "$1" "$2"
}

cleanup() {
  [ "$cleaned" = 0 ] || return 0
  cleaned=1
  if [ -n "$artifact_path" ]; then
    request "$TOKEN_A" DELETE "$ARTIFACTS/$artifact_path"
    case $STATUS in 200 | 204 | 404) ok "$STATUS" "cleanup: artifact" ;; *) bad "$STATUS" "200" "cleanup: artifact" ;; esac
  fi
  if [ -n "$run_id" ]; then
    request "$TOKEN_A" POST "$API/runs/delete" "{\"run_id\":\"$run_id\"}"
    expect 200 "cleanup: run"
  fi
  if [ -n "$exp_id" ]; then
    request "$TOKEN_A" POST "$API/experiments/delete" "{\"experiment_id\":\"$exp_id\"}"
    expect 200 "cleanup: experiment"
  fi
  if [ -n "$model_name" ]; then
    request "$TOKEN_A" DELETE "$API/registered-models/delete" "{\"name\":\"$model_name\"}"
    expect 200 "cleanup: registered model"
  fi
}

# shellcheck disable=SC2329 # invoked through the EXIT trap
finish() {
  rc=$?
  cleanup
  rm -rf "$work"
  if [ "$rc" -eq 0 ] && [ "$fail" -ne 0 ]; then rc=1; fi
  exit "$rc"
}
trap finish EXIT
trap 'exit 130' INT TERM

# ---- authentication basics ------------------------------------------------------------------
request "" GET "$API/experiments/search?max_results=1"
expect 401 "anonymous API"
request "" GET /
expect 401 "anonymous UI"
request "iop_invalid_token_for_the_matrix_000000" GET "$API/experiments/search?max_results=1"
expect 401 "invalid token"
request "$TOKEN_A" POST /api/3.0/mlflow/workspaces '{"name":"x"}'
expect 403 "workspace admin endpoint blocked"
request "$TOKEN_A" POST "$API/users/create" '{"username":"x","password":"y"}'
expect 403 "user admin endpoint blocked"

# ---- 1. A creates its objects -----------------------------------------------------------------
request "$TOKEN_A" POST "$API/experiments/create" "{\"name\":\"$NAME\"}"
expect 200 "A creates an experiment"
exp_id=$(jget "$work/body" experiment_id)
request "$TOKEN_A" POST "$API/runs/create" "{\"experiment_id\":\"$exp_id\"}"
expect 200 "A creates a run"
run_id=$(jget "$work/body" run.info.run_id)
artifact_uri=$(jget "$work/body" run.info.artifact_uri)
if [ -z "$exp_id" ] || [ -z "$run_id" ] || [ -z "$artifact_uri" ]; then
  echo "FAIL could not create the experiment/run as A: nothing else can be checked"
  exit 1
fi
printf '%s' "$MARKER" >"$work/artifact.txt"
artifact_path="${artifact_uri#mlflow-artifacts:/}/matrix.txt"
request "$TOKEN_A" PUT "$ARTIFACTS/$artifact_path" "@$work/artifact.txt"
expect 200 "A uploads an artifact"
model_name=$NAME-model
request "$TOKEN_A" POST "$API/registered-models/create" "{\"name\":\"$model_name\"}"
expect 200 "A creates a registered model"

# ---- 2. the owner reads each of them (200) ------------------------------------------------------
request "$TOKEN_A" GET "$API/experiments/get?experiment_id=$exp_id"
expect 200 "A reads its experiment"
request "$TOKEN_A" GET "$API/runs/get?run_id=$run_id"
expect 200 "A reads its run"
request "$TOKEN_A" GET "$ARTIFACTS/$artifact_path"
expect 200 "A reads its artifact"
[ "$(cat "$work/body")" = "$MARKER" ] || bad "-" "artifact bytes" "A reads back different artifact bytes"
request "$TOKEN_A" GET "$API/registered-models/get?name=$model_name"
expect 200 "A reads its registered model"

if [ -n "$TOKEN_A_READ" ]; then
  # After A's first write: a workspace is created by a write, never by a read.
  request "$TOKEN_A_READ" GET "$API/runs/get?run_id=$run_id"
  expect 200 "read token reads A's run"
  request "$TOKEN_A_READ" POST "$API/experiments/create" "{\"name\":\"$NAME-denied\"}"
  expect 403 "read token cannot create"
fi

# ---- 3. context B on the SAME ids: 403 or 404, never 200 -----------------------------------------
request "$TOKEN_B" GET "$API/experiments/get?experiment_id=$exp_id"
expect_one_of "403 404" "B cannot read A's experiment"
request "$TOKEN_B" GET "$API/runs/get?run_id=$run_id"
expect_one_of "403 404" "B cannot read A's run"
request "$TOKEN_B" GET "$ARTIFACTS/$artifact_path"
expect_one_of "403 404" "B cannot read A's artifact"
if grep -q "$MARKER" "$work/body"; then bad "200" "no marker" "A's artifact bytes reached B"; fi
request "$TOKEN_B" GET "$API/registered-models/get?name=$model_name"
expect_one_of "403 404" "B cannot read A's registered model"
request "$TOKEN_B" PUT "$ARTIFACTS/$artifact_path" "@$work/artifact.txt"
expect_one_of "403 404" "B cannot overwrite A's artifact"
request "$TOKEN_B" POST "$API/runs/delete" "{\"run_id\":\"$run_id\"}"
expect_one_of "403 404" "B cannot delete A's run"
request "$TOKEN_B" POST "$API/experiments/search" '{"max_results":1000}'
case $STATUS in 200 | 404) ok "$STATUS" "B searches its own experiments" ;; *) bad "$STATUS" "200 or 404" "B searches its own experiments" ;; esac
if grep -q "$NAME" "$work/body"; then bad "200" "no foreign name" "A's experiment is listed to B"; fi

# the owner's data survived B's attempts
request "$TOKEN_A" GET "$API/runs/get?run_id=$run_id"
expect 200 "A's run is intact after B's attempts"

# ---- 4. nonexistent-id control: 404 for both ------------------------------------------------------
missing_run=00000000000000000000000000000000
for who in A B; do
  if [ "$who" = A ]; then token=$TOKEN_A; else token=$TOKEN_B; fi
  request "$token" GET "$API/experiments/get?experiment_id=999999999999"
  expect 404 "$who: nonexistent experiment id"
  request "$token" GET "$API/runs/get?run_id=$missing_run"
  expect 404 "$who: nonexistent run id"
  request "$token" GET "$API/registered-models/get?name=$NAME-does-not-exist"
  expect 404 "$who: nonexistent registered model"
done

# ---- 5. revocation (optional) ------------------------------------------------------------------------
if [ -n "$TOKEN_REVOKE" ] && [ -n "$REVOKE_COMMAND" ]; then
  request "$TOKEN_REVOKE" GET "$API/experiments/search?max_results=1"
  expect 200 "revocable token works before revocation"
  if sh -c "$REVOKE_COMMAND" >/dev/null 2>&1; then
    waited=0
    while [ "$waited" -lt "$REVOKE_WAIT_SECONDS" ]; do
      request "$TOKEN_REVOKE" GET "$API/experiments/search?max_results=1"
      case $STATUS in 401 | 403) break ;; esac
      sleep 1
      waited=$((waited + 1))
    done
    case $STATUS in
      401 | 403) ok "$STATUS" "revoked token refused after ${waited}s" ;;
      *) bad "$STATUS" "401" "revoked token still accepted after ${REVOKE_WAIT_SECONDS}s" ;;
    esac
  else
    bad "-" "revocation" "REVOKE_COMMAND failed"
  fi
else
  echo "skip revocation (set TOKEN_REVOKE and REVOKE_COMMAND to check it)"
fi

cleanup
exit "$fail"
