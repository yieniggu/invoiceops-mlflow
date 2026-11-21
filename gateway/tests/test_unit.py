"""Policy, configuration and cache rules that do not need a running MLflow."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from conftest import ORG_A, SERVICE_SECRET, WS_A, make_settings

from invoiceops_mlflow_gateway.config import ConfigError, Settings
from invoiceops_mlflow_gateway.identity import Introspector, workspace_name
from invoiceops_mlflow_gateway.policy import (
    artifact_path_allowed,
    classify,
    is_safe_path,
    model_version_source_allowed,
)


@pytest.mark.parametrize(
    ("method", "path", "access"),
    [
        ("GET", "/api/2.0/mlflow/experiments/get", "read"),
        ("POST", "/ajax-api/2.0/mlflow/runs/search", "read"),
        ("POST", "/api/2.0/mlflow/runs/log-batch", "write"),
        ("PUT", "/api/2.0/mlflow-artifacts/artifacts/workspaces/x/a.txt", "write"),
        ("GET", "/api/2.0/mlflow-artifacts/artifacts/workspaces/x/a.txt", "read"),
        ("DELETE", "/api/2.0/mlflow/registered-models/delete", "write"),
        ("POST", "/api/3.0/mlflow/logged-models", "write"),
        ("GET", "/api/3.0/mlflow/logged-models/m-abc", "read"),
        ("GET", "/", "read"),
        ("POST", "/graphql", "read"),
    ],
)
def test_allowed_endpoints_classify_by_access(method: str, path: str, access: str) -> None:
    decision = classify(method, path)
    assert decision is not None and decision.access == access


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/2.0/mlflow/users/get"),
        ("POST", "/api/3.0/mlflow/workspaces"),
        ("GET", "/api/3.0/mlflow/gateway/secrets/list"),
        ("DELETE", "/api/2.0/mlflow/experiments/get"),
        ("PUT", "/api/2.0/mlflow-artifacts/artifacts"),
        ("POST", "/api/2.0/mlflow-artifacts/mpu/create/x"),
        ("GET", "/api/2.0/mlflow-artifacts/presigned/x"),
        ("GET", "/version"),
        ("TRACE", "/"),
    ],
)
def test_everything_else_is_denied(method: str, path: str) -> None:
    assert classify(method, path) is None


def test_path_safety() -> None:
    assert is_safe_path("/api/2.0/mlflow/runs/get")
    for bad in ["/a/../b", "/a//b", "/a/./b", "/a\\b", "/a\x00b", "api"]:
        assert not is_safe_path(bad)


def test_artifact_and_source_rules() -> None:
    assert artifact_path_allowed(f"workspaces/{WS_A}/1/r/artifacts/m.pkl", WS_A)
    assert not artifact_path_allowed("workspaces/org-other/1", WS_A)
    assert not artifact_path_allowed("1/run/artifacts", WS_A)
    assert not artifact_path_allowed(f"workspaces/{WS_A}/../x", WS_A)
    assert model_version_source_allowed("runs:/abc/model", WS_A)
    assert model_version_source_allowed(f"mlflow-artifacts:/workspaces/{WS_A}/1/r/artifacts", WS_A)
    assert not model_version_source_allowed("mlflow-artifacts:/workspaces/org-other/1", WS_A)
    assert not model_version_source_allowed("s3://bucket/key", WS_A)
    assert not model_version_source_allowed(None, WS_A)


def test_workspace_names_derive_only_from_uuids() -> None:
    assert workspace_name(ORG_A, None) == f"org-{ORG_A}"
    assert workspace_name(ORG_A, "bad id") is None
    assert workspace_name("../../default", None) is None
    assert len(f"org-{ORG_A}") <= 63


@pytest.mark.parametrize(
    "overrides",
    [
        {"GATEWAY_INTROSPECTION_SECRET": "short"},
        {"GATEWAY_INTROSPECTION_SECRET": "x" * 20 + " " + "x" * 20},
        {"GATEWAY_INTROSPECTION_SECRET": "x" * 513},
        {"GATEWAY_INTROSPECTION_SECRET": ""},
        {"GATEWAY_UPSTREAM_PASSWORD": "replace-with-generated-password-xxxxxxxxxxxx"},
        {"GATEWAY_SERVICE_READ_TOKEN": "tooshort"},
        {"GATEWAY_PUBLIC_ORIGIN": "https://mlflow.example.test/path"},
        {"GATEWAY_PUBLIC_ORIGIN": "mlflow.example.test"},
        {"GATEWAY_APP_ORIGIN": ""},
        {"GATEWAY_APP_ORIGIN": "app.example.test"},
        {"GATEWAY_APP_ORIGIN": "https://app.example.test/login"},
        {"GATEWAY_APP_ORIGIN": "https://app.example.test?x=1"},
        {"GATEWAY_APP_ORIGIN": "https://app.example.test#frag"},
        {"GATEWAY_APP_ORIGIN": "https://user:pw@app.example.test"},
        {"GATEWAY_PUBLIC_ORIGIN": "https://user@mlflow.example.test"},
        {"GATEWAY_PUBLIC_ORIGIN": "https://mlflow.example.test:notaport"},
        # Plain http needs the explicit development flag.
        {"GATEWAY_PUBLIC_ORIGIN": "http://localhost:8080", "GATEWAY_APP_ORIGIN": "http://a:3000"},
        {"GATEWAY_ALLOW_INSECURE_HTTP": "maybe"},
        # https gateway in front of a plain-http app is a downgrade.
        {"GATEWAY_APP_ORIGIN": "http://app.example.test"},
        {"GATEWAY_CACHE_ACTIVE_SECONDS": "31"},
        {"GATEWAY_CACHE_INACTIVE_SECONDS": "6"},
    ],
)
def test_invalid_configuration_refuses_to_start(overrides: dict[str, str]) -> None:
    with pytest.raises(ConfigError) as caught:
        make_settings("http://m:5000", "http://a:3000", **overrides)
    for value in overrides.values():
        assert not value or value not in str(caught.value) or len(value) < 8


def test_missing_settings_refuse_to_start() -> None:
    with pytest.raises(ConfigError):
        Settings.from_env({})


def test_cache_clamps_app_ttl_and_keys_by_hash() -> None:
    now = [0.0]
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.content)
        return httpx.Response(
            200,
            json={
                "active": True,
                "userId": "u",
                "organizationId": ORG_A,
                "groupId": None,
                "scopes": ["mlflow:read"],
                "cacheTtlSeconds": 3600,
            },
        )

    settings = make_settings("http://m:5000", "http://a:3000")
    assert SERVICE_SECRET
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    introspector = Introspector(settings, client, clock=lambda: now[0])

    async def run() -> None:
        assert await introspector.token("tok-1")
        await introspector.token("tok-1")
        assert len(calls) == 1
        now[0] = 29.9
        await introspector.token("tok-1")
        assert len(calls) == 1
        now[0] = 30.1  # never trusted for longer than 30 s, whatever the app answered
        await introspector.token("tok-1")
        assert len(calls) == 2
        assert all(b"tok-1" not in key.encode() for key in introspector._cache._items)  # noqa: SLF001

    asyncio.run(run())


def test_handoff_settings_are_derived_from_the_two_origins() -> None:
    secure = make_settings(
        "http://m:5000", "http://a:3000", GATEWAY_APP_ORIGIN="https://app.example.test/"
    )
    assert secure.app_origin == "https://app.example.test"
    assert secure.callback_url == "https://mlflow.example.test/_gateway/callback"
    assert secure.session_cookie_name == "__Host-mlflow_session"
    assert secure.state_cookie_name == "__Host-mlflow_oauth_state"
    local = make_settings(
        "http://m:5000",
        "http://a:3000",
        GATEWAY_PUBLIC_ORIGIN="http://localhost:8080",
        GATEWAY_APP_ORIGIN="http://localhost:3000",
        GATEWAY_ALLOW_INSECURE_HTTP="1",
    )
    # __Host- requires Secure, so plain http (local runs) uses unprefixed names.
    assert local.session_cookie_name == "mlflow_session"
    assert local.state_cookie_name == "mlflow_oauth_state"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://MLflow.Example.TEST", "https://mlflow.example.test"),
        ("HTTPS://mlflow.example.test:443/", "https://mlflow.example.test"),
        ("https://mlflow.example.test:8443", "https://mlflow.example.test:8443"),
        ("  https://mlflow.example.test/  ", "https://mlflow.example.test"),
    ],
)
def test_origins_are_normalized_like_the_app(raw: str, expected: str) -> None:
    settings = make_settings("http://m:5000", "http://a:3000", GATEWAY_PUBLIC_ORIGIN=raw)
    assert settings.public_origin == expected
    assert settings.callback_url == f"{expected}/_gateway/callback"


def test_http_origins_are_accepted_with_the_development_flag_and_normalized() -> None:
    settings = make_settings(
        "http://m:5000",
        "http://a:3000",
        GATEWAY_PUBLIC_ORIGIN="HTTP://LocalHost:80",
        GATEWAY_APP_ORIGIN="http://localhost:3000",
        GATEWAY_ALLOW_INSECURE_HTTP="1",
    )
    assert settings.public_origin == "http://localhost"
