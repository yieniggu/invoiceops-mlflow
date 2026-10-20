# InvoiceOps MLflow

Stack local e independiente de MLflow para InvoiceOps. MLflow 3.16.1 ejecuta
Basic Auth, Workspaces y RBAC con PostgreSQL para metadata y autorización, y MinIO
como almacenamiento S3-compatible para artefactos.

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

El pin `3.16.1` incorpora la corrección publicada que permite servir la UI a
usuarios no administradores autenticados con autorización fail-closed. Antes de
actualizar una instalación con datos, respalda la base PostgreSQL que contiene
metadata y autorización, verifica el archivo de respaldo y conserva la imagen
anterior para una reversión controlada; nunca elimines los volúmenes al recrear
únicamente MLflow. La autenticación anónima y las rutas desconocidas siguen
denegadas. Este parche no concede permisos nuevos sobre recursos ni sustituye
el aprovisionamiento manual de cuentas.

Abre `http://127.0.0.1:5000` e inicia sesión con el usuario administrador local.
Los clientes autenticados usan:

```bash
export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
export MLFLOW_TRACKING_USERNAME="$MLFLOW_AUTH_ADMIN_USERNAME"
export MLFLOW_TRACKING_PASSWORD="$MLFLOW_AUTH_ADMIN_PASSWORD"
```

## Workspaces por organización

Los Workspaces están habilitados en el servidor. Una organización de InvoiceOps
se representa manualmente con un Workspace que usa exactamente
`Organization.slug` como nombre. Antes de crearlo, el administrador debe
verificar que el slug satisface las reglas de nombres de Workspaces de la versión
de MLflow instalada. Un slug inválido no se aprovisiona: no existe normalización,
fallback ni mapeo alternativo hasta que se apruebe una política explícita.

La creación es una operación administrativa manual en este ticket. Con el stack
iniciado, un administrador puede crear un workspace desde el contenedor sin
exponer las credenciales al host:

```bash
docker compose exec -T mlflow sh -c '
  export MLFLOW_TRACKING_URI=http://127.0.0.1:5000
  export MLFLOW_TRACKING_USERNAME="$MLFLOW_AUTH_ADMIN_USERNAME"
  export MLFLOW_TRACKING_PASSWORD="$MLFLOW_AUTH_ADMIN_PASSWORD"
  exec python - "$@"
' sh <organization-slug> <<'PY'
import sys
import mlflow

workspace = mlflow.create_workspace(
    name=sys.argv[1],
    description="InvoiceOps organization workspace",
)
print(workspace.name)
PY
```

Para trabajar desde un notebook o cliente, selecciona explícitamente el
workspace activo antes de importar o usar MLflow:

```bash
export MLFLOW_WORKSPACE=<organization-slug>
```

Un usuario puede usar varios Workspaces seleccionando un valor distinto de
`MLFLOW_WORKSPACE` por shell o ejecución. Los recursos de MLflow quedan aislados
lógicamente por Workspace; `default` continúa disponible para recursos
preexistentes.

## RBAC y grupos

Con Basic Auth y `--enable-workspaces`, MLflow 3.16 habilita sus roles RBAC
por Workspace; no requiere otro flag de servidor. La aplicación académica sigue
siendo la fuente de verdad: este repositorio no lee ni sincroniza usuarios o
grupos de InvoiceOps. INT-02 establece `User.rut` normalizado como el nombre de
usuario de MLflow y `group-<Group.id>` como el rol de grupo, donde `Group.id` es
el UUID canónico de InvoiceOps. El aprovisionamiento idempotente corresponde a
`MLFLOW-07`.

El administrador de plataforma crea usuarios, roles y asignaciones con
`AuthServiceClient` y las APIs `create_role`, `add_role_permission` y
`assign_role`. Un rol puede asignarse a varios usuarios y un usuario puede tener
roles en varios grupos y Workspaces.

La política mínima es:

- El rol de miembro del Workspace tiene `(workspace, *, USE)`: permite entrar al
  Workspace y crear recursos, pero no edita recursos ajenos.
- Cada rol de grupo tiene `EDIT` únicamente sobre los IDs/nombres de recursos de
  ese grupo. MLflow 3.16 no interpreta prefijos como patrones de recursos: un
  patrón concreto es el ID/nombre exacto o `*`.
- El rol administrativo de la organización tiene `(workspace, *, MANAGE)` y
  puede administrar roles, permisos y asignaciones dentro de ese Workspace.

Los permisos efectivos se acumulan por máximo (`MANAGE > EDIT > USE > READ`).
No existe un deny explícito que pueda anular un grant mayor; por eso no se debe
otorgar `EDIT` con wildcard a un miembro que necesite aislamiento entre grupos.

### Cuenta técnica de lectura para InvoiceOps App

Asigna una identidad no administradora distinta a cada integración. En el
Workspace autorizado necesita `USE` sobre `(workspace, *)` y `READ` sólo sobre
los IDs de Experiments y nombres de Registered Models que consume. No asignes
`EDIT`, `MANAGE`, permisos sobre otro Workspace ni patrones `*` para modelos o
experimentos. Comprueba con recursos propios y ajenos **existentes**: lectura
propia permitida, edición de recurso existente y lectura directa de recursos
ajenos denegadas. Audita periódicamente las asignaciones y revoca el rol o la
cuenta cuando termine la integración; rota su credencial fuera del repositorio.

**Operación con MLflow 3.16.1 (sólo en el contenedor local):** inventaría primero
el usuario técnico, sus roles con `AuthServiceClient.list_user_roles(username)`
y, para cada rol, `list_role_permissions(role.id)`. Comprueba que
`get_user(username).is_admin` sea falso, que sólo haya roles en los Workspaces
previstos y que el conjunto de grants sea exactamente `(workspace, *, USE)` en
el Workspace previsto más `(experiment, <ID>, READ)` y
`(registered_model, <nombre>, READ)` para cada recurso autorizado. Revisa
asignaciones adicionales y permisos efectivos antes de aceptar el inventario:
los roles se acumulan y una asignación extra puede ampliar el acceso. No listes
ni alteres usuarios o grants ajenos como parte de una prueba de fixture.

Con una fixture temporal identificada por un prefijo propio, comprueba mediante
solicitudes autenticadas y `X-MLFLOW-WORKSPACE` explícito: `GET` de Experiment y
Registered Model propios existentes → `200`; edición de un Experiment existente
→ `403` sin cambio de tag; `GET` directo de Experiment y modelo conocidos y
existentes en otro Workspace propio de prueba → `403`. Un listado vacío no
demuestra la denegación de lectura. Para rotar la credencial de esa cuenta, usa
`AuthServiceClient.update_user_password(username, new_password)` como admin
local (la ruta self-service requiere `current_password`): verifica contraseña
anterior → `401` y nueva → `200` en la misma lectura propia. Tras actualizar al
consumidor, revoca la asignación exacta con `unassign_role(username, role_id)`,
verifica `list_user_roles(username) == []` y lectura del Workspace con la nueva
contraseña → `403`; no hay un deny que anule otros roles. Elimina la cuenta de
fixture con `delete_user(username)` y el rol propio con `delete_role(role_id)`;
confirma su ausencia. Para revocar una integración real, coordina primero su
ventana de servicio y audita todas las asignaciones antes de retirar la cuenta.
Genera las contraseñas en memoria privada; no las pongas en argumentos, logs,
comandos versionados ni salidas compartidas. Estas pruebas de fixture no
significan que se haya rotado una cuenta persistente de producción.

**Límite nativo:** `USE` permite también crear recursos nuevos en ese Workspace;
esta cuenta no es de solo lectura absoluta. `READ` exacto y las denegaciones de
edición y de otros Workspaces reducen el alcance, pero no eliminan esa capacidad.
Si se exige impedir toda creación, hace falta aprobar una frontera de autorización
distinta; no retires `USE` afirmando que la UI sigue funcionando ni desactives
`fail-closed`.

### Respaldo y restauración de metadata y autorización

Antes de un cambio de versión, guarda un archivo PostgreSQL en formato custom
que incluya **ambos** esquemas, de tracking y Basic Auth. Mantén el respaldo,
la configuración privada y la imagen anterior; no sobrescribas la base operada.
Verifica permisos `600`, hash SHA-256 y el inventario con `pg_restore --list`.
Para probar el rollback, restaura con `pg_restore --exit-on-error
--single-transaction` únicamente en una base **nueva y vacía**, del mismo major
de PostgreSQL, con su red y credenciales privadas propias. Comprueba conteos
de tablas y filas, claves foráneas validadas, tablas de autorización, y arranca
la versión de MLflow bajo prueba contra esa base aislada: un administrador
autenticado debe consultar metadata y el acceso anónimo debe devolver `401`.
No confundas una lectura del archivo ni un arranque de la versión nueva con
una prueba de arranque de la imagen anterior; no restaures nunca sobre la base
original sin un procedimiento y autorización independientes. Esta prueba de
metadata no demuestra recuperación de objetos de artifact almacenados aparte.

**Ensayo local CR-007 (2026-10-06):** además de restaurar el archivo en una
instancia nueva con MLflow 3.16.1, se restauró otra vez en PostgreSQL 16.9
aislado y se arrancó la imagen anterior **3.16.0** con un solo worker y ambas
URIs de base apuntando exclusivamente al clon. El servidor anterior leyó
metadata con autenticación (`200`) y rechazó acceso anónimo (`401`); permanecieron
71 tablas, 5 filas, 66 claves foráneas validadas y las revisiones de tracking
y autorización. La base restaurada tenía un administrador y **cero** roles o
asignaciones: se comprobaron los conteos y la ausencia de referencias huérfanas,
no permisos históricos de roles que no existían en ese respaldo. Se retiraron
contenedores, red, volumen anónimo y credenciales nuevos; el respaldo y los
servicios operados no se restauraron ni alteraron. Esta evidencia acredita
lectura de metadata/autorización con la imagen anterior, **no** descarga de
artifacts externos, recuperación completa de MinIO o rollback productivo.

### Inventario previo a limpiar fixtures

Antes de borrar metadata o hacer `CASCADE`, conserva en un manifiesto privado
`700/600` la identidad del Workspace, cada experimento, run, versión y **URI
completa** del artifact de la fixture. Elimina artifacts propios mediante el
proxy nativo usando sólo rutas exactas comprobadas y verifica su ausencia antes
de eliminar las referencias; luego inventaría y retira sólo la cuenta, roles y
Workspace propios. Si faltan URIs o IDs, detén el borrado físico: ni un prefijo
parecido ni una búsqueda general en el bucket prueban propiedad. Registra el
objeto como pendiente de procedencia, no como eliminado.

Para una fixture antigua sin metadata, recupera la ruta literal sólo de un recibo
propio o un log de subida/lectura del proxy y comprueba en el código de la versión
operada cómo se asigna al Workspace; conserva esa procedencia en el manifiesto
antes de actuar. Un `DELETE` aceptado o un directorio vacío no prueban por sí
solos la ausencia física: verifica además la ruta exacta y clasifica el error de
objeto faltante en la respuesta, sin divulgar trazas ni URLs firmadas. Si no hay
procedencia o la respuesta es ambigua, deja el objeto como pendiente; no repitas
el borrado a ciegas ni listes el bucket completo.

## Convenciones de ownership

Los recursos owner-scoped usan estas convenciones canónicas dentro del Workspace
de `Organization.slug`:

```text
individual experiment: student/<created_by_rut>/invoice-risk
individual registered model: student-<created_by_rut>-invoice-review
experiment: group/<Group.id>/invoice-risk
registered model: group-<Group.id>-invoice-review
```

Para trabajo individual, `created_by_rut` siempre identifica al owner del
recurso; no existe delegación. Los tags `organization_slug`, `owner_type`,
`owner_id` y `created_by_rut` permiten resolver el owner desde la UI y deben
aplicarse también al Registered Model owner-scoped. `owner_type` sigue limitado
a `user` o `group`.

`invoice-review-production` es el Registered Model compartido de producción. Es
una excepción explícita: no tiene owner académico, por lo que no recibe tags de
ownership y no se fuerza un `owner_type=shared` inválido. Su promoción y
automatización no se implementan aquí.

El administrador debe agregar `EDIT` al rol de grupo para el ID/nombre exacto de
cada recurso recién creado. `Group.name` no es una identidad ni un prefijo de
recursos porque es mutable y no único. El contrato completo está en
`../dev/tickets/INT-02_ownership_academico_mlflow.md`. MLFLOW-07 conserva el
aprovisionamiento y la reconciliación fuera de este alcance.

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

El smoke versionado de autenticación es opt-in: crea y recupera un run con
artifact autenticado, recrea solamente `mlflow` y vuelve a verificar la
persistencia. No instala paquetes ni imprime secretos. Carga `.env` en la shell
local sin mostrarlo y ejecútalo así:

```bash
set -a
. ./.env
set +a
./scripts/mlflow-auth-smoke.sh
```

El script no se ejecuta durante `docker compose up`; deja los datos de smoke en
los volúmenes persistentes para demostrar la recuperación post-recreate.

El smoke de Workspaces valida la capacidad administrativa completa: crea y lista
dos Workspaces, selecciona uno con `MLFLOW_WORKSPACE`, registra y recupera un
artefacto, recrea sólo `mlflow` y recupera el mismo artefacto. También deja sus
recursos de evidencia en PostgreSQL y MinIO:

```bash
./scripts/mlflow-workspaces-smoke.sh
```

El smoke RBAC es opt-in y usa cuentas no autorizadas reales para comprobar el
rechazo de edición entre grupos. Usa fixtures UUID `Group.id` deterministas y
sintácticamente válidos, y verifica el rol canónico `group-<Group.id>`, el
experimento `group/<Group.id>/invoice-risk` y el Registered Model
`group-<Group.id>-invoice-review` antes de probar la autorización. Concede `EDIT`
sólo al ID exacto del experimento y al nombre del Registered Model mediante
`resource_type="registered_model"`, y luego verifica una edición permitida sobre
el recurso propio y una denegada entre grupos para cada tipo de recurso. También
asigna un rol de grupo a varios usuarios, comprueba un usuario con roles en dos
Workspaces y verifica que un Workspace Manager pueda crear un rol. No imprime
contraseñas ni secretos:

```bash
./scripts/mlflow-rbac-smoke.sh
```

El smoke reutiliza únicamente los Workspaces y usuarios `mlflow-rbac-smoke-`, los
roles deterministas exactos `group-<Group.id>` y los Registered Models bajo el
prefijo UUID fijo del smoke. Elimina selectivamente esos usuarios, roles y modelos
antes y después de ejecutarse. Conserva los tres experimentos de fixture porque
MLflow sólo permite eliminar Workspaces vacíos y el borrado de experimentos es
lógico. No modifica recursos fuera de esos nombres controlados. La cobertura de
runtime valida el aislamiento de edición para experimentos y Registered Models;
las etiquetas, la ampliación de propiedad y el aprovisionamiento siguen fuera de
alcance para trabajo posterior. El aprovisionamiento y la reconciliación
corresponden a MLFLOW-07.

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

Las migraciones de tracking y autorización se ejecutan en ese orden, antes de
iniciar workers, mediante los callbacks de MLflow 3.16.1 dentro de procesos Python
que leen las URIs del entorno, sin incluirlas en argumentos de proceso. MinIO
importa las credenciales como JSON por stdin y conserva su configuración privada
en el filesystem del inicializador, sin nuevos volúmenes. El serializador exige
`printf` interno de la shell y se detiene si no está disponible; no instala herramientas.
Se suprimen ambas salidas de los clientes y sólo se publica un diagnóstico fijo
de la fase fallida. Esto reduce exposición en argv, no elimina secretos del
entorno, memoria o archivos privados ni protege frente a un administrador del host.

Los contratos offline usan dependencias simuladas: no acreditan disponibilidad
de la shell en la imagen fijada, migraciones reales ni compatibilidad S3 en runtime.
Antes de desplegar, respalda metadata y autorización y verifica por separado el
arranque y la inicialización idempotente con las imágenes reales. Las migraciones
pueden no ser transaccionales; una reversión del código anterior reintroduce la
exposición de credenciales en argv.

La autorización de una instalación nueva es fail-closed:
`grant_default_workspace_access=false`. Workspaces y RBAC están habilitados;
SSO, HTTPS, acceso remoto y provisioning siguen fuera del alcance de este batch.

Este stack es sólo local. No reutilices este patrón ni sus secretos en entornos
compartidos, remotos o de producción.

## Respaldo y rollback

Antes de habilitar Workspaces o realizar una actualización relevante, crea un
backup lógico con PostgreSQL activo. `backups/` está ignorado por Git:

```bash
mkdir -p backups
docker compose up -d postgres
docker compose exec -T postgres pg_dump --format=custom --no-owner --no-privileges \
  -U mlflow -d mlflow > backups/mlflow-before-upgrade.dump
docker compose stop postgres
```

Para detener el stack sin eliminar datos usa `docker compose down`. Para volver
a una revisión anterior antes de crear recursos no-default, restaura sus
archivos versionados, conserva el mismo `.env` y ejecuta `docker compose up
--build -d`. Después de crear recursos no-default, MLflow exige eliminar o
migrar esos recursos antes de deshabilitar Workspaces. Si fuese necesario
recuperar el backup, hazlo sólo con PostgreSQL detenido y mediante un contenedor
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
