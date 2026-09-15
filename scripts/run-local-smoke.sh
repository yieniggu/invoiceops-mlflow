#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  printf '%s\n' 'Usage: ./scripts/run-local-smoke.sh path/to/smoke.env' >&2
  exit 64
fi

env_file="$1"
case "$(basename "$env_file")" in
  .env|.env.*)
    printf '%s\n' 'Refusing to use .env files. Supply a dedicated smoke.env file.' >&2
    exit 64
    ;;
esac

if [ ! -r "$env_file" ]; then
  printf '%s\n' "Smoke environment file is not readable: $env_file" >&2
  exit 66
fi

project_token_file="$(mktemp "${TMPDIR:-/tmp}/invoiceops-mlflow-int02-smoke.XXXXXX")"
project_name="invoiceops-mlflow-int02-smoke-${project_token_file##*.}"
project_name="$(printf '%s' "$project_name" | tr '[:upper:]' '[:lower:]')"
receipt_file="LOCAL_SMOKE_RECEIPT.md"
receipt_tmp="$(mktemp "${TMPDIR:-/tmp}/invoiceops-mlflow-smoke-receipt.XXXXXX")"
started_at="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
git_revision="$(git rev-parse HEAD 2>/dev/null || printf '%s' 'unavailable')"
finalized=0
interrupted_by=''
compose() {
  docker compose --env-file "$env_file" -p "$project_name" \
    -f compose.yml -f compose.smoke.yml "$@"
}

write_receipt_header() {
  cat >"$receipt_tmp" <<EOF
# Recibo de ejecución local de MLflow

- Inicio UTC: $started_at
- Revisión Git de invoiceops-mlflow: $git_revision
- Proyecto Compose aislado: $project_name
- Archivo de entorno: dedicado y no incluido en el recibo

| Escenario | Comando ejecutado | Código de salida | Aserción de éxito |
| --- | --- | ---: | --- |
EOF
}

record_scenario() {
  scenario="$1"
  command="$2"
  assertion="$3"
  shift 3

  if "$@" >/dev/null 2>&1; then
    exit_code=0
  else
    exit_code=$?
  fi

  printf '| %s | `%s` | %s | %s |\n' \
    "$scenario" "$command" "$exit_code" "$assertion" >>"$receipt_tmp"
  [ "$exit_code" -eq 0 ]
}

cleanup() {
  smoke_exit_code="$1"
  if [ "$finalized" -ne 0 ]; then
    return
  fi
  finalized=1
  trap - EXIT HUP INT TERM

  if compose down --volumes --remove-orphans >/dev/null 2>&1; then
    cleanup_exit_code=0
    cleanup_disposition='Se eliminaron los contenedores, red y volúmenes del proyecto Compose aislado.'
  else
    cleanup_exit_code=$?
    cleanup_disposition='La limpieza del proyecto Compose aislado falló; revisar Docker localmente.'
  fi

  if [ -n "$interrupted_by" ]; then
    final_state="Interrumpido por señal $interrupted_by."
  else
    final_state="Finalizado con código de salida $smoke_exit_code."
  fi

  cat >>"$receipt_tmp" <<EOF

## Resultado final

- Estado: $final_state
- Código de salida del arnés: $smoke_exit_code
- Comando de limpieza: docker compose -p $project_name down --volumes --remove-orphans
- Código de salida de limpieza: $cleanup_exit_code
- Disposición: $cleanup_disposition
EOF
  mv "$receipt_tmp" "$receipt_file"
  rm -f "$project_token_file"
}

handle_signal() {
  interrupted_by="$1"
  trap - HUP INT TERM
  exit "$2"
}

write_receipt_header
trap 'cleanup "$?"' EXIT
trap 'handle_signal HUP 129' HUP
trap 'handle_signal INT 130' INT
trap 'handle_signal TERM 143' TERM

set -a
. "$env_file"
set +a

: "${MLFLOW_AUTH_ADMIN_USERNAME:?MLFLOW_AUTH_ADMIN_USERNAME must be set in the smoke environment}"
: "${MLFLOW_AUTH_ADMIN_PASSWORD:?MLFLOW_AUTH_ADMIN_PASSWORD must be set in the smoke environment}"
: "${MLFLOW_FLASK_SERVER_SECRET_KEY:?MLFLOW_FLASK_SERVER_SECRET_KEY must be set in the smoke environment}"

case "$MLFLOW_AUTH_ADMIN_USERNAME:$MLFLOW_AUTH_ADMIN_PASSWORD:$MLFLOW_FLASK_SERVER_SECRET_KEY" in
  *replace-with-initial-admin-username*|*replace-with-generated-password*|*replace-with-generated-secret-key*)
    printf '%s\n' 'Smoke environment still contains an example placeholder.' >&2
    exit 65
    ;;
esac

record_scenario \
  'configuración de Compose' \
  'docker compose --env-file <smoke.env dedicado> -p <proyecto aislado> -f compose.yml -f compose.smoke.yml config --quiet' \
  'La configuración aislada se validó sin renderizarla en el recibo.' \
  compose config --quiet
record_scenario \
  'inicio aislado de Compose' \
  'docker compose --env-file <smoke.env dedicado> -p <proyecto aislado> -f compose.yml -f compose.smoke.yml up --build -d' \
  'El stack aislado se inició.' \
  compose up --build -d

record_scenario \
  'persistencia autenticada' \
  'MLFLOW_SMOKE_ENV_FILE=<smoke.env dedicado> COMPOSE_FILE=compose.yml:compose.smoke.yml COMPOSE_PROJECT_NAME=<proyecto aislado> ./scripts/mlflow-auth-smoke.sh' \
  'Un run y su artefacto autenticados se recuperaron antes y después de recrear sólo MLflow.' \
  env MLFLOW_SMOKE_ENV_FILE="$env_file" \
  COMPOSE_FILE="compose.yml:compose.smoke.yml" \
  COMPOSE_PROJECT_NAME="$project_name" \
  ./scripts/mlflow-auth-smoke.sh

record_scenario \
  'RBAC UUID' \
  'MLFLOW_SMOKE_ENV_FILE=<smoke.env dedicado> COMPOSE_FILE=compose.yml:compose.smoke.yml COMPOSE_PROJECT_NAME=<proyecto aislado> ./scripts/mlflow-rbac-smoke.sh' \
  'Las ediciones permitidas y denegadas entre grupos se validaron para experimentos y Registered Models con fixtures Group.id UUID.' \
  env MLFLOW_SMOKE_ENV_FILE="$env_file" \
  COMPOSE_FILE="compose.yml:compose.smoke.yml" \
  COMPOSE_PROJECT_NAME="$project_name" \
  ./scripts/mlflow-rbac-smoke.sh

host_port="$(compose port mlflow 5000)"
if status="$(curl -sS -o /dev/null -w '%{http_code}' "http://${host_port}/")"; then
  curl_exit_code=0
else
  curl_exit_code=$?
fi
printf '| %s | `%s` | %s | %s |\n' \
  'HTTP anónimo' \
  "curl -sS -o /dev/null -w '%{http_code}' <endpoint MLflow aislado>" \
  "$curl_exit_code" \
  "HTTP observado: $status; se exige 401." >>"$receipt_tmp"
if [ "$curl_exit_code" -ne 0 ]; then
  exit "$curl_exit_code"
fi
if [ "$status" != 401 ]; then
  printf '%s\n' "Expected anonymous MLflow request to return 401, got $status." >&2
  exit 1
fi

printf '%s\n' 'MLflow local smoke passed with disposable Compose resources.'
