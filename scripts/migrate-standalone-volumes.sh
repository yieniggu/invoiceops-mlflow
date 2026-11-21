#!/bin/sh
# One-off copy of the data volumes of a standalone checkout created BEFORE the volume keys were
# prefixed (compose.yml used `postgres_data` / `minio_data`; now `mlflow_postgres_data` /
# `mlflow_minio_data`, so the stack project `invoiceops` gets invoiceops_mlflow_*).
#
#   scripts/migrate-standalone-volumes.sh [SOURCE_PROJECT [TARGET_PROJECT]]
#
# Defaults: both projects are `invoiceops-mlflow` (the standalone project name), i.e.
#   invoiceops-mlflow_postgres_data -> invoiceops-mlflow_mlflow_postgres_data
#   invoiceops-mlflow_minio_data    -> invoiceops-mlflow_mlflow_minio_data
# To move standalone data into the unified stack: scripts/migrate-standalone-volumes.sh
# invoiceops-mlflow invoiceops. The source volumes are only read and are never deleted.
# Stop the stack first (`docker compose down`, WITHOUT -v). Refuses to overwrite a non-empty
# target volume or to copy while a container still uses a source volume. Exits non-zero when
# nothing was copied (typically a mistyped or non-existent SOURCE_PROJECT), so a wrong argument
# can never look like a successful migration.
set -eu

src=${1:-invoiceops-mlflow}
dst=${2:-invoiceops-mlflow}
IMAGE=${MIGRATE_IMAGE:-postgres:16.9-alpine}
copied=0

copy() {
  from=$1
  to=$2
  docker volume inspect "$from" >/dev/null 2>&1 || {
    echo "Source volume $from does not exist; skipping it" >&2
    return 0
  }
  if [ -n "$(docker ps -q --filter "volume=$from")" ]; then
    echo "A running container still uses $from: stop the stack first" >&2
    return 1
  fi
  if docker volume inspect "$to" >/dev/null 2>&1; then
    left=$(docker run --rm -v "$to:/to:ro" --entrypoint sh "$IMAGE" -c 'ls -A /to | head -n 1')
    [ -z "$left" ] || {
      echo "Target volume $to already holds data; refusing to overwrite it" >&2
      return 1
    }
  else
    docker volume create "$to" >/dev/null
  fi
  echo "Copying $from -> $to" >&2
  docker run --rm -v "$from:/from:ro" -v "$to:/to" --entrypoint sh "$IMAGE" -c 'cp -a /from/. /to/'
  copied=$((copied + 1))
}

copy "${src}_postgres_data" "${dst}_mlflow_postgres_data"
copy "${src}_minio_data" "${dst}_mlflow_minio_data"
[ "$copied" -gt 0 ] || {
  echo "Nothing was copied: project '$src' has no ${src}_postgres_data or ${src}_minio_data volume" >&2
  exit 1
}
echo "Done ($copied volume(s) copied). Start the stack and check it; remove the old volumes yourself once satisfied." >&2
