import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
TESTS_DIR = os.path.join(REPO_ROOT, "tests")
sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, TESTS_DIR)

from knowledge_index import rebuild_vault_knowledge_indexes
from memory_judge import render_formal_memory_entry
from memory_lifecycle import find_records
from memory_relation_batch import (
    RelationBatchError,
    RelationBatchPreconditionError,
    apply_relation_batch,
    preview_relation_batch,
    write_relation_plan,
)
from test_memory_lifecycle import aggregate_record, write_project_records
from workflow_memory import render_formal_rule as render_workflow_rule


class MemoryRelationBatchTests(unittest.TestCase):
    def test_plan_freezes_both_records_and_is_read_only(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            write_formal_fixture(vault)
            source_path = os.path.join(
                vault,
                "05-Agent-Memory",
                "personal-memory.md",
            )
            before = read_bytes(source_path)

            result = write_relation_plan(
                cfg,
                relation_proposals(),
                "04-Feedback/_relation-proposals/semantic-plan.md",
                now=datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc),
            )

            self.assertEqual(read_bytes(source_path), before)
            self.assertEqual(result["action_count"], 1)
            self.assertRegex(result["canonical_sha256"], r"^[0-9a-f]{64}$")
            plan = yaml.safe_load(
                read_text(result["path"]).split("---", 2)[1]
            )
            action = plan["actions"][0]
            self.assertEqual(action["source_id"], "project_rule-source-first")
            self.assertEqual(action["target_id"], "workflow-source-first")
            self.assertEqual(action["relation"], "operationalized_as")
            self.assertRegex(action["source_revision"], r"^[0-9a-f]{64}$")
            self.assertRegex(action["target_revision"], r"^[0-9a-f]{64}$")
            self.assertRegex(action["source_digest"], r"^[0-9a-f]{64}$")
            self.assertRegex(action["target_digest"], r"^[0-9a-f]{64}$")
            self.assertIn(
                "#section[id=project_rule-source-first]",
                action["source_locator"],
            )
            self.assertIn(
                "#section[id=workflow-source-first]",
                action["target_locator"],
            )
            preview = preview_relation_batch(
                cfg,
                result["path"],
                result["canonical_sha256"],
            )
            self.assertEqual(preview["action_count"], 1)
            self.assertFalse(preview["applied"])

    def test_apply_updates_revision_and_relation_then_marks_plan_applied(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            write_formal_fixture(vault)
            result = write_relation_plan(
                cfg,
                relation_proposals(),
                "04-Feedback/_relation-proposals/semantic-plan.md",
            )
            before = one_record(cfg, "project_rule-source-first")

            applied = apply_relation_batch(
                cfg,
                result["path"],
                result["canonical_sha256"],
                apply=True,
                rebuilders=[lambda _cfg: None],
            )

            after = one_record(cfg, "project_rule-source-first")
            self.assertTrue(applied["applied"])
            self.assertNotEqual(after.revision, before.revision)
            self.assertEqual(
                after.record["operationalized_as"],
                ["workflow-source-first"],
            )
            plan = yaml.safe_load(
                read_text(result["path"]).split("---", 2)[1]
            )
            self.assertEqual(plan["approval_status"], "applied")
            self.assertEqual(
                plan["approved_canonical_sha256"],
                result["canonical_sha256"],
            )

    def test_preview_rejects_record_drift_after_plan_generation(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            write_formal_fixture(vault)
            result = write_relation_plan(
                cfg,
                relation_proposals(),
                "04-Feedback/_relation-proposals/semantic-plan.md",
            )
            path = os.path.join(
                vault,
                "05-Agent-Memory",
                "workflow-rules.md",
            )
            content = read_text(path).replace(
                "避免根据名称猜测项目",
                "避免根据名称猜测项目并补充来源验证",
                1,
            )
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)

            with self.assertRaises(RelationBatchPreconditionError):
                preview_relation_batch(
                    cfg,
                    result["path"],
                    result["canonical_sha256"],
                )

    def test_preview_rejects_plan_body_tampering(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            write_formal_fixture(vault)
            result = write_relation_plan(
                cfg,
                relation_proposals(),
                "04-Feedback/_relation-proposals/semantic-plan.md",
            )
            write_text(
                result["path"],
                read_text(result["path"]) + "\n未经审批的正文变更\n",
            )

            with self.assertRaisesRegex(
                RelationBatchPreconditionError,
                "content changed",
            ):
                preview_relation_batch(
                    cfg,
                    result["path"],
                    result["canonical_sha256"],
                )

    def test_apply_updates_aggregate_decision_relation(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            write_formal_fixture(vault)
            decision = aggregate_record(
                "decision-source-first",
                "decision",
                "分析前读取上游源码",
                "避免根据名称猜测项目",
            )
            decisions_path = write_project_records(
                vault,
                decisions=[decision],
            )
            proposals = [
                {
                    "source_id": "decision-source-first",
                    "relation": "supports",
                    "target_id": "workflow-source-first",
                    "reason": "该技术决定支持对应的执行流程",
                    "evidence_refs": [
                        "memory:decision-source-first",
                        "memory:workflow-source-first",
                    ],
                }
            ]
            result = write_relation_plan(
                cfg,
                proposals,
                "04-Feedback/_relation-proposals/aggregate-plan.md",
            )
            before = one_record(cfg, "decision-source-first")

            apply_relation_batch(
                cfg,
                result["path"],
                result["canonical_sha256"],
                apply=True,
                rebuilders=[lambda _cfg: None],
            )

            after = one_record(cfg, "decision-source-first")
            frontmatter = yaml.safe_load(
                read_text(decisions_path).split("---", 2)[1]
            )
            updated = frontmatter["decisions"][0]
            self.assertNotEqual(after.revision, before.revision)
            self.assertEqual(updated["revision"], after.revision)
            self.assertEqual(updated["supports"], ["workflow-source-first"])

    def test_rebuild_failure_restores_sources_and_derived_indexes(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            write_formal_fixture(vault)
            rebuild_vault_knowledge_indexes(cfg)
            memory_index_path = os.path.join(
                vault,
                "00-Inbox",
                "Agent Memory Index.md",
            )
            write_text(memory_index_path, "index-before\n")
            result = write_relation_plan(
                cfg,
                relation_proposals(),
                "04-Feedback/_relation-proposals/semantic-plan.md",
            )
            source_path = os.path.join(
                vault,
                "05-Agent-Memory",
                "personal-memory.md",
            )
            derived_paths = [
                os.path.join(vault, "05-Agent-Memory", "keyword-index.json"),
                os.path.join(vault, "05-Agent-Memory", "keyword-index.md"),
                os.path.join(vault, "05-Agent-Memory", "global-atoms.json"),
                os.path.join(vault, "05-Agent-Memory", "global-atoms.md"),
                os.path.join(vault, "05-Agent-Memory", "recall-index.json"),
                os.path.join(vault, "05-Agent-Memory", "memory-graph.json"),
                os.path.join(
                    vault,
                    "05-Agent-Memory",
                    "memory-graph-quality.md",
                ),
                os.path.join(vault, "05-Agent-Memory", "recall-context.md"),
                memory_index_path,
            ]
            before = {
                path: read_bytes(path)
                for path in [source_path, result["path"], *derived_paths]
            }

            def failing_rebuilder(current_cfg):
                rebuild_vault_knowledge_indexes(current_cfg)
                write_text(memory_index_path, "index-during\n")
                raise RuntimeError("injected relation rebuild failure")

            with self.assertRaisesRegex(
                RelationBatchError,
                "rebuild failure",
            ):
                apply_relation_batch(
                    cfg,
                    result["path"],
                    result["canonical_sha256"],
                    apply=True,
                    rebuilders=[failing_rebuilder],
                )

            self.assertEqual(
                {
                    path: read_bytes(path)
                    for path in [source_path, result["path"], *derived_paths]
                },
                before,
            )


def fixture_config(vault):
    return {
        "vault_path": vault,
        "personal_memory": {
            "formal_path": "05-Agent-Memory/personal-memory.md",
        },
        "workflow_memory": {
            "formal_path": "05-Agent-Memory/workflow-rules.md",
        },
    }


def write_formal_fixture(vault):
    memory_dir = os.path.join(vault, "05-Agent-Memory")
    os.makedirs(memory_dir, exist_ok=True)
    personal = render_formal_memory_entry(
        {
            "memory_id": "project_rule-source-first",
            "title": "分析 GitHub 项目前先读上游源码",
            "content": "不要根据仓库名称猜测用途",
            "type": "project_rule",
            "project": "demo",
            "scope": "project",
            "source_ids": ["source-first"],
            "confidence": 0.9,
            "seen_count": 2,
        }
    )
    workflow = render_workflow_rule(
        {
            "memory_id": "workflow-source-first",
            "rule_name": "github_source_first",
            "trigger_scene": "用户给出 GitHub 仓库并要求分析",
            "desired_behavior": "先阅读 README 和关键源码后再分析",
            "why_it_matters": "避免根据名称猜测项目",
            "positive_signals": ["GitHub", "README"],
            "negative_signals": ["用户明确要求离线"],
            "project": "demo",
            "source_ids": ["source-first"],
            "confidence": 0.9,
            "seen_count": 2,
        }
    )
    write_text(
        os.path.join(memory_dir, "personal-memory.md"),
        "---\nschema_version: '2.0'\nsummary_type: personal-memory\n---\n\n"
        + personal,
    )
    write_text(
        os.path.join(memory_dir, "workflow-rules.md"),
        "---\nschema_version: '2.0'\nsummary_type: workflow-rules\n---\n\n"
        + workflow,
    )


def relation_proposals():
    return [
        {
            "source_id": "project_rule-source-first",
            "relation": "operationalized_as",
            "target_id": "workflow-source-first",
            "reason": "该 Workflow 是项目规则的明确执行形式",
            "evidence_refs": [
                "memory:project_rule-source-first",
                "memory:workflow-source-first",
            ],
        }
    ]


def one_record(cfg, memory_id):
    matches = find_records(cfg, memory_id=memory_id)
    if len(matches) != 1:
        raise AssertionError(f"expected one formal record for {memory_id}")
    return matches[0]


def write_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()
