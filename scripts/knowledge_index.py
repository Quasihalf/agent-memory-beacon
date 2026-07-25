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
from experience_memory import build_experience_bundles
from memory_schema import (
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
GENERATED_INDEX_FILES = {
    "keyword-index.md",
    "global-atoms.md",
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
        *error_evidence_candidate_roots(cfg),
        *annotation_candidate_roots(cfg),
        *insight_candidate_roots(cfg),
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

    keyword_path = os.path.join(output_dir, "keyword-index.json")
    keyword_md_path = os.path.join(output_dir, "keyword-index.md")
    atoms_path = os.path.join(output_dir, "global-atoms.json")
    atoms_md_path = os.path.join(output_dir, "global-atoms.md")
    recall_path = configured_recall_index_path(cfg)
    ensure_directory_tree(os.path.dirname(recall_path), vault)
    graph_path = os.path.join(output_dir, "memory-graph.json")
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
        recall_md_path,
        render_recall_context_markdown(recall_index, memory_graph),
        root=vault,
    )

    return {
        "keyword_terms": len(keyword_index.get("keywords", {})),
        "global_atoms": len(atoms.get("atoms", [])),
        "recall_units": len(recall_index.get("units", [])),
        "graph_nodes": len(memory_graph.get("nodes", [])),
        "graph_edges": len(memory_graph.get("edges", [])),
        "written": [
            keyword_path,
            keyword_md_path,
            atoms_path,
            atoms_md_path,
            recall_path,
            graph_path,
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

    return {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "generated_by": "knowledge_index.py",
        "generated_at": datetime.now(CST).isoformat(),
        "unit_count": len(units),
        "units": units,
        "experience_bundles": experience_bundles,
        "suppressed_dependencies": suppressed,
        "suppressed_quality": suppressed_quality,
        "duplicate_groups": duplicate_groups,
        "terms": {k: v[:50] for k, v in sorted(terms.items())},
        "projects": {k: v[:100] for k, v in sorted(projects.items())},
        "types": {k: v[:100] for k, v in sorted(types.items())},
    }


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
    unit["terms"] = extract_terms(
        " ".join(
            [
                unit["title"],
                unit.get("project", ""),
                unit["summary"],
                operational_text,
                insight_text,
            ]
        ),
        limit=60,
    )
    return unit


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
    edges = []

    for note in notes:
        if not is_indexable_note(note):
            continue
        note_id = f"note:{note['path']}"
        add_node(nodes, note_id, note["type"], note["title"], note["path"], note.get("project", ""))
        if note.get("project"):
            project_id = f"project:{note['project']}"
            add_node(nodes, project_id, "project", note["project"], "", note["project"])
            add_edge(edges, note_id, project_id, "belongs_to")
        for link in note.get("links", []):
            target_id = f"note:{link}"
            add_node(nodes, target_id, "note-ref", os.path.basename(link), link, "")
            add_edge(edges, note_id, target_id, "links_to")

    for unit in recall_index.get("units", []):
        if is_error_evidence_candidate_path(unit.get("path")):
            continue
        add_node(
            nodes,
            unit["id"],
            unit["type"],
            unit["title"],
            unit.get("path", ""),
            unit.get("project", ""),
        )
        source_note = unit.get("source_note")
        if source_note and source_note != unit["id"]:
            add_edge(edges, unit["id"], source_note, "recorded_in")
        if unit.get("project"):
            project_id = f"project:{unit['project']}"
            add_node(nodes, project_id, "project", unit["project"], "", unit["project"])
            add_edge(edges, unit["id"], project_id, "belongs_to")
        for dependency in unit.get("requires") or []:
            add_edge(edges, unit["id"], dependency, "depends_on")
        if unit.get("type") == "insight":
            add_insight_graph_relations(nodes, edges, unit)

    units_by_id = {
        unit.get("id"): unit
        for unit in recall_index.get("units", [])
        if isinstance(unit, dict) and unit.get("id")
    }
    for bundle in recall_index.get("experience_bundles") or []:
        if not isinstance(bundle, dict) or not bundle.get("id"):
            continue
        project = str(bundle.get("project") or "")
        session_ref = str(bundle.get("session_ref") or "")
        add_node(
            nodes,
            bundle["id"],
            "experience",
            f"{project}: {session_ref.removeprefix('session:')}",
            "",
            project,
        )
        if project:
            project_id = f"project:{project}"
            add_node(nodes, project_id, "project", project, "", project)
            add_edge(edges, bundle["id"], project_id, "belongs_to")
        for member in bundle.get("members") or []:
            if not isinstance(member, dict):
                continue
            unit = units_by_id.get(member.get("id"))
            if (
                unit
                and unit.get("revision") == member.get("revision")
                and unit.get("project") == project
            ):
                add_edge(edges, unit["id"], bundle["id"], "part_of_experience")

    return {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "generated_by": "knowledge_index.py",
        "generated_at": datetime.now(CST).isoformat(),
        "nodes": list(nodes.values()),
        "edges": sorted(edges, key=lambda item: (item["source"], item["relation"], item["target"])),
    }


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


def insight_candidate_roots(cfg):
    vault = cfg.get("vault_path")
    raw = (cfg.get("insight_memory") or {}).get(
        "candidate_dir",
        DEFAULT_INSIGHT_CANDIDATE_DIR,
    )
    candidate = safe_vault_path(vault, raw)
    assert_no_symlink_components(candidate, vault)
    relative = os.path.relpath(candidate, vault).replace(os.sep, "/")
    return tuple(dict.fromkeys((relative, DEFAULT_INSIGHT_CANDIDATE_DIR)))


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


def add_node(nodes, node_id, node_type, label, path, project):
    existing = nodes.get(node_id)
    if existing and not (
        existing.get("type") == "note-ref" and node_type != "note-ref"
    ):
        return
    nodes[node_id] = {
        "id": node_id,
        "type": node_type,
        "label": str(label or node_id)[:160],
        "path": path or "",
        "project": project or "",
    }


def add_edge(edges, source, target, relation):
    edge = {"source": source, "target": target, "relation": relation}
    if edge not in edges:
        edges.append(edge)


def add_insight_graph_relations(nodes, edges, unit):
    source_refs = [
        source_ref
        for source_ref in dict.fromkeys(unit.get("source_refs") or [])
        if source_ref != unit.get("source_note")
    ]
    for index, source_ref in enumerate(source_refs):
        add_node(nodes, source_ref, "source-ref", source_ref, "", "")
        relation = "derived_from" if index == 0 else "reinforced_by"
        add_edge(edges, unit["id"], source_ref, relation)

    for transfer in unit.get("transfer") or []:
        target = concept_node_id(transfer)
        add_node(nodes, target, "concept", transfer, "", unit.get("project", ""))
        add_edge(edges, unit["id"], target, "applies_to")

    relation_types = {
        "supports": "memory-ref",
        "operationalized_as": "memory-ref",
        "related_to": "insight-ref",
    }
    for relation, node_type in relation_types.items():
        for target in unit.get(relation) or []:
            add_node(nodes, target, node_type, target, "", "")
            add_edge(edges, unit["id"], target, relation)


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
