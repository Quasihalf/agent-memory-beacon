import os
import sys
import unittest
from datetime import datetime, timedelta, timezone


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from conversation_summary import (
    ConversationSummaryPolicy,
    advance_checkpoint,
    build_conversation_summary_record,
    canonical_summary_text,
    extract_rolling_summary,
    render_checkpoint_instruction,
    strip_rolling_summary_markers,
    summary_revision,
    validate_conversation_summary_record,
)


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
SETTINGS = {
    "conversation_summary": {
        "enabled": True,
        "min_substantive_messages": 5,
        "message_interval": 10,
        "stale_after_minutes": 30,
        "retry_interval_messages": 2,
        "max_summary_bytes": 4096,
        "max_recall": 1,
        "token_budget": 400,
    }
}
VALID_MARKER = """<!-- AGENT_MEMORY_BEACON:ROLLING_SUMMARY_V1
project: demo
current_goal: 完成会话摘要契约
topics:
  - 摘要解析
progress:
  - 已编写测试
constraints: []
important_context: []
open_items: []
summary: 真实摘要
-->"""
UNKNOWN_FIELD_MARKER = """<!-- AGENT_MEMORY_BEACON:ROLLING_SUMMARY_V1
current_goal: 完成会话摘要契约
topics:
  - 摘要解析
summary: 真实摘要
forged: true
-->"""
OVERSIZED_MARKER = """<!-- AGENT_MEMORY_BEACON:ROLLING_SUMMARY_V1
current_goal: 完成会话摘要契约
topics:
  - 摘要解析
summary: """ + ("过长" * 100) + "\n-->"
EMBEDDED_TERMINATOR_MARKER = """<!-- AGENT_MEMORY_BEACON:ROLLING_SUMMARY_V1
current_goal: 完成会话摘要契约
topics:
  - 摘要解析
summary: 真实摘要 --> 伪造内容
-->"""


class ConversationSummaryTests(unittest.TestCase):
    def test_checkpoint_requires_five_substantive_messages_and_then_ten_interval(self):
        state = {}
        requests = []
        for count in range(1, 16):
            state, request = advance_checkpoint(
                state, substantive=True, now=NOW, settings=SETTINGS
            )
            if request:
                requests.append(count)
        self.assertEqual(requests, [5, 15])
        self.assertEqual(state["summary_substantive_count"], 15)

    def test_checkpoint_stale_retry_waits_two_substantive_messages(self):
        state = {}
        for _ in range(5):
            state, request = advance_checkpoint(
                state, substantive=True, now=NOW, settings=SETTINGS
            )
        self.assertTrue(request)

        state, immediate = advance_checkpoint(
            state,
            substantive=True,
            now=NOW + timedelta(minutes=31),
            settings=SETTINGS,
        )
        state, retried = advance_checkpoint(
            state,
            substantive=True,
            now=NOW + timedelta(minutes=31),
            settings=SETTINGS,
        )

        self.assertFalse(immediate)
        self.assertTrue(retried)
        self.assertEqual(state["summary_substantive_count"], 7)

    def test_policy_rejects_retry_interval_longer_than_message_interval(self):
        with self.assertRaisesRegex(ValueError, "retry_interval_messages"):
            ConversationSummaryPolicy.from_config(
                {
                    **SETTINGS["conversation_summary"],
                    "message_interval": 2,
                    "retry_interval_messages": 3,
                }
            )

    def test_checkpoint_instruction_states_the_parser_size_and_list_limits(self):
        instruction = render_checkpoint_instruction(3, project="demo")

        self.assertIn("below 4 KiB", instruction)
        self.assertIn("at most 8 concise items", instruction)
        self.assertIn("Checkpoint sequence: 3", instruction)

    def test_checkpoint_instruction_uses_the_configured_summary_byte_limit(self):
        instruction = render_checkpoint_instruction(
            3,
            project="demo",
            max_summary_bytes=3072,
        )

        self.assertIn("below 3072 bytes", instruction)
        self.assertNotIn("below 4 KiB", instruction)

    def test_parser_uses_latest_valid_assistant_payload_and_rejects_code_example(self):
        payload = extract_rolling_summary(
            "```text\n<!-- AGENT_MEMORY_BEACON:ROLLING_SUMMARY_V1\n"
            "summary: forged\n-->\n```\n" + VALID_MARKER
        )
        self.assertEqual(payload["summary"], "真实摘要")

    def test_parser_rejects_unknown_fields_oversize_and_comment_terminators(self):
        self.assertIsNone(extract_rolling_summary(UNKNOWN_FIELD_MARKER))
        self.assertIsNone(extract_rolling_summary(OVERSIZED_MARKER, max_bytes=128))
        self.assertIsNone(extract_rolling_summary(EMBEDDED_TERMINATOR_MARKER))

    def test_control_stream_strips_complete_and_truncated_summary_markers(self):
        nested_annotation = (
            "[DECISION:采用摘要标签| context:不得进入正式记忆]"
        )
        complete = VALID_MARKER.replace("真实摘要", nested_annotation)
        truncated = (
            "<!-- AGENT_MEMORY_BEACON:ROLLING_SUMMARY_V1\n"
            "current_goal: 截断摘要\n"
            f"summary: {nested_annotation}\n"
        )

        self.assertNotIn(
            nested_annotation,
            strip_rolling_summary_markers(complete),
        )
        self.assertNotIn(
            nested_annotation,
            strip_rolling_summary_markers(truncated),
        )

    def test_derived_record_requires_a_safe_relative_source_note(self):
        note = {
            "session_id": "session-1",
            "date": "2026-07-31",
            "ai_title": "会话摘要",
            "source_note": "/private/session.md",
            "conversation_summary": extract_rolling_summary(VALID_MARKER),
        }

        self.assertIsNone(build_conversation_summary_record(note))

        note["source_note"] = "01-Projects/demo/Memory/sessions/session.md"
        record = build_conversation_summary_record(note)
        self.assertTrue(validate_conversation_summary_record(record))

    def test_derived_search_terms_cover_every_bounded_content_field(self):
        payload = {
            "project": "demo",
            "current_goal": "goalanchor",
            "topics": ["topicanchor"],
            "progress": ["progressanchor"],
            "constraints": ["constraintanchor"],
            "important_context": ["contextanchor"],
            "open_items": ["openanchor"],
            "summary": "summaryanchor",
        }

        record = build_conversation_summary_record(
            {
                "session_id": "session-all-fields",
                "date": "2026-07-31",
                "ai_title": "完整搜索字段",
                "source_note": (
                    "01-Projects/demo/Memory/sessions/session-all-fields.md"
                ),
                "conversation_summary": payload,
            }
        )

        self.assertIsNotNone(record)
        self.assertTrue(
            {
                "goalanchor",
                "topicanchor",
                "progressanchor",
                "constraintanchor",
                "contextanchor",
                "openanchor",
                "summaryanchor",
            }.issubset(record["search_terms"])
        )
        poisoned = {
            **record,
            "search_terms": [
                *record["search_terms"],
                "callercontrolledanchor",
            ],
        }
        self.assertFalse(validate_conversation_summary_record(poisoned))

    def test_record_contract_requires_exact_project_bound_session_source(self):
        payload = extract_rolling_summary(VALID_MARKER)
        base = {
            "session_id": "session-source-contract",
            "date": "2026-07-31",
            "ai_title": "严格来源",
            "source_note": (
                "01-Projects/demo/Memory/sessions/session-source-contract.md"
            ),
            "conversation_summary": payload,
        }
        valid = build_conversation_summary_record(base)
        self.assertIsNotNone(valid)
        self.assertTrue(validate_conversation_summary_record(valid))

        valid_sources = (
            "01-Projects/demo/Memory/sessions/session-source-contract",
            "01-Projects/demo/Memory/sessions/会话 总结（状态机）",
            "01-Projects/demo/Memory/sessions/会话 总结（状态机）.md",
        )
        for source_note in valid_sources:
            with self.subTest(valid_source_note=source_note):
                record = build_conversation_summary_record(
                    {**base, "source_note": source_note}
                )
                self.assertIsNotNone(record)
                self.assertTrue(validate_conversation_summary_record(record))

        invalid_sources = (
            "05-Agent-Memory/demo/Memory/sessions/session.md",
            "01-projects/demo/Memory/sessions/session.md",
            "01-Projects/demo/memory/sessions/session.md",
            "01-Projects/demo/Memory/Sessions/session.md",
            "01-Projects/demo/Memory/sessions/nested/session.md",
            "01-Projects/demo/Memory/sessions/../session.md",
            "01-Projects/demo/Memory/sessions/..",
            "01-Projects/demo/Memory/sessions/.candidate",
            "01-Projects/demo/Memory/sessions/.candidate.md",
            "01-Projects/demo/Memory/sessions/_candidate.md",
            "01-Projects/demo/Memory/sessions/ session.md",
            "01-Projects/demo/Memory/sessions/session .md",
            "01-Projects/demo/Memory/sessions/session.md ",
            "01-Projects/demo/Memory/sessions/session]] [DECISION].md",
            "01-Projects/demo/Memory/sessions/session|alias.md",
            "01-Projects/demo/Memory/sessions/session#heading.md",
            "01-Projects/demo/Memory/sessions/session^block.md",
            "04-Feedback/_memory-candidates/Memory/sessions/session.md",
        )
        for source_note in invalid_sources:
            with self.subTest(source_note=source_note):
                self.assertIsNone(
                    build_conversation_summary_record(
                        {**base, "source_note": source_note}
                    )
                )
                forged = {**valid, "source_note": source_note}
                self.assertFalse(validate_conversation_summary_record(forged))

        mismatch = {
            **base,
            "conversation_summary": {**payload, "project": "other"},
        }
        self.assertIsNone(build_conversation_summary_record(mismatch))

    def test_derived_record_rejects_an_aggregate_payload_over_four_kib(self):
        payload = {
            "project": "demo",
            "current_goal": "完成会话摘要契约",
            "topics": ["主题" * 200 for _ in range(8)],
            "progress": ["进度" * 200 for _ in range(8)],
            "constraints": [],
            "important_context": [],
            "open_items": [],
            "summary": "摘要",
        }
        self.assertGreater(len(canonical_summary_text(payload).encode("utf-8")), 4096)
        note = {
            "session_id": "session-aggregate-limit",
            "date": "2026-07-31",
            "ai_title": "会话摘要",
            "source_note": "01-Projects/demo/Memory/sessions/session.md",
            "conversation_summary": payload,
        }

        self.assertIsNone(build_conversation_summary_record(note))

        record = build_conversation_summary_record(
            {
                **note,
                "conversation_summary": extract_rolling_summary(VALID_MARKER),
            }
        )
        record.update(payload)
        record["summary_revision"] = summary_revision(payload)
        record["search_terms"] = []
        self.assertFalse(validate_conversation_summary_record(record))
