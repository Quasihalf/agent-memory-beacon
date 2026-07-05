import os
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from knowledge_index import rebuild_vault_knowledge_indexes
from memory_recall import load_recall_index, recall

try:
    from test_knowledge_index import write_fixture_vault
except ModuleNotFoundError:
    from tests.test_knowledge_index import write_fixture_vault


class MemoryRecallTests(unittest.TestCase):
    def test_recall_finds_related_decision_for_query(self):
        with tempfile.TemporaryDirectory() as vault:
            write_fixture_vault(vault)
            rebuild_vault_knowledge_indexes({"vault_path": vault})
            index = load_recall_index(vault)

            results = recall("Obsidian 中文 主存储", index, project="demo", limit=3)

            self.assertTrue(results)
            self.assertEqual(results[0]["type"], "decision")
            self.assertIn("Obsidian Markdown", results[0]["title"])
            self.assertGreater(results[0]["score"], 0)

    def test_project_filter_excludes_other_projects(self):
        with tempfile.TemporaryDirectory() as vault:
            write_fixture_vault(vault)
            rebuild_vault_knowledge_indexes({"vault_path": vault})
            index = load_recall_index(vault)

            results = recall("Obsidian 中文 主存储", index, project="other", limit=3)

            self.assertEqual(results, [])

    def test_recall_deduplicates_same_memory_from_multiple_sources(self):
        index = {
            "units": [
                recall_unit("decision:1", "01-Projects/demo/Memory/sessions/a"),
                recall_unit("decision:2", "01-Projects/demo/Memory/decisions"),
            ]
        }

        results = recall("Obsidian 中文 主存储", index, project="demo", limit=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["path"], "01-Projects/demo/Memory/sessions/a")


def recall_unit(unit_id, path):
    return {
        "id": unit_id,
        "type": "decision",
        "title": "保留 Obsidian Markdown 作为主存储",
        "path": path,
        "project": "demo",
        "date": "2026-07-04",
        "summary": "保留 Obsidian Markdown 作为主存储 context: 用户需要中文可读",
        "terms": ["obsidian", "markdown", "中文", "主存储"],
    }


if __name__ == "__main__":
    unittest.main()
