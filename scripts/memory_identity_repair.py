#!/usr/bin/env python3
"""Guarded batch repair for approved formal-memory identity conflicts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import yaml

from config import load_config
from knowledge_index import configured_recall_index_path
from memory_lifecycle import (
    DERIVED_VAULT_PATHS,
    MAX_FORMAL_FILE_BYTES,
    _default_rebuilders,
    _restore_snapshots,
    _snapshot_file,
    find_records,
)
from memory_quality_audit import audit_formal_memories
from memory_schema import (
    FORMAL_MEMORY_STATUSES,
    RUNTIME_SCHEMA_VERSION,
    canonical_project,
    is_valid_memory_id,
    memory_revision,
    normalize_formal_record,
)
from safety import (
    durable_atomic_write,
    ensure_directory_tree,
    exclusive_file_lock,
    safe_vault_path,
    secure_read_bytes,
    split_frontmatter_text,
)


MAX_PLAN_BYTES = 16 * 1024 * 1024
PLAN_SUMMARY_TYPE = "memory-identity-conflict-plan"
PLAN_RECOMMENDATION_SCHEMA = "1.0"
SUPPORTED_ACTIONS = frozenset(
    {
        "rekey_then_supersede",
        "rekey_then_retract",
        "rekey_and_keep",
        "rekey_and_retain_inactive",
    }
)
SUPPORTED_OWNER_ACTIONS = frozenset(
    {
        "retain_id_and_keep",
        "retain_id_and_keep_inactive",
        "retain_id_then_retract",
    }
)


class IdentityRepairError(RuntimeError):
    pass


class IdentityRepairPreconditionError(IdentityRepairError):
    pass


@dataclass(frozen=True)
class ApprovedOwner:
    source: str
    source_locator: str
    source_digest: str
    revision: str
    status: str
    project: str
    action: str
    resulting_status: str
    target_project: str
    target_source: str
    relocate: bool
    source_refs_after_merge: tuple[str, ...]


@dataclass(frozen=True)
class ApprovedAction:
    source: str
    source_locator: str
    source_identity: str
    source_digest: str
    revision: str
    current_status: str
    action: str
    proposed_id: str
    replacement_id: str
    target_project: str
    target_source: str
    relocate: bool
    resulting_status: str
    preserve_source_refs: bool
    merge_source_refs_into_owner: bool
    reason: str


@dataclass(frozen=True)
class ApprovedConflict:
    memory_id: str
    fact_relation: str
    confidence: str
    recommendation_reason: str
    owner: ApprovedOwner
    actions: tuple[ApprovedAction, ...]


@dataclass(frozen=True)
class ApprovedPlan:
    path: str
    sha256: str
    content: bytes
    generated_at: str
    conflicts: tuple[ApprovedConflict, ...]


@dataclass
class _AggregateDocument:
    path: str
    relative_path: str
    digest: str
    frontmatter: dict
    body: str
    key: str


@dataclass(frozen=True)
class _PreparedRepair:
    plan: ApprovedPlan
    documents: dict[str, _AggregateDocument]
    rendered: dict[str, bytes]
    resulting_records: dict[str, dict]
    original_source_refs: dict[str, tuple[str, ...]]


def preview_identity_repair(cfg, plan_path, expected_sha256):
    """Validate an approved plan and the live Vault without changing either."""
    vault = _vault(cfg)
    lock_path = safe_vault_path(vault, "04-Feedback", "_logs", "harvester.lock")
    ensure_directory_tree(os.path.dirname(lock_path), vault)
    with exclusive_file_lock(lock_path, root=vault):
        prepared = _prepare_repair(cfg, plan_path, expected_sha256)
        return _preview_dict(prepared)


def apply_identity_repair(
    cfg,
    plan_path,
    expected_sha256,
    *,
    apply=False,
    rebuilders=None,
):
    """Apply one exact approved identity plan as a rollback-protected batch."""
    if not apply:
        return preview_identity_repair(cfg, plan_path, expected_sha256)
    selected_rebuilders = (
        _default_rebuilders() if rebuilders is None else list(rebuilders)
    )
    if not selected_rebuilders:
        raise ValueError("at least one identity repair rebuilder is required")
    if not all(callable(rebuilder) for rebuilder in selected_rebuilders):
        raise TypeError("every identity repair rebuilder must be callable")

    vault = _vault(cfg)
    lock_path = safe_vault_path(vault, "04-Feedback", "_logs", "harvester.lock")
    ensure_directory_tree(os.path.dirname(lock_path), vault)
    with exclusive_file_lock(lock_path, root=vault):
        prepared = _prepare_repair(cfg, plan_path, expected_sha256)
        operation_id = _operation_id(prepared.plan)
        snapshots, manifest_path = _create_rollback_snapshot(
            cfg,
            prepared,
            operation_id,
        )
        try:
            for path in sorted(prepared.rendered):
                durable_atomic_write(
                    path,
                    prepared.rendered[path],
                    root=vault,
                )
            for rebuilder in selected_rebuilders:
                rebuilder(cfg)
            verification = _verify_applied_state(cfg, prepared)
            _append_audit(cfg, operation_id, manifest_path, prepared)
            _mark_manifest(
                manifest_path,
                vault,
                "applied",
                verification=verification,
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
                raise IdentityRepairError(
                    f"identity repair failed: {apply_error}; rollback failed: "
                    + "; ".join(rollback_errors)
                ) from apply_error
            raise IdentityRepairError(
                f"identity repair apply failed: {apply_error}"
            ) from apply_error

        return {
            **_preview_dict(prepared),
            **verification,
            "applied": True,
            "operation_id": operation_id,
            "rollback_manifest": manifest_path,
        }


def _prepare_repair(cfg, plan_path, expected_sha256):
    plan = _load_approved_plan(cfg, plan_path, expected_sha256)
    report = audit_formal_memories(cfg)
    _validate_current_snapshot(plan, report, cfg)
    documents, rendered, resulting_records, source_refs = _build_mutations(
        cfg,
        plan,
    )
    return _PreparedRepair(
        plan=plan,
        documents=documents,
        rendered=rendered,
        resulting_records=resulting_records,
        original_source_refs=source_refs,
    )


def _load_approved_plan(cfg, plan_path, expected_sha256):
    vault = _vault(cfg)
    expected = str(expected_sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise IdentityRepairPreconditionError(
            "expected plan SHA256 must be 64 lowercase hexadecimal characters"
        )
    path = safe_vault_path(vault, os.path.abspath(os.path.expanduser(plan_path)))
    data = secure_read_bytes(path, MAX_PLAN_BYTES, root=vault)
    if len(data) > MAX_PLAN_BYTES:
        raise IdentityRepairPreconditionError("approved plan exceeds size limit")
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise IdentityRepairPreconditionError(
            f"approved plan SHA256 mismatch: expected {expected}, current {actual}"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IdentityRepairPreconditionError(
            "approved plan is not UTF-8"
        ) from exc
    frontmatter_text, body = split_frontmatter_text(text)
    if frontmatter_text is None or body is None:
        raise IdentityRepairPreconditionError("approved plan has no frontmatter")
    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise IdentityRepairPreconditionError(
            "approved plan frontmatter is invalid"
        ) from exc
    if not isinstance(frontmatter, dict):
        raise IdentityRepairPreconditionError(
            "approved plan frontmatter must be a mapping"
        )
    if frontmatter.get("summary_type") != PLAN_SUMMARY_TYPE:
        raise IdentityRepairPreconditionError("unexpected approved plan type")
    if frontmatter.get("read_only") is not True:
        raise IdentityRepairPreconditionError("approved plan is not read-only")
    if (
        str(frontmatter.get("recommendation_schema_version") or "")
        != PLAN_RECOMMENDATION_SCHEMA
    ):
        raise IdentityRepairPreconditionError(
            "unsupported conflict recommendation schema"
        )
    conflicts = _parse_conflicts(body)
    expected_count = frontmatter.get("conflict_count")
    if not isinstance(expected_count, int) or expected_count != len(conflicts):
        raise IdentityRepairPreconditionError(
            "approved plan conflict count does not match its body"
        )
    if not conflicts:
        raise IdentityRepairPreconditionError("approved plan has no conflicts")
    return ApprovedPlan(
        path=path,
        sha256=actual,
        content=data,
        generated_at=str(frontmatter.get("generated_at") or ""),
        conflicts=tuple(conflicts),
    )


def _parse_conflicts(body):
    headings = list(
        re.finditer(r"(?m)^## ([0-9]+)\. `([^`]+)`[ \t]*$", body)
    )
    conflicts = []
    seen_ids = set()
    seen_proposed_ids = set()
    for offset, heading in enumerate(headings):
        number = int(heading.group(1))
        memory_id = heading.group(2).strip()
        if number != offset + 1:
            raise IdentityRepairPreconditionError(
                "approved plan conflict numbering is not sequential"
            )
        if memory_id in seen_ids or not is_valid_memory_id(memory_id):
            raise IdentityRepairPreconditionError(
                f"approved plan has an invalid or duplicate conflict ID: {memory_id}"
            )
        seen_ids.add(memory_id)
        end = headings[offset + 1].start() if offset + 1 < len(headings) else len(body)
        section = body[heading.end():end]
        conflicts.append(
            _parse_conflict_section(
                memory_id,
                section,
                seen_proposed_ids,
            )
        )
    return conflicts


def _parse_conflict_section(memory_id, section, seen_proposed_ids):
    fact_relation = _field(section, "事实关系")
    confidence = _field(section, "置信度")
    retained_id = _field(section, "保留原 ID")
    if retained_id != memory_id:
        raise IdentityRepairPreconditionError(
            f"approved plan retained ID mismatch for {memory_id}"
        )
    owner_match = _single_match(
        r"(?m)^- 推荐 Owner: \[\[([^\]|]+)\|source\]\] \+ `([^`]+)`"
        r"（`([^`]+)` / ([^)]+)）[ \t]*$",
        section,
        f"owner binding for {memory_id}",
    )
    owner_target = _single_match(
        r"(?m)^- Owner Target: ([^/\n]+?) / \[\[([^\]|]+)\|target\]\][ \t]*$",
        section,
        f"owner target for {memory_id}",
    )
    owner = ApprovedOwner(
        source=_wiki_path(owner_match.group(1)),
        revision=owner_match.group(2).strip(),
        status=owner_match.group(3).strip(),
        project=owner_match.group(4).strip(),
        source_locator=_field(section, "Owner Locator"),
        source_digest=_field(section, "Owner Source Digest"),
        action=_field(section, "Owner Action"),
        resulting_status=_field(section, "Owner Resulting Status"),
        target_project=owner_target.group(1).strip(),
        target_source=_wiki_path(owner_target.group(2)),
        relocate=_yes_no(_field(section, "Owner Relocate"), "Owner Relocate"),
        source_refs_after_merge=_csv_field(
            _field(section, "Owner Source Refs After Merge")
        ),
    )
    if owner.action not in SUPPORTED_OWNER_ACTIONS:
        raise IdentityRepairPreconditionError(
            f"unsupported owner action in approved plan: {owner.action}"
        )
    if owner.relocate:
        raise IdentityRepairPreconditionError(
            "owner relocation is not supported by the guarded repair executor"
        )

    action_heading = list(
        re.finditer(r"(?m)^#### Action ([0-9]+)[ \t]*$", section)
    )
    actions = []
    for offset, heading in enumerate(action_heading):
        if int(heading.group(1)) != offset + 1:
            raise IdentityRepairPreconditionError(
                f"approved action numbering is not sequential for {memory_id}"
            )
        end = (
            action_heading[offset + 1].start()
            if offset + 1 < len(action_heading)
            else len(section)
        )
        action_section = section[heading.end():end]
        action = _parse_action(memory_id, action_section)
        if action.proposed_id in seen_proposed_ids:
            raise IdentityRepairPreconditionError(
                f"approved plan repeats proposed ID {action.proposed_id}"
            )
        seen_proposed_ids.add(action.proposed_id)
        actions.append(action)
    if not actions:
        raise IdentityRepairPreconditionError(
            f"approved conflict has no repair actions: {memory_id}"
        )
    reason_match = _single_match(
        r"(?m)^- 理由: (.+?)[ \t]*$",
        section,
        f"recommendation reason for {memory_id}",
    )
    return ApprovedConflict(
        memory_id=memory_id,
        fact_relation=fact_relation,
        confidence=confidence,
        recommendation_reason=reason_match.group(1).strip(),
        owner=owner,
        actions=tuple(actions),
    )


def _parse_action(memory_id, section):
    source_match = _single_match(
        r"(?m)^- Source: \[\[([^\]|]+)\|source\]\][ \t]*$",
        section,
        f"action source for {memory_id}",
    )
    target_match = _single_match(
        r"(?m)^- Target Source: \[\[([^\]|]+)\|target\]\][ \t]*$",
        section,
        f"action target for {memory_id}",
    )
    action_name = _field(section, "Action")
    if action_name not in SUPPORTED_ACTIONS:
        raise IdentityRepairPreconditionError(
            f"unsupported action in approved plan: {action_name}"
        )
    proposed_id = _field(section, "Proposed ID")
    if not is_valid_memory_id(proposed_id) or proposed_id == memory_id:
        raise IdentityRepairPreconditionError(
            f"invalid proposed ID for {memory_id}: {proposed_id}"
        )
    replacement_id = _field(section, "Replacement ID")
    if replacement_id == "-":
        replacement_id = ""
    if action_name == "rekey_then_supersede":
        if replacement_id != memory_id:
            raise IdentityRepairPreconditionError(
                f"supersede replacement must be the retained ID {memory_id}"
            )
    elif replacement_id:
        raise IdentityRepairPreconditionError(
            f"replacement ID is invalid for {action_name}"
        )
    preserve_source_refs = _yes_no(
        _field(section, "Preserve Source Refs"),
        "Preserve Source Refs",
    )
    if not preserve_source_refs:
        raise IdentityRepairPreconditionError(
            "identity repair cannot discard approved source evidence"
        )
    return ApprovedAction(
        source=_wiki_path(source_match.group(1)),
        source_locator=_field(section, "Source Locator"),
        source_identity=_field(section, "Source Identity"),
        source_digest=_field(section, "Source Digest"),
        revision=_field(section, "Expected Revision"),
        current_status=_field(section, "Current Status"),
        action=action_name,
        proposed_id=proposed_id,
        replacement_id=replacement_id,
        target_project=_field(section, "Target Project"),
        target_source=_wiki_path(target_match.group(1)),
        relocate=_yes_no(_field(section, "Relocate"), "Relocate"),
        resulting_status=_field(section, "Resulting Status"),
        preserve_source_refs=preserve_source_refs,
        merge_source_refs_into_owner=_yes_no(
            _field(section, "Merge Source Refs Into Owner"),
            "Merge Source Refs Into Owner",
        ),
        reason=_plain_field(section, "Reason"),
    )


def _field(section, label):
    match = _single_match(
        rf"(?m)^- {re.escape(label)}: `([^`]+)`[ \t]*$",
        section,
        label,
    )
    return match.group(1).strip()


def _plain_field(section, label):
    match = _single_match(
        rf"(?m)^- {re.escape(label)}: (.+?)[ \t]*$",
        section,
        label,
    )
    return match.group(1).strip()


def _single_match(pattern, text, label):
    matches = list(re.finditer(pattern, text))
    if len(matches) != 1:
        raise IdentityRepairPreconditionError(
            f"approved plan must contain exactly one {label}"
        )
    return matches[0]


def _wiki_path(value):
    path = str(value or "").strip()
    return path if path.endswith(".md") else path + ".md"


def _yes_no(value, label):
    if value not in {"yes", "no"}:
        raise IdentityRepairPreconditionError(
            f"approved plan {label} must be yes or no"
        )
    return value == "yes"


def _csv_field(value):
    if value == "-":
        return ()
    return tuple(sorted(item.strip() for item in value.split(",") if item.strip()))


def _validate_current_snapshot(plan, report, cfg):
    current_conflicts = {
        str(item.get("id") or ""): item
        for item in report.get("identity_conflicts") or []
    }
    approved_ids = {item.memory_id for item in plan.conflicts}
    if set(current_conflicts) != approved_ids:
        raise IdentityRepairPreconditionError(
            "approved snapshot changed: current conflict IDs differ from the plan"
        )
    if report.get("identity_conflict_count") != len(plan.conflicts):
        raise IdentityRepairPreconditionError(
            "approved snapshot changed: conflict count differs from the plan"
        )

    for approved in plan.conflicts:
        current = current_conflicts[approved.memory_id]
        _compare_snapshot_field(
            approved.memory_id,
            "fact_relation",
            approved.fact_relation,
            current.get("fact_relation"),
        )
        _compare_snapshot_field(
            approved.memory_id,
            "confidence",
            approved.confidence,
            current.get("confidence"),
        )
        _compare_snapshot_mapping(
            approved.memory_id,
            "owner",
            _owner_dict(approved.owner),
            _current_owner_dict(current.get("recommended_owner") or {}),
        )
        approved_actions = [_action_dict(item) for item in approved.actions]
        current_actions = [
            _current_action_dict(item)
            for item in current.get("recommended_actions") or []
        ]
        if approved_actions != current_actions:
            raise IdentityRepairPreconditionError(
                f"approved snapshot changed: action bindings differ for {approved.memory_id}"
            )

    used = set()
    for location in find_records(cfg):
        used.add(location.memory_id)
        used.update(str(item).strip() for item in location.record.get("aliases") or [])
        used.update(str(item).strip() for item in location.record.get("requires") or [])
        successor = str(location.record.get("superseded_by") or "").strip()
        if successor:
            used.add(successor)
    collisions = sorted(
        action.proposed_id
        for conflict in plan.conflicts
        for action in conflict.actions
        if action.proposed_id in used
    )
    if collisions:
        raise IdentityRepairPreconditionError(
            "approved snapshot changed: proposed IDs became used: "
            + ", ".join(collisions[:10])
        )


def _owner_dict(owner):
    return {
        "source": owner.source,
        "source_locator": owner.source_locator,
        "source_digest": owner.source_digest,
        "revision": owner.revision,
        "status": owner.status,
        "project": owner.project,
        "action": owner.action,
        "resulting_status": owner.resulting_status,
        "target_project": owner.target_project,
        "target_source": owner.target_source,
        "relocate": owner.relocate,
        "source_refs_after_merge": tuple(owner.source_refs_after_merge),
    }


def _current_owner_dict(owner):
    return {
        "source": str(owner.get("source") or ""),
        "source_locator": str(owner.get("source_locator") or ""),
        "source_digest": str(owner.get("source_digest") or ""),
        "revision": str(owner.get("revision") or ""),
        "status": str(owner.get("status") or ""),
        "project": str(owner.get("project") or ""),
        "action": str(owner.get("action") or ""),
        "resulting_status": str(owner.get("resulting_status") or ""),
        "target_project": str(owner.get("target_project") or ""),
        "target_source": str(owner.get("target_source") or ""),
        "relocate": bool(owner.get("relocate")),
        "source_refs_after_merge": tuple(
            sorted(str(item) for item in owner.get("source_refs_after_merge") or [])
        ),
    }


def _action_dict(action):
    return asdict(action)


def _current_action_dict(action):
    return {
        "source": str(action.get("source") or ""),
        "source_locator": str(action.get("source_locator") or ""),
        "source_identity": str(action.get("source_identity") or ""),
        "source_digest": str(action.get("source_digest") or ""),
        "revision": str(action.get("revision") or ""),
        "current_status": str(action.get("current_status") or ""),
        "action": str(action.get("action") or ""),
        "proposed_id": str(action.get("proposed_id") or ""),
        "replacement_id": str(action.get("replacement_id") or ""),
        "target_project": str(action.get("target_project") or ""),
        "target_source": str(action.get("target_source") or ""),
        "relocate": bool(action.get("relocate")),
        "resulting_status": str(action.get("resulting_status") or ""),
        "preserve_source_refs": bool(action.get("preserve_source_refs")),
        "merge_source_refs_into_owner": bool(
            action.get("merge_source_refs_into_owner")
        ),
        "reason": str(action.get("reason") or ""),
    }


def _compare_snapshot_mapping(memory_id, label, expected, current):
    for key in expected:
        _compare_snapshot_field(
            memory_id,
            f"{label}.{key}",
            expected[key],
            current.get(key),
        )


def _compare_snapshot_field(memory_id, field, expected, current):
    if expected != current:
        raise IdentityRepairPreconditionError(
            f"approved snapshot changed: {memory_id} {field} differs"
        )


def _build_mutations(cfg, plan):
    vault = _vault(cfg)
    documents = {}
    resulting_records = {}
    original_source_refs = {}
    planned_identity_ids = {
        conflict.memory_id
        for conflict in plan.conflicts
    } | {
        action.proposed_id
        for conflict in plan.conflicts
        for action in conflict.actions
    }
    occupied_locators = set()
    approved_source_digests = {}
    for conflict in plan.conflicts:
        bindings = [(conflict.owner.source, conflict.owner.source_digest)]
        bindings.extend(
            (action.source, action.source_digest)
            for action in conflict.actions
        )
        for source, digest in bindings:
            previous = approved_source_digests.setdefault(source, digest)
            if previous != digest:
                raise IdentityRepairPreconditionError(
                    f"approved plan has conflicting digests for {source}"
                )
    for conflict in plan.conflicts:
        for target_source in [
            conflict.owner.target_source,
            *(action.target_source for action in conflict.actions),
        ]:
            if target_source not in approved_source_digests:
                raise IdentityRepairPreconditionError(
                    f"approved target source has no bound source digest: {target_source}"
                )

    def document(relative_path, expected_digest=""):
        path = safe_vault_path(vault, relative_path)
        item = documents.get(path)
        if item is None:
            item = _read_aggregate_document(path, vault)
            documents[path] = item
        if expected_digest and item.digest != expected_digest:
            raise IdentityRepairPreconditionError(
                f"approved snapshot changed: source digest differs for {relative_path}"
            )
        return item

    owner_records = {}
    for conflict in plan.conflicts:
        owner = conflict.owner
        owner_doc = document(owner.source, owner.source_digest)
        owner_index = _locator_index(
            owner.source_locator,
            owner.source,
            owner_doc.key,
        )
        owner_record = _record_at(owner_doc, owner_index)
        _assert_record_binding(
            owner_record,
            conflict.memory_id,
            owner.revision,
            owner.status,
            owner.source_locator,
        )
        owner_records[conflict.memory_id] = (owner_doc, owner_index)
        occupied_locators.add(owner.source_locator)
        document(
            owner.target_source,
            approved_source_digests[owner.target_source],
        )

        for action in conflict.actions:
            action_doc = document(action.source, action.source_digest)
            action_index = _locator_index(
                action.source_locator,
                action.source,
                action_doc.key,
            )
            action_record = _record_at(action_doc, action_index)
            _assert_record_binding(
                action_record,
                conflict.memory_id,
                action.revision,
                action.current_status,
                action.source_locator,
            )
            if action.source_locator in occupied_locators:
                raise IdentityRepairPreconditionError(
                    f"approved plan reuses source locator {action.source_locator}"
                )
            occupied_locators.add(action.source_locator)
            document(
                action.target_source,
                approved_source_digests[action.target_source],
            )

    removals = {}
    appends = {}
    for conflict in plan.conflicts:
        owner_doc, owner_index = owner_records[conflict.memory_id]
        owner_record = dict(_record_at(owner_doc, owner_index))
        if conflict.owner.action == "retain_id_then_retract":
            _clear_lifecycle_fields(owner_record)
            owner_record["status"] = "retracted"
            owner_record["retracted_reason"] = conflict.recommendation_reason
        owner_record["source_refs"] = list(
            conflict.owner.source_refs_after_merge
        )
        owner_record["aliases"] = _aliases_without_reserved_ids(
            owner_record,
            planned_identity_ids,
        )
        owner_record = _normalize_for_target(
            owner_record,
            "decision" if owner_doc.key == "decisions" else "error",
            conflict.owner.target_project,
            conflict.owner.target_source,
            owner_doc.key,
            owner_index,
        )
        if owner_record["id"] != conflict.memory_id:
            raise IdentityRepairPreconditionError("owner ID changed during preparation")
        if owner_record["status"] != conflict.owner.resulting_status:
            raise IdentityRepairPreconditionError(
                f"owner resulting status mismatch for {conflict.memory_id}"
            )
        owner_doc.frontmatter[owner_doc.key][owner_index] = owner_record
        resulting_records[conflict.memory_id] = owner_record

        for action in conflict.actions:
            source_doc = documents[safe_vault_path(vault, action.source)]
            source_index = _locator_index(
                action.source_locator,
                action.source,
                source_doc.key,
            )
            source_record = dict(_record_at(source_doc, source_index))
            original_source_refs[action.proposed_id] = tuple(
                sorted(str(item) for item in source_record.get("source_refs") or [])
            )
            source_record["id"] = action.proposed_id
            source_record["project"] = canonical_project(action.target_project)
            source_record["aliases"] = _aliases_without_reserved_ids(
                source_record,
                planned_identity_ids,
            )
            if action.action == "rekey_then_supersede":
                _clear_lifecycle_fields(source_record)
                source_record["status"] = "superseded"
                source_record["superseded_by"] = action.replacement_id
            elif action.action == "rekey_then_retract":
                _clear_lifecycle_fields(source_record)
                source_record["status"] = "retracted"
                source_record["retracted_reason"] = action.reason
            elif action.action == "rekey_and_keep":
                _clear_lifecycle_fields(source_record)
                source_record["status"] = "active"
            elif action.action == "rekey_and_retain_inactive":
                pass
            source_record = _normalize_for_target(
                source_record,
                "decision" if source_doc.key == "decisions" else "error",
                action.target_project,
                action.target_source,
                source_doc.key,
                -1,
            )
            if source_record["status"] != action.resulting_status:
                raise IdentityRepairPreconditionError(
                    f"resulting status mismatch for {action.proposed_id}"
                )
            if conflict.memory_id in (source_record.get("aliases") or []):
                raise IdentityRepairPreconditionError(
                    f"old conflicting ID leaked into aliases for {action.proposed_id}"
                )
            if not set(original_source_refs[action.proposed_id]).issubset(
                set(source_record.get("source_refs") or [])
            ):
                raise IdentityRepairPreconditionError(
                    f"source evidence would be lost for {action.proposed_id}"
                )
            resulting_records[action.proposed_id] = source_record
            if action.relocate:
                removals.setdefault(source_doc.path, set()).add(source_index)
                target_path = safe_vault_path(vault, action.target_source)
                appends.setdefault(target_path, []).append(source_record)
            else:
                source_doc.frontmatter[source_doc.key][source_index] = source_record

    for path, indexes in removals.items():
        doc = documents[path]
        for index in sorted(indexes, reverse=True):
            del doc.frontmatter[doc.key][index]
    for path, records in appends.items():
        doc = documents[path]
        doc.frontmatter[doc.key].extend(records)

    _validate_mutated_documents(cfg, plan, documents, resulting_records)
    rendered = {
        path: _render_aggregate_document(doc).encode("utf-8")
        for path, doc in documents.items()
        if hashlib.sha256(_render_aggregate_document(doc).encode("utf-8")).hexdigest()
        != doc.digest
    }
    if not rendered:
        raise IdentityRepairPreconditionError("approved plan produced no mutations")
    return documents, rendered, resulting_records, original_source_refs


def _read_aggregate_document(path, vault):
    data = secure_read_bytes(path, MAX_FORMAL_FILE_BYTES, root=vault)
    if len(data) > MAX_FORMAL_FILE_BYTES:
        raise IdentityRepairPreconditionError(
            f"formal memory source exceeds size limit: {_relative(path, vault)}"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IdentityRepairPreconditionError(
            f"formal memory source is not UTF-8: {_relative(path, vault)}"
        ) from exc
    frontmatter_text, body = split_frontmatter_text(text)
    if frontmatter_text is None or body is None:
        raise IdentityRepairPreconditionError(
            f"formal memory source has no frontmatter: {_relative(path, vault)}"
        )
    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise IdentityRepairPreconditionError(
            f"formal memory source YAML is invalid: {_relative(path, vault)}"
        ) from exc
    filename = os.path.basename(path)
    key = "decisions" if filename == "decisions.md" else "pitfalls" if filename == "pitfalls.md" else ""
    if (
        not isinstance(frontmatter, dict)
        or frontmatter.get("schema_version") != RUNTIME_SCHEMA_VERSION
        or not key
        or not isinstance(frontmatter.get(key), list)
    ):
        raise IdentityRepairPreconditionError(
            f"unsupported formal aggregate source: {_relative(path, vault)}"
        )
    physical_project = _project_from_path(_relative(path, vault))
    if canonical_project(frontmatter.get("project")) != physical_project:
        raise IdentityRepairPreconditionError(
            f"formal aggregate project/path mismatch: {_relative(path, vault)}"
        )
    return _AggregateDocument(
        path=path,
        relative_path=_relative(path, vault),
        digest=hashlib.sha256(data).hexdigest(),
        frontmatter=frontmatter,
        body=body,
        key=key,
    )


def _locator_index(locator, expected_path, expected_key):
    match = re.fullmatch(
        r"(.+\.md)#(decisions|pitfalls)\[([0-9]+)\]",
        locator,
    )
    if not match:
        raise IdentityRepairPreconditionError(
            f"approved locator is not an aggregate record: {locator}"
        )
    if match.group(1) != expected_path or match.group(2) != expected_key:
        raise IdentityRepairPreconditionError(
            f"approved locator/source mismatch: {locator}"
        )
    return int(match.group(3))


def _record_at(document, index):
    values = document.frontmatter[document.key]
    if index < 0 or index >= len(values) or not isinstance(values[index], dict):
        raise IdentityRepairPreconditionError(
            f"approved source locator no longer exists: {document.relative_path}#{document.key}[{index}]"
        )
    return values[index]


def _assert_record_binding(record, memory_id, revision, status, locator):
    if (
        str(record.get("id") or "") != memory_id
        or str(record.get("revision") or "") != revision
        or str(record.get("status") or "") != status
    ):
        raise IdentityRepairPreconditionError(
            f"approved snapshot changed at {locator}"
        )


def _normalize_for_target(
    record,
    memory_type,
    project,
    target_source,
    key,
    index,
):
    project = canonical_project(project)
    source_ref = "note:" + target_source.removesuffix(".md")
    raw = dict(record)
    raw["project"] = project
    refs = {
        str(item).strip()
        for item in raw.get("source_refs") or []
        if str(item).strip()
    }
    refs.add(source_ref)
    raw["source_refs"] = sorted(refs)
    raw["aliases"] = sorted(
        {
            str(item).strip()
            for item in raw.get("aliases") or []
            if str(item).strip() and str(item).strip() != raw.get("id")
        }
    )
    normalized = normalize_formal_record(
        raw,
        memory_type=memory_type,
        default_project=project,
        source_ref=source_ref,
        source_record_key=f"{key}:{index}",
    )
    raw["revision"] = memory_revision(normalized)
    return raw


def _clear_lifecycle_fields(record):
    for key in ("superseded_by", "retracted_reason", "expired_reason"):
        record.pop(key, None)


def _aliases_without_reserved_ids(record, reserved_ids):
    return sorted(
        {
            str(item).strip()
            for item in record.get("aliases") or []
            if str(item).strip() and str(item).strip() not in reserved_ids
        }
    )


def _validate_mutated_documents(cfg, plan, documents, resulting_records):
    affected_paths = set(documents)
    all_ids = []
    alias_holders = {}
    for location in find_records(cfg):
        if location.path not in affected_paths:
            all_ids.append(location.memory_id)
            for alias in location.record.get("aliases") or []:
                alias = str(alias).strip()
                if alias:
                    alias_holders.setdefault(alias, []).append(location.memory_id)
    for document in documents.values():
        expected_type = "decision" if document.key == "decisions" else "error"
        physical_project = _project_from_path(document.relative_path)
        for index, raw in enumerate(document.frontmatter[document.key]):
            if not isinstance(raw, dict):
                raise IdentityRepairPreconditionError(
                    f"non-record remains in {document.relative_path} at index {index}"
                )
            normalized = normalize_formal_record(
                raw,
                memory_type=expected_type,
                default_project=physical_project,
                source_ref="note:" + document.relative_path.removesuffix(".md"),
                source_record_key=f"{document.key}:{index}",
            )
            if raw.get("revision") != normalized.get("revision"):
                raise IdentityRepairPreconditionError(
                    f"invalid resulting revision for {raw.get('id', '')}"
                )
            if (
                normalized.get("id") in resulting_records
                and normalized.get("project") != physical_project
            ):
                raise IdentityRepairPreconditionError(
                    f"resulting project/path mismatch for {raw.get('id', '')}"
                )
            if normalized.get("status") not in FORMAL_MEMORY_STATUSES:
                raise IdentityRepairPreconditionError(
                    f"invalid resulting status for {raw.get('id', '')}"
                )
            all_ids.append(normalized["id"])
            for alias in normalized.get("aliases") or []:
                alias = str(alias).strip()
                if alias:
                    alias_holders.setdefault(alias, []).append(normalized["id"])
    duplicates = sorted(
        memory_id for memory_id in set(all_ids) if all_ids.count(memory_id) > 1
    )
    if duplicates:
        raise IdentityRepairPreconditionError(
            "identity repair would leave duplicate IDs: "
            + ", ".join(duplicates[:10])
        )
    expected_ids = {
        conflict.memory_id
        for conflict in plan.conflicts
    } | {
        action.proposed_id
        for conflict in plan.conflicts
        for action in conflict.actions
    }
    if set(resulting_records) != expected_ids:
        raise IdentityRepairPreconditionError(
            "identity repair did not construct every approved result"
        )
    shadowed = sorted(expected_ids & set(alias_holders))
    if shadowed:
        raise IdentityRepairPreconditionError(
            "identity repair would leave planned IDs in aliases: "
            + ", ".join(shadowed[:10])
        )


def _render_aggregate_document(document):
    return (
        "---\n"
        + yaml.safe_dump(
            document.frontmatter,
            allow_unicode=True,
            sort_keys=False,
        )
        + "---\n"
        + str(document.body or "")
    )


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

    targets = [("vault", path) for path in prepared.rendered]
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
            raise IdentityRepairPreconditionError(
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
        "operation_type": "formal-memory-identity-repair",
        "operation_id": operation_id,
        "status": "prepared",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "authority": "user",
        "approved_plan": _relative(prepared.plan.path, vault),
        "approved_plan_sha256": prepared.plan.sha256,
        "approved_conflict_ids": [
            conflict.memory_id for conflict in prepared.plan.conflicts
        ],
        "approved_action_ids": [
            action.proposed_id
            for conflict in prepared.plan.conflicts
            for action in conflict.actions
        ],
        "targets": manifest_targets,
    }
    manifest_path = safe_vault_path(operation_root, "manifest.json")
    durable_atomic_write(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        root=vault,
    )
    return snapshots, manifest_path


def _classify_snapshot_path(vault, path):
    try:
        inside = os.path.commonpath([vault, path]) == vault
    except ValueError:
        inside = False
    return ("vault", safe_vault_path(vault, path)) if inside else ("external", path)


def _verify_restored_snapshots(vault, snapshots):
    mismatches = []
    for item in snapshots:
        path = (
            safe_vault_path(vault, item["path"])
            if item["kind"] == "vault"
            else item["path"]
        )
        data, _mode = _snapshot_file(item["kind"], path, vault)
        if item["existed"]:
            if data != item["data"]:
                mismatches.append(path)
        elif data is not None:
            mismatches.append(path)
    if mismatches:
        raise RuntimeError(
            "rollback verification failed for: " + ", ".join(mismatches[:10])
        )


def _verify_applied_state(cfg, prepared):
    report = audit_formal_memories(cfg)
    if report.get("identity_conflict_count") != 0:
        raise RuntimeError(
            "identity conflicts remain after repair: "
            + str(report.get("identity_conflict_count"))
        )
    locations = find_records(cfg)
    by_id = {}
    for location in locations:
        if location.memory_id in by_id:
            raise RuntimeError(
                f"formal memory ID remained non-unique: {location.memory_id}"
            )
        by_id[location.memory_id] = location
    checked_ids = {
        conflict.memory_id
        for conflict in prepared.plan.conflicts
    } | {
        action.proposed_id
        for conflict in prepared.plan.conflicts
        for action in conflict.actions
    }
    alias_holders = {
        alias: location.memory_id
        for location in locations
        for alias in (
            str(item).strip()
            for item in location.record.get("aliases") or []
        )
        if alias in checked_ids
    }
    if alias_holders:
        raise RuntimeError(
            "planned IDs remained in formal aliases: "
            + ", ".join(sorted(alias_holders)[:10])
        )

    for conflict in prepared.plan.conflicts:
        owner = by_id.get(conflict.memory_id)
        if owner is None:
            raise RuntimeError(f"owner disappeared after repair: {conflict.memory_id}")
        if (
            _relative(owner.path, _vault(cfg)) != conflict.owner.target_source
            or owner.status != conflict.owner.resulting_status
            or owner.project != conflict.owner.target_project
        ):
            raise RuntimeError(
                f"owner postcondition failed for {conflict.memory_id}"
            )
        if tuple(sorted(owner.record.get("source_refs") or [])) != tuple(
            sorted(conflict.owner.source_refs_after_merge)
        ):
            raise RuntimeError(
                f"owner source_refs postcondition failed for {conflict.memory_id}"
            )
        for action in conflict.actions:
            location = by_id.get(action.proposed_id)
            if location is None:
                raise RuntimeError(
                    f"rekeyed record disappeared: {action.proposed_id}"
                )
            if (
                _relative(location.path, _vault(cfg)) != action.target_source
                or location.status != action.resulting_status
                or location.project != action.target_project
            ):
                raise RuntimeError(
                    f"action postcondition failed for {action.proposed_id}"
                )
            if conflict.memory_id in (location.record.get("aliases") or []):
                raise RuntimeError(
                    f"old ID entered aliases for {action.proposed_id}"
                )
            original_refs = set(
                prepared.original_source_refs.get(action.proposed_id) or []
            )
            if not original_refs.issubset(set(location.record.get("source_refs") or [])):
                raise RuntimeError(
                    f"source_refs were lost for {action.proposed_id}"
                )
            if action.action == "rekey_then_supersede" and (
                location.record.get("superseded_by") != conflict.memory_id
            ):
                raise RuntimeError(
                    f"supersession target failed for {action.proposed_id}"
                )

    recall_path = configured_recall_index_path(cfg)
    try:
        payload = json.loads(_read_text(recall_path, _vault(cfg)))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("runtime recall index is invalid after repair") from exc
    identity_counts = {}
    for item in payload.get("units") or []:
        if not isinstance(item, dict):
            continue
        for value in [item.get("id"), *(item.get("aliases") or [])]:
            identity = str(value or "").strip()
            if identity:
                identity_counts[identity] = identity_counts.get(identity, 0) + 1
    identities = set(identity_counts)
    for memory_id in checked_ids:
        location = by_id[memory_id]
        expected_count = 1 if location.status == "active" else 0
        actual_count = identity_counts.get(memory_id, 0)
        if actual_count != expected_count:
            raise RuntimeError(
                "repaired memory has an invalid recall identity count: "
                f"{memory_id} expected={expected_count} actual={actual_count}"
            )
    return {
        "identity_conflict_count_after": 0,
        "formal_record_count_after": len(locations),
        "recall_identity_count_after": len(identities),
    }


def _append_audit(cfg, operation_id, manifest_path, prepared):
    vault = _vault(cfg)
    path = _audit_path(cfg)
    manifest_href = os.path.relpath(
        manifest_path,
        os.path.dirname(path),
    ).replace(os.sep, "/")
    if os.path.isfile(path) and not os.path.islink(path):
        existing = _read_text(path, vault)
    else:
        existing = (
            "---\n"
            "title: Memory Lifecycle Audit\n"
            "summary_type: lifecycle-audit\n"
            "generated_by: memory_identity_repair.py\n"
            f"schema_version: '{RUNTIME_SCHEMA_VERSION}'\n"
            "---\n\n"
            "# Memory Lifecycle Audit\n\n"
            "正式记忆状态与身份变更的审计记录。\n"
        )
    timestamp = datetime.now(timezone.utc).isoformat()
    lines = [
        f"## {timestamp} identity-repair {prepared.plan.sha256[:12]}",
        "",
        f"- operation_id: `{operation_id}`",
        "- action: `identity-repair-batch`",
        "- authority: `user`",
        f"- approved_plan_sha256: `{prepared.plan.sha256}`",
        f"- conflict_count: `{len(prepared.plan.conflicts)}`",
        "- action_count: `{}`".format(
            sum(len(item.actions) for item in prepared.plan.conflicts)
        ),
        f"- rollback_manifest: [manifest.json](<{manifest_href}>)",
        "",
        "### Identity Changes",
        "",
        "| Retained ID | New ID | Action | Result | Source | Target | Revision Precondition | Source Digest | Source Locator |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for conflict in prepared.plan.conflicts:
        for action in conflict.actions:
            lines.append(
                "| `{old}` | `{new}` | `{action}` | `{status}` | [[{source}]] | "
                "[[{target}]] | `{revision}` | `{digest}` | `{locator}` |".format(
                    old=conflict.memory_id,
                    new=action.proposed_id,
                    action=action.action,
                    status=action.resulting_status,
                    source=action.source.removesuffix(".md"),
                    target=action.target_source.removesuffix(".md"),
                    revision=action.revision,
                    digest=action.source_digest,
                    locator=action.source_locator,
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
    actions = [
        {
            "retained_id": conflict.memory_id,
            **asdict(action),
        }
        for conflict in prepared.plan.conflicts
        for action in conflict.actions
    ]
    return {
        "applied": False,
        "plan": prepared.plan.path,
        "plan_sha256": prepared.plan.sha256,
        "conflict_count": len(prepared.plan.conflicts),
        "action_count": len(actions),
        "affected_file_count": len(prepared.rendered),
        "actions": actions,
    }


def _operation_id(plan):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"identity-repair-{stamp}-{plan.sha256[:12]}"


def _audit_path(cfg):
    vault = _vault(cfg)
    raw = (cfg.get("memory_lifecycle") or {}).get(
        "audit_path",
        "05-Agent-Memory/lifecycle-audit.md",
    )
    return safe_vault_path(vault, raw)


def _read_text(path, vault):
    data = secure_read_bytes(path, MAX_FORMAL_FILE_BYTES, root=vault)
    if len(data) > MAX_FORMAL_FILE_BYTES:
        raise ValueError(f"file exceeds size limit: {_relative(path, vault)}")
    return data.decode("utf-8")


def _project_from_path(path):
    match = re.fullmatch(
        r"01-Projects/([^/]+)/Memory/(?:decisions|pitfalls)\.md",
        str(path or "").replace("\\", "/"),
    )
    return canonical_project(match.group(1)) if match else ""


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
        description="Preview or apply an explicitly approved identity conflict plan"
    )
    parser.add_argument("--plan", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the approved batch; without this flag the command is read-only",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        cfg = load_config()
        result = apply_identity_repair(
            cfg,
            args.plan,
            args.expected_sha256,
            apply=args.apply,
        )
    except (IdentityRepairError, OSError, ValueError) as exc:
        print(f"identity repair failed: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        mode = "applied" if result.get("applied") else "preview"
        print(
            f"identity repair {mode}: {result['conflict_count']} conflicts, "
            f"{result['action_count']} actions, SHA256 {result['plan_sha256']}"
        )
        if result.get("rollback_manifest"):
            print(f"rollback manifest: {result['rollback_manifest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
