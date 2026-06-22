"""Transcript discovery and parsing for Claude Code and Codex."""
import json
import os
from datetime import datetime


def expand_path(path):
    if not path:
        return ""
    return os.path.expandvars(os.path.expanduser(str(path)))


def get_agent_type(cfg):
    return str(cfg.get("agent", "codex")).lower()


def get_transcript_roots(cfg):
    """Return configured transcript search roots, preserving order."""
    roots = []

    for key in ("transcript_paths", "session_paths"):
        for path in cfg.get(key, []) or []:
            roots.append(path)

    agent = get_agent_type(cfg)

    if agent == "codex":
        if cfg.get("codex_sessions_path"):
            roots.append(cfg["codex_sessions_path"])
        codex_home = expand_path(cfg.get("codex_home") or os.path.join("~", ".codex"))
        roots.append(os.path.join(codex_home, "sessions"))

    if cfg.get("claude_project_path"):
        roots.append(cfg["claude_project_path"])

    if agent == "claude":
        roots.extend([
            os.path.join("~", ".claude", "transcripts"),
            os.path.join("~", ".claude", "projects"),
        ])

    seen = set()
    expanded = []
    for root in roots:
        root = expand_path(root)
        if root and root not in seen:
            seen.add(root)
            expanded.append(root)
    return expanded


def iter_transcript_files(roots, max_depth=6):
    for root in roots:
        root = expand_path(root)
        if not root or not os.path.exists(root):
            continue
        if os.path.isfile(root) and root.endswith(".jsonl"):
            yield root
            continue
        if not os.path.isdir(root):
            continue
        for current, dirs, files in os.walk(root):
            depth = os.path.relpath(current, root).count(os.sep)
            if depth > max_depth:
                dirs[:] = []
                continue
            for filename in files:
                if filename.endswith(".jsonl") and not filename.startswith("agent-"):
                    yield os.path.join(current, filename)


def find_recent_transcripts(cfg, processed_ids=None, hours=48):
    processed_ids = processed_ids or set()
    cutoff = datetime.now().timestamp() - (hours * 3600)
    candidates = []

    for path in iter_transcript_files(get_transcript_roots(cfg)):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        session_id = session_id_from_path(path)
        if session_id in processed_ids:
            continue
        candidates.append((mtime, path))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in candidates]


def find_latest_transcript(cfg, hours=24):
    candidates = find_recent_transcripts(cfg, processed_ids=set(), hours=hours)
    return candidates[0] if candidates else None


def session_id_from_path(path):
    filename = os.path.basename(path)
    if filename.endswith(".jsonl"):
        filename = filename[:-6]
    if filename.startswith("rollout-") and "-" in filename:
        parts = filename.split("-")
        if len(parts) >= 7:
            return "-".join(parts[-5:])
    return filename


def parse_transcript(path):
    """Parse a Claude/Codex JSONL transcript into {text, meta, messages}."""
    if not path or not os.path.exists(path):
        return {"text": "", "meta": {}, "messages": []}

    if os.path.isdir(path):
        files = sorted(
            [f for f in os.listdir(path) if f.endswith(".jsonl")],
            key=lambda f: os.path.getmtime(os.path.join(path, f)),
            reverse=True,
        )
        if not files:
            return {"text": "", "meta": {}, "messages": []}
        path = os.path.join(path, files[0])

    if not path.endswith(".jsonl"):
        return {"text": "", "meta": {}, "messages": []}

    messages = []
    seen_messages = set()
    meta = {"session_id": session_id_from_path(path)}

    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except (IOError, UnicodeDecodeError):
        return {"text": "", "meta": meta, "messages": []}

    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue

        timestamp = record.get("timestamp")
        if timestamp and not meta.get("timestamp"):
            meta["timestamp"] = timestamp
            meta["date"] = timestamp[:10]

        if record.get("sessionId") and not meta.get("session_id"):
            meta["session_id"] = record["sessionId"]
        if record.get("cwd") and not meta.get("cwd"):
            meta["cwd"] = record["cwd"]
        if record.get("type") in ("user", "assistant") and not meta.get("agent"):
            meta["agent"] = "claude"

        record_type = record.get("type")
        payload = record.get("payload") or {}

        if record_type == "session_meta" and isinstance(payload, dict):
            meta.update({
                "session_id": payload.get("id") or meta.get("session_id"),
                "cwd": payload.get("cwd") or meta.get("cwd"),
                "agent": "codex",
                "source": payload.get("source") or meta.get("source"),
            })
            if payload.get("timestamp"):
                meta.setdefault("timestamp", payload["timestamp"])
                meta.setdefault("date", payload["timestamp"][:10])
            continue

        for role, text in _extract_record_messages(record):
            if text:
                normalized_role = _canonical_role(role)
                key = (normalized_role, _normalize_message_text(text))
                if key in seen_messages:
                    continue
                seen_messages.add(key)
                messages.append({"role": normalized_role, "text": text})

    parts = []
    if meta.get("cwd"):
        parts.append(f"[cwd: {meta['cwd']}]")
    parts.extend(m["text"] for m in messages)
    return {"text": "\n".join(parts), "meta": meta, "messages": messages}


def _extract_record_messages(record):
    """Yield (role, text) pairs from Claude or Codex record shapes."""
    message = record.get("message")
    if isinstance(message, dict):
        role = message.get("role") or record.get("type") or "message"
        text = _content_to_text(message.get("content", ""))
        if text:
            yield role, text
        return

    payload = record.get("payload") or {}
    if not isinstance(payload, dict):
        return

    if record.get("type") == "response_item":
        if payload.get("type") != "message":
            return
        role = payload.get("role") or "message"
        text = _content_to_text(payload.get("content", ""))
        if text:
            yield role, text
        return

    if record.get("type") == "event_msg":
        if payload.get("type") not in ("user_message", "agent_message"):
            return
        message_text = payload.get("message")
        if message_text:
            yield payload.get("type", "event"), str(message_text)


def _canonical_role(role):
    if role in ("agent_message", "assistant"):
        return "assistant"
    if role in ("user_message", "user"):
        return "user"
    return role or "message"


def _normalize_message_text(text):
    return "\n".join(str(text).strip().split())


def _content_to_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "input_text", "output_text"):
                    if item.get(key):
                        parts.append(str(item[key]))
                        break
        return "\n".join(parts)
    if isinstance(content, dict):
        for key in ("text", "input_text", "output_text"):
            if content.get(key):
                return str(content[key])
    return ""
