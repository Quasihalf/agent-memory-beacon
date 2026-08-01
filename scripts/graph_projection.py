"""Render the canonical memory graph as Obsidian-visible projection notes."""
from __future__ import annotations

import hashlib
import json
import os

import yaml

from safety import (
    durable_atomic_write,
    durable_unlink,
    ensure_directory_tree,
    safe_filename,
    safe_vault_path,
    secure_read_bytes,
)


PROJECTION_SCHEMA_VERSION = "1.0"
DEFAULT_OUTPUT_DIR = "03-Maps/_memory-nodes"
DEFAULT_MAX_NODES = 5000
MANIFEST_FILENAME = "_projection-manifest.json"
PROJECTABLE_NODE_TYPES = frozenset(
    {
        "concept",
        "experience",
        "memory",
        "source",
    }
)
SEMANTIC_RELATIONS = frozenset(
    {
        "applies_to",
        "contradicts",
        "depends_on",
        "derived_from",
        "operationalized_as",
        "part_of_experience",
        "reinforced_by",
        "related_to",
        "superseded_by",
        "supports",
    }
)
KIND_DIRECTORIES = {
    "decision": "Decisions",
    "environment": "Preferences",
    "error": "Errors",
    "insight": "Insights",
    "personal-memory": "Preferences",
    "preference": "Preferences",
    "project_rule": "Preferences",
    "session-bundle": "Experiences",
    "skill": "Skills",
    "skill-routing-rules": "Skills",
    "workflow": "Workflows",
    "workflow-rules": "Workflows",
}
EXPERIENCE_LABEL_PRIORITY = {
    "error": 0,
    "decision": 1,
    "insight": 2,
    "workflow": 3,
    "preference": 4,
}


def build_graph_projection_plan(vault, graph, settings=None):
    """Return deterministic projection paths and Markdown without writing."""
    settings = _projection_settings(settings)
    _validate_graph_shape(graph)
    vault = os.path.abspath(os.path.expanduser(os.fspath(vault)))
    output_dir = _relative_output_dir(settings["output_dir"])
    nodes = {
        str(node.get("id")): dict(node)
        for node in graph["nodes"]
        if isinstance(node, dict) and node.get("id")
    }
    projectable = {
        node_id: node
        for node_id, node in nodes.items()
        if node.get("type") in PROJECTABLE_NODE_TYPES
        and bool(node.get("resolved", True))
    }
    if len(projectable) > settings["max_nodes"]:
        raise ValueError(
            "graph projection exceeds node limit: "
            f"{len(projectable)} > {settings['max_nodes']}"
        )

    edges = [
        dict(edge)
        for edge in graph["edges"]
        if isinstance(edge, dict)
        and edge.get("source") in nodes
        and edge.get("target") in nodes
    ]
    experience_members = _experience_members(nodes, edges)
    display_labels = {
        node_id: _display_label(
            node,
            experience_members.get(node_id, ()),
        )
        for node_id, node in projectable.items()
    }
    node_targets = {
        node_id: _projection_target(
            output_dir,
            node,
            display_labels[node_id],
        )
        for node_id, node in projectable.items()
    }
    target_lookup = {
        node_id: _existing_or_projected_target(vault, node, node_targets)
        for node_id, node in nodes.items()
    }
    experience_sources = {
        str(edge.get("source"))
        for edge in edges
        if edge.get("relation") == "part_of_experience"
        and nodes.get(str(edge.get("source")), {}).get("type") == "memory"
    }
    visual_edges = [
        edge
        for edge in edges
        if _include_visual_edge(edge, nodes, experience_sources)
        and target_lookup.get(str(edge.get("source")))
        and target_lookup.get(str(edge.get("target")))
    ]
    outbound = {}
    for edge in visual_edges:
        outbound.setdefault(str(edge["source"]), []).append(edge)

    notes = {}
    for node_id, node in sorted(projectable.items()):
        target = node_targets[node_id]
        notes[target] = _render_projection_note(
            node,
            display_labels[node_id],
            outbound.get(node_id, ()),
            target_lookup,
            graph["generation_id"],
        )
    return {
        "generation_id": str(graph["generation_id"]),
        "node_targets": node_targets,
        "notes": notes,
        "edge_count": len(visual_edges),
        "output_dir": output_dir,
        "vault": vault,
    }


def sync_graph_projection(
    vault,
    graph,
    settings=None,
    *,
    ensure_directory=None,
    read_text=None,
    write_text=None,
    remove_file=None,
):
    """Synchronize generated notes while preserving every unmanaged file."""
    plan = build_graph_projection_plan(vault, graph, settings)
    vault = plan["vault"]
    root = safe_vault_path(vault, plan["output_dir"])
    ensure_directory = ensure_directory or (
        lambda path: ensure_directory_tree(path, vault)
    )
    read_text = read_text or (lambda path: _read_text(path, vault))
    write_text = write_text or (
        lambda path, content: durable_atomic_write(path, content, root=vault)
    )
    remove_file = remove_file or (
        lambda path: durable_unlink(path, root=vault)
    )

    ensure_directory(root)
    expected_files = []
    written = 0
    for target, content in sorted(plan["notes"].items()):
        path = safe_vault_path(vault, target + ".md")
        ensure_directory(os.path.dirname(path))
        expected_files.append(
            os.path.relpath(path, vault).replace(os.sep, "/")
        )
        if _read_optional(read_text, path) == content:
            continue
        write_text(path, content)
        written += 1

    manifest_path = safe_vault_path(root, MANIFEST_FILENAME)
    previous = _load_manifest(_read_optional(read_text, manifest_path))
    expected_set = set(expected_files)
    removed = 0
    for relative_path in sorted(set(previous.get("files", ())) - expected_set):
        if not _manifest_path_is_managed(relative_path, plan["output_dir"]):
            continue
        path = safe_vault_path(vault, relative_path)
        existing = _read_optional(read_text, path)
        if existing is None or not _is_projection_note(existing):
            continue
        remove_file(path)
        removed += 1

    manifest = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "generation_id": plan["generation_id"],
        "node_count": len(plan["notes"]),
        "edge_count": plan["edge_count"],
        "files": sorted(expected_files),
    }
    manifest_content = json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if _read_optional(read_text, manifest_path) != manifest_content:
        write_text(manifest_path, manifest_content)

    return {
        "generation_id": plan["generation_id"],
        "nodes": len(plan["notes"]),
        "edges": plan["edge_count"],
        "written": written,
        "removed": removed,
        "root": root,
        "manifest_path": manifest_path,
        "node_targets": plan["node_targets"],
    }


def _projection_settings(settings):
    settings = dict(settings or {})
    output_dir = str(settings.get("output_dir") or DEFAULT_OUTPUT_DIR).strip()
    max_nodes = settings.get("max_nodes", DEFAULT_MAX_NODES)
    if isinstance(max_nodes, bool) or not isinstance(max_nodes, int):
        raise TypeError("graph_projection.max_nodes must be an integer")
    if max_nodes <= 0:
        raise ValueError("graph_projection.max_nodes must be positive")
    return {
        "output_dir": output_dir,
        "max_nodes": max_nodes,
    }


def _validate_graph_shape(graph):
    if not isinstance(graph, dict):
        raise TypeError("memory graph must be a mapping")
    if str(graph.get("schema_version") or "") != "3.0":
        raise ValueError("graph projection requires memory graph schema 3.0")
    if not str(graph.get("generation_id") or ""):
        raise ValueError("graph projection requires a generation id")
    if not isinstance(graph.get("nodes"), list):
        raise TypeError("memory graph nodes must be a list")
    if not isinstance(graph.get("edges"), list):
        raise TypeError("memory graph edges must be a list")


def _relative_output_dir(value):
    value = str(value or "").replace("\\", "/").strip().strip("/")
    if not value or value.startswith(".") or "/../" in f"/{value}/":
        raise ValueError("graph_projection.output_dir is invalid")
    return value


def _experience_members(nodes, edges):
    members = {}
    for edge in edges:
        if edge.get("relation") != "part_of_experience":
            continue
        source = nodes.get(str(edge.get("source")))
        target = nodes.get(str(edge.get("target")))
        if (
            not source
            or not target
            or source.get("type") != "memory"
            or target.get("type") != "experience"
        ):
            continue
        members.setdefault(str(edge["target"]), []).append(source)
    return {
        node_id: tuple(
            sorted(
                items,
                key=lambda item: (
                    EXPERIENCE_LABEL_PRIORITY.get(str(item.get("kind")), 9),
                    str(item.get("label") or ""),
                    str(item.get("id") or ""),
                ),
            )
        )
        for node_id, items in members.items()
    }


def _display_label(node, experience_members):
    label = str(node.get("label") or node.get("id") or "Memory").strip()
    if node.get("type") != "experience" or not experience_members:
        return label
    lead = str(
        experience_members[0].get("label")
        or experience_members[0].get("id")
        or "会话记忆"
    ).strip()
    date = str(node.get("date") or "").strip()
    suffix = (
        f"等 {len(experience_members)} 条记忆"
        if len(experience_members) > 1
        else "经验束"
    )
    return " ".join(part for part in (date, lead, suffix) if part)


def _projection_target(output_dir, node, display_label):
    directory = KIND_DIRECTORIES.get(
        str(node.get("kind") or ""),
        {
            "concept": "Concepts",
            "experience": "Experiences",
            "source": "Sources",
        }.get(str(node.get("type") or ""), "Other"),
    )
    path_label = display_label
    if node.get("type") == "experience":
        path_label = " ".join(
            part
            for part in (
                str(node.get("date") or "").strip(),
                str(node.get("project") or "").strip(),
                "经验束",
            )
            if part
        )
    label = safe_filename(path_label, default="Memory", max_length=64)
    digest = hashlib.sha256(
        str(node.get("id") or "").encode("utf-8")
    ).hexdigest()[:10]
    filename = safe_filename(
        f"{label}--{digest}",
        default=f"Memory--{digest}",
        max_length=80,
    )
    return f"{output_dir}/{directory}/{filename}"


def _existing_or_projected_target(vault, node, node_targets):
    node_id = str(node.get("id") or "")
    if node_id in node_targets:
        return node_targets[node_id]
    if node.get("type") == "note" and node.get("path"):
        return str(node["path"])
    if node.get("type") == "project":
        project = str(node.get("project") or node.get("label") or "").strip()
        if project:
            target = f"03-Maps/Projects/{project}"
            if os.path.isfile(safe_vault_path(vault, target + ".md")):
                return target
        return ""
    return str(node.get("path") or "").strip()


def _include_visual_edge(edge, nodes, experience_sources):
    source_id = str(edge.get("source") or "")
    target_id = str(edge.get("target") or "")
    relation = str(edge.get("relation") or "")
    source = nodes.get(source_id) or {}
    target = nodes.get(target_id) or {}
    if source.get("type") == "memory":
        if relation == "recorded_in":
            return False
        if relation == "belongs_to":
            return source_id not in experience_sources
        if relation in SEMANTIC_RELATIONS:
            return target.get("type") in PROJECTABLE_NODE_TYPES
        return False
    if source.get("type") == "experience":
        return relation == "belongs_to" and target.get("type") == "project"
    return relation in SEMANTIC_RELATIONS


def _render_projection_note(
    node,
    display_label,
    edges,
    target_lookup,
    generation_id,
):
    frontmatter = {
        "schema_version": PROJECTION_SCHEMA_VERSION,
        "type": "memory-graph-projection",
        "graph_node_id": str(node.get("id") or ""),
        "graph_node_type": str(node.get("type") or ""),
        "memory_kind": str(node.get("kind") or ""),
        "project": str(node.get("project") or ""),
        "date": str(node.get("date") or ""),
        "source_revision": str(node.get("revision") or ""),
        "generation_id": str(generation_id),
        "status": "generated",
    }
    lines = [
        "---",
        yaml.safe_dump(
            frontmatter,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        ).strip(),
        "---",
        "",
        f"# {display_label}",
        "",
        "> 这是正式记忆图谱的只读投影；正式来源和生命周期状态不在此文件修改。",
        "",
        f"- 记忆 ID: `{node.get('id', '')}`",
    ]
    if node.get("path"):
        lines.append(f"- 正式来源: `{node['path']}`")
    if node.get("project"):
        lines.append(f"- 项目: `{node['project']}`")
    if node.get("date"):
        lines.append(f"- 日期: `{node['date']}`")

    rendered_edges = []
    seen = set()
    for edge in sorted(
        edges,
        key=lambda item: (
            str(item.get("relation") or ""),
            str(item.get("target") or ""),
        ),
    ):
        target = target_lookup.get(str(edge.get("target") or ""))
        if not target:
            continue
        key = (str(edge.get("relation") or ""), target)
        if key in seen:
            continue
        seen.add(key)
        rendered_edges.append(f"- `{key[0]}` [[{target}]]")
    if rendered_edges:
        lines.extend(["", "## 关系", "", *rendered_edges])
    return "\n".join(lines).rstrip() + "\n"


def _read_text(path, vault):
    try:
        data = secure_read_bytes(path, 1024 * 1024, root=vault)
    except FileNotFoundError:
        return None
    return data.decode("utf-8")


def _read_optional(reader, path):
    try:
        return reader(path)
    except FileNotFoundError:
        return None


def _load_manifest(content):
    if not content:
        return {}
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return {}
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != PROJECTION_SCHEMA_VERSION
        or not isinstance(payload.get("files"), list)
    ):
        return {}
    return payload


def _manifest_path_is_managed(relative_path, output_dir):
    if not isinstance(relative_path, str):
        return False
    normalized = relative_path.replace("\\", "/").lstrip("/")
    return (
        normalized.startswith(output_dir.rstrip("/") + "/")
        and normalized.endswith(".md")
        and "/../" not in f"/{normalized}/"
    )


def _is_projection_note(content):
    return "type: memory-graph-projection" in str(content)[:1024]
