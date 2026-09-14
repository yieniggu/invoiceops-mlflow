#!/bin/sh
set -eu

wait_for_mlflow() {
  attempt=1
  while [ "$attempt" -le 30 ]; do
    container_id="$(docker compose ps -q mlflow)"
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
  docker compose exec -T mlflow sh -c '
    export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
    export MLFLOW_TRACKING_USERNAME="$MLFLOW_AUTH_ADMIN_USERNAME"
    export MLFLOW_TRACKING_PASSWORD="$MLFLOW_AUTH_ADMIN_PASSWORD"
    export MLFLOW_WORKSPACE="$3"
    export GIT_PYTHON_REFRESH=quiet
    exec python - "$1" "$2" "$3"
  ' sh "$1" "$2" "$3" <<'PY'
import sys
import tempfile
from pathlib import Path

import mlflow

run_id, marker, workspace = sys.argv[1:]
client = mlflow.tracking.MlflowClient()
run = client.get_run(run_id)
if run.data.tags.get("smoke.marker") != marker:
    raise SystemExit("Run marker was not recovered.")
experiment = client.get_experiment(run.info.experiment_id)
if experiment.workspace != workspace:
    raise SystemExit("MLFLOW_WORKSPACE was not selected.")

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
marker="mlflow-workspaces-smoke-$(date +%s)-$$"
workspace_one="organization-one-$marker"
workspace_two="organization-two-$marker"
run_id_file="/tmp/$marker.run-id"

docker compose exec -T mlflow sh -c '
  export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
  export MLFLOW_TRACKING_USERNAME="$MLFLOW_AUTH_ADMIN_USERNAME"
  export MLFLOW_TRACKING_PASSWORD="$MLFLOW_AUTH_ADMIN_PASSWORD"
  exec python - "$@"
' sh "$workspace_one" "$workspace_two" <<'PY'
import sys

import mlflow

workspace_one, workspace_two = sys.argv[1:]
mlflow.create_workspace(name=workspace_one, description="Workspace smoke organization one")
mlflow.create_workspace(name=workspace_two, description="Workspace smoke organization two")
names = {workspace.name for workspace in mlflow.list_workspaces()}
if {workspace_one, workspace_two} - names:
    raise SystemExit("Created workspaces were not listed.")
PY

docker compose exec -T mlflow sh -c '
  export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
  export MLFLOW_TRACKING_USERNAME="$MLFLOW_AUTH_ADMIN_USERNAME"
  export MLFLOW_TRACKING_PASSWORD="$MLFLOW_AUTH_ADMIN_PASSWORD"
  export MLFLOW_WORKSPACE="$3"
  export GIT_PYTHON_REFRESH=quiet
  exec python - "$@"
' sh "$marker" "$run_id_file" "$workspace_one" <<'PY'
import sys

import mlflow

marker, run_id_file, workspace = sys.argv[1:]
experiment = mlflow.set_experiment("mlflow-workspaces-smoke")
if experiment.workspace != workspace:
    raise SystemExit("MLFLOW_WORKSPACE was not selected.")

with mlflow.start_run(tags={"smoke.marker": marker}) as run:
    mlflow.log_text(marker + "\n", "smoke.txt")
    with open(run_id_file, "w", encoding="utf-8") as file:
        file.write(run.info.run_id)
PY
run_id="$(docker compose exec -T mlflow cat "$run_id_file")"
docker compose exec -T mlflow rm -f "$run_id_file"

run_client_check "$run_id" "$marker" "$workspace_one"
docker compose up -d --force-recreate --no-deps mlflow
wait_for_mlflow
run_client_check "$run_id" "$marker" "$workspace_one"
printf '%s\n' "MLflow workspace smoke passed for run $run_id."
