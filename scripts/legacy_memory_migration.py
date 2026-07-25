"""Reversible migration from legacy Vault memory to runtime schema 2.0."""
import hashlib
import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import yaml

from brand_migration import (
    _cleanup_backup_failure,
    _create_staging_directory,
    _hash_backup_file,
    _inode_from_stat,
    _inventory_staging_tree,
    _named_stat,
    _open_or_create_directory_chain,
    _open_relative_directory,
    _open_relative_parent,
    _open_vault_directory,
    _publish_staging,
    _quarantine_remove_name,
    _rename_exchange,
    _rename_exclusive,
    _seal_staging_tree,
    _serialize_manifest_bytes,
    _stat_relative,
    _verify_sealed_staging,
    _write_new_file,
    migration_writer_guard,
)
from memory_judge import is_one_off_action_request, is_question_only
from memory_schema import (
    FORMAL_MEMORY_STATUSES,
    OPERATIONAL_MEMORY_FIELDS,
    RUNTIME_SCHEMA_VERSION,
    canonical_project,
    formal_identity_key,
    is_valid_memory_id,
    memory_revision,
    merge_formal_records,
    normalize_fact_text,
    normalize_formal_record,
    stable_memory_id,
)
from safety import (
    normalize_project_slug,
    safe_vault_path,
    split_frontmatter_text,
    strip_platform_injected_context,
)


CST = timezone(timedelta(hours=8))
ROLLBACK_ROOT = ("04-Feedback", "_rollback", "memory-v2")
SEALED_BACKUP_MTIME_NS = 946684800_000_000_000
CANDIDATE_DIRS = {
    "_memory-candidates": "personal",
    "_skill-preferences": "skill",
    "_workflow-candidates": "workflow",
}
PLACEHOLDER_PROJECTS = frozenset({"slug", "project-slug"})
TRANSIENT_PROCESS_DECISION_PATTERNS = (
    re.compile(
        r"^task\s*\d+\b.{0,80}(?:判定|复审|审查|review\s*fix|获批|通过|needs?\s*fix)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:审查|复审|验收|报告).{0,40}(?:结论|判定|评为|通过|获批|"
        r"pass\b|needs\s+(?:revision|fixes?)|blocked\b|ready\b)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:结论|判定|评为).{0,40}(?:pass\b|needs\s+(?:revision|fixes?)|"
        r"blocked\b|ready\b|通过|达标|获批)",
        re.IGNORECASE,
    ),
    re.compile(r"^本轮(?:只|仅|不|保持)"),
    re.compile(
        r"^(?:不由|不代替).{0,50}(?:reviewer|controller).{0,50}(?:receipt|报告|凭证)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:same-context|同上下文).{0,60}(?:自审|审查|precheck|pass)",
        re.IGNORECASE,
    ),
    re.compile(r"^审查凭证使用\s+new_conversation", re.IGNORECASE),
    re.compile(r"^以\d{1,2}:\d{2}后.{0,30}(?:快照|基线)"),
    re.compile(
        r"^不把(?:本轮|当前).{0,60}(?:pass|结论|验证证据)",
        re.IGNORECASE,
    ),
)
INVALID_TITLES = frozenset(
    {
        "",
        "...",
        "content",
        "summary",
        "一句话说明选择了什么",
    }
)
_NO_EXPECTED_STATE = object()


@dataclass(frozen=True)
class PlannedWrite:
    relative_path: str
    content: bytes
    existed_before: bool
    before_sha256: str
    desired_sha256: str
    reason: str


@dataclass(frozen=True)
class LegacyMemoryMigrationPlan:
    vault: str
    created_at: str
    writes: tuple
    stats: dict

    def content_for(self, relative_path):
        normalized = _relative_path(relative_path)
        for item in self.writes:
            if item.relative_path == normalized:
                return item.content
        raise KeyError(normalized)

    def preview(self):
        return {
            "status": "ready",
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "vault": self.vault,
            "created_at": self.created_at,
            **self.stats,
            "writes": [
                {
                    "path": item.relative_path,
                    "reason": item.reason,
                    "existed_before": item.existed_before,
                    "before_sha256": item.before_sha256,
                    "desired_sha256": item.desired_sha256,
                }
                for item in self.writes
            ],
        }


def build_migration_plan(vault, project_aliases=None):
    vault_path = Path(vault).expanduser().resolve()
    if not vault_path.is_dir():
        raise FileNotFoundError(f"Vault path not found: {vault_path}")
    created_at = datetime.now(CST).isoformat()
    aliases = dict(project_aliases or {})
    documents = _load_memory_documents(vault_path)
    occurrences, session_routes = _collect_occurrences(documents, aliases)
    occurrences = _sanitize_occurrences(vault_path, occurrences)
    occurrences = _reroute_placeholder_occurrences(occurrences)

    normalized = []
    for occurrence in occurrences:
        raw = dict(occurrence["record"])
        raw["project"] = occurrence["project"]
        if not occurrence.get("formal_authority"):
            _classify_legacy_record(raw, occurrence["memory_type"])
        raw["path"] = occurrence["relative_path"].removesuffix(".md")
        raw["source_note"] = f"note:{raw['path']}"
        record = normalize_formal_record(
            raw,
            memory_type=occurrence["memory_type"],
            default_project=occurrence["project"],
            source_ref=(
                ""
                if occurrence.get("formal_authority")
                else occurrence["source_ref"]
            ),
            source_record_key=(
                f"{occurrence['record_key']}:{occurrence['record_index']}"
            ),
            date=occurrence["date"],
            project_aliases=aliases,
        )
        normalized.append(
            {
                "occurrence": occurrence,
                "record": record,
            }
        )

    merged, occurrence_records, superseded = _merge_migration_occurrences(
        normalized
    )

    desired = {}
    reasons = {}
    _plan_session_rewrites(
        documents,
        session_routes,
        occurrence_records,
        desired,
        reasons,
        aliases,
    )
    _plan_project_aggregates(documents, merged, desired, reasons, created_at)
    candidate_records, rejected = _plan_candidate_rewrites(
        documents,
        desired,
        reasons,
        aliases,
    )
    _plan_formal_adaptive_memory(
        documents,
        candidate_records,
        desired,
        reasons,
    )
    _sanitize_planned_markdown(vault_path, desired)

    writes = []
    for relative_path in sorted(desired):
        target = vault_path / relative_path
        _assert_safe_target(vault_path, target)
        before = target.read_bytes() if target.exists() else b""
        content = desired[relative_path]
        if target.exists() and before == content:
            continue
        writes.append(
            PlannedWrite(
                relative_path=relative_path,
                content=content,
                existed_before=target.exists(),
                before_sha256=_sha256(before) if target.exists() else "",
                desired_sha256=_sha256(content),
                reason=reasons.get(relative_path, "schema-v2-normalization"),
            )
        )

    stats = {
        "planned_writes": len(writes),
        "new_files": sum(not item.existed_before for item in writes),
        "legacy_occurrences": len(occurrences),
        "formal_records": len(merged),
        "active_records": sum(item.get("status") == "active" for item in merged),
        "inactive_records": sum(item.get("status") != "active" for item in merged),
        "duplicates_merged": max(0, len(occurrences) - len(merged)),
        "records_superseded": superseded,
        "candidates_rejected": rejected,
    }
    return LegacyMemoryMigrationPlan(
        vault=str(vault_path),
        created_at=created_at,
        writes=tuple(writes),
        stats=stats,
    )


def _sanitize_planned_markdown(vault, desired):
    from session_harvester import sanitize_generated_memory_markdown

    cfg = {"vault_path": str(vault)}
    for relative_path, content in list(desired.items()):
        sanitized = sanitize_generated_memory_markdown(
            content.decode("utf-8"),
            cfg,
            relative_path,
        )
        desired[relative_path] = sanitized.encode("utf-8")


def _sanitize_occurrences(vault, occurrences):
    from session_harvester import sanitize_obsidian_markdown

    cfg = {"vault_path": str(vault)}
    sanitized = []
    for occurrence in occurrences:
        if occurrence.get("formal_authority"):
            sanitized.append(occurrence)
            continue
        current = dict(occurrence)
        record = dict(current["record"])
        fields = (
            ("text", "title", "context", "summary")
            if current["memory_type"] == "decision"
            else ("type", "title", "resolution", "summary")
        )
        for field in fields:
            if field in record:
                record[field] = sanitize_obsidian_markdown(record[field], cfg)
        current["record"] = record
        sanitized.append(current)
    return sanitized


def apply_migration(
    plan,
    migration_id=None,
    guard_factory=migration_writer_guard,
):
    if not isinstance(plan, LegacyMemoryMigrationPlan):
        raise TypeError("plan must be a LegacyMemoryMigrationPlan")
    vault = Path(plan.vault)
    migration_id = migration_id or datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    if normalize_project_slug(migration_id) != migration_id:
        raise ValueError(f"invalid migration id: {migration_id}")

    manifest_path = None
    mutation_attempted = False
    rollback_completed = False
    first_rollback_error = None
    try:
        with guard_factory(vault):
            _verify_plan_inputs(plan)
            manifest_path = _create_backup(plan, migration_id)
            try:
                for item in plan.writes:
                    _verify_plan_item_input(
                        vault,
                        item,
                        error_context="changed after backup",
                    )
                    mutation_attempted = True
                    _atomic_write_bytes(
                        vault / item.relative_path,
                        item.content,
                        expected_sha256=(
                            item.before_sha256 if item.existed_before else None
                        ),
                    )
                _verify_desired_outputs(plan)
            except BaseException:
                if mutation_attempted:
                    try:
                        _restore_from_manifest(
                            vault,
                            manifest_path,
                            verify_current=True,
                            allow_partial=True,
                        )
                        rollback_completed = True
                    except BaseException as rollback_error:
                        first_rollback_error = rollback_error
                raise
    except BaseException as apply_error:
        if (
            mutation_attempted
            and not rollback_completed
            and manifest_path is not None
        ):
            try:
                with guard_factory(vault):
                    _restore_from_manifest(
                        vault,
                        manifest_path,
                        verify_current=True,
                        allow_partial=True,
                    )
                rollback_completed = True
            except BaseException as rollback_error:
                details = [
                    str(error)
                    for error in (first_rollback_error, rollback_error)
                    if error is not None
                ]
                detail = "; ".join(details)
                raise RuntimeError(
                    f"migration apply failed ({apply_error}) and automatic "
                    f"rollback refused: {detail}; manual recovery manifest: "
                    f"{manifest_path}"
                ) from apply_error
        raise
    return {
        "status": "applied",
        "migration_id": migration_id,
        "manifest": str(manifest_path),
        **plan.stats,
    }


def rollback_migration(
    vault,
    manifest_path,
    guard_factory=migration_writer_guard,
):
    vault_path = Path(vault).expanduser().resolve()
    manifest_path = Path(manifest_path).expanduser().resolve()
    with guard_factory(vault_path):
        manifest = _restore_from_manifest(
            vault_path,
            manifest_path,
            verify_current=True,
        )
    return {
        "status": "rolled_back",
        "migration_id": manifest.get("migration_id", ""),
        "manifest": str(manifest_path),
    }


def _load_memory_documents(vault):
    documents = {}
    roots = [vault / "01-Projects", vault / "04-Feedback", vault / "05-Agent-Memory"]
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*.md")):
            relative = path.relative_to(vault).as_posix()
            if any(part in {"_raw-sessions", "_rollback", "_logs", "codex-profile"} for part in path.parts):
                continue
            if not _is_memory_migration_document(relative):
                continue
            _assert_safe_target(vault, path)
            content = path.read_bytes()
            frontmatter, body = _split_markdown(content)
            documents[relative] = {
                "path": path,
                "frontmatter": frontmatter,
                "body": body,
                "content": content,
            }
    return documents


def _is_memory_migration_document(relative):
    return bool(
        re.match(r"^01-Projects/[^/]+/Memory/(?:decisions|pitfalls)\.md$", relative)
        or re.match(r"^01-Projects/[^/]+/Memory/sessions/[^/]+\.md$", relative)
        or re.match(
            r"^04-Feedback/(?:_memory-candidates|_skill-preferences|_workflow-candidates)/[^/]+\.md$",
            relative,
        )
        or relative
        in {
            "05-Agent-Memory/personal-memory.md",
            "05-Agent-Memory/skill-routing-rules.md",
            "05-Agent-Memory/workflow-rules.md",
        }
    )


def _collect_occurrences(documents, aliases):
    occurrences = []
    session_routes = {}
    for relative, document in documents.items():
        frontmatter = document["frontmatter"]
        session_match = re.match(
            r"^01-Projects/([^/]+)/Memory/sessions/[^/]+\.md$",
            relative,
        )
        if session_match:
            if str(frontmatter.get("memory_schema_version") or "") == RUNTIME_SCHEMA_VERSION:
                continue
            container = canonical_project(
                frontmatter.get("project") or session_match.group(1),
                aliases,
            ) or session_match.group(1)
            decisions = [
                item for item in frontmatter.get("decisions_made", []) or []
                if isinstance(item, dict)
            ]
            errors = [
                item for item in frontmatter.get("errors_encountered", []) or []
                if isinstance(item, dict)
            ]
            explicit = {
                canonical_project(item.get("project"), aliases)
                for item in [*decisions, *errors]
                if canonical_project(item.get("project"), aliases)
            }
            non_container = {item for item in explicit if item != container}
            inherited = next(iter(non_container)) if len(non_container) == 1 else ""
            session_routes[relative] = {
                "container": container,
                "inherited": inherited,
            }
            source_ref = f"session:{frontmatter.get('session_id') or relative}"
            date = str(frontmatter.get("date") or "")
            for memory_type, key, records in (
                ("decision", "decisions_made", decisions),
                ("error", "errors_encountered", errors),
            ):
                for index, record in enumerate(records):
                    project = canonical_project(record.get("project"), aliases) or inherited or container
                    occurrences.append(
                        _occurrence(
                            relative,
                            key,
                            index,
                            memory_type,
                            record,
                            project,
                            source_ref,
                            date,
                            formal_authority=False,
                        )
                    )
            continue

        aggregate_match = re.match(
            r"^01-Projects/([^/]+)/Memory/(decisions|pitfalls)\.md$",
            relative,
        )
        if not aggregate_match:
            continue
        container = canonical_project(
            frontmatter.get("project") or aggregate_match.group(1),
            aliases,
        ) or aggregate_match.group(1)
        key = aggregate_match.group(2)
        memory_type = "decision" if key == "decisions" else "error"
        source_ref = f"note:{relative.removesuffix('.md')}"
        schema_v2 = str(frontmatter.get("schema_version") or "") == RUNTIME_SCHEMA_VERSION
        for index, record in enumerate(frontmatter.get(key, []) or []):
            if not isinstance(record, dict):
                continue
            project = canonical_project(record.get("project"), aliases) or container
            occurrences.append(
                _occurrence(
                    relative,
                    key,
                    index,
                    memory_type,
                    record,
                    project,
                    source_ref,
                    str(record.get("date") or ""),
                    formal_authority=(
                        schema_v2
                        and _is_schema_v2_formal_record(
                            record,
                            memory_type,
                        )
                    ),
                )
            )
    return occurrences, session_routes


def _occurrence(
    relative,
    key,
    index,
    memory_type,
    record,
    project,
    source_ref,
    date,
    *,
    formal_authority,
):
    return {
        "key": (relative, key, index),
        "relative_path": relative,
        "record_key": key,
        "record_index": index,
        "memory_type": memory_type,
        "record": dict(record),
        "project": project,
        "source_ref": source_ref,
        "date": date,
        "formal_authority": bool(formal_authority),
    }


def _is_schema_v2_formal_record(record, memory_type):
    if not isinstance(record, dict):
        return False
    if not is_valid_memory_id(record.get("id")):
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", str(record.get("revision") or "")):
        return False
    if record.get("status") not in FORMAL_MEMORY_STATUSES:
        return False
    if record.get("scope") != "project" or not canonical_project(record.get("project")):
        return False
    refs = record.get("source_refs")
    if not (
        isinstance(refs, list)
        and refs
        and all(isinstance(item, str) and item.strip() for item in refs)
    ):
        return False
    return memory_type in {"decision", "error"}


def _reroute_placeholder_occurrences(occurrences):
    real_projects = {}
    for item in occurrences:
        if item["project"] in PLACEHOLDER_PROJECTS:
            continue
        real_projects.setdefault(_occurrence_content_key(item), set()).add(item["project"])
    rerouted = []
    for item in occurrences:
        item = dict(item)
        if (
            item["project"] in PLACEHOLDER_PROJECTS
            and not item.get("formal_authority")
        ):
            matches = real_projects.get(_occurrence_content_key(item), set())
            if len(matches) == 1:
                item["project"] = next(iter(matches))
            else:
                record = dict(item["record"])
                record["status"] = "retracted"
                record["retracted_reason"] = "legacy_placeholder_route"
                item["record"] = record
        rerouted.append(item)
    return rerouted


def _occurrence_content_key(item):
    record = item["record"]
    if item["memory_type"] == "decision":
        title = record.get("text") or record.get("title")
        summary = record.get("context") or record.get("summary")
    else:
        title = record.get("type") or record.get("title")
        summary = record.get("resolution") or record.get("summary")
    return (
        item["memory_type"],
        normalize_fact_text(title),
        normalize_fact_text(summary),
    )


def _merge_migration_occurrences(entries):
    authorities = [
        item for item in entries
        if item["occurrence"].get("formal_authority")
    ]
    legacy = [
        item for item in entries
        if not item["occurrence"].get("formal_authority")
    ]
    authority_by_id = {}
    authority_by_fact = {}
    occurrence_records = {}
    for item in authorities:
        record = item["record"]
        memory_id = record["id"]
        if memory_id in authority_by_id:
            raise ValueError(
                "duplicate schema 2.0 formal memory ID requires lifecycle repair: "
                f"{memory_id}"
            )
        authority_by_id[memory_id] = record
        authority_by_fact.setdefault(formal_identity_key(record), []).append(record)
        occurrence_records[item["occurrence"]["key"]] = record

    reserved_identities = set(authority_by_id)
    for record in authority_by_id.values():
        reserved_identities.update(record.get("aliases") or [])

    unmatched = []
    for item in legacy:
        record = item["record"]
        fact = formal_identity_key(record)
        candidates = authority_by_fact.get(fact, [])
        explicit_id = str(
            item["occurrence"]["record"].get("id")
            or item["occurrence"]["record"].get("memory_id")
            or ""
        ).strip()
        matching_id = next(
            (candidate for candidate in candidates if candidate["id"] == explicit_id),
            None,
        )
        target = matching_id or (candidates[0] if len(candidates) == 1 else None)
        if target is None:
            if candidates:
                continue
            unmatched.append(item)
            continue
        _merge_legacy_evidence(
            target,
            record,
            reserved_identities=reserved_identities,
        )
        occurrence_records[item["occurrence"]["key"]] = target

    legacy_by_fact = {}
    for item in unmatched:
        legacy_by_fact.setdefault(
            formal_identity_key(item["record"]),
            [],
        ).append(item)

    migrated = []
    occupied = set(reserved_identities)
    migrated_entries = []
    for fact in sorted(legacy_by_fact):
        group = legacy_by_fact[fact]
        canonical = merge_formal_records(
            [item["record"] for item in group]
        )[0]
        if canonical["id"] in occupied:
            previous_id = canonical["id"]
            canonical["id"] = _unused_migration_id(
                canonical,
                group[0]["occurrence"],
                occupied,
            )
            aliases = set(canonical.get("aliases") or [])
            aliases.discard(previous_id)
            canonical["aliases"] = sorted(aliases)
        occupied.add(canonical["id"])
        migrated.append(canonical)
        migrated_entries.append((canonical, group))

    all_formal_ids = set(authority_by_id)
    all_formal_ids.update(record["id"] for record in migrated)
    for record in migrated:
        record["aliases"] = sorted(
            alias
            for alias in set(record.get("aliases") or [])
            if alias not in all_formal_ids and alias != record["id"]
        )
        record["revision"] = memory_revision(record)

    superseded = _apply_ready_supersession(migrated)
    for canonical, group in migrated_entries:
        for item in group:
            occurrence_records[item["occurrence"]["key"]] = canonical

    merged = sorted(
        [*authority_by_id.values(), *migrated],
        key=lambda record: (formal_identity_key(record), str(record["id"])),
    )
    return merged, occurrence_records, superseded


def _merge_legacy_evidence(target, evidence, *, reserved_identities):
    refs = set(target.get("source_refs") or [])
    refs.update(evidence.get("source_refs") or [])
    target["source_refs"] = sorted(refs)
    aliases = set(target.get("aliases") or [])
    evidence_id = str(evidence.get("id") or "")
    if (
        evidence_id
        and evidence_id != target["id"]
        and evidence_id not in reserved_identities
    ):
        aliases.add(evidence_id)
    aliases.update(
        alias
        for alias in evidence.get("aliases") or []
        if alias not in reserved_identities and alias != target["id"]
    )
    target["aliases"] = sorted(aliases)


def _unused_migration_id(record, occurrence, occupied):
    source_note = str(record.get("source_note") or occurrence["source_ref"])
    source_key = f"{occurrence['record_key']}:{occurrence['record_index']}"
    for attempt in range(1000):
        candidate_key = source_key if attempt == 0 else f"{source_key}:migration-{attempt}"
        candidate = stable_memory_id(
            record["type"],
            record.get("project", ""),
            source_note,
            candidate_key,
        )
        if candidate not in occupied:
            return candidate
    raise RuntimeError("unable to allocate a collision-free migration memory ID")


def _classify_legacy_record(record, memory_type):
    existing_status = str(record.get("status") or "").strip().lower()
    if existing_status and existing_status != "active":
        return
    if memory_type == "decision":
        title = str(record.get("text") or record.get("title") or "").strip()
        summary = str(record.get("context") or record.get("summary") or "").strip()
    else:
        title = str(record.get("type") or record.get("title") or "").strip()
        summary = str(record.get("resolution") or record.get("summary") or "").strip()
    normalized_title = normalize_fact_text(title)
    if (
        normalized_title in INVALID_TITLES
        or title.startswith("\\s*")
        or len(title) > 500
        or len(summary) > 1200
    ):
        record["status"] = "retracted"
        record["retracted_reason"] = "legacy_parser_noise"
        return
    if not normalize_fact_text(summary):
        record["status"] = "retracted"
        record["retracted_reason"] = "legacy_incomplete_record"
        return
    if (
        memory_type == "decision"
        and _is_transient_process_decision(title)
        and not _is_phase_ready_state(title)
    ):
        record["status"] = "expired"
        record["expired_reason"] = "legacy_process_state"
        return
    record["status"] = "active"


def _is_transient_process_decision(title):
    text = str(title or "").strip()
    return any(pattern.search(text) for pattern in TRANSIENT_PROCESS_DECISION_PATTERNS)


def _is_phase_ready_state(title):
    text = str(title or "")
    return "Phase A" in text and "Ready" in text


def _apply_ready_supersession(records):
    finals = {}
    for record in records:
        title = str(record.get("title") or "")
        if "Phase A" in title and "最终" in title and "Ready" in title and "Not Ready" not in title:
            finals[record.get("project", "")] = record
            record["status"] = "active"
            record.pop("expired_reason", None)
            record["revision"] = memory_revision(record)
    changed = 0
    for record in records:
        title = str(record.get("title") or "")
        final = finals.get(record.get("project", ""))
        if not final or "Phase A" not in title or "Not Ready" not in title:
            continue
        record["status"] = "superseded"
        record["superseded_by"] = final["id"]
        record.pop("expired_reason", None)
        record["revision"] = memory_revision(record)
        changed += 1
    return changed


def _plan_session_rewrites(
    documents,
    session_routes,
    occurrence_records,
    desired,
    reasons,
    aliases,
):
    for relative, route in session_routes.items():
        document = documents[relative]
        frontmatter = dict(document["frontmatter"])
        body = document["body"]
        frontmatter["project"] = canonical_project(
            frontmatter.get("project") or route["container"],
            aliases,
        ) or route["container"]
        projects = {
            canonical_project(item, aliases)
            for item in frontmatter.get("projects", []) or []
        }
        projects.discard("")
        projects.add(frontmatter["project"])
        final_title = ""
        for memory_type, key in (
            ("decision", "decisions_made"),
            ("error", "errors_encountered"),
        ):
            updated = []
            for index, original in enumerate(frontmatter.get(key, []) or []):
                record = occurrence_records.get((relative, key, index))
                if record is None:
                    updated.append(original)
                    continue
                updated.append(_serialize_record(record, memory_type))
                if record.get("project"):
                    projects.add(record["project"])
                if (
                    memory_type == "decision"
                    and "Phase A" in record.get("title", "")
                    and "最终" in record.get("title", "")
                    and record.get("status") == "active"
                ):
                    final_title = record["title"]
            frontmatter[key] = updated
        frontmatter["projects"] = sorted(projects)
        frontmatter["memory_schema_version"] = RUNTIME_SCHEMA_VERSION
        if final_title:
            frontmatter["ai_title"] = final_title
            body = re.sub(r"(?m)^#\s+.*$", f"# {final_title}", body, count=1)
        desired[relative] = _render_markdown(frontmatter, body)
        reasons[relative] = "session-evidence-schema-v2"


def _plan_project_aggregates(documents, records, desired, reasons, created_at):
    grouped = {}
    existing_projects = set()
    for relative in documents:
        match = re.match(r"^01-Projects/([^/]+)/Memory/(?:decisions|pitfalls)\.md$", relative)
        if match:
            existing_projects.add(canonical_project(match.group(1)) or match.group(1))
    for record in records:
        project = record.get("project") or "Project-Infra"
        grouped.setdefault((project, record["type"]), []).append(record)
    projects = existing_projects | {project for project, _kind in grouped}
    for project in sorted(projects):
        for memory_type, key, filename in (
            ("decision", "decisions", "decisions.md"),
            ("error", "pitfalls", "pitfalls.md"),
        ):
            project_records = grouped.get((project, memory_type), [])
            relative = f"01-Projects/{project}/Memory/{filename}"
            existing = documents.get(relative)
            if _aggregate_semantics_match(
                existing,
                project,
                key,
                memory_type,
                project_records,
            ):
                desired[relative] = existing["content"]
                reasons[relative] = "canonical-formal-memory"
                continue
            previous_updated_at = ""
            if existing:
                previous_updated_at = str(
                    existing["frontmatter"].get("last_updated") or ""
                )
            rendered = _render_project_records(
                project,
                key,
                project_records,
                previous_updated_at or created_at,
            )
            if (
                existing
                and previous_updated_at
                and rendered != existing["content"]
            ):
                rendered = _render_project_records(
                    project,
                    key,
                    project_records,
                    created_at,
                )
            desired[relative] = rendered
            reasons[relative] = "canonical-formal-memory"


def _aggregate_semantics_match(
    existing,
    project,
    key,
    memory_type,
    records,
):
    if not existing:
        return False
    frontmatter = existing.get("frontmatter") or {}
    if (
        str(frontmatter.get("schema_version") or "")
        != RUNTIME_SCHEMA_VERSION
        or str(frontmatter.get("project") or "") != project
    ):
        return False
    raw_records = frontmatter.get(key)
    if not isinstance(raw_records, list):
        return False

    current = {}
    for index, raw in enumerate(raw_records):
        if not _is_schema_v2_formal_record(raw, memory_type):
            return False
        normalized = normalize_formal_record(
            raw,
            memory_type=memory_type,
            default_project=project,
            source_ref="",
            source_record_key=f"{key}:{index}",
            date=str(raw.get("date") or ""),
        )
        if normalized["revision"] != raw.get("revision"):
            return False
        memory_id = normalized["id"]
        if memory_id in current:
            return False
        current[memory_id] = _formal_record_projection(normalized)

    planned = {}
    for record in records:
        memory_id = str(record.get("id") or "")
        if not memory_id or memory_id in planned:
            return False
        planned[memory_id] = _formal_record_projection(record)
    return current == planned


def _formal_record_projection(record):
    keys = (
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
    )
    return {
        key: (
            list(record.get(key) or [])
            if key in {"source_refs", "aliases", "requires"}
            else str(record.get(key) or "")
        )
        for key in keys
    }


def _render_project_records(project, key, records, updated_at):
    memory_type = "decision" if key == "decisions" else "error"
    serialized = [
        _serialize_record(record, memory_type)
        for record in sorted(
            records,
            key=lambda item: (
                formal_identity_key(item),
                str(item.get("id") or ""),
            ),
        )
    ]
    frontmatter = {
        "project": project,
        "schema_version": RUNTIME_SCHEMA_VERSION,
        key: serialized,
        "last_updated": updated_at,
    }
    title = "Decisions" if key == "decisions" else "Pitfalls"
    body = [
        f"# {title}",
        "",
        "## Related",
        "",
        "- [[00-Inbox/Agent Memory Index|Agent Memory Index]]",
        "- [[03-Maps/timeline|Timeline]]",
        "- [[03-Maps/topic-index|Topic Index]]",
        "",
        "## Formal Memory",
        "",
    ]
    for item in serialized:
        if memory_type == "decision":
            label = item.get("text", "")
            detail = f"context: {item.get('context', '')}"
        else:
            label = item.get("type", "")
            detail = f"resolution: {item.get('resolution', '')}"
        refs = ", ".join(f"`{ref}`" for ref in item.get("source_refs", [])) or "-"
        body.append(
            f"- [{item.get('date') or '-'}] **{label}** | status: "
            f"`{item.get('status')}` | {detail} | sources: {refs}"
        )
    return _render_markdown(frontmatter, "\n".join(body).rstrip() + "\n")


def _serialize_record(record, memory_type):
    output = {
        "id": record["id"],
        "revision": record["revision"],
    }
    if memory_type == "decision":
        output["text"] = record.get("title", "")
        output["context"] = record.get("summary", "")
    else:
        output["type"] = record.get("title", "")
        output["resolution"] = record.get("summary", "")
    output.update(
        {
            "status": record.get("status", "active"),
            "project": record.get("project", ""),
            "scope": record.get("scope", "project"),
            "date": record.get("date", ""),
            "source_refs": list(record.get("source_refs") or []),
            "aliases": list(record.get("aliases") or []),
        }
    )
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


def _plan_candidate_rewrites(documents, desired, reasons, aliases):
    candidate_records = {"personal": [], "skill": [], "workflow": []}
    rejected = 0
    for relative, document in documents.items():
        match = re.match(
            r"^04-Feedback/(_memory-candidates|_skill-preferences|_workflow-candidates)/[^/]+\.md$",
            relative,
        )
        if not match:
            continue
        kind = CANDIDATE_DIRS[match.group(1)]
        if (
            str(document["frontmatter"].get("schema_version") or "")
            == RUNTIME_SCHEMA_VERSION
        ):
            frontmatter = dict(document["frontmatter"])
            if frontmatter.get("status") == "rejected":
                rejected += 1
            candidate_records[kind].append(frontmatter)
            continue
        frontmatter = _clean_candidate_frontmatter(
            document["frontmatter"],
            kind,
            aliases,
        )
        if frontmatter.get("status") == "rejected":
            rejected += 1
        body = strip_platform_injected_context(document["body"])
        desired[relative] = _render_markdown(frontmatter, body.rstrip() + "\n")
        reasons[relative] = "candidate-lifecycle-cleanup"
        candidate_records[kind].append(frontmatter)
    return candidate_records, rejected


def _clean_candidate_frontmatter(frontmatter, kind, aliases):
    original = dict(frontmatter or {})
    cleaned = _clean_value(original)
    cleaned["schema_version"] = RUNTIME_SCHEMA_VERSION
    cleaned["project"] = canonical_project(cleaned.get("project"), aliases)
    cleaned["scope"] = str(
        cleaned.get("scope")
        or ("project" if cleaned.get("project") else "global")
    )
    original_text = json.dumps(original, ensure_ascii=False)
    status = str(cleaned.get("status") or "candidate")
    reason = ""
    if status == "rejected" and cleaned.get("rejection_reason"):
        reason = str(cleaned["rejection_reason"])
    elif status != "promoted":
        if kind == "personal":
            candidate_texts = [
                str(cleaned.get(key) or "")
                for key in ("content", "evidence")
                if cleaned.get(key)
            ]
            if any(is_question_only(text) for text in candidate_texts):
                reason = "information_question"
            elif any(is_one_off_action_request(text) for text in candidate_texts):
                reason = "one_off_action"
        elif kind == "skill":
            skill_name = str(cleaned.get("skill_name") or "")
            evidence = str(cleaned.get("evidence_excerpt") or "")
            if "subagent_notification" in original_text:
                reason = "platform_injected_evidence"
            elif not skill_name or skill_name != skill_name.lower():
                reason = "invalid_skill_identifier"
            elif any(marker in evidence for marker in ("有什么区别", "什么区别", "有何区别")):
                reason = "comparison_not_invocation"
    if reason:
        cleaned["status"] = "rejected"
        cleaned["rejection_reason"] = reason
    else:
        cleaned["status"] = status
    cleaned["revision"] = _candidate_revision(cleaned)
    return cleaned


def _clean_value(value):
    if isinstance(value, str):
        return strip_platform_injected_context(value)
    if isinstance(value, list):
        return [item for item in (_clean_value(item) for item in value) if item not in ("", None)]
    if isinstance(value, dict):
        return {key: _clean_value(item) for key, item in value.items()}
    return value


def _candidate_revision(record):
    visible = {
        key: value
        for key, value in record.items()
        if key not in {"revision", "last_seen", "first_seen"}
    }
    return _sha256(json.dumps(visible, ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _plan_formal_adaptive_memory(documents, candidates, desired, reasons):
    promoted_personal = [item for item in candidates["personal"] if item.get("status") == "promoted"]
    relative = "05-Agent-Memory/personal-memory.md"
    if _is_current_formal_adaptive_memory(documents.get(relative)):
        pass
    elif promoted_personal or relative in documents:
        desired[relative] = _render_personal_memory(promoted_personal)
        reasons[relative] = "regenerate-promoted-personal-memory"

    promoted_skills = [item for item in candidates["skill"] if item.get("status") == "promoted"]
    relative = "05-Agent-Memory/skill-routing-rules.md"
    if _is_current_formal_adaptive_memory(documents.get(relative)):
        pass
    elif promoted_skills or relative in documents:
        desired[relative] = _render_skill_rules(promoted_skills)
        reasons[relative] = "regenerate-promoted-skill-memory"

    promoted_workflows = [item for item in candidates["workflow"] if item.get("status") == "promoted"]
    relative = "05-Agent-Memory/workflow-rules.md"
    if _is_current_formal_adaptive_memory(documents.get(relative)):
        pass
    elif promoted_workflows or relative in documents:
        desired[relative] = _render_workflow_rules(promoted_workflows)
        reasons[relative] = "regenerate-promoted-workflow-memory"


def _is_current_formal_adaptive_memory(document):
    return bool(
        document
        and str(document["frontmatter"].get("schema_version") or "")
        == RUNTIME_SCHEMA_VERSION
    )


def _render_personal_memory(records):
    frontmatter = {
        "title": "Personal Memory",
        "generated_by": "memory_judge.py",
        "schema_version": RUNTIME_SCHEMA_VERSION,
    }
    lines = [
        "# Personal Memory",
        "",
        "Promoted memories from repeated or high-confidence conversations.",
        "",
        "## Related",
        "",
        "- [[00-Inbox/Agent Memory Index|Agent Memory Index]]",
        "- [[03-Maps/timeline|Timeline]]",
        "- [[03-Maps/topic-index|Topic Index]]",
    ]
    for raw in sorted(records, key=lambda item: str(item.get("memory_id") or "")):
        memory_type = str(raw.get("type") or "preference").replace("environment_fact", "environment")
        scope = "global" if memory_type == "preference" else str(raw.get("scope") or "project")
        project = "" if scope == "global" else canonical_project(raw.get("project"))
        formal = normalize_formal_record(
            {
                "id": raw.get("memory_id"),
                "title": raw.get("title"),
                "content": raw.get("content"),
                "project": project,
                "scope": scope,
                "status": "active",
                "source_refs": [
                    f"session:{item}"
                    for item in raw.get("source_ids", []) or []
                    if item
                ],
            },
            memory_type=memory_type,
            default_project=project,
            source_ref=f"candidate:{raw.get('memory_id')}",
        )
        source_refs = ", ".join(f"`{item}`" for item in formal["source_refs"])
        lines.extend(
            [
                "",
                f"## {raw.get('title') or raw.get('memory_id')}",
                "",
                f"- id: `{formal['id']}`",
                f"- revision: `{formal['revision']}`",
                f"- type: `{memory_type}`",
                "- status: `active`",
                f"- scope: `{scope}`",
                f"- project: {_project_link(project)}",
                f"- source_refs: {source_refs}",
                f"- confidence: `{raw.get('confidence', '')}`",
                f"- seen_count: `{raw.get('seen_count', '')}`",
                f"- memory: {raw.get('content', '')}",
            ]
        )
    return _render_markdown(frontmatter, "\n".join(lines).rstrip() + "\n")


def _render_skill_rules(records):
    frontmatter = {
        "title": "Skill Routing Rules",
        "generated_by": "skill_preference_learner.py",
        "summary_type": "skill-routing-rules",
        "schema_version": RUNTIME_SCHEMA_VERSION,
    }
    lines = ["# Skill Routing Rules"]
    for raw in sorted(records, key=lambda item: str(item.get("memory_id") or "")):
        project = canonical_project(raw.get("project"))
        formal = normalize_formal_record(
            {
                "id": raw.get("memory_id"),
                "title": f"{raw.get('skill_name', '')}: {raw.get('task_intent', '')}",
                "content": raw.get("why_skill_fits", ""),
                "project": project,
                "scope": "project" if project else "global",
                "status": "active",
            },
            memory_type="skill",
            default_project=project,
            source_ref=f"candidate:{raw.get('memory_id')}",
        )
        source_refs = ", ".join(f"`{item}`" for item in formal["source_refs"])
        lines.extend(
            [
                "",
                f"## {raw.get('skill_name', '')}: {raw.get('task_intent', '')}",
                "",
                f"- id: `{formal['id']}`",
                f"- revision: `{formal['revision']}`",
                "- status: `active`",
                f"- scope: `{formal['scope']}`",
                f"- skill_name: `{raw.get('skill_name', '')}`",
                f"- project: {_project_link(project)}",
                f"- source_refs: {source_refs}",
                f"- confidence: `{raw.get('confidence', '')}`",
                f"- seen_count: `{raw.get('seen_count', '')}`",
                "",
                "### When to consider",
                "",
                *[f"- {item}" for item in raw.get("positive_signals", []) or []],
                "",
                "### Why this skill fits",
                "",
                str(raw.get("why_skill_fits") or ""),
                "",
                "### Do not use when",
                "",
                *[f"- {item}" for item in raw.get("negative_signals", []) or []],
                "",
                "### Evidence",
                "",
                str(raw.get("evidence_excerpt") or ""),
            ]
        )
    return _render_markdown(frontmatter, "\n".join(lines).rstrip() + "\n")


def _render_workflow_rules(records):
    frontmatter = {
        "title": "Workflow Rules",
        "generated_by": "workflow_memory.py",
        "summary_type": "workflow-rules",
        "schema_version": RUNTIME_SCHEMA_VERSION,
    }
    lines = ["# Workflow Rules"]
    for raw in sorted(records, key=lambda item: str(item.get("memory_id") or "")):
        project = canonical_project(raw.get("project"))
        formal = normalize_formal_record(
            {
                "id": raw.get("memory_id"),
                "title": f"{raw.get('rule_name', '')}: {raw.get('desired_behavior', '')}",
                "content": raw.get("why_it_matters", ""),
                "project": project,
                "scope": "project" if project else "global",
                "status": "active",
            },
            memory_type="workflow",
            default_project=project,
            source_ref=f"candidate:{raw.get('memory_id')}",
        )
        source_refs = ", ".join(f"`{item}`" for item in formal["source_refs"])
        lines.extend(
            [
                "",
                f"## {raw.get('rule_name', '')}: {raw.get('desired_behavior', '')}",
                "",
                f"- id: `{formal['id']}`",
                f"- revision: `{formal['revision']}`",
                "- status: `active`",
                f"- scope: `{formal['scope']}`",
                f"- rule_name: `{raw.get('rule_name', '')}`",
                f"- project: {_project_link(project)}",
                f"- source_refs: {source_refs}",
                f"- confidence: `{raw.get('confidence', '')}`",
                f"- seen_count: `{raw.get('seen_count', '')}`",
                "",
                "### Trigger scene",
                "",
                str(raw.get("trigger_scene") or ""),
                "",
                "### When to apply",
                "",
                *[f"- {item}" for item in raw.get("positive_signals", []) or []],
                "",
                "### Desired behavior",
                "",
                str(raw.get("desired_behavior") or ""),
                "",
                "### Why this matters",
                "",
                str(raw.get("why_it_matters") or ""),
                "",
                "### Do not apply when",
                "",
                *[f"- {item}" for item in raw.get("negative_signals", []) or []],
                "",
                "### Evidence",
                "",
                str(raw.get("evidence_excerpt") or ""),
            ]
        )
    return _render_markdown(frontmatter, "\n".join(lines).rstrip() + "\n")


def _project_link(project):
    if not project:
        return "`global`"
    return f"[[01-Projects/{project}/Memory/decisions|{project}]]"


def _create_backup(plan, migration_id):
    vault = Path(plan.vault)
    rollback_parent = Path(safe_vault_path(vault, *ROLLBACK_ROOT))
    final = rollback_parent / migration_id
    vault_pin = None
    parent_pins = []
    staging_pin = None
    published_name = None
    try:
        vault_pin = _open_vault_directory(vault)
        _open_or_create_directory_chain(
            vault_pin,
            ROLLBACK_ROOT,
            parent_pins,
        )
        for pin in reversed(parent_pins):
            os.fsync(pin.fd)
        os.fsync(vault_pin.fd)
        rollback_pin = parent_pins[-1]
        if _named_stat(rollback_pin.fd, migration_id) is not None:
            raise FileExistsError(f"migration backup already exists: {final}")
        staging_pin = _create_staging_directory(vault_pin)

        files = []
        expected_files = {}
        source_inodes = set()
        for item in plan.writes:
            backup_relative = ""
            if item.existed_before:
                source = vault / item.relative_path
                content, source_inode = _read_backup_source(
                    source,
                    item.before_sha256,
                )
                source_inodes.add(source_inode)
                relative = Path("files") / Path(item.relative_path)
                parent_fd, leaf = _open_relative_parent(
                    staging_pin.fd,
                    relative,
                    create=True,
                )
                try:
                    _write_new_file(parent_fd, leaf, content)
                finally:
                    os.close(parent_fd)
                backup_relative = relative.as_posix()
                expected_files[relative] = (item.before_sha256, "backup")
            files.append(
                {
                    "path": item.relative_path,
                    "existed_before": item.existed_before,
                    "before_sha256": item.before_sha256,
                    "desired_sha256": item.desired_sha256,
                    "backup": backup_relative,
                }
            )
        _verify_plan_inputs(plan)
        manifest = {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "migration_id": migration_id,
            "status": "prepared",
            "vault": str(vault),
            "created_at": plan.created_at,
            "sealed_mtime_ns": SEALED_BACKUP_MTIME_NS,
            "files": files,
        }
        manifest_bytes = _serialize_manifest_bytes(manifest)
        _write_new_file(
            staging_pin.fd,
            "manifest.json",
            manifest_bytes,
        )
        expected_files[Path("manifest.json")] = (
            _sha256(manifest_bytes),
            "manifest",
        )
        sealed_files, sealed_directories = _seal_staging_tree(
            staging_pin.fd,
            expected_files,
            frozenset(source_inodes),
        )
        _set_sealed_backup_mtime(
            staging_pin.fd,
            tuple(expected_files),
            tuple(binding.relative for binding in sealed_directories),
        )
        _verify_plan_inputs(plan)
        _verify_sealed_staging(
            staging_pin.fd,
            sealed_files,
            sealed_directories,
            frozenset(source_inodes),
        )
        _verify_sealed_backup_mtime(
            staging_pin.fd,
            tuple(expected_files),
            tuple(binding.relative for binding in sealed_directories),
        )
        _publish_staging(
            staging_pin.name,
            migration_id,
            vault_pin.fd,
            rollback_pin.fd,
            staging_pin.fd,
        )
        published_name = migration_id
        os.fsync(vault_pin.fd)
        os.fsync(rollback_pin.fd)
        published = _named_stat(rollback_pin.fd, migration_id)
        if (
            published is None
            or not stat.S_ISDIR(published.st_mode)
            or _inode_from_stat(published) != staging_pin.inode
        ):
            raise RuntimeError("published memory migration backup changed")
        _verify_sealed_staging(
            staging_pin.fd,
            sealed_files,
            sealed_directories,
            frozenset(source_inodes),
        )
        _verify_sealed_backup_mtime(
            staging_pin.fd,
            tuple(expected_files),
            tuple(binding.relative for binding in sealed_directories),
        )
        return final / "manifest.json"
    except BaseException as primary_error:
        if vault_pin is not None and staging_pin is not None and parent_pins:
            rollback_pin = parent_pins[-1]
            if published_name is None:
                possible = _named_stat(rollback_pin.fd, migration_id)
                if (
                    possible is not None
                    and stat.S_ISDIR(possible.st_mode)
                    and _inode_from_stat(possible) == staging_pin.inode
                ):
                    published_name = migration_id
            _cleanup_backup_failure(
                primary_error,
                staging_pin,
                published_name,
                rollback_pin,
                parent_pins,
            )
        raise
    finally:
        if staging_pin is not None:
            os.close(staging_pin.fd)
        for pin in reversed(parent_pins):
            os.close(pin.fd)
        if vault_pin is not None:
            os.close(vault_pin.fd)


def _read_backup_source(path, expected_sha256):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeError(f"migration backup source is unsafe: {path}")
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        content = b"".join(chunks)
        after = os.fstat(fd)
        if _stable_stat_identity(before) != _stable_stat_identity(after):
            raise RuntimeError(f"migration input changed during backup: {path}")
        if _sha256(content) != expected_sha256:
            raise RuntimeError(f"migration input changed during backup: {path}")
        return content, _inode_from_stat(after)
    finally:
        os.close(fd)


def _set_sealed_backup_mtime(root_fd, files, directories):
    for relative in sorted(files):
        parent_fd, leaf = _open_relative_parent(root_fd, relative)
        try:
            fd = os.open(
                leaf,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=parent_fd,
            )
            try:
                os.utime(
                    fd,
                    ns=(SEALED_BACKUP_MTIME_NS, SEALED_BACKUP_MTIME_NS),
                )
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            os.close(parent_fd)
    for relative in sorted(
        directories,
        key=lambda item: (len(Path(item).parts), str(item)),
        reverse=True,
    ):
        fd = _open_relative_directory(root_fd, relative)
        try:
            os.utime(
                fd,
                ns=(SEALED_BACKUP_MTIME_NS, SEALED_BACKUP_MTIME_NS),
            )
            os.fsync(fd)
        finally:
            os.close(fd)


def _verify_sealed_backup_mtime(root_fd, files, directories):
    for relative in files:
        current = _stat_relative(root_fd, relative)
        if current.st_mtime_ns != SEALED_BACKUP_MTIME_NS:
            raise RuntimeError(f"sealed backup metadata changed: {relative}")
    for relative in directories:
        fd = _open_relative_directory(root_fd, relative)
        try:
            if os.fstat(fd).st_mtime_ns != SEALED_BACKUP_MTIME_NS:
                raise RuntimeError(
                    f"sealed backup directory metadata changed: {relative}"
                )
        finally:
            os.close(fd)


def _verify_plan_inputs(plan):
    vault = Path(plan.vault)
    for item in plan.writes:
        _verify_plan_item_input(
            vault,
            item,
            error_context="changed after preview",
        )


def _verify_plan_item_input(vault, item, error_context):
    target = Path(vault) / item.relative_path
    _assert_safe_target(vault, target)
    if item.existed_before:
        if not target.is_file() or _sha256(target.read_bytes()) != item.before_sha256:
            raise RuntimeError(
                f"migration input {error_context}: {item.relative_path}"
            )
    elif os.path.lexists(target):
        raise RuntimeError(f"migration input {error_context}: {item.relative_path}")


def _verify_desired_outputs(plan):
    vault = Path(plan.vault)
    for item in plan.writes:
        target = vault / item.relative_path
        if not target.is_file() or _sha256(target.read_bytes()) != item.desired_sha256:
            raise RuntimeError(f"migration output verification failed: {item.relative_path}")


def _restore_from_manifest(
    vault,
    manifest_path,
    verify_current,
    allow_partial=False,
):
    manifest = _read_manifest(manifest_path, vault)
    if Path(manifest.get("vault", "")).resolve() != Path(vault).resolve():
        raise ValueError("migration manifest Vault does not match")
    prepared = []
    vault_pin = _open_vault_directory(vault)
    backup_fd = None
    try:
        backup_fd = _open_relative_directory(
            vault_pin.fd,
            Path(*ROLLBACK_ROOT, manifest["migration_id"]),
        )
        for item in manifest.get("files", []):
            relative = _relative_path(item.get("path"))
            target = Path(safe_vault_path(vault, relative))
            _assert_safe_target(vault, target)
            current_sha = _sha256(target.read_bytes()) if target.is_file() else None
            if os.path.lexists(target) and current_sha is None:
                raise RuntimeError(f"migration output changed before rollback: {relative}")
            before_sha = item.get("before_sha256") or None
            desired_sha = item.get("desired_sha256") or None
            if verify_current:
                allowed = {desired_sha}
                allowed.add(before_sha if item.get("existed_before") else None)
                if current_sha not in allowed:
                    raise RuntimeError(f"migration output changed before rollback: {relative}")
            backup_content = None
            if item.get("existed_before"):
                backup_content = _read_sealed_backup_bytes(
                    backup_fd,
                    Path(item["backup"]),
                    item["before_sha256"],
                )
            prepared.append(
                (item, relative, target, current_sha, backup_content)
            )
    finally:
        if backup_fd is not None:
            os.close(backup_fd)
        os.close(vault_pin.fd)

    for item, relative, target, current_sha, backup_content in prepared:
        before_sha = item.get("before_sha256") or None
        if item.get("existed_before") and current_sha == before_sha:
            continue
        if not item.get("existed_before") and current_sha is None:
            continue
        if item.get("existed_before"):
            if verify_current and (
                not target.is_file()
                or _sha256(target.read_bytes()) != item.get("desired_sha256")
            ):
                raise RuntimeError(f"migration output changed during rollback: {relative}")
            _atomic_write_bytes(
                target,
                backup_content,
                expected_sha256=item.get("desired_sha256"),
            )
        else:
            if os.path.lexists(target):
                _atomic_remove_expected_file(
                    target,
                    item.get("desired_sha256") if verify_current else None,
                )
                _remove_empty_parents(target.parent, Path(vault))
    for item, relative, target, _current_sha, _backup in prepared:
        if item.get("existed_before"):
            if (
                not target.is_file()
                or _sha256(target.read_bytes()) != item.get("before_sha256")
            ):
                raise RuntimeError(f"migration output changed during rollback: {relative}")
        elif os.path.lexists(target):
            raise RuntimeError(f"migration output changed during rollback: {relative}")
    return manifest


def _read_sealed_backup_bytes(root_fd, relative, expected_sha256):
    parent_fd, leaf = _open_relative_parent(root_fd, relative)
    try:
        fd = os.open(
            leaf,
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            before = os.fstat(fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o400
                or before.st_mtime_ns != SEALED_BACKUP_MTIME_NS
            ):
                raise RuntimeError(f"sealed backup binding changed: {relative}")
            chunks = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            content = b"".join(chunks)
            digest = _sha256(content)
            after = os.fstat(fd)
            if (
                _stable_stat_identity(before) != _stable_stat_identity(after)
                or digest != expected_sha256
            ):
                raise RuntimeError(
                    f"migration backup changed during rollback: {relative}"
                )
            return content
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _atomic_remove_expected_file(path, expected_sha256):
    path = Path(path)
    parent_fd = os.open(
        path.parent.resolve(),
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        current = _named_stat(parent_fd, path.name)
        if current is None:
            return
        expected_inode, observed_sha = _read_stable_named_file(
            parent_fd,
            path.name,
            path,
        )
        if expected_sha256 is not None and observed_sha != expected_sha256:
            raise RuntimeError(
                f"migration output changed during rollback: {path}"
            )
        _quarantine_remove_name(
            parent_fd,
            path.name,
            expected_inode,
            "memory migration rollback target removal",
        )
        if _named_stat(parent_fd, path.name) is not None:
            raise RuntimeError(
                f"migration output changed during rollback: {path}"
            )
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)


def _read_manifest(path, vault):
    path = Path(path).expanduser().absolute()
    if path.name != "manifest.json":
        raise ValueError("invalid memory migration manifest path")
    migration_id = path.parent.name
    expected_path = Path(
        safe_vault_path(
            vault,
            *ROLLBACK_ROOT,
            migration_id,
            "manifest.json",
        )
    )
    if path != expected_path:
        raise ValueError("memory migration manifest is outside the Vault backup root")
    backup_root = path.parent
    root_fd = os.open(
        backup_root,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    try:
        root_stat = os.fstat(root_fd)
        if (
            stat.S_IMODE(root_stat.st_mode) != 0o500
            or root_stat.st_mtime_ns != SEALED_BACKUP_MTIME_NS
        ):
            raise RuntimeError("sealed backup root mode changed")
        manifest_fd = os.open(
            "manifest.json",
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            before = os.fstat(manifest_fd)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or stat.S_IMODE(before.st_mode) != 0o400
                or before.st_mtime_ns != SEALED_BACKUP_MTIME_NS
            ):
                raise RuntimeError("sealed manifest binding changed")
            chunks = []
            while True:
                chunk = os.read(manifest_fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(manifest_fd)
            if _stable_stat_identity(before) != _stable_stat_identity(after):
                raise RuntimeError("sealed manifest changed while reading")
        finally:
            os.close(manifest_fd)
        try:
            manifest = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid memory migration manifest") from exc
        _validate_manifest_payload(manifest, backup_root)

        expected_files = {Path("manifest.json")}
        for item in manifest["files"]:
            if item["existed_before"]:
                expected_files.add(Path(item["backup"]))
        actual_files, actual_directories = _inventory_staging_tree(root_fd)
        if set(actual_files) != expected_files:
            raise RuntimeError("sealed backup file inventory changed")
        for relative in actual_directories:
            directory_fd = _open_relative_directory(root_fd, relative)
            try:
                current = os.fstat(directory_fd)
                if (
                    not stat.S_ISDIR(current.st_mode)
                    or stat.S_IMODE(current.st_mode) != 0o500
                    or current.st_mtime_ns != SEALED_BACKUP_MTIME_NS
                ):
                    raise RuntimeError("sealed backup directory mode changed")
            finally:
                os.close(directory_fd)
        for item in manifest["files"]:
            if not item["existed_before"]:
                continue
            relative = Path(item["backup"])
            current = _stat_relative(root_fd, relative)
            if (
                not stat.S_ISREG(current.st_mode)
                or current.st_nlink != 1
                or stat.S_IMODE(current.st_mode) != 0o400
                or current.st_mtime_ns != SEALED_BACKUP_MTIME_NS
            ):
                raise RuntimeError(f"sealed backup binding changed: {relative}")
            if _hash_backup_file(root_fd, relative) != item["before_sha256"]:
                raise RuntimeError(f"sealed backup hash changed: {relative}")
        return manifest
    finally:
        os.close(root_fd)


def _validate_manifest_payload(manifest, backup_root):
    required = {
        "schema_version",
        "migration_id",
        "status",
        "vault",
        "created_at",
        "sealed_mtime_ns",
        "files",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise ValueError("invalid memory migration manifest")
    migration_id = str(manifest.get("migration_id") or "")
    if (
        manifest.get("schema_version") != RUNTIME_SCHEMA_VERSION
        or manifest.get("status") != "prepared"
        or manifest.get("sealed_mtime_ns") != SEALED_BACKUP_MTIME_NS
        or normalize_project_slug(migration_id) != migration_id
        or migration_id != backup_root.name
        or not isinstance(manifest.get("files"), list)
    ):
        raise ValueError("invalid memory migration manifest")
    seen_paths = set()
    seen_backups = set()
    digest_pattern = re.compile(r"^[0-9a-f]{64}$")
    item_keys = {
        "path",
        "existed_before",
        "before_sha256",
        "desired_sha256",
        "backup",
    }
    for item in manifest["files"]:
        if not isinstance(item, dict) or set(item) != item_keys:
            raise ValueError("invalid memory migration manifest file entry")
        relative = _relative_path(item.get("path"))
        if relative != item.get("path") or relative in seen_paths:
            raise ValueError("invalid memory migration manifest file path")
        seen_paths.add(relative)
        existed_before = item.get("existed_before")
        before_sha = item.get("before_sha256")
        desired_sha = item.get("desired_sha256")
        backup = item.get("backup")
        if not isinstance(existed_before, bool) or not digest_pattern.fullmatch(
            str(desired_sha or "")
        ):
            raise ValueError("invalid memory migration manifest digest")
        expected_backup = f"files/{relative}" if existed_before else ""
        if backup != expected_backup:
            raise ValueError("invalid memory migration manifest backup path")
        if existed_before:
            if not digest_pattern.fullmatch(str(before_sha or "")):
                raise ValueError("invalid memory migration manifest digest")
            if backup in seen_backups:
                raise ValueError("duplicate memory migration backup path")
            seen_backups.add(backup)
        elif before_sha != "":
            raise ValueError("invalid memory migration manifest digest")


def _split_markdown(content):
    text = content.decode("utf-8")
    frontmatter_text, body = split_frontmatter_text(text)
    if frontmatter_text is None and not text.startswith("---"):
        return {}, text
    if frontmatter_text is None:
        raise ValueError("malformed Markdown frontmatter")
    frontmatter = yaml.safe_load(frontmatter_text) or {}
    if not isinstance(frontmatter, dict):
        raise ValueError("Markdown frontmatter must be a mapping")
    return frontmatter, body.lstrip("\n")


def _render_markdown(frontmatter, body):
    return (
        "---\n"
        + yaml.dump(
            frontmatter,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        + "---\n\n"
        + str(body or "").rstrip()
        + "\n"
    ).encode("utf-8")


def _atomic_write_bytes(
    path,
    content,
    expected_sha256=_NO_EXPECTED_STATE,
):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    parent = path.parent.resolve()
    parent_fd = os.open(
        parent,
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
    )
    tmp_name = path.name + f".tmp-{secrets.token_hex(6)}"
    scratch_inode = None
    scratch_owned = False
    try:
        scratch_inode = _write_new_file(parent_fd, tmp_name, content)
        scratch_owned = True
        current = _named_stat(parent_fd, path.name)
        if expected_sha256 is None:
            if current is not None:
                raise RuntimeError(
                    f"migration target changed during atomic publish: {path}"
                )
            try:
                _rename_exclusive(parent_fd, tmp_name, parent_fd, path.name)
            except FileExistsError as exc:
                raise RuntimeError(
                    f"migration target changed during atomic publish: {path}"
                ) from exc
            scratch_owned = False
            _verify_installed_inode(parent_fd, path.name, scratch_inode, path)
            os.fsync(parent_fd)
            return

        if current is None:
            if expected_sha256 is not _NO_EXPECTED_STATE:
                raise RuntimeError(
                    f"migration target changed during atomic publish: {path}"
                )
            _rename_exclusive(parent_fd, tmp_name, parent_fd, path.name)
            scratch_owned = False
            _verify_installed_inode(parent_fd, path.name, scratch_inode, path)
            os.fsync(parent_fd)
            return

        expected_inode, observed_sha = _read_stable_named_file(
            parent_fd,
            path.name,
            path,
        )
        if (
            expected_sha256 is not _NO_EXPECTED_STATE
            and observed_sha != expected_sha256
        ):
            raise RuntimeError(
                f"migration target changed during atomic publish: {path}"
            )

        _rename_exchange(parent_fd, tmp_name, parent_fd, path.name)
        exchanged = True
        try:
            installed = _named_stat(parent_fd, path.name)
            displaced = _named_stat(parent_fd, tmp_name)
            if (
                installed is None
                or displaced is None
                or _inode_from_stat(installed) != scratch_inode
                or _inode_from_stat(displaced) != expected_inode
            ):
                raise RuntimeError(
                    f"migration target changed during atomic publish: {path}"
                )
            displaced_inode, displaced_sha = _read_stable_named_file(
                parent_fd,
                tmp_name,
                path,
            )
            if displaced_inode != expected_inode or displaced_sha != observed_sha:
                raise RuntimeError(
                    f"migration target changed during atomic publish: {path}"
                )
            _quarantine_remove_name(
                parent_fd,
                tmp_name,
                expected_inode,
                "memory migration displaced target cleanup",
            )
            scratch_owned = False
            exchanged = False
            os.fsync(parent_fd)
        except BaseException as publish_error:
            if exchanged:
                try:
                    _restore_exchanged_target(
                        parent_fd,
                        path.name,
                        tmp_name,
                        scratch_inode,
                    )
                    scratch_owned = False
                except BaseException as recovery_error:
                    scratch_owned = False
                    raise RuntimeError(
                        f"{publish_error}; atomic publish recovery failed: "
                        f"{recovery_error}"
                    ) from publish_error
            raise
    finally:
        if scratch_owned and scratch_inode is not None:
            current = _named_stat(parent_fd, tmp_name)
            if current is not None and _inode_from_stat(current) == scratch_inode:
                _quarantine_remove_name(
                    parent_fd,
                    tmp_name,
                    scratch_inode,
                    "memory migration staging cleanup",
                )
        os.close(parent_fd)


def _read_stable_named_file(parent_fd, name, display_path):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise RuntimeError(
                f"migration target changed during atomic publish: {display_path}"
            )
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        digest = _sha256(b"".join(chunks))
        after = os.fstat(fd)
        if _stable_stat_identity(before) != _stable_stat_identity(after):
            raise RuntimeError(
                f"migration target changed during atomic publish: {display_path}"
            )
        return _inode_from_stat(after), digest
    finally:
        os.close(fd)


def _stable_stat_identity(result):
    return (
        result.st_dev,
        result.st_ino,
        stat.S_IFMT(result.st_mode),
        result.st_nlink,
        result.st_size,
        result.st_mtime_ns,
        result.st_ctime_ns,
    )


def _verify_installed_inode(parent_fd, name, expected_inode, display_path):
    installed = _named_stat(parent_fd, name)
    if (
        installed is None
        or not stat.S_ISREG(installed.st_mode)
        or installed.st_nlink != 1
        or _inode_from_stat(installed) != expected_inode
    ):
        raise RuntimeError(
            f"migration target changed during atomic publish: {display_path}"
        )


def _restore_exchanged_target(
    parent_fd,
    target_name,
    scratch_name,
    installed_inode,
):
    installed = _named_stat(parent_fd, target_name)
    displaced = _named_stat(parent_fd, scratch_name)
    if (
        installed is None
        or displaced is None
        or _inode_from_stat(installed) != installed_inode
        or not stat.S_ISREG(displaced.st_mode)
        or displaced.st_nlink != 1
    ):
        raise RuntimeError("cannot safely restore exchanged migration target")
    displaced_inode = _inode_from_stat(displaced)
    _rename_exchange(parent_fd, scratch_name, parent_fd, target_name)
    restored = _named_stat(parent_fd, target_name)
    scratch = _named_stat(parent_fd, scratch_name)
    if (
        restored is None
        or scratch is None
        or _inode_from_stat(restored) != displaced_inode
        or _inode_from_stat(scratch) != installed_inode
    ):
        raise RuntimeError("atomic publish exchange-back verification failed")
    _quarantine_remove_name(
        parent_fd,
        scratch_name,
        installed_inode,
        "memory migration exchange-back cleanup",
    )
    os.fsync(parent_fd)


def _assert_safe_target(vault, target):
    vault = Path(vault).resolve()
    target = Path(target)
    _assert_inside(vault, target)
    current = vault
    for part in target.relative_to(vault).parts:
        current = current / part
        if os.path.lexists(current) and current.is_symlink():
            raise ValueError(f"migration path contains symlink: {current}")
    if target.exists():
        mode = target.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ValueError(f"migration target is not a regular file: {target}")


def _assert_inside(root, path):
    root = Path(root).resolve()
    path = Path(path).resolve(strict=False)
    if path != root and root not in path.parents:
        raise ValueError(f"path is outside migration root: {path}")


def _relative_path(value):
    path = Path(str(value or ""))
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError(f"invalid relative migration path: {value}")
    return path.as_posix()


def _remove_empty_parents(path, stop):
    path = Path(path)
    stop = Path(stop)
    while path != stop and stop in path.parents:
        try:
            path.rmdir()
        except OSError:
            break
        path = path.parent


def _sha256(content):
    return hashlib.sha256(content).hexdigest()
