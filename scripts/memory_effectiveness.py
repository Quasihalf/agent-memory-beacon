#!/usr/bin/env python3
"""Privacy-safe effectiveness events and Obsidian reports for memory recall."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping

from memory_schema import RUNTIME_MEMORY_TYPES, is_valid_memory_id
from safety import safe_vault_path


EFFECTIVENESS_SCHEMA_VERSION = "1.0"
EFFECTIVENESS_EVENT_ALLOWED_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "event_kind",
        "timestamp",
        "session_hash",
        "parent_event_id",
        "trigger",
        "outcome",
        "confidence",
        "signal_source",
        "duration_ms",
        "estimated_tokens",
        "memories",
    }
)
EVENT_KINDS = frozenset({"exposure", "feedback", "manual"})
EVENT_OUTCOMES = frozenset(
    {
        "exposed",
        "accepted",
        "corrected",
        "unobserved",
        "helpful",
        "misleading",
    }
)
_SESSION_HASH = re.compile(r"[A-Za-z0-9_-]{16,64}")
_REVISION = re.compile(r"[0-9a-f]{64}")
_EVENT_ID = re.compile(r"[0-9a-f]{64}")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_POSITIVE = re.compile(
    r"^(?:对(?:的)?|没错|正确|这样(?:就)?对了|可以|好(?:的)?|行|ok|yes)"
    r"(?:[，,。.!！\s]*(?:继续|就这样|按这个|开始)?)?$",
    re.IGNORECASE,
)
_CORRECTION = re.compile(
    r"^(?:不是|不对|错了|说错了|我说的是|我的意思是|不要这样|并不是)",
    re.IGNORECASE,
)


def build_exposure_event(
    *,
    timestamp,
    session_hash,
    trigger,
    memories,
    duration_ms=0,
    estimated_tokens=0,
):
    """Build one deterministic exposure event without memory or prompt bodies."""
    event = _base_event(
        timestamp=timestamp,
        session_hash=session_hash,
        event_kind="exposure",
        parent_event_id="",
        trigger=trigger,
        outcome="exposed",
        confidence=1.0,
        signal_source="runtime",
        duration_ms=duration_ms,
        estimated_tokens=estimated_tokens,
        memories=memories,
    )
    return _bind_event_id(event)


def classify_feedback(prompt):
    """Return a conservative weak signal from one bounded user response."""
    text = re.sub(r"\s+", " ", str(prompt or "")).strip()
    if not text or len(text) > 240 or _CONTROL.search(text):
        return "unobserved", 0.0
    if _POSITIVE.fullmatch(text):
        return "accepted", 0.6
    if _CORRECTION.search(text):
        return "corrected", 0.7
    return "unobserved", 0.0


def build_feedback_event(exposure, prompt, *, timestamp):
    """Close one exact prior exposure with weak, body-free feedback evidence."""
    if not _valid_event(exposure) or exposure.get("event_kind") != "exposure":
        raise ValueError("feedback requires a valid exposure event")
    outcome, confidence = classify_feedback(prompt)
    signal_source = "user-explicit-weak" if confidence else "runtime-unobserved"
    event = _base_event(
        timestamp=timestamp,
        session_hash=exposure["session_hash"],
        event_kind="feedback",
        parent_event_id=exposure["event_id"],
        trigger=exposure["trigger"],
        outcome=outcome,
        confidence=confidence,
        signal_source=signal_source,
        duration_ms=0,
        estimated_tokens=0,
        memories=exposure["memories"],
    )
    return _bind_event_id(event)


def build_manual_event(
    *,
    timestamp,
    session_hash,
    memories,
    outcome,
    parent_event_id="",
):
    """Build an explicit human-confirmed helpful or misleading event."""
    if outcome not in {"helpful", "misleading"}:
        raise ValueError("manual outcome must be helpful or misleading")
    event = _base_event(
        timestamp=timestamp,
        session_hash=session_hash,
        event_kind="manual",
        parent_event_id=parent_event_id,
        trigger="manual",
        outcome=outcome,
        confidence=1.0,
        signal_source="user-explicit",
        duration_ms=0,
        estimated_tokens=0,
        memories=memories,
    )
    return _bind_event_id(event)


def aggregate_events(events: Iterable[Mapping[str, object]]):
    """Aggregate unique valid events without inventing a utility verdict."""
    unique = {}
    for event in events or []:
        if _valid_event(event):
            unique.setdefault(event["event_id"], dict(event))

    memories = {}
    exposure_count = 0
    estimated_tokens = 0
    for event in sorted(unique.values(), key=lambda item: (item["timestamp"], item["event_id"])):
        if event["event_kind"] == "exposure":
            exposure_count += 1
            estimated_tokens += event["estimated_tokens"]
        for memory in event["memories"]:
            key = f"{memory['id']}@{memory['revision']}"
            item = memories.setdefault(
                key,
                {
                    "id": memory["id"],
                    "revision": memory["revision"],
                    "type": memory["type"],
                    "exposures": 0,
                    "accepted": 0,
                    "corrected": 0,
                    "unobserved": 0,
                    "manual_helpful": 0,
                    "manual_misleading": 0,
                    "last_seen": "",
                    "channels": [],
                },
            )
            item["last_seen"] = max(item["last_seen"], event["timestamp"])
            item["channels"] = sorted(
                set(item["channels"]) | set(memory.get("channels") or [])
            )
            if event["event_kind"] == "exposure":
                item["exposures"] += 1
            elif event["outcome"] == "accepted":
                item["accepted"] += 1
            elif event["outcome"] == "corrected":
                item["corrected"] += 1
            elif event["outcome"] == "unobserved":
                item["unobserved"] += 1
            elif event["outcome"] == "helpful":
                item["manual_helpful"] += 1
            elif event["outcome"] == "misleading":
                item["manual_misleading"] += 1

    return {
        "schema_version": EFFECTIVENESS_SCHEMA_VERSION,
        "event_count": len(unique),
        "exposure_count": exposure_count,
        "estimated_tokens": estimated_tokens,
        "memories": memories,
    }


def read_effectiveness_events(path):
    """Read unique valid events from one JSONL file, ignoring corrupt lines."""
    source = Path(path)
    if not source.exists():
        return []
    events = {}
    with source.open("r", encoding="utf-8") as handle:
        for line in handle:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if _valid_event(event):
                events.setdefault(event["event_id"], event)
    return list(events.values())


def is_valid_effectiveness_event(event, *, event_kind=None):
    """Validate a persisted event, optionally requiring one exact kind."""
    if not _valid_event(event):
        return False
    return event_kind is None or event.get("event_kind") == event_kind


def render_effectiveness_report(aggregate, *, max_items=100):
    """Render an Obsidian report containing IDs and counts, never memory bodies."""
    max_items = max(1, int(max_items))
    items = sorted(
        (aggregate.get("memories") or {}).values(),
        key=lambda item: (
            item.get("manual_misleading", 0),
            item.get("corrected", 0),
            item.get("exposures", 0),
            item.get("last_seen", ""),
            item.get("id", ""),
        ),
        reverse=True,
    )[:max_items]
    lines = [
        "---",
        "title: Memory Effectiveness",
        "type: memory-effectiveness",
        f"schema_version: '{EFFECTIVENESS_SCHEMA_VERSION}'",
        "---",
        "",
        "# Memory Effectiveness",
        "",
        "> 自动反馈属于弱证据，只用于观察和生成建议，不能自动修改正式记忆。",
        "",
        "## Summary",
        "",
        f"- Events: {int(aggregate.get('event_count') or 0)}",
        f"- Exposures: {int(aggregate.get('exposure_count') or 0)}",
        f"- Estimated injected tokens: {int(aggregate.get('estimated_tokens') or 0)}",
        "",
        "## Memory Revisions",
        "",
        "| Memory | Revision | Type | Exposed | Accepted* | Corrected* | Helpful | Misleading | Last Seen |",
        "|---|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for item in items:
        lines.append(
            "| `{id}` | `{revision}` | {type} | {exposures} | {accepted} | "
            "{corrected} | {manual_helpful} | {manual_misleading} | {last_seen} |".format(
                id=item["id"],
                revision=item["revision"][:12],
                type=item["type"],
                exposures=item["exposures"],
                accepted=item["accepted"],
                corrected=item["corrected"],
                manual_helpful=item["manual_helpful"],
                manual_misleading=item["manual_misleading"],
                last_seen=item["last_seen"],
            )
        )
    if not items:
        lines.append("| - | - | - | 0 | 0 | 0 | 0 | 0 | - |")
    lines.extend(
        [
            "",
            "`Accepted*` 和 `Corrected*` 来自紧邻召回后的短确认或明确纠正，属于弱信号。",
            "",
        ]
    )
    return "\n".join(lines)


def write_effectiveness_report(vault, config):
    """Read configured events and atomically refresh the visible report."""
    settings = dict((config or {}).get("memory_effectiveness") or {})
    if not settings.get("enabled", True):
        return {"path": "", "event_count": 0, "written": False}
    vault = os.path.abspath(os.path.expanduser(os.fspath(vault)))
    event_path = safe_vault_path(
        vault,
        settings.get("event_log_path", "04-Feedback/_logs/memory-effectiveness.jsonl"),
    )
    report_path = safe_vault_path(
        vault,
        settings.get("report_path", "04-Feedback/memory-effectiveness.md"),
    )
    aggregate = aggregate_events(read_effectiveness_events(event_path))
    report = render_effectiveness_report(
        aggregate,
        max_items=settings.get("max_report_items", 100),
    )
    _atomic_write(report_path, report)
    return {
        "path": report_path,
        "event_count": aggregate["event_count"],
        "memory_count": len(aggregate["memories"]),
        "written": True,
    }


def _base_event(
    *,
    timestamp,
    session_hash,
    event_kind,
    parent_event_id,
    trigger,
    outcome,
    confidence,
    signal_source,
    duration_ms,
    estimated_tokens,
    memories,
):
    timestamp = _normalize_timestamp(timestamp)
    session_hash = str(session_hash or "").strip()
    if not _SESSION_HASH.fullmatch(session_hash):
        raise ValueError("session_hash must be a bounded opaque identifier")
    if event_kind not in EVENT_KINDS or outcome not in EVENT_OUTCOMES:
        raise ValueError("invalid effectiveness event kind or outcome")
    parent_event_id = str(parent_event_id or "").strip()
    if parent_event_id and not _EVENT_ID.fullmatch(parent_event_id):
        raise ValueError("parent_event_id must be a SHA-256 digest")
    trigger = _safe_scalar(trigger, 64)
    signal_source = _safe_scalar(signal_source, 64)
    confidence = float(confidence)
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    duration_ms = float(duration_ms or 0)
    if not math.isfinite(duration_ms):
        raise ValueError("duration_ms must be finite")
    duration_ms = round(max(0.0, duration_ms), 3)
    estimated_tokens = max(0, int(estimated_tokens or 0))
    normalized_memories = _normalize_memories(memories)
    if not normalized_memories:
        raise ValueError("effectiveness event requires at least one memory")
    return {
        "schema_version": EFFECTIVENESS_SCHEMA_VERSION,
        "event_id": "",
        "event_kind": event_kind,
        "timestamp": timestamp,
        "session_hash": session_hash,
        "parent_event_id": parent_event_id,
        "trigger": trigger,
        "outcome": outcome,
        "confidence": confidence,
        "signal_source": signal_source,
        "duration_ms": duration_ms,
        "estimated_tokens": estimated_tokens,
        "memories": normalized_memories,
    }


def _normalize_memories(memories):
    normalized = {}
    for item in memories or []:
        if not isinstance(item, Mapping):
            continue
        memory_id = str(item.get("id") or "").strip()
        revision = str(item.get("revision") or "").strip()
        memory_type = str(item.get("type") or "").strip()
        if (
            not is_valid_memory_id(memory_id)
            or not _REVISION.fullmatch(revision)
            or memory_type not in RUNTIME_MEMORY_TYPES
        ):
            raise ValueError("effectiveness memory identity is invalid")
        channels = sorted(
            {
                _safe_scalar(channel, 32)
                for channel in item.get("retrieval_channels") or item.get("channels") or []
                if str(channel or "").strip()
            }
        )[:8]
        normalized[(memory_id, revision)] = {
            "id": memory_id,
            "revision": revision,
            "type": memory_type,
            "channels": channels,
        }
    return [normalized[key] for key in sorted(normalized)]


def _bind_event_id(event):
    payload = dict(event)
    payload["event_id"] = ""
    event_id = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    result = dict(event)
    result["event_id"] = event_id
    return result


def _valid_event(event):
    if not isinstance(event, Mapping) or set(event) != EFFECTIVENESS_EVENT_ALLOWED_FIELDS:
        return False
    try:
        rebuilt = _base_event(
            timestamp=event["timestamp"],
            session_hash=event["session_hash"],
            event_kind=event["event_kind"],
            parent_event_id=event["parent_event_id"],
            trigger=event["trigger"],
            outcome=event["outcome"],
            confidence=event["confidence"],
            signal_source=event["signal_source"],
            duration_ms=event["duration_ms"],
            estimated_tokens=event["estimated_tokens"],
            memories=event["memories"],
        )
    except (KeyError, TypeError, ValueError):
        return False
    if event.get("schema_version") != EFFECTIVENESS_SCHEMA_VERSION:
        return False
    return event.get("event_id") == _bind_event_id(rebuilt)["event_id"]


def _normalize_timestamp(value):
    raw = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.isoformat()


def _safe_scalar(value, limit):
    text = str(value or "").strip()
    if len(text) > limit or _CONTROL.search(text):
        raise ValueError("unsafe effectiveness scalar")
    return text


def _atomic_write(path, content):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def main():
    parser = argparse.ArgumentParser(description="Render memory effectiveness report")
    parser.add_argument("--vault", default="", help="Obsidian Vault path")
    args = parser.parse_args()
    from config import load_config

    cfg = load_config()
    vault = args.vault or cfg.get("vault_path")
    if not vault:
        parser.error("--vault or configured vault_path is required")
    result = write_effectiveness_report(vault, cfg)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
