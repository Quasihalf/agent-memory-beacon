#!/usr/bin/env python3
"""User-authorized lifecycle control for formal Agent Memory Beacon records."""
import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml

from knowledge_index import configured_recall_index_path
from memory_schema import (
    FORMAL_MEMORY_STATUSES,
    RUNTIME_SCHEMA_VERSION,
    canonical_project,
    is_valid_memory_id,
    memory_revision,
    normalize_expires_at,
    normalize_formal_record,
    parse_formal_section,
    suppress_unmet_dependencies,
)
from safety import (
    VAULT_INTERNAL_DIR_NAMES,
    durable_atomic_write,
    durable_unlink,
    ensure_directory_tree,
    exclusive_file_lock,
    redact_sensitive,
    safe_filename,
    safe_vault_path,
    secure_list_directory,
    secure_read_bytes,
    split_frontmatter_text,
)


MAX_FORMAL_FILE_BYTES = 64 * 1024 * 1024
LIFECYCLE_ACTIONS = frozenset(
    {
        "supersede",
        "retract",
        "expire",
        "restore",
        "schedule-expiry",
    }
)
ADAPTIVE_FILES = {
    "personal-memory.md": "personal",
    "skill-routing-rules.md": "skill",
    "workflow-rules.md": "workflow",
    "insights.md": "insight",
}
DERIVED_VAULT_PATHS = (
    "00-Inbox/Agent Memory Index.md",
    "05-Agent-Memory/keyword-index.json",
    "05-Agent-Memory/keyword-index.md",
    "05-Agent-Memory/global-atoms.json",
    "05-Agent-Memory/global-atoms.md",
    "05-Agent-Memory/memory-graph.json",
    "05-Agent-Memory/recall-context.md",
)


class LifecycleError(RuntimeError):
    pass


class LifecycleConflict(LifecycleError):
    pass


class LifecyclePreconditionError(LifecycleError):
    pass


@dataclass(frozen=True)
class MemoryLocation:
    memory_id: str
    revision: str
    memory_type: str
    status: str
    project: str
    scope: str
    title: str
    summary: str
    path: str
    storage: str
    source_digest: str
    record: dict = field(repr=False, compare=False)
    aggregate_key: str = ""
    aggregate_index: int = -1
    section_start: int = -1
    section_end: int = -1


@dataclass(frozen=True)
class LifecyclePlan:
    action: str
    memory_id: str
    reason: str
    expected_revision: str
    before_status: str
    after_status: str
    source_path: str
    source_digest: str
    replacement_id: str = ""
    expires_at: str = ""
    automatic: bool = False


@dataclass(frozen=True)
class LifecycleResult:
    memory_id: str
    action: str
    before_status: str
    after_status: str
    source_path: str
    revision: str = ""
    rollback_manifest: str = ""
    applied: bool = True


def find_records(cfg, memory_id="", query=""):
    """Return structurally valid formal records without following Vault symlinks."""
    vault = _vault(cfg)
    wanted_id = str(memory_id or "").strip()
    if wanted_id and not is_valid_memory_id(wanted_id):
        raise ValueError("invalid memory ID")
    needle = " ".join(str(query or "").casefold().split())
    records = []
    for path in _formal_store_paths(cfg):
        records.extend(_read_formal_store(path, vault, cfg))
    if wanted_id:
        records = [item for item in records if item.memory_id == wanted_id]
    if needle:
        records = [
            item
            for item in records
            if needle
            in " ".join(
                [
                    item.memory_id,
                    item.memory_type,
                    item.project,
                    item.title,
                    item.summary,
                ]
            ).casefold()
        ]
    records.sort(key=lambda item: (item.memory_id, item.path))
    return records


def plan_transition(
    cfg,
    action,
    memory_id,
    reason,
    replacement_id="",
    expected_revision="",
    expires_at="",
    now=None,
    automatic=False,
    _record_snapshot=None,
):
    """Validate one exact transition and freeze its source preconditions."""
    action = str(action or "").strip().lower()
    if action not in LIFECYCLE_ACTIONS:
        raise ValueError(f"unsupported lifecycle action: {action}")
    reason = _one_line(redact_sensitive(reason))
    if not reason:
        raise ValueError("lifecycle reason is required")
    location = _snapshot_one_record(cfg, _record_snapshot, memory_id)
    if expected_revision and expected_revision != location.revision:
        raise LifecyclePreconditionError("memory revision precondition failed")
    expected_revision = expected_revision or location.revision
    replacement_id = str(replacement_id or "").strip()
    normalized_expiry = normalize_expires_at(expires_at)

    if automatic:
        if action != "expire":
            raise LifecycleConflict(
                "automatic authority is limited to explicit expiry"
            )
        record_expiry = normalize_expires_at(location.record.get("expires_at"))
        if not record_expiry:
            raise LifecycleConflict(
                "automatic expiry requires an existing expires_at timestamp"
            )
        if datetime.fromisoformat(record_expiry) > _aware_now(now):
            raise LifecycleConflict("automatic expiry is not due yet")

    if action in {"retract", "expire", "supersede", "schedule-expiry"}:
        if location.status != "active":
            raise LifecycleConflict(
                f"{action} requires an active memory, got {location.status}"
            )
    if action == "restore" and location.status not in {
        "superseded",
        "retracted",
        "expired",
    }:
        raise LifecycleConflict(
            "restore is allowed only for superseded, retracted, or expired memory"
        )
    if action == "restore" and location.status == "superseded":
        _assert_no_active_successor(cfg, location)
    if action == "supersede":
        if not replacement_id or replacement_id == location.memory_id:
            raise LifecycleConflict("supersede requires a distinct replacement ID")
        replacement = _snapshot_one_record(cfg, _record_snapshot, replacement_id)
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
        _assert_replacement_eligible_after_supersession(
            cfg,
            location.memory_id,
            replacement.memory_id,
            records=_snapshot_records(_record_snapshot),
        )
    elif replacement_id:
        raise ValueError("replacement_id is valid only for supersede")

    if action == "schedule-expiry" and not normalized_expiry:
        raise ValueError("schedule-expiry requires expires_at")
    if action != "schedule-expiry" and normalized_expiry:
        raise ValueError("expires_at is valid only for schedule-expiry")

    after_status = {
        "supersede": "superseded",
        "retract": "retracted",
        "expire": "expired",
        "restore": "active",
        "schedule-expiry": "active",
    }[action]
    return LifecyclePlan(
        action=action,
        memory_id=location.memory_id,
        reason=reason,
        expected_revision=expected_revision,
        before_status=location.status,
        after_status=after_status,
        source_path=location.path,
        source_digest=location.source_digest,
        replacement_id=replacement_id,
        expires_at=normalized_expiry,
        automatic=bool(automatic),
    )


def apply_transition(cfg, plan, rebuilders=None):
    """Apply one frozen lifecycle plan with rollback and derived rebuilds."""
    if not isinstance(plan, LifecyclePlan):
        raise TypeError("plan must be a LifecyclePlan")
    selected_rebuilders = (
        _default_rebuilders() if rebuilders is None else list(rebuilders)
    )
    if not selected_rebuilders:
        raise ValueError("at least one lifecycle rebuilder is required")
    if not all(callable(rebuilder) for rebuilder in selected_rebuilders):
        raise TypeError("every lifecycle rebuilder must be callable")
    vault = _vault(cfg)
    lock_path = safe_vault_path(vault, "04-Feedback", "_logs", "harvester.lock")
    ensure_directory_tree(os.path.dirname(lock_path), vault)
    with exclusive_file_lock(lock_path, root=vault):
        current_plan = plan_transition(
            cfg,
            plan.action,
            plan.memory_id,
            plan.reason,
            replacement_id=plan.replacement_id,
            expected_revision=plan.expected_revision,
            expires_at=plan.expires_at,
            automatic=plan.automatic,
        )
        if current_plan.source_digest != plan.source_digest:
            raise LifecyclePreconditionError("formal memory source changed after preview")
        operation_id = _operation_id(plan)
        snapshots, manifest_path = _create_rollback_snapshot(
            cfg,
            plan.source_path,
            operation_id,
        )
        try:
            content, revision = _updated_source_content(cfg, current_plan)
            durable_atomic_write(
                plan.source_path,
                content,
                root=vault,
            )
            for rebuilder in selected_rebuilders:
                rebuilder(cfg)
            _verify_runtime_state(cfg, plan.memory_id, plan.after_status)
            _append_audit(cfg, operation_id, current_plan, revision)
            _mark_manifest(manifest_path, vault, "applied", revision=revision)
        except Exception as apply_error:
            rollback_errors = []
            try:
                _restore_snapshots(vault, snapshots)
            except Exception as rollback_error:
                rollback_errors.append(f"data restore: {rollback_error}")
            manifest_status = "rollback_failed" if rollback_errors else "rolled_back"
            try:
                _mark_manifest(manifest_path, vault, manifest_status)
            except Exception as manifest_error:
                rollback_errors.append(f"manifest update: {manifest_error}")
            if rollback_errors:
                raise LifecycleError(
                    f"lifecycle apply failed: {apply_error}; rollback failed: "
                    + "; ".join(rollback_errors)
                ) from apply_error
            raise
        return LifecycleResult(
            memory_id=plan.memory_id,
            action=plan.action,
            before_status=plan.before_status,
            after_status=plan.after_status,
            source_path=plan.source_path,
            revision=revision,
            rollback_manifest=manifest_path,
            applied=True,
        )


def create_proposal(
    cfg,
    *,
    action,
    memory_id,
    reason,
    evidence_refs=None,
    replacement_id="",
    expected_revision="",
    replacement_revision="",
    now=None,
    _record_snapshot=None,
):
    """Write an isolated lifecycle proposal without changing formal memory."""
    action = str(action or "").strip().lower()
    if action not in LIFECYCLE_ACTIONS:
        raise ValueError("invalid proposal action")
    location = _snapshot_one_record(cfg, _record_snapshot, memory_id)
    expected_revision = str(expected_revision or "").strip()
    if expected_revision and expected_revision != location.revision:
        raise LifecycleConflict(
            f"stale revision for {location.memory_id}: expected "
            f"{expected_revision}, current {location.revision}"
        )
    expected_revision = expected_revision or location.revision
    reason = _one_line(redact_sensitive(reason))
    if not reason:
        raise ValueError("proposal reason is required")
    refs = []
    for item in evidence_refs or []:
        value = _one_line(redact_sensitive(item))
        if value and value not in refs:
            refs.append(value)
    replacement_id = str(replacement_id or "").strip()
    requested_replacement_revision = str(replacement_revision or "").strip()
    replacement_revision = ""
    if action == "supersede":
        plan_transition(
            cfg,
            action,
            location.memory_id,
            reason,
            replacement_id=replacement_id,
            expected_revision=expected_revision,
            _record_snapshot=_record_snapshot,
        )
        replacement = _snapshot_one_record(cfg, _record_snapshot, replacement_id)
        if (
            requested_replacement_revision
            and requested_replacement_revision != replacement.revision
        ):
            raise LifecycleConflict(
                f"stale replacement revision for {replacement.memory_id}: expected "
                f"{requested_replacement_revision}, current {replacement.revision}"
            )
        replacement_revision = replacement.revision
    elif replacement_id or requested_replacement_revision:
        raise ValueError(
            "replacement_id and replacement_revision are valid only for supersede proposals"
        )
    timestamp = _aware_now(now)
    proposal_id = hashlib.sha256(
        "\x1f".join(
            [
                location.memory_id,
                location.revision,
                action,
                reason,
                replacement_id,
                replacement_revision,
                *sorted(refs),
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    vault = _vault(cfg)
    directory = _lifecycle_path(
        cfg,
        "proposal_dir",
        "04-Feedback/_lifecycle-proposals",
    )
    ensure_directory_tree(directory, vault)
    filename = safe_filename(
        f"{action} {location.memory_id} {proposal_id}",
        default=f"lifecycle-{proposal_id}",
        max_length=120,
    ) + ".md"
    path = safe_vault_path(vault, os.path.relpath(directory, vault), filename)
    if _is_real_file(path):
        existing = _secure_read_text(path, vault)
        frontmatter_text, _body = split_frontmatter_text(existing)
        try:
            existing_frontmatter = yaml.safe_load(frontmatter_text) or {}
        except yaml.YAMLError as exc:
            raise LifecycleConflict("existing lifecycle proposal is invalid") from exc
        if (
            isinstance(existing_frontmatter, dict)
            and existing_frontmatter.get("proposal_id") == proposal_id
        ):
            if existing_frontmatter.get("status") == "pending":
                return path
            if existing_frontmatter.get("status") == "stale":
                existing_frontmatter["status"] = "pending"
                existing_frontmatter["reactivated_at"] = timestamp.isoformat()
                existing_frontmatter.pop("stale_at", None)
                existing_frontmatter.pop("stale_reason", None)
                restored_body = str(_body or "").replace(
                    "# 已过期提案:",
                    "# 待确认:",
                    1,
                ).replace(
                    "该提案已被最新质量审计标记为过期，不能作为当前批准依据。",
                    "该文件只是待确认提案，不会进入正式召回。",
                    1,
                )
                durable_atomic_write(
                    path,
                    _render_frontmatter(existing_frontmatter, restored_body),
                    root=vault,
                )
                return path
        raise LifecycleConflict("lifecycle proposal path collision")
    frontmatter = {
        "proposal_id": proposal_id,
        "status": "pending",
        "summary_type": "lifecycle-proposal",
        "created_at": timestamp.isoformat(),
        "action": action,
        "memory_id": location.memory_id,
        "expected_revision": location.revision,
        "reason": reason,
        "evidence_refs": refs,
    }
    if replacement_id:
        frontmatter["replacement_id"] = replacement_id
        frontmatter["replacement_revision"] = replacement_revision
    body = "\n".join(
        [
            f"# 待确认: {action} {location.memory_id}",
            "",
            f"- 当前状态: `{location.status}`",
            f"- 类型: `{location.memory_type}`",
            f"- 内容: {location.title}",
            f"- 原因: {reason}",
            *(
                [
                    f"- 替代记忆: `{replacement_id}`",
                    f"- 替代 revision: `{replacement_revision}`",
                ]
                if replacement_id
                else []
            ),
            f"- 来源: [[{_vault_relative(location.path, vault)}]]",
            "",
            "该文件只是待确认提案，不会进入正式召回。",
            "",
        ]
    )
    durable_atomic_write(
        path,
        _render_frontmatter(frontmatter, body),
        root=vault,
    )
    return path


def sweep_expired(cfg, now=None, apply=False, rebuilders=None):
    """Preview or apply only explicitly timestamped automatic expirations."""
    current = _aware_now(now)
    due = []
    for location in find_records(cfg):
        if location.status != "active" or not location.record.get("expires_at"):
            continue
        expiry = datetime.fromisoformat(location.record["expires_at"])
        if expiry <= current:
            due.append((expiry, location))
    due.sort(key=lambda item: (item[0], item[1].memory_id))
    results = []
    for _expiry, location in due:
        reason = f"达到预先设置的有效期 {location.record['expires_at']}"
        if not apply:
            results.append(
                LifecycleResult(
                    memory_id=location.memory_id,
                    action="expire",
                    before_status="active",
                    after_status="expired",
                    source_path=location.path,
                    revision=location.revision,
                    applied=False,
                )
            )
            continue
        plan = plan_transition(
            cfg,
            "expire",
            location.memory_id,
            reason,
            expected_revision=location.revision,
            automatic=True,
        )
        results.append(apply_transition(cfg, plan, rebuilders=rebuilders))
    return results


def _formal_store_paths(cfg):
    vault = _vault(cfg)
    paths = []
    projects_root = safe_vault_path(vault, "01-Projects")
    if _is_real_directory(projects_root):
        directories, _files = secure_list_directory(projects_root, vault)
        for project in directories:
            try:
                if canonical_project(project) != project:
                    continue
            except ValueError:
                continue
            memory_dir = safe_vault_path(vault, "01-Projects", project, "Memory")
            if not _is_real_directory(memory_dir):
                continue
            _directories, files = secure_list_directory(memory_dir, vault)
            for filename in ("decisions.md", "pitfalls.md"):
                if filename in files:
                    paths.append(safe_vault_path(memory_dir, filename))
    for section, key, default in (
        ("personal_memory", "formal_path", "05-Agent-Memory/personal-memory.md"),
        (
            "skill_preferences",
            "formal_path",
            "05-Agent-Memory/skill-routing-rules.md",
        ),
        (
            "workflow_memory",
            "formal_path",
            "05-Agent-Memory/workflow-rules.md",
        ),
        (
            "insight_memory",
            "formal_path",
            "05-Agent-Memory/insights.md",
        ),
    ):
        raw = (cfg.get(section) or {}).get(key, default)
        path = safe_vault_path(vault, raw)
        if _is_real_file(path):
            paths.append(path)
    return list(dict.fromkeys(paths))


def _read_formal_store(path, vault, cfg):
    content = _secure_read_text(path, vault)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    filename = os.path.basename(path)
    if filename in {"decisions.md", "pitfalls.md"}:
        return _read_aggregate_store(path, vault, content, digest)
    kind = _adaptive_store_kind(cfg, path, vault)
    if kind:
        return _read_adaptive_store(path, content, digest, kind)
    return []


def _adaptive_store_kind(cfg, path, vault):
    path = os.path.abspath(path)
    for section, default, kind in (
        ("personal_memory", "05-Agent-Memory/personal-memory.md", "personal"),
        (
            "skill_preferences",
            "05-Agent-Memory/skill-routing-rules.md",
            "skill",
        ),
        ("workflow_memory", "05-Agent-Memory/workflow-rules.md", "workflow"),
        ("insight_memory", "05-Agent-Memory/insights.md", "insight"),
    ):
        raw = (cfg.get(section) or {}).get("formal_path", default)
        if os.path.abspath(safe_vault_path(vault, raw)) == path:
            return kind
    return ADAPTIVE_FILES.get(os.path.basename(path))


def _read_aggregate_store(path, vault, content, digest):
    frontmatter_text, _body = split_frontmatter_text(content)
    if frontmatter_text is None:
        return []
    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(frontmatter, dict) or frontmatter.get("schema_version") != RUNTIME_SCHEMA_VERSION:
        return []
    key = "decisions" if os.path.basename(path) == "decisions.md" else "pitfalls"
    memory_type = "decision" if key == "decisions" else "error"
    project = canonical_project(frontmatter.get("project"))
    if not project or project != _project_from_aggregate_path(path, vault):
        return []
    values = frontmatter.get(key)
    if not isinstance(values, list):
        return []
    source_note = "note:" + _vault_relative(path, vault).removesuffix(".md")
    records = []
    for index, raw in enumerate(values):
        if not isinstance(raw, dict) or raw.get("status") not in FORMAL_MEMORY_STATUSES:
            continue
        try:
            normalized = normalize_formal_record(
                raw,
                memory_type=memory_type,
                default_project=project,
                source_ref=source_note,
                source_record_key=f"{key}:{index}",
            )
        except (TypeError, ValueError):
            continue
        if raw.get("revision") != normalized.get("revision"):
            continue
        records.append(
            _location_from_record(
                normalized,
                path,
                "aggregate",
                digest,
                aggregate_key=key,
                aggregate_index=index,
            )
        )
    return records


def _read_adaptive_store(path, content, digest, kind):
    frontmatter_text, body = split_frontmatter_text(content)
    if frontmatter_text is None or body is None:
        return []
    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError:
        return []
    if not isinstance(frontmatter, dict) or frontmatter.get("schema_version") != RUNTIME_SCHEMA_VERSION:
        return []
    body_start = len(content) - len(body)
    headings = list(re.finditer(r"^##\s+(.+?)\s*$", body, re.MULTILINE))
    records = []
    for index, heading in enumerate(headings):
        section_end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        section = body[heading.end():section_end].strip()
        record = parse_formal_section(heading.group(1).strip(), section, kind)
        if not record:
            continue
        records.append(
            _location_from_record(
                record,
                path,
                "markdown",
                digest,
                section_start=body_start + heading.start(),
                section_end=body_start + section_end,
            )
        )
    return records


def _location_from_record(record, path, storage, digest, **position):
    return MemoryLocation(
        memory_id=record["id"],
        revision=record["revision"],
        memory_type=record["type"],
        status=record["status"],
        project=record.get("project", ""),
        scope=record["scope"],
        title=record["title"],
        summary=record["summary"],
        path=path,
        storage=storage,
        source_digest=digest,
        record=dict(record),
        **position,
    )


def _updated_source_content(cfg, plan):
    location = _one_record(cfg, plan.memory_id)
    if location.revision != plan.expected_revision:
        raise LifecyclePreconditionError("memory revision changed before write")
    if location.source_digest != plan.source_digest:
        raise LifecyclePreconditionError("formal memory source changed before write")
    if location.storage == "aggregate":
        return _update_aggregate_content(cfg, location, plan)
    return _update_markdown_content(cfg, location, plan)


def _update_aggregate_content(cfg, location, plan):
    vault = _vault(cfg)
    content = _secure_read_text(location.path, vault)
    frontmatter_text, body = split_frontmatter_text(content)
    frontmatter = yaml.safe_load(frontmatter_text) or {}
    values = frontmatter.get(location.aggregate_key)
    if not isinstance(values, list):
        raise LifecyclePreconditionError("formal aggregate changed shape")
    matches = [
        index
        for index, item in enumerate(values)
        if isinstance(item, dict) and item.get("id") == location.memory_id
    ]
    if len(matches) != 1:
        raise LifecyclePreconditionError("formal memory ID is no longer unique")
    raw = dict(values[matches[0]])
    _apply_record_transition(raw, plan)
    normalized = normalize_formal_record(
        raw,
        memory_type=location.memory_type,
        default_project=location.project,
        source_ref="note:" + _vault_relative(location.path, vault).removesuffix(".md"),
        source_record_key=f"{location.aggregate_key}:{matches[0]}",
    )
    raw["revision"] = normalized["revision"]
    values[matches[0]] = raw
    frontmatter[location.aggregate_key] = values
    return _render_frontmatter(frontmatter, body), normalized["revision"]


def _update_markdown_content(cfg, location, plan):
    vault = _vault(cfg)
    content = _secure_read_text(location.path, vault)
    segment = content[location.section_start:location.section_end]
    if f"- id: `{location.memory_id}`" not in segment:
        raise LifecyclePreconditionError("formal section changed before write")
    record = dict(location.record)
    _apply_record_transition(record, plan)
    revision = memory_revision(record)
    segment = _set_markdown_field(segment, "status", record["status"], code=True)
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
    return (
        content[:location.section_start]
        + segment
        + content[location.section_end:],
        revision,
    )


def _apply_record_transition(record, plan):
    for key in ("superseded_by", "retracted_reason", "expired_reason"):
        record.pop(key, None)
    if plan.action == "supersede":
        record["status"] = "superseded"
        record["superseded_by"] = plan.replacement_id
    elif plan.action == "retract":
        record["status"] = "retracted"
        record["retracted_reason"] = plan.reason
    elif plan.action == "expire":
        record["status"] = "expired"
        record["expired_reason"] = plan.reason
    elif plan.action == "restore":
        record["status"] = "active"
        if plan.before_status == "expired":
            record.pop("expires_at", None)
    elif plan.action == "schedule-expiry":
        record["status"] = "active"
        record["expires_at"] = plan.expires_at
    else:  # pragma: no cover - plan validation prevents this.
        raise ValueError("unsupported lifecycle action")
    record["revision"] = memory_revision(record)


def _set_markdown_field(segment, key, value, code=False):
    pattern = re.compile(rf"(?m)^-\s*{re.escape(key)}:\s*.*(?:\n|$)")
    if not value:
        return pattern.sub("", segment)
    rendered = f"`{value}`" if code else str(value)
    line = f"- {key}: {rendered}\n"
    if pattern.search(segment):
        return pattern.sub(line, segment, count=1)
    status = re.search(r"(?m)^-\s*status:\s*.*\n", segment)
    if not status:
        raise LifecyclePreconditionError("formal section has no status field")
    return segment[:status.end()] + line + segment[status.end():]


def _default_rebuilders():
    def rebuild_index(cfg):
        from session_harvester import rebuild_memory_index

        rebuild_memory_index(cfg, repair_generated=False)

    def rebuild_context(cfg):
        from compiler import run

        result = run(
            cfg,
            dry_run=False,
            step_results={},
            sync_agent_memory=False,
        )
        errors = list(result.get("context_target_errors") or [])
        if result.get("memory_error"):
            errors.append(str(result["memory_error"]))
        if errors:
            raise RuntimeError("context rebuild failed: " + "; ".join(errors))

    return [rebuild_index, rebuild_context]


def _verify_runtime_state(cfg, memory_id, after_status):
    vault = _vault(cfg)
    index_path = configured_recall_index_path(cfg)
    data = json.loads(_secure_read_text(index_path, vault))
    runtime_identities = set()
    for item in data.get("units", []):
        if not isinstance(item, dict):
            continue
        for value in [item.get("id"), *(item.get("aliases") or [])]:
            value = str(value or "").strip()
            if value:
                runtime_identities.add(value)

    location = _one_record(cfg, memory_id)
    if location.status != after_status:
        raise RuntimeError(
            f"formal memory status mismatch after rebuild: "
            f"expected {after_status}, got {location.status}"
        )

    if after_status != "active" and memory_id in runtime_identities:
        raise RuntimeError(
            "inactive or suppressed memory remained in the runtime index: "
            + memory_id
        )

    expected_runtime_ids = set()
    suppressed_dependencies = data.get("suppressed_dependencies") or {}
    dependency_suppressed = (
        isinstance(suppressed_dependencies, dict)
        and memory_id in suppressed_dependencies
    )
    if after_status == "active" and not dependency_suppressed:
        expected_runtime_ids.add(memory_id)
    elif after_status == "superseded":
        replacement_id = str(location.record.get("superseded_by") or "").strip()
        if replacement_id:
            expected_runtime_ids.add(replacement_id)

    missing = expected_runtime_ids - runtime_identities
    if missing:
        raise RuntimeError(
            "eligible memory is missing from the runtime index: "
            + ", ".join(sorted(missing)[:10])
        )


def _create_rollback_snapshot(cfg, source_path, operation_id):
    vault = _vault(cfg)
    rollback_root = _lifecycle_path(
        cfg,
        "rollback_dir",
        "04-Feedback/_rollback/lifecycle",
    )
    operation_root = safe_vault_path(
        vault,
        os.path.relpath(rollback_root, vault),
        operation_id,
    )
    files_root = safe_vault_path(
        vault,
        os.path.relpath(operation_root, vault),
        "files",
    )
    ensure_directory_tree(files_root, vault)
    targets = [("vault", source_path)]
    targets.extend(
        ("vault", safe_vault_path(vault, relative))
        for relative in DERIVED_VAULT_PATHS
    )
    targets.append(("vault", configured_recall_index_path(cfg)))
    targets.append(("vault", _audit_path(cfg)))
    profile_dir = str(cfg.get("codex_profile_path") or "").strip()
    if profile_dir:
        profile_path = os.path.abspath(
            os.path.expanduser(os.path.join(profile_dir, "AGENTS.shared.md"))
        )
        try:
            profile_in_vault = os.path.commonpath([vault, profile_path]) == vault
        except ValueError:
            profile_in_vault = False
        targets.append(
            (
                "vault" if profile_in_vault else "external",
                safe_vault_path(vault, os.path.relpath(profile_path, vault))
                if profile_in_vault
                else profile_path,
            )
        )
    for target in cfg.get("context_targets") or []:
        targets.append(("external", os.path.abspath(os.path.expanduser(target))))
    unique = []
    seen = set()
    for kind, path in targets:
        identity = (kind, path)
        if identity not in seen:
            seen.add(identity)
            unique.append(identity)

    snapshots = []
    manifest_items = []
    for index, (kind, path) in enumerate(unique):
        data, mode = _snapshot_file(kind, path, vault)
        backup_relative = ""
        if data is not None:
            backup_relative = f"files/{index:04d}.bin"
            durable_atomic_write(
                safe_vault_path(operation_root, backup_relative),
                data,
                mode=0o600,
                root=vault,
            )
        item = {
            "kind": kind,
            "path": _vault_relative(path, vault) if kind == "vault" else path,
            "existed": data is not None,
            "mode": mode,
            "sha256": hashlib.sha256(data).hexdigest() if data is not None else "",
            "backup": backup_relative,
        }
        snapshots.append({**item, "data": data})
        manifest_items.append(item)
    manifest = {
        "schema_version": 1,
        "operation_id": operation_id,
        "status": "prepared",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "targets": manifest_items,
    }
    manifest_path = safe_vault_path(operation_root, "manifest.json")
    durable_atomic_write(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        root=vault,
    )
    return snapshots, manifest_path


def _snapshot_file(kind, path, vault):
    if kind == "vault":
        if not _is_real_file(path):
            return None, 0o600
        data = secure_read_bytes(path, MAX_FORMAL_FILE_BYTES, root=vault)
        if len(data) > MAX_FORMAL_FILE_BYTES:
            raise ValueError("rollback source exceeds size limit")
        return data, stat.S_IMODE(os.lstat(path).st_mode)
    try:
        current = os.lstat(path)
    except FileNotFoundError:
        return None, 0o600
    if not stat.S_ISREG(current.st_mode):
        raise ValueError(f"external lifecycle target must be a regular file: {path}")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        data = b""
        while len(data) <= MAX_FORMAL_FILE_BYTES:
            chunk = os.read(
                descriptor,
                min(65_536, MAX_FORMAL_FILE_BYTES + 1 - len(data)),
            )
            if not chunk:
                break
            data += chunk
    finally:
        os.close(descriptor)
    if len(data) > MAX_FORMAL_FILE_BYTES:
        raise ValueError("external rollback source exceeds size limit")
    return data, stat.S_IMODE(current.st_mode)


def _restore_snapshots(vault, snapshots):
    errors = []
    for item in snapshots:
        path = (
            safe_vault_path(vault, item["path"])
            if item["kind"] == "vault"
            else item["path"]
        )
        try:
            if item["existed"]:
                if item["kind"] == "vault":
                    ensure_directory_tree(os.path.dirname(path), vault)
                    durable_atomic_write(
                        path,
                        item["data"],
                        mode=item["mode"],
                        root=vault,
                    )
                else:
                    _atomic_write_external(path, item["data"], item["mode"])
            elif os.path.lexists(path):
                if item["kind"] == "vault":
                    durable_unlink(path, root=vault)
                else:
                    current = os.lstat(path)
                    if not stat.S_ISREG(current.st_mode):
                        raise OSError("refusing to remove non-regular external target")
                    os.unlink(path)
        except Exception as exc:  # Preserve every later restore attempt.
            errors.append(f"{path}: {exc}")
    if errors:
        raise RuntimeError("lifecycle rollback incomplete: " + "; ".join(errors))


def _atomic_write_external(path, data, mode):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".lifecycle-", dir=parent)
    try:
        os.fchmod(descriptor, mode or 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.lexists(path) and not stat.S_ISREG(os.lstat(path).st_mode):
            raise ValueError("external restore target is not a regular file")
        os.replace(temporary, path)
        temporary = ""
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _append_audit(cfg, operation_id, plan, revision):
    vault = _vault(cfg)
    path = _audit_path(cfg)
    if _is_real_file(path):
        existing = _secure_read_text(path, vault)
    else:
        existing = _render_frontmatter(
            {
                "title": "Memory Lifecycle Audit",
                "summary_type": "lifecycle-audit",
                "generated_by": "memory_lifecycle.py",
                "schema_version": RUNTIME_SCHEMA_VERSION,
            },
            "# Memory Lifecycle Audit\n\n正式记忆状态变更的审计记录。\n",
        )
    timestamp = datetime.now(timezone.utc).isoformat()
    entry = "\n".join(
        [
            f"## {timestamp} {plan.action} {plan.memory_id}",
            "",
            f"- operation_id: `{operation_id}`",
            f"- memory_id: `{plan.memory_id}`",
            f"- action: `{plan.action}`",
            f"- authority: `{'explicit-expiry' if plan.automatic else 'user'}`",
            f"- before_status: `{plan.before_status}`",
            f"- after_status: `{plan.after_status}`",
            f"- expected_revision: `{plan.expected_revision}`",
            f"- resulting_revision: `{revision}`",
            f"- reason: {plan.reason}",
            f"- source: [[{_vault_relative(plan.source_path, vault).removesuffix('.md')}]]",
        ]
    )
    ensure_directory_tree(os.path.dirname(path), vault)
    durable_atomic_write(
        path,
        existing.rstrip() + "\n\n" + entry.rstrip() + "\n",
        root=vault,
    )


def _mark_manifest(path, vault, status, revision=""):
    data = json.loads(_secure_read_text(path, vault))
    data["status"] = status
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    if revision:
        data["resulting_revision"] = revision
    durable_atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        root=vault,
    )


def _snapshot_records(snapshot):
    if snapshot is None:
        return None
    if isinstance(snapshot, dict):
        values = []
        for records in snapshot.values():
            values.extend(records if isinstance(records, (list, tuple)) else [records])
        return tuple(values)
    return tuple(snapshot)


def _snapshot_one_record(cfg, snapshot, memory_id):
    if snapshot is None:
        return _one_record(cfg, memory_id)
    records = snapshot.get(memory_id, ()) if isinstance(snapshot, dict) else (
        item for item in snapshot if getattr(item, "memory_id", "") == memory_id
    )
    records = list(records)
    if not records:
        raise LifecycleConflict(f"formal memory not found: {memory_id}")
    if len(records) != 1:
        raise LifecycleConflict(f"formal memory ID is not unique: {memory_id}")
    return records[0]


def _one_record(cfg, memory_id):
    records = find_records(cfg, memory_id=memory_id)
    if not records:
        raise LifecycleConflict(f"formal memory not found: {memory_id}")
    if len(records) != 1:
        raise LifecycleConflict(f"formal memory ID is not unique: {memory_id}")
    return records[0]


def _assert_replacement_eligible_after_supersession(
    cfg,
    superseded_id,
    replacement_id,
    records=None,
):
    active_records = [
        item.record
        for item in (records if records is not None else find_records(cfg))
        if item.status == "active" and item.memory_id != superseded_id
    ]
    eligible, _suppressed = suppress_unmet_dependencies(active_records)
    eligible_ids = {str(item.get("id") or "") for item in eligible}
    if replacement_id not in eligible_ids:
        raise LifecycleConflict(
            "replacement memory would be dependency-suppressed after supersession"
        )


def _assert_no_active_successor(cfg, location):
    successor_id = str(location.record.get("superseded_by") or "").strip()
    if not successor_id:
        raise LifecycleConflict("superseded memory has no successor ID")
    seen = {location.memory_id}
    while successor_id:
        if successor_id in seen:
            raise LifecycleConflict("supersession chain contains a cycle")
        seen.add(successor_id)
        successor = _one_record(cfg, successor_id)
        if (
            successor.memory_type != location.memory_type
            or successor.scope != location.scope
            or successor.project != location.project
        ):
            raise LifecycleConflict(
                "successor must have the same type, scope, and project"
            )
        if successor.status == "active":
            raise LifecycleConflict("an active successor still blocks restoration")
        if successor.status in {"retracted", "expired"}:
            return
        if successor.status != "superseded":
            raise LifecycleConflict(
                f"unsupported successor status: {successor.status}"
            )
        successor_id = str(successor.record.get("superseded_by") or "").strip()
        if not successor_id:
            raise LifecycleConflict("supersession chain is incomplete")


def _vault(cfg):
    vault = os.path.abspath(os.path.expanduser(str(cfg.get("vault_path") or "")))
    if not vault or not _is_real_directory(vault):
        raise ValueError("configured Vault must be a real directory")
    return vault


def _lifecycle_path(cfg, key, default):
    vault = _vault(cfg)
    settings = cfg.get("memory_lifecycle") or {}
    return safe_vault_path(vault, settings.get(key, default))


def _audit_path(cfg):
    return _lifecycle_path(
        cfg,
        "audit_path",
        "05-Agent-Memory/lifecycle-audit.md",
    )


def _project_from_aggregate_path(path, vault):
    relative = _vault_relative(path, vault)
    match = re.fullmatch(
        r"01-Projects/([^/]+)/Memory/(?:decisions|pitfalls)\.md",
        relative,
    )
    return canonical_project(match.group(1)) if match else ""


def _secure_read_text(path, vault):
    data = secure_read_bytes(path, MAX_FORMAL_FILE_BYTES, root=vault)
    if len(data) > MAX_FORMAL_FILE_BYTES:
        raise ValueError("formal memory file exceeds size limit")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("formal memory file is not UTF-8") from exc


def _render_frontmatter(frontmatter, body):
    return (
        "---\n"
        + yaml.safe_dump(
            frontmatter,
            allow_unicode=True,
            sort_keys=False,
        )
        + "---\n"
        + str(body or "")
    )


def _vault_relative(path, vault):
    return os.path.relpath(path, vault).replace(os.sep, "/")


def _is_real_directory(path):
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _is_real_file(path):
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except FileNotFoundError:
        return False


def _one_line(value):
    return " ".join(str(value or "").split())


def _aware_now(value=None):
    current = value or datetime.now().astimezone()
    if current.tzinfo is None or current.utcoffset() is None:
        raise ValueError("lifecycle time must include a timezone")
    return current


def _operation_id(plan):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    digest = hashlib.sha256(
        "\x1f".join(
            [plan.action, plan.memory_id, plan.expected_revision, stamp]
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"{stamp}-{digest}"


def _result_dict(value):
    return {
        key: getattr(value, key)
        for key in value.__dataclass_fields__
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Preview or apply formal memory lifecycle transitions"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    listing = subparsers.add_parser("list")
    listing.add_argument("--id", default="")
    listing.add_argument("--query", default="")
    listing.add_argument("--json", action="store_true")

    proposal = subparsers.add_parser("propose")
    proposal.add_argument("action", choices=sorted(LIFECYCLE_ACTIONS))
    proposal.add_argument("--id", required=True)
    proposal.add_argument("--reason", required=True)
    proposal.add_argument("--evidence", action="append", default=[])
    proposal.add_argument("--replacement-id", default="")
    proposal.add_argument("--expected-revision", default="")
    proposal.add_argument("--replacement-revision", default="")

    for action in sorted(LIFECYCLE_ACTIONS):
        command = subparsers.add_parser(action)
        command.add_argument("--id", required=True)
        command.add_argument("--reason", required=True)
        command.add_argument("--expected-revision", default="")
        command.add_argument("--replacement-id", default="")
        command.add_argument("--expires-at", default="")
        command.add_argument("--apply", action="store_true")

    sweep = subparsers.add_parser("sweep-expired")
    sweep.add_argument("--apply", action="store_true")
    sweep.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from config import load_config

    cfg = load_config()
    if args.command == "list":
        records = find_records(cfg, memory_id=args.id, query=args.query)
        payload = [
            {
                "id": item.memory_id,
                "revision": item.revision,
                "type": item.memory_type,
                "status": item.status,
                "project": item.project,
                "title": item.title,
                "path": item.path,
            }
            for item in records
        ]
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for item in payload:
                print(
                    f"[{item['status']}] {item['id']} {item['title']} "
                    f"revision={item['revision']}"
                )
        return 0
    if args.command == "propose":
        path = create_proposal(
            cfg,
            action=args.action,
            memory_id=args.id,
            reason=args.reason,
            evidence_refs=args.evidence,
            replacement_id=args.replacement_id,
            expected_revision=args.expected_revision,
            replacement_revision=args.replacement_revision,
        )
        print(f"PROPOSED {path}")
        return 0
    if args.command == "sweep-expired":
        results = sweep_expired(cfg, apply=args.apply)
        payload = [_result_dict(item) for item in results]
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for item in results:
                print(
                    f"{'EXPIRED' if item.applied else 'WOULD_EXPIRE'} "
                    f"{item.memory_id}"
                )
        return 0

    if args.apply and not args.expected_revision:
        parser.error("--apply requires --expected-revision from a prior preview")
    plan = plan_transition(
        cfg,
        args.command,
        args.id,
        args.reason,
        replacement_id=args.replacement_id,
        expected_revision=args.expected_revision,
        expires_at=args.expires_at,
    )
    if not args.apply:
        print(json.dumps(_result_dict(plan), ensure_ascii=False, indent=2))
        return 0
    result = apply_transition(cfg, plan)
    print(json.dumps(_result_dict(result), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
