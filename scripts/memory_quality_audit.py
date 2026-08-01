#!/usr/bin/env python3
"""Audit existing formal memory without mutating its lifecycle state."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timezone

import yaml

from annotation_quality import (
    QUALITY_FORMAL,
    FORMAL_RECALL_SUPPRESSION_REASONS,
    assess_decision,
    assess_error,
    assess_favor,
    collapse_runtime_duplicates,
)
from memory_lifecycle import (
    LifecycleConflict,
    create_proposal,
    find_records,
)
from memory_schema import (
    MEMORY_RELATION_FIELDS,
    OPERATIONAL_MEMORY_FIELDS,
    normalize_fact_text,
    suppress_unmet_dependencies,
)
from safety import (
    durable_atomic_write,
    ensure_directory_tree,
    exclusive_file_lock,
    redact_sensitive,
    safe_vault_path,
    secure_list_directory,
    secure_read_bytes,
    split_frontmatter_text,
)


DEFAULT_REPORT_PATH = "04-Feedback/memory-quality-report.md"
DEFAULT_CONFLICT_PLAN_PATH = "04-Feedback/memory-quality-conflicts.md"
OLD_MEMORY_PLAN_SCHEMA_VERSION = "1.0"
HIGH_CONFIDENCE_RETRACT_REASONS = FORMAL_RECALL_SUPPRESSION_REASONS
MAX_PROPOSAL_BYTES = 1024 * 1024
ROUTING_HOST_PROJECT = "agent-memory-beacon"
PLACEHOLDER_TITLES = frozenset(
    {"summary", "decision", "placeholder", "todo", "摘要", "总结", "决策"}
)
PLACEHOLDER_SUMMARIES = frozenset(
    {"why", "context", "reason", "placeholder", "原因", "上下文", "待补充"}
)


def audit_formal_memories(cfg):
    all_locations = find_records(cfg)
    locations = [item for item in all_locations if item.status == "active"]
    locations_by_id = defaultdict(list)
    for location in all_locations:
        locations_by_id[location.memory_id].append(location)
    identity_conflict_ids = {
        memory_id
        for memory_id, members in locations_by_id.items()
        if len(members) > 1
    }
    identity_conflicts = []
    used_memory_ids = set(locations_by_id)
    for location in all_locations:
        used_memory_ids.update(
            str(item).strip()
            for key in ("aliases", "requires")
            for item in (location.record.get(key) or [])
            if str(item).strip()
        )
        superseded_by = str(location.record.get("superseded_by") or "").strip()
        if superseded_by:
            used_memory_ids.add(superseded_by)
    for memory_id in sorted(identity_conflict_ids):
        members = sorted(
            locations_by_id[memory_id],
            key=lambda item: (
                item.path,
                item.storage,
                item.aggregate_index,
                item.section_start,
                item.revision,
            ),
        )
        source_occurrences = defaultdict(int)
        record_rows = []
        for item in members:
            occurrence_key = (item.path, item.storage, item.revision)
            occurrence = source_occurrences[occurrence_key]
            source_occurrences[occurrence_key] += 1
            record_rows.append(
                _identity_conflict_record(
                    item,
                    cfg["vault_path"],
                    occurrence,
                )
            )
        conflict = {
            "id": memory_id,
            "record_count": len(members),
            "records": record_rows,
        }
        conflict.update(
            _recommend_identity_conflict_resolution(
                conflict,
                used_memory_ids=used_memory_ids,
            )
        )
        identity_conflicts.append(conflict)
    unique_locations = [
        item for item in locations if item.memory_id not in identity_conflict_ids
    ]
    by_id = {item.memory_id: item for item in unique_locations}
    active_counts = Counter(item.memory_type for item in locations)
    quality_counts = defaultdict(Counter)
    low_quality = []

    for location in locations:
        assessment = assess_formal_location(location)
        quality_counts[location.memory_type][assessment.status] += 1
        if assessment.status == QUALITY_FORMAL:
            continue
        low_quality.append(
            {
                "id": location.memory_id,
                "expected_revision": location.revision,
                "type": location.memory_type,
                "project": location.project,
                "title": redact_sensitive(location.title),
                "summary": redact_sensitive(location.summary),
                "quality_status": assessment.status,
                "quality_score": assessment.score,
                "quality_reasons": list(assessment.reasons),
                "path": _vault_relative(location.path, cfg["vault_path"]),
                "identity_conflict": location.memory_id in identity_conflict_ids,
            }
        )

    _collapsed, raw_duplicate_groups = collapse_runtime_duplicates(
        [item.record for item in unique_locations]
    )
    duplicate_groups = []
    duplicate_member_ids = set()
    for group in raw_duplicate_groups:
        representative_id = group["representative_id"]
        representative = by_id.get(representative_id)
        members = [by_id[item] for item in group["member_ids"] if item in by_id]
        if representative is None or len(members) < 2:
            continue
        member_rows = [
            {
                "id": item.memory_id,
                "revision": item.revision,
                "title": redact_sensitive(item.title),
                "summary": redact_sensitive(item.summary),
                "path": _vault_relative(item.path, cfg["vault_path"]),
            }
            for item in members
        ]
        duplicate_groups.append(
            {
                "representative_id": representative_id,
                "representative_revision": representative.revision,
                "type": representative.memory_type,
                "project": representative.project,
                "reason": group["reason"],
                "member_ids": [item["id"] for item in member_rows],
                "members": member_rows,
            }
        )
        duplicate_member_ids.update(group["member_ids"])

    candidate_actions = []
    for group in duplicate_groups:
        for member in group["members"]:
            if member["id"] == group["representative_id"]:
                continue
            candidate_actions.append(
                {
                    "action": "supersede",
                    "memory_id": member["id"],
                    "expected_revision": member["revision"],
                    "replacement_id": group["representative_id"],
                    "replacement_revision": group["representative_revision"],
                    "reason": (
                        "历史质量审计识别为同一事实的近重复记录；"
                        f"保留 {group['representative_id']} 作为代表"
                    ),
                    "reason_codes": [group["reason"]],
                }
            )

    for item in low_quality:
        if (
            item["id"] in duplicate_member_ids
            or item["id"] in identity_conflict_ids
        ):
            continue
        strong_reasons = sorted(
            set(item["quality_reasons"]) & HIGH_CONFIDENCE_RETRACT_REASONS
        )
        if not strong_reasons:
            continue
        candidate_actions.append(
            {
                "action": "retract",
                "memory_id": item["id"],
                "expected_revision": item["expected_revision"],
                "replacement_id": "",
                "replacement_revision": "",
                "reason": (
                    "历史质量审计判定该记录不应作为长期正式记忆："
                    + ", ".join(strong_reasons)
                ),
                "reason_codes": strong_reasons,
            }
        )

    actions = []
    blocked_lifecycle_actions = []
    for action in candidate_actions:
        alias_owners = _unapproved_active_alias_owners(action, locations_by_id)
        if alias_owners:
            blocked_lifecycle_actions.append(
                {
                    **action,
                    "blocked_reason": "active_alias_owner",
                    "blocking_memory_ids": alias_owners,
                }
            )
        else:
            actions.append(action)
    actions.sort(
        key=lambda item: (
            item["action"],
            item["memory_id"],
            item.get("replacement_id", ""),
        )
    )
    duplicate_groups.sort(
        key=lambda item: (
            item["project"],
            item["type"],
            item["representative_id"],
        )
    )
    low_quality.sort(
        key=lambda item: (
            item["quality_status"],
            item["quality_score"],
            item["type"],
            item["id"],
        )
    )
    blocked_lifecycle_actions.sort(
        key=lambda item: (
            item["action"],
            item["memory_id"],
            item.get("replacement_id", ""),
        )
    )
    candidate_action_ids = {
        item["memory_id"] for item in candidate_actions
    }
    evidence_insufficient = [
        item
        for item in low_quality
        if item["id"] not in candidate_action_ids
        and item["id"] not in identity_conflict_ids
    ]
    evidence_insufficient_by_type = Counter(
        item["type"] for item in evidence_insufficient
    )
    evidence_insufficient_by_status = Counter(
        item["quality_status"] for item in evidence_insufficient
    )
    evidence_insufficient_by_reason = Counter(
        reason
        for item in evidence_insufficient
        for reason in item["quality_reasons"]
    )
    return {
        "schema_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "active_record_count": len(locations),
        "active_counts": dict(sorted(active_counts.items())),
        "quality_counts": {
            memory_type: dict(sorted(counts.items()))
            for memory_type, counts in sorted(quality_counts.items())
        },
        "low_quality_count": len(low_quality),
        "low_quality": low_quality,
        "identity_conflict_count": len(identity_conflicts),
        "identity_conflicts": identity_conflicts,
        "duplicate_group_count": len(duplicate_groups),
        "duplicate_groups": duplicate_groups,
        "evidence_insufficient_count": len(evidence_insufficient),
        "evidence_insufficient_breakdown": {
            "by_type": dict(sorted(evidence_insufficient_by_type.items())),
            "by_quality_status": dict(
                sorted(evidence_insufficient_by_status.items())
            ),
            "by_reason": dict(
                sorted(evidence_insufficient_by_reason.items())
            ),
        },
        "blocked_lifecycle_action_count": len(blocked_lifecycle_actions),
        "blocked_lifecycle_actions": blocked_lifecycle_actions,
        "blocked_lifecycle_reason_counts": {
            "active_alias_owner": len(blocked_lifecycle_actions)
        }
        if blocked_lifecycle_actions
        else {},
        "executable_recommendation_count": len(actions),
        "recommended_action_count": len(actions),
        "recommended_actions": actions,
    }


def assess_formal_location(location):
    if location.memory_type == "decision":
        return assess_decision(
            {"text": location.title, "context": location.summary}
        )
    if location.memory_type == "error":
        return assess_error(
            {"type": location.title, "resolution": location.summary}
        )
    if location.memory_type in {"preference", "project_rule", "environment"}:
        return assess_favor(
            {
                "content": location.summary,
                "context": location.title,
                "type": location.memory_type,
            }
        )
    return _AlwaysFormalAssessment()


class _AlwaysFormalAssessment:
    status = QUALITY_FORMAL
    score = 1.0
    reasons = ()


def create_quality_proposals(cfg, report, *, reconcile=True):
    """Create idempotent pending proposals; never apply lifecycle changes."""
    if reconcile not in {True, False, "selected"}:
        raise ValueError("proposal reconciliation must be true, false, or selected")
    actions = list(report.get("recommended_actions") or [])
    _preflight_quality_proposals(cfg, actions)
    vault = os.path.abspath(os.path.expanduser(str(cfg.get("vault_path") or "")))
    lock_path = safe_vault_path(vault, "04-Feedback", "_logs", "harvester.lock")
    ensure_directory_tree(os.path.dirname(lock_path), vault)
    with exclusive_file_lock(lock_path, root=vault):
        # Recheck under the shared Vault lock to close the preflight/write race.
        records_by_id = _preflight_quality_proposals(cfg, actions)
        paths = []
        for action in actions:
            evidence = list(
                dict.fromkeys(
                    [
                        "memory-quality-audit:" + code
                        for code in action.get("reason_codes") or []
                    ]
                    + [
                        str(ref).strip()
                        for ref in action.get("evidence_refs") or []
                        if str(ref).strip()
                    ]
                )
            )
            path = create_proposal(
                cfg,
                action=action["action"],
                memory_id=action["memory_id"],
                replacement_id=action.get("replacement_id", ""),
                expected_revision=action.get("expected_revision", ""),
                replacement_revision=action.get("replacement_revision", ""),
                reason=action["reason"],
                evidence_refs=evidence,
                _record_snapshot=records_by_id,
            )
            paths.append(path)
        paths = list(dict.fromkeys(paths))
        if reconcile:
            _reconcile_quality_audit_proposals(
                cfg,
                paths,
                stale_at=report.get("generated_at", ""),
                memory_ids=(
                    {
                        str(action.get("memory_id") or "").strip()
                        for action in actions
                    }
                    if reconcile == "selected"
                    else None
                ),
            )
        return paths


def _preflight_quality_proposals(cfg, actions, records_by_id=None):
    """Validate the complete audit batch before any proposal file is changed."""
    if records_by_id is None:
        records_by_id = defaultdict(list)
        for location in find_records(cfg):
            records_by_id[location.memory_id].append(location)

    for action in actions:
        action_name = str(action.get("action") or "").strip().lower()
        if action_name not in {"retract", "supersede"}:
            raise ValueError(f"unsupported quality audit action: {action_name}")
        memory_id = str(action.get("memory_id") or "").strip()
        location = _unique_preflight_location(records_by_id, memory_id)
        expected_revision = str(action.get("expected_revision") or "").strip()
        if expected_revision and expected_revision != location.revision:
            raise LifecycleConflict(
                f"stale revision for {location.memory_id}: expected "
                f"{expected_revision}, current {location.revision}"
            )
        replacement_id = str(action.get("replacement_id") or "").strip()
        replacement_revision = str(
            action.get("replacement_revision") or ""
        ).strip()
        if action_name == "supersede":
            replacement = _unique_preflight_location(
                records_by_id,
                replacement_id,
            )
            if replacement_revision and replacement_revision != replacement.revision:
                raise LifecycleConflict(
                    f"stale replacement revision for {replacement.memory_id}: expected "
                    f"{replacement_revision}, current {replacement.revision}"
                )
        elif replacement_id or replacement_revision:
            raise ValueError(
                "replacement_id and replacement_revision are valid only for supersede proposals"
            )
        if not " ".join(str(redact_sensitive(action.get("reason", ""))).split()):
            raise ValueError("proposal reason is required")
        if location.status != "active":
            raise LifecycleConflict(
                f"{action_name} requires an active memory, got {location.status}"
            )
        alias_owners = _unapproved_active_alias_owners(action, records_by_id)
        if alias_owners:
            qualifier = "unapproved " if action_name == "supersede" else ""
            raise LifecycleConflict(
                f"{qualifier}active alias owner for {action_name} target "
                f"{memory_id}: {', '.join(alias_owners)}"
            )
        if action_name == "supersede":
            _preflight_supersede_snapshot(
                records_by_id,
                location,
                replacement,
            )
    return records_by_id


def _unapproved_active_alias_owners(action, records_by_id):
    """Return active records that would keep an unapproved target alias alive."""
    memory_id = str(action.get("memory_id") or "").strip()
    owners = {
        location.memory_id
        for members in records_by_id.values()
        for location in members
        if location.status == "active"
        and memory_id in (location.record.get("aliases") or [])
    }
    if str(action.get("action") or "").strip().lower() == "supersede":
        owners.discard(str(action.get("replacement_id") or "").strip())
    return sorted(owner for owner in owners if owner)


def write_old_memory_lifecycle_plan(cfg, cutoff_exclusive, *, now=None):
    """Write a reproducible, read-only approval plan for dated old memory."""
    vault = os.path.abspath(os.path.expanduser(str(cfg.get("vault_path") or "")))
    cutoff = _parse_cutoff_date(cutoff_exclusive)
    timestamp = _plan_timestamp(now)
    lock_path = safe_vault_path(vault, "04-Feedback", "_logs", "harvester.lock")
    ensure_directory_tree(os.path.dirname(lock_path), vault)
    with exclusive_file_lock(lock_path, root=vault):
        report = audit_formal_memories(cfg)
        snapshot = _old_memory_lifecycle_snapshot(
            cfg,
            report,
            cutoff,
            generated_at=timestamp,
        )
        path = _old_memory_plan_path(cfg, cutoff)
        ensure_directory_tree(os.path.dirname(path), vault)
        durable_atomic_write(
            path,
            _render_old_memory_lifecycle_plan(snapshot),
            root=vault,
        )
    return {**snapshot, "path": path}


def _old_memory_lifecycle_snapshot(cfg, report, cutoff, *, generated_at):
    vault = os.path.abspath(os.path.expanduser(str(cfg.get("vault_path") or "")))
    records_by_id = defaultdict(list)
    active_locations = []
    undated_count = 0
    invalid_date_count = 0
    old_locations = []
    for location in find_records(cfg):
        records_by_id[location.memory_id].append(location)
        if location.status != "active":
            continue
        active_locations.append(location)
        raw_date = str(location.record.get("date") or "").strip()
        if not raw_date:
            undated_count += 1
            continue
        try:
            record_date = date.fromisoformat(raw_date)
        except ValueError:
            invalid_date_count += 1
            continue
        if record_date < cutoff:
            old_locations.append(location)

    old_ids = {location.memory_id for location in old_locations}
    selected_actions = [
        dict(action)
        for action in report.get("recommended_actions") or []
        if str(action.get("memory_id") or "") in old_ids
    ]
    _preflight_quality_proposals(
        cfg,
        selected_actions,
        records_by_id=records_by_id,
    )

    actions = [
        _freeze_old_memory_action(cfg, records_by_id, action)
        for action in selected_actions
    ]
    actions.sort(
        key=lambda item: (
            item["action"],
            item["memory_id"],
            item["replacement_id"],
        )
    )
    canonical_payload = {
        "schema_version": OLD_MEMORY_PLAN_SCHEMA_VERSION,
        "cutoff_exclusive": cutoff.isoformat(),
        "actions": actions,
    }
    canonical_sha256 = hashlib.sha256(
        json.dumps(
            canonical_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

    low_quality_ids = {
        location.memory_id
        for location in old_locations
        if assess_formal_location(location).status != QUALITY_FORMAL
    }
    action_ids = {item["memory_id"] for item in actions}
    return {
        "schema_version": OLD_MEMORY_PLAN_SCHEMA_VERSION,
        "generated_at": generated_at,
        "cutoff_exclusive": cutoff.isoformat(),
        "read_only": True,
        "approval_status": "pending" if actions else "not_required",
        "canonical_sha256": canonical_sha256,
        "active_record_count": len(active_locations),
        "undated_active_record_count": undated_count,
        "invalid_date_active_record_count": invalid_date_count,
        "old_active_record_count": len(old_locations),
        "old_quality_pass_count": len(old_locations) - len(low_quality_ids),
        "old_low_quality_count": len(low_quality_ids),
        "old_low_quality_action_count": len(low_quality_ids & action_ids),
        "old_low_quality_without_action_count": len(low_quality_ids - action_ids),
        "recommended_action_count": len(actions),
        "recommended_actions": actions,
    }


def _freeze_old_memory_action(cfg, records_by_id, action):
    vault = os.path.abspath(os.path.expanduser(str(cfg.get("vault_path") or "")))
    location = _unique_preflight_location(
        records_by_id,
        str(action.get("memory_id") or "").strip(),
    )
    replacement_id = str(action.get("replacement_id") or "").strip()
    replacement = (
        _unique_preflight_location(records_by_id, replacement_id)
        if replacement_id
        else None
    )
    reason_codes = sorted(
        {
            str(code).strip()
            for code in action.get("reason_codes") or []
            if str(code).strip()
        }
    )
    source_refs = [
        redact_sensitive(ref)
        for ref in location.record.get("source_refs") or []
        if str(ref).strip()
    ]
    evidence_refs = list(
        dict.fromkeys(
            ["memory-quality-audit:" + code for code in reason_codes]
            + source_refs
        )
    )
    return {
        "action": str(action.get("action") or "").strip(),
        "memory_id": location.memory_id,
        "expected_revision": location.revision,
        "type": location.memory_type,
        "project": location.project,
        "scope": location.scope,
        "date": str(location.record.get("date") or ""),
        "title": redact_sensitive(location.title),
        "summary": redact_sensitive(location.summary),
        "source_path": _vault_relative(location.path, vault),
        "source_locator": _approval_source_locator(location, vault),
        "source_digest": _approval_source_digest(location),
        "source_digest_scope": "canonical-record-v1",
        "replacement_id": replacement.memory_id if replacement else "",
        "replacement_revision": replacement.revision if replacement else "",
        "replacement_source_locator": (
            _approval_source_locator(replacement, vault) if replacement else ""
        ),
        "replacement_source_digest": (
            _approval_source_digest(replacement) if replacement else ""
        ),
        "replacement_source_digest_scope": (
            "canonical-record-v1" if replacement else ""
        ),
        "reason": " ".join(
            str(redact_sensitive(action.get("reason", ""))).split()
        ),
        "reason_codes": reason_codes,
        "evidence_refs": evidence_refs,
    }


def _approval_source_locator(location, vault):
    source = _vault_relative(location.path, vault)
    if location.storage == "aggregate" and location.aggregate_key:
        return f"{source}#{location.aggregate_key}[id={location.memory_id}]"
    if location.storage == "markdown":
        return f"{source}#section[id={location.memory_id}]"
    return f"{source}#id={location.memory_id}"


def _approval_source_digest(location):
    fields = (
        "id",
        "revision",
        "type",
        "status",
        "project",
        "scope",
        "title",
        "summary",
        "date",
        "source_refs",
        "aliases",
        "requires",
        "expires_at",
        "superseded_by",
        "retracted_reason",
        "expired_reason",
        *OPERATIONAL_MEMORY_FIELDS,
        *MEMORY_RELATION_FIELDS,
    )
    payload = {
        key: (
            list(location.record.get(key) or [])
            if key in {
                "source_refs",
                "aliases",
                "requires",
                *MEMORY_RELATION_FIELDS,
            }
            else str(location.record.get(key) or "")
        )
        for key in fields
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _old_memory_plan_path(cfg, cutoff):
    vault = os.path.abspath(os.path.expanduser(str(cfg.get("vault_path") or "")))
    lifecycle = cfg.get("memory_lifecycle") or {}
    directory = safe_vault_path(
        vault,
        lifecycle.get(
            "proposal_dir",
            "04-Feedback/_lifecycle-proposals",
        ),
    )
    return safe_vault_path(
        directory,
        f"old-memory-lifecycle-plan-before-{cutoff.isoformat()}.md",
    )


def _parse_cutoff_date(value):
    raw = str(value or "").strip()
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("old-memory cutoff must be an ISO date") from exc
    if parsed.isoformat() != raw:
        raise ValueError("old-memory cutoff must use YYYY-MM-DD")
    return parsed


def _plan_timestamp(now):
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("old-memory plan time must include a timezone")
    return value.astimezone(timezone.utc).isoformat()


def _render_old_memory_lifecycle_plan(snapshot):
    frontmatter = {
        "title": "Old Formal Memory Lifecycle Approval Plan",
        "summary_type": "old-memory-lifecycle-approval-plan",
        "generated_by": "memory_quality_audit.py",
        **snapshot,
        "actions": snapshot["recommended_actions"],
    }
    frontmatter.pop("recommended_actions", None)
    lines = [
        "# 旧正式记忆生命周期审批计划",
        "",
        "本计划只冻结可复算的建议操作，不会修改正式记忆。",
        "只有用户明确批准本文件的 Canonical SHA256 后，后续执行器才可预览对应操作。",
        "",
        "## 范围与统计",
        "",
        f"- 排他截止日: `{snapshot['cutoff_exclusive']}`",
        f"- 当前 active 正式记忆: `{snapshot['active_record_count']}`",
        f"- 截止日前 active 旧记忆: `{snapshot['old_active_record_count']}`",
        f"- 通过质量门: `{snapshot['old_quality_pass_count']}`",
        f"- 低质量待复核: `{snapshot['old_low_quality_count']}`",
        f"- 低质量且已有保守建议: `{snapshot['old_low_quality_action_count']}`",
        f"- 证据不足、未生成操作: `{snapshot['old_low_quality_without_action_count']}`",
        f"- 全部建议操作: `{snapshot['recommended_action_count']}`",
        f"- 无日期 active 记录（不纳入旧记忆范围）: `{snapshot['undated_active_record_count']}`",
        f"- 日期无效 active 记录（不纳入旧记忆范围）: `{snapshot['invalid_date_active_record_count']}`",
        f"- Canonical SHA256: `{snapshot['canonical_sha256']}`",
        "",
        "## 精确建议",
        "",
    ]
    if not snapshot["recommended_actions"]:
        lines.append("当前范围没有达到保守生命周期阈值的建议操作。")
    for index, action in enumerate(snapshot["recommended_actions"], start=1):
        lines.extend(
            [
                f"### {index}. `{_table(action['action'])}` `{_table(action['memory_id'])}`",
                "",
                f"- ID: `{_table(action['memory_id'])}`",
                f"- Expected Revision: `{_table(action['expected_revision'])}`",
                f"- Type / Project / Scope: `{_table(action['type'])}` / `{_table(action['project'])}` / `{_table(action['scope'])}`",
                f"- Date: `{_table(action['date'])}`",
                f"- Source: [[{str(action['source_path']).removesuffix('.md')}|source]]",
                f"- Source Locator: `{_table(action['source_locator'])}`",
                f"- Record Source Digest: `{_table(action['source_digest'])}`",
                f"- Source Digest Scope: `{_table(action['source_digest_scope'])}`",
                f"- Replacement ID: `{_table(action['replacement_id']) or '-'}`",
                f"- Replacement Revision: `{_table(action['replacement_revision']) or '-'}`",
                f"- Replacement Source Locator: `{_table(action['replacement_source_locator']) or '-'}`",
                f"- Replacement Record Source Digest: `{_table(action['replacement_source_digest']) or '-'}`",
                f"- Replacement Source Digest Scope: `{_table(action['replacement_source_digest_scope']) or '-'}`",
                f"- 当前内容: {_table(action['title'])}",
                f"- 当前上下文: {_table(action['summary'])}",
                f"- Reason: {_table(action['reason'])}",
                f"- Reason Codes: `{_table(', '.join(action['reason_codes'])) or '-'}`",
                f"- Evidence Refs: `{_table(', '.join(action['evidence_refs'])) or '-'}`",
                "",
            ]
        )
    lines.extend(
        [
            "## 审批边界",
            "",
            "批准必须引用本文件中的 Canonical SHA256；任何 ID、Revision、稳定 Source Locator、单记录 Source Digest、替代项或理由漂移都需要重新生成计划。",
            "本文件本身不是正式记忆，也不会进入召回。",
        ]
    )
    return (
        "---\n"
        + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + "\n".join(lines).rstrip()
        + "\n"
    )


def _unique_preflight_location(records_by_id, memory_id):
    records = records_by_id.get(memory_id) or []
    if not records:
        raise LifecycleConflict(f"formal memory not found: {memory_id}")
    if len(records) != 1:
        raise LifecycleConflict(f"formal memory ID is not unique: {memory_id}")
    return records[0]


def _preflight_supersede_snapshot(records_by_id, location, replacement):
    if not replacement.memory_id or replacement.memory_id == location.memory_id:
        raise LifecycleConflict("supersede requires a distinct replacement ID")
    if replacement.status != "active":
        raise LifecycleConflict("replacement memory must be active")
    if (
        replacement.memory_type != location.memory_type
        or replacement.scope != location.scope
        or replacement.project != location.project
    ):
        raise LifecycleConflict(
            "replacement must have the same type, scope, and project"
        )
    if location.memory_id in (replacement.record.get("requires") or []):
        raise LifecycleConflict("replacement cannot require the superseded memory")

    active_records = [
        item.record
        for members in records_by_id.values()
        for item in members
        if item.status == "active" and item.memory_id != location.memory_id
    ]
    eligible, _suppressed = suppress_unmet_dependencies(active_records)
    eligible_ids = {str(item.get("id") or "") for item in eligible}
    if replacement.memory_id not in eligible_ids:
        raise LifecycleConflict(
            "replacement memory would be dependency-suppressed after supersession"
        )


def _reconcile_quality_audit_proposals(
    cfg,
    active_paths,
    stale_at="",
    memory_ids=None,
):
    """Mark superseded audit proposals stale without deleting their evidence."""
    vault = os.path.abspath(os.path.expanduser(str(cfg.get("vault_path") or "")))
    lifecycle = cfg.get("memory_lifecycle") or {}
    directory = safe_vault_path(
        vault,
        lifecycle.get(
            "proposal_dir",
            "04-Feedback/_lifecycle-proposals",
        ),
    )
    if not os.path.isdir(directory):
        return
    active = {os.path.abspath(path) for path in active_paths}
    selected_ids = (
        {
            str(memory_id).strip()
            for memory_id in memory_ids
            if str(memory_id).strip()
        }
        if memory_ids is not None
        else None
    )
    _directories, filenames = secure_list_directory(directory, vault)
    for filename in filenames:
        if not filename.endswith(".md"):
            continue
        path = safe_vault_path(directory, filename)
        if os.path.abspath(path) in active:
            continue
        data = secure_read_bytes(path, MAX_PROPOSAL_BYTES, root=vault)
        if len(data) > MAX_PROPOSAL_BYTES:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        frontmatter_text, body = split_frontmatter_text(text)
        if frontmatter_text is None:
            continue
        try:
            frontmatter = yaml.safe_load(frontmatter_text) or {}
        except yaml.YAMLError:
            continue
        if not isinstance(frontmatter, dict):
            continue
        if (
            selected_ids is not None
            and str(frontmatter.get("memory_id") or "").strip()
            not in selected_ids
        ):
            continue
        evidence_refs = frontmatter.get("evidence_refs") or []
        if (
            frontmatter.get("summary_type") != "lifecycle-proposal"
            or frontmatter.get("status") != "pending"
            or not isinstance(evidence_refs, list)
            or not any(
                str(ref).startswith("memory-quality-audit:")
                for ref in evidence_refs
            )
        ):
            continue
        frontmatter["status"] = "stale"
        frontmatter["stale_at"] = (
            str(stale_at or "").strip()
            or datetime.now(timezone.utc).isoformat()
        )
        frontmatter["stale_reason"] = (
            "not_recommended_by_latest_memory_quality_audit"
        )
        stale_body = str(body or "").replace(
            "# 待确认:",
            "# 已过期提案:",
            1,
        ).replace(
            "该文件只是待确认提案，不会进入正式召回。",
            "该提案已被最新质量审计标记为过期，不能作为当前批准依据。",
            1,
        )
        content = (
            "---\n"
            + yaml.safe_dump(
                frontmatter,
                allow_unicode=True,
                sort_keys=False,
            )
            + "---\n"
            + stale_body
        )
        durable_atomic_write(path, content, root=vault)


def _identity_conflict_record(location, vault, occurrence):
    return {
        "revision": location.revision,
        "status": location.status,
        "type": location.memory_type,
        "project": location.project,
        "scope": location.scope,
        "title": redact_sensitive(location.title),
        "summary": redact_sensitive(location.summary),
        "path": _vault_relative(location.path, vault),
        "source_locator": _memory_source_locator(location, vault),
        "source_identity": _memory_source_identity(
            location,
            vault,
            occurrence,
        ),
        "source_digest": location.source_digest,
        "storage": location.storage,
        "source_refs": [
            redact_sensitive(ref)
            for ref in location.record.get("source_refs") or []
        ],
        "requires": list(location.record.get("requires") or []),
        "expires_at": str(location.record.get("expires_at") or ""),
        "superseded_by": str(location.record.get("superseded_by") or ""),
        "operational": {
            key: redact_sensitive(location.record.get(key, ""))
            for key in OPERATIONAL_MEMORY_FIELDS
            if str(location.record.get(key) or "").strip()
        },
    }


def _memory_source_locator(location, vault):
    source = _vault_relative(location.path, vault)
    if location.storage == "aggregate" and location.aggregate_index >= 0:
        return f"{source}#{location.aggregate_key}[{location.aggregate_index}]"
    if location.storage == "markdown" and location.section_start >= 0:
        return f"{source}#section@{location.section_start}"
    return source


def _memory_source_identity(location, vault, occurrence):
    source = _vault_relative(location.path, vault)
    return (
        f"{source}#{location.memory_type}:{location.revision}:"
        f"occurrence[{occurrence}]"
    )


def _recommend_identity_conflict_resolution(conflict, used_memory_ids):
    """Build a deterministic, read-only repair recommendation for one ID."""
    records = list(conflict.get("records") or [])
    fact_signatures = {_conflict_fact_signature(item) for item in records}
    placeholders = bool(records) and all(
        _looks_like_placeholder_record(item) for item in records
    )
    if placeholders:
        fact_relation = "low_quality_placeholder"
    elif (
        len(fact_signatures) == 1
        and _same_fact_can_cross_projects(records)
    ):
        fact_relation = "exact_duplicate"
    else:
        fact_relation = "distinct_facts"

    owner, owner_basis = _select_conflict_owner(records, fact_relation)
    confidence = _conflict_confidence(fact_relation, owner_basis)
    merge_refs = fact_relation == "exact_duplicate"
    owner_refs = set(owner.get("source_refs") or [])
    if merge_refs:
        for record in records:
            owner_refs.update(record.get("source_refs") or [])

    owner_target_project = owner.get("project") or _project_from_memory_path(
        owner.get("path", "")
    )
    owner_target_source = _canonical_formal_source(
        owner_target_project,
        owner.get("type", ""),
        fallback=owner.get("path", ""),
    )
    if fact_relation == "low_quality_placeholder":
        owner_action = (
            "retain_id_then_retract"
            if owner.get("status") == "active"
            else "retain_id_and_keep_inactive"
        )
        owner_resulting_status = (
            "retracted"
            if owner.get("status") == "active"
            else owner.get("status", "")
        )
    else:
        owner_action = "retain_id_and_keep"
        owner_resulting_status = owner.get("status", "")
    owner_summary = {
        "retained_id": conflict.get("id", ""),
        "source": owner.get("path", ""),
        "source_locator": owner.get("source_locator", ""),
        "source_identity": owner.get("source_identity", ""),
        "source_digest": owner.get("source_digest", ""),
        "revision": owner.get("revision", ""),
        "status": owner.get("status", ""),
        "action": owner_action,
        "resulting_status": owner_resulting_status,
        "type": owner.get("type", ""),
        "project": owner.get("project", ""),
        "target_project": owner_target_project,
        "target_source": owner_target_source,
        "relocate": owner.get("path", "") != owner_target_source,
        "title": owner.get("title", ""),
        "source_refs_after_merge": sorted(owner_refs),
    }
    owner_key = owner.get("source_locator")
    actions = []
    for record in records:
        if record.get("source_locator") == owner_key:
            continue
        proposed_id = _proposed_conflict_rekey_id(
            conflict.get("id", ""),
            record,
            used_memory_ids,
        )
        action, resulting_status = _conflict_member_action(
            record,
            fact_relation,
        )
        if fact_relation == "exact_duplicate":
            target_project = owner_target_project
            target_source = owner_target_source
        else:
            target_project = record.get("project") or _project_from_memory_path(
                record.get("path", "")
            )
            target_source = _canonical_formal_source(
                target_project,
                record.get("type", ""),
                fallback=record.get("path", ""),
            )
        actions.append(
            {
                "source": record.get("path", ""),
                "source_locator": record.get("source_locator", ""),
                "source_identity": record.get("source_identity", ""),
                "source_digest": record.get("source_digest", ""),
                "revision": record.get("revision", ""),
                "current_status": record.get("status", ""),
                "action": action,
                "proposed_id": proposed_id,
                "replacement_id": (
                    conflict.get("id", "")
                    if action == "rekey_then_supersede"
                    else ""
                ),
                "target_project": target_project,
                "target_source": target_source,
                "resulting_status": resulting_status,
                "relocate": record.get("path", "") != target_source,
                "preserve_source_refs": True,
                "merge_source_refs_into_owner": merge_refs,
                "reason": _conflict_action_reason(action, conflict.get("id", "")),
            }
        )

    return {
        "fact_relation": fact_relation,
        "recommended_owner": owner_summary,
        "recommended_actions": actions,
        "confidence": confidence,
        "reason": _conflict_recommendation_reason(
            fact_relation,
            owner_basis,
            owner,
        ),
        "approval_status": "pending",
        "approval_preconditions": [
            "source_locator",
            "revision",
            "source_digest",
        ],
    }


def _conflict_fact_signature(record):
    return (
        str(record.get("type") or ""),
        str(record.get("scope") or ""),
        normalize_fact_text(record.get("title")),
        normalize_fact_text(record.get("summary")),
        tuple(sorted(str(item) for item in record.get("requires") or [])),
        str(record.get("expires_at") or ""),
        str(record.get("superseded_by") or ""),
        tuple(
            (
                key,
                normalize_fact_text((record.get("operational") or {}).get(key)),
            )
            for key in OPERATIONAL_MEMORY_FIELDS
        ),
    )


def _same_fact_can_cross_projects(records):
    projects = {str(item.get("project") or "") for item in records}
    if len(projects) <= 1:
        return True
    concrete = {
        project
        for project in projects
        if project and project != ROUTING_HOST_PROJECT
    }
    return (
        ROUTING_HOST_PROJECT in projects
        and len(concrete) == 1
        and projects == ({ROUTING_HOST_PROJECT} | concrete)
    )


def _looks_like_placeholder_record(record):
    return (
        normalize_fact_text(record.get("title")) in PLACEHOLDER_TITLES
        and normalize_fact_text(record.get("summary")) in PLACEHOLDER_SUMMARIES
    )


def _select_conflict_owner(records, fact_relation):
    if not records:
        return {}, "no_record"
    pool = list(records)
    meaningful = [item for item in pool if not _looks_like_placeholder_record(item)]
    if meaningful:
        pool = meaningful
    if fact_relation == "low_quality_placeholder":
        inactive = [item for item in pool if item.get("status") != "active"]
        if inactive:
            pool = inactive
    else:
        active = [item for item in pool if item.get("status") == "active"]
        if len(active) == 1:
            return active[0], "single_active_record"
        if active:
            pool = active
    canonical = [
        item
        for item in pool
        if _project_from_memory_path(item.get("path", ""))
        == str(item.get("project") or "")
    ]
    if len(canonical) == 1:
        return canonical[0], "single_canonical_path"
    if canonical:
        pool = canonical
    specific_projects = [
        item
        for item in pool
        if str(item.get("project") or "")
        and str(item.get("project") or "") != ROUTING_HOST_PROJECT
    ]
    if len(specific_projects) == 1:
        return specific_projects[0], "specific_project_preferred"
    if specific_projects:
        pool = specific_projects
    active = [item for item in pool if item.get("status") == "active"]
    if len(active) == 1:
        return active[0], "single_active_record"
    if active:
        pool = active
    return sorted(
        pool,
        key=lambda item: (
            item.get("source_locator", ""),
            item.get("revision", ""),
        ),
    )[0], "deterministic_fallback"


def _project_from_memory_path(path):
    parts = str(path or "").replace("\\", "/").split("/")
    try:
        index = parts.index("01-Projects")
    except ValueError:
        return ""
    return parts[index + 1] if index + 1 < len(parts) else ""


def _canonical_formal_source(project, memory_type, fallback=""):
    project = str(project or "").strip()
    filename = {
        "decision": "decisions.md",
        "error": "pitfalls.md",
    }.get(str(memory_type or ""))
    if project and filename:
        return f"01-Projects/{project}/Memory/{filename}"
    return str(fallback or "")


def _proposed_conflict_rekey_id(memory_id, record, used_memory_ids):
    prefix = str(record.get("type") or "memory").strip() or "memory"
    for nonce in range(1000):
        material = "\x1f".join(
            [
                "identity-conflict-repair-v1",
                str(memory_id or ""),
                str(record.get("source_identity") or record.get("path") or ""),
                str(record.get("revision") or ""),
                str(nonce),
            ]
        )
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
        candidate = f"{prefix}-{digest}"
        if candidate not in used_memory_ids:
            used_memory_ids.add(candidate)
            return candidate
    raise RuntimeError("unable to allocate a unique conflict repair ID")


def _conflict_member_action(record, fact_relation):
    status = str(record.get("status") or "")
    if _looks_like_placeholder_record(record):
        if status == "active":
            return "rekey_then_retract", "retracted"
        return "rekey_and_retain_inactive", status
    if fact_relation == "low_quality_placeholder":
        if status == "active":
            return "rekey_then_retract", "retracted"
        return "rekey_and_retain_inactive", status
    if fact_relation == "exact_duplicate":
        if status == "active":
            return "rekey_then_supersede", "superseded"
        return "rekey_and_retain_inactive", status
    if status == "active":
        return "rekey_and_keep", "active"
    return "rekey_and_retain_inactive", status


def _conflict_confidence(fact_relation, owner_basis):
    if fact_relation == "low_quality_placeholder":
        return "low"
    if owner_basis == "single_canonical_path":
        return "high"
    if owner_basis in {"specific_project_preferred", "single_active_record"}:
        return "medium"
    return "low"


def _conflict_recommendation_reason(fact_relation, owner_basis, owner):
    source = str(owner.get("path") or "")
    if fact_relation == "low_quality_placeholder":
        if owner.get("status") == "active":
            return (
                "各来源都是无可复用语义的 active 占位内容；建议由 "
                f"{source} 保留原 ID 后转为 retracted，其余来源重新编号后也转为 inactive。"
            )
        return (
            "各来源都是无可复用语义的占位内容；建议由具体项目中的 inactive "
            f"来源 {source} 保留原 ID，其余来源重新编号后维持或转为 inactive。"
        )
    if fact_relation == "exact_duplicate":
        if owner_basis == "single_canonical_path":
            return (
                "各来源表达同一事实，且该来源的物理路径与 project 元数据唯一一致；"
                f"建议由 {source} 保留原 ID，并合并全部 source_refs。"
            )
        if owner_basis == "single_active_record":
            return (
                "各来源表达同一事实，但只有该来源仍为 active；"
                f"建议由 {source} 保留原 ID，必要时迁入 canonical 项目路径，"
                "并保持其他 inactive 证据不被重新激活。"
            )
        return (
            "各来源表达同一事实；具体业务项目比记忆系统的路由宿主更适合作为事实所有者，"
            f"建议由 {source} 保留原 ID，并合并全部 source_refs。"
        )
    if owner_basis == "single_canonical_path":
        return (
            "同一 ID 下是不同且可保留的事实；该来源的物理路径与 project 元数据唯一一致，"
            f"建议由 {source} 保留原 ID，其余事实重新编号并保留 active。"
        )
    return (
        "同一 ID 下是不同事实，但没有唯一的强结构证据；当前 owner 仅按保守确定性规则选择，"
        "批准前应人工复核。"
    )


def _conflict_action_reason(action, retained_id):
    if action == "rekey_then_supersede":
        return f"与保留项是同一事实；重新编号后 supersede 到 {retained_id}"
    if action == "rekey_and_keep":
        return "内容是独立有效事实；重新编号后继续保持 active"
    if action == "rekey_then_retract":
        return "内容是低质量占位符；重新编号后转为 retracted"
    return "保留当前 inactive 语义，仅重新编号以解除身份冲突"


def _conflict_plan_path(cfg):
    vault = os.path.abspath(os.path.expanduser(str(cfg.get("vault_path") or "")))
    return safe_vault_path(vault, DEFAULT_CONFLICT_PLAN_PATH)


def _conflict_guidance(conflict):
    recommendation = str(conflict.get("reason") or "").strip()
    if recommendation:
        return recommendation
    records = list(conflict.get("records") or [])
    active = [item for item in records if item.get("status") == "active"]
    signatures = {
        (
            item.get("status"),
            item.get("type"),
            item.get("project"),
            item.get("title"),
            item.get("summary"),
        )
        for item in records
    }
    if len(active) == 1:
        return (
            "候选保留唯一 active 记录，但仍需确认它的项目和来源路径；"
            "其他记录只能在确认身份后再提交 lifecycle 操作。"
        )
    if not active:
        return (
            "当前没有 active 记录；先确认哪一个历史记录应恢复为正式身份，"
            "不要直接创建新的同名 ID。"
        )
    if len(signatures) == 1:
        return (
            "所有副本的状态、项目和内容一致；优先确认唯一 canonical 来源路径，"
            "不要仅凭路径自动迁移。"
        )
    return (
        "存在多个 active 记录且内容或项目不同，必须人工选择 canonical 来源；"
        "在选择前不生成 supersede/retract 动作。"
    )


def write_identity_conflict_plan(cfg, report):
    """Write a read-only, source-specific review plan for duplicate formal IDs."""
    vault = os.path.abspath(os.path.expanduser(str(cfg.get("vault_path") or "")))
    path = _conflict_plan_path(cfg)
    ensure_directory_tree(os.path.dirname(path), vault)
    conflicts = list(report.get("identity_conflicts") or [])
    frontmatter = {
        "title": "Formal Memory Identity Conflict Plan",
        "summary_type": "memory-identity-conflict-plan",
        "generated_by": "memory_quality_audit.py",
        "schema_version": report.get("schema_version", "1.0"),
        "generated_at": report.get("generated_at", ""),
        "conflict_count": len(conflicts),
        "read_only": True,
        "recommendation_schema_version": "1.0",
        "approval_status": "pending" if conflicts else "not_required",
    }
    lines = [
        "# 正式记忆身份冲突修复计划",
        "",
        "本文件是只读人工复核清单，不会改变正式记忆、revision、状态或证据历史。",
        "同一个 ID 出现多个来源时，必须先确认 canonical 来源路径，再由用户明确批准精确的 lifecycle 操作。",
        "",
        f"- 冲突组: `{len(conflicts)}`",
        f"- 生成时间: `{report.get('generated_at', '')}`",
        "- 当前状态: `仅供复核`",
        "",
    ]
    if not conflicts:
        lines.append("当前没有发现正式记忆身份冲突。")
    for index, conflict in enumerate(conflicts, start=1):
        memory_id = _table(conflict.get("id", ""))
        lines.extend(
            [
                f"## {index}. `{memory_id}`",
                "",
                f"- 记录数: `{conflict.get('record_count', 0)}`",
                f"- 复核建议: {_conflict_guidance(conflict)}",
                "",
                "| # | Status | Type | Scope | Project | Revision | Source | Locator | Title | Summary |",
                "|---:|---|---|---|---|---|---|---|---|---|",
            ]
        )
        for member_index, record in enumerate(conflict.get("records") or [], start=1):
            source = str(record.get("path") or "").removesuffix(".md")
            lines.append(
                "| {index} | `{status}` | `{kind}` | `{scope}` | {project} | `{revision}` | "
                "[[{source}|source]] | `{locator}` | {title} | {summary} |".format(
                    index=member_index,
                    status=_table(record.get("status", "")),
                    kind=_table(record.get("type", "")),
                    scope=_table(record.get("scope", "")),
                    project=_table(record.get("project", "")),
                    revision=_table(record.get("revision", "")),
                    source=source,
                    locator=_table(record.get("source_locator", "")),
                    title=_table(record.get("title", "")),
                    summary=_table(record.get("summary", "")),
                )
            )
        lines.extend(["", "### 来源语义与证据", ""])
        for member_index, record in enumerate(
            conflict.get("records") or [],
            start=1,
        ):
            operational = json.dumps(
                record.get("operational") or {},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            lines.extend(
                [
                    f"#### Source {member_index}",
                    "",
                    f"- Source Locator: `{_table(record.get('source_locator', ''))}`",
                    f"- Source Identity: `{_table(record.get('source_identity', ''))}`",
                    f"- Source Digest: `{_table(record.get('source_digest', ''))}`",
                    f"- Scope: `{_table(record.get('scope', ''))}`",
                    "- Requires: `{value}`".format(
                        value=_table(", ".join(record.get("requires") or [])) or "-",
                    ),
                    f"- Expires At: `{_table(record.get('expires_at', '')) or '-'}`",
                    f"- Superseded By: `{_table(record.get('superseded_by', '')) or '-'}`",
                    f"- Operational: `{_table(operational)}`",
                    "- Source Refs: `{value}`".format(
                        value=_table(", ".join(record.get("source_refs") or [])) or "-",
                    ),
                    "",
                ]
            )
        owner = conflict.get("recommended_owner") or {}
        owner_source = str(owner.get("source") or "").removesuffix(".md")
        lines.extend(
            [
                "",
                "### 推荐方案",
                "",
                f"- 事实关系: `{_table(conflict.get('fact_relation', ''))}`",
                f"- 置信度: `{_table(conflict.get('confidence', ''))}`",
                f"- 审批状态: `{_table(conflict.get('approval_status', 'pending'))}`",
                "- 保留原 ID: `{memory_id}`".format(
                    memory_id=_table(conflict.get("id", "")),
                ),
                "- 推荐 Owner: [[{source}|source]] + `{revision}`（`{status}` / {project}）".format(
                    source=owner_source,
                    revision=_table(owner.get("revision", "")),
                    status=_table(owner.get("status", "")),
                    project=_table(owner.get("project", "")),
                ),
                f"- Owner Locator: `{_table(owner.get('source_locator', ''))}`",
                f"- Owner Source Digest: `{_table(owner.get('source_digest', ''))}`",
                f"- Owner Action: `{_table(owner.get('action', ''))}`",
                f"- Owner Resulting Status: `{_table(owner.get('resulting_status', ''))}`",
                "- Owner Target: {project} / [[{source}|target]]".format(
                    project=_table(owner.get("target_project", "")),
                    source=str(owner.get("target_source") or "").removesuffix(".md"),
                ),
                "- Owner Relocate: `{value}`".format(
                    value="yes" if owner.get("relocate") else "no",
                ),
                "- Owner Source Refs After Merge: `{value}`".format(
                    value=(
                        _table(", ".join(owner.get("source_refs_after_merge") or []))
                        or "-"
                    ),
                ),
                f"- 理由: {_table(conflict.get('reason', ''))}",
                "",
                "### 建议动作",
                "",
            ]
        )
        for action_index, action in enumerate(
            conflict.get("recommended_actions") or [],
            start=1,
        ):
            action_source = str(action.get("source") or "").removesuffix(".md")
            target_source = str(action.get("target_source") or "").removesuffix(".md")
            lines.extend(
                [
                    f"#### Action {action_index}",
                    "",
                    f"- Source: [[{action_source}|source]]",
                    f"- Source Locator: `{_table(action.get('source_locator', ''))}`",
                    f"- Source Identity: `{_table(action.get('source_identity', ''))}`",
                    f"- Source Digest: `{_table(action.get('source_digest', ''))}`",
                    f"- Expected Revision: `{_table(action.get('revision', ''))}`",
                    f"- Current Status: `{_table(action.get('current_status', ''))}`",
                    f"- Action: `{_table(action.get('action', ''))}`",
                    f"- Proposed ID: `{_table(action.get('proposed_id', ''))}`",
                    f"- Replacement ID: `{_table(action.get('replacement_id', '')) or '-'}`",
                    f"- Target Project: `{_table(action.get('target_project', ''))}`",
                    f"- Target Source: [[{target_source}|target]]",
                    "- Relocate: `{value}`".format(
                        value="yes" if action.get("relocate") else "no",
                    ),
                    f"- Resulting Status: `{_table(action.get('resulting_status', ''))}`",
                    "- Preserve Source Refs: `{value}`".format(
                        value="yes" if action.get("preserve_source_refs") else "no",
                    ),
                    "- Merge Source Refs Into Owner: `{value}`".format(
                        value=(
                            "yes"
                            if action.get("merge_source_refs_into_owner")
                            else "no"
                        ),
                    ),
                    f"- Reason: {_table(action.get('reason', ''))}",
                    "",
                ]
            )
        lines.extend(
            [
                "**需要用户确认：** 按 `ID + Source + Revision + Source Digest` 批准上述推荐方案；同文件同 revision 时同时给出 `Source Locator`。",
                "在此之前不执行任何正式记忆变更。",
                "",
            ]
        )
    content = (
        "---\n"
        + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + "\n".join(lines).rstrip()
        + "\n"
    )
    durable_atomic_write(path, content, root=vault)
    return path


def write_quality_report(cfg, report):
    vault = os.path.abspath(os.path.expanduser(str(cfg.get("vault_path") or "")))
    settings = cfg.get("annotation_quality") or {}
    conflict_plan_path = write_identity_conflict_plan(cfg, report)
    path = safe_vault_path(
        vault,
        settings.get("report_path", DEFAULT_REPORT_PATH),
    )
    ensure_directory_tree(os.path.dirname(path), vault)
    frontmatter = {
        "title": "Memory Quality Report",
        "summary_type": "memory-quality-report",
        "generated_by": "memory_quality_audit.py",
        "schema_version": report.get("schema_version", "1.0"),
        "generated_at": report.get("generated_at", ""),
        "active_record_count": report.get("active_record_count", 0),
        "low_quality_count": report.get("low_quality_count", 0),
        "identity_conflict_count": report.get("identity_conflict_count", 0),
        "identity_conflict_plan": _vault_relative(conflict_plan_path, vault),
        "duplicate_group_count": report.get("duplicate_group_count", 0),
        "evidence_insufficient_count": report.get(
            "evidence_insufficient_count", 0
        ),
        "blocked_lifecycle_action_count": report.get(
            "blocked_lifecycle_action_count", 0
        ),
        "executable_recommendation_count": report.get(
            "executable_recommendation_count",
            report.get("recommended_action_count", 0),
        ),
        "recommended_action_count": report.get("recommended_action_count", 0),
    }
    lines = [
        "# 记忆质量报告",
        "",
        "本报告只审计正式记忆并生成待确认建议，不会直接改变记忆状态。",
        "",
        "## 概览",
        "",
        f"- 正式 active 记录: `{report.get('active_record_count', 0)}`",
        f"- 待复核低质量记录: `{report.get('low_quality_count', 0)}`",
        f"- 正式 ID 身份冲突: `{report.get('identity_conflict_count', 0)}`",
        f"- 冲突复核计划: [[{_vault_relative(conflict_plan_path, vault).removesuffix('.md')}]]",
        f"- 近重复组: `{report.get('duplicate_group_count', 0)}`",
        f"- 证据不足、暂不建议变更: `{report.get('evidence_insufficient_count', 0)}`",
        f"- 被生命周期约束阻断: `{report.get('blocked_lifecycle_action_count', 0)}`",
        f"- 可执行建议: `{report.get('executable_recommendation_count', report.get('recommended_action_count', 0))}`",
        "",
        "## 身份冲突",
        "",
        "同一正式 ID 指向多个记录时无法安全判断其身份；这些记录只进入报告，不会生成生命周期提案。",
        "",
        "| ID | Status | Type | Project | Revision | Source | Title |",
        "|---|---|---|---|---|---|---|",
    ]
    for conflict in report.get("identity_conflicts", [])[:200]:
        for record in conflict.get("records", []):
            source = str(record.get("path") or "").removesuffix(".md")
            lines.append(
                "| `{memory_id}` | `{status}` | `{kind}` | {project} | `{revision}` | [[{source}|source]] | {title} |".format(
                    memory_id=_table(conflict.get("id", "")),
                    status=_table(record.get("status", "")),
                    kind=_table(record.get("type", "")),
                    project=_table(record.get("project", "")),
                    revision=_table(record.get("revision", "")),
                    source=source,
                    title=_table(record.get("title", "")),
                )
            )
    lines.extend(
        [
            "",
            "## 近重复",
            "",
            "| Project | Type | Representative | Members | Reason |",
            "|---|---|---|---:|---|",
        ]
    )
    for group in report.get("duplicate_groups", [])[:200]:
        lines.append(
            "| {project} | `{kind}` | `{representative}` | {members} | `{reason}` |".format(
                project=_table(group.get("project", "")),
                kind=_table(group.get("type", "")),
                representative=_table(group.get("representative_id", "")),
                members=len(group.get("member_ids") or []),
                reason=_table(group.get("reason", "")),
            )
        )
    lines.extend(
        [
            "",
            "## 质量积压分层",
            "",
            "证据不足记录只保留在复核积压中；被阻断建议会显示具体约束；只有可执行建议可以进入提案流程。",
            "",
            "### 证据不足原因",
            "",
            "| Reason | Count |",
            "|---|---:|",
        ]
    )
    for reason, count in sorted(
        (
            report.get("evidence_insufficient_breakdown", {}).get(
                "by_reason", {}
            )
            or {}
        ).items()
    ):
        lines.append(f"| `{_table(reason)}` | {count} |")
    lines.extend(
        [
            "",
            "### 被阻断的生命周期建议",
            "",
            "| Action | Memory | Blocked Reason | Blocking Memories |",
            "|---|---|---|---|",
        ]
    )
    for item in report.get("blocked_lifecycle_actions", [])[:500]:
        lines.append(
            "| `{action}` | `{memory}` | `{reason}` | {blockers} |".format(
                action=_table(item.get("action", "")),
                memory=_table(item.get("memory_id", "")),
                reason=_table(item.get("blocked_reason", "")),
                blockers=_table(
                    ", ".join(item.get("blocking_memory_ids") or [])
                )
                or "-",
            )
        )
    lines.extend(
        [
            "",
            "## 待复核记录",
            "",
            "| Type | Project | ID | Score | Reasons | Source |",
            "|---|---|---|---:|---|---|",
        ]
    )
    for item in report.get("low_quality", [])[:300]:
        source = str(item.get("path") or "").removesuffix(".md")
        lines.append(
            "| `{kind}` | {project} | `{memory_id}` | {score} | {reasons} | [[{source}|source]] |".format(
                kind=_table(item.get("type", "")),
                project=_table(item.get("project", "")),
                memory_id=_table(item.get("id", "")),
                score=item.get("quality_score", ""),
                reasons=_table(", ".join(item.get("quality_reasons") or [])),
                source=source,
            )
        )
    lines.extend(
        [
            "",
            "## 建议操作",
            "",
            "| Action | Memory | Expected Revision | Replacement | Reason |",
            "|---|---|---|---|---|",
        ]
    )
    for item in report.get("recommended_actions", [])[:500]:
        lines.append(
            "| `{action}` | `{memory}` | `{revision}` | `{replacement}` | {reason} |".format(
                action=_table(item.get("action", "")),
                memory=_table(item.get("memory_id", "")),
                revision=_table(item.get("expected_revision", "")),
                replacement=_table(item.get("replacement_id", "")) or "-",
                reason=_table(item.get("reason", "")),
            )
        )
    content = (
        "---\n"
        + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + "\n".join(lines).rstrip()
        + "\n"
    )
    durable_atomic_write(path, content, root=vault)
    return path


def _vault_relative(path, vault):
    return os.path.relpath(path, vault).replace(os.sep, "/")


def _table(value):
    return str(value or "").replace("|", "/").replace("\n", " ").strip()


def _audit_report_snapshot(cfg, write_report=False):
    """Freeze one writer-consistent audit snapshot and optional report."""
    vault = os.path.abspath(os.path.expanduser(str(cfg.get("vault_path") or "")))
    lock_path = safe_vault_path(
        vault,
        "04-Feedback",
        "_logs",
        "harvester.lock",
    )
    ensure_directory_tree(os.path.dirname(lock_path), vault)
    with exclusive_file_lock(lock_path, root=vault):
        report = audit_formal_memories(cfg)
        report_path = write_quality_report(cfg, report) if write_report else ""
    return report, report_path


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Audit formal DECISION/ERROR/FAVOR memory quality"
    )
    parser.add_argument("--write-report", action="store_true")
    parser.add_argument("--propose", action="store_true")
    parser.add_argument(
        "--old-before",
        default="",
        metavar="YYYY-MM-DD",
        help=(
            "write an exact approval plan for active records dated before "
            "this day; --propose creates only this subset without global reconciliation"
        ),
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    from config import load_config

    cfg = load_config()
    if args.old_before:
        if args.write_report:
            parser.error("--old-before cannot be combined with --write-report")
        old_plan = write_old_memory_lifecycle_plan(cfg, args.old_before)
        proposal_paths = (
            create_quality_proposals(cfg, old_plan, reconcile="selected")
            if args.propose
            else []
        )
        payload = {**old_plan, "proposal_paths": proposal_paths}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print(
                "Old memory quality: "
                f"before={old_plan['cutoff_exclusive']}, "
                f"active={old_plan['old_active_record_count']}, "
                f"low_quality={old_plan['old_low_quality_count']}, "
                f"recommended_actions={old_plan['recommended_action_count']}, "
                f"canonical_sha256={old_plan['canonical_sha256']}"
            )
            print(f"PLAN {old_plan['path']}")
            for path in proposal_paths:
                print(f"PROPOSED {path}")
        return 0
    report, report_path = _audit_report_snapshot(
        cfg,
        write_report=args.write_report,
    )
    conflict_plan_path = (
        _conflict_plan_path(cfg) if args.write_report else ""
    )
    proposal_paths = create_quality_proposals(cfg, report) if args.propose else []
    if args.json:
        payload = {
            **report,
            "report_path": report_path,
            "identity_conflict_plan_path": conflict_plan_path,
            "proposal_paths": proposal_paths,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print(
            "Memory quality: "
            f"active={report['active_record_count']}, "
            f"low_quality={report['low_quality_count']}, "
            f"duplicate_groups={report['duplicate_group_count']}, "
            f"evidence_insufficient={report.get('evidence_insufficient_count', 0)}, "
            f"blocked_actions={report.get('blocked_lifecycle_action_count', 0)}, "
            "executable_recommendations="
            f"{report.get('executable_recommendation_count', report.get('recommended_action_count', 0))}"
        )
        if report_path:
            print(f"REPORT {report_path}")
        for path in proposal_paths:
            print(f"PROPOSED {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
