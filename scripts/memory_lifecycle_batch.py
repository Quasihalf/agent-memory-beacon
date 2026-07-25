#!/usr/bin/env python3
"""Apply one exact approved old-memory lifecycle plan as a batch."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

import yaml

from config import load_config
from knowledge_index import configured_recall_index_path
from memory_lifecycle import (
    DERIVED_VAULT_PATHS,
    MAX_FORMAL_FILE_BYTES,
    LifecyclePlan,
    _apply_record_transition,
    _default_rebuilders,
    _restore_snapshots,
    _set_markdown_field,
    _snapshot_file,
    find_records,
    plan_transition,
)
from memory_quality_audit import (
    MAX_PROPOSAL_BYTES,
    OLD_MEMORY_PLAN_SCHEMA_VERSION,
    _approval_source_digest,
    _approval_source_locator,
    _render_old_memory_lifecycle_plan,
    _unapproved_active_alias_owners,
)
from memory_schema import (
    RUNTIME_SCHEMA_VERSION,
    is_valid_memory_id,
    memory_revision,
    normalize_formal_record,
    suppress_unmet_dependencies,
)
from safety import (
    durable_atomic_write,
    ensure_directory_tree,
    exclusive_file_lock,
    safe_vault_path,
    secure_list_directory,
    secure_read_bytes,
    split_frontmatter_text,
)


MAX_PLAN_BYTES = 16 * 1024 * 1024
PLAN_SUMMARY_TYPE = "old-memory-lifecycle-approval-plan"
SUPPORTED_BATCH_ACTIONS = frozenset({"retract", "supersede"})
ACTION_FIELDS = frozenset(
    {
        "action",
        "memory_id",
        "expected_revision",
        "type",
        "project",
        "scope",
        "date",
        "title",
        "summary",
        "source_path",
        "source_locator",
        "source_digest",
        "source_digest_scope",
        "replacement_id",
        "replacement_revision",
        "replacement_source_locator",
        "replacement_source_digest",
        "replacement_source_digest_scope",
        "reason",
        "reason_codes",
        "evidence_refs",
    }
)


class BatchLifecycleError(RuntimeError):
    pass


class BatchLifecyclePreconditionError(BatchLifecycleError):
    pass


@dataclass(frozen=True)
class ApprovedLifecyclePlan:
    path: str
    canonical_sha256: str
    content: bytes
    generated_at: str
    cutoff_exclusive: str
    actions: tuple[dict, ...]
    frontmatter: dict
    body: str


@dataclass(frozen=True)
class ProposalUpdate:
    frontmatter: dict
    body: str
    timestamp_key: str


@dataclass(frozen=True)
class PreparedLifecycleBatch:
    plan: ApprovedLifecyclePlan
    lifecycle_plans: tuple[LifecyclePlan, ...]
    rendered_sources: dict[str, bytes]
    resulting_revisions: dict[str, str]
    proposal_updates: dict[str, bytes]
    proposal_applied_count: int
    proposal_stale_count: int


def preview_lifecycle_batch(cfg, plan_path, expected_sha256):
    """Validate one approved lifecycle batch without writing any Vault data."""
    vault = _vault(cfg)
    lock_path = safe_vault_path(vault, "04-Feedback", "_logs", "harvester.lock")
    ensure_directory_tree(os.path.dirname(lock_path), vault)
    with exclusive_file_lock(lock_path, root=vault):
        prepared = _prepare_batch(cfg, plan_path, expected_sha256)
        return _preview_dict(prepared)


def apply_lifecycle_batch(
    cfg,
    plan_path,
    expected_sha256,
    *,
    apply=False,
    rebuilders=None,
):
    """Apply an exact approved plan with one lock, rebuild, and rollback set."""
    if not apply:
        return preview_lifecycle_batch(cfg, plan_path, expected_sha256)
    selected_rebuilders = (
        _default_rebuilders() if rebuilders is None else list(rebuilders)
    )
    if not selected_rebuilders:
        raise ValueError("at least one batch lifecycle rebuilder is required")
    if not all(callable(rebuilder) for rebuilder in selected_rebuilders):
        raise TypeError("every batch lifecycle rebuilder must be callable")

    vault = _vault(cfg)
    lock_path = safe_vault_path(vault, "04-Feedback", "_logs", "harvester.lock")
    ensure_directory_tree(os.path.dirname(lock_path), vault)
    with exclusive_file_lock(lock_path, root=vault):
        prepared = _prepare_batch(cfg, plan_path, expected_sha256)
        operation_id = _operation_id(prepared.plan)
        snapshots, manifest_path = _create_rollback_snapshot(
            cfg,
            prepared,
            operation_id,
        )
        try:
            for path in sorted(prepared.rendered_sources):
                durable_atomic_write(
                    path,
                    prepared.rendered_sources[path],
                    root=vault,
                )
            for rebuilder in selected_rebuilders:
                rebuilder(cfg)
            verification = _verify_applied_state(cfg, prepared)
            timestamp = datetime.now(timezone.utc).isoformat()
            for path in sorted(prepared.proposal_updates):
                content = _render_proposal_update(
                    prepared.proposal_updates[path],
                    operation_id,
                    timestamp,
                )
                durable_atomic_write(path, content, root=vault)
            durable_atomic_write(
                prepared.plan.path,
                _render_applied_plan(prepared.plan, operation_id, timestamp),
                root=vault,
            )
            _append_audit(
                cfg,
                operation_id,
                manifest_path,
                prepared,
                timestamp,
            )
            _mark_manifest(
                manifest_path,
                vault,
                "applied",
                verification=verification,
                resulting_revisions=prepared.resulting_revisions,
                proposal_applied_count=prepared.proposal_applied_count,
                proposal_stale_count=prepared.proposal_stale_count,
            )
        except Exception as apply_error:
            rollback_errors = []
            try:
                _restore_snapshots(vault, snapshots)
                _verify_restored_snapshots(vault, snapshots)
            except Exception as rollback_error:
                rollback_errors.append(str(rollback_error))
            status = "rollback_failed" if rollback_errors else "rolled_back"
            try:
                _mark_manifest(
                    manifest_path,
                    vault,
                    status,
                    error=str(apply_error),
                    rollback_errors=rollback_errors,
                )
            except Exception as manifest_error:
                rollback_errors.append(f"manifest update: {manifest_error}")
            if rollback_errors:
                raise BatchLifecycleError(
                    f"batch lifecycle failed: {apply_error}; rollback failed: "
                    + "; ".join(rollback_errors)
                ) from apply_error
            raise BatchLifecycleError(
                f"batch lifecycle apply failed: {apply_error}"
            ) from apply_error

        return {
            **_preview_dict(prepared),
            **verification,
            "applied": True,
            "operation_id": operation_id,
            "rollback_manifest": manifest_path,
        }


def _prepare_batch(cfg, plan_path, expected_sha256):
    plan = _load_approved_plan(cfg, plan_path, expected_sha256)
    locations = find_records(cfg)
    records_by_id = _records_by_id(locations)
    lifecycle_plans = _validate_live_actions(cfg, plan, records_by_id)
    _validate_batch_dependency_state(plan, locations)
    rendered_sources, revisions = _render_source_updates(
        cfg,
        plan,
        lifecycle_plans,
        records_by_id,
    )
    proposal_updates, applied_count, stale_count = _prepare_proposal_updates(
        cfg,
        plan,
    )
    return PreparedLifecycleBatch(
        plan=plan,
        lifecycle_plans=tuple(lifecycle_plans),
        rendered_sources=rendered_sources,
        resulting_revisions=revisions,
        proposal_updates=proposal_updates,
        proposal_applied_count=applied_count,
        proposal_stale_count=stale_count,
    )


def _load_approved_plan(cfg, plan_path, expected_sha256):
    vault = _vault(cfg)
    expected = str(expected_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise BatchLifecyclePreconditionError(
            "expected canonical SHA256 must be 64 lowercase hexadecimal characters"
        )
    raw_path = os.path.expanduser(str(plan_path or ""))
    if not raw_path:
        raise BatchLifecyclePreconditionError("approved plan path is required")
    path = safe_vault_path(vault, raw_path)
    try:
        current = os.lstat(path)
    except FileNotFoundError as exc:
        raise BatchLifecyclePreconditionError("approved plan does not exist") from exc
    if not stat.S_ISREG(current.st_mode):
        raise BatchLifecyclePreconditionError(
            "approved plan must be a regular Vault file"
        )
    data = secure_read_bytes(path, MAX_PLAN_BYTES, root=vault)
    if len(data) > MAX_PLAN_BYTES:
        raise BatchLifecyclePreconditionError("approved plan exceeds size limit")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BatchLifecyclePreconditionError(
            "approved plan is not UTF-8"
        ) from exc
    frontmatter_text, body = split_frontmatter_text(text)
    if frontmatter_text is None or body is None:
        raise BatchLifecyclePreconditionError("approved plan has no frontmatter")
    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise BatchLifecyclePreconditionError(
            "approved plan frontmatter is invalid"
        ) from exc
    if not isinstance(frontmatter, dict):
        raise BatchLifecyclePreconditionError(
            "approved plan frontmatter must be a mapping"
        )
    if frontmatter.get("summary_type") != PLAN_SUMMARY_TYPE:
        raise BatchLifecyclePreconditionError("unexpected approved plan type")
    if frontmatter.get("generated_by") != "memory_quality_audit.py":
        raise BatchLifecyclePreconditionError("unexpected approved plan generator")
    if frontmatter.get("read_only") is not True:
        raise BatchLifecyclePreconditionError("approved plan is not read-only")
    if frontmatter.get("approval_status") != "pending":
        raise BatchLifecyclePreconditionError(
            "approved plan is not pending or was already applied"
        )
    schema_version = str(frontmatter.get("schema_version") or "")
    if schema_version != OLD_MEMORY_PLAN_SCHEMA_VERSION:
        raise BatchLifecyclePreconditionError(
            "unsupported old-memory plan schema"
        )
    actions = frontmatter.get("actions")
    if not isinstance(actions, list) or not actions:
        raise BatchLifecyclePreconditionError("approved plan has no actions")
    if frontmatter.get("recommended_action_count") != len(actions):
        raise BatchLifecyclePreconditionError(
            "approved plan action count does not match its actions"
        )
    normalized_actions = tuple(_validate_action_shape(item) for item in actions)
    cutoff = str(frontmatter.get("cutoff_exclusive") or "")
    canonical_payload = {
        "schema_version": schema_version,
        "cutoff_exclusive": cutoff,
        "actions": list(normalized_actions),
    }
    actual = hashlib.sha256(
        json.dumps(
            canonical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    declared = str(frontmatter.get("canonical_sha256") or "").strip().lower()
    if declared != actual:
        raise BatchLifecyclePreconditionError(
            "approved plan canonical payload changed after generation"
        )
    if actual != expected:
        raise BatchLifecyclePreconditionError(
            f"approved plan canonical SHA256 mismatch: expected {expected}, "
            f"current {actual}"
        )
    _validate_rendered_plan_integrity(frontmatter, normalized_actions, data)
    return ApprovedLifecyclePlan(
        path=path,
        canonical_sha256=actual,
        content=data,
        generated_at=str(frontmatter.get("generated_at") or ""),
        cutoff_exclusive=cutoff,
        actions=normalized_actions,
        frontmatter=dict(frontmatter),
        body=body,
    )


def _validate_action_shape(raw):
    if not isinstance(raw, dict):
        raise BatchLifecyclePreconditionError(
            "every approved lifecycle action must be a mapping"
        )
    if set(raw) != ACTION_FIELDS:
        missing = sorted(ACTION_FIELDS - set(raw))
        extra = sorted(set(raw) - ACTION_FIELDS)
        raise BatchLifecyclePreconditionError(
            f"approved action fields changed: missing={missing} extra={extra}"
        )
    action = dict(raw)
    action_name = str(action.get("action") or "")
    memory_id = str(action.get("memory_id") or "")
    if action_name not in SUPPORTED_BATCH_ACTIONS:
        raise BatchLifecyclePreconditionError(
            f"unsupported batch lifecycle action: {action_name}"
        )
    if not is_valid_memory_id(memory_id):
        raise BatchLifecyclePreconditionError(
            f"invalid approved memory ID: {memory_id}"
        )
    for key in (
        "expected_revision",
        "source_digest",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(action.get(key) or "")):
            raise BatchLifecyclePreconditionError(
                f"approved action has an invalid {key}: {memory_id}"
            )
    if action.get("source_digest_scope") != "canonical-record-v1":
        raise BatchLifecyclePreconditionError(
            f"unsupported source digest scope for {memory_id}"
        )
    if not str(action.get("reason") or "").strip():
        raise BatchLifecyclePreconditionError(
            f"approved action has no reason: {memory_id}"
        )
    for key in ("reason_codes", "evidence_refs"):
        values = action.get(key)
        if not isinstance(values, list) or not all(
            isinstance(item, str) and item.strip() for item in values
        ):
            raise BatchLifecyclePreconditionError(
                f"approved action has invalid {key}: {memory_id}"
            )
    replacement_id = str(action.get("replacement_id") or "")
    if action_name == "supersede":
        if not is_valid_memory_id(replacement_id) or replacement_id == memory_id:
            raise BatchLifecyclePreconditionError(
                f"invalid approved replacement ID for {memory_id}"
            )
        for key in ("replacement_revision", "replacement_source_digest"):
            if not re.fullmatch(r"[0-9a-f]{64}", str(action.get(key) or "")):
                raise BatchLifecyclePreconditionError(
                    f"approved action has an invalid {key}: {memory_id}"
                )
        if action.get("replacement_source_digest_scope") != "canonical-record-v1":
            raise BatchLifecyclePreconditionError(
                f"unsupported replacement digest scope for {memory_id}"
            )
        if not str(action.get("replacement_source_locator") or "").strip():
            raise BatchLifecyclePreconditionError(
                f"approved action has no replacement locator: {memory_id}"
            )
    elif any(
        str(action.get(key) or "")
        for key in (
            "replacement_id",
            "replacement_revision",
            "replacement_source_locator",
            "replacement_source_digest",
            "replacement_source_digest_scope",
        )
    ):
        raise BatchLifecyclePreconditionError(
            f"retract action has replacement fields: {memory_id}"
        )
    return action


def _validate_rendered_plan_integrity(frontmatter, actions, data):
    snapshot = {
        key: value
        for key, value in frontmatter.items()
        if key not in {"title", "summary_type", "generated_by", "actions"}
    }
    snapshot["recommended_actions"] = list(actions)
    rendered = _render_old_memory_lifecycle_plan(snapshot).encode("utf-8")
    if rendered != data:
        raise BatchLifecyclePreconditionError(
            "approved plan body or frontmatter content drifted from its canonical data"
        )


def _records_by_id(locations):
    records = {}
    for location in locations:
        records.setdefault(location.memory_id, []).append(location)
    duplicates = sorted(
        memory_id for memory_id, values in records.items() if len(values) != 1
    )
    if duplicates:
        raise BatchLifecyclePreconditionError(
            "formal memory IDs are not unique: " + ", ".join(duplicates[:10])
        )
    return records


def _validate_live_actions(cfg, plan, records_by_id):
    target_ids = [str(item["memory_id"]) for item in plan.actions]
    if len(target_ids) != len(set(target_ids)):
        raise BatchLifecyclePreconditionError(
            "approved lifecycle plan repeats a target ID"
        )
    replacement_ids = {
        str(item.get("replacement_id") or "")
        for item in plan.actions
        if str(item.get("replacement_id") or "")
    }
    overlap = sorted(set(target_ids) & replacement_ids)
    if overlap:
        raise BatchLifecyclePreconditionError(
            "batch target/replacement overlap is forbidden: "
            + ", ".join(overlap[:10])
        )

    for action in plan.actions:
        alias_owners = _unapproved_active_alias_owners(action, records_by_id)
        if not alias_owners:
            continue
        qualifier = "unapproved " if action["action"] == "supersede" else ""
        raise BatchLifecyclePreconditionError(
            f"{qualifier}active alias owner for {action['action']} target "
            f"{action['memory_id']}: {', '.join(alias_owners[:10])}"
        )

    lifecycle_plans = []
    for action in plan.actions:
        memory_id = action["memory_id"]
        location = _one_snapshot_record(records_by_id, memory_id)
        _compare_action_to_location(cfg, action, location, replacement=False)
        replacement_id = action["replacement_id"]
        if replacement_id:
            replacement = _one_snapshot_record(records_by_id, replacement_id)
            _compare_action_to_location(cfg, action, replacement, replacement=True)
        try:
            lifecycle_plan = plan_transition(
                cfg,
                action["action"],
                memory_id,
                action["reason"],
                replacement_id=replacement_id,
                expected_revision=action["expected_revision"],
                _record_snapshot=records_by_id,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            raise BatchLifecyclePreconditionError(
                f"approved transition is no longer valid for {memory_id}: {exc}"
            ) from exc
        lifecycle_plans.append(lifecycle_plan)
    return lifecycle_plans


def _one_snapshot_record(records_by_id, memory_id):
    values = records_by_id.get(memory_id) or []
    if not values:
        raise BatchLifecyclePreconditionError(
            f"formal memory not found: {memory_id}"
        )
    if len(values) != 1:
        raise BatchLifecyclePreconditionError(
            f"formal memory ID is not unique: {memory_id}"
        )
    return values[0]


def _compare_action_to_location(cfg, action, location, *, replacement):
    vault = _vault(cfg)
    prefix = "replacement_" if replacement else ""
    expected_id = action[prefix + "id"] if replacement else action["memory_id"]
    expected_revision = action[prefix + "revision"] if replacement else action[
        "expected_revision"
    ]
    expected_locator = action[prefix + "source_locator"]
    expected_digest = action[prefix + "source_digest"]
    if location.memory_id != expected_id or location.revision != expected_revision:
        raise BatchLifecyclePreconditionError(
            f"approved revision changed for {expected_id}"
        )
    if _approval_source_locator(location, vault) != expected_locator:
        raise BatchLifecyclePreconditionError(
            f"approved source locator changed for {expected_id}"
        )
    if _approval_source_digest(location) != expected_digest:
        raise BatchLifecyclePreconditionError(
            f"approved canonical record digest changed for {expected_id}"
        )
    if not replacement:
        expected_path = str(action["source_path"] or "").replace("\\", "/")
        actual_path = os.path.relpath(location.path, vault).replace(os.sep, "/")
        expected_fields = {
            "type": location.memory_type,
            "project": location.project,
            "scope": location.scope,
            "date": str(location.record.get("date") or ""),
            "title": location.title,
            "summary": location.summary,
        }
        if expected_path != actual_path:
            raise BatchLifecyclePreconditionError(
                f"approved source path changed for {expected_id}"
            )
        for key, actual in expected_fields.items():
            if action.get(key) != actual:
                raise BatchLifecyclePreconditionError(
                    f"approved {key} changed for {expected_id}"
                )


def _validate_batch_dependency_state(plan, locations):
    target_ids = {str(item["memory_id"]) for item in plan.actions}
    active_records = [
        item.record
        for item in locations
        if item.status == "active" and item.memory_id not in target_ids
    ]
    eligible, _suppressed = suppress_unmet_dependencies(active_records)
    eligible_ids = {str(item.get("id") or "") for item in eligible}
    unavailable = sorted(
        str(item["replacement_id"])
        for item in plan.actions
        if item["action"] == "supersede"
        and str(item["replacement_id"]) not in eligible_ids
    )
    if unavailable:
        raise BatchLifecyclePreconditionError(
            "replacement memory would be dependency-suppressed after the entire "
            "batch: " + ", ".join(unavailable[:10])
        )


def _render_source_updates(cfg, plan, lifecycle_plans, records_by_id):
    vault = _vault(cfg)
    grouped = {}
    by_memory_id = {item.memory_id: item for item in lifecycle_plans}
    for action in plan.actions:
        location = _one_snapshot_record(records_by_id, action["memory_id"])
        grouped.setdefault(location.path, []).append(
            (location, by_memory_id[action["memory_id"]])
        )

    rendered = {}
    revisions = {}
    for path, updates in grouped.items():
        content = _read_text(path, vault)
        storage_types = {location.storage for location, _plan in updates}
        if storage_types == {"aggregate"}:
            output, source_revisions = _render_aggregate_updates(
                content,
                updates,
                vault,
            )
        elif storage_types == {"markdown"}:
            output, source_revisions = _render_markdown_updates(content, updates)
        else:
            raise BatchLifecyclePreconditionError(
                f"unsupported mixed formal store at {path}"
            )
        rendered[path] = output.encode("utf-8")
        revisions.update(source_revisions)
    return rendered, revisions


def _render_aggregate_updates(content, updates, vault):
    frontmatter_text, body = split_frontmatter_text(content)
    if frontmatter_text is None or body is None:
        raise BatchLifecyclePreconditionError("formal aggregate lost frontmatter")
    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise BatchLifecyclePreconditionError(
            "formal aggregate frontmatter became invalid"
        ) from exc
    revisions = {}
    for location, lifecycle_plan in updates:
        values = frontmatter.get(location.aggregate_key)
        if not isinstance(values, list):
            raise BatchLifecyclePreconditionError(
                "formal aggregate changed shape before batch render"
            )
        matches = [
            index
            for index, item in enumerate(values)
            if isinstance(item, dict) and item.get("id") == location.memory_id
        ]
        if len(matches) != 1:
            raise BatchLifecyclePreconditionError(
                f"formal memory ID changed before render: {location.memory_id}"
            )
        index = matches[0]
        raw = dict(values[index])
        _apply_record_transition(raw, lifecycle_plan)
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


def _render_markdown_updates(content, updates):
    revisions = {}
    output = content
    ordered = sorted(
        updates,
        key=lambda item: item[0].section_start,
        reverse=True,
    )
    for location, lifecycle_plan in ordered:
        segment = output[location.section_start:location.section_end]
        if f"- id: `{location.memory_id}`" not in segment:
            raise BatchLifecyclePreconditionError(
                f"formal Markdown section changed: {location.memory_id}"
            )
        record = dict(location.record)
        _apply_record_transition(record, lifecycle_plan)
        revision = memory_revision(record)
        segment = _set_markdown_field(
            segment,
            "status",
            record["status"],
            code=True,
        )
        segment = _set_markdown_field(
            segment,
            "superseded_by",
            record.get("superseded_by", ""),
            code=True,
        )
        segment = _set_markdown_field(
            segment,
            "retracted_reason",
            record.get("retracted_reason", ""),
        )
        segment = _set_markdown_field(
            segment,
            "expired_reason",
            record.get("expired_reason", ""),
        )
        segment = _set_markdown_field(
            segment,
            "expires_at",
            record.get("expires_at", ""),
            code=True,
        )
        segment = _set_markdown_field(segment, "revision", revision, code=True)
        output = (
            output[:location.section_start]
            + segment
            + output[location.section_end:]
        )
        revisions[location.memory_id] = revision
    return output, revisions


def _prepare_proposal_updates(cfg, plan):
    vault = _vault(cfg)
    lifecycle = cfg.get("memory_lifecycle") or {}
    directory = safe_vault_path(
        vault,
        lifecycle.get("proposal_dir", "04-Feedback/_lifecycle-proposals"),
    )
    try:
        current = os.lstat(directory)
    except FileNotFoundError:
        return {}, 0, 0
    if not stat.S_ISDIR(current.st_mode):
        raise BatchLifecyclePreconditionError(
            "lifecycle proposal directory is not a real directory"
        )
    _directories, filenames = secure_list_directory(directory, vault)
    actions_by_id = {item["memory_id"]: item for item in plan.actions}
    updates = {}
    applied_count = 0
    stale_count = 0
    for filename in filenames:
        if not filename.endswith(".md"):
            continue
        path = safe_vault_path(directory, filename)
        data = secure_read_bytes(path, MAX_PROPOSAL_BYTES, root=vault)
        if len(data) > MAX_PROPOSAL_BYTES:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        frontmatter_text, body = split_frontmatter_text(text)
        if frontmatter_text is None or body is None:
            continue
        try:
            frontmatter = yaml.safe_load(frontmatter_text) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(frontmatter, dict):
            continue
        if frontmatter.get("summary_type") != "lifecycle-proposal":
            continue
        memory_id = str(frontmatter.get("memory_id") or "")
        action = actions_by_id.get(memory_id)
        if action is None or frontmatter.get("status") not in {"pending", "stale"}:
            continue
        exact = _proposal_matches_action(frontmatter, action)
        if exact:
            frontmatter["status"] = "applied"
            frontmatter["approved_plan_sha256"] = plan.canonical_sha256
            frontmatter.pop("stale_at", None)
            frontmatter.pop("stale_reason", None)
            updated_body = str(body).replace(
                "# 待确认:",
                "# 已执行:",
                1,
            ).replace(
                "# 已过期提案:",
                "# 已执行:",
                1,
            ).replace(
                "该文件只是待确认提案，不会进入正式召回。",
                "该提案已通过精确审批计划执行，保留用于审计。",
                1,
            ).replace(
                "该提案已被最新质量审计标记为过期，不能作为当前批准依据。",
                "该提案已通过精确审批计划执行，保留用于审计。",
                1,
            )
            timestamp_key = "applied_at"
            applied_count += 1
        elif frontmatter.get("status") == "pending":
            frontmatter["status"] = "stale"
            frontmatter["stale_reason"] = (
                "superseded_by_applied_lifecycle_plan"
            )
            frontmatter["approved_plan_sha256"] = plan.canonical_sha256
            updated_body = str(body).replace(
                "# 待确认:",
                "# 已过期提案:",
                1,
            ).replace(
                "该文件只是待确认提案，不会进入正式召回。",
                "该提案未被本次精确审批采用，已停止等待确认。",
                1,
            )
            timestamp_key = "stale_at"
            stale_count += 1
        else:
            continue
        updates[path] = ProposalUpdate(
            frontmatter=frontmatter,
            body=updated_body,
            timestamp_key=timestamp_key,
        )
    return updates, applied_count, stale_count


def _proposal_matches_action(frontmatter, action):
    refs = frontmatter.get("evidence_refs") or []
    if not isinstance(refs, list):
        return False
    return (
        frontmatter.get("action") == action["action"]
        and frontmatter.get("memory_id") == action["memory_id"]
        and frontmatter.get("expected_revision") == action["expected_revision"]
        and frontmatter.get("reason") == action["reason"]
        and str(frontmatter.get("replacement_id") or "")
        == action["replacement_id"]
        and str(frontmatter.get("replacement_revision") or "")
        == action["replacement_revision"]
        and sorted(str(item) for item in refs)
        == sorted(str(item) for item in action["evidence_refs"])
    )


def _render_proposal_update(update, operation_id, timestamp):
    frontmatter = dict(update.frontmatter)
    frontmatter[update.timestamp_key] = timestamp
    frontmatter["applied_operation_id"] = operation_id
    return _render_frontmatter(frontmatter, update.body)


def _create_rollback_snapshot(cfg, prepared, operation_id):
    vault = _vault(cfg)
    lifecycle = cfg.get("memory_lifecycle") or {}
    rollback_root = safe_vault_path(
        vault,
        lifecycle.get("rollback_dir", "04-Feedback/_rollback/lifecycle"),
    )
    operation_root = safe_vault_path(rollback_root, operation_id)
    files_root = safe_vault_path(operation_root, "files")
    ensure_directory_tree(files_root, vault)
    durable_atomic_write(
        safe_vault_path(operation_root, "approved-plan.md"),
        prepared.plan.content,
        mode=0o600,
        root=vault,
    )

    targets = [("vault", path) for path in prepared.rendered_sources]
    targets.extend(("vault", path) for path in prepared.proposal_updates)
    targets.append(("vault", prepared.plan.path))
    targets.extend(
        ("vault", safe_vault_path(vault, relative))
        for relative in DERIVED_VAULT_PATHS
    )
    targets.append(("vault", configured_recall_index_path(cfg)))
    memory_index = cfg.get("memory_index_path") or safe_vault_path(
        vault,
        "00-Inbox/Agent Memory Index.md",
    )
    targets.append(("vault", safe_vault_path(vault, memory_index)))
    targets.append(("vault", _audit_path(cfg)))
    profile_dir = str(cfg.get("codex_profile_path") or "").strip()
    if profile_dir:
        targets.append(
            _classify_snapshot_path(
                vault,
                os.path.abspath(
                    os.path.expanduser(os.path.join(profile_dir, "AGENTS.shared.md"))
                ),
            )
        )
    for target in cfg.get("context_targets") or []:
        targets.append(
            _classify_snapshot_path(
                vault,
                os.path.abspath(os.path.expanduser(str(target))),
            )
        )
    unique = []
    seen = set()
    for item in targets:
        if item not in seen:
            seen.add(item)
            unique.append(item)

    snapshots = []
    manifest_targets = []
    for index, (kind, path) in enumerate(unique):
        if os.path.lexists(path) and not stat.S_ISREG(os.lstat(path).st_mode):
            raise BatchLifecyclePreconditionError(
                f"rollback target must be a regular file or absent: {path}"
            )
        data, mode = _snapshot_file(kind, path, vault)
        backup = ""
        if data is not None:
            backup = f"files/{index:04d}.bin"
            durable_atomic_write(
                safe_vault_path(operation_root, backup),
                data,
                mode=0o600,
                root=vault,
            )
        item = {
            "kind": kind,
            "path": _relative(path, vault) if kind == "vault" else path,
            "existed": data is not None,
            "mode": mode,
            "sha256": hashlib.sha256(data).hexdigest() if data is not None else "",
            "backup": backup,
        }
        snapshots.append({**item, "data": data})
        manifest_targets.append(item)
    manifest = {
        "schema_version": 1,
        "operation_type": "formal-memory-lifecycle-batch",
        "operation_id": operation_id,
        "status": "prepared",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "authority": "user",
        "approved_plan": _relative(prepared.plan.path, vault),
        "approved_plan_sha256": prepared.plan.canonical_sha256,
        "approved_action_ids": [
            item["memory_id"] for item in prepared.plan.actions
        ],
        "approved_action_count": len(prepared.plan.actions),
        "targets": manifest_targets,
    }
    manifest_path = safe_vault_path(operation_root, "manifest.json")
    durable_atomic_write(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        root=vault,
    )
    return snapshots, manifest_path


def _verify_restored_snapshots(vault, snapshots):
    mismatches = []
    for item in snapshots:
        path = (
            safe_vault_path(vault, item["path"])
            if item["kind"] == "vault"
            else item["path"]
        )
        data, mode = _snapshot_file(item["kind"], path, vault)
        if item["existed"]:
            if data != item["data"] or mode != item["mode"]:
                mismatches.append(path)
        elif data is not None:
            mismatches.append(path)
    if mismatches:
        raise RuntimeError(
            "rollback verification failed for: " + ", ".join(mismatches[:10])
        )


def _verify_applied_state(cfg, prepared):
    locations = find_records(cfg)
    by_id = {}
    for location in locations:
        if location.memory_id in by_id:
            raise RuntimeError(
                f"formal memory ID became non-unique: {location.memory_id}"
            )
        by_id[location.memory_id] = location
    for lifecycle_plan in prepared.lifecycle_plans:
        location = by_id.get(lifecycle_plan.memory_id)
        if location is None:
            raise RuntimeError(
                f"formal memory disappeared after batch: {lifecycle_plan.memory_id}"
            )
        if location.status != lifecycle_plan.after_status:
            raise RuntimeError(
                f"formal status postcondition failed for {lifecycle_plan.memory_id}"
            )
        if location.revision != prepared.resulting_revisions[lifecycle_plan.memory_id]:
            raise RuntimeError(
                f"formal revision postcondition failed for {lifecycle_plan.memory_id}"
            )

    recall_path = configured_recall_index_path(cfg)
    try:
        payload = json.loads(_read_text(recall_path, _vault(cfg)))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("runtime recall index is invalid after batch") from exc
    identity_counts = {}
    primary_counts = {}
    alias_owners = {}
    for item in payload.get("units") or []:
        if not isinstance(item, dict):
            continue
        primary_id = str(item.get("id") or "").strip()
        if primary_id:
            primary_counts[primary_id] = primary_counts.get(primary_id, 0) + 1
        for value in [primary_id, *(item.get("aliases") or [])]:
            identity = str(value or "").strip()
            if identity:
                identity_counts[identity] = identity_counts.get(identity, 0) + 1
        for value in item.get("aliases") or []:
            alias = str(value or "").strip()
            if alias and primary_id:
                alias_owners.setdefault(alias, set()).add(primary_id)
    primary_residual = sorted(
        item.memory_id
        for item in prepared.lifecycle_plans
        if primary_counts.get(item.memory_id, 0)
    )
    if primary_residual:
        raise RuntimeError(
            "inactive batch targets remained primary in recall: "
            + ", ".join(primary_residual[:10])
        )
    alias_violations = []
    for item in prepared.lifecycle_plans:
        owners = set(alias_owners.get(item.memory_id) or set())
        allowed = {item.replacement_id} if item.replacement_id else set()
        unexpected = sorted(owners - allowed)
        if unexpected:
            alias_violations.append(
                f"{item.memory_id}=>{','.join(unexpected)}"
            )
    if alias_violations:
        raise RuntimeError(
            "inactive batch targets have unapproved recall alias owners: "
            + "; ".join(alias_violations[:10])
        )
    replacement_ids = {
        item.replacement_id
        for item in prepared.lifecycle_plans
        if item.replacement_id
    }
    missing = sorted(
        memory_id
        for memory_id in replacement_ids
        if primary_counts.get(memory_id, 0) != 1
    )
    if missing:
        raise RuntimeError(
            "approved replacement is missing or ambiguous in recall: "
            + ", ".join(missing[:10])
        )
    return {
        "formal_record_count_after": len(locations),
        "recall_identity_count_after": len(identity_counts),
        "verified_action_count": len(prepared.lifecycle_plans),
    }


def _render_applied_plan(plan, operation_id, timestamp):
    frontmatter = dict(plan.frontmatter)
    frontmatter["approval_status"] = "applied"
    frontmatter["applied_at"] = timestamp
    frontmatter["applied_operation_id"] = operation_id
    body = plan.body.rstrip() + "\n\n" + "\n".join(
        [
            "## 执行结果",
            "",
            f"- 状态: `applied`",
            f"- Operation ID: `{operation_id}`",
            f"- Applied At: `{timestamp}`",
            f"- Canonical SHA256: `{plan.canonical_sha256}`",
        ]
    ) + "\n"
    return _render_frontmatter(frontmatter, body)


def _append_audit(
    cfg,
    operation_id,
    manifest_path,
    prepared,
    timestamp,
):
    vault = _vault(cfg)
    path = _audit_path(cfg)
    if os.path.isfile(path) and not os.path.islink(path):
        existing = _read_text(path, vault)
    else:
        existing = _render_frontmatter(
            {
                "title": "Memory Lifecycle Audit",
                "summary_type": "lifecycle-audit",
                "generated_by": "memory_lifecycle_batch.py",
                "schema_version": RUNTIME_SCHEMA_VERSION,
            },
            "# Memory Lifecycle Audit\n\n正式记忆状态变更的审计记录。\n",
        )
    manifest_href = os.path.relpath(
        manifest_path,
        os.path.dirname(path),
    ).replace(os.sep, "/")
    lines = [
        f"## {timestamp} lifecycle-batch {prepared.plan.canonical_sha256[:12]}",
        "",
        f"- operation_id: `{operation_id}`",
        "- action: `lifecycle-batch`",
        "- authority: `user`",
        f"- approved_plan_sha256: `{prepared.plan.canonical_sha256}`",
        f"- action_count: `{len(prepared.plan.actions)}`",
        f"- rollback_manifest: [manifest.json](<{manifest_href}>)",
        "",
        "### Lifecycle Changes",
        "",
        "| ID | Action | Result | Revision Precondition | Resulting Revision | Replacement | Source Digest | Source Locator |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for action in prepared.plan.actions:
        lines.append(
            "| `{id}` | `{action}` | `{result}` | `{before}` | `{after}` | "
            "`{replacement}` | `{digest}` | `{locator}` |".format(
                id=action["memory_id"],
                action=action["action"],
                result=(
                    "superseded" if action["action"] == "supersede" else "retracted"
                ),
                before=action["expected_revision"],
                after=prepared.resulting_revisions[action["memory_id"]],
                replacement=action["replacement_id"] or "-",
                digest=action["source_digest"],
                locator=action["source_locator"],
            )
        )
    ensure_directory_tree(os.path.dirname(path), vault)
    durable_atomic_write(
        path,
        existing.rstrip() + "\n\n" + "\n".join(lines).rstrip() + "\n",
        root=vault,
    )


def _mark_manifest(path, vault, status, **extra):
    payload = json.loads(_read_text(path, vault))
    payload["status"] = status
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    payload.update(extra)
    durable_atomic_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        root=vault,
    )


def _preview_dict(prepared):
    return {
        "applied": False,
        "plan": prepared.plan.path,
        "canonical_sha256": prepared.plan.canonical_sha256,
        "cutoff_exclusive": prepared.plan.cutoff_exclusive,
        "action_count": len(prepared.plan.actions),
        "affected_source_count": len(prepared.rendered_sources),
        "proposal_applied_count": prepared.proposal_applied_count,
        "proposal_stale_count": prepared.proposal_stale_count,
        "actions": [dict(item) for item in prepared.plan.actions],
    }


def _operation_id(plan):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"lifecycle-batch-{stamp}-{plan.canonical_sha256[:12]}"


def _audit_path(cfg):
    vault = _vault(cfg)
    raw = (cfg.get("memory_lifecycle") or {}).get(
        "audit_path",
        "05-Agent-Memory/lifecycle-audit.md",
    )
    return safe_vault_path(vault, raw)


def _classify_snapshot_path(vault, path):
    try:
        inside = os.path.commonpath([vault, path]) == vault
    except ValueError:
        inside = False
    return ("vault", safe_vault_path(vault, path)) if inside else ("external", path)


def _render_frontmatter(frontmatter, body):
    return (
        "---\n"
        + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
        + "---\n"
        + str(body or "")
    )


def _read_text(path, vault):
    data = secure_read_bytes(path, MAX_FORMAL_FILE_BYTES, root=vault)
    if len(data) > MAX_FORMAL_FILE_BYTES:
        raise ValueError(f"file exceeds size limit: {_relative(path, vault)}")
    return data.decode("utf-8")


def _relative(path, vault):
    return os.path.relpath(path, vault).replace(os.sep, "/")


def _vault(cfg):
    raw = str(cfg.get("vault_path") or "").strip()
    if not raw:
        raise ValueError("vault_path is required")
    vault = os.path.abspath(os.path.expanduser(raw))
    current = os.lstat(vault)
    if not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode):
        raise ValueError("vault_path must be a real directory")
    return vault


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Preview or apply an exact approved lifecycle batch"
    )
    parser.add_argument("--plan", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the approved batch; without this flag the command is read-only",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        cfg = load_config()
        result = apply_lifecycle_batch(
            cfg,
            args.plan,
            args.expected_sha256,
            apply=args.apply,
        )
    except (BatchLifecycleError, OSError, TypeError, ValueError) as exc:
        print(f"batch lifecycle failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        mode = "applied" if result.get("applied") else "preview"
        print(
            f"lifecycle batch {mode}: {result['action_count']} actions, "
            f"Canonical SHA256 {result['canonical_sha256']}"
        )
        if result.get("rollback_manifest"):
            print(f"rollback manifest: {result['rollback_manifest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
