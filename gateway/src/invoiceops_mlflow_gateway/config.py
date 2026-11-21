"""Environment-driven settings. Every invalid value refuses to start (fail early)."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

MIN_SECRET_LENGTH = 32
MAX_SECRET_LENGTH = 512
CACHE_MAX_ACTIVE_SECONDS = 30
CACHE_MAX_INACTIVE_SECONDS = 5

_PLACEHOLDER = re.compile(r"replace-with|changeme", re.IGNORECASE)


class ConfigError(ValueError):
    """Raised for a missing or invalid setting; the message never contains a secret."""


def validate_secret(name: str, value: str | None, *, required: bool = True) -> str | None:
    if value is None or value == "":
        if required:
            raise ConfigError(f"{name} must be set")
        return None
    if len(value) < MIN_SECRET_LENGTH or len(value) > MAX_SECRET_LENGTH:
        raise ConfigError(
            f"{name} must be between {MIN_SECRET_LENGTH} and {MAX_SECRET_LENGTH} characters"
        )
    if re.search(r"\s", value):
        raise ConfigError(f"{name} must not contain whitespace")
    if _PLACEHOLDER.search(value):
        raise ConfigError(f"{name} must not use the example placeholder")
    return value


def _url(name: str, value: str | None) -> str:
    if not value:
        raise ConfigError(f"{name} must be set")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError(f"{name} must be an absolute http(s) URL")
    return value.rstrip("/")


_DEFAULT_PORTS = {"http": 80, "https": 443}


def normalize_origin(name: str, value: str | None) -> str:
    """Canonical form of a bare origin: lowercase scheme and host, default port dropped.

    Mirrors the app's ``new URL(value).origin`` so both sides compare the same string.
    Userinfo, path, query and fragment are refused (a lone trailing ``/`` is tolerated).
    """
    if not value:
        raise ConfigError(f"{name} must be set")
    value = value.strip()
    if any(c in value for c in "?#"):
        raise ConfigError(f"{name} must be an origin without path, query or fragment")
    parts = urlsplit(value)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not parts.netloc:
        raise ConfigError(f"{name} must be an absolute http(s) URL")
    if "@" in parts.netloc:
        raise ConfigError(f"{name} must not contain credentials")
    if parts.path not in ("", "/"):
        raise ConfigError(f"{name} must be an origin without path, e.g. https://mlflow.example.com")
    try:
        port = parts.port
    except ValueError as error:
        raise ConfigError(f"{name} has an invalid port") from error
    host = (parts.hostname or "").lower()
    if not host:
        raise ConfigError(f"{name} must be an absolute http(s) URL")
    if ":" in host:
        host = f"[{host}]"
    suffix = "" if port is None or port == _DEFAULT_PORTS[scheme] else f":{port}"
    return f"{scheme}://{host}{suffix}"


def _flag(env: Mapping[str, str], name: str) -> bool:
    raw = env.get(name, "").strip().lower()
    if raw in ("", "0", "false"):
        return False
    if raw in ("1", "true"):
        return True
    raise ConfigError(f"{name} must be 1 or 0")


def _int(env: Mapping[str, str], name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")


def _header_name(env: Mapping[str, str], name: str) -> str:
    raw = env.get(name, "").strip().lower()
    if raw and not _HEADER_NAME.match(raw):
        raise ConfigError(f"{name} must be an HTTP header name")
    return raw


@dataclass(frozen=True)
class Settings:
    upstream_url: str
    upstream_username: str
    upstream_password: str
    introspection_url: str
    introspection_secret: str
    public_origin: str
    service_read_token: str | None
    cache_active_seconds: int
    cache_inactive_seconds: int
    max_artifact_bytes: int
    max_body_bytes: int
    context_cookie_max_age: int
    app_origin: str
    # Abuse bounds . Defaults keep every existing deployment working.
    max_concurrent_per_principal: int = 16
    upload_deadline_seconds: int = 3600
    # Failed lookups that reached InvoiceOps, counted per client address AND credential class
    # (token / session / handoff), so students behind one NAT do not block each other and a flood
    # of expired cookies never blocks a valid API token.
    auth_failure_limit: int = 120
    auth_failure_window_seconds: int = 60
    # Failed handoff-code exchanges and logout revokes per client address. A person
    # makes one of these per sign-in, so the budget is much smaller.
    handoff_failure_limit: int = 20
    # Header that carries the client IP behind the tunnel (e.g. ``cf-connecting-ip``). Empty:
    # the TCP peer address is used. Only set it when the gateway is reachable solely through
    # the proxy that writes the header.
    client_ip_header: str = ""
    # Dedicated key of the audit digest (GATEWAY_AUDIT_HMAC_KEY). Empty: derived from the
    # introspection secret, see ``audit_key``.
    audit_hmac_key: str | None = field(default=None, repr=False)

    @property
    def audit_key(self) -> bytes:
        """Key of ``principal_hash`` in the audit log: dedicated, else derived (never the secret).

        Default derivation: ``HMAC-SHA-256(GATEWAY_INTROSPECTION_SECRET,
        "invoiceops-mlflow-gateway/audit-key/v1")``. The introspection secret itself is never used
        as an HMAC key for logs, so a leaked log digest says nothing about it.
        """
        if self.audit_hmac_key:
            return self.audit_hmac_key.encode()
        return hmac.new(
            self.introspection_secret.encode(),
            b"invoiceops-mlflow-gateway/audit-key/v1",
            hashlib.sha256,
        ).digest()

    @property
    def secure_cookies(self) -> bool:
        return self.public_origin.startswith("https://")

    @property
    def session_cookie_name(self) -> str:
        """Gateway session cookie: host-only, so the ``__Host-`` prefix whenever it is Secure."""
        return "__Host-mlflow_session" if self.secure_cookies else "mlflow_session"

    @property
    def state_cookie_name(self) -> str:
        return "__Host-mlflow_oauth_state" if self.secure_cookies else "mlflow_oauth_state"

    @property
    def attempts_cookie_name(self) -> str:
        """Counts consecutive handoffs that did not end in a usable session (loop breaker)."""
        return (
            "__Host-mlflow_handoff_attempts" if self.secure_cookies else "mlflow_handoff_attempts"
        )

    @property
    def callback_url(self) -> str:
        """The exact redirect_uri the app is configured to accept."""
        return f"{self.public_origin}/_gateway/callback"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        username = env.get("GATEWAY_UPSTREAM_USERNAME", "")
        if not username or _PLACEHOLDER.search(username):
            raise ConfigError("GATEWAY_UPSTREAM_USERNAME must be set")
        public_origin = normalize_origin("GATEWAY_PUBLIC_ORIGIN", env.get("GATEWAY_PUBLIC_ORIGIN"))
        app_origin = normalize_origin("GATEWAY_APP_ORIGIN", env.get("GATEWAY_APP_ORIGIN"))
        # Plain http is for tests and local development only (explicit opt-in).
        if not _flag(env, "GATEWAY_ALLOW_INSECURE_HTTP"):
            for origin_name, origin in (
                ("GATEWAY_PUBLIC_ORIGIN", public_origin),
                ("GATEWAY_APP_ORIGIN", app_origin),
            ):
                if origin.startswith("http://"):
                    raise ConfigError(
                        f"{origin_name} must use https (GATEWAY_ALLOW_INSECURE_HTTP=1 is for "
                        "local development and tests only)"
                    )
        if public_origin.startswith("https://") and not app_origin.startswith("https://"):
            raise ConfigError("GATEWAY_APP_ORIGIN must use https when GATEWAY_PUBLIC_ORIGIN does")
        return cls(
            upstream_url=_url("GATEWAY_UPSTREAM_URL", env.get("GATEWAY_UPSTREAM_URL")),
            upstream_username=username,
            upstream_password=str(
                validate_secret("GATEWAY_UPSTREAM_PASSWORD", env.get("GATEWAY_UPSTREAM_PASSWORD"))
            ),
            introspection_url=_url(
                "GATEWAY_INTROSPECTION_URL", env.get("GATEWAY_INTROSPECTION_URL")
            ),
            introspection_secret=str(
                validate_secret(
                    "GATEWAY_INTROSPECTION_SECRET", env.get("GATEWAY_INTROSPECTION_SECRET")
                )
            ),
            public_origin=public_origin,
            service_read_token=validate_secret(
                "GATEWAY_SERVICE_READ_TOKEN", env.get("GATEWAY_SERVICE_READ_TOKEN"), required=False
            ),
            cache_active_seconds=_int(
                env,
                "GATEWAY_CACHE_ACTIVE_SECONDS",
                CACHE_MAX_ACTIVE_SECONDS,
                minimum=0,
                maximum=CACHE_MAX_ACTIVE_SECONDS,
            ),
            cache_inactive_seconds=_int(
                env,
                "GATEWAY_CACHE_INACTIVE_SECONDS",
                CACHE_MAX_INACTIVE_SECONDS,
                minimum=0,
                maximum=CACHE_MAX_INACTIVE_SECONDS,
            ),
            max_artifact_bytes=_int(
                env, "GATEWAY_MAX_ARTIFACT_BYTES", 10 * 1024**3, minimum=1, maximum=1024**4
            ),
            max_body_bytes=_int(
                env, "GATEWAY_MAX_BODY_BYTES", 10 * 1024**2, minimum=1024, maximum=1024**3
            ),
            context_cookie_max_age=_int(
                env, "GATEWAY_CONTEXT_COOKIE_MAX_AGE", 12 * 3600, minimum=60, maximum=30 * 86400
            ),
            app_origin=app_origin,
            max_concurrent_per_principal=_int(
                env, "GATEWAY_MAX_CONCURRENT_PER_PRINCIPAL", 16, minimum=1, maximum=1000
            ),
            upload_deadline_seconds=_int(
                env, "GATEWAY_UPLOAD_DEADLINE_SECONDS", 3600, minimum=1, maximum=86400
            ),
            auth_failure_limit=_int(
                env, "GATEWAY_AUTH_FAILURE_LIMIT", 120, minimum=1, maximum=10000
            ),
            auth_failure_window_seconds=_int(
                env, "GATEWAY_AUTH_FAILURE_WINDOW_SECONDS", 60, minimum=1, maximum=3600
            ),
            handoff_failure_limit=_int(
                env, "GATEWAY_HANDOFF_FAILURE_LIMIT", 20, minimum=1, maximum=10000
            ),
            client_ip_header=_header_name(env, "GATEWAY_CLIENT_IP_HEADER"),
            audit_hmac_key=validate_secret(
                "GATEWAY_AUDIT_HMAC_KEY", env.get("GATEWAY_AUDIT_HMAC_KEY"), required=False
            ),
        )
