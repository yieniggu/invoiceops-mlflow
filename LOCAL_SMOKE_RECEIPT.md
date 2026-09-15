# Recibo de ejecución local de MLflow

- Inicio UTC: 2026-09-15T14:41:46Z
- Revisión Git de invoiceops-mlflow: b4341c56d0b3887f5b3320b6aec45d8d71d1d080
- Proyecto Compose aislado: invoiceops-mlflow-int02-smoke-fpqt5s
- Archivo de entorno: dedicado y no incluido en el recibo

| Escenario | Comando ejecutado | Código de salida | Aserción de éxito |
| --- | --- | ---: | --- |
| configuración de Compose | `docker compose --env-file <smoke.env dedicado> -p <proyecto aislado> -f compose.yml -f compose.smoke.yml config --quiet` | 0 | La configuración aislada se validó sin renderizarla en el recibo. |
| inicio aislado de Compose | `docker compose --env-file <smoke.env dedicado> -p <proyecto aislado> -f compose.yml -f compose.smoke.yml up --build -d` | 0 | El stack aislado se inició. |
| persistencia autenticada | `MLFLOW_SMOKE_ENV_FILE=<smoke.env dedicado> COMPOSE_FILE=compose.yml:compose.smoke.yml COMPOSE_PROJECT_NAME=<proyecto aislado> ./scripts/mlflow-auth-smoke.sh` | 0 | Un run y su artefacto autenticados se recuperaron antes y después de recrear sólo MLflow. |
| RBAC UUID | `MLFLOW_SMOKE_ENV_FILE=<smoke.env dedicado> COMPOSE_FILE=compose.yml:compose.smoke.yml COMPOSE_PROJECT_NAME=<proyecto aislado> ./scripts/mlflow-rbac-smoke.sh` | 0 | Las ediciones permitidas y denegadas entre grupos se validaron para experimentos y Registered Models con fixtures Group.id UUID. |
| HTTP anónimo | `curl -sS -o /dev/null -w '%{http_code}' <endpoint MLflow aislado>` | 0 | HTTP observado: 401; se exige 401. |

## Resultado final

- Estado: Finalizado con código de salida 0.
- Código de salida del arnés: 0
- Comando de limpieza: docker compose -p invoiceops-mlflow-int02-smoke-fpqt5s down --volumes --remove-orphans
- Código de salida de limpieza: 0
- Disposición: Se eliminaron los contenedores, red y volúmenes del proyecto Compose aislado.
