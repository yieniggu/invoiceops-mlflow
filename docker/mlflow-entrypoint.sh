#!/bin/sh
set -eu

: "${MLFLOW_AUTH_CONFIG_PATH:?MLFLOW_AUTH_CONFIG_PATH must be set}"
: "${MLFLOW_AUTH_DATABASE_URI:?MLFLOW_AUTH_DATABASE_URI must be set}"
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

exec "$@"
