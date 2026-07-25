import json
import os
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from memory_effectiveness import (
    EFFECTIVENESS_EVENT_ALLOWED_FIELDS,
    aggregate_events,
    build_exposure_event,
    build_feedback_event,
    classify_feedback,
    read_effectiveness_events,
    render_effectiveness_report,
    write_effectiveness_report,
)


class MemoryEffectivenessTests(unittest.TestCase):
    def setUp(self):
        self.memories = [
            {
                "id": "decision-demo",
                "revision": "a" * 64,
                "type": "decision",
                "retrieval_channels": ["structured", "lexical", "lexical"],
                "title": "不得进入账本",
                "summary": "正文同样不得进入账本",
            },
            {
                "id": "error-demo",
                "revision": "b" * 64,
                "type": "error",
                "retrieval_channels": ["temporal"],
            },
        ]

    def exposure(self, timestamp="2026-07-22T10:00:00+08:00"):
        return build_exposure_event(
            timestamp=timestamp,
            session_hash="sessionhash0123456789abcdef012345",
            trigger="topic_changed",
            memories=self.memories,
            duration_ms=12.5,
            estimated_tokens=180,
        )

    def test_exposure_is_stable_revision_bound_and_privacy_safe(self):
        first = self.exposure()
        second = self.exposure()

        self.assertEqual(first, second)
        self.assertEqual(first["event_kind"], "exposure")
        self.assertEqual(first["outcome"], "exposed")
        self.assertEqual(len(first["event_id"]), 64)
        self.assertEqual(
            set(first),
            EFFECTIVENESS_EVENT_ALLOWED_FIELDS,
        )
        serialized = json.dumps(first, ensure_ascii=False)
        self.assertNotIn("不得进入账本", serialized)
        self.assertNotIn("正文同样不得进入账本", serialized)
        self.assertNotIn("prompt", serialized.casefold())
        self.assertEqual(
            first["memories"][0],
            {
                "id": "decision-demo",
                "revision": "a" * 64,
                "type": "decision",
                "channels": ["lexical", "structured"],
            },
        )

    def test_event_identity_changes_when_revision_changes(self):
        first = self.exposure()
        self.memories[0]["revision"] = "c" * 64
        second = self.exposure()

        self.assertNotEqual(first["event_id"], second["event_id"])

    def test_feedback_classifier_is_conservative(self):
        self.assertEqual(classify_feedback("对的，继续"), ("accepted", 0.6))
        self.assertEqual(classify_feedback("不是，我说的是先检查源码"), ("corrected", 0.7))
        self.assertEqual(classify_feedback("开始另一个完全不同的项目"), ("unobserved", 0.0))
        self.assertEqual(
            classify_feedback("不是" + "很长的说明" * 80),
            ("unobserved", 0.0),
        )

    def test_feedback_closes_exact_exposure_without_copying_prompt(self):
        exposure = self.exposure()
        feedback = build_feedback_event(
            exposure,
            "不是，我说的是先检查源码",
            timestamp="2026-07-22T10:01:00+08:00",
        )

        self.assertEqual(feedback["event_kind"], "feedback")
        self.assertEqual(feedback["outcome"], "corrected")
        self.assertEqual(feedback["confidence"], 0.7)
        self.assertEqual(feedback["parent_event_id"], exposure["event_id"])
        self.assertEqual(feedback["memories"], exposure["memories"])
        self.assertNotIn("检查源码", json.dumps(feedback, ensure_ascii=False))

    def test_aggregation_deduplicates_events_and_separates_weak_signals(self):
        exposure = self.exposure()
        accepted = build_feedback_event(
            exposure,
            "对的，继续",
            timestamp="2026-07-22T10:01:00+08:00",
        )
        corrected = build_feedback_event(
            self.exposure("2026-07-22T11:00:00+08:00"),
            "不是，我说的是另一种方式",
            timestamp="2026-07-22T11:01:00+08:00",
        )

        aggregate = aggregate_events(
            [exposure, exposure, accepted, corrected]
        )

        self.assertEqual(aggregate["event_count"], 3)
        self.assertEqual(aggregate["exposure_count"], 1)
        self.assertEqual(aggregate["estimated_tokens"], 180)
        decision = aggregate["memories"]["decision-demo@" + "a" * 64]
        self.assertEqual(decision["exposures"], 1)
        self.assertEqual(decision["accepted"], 1)
        self.assertEqual(decision["corrected"], 1)
        self.assertEqual(decision["manual_helpful"], 0)
        self.assertEqual(decision["manual_misleading"], 0)

    def test_reader_ignores_malformed_duplicate_and_unknown_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "events.jsonl")
            exposure = self.exposure()
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(exposure, ensure_ascii=False) + "\n")
                handle.write(json.dumps(exposure, ensure_ascii=False) + "\n")
                handle.write("not-json\n")
                handle.write(json.dumps({"schema_version": "9.9"}) + "\n")

            events = read_effectiveness_events(path)

        self.assertEqual(events, [exposure])

    def test_report_is_human_readable_and_never_contains_memory_body(self):
        exposure = self.exposure()
        aggregate = aggregate_events([exposure])

        report = render_effectiveness_report(aggregate, max_items=20)

        self.assertIn("# Memory Effectiveness", report)
        self.assertIn("自动反馈属于弱证据", report)
        self.assertIn("decision-demo", report)
        self.assertNotIn("不得进入账本", report)
        self.assertNotIn("正文同样不得进入账本", report)

    def test_report_writer_uses_configured_vault_relative_paths(self):
        with tempfile.TemporaryDirectory() as vault:
            log_path = os.path.join(vault, "04-Feedback", "_logs", "events.jsonl")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(self.exposure(), ensure_ascii=False) + "\n")
            cfg = {
                "vault_path": vault,
                "memory_effectiveness": {
                    "enabled": True,
                    "event_log_path": "04-Feedback/_logs/events.jsonl",
                    "report_path": "04-Feedback/memory-effectiveness.md",
                    "max_report_items": 100,
                },
            }

            result = write_effectiveness_report(vault, cfg)

            report_path = os.path.join(vault, "04-Feedback", "memory-effectiveness.md")
            self.assertEqual(result["path"], report_path)
            self.assertEqual(result["event_count"], 1)
            with open(report_path, "r", encoding="utf-8") as handle:
                report = handle.read()
            self.assertIn("decision-demo", report)


if __name__ == "__main__":
    unittest.main()
