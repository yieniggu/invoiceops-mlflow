import subprocess
import unittest
from pathlib import Path


class LocalRunbookTests(unittest.TestCase):
    def test_fresh_install_recipe_uses_current_run_ids_only(self):
        runbook = (Path(__file__).resolve().parents[1] / "LOCAL_INVOICEOPS_RUNBOOK.md").read_text(
            encoding="utf-8"
        )
        recipe = runbook.split("## Train existing local candidates; promote only a Gate PASS")[
            1
        ].split("## Start App and Model API; verify release path")[0]
        self.assertIn("uv run invoiceops-train-candidate", recipe)
        self.assertIn('json.load(sys.stdin)["run_id"]', recipe)
        self.assertIn('uv run invoiceops-quality-gate --run-id "$RF_RUN_ID"', recipe)
        self.assertNotIn('RUN_IDS=(53713946', recipe)
        self.assertIn('HGB_RUN_IDS+=("$RUN_ID")', recipe)
        self.assertIn('--candidate-run-id "${HGB_RUN_IDS[0]}"', recipe)
        self.assertNotIn('--candidate-run-id "${RUN_IDS[0]}"', recipe)
        script = recipe.split("```bash", 1)[1].split("```", 1)[0]
        result = subprocess.run(["bash", "-n"], input=script, text=True,
                                capture_output=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
