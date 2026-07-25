import os
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from analyzer import keyword_screen


class AnalyzerTests(unittest.TestCase):
    def test_category_level_error_annotations_are_counted(self):
        with tempfile.TemporaryDirectory() as vault:
            for project, session in (("one", "s1"), ("two", "s2")):
                path = os.path.join(
                    vault,
                    "01-Projects",
                    project,
                    "Memory",
                    "sessions",
                    f"2026-07-10-{session}.md",
                )
                os.makedirs(os.path.dirname(path), exist_ok=True)
                write_text(
                    path,
                    """---
errors_encountered:
- type: path-filesystem
  resolution: 修复越界路径
---
""",
                )
            taxonomy = {
                "categories": [
                    {"name": "path-filesystem", "subcategories": ["permission"]}
                ]
            }

            patterns = keyword_screen(
                vault,
                error_types=["permission"],
                taxonomy=taxonomy,
            )

            self.assertEqual(len(patterns), 1)
            self.assertEqual(patterns[0]["error_type"], "path-filesystem")
            self.assertEqual(patterns[0]["count"], 2)


def write_text(path, content):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


if __name__ == "__main__":
    unittest.main()
