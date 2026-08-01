"""Build vault-level keyword and cross-project atom indexes.

This is a conservative v4-inspired layer for the Obsidian-vault workflow:
- keyword-index.json gives agents a small machine-readable retrieval map.
- global-atoms.json/md promotes only pitfalls repeated across projects.
"""
import hashlib
import json
import os
import posixpath
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import yaml

from annotation_quality import (
    annotation_candidate_roots,
    collapse_runtime_duplicates,
    filter_runtime_quality,
    is_annotation_candidate_path,
)
from conversation_summary import (
    REQUIRED_FIELDS as CONVERSATION_SUMMARY_REQUIRED_FIELDS,
    SUMMARY_FIELDS as CONVERSATION_SUMMARY_FIELDS,
    build_conversation_summary_record,
    conversation_summary_source_project,
)
from experience_memory import build_experience_bundles
from graph_projection import sync_graph_projection
from memory_graph import (
    GRAPH_SCHEMA_VERSION,
    add_graph_edge,
    analyze_memory_graph,
    graph_evidence,
    graph_node,
    graph_path_for_index,
    render_memory_graph_quality_markdown,
    upsert_graph_node,
    validate_memory_graph,
)
from memory_schema import (
    MEMORY_RELATION_FIELDS,
    RUNTIME_SCHEMA_VERSION,
    canonical_project,
    formal_identity_key,
    is_runtime_record,
    is_valid_active_project_record,
    is_valid_formal_project_record,
    merge_formal_records,
    normalize_formal_record,
    parse_active_formal_section,
    parse_formal_section,
    suppress_unmet_dependencies,
)
from safety import (
    VAULT_INTERNAL_DIR_NAMES,
    assert_no_symlink_components,
    durable_atomic_write,
    ensure_directory_tree,
    safe_vault_path,
    split_frontmatter_text,
)


CST = timezone(timedelta(hours=8))
DEFAULT_OUTPUT_DIR = "05-Agent-Memory"
DEFAULT_RECALL_INDEX_PATH = "05-Agent-Memory/recall-index.json"
MAX_TERMS_PER_NOTE = 100
MAX_ENTRIES_PER_TERM = 20
SESSION_SUMMARY_PATTERN = re.compile(
    r"(?ms)^## Session Summary\s*\n(.*?)(?=^## |\Z)"
)
LEGACY_SUMMARY_FIELDS = frozenset(
    {"projects", "primary", "decisions", "errors", "session_id", "summary"}
)
LEGACY_SUMMARY_REQUIRED_FIELDS = frozenset({"projects", "primary", "summary"})
LEGACY_DECISION_FIELDS = frozenset({"id", "text", "context"})
LEGACY_ERROR_FIELDS = frozenset({"type", "resolution", "repeated_from"})
MAPPING_LOOKING_LINE = re.compile(r"^\s*[^#\s][^:\n]*:\s*", re.MULTILINE)
YAML_INDICATOR_CHARS = frozenset("-?:,[]{}#&*!|>'\"%@`")
SUMMARY_CURSOR_PATTERN = re.compile(
    r"^(file-bytes|zcode-messages):([0-9]+)$"
)
GENERATED_INDEX_FILES = {
    "keyword-index.md",
    "global-atoms.md",
    "memory-graph-quality.md",
    "recall-context.md",
}
CONFIGURABLE_ADAPTIVE_NOTE_TYPES = frozenset(
    {
        "personal-memory",
        "skill-routing-rules",
        "workflow-rules",
        "insights",
    }
)
STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "into",
    "after",
    "before",
    "context",
    "session",
    "project",
    "memory",
    "error",
    "decision",
    "related",
    "timeline",
    "topic",
    "index",
    "projects",
    "inbox",
    "maps",
    "date",
    "evidence",
    "resolution",
    "encountered",
}
CHINESE_KEY_TERMS = [
    "用户偏好",
    "项目规则",
    "待确认",
    "正式记录",
    "中文",
    "路径",
    "渲染",
    "标注",
    "记忆",
    "图谱",
    "自动化",
    "技能偏好",
    "技能路由",
    "流程记忆",
    "行为规则",
    "先查源码",
    "直接修复",
    "何时用",
    "何时不用",
    "错误",
    "决策",
    "记录",
    "会话",
    "修复",
    "测试",
]


def rebuild_vault_knowledge_indexes(cfg):
    """Rebuild keyword and global atom indexes.

    Returns a summary dict suitable for logging/frontmatter.
    """
    vault = cfg.get("vault_path")
    if not vault or not os.path.isdir(vault):
        return {"keyword_terms": 0, "global_atoms": 0, "written": []}

    output_dir = os.path.join(vault, DEFAULT_OUTPUT_DIR)
    ensure_directory_tree(output_dir, vault)

    candidate_roots = (
        *adaptive_candidate_roots(cfg),
        *error_evidence_candidate_roots(cfg),
        *annotation_candidate_roots(cfg),
        *promotion_proposal_roots(cfg),
    )
    notes = collect_indexable_notes(
        vault,
        excluded_roots=candidate_roots,
        additional_note_types=configured_adaptive_formal_paths(cfg),
    )
    keyword_index = build_keyword_index(notes)
    atoms = build_global_atoms(vault)
    recall_index = build_recall_index(notes)
    memory_graph = build_memory_graph(notes, recall_index)
    graph_quality = validate_memory_graph(
        memory_graph,
        recall_index.get("units"),
        allow_legacy=False,
        expected_generation_id=recall_index.get("generation_id", ""),
    )

    keyword_path = os.path.join(output_dir, "keyword-index.json")
    keyword_md_path = os.path.join(output_dir, "keyword-index.md")
    atoms_path = os.path.join(output_dir, "global-atoms.json")
    atoms_md_path = os.path.join(output_dir, "global-atoms.md")
    recall_path = configured_recall_index_path(cfg)
    ensure_directory_tree(os.path.dirname(recall_path), vault)
    graph_path = graph_path_for_index(recall_path)
    graph_quality_path = os.path.join(output_dir, "memory-graph-quality.md")
    recall_md_path = os.path.join(output_dir, "recall-context.md")

    atomic_write_json(keyword_path, keyword_index, root=vault)
    atomic_write_text(
        keyword_md_path,
        render_keyword_index_markdown(keyword_index),
        root=vault,
    )
    atomic_write_json(atoms_path, atoms, root=vault)
    atomic_write_text(
        atoms_md_path,
        render_global_atoms_markdown(atoms),
        root=vault,
    )
    atomic_write_json(recall_path, recall_index, root=vault)
    atomic_write_json(graph_path, memory_graph, root=vault)
    atomic_write_text(
        graph_quality_path,
        render_memory_graph_quality_markdown(
            memory_graph,
            recall_index.get("units"),
        ),
        root=vault,
    )
    atomic_write_text(
        recall_md_path,
        render_recall_context_markdown(recall_index, memory_graph),
        root=vault,
    )
    projection = {
        "nodes": 0,
        "edges": 0,
        "written": 0,
        "removed": 0,
        "root": "",
    }
    projection_cfg = dict(cfg.get("graph_projection") or {})
    if projection_cfg.get("enabled", True):
        projection = sync_graph_projection(
            vault,
            memory_graph,
            projection_cfg,
        )

    return {
        "keyword_terms": len(keyword_index.get("keywords", {})),
        "global_atoms": len(atoms.get("atoms", [])),
        "recall_units": len(recall_index.get("units", [])),
        "graph_nodes": len(memory_graph.get("nodes", [])),
        "graph_edges": len(memory_graph.get("edges", [])),
        "graph_invalid_edges": graph_quality["invalid_edges"],
        "graph_missing_evidence": graph_quality["missing_evidence"],
        "graph_unbound_evidence": graph_quality["unbound_evidence"],
        "graph_missing_memory_nodes": graph_quality["missing_memory_nodes"],
        "graph_generation_id": recall_index.get("generation_id", ""),
        "graph_projection_nodes": projection["nodes"],
        "graph_projection_edges": projection["edges"],
        "graph_projection_written": projection["written"],
        "graph_projection_removed": projection["removed"],
        "graph_projection_root": projection["root"],
        "written": [
            keyword_path,
            keyword_md_path,
            atoms_path,
            atoms_md_path,
            recall_path,
            graph_path,
            graph_quality_path,
            recall_md_path,
        ],
    }


def collect_indexable_notes(
    vault,
    excluded_roots=None,
    additional_note_types=None,
):
    notes = []
    restrict_adaptive_paths = additional_note_types is not None
    additional_note_types = {
        os.path.abspath(path): note_type
        for path, note_type in dict(additional_note_types or {}).items()
    }
    seen_paths = set()
    excluded_roots = tuple(excluded_roots or (DEFAULT_ERROR_EVIDENCE_DIR,))
    roots = [
        os.path.join(vault, "01-Projects"),
        os.path.join(vault, "05-Agent-Memory"),
    ]
    for root in roots:
        if not os.path.exists(root):
            continue
        if os.path.isfile(root):
            paths = [root]
        else:
            paths = []
            for current, dirs, files in os.walk(root):
                dirs[:] = [
                    directory
                    for directory in dirs
                    if directory not in VAULT_INTERNAL_DIR_NAMES
                    and not os.path.islink(os.path.join(current, directory))
                    and not is_error_evidence_candidate_path(
                        os.path.relpath(
                            os.path.join(current, directory),
                            vault,
                        ).replace(os.sep, "/"),
                        excluded_roots,
                    )
                ]
                for filename in files:
                    if filename.endswith(".md"):
                        paths.append(os.path.join(current, filename))
        for path in sorted(paths):
            absolute_path = os.path.abspath(path)
            if absolute_path in seen_paths:
                continue
            rel_path = os.path.relpath(path, vault).replace(os.sep, "/")
            if os.path.islink(path):
                continue
            if is_error_evidence_candidate_path(rel_path, excluded_roots):
                continue
            if (
                restrict_adaptive_paths
                and absolute_path not in additional_note_types
                and note_type_from_path(rel_path.removesuffix(".md"))
                in CONFIGURABLE_ADAPTIVE_NOTE_TYPES
            ):
                continue
            if os.path.basename(path).startswith("_"):
                continue
            if os.path.basename(path) in GENERATED_INDEX_FILES:
                continue
            note = read_note(
                path,
                vault,
                note_type=additional_note_types.get(absolute_path),
            )
            if note and is_indexable_note(note):
                notes.append(note)
                seen_paths.add(absolute_path)
    for path, note_type in sorted(additional_note_types.items()):
        if path in seen_paths or not os.path.isfile(path) or os.path.islink(path):
            continue
        note = read_note(path, vault, note_type=note_type)
        if note and is_indexable_note(note):
            notes.append(note)
            seen_paths.add(path)
    return notes


def read_note(path, vault, note_type=None):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return None
    fm, body = split_frontmatter(content)
    rel_path = os.path.relpath(path, vault).replace(os.sep, "/")
    if rel_path.endswith(".md"):
        rel_path = rel_path[:-3]
    title = (
        fm.get("ai_title")
        or fm.get("title")
        or first_heading(body)
        or os.path.basename(rel_path)
    )
    return {
        "path": rel_path,
        "title": str(title),
        "project": str(fm.get("project", "")),
        "type": note_type or note_type_from_path(rel_path),
        "text": searchable_text(fm, body),
        "frontmatter": fm,
        "body": body,
        "links": extract_wikilinks(body),
    }


def configured_adaptive_formal_paths(cfg):
    vault = cfg.get("vault_path")
    if not vault:
        return {}
    configured = {}
    for section, default, note_type in (
        (
            "personal_memory",
            "05-Agent-Memory/personal-memory.md",
            "personal-memory",
        ),
        (
            "skill_preferences",
            "05-Agent-Memory/skill-routing-rules.md",
            "skill-routing-rules",
        ),
        (
            "workflow_memory",
            "05-Agent-Memory/workflow-rules.md",
            "workflow-rules",
        ),
        (
            "insight_memory",
            "05-Agent-Memory/insights.md",
            "insights",
        ),
    ):
        raw = (cfg.get(section) or {}).get("formal_path", default)
        try:
            configured[safe_vault_path(vault, raw)] = note_type
        except ValueError:
            continue
    return configured


def configured_recall_index_path(cfg):
    vault = cfg.get("vault_path")
    raw = (cfg.get("memory_runtime") or {}).get(
        "index_path",
        DEFAULT_RECALL_INDEX_PATH,
    )
    return safe_vault_path(vault, raw)


def build_keyword_index(notes):
    keywords = defaultdict(list)
    now = datetime.now(CST).isoformat()
    for note in notes:
        if not is_indexable_note(note):
            continue
        terms = extract_terms(
            " ".join([note["title"], note["project"], note["text"]]),
            limit=MAX_TERMS_PER_NOTE,
        )
        for term in terms:
            entry = {
                "path": note["path"],
                "title": note["title"],
                "type": note["type"],
            }
            if note.get("project"):
                entry["project"] = note["project"]
            if entry not in keywords[term]:
                keywords[term].append(entry)

    compact = {}
    for term, entries in keywords.items():
        entries.sort(key=lambda item: (item.get("project", ""), item["path"]))
        compact[term] = entries[:MAX_ENTRIES_PER_TERM]

    return {
        "schema_version": "1.0",
        "generated_by": "knowledge_index.py",
        "generated_at": now,
        "term_count": len(compact),
        "keywords": dict(sorted(compact.items())),
    }


def build_recall_index(notes):
    records = []
    inactive_identities = set()
    hard_tombstone_identities = set()
    authorized_successors = defaultdict(set)
    for note in notes:
        if not is_indexable_note(note):
            continue
        records.extend(recall_units_from_note(note))
        for record in inactive_recall_units_from_note(note):
            identity = formal_identity_key(record)
            inactive_identities.add(identity)
            successor = str(record.get("superseded_by") or "").strip()
            if record.get("status") == "superseded" and successor:
                authorized_successors[identity].add(successor)
            else:
                hard_tombstone_identities.add(identity)

    candidates = [
        record
        for record in merge_formal_records(records)
        if is_runtime_record(record)
        and not is_session_memory_path(record.get("path"))
        and (
            formal_identity_key(record) not in inactive_identities
            or (
                formal_identity_key(record) not in hard_tombstone_identities
                and record.get("id")
                in authorized_successors.get(formal_identity_key(record), set())
            )
        )
    ]
    quality_eligible, suppressed_quality = filter_runtime_quality(candidates)
    eligible, suppressed = suppress_unmet_dependencies(quality_eligible)
    eligible, duplicate_groups = collapse_runtime_duplicates(eligible)
    units = [
        enrich_runtime_unit(record)
        for record in eligible
    ]

    units.sort(
        key=lambda item: (
            str(item.get("project", "")),
            str(item.get("type", "")),
            str(item.get("date", "")),
            str(item.get("title", "")),
        ),
        reverse=True,
    )

    terms = defaultdict(list)
    projects = defaultdict(list)
    types = defaultdict(list)
    for unit in units:
        for term in unit.get("terms", []):
            append_unique(terms[term], unit["id"])
        if unit.get("project"):
            append_unique(projects[unit["project"]], unit["id"])
        append_unique(types[unit["type"]], unit["id"])

    experience_bundles = build_experience_bundles(units)
    conversation_summaries = build_conversation_summaries(notes)

    index = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "generated_by": "knowledge_index.py",
        "generated_at": datetime.now(CST).isoformat(),
        "unit_count": len(units),
        "units": units,
        "experience_bundles": experience_bundles,
        "conversation_summary_count": len(conversation_summaries),
        "conversation_summaries": conversation_summaries,
        "suppressed_dependencies": suppressed,
        "suppressed_quality": suppressed_quality,
        "duplicate_groups": duplicate_groups,
        "terms": {k: v[:50] for k, v in sorted(terms.items())},
        "projects": {k: v[:100] for k, v in sorted(projects.items())},
        "types": {k: v[:100] for k, v in sorted(types.items())},
    }
    index["generation_id"] = memory_generation_id(notes, index)
    return index


def build_conversation_summaries(notes):
    """Derive one latest non-formal summary for each stable session ID."""
    latest = {}
    for note in notes or []:
        record = conversation_summary_from_note(note)
        if record is None:
            continue
        session_id = record["session_id"]
        candidate = (_conversation_summary_freshness(note, record), record)
        current = latest.get(session_id)
        if current is None or candidate[0] > current[0]:
            latest[session_id] = candidate
    return sorted(
        (candidate[1] for candidate in latest.values()),
        key=lambda item: (
            str(item.get("project") or ""),
            str(item.get("date") or ""),
            str(item.get("title") or ""),
            str(item.get("session_id") or ""),
            str(item.get("id") or ""),
        ),
        reverse=True,
    )


def conversation_summary_from_note(note):
    """Return a Task 1 summary record from one persisted session note."""
    if not is_indexable_note(note) or note.get("type") != "session":
        return None
    frontmatter = note.get("frontmatter")
    if not isinstance(frontmatter, dict):
        return None
    source_note = str(note.get("path") or "")
    source_project = conversation_summary_source_project(source_note)
    if not source_project:
        return None
    declared_projects = {
        str(value).strip()
        for value in (frontmatter.get("project"), note.get("project"))
        if str(value or "").strip()
    }
    if declared_projects and declared_projects != {source_project}:
        return None
    payload = _conversation_summary_payload(note)
    if payload is None:
        return None

    payload_project = str(payload.get("project") or "").strip()
    if payload_project and payload_project != source_project:
        return None
    payload = dict(payload)
    payload["project"] = source_project
    return build_conversation_summary_record(
        {
            "frontmatter": frontmatter,
            "source_note": source_note,
            "title": str(note.get("title") or ""),
            "conversation_summary": payload,
        }
    )


def _conversation_summary_payload(note):
    match = SESSION_SUMMARY_PATTERN.search(str(note.get("body") or ""))
    if not match:
        return None
    summary_text = match.group(1).strip()
    if not summary_text:
        return None
    try:
        parsed = yaml.safe_load(summary_text)
    except yaml.YAMLError:
        if not _clearly_plain_conversation_summary(summary_text):
            return None
        parsed = summary_text

    if isinstance(parsed, dict):
        keys = set(parsed)
        structured_fields = set(CONVERSATION_SUMMARY_FIELDS)
        if keys & (structured_fields - {"summary"}):
            if not CONVERSATION_SUMMARY_REQUIRED_FIELDS.issubset(keys):
                return None
            return dict(parsed)
        if _valid_legacy_conversation_summary(parsed):
            return _legacy_conversation_summary_payload(
                note,
                parsed["summary"],
            )
        return None
    if (
        isinstance(parsed, str)
        and _clearly_plain_conversation_summary(summary_text)
    ):
        return _legacy_conversation_summary_payload(note, parsed)
    return None


def _clearly_plain_conversation_summary(summary_text):
    if not isinstance(summary_text, str):
        return False
    stripped = summary_text.strip()
    if not stripped or MAPPING_LOOKING_LINE.search(stripped):
        return False
    for line in stripped.splitlines():
        candidate = line.lstrip()
        if candidate and (
            candidate[0] in YAML_INDICATOR_CHARS
            or candidate.startswith("...")
        ):
            return False
    return True


def _valid_legacy_conversation_summary(payload):
    if (
        not isinstance(payload, dict)
        or not LEGACY_SUMMARY_REQUIRED_FIELDS.issubset(payload)
        or set(payload) - LEGACY_SUMMARY_FIELDS
        or not isinstance(payload.get("summary"), str)
        or not payload["summary"].strip()
        or not isinstance(payload.get("primary"), str)
        or not payload["primary"].strip()
        or not isinstance(payload.get("projects"), list)
        or not payload["projects"]
        or any(
            not isinstance(item, str) or not item.strip()
            for item in payload["projects"]
        )
        or not _valid_legacy_entries(
            payload.get("decisions"),
            LEGACY_DECISION_FIELDS,
            optional=True,
        )
        or not _valid_legacy_entries(
            payload.get("errors"),
            LEGACY_ERROR_FIELDS,
            list_fields={"repeated_from"},
            optional=True,
        )
        or (
            "session_id" in payload
            and not _valid_legacy_scalar(payload.get("session_id"))
        )
    ):
        return False
    return True


def _valid_legacy_entries(
    value,
    allowed_fields,
    *,
    list_fields=frozenset(),
    optional=False,
):
    if value is None:
        return optional
    if not isinstance(value, list):
        return False
    for item in value:
        if (
            not isinstance(item, dict)
            or not item
            or set(item) - set(allowed_fields)
        ):
            return False
        for key, field_value in item.items():
            if key in list_fields:
                if (
                    not isinstance(field_value, list)
                    or any(not isinstance(member, str) for member in field_value)
                ):
                    return False
            elif not isinstance(field_value, str):
                return False
    return True


def _valid_legacy_scalar(value):
    return (
        isinstance(value, (str, int, float, bool))
        and (not isinstance(value, str) or bool(value.strip()))
    )


def _legacy_conversation_summary_payload(note, summary):
    title = str(note.get("title") or "").strip()
    project = conversation_summary_source_project(str(note.get("path") or ""))
    return {
        "project": project,
        "current_goal": summary,
        "topics": [title] if title else [],
        "progress": [],
        "constraints": [],
        "important_context": [],
        "open_items": [],
        "summary": summary,
    }


def _conversation_summary_freshness(note, record):
    frontmatter = note.get("frontmatter") or {}
    checkpoint = frontmatter.get("summary_checkpoint")
    if isinstance(checkpoint, bool) or not isinstance(checkpoint, int):
        checkpoint = -1
    timestamp = _summary_timestamp(frontmatter.get("summary_updated_at"))
    cursor = _summary_cursor(frontmatter.get("summary_source_cursor"))
    return (
        cursor is not None,
        cursor[0] if cursor else "",
        cursor[1] if cursor else -1,
        timestamp is not None,
        timestamp or datetime.min.replace(tzinfo=timezone.utc),
        checkpoint,
        str(record.get("date") or ""),
        str(note.get("path") or ""),
        str(record.get("summary_revision") or ""),
    )


def _summary_timestamp(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip() == value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _summary_cursor(value):
    if not isinstance(value, str):
        return None
    match = SUMMARY_CURSOR_PATTERN.fullmatch(value)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def is_session_memory_path(path):
    normalized = "/" + str(path or "").replace("\\", "/").lstrip("/")
    canonical = posixpath.normpath(normalized)
    marker = "/memory/sessions/"
    return marker in normalized.casefold() or marker in canonical.casefold()


def recall_units_from_note(note):
    if not is_indexable_note(note):
        return []
    if note.get("type") == "session":
        return []
    fm = note.get("frontmatter") or {}
    body = note.get("body") or ""
    units = []
    source_note = f"note:{note['path']}"
    if note["type"] in {
        "decisions",
        "pitfalls",
        "personal-memory",
        "skill-routing-rules",
        "workflow-rules",
        "insights",
    } and fm.get("schema_version") != RUNTIME_SCHEMA_VERSION:
        return []

    if note["type"] in {"decisions", "pitfalls"}:
        expected_project = aggregate_project_from_path(note["path"])
        if (
            not expected_project
            or str(fm.get("project") or "").strip() != expected_project
        ):
            return []
        key = "decisions" if note["type"] == "decisions" else "pitfalls"
        memory_type = "decision" if key == "decisions" else "error"
        for record in list_items(fm, key):
            if not is_valid_active_project_record(
                record,
                memory_type,
                expected_project,
            ):
                continue
            raw = dict(record)
            raw.update({"path": note["path"], "source_note": source_note})
            units.append(
                normalize_formal_record(
                    raw,
                    memory_type=memory_type,
                    default_project=expected_project,
                    source_ref=source_note,
                    date=fm.get("date") or "",
                )
            )

    if note["type"] == "personal-memory":
        for entry in parse_personal_memory_entries(body):
            raw = dict(entry)
            raw.update({"path": note["path"], "source_note": source_note})
            units.append(
                normalize_formal_record(
                    raw,
                    memory_type=entry.get("type") or "preference",
                    default_project=entry.get("project", ""),
                    source_ref=source_note,
                )
            )

    if note["type"] == "skill-routing-rules":
        for entry in parse_skill_routing_entries(body):
            raw = dict(entry)
            raw.update({"path": note["path"], "source_note": source_note})
            units.append(
                normalize_formal_record(
                    raw,
                    memory_type="skill",
                    default_project=entry.get("project", ""),
                    source_ref=source_note,
                )
            )

    if note["type"] == "workflow-rules":
        for entry in parse_workflow_rule_entries(body):
            raw = dict(entry)
            raw.update({"path": note["path"], "source_note": source_note})
            units.append(
                normalize_formal_record(
                    raw,
                    memory_type="workflow",
                    default_project=entry.get("project", ""),
                    source_ref=source_note,
                )
            )

    if note["type"] == "insights":
        for entry in parse_insight_entries(body):
            raw = dict(entry)
            raw.update({"path": note["path"], "source_note": source_note})
            units.append(
                normalize_formal_record(
                    raw,
                    memory_type="insight",
                    default_project=entry.get("project", ""),
                    source_ref=source_note,
                )
            )

    for unit in units:
        unit["source_kind"] = note.get("type", "")
    return units


def inactive_recall_units_from_note(note):
    """Return validated inactive formal facts used as exact recall tombstones."""
    if not is_indexable_note(note) or note.get("type") == "session":
        return []
    fm = note.get("frontmatter") or {}
    body = note.get("body") or ""
    if note.get("type") in {
        "decisions",
        "pitfalls",
        "personal-memory",
        "skill-routing-rules",
        "workflow-rules",
        "insights",
    } and fm.get("schema_version") != RUNTIME_SCHEMA_VERSION:
        return []
    source_note = f"note:{note['path']}"
    units = []
    if note["type"] in {"decisions", "pitfalls"}:
        expected_project = aggregate_project_from_path(note["path"])
        if (
            not expected_project
            or str(fm.get("project") or "").strip() != expected_project
        ):
            return []
        key = "decisions" if note["type"] == "decisions" else "pitfalls"
        memory_type = "decision" if key == "decisions" else "error"
        for record in list_items(fm, key):
            if record.get("status") == "active" or not is_valid_formal_project_record(
                record,
                memory_type,
                expected_project,
            ):
                continue
            raw = dict(record)
            raw.update({"path": note["path"], "source_note": source_note})
            units.append(
                normalize_formal_record(
                    raw,
                    memory_type=memory_type,
                    default_project=expected_project,
                    source_ref=source_note,
                    date=fm.get("date") or "",
                )
            )
        return units

    kind_by_type = {
        "personal-memory": "personal",
        "skill-routing-rules": "skill",
        "workflow-rules": "workflow",
        "insights": "insight",
    }
    kind = kind_by_type.get(note.get("type"))
    if not kind:
        return []
    for entry in parse_adaptive_entries(body, kind, active_only=False):
        if entry.get("status") == "active":
            continue
        raw = dict(entry)
        raw.update({"path": note["path"], "source_note": source_note})
        units.append(
            normalize_formal_record(
                raw,
                memory_type=entry["type"],
                default_project=entry.get("project", ""),
                source_ref=source_note,
            )
        )
    return units


def enrich_runtime_unit(record):
    unit = dict(record)
    supplied_terms = list(unit.get("terms") or [])
    unit["title"] = str(unit.get("title") or unit.get("type") or "")
    unit["summary"] = str(unit.get("summary") or "")
    operational_text = " | ".join(
        f"{field}: {str(unit.get(field) or '').strip()}"
        for field in ("name", "when", "avoid", "trigger", "behavior")
        if str(unit.get(field) or "").strip()
    )
    insight_text = " | ".join(
        part
        for part in (
            f"novelty: {str(unit.get('novelty') or '').strip()}",
            "transfer: " + ", ".join(unit.get("transfer") or []),
            f"boundary: {str(unit.get('boundary') or '').strip()}",
        )
        if part.split(":", 1)[-1].strip()
    ) if unit.get("type") == "insight" else ""
    recall_text = " | ".join(
        part for part in (unit["summary"], operational_text, insight_text) if part
    )
    unit["recall_summary"] = compact_text(recall_text, 600)
    unit["path"] = str(unit.get("path") or "")
    unit["source_note"] = str(unit.get("source_note") or "")
    trusted_search_text = " ".join(
        [
            unit["title"],
            unit.get("project", ""),
            unit["summary"],
            operational_text,
            insight_text,
        ]
    )
    unit["terms"] = extract_terms(trusted_search_text, limit=60)
    normalized_search_text = re.sub(
        r"\s+",
        " ",
        trusted_search_text.casefold(),
    )
    for term in supplied_terms:
        term = str(term or "").strip()
        if (
            term
            and term.casefold() in normalized_search_text
            and term not in unit["terms"]
        ):
            unit["terms"].append(term)
    return unit


def memory_generation_id(notes, recall_index):
    """Bind one recall index and graph to the same deterministic Vault snapshot."""
    note_snapshot = [
        {
            "path": str(note.get("path") or ""),
            "title": str(note.get("title") or ""),
            "type": str(note.get("type") or ""),
            "project": str(note.get("project") or ""),
            "date": str((note.get("frontmatter") or {}).get("date") or ""),
            "links": sorted(str(link) for link in note.get("links") or []),
        }
        for note in notes or []
        if is_indexable_note(note)
    ]
    payload = {
        "notes": sorted(note_snapshot, key=lambda item: item["path"]),
        "units": recall_index.get("units") or [],
        "experience_bundles": recall_index.get("experience_bundles") or [],
        "conversation_summaries": (
            recall_index.get("conversation_summaries") or []
        ),
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def graph_source_revision(kind, payload):
    """Hash the behavior-affecting state of a non-memory graph source."""
    return hashlib.sha256(
        json.dumps(
            {
                "kind": str(kind or ""),
                "payload": payload,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def make_recall_unit(unit_type, title, content, path, project, date, source_note):
    content = str(content or "").strip()
    title = str(title or unit_type).strip()
    search_text = " ".join([title, project or "", content])
    return {
        "id": recall_unit_id(unit_type, path, title, content),
        "type": unit_type,
        "title": title[:160],
        "path": path,
        "project": project or "",
        "date": str(date or ""),
        "source_note": source_note,
        "summary": compact_text(content, 360),
        "terms": extract_terms(search_text, limit=60),
    }


def build_memory_graph(notes, recall_index):
    nodes = {}
    edges = {}
    generation_id = str(recall_index.get("generation_id") or "")
    if not generation_id:
        generation_id = memory_generation_id(notes, recall_index)

    for note in notes:
        if not is_indexable_note(note):
            continue
        note_id = f"note:{note['path']}"
        note_revision = graph_source_revision(
            "note",
            {
                "path": note.get("path", ""),
                "type": note.get("type", ""),
                "title": note.get("title", ""),
                "project": note.get("project", ""),
                "date": str(
                    (note.get("frontmatter") or {}).get("date") or ""
                ),
                "links": sorted(str(link) for link in note.get("links") or []),
            },
        )
        add_node(
            nodes,
            note_id,
            "note",
            note.get("type") or "note",
            note["title"],
            note["path"],
            note.get("project", ""),
            date=(note.get("frontmatter") or {}).get("date", ""),
            revision=note_revision,
            source_refs=[note_id],
        )
        if note.get("project"):
            project_id = f"project:{note['project']}"
            add_node(
                nodes,
                project_id,
                "project",
                "project",
                note["project"],
                "",
                note["project"],
                source_refs=[note_id],
            )
            add_edge(
                edges,
                nodes,
                note_id,
                project_id,
                "belongs_to",
                source_ref=note_id,
                source_revision=note_revision,
                observed_at=(note.get("frontmatter") or {}).get("date", ""),
                derivation="note-frontmatter",
            )
        for link in note.get("links", []):
            target_id = f"note:{link}"
            add_node(
                nodes,
                target_id,
                "note",
                "reference",
                os.path.basename(link),
                link,
                "",
                source_refs=[note_id],
                resolved=False,
            )
            add_edge(
                edges,
                nodes,
                note_id,
                target_id,
                "links_to",
                source_ref=note_id,
                source_revision=note_revision,
                observed_at=(note.get("frontmatter") or {}).get("date", ""),
                derivation="wikilink",
            )

    units = [
        unit
        for unit in recall_index.get("units", [])
        if not is_error_evidence_candidate_path(unit.get("path"))
    ]
    units_by_id = {
        unit.get("id"): unit
        for unit in units
        if isinstance(unit, dict) and unit.get("id")
    }
    for unit in units:
        if is_error_evidence_candidate_path(unit.get("path")):
            continue
        source_note = unit.get("source_note")
        add_node(
            nodes,
            unit["id"],
            "memory",
            unit["type"],
            unit["title"],
            unit.get("path", ""),
            unit.get("project", ""),
            date=unit.get("date", ""),
            revision=unit.get("revision", ""),
            source_refs=memory_graph_source_refs(unit),
        )
        if source_note and source_note != unit["id"]:
            if source_note not in nodes:
                add_node(
                    nodes,
                    source_note,
                    "note",
                    "reference",
                    source_note.removeprefix("note:"),
                    source_note.removeprefix("note:"),
                    unit.get("project", ""),
                    source_refs=[source_note],
                    resolved=False,
                )
            add_edge(
                edges,
                nodes,
                unit["id"],
                source_note,
                "recorded_in",
                source_ref=source_note,
                source_revision=unit.get("revision", ""),
                observed_at=unit.get("date", ""),
                derivation="formal-record",
            )
        if unit.get("project"):
            project_id = f"project:{unit['project']}"
            add_node(
                nodes,
                project_id,
                "project",
                "project",
                unit["project"],
                "",
                unit["project"],
                source_refs=unit.get("source_refs"),
            )
            add_edge(
                edges,
                nodes,
                unit["id"],
                project_id,
                "belongs_to",
                source_ref=source_note or unit["id"],
                source_revision=unit.get("revision", ""),
                observed_at=unit.get("date", ""),
                derivation="formal-record",
            )

    for unit in units:
        for dependency in unit.get("requires") or []:
            target = units_by_id.get(dependency)
            if target:
                add_node(
                    nodes,
                    target["id"],
                    "memory",
                    target["type"],
                    target["title"],
                    target.get("path", ""),
                    target.get("project", ""),
                    date=target.get("date", ""),
                    revision=target.get("revision", ""),
                    source_refs=memory_graph_source_refs(target),
                )
            else:
                add_node(
                    nodes,
                    dependency,
                    "memory",
                    "reference",
                    dependency,
                    "",
                    unit.get("project", ""),
                    source_refs=[unit.get("source_note") or unit["id"]],
                    resolved=False,
                )
            add_edge(
                edges,
                nodes,
                unit["id"],
                dependency,
                "depends_on",
                source_ref=unit.get("source_note") or unit["id"],
                source_revision=unit.get("revision", ""),
                observed_at=unit.get("date", ""),
                derivation="formal-record",
            )
        superseded_by = unit.get("superseded_by")
        if superseded_by:
            target = units_by_id.get(superseded_by)
            if target:
                add_node(
                    nodes,
                    target["id"],
                    "memory",
                    target["type"],
                    target["title"],
                    target.get("path", ""),
                    target.get("project", ""),
                    date=target.get("date", ""),
                    revision=target.get("revision", ""),
                    source_refs=memory_graph_source_refs(target),
                )
            else:
                add_node(
                    nodes,
                    superseded_by,
                    "memory",
                    "reference",
                    superseded_by,
                    "",
                    unit.get("project", ""),
                    source_refs=[unit.get("source_note") or unit["id"]],
                    resolved=False,
                )
            add_edge(
                edges,
                nodes,
                unit["id"],
                superseded_by,
                "superseded_by",
                source_ref=unit.get("source_note") or unit["id"],
                source_revision=unit.get("revision", ""),
                observed_at=unit.get("date", ""),
                derivation="formal-record",
            )
        add_declared_memory_relations(nodes, edges, unit, units_by_id)
        if unit.get("type") == "insight":
            add_insight_graph_relations(nodes, edges, unit, units_by_id)

    for bundle in recall_index.get("experience_bundles") or []:
        if not isinstance(bundle, dict) or not bundle.get("id"):
            continue
        project = str(bundle.get("project") or "")
        session_ref = str(bundle.get("session_ref") or "")
        bundle_revision = graph_source_revision(
            "experience",
            {
                "id": bundle.get("id", ""),
                "project": project,
                "session_ref": session_ref,
                "date": bundle.get("date", ""),
                "members": bundle.get("members") or [],
            },
        )
        add_node(
            nodes,
            bundle["id"],
            "experience",
            "session-bundle",
            f"{project}: {session_ref.removeprefix('session:')}",
            "",
            project,
            date=bundle.get("date", ""),
            revision=bundle_revision,
            source_refs=[session_ref],
        )
        if project:
            project_id = f"project:{project}"
            add_node(
                nodes,
                project_id,
                "project",
                "project",
                project,
                "",
                project,
                source_refs=[session_ref],
            )
            add_edge(
                edges,
                nodes,
                bundle["id"],
                project_id,
                "belongs_to",
                source_ref=session_ref,
                source_revision=bundle_revision,
                observed_at=bundle.get("date", ""),
                derivation="experience-bundle",
            )
        for member in bundle.get("members") or []:
            if not isinstance(member, dict):
                continue
            unit = units_by_id.get(member.get("id"))
            if (
                unit
                and unit.get("revision") == member.get("revision")
                and unit.get("project") == project
            ):
                add_edge(
                    edges,
                    nodes,
                    unit["id"],
                    bundle["id"],
                    "part_of_experience",
                    source_ref=session_ref,
                    source_revision=unit.get("revision", ""),
                    observed_at=member.get("date", ""),
                    derivation="experience-bundle",
                )

    graph = {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "generated_by": "knowledge_index.py",
        "generated_at": datetime.now(CST).isoformat(),
        "generation_id": generation_id,
        "nodes": sorted(nodes.values(), key=lambda item: item["id"]),
        "edges": sorted(
            edges.values(),
            key=lambda item: (item["source"], item["relation"], item["target"]),
        ),
    }
    graph["quality"] = analyze_memory_graph(
        graph,
        units,
        expected_generation_id=generation_id,
    )
    validate_memory_graph(
        graph,
        units,
        allow_legacy=False,
        expected_generation_id=generation_id,
    )
    return graph


def is_indexable_note(note):
    if not isinstance(note, dict):
        return False
    return (
        note.get("type") != "error-evidence-candidate"
        and note.get("type") != "annotation-candidate"
        and (note.get("frontmatter") or {}).get("type")
        != "error-evidence-candidate"
        and (note.get("frontmatter") or {}).get("type")
        != "annotation-candidate"
        and not is_error_evidence_candidate_path(note.get("path"))
        and not is_annotation_candidate_path(note.get("path"))
    )


DEFAULT_ERROR_EVIDENCE_DIR = "04-Feedback/_error-candidates"
DEFAULT_INSIGHT_CANDIDATE_DIR = "04-Feedback/_insight-candidates"
DEFAULT_PROMOTION_PROPOSAL_DIR = "04-Feedback/_promotion-proposals"
ADAPTIVE_CANDIDATE_DIRS = (
    ("personal_memory", "04-Feedback/_memory-candidates"),
    ("skill_preferences", "04-Feedback/_skill-preferences"),
    ("workflow_memory", "04-Feedback/_workflow-candidates"),
    ("insight_memory", DEFAULT_INSIGHT_CANDIDATE_DIR),
)


def error_evidence_candidate_roots(cfg):
    vault = cfg.get("vault_path")
    raw = (cfg.get("error_evidence") or {}).get(
        "candidate_dir",
        DEFAULT_ERROR_EVIDENCE_DIR,
    )
    candidate = safe_vault_path(vault, raw)
    assert_no_symlink_components(candidate, vault)
    relative = os.path.relpath(candidate, vault).replace(os.sep, "/")
    return (relative, DEFAULT_ERROR_EVIDENCE_DIR)


def is_error_evidence_candidate_path(path, candidate_roots=None):
    canonical = posixpath.normpath(
        "/" + str(path or "").replace("\\", "/").lstrip("/")
    ).casefold()
    for root in candidate_roots or (DEFAULT_ERROR_EVIDENCE_DIR,):
        root_canonical = posixpath.normpath(
            "/" + str(root or "").replace("\\", "/").lstrip("/")
        ).casefold()
        if canonical == root_canonical or canonical.startswith(root_canonical + "/"):
            return True
    return False


def adaptive_candidate_roots(cfg):
    """Return every configured adaptive candidate root plus its default."""
    vault = cfg.get("vault_path")
    roots = []
    for section, default in ADAPTIVE_CANDIDATE_DIRS:
        raw = (cfg.get(section) or {}).get("candidate_dir", default)
        candidate = safe_vault_path(vault, raw)
        assert_no_symlink_components(candidate, vault)
        roots.extend(
            (
                os.path.relpath(candidate, vault).replace(os.sep, "/"),
                default,
            )
        )
    return tuple(dict.fromkeys(roots))


def promotion_proposal_roots(cfg):
    vault = cfg.get("vault_path")
    raw = (cfg.get("memory_promotion") or {}).get(
        "proposal_dir",
        DEFAULT_PROMOTION_PROPOSAL_DIR,
    )
    proposal = safe_vault_path(vault, raw)
    assert_no_symlink_components(proposal, vault)
    relative = os.path.relpath(proposal, vault).replace(os.sep, "/")
    return tuple(dict.fromkeys((relative, DEFAULT_PROMOTION_PROPOSAL_DIR)))


def append_unique(items, value):
    if value not in items:
        items.append(value)


def list_items(fm, *keys):
    items = []
    for key in keys:
        value = fm.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    return items


def parse_personal_memory_entries(body):
    return parse_adaptive_entries(body, "personal")


def skill_preference_title(note):
    fm = note.get("frontmatter") or {}
    skill_name = str(fm.get("skill_name") or "").strip()
    task_intent = str(fm.get("task_intent") or note.get("title") or "").strip()
    return f"{skill_name}: {task_intent}".strip(": ")


def skill_preference_content(fm):
    parts = [
        "技能偏好候选",
        f"skill_name: {fm.get('skill_name', '')}",
        f"task_intent: {fm.get('task_intent', '')}",
        f"artifact_type: {fm.get('artifact_type', '')}",
        f"pain_point: {fm.get('pain_point', '')}",
        f"why_skill_fits: {fm.get('why_skill_fits', '')}",
        "positive_signals: " + ", ".join(str(item) for item in fm.get("positive_signals", []) or []),
        "negative_signals: " + ", ".join(str(item) for item in fm.get("negative_signals", []) or []),
        f"evidence_excerpt: {fm.get('evidence_excerpt', '')}",
    ]
    return "\n".join(part for part in parts if part.strip())


def workflow_memory_title(note):
    fm = note.get("frontmatter") or {}
    rule_name = str(fm.get("rule_name") or "").strip()
    title = str(fm.get("title") or note.get("title") or "").strip()
    return f"{rule_name}: {title}".strip(": ")


def workflow_memory_content(fm):
    parts = [
        "流程记忆候选",
        f"rule_name: {fm.get('rule_name', '')}",
        f"trigger_scene: {fm.get('trigger_scene', '')}",
        f"user_correction: {fm.get('user_correction', '')}",
        f"desired_behavior: {fm.get('desired_behavior', '')}",
        f"why_it_matters: {fm.get('why_it_matters', '')}",
        "positive_signals: " + ", ".join(str(item) for item in fm.get("positive_signals", []) or []),
        "negative_signals: " + ", ".join(str(item) for item in fm.get("negative_signals", []) or []),
        f"evidence_excerpt: {fm.get('evidence_excerpt', '')}",
    ]
    return "\n".join(part for part in parts if part.strip())


def parse_skill_routing_entries(body):
    return parse_adaptive_entries(body, "skill")


def parse_workflow_rule_entries(body):
    return parse_adaptive_entries(body, "workflow")


def parse_insight_entries(body):
    return parse_adaptive_entries(body, "insight")


def parse_adaptive_entries(body, kind, active_only=True):
    entries = []
    headings = list(re.finditer(r"^##\s+(.+?)\s*$", str(body or ""), re.MULTILINE))
    for index, match in enumerate(headings):
        title = match.group(1).strip()
        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        section = body[start:end].strip()
        parser = parse_active_formal_section if active_only else parse_formal_section
        record = parser(title, section, kind)
        if record is not None:
            entries.append(record)
    return entries


def aggregate_project_from_path(path):
    match = re.fullmatch(
        r"01-Projects/([^/]+)/Memory/(?:decisions|pitfalls)",
        str(path or "").replace("\\", "/"),
    )
    return canonical_project(match.group(1)) if match else ""


def looks_like_markdown_table_memory(content):
    return bool(re.match(r"^\|\s*\d{4}-\d{2}-\d{2}\s*\|", str(content or "").strip()))


def dedupe_units(units):
    seen = set()
    unique = []
    for unit in units:
        if unit["id"] in seen:
            continue
        seen.add(unit["id"])
        unique.append(unit)
    return unique


def recall_unit_id(unit_type, path, title, content):
    digest = hashlib.sha1(
        f"{unit_type}:{path}:{title}:{content}".encode("utf-8"),
        usedforsecurity=False,
    ).hexdigest()[:12]
    return f"{unit_type}:{digest}"


def compact_text(text, limit):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def add_node(
    nodes,
    node_id,
    node_type,
    kind,
    label,
    path,
    project,
    *,
    date="",
    revision="",
    source_refs=None,
    resolved=True,
):
    upsert_graph_node(
        nodes,
        graph_node(
            node_id,
            node_type,
            kind,
            label,
            path=path,
            project=project,
            date=date,
            revision=revision,
            source_refs=source_refs,
            resolved=resolved,
        ),
    )


def memory_graph_source_refs(unit):
    """Include the formal note locator in derived graph provenance."""
    refs = list(unit.get("source_refs") or [])
    source_note = str(unit.get("source_note") or "")
    if source_note and source_note not in refs:
        refs.append(source_note)
    return refs


def add_edge(
    edges,
    nodes,
    source,
    target,
    relation,
    *,
    source_ref,
    source_revision="",
    observed_at="",
    derivation,
    confidence=1.0,
):
    add_graph_edge(
        edges,
        nodes,
        source,
        target,
        relation,
        graph_evidence(
            source_ref,
            source_revision=source_revision,
            observed_at=observed_at,
            derivation=derivation,
        ),
        confidence=confidence,
    )


def add_insight_graph_relations(nodes, edges, unit, units_by_id):
    source_refs = [
        source_ref
        for source_ref in dict.fromkeys(unit.get("source_refs") or [])
        if source_ref != unit.get("source_note")
    ]
    for index, source_ref in enumerate(source_refs):
        add_node(
            nodes,
            source_ref,
            "source",
            source_ref.partition(":")[0] or "source",
            source_ref,
            "",
            "",
            source_refs=[source_ref],
        )
        relation = "derived_from" if index == 0 else "reinforced_by"
        add_edge(
            edges,
            nodes,
            unit["id"],
            source_ref,
            relation,
            source_ref=source_ref,
            source_revision=unit.get("revision", ""),
            observed_at=unit.get("date", ""),
            derivation="insight-metadata",
        )

    for transfer in unit.get("transfer") or []:
        target = concept_node_id(transfer)
        add_node(
            nodes,
            target,
            "concept",
            "transfer",
            transfer,
            "",
            unit.get("project", ""),
            source_refs=[unit.get("source_note") or unit["id"]],
        )
        add_edge(
            edges,
            nodes,
            unit["id"],
            target,
            "applies_to",
            source_ref=unit.get("source_note") or unit["id"],
            source_revision=unit.get("revision", ""),
            observed_at=unit.get("date", ""),
            derivation="insight-metadata",
        )

def add_declared_memory_relations(nodes, edges, unit, units_by_id):
    """Derive only explicitly declared, revision-bound memory relations."""
    for relation in MEMORY_RELATION_FIELDS:
        for target in unit.get(relation) or []:
            target_unit = units_by_id.get(target)
            add_node(
                nodes,
                target,
                "memory",
                target_unit.get("type", "reference") if target_unit else "reference",
                target_unit.get("title", target) if target_unit else target,
                target_unit.get("path", "") if target_unit else "",
                target_unit.get("project", "") if target_unit else "",
                date=target_unit.get("date", "") if target_unit else "",
                revision=target_unit.get("revision", "") if target_unit else "",
                source_refs=(
                    memory_graph_source_refs(target_unit)
                    if target_unit
                    else [unit.get("source_note") or unit["id"]]
                ),
                resolved=bool(target_unit),
            )
            add_edge(
                edges,
                nodes,
                unit["id"],
                target,
                relation,
                source_ref=unit.get("source_note") or unit["id"],
                source_revision=unit.get("revision", ""),
                observed_at=unit.get("date", ""),
                derivation="formal-record",
            )


def concept_node_id(value):
    normalized = re.sub(r"\s+", " ", str(value or "")).strip().casefold()
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"concept:{digest}"


def extract_wikilinks(body):
    links = []
    for match in re.findall(r"\[\[([^|\]\n]+)(?:\|[^\]\n]*)?\]\]", str(body or "")):
        target = match.strip()
        if target and target not in links:
            links.append(target)
    return links


def build_global_atoms(vault):
    grouped = defaultdict(list)
    projects_dir = os.path.join(vault, "01-Projects")
    if os.path.isdir(projects_dir):
        for project in sorted(os.listdir(projects_dir)):
            pitfalls_path = os.path.join(projects_dir, project, "Memory", "pitfalls.md")
            if not os.path.exists(pitfalls_path):
                continue
            for pitfall in read_pitfalls(pitfalls_path, project):
                err_type = str(pitfall.get("type", "")).strip()
                resolution = str(pitfall.get("resolution", "")).strip()
                if not err_type or not resolution:
                    continue
                key = normalized_atom_key(err_type, resolution)
                grouped[key].append(
                    {
                        "project": project,
                        "type": err_type,
                        "resolution": resolution,
                        "path": f"01-Projects/{project}/Memory/pitfalls",
                    }
                )

    atoms = []
    for key, items in grouped.items():
        projects = sorted({item["project"] for item in items})
        if len(projects) < 2:
            continue
        representative = items[0]
        atom_id = atom_id_for(representative["type"], key)
        atoms.append(
            {
                "id": atom_id,
                "type": representative["type"],
                "phase": "post",
                "projects": projects,
                "trigger": extract_terms(
                    representative["type"] + " " + representative["resolution"],
                    limit=6,
                ),
                "one_liner": representative["resolution"][:180],
                "pointers": sorted({item["path"] for item in items}),
                "occurrences": len(items),
            }
        )
    atoms.sort(key=lambda item: (-len(item["projects"]), item["type"], item["id"]))
    return {
        "schema_version": "1.0",
        "generated_by": "knowledge_index.py",
        "generated_at": datetime.now(CST).isoformat(),
        "promotion_rule": "same normalized pitfall appears in two or more projects",
        "atoms": atoms[:20],
    }


def read_pitfalls(path, expected_project):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return []
    fm, _ = split_frontmatter(content)
    if (
        fm.get("schema_version") != RUNTIME_SCHEMA_VERSION
        or str(fm.get("project") or "").strip() != expected_project
    ):
        return []
    pitfalls = fm.get("pitfalls", [])
    if not isinstance(pitfalls, list):
        return []
    return [
        item
        for item in pitfalls
        if is_valid_active_project_record(item, "error", expected_project)
    ]


def render_global_atoms_markdown(data):
    lines = [
        "---",
        "title: Global Atoms",
        "generated_by: knowledge_index.py",
        f"updated_at: {data.get('generated_at', '')}",
        f"atom_count: {len(data.get('atoms', []))}",
        "---",
        "",
        "# Global Atoms",
        "",
        "Cross-project pitfalls promoted from repeated resolved errors.",
        "",
        "## Related",
        "",
        "- [[00-Inbox/Agent Memory Index|Agent Memory Index]]",
        "- [[05-Agent-Memory/personal-memory|Personal Memory]]",
        "",
    ]
    atoms = data.get("atoms", [])
    if not atoms:
        lines.append("_No cross-project atoms detected yet._")
        return "\n".join(lines).rstrip() + "\n"

    for atom in atoms:
        lines.extend(
            [
                f"## {atom['id']}",
                "",
                f"- type: `{atom.get('type', '')}`",
                f"- projects: {', '.join(project_link(p) for p in atom.get('projects', []))}",
                f"- triggers: {', '.join(f'`{t}`' for t in atom.get('trigger', []))}",
                f"- occurrences: `{atom.get('occurrences', 0)}`",
                f"- lesson: {atom.get('one_liner', '')}",
                "- pointers:",
            ]
        )
        for pointer in atom.get("pointers", []):
            lines.append(f"  - [[{pointer}|{os.path.basename(pointer)}]]")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_keyword_index_markdown(data):
    keywords = data.get("keywords", {})
    top_terms = sorted(
        keywords.items(),
        key=lambda item: (-len(item[1]), item[0]),
    )[:80]
    lines = [
        "---",
        "title: Keyword Index",
        "generated_by: knowledge_index.py",
        f"updated_at: {data.get('generated_at', '')}",
        f"term_count: {data.get('term_count', 0)}",
        "---",
        "",
        "# Keyword Index",
        "",
        "Machine-readable retrieval index generated from sessions, decisions, pitfalls, and personal memory.",
        "",
        "## Related",
        "",
        "- [[00-Inbox/Agent Memory Index|Agent Memory Index]]",
        "- [[05-Agent-Memory/personal-memory|Personal Memory]]",
        "- [[05-Agent-Memory/global-atoms|Global Atoms]]",
        "",
        "## Top Terms",
        "",
        "| Term | Matches | Example Links |",
        "|---|---:|---|",
    ]
    for term, entries in top_terms:
        examples = []
        for entry in entries[:3]:
            examples.append(f"[[{entry['path']}|{entry['title']}]]")
        lines.append(f"| `{escape_table(term)}` | {len(entries)} | {'; '.join(examples)} |")
    return "\n".join(lines).rstrip() + "\n"


def render_recall_context_markdown(recall_index, memory_graph):
    units = recall_index.get("units", [])
    top_units = sorted(
        units,
        key=lambda item: (
            type_rank(item.get("type", "")),
            str(item.get("date") or ""),
            str(item.get("title") or ""),
        ),
        reverse=True,
    )[:40]
    lines = [
        "---",
        "title: Recall Context",
        "generated_by: knowledge_index.py",
        f"updated_at: {recall_index.get('generated_at', '')}",
        f"recall_units: {len(units)}",
        f"graph_nodes: {len(memory_graph.get('nodes', []))}",
        f"graph_edges: {len(memory_graph.get('edges', []))}",
        "---",
        "",
        "# Recall Context",
        "",
        "Cognee-style lightweight recall layer generated from Obsidian Markdown.",
        "",
        "## Related",
        "",
        "- [[00-Inbox/Agent Memory Index|Agent Memory Index]]",
        "- [[05-Agent-Memory/keyword-index|Keyword Index]]",
        "- [[05-Agent-Memory/global-atoms|Global Atoms]]",
        "",
        "## Usage",
        "",
        "```bash",
        'python scripts/memory_recall.py "query" --project project-name',
        "```",
        "",
        "## Recall Units",
        "",
        "| Type | Project | Memory | Source |",
        "|---|---|---|---|",
    ]
    for unit in top_units:
        lines.append(
            "| `{type}` | {project} | {title} | {source} |".format(
                type=escape_table(unit.get("type", "")),
                project=escape_table(unit.get("project", "")),
                title=escape_table(compact_text(unit.get("title", ""), 100)),
                source=obsidian_link(unit.get("path", ""), os.path.basename(unit.get("path", ""))),
            )
        )
    return "\n".join(lines).rstrip() + "\n"


def split_frontmatter(content):
    frontmatter_text, body = split_frontmatter_text(content)
    if frontmatter_text is None:
        return {}, content
    try:
        fm = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError:
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    return fm, body


def searchable_text(fm, body):
    values = []
    for key in (
        "content",
        "evidence",
        "summary",
        "project",
        "skill_name",
        "task_intent",
        "artifact_type",
        "pain_point",
        "why_skill_fits",
        "rule_name",
        "trigger_scene",
        "user_correction",
        "desired_behavior",
        "why_it_matters",
        "evidence_excerpt",
    ):
        if fm.get(key):
            values.append(str(fm[key]))
    for key in ("positive_signals", "negative_signals"):
        if isinstance(fm.get(key), list):
            values.extend(str(item) for item in fm[key])
    for key in ("decisions_made", "errors_encountered", "decisions", "pitfalls"):
        if isinstance(fm.get(key), list):
            values.extend(flatten_item(item) for item in fm[key])
    values.append(strip_markdown(body)[:2000])
    return " ".join(v for v in values if v)


def flatten_item(item):
    if isinstance(item, dict):
        return " ".join(str(v) for v in item.values() if isinstance(v, (str, int, float)))
    return str(item)


def strip_markdown(text):
    text = re.sub(r"```.*?```", " ", text or "", flags=re.DOTALL)
    text = re.sub(r"\[\[([^|\]]+)\|?([^\]]*)\]\]", r" \1 \2 ", text)
    text = re.sub(r"[#>*_`|]", " ", text)
    return text


def extract_terms(text, limit=None):
    text = str(text or "").lower()
    terms = set()
    for term in re.findall(r"[a-z][a-z0-9_-]{2,}", text):
        if term not in STOPWORDS:
            terms.add(term)
    for term in CHINESE_KEY_TERMS:
        if term in text:
            terms.add(term)
    for term in re.findall(r"[\u4e00-\u9fff]{2,8}", text):
        if len(term) <= 8:
            terms.add(term)
    result = sorted(terms)
    return result[:limit] if limit else result


def normalized_atom_key(err_type, resolution):
    text = f"{err_type} {resolution}".lower()
    text = re.sub(r"`[^`]+`", " ", text)
    text = re.sub(r"[/\\][^\s]+", " ", text)
    text = re.sub(r"\b[0-9a-f]{8,}\b", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def atom_id_for(err_type, key):
    digest = hashlib.sha256(f"{err_type}:{key}".encode("utf-8")).hexdigest()[:8]
    prefix = re.sub(r"[^A-Z0-9]", "", err_type.upper())[:6] or "GEN"
    return f"{prefix}-{digest}"


def note_type_from_path(rel_path):
    if "/sessions/" in rel_path:
        return "session"
    if rel_path.endswith("/decisions") or rel_path.endswith("decisions"):
        return "decisions"
    if rel_path.endswith("/pitfalls") or rel_path.endswith("pitfalls"):
        return "pitfalls"
    if "_memory-candidates" in rel_path:
        return "memory-candidate"
    if "_skill-preferences" in rel_path:
        return "skill-preference"
    if "_workflow-candidates" in rel_path:
        return "workflow-candidate"
    if "_insight-candidates" in rel_path:
        return "insight-candidate"
    if "_annotation-candidates" in rel_path:
        return "annotation-candidate"
    if rel_path.endswith("personal-memory"):
        return "personal-memory"
    if rel_path.endswith("skill-routing-rules"):
        return "skill-routing-rules"
    if rel_path.endswith("workflow-rules"):
        return "workflow-rules"
    if rel_path.endswith("insights"):
        return "insights"
    return "note"


def type_rank(unit_type):
    return {
        "skill": 8,
        "workflow": 8,
        "insight": 7,
        "decision": 6,
        "error": 5,
        "preference": 4,
        "project_rule": 4,
        "environment": 4,
    }.get(unit_type, 1)


def first_heading(body):
    for line in str(body or "").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def project_link(project):
    return f"[[01-Projects/{project}/Memory/decisions|{project}]]"


def obsidian_link(path_without_ext, label):
    if not path_without_ext:
        return ""
    return f"[[{path_without_ext}|{escape_table(label or path_without_ext)}]]"


def escape_table(value):
    return str(value).replace("|", "\\|")


def atomic_write_json(path, data, root=None):
    durable_atomic_write(
        path,
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        root=root,
    )


def atomic_write_text(path, content, root=None):
    durable_atomic_write(path, content, root=root)
