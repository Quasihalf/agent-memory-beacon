import hashlib
import json
import os
import sys
import unittest
import tempfile

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from knowledge_index import rebuild_vault_knowledge_indexes
from link_validator import run as validate_links
from memory_identity_repair import (
    IdentityRepairError,
    IdentityRepairPreconditionError,
    apply_identity_repair,
    preview_identity_repair,
)
from memory_quality_audit import (
    audit_formal_memories,
    write_identity_conflict_plan,
)
from memory_schema import RUNTIME_SCHEMA_VERSION, memory_revision


class MemoryIdentityRepairTests(unittest.TestCase):
    def test_identity_repair_has_a_dedicated_guarded_entry_point(self):
        script = os.path.join(REPO_ROOT, "scripts", "memory_identity_repair.py")

        self.assertTrue(
            os.path.isfile(script),
            "identity conflict repair must not bypass the guarded batch executor",
        )

    def test_preview_binds_the_exact_plan_without_writing(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            paths = write_distinct_conflict(vault)
            plan_path, plan_sha = write_plan(cfg)
            before = {path: read_bytes(path) for path in paths}

            preview = preview_identity_repair(cfg, plan_path, plan_sha)

            self.assertEqual(preview["plan_sha256"], plan_sha)
            self.assertEqual(preview["conflict_count"], 1)
            self.assertEqual(preview["action_count"], 1)
            self.assertEqual(preview["actions"][0]["action"], "rekey_and_keep")
            self.assertEqual(
                {path: read_bytes(path) for path in paths},
                before,
            )
            self.assertFalse(os.path.exists(rollback_root(vault)))

    def test_apply_entry_point_is_read_only_without_explicit_apply_authority(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            paths = write_distinct_conflict(vault)
            plan_path, plan_sha = write_plan(cfg)
            before = {path: read_bytes(path) for path in paths}

            result = apply_identity_repair(cfg, plan_path, plan_sha)

            self.assertFalse(result["applied"])
            self.assertEqual({path: read_bytes(path) for path in paths}, before)
            self.assertFalse(os.path.exists(rollback_root(vault)))

    def test_preview_rejects_the_wrong_approval_hash(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            paths = write_distinct_conflict(vault)
            plan_path, _plan_sha = write_plan(cfg)
            before = {path: read_bytes(path) for path in paths}

            with self.assertRaisesRegex(
                IdentityRepairPreconditionError,
                "SHA256",
            ):
                preview_identity_repair(cfg, plan_path, "0" * 64)

            self.assertEqual(
                {path: read_bytes(path) for path in paths},
                before,
            )

    def test_preview_rejects_source_digest_drift_after_approval(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            paths = write_distinct_conflict(vault)
            plan_path, plan_sha = write_plan(cfg)
            host_path = paths[0]
            document = read_frontmatter(host_path)
            document["decisions"].append(
                formal_record(
                    "decision-unrelated-after-approval",
                    "decision",
                    "agent-memory-beacon",
                    "审批后新增事实",
                    "不能静默改变已批准快照",
                    source="01-Projects/agent-memory-beacon/Memory/decisions",
                )
            )
            write_aggregate_document(host_path, document)
            changed = read_bytes(host_path)

            with self.assertRaisesRegex(
                IdentityRepairPreconditionError,
                "approved snapshot",
            ):
                preview_identity_repair(cfg, plan_path, plan_sha)

            self.assertEqual(read_bytes(host_path), changed)
            self.assertFalse(os.path.exists(rollback_root(vault)))

    def test_apply_rekeys_and_relocates_a_distinct_active_fact(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            host_path, target_path = write_distinct_conflict(vault)
            plan_path, plan_sha = write_plan(cfg)
            preview = preview_identity_repair(cfg, plan_path, plan_sha)
            proposed_id = preview["actions"][0]["proposed_id"]

            result = apply_identity_repair(
                cfg,
                plan_path,
                plan_sha,
                apply=True,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            host_records = read_frontmatter(host_path)["decisions"]
            target_records = read_frontmatter(target_path)["decisions"]
            owner = by_id(target_records, "decision-shared")
            moved = by_id(target_records, proposed_id)
            self.assertEqual(host_records, [])
            self.assertEqual(owner["text"], "目标项目自己的决定")
            self.assertEqual(moved["text"], "错误路由但有效的决定")
            self.assertEqual(moved["project"], "demo")
            self.assertEqual(moved["status"], "active")
            self.assertNotIn("decision-shared", moved.get("aliases") or [])
            self.assertEqual(
                set(moved["source_refs"]),
                {
                    "note:01-Projects/agent-memory-beacon/Memory/decisions",
                    "note:01-Projects/demo/Memory/decisions",
                },
            )
            self.assertEqual(result["identity_conflict_count_after"], 0)
            self.assertEqual(read_manifest(result)["status"], "applied")
            self.assertTrue(
                os.path.isfile(
                    os.path.join(
                        os.path.dirname(result["rollback_manifest"]),
                        "approved-plan.md",
                    )
                )
            )
            recall_ids = read_recall_ids(vault)
            self.assertIn("decision-shared", recall_ids)
            self.assertIn(proposed_id, recall_ids)
            self.assertIn(proposed_id, read_text(audit_path(vault)))
            self.assertFalse(
                [
                    item
                    for item in validate_links(vault)
                    if item["target"].endswith("/manifest")
                ]
            )

    def test_apply_removes_cross_conflict_aliases_that_shadow_planned_ids(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            host_path = aggregate_path(
                vault,
                "agent-memory-beacon",
                "decisions",
            )
            target_path = aggregate_path(vault, "demo", "decisions")
            first_moved = formal_record(
                "decision-first",
                "decision",
                "demo",
                "迁移第一项",
                "第一项是独立事实",
                source="01-Projects/agent-memory-beacon/Memory/decisions",
            )
            first_moved["aliases"] = ["decision-second"]
            second_moved = formal_record(
                "decision-second",
                "decision",
                "demo",
                "迁移第二项",
                "第二项也是独立事实",
                source="01-Projects/agent-memory-beacon/Memory/decisions",
            )
            first_owner = formal_record(
                "decision-first",
                "decision",
                "demo",
                "保留第一项",
                "第一项原 ID 的所有者",
                source="01-Projects/demo/Memory/decisions",
            )
            second_owner = formal_record(
                "decision-second",
                "decision",
                "demo",
                "保留第二项",
                "第二项原 ID 的所有者",
                source="01-Projects/demo/Memory/decisions",
            )
            write_aggregate(
                host_path,
                "agent-memory-beacon",
                "decisions",
                [first_moved, second_moved],
            )
            write_aggregate(
                target_path,
                "demo",
                "decisions",
                [first_owner, second_owner],
            )
            plan_path, plan_sha = write_plan(cfg)
            preview = preview_identity_repair(cfg, plan_path, plan_sha)
            planned_ids = {
                "decision-first",
                "decision-second",
                *(item["proposed_id"] for item in preview["actions"]),
            }

            apply_identity_repair(
                cfg,
                plan_path,
                plan_sha,
                apply=True,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            records = read_frontmatter(target_path)["decisions"]
            alias_values = {
                alias
                for record in records
                for alias in record.get("aliases") or []
            }
            self.assertTrue(planned_ids.isdisjoint(alias_values))
            recall_counts = read_recall_identity_counts(vault)
            self.assertEqual(
                {memory_id: recall_counts[memory_id] for memory_id in planned_ids},
                {memory_id: 1 for memory_id in planned_ids},
            )

    def test_apply_preserves_unapproved_legacy_misrouted_records(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            host_path, target_path = write_distinct_conflict(vault)
            host = read_frontmatter(host_path)
            unrelated = formal_record(
                "decision-unapproved-misroute",
                "decision",
                "other-project",
                "不属于当前冲突计划",
                "本批不能顺带迁移或拒绝",
                source="01-Projects/agent-memory-beacon/Memory/decisions",
            )
            host["decisions"].append(unrelated)
            write_aggregate_document(host_path, host)
            plan_path, plan_sha = write_plan(cfg)

            apply_identity_repair(
                cfg,
                plan_path,
                plan_sha,
                apply=True,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            remaining = by_id(
                read_frontmatter(host_path)["decisions"],
                unrelated["id"],
            )
            self.assertEqual(remaining, unrelated)
            self.assertEqual(remaining["project"], "other-project")
            self.assertEqual(
                audit_formal_memories(cfg)["identity_conflict_count"],
                0,
            )
            self.assertTrue(os.path.isfile(target_path))

    def test_apply_supersedes_an_exact_duplicate_and_merges_evidence(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            host_path, target_path = write_exact_duplicate_conflict(vault)
            plan_path, plan_sha = write_plan(cfg)
            preview = preview_identity_repair(cfg, plan_path, plan_sha)
            proposed_id = preview["actions"][0]["proposed_id"]

            apply_identity_repair(
                cfg,
                plan_path,
                plan_sha,
                apply=True,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            self.assertEqual(read_frontmatter(host_path)["decisions"], [])
            target_records = read_frontmatter(target_path)["decisions"]
            owner = by_id(target_records, "decision-shared")
            duplicate = by_id(target_records, proposed_id)
            self.assertEqual(
                set(owner["source_refs"]),
                {
                    "note:01-Projects/agent-memory-beacon/Memory/decisions",
                    "note:01-Projects/demo/Memory/decisions",
                },
            )
            self.assertEqual(duplicate["status"], "superseded")
            self.assertEqual(duplicate["superseded_by"], "decision-shared")
            self.assertNotIn("decision-shared", duplicate.get("aliases") or [])
            recall_ids = read_recall_ids(vault)
            self.assertIn("decision-shared", recall_ids)
            self.assertNotIn(proposed_id, recall_ids)

    def test_apply_rekeys_and_retracts_a_placeholder_without_relocation(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            host_path, target_path = write_placeholder_conflict(vault)
            plan_path, plan_sha = write_plan(cfg)
            preview = preview_identity_repair(cfg, plan_path, plan_sha)
            proposed_id = preview["actions"][0]["proposed_id"]

            apply_identity_repair(
                cfg,
                plan_path,
                plan_sha,
                apply=True,
                rebuilders=[rebuild_vault_knowledge_indexes],
            )

            host_records = read_frontmatter(host_path)["decisions"]
            target_records = read_frontmatter(target_path)["decisions"]
            placeholder = by_id(host_records, proposed_id)
            owner = by_id(target_records, "decision-placeholder")
            self.assertEqual(placeholder["status"], "retracted")
            self.assertIn("低质量占位符", placeholder["retracted_reason"])
            self.assertEqual(owner["status"], "retracted")
            self.assertEqual(audit_formal_memories(cfg)["identity_conflict_count"], 0)
            recall_ids = read_recall_ids(vault)
            self.assertNotIn(proposed_id, recall_ids)
            self.assertNotIn("decision-placeholder", recall_ids)

    def test_apply_rolls_back_every_file_when_a_rebuilder_fails(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            host_path, target_path = write_distinct_conflict(vault)
            plan_path, plan_sha = write_plan(cfg)
            recall_path = os.path.join(vault, "05-Agent-Memory", "recall-index.json")
            os.makedirs(os.path.dirname(recall_path), exist_ok=True)
            write_text(recall_path, '{"before": true}\n')
            before = {
                path: read_bytes(path)
                for path in (host_path, target_path, recall_path, plan_path)
            }

            def failing_rebuilder(_cfg):
                write_text(recall_path, '{"during": true}\n')
                raise RuntimeError("injected rebuild failure")

            with self.assertRaisesRegex(IdentityRepairError, "rebuild failure"):
                apply_identity_repair(
                    cfg,
                    plan_path,
                    plan_sha,
                    apply=True,
                    rebuilders=[failing_rebuilder],
                )

            self.assertEqual(
                {
                    path: read_bytes(path)
                    for path in (host_path, target_path, recall_path, plan_path)
                },
                before,
            )
            manifests = find_manifests(vault)
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(read_text(manifests[0]))
            self.assertEqual(manifest["status"], "rolled_back")
            self.assertTrue(
                os.path.isfile(
                    os.path.join(os.path.dirname(manifests[0]), "approved-plan.md")
                )
            )
            self.assertFalse(os.path.exists(audit_path(vault)))

    def test_apply_rejects_a_proposed_id_that_became_used(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = fixture_config(vault)
            host_path, target_path = write_distinct_conflict(vault)
            plan_path, plan_sha = write_plan(cfg)
            preview = preview_identity_repair(cfg, plan_path, plan_sha)
            proposed_id = preview["actions"][0]["proposed_id"]
            collision_path = aggregate_path(vault, "collision", "decisions")
            collision = formal_record(
                proposed_id,
                "decision",
                "collision",
                "审批后占用了建议 ID",
                "执行器必须停止并要求重新批准",
                source="01-Projects/collision/Memory/decisions",
            )
            write_aggregate(collision_path, "collision", "decisions", [collision])
            before = {
                path: read_bytes(path)
                for path in (host_path, target_path, collision_path)
            }

            with self.assertRaisesRegex(
                IdentityRepairPreconditionError,
                "approved snapshot",
            ):
                apply_identity_repair(
                    cfg,
                    plan_path,
                    plan_sha,
                    apply=True,
                    rebuilders=[rebuild_vault_knowledge_indexes],
                )

            self.assertEqual(
                {
                    path: read_bytes(path)
                    for path in (host_path, target_path, collision_path)
                },
                before,
            )
            self.assertFalse(os.path.exists(rollback_root(vault)))


def fixture_config(vault):
    return {
        "vault_path": vault,
        "projects": ["agent-memory-beacon", "demo", "slug", "collision"],
        "project_keywords": {},
        "context_targets": [],
        "codex_profile_path": "",
        "memory_index_path": "",
        "personal_memory": {
            "formal_path": "05-Agent-Memory/personal-memory.md",
        },
        "skill_preferences": {
            "formal_path": "05-Agent-Memory/skill-routing-rules.md",
        },
        "workflow_memory": {
            "formal_path": "05-Agent-Memory/workflow-rules.md",
        },
        "annotation_quality": {
            "report_path": "04-Feedback/memory-quality-report.md",
        },
        "memory_lifecycle": {
            "audit_path": "05-Agent-Memory/lifecycle-audit.md",
            "rollback_dir": "04-Feedback/_rollback/lifecycle",
        },
    }


def write_distinct_conflict(vault):
    host = formal_record(
        "decision-shared",
        "decision",
        "demo",
        "错误路由但有效的决定",
        "迁回真实项目并继续召回",
        source="01-Projects/agent-memory-beacon/Memory/decisions",
    )
    owner = formal_record(
        "decision-shared",
        "decision",
        "demo",
        "目标项目自己的决定",
        "保留原 ID",
        source="01-Projects/demo/Memory/decisions",
    )
    host_path = aggregate_path(vault, "agent-memory-beacon", "decisions")
    target_path = aggregate_path(vault, "demo", "decisions")
    write_aggregate(host_path, "agent-memory-beacon", "decisions", [host])
    write_aggregate(target_path, "demo", "decisions", [owner])
    return host_path, target_path


def write_exact_duplicate_conflict(vault):
    host = formal_record(
        "decision-shared",
        "decision",
        "demo",
        "统一运行目录",
        "避免 Hook 依赖开发仓库",
        source="01-Projects/agent-memory-beacon/Memory/decisions",
    )
    owner = formal_record(
        "decision-shared",
        "decision",
        "demo",
        "统一运行目录",
        "避免 Hook 依赖开发仓库",
        source="01-Projects/demo/Memory/decisions",
    )
    host_path = aggregate_path(vault, "agent-memory-beacon", "decisions")
    target_path = aggregate_path(vault, "demo", "decisions")
    write_aggregate(host_path, "agent-memory-beacon", "decisions", [host])
    write_aggregate(target_path, "demo", "decisions", [owner])
    return host_path, target_path


def write_placeholder_conflict(vault):
    host = formal_record(
        "decision-placeholder",
        "decision",
        "agent-memory-beacon",
        "summary",
        "why",
        source="01-Projects/agent-memory-beacon/Memory/decisions",
    )
    owner = formal_record(
        "decision-placeholder",
        "decision",
        "slug",
        "summary",
        "why",
        status="retracted",
        source="01-Projects/slug/Memory/decisions",
    )
    host_path = aggregate_path(vault, "agent-memory-beacon", "decisions")
    target_path = aggregate_path(vault, "slug", "decisions")
    write_aggregate(host_path, "agent-memory-beacon", "decisions", [host])
    write_aggregate(target_path, "slug", "decisions", [owner])
    return host_path, target_path


def formal_record(
    memory_id,
    memory_type,
    project,
    title,
    summary,
    *,
    source,
    status="active",
):
    record = {
        "id": memory_id,
        "status": status,
        "project": project,
        "scope": "project",
        "date": "2026-07-18",
        "source_refs": [f"note:{source}"],
        "aliases": [],
    }
    if memory_type == "decision":
        record.update({"text": title, "context": summary})
    else:
        record.update({"type": title, "resolution": summary})
    record["revision"] = memory_revision(
        {
            "type": memory_type,
            "status": status,
            "project": project,
            "scope": "project",
            "title": title,
            "summary": summary,
        }
    )
    return record


def aggregate_path(vault, project, key):
    filename = "decisions.md" if key == "decisions" else "pitfalls.md"
    return os.path.join(vault, "01-Projects", project, "Memory", filename)


def write_aggregate(path, project, key, records):
    document = {
        "title": f"{project} {key}",
        "summary_type": key,
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "project": project,
        key: records,
    }
    write_aggregate_document(path, document)


def write_aggregate_document(path, document):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = (
        "---\n"
        + yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        + "---\n\n# Formal Memory\n"
    )
    write_text(path, content)


def write_plan(cfg):
    report = audit_formal_memories(cfg)
    path = write_identity_conflict_plan(cfg, report)
    digest = hashlib.sha256(read_bytes(path)).hexdigest()
    return path, digest


def read_frontmatter(path):
    text = read_text(path)
    return yaml.safe_load(text.split("---", 2)[1]) or {}


def read_recall_ids(vault):
    path = os.path.join(vault, "05-Agent-Memory", "recall-index.json")
    payload = json.loads(read_text(path))
    return {
        identity
        for item in payload.get("units", [])
        for identity in [item.get("id"), *(item.get("aliases") or [])]
        if identity
    }


def read_recall_identity_counts(vault):
    path = os.path.join(vault, "05-Agent-Memory", "recall-index.json")
    payload = json.loads(read_text(path))
    counts = {}
    for item in payload.get("units", []):
        for identity in [item.get("id"), *(item.get("aliases") or [])]:
            if identity:
                counts[identity] = counts.get(identity, 0) + 1
    return counts


def by_id(records, memory_id):
    matches = [record for record in records if record.get("id") == memory_id]
    if len(matches) != 1:
        raise AssertionError(f"expected one {memory_id}, got {len(matches)}")
    return matches[0]


def rollback_root(vault):
    return os.path.join(vault, "04-Feedback", "_rollback", "lifecycle")


def find_manifests(vault):
    root = rollback_root(vault)
    if not os.path.isdir(root):
        return []
    return sorted(
        os.path.join(root, entry, "manifest.json")
        for entry in os.listdir(root)
        if os.path.isfile(os.path.join(root, entry, "manifest.json"))
    )


def audit_path(vault):
    return os.path.join(vault, "05-Agent-Memory", "lifecycle-audit.md")


def read_manifest(result):
    return json.loads(read_text(result["rollback_manifest"]))


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


def write_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


if __name__ == "__main__":
    unittest.main()
