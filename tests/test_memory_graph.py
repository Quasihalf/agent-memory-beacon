import os
import sys
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from memory_graph import (
    add_graph_edge,
    analyze_memory_graph,
    graph_evidence,
    graph_node,
    semantic_memory_paths,
    validate_memory_graph,
)


REVISION_A = "a" * 64
REVISION_B = "b" * 64
REVISION_C = "c" * 64
GENERATION_A = "generation-a"
GENERATION_B = "generation-b"


class MemoryGraphTests(unittest.TestCase):
    def test_graph_requires_every_runtime_memory_node(self):
        graph = graph_document(
            [memory_node("decision:present", REVISION_A)],
            [],
        )
        units = [
            memory_unit("decision:present", REVISION_A),
            memory_unit("workflow:missing", REVISION_B, memory_type="workflow"),
        ]

        quality = analyze_memory_graph(graph, units)

        self.assertEqual(quality["missing_memory_nodes"], 1)
        self.assertFalse(quality["valid"])
        with self.assertRaisesRegex(ValueError, "missing_memory_nodes=1"):
            validate_memory_graph(graph, units, allow_legacy=False)

    def test_graph_rejects_malformed_top_level_containers(self):
        for schema_version in ("2.0", "3.0"):
            with self.subTest(schema_version=schema_version):
                graph = {
                    "schema_version": schema_version,
                    "generated_by": "test",
                    "generated_at": "2026-07-26T10:00:00+08:00",
                    "generation_id": GENERATION_A,
                    "nodes": [],
                    "edges": {"not-edge": True},
                }

                quality = analyze_memory_graph(graph)

                self.assertEqual(quality["invalid_graph_shape"], 1)
                self.assertFalse(quality["valid"])
                with self.assertRaisesRegex(ValueError, "invalid_graph_shape=1"):
                    validate_memory_graph(graph)

    def test_graph_generation_must_match_recall_index_generation(self):
        graph = graph_document([], [], generation_id=GENERATION_A)

        quality = analyze_memory_graph(
            graph,
            [],
            expected_generation_id=GENERATION_B,
        )

        self.assertEqual(quality["generation_mismatch"], 1)
        self.assertFalse(quality["valid"])
        with self.assertRaisesRegex(ValueError, "generation_mismatch=1"):
            validate_memory_graph(
                graph,
                [],
                allow_legacy=False,
                expected_generation_id=GENERATION_B,
            )

    def test_stale_source_revision_invalidates_v3_graph(self):
        nodes = [
            memory_node("decision:source", REVISION_A),
            memory_node("workflow:target", REVISION_B, kind="workflow"),
        ]
        graph = graph_document(
            nodes,
            [
                graph_edge(
                    "decision:source",
                    "supports",
                    "workflow:target",
                    REVISION_A,
                )
            ],
        )
        units = [
            memory_unit("decision:source", REVISION_C),
            memory_unit("workflow:target", REVISION_B, memory_type="workflow"),
        ]

        quality = analyze_memory_graph(graph, units)

        self.assertEqual(quality["stale_revision_edges"], 1)
        self.assertFalse(quality["valid"])
        with self.assertRaisesRegex(ValueError, "stale_revision_edges=1"):
            validate_memory_graph(graph, units, allow_legacy=False)

    def test_semantic_edge_must_be_declared_by_authoritative_source_unit(self):
        source = memory_unit("decision:source", REVISION_A)
        target = memory_unit(
            "workflow:target",
            REVISION_B,
            memory_type="workflow",
        )
        graph = graph_document(
            [
                memory_node(source["id"], source["revision"]),
                memory_node(
                    target["id"],
                    target["revision"],
                    kind=target["type"],
                ),
            ],
            [
                graph_edge(
                    source["id"],
                    "supports",
                    target["id"],
                    source["revision"],
                )
            ],
        )

        quality = analyze_memory_graph(graph, [source, target])

        self.assertEqual(quality.get("undeclared_semantic_edges"), 1)
        with self.assertRaisesRegex(ValueError, "undeclared_semantic_edges=1"):
            validate_memory_graph(
                graph,
                [source, target],
                allow_legacy=False,
            )

    def test_every_edge_requires_source_revision_binding(self):
        note = graph_node(
            "note:demo",
            "note",
            "session",
            "Demo note",
            path="01-Projects/demo/Memory/sessions/demo",
            project="demo",
            revision=REVISION_A,
            source_refs=["note:demo"],
        )
        project = graph_node(
            "project:demo",
            "project",
            "project",
            "demo",
            project="demo",
            source_refs=["note:demo"],
        )
        graph = graph_document(
            [note, project],
            [
                {
                    "source": note["id"],
                    "target": project["id"],
                    "relation": "belongs_to",
                    "confidence": 1.0,
                    "evidence": [
                        graph_evidence(
                            "note:demo",
                            observed_at="2026-07-26",
                            derivation="note-frontmatter",
                        )
                    ],
                }
            ],
        )

        quality = analyze_memory_graph(graph, [])

        self.assertEqual(quality["missing_evidence"], 1)
        with self.assertRaisesRegex(ValueError, "missing_evidence=1"):
            validate_memory_graph(graph, [], allow_legacy=False)

    def test_edge_revision_must_match_source_node_without_unit_snapshot(self):
        graph = graph_document(
            [
                memory_node("decision:source", REVISION_A),
                memory_node("workflow:target", REVISION_B, kind="workflow"),
            ],
            [
                graph_edge(
                    "decision:source",
                    "supports",
                    "workflow:target",
                    REVISION_C,
                )
            ],
        )

        quality = analyze_memory_graph(graph)

        self.assertEqual(quality["stale_revision_edges"], 1)
        self.assertFalse(quality["valid"])
        with self.assertRaisesRegex(ValueError, "stale_revision_edges=1"):
            validate_memory_graph(graph, allow_legacy=False)

    def test_edge_source_ref_must_be_bound_to_source_node(self):
        graph = graph_document(
            [
                memory_node("decision:source", REVISION_A),
                memory_node("workflow:target", REVISION_B, kind="workflow"),
            ],
            [
                {
                    **graph_edge(
                        "decision:source",
                        "supports",
                        "workflow:target",
                        REVISION_A,
                    ),
                    "evidence": [
                        graph_evidence(
                            "session:unbound",
                            source_revision=REVISION_A,
                            observed_at="2026-07-26",
                            derivation="formal-record",
                        )
                    ],
                }
            ],
        )

        quality = analyze_memory_graph(graph)

        self.assertEqual(quality["unbound_evidence"], 1)
        self.assertFalse(quality["valid"])
        with self.assertRaisesRegex(ValueError, "unbound_evidence=1"):
            validate_memory_graph(graph, allow_legacy=False)

    def test_stale_or_orphaned_resolved_memory_nodes_invalidate_v3_graph(self):
        graph = graph_document(
            [
                memory_node("decision:stale", REVISION_A),
                memory_node("decision:orphaned", REVISION_B),
            ],
            [],
        )
        units = [memory_unit("decision:stale", REVISION_C)]

        quality = analyze_memory_graph(graph, units)

        self.assertEqual(quality["stale_revision_nodes"], 1)
        self.assertEqual(quality["orphaned_memory_nodes"], 1)
        self.assertFalse(quality["valid"])
        with self.assertRaisesRegex(
            ValueError,
            "stale_revision_nodes=1, orphaned_memory_nodes=1",
        ):
            validate_memory_graph(graph, units, allow_legacy=False)

    def test_invalid_relation_domain_and_range_are_rejected(self):
        nodes = [
            graph_node(
                "note:demo",
                "note",
                "session",
                "Demo note",
                path="01-Projects/demo/Memory/sessions/demo",
                project="demo",
                source_refs=["note:demo"],
            ),
            graph_node(
                "project:demo",
                "project",
                "project",
                "demo",
                project="demo",
                source_refs=["note:demo"],
            ),
        ]
        graph = graph_document(
            nodes,
            [
                {
                    "source": "project:demo",
                    "target": "note:demo",
                    "relation": "recorded_in",
                    "confidence": 1.0,
                    "evidence": [
                        graph_evidence(
                            "note:demo",
                            observed_at="2026-07-26",
                            derivation="test",
                        )
                    ],
                }
            ],
        )

        quality = analyze_memory_graph(graph)

        self.assertEqual(quality["invalid_edges"], 1)
        self.assertFalse(quality["valid"])
        with self.assertRaisesRegex(ValueError, "invalid_edges=1"):
            validate_memory_graph(graph, allow_legacy=False)

    def test_legacy_v2_graph_is_read_only_compatible(self):
        graph = {
            "schema_version": "2.0",
            "nodes": [
                {
                    "id": "decision:legacy",
                    "type": "decision",
                    "label": "Legacy decision",
                    "path": "01-Projects/demo/Memory/decisions",
                    "project": "demo",
                }
            ],
            "edges": [],
        }

        quality = validate_memory_graph(graph)

        self.assertTrue(quality["legacy"])
        self.assertTrue(quality["valid"])
        self.assertEqual(
            semantic_memory_paths(
                graph,
                ["decision:legacy"],
                [memory_unit("decision:legacy", REVISION_A)],
            ),
            {},
        )
        with self.assertRaisesRegex(ValueError, "schema must be 3.0"):
            validate_memory_graph(graph, allow_legacy=False)

    def test_semantic_paths_can_traverse_a_relation_in_reverse(self):
        source = memory_unit("decision:source", REVISION_A)
        target = memory_unit(
            "workflow:target",
            REVISION_B,
            memory_type="workflow",
        )
        source["operationalized_as"] = [target["id"]]
        graph = graph_document(
            [
                memory_node(source["id"], source["revision"]),
                memory_node(
                    target["id"],
                    target["revision"],
                    kind=target["type"],
                ),
            ],
            [
                graph_edge(
                    source["id"],
                    "operationalized_as",
                    target["id"],
                    source["revision"],
                )
            ],
        )

        paths = semantic_memory_paths(
            graph,
            [target["id"]],
            [source, target],
        )

        self.assertEqual(paths[source["id"]]["seed"], target["id"])
        self.assertEqual(
            paths[source["id"]]["path"],
            [
                {
                    "source": source["id"],
                    "relation": "operationalized_as",
                    "target": target["id"],
                    "direction": "reverse",
                    "confidence": 1.0,
                }
            ],
        )

    def test_semantic_paths_respect_edge_confidence(self):
        source = memory_unit("decision:source", REVISION_A)
        low = memory_unit("workflow:low", REVISION_B, memory_type="workflow")
        partial = memory_unit(
            "workflow:partial",
            REVISION_C,
            memory_type="workflow",
        )
        full_revision = "d" * 64
        full = memory_unit(
            "workflow:full",
            full_revision,
            memory_type="workflow",
        )
        source["supports"] = [low["id"], partial["id"], full["id"]]
        graph = graph_document(
            [
                memory_node(source["id"], source["revision"]),
                memory_node(low["id"], low["revision"], kind=low["type"]),
                memory_node(
                    partial["id"],
                    partial["revision"],
                    kind=partial["type"],
                ),
                memory_node(full["id"], full["revision"], kind=full["type"]),
            ],
            [
                {
                    **graph_edge(
                        source["id"],
                        "supports",
                        low["id"],
                        source["revision"],
                    ),
                    "confidence": 0.49,
                },
                {
                    **graph_edge(
                        source["id"],
                        "supports",
                        partial["id"],
                        source["revision"],
                    ),
                    "confidence": 0.75,
                },
                graph_edge(
                    source["id"],
                    "supports",
                    full["id"],
                    source["revision"],
                ),
            ],
        )

        paths = semantic_memory_paths(
            graph,
            [source["id"]],
            [source, low, partial, full],
        )

        self.assertNotIn(low["id"], paths)
        self.assertIn(partial["id"], paths)
        self.assertIn(full["id"], paths)
        self.assertLess(paths[partial["id"]]["score"], paths[full["id"]]["score"])
        self.assertEqual(paths[partial["id"]]["confidence"], 0.75)
        self.assertEqual(
            paths[partial["id"]]["path"][0]["confidence"],
            0.75,
        )

    def test_duplicate_edge_evidence_merges_deterministically(self):
        nodes = {
            "decision:source": memory_node("decision:source", REVISION_A),
            "workflow:target": memory_node(
                "workflow:target",
                REVISION_B,
                kind="workflow",
            ),
        }
        edges = {}
        evidence_late = graph_evidence(
            "session:z",
            source_revision=REVISION_A,
            observed_at="2026-07-26",
            derivation="formal-record",
        )
        evidence_early = graph_evidence(
            "session:a",
            source_revision=REVISION_A,
            observed_at="2026-07-25",
            derivation="formal-record",
        )

        add_graph_edge(
            edges,
            nodes,
            "decision:source",
            "workflow:target",
            "supports",
            evidence_late,
            confidence=0.8,
        )
        add_graph_edge(
            edges,
            nodes,
            "decision:source",
            "workflow:target",
            "supports",
            evidence_early,
            confidence=0.9,
        )
        add_graph_edge(
            edges,
            nodes,
            "decision:source",
            "workflow:target",
            "supports",
            evidence_late,
            confidence=0.7,
        )

        edge = edges[
            ("decision:source", "supports", "workflow:target")
        ]
        self.assertEqual(edge["confidence"], 0.9)
        self.assertEqual(edge["evidence"], [evidence_early, evidence_late])

    def test_edge_insertion_rejects_malformed_evidence(self):
        nodes = {
            "decision:source": memory_node("decision:source", REVISION_A),
            "workflow:target": memory_node(
                "workflow:target",
                REVISION_B,
                kind="workflow",
            ),
        }
        malformed = {
            "source_ref": "",
            "source_revision": "not-a-revision",
            "observed_at": "2026-07-26",
            "derivation": "formal-record",
        }

        with self.assertRaisesRegex(ValueError, "evidence is invalid"):
            add_graph_edge(
                {},
                nodes,
                "decision:source",
                "workflow:target",
                "supports",
                malformed,
            )


def memory_node(memory_id, revision, *, kind="decision"):
    return graph_node(
        memory_id,
        "memory",
        kind,
        memory_id,
        path="01-Projects/demo/Memory/formal",
        project="demo",
        date="2026-07-26",
        revision=revision,
        source_refs=["session:test"],
    )


def memory_unit(memory_id, revision, *, memory_type="decision"):
    return {
        "id": memory_id,
        "type": memory_type,
        "revision": revision,
    }


def graph_edge(source, relation, target, revision):
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "confidence": 1.0,
        "evidence": [
            graph_evidence(
                "session:test",
                source_revision=revision,
                observed_at="2026-07-26",
                derivation="formal-record",
            )
        ],
    }


def graph_document(nodes, edges, *, generation_id=GENERATION_A):
    return {
        "schema_version": "3.0",
        "generated_by": "test",
        "generated_at": "2026-07-26T10:00:00+08:00",
        "generation_id": generation_id,
        "nodes": nodes,
        "edges": edges,
    }


if __name__ == "__main__":
    unittest.main()
