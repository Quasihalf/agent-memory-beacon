import json
import math
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from evaluate_memory_comparison import (
    ComparisonContractError,
    compare_memory_reports,
    main,
    probe_codex_memory,
    score_arm_report,
)


class MemoryComparisonTests(unittest.TestCase):
    def test_disabled_empty_codex_memory_is_na_without_fake_loss(self):
        beacon = arm_report("beacon")
        probe = codex_probe(
            available=False,
            feature_enabled=False,
            stage1_output_count=0,
            unavailable_reasons=["feature_disabled", "empty_memory_store"],
        )

        result = compare_memory_reports(beacon, None, probe)

        self.assertEqual(result["status"], "N/A")
        self.assertFalse(result["claim_allowed"])
        self.assertIsNotNone(result["beacon_score"])
        self.assertIsNone(result["codex_memory_score"])
        self.assertIsNone(result["score_delta"])
        self.assertEqual(
            result["unavailable_reasons"],
            ["empty_memory_store", "feature_disabled"],
        )
        self.assertNotIn("winner", result)

    def test_claim_gate_requires_85_and_exact_15_point_lead(self):
        beacon = arm_report(
            "beacon",
            precision_at_k=1.0,
            critical_error_recall=1.0,
            irrelevant_trigger_rate=0.0,
            contamination_count=0,
            long_task_freshness_rate=2 / 3,
            recall_p95_ms=500,
            max_estimated_tokens=1500,
        )
        codex = arm_report(
            "codex_memory",
            precision_at_k=1.0,
            critical_error_recall=0.8,
            irrelevant_trigger_rate=0.0,
            contamination_count=0,
            long_task_freshness_rate=0.0,
            recall_p95_ms=500,
            max_estimated_tokens=1500,
        )

        exact = compare_memory_reports(beacon, codex, codex_probe())

        self.assertEqual(exact["beacon_score"], 85.0)
        self.assertEqual(exact["codex_memory_score"], 70.0)
        self.assertEqual(exact["score_delta"], 15.0)
        self.assertTrue(exact["claim_allowed"])
        self.assertEqual(exact["verdict"], "beacon_exceeds_codex_memory")

        below_absolute = arm_report(
            "beacon",
            **{
                **beacon["metrics"],
                "long_task_freshness_rate": 0.6665,
            },
        )
        self.assertFalse(
            compare_memory_reports(
                below_absolute,
                codex,
                codex_probe(),
            )["claim_allowed"]
        )

        below_delta = arm_report(
            "codex_memory",
            **{
                **codex["metrics"],
                "critical_error_recall": 0.8001,
            },
        )
        self.assertFalse(
            compare_memory_reports(
                beacon,
                below_delta,
                codex_probe(),
            )["claim_allowed"]
        )

    def test_score_breakdown_uses_only_approved_dimensions(self):
        result = score_arm_report(arm_report("beacon"))

        self.assertEqual(result["total"], 100.0)
        self.assertEqual(
            result["dimensions"],
            {
                "relevance_accuracy": 30.0,
                "critical_error_recall": 25.0,
                "pollution_control": 20.0,
                "long_task_freshness": 15.0,
                "latency_and_context_cost": 10.0,
            },
        )

    def test_comparison_rejects_version_fixture_and_metric_drift(self):
        beacon = arm_report("beacon")
        codex = arm_report("codex_memory")
        probe = codex_probe()

        mutations = []
        wrong_version = dict(codex)
        wrong_version["codex_version"] = "0.145.0"
        mutations.append(("same Codex version", wrong_version, probe))

        wrong_fixture = dict(codex)
        wrong_fixture["fixture_sha256"] = "b" * 64
        mutations.append(("same fixture", wrong_fixture, probe))

        wrong_probe = dict(probe)
        wrong_probe["codex_version"] = "0.145.0"
        mutations.append(("probe version", codex, wrong_probe))

        for message, changed_codex, changed_probe in mutations:
            with self.subTest(message=message), self.assertRaisesRegex(
                ComparisonContractError,
                message,
            ):
                compare_memory_reports(beacon, changed_codex, changed_probe)

        for value in (-0.1, 1.1, math.nan, math.inf):
            with self.subTest(metric=value):
                invalid = arm_report("beacon")
                invalid["metrics"]["precision_at_k"] = value
                with self.assertRaises(ComparisonContractError):
                    score_arm_report(invalid)

    def test_probe_separates_feature_state_from_database_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = os.path.join(tmp, "memories.sqlite")
            create_memory_database(database, stage1_rows=0)
            calls = []

            def runner(args, **kwargs):
                calls.append((list(args), dict(kwargs)))
                if args[-1] == "--version":
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        stdout="codex-cli 0.144.1\n",
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout="memories experimental false\n",
                    stderr="",
                )

            result = probe_codex_memory(
                codex_bin="/tmp/fake-codex",
                memory_db=database,
                runner=runner,
            )

            self.assertEqual(result["codex_version"], "0.144.1")
            self.assertEqual(result["feature_maturity"], "experimental")
            self.assertFalse(result["feature_enabled"])
            self.assertEqual(result["job_count"], 0)
            self.assertEqual(result["stage1_output_count"], 0)
            self.assertFalse(result["available"])
            self.assertEqual(
                result["unavailable_reasons"],
                ["empty_memory_store", "feature_disabled"],
            )
            self.assertEqual(
                [item[0][1:] for item in calls],
                [["--version"], ["features", "list"]],
            )
            self.assertTrue(all(item[1]["shell"] is False for item in calls))

    def test_probe_contract_rejects_integer_feature_state(self):
        probe = codex_probe(
            available=False,
            feature_enabled=1,
            stage1_output_count=0,
            unavailable_reasons=["empty_memory_store"],
        )

        with self.assertRaisesRegex(
            ComparisonContractError,
            "feature_enabled",
        ):
            compare_memory_reports(arm_report("beacon"), None, probe)

    def test_cli_emits_valid_na_report_without_codex_arm(self):
        with tempfile.TemporaryDirectory() as tmp:
            beacon_path = os.path.join(tmp, "beacon.json")
            probe_path = os.path.join(tmp, "probe.json")
            write_json(beacon_path, arm_report("beacon"))
            write_json(
                probe_path,
                codex_probe(
                    available=False,
                    feature_enabled=False,
                    stage1_output_count=0,
                    unavailable_reasons=[
                        "feature_disabled",
                        "empty_memory_store",
                    ],
                ),
            )

            with redirect_stdout(StringIO()) as output:
                result = main(
                    [
                        "--beacon-report",
                        beacon_path,
                        "--codex-probe",
                        probe_path,
                    ]
                )

            self.assertEqual(result, 0)
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["status"], "N/A")
            self.assertFalse(payload["claim_allowed"])


def arm_report(arm, **metrics):
    defaults = {
        "precision_at_k": 1.0,
        "critical_error_recall": 1.0,
        "irrelevant_trigger_rate": 0.0,
        "contamination_count": 0,
        "long_task_freshness_rate": 1.0,
        "recall_p95_ms": 0,
        "max_estimated_tokens": 0,
    }
    defaults.update(metrics)
    return {
        "schema_version": "1.0",
        "arm": arm,
        "codex_version": "0.144.1",
        "fixture_id": "agent-memory-black-box-v1",
        "fixture_sha256": "a" * 64,
        "evidence_status": "valid",
        "metrics": defaults,
        "evidence_refs": ["fixture:agent-memory-black-box-v1"],
    }


def codex_probe(**overrides):
    result = {
        "schema_version": "1.0",
        "probe_type": "codex-memory-capability",
        "codex_version": "0.144.1",
        "feature_maturity": "experimental",
        "feature_enabled": True,
        "memory_store_exists": True,
        "job_count": 1,
        "stage1_output_count": 1,
        "available": True,
        "unavailable_reasons": [],
    }
    result.update(overrides)
    return result


def create_memory_database(path, stage1_rows=0):
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE jobs (kind TEXT, job_key TEXT)")
        connection.execute(
            "CREATE TABLE stage1_outputs (thread_id TEXT, raw_memory TEXT)"
        )
        for index in range(stage1_rows):
            connection.execute(
                "INSERT INTO stage1_outputs VALUES (?, ?)",
                (f"thread-{index}", "memory"),
            )
        connection.commit()
    finally:
        connection.close()


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
