# Operate the isolated InvoiceOps stack locally

This procedure is for a **new, disposable, loopback-only** Compose installation. Infra owns provisioning and maintenance when the local infrastructure does not already exist. Do not reuse an existing Docker project, MLflow registry, volume, environment file, or production endpoint. Run commands only after the separate operations phase is authorized; none are part of static configuration validation.

**Existing-stack evidence (2026-09-29):** the isolated App → Model API → MLflow demonstration, local release smoke and authenticated policy decision passed. That result does not apply to a new installation. The full ML pytest remains incomplete with unknown failure causality. Actual run IDs and the history of blocked attempts are recorded only in `odd/tasks/invoiceops-local-stack.md`, not in this reusable recipe.

## Prepare a private environment

Run from `apps/v2/invoiceops-mlflow` with Docker Compose v2, Node/pnpm and Python/uv already installed. Pick unused loopback ports. Before creating the file, check that `invoiceops-local.env` **does not exist**. Copy `invoiceops-local.env.example` to that new ignored file, set mode `600`, and replace all placeholders with independent, machine-local secrets. Use URL-safe alphanumeric DB/MinIO credentials because PostgreSQL connection URIs embed them without encoding. Never use existing `.env` files or show credentials in logs, tickets, shell tracing or `docker compose config` output. Store the same local MLflow admin credentials for its clients; do not send them to other servers.

```sh
test ! -e invoiceops-local.env || { echo 'Local config already exists; stop'; exit 1; }
cp invoiceops-local.env.example invoiceops-local.env
chmod 600 invoiceops-local.env
# In a private editor, replace every placeholder; the later ops worker generates the secrets.
set -a
. ./invoiceops-local.env
set +a
APP=../invoiceops-app
ML=../invoiceops-ml
API=../invoiceops-model-api
# Confirm this exact network does not already exist before creating it; never reuse another project's network.
docker network create invoiceops-local-link
```

The dedicated external bridge `invoiceops-local-link` is shared by only the new MLflow and App projects. Do not attach other services. Ports default to MLflow `5500`, App DB `5545`, backend `5300`, frontend `5174`, Model API `5800`; all are bound to `127.0.0.1`. The MLflow overlay narrowly allows its internal `mlflow:5000` network hostname plus only the loopback Host values needed by its healthcheck and operator port; it does not allow wildcard/external hosts. For proxied artifact transfers, this local overlay also passes `--uvicorn-opts "--timeout-keep-alive=120"` to `mlflow server` as a bounded mitigation. The existing default/remote Compose stack is unchanged when the overlay is omitted. The MLflow overlay replaces base-file example DB and MinIO credentials; MLflow/Postgres/MinIO and the App each retain their **own project-prefixed named volumes**. Do not use `down -v`.

The isolated App overlay sets `NODE_ENV=development` on the backend solely for this loopback HTTP demonstration: its session cookie has `Secure=false` here, while `HttpOnly`, `SameSite`, and the existing same-origin CSRF check remain in force. Without the overlay, production mode still sets `Secure=true`. The frontend Nginx API proxy forwards the complete request Host including the published port and `X-Forwarded-Proto`, so the existing Origin check can compare the exact loopback origin. This does not authorize exposing the demo over a non-loopback interface.

## Start infrastructure; migrate and seed

```sh
docker compose -p invoiceops-local-mlflow --env-file ./invoiceops-local.env -f compose.yml -f compose.invoiceops-local.yml up --build -d
docker compose -p invoiceops-local-mlflow --env-file ./invoiceops-local.env -f compose.yml -f compose.invoiceops-local.yml ps
curl -sS -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:${LOCAL_MLFLOW_PORT:-5500}/" # anonymous 401
docker compose -p invoiceops-local-app --env-file ./invoiceops-local.env -f "$APP/docker-compose.yml" -f "$APP/compose.invoiceops-local.yml" up -d db
export DATABASE_URL="postgresql://${POSTGRES_USER}:${POSTGRES_PASSWORD}@127.0.0.1:${LOCAL_APP_DB_PORT:-5545}/${POSTGRES_DB}?schema=public"
pnpm -C "$APP" --filter @invoiceops/backend exec prisma migrate deploy
NODE_ENV=development pnpm -C "$APP" --filter @invoiceops/backend local-demonstration
USER_ID=$(docker compose -p invoiceops-local-app --env-file ./invoiceops-local.env -f "$APP/docker-compose.yml" -f "$APP/compose.invoiceops-local.yml" exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'SELECT id FROM "User" WHERE rut = '\''111111111'\''')
ORG_ID=$(docker compose -p invoiceops-local-app --env-file ./invoiceops-local.env -f "$APP/docker-compose.yml" -f "$APP/compose.invoiceops-local.yml" exec -T db psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc 'SELECT id FROM "Organization" WHERE slug = '\''app-07-local-demonstration'\''')
test -n "$USER_ID" && test -n "$ORG_ID" || { echo 'Demo seed missing; stop'; exit 1; }
```

The built-in APP-07 demonstration creates synthetic user RUT `111111111`, organization slug `app-07-local-demonstration`, and **actual generated UUIDs**. Never invent ownership IDs or use personal data. Migration targets only the new App DB. The production backend image does not contain the seed CLI; use the local checkout commands above.

## Train existing local candidates; promote only a Gate PASS

For a **fresh installation or authorized new candidate attempt**, run the canonical RF CLI once to materialize the synthetic dataset and capture its new run ID. Evaluate its Gate for evidence; RF is not compatible with the current local loader and never enters the promotion selector, even if it passes. A Gate FAIL must not prevent notebook 05 from running. Notebook 06 is a teaching/UI comparison, not the local selector. Execute notebooks 03/04/05 in memory, saving outputs **only** in a private temporary directory; do not change notebook sources.

```bash
export MLFLOW_TRACKING_URI="http://127.0.0.1:${LOCAL_MLFLOW_PORT:-5500}"
export MLFLOW_TRACKING_USERNAME="$MLFLOW_AUTH_ADMIN_USERNAME"
export MLFLOW_TRACKING_PASSWORD="$MLFLOW_AUTH_ADMIN_PASSWORD"
unset MLFLOW_WORKSPACE
export INVOICEOPS_ORGANIZATION_SLUG=app-07-local-demonstration
export INVOICEOPS_OWNER_TYPE=user
export INVOICEOPS_OWNER_ID="$USER_ID"
export INVOICEOPS_CREATED_BY_RUT=111111111
export INVOICEOPS_DATASET_DIR="$(realpath "$ML/notebooks")/data/invoice-risk-v1"
export NOTEBOOK_OUTPUT_DIR="$(mktemp -d)"
chmod 700 "$NOTEBOOK_OUTPUT_DIR"
trap 'rm -rf "$NOTEBOOK_OUTPUT_DIR"' EXIT
RF_JSON=$( cd "$ML" && uv run invoiceops-train-candidate \
  --spec config/candidates/rf-candidate-v1.json \
  --dataset-dir notebooks/data/invoice-risk-v1 2>"$NOTEBOOK_OUTPUT_DIR/candidate.stderr" ) || { echo 'Candidate training failed; stop'; exit 1; }
RF_RUN_ID=$(printf '%s' "$RF_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])') || { echo 'Candidate report invalid; stop'; exit 1; }
test -n "$RF_RUN_ID" || { echo 'Candidate run ID missing; stop'; exit 1; }
export RF_RUN_ID
test -f "$INVOICEOPS_DATASET_DIR/train.csv" && test -f "$INVOICEOPS_DATASET_DIR/validation.csv" && test -f "$INVOICEOPS_DATASET_DIR/test.csv" || { echo 'Canonical dataset missing; stop'; exit 1; }
if RF_GATE_JSON=$( cd "$ML" && uv run invoiceops-quality-gate --run-id "$RF_RUN_ID" 2>"$NOTEBOOK_OUTPUT_DIR/gate.stderr" ); then
  RF_GATE_EXIT=0
else
  RF_GATE_EXIT=$?
fi
RF_GATE_STATUS=$(printf '%s' "$RF_GATE_JSON" | python3 -c 'import json,os,sys; d=json.load(sys.stdin); assert d["run_id"] == os.environ["RF_RUN_ID"] and d["version"] == "invoice-risk-gate-v1" and isinstance(d["passed"], bool); print("PASS" if d["passed"] else "FAIL")') || { echo 'RF Gate report invalid; stop'; exit 1; }
if { test "$RF_GATE_EXIT" -eq 0 && test "$RF_GATE_STATUS" = PASS; } || { test "$RF_GATE_EXIT" -eq 1 && test "$RF_GATE_STATUS" = FAIL; }; then
  printf 'RF candidate Gate: %s (not eligible for local promotion)\n' "$RF_GATE_STATUS"
else
  echo 'RF Gate execution inconsistent; stop'; exit 1
fi
HGB_RUN_IDS=()
for NOTEBOOK_NAME in 03_logistic_regression 04_random_forest 05_hist_gradient_boosting; do
  export NOTEBOOK_NAME
  RUN_ID=$( cd "$ML" && uv run python -c '
import ast
import os
from pathlib import Path
import nbformat
from nbclient import NotebookClient

name = os.environ["NOTEBOOK_NAME"]
notebook = nbformat.read(Path("notebooks") / f"{name}.ipynb", as_version=4)
NotebookClient(notebook, timeout=300, kernel_name="python3").execute(cwd=os.environ["NOTEBOOK_OUTPUT_DIR"])
nbformat.write(notebook, Path(os.environ["NOTEBOOK_OUTPUT_DIR"]) / f"{name}.ipynb")
last_code = next(cell for cell in reversed(notebook.cells) if cell.cell_type == "code")
print(ast.literal_eval(last_code.outputs[-1].data["text/plain"]))
' 2>"$NOTEBOOK_OUTPUT_DIR/$NOTEBOOK_NAME.stderr" ) || { echo 'Notebook execution failed; stop'; exit 1; }
  test -n "$RUN_ID" || { echo 'Notebook run ID missing; stop'; exit 1; }
  if [ "$NOTEBOOK_NAME" = 05_hist_gradient_boosting ]; then
    HGB_RUN_IDS+=("$RUN_ID")
  fi
done
test "${#HGB_RUN_IDS[@]}" -gt 0 || { echo 'No HGB run IDs; stop without smoke'; exit 1; }
PROMOTION_JSON=$( cd "$ML" && uv run invoiceops-promote-local \
  --candidate-run-id "${HGB_RUN_IDS[0]}" 2>"$NOTEBOOK_OUTPUT_DIR/promotion.stderr" ) || { echo 'No eligible HGB Gate-PASS candidate; stop without alias or smoke'; exit 1; }
RUN_ID=$(printf '%s' "$PROMOTION_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')
MODEL_VERSION=$(printf '%s' "$PROMOTION_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["version"])')
printf '%s\n' "$PROMOTION_JSON" # run/model/version/alias/gate_metrics only; no credentials or URLs
```

The selector and explicit `--run-id` permit **only HistGradientBoosting** runs from the synthetic owner's experiment, with exact ownership tags, FINISHED status, finite validation metrics, a directory artifact named `model`, and versioned Gate PASS. The loader currently trusts only the exact HGB skops predictor type. Enabling another model family requires enabling and testing its exact loader/skops types **before** adding it to the selector. ML-09 ordering (`validation_recall`, `validation_precision`, `validation_f1`, descending) chooses among eligible PASS runs, with ascending run ID for ties; test metrics never decide. The winner's Gate is re-run before alias assignment. If none passes, **stop without registering, assigning `champion`, starting Model API/App or running smoke**. The promotion CLI refuses ownership mismatch, missing artifact, non-loopback URI, non-default workspace, or a distinct `MLFLOW_REGISTRY_URI`. Only isolated MLflow receives `invoice-review-production@champion`; repeated promotion of the same run reuses its version. A loopback URL is a necessary guard, not proof against a deliberately configured local reverse proxy: verify the Compose project and port before executing. Remote/production promotion remains human-controlled. Never set a remote registry URI for this step.

## Start App and Model API; verify release path

```sh
docker compose -p invoiceops-local-app --env-file ./invoiceops-local.env -f "$APP/docker-compose.yml" -f "$APP/compose.invoiceops-local.yml" up --build --wait --wait-timeout 180 model-api backend frontend || { echo 'Model API did not become healthy; stop before smoke'; exit 1; }
export MODEL_API_BASE_URL="http://127.0.0.1:${LOCAL_MODEL_API_PORT:-5800}"
export MODEL_API_ADMIN_TOKEN="$LOCAL_MODEL_API_ADMIN_TOKEN"
export MODEL_RELEASE_EXPECTED_MODEL_VERSION="$MODEL_VERSION"
export MODEL_RELEASE_EXPECTED_RUN_ID="$RUN_ID"
( cd "$API" && MODEL_RELEASE_HTTP_TIMEOUT_SECONDS=45 python3 scripts/model_release_smoke.py )
```

The local Model API healthcheck requires `GET /models/invoice-review/health` to succeed, which means the `champion` model is loaded; the generic `/health` does not prove this. The backend waits for Model API to be healthy, and `up --wait` must succeed before running the smoke. If it times out or reports an unhealthy service, stop: do not run the smoke until the model is loaded and Model API is healthy. The existing release smoke script executes refresh → health → predict, checks that all endpoints report the promoted `model_version` and `run_id`, and validates a finite probability. Refresh changes only the disposable local Model API process. Do not point this operation at another instance.

The 45-second override applies **only** to the local smoke process and each of its HTTP requests. It gives bounded margin above the Model API's finite 30-second refresh cap without weakening the smoke's 10-second default for remote use; the smoke's child-process watchdog remains in place. If full refresh in the local `proxy-http` mode still exceeds 45 seconds, stop and report the result rather than raising the limit again. This command is an operator procedure, not evidence that the smoke has passed.

The App overlay opts the Model API into `MODEL_API_ARTIFACT_DOWNLOAD_MODE=proxy-http` for this isolated local stack only. This is a mitigation for the observed SDK artifact-download stall, **not** a diagnosis of its root cause or proof of a successful runtime model load. It resolves the pinned version's run and lists its `model` files through MLflow metadata, then downloads only `mlflow-artifacts:/` run files from the configured `http://mlflow:5000` proxy with the existing MLflow Basic Auth pair. Redirects, other origins and unsafe paths are refused; transfers have 64-file, 32 MiB per-file, 64 MiB total, 8-second per-request and 25-second overall HTTP deadlines. A private temporary directory is removed after model loading or failure. No MinIO endpoint or credential is configured on the Model API. Omitting the overlay leaves the `sdk` default and existing `models:/` loading unchanged; to roll back locally, set the mode to `sdk` in the local overlay and recreate only the disposable Model API service after separate operational approval. Model-loaded health, refresh smoke and App decision remain unverified until an authorized operations phase actually runs them.

For the isolated local HistGradientBoosting artifact, the `proxy-http` loader inspects `model/model.skops` metadata before deserialization. It trusts exactly `sklearn.ensemble._hist_gradient_boosting.predictor.TreePredictor` via an explicit `skops.io.load(..., trusted=[...])` list. Missing or additional untrusted types fail closed without calling the deserializer; no wildcard, global trust setting, or SDK-mode policy changes are made. Loader failures remain generic in API responses and logs. Runtime model-loaded health and release smoke are still pending.

For the App decision, log in as the built-in demonstration user (`rut=111111111`, password `app-07-local-demonstration`). Use the local frontend at `http://127.0.0.1:5174` and its invoice workspace (or the authenticated backend endpoint) with organization ID `$ORG_ID`, `ownerType=user`, `ownerId=$USER_ID`, invoice `DEMO-RULE-AUTO-POLICY-MANUAL`, and `PROBABILITY_POLICY` / `ml-policy-v1`. Check the decision audit event has `policyProbabilitySource=MODEL_API`, non-null probability, and matching `modelRunId`/`modelVersion`, **not** `MODEL_API_FALLBACK`. The endpoint is `POST /invoices/DEMO-RULE-AUTO-POLICY-MANUAL/decision?organizationId=<ORG_ID>&ownerType=user&ownerId=<USER_ID>` with JSON `{"mode":"PROBABILITY_POLICY","policyVersion":"ml-policy-v1"}` and a same-origin `Origin` header and session cookie; do not publish the cookie. Reseed between repeated decision attempts to restore demo invoices to `PENDING`.

## Preserve and hand off

For actual 2026-09-29 outcomes, run IDs, Gate results, blocked attempts and subsequent recovery, see `odd/tasks/invoiceops-local-stack.md`. Do not reuse those identifiers in a fresh installation.

Leave both projects running after successful validation. If an operator explicitly chooses to stop later, use `docker compose -p <dedicated-project> --env-file ./invoiceops-local.env -f <base> -f <overlay> down` separately for each project, **without `--volumes`**; data persists. Never remove pre-existing networks, projects or volumes. Record operational outcomes against `odd/tasks/invoiceops-local-stack.md` only after the ops phase actually runs. If image pulls, startup, authentication, Gate or smoke fail, stop dependent steps and report the blocker; never claim a pending criterion passed.
