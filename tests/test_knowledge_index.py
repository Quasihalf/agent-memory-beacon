import json
import os
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from knowledge_index import rebuild_vault_knowledge_indexes


class KnowledgeIndexTests(unittest.TestCase):
    def test_rebuild_writes_recall_index_and_memory_graph(self):
        with tempfile.TemporaryDirectory() as vault:
            write_fixture_vault(vault)

            result = rebuild_vault_knowledge_indexes({"vault_path": vault})

            output_dir = os.path.join(vault, "05-Agent-Memory")
            recall_path = os.path.join(output_dir, "recall-index.json")
            graph_path = os.path.join(output_dir, "memory-graph.json")
            context_path = os.path.join(output_dir, "recall-context.md")

            self.assertTrue(os.path.exists(recall_path))
            self.assertTrue(os.path.exists(graph_path))
            self.assertTrue(os.path.exists(context_path))
            self.assertGreater(result["recall_units"], 0)
            self.assertGreater(result["graph_nodes"], 0)
            self.assertGreater(result["graph_edges"], 0)

            recall = load_json(recall_path)
            graph = load_json(graph_path)
            unit_types = {unit["type"] for unit in recall["units"]}
            self.assertIn("decision", unit_types)
            self.assertIn("error", unit_types)
            self.assertIn("personal-memory", unit_types)
            self.assertIn("obsidian", recall["terms"])
            self.assertFalse(
                any(unit["summary"].startswith("| 2026-") for unit in recall["units"])
            )

            node_types = {node["type"] for node in graph["nodes"]}
            edge_relations = {edge["relation"] for edge in graph["edges"]}
            self.assertIn("project", node_types)
            self.assertIn("decision", node_types)
            self.assertIn("belongs_to", edge_relations)
            self.assertIn("recorded_in", edge_relations)


def write_fixture_vault(vault):
    session_path = os.path.join(
        vault,
        "01-Projects/demo/Memory/sessions/2026-07-04-obsidian-recall.md",
    )
    personal_path = os.path.join(vault, "05-Agent-Memory/personal-memory.md")
    os.makedirs(os.path.dirname(session_path), exist_ok=True)
    os.makedirs(os.path.dirname(personal_path), exist_ok=True)
    write_text(
        session_path,
        """---
session_id: sess-1
date: '2026-07-04'
project: demo
ai_title: Obsidian 中文召回
summary_type: session
decisions_made:
- text: 保留 Obsidian Markdown 作为主存储
  context: 用户需要中文可读、可手动检查的长期记忆
errors_encountered:
- type: path-filesystem
  resolution: 修复 Obsidian 本地路径误链接
---

# Obsidian 中文召回

## Related

- [[05-Agent-Memory/personal-memory|Personal Memory]]
""",
    )
    write_text(
        personal_path,
        """---
title: Personal Memory
generated_by: memory_judge.py
---

# Personal Memory

## 用户偏好: 中文说明

- id: `preference-demo`
- type: `preference`
- project: [[01-Projects/demo/Memory/decisions|demo]]
- memory: 用户偏好中文解释和清晰步骤

## 项目规则: | 2026-07-04 | demo |

- id: `bad-table-memory`
- type: `project_rule`
- project: [[01-Projects/demo/Memory/decisions|demo]]
- memory: | 2026-07-04 | demo | 这是旧索引表格行，不应该进入 recall |
""",
    )


def write_text(path, content):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


if __name__ == "__main__":
    unittest.main()
