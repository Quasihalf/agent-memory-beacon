"""Transcript discovery and parsing for Claude Code and Codex."""
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone

from safety import normalize_iso_date, redact_sensitive


ZCODE_LOCATOR_SEP = "::"
SQLITE_EXTENSIONS = (".sqlite", ".sqlite3", ".db", ".db3")
FILE_CURSOR_PREFIX = "file-bytes:"
ZCODE_CURSOR_PREFIX = "zcode-messages:"
MAX_JSONL_RECORD_BYTES = 65_536
JSONL_DRAIN_CHUNK_BYTES = 8_192
JSONL_CONTEXT_LOOKBACK_BYTES = 4 * 1024 * 1024
MAX_TRANSCRIPT_METADATA_BYTES = 1024 * 1024
MAX_CONTEXT_MESSAGES = 4
MAX_CONTEXT_MESSAGE_CHARS = 8_000


class TranscriptReadError(OSError):
    """A transcript source could not be read reliably."""


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

    configured_agents = cfg.get("transcript_agents")
    if configured_agents is None:
        agents = {get_agent_type(cfg)}
    else:
        agents = {str(agent).lower() for agent in configured_agents}

    if "codex" in agents:
        if cfg.get("codex_sessions_path"):
            roots.append(cfg["codex_sessions_path"])
        codex_home = expand_path(cfg.get("codex_home") or os.path.join("~", ".codex"))
        roots.append(os.path.join(codex_home, "sessions"))

    if "zcode" in agents:
        if cfg.get("zcode_db_path"):
            roots.append(cfg["zcode_db_path"])
        zcode_home = expand_path(cfg.get("zcode_home") or os.path.join("~", ".zcode"))
        roots.append(os.path.join(zcode_home, "cli", "db", "db.sqlite"))

    if "claude" in agents and cfg.get("claude_project_path"):
        roots.append(cfg["claude_project_path"])

    if "claude" in agents:
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
    seen = set()

    def unseen(path):
        db_path, session_id = split_zcode_locator(path)
        if db_path and session_id:
            identity = make_zcode_locator(os.path.realpath(db_path), session_id)
        else:
            identity = os.path.realpath(path)
        if identity in seen:
            return False
        seen.add(identity)
        return True

    for root in roots:
        root = expand_path(root)
        if not root or not os.path.exists(root):
            continue
        if os.path.isfile(root) and _is_sqlite_path(root):
            for locator in _iter_zcode_sqlite_sessions(root):
                if unseen(locator):
                    yield locator
            continue
        if os.path.isfile(root) and root.endswith(".jsonl"):
            if unseen(root):
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
                    if unseen(path):
                        yield path
                elif _is_sqlite_path(filename):
                    for locator in _iter_zcode_sqlite_sessions(path):
                        if unseen(locator):
                            yield locator


def transcript_mtime(path):
    db_path, session_id = split_zcode_locator(path)
    if db_path and session_id:
        updated = _zcode_session_time_updated(db_path, session_id)
        if updated:
            return updated / 1000
        return os.path.getmtime(db_path)
    return os.path.getmtime(path)


def transcript_version(path):
    """Return a cheap version token suitable for incremental harvesting."""
    return transcript_snapshot(path)[0]


def transcript_cursor(path):
    """Return a content-free high-water mark for adaptive memory learning."""
    return transcript_snapshot(path)[1]


def transcript_snapshot(path):
    """Capture a consistent version/cursor pair for one transcript source."""
    db_path, session_id = split_zcode_locator(path)
    if db_path and session_id:
        conn = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            conn.execute("begin")
            session = conn.execute(
                "select time_updated from session where id = ?",
                (session_id,),
            ).fetchone()
            if session is None or session[0] is None:
                raise TranscriptReadError(
                    f"ZCode session not found while capturing snapshot: {session_id}"
                )
            count = conn.execute(
                "select count(*) from message where session_id = ?",
                (session_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise TranscriptReadError(
                f"cannot capture ZCode transcript snapshot: {db_path}"
            ) from exc
        finally:
            if conn is not None:
                conn.close()
        updated_seconds = float(session[0]) / 1000
        message_count = int(count[0]) if count else 0
        return (
            f"zcode:{session_id}:{updated_seconds:.6f}",
            f"{ZCODE_CURSOR_PREFIX}{message_count}",
        )
    stat = os.stat(path)
    return (
        f"file:{stat.st_size}:{stat.st_mtime_ns}",
        f"{FILE_CURSOR_PREFIX}{stat.st_size}",
    )


def parse_transcript_since(path, cursor, end_cursor=None):
    """Parse only messages appended after a previously persisted cursor."""
    if not cursor:
        return parse_transcript(path)

    start_kind, start_value = _parse_cursor(cursor)
    end_kind, end_value = _parse_cursor(end_cursor or transcript_cursor(path))
    if not start_kind or start_kind != end_kind or start_value >= end_value:
        return {"text": "", "meta": {}, "messages": [], "observations": []}

    db_path, session_id = split_zcode_locator(path)
    if start_kind == "zcode" and db_path and session_id:
        return parse_zcode_sqlite_transcript(
            db_path,
            session_id,
            message_start=start_value,
            message_end=end_value,
        )
    if start_kind == "file" and str(path).endswith(".jsonl"):
        return _parse_jsonl_byte_range(path, start_value, end_value)
    return {"text": "", "meta": {}, "messages": [], "observations": []}


def read_transcript_metadata(path):
    """Read bounded routing metadata without parsing the transcript body."""
    db_path, zcode_session_id = split_zcode_locator(path)
    if db_path:
        return parse_zcode_sqlite_transcript(
            db_path,
            zcode_session_id,
            message_start=0,
            message_end=0,
        )["meta"]

    if path and _is_sqlite_path(path) and os.path.exists(path):
        return parse_zcode_sqlite_transcript(
            path,
            None,
            message_start=0,
            message_end=0,
        )["meta"]

    meta = {
        "session_id": session_id_from_path(path),
        "is_subagent": False,
    }
    normalized_path = os.path.abspath(os.path.expanduser(str(path or "")))
    if f"{os.sep}.codex{os.sep}" in normalized_path:
        meta["agent"] = "codex"
    elif f"{os.sep}.claude{os.sep}" in normalized_path:
        meta["agent"] = "claude"

    if not path or not str(path).endswith(".jsonl"):
        return meta
    try:
        handle = open(path, "rb")
    except OSError as exc:
        raise TranscriptReadError(f"cannot read transcript metadata: {path}") from exc

    embedded_session_id_seen = False
    with handle:
        for _record_index, raw_line in _iter_bounded_jsonl_records(
            handle,
            MAX_TRANSCRIPT_METADATA_BYTES,
        ):
            if raw_line is None:
                continue
            try:
                record = json.loads(raw_line.decode("utf-8", errors="replace"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                continue
            if not isinstance(record, dict):
                continue
            embedded_session_id_seen = _apply_jsonl_record_metadata(
                record,
                meta,
                embedded_session_id_seen,
            )
            if record.get("type") == "session_meta" or (
                meta.get("agent") and meta.get("cwd") and meta.get("timestamp")
            ):
                break
    return meta


def _parse_cursor(cursor):
    text = str(cursor or "")
    for prefix, kind in (
        (FILE_CURSOR_PREFIX, "file"),
        (ZCODE_CURSOR_PREFIX, "zcode"),
    ):
        if not text.startswith(prefix):
            continue
        try:
            return kind, max(0, int(text[len(prefix):]))
        except ValueError:
            return None, 0
    return None, 0


def _parse_jsonl_byte_range(path, start, end):
    messages = []
    context_messages = []
    seen_messages = set()
    seen_context_messages = set()
    observation_collector = CodexObservationCollector()
    try:
        file_size = os.path.getsize(path)
        end = min(max(0, int(end)), file_size)
        start = min(max(0, int(start)), end)
        with open(path, "rb") as handle:
            range_start = _jsonl_record_boundary(handle, start)
            context_start = _jsonl_context_start(
                handle,
                range_start,
                JSONL_CONTEXT_LOOKBACK_BYTES,
            )
            handle.seek(context_start)
            for record_index, raw_line in _iter_bounded_jsonl_records(handle, end):
                if raw_line is None:
                    continue
                try:
                    record = json.loads(raw_line.decode("utf-8", errors="replace"))
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                    continue
                if not isinstance(record, dict):
                    continue
                emit = record_index >= range_start
                observation_collector.observe(record, record_index, emit=emit)
                for role, text in _extract_record_messages(record):
                    if not text:
                        continue
                    normalized_role = _canonical_role(role)
                    if not emit:
                        if normalized_role != "user":
                            continue
                        bounded = text[:MAX_CONTEXT_MESSAGE_CHARS]
                        context_key = _normalize_message_text(bounded)
                        if not context_key or context_key in seen_context_messages:
                            continue
                        seen_context_messages.add(context_key)
                        context_messages.append(
                            {"role": "user", "text": bounded}
                        )
                        context_messages = context_messages[-MAX_CONTEXT_MESSAGES:]
                        continue
                    key = (normalized_role, _normalize_message_text(text))
                    if key in seen_messages:
                        continue
                    seen_messages.add(key)
                    messages.append({"role": normalized_role, "text": text})
    except OSError as exc:
        raise TranscriptReadError(
            f"cannot read transcript byte range: {path}"
        ) from exc

    return {
        "text": "\n".join(message["text"] for message in messages),
        "meta": {},
        "messages": messages,
        "context_messages": context_messages,
        "observations": observation_collector.observations,
    }


def _jsonl_context_start(handle, range_start, lookback_bytes):
    """Start on a nearby complete record without rescanning all history."""
    lookback_bytes = max(0, int(lookback_bytes))
    if range_start <= lookback_bytes:
        return 0
    candidate = range_start - lookback_bytes
    handle.seek(candidate - 1)
    if handle.read(1) == b"\n":
        return candidate
    handle.seek(candidate)
    fragment = handle.readline(MAX_JSONL_RECORD_BYTES + 1)
    if fragment.endswith(b"\n") and handle.tell() <= range_start:
        return handle.tell()
    return range_start


def _jsonl_record_boundary(handle, offset):
    """Rewind an incomplete append cursor to the start of its JSONL record."""
    if offset <= 0:
        return 0
    handle.seek(offset - 1)
    if handle.read(1) == b"\n":
        return offset

    position = offset - 1
    while position > 0:
        chunk_start = max(0, position - 4096)
        handle.seek(chunk_start)
        chunk = handle.read(position - chunk_start)
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            return chunk_start + newline + 1
        position = chunk_start
    return 0


def _iter_bounded_jsonl_records(handle, end=None):
    """Yield bounded raw JSONL records while draining oversized lines in chunks."""
    while end is None or handle.tell() < end:
        record_start = handle.tell()
        remaining = None if end is None else end - record_start
        if remaining is not None and remaining <= 0:
            return
        read_limit = MAX_JSONL_RECORD_BYTES + 1
        if remaining is not None:
            read_limit = min(read_limit, remaining)
        raw_line = handle.readline(read_limit)
        if not raw_line:
            return
        complete = raw_line.endswith(b"\n") or (
            end is not None and handle.tell() >= end
        )
        oversized = len(raw_line) > MAX_JSONL_RECORD_BYTES or not complete
        if oversized and not raw_line.endswith(b"\n"):
            while end is None or handle.tell() < end:
                remaining = None if end is None else end - handle.tell()
                if remaining is not None and remaining <= 0:
                    break
                chunk_limit = JSONL_DRAIN_CHUNK_BYTES
                if remaining is not None:
                    chunk_limit = min(chunk_limit, remaining)
                chunk = handle.readline(chunk_limit)
                if not chunk or chunk.endswith(b"\n"):
                    break
        yield record_start, None if oversized else raw_line


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


def _zcode_message_count(db_path, session_id):
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = conn.execute(
            "select count(*) from message where session_id = ?",
            (session_id,),
        ).fetchone()
        conn.close()
    except sqlite3.Error as exc:
        raise OSError(f"cannot read ZCode message cursor: {db_path}") from exc
    return int(row[0]) if row else 0


def find_recent_transcripts(cfg, processed_ids=None, hours=48):
    processed_ids = processed_ids or {}
    cutoff = datetime.now().timestamp() - (hours * 3600)
    candidates = []

    for path in iter_transcript_files(get_transcript_roots(cfg)):
        try:
            mtime = transcript_mtime(path)
        except OSError:
            continue
        if mtime < cutoff:
            continue
        session_id = transcript_state_key(path)
        if isinstance(processed_ids, dict):
            prior_version = processed_ids.get(session_id)
            if prior_version is None:
                prior_version = processed_ids.get(session_id_from_path(path))
            if prior_version:
                try:
                    if prior_version == transcript_version(path):
                        continue
                except OSError:
                    continue
        elif session_id in processed_ids:
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


def transcript_state_key(path):
    """Return a collision-resistant heartbeat key without changing session IDs."""
    db_path, zcode_session_id = split_zcode_locator(path)
    if zcode_session_id:
        return zcode_session_id
    session_id = session_id_from_path(path)
    if re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        session_id,
        re.IGNORECASE,
    ):
        return session_id
    source = os.path.normcase(os.path.abspath(expand_path(db_path or path)))
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
    return f"{session_id}@{digest}"


def _apply_jsonl_record_metadata(record, meta, embedded_session_id_seen):
    timestamp = record.get("timestamp")
    if timestamp and not meta.get("timestamp"):
        meta["timestamp"] = timestamp
        meta["date"] = normalize_iso_date(str(timestamp)[:10])

    if record.get("sessionId") and not embedded_session_id_seen:
        meta["session_id"] = record["sessionId"]
        embedded_session_id_seen = True
    if record.get("cwd") and not meta.get("cwd"):
        meta["cwd"] = record["cwd"]
    if record.get("type") in ("user", "assistant") and not meta.get("agent"):
        meta["agent"] = "claude"

    payload = record.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}
    if has_subagent_marker(record):
        meta["is_subagent"] = True
    if record.get("type") == "session_meta":
        meta.update(
            {
                "session_id": payload.get("id") or meta.get("session_id"),
                "cwd": payload.get("cwd") or meta.get("cwd"),
                "agent": "codex",
                "source": payload.get("source") or meta.get("source"),
                "thread_source": (
                    payload.get("thread_source") or meta.get("thread_source")
                ),
            }
        )
        if has_subagent_marker(payload):
            meta["is_subagent"] = True
        if payload.get("timestamp"):
            meta.setdefault("timestamp", payload["timestamp"])
            meta.setdefault(
                "date",
                normalize_iso_date(str(payload["timestamp"])[:10]),
            )
    return embedded_session_id_seen


def parse_transcript(path):
    """Parse a Claude/Codex JSONL transcript into {text, meta, messages}."""
    db_path, zcode_session_id = split_zcode_locator(path)
    if db_path:
        return parse_zcode_sqlite_transcript(db_path, zcode_session_id)

    if path and _is_sqlite_path(path) and os.path.exists(path):
        return parse_zcode_sqlite_transcript(path, None)

    if not path or not os.path.exists(path):
        return {"text": "", "meta": {}, "messages": [], "observations": []}

    if os.path.isdir(path):
        files = sorted(
            [f for f in os.listdir(path) if f.endswith(".jsonl")],
            key=lambda f: os.path.getmtime(os.path.join(path, f)),
            reverse=True,
        )
        if not files:
            return {"text": "", "meta": {}, "messages": [], "observations": []}
        path = os.path.join(path, files[0])

    if not path.endswith(".jsonl"):
        return {"text": "", "meta": {}, "messages": [], "observations": []}

    messages = []
    seen_messages = set()
    observation_collector = CodexObservationCollector()
    meta = {
        "session_id": session_id_from_path(path),
        "is_subagent": False,
    }
    embedded_session_id_seen = False

    try:
        handle = open(path, "rb")
    except OSError as exc:
        raise TranscriptReadError(f"cannot read transcript: {path}") from exc

    with handle:
        for event_index, raw_line in _iter_bounded_jsonl_records(handle):
            if raw_line is None:
                continue
            try:
                record = json.loads(raw_line.decode("utf-8", errors="replace"))
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
                continue
            if not isinstance(record, dict):
                continue

            observation_collector.observe(record, event_index)

            record_type = record.get("type")
            payload = record.get("payload") or {}
            embedded_session_id_seen = _apply_jsonl_record_metadata(
                record,
                meta,
                embedded_session_id_seen,
            )

            if record_type == "session_meta":
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
    return {
        "text": "\n".join(parts),
        "meta": meta,
        "messages": messages,
        "observations": observation_collector.observations,
    }


def has_subagent_marker(record):
    """Recognize Codex and Claude child-agent transcript metadata."""
    if not isinstance(record, dict):
        return False
    if record.get("isSidechain") is True:
        return True
    if str(record.get("thread_source") or "").lower() == "subagent":
        return True
    source = record.get("source")
    if isinstance(source, dict):
        return "subagent" in source
    return str(source or "").lower() == "subagent"


def parse_zcode_sqlite_transcript(
    db_path,
    session_id=None,
    message_start=0,
    message_end=None,
):
    """Parse one ZCode SQLite session into the common transcript shape."""
    meta = {"agent": "zcode", "is_subagent": False}
    messages = []
    seen_messages = set()

    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        if not session_id:
            row = conn.execute(
                "select id from session order by time_updated desc, id desc limit 1"
            ).fetchone()
            session_id = row["id"] if row else None
        if not session_id:
            return {
                "text": "",
                "meta": meta,
                "messages": [],
                "observations": [],
            }

        session = conn.execute(
            "select id, directory, title, time_created, time_updated from session where id = ?",
            (session_id,),
        ).fetchone()
        if not session:
            raise TranscriptReadError(
                f"ZCode session not found while parsing: {session_id}"
            )

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
        message_rows = message_rows[
            max(0, int(message_start or 0)):
            None if message_end is None else max(0, int(message_end))
        ]
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
    except sqlite3.Error as exc:
        raise TranscriptReadError(
            f"cannot read ZCode transcript: {db_path}"
        ) from exc
    finally:
        if conn is not None:
            conn.close()

    parts = []
    if meta.get("cwd"):
        parts.append(f"[cwd: {meta['cwd']}]")
    parts.extend(m["text"] for m in messages)
    return {
        "text": "\n".join(parts),
        "meta": meta,
        "messages": messages,
        "observations": [],
    }


class CodexObservationCollector:
    """Extract bounded machine evidence without exposing raw tool input."""

    def __init__(self):
        self.calls = {}
        self.observations = []

    def observe(self, record, event_index, emit=True):
        if not isinstance(record, dict):
            return
        payload = record.get("payload") or {}
        if not isinstance(payload, dict):
            return
        if record.get("type") == "response_item":
            payload_type = payload.get("type")
            if payload_type in {"function_call", "custom_tool_call"}:
                self._remember_call(payload)
            elif payload_type in {
                "function_call_output",
                "custom_tool_call_output",
            } and emit:
                self._record_tool_result(payload, event_index)

    def _remember_call(self, payload):
        call_id = str(payload.get("call_id") or "").strip()
        if not call_id or len(call_id) > MAX_CALL_ID_CHARS:
            return
        if call_id not in self.calls and len(self.calls) >= MAX_TRACKED_TOOL_CALLS:
            return
        operation = _safe_operation_name(payload.get("name"))
        raw_input = payload.get("arguments")
        if raw_input is None:
            raw_input = payload.get("input")
        canonical_input = _canonical_tool_input(raw_input)
        if canonical_input is None:
            return
        self.calls[call_id] = {
            "operation": operation,
            "operation_hash": _sha256_text(f"{operation}:{canonical_input}"),
            "is_test": bool(TEST_COMMAND_PATTERN.search(canonical_input)),
        }

    def _record_tool_result(self, payload, event_index):
        if len(self.observations) >= MAX_ERROR_OBSERVATIONS:
            return
        call_id = str(payload.get("call_id") or "").strip()
        if not call_id or len(call_id) > MAX_CALL_ID_CHARS:
            return
        call = self.calls.get(call_id) or {
            "operation": "unknown",
            "operation_hash": _sha256_text(f"unknown:{call_id}"),
            "is_test": False,
        }
        output_text = _tool_output_text(payload.get("output"))
        if output_text is None:
            return
        success = _tool_success(output_text, payload.get("output"))
        if success is None:
            return
        excerpt = _structured_tool_diagnostic(
            call["operation"],
            success,
            output_text,
            payload.get("output"),
        )
        self.observations.append(
            {
                "event_index": int(event_index),
                "kind": "tool_result",
                "operation": call["operation"],
                "operation_hash": call["operation_hash"],
                "is_test": call["is_test"],
                "success": success,
                "excerpt": excerpt,
            }
        )


TEST_COMMAND_PATTERN = re.compile(
    r"(?:\bpytest\b|\bpy\.test\b|\bunittest\b|\bnpm\s+test\b|"
    r"\bpnpm\s+test\b|\byarn\s+test\b|\bcargo\s+test\b|"
    r"\bgo\s+test\b|\bswift\s+test\b|\bxcodebuild\b)",
    re.IGNORECASE,
)
MAX_CALL_ID_CHARS = 200
MAX_TOOL_INPUT_CHARS = 65_536
MAX_TOOL_OUTPUT_CHARS = 65_536
MAX_TOOL_CONTENT_ITEMS = 64
MAX_TOOL_STRUCTURE_DEPTH = 16
MAX_TRACKED_TOOL_CALLS = 4_096
MAX_ERROR_OBSERVATIONS = 4_096
TOOL_FAILURE_PATTERN = re.compile(
    r"(?:\bScript failed\b|\bProcess exited with code\s+[1-9]\d*\b|"
    r"[\"']?exit_code[\"']?\s*[:=]\s*[1-9]\d*|\bexit=[1-9]\d*\b)",
    re.IGNORECASE,
)
TOOL_SUCCESS_PATTERN = re.compile(
    r"(?:\bScript completed\b|\bProcess exited with code\s+0\b|"
    r"[\"']?exit_code[\"']?\s*[:=]\s*0\b|\bexit=0\b)",
    re.IGNORECASE,
)
DIAGNOSTIC_PATTERNS = (
    ("assertion_failure", re.compile(r"\b(?:AssertionError|assertion failed)\b", re.I)),
    ("permission_denied", re.compile(r"\b(?:PermissionError|permission denied)\b", re.I)),
    ("file_not_found", re.compile(r"\b(?:FileNotFoundError|no such file or directory)\b", re.I)),
    ("command_not_found", re.compile(r"\bcommand not found\b", re.I)),
    ("timeout", re.compile(r"\b(?:TimeoutError|timed out|timeout)\b", re.I)),
    ("connection_failure", re.compile(r"\b(?:ConnectionError|connection refused|connection reset)\b", re.I)),
    ("syntax_error", re.compile(r"\bSyntaxError\b", re.I)),
    ("invalid_argument", re.compile(r"\b(?:ValueError|invalid argument)\b", re.I)),
    ("dependency_missing", re.compile(r"\b(?:ModuleNotFoundError|ImportError|package not found)\b", re.I)),
    ("lock_ownership_lost", re.compile(r"\block ownership lost\b", re.I)),
    ("process_killed", re.compile(r"\b(?:killed|SIGKILL|SIGTERM)\b", re.I)),
)


def _canonical_tool_input(value):
    if isinstance(value, str):
        if len(value) > MAX_TOOL_INPUT_CHARS:
            return None
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, RecursionError):
            return value.strip()
        value = parsed
    if isinstance(value, (dict, list)):
        if not _bounded_tool_structure(value):
            return None
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, RecursionError):
            return None
    text = str(value or "").strip()
    return text if len(text) <= MAX_TOOL_INPUT_CHARS else None


def _bounded_tool_structure(value, depth=0, state=None):
    if depth > MAX_TOOL_STRUCTURE_DEPTH:
        return False
    state = state if state is not None else {"items": 0, "chars": 0}
    if isinstance(value, dict):
        state["items"] += len(value)
        if state["items"] > MAX_TOOL_CONTENT_ITEMS:
            return False
        for key, child in value.items():
            state["chars"] += len(str(key))
            if state["chars"] > MAX_TOOL_INPUT_CHARS:
                return False
            if not _bounded_tool_structure(child, depth + 1, state):
                return False
        return True
    if isinstance(value, list):
        state["items"] += len(value)
        if state["items"] > MAX_TOOL_CONTENT_ITEMS:
            return False
        return all(
            _bounded_tool_structure(child, depth + 1, state)
            for child in value
        )
    if isinstance(value, str):
        state["chars"] += len(value)
        return state["chars"] <= MAX_TOOL_INPUT_CHARS
    return value is None or isinstance(value, (bool, int, float))


def _safe_operation_name(value):
    operation = re.sub(r"[^A-Za-z0-9_.:-]", "", str(value or ""))[:80]
    return operation or "unknown"


def _sha256_text(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _tool_output_text(output):
    if isinstance(output, str):
        return output if len(output) <= MAX_TOOL_OUTPUT_CHARS else None
    if isinstance(output, list):
        if len(output) > MAX_TOOL_CONTENT_ITEMS:
            return None
        parts = []
        total = 0
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "input_text":
                continue
            text = item.get("text")
            if not isinstance(text, str):
                continue
            total += len(text)
            if total > MAX_TOOL_OUTPUT_CHARS:
                return None
            parts.append(text)
        return "\n".join(parts)
    if isinstance(output, dict):
        parts = []
        total = 0
        for key in ("error", "stderr", "output", "stdout", "message"):
            if isinstance(output.get(key), str):
                text = output[key]
                total += len(text)
                if total > MAX_TOOL_OUTPUT_CHARS:
                    return None
                parts.append(text)
        if "exit_code" in output:
            parts.append(f"exit_code={output.get('exit_code')}")
        return "\n".join(parts)
    return ""


def _tool_success(text, raw_output):
    if isinstance(raw_output, str):
        if len(raw_output) > MAX_TOOL_OUTPUT_CHARS:
            return None
        try:
            parsed = json.loads(raw_output)
        except (json.JSONDecodeError, RecursionError):
            parsed = None
        if isinstance(parsed, dict):
            exit_code = parsed.get("exit_code")
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                return exit_code == 0
    if isinstance(raw_output, dict):
        exit_code = raw_output.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            return exit_code == 0
    if TOOL_FAILURE_PATTERN.search(text):
        return False
    if TOOL_SUCCESS_PATTERN.search(text):
        return True
    return None


def _structured_tool_diagnostic(operation, success, text, raw_output):
    state = "completed" if success else "failed"
    parts = [f"{operation} {state}"]
    exit_code = _tool_exit_code(raw_output, text)
    if exit_code is not None:
        parts.append(f"exit_code={exit_code}")
    categories = [
        name for name, pattern in DIAGNOSTIC_PATTERNS if pattern.search(text)
    ]
    if categories:
        parts.append("diagnostic=" + ",".join(categories[:4]))
    elif not success:
        parts.append("diagnostic=process_failure")
    return "; ".join(parts)


def _tool_exit_code(raw_output, text):
    parsed = raw_output
    if isinstance(raw_output, str) and len(raw_output) <= MAX_TOOL_OUTPUT_CHARS:
        try:
            parsed = json.loads(raw_output)
        except (json.JSONDecodeError, RecursionError):
            parsed = None
    if isinstance(parsed, dict):
        value = parsed.get("exit_code")
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    match = re.search(
        r"(?:exit_code[\"']?\s*[:=]\s*|exit=|code\s+)(-?\d+)",
        text,
        re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


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
