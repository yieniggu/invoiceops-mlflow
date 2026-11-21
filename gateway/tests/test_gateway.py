from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from conftest import (
    ORG_A,
    ORG_B,
    SERVICE_READ_TOKEN,
    WS_A,
    WS_B,
    FakeInvoiceOps,
    bearer,
)

API = "/api/2.0/mlflow"
ARTIFACTS = "/api/2.0/mlflow-artifacts/artifacts"
SECRET = b"TOP-SECRET-CONTENT"


def client(base: str) -> httpx.Client:
    return httpx.Client(base_url=base, timeout=30)


@pytest.fixture(scope="module")
def seeded(gateway: str, tokens: dict[str, str]) -> dict[str, Any]:
    """Resources owned by organization A, created through the gateway."""
    with client(gateway) as c:
        h = bearer(tokens["a_write"])
        exp = c.post(f"{API}/experiments/create", json={"name": "exp-a"}, headers=h)
        assert exp.status_code == 200, exp.text
        exp_id = exp.json()["experiment_id"]
        run = c.post(f"{API}/runs/create", json={"experiment_id": exp_id}, headers=h).json()["run"]
        run_id, art_uri = run["info"]["run_id"], run["info"]["artifact_uri"]
        assert art_uri.startswith(f"mlflow-artifacts:/workspaces/{WS_A}/")
        art_path = art_uri.removeprefix("mlflow-artifacts:/") + "/secret.txt"
        put = c.put(f"{ARTIFACTS}/{art_path}", content=SECRET, headers=h)
        assert put.status_code == 200, put.text
        assert (
            c.post(
                f"{API}/registered-models/create", json={"name": "model-a"}, headers=h
            ).status_code
            == 200
        )
        mv = c.post(
            f"{API}/model-versions/create",
            json={"name": "model-a", "source": art_uri, "run_id": run_id},
            headers=h,
        )
        assert mv.status_code == 200, mv.text
        lm = c.post(f"{API}/logged-models", json={"experiment_id": exp_id, "name": "lm"}, headers=h)
        assert lm.status_code == 200, lm.text
        return {
            "exp_id": exp_id,
            "run_id": run_id,
            "art_path": art_path,
            "model_id": lm.json()["model"]["info"]["model_id"],
        }


def test_anonymous_is_401_for_api_and_ui(gateway: str) -> None:
    with client(gateway) as c:
        api = c.get(f"{API}/experiments/search")
        assert api.status_code == 401
        assert api.headers["www-authenticate"].startswith("Bearer")
        # A browser navigation starts the sign-in handoff (see test_handoff.py).
        ui = c.get("/", headers={"accept": "text/html"})
        assert ui.status_code == 302
        assert c.get("/static-files/index.html").status_code == 401
        assert c.get("/graphql").status_code == 401
        assert c.get("/_gateway/healthz").status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": "Bearer iop_unknown_token_value"},
        {"Authorization": "Basic Z2F0ZXdheS1hZG1pbjpwYXNz"},
        {"Authorization": "Bearer"},
        {"Cookie": "__Host-mlflow_session=unknown"},
    ],
)
def test_unknown_credentials_are_401(gateway: str, headers: dict[str, str]) -> None:
    with client(gateway) as c:
        assert c.get(f"{API}/experiments/search", headers=headers).status_code == 401


def test_valid_token_reads_only_its_workspace(
    gateway: str, tokens: dict[str, str], seeded: dict[str, Any]
) -> None:
    with client(gateway) as c:
        mine = c.post(
            f"{API}/experiments/search",
            json={"max_results": 100},
            headers=bearer(tokens["a_write"]),
        )
        assert mine.status_code == 200
        mine_names = {e["name"] for e in mine.json()["experiments"]}
        assert "exp-a" in mine_names
        assert "exp-b" not in mine_names
        # A workspace exists once its context wrote something (reads never create it).
        c.post(
            f"{API}/experiments/create", json={"name": "exp-b"}, headers=bearer(tokens["b_write"])
        )
        other = c.post(
            f"{API}/experiments/search",
            json={"max_results": 100},
            headers=bearer(tokens["b_write"]),
        )
        assert other.status_code == 200
        assert "exp-a" not in other.text
        info = c.get("/api/3.0/mlflow/server-info", headers=bearer(tokens["a_read"]))
        assert info.status_code == 200


def test_read_token_cannot_write(
    gateway: str, tokens: dict[str, str], seeded: dict[str, Any]
) -> None:
    h = bearer(tokens["a_read"])
    with client(gateway) as c:
        assert (
            c.get(f"{API}/runs/get", params={"run_id": seeded["run_id"]}, headers=h).status_code
            == 200
        )
        for method, path, body in [
            ("POST", f"{API}/experiments/create", {"name": "nope"}),
            ("POST", f"{API}/runs/create", {"experiment_id": seeded["exp_id"]}),
            ("POST", f"{API}/registered-models/create", {"name": "nope"}),
            (
                "POST",
                f"{API}/runs/log-metric",
                {"run_id": seeded["run_id"], "key": "k", "value": 1, "timestamp": 1},
            ),
            ("PATCH", f"{API}/registered-models/update", {"name": "model-a", "description": "x"}),
            ("DELETE", f"{API}/registered-models/delete", {"name": "model-a"}),
            ("DELETE", f"{ARTIFACTS}/{seeded['art_path']}", None),
        ]:
            response = c.request(method, path, json=body, headers=h)
            assert response.status_code == 403, (method, path, response.text)
        put = c.put(f"{ARTIFACTS}/{seeded['art_path']}", content=b"x", headers=h)
        assert put.status_code == 403
        still = c.get(f"{ARTIFACTS}/{seeded['art_path']}", headers=h)
        assert still.content == SECRET


def test_cross_workspace_direct_ids_are_denied(
    gateway: str, tokens: dict[str, str], seeded: dict[str, Any]
) -> None:
    other_write = bearer(tokens["b_write"])
    other_group = bearer(tokens["g_write"])  # same organization, different (group) workspace
    run_id, exp_id = seeded["run_id"], seeded["exp_id"]
    gets = [
        (f"{API}/experiments/get", {"experiment_id": exp_id}),
        (f"{API}/experiments/get-by-name", {"experiment_name": "exp-a"}),
        (f"{API}/runs/get", {"run_id": run_id}),
        (f"{API}/metrics/get-history", {"run_id": run_id, "metric_key": "m"}),
        (f"{API}/artifacts/list", {"run_id": run_id}),
        ("/get-artifact", {"run_id": run_id, "path": "secret.txt"}),
        (f"{ARTIFACTS}/{seeded['art_path']}", {}),
        (ARTIFACTS, {"path": seeded["art_path"].rsplit("/", 1)[0]}),
        (ARTIFACTS, {"path": f"workspaces/{WS_A}"}),
        (f"{API}/registered-models/get", {"name": "model-a"}),
        (f"{API}/model-versions/get", {"name": "model-a", "version": "1"}),
        (f"{API}/model-versions/get-download-uri", {"name": "model-a", "version": "1"}),
        ("/model-versions/get-artifact", {"name": "model-a", "version": "1", "path": "secret.txt"}),
        (f"{API}/logged-models/{seeded['model_id']}", {}),
    ]
    with client(gateway) as c:
        for headers in (other_write, other_group):
            for path, params in gets:
                response = c.get(path, params=params, headers=headers)
                assert response.status_code in (403, 404), (
                    path,
                    response.status_code,
                    response.text,
                )
                assert SECRET not in response.content
            for path, body in [
                (f"{API}/runs/delete", {"run_id": run_id}),
                (
                    f"{API}/runs/log-metric",
                    {"run_id": run_id, "key": "x", "value": 1, "timestamp": 1},
                ),
                (f"{API}/runs/search", {"experiment_ids": [exp_id]}),
            ]:
                response = c.post(path, json=body, headers=headers)
                if path.endswith("search"):
                    assert response.json().get("runs", []) == []
                else:
                    assert response.status_code in (403, 404), (path, response.text)
            put = c.put(f"{ARTIFACTS}/{seeded['art_path']}", content=b"overwrite", headers=headers)
            assert put.status_code in (403, 404)
        # Owner still sees intact data.
        mine = c.get(f"{ARTIFACTS}/{seeded['art_path']}", headers=bearer(tokens["a_write"]))
        assert mine.content == SECRET


def test_client_supplied_workspace_selectors_are_ignored(
    gateway: str, tokens: dict[str, str], seeded: dict[str, Any]
) -> None:
    with client(gateway) as c:
        # B asks for A's workspace through every selector it can think of.
        h = {**bearer(tokens["b_write"]), "X-MLFLOW-WORKSPACE": WS_A, "x-mlflow-workspace": WS_A}
        for params in ({}, {"workspace": WS_A}, {"x-mlflow-workspace": WS_A}):
            response = c.get(
                f"{API}/experiments/get",
                params={"experiment_id": seeded["exp_id"], **params},
                headers=h,
            )
            assert response.status_code in (403, 404)
        listing = c.post(f"{API}/experiments/search", json={"max_results": 100}, headers=h)
        assert "exp-a" not in listing.text
        # A write with a spoofed header lands in B's workspace, never in A's.
        created = c.post(f"{API}/experiments/create", json={"name": "spoof-b"}, headers=h)
        assert created.status_code == 200
        names_a = c.post(
            f"{API}/experiments/search",
            json={"max_results": 100},
            headers=bearer(tokens["a_write"]),
        ).text
        assert "spoof-b" not in names_a
        names_b = c.post(
            f"{API}/experiments/search",
            json={"max_results": 100},
            headers=bearer(tokens["b_write"]),
        ).text
        assert "spoof-b" in names_b
        # The workspace listing only ever shows the caller's own workspace.
        ws = c.get("/api/3.0/mlflow/workspaces", headers=bearer(tokens["b_write"])).json()
        assert [w["name"] for w in ws["workspaces"]] == [WS_B]
        assert (
            c.get(
                f"/api/3.0/mlflow/workspaces/{WS_A}", headers=bearer(tokens["b_write"])
            ).status_code
            == 403
        )
        assert (
            c.get(
                f"/api/3.0/mlflow/workspaces/{WS_B}", headers=bearer(tokens["b_write"])
            ).status_code
            == 200
        )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/3.0/mlflow/workspaces"),
        ("DELETE", f"/api/3.0/mlflow/workspaces/{WS_B}"),
        ("PATCH", f"/api/3.0/mlflow/workspaces/{WS_B}"),
        ("POST", "/api/2.0/mlflow/users/create"),
        ("POST", "/api/3.0/mlflow/users/create"),
        ("PATCH", "/api/2.0/mlflow/users/update-password"),
        ("DELETE", "/api/2.0/mlflow/users/delete"),
        ("POST", "/api/2.0/mlflow/experiments/permissions/create"),
        ("POST", "/api/3.0/mlflow/roles/create"),
        ("GET", "/api/3.0/mlflow/roles/list"),
        ("POST", "/api/3.0/mlflow/gateway/secrets/create"),
        ("GET", "/api/3.0/mlflow/gateway/secrets/list"),
        ("POST", "/api/3.0/mlflow/webhooks"),
        ("POST", "/api/3.0/mlflow/scorers/register"),
        ("GET", "/api/3.0/mlflow/traces"),
        ("POST", "/ajax-api/2.0/mlflow/upload-artifact"),
        ("POST", f"{ARTIFACTS.replace('artifacts', 'mpu')}/create/x"),
        ("GET", "/api/2.0/mlflow-artifacts/presigned/x"),
        ("POST", "/ajax-api/2.0/mlflow/gateway-proxy"),
        ("GET", "/signup"),
        ("GET", "/metrics"),
        ("OPTIONS", f"{API}/experiments/search"),
    ],
)
def test_admin_and_unknown_endpoints_are_blocked(
    gateway: str, tokens: dict[str, str], method: str, path: str
) -> None:
    with client(gateway) as c:
        response = c.request(
            method, path, json={} if method != "GET" else None, headers=bearer(tokens["a_write"])
        )
        assert response.status_code == 403, (method, path, response.text)


def test_path_tricks_are_rejected(
    gateway: str, tokens: dict[str, str], seeded: dict[str, Any]
) -> None:
    h = bearer(tokens["b_write"])
    other_artifact = seeded["art_path"]
    with client(gateway) as c:
        for path in [
            f"{ARTIFACTS}/workspaces/{WS_B}/../../{other_artifact}",
            f"{ARTIFACTS}/workspaces/{WS_B}/%2e%2e/%2e%2e/{other_artifact}",
            f"{ARTIFACTS}//{other_artifact}",
            f"{ARTIFACTS}/workspaces/{WS_B}%2F..%2F..%2F{other_artifact}",
            f"{ARTIFACTS}/workspaces\\{WS_A}",
        ]:
            response = c.get(path, headers=h)
            assert response.status_code in (400, 403, 404), (path, response.status_code)
            assert SECRET not in response.content


def test_model_version_source_must_stay_in_workspace(
    gateway: str, tokens: dict[str, str], seeded: dict[str, Any]
) -> None:
    h = bearer(tokens["b_write"])
    with client(gateway) as c:
        assert (
            c.post(
                f"{API}/registered-models/create", json={"name": "model-b"}, headers=h
            ).status_code
            == 200
        )
        for source in [
            f"mlflow-artifacts:/workspaces/{WS_A}/1/x/artifacts",
            "s3://other-bucket/models",
            "https://evil.example/model",
            "file:///etc",
            "mlflow-artifacts:/1/x",
            f"mlflow-artifacts:/workspaces/{WS_B}/../{WS_A}/x",
            "",
        ]:
            response = c.post(
                f"{API}/model-versions/create",
                json={"name": "model-b", "source": source},
                headers=h,
            )
            assert response.status_code == 403, (source, response.text)
        ok = c.post(
            f"{API}/model-versions/create",
            json={"name": "model-b", "source": "runs:/abc/model"},
            headers=h,
        )
        assert ok.status_code == 200, ok.text
        own = c.post(
            f"{API}/model-versions/create",
            json={
                "name": "model-b",
                "source": f"mlflow-artifacts:/workspaces/{WS_B}/1/x/artifacts",
            },
            headers=h,
        )
        assert own.status_code == 200, own.text


def test_revoked_token_stops_working_after_cache_ttl(
    make_gateway: Any, invoiceops: FakeInvoiceOps
) -> None:
    """The cache TTL is driven by an injected clock: no sleeping, no wall-clock race."""
    token = "iop_revoke_me_" + "r" * 30
    invoiceops.add_token(token, ORG_B, ["mlflow:read"], ttl=2)
    now = [1000.0]
    with make_gateway(clock=lambda: now[0]) as base, client(base) as c:
        h = bearer(token)

        def lookups() -> int:
            return len([k for k, v in invoiceops.calls if v == token])

        assert c.get("/api/3.0/mlflow/server-info", headers=h).status_code == 200
        before = lookups()
        assert c.get("/api/3.0/mlflow/server-info", headers=h).status_code == 200
        assert lookups() == before  # served from cache
        invoiceops.tokens[token] = {"active": False, "cacheTtlSeconds": 5}  # revoked in InvoiceOps
        now[0] += 1.9  # still inside the 2 s TTL: the cached identity is served
        assert c.get("/api/3.0/mlflow/server-info", headers=h).status_code == 200
        assert lookups() == before
        now[0] += 0.2  # TTL elapsed: the next request asks InvoiceOps and is refused
        assert c.get("/api/3.0/mlflow/server-info", headers=h).status_code == 401
        assert lookups() == before + 1


def test_introspection_outage_fails_closed(make_gateway: Any, invoiceops: FakeInvoiceOps) -> None:
    token = "iop_outage_____" + "o" * 30
    invoiceops.add_token(token, ORG_B, ["mlflow:read"])
    with make_gateway() as base, client(base) as c:
        invoiceops.down = True
        try:
            assert c.get("/api/3.0/mlflow/server-info", headers=bearer(token)).status_code == 503
        finally:
            invoiceops.down = False
        assert c.get("/api/3.0/mlflow/server-info", headers=bearer(token)).status_code == 200


def test_service_read_identity(gateway: str, seeded: dict[str, Any]) -> None:
    with client(gateway) as c:
        h = bearer(SERVICE_READ_TOKEN)
        assert c.get(f"{API}/experiments/search", headers=h).status_code == 400  # needs a workspace
        scoped = {**h, "X-InvoiceOps-Workspace": WS_A}
        got = c.get(f"{API}/runs/get", params={"run_id": seeded["run_id"]}, headers=scoped)
        assert got.status_code == 200
        assert c.get(f"{ARTIFACTS}/{seeded['art_path']}", headers=scoped).content == SECRET
        assert (
            c.post(f"{API}/experiments/create", json={"name": "svc"}, headers=scoped).status_code
            == 403
        )
        assert (
            c.get(
                f"{API}/runs/get",
                params={"run_id": seeded["run_id"]},
                headers={**h, "X-InvoiceOps-Workspace": WS_B},
            ).status_code
            == 404
        )
        bad = {**h, "X-InvoiceOps-Workspace": "default"}
        assert c.get(f"{API}/experiments/search", headers=bad).status_code == 400


def test_artifact_upload_limit_and_streaming(make_gateway: Any, tokens: dict[str, str]) -> None:
    with make_gateway(GATEWAY_MAX_ARTIFACT_BYTES="1048576") as base, client(base) as c:
        h = bearer(tokens["b_write"])
        exp = c.post(f"{API}/experiments/create", json={"name": "big"}, headers=h).json()[
            "experiment_id"
        ]
        run = c.post(f"{API}/runs/create", json={"experiment_id": exp}, headers=h).json()["run"]
        path = run["info"]["artifact_uri"].removeprefix("mlflow-artifacts:/") + "/blob.bin"
        small = b"s" * 900_000
        assert c.put(f"{ARTIFACTS}/{path}", content=small, headers=h).status_code == 200
        assert c.get(f"{ARTIFACTS}/{path}", headers=h).content == small
        # Declared length over the limit is refused before any byte is read.
        assert c.put(f"{ARTIFACTS}/{path}", content=b"x" * 1_100_000, headers=h).status_code == 413

        def chunks() -> Any:  # chunked upload without Content-Length
            for _ in range(20):
                yield b"c" * 100_000

        over = c.put(f"{ARTIFACTS}/{path}2", content=chunks(), headers=h)
        assert over.status_code == 413
        assert over.json()["error_code"] == "PAYLOAD_TOO_LARGE"
        # Downloads carry the sandbox policy so user files never run as a page.
        got = c.get(f"{ARTIFACTS}/{path}", headers=h)
        assert "sandbox" in got.headers["content-security-policy"]


def test_tokens_and_cookies_are_never_logged(
    gateway: str, tokens: dict[str, str], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="mlflow_gateway")
    with client(gateway) as c:
        c.get(
            f"{API}/experiments/search",
            headers={"Authorization": "Bearer iop_log_probe_" + "q" * 30},
        )
        c.get(
            f"{API}/experiments/search",
            headers=bearer(tokens["a_write"]),
            params={"token": "iop_in_query"},
        )
    text = caplog.text
    assert "request_id" in text
    assert "iop_log_probe_" not in text
    assert tokens["a_write"] not in text
    assert "iop_in_query" not in text


def test_gateway_provisions_workspaces_idempotently(
    make_gateway: Any, tokens: dict[str, str], invoiceops: FakeInvoiceOps, mlflow_server: str
) -> None:
    from conftest import UPSTREAM_PASSWORD  # noqa: PLC0415

    def names() -> set[str]:
        listing = httpx.get(
            f"{mlflow_server}/api/3.0/mlflow/workspaces",
            auth=("gateway-admin", UPSTREAM_PASSWORD),
            timeout=30,
        ).json()
        return {w["name"] for w in listing["workspaces"]}

    assert {WS_A, WS_B} <= names()  # provisioned by the shared fixture, not by another test
    fresh_group = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    fresh_workspace = f"grp-{fresh_group}"
    fresh_token = "iop_provision_" + "p" * 30
    invoiceops.add_token(fresh_token, ORG_A, ["mlflow:read", "mlflow:write"], group=fresh_group)
    assert fresh_workspace not in names()
    # A fresh gateway process (empty provisioning cache) meets existing workspaces: 409 is fine.
    with make_gateway() as base, client(base) as c:
        assert (
            c.get("/api/3.0/mlflow/server-info", headers=bearer(tokens["a_write"])).status_code
            == 200
        )
        assert (
            c.post(
                f"{API}/experiments/search",
                json={"max_results": 100},
                headers=bearer(tokens["g_write"]),
            ).status_code
            == 200
        )
        # A read never provisions; the first authorized write does, once.
        read = c.post(
            f"{API}/experiments/search", json={"max_results": 5}, headers=bearer(fresh_token)
        )
        assert read.status_code == 404
        assert fresh_workspace not in names()
        created = c.post(
            f"{API}/experiments/create", json={"name": "provisioned"}, headers=bearer(fresh_token)
        )
        assert created.status_code == 200, created.text
        again = c.post(
            f"{API}/experiments/create", json={"name": "provisioned-2"}, headers=bearer(fresh_token)
        )
        assert again.status_code == 200, again.text
    assert fresh_workspace in names()


def test_mlflow_is_not_reachable_without_the_gateway_identity(mlflow_server: str) -> None:
    assert httpx.get(f"{mlflow_server}/api/2.0/mlflow/experiments/search").status_code == 401


def test_graphql_is_isolated_per_workspace(
    gateway: str, tokens: dict[str, str], seeded: dict[str, Any]
) -> None:
    query = {
        "query": (
            "query($id: String!) { mlflowGetRun(input: {runId: $id}) "
            "{ run { info { runId experimentId } } } }"
        ),
        "variables": {"id": seeded["run_id"]},
    }
    with client(gateway) as c:
        owner = c.post("/graphql", json=query, headers=bearer(tokens["a_write"]))
        assert owner.status_code == 200
        assert seeded["run_id"] in owner.text
        for other in ("b_write", "g_write"):  # another organization, another group context
            denied = c.post("/graphql", json=query, headers=bearer(tokens[other]))
            assert seeded["run_id"] not in denied.text, other
            payload = denied.json()
            assert not ((payload.get("data") or {}).get("mlflowGetRun") or {}).get("run"), other
        # The owner's run is untouched by the other contexts' queries.
        still = c.get(
            f"{API}/runs/get",
            params={"run_id": seeded["run_id"]},
            headers=bearer(tokens["a_write"]),
        )
        assert still.status_code == 200
