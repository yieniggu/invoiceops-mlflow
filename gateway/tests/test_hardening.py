"""Regression tests for the verifier's exploits: cookie tossing, URI/param differentials, CSP."""

from __future__ import annotations

import socket

import httpx
import pytest
from conftest import GRP_A1, ORG_A, WS_A, WS_B, WS_G, FakeInvoiceOps, bearer

from invoiceops_mlflow_gateway.app import UI_CSP
from invoiceops_mlflow_gateway.policy import model_version_source_allowed

API = "/api/2.0/mlflow"


@pytest.fixture
def session_cookie(invoiceops: FakeInvoiceOps) -> str:
    cookie = "mls_hardening_" + "h" * 30
    invoiceops.add_session(cookie, [(ORG_A, None), (ORG_A, GRP_A1)])
    return cookie


def raw_get(base_url: str, target: str, headers: list[str]) -> bytes:
    host, port = base_url.removeprefix("http://").split(":")
    with socket.create_connection((host, int(port)), timeout=10) as sock:
        request = [f"GET {target} HTTP/1.1", f"Host: {host}", *headers, "Connection: close"]
        sock.sendall(("\r\n".join(request) + "\r\n\r\n").encode("latin-1"))
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    return data


# ---- 1. CSP on the UI shell ---------------------------------------------------------------


def test_ui_shell_has_strict_csp(gateway: str, tokens: dict[str, str]) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        page = c.get("/", headers={**bearer(tokens["a_write"]), "accept": "text/html"})
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    assert csp == UI_CSP
    for directive in ("script-src 'self'", "connect-src 'self'", "frame-ancestors 'self'"):
        assert directive in csp
    assert "unsafe-eval" not in csp and "script-src 'self' 'unsafe" not in csp
    assert page.headers["x-content-type-options"] == "nosniff"


def test_gateway_own_html_has_csp(gateway: str) -> None:
    failed = httpx.get(gateway + "/_gateway/callback")
    assert failed.status_code == 400
    assert failed.headers["content-security-policy"] == UI_CSP


def test_artifacts_keep_sandbox_csp(gateway: str, tokens: dict[str, str]) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        path = f"/api/2.0/mlflow-artifacts/artifacts/workspaces/{WS_A}/x.html"
        headers = {**bearer(tokens["a_write"]), "Content-Type": "text/html"}
        assert c.put(path, content=b"<script>1</script>", headers=headers).status_code == 200
        got = c.get(path, headers=bearer(tokens["a_write"]))
    assert got.headers["content-security-policy"] == "sandbox; default-src 'none'"


# ---- 2. duplicate session cookie ------------------------------------------------------------


def test_duplicate_session_cookie_is_unauthenticated(
    gateway: str, session_cookie: str, invoiceops: FakeInvoiceOps
) -> None:
    valid = f"__Host-mlflow_session={session_cookie}"
    url = f"{API}/experiments/search?max_results=10"
    with httpx.Client(base_url=gateway, timeout=30) as c:
        for cookie in (
            f"{valid}; __Host-mlflow_session=bogus",
            f"__Host-mlflow_session=bogus; {valid}",
            f"{valid}; __Host-mlflow_session={session_cookie}",
        ):
            assert c.get(url, headers={"Cookie": cookie}).status_code == 401, cookie
        ok = c.get(url, headers={"Cookie": f"{valid}; mlflow_ctx={WS_A}"})
        assert ok.status_code == 200


# ---- 3. model version source --------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "runs:/abc123/model",
        "runs:/abc123",
        "models:/my-model/1",
        "models:/m-0123456789abcdef",
        f"mlflow-artifacts:/workspaces/{WS_A}/1/r/artifacts/model",
    ],
)
def test_sources_accepted(source: str) -> None:
    assert model_version_source_allowed(source, WS_A)


@pytest.mark.parametrize(
    "source",
    [
        "runs://evil.host/x/y",
        "models://evil.host/x/1",
        "runs:///x",
        f"mlflow-artifacts://workspaces/{WS_A}/x",
        f"mlflow-artifacts://evil.host/workspaces/{WS_A}/x",
        f"mlflow-artifacts:///workspaces/{WS_A}/x",
        "mlflow-artifacts:/workspaces/org-other/x",
        f"mlflow-artifacts:/workspaces/{WS_A}/../{WS_B}/x",
        f"mlflow-artifacts:/workspaces/{WS_A}/x?/../../{WS_B}",
        f"mlflow-artifacts:/workspaces/{WS_A}/x#/../..",
        "runs:/x/%2e%2e/%2e%2e",
        "runs:/x/../y",
        "RUNS:/x/y",
        "runs:/x/y\n",
        "models:/x/1?../..",
        "s3://bucket/key",
    ],
)
def test_sources_rejected(source: str) -> None:
    assert not model_version_source_allowed(source, WS_A)


def test_netloc_source_refused_by_gateway(gateway: str, tokens: dict[str, str]) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        response = c.post(
            f"{API}/model-versions/create",
            json={"name": "m", "source": "runs://evil.host/x/y"},
            headers=bearer(tokens["a_write"]),
        )
    assert response.status_code == 403


# ---- 4. repeated query parameters -----------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        f"/api/2.0/mlflow-artifacts/artifacts?path=workspaces/{WS_B}&path=workspaces/{WS_A}",
        f"/api/2.0/mlflow-artifacts/artifacts?path=workspaces/{WS_A}&path=workspaces/{WS_B}",
        f"/api/2.0/mlflow-artifacts/artifacts?path=&path=workspaces/{WS_B}",
        "/get-artifact?run_id=x&path=ok&path=../../etc/passwd",
    ],
)
def test_repeated_path_param_rejected(gateway: str, tokens: dict[str, str], target: str) -> None:
    response = httpx.get(gateway + target, headers=bearer(tokens["a_write"]))
    assert response.status_code == 400


def test_repeated_context_param_rejected(gateway: str, session_cookie: str) -> None:
    response = httpx.get(
        f"{gateway}/?context={WS_G}&context={WS_A}",
        headers={"Cookie": f"__Host-mlflow_session={session_cookie}"},
    )
    assert response.status_code == 400


# ---- 5. non-latin-1 path with ?context= ------------------------------------------------------


@pytest.mark.parametrize("path", ["/%E2%82%AC", "/%ff", "/caf%C3%A9"])
def test_non_latin1_path_with_context_never_fails(
    gateway: str, session_cookie: str, path: str
) -> None:
    response = httpx.get(
        f"{gateway}{path}?context={WS_G}&q=%E2%82%AC",
        headers={
            "Cookie": f"__Host-mlflow_session={session_cookie}",
            "Sec-Fetch-Site": "same-origin",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.isascii() and location.startswith(path.split("%")[0] + "%")
    assert "context" not in location and "q=%E2%82%AC" in location


def test_raw_non_ascii_bytes_do_not_reset(gateway: str, session_cookie: str) -> None:
    data = raw_get(
        gateway,
        f"/€?context={WS_G}".encode().decode("latin-1"),
        [f"Cookie: __Host-mlflow_session={session_cookie}", "Sec-Fetch-Site: same-origin"],
    )
    assert data.startswith(b"HTTP/1.1 "), data[:80]
    assert not data.startswith(b"HTTP/1.1 5")


# ---- 6. context switch requires a same-site navigation ---------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"Sec-Fetch-Site": "cross-site"},
        {"Referer": "https://evil.example.test/"},
        {},
    ],
)
def test_cross_site_context_switch_is_ignored(
    gateway: str, session_cookie: str, extra: dict[str, str]
) -> None:
    response = httpx.get(
        f"{gateway}/?context={WS_G}",
        headers={"Cookie": f"__Host-mlflow_session={session_cookie}", **extra},
        follow_redirects=False,
    )
    assert "set-cookie" not in response.headers
    assert response.status_code != 303


@pytest.mark.parametrize(
    "extra",
    [
        {"Sec-Fetch-Site": "same-origin"},
        {"Sec-Fetch-Site": "same-site"},
        {"Sec-Fetch-Site": "none"},
        {"Referer": "https://mlflow.example.test/some/page"},
    ],
)
def test_same_site_context_switch_is_accepted(
    gateway: str, session_cookie: str, extra: dict[str, str]
) -> None:
    response = httpx.get(
        f"{gateway}/?context={WS_G}",
        headers={"Cookie": f"__Host-mlflow_session={session_cookie}", **extra},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert f"mlflow_ctx={WS_G}" in response.headers["set-cookie"]


def test_app_origin_referer_is_accepted(make_gateway: object, session_cookie: str) -> None:
    with make_gateway(GATEWAY_APP_ORIGIN="https://app.example.test") as url:  # type: ignore[operator]
        response = httpx.get(
            f"{url}/?context={WS_G}",
            headers={
                "Cookie": f"__Host-mlflow_session={session_cookie}",
                "Referer": "https://app.example.test/groups",
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
