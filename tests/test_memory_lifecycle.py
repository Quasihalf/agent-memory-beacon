import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from memory_lifecycle import (
    LifecycleConflict,
    LifecycleError,
    LifecyclePreconditionError,
    apply_transition,
    create_proposal,
    find_records,
    plan_transition,
    sweep_expired,
)
from memory_schema import memory_revision, normalize_formal_record
from knowledge_index import rebuild_vault_knowledge_indexes


CST = timezone(timedelta(hours=8))


class MemoryLifecycleTests(unittest.TestCase):
    def test_insight_store_participates_in_explicit_lifecycle_transitions(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            source, record = write_insight_memory(vault)

            found = find_records(cfg, memory_id=record["id"])

            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].memory_type, "insight")
            plan = plan_transition(
                cfg,
                "retract",
                record["id"],
                "用户明确撤回该启发",
                expected_revision=record["revision"],
            )
            result = apply_transition(
                cfg,
                plan,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            content = read_text(source)
            self.assertEqual(result.after_status, "retracted")
            self.assertIn("- status: `retracted`", content)
            self.assertIn("- retracted_reason: 用户明确撤回该启发", content)
            self.assertIn("### Transfer\n\n- 记忆召回", content)

    def test_find_records_uses_custom_adaptive_formal_filename(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            original = write_personal_memory(
                vault,
                "preference-custom-lifecycle",
                status="active",
            )
            custom = os.path.join(vault, "06-Custom", "preferences.md")
            os.makedirs(os.path.dirname(custom))
            os.replace(original, custom)
            cfg["personal_memory"]["formal_path"] = "06-Custom/preferences.md"

            records = find_records(
                cfg,
                memory_id="preference-custom-lifecycle",
            )

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].path, custom)

    def test_restore_allows_active_memory_suppressed_by_inactive_dependency(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            dependency = aggregate_record(
                "decision-inactive-dependency",
                "decision",
                "旧依赖",
                "依赖已经撤回",
                status="retracted",
            )
            target = aggregate_record(
                "decision-dependent-restore",
                "decision",
                "依赖旧决定的规则",
                "恢复后仍应保持可审计但不进入召回",
                status="retracted",
                requires=[dependency["id"]],
            )
            source = write_project_records(vault, decisions=[dependency, target])
            plan = plan_transition(
                cfg,
                "restore",
                target["id"],
                "用户确认恢复记录但依赖仍未恢复",
                expected_revision=target["revision"],
            )

            result = apply_transition(
                cfg,
                plan,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            restored = by_id(read_project_records(source, "decisions"), target["id"])
            self.assertEqual(result.after_status, "active")
            self.assertEqual(restored["status"], "active")
            recall = json.loads(
                read_text(os.path.join(vault, "05-Agent-Memory/recall-index.json"))
            )
            self.assertIn(target["id"], recall["suppressed_dependencies"])

    def test_find_records_searches_project_and_adaptive_formal_stores(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            write_project_records(
                vault,
                decisions=[
                    aggregate_record(
                        "decision-project",
                        "decision",
                        "使用稳定运行目录",
                        "Hook 不依赖开发仓库",
                    )
                ],
            )
            write_personal_memory(vault, "preference-language", status="active")

            all_records = find_records(cfg)
            queried = find_records(cfg, query="中文")

            self.assertEqual(
                {item.memory_id for item in all_records},
                {
                    "decision-project",
                    "preference-language",
                    "preference-unrelated",
                },
            )
            self.assertEqual(
                [item.memory_id for item in queried],
                ["preference-language"],
            )
            self.assertEqual(find_records(cfg, memory_id="missing"), [])

    def test_retract_requires_exact_revision_and_updates_only_named_record(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            first = aggregate_record(
                "decision-first",
                "decision",
                "第一条决定",
                "需要撤回",
            )
            second = aggregate_record(
                "decision-second",
                "decision",
                "第二条决定",
                "应保持有效",
            )
            source = write_project_records(vault, decisions=[first, second])
            before = read_bytes(source)

            with self.assertRaises(LifecyclePreconditionError):
                plan_transition(
                    cfg,
                    "retract",
                    "decision-first",
                    "用户明确撤回",
                    expected_revision="0" * 64,
                )
            self.assertEqual(read_bytes(source), before)

            plan = plan_transition(
                cfg,
                "retract",
                "decision-first",
                "用户明确撤回",
                expected_revision=first["revision"],
            )
            result = apply_transition(
                cfg,
                plan,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            records = read_project_records(source, "decisions")
            updated = by_id(records, "decision-first")
            untouched = by_id(records, "decision-second")
            self.assertEqual(updated["status"], "retracted")
            self.assertEqual(updated["retracted_reason"], "用户明确撤回")
            self.assertNotEqual(updated["revision"], first["revision"])
            self.assertEqual(untouched, second)
            self.assertEqual(result.after_status, "retracted")
            self.assertTrue(os.path.exists(result.rollback_manifest))
            self.assertIn("decision-first", read_text(audit_path(vault)))

    def test_apply_rejects_empty_rebuilder_chain_before_mutation(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            record = aggregate_record(
                "decision-empty-rebuilders",
                "decision",
                "必须重建派生状态",
                "空链不能绕过验证",
            )
            source = write_project_records(vault, decisions=[record])
            before = read_bytes(source)
            plan = plan_transition(
                cfg,
                "retract",
                record["id"],
                "用户撤回",
                expected_revision=record["revision"],
            )

            with self.assertRaisesRegex(ValueError, "rebuilder"):
                apply_transition(cfg, plan, rebuilders=[])

            self.assertEqual(read_bytes(source), before)
            self.assertFalse(os.path.exists(audit_path(vault)))

    def test_supersede_requires_compatible_active_replacement(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            old = aggregate_record(
                "decision-old",
                "decision",
                "使用开发仓库运行",
                "旧方案",
            )
            replacement = aggregate_record(
                "decision-new",
                "decision",
                "使用稳定目录运行",
                "新方案",
            )
            incompatible = aggregate_record(
                "error-new",
                "error",
                "path-filesystem",
                "错误类型不能替代决定",
            )
            source = write_project_records(
                vault,
                decisions=[old, replacement],
                pitfalls=[incompatible],
            )

            with self.assertRaises(LifecycleConflict):
                plan_transition(
                    cfg,
                    "supersede",
                    "decision-old",
                    "用户采用新方案",
                    replacement_id="error-new",
                    expected_revision=old["revision"],
                )

            plan = plan_transition(
                cfg,
                "supersede",
                "decision-old",
                "用户采用新方案",
                replacement_id="decision-new",
                expected_revision=old["revision"],
            )
            apply_transition(
                cfg,
                plan,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            updated = by_id(read_project_records(source, "decisions"), "decision-old")
            self.assertEqual(updated["status"], "superseded")
            self.assertEqual(updated["superseded_by"], "decision-new")

    def test_supersede_rejects_replacement_suppressed_by_dependency_chain(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            old = aggregate_record(
                "decision-old-chain",
                "decision",
                "旧方案",
                "仍被桥接记忆依赖",
            )
            bridge = aggregate_record(
                "decision-bridge-chain",
                "decision",
                "桥接方案",
                "依赖旧方案",
                requires=[old["id"]],
            )
            replacement = aggregate_record(
                "decision-new-chain",
                "decision",
                "新方案",
                "通过桥接方案间接依赖旧方案",
                requires=[bridge["id"]],
            )
            source = write_project_records(
                vault,
                decisions=[old, bridge, replacement],
            )
            before = read_bytes(source)

            with self.assertRaises(LifecycleConflict):
                plan_transition(
                    cfg,
                    "supersede",
                    old["id"],
                    "用户采用新方案",
                    replacement_id=replacement["id"],
                    expected_revision=old["revision"],
                )

            self.assertEqual(read_bytes(source), before)

    def test_restore_allows_retracted_or_expired_but_not_live_supersession(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            retracted = aggregate_record(
                "decision-retracted",
                "decision",
                "可恢复记忆",
                "用户曾撤回",
                status="retracted",
                retracted_reason="先前撤回",
            )
            successor = aggregate_record(
                "decision-successor",
                "decision",
                "当前替代记忆",
                "仍然有效",
            )
            superseded = aggregate_record(
                "decision-superseded",
                "decision",
                "旧替代记忆",
                "已被新记忆替代",
                status="superseded",
                superseded_by="decision-successor",
            )
            source = write_project_records(
                vault,
                decisions=[retracted, successor, superseded],
            )

            with self.assertRaises(LifecycleConflict):
                plan_transition(
                    cfg,
                    "restore",
                    "decision-superseded",
                    "用户要求恢复",
                    expected_revision=superseded["revision"],
                )

            plan = plan_transition(
                cfg,
                "restore",
                "decision-retracted",
                "用户明确恢复",
                expected_revision=retracted["revision"],
            )
            apply_transition(
                cfg,
                plan,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            restored = by_id(
                read_project_records(source, "decisions"),
                "decision-retracted",
            )
            self.assertEqual(restored["status"], "active")
            self.assertNotIn("retracted_reason", restored)

    def test_restore_superseded_memory_checks_the_complete_successor_chain(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            active_leaf = aggregate_record(
                "decision-active-leaf",
                "decision",
                "当前有效方案",
                "仍然阻止旧方案恢复",
            )
            active_bridge = aggregate_record(
                "decision-active-bridge",
                "decision",
                "中间替代方案",
                "已被当前方案替代",
                status="superseded",
                superseded_by=active_leaf["id"],
            )
            blocked = aggregate_record(
                "decision-blocked-restore",
                "decision",
                "不可恢复的旧方案",
                "替代链末端仍然有效",
                status="superseded",
                superseded_by=active_bridge["id"],
            )
            inactive_leaf = aggregate_record(
                "decision-inactive-leaf",
                "decision",
                "已撤回的当前方案",
                "不再阻止旧方案恢复",
                status="retracted",
                retracted_reason="用户撤回替代方案",
            )
            inactive_bridge = aggregate_record(
                "decision-inactive-bridge",
                "decision",
                "失效链中的中间方案",
                "末端替代方案已撤回",
                status="superseded",
                superseded_by=inactive_leaf["id"],
            )
            restorable = aggregate_record(
                "decision-restorable",
                "decision",
                "可以恢复的旧方案",
                "整个替代链均已失效",
                status="superseded",
                superseded_by=inactive_bridge["id"],
            )
            source = write_project_records(
                vault,
                decisions=[
                    active_leaf,
                    active_bridge,
                    blocked,
                    inactive_leaf,
                    inactive_bridge,
                    restorable,
                ],
            )

            with self.assertRaises(LifecycleConflict):
                plan_transition(
                    cfg,
                    "restore",
                    blocked["id"],
                    "用户要求恢复",
                    expected_revision=blocked["revision"],
                )

            plan = plan_transition(
                cfg,
                "restore",
                restorable["id"],
                "用户确认恢复旧方案",
                expected_revision=restorable["revision"],
            )
            apply_transition(
                cfg,
                plan,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            restored = by_id(
                read_project_records(source, "decisions"),
                restorable["id"],
            )
            self.assertEqual(restored["status"], "active")
            self.assertNotIn("superseded_by", restored)

    def test_adaptive_markdown_transition_preserves_unrelated_section_bytes(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            path = write_personal_memory(vault, "preference-language", status="active")
            original = read_text(path)
            unrelated = original.split("## 用户偏好: 保持简洁", 1)[1]
            record = find_records(cfg, memory_id="preference-language")[0]

            plan = plan_transition(
                cfg,
                "retract",
                "preference-language",
                "用户明确说明这不是偏好",
                expected_revision=record.revision,
            )
            apply_transition(
                cfg,
                plan,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            updated = read_text(path)
            self.assertIn("- status: `retracted`", updated)
            self.assertIn("- retracted_reason: 用户明确说明这不是偏好", updated)
            self.assertEqual(updated.split("## 用户偏好: 保持简洁", 1)[1], unrelated)
            self.assertEqual(find_records(cfg, memory_id="preference-language")[0].status, "retracted")

    def test_schedule_and_sweep_expire_only_explicit_passed_timestamp(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            now = datetime(2026, 7, 13, 12, 0, tzinfo=CST)
            due = aggregate_record(
                "decision-due",
                "decision",
                "临时决定",
                "只在迁移前有效",
                expires_at="2026-07-13T11:00:00+08:00",
            )
            future = aggregate_record(
                "decision-future",
                "decision",
                "未来到期决定",
                "尚未到期",
                expires_at="2026-07-14T11:00:00+08:00",
            )
            no_expiry = aggregate_record(
                "decision-no-expiry",
                "decision",
                "没有到期时间",
                "程序不能猜测",
            )
            source = write_project_records(
                vault,
                decisions=[due, future, no_expiry],
            )

            preview = sweep_expired(
                cfg,
                now=now,
                apply=False,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )
            self.assertEqual([item.memory_id for item in preview], ["decision-due"])
            self.assertEqual(by_id(read_project_records(source, "decisions"), "decision-due")["status"], "active")

            applied = sweep_expired(
                cfg,
                now=now,
                apply=True,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )
            self.assertEqual([item.memory_id for item in applied], ["decision-due"])
            records = read_project_records(source, "decisions")
            self.assertEqual(by_id(records, "decision-due")["status"], "expired")
            self.assertEqual(by_id(records, "decision-future")["status"], "active")
            self.assertEqual(by_id(records, "decision-no-expiry")["status"], "active")

    def test_automatic_authority_cannot_retract_or_expire_early(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            future = aggregate_record(
                "decision-future-auto",
                "decision",
                "未来才到期",
                "自动流程不能提前修改",
                expires_at="2026-07-14T11:00:00+08:00",
            )
            no_expiry = aggregate_record(
                "decision-no-auto-expiry",
                "decision",
                "没有显式有效期",
                "自动流程没有修改权限",
            )
            write_project_records(vault, decisions=[future, no_expiry])
            now = datetime(2026, 7, 13, 12, 0, tzinfo=CST)

            with self.assertRaises(LifecycleConflict):
                plan_transition(
                    cfg,
                    "retract",
                    "decision-future-auto",
                    "程序推断内容过时",
                    expected_revision=future["revision"],
                    automatic=True,
                    now=now,
                )
            for record in (future, no_expiry):
                with self.subTest(memory_id=record["id"]):
                    with self.assertRaises(LifecycleConflict):
                        plan_transition(
                            cfg,
                            "expire",
                            record["id"],
                            "自动到期",
                            expected_revision=record["revision"],
                            automatic=True,
                            now=now,
                        )

    def test_restoring_expired_memory_clears_old_expiry_timestamp(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            expired = aggregate_record(
                "decision-restore-expired",
                "decision",
                "恢复后的决定",
                "不应在下一次周检再次过期",
                status="expired",
                expired_reason="达到旧有效期",
                expires_at="2026-07-12T11:00:00+08:00",
            )
            source = write_project_records(vault, decisions=[expired])
            plan = plan_transition(
                cfg,
                "restore",
                expired["id"],
                "用户明确恢复且取消旧有效期",
                expected_revision=expired["revision"],
            )

            apply_transition(
                cfg,
                plan,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            restored = by_id(read_project_records(source, "decisions"), expired["id"])
            self.assertEqual(restored["status"], "active")
            self.assertNotIn("expires_at", restored)

    def test_inferred_conflict_creates_proposal_without_formal_mutation(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            record = aggregate_record(
                "decision-proposed",
                "decision",
                "可能过时的决定",
                "需要用户确认",
            )
            source = write_project_records(vault, decisions=[record])
            before = read_bytes(source)

            proposal = create_proposal(
                cfg,
                action="retract",
                memory_id="decision-proposed",
                reason="程序检测到潜在冲突",
                evidence_refs=["session:proposal-test"],
            )

            self.assertEqual(read_bytes(source), before)
            self.assertTrue(os.path.exists(proposal))
            frontmatter = read_frontmatter(proposal)
            self.assertEqual(frontmatter["status"], "pending")
            self.assertEqual(frontmatter["memory_id"], "decision-proposed")
            self.assertNotIn("程序检测到潜在冲突", read_text(audit_path(vault)) if os.path.exists(audit_path(vault)) else "")

    def test_supersede_proposal_names_exact_replacement_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            old = aggregate_record(
                "decision-proposal-old",
                "decision",
                "旧决定",
                "与新决定内容重复",
            )
            replacement = aggregate_record(
                "decision-proposal-new",
                "decision",
                "新决定",
                "保留更完整的原因和验证",
            )
            source = write_project_records(vault, decisions=[old, replacement])
            before = read_bytes(source)

            first = create_proposal(
                cfg,
                action="supersede",
                memory_id=old["id"],
                replacement_id=replacement["id"],
                reason="两条记忆表达同一决定，保留信息更完整的一条",
            )
            second = create_proposal(
                cfg,
                action="supersede",
                memory_id=old["id"],
                replacement_id=replacement["id"],
                reason="两条记忆表达同一决定，保留信息更完整的一条",
            )

            self.assertEqual(first, second)
            self.assertEqual(read_bytes(source), before)
            frontmatter = read_frontmatter(first)
            self.assertEqual(frontmatter["replacement_id"], replacement["id"])
            self.assertEqual(
                frontmatter["replacement_revision"], replacement["revision"]
            )

    def test_rebuild_failure_restores_exact_source_and_does_not_append_audit(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            record = aggregate_record(
                "decision-rollback",
                "decision",
                "需要事务回滚",
                "重建失败时不能留下半状态",
            )
            source = write_project_records(vault, decisions=[record])
            before = read_bytes(source)

            def fail_rebuild(_cfg):
                raise RuntimeError("forced rebuild failure")

            plan = plan_transition(
                cfg,
                "retract",
                "decision-rollback",
                "用户撤回",
                expected_revision=record["revision"],
            )
            with self.assertRaisesRegex(RuntimeError, "forced rebuild failure"):
                apply_transition(cfg, plan, rebuilders=[fail_rebuild])

            self.assertEqual(read_bytes(source), before)
            self.assertFalse(os.path.exists(audit_path(vault)))

    def test_rebuild_failure_restores_preexisting_derived_bytes(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            record = aggregate_record(
                "decision-derived-rollback",
                "decision",
                "恢复派生文件",
                "失败不能留下新索引",
            )
            source = write_project_records(vault, decisions=[record])
            derived = os.path.join(vault, "05-Agent-Memory", "recall-index.json")
            write_text(derived, '{"before": true}\n')
            before_source = read_bytes(source)
            before_derived = read_bytes(derived)
            profile_dir = os.path.join(
                vault,
                "05-Agent-Memory",
                "codex-profile",
            )
            shared_agents = os.path.join(profile_dir, "AGENTS.shared.md")
            write_text(shared_agents, "profile before\n")
            cfg["codex_profile_path"] = profile_dir
            before_profile = read_bytes(shared_agents)

            def mutate_index(_cfg):
                write_text(derived, '{"after": true}\n')
                write_text(shared_agents, "profile after\n")

            def fail_after_mutation(_cfg):
                raise RuntimeError("forced post-index failure")

            plan = plan_transition(
                cfg,
                "retract",
                record["id"],
                "用户撤回",
                expected_revision=record["revision"],
            )
            with self.assertRaisesRegex(RuntimeError, "forced post-index failure"):
                apply_transition(
                    cfg,
                    plan,
                    rebuilders=[mutate_index, fail_after_mutation],
                )

            self.assertEqual(read_bytes(source), before_source)
            self.assertEqual(read_bytes(derived), before_derived)
            self.assertEqual(read_bytes(shared_agents), before_profile)

    def test_rebuild_failure_restores_configured_recall_index(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            cfg["memory_runtime"] = {
                "index_path": "06-Custom/runtime-recall.json"
            }
            record = aggregate_record(
                "decision-custom-index-rollback",
                "decision",
                "恢复自定义召回索引",
                "生命周期失败不能留下半状态",
            )
            source = write_project_records(vault, decisions=[record])
            recall_path = os.path.join(
                vault,
                "06-Custom",
                "runtime-recall.json",
            )
            write_text(recall_path, '{"before": true}\n')
            before_source = read_bytes(source)
            before_recall = read_bytes(recall_path)

            def mutate_custom_index(_cfg):
                write_text(recall_path, '{"after": true}\n')

            def fail_after_mutation(_cfg):
                raise RuntimeError("forced custom index failure")

            plan = plan_transition(
                cfg,
                "retract",
                record["id"],
                "用户撤回",
                expected_revision=record["revision"],
            )
            with self.assertRaisesRegex(
                RuntimeError,
                "forced custom index failure",
            ):
                apply_transition(
                    cfg,
                    plan,
                    rebuilders=[mutate_custom_index, fail_after_mutation],
                )

            self.assertEqual(read_bytes(source), before_source)
            self.assertEqual(read_bytes(recall_path), before_recall)

    def test_rollback_failure_reports_original_and_rollback_errors(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            record = aggregate_record(
                "decision-double-failure",
                "decision",
                "保留双重失败证据",
                "原始异常不能被回滚异常覆盖",
            )
            write_project_records(vault, decisions=[record])

            def fail_rebuild(_cfg):
                raise RuntimeError("forced rebuild failure")

            plan = plan_transition(
                cfg,
                "retract",
                record["id"],
                "用户撤回",
                expected_revision=record["revision"],
            )
            with patch(
                "memory_lifecycle._restore_snapshots",
                side_effect=RuntimeError("forced rollback failure"),
            ):
                with self.assertRaises(LifecycleError) as raised:
                    apply_transition(cfg, plan, rebuilders=[fail_rebuild])

            message = str(raised.exception)
            self.assertIn("forced rebuild failure", message)
            self.assertIn("forced rollback failure", message)

    def test_default_rebuild_removes_inactive_memory_from_runtime_and_context(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            os.makedirs(os.path.join(vault, "00-Rules"))
            record = aggregate_record(
                "decision-default-rebuild",
                "decision",
                "必须从运行时消失的决定",
                "撤回后不能继续注入",
            )
            write_project_records(vault, decisions=[record])
            agents = os.path.join(vault, "AGENTS.md")
            write_text(
                agents,
                """before
<!-- COMPILED:RULES_START -->
<!-- COMPILED:RULES_END -->
<!-- COMPILED:PROJECTS_START -->
<!-- COMPILED:PROJECTS_END -->
after
""",
            )
            cfg["context_targets"] = [agents]
            cfg["skip_git_probe"] = True
            rebuild_vault_knowledge_indexes(cfg)
            self.assertIn(
                record["id"],
                {item["id"] for item in load_json(os.path.join(vault, "05-Agent-Memory", "recall-index.json"))["units"]},
            )

            plan = plan_transition(
                cfg,
                "retract",
                record["id"],
                "用户明确撤回",
                expected_revision=record["revision"],
            )
            apply_transition(cfg, plan)

            runtime_ids = {
                item["id"]
                for item in load_json(
                    os.path.join(vault, "05-Agent-Memory", "recall-index.json")
                )["units"]
            }
            self.assertNotIn(record["id"], runtime_ids)
            self.assertNotIn("必须从运行时消失的决定", read_text(agents))

    def test_runtime_verification_accepts_active_memory_represented_by_alias(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            canonical = aggregate_record(
                "decision-canonical",
                "decision",
                "相同事实",
                "运行时允许精确事实合并",
            )
            alias = aggregate_record(
                "decision-alias",
                "decision",
                "相同事实",
                "运行时允许精确事实合并",
            )
            target = aggregate_record(
                "decision-alias-target",
                "decision",
                "需要撤回的独立事实",
                "触发生命周期核验",
            )
            write_project_records(vault, decisions=[canonical, alias, target])

            def write_merged_runtime(_cfg):
                write_text(
                    os.path.join(vault, "05-Agent-Memory", "recall-index.json"),
                    json.dumps(
                        {
                            "units": [
                                {
                                    "id": canonical["id"],
                                    "aliases": [alias["id"]],
                                }
                            ]
                        }
                    ),
                )

            plan = plan_transition(
                cfg,
                "retract",
                target["id"],
                "用户明确撤回",
                expected_revision=target["revision"],
            )

            apply_transition(cfg, plan, rebuilders=[write_merged_runtime])

    def test_runtime_verification_uses_configured_recall_index(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            cfg["memory_runtime"] = {
                "index_path": "06-Custom/runtime-recall.json"
            }
            target = aggregate_record(
                "decision-custom-runtime-index",
                "decision",
                "使用配置的召回索引",
                "生命周期核验必须读取同一个运行时文件",
            )
            write_project_records(vault, decisions=[target])
            recall_path = os.path.join(
                vault,
                "06-Custom",
                "runtime-recall.json",
            )

            def write_custom_runtime(_cfg):
                write_text(recall_path, json.dumps({"units": []}))

            plan = plan_transition(
                cfg,
                "retract",
                target["id"],
                "用户明确撤回",
                expected_revision=target["revision"],
            )

            result = apply_transition(
                cfg,
                plan,
                rebuilders=[write_custom_runtime],
            )

            self.assertEqual(result.after_status, "retracted")

    def test_runtime_verification_ignores_unrelated_active_memory_omitted_by_index(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            target = aggregate_record(
                "decision-transaction-target",
                "decision",
                "需要撤回的目标",
                "本次生命周期转换必须验证它从运行时消失",
            )
            unrelated = aggregate_record(
                "decision-unrelated-omitted",
                "decision",
                "无关的有效记忆",
                "索引策略可以因独立原因不收录它",
            )
            source = write_project_records(vault, decisions=[target, unrelated])

            def write_target_free_runtime(_cfg):
                write_text(
                    os.path.join(vault, "05-Agent-Memory", "recall-index.json"),
                    json.dumps({"units": []}),
                )

            plan = plan_transition(
                cfg,
                "retract",
                target["id"],
                "用户明确撤回",
                expected_revision=target["revision"],
            )

            apply_transition(cfg, plan, rebuilders=[write_target_free_runtime])

            records = read_project_records(source, "decisions")
            self.assertEqual(by_id(records, target["id"])["status"], "retracted")
            self.assertEqual(by_id(records, unrelated["id"])["status"], "active")

    def test_runtime_verification_rejects_inactive_memory_hidden_in_alias(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            active = aggregate_record(
                "decision-still-active",
                "decision",
                "继续有效的事实",
                "应保留在运行时",
            )
            target = aggregate_record(
                "decision-leaked-alias",
                "decision",
                "需要撤回的事实",
                "不得作为 alias 留在运行时",
            )
            source = write_project_records(vault, decisions=[active, target])
            before = read_bytes(source)

            def write_leaking_runtime(_cfg):
                write_text(
                    os.path.join(vault, "05-Agent-Memory", "recall-index.json"),
                    json.dumps(
                        {
                            "units": [
                                {
                                    "id": active["id"],
                                    "aliases": [target["id"]],
                                }
                            ]
                        }
                    ),
                )

            plan = plan_transition(
                cfg,
                "retract",
                target["id"],
                "用户明确撤回",
                expected_revision=target["revision"],
            )

            with self.assertRaisesRegex(RuntimeError, "inactive or suppressed"):
                apply_transition(cfg, plan, rebuilders=[write_leaking_runtime])
            self.assertEqual(read_bytes(source), before)

    def test_apply_uses_harvester_writer_lock_inside_vault(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            record = aggregate_record(
                "decision-lock",
                "decision",
                "共享写锁",
                "生命周期修改不能和收割并发",
            )
            write_project_records(vault, decisions=[record])
            plan = plan_transition(
                cfg,
                "retract",
                "decision-lock",
                "用户撤回",
                expected_revision=record["revision"],
            )

            from safety import exclusive_file_lock as real_lock

            calls = []

            def recording_lock(path, root=None):
                calls.append((path, root))
                return real_lock(path, root=root)

            with patch("memory_lifecycle.exclusive_file_lock", recording_lock):
                apply_transition(
                    cfg,
                    plan,
                    rebuilders=[rebuild_vault_knowledge_indexes],
                )

            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][1], vault)
            self.assertEqual(
                calls[0][0],
                os.path.join(vault, "04-Feedback", "_logs", "harvester.lock"),
            )

    def test_formal_store_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            outside = os.path.join(root, "outside")
            os.makedirs(os.path.join(vault, "01-Projects"))
            os.makedirs(os.path.join(outside, "Memory"))
            sentinel = os.path.join(outside, "Memory", "decisions.md")
            write_text(sentinel, "SECRET OUTSIDE VAULT\n")
            os.symlink(
                outside,
                os.path.join(vault, "01-Projects", "demo"),
            )
            cfg = fixture_config(vault)

            self.assertEqual(find_records(cfg), [])
            self.assertEqual(read_text(sentinel), "SECRET OUTSIDE VAULT\n")


def fixture_config(vault):
    return {
        "vault_path": vault,
        "agent_memory_path": os.path.join(vault, "05-Agent-Memory"),
        "memory_index_path": os.path.join(vault, "00-Inbox", "Agent Memory Index.md"),
        "context_targets": [],
        "personal_memory": {
            "formal_path": "05-Agent-Memory/personal-memory.md",
        },
        "skill_preferences": {
            "formal_path": "05-Agent-Memory/skill-routing-rules.md",
        },
        "workflow_memory": {
            "formal_path": "05-Agent-Memory/workflow-rules.md",
        },
        "insight_memory": {
            "formal_path": "05-Agent-Memory/insights.md",
        },
        "memory_lifecycle": {
            "proposal_dir": "04-Feedback/_lifecycle-proposals",
            "audit_path": "05-Agent-Memory/lifecycle-audit.md",
            "rollback_dir": "04-Feedback/_rollback/lifecycle",
        },
    }


def aggregate_record(
    memory_id,
    memory_type,
    title,
    summary,
    *,
    status="active",
    superseded_by="",
    retracted_reason="",
    expired_reason="",
    expires_at="",
    requires=None,
):
    raw = {
        "id": memory_id,
        "project": "demo",
        "scope": "project",
        "status": status,
        "title": title,
        "summary": summary,
        "date": "2026-07-13",
        "superseded_by": superseded_by,
        "retracted_reason": retracted_reason,
        "expired_reason": expired_reason,
        "expires_at": expires_at,
        "requires": list(requires or []),
    }
    normalized = normalize_formal_record(
        raw,
        memory_type=memory_type,
        default_project="demo",
        source_ref=f"session:{memory_id}",
    )
    record = {
        "id": normalized["id"],
        "revision": normalized["revision"],
        "status": normalized["status"],
        "project": normalized["project"],
        "scope": normalized["scope"],
        "date": normalized["date"],
        "source_refs": normalized["source_refs"],
        "aliases": [],
    }
    if memory_type == "decision":
        record.update({"text": title, "context": summary})
    else:
        record.update({"type": title, "resolution": summary})
    for key in (
        "superseded_by",
        "retracted_reason",
        "expired_reason",
        "expires_at",
        "requires",
    ):
        if normalized.get(key):
            record[key] = normalized[key]
    return record


def write_project_records(vault, decisions=None, pitfalls=None):
    memory_dir = os.path.join(vault, "01-Projects", "demo", "Memory")
    os.makedirs(memory_dir, exist_ok=True)
    decisions_path = os.path.join(memory_dir, "decisions.md")
    pitfalls_path = os.path.join(memory_dir, "pitfalls.md")
    write_text(
        decisions_path,
        markdown_document(
            {
                "project": "demo",
                "schema_version": "2.0",
                "decisions": list(decisions or []),
            },
            "# Decisions\n\nUnrelated body must remain readable.\n",
        ),
    )
    write_text(
        pitfalls_path,
        markdown_document(
            {
                "project": "demo",
                "schema_version": "2.0",
                "pitfalls": list(pitfalls or []),
            },
            "# Pitfalls\n",
        ),
    )
    return decisions_path


def write_personal_memory(vault, memory_id, status="active"):
    path = os.path.join(vault, "05-Agent-Memory", "personal-memory.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    canonical = {
        "type": "preference",
        "status": status,
        "project": "",
        "scope": "global",
        "title": "用户偏好: 使用中文解释",
        "summary": "复杂内容默认使用中文解释",
        "superseded_by": "",
    }
    first_revision = memory_revision(canonical)
    unrelated = {
        **canonical,
        "title": "用户偏好: 保持简洁",
        "summary": "简单问题直接回答",
    }
    body = f"""# Personal Memory

## 用户偏好: 使用中文解释

- id: `{memory_id}`
- revision: `{first_revision}`
- type: `preference`
- status: `{status}`
- scope: `global`
- project: `global`
- source_refs: `session:{memory_id}`
- memory: 复杂内容默认使用中文解释

## 用户偏好: 保持简洁

- id: `preference-unrelated`
- revision: `{memory_revision(unrelated)}`
- type: `preference`
- status: `active`
- scope: `global`
- project: `global`
- source_refs: `session:preference-unrelated`
- memory: 简单问题直接回答
"""
    write_text(
        path,
        markdown_document(
            {
                "title": "Personal Memory",
                "generated_by": "memory_judge.py",
                "schema_version": "2.0",
            },
            body,
        ),
    )
    return path


def write_insight_memory(vault):
    path = os.path.join(vault, "05-Agent-Memory", "insights.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = normalize_formal_record(
        {
            "id": "insight-lifecycle-seed",
            "status": "active",
            "scope": "project",
            "project": "demo",
            "title": "一次性启发也应被保留",
            "summary": "高价值启发不应以重复作为准入门槛",
            "maturity": "seed",
            "confidence": 0.86,
            "novelty": "把启发价值和重复次数分离",
            "transfer": ["记忆召回", "研究假设"],
            "boundary": "普通进度和随口猜想不适用",
            "origin": "user",
            "source_refs": ["session:insight-source"],
        },
        memory_type="insight",
    )
    body = f"""# Insights

## {record['title']}

- id: `{record['id']}`
- revision: `{record['revision']}`
- status: `active`
- scope: `project`
- maturity: `seed`
- confidence: `0.86`
- origin: `user`
- project: [[01-Projects/demo/Memory/decisions|demo]]
- source_refs: `session:insight-source`

### Insight

{record['summary']}

### Novelty

把启发价值和重复次数分离

### Transfer

- 记忆召回
- 研究假设

### Boundary

普通进度和随口猜想不适用
"""
    write_text(
        path,
        markdown_document(
            {
                "title": "Insights",
                "generated_by": "insight_memory.py",
                "schema_version": "2.0",
            },
            body,
        ),
    )
    return path, record


def markdown_document(frontmatter, body):
    return (
        "---\n"
        + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + body
    )


def read_project_records(path, key):
    return read_frontmatter(path).get(key, [])


def read_frontmatter(path):
    content = read_text(path)
    return yaml.safe_load(content.split("---", 2)[1]) or {}


def by_id(records, memory_id):
    return next(item for item in records if item.get("id") == memory_id)


def audit_path(vault):
    return os.path.join(vault, "05-Agent-Memory", "lifecycle-audit.md")


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def write_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


if __name__ == "__main__":
    unittest.main()
