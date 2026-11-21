"""scripts/gateway-matrix.sh against the real gateway + MLflow: it must pass and clean up."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest
from conftest import UPSTREAM_PASSWORD, WS_A

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "gateway-matrix.sh"


@pytest.mark.skipif(
    not (shutil.which("curl") and shutil.which("python3") and SCRIPT.exists()),
    reason="curl, python3 and the repository scripts are needed",
)
def test_matrix_passes_against_the_real_gateway(
    gateway: str, tokens: dict[str, str], mlflow_server: str
) -> None:
    env = {
        "PATH": os.environ["PATH"],
        "GATEWAY_URL": gateway,
        "TOKEN_A": tokens["a_write"],
        "TOKEN_A_READ": tokens["a_read"],
        "TOKEN_B": tokens["b_write"],
    }
    result = subprocess.run(
        ["/bin/sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=180, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "FAIL" not in result.stdout
    assert "cleanup: artifact" in result.stdout
    # Nothing of the matrix is left visible in the owner's workspace.
    leftovers = httpx.post(
        f"{mlflow_server}/api/2.0/mlflow/experiments/search",
        json={"max_results": 1000},
        headers={"X-MLFLOW-WORKSPACE": WS_A},
        auth=("gateway-admin", UPSTREAM_PASSWORD),
    ).json()
    assert not [e for e in leftovers.get("experiments", []) if e["name"].startswith("matrix-")]
