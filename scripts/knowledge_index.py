"""Build vault-level keyword and cross-project atom indexes.

This is a conservative v4-inspired layer for the Obsidian-vault workflow:
- keyword-index.json gives agents a small machine-readable retrieval map.
- global-atoms.json/md promotes only pitfalls repeated across projects.
"""
import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import yaml


CST = timezone(timedelta(hours=8))
DEFAULT_OUTPUT_DIR = "05-Agent-Memory"
MAX_TERMS_PER_NOTE = 100
MAX_ENTRIES_PER_TERM = 20
GENERATED_INDEX_FILES = {
    "keyword-index.md",
    "global-atoms.md",
    "recall-context.md",
}
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
    os.makedirs(output_dir, exist_ok=True)

    notes = collect_indexable_notes(vault)
    keyword_index = build_keyword_index(notes)
    atoms = build_global_atoms(vault)
    recall_index = build_recall_index(notes)
    memory_graph = build_memory_graph(notes, recall_index)

    keyword_path = os.path.join(output_dir, "keyword-index.json")
    keyword_md_path = os.path.join(output_dir, "keyword-index.md")
    atoms_path = os.path.join(output_dir, "global-atoms.json")
    atoms_md_path = os.path.join(output_dir, "global-atoms.md")
    recall_path = os.path.join(output_dir, "recall-index.json")
    graph_path = os.path.join(output_dir, "memory-graph.json")
    recall_md_path = os.path.join(output_dir, "recall-context.md")

    atomic_write_json(keyword_path, keyword_index)
    atomic_write_text(keyword_md_path, render_keyword_index_markdown(keyword_index))
    atomic_write_json(atoms_path, atoms)
    atomic_write_text(atoms_md_path, render_global_atoms_markdown(atoms))
    atomic_write_json(recall_path, recall_index)
    atomic_write_json(graph_path, memory_graph)
    atomic_write_text(
        recall_md_path,
        render_recall_context_markdown(recall_index, memory_graph),
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


def collect_indexable_notes(vault):
    notes = []
    roots = [
        os.path.join(vault, "01-Projects"),
        os.path.join(vault, "04-Feedback", "_memory-candidates"),
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
                dirs[:] = [d for d in dirs if d not in {"_raw-sessions", "_rollback", ".git"}]
                for filename in files:
                    if filename.endswith(".md"):
                        paths.append(os.path.join(current, filename))
        for path in sorted(paths):
            if os.path.basename(path).startswith("_"):
                continue
            if os.path.basename(path) in GENERATED_INDEX_FILES:
                continue
            note = read_note(path, vault)
            if note:
                notes.append(note)
    return notes


def read_note(path, vault):
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
        "type": note_type_from_path(rel_path),
        "text": searchable_text(fm, body),
        "frontmatter": fm,
        "body": body,
        "links": extract_wikilinks(body),
    }


def build_keyword_index(notes):
    keywords = defaultdict(list)
    now = datetime.now(CST).isoformat()
    for note in notes:
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
    units = []
    for note in notes:
        units.extend(recall_units_from_note(note))

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

    return {
        "schema_version": "1.0",
        "generated_by": "knowledge_index.py",
        "generated_at": datetime.now(CST).isoformat(),
        "unit_count": len(units),
        "units": units,
        "terms": {k: v[:50] for k, v in sorted(terms.items())},
        "projects": {k: v[:100] for k, v in sorted(projects.items())},
        "types": {k: v[:100] for k, v in sorted(types.items())},
    }


def recall_units_from_note(note):
    fm = note.get("frontmatter") or {}
    body = note.get("body") or ""
    units = [
        make_recall_unit(
            unit_type=note["type"],
            title=note["title"],
            content=note["text"],
            path=note["path"],
            project=note.get("project", ""),
            date=fm.get("date") or fm.get("last_seen") or "",
            source_note=f"note:{note['path']}",
        )
    ]

    for decision in list_items(fm, "decisions_made", "decisions"):
        text = str(decision.get("text", "")).strip()
        context = str(decision.get("context", "")).strip()
        if not text and not context:
            continue
        units.append(
            make_recall_unit(
                unit_type="decision",
                title=text or "Decision",
                content=f"{text}\ncontext: {context}",
                path=note["path"],
                project=note.get("project", ""),
                date=fm.get("date") or "",
                source_note=f"note:{note['path']}",
            )
        )

    for error in list_items(fm, "errors_encountered", "pitfalls"):
        err_type = str(error.get("type", "")).strip()
        resolution = str(error.get("resolution", "")).strip()
        if not err_type and not resolution:
            continue
        units.append(
            make_recall_unit(
                unit_type="error",
                title=err_type or "Error",
                content=f"{err_type}\nresolution: {resolution}",
                path=note["path"],
                project=note.get("project", ""),
                date=fm.get("date") or "",
                source_note=f"note:{note['path']}",
            )
        )

    if note["type"] == "memory-candidate" and fm.get("content"):
        units.append(
            make_recall_unit(
                unit_type="memory-candidate",
                title=str(fm.get("title") or note["title"]),
                content=str(fm.get("content", "")),
                path=note["path"],
                project=str(fm.get("project", "")),
                date=fm.get("last_seen") or fm.get("date") or "",
                source_note=f"note:{note['path']}",
            )
        )

    if note["type"] == "personal-memory":
        for entry in parse_personal_memory_entries(body):
            units.append(
                make_recall_unit(
                    unit_type="personal-memory",
                    title=entry["title"],
                    content=entry["content"],
                    path=note["path"],
                    project=entry.get("project", ""),
                    date="",
                    source_note=f"note:{note['path']}",
                )
            )

    return dedupe_units(units)


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

    return {
        "schema_version": "1.0",
        "generated_by": "knowledge_index.py",
        "generated_at": datetime.now(CST).isoformat(),
        "nodes": list(nodes.values()),
        "edges": sorted(edges, key=lambda item: (item["source"], item["relation"], item["target"])),
    }


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
    entries = []
    current_title = ""
    current_lines = []
    for line in str(body or "").splitlines():
        if line.startswith("## "):
            if current_title and current_lines:
                entries.append(memory_entry_from_lines(current_title, current_lines))
            current_title = line[3:].strip()
            current_lines = []
        elif current_title:
            current_lines.append(line)
    if current_title and current_lines:
        entries.append(memory_entry_from_lines(current_title, current_lines))
    return [entry for entry in entries if entry.get("content")]


def memory_entry_from_lines(title, lines):
    content = ""
    project = ""
    for line in lines:
        if line.strip().startswith("- memory:"):
            content = line.split(":", 1)[1].strip()
        elif line.strip().startswith("- project:"):
            raw = line.split(":", 1)[1].strip()
            match = re.search(r"\[\[01-Projects/([^/\]]+)/", raw)
            project = match.group(1) if match else raw.strip("` ")
    if looks_like_markdown_table_memory(content):
        content = ""
    return {"title": title, "content": content, "project": project}


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
        f"{unit_type}:{path}:{title}:{content}".encode("utf-8")
    ).hexdigest()[:12]
    return f"{unit_type}:{digest}"


def compact_text(text, limit):
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def add_node(nodes, node_id, node_type, label, path, project):
    nodes.setdefault(
        node_id,
        {
            "id": node_id,
            "type": node_type,
            "label": str(label or node_id)[:160],
            "path": path or "",
            "project": project or "",
        },
    )


def add_edge(edges, source, target, relation):
    edge = {"source": source, "target": target, "relation": relation}
    if edge not in edges:
        edges.append(edge)


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
            for pitfall in read_pitfalls(pitfalls_path):
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


def read_pitfalls(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return []
    fm, _ = split_frontmatter(content)
    pitfalls = fm.get("pitfalls", [])
    return pitfalls if isinstance(pitfalls, list) else []


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
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    try:
        fm = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        fm = {}
    if not isinstance(fm, dict):
        fm = {}
    return fm, parts[2]


def searchable_text(fm, body):
    values = []
    for key in ("content", "evidence", "summary", "project"):
        if fm.get(key):
            values.append(str(fm[key]))
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
    if rel_path.endswith("personal-memory"):
        return "personal-memory"
    return "note"


def type_rank(unit_type):
    return {
        "decision": 6,
        "error": 5,
        "personal-memory": 4,
        "memory-candidate": 3,
        "session": 2,
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


def atomic_write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def atomic_write_text(path, content):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(tmp, path)
