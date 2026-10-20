"""Offline policy audit, not a Docker matcher or proof of a packed context.

The canonical rules deny everything, reopen the docker parent for traversal,
deny its descendants again, then reopen only three literal source files.
Docker sends build metadata specially; this audit does not measure that transfer.
"""

from pathlib import Path
import shlex
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCES = frozenset({
    "docker/mlflow-entrypoint.sh",
    "docker/mlflow-healthcheck.py",
    "docker/mlflow-migrate.py",
})
METADATA = frozenset({"Dockerfile.mlflow", ".dockerignore"})
CANONICAL_RULES = [
    "**",
    "!Dockerfile.mlflow",
    "!.dockerignore",
    "!docker/",
    "docker/**",
    "!docker/mlflow-entrypoint.sh",
    "!docker/mlflow-healthcheck.py",
    "!docker/mlflow-migrate.py",
]


class DockerContextContractTests(unittest.TestCase):
    def setUp(self):
        self.rules = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

    def test_canonical_deny_reopen_deny_allow_order(self):
        # Exact syntax and ordering are intentional: no general matcher is used.
        self.assertEqual(self.rules, CANONICAL_RULES)

    def test_literal_file_exceptions_are_exactly_the_required_allowlist(self):
        self.assertEqual(self.rules, CANONICAL_RULES)
        exceptions = {
            rule[1:] for rule in self.rules
            if rule.startswith("!") and not rule.endswith("/")
        }
        self.assertEqual(exceptions, SOURCES | METADATA)
        # Synthetic names only: never read, enumerate, or hash environment files.
        for candidate in (
            ".env", ".env.local", ".env.production", ".git/config",
            "data/private.csv", "mlruns/secret", "artifacts/model.pkl",
            "__pycache__/module.pyc", ".cache/pip/token", "README.md",
            "compose.yml", "tests/test_docker_context_contract.py",
            "docker/.env", "docker/.env.local", "docker/private.key",
            "docker/future-secret.txt", "docker/nested/private.key",
            "docker/nested/mlflow-entrypoint.sh",
            "docker/__pycache__/mlflow-migrate.cpython-312.pyc",
            "docker/mlflow-entrypoint.sh.bak",
        ):
            with self.subTest(candidate=candidate):
                self.assertNotIn(candidate, exceptions)

    def test_dockerfile_copy_sources_match_allowlist(self):
        sources = []
        dockerfile = (ROOT / "Dockerfile.mlflow").read_text(encoding="utf-8")
        # Join the existing backslash continuations before parsing instructions.
        for line in dockerfile.replace("\\\n", " ").splitlines():
            tokens = shlex.split(line, comments=True)
            if not tokens:
                continue
            self.assertNotEqual(tokens[0].upper(), "ADD", "ADD requires policy review")
            if tokens[0].upper() == "COPY":
                # Fail closed on flags, JSON, or multiple sources.
                self.assertEqual(len(tokens), 3, "COPY syntax requires policy review")
                self.assertIn(tokens[1], SOURCES)
                self.assertTrue(tokens[2].startswith("/"))
                sources.append(tokens[1])
        self.assertEqual(len(sources), 3)
        self.assertEqual(set(sources), SOURCES)

    def test_allowed_paths_are_regular_files_without_symlink_parents(self):
        for relative in sorted(SOURCES | METADATA):
            with self.subTest(path=relative):
                path = ROOT / relative
                self.assertTrue(path.is_file())
                self.assertFalse(path.is_symlink())
                self.assertFalse(path.parent.is_symlink())

    def test_no_dockerfile_specific_ignore_override(self):
        self.assertFalse(
            (ROOT / "Dockerfile.mlflow.dockerignore").exists(),
            "A Dockerfile-specific ignore file would override the root policy",
        )


if __name__ == "__main__":
    unittest.main()
