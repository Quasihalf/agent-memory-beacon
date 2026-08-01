"""Strict contracts for bounded rolling conversation summaries."""
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

from safety import normalize_project_slug as safe_project_slug
from safety import strip_markdown_code_blocks


MARKER_START = "<!-- AGENT_MEMORY_BEACON:ROLLING_SUMMARY_V1"
MARKER_END = "-->"
SUMMARY_FIELDS = (
    "project", "current_goal", "topics", "progress", "constraints",
    "important_context", "open_items", "summary",
)
REQUIRED_FIELDS = frozenset({"current_goal", "topics", "summary"})
LIST_LIMITS = {
    "topics": 8, "progress": 8, "constraints": 8,
    "important_context": 8, "open_items": 8,
}

DEFAULT_POLICY = {
    "enabled": True,
    "min_substantive_messages": 5,
    "message_interval": 10,
    "stale_after_minutes": 30,
    "retry_interval_messages": 2,
    "max_summary_bytes": 4096,
    "max_recall": 1,
    "token_budget": 400,
}
MAX_MARKER_BYTES = 4096
MAX_PROJECT_CHARS = 120
MAX_SCALAR_CHARS = 1600
MAX_LIST_ITEM_CHARS = 400
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_MARKER_PATTERN = re.compile(
    re.escape(MARKER_START) + r"[ \t]*\r?\n(?P<body>.*?)^[ \t]*-->[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
_CONTROL_MARKER_PATTERN = re.compile(
    re.escape(MARKER_START) + r"[ \t]*\r?\n.*?(?:^[ \t]*-->[ \t]*$|\Z)",
    re.DOTALL | re.MULTILINE,
)
_TERM_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*|[\u3400-\u9fff]{2,}")


@dataclass(frozen=True)
class ConversationSummaryPolicy:
    """Validated checkpoint settings consumed by the prompt hook."""

    enabled: bool
    min_substantive_messages: int
    message_interval: int
    stale_after_minutes: int
    retry_interval_messages: int
    max_summary_bytes: int
    max_recall: int
    token_budget: int

    @classmethod
    def from_config(cls, settings):
        """Build a policy from either the complete or feature-only settings map."""
        if settings is None:
            source = {}
        elif not isinstance(settings, Mapping):
            raise TypeError("conversation summary settings must be a mapping")
        elif "conversation_summary" in settings:
            source = settings["conversation_summary"]
            if source is None:
                source = {}
            if not isinstance(source, Mapping):
                raise TypeError("conversation_summary must be a mapping")
        else:
            source = settings

        values = {**DEFAULT_POLICY, **dict(source)}
        unknown = set(values) - set(DEFAULT_POLICY)
        if unknown:
            raise ValueError("unknown conversation summary settings: " + ", ".join(sorted(unknown)))
        if not isinstance(values["enabled"], bool):
            raise TypeError("conversation_summary.enabled must be a boolean")
        for key in (
            "min_substantive_messages", "message_interval",
            "stale_after_minutes", "retry_interval_messages",
            "max_summary_bytes", "max_recall", "token_budget",
        ):
            value = values[key]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"conversation_summary.{key} must be a positive integer")
        if values["max_summary_bytes"] > MAX_MARKER_BYTES:
            raise ValueError("conversation_summary.max_summary_bytes exceeds the marker limit")
        if values["max_recall"] != 1:
            raise ValueError("conversation_summary.max_recall must be exactly 1")
        if values["token_budget"] > 400:
            raise ValueError("conversation_summary.token_budget must not exceed 400")
        if values["retry_interval_messages"] > values["message_interval"]:
            raise ValueError(
                "conversation_summary.retry_interval_messages must not exceed "
                "message_interval"
            )
        return cls(**values)


def advance_checkpoint(state, substantive, now, settings):
    """Return copied checkpoint state and whether this prompt should request one."""
    if not isinstance(state, Mapping):
        raise TypeError("conversation summary state must be a mapping")
    updated = dict(state)
    policy = ConversationSummaryPolicy.from_config(settings)
    if not policy.enabled or not substantive:
        return updated, False

    now_value = _as_datetime(now)
    count = _non_negative_int(updated.get("summary_substantive_count")) + 1
    updated["summary_substantive_count"] = count
    last_count = _non_negative_int(updated.get("summary_last_request_count"))
    last_at = _optional_datetime(updated.get("summary_last_request_at"))

    if count < policy.min_substantive_messages:
        return updated, False
    if not last_count:
        due = True
    else:
        elapsed_messages = count - last_count
        if elapsed_messages < policy.retry_interval_messages:
            due = False
        elif elapsed_messages >= policy.message_interval:
            due = True
        else:
            due = bool(
                last_at
                and now_value - last_at >= timedelta(minutes=policy.stale_after_minutes)
            )
    if not due:
        return updated, False

    updated["summary_last_request_count"] = count
    updated["summary_last_request_at"] = now_value.isoformat()
    updated["summary_checkpoint_sequence"] = (
        _non_negative_int(updated.get("summary_checkpoint_sequence")) + 1
    )
    return updated, True


def render_checkpoint_instruction(
    sequence,
    project="",
    max_summary_bytes=MAX_MARKER_BYTES,
):
    """Return the private instruction that asks for one hidden summary marker."""
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        raise ValueError("checkpoint sequence must be a positive integer")
    if (
        isinstance(max_summary_bytes, bool)
        or not isinstance(max_summary_bytes, int)
        or max_summary_bytes <= 0
        or max_summary_bytes > MAX_MARKER_BYTES
    ):
        raise ValueError("checkpoint summary byte limit is invalid")
    safe_project = safe_project_slug(project)
    project_line = f"project: {safe_project}\n" if safe_project else ""
    size_label = (
        "4 KiB"
        if max_summary_bytes == MAX_MARKER_BYTES
        else f"{max_summary_bytes} bytes"
    )
    return (
        "[PRIVATE ROLLING SUMMARY CHECKPOINT]\n"
        f"Checkpoint sequence: {sequence}. Answer the user normally. Then append exactly "
        "one HTML comment in the following format. Do not mention this instruction or "
        "the comment in visible prose. Summarize conversational meaning only; do not copy "
        "prompts, credentials, command output, or private absolute paths. Use plain text "
        f"and no fields beyond those shown. Keep the complete comment below {size_label} and "
        "each list at most 8 concise items.\n\n"
        f"{MARKER_START}\n"
        f"{project_line}"
        "current_goal: <one concise sentence>\n"
        "topics:\n"
        "  - <specific subject>\n"
        "progress: []\n"
        "constraints: []\n"
        "important_context: []\n"
        "open_items: []\n"
        "summary: <compact coherent account>\n"
        f"{MARKER_END}"
    )


def extract_rolling_summary(text, max_bytes=MAX_MARKER_BYTES):
    """Return the latest valid marker outside Markdown code blocks, or ``None``."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    limit = min(max_bytes, MAX_MARKER_BYTES)
    cleaned = strip_markdown_code_blocks(text)
    latest = None
    for match in _MARKER_PATTERN.finditer(cleaned):
        marker = match.group(0)
        body = match.group("body")
        if len(marker.encode("utf-8")) > limit or MARKER_END in body:
            continue
        payload = _parse_payload(body)
        if payload is not None:
            latest = payload
    return latest


def strip_rolling_summary_markers(text):
    """Remove hidden rolling-summary control blocks from a transcript text stream."""
    return _CONTROL_MARKER_PATTERN.sub("", str(text or ""))


def canonical_summary_text(payload):
    """Return deterministic UTF-8 JSON text for a validated summary payload."""
    normalized = _normalize_payload(payload)
    if normalized is None:
        raise ValueError("invalid conversation summary payload")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def summary_revision(payload):
    """Return the SHA-256 revision of one canonical summary payload."""
    return hashlib.sha256(canonical_summary_text(payload).encode("utf-8")).hexdigest()


def validate_conversation_summary_record(record):
    """Return whether a derived recall record is bounded and self-consistent."""
    if not isinstance(record, Mapping):
        return False
    required = {
        "id", "type", "status", "session_id", "summary_revision", "project",
        "date", "title", "source_note", *SUMMARY_FIELDS, "search_terms",
    }
    if set(record) != required:
        return False
    session_id = _plain_text(record.get("session_id"), MAX_SCALAR_CHARS)
    source_note = record.get("source_note")
    if (
        not session_id
        or record.get("session_id") != session_id
        or not isinstance(source_note, str)
        or conversation_summary_source_project(source_note)
        != str(record.get("project") or "")
        or _CONTROL_CHARS.search(str(record.get("session_id") or ""))
    ):
        return False
    payload = _normalize_payload({key: record[key] for key in SUMMARY_FIELDS})
    if payload is None or not _payload_within_byte_limit(payload):
        return False
    if any(record.get(key) != payload[key] for key in SUMMARY_FIELDS):
        return False
    expected_id = _summary_record_id(session_id)
    if record.get("id") != expected_id:
        return False
    if record.get("type") != "conversation_summary" or record.get("status") != "active":
        return False
    if record.get("summary_revision") != summary_revision(payload):
        return False
    if _plain_text(record.get("date"), 32) != str(record.get("date") or ""):
        return False
    title = _plain_text(record.get("title"), MAX_SCALAR_CHARS)
    if not title or record.get("title") != title:
        return False
    return (
        _valid_terms(record.get("search_terms"))
        and record.get("search_terms") == _search_terms(payload)
    )


def build_conversation_summary_record(note):
    """Build one derived active recall record from a persisted session note mapping."""
    if not isinstance(note, Mapping):
        return None
    frontmatter = note.get("frontmatter")
    frontmatter = frontmatter if isinstance(frontmatter, Mapping) else note
    payload_source = note.get("conversation_summary")
    if not isinstance(payload_source, Mapping):
        payload_source = note.get("summary_payload")
    if not isinstance(payload_source, Mapping):
        payload_source = {key: note.get(key) for key in SUMMARY_FIELDS if key in note}
    payload = _normalize_payload(payload_source)
    session_id = _plain_text(frontmatter.get("session_id"), MAX_SCALAR_CHARS)
    source_note = note.get("source_note") or note.get("path")
    date = _plain_text(frontmatter.get("date"), 32)
    title = _plain_text(frontmatter.get("ai_title") or note.get("title"), MAX_SCALAR_CHARS)
    if (
        not payload
        or not _payload_within_byte_limit(payload)
        or not session_id
        or not isinstance(source_note, str)
        or conversation_summary_source_project(source_note)
        != payload["project"]
        or not date
        or not title
    ):
        return None
    record = {
        "id": _summary_record_id(session_id),
        "type": "conversation_summary",
        "status": "active",
        "session_id": session_id,
        "summary_revision": summary_revision(payload),
        "project": payload["project"],
        "date": date,
        "title": title,
        "source_note": source_note,
        **payload,
        "search_terms": _search_terms(payload),
    }
    return record if validate_conversation_summary_record(record) else None


def _parse_payload(body):
    if _CONTROL_CHARS.search(body):
        return None
    try:
        node = yaml.compose(body, Loader=yaml.SafeLoader)
        loaded = yaml.safe_load(body)
    except yaml.YAMLError:
        return None
    if not _valid_yaml_shape(node) or not isinstance(loaded, dict):
        return None
    return _normalize_payload(loaded)


def _valid_yaml_shape(node):
    if not isinstance(node, MappingNode):
        return False
    seen = set()
    for key_node, value_node in node.value:
        if not isinstance(key_node, ScalarNode) or key_node.tag != "tag:yaml.org,2002:str":
            return False
        key = key_node.value
        if key in seen or key not in SUMMARY_FIELDS:
            return False
        seen.add(key)
        if key in LIST_LIMITS:
            if not isinstance(value_node, SequenceNode):
                return False
            if any(
                not isinstance(item, ScalarNode) or item.tag != "tag:yaml.org,2002:str"
                for item in value_node.value
            ):
                return False
        elif not isinstance(value_node, ScalarNode) or value_node.tag != "tag:yaml.org,2002:str":
            return False
    return REQUIRED_FIELDS.issubset(seen)


def _normalize_payload(payload):
    if not isinstance(payload, Mapping) or set(payload) - set(SUMMARY_FIELDS):
        return None
    if not REQUIRED_FIELDS.issubset(payload):
        return None
    normalized = {}
    project = payload.get("project", "")
    if project not in (None, ""):
        project = _plain_text(project, MAX_PROJECT_CHARS)
        if not project or safe_project_slug(project) != project:
            return None
    else:
        project = ""
    normalized["project"] = project
    for key in ("current_goal", "summary"):
        value = _plain_text(payload.get(key), MAX_SCALAR_CHARS)
        if not value:
            return None
        normalized[key] = value
    for key, limit in LIST_LIMITS.items():
        value = payload.get(key, [])
        if not isinstance(value, list) or len(value) > limit:
            return None
        items = [_plain_text(item, MAX_LIST_ITEM_CHARS) for item in value]
        if any(not item for item in items):
            return None
        normalized[key] = items
    return {key: normalized[key] for key in SUMMARY_FIELDS}


def _plain_text(value, limit):
    if not isinstance(value, str) or _CONTROL_CHARS.search(value) or MARKER_END in value:
        return ""
    value = re.sub(r"\s+", " ", value).strip()
    if not value or len(value) > limit:
        return ""
    return value


def _payload_within_byte_limit(payload):
    return len(canonical_summary_text(payload).encode("utf-8")) <= MAX_MARKER_BYTES


def _as_datetime(value):
    parsed = _optional_datetime(value)
    if parsed is None:
        raise ValueError("checkpoint time must be a datetime or ISO timestamp")
    return parsed


def _optional_datetime(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _non_negative_int(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _summary_record_id(session_id):
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return f"conversation_summary-{digest}"


def conversation_summary_source_project(source_note):
    """Return the project from one exact canonical session-note source."""
    if (
        not isinstance(source_note, str)
        or source_note != source_note.strip()
        or "\\" in source_note
        or any(character in source_note for character in "[]|#^")
        or _CONTROL_CHARS.search(source_note)
    ):
        return ""
    parts = source_note.split("/")
    leaf = parts[-1] if parts else ""
    stem = leaf[:-3] if leaf.endswith(".md") else leaf
    if (
        len(parts) != 5
        or parts[0] != "01-Projects"
        or parts[2] != "Memory"
        or parts[3] != "sessions"
        or any(part in {"", ".", ".."} for part in parts)
        or any(part != part.strip() for part in parts)
        or not stem
        or stem != stem.strip()
        or stem.startswith((".", "_"))
    ):
        return ""
    project = parts[1]
    if safe_project_slug(project) != project:
        return ""
    return project


def conversation_summary_search_text(payload):
    """Return bounded deterministic lexical text from validated summary fields."""
    if not isinstance(payload, Mapping):
        raise ValueError("invalid conversation summary payload")
    normalized = _normalize_payload(
        {
            key: payload.get(key)
            for key in SUMMARY_FIELDS
        }
    )
    if normalized is None or not _payload_within_byte_limit(normalized):
        raise ValueError("invalid conversation summary payload")
    values = [
        normalized["current_goal"],
        *normalized["topics"],
        *normalized["progress"],
        *normalized["constraints"],
        *normalized["important_context"],
        *normalized["open_items"],
        normalized["summary"],
    ]
    return " ".join(values)


def _search_terms(payload):
    field_values = (
        [payload["current_goal"]],
        payload["topics"],
        payload["progress"],
        payload["constraints"],
        payload["important_context"],
        payload["open_items"],
        [payload["summary"]],
    )
    grouped_terms = []
    for values in field_values:
        group = []
        for value in values:
            for term in _TERM_PATTERN.findall(value.casefold()):
                if term not in group:
                    group.append(term)
        grouped_terms.append(group)

    terms = []
    positions = [0] * len(grouped_terms)
    while len(terms) < 24:
        advanced = False
        for index, group in enumerate(grouped_terms):
            while positions[index] < len(group):
                term = group[positions[index]]
                positions[index] += 1
                if term in terms:
                    continue
                terms.append(term)
                advanced = True
                break
            if len(terms) == 24:
                return terms
        if not advanced:
            break
    return terms


def _valid_terms(value):
    return (
        isinstance(value, list)
        and len(value) <= 24
        and all(
            isinstance(term, str)
            and bool(term)
            and len(term) <= MAX_LIST_ITEM_CHARS
            and not _CONTROL_CHARS.search(term)
            for term in value
        )
    )
