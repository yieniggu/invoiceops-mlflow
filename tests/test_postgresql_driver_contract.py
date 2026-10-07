import unittest
from pathlib import Path
from urllib.parse import urlsplit

import yaml


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
        entrypoint = (ROOT / "docker/mlflow-entrypoint.sh").read_text(encoding="utf-8")
        backend = 'mlflow db upgrade "$MLFLOW_BACKEND_STORE_URI"'
        auth = 'python -m mlflow.server.auth db upgrade --url "$MLFLOW_AUTH_DATABASE_URI"'
        self.assertIn(backend, entrypoint)
        self.assertIn(auth, entrypoint)
        self.assertLess(entrypoint.index(backend), entrypoint.index(auth))
        self.assertLess(entrypoint.index(auth), entrypoint.index('exec "$@"'))

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
        self.assert_database_uris_use_installed_driver(
            mlflow_environment("compose.yml"), "mlflow-local-password"
        )

    def test_local_overlay_uses_explicit_postgresql_driver(self):
        local_password = "${LOCAL_MLFLOW_DB_PASSWORD:?Set the local MLflow database password}"
        overlay = mlflow_environment("compose.invoiceops-local.yml")
        self.assert_database_uris_use_installed_driver(overlay, local_password)
        merged = mlflow_environment("compose.yml") | overlay
        self.assert_database_uris_use_installed_driver(merged, local_password)


if __name__ == "__main__":
    unittest.main()
