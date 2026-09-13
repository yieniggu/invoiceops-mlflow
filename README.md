# InvoiceOps MLflow

Stack local e independiente de MLflow para InvoiceOps. `MLFLOW-01` ejecuta
MLflow con PostgreSQL para metadata y MinIO como almacenamiento S3-compatible
para artefactos.

## Requisitos

- Docker Desktop con Docker Compose v2.
- Puerto local `5000` disponible.

PostgreSQL y MinIO no exponen puertos al equipo anfitrión. Sólo la interfaz y
endpoint de MLflow se publica en loopback.

## Inicio local

Desde este directorio, ejecuta:

```bash
docker compose up --build -d
```

La primera ejecución construye la imagen local de MLflow e instala sus
dependencias. Compose espera que PostgreSQL y MinIO estén saludables, crea el
bucket `mlflow-artifacts` de forma idempotente y después inicia MLflow.

Abre `http://127.0.0.1:5000` para usar la UI y configura clientes locales con:

```bash
export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
```

## Verificación

Comprueba que los cuatro servicios hayan terminado en el estado esperado:

```bash
docker compose ps
```

Se espera que `postgres` y `minio` estén `healthy`, `minio-init` haya finalizado
correctamente y `mlflow` esté ejecutándose. Los metadatos de experimentos y runs
se guardan en PostgreSQL; MLflow envía los artefactos al bucket
`s3://mlflow-artifacts` de MinIO mediante la red interna de Compose.

## Configuración y seguridad local

Las credenciales de PostgreSQL y MinIO incluidas en `compose.yml` son valores
públicos exclusivamente para este entorno local aislado. No son secretos ni son
aptos para entornos compartidos, remotos o de producción. No publiques puertos
de PostgreSQL o MinIO ni reutilices estas credenciales fuera de desarrollo.

Este batch no incluye autenticación, RBAC, HTTPS, proveedores cloud ni acceso
remoto. Esas capacidades corresponden a tickets posteriores.

`.env.example` conserva variables de referencia para futuros tickets; este
stack no necesita crear un archivo `.env`.

## Parada y limpieza

Para detener el stack sin eliminar sus datos persistentes:

```bash
docker compose down
```

El siguiente comando es destructivo: elimina los volúmenes de PostgreSQL y
MinIO, y por tanto todos los metadatos y artefactos locales.

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
