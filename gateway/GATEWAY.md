# InvoiceOps MLflow gateway

> Source of truth for how the gateway behaves. Deployment, rollout, backup and operational risks are in
> [`../GATEWAY.md`](../GATEWAY.md) (Spanish), which links here instead of repeating these rules.

Starlette + httpx reverse proxy in front of MLflow 3.16.1 (basic-auth app, workspaces enabled).
MLflow is never exposed directly: the gateway authenticates the caller, maps it to one MLflow
workspace, enforces an endpoint allowlist and streams the request upstream with its own admin
credentials. Anything not explicitly allowed is denied.

## Browser sign-in: redirect handoff, host-only cookies

The app (`https://invoiceops.aglai.dev`) and the gateway (`https://mlflow-invoiceops.aglai.dev`) share
only the parent domain, so there is **no shared session cookie**: each side keeps a host-only cookie
and `SESSION_COOKIE_DOMAIN` must stay unset in the app.

1. A browser navigation (`GET`, `Accept: text/html`, no `Authorization` header) without a valid
   gateway session gets `302` to `<APP_PUBLIC_URL>/api/v1/mlflow/authorize?redirect_uri=<gateway
   origin>/_gateway/callback&state=<random>`. The 256-bit `state` is bound to a `__Host-` state cookie
   (`Secure; HttpOnly; SameSite=Lax; Path=/`, 5 min) that also carries the return path (a relative
   path only: `//`, backslashes, schemes, control characters and the gateway's own routes fall back to `/`).
   API/XHR requests (no `text/html`, or with `Authorization`) keep getting `401`.
2. The app checks `redirect_uri` against its configured value (`MLFLOW_PUBLIC_URL` +
   `/_gateway/callback`), then redirects back with a single-use, 60 s `code` and the `state`.
3. `GET /_gateway/callback` compares `state` with the state cookie in constant time (mismatch or
   missing: `400`, nothing set), clears the state cookie, exchanges the code server-to-server
   (`POST /internal/sessions/exchange`, service secret; replays and expired codes fail) and stores the
   returned **MLflow-only child token** in the `__Host-mlflow_session` cookie (`Secure; HttpOnly;
   SameSite=Lax; Path=/`, no `Domain`), then redirects to the validated return path. Over plain http
   (local runs) the cookies lose the `__Host-` prefix because it requires `Secure`.
4. Every request resolves the child token through `/internal/sessions/introspect` (cache <= 30 s).
   The child cannot authenticate the app, dies with the app session (logout, password change,
   deactivation, expiry), and lives at most 8 h. A revoked child sends the browser back through step 1.
5. `POST /_gateway/logout` (needs `Origin == GATEWAY_PUBLIC_ORIGIN`) revokes the child through
   `/internal/sessions/revoke`, clears the gateway cookies and redirects to the app. If the revocation call
   fails it answers `503` and **keeps** the cookie (and evicts the cached answer) so the user can retry; the
   cookie is cleared only once the revoke succeeded (it is idempotent, so an already-inactive token also
   succeeds). App logout also kills every child.
6. Loop breaker: each handoff increments a short-lived `__Host-mlflow_handoff_attempts` cookie (5 min) and a
   request that authenticates with a usable session clears it. After 2 handoffs that did not end in a usable
   session (for example the app reports a child active but the gateway rejects its contexts) the next
   navigation gets a plain `403` page instead of another redirect.

## Architecture

`client -> gateway -> MLflow`. The gateway also calls the InvoiceOps backend
(`/internal/api-tokens/introspect`, `/internal/sessions/introspect`, `/internal/sessions/exchange`,
`/internal/sessions/revoke`) with a shared secret.
Introspection results are cached (clamped to 30 s active, 5 s inactive). Only a fixed set of
request headers reaches MLflow (no cookies, Authorization, Origin, X-Forwarded-*, or client
`X-MLFLOW-WORKSPACE`); the gateway sets `Authorization` (upstream admin) and `X-MLFLOW-WORKSPACE`.
Set-Cookie, WWW-Authenticate and CORS headers from MLflow are dropped.

## Auth modes

- API token: `Authorization: Bearer iop_...`, introspected; needs `mlflow:read`, and
  `mlflow:write` for mutating endpoints. The token carries one organization (or group) context.
- Browser session: the gateway's own `__Host-mlflow_session` child token, introspected (the app's
  `invoiceops_session` cookie is never read). A request with more than one such cookie is
  unauthenticated.
- Service read token: see below.
- A malformed Authorization header never falls back to the cookie.

## Workspace mapping

One workspace per context, derived only from validated UUIDs: `org-<org uuid>` and
`grp-<group uuid>`. Workspaces are created on first authorized use. Session users with several
contexts pick one via `/_gateway/contexts` or `/?context=<workspace>`, stored in the HttpOnly
`mlflow_ctx` cookie; the value must belong to the session. `?context=` is honoured only for
same-site navigations (`Sec-Fetch-Site` same-origin/same-site/none, or, when absent, a Referer
from `GATEWAY_PUBLIC_ORIGIN` / `GATEWAY_APP_ORIGIN`); otherwise it is ignored.

## Endpoint policy

`policy.py` holds the allowlist (verified against MLflow 3.16.1): experiments, runs, metrics,
registry, logged models, artifacts, GraphQL and static UI files. Denied: users/permissions/roles,
workspace mutation, AI gateway, webhooks, scorers, datasets, jobs, presigned/multipart artifact
URLs. Paths with `..`, `.`, `//`, backslashes or control characters are rejected. Artifact paths
must live under `workspaces/<own workspace>/`. `workspaces` list/get is answered by the gateway.
The inspected query parameters `path` and `context` must not repeat (400): MLflow and the
gateway would otherwise pick different values.

## Model-version source restriction

`model-versions/create` accepts only: `runs:/<run_id>/<path>`, `models:/<name-or-m-id>/...`, and
`mlflow-artifacts:/workspaces/<own workspace>/...`. Netloc forms (`runs://`, `models://`,
`mlflow-artifacts://host`), other schemes, `..`, `?`, `#`, `%` and control characters are refused.

## Service read token

`GATEWAY_SERVICE_READ_TOKEN` lets the InvoiceOps backend read any workspace (read-only, never
write) by sending `X-InvoiceOps-Workspace: <org-/grp- name>`. Treat it as a high-value secret.

## Cookie, CSRF, CSP

- Session principals: non-safe methods require `Origin == GATEWAY_PUBLIC_ORIGIN` (the gateway's own origin).
- `__Host-mlflow_session` and `__Host-mlflow_oauth_state`: HttpOnly, SameSite=Lax, Secure, Path=/, no Domain.
- `mlflow_ctx`: HttpOnly, SameSite=Lax, Secure on https, host-only.
- UI HTML (MLflow shell and gateway pages) gets a strict CSP: `default-src 'none'; script-src 'self';
  style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:;
  connect-src 'self'; worker-src 'self' blob:; frame-src 'self' blob: data:; manifest-src 'self';
  object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'`. Derived from the
  3.16.1 bundle (one same-origin deferred script, emotion inline styles, same-origin workers). There
  is no `unsafe-eval`: one bundled library path uses `new Function` (expression predicates) and will
  not work. Re-check the CSP when upgrading MLflow.
- Artifacts and `get-artifact`: `sandbox; default-src 'none'` plus `nosniff`.

## Abuse bounds

- **Two HTTP clients.** InvoiceOps introspection and MLflow workspace provisioning use their own small
  client (50 connections, short timeouts); the MLflow data path has another (200 connections). A burst of
  slow uploads or downloads can saturate the second one but never delays authentication.
- **Per-principal concurrency.** A principal (token user, session user, service) may hold at most
  `GATEWAY_MAX_CONCURRENT_PER_PRINCIPAL` (default 16) in-flight upstream requests; more get `429` with
  `Retry-After`. The slot is released when the response ends, when the client disconnects mid-stream
  (the upstream response is closed too) or on any error.
- **Upload deadline.** A request body (artifact upload, model-version JSON) must finish within
  `GATEWAY_UPLOAD_DEADLINE_SECONDS` (default 3600, an absolute deadline, not an idle timeout): `408` after it.
- **Format prefilters.** A bearer value that is not shaped like an API token (`iop_` + 20-128 base64url
  characters), a session cookie that is not `mls_` + 20-128 base64url characters and a handoff code that is not
  20-128 base64url characters are refused (`401` / `400`) without asking InvoiceOps. This also covers
  `/_gateway/callback` and `/_gateway/logout`. `tests/test_credential_shapes.py` pins the three shapes to samples
  generated by the backend's own code, so a change there breaks this repository's tests.
- **Failed-auth throttle.** Only a lookup that really reaches InvoiceOps (a cache miss) can fail and count, and
  failures are counted per client address AND credential class (API token, session cookie), so 40 expired cookies
  from a school NAT cannot lock out a valid API token or a cached session. After `GATEWAY_AUTH_FAILURE_LIMIT`
  (default 120) failed lookups of one class within `GATEWAY_AUTH_FAILURE_WINDOW_SECONDS` (default 60) from one
  address, further uncached lookups of that class get `429` until the window slides. Requests answered from the
  cache or by a prefilter are never counted. The address is the TCP peer, or the value of the header named in
  `GATEWAY_CLIENT_IP_HEADER` (compose defaults to `cf-connecting-ip` when the variable is unset; set it to an empty
  value to use the TCP peer; use the header only when the proxy that writes it is the sole way in).
- **Handoff throttle.** Failed handoff-code exchanges (`/_gateway/callback`) and failed session revokes
  (`/_gateway/logout`, InvoiceOps unavailable) share a smaller budget per address: `GATEWAY_HANDOFF_FAILURE_LIMIT`
  (default 20) within the same window, then `429` and no further InvoiceOps calls. A person makes one of these per
  sign-in, so 20 per minute per address is generous.
- **Introspection single-flight.** Concurrent requests with the same credential share one InvoiceOps call; if
  the request that made it is cancelled, the others retry instead of failing.

## Workspaces are created by writes

The MLflow workspace of a context is created on the first authorized **write** (`POST`/`PUT`/`PATCH`/`DELETE`).
A read in a context whose workspace does not exist answers `404` from MLflow and creates nothing.

## Logging and audit

One JSON line per request: `request_id`, method, path (no query), status, duration, outcome `reason`,
principal kind and workspace. Never tokens, cookies, codes or the query string. Mutating requests add
`principal_hash`, a keyed one-way digest (HMAC-SHA-256, 16 hex characters) of the principal's subject: it
correlates actions of one user without revealing who. The key is `GATEWAY_AUDIT_HMAC_KEY` when set; otherwise it is
derived: `HMAC-SHA-256(GATEWAY_INTROSPECTION_SECRET, "invoiceops-mlflow-gateway/audit-key/v1")`, so the
introspection secret itself never keys the logs and a dedicated key survives its rotation. A `metrics` line with the request
count by outcome (`ok`, `unauthenticated`, `concurrency_limit`, ...) is logged every 100 requests. `httpx` and
`httpcore` are set to WARNING at start-up (they log URLs at INFO), and `tests/test_abuse.py` asserts that with
the root logger at INFO no logger of any library emits a secret or an HTTP client record.

## Configuration (environment)

Required: `GATEWAY_UPSTREAM_URL`, `GATEWAY_UPSTREAM_USERNAME`, `GATEWAY_UPSTREAM_PASSWORD`,
`GATEWAY_INTROSPECTION_URL`, `GATEWAY_INTROSPECTION_SECRET`, `GATEWAY_PUBLIC_ORIGIN` (this gateway,
`https://mlflow-invoiceops.aglai.dev`, must equal `MLFLOW_PUBLIC_URL` of the app) and
`GATEWAY_APP_ORIGIN` (the app, `https://invoiceops.aglai.dev`; compose maps it from `APP_PUBLIC_URL`).
Both are normalized (lowercase scheme and host, default port dropped, like the app's `new URL().origin`) and
must be bare origins: userinfo, path, query and fragment are refused. Plain `http` origins refuse to start
unless `GATEWAY_ALLOW_INSECURE_HTTP=1` (local development and tests only).
Optional: `GATEWAY_SERVICE_READ_TOKEN`, `GATEWAY_CONTEXT_COOKIE_MAX_AGE`,
`GATEWAY_CACHE_ACTIVE_SECONDS`, `GATEWAY_CACHE_INACTIVE_SECONDS`, `GATEWAY_MAX_BODY_BYTES`,
`GATEWAY_MAX_ARTIFACT_BYTES`, `GATEWAY_MAX_CONCURRENT_PER_PRINCIPAL`, `GATEWAY_UPLOAD_DEADLINE_SECONDS`,
`GATEWAY_AUTH_FAILURE_LIMIT`, `GATEWAY_AUTH_FAILURE_WINDOW_SECONDS`, `GATEWAY_HANDOFF_FAILURE_LIMIT`,
`GATEWAY_CLIENT_IP_HEADER`, `GATEWAY_AUDIT_HMAC_KEY`. `compose.gateway.yml` forwards every one of them with its
default (`tests/test_compose_and_docs_contracts.py` fails if one is missing). Secrets are 32-512 chars, no whitespace, no placeholder text.

## Residual risks

- Sibling subdomains of `aglai.dev` cannot overwrite the `__Host-` cookies, but can still send
  requests to the gateway; the Origin check and the same-site context switch bound the impact.
- `Sec-Fetch-Site: same-site` includes sibling subdomains; context switching is low impact but not
  cross-origin proof.
- Revoked sessions/tokens stay valid for up to the cache TTL (<= 30 s).
- Model-version sources are validated syntactically; MLflow does not verify that a `runs:/` run
  belongs to the workspace, so consumers resolving sources natively must stay workspace-scoped.
- Duplicate JSON keys in request bodies are parsed by the gateway and MLflow independently.
- A user with write scope can store arbitrary files in their own workspace (served sandboxed).
- MLflow's own UI features that need `unsafe-eval` are unsupported under the CSP.
