import os
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from validate_frontmatter import validate_frontmatter


class ValidateFrontmatterTests(unittest.TestCase):
    def test_decisions_log_without_project_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "decisions.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("---\ndecisions: []\n---\n\n# Decisions\n")

            ok, errors, template_type = validate_frontmatter(path)

            self.assertFalse(ok)
            self.assertEqual(template_type, "decisions")
            self.assertIn("Missing required field: project", errors)

    def test_frontmatter_delimiter_inside_quoted_scalar_is_not_a_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "decisions.md")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    "---\n"
                    "project: demo\n"
                    "decisions: []\n"
                    "evidence_examples:\n"
                    "- 'before --- name: embedded --- after'\n"
                    "---\n\n"
                    "# Decisions\n"
                )

            ok, errors, template_type = validate_frontmatter(path)

            self.assertTrue(ok, errors)
            self.assertEqual(template_type, "decisions")


if __name__ == "__main__":
    unittest.main()
