"""Transcript discovery and parsing for Claude Code and Codex."""
import json
import os
import sqlite3
from datetime import datetime, timezone


ZCODE_LOCATOR_SEP = "::"
SQLITE_EXTENSIONS = (".sqlite", ".sqlite3", ".db", ".db3")


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

    if agent == "zcode":
        if cfg.get("zcode_db_path"):
            roots.append(cfg["zcode_db_path"])
        zcode_home = expand_path(cfg.get("zcode_home") or os.path.join("~", ".zcode"))
        roots.append(os.path.join(zcode_home, "cli", "db", "db.sqlite"))

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
        if os.path.isfile(root) and _is_sqlite_path(root):
            yield from _iter_zcode_sqlite_sessions(root)
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
                path = os.path.join(current, filename)
                if filename.endswith(".jsonl") and not filename.startswith("agent-"):
                    yield path
                elif _is_sqlite_path(filename):
                    yield from _iter_zcode_sqlite_sessions(path)


def transcript_mtime(path):
    db_path, session_id = split_zcode_locator(path)
    if db_path and session_id:
        updated = _zcode_session_time_updated(db_path, session_id)
        if updated:
            return updated / 1000
        return os.path.getmtime(db_path)
    return os.path.getmtime(path)


def _iter_zcode_sqlite_sessions(db_path):
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        rows = conn.execute(
            "select id from session order by time_updated desc, id desc"
        ).fetchall()
        conn.close()
    except sqlite3.Error:
        return
    for (session_id,) in rows:
        yield make_zcode_locator(db_path, session_id)


def make_zcode_locator(db_path, session_id):
    return f"{db_path}{ZCODE_LOCATOR_SEP}{session_id}"


def split_zcode_locator(path):
    text = str(path or "")
    if ZCODE_LOCATOR_SEP not in text:
        return None, None
    db_path, session_id = text.rsplit(ZCODE_LOCATOR_SEP, 1)
    if not _is_sqlite_path(db_path) or not session_id:
        return None, None
    return db_path, session_id


def _is_sqlite_path(path):
    return str(path).lower().endswith(SQLITE_EXTENSIONS)


def _zcode_session_time_updated(db_path, session_id):
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            "select time_updated from session where id = ?",
            (session_id,),
        ).fetchone()
        conn.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def find_recent_transcripts(cfg, processed_ids=None, hours=48):
    processed_ids = processed_ids or set()
    cutoff = datetime.now().timestamp() - (hours * 3600)
    candidates = []

    for path in iter_transcript_files(get_transcript_roots(cfg)):
        try:
            mtime = transcript_mtime(path)
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
    _db_path, zcode_session_id = split_zcode_locator(path)
    if zcode_session_id:
        return zcode_session_id

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
    db_path, zcode_session_id = split_zcode_locator(path)
    if db_path:
        return parse_zcode_sqlite_transcript(db_path, zcode_session_id)

    if path and _is_sqlite_path(path) and os.path.exists(path):
        return parse_zcode_sqlite_transcript(path, None)

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


def parse_zcode_sqlite_transcript(db_path, session_id=None):
    """Parse one ZCode SQLite session into the common transcript shape."""
    meta = {"agent": "zcode"}
    messages = []
    seen_messages = set()

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        if not session_id:
            row = conn.execute(
                "select id from session order by time_updated desc, id desc limit 1"
            ).fetchone()
            session_id = row["id"] if row else None
        if not session_id:
            conn.close()
            return {"text": "", "meta": meta, "messages": []}

        session = conn.execute(
            "select id, directory, title, time_created, time_updated from session where id = ?",
            (session_id,),
        ).fetchone()
        if not session:
            conn.close()
            return {"text": "", "meta": meta, "messages": []}

        meta.update({
            "session_id": session["id"],
            "cwd": session["directory"],
            "title": session["title"],
        })
        timestamp = _ms_to_iso_timestamp(session["time_created"])
        if timestamp:
            meta["timestamp"] = timestamp
            meta["date"] = timestamp[:10]

        message_rows = conn.execute(
            "select id, data from message where session_id = ? order by time_created, id",
            (session_id,),
        ).fetchall()
        for message_row in message_rows:
            message_data = _json_loads(message_row["data"])
            role = _canonical_role(message_data.get("role") or "message")
            parts = conn.execute(
                "select data from part where message_id = ? order by time_created, id",
                (message_row["id"],),
            ).fetchall()
            text_parts = []
            for part_row in parts:
                part_data = _json_loads(part_row["data"])
                if part_data.get("type") != "text":
                    continue
                text = _content_to_text(part_data.get("text", ""))
                if text:
                    text_parts.append(text)
            text = "\n".join(text_parts).strip()
            if not text:
                continue
            key = (role, _normalize_message_text(text))
            if key in seen_messages:
                continue
            seen_messages.add(key)
            messages.append({"role": role, "text": text})
        conn.close()
    except sqlite3.Error:
        return {"text": "", "meta": meta, "messages": []}

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

    if record.get("type") == "model_io":
        response = record.get("response") or {}
        if isinstance(response, dict) and response.get("text"):
            yield "assistant", str(response["text"])


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


def _json_loads(text):
    try:
        data = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _ms_to_iso_timestamp(value):
    try:
        seconds = int(value) / 1000
    except (TypeError, ValueError):
        return ""
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")
