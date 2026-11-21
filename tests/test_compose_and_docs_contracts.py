"""Configuration, script and documentation contracts of the compose files and helper scripts."""

import os
import re
import stat
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

import yaml

from test_secret_argv_contract import ShellSandbox

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "gateway" / "src" / "invoiceops_mlflow_gateway" / "config.py"


class ComposeLoader(yaml.SafeLoader):
    pass


ComposeLoader.add_constructor("!override", lambda loader, node: loader.construct_sequence(node))


def load_compose(name):
    return yaml.load((ROOT / name).read_text(encoding="utf-8"), Loader=ComposeLoader)


def settings_variables():
    """Every GATEWAY_* variable Settings.from_env reads."""
    return set(re.findall(r'"(GATEWAY_[A-Z_]+)"', CONFIG.read_text(encoding="utf-8")))


def documented_variables():
    """GATEWAY_* names in gateway.env.example, commented-out optional ones included."""
    text = (ROOT / "gateway.env.example").read_text(encoding="utf-8")
    return dict(re.findall(r"^#?\s*(GATEWAY_[A-Z_]+)=(\S*)", text, re.MULTILINE))


# Set by compose from other variables (MLFLOW_AUTH_*, APP_PUBLIC_URL, ...) or for development only.
MAPPED_OR_DEV_ONLY = {
    "GATEWAY_ALLOW_INSECURE_HTTP",
    "GATEWAY_UPSTREAM_URL",
    "GATEWAY_UPSTREAM_USERNAME",
    "GATEWAY_UPSTREAM_PASSWORD",
    "GATEWAY_INTROSPECTION_SECRET",
    "GATEWAY_APP_ORIGIN",
}


class GatewayEnvironmentForwardingTests(unittest.TestCase):
    """A documented setting put in .env must reach the container."""

    def setUp(self):
        self.environment = load_compose("compose.gateway.yml")["services"]["gateway"]["environment"]

    def test_every_variable_read_and_documented_is_in_the_service_environment(self):
        missing = (settings_variables() & set(documented_variables())) - set(self.environment)
        self.assertEqual(missing, set())

    def test_every_optional_setting_is_documented_and_forwarded(self):
        optional = settings_variables() - MAPPED_OR_DEV_ONLY
        self.assertEqual(optional - set(documented_variables()), set(), "undocumented in gateway.env.example")
        self.assertEqual(optional - set(self.environment), set(), "not forwarded by compose.gateway.yml")

    def test_the_development_only_flag_is_never_forwarded(self):
        self.assertNotIn("GATEWAY_ALLOW_INSECURE_HTTP", self.environment)

    def test_forwarded_defaults_equal_the_documented_defaults(self):
        documented = documented_variables()
        for name, value in self.environment.items():
            match = re.fullmatch(r"\$\{%s:-([^}]*)\}" % name, str(value))
            if match is None or name not in documented or not documented[name]:
                continue
            with self.subTest(name=name):
                self.assertEqual(match.group(1), documented[name])

    def test_the_contract_detects_a_missing_variable(self):
        broken = {k: v for k, v in self.environment.items() if k != "GATEWAY_AUTH_FAILURE_LIMIT"}
        missing = (settings_variables() & set(documented_variables())) - set(broken)
        self.assertEqual(missing, {"GATEWAY_AUTH_FAILURE_LIMIT"})


class PublicCredentialsGuardTests(unittest.TestCase):
    """The production overlay rejects the throwaway values of mlflow-local.example.env."""

    @staticmethod
    def public_values():
        return dict(
            line.split("=", 1)
            for line in (ROOT / "mlflow-local.example.env").read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        )

    def uri(self, password):
        return f"postgresql+psycopg2://mlflow:{password}@postgres:5432/mlflow"

    def test_overlay_turns_the_guard_on(self):
        environment = load_compose("compose.gateway.yml")["services"]["mlflow"]["environment"]
        self.assertEqual(environment["MLFLOW_REJECT_PUBLIC_CREDENTIALS"], "1")
        self.assertNotIn("MLFLOW_REJECT_PUBLIC_CREDENTIALS", load_compose("compose.yml")["services"]["mlflow"]["environment"])

    def test_each_public_value_is_rejected_in_production_mode(self):
        values = self.public_values()
        cases = {
            "database password in the backend URI": {"MLFLOW_BACKEND_STORE_URI": self.uri(values["MLFLOW_DB_PASSWORD"])},
            "database password in the auth URI": {"MLFLOW_AUTH_DATABASE_URI": self.uri(values["MLFLOW_DB_PASSWORD"])},
            "MinIO user": {"AWS_ACCESS_KEY_ID": values["MINIO_ROOT_USER"]},
            "MinIO password": {"AWS_SECRET_ACCESS_KEY": values["MINIO_ROOT_PASSWORD"]},
        }
        for label, overrides in cases.items():
            with self.subTest(label), ShellSandbox() as sandbox:
                result, events = sandbox.run(overrides={"MLFLOW_REJECT_PUBLIC_CREDENTIALS": "1", **overrides})
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(events, [], "an external tool ran before the guard")
                self.assertFalse(sandbox.config.exists())
                self.assertIn(b"public local example values", result.stderr)

    def test_the_same_values_still_work_for_the_local_standalone_compose(self):
        values = self.public_values()
        with ShellSandbox() as sandbox:
            result, _ = sandbox.run(
                overrides={
                    "MLFLOW_BACKEND_STORE_URI": self.uri(values["MLFLOW_DB_PASSWORD"]),
                    "AWS_ACCESS_KEY_ID": values["MINIO_ROOT_USER"],
                    "AWS_SECRET_ACCESS_KEY": values["MINIO_ROOT_PASSWORD"],
                }
            )
            self.assertEqual(result.returncode, 0)

    def test_generated_values_pass_the_production_guard(self):
        with ShellSandbox() as sandbox:
            result, _ = sandbox.run(
                overrides={
                    "MLFLOW_REJECT_PUBLIC_CREDENTIALS": "1",
                    "AWS_ACCESS_KEY_ID": "generated-user-9f3a",
                    "AWS_SECRET_ACCESS_KEY": "generated-secret-0c6d2f4e8b13",
                }
            )
            self.assertEqual(result.returncode, 0)

    def test_guard_literals_equal_the_example_file(self):
        entrypoint = (ROOT / "docker" / "mlflow-entrypoint.sh").read_text(encoding="utf-8")
        values = self.public_values()
        self.assertIn(values["MLFLOW_DB_PASSWORD"], entrypoint)
        self.assertIn(values["MINIO_ROOT_PASSWORD"], entrypoint)
        self.assertIn(f"\n    {values['MINIO_ROOT_USER']})", entrypoint)


class PostgresPlaceholderGuardTests(unittest.TestCase):
    """The postgres service refuses a placeholder password before its volume is initialized."""

    def setUp(self):
        service = load_compose("compose.yml")["services"]["postgres"]
        self.service = service
        # Compose renders "$$" as a literal "$" before the shell sees the script.
        self.entrypoint = [part.replace("$$", "$") for part in service["entrypoint"]]
        self.command = service["command"]

    def run_guard(self, password):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            fake = bin_dir / "docker-entrypoint.sh"
            fake.write_text('#!/bin/sh\necho "started $*" >>"$FAKE_LOG"\n')
            fake.chmod(0o755)
            log = Path(tmp) / "started.log"
            result = subprocess.run(
                [*self.entrypoint, *self.command],
                env={"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_LOG": str(log), "POSTGRES_PASSWORD": password},
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            started = log.read_text().strip() if log.exists() else ""
        return result, started

    def test_the_guard_wraps_the_image_entrypoint_and_keeps_its_command(self):
        self.assertEqual(self.entrypoint[:2], ["/bin/sh", "-c"])
        self.assertIn("docker-entrypoint.sh", self.entrypoint[2])
        self.assertEqual(self.command, ["postgres"])
        # The wrapper must not skip the image's own initialization (initdb, privilege drop).
        _, started = self.run_guard("generated-db-password-9f3a")
        self.assertEqual(started, "started postgres")

    def test_every_placeholder_is_refused_before_the_image_entrypoint_runs(self):
        for password in (
            "replace-with-generated-db-password",
            "prefix-REPLACE_WITH-suffix",
            "changeme",
            "xx-changeme-yy",
        ):
            with self.subTest(password=password):
                result, started = self.run_guard(password)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(started, "", "the database entrypoint ran with a placeholder")
                self.assertIn("must not use the example placeholder", result.stderr)
                self.assertNotIn(password, result.stderr + result.stdout)

    def test_the_password_comes_from_the_same_variable_as_mlflow(self):
        self.assertIn("MLFLOW_DB_PASSWORD", str(self.service["environment"]["POSTGRES_PASSWORD"]))

    def test_the_production_overlay_keeps_the_guard(self):
        merged = load_compose("compose.gateway.yml")["services"]["postgres"]
        self.assertNotIn("entrypoint", merged)  # inherited from compose.yml, never replaced
        self.assertNotIn("command", merged)

    def test_the_readme_explains_how_to_recover_a_volume_with_a_wrong_password(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("down -v", readme)
        self.assertRegex(readme, r"\\password mlflow")


class MlflowImageTests(unittest.TestCase):
    """Unprivileged runtime user and a fully pinned, hash-verified dependency set."""

    def instructions(self):
        text = (ROOT / "Dockerfile.mlflow").read_text(encoding="utf-8").replace("\\\n", " ")
        return [line.split() for line in text.splitlines() if line.strip() and not line.startswith("#")]

    def test_the_image_runs_as_a_non_root_user(self):
        users = [parts[1] for parts in self.instructions() if parts[0].upper() == "USER"]
        self.assertTrue(users)
        self.assertNotIn(users[-1].split(":")[0], ("root", "0"))
        self.assertTrue(users[-1].split(":")[0].isdigit() or users[-1] != "root")

    def test_dependencies_install_only_from_the_hashed_lock(self):
        runs = [" ".join(parts) for parts in self.instructions() if parts[0].upper() == "RUN"]
        install = next(run for run in runs if "pip install" in run)
        self.assertIn("--require-hashes", install)
        self.assertIn("--only-binary=:all:", install)
        self.assertIn("-r /tmp/requirements.mlflow.txt", install)
        self.assertNotIn("mlflow[auth]==", install)

    def test_the_lock_pins_every_package_with_hashes(self):
        text = (ROOT / "requirements.mlflow.txt").read_text(encoding="utf-8")
        entries = re.findall(r"^([A-Za-z0-9_.\-\[\]]+)==\S+.*?(?=^\S|\Z)", text, re.MULTILINE | re.DOTALL)
        self.assertGreater(len(entries), 30)  # the resolved set, not just the three top-level pins
        blocks = re.split(r"^(?=[A-Za-z0-9_.\-]+==)", text, flags=re.MULTILINE)[1:]
        for block in blocks:
            with self.subTest(package=block.split("==")[0]):
                self.assertIn("--hash=sha256:", block)
        sources = (ROOT / "requirements.mlflow.in").read_text(encoding="utf-8")
        for pin in ("mlflow[auth]==3.16.1", "psycopg2-binary==2.9.10", "boto3==1.37.34"):
            self.assertIn(pin, sources)
        self.assertRegex(text, r"(?m)^mlflow==3\.16\.1 ")
        self.assertIn("urllib3", text)  # a transitive dependency is pinned too


FAKE_DOCKER = textwrap.dedent(
    """\
    #!/bin/sh
    echo "$*" >>"$FAKE_LOG"
    case "$1 $2" in
      "volume inspect")
        for volume in $FAKE_VOLUMES; do [ "$volume" = "$3" ] && exit 0; done
        exit 1 ;;
      "volume create") exit 0 ;;
    esac
    case "$1" in
      ps) exit 0 ;;
      run)
        case "$*" in *"ls -A"*) printf '%s' "$FAKE_TARGET_CONTENT" ;; esac
        exit 0 ;;
    esac
    exit 0
    """
)


class MigrateStandaloneVolumesTests(unittest.TestCase):
    """A migration that copied nothing must not look like a success."""

    def run_script(self, args, volumes, target_content=""):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp) / "bin"
            bin_dir.mkdir()
            docker = bin_dir / "docker"
            docker.write_text(FAKE_DOCKER)
            docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
            log = Path(tmp) / "calls.log"
            env = {
                "PATH": f"{bin_dir}:/usr/bin:/bin",
                "FAKE_LOG": str(log),
                "FAKE_VOLUMES": " ".join(volumes),
                "FAKE_TARGET_CONTENT": target_content,
            }
            result = subprocess.run(
                ["/bin/sh", str(ROOT / "scripts" / "migrate-standalone-volumes.sh"), *args],
                env=env, capture_output=True, text=True, check=False, timeout=30,
            )
            calls = log.read_text().splitlines() if log.exists() else []
        copies = [call for call in calls if call.startswith("run ") and "cp -a" in call]
        return result, copies

    def test_missing_source_project_exits_non_zero_and_copies_nothing(self):
        result, copies = self.run_script(["no-such-project"], volumes=[])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(copies, [])
        self.assertIn("Nothing was copied", result.stderr)

    def test_only_unrelated_volumes_still_counts_as_nothing_copied(self):
        result, copies = self.run_script([], volumes=["other_postgres_data"])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(copies, [])

    def test_both_volumes_are_copied_and_exit_zero(self):
        volumes = ["invoiceops-mlflow_postgres_data", "invoiceops-mlflow_minio_data"]
        result, copies = self.run_script([], volumes=volumes)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(copies), 2)
        self.assertIn("2 volume(s) copied", result.stderr)

    def test_one_present_volume_is_copied_and_the_other_is_skipped(self):
        result, copies = self.run_script(["src", "dst"], volumes=["src_minio_data"])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(copies), 1)
        self.assertIn("src_postgres_data does not exist", result.stderr)

    def test_a_non_empty_target_still_refuses_with_a_failure(self):
        volumes = ["src_postgres_data", "dst_mlflow_postgres_data"]
        result, copies = self.run_script(["src", "dst"], volumes=volumes, target_content="data")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(copies, [])


class RestoreScriptTests(unittest.TestCase):
    """One-transaction restore and a manifest that must match the directory exactly."""

    def test_pg_restore_runs_in_a_single_transaction_and_stops_on_error(self):
        script = (ROOT / "scripts" / "restore.sh").read_text(encoding="utf-8")
        command = script[script.index("pg_restore") :].split("<", 1)[0]
        self.assertIn("--single-transaction", command)
        self.assertIn("--exit-on-error", command)

    def make_backup(self, root, extra=None, drop=None):
        backup = Path(root) / "backup"
        (backup / "artifacts").mkdir(parents=True)
        (backup / "mlflow.pgdump").write_bytes(b"PGDMP")
        (backup / "artifacts" / "a.bin").write_bytes(b"artifact")
        subprocess.run(
            # Same shape as scripts/backup.sh: the manifest never lists itself.
            "m=$(find . -type f | LC_ALL=C sort | while IFS= read -r f; do shasum -a 256 \"$f\"; done);"
            " printf '%s\\n' \"$m\" > MANIFEST.sha256",
            shell=True, cwd=backup, check=True,
        )
        if extra:
            (backup / extra).write_bytes(b"injected")
        if drop:
            (backup / drop).unlink()
        return backup

    def restore(self, backup, root):
        marker = Path(root) / "compose-called"
        compose = Path(root) / "fake-compose"
        compose.write_text(f"#!/bin/sh\necho called >>{marker}\nexit 1\n")
        compose.chmod(0o755)
        result = subprocess.run(
            ["/bin/sh", str(ROOT / "scripts" / "restore.sh"), str(backup)],
            env={"PATH": "/usr/bin:/bin:/usr/local/bin", "MLFLOW_COMPOSE": str(compose)},
            capture_output=True, text=True, check=False, timeout=30,
        )
        return result, marker.exists()

    def test_an_exact_manifest_passes_the_check_and_reaches_the_stack(self):
        with tempfile.TemporaryDirectory() as root:
            result, compose_called = self.restore(self.make_backup(root), root)
        self.assertTrue(compose_called, result.stderr)  # went past verification
        self.assertIn("Could not stop gateway and mlflow", result.stderr)

    def test_an_extra_file_is_refused_before_anything_is_touched(self):
        for extra in ("artifacts/injected.bin", "injected-at-root.txt"):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as root:
                result, compose_called = self.restore(self.make_backup(root, extra=extra), root)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(compose_called)
                self.assertIn("manifest does not match", result.stderr)

    def test_an_extra_symlink_is_refused_too(self):
        with tempfile.TemporaryDirectory() as root:
            backup = self.make_backup(root)
            os.symlink("/etc/hostname", backup / "artifacts" / "link")
            result, compose_called = self.restore(backup, root)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(compose_called)

    def test_a_missing_file_is_refused_by_the_checksum_step(self):
        with tempfile.TemporaryDirectory() as root:
            result, compose_called = self.restore(self.make_backup(root, drop="artifacts/a.bin"), root)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(compose_called)


class ReadmeClaimsTests(unittest.TestCase):
    """The README states what is published where."""

    def setUp(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.readme = re.sub(r"\s*\n>?\s*", " ", text)  # unwrap lines and blockquotes

    def test_standalone_compose_is_loopback_local_only_and_production_goes_through_the_gateway(self):
        text = self.readme
        self.assertRegex(text, r"(?is)compose standalone.{0,400}127\.0\.0\.1:5000.{0,200}solo para uso local")
        self.assertRegex(text, r"(?is)en producci.n MLflow \*\*no publica ning.n puerto\*\*.{0,200}gateway")

    def test_foreign_ids_answer_404(self):
        self.assertRegex(self.readme, r"(?is)otro workspace.{0,200}responde \*\*404\*\*")

    def test_compose_really_publishes_on_loopback_only(self):
        ports = load_compose("compose.yml")["services"]["mlflow"]["ports"]
        self.assertEqual(ports, ["127.0.0.1:5000:5000"])
        overlay = load_compose("compose.gateway.yml")["services"]["mlflow"]
        self.assertEqual(overlay["ports"], [])


# Where each citation lives and the test class the claim next to it is about.
CITATIONS = {
    "compose.gateway.yml": ("GatewayEnvironmentForwardingTests", ROOT),
    "docker/mlflow-entrypoint.sh": ("PublicCredentialsGuardTests", ROOT),
    "gateway/GATEWAY.md": ("GatewayEnvironmentForwardingTests", ROOT / "gateway"),
}


class CitedTestsExistTests(unittest.TestCase):
    """A comment that names a test file must name one that exists and holds the check."""

    def test_every_cited_tests_path_exists(self):
        for name, (_, base) in CITATIONS.items():
            cited = re.findall(r"tests/[\w/.-]+\.py", (ROOT / name).read_text(encoding="utf-8"))
            self.assertTrue(cited, name)
            for path in cited:
                found = (base / path).is_file() or (ROOT / path).is_file()  # gateway/ or repo tests
                self.assertTrue(found, f"{name} cites missing {path}")

    def test_the_forwarding_and_guard_claims_cite_the_file_that_holds_them(self):
        claims = {
            "compose.gateway.yml": "tests/test_compose_and_docs_contracts.py",
            "docker/mlflow-entrypoint.sh": "tests/test_compose_and_docs_contracts.py",
            "gateway/GATEWAY.md": "tests/test_compose_and_docs_contracts.py",
        }
        for name, (test_class, base) in CITATIONS.items():
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn(claims[name], text, name)
            holder = (ROOT / claims[name]).read_text(encoding="utf-8")
            self.assertIn(f"class {test_class}", holder, name)


DOCUMENTS = ("GATEWAY.md", "README.md")
SECTION_CITATION = re.compile(r'(?<![\w/.-])(GATEWAY\.md|README(?:\.md)?),? ["\u00ab]([^"\u00bb]+)["\u00bb]')


def headings(path):
    text = path.read_text(encoding="utf-8")
    return [line.lstrip("#").strip().lower() for line in text.splitlines() if line.startswith("#")]


def cited_sections(text):
    """(document, section) pairs written as ``GATEWAY.md, "Section"`` or ``README, "Section"``."""
    return [
        (document if document.endswith(".md") else document + ".md", section)
        for document, section in SECTION_CITATION.findall(text)
    ]


def missing_sections(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    return [
        f"{document} \u00ab{section}\u00bb"
        for document, section in cited_sections(text)
        if section.lower() not in headings(ROOT / document)
    ]


class CitedDocumentSectionsExistTests(unittest.TestCase):
    """A comment that names a section of GATEWAY.md or README.md must name a heading that exists."""

    scanned = [
        "compose.yml",
        "compose.gateway.yml",
        "Dockerfile.mlflow",
        "docker/mlflow-entrypoint.sh",
        "README.md",
        "gateway/GATEWAY.md",
    ]

    def test_every_cited_section_exists(self):
        for name in self.scanned:
            with self.subTest(name=name):
                self.assertEqual(missing_sections(name), [])

    def test_the_minio_pin_comment_cites_a_section_that_exists(self):
        compose = (ROOT / "compose.yml").read_text(encoding="utf-8")
        self.assertIn(("GATEWAY.md", "Riesgos residuales"), cited_sections(compose))

    def test_the_check_detects_a_section_that_does_not_exist(self):
        self.assertEqual(
            cited_sections('# (see GATEWAY.md, "Imagenes de MinIO").'),
            [("GATEWAY.md", "Imagenes de MinIO")],
        )
        self.assertNotIn("imagenes de minio", headings(ROOT / "GATEWAY.md"))
        self.assertIn("riesgos residuales", headings(ROOT / "GATEWAY.md"))

    def test_a_citation_of_another_repository_is_not_checked_here(self):
        self.assertEqual(cited_sections('see invoiceops-stack/README.md, "Levantar todo"'), [])


if __name__ == "__main__":
    unittest.main()
