# InvoiceOps MLflow

Stack local e independiente de MLflow para InvoiceOps. MLflow 3.16.0 ejecuta
Basic Auth con PostgreSQL para metadata y autorización, y MinIO como
almacenamiento S3-compatible para artefactos.

## Requisitos

- Docker Desktop con Docker Compose v2.
- Puerto local `5000` disponible.

PostgreSQL y MinIO no exponen puertos al equipo anfitrión. Sólo la interfaz y
endpoint de MLflow se publica en loopback.

## Configuración local segura

MLflow no arranca sin un `.env` local. La cuenta inicial se crea al habilitar
Basic Auth por primera vez; no existe un password por defecto en esta
configuración.

```bash
cp .env.example .env
chmod 600 .env
```

Genera valores distintos para `MLFLOW_AUTH_ADMIN_PASSWORD` y
`MLFLOW_FLASK_SERVER_SECRET_KEY`; por ejemplo, cada valor aleatorio puede
generarse localmente con:

```bash
openssl rand -base64 32
```

Elige también un nombre de usuario local no vacío. Sustituye los marcadores de
`.env` antes de continuar: el entrypoint rechaza explícitamente los valores de
ejemplo, incluidos el usuario, password y secreto CSRF. `.env` está ignorado
por Git: no lo incluyas en commits, salidas de `docker compose config`, logs,
tickets ni documentación. Las credenciales de cliente pueden exportarse desde
ese archivo en una shell local de confianza, sin imprimirlas:

```bash
set -a
. ./.env
set +a
```

## Inicio local

Desde este directorio, ejecuta:

```bash
docker compose up --build -d
```

La primera ejecución construye la imagen local de MLflow e instala sus
dependencias. Compose espera que PostgreSQL y MinIO estén saludables, crea el
bucket `mlflow-artifacts` de forma idempotente y después inicia MLflow. MLflow
escucha exclusivamente en `127.0.0.1:5000`; PostgreSQL y MinIO no publican
puertos al host.

Abre `http://127.0.0.1:5000` e inicia sesión con el usuario administrador local.
Los clientes autenticados usan:

```bash
export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
export MLFLOW_TRACKING_USERNAME="$MLFLOW_AUTH_ADMIN_USERNAME"
export MLFLOW_TRACKING_PASSWORD="$MLFLOW_AUTH_ADMIN_PASSWORD"
```

## Verificación y smoke

Comprueba que los cuatro servicios hayan terminado en el estado esperado:

```bash
docker compose ps
```

Se espera que `postgres`, `minio` y `mlflow` estén `healthy`, y que `minio-init` haya finalizado
correctamente. El healthcheck de MLflow comprueba que el endpoint protegido
responde el rechazo anónimo esperado (`401`) sin usar credenciales. Los metadatos de experimentos, runs
y autenticación se guardan en PostgreSQL; MLflow envía los artefactos al bucket
`s3://mlflow-artifacts` de MinIO mediante la red interna de Compose.

Una petición anónima debe ser rechazada con `401`:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5000/
```

El smoke versionado es opt-in: crea y recupera un run con artifact autenticado,
recrea solamente `mlflow` y vuelve a verificar la persistencia. No instala
paquetes ni imprime secretos. Carga `.env` en la shell local sin mostrarlo y
ejecútalo así:

```bash
set -a
. ./.env
set +a
./scripts/mlflow-auth-smoke.sh
```

El script no se ejecuta durante `docker compose up`; deja los datos de smoke en
los volúmenes persistentes para demostrar la recuperación post-recreate.

Si el servidor no inicia, revisa errores sin imprimir el entorno completo:

```bash
docker compose logs --tail=100 mlflow
docker compose ps
```

Verifica que `.env` siga ignorado con `git status --short`; nunca uses
`docker compose config` en una terminal o registro que vaya a compartirse,
porque resuelve variables de entorno.

## Configuración y seguridad local

Las credenciales nuevas de MLflow viven únicamente en `.env`. La imagen genera
el archivo de configuración de Basic Auth dentro del contenedor con permisos
restrictivos y no lo versiona. `MLFLOW_FLASK_SERVER_SECRET_KEY` protege CSRF y
nunca debe cambiarse mientras una instancia esté activa. Las credenciales
públicas locales preexistentes de PostgreSQL y MinIO continúan aisladas de la
red del host y no son aptas para ningún entorno compartido.

La autorización de una instalación nueva es fail-closed:
`grant_default_workspace_access=false`. Workspaces, RBAC, grupos, SSO, HTTPS,
acceso remoto y provisioning siguen fuera del alcance de este batch.

Este stack es sólo local. No reutilices este patrón ni sus secretos en entornos
compartidos, remotos o de producción.

## Respaldo y rollback

Antes de una actualización relevante, crea un backup lógico con PostgreSQL
activo. `backups/` está ignorado por Git:

```bash
mkdir -p backups
docker compose up -d postgres
docker compose exec -T postgres pg_dump --format=custom --no-owner --no-privileges \
  -U mlflow -d mlflow > backups/mlflow-before-upgrade.dump
docker compose stop postgres
```

Para detener el stack sin eliminar datos usa `docker compose down`. Para volver
a una revisión anterior, restaura sus archivos versionados, conserva el mismo
`.env` y ejecuta `docker compose up --build -d`. Si fuese necesario recuperar
el backup, hazlo sólo con PostgreSQL detenido y mediante un contenedor
temporal compatible; no uses `down --volumes`, reset de base de datos ni
borrado masivo de volúmenes.

## Parada y limpieza

Para detener el stack sin eliminar sus datos persistentes:

```bash
docker compose down
```

El siguiente comando es destructivo: elimina los volúmenes de PostgreSQL y
MinIO, y por tanto todos los metadatos, usuarios, configuración de auth y
artefactos locales. No lo ejecutes para mantenimiento ni rollback.

```bash
docker compose down --volumes
```

## Separación de responsabilidades

- `invoiceops-app` conserva la aplicación y la estructura académica fuente.
- `invoiceops-mlflow` contiene únicamente la infraestructura y documentación
  de MLflow.
- Los notebooks y el workspace de ML pertenecen a su repositorio independiente.

No se modifican contratos, datos ni configuración de `invoiceops-app` desde
este repositorio.
