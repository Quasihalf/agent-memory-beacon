#!/usr/bin/env python3
"""Run versioned, privacy-safe quality and latency checks for memory runtime."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import MEMORY_RUNTIME_DEFAULTS
from memory_runtime import (
    JsonStateStore,
    PromptEvent,
    RuntimePolicy,
    TriggerDecision,
    decide_trigger,
    estimate_tokens,
    handle_prompt,
    render_refresh,
    retrieve_memories,
    topic_signature,
)
from memory_graph import (
    GRAPH_SCHEMA_VERSION,
    semantic_memory_paths,
    validate_memory_graph,
)
from memory_schema import RUNTIME_SCHEMA_VERSION, memory_revision
from memory_recall import validate_recall_index


FIXTURE_NOW = datetime(2026, 7, 13, 8, 0, tzinfo=timezone(timedelta(hours=8)))
FIXTURE_INDEX_VERSION = (100, 200, 300, 400)
MAX_LONG_TASK_INJECTIONS = 19
FIXTURE_CASE_SCHEMA_VERSION = "1.4"
# Append a new entry when intentionally changing the evaluated assertion baseline.
ASSERTION_CONTRACT_BASELINES = {
    1: "78c4eade685a8554decb051930065593085849b1f41279e97fe2aa3c03298702",
    2: "75c7608a2f0a16e84de12c92e0518f7962b3529fe41ed63bed0837145777f4ca",
    3: "af58d0603dc7e246c5677ee1f0526bde521952a99dd241325d8ee84ce9740233",
    4: "61c56e94ece2fb120f817d99fef2bc1b8d780014319eeb339f1da92160631306",
}
CURRENT_ASSERTION_CONTRACT_VERSION = max(ASSERTION_CONTRACT_BASELINES)
REQUIRED_RETRIEVAL_CASE_IDS = frozenset(
    {
        "pensive-workflow",
        "github-source-first",
        "global-chinese-preference",
        "global-humanizer-skill",
        "formal-index-decision",
        "path-error",
        "reconnect-error",
        "tcad-decision",
        "tcad-license-error",
        "tcad-remote-workflow",
        "cross-project-common-term",
        "unknown-project-is-global-only",
        "candidate-lure",
        "session-lure",
        "insight-one-shot-seed",
        "insight-ordinary-suppressed",
        "insight-ambiguous-suppressed",
        "insight-inventory-safe",
        "graph-v3-semantic-paths",
        "conversation-summary-context",
    }
)
REQUIRED_TRIGGER_CASE_IDS = frozenset(
    {
        "first",
        "first-short",
        "same-topic",
        "topic-changed",
        "stale",
        "index-changed",
        "risk-error",
        "short-index-change",
        "weak-topic",
    }
)


class MutableIndexStore:
    def __init__(self, index):
        self.index = index
        self.current_version = FIXTURE_INDEX_VERSION
        self.load_calls = 0

    def version(self):
        return self.current_version

    def load(self):
        self.load_calls += 1
        return self.index

    def replace(self, index):
        self.index = index
        dev, inode, size, modified = self.current_version
        self.current_version = (dev, inode + 1, size + 1, modified + 1)


def load_fixtures(fixture_dir):
    fixture_dir = Path(fixture_dir)
    index = _load_json(fixture_dir / "index.json")
    graph = _load_json(fixture_dir / "graph.json")
    cases = _load_json(fixture_dir / "cases.json")
    validate_recall_index(index)
    if index.get("unit_count") != len(index.get("units", [])):
        raise ValueError("fixture index unit_count does not match units")
    if graph.get("schema_version") != GRAPH_SCHEMA_VERSION:
        raise ValueError("fixture graph schema is incompatible")
    validate_memory_graph(
        graph,
        index.get("units"),
        allow_legacy=False,
        expected_generation_id=index.get("generation_id", ""),
    )
    if cases.get("schema_version") != FIXTURE_CASE_SCHEMA_VERSION:
        raise ValueError("fixture case schema is incompatible")
    validate_case_manifest(cases)
    runtime_index = copy.deepcopy(index)
    runtime_index["_graph"] = graph
    runtime_index["_graph_validated"] = True
    return {"index": runtime_index, "graph": graph, "cases": cases}


def validate_case_manifest(cases):
    if not isinstance(cases, dict):
        raise ValueError("fixture cases must be an object")
    retrieval_cases = cases.get("retrieval_cases")
    trigger_cases = cases.get("trigger_cases")
    long_task = cases.get("long_task")
    if not isinstance(retrieval_cases, list) or not isinstance(trigger_cases, list):
        raise ValueError("fixture retrieval_cases and trigger_cases must be lists")
    if not isinstance(long_task, dict):
        raise ValueError("fixture long_task must be an object")

    retrieval_ids = _unique_case_ids(retrieval_cases, "retrieval")
    trigger_ids = _unique_case_ids(trigger_cases, "trigger")
    if retrieval_ids != REQUIRED_RETRIEVAL_CASE_IDS:
        raise ValueError("fixture retrieval case manifest changed")
    if trigger_ids != REQUIRED_TRIGGER_CASE_IDS:
        raise ValueError("fixture trigger case manifest changed")

    retrieval_fields = {
        "id",
        "prompt",
        "cwd",
        "trigger",
        "allowed_ids",
        "required_ids",
        "forbidden_ids",
    }
    for case in retrieval_cases:
        if not retrieval_fields.issubset(case):
            raise ValueError(f"retrieval case {case.get('id')} lacks assertions")
        for field in ("allowed_ids", "required_ids", "forbidden_ids"):
            if not _string_list(case.get(field)):
                raise ValueError(f"retrieval case {case.get('id')} has invalid {field}")
        for field in (
            "critical_error_ids",
            "candidate_lure_ids",
            "assistant_lure_ids",
        ):
            value = case.get(field, [])
            if not _string_list(value):
                raise ValueError(f"retrieval case {case.get('id')} has invalid {field}")

    trigger_fields = {"id", "prompt", "state", "expected_trigger"}
    for case in trigger_cases:
        if not trigger_fields.issubset(case):
            raise ValueError(f"trigger case {case.get('id')} lacks assertions")
        if "irrelevant" in case and not isinstance(case.get("irrelevant"), bool):
            raise ValueError(f"trigger case {case.get('id')} has invalid irrelevant flag")

    if not any(case.get("allowed_ids") for case in retrieval_cases):
        raise ValueError("fixture precision denominator is empty")
    if not any(case.get("critical_error_ids") for case in retrieval_cases):
        raise ValueError("fixture critical-error denominator is empty")
    if not any(case.get("irrelevant") for case in trigger_cases):
        raise ValueError("fixture irrelevant-trigger denominator is empty")

    required_long_fields = {
        "message_count",
        "base_ids",
        "fresh_id",
        "editable_id",
        "deletable_id",
    }
    if not required_long_fields.issubset(long_task):
        raise ValueError("fixture long_task lacks required assertions")
    if long_task.get("message_count") != 100 or not _string_list(long_task.get("base_ids")):
        raise ValueError("fixture long_task contract changed")

    _validate_assertion_contract(cases)


def _validate_assertion_contract(cases):
    contract = cases.get("assertion_contract")
    if not isinstance(contract, dict):
        raise ValueError("fixture assertion contract is missing")

    version = contract.get("version")
    expected_digest = ASSERTION_CONTRACT_BASELINES.get(version)
    if version != CURRENT_ASSERTION_CONTRACT_VERSION or expected_digest is None:
        raise ValueError("fixture assertion contract version is incompatible")
    if contract.get("sha256") != expected_digest:
        raise ValueError("fixture assertion contract digest is incompatible")
    if _assertion_contract_sha256(cases) != expected_digest:
        raise ValueError(
            "fixture assertion contract changed; add a new baseline version and digest"
        )


def _assertion_contract_sha256(cases):
    payload = {
        key: value
        for key, value in cases.items()
        if key != "assertion_contract"
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _unique_case_ids(cases, label):
    ids = []
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError(f"fixture {label} case has invalid ID")
        ids.append(case["id"])
    if len(ids) != len(set(ids)):
        raise ValueError(f"fixture {label} case IDs are duplicated")
    return frozenset(ids)


def _string_list(value):
    return isinstance(value, list) and all(
        isinstance(item, str) and bool(item) for item in value
    )


def evaluate_cases(fixtures):
    cases = fixtures["cases"]
    index = fixtures["index"]
    config = {
        "projects": cases.get("projects", []),
        "project_keywords": cases.get("project_keywords", {}),
    }
    policy = RuntimePolicy.from_config(MEMORY_RUNTIME_DEFAULTS)
    relevant_hits = 0
    returned_count = 0
    critical_hits = 0
    critical_total = 0
    candidate_leaks = 0
    assistant_source_acceptances = 0
    duplicate_memories = 0
    max_estimated_tokens = 0
    recall_durations = []
    failures = []

    for case in cases.get("retrieval_cases", []):
        event = PromptEvent(
            session_key=f"fixture:{case['id']}",
            prompt=case["prompt"],
            cwd=case.get("cwd", ""),
        )
        trigger = TriggerDecision(
            triggered=True,
            primary_reason=case.get("trigger", "first_prompt"),
            reasons=(case.get("trigger", "first_prompt"),),
            substantive=True,
            risk_or_error=bool(case.get("risk")),
        )
        started = time.perf_counter()
        results = retrieve_memories(
            event,
            index,
            {},
            trigger,
            policy,
            config,
            now=FIXTURE_NOW,
        )
        rendered = render_refresh(trigger, results, policy.token_budget)
        recall_durations.append((time.perf_counter() - started) * 1000)
        ids = [item.get("id") for item in results]
        returned_count += len(ids)
        allowed = set(case.get("allowed_ids", []))
        relevant_hits += sum(memory_id in allowed for memory_id in ids)

        missing = set(case.get("required_ids", [])) - set(ids)
        forbidden = set(case.get("forbidden_ids", [])) & set(ids)
        if missing:
            failures.append(f"{case['id']}:missing:{','.join(sorted(missing))}")
        if forbidden:
            failures.append(f"{case['id']}:forbidden:{','.join(sorted(forbidden))}")

        critical = set(case.get("critical_error_ids", []))
        critical_total += len(critical)
        critical_hits += len(critical & set(ids))
        candidate_leaks += len(set(case.get("candidate_lure_ids", [])) & set(ids))
        assistant_source_acceptances += len(
            set(case.get("assistant_lure_ids", [])) & set(ids)
        )
        duplicate_memories += _duplicate_count(results)
        max_estimated_tokens = max(max_estimated_tokens, estimate_tokens(rendered))

    trigger_metrics = _evaluate_trigger_cases(cases, policy)
    failures.extend(trigger_metrics["failures"])
    return {
        "precision_at_k": relevant_hits / returned_count if returned_count else 1.0,
        "critical_error_recall": critical_hits / critical_total if critical_total else 1.0,
        "irrelevant_trigger_rate": trigger_metrics["irrelevant_trigger_rate"],
        "candidate_leaks": candidate_leaks,
        "assistant_source_acceptances": assistant_source_acceptances,
        "duplicate_memories": duplicate_memories,
        "max_estimated_tokens": max_estimated_tokens,
        "recall_durations": recall_durations,
        "no_trigger_durations": trigger_metrics["no_trigger_durations"],
        "case_failures": failures,
    }


def simulate_long_task(fixtures):
    cases = fixtures["cases"]
    long_case = cases["long_task"]
    by_id = {unit["id"]: copy.deepcopy(unit) for unit in fixtures["index"]["units"]}
    active_ids = list(long_case["base_ids"])
    active_ids.extend(["preference:chinese", "skill:humanizer"])
    index = _index_for_ids(by_id, active_ids, fixtures["graph"])
    index_store = MutableIndexStore(index)
    injection_count = 0
    silent_count = 0
    new_memory_seen = False
    changed_revision_seen = False
    deleted_residuals = 0
    recall_durations = []
    no_trigger_durations = []

    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp) / "vault"
        runtime = dict(MEMORY_RUNTIME_DEFAULTS)
        runtime.update(
            {
                "resolved_index_path": str(vault / "05-Agent-Memory/recall-index.json"),
                "resolved_state_dir": str(vault / "04-Feedback/_logs/recall-state"),
                "resolved_log_path": str(vault / "04-Feedback/_logs/recall-hook.jsonl"),
            }
        )
        config = {
            "vault_path": str(vault),
            "memory_runtime": runtime,
            "projects": cases.get("projects", []),
            "project_keywords": cases.get("project_keywords", {}),
        }
        state_store = JsonStateStore(runtime["resolved_state_dir"])

        for number in range(int(long_case["message_count"])):
            prompt = "formalprobe 检查正式索引"
            if number == 10:
                prompt = "deletelongprobe 检查长期记忆"
            elif number == 20:
                active_ids.append(long_case["fresh_id"])
                index_store.replace(
                    _index_for_ids(by_id, active_ids, fixtures["graph"])
                )
                prompt = "freshlongprobe 检查新晋升记忆"
            elif number == 40:
                prompt = "humanizer 模板自然表达"
            elif number == 60:
                editable = copy.deepcopy(by_id[long_case["editable_id"]])
                editable["summary"] = "editlongprobe 更新后的修订已经生效"
                editable["recall_summary"] = editable["summary"]
                editable["revision"] = memory_revision(editable)
                by_id[long_case["editable_id"]] = editable
                index_store.replace(
                    _index_for_ids(by_id, active_ids, fixtures["graph"])
                )
                prompt = "editlongprobe 检查更新后的修订"
            elif number == 80:
                active_ids.remove(long_case["deletable_id"])
                index_store.replace(
                    _index_for_ids(by_id, active_ids, fixtures["graph"])
                )
                prompt = "deletelongprobe 检查长期记忆"
            elif number % 11 == 0:
                prompt = "可以"

            event = PromptEvent(
                session_key="fixture:long-task",
                prompt=prompt,
                cwd="/tmp/agent-memory-beacon",
            )
            now = FIXTURE_NOW + timedelta(minutes=number)
            started = time.perf_counter()
            result = handle_prompt(
                event,
                config,
                clock=lambda now=now: now,
                monotonic=time.perf_counter,
                index_store=index_store,
                state_store=state_store,
            )
            duration = (time.perf_counter() - started) * 1000
            if result.additional_context:
                injection_count += 1
                recall_durations.append(duration)
            else:
                silent_count += 1
                no_trigger_durations.append(duration)
            if number >= 20 and by_id[long_case["fresh_id"]]["title"] in result.additional_context:
                new_memory_seen = True
            if number >= 60 and "更新后的修订已经生效" in result.additional_context:
                changed_revision_seen = True
            if number >= 80 and by_id[long_case["deletable_id"]]["title"] in result.additional_context:
                deleted_residuals += 1

    return {
        "message_count": int(long_case["message_count"]),
        "injection_count": injection_count,
        "silent_count": silent_count,
        "new_memory_seen": new_memory_seen,
        "changed_revision_seen": changed_revision_seen,
        "deleted_residuals": deleted_residuals,
        "recall_durations": recall_durations,
        "no_trigger_durations": no_trigger_durations,
    }


def evaluate_fixture_dir(fixture_dir):
    fixtures = load_fixtures(fixture_dir)
    case_metrics = evaluate_cases(fixtures)
    long_task = simulate_long_task(fixtures)
    graph_scale = evaluate_graph_scale()
    failures = list(case_metrics["case_failures"])
    failures.extend(graph_scale["failures"])
    if not long_task["new_memory_seen"]:
        failures.append("long-task:new-memory-not-seen")
    if not long_task["changed_revision_seen"]:
        failures.append("long-task:changed-revision-not-seen")
    report = {
        "fixture_schema_version": fixtures["cases"]["schema_version"],
        "runtime_schema_version": RUNTIME_SCHEMA_VERSION,
        "precision_at_k": round(case_metrics["precision_at_k"], 6),
        "critical_error_recall": round(case_metrics["critical_error_recall"], 6),
        "irrelevant_trigger_rate": round(
            case_metrics["irrelevant_trigger_rate"], 6
        ),
        "candidate_leaks": case_metrics["candidate_leaks"],
        "assistant_source_acceptances": case_metrics[
            "assistant_source_acceptances"
        ],
        "duplicate_memories": case_metrics["duplicate_memories"],
        "deleted_residuals": long_task["deleted_residuals"],
        "no_trigger_p95_ms": round(
            _percentile_95(
                case_metrics["no_trigger_durations"]
                + long_task["no_trigger_durations"]
            ),
            3,
        ),
        "recall_p95_ms": round(
            _percentile_95(
                case_metrics["recall_durations"] + long_task["recall_durations"]
            ),
            3,
        ),
        "graph_scale_p95_ms": round(graph_scale["p95_ms"], 3),
        "graph_scale_nodes": graph_scale["nodes"],
        "graph_scale_edges": graph_scale["edges"],
        "max_estimated_tokens": case_metrics["max_estimated_tokens"],
        "case_failures": sorted(failures),
        "long_task": {
            key: value
            for key, value in long_task.items()
            if not key.endswith("durations")
        },
    }
    return report


def report_passes(report):
    return bool(
        report.get("precision_at_k", 0) >= 0.85
        and report.get("critical_error_recall", 0) >= 0.80
        and report.get("irrelevant_trigger_rate", 1) <= 0.10
        and report.get("candidate_leaks") == 0
        and report.get("assistant_source_acceptances") == 0
        and report.get("duplicate_memories") == 0
        and report.get("deleted_residuals") == 0
        and report.get("no_trigger_p95_ms", float("inf")) <= 100
        and report.get("recall_p95_ms", float("inf")) <= 500
        and report.get("graph_scale_p95_ms", float("inf")) <= 500
        and report.get("max_estimated_tokens", float("inf")) <= 1500
        and (report.get("long_task") or {}).get(
            "injection_count", float("inf")
        ) <= MAX_LONG_TASK_INJECTIONS
        and not report.get("case_failures")
    )


def evaluate_graph_scale():
    """Exercise bounded semantic traversal at the current production scale."""
    node_count = 1600
    generation_id = "runtime-scale-fixture"
    units = []
    nodes = []
    revisions = {}
    for index in range(node_count):
        memory_id = f"decision:scale-{index}"
        revision = hashlib.sha256(memory_id.encode("utf-8")).hexdigest()
        source_ref = f"source:scale-{index}"
        revisions[memory_id] = revision
        units.append(
            {
                "id": memory_id,
                "type": "decision",
                "revision": revision,
                "requires": [],
            }
        )
        nodes.append(
            {
                "id": memory_id,
                "type": "memory",
                "kind": "decision",
                "label": memory_id,
                "path": "01-Projects/scale/Memory/decisions",
                "project": "scale",
                "date": "2026-07-26",
                "revision": revision,
                "source_refs": [source_ref],
                "resolved": True,
            }
        )

    edges = []
    for offset in (1, 7, 31, 127):
        for index in range(node_count):
            source = f"decision:scale-{index}"
            target = f"decision:scale-{(index + offset) % node_count}"
            units[index]["requires"].append(target)
            edges.append(_scale_edge(source, target, revisions[source], index))
    for index in range(200):
        source = f"decision:scale-{index}"
        target = f"decision:scale-{(index + 257) % node_count}"
        units[index]["requires"].append(target)
        edges.append(_scale_edge(source, target, revisions[source], index))

    graph = {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "generated_by": "evaluate_memory_runtime.py",
        "generated_at": FIXTURE_NOW.isoformat(),
        "generation_id": generation_id,
        "nodes": nodes,
        "edges": edges,
    }
    validate_memory_graph(
        graph,
        units,
        allow_legacy=False,
        expected_generation_id=generation_id,
    )

    durations = []
    result = {}
    allowed_ids = {unit["id"] for unit in units}
    for _ in range(5):
        started = time.perf_counter()
        result = semantic_memory_paths(
            graph,
            ["decision:scale-0"],
            units,
            max_hops=2,
            validated=True,
            allowed_node_ids=allowed_ids,
            seed_scores={"decision:scale-0": 100},
        )
        durations.append((time.perf_counter() - started) * 1000)

    failures = []
    if "decision:scale-1" not in result:
        failures.append("graph-scale:one-hop-missing")
    if "decision:scale-2" not in result:
        failures.append("graph-scale:two-hop-missing")
    if any(len(item.get("path") or []) > 2 for item in result.values()):
        failures.append("graph-scale:hop-bound-exceeded")
    return {
        "nodes": len(nodes),
        "edges": len(edges),
        "p95_ms": _percentile_95(durations),
        "failures": failures,
    }


def _scale_edge(source, target, revision, index):
    return {
        "source": source,
        "target": target,
        "relation": "depends_on",
        "confidence": 1.0,
        "evidence": [
            {
                "source_ref": f"source:scale-{index}",
                "source_revision": revision,
                "observed_at": "2026-07-26",
                "derivation": "scale-fixture",
            }
        ],
    }


def _evaluate_trigger_cases(cases, policy):
    false_triggers = 0
    irrelevant_count = 0
    no_trigger_durations = []
    failures = []
    for case in cases.get("trigger_cases", []):
        state_kind = case.get("state", "current")
        if state_kind == "first":
            state = {}
        else:
            baseline = case.get("baseline_prompt", "Obsidian Codex 动态召回索引")
            refreshed = FIXTURE_NOW
            version = FIXTURE_INDEX_VERSION
            if state_kind == "stale":
                refreshed = FIXTURE_NOW - timedelta(minutes=31)
            if state_kind == "index_changed":
                version = (100, 200, 300, 399)
            state = {
                "schema_version": 1,
                "session_hash": "0" * 32,
                "initialized_at": (FIXTURE_NOW - timedelta(hours=1)).isoformat(),
                "last_evaluated_index_version": list(version),
                "last_refresh_attempt_at": refreshed.isoformat(),
                "last_substantive_at": refreshed.isoformat(),
                "topic_term_weights": topic_signature(baseline),
                "pending_index_change": False,
                "recently_loaded": {},
            }
        event = PromptEvent(
            session_key=f"trigger:{case['id']}",
            prompt=case["prompt"],
            cwd="/tmp/agent-memory-beacon",
        )
        started = time.perf_counter()
        decision = decide_trigger(
            event,
            state,
            FIXTURE_INDEX_VERSION,
            policy,
            FIXTURE_NOW,
        )
        duration = (time.perf_counter() - started) * 1000
        expected = case.get("expected_trigger", "")
        if decision.primary_reason != expected:
            failures.append(
                f"trigger:{case['id']}:expected:{expected}:actual:{decision.primary_reason}"
            )
        if (
            "expected_pending" in case
            and bool(case.get("expected_pending"))
            != bool(decision.pending_index_change)
        ):
            failures.append(f"trigger:{case['id']}:pending-index-mismatch")
        if case.get("irrelevant"):
            irrelevant_count += 1
            false_triggers += int(decision.triggered)
        if not decision.triggered:
            no_trigger_durations.append(duration)
    return {
        "irrelevant_trigger_rate": (
            false_triggers / irrelevant_count if irrelevant_count else 0.0
        ),
        "no_trigger_durations": no_trigger_durations,
        "failures": failures,
    }


def _index_for_ids(by_id, ids, graph):
    units = [copy.deepcopy(by_id[memory_id]) for memory_id in ids]
    generation_id = hashlib.sha256(
        json.dumps(
            {
                "base_generation_id": str(graph.get("generation_id") or ""),
                "units": units,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    selected_ids = {unit["id"] for unit in units}
    unit_by_id = {unit["id"]: unit for unit in units}
    snapshot = copy.deepcopy(graph)
    nodes = []
    included_node_ids = set()
    for node in snapshot.get("nodes") or []:
        node_id = str(node.get("id") or "")
        if node.get("type") == "memory" and node_id not in selected_ids:
            continue
        if node_id in unit_by_id:
            unit = unit_by_id[node_id]
            source_refs = list(unit.get("source_refs") or [])
            source_note = str(unit.get("source_note") or "")
            if source_note and source_note not in source_refs:
                source_refs.append(source_note)
            node.update(
                {
                    "kind": unit.get("type", ""),
                    "label": unit.get("title", node_id),
                    "path": unit.get("path", ""),
                    "project": unit.get("project", ""),
                    "date": unit.get("date", ""),
                    "revision": unit.get("revision", ""),
                    "source_refs": sorted(source_refs),
                    "resolved": True,
                }
            )
        nodes.append(node)
        included_node_ids.add(node_id)

    edges = []
    for edge in snapshot.get("edges") or []:
        if (
            edge.get("source") not in included_node_ids
            or edge.get("target") not in included_node_ids
        ):
            continue
        source_unit = unit_by_id.get(edge.get("source"))
        if source_unit:
            for evidence in edge.get("evidence") or []:
                evidence["source_revision"] = source_unit.get("revision", "")
        edges.append(edge)

    snapshot["generation_id"] = generation_id
    snapshot["nodes"] = nodes
    snapshot["edges"] = edges
    snapshot.pop("quality", None)
    runtime_index = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "generation_id": generation_id,
        "unit_count": len(units),
        "units": units,
        "_graph": snapshot,
        "_graph_validated": True,
    }
    validate_recall_index(runtime_index)
    runtime_index["_graph_quality"] = validate_memory_graph(
        snapshot,
        units,
        allow_legacy=False,
        expected_generation_id=generation_id,
    )
    return runtime_index


def _duplicate_count(results):
    ids = [item.get("id") for item in results]
    facts = [
        (
            item.get("type"),
            str(item.get("title") or "").casefold(),
            str(item.get("summary") or "").casefold(),
            item.get("project"),
        )
        for item in results
    ]
    return (len(ids) - len(set(ids))) + (len(facts) - len(set(facts)))


def _percentile_95(values):
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    index = max(0, int((len(ordered) * 0.95) + 0.999999) - 1)
    return ordered[min(index, len(ordered) - 1)]


def _load_json(path):
    with Path(path).open("r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError(f"fixture must contain a JSON object: {path}")
    return value


def main():
    parser = argparse.ArgumentParser(description="Evaluate Agent Memory Beacon runtime")
    parser.add_argument("--fixtures", required=True, help="fixture directory")
    args = parser.parse_args()
    try:
        report = evaluate_fixture_dir(args.fixtures)
    except Exception as exc:
        report = {"status": "error", "error_type": type(exc).__name__}
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report_passes(report) else 1


if __name__ == "__main__":
    raise SystemExit(main())
