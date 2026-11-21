"""Failures of MLflow itself surface as 502 BAD_GATEWAY, never a leaked upstream reply."""

from __future__ import annotations

import httpx
from conftest import (
    ORG_A,
    FakeInvoiceOps,
    bearer,
    free_port,
    make_settings,
)
from test_abuse import API, new_token, running


def test_an_upstream_that_is_down_is_502_bad_gateway(invoiceops: FakeInvoiceOps) -> None:
    token = new_token(invoiceops, "down", ORG_A)
    dead_port = free_port()
    dead = f"http://127.0.0.1:{dead_port}"  # nothing listens there
    settings = make_settings(dead, invoiceops.url)  # type: ignore[attr-defined]
    with running(settings) as (base, gateway), httpx.Client(base_url=base, timeout=30) as c:
        read = c.get(f"{API}/experiments/search", headers=bearer(token))
        write = c.post(f"{API}/experiments/create", json={"name": "x"}, headers=bearer(token))
        for response in (read, write):
            assert response.status_code == 502
            body = response.json()
            assert body["error_code"] == "BAD_GATEWAY"
            assert body["message"] == "MLflow is unavailable"
            assert response.headers["x-request-id"] == body["request_id"]
            # no upstream address in the answer or its headers: the port is the one actually dead
            assert str(dead_port) not in response.text + str(dict(response.headers))
            assert "127.0.0.1" not in response.text
        assert gateway.metrics_snapshot()["by_outcome"]["upstream_error:ConnectError"] >= 2
        # The gateway itself stays healthy and keeps answering locally.
        assert c.get("/_gateway/healthz").status_code == 200


def test_the_leak_assertion_would_catch_the_dead_port(invoiceops: FakeInvoiceOps) -> None:
    """Guard: the asserted port is the one the gateway really dials."""
    dead_port = free_port()
    settings = make_settings(f"http://127.0.0.1:{dead_port}", invoiceops.url)  # type: ignore[attr-defined]
    assert settings.upstream_url.endswith(f":{dead_port}")


def test_mlflow_rejecting_the_gateway_identity_is_502_not_a_401(
    mlflow_server: str, invoiceops: FakeInvoiceOps, provisioned_workspaces: tuple[str, ...]
) -> None:
    token = new_token(invoiceops, "badid", ORG_A)
    settings = make_settings(
        mlflow_server,
        invoiceops.url,  # type: ignore[attr-defined]
        GATEWAY_UPSTREAM_PASSWORD="a-password-mlflow-does-not-know-" + "p" * 10,
    )
    with running(settings) as (base, _), httpx.Client(base_url=base, timeout=30) as c:
        response = c.get(f"{API}/experiments/search", headers=bearer(token))
    assert response.status_code == 502
    body = response.json()
    assert body["error_code"] == "BAD_GATEWAY"
    assert body["message"] == "MLflow rejected the gateway identity"
    # The caller's own credential was fine: no WWW-Authenticate challenge may suggest otherwise.
    assert "www-authenticate" not in response.headers
