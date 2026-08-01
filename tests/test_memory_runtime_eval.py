import json
import copy
import os
import subprocess
import sys
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
FIXTURE_DIR = os.path.join(REPO_ROOT, "tests", "fixtures", "memory_runtime")
sys.path.insert(0, SCRIPTS_DIR)


class MemoryRuntimeEvaluationTests(unittest.TestCase):
    def test_versioned_fixture_meets_all_phase_b_thresholds(self):
        from evaluate_memory_runtime import evaluate_fixture_dir, report_passes

        report = evaluate_fixture_dir(FIXTURE_DIR)

        self.assertTrue(report_passes(report), report)
        self.assertGreaterEqual(report["precision_at_k"], 0.85)
        self.assertGreaterEqual(report["critical_error_recall"], 0.80)
        self.assertLessEqual(report["irrelevant_trigger_rate"], 0.10)
        self.assertEqual(report["candidate_leaks"], 0)
        self.assertEqual(report["assistant_source_acceptances"], 0)
        self.assertEqual(report["duplicate_memories"], 0)
        self.assertEqual(report["deleted_residuals"], 0)
        self.assertLessEqual(report["no_trigger_p95_ms"], 100)
        self.assertLessEqual(report["recall_p95_ms"], 500)
        self.assertLessEqual(report["graph_scale_p95_ms"], 500)
        self.assertEqual(report["graph_scale_nodes"], 1600)
        self.assertEqual(report["graph_scale_edges"], 6600)
        self.assertLessEqual(report["max_estimated_tokens"], 1500)
        self.assertLessEqual(report["long_task"]["injection_count"], 19)
        self.assertEqual(report["case_failures"], [])

    def test_long_task_simulation_refreshes_new_and_changed_memory(self):
        from evaluate_memory_runtime import load_fixtures, simulate_long_task

        fixtures = load_fixtures(FIXTURE_DIR)
        result = simulate_long_task(fixtures)

        self.assertEqual(result["message_count"], 100)
        self.assertTrue(result["new_memory_seen"])
        self.assertTrue(result["changed_revision_seen"])
        self.assertEqual(result["deleted_residuals"], 0)
        self.assertLess(result["injection_count"], 20)
        self.assertGreater(result["silent_count"], result["injection_count"])

    def test_evaluator_subset_rebuilds_a_strict_generation_bound_graph(self):
        from evaluate_memory_runtime import _index_for_ids, load_fixtures
        from memory_graph import validate_memory_graph

        fixtures = load_fixtures(FIXTURE_DIR)
        by_id = {
            unit["id"]: copy.deepcopy(unit)
            for unit in fixtures["index"]["units"]
        }
        selected = [
            "decision:formal-index",
            "workflow:pensive",
        ]

        runtime_index = _index_for_ids(
            by_id,
            selected,
            fixtures["graph"],
        )
        valid = False
        try:
            validate_memory_graph(
                runtime_index.get("_graph"),
                runtime_index.get("units"),
                allow_legacy=False,
                expected_generation_id=runtime_index.get("generation_id", ""),
            )
            valid = bool(runtime_index.get("_graph_validated"))
        except (KeyError, TypeError, ValueError):
            valid = False

        self.assertTrue(valid)

    def test_evaluator_revision_change_rebinds_graph_node_and_edge_evidence(self):
        from evaluate_memory_runtime import _index_for_ids, load_fixtures
        from memory_graph import validate_memory_graph
        from memory_schema import memory_revision

        fixtures = load_fixtures(FIXTURE_DIR)
        by_id = {
            unit["id"]: copy.deepcopy(unit)
            for unit in fixtures["index"]["units"]
        }
        changed = by_id["workflow:pensive"]
        changed["summary"] = changed["summary"] + "，并重新绑定评估图版本"
        changed["revision"] = memory_revision(changed)

        runtime_index = _index_for_ids(
            by_id,
            ["workflow:pensive"],
            fixtures["graph"],
        )
        valid = False
        try:
            validate_memory_graph(
                runtime_index.get("_graph"),
                runtime_index.get("units"),
                allow_legacy=False,
                expected_generation_id=runtime_index.get("generation_id", ""),
            )
            graph_node = next(
                node
                for node in runtime_index["_graph"]["nodes"]
                if node["id"] == changed["id"]
            )
            valid = graph_node["revision"] == changed["revision"]
        except (KeyError, StopIteration, TypeError, ValueError):
            valid = False

        self.assertTrue(valid)

    def test_report_gate_rejects_each_hard_metric_regression(self):
        from evaluate_memory_runtime import report_passes

        passing = {
            "precision_at_k": 0.85,
            "critical_error_recall": 0.80,
            "irrelevant_trigger_rate": 0.10,
            "candidate_leaks": 0,
            "assistant_source_acceptances": 0,
            "duplicate_memories": 0,
            "deleted_residuals": 0,
            "no_trigger_p95_ms": 100,
            "recall_p95_ms": 500,
            "graph_scale_p95_ms": 500,
            "max_estimated_tokens": 1500,
            "long_task": {"injection_count": 19},
            "case_failures": [],
        }
        regressions = {
            "precision_at_k": 0.849,
            "critical_error_recall": 0.799,
            "irrelevant_trigger_rate": 0.101,
            "candidate_leaks": 1,
            "assistant_source_acceptances": 1,
            "duplicate_memories": 1,
            "deleted_residuals": 1,
            "no_trigger_p95_ms": 100.1,
            "recall_p95_ms": 500.1,
            "graph_scale_p95_ms": 500.1,
            "max_estimated_tokens": 1501,
            "long_task": {"injection_count": 20},
            "case_failures": ["missing-required-memory"],
        }
        self.assertTrue(report_passes(passing))
        for key, value in regressions.items():
            with self.subTest(key=key):
                report = dict(passing)
                report[key] = value
                self.assertFalse(report_passes(report))

    def test_fixture_contract_rejects_case_erosion_and_unknown_cases(self):
        from evaluate_memory_runtime import load_fixtures, validate_case_manifest

        cases = load_fixtures(FIXTURE_DIR)["cases"]
        mutations = {}

        missing_retrieval = copy.deepcopy(cases)
        missing_retrieval["retrieval_cases"].pop()
        mutations["missing-retrieval"] = missing_retrieval

        duplicate_retrieval = copy.deepcopy(cases)
        duplicate_retrieval["retrieval_cases"].append(
            copy.deepcopy(duplicate_retrieval["retrieval_cases"][0])
        )
        mutations["duplicate-retrieval"] = duplicate_retrieval

        unknown_trigger = copy.deepcopy(cases)
        unknown_trigger["trigger_cases"].append(
            {
                "id": "unknown-trigger",
                "prompt": "unknown",
                "expected_trigger": "",
                "irrelevant": True,
            }
        )
        mutations["unknown-trigger"] = unknown_trigger

        missing_assertion = copy.deepcopy(cases)
        missing_assertion["retrieval_cases"][0].pop("allowed_ids")
        mutations["missing-assertion"] = missing_assertion

        no_critical_denominator = copy.deepcopy(cases)
        for case in no_critical_denominator["retrieval_cases"]:
            case["critical_error_ids"] = []
        mutations["no-critical-denominator"] = no_critical_denominator

        no_irrelevant_denominator = copy.deepcopy(cases)
        for case in no_irrelevant_denominator["trigger_cases"]:
            case["irrelevant"] = False
        mutations["no-irrelevant-denominator"] = no_irrelevant_denominator

        for label, mutated in mutations.items():
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    validate_case_manifest(mutated)

    def test_fixture_contract_rejects_replaced_assertions_with_preserved_case_ids(
        self,
    ):
        from evaluate_memory_runtime import load_fixtures, validate_case_manifest

        weakened = copy.deepcopy(load_fixtures(FIXTURE_DIR)["cases"])
        for case in weakened["retrieval_cases"]:
            case["allowed_ids"] = ["anything"]
            case["required_ids"] = []
            case["forbidden_ids"] = []
            case["critical_error_ids"] = ["anything"]
            case["candidate_lure_ids"] = []
        for index, case in enumerate(weakened["trigger_cases"]):
            case["expected_trigger"] = ""
            case["irrelevant"] = index == 0

        with self.assertRaisesRegex(ValueError, "assertion contract"):
            validate_case_manifest(weakened)

    def test_cli_emits_json_report_and_success_exit(self):
        script = os.path.join(SCRIPTS_DIR, "evaluate_memory_runtime.py")

        completed = subprocess.run(
            [sys.executable, script, "--fixtures", FIXTURE_DIR],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["fixture_schema_version"], "1.4")
        self.assertEqual(report["runtime_schema_version"], "2.0")
        self.assertNotIn("Traceback", completed.stderr)


if __name__ == "__main__":
    unittest.main()
