"""Browser path: InvoiceOps session cookie, context selection and CSRF."""

from __future__ import annotations

import httpx
import pytest
from conftest import GRP_A1, ORG_A, WS_A, WS_B, WS_G, FakeInvoiceOps, bearer

API = "/api/2.0/mlflow"
ORIGIN = "https://mlflow.example.test"


@pytest.fixture
def one_context(invoiceops: FakeInvoiceOps) -> str:
    invoiceops.add_session("mls_session_one_" + "1" * 30, [(ORG_A, None)])
    return "mls_session_one_" + "1" * 30


@pytest.fixture
def many_contexts(invoiceops: FakeInvoiceOps) -> str:
    cookie = "mls_session_many_" + "2" * 30
    invoiceops.add_session(cookie, [(ORG_A, None), (ORG_A, GRP_A1)])
    return cookie


def jar(cookie: str, **extra: str) -> dict[str, str]:
    value = "; ".join([f"__Host-mlflow_session={cookie}", *(f"{k}={v}" for k, v in extra.items())])
    return {"Cookie": value}


def nav(cookie: str) -> dict[str, str]:
    return {**jar(cookie), "Sec-Fetch-Site": "same-origin"}


def test_session_opens_ui_without_second_login(gateway: str, one_context: str) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        page = c.get("/", headers={**jar(one_context), "accept": "text/html"})
        assert page.status_code == 200
        assert "<html" in page.text.lower()
        assert "set-cookie" not in page.headers
        assert c.get("/static-files/index.html", headers=jar(one_context)).status_code in (200, 404)


def test_session_sees_only_its_scope(
    gateway: str, one_context: str, tokens: dict[str, str]
) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        created = c.post(
            f"{API}/experiments/create",
            json={"name": "from-ui"},
            headers={**jar(one_context), "Origin": ORIGIN},
        )
        assert created.status_code == 200, created.text
        # The same experiment is the one the org A token sees; org B does not.
        assert (
            "from-ui"
            in c.post(
                f"{API}/experiments/search",
                json={"max_results": 100},
                headers=bearer(tokens["a_write"]),
            ).text
        )
        assert (
            "from-ui"
            not in c.post(
                f"{API}/experiments/search",
                json={"max_results": 100},
                headers=bearer(tokens["b_write"]),
            ).text
        )
        listing = c.get("/ajax-api/3.0/mlflow/workspaces", headers=jar(one_context)).json()
        assert [w["name"] for w in listing["workspaces"]] == [WS_A]


def test_cookie_writes_require_same_origin(gateway: str, one_context: str) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        body = {"name": "csrf-probe"}
        for headers in (
            jar(one_context),
            {**jar(one_context), "Origin": "https://app.example.test"},
            {**jar(one_context), "Origin": "https://evil.example"},
            {**jar(one_context), "Origin": "null"},
        ):
            assert (
                c.post(f"{API}/experiments/create", json=body, headers=headers).status_code == 403
            )
        assert (
            "csrf-probe"
            not in c.get(
                f"{API}/experiments/search", params={"max_results": 100}, headers=jar(one_context)
            ).text
        )


def test_context_selection_is_validated_against_the_session(
    gateway: str, many_contexts: str
) -> None:
    with httpx.Client(base_url=gateway, follow_redirects=False, timeout=30) as c:
        ambiguous = c.get(
            f"{API}/experiments/search", params={"max_results": 100}, headers=jar(many_contexts)
        )
        assert ambiguous.status_code == 409
        page = c.get("/", headers={**jar(many_contexts), "accept": "text/html"})
        assert page.status_code == 200 and WS_G in page.text and WS_A in page.text
        # ?context= sets a host-only HttpOnly cookie and redirects without the parameter.
        chosen = c.get("/", params={"context": WS_G}, headers=nav(many_contexts))
        assert chosen.status_code == 303 and chosen.headers["location"] == "/"
        cookie = chosen.headers["set-cookie"].lower()
        assert f"mlflow_ctx={WS_G}" in cookie and "httponly" in cookie and "secure" in cookie
        assert "domain=" not in cookie
        ok = c.get(
            f"{API}/experiments/search",
            params={"max_results": 100},
            headers=jar(many_contexts, mlflow_ctx=WS_G),
        )
        assert ok.status_code == 200
        # A context the session does not own is refused, whether requested or forged in the cookie.
        assert c.get("/", params={"context": WS_B}, headers=nav(many_contexts)).status_code == 403
        forged = c.get(
            f"{API}/experiments/search",
            params={"max_results": 100},
            headers=jar(many_contexts, mlflow_ctx=WS_B),
        )
        assert forged.status_code == 409
        # The context parameter is honoured for sessions only, never for tokens.
        token_call = c.get("/", params={"context": WS_G}, headers={"Authorization": "Bearer nope"})
        assert token_call.status_code == 401


def test_contexts_are_isolated_from_each_other(gateway: str, many_contexts: str) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        org = jar(many_contexts, mlflow_ctx=WS_A)
        grp = jar(many_contexts, mlflow_ctx=WS_G)
        h = {"Origin": ORIGIN}
        made = c.post(f"{API}/experiments/create", json={"name": "org-only"}, headers={**org, **h})
        assert made.status_code == 200
        exp_id = made.json()["experiment_id"]
        assert (
            c.get(
                f"{API}/experiments/get", params={"experiment_id": exp_id}, headers=org
            ).status_code
            == 200
        )
        assert (
            c.get(
                f"{API}/experiments/get", params={"experiment_id": exp_id}, headers=grp
            ).status_code
            == 404
        )


def test_bearer_wins_and_cookie_never_reaches_mlflow(
    gateway: str, one_context: str, tokens: dict[str, str]
) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        response = c.get(
            f"{API}/experiments/search",
            params={"max_results": 100},
            headers={**jar(one_context), **bearer(tokens["b_write"])},
        )
        assert response.status_code == 200
        assert "from-ui" not in response.text  # org B's token decided the workspace
