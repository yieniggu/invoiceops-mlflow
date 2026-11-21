"""The gateway's format prefilters against what the InvoiceOps backend really generates.

``fixtures/backend_credential_shapes.json`` holds random samples produced by the backend's
generators (``generateTokenSecret``, the MLflow handoff code and session token). When the backend
checkout is next to this repository the constants are cross-checked against its source too.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

import pytest

from invoiceops_mlflow_gateway.identity import (
    CODE_FORMAT_RE,
    SESSION_FORMAT_RE,
    TOKEN_FORMAT_RE,
    code_format_ok,
    session_format_ok,
    token_format_ok,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "backend_credential_shapes.json").read_text()
)
BACKEND_SRC = Path(__file__).resolve().parents[3] / "invoiceops-app" / "apps" / "backend" / "src"
ENCODED_LENGTH = math.ceil(
    FIXTURE["source_constants"]["random_bytes"] * 8 / 6
)  # base64url, no padding


@pytest.mark.parametrize("value", FIXTURE["api_tokens"])
def test_every_generated_api_token_passes_the_token_prefilter(value: str) -> None:
    assert value.startswith(FIXTURE["source_constants"]["token_prefix"])
    assert len(value) == 4 + ENCODED_LENGTH
    assert token_format_ok(value)
    assert not session_format_ok(value)


@pytest.mark.parametrize("value", FIXTURE["session_tokens"])
def test_every_generated_session_token_passes_the_session_prefilter(value: str) -> None:
    assert value.startswith(FIXTURE["source_constants"]["session_prefix"])
    assert len(value) == 4 + ENCODED_LENGTH
    assert session_format_ok(value)
    assert not token_format_ok(value)


@pytest.mark.parametrize("value", FIXTURE["handoff_codes"])
def test_every_generated_handoff_code_passes_the_code_prefilter(value: str) -> None:
    assert len(value) == ENCODED_LENGTH
    assert code_format_ok(value)


def test_the_regex_bounds_contain_the_exact_generated_shape() -> None:
    assert "{20,128}" in TOKEN_FORMAT_RE.pattern
    assert 20 <= ENCODED_LENGTH <= 128
    assert SESSION_FORMAT_RE.pattern.startswith("^mls_") and CODE_FORMAT_RE.pattern.startswith("^[")


@pytest.mark.skipif(not BACKEND_SRC.exists(), reason="the backend checkout is not available")
def test_the_fixture_constants_still_match_the_backend_source() -> None:
    consts = FIXTURE["source_constants"]
    secret = (BACKEND_SRC / "features/api-tokens/secret.ts").read_text()
    sso = (BACKEND_SRC / "features/mlflow-sso/service.ts").read_text()
    assert f'TOKEN_SECRET_PREFIX = "{consts["token_prefix"]}"' in secret
    assert f"SECRET_BYTES = {consts['random_bytes']}" in secret
    assert f'MLFLOW_SESSION_PREFIX = "{consts["session_prefix"]}"' in sso
    assert f"RANDOM_BYTES = {consts['random_bytes']}" in sso
    assert 'toString("base64url")' in secret and 'toString("base64url")' in sso


def test_a_prefix_drift_in_the_backend_would_be_caught_by_the_fixture_check() -> None:
    # The regexes accept the CURRENT prefixes only: a backend that changed one would fail above.
    assert not token_format_ok("tok_" + "A" * 43)
    assert not session_format_ok("ses_" + "A" * 43)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", FIXTURE["api_tokens"][0][4:])
