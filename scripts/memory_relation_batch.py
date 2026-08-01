#!/usr/bin/env python3
"""Generate and apply exact, approval-bound formal-memory relation plans."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone

import yaml

from config import load_config
from knowledge_index import configured_recall_index_path, graph_path_for_index
from memory_lifecycle import (
    DERIVED_VAULT_PATHS,
    _restore_snapshots,
    _snapshot_file,
    find_records,
)
from memory_quality_audit import (
    _approval_source_digest,
    _approval_source_locator,
)
from memory_schema import (
    MEMORY_RELATION_FIELDS,
    memory_revision,
    normalize_formal_record,
    normalize_memory_relations,
)
from safety import (
    durable_atomic_write,
    ensure_directory_tree,
    exclusive_file_lock,
    redact_sensitive,
    safe_vault_path,
    secure_read_bytes,
    split_frontmatter_text,
)


PLAN_SCHEMA_VERSION = "1.0"
PLAN_SUMMARY_TYPE = "memory-relation-approval-plan"
PLAN_GENERATOR = "memory_relation_batch.py"
MAX_PLAN_BYTES = 8 * 1024 * 1024
ACTION_FIELDS = frozenset(
    {
        "source_id",
        "source_revision",
        "source_type",
        "source_project",
        "source_locator",
        "source_digest",
        "source_digest_scope",
        "source_excerpt",
        "relation",
        "target_id",
        "target_revision",
        "target_type",
        "target_project",
        "target_locator",
        "target_digest",
        "target_digest_scope",
        "target_excerpt",
        "reason",
        "evidence_refs",
    }
)


class RelationBatchError(RuntimeError):
    pass


class RelationBatchPreconditionError(RelationBatchError):
    pass


def write_relation_plan(cfg, proposals, output_path, *, now=None):
    """Freeze explicit relation proposals into a read-only approval plan."""
    vault = _vault(cfg)
    lock_path = _lock_path(vault)
    ensure_directory_tree(os.path.dirname(lock_path), vault)
    with exclusive_file_lock(lock_path, root=vault):
        records = _records_by_id(find_records(cfg))
        actions = [
            _freeze_proposal(proposal, records, vault)
            for proposal in proposals or []
        ]
        if not actions:
            raise ValueError("at least one semantic relation proposal is required")
        actions.sort(
            key=lambda item: (
                item["source_id"],
                item["relation"],
                item["target_id"],
            )
        )
        identities = [
            (item["source_id"], item["relation"], item["target_id"])
            for item in actions
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("semantic relation proposals contain a duplicate edge")
        canonical_sha256 = _canonical_sha256(actions)
        generated_at = _aware_now(now).isoformat()
        content = _render_plan(actions, generated_at, canonical_sha256)
        path = safe_vault_path(vault, os.path.expanduser(str(output_path or "")))
        ensure_directory_tree(os.path.dirname(path), vault)
        durable_atomic_write(path, content, root=vault)
        return {
            "path": path,
            "canonical_sha256": canonical_sha256,
            "action_count": len(actions),
            "actions": actions,
            "read_only": True,
        }


def preview_relation_batch(cfg, plan_path, expected_sha256):
    """Validate an exact pending plan against current formal memory."""
    vault = _vault(cfg)
    lock_path = _lock_path(vault)
    ensure_directory_tree(os.path.dirname(lock_path), vault)
    with exclusive_file_lock(lock_path, root=vault):
        prepared = _prepare_batch(cfg, plan_path, expected_sha256)
        return _preview(prepared)


def apply_relation_batch(
    cfg,
    plan_path,
    expected_sha256,
    *,
    apply=False,
    rebuilders=None,
):
    """Apply an exact approved relation plan and roll back on any failure."""
    if not apply:
        return preview_relation_batch(cfg, plan_path, expected_sha256)
    selected_rebuilders = (
        _default_rebuilders() if rebuilders is None else list(rebuilders)
    )
    if not selected_rebuilders or not all(
        callable(rebuilder) for rebuilder in selected_rebuilders
    ):
        raise ValueError("at least one callable relation rebuilder is required")

    vault = _vault(cfg)
    lock_path = _lock_path(vault)
    ensure_directory_tree(os.path.dirname(lock_path), vault)
    with exclusive_file_lock(lock_path, root=vault):
        prepared = _prepare_batch(cfg, plan_path, expected_sha256)
        snapshots = _create_rollback_snapshots(cfg, prepared)
        try:
            for path in sorted(prepared["rendered_sources"]):
                durable_atomic_write(
                    path,
                    prepared["rendered_sources"][path],
                    root=vault,
                )
            for rebuilder in selected_rebuilders:
                rebuilder(cfg)
            _verify_applied(cfg, prepared)
            applied_at = datetime.now(timezone.utc).isoformat()
            durable_atomic_write(
                prepared["plan_path"],
                _render_applied_plan(
                    prepared["plan_frontmatter"],
                    prepared["plan_body"],
                    prepared["canonical_sha256"],
                    applied_at,
                ),
                root=vault,
            )
        except Exception as exc:
            rollback_errors = []
            try:
                _restore_snapshots(vault, snapshots)
                _verify_restored_snapshots(vault, snapshots)
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))
            if rollback_errors:
                raise RelationBatchError(
                    f"relation batch failed: {exc}; rollback failed: "
                    + "; ".join(rollback_errors)
                ) from exc
            raise RelationBatchError(
                f"relation batch failed and was rolled back: {exc}"
            ) from exc
        return {
            **_preview(prepared),
            "applied": True,
            "resulting_revisions": dict(prepared["resulting_revisions"]),
        }


def _prepare_batch(cfg, plan_path, expected_sha256):
    plan = _load_plan(cfg, plan_path, expected_sha256)
    vault = _vault(cfg)
    records = _records_by_id(find_records(cfg))
    selected = {}
    for action in plan["actions"]:
        source = _exact_live_record(action, "source", records, vault)
        _exact_live_record(action, "target", records, vault)
        existing = source.record.get(action["relation"]) or []
        if action["target_id"] in existing:
            raise RelationBatchPreconditionError(
                "approved semantic relation already exists: "
                f"{source.memory_id} {action['relation']} {action['target_id']}"
            )
        selected.setdefault(source.memory_id, []).append(action)
    rendered_sources, revisions = _render_source_updates(cfg, selected, records)
    return {
        **plan,
        "rendered_sources": rendered_sources,
        "resulting_revisions": revisions,
    }


def _load_plan(cfg, plan_path, expected_sha256):
    vault = _vault(cfg)
    expected = str(expected_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RelationBatchPreconditionError(
            "expected canonical SHA256 must be 64 lowercase hexadecimal characters"
        )
    path = safe_vault_path(vault, os.path.expanduser(str(plan_path or "")))
    try:
        current = os.lstat(path)
    except FileNotFoundError as exc:
        raise RelationBatchPreconditionError("relation plan does not exist") from exc
    if not stat.S_ISREG(current.st_mode):
        raise RelationBatchPreconditionError(
            "relation plan must be a regular Vault file"
        )
    data = secure_read_bytes(path, MAX_PLAN_BYTES, root=vault)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RelationBatchPreconditionError(
            "relation plan is not UTF-8"
        ) from exc
    frontmatter_text, body = split_frontmatter_text(text)
    if frontmatter_text is None or body is None:
        raise RelationBatchPreconditionError("relation plan has no frontmatter")
    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise RelationBatchPreconditionError(
            "relation plan frontmatter is invalid"
        ) from exc
    if not isinstance(frontmatter, dict):
        raise RelationBatchPreconditionError(
            "relation plan frontmatter must be a mapping"
        )
    if (
        frontmatter.get("summary_type") != PLAN_SUMMARY_TYPE
        or frontmatter.get("generated_by") != PLAN_GENERATOR
        or frontmatter.get("schema_version") != PLAN_SCHEMA_VERSION
        or frontmatter.get("read_only") is not True
        or frontmatter.get("approval_status") != "pending"
    ):
        raise RelationBatchPreconditionError(
            "relation plan is not a supported pending approval plan"
        )
    raw_actions = frontmatter.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise RelationBatchPreconditionError("relation plan has no actions")
    actions = [_validate_action(action) for action in raw_actions]
    if frontmatter.get("action_count") != len(actions):
        raise RelationBatchPreconditionError(
            "relation plan action count does not match"
        )
    actual = _canonical_sha256(actions)
    if (
        str(frontmatter.get("canonical_sha256") or "") != actual
        or actual != expected
    ):
        raise RelationBatchPreconditionError(
            "relation plan canonical SHA256 mismatch"
        )
    rendered = _render_plan(
        actions,
        str(frontmatter.get("generated_at") or ""),
        actual,
    ).encode("utf-8")
    if rendered != data:
        raise RelationBatchPreconditionError(
            "relation plan content changed after generation"
        )
    return {
        "plan_path": path,
        "plan_frontmatter": frontmatter,
        "plan_body": body,
        "canonical_sha256": actual,
        "actions": actions,
    }


def _freeze_proposal(proposal, records, vault):
    if not isinstance(proposal, dict):
        raise ValueError("every relation proposal must be a mapping")
    allowed = {
        "source_id",
        "relation",
        "target_id",
        "reason",
        "evidence_refs",
    }
    if set(proposal) != allowed:
        raise ValueError("relation proposal fields changed")
    source_id = str(proposal.get("source_id") or "").strip()
    target_id = str(proposal.get("target_id") or "").strip()
    relation = str(proposal.get("relation") or "").strip()
    source = _one_record(records, source_id)
    target = _one_record(records, target_id)
    if source.status != "active" or target.status != "active":
        raise ValueError("semantic relations require active source and target memories")
    if source_id == target_id:
        raise ValueError("semantic relation cannot target its source")
    if relation not in MEMORY_RELATION_FIELDS:
        raise ValueError(f"unsupported semantic relation: {relation}")
    normalize_memory_relations([target_id], memory_id=source_id, field=relation)
    if target_id in (source.record.get(relation) or []):
        raise ValueError("semantic relation already exists")
    reason = _one_line(redact_sensitive(proposal.get("reason")))
    evidence_refs = [
        _one_line(redact_sensitive(item))
        for item in proposal.get("evidence_refs") or []
        if _one_line(redact_sensitive(item))
    ]
    if not reason or not evidence_refs:
        raise ValueError("semantic relation requires a reason and evidence refs")
    return {
        "source_id": source.memory_id,
        "source_revision": source.revision,
        "source_type": source.memory_type,
        "source_project": source.project,
        "source_locator": _approval_source_locator(source, vault),
        "source_digest": _approval_source_digest(source),
        "source_digest_scope": "canonical-record-v1",
        "source_excerpt": _excerpt(source),
        "relation": relation,
        "target_id": target.memory_id,
        "target_revision": target.revision,
        "target_type": target.memory_type,
        "target_project": target.project,
        "target_locator": _approval_source_locator(target, vault),
        "target_digest": _approval_source_digest(target),
        "target_digest_scope": "canonical-record-v1",
        "target_excerpt": _excerpt(target),
        "reason": reason,
        "evidence_refs": evidence_refs,
    }


def _validate_action(raw):
    if not isinstance(raw, dict) or set(raw) != ACTION_FIELDS:
        raise RelationBatchPreconditionError(
            "approved relation action fields changed"
        )
    action = dict(raw)
    if action["relation"] not in MEMORY_RELATION_FIELDS:
        raise RelationBatchPreconditionError(
            f"unsupported approved relation: {action['relation']}"
        )
    if action["source_id"] == action["target_id"]:
        raise RelationBatchPreconditionError(
            "approved relation cannot target its source"
        )
    for prefix in ("source", "target"):
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}",
            str(action.get(f"{prefix}_id") or ""),
        ):
            raise RelationBatchPreconditionError(
                f"approved relation has invalid {prefix} ID"
            )
        for key in ("revision", "digest"):
            if not re.fullmatch(
                r"[0-9a-f]{64}",
                str(action.get(f"{prefix}_{key}") or ""),
            ):
                raise RelationBatchPreconditionError(
                    f"approved relation has invalid {prefix} {key}"
                )
        if action.get(f"{prefix}_digest_scope") != "canonical-record-v1":
            raise RelationBatchPreconditionError(
                f"approved relation has invalid {prefix} digest scope"
            )
        for key in ("type", "locator", "excerpt"):
            if not str(action.get(f"{prefix}_{key}") or "").strip():
                raise RelationBatchPreconditionError(
                    f"approved relation has empty {prefix} {key}"
                )
    if not str(action.get("reason") or "").strip():
        raise RelationBatchPreconditionError("approved relation has no reason")
    refs = action.get("evidence_refs")
    if not isinstance(refs, list) or not refs or not all(
        isinstance(item, str) and item.strip() for item in refs
    ):
        raise RelationBatchPreconditionError(
            "approved relation has invalid evidence refs"
        )
    return action


def _exact_live_record(action, prefix, records, vault):
    memory_id = action[f"{prefix}_id"]
    location = _one_record(records, memory_id)
    expected = {
        "revision": location.revision,
        "type": location.memory_type,
        "project": location.project,
        "locator": _approval_source_locator(location, vault),
        "digest": _approval_source_digest(location),
        "digest_scope": "canonical-record-v1",
        "excerpt": _excerpt(location),
    }
    for key, value in expected.items():
        if action.get(f"{prefix}_{key}") != value:
            raise RelationBatchPreconditionError(
                f"{prefix} formal memory changed after plan generation: {memory_id}"
            )
    if location.status != "active":
        raise RelationBatchPreconditionError(
            f"{prefix} formal memory is not active: {memory_id}"
        )
    return location


def _render_source_updates(cfg, selected, records):
    vault = _vault(cfg)
    grouped = {}
    for source_id, actions in selected.items():
        source = _one_record(records, source_id)
        grouped.setdefault(source.path, []).append((source, actions))
    rendered = {}
    revisions = {}
    for path, updates in grouped.items():
        content = secure_read_bytes(
            path,
            MAX_PLAN_BYTES * 4,
            root=vault,
        ).decode("utf-8")
        storage = {source.storage for source, _actions in updates}
        if storage == {"aggregate"}:
            output, updated = _render_aggregate(content, updates, vault)
        elif storage == {"markdown"}:
            output, updated = _render_markdown(content, updates)
        else:
            raise RelationBatchPreconditionError(
                "relation batch cannot update a mixed formal store"
            )
        rendered[path] = output.encode("utf-8")
        revisions.update(updated)
    return rendered, revisions


def _render_aggregate(content, updates, vault):
    frontmatter_text, body = split_frontmatter_text(content)
    if frontmatter_text is None or body is None:
        raise RelationBatchPreconditionError(
            "formal aggregate lost frontmatter"
        )
    frontmatter = yaml.safe_load(frontmatter_text) or {}
    revisions = {}
    for location, actions in updates:
        values = frontmatter.get(location.aggregate_key)
        matches = [
            index
            for index, item in enumerate(values or [])
            if isinstance(item, dict) and item.get("id") == location.memory_id
        ]
        if len(matches) != 1:
            raise RelationBatchPreconditionError(
                f"formal aggregate source changed: {location.memory_id}"
            )
        index = matches[0]
        raw = dict(values[index])
        _apply_relation_actions(raw, actions, location.memory_id)
        normalized = normalize_formal_record(
            raw,
            memory_type=location.memory_type,
            default_project=location.project,
            source_ref=(
                "note:"
                + os.path.relpath(location.path, vault)
                .replace(os.sep, "/")
                .removesuffix(".md")
            ),
            source_record_key=f"{location.aggregate_key}:{index}",
        )
        raw["revision"] = normalized["revision"]
        values[index] = raw
        revisions[location.memory_id] = normalized["revision"]
    return _render_frontmatter(frontmatter, body), revisions


def _render_markdown(content, updates):
    output = content
    revisions = {}
    for location, actions in sorted(
        updates,
        key=lambda item: item[0].section_start,
        reverse=True,
    ):
        segment = output[location.section_start:location.section_end]
        if f"- id: `{location.memory_id}`" not in segment:
            raise RelationBatchPreconditionError(
                f"formal Markdown source changed: {location.memory_id}"
            )
        record = dict(location.record)
        _apply_relation_actions(record, actions, location.memory_id)
        revision = memory_revision(record)
        for relation in MEMORY_RELATION_FIELDS:
            segment = _set_markdown_list_field(
                segment,
                relation,
                record.get(relation) or [],
            )
        segment = _set_markdown_scalar_field(
            segment,
            "revision",
            revision,
        )
        output = (
            output[:location.section_start]
            + segment
            + output[location.section_end:]
        )
        revisions[location.memory_id] = revision
    return output, revisions


def _apply_relation_actions(record, actions, memory_id):
    for action in actions:
        relation = action["relation"]
        targets = list(record.get(relation) or [])
        targets.append(action["target_id"])
        record[relation] = normalize_memory_relations(
            targets,
            memory_id=memory_id,
            field=relation,
        )


def _set_markdown_list_field(segment, key, values):
    pattern = re.compile(rf"(?m)^-\s*{re.escape(key)}:\s*.*(?:\n|$)")
    if not values:
        return pattern.sub("", segment)
    line = (
        f"- {key}: "
        + ", ".join(f"`{item}`" for item in values)
        + "\n"
    )
    if pattern.search(segment):
        return pattern.sub(line, segment, count=1)
    status = re.search(r"(?m)^-\s*status:\s*.*\n", segment)
    if not status:
        raise RelationBatchPreconditionError(
            "formal Markdown section has no status field"
        )
    return segment[:status.end()] + line + segment[status.end():]


def _set_markdown_scalar_field(segment, key, value):
    pattern = re.compile(rf"(?m)^-\s*{re.escape(key)}:\s*.*(?:\n|$)")
    line = f"- {key}: `{value}`\n"
    if not pattern.search(segment):
        raise RelationBatchPreconditionError(
            f"formal Markdown section has no {key} field"
        )
    return pattern.sub(line, segment, count=1)


def _render_frontmatter(frontmatter, body):
    return (
        "---\n"
        + yaml.dump(
            frontmatter,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        + "---\n"
        + body
    )


def _verify_applied(cfg, prepared):
    records = _records_by_id(find_records(cfg))
    for action in prepared["actions"]:
        source = _one_record(records, action["source_id"])
        if source.revision != prepared["resulting_revisions"][source.memory_id]:
            raise RelationBatchPreconditionError(
                f"resulting revision mismatch: {source.memory_id}"
            )
        if action["target_id"] not in (
            source.record.get(action["relation"]) or []
        ):
            raise RelationBatchPreconditionError(
                f"semantic relation missing after apply: {source.memory_id}"
            )


def _render_plan(actions, generated_at, canonical_sha256):
    frontmatter = {
        "title": "旧正式记忆语义关系审批计划",
        "summary_type": PLAN_SUMMARY_TYPE,
        "generated_by": PLAN_GENERATOR,
        "schema_version": PLAN_SCHEMA_VERSION,
        "generated_at": generated_at,
        "read_only": True,
        "approval_status": "pending",
        "action_count": len(actions),
        "canonical_sha256": canonical_sha256,
        "actions": actions,
    }
    lines = [
        "# 旧正式记忆语义关系审批计划",
        "",
        "该文件只冻结待审批关系，不会自动修改正式记忆。",
        "",
        f"Canonical SHA256: `{canonical_sha256}`",
        "",
    ]
    for index, action in enumerate(actions, 1):
        lines.extend(
            [
                f"## {index}. {action['source_id']} {action['relation']} {action['target_id']}",
                "",
                f"- Source: `{action['source_id']}` @ `{action['source_revision']}`",
                f"- Source Locator: `{action['source_locator']}`",
                f"- Source Digest: `{action['source_digest']}`",
                f"- Source Evidence: {action['source_excerpt']}",
                f"- Relation: `{action['relation']}`",
                f"- Target: `{action['target_id']}` @ `{action['target_revision']}`",
                f"- Target Locator: `{action['target_locator']}`",
                f"- Target Digest: `{action['target_digest']}`",
                f"- Target Evidence: {action['target_excerpt']}",
                f"- Reason: {action['reason']}",
                "- Evidence Refs: "
                + ", ".join(f"`{item}`" for item in action["evidence_refs"]),
                "",
            ]
        )
    return (
        "---\n"
        + yaml.dump(
            frontmatter,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        + "---\n\n"
        + "\n".join(lines).rstrip()
        + "\n"
    )


def _render_applied_plan(frontmatter, body, canonical_sha256, applied_at):
    updated = dict(frontmatter)
    updated["approval_status"] = "applied"
    updated["approved_canonical_sha256"] = canonical_sha256
    updated["applied_at"] = applied_at
    updated_body = str(body).replace(
        "该文件只冻结待审批关系，不会自动修改正式记忆。",
        "该计划已按精确 Canonical SHA256 执行，保留用于审计。",
        1,
    )
    return _render_frontmatter(updated, updated_body)


def _canonical_sha256(actions):
    payload = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "actions": actions,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _records_by_id(locations):
    records = {}
    for location in locations:
        records.setdefault(location.memory_id, []).append(location)
    return records


def _one_record(records, memory_id):
    matches = records.get(str(memory_id or "").strip()) or []
    if not matches:
        raise RelationBatchPreconditionError(
            f"formal memory not found: {memory_id}"
        )
    if len(matches) != 1:
        raise RelationBatchPreconditionError(
            f"formal memory ID is not unique: {memory_id}"
        )
    return matches[0]


def _excerpt(location):
    return _one_line(f"{location.title} | {location.summary}")[:600]


def _one_line(value):
    return " ".join(str(value or "").split())


def _aware_now(now):
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("relation plan timestamp must include a timezone")
    return current


def _vault(cfg):
    path = os.path.abspath(
        os.path.expanduser(str((cfg or {}).get("vault_path") or ""))
    )
    if not path or path == os.path.sep:
        raise ValueError("vault_path is required")
    return path


def _lock_path(vault):
    return safe_vault_path(vault, "04-Feedback", "_logs", "harvester.lock")


def _preview(prepared):
    return {
        "plan_path": prepared["plan_path"],
        "canonical_sha256": prepared["canonical_sha256"],
        "action_count": len(prepared["actions"]),
        "source_count": len(prepared["resulting_revisions"]),
        "applied": False,
    }


def _default_rebuilders():
    def rebuild(cfg):
        from session_harvester import rebuild_memory_index

        rebuild_memory_index(cfg, repair_generated=False)

    return [rebuild]


def _create_rollback_snapshots(cfg, prepared):
    vault = _vault(cfg)
    paths = [
        *prepared["rendered_sources"],
        prepared["plan_path"],
        *(
            safe_vault_path(vault, relative)
            for relative in DERIVED_VAULT_PATHS
        ),
    ]
    recall_path = configured_recall_index_path(cfg)
    paths.extend([recall_path, graph_path_for_index(recall_path)])
    memory_index = cfg.get("memory_index_path") or safe_vault_path(
        vault,
        "00-Inbox/Agent Memory Index.md",
    )
    paths.append(safe_vault_path(vault, memory_index))

    snapshots = []
    for path in dict.fromkeys(paths):
        if os.path.lexists(path) and not stat.S_ISREG(os.lstat(path).st_mode):
            raise RelationBatchPreconditionError(
                f"rollback target must be a regular file or absent: {path}"
            )
        data, mode = _snapshot_file("vault", path, vault)
        snapshots.append(
            {
                "kind": "vault",
                "path": os.path.relpath(path, vault),
                "existed": data is not None,
                "mode": mode,
                "data": data,
            }
        )
    return snapshots


def _verify_restored_snapshots(vault, snapshots):
    mismatches = []
    for item in snapshots:
        path = safe_vault_path(vault, item["path"])
        data, mode = _snapshot_file("vault", path, vault)
        if item["existed"]:
            if data != item["data"] or mode != item["mode"]:
                mismatches.append(path)
        elif data is not None:
            mismatches.append(path)
    if mismatches:
        raise RuntimeError(
            "relation rollback verification failed for: "
            + ", ".join(mismatches[:10])
        )


def _build_parser():
    parser = argparse.ArgumentParser(
        description="Generate or apply exact formal-memory relation plans."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan")
    plan.add_argument("--proposals-json", required=True)
    plan.add_argument("--output", required=True)

    for name in ("preview", "apply"):
        command = subparsers.add_parser(name)
        command.add_argument("--plan", required=True)
        command.add_argument("--expected-sha256", required=True)
        if name == "apply":
            command.add_argument("--apply", action="store_true")
    return parser


def main(argv=None):
    args = _build_parser().parse_args(argv)
    cfg = load_config()
    if args.command == "plan":
        with open(args.proposals_json, "r", encoding="utf-8") as handle:
            proposals = json.load(handle)
        result = write_relation_plan(cfg, proposals, args.output)
    elif args.command == "preview":
        result = preview_relation_batch(
            cfg,
            args.plan,
            args.expected_sha256,
        )
    else:
        if not args.apply:
            raise RelationBatchPreconditionError(
                "apply command requires --apply"
            )
        result = apply_relation_batch(
            cfg,
            args.plan,
            args.expected_sha256,
            apply=True,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RelationBatchError) as exc:
        print(f"memory relation batch failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
