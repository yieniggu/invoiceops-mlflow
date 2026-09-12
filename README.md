# InvoiceOps MLflow

Repositorio independiente para la infraestructura de MLflow de InvoiceOps.
Este scaffold establece sus límites operacionales sin implementar el stack.

## Estado

`MLFLOW-00` está completado. La implementación local del stack corresponde a
`MLFLOW-01` y no forma parte de este repositorio inicial.

## Configuración local

1. Copia `.env.example` a `.env` si un ticket posterior requiere variables de
   entorno locales.
2. Reemplaza los marcadores por valores propios de tu equipo.
3. Nunca publiques `.env` ni credenciales en Git.

Las variables incluidas son referencias para futuros tickets; este scaffold no
las consume ni inicia servicios.

| Variable | Propósito | Estado actual |
| --- | --- | --- |
| `MLFLOW_TRACKING_URI` | URI del servidor de tracking | Reservada para MLFLOW-01 y posteriores |
| `MLFLOW_TRACKING_USERNAME` | Usuario de acceso | Reservada para MLFLOW-02 y posteriores |
| `MLFLOW_TRACKING_PASSWORD` | Contraseña de acceso | Reservada para MLFLOW-02 y posteriores |
| `MLFLOW_WORKSPACE` | Workspace académico seleccionado | Reservada para MLFLOW-03 y posteriores |

## Límites local y remoto

### Local

El entorno local se implementará en `MLFLOW-01`. Ese ticket definirá y validará
el stack de MLflow, la persistencia de metadatos y el almacenamiento de
artefactos. Este repositorio no incluye Compose, imágenes, puertos, bases de
datos, object storage ni procesos en ejecución.

### Remoto

El despliegue remoto se implementará en `MLFLOW-06`, después de autenticación,
workspaces y RBAC. Hasta entonces no existe proveedor cloud, URL remota,
credencial ni configuración de despliegue en este repositorio.

## Separación de responsabilidades

- `invoiceops-app` conserva la aplicación y la estructura académica fuente.
- `invoiceops-mlflow` contendrá únicamente infraestructura y documentación de
  MLflow cuando los tickets posteriores lo habiliten.
- Los notebooks y el workspace de ML pertenecen a su repositorio independiente.

No se modifican contratos, datos ni configuración de `invoiceops-app` desde
este repositorio.

## Siguiente paso

Implementar `MLFLOW-01 — Stack MLflow local` con su Change Contract aprobado.
