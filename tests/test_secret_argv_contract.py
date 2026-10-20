"""Offline shell contracts, not evidence of real MLflow/MinIO compatibility.

Execute the production entrypoint, Python migration helper, and narrow Compose
folded command. Only MLflow dependency callbacks and external tools are synthetic.
No installed client is invoked. The callbacks model the pinned 3.16.1 signatures;
the JSON import boundary models pinned mc's schema, not real S3 compatibility.
The Compose extraction parses YAML but is not a Compose validator: it supports
only the current single folded command and renders its escaped dollars, without
loading environment files, resolving overlays, or general interpolation.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from urllib.parse import quote

import yaml


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "docker/mlflow-entrypoint.sh"

# These stand-ins record their own actual argv, not an expected command assembled
# by the parent. Utility stand-ins keep PATH closed to every real external tool.
BOUNDARY = r'''
import json
import os
from pathlib import Path
import runpy
import sys
import types

tool = Path(sys.argv[0]).name
args = sys.argv[1:]
event = {"tool": tool, "argv": list(sys.argv)}
if tool in ("mlflow", "python"):
    event["database_environment"] = {
        key: os.environ[key] for key in
        ("MLFLOW_BACKEND_STORE_URI", "MLFLOW_AUTH_DATABASE_URI")
    }
def record(event):
    fd = os.open(os.environ["RECORDER"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(event) + "\n")

record(event)

phase = tool
if tool == "mlflow":
    sys.exit(92)  # A CLI migration regression must never look like a callback.
elif tool == "python":
    if len(args) != 2 or Path(args[0]).name != "mlflow-migrate.py":
        sys.exit(92)
    phase = args[1]
    if phase not in ("tracking", "auth"):
        sys.exit(92)
    def callback(*, url, revision=None):
        record({"tool": phase, "argv": list(sys.argv), "url": url,
                "revision": revision, "database_environment": event["database_environment"]})
        key = "MLFLOW_BACKEND_STORE_URI" if phase == "tracking" else "MLFLOW_AUTH_DATABASE_URI"
        if url != os.environ[key] or revision != ("head" if phase == "auth" else None):
            raise RuntimeError(os.environ["FAIL_CANARY"])
        if os.environ.get("FAIL_PHASE") == "systemexit-" + phase:
            raise SystemExit(os.environ["FAIL_CANARY"])
        if os.environ.get("FAIL_PHASE") == phase:
            print(os.environ["FAIL_CANARY"])
            print(os.environ["FAIL_CANARY"], file=sys.stderr)
            os.write(1, os.environ["FAIL_CANARY"].encode())
            os.write(2, os.environ["FAIL_CANARY"].encode())
            raise RuntimeError(os.environ["FAIL_CANARY"])
    modules = ("mlflow", "mlflow.db", "mlflow.server", "mlflow.server.auth",
               "mlflow.server.auth.db", "mlflow.server.auth.db.cli")
    for name in modules:
        module = types.ModuleType(name)
        module.__path__ = []
        module.upgrade = types.SimpleNamespace(callback=callback)
        sys.modules[name] = module
    if os.environ.get("FAIL_PHASE") == "import-" + phase:
        import builtins
        original_import = builtins.__import__
        def fail_import(name, *positional, **keywords):
            if name.startswith("mlflow"):
                os.write(1, os.environ["FAIL_CANARY"].encode())
                os.write(2, os.environ["FAIL_CANARY"].encode())
                raise ImportError(os.environ["FAIL_CANARY"])
            return original_import(name, *positional, **keywords)
        builtins.__import__ = fail_import
    sys.argv = args
    runpy.run_path(args[0], run_name="__main__")
    sys.exit(93)  # The actual helper must terminate, not fall through.
elif tool == "mc":
    if args == ["alias", "import", "local"]:
        payload = json.load(sys.stdin)
        if payload != {"url": "http://minio:9000", "api": "s3v4", "path": "auto",
                       "accessKey": os.environ["MINIO_ROOT_USER"],
                       "secretKey": os.environ["MINIO_ROOT_PASSWORD"]}:
            sys.exit(92)
        record({"tool": "import-payload", "argv": list(sys.argv), "payload": payload,
                "umask": os.umask(0o077)})
        phase = "alias"
    elif args == ["mb", "--ignore-existing", "local/mlflow-artifacts"]:
        phase = "bucket"
    else:
        sys.exit(92)
elif tool == "dirname":
    print(str(Path(args[0]).parent))
elif tool == "mkdir":
    if len(args) != 2 or args[0] != "-p":
        sys.exit(92)
    Path(args[1]).mkdir(parents=True, exist_ok=True)
elif tool == "cat":
    if args:
        sys.exit(92)
    sys.stdout.write(sys.stdin.read())
elif tool != "server-sentinel":
    sys.exit(92)

if os.environ.get("FAIL_PHASE") == phase:
    # Deliberately hostile synthetic client output must stay out of public logs.
    print(os.environ["FAIL_CANARY"])
    print(os.environ["FAIL_CANARY"], file=sys.stderr)
    sys.exit(23)
if phase == "bucket":
    Path(os.environ["BUCKET_STATE"]).touch(exist_ok=True)
if tool == "mc":
    # Real alias JSON output contains credentials even on success.
    print(os.environ["FAIL_CANARY"])
    print(os.environ["FAIL_CANARY"], file=sys.stderr)
'''


def minio_command():
    """Fail closed if the supported Compose command shape changes."""
    text = (ROOT / "compose.yml").read_text(encoding="utf-8")
    service = text.split("\n  minio-init:\n", 1)[1].split("\n  mlflow:\n", 1)[0]
    if '    entrypoint: ["/bin/sh", "-c"]\n' not in service:
        raise AssertionError("Unsupported MinIO entrypoint shape")
    block = service.split("    command:\n      - >-\n", 1)[1].split(
        "    environment:\n", 1
    )[0]
    lines = block.splitlines()
    if not lines or any(not line.startswith("        ") for line in lines):
        raise AssertionError("Unsupported MinIO folded command shape")
    # Parse YAML folding, including more-indented shell function bodies. No
    # Compose interpolation, overlays, environment-file or client access occurs.
    command = yaml.safe_load(text)["services"]["minio-init"]["command"][0]
    if "$" in command.replace("$$", ""):
        raise AssertionError("Unsupported Compose interpolation")
    return command.replace("$$", "$")


class ShellSandbox:
    """Own private temporary files; never inherit the caller's environment."""

    def __init__(self, reserved=False):
        self.temporary = tempfile.TemporaryDirectory(prefix="argv-contract-")
        self.root = Path(self.temporary.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # An absolute interpreter prevents shebang resolution through ambient PATH.
        if " " in sys.executable:
            raise AssertionError("Harness requires an interpreter path without spaces")
        for tool in ("mlflow", "python", "mc", "server-sentinel", "mkdir", "dirname", "cat", "printf"):
            executable = self.bin / tool
            executable.write_text(f"#!{sys.executable} -I\n" + BOUNDARY, encoding="utf-8")
            executable.chmod(0o700)
        password = "synthetic-% space 'quote\"-ñ" if reserved else "synthetic-db-canary"
        encoded = quote(password, safe="")
        self.uri = f"postgresql+psycopg2://synthetic-user:{encoded}@invalid:5432/mlflow"
        self.config = self.root / "private" / "auth.ini"
        self.recorder = self.root / "events.jsonl"
        self.env = {
            "PATH": str(self.bin), "HOME": str(self.root), "TMPDIR": str(self.root),
            "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1",
            "RECORDER": str(self.recorder), "BUCKET_STATE": str(self.root / "bucket"),
            "MLFLOW_AUTH_CONFIG_PATH": str(self.config),
            "MLFLOW_BACKEND_STORE_URI": self.uri, "MLFLOW_AUTH_DATABASE_URI": self.uri,
            "MLFLOW_AUTH_ADMIN_USERNAME": "synthetic-admin",
            "MLFLOW_AUTH_ADMIN_PASSWORD": password,
            "MLFLOW_FLASK_SERVER_SECRET_KEY": "synthetic-flask-canary",
            "MINIO_ROOT_USER": "synthetic-minio-user",
            "MINIO_ROOT_PASSWORD": password,
            "FAIL_CANARY": "synthetic-client-failure-canary",
        }
        self.secrets = (password, encoded, self.uri, self.env["MINIO_ROOT_USER"],
                        self.env["MLFLOW_AUTH_ADMIN_USERNAME"],
                        self.env["MLFLOW_FLASK_SERVER_SECRET_KEY"], self.env["FAIL_CANARY"])

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.temporary.cleanup()

    def run(self, *, minio=False, fail="", missing=None, overrides=None,
            helper=None, shell_prefix=""):
        self.recorder.unlink(missing_ok=True)
        env = dict(self.env, FAIL_PHASE=fail)
        env.update(overrides or {})
        if missing:
            env.pop(missing)
        # No source rewriting/path substitution: the auth path is a supported env input.
        command = (["/bin/sh", "-c", shell_prefix + minio_command()] if minio else
                   ["/bin/sh", str(ENTRYPOINT), "server-sentinel"])
        if helper:
            command = [str(self.bin / "python"), str(ROOT / "docker/mlflow-migrate.py"), helper]
        try:
            result = subprocess.run(command, cwd=self.root, env=env, capture_output=True,
                                    timeout=8, check=False)
        except subprocess.TimeoutExpired:
            raise AssertionError("Sandbox shell exceeded its execution budget") from None
        events = []
        if self.recorder.exists():
            if self.recorder.stat().st_mode & 0o777 != 0o600:
                raise AssertionError("Recorder must remain private")
            events = [json.loads(line) for line in self.recorder.read_text().splitlines()]
        return result, events


def phases(events):
    return [event["tool"] for event in events
            if event["tool"] in ("tracking", "auth", "server-sentinel", "mc")]


def assert_sanitized(case, sandbox, result):
    public = (result.stdout + result.stderr).decode("utf-8", errors="replace")
    case.assertFalse(any(secret in public for secret in sandbox.secrets),
                     "Public output contains a synthetic secret/canary")


def assert_no_secret_argv(case, sandbox, events):
    # Never include argv values in assertion messages or unittest diffs.
    positions = [(event["tool"], index) for event in events
                 for index, argument in enumerate(event["argv"])
                 if any(secret in argument for secret in sandbox.secrets)]
    case.assertFalse(bool(positions), f"Secret-bearing argv positions: {positions}")


class SecretArgvContractTests(unittest.TestCase):
    def test_minio_json_preserves_controls_reserved_and_unicode_without_argv_secrets(self):
        passwords = ["synthetic:" + "".join(chr(code) for code in range(1, 32)),
                     "synthetic 'quote\" \\ slash/ @:% ?= &$`$(unused) ñ中😀\n\n",
                     "synthetic-" + "x" * 5000]
        for index, password in enumerate(passwords):
            with self.subTest(case=index), ShellSandbox() as sandbox:
                user = "synthetic-user:\"\\ñ\t"
                sandbox.secrets += (password, quote(password, safe=""), user)
                result, events = sandbox.run(minio=True, overrides={
                    "MINIO_ROOT_USER": user, "MINIO_ROOT_PASSWORD": password})
                self.assertEqual(result.returncode, 0, "Initializer did not preserve JSON inputs")
                self.assertEqual(phases(events), ["mc", "mc"])
                imported = [e for e in events if e["tool"] == "import-payload"]
                self.assertEqual(len(imported), 1)
                self.assertTrue(imported[0]["payload"]["secretKey"] == password,
                                "JSON password changed")
                self.assertTrue(imported[0]["payload"]["accessKey"] == user,
                                "JSON access key changed")
                self.assertEqual(imported[0]["umask"], 0o077)
                assert_sanitized(self, sandbox, result)
                assert_no_secret_argv(self, sandbox, events)

    def test_minio_rejects_external_printf_before_credentials_or_client(self):
        with ShellSandbox() as sandbox:
            result, events = sandbox.run(
                minio=True, shell_prefix="command() { echo /unavailable/printf; }; ")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(events)
            self.assertIn(b"requires builtin printf", result.stderr)
            assert_sanitized(self, sandbox, result)

    def test_minio_required_variables_fail_before_client(self):
        for key in ("MINIO_ROOT_USER", "MINIO_ROOT_PASSWORD"):
            with self.subTest(variable=key), ShellSandbox() as sandbox:
                result, events = sandbox.run(minio=True, missing=key)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(events)
                assert_sanitized(self, sandbox, result)

    def test_helper_itself_suppresses_import_and_callback_output(self):
        for phase in ("tracking", "auth"):
            for failure in ("", phase, "import-" + phase, "systemexit-" + phase):
                with self.subTest(phase=phase, failure=failure), ShellSandbox() as sandbox:
                    result, events = sandbox.run(helper=phase, fail=failure)
                    self.assertEqual(result.returncode, 1 if failure else 0)
                    self.assertEqual(phases(events), [] if failure.startswith("import-") else [phase])
                    assert_sanitized(self, sandbox, result)
                    assert_no_secret_argv(self, sandbox, events)

    def test_import_failures_block_later_phases(self):
        for phase, expected in (("tracking", []), ("auth", ["tracking"])):
            with self.subTest(phase=phase), ShellSandbox() as sandbox:
                result, events = sandbox.run(fail="import-" + phase)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(phases(events), expected)
                assert_sanitized(self, sandbox, result)
                assert_no_secret_argv(self, sandbox, events)

    def test_distinct_database_uris_reach_the_correct_callbacks_unchanged(self):
        with ShellSandbox(reserved=True) as sandbox:
            auth_uri = sandbox.uri.replace("/mlflow", "/mlflow_auth")
            sandbox.secrets += (auth_uri,)
            result, events = sandbox.run(overrides={"MLFLOW_AUTH_DATABASE_URI": auth_uri})
            self.assertEqual(result.returncode, 0)
            callbacks = [event for event in events if event["tool"] in ("tracking", "auth")]
            self.assertEqual([event["tool"] for event in callbacks], ["tracking", "auth"])
            self.assertTrue(callbacks[0]["url"] == sandbox.uri, "Tracking URI changed")
            self.assertTrue(callbacks[1]["url"] == auth_uri, "Authorization URI changed")
            assert_sanitized(self, sandbox, result)
            assert_no_secret_argv(self, sandbox, events)

    def test_tracking_migration_has_no_secret_argv(self):
        self.check_migration_argv("tracking")

    def test_auth_migration_has_no_secret_argv(self):
        self.check_migration_argv("auth")

    def check_migration_argv(self, tool):
        for reserved in (False, True):
            with self.subTest(reserved=reserved), ShellSandbox(reserved) as sandbox:
                result, events = sandbox.run()
                self.assertEqual(result.returncode, 0, "Shell did not reach successful startup")
                self.assertEqual(phases(events), ["tracking", "auth", "server-sentinel"])
                assert_sanitized(self, sandbox, result)
                migration = [event for event in events if event["tool"] == tool]
                self.assertEqual(len(migration), 1)
                self.assertTrue(all(value == sandbox.uri for value in
                                    migration[0]["database_environment"].values()),
                                "Encoded database environment changed")
                assert_no_secret_argv(self, sandbox, events)

    def test_minio_has_no_secret_argv(self):
        for reserved in (False, True):
            with self.subTest(reserved=reserved), ShellSandbox(reserved) as sandbox:
                result, events = sandbox.run(minio=True)
                self.assertEqual(result.returncode, 0)
                self.assertEqual(phases(events), ["mc", "mc"])
                calls = [event for event in events if event["tool"] == "mc"]
                self.assertTrue(calls[1]["argv"][1:] ==
                                ["mb", "--ignore-existing", "local/mlflow-artifacts"],
                                "Bucket call changed the ignore-existing contract")
                assert_sanitized(self, sandbox, result)
                assert_no_secret_argv(self, sandbox, events)

    def test_private_auth_config_preserves_values_before_migrations(self):
        with ShellSandbox(reserved=True) as sandbox:
            result, events = sandbox.run(fail="tracking")
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(phases(events), ["tracking"])
            self.assertEqual(sandbox.config.stat().st_mode & 0o777, 0o600)
            config = sandbox.config.read_text(encoding="utf-8")
            for key, value in (("database_uri", sandbox.uri),
                               ("admin_username", sandbox.env["MLFLOW_AUTH_ADMIN_USERNAME"]),
                               ("admin_password", sandbox.env["MLFLOW_AUTH_ADMIN_PASSWORD"]),
                               ("default_permission", "READ"),
                               ("grant_default_workspace_access", "false")):
                self.assertTrue(f"{key} = {value}\n" in config,
                                f"Auth config did not preserve field {key}")
            assert_sanitized(self, sandbox, result)

    def test_required_variables_fail_before_config_or_external_tools(self):
        keys = ("MLFLOW_AUTH_CONFIG_PATH", "MLFLOW_AUTH_DATABASE_URI",
                "MLFLOW_BACKEND_STORE_URI", "MLFLOW_AUTH_ADMIN_USERNAME",
                "MLFLOW_AUTH_ADMIN_PASSWORD", "MLFLOW_FLASK_SERVER_SECRET_KEY")
        for key in keys:
            with self.subTest(variable=key), ShellSandbox() as sandbox:
                result, events = sandbox.run(missing=key)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(bool(events), "Required-variable guard invoked external tools")
                self.assertFalse(sandbox.config.exists())
                self.assertIn(key.encode(), result.stderr)
                assert_sanitized(self, sandbox, result)

    def test_example_guards_precede_config_and_external_tools(self):
        guards = {"MLFLOW_AUTH_ADMIN_USERNAME": "replace-with-initial-admin-username",
                  "MLFLOW_AUTH_ADMIN_PASSWORD": "replace-with-generated-password",
                  "MLFLOW_FLASK_SERVER_SECRET_KEY": "replace-with-generated-secret-key"}
        for key, placeholder in guards.items():
            with self.subTest(variable=key), ShellSandbox() as sandbox:
                result, events = sandbox.run(overrides={key: placeholder})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(bool(events), "Placeholder guard invoked external tools")
                self.assertFalse(sandbox.config.exists())
                self.assertIn(b"must not use the example placeholder", result.stderr)
                assert_sanitized(self, sandbox, result)

    def test_migration_failures_block_later_phases_and_hide_client_output(self):
        for phase, expected, message in (
            ("tracking", ["tracking"], b"MLflow backend database migration failed"),
            ("auth", ["tracking", "auth"], b"MLflow authorization database migration failed"),
        ):
            with self.subTest(phase=phase), ShellSandbox() as sandbox:
                result, events = sandbox.run(fail=phase)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(phases(events), expected)
                self.assertIn(message, result.stderr)
                assert_sanitized(self, sandbox, result)

    def test_minio_errors_fail_closed(self):
        for phase, count in (("alias", 1), ("bucket", 2)):
            with self.subTest(phase=phase), ShellSandbox() as sandbox:
                result, events = sandbox.run(minio=True, fail=phase)
                self.assertEqual(result.returncode, 23)
                self.assertEqual(phases(events), ["mc"] * count)
                self.assertFalse((sandbox.root / "bucket").exists())

    def test_minio_failure_output_is_sanitized(self):
        for phase in ("alias", "bucket"):
            with self.subTest(phase=phase), ShellSandbox() as sandbox:
                result, events = sandbox.run(minio=True, fail=phase)
                self.assertEqual(result.returncode, 23)
                self.assertEqual(phases(events), ["mc"] * (1 if phase == "alias" else 2))
                assert_sanitized(self, sandbox, result)

    def test_minio_repeated_initialization_keeps_ignore_existing_contract(self):
        with ShellSandbox() as sandbox:
            for invocation in range(2):
                with self.subTest(invocation=invocation):
                    result, events = sandbox.run(minio=True)
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(phases(events), ["mc", "mc"])
                    self.assertTrue(events[-1]["argv"][1:] ==
                                    ["mb", "--ignore-existing", "local/mlflow-artifacts"],
                                    "Repeated bucket call lost ignore-existing behavior")
                    self.assertTrue((sandbox.root / "bucket").exists())

    def test_minio_readiness_remains_a_compose_health_dependency(self):
        # Static prerequisite only: no health probe or retry loop runs offline.
        compose = (ROOT / "compose.yml").read_text(encoding="utf-8")
        service = compose.split("\n  minio-init:\n", 1)[1].split("\n  mlflow:\n", 1)[0]
        self.assertIn("    depends_on:\n      minio:\n        condition: service_healthy\n",
                      service)


if __name__ == "__main__":
    unittest.main()
