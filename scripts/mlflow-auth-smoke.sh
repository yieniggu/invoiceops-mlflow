#!/bin/sh
set -eu

: "${MLFLOW_AUTH_ADMIN_USERNAME:?Load .env before running this script}"
: "${MLFLOW_AUTH_ADMIN_PASSWORD:?Load .env before running this script}"

smoke_env_file="${MLFLOW_SMOKE_ENV_FILE:-.env}"

compose() {
  docker compose --env-file "$smoke_env_file" "$@"
}

export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
export MLFLOW_TRACKING_USERNAME="$MLFLOW_AUTH_ADMIN_USERNAME"
export MLFLOW_TRACKING_PASSWORD="$MLFLOW_AUTH_ADMIN_PASSWORD"
export GIT_PYTHON_REFRESH=quiet

wait_for_mlflow() {
  attempt=1
  while [ "$attempt" -le 30 ]; do
    container_id="$(compose ps -q mlflow)"
    if [ -n "$container_id" ] && [ "$(docker inspect --format '{{.State.Health.Status}}' "$container_id")" = "healthy" ]; then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 2
  done

  printf '%s\n' 'MLflow did not become healthy.' >&2
  return 1
}

run_client_check() {
  compose exec -T \
    -e MLFLOW_TRACKING_URI \
    -e MLFLOW_TRACKING_USERNAME \
    -e MLFLOW_TRACKING_PASSWORD \
    mlflow python - "$1" "$2" <<'PY'
import sys
import tempfile
from pathlib import Path

import mlflow

run_id, marker = sys.argv[1:]
client = mlflow.tracking.MlflowClient()
run = client.get_run(run_id)
if run.data.tags.get("smoke.marker") != marker:
    raise SystemExit("Run marker was not recovered.")

with tempfile.TemporaryDirectory() as directory:
    artifact = Path(
        mlflow.artifacts.download_artifacts(
            run_id=run_id,
            artifact_path="smoke.txt",
            dst_path=directory,
        )
    )
    if artifact.read_text(encoding="utf-8") != marker + "\n":
        raise SystemExit("Artifact content was not recovered.")
PY
}

wait_for_mlflow
marker="mlflow-auth-smoke-$(date +%s)-$$"
run_id_file="/tmp/$marker.run-id"
compose exec -T \
  -e MLFLOW_TRACKING_URI \
  -e MLFLOW_TRACKING_USERNAME \
  -e MLFLOW_TRACKING_PASSWORD \
  -e GIT_PYTHON_REFRESH \
  mlflow python - "$marker" "$run_id_file" <<'PY'
import sys

import mlflow

marker, run_id_file = sys.argv[1:]
mlflow.set_experiment("mlflow-auth-smoke")
with mlflow.start_run(tags={"smoke.marker": marker}) as run:
    mlflow.log_text(marker + "\n", "smoke.txt")
    with open(run_id_file, "w", encoding="utf-8") as file:
        file.write(run.info.run_id)
PY
run_id="$(compose exec -T mlflow cat "$run_id_file")"
compose exec -T mlflow rm -f "$run_id_file"

run_client_check "$run_id" "$marker"
compose up -d --force-recreate --no-deps mlflow
wait_for_mlflow
run_client_check "$run_id" "$marker"
printf '%s\n' "MLflow authenticated smoke passed for run $run_id."
