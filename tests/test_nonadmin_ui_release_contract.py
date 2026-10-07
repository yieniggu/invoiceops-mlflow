import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NonAdminUIReleaseContractTests(unittest.TestCase):
    def test_image_pins_published_nonadmin_ui_fix(self):
        dockerfile = (ROOT / "Dockerfile.mlflow").read_text(encoding="utf-8")
        self.assertIn('"mlflow[auth]==3.16.1"', dockerfile)
        self.assertNotIn('"mlflow[auth]==3.16.0"', dockerfile)


if __name__ == "__main__":
    unittest.main()
