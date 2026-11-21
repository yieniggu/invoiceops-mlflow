"""Allowlist of MLflow 3.16.1 endpoints the gateway forwards.

Anything not listed here is denied: administration (users, permissions, roles, workspace
mutation), the AI gateway and its secrets, webhooks, scorers, datasets, traces, jobs,
prompt optimisation, presigned and multipart artifact URLs (they would leak the internal
object-store address). Verified against ``mlflow.server.handlers.get_endpoints()`` of 3.16.1.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Access = Literal["read", "write"]
Kind = Literal[
    "proxy",
    "workspace_list",
    "workspace_get",
    "artifact",
    "model_version_create",
    "graphql",
    "static",
]

READ_METHODS = frozenset({"GET", "HEAD"})
SAFE_METHODS = READ_METHODS

_ID = r"[A-Za-z0-9_-]{1,128}"
_API_PREFIX = re.compile(r"^/(?:ajax-api|api)/(?:2\.0|3\.0)/mlflow/(?P<rel>.+)$")
_ARTIFACT_PREFIX = re.compile(
    r"^/(?:ajax-api|api)/2\.0/mlflow-artifacts/artifacts(?:/(?P<ap>.*))?$"
)
_WORKSPACE_NAME = re.compile(r"^workspaces/(?P<name>[a-z0-9-]+)$")


@dataclass(frozen=True)
class Rule:
    methods: frozenset[str]
    pattern: re.Pattern[str]
    access: Access
    kind: Kind = "proxy"


def _rules(access: Access, methods: str, paths: list[str], kind: Kind = "proxy") -> list[Rule]:
    verbs = frozenset(methods.split("|"))
    return [Rule(verbs, re.compile(f"^{p}$"), access, kind) for p in paths]


_REL_RULES: list[Rule] = [
    # Experiments and runs.
    *_rules("read", "GET", ["experiments/get", "experiments/get-by-name", "experiments/search"]),
    *_rules("read", "POST", ["experiments/search", "runs/search"]),
    *_rules("read", "GET", ["runs/get", "artifacts/list"]),
    *_rules("read", "GET", ["metrics/get-history", "metrics/get-history-bulk-interval"]),
    *_rules(
        "write",
        "POST",
        [
            "experiments/create",
            "experiments/update",
            "experiments/delete",
            "experiments/restore",
            "experiments/set-experiment-tag",
            "experiments/delete-experiment-tag",
            "runs/create",
            "runs/update",
            "runs/delete",
            "runs/restore",
            "runs/log-batch",
            "runs/log-metric",
            "runs/log-parameter",
            "runs/log-inputs",
            "runs/log-model",
            "runs/outputs",
            "runs/set-tag",
            "runs/delete-tag",
        ],
    ),
    # Model registry.
    *_rules(
        "read",
        "GET",
        [
            "registered-models/get",
            "registered-models/search",
            "registered-models/get-latest-versions",
            "registered-models/alias",
            "model-versions/get",
            "model-versions/search",
            "model-versions/get-download-uri",
        ],
    ),
    *_rules("read", "POST", ["registered-models/get-latest-versions"]),
    *_rules(
        "write",
        "POST",
        [
            "registered-models/create",
            "registered-models/rename",
            "registered-models/set-tag",
            "registered-models/alias",
            "model-versions/set-tag",
            "model-versions/transition-stage",
        ],
    ),
    *_rules("write", "PATCH", ["registered-models/update", "model-versions/update"]),
    *_rules(
        "write",
        "DELETE",
        [
            "registered-models/delete",
            "registered-models/delete-tag",
            "registered-models/alias",
            "model-versions/delete",
            "model-versions/delete-tag",
        ],
    ),
    *_rules("write", "POST", ["model-versions/create"], "model_version_create"),
    # Logged models (MLflow 3).
    *_rules("read", "GET", [f"logged-models/{_ID}", f"logged-models/{_ID}/artifacts/directories"]),
    *_rules("read", "POST", ["logged-models/search"]),
    *_rules("write", "POST", ["logged-models", f"logged-models/{_ID}/params"]),
    *_rules("write", "PATCH", [f"logged-models/{_ID}", f"logged-models/{_ID}/tags"]),
    *_rules("write", "DELETE", [f"logged-models/{_ID}", f"logged-models/{_ID}/tags/[^/]{{1,250}}"]),
    # Server capabilities used by the client and the UI.
    *_rules("read", "GET", ["server-info"]),
    # Answered by the gateway itself, never forwarded: a caller only ever sees its workspace.
    *_rules("read", "GET", ["workspaces"], "workspace_list"),
    *_rules("read", "GET", ["workspaces/[a-z0-9-]{1,63}"], "workspace_get"),
]

# Non-/api routes of the MLflow Flask app.
_ROOT_RULES: list[Rule] = [
    *_rules("read", "GET|HEAD", ["/get-artifact", "/model-versions/get-artifact"]),
    # UI shell and its static assets (read-only, no data).
    *_rules("read", "GET|HEAD", ["/", "/favicon.ico", "/static-files/.+"], "static"),
    # The UI queries runs/experiments through GraphQL; the 3.16.1 schema only exposes reads
    # (mlflowSearchRuns/mlflowSearchDatasets are "mutation" fields that only search).
    *_rules("read", "POST", ["/graphql"], "graphql"),
]


@dataclass(frozen=True)
class Decision:
    access: Access
    kind: Kind
    artifact_path: str | None = None


def is_safe_path(path: str) -> bool:
    """Reject anything a proxy and MLflow could parse differently."""
    if not path.startswith("/") or "\\" in path or "//" in path:
        return False
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in path):
        return False
    return all(segment not in (".", "..") for segment in path.split("/"))


def classify(method: str, path: str) -> Decision | None:
    """Return the decision for an allowed endpoint, ``None`` when it must be denied."""
    method = method.upper()
    if (artifact := _ARTIFACT_PREFIX.match(path)) is not None:
        ap = artifact.group("ap") or ""
        if method in READ_METHODS:
            return Decision("read", "artifact", ap)
        if method in ("PUT", "DELETE") and ap:
            return Decision("write", "artifact", ap)
        return None
    if (api := _API_PREFIX.match(path)) is not None:
        rel = api.group("rel")
        for rule in _REL_RULES:
            if method in rule.methods and rule.pattern.match(rel):
                return Decision(rule.access, rule.kind)
        return None
    for rule in _ROOT_RULES:
        if method in rule.methods and rule.pattern.match(path):
            return Decision(rule.access, rule.kind)
    return None


def artifact_path_allowed(artifact_path: str, workspace: str) -> bool:
    """Artifact paths must live under ``workspaces/<own workspace>/`` (or be the empty root)."""
    if artifact_path == "":
        return True
    segments = artifact_path.split("/")
    if any(s in ("", ".", "..") for s in segments):
        return False
    return segments[0] == "workspaces" and len(segments) >= 2 and segments[1] == workspace


def query_artifact_path_allowed(path_param: str | None, workspace: str) -> bool:
    """The ``path`` query of a directory listing: empty, the workspace root or below it."""
    if not path_param:
        return True
    match = _WORKSPACE_NAME.match(path_param)
    if match is not None:
        return match.group("name") == workspace
    return artifact_path_allowed(path_param.rstrip("/"), workspace)


# ``runs:/<run_id>/<path>`` and ``models:/<name-or-m-id>/...``: exactly one slash after the
# scheme. The ``scheme://host/...`` (netloc) forms are rejected because MLflow's URI parsing
# and ours would disagree about what they point at.
_SOURCE_SCHEMES = re.compile(r"^(?:runs|models):/(?!/)[^\s/?#%]+(?:/[^\s?#%]*)?$")
_ARTIFACTS_SOURCE_PREFIX = "mlflow-artifacts:/workspaces/"


def model_version_source_allowed(source: object, workspace: str) -> bool:
    """A model version may only point at its own workspace's artifacts or at runs/models.

    MLflow 3.16.1 accepts any ``source`` string and later serves the artifacts it points to
    with its own (admin) credentials, so a registration pointing at ``s3://other-bucket`` or at
    another workspace's artifact prefix would be a confused-deputy pointer for any consumer
    that resolves the source natively (for example the Model API).
    """
    if not isinstance(source, str) or not source or len(source) > 2000:
        return False
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in source):
        return False
    if _SOURCE_SCHEMES.match(source):
        return ".." not in source.split("/")
    if source.startswith(_ARTIFACTS_SOURCE_PREFIX):
        if any(c in source for c in "?#%\\"):
            return False
        return artifact_path_allowed(source[len("mlflow-artifacts:/") :].rstrip("/"), workspace)
    return False
