"""Check public-export privacy boundaries with synthetic files only."""
from pathlib import Path
import tempfile
import unittest

from verify_public_export import REQUIRED_FILES, verify_public_tree


class PublicExportPrivacyTests(unittest.TestCase):
    def check_rejected(self, relative, content, expected):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in REQUIRED_FILES:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("", encoding="utf-8")
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, expected):
                verify_public_tree(root)

    def test_real_environment_is_rejected(self):
        self.check_rejected(".env", "EXAMPLE=value", "real environment file")

    def test_example_secret_must_be_blank(self):
        self.check_rejected("backend/.env.example", "OPENAI_API_KEY=example", "must be blank")

    def test_private_incident_fixture_is_rejected(self):
        self.check_rejected("backend/tests/fixtures/example.json", "{}", "private incident fixture")

    def test_private_fixture_reference_is_rejected(self):
        marker = "unfinished_" + "task_move.json"
        self.check_rejected("backend/tests/test_example.py", marker, "private evaluation test")

    def test_private_deployment_identifier_is_rejected(self):
        self.check_rejected("docs/example.md", "dpl_" + "a" * 25, "private deployment coordinates")

    def test_personal_home_path_is_rejected(self):
        self.check_rejected("docs/example.md", "C:/Users/" + "private-person/project", "personal Windows home path")

    def test_database_is_rejected(self):
        self.check_rejected("backend/accounts/example.sqlite", "", "database or generated artifact")

    def test_auth_file_is_rejected(self):
        self.check_rejected("backend/accounts/auth.json", "{}", "private file")


if __name__ == "__main__":
    unittest.main()
