import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import nullcontext
from unittest.mock import patch

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import legacy_memory_migration
from legacy_memory_migration import (
    _classify_legacy_record,
    apply_migration,
    build_migration_plan,
    rollback_migration,
)
from memory_schema import (
    merge_formal_records,
    normalize_formal_record,
    stable_memory_id,
)
from session_harvester import write_formal_project_records


class LegacyMemoryMigrationTests(unittest.TestCase):
    def test_incomplete_legacy_record_is_retracted_instead_of_recalled(self):
        for memory_type, record in (
            ("decision", {"text": "使用 STHeiti 字体", "context": ""}),
            ("error", {"type": "path-filesystem", "resolution": ""}),
        ):
            with self.subTest(memory_type=memory_type):
                _classify_legacy_record(record, memory_type)
                self.assertEqual(record["status"], "retracted")
                self.assertEqual(
                    record["retracted_reason"],
                    "legacy_incomplete_record",
                )

    def test_process_lifecycle_expires_one_run_state_without_losing_architecture(self):
        transient = [
            "Task 3 判定为 Needs fixes",
            "本轮不修改 old-hand 文件",
            "claim-support-reviewer 报告采用 PASS 结论",
            "不由 reviewer 写入 controller dispatch receipt",
            "不启动 subagent，仅做 same-context Pensive 自审",
        ]
        for title in transient:
            with self.subTest(title=title):
                record = {"text": title, "context": "单轮执行状态", "status": "active"}
                _classify_legacy_record(record, "decision")
                self.assertEqual(record["status"], "expired")
                self.assertEqual(record["expired_reason"], "legacy_process_state")

        durable = [
            "Task 7 使用 descriptor-pinned 原子 apply/rollback 并仅消费权威 binding",
            "将 Supercheck V2 改为 controller/package-only 与 fresh reviewer/final-audit 两阶段流程",
        ]
        for title in durable:
            with self.subTest(title=title):
                record = {"text": title, "context": "长期架构约束", "status": "active"}
                _classify_legacy_record(record, "decision")
                self.assertEqual(record["status"], "active")

        inactive = {
            "text": "历史人工撤回内容",
            "context": "保留原生命周期",
            "status": "retracted",
        }
        _classify_legacy_record(inactive, "decision")
        self.assertEqual(inactive["status"], "retracted")

    def test_preview_normalizes_legacy_memory_without_writing(self):
        with tempfile.TemporaryDirectory() as vault:
            paths = write_legacy_vault(vault)
            before = {path: read_bytes(path) for path in paths}

            plan = build_migration_plan(vault)

            self.assertGreater(plan.stats["planned_writes"], 0)
            self.assertGreater(plan.stats["duplicates_merged"], 0)
            self.assertGreater(plan.stats["candidates_rejected"], 0)
            self.assertEqual({path: read_bytes(path) for path in paths}, before)

            decisions = frontmatter_from_bytes(
                plan.content_for("01-Projects/agent-memory-beacon/Memory/decisions.md")
            )["decisions"]
            self.assertEqual(
                len([item for item in decisions if item["text"] == "Phase A 最终复核判定为 Ready"]),
                1,
            )
            ready = next(
                item for item in decisions
                if item["text"] == "Phase A 最终复核判定为 Ready"
            )
            stale = next(
                item for item in decisions
                if item["text"] == "Phase A 当前判定为 Not Ready"
            )
            stale_process = next(
                item for item in decisions
                if item["text"] == "Task 8 reviewer 结论为 PASS"
            )
            self.assertEqual(ready["status"], "active")
            self.assertEqual(stale["status"], "superseded")
            self.assertEqual(stale["superseded_by"], ready["id"])
            self.assertEqual(stale_process["status"], "expired")
            self.assertEqual(
                stale_process["expired_reason"],
                "legacy_process_state",
            )
            for item in (ready, stale):
                self.assertTrue(item["id"])
                self.assertTrue(item["revision"])
                self.assertEqual(item["scope"], "project")
                self.assertEqual(item["project"], "agent-memory-beacon")
                self.assertTrue(item["source_refs"])

            slug = frontmatter_from_bytes(
                plan.content_for("01-Projects/slug/Memory/decisions.md")
            )["decisions"]
            self.assertFalse(any(item["status"] == "active" for item in slug))

            session = frontmatter_from_bytes(
                plan.content_for(
                    "01-Projects/agent-memory-beacon/Memory/sessions/2026-07-12-state.md"
                )
            )
            self.assertEqual(session["project"], "agent-memory-beacon")
            self.assertEqual(session["memory_schema_version"], "2.0")
            self.assertEqual(
                session["decisions_made"][-1]["status"],
                "active",
            )

            rejected = frontmatter_from_bytes(
                plan.content_for(
                    "04-Feedback/_memory-candidates/用户偏好 TDC 是否需要这些.md"
                )
            )
            self.assertEqual(rejected["status"], "rejected")
            self.assertEqual(rejected["rejection_reason"], "information_question")

            app_storage = frontmatter_from_bytes(
                plan.content_for(
                    "04-Feedback/_skill-preferences/技能偏好 AppStorage.md"
                )
            )
            self.assertEqual(app_storage["status"], "rejected")
            self.assertNotIn("subagent_notification", str(app_storage))

            personal = plan.content_for("05-Agent-Memory/personal-memory.md").decode("utf-8")
            self.assertIn("- status: `active`", personal)
            self.assertIn("- scope: `global`", personal)
            self.assertNotIn("github-obsidian-knowledge-brain", personal)

    def test_schema_v2_formal_records_override_stale_session_identity(self):
        with tempfile.TemporaryDirectory() as vault:
            decisions = os.path.join(
                vault,
                "01-Projects/demo/Memory/decisions.md",
            )
            session = os.path.join(
                vault,
                "01-Projects/demo/Memory/sessions/2026-07-18-stale.md",
            )
            owner = normalize_formal_record(
                {
                    "id": "decision-owner",
                    "text": "采用稳定的正式身份",
                    "context": "正式聚合文件是生命周期权威来源",
                    "status": "active",
                    "project": "demo",
                    "scope": "project",
                    "date": "2026-07-18",
                    "source_refs": ["session:owner"],
                },
                memory_type="decision",
                default_project="demo",
            )
            repaired = normalize_formal_record(
                {
                    "id": "decision-repaired",
                    "text": "Task 8 reviewer 结论为 PASS",
                    "context": "该记录已由正式生命周期流程确认保留",
                    "status": "active",
                    "project": "demo",
                    "scope": "project",
                    "date": "2026-07-18",
                    "source_refs": ["session:repaired"],
                    "requires": ["decision-owner"],
                    "expires_at": "2030-01-01T00:00:00+08:00",
                },
                memory_type="decision",
                default_project="demo",
            )
            incomplete = normalize_formal_record(
                {
                    "id": "decision-incomplete",
                    "text": "旧解析残片",
                    "context": "",
                    "status": "active",
                    "project": "demo",
                    "scope": "project",
                    "date": "2026-07-18",
                    "source_refs": ["session:incomplete"],
                },
                memory_type="decision",
                default_project="demo",
            )
            write_formal_project_note(
                decisions,
                "demo",
                "decisions",
                [owner, repaired, incomplete],
            )
            stale = dict(repaired)
            stale["id"] = owner["id"]
            stale.pop("requires", None)
            stale.pop("expires_at", None)
            write_text(
                session,
                render_frontmatter(
                    {
                        "session_id": "stale-session",
                        "date": "2026-07-18",
                        "project": "demo",
                        "projects": ["demo"],
                        "memory_schema_version": "2.0",
                        "decisions_made": [serialize_session_decision(stale)],
                        "errors_encountered": [],
                    },
                    "# Historical evidence\n",
                ),
            )

            plan = build_migration_plan(vault)
            planned = frontmatter_for_plan_or_path(plan, decisions)["decisions"]
            by_id = {item["id"]: item for item in planned}

            self.assertEqual(
                set(by_id),
                {"decision-owner", "decision-repaired", "decision-incomplete"},
            )
            self.assertEqual(by_id["decision-owner"]["status"], "active")
            self.assertEqual(by_id["decision-repaired"]["status"], "active")
            self.assertEqual(by_id["decision-incomplete"]["status"], "active")
            self.assertEqual(
                by_id["decision-repaired"]["requires"],
                ["decision-owner"],
            )
            self.assertEqual(
                by_id["decision-repaired"]["expires_at"],
                "2030-01-01T00:00:00+08:00",
            )
            self.assertNotIn(
                os.path.relpath(session, vault),
                {item.relative_path for item in plan.writes},
            )

    def test_applied_migration_has_an_empty_second_preview(self):
        with tempfile.TemporaryDirectory() as vault:
            write_legacy_vault(vault)
            first = build_migration_plan(vault)
            apply_migration(
                first,
                migration_id="idempotent-memory-v2",
                guard_factory=lambda _vault: nullcontext(),
            )

            second = build_migration_plan(vault)

            self.assertEqual(second.stats["planned_writes"], 0)
            self.assertEqual(second.writes, ())

    def test_harvester_record_order_keeps_migration_preview_idempotent(self):
        with tempfile.TemporaryDirectory() as vault:
            path = os.path.join(
                vault,
                "01-Projects/demo/Memory/pitfalls.md",
            )
            os.makedirs(os.path.dirname(path), exist_ok=True)
            records = [
                normalize_formal_record(
                    {
                        "id": "error-zeta-old",
                        "type": "zeta",
                        "resolution": "旧日期但 identity 排序靠后，修复后验证通过",
                        "status": "active",
                        "project": "demo",
                        "scope": "project",
                        "date": "2026-07-01",
                        "source_refs": ["session:zeta"],
                    },
                    memory_type="error",
                    default_project="demo",
                ),
                normalize_formal_record(
                    {
                        "id": "error-alpha-new",
                        "type": "alpha",
                        "resolution": "新日期但 identity 排序靠前，修复后验证通过",
                        "status": "active",
                        "project": "demo",
                        "scope": "project",
                        "date": "2026-07-02",
                        "source_refs": ["session:alpha"],
                    },
                    memory_type="error",
                    default_project="demo",
                ),
            ]
            write_formal_project_records(
                path,
                "pitfalls",
                "demo",
                merge_formal_records(records),
            )
            write_formal_project_records(
                os.path.join(
                    vault,
                    "01-Projects/demo/Memory/decisions.md",
                ),
                "decisions",
                "demo",
                [],
            )

            plan = build_migration_plan(vault)

            self.assertEqual(plan.stats["planned_writes"], 0)
            self.assertEqual(plan.writes, ())

    def test_schema_v2_candidate_is_not_rewritten_by_legacy_cleanup(self):
        with tempfile.TemporaryDirectory() as vault:
            candidate = os.path.join(
                vault,
                "04-Feedback/_skill-preferences/技能偏好 humanizer.md",
            )
            content = render_frontmatter(
                {
                    "memory_id": "skill-humanizer",
                    "status": "candidate",
                    "type": "skill_preference",
                    "skill_name": "humanizer",
                    "evidence_excerpt": (
                        "[$humanizer](/Users/demo/.codex/skills/humanizer/SKILL.md)"
                    ),
                    "schema_version": "2.0",
                    "revision": "a" * 64,
                },
                "# Candidate evidence\n",
            )
            write_text(candidate, content)

            plan = build_migration_plan(vault)

            self.assertNotIn(
                os.path.relpath(os.path.realpath(candidate), os.path.realpath(vault)),
                {item.relative_path for item in plan.writes},
            )
            self.assertEqual(read_bytes(candidate), content.encode("utf-8"))

    def test_relocated_schema_v2_record_is_idempotent_after_one_apply(self):
        with tempfile.TemporaryDirectory() as vault:
            source = os.path.join(
                vault,
                "01-Projects/host/Memory/decisions.md",
            )
            record = normalize_formal_record(
                {
                    "id": "decision-relocated",
                    "text": "归位到记录自己的项目",
                    "context": "容器项目不是正式记录的项目归属",
                    "status": "active",
                    "project": "target",
                    "scope": "project",
                    "date": "2026-07-18",
                    "source_refs": ["session:relocation"],
                },
                memory_type="decision",
                default_project="target",
            )
            write_formal_project_note(
                source,
                "host",
                "decisions",
                [record],
            )

            first = build_migration_plan(vault)
            apply_migration(
                first,
                migration_id="relocation-idempotence",
                guard_factory=lambda _vault: nullcontext(),
            )
            second = build_migration_plan(vault)
            target = os.path.join(
                vault,
                "01-Projects/target/Memory/decisions.md",
            )
            relocated = frontmatter_for_plan_or_path(second, target)["decisions"][0]

            self.assertEqual(second.stats["planned_writes"], 0)
            self.assertEqual(relocated["id"], record["id"])
            self.assertEqual(relocated["source_refs"], ["session:relocation"])

    def test_apply_creates_verified_backup_and_rollback_restores_exact_bytes(self):
        with tempfile.TemporaryDirectory() as vault:
            paths = write_legacy_vault(vault)
            before = {path: read_bytes(path) for path in paths}
            plan = build_migration_plan(vault)

            result = apply_migration(
                plan,
                migration_id="test-memory-v2",
                guard_factory=lambda _vault: nullcontext(),
            )

            manifest_path = result["manifest"]
            self.assertTrue(os.path.exists(manifest_path))
            with open(manifest_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            self.assertEqual(manifest["schema_version"], "2.0")
            self.assertEqual(manifest["status"], "prepared")
            self.assertEqual(len(manifest["files"]), len(plan.writes))
            self.assertNotEqual(
                read_bytes(paths[0]),
                before[paths[0]],
            )

            rollback_migration(
                vault,
                manifest_path,
                guard_factory=lambda _vault: nullcontext(),
            )

            self.assertEqual({path: read_bytes(path) for path in paths}, before)

    def test_apply_rejects_input_drift_before_backup_or_write(self):
        with tempfile.TemporaryDirectory() as vault:
            paths = write_legacy_vault(vault)
            plan = build_migration_plan(vault)
            write_text(paths[0], read_bytes(paths[0]).decode("utf-8") + "\nchanged\n")

            with self.assertRaisesRegex(RuntimeError, "changed after preview"):
                apply_migration(
                    plan,
                    migration_id="drift-memory-v2",
                    guard_factory=lambda _vault: nullcontext(),
                )

            self.assertFalse(
                os.path.exists(
                    os.path.join(
                        vault,
                        "04-Feedback/_rollback/memory-v2/drift-memory-v2",
                    )
                )
            )

    def test_apply_preserves_edit_made_after_backup_before_first_write(self):
        with tempfile.TemporaryDirectory() as vault:
            paths = write_legacy_vault(vault)
            plan = build_migration_plan(vault)
            original_create_backup = legacy_memory_migration._create_backup
            concurrent = read_bytes(paths[0]) + b"\nconcurrent edit after backup\n"

            def create_backup_then_edit(current_plan, migration_id):
                manifest = original_create_backup(current_plan, migration_id)
                with open(paths[0], "wb") as handle:
                    handle.write(concurrent)
                return manifest

            with (
                patch.object(
                    legacy_memory_migration,
                    "_create_backup",
                    side_effect=create_backup_then_edit,
                ),
                self.assertRaisesRegex(RuntimeError, "changed after backup"),
            ):
                apply_migration(
                    plan,
                    migration_id="post-backup-drift",
                    guard_factory=lambda _vault: nullcontext(),
                )

            self.assertEqual(read_bytes(paths[0]), concurrent)

    def test_apply_preserves_edit_made_during_atomic_publish_window(self):
        with tempfile.TemporaryDirectory() as vault:
            write_legacy_vault(vault)
            plan = build_migration_plan(vault)
            target = os.path.join(plan.vault, plan.writes[0].relative_path)
            concurrent = read_bytes(target) + b"\nconcurrent edit during publish\n"
            original_atomic_write = legacy_memory_migration._atomic_write_bytes
            injected = False

            def atomic_write_after_edit(path, content, *args, **kwargs):
                nonlocal injected
                if os.fspath(path) == target and not injected:
                    injected = True
                    with open(target, "wb") as handle:
                        handle.write(concurrent)
                return original_atomic_write(path, content, *args, **kwargs)

            with (
                patch.object(
                    legacy_memory_migration,
                    "_atomic_write_bytes",
                    side_effect=atomic_write_after_edit,
                ),
                self.assertRaisesRegex(RuntimeError, "changed during atomic publish"),
            ):
                apply_migration(
                    plan,
                    migration_id="atomic-publish-drift",
                    guard_factory=lambda _vault: nullcontext(),
                )

            self.assertTrue(injected)
            self.assertEqual(read_bytes(target), concurrent)

    def test_first_publish_fsync_failure_triggers_automatic_rollback(self):
        with tempfile.TemporaryDirectory() as vault:
            write_legacy_vault(vault)
            plan = build_migration_plan(vault)
            first = plan.writes[0]
            target = os.path.join(plan.vault, first.relative_path)
            before = read_bytes(target) if first.existed_before else None
            original_fsync = legacy_memory_migration.os.fsync
            original_create_backup = legacy_memory_migration._create_backup
            failed_directory_fsync = False

            def fail_directory_fsync(fd):
                nonlocal failed_directory_fsync
                current = os.fstat(fd)
                if stat.S_ISDIR(current.st_mode) and not failed_directory_fsync:
                    failed_directory_fsync = True
                    raise OSError("simulated post-publish fsync failure")
                return original_fsync(fd)

            def create_backup_with_real_fsync(current_plan, migration_id):
                with patch.object(
                    legacy_memory_migration.os,
                    "fsync",
                    side_effect=original_fsync,
                ):
                    return original_create_backup(current_plan, migration_id)

            with (
                patch.object(
                    legacy_memory_migration,
                    "_create_backup",
                    side_effect=create_backup_with_real_fsync,
                ),
                patch.object(
                    legacy_memory_migration.os,
                    "fsync",
                    side_effect=fail_directory_fsync,
                ),
                self.assertRaisesRegex(OSError, "post-publish fsync failure"),
            ):
                apply_migration(
                    plan,
                    migration_id="post-publish-fsync-failure",
                    guard_factory=lambda _vault: nullcontext(),
                )

            self.assertTrue(failed_directory_fsync)
            if first.existed_before:
                self.assertEqual(read_bytes(target), before)
            else:
                self.assertFalse(os.path.lexists(target))

    def test_guard_exit_failure_reacquires_lock_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as vault:
            paths = write_legacy_vault(vault)
            before = {path: read_bytes(path) for path in paths}
            plan = build_migration_plan(vault)
            exits = 0

            class FailFirstExit:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    nonlocal exits
                    exits += 1
                    if exits == 1:
                        raise RuntimeError("simulated guard exit failure")
                    return False

            with self.assertRaisesRegex(RuntimeError, "guard exit failure"):
                apply_migration(
                    plan,
                    migration_id="guard-exit-failure",
                    guard_factory=lambda _vault: FailFirstExit(),
                )

            self.assertGreaterEqual(exits, 2)
            self.assertEqual({path: read_bytes(path) for path in paths}, before)

    def test_atomic_publish_restores_edit_made_after_digest_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "memory.md")
            original = b"original memory\n"
            desired = b"schema 2 memory\n"
            concurrent = original + b"concurrent edit after digest\n"
            with open(target, "wb") as handle:
                handle.write(original)
            expected_sha = legacy_memory_migration._sha256(original)
            original_sha256 = legacy_memory_migration._sha256
            injected = False

            def digest_then_edit(content):
                nonlocal injected
                digest = original_sha256(content)
                if content == original and not injected:
                    injected = True
                    with open(target, "wb") as handle:
                        handle.write(concurrent)
                return digest

            with (
                patch.object(
                    legacy_memory_migration,
                    "_sha256",
                    side_effect=digest_then_edit,
                ),
                self.assertRaisesRegex(RuntimeError, "changed during atomic publish"),
            ):
                legacy_memory_migration._atomic_write_bytes(
                    target,
                    desired,
                    expected_sha256=expected_sha,
                )

            self.assertTrue(injected)
            self.assertEqual(read_bytes(target), concurrent)

    def test_rollback_preflights_every_target_before_restoring_any_file(self):
        with tempfile.TemporaryDirectory() as vault:
            write_legacy_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_migration(
                plan,
                migration_id="rollback-preflight",
                guard_factory=lambda _vault: nullcontext(),
            )
            first = os.path.join(vault, plan.writes[0].relative_path)
            later = os.path.join(vault, plan.writes[1].relative_path)
            first_desired = read_bytes(first)
            with open(later, "ab") as handle:
                handle.write(b"\nconcurrent edit before rollback\n")

            with self.assertRaisesRegex(RuntimeError, "changed before rollback"):
                rollback_migration(
                    vault,
                    result["manifest"],
                    guard_factory=lambda _vault: nullcontext(),
                )

            self.assertEqual(read_bytes(first), first_desired)

    def test_interrupted_rollback_can_retry_after_an_existing_file_was_restored(self):
        with tempfile.TemporaryDirectory() as vault:
            write_legacy_vault(vault)
            plan = build_migration_plan(vault)
            first_existing = next(item for item in plan.writes if item.existed_before)
            target = os.path.join(plan.vault, first_existing.relative_path)
            before = read_bytes(target)
            result = apply_migration(
                plan,
                migration_id="rollback-retry-existing",
                guard_factory=lambda _vault: nullcontext(),
            )
            original_atomic_write = legacy_memory_migration._atomic_write_bytes
            interrupted = False

            def restore_then_interrupt(path, content, *args, **kwargs):
                nonlocal interrupted
                restored = original_atomic_write(path, content, *args, **kwargs)
                if os.fspath(path) == target and not interrupted:
                    interrupted = True
                    raise RuntimeError("simulated rollback interruption")
                return restored

            with (
                patch.object(
                    legacy_memory_migration,
                    "_atomic_write_bytes",
                    side_effect=restore_then_interrupt,
                ),
                self.assertRaisesRegex(RuntimeError, "simulated rollback interruption"),
            ):
                rollback_migration(
                    vault,
                    result["manifest"],
                    guard_factory=lambda _vault: nullcontext(),
                )

            self.assertTrue(interrupted)
            self.assertEqual(read_bytes(target), before)
            retry = rollback_migration(
                vault,
                result["manifest"],
                guard_factory=lambda _vault: nullcontext(),
            )
            self.assertEqual(retry["status"], "rolled_back")
            self.assertEqual(read_bytes(target), before)

    def test_rollback_accepts_a_new_file_that_is_already_absent(self):
        with tempfile.TemporaryDirectory() as vault:
            write_legacy_vault(vault)
            base = build_migration_plan(vault)
            relative = "05-Agent-Memory/retry-new-file.md"
            content = b"schema 2 generated memory\n"
            extra = legacy_memory_migration.PlannedWrite(
                relative_path=relative,
                content=content,
                existed_before=False,
                before_sha256="",
                desired_sha256=legacy_memory_migration._sha256(content),
                reason="rollback-idempotence-test",
            )
            plan = legacy_memory_migration.LegacyMemoryMigrationPlan(
                vault=base.vault,
                created_at=base.created_at,
                writes=base.writes + (extra,),
                stats={**base.stats, "planned_writes": len(base.writes) + 1},
            )
            result = apply_migration(
                plan,
                migration_id="rollback-retry-new",
                guard_factory=lambda _vault: nullcontext(),
            )
            target = os.path.join(vault, relative)
            os.unlink(target)

            retry = rollback_migration(
                vault,
                result["manifest"],
                guard_factory=lambda _vault: nullcontext(),
            )

            self.assertEqual(retry["status"], "rolled_back")
            self.assertFalse(os.path.lexists(target))

    def test_rollback_preserves_new_file_replaced_after_digest_check(self):
        with tempfile.TemporaryDirectory() as vault:
            write_legacy_vault(vault)
            base = build_migration_plan(vault)
            relative = "05-Agent-Memory/concurrent-new-file.md"
            desired = b"schema 2 generated memory\n"
            extra = legacy_memory_migration.PlannedWrite(
                relative_path=relative,
                content=desired,
                existed_before=False,
                before_sha256="",
                desired_sha256=legacy_memory_migration._sha256(desired),
                reason="rollback-toctou-test",
            )
            plan = legacy_memory_migration.LegacyMemoryMigrationPlan(
                vault=base.vault,
                created_at=base.created_at,
                writes=base.writes + (extra,),
                stats={**base.stats, "planned_writes": len(base.writes) + 1},
            )
            result = apply_migration(
                plan,
                migration_id="rollback-new-file-race",
                guard_factory=lambda _vault: nullcontext(),
            )
            target = os.path.join(vault, relative)
            concurrent = b"concurrent replacement\n"
            original_sha256 = legacy_memory_migration._sha256
            desired_hashes = 0

            def digest_then_replace(content):
                nonlocal desired_hashes
                digest = original_sha256(content)
                if content == desired:
                    desired_hashes += 1
                    if desired_hashes == 2:
                        replacement = target + ".replacement"
                        with open(replacement, "wb") as handle:
                            handle.write(concurrent)
                        os.replace(replacement, target)
                return digest

            with (
                patch.object(
                    legacy_memory_migration,
                    "_sha256",
                    side_effect=digest_then_replace,
                ),
                self.assertRaisesRegex(RuntimeError, "rollback|concurrent"),
            ):
                rollback_migration(
                    vault,
                    result["manifest"],
                    guard_factory=lambda _vault: nullcontext(),
                )

            self.assertEqual(read_bytes(target), concurrent)

    def test_backup_is_fsynced_and_sealed_before_publication(self):
        with tempfile.TemporaryDirectory() as vault:
            write_legacy_vault(vault)
            plan = build_migration_plan(vault)
            original_fsync = legacy_memory_migration.os.fsync
            fsync_calls = 0
            fsynced_inodes = set()

            def count_fsync(fd):
                nonlocal fsync_calls
                fsync_calls += 1
                current = os.fstat(fd)
                if stat.S_ISDIR(current.st_mode):
                    fsynced_inodes.add((current.st_dev, current.st_ino))
                return original_fsync(fd)

            with patch.object(
                legacy_memory_migration.os,
                "fsync",
                side_effect=count_fsync,
            ):
                manifest_path = legacy_memory_migration._create_backup(
                    plan,
                    "sealed-backup",
                )

            backup_root = os.path.dirname(manifest_path)
            self.assertGreater(fsync_calls, len(plan.writes))
            for directory in (
                vault,
                os.path.join(vault, "04-Feedback"),
                os.path.join(vault, "04-Feedback/_rollback"),
                os.path.join(vault, "04-Feedback/_rollback/memory-v2"),
            ):
                current = os.stat(directory)
                self.assertIn(
                    (current.st_dev, current.st_ino),
                    fsynced_inodes,
                )
            self.assertEqual(stat.S_IMODE(os.stat(backup_root).st_mode), 0o500)
            self.assertEqual(stat.S_IMODE(os.stat(manifest_path).st_mode), 0o400)
            with open(manifest_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            for item in manifest["files"]:
                if not item["existed_before"]:
                    continue
                backup = os.path.join(backup_root, item["backup"])
                self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o400)

    def test_rollback_rejects_coordinated_manifest_and_backup_tampering(self):
        with tempfile.TemporaryDirectory() as vault:
            write_legacy_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_migration(
                plan,
                migration_id="tampered-sealed-backup",
                guard_factory=lambda _vault: nullcontext(),
            )
            manifest_path = result["manifest"]
            backup_root = os.path.dirname(manifest_path)
            os.chmod(backup_root, 0o700)
            os.chmod(manifest_path, 0o600)
            with open(manifest_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            item = next(entry for entry in manifest["files"] if entry["existed_before"])
            forged = b"forged rollback content\n"
            backup = os.path.join(backup_root, item["backup"])
            os.chmod(backup, 0o600)
            with open(backup, "wb") as handle:
                handle.write(forged)
            item["before_sha256"] = legacy_memory_migration._sha256(forged)
            with open(manifest_path, "w", encoding="utf-8") as handle:
                json.dump(manifest, handle, ensure_ascii=False, indent=2)
                handle.write("\n")

            with self.assertRaisesRegex(
                (RuntimeError, ValueError),
                "sealed|manifest|backup",
            ):
                rollback_migration(
                    vault,
                    manifest_path,
                    guard_factory=lambda _vault: nullcontext(),
                )

    def test_rollback_rejects_symlinked_backup_ancestor(self):
        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            write_legacy_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_migration(
                plan,
                migration_id="symlinked-backup-ancestor",
                guard_factory=lambda _vault: nullcontext(),
            )
            memory_v2 = os.path.join(
                vault,
                "04-Feedback/_rollback/memory-v2",
            )
            outside = os.path.join(root, "outside-memory-v2")
            os.rename(memory_v2, outside)
            os.symlink(outside, memory_v2, target_is_directory=True)

            with self.assertRaisesRegex(
                (RuntimeError, ValueError, OSError),
                "symlink|outside|manifest|backup",
            ):
                rollback_migration(
                    vault,
                    result["manifest"],
                    guard_factory=lambda _vault: nullcontext(),
                )

    def test_rollback_does_not_write_backup_changed_after_digest_check(self):
        with tempfile.TemporaryDirectory() as vault:
            paths = write_legacy_vault(vault)
            before = {path: read_bytes(path) for path in paths}
            plan = build_migration_plan(vault)
            result = apply_migration(
                plan,
                migration_id="backup-read-race",
                guard_factory=lambda _vault: nullcontext(),
            )
            item = next(
                entry
                for entry in plan.writes
                if entry.existed_before
                and os.path.join(vault, entry.relative_path) in before
            )
            target = os.path.join(vault, item.relative_path)
            desired = read_bytes(target)
            original = before[target]
            manifest_path = result["manifest"]
            with open(manifest_path, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
            record = next(
                entry for entry in manifest["files"]
                if entry["path"] == item.relative_path
            )
            backup_root = os.path.dirname(manifest_path)
            backup = os.path.join(backup_root, record["backup"])
            original_sha256 = legacy_memory_migration._sha256
            changed = False

            def digest_then_change_backup(content):
                nonlocal changed
                digest = original_sha256(content)
                if content == original and not changed:
                    changed = True
                    os.chmod(backup_root, 0o700)
                    os.chmod(backup, 0o600)
                    with open(backup, "wb") as handle:
                        handle.write(b"forged after digest\n")
                return digest

            with (
                patch.object(
                    legacy_memory_migration,
                    "_sha256",
                    side_effect=digest_then_change_backup,
                ),
                self.assertRaisesRegex(RuntimeError, "backup|rollback"),
            ):
                rollback_migration(
                    vault,
                    manifest_path,
                    guard_factory=lambda _vault: nullcontext(),
                )

            self.assertTrue(changed)
            self.assertEqual(read_bytes(target), desired)

    def test_rollback_rejects_recovery_file_mtime_changes(self):
        for evidence_kind in ("manifest", "backup"):
            with self.subTest(evidence_kind=evidence_kind):
                with tempfile.TemporaryDirectory() as vault:
                    write_legacy_vault(vault)
                    plan = build_migration_plan(vault)
                    result = apply_migration(
                        plan,
                        migration_id=f"mtime-{evidence_kind}",
                        guard_factory=lambda _vault: nullcontext(),
                    )
                    manifest_path = result["manifest"]
                    with open(manifest_path, "r", encoding="utf-8") as handle:
                        manifest = json.load(handle)
                    item = next(
                        entry for entry in manifest["files"]
                        if entry["existed_before"]
                    )
                    target = os.path.join(vault, item["path"])
                    desired = read_bytes(target)
                    evidence = manifest_path
                    if evidence_kind == "backup":
                        evidence = os.path.join(
                            os.path.dirname(manifest_path),
                            item["backup"],
                        )
                    current = os.stat(evidence)
                    os.utime(
                        evidence,
                        ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000),
                    )

                    with self.assertRaisesRegex(
                        (RuntimeError, ValueError),
                        "metadata|mtime|sealed",
                    ):
                        rollback_migration(
                            vault,
                            manifest_path,
                            guard_factory=lambda _vault: nullcontext(),
                        )

                    self.assertEqual(read_bytes(target), desired)

    def test_post_apply_rebuild_keeps_manifest_targets_rollbackable(self):
        from session_harvester import rebuild_memory_index

        with tempfile.TemporaryDirectory() as vault:
            decisions = os.path.join(
                vault,
                "01-Projects/demo/Memory/decisions.md",
            )
            original = """---
project: demo
decisions:
- text: 清洗敏感内容
  context: 卡号 4111 1111 1111 1111 必须隐藏
---
# Legacy
"""
            write_text(decisions, original)
            plan = build_migration_plan(vault)
            planned_frontmatter = frontmatter_from_bytes(
                plan.content_for(
                    "01-Projects/demo/Memory/decisions.md"
                )
            )
            planned_record = planned_frontmatter["decisions"][0]
            self.assertEqual(
                planned_record["id"],
                stable_memory_id(
                    "decision",
                    "demo",
                    "note:01-Projects/demo/Memory/decisions",
                    "decisions:0",
                ),
            )
            result = apply_migration(
                plan,
                migration_id="post-apply-rebuild",
                guard_factory=lambda _vault: nullcontext(),
            )
            desired = read_bytes(decisions)

            rebuild_memory_index({"vault_path": vault})

            self.assertEqual(read_bytes(decisions), desired)
            rollback_migration(
                vault,
                result["manifest"],
                guard_factory=lambda _vault: nullcontext(),
            )
            self.assertEqual(read_bytes(decisions), original.encode("utf-8"))

    def test_existing_formal_note_is_rebuilt_empty_when_no_candidate_is_promoted(self):
        with tempfile.TemporaryDirectory() as vault:
            formal = os.path.join(vault, "05-Agent-Memory/personal-memory.md")
            candidate = os.path.join(
                vault,
                "04-Feedback/_memory-candidates/用户偏好 临时问题.md",
            )
            write_text(
                formal,
                """---
title: Personal Memory
---

# Personal Memory

## 旧的错误正式记忆

- id: `stale-memory`
- memory: 这条内容不应继续保留
""",
            )
            write_text(
                candidate,
                """---
memory_id: temporary-question
status: candidate
type: preference
content: 这个功能是否需要？
evidence: 这个功能是否需要？
---
""",
            )

            plan = build_migration_plan(vault)
            rebuilt = plan.content_for("05-Agent-Memory/personal-memory.md").decode("utf-8")

            self.assertIn("schema_version: '2.0'", rebuilt)
            self.assertNotIn("stale-memory", rebuilt)
            self.assertNotIn("这条内容不应继续保留", rebuilt)


def write_legacy_vault(vault):
    session = os.path.join(
        vault,
        "01-Projects/agent-memory-beacon/Memory/sessions/2026-07-12-state.md",
    )
    decisions = os.path.join(
        vault,
        "01-Projects/agent-memory-beacon/Memory/decisions.md",
    )
    pitfalls = os.path.join(
        vault,
        "01-Projects/agent-memory-beacon/Memory/pitfalls.md",
    )
    slug_decisions = os.path.join(vault, "01-Projects/slug/Memory/decisions.md")
    slug_pitfalls = os.path.join(vault, "01-Projects/slug/Memory/pitfalls.md")
    memory_candidate = os.path.join(
        vault,
        "04-Feedback/_memory-candidates/用户偏好 TDC 是否需要这些.md",
    )
    promoted_candidate = os.path.join(
        vault,
        "04-Feedback/_memory-candidates/用户偏好 中文输出.md",
    )
    skill_candidate = os.path.join(
        vault,
        "04-Feedback/_skill-preferences/技能偏好 AppStorage.md",
    )
    personal = os.path.join(vault, "05-Agent-Memory/personal-memory.md")

    write_text(
        session,
        """---
session_id: sess-state
date: '2026-07-12'
project: github-obsidian-knowledge-brain
projects: [github-obsidian-knowledge-brain]
ai_title: Phase A 当前判定为 Not Ready
summary_type: session
decisions_made:
- text: Phase A 当前判定为 Not Ready
  context: 当时仍有阻断问题
- text: Phase A 最终复核判定为 Ready
  context: 最终验证全部通过
errors_encountered:
- type: path-filesystem
  resolution: 改用真实路径
---

# Phase A 当前判定为 Not Ready
""",
    )
    write_text(
        decisions,
        """---
project: github-obsidian-knowledge-brain
decisions:
- text: Phase A 当前判定为 Not Ready
  context: 当时仍有阻断问题
- text: Phase A 最终复核判定为 Ready
  context: 最终验证全部通过
- text: Phase A 最终复核判定为 Ready
  context: 最终验证全部通过
- text: Task 8 reviewer 结论为 PASS
  context: 这只是单轮审查状态
  status: active
---

# Legacy Decisions
""",
    )
    write_text(
        pitfalls,
        """---
project: github-obsidian-knowledge-brain
pitfalls:
- type: path-filesystem
  resolution: 改用真实路径
- type: path-filesystem
  resolution: 改用真实路径
---

# Legacy Pitfalls
""",
    )
    write_text(
        slug_decisions,
        """---
decisions:
- text: Phase A 最终复核判定为 Ready
  context: 最终验证全部通过
- text: summary
  context: why
---
""",
    )
    write_text(
        slug_pitfalls,
        """---
pitfalls:
- type: path-filesystem
  resolution: 改用真实路径
---
""",
    )
    write_text(
        memory_candidate,
        """---
memory_id: preference-question
status: candidate
type: preference
project: tcad
title: '用户偏好: TDC 是否需要这些'
content: 如果以后要完成 TDC 仿真和设计，需要用这些吗
evidence: 如果以后要完成 TDC 仿真和设计，需要用这些吗？
seen_count: 1
---

# 用户偏好: TDC 是否需要这些
""",
    )
    write_text(
        promoted_candidate,
        """---
memory_id: preference-chinese
status: promoted
type: preference
project: github-obsidian-knowledge-brain
title: '用户偏好: 默认用中文输出'
content: 默认用中文输出复杂审查
evidence: 默认用中文输出复杂审查
confidence: 0.9
seen_count: 2
---

# 用户偏好: 默认用中文输出
""",
    )
    write_text(
        skill_candidate,
        """---
memory_id: skillpref-appstorage
status: candidate
type: skill_preference
skill_name: AppStorage
scene_key: generic_manual_skill_invocation
title: '技能偏好: AppStorage'
task_intent: 使用 AppStorage
evidence_excerpt: '<subagent_notification>使用 $AppStorage</subagent_notification>'
seen_count: 1
---

# 技能偏好: AppStorage
""",
    )
    write_text(
        personal,
        """---
title: Personal Memory
generated_by: memory_judge.py
---

# Personal Memory

## 用户偏好: 默认用中文输出

- id: `preference-chinese`
- type: `preference`
- project: [[01-Projects/agent-memory-beacon/Memory/decisions|github-obsidian-knowledge-brain]]
- memory: 默认用中文输出复杂审查
""",
    )
    return [
        session,
        decisions,
        pitfalls,
        slug_decisions,
        slug_pitfalls,
        memory_candidate,
        promoted_candidate,
        skill_candidate,
        personal,
    ]


def frontmatter_from_bytes(content):
    return yaml.safe_load(content.decode("utf-8").split("---", 2)[1]) or {}


def render_frontmatter(frontmatter, body=""):
    return (
        "---\n"
        + yaml.dump(
            frontmatter,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        + "---\n\n"
        + body
    )


def write_formal_project_note(path, project, key, records):
    write_text(
        path,
        render_frontmatter(
            {
                "project": project,
                "schema_version": "2.0",
                key: [serialize_session_decision(item) for item in records],
                "last_updated": "2026-07-18T12:00:00+08:00",
            },
            "# Formal Memory\n",
        ),
    )


def serialize_session_decision(record):
    output = {
        "id": record["id"],
        "revision": record["revision"],
        "text": record["title"],
        "context": record["summary"],
        "status": record["status"],
        "project": record["project"],
        "scope": record["scope"],
        "date": record.get("date", ""),
        "source_refs": list(record.get("source_refs") or []),
        "aliases": list(record.get("aliases") or []),
    }
    for key in (
        "requires",
        "expires_at",
        "superseded_by",
        "retracted_reason",
        "expired_reason",
    ):
        if record.get(key):
            output[key] = record[key]
    return output


def frontmatter_for_plan_or_path(plan, path):
    relative = os.path.relpath(
        os.path.realpath(path),
        os.path.realpath(plan.vault),
    )
    try:
        content = plan.content_for(relative)
    except KeyError:
        content = read_bytes(path)
    return frontmatter_from_bytes(content)


def write_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


if __name__ == "__main__":
    unittest.main()
