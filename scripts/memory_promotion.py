#!/usr/bin/env python3
"""Generate isolated, revision-bound proposals for stronger memory owners."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Mapping

import yaml

from memory_authority import normalize_authority_metadata
from memory_effectiveness import aggregate_events, read_effectiveness_events
from memory_schema import is_valid_memory_id
from safety import durable_atomic_write, ensure_directory_tree, safe_filename, safe_vault_path


PROMOTION_SCHEMA_VERSION = "1.0"
DEFAULT_PROPOSAL_DIR = "04-Feedback/_promotion-proposals"
ELIGIBLE_TYPES = frozenset({"decision", "error", "workflow"})
PROPOSAL_FIELDS = frozenset(
    {
        "schema_version",
        "proposal_id",
        "status",
        "memory_id",
        "expected_revision",
        "memory_type",
        "project",
        "source_count",
        "exposure_count",
        "positive_signal_count",
        "negative_signal_count",
        "recommended_surface",
        "reason",
        "proposal_digest",
    }
)
_REVISION = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN_PATH = re.compile(r"(?i)(?:candidate|proposal|_raw-sessions)")


def refresh_promotion_proposals(vault, config, *, apply=True):
    """Read current runtime evidence and refresh isolated promotion proposals."""
    vault = os.path.abspath(os.path.expanduser(os.fspath(vault)))
    settings = _settings(config)
    if not settings["enabled"]:
        return {"proposals": 0, "written": 0, "paths": []}
    runtime = config.get("memory_runtime") if isinstance(config, Mapping) else {}
    effectiveness = (
        config.get("memory_effectiveness") if isinstance(config, Mapping) else {}
    )
    runtime = runtime if isinstance(runtime, Mapping) else {}
    effectiveness = effectiveness if isinstance(effectiveness, Mapping) else {}
    index_path = safe_vault_path(
        vault,
        runtime.get("index_path", "05-Agent-Memory/recall-index.json"),
    )
    if not os.path.exists(index_path):
        return {"proposals": 0, "written": 0, "paths": []}
    if os.path.islink(index_path) or not os.path.isfile(index_path):
        raise ValueError("promotion recall index must be a regular file")
    try:
        index = json.loads(Path(index_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("promotion recall index is unreadable") from exc
    event_path = safe_vault_path(
        vault,
        effectiveness.get(
            "event_log_path",
            "04-Feedback/_logs/memory-effectiveness.jsonl",
        ),
    )
    aggregate = aggregate_events(read_effectiveness_events(event_path))
    proposals = scan_promotion_opportunities(index, aggregate, config)
    raw_promotion = config.get("memory_promotion") if isinstance(config, Mapping) else {}
    raw_promotion = raw_promotion if isinstance(raw_promotion, Mapping) else {}
    return write_proposals(
        vault,
        proposals,
        apply=apply,
        proposal_dir=raw_promotion.get("proposal_dir", DEFAULT_PROPOSAL_DIR),
    )


def scan_promotion_opportunities(index, effectiveness, config):
    """Return deterministic candidate proposals without writing or mutating input."""
    settings = _settings(config)
    if not settings["enabled"]:
        return []
    units = index.get("units") if isinstance(index, Mapping) else None
    if not isinstance(units, list):
        return []
    aggregates = effectiveness.get("memories") if isinstance(effectiveness, Mapping) else {}
    if not isinstance(aggregates, Mapping):
        aggregates = {}

    proposals = []
    for unit in units:
        if not _eligible_unit(unit):
            continue
        revision = str(unit.get("revision") or "")
        evidence = aggregates.get(f"{unit['id']}@{revision}") or {}
        if not isinstance(evidence, Mapping):
            evidence = {}
        if (
            evidence
            and (
                evidence.get("id") != unit["id"]
                or evidence.get("revision") != revision
            )
        ):
            evidence = {}

        source_count = _independent_source_count(unit.get("source_refs"))
        exposure_count = _bounded_count(evidence.get("exposures"))
        positive = _bounded_count(evidence.get("accepted")) + _bounded_count(
            evidence.get("manual_helpful")
        )
        negative = _bounded_count(evidence.get("corrected")) + _bounded_count(
            evidence.get("manual_misleading")
        )
        if negative:
            continue
        source_eligible = source_count >= settings["min_source_count"]
        effect_eligible = (
            exposure_count >= settings["min_exposure_count"] and positive > 0
        )
        if not source_eligible and not effect_eligible:
            continue
        proposals.append(
            _build_proposal(
                unit,
                source_count=source_count,
                exposure_count=exposure_count,
                positive=positive,
                negative=negative,
                source_eligible=source_eligible,
                effect_eligible=effect_eligible,
            )
        )

    proposals.sort(
        key=lambda item: (
            -item["negative_signal_count"],
            -item["positive_signal_count"],
            -item["source_count"],
            -item["exposure_count"],
            item["memory_id"],
            item["expected_revision"],
        )
    )
    return proposals[: settings["max_proposals_per_run"]]


def write_proposals(
    vault,
    proposals,
    *,
    apply=False,
    proposal_dir=DEFAULT_PROPOSAL_DIR,
):
    """Preview or write candidate files; never edit formal memory or source code."""
    vault = os.path.abspath(os.path.expanduser(os.fspath(vault)))
    validated = [_validate_proposal(item) for item in proposals or []]
    paths = [
        safe_vault_path(
            vault,
            proposal_dir,
            safe_filename(
                f"{item['proposal_id']}-{item['proposal_digest'][:12]}",
                default="memory-promotion-proposal",
                max_length=140,
            )
            + ".md",
        )
        for item in validated
    ]
    if not apply:
        return {"proposals": len(validated), "written": 0, "paths": paths}

    destination = safe_vault_path(vault, proposal_dir)
    ensure_directory_tree(destination, vault)
    written = 0
    for proposal, path in zip(validated, paths):
        content = _render_proposal(proposal)
        try:
            existing = Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            existing = None
        if existing == content:
            continue
        if existing is not None:
            raise ValueError("promotion proposal path already contains different content")
        durable_atomic_write(path, content, root=vault)
        written += 1
    return {"proposals": len(validated), "written": written, "paths": paths}


def _settings(config):
    raw = config.get("memory_promotion") if isinstance(config, Mapping) else None
    if not isinstance(raw, Mapping):
        raw = config if isinstance(config, Mapping) else {}
    values = {
        "enabled": raw.get("enabled", True),
        "min_source_count": raw.get("min_source_count", 3),
        "min_exposure_count": raw.get("min_exposure_count", 2),
        "max_proposals_per_run": raw.get("max_proposals_per_run", 10),
    }
    if not isinstance(values["enabled"], bool):
        raise ValueError("memory_promotion.enabled must be a boolean")
    for key in ("min_source_count", "min_exposure_count", "max_proposals_per_run"):
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"memory_promotion.{key} must be a positive integer")
    return values


def _eligible_unit(unit):
    if not isinstance(unit, Mapping):
        return False
    if (
        unit.get("type") not in ELIGIBLE_TYPES
        or unit.get("status", "active") != "active"
        or not is_valid_memory_id(unit.get("id"))
        or not _REVISION.fullmatch(str(unit.get("revision") or ""))
        or _FORBIDDEN_PATH.search(
            " ".join(
                [
                    str(unit.get("path") or ""),
                    str(unit.get("source_note") or ""),
                    *(str(item) for item in unit.get("source_refs") or []),
                ]
            )
        )
    ):
        return False
    try:
        authority = normalize_authority_metadata(unit)
    except ValueError:
        return False
    return not authority.get("enforced_by")


def _independent_source_count(source_refs):
    if not isinstance(source_refs, list):
        return 0
    refs = {
        str(item).strip()
        for item in source_refs
        if str(item or "").strip()
        and not str(item).startswith(("note:", "candidate:", "memory:"))
        and not _FORBIDDEN_PATH.search(str(item))
    }
    return len(refs)


def _bounded_count(value):
    if isinstance(value, bool):
        return 0
    try:
        return min(1_000_000, max(0, int(value or 0)))
    except (TypeError, ValueError):
        return 0


def _build_proposal(
    unit,
    *,
    source_count,
    exposure_count,
    positive,
    negative,
    source_eligible,
    effect_eligible,
):
    slug = _surface_slug(unit["id"])
    surfaces = {
        "decision": "repo:AGENTS.md#memory-" + slug,
        "error": "test:tests/regressions/test_" + slug + ".py",
        "workflow": "runbook:docs/workflows/" + slug + ".md",
    }
    evidence_parts = []
    if source_eligible:
        evidence_parts.append(f"{source_count} 份独立来源")
    if effect_eligible:
        evidence_parts.append(
            f"{exposure_count} 次召回和 {positive} 次正向信号"
        )
    proposal = {
        "schema_version": PROMOTION_SCHEMA_VERSION,
        "proposal_id": f"promotion:{unit['id']}:{unit['revision'][:12]}",
        "status": "candidate",
        "memory_id": unit["id"],
        "expected_revision": unit["revision"],
        "memory_type": unit["type"],
        "project": str(unit.get("project") or ""),
        "source_count": source_count,
        "exposure_count": exposure_count,
        "positive_signal_count": positive,
        "negative_signal_count": negative,
        "recommended_surface": surfaces[unit["type"]],
        "reason": "该记忆已有" + "，".join(evidence_parts) + "，建议评估更强执行面。",
        "proposal_digest": "",
    }
    proposal["proposal_digest"] = _proposal_digest(proposal)
    return proposal


def _proposal_digest(proposal):
    payload = dict(proposal)
    payload["proposal_digest"] = ""
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _validate_proposal(proposal):
    if not isinstance(proposal, Mapping) or set(proposal) != PROPOSAL_FIELDS:
        raise ValueError("promotion proposal has an invalid shape")
    item = dict(proposal)
    if (
        item.get("schema_version") != PROMOTION_SCHEMA_VERSION
        or item.get("status") != "candidate"
        or item.get("memory_type") not in ELIGIBLE_TYPES
        or not is_valid_memory_id(item.get("memory_id"))
        or not _REVISION.fullmatch(str(item.get("expected_revision") or ""))
        or item.get("proposal_digest") != _proposal_digest(item)
    ):
        raise ValueError("promotion proposal identity or digest is invalid")
    normalize_authority_metadata(
        {
            "authority_role": "canonical",
            "authority_owner": "promotion proposal",
            "canonical_source": item.get("recommended_surface"),
        }
    )
    return item


def _render_proposal(proposal):
    frontmatter = {
        "schema_version": proposal["schema_version"],
        "type": "memory-promotion-proposal",
        **{key: proposal[key] for key in proposal if key != "schema_version"},
    }
    body = [
        "# Memory Promotion Proposal",
        "",
        "> 这是隔离的候选建议，不会自动修改正式记忆、代码、规则或测试。",
        "",
        f"- Memory: `{proposal['memory_id']}`",
        f"- Expected revision: `{proposal['expected_revision']}`",
        f"- Recommended surface: `{proposal['recommended_surface']}`",
        f"- Reason: {proposal['reason']}",
        "",
    ]
    return (
        "---\n"
        + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + "\n".join(body)
    )


def _surface_slug(memory_id):
    slug = re.sub(r"[^a-z0-9]+", "-", str(memory_id).casefold()).strip("-")
    return slug[:80] or "memory"
