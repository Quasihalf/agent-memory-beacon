"""Typed, provenance-bound contracts for the derived memory graph."""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict, deque


GRAPH_SCHEMA_VERSION = "3.0"
GRAPH_FILENAME = "memory-graph.json"
LEGACY_GRAPH_SCHEMA_VERSIONS = frozenset({"", "2.0"})
GRAPH_NODE_TYPES = frozenset(
    {
        "concept",
        "experience",
        "memory",
        "note",
        "project",
        "source",
    }
)
GRAPH_RELATION_CONTRACT = {
    "applies_to": frozenset({("memory", "concept")}),
    "belongs_to": frozenset(
        {
            ("experience", "project"),
            ("memory", "project"),
            ("note", "project"),
        }
    ),
    "contradicts": frozenset({("memory", "memory")}),
    "depends_on": frozenset({("memory", "memory")}),
    "derived_from": frozenset({("memory", "source")}),
    "links_to": frozenset({("note", "note")}),
    "operationalized_as": frozenset({("memory", "memory")}),
    "part_of_experience": frozenset({("memory", "experience")}),
    "recorded_in": frozenset({("memory", "note")}),
    "reinforced_by": frozenset({("memory", "source")}),
    "related_to": frozenset({("memory", "memory")}),
    "superseded_by": frozenset({("memory", "memory")}),
    "supports": frozenset({("memory", "memory")}),
}
SEMANTIC_PATH_RELATIONS = frozenset(
    {
        "contradicts",
        "depends_on",
        "operationalized_as",
        "superseded_by",
        "supports",
    }
)
DECLARED_MEMORY_RELATION_FIELDS = {
    "contradicts": "contradicts",
    "depends_on": "requires",
    "operationalized_as": "operationalized_as",
    "related_to": "related_to",
    "superseded_by": "superseded_by",
    "supports": "supports",
}
SEMANTIC_RELATION_WEIGHTS = {
    "contradicts": 9.0,
    "depends_on": 8.0,
    "operationalized_as": 9.0,
    "superseded_by": 9.0,
    "supports": 8.0,
}
MIN_SEMANTIC_EDGE_CONFIDENCE = 0.5
_REVISION_PATTERN = re.compile(r"[0-9a-f]{64}")
_NODE_REQUIRED_FIELDS = frozenset(
    {
        "date",
        "id",
        "kind",
        "label",
        "path",
        "project",
        "resolved",
        "revision",
        "source_refs",
        "type",
    }
)
_EDGE_REQUIRED_FIELDS = frozenset(
    {
        "confidence",
        "evidence",
        "relation",
        "source",
        "target",
    }
)
_EVIDENCE_REQUIRED_FIELDS = frozenset(
    {
        "derivation",
        "observed_at",
        "source_ref",
        "source_revision",
    }
)


def graph_path_for_index(index_path):
    """Return the canonical sibling path for a recall index's graph."""
    return os.path.join(
        os.path.dirname(os.path.abspath(os.fspath(index_path))),
        GRAPH_FILENAME,
    )


def graph_node(
    node_id,
    node_type,
    kind,
    label,
    *,
    path="",
    project="",
    date="",
    revision="",
    source_refs=None,
    resolved=True,
):
    """Return one fixed-shape graph node."""
    node = {
        "id": _scalar(node_id, "node id", 512),
        "type": _scalar(node_type, "node type", 64),
        "kind": _scalar(kind, "node kind", 96),
        "label": _scalar(label or node_id, "node label", 160),
        "path": _optional_scalar(path, "node path", 512),
        "project": _optional_scalar(project, "node project", 120),
        "date": _optional_scalar(date, "node date", 64),
        "revision": _optional_scalar(revision, "node revision", 64),
        "source_refs": _string_list(source_refs),
        "resolved": bool(resolved),
    }
    if node["type"] not in GRAPH_NODE_TYPES:
        raise ValueError(f"unsupported graph node type: {node['type']}")
    if node["revision"] and not _REVISION_PATTERN.fullmatch(node["revision"]):
        raise ValueError("graph node revision is invalid")
    if node["type"] == "memory" and node["resolved"] and not node["revision"]:
        raise ValueError("resolved memory graph node requires revision")
    return node


def graph_evidence(
    source_ref,
    *,
    source_revision="",
    observed_at="",
    derivation,
):
    """Return privacy-safe provenance for one derived edge."""
    revision = _optional_scalar(source_revision, "evidence revision", 64)
    if revision and not _REVISION_PATTERN.fullmatch(revision):
        raise ValueError("graph evidence revision is invalid")
    return {
        "source_ref": _scalar(source_ref, "evidence source", 512),
        "source_revision": revision,
        "observed_at": _optional_scalar(observed_at, "evidence date", 64),
        "derivation": _scalar(derivation, "evidence derivation", 64),
    }


def upsert_graph_node(nodes, node):
    """Insert a node, upgrading unresolved references deterministically."""
    node_id = node["id"]
    existing = nodes.get(node_id)
    if existing is None:
        nodes[node_id] = dict(node)
        return
    if existing["type"] != node["type"]:
        raise ValueError(
            f"graph node type conflict for {node_id}: "
            f"{existing['type']} != {node['type']}"
        )
    if not existing.get("resolved") and node.get("resolved"):
        nodes[node_id] = dict(node)
        return
    merged = dict(existing)
    merged["source_refs"] = sorted(
        set(existing.get("source_refs") or []) | set(node.get("source_refs") or [])
    )
    for key in ("date", "kind", "label", "path", "project", "revision"):
        if not merged.get(key) and node.get(key):
            merged[key] = node[key]
    nodes[node_id] = merged


def add_graph_edge(
    edges,
    nodes,
    source,
    target,
    relation,
    evidence,
    *,
    confidence=1.0,
):
    """Insert or merge one typed edge after checking its domain and range."""
    source = _scalar(source, "edge source", 512)
    target = _scalar(target, "edge target", 512)
    relation = _scalar(relation, "edge relation", 64)
    if source not in nodes or target not in nodes:
        raise ValueError(f"graph edge has unknown endpoint: {source} -> {target}")
    allowed = GRAPH_RELATION_CONTRACT.get(relation)
    pair = (nodes[source]["type"], nodes[target]["type"])
    if not allowed or pair not in allowed:
        raise ValueError(
            f"invalid graph relation {relation}: {pair[0]} -> {pair[1]}"
        )
    if isinstance(confidence, bool):
        raise ValueError("graph edge confidence must be numeric")
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        raise ValueError("graph edge confidence must be between 0 and 1")
    evidence = dict(evidence)
    if set(evidence) != _EVIDENCE_REQUIRED_FIELDS:
        raise ValueError("graph edge evidence has an invalid shape")
    if not _valid_evidence(evidence):
        raise ValueError("graph edge evidence is invalid")
    key = (source, relation, target)
    existing = edges.get(key)
    if existing is None:
        edges[key] = {
            "source": source,
            "target": target,
            "relation": relation,
            "confidence": confidence,
            "evidence": [evidence],
        }
        return
    existing["confidence"] = max(float(existing["confidence"]), confidence)
    if evidence not in existing["evidence"]:
        existing["evidence"].append(evidence)
        existing["evidence"].sort(key=_evidence_sort_key)


def analyze_memory_graph(graph, units=None, *, expected_generation_id=""):
    """Return deterministic structural and provenance quality metrics."""
    graph_is_mapping = isinstance(graph, dict)
    graph = graph if graph_is_mapping else {}
    schema_version = str(graph.get("schema_version") or "")
    raw_nodes = graph.get("nodes")
    raw_edges = graph.get("edges")
    nodes_are_list = isinstance(raw_nodes, list)
    edges_are_list = isinstance(raw_edges, list)
    nodes = raw_nodes if nodes_are_list else []
    edges = raw_edges if edges_are_list else []
    generation_id = str(graph.get("generation_id") or "")
    legacy = schema_version in LEGACY_GRAPH_SCHEMA_VERSIONS
    legacy_shape_valid = (
        _valid_legacy_graph(nodes, edges)
        if legacy and nodes_are_list and edges_are_list
        else not legacy
    )
    invalid_graph_shape = int(
        not graph_is_mapping
        or not nodes_are_list
        or not edges_are_list
        or (legacy and not legacy_shape_valid)
        or (schema_version == GRAPH_SCHEMA_VERSION and not generation_id)
    )
    generation_mismatch = int(
        schema_version == GRAPH_SCHEMA_VERSION
        and bool(expected_generation_id)
        and generation_id != str(expected_generation_id)
    )
    has_unit_snapshot = units is not None
    unit_snapshot = list(units or [])
    unit_by_id = {
        str(unit.get("id")): unit
        for unit in unit_snapshot
        if isinstance(unit, dict) and unit.get("id")
    }
    unit_revisions = {
        str(unit.get("id")): str(unit.get("revision"))
        for unit in unit_snapshot
        if isinstance(unit, dict) and unit.get("id") and unit.get("revision")
    }

    invalid_nodes = 0
    duplicate_node_ids = 0
    node_by_id = {}
    for node in nodes:
        if not _valid_node(node):
            invalid_nodes += 1
            continue
        if node["id"] in node_by_id:
            duplicate_node_ids += 1
            continue
        node_by_id[node["id"]] = node

    stale_revision_nodes = 0
    orphaned_memory_nodes = 0
    missing_memory_nodes = 0
    if has_unit_snapshot:
        resolved_memory_ids = {
            node["id"]
            for node in node_by_id.values()
            if node["type"] == "memory" and node.get("resolved")
        }
        missing_memory_nodes = len(set(unit_revisions) - resolved_memory_ids)
        for node in node_by_id.values():
            if node["type"] != "memory" or not node.get("resolved"):
                continue
            current_revision = unit_revisions.get(node["id"])
            if current_revision is None:
                orphaned_memory_nodes += 1
            elif node["revision"] != current_revision:
                stale_revision_nodes += 1

    invalid_edges = 0
    missing_evidence = 0
    unbound_evidence = 0
    stale_revision_edges = 0
    undeclared_semantic_edges = 0
    duplicate_edges = 0
    edge_keys = set()
    degree = Counter()
    relation_counts = Counter()
    for edge in edges:
        if not isinstance(edge, dict):
            invalid_edges += 1
            continue
        source = str(edge.get("source") or "")
        target = str(edge.get("target") or "")
        relation = str(edge.get("relation") or "")
        key = (source, relation, target)
        if key in edge_keys:
            duplicate_edges += 1
        edge_keys.add(key)
        relation_counts[relation] += 1
        source_node = node_by_id.get(source)
        target_node = node_by_id.get(target)
        if (
            not _valid_edge_shape(edge)
            or source_node is None
            or target_node is None
            or (
                source_node["type"],
                target_node["type"],
            )
            not in GRAPH_RELATION_CONTRACT.get(relation, ())
        ):
            invalid_edges += 1
            continue
        degree[source] += 1
        degree[target] += 1
        evidence_items = edge.get("evidence")
        trusted_source_revision = unit_revisions.get(source)
        if trusted_source_revision is None:
            trusted_source_revision = str(source_node.get("revision") or "")
        source_unit = unit_by_id.get(source)
        declared_field = DECLARED_MEMORY_RELATION_FIELDS.get(relation)
        if (
            has_unit_snapshot
            and declared_field
            and source_unit is not None
            and target
            not in _declared_relation_targets(source_unit, declared_field)
        ):
            undeclared_semantic_edges += 1
        source_refs = set(source_node.get("source_refs") or [])
        evidence_valid = True
        edge_stale = False
        for evidence in evidence_items:
            if not _valid_evidence(evidence):
                evidence_valid = False
                continue
            revision = evidence["source_revision"]
            if not trusted_source_revision or not revision:
                evidence_valid = False
            if evidence["source_ref"] not in source_refs:
                unbound_evidence += 1
            if (
                trusted_source_revision
                and revision
                and revision != trusted_source_revision
            ):
                edge_stale = True
        if not evidence_valid:
            missing_evidence += 1
        if edge_stale:
            stale_revision_edges += 1

    unresolved_reference_nodes = sum(
        1 for node in node_by_id.values() if not node.get("resolved")
    )
    isolated_nodes = sum(
        1 for node_id in node_by_id if degree.get(node_id, 0) == 0
    )
    blocking = (
        invalid_graph_shape
        + generation_mismatch
        + invalid_nodes
        + duplicate_node_ids
        + invalid_edges
        + duplicate_edges
        + missing_evidence
        + unbound_evidence
        + stale_revision_edges
        + undeclared_semantic_edges
        + stale_revision_nodes
        + orphaned_memory_nodes
        + missing_memory_nodes
    )
    return {
        "schema_version": schema_version,
        "generation_id": generation_id,
        "legacy": legacy,
        "nodes": len(nodes),
        "edges": len(edges),
        "invalid_graph_shape": invalid_graph_shape,
        "generation_mismatch": generation_mismatch,
        "invalid_nodes": invalid_nodes,
        "duplicate_node_ids": duplicate_node_ids,
        "invalid_edges": invalid_edges,
        "duplicate_edges": duplicate_edges,
        "missing_evidence": missing_evidence,
        "unbound_evidence": unbound_evidence,
        "stale_revision_edges": stale_revision_edges,
        "undeclared_semantic_edges": undeclared_semantic_edges,
        "stale_revision_nodes": stale_revision_nodes,
        "orphaned_memory_nodes": orphaned_memory_nodes,
        "missing_memory_nodes": missing_memory_nodes,
        "unresolved_reference_nodes": unresolved_reference_nodes,
        "isolated_nodes": isolated_nodes,
        "relation_counts": dict(sorted(relation_counts.items())),
        "valid": (
            (legacy and invalid_graph_shape == 0)
            or (schema_version == GRAPH_SCHEMA_VERSION and blocking == 0)
        ),
    }


def validate_memory_graph(
    graph,
    units=None,
    *,
    allow_legacy=True,
    expected_generation_id="",
):
    """Reject malformed v3 graphs while permitting read-only v2 compatibility."""
    quality = analyze_memory_graph(
        graph,
        units,
        expected_generation_id=expected_generation_id,
    )
    if quality["legacy"] and allow_legacy:
        if quality["valid"]:
            return quality
        raise ValueError(
            "invalid memory graph: "
            f"invalid_graph_shape={quality['invalid_graph_shape']}"
        )
    if quality["schema_version"] != GRAPH_SCHEMA_VERSION:
        raise ValueError(
            f"memory graph schema must be {GRAPH_SCHEMA_VERSION}"
        )
    if not quality["valid"]:
        problems = [
            f"{key}={quality[key]}"
            for key in (
                "invalid_graph_shape",
                "generation_mismatch",
                "invalid_nodes",
                "duplicate_node_ids",
                "invalid_edges",
                "duplicate_edges",
                "missing_evidence",
                "unbound_evidence",
                "stale_revision_edges",
                "undeclared_semantic_edges",
                "stale_revision_nodes",
                "orphaned_memory_nodes",
                "missing_memory_nodes",
            )
            if quality[key]
        ]
        raise ValueError("invalid memory graph: " + ", ".join(problems))
    return quality


def semantic_memory_paths(
    graph,
    seed_ids,
    units,
    *,
    max_hops=2,
    validated=False,
    allowed_node_ids=None,
    seed_scores=None,
):
    """Return the strongest bounded semantic path from content anchors."""
    if not isinstance(graph, dict) or graph.get("schema_version") != GRAPH_SCHEMA_VERSION:
        return {}
    max_hops = max(0, min(int(max_hops or 0), 2))
    if not max_hops:
        return {}
    unit_by_id = {
        str(unit.get("id")): unit
        for unit in units or []
        if isinstance(unit, dict) and unit.get("id") and unit.get("revision")
    }
    if not validated:
        validate_memory_graph(graph, unit_by_id.values(), allow_legacy=False)
    traversable_ids = (
        set(unit_by_id)
        if allowed_node_ids is None
        else set(allowed_node_ids) & set(unit_by_id)
    )
    seed_scores = {
        str(key): float(value or 0)
        for key, value in dict(seed_scores or {}).items()
    }
    seeds = sorted(set(seed_ids or []) & traversable_ids)
    if not seeds:
        return {}

    adjacency = defaultdict(list)
    for edge in graph.get("edges") or []:
        relation = edge.get("relation")
        source = edge.get("source")
        target = edge.get("target")
        confidence = float(edge.get("confidence") or 0)
        if (
            relation not in SEMANTIC_PATH_RELATIONS
            or source not in traversable_ids
            or target not in traversable_ids
            or confidence < MIN_SEMANTIC_EDGE_CONFIDENCE
        ):
            continue
        forward = {
            "source": source,
            "relation": relation,
            "target": target,
            "direction": "forward",
            "confidence": confidence,
        }
        reverse = {
            "source": source,
            "relation": relation,
            "target": target,
            "direction": "reverse",
            "confidence": confidence,
        }
        adjacency[source].append((target, forward))
        adjacency[target].append((source, reverse))
    for node_id in adjacency:
        adjacency[node_id].sort(
            key=lambda item: (
                item[1]["relation"],
                item[0],
                item[1]["direction"],
            )
        )

    found = {}
    for seed in seeds:
        queue = deque([(seed, [])])
        best_depth = {seed: 0}
        while queue:
            current, path = queue.popleft()
            if len(path) >= max_hops:
                continue
            for neighbor, step in adjacency.get(current, ()):
                next_path = [*path, step]
                depth = len(next_path)
                if depth > best_depth.get(neighbor, max_hops + 1):
                    continue
                best_depth[neighbor] = depth
                if neighbor != seed:
                    candidate = {
                        "seed": seed,
                        "path": next_path,
                        "score": _semantic_path_score(next_path),
                        "confidence": min(
                            step["confidence"] for step in next_path
                        ),
                        "seed_score": seed_scores.get(seed, 0),
                    }
                    existing = found.get(neighbor)
                    if existing is None or _path_rank(candidate) < _path_rank(existing):
                        found[neighbor] = candidate
                if depth < max_hops:
                    queue.append((neighbor, next_path))
    return found


def render_memory_graph_quality_markdown(graph, units=None):
    """Render a human-readable, generated graph quality report."""
    quality = analyze_memory_graph(graph, units)
    lines = [
        "---",
        "title: Memory Graph Quality",
        "type: memory-graph-quality",
        f"schema_version: '{quality['schema_version']}'",
        f"generation_id: '{quality['generation_id']}'",
        "generated_by: memory_graph.py",
        f"invalid_graph_shape: {quality['invalid_graph_shape']}",
        f"generation_mismatch: {quality['generation_mismatch']}",
        f"invalid_nodes: {quality['invalid_nodes']}",
        f"invalid_edges: {quality['invalid_edges']}",
        f"missing_evidence: {quality['missing_evidence']}",
        f"unbound_evidence: {quality['unbound_evidence']}",
        f"stale_revision_edges: {quality['stale_revision_edges']}",
        f"undeclared_semantic_edges: {quality['undeclared_semantic_edges']}",
        f"stale_revision_nodes: {quality['stale_revision_nodes']}",
        f"orphaned_memory_nodes: {quality['orphaned_memory_nodes']}",
        f"missing_memory_nodes: {quality['missing_memory_nodes']}",
        "---",
        "",
        "# Memory Graph Quality",
        "",
        f"- Status: `{'PASS' if quality['valid'] else 'FAIL'}`",
        f"- Nodes: `{quality['nodes']}`",
        f"- Edges: `{quality['edges']}`",
        f"- Duplicate node IDs: `{quality['duplicate_node_ids']}`",
        f"- Duplicate edges: `{quality['duplicate_edges']}`",
        f"- Missing runtime memory nodes: `{quality['missing_memory_nodes']}`",
        f"- Evidence outside source provenance: `{quality['unbound_evidence']}`",
        f"- Undeclared semantic edges: `{quality['undeclared_semantic_edges']}`",
        f"- Stale memory revisions: `{quality['stale_revision_nodes']}`",
        f"- Orphaned resolved memories: `{quality['orphaned_memory_nodes']}`",
        f"- Unresolved references: `{quality['unresolved_reference_nodes']}`",
        f"- Isolated nodes: `{quality['isolated_nodes']}`",
        "",
        "## Relations",
        "",
        "| Relation | Count |",
        "|---|---:|",
    ]
    for relation, count in quality["relation_counts"].items():
        lines.append(f"| `{relation}` | {count} |")
    if not quality["relation_counts"]:
        lines.append("| - | 0 |")
    return "\n".join(lines) + "\n"


def _valid_node(node):
    if not isinstance(node, dict) or not _NODE_REQUIRED_FIELDS.issubset(node):
        return False
    if (
        not isinstance(node.get("id"), str)
        or not node["id"]
        or node.get("type") not in GRAPH_NODE_TYPES
        or not isinstance(node.get("kind"), str)
        or not isinstance(node.get("label"), str)
        or not isinstance(node.get("path"), str)
        or not isinstance(node.get("project"), str)
        or not isinstance(node.get("date"), str)
        or not isinstance(node.get("revision"), str)
        or not isinstance(node.get("source_refs"), list)
        or not all(isinstance(item, str) and item for item in node["source_refs"])
        or not isinstance(node.get("resolved"), bool)
    ):
        return False
    revision = node["revision"]
    if revision and not _REVISION_PATTERN.fullmatch(revision):
        return False
    return not (
        node["type"] == "memory"
        and node["resolved"]
        and not revision
    )


def _valid_edge_shape(edge):
    if not _EDGE_REQUIRED_FIELDS.issubset(edge):
        return False
    confidence = edge.get("confidence")
    return (
        isinstance(edge.get("source"), str)
        and bool(edge["source"])
        and isinstance(edge.get("target"), str)
        and bool(edge["target"])
        and isinstance(edge.get("relation"), str)
        and bool(edge["relation"])
        and isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and 0 <= float(confidence) <= 1
        and isinstance(edge.get("evidence"), list)
        and bool(edge["evidence"])
    )


def _valid_evidence(evidence):
    return (
        isinstance(evidence, dict)
        and set(evidence) == _EVIDENCE_REQUIRED_FIELDS
        and isinstance(evidence.get("source_ref"), str)
        and bool(evidence["source_ref"])
        and isinstance(evidence.get("source_revision"), str)
        and (
            not evidence["source_revision"]
            or bool(_REVISION_PATTERN.fullmatch(evidence["source_revision"]))
        )
        and isinstance(evidence.get("observed_at"), str)
        and isinstance(evidence.get("derivation"), str)
        and bool(evidence["derivation"])
    )


def _valid_legacy_graph(nodes, edges):
    node_ids = set()
    for node in nodes:
        if not isinstance(node, dict):
            return False
        node_id = node.get("id")
        if not isinstance(node_id, str) or not node_id or node_id in node_ids:
            return False
        node_ids.add(node_id)
    edge_keys = set()
    for edge in edges:
        if not isinstance(edge, dict):
            return False
        source = edge.get("source")
        target = edge.get("target")
        relation = edge.get("relation")
        key = (source, relation, target)
        if (
            not all(
                isinstance(value, str) and bool(value)
                for value in key
            )
            or source not in node_ids
            or target not in node_ids
            or relation not in GRAPH_RELATION_CONTRACT
            or key in edge_keys
        ):
            return False
        edge_keys.add(key)
    return True


def _declared_relation_targets(unit, field):
    value = unit.get(field)
    if field == "superseded_by":
        return {str(value).strip()} if str(value or "").strip() else set()
    if not isinstance(value, (list, tuple, set, frozenset)):
        return set()
    return {
        str(item).strip()
        for item in value
        if str(item or "").strip()
    }


def _semantic_path_score(path):
    weakest = min(
        SEMANTIC_RELATION_WEIGHTS.get(step["relation"], 1.0)
        for step in path
    )
    confidence = min(float(step["confidence"]) for step in path)
    relation_score = max(1.0, weakest - max(0, len(path) - 1) * 2.0)
    return relation_score * confidence


def _path_rank(candidate):
    path = candidate["path"]
    calibrated_score = (
        float(candidate.get("seed_score") or 0)
        * float(candidate.get("confidence") or 0)
        * (0.85 ** max(0, len(path) - 1))
    )
    return (
        -calibrated_score,
        len(path),
        -float(candidate["score"]),
        json.dumps(path, ensure_ascii=False, sort_keys=True),
        candidate["seed"],
    )


def _evidence_sort_key(evidence):
    return (
        evidence["source_ref"],
        evidence["source_revision"],
        evidence["observed_at"],
        evidence["derivation"],
    )


def _scalar(value, label, limit):
    value = str(value or "").strip()
    if not value or "\n" in value or "\r" in value or len(value) > limit:
        raise ValueError(f"{label} is invalid")
    return value


def _optional_scalar(value, label, limit):
    value = str(value or "").strip()
    if "\n" in value or "\r" in value or len(value) > limit:
        raise ValueError(f"{label} is invalid")
    return value


def _string_list(values):
    result = []
    for value in values or []:
        value = _scalar(value, "source ref", 512)
        if value not in result:
            result.append(value)
    return sorted(result)
