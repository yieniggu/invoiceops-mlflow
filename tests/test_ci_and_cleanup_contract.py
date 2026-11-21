"""test-backup-restore.sh cleans its images, and CI builds the MLflow image."""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


class BackupRestoreCleanupTests(unittest.TestCase):
    script = (ROOT / "scripts" / "test-backup-restore.sh").read_text(encoding="utf-8")

    def cleanup_body(self):
        match = re.search(r"^cleanup\(\) \{\n(.*?)^\}", self.script, re.S | re.M)
        self.assertIsNotNone(match, "cleanup() not found")
        return match.group(1)

    def test_cleanup_removes_the_images_of_its_own_project_only(self):
        self.assertIn("remove_project_images", self.cleanup_body())
        helper = re.search(r"^remove_project_images\(\) \{\n(.*?)^\}", self.script, re.S | re.M)
        self.assertIsNotNone(helper)
        body = helper.group(1)
        self.assertIn('--filter "reference=$project-*"', body)
        self.assertIn("docker image rm", body)

    def test_cleanup_never_prunes_or_removes_unrelated_images(self):
        for forbidden in ("image prune", "system prune", "rmi -a", "--rmi all", "docker rmi"):
            self.assertNotIn(forbidden, self.script)
        self.assertNotIn("invoiceops-local", self.script)

    def test_the_project_name_is_unique_per_run(self):
        self.assertIn('project="mlflow-br-$$"', self.script)


class MlflowImageCiTests(unittest.TestCase):
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "gateway.yml").read_text())

    def test_a_job_builds_dockerfile_mlflow_for_linux_amd64(self):
        builds = [
            step["run"]
            for job in self.workflow["jobs"].values()
            for step in job["steps"]
            if "docker build" in step.get("run", "") or "buildx build" in step.get("run", "")
        ]
        mlflow_builds = [b for b in builds if "Dockerfile.mlflow" in b]
        self.assertTrue(mlflow_builds, "no CI step builds Dockerfile.mlflow")
        for command in mlflow_builds:
            self.assertIn("--platform linux/amd64", command)
            self.assertRegex(command, r"(^|\s)\.\s*$", "the build context must be the repository root")

    def test_the_mlflow_image_is_built_in_the_job_with_docker_available(self):
        job = next(
            name
            for name, spec in self.workflow["jobs"].items()
            if any("Dockerfile.mlflow" in s.get("run", "") for s in spec["steps"])
        )
        self.assertEqual(self.workflow["jobs"][job]["runs-on"], "ubuntu-latest")


def normalise(path):
    return str(path).strip().rstrip("/").removeprefix("./") or "."


def checkout_covers(step, directory):
    if not str(step.get("uses", "")).startswith("actions/checkout@"):
        return False
    path = normalise((step.get("with") or {}).get("path", "."))
    return path == "." or directory == path or directory.startswith(path + "/")


def steps_running_before_their_directory_exists(workflow):
    """Names of `run` steps whose working directory is not yet created by a prior checkout."""
    offenders = []
    workflow_default = ((workflow.get("defaults") or {}).get("run") or {}).get("working-directory")
    for job_name, job in workflow["jobs"].items():
        job_default = ((job.get("defaults") or {}).get("run") or {}).get("working-directory")
        checked_out = []
        for step in job["steps"]:
            if "uses" in step:
                checked_out.append(step)
            if "run" not in step:
                continue
            directory = normalise(step.get("working-directory") or job_default or workflow_default or ".")
            if directory != "." and not any(checkout_covers(c, directory) for c in checked_out):
                offenders.append(f"{job_name}: {step.get('name', step['run'][:40])}")
    return offenders


class WorkflowStepOrderTests(unittest.TestCase):
    """No `run` step inherits a working directory before the checkout that creates it."""

    def test_the_workflow_has_no_run_step_before_the_checkout_of_its_directory(self):
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "gateway.yml").read_text())
        self.assertEqual(steps_running_before_their_directory_exists(workflow), [])

    def test_the_contract_detects_a_step_that_inherits_a_default_before_the_checkout(self):
        broken = yaml.safe_load(
            """
jobs:
  checks:
    defaults: {run: {working-directory: app}}
    steps:
      - name: guard
        run: echo guard
      - uses: actions/checkout@v1
        with: {path: app}
"""
        )
        self.assertEqual(steps_running_before_their_directory_exists(broken), ["checks: guard"])

    def test_an_explicit_working_directory_opts_a_step_out_of_the_inherited_default(self):
        fixed = yaml.safe_load(
            """
jobs:
  checks:
    defaults: {run: {working-directory: app}}
    steps:
      - name: guard
        working-directory: .
        run: echo guard
      - uses: actions/checkout@v1
        with: {path: app}
"""
        )
        self.assertEqual(steps_running_before_their_directory_exists(fixed), [])


if __name__ == "__main__":
    unittest.main()
