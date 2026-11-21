import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NonAdminUIReleaseContractTests(unittest.TestCase):
    def test_image_pins_published_nonadmin_ui_fix(self):
        # The Dockerfile installs only from the hashed lock (requirements.mlflow.txt), generated
        # from requirements.mlflow.in.
        dockerfile = (ROOT / "Dockerfile.mlflow").read_text(encoding="utf-8")
        self.assertIn("requirements.mlflow.txt", dockerfile)
        sources = (ROOT / "requirements.mlflow.in").read_text(encoding="utf-8")
        self.assertIn("mlflow[auth]==3.16.1", sources)
        self.assertNotIn("3.16.0", sources)
        lock = (ROOT / "requirements.mlflow.txt").read_text(encoding="utf-8")
        self.assertRegex(lock, r"(?m)^mlflow==3\.16\.1 ")
        self.assertIsNone(re.search(r"(?m)^mlflow==3\.16\.0 ", lock))


if __name__ == "__main__":
    unittest.main()
