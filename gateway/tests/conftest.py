"""Real MLflow 3.16.1 (sqlite + local artifacts, basic-auth, workspaces) behind the gateway.

Only the InvoiceOps introspection API is faked; everything else is the production stack
shape, so the workspace-isolation claims are tested against MLflow itself.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from invoiceops_mlflow_gateway.app import create_app
from invoiceops_mlflow_gateway.config import Settings

SERVICE_SECRET = "introspection-secret-" + "x" * 24
UPSTREAM_PASSWORD = "mlflow-admin-password-" + "y" * 20
SERVICE_READ_TOKEN = "service-read-token-" + "z" * 24
ORG_A = "11111111-1111-4111-8111-111111111111"
ORG_B = "22222222-2222-4222-8222-222222222222"
GRP_A1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WS_A = f"org-{ORG_A}"
WS_B = f"org-{ORG_B}"
WS_G = f"grp-{GRP_A1}"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class UvicornThread:
    def __init__(self, app: Any, port: int) -> None:
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", access_log=False
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.url = f"http://127.0.0.1:{port}"

    def __enter__(self) -> UvicornThread:
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started and time.time() < deadline:
            time.sleep(0.05)
        assert self.server.started, "server did not start"
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


class FakeInvoiceOps:
    """Stands in for /internal/api-tokens/introspect and /internal/sessions/introspect."""

    def __init__(self) -> None:
        self.tokens: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.codes: dict[str, str] = {}
        self.down = False

        async def guard(request: Request, kind: str) -> JSONResponse | dict[str, Any]:
            if self.down:
                return JSONResponse({"error": "down"}, status_code=500)
            if request.headers.get("authorization") != f"Bearer {SERVICE_SECRET}":
                return JSONResponse({"error": "unauthenticated"}, status_code=401)
            body = await request.json()
            self.calls.append(
                (kind, body.get("token") or body.get("sessionToken") or body.get("code"))
            )
            return body

        async def token(request: Request) -> JSONResponse:
            body = await guard(request, "token")
            if isinstance(body, JSONResponse):
                return body
            return JSONResponse(
                self.tokens.get(body["token"], {"active": False, "cacheTtlSeconds": 5})
            )

        async def session(request: Request) -> JSONResponse:
            body = await guard(request, "session")
            if isinstance(body, JSONResponse):
                return body
            return JSONResponse(
                self.sessions.get(body["sessionToken"], {"active": False, "cacheTtlSeconds": 5})
            )

        async def exchange(request: Request) -> JSONResponse:
            body = await guard(request, "exchange")
            if isinstance(body, JSONResponse):
                return body
            child = self.codes.pop(body["code"], None)  # single use
            if child is None:
                return JSONResponse({"active": False})
            return JSONResponse({"active": True, "sessionToken": child, "expiresInSeconds": 3600})

        async def revoke(request: Request) -> Response:
            body = await guard(request, "revoke")
            if isinstance(body, JSONResponse):
                return body
            self.sessions.pop(body["sessionToken"], None)
            return Response(status_code=204)

        self.app = Starlette(
            routes=[
                Route("/internal/sessions/exchange", exchange, methods=["POST"]),
                Route("/internal/sessions/revoke", revoke, methods=["POST"]),
                Route("/internal/api-tokens/introspect", token, methods=["POST"]),
                Route("/internal/sessions/introspect", session, methods=["POST"]),
            ]
        )

    def add_token(
        self, token: str, org: str, scopes: list[str], group: str | None = None, ttl: int = 30
    ) -> None:
        self.tokens[token] = {
            "active": True,
            "userId": "user-" + token[-4:],
            "organizationId": org,
            "groupId": group,
            "scopes": scopes,
            "cacheTtlSeconds": ttl,
        }

    def add_session(
        self, cookie: str, contexts: list[tuple[str, str | None]], ttl: int = 30
    ) -> None:
        self.sessions[cookie] = {
            "active": True,
            "userId": "user-" + cookie[-4:],
            "contexts": [
                {"organizationId": o, "groupId": g, "label": f"label {g or o}"} for o, g in contexts
            ],
            "cacheTtlSeconds": ttl,
        }


def stop_process_group(process: subprocess.Popen[Any], grace: float = 15) -> None:
    """Stop ``process`` AND every child it started: the whole session, not just the leader.

    ``mlflow server`` forks gunicorn/uvicorn workers; terminating only the leader leaves them
    running (and holding the port) after the test session.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=grace)
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)  # stragglers of the group, if any
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=grace)


@pytest.fixture(scope="session")
def mlflow_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    root = tmp_path_factory.mktemp("mlflow")
    port = free_port()
    auth_ini = root / "auth.ini"
    auth_ini.write_text(
        "[mlflow]\n"
        f"database_uri = sqlite:///{root}/auth.db\n"
        "admin_username = gateway-admin\n"
        f"admin_password = {UPSTREAM_PASSWORD}\n"
        "default_permission = READ\n"
        "grant_default_workspace_access = false\n"
    )
    env = {
        **os.environ,
        "MLFLOW_AUTH_CONFIG_PATH": str(auth_ini),
        "MLFLOW_FLASK_SERVER_SECRET_KEY": "k" * 48,
        "MLFLOW_DISABLE_AGENT_HINT": "1",
        "GIT_PYTHON_REFRESH": "quiet",
    }
    log = (root / "server.log").open("w")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "mlflow",
            "server",
            "--app-name=basic-auth",
            "--enable-workspaces",
            "--serve-artifacts",
            "--backend-store-uri",
            f"sqlite:///{root}/mlflow.db",
            "--artifacts-destination",
            str(root / "artifacts"),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,  # its own process group, so teardown can kill every child
    )
    url = f"http://127.0.0.1:{port}"
    deadline = time.time() + 120
    while time.time() < deadline:
        if process.poll() is not None:
            stop_process_group(process)
            pytest.fail("MLflow exited early:\n" + (root / "server.log").read_text()[-3000:])
        with contextlib.suppress(httpx.HTTPError):
            if httpx.get(url + "/health", timeout=2).status_code == 200:
                break
        time.sleep(0.5)
    else:
        stop_process_group(process)
        pytest.fail("MLflow did not start")
    try:
        yield url
    finally:
        stop_process_group(process)


@pytest.fixture(scope="session")
def invoiceops() -> Iterator[FakeInvoiceOps]:
    fake = FakeInvoiceOps()
    with UvicornThread(fake.app, free_port()) as server:
        fake.url = server.url  # type: ignore[attr-defined]
        yield fake


def make_settings(mlflow_url: str, invoiceops_url: str, **overrides: str) -> Settings:
    env = {
        "GATEWAY_UPSTREAM_URL": mlflow_url,
        "GATEWAY_UPSTREAM_USERNAME": "gateway-admin",
        "GATEWAY_UPSTREAM_PASSWORD": UPSTREAM_PASSWORD,
        "GATEWAY_INTROSPECTION_URL": invoiceops_url,
        "GATEWAY_INTROSPECTION_SECRET": SERVICE_SECRET,
        "GATEWAY_PUBLIC_ORIGIN": "https://mlflow.example.test",
        "GATEWAY_APP_ORIGIN": "https://app.example.test",
        "GATEWAY_SERVICE_READ_TOKEN": SERVICE_READ_TOKEN,
        **overrides,
    }
    return Settings.from_env(env)


def provision_workspace(mlflow_url: str, name: str) -> None:
    """Create a workspace straight in MLflow (what the gateway does on a first authorized write)."""
    response = httpx.post(
        f"{mlflow_url}/api/3.0/mlflow/workspaces",
        json={"name": name, "description": "test workspace"},
        auth=("gateway-admin", UPSTREAM_PASSWORD),
        timeout=30,
    )
    exists = response.status_code in (400, 409) and "RESOURCE_ALREADY_EXISTS" in response.text
    assert response.status_code in (200, 201) or exists, response.text


@pytest.fixture(scope="session")
def provisioned_workspaces(mlflow_server: str) -> tuple[str, ...]:
    """The workspaces the shared fixtures' contexts read from, whatever test runs first.

    Reads never create a workspace (they answer 404), so a test that only reads would otherwise
    depend on another test having written first.
    """
    names = (WS_A, WS_B, WS_G)
    for name in names:
        provision_workspace(mlflow_server, name)
    return names


@pytest.fixture(scope="session")
def gateway(
    mlflow_server: str, invoiceops: FakeInvoiceOps, provisioned_workspaces: tuple[str, ...]
) -> Iterator[str]:
    app = create_app(make_settings(mlflow_server, invoiceops.url))  # type: ignore[attr-defined]
    with UvicornThread(app, free_port()) as server:
        yield server.url


@pytest.fixture
def make_gateway(
    mlflow_server: str, invoiceops: FakeInvoiceOps, provisioned_workspaces: tuple[str, ...]
) -> Any:
    @contextlib.contextmanager
    def factory(clock: Any = time.monotonic, **overrides: str) -> Iterator[str]:
        settings = make_settings(mlflow_server, invoiceops.url, **overrides)  # type: ignore[attr-defined]
        app = create_app(settings, clock=clock)
        with UvicornThread(app, free_port()) as server:
            yield server.url

    return factory


@pytest.fixture(scope="session")
def tokens(invoiceops: FakeInvoiceOps) -> dict[str, str]:
    issued = {
        "a_write": "iop_a_write_" + "a" * 30,
        "a_read": "iop_a_read__" + "b" * 30,
        "b_write": "iop_b_write_" + "c" * 30,
        "g_write": "iop_g_write_" + "d" * 30,
    }
    invoiceops.add_token(issued["a_write"], ORG_A, ["mlflow:read", "mlflow:write"])
    invoiceops.add_token(issued["a_read"], ORG_A, ["mlflow:read"])
    invoiceops.add_token(issued["b_write"], ORG_B, ["mlflow:read", "mlflow:write"])
    invoiceops.add_token(issued["g_write"], ORG_A, ["mlflow:read", "mlflow:write"], group=GRP_A1)
    return issued


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
