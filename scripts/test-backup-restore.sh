#!/bin/sh
# Proves the backup: seed data -> backup -> DESTROY the volumes -> restore into fresh volumes
# -> runs, metrics, registry entries and artifact bytes are identical.
# Needs Docker. Uses its own throw-away compose project and random secrets; run from the
# repository root: scripts/test-backup-restore.sh
set -eu

project="mlflow-br-$$"
work=$(mktemp -d)
rand() { openssl rand -hex 24; }

export MLFLOW_AUTH_ADMIN_USERNAME=br-admin
MLFLOW_AUTH_ADMIN_PASSWORD=$(rand); export MLFLOW_AUTH_ADMIN_PASSWORD
MLFLOW_FLASK_SERVER_SECRET_KEY=$(rand); export MLFLOW_FLASK_SERVER_SECRET_KEY
MLFLOW_DB_PASSWORD=$(rand); export MLFLOW_DB_PASSWORD
MINIO_ROOT_USER=br$(rand | cut -c1-12)
export MINIO_ROOT_USER
MINIO_ROOT_PASSWORD=$(rand); export MINIO_ROOT_PASSWORD
INTERNAL_INTROSPECTION_SECRET=$(rand); export INTERNAL_INTROSPECTION_SECRET
export GATEWAY_PUBLIC_ORIGIN=https://mlflow.example.test
export APP_PUBLIC_URL=https://app.example.test

# The overlay joins two external networks (reverse proxy, InvoiceOps backend): create throw-away ones.
export GATEWAY_EDGE_NETWORK="$project-edge" GATEWAY_BACKEND_NETWORK="$project-api"
docker network create "$GATEWAY_EDGE_NETWORK" >/dev/null
docker network create "$GATEWAY_BACKEND_NETWORK" >/dev/null
export MLFLOW_COMPOSE="docker compose -p $project -f compose.yml -f compose.gateway.yml -f compose.networks.yml"
COMPOSE=$MLFLOW_COMPOSE

# The images compose builds for this throw-away project are named "$project-<service>". `down --rmi
# local` leaves them behind when a step fails half way, so remove exactly those (never other images).
remove_project_images() {
  docker image ls --filter "reference=$project-*" --format '{{.ID}}' | sort -u |
    while read -r image_id; do
      docker image rm -f "$image_id" >/dev/null 2>&1 || true
    done
}

cleanup() {
  $COMPOSE --profile tools down -v --rmi local --remove-orphans >/dev/null 2>&1 || true
  remove_project_images
  docker network rm "$GATEWAY_EDGE_NETWORK" "$GATEWAY_BACKEND_NETWORK" >/dev/null 2>&1 || true
  rm -rf "$work"
}
trap cleanup EXIT INT TERM

SEED_PY='
import hashlib, json, os, sys, tempfile
import mlflow
from mlflow import MlflowClient

mode = sys.argv[1]
os.environ["MLFLOW_TRACKING_URI"] = "http://127.0.0.1:5000"
os.environ["MLFLOW_TRACKING_USERNAME"] = os.environ["MLFLOW_AUTH_ADMIN_USERNAME"]
os.environ["MLFLOW_TRACKING_PASSWORD"] = os.environ["MLFLOW_AUTH_ADMIN_PASSWORD"]
os.environ["GIT_PYTHON_REFRESH"] = "quiet"
WS = "br-test"
os.environ["MLFLOW_WORKSPACE"] = WS
client = MlflowClient()

def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()

if mode == "seed":
    mlflow.create_workspace(name=WS, description="backup test")
    exp = mlflow.set_experiment("br-exp")
    blob = os.urandom(3 * 1024 * 1024)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "blob.bin")
        open(path, "wb").write(blob)
        with mlflow.start_run() as run:
            mlflow.log_param("alpha", "0.5")
            mlflow.log_metric("auc", 0.87, step=1)
            mlflow.log_artifact(path)
    client.create_registered_model("br-model")
    mv = client.create_model_version("br-model", f"{run.info.artifact_uri}", run_id=run.info.run_id)
    print(json.dumps({"run_id": run.info.run_id, "version": mv.version, "sha": hashlib.sha256(blob).hexdigest(), "experiment_id": exp.experiment_id}))
else:
    fp = json.loads(os.environ["FINGERPRINT"])
    run = client.get_run(fp["run_id"])
    assert run.data.params["alpha"] == "0.5", run.data.params
    assert abs(run.data.metrics["auc"] - 0.87) < 1e-9, run.data.metrics
    mv = client.get_model_version("br-model", fp["version"])
    assert mv.run_id == fp["run_id"]
    with tempfile.TemporaryDirectory() as d:
        got = mlflow.artifacts.download_artifacts(run_id=fp["run_id"], artifact_path="blob.bin", dst_path=d)
        assert sha(got) == fp["sha"], "artifact bytes differ"
    print("verified")
'

run_py() { $COMPOSE exec -T -e FINGERPRINT="${FINGERPRINT:-}" mlflow python - "$1" <<PY
$SEED_PY
PY
}

# The first request after a (re)start can take longer than the MLflow client's 3 s probe timeout.
warm_up() {
  warm=0
  until $COMPOSE exec -T mlflow python -c "
import urllib.error, urllib.request
try:
    urllib.request.urlopen('http://127.0.0.1:5000/api/3.0/mlflow/server-info', timeout=120)
except urllib.error.HTTPError:
    pass
" >/dev/null 2>&1; do
  warm=$((warm + 1))
  [ "$warm" -lt 5 ] || { echo "MLflow did not answer" >&2; exit 1; }
  sleep 3
  done
}

counts() {
  $COMPOSE exec -T postgres psql -U mlflow -d mlflow -tA -c "
    select (select count(*) from experiments) || ',' || (select count(*) from runs) || ',' ||
           (select count(*) from metrics) || ',' || (select count(*) from params) || ',' ||
           (select count(*) from registered_models) || ',' || (select count(*) from model_versions) || ',' ||
           (select count(*) from workspaces)"
}

echo "== start fresh stack ($project)"
$COMPOSE up -d --build --wait mlflow
warm_up
echo "== seed"
seed_output=$(run_py seed) || { echo "seeding failed" >&2; exit 1; }
FINGERPRINT=$(printf '%s\n' "$seed_output" | tail -n 1)
export FINGERPRINT
echo "$FINGERPRINT" >"$work/fingerprint.json"
before=$(counts)
echo "rows before: $before"
run_py verify

echo "== backup"
scripts/backup.sh "$work/backup"

echo "== destroy containers and volumes"
$COMPOSE --profile tools down -v --remove-orphans
left=$(docker volume ls -q --filter "label=com.docker.compose.project=$project" | wc -l | tr -d ' ')
[ "$left" = "0" ] || { echo "volumes survived: $left" >&2; exit 1; }

echo "== fresh volumes: start empty stack"
$COMPOSE up -d --build --wait mlflow
fresh=$(counts)
[ "$fresh" != "$before" ] || { echo "fresh stack unexpectedly already holds the data" >&2; exit 1; }
if run_py verify >/dev/null 2>&1; then echo "data present before restore?" >&2; exit 1; fi

echo "== restore"
PRE_RESTORE_DIR="$work/pre-restore" scripts/restore.sh "$work/backup" --force
[ -s "$work/pre-restore/mlflow.pgdump" ] && [ -f "$work/pre-restore/MANIFEST.sha256" ] || { echo "no pre-restore backup was taken" >&2; exit 1; }

warm_up
after=$(counts)
echo "rows after:  $after"
[ "$before" = "$after" ] || { echo "row counts differ: $before vs $after" >&2; exit 1; }
run_py verify

echo "== gateway smoke (restored stack): healthy, anonymous 401, nothing published on the host"
$COMPOSE exec -T gateway python - <<'PY'
import urllib.error, urllib.request
def status(path):
    try:
        return urllib.request.urlopen("http://127.0.0.1:8080" + path, timeout=5).status
    except urllib.error.HTTPError as error:
        return error.code
assert status("/_gateway/healthz") == 200
assert status("/api/2.0/mlflow/experiments/search?max_results=1") == 401
assert status("/") == 401
print("gateway ok")
PY
published=$(docker ps --filter "label=com.docker.compose.project=$project" --format '{{.Ports}}' | grep -c -- '->' || true)
[ "$published" = "0" ] || { echo "ports published on the host: $published" >&2; exit 1; }
echo "BACKUP/RESTORE TEST PASSED (rows $before)"
