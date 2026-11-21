"""ASGI application: authenticate, pick the workspace, enforce scopes, stream to MLflow."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import html
import json
import logging
import re
import secrets
import time
import uuid
from collections import Counter, deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import anyio
import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Route

from .config import Settings
from .identity import (
    WORKSPACE_RE,
    Context,
    IntrospectionUnavailable,
    Introspector,
    Throttled,
    code_format_ok,
    session_format_ok,
    token_format_ok,
)
from .policy import (
    SAFE_METHODS,
    Decision,
    artifact_path_allowed,
    classify,
    is_safe_path,
    model_version_source_allowed,
    query_artifact_path_allowed,
)

logger = logging.getLogger("mlflow_gateway")

CONTEXT_COOKIE = "mlflow_ctx"
CALLBACK_PATH = "/_gateway/callback"
LOGOUT_PATH = "/_gateway/logout"
# Authorize endpoint of the InvoiceOps backend (browser leg of the handoff).
APP_AUTHORIZE_PATH = "/api/v1/mlflow/authorize"
STATE_COOKIE_MAX_AGE = 300
# Loop breaker: handoffs that did not end in a usable session within this window are counted;
# the third one is refused with an error page instead of another redirect.
HANDOFF_ATTEMPTS_WINDOW = 300
MAX_HANDOFF_ATTEMPTS = 2
STATE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
MAX_RETURN_PATH = 1024
MAX_CODE_LENGTH = 512
SERVICE_WORKSPACE_HEADER = "x-invoiceops-workspace"
RESERVED_PREFIX = "/_gateway/"
_PATH_SAFE = "/:@!$&()*+,;=-._~"

# Only these request headers reach MLflow. Cookies, Authorization, Origin/Referer, Host,
# X-Forwarded-*, X-MLFLOW-WORKSPACE and every other client header are dropped.
FORWARDED_REQUEST_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "accept-language",
        "content-type",
        "content-length",
        "content-encoding",
        "range",
        "if-none-match",
        "if-modified-since",
        "user-agent",
    }
)
# Hop-by-hop and anything that could set cookies, prompt for Basic auth or open CORS.
DROPPED_RESPONSE_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "set-cookie",
        "www-authenticate",
        "server",
        "date",
        "access-control-allow-origin",
        "access-control-allow-credentials",
        "access-control-allow-headers",
        "access-control-allow-methods",
        "access-control-expose-headers",
        "access-control-max-age",
    }
)
# CSP for the MLflow UI shell (HTML served through the gateway). Derived from the MLflow 3.16.1
# bundle: one deferred same-origin script, emotion injects inline styles, the telemetry and
# syntax-highlight workers are same-origin (blob: for the latter), images may be data:/blob:
# (markdown/trace previews), fonts are bundled, every fetch/EventSource is same-origin, and
# artifact previews render in blob:/data: frames. No 'unsafe-eval' (see GATEWAY.md).
UI_CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "connect-src 'self'",
        "worker-src 'self' blob:",
        "frame-src 'self' blob: data:",
        "manifest-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'self'",
    ]
)
SESSION_COOKIE_DUPLICATE = object()
# Query parameters the gateway inspects; a repeated one is parsed differently by MLflow.
INSPECTED_PARAMS = ("path", "context")
_BEARER = re.compile(r"^Bearer ([^\s]{1,512})$", re.IGNORECASE)


@dataclass(frozen=True)
class Principal:
    kind: str  # "token" | "session" | "service"
    subject: str
    workspace: str | None
    can_read: bool
    can_write: bool
    contexts: tuple[Context, ...] = ()


class BodyTooLarge(Exception):
    pass


class BodyTooSlow(Exception):
    """The upload did not finish before its absolute deadline."""


class Lease:
    """One in-flight upstream request of a principal; ``release`` is idempotent."""

    def __init__(self, limiter: ConcurrencyLimiter, key: str) -> None:
        self._limiter = limiter
        self._key: str | None = key

    def release(self) -> None:
        key, self._key = self._key, None
        if key is not None:
            self._limiter.release(key)


class ConcurrencyLimiter:
    """Caps the upstream requests one principal can hold at once (per-principal fairness).

    A slow upload or download by one token cannot occupy the shared upstream pool: the
    principal gets 429 beyond its own limit while every other principal keeps being served.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._active: dict[str, int] = {}

    def acquire(self, key: str) -> Lease | None:
        current = self._active.get(key, 0)
        if current >= self._limit:
            return None
        self._active[key] = current + 1
        return Lease(self, key)

    def release(self, key: str) -> None:
        remaining = self._active.get(key, 0) - 1
        if remaining > 0:
            self._active[key] = remaining
        else:
            self._active.pop(key, None)

    @property
    def inflight(self) -> int:
        return sum(self._active.values())


class FailureThrottle:
    """Sliding-window count of failed authentications per client address."""

    MAX_TRACKED = 10_000

    def __init__(self, limit: int, window: float, clock: Callable[[], float] = time.monotonic):
        self._limit = limit
        self._window = window
        self._clock = clock
        self._failures: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float] | None:
        events = self._failures.get(key)
        if events is None:
            return None
        while events and events[0] <= now - self._window:
            events.popleft()
        if not events:
            del self._failures[key]
            return None
        return events

    def blocked(self, key: str) -> bool:
        events = self._prune(key, self._clock())
        return events is not None and len(events) >= self._limit

    def record(self, key: str) -> None:
        now = self._clock()
        if key not in self._failures and len(self._failures) >= self.MAX_TRACKED:
            for stale in [k for k in list(self._failures) if self._prune(k, now) is None]:
                self._failures.pop(stale, None)
            if len(self._failures) >= self.MAX_TRACKED:
                self._failures.pop(next(iter(self._failures)))
        self._failures.setdefault(key, deque()).append(now)


class BackendGate:
    """Per-lookup guard handed to the introspector: it runs only when InvoiceOps will really be
    called (a cache miss). It refuses when the address has spent its budget for this credential
    class and remembers that a backend call happened, so only those lookups can count as failures.
    """

    def __init__(self, throttle: FailureThrottle, client_ip: str) -> None:
        self._throttle = throttle
        self._ip = client_ip
        self.called = False

    def __call__(self) -> None:
        if self._throttle.blocked(self._ip):
            raise Throttled
        self.called = True

    def failed(self) -> None:
        if self.called:
            self._throttle.record(self._ip)


class ProxyResponse(StreamingResponse):
    """Streams the upstream body and ALWAYS closes the upstream and releases the lease.

    Covers normal completion, a client that disconnects mid-stream (the send is cancelled) and
    any error, so a dropped download never keeps an upstream connection or a concurrency slot.
    """

    def __init__(self, upstream: httpx.Response, lease: Lease, **kwargs: Any) -> None:
        super().__init__(upstream.aiter_raw(), **kwargs)
        self._upstream = upstream
        self._lease = lease

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._upstream.aclose()
            self._lease.release()


def error(
    status: int, code: str, message: str, request_id: str, headers: dict[str, str] | None = None
) -> Response:
    response = JSONResponse(
        {"error_code": code, "message": message, "request_id": request_id},
        status_code=status,
        headers=headers,
    )
    response.headers["x-request-id"] = request_id
    response.headers["cache-control"] = "no-store"
    return response


HEALTHZ_PATH = "/_gateway/healthz"


def is_healthcheck(request: Request) -> bool:
    """The container healthcheck polls this every few seconds: it is never logged or counted."""
    return request.scope["path"] == HEALTHZ_PATH and request.method.upper() in SAFE_METHODS


class Provisioner:
    """Creates the MLflow workspace of a context on first authorized use (idempotent)."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client
        self._known: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    async def ensure(self, workspace: str, request_id: str) -> None:
        if workspace in self._known:
            return
        lock = self._locks.setdefault(workspace, asyncio.Lock())
        async with lock:
            if workspace in self._known:
                return
            response = await self._client.post(
                f"{self._settings.upstream_url}/api/3.0/mlflow/workspaces",
                json={"name": workspace, "description": "InvoiceOps context workspace"},
                headers=upstream_auth(self._settings, request_id),
                timeout=httpx.Timeout(10.0),
            )
            # 201 created; MLflow answers "already exists" with RESOURCE_ALREADY_EXISTS
            # (HTTP 400/409), which an earlier or concurrent provisioning explains.
            exists = (
                response.status_code in (400, 409) and "RESOURCE_ALREADY_EXISTS" in response.text
            )
            if response.status_code not in (200, 201) and not exists:
                raise httpx.HTTPError(f"workspace provisioning failed: {response.status_code}")
            self._known.add(workspace)
        self._locks.pop(workspace, None)


def upstream_auth(settings: Settings, request_id: str) -> dict[str, str]:
    basic = base64.b64encode(
        f"{settings.upstream_username}:{settings.upstream_password}".encode()
    ).decode()
    return {"authorization": f"Basic {basic}", "x-request-id": request_id}


def _cookies(request: Request) -> dict[str, str]:
    jar: SimpleCookie = SimpleCookie()
    try:
        jar.load(request.headers.get("cookie", ""))
    except Exception:  # noqa: BLE001 - malformed cookie header is just "no cookie"
        return {}
    return {k: m.value for k, m in jar.items()}


def safe_return_path(value: str | None) -> str:
    """A same-origin relative path (with optional query) or ``/``; never an open redirect.

    Rejects anything that is not a single-slash absolute path: ``//host``, backslashes
    (browsers treat them as ``/``), schemes, control characters, encoded slash/backslash right
    after the leading ``/``, over-long values and the gateway's own reserved routes.
    """
    if not value or len(value) > MAX_RETURN_PATH or not value.startswith("/"):
        return "/"
    if (
        value.startswith("//")
        or "\\" in value
        or any(ord(c) < 0x20 or ord(c) == 0x7F for c in value)
    ):
        return "/"
    if value[1:4].lower() in ("%2f", "%5c"):
        return "/"
    parts = urlsplit(value)
    if parts.scheme or parts.netloc or value.startswith(RESERVED_PREFIX):
        return "/"
    return value


def _session_cookie(request: Request, name: str) -> str | None | object:
    """The cookie value, ``None`` when absent, ``SESSION_COOKIE_DUPLICATE`` when repeated.

    Parsed exactly like the InvoiceOps app (split on ``;``, first ``=``), and a request that
    carries the cookie more than once is never trusted: a repeated cookie must not let
    ordering pick a winner.
    """
    found: list[str] = []
    for part in request.headers.get("cookie", "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            found.append(value)
    if len(found) > 1:
        return SESSION_COOKIE_DUPLICATE
    return found[0] if found else None


def _q(value: str) -> str:
    return quote(value, safe="", encoding="latin-1")


def _query(request: Request) -> dict[str, list[str]]:
    return parse_qs(
        request.scope["query_string"].decode("latin-1"), keep_blank_values=True, encoding="latin-1"
    )


def _wants_html(request: Request) -> bool:
    return request.method == "GET" and "text/html" in request.headers.get("accept", "")


METRICS_LOG_EVERY = 100


class Gateway:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        auth_client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        # Two pools on purpose: a burst of slow uploads/downloads can exhaust ``client`` (the
        # MLflow data path) but must never keep InvoiceOps introspection or workspace
        # provisioning waiting, so those get their own small client and limits.
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=600.0, write=600.0, pool=10.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
            follow_redirects=False,
        )
        self.auth_client = auth_client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=2.0, pool=3.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
            follow_redirects=False,
        )
        self.introspector = Introspector(settings, self.auth_client, clock=clock)
        self.provisioner = Provisioner(settings, self.auth_client)
        self.limiter = ConcurrencyLimiter(settings.max_concurrent_per_principal)
        # One budget per credential class: expired cookies cannot lock out a valid API token, and a
        # flood of junk handoff codes cannot lock out sessions.
        window = settings.auth_failure_window_seconds
        self.token_failures = FailureThrottle(settings.auth_failure_limit, window)
        self.session_failures = FailureThrottle(settings.auth_failure_limit, window)
        self.handoff_failures = FailureThrottle(settings.handoff_failure_limit, window)
        self.counters: Counter[str] = Counter()
        self._requests = 0

    # ---- observability --------------------------------------------------------------

    def subject_hash(self, subject: str) -> str:
        """Keyed, one-way digest of a principal for audit logs (never the subject itself)."""
        return hmac.new(self.settings.audit_key, subject.encode(), "sha256").hexdigest()[:16]

    def client_ip(self, request: Request) -> str:
        header = self.settings.client_ip_header
        if header:
            value = request.headers.get(header, "").split(",")[0].strip()
            if value and len(value) <= 45:
                return value
        return request.client.host if request.client else "unknown"

    def metrics_snapshot(self) -> dict[str, Any]:
        return {"requests": self._requests, "by_outcome": dict(sorted(self.counters.items()))}

    # ---- authentication -------------------------------------------------------------

    def throttled(self, request_id: str, outcome: dict[str, Any]) -> Response:
        outcome["reason"] = "auth_throttled"
        return error(
            429,
            "TOO_MANY_REQUESTS",
            "Too many failed authentication attempts; retry later",
            request_id,
            {"retry-after": str(self.settings.auth_failure_window_seconds)},
        )

    async def authenticate(self, request: Request, client_ip: str) -> Principal | None:
        """The caller, or ``None``. Raises ``Throttled`` (429) only when InvoiceOps would have
        to be asked and this address spent its failure budget for that credential class."""
        header = request.headers.get("authorization")
        if header is not None:
            match = _BEARER.match(header)
            if match is None:
                return None
            secret = match.group(1)
            service = self.settings.service_read_token
            if service is not None and hmac.compare_digest(secret.encode(), service.encode()):
                requested = request.headers.get(SERVICE_WORKSPACE_HEADER, "")
                if not WORKSPACE_RE.match(requested):
                    return Principal("service", "service", None, True, False)
                return Principal("service", "service", requested, True, False)
            if not token_format_ok(secret):
                # Not shaped like an InvoiceOps token: refuse without asking InvoiceOps.
                return None
            gate = BackendGate(self.token_failures, client_ip)
            identity = await self.introspector.token(secret, gate)
            if identity is None:
                gate.failed()  # only a lookup that reached InvoiceOps counts
                return None
            if not identity.can_read:
                return None
            return Principal(
                "token", identity.user_id, identity.context.workspace, True, identity.can_write
            )
        session_token = _session_cookie(request, self.settings.session_cookie_name)
        if session_token is SESSION_COOKIE_DUPLICATE:
            return None
        if isinstance(session_token, str) and session_token:
            if not session_format_ok(session_token):
                return None  # not shaped like a gateway session: no backend call
            gate = BackendGate(self.session_failures, client_ip)
            identity = await self.introspector.session(session_token, gate)
            if identity is None:
                gate.failed()
                return None
            return Principal(
                "session", identity.user_id, None, True, True, contexts=identity.contexts
            )
        return None

    def same_site_navigation(self, request: Request) -> bool:
        """Context switching is a state change through GET: only accept same-site navigations."""
        fetch_site = request.headers.get("sec-fetch-site")
        if fetch_site is not None:
            return fetch_site.lower() in ("same-origin", "same-site", "none")
        referer = urlsplit(request.headers.get("referer", ""))
        origin = f"{referer.scheme}://{referer.netloc}" if referer.netloc else ""
        allowed = {self.settings.public_origin}
        if self.settings.app_origin:
            allowed.add(self.settings.app_origin)
        return origin in allowed

    def select_context(self, principal: Principal, request: Request) -> Context | None:
        by_name = {c.workspace: c for c in principal.contexts}
        chosen = _cookies(request).get(CONTEXT_COOKIE)
        if chosen in by_name:
            return by_name[chosen]
        if len(principal.contexts) == 1:
            return principal.contexts[0]
        return None

    # ---- responses ------------------------------------------------------------------

    def unauthenticated(self, request: Request, request_id: str) -> Response:
        # A client that sent its own credential (Bearer) is an API client, not a browser.
        if _wants_html(request) and "authorization" not in request.headers:
            return self.begin_login(request, request_id)
        return error(
            401,
            "UNAUTHENTICATED",
            "A valid InvoiceOps API token (Authorization: Bearer) or session is required",
            request_id,
            {"www-authenticate": 'Bearer realm="mlflow"'},
        )

    def _cookie_kwargs(self, max_age: int | None = None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "path": "/",
            "httponly": True,
            "secure": self.settings.secure_cookies,
            "samesite": "lax",
        }
        if max_age is not None:
            kwargs["max_age"] = max_age
        return kwargs

    def _clear_cookie(self, response: Response, name: str) -> None:
        response.delete_cookie(
            name, path="/", secure=self.settings.secure_cookies, httponly=True, samesite="lax"
        )

    def begin_login(self, request: Request, request_id: str) -> Response:
        """Browser without a (valid) gateway session: hand off to InvoiceOps and come back."""
        attempts = self._handoff_attempts(request)
        if attempts >= MAX_HANDOFF_ATTEMPTS:
            return self._handoff_loop(request, request_id)
        state = secrets.token_urlsafe(32)  # 256 bits
        target = quote(request.scope["path"], safe=_PATH_SAFE)
        query = request.scope["query_string"].decode("latin-1")
        return_path = safe_return_path(target + (f"?{query}" if query else ""))
        bound = base64.urlsafe_b64encode(return_path.encode()).decode().rstrip("=")
        location = f"{self.settings.app_origin}{APP_AUTHORIZE_PATH}?" + urlencode(
            {"redirect_uri": self.settings.callback_url, "state": state}
        )
        response = Response(
            status_code=302,
            headers={
                "location": location,
                "cache-control": "no-store",
                "x-request-id": request_id,
            },
        )
        response.set_cookie(
            self.settings.state_cookie_name,
            f"{state}.{bound}",
            **self._cookie_kwargs(STATE_COOKIE_MAX_AGE),
        )
        response.set_cookie(
            self.settings.attempts_cookie_name,
            str(attempts + 1),
            **self._cookie_kwargs(HANDOFF_ATTEMPTS_WINDOW),
        )
        # A stale or revoked child session must not linger next to the new one.
        if _session_cookie(request, self.settings.session_cookie_name) is not None:
            self._clear_cookie(response, self.settings.session_cookie_name)
        return response

    def _handoff_attempts(self, request: Request) -> int:
        raw = _session_cookie(request, self.settings.attempts_cookie_name)
        if isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) <= 3:
            return int(raw)
        return 0

    def _handoff_loop(self, request: Request, request_id: str) -> Response:
        """The app keeps vouching for a session the gateway then rejects: stop redirecting."""
        page: Response = HTMLResponse(
            "<!doctype html><title>Sign-in failed</title><h1>Sign-in failed</h1>"
            "<p>Your account could not be signed in to MLflow. Contact an administrator "
            "or try again in a few minutes.</p>",
            status_code=403,
        )
        page.headers["content-security-policy"] = UI_CSP
        page.headers["x-request-id"] = request_id
        page.headers["cache-control"] = "no-store"
        self._clear_cookie(page, self.settings.state_cookie_name)
        if _session_cookie(request, self.settings.session_cookie_name) is not None:
            self._clear_cookie(page, self.settings.session_cookie_name)
        return page

    def _bad_callback(self, request_id: str, *, clear_state: bool) -> Response:
        page: Response = HTMLResponse(
            "<!doctype html><title>Sign-in failed</title><h1>Sign-in failed</h1>"
            '<p>The sign-in link is invalid or expired. <a href="/">Try again</a></p>',
            status_code=400,
        )
        page.headers["content-security-policy"] = UI_CSP
        page.headers["x-request-id"] = request_id
        page.headers["cache-control"] = "no-store"
        if clear_state:
            self._clear_cookie(page, self.settings.state_cookie_name)
        return page

    async def callback(
        self, request: Request, request_id: str, outcome: dict[str, Any], client_ip: str
    ) -> Response:
        query = _query(request)
        states, codes = query.get("state", []), query.get("code", [])
        stored = _session_cookie(request, self.settings.state_cookie_name)
        state = states[0] if len(states) == 1 else ""
        cookie_state, _, bound = (stored if isinstance(stored, str) else "").partition(".")
        # Constant-time comparison; nothing is set or cleared when the state does not match.
        if (
            not STATE_RE.match(state)
            or not STATE_RE.match(cookie_state)
            or not hmac.compare_digest(state.encode(), cookie_state.encode())
        ):
            outcome["reason"] = "state_mismatch"
            return self._bad_callback(request_id, clear_state=False)
        try:
            padded = bound + "=" * (-len(bound) % 4)
            return_path = safe_return_path(base64.urlsafe_b64decode(padded).decode())
        except ValueError:
            return_path = "/"
        code = codes[0] if len(codes) == 1 else ""
        if not code or len(code) > MAX_CODE_LENGTH:
            outcome["reason"] = "code_missing"
            return self._bad_callback(request_id, clear_state=True)
        if not code_format_ok(code):
            outcome["reason"] = "code_malformed"  # prefilter: InvoiceOps is never asked
            return self._bad_callback(request_id, clear_state=True)
        if self.handoff_failures.blocked(client_ip):
            return self.throttled(request_id, outcome)
        exchanged = await self.introspector.exchange(code)
        if exchanged is None:
            self.handoff_failures.record(client_ip)
            outcome["reason"] = "code_rejected"
            return self._bad_callback(request_id, clear_state=True)
        token, expires_in = exchanged
        response = Response(
            status_code=302,
            headers={
                "location": return_path,
                "cache-control": "no-store",
                "referrer-policy": "no-referrer",
                "x-request-id": request_id,
            },
        )
        response.set_cookie(
            self.settings.session_cookie_name,
            token,
            **self._cookie_kwargs(min(expires_in, 30 * 86400)),
        )
        self._clear_cookie(response, self.settings.state_cookie_name)
        return response

    async def logout(
        self, request: Request, request_id: str, outcome: dict[str, Any], client_ip: str
    ) -> Response:
        if request.headers.get("origin") != self.settings.public_origin:
            outcome["reason"] = "csrf"
            return error(403, "FORBIDDEN", "Cross-origin request refused", request_id)
        token = _session_cookie(request, self.settings.session_cookie_name)
        # A cookie that is not shaped like a gateway session cannot be a session: skip the
        # backend and just clear it.
        if isinstance(token, str) and session_format_ok(token):
            if self.handoff_failures.blocked(client_ip):
                return self.throttled(request_id, outcome)
            try:
                await self.introspector.revoke(token)
            except IntrospectionUnavailable:
                self.handoff_failures.record(client_ip)
                outcome["reason"] = "revoke_unavailable"
                # Keep the cookies: the browser still holds the only copy of the token, so
                # the user can retry. The cached answer is evicted so it is not served stale.
                self.introspector.forget_session(token)
                return error(
                    503, "UNAVAILABLE", "Could not revoke the session; try again", request_id
                )
        response: Response = Response(
            status_code=303,
            headers={
                "location": f"{self.settings.app_origin}/",
                "cache-control": "no-store",
                "x-request-id": request_id,
            },
        )
        self._clear_cookie(response, self.settings.session_cookie_name)
        self._clear_cookie(response, CONTEXT_COOKIE)
        return response

    def context_page(self, principal: Principal, request_id: str, status: int = 200) -> Response:
        items = "".join(
            f'<li><a href="/?context={quote(c.workspace)}">{html.escape(c.label)}</a></li>'
            for c in principal.contexts
        )
        page = HTMLResponse(
            "<!doctype html><title>Choose a context</title><h1>Choose a context</h1>"
            f"<ul>{items}</ul>",
            status_code=status,
        )
        page.headers["content-security-policy"] = UI_CSP
        page.headers["x-request-id"] = request_id
        page.headers["cache-control"] = "no-store"
        return page

    def set_context_cookie(self, response: Response, workspace: str) -> None:
        response.set_cookie(
            CONTEXT_COOKIE,
            workspace,
            max_age=self.settings.context_cookie_max_age,
            path="/",
            httponly=True,
            secure=self.settings.secure_cookies,
            samesite="lax",
        )

    # ---- request handling -----------------------------------------------------------

    async def handle(self, request: Request) -> Response:
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        outcome: dict[str, Any] = {}
        try:
            response = await self._handle(request, request_id, outcome)
            if (
                outcome.get("principal") == "session"
                and _session_cookie(request, self.settings.attempts_cookie_name) is not None
            ):
                # A usable session ends the streak of failed handoffs.
                self._clear_cookie(response, self.settings.attempts_cookie_name)
        except IntrospectionUnavailable:
            outcome["reason"] = "introspection_unavailable"
            response = error(503, "UNAVAILABLE", "Authentication service unavailable", request_id)
        except BodyTooLarge:
            outcome["reason"] = "body_too_large"
            response = error(413, "PAYLOAD_TOO_LARGE", "Request body too large", request_id)
        except BodyTooSlow:
            outcome["reason"] = "body_too_slow"
            response = error(408, "REQUEST_TIMEOUT", "The upload took too long", request_id)
        except httpx.TimeoutException:
            outcome["reason"] = "upstream_timeout"
            response = error(504, "UPSTREAM_TIMEOUT", "MLflow timed out", request_id)
        except httpx.HTTPError as exc:
            outcome["reason"] = f"upstream_error:{type(exc).__name__}"
            response = error(502, "BAD_GATEWAY", "MLflow is unavailable", request_id)
        if not is_healthcheck(request):
            self._record(request, request_id, started, response.status_code, outcome)
        return response

    def _record(
        self,
        request: Request,
        request_id: str,
        started: float,
        status: int,
        outcome: dict[str, Any],
    ) -> None:
        subject = outcome.pop("subject", None)
        fields: dict[str, Any] = {}
        if subject is not None and request.method.upper() not in SAFE_METHODS:
            # Audit trail of who mutated what: a keyed one-way digest, never the subject.
            fields["principal_hash"] = self.subject_hash(subject)
        logger.info(
            json.dumps(
                {
                    "event": "request",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.scope["path"],
                    "status": status,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    **outcome,
                    **fields,
                }
            )
        )
        self._requests += 1
        self.counters[str(outcome.get("reason", "ok"))] += 1
        if self._requests % METRICS_LOG_EVERY == 0:
            logger.info(json.dumps({"event": "metrics", **self.metrics_snapshot()}))

    async def _handle(self, request: Request, request_id: str, outcome: dict[str, Any]) -> Response:
        path = request.scope["path"]
        method = request.method.upper()
        if not is_safe_path(path):
            outcome["reason"] = "unsafe_path"
            return error(400, "BAD_REQUEST", "Invalid path", request_id)
        if is_healthcheck(request):
            return PlainTextResponse("ok", headers={"cache-control": "no-store"})
        query = _query(request)
        if any(len(query.get(name, ())) > 1 for name in INSPECTED_PARAMS):
            outcome["reason"] = "duplicate_query_param"
            return error(400, "BAD_REQUEST", "Repeated query parameter", request_id)

        client_ip = self.client_ip(request)
        if path == CALLBACK_PATH and method == "GET":
            return await self.callback(request, request_id, outcome, client_ip)
        if path == LOGOUT_PATH and method == "POST":
            return await self.logout(request, request_id, outcome, client_ip)

        try:
            principal = await self.authenticate(request, client_ip)
        except Throttled:
            return self.throttled(request_id, outcome)
        if principal is None:
            outcome["reason"] = "unauthenticated"
            return self.unauthenticated(request, request_id)
        outcome.update(principal=principal.kind, subject=principal.subject)

        # Browser context selection: only for session principals, only via the gateway.
        if principal.kind == "session":
            if path == "/_gateway/contexts" and method == "GET":
                return self.context_page(principal, request_id)
            if method == "GET" and "context" in query and self.same_site_navigation(request):
                wanted = query["context"][0]
                if wanted not in {c.workspace for c in principal.contexts}:
                    outcome["reason"] = "context_not_allowed"
                    return error(403, "FORBIDDEN", "Context not available to this user", request_id)
                rest = "&".join(
                    f"{_q(k)}={_q(v)}" for k, vs in query.items() if k != "context" for v in vs
                )
                target = quote(path, safe=_PATH_SAFE)
                redirect = Response(
                    status_code=303,
                    headers={
                        "location": target + (f"?{rest}" if rest else ""),
                        "cache-control": "no-store",
                    },
                )
                self.set_context_cookie(redirect, wanted)
                return redirect
        if path.startswith(RESERVED_PREFIX):
            return error(404, "NOT_FOUND", "Not found", request_id)

        decision = classify(method, path)
        if decision is None:
            outcome["reason"] = "endpoint_not_allowed"
            return error(403, "FORBIDDEN", "This MLflow endpoint is not available", request_id)
        if decision.access == "write" and not principal.can_write:
            outcome["reason"] = "scope_write_missing"
            return error(403, "FORBIDDEN", "Token lacks the mlflow:write scope", request_id)

        workspace = principal.workspace
        if principal.kind == "session":
            # CSRF: the gateway's own origin is the only one allowed to write.
            if (
                method not in SAFE_METHODS
                and request.headers.get("origin") != self.settings.public_origin
            ):
                outcome["reason"] = "csrf"
                return error(403, "FORBIDDEN", "Cross-origin request refused", request_id)
            selected = self.select_context(principal, request)
            if selected is None:
                outcome["reason"] = "context_required"
                if _wants_html(request):
                    return self.context_page(principal, request_id, status=200)
                return error(
                    409,
                    "CONTEXT_REQUIRED",
                    "Several contexts available: open /_gateway/contexts and choose one",
                    request_id,
                )
            workspace = selected.workspace
        if workspace is None:
            outcome["reason"] = "workspace_required"
            return error(
                400,
                "BAD_REQUEST",
                f"Service callers must send {SERVICE_WORKSPACE_HEADER}",
                request_id,
            )
        outcome.update(workspace=workspace)

        if decision.kind == "workspace_list":
            return self._workspace_json(principal, workspace, request_id, listing=True)
        if decision.kind == "workspace_get":
            name = path.rsplit("/", 1)[-1]
            if name != workspace:
                outcome["reason"] = "workspace_not_allowed"
                return error(403, "FORBIDDEN", "Workspace not available", request_id)
            return self._workspace_json(principal, workspace, request_id, listing=False)

        body: bytes | None = None
        limit = self.settings.max_body_bytes
        if decision.kind == "artifact":
            limit = self.settings.max_artifact_bytes
            if not artifact_path_allowed(decision.artifact_path or "", workspace):
                outcome["reason"] = "artifact_path_not_allowed"
                return error(403, "FORBIDDEN", "Artifact path outside the workspace", request_id)
            if method in SAFE_METHODS and not decision.artifact_path:
                if not query_artifact_path_allowed(query.get("path", [""])[0], workspace):
                    outcome["reason"] = "artifact_path_not_allowed"
                    return error(
                        403, "FORBIDDEN", "Artifact path outside the workspace", request_id
                    )
        elif path in ("/get-artifact", "/model-versions/get-artifact"):
            rel = query.get("path", [""])[0]
            if any(s in ("..",) for s in rel.split("/")) or rel.startswith("/"):
                outcome["reason"] = "artifact_path_not_allowed"
                return error(403, "FORBIDDEN", "Invalid artifact path", request_id)
        if decision.kind == "model_version_create":
            body = await self._read_body(request, limit)
            try:
                payload = json.loads(body or b"{}")
            except ValueError:
                return error(400, "BAD_REQUEST", "Invalid JSON body", request_id)
            if not isinstance(payload, dict) or not model_version_source_allowed(
                payload.get("source"), workspace
            ):
                outcome["reason"] = "model_source_not_allowed"
                return error(
                    403,
                    "FORBIDDEN",
                    "Model version source must be runs:/, models:/ or this workspace's artifacts",
                    request_id,
                )

        if decision.access == "write":
            # Only a write creates the workspace; reads never do.
            await self.provisioner.ensure(workspace, request_id)
        lease = self.limiter.acquire(f"{principal.kind}:{principal.subject}")
        if lease is None:
            outcome["reason"] = "concurrency_limit"
            return error(
                429,
                "TOO_MANY_REQUESTS",
                "Too many concurrent requests for this principal",
                request_id,
                {"retry-after": "1"},
            )
        return await self._forward(request, decision, workspace, request_id, body, limit, lease)

    def _workspace_json(
        self, principal: Principal, workspace: str, request_id: str, listing: bool
    ) -> Response:
        item = {"name": workspace, "description": "InvoiceOps context workspace"}
        content = {"workspaces": [item]} if listing else {"workspace": item}
        return JSONResponse(
            content, headers={"x-request-id": request_id, "cache-control": "no-store"}
        )

    async def _read_body(self, request: Request, limit: int) -> bytes:
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            raise BodyTooLarge
        return b"".join([chunk async for chunk in self._limited(request, limit)])

    async def _limited(self, request: Request, limit: int) -> AsyncIterator[bytes]:
        """Request body with a byte cap and an ABSOLUTE deadline (not an idle timeout).

        A client that trickles one byte at a time would never trip an idle timeout, so the
        whole upload gets ``upload_deadline_seconds`` and each wait is bounded by what is left.
        """
        deadline = time.monotonic() + self.settings.upload_deadline_seconds
        total = 0
        chunks = request.stream().__aiter__()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise BodyTooSlow
            try:
                chunk = await asyncio.wait_for(chunks.__anext__(), remaining)
            except StopAsyncIteration:
                return
            except TimeoutError:
                raise BodyTooSlow from None
            total += len(chunk)
            if total > limit:
                raise BodyTooLarge
            yield chunk

    async def _forward(
        self,
        request: Request,
        decision: Decision,
        workspace: str,
        request_id: str,
        body: bytes | None,
        limit: int,
        lease: Lease,
    ) -> Response:
        try:
            return await self._send_upstream(
                request, decision, workspace, request_id, body, limit, lease
            )
        except BaseException:
            lease.release()
            raise

    async def _send_upstream(
        self,
        request: Request,
        decision: Decision,
        workspace: str,
        request_id: str,
        body: bytes | None,
        limit: int,
        lease: Lease,
    ) -> Response:
        headers = {
            k: v for k, v in request.headers.items() if k.lower() in FORWARDED_REQUEST_HEADERS
        }
        if body is not None:
            headers["content-length"] = str(len(body))
        headers.update(upstream_auth(self.settings, request_id))
        headers["x-mlflow-workspace"] = workspace  # overwrites anything the client sent
        url = f"{self.settings.upstream_url}{quote(request.scope['path'], safe=_PATH_SAFE)}"
        query = request.scope["query_string"]
        if query:
            url += "?" + query.decode("latin-1")

        content: Any
        if body is not None:
            content = body
        elif (
            request.headers.get("content-length", "0") != "0"
            or "transfer-encoding" in request.headers
        ):
            declared = request.headers.get("content-length")
            if declared is not None and declared.isdigit() and int(declared) > limit:
                raise BodyTooLarge
            content = self._limited(request, limit)
        else:
            content = None
        upstream_request = self.client.build_request(
            request.method, url, headers=headers, content=content
        )
        upstream = await self.client.send(upstream_request, stream=True)
        if upstream.status_code == 401:
            await upstream.aclose()
            lease.release()
            logger.error(json.dumps({"event": "upstream_auth_failed", "request_id": request_id}))
            return error(502, "BAD_GATEWAY", "MLflow rejected the gateway identity", request_id)
        out_headers = {
            k: v for k, v in upstream.headers.items() if k.lower() not in DROPPED_RESPONSE_HEADERS
        }
        out_headers["x-request-id"] = request_id
        out_headers["x-content-type-options"] = "nosniff"
        if decision.kind == "artifact" or request.scope["path"].endswith("get-artifact"):
            # User-supplied files must never run as a page on the MLflow origin.
            out_headers["content-security-policy"] = "sandbox; default-src 'none'"
        elif out_headers.get("content-type", "").lower().startswith("text/html"):
            out_headers["content-security-policy"] = UI_CSP
        out_headers["cache-control"] = (
            "no-store"
            if decision.kind != "static"
            else out_headers.get("cache-control", "no-cache")
        )
        return ProxyResponse(upstream, lease, status_code=upstream.status_code, headers=out_headers)

    async def close(self) -> None:
        await self.client.aclose()
        await self.auth_client.aclose()


def create_app(
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    auth_client: httpx.AsyncClient | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Starlette:
    gateway = Gateway(settings, client, auth_client, clock=clock)

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await gateway.close()

    async def endpoint(request: Request) -> Response:
        return await gateway.handle(request)

    app = Starlette(
        routes=[
            Route(
                "/{path:path}",
                endpoint,
                methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            )
        ],
        lifespan=lifespan,
    )
    app.state.gateway = gateway
    return app
