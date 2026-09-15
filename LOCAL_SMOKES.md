# Arneses de smoke locales

Estos comandos validan los recorridos de PostgreSQL y MLflow dependientes de
Docker sin leer un `.env` existente, reutilizar recursos de Compose ni conservar
datos de prueba. Son exclusivamente locales y no aprovisionan usuarios, grupos,
Workspaces ni la reconciliación de MLFLOW-07 de InvoiceOps.

## Integración con PostgreSQL

Desde `../invoiceops-app`, ejecuta:

```bash
./scripts/postgres-integration-smoke.sh
```

El arnés inicia sólo `db-test` bajo un proyecto Compose único con el prefijo
`invoiceops-app-int02-smoke-`. Asigna PostgreSQL a un puerto efímero de loopback,
confirma que falle la protección ante `TEST_DATABASE_URL` ausente, aplica las
migraciones Prisma existentes y ejecuta `pnpm test:integration` contra
`invoiceops_test`.

Resultado esperado:

```text
PostgreSQL integration smoke passed with disposable Compose resources.
```

El trap de salida ejecuta `docker compose down --volumes --remove-orphans` sólo
para el proyecto de smoke generado. El contenedor, red y volumen de PostgreSQL
acotados a ese proyecto se eliminan tanto si el smoke aprueba como si falla, sin
afectar una ejecución superpuesta.

## Persistencia autenticada de MLflow

Desde este repositorio, crea un archivo de entorno local dedicado sin leer ni
copiar un `.env` existente:

```bash
cp smoke.env.example smoke.env
chmod 600 smoke.env
# Sustituye localmente cada marcador por valores únicos.
./scripts/run-local-smoke.sh smoke.env
```

`smoke.env` está ignorado. El arnés rechaza rutas `.env` y marcadores del ejemplo.
Usa un proyecto Compose único con el prefijo `invoiceops-mlflow-int02-smoke-`,
crea volúmenes nuevos de PostgreSQL y MinIO para ese proyecto y asigna MLflow a
un puerto efímero de loopback. Valida la configuración renderizada sin imprimirla,
recrear sólo MLflow, ejecuta el smoke RBAC alineado con UUID en ese mismo proyecto
aislado y luego comprueba que HTTP anónimo reciba `401`.

Resultado esperado:

```text
MLflow local smoke passed with disposable Compose resources.
```

El trap de salida elimina sólo los contenedores, red y volúmenes del proyecto de
smoke generado. No uses este arnés contra contextos Docker compartidos, remotos
ni de producción. No aprovisiona usuarios, grupos, Workspaces, etiquetas de
propiedad, membresías ni la reconciliación de MLFLOW-07 de InvoiceOps.

El runner actualiza `LOCAL_SMOKE_RECEIPT.md` en la raíz del repositorio al
finalizar, incluso si el smoke falla. El recibo no contiene el contenido de
`smoke.env`, credenciales, tokens, URLs con credenciales ni logs de contenedores.
Registra inicio UTC, revisión Git disponible, proyecto Compose aislado, escenarios
y comandos saneados, códigos de salida, aserciones explícitas y resultado de la
limpieza. Si recibe `HUP`, `INT` o `TERM`, termina con el código de señal
correspondiente y registra el estado `Interrumpido` antes de limpiar una sola vez.
Revísalo junto con el código de salida del runner.

## Fixture de nombres RBAC

El runner local invoca el smoke RBAC después de la persistencia autenticada
mientras el stack aislado sigue en ejecución. Sus fixtures de grupos deterministas
son UUID sintácticamente válidos y verifican los nombres de INT-02 antes de hacer
las comprobaciones de autorización:

```text
role: group-018f2d1c-6b9e-4d83-8c95-7e0d71d05a01
experiment: group/018f2d1c-6b9e-4d83-8c95-7e0d71d05a01/invoice-risk
registered model: group-018f2d1c-6b9e-4d83-8c95-7e0d71d05a01-invoice-review
```

Para un stack local ya iniciado, ejecútalo sólo después de cargar valores locales
ignorados y válidos en una shell de confianza:

```bash
./scripts/mlflow-rbac-smoke.sh
```

El smoke no aprovisiona ni reconcilia usuarios, grupos, Workspaces ni membresías
de InvoiceOps. Esas operaciones siguen en MLFLOW-07.

## Comprobaciones estáticas

```bash
docker compose --env-file /dev/null -p invoiceops-app-int02-smoke \
  -f ../invoiceops-app/docker-compose.yml \
  -f ../invoiceops-app/docker-compose.smoke.yml --profile test config --quiet

docker compose --env-file smoke.env.example -p invoiceops-mlflow-int02-smoke \
  -f compose.yml -f compose.smoke.yml config --quiet
```

La comprobación estática de MLflow acepta marcadores sólo para validar la sintaxis
de Compose. El arnés de runtime los rechaza antes de iniciar contenedores.
