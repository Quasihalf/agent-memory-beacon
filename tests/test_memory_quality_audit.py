import hashlib
import json
import os
import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from unittest import mock

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import memory_quality_audit
from memory_quality_audit import (
    audit_formal_memories,
    create_quality_proposals,
    write_old_memory_lifecycle_plan,
    write_quality_report,
)
from memory_lifecycle import LifecycleConflict
import memory_lifecycle
from memory_schema import normalize_formal_record
from insight_memory import render_formal_record


class MemoryQualityAuditTests(unittest.TestCase):
    def test_formal_insight_participates_in_revision_and_source_quality_audit(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            cfg["insight_memory"] = {
                "formal_path": "05-Agent-Memory/insights.md",
                "candidate_dir": "04-Feedback/_insight-candidates",
            }
            path = os.path.join(vault, "05-Agent-Memory", "insights.md")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            valid = normalize_formal_record(
                {
                    "id": "insight-audit-valid",
                    "type": "insight",
                    "status": "active",
                    "maturity": "seed",
                    "confidence": 0.76,
                    "origin": "user",
                    "project": "demo",
                    "scope": "project",
                    "title": "质量审计识别正式启发",
                    "summary": "正式 Insight 必须有正确 revision 和来源",
                    "novelty": "把启发纳入统一正式记忆治理",
                    "transfer": ["质量审计"],
                    "boundary": "候选和伪造 revision 不得计入",
                    "source_refs": ["session:audit-insight"],
                },
                memory_type="insight",
                default_project="demo",
                source_ref="",
            )
            forged = render_formal_record({**valid, "id": "insight-audit-forged"})
            forged = re.sub(
                r"(?m)^- revision: `[0-9a-f]{64}`$",
                "- revision: `" + "0" * 64 + "`",
                forged,
            )
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(
                    "---\nschema_version: '2.0'\nsummary_type: insights\n---\n\n"
                    + render_formal_record(valid)
                    + "\n"
                    + forged
                )

            report = audit_formal_memories(cfg)

            self.assertEqual(report["active_counts"].get("insight"), 1)
            self.assertEqual(report["quality_counts"]["insight"]["formal"], 1)
            self.assertNotIn(
                "insight-audit-forged",
                {item["id"] for item in report["low_quality"]},
            )
    def test_main_old_before_writes_plan_and_uses_subset_safe_proposals(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            snapshot = {
                "path": os.path.join(
                    vault,
                    "04-Feedback/_lifecycle-proposals/old-plan.md",
                ),
                "generated_at": "2026-07-19T00:00:00+00:00",
                "cutoff_exclusive": "2026-07-13",
                "canonical_sha256": "a" * 64,
                "old_active_record_count": 3,
                "old_quality_pass_count": 2,
                "old_low_quality_count": 1,
                "old_low_quality_action_count": 1,
                "old_low_quality_without_action_count": 0,
                "recommended_action_count": 1,
                "recommended_actions": [
                    {
                        "action": "retract",
                        "memory_id": "error-old",
                        "expected_revision": "b" * 64,
                        "reason": "旧占位记录",
                    }
                ],
            }
            with mock.patch(
                "config.load_config",
                return_value=cfg,
            ), mock.patch.object(
                memory_quality_audit,
                "write_old_memory_lifecycle_plan",
                return_value=snapshot,
            ) as write_plan, mock.patch.object(
                memory_quality_audit,
                "create_quality_proposals",
                return_value=["/tmp/proposal.md"],
            ) as create_proposals, redirect_stdout(StringIO()) as output:
                result = memory_quality_audit.main(
                    ["--old-before", "2026-07-13", "--propose", "--json"]
                )

            self.assertEqual(result, 0)
            write_plan.assert_called_once_with(cfg, "2026-07-13")
            create_proposals.assert_called_once_with(
                cfg,
                snapshot,
                reconcile="selected",
            )
            payload = json.loads(output.getvalue())
            self.assertEqual(payload["canonical_sha256"], "a" * 64)
            self.assertEqual(payload["proposal_paths"], ["/tmp/proposal.md"])

    def test_main_holds_vault_lock_through_report_snapshot(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            events = []

            @contextmanager
            def fake_lock(path, root=None):
                self.assertEqual(root, vault)
                self.assertTrue(path.endswith("04-Feedback/_logs/harvester.lock"))
                events.append("lock-enter")
                try:
                    yield
                finally:
                    events.append("lock-exit")

            def fake_audit(_cfg):
                self.assertEqual(events, ["lock-enter"])
                events.append("audit")
                return {
                    "active_record_count": 0,
                    "low_quality_count": 0,
                    "duplicate_group_count": 0,
                    "recommended_action_count": 0,
                    "identity_conflicts": [],
                    "recommended_actions": [],
                }

            def fake_write(_cfg, report):
                self.assertEqual(events, ["lock-enter", "audit"])
                self.assertEqual(report["active_record_count"], 0)
                events.append("write")
                return os.path.join(vault, "04-Feedback/memory-quality-report.md")

            with mock.patch(
                "config.load_config",
                return_value=cfg,
            ), mock.patch.object(
                memory_quality_audit,
                "exclusive_file_lock",
                side_effect=fake_lock,
            ), mock.patch.object(
                memory_quality_audit,
                "audit_formal_memories",
                side_effect=fake_audit,
            ), mock.patch.object(
                memory_quality_audit,
                "write_quality_report",
                side_effect=fake_write,
            ):
                result = memory_quality_audit.main(["--write-report"])

            self.assertEqual(result, 0)
            self.assertEqual(
                events,
                ["lock-enter", "audit", "write", "lock-exit"],
            )

    def test_audit_builds_exact_non_mutating_duplicate_and_retract_actions(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            records = [
                aggregate_record(
                    "error-pdf-a",
                    "shell-cli",
                    "本机缺少 pdftotext，改用 pypdf 完成文本校验",
                ),
                aggregate_record(
                    "error-pdf-b",
                    "shell-cli",
                    "系统没有 pdftotext，改用 PyMuPDF 完成文本核对",
                ),
                aggregate_record(
                    "error-auth-unresolved",
                    "api-network",
                    "API Key 无效或无权限，尚未替换凭据",
                ),
            ]
            source = write_pitfalls(vault, records)
            with open(source, "rb") as handle:
                before = handle.read()

            report = audit_formal_memories(cfg)
            actions = report["recommended_actions"]

            self.assertEqual(report["active_counts"]["error"], 3)
            self.assertEqual(len(report["duplicate_groups"]), 1)
            self.assertEqual(
                {item["action"] for item in actions},
                {"supersede", "retract"},
            )
            for item in actions:
                self.assertRegex(item["expected_revision"], r"^[0-9a-f]{64}$")
            supersede = next(item for item in actions if item["action"] == "supersede")
            self.assertIn(supersede["replacement_id"], {"error-pdf-a", "error-pdf-b"})

            report_path = write_quality_report(cfg, report)
            proposal_paths = create_quality_proposals(cfg, report)

            self.assertTrue(os.path.exists(report_path))
            self.assertEqual(len(proposal_paths), 2)
            with open(source, "rb") as handle:
                self.assertEqual(handle.read(), before)

    def test_quality_proposals_reject_a_stale_target_revision(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            original = aggregate_record(
                "error-auth-unresolved",
                "api-network",
                "API Key 无效或无权限，尚未替换凭据",
            )
            write_pitfalls(vault, [original])
            report = audit_formal_memories(cfg)
            self.assertEqual(report["recommended_action_count"], 1)

            updated = aggregate_record(
                "error-auth-unresolved",
                "api-network",
                "API Key 无权限，调整分组权限并重新请求后验证成功",
            )
            write_pitfalls(vault, [updated])

            with self.assertRaises(LifecycleConflict):
                create_quality_proposals(cfg, report)

    def test_quality_proposals_reject_a_stale_replacement_revision(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            records = [
                aggregate_record(
                    "error-pdf-a",
                    "shell-cli",
                    "本机缺少 pdftotext，改用 pypdf 完成文本校验",
                ),
                aggregate_record(
                    "error-pdf-b",
                    "shell-cli",
                    "系统没有 pdftotext，改用 PyMuPDF 完成文本核对",
                ),
            ]
            write_pitfalls(vault, records)
            report = audit_formal_memories(cfg)
            action = report["recommended_actions"][0]
            self.assertEqual(action["action"], "supersede")

            replacement_id = action["replacement_id"]
            updated_records = [
                (
                    aggregate_record(
                        record["id"],
                        record["type"],
                        record["resolution"] + "，并记录工具版本",
                    )
                    if record["id"] == replacement_id
                    else record
                )
                for record in records
            ]
            write_pitfalls(vault, updated_records)

            with self.assertRaises(LifecycleConflict):
                create_quality_proposals(cfg, report)

    def test_quality_proposals_mark_no_longer_recommended_items_stale(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            original = aggregate_record(
                "error-auth-unresolved",
                "api-network",
                "API Key 无效或无权限，尚未替换凭据",
            )
            source = write_pitfalls(vault, [original])
            first_report = audit_formal_memories(cfg)
            first_path = create_quality_proposals(cfg, first_report)[0]
            self.assertEqual(read_frontmatter(first_path)["status"], "pending")

            resolved = aggregate_record(
                "error-auth-unresolved",
                "api-network",
                "API Key 无权限，调整分组权限并重新请求后验证成功",
            )
            write_pitfalls(vault, [resolved])
            second_report = audit_formal_memories(cfg)
            self.assertEqual(second_report["recommended_action_count"], 0)
            self.assertEqual(create_quality_proposals(cfg, second_report), [])

            stale = read_frontmatter(first_path)
            self.assertEqual(stale["status"], "stale")
            self.assertEqual(
                stale["stale_reason"],
                "not_recommended_by_latest_memory_quality_audit",
            )
            self.assertEqual(read_frontmatter(source)["pitfalls"][0]["status"], "active")

    def test_quality_proposals_leave_only_latest_revision_pending(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            first = aggregate_record(
                "error-auth-unresolved",
                "api-network",
                "API Key 无效或无权限，尚未替换凭据",
            )
            write_pitfalls(vault, [first])
            first_path = create_quality_proposals(
                cfg,
                audit_formal_memories(cfg),
            )[0]

            second = aggregate_record(
                "error-auth-unresolved",
                "api-network",
                "API Key 仍无权限，尚未完成凭据授权",
            )
            write_pitfalls(vault, [second])
            second_path = create_quality_proposals(
                cfg,
                audit_formal_memories(cfg),
            )[0]

            self.assertNotEqual(first_path, second_path)
            self.assertEqual(read_frontmatter(first_path)["status"], "stale")
            self.assertEqual(read_frontmatter(second_path)["status"], "pending")

    def test_quality_proposal_can_reactivate_after_exact_recommendation_returns(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            unresolved = aggregate_record(
                "error-auth-unresolved",
                "api-network",
                "API Key 无效或无权限，尚未替换凭据",
            )
            write_pitfalls(vault, [unresolved])
            first_path = create_quality_proposals(
                cfg,
                audit_formal_memories(cfg),
            )[0]

            resolved = aggregate_record(
                "error-auth-unresolved",
                "api-network",
                "API Key 无权限，调整分组权限并重新请求后验证成功",
            )
            write_pitfalls(vault, [resolved])
            create_quality_proposals(cfg, audit_formal_memories(cfg))
            self.assertEqual(read_frontmatter(first_path)["status"], "stale")

            write_pitfalls(vault, [unresolved])
            reactivated_path = create_quality_proposals(
                cfg,
                audit_formal_memories(cfg),
            )[0]

            self.assertEqual(reactivated_path, first_path)
            self.assertEqual(read_frontmatter(first_path)["status"], "pending")

    def test_subset_quality_proposals_do_not_stale_unrelated_pending_proposals(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            write_pitfalls(
                vault,
                [
                    aggregate_record(
                        "error-a-unresolved",
                        "api-network",
                        "API Key 无效或无权限，尚未替换凭据",
                    ),
                    aggregate_record(
                        "error-b-unresolved",
                        "path-filesystem",
                        "目标路径不存在，尚未重新定位真实文件",
                    ),
                ],
            )
            report = audit_formal_memories(cfg)
            proposal_paths = create_quality_proposals(cfg, report)
            self.assertEqual(len(proposal_paths), 2)

            selected = {
                **report,
                "recommended_actions": [
                    {
                        **report["recommended_actions"][0],
                        "evidence_refs": ["session:selected-evidence"],
                    }
                ],
            }
            selected_paths = create_quality_proposals(
                cfg,
                selected,
                reconcile="selected",
            )

            self.assertEqual(
                [read_frontmatter(path)["status"] for path in proposal_paths],
                ["stale", "pending"],
            )
            self.assertEqual(len(selected_paths), 1)
            self.assertEqual(read_frontmatter(selected_paths[0])["status"], "pending")

    def test_old_memory_plan_is_reproducible_and_binds_exact_sources(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            records = [
                aggregate_record(
                    "error-old-unresolved",
                    "api-network",
                    "API Key 无效或无权限，尚未替换凭据",
                    date="2026-07-12",
                ),
                aggregate_record(
                    "error-old-pdf-a",
                    "shell-cli",
                    "本机缺少 pdftotext，改用 pypdf 完成文本校验",
                    date="2026-07-12",
                ),
                aggregate_record(
                    "error-old-pdf-b",
                    "shell-cli",
                    "系统没有 pdftotext，改用 PyMuPDF 完成文本核对",
                    date="2026-07-12",
                ),
                aggregate_record(
                    "error-new-unresolved",
                    "path-filesystem",
                    "目标路径不存在，尚未重新定位真实文件",
                    date="2026-07-13",
                ),
            ]
            source = write_pitfalls(vault, records)
            before = read_bytes(source)
            fixed_now = datetime(2026, 7, 19, 1, 2, 3, tzinfo=timezone.utc)

            result = write_old_memory_lifecycle_plan(
                cfg,
                "2026-07-13",
                now=fixed_now,
            )
            frontmatter = read_frontmatter(result["path"])

            self.assertEqual(result["old_active_record_count"], 3)
            self.assertEqual(result["old_quality_pass_count"], 2)
            self.assertEqual(result["old_low_quality_count"], 1)
            self.assertEqual(result["recommended_action_count"], 2)
            self.assertEqual(
                {item["action"] for item in result["recommended_actions"]},
                {"retract", "supersede"},
            )
            self.assertNotIn(
                "error-new-unresolved",
                {item["memory_id"] for item in result["recommended_actions"]},
            )
            self.assertEqual(frontmatter["canonical_sha256"], result["canonical_sha256"])
            self.assertRegex(result["canonical_sha256"], r"^[0-9a-f]{64}$")
            self.assertEqual(frontmatter["actions"], result["recommended_actions"])
            for action in frontmatter["actions"]:
                self.assertRegex(action["expected_revision"], r"^[0-9a-f]{64}$")
                self.assertRegex(action["source_digest"], r"^[0-9a-f]{64}$")
                self.assertEqual(action["source_digest_scope"], "canonical-record-v1")
                self.assertIn(
                    f"#pitfalls[id={action['memory_id']}]",
                    action["source_locator"],
                )
                self.assertTrue(action["reason"])
                self.assertTrue(action["evidence_refs"])
                if action["action"] == "supersede":
                    self.assertTrue(action["replacement_id"])
                    self.assertRegex(
                        action["replacement_revision"],
                        r"^[0-9a-f]{64}$",
                    )
                    self.assertIn(
                        f"#pitfalls[id={action['replacement_id']}]",
                        action["replacement_source_locator"],
                    )
                    self.assertRegex(
                        action["replacement_source_digest"],
                        r"^[0-9a-f]{64}$",
                    )

            canonical_payload = {
                "schema_version": "1.0",
                "cutoff_exclusive": "2026-07-13",
                "actions": frontmatter["actions"],
            }
            expected_sha = hashlib.sha256(
                json.dumps(
                    canonical_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            self.assertEqual(result["canonical_sha256"], expected_sha)
            self.assertEqual(read_bytes(source), before)

            write_pitfalls(
                vault,
                [
                    aggregate_record(
                        "error-new-unrelated",
                        "other",
                        "新记录写入同一文件，完成验证且不改变旧审批目标",
                        date="2026-07-19",
                    ),
                    *records,
                ],
            )
            repeated = write_old_memory_lifecycle_plan(
                cfg,
                "2026-07-13",
                now=fixed_now,
            )
            self.assertEqual(repeated["canonical_sha256"], result["canonical_sha256"])
            self.assertEqual(
                repeated["recommended_actions"],
                result["recommended_actions"],
            )

    def test_audit_skips_supersede_with_unapproved_active_alias_owner(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            target = aggregate_record(
                "error-alias-a-target",
                "shell-cli",
                "本机缺少 pdftotext，改用 pypdf 完成文本校验",
            )
            replacement = aggregate_record(
                "error-alias-z-replacement",
                "shell-cli",
                "本机缺少 pdftotext，改用 pypdf 完成文本校验",
            )
            alias_owner = aggregate_record(
                "error-alias-owner",
                "path-filesystem",
                "配置路径不存在，重新定位真实文件并完成验证",
            )
            alias_owner["aliases"] = [target["id"], replacement["id"]]
            write_pitfalls(vault, [target, replacement, alias_owner])

            report = audit_formal_memories(cfg)

            self.assertEqual(report["duplicate_group_count"], 1)
            self.assertEqual(report["recommended_action_count"], 0)
            self.assertEqual(report["executable_recommendation_count"], 0)
            self.assertEqual(report["blocked_lifecycle_action_count"], 1)
            blocked = report["blocked_lifecycle_actions"][0]
            self.assertEqual(blocked["action"], "supersede")
            self.assertEqual(blocked["blocked_reason"], "active_alias_owner")
            self.assertEqual(
                blocked["blocking_memory_ids"],
                [alias_owner["id"]],
            )

    def test_audit_stratifies_evidence_insufficient_and_executable_backlog(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            write_pitfalls(
                vault,
                [
                    aggregate_record(
                        "error-needs-evidence",
                        "path-filesystem",
                        "目标路径不存在，重新定位到真实路径",
                    ),
                    aggregate_record(
                        "error-unresolved",
                        "api-network",
                        "API Key 无效或无权限，尚未替换凭据",
                    ),
                ],
            )

            report = audit_formal_memories(cfg)

            self.assertEqual(report["low_quality_count"], 2)
            self.assertEqual(report["evidence_insufficient_count"], 1)
            self.assertEqual(
                report["evidence_insufficient_breakdown"]["by_type"],
                {"error": 1},
            )
            self.assertIn(
                "missing_verification",
                report["evidence_insufficient_breakdown"]["by_reason"],
            )
            self.assertEqual(report["blocked_lifecycle_action_count"], 0)
            self.assertEqual(report["executable_recommendation_count"], 1)
            self.assertEqual(report["recommended_action_count"], 1)

    def test_audit_reports_duplicate_formal_ids_without_recommending_actions(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            write_pitfalls(
                vault,
                [
                    aggregate_record(
                        "error-shared-id",
                        "shell-cli",
                        "本机缺少 pdftotext，改用 pypdf 完成文本校验",
                        project="demo",
                    ),
                    aggregate_record(
                        "error-near-unique",
                        "shell-cli",
                        "系统没有 pdftotext，改用 PyMuPDF 完成文本核对",
                        project="demo",
                    ),
                ],
                project="demo",
            )
            write_pitfalls(
                vault,
                [
                    aggregate_record(
                        "error-shared-id",
                        "path-filesystem",
                        "目标路径不存在，尚未重新定位真实文件",
                        project="other",
                    )
                ],
                project="other",
            )

            report = audit_formal_memories(cfg)

            self.assertEqual(report["identity_conflict_count"], 1)
            self.assertEqual(
                report["identity_conflicts"][0]["id"],
                "error-shared-id",
            )
            self.assertEqual(
                {item["project"] for item in report["identity_conflicts"][0]["records"]},
                {"demo", "other"},
            )
            self.assertFalse(
                any(
                    "error-shared-id"
                    in {
                        item["memory_id"],
                        item.get("replacement_id", ""),
                    }
                    for item in report["recommended_actions"]
                )
            )
            self.assertEqual(report["duplicate_group_count"], 0)

            report_path = write_quality_report(cfg, report)
            with open(report_path, encoding="utf-8") as handle:
                rendered = handle.read()
            self.assertIn("## 身份冲突", rendered)
            self.assertIn("error-shared-id", rendered)
            conflict_plan = os.path.join(
                vault,
                "04-Feedback/memory-quality-conflicts.md",
            )
            self.assertTrue(os.path.exists(conflict_plan))
            with open(conflict_plan, encoding="utf-8") as handle:
                plan = handle.read()
            self.assertIn("memory-identity-conflict-plan", plan)
            self.assertIn("Source + Revision", plan)
            self.assertIn("01-Projects/demo/Memory/pitfalls", plan)
            self.assertIn("01-Projects/other/Memory/pitfalls", plan)

    def test_conflict_recommendation_keeps_domain_copy_of_exact_fact(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            memory_id = "decision-shared-domain-fact"
            write_decisions(
                vault,
                [
                    decision_record(
                        memory_id,
                        "使用项目自己的验收命令",
                        "避免跨项目套用不相关的测试入口",
                        project="agent-memory-beacon",
                        source_ref="session:wrong-route",
                    )
                ],
                project="agent-memory-beacon",
            )
            write_decisions(
                vault,
                [
                    decision_record(
                        memory_id,
                        "使用项目自己的验收命令",
                        "避免跨项目套用不相关的测试入口",
                        project="demo",
                        source_ref="session:domain-source",
                    )
                ],
                project="demo",
            )

            report = audit_formal_memories(cfg)
            conflict = report["identity_conflicts"][0]

            self.assertEqual(conflict["fact_relation"], "exact_duplicate")
            self.assertEqual(conflict["confidence"], "medium")
            self.assertEqual(conflict["approval_status"], "pending")
            self.assertEqual(
                conflict["recommended_owner"]["source"],
                "01-Projects/demo/Memory/decisions.md",
            )
            self.assertTrue(
                {"session:domain-source", "session:wrong-route"}.issubset(
                    set(conflict["recommended_owner"]["source_refs_after_merge"])
                )
            )
            action = conflict["recommended_actions"][0]
            self.assertEqual(action["action"], "rekey_then_supersede")
            self.assertEqual(action["target_project"], "demo")
            self.assertEqual(action["resulting_status"], "superseded")
            self.assertTrue(action["preserve_source_refs"])
            self.assertTrue(action["merge_source_refs_into_owner"])
            self.assertRegex(action["proposed_id"], r"^decision-[0-9a-f]{16}$")

    def test_conflict_recommendation_rekeys_distinct_fact_from_wrong_path(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            memory_id = "decision-shared-distinct-facts"
            misplaced = decision_record(
                memory_id,
                "保留第一条有效事实",
                "这条事实仍需在后续会话复用",
                project="demo",
                source_ref="session:misplaced",
            )
            canonical = decision_record(
                memory_id,
                "保留第二条有效事实",
                "canonical 项目路径继续持有原 ID",
                project="demo",
                source_ref="session:canonical",
            )
            write_decisions(
                vault,
                [misplaced],
                project="agent-memory-beacon",
            )
            write_decisions(vault, [canonical], project="demo")

            report = audit_formal_memories(cfg)
            first = report["identity_conflicts"][0]
            second = audit_formal_memories(cfg)["identity_conflicts"][0]

            self.assertEqual(first["fact_relation"], "distinct_facts")
            self.assertEqual(first["confidence"], "high")
            self.assertEqual(
                first["recommended_owner"]["source"],
                "01-Projects/demo/Memory/decisions.md",
            )
            action = first["recommended_actions"][0]
            self.assertEqual(action["action"], "rekey_and_keep")
            self.assertEqual(action["source"], "01-Projects/agent-memory-beacon/Memory/decisions.md")
            self.assertEqual(action["target_project"], "demo")
            self.assertEqual(action["target_source"], "01-Projects/demo/Memory/decisions.md")
            self.assertEqual(action["resulting_status"], "active")
            self.assertTrue(action["relocate"])
            self.assertEqual(
                action["proposed_id"],
                second["recommended_actions"][0]["proposed_id"],
            )
            self.assertFalse(
                any(
                    memory_id
                    in {item["memory_id"], item.get("replacement_id", "")}
                    for item in report["recommended_actions"]
                )
            )

            report_path = write_quality_report(cfg, report)
            with open(
                os.path.join(vault, "04-Feedback/memory-quality-conflicts.md"),
                encoding="utf-8",
            ) as handle:
                rendered = handle.read()
            self.assertTrue(os.path.exists(report_path))
            self.assertIn("rekey_and_keep", rendered)
            self.assertIn(action["proposed_id"], rendered)
            self.assertIn(action["revision"], rendered)

    def test_conflict_owner_path_evidence_precedes_quality_score(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            memory_id = "decision-canonical-path-precedence"
            write_decisions(
                vault,
                [
                    decision_record(
                        memory_id,
                        "使用完整测试命令验证所有合同",
                        "确保每项公开行为都有回归测试并通过",
                        project="demo",
                    )
                ],
                project="agent-memory-beacon",
            )
            write_decisions(
                vault,
                [
                    decision_record(
                        memory_id,
                        "C 仍为唯一完整 PASS",
                        "证据范围不应扩大",
                        project="demo",
                    )
                ],
                project="demo",
            )

            conflict = audit_formal_memories(cfg)["identity_conflicts"][0]

            self.assertEqual(
                conflict["recommended_owner"]["source"],
                "01-Projects/demo/Memory/decisions.md",
            )
            self.assertEqual(conflict["confidence"], "high")

    def test_exact_duplicate_never_supersedes_to_inactive_owner(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            memory_id = "decision-active-owner-required"
            write_decisions(
                vault,
                [
                    decision_record(
                        memory_id,
                        "使用稳定索引执行召回",
                        "避免每轮重新扫描完整 Vault",
                        project="demo",
                        status="active",
                    )
                ],
                project="agent-memory-beacon",
            )
            write_decisions(
                vault,
                [
                    decision_record(
                        memory_id,
                        "使用稳定索引执行召回",
                        "避免每轮重新扫描完整 Vault",
                        project="demo",
                        status="retracted",
                    )
                ],
                project="demo",
            )

            conflict = audit_formal_memories(cfg)["identity_conflicts"][0]

            self.assertEqual(conflict["fact_relation"], "exact_duplicate")
            self.assertEqual(conflict["recommended_owner"]["status"], "active")
            self.assertEqual(
                conflict["recommended_owner"]["source"],
                "01-Projects/agent-memory-beacon/Memory/decisions.md",
            )
            self.assertTrue(conflict["recommended_owner"]["relocate"])
            self.assertEqual(
                conflict["recommended_owner"]["target_source"],
                "01-Projects/demo/Memory/decisions.md",
            )
            self.assertEqual(
                conflict["recommended_actions"][0]["action"],
                "rekey_and_retain_inactive",
            )

    def test_mixed_placeholder_is_retracted_not_kept_active(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            memory_id = "decision-mixed-placeholder"
            write_decisions(
                vault,
                [
                    decision_record(
                        memory_id,
                        "summary",
                        "why",
                        project="agent-memory-beacon",
                        status="active",
                    )
                ],
                project="agent-memory-beacon",
            )
            write_decisions(
                vault,
                [
                    decision_record(
                        memory_id,
                        "使用项目索引执行召回",
                        "避免重复读取全部正式记忆",
                        project="demo",
                        status="retracted",
                    )
                ],
                project="demo",
            )

            conflict = audit_formal_memories(cfg)["identity_conflicts"][0]

            self.assertEqual(conflict["fact_relation"], "distinct_facts")
            self.assertEqual(
                conflict["recommended_owner"]["source"],
                "01-Projects/demo/Memory/decisions.md",
            )
            self.assertEqual(
                conflict["recommended_actions"][0]["action"],
                "rekey_then_retract",
            )
            self.assertEqual(
                conflict["recommended_actions"][0]["resulting_status"],
                "retracted",
            )

    def test_conflict_recommendation_distinguishes_same_file_same_revision(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            memory_id = "decision-same-file-duplicate"
            record = decision_record(
                memory_id,
                "使用固定版本运行验收",
                "避免依赖漂移导致相同输入产生不同结果",
                project="demo",
            )
            write_decisions(vault, [record, dict(record)], project="demo")

            conflict = audit_formal_memories(cfg)["identity_conflicts"][0]

            self.assertEqual(conflict["record_count"], 2)
            self.assertEqual(len(conflict["recommended_actions"]), 1)
            self.assertEqual(
                conflict["recommended_owner"]["source_locator"],
                "01-Projects/demo/Memory/decisions.md#decisions[0]",
            )
            self.assertEqual(
                conflict["recommended_actions"][0]["source_locator"],
                "01-Projects/demo/Memory/decisions.md#decisions[1]",
            )
            self.assertNotEqual(
                conflict["recommended_owner"]["source_locator"],
                conflict["recommended_actions"][0]["source_locator"],
            )

    def test_conflict_rekey_id_survives_unrelated_source_insertion(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            memory_id = "decision-stable-rekey-source"
            misplaced = decision_record(
                memory_id,
                "保留被误路由的独立事实",
                "重新编号后仍应继续召回",
                project="demo",
            )
            canonical = decision_record(
                memory_id,
                "由 canonical 路径保留原 ID",
                "物理路径和项目元数据一致",
                project="demo",
            )
            write_decisions(
                vault,
                [misplaced],
                project="agent-memory-beacon",
            )
            write_decisions(vault, [canonical], project="demo")
            first = audit_formal_memories(cfg)["identity_conflicts"][0][
                "recommended_actions"
            ][0]

            unrelated = decision_record(
                "decision-unrelated-prefix",
                "使用另一条独立规则",
                "不应改变冲突记录的稳定修复身份",
                project="agent-memory-beacon",
            )
            write_decisions(
                vault,
                [unrelated, misplaced],
                project="agent-memory-beacon",
            )
            second = audit_formal_memories(cfg)["identity_conflicts"][0][
                "recommended_actions"
            ][0]

            self.assertNotEqual(first["source_locator"], second["source_locator"])
            self.assertNotEqual(first["source_digest"], second["source_digest"])
            self.assertEqual(first["source_identity"], second["source_identity"])
            self.assertEqual(first["proposed_id"], second["proposed_id"])

    def test_conflict_rekey_id_does_not_reuse_existing_alias(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            memory_id = "decision-rekey-alias-collision"
            misplaced = decision_record(
                memory_id,
                "使用单独 ID 保留第一条事实",
                "避免两个可复用事实共享身份",
                project="demo",
            )
            canonical = decision_record(
                memory_id,
                "使用 canonical 路径保留原 ID",
                "项目路径是现有身份所有权证据",
                project="demo",
            )
            write_decisions(
                vault,
                [misplaced],
                project="agent-memory-beacon",
            )
            write_decisions(vault, [canonical], project="demo")
            first_id = audit_formal_memories(cfg)["identity_conflicts"][0][
                "recommended_actions"
            ][0]["proposed_id"]

            alias_holder = decision_record(
                "decision-alias-holder",
                "保留历史别名用于追溯",
                "旧链接仍可解析到正式事实",
                project="demo",
            )
            alias_holder["aliases"] = [first_id]
            write_decisions(vault, [canonical, alias_holder], project="demo")

            second_id = audit_formal_memories(cfg)["identity_conflicts"][0][
                "recommended_actions"
            ][0]["proposed_id"]

            self.assertNotEqual(second_id, first_id)

    def test_conflict_rekey_id_reserves_dependency_and_supersession_targets(self):
        for reservation in ("requires", "superseded_by"):
            with self.subTest(reservation=reservation), tempfile.TemporaryDirectory() as vault:
                cfg = fixture_config(vault)
                memory_id = f"decision-rekey-{reservation}-collision"
                misplaced = decision_record(
                    memory_id,
                    "使用单独 ID 保留第一条事实",
                    "避免两个可复用事实共享身份",
                    project="demo",
                )
                canonical = decision_record(
                    memory_id,
                    "使用 canonical 路径保留原 ID",
                    "项目路径是现有身份所有权证据",
                    project="demo",
                )
                write_decisions(
                    vault,
                    [misplaced],
                    project="agent-memory-beacon",
                )
                write_decisions(vault, [canonical], project="demo")
                first_id = audit_formal_memories(cfg)["identity_conflicts"][0][
                    "recommended_actions"
                ][0]["proposed_id"]

                holder_kwargs = (
                    {"requires": [first_id]}
                    if reservation == "requires"
                    else {
                        "status": "superseded",
                        "superseded_by": first_id,
                    }
                )
                holder = decision_record(
                    f"decision-{reservation}-holder",
                    "保留历史身份引用",
                    "旧依赖和替代链仍需保持无歧义",
                    project="demo",
                    **holder_kwargs,
                )
                write_decisions(vault, [canonical, holder], project="demo")

                second_id = audit_formal_memories(cfg)["identity_conflicts"][0][
                    "recommended_actions"
                ][0]["proposed_id"]

                self.assertNotEqual(second_id, first_id)

    def test_conflict_fact_relation_includes_dependency_semantics(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            memory_id = "decision-shared-text-different-dependency"
            write_decisions(
                vault,
                [
                    decision_record(
                        memory_id,
                        "使用索引结果作为召回输入",
                        "避免每次扫描完整 Vault",
                        project="demo",
                        requires=["decision-index-ready"],
                    )
                ],
                project="agent-memory-beacon",
            )
            write_decisions(
                vault,
                [
                    decision_record(
                        memory_id,
                        "使用索引结果作为召回输入",
                        "避免每次扫描完整 Vault",
                        project="demo",
                    )
                ],
                project="demo",
            )

            conflict = audit_formal_memories(cfg)["identity_conflicts"][0]

            self.assertEqual(conflict["fact_relation"], "distinct_facts")
            self.assertEqual(
                conflict["recommended_actions"][0]["action"],
                "rekey_and_keep",
            )

            write_quality_report(cfg, audit_formal_memories(cfg))
            with open(
                os.path.join(vault, "04-Feedback/memory-quality-conflicts.md"),
                encoding="utf-8",
            ) as handle:
                rendered = handle.read()
            self.assertIn("Requires", rendered)
            self.assertIn("decision-index-ready", rendered)
            self.assertIn("Source Digest", rendered)
            self.assertIn("Scope", rendered)
            self.assertIn("Expires At", rendered)
            self.assertIn("Operational", rendered)
            self.assertIn("Current Status", rendered)

    def test_conflict_fact_relation_includes_expiry_and_operational_semantics(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            expiry_id = "decision-shared-text-different-expiry"
            operation_id = "decision-shared-text-different-operation"
            write_decisions(
                vault,
                [
                    decision_record(
                        expiry_id,
                        "使用索引结果作为召回输入",
                        "避免每次扫描完整 Vault",
                        project="demo",
                        expires_at="2026-08-01T00:00:00+08:00",
                    ),
                    decision_record(
                        operation_id,
                        "按场景调用审查 skill",
                        "减少与任务无关的工具调用",
                        project="demo",
                        operational={"when": "代码审查", "avoid": "普通问答"},
                    ),
                ],
                project="agent-memory-beacon",
            )
            write_decisions(
                vault,
                [
                    decision_record(
                        expiry_id,
                        "使用索引结果作为召回输入",
                        "避免每次扫描完整 Vault",
                        project="demo",
                    ),
                    decision_record(
                        operation_id,
                        "按场景调用审查 skill",
                        "减少与任务无关的工具调用",
                        project="demo",
                        operational={"when": "安全审查", "avoid": "普通问答"},
                    ),
                ],
                project="demo",
            )

            conflicts = {
                item["id"]: item for item in audit_formal_memories(cfg)["identity_conflicts"]
            }

            self.assertEqual(conflicts[expiry_id]["fact_relation"], "distinct_facts")
            self.assertEqual(
                conflicts[operation_id]["fact_relation"],
                "distinct_facts",
            )

    def test_same_text_in_two_business_projects_remains_distinct(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            memory_id = "decision-shared-cross-project-text"
            for project in ("alpha", "beta"):
                write_decisions(
                    vault,
                    [
                        decision_record(
                            memory_id,
                            "使用项目自己的完整测试入口",
                            "避免遗漏该项目的合同测试",
                            project=project,
                            source_ref=f"session:{project}",
                        )
                    ],
                    project=project,
                )

            conflict = audit_formal_memories(cfg)["identity_conflicts"][0]

            self.assertEqual(conflict["fact_relation"], "distinct_facts")
            self.assertEqual(conflict["confidence"], "low")
            self.assertFalse(
                conflict["recommended_actions"][0]["merge_source_refs_into_owner"]
            )
            self.assertEqual(
                conflict["recommended_actions"][0]["action"],
                "rekey_and_keep",
            )
            self.assertEqual(
                conflict["recommended_actions"][0]["target_project"],
                "beta",
            )

    def test_conflict_recommendation_retracts_active_placeholder(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            memory_id = "decision-placeholder-conflict"
            write_decisions(
                vault,
                [
                    decision_record(
                        memory_id,
                        "summary",
                        "why",
                        project="agent-memory-beacon",
                        status="active",
                        source_ref="session:placeholder-active",
                    )
                ],
                project="agent-memory-beacon",
            )
            write_decisions(
                vault,
                [
                    decision_record(
                        memory_id,
                        "summary",
                        "why",
                        project="slug",
                        status="retracted",
                        source_ref="migration:legacy-placeholder",
                    )
                ],
                project="slug",
            )

            conflict = audit_formal_memories(cfg)["identity_conflicts"][0]

            self.assertEqual(conflict["fact_relation"], "low_quality_placeholder")
            self.assertEqual(conflict["confidence"], "low")
            self.assertEqual(
                conflict["recommended_owner"]["source"],
                "01-Projects/slug/Memory/decisions.md",
            )
            self.assertEqual(conflict["recommended_owner"]["status"], "retracted")
            action = conflict["recommended_actions"][0]
            self.assertEqual(action["action"], "rekey_then_retract")
            self.assertEqual(action["resulting_status"], "retracted")
            self.assertFalse(action["merge_source_refs_into_owner"])

    def test_placeholder_conflict_prefers_inactive_owner_regardless_of_project(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            memory_id = "decision-placeholder-inactive-owner"
            write_decisions(
                vault,
                [
                    decision_record(
                        memory_id,
                        "summary",
                        "why",
                        project="demo",
                        status="active",
                    )
                ],
                project="demo",
            )
            write_decisions(
                vault,
                [
                    decision_record(
                        memory_id,
                        "summary",
                        "why",
                        project="agent-memory-beacon",
                        status="retracted",
                    )
                ],
                project="agent-memory-beacon",
            )

            conflict = audit_formal_memories(cfg)["identity_conflicts"][0]

            self.assertEqual(conflict["fact_relation"], "low_quality_placeholder")
            self.assertEqual(conflict["recommended_owner"]["status"], "retracted")
            self.assertEqual(
                conflict["recommended_owner"]["resulting_status"],
                "retracted",
            )
            self.assertEqual(
                conflict["recommended_actions"][0]["action"],
                "rekey_then_retract",
            )

    def test_all_active_placeholders_retract_owner_as_well(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            memory_id = "decision-all-active-placeholders"
            for project in ("alpha", "beta"):
                write_decisions(
                    vault,
                    [
                        decision_record(
                            memory_id,
                            "summary",
                            "why",
                            project=project,
                            status="active",
                        )
                    ],
                    project=project,
                )

            conflict = audit_formal_memories(cfg)["identity_conflicts"][0]

            self.assertEqual(conflict["fact_relation"], "low_quality_placeholder")
            self.assertEqual(
                conflict["recommended_owner"]["action"],
                "retain_id_then_retract",
            )
            self.assertEqual(
                conflict["recommended_owner"]["resulting_status"],
                "retracted",
            )
            self.assertTrue(
                all(
                    action["resulting_status"] == "retracted"
                    for action in conflict["recommended_actions"]
                )
            )

    def test_quality_proposals_preflight_all_revisions_before_any_write(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            records = [
                aggregate_record(
                    "error-a-valid",
                    "api-network",
                    "API Key 无效或无权限，尚未替换凭据",
                ),
                aggregate_record(
                    "error-z-stale",
                    "path-filesystem",
                    "目标路径不存在，尚未重新定位真实文件",
                ),
            ]
            write_pitfalls(vault, records)
            report = audit_formal_memories(cfg)
            self.assertEqual(report["recommended_action_count"], 2)

            updated = [
                (
                    aggregate_record(
                        record["id"],
                        record["type"],
                        "目标路径不存在，重新定位真实文件并完成读取验证",
                    )
                    if record["id"] == "error-z-stale"
                    else record
                )
                for record in records
            ]
            write_pitfalls(vault, updated)

            with self.assertRaises(LifecycleConflict):
                create_quality_proposals(cfg, report)

            proposal_dir = os.path.join(vault, "04-Feedback/_lifecycle-proposals")
            self.assertFalse(os.path.exists(proposal_dir))

    def test_quality_proposals_preflight_duplicate_ids_before_any_write(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            unique = aggregate_record(
                "error-a-valid",
                "api-network",
                "API Key 无效或无权限，尚未替换凭据",
            )
            shared_demo = aggregate_record(
                "error-z-shared",
                "path-filesystem",
                "目标路径不存在，尚未重新定位真实文件",
                project="demo",
            )
            shared_other = aggregate_record(
                "error-z-shared",
                "shell-cli",
                "命令不可用，尚未找到替代工具",
                project="other",
            )
            write_pitfalls(vault, [unique, shared_demo], project="demo")
            write_pitfalls(vault, [shared_other], project="other")
            report = {
                "generated_at": "2026-07-18T00:00:00+00:00",
                "recommended_actions": [
                    {
                        "action": "retract",
                        "memory_id": unique["id"],
                        "expected_revision": unique["revision"],
                        "replacement_id": "",
                        "replacement_revision": "",
                        "reason": "测试全量预检",
                        "reason_codes": ["unresolved_failure"],
                    },
                    {
                        "action": "retract",
                        "memory_id": shared_demo["id"],
                        "expected_revision": shared_demo["revision"],
                        "replacement_id": "",
                        "replacement_revision": "",
                        "reason": "测试重复身份预检",
                        "reason_codes": ["unresolved_failure"],
                    },
                ],
            }

            with self.assertRaises(LifecycleConflict):
                create_quality_proposals(cfg, report)

            proposal_dir = os.path.join(vault, "04-Feedback/_lifecycle-proposals")
            self.assertFalse(os.path.exists(proposal_dir))

    def test_quality_proposal_batch_preflight_does_not_rescan_vault_per_action(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            write_pitfalls(
                vault,
                [
                    aggregate_record(
                        "error-a",
                        "api-network",
                        "API Key 无效或无权限，尚未替换凭据",
                    ),
                    aggregate_record(
                        "error-b",
                        "path-filesystem",
                        "目标路径不存在，尚未重新定位真实文件",
                    ),
                ],
            )
            report = audit_formal_memories(cfg)
            self.assertEqual(report["recommended_action_count"], 2)

            with mock.patch.object(
                memory_lifecycle,
                "find_records",
                wraps=memory_lifecycle.find_records,
            ) as lifecycle_scan, mock.patch(
                "memory_quality_audit.find_records",
                wraps=memory_quality_audit.find_records,
            ) as audit_scan:
                create_quality_proposals(cfg, report)

            self.assertEqual(lifecycle_scan.call_count, 0)
            self.assertEqual(audit_scan.call_count, 2)

    def test_quality_proposal_snapshot_covers_supersede_validation(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            write_pitfalls(
                vault,
                [
                    aggregate_record(
                        "error-a",
                        "shell-cli",
                        "本机缺少 pdftotext，改用 pypdf 完成文本校验",
                    ),
                    aggregate_record(
                        "error-b",
                        "shell-cli",
                        "系统没有 pdftotext，改用 PyMuPDF 完成文本核对",
                    ),
                ],
            )
            report = audit_formal_memories(cfg)
            self.assertEqual(report["recommended_action_count"], 1)
            self.assertEqual(report["recommended_actions"][0]["action"], "supersede")

            with mock.patch.object(
                memory_lifecycle,
                "find_records",
                wraps=memory_lifecycle.find_records,
            ) as lifecycle_scan, mock.patch(
                "memory_quality_audit.find_records",
                wraps=memory_quality_audit.find_records,
            ) as audit_scan:
                paths = create_quality_proposals(cfg, report)

            self.assertEqual(len(paths), 1)
            self.assertEqual(lifecycle_scan.call_count, 0)
            self.assertEqual(audit_scan.call_count, 2)


def fixture_config(vault):
    return {
        "vault_path": vault,
        "annotation_quality": {
            "enabled": True,
            "candidate_dir": "04-Feedback/_annotation-candidates",
            "report_path": "04-Feedback/memory-quality-report.md",
        },
        "memory_lifecycle": {
            "proposal_dir": "04-Feedback/_lifecycle-proposals",
            "audit_path": "05-Agent-Memory/lifecycle-audit.md",
            "rollback_dir": "04-Feedback/_rollback/lifecycle",
        },
    }


def aggregate_record(
    memory_id,
    error_type,
    resolution,
    project="demo",
    date="2026-07-18",
):
    normalized = normalize_formal_record(
        {
            "id": memory_id,
            "project": project,
            "scope": "project",
            "status": "active",
            "title": error_type,
            "summary": resolution,
            "date": date,
        },
        memory_type="error",
        default_project=project,
        source_ref=f"session:{memory_id}",
    )
    return {
        "id": normalized["id"],
        "revision": normalized["revision"],
        "type": normalized["title"],
        "resolution": normalized["summary"],
        "status": normalized["status"],
        "project": normalized["project"],
        "scope": normalized["scope"],
        "date": normalized["date"],
        "source_refs": normalized["source_refs"],
        "aliases": [],
    }


def decision_record(
    memory_id,
    title,
    context,
    project="demo",
    status="active",
    source_ref="",
    requires=None,
    expires_at="",
    operational=None,
    superseded_by="",
):
    raw = {
        "id": memory_id,
        "project": project,
        "scope": "project",
        "status": status,
        "title": title,
        "summary": context,
        "date": "2026-07-18",
        "requires": list(requires or []),
    }
    if expires_at:
        raw["expires_at"] = expires_at
    if superseded_by:
        raw["superseded_by"] = superseded_by
    raw.update(dict(operational or {}))
    normalized = normalize_formal_record(
        raw,
        memory_type="decision",
        default_project=project,
        source_ref=source_ref or f"session:{memory_id}:{project}",
    )
    record = {
        "id": normalized["id"],
        "revision": normalized["revision"],
        "title": normalized["title"],
        "summary": normalized["summary"],
        "status": normalized["status"],
        "project": normalized["project"],
        "scope": normalized["scope"],
        "date": normalized["date"],
        "source_refs": normalized["source_refs"],
        "aliases": [],
    }
    if normalized.get("requires"):
        record["requires"] = normalized["requires"]
    if normalized.get("expires_at"):
        record["expires_at"] = normalized["expires_at"]
    if normalized.get("superseded_by"):
        record["superseded_by"] = normalized["superseded_by"]
    for key in ("name", "when", "avoid", "trigger", "behavior"):
        if normalized.get(key):
            record[key] = normalized[key]
    return record


def write_decisions(vault, records, project="demo"):
    path = os.path.join(vault, f"01-Projects/{project}/Memory/decisions.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = (
        "---\n"
        + yaml.safe_dump(
            {
                "project": project,
                "schema_version": "2.0",
                "decisions": records,
            },
            allow_unicode=True,
            sort_keys=False,
        )
        + "---\n\n# Decisions\n"
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def write_pitfalls(vault, records, project="demo"):
    path = os.path.join(vault, f"01-Projects/{project}/Memory/pitfalls.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = (
        "---\n"
        + yaml.safe_dump(
            {
                "project": project,
                "schema_version": "2.0",
                "pitfalls": records,
            },
            allow_unicode=True,
            sort_keys=False,
        )
        + "---\n\n# Pitfalls\n"
    )
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    return path


def read_frontmatter(path):
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    _opening, raw, _body = text.split("---", 2)
    return yaml.safe_load(raw) or {}


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


if __name__ == "__main__":
    unittest.main()
