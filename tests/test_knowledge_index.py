import json
import os
import re
import sys
import tempfile
import unittest

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from knowledge_index import (
    build_global_atoms,
    build_keyword_index,
    build_memory_graph,
    build_recall_index,
    collect_indexable_notes,
    rebuild_vault_knowledge_indexes,
)
from conversation_summary import canonical_summary_text
from memory_judge import render_formal_memory_entry
from insight_memory import render_formal_record
from memory_graph import validate_memory_graph
from memory_recall import load_recall_index
from memory_schema import normalize_formal_record
from skill_preference_learner import render_formal_rule as render_skill_rule
from workflow_memory import render_formal_rule as render_workflow_rule


class KnowledgeIndexTests(unittest.TestCase):
    def test_conversation_summaries_keep_latest_revision_per_stable_session(self):
        older = conversation_summary_note(
            "shared-session",
            "旧的滚动摘要",
            path="01-Projects/demo/Memory/sessions/older",
            updated_at="2026-07-31T09:00:00+08:00",
        )
        newer = conversation_summary_note(
            "shared-session",
            "新的滚动摘要",
            path="01-Projects/demo/Memory/sessions/newer",
            updated_at="2026-07-31T10:00:00+08:00",
        )

        first = build_recall_index([newer, older])
        second = build_recall_index([older, newer])

        self.assertEqual(first["conversation_summary_count"], 1)
        self.assertEqual(
            first["conversation_summaries"],
            second["conversation_summaries"],
        )
        record = first["conversation_summaries"][0]
        self.assertEqual(record["session_id"], "shared-session")
        self.assertEqual(record["summary"], "新的滚动摘要")
        self.assertEqual(
            record["search_terms"],
            second["conversation_summaries"][0]["search_terms"],
        )
        self.assertEqual(
            record["summary_revision"],
            second["conversation_summaries"][0]["summary_revision"],
        )

    def test_conversation_summary_index_supports_legacy_plain_session_summary(self):
        note = conversation_summary_note(
            "legacy-session",
            "继续完成旧会话的索引兼容",
            structured=False,
        )

        record = build_recall_index([note])["conversation_summaries"][0]

        self.assertEqual(record["project"], "demo")
        self.assertEqual(record["summary"], "继续完成旧会话的索引兼容")
        self.assertTrue(record["current_goal"])
        self.assertTrue(record["topics"])

    def test_conversation_summary_index_derives_missing_project_from_session_path(self):
        note = conversation_summary_note(
            "path-project-session",
            "从规范会话路径绑定项目",
            project="",
        )

        record = build_recall_index([note])["conversation_summaries"][0]

        self.assertEqual(record["project"], "demo")

    def test_conversation_summary_index_rejects_candidates_and_missing_session_ids(self):
        candidate = conversation_summary_note(
            "candidate-session",
            "候选摘要不得进入索引",
            path="04-Feedback/_memory-candidates/Memory/sessions/candidate",
        )
        missing_id = conversation_summary_note(
            "",
            "缺少稳定会话 ID",
            path="01-Projects/demo/Memory/sessions/missing-id",
        )

        index = build_recall_index([candidate, missing_id])

        self.assertEqual(index["conversation_summary_count"], 0)
        self.assertEqual(index["conversation_summaries"], [])

    def test_conversation_summary_index_does_not_downgrade_malformed_json_to_plain_text(self):
        note = conversation_summary_note(
            "malformed-session",
            "临时占位",
        )
        note["body"] = (
            "# 滚动会话摘要\n\n## Session Summary\n"
            '{"project":"demo","current_goal":\n'
        )

        index = build_recall_index([note])

        self.assertEqual(index["conversation_summaries"], [])

    def test_conversation_summary_index_rejects_structured_lookalikes_but_accepts_complete_legacy(self):
        malformed_yaml = conversation_summary_note(
            "malformed-yaml",
            "临时占位",
        )
        malformed_yaml["body"] = (
            "# 滚动会话摘要\n\n## Session Summary\n"
            "current_goal: unfinished\n"
            "topics: [indexing]\n"
            'summary: "unterminated\n'
        )
        malformed_long_key = conversation_summary_note(
            "malformed-long-key",
            "临时占位",
        )
        malformed_long_key["body"] = (
            "# 滚动会话摘要\n\n## Session Summary\n"
            + ("mappingkey" * 20)
            + ": [unterminated\n"
        )
        partial_v1 = conversation_summary_note("partial-v1", "临时占位")
        partial_v1["body"] = (
            "# 滚动会话摘要\n\n## Session Summary\n"
            "current_goal: incomplete\n"
            "summary: must-not-downgrade\n"
        )
        incomplete_legacy = conversation_summary_note(
            "incomplete-legacy",
            "临时占位",
        )
        incomplete_legacy["body"] = (
            "# 滚动会话摘要\n\n## Session Summary\n"
            "summary: missing legacy envelope\n"
        )
        complete_legacy = conversation_summary_note(
            "complete-legacy",
            "临时占位",
        )
        complete_legacy["body"] = (
            "# 滚动会话摘要\n\n## Session Summary\n"
            "projects: [demo]\n"
            "primary: demo\n"
            "summary: 完整旧版总结\n"
        )

        index = build_recall_index(
            [
                malformed_yaml,
                malformed_long_key,
                partial_v1,
                incomplete_legacy,
                complete_legacy,
            ]
        )

        self.assertEqual(index["conversation_summary_count"], 1)
        self.assertEqual(
            index["conversation_summaries"][0]["summary"],
            "完整旧版总结",
        )

    def test_conversation_summary_index_accepts_migrated_legacy_envelopes(self):
        stale_route = conversation_summary_note(
            "frontmatter-stale-route",
            "临时占位",
            path=(
                "01-Projects/agent-memory-beacon/Memory/sessions/"
                "stale-pre-migration-route"
            ),
            project="agent-memory-beacon",
        )
        stale_route["body"] = (
            "# 滚动会话摘要\n\n## Session Summary\n"
            "projects: [github-obsidian-knowledge-brain]\n"
            "primary: github-obsidian-knowledge-brain\n"
            "decisions:\n"
            "errors: []\n"
            "summary: 迁移后以规范路径和 frontmatter 为准\n"
        )
        body_session_id = conversation_summary_note(
            "frontmatter-session-id",
            "临时占位",
            path="01-Projects/demo/Memory/sessions/body-session-id",
        )
        body_session_id["body"] = (
            "# 滚动会话摘要\n\n## Session Summary\n"
            "projects: [old-demo-route]\n"
            "primary: old-demo-route\n"
            "decisions: []\n"
            "errors: []\n"
            "session_id: ignored-body-session-id\n"
            "summary: 正文会话 ID 不覆盖 frontmatter\n"
        )

        index = build_recall_index([stale_route, body_session_id])
        records = {
            record["summary"]: record
            for record in index["conversation_summaries"]
        }

        self.assertEqual(index["conversation_summary_count"], 2)
        self.assertEqual(
            records["迁移后以规范路径和 frontmatter 为准"]["project"],
            "agent-memory-beacon",
        )
        self.assertEqual(
            records["正文会话 ID 不覆盖 frontmatter"]["session_id"],
            "frontmatter-session-id",
        )

    def test_conversation_summary_index_rejects_invalid_legacy_field_shapes(self):
        invalid_bodies = (
            "projects: demo\nprimary: demo\nsummary: invalid projects\n",
            "projects: [demo]\nprimary: [demo]\nsummary: invalid primary\n",
            "projects: [demo]\nprimary: demo\nsummary: [invalid]\n",
            (
                "projects: [demo]\nprimary: demo\ndecisions: {id: bad}\n"
                "summary: invalid decisions\n"
            ),
            (
                "projects: [demo]\nprimary: demo\nerrors: {type: bad}\n"
                "summary: invalid errors\n"
            ),
            (
                "projects: [demo]\nprimary: demo\nsession_id: [bad]\n"
                "summary: invalid session id\n"
            ),
            (
                "projects: [demo]\nprimary: demo\nunknown: true\n"
                "summary: unknown field\n"
            ),
        )

        notes = []
        for index, body in enumerate(invalid_bodies):
            note = conversation_summary_note(
                f"invalid-legacy-shape-{index}",
                "临时占位",
                path=(
                    "01-Projects/demo/Memory/sessions/"
                    f"invalid-legacy-shape-{index}"
                ),
            )
            note["body"] = (
                "# 滚动会话摘要\n\n## Session Summary\n" + body
            )
            notes.append(note)

        self.assertEqual(
            build_recall_index(notes)["conversation_summaries"],
            [],
        )

    def test_conversation_summary_index_rejects_yaml_indicator_downgrades(self):
        malformed_bodies = (
            '"unterminated quoted legacy summary',
            "'unterminated quoted legacy summary",
            "%YAML 1.2\nplain text without a document start",
            "---\n[unterminated collection",
            "|-\n  summary: [unterminated",
        )
        notes = []
        for index, body in enumerate(malformed_bodies):
            note = conversation_summary_note(
                f"malformed-indicator-{index}",
                "临时占位",
                path=(
                    "01-Projects/demo/Memory/sessions/"
                    f"malformed-indicator-{index}"
                ),
            )
            note["body"] = (
                "# 滚动会话摘要\n\n## Session Summary\n" + body + "\n"
            )
            notes.append(note)
        plain = conversation_summary_note(
            "clearly-plain-legacy",
            "Clearly plain legacy prose remains compatible.",
            path=(
                "01-Projects/demo/Memory/sessions/"
                "clearly-plain-legacy"
            ),
            structured=False,
        )

        index = build_recall_index([*notes, plain])

        self.assertEqual(index["conversation_summary_count"], 1)
        self.assertEqual(
            index["conversation_summaries"][0]["summary"],
            "Clearly plain legacy prose remains compatible.",
        )

    def test_conversation_summary_admission_uses_exact_shared_source_contract(self):
        invalid_paths = (
            "01-projects/demo/Memory/sessions/session",
            "01-Projects/demo/memory/sessions/session",
            "01-Projects/demo/Memory/Sessions/session",
            "01-Projects/demo/Memory/sessions/nested/session",
            "01-Projects/demo/Memory/sessions/..",
            "01-Projects/demo/Memory/sessions/.candidate",
            "01-Projects/demo/Memory/sessions/.candidate.md",
            "01-Projects/demo/Memory/sessions/_candidate.md",
            "01-Projects/demo/Memory/sessions/ session.md",
            "01-Projects/demo/Memory/sessions/session .md",
            "01-Projects/demo/Memory/sessions/session.md ",
            "04-Feedback/_memory-candidates/Memory/sessions/session",
        )

        for index, path in enumerate(invalid_paths):
            with self.subTest(path=path):
                note = conversation_summary_note(
                    f"invalid-source-{index}",
                    "不得索引",
                    path=path,
                )
                self.assertEqual(
                    build_recall_index([note])["conversation_summaries"],
                    [],
                )

        for index, path in enumerate(
            (
                "01-Projects/demo/Memory/sessions/真实 会话标题",
                "01-Projects/demo/Memory/sessions/真实 会话标题.md",
            )
        ):
            with self.subTest(valid_path=path):
                note = conversation_summary_note(
                    f"valid-unicode-source-{index}",
                    "应索引真实 Unicode 标题",
                    path=path,
                )
                self.assertEqual(
                    build_recall_index([note])[
                        "conversation_summary_count"
                    ],
                    1,
                )

        mismatch = conversation_summary_note(
            "project-mismatch",
            "不得跨项目",
            path="01-Projects/demo/Memory/sessions/project-mismatch",
            project="other",
        )
        self.assertEqual(
            build_recall_index([mismatch])["conversation_summaries"],
            [],
        )

    def test_conversation_summary_freshness_prioritizes_numeric_cursor_before_time(self):
        absolute_older = conversation_summary_note(
            "absolute-time-session",
            "绝对时间较旧",
            path="01-Projects/demo/Memory/sessions/absolute-older",
            updated_at="2026-07-31T10:00:00+08:00",
            cursor="file-bytes:99",
        )
        absolute_newer = conversation_summary_note(
            "absolute-time-session",
            "绝对时间较新",
            path="01-Projects/demo/Memory/sessions/absolute-newer",
            updated_at="2026-07-31T03:00:00Z",
            cursor="file-bytes:9",
        )
        self.assertEqual(
            build_recall_index(
                [absolute_newer, absolute_older]
            )["conversation_summaries"][0]["summary"],
            "绝对时间较旧",
        )

        for cursor_kind in ("file-bytes", "zcode-messages"):
            with self.subTest(cursor_kind=cursor_kind):
                nine = conversation_summary_note(
                    f"numeric-cursor-{cursor_kind}",
                    "游标九",
                    path=(
                        "01-Projects/demo/Memory/sessions/"
                        f"numeric-nine-{cursor_kind}"
                    ),
                    updated_at="2026-07-31T10:00:00+08:00",
                    cursor=f"{cursor_kind}:9",
                )
                ten = conversation_summary_note(
                    f"numeric-cursor-{cursor_kind}",
                    "游标十",
                    path=(
                        "01-Projects/demo/Memory/sessions/"
                        f"numeric-ten-{cursor_kind}"
                    ),
                    updated_at="2026-07-31T02:00:00Z",
                    cursor=f"{cursor_kind}:10",
                )
                first = build_recall_index([nine, ten])
                second = build_recall_index([ten, nine])
                self.assertEqual(
                    first["conversation_summaries"],
                    second["conversation_summaries"],
                )
                self.assertEqual(
                    first["conversation_summaries"][0]["summary"],
                    "游标十",
                )

        legacy_a = conversation_summary_note(
            "legacy-fallback",
            "旧版甲",
            path="01-Projects/demo/Memory/sessions/legacy-a",
            updated_at="",
        )
        legacy_b = conversation_summary_note(
            "legacy-fallback",
            "旧版乙",
            path="01-Projects/demo/Memory/sessions/legacy-b",
            updated_at="",
        )
        self.assertEqual(
            build_recall_index([legacy_a, legacy_b])["conversation_summaries"],
            build_recall_index([legacy_b, legacy_a])["conversation_summaries"],
        )

    def test_conversation_summary_body_changes_generation_identity(self):
        first = build_recall_index(
            [conversation_summary_note("generation-session", "第一版摘要")]
        )
        second = build_recall_index(
            [conversation_summary_note("generation-session", "第二版摘要")]
        )

        self.assertNotEqual(first["generation_id"], second["generation_id"])

    def test_conversation_summaries_stay_out_of_formal_units_graph_and_experiences(self):
        note = conversation_summary_note(
            "isolated-session",
            "滚动摘要只能进入独立派生集合",
        )

        index = build_recall_index([note])
        graph = build_memory_graph([note], index)
        summary_id = index["conversation_summaries"][0]["id"]

        self.assertEqual(index["units"], [])
        self.assertEqual(index["experience_bundles"], [])
        self.assertNotIn(summary_id, {node["id"] for node in graph["nodes"]})

    def test_rebuild_writes_observable_graph_projection_nodes(self):
        with tempfile.TemporaryDirectory() as vault:
            memory_dir = os.path.join(
                vault,
                "01-Projects",
                "demo",
                "Memory",
            )
            os.makedirs(memory_dir)
            decision = aggregate_record(
                "decision-observable-graph",
                "decision",
                "把正式记忆显示为独立图谱节点",
                "投影层不改变正式来源",
            )
            write_text(
                os.path.join(memory_dir, "decisions.md"),
                markdown_document(
                    {
                        "schema_version": "2.0",
                        "project": "demo",
                        "decisions": [decision],
                    },
                    "# Decisions\n",
                ),
            )

            result = rebuild_vault_knowledge_indexes({"vault_path": vault})

            projection_root = os.path.join(
                vault,
                "03-Maps",
                "_memory-nodes",
            )
            projected = [
                os.path.join(current, filename)
                for current, _dirs, files in os.walk(projection_root)
                for filename in files
                if filename.endswith(".md")
            ]
            self.assertGreaterEqual(result["graph_projection_nodes"], 1)
            self.assertEqual(
                len(projected),
                result["graph_projection_nodes"],
            )
            self.assertTrue(
                any(
                    "把正式记忆显示为独立图谱节点" in read_text(path)
                    for path in projected
                )
            )
            second = rebuild_vault_knowledge_indexes({"vault_path": vault})
            self.assertEqual(second["recall_units"], 1)
            self.assertEqual(
                second["graph_projection_nodes"],
                result["graph_projection_nodes"],
            )
            self.assertEqual(second["graph_projection_written"], 0)

    def test_project_and_adaptive_records_round_trip_authority_metadata(self):
        with tempfile.TemporaryDirectory() as vault:
            project_dir = os.path.join(vault, "01-Projects", "demo", "Memory")
            memory_dir = os.path.join(vault, "05-Agent-Memory")
            os.makedirs(project_dir)
            os.makedirs(memory_dir)
            authority = {
                "authority_role": "canonical",
                "authority_owner": "runtime repository",
                "canonical_source": "repo:scripts/memory_runtime.py",
                "enforced_by": ["test:tests/test_memory_runtime.py"],
                "verification_refs": ["runbook:release/verify"],
                "verified_at": "2026-07-22",
                "freshness_policy": "source-change",
            }
            decision = aggregate_record(
                "decision-authority-index",
                "decision",
                "运行时源码拥有动态召回行为",
                "Obsidian 保存理由和证据",
                authority=authority,
            )
            write_text(
                os.path.join(project_dir, "decisions.md"),
                markdown_document(
                    {
                        "schema_version": "2.0",
                        "project": "demo",
                        "decisions": [decision],
                    },
                    "# Decisions\n",
                ),
            )
            workflow_authority = {
                "authority_role": "operationalized",
                "authority_owner": "Codex hook",
                "enforced_by": ["system:codex/UserPromptSubmit"],
                "verification_refs": ["test:tests/test_codex_prompt_hook.py"],
                "verified_at": "2026-07-22",
                "freshness_policy": "source-change",
            }
            write_text(
                os.path.join(memory_dir, "workflow-rules.md"),
                "---\nschema_version: '2.0'\n---\n\n"
                + render_workflow_rule(
                    {
                        "memory_id": "workflow-authority-index",
                        "rule_name": "source_first",
                        "trigger_scene": "用户要求分析 GitHub 项目",
                        "desired_behavior": "先阅读上游源码",
                        "why_it_matters": "避免根据名称猜测",
                        "positive_signals": ["GitHub 项目"],
                        "negative_signals": ["用户要求离线"],
                        "project": "demo",
                        "source_ids": ["sess-workflow"],
                    },
                    lifecycle_metadata=workflow_authority,
                ),
            )

            rebuild_vault_knowledge_indexes({"vault_path": vault})

            units = {
                item["id"]: item
                for item in load_json(os.path.join(memory_dir, "recall-index.json"))["units"]
            }
            for key, value in authority.items():
                self.assertEqual(units["decision-authority-index"][key], value)
            for key, value in workflow_authority.items():
                self.assertEqual(units["workflow-authority-index"][key], value)
    def test_custom_adaptive_formal_path_is_indexed(self):
        with tempfile.TemporaryDirectory() as vault:
            custom = os.path.join(vault, "06-Custom", "workflow-memory.md")
            os.makedirs(os.path.dirname(custom))
            default_memory = os.path.join(vault, "05-Agent-Memory")
            os.makedirs(default_memory)
            write_text(
                os.path.join(default_memory, "workflow-rules.md"),
                "---\nschema_version: '2.0'\n---\n\n"
                + render_workflow_rule(
                    {
                        "memory_id": "workflow-stale-default",
                        "rule_name": "stale_default",
                        "trigger_scene": "旧默认路径仍存在",
                        "desired_behavior": "不得进入召回",
                        "why_it_matters": "配置路径才是正式来源",
                        "positive_signals": ["旧路径"],
                        "negative_signals": [],
                        "project": "demo",
                        "source_ids": ["sess-stale"],
                    }
                ),
            )
            write_text(
                custom,
                "---\nschema_version: '2.0'\n---\n\n"
                + render_workflow_rule(
                    {
                        "memory_id": "workflow-custom-index",
                        "rule_name": "custom_source_first",
                        "trigger_scene": "用户提供自定义仓库",
                        "desired_behavior": "先读取自定义源码",
                        "why_it_matters": "避免猜测",
                        "positive_signals": ["自定义仓库"],
                        "negative_signals": ["用户明确要求离线"],
                        "project": "demo",
                        "source_ids": ["sess-custom"],
                    }
                ),
            )
            cfg = {
                "vault_path": vault,
                "workflow_memory": {
                    "formal_path": "06-Custom/workflow-memory.md"
                },
            }

            rebuild_vault_knowledge_indexes(cfg)

            recall = load_json(
                os.path.join(vault, "05-Agent-Memory", "recall-index.json")
            )
            self.assertEqual(
                [item["id"] for item in recall["units"]],
                ["workflow-custom-index"],
            )

    def test_configured_promotion_proposal_root_is_never_indexed(self):
        with tempfile.TemporaryDirectory() as vault:
            proposal_dir = os.path.join(
                vault,
                "05-Agent-Memory",
                "private-promotion-proposals",
            )
            os.makedirs(proposal_dir)
            write_text(
                os.path.join(proposal_dir, "proposal.md"),
                markdown_document(
                    {
                        "schema_version": "1.0",
                        "type": "memory-promotion-proposal",
                        "status": "candidate",
                    },
                    "# privatepromotionleaktoken\n",
                ),
            )

            rebuild_vault_knowledge_indexes(
                {
                    "vault_path": vault,
                    "memory_promotion": {
                        "proposal_dir": "05-Agent-Memory/private-promotion-proposals"
                    },
                }
            )

            recall = load_json(
                os.path.join(vault, "05-Agent-Memory", "recall-index.json")
            )
            self.assertNotIn("privatepromotionleaktoken", json.dumps(recall))

    def test_all_configured_adaptive_candidate_roots_are_never_indexed(self):
        for section in (
            "personal_memory",
            "skill_preferences",
            "workflow_memory",
            "insight_memory",
        ):
            with self.subTest(section=section), tempfile.TemporaryDirectory() as vault:
                relative_dir = f"05-Agent-Memory/private-{section}-candidates"
                candidate_dir = os.path.join(vault, relative_dir)
                os.makedirs(candidate_dir)
                token = f"{section.replace('_', '')}candidateleaktoken"
                write_text(
                    os.path.join(candidate_dir, "private.md"),
                    markdown_document(
                        {
                            "schema_version": "2.0",
                            "status": "candidate",
                        },
                        f"# {token}\n",
                    ),
                )

                result = rebuild_vault_knowledge_indexes(
                    {
                        "vault_path": vault,
                        section: {"candidate_dir": relative_dir},
                    }
                )

                machine_text = "\n".join(
                    read_text(path) for path in result["written"]
                )
                self.assertNotIn(token, machine_text)

    def test_rebuild_writes_configured_runtime_recall_index_path(self):
        with tempfile.TemporaryDirectory() as vault:
            custom = os.path.join(vault, "06-Custom", "runtime-recall.json")
            cfg = {
                "vault_path": vault,
                "memory_runtime": {
                    "index_path": "06-Custom/runtime-recall.json"
                },
            }

            rebuild_vault_knowledge_indexes(cfg)

            self.assertTrue(os.path.exists(custom))
            custom_graph = os.path.join(vault, "06-Custom", "memory-graph.json")
            self.assertTrue(os.path.exists(custom_graph))
            self.assertIn("_graph", load_recall_index(custom))
            self.assertFalse(
                os.path.exists(
                    os.path.join(vault, "05-Agent-Memory", "recall-index.json")
                )
            )
            self.assertFalse(
                os.path.exists(
                    os.path.join(vault, "05-Agent-Memory", "memory-graph.json")
                )
            )

    def test_graph_generation_changes_when_note_links_change(self):
        original_note = {
            "path": "01-Projects/demo/Memory/decisions",
            "title": "Decisions",
            "project": "demo",
            "type": "decisions",
            "frontmatter": {"schema_version": "2.0", "project": "demo"},
            "body": "# Decisions\n",
            "links": ["01-Projects/demo/Memory/pitfalls"],
        }
        changed_note = {
            **original_note,
            "links": [],
        }
        original_index = build_recall_index([original_note])
        changed_index = build_recall_index([changed_note])
        old_graph = build_memory_graph([original_note], original_index)

        self.assertNotEqual(
            original_index["generation_id"],
            changed_index["generation_id"],
        )
        with self.assertRaisesRegex(ValueError, "generation_mismatch=1"):
            validate_memory_graph(
                old_graph,
                changed_index["units"],
                allow_legacy=False,
                expected_generation_id=changed_index["generation_id"],
            )

    def test_graph_generation_changes_when_note_title_changes(self):
        original_note = {
            "path": "01-Projects/demo/Memory/sessions/session-one",
            "title": "原始会话标题",
            "project": "demo",
            "type": "session",
            "frontmatter": {"date": "2026-07-26", "project": "demo"},
            "body": "# 原始会话标题\n",
            "links": [],
        }
        changed_note = {
            **original_note,
            "title": "更新后的会话标题",
            "body": "# 更新后的会话标题\n",
        }

        original_index = build_recall_index([original_note])
        changed_index = build_recall_index([changed_note])

        self.assertNotEqual(
            original_index["generation_id"],
            changed_index["generation_id"],
        )

    def test_skill_operational_boundaries_are_available_to_recall(self):
        with tempfile.TemporaryDirectory() as vault:
            memory_dir = os.path.join(vault, "05-Agent-Memory")
            os.makedirs(memory_dir)
            write_text(
                os.path.join(memory_dir, "skill-routing-rules.md"),
                "---\nschema_version: '2.0'\n---\n\n"
                + render_skill_rule(
                    {
                        "memory_id": "skill-operational-recall",
                        "skill_name": "humanizer",
                        "task_intent": "让中文表达更自然",
                        "why_skill_fits": "降低模板感",
                        "positive_signals": ["用户要求说人话"],
                        "negative_signals": ["用户要求逐字引用"],
                        "project": "demo",
                        "source_ids": ["sess-skill"],
                    }
                ),
            )

            rebuild_vault_knowledge_indexes({"vault_path": vault})

            unit = load_json(
                os.path.join(vault, "05-Agent-Memory", "recall-index.json")
            )["units"][0]
            self.assertEqual(unit["when"], "用户要求说人话")
            self.assertEqual(unit["avoid"], "用户要求逐字引用")
            self.assertIn("用户要求说人话", unit["recall_summary"])
            self.assertIn("用户要求逐字引用", unit["recall_summary"])

    def test_inactive_exact_fact_suppresses_duplicate_active_id_from_recall(self):
        inactive = aggregate_record(
            "decision-retired",
            "decision",
            "停用源码目录 hook",
            "稳定运行时已经接管 hook",
            project="demo",
            status="retracted",
        )
        active_duplicate = aggregate_record(
            "decision-duplicate-active",
            "decision",
            "停用源码目录 hook",
            "稳定运行时已经接管 hook",
            project="demo",
        )
        note = {
            "path": "01-Projects/demo/Memory/decisions",
            "title": "Decisions",
            "project": "demo",
            "type": "decisions",
            "text": "",
            "frontmatter": {
                "project": "demo",
                "schema_version": "2.0",
                "decisions": [inactive, active_duplicate],
            },
            "body": "# Decisions\n",
            "links": [],
        }

        recall = build_recall_index([note])

        self.assertEqual(recall["units"], [])

    def test_superseded_exact_fact_does_not_suppress_its_named_active_successor(self):
        successor = aggregate_record(
            "decision-current",
            "decision",
            "停用源码目录 hook",
            "稳定运行时已经接管 hook",
            project="demo",
        )
        superseded = aggregate_record(
            "decision-retired-copy",
            "decision",
            "停用源码目录 hook",
            "稳定运行时已经接管 hook",
            project="demo",
            status="superseded",
            superseded_by=successor["id"],
        )
        note = {
            "path": "01-Projects/demo/Memory/decisions",
            "title": "Decisions",
            "project": "demo",
            "type": "decisions",
            "text": "",
            "frontmatter": {
                "project": "demo",
                "schema_version": "2.0",
                "decisions": [successor, superseded],
            },
            "body": "# Decisions\n",
            "links": [],
        }

        recall = build_recall_index([note])

        self.assertEqual([item["id"] for item in recall["units"]], [successor["id"]])

    def test_retracted_exact_fact_still_vetoes_a_supersession_successor(self):
        successor = aggregate_record(
            "decision-current-but-retracted",
            "decision",
            "停用源码目录 hook",
            "稳定运行时已经接管 hook",
            project="demo",
        )
        superseded = aggregate_record(
            "decision-retired-copy",
            "decision",
            "停用源码目录 hook",
            "稳定运行时已经接管 hook",
            project="demo",
            status="superseded",
            superseded_by=successor["id"],
        )
        retracted = aggregate_record(
            "decision-explicitly-retracted",
            "decision",
            "停用源码目录 hook",
            "稳定运行时已经接管 hook",
            project="demo",
            status="retracted",
        )
        note = {
            "path": "01-Projects/demo/Memory/decisions",
            "title": "Decisions",
            "project": "demo",
            "type": "decisions",
            "text": "",
            "frontmatter": {
                "project": "demo",
                "schema_version": "2.0",
                "decisions": [successor, superseded, retracted],
            },
            "body": "# Decisions\n",
            "links": [],
        }

        recall = build_recall_index([note])

        self.assertEqual(recall["units"], [])

    def test_recall_index_conservatively_collapses_historical_error_duplicates(self):
        note = {
            "path": "01-Projects/tcad/Memory/pitfalls",
            "title": "Pitfalls",
            "project": "tcad",
            "type": "pitfalls",
            "text": "",
            "frontmatter": {
                "project": "tcad",
                "schema_version": "2.0",
                "pitfalls": [
                    aggregate_record(
                    "error-pdf-a",
                    "error",
                    "shell-cli",
                    "本机缺少 pdftotext，改用 pypdf 完成文本校验",
                    project="tcad",
                ),
                    aggregate_record(
                    "error-pdf-b",
                    "error",
                    "shell-cli",
                    "系统没有 pdftotext，改用 PyMuPDF 完成文本核对",
                    project="tcad",
                ),
                ],
            },
            "body": "# Pitfalls\n",
            "links": [],
        }

        recall = build_recall_index([note])

        self.assertEqual(recall["unit_count"], 1)
        self.assertEqual(len(recall["duplicate_groups"]), 1)
        self.assertEqual(
            set(recall["duplicate_groups"][0]["member_ids"]),
            {"error-pdf-a", "error-pdf-b"},
        )

    def test_recall_index_reports_quality_suppression_without_mutating_source(self):
        record = aggregate_record(
            "decision-review-outcome",
            "decision",
            "logic-reviewer 结论为 NEEDS REVISION",
            "发现两个仍需修复的验证门缺陷",
            project="demo",
        )
        note = {
            "path": "01-Projects/demo/Memory/decisions",
            "title": "Decisions",
            "project": "demo",
            "type": "decisions",
            "text": "",
            "frontmatter": {
                "project": "demo",
                "schema_version": "2.0",
                "decisions": [record],
            },
            "body": "# Decisions\n",
            "links": [],
        }

        recall = build_recall_index([note])

        self.assertEqual(recall["unit_count"], 0)
        self.assertEqual(
            recall["suppressed_quality"]["decision-review-outcome"],
            ["evaluation_outcome"],
        )
        self.assertEqual(record["status"], "active")

    def test_error_evidence_candidates_never_enter_any_knowledge_index(self):
        with tempfile.TemporaryDirectory() as vault:
            path = os.path.join(
                vault,
                "04-Feedback/_error-candidates/error-evidence-private.md",
            )
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_text(
                path,
                markdown_document(
                    {
                        "schema_version": "2.0",
                        "type": "error-evidence-candidate",
                        "status": "candidate",
                        "project": "demo",
                        "excerpt": "privatecandidateleaktoken unresolved failure",
                    },
                    "# privatecandidateleaktoken\n",
                ),
            )
            custom_path = os.path.join(
                vault,
                "05-Agent-Memory/custom-error-evidence/private.md",
            )
            os.makedirs(os.path.dirname(custom_path), exist_ok=True)
            write_text(
                custom_path,
                markdown_document(
                    {
                        "schema_version": "2.0",
                        "status": "candidate",
                        "project": "demo",
                        "excerpt": "customcandidateleaktoken unresolved failure",
                    },
                    "# customcandidateleaktoken\n",
                ),
            )
            note = {
                "path": "04-Feedback/_error-candidates/error-evidence-private",
                "title": "privatecandidateleaktoken",
                "project": "demo",
                "type": "error-evidence-candidate",
                "text": "privatecandidateleaktoken unresolved failure",
                "frontmatter": {
                    "schema_version": "2.0",
                    "type": "error-evidence-candidate",
                    "status": "candidate",
                },
                "body": "# privatecandidateleaktoken\n",
                "links": [],
            }

            self.assertEqual(
                collect_indexable_notes(
                    vault,
                    excluded_roots=["05-Agent-Memory/custom-error-evidence"],
                ),
                [],
            )
            rebuilt = rebuild_vault_knowledge_indexes(
                {
                    "vault_path": vault,
                    "error_evidence": {
                        "candidate_dir": "05-Agent-Memory/custom-error-evidence"
                    },
                }
            )
            machine_text = "\n".join(read_text(path) for path in rebuilt["written"])
            self.assertNotIn("customcandidateleaktoken", machine_text)
            keyword = build_keyword_index([note])
            recall = build_recall_index([note])
            graph = build_memory_graph([note], recall)

            self.assertNotIn("privatecandidateleaktoken", keyword["keywords"])
            self.assertEqual(recall["units"], [])
            self.assertNotIn("privatecandidateleaktoken", recall["terms"])
            self.assertEqual(graph["nodes"], [])
            self.assertEqual(graph["edges"], [])

    def test_candidate_root_symlink_alias_is_rejected_before_index_walk(self):
        with tempfile.TemporaryDirectory() as vault:
            real_root = os.path.join(
                vault,
                "01-Projects",
                "demo",
                "candidate-state",
            )
            os.makedirs(real_root)
            write_text(
                os.path.join(real_root, "malformed.md"),
                markdown_document(
                    {"status": "candidate", "project": "demo"},
                    "# resolvedrootleaktoken\n",
                ),
            )
            alias_parent = os.path.join(vault, "05-Agent-Memory")
            os.makedirs(alias_parent)
            os.symlink(
                os.path.join(vault, "01-Projects", "demo"),
                os.path.join(alias_parent, "candidate-alias"),
            )
            cfg = {
                "vault_path": vault,
                "error_evidence": {
                    "candidate_dir": (
                        "05-Agent-Memory/candidate-alias/candidate-state"
                    )
                },
            }

            with self.assertRaises(ValueError):
                rebuild_vault_knowledge_indexes(cfg)

            self.assertFalse(
                os.path.exists(
                    os.path.join(vault, "05-Agent-Memory", "keyword-index.json")
                )
            )

    def test_global_atoms_require_schema_2_active_verified_pitfalls(self):
        with tempfile.TemporaryDirectory() as vault:
            def write_pitfall(project, schema_version, record):
                path = os.path.join(
                    vault,
                    f"01-Projects/{project}/Memory/pitfalls.md",
                )
                os.makedirs(os.path.dirname(path), exist_ok=True)
                write_text(
                    path,
                    markdown_document(
                        {
                            "project": project,
                            "schema_version": schema_version,
                            "pitfalls": [record],
                        },
                        "# Pitfalls\n",
                    ),
                )

            alpha = aggregate_record(
                "error-alpha",
                "error",
                "path-filesystem",
                "使用 canonical 路径避免重复失败",
                project="alpha",
            )
            beta = aggregate_record(
                "error-beta",
                "error",
                "path-filesystem",
                "使用 canonical 路径避免重复失败",
                project="beta",
            )
            write_pitfall("alpha", "1.0", alpha)
            write_pitfall("beta", "1.0", beta)
            self.assertEqual(build_global_atoms(vault)["atoms"], [])

            forged = dict(beta)
            forged["revision"] = "0" * 64
            write_pitfall("alpha", "2.0", alpha)
            write_pitfall("beta", "2.0", forged)
            self.assertEqual(build_global_atoms(vault)["atoms"], [])

            inactive = aggregate_record(
                "error-beta",
                "error",
                "path-filesystem",
                "使用 canonical 路径避免重复失败",
                project="beta",
                status="retracted",
            )
            write_pitfall("beta", "2.0", inactive)
            self.assertEqual(build_global_atoms(vault)["atoms"], [])

            write_pitfall("beta", "2.0", beta)
            atoms = build_global_atoms(vault)["atoms"]
            self.assertEqual(len(atoms), 1)
            self.assertEqual(atoms[0]["projects"], ["alpha", "beta"])

    def test_recall_rejects_legacy_aggregate_and_forged_formal_revision(self):
        with tempfile.TemporaryDirectory() as vault:
            decisions = os.path.join(vault, "01-Projects/demo/Memory/decisions.md")
            personal = os.path.join(vault, "05-Agent-Memory/personal-memory.md")
            os.makedirs(os.path.dirname(decisions), exist_ok=True)
            os.makedirs(os.path.dirname(personal), exist_ok=True)
            write_text(
                decisions,
                """---
project: demo
schema_version: '1.0'
decisions:
- id: legacy-decision
  revision: legacy-revision
  text: Legacy decision must not be recalled
  context: Schema 1 is historical input only
  status: active
  project: demo
  scope: project
  source_refs: [session:legacy]
---
""",
            )
            section = render_formal_memory_entry(
                {
                    "memory_id": "forged-preference",
                    "title": "用户偏好: 伪造 revision",
                    "content": "这条记录必须被拒绝",
                    "type": "preference",
                    "source_ids": ["sess-forged"],
                }
            )
            section = re.sub(
                r"(?m)^- revision: `[0-9a-f]{64}`$",
                "- revision: `" + "0" * 64 + "`",
                section,
            )
            write_text(
                personal,
                "---\nschema_version: '2.0'\n---\n\n" + section,
            )

            rebuild_vault_knowledge_indexes({"vault_path": vault})

            recall = load_json(
                os.path.join(vault, "05-Agent-Memory/recall-index.json")
            )
            self.assertEqual(recall["units"], [])

    def test_formal_adaptive_units_preserve_revision_summary_and_source_refs(self):
        with tempfile.TemporaryDirectory() as vault:
            memory_dir = os.path.join(vault, "05-Agent-Memory")
            os.makedirs(memory_dir)
            long_personal_summary = "默认用中文解释" + "，保留完整上下文" * 50
            fixtures = [
                (
                    "personal-memory.md",
                    render_formal_memory_entry(
                        {
                            "memory_id": "preference-index",
                            "title": "用户偏好: 中文输出",
                            "content": long_personal_summary,
                            "type": "preference",
                            "source_ids": ["sess-personal"],
                        }
                    ),
                    long_personal_summary,
                ),
                (
                    "skill-routing-rules.md",
                    render_skill_rule(
                        {
                            "memory_id": "skill-index",
                            "skill_name": "humanizer",
                            "task_intent": "让中文表达更自然",
                            "why_skill_fits": "适合减少模板感",
                            "positive_signals": ["说人话"],
                            "negative_signals": ["逐字引用"],
                            "project": "demo",
                            "source_ids": ["sess-skill"],
                        }
                    ),
                    "适合减少模板感",
                ),
                (
                    "workflow-rules.md",
                    render_workflow_rule(
                        {
                            "memory_id": "workflow-index",
                            "rule_name": "github_source_first",
                            "trigger_scene": "用户要求分析 GitHub 项目",
                            "desired_behavior": "先阅读源码",
                            "why_it_matters": "避免根据名称猜测",
                            "positive_signals": ["GitHub 项目"],
                            "negative_signals": ["用户要求离线"],
                            "project": "demo",
                            "source_ids": ["sess-workflow"],
                        }
                    ),
                    "避免根据名称猜测",
                ),
            ]
            expected = {}
            for filename, section, summary in fixtures:
                write_text(
                    os.path.join(memory_dir, filename),
                    "---\nschema_version: '2.0'\n---\n\n" + section,
                )
                memory_id = re.search(r"(?m)^- id: `([^`]+)`$", section).group(1)
                revision = re.search(
                    r"(?m)^- revision: `([0-9a-f]{64})`$",
                    section,
                ).group(1)
                source_line = re.search(
                    r"(?m)^- source_refs: (.+)$",
                    section,
                ).group(1)
                expected[memory_id] = {
                    "revision": revision,
                    "summary": summary,
                    "source_refs": set(re.findall(r"`([^`]+)`", source_line)),
                }

            rebuild_vault_knowledge_indexes({"vault_path": vault})

            recall = load_json(
                os.path.join(vault, "05-Agent-Memory/recall-index.json")
            )
            units = {unit["id"]: unit for unit in recall["units"]}
            self.assertEqual(set(units), set(expected))
            for memory_id, wanted in expected.items():
                with self.subTest(memory_id=memory_id):
                    self.assertEqual(units[memory_id]["revision"], wanted["revision"])
                    self.assertEqual(units[memory_id]["summary"], wanted["summary"])
                    self.assertTrue(
                        wanted["source_refs"].issubset(units[memory_id]["source_refs"])
                    )

    def test_formal_insight_is_indexed_with_relations_and_candidate_is_excluded(self):
        with tempfile.TemporaryDirectory() as vault:
            memory_dir = os.path.join(vault, "05-Agent-Memory")
            candidate_dir = os.path.join(memory_dir, "private-insight-candidates")
            os.makedirs(candidate_dir, exist_ok=True)
            insight = normalize_formal_record(
                {
                    "id": "insight-index-demo",
                    "status": "active",
                    "maturity": "reinforced",
                    "confidence": 0.84,
                    "origin": "user",
                    "project": "demo",
                    "scope": "project",
                    "title": "互补弱通道通过融合形成稳定系统",
                    "summary": "多个互补的弱通道可以通过排名融合提高稳定性",
                    "novelty": "不依赖单一语义检索或单一评分器",
                    "transfer": ["记忆召回", "技能路由"],
                    "boundary": "通道共享同一偏置时收益有限",
                    "source_refs": ["session:sess-first", "session:sess-second"],
                    "supports": ["decision-formal-demo"],
                    "operationalized_as": ["workflow-demo"],
                    "related_to": ["insight-related-demo"],
                },
                memory_type="insight",
                default_project="demo",
                source_ref="",
            )
            write_text(
                os.path.join(memory_dir, "insights.md"),
                "---\nschema_version: '2.0'\nsummary_type: insights\n---\n\n"
                + render_formal_record(insight),
            )
            write_text(
                os.path.join(candidate_dir, "private.md"),
                "---\ntype: insight-candidate\nstatus: candidate\n---\n\n"
                "# privateinsightleaktoken\n",
            )
            cfg = {
                "vault_path": vault,
                "insight_memory": {
                    "enabled": True,
                    "formal_path": "05-Agent-Memory/insights.md",
                    "candidate_dir": "05-Agent-Memory/private-insight-candidates",
                },
            }

            rebuild_vault_knowledge_indexes(cfg)

            recall = load_json(os.path.join(memory_dir, "recall-index.json"))
            units = [unit for unit in recall["units"] if unit["type"] == "insight"]
            self.assertEqual(len(units), 1)
            unit = units[0]
            self.assertEqual(unit["id"], "insight-index-demo")
            self.assertEqual(unit["maturity"], "reinforced")
            self.assertIn("单一语义检索", unit["recall_summary"])
            self.assertIn("记忆召回", unit["terms"])
            self.assertNotIn("privateinsightleaktoken", json.dumps(recall, ensure_ascii=False))

            graph = load_json(os.path.join(memory_dir, "memory-graph.json"))
            edges = {
                (edge["source"], edge["relation"], edge["target"])
                for edge in graph["edges"]
                if edge["source"] == "insight-index-demo"
            }
            self.assertIn(
                ("insight-index-demo", "derived_from", "session:sess-first"),
                edges,
            )
            self.assertIn(
                ("insight-index-demo", "reinforced_by", "session:sess-second"),
                edges,
            )
            self.assertIn(
                ("insight-index-demo", "supports", "decision-formal-demo"),
                edges,
            )
            self.assertIn(
                ("insight-index-demo", "operationalized_as", "workflow-demo"),
                edges,
            )
            self.assertIn(
                ("insight-index-demo", "related_to", "insight-related-demo"),
                edges,
            )
            applies = [edge for edge in edges if edge[1] == "applies_to"]
            self.assertEqual(len(applies), 2)
            self.assertTrue(all(edge[2].startswith("concept:") for edge in applies))

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
            self.assertEqual(result["graph_unbound_evidence"], 0)
            self.assertEqual(result["graph_missing_memory_nodes"], 0)

            recall = load_json(recall_path)
            graph = load_json(graph_path)
            self.assertEqual(
                result["graph_generation_id"],
                recall["generation_id"],
            )
            self.assertEqual(graph["generation_id"], recall["generation_id"])
            unit_types = {unit["type"] for unit in recall["units"]}
            self.assertEqual(recall["schema_version"], "2.0")
            self.assertIn("decision", unit_types)
            self.assertIn("error", unit_types)
            self.assertIn("preference", unit_types)
            self.assertIn("skill", unit_types)
            self.assertIn("workflow", unit_types)
            self.assertNotIn("memory-candidate", unit_types)
            self.assertNotIn("skill-preference", unit_types)
            self.assertNotIn("workflow-candidate", unit_types)
            self.assertNotIn("session", unit_types)
            self.assertNotIn("decisions", unit_types)
            self.assertNotIn("pitfalls", unit_types)
            self.assertIn("obsidian", recall["terms"])
            self.assertIn("技能路由", recall["terms"])
            self.assertIn("流程记忆", recall["terms"])
            self.assertFalse(
                any(unit["summary"].startswith("| 2026-") for unit in recall["units"])
            )
            self.assertTrue(
                all(
                    unit.get("id")
                    and unit.get("revision")
                    and unit.get("status") == "active"
                    and unit.get("scope") in {"global", "project"}
                    and unit.get("source_refs")
                    for unit in recall["units"]
                )
            )
            matching_decisions = [
                unit
                for unit in recall["units"]
                if unit["type"] == "decision"
                and unit["title"] == "保留 Obsidian Markdown 作为主存储"
            ]
            self.assertEqual(len(matching_decisions), 1)
            self.assertEqual(len(matching_decisions[0]["source_refs"]), 2)

            node_types = {node["type"] for node in graph["nodes"]}
            memory_kinds = {
                node["kind"]
                for node in graph["nodes"]
                if node["type"] == "memory"
            }
            edge_relations = {edge["relation"] for edge in graph["edges"]}
            self.assertEqual(graph["schema_version"], "3.0")
            self.assertIn("project", node_types)
            self.assertIn("memory", node_types)
            self.assertIn("note", node_types)
            self.assertIn("decision", memory_kinds)
            self.assertIn("skill", memory_kinds)
            self.assertIn("workflow", memory_kinds)
            self.assertIn("belongs_to", edge_relations)
            self.assertIn("recorded_in", edge_relations)
            personal_node = next(
                node
                for node in graph["nodes"]
                if node["id"] == "note:05-Agent-Memory/personal-memory"
            )
            self.assertEqual(personal_node["type"], "note")
            self.assertEqual(personal_node["kind"], "personal-memory")

    def test_rebuild_does_not_follow_predictable_index_temp_symlinks(self):
        with tempfile.TemporaryDirectory() as vault:
            output_dir = os.path.join(vault, "05-Agent-Memory")
            os.makedirs(output_dir)
            outside = os.path.join(vault, "outside.md")
            write_text(outside, "keep\n")
            output_names = (
                "keyword-index.json",
                "keyword-index.md",
                "global-atoms.json",
                "global-atoms.md",
                "recall-index.json",
                "memory-graph.json",
                "memory-graph-quality.md",
                "recall-context.md",
            )
            for name in output_names:
                os.symlink(outside, os.path.join(output_dir, name + ".tmp"))

            rebuild_vault_knowledge_indexes({"vault_path": vault})

            self.assertEqual(read_text(outside), "keep\n")
            for name in output_names:
                with self.subTest(name=name):
                    self.assertTrue(os.path.isfile(os.path.join(output_dir, name)))
                    self.assertFalse(os.path.islink(os.path.join(output_dir, name)))

    def test_recall_index_never_emits_runtime_units_from_session_paths(self):
        with tempfile.TemporaryDirectory() as vault:
            session_path = os.path.join(
                vault,
                "01-Projects/demo/Memory/sessions/session-only.md",
            )
            os.makedirs(os.path.dirname(session_path), exist_ok=True)
            write_text(
                session_path,
                """---
session_id: session-only
date: '2026-07-12'
project: demo
summary_type: session
decisions_made:
- id: decision-session-only
  revision: forged-session-revision
  text: Session-only decision must remain evidence
  context: Historical sessions are not runtime memory
  status: active
  scope: project
  source_refs: [session:session-only]
---

# Session evidence
""",
            )

            rebuild_vault_knowledge_indexes({"vault_path": vault})

            recall = load_json(
                os.path.join(vault, "05-Agent-Memory/recall-index.json")
            )
            self.assertFalse(
                any(
                    "/memory/sessions/"
                    in "/" + unit.get("path", "").replace("\\", "/").lower().lstrip("/")
                    for unit in recall["units"]
                )
            )

    def test_codex_profile_documents_are_not_indexed_as_user_memory(self):
        with tempfile.TemporaryDirectory() as vault:
            write_fixture_vault(vault)
            profile_skill = os.path.join(
                vault,
                "05-Agent-Memory/codex-profile/skills/demo/SKILL.md",
            )
            os.makedirs(os.path.dirname(profile_skill), exist_ok=True)
            write_text(
                profile_skill,
                "# Internal Skill\n\nprofilepollutiontoken should never be recalled.\n",
            )

            rebuild_vault_knowledge_indexes({"vault_path": vault})

            recall = load_json(
                os.path.join(vault, "05-Agent-Memory/recall-index.json")
            )
            self.assertFalse(
                any(
                    unit["path"].startswith("05-Agent-Memory/codex-profile/")
                    for unit in recall["units"]
                )
            )
            self.assertNotIn("profilepollutiontoken", recall["terms"])

    def test_recall_index_recursively_excludes_unmet_required_memories(self):
        base = aggregate_record(
            "decision-base",
            "decision",
            "基础记忆",
            "基础记忆已经被用户撤回",
            status="retracted",
        )
        dependent = aggregate_record(
            "decision-dependent",
            "decision",
            "一级依赖记忆",
            "依赖基础记忆",
            requires=["decision-base"],
        )
        transitive = aggregate_record(
            "decision-transitive",
            "decision",
            "二级依赖记忆",
            "依赖一级记忆",
            requires=["decision-dependent"],
        )
        independent = aggregate_record(
            "decision-independent",
            "decision",
            "独立记忆",
            "拥有独立证据，不依赖被撤回内容",
        )
        note = {
            "path": "01-Projects/demo/Memory/decisions",
            "title": "Decisions",
            "project": "demo",
            "type": "decisions",
            "text": "",
            "frontmatter": {
                "project": "demo",
                "schema_version": "2.0",
                "decisions": [base, dependent, transitive, independent],
            },
            "body": "# Decisions\n",
            "links": [],
        }

        recall = build_recall_index([note])

        self.assertEqual(
            {unit["id"] for unit in recall["units"]},
            {"decision-independent"},
        )
        self.assertEqual(
            recall["suppressed_dependencies"],
            {
                "decision-dependent": ["decision-base"],
                "decision-transitive": ["decision-dependent"],
            },
        )

    def test_memory_graph_v3_normalizes_nodes_and_binds_edge_evidence(self):
        unit = normalize_formal_record(
            {
                "id": "decision-graph-contract",
                "type": "decision",
                "status": "active",
                "project": "demo",
                "scope": "project",
                "title": "图谱关系必须绑定来源版本",
                "summary": "派生关系不能脱离正式记忆证据",
                "date": "2026-07-26",
                "source_refs": ["session:graph-contract"],
            },
            memory_type="decision",
            default_project="demo",
            source_ref="",
        )
        unit.update(
            {
                "path": "01-Projects/demo/Memory/decisions",
                "source_note": "note:01-Projects/demo/Memory/decisions",
                "terms": ["图谱", "关系", "来源", "版本"],
                "aliases": [],
            }
        )
        note = {
            "path": "01-Projects/demo/Memory/decisions",
            "title": "Decisions",
            "project": "demo",
            "type": "decisions",
            "frontmatter": {"schema_version": "2.0", "project": "demo"},
            "body": "# Decisions\n",
            "links": [],
        }

        graph = build_memory_graph(
            [note],
            {"schema_version": "2.0", "units": [unit]},
        )

        self.assertEqual(graph["schema_version"], "3.0")
        nodes = {node["id"]: node for node in graph["nodes"]}
        self.assertEqual(nodes[unit["id"]]["type"], "memory")
        self.assertEqual(nodes[unit["id"]]["kind"], "decision")
        self.assertEqual(nodes[unit["id"]]["revision"], unit["revision"])
        self.assertEqual(nodes[unit["source_note"]]["type"], "note")
        self.assertEqual(nodes[unit["source_note"]]["kind"], "decisions")
        recorded = next(
            edge
            for edge in graph["edges"]
            if edge["source"] == unit["id"]
            and edge["relation"] == "recorded_in"
        )
        self.assertEqual(recorded["confidence"], 1.0)
        self.assertEqual(
            recorded["evidence"],
            [
                {
                    "source_ref": unit["source_note"],
                    "source_revision": unit["revision"],
                    "observed_at": "2026-07-26",
                    "derivation": "formal-record",
                }
            ],
        )

    def test_memory_graph_emits_declared_relations_for_non_insight_memory(self):
        source = normalize_formal_record(
            {
                "id": "decision-semantic-source",
                "status": "active",
                "project": "demo",
                "scope": "project",
                "title": "正式记忆显式声明语义关系",
                "summary": "图谱只派生有来源约束的关系",
                "date": "2026-07-26",
                "source_refs": ["session:semantic-source"],
                "supports": ["project_rule-supported"],
                "operationalized_as": ["workflow-implementation"],
                "related_to": ["preference-related"],
                "contradicts": ["decision-conflicting"],
            },
            memory_type="decision",
            default_project="demo",
        )
        source.update(
            {
                "path": "01-Projects/demo/Memory/decisions",
                "source_note": "note:01-Projects/demo/Memory/decisions",
                "terms": ["正式记忆", "语义关系"],
                "aliases": [],
            }
        )

        graph = build_memory_graph(
            [],
            {"schema_version": "2.0", "units": [source]},
        )

        edges = {
            (edge["source"], edge["relation"], edge["target"])
            for edge in graph["edges"]
        }
        self.assertTrue(
            {
                (
                    "decision-semantic-source",
                    "supports",
                    "project_rule-supported",
                ),
                (
                    "decision-semantic-source",
                    "operationalized_as",
                    "workflow-implementation",
                ),
                (
                    "decision-semantic-source",
                    "related_to",
                    "preference-related",
                ),
                (
                    "decision-semantic-source",
                    "contradicts",
                    "decision-conflicting",
                ),
            }.issubset(edges)
        )
        relation_edges = [
            edge
            for edge in graph["edges"]
            if edge["source"] == source["id"]
            and edge["relation"]
            in {
                "supports",
                "operationalized_as",
                "related_to",
                "contradicts",
            }
        ]
        self.assertTrue(
            all(
                edge["evidence"][0]["source_revision"] == source["revision"]
                and edge["evidence"][0]["derivation"] == "formal-record"
                for edge in relation_edges
            )
        )

    def test_rebuild_writes_memory_graph_quality_report(self):
        with tempfile.TemporaryDirectory() as vault:
            write_fixture_vault(vault)

            result = rebuild_vault_knowledge_indexes({"vault_path": vault})

            report_path = os.path.join(
                vault,
                "05-Agent-Memory",
                "memory-graph-quality.md",
            )
            self.assertTrue(os.path.exists(report_path))
            self.assertIn(report_path, result["written"])
            report = read_text(report_path)
            self.assertIn("schema_version: '3.0'", report)
            self.assertIn("invalid_edges: 0", report)
            self.assertIn("missing_evidence: 0", report)
            self.assertIn("stale_revision_edges: 0", report)


def write_fixture_vault(vault):
    session_path = os.path.join(
        vault,
        "01-Projects/demo/Memory/sessions/2026-07-04-obsidian-recall.md",
    )
    personal_path = os.path.join(vault, "05-Agent-Memory/personal-memory.md")
    decisions_path = os.path.join(vault, "01-Projects/demo/Memory/decisions.md")
    pitfalls_path = os.path.join(vault, "01-Projects/demo/Memory/pitfalls.md")
    skill_rules_path = os.path.join(vault, "05-Agent-Memory/skill-routing-rules.md")
    workflow_rules_path = os.path.join(vault, "05-Agent-Memory/workflow-rules.md")
    skill_candidate_path = os.path.join(
        vault,
        "04-Feedback/_skill-preferences/技能偏好 humanizer - 中文表达自然化.md",
    )
    workflow_candidate_path = os.path.join(
        vault,
        "04-Feedback/_workflow-candidates/流程记忆 GitHub 项目先查源码.md",
    )
    os.makedirs(os.path.dirname(session_path), exist_ok=True)
    os.makedirs(os.path.dirname(personal_path), exist_ok=True)
    os.makedirs(os.path.dirname(skill_candidate_path), exist_ok=True)
    os.makedirs(os.path.dirname(workflow_candidate_path), exist_ok=True)
    decision = aggregate_record(
        "decision-formal-demo",
        "decision",
        "保留 Obsidian Markdown 作为主存储",
        "用户需要中文可读、可手动检查的长期记忆",
    )
    write_text(
        decisions_path,
        markdown_document(
            {
                "project": "demo",
                "schema_version": "2.0",
                "decisions": [decision],
            },
            "# Decisions\n",
        ),
    )
    pitfall = aggregate_record(
        "error-formal-demo",
        "error",
        "path-filesystem",
        "修复 Obsidian 本地路径误链接",
    )
    write_text(
        pitfalls_path,
        markdown_document(
            {
                "project": "demo",
                "schema_version": "2.0",
                "pitfalls": [pitfall],
            },
            "# Pitfalls\n",
        ),
    )
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
        skill_rules_path,
        markdown_document(
            {
                "title": "Skill Routing Rules",
                "generated_by": "skill_preference_learner.py",
                "summary_type": "skill-routing-rules",
                "schema_version": "2.0",
            },
            "# Skill Routing Rules\n\n"
            + render_skill_rule(
                {
                    "memory_id": "skillpref-demo",
                    "skill_name": "humanizer",
                    "task_intent": "把已有中文文本改得更自然、更像真人表达",
                    "why_skill_fits": "技能路由中，humanizer 适合在用户明确要求去 AI 味、保留事实但调整中文表达时使用。",
                    "positive_signals": ["自然一点", "说人话", "不像 AI"],
                    "negative_signals": ["用户只是要求翻译", "用户要求保留正式学术语气"],
                    "project": "demo",
                    "source_ids": ["sess-skill-1"],
                    "confidence": 0.76,
                    "seen_count": 2,
                }
            ),
        ),
    )
    write_text(
        workflow_rules_path,
        markdown_document(
            {
                "title": "Workflow Rules",
                "generated_by": "workflow_memory.py",
                "summary_type": "workflow-rules",
                "schema_version": "2.0",
            },
            "# Workflow Rules\n\n"
            + render_workflow_rule(
                {
                    "memory_id": "workflow-demo",
                    "rule_name": "github_source_first",
                    "trigger_scene": "用户提供 GitHub skill、插件、仓库、项目截图或名称，并要求解释或借鉴。",
                    "desired_behavior": "先打开 upstream GitHub，阅读 README、目录结构、关键源码或 manifest，再给结论。",
                    "why_it_matters": "流程记忆要求先查源码，避免只根据名称猜用途。",
                    "positive_signals": ["GitHub", "README", "源码"],
                    "negative_signals": ["用户明确说不要联网", "用户只要求根据本地文件分析"],
                    "project": "demo",
                    "source_ids": ["sess-workflow-1"],
                    "confidence": 0.76,
                    "seen_count": 2,
                }
            ),
        ),
    )
    write_text(
        skill_candidate_path,
        """---
memory_id: skillpref-candidate
status: candidate
type: skill_preference
skill_name: humanizer
task_intent: 把已有中文文本改得更自然
artifact_type: 中文文本、说明或回复
pain_point: 普通改写可能仍保留模板感
why_skill_fits: humanizer 专门用于去 AI 味和说人话
positive_signals:
- 自然一点
- 说人话
negative_signals:
- 用户只是要求翻译
evidence_excerpt: 用 humanizer 说人话一点
seen_count: 1
confidence: 0.58
source_session: sess-skill-1
project: demo
last_seen: '2026-07-06T10:00:00+08:00'
---

# 技能偏好: humanizer - 中文表达自然化

## Related

- [[00-Inbox/Agent Memory Index|Agent Memory Index]]
- [[05-Agent-Memory/skill-routing-rules|Skill Routing Rules]]
- [[01-Projects/demo/Memory/decisions|demo]]
""",
    )
    write_text(
        workflow_candidate_path,
        """---
memory_id: workflow-candidate
status: candidate
type: workflow_memory
rule_name: github_source_first
trigger_scene: 用户提供 GitHub skill、插件、仓库、项目截图或名称
user_correction: 不要根据名字猜，先看 README 和源码
desired_behavior: 先打开 upstream GitHub，阅读 README、目录结构、关键源码或 manifest，再给结论
why_it_matters: 只根据名称判断容易误判项目用途
positive_signals:
- GitHub
- README
- 源码
negative_signals:
- 用户明确说不要联网
evidence_excerpt: 你先去 GitHub 看一下原代码，不要根据名字猜
seen_count: 1
confidence: 0.58
source_session: sess-workflow-1
project: demo
last_seen: '2026-07-06T10:00:00+08:00'
---

# 流程记忆: GitHub 项目先查源码

## Related

- [[00-Inbox/Agent Memory Index|Agent Memory Index]]
- [[05-Agent-Memory/workflow-rules|Workflow Rules]]
- [[01-Projects/demo/Memory/decisions|demo]]
""",
    )
    write_text(
        personal_path,
        markdown_document(
            {
                "title": "Personal Memory",
                "generated_by": "memory_judge.py",
                "schema_version": "2.0",
            },
            "# Personal Memory\n\n"
            + render_formal_memory_entry(
                {
                    "memory_id": "preference-demo",
                    "title": "用户偏好: 中文说明",
                    "content": "用户偏好中文解释和清晰步骤",
                    "type": "preference",
                    "source_ids": ["sess-personal-1"],
                }
            )
            + "\n## 项目规则: | 2026-07-04 | demo |\n\n"
            "- id: `bad-table-memory`\n"
            "- type: `project_rule`\n"
            "- project: [[01-Projects/demo/Memory/decisions|demo]]\n"
            "- memory: | 2026-07-04 | demo | 这是旧索引表格行，不应该进入 recall |\n",
        ),
    )


def conversation_summary_note(
    session_id,
    summary,
    *,
    path="01-Projects/demo/Memory/sessions/summary-session",
    project="demo",
    updated_at="2026-07-31T10:00:00+08:00",
    cursor="",
    structured=True,
):
    title = "滚动会话摘要"
    if structured:
        summary_body = canonical_summary_text(
            {
                "project": project,
                "current_goal": "完成滚动会话摘要索引",
                "topics": ["会话摘要", "派生索引"],
                "progress": ["已保存最新摘要"],
                "constraints": ["不得进入正式记忆"],
                "important_context": [],
                "open_items": [],
                "summary": summary,
            }
        )
    else:
        summary_body = summary
    frontmatter = {
        "session_id": session_id,
        "date": "2026-07-31",
        "project": project,
        "ai_title": title,
        "summary_type": "session",
        "summary_updated_at": updated_at,
    }
    if cursor:
        frontmatter["summary_source_cursor"] = cursor
    body = f"# {title}\n\n## Session Summary\n{summary_body}\n"
    return {
        "path": path,
        "title": title,
        "project": project,
        "type": "session",
        "text": body,
        "frontmatter": frontmatter,
        "body": body,
        "links": [],
    }


def aggregate_record(
    memory_id,
    memory_type,
    title,
    summary,
    *,
    project="demo",
    status="active",
    requires=None,
    superseded_by="",
    authority=None,
):
    normalized = normalize_formal_record(
        {
            "id": memory_id,
            "project": project,
            "scope": "project",
            "status": status,
            "title": title,
            "summary": summary,
            "date": "2026-07-04",
            "requires": list(requires or []),
            "superseded_by": superseded_by,
            **dict(authority or {}),
        },
        memory_type=memory_type,
        default_project=project,
        source_ref=f"session:{memory_id}",
    )
    result = {
        "id": normalized["id"],
        "revision": normalized["revision"],
        "status": normalized["status"],
        "project": normalized["project"],
        "scope": normalized["scope"],
        "date": normalized["date"],
        "source_refs": normalized["source_refs"],
    }
    if normalized.get("requires"):
        result["requires"] = normalized["requires"]
    if normalized.get("superseded_by"):
        result["superseded_by"] = normalized["superseded_by"]
    for key in (
        "authority_role",
        "authority_owner",
        "canonical_source",
        "enforced_by",
        "verification_refs",
        "verified_at",
        "freshness_policy",
    ):
        if key in normalized:
            result[key] = normalized[key]
    if memory_type == "decision":
        result.update({"text": title, "context": summary})
    else:
        result.update({"type": title, "resolution": summary})
    return result


def markdown_document(frontmatter, body):
    return (
        "---\n"
        + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + body
    )


def write_text(path, content):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


if __name__ == "__main__":
    unittest.main()
