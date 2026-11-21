"""The old local runbook, its compose overlay and env template are retired.

The local end-to-end flow lives in ``invoiceops-stack`` (``LOCAL_E2E.md`` + ``compose.local.yml``),
which goes through the gateway. These tests fail if the retired files come back or if any
documentation, script or compose file of this repository mentions a removed command, route or
variable again.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

RETIRED_FILES = (
    "LOCAL_INVOICEOPS_RUNBOOK.md",
    "compose.invoiceops-local.yml",
    "invoiceops-local.env.example",
)
REMOVED_NAMES = (
    "invoiceops-train-candidate",
    "invoiceops-quality-gate",
    "invoice-risk-gate-v1",
    "invoiceops-promote-local",
    "HGB_RUN_IDS",
    "MODEL_API_ADMIN_TOKEN",
    "LOCAL_MODEL_API_ADMIN_TOKEN",
    "LOCAL_MODEL_API_PORT",
    "model_release_smoke",
    "/models/invoice-review",
    "proxy-http",
    "MODEL_API_ARTIFACT_DOWNLOAD_MODE",
    "invoiceops-local-link",
    "compose.invoiceops-local.yml",
    "LOCAL_INVOICEOPS_RUNBOOK",
    "LOCAL_INFRASTRUCTURE_RUNBOOK",
)
SCANNED_SUFFIXES = {".md", ".sh", ".yml", ".yaml", ".py", ".example", ".toml"}
SKIPPED_DIRS = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__", ".codegraph"}


def scanned_files():
    for path in ROOT.rglob("*"):
        if (
            path.is_file()
            and path.suffix in SCANNED_SUFFIXES
            and not SKIPPED_DIRS & set(path.relative_to(ROOT).parts)
            and path.name != "test_local_runbook.py"
            and not path.name.startswith(".env")
        ):
            yield path


class RetiredLocalRunbookTests(unittest.TestCase):
    def test_retired_files_stay_deleted(self):
        for name in RETIRED_FILES:
            self.assertFalse((ROOT / name).exists(), f"{name} was retired; use invoiceops-stack")

    def test_no_file_mentions_a_removed_command_route_or_variable(self):
        offenders = []
        for path in scanned_files():
            text = path.read_text(encoding="utf-8", errors="ignore")
            offenders += [
                f"{path.relative_to(ROOT)}: {name}" for name in REMOVED_NAMES if name in text
            ]
        self.assertEqual(offenders, [])

    def test_readme_points_to_the_stack_end_to_end_guide(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("invoiceops-stack/LOCAL_E2E.md", readme)
        self.assertFalse(re.search(r"\]\([^)]*LOCAL_INFRASTRUCTURE", readme))

    def test_local_flow_documentation_states_the_gateway_is_never_bypassed(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("gateway", readme.split("LOCAL_E2E.md", 1)[1][:600].lower())


if __name__ == "__main__":
    unittest.main()
