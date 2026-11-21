"""Redirect-based sign-in handoff: host-only cookies on both hosts, no shared parent cookie."""

from __future__ import annotations

import base64
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from conftest import ORG_A, FakeInvoiceOps, bearer

API = "/api/2.0/mlflow"
ORIGIN = "https://mlflow.example.test"
APP = "https://app.example.test"
STATE_COOKIE = "__Host-mlflow_oauth_state"
SESSION_COOKIE = "__Host-mlflow_session"
ATTEMPTS_COOKIE = "__Host-mlflow_handoff_attempts"
HTML = {"accept": "text/html"}


def set_cookies(response: httpx.Response) -> dict[str, str]:
    return {h.split("=", 1)[0]: h for h in response.headers.get_list("set-cookie")}


def bound(path: str) -> str:
    return base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")


def begin(client: httpx.Client, path: str = "/") -> tuple[str, str]:
    """Run the first leg; returns (state, state cookie value)."""
    response = client.get(path, headers=HTML)
    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    cookie = set_cookies(response)[STATE_COOKIE].split(";")[0].split("=", 1)[1]
    return query["state"][0], cookie


def callback(
    client: httpx.Client, state: str | None, code: str | None, cookie: str | None
) -> httpx.Response:
    params = {k: v for k, v in (("state", state), ("code", code)) if v is not None}
    headers = {"Cookie": f"{STATE_COOKIE}={cookie}"} if cookie is not None else {}
    return client.get("/_gateway/callback", params=params, headers=headers)


@pytest.fixture
def child(invoiceops: FakeInvoiceOps) -> str:
    token = "mls_child_" + uuid.uuid4().hex
    invoiceops.add_session(token, [(ORG_A, None)], ttl=0)
    return token


def test_navigation_without_session_redirects_to_the_app_with_state(gateway: str) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        first = c.get("/experiments/1?view=table", headers=HTML)
        second = c.get("/", headers=HTML)

    assert first.status_code == 302
    target = urlsplit(first.headers["location"])
    assert f"{target.scheme}://{target.netloc}{target.path}" == f"{APP}/api/v1/mlflow/authorize"
    query = parse_qs(target.query)
    assert query["redirect_uri"] == [f"{ORIGIN}/_gateway/callback"]
    state = query["state"][0]
    assert len(base64.urlsafe_b64decode(state + "=" * (-len(state) % 4))) >= 16  # >= 128 bits
    assert state != parse_qs(urlsplit(second.headers["location"]).query)["state"][0]
    raw = set_cookies(first)[STATE_COOKIE]
    assert raw.startswith(f"{STATE_COOKIE}={state}.")
    cookie = raw.lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert "path=/" in cookie and "domain=" not in cookie
    max_age = int(cookie.split("max-age=")[1].split(";")[0])
    assert 0 < max_age <= 300
    assert first.headers["cache-control"] == "no-store"


def test_api_and_xhr_requests_without_session_stay_401(gateway: str) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        responses = [
            c.get(f"{API}/experiments/search"),
            c.get("/", headers={"accept": "application/json"}),
            c.get("/static-files/index.html"),
            c.post(f"{API}/experiments/search", json={}, headers=HTML),
            c.get("/", headers={"authorization": "Bearer iop_unknown_token_value", **HTML}),
        ]
    for response in responses:
        assert response.status_code == 401
        assert "set-cookie" not in response.headers
        assert "location" not in response.headers


def test_success_sets_a_host_only_cookie_and_returns_to_the_bound_path(
    gateway: str, invoiceops: FakeInvoiceOps, child: str
) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        state, cookie = begin(c, "/experiments/1?view=table")
        invoiceops.codes["code-ok-cccccccccccccccccccc"] = child

        done = callback(c, state, "code-ok-cccccccccccccccccccc", cookie)

        assert done.status_code == 302
        assert done.headers["location"] == "/experiments/1?view=table"
        cookies = set_cookies(done)
        session = cookies[SESSION_COOKIE].lower()
        assert session.startswith(f"{SESSION_COOKIE.lower()}={child}")
        assert "secure" in session and "httponly" in session and "samesite=lax" in session
        assert "path=/" in session and "domain=" not in session
        assert "max-age=0" in cookies[STATE_COOKIE].lower()  # state is single use
        assert ("exchange", "code-ok-cccccccccccccccccccc") in invoiceops.calls
        # The child session now authenticates the browser (and nothing else was needed).
        ok = c.get(
            f"{API}/experiments/search",
            params={"max_results": 1},
            headers={"Cookie": f"{SESSION_COOKIE}={child}"},
        )
        assert ok.status_code == 200
        # The app's own cookie name is no longer read by the gateway.
        legacy = c.get(
            f"{API}/experiments/search", headers={"Cookie": f"invoiceops_session={child}"}
        )
        assert legacy.status_code == 401


def test_a_code_works_once(gateway: str, invoiceops: FakeInvoiceOps, child: str) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        state, cookie = begin(c)
        invoiceops.codes["code-once-cccccccccccccccccccc"] = child
        assert callback(c, state, "code-once-cccccccccccccccccccc", cookie).status_code == 302
        replay = callback(c, state, "code-once-cccccccccccccccccccc", cookie)
    assert replay.status_code == 400
    assert SESSION_COOKIE not in set_cookies(replay)


@pytest.mark.parametrize("scenario", ["no_cookie", "wrong_state", "no_state", "malformed", "dup"])
def test_callback_state_failures_are_400_and_set_nothing(
    gateway: str, invoiceops: FakeInvoiceOps, child: str, scenario: str
) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        state, cookie = begin(c)
        invoiceops.codes["code-state-cccccccccccccccccccc"] = child
        before = len(invoiceops.calls)
        if scenario == "no_cookie":
            response = callback(c, state, "code-state-cccccccccccccccccccc", None)
        elif scenario == "wrong_state":
            response = callback(c, "x" * 43, "code-state-cccccccccccccccccccc", cookie)
        elif scenario == "no_state":
            response = callback(c, None, "code-state-cccccccccccccccccccc", cookie)
        elif scenario == "malformed":
            response = callback(
                c, "short", "code-state-cccccccccccccccccccc", "short." + bound("/")
            )
        else:
            response = c.get(
                "/_gateway/callback",
                params={"state": state, "code": "code-state-cccccccccccccccccccc"},
                headers={"Cookie": f"{STATE_COOKIE}={cookie}; {STATE_COOKIE}={cookie}"},
            )
        assert response.status_code == 400
        assert "set-cookie" not in response.headers
        # The code was never presented to InvoiceOps.
        assert len(invoiceops.calls) == before
        assert "code-state-cccccccccccccccccccc" in invoiceops.codes


def test_callback_without_a_code_or_with_a_bad_code_is_400(gateway: str) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        state, cookie = begin(c)
        for code in (None, "", "unknown", "x" * 600):
            response = callback(c, state, code, cookie)
            assert response.status_code == 400
            assert SESSION_COOKIE not in set_cookies(response)


@pytest.mark.parametrize(
    "evil",
    [
        "//evil.example",
        "//evil.example/path",
        "https://evil.example",
        "http:/evil.example",
        "javascript:alert(1)",
        "/\\evil.example",
        "\\\\evil.example",
        "/%2f/evil.example",
        "/%5cevil.example",
        "evil.example",
        "/ok\r\nSet-Cookie: x=1",
        "/_gateway/logout",
        "/" + "a" * 2000,
        "",
    ],
)
def test_return_path_never_becomes_an_open_redirect(
    gateway: str, invoiceops: FakeInvoiceOps, child: str, evil: str
) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        state, _ = begin(c)
        invoiceops.codes["code-evil-cccccccccccccccccccc"] = child
        # A forged state cookie (value the gateway itself would never produce).
        done = callback(c, state, "code-evil-cccccccccccccccccccc", f"{state}.{bound(evil)}")
    assert done.status_code == 302
    assert done.headers["location"] == "/"


def test_navigating_to_a_double_slash_path_never_redirects_off_site(
    gateway: str, invoiceops: FakeInvoiceOps, child: str
) -> None:
    with httpx.Client() as c:
        response = c.get(f"{gateway}//evil.example/x", headers=HTML)
        if response.status_code == 400:  # refused outright is fine too
            return
        state = parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
        cookie = set_cookies(response)[STATE_COOKIE].split(";")[0].split("=", 1)[1]
        invoiceops.codes["code-slash-cccccccccccccccccccc"] = child
        done = c.get(
            f"{gateway}/_gateway/callback",
            params={"state": state, "code": "code-slash-cccccccccccccccccccc"},
            headers={"Cookie": f"{STATE_COOKIE}={cookie}"},
        )
    assert not done.headers["location"].startswith("//")
    assert urlsplit(done.headers["location"]).netloc == ""


def test_a_revoked_child_goes_back_to_the_login_flow(
    gateway: str, invoiceops: FakeInvoiceOps, child: str
) -> None:
    cookie = {"Cookie": f"{SESSION_COOKIE}={child}"}
    with httpx.Client(base_url=gateway, timeout=30) as c:
        assert (
            c.get(
                f"{API}/experiments/search", params={"max_results": 1}, headers=cookie
            ).status_code
            == 200
        )
        invoiceops.sessions.pop(child)  # the app revoked it (logout or parent expiry)

        api = c.get(f"{API}/experiments/search", params={"max_results": 1}, headers=cookie)
        nav = c.get("/", headers={**cookie, **HTML})

    assert api.status_code == 401
    assert nav.status_code == 302
    assert nav.headers["location"].startswith(f"{APP}/api/v1/mlflow/authorize?")
    cookies = set_cookies(nav)
    assert STATE_COOKIE in cookies
    assert "max-age=0" in cookies[SESSION_COOKIE].lower()  # the stale cookie is dropped


def test_logout_clears_the_cookie_and_revokes_the_child(
    gateway: str, invoiceops: FakeInvoiceOps, child: str
) -> None:
    cookie = {"Cookie": f"{SESSION_COOKIE}={child}"}
    with httpx.Client(base_url=gateway, timeout=30) as c:
        assert (
            c.get(
                f"{API}/experiments/search", params={"max_results": 1}, headers=cookie
            ).status_code
            == 200
        )
        # Not from the gateway's own origin: refused, nothing revoked.
        for headers in (cookie, {**cookie, "Origin": APP}, {**cookie, "Origin": "null"}):
            assert c.post("/_gateway/logout", headers=headers).status_code == 403
        assert child in invoiceops.sessions

        done = c.post("/_gateway/logout", headers={**cookie, "Origin": ORIGIN})

        assert done.status_code == 303
        assert done.headers["location"] == f"{APP}/"
        assert "max-age=0" in set_cookies(done)[SESSION_COOKIE].lower()
        assert ("revoke", child) in invoiceops.calls
        assert child not in invoiceops.sessions
        assert (
            c.get(
                f"{API}/experiments/search", params={"max_results": 1}, headers=cookie
            ).status_code
            == 401
        )


def test_failed_revocation_keeps_the_cookie_so_the_user_can_retry(
    gateway: str, invoiceops: FakeInvoiceOps, child: str
) -> None:
    cookie = {"Cookie": f"{SESSION_COOKIE}={child}", "Origin": ORIGIN}
    with httpx.Client(base_url=gateway, timeout=30) as c:
        invoiceops.down = True
        try:
            done = c.post("/_gateway/logout", headers=cookie)
        finally:
            invoiceops.down = False
        assert done.status_code == 503
        assert SESSION_COOKIE not in set_cookies(done)  # nothing cleared: the token is kept
        assert child in invoiceops.sessions

        retry = c.post("/_gateway/logout", headers=cookie)

    assert retry.status_code == 303
    assert "max-age=0" in set_cookies(retry)[SESSION_COOKIE].lower()
    assert child not in invoiceops.sessions


def test_failed_revocation_evicts_the_cached_session(
    gateway: str, invoiceops: FakeInvoiceOps
) -> None:
    token = "mls_cached_" + uuid.uuid4().hex
    invoiceops.add_session(token, [(ORG_A, None)], ttl=30)  # cacheable for 30 s
    cookie = {"Cookie": f"{SESSION_COOKIE}={token}"}
    with httpx.Client(base_url=gateway, timeout=30) as c:
        assert (
            c.get(
                f"{API}/experiments/search", params={"max_results": 1}, headers=cookie
            ).status_code
            == 200
        )
        invoiceops.sessions.pop(token)  # revoked behind the cache's back
        invoiceops.down = True
        try:
            assert (
                c.post("/_gateway/logout", headers={**cookie, "Origin": ORIGIN}).status_code == 503
            )
        finally:
            invoiceops.down = False
        # Without the eviction the cached "active" answer would still be served.
        assert (
            c.get(
                f"{API}/experiments/search", params={"max_results": 1}, headers=cookie
            ).status_code
            == 401
        )


def test_logout_without_a_session_still_clears_and_redirects(gateway: str) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        done = c.post("/_gateway/logout", headers={"Origin": ORIGIN})
    assert done.status_code == 303
    assert "max-age=0" in set_cookies(done)[SESSION_COOKIE].lower()


def test_a_redirect_loop_is_cut_after_two_failed_handoffs(
    gateway: str, invoiceops: FakeInvoiceOps
) -> None:
    # The app vouches for the child but the gateway rejects it (no usable contexts).
    token = "mls_noctx_" + uuid.uuid4().hex
    invoiceops.sessions[token] = {
        "active": True,
        "userId": "u",
        "contexts": [],
        "cacheTtlSeconds": 0,
    }
    attempts = ""
    final: httpx.Response | None = None
    with httpx.Client(base_url=gateway, timeout=30) as c:
        statuses = []
        for _ in range(3):
            headers = dict(HTML)
            if attempts:
                headers["Cookie"] = f"{ATTEMPTS_COOKIE}={attempts}"
            nav = c.get("/", headers=headers)
            statuses.append(nav.status_code)
            if nav.status_code != 302:
                final = nav
                break
            state = parse_qs(urlsplit(nav.headers["location"]).query)["state"][0]
            attempts = set_cookies(nav)[ATTEMPTS_COOKIE].split(";")[0].split("=", 1)[1]
            state_cookie = set_cookies(nav)[STATE_COOKIE].split(";")[0].split("=", 1)[1]
            invoiceops.codes["code-loop-cccccccccccccccccccc"] = token
            done = callback(c, state, "code-loop-cccccccccccccccccccc", state_cookie)
            assert done.status_code == 302
            # The browser now carries the child token and the attempts counter.
            headers = {
                **HTML,
                "Cookie": f"{SESSION_COOKIE}={token}; {ATTEMPTS_COOKIE}={attempts}",
            }
            again = c.get("/", headers=headers)
            statuses.append(again.status_code)
            if again.status_code != 302:
                final = again
                break
            attempts = set_cookies(again)[ATTEMPTS_COOKIE].split(";")[0].split("=", 1)[1]
    assert statuses[-1] == 403
    assert final is not None
    assert "location" not in final.headers
    assert "Sign-in failed" in final.text


def test_a_usable_session_resets_the_attempts_counter(
    gateway: str, invoiceops: FakeInvoiceOps, child: str
) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        ok = c.get(
            f"{API}/experiments/search",
            params={"max_results": 1},
            headers={"Cookie": f"{SESSION_COOKIE}={child}; {ATTEMPTS_COOKIE}=1"},
        )
    assert ok.status_code == 200
    assert "max-age=0" in set_cookies(ok)[ATTEMPTS_COOKIE].lower()


def test_a_forged_attempts_counter_never_helps_an_attacker(gateway: str) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        for junk in ("abc", "-1", "9999999", "1.5", ""):
            nav = c.get("/", headers={**HTML, "Cookie": f"{ATTEMPTS_COOKIE}={junk}"})
            assert nav.status_code == 302  # unparsable counts as zero


def test_bearer_path_is_unchanged_by_the_handoff(gateway: str, tokens: dict[str, str]) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        ok = c.post(
            f"{API}/experiments/search", json={"max_results": 1}, headers=bearer(tokens["a_read"])
        )
        # A valid token on a navigation is served, never redirected into the handoff.
        nav = c.get("/", headers={**bearer(tokens["a_read"]), **HTML})
        bad = c.get("/", headers={**bearer("iop_unknown_token_value"), **HTML})
    assert ok.status_code == 200
    assert nav.status_code == 200
    assert bad.status_code == 401


# ---- callback and logout are prefiltered and throttled --------------------------


def well_formed_unknown_code(number: int) -> str:
    return f"unknown-code-{number:04d}-" + "u" * 20


def test_malformed_codes_and_cookies_never_reach_invoiceops(
    gateway: str, invoiceops: FakeInvoiceOps
) -> None:
    with httpx.Client(base_url=gateway, timeout=30) as c:
        state, cookie = begin(c)
        before = len(invoiceops.calls)
        for junk in ("x", "short", "a b" * 20, "é" * 30, "x" * 129, "a" * 19):
            response = callback(c, state, junk, cookie)
            assert response.status_code == 400
        logout = c.post(
            "/_gateway/logout",
            headers={"Origin": ORIGIN, "Cookie": f"{SESSION_COOKIE}=junk-not-a-session"},
            follow_redirects=False,
        )
        assert logout.status_code == 303  # still logs the browser out locally
        assert len(invoiceops.calls) == before


def test_a_hundred_failed_callbacks_from_one_address_are_throttled(
    make_gateway, invoiceops: FakeInvoiceOps, child: str
) -> None:
    with (
        make_gateway(GATEWAY_CLIENT_IP_HEADER="X-Client-IP") as base,
        httpx.Client(base_url=base, headers={"X-Client-IP": "203.0.113.99"}) as c,
    ):
        state, cookie = begin(c)
        before = len(invoiceops.calls)
        statuses = [
            callback(c, state, well_formed_unknown_code(n), cookie).status_code for n in range(100)
        ]
        reached = len(invoiceops.calls) - before
        assert statuses[:20] == [400] * 20
        assert set(statuses[20:]) == {429}
        assert reached == 20  # nothing past the budget was sent to InvoiceOps
        # Another address still signs in; so does a valid exchange once the budget slides.
        other = {"X-Client-IP": "198.51.100.77"}
        invoiceops.codes["code-fine-cccccccccccccccccccc"] = child
        ok = c.get(
            "/_gateway/callback",
            params={"state": state, "code": "code-fine-cccccccccccccccccccc"},
            headers={**other, "Cookie": f"{STATE_COOKIE}={cookie}"},
            follow_redirects=False,
        )
        assert ok.status_code == 302


def test_failed_revokes_count_towards_the_same_budget(
    make_gateway, invoiceops: FakeInvoiceOps
) -> None:
    session = "mls_revoke_budget_" + "r" * 24
    with (
        make_gateway(
            GATEWAY_CLIENT_IP_HEADER="X-Client-IP", GATEWAY_HANDOFF_FAILURE_LIMIT="3"
        ) as base,
        httpx.Client(base_url=base, headers={"X-Client-IP": "203.0.113.98"}) as c,
    ):
        headers = {"Origin": ORIGIN, "Cookie": f"{SESSION_COOKIE}={session}"}
        invoiceops.down = True
        try:
            first = [c.post("/_gateway/logout", headers=headers).status_code for _ in range(3)]
            before = len(invoiceops.calls)
            blocked = c.post("/_gateway/logout", headers=headers)
        finally:
            invoiceops.down = False
        assert first == [503, 503, 503]
        assert blocked.status_code == 429
        assert len(invoiceops.calls) == before
