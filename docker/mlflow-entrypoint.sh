#!/bin/sh
set +x
set -eu

: "${MLFLOW_AUTH_CONFIG_PATH:?MLFLOW_AUTH_CONFIG_PATH must be set}"
: "${MLFLOW_AUTH_DATABASE_URI:?MLFLOW_AUTH_DATABASE_URI must be set}"
: "${MLFLOW_BACKEND_STORE_URI:?MLFLOW_BACKEND_STORE_URI must be set}"
: "${MLFLOW_AUTH_ADMIN_USERNAME:?MLFLOW_AUTH_ADMIN_USERNAME must be set}"
: "${MLFLOW_AUTH_ADMIN_PASSWORD:?MLFLOW_AUTH_ADMIN_PASSWORD must be set}"
: "${MLFLOW_FLASK_SERVER_SECRET_KEY:?MLFLOW_FLASK_SERVER_SECRET_KEY must be set}"

case "$MLFLOW_AUTH_ADMIN_USERNAME" in
  replace-with-initial-admin-username)
    echo "MLFLOW_AUTH_ADMIN_USERNAME must not use the example placeholder" >&2
    exit 1
    ;;
esac

case "$MLFLOW_AUTH_ADMIN_PASSWORD" in
  replace-with-generated-password)
    echo "MLFLOW_AUTH_ADMIN_PASSWORD must not use the example placeholder" >&2
    exit 1
    ;;
esac

case "$MLFLOW_FLASK_SERVER_SECRET_KEY" in
  replace-with-generated-secret-key)
    echo "MLFLOW_FLASK_SERVER_SECRET_KEY must not use the example placeholder" >&2
    exit 1
    ;;
esac

# Placeholders from example files must never reach a running service: the database password
# (inside the URIs) and the MinIO/S3 credentials are rejected like the admin credentials.
case "$MLFLOW_BACKEND_STORE_URI:$MLFLOW_AUTH_DATABASE_URI:${AWS_ACCESS_KEY_ID:-}:${AWS_SECRET_ACCESS_KEY:-}" in
  *replace-with*|*REPLACE_WITH*|*changeme*)
    echo "MLflow database or MinIO credentials must not use the example placeholder" >&2
    exit 1
    ;;
esac

# Production overlay (compose.gateway.yml sets MLFLOW_REJECT_PUBLIC_CREDENTIALS=1): the throwaway
# credentials published in mlflow-local.example.env are for a local, standalone compose only and
# must never protect a shared host. tests/test_compose_and_docs_contracts.py keeps these literals equal to
# that file.
if [ "${MLFLOW_REJECT_PUBLIC_CREDENTIALS:-}" = "1" ]; then
  case "$MLFLOW_BACKEND_STORE_URI:$MLFLOW_AUTH_DATABASE_URI:${AWS_SECRET_ACCESS_KEY:-}" in
    *mlflow-local-password*|*minio-local-password*)
      echo "MLflow database or MinIO credentials must not use the public local example values" >&2
      exit 1
      ;;
  esac
  case "${AWS_ACCESS_KEY_ID:-}" in
    minio)
      echo "MLflow database or MinIO credentials must not use the public local example values" >&2
      exit 1
      ;;
  esac
fi

umask 077
mkdir -p "$(dirname "$MLFLOW_AUTH_CONFIG_PATH")"

cat >"$MLFLOW_AUTH_CONFIG_PATH" <<EOF
[mlflow]
database_uri = ${MLFLOW_AUTH_DATABASE_URI}
admin_username = ${MLFLOW_AUTH_ADMIN_USERNAME}
admin_password = ${MLFLOW_AUTH_ADMIN_PASSWORD}
default_permission = READ
grant_default_workspace_access = false
EOF

# The backend and basic-auth stores share PostgreSQL. Initialize both schemas
# before starting concurrent server workers; never print database URLs or secrets.
python "$(dirname "$0")/mlflow-migrate.py" tracking >/dev/null 2>&1 || {
  echo 'MLflow backend database migration failed' >&2
  exit 1
}
python "$(dirname "$0")/mlflow-migrate.py" auth >/dev/null 2>&1 || {
  echo 'MLflow authorization database migration failed' >&2
  exit 1
}

exec "$@"
