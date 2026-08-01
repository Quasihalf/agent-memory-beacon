"""Strict, dependency-free protocol primitives for Agent Memory Beacon sync."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from safety import (
    durable_atomic_write as secure_atomic_write,
    durable_rmdir as secure_rmdir,
    durable_unlink as secure_unlink,
    ensure_directory_tree as secure_ensure_directory_tree,
    exclusive_file_lock as secure_exclusive_file_lock,
    secure_open_file,
)


PROTOCOL_EVENT = "agent-memory-beacon-sync-event"
PROTOCOL_READY = "agent-memory-beacon-sync-ready"
PROTOCOL_RECEIPT = "agent-memory-beacon-sync-receipt"
PROTOCOL_SNAPSHOT = "agent-memory-beacon-sync-snapshot"
PROTOCOL_COMPLETE = "agent-memory-beacon-sync-complete"
PROTOCOL_CURRENT = "agent-memory-beacon-sync-current"
SCHEMA_VERSION = 1
TRANSCRIPT_SCHEMA_VERSION = 1
ATTACHMENT_SCHEMA_VERSION = 2
LEGACY_ATTACHMENT_SCHEMA_VERSION = 1
MAX_EVENT_JSON_BYTES = 128 * 1024
MIN_WINDOWS_ATOMIC_BUILD = 17763
SIGNED_INT64_MIN = -(2**63)
SIGNED_INT64_MAX = 2**63 - 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
WINDOWS_ABSOLUTE_RE = re.compile(r"^[A-Za-z]:[\\/]")
EVENT_SEQUENCE_DIRECTORY_RE = re.compile(r"^seq-([0-9]{20})$")
WINDOWS_RESERVED = frozenset(
    {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }
)
EVENT_FIELDS = frozenset(
    {
        "protocol",
        "schema_version",
        "device_id",
        "producer_instance_id",
        "seq",
        "event_id",
        "event_kind",
        "created_at",
        "agent",
        "session_id",
        "stream_id",
        "stream_epoch",
        "logical_record_id",
        "source_cursor",
        "metadata",
        "payload",
        "extensions",
    }
)
READY_FIELDS = frozenset(
    {
        "protocol",
        "schema_version",
        "device_id",
        "producer_instance_id",
        "seq",
        "event_id",
        "event_sha256",
        "object_count",
        "object_bytes",
    }
)
METADATA_FIELDS = frozenset(
    {"cwd", "timestamp", "date", "is_subagent", "source"}
)
PAYLOAD_FIELDS = frozenset({"sha256", "bytes", "media_type", "role"})
CURSOR_FIELDS = frozenset({"start", "end"})
ATTACHMENT_EXTENSION_FIELDS = frozenset({"attachment"})
ATTACHMENT_FIELDS = frozenset(
    {
        "reference_id",
        "original_name",
        "source_locator_sha256",
        "reference_kind",
    }
)
LEGACY_ATTACHMENT_FIELDS = frozenset(
    {
        "attachment_id",
        "original_name",
        "source_locator_sha256",
        "reference_kind",
        "transcript_cursor",
    }
)
ATTACHMENT_REFERENCE_KINDS = frozenset(
    {
        "codex.local_image",
        "codex.local_audio",
        "user.file_mention",
        "claude.attachment",
    }
)
MEDIA_TYPE_RE = re.compile(
    r"^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/"
    r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$"
)
ALLOWED_EVENT_KINDS = frozenset(
    {"transcript.chunk", "transcript.gap", "attachment.blob"}
)
OBJECT_PAYLOAD_ROLES = frozenset({"transcript-source", "attachment-source"})
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


class ProtocolError(ValueError):
    """Protocol bytes or a path failed a fail-closed validation rule."""


def canonical_json_bytes(value):
    """Encode deterministic protocol JSON after rejecting ambiguous values."""
    _validate_json_value(value)
    try:
        text = json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ProtocolError("value cannot be encoded as canonical JSON") from exc
    return text.encode("ascii") + b"\n"


def decode_bounded_json(data, *, max_bytes):
    """Decode one bounded protocol document with deterministic number rules."""
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or max_bytes <= 0
    ):
        raise ProtocolError("protocol JSON size limit is invalid")
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ProtocolError("protocol JSON must be bytes")
    try:
        document_size = len(data)
    except (TypeError, ValueError) as exc:
        raise ProtocolError("protocol JSON must be bytes") from exc
    if document_size > max_bytes:
        raise ProtocolError("protocol JSON exceeds size limit")
    try:
        document = bytes(data)
    except (TypeError, ValueError) as exc:
        raise ProtocolError("protocol JSON must be bytes") from exc
    try:
        value = json.loads(
            document,
            parse_int=_decode_signed_int64,
            parse_float=_reject_json_number,
            parse_constant=_reject_json_number,
        )
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        TypeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise ProtocolError("protocol JSON is malformed or outside bounds") from exc
    _validate_json_value(value)
    return value


def _decode_signed_int64(text):
    digits = text[1:] if text.startswith("-") else text
    if len(digits) > 19:
        raise ValueError("integer is outside signed 64-bit range")
    value = int(text)
    if value < SIGNED_INT64_MIN or value > SIGNED_INT64_MAX:
        raise ValueError("integer is outside signed 64-bit range")
    return value


def _reject_json_number(_text):
    raise ValueError("non-integer JSON numbers are not allowed")


def _validate_json_value(value, depth=0):
    if depth > 32:
        raise ProtocolError("JSON structure is too deep")
    if value is None or isinstance(value, (bool, str)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        if value < SIGNED_INT64_MIN or value > SIGNED_INT64_MAX:
            raise ProtocolError("integer is outside signed 64-bit range")
        return
    if isinstance(value, float):
        raise ProtocolError("float values are not allowed in protocol JSON")
    if isinstance(value, list):
        for item in value:
            _validate_json_value(item, depth + 1)
        return
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ProtocolError("protocol JSON objects require string keys")
        for child in value.values():
            _validate_json_value(child, depth + 1)
        return
    raise ProtocolError(f"unsupported protocol JSON value: {type(value).__name__}")


def sha256_bytes(value):
    return hashlib.sha256(bytes(value)).hexdigest()


def build_event(
    *,
    device_id,
    producer_instance_id,
    seq,
    event_kind,
    created_at,
    agent,
    session_id,
    stream_epoch,
    source_cursor,
    metadata,
    payload,
    extensions=None,
):
    """Build a deterministic immutable source event."""
    agent = str(agent or "").strip().lower()
    session_id = str(session_id or "").strip()
    event_kind = str(event_kind or "").strip()
    stream_id = "stream-" + sha256_bytes(
        f"{agent}\0{session_id}".encode("utf-8")
    )
    event = {
        "protocol": PROTOCOL_EVENT,
        "schema_version": (
            ATTACHMENT_SCHEMA_VERSION
            if event_kind == "attachment.blob"
            else TRANSCRIPT_SCHEMA_VERSION
        ),
        "device_id": str(device_id or "").strip(),
        "producer_instance_id": str(producer_instance_id or "").strip(),
        "seq": seq,
        "event_id": "",
        "event_kind": event_kind,
        "created_at": str(created_at or "").strip(),
        "agent": agent,
        "session_id": session_id,
        "stream_id": stream_id,
        "stream_epoch": str(stream_epoch or "").strip(),
        "logical_record_id": f"session:{agent}:{session_id}",
        "source_cursor": dict(source_cursor or {}),
        "metadata": dict(metadata or {}),
        "payload": dict(payload or {}),
        "extensions": dict(extensions or {}),
    }
    event["event_id"] = derive_event_id(event)
    validate_event(event)
    return event


def derive_event_id(event):
    """Derive the stable event ID, including kinds a reducer may reject."""
    identity = {
        key: event[key]
        for key in (
            "device_id",
            "producer_instance_id",
            "seq",
            "event_kind",
            "agent",
            "session_id",
            "stream_id",
            "stream_epoch",
            "logical_record_id",
            "source_cursor",
            "payload",
        )
    }
    if event.get("event_kind") == "attachment.blob":
        identity["extensions"] = event.get("extensions")
    return "event-" + sha256_bytes(canonical_json_bytes(identity))


def validate_event(event, *, expected_device_id=None, allow_unknown_kind=False):
    return _validate_event(
        event,
        expected_device_id=expected_device_id,
        allow_unknown_kind=allow_unknown_kind,
        legacy_attachment=False,
    )


def validate_legacy_attachment_event(event, *, expected_device_id=None):
    """Validate one already-durable v1 attachment without enabling v1 writes."""
    return _validate_event(
        event,
        expected_device_id=expected_device_id,
        allow_unknown_kind=False,
        legacy_attachment=True,
    )


def _validate_event(
    event,
    *,
    expected_device_id=None,
    allow_unknown_kind=False,
    legacy_attachment=False,
):
    if not isinstance(event, dict):
        raise ProtocolError("event must be an object")
    unknown = set(event) - EVENT_FIELDS
    missing = EVENT_FIELDS - set(event)
    if unknown:
        raise ProtocolError("unknown event fields: " + ", ".join(sorted(unknown)))
    if missing:
        raise ProtocolError("missing event fields: " + ", ".join(sorted(missing)))
    for key, name in (
        ("protocol", "event protocol"),
        ("device_id", "device ID"),
        ("producer_instance_id", "producer instance"),
        ("event_id", "event ID"),
        ("event_kind", "event kind"),
        ("created_at", "created_at"),
        ("agent", "transcript agent"),
        ("session_id", "session ID"),
        ("stream_id", "stream ID"),
        ("stream_epoch", "stream epoch"),
        ("logical_record_id", "logical record ID"),
    ):
        _require_string(event[key], name)
    if event["protocol"] != PROTOCOL_EVENT:
        raise ProtocolError("unsupported event protocol or schema")
    schema_version = event["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ProtocolError("unsupported event protocol or schema")
    event_kind = event["event_kind"]
    if legacy_attachment:
        if (
            schema_version != LEGACY_ATTACHMENT_SCHEMA_VERSION
            or event_kind != "attachment.blob"
        ):
            raise ProtocolError("unsupported legacy attachment event schema")
    elif event_kind == "attachment.blob":
        if schema_version != ATTACHMENT_SCHEMA_VERSION:
            raise ProtocolError("unsupported attachment event schema")
    elif schema_version != TRANSCRIPT_SCHEMA_VERSION:
        raise ProtocolError("unsupported event protocol or schema")

    _valid_utc_timestamp(event["created_at"], "created_at")
    device_id = _safe_id(event["device_id"], "device ID", max_length=128)
    if expected_device_id is not None and device_id != expected_device_id:
        raise ProtocolError("event device does not match inbox binding")
    _valid_uuid(event["producer_instance_id"], "producer instance")
    _positive_int(event["seq"], "event sequence")
    if event_kind not in ALLOWED_EVENT_KINDS and not allow_unknown_kind:
        raise ProtocolError("unsupported event kind")
    if event["agent"] not in {"codex", "claude"}:
        raise ProtocolError("unsupported transcript agent")
    _safe_id(event["session_id"], "session ID")
    expected_stream = "stream-" + sha256_bytes(
        f"{event['agent']}\0{event['session_id']}".encode("utf-8")
    )
    if event["stream_id"] != expected_stream:
        raise ProtocolError("stream ID does not match agent and session")
    _valid_uuid(event["stream_epoch"], "stream epoch")
    expected_logical = f"session:{event['agent']}:{event['session_id']}"
    if event["logical_record_id"] != expected_logical:
        raise ProtocolError("logical record ID does not match event identity")
    if not re.fullmatch(r"event-[0-9a-f]{64}", event["event_id"]):
        raise ProtocolError("invalid event ID")

    _exact_mapping(event["source_cursor"], CURSOR_FIELDS, "source cursor")
    start = _nonnegative_int(event["source_cursor"]["start"], "cursor start")
    end = _nonnegative_int(event["source_cursor"]["end"], "cursor end")
    if end <= start:
        raise ProtocolError("source cursor must advance")

    metadata = event["metadata"]
    if not isinstance(metadata, dict) or set(metadata) - METADATA_FIELDS:
        raise ProtocolError("invalid metadata fields")
    for key, value in metadata.items():
        if key == "is_subagent":
            if not isinstance(value, bool):
                raise ProtocolError("metadata is_subagent must be a boolean")
            continue
        if not isinstance(value, str) or len(value) > 2048:
            raise ProtocolError(f"metadata {key} is invalid")
        if _has_control(value):
            raise ProtocolError(f"metadata {key} contains control characters")

    _exact_mapping(event["payload"], PAYLOAD_FIELDS, "payload")
    for key, name in (
        ("sha256", "payload SHA-256"),
        ("media_type", "payload media type"),
        ("role", "payload role"),
    ):
        _require_string(event["payload"][key], name)
    _valid_sha256(event["payload"]["sha256"], "payload SHA-256")
    payload_bytes = _positive_int(event["payload"]["bytes"], "payload bytes")
    if not (
        event_kind == "attachment.blob"
        and schema_version == ATTACHMENT_SCHEMA_VERSION
    ) and payload_bytes != end - start:
        raise ProtocolError("payload byte count does not match source cursor")
    if event_kind in ALLOWED_EVENT_KINDS:
        expected_role = {
            "transcript.chunk": "transcript-source",
            "transcript.gap": "transcript-gap",
            "attachment.blob": "attachment-source",
        }[event_kind]
        if event["payload"]["role"] != expected_role:
            raise ProtocolError("payload role does not match event kind")
    if event_kind in {"transcript.chunk", "transcript.gap"}:
        if event["payload"]["media_type"] != "application/x-ndjson":
            raise ProtocolError("unsupported payload media type")
    elif event_kind == "attachment.blob":
        if legacy_attachment:
            _validate_legacy_attachment_extensions(event)
        else:
            _validate_attachment_extensions(event)
        media_type = event["payload"]["media_type"]
        if (
            not isinstance(media_type, str)
            or len(media_type) > 128
            or not MEDIA_TYPE_RE.fullmatch(media_type)
        ):
            raise ProtocolError("attachment media type is invalid")
    if not isinstance(event["extensions"], dict):
        raise ProtocolError("event extensions must be an object")
    if len(canonical_json_bytes(event["extensions"])) > 4096:
        raise ProtocolError("event extensions are oversized")
    if event["event_id"] != derive_event_id(event):
        raise ProtocolError("event ID does not match event bytes")
    if len(canonical_json_bytes(event)) > MAX_EVENT_JSON_BYTES:
        raise ProtocolError("event JSON exceeds the size limit")
    return event


def build_ready(event, event_bytes, *, allow_unknown_kind=False):
    validate_event(event, allow_unknown_kind=allow_unknown_kind)
    if bytes(event_bytes) != canonical_json_bytes(event):
        raise ProtocolError("event bytes are not canonical")
    object_count = 1 if event["payload"]["role"] in OBJECT_PAYLOAD_ROLES else 0
    object_bytes = event["payload"]["bytes"] if object_count else 0
    return {
        "protocol": PROTOCOL_READY,
        "schema_version": event["schema_version"],
        "device_id": event["device_id"],
        "producer_instance_id": event["producer_instance_id"],
        "seq": event["seq"],
        "event_id": event["event_id"],
        "event_sha256": sha256_bytes(event_bytes),
        "object_count": object_count,
        "object_bytes": object_bytes,
    }


def validate_ready(ready, event, event_bytes, *, allow_unknown_kind=False):
    return _validate_ready(
        ready,
        event,
        event_bytes,
        allow_unknown_kind=allow_unknown_kind,
        legacy_attachment=False,
    )


def validate_legacy_attachment_ready(ready, event, event_bytes):
    """Validate a ready marker for an already-durable v1 attachment."""
    return _validate_ready(
        ready,
        event,
        event_bytes,
        allow_unknown_kind=False,
        legacy_attachment=True,
    )


def _validate_ready(
    ready,
    event,
    event_bytes,
    *,
    allow_unknown_kind=False,
    legacy_attachment=False,
):
    _exact_mapping(ready, READY_FIELDS, "ready")
    for key, name in (
        ("protocol", "ready protocol"),
        ("device_id", "ready device ID"),
        ("producer_instance_id", "ready producer instance"),
        ("event_id", "ready event ID"),
        ("event_sha256", "ready event SHA-256"),
    ):
        _require_string(ready[key], name)
    for key, name in (
        ("schema_version", "ready schema version"),
        ("seq", "ready sequence"),
        ("object_count", "ready object count"),
        ("object_bytes", "ready object bytes"),
    ):
        _require_int(ready[key], name)
    if ready["protocol"] != PROTOCOL_READY:
        raise ProtocolError("unsupported ready protocol or schema")
    if legacy_attachment:
        validate_legacy_attachment_event(event)
    else:
        validate_event(event, allow_unknown_kind=allow_unknown_kind)
    if ready["schema_version"] != event["schema_version"]:
        raise ProtocolError("unsupported ready protocol or schema")
    if bytes(event_bytes) != canonical_json_bytes(event):
        raise ProtocolError("event bytes are not canonical")
    for key in ("device_id", "producer_instance_id", "seq", "event_id"):
        if ready[key] != event[key]:
            raise ProtocolError(f"ready {key} does not match event")
    if ready["event_sha256"] != sha256_bytes(event_bytes):
        raise ProtocolError("ready event hash does not match event bytes")
    expected_count = 1 if event["payload"]["role"] in OBJECT_PAYLOAD_ROLES else 0
    expected_bytes = event["payload"]["bytes"] if expected_count else 0
    if ready["object_count"] != expected_count:
        raise ProtocolError("ready object count does not match event")
    if ready["object_bytes"] != expected_bytes:
        raise ProtocolError("ready object bytes do not match event")
    return ready


def _validate_attachment_extensions(event):
    extensions = event["extensions"]
    _exact_mapping(
        extensions,
        ATTACHMENT_EXTENSION_FIELDS,
        "attachment extensions",
    )
    attachment = extensions["attachment"]
    _exact_mapping(attachment, ATTACHMENT_FIELDS, "attachment metadata")
    for key in ATTACHMENT_FIELDS:
        _require_string(attachment[key], f"attachment {key}")
    original_name = _valid_attachment_name(attachment["original_name"])
    source_digest = _valid_sha256(
        attachment["source_locator_sha256"],
        "attachment source locator SHA-256",
    )
    reference_kind = attachment["reference_kind"]
    if (
        not isinstance(reference_kind, str)
        or reference_kind not in ATTACHMENT_REFERENCE_KINDS
    ):
        raise ProtocolError("attachment reference kind is invalid")
    expected_id = derive_attachment_reference_id(
        producer_instance_id=event["producer_instance_id"],
        stream_id=event["stream_id"],
        stream_epoch=event["stream_epoch"],
        source_cursor=event["source_cursor"],
        source_locator_sha256=source_digest,
        original_name=original_name,
        payload_sha256=event["payload"]["sha256"],
    )
    if attachment["reference_id"] != expected_id:
        raise ProtocolError("attachment reference ID does not match event identity")


def derive_attachment_reference_id(
    *,
    producer_instance_id,
    stream_id,
    stream_epoch,
    source_cursor,
    source_locator_sha256,
    original_name,
    payload_sha256,
):
    """Bind one attachment reference to its exact transcript record."""
    _valid_uuid(producer_instance_id, "producer instance")
    if not isinstance(stream_id, str) or not re.fullmatch(
        r"stream-[0-9a-f]{64}", stream_id
    ):
        raise ProtocolError("attachment stream ID is invalid")
    _valid_uuid(stream_epoch, "stream epoch")
    _exact_mapping(source_cursor, CURSOR_FIELDS, "attachment source cursor")
    start = _nonnegative_int(
        source_cursor["start"],
        "attachment source cursor start",
    )
    end = _nonnegative_int(
        source_cursor["end"],
        "attachment source cursor end",
    )
    if end <= start:
        raise ProtocolError("attachment source cursor must advance")
    source_digest = _valid_sha256(
        source_locator_sha256,
        "attachment source locator SHA-256",
    )
    name = _valid_attachment_name(original_name)
    payload_digest = _valid_sha256(
        payload_sha256,
        "attachment payload SHA-256",
    )
    identity = {
        "original_name": name,
        "payload_sha256": payload_digest,
        "producer_instance_id": str(producer_instance_id),
        "source_cursor": dict(source_cursor),
        "source_locator_sha256": source_digest,
        "stream_epoch": str(stream_epoch),
        "stream_id": str(stream_id),
    }
    return "reference-" + sha256_bytes(canonical_json_bytes(identity))


def _validate_legacy_attachment_extensions(event):
    extensions = event["extensions"]
    _exact_mapping(
        extensions,
        ATTACHMENT_EXTENSION_FIELDS,
        "legacy attachment extensions",
    )
    attachment = extensions["attachment"]
    _exact_mapping(
        attachment,
        LEGACY_ATTACHMENT_FIELDS,
        "legacy attachment metadata",
    )
    for key in LEGACY_ATTACHMENT_FIELDS - {"transcript_cursor"}:
        _require_string(attachment[key], f"legacy attachment {key}")
    original_name = _valid_attachment_name(attachment["original_name"])
    source_digest = _valid_sha256(
        attachment["source_locator_sha256"],
        "legacy attachment source locator SHA-256",
    )
    reference_kind = attachment["reference_kind"]
    if (
        not isinstance(reference_kind, str)
        or reference_kind not in ATTACHMENT_REFERENCE_KINDS
    ):
        raise ProtocolError("legacy attachment reference kind is invalid")
    _exact_mapping(
        attachment["transcript_cursor"],
        CURSOR_FIELDS,
        "legacy attachment transcript cursor",
    )
    transcript_start = _nonnegative_int(
        attachment["transcript_cursor"]["start"],
        "legacy attachment transcript cursor start",
    )
    transcript_end = _nonnegative_int(
        attachment["transcript_cursor"]["end"],
        "legacy attachment transcript cursor end",
    )
    if transcript_end <= transcript_start:
        raise ProtocolError("legacy attachment transcript cursor must advance")
    identity = {
        "original_name": original_name,
        "payload_sha256": event["payload"]["sha256"],
        "source_locator_sha256": source_digest,
    }
    expected_id = "attachment-" + sha256_bytes(canonical_json_bytes(identity))
    if attachment["attachment_id"] != expected_id:
        raise ProtocolError("legacy attachment ID does not match metadata")


def _valid_attachment_name(value):
    if not isinstance(value, str):
        raise ProtocolError("attachment original name is invalid")
    text = value
    if (
        not text
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
        or _has_control(text)
        or text.endswith((" ", "."))
        or any(character in text for character in '<>:"|?*')
        or len(text.encode("utf-8")) > 255
    ):
        raise ProtocolError("attachment original name is invalid")
    stem = text.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED:
        raise ProtocolError("attachment original name is Windows reserved")
    return text


def event_bundle_name(event):
    validate_event(event)
    return f"{event['seq']:020d}-{event['event_id']}"


def event_sequence_directory_name(seq):
    seq = _positive_int(seq, "event sequence")
    if seq > SIGNED_INT64_MAX:
        raise ProtocolError("event sequence exceeds signed 64-bit range")
    return f"seq-{seq:020d}"


def is_event_sequence_directory_name(value):
    if not isinstance(value, str):
        return False
    match = EVENT_SEQUENCE_DIRECTORY_RE.fullmatch(value)
    if match is None:
        return False
    try:
        return event_sequence_directory_name(int(match.group(1))) == value
    except ProtocolError:
        return False


def validate_replica_path(value):
    """Validate one portable, forward-slash, Vault-relative manifest path."""
    if not isinstance(value, str):
        raise ProtocolError("replica path must be a string")
    text = value
    if (
        not text
        or text.startswith(("/", "\\"))
        or WINDOWS_ABSOLUTE_RE.match(text)
        or "\\" in text
        or _has_control(text)
    ):
        raise ProtocolError("replica path must be a portable relative path")
    parts = text.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ProtocolError("replica path contains an invalid component")
    for part in parts:
        if part.endswith((" ", ".")) or any(char in part for char in '<>:"|?*'):
            raise ProtocolError("replica path is not Windows portable")
        stem = part.split(".", 1)[0].upper()
        if stem in WINDOWS_RESERVED:
            raise ProtocolError("replica path uses a Windows reserved name")
        if len(part.encode("utf-8")) > 255:
            raise ProtocolError("replica path component is too long")
    if len(text) > 1024:
        raise ProtocolError("replica path is too long")
    return text


def portable_atomic_write(path, data, *, root, mode=0o600):
    """Atomically replace one regular file under a trusted root."""
    path = _rooted_path(path, root)
    root = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))
    root.mkdir(parents=True, exist_ok=True)
    _ensure_safe_directories(path.parent, root)
    if os.name != "nt":
        try:
            secure_atomic_write(
                path,
                bytes(data),
                mode=mode,
                root=root,
                preserve_existing_mode=False,
            )
        except (OSError, ValueError) as exc:
            raise ProtocolError(str(exc)) from exc
        return path
    _assert_supported_windows_atomic_filesystem()
    if os.path.lexists(path):
        current = os.lstat(path)
        if _is_link_or_reparse(current):
            raise ProtocolError("atomic destination is a symlink")
        if not stat.S_ISREG(current.st_mode):
            raise ProtocolError("atomic destination is not a regular file")
    temp = path.parent / f".{path.name}.{secrets.token_hex(16)}.tmp"
    descriptor = None
    try:
        descriptor = _windows_create_exclusive_temp(
            temp,
            root=root,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(bytes(data))
            handle.flush()
            os.fsync(handle.fileno())
            if os.path.lexists(path):
                current = os.lstat(path)
                if _is_link_or_reparse(current) or not stat.S_ISREG(
                    current.st_mode
                ):
                    raise ProtocolError(
                        "atomic destination changed before publish"
                    )
            _windows_atomic_replace(
                handle.fileno(),
                path,
                root=root,
                mode=mode,
            )
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            portable_unlink_regular(temp, root=root)
        except (FileNotFoundError, ProtocolError):
            pass
    return path


def portable_ensure_directory_tree(path, *, root):
    """Create one trusted directory chain on POSIX or Windows."""
    path = _rooted_path(path, root)
    root = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))
    if os.name != "nt":
        try:
            return Path(secure_ensure_directory_tree(path, root))
        except (OSError, ValueError) as exc:
            raise ProtocolError(str(exc)) from exc
    _ensure_safe_directories(path, root)
    return path


def _windows_rename_buffer_size(
    filename_bytes,
    filename_offset,
    structure_size,
):
    values = (filename_bytes, filename_offset, structure_size)
    if (
        any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in values
        )
        or filename_bytes <= 0
        or filename_bytes % 2
        or filename_offset < 0
        or structure_size <= 0
        or filename_offset >= structure_size
    ):
        raise ProtocolError("Windows rename buffer metadata is invalid")
    return max(structure_size, filename_offset + filename_bytes)


def _assert_supported_windows_atomic_filesystem(version=None):
    if version is None:
        if os.name != "nt":
            return
        get_version = getattr(sys, "getwindowsversion", None)
        if get_version is None:
            raise ProtocolError("Windows version information is unavailable")
        version = get_version()
    try:
        major = int(version.major)
        build = int(version.build)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProtocolError("Windows version information is invalid") from exc
    if major < 10 or (major == 10 and build < MIN_WINDOWS_ATOMIC_BUILD):
        raise ProtocolError(
            "Windows build 17763 or newer is required for safe atomic "
            "read-only file replacement"
        )


def _windows_create_exclusive_temp(path, *, root):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = (
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        )

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = (
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", FILETIME),
            ("ftLastAccessTime", FILETIME),
            ("ftLastWriteTime", FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        )

    class FILE_DISPOSITION_INFO_EX(ctypes.Structure):
        _fields_ = (("Flags", wintypes.DWORD),)

    generic_write = 0x40000000
    delete_access = 0x00010000
    file_read_attributes = 0x00000080
    file_write_attributes = 0x00000100
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    create_new = 1
    file_attribute_normal = 0x00000080
    open_reparse = 0x00200000
    file_attribute_directory = 0x00000010
    file_attribute_reparse = 0x00000400
    file_disposition_info_ex = 21
    delete_flags = 0x00000001 | 0x00000002 | 0x00000010

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(BY_HANDLE_FILE_INFORMATION),
    )
    get_information.restype = wintypes.BOOL
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_information.restype = wintypes.BOOL
    final_path_name = kernel32.GetFinalPathNameByHandleW
    final_path_name.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    final_path_name.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        os.fspath(path),
        generic_write
        | delete_access
        | file_read_attributes
        | file_write_attributes,
        share_all,
        None,
        create_new,
        file_attribute_normal | open_reparse,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ProtocolError(str(ctypes.WinError(ctypes.get_last_error())))
    transferred = False
    try:
        information = BY_HANDLE_FILE_INFORMATION()
        if not get_information(handle, ctypes.byref(information)):
            raise ProtocolError(str(ctypes.WinError(ctypes.get_last_error())))
        attributes = int(information.dwFileAttributes)
        if (
            attributes & (file_attribute_directory | file_attribute_reparse)
            or int(information.nNumberOfLinks) != 1
        ):
            raise ProtocolError("atomic temporary file is unsafe")
        try:
            _assert_windows_handle_under_root(
                handle,
                root,
                final_path_name,
                ctypes,
                wintypes,
            )
        except Exception:
            disposition = FILE_DISPOSITION_INFO_EX(delete_flags)
            set_information(
                handle,
                file_disposition_info_ex,
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            )
            raise
        descriptor = msvcrt.open_osfhandle(
            int(handle),
            os.O_WRONLY | getattr(os, "O_BINARY", 0),
        )
        transferred = True
        return descriptor
    finally:
        if not transferred:
            close_handle(handle)


def _windows_atomic_replace(descriptor, destination, *, root, mode):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = (
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        )

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = (
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", FILETIME),
            ("ftLastAccessTime", FILETIME),
            ("ftLastWriteTime", FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        )

    class FILE_BASIC_INFO(ctypes.Structure):
        _fields_ = (
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        )

    class FILE_RENAME_INFO_EX(ctypes.Structure):
        _fields_ = (
            ("Flags", wintypes.DWORD),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        )

    synchronize = 0x00100000
    file_add_file = 0x00000002
    file_traverse = 0x00000020
    file_delete_child = 0x00000040
    file_read_attributes = 0x00000080
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    open_reparse = 0x00200000
    backup_semantics = 0x02000000
    file_attribute_directory = 0x00000010
    file_attribute_normal = 0x00000080
    file_attribute_readonly = 0x00000001
    file_attribute_reparse = 0x00000400
    file_basic_info = 0
    file_rename_info_ex = 22
    rename_flags = 0x00000001 | 0x00000002 | 0x00000040

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(BY_HANDLE_FILE_INFORMATION),
    )
    get_information.restype = wintypes.BOOL
    get_information_ex = kernel32.GetFileInformationByHandleEx
    get_information_ex.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_information_ex.restype = wintypes.BOOL
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_information.restype = wintypes.BOOL
    final_path_name = kernel32.GetFinalPathNameByHandleW
    final_path_name.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    final_path_name.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    parent_handle = create_file(
        os.fspath(Path(destination).parent),
        file_add_file
        | file_traverse
        | file_delete_child
        | file_read_attributes
        | synchronize,
        share_all,
        None,
        open_existing,
        open_reparse | backup_semantics,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if parent_handle == invalid_handle:
        raise ProtocolError(str(ctypes.WinError(ctypes.get_last_error())))
    try:
        parent_information = BY_HANDLE_FILE_INFORMATION()
        if not get_information(
            parent_handle,
            ctypes.byref(parent_information),
        ):
            raise ProtocolError(str(ctypes.WinError(ctypes.get_last_error())))
        parent_attributes = int(parent_information.dwFileAttributes)
        if (
            not parent_attributes & file_attribute_directory
            or parent_attributes & file_attribute_reparse
        ):
            raise ProtocolError("atomic destination parent is unsafe")
        _assert_windows_handle_under_root(
            parent_handle,
            root,
            final_path_name,
            ctypes,
            wintypes,
        )

        if (
            isinstance(descriptor, bool)
            or not isinstance(descriptor, int)
            or descriptor < 0
        ):
            raise ProtocolError("atomic temporary descriptor is invalid")
        try:
            raw_temp_handle = msvcrt.get_osfhandle(descriptor)
        except OSError as exc:
            raise ProtocolError(str(exc)) from exc
        if raw_temp_handle == -1:
            raise ProtocolError("atomic temporary descriptor is invalid")
        temp_handle = wintypes.HANDLE(raw_temp_handle)
        temp_information = BY_HANDLE_FILE_INFORMATION()
        if not get_information(temp_handle, ctypes.byref(temp_information)):
            raise ProtocolError(str(ctypes.WinError(ctypes.get_last_error())))
        temp_attributes = int(temp_information.dwFileAttributes)
        if (
            temp_attributes & (file_attribute_directory | file_attribute_reparse)
            or int(temp_information.nNumberOfLinks) != 1
        ):
            raise ProtocolError("atomic temporary file changed before publish")
        _assert_windows_handle_under_root(
            temp_handle,
            root,
            final_path_name,
            ctypes,
            wintypes,
        )

        basic = FILE_BASIC_INFO()
        if not get_information_ex(
            temp_handle,
            file_basic_info,
            ctypes.byref(basic),
            ctypes.sizeof(basic),
        ):
            raise ProtocolError(str(ctypes.WinError(ctypes.get_last_error())))
        attributes = int(basic.FileAttributes)
        if int(mode) & stat.S_IWUSR:
            attributes &= ~file_attribute_readonly
        else:
            attributes |= file_attribute_readonly
        basic.FileAttributes = attributes or file_attribute_normal
        if not set_information(
            temp_handle,
            file_basic_info,
            ctypes.byref(basic),
            ctypes.sizeof(basic),
        ):
            raise ProtocolError(str(ctypes.WinError(ctypes.get_last_error())))

        filename_bytes = Path(destination).name.encode("utf-16-le")
        filename_offset = FILE_RENAME_INFO_EX.FileName.offset
        buffer = ctypes.create_string_buffer(
            _windows_rename_buffer_size(
                len(filename_bytes),
                filename_offset,
                ctypes.sizeof(FILE_RENAME_INFO_EX),
            )
        )
        rename = FILE_RENAME_INFO_EX.from_buffer(buffer)
        rename.Flags = rename_flags
        rename.RootDirectory = parent_handle
        rename.FileNameLength = len(filename_bytes)
        ctypes.memmove(
            ctypes.addressof(buffer) + filename_offset,
            filename_bytes,
            len(filename_bytes),
        )
        if not set_information(
            temp_handle,
            file_rename_info_ex,
            buffer,
            len(buffer),
        ):
            raise ProtocolError(str(ctypes.WinError(ctypes.get_last_error())))
    finally:
        close_handle(parent_handle)


def portable_unlink_regular(path, *, root, expected_identity=None):
    """Delete one regular file without following a swapped managed path."""
    path = _rooted_path(path, root)
    root = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))
    expected = _normalize_expected_identity(expected_identity)
    if os.name == "nt":
        return _windows_delete_by_handle(
            path,
            root=root,
            expected_identity=expected,
            expect_directory=False,
        )
    try:
        secure_unlink(
            path,
            root=root,
            expected_identity=expected[:2] if expected is not None else None,
        )
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as exc:
        raise ProtocolError(str(exc)) from exc
    return True


def portable_rmdir_empty(path, *, root, expected_identity=None):
    """Delete one empty real directory without following a swapped path."""
    path = _rooted_path(path, root)
    root = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))
    expected = _normalize_expected_identity(expected_identity)
    if os.name == "nt":
        return _windows_delete_by_handle(
            path,
            root=root,
            expected_identity=expected,
            expect_directory=True,
        )
    try:
        secure_rmdir(
            path,
            root=root,
            expected_identity=expected[:2] if expected is not None else None,
        )
    except FileNotFoundError:
        return False
    except (OSError, ValueError) as exc:
        raise ProtocolError(str(exc)) from exc
    return True


def portable_rmtree(path, *, root):
    """Recursively remove one real managed tree without following links."""
    path = _rooted_path(path, root)
    root = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))
    if not os.path.lexists(path):
        return False
    try:
        _portable_rmtree_entry(path, root, expected_identity=os.lstat(path))
    except FileNotFoundError as exc:
        raise ProtocolError("managed tree changed during removal") from exc
    except ProtocolError:
        raise
    except (OSError, ValueError) as exc:
        raise ProtocolError(str(exc)) from exc
    return True


def _portable_rmtree_entry(path, root, *, expected_identity):
    path = _rooted_path(path, root)
    current = os.lstat(path)
    expected = _normalize_expected_identity(expected_identity)
    if (
        _is_link_or_reparse(current)
        or not stat.S_ISDIR(current.st_mode)
        or (int(current.st_dev), int(current.st_ino)) != expected[:2]
    ):
        raise ProtocolError("managed tree contains an unsafe directory")
    entries = list(os.scandir(path))
    after_scan = os.lstat(path)
    if (
        not stat.S_ISDIR(after_scan.st_mode)
        or (int(after_scan.st_dev), int(after_scan.st_ino)) != expected[:2]
    ):
        raise ProtocolError("managed tree directory changed during removal")
    for entry in entries:
        info = entry.stat(follow_symlinks=False)
        if _is_link_or_reparse(info):
            raise ProtocolError("managed tree contains an unsafe link")
        if stat.S_ISREG(info.st_mode):
            if info.st_nlink != 1:
                raise ProtocolError(
                    "managed tree file has unexpected hard links"
                )
            portable_unlink_regular(
                entry.path,
                root=root,
                expected_identity=info,
            )
        elif stat.S_ISDIR(info.st_mode):
            _portable_rmtree_entry(
                Path(entry.path),
                root,
                expected_identity=info,
            )
        else:
            raise ProtocolError("managed tree contains an unsafe entry")
    final_info = os.lstat(path)
    if (
        not stat.S_ISDIR(final_info.st_mode)
        or (int(final_info.st_dev), int(final_info.st_ino)) != expected[:2]
    ):
        raise ProtocolError("managed tree directory changed during removal")
    portable_rmdir_empty(
        path,
        root=root,
        expected_identity=final_info,
    )


def _normalize_expected_identity(value):
    if value is None:
        return None
    if hasattr(value, "st_dev") and hasattr(value, "st_ino"):
        return (
            int(value.st_dev),
            int(value.st_ino),
            int(getattr(value, "st_mode", 0)),
            int(getattr(value, "st_size", 0)),
        )
    if (
        not isinstance(value, (list, tuple))
        or len(value) < 2
        or any(isinstance(item, bool) or not isinstance(item, int) for item in value)
    ):
        raise ProtocolError("expected path identity is invalid")
    return tuple(int(item) for item in value)


def _windows_delete_by_handle(
    path,
    *,
    root,
    expected_identity,
    expect_directory,
):
    """Pin and delete a Windows file or empty directory by its open handle."""
    _assert_supported_windows_atomic_filesystem()
    import ctypes
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = (
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        )

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = (
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", FILETIME),
            ("ftLastAccessTime", FILETIME),
            ("ftLastWriteTime", FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        )

    class FILE_BASIC_INFO(ctypes.Structure):
        _fields_ = (
            ("CreationTime", ctypes.c_longlong),
            ("LastAccessTime", ctypes.c_longlong),
            ("LastWriteTime", ctypes.c_longlong),
            ("ChangeTime", ctypes.c_longlong),
            ("FileAttributes", wintypes.DWORD),
        )

    class FILE_DISPOSITION_INFO(ctypes.Structure):
        _fields_ = (("DeleteFile", ctypes.c_ubyte),)

    class FILE_DISPOSITION_INFO_EX(ctypes.Structure):
        _fields_ = (("Flags", wintypes.DWORD),)

    delete_access = 0x00010000
    file_read_attributes = 0x00000080
    file_write_attributes = 0x00000100
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    open_reparse = 0x00200000
    backup_semantics = 0x02000000
    file_attribute_directory = 0x00000010
    file_attribute_normal = 0x00000080
    file_attribute_readonly = 0x00000001
    file_attribute_reparse = 0x00000400
    file_basic_info = 0
    file_disposition_info = 4
    file_disposition_info_ex = 21
    disposition_delete = 0x00000001
    disposition_posix = 0x00000002
    disposition_ignore_readonly = 0x00000010

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(BY_HANDLE_FILE_INFORMATION),
    )
    get_information.restype = wintypes.BOOL
    get_information_ex = kernel32.GetFileInformationByHandleEx
    get_information_ex.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_information_ex.restype = wintypes.BOOL
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_information.restype = wintypes.BOOL
    final_path_name = kernel32.GetFinalPathNameByHandleW
    final_path_name.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    final_path_name.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        os.fspath(path),
        delete_access | file_read_attributes | file_write_attributes,
        share_all,
        None,
        open_existing,
        open_reparse | backup_semantics,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            return False
        raise ProtocolError(str(ctypes.WinError(error)))

    try:
        information = BY_HANDLE_FILE_INFORMATION()
        if not get_information(handle, ctypes.byref(information)):
            raise ProtocolError(str(ctypes.WinError(ctypes.get_last_error())))
        attributes = int(information.dwFileAttributes)
        is_directory = bool(attributes & file_attribute_directory)
        if attributes & file_attribute_reparse:
            raise ProtocolError("delete destination is a reparse point")
        if is_directory != bool(expect_directory):
            expected_kind = "directory" if expect_directory else "regular file"
            raise ProtocolError(f"delete destination is not a {expected_kind}")
        if not expect_directory and int(information.nNumberOfLinks) != 1:
            raise ProtocolError("regular file has unexpected hard links")

        _assert_windows_handle_under_root(
            handle,
            root,
            final_path_name,
            ctypes,
            wintypes,
        )
        file_index = (
            int(information.nFileIndexHigh) << 32
        ) | int(information.nFileIndexLow)
        file_size = (
            int(information.nFileSizeHigh) << 32
        ) | int(information.nFileSizeLow)
        if expected_identity is not None:
            if int(expected_identity[1]) != file_index:
                raise ProtocolError("delete destination changed")
            if (
                not expect_directory
                and len(expected_identity) >= 4
                and int(expected_identity[3]) != file_size
            ):
                raise ProtocolError("delete destination changed")

        disposition_ex = FILE_DISPOSITION_INFO_EX(
            disposition_delete
            | disposition_posix
            | disposition_ignore_readonly
        )
        if not set_information(
            handle,
            file_disposition_info_ex,
            ctypes.byref(disposition_ex),
            ctypes.sizeof(disposition_ex),
        ):
            basic = FILE_BASIC_INFO()
            if not get_information_ex(
                handle,
                file_basic_info,
                ctypes.byref(basic),
                ctypes.sizeof(basic),
            ):
                raise ProtocolError(str(ctypes.WinError(ctypes.get_last_error())))
            original_attributes = int(basic.FileAttributes)
            basic.FileAttributes = (
                original_attributes & ~file_attribute_readonly
            ) or file_attribute_normal
            if not set_information(
                handle,
                file_basic_info,
                ctypes.byref(basic),
                ctypes.sizeof(basic),
            ):
                raise ProtocolError(str(ctypes.WinError(ctypes.get_last_error())))
            disposition = FILE_DISPOSITION_INFO(1)
            if not set_information(
                handle,
                file_disposition_info,
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            ):
                delete_error = ctypes.get_last_error()
                basic.FileAttributes = (
                    original_attributes or file_attribute_normal
                )
                if not set_information(
                    handle,
                    file_basic_info,
                    ctypes.byref(basic),
                    ctypes.sizeof(basic),
                ):
                    raise ProtocolError(
                        "delete failed and original attributes could not be "
                        "restored: "
                        + str(ctypes.WinError(ctypes.get_last_error()))
                    )
                raise ProtocolError(str(ctypes.WinError(delete_error)))
    finally:
        close_handle(handle)
    return True


def _assert_windows_handle_under_root(
    handle,
    root,
    final_path_name,
    ctypes,
    wintypes,
):
    required = final_path_name(handle, None, 0, 0)
    if required == 0:
        raise ProtocolError(str(ctypes.WinError(ctypes.get_last_error())))
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = final_path_name(handle, buffer, len(buffer), 0)
    if written == 0 or written >= len(buffer):
        raise ProtocolError(str(ctypes.WinError(ctypes.get_last_error())))
    resolved = buffer.value
    if resolved.startswith("\\\\?\\UNC\\"):
        resolved = "\\\\" + resolved[8:]
    elif resolved.startswith("\\\\?\\"):
        resolved = resolved[4:]
    root_path = os.path.normcase(os.path.realpath(os.fspath(root)))
    target_path = os.path.normcase(os.path.abspath(resolved))
    try:
        if os.path.commonpath([root_path, target_path]) != root_path:
            raise ProtocolError("handle resolves outside trusted root")
    except ValueError as exc:
        raise ProtocolError("handle resolves outside trusted root") from exc


def write_immutable(path, data, *, root, mode=0o600):
    path = _rooted_path(path, root)
    if os.path.lexists(path):
        existing = read_bounded_regular_file(
            path,
            max_bytes=max(len(data), 1),
            root=root,
        )
        if existing != bytes(data):
            raise ProtocolError(f"immutable file conflicts with existing bytes: {path}")
        return False
    portable_atomic_write(path, data, root=root, mode=mode)
    return True


def read_bounded_regular_file(path, *, max_bytes, root):
    data, _identity = _read_bounded_regular_file(
        path,
        max_bytes=max_bytes,
        root=root,
    )
    return data


def read_bounded_regular_file_with_identity(path, *, max_bytes, root):
    """Read bounded bytes and identity from one pinned verified handle."""
    return _read_bounded_regular_file(
        path,
        max_bytes=max_bytes,
        root=root,
    )


def _read_bounded_regular_file(path, *, max_bytes, root):
    path = _rooted_path(path, root)
    if os.path.abspath(path) == os.path.abspath(
        os.path.expanduser(os.fspath(root))
    ):
        raise ProtocolError(f"path is not a regular file: {path}")
    descriptor = None
    try:
        if os.name != "nt":
            descriptor = secure_open_file(
                path,
                os.O_RDONLY,
                root=root,
            )
        else:
            descriptor = _windows_open_regular_for_read(path, root=root)
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode):
            raise ProtocolError(f"path is not a regular file: {path}")
        if current.st_nlink != 1:
            raise ProtocolError(
                f"regular file has unexpected hard links: {path}"
            )
        if current.st_size > int(max_bytes):
            raise ProtocolError(f"file exceeds size limit: {path}")
        identity = (int(current.st_dev), int(current.st_ino))
        chunks = []
        remaining = int(max_bytes) + 1
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > int(max_bytes):
            raise ProtocolError(f"file exceeds size limit: {path}")
        return data, identity
    except FileNotFoundError:
        raise
    except ProtocolError:
        raise
    except (OSError, ValueError) as exc:
        raise ProtocolError(str(exc)) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _windows_open_regular_for_read(path, *, root):
    """Open one non-reparse regular file and transfer its pinned handle."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = (
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        )

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = (
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", FILETIME),
            ("ftLastAccessTime", FILETIME),
            ("ftLastWriteTime", FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        )

    generic_read = 0x80000000
    file_read_attributes = 0x00000080
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_existing = 3
    open_reparse = 0x00200000
    backup_semantics = 0x02000000
    file_attribute_directory = 0x00000010
    file_attribute_reparse = 0x00000400

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(BY_HANDLE_FILE_INFORMATION),
    )
    get_information.restype = wintypes.BOOL
    final_path_name = kernel32.GetFinalPathNameByHandleW
    final_path_name.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    final_path_name.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        os.fspath(path),
        generic_read | file_read_attributes,
        share_all,
        None,
        open_existing,
        open_reparse | backup_semantics,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        if error in {2, 3}:
            raise FileNotFoundError(os.fspath(path))
        raise ProtocolError(str(ctypes.WinError(error)))
    transferred = False
    try:
        information = BY_HANDLE_FILE_INFORMATION()
        if not get_information(handle, ctypes.byref(information)):
            raise ProtocolError(str(ctypes.WinError(ctypes.get_last_error())))
        attributes = int(information.dwFileAttributes)
        if attributes & (file_attribute_directory | file_attribute_reparse):
            raise ProtocolError(f"path is not a regular file: {path}")
        if int(information.nNumberOfLinks) != 1:
            raise ProtocolError(
                f"regular file has unexpected hard links: {path}"
            )
        _assert_windows_handle_under_root(
            handle,
            root,
            final_path_name,
            ctypes,
            wintypes,
        )
        descriptor = msvcrt.open_osfhandle(
            int(handle),
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        transferred = True
        return descriptor
    finally:
        if not transferred:
            close_handle(handle)


def _windows_open_lock_file(path, *, root):
    """Open or create one non-reparse lock file under its trusted root."""
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FILETIME(ctypes.Structure):
        _fields_ = (
            ("dwLowDateTime", wintypes.DWORD),
            ("dwHighDateTime", wintypes.DWORD),
        )

    class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
        _fields_ = (
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", FILETIME),
            ("ftLastAccessTime", FILETIME),
            ("ftLastWriteTime", FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        )

    generic_read = 0x80000000
    generic_write = 0x40000000
    file_read_attributes = 0x00000080
    share_all = 0x00000001 | 0x00000002 | 0x00000004
    open_always = 4
    file_attribute_normal = 0x00000080
    open_reparse = 0x00200000
    file_attribute_directory = 0x00000010
    file_attribute_reparse = 0x00000400

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(BY_HANDLE_FILE_INFORMATION),
    )
    get_information.restype = wintypes.BOOL
    final_path_name = kernel32.GetFinalPathNameByHandleW
    final_path_name.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    final_path_name.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        os.fspath(path),
        generic_read | generic_write | file_read_attributes,
        share_all,
        None,
        open_always,
        file_attribute_normal | open_reparse,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ProtocolError(str(ctypes.WinError(ctypes.get_last_error())))
    transferred = False
    try:
        information = BY_HANDLE_FILE_INFORMATION()
        if not get_information(handle, ctypes.byref(information)):
            raise ProtocolError(str(ctypes.WinError(ctypes.get_last_error())))
        attributes = int(information.dwFileAttributes)
        if attributes & (file_attribute_directory | file_attribute_reparse):
            raise ProtocolError("lock path is not a regular file")
        if int(information.nNumberOfLinks) != 1:
            raise ProtocolError("lock file has unexpected hard links")
        _assert_windows_handle_under_root(
            handle,
            root,
            final_path_name,
            ctypes,
            wintypes,
        )
        descriptor = msvcrt.open_osfhandle(
            int(handle),
            os.O_RDWR | getattr(os, "O_BINARY", 0),
        )
        transferred = True
        return descriptor
    finally:
        if not transferred:
            close_handle(handle)


@contextmanager
def portable_file_lock(path, *, root, blocking=True):
    """Serialize producer/materializer state across local threads and processes."""
    path = _rooted_path(path, root)
    _ensure_safe_directories(path.parent, Path(root))
    if os.name != "nt" and blocking:
        lock_context = secure_exclusive_file_lock(path, root=root)
        try:
            lock_context.__enter__()
        except (OSError, ValueError) as exc:
            raise ProtocolError(str(exc)) from exc
        try:
            yield
        finally:
            lock_context.__exit__(None, None, None)
        return
    key = os.path.normcase(os.path.abspath(path))
    with _LOCKS_GUARD:
        thread_lock = _LOCKS.setdefault(key, threading.RLock())
    with thread_lock:
        if os.name == "nt":
            descriptor = _windows_open_lock_file(path, root=root)
            file_context = os.fdopen(descriptor, "r+b")
        else:
            file_context = open(path, "a+b")
        with file_context as handle:
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                try:
                    msvcrt.locking(handle.fileno(), mode, 1)
                except OSError as exc:
                    raise BlockingIOError("portable lock is already held") from exc
                try:
                    yield
                finally:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                flags = fcntl.LOCK_EX
                if not blocking:
                    flags |= fcntl.LOCK_NB
                try:
                    fcntl.flock(handle.fileno(), flags)
                except (BlockingIOError, OSError) as exc:
                    raise BlockingIOError("portable lock is already held") from exc
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _rooted_path(path, root):
    root_path = os.path.abspath(os.path.expanduser(os.fspath(root)))
    candidate = os.path.abspath(os.path.expanduser(os.fspath(path)))
    try:
        if os.path.commonpath([root_path, candidate]) != root_path:
            raise ProtocolError("path is outside the trusted root")
    except ValueError as exc:
        raise ProtocolError("path is outside the trusted root") from exc
    _reject_existing_link_components(candidate, root_path)
    real_root = os.path.realpath(root_path)
    resolved_anchor = candidate if os.path.lexists(candidate) else os.path.dirname(candidate)
    real_parent = os.path.realpath(resolved_anchor)
    try:
        if os.path.commonpath([real_root, real_parent]) != real_root:
            raise ProtocolError("path resolves outside the trusted root")
    except ValueError as exc:
        raise ProtocolError("path resolves outside the trusted root") from exc
    return Path(candidate)


def _ensure_safe_directories(parent, root):
    root = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))
    parent = _rooted_path(parent, root)
    root.mkdir(parents=True, exist_ok=True)
    current = root
    relative = os.path.relpath(parent, root)
    parts = [] if relative == "." else relative.split(os.sep)
    for part in parts:
        current = current / part
        if os.path.lexists(current):
            info = os.lstat(current)
            if _is_link_or_reparse(info) or not stat.S_ISDIR(info.st_mode):
                raise ProtocolError("trusted path contains a symlink or non-directory")
        else:
            current.mkdir(mode=0o700)


def _reject_existing_link_components(candidate, root):
    root = Path(root)
    candidate = Path(candidate)
    if os.path.lexists(root):
        root_info = os.lstat(root)
        if _is_link_or_reparse(root_info) or not stat.S_ISDIR(root_info.st_mode):
            raise ProtocolError("trusted root is a symlink or non-directory")
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ProtocolError("path is outside the trusted root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if not os.path.lexists(current):
            break
        info = os.lstat(current)
        if _is_link_or_reparse(info):
            raise ProtocolError("trusted path contains a symlink or reparse point")


def _is_link_or_reparse(info):
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _fsync_directory(path):
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exact_mapping(value, fields, name):
    if not isinstance(value, dict):
        raise ProtocolError(f"{name} must be an object")
    unknown = set(value) - set(fields)
    missing = set(fields) - set(value)
    if unknown or missing:
        raise ProtocolError(f"{name} fields do not match schema")


def _safe_id(value, name, max_length=200):
    if not isinstance(value, str):
        raise ProtocolError(f"invalid {name}")
    text = value
    if len(text) > max_length or not SAFE_ID_RE.fullmatch(text):
        raise ProtocolError(f"invalid {name}")
    return text


def _valid_uuid(value, name):
    if not isinstance(value, str):
        raise ProtocolError(f"invalid {name} UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ProtocolError(f"invalid {name} UUID") from exc
    if str(parsed) != value:
        raise ProtocolError(f"{name} UUID is not canonical")
    return str(parsed)


def _valid_sha256(value, name):
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise ProtocolError(f"invalid {name}")
    return value


def _require_string(value, name):
    if not isinstance(value, str):
        raise ProtocolError(f"{name} must be a string")
    return value


def _require_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolError(f"{name} must be an integer")
    return value


def _valid_utc_timestamp(value, name):
    if not isinstance(value, str) or len(value) != 20:
        raise ProtocolError(f"invalid {name}")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ProtocolError(f"invalid {name}") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != value:
        raise ProtocolError(f"invalid {name}")
    return value


def _positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProtocolError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProtocolError(f"{name} must be a non-negative integer")
    return value


def _has_control(value):
    return any(ord(character) < 32 or ord(character) == 127 for character in str(value))
