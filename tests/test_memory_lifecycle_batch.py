import hashlib
import json
import os
import sys
import tempfile
import unittest
from collections import defaultdict
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
TESTS_DIR = os.path.join(REPO_ROOT, "tests")
sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, TESTS_DIR)

from knowledge_index import rebuild_vault_knowledge_indexes
from memory_lifecycle import create_proposal, find_records
from memory_lifecycle_batch import (
    BatchLifecycleError,
    BatchLifecyclePreconditionError,
    apply_lifecycle_batch,
    main,
    preview_lifecycle_batch,
)
from memory_quality_audit import (
    _freeze_old_memory_action,
    _render_old_memory_lifecycle_plan,
)
from test_memory_lifecycle import (
    aggregate_record,
    fixture_config,
    read_bytes,
    read_frontmatter,
    read_project_records,
    read_text,
    write_personal_memory,
    write_project_records,
    write_text,
)


class MemoryLifecycleBatchTests(unittest.TestCase):
    def test_preview_binds_canonical_plan_without_writing(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            first = aggregate_record(
                "decision-batch-first",
                "decision",
                "第一条旧决定",
                "应从正式召回撤回",
            )
            second = aggregate_record(
                "decision-batch-second",
                "decision",
                "第二条旧决定",
                "也应从正式召回撤回",
            )
            source = write_project_records(vault, decisions=[first, second])
            plan_path, plan_sha, _actions = write_lifecycle_plan(
                cfg,
                [
                    retract_action(first),
                    retract_action(second),
                ],
            )
            before = {source: read_bytes(source), plan_path: read_bytes(plan_path)}

            result = preview_lifecycle_batch(cfg, plan_path, plan_sha)

            self.assertFalse(result["applied"])
            self.assertEqual(result["canonical_sha256"], plan_sha)
            self.assertEqual(result["action_count"], 2)
            self.assertEqual(result["affected_source_count"], 1)
            self.assertEqual(
                {source: read_bytes(source), plan_path: read_bytes(plan_path)},
                before,
            )
            self.assertFalse(os.path.exists(rollback_root(vault)))

    def test_apply_entry_point_remains_read_only_without_apply_flag(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            record = aggregate_record(
                "decision-batch-preview-only",
                "decision",
                "只预览",
                "缺少 apply 不能写入",
            )
            source = write_project_records(vault, decisions=[record])
            plan_path, plan_sha, _actions = write_lifecycle_plan(
                cfg,
                [retract_action(record)],
            )
            before = {source: read_bytes(source), plan_path: read_bytes(plan_path)}

            result = apply_lifecycle_batch(cfg, plan_path, plan_sha)

            self.assertFalse(result["applied"])
            self.assertEqual(
                {source: read_bytes(source), plan_path: read_bytes(plan_path)},
                before,
            )
            self.assertFalse(os.path.exists(rollback_root(vault)))

    def test_preview_rejects_wrong_sha_and_canonical_content_drift(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            record = aggregate_record(
                "decision-batch-hash",
                "decision",
                "冻结审批载荷",
                "内容漂移必须重新批准",
            )
            source = write_project_records(vault, decisions=[record])
            plan_path, plan_sha, actions = write_lifecycle_plan(
                cfg,
                [retract_action(record)],
            )
            before = read_bytes(source)

            with self.assertRaisesRegex(
                BatchLifecyclePreconditionError,
                "SHA256",
            ):
                preview_lifecycle_batch(cfg, plan_path, "0" * 64)

            drifted = dict(actions[0])
            drifted["reason"] = "审批后被改写的理由"
            write_lifecycle_plan(
                cfg,
                [drifted],
                path=plan_path,
                canonical_sha256=plan_sha,
            )
            with self.assertRaisesRegex(
                BatchLifecyclePreconditionError,
                "canonical",
            ):
                preview_lifecycle_batch(cfg, plan_path, plan_sha)

            self.assertEqual(read_bytes(source), before)
            self.assertFalse(os.path.exists(rollback_root(vault)))

    def test_preview_rejects_record_revision_digest_and_locator_drift(self):
        for drift in ("record", "digest", "locator"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as vault:
                cfg = fixture_config(vault)
                record = aggregate_record(
                    "decision-batch-drift",
                    "decision",
                    "绑定稳定记录",
                    "revision digest locator 都必须一致",
                )
                source = write_project_records(vault, decisions=[record])
                plan_path, plan_sha, actions = write_lifecycle_plan(
                    cfg,
                    [retract_action(record)],
                )
                if drift == "record":
                    changed = aggregate_record(
                        record["id"],
                        "decision",
                        "绑定稳定记录",
                        "审批后正式记录发生变化",
                    )
                    write_project_records(vault, decisions=[changed])
                else:
                    changed_action = dict(actions[0])
                    key = (
                        "source_digest"
                        if drift == "digest"
                        else "source_locator"
                    )
                    changed_action[key] = (
                        "0" * 64
                        if drift == "digest"
                        else changed_action[key] + "-drift"
                    )
                    plan_path, plan_sha, _actions = write_lifecycle_plan(
                        cfg,
                        [changed_action],
                        path=plan_path,
                    )
                changed_bytes = read_bytes(source)

                with self.assertRaises(BatchLifecyclePreconditionError):
                    preview_lifecycle_batch(cfg, plan_path, plan_sha)

                self.assertEqual(read_bytes(source), changed_bytes)
                self.assertFalse(os.path.exists(rollback_root(vault)))

    def test_apply_combines_shared_source_and_rebuilds_once(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            first = aggregate_record(
                "decision-batch-shared-first",
                "decision",
                "共享文件第一项",
                "批量更新不能覆盖另一项",
            )
            second = aggregate_record(
                "decision-batch-shared-second",
                "decision",
                "共享文件第二项",
                "同一聚合文件只发布一次",
            )
            source = write_project_records(vault, decisions=[first, second])
            plan_path, plan_sha, _actions = write_lifecycle_plan(
                cfg,
                [retract_action(first), retract_action(second)],
            )
            calls = []

            def rebuild_once(current_cfg):
                calls.append("rebuild")
                rebuild_vault_knowledge_indexes(current_cfg)

            result = apply_lifecycle_batch(
                cfg,
                plan_path,
                plan_sha,
                apply=True,
                rebuilders=[rebuild_once],
            )

            records = read_project_records(source, "decisions")
            self.assertEqual(
                {item["id"]: item["status"] for item in records},
                {
                    first["id"]: "retracted",
                    second["id"]: "retracted",
                },
            )
            self.assertEqual(calls, ["rebuild"])
            self.assertTrue(result["applied"])
            self.assertEqual(result["action_count"], 2)
            self.assertEqual(read_manifest(result)["status"], "applied")
            self.assertEqual(
                read_manifest(result)["approved_plan_sha256"],
                plan_sha,
            )
            self.assertEqual(
                read_frontmatter(plan_path)["approval_status"],
                "applied",
            )
            audit = read_text(audit_path(vault))
            self.assertIn(plan_sha, audit)
            self.assertEqual(audit.count("action: `lifecycle-batch`"), 1)

    def test_preview_allows_unrelated_append_to_the_same_aggregate(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            target = aggregate_record(
                "decision-batch-stable-locator",
                "decision",
                "稳定单记录定位",
                "无关追加不应废止审批",
            )
            source = write_project_records(vault, decisions=[target])
            plan_path, plan_sha, _actions = write_lifecycle_plan(
                cfg,
                [retract_action(target)],
            )
            unrelated = aggregate_record(
                "decision-batch-unrelated-append",
                "decision",
                "审批后新增的无关记录",
                "不属于已批准动作",
            )
            write_project_records(vault, decisions=[target, unrelated])
            changed = read_bytes(source)

            result = preview_lifecycle_batch(cfg, plan_path, plan_sha)

            self.assertEqual(result["action_count"], 1)
            self.assertEqual(read_bytes(source), changed)
            self.assertFalse(os.path.exists(rollback_root(vault)))

    def test_apply_combines_multiple_adaptive_sections_in_one_file(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            source = write_personal_memory(
                vault,
                "preference-batch-adaptive-first",
                status="active",
            )
            locations = {
                item.memory_id: item
                for item in find_records(cfg)
                if item.path == source
            }
            first = locations["preference-batch-adaptive-first"]
            second = locations["preference-unrelated"]
            plan_path, plan_sha, _actions = write_lifecycle_plan(
                cfg,
                [
                    retract_location_action(first),
                    retract_location_action(second),
                ],
            )

            apply_lifecycle_batch(
                cfg,
                plan_path,
                plan_sha,
                apply=True,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            statuses = {
                item.memory_id: item.status
                for item in find_records(cfg)
                if item.path == source
            }
            self.assertEqual(
                statuses,
                {
                    "preference-batch-adaptive-first": "retracted",
                    "preference-unrelated": "retracted",
                },
            )

    def test_preflight_rejects_target_replacement_overlap(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            old = aggregate_record(
                "decision-batch-overlap-old",
                "decision",
                "旧决定",
                "由第二条替代",
            )
            replacement = aggregate_record(
                "decision-batch-overlap-replacement",
                "decision",
                "替代决定",
                "不能在同批又被撤回",
            )
            source = write_project_records(vault, decisions=[old, replacement])
            plan_path, plan_sha, _actions = write_lifecycle_plan(
                cfg,
                [
                    supersede_action(old, replacement),
                    retract_action(replacement),
                ],
            )
            before = read_bytes(source)

            with self.assertRaisesRegex(
                BatchLifecyclePreconditionError,
                "overlap",
            ):
                preview_lifecycle_batch(cfg, plan_path, plan_sha)

            self.assertEqual(read_bytes(source), before)

    def test_preflight_checks_replacement_eligibility_after_entire_batch(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            old = aggregate_record(
                "decision-batch-dependency-old",
                "decision",
                "旧决定",
                "由依赖型决定替代",
            )
            dependency = aggregate_record(
                "decision-batch-dependency",
                "decision",
                "替代项依赖",
                "同批将被撤回",
            )
            replacement = aggregate_record(
                "decision-batch-dependent-replacement",
                "decision",
                "依赖型替代项",
                "整批结束后必须仍可召回",
                requires=[dependency["id"]],
            )
            source = write_project_records(
                vault,
                decisions=[old, dependency, replacement],
            )
            plan_path, plan_sha, _actions = write_lifecycle_plan(
                cfg,
                [
                    supersede_action(old, replacement),
                    retract_action(dependency),
                ],
            )
            before = read_bytes(source)

            with self.assertRaisesRegex(
                BatchLifecyclePreconditionError,
                "dependency-suppressed",
            ):
                preview_lifecycle_batch(cfg, plan_path, plan_sha)

            self.assertEqual(read_bytes(source), before)

    def test_apply_allows_target_alias_owned_only_by_approved_replacement(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            old = aggregate_record(
                "decision-batch-approved-alias-old",
                "decision",
                "旧的运行时选择",
                "由明确批准的新选择替代",
            )
            replacement = aggregate_record(
                "decision-batch-approved-alias-replacement",
                "decision",
                "新的运行时选择",
                "保留旧 ID 作为兼容 alias",
            )
            replacement["aliases"] = [old["id"]]
            source = write_project_records(vault, decisions=[old, replacement])
            plan_path, plan_sha, _actions = write_lifecycle_plan(
                cfg,
                [supersede_action(old, replacement)],
            )

            result = apply_lifecycle_batch(
                cfg,
                plan_path,
                plan_sha,
                apply=True,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            records = {
                item["id"]: item
                for item in read_project_records(source, "decisions")
            }
            self.assertEqual(records[old["id"]]["status"], "superseded")
            self.assertEqual(
                records[old["id"]]["superseded_by"],
                replacement["id"],
            )
            recall_path = os.path.join(
                vault,
                "05-Agent-Memory",
                "recall-index.json",
            )
            units = json.loads(read_text(recall_path))["units"]
            self.assertNotIn(old["id"], {item["id"] for item in units})
            self.assertEqual(
                [
                    item["id"]
                    for item in units
                    if old["id"] in (item.get("aliases") or [])
                ],
                [replacement["id"]],
            )
            self.assertTrue(result["applied"])

    def test_preflight_rejects_unapproved_active_alias_owner_for_supersede(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            old = aggregate_record(
                "decision-batch-unapproved-alias-old",
                "decision",
                "旧的工具选择",
                "由批准的替代项取代",
            )
            replacement = aggregate_record(
                "decision-batch-unapproved-alias-replacement",
                "decision",
                "批准的工具选择",
                "这是计划绑定的唯一替代项",
            )
            unrelated = aggregate_record(
                "decision-batch-unapproved-alias-owner",
                "decision",
                "无关的活跃记录",
                "不能通过 alias 冒充批准的替代项",
            )
            unrelated["aliases"] = [old["id"]]
            source = write_project_records(
                vault,
                decisions=[old, replacement, unrelated],
            )
            plan_path, plan_sha, _actions = write_lifecycle_plan(
                cfg,
                [supersede_action(old, replacement)],
            )
            before = read_bytes(source)

            with self.assertRaisesRegex(
                BatchLifecyclePreconditionError,
                "unapproved active alias owner",
            ):
                preview_lifecycle_batch(cfg, plan_path, plan_sha)

            self.assertEqual(read_bytes(source), before)
            self.assertFalse(os.path.exists(rollback_root(vault)))

    def test_preflight_rejects_active_alias_owner_for_retract(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            target = aggregate_record(
                "decision-batch-retract-alias-target",
                "decision",
                "待撤回的旧决定",
                "撤回后旧 ID 必须完全退出召回",
            )
            alias_owner = aggregate_record(
                "decision-batch-retract-alias-owner",
                "decision",
                "仍然活跃的其他决定",
                "不能继续持有已撤回 ID",
            )
            alias_owner["aliases"] = [target["id"]]
            source = write_project_records(
                vault,
                decisions=[target, alias_owner],
            )
            plan_path, plan_sha, _actions = write_lifecycle_plan(
                cfg,
                [retract_action(target)],
            )
            before = read_bytes(source)

            with self.assertRaisesRegex(
                BatchLifecyclePreconditionError,
                "active alias owner",
            ):
                preview_lifecycle_batch(cfg, plan_path, plan_sha)

            self.assertEqual(read_bytes(source), before)
            self.assertFalse(os.path.exists(rollback_root(vault)))

    def test_apply_marks_exact_proposal_applied_and_other_pending_stale(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            record = aggregate_record(
                "decision-batch-proposal",
                "decision",
                "提案收敛",
                "执行后不能继续显示待确认",
            )
            write_project_records(vault, decisions=[record])
            plan_path, plan_sha, actions = write_lifecycle_plan(
                cfg,
                [retract_action(record)],
            )
            exact = create_matching_proposal(cfg, actions[0])
            other = create_proposal(
                cfg,
                action="retract",
                memory_id=record["id"],
                reason="另一个没有被批准的理由",
                evidence_refs=["memory-quality-audit:other"],
                expected_revision=record["revision"],
            )

            result = apply_lifecycle_batch(
                cfg,
                plan_path,
                plan_sha,
                apply=True,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            exact_fm = read_frontmatter(exact)
            other_fm = read_frontmatter(other)
            self.assertEqual(exact_fm["status"], "applied")
            self.assertEqual(exact_fm["approved_plan_sha256"], plan_sha)
            self.assertEqual(other_fm["status"], "stale")
            self.assertEqual(
                other_fm["stale_reason"],
                "superseded_by_applied_lifecycle_plan",
            )
            self.assertEqual(result["proposal_applied_count"], 1)
            self.assertEqual(result["proposal_stale_count"], 1)

    def test_proposal_reconciliation_preserves_literal_internal_marker_text(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            record = aggregate_record(
                "decision-batch-proposal-literal",
                "decision",
                "保留提案证据原文",
                "内部字段绑定不能改写用户内容",
            )
            write_project_records(vault, decisions=[record])
            plan_path, plan_sha, _actions = write_lifecycle_plan(
                cfg,
                [retract_action(record)],
            )
            literal_reason = (
                "保留 __BATCH_TIMESTAMP__ 和 __BATCH_OPERATION_ID__ 原文字面量"
            )
            proposal = create_proposal(
                cfg,
                action="retract",
                memory_id=record["id"],
                reason=literal_reason,
                evidence_refs=["memory-quality-audit:literal-marker"],
                expected_revision=record["revision"],
            )

            apply_lifecycle_batch(
                cfg,
                plan_path,
                plan_sha,
                apply=True,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            frontmatter = read_frontmatter(proposal)
            self.assertEqual(frontmatter["status"], "stale")
            self.assertEqual(frontmatter["reason"], literal_reason)

    def test_partial_source_write_failure_rolls_back_all_sources(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            decision = aggregate_record(
                "decision-batch-write-failure",
                "decision",
                "先写入的决定",
                "后续失败时必须恢复",
            )
            pitfall = aggregate_record(
                "error-batch-write-failure",
                "error",
                "shell-cli",
                "第二个源文件写入失败",
            )
            decisions_path = write_project_records(
                vault,
                decisions=[decision],
                pitfalls=[pitfall],
            )
            pitfalls_path = os.path.join(
                os.path.dirname(decisions_path),
                "pitfalls.md",
            )
            plan_path, plan_sha, _actions = write_lifecycle_plan(
                cfg,
                [retract_action(decision), retract_action(pitfall)],
            )
            before = {
                path: read_bytes(path)
                for path in (decisions_path, pitfalls_path, plan_path)
            }

            from memory_lifecycle_batch import durable_atomic_write as real_write

            def fail_second_source(path, content, **kwargs):
                if os.path.abspath(path) == os.path.abspath(pitfalls_path):
                    raise OSError("injected second source write failure")
                return real_write(path, content, **kwargs)

            with patch(
                "memory_lifecycle_batch.durable_atomic_write",
                side_effect=fail_second_source,
            ), self.assertRaisesRegex(BatchLifecycleError, "second source"):
                apply_lifecycle_batch(
                    cfg,
                    plan_path,
                    plan_sha,
                    apply=True,
                    rebuilders=[rebuild_vault_knowledge_indexes],
                )

            self.assertEqual(
                {path: read_bytes(path) for path in before},
                before,
            )
            manifest = latest_manifest(vault)
            self.assertEqual(json.loads(read_text(manifest))["status"], "rolled_back")
            self.assertFalse(os.path.exists(audit_path(vault)))

    def test_rebuild_failure_restores_source_derived_context_plan_and_proposal(self):
        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            os.makedirs(vault)
            cfg = fixture_config(vault)
            context_path = os.path.join(root, "AGENTS.md")
            cfg["context_targets"] = [context_path]
            record = aggregate_record(
                "decision-batch-rollback",
                "decision",
                "整批回滚",
                "所有治理与派生文件都恢复",
            )
            source = write_project_records(vault, decisions=[record])
            rebuild_vault_knowledge_indexes(cfg)
            write_text(context_path, "context-before\n")
            plan_path, plan_sha, actions = write_lifecycle_plan(
                cfg,
                [retract_action(record)],
            )
            proposal_path = create_matching_proposal(cfg, actions[0])
            recall_path = os.path.join(
                vault,
                "05-Agent-Memory",
                "recall-index.json",
            )
            before = {
                path: read_bytes(path)
                for path in (
                    source,
                    recall_path,
                    context_path,
                    plan_path,
                    proposal_path,
                )
            }

            def failing_rebuilder(current_cfg):
                rebuild_vault_knowledge_indexes(current_cfg)
                write_text(context_path, "context-during\n")
                write_text(plan_path, "plan-during\n")
                write_text(proposal_path, "proposal-during\n")
                raise RuntimeError("injected batch rebuild failure")

            with self.assertRaisesRegex(BatchLifecycleError, "rebuild failure"):
                apply_lifecycle_batch(
                    cfg,
                    plan_path,
                    plan_sha,
                    apply=True,
                    rebuilders=[failing_rebuilder],
                )

            self.assertEqual(
                {path: read_bytes(path) for path in before},
                before,
            )
            manifest = latest_manifest(vault)
            payload = json.loads(read_text(manifest))
            self.assertEqual(payload["status"], "rolled_back")
            self.assertEqual(payload["approved_plan_sha256"], plan_sha)
            self.assertTrue(
                os.path.isfile(
                    os.path.join(os.path.dirname(manifest), "approved-plan.md")
                )
            )

    def test_cli_without_apply_only_previews(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            record = aggregate_record(
                "decision-batch-cli-preview",
                "decision",
                "CLI 默认预览",
                "只有显式 apply 才能修改",
            )
            source = write_project_records(vault, decisions=[record])
            plan_path, plan_sha, _actions = write_lifecycle_plan(
                cfg,
                [retract_action(record)],
            )
            before = {source: read_bytes(source), plan_path: read_bytes(plan_path)}

            with patch("memory_lifecycle_batch.load_config", return_value=cfg), \
                    redirect_stdout(StringIO()) as output:
                result = main(
                    [
                        "--plan",
                        plan_path,
                        "--expected-sha256",
                        plan_sha,
                        "--json",
                    ]
                )

            self.assertEqual(result, 0)
            self.assertFalse(json.loads(output.getvalue())["applied"])
            self.assertEqual(
                {source: read_bytes(source), plan_path: read_bytes(plan_path)},
                before,
            )
            self.assertFalse(os.path.exists(rollback_root(vault)))


def retract_action(record):
    return {
        "action": "retract",
        "memory_id": record["id"],
        "expected_revision": record["revision"],
        "replacement_id": "",
        "replacement_revision": "",
        "reason": "历史质量审计判定该记录不应作为长期正式记忆：temporary_or_one_off",
        "reason_codes": ["temporary_or_one_off"],
    }


def supersede_action(record, replacement):
    return {
        "action": "supersede",
        "memory_id": record["id"],
        "expected_revision": record["revision"],
        "replacement_id": replacement["id"],
        "replacement_revision": replacement["revision"],
        "reason": (
            "历史质量审计识别为同一事实的近重复记录；"
            f"保留 {replacement['id']} 作为代表"
        ),
        "reason_codes": ["duplicate"],
    }


def retract_location_action(location):
    return {
        "action": "retract",
        "memory_id": location.memory_id,
        "expected_revision": location.revision,
        "replacement_id": "",
        "replacement_revision": "",
        "reason": "历史质量审计判定该记录不应作为长期正式记忆：temporary_or_one_off",
        "reason_codes": ["temporary_or_one_off"],
    }


def write_lifecycle_plan(
    cfg,
    raw_actions,
    *,
    path="",
    canonical_sha256="",
):
    records_by_id = defaultdict(list)
    for location in find_records(cfg):
        records_by_id[location.memory_id].append(location)
    actions = []
    for raw in raw_actions:
        if "source_path" in raw:
            actions.append(dict(raw))
        else:
            actions.append(_freeze_old_memory_action(cfg, records_by_id, raw))
    actions.sort(
        key=lambda item: (
            item["action"],
            item["memory_id"],
            item["replacement_id"],
        )
    )
    payload = {
        "schema_version": "1.0",
        "cutoff_exclusive": "2026-07-14",
        "actions": actions,
    }
    computed_sha = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    snapshot = {
        "schema_version": "1.0",
        "generated_at": "2026-07-19T00:00:00+00:00",
        "cutoff_exclusive": "2026-07-14",
        "read_only": True,
        "approval_status": "pending",
        "canonical_sha256": canonical_sha256 or computed_sha,
        "active_record_count": len(find_records(cfg)),
        "undated_active_record_count": 0,
        "invalid_date_active_record_count": 0,
        "old_active_record_count": len(find_records(cfg)),
        "old_quality_pass_count": 0,
        "old_low_quality_count": len(actions),
        "old_low_quality_action_count": len(actions),
        "old_low_quality_without_action_count": 0,
        "recommended_action_count": len(actions),
        "recommended_actions": actions,
    }
    plan_path = path or os.path.join(
        cfg["vault_path"],
        "04-Feedback",
        "_lifecycle-proposals",
        "old-memory-lifecycle-plan-before-2026-07-14.md",
    )
    write_text(plan_path, _render_old_memory_lifecycle_plan(snapshot))
    return plan_path, canonical_sha256 or computed_sha, actions


def create_matching_proposal(cfg, action):
    return create_proposal(
        cfg,
        action=action["action"],
        memory_id=action["memory_id"],
        reason=action["reason"],
        evidence_refs=action["evidence_refs"],
        replacement_id=action["replacement_id"],
        expected_revision=action["expected_revision"],
        replacement_revision=action["replacement_revision"],
    )


def rollback_root(vault):
    return os.path.join(vault, "04-Feedback", "_rollback", "lifecycle")


def audit_path(vault):
    return os.path.join(vault, "05-Agent-Memory", "lifecycle-audit.md")


def latest_manifest(vault):
    root = rollback_root(vault)
    manifests = []
    for directory, _subdirectories, filenames in os.walk(root):
        if "manifest.json" in filenames:
            manifests.append(os.path.join(directory, "manifest.json"))
    return sorted(manifests)[-1]


def read_manifest(result):
    return json.loads(read_text(result["rollback_manifest"]))


if __name__ == "__main__":
    unittest.main()
