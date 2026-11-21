#!/bin/sh
# Backup of the MLflow PostgreSQL database (tracking, registry, auth, workspaces) and of the
# artifact bucket. Usage: scripts/backup.sh [destination-directory]
#
# Order matters: the database is dumped first, the artifacts afterwards. Artifacts are
# write-once, so every artifact the dump references exists in the mirror (the mirror may hold
# a few newer, unreferenced ones). Run it from the repository root.
set -eu

COMPOSE=${MLFLOW_COMPOSE:-"docker compose -f compose.yml -f compose.gateway.yml -f compose.networks.yml"}
dest=${1:-backups/$(date -u +%Y%m%dT%H%M%SZ)}

umask 077
mkdir -p "$dest/artifacts"
dest=$(cd "$dest" && pwd)

sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$@"; else shasum -a 256 "$@"; fi
}

echo "Dumping PostgreSQL to $dest/mlflow.pgdump" >&2
$COMPOSE exec -T postgres pg_dump -U mlflow -d mlflow --format=custom --no-owner >"$dest/mlflow.pgdump"
[ -s "$dest/mlflow.pgdump" ] || { echo "Empty database dump" >&2; exit 1; }

echo "Mirroring artifacts to $dest/artifacts" >&2
$COMPOSE run --rm -T --no-deps --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$dest/artifacts:/backup/artifacts" minio-tools \
  'mc mirror --quiet --overwrite --remove local/mlflow-artifacts /backup/artifacts'

rm -f "$dest/MANIFEST.sha256"
manifest=$(
  cd "$dest"
  find . -type f | LC_ALL=C sort | while IFS= read -r file; do
    sha256 "$file"
  done
)
printf '%s\n' "$manifest" >"$dest/MANIFEST.sha256"
echo "Backup complete: $dest ($(wc -l <"$dest/MANIFEST.sha256" | tr -d ' ') files)" >&2
