# Gateway de MLflow (MLOPS-02)

Servicio `gateway/` (Python, Starlette + httpx, streaming) delante de MLflow 3.16.1. Es lo único
publicado (a la red del reverse proxy, sin puerto en el host); PostgreSQL, MinIO y MLflow quedan
en la red privada del proyecto. En producción se une a las redes externas `aglai_edge` (conector del túnel
Cloudflare, que lo ve como `invoiceops-mlflow-gateway:8080`) e `invoiceops_api` (backend y Model API); la
secuencia completa (stack unificado de `invoiceops-stack`, que incluye `compose.yml` + `compose.gateway.yml`; `compose.networks.yml` solo se usa fuera del stack) está en «Runbook de despliegue» de `dev/tickets/MLOPS-07_plan.md`. Despliegue (agregue `gateway.env.example` a `.env` y reemplace cada placeholder: secretos hex, `openssl rand -hex 24`):

```sh
cat gateway.env.example >> .env
docker compose -f compose.yml -f compose.gateway.yml -f compose.networks.yml --env-file .env up -d --build --wait
```

## Mecanismo de workspace verificado en 3.16.1

- El workspace de una petición se elige **solo por la cabecera `X-MLFLOW-WORKSPACE`**
  (`mlflow/utils/workspace_utils.py:9`; leída en `server/workspace_helpers.py:134` para Flask y
  `server/fastapi_app.py:113-125` para FastAPI). No hay prefijo de ruta ni parámetro de query. Sin
  cabecera cae al workspace `default` (`workspace_helpers.py:39-45`), por eso el gateway **siempre**
  la sobrescribe.
- Las lecturas y escrituras por ID quedan acotadas al workspace activo en el store:
  experimentos `store/tracking/sqlalchemy_workspace_store.py:231-247`, runs `:366-384`
  (`_validate_run_accessible`), trazas/datasets `:205-230`, `:388+`; registro de modelos
  `store/model_registry/sqlalchemy_workspace_store.py`. Probado de forma empírica (los tests del
  gateway lo repiten): con el ID de otro workspace, experimento/run/métricas/lista de artefactos/
  `get-artifact`/modelo registrado/versión/`get-download-uri`/logged model responden **404**.
- Artefactos con `--serve-artifacts`: cada ruta se reescribe a `workspaces/<ws>/...` y una ruta que
  apunte a otro workspace se rechaza con **400** (`server/handlers.py:3636-3690`). Las
  ubicaciones de artefactos son del servidor (`artifact_location cannot be specified when
  workspaces are enabled`, `sqlalchemy_workspace_store.py:579`).
- UI: el JS guarda el workspace en `localStorage["mlflow.activeWorkspace"]` y lo envía en esa
  cabecera; lista workspaces con `GET /ajax-api/3.0/mlflow/workspaces`. Los workspaces se crean con
  `POST /api/3.0/mlflow/workspaces` (`handlers.py:1491`), que exige un usuario con permiso de
  administración cuando `basic-auth` está activo.
- **Brecha encontrada**: `model-versions/create` acepta cualquier `source` (`s3://otro-bucket`,
  `mlflow-artifacts:/workspaces/<otro>/...`). Leer a través de MLflow sigue aislado, pero un
  consumidor que resuelva el `source` de forma nativa (la Model API) sería un confused deputy. El
  gateway valida `source`: solo `runs:/`, `models:/` o el prefijo de artefactos de **su** workspace.

## Diseño

**La fuente de verdad del comportamiento del gateway es [`gateway/GATEWAY.md`](gateway/GATEWAY.md)**
(autenticación Bearer y sesión, handoff, mapeo de workspaces, allowlist, límites de subida y de
concurrencia, throttling de autenticación fallida, logs y variables de entorno). Este documento solo
conserva lo de operación: el mecanismo de workspace verificado en MLflow, el despliegue, el orden de
rollout, el respaldo y los riesgos residuales. No repita aquí reglas del gateway: enlácelas.

Decisiones que sí viven aquí porque son de despliegue:

- El gateway es un ASGI propio (no nginx `auth_request`) porque necesita caché por hash, inspección
  de cuerpo, auto-provisión de workspaces y selección de contexto.
- MLflow corre con `basic-auth` y el usuario admin de servicio (`MLFLOW_AUTH_ADMIN_*`): quien alcance
  MLflow sin el gateway recibe 401; el gateway impone el workspace y el RBAC de MLflow no se usa.
- El contenedor del gateway va endurecido en `compose.gateway.yml`: `cap_drop: [ALL]`,
  `no-new-privileges`, sistema de archivos de solo lectura (con `/tmp` en tmpfs), `pids_limit: 128` y
  `mem_limit: 256m`. Medición aproximada: ~28 MiB residentes tras 400 peticiones y un solo proceso; el
  peor caso (20 000 identidades en caché + 200 conexiones en streaming) queda muy por debajo del límite.
- Los workspaces se crean solo con una **escritura** autorizada; una lectura en un workspace
  inexistente responde 404 sin crearlo (una organización nueva ve 404 hasta su primera escritura).

Códigos: 401 sin credencial válida; 403 endpoint no permitido, scope insuficiente, ruta de artefacto
fuera del workspace o CSRF; 404 recurso de otro workspace por ID (MLflow); 409 contexto por elegir;
408 subida más lenta que su plazo absoluto; 413 cuerpo demasiado grande; 429 demasiadas peticiones
concurrentes de un mismo principal o demasiados fallos de autenticación desde una IP; 502/504 MLflow;
503 InvoiceOps no responde.

## Orden de despliegue

1. **`prisma migrate deploy`** en la app (migración `20261125000000_mlflow_sso_handoff`).
2. **Backend nuevo** con `MLFLOW_PUBLIC_URL` (origen del gateway, sin ruta).
3. **Gateway nuevo** con `GATEWAY_APP_ORIGIN` y `GATEWAY_PUBLIC_ORIGIN` (igual a `MLFLOW_PUBLIC_URL`), junto con el backend o justo después.

Con otro orden: sin la migración `authorize`/`exchange` responden 500; un gateway nuevo sin backend nuevo redirige a un `authorize` inexistente (404/503) y nadie abre la UI (los tokens Bearer siguen sirviendo); un gateway antiguo con el backend nuevo no encuentra la cookie compartida y pide iniciar sesión sin completarlo; un origen distinto en `MLFLOW_PUBLIC_URL` y `GATEWAY_PUBLIC_ORIGIN` da 400 en `authorize`. Rollback: primero la imagen anterior del gateway, luego la del backend; la migración es aditiva y no se revierte.

## Riesgos residuales

- La revocación se ve en ≤30 s en peticiones nuevas; una subida/descarga ya en curso no se corta (las subidas tienen un plazo absoluto `GATEWAY_UPLOAD_DEADLINE_SECONDS`).
- El aislamiento por ID depende de MLflow (probado en 3.16.1); la allowlist y el prefijo de ruta de
  artefactos del gateway son una segunda barrera, no una sustitución. Al subir de versión de MLflow
  hay que repetir `pytest` (usa un MLflow real) y revisar `get_endpoints()` contra `policy.py`.
- Cualquier miembro de un contexto con sesión puede borrar/modificar recursos de ese contexto
  (modelo de permisos de MLOPS-01: sin roles dentro del contexto).
- Las cookies del gateway son `__Host-` (host-only): un subdominio hermano no puede sobrescribirlas.
- Un solo proceso de gateway: la caché y el registro de workspaces provisionados son locales (con
  varias réplicas solo se pierde eficiencia, no corrección).
- Modelos servidos desde `runs:/`/`models:/` se resuelven en el workspace del consumidor; MLOPS-04
  debe resolverlos siempre con el workspace de la identidad de servicio.
- Las imágenes `minio/minio` y `quay.io/minio/*` ya no se pueden descargar. Los pines actuales
  (`compose.yml`, `compose.gateway.yml`) son el fork comunitario `pgsty/minio` y `pgsty/mc`, por tag **y**
  digest del índice multi-arch (amd64/arm64). Es un fork de terceros: revise el digest antes de actualizar
  y, si prefiere no depender de él, copie las imágenes a un registro propio con
  `docker buildx imagetools create --tag <registro>/minio:<tag> <origen>@<digest>` y defina
  `MINIO_IMAGE`/`MINIO_MC_IMAGE`. Los datos existentes en el volumen de MinIO son compatibles (formato en disco sin cambios).

## Backup y restauración

`scripts/backup.sh [destino]` (volcado `pg_dump -Fc` primero, luego `mc mirror` de artefactos, más
`MANIFEST.sha256`) y `scripts/restore.sh <backup> [--force]` (verifica checksums, detiene mlflow y
gateway y **aborta si no puede detenerlos**, toma un respaldo previo del estado actual en
`backups/pre-restore-<fecha>` (o `PRE_RESTORE_DIR`) cuando la base no está vacía y aborta si ese respaldo
falla, restaura PostgreSQL y espeja el bucket con `mc mirror --remove`: tras restaurar, el bucket
contiene exactamente lo del respaldo y los objetos posteriores quedan solo en el respaldo previo;
luego reinicia). `scripts/test-backup-restore.sh` los prueba de
extremo a extremo con volúmenes destruidos y compara filas y bytes de artefactos. Los respaldos
contienen datos de estudiantes: asegúrese de que tengan permisos 0700. Hoy los respaldos quedan en el mismo servidor (riesgo aceptado, ver MLOPS-07 §9); como recomendación opcional, copie periódicamente los respaldos a un lugar fuera del servidor, con el mismo cuidado de acceso.

## Volúmenes: renombre y migración

Las claves de volumen son `mlflow_postgres_data` y `mlflow_minio_data` (para convivir con el volumen
`postgres_data` de la app). Bajo el stack (`name: invoiceops`) resuelven a `invoiceops_mlflow_postgres_data` e
`invoiceops_mlflow_minio_data`, que es lo que espera `invoiceops-stack`. Un checkout **independiente** (proyecto
`invoiceops-mlflow`) que ya tenía datos en `invoiceops-mlflow_postgres_data` / `invoiceops-mlflow_minio_data`
los copia, con el stack detenido y sin borrar el origen, así:

```sh
scripts/migrate-standalone-volumes.sh
```

Para llevar esos datos al stack unificado: `scripts/migrate-standalone-volumes.sh invoiceops-mlflow invoiceops`.

## Credenciales locales

`compose.yml` ya no trae contraseñas fijas: exige `MLFLOW_DB_PASSWORD`, `MINIO_ROOT_USER` y
`MINIO_ROOT_PASSWORD`. Para un `docker compose up` local e independiente use los valores desechables de
`mlflow-local.example.env` (`--env-file mlflow-local.example.env --env-file .env`); el overlay de producción sigue
exigiendo sus propios valores generados. Los marcadores `replace-with-…` se rechazan al arrancar (entrypoint de
MLflow y `minio-init`).

Las variables de entorno del gateway (obligatorias y opcionales, con su valor por defecto) están en la sección «Configuration (environment)» de [`gateway/GATEWAY.md`](gateway/GATEWAY.md).

## Pruebas

`scripts/gateway-matrix.sh` comprueba el aislamiento A/B contra un despliegue (ver su cabecera).

```sh
cd gateway && pip install -e '.[test]' && ruff check . && ruff format --check . && pytest
TOKEN_A=... TOKEN_B=... GATEWAY_URL=... scripts/gateway-matrix.sh
shellcheck scripts/*.sh docker/*.sh && python3 -m unittest discover -s tests
```

`pytest` levanta un MLflow 3.16.1 real (sqlite, `basic-auth`, workspaces, `--serve-artifacts`),
una InvoiceOps falsa (solo la introspección) y el gateway, y usa además el cliente real de MLflow
con solo `MLFLOW_TRACKING_URI` y `MLFLOW_TRACKING_TOKEN`.
