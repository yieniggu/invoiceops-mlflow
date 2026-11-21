"""Compose contracts: no fixed credentials, stable volume names, hardened gateway, placeholders."""

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

from test_secret_argv_contract import ShellSandbox, phases

ROOT = Path(__file__).resolve().parents[1]
HAS_DOCKER = shutil.which("docker") is not None and (
    subprocess.run(["docker", "compose", "version"], capture_output=True, check=False).returncode
    == 0
)
DUMMY_ENV = {
    "MLFLOW_AUTH_ADMIN_USERNAME": "ci-admin",
    "MLFLOW_AUTH_ADMIN_PASSWORD": "ci-admin-password-0123456789abcdef",
    "MLFLOW_FLASK_SERVER_SECRET_KEY": "ci-flask-secret-0123456789abcdef",
    "MLFLOW_DB_PASSWORD": "ci-db-password",
    "MINIO_ROOT_USER": "ci-minio-user",
    "MINIO_ROOT_PASSWORD": "ci-minio-password",
    "INTERNAL_INTROSPECTION_SECRET": "ci-introspection-secret-0123456789abcdef",
    "GATEWAY_PUBLIC_ORIGIN": "https://mlflow.example.test",
    "APP_PUBLIC_URL": "https://app.example.test",
}


def compose_config(project, files, env=None):
    command = ["docker", "compose", "-p", project]
    for name in files:
        command += ["-f", str(ROOT / name)]
    command += ["config", "--format", "json"]
    result = subprocess.run(
        command,
        cwd=ROOT,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/"), **(env or DUMMY_ENV)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


class NoFixedCredentialsTests(unittest.TestCase):
    def test_compose_files_hold_no_fixed_passwords(self):
        for name in ("compose.yml", "compose.gateway.yml"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("-local-password", text, name)
            self.assertNotIn("MINIO_ROOT_PASSWORD: minio", text, name)

    def test_base_compose_requires_every_credential_from_the_environment(self):
        text = (ROOT / "compose.yml").read_text(encoding="utf-8")
        for variable in ("MLFLOW_DB_PASSWORD", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"):
            self.assertIn("${%s:?" % variable, text)

    def test_example_file_is_not_a_dotenv_and_supplies_throwaway_values(self):
        example = ROOT / "mlflow-local.example.env"
        self.assertFalse(example.name.startswith(".env"))
        values = dict(
            line.split("=", 1)
            for line in example.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        )
        self.assertEqual(set(values), {"MLFLOW_DB_PASSWORD", "MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"})

    @unittest.skipUnless(HAS_DOCKER, "docker compose needed")
    def test_base_compose_without_credentials_fails_before_anything_starts(self):
        result = subprocess.run(
            ["docker", "compose", "-f", "compose.yml", "config", "-q"],
            cwd=ROOT,
            env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must be set", result.stderr)


@unittest.skipUnless(HAS_DOCKER, "docker compose needed")
class VolumeNameTests(unittest.TestCase):
    def names(self, project):
        config = compose_config(project, ["compose.yml"])
        return {key: value["name"] for key, value in config["volumes"].items()}

    def test_stack_project_resolves_to_the_names_the_stack_expects(self):
        # invoiceops-stack/tests/test_stack_config.py asserts exactly these two names.
        self.assertEqual(
            self.names("invoiceops"),
            {
                "mlflow_postgres_data": "invoiceops_mlflow_postgres_data",
                "mlflow_minio_data": "invoiceops_mlflow_minio_data",
            },
        )

    def test_standalone_project_names_are_prefixed_by_its_project(self):
        self.assertEqual(
            self.names("invoiceops-mlflow"),
            {
                "mlflow_postgres_data": "invoiceops-mlflow_mlflow_postgres_data",
                "mlflow_minio_data": "invoiceops-mlflow_mlflow_minio_data",
            },
        )

    def test_the_app_volume_name_can_never_collide(self):
        for project in ("invoiceops", "invoiceops-mlflow"):
            self.assertNotIn("postgres_data", {n.removeprefix(project + "_") for n in self.names(project).values()})

    def test_migration_script_copies_between_the_old_and_new_names(self):
        script = (ROOT / "scripts" / "migrate-standalone-volumes.sh").read_text(encoding="utf-8")
        self.assertIn('"${src}_postgres_data" "${dst}_mlflow_postgres_data"', script)
        self.assertIn('"${src}_minio_data" "${dst}_mlflow_minio_data"', script)
        self.assertIn("refusing to overwrite", script)
        self.assertNotIn("volume rm", script)  # the source is never deleted


@unittest.skipUnless(HAS_DOCKER, "docker compose needed")
class GatewayContainerHardeningTests(unittest.TestCase):
    def test_gateway_service_is_locked_down(self):
        config = compose_config(
            "hardening", ["compose.yml", "compose.gateway.yml", "compose.networks.yml"]
        )
        gateway = config["services"]["gateway"]
        self.assertEqual(gateway["cap_drop"], ["ALL"])
        self.assertIn("no-new-privileges:true", gateway["security_opt"])
        self.assertTrue(gateway["read_only"])
        self.assertTrue(any(str(t).startswith("/tmp") for t in gateway["tmpfs"]))
        self.assertGreater(int(gateway["pids_limit"]), 0)
        self.assertGreater(int(gateway["mem_limit"]), 0)
        self.assertNotIn("ports", gateway)


class ClientIpHeaderTests(unittest.TestCase):
    FILES = ["compose.yml", "compose.gateway.yml", "compose.networks.yml"]

    def _header(self, extra):
        config = compose_config("clientip", self.FILES, {**DUMMY_ENV, **extra})
        return config["services"]["gateway"]["environment"]["GATEWAY_CLIENT_IP_HEADER"]

    @unittest.skipUnless(HAS_DOCKER, "docker compose is not available")
    def test_unset_defaults_to_cloudflare_header(self):
        self.assertEqual(self._header({}), "cf-connecting-ip")

    @unittest.skipUnless(HAS_DOCKER, "docker compose is not available")
    def test_set_but_empty_selects_the_tcp_peer(self):
        self.assertEqual(self._header({"GATEWAY_CLIENT_IP_HEADER": ""}), "")

    @unittest.skipUnless(HAS_DOCKER, "docker compose is not available")
    def test_explicit_value_is_forwarded(self):
        self.assertEqual(self._header({"GATEWAY_CLIENT_IP_HEADER": "x-real-ip"}), "x-real-ip")


class PlaceholderTests(unittest.TestCase):
    PLACEHOLDERS = (
        ("MINIO_ROOT_PASSWORD", "replace-with-generated-minio-password"),
        ("MINIO_ROOT_USER", "replace-with-generated-minio-user"),
        ("MINIO_ROOT_PASSWORD", "REPLACE_WITH_URL_SAFE_RANDOM_VALUE"),
    )

    def test_minio_init_rejects_example_placeholders_before_touching_mc(self):
        for key, value in self.PLACEHOLDERS:
            with self.subTest(key=key, value=value), ShellSandbox() as sandbox:
                result, events = sandbox.run(minio=True, overrides={key: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(phases(events), [], "mc ran with a placeholder credential")
                self.assertIn(b"example placeholder", result.stderr)

    def test_mlflow_entrypoint_rejects_placeholder_database_and_s3_credentials(self):
        uri = "postgresql+psycopg2://mlflow:replace-with-generated-db-password@postgres:5432/mlflow"
        cases = (
            {"MLFLOW_BACKEND_STORE_URI": uri},
            {"MLFLOW_AUTH_DATABASE_URI": uri},
            {"AWS_SECRET_ACCESS_KEY": "replace-with-generated-minio-password"},
            {"AWS_ACCESS_KEY_ID": "replace-with-generated-minio-user"},
        )
        for overrides in cases:
            with self.subTest(overrides=list(overrides)), ShellSandbox() as sandbox:
                result, events = sandbox.run(overrides=overrides)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(events, [], "an external tool ran before the guard")
                self.assertFalse(sandbox.config.exists())
                self.assertIn(b"example placeholder", result.stderr)

    def test_real_looking_values_still_start(self):
        with ShellSandbox() as sandbox:
            result, _ = sandbox.run(overrides={"AWS_SECRET_ACCESS_KEY": "s3cret-not-a-placeholder"})
            self.assertEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
