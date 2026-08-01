import json
import os
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from graph_projection import (
    build_graph_projection_plan,
    sync_graph_projection,
)


REVISION = "a" * 64
GENERATION_ID = "b" * 64


class GraphProjectionTests(unittest.TestCase):
    def test_projection_uses_experience_hubs_instead_of_aggregate_starbursts(self):
        with tempfile.TemporaryDirectory() as vault:
            project_map = os.path.join(
                vault,
                "03-Maps",
                "Projects",
                "demo.md",
            )
            os.makedirs(os.path.dirname(project_map), exist_ok=True)
            with open(project_map, "w", encoding="utf-8") as handle:
                handle.write("# demo\n")
            graph = sample_graph()

            plan = build_graph_projection_plan(vault, graph)

            self.assertEqual(len(plan["notes"]), 3)
            decision_path = plan["node_targets"]["decision-demo"]
            error_path = plan["node_targets"]["error-demo"]
            experience_path = plan["node_targets"]["experience:demo"]
            self.assertIn("/Decisions/", decision_path)
            self.assertIn("/Errors/", error_path)
            self.assertIn("/Experiences/", experience_path)
            self.assertIn("使用语义图谱投影", decision_path)
            self.assertIn("demo 经验束", experience_path)

            decision_content = plan["notes"][decision_path]
            experience_content = plan["notes"][experience_path]
            self.assertIn(f"[[{experience_path}]]", decision_content)
            self.assertNotIn(
                "[[01-Projects/demo/Memory/decisions]]",
                decision_content,
            )
            self.assertNotIn(
                "[[03-Maps/Projects/demo]]",
                decision_content,
            )
            self.assertIn("[[03-Maps/Projects/demo]]", experience_content)
            self.assertIn(
                "`01-Projects/demo/Memory/decisions`",
                decision_content,
            )

    def test_projection_omits_missing_project_map_link(self):
        with tempfile.TemporaryDirectory() as vault:
            plan = build_graph_projection_plan(vault, sample_graph())

            experience_path = plan["node_targets"]["experience:demo"]
            experience_content = plan["notes"][experience_path]
            self.assertNotIn(
                "[[03-Maps/Projects/demo]]",
                experience_content,
            )
            self.assertEqual(plan["edge_count"], 2)

    def test_sync_removes_only_manifest_owned_stale_notes(self):
        with tempfile.TemporaryDirectory() as vault:
            first = sync_graph_projection(vault, sample_graph())
            root = first["root"]
            user_note = os.path.join(root, "user-note.md")
            with open(user_note, "w", encoding="utf-8") as handle:
                handle.write("# keep me\n")
            stale_path = os.path.join(
                vault,
                first["node_targets"]["error-demo"] + ".md",
            )
            self.assertTrue(os.path.exists(stale_path))

            graph = sample_graph()
            graph["nodes"] = [
                node
                for node in graph["nodes"]
                if node["id"] != "error-demo"
            ]
            graph["edges"] = [
                edge
                for edge in graph["edges"]
                if edge["source"] != "error-demo"
                and edge["target"] != "error-demo"
            ]
            second = sync_graph_projection(vault, graph)

            self.assertEqual(second["removed"], 1)
            self.assertFalse(os.path.exists(stale_path))
            self.assertTrue(os.path.exists(user_note))
            with open(second["manifest_path"], encoding="utf-8") as handle:
                manifest = json.load(handle)
            self.assertNotIn(
                first["node_targets"]["error-demo"] + ".md",
                manifest["files"],
            )

    def test_projection_enforces_configured_node_limit(self):
        with tempfile.TemporaryDirectory() as vault:
            graph = sample_graph()

            with self.assertRaisesRegex(
                ValueError,
                "graph projection exceeds node limit",
            ):
                build_graph_projection_plan(
                    vault,
                    graph,
                    {"max_nodes": 2},
                )


def sample_graph():
    nodes = [
        graph_node(
            "decision-demo",
            "memory",
            "decision",
            "使用语义图谱投影",
            "01-Projects/demo/Memory/decisions",
        ),
        graph_node(
            "error-demo",
            "memory",
            "error",
            "修复投影路径并验证",
            "01-Projects/demo/Memory/pitfalls",
        ),
        graph_node(
            "experience:demo",
            "experience",
            "session-bundle",
            "demo: session-1",
            "",
        ),
        graph_node(
            "note:01-Projects/demo/Memory/decisions",
            "note",
            "decisions",
            "Decisions",
            "01-Projects/demo/Memory/decisions",
        ),
        graph_node(
            "note:01-Projects/demo/Memory/pitfalls",
            "note",
            "pitfalls",
            "Pitfalls",
            "01-Projects/demo/Memory/pitfalls",
        ),
        graph_node(
            "project:demo",
            "project",
            "project",
            "demo",
            "01-Projects/demo",
        ),
    ]
    edges = [
        graph_edge("decision-demo", "experience:demo", "part_of_experience"),
        graph_edge("error-demo", "experience:demo", "part_of_experience"),
        graph_edge("decision-demo", "project:demo", "belongs_to"),
        graph_edge("error-demo", "project:demo", "belongs_to"),
        graph_edge(
            "decision-demo",
            "note:01-Projects/demo/Memory/decisions",
            "recorded_in",
        ),
        graph_edge(
            "error-demo",
            "note:01-Projects/demo/Memory/pitfalls",
            "recorded_in",
        ),
        graph_edge("experience:demo", "project:demo", "belongs_to"),
    ]
    return {
        "schema_version": "3.0",
        "generation_id": GENERATION_ID,
        "nodes": nodes,
        "edges": edges,
    }


def graph_node(node_id, node_type, kind, label, path):
    return {
        "id": node_id,
        "type": node_type,
        "kind": kind,
        "label": label,
        "path": path,
        "project": "demo",
        "date": "2026-07-31",
        "revision": REVISION,
        "source_refs": ["session:session-1"],
        "resolved": True,
    }


def graph_edge(source, target, relation):
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "confidence": 1.0,
        "evidence": [
            {
                "source_ref": "session:session-1",
                "source_revision": REVISION,
                "observed_at": "2026-07-31",
                "derivation": "test",
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
