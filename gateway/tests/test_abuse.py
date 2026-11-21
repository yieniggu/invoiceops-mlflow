"""Abuse bounds: fairness between principals, upload deadline, auth prefilter/throttle, logs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import socket
import threading
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from conftest import (
    ORG_A,
    ORG_B,
    SERVICE_SECRET,
    UPSTREAM_PASSWORD,
    FakeInvoiceOps,
    UvicornThread,
    bearer,
    free_port,
    make_settings,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from invoiceops_mlflow_gateway.app import Gateway, create_app
from invoiceops_mlflow_gateway.config import Settings
from invoiceops_mlflow_gateway.identity import Introspector, token_format_ok
from invoiceops_mlflow_gateway.main import QUIET_LOGGERS, configure_logging

API = "/api/2.0/mlflow"
ARTIFACTS = "/api/2.0/mlflow-artifacts/artifacts"
SERVER_INFO = "/api/3.0/mlflow/server-info"


@contextlib.contextmanager
def running(settings: Settings, **kwargs: Any) -> Iterator[tuple[str, Gateway]]:
    app = create_app(settings, **kwargs)
    with UvicornThread(app, free_port()) as server:
        yield server.url, app.state.gateway


def new_token(invoiceops: FakeInvoiceOps, label: str, org: str = ORG_A, scopes=None) -> str:
    token = "iop_" + "t" * 30 + f"_{label}"  # the fake derives the user id from the last 4 chars
    invoiceops.add_token(token, org, scopes or ["mlflow:read", "mlflow:write"])
    return token


class SlowUpload:
    """A client that announces a big body, sends a few bytes and then stalls."""

    def __init__(self, base_url: str, path: str, token: str) -> None:
        host, port = base_url.removeprefix("http://").split(":")
        self.sock = socket.create_connection((host, int(port)), timeout=15)
        head = (
            f"PUT {path} HTTP/1.1\r\nHost: {host}\r\nAuthorization: Bearer {token}\r\n"
            "Content-Type: application/octet-stream\r\nContent-Length: 1000000\r\n\r\n"
        )
        self.sock.sendall(head.encode() + b"x" * 10)

    def response_status(self, timeout: float) -> int:
        self.sock.settimeout(timeout)
        data = self.sock.recv(4096)
        return int(data.split(b" ", 2)[1])

    def close(self) -> None:
        self.sock.close()


def wait_for(predicate: Callable[[], bool], timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.05)


def upload_path(base: str, token: str, name: str) -> str:
    with httpx.Client(base_url=base, timeout=30) as c:
        h = bearer(token)
        exp = c.post(f"{API}/experiments/create", json={"name": name}, headers=h).json()
        run = c.post(
            f"{API}/runs/create", json={"experiment_id": exp["experiment_id"]}, headers=h
        ).json()["run"]
    return f"{ARTIFACTS}/" + run["info"]["artifact_uri"].removeprefix("mlflow-artifacts:/") + "/b"


# ---- fairness and deadline ------------------------------------------------------


def test_slow_uploads_of_one_principal_do_not_starve_another(
    mlflow_server: str, invoiceops: FakeInvoiceOps
) -> None:
    slow, other = new_token(invoiceops, "slow"), new_token(invoiceops, "other", ORG_B)
    settings = make_settings(
        mlflow_server,
        invoiceops.url,  # type: ignore[attr-defined]
        GATEWAY_MAX_CONCURRENT_PER_PRINCIPAL="1",
    )
    with running(settings) as (base, gateway):
        path = upload_path(base, slow, "fairness")
        stalled = SlowUpload(base, path, slow)
        try:
            wait_for(lambda: gateway.limiter.inflight == 1)
            with httpx.Client(base_url=base, timeout=10) as c:
                # The slow principal is capped at its own limit...
                again = c.get("/api/3.0/mlflow/server-info", headers=bearer(slow))
                assert again.status_code == 429
                assert again.headers["retry-after"]
                # ...while somebody else is served normally.
                served = c.get("/api/3.0/mlflow/server-info", headers=bearer(other))
                assert served.status_code == 200
        finally:
            stalled.close()
        wait_for(lambda: gateway.limiter.inflight == 0)  # the dropped upload frees its slot
        with httpx.Client(base_url=base, timeout=10) as c:
            assert c.get("/api/3.0/mlflow/server-info", headers=bearer(slow)).status_code == 200


def test_exhausted_upstream_pool_does_not_stall_authentication(
    mlflow_server: str, invoiceops: FakeInvoiceOps
) -> None:
    slow, victim = new_token(invoiceops, "pool_a"), new_token(invoiceops, "pool_b", ORG_B)
    settings = make_settings(mlflow_server, invoiceops.url)  # type: ignore[attr-defined]
    pool_timeout = 1.5
    tiny_pool = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=1), timeout=httpx.Timeout(10.0, pool=pool_timeout)
    )
    with running(settings, client=tiny_pool) as (base, gateway):
        path = upload_path(base, slow, "pool")
        stalled = SlowUpload(base, path, slow)
        try:
            wait_for(lambda: gateway.limiter.inflight == 1)
            started = time.monotonic()
            with httpx.Client(base_url=base, timeout=15) as c:
                response = c.get("/api/3.0/mlflow/server-info", headers=bearer(victim))
            # The data path is saturated (pool timeout: 504), but authentication was NOT queued
            # behind it: InvoiceOps was asked and answered at once, on its own client.
            assert response.status_code == 504
            assert any(v == victim for _, v in invoiceops.calls)
            assert time.monotonic() - started < 4 * pool_timeout  # relative to the configured wait
        finally:
            stalled.close()


def test_upload_has_an_absolute_deadline(mlflow_server: str, invoiceops: FakeInvoiceOps) -> None:
    token = new_token(invoiceops, "deadline")
    settings = make_settings(
        mlflow_server,
        invoiceops.url,  # type: ignore[attr-defined]
        GATEWAY_UPLOAD_DEADLINE_SECONDS="1",
    )
    with running(settings) as (base, gateway):
        path = upload_path(base, token, "deadline")
        stalled = SlowUpload(base, path, token)
        try:
            started = time.monotonic()
            assert stalled.response_status(timeout=10) == 408
            assert time.monotonic() - started < 8
        finally:
            stalled.close()
        wait_for(lambda: gateway.limiter.inflight == 0)


# ---- prefilter and failed-auth throttle ----------------------------------------


@pytest.mark.parametrize(
    "secret",
    ["", "x", "iop_", "iop_short", "bearer-token-without-prefix" + "a" * 30, "iop_" + "a b" * 20]
    + ["iop_" + "a" * 129, "IOP_" + "a" * 40],
)
def test_token_format_prefilter_rejects_junk(secret: str) -> None:
    assert not token_format_ok(secret)


def test_token_format_prefilter_accepts_real_tokens() -> None:
    assert token_format_ok("iop_" + "A" * 43)
    assert token_format_ok("iop_" + "a-_" * 14)


def test_junk_bearer_never_reaches_invoiceops(
    mlflow_server: str, invoiceops: FakeInvoiceOps
) -> None:
    settings = make_settings(mlflow_server, invoiceops.url)  # type: ignore[attr-defined]
    with running(settings) as (base, _):
        before = len(invoiceops.calls)
        with httpx.Client(base_url=base) as c:
            for junk in ("nope", "iop_short", "x" * 300, "iop_" + "$" * 40):
                assert c.get(f"{API}/experiments/search", headers=bearer(junk)).status_code == 401
        assert len(invoiceops.calls) == before


def unknown_token(label: str) -> str:
    return "iop_unknown_" + label.ljust(30, "u")  # well formed: it passes the prefilter


def test_failed_authentication_is_throttled_per_client_address(
    mlflow_server: str, invoiceops: FakeInvoiceOps
) -> None:
    good = new_token(invoiceops, "throttle_ok")
    settings = make_settings(
        mlflow_server,
        invoiceops.url,  # type: ignore[attr-defined]
        GATEWAY_AUTH_FAILURE_LIMIT="3",
        GATEWAY_CLIENT_IP_HEADER="X-Client-IP",
    )
    with running(settings) as (base, _), httpx.Client(base_url=base) as c:
        attacker = {"X-Client-IP": "203.0.113.7"}
        for attempt in range(3):
            response = c.get(
                f"{API}/experiments/search",
                headers={**attacker, **bearer(unknown_token(f"a{attempt}"))},
            )
            assert response.status_code == 401
        before = len(invoiceops.calls)
        blocked = c.get(f"{API}/experiments/search", headers={**attacker, **bearer(good)})
        assert blocked.status_code == 429
        assert int(blocked.headers["retry-after"]) > 0
        assert len(invoiceops.calls) == before  # the budget is spent: InvoiceOps is not asked
        # Another address is unaffected, and so are requests without credentials.
        neighbour = {"X-Client-IP": "198.51.100.9"}
        assert c.get(
            "/api/3.0/mlflow/server-info", headers={**neighbour, **bearer(good)}
        ).is_success
        assert c.get("/_gateway/healthz", headers=attacker).status_code == 200


# ---- only lookups that reach InvoiceOps count, per credential class -------------


def test_expired_cookies_never_block_a_valid_cached_token_from_the_same_address(
    mlflow_server: str, invoiceops: FakeInvoiceOps
) -> None:
    good = new_token(invoiceops, "cached_ok")
    settings = make_settings(
        mlflow_server,
        invoiceops.url,  # type: ignore[attr-defined]
        GATEWAY_AUTH_FAILURE_LIMIT="30",  # fewer than the 40 expired cookies below
        GATEWAY_CLIENT_IP_HEADER="X-Client-IP",
    )
    nat = {"X-Client-IP": "192.0.2.50"}  # one school NAT: many students share it
    with running(settings) as (base, _), httpx.Client(base_url=base, timeout=30) as c:
        assert c.get(SERVER_INFO, headers={**nat, **bearer(good)}).is_success
        statuses = []
        for number in range(40):
            expired = f"mls_expired_{number:03d}_" + "e" * 20  # well formed, unknown to InvoiceOps
            response = c.get(
                SERVER_INFO,
                headers={**nat, "Cookie": f"__Host-mlflow_session={expired}"},
            )
            statuses.append(response.status_code)
        assert statuses[:30] == [401] * 30
        assert set(statuses[30:]) == {429}  # the cookie class of this address is spent...
        again = c.get(SERVER_INFO, headers={**nat, **bearer(good)})
        assert again.is_success  # ...but the valid token (cached AND another class) passes


def test_a_valid_cached_credential_is_not_throttled_even_in_its_own_class(
    mlflow_server: str, invoiceops: FakeInvoiceOps
) -> None:
    good = new_token(invoiceops, "cached_cls")
    settings = make_settings(
        mlflow_server,
        invoiceops.url,  # type: ignore[attr-defined]
        GATEWAY_AUTH_FAILURE_LIMIT="2",
        GATEWAY_CLIENT_IP_HEADER="X-Client-IP",
    )
    nat = {"X-Client-IP": "192.0.2.51"}
    with running(settings) as (base, _), httpx.Client(base_url=base, timeout=30) as c:
        assert c.get(SERVER_INFO, headers={**nat, **bearer(good)}).is_success
        for number in range(2):
            c.get(SERVER_INFO, headers={**nat, **bearer(unknown_token(f"n{number}"))})
        blocked = c.get(SERVER_INFO, headers={**nat, **bearer(unknown_token("z"))})
        assert blocked.status_code == 429  # a miss from a spent address is refused
        assert c.get(SERVER_INFO, headers={**nat, **bearer(good)}).is_success


def test_the_default_failure_budget_tolerates_a_shared_address() -> None:
    from invoiceops_mlflow_gateway.config import Settings  # noqa: PLC0415

    settings = make_settings("http://m.invalid", "http://i.invalid")
    assert isinstance(settings, Settings)
    assert settings.auth_failure_limit >= 100  # many students behind one NAT
    assert settings.handoff_failure_limit < settings.auth_failure_limit


def test_requests_answered_from_the_cache_or_the_prefilter_never_count(
    mlflow_server: str, invoiceops: FakeInvoiceOps
) -> None:
    settings = make_settings(
        mlflow_server,
        invoiceops.url,  # type: ignore[attr-defined]
        GATEWAY_AUTH_FAILURE_LIMIT="2",
        GATEWAY_CLIENT_IP_HEADER="X-Client-IP",
    )
    nat = {"X-Client-IP": "192.0.2.52"}
    with running(settings) as (base, gateway), httpx.Client(base_url=base) as c:
        for junk in ("nope", "iop_short", "x" * 300) * 5:  # prefilter: no backend call
            assert c.get(SERVER_INFO, headers={**nat, **bearer(junk)}).status_code == 401
        same = bearer(unknown_token("same"))
        for _ in range(5):  # the negative answer is cached: one backend call, one failure
            assert c.get(SERVER_INFO, headers={**nat, **same}).status_code == 401
        assert not gateway.token_failures.blocked("192.0.2.52")


# ---- single-flight introspection survives a cancelled leader -------------------


def test_cancelled_leader_does_not_fail_the_followers(mlflow_server: str) -> None:
    settings = make_settings(mlflow_server, "http://introspection.invalid")
    release = asyncio.Event
    calls: list[int] = []

    async def scenario() -> list[Any]:
        gate = release()

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(len(calls) + 1)
            if len(calls) == 1:
                await gate.wait()  # the first (leader) call hangs until it is cancelled
            return httpx.Response(
                200,
                json={
                    "active": True,
                    "userId": "u-1",
                    "organizationId": ORG_A,
                    "groupId": None,
                    "scopes": ["mlflow:read"],
                    "cacheTtlSeconds": 30,
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        introspector = Introspector(settings, client)
        token = "iop_" + "z" * 40
        leader = asyncio.ensure_future(introspector.token(token))
        await asyncio.sleep(0.05)
        followers = [asyncio.ensure_future(introspector.token(token)) for _ in range(3)]
        await asyncio.sleep(0.05)
        leader.cancel()
        results = await asyncio.wait_for(asyncio.gather(*followers, return_exceptions=True), 5)
        await client.aclose()
        return results

    results = asyncio.run(scenario())
    assert all(getattr(r, "user_id", None) == "u-1" for r in results), results
    assert len(calls) == 2  # the cancelled leader, then ONE retry shared by the followers


def test_a_cancelled_follower_is_not_swallowed(mlflow_server: str) -> None:
    settings = make_settings(mlflow_server, "http://introspection.invalid")

    async def scenario() -> None:
        gate = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            await gate.wait()
            return httpx.Response(200, json={"active": False})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        introspector = Introspector(settings, client)
        token = "iop_" + "y" * 40
        leader = asyncio.ensure_future(introspector.token(token))
        await asyncio.sleep(0.05)
        follower = asyncio.ensure_future(introspector.token(token))
        await asyncio.sleep(0.05)
        follower.cancel()
        with pytest.raises(asyncio.CancelledError):
            await follower
        gate.set()
        assert await leader is None
        await client.aclose()

    asyncio.run(scenario())


# ---- reads never create a workspace --------------------------------------------


def test_reads_on_a_missing_workspace_404_without_creating_it(
    mlflow_server: str, invoiceops: FakeInvoiceOps
) -> None:
    org = "44444444-4444-4444-8444-444444444444"
    token = new_token(invoiceops, "fresh", org)
    workspace = f"org-{org}"
    settings = make_settings(mlflow_server, invoiceops.url)  # type: ignore[attr-defined]

    def workspaces() -> set[str]:
        listing = httpx.get(
            f"{mlflow_server}/api/3.0/mlflow/workspaces",
            auth=("gateway-admin", UPSTREAM_PASSWORD),
            timeout=30,
        ).json()
        return {w["name"] for w in listing["workspaces"]}

    assert workspace not in workspaces()
    with running(settings) as (base, _), httpx.Client(base_url=base, timeout=30) as c:
        read = c.post(f"{API}/experiments/search", json={"max_results": 5}, headers=bearer(token))
        assert read.status_code == 404
        assert workspace not in workspaces()  # the read did not provision anything
        write = c.post(f"{API}/experiments/create", json={"name": "first"}, headers=bearer(token))
        assert write.status_code == 200
        assert workspace in workspaces()
        again = c.post(f"{API}/experiments/search", json={"max_results": 5}, headers=bearer(token))
        assert again.status_code == 200


# ---- audit hash and counters ------------------------------------------------------


def test_mutations_log_a_keyed_hash_of_the_principal_never_the_subject(
    mlflow_server: str, invoiceops: FakeInvoiceOps, caplog: pytest.LogCaptureFixture
) -> None:
    token = "iop_audit_" + "u" * 30
    invoiceops.tokens[token] = {
        "active": True,
        "userId": "subject-7f3a9c2e-b1d4-4e08-9a55-0c6d2f4e8b13",
        "organizationId": ORG_B,
        "groupId": None,
        "scopes": ["mlflow:read", "mlflow:write"],
        "cacheTtlSeconds": 30,
    }
    settings = make_settings(mlflow_server, invoiceops.url)  # type: ignore[attr-defined]
    caplog.set_level(logging.INFO, logger="mlflow_gateway")
    with running(settings) as (base, gateway), httpx.Client(base_url=base, timeout=30) as c:
        c.post(f"{API}/experiments/create", json={"name": "audit-exp"}, headers=bearer(token))
        c.get("/api/3.0/mlflow/server-info", headers=bearer(token))
        snapshot = gateway.metrics_snapshot()
        expected = gateway.subject_hash("subject-7f3a9c2e-b1d4-4e08-9a55-0c6d2f4e8b13")
    records = [json.loads(r.message) for r in caplog.records if r.name == "mlflow_gateway"]
    requests = [r for r in records if r.get("event") == "request"]
    mutation = next(r for r in requests if r["method"] == "POST")
    read = next(r for r in requests if r["method"] == "GET")
    assert mutation["principal_hash"] == expected
    assert len(expected) == 16 and int(expected, 16) >= 0
    assert "principal_hash" not in read
    assert "subject" not in mutation
    text = caplog.text
    assert "subject-7f3a9c2e" not in text
    assert token not in text and SERVICE_SECRET not in text
    assert snapshot["requests"] == 2
    assert snapshot["by_outcome"]["ok"] == 2


def test_subject_hash_is_keyed_and_not_the_plain_digest(
    mlflow_server: str, invoiceops: FakeInvoiceOps
) -> None:
    import hashlib  # noqa: PLC0415

    gateway = Gateway(make_settings(mlflow_server, invoiceops.url))  # type: ignore[attr-defined]
    digest = gateway.subject_hash("user-1")
    assert digest != hashlib.sha256(b"user-1").hexdigest()[:16]
    other = make_settings(
        mlflow_server,
        invoiceops.url,  # type: ignore[attr-defined]
        GATEWAY_INTROSPECTION_SECRET="another-introspection-secret-" + "q" * 20,
    )
    assert Gateway(other).subject_hash("user-1") != digest


# ---- no logger leaks, including the HTTP client libraries ----------------------


@pytest.fixture
def clean_logging() -> Iterator[None]:
    yield
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)


def test_no_logger_at_info_leaks_secrets_or_urls(
    mlflow_server: str,
    invoiceops: FakeInvoiceOps,
    caplog: pytest.LogCaptureFixture,
    clean_logging: None,
) -> None:
    token = new_token(invoiceops, "logprobe", ORG_B)
    session = "mls_logprobe_" + "s" * 30
    invoiceops.add_session(session, [(ORG_B, None)])
    configure_logging()
    caplog.set_level(logging.INFO)  # root at INFO: every logger of every library is captured
    settings = make_settings(mlflow_server, invoiceops.url)  # type: ignore[attr-defined]
    with running(settings) as (base, _), httpx.Client(base_url=base, timeout=30) as c:
        c.post(f"{API}/experiments/create", json={"name": "log-probe"}, headers=bearer(token))
        c.get(f"{API}/experiments/search", headers={"Cookie": f"mlflow_session={session}"})
        c.get(f"{API}/experiments/search?probe=query-canary", headers=bearer(token))
        c.get("/_gateway/callback?state=x&code=code-canary-cccccccccccccccccccc")
    names = {r.name for r in caplog.records}
    assert "mlflow_gateway" in names
    assert not {n for n in names if n.split(".")[0] in QUIET_LOGGERS}, names
    for secret in (
        token,
        session,
        "code-canary-cccccccccccccccccccc",
        "query-canary",
        SERVICE_SECRET,
    ):
        assert secret not in caplog.text
    for name in QUIET_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING


# ---- a client that disconnects mid-stream closes the upstream -------------------


class StreamingUpstream:
    """Upstream stub: an endless body that records when its consumer goes away."""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.closed = threading.Event()

        async def endless(request: Request) -> StreamingResponse:
            async def body() -> Any:
                self.started.set()
                try:
                    while True:
                        yield b"z" * 16384
                        await asyncio.sleep(0.01)
                finally:
                    self.closed.set()

            return StreamingResponse(body(), media_type="application/octet-stream")

        async def other(request: Request) -> JSONResponse:
            return JSONResponse({})

        self.app = Starlette(
            routes=[
                Route("/get-artifact", endless, methods=["GET"]),
                Route("/{path:path}", other, methods=["GET", "POST"]),
            ]
        )


def test_client_disconnect_closes_the_upstream_and_frees_the_slot(
    invoiceops: FakeInvoiceOps,
) -> None:
    token = new_token(invoiceops, "disconnect")
    upstream = StreamingUpstream()
    with UvicornThread(upstream.app, free_port()) as stub:
        settings = make_settings(stub.url, invoiceops.url)  # type: ignore[attr-defined]
        with running(settings) as (base, gateway):
            with httpx.Client(base_url=base, timeout=15) as c:
                with c.stream(
                    "GET", "/get-artifact?run_id=r&path=p", headers=bearer(token)
                ) as response:
                    assert response.status_code == 200
                    assert next(response.iter_raw())  # the stream is flowing
                    assert gateway.limiter.inflight == 1
                # leaving the block closes the client connection while the body is unfinished
            assert upstream.closed.wait(10), "the upstream stream was never closed"
            wait_for(lambda: gateway.limiter.inflight == 0)


# ---- the audit digest has its own key --------------------------------------------


def test_audit_key_is_derived_not_the_introspection_secret(
    mlflow_server: str, invoiceops: FakeInvoiceOps
) -> None:
    import hashlib  # noqa: PLC0415
    import hmac  # noqa: PLC0415

    settings = make_settings(mlflow_server, invoiceops.url)  # type: ignore[attr-defined]
    assert settings.audit_key != SERVICE_SECRET.encode()
    expected = hmac.new(
        SERVICE_SECRET.encode(), b"invoiceops-mlflow-gateway/audit-key/v1", hashlib.sha256
    ).digest()
    assert settings.audit_key == expected  # the documented default derivation
    digest = Gateway(settings).subject_hash("user-1")
    assert digest != hmac.new(SERVICE_SECRET.encode(), b"user-1", "sha256").hexdigest()[:16]


def test_a_dedicated_audit_key_replaces_the_derivation(
    mlflow_server: str, invoiceops: FakeInvoiceOps
) -> None:
    import hmac  # noqa: PLC0415

    key = "dedicated-audit-hmac-key-" + "k" * 24
    base = make_settings(mlflow_server, invoiceops.url)  # type: ignore[attr-defined]
    dedicated = make_settings(
        mlflow_server,
        invoiceops.url,  # type: ignore[attr-defined]
        GATEWAY_AUDIT_HMAC_KEY=key,
    )
    assert dedicated.audit_key == key.encode()
    assert (
        Gateway(dedicated).subject_hash("user-1")
        == hmac.new(key.encode(), b"user-1", "sha256").hexdigest()[:16]
    )
    assert Gateway(dedicated).subject_hash("user-1") != Gateway(base).subject_hash("user-1")
    # Rotating the introspection secret does not change the audit digests when a key is set.
    rotated = make_settings(
        mlflow_server,
        invoiceops.url,  # type: ignore[attr-defined]
        GATEWAY_AUDIT_HMAC_KEY=key,
        GATEWAY_INTROSPECTION_SECRET="rotated-introspection-secret-" + "q" * 20,
    )
    assert Gateway(rotated).subject_hash("user-1") == Gateway(dedicated).subject_hash("user-1")


def test_an_invalid_audit_key_refuses_to_start(
    mlflow_server: str, invoiceops: FakeInvoiceOps
) -> None:
    from invoiceops_mlflow_gateway.config import ConfigError  # noqa: PLC0415

    with pytest.raises(ConfigError, match="GATEWAY_AUDIT_HMAC_KEY"):
        make_settings(
            mlflow_server,
            invoiceops.url,  # type: ignore[attr-defined]
            GATEWAY_AUDIT_HMAC_KEY="short",
        )


# ---- the container healthcheck is neither logged nor counted ---------------------


def test_healthz_is_not_logged_or_counted_but_other_requests_still_are(
    mlflow_server: str, invoiceops: FakeInvoiceOps, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="mlflow_gateway")
    settings = make_settings(mlflow_server, invoiceops.url)  # type: ignore[attr-defined]
    with running(settings) as (base, gateway), httpx.Client(base_url=base, timeout=30) as c:
        for _ in range(5):
            assert c.get("/_gateway/healthz").status_code == 200
        assert c.head("/_gateway/healthz").status_code == 200
        assert gateway.metrics_snapshot() == {"requests": 0, "by_outcome": {}}
        assert [r for r in caplog.records if "healthz" in r.getMessage()] == []
        assert c.get(f"{API}/experiments/search").status_code == 401  # a real request
        assert gateway.metrics_snapshot()["requests"] == 1
    assert len([r for r in caplog.records if r.name == "mlflow_gateway"]) == 1


def test_healthz_does_not_advance_the_periodic_metrics_log(
    mlflow_server: str, invoiceops: FakeInvoiceOps, caplog: pytest.LogCaptureFixture, monkeypatch
) -> None:
    import invoiceops_mlflow_gateway.app as app_module  # noqa: PLC0415

    monkeypatch.setattr(app_module, "METRICS_LOG_EVERY", 3)
    caplog.set_level(logging.INFO, logger="mlflow_gateway")
    settings = make_settings(mlflow_server, invoiceops.url)  # type: ignore[attr-defined]
    with running(settings) as (base, _), httpx.Client(base_url=base, timeout=30) as c:
        for _ in range(10):
            c.get("/_gateway/healthz")
    assert not [r for r in caplog.records if '"event": "metrics"' in r.getMessage()]
