import unittest
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from test_secret_argv_contract import (
    ShellSandbox,
    assert_no_secret_argv,
    assert_sanitized,
    phases,
)


ROOT = Path(__file__).resolve().parents[1]
DATABASE_URI_KEYS = ("MLFLOW_AUTH_DATABASE_URI", "MLFLOW_BACKEND_STORE_URI")


class ComposeLoader(yaml.SafeLoader):
    pass


ComposeLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node))


def mlflow_environment(filename):
    with (ROOT / filename).open(encoding="utf-8") as compose_file:
        return yaml.load(compose_file, Loader=ComposeLoader)["services"]["mlflow"]["environment"]


class PostgreSQLDriverContractTests(unittest.TestCase):
    def test_entrypoint_serializes_both_database_migrations_before_workers(self):
        with ShellSandbox() as sandbox:
            result, events = sandbox.run()
            self.assertEqual(result.returncode, 0)
            self.assertEqual(phases(events), ["tracking", "auth", "server-sentinel"])
            self.assertTrue(sandbox.config.exists())
            assert_sanitized(self, sandbox, result)
            assert_no_secret_argv(self, sandbox, events)

    def assert_database_uris_use_installed_driver(self, environment, expected_password):
        self.assertEqual(set(DATABASE_URI_KEYS) & environment.keys(), set(DATABASE_URI_KEYS))
        for key in DATABASE_URI_KEYS:
            with self.subTest(uri=key):
                self.assertIn(f":{expected_password}@", environment[key])
                parsed = urlsplit(environment[key].replace(expected_password, "synthetic-password"))
                self.assertEqual(parsed.scheme, "postgresql+psycopg2")
                self.assertEqual(parsed.username, "mlflow")
                self.assertEqual(parsed.password, "synthetic-password")
                self.assertEqual(parsed.hostname, "postgres")
                self.assertEqual(parsed.port, 5432)
                self.assertEqual(parsed.path, "/mlflow")

    def test_base_compose_uses_explicit_postgresql_driver(self):
        # The password comes from the environment (no fixed credential in compose.yml).
        password = "${MLFLOW_DB_PASSWORD:?MLFLOW_DB_PASSWORD must be set (see mlflow-local.example.env)}"
        self.assert_database_uris_use_installed_driver(mlflow_environment("compose.yml"), password)

    def test_production_overlay_uses_explicit_postgresql_driver(self):
        password = "${MLFLOW_DB_PASSWORD:?MLFLOW_DB_PASSWORD must be set in .env}"
        overlay = mlflow_environment("compose.gateway.yml")
        self.assert_database_uris_use_installed_driver(overlay, password)


if __name__ == "__main__":
    unittest.main()
