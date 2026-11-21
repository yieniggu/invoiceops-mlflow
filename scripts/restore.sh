#!/bin/sh
# Restore a backup made by scripts/backup.sh into the current (fresh) volumes.
# Usage: scripts/restore.sh <backup-directory> [--force]
#
# Refuses to touch a database that already has MLflow tables unless --force is given
# (--force drops and recreates the objects contained in the dump, in ONE transaction: if any
# statement fails nothing is changed). The manifest must list exactly the files of the directory. Stops mlflow and the
# gateway while restoring (aborting if they cannot be stopped) and starts the whole stack again
# at the end. When the target is not empty, a pre-restore backup of its CURRENT state is taken
# first (PRE_RESTORE_DIR, default backups/pre-restore-<UTC timestamp>) and the restore aborts if
# that backup fails. The artifact bucket is mirrored with --remove, so after the restore it holds
# exactly what the backup holds: objects written after the backup are deleted from the bucket
# (they are in the pre-restore backup), which keeps the bucket consistent with the restored database.
set -eu

COMPOSE=${MLFLOW_COMPOSE:-"docker compose -f compose.yml -f compose.gateway.yml -f compose.networks.yml"}
src=${1:?Usage: scripts/restore.sh <backup-directory> [--force]}
force=${2:-}

[ -f "$src/mlflow.pgdump" ] && [ -f "$src/MANIFEST.sha256" ] || {
  echo "Not a backup directory: $src" >&2
  exit 1
}
src=$(cd "$src" && pwd)

sha256_check() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum -c --quiet MANIFEST.sha256; else shasum -a 256 -c --quiet MANIFEST.sha256; fi
}
(cd "$src" && sha256_check) || { echo "Backup checksum verification failed" >&2; exit 1; }

# The manifest must describe the directory exactly: a file that is not listed (an injected or
# leftover artifact) would be mirrored into the bucket without ever being checked.
actual=$(cd "$src" && find . ! -type d ! -path ./MANIFEST.sha256 | LC_ALL=C sort)
listed=$(sed 's/^[0-9a-fA-F]* [ *]//' "$src/MANIFEST.sha256" | LC_ALL=C sort)
[ "$actual" = "$listed" ] || {
  echo "Backup manifest does not match the directory contents (extra or missing files); refusing to restore" >&2
  exit 1
}

$COMPOSE stop gateway mlflow >/dev/null || {
  echo "Could not stop gateway and mlflow; aborting before touching any data" >&2
  exit 1
}
$COMPOSE up -d --wait postgres minio

tables=$($COMPOSE exec -T postgres psql -U mlflow -d mlflow -tAc \
  "select count(*) from information_schema.tables where table_schema = 'public'")
if [ "$tables" != "0" ] && [ "$force" != "--force" ]; then
  echo "The target database is not empty ($tables tables). Use fresh volumes or pass --force." >&2
  exit 1
fi

if [ "$tables" != "0" ]; then
  pre=${PRE_RESTORE_DIR:-backups/pre-restore-$(date -u +%Y%m%dT%H%M%SZ)}
  echo "Backing up the current state to $pre before overwriting it" >&2
  "$(dirname "$0")/backup.sh" "$pre" || {
    echo "Pre-restore backup failed; aborting before touching any data" >&2
    exit 1
  }
fi

echo "Restoring PostgreSQL" >&2
$COMPOSE exec -T postgres pg_restore -U mlflow -d mlflow --clean --if-exists --no-owner \
  --single-transaction --exit-on-error <"$src/mlflow.pgdump"

echo "Restoring artifacts" >&2
$COMPOSE run --rm -T --no-deps --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$src/artifacts:/backup/artifacts:ro" minio-tools \
  'mc mb --ignore-existing local/mlflow-artifacts >/dev/null && mc mirror --quiet --overwrite --remove /backup/artifacts local/mlflow-artifacts'

$COMPOSE up -d --wait
echo "Restore complete from $src" >&2
