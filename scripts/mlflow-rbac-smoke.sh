#!/bin/sh
set -eu

wait_for_mlflow() {
  attempt=1
  while [ "$attempt" -le 30 ]; do
    container_id="$(docker compose ps -q mlflow)"
    if [ -n "$container_id" ] && [ "$(docker inspect --format '{{.State.Health.Status}}' "$container_id")" = "healthy" ]; then
      return 0
    fi
    attempt=$((attempt + 1))
    sleep 2
  done

  printf '%s\n' 'MLflow did not become healthy.' >&2
  return 1
}

wait_for_mlflow

docker compose exec -T mlflow python - <<'PY'
import os
import secrets
from contextlib import contextmanager

import mlflow
from mlflow.exceptions import MlflowException, RestException
from mlflow.server import get_app_client
from mlflow.tracking import MlflowClient
from mlflow.utils.workspace_context import WorkspaceContext

TRACKING_URI = "http://127.0.0.1:5000"
PREFIX = "mlflow-rbac-smoke"
WORKSPACE_ONE = f"{PREFIX}-organization-one"
WORKSPACE_TWO = f"{PREFIX}-organization-two"
USERS = {
    "group_a": f"{PREFIX}-group-a",
    "group_b": f"{PREFIX}-group-b",
    "multi": f"{PREFIX}-multi",
    "manager": f"{PREFIX}-manager",
}
REGISTERED_MODELS = {
    "group_a": "group-mlflow-rbac-smoke-group-a-invoice-review",
    "group_b": "group-mlflow-rbac-smoke-group-b-invoice-review",
}
REGISTERED_MODEL_PREFIX = f"group-{PREFIX}"
ROLE_NAMES = (
    f"{PREFIX}-workspace-one-member",
    f"{PREFIX}-workspace-one-group-a",
    f"{PREFIX}-workspace-one-group-b",
    f"{PREFIX}-workspace-one-manager",
    f"{PREFIX}-workspace-two-member",
    f"{PREFIX}-workspace-two-group-c",
    f"{PREFIX}-manager-created",
)

os.environ["MLFLOW_TRACKING_URI"] = TRACKING_URI
os.environ["MLFLOW_TRACKING_USERNAME"] = os.environ["MLFLOW_AUTH_ADMIN_USERNAME"]
os.environ["MLFLOW_TRACKING_PASSWORD"] = os.environ["MLFLOW_AUTH_ADMIN_PASSWORD"]


@contextmanager
def identity(username, password, workspace):
    previous_username = os.environ.get("MLFLOW_TRACKING_USERNAME")
    previous_password = os.environ.get("MLFLOW_TRACKING_PASSWORD")
    os.environ["MLFLOW_TRACKING_USERNAME"] = username
    os.environ["MLFLOW_TRACKING_PASSWORD"] = password
    try:
        with WorkspaceContext(workspace):
            yield
    finally:
        if previous_username is None:
            os.environ.pop("MLFLOW_TRACKING_USERNAME", None)
        else:
            os.environ["MLFLOW_TRACKING_USERNAME"] = previous_username
        if previous_password is None:
            os.environ.pop("MLFLOW_TRACKING_PASSWORD", None)
        else:
            os.environ["MLFLOW_TRACKING_PASSWORD"] = previous_password


admin_auth = get_app_client("basic-auth", tracking_uri=TRACKING_URI)
passwords = {name: secrets.token_urlsafe(24) for name in USERS}
created_role_ids = []


def delete_previous_smoke_roles():
    for workspace in (WORKSPACE_ONE, WORKSPACE_TWO):
        for role in admin_auth.list_roles(workspace):
            if role.name in ROLE_NAMES:
                admin_auth.delete_role(role.id)


def delete_smoke_users():
    for username in USERS.values():
        try:
            admin_auth.delete_user(username)
        except RestException as error:
            if error.get_http_status_code() != 404:
                raise


def ensure_workspace(name):
    if name not in {workspace.name for workspace in mlflow.list_workspaces()}:
        mlflow.create_workspace(name=name, description="RBAC smoke workspace")


def ensure_experiment(workspace, name):
    with WorkspaceContext(workspace):
        client = MlflowClient()
        experiment = client.get_experiment_by_name(name)
        if experiment is None:
            experiment_id = client.create_experiment(name)
            experiment = client.get_experiment(experiment_id)
        return experiment


def delete_smoke_registered_models():
    with WorkspaceContext(WORKSPACE_ONE):
        client = MlflowClient()
        for name in REGISTERED_MODELS.values():
            if not name.startswith(REGISTERED_MODEL_PREFIX):
                raise SystemExit(f"Refusing to delete a model outside the smoke prefix: {name}")
            try:
                client.delete_registered_model(name)
            except RestException as error:
                if error.error_code != "RESOURCE_DOES_NOT_EXIST":
                    raise


def create_registered_model(workspace, name):
    with WorkspaceContext(workspace):
        return MlflowClient().create_registered_model(name)


def create_role(workspace, name, permissions, members):
    with WorkspaceContext(workspace):
        role = admin_auth.create_role(workspace=workspace, name=name)
        created_role_ids.append(role.id)
        for resource_type, resource_pattern, permission in permissions:
            admin_auth.add_role_permission(
                role_id=role.id,
                resource_type=resource_type,
                resource_pattern=resource_pattern,
                permission=permission,
            )
        for member in members:
            admin_auth.assign_role(username=member, role_id=role.id)
        return role


def set_tag_as(username, password, workspace, experiment_id, value):
    with identity(username, password, workspace):
        MlflowClient().set_experiment_tag(experiment_id, "smoke.rbac", value)


def expect_edit_denied(username, password, workspace, experiment_id):
    try:
        set_tag_as(username, password, workspace, experiment_id, "denied")
    except MlflowException as error:
        if "Permission denied" in str(error):
            return
        raise
    raise SystemExit("An unauthorized group edited another group's experiment.")


def update_registered_model_as(username, password, workspace, name, description):
    with identity(username, password, workspace):
        MlflowClient().update_registered_model(name, description)


def expect_registered_model_edit_denied(username, password, workspace, name):
    try:
        update_registered_model_as(username, password, workspace, name, "denied")
    except MlflowException as error:
        if "Permission denied" in str(error):
            return
        raise
    raise SystemExit("An unauthorized group edited another group's registered model.")


try:
    delete_previous_smoke_roles()
    delete_smoke_users()
    ensure_workspace(WORKSPACE_ONE)
    ensure_workspace(WORKSPACE_TWO)
    delete_smoke_registered_models()

    group_a = ensure_experiment(WORKSPACE_ONE, "group/group-a/invoice-risk")
    group_b = ensure_experiment(WORKSPACE_ONE, "group/group-b/invoice-risk")
    group_c = ensure_experiment(WORKSPACE_TWO, "group/group-c/invoice-risk")
    group_a_model = create_registered_model(WORKSPACE_ONE, REGISTERED_MODELS["group_a"])
    group_b_model = create_registered_model(WORKSPACE_ONE, REGISTERED_MODELS["group_b"])

    for name, username in USERS.items():
        admin_auth.create_user(username=username, password=passwords[name])

    create_role(
        WORKSPACE_ONE,
        f"{PREFIX}-workspace-one-member",
        (("workspace", "*", "USE"),),
        (USERS["group_a"], USERS["group_b"], USERS["multi"]),
    )
    create_role(
        WORKSPACE_ONE,
        f"{PREFIX}-workspace-one-group-a",
        (
            ("experiment", group_a.experiment_id, "EDIT"),
            ("registered_model", group_a_model.name, "EDIT"),
        ),
        (USERS["group_a"], USERS["multi"]),
    )
    create_role(
        WORKSPACE_ONE,
        f"{PREFIX}-workspace-one-group-b",
        (
            ("experiment", group_b.experiment_id, "EDIT"),
            ("registered_model", group_b_model.name, "EDIT"),
        ),
        (USERS["group_b"],),
    )
    create_role(
        WORKSPACE_ONE,
        f"{PREFIX}-workspace-one-manager",
        (("workspace", "*", "MANAGE"),),
        (USERS["manager"],),
    )
    create_role(
        WORKSPACE_TWO,
        f"{PREFIX}-workspace-two-member",
        (("workspace", "*", "USE"),),
        (USERS["multi"],),
    )
    create_role(
        WORKSPACE_TWO,
        f"{PREFIX}-workspace-two-group-c",
        (("experiment", group_c.experiment_id, "EDIT"),),
        (USERS["multi"],),
    )

    set_tag_as(
        USERS["group_a"], passwords["group_a"], WORKSPACE_ONE, group_a.experiment_id, "group-a"
    )
    set_tag_as(
        USERS["group_b"], passwords["group_b"], WORKSPACE_ONE, group_b.experiment_id, "group-b"
    )
    expect_edit_denied(
        USERS["group_b"], passwords["group_b"], WORKSPACE_ONE, group_a.experiment_id
    )
    update_registered_model_as(
        USERS["group_a"],
        passwords["group_a"],
        WORKSPACE_ONE,
        group_a_model.name,
        "group-a",
    )
    update_registered_model_as(
        USERS["group_b"],
        passwords["group_b"],
        WORKSPACE_ONE,
        group_b_model.name,
        "group-b",
    )
    expect_registered_model_edit_denied(
        USERS["group_b"], passwords["group_b"], WORKSPACE_ONE, group_a_model.name
    )
    set_tag_as(USERS["multi"], passwords["multi"], WORKSPACE_ONE, group_a.experiment_id, "multi-one")
    set_tag_as(USERS["multi"], passwords["multi"], WORKSPACE_TWO, group_c.experiment_id, "multi-two")

    with identity(USERS["manager"], passwords["manager"], WORKSPACE_ONE):
        manager_auth = get_app_client("basic-auth", tracking_uri=TRACKING_URI)
        manager_role = manager_auth.create_role(
            workspace=WORKSPACE_ONE, name=f"{PREFIX}-manager-created"
        )
        created_role_ids.append(manager_role.id)

    print("MLflow RBAC smoke passed.")
finally:
    for role_id in reversed(created_role_ids):
        try:
            admin_auth.delete_role(role_id)
        except RestException as error:
            if error.error_code != "RESOURCE_DOES_NOT_EXIST":
                raise
    delete_previous_smoke_roles()
    delete_smoke_users()
    delete_smoke_registered_models()
PY
