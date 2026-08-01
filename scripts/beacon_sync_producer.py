"""Portable Codex/Claude transcript outbox producer for beacon-sync."""
from __future__ import annotations

import errno
import hashlib
import json
import mimetypes
import os
import re
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path

from beacon_sync_protocol import (
    PROTOCOL_RECEIPT,
    ProtocolError,
    build_event,
    build_ready,
    canonical_json_bytes,
    derive_attachment_reference_id,
    event_bundle_name,
    event_sequence_directory_name,
    is_event_sequence_directory_name,
    portable_atomic_write,
    portable_file_lock,
    portable_rmdir_empty,
    portable_rmtree,
    portable_unlink_regular,
    read_bounded_regular_file,
    sha256_bytes,
    validate_event,
    validate_legacy_attachment_event,
    validate_legacy_attachment_ready,
    validate_ready,
    write_immutable,
)
from transcript_utils import (
    iter_transcript_files,
    read_transcript_metadata,
)


PRODUCER_STATE_PROTOCOL = "agent-memory-beacon-sync-producer-state"
PRODUCER_IDENTITY_PROTOCOL = "agent-memory-beacon-sync-identity"
ATTACHMENT_CAPTURE_PROTOCOL = "agent-memory-beacon-sync-attachment-capture"
ATTACHMENT_CAPTURE_READY_PROTOCOL = (
    "agent-memory-beacon-sync-attachment-capture-ready"
)
DEFAULT_MAX_CHUNK_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_GAP_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024
DEFAULT_MAX_EVENTS_PER_RUN = 32
DEFAULT_GC_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_EVENTS_PER_RUN_HARD_LIMIT = 4096
MAX_TRANSCRIPT_DISCOVERY_ITEMS_PER_RUN = 256
MAX_GC_BUNDLES_PER_RUN = 256
MAX_ATTACHMENT_REFERENCES_PER_TRANSCRIPT_EVENT = 32
MAX_ATTACHMENT_CAPTURE_JOURNALS = 4096
PRODUCER_STATE_SCHEMA_VERSION = 3
PRODUCER_PROGRESS_PROTOCOL = "agent-memory-beacon-sync-producer-progress"
SAFE_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SAFE_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")
ATTACHMENT_QUEUE_FIELDS = frozenset(
    {
        "reference_id",
        "original_name",
        "sha256",
        "bytes",
        "media_type",
        "source_locator_sha256",
        "reference_kind",
        "source_cursor",
        "agent",
        "session_id",
        "stream_epoch",
        "metadata",
    }
)
LEGACY_ATTACHMENT_QUEUE_FIELDS = frozenset(
    {
        "attachment_id",
        "original_name",
        "sha256",
        "bytes",
        "media_type",
        "source_locator_sha256",
        "reference_kind",
        "transcript_cursor",
        "agent",
        "session_id",
        "stream_epoch",
        "metadata",
    }
)
FORBIDDEN_ATTACHMENT_NAMES = frozenset(
    {
        ".env",
        "auth.json",
        "credential.json",
        "credentials.json",
        "secret.json",
        "secrets.json",
        "token.json",
        "tokens.json",
    }
)
FORBIDDEN_ATTACHMENT_SUFFIXES = frozenset(
    {".key", ".pem", ".p12", ".pfx", ".kdb", ".kdbx"}
)
LEGACY_ATTACHMENT_CAPTURE_FIELDS = frozenset(
    {"protocol", "schema_version", "capture_id", "item"}
)
ATTACHMENT_CAPTURE_FIELDS = frozenset(
    {"protocol", "schema_version", "capture_id", "item", "source_identity"}
)
ATTACHMENT_CAPTURE_SOURCE_FIELDS = frozenset(
    {"source_key", "file_identity"}
)
ATTACHMENT_CAPTURE_READY_FIELDS = frozenset(
    {
        "protocol",
        "schema_version",
        "capture_id",
        "capture_sha256",
        "payload_sha256",
        "payload_bytes",
    }
)


class ProducerError(RuntimeError):
    """A producer state or immutable source invariant was violated."""


class _AttachmentContentRejected(RuntimeError):
    """An attachment was invalid before capture persistence began."""


def load_producer_state(config):
    state_dir = _state_dir(config)
    path = state_dir / "producer-state.json"
    if not path.exists():
        return _new_state(config)
    try:
        data = json.loads(
            read_bounded_regular_file(
                path,
                max_bytes=16 * 1024 * 1024,
                root=state_dir,
            )
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ProtocolError) as exc:
        raise ProducerError(f"producer state is invalid: {path}") from exc
    data = _migrate_state(data, config=config)
    _validate_state(data)
    expected_device = str(config.get("device_id") or "").strip()
    if expected_device and data["device_id"] != expected_device:
        raise ProducerError("configured device_id conflicts with persisted producer state")
    return data


def initialize_producer(config, *, now=None):
    state_dir = _state_dir(config)
    outbox = _outbox_dir(config)
    state_dir.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    lock = state_dir / "producer.lock"
    with portable_file_lock(lock, root=state_dir):
        state = load_producer_state(config)
        _validate_existing_identity_device(config, state["device_id"])
        _write_state(config, state)
        _write_identity(config, state)
        if not state.get("pending_event"):
            _discard_pending_payload(config)
        _cleanup_attachment_cas(config, state)
    return {
        "device_id": state["device_id"],
        "producer_instance_id": state["producer_instance_id"],
        "state_path": str(state_dir / "producer-state.json"),
        "outbox_path": str(outbox),
    }


def collect_transcripts(
    config,
    *,
    include_existing=False,
    now=None,
    fault_point=None,
):
    """Emit bounded immutable JSONL source events from configured transcripts."""
    now = _utc_now(now)
    state_dir = _state_dir(config)
    outbox = _outbox_dir(config)
    state_dir.mkdir(parents=True, exist_ok=True)
    outbox.mkdir(parents=True, exist_ok=True)
    result = {
        "emitted": 0,
        "baselined": 0,
        "recovered": 0,
        "deferred_partial": 0,
        "attachments_queued": 0,
        "attachments_emitted": 0,
        "attachments_rejected": 0,
        "limited": False,
        "pending_discovery": 0,
    }
    with portable_file_lock(state_dir / "producer.lock", root=state_dir):
        state = load_producer_state(config)
        _write_identity(config, state)
        if state.get("pending_event"):
            pending_kind = state["pending_event"]["event"]["event_kind"]
            _publish_pending(config, state, fault_point=fault_point)
            result["recovered"] += 1
            if pending_kind == "attachment.blob":
                result["attachments_emitted"] += 1
        else:
            _discard_pending_payload(config)
        _cleanup_attachment_cas(config, state)

        max_events = min(
            _positive_config_int(
                config,
                "max_events_per_run",
                DEFAULT_MAX_EVENTS_PER_RUN,
            ),
            MAX_EVENTS_PER_RUN_HARD_LIMIT,
        )
        _drain_attachment_queue(
            config,
            state,
            result,
            now,
            max_events=max_events,
            fault_point=fault_point,
        )
        if result["emitted"] >= max_events:
            result["limited"] = True
            result["pending_discovery"] = 1
            return result

        progress = _load_producer_progress(config)
        paths, next_discovery_cursor, discovery_cursor_missing = _discover_transcripts(
            config,
            after=progress["discovery_cursor"],
            limit=MAX_TRANSCRIPT_DISCOVERY_ITEMS_PER_RUN,
        )
        discovery_limited = bool(next_discovery_cursor) or discovery_cursor_missing
        first_baseline = not state.get("baseline_initialized_at")
        if first_baseline and not include_existing:
            for path in paths:
                source_key, source = _source_descriptor(path)
                source["cursor"] = os.path.getsize(path)
                _update_source_anchor(source, path)
                state["sources"][source_key] = source
                result["baselined"] += 1
            progress["discovery_cursor"] = next_discovery_cursor
            if discovery_limited:
                result["limited"] = True
                result["pending_discovery"] = 1
            else:
                state["baseline_initialized_at"] = _iso_utc(now)
            _write_state(config, state)
            _write_producer_progress(config, progress)
            return result
        if first_baseline:
            state["baseline_initialized_at"] = _iso_utc(now)
            _write_state(config, state)

        for path_index, path in enumerate(paths):
            if result["emitted"] >= max_events:
                result["limited"] = True
                result["pending_discovery"] = 1
                progress["discovery_cursor"] = _discovery_path_key(path)
                _write_producer_progress(config, progress)
                break
            source_key, discovered = _source_descriptor(path)
            source = state["sources"].get(source_key)
            if source is None:
                source = discovered
                state["sources"][source_key] = source
                _write_state(config, state)
            else:
                if _refresh_source_identity(source, discovered, path):
                    _write_state(config, state)

            while result["emitted"] < max_events:
                size = os.path.getsize(path)
                if (
                    size < source["cursor"]
                    or _source_prefix(path, source["prefix_bytes"])
                    != source["prefix_sha256"]
                    or not _source_anchor_matches(path, source)
                ):
                    prefix_bytes = min(size, 4096)
                    source.update(
                        {
                            "stream_epoch": str(uuid.uuid4()),
                            "cursor": 0,
                            "prefix_bytes": prefix_bytes,
                            "prefix_sha256": _source_prefix(path, prefix_bytes),
                            "file_identity": _file_identity(path),
                            "anchor_bytes": 0,
                            "anchor_sha256": sha256_bytes(b""),
                        }
                    )
                    _write_state(config, state)
                segment = _next_segment(
                    path,
                    source["cursor"],
                    max_chunk_bytes=_positive_config_int(
                        config,
                        "max_chunk_bytes",
                        DEFAULT_MAX_CHUNK_BYTES,
                    ),
                    max_gap_bytes=_positive_config_int(
                        config,
                        "max_gap_bytes",
                        DEFAULT_MAX_GAP_BYTES,
                    ),
                )
                if segment is None:
                    if size > source["cursor"]:
                        result["deferred_partial"] += 1
                    break
                attachments, rejected = _capture_segment_attachments(
                    config,
                    state,
                    source,
                    segment,
                    fault_point=fault_point,
                )
                result["attachments_rejected"] += rejected
                result["attachments_queued"] += len(attachments)
                event, payload_bytes = _allocate_event(
                    config,
                    state,
                    source_key,
                    source,
                    segment,
                    now,
                    attachments=attachments,
                )
                _stage_pending_payload(config, event, payload_bytes)
                _write_state(config, state)
                if fault_point == "after_allocation_state":
                    raise ProducerError("injected failure after_allocation_state")
                _publish_pending(
                    config,
                    state,
                    payload_bytes=payload_bytes,
                    fault_point=fault_point,
                )
                result["emitted"] += 1
                source = state["sources"][source_key]
                _drain_attachment_queue(
                    config,
                    state,
                    result,
                    now,
                    max_events=max_events,
                    fault_point=fault_point,
                )
            if result["emitted"] >= max_events:
                result["limited"] = True
                source_key = _source_descriptor(path)[0]
                source = state["sources"].get(source_key, {})
                if os.path.getsize(path) <= int(source.get("cursor") or 0):
                    if path_index < len(paths) - 1:
                        progress["discovery_cursor"] = _discovery_path_key(
                            paths[path_index + 1]
                        )
                    else:
                        progress["discovery_cursor"] = next_discovery_cursor
                else:
                    progress["discovery_cursor"] = _discovery_path_key(path)
                result["pending_discovery"] = 1
                _write_producer_progress(config, progress)
                break
        else:
            progress["discovery_cursor"] = next_discovery_cursor
            _write_producer_progress(config, progress)
        if discovery_limited:
            result["limited"] = True
            result["pending_discovery"] = 1
        return result


def garbage_collect_outbox(config, *, now=None):
    """Delete immutable bundles only after a matching GC-authorizing receipt."""
    now = _utc_now(now)
    outbox = _outbox_dir(config)
    state_dir = _state_dir(config)
    receipt_root = (
        Path(
            os.path.abspath(
                os.path.expanduser(str(config.get("received_published_dir") or ""))
            )
        )
        / "v1"
        / "receipts"
    )
    result = {
        "examined": 0,
        "removed": 0,
        "denied": 0,
        "limited": False,
        "pending": 0,
    }
    if not receipt_root.is_dir() or not outbox.is_dir():
        return result
    retention = _nonnegative_config_int(
        config,
        "gc_retention_seconds",
        DEFAULT_GC_RETENTION_SECONDS,
    )
    verified_generations = set()
    with portable_file_lock(state_dir / "producer.lock", root=state_dir):
        progress = _load_producer_progress(config)
        limit = int(MAX_GC_BUNDLES_PER_RUN)
        if limit <= 0:
            raise ProducerError("outbox GC limit is invalid")
        selected, next_cursor, cursor_missing = _continuation_page(
            _iter_outbox_ready_paths(outbox),
            progress["gc_cursor"],
            limit,
            key=lambda path: _gc_path_key(path, outbox),
        )
        result["pending"] = int(bool(next_cursor) or cursor_missing)
        result["limited"] = bool(result["pending"])
        for ready_path in selected:
            bundle = ready_path.parent
            result["examined"] += 1
            try:
                event_bytes = read_bounded_regular_file(
                    bundle / "event.json",
                    max_bytes=128 * 1024,
                    root=outbox,
                )
                event = json.loads(event_bytes)
                legacy_attachment = _is_legacy_attachment_event(event)
                if legacy_attachment:
                    validate_legacy_attachment_event(event)
                else:
                    validate_event(event)
                ready_bytes = read_bounded_regular_file(
                    ready_path,
                    max_bytes=16 * 1024,
                    root=outbox,
                )
                ready = json.loads(ready_bytes)
                if canonical_json_bytes(ready) != ready_bytes:
                    raise ProducerError("ready marker is not canonical")
                if legacy_attachment:
                    validate_legacy_attachment_ready(
                        ready,
                        event,
                        event_bytes,
                    )
                else:
                    validate_ready(ready, event, event_bytes)
                receipt_path = (
                    receipt_root
                    / event["producer_instance_id"]
                    / f"{event['seq']:020d}-{event['event_id']}.json"
                )
                if not receipt_path.is_file():
                    result["denied"] += 1
                    continue
                receipt_bytes = read_bounded_regular_file(
                    receipt_path,
                    max_bytes=64 * 1024,
                    root=receipt_root,
                )
                receipt = json.loads(receipt_bytes)
                if canonical_json_bytes(receipt) != receipt_bytes:
                    raise ProducerError("receipt is not canonical")
                _validate_gc_receipt(receipt, event, event_bytes)
                generation_key = (
                    receipt["canonical_generation"],
                    receipt["generation_id"],
                )
                if generation_key not in verified_generations:
                    _validate_gc_generation(config, receipt)
                    verified_generations.add(generation_key)
                created = _parse_utc(event["created_at"])
                if (now - created).total_seconds() < retention:
                    result["denied"] += 1
                    continue
                _remove_bundle(bundle, outbox)
                result["removed"] += 1
            except (
                FileNotFoundError,
                json.JSONDecodeError,
                ProtocolError,
                ProducerError,
                ValueError,
            ):
                result["denied"] += 1
        progress["gc_cursor"] = next_cursor
        _write_producer_progress(config, progress)
    return result


def _new_state(config):
    device_id = str(config.get("device_id") or "").strip()
    if not device_id:
        raise ProducerError("beacon_sync.device_id is required for a producer")
    if not re_safe_device_id(device_id):
        raise ProducerError("beacon_sync.device_id is invalid")
    return {
        "protocol": PRODUCER_STATE_PROTOCOL,
        "schema_version": PRODUCER_STATE_SCHEMA_VERSION,
        "device_id": device_id,
        "producer_instance_id": str(uuid.uuid4()),
        "next_seq": 1,
        "baseline_initialized_at": None,
        "sources": {},
        "attachment_queue": [],
        "pending_event": None,
    }


def _validate_state(state):
    required = {
        "protocol",
        "schema_version",
        "device_id",
        "producer_instance_id",
        "next_seq",
        "baseline_initialized_at",
        "sources",
        "attachment_queue",
        "pending_event",
    }
    if not isinstance(state, dict) or set(state) != required:
        raise ProducerError("producer state fields do not match schema")
    if (
        state["protocol"] != PRODUCER_STATE_PROTOCOL
        or state["schema_version"] != PRODUCER_STATE_SCHEMA_VERSION
    ):
        raise ProducerError("unsupported producer state schema")
    if not re_safe_device_id(state["device_id"]):
        raise ProducerError("producer state device ID is invalid")
    if not _is_canonical_uuid(state["producer_instance_id"]):
        raise ProducerError("producer instance ID is invalid")
    if (
        isinstance(state["next_seq"], bool)
        or not isinstance(state["next_seq"], int)
        or state["next_seq"] <= 0
    ):
        raise ProducerError("producer next sequence is invalid")
    if not isinstance(state["sources"], dict):
        raise ProducerError("producer sources state is invalid")
    if not isinstance(state["attachment_queue"], list):
        raise ProducerError("producer attachment queue is invalid")
    for item in state["attachment_queue"]:
        _validate_attachment_queue_item(
            item,
            producer_instance_id=state["producer_instance_id"],
        )
    _validate_pending_state(state)


def _migrate_state(state, *, config=None):
    if not isinstance(state, dict):
        raise ProducerError("producer state fields do not match schema")
    if state.get("protocol") != PRODUCER_STATE_PROTOCOL:
        return state
    schema_version = state.get("schema_version")
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise ProducerError("unsupported producer state schema")
    if schema_version == PRODUCER_STATE_SCHEMA_VERSION:
        return state
    if schema_version == 1:
        migrated = dict(state)
        migrated["schema_version"] = PRODUCER_STATE_SCHEMA_VERSION
        migrated["attachment_queue"] = []
        pending = migrated.get("pending_event")
        if isinstance(pending, dict):
            pending = dict(pending)
            pending.setdefault("pending_type", "transcript")
            pending.setdefault("attachments", [])
            if pending["pending_type"] != "transcript":
                raise ProducerError("version one pending event is not a transcript")
            migrated["pending_event"] = pending
        return migrated
    if schema_version == 2:
        producer_instance_id = state.get("producer_instance_id")
        migrated = dict(state)
        old_queue = state.get("attachment_queue")
        if not isinstance(old_queue, list):
            raise ProducerError("version two attachment queue is invalid")
        new_queue = [
            _migrate_v2_attachment_queue_item(
                item,
                producer_instance_id=producer_instance_id,
            )
            for item in old_queue
        ]
        migrated["schema_version"] = PRODUCER_STATE_SCHEMA_VERSION
        migrated["attachment_queue"] = new_queue
        pending = state.get("pending_event")
        if pending is None:
            return migrated
        if not isinstance(pending, dict):
            raise ProducerError("version two pending event is invalid")
        pending = dict(pending)
        pending_type = pending.get("pending_type") or "transcript"
        pending["pending_type"] = pending_type
        if pending_type == "transcript":
            attachments = pending.get("attachments") or []
            if not isinstance(attachments, list):
                raise ProducerError(
                    "version two pending transcript attachments are invalid"
                )
            pending["attachments"] = [
                _migrate_v2_attachment_queue_item(
                    item,
                    producer_instance_id=producer_instance_id,
                )
                for item in attachments
            ]
            migrated["pending_event"] = pending
            return migrated
        if pending_type != "attachment":
            raise ProducerError("version two pending event type is invalid")
        event = pending.get("event")
        try:
            validate_legacy_attachment_event(event)
        except ProtocolError as exc:
            raise ProducerError(
                "version two pending attachment is not a valid v1 event"
            ) from exc
        if (
            not old_queue
            or old_queue[0].get("attachment_id") != pending.get("queue_id")
        ):
            raise ProducerError(
                "version two pending attachment does not match its queue"
            )
        pending["queue_id"] = new_queue[0]["reference_id"]
        _validate_pending_attachment_binding(
            event,
            new_queue[0],
            legacy_attachment=True,
        )
        if config is None:
            raise ProducerError(
                "producer config is required to migrate a pending attachment"
            )
        outbox = _outbox_dir(config)
        bundle = (
            outbox
            / "v1"
            / "events"
            / event["producer_instance_id"]
            / _legacy_event_bundle_name(event)
        )
        if _durable_pending_bundle(
            bundle,
            outbox,
            event,
            legacy_attachment=True,
        ):
            migrated["pending_event"] = pending
        else:
            migrated["pending_event"] = None
        return migrated
    return state


def _validate_attachment_queue_item(item, *, producer_instance_id=None):
    if not isinstance(item, dict) or set(item) != ATTACHMENT_QUEUE_FIELDS:
        raise ProducerError("producer attachment queue item fields are invalid")
    for key in (
        "reference_id",
        "original_name",
        "sha256",
        "media_type",
        "source_locator_sha256",
        "reference_kind",
        "agent",
        "session_id",
        "stream_epoch",
    ):
        if not isinstance(item[key], str) or not item[key]:
            raise ProducerError("producer attachment queue item is invalid")
    if (
        isinstance(item["bytes"], bool)
        or not isinstance(item["bytes"], int)
        or item["bytes"] <= 0
    ):
        raise ProducerError("producer attachment queue size is invalid")
    cursor = item["source_cursor"]
    if (
        not isinstance(cursor, dict)
        or set(cursor) != {"start", "end"}
        or isinstance(cursor["start"], bool)
        or isinstance(cursor["end"], bool)
        or not isinstance(cursor["start"], int)
        or not isinstance(cursor["end"], int)
        or cursor["start"] < 0
        or cursor["end"] <= cursor["start"]
    ):
        raise ProducerError("producer attachment source cursor is invalid")
    if not isinstance(item["metadata"], dict):
        raise ProducerError("producer attachment metadata is invalid")
    if producer_instance_id is not None:
        stream_id = "stream-" + sha256_bytes(
            f"{item['agent']}\0{item['session_id']}".encode("utf-8")
        )
        try:
            expected_id = derive_attachment_reference_id(
                producer_instance_id=producer_instance_id,
                stream_id=stream_id,
                stream_epoch=item["stream_epoch"],
                source_cursor=cursor,
                source_locator_sha256=item["source_locator_sha256"],
                original_name=item["original_name"],
                payload_sha256=item["sha256"],
            )
        except ProtocolError as exc:
            raise ProducerError(
                "producer attachment reference identity is invalid"
            ) from exc
        if item["reference_id"] != expected_id:
            raise ProducerError(
                "producer attachment reference ID does not match identity"
            )


def _validate_pending_state(state):
    pending = state["pending_event"]
    if pending is None:
        return
    if not isinstance(pending, dict):
        raise ProducerError("producer pending event is invalid")
    pending_type = pending.get("pending_type") or "transcript"
    event = pending.get("event")
    try:
        if _is_legacy_attachment_event(event):
            validate_legacy_attachment_event(event)
        else:
            validate_event(event)
    except ProtocolError as exc:
        raise ProducerError("producer pending event protocol is invalid") from exc
    if event["seq"] != state["next_seq"]:
        raise ProducerError("producer pending event sequence is invalid")
    if pending_type == "transcript":
        if event["event_kind"] not in {"transcript.chunk", "transcript.gap"}:
            raise ProducerError("producer pending transcript kind is invalid")
        attachments = pending.get("attachments") or []
        if not isinstance(attachments, list):
            raise ProducerError("producer pending attachments are invalid")
        for item in attachments:
            _validate_attachment_queue_item(
                item,
                producer_instance_id=state["producer_instance_id"],
            )
            if (
                item["agent"] != event["agent"]
                or item["session_id"] != event["session_id"]
                or item["stream_epoch"] != event["stream_epoch"]
                or item["metadata"] != event["metadata"]
                or item["source_cursor"]["start"]
                < event["source_cursor"]["start"]
                or item["source_cursor"]["end"] > event["source_cursor"]["end"]
            ):
                raise ProducerError(
                    "producer pending transcript attachment is out of scope"
                )
        return
    if pending_type == "attachment":
        if event["event_kind"] != "attachment.blob":
            raise ProducerError("producer pending attachment kind is invalid")
        if (
            not state["attachment_queue"]
            or state["attachment_queue"][0]["reference_id"]
            != pending.get("queue_id")
        ):
            raise ProducerError("producer pending attachment does not match queue")
        _validate_pending_attachment_binding(
            event,
            state["attachment_queue"][0],
            legacy_attachment=_is_legacy_attachment_event(event),
        )
        return
    raise ProducerError("producer pending event type is invalid")


def _validate_pending_attachment_binding(
    event,
    item,
    *,
    legacy_attachment,
):
    attachment = event["extensions"]["attachment"]
    event_cursor = (
        attachment["transcript_cursor"]
        if legacy_attachment
        else event["source_cursor"]
    )
    if legacy_attachment:
        legacy_identity = {
            "original_name": item["original_name"],
            "payload_sha256": item["sha256"],
            "source_locator_sha256": item["source_locator_sha256"],
        }
        expected_reference = "attachment-" + sha256_bytes(
            canonical_json_bytes(legacy_identity)
        )
        actual_reference = attachment["attachment_id"]
    else:
        expected_reference = item["reference_id"]
        actual_reference = attachment["reference_id"]
    if (
        actual_reference != expected_reference
        or attachment["original_name"] != item["original_name"]
        or attachment["source_locator_sha256"]
        != item["source_locator_sha256"]
        or attachment["reference_kind"] != item["reference_kind"]
        or event_cursor != item["source_cursor"]
        or event["payload"]["sha256"] != item["sha256"]
        or event["payload"]["bytes"] != item["bytes"]
        or event["payload"]["media_type"] != item["media_type"]
        or event["agent"] != item["agent"]
        or event["session_id"] != item["session_id"]
        or event["stream_epoch"] != item["stream_epoch"]
        or event["metadata"] != item["metadata"]
    ):
        raise ProducerError("producer pending attachment does not match queue item")


def _migrate_v2_attachment_queue_item(item, *, producer_instance_id):
    _validate_v2_attachment_queue_item(item)
    cursor = dict(item["transcript_cursor"])
    stream_id = "stream-" + sha256_bytes(
        f"{item['agent']}\0{item['session_id']}".encode("utf-8")
    )
    try:
        reference_id = derive_attachment_reference_id(
            producer_instance_id=producer_instance_id,
            stream_id=stream_id,
            stream_epoch=item["stream_epoch"],
            source_cursor=cursor,
            source_locator_sha256=item["source_locator_sha256"],
            original_name=item["original_name"],
            payload_sha256=item["sha256"],
        )
    except ProtocolError as exc:
        raise ProducerError(
            "version two attachment reference identity is invalid"
        ) from exc
    migrated = {
        key: value
        for key, value in item.items()
        if key not in {"attachment_id", "transcript_cursor"}
    }
    migrated["reference_id"] = reference_id
    migrated["source_cursor"] = cursor
    _validate_attachment_queue_item(
        migrated,
        producer_instance_id=producer_instance_id,
    )
    return migrated


def _validate_v2_attachment_queue_item(item):
    if not isinstance(item, dict) or set(item) != LEGACY_ATTACHMENT_QUEUE_FIELDS:
        raise ProducerError("version two attachment queue item fields are invalid")
    for key in (
        "attachment_id",
        "original_name",
        "sha256",
        "media_type",
        "source_locator_sha256",
        "reference_kind",
        "agent",
        "session_id",
        "stream_epoch",
    ):
        if not isinstance(item[key], str) or not item[key]:
            raise ProducerError("version two attachment queue item is invalid")
    if (
        isinstance(item["bytes"], bool)
        or not isinstance(item["bytes"], int)
        or item["bytes"] <= 0
    ):
        raise ProducerError("version two attachment queue size is invalid")
    cursor = item["transcript_cursor"]
    if (
        not isinstance(cursor, dict)
        or set(cursor) != {"start", "end"}
        or isinstance(cursor["start"], bool)
        or isinstance(cursor["end"], bool)
        or not isinstance(cursor["start"], int)
        or not isinstance(cursor["end"], int)
        or cursor["start"] < 0
        or cursor["end"] <= cursor["start"]
    ):
        raise ProducerError("version two attachment cursor is invalid")
    if not isinstance(item["metadata"], dict):
        raise ProducerError("version two attachment metadata is invalid")
    identity = {
        "original_name": item["original_name"],
        "payload_sha256": item["sha256"],
        "source_locator_sha256": item["source_locator_sha256"],
    }
    expected_id = "attachment-" + sha256_bytes(
        canonical_json_bytes(identity)
    )
    if item["attachment_id"] != expected_id:
        raise ProducerError("version two attachment ID does not match identity")


def re_safe_device_id(value):
    return bool(SAFE_DEVICE_ID_RE.fullmatch(str(value or "")))


def _is_canonical_uuid(value):
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, TypeError, AttributeError):
        return False


def _write_state(config, state):
    _validate_state(state)
    state_dir = _state_dir(config)
    portable_atomic_write(
        state_dir / "producer-state.json",
        canonical_json_bytes(state),
        root=state_dir,
    )


def _write_identity(config, state):
    outbox = _outbox_dir(config)
    identity = {
        "protocol": PRODUCER_IDENTITY_PROTOCOL,
        "schema_version": 1,
        "device_id": state["device_id"],
        "producer_instance_id": state["producer_instance_id"],
    }
    current_path = outbox / "v1" / "identity.json"
    if current_path.is_file():
        current = _read_identity(current_path, outbox)
        if current["device_id"] != state["device_id"]:
            raise ProducerError(
                "existing outbox identity belongs to another device"
            )
        _write_identity_registry_entry(outbox, current)
    _write_identity_registry_entry(outbox, identity)
    identity_bytes = canonical_json_bytes(identity)
    if current_path.is_file():
        current_bytes = read_bounded_regular_file(
            current_path,
            max_bytes=16 * 1024,
            root=outbox,
        )
        if current_bytes == identity_bytes:
            return
    portable_atomic_write(
        current_path,
        identity_bytes,
        root=outbox,
    )


def _validate_existing_identity_device(config, device_id):
    outbox = _outbox_dir(config)
    current_path = outbox / "v1" / "identity.json"
    if not current_path.is_file():
        return
    current = _read_identity(current_path, outbox)
    if current["device_id"] != device_id:
        raise ProducerError("existing outbox identity belongs to another device")


def _write_identity_registry_entry(outbox, identity):
    write_immutable(
        outbox
        / "v1"
        / "identities"
        / f"{identity['producer_instance_id']}.json",
        canonical_json_bytes(identity),
        root=outbox,
    )


def _read_identity(path, outbox):
    try:
        data = read_bounded_regular_file(
            path,
            max_bytes=16 * 1024,
            root=outbox,
        )
        identity = json.loads(data)
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        ProtocolError,
    ) as exc:
        raise ProducerError("existing outbox identity is invalid") from exc
    required = {
        "protocol",
        "schema_version",
        "device_id",
        "producer_instance_id",
    }
    if (
        not isinstance(identity, dict)
        or set(identity) != required
        or identity["protocol"] != PRODUCER_IDENTITY_PROTOCOL
        or identity["schema_version"] != 1
        or canonical_json_bytes(identity) != data
        or not re_safe_device_id(identity["device_id"])
    ):
        raise ProducerError("existing outbox identity is invalid")
    if not _is_canonical_uuid(identity["producer_instance_id"]):
        raise ProducerError("existing outbox identity is invalid")
    return identity


def _discover_transcripts(config, *, after, limit):
    if int(limit) <= 0:
        raise ProducerError("transcript discovery limit is invalid")
    roots = []
    roots.extend(config.get("transcript_paths") or [])
    for key in ("codex_sessions_path", "claude_project_path"):
        if config.get(key):
            roots.append(config[key])
    return _continuation_page(
        _iter_discovered_transcripts(roots),
        after,
        int(limit),
        key=_discovery_path_key,
    )


def _iter_discovered_transcripts(roots):
    for path in iter_transcript_files(roots):
        if str(path).lower().endswith(".jsonl") and os.path.isfile(path):
            yield Path(path)


def _discovery_path_key(path):
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(path))))


def _gc_path_key(path, outbox):
    return Path(path).relative_to(outbox).as_posix()


def _continuation_page(items, cursor, limit, *, key):
    selected = []
    started = not cursor
    for item in items:
        item_key = key(item)
        if not started:
            if item_key != cursor:
                continue
            started = True
        selected.append(item)
        if len(selected) > limit:
            break
    if cursor and not started:
        return [], "", True
    next_cursor = key(selected[limit]) if len(selected) > limit else ""
    return selected[:limit], next_cursor, False


def _iter_outbox_ready_paths(outbox):
    events_root = outbox / "v1" / "events"
    if not os.path.lexists(events_root):
        return
    root_info = os.lstat(events_root)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ProducerError("outbox events root is unsafe")
    with os.scandir(events_root) as producers:
        for producer in producers:
            producer_info = producer.stat(follow_symlinks=False)
            if (
                stat.S_ISLNK(producer_info.st_mode)
                or not stat.S_ISDIR(producer_info.st_mode)
            ):
                continue
            with os.scandir(producer.path) as entries:
                for entry in entries:
                    entry_info = entry.stat(follow_symlinks=False)
                    if (
                        stat.S_ISLNK(entry_info.st_mode)
                        or not stat.S_ISDIR(entry_info.st_mode)
                    ):
                        continue
                    if not is_event_sequence_directory_name(entry.name):
                        ready_path = Path(entry.path) / "ready.json"
                        if os.path.lexists(ready_path):
                            yield ready_path
                        continue
                    with os.scandir(entry.path) as bundles:
                        for bundle in bundles:
                            bundle_info = bundle.stat(follow_symlinks=False)
                            if (
                                stat.S_ISLNK(bundle_info.st_mode)
                                or not stat.S_ISDIR(bundle_info.st_mode)
                            ):
                                continue
                            ready_path = Path(bundle.path) / "ready.json"
                            if os.path.lexists(ready_path):
                                yield ready_path


def _capture_segment_attachments(
    config,
    state,
    source,
    segment,
    *,
    fault_point=None,
):
    if (
        segment["kind"] != "transcript.chunk"
        or not segment.get("data")
        or not (config.get("attachment_roots") or [])
    ):
        return [], 0
    references = _attachment_references(
        segment["data"],
        segment_start=segment["start"],
    )
    captured = []
    rejected = 0
    seen = set()
    for reference in references:
        if len(captured) >= MAX_ATTACHMENT_REFERENCES_PER_TRANSCRIPT_EVENT:
            rejected += 1
            continue
        try:
            descriptor = _attachment_reference_descriptor(
                config,
                state,
                source,
                reference,
            )
        except (ProtocolError, ProducerError, ValueError):
            rejected += 1
            continue
        try:
            (
                item,
                data,
                capture_id,
                source_identity,
                recovered,
            ) = _capture_attachment_reference(
                config,
                state,
                source,
                reference,
                descriptor=descriptor,
            )
        except _AttachmentContentRejected:
            rejected += 1
            continue
        if not recovered:
            _write_attachment_capture_intent(
                config,
                capture_id,
                item,
                data,
                source_identity,
            )
        write_immutable(
            _attachment_cas_path(config, item["sha256"]),
            data,
            root=_state_dir(config),
        )
        if not recovered:
            _write_attachment_capture_ready(
                config,
                capture_id,
                item,
            )
        if (
            not recovered
            and fault_point == "after_attachment_capture_ready"
        ):
            raise ProducerError(
                "injected failure after_attachment_capture_ready"
            )
        if item["reference_id"] in seen:
            continue
        seen.add(item["reference_id"])
        captured.append(item)
    return captured, rejected


def _attachment_references(payload_bytes, *, segment_start):
    references = []
    offset = int(segment_start)
    for raw_line in bytes(payload_bytes).splitlines(keepends=True):
        line_start = offset
        offset += len(raw_line)
        if not raw_line.endswith(b"\n"):
            continue
        try:
            record = json.loads(raw_line)
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            continue
        if not isinstance(record, dict):
            continue
        cursor = {"start": line_start, "end": offset}
        references.extend(_record_attachment_references(record, cursor))
    return references


def _record_attachment_references(record, cursor):
    references = []
    message_texts = []
    record_type = record.get("type")
    payload = record.get("payload")
    if (
        record_type == "event_msg"
        and isinstance(payload, dict)
        and payload.get("type") == "user_message"
    ):
        references.extend(
            _path_list_references(
                payload.get("local_images"),
                "codex.local_image",
                cursor,
            )
        )
        references.extend(
            _path_list_references(
                payload.get("local_audio"),
                "codex.local_audio",
                cursor,
            )
        )
        references.extend(
            _attachment_value_references(
                payload.get("attachments"),
                "user.file_mention",
                cursor,
            )
        )
        if isinstance(payload.get("message"), str):
            message_texts.append(payload["message"])
    elif (
        record_type == "response_item"
        and isinstance(payload, dict)
        and payload.get("type") == "message"
        and payload.get("role") == "user"
    ):
        for item in payload.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "input_text" and isinstance(
                item.get("text"), str
            ):
                message_texts.append(item["text"])
            elif item.get("type") in {
                "file",
                "input_file",
                "local_image",
                "local_audio",
            }:
                references.extend(
                    _attachment_value_references(
                        item,
                        (
                            "codex.local_image"
                            if item.get("type") == "local_image"
                            else "codex.local_audio"
                            if item.get("type") == "local_audio"
                            else "user.file_mention"
                        ),
                        cursor,
                    )
                )
    elif record_type == "user":
        message = record.get("message")
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                message_texts.append(content)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, str):
                        message_texts.append(item)
                    elif isinstance(item, dict):
                        if item.get("type") == "text" and isinstance(
                            item.get("text"), str
                        ):
                            message_texts.append(item["text"])
                        elif item.get("type") in {
                            "attachment",
                            "file",
                            "image",
                        }:
                            references.extend(
                                _attachment_value_references(
                                    item,
                                    "claude.attachment",
                                    cursor,
                                )
                            )
    for text in message_texts:
        references.extend(_file_mention_references(text, cursor))
    return references


def _path_list_references(values, reference_kind, cursor):
    if not isinstance(values, list):
        return []
    return [
        {
            "path": value,
            "reference_kind": reference_kind,
            "source_cursor": dict(cursor),
        }
        for value in values
        if isinstance(value, str) and value.strip()
    ]


def _attachment_value_references(value, reference_kind, cursor):
    values = value if isinstance(value, list) else [value]
    references = []
    for item in values:
        candidate = ""
        if isinstance(item, str):
            candidate = item
        elif isinstance(item, dict):
            for key in ("path", "file_path", "local_path"):
                if isinstance(item.get(key), str):
                    candidate = item[key]
                    break
            if not candidate and isinstance(item.get("source"), dict):
                for key in ("path", "file_path"):
                    if isinstance(item["source"].get(key), str):
                        candidate = item["source"][key]
                        break
        if candidate.strip():
            references.append(
                {
                    "path": candidate,
                    "reference_kind": reference_kind,
                    "source_cursor": dict(cursor),
                }
            )
    return references


def _file_mention_references(text, cursor):
    in_files = False
    references = []
    for line in str(text).splitlines():
        stripped = line.strip()
        if stripped == "# Files mentioned by the user:":
            in_files = True
            continue
        if in_files and stripped.startswith("## My request"):
            break
        if not in_files:
            continue
        match = re.match(r"^##\s+[^:]+:\s+(.+?)\s*$", stripped)
        if match:
            references.append(
                {
                    "path": match.group(1),
                    "reference_kind": "user.file_mention",
                    "source_cursor": dict(cursor),
                }
            )
    return references


def _capture_attachment_reference(
    config,
    state,
    source,
    reference,
    *,
    descriptor,
):
    recovered = _load_attachment_capture_journal(
        config,
        state,
        descriptor["capture_id"],
        descriptor=descriptor,
    )
    if recovered is not None:
        item, data, source_identity = recovered
        return (
            item,
            data,
            descriptor["capture_id"],
            source_identity,
            True,
        )
    max_bytes = _positive_config_int(
        config,
        "max_attachment_bytes",
        DEFAULT_MAX_ATTACHMENT_BYTES,
    )
    data = _load_attachment_capture_payload(
        config,
        descriptor["capture_id"],
        max_bytes=max_bytes,
    )
    if data is None:
        try:
            data = read_bounded_regular_file(
                descriptor["candidate"],
                max_bytes=max_bytes,
                root=descriptor["root"],
            )
        except (OSError, ProtocolError) as exc:
            if not _attachment_source_error_is_content_rejection(exc):
                raise ProducerError("attachment source I/O failed") from exc
            raise _AttachmentContentRejected(str(exc)) from exc
    if not data:
        raise _AttachmentContentRejected("empty attachment is not transported")
    digest = sha256_bytes(data)
    reference_id = derive_attachment_reference_id(
        producer_instance_id=state["producer_instance_id"],
        stream_id=descriptor["stream_id"],
        stream_epoch=source["stream_epoch"],
        source_cursor=reference["source_cursor"],
        source_locator_sha256=descriptor["source_locator_sha256"],
        original_name=descriptor["original_name"],
        payload_sha256=digest,
    )
    item = {
        "reference_id": reference_id,
        "original_name": descriptor["original_name"],
        "sha256": digest,
        "bytes": len(data),
        "media_type": descriptor["media_type"],
        "source_locator_sha256": descriptor["source_locator_sha256"],
        "reference_kind": reference["reference_kind"],
        "source_cursor": dict(reference["source_cursor"]),
        "agent": source["agent"],
        "session_id": source["session_id"],
        "stream_epoch": source["stream_epoch"],
        "metadata": dict(source["metadata"]),
    }
    _validate_attachment_queue_item(
        item,
        producer_instance_id=state["producer_instance_id"],
    )
    return (
        item,
        data,
        descriptor["capture_id"],
        descriptor["source_identity"],
        False,
    )


def _attachment_source_error_is_content_rejection(exc):
    cause = exc.__cause__ if isinstance(exc, ProtocolError) else exc
    if not isinstance(cause, OSError):
        return True
    return cause.errno in {
        errno.ENOENT,
        errno.ENOTDIR,
        errno.EISDIR,
        errno.ENAMETOOLONG,
        errno.EINVAL,
    }


def _attachment_reference_descriptor(config, state, source, reference):
    raw_path = str(reference["path"] or "").strip().strip("\"'")
    if not raw_path or "\x00" in raw_path:
        raise ProducerError("attachment reference path is invalid")
    expanded = os.path.expandvars(os.path.expanduser(raw_path))
    if not os.path.isabs(expanded):
        cwd = str(source.get("metadata", {}).get("cwd") or "")
        if not cwd or not os.path.isabs(cwd):
            raise ProducerError("relative attachment reference has no absolute cwd")
        expanded = os.path.join(cwd, expanded)
    candidate = os.path.abspath(expanded)
    root = _matching_attachment_root(config, candidate)
    if root is None:
        raise ProducerError("attachment reference is outside configured roots")
    original_name = os.path.basename(candidate)
    _validate_attachment_source_name(original_name)
    source_locator_sha256 = sha256_bytes(
        os.path.normcase(candidate).encode("utf-8")
    )
    media_type = (mimetypes.guess_type(original_name)[0] or "application/octet-stream")
    media_type = media_type.lower()
    stream_id = "stream-" + sha256_bytes(
        f"{source['agent']}\0{source['session_id']}".encode("utf-8")
    )
    capture_identity = {
        "producer_instance_id": state["producer_instance_id"],
        "stream_id": stream_id,
        "stream_epoch": source["stream_epoch"],
        "source_cursor": dict(reference["source_cursor"]),
        "source_locator_sha256": source_locator_sha256,
        "original_name": original_name,
        "reference_kind": reference["reference_kind"],
    }
    return {
        "capture_id": "capture-" + sha256_bytes(
            canonical_json_bytes(capture_identity)
        ),
        "candidate": candidate,
        "root": root,
        "original_name": original_name,
        "source_locator_sha256": source_locator_sha256,
        "media_type": media_type,
        "stream_id": stream_id,
        "reference_kind": reference["reference_kind"],
        "source_cursor": dict(reference["source_cursor"]),
        "agent": source["agent"],
        "session_id": source["session_id"],
        "stream_epoch": source["stream_epoch"],
        "source_identity": {
            "source_key": hashlib.sha256(
                os.path.normcase(
                    os.path.realpath(source["path"])
                ).encode("utf-8")
            ).hexdigest(),
            "file_identity": str(source.get("file_identity") or ""),
        },
    }


def _attachment_capture_document(capture_id, item, source_identity):
    return {
        "protocol": ATTACHMENT_CAPTURE_PROTOCOL,
        "schema_version": 2,
        "capture_id": capture_id,
        "item": item,
        "source_identity": dict(source_identity),
    }


def _write_attachment_capture_intent(
    config,
    capture_id,
    item,
    data,
    source_identity,
):
    payload = bytes(data)
    if len(payload) != item["bytes"] or sha256_bytes(payload) != item["sha256"]:
        raise ProducerError("attachment capture payload conflicts with intent")
    bundle = _attachment_capture_root(config) / capture_id
    write_immutable(
        bundle / "payload.bin",
        payload,
        root=_state_dir(config),
    )
    capture = _attachment_capture_document(
        capture_id,
        item,
        source_identity,
    )
    write_immutable(
        bundle / "capture.json",
        canonical_json_bytes(capture),
        root=_state_dir(config),
    )


def _write_attachment_capture_ready(config, capture_id, item):
    bundle = _attachment_capture_root(config) / capture_id
    try:
        capture_bytes = read_bounded_regular_file(
            bundle / "capture.json",
            max_bytes=128 * 1024,
            root=_state_dir(config),
        )
    except (FileNotFoundError, ProtocolError) as exc:
        raise ProducerError("attachment capture intent is unavailable") from exc
    ready = {
        "protocol": ATTACHMENT_CAPTURE_READY_PROTOCOL,
        "schema_version": 1,
        "capture_id": capture_id,
        "capture_sha256": sha256_bytes(capture_bytes),
        "payload_sha256": item["sha256"],
        "payload_bytes": item["bytes"],
    }
    write_immutable(
        bundle / "ready.json",
        canonical_json_bytes(ready),
        root=_state_dir(config),
    )


def _load_attachment_capture_payload(config, capture_id, *, max_bytes):
    state_dir = _state_dir(config)
    path = _attachment_capture_root(config) / capture_id / "payload.bin"
    if not os.path.lexists(path):
        return None
    try:
        return read_bounded_regular_file(
            path,
            max_bytes=max(max(1, int(max_bytes)), 1),
            root=state_dir,
        )
    except (FileNotFoundError, ProtocolError) as exc:
        raise ProducerError("attachment capture payload is invalid") from exc


def _load_attachment_capture_journal(
    config,
    state,
    capture_id,
    *,
    descriptor=None,
):
    if not re.fullmatch(r"capture-[0-9a-f]{64}", str(capture_id or "")):
        raise ProducerError("attachment capture ID is invalid")
    state_dir = _state_dir(config)
    bundle = _attachment_capture_root(config) / capture_id
    capture_path = bundle / "capture.json"
    ready_path = bundle / "ready.json"
    if not os.path.lexists(capture_path):
        if os.path.lexists(ready_path):
            raise ProducerError("attachment capture journal is invalid")
        return None
    try:
        capture_bytes = read_bounded_regular_file(
            capture_path,
            max_bytes=128 * 1024,
            root=state_dir,
        )
        capture = json.loads(capture_bytes)
    except (
        FileNotFoundError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        ProtocolError,
        RecursionError,
    ) as exc:
        raise ProducerError("attachment capture journal is invalid") from exc
    if not isinstance(capture, dict):
        raise ProducerError("attachment capture journal is invalid")
    capture_version = capture.get("schema_version")
    expected_fields = (
        LEGACY_ATTACHMENT_CAPTURE_FIELDS
        if capture_version == 1
        else ATTACHMENT_CAPTURE_FIELDS
        if capture_version == 2
        else frozenset()
    )
    if (
        set(capture) != expected_fields
        or capture.get("protocol") != ATTACHMENT_CAPTURE_PROTOCOL
        or capture.get("capture_id") != capture_id
        or canonical_json_bytes(capture) != capture_bytes
    ):
        raise ProducerError("attachment capture journal is invalid")
    item = capture["item"]
    _validate_attachment_queue_item(
        item,
        producer_instance_id=state["producer_instance_id"],
    )
    source_identity = capture.get("source_identity")
    if source_identity is not None and (
        not isinstance(source_identity, dict)
        or set(source_identity) != ATTACHMENT_CAPTURE_SOURCE_FIELDS
        or not re.fullmatch(
            r"[0-9a-f]{64}",
            str(source_identity.get("source_key") or ""),
        )
        or not isinstance(source_identity.get("file_identity"), str)
    ):
        raise ProducerError("attachment capture source identity is invalid")
    if descriptor is not None:
        expected = {
            "original_name": descriptor["original_name"],
            "source_locator_sha256": descriptor["source_locator_sha256"],
            "reference_kind": descriptor["reference_kind"],
            "source_cursor": descriptor["source_cursor"],
            "agent": descriptor["agent"],
            "session_id": descriptor["session_id"],
            "stream_epoch": descriptor["stream_epoch"],
        }
        if any(item[key] != value for key, value in expected.items()):
            raise ProducerError(
                "attachment capture does not match transcript reference"
            )
        if (
            source_identity is not None
            and source_identity != descriptor["source_identity"]
        ):
            raise ProducerError(
                "attachment capture does not match transcript source"
            )
    ready_exists = os.path.lexists(ready_path)
    if ready_exists:
        try:
            ready_bytes = read_bounded_regular_file(
                ready_path,
                max_bytes=16 * 1024,
                root=state_dir,
            )
            ready = json.loads(ready_bytes)
        except (
            FileNotFoundError,
            json.JSONDecodeError,
            UnicodeDecodeError,
            ProtocolError,
            RecursionError,
        ) as exc:
            raise ProducerError(
                "attachment capture ready marker is invalid"
            ) from exc
        if (
            not isinstance(ready, dict)
            or set(ready) != ATTACHMENT_CAPTURE_READY_FIELDS
            or ready["protocol"] != ATTACHMENT_CAPTURE_READY_PROTOCOL
            or ready["schema_version"] != 1
            or ready["capture_id"] != capture_id
            or ready["capture_sha256"] != sha256_bytes(capture_bytes)
            or ready["payload_sha256"] != item["sha256"]
            or ready["payload_bytes"] != item["bytes"]
            or canonical_json_bytes(ready) != ready_bytes
        ):
            raise ProducerError("attachment capture ready marker is invalid")

    data = _load_attachment_capture_payload(
        config,
        capture_id,
        max_bytes=item["bytes"],
    )
    cas_path = _attachment_cas_path(config, item["sha256"])
    if data is None:
        try:
            data = read_bounded_regular_file(
                cas_path,
                max_bytes=item["bytes"],
                root=state_dir,
            )
        except (FileNotFoundError, ProtocolError) as exc:
            if ready_exists:
                raise ProducerError(
                    "attachment capture CAS object is unavailable"
                ) from exc
            return None
    if len(data) != item["bytes"] or sha256_bytes(data) != item["sha256"]:
        raise ProducerError("attachment capture payload is corrupt")
    if not ready_exists:
        write_immutable(cas_path, data, root=state_dir)
        _write_attachment_capture_ready(config, capture_id, item)
    elif os.path.lexists(bundle / "payload.bin"):
        try:
            cas_data = read_bounded_regular_file(
                cas_path,
                max_bytes=item["bytes"],
                root=state_dir,
            )
        except (FileNotFoundError, ProtocolError) as exc:
            raise ProducerError(
                "attachment capture CAS object is unavailable"
            ) from exc
        if len(cas_data) != item["bytes"] or sha256_bytes(cas_data) != item["sha256"]:
            raise ProducerError("attachment capture CAS object is corrupt")
    return item, data, source_identity


def _attachment_capture_root(config):
    return _state_dir(config) / "attachment-captures" / "v1"


def _attachment_capture_has_durable_state_reference(state, item):
    reference_id = item["reference_id"]
    if any(
        candidate.get("reference_id") == reference_id
        for candidate in state.get("attachment_queue") or []
        if isinstance(candidate, dict)
    ):
        return True
    pending = state.get("pending_event")
    if isinstance(pending, dict):
        if any(
            candidate.get("reference_id") == reference_id
            for candidate in pending.get("attachments") or []
            if isinstance(candidate, dict)
        ):
            return True
        event = pending.get("event")
        attachment = (
            event.get("extensions", {}).get("attachment", {})
            if isinstance(event, dict)
            else {}
        )
        if attachment.get("reference_id") == reference_id:
            return True
    for source in state.get("sources", {}).values():
        if (
            source.get("agent") == item["agent"]
            and source.get("session_id") == item["session_id"]
            and source.get("stream_epoch") == item["stream_epoch"]
            and int(source.get("cursor") or 0)
            >= int(item["source_cursor"]["end"])
        ):
            return True
    return False


def _attachment_capture_is_unreplayable(state, item, source_identity):
    if source_identity is None:
        matching = [
            source
            for source in state.get("sources", {}).values()
            if source.get("agent") == item["agent"]
            and source.get("session_id") == item["session_id"]
        ]
        return bool(matching) and all(
            source.get("stream_epoch") != item["stream_epoch"]
            for source in matching
        )
    source = state.get("sources", {}).get(source_identity["source_key"])
    if not isinstance(source, dict):
        return False
    captured_file_identity = source_identity["file_identity"]
    current_file_identity = str(source.get("file_identity") or "")
    return bool(
        source.get("agent") != item["agent"]
        or source.get("session_id") != item["session_id"]
        or source.get("stream_epoch") != item["stream_epoch"]
        or (
            captured_file_identity
            and current_file_identity
            and captured_file_identity != current_file_identity
        )
    )


def _retire_attachment_capture_bundle(bundle, state_dir):
    ready_path = bundle / "ready.json"
    if os.path.lexists(ready_path):
        try:
            portable_unlink_regular(
                ready_path,
                root=state_dir,
                expected_identity=os.lstat(ready_path),
            )
        except ProtocolError as exc:
            raise ProducerError(
                "attachment capture ready marker is unsafe"
            ) from exc
    portable_rmtree(bundle, root=state_dir)


def _attachment_capture_journal_references(config, state):
    state_dir = _state_dir(config)
    root = _attachment_capture_root(config)
    if not os.path.lexists(root):
        return set()
    root_info = os.lstat(root)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ProducerError("attachment capture journal root is unsafe")
    entries = []
    recovery_scan_limit = max(
        1024,
        MAX_ATTACHMENT_CAPTURE_JOURNALS * 2,
        MAX_ATTACHMENT_CAPTURE_JOURNALS + 1024,
    )
    with os.scandir(root) as iterator:
        for entry in iterator:
            entries.append(entry)
            if len(entries) > recovery_scan_limit:
                raise ProducerError(
                    "attachment capture recovery scan exceeds limit"
                )
    referenced = set()
    retained_count = 0
    for entry in sorted(entries, key=lambda candidate: candidate.name):
        if not re.fullmatch(r"capture-[0-9a-f]{64}", entry.name):
            raise ProducerError("attachment capture journal entry is invalid")
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ProducerError("attachment capture journal entry is unsafe")
        bundle = Path(entry.path)
        if not os.path.lexists(bundle / "capture.json"):
            if os.path.lexists(bundle / "payload.bin"):
                retained_count += 1
                continue
            if os.path.lexists(bundle / "ready.json"):
                _retire_attachment_capture_bundle(bundle, state_dir)
                continue
            portable_rmtree(bundle, root=state_dir)
            continue
        recovered = _load_attachment_capture_journal(
            config,
            state,
            entry.name,
        )
        if recovered is None:
            retained_count += 1
            continue
        item, _data, source_identity = recovered
        if (
            _attachment_capture_has_durable_state_reference(state, item)
            or _attachment_capture_is_unreplayable(
                state,
                item,
                source_identity,
            )
        ):
            _retire_attachment_capture_bundle(bundle, state_dir)
        else:
            referenced.add(item["sha256"])
            retained_count += 1
    if retained_count > MAX_ATTACHMENT_CAPTURE_JOURNALS:
        raise ProducerError("attachment capture journal count exceeds limit")
    for parent in (root, root.parent):
        if os.path.lexists(parent) and not any(parent.iterdir()):
            portable_rmdir_empty(
                parent,
                root=state_dir,
                expected_identity=os.lstat(parent),
            )
    return referenced


def _matching_attachment_root(config, candidate):
    candidate_key = os.path.normcase(os.path.abspath(candidate))
    for configured in config.get("attachment_roots") or []:
        root = os.path.abspath(
            os.path.expandvars(os.path.expanduser(str(configured)))
        )
        if not os.path.isabs(root) or not os.path.isdir(root):
            continue
        root_key = os.path.normcase(root)
        try:
            if os.path.commonpath((root_key, candidate_key)) == root_key:
                return Path(root)
        except ValueError:
            continue
    return None


def _validate_attachment_source_name(name):
    text = str(name or "")
    lowered = text.casefold()
    if (
        not text
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
        or text.endswith((" ", "."))
        or any(character in text for character in '<>:"|?*')
        or len(text.encode("utf-8")) > 255
        or lowered in FORBIDDEN_ATTACHMENT_NAMES
        or Path(lowered).suffix in FORBIDDEN_ATTACHMENT_SUFFIXES
        or any(
            marker in lowered
            for marker in ("credential", "access-token", "refresh-token", "api-key")
        )
    ):
        raise ProducerError("attachment source name is not allowed")


def _attachment_cas_path(config, digest):
    return _state_dir(config) / "attachment-cas" / digest[:2] / digest


def _source_descriptor(path):
    path = os.path.abspath(os.path.expanduser(str(path)))
    metadata = read_transcript_metadata(path)
    session_id = str(metadata.get("session_id") or Path(path).stem).strip()
    agent = str(metadata.get("agent") or "").strip().lower()
    if agent not in {"codex", "claude"}:
        normalized = path.replace("\\", "/").lower()
        agent = "claude" if "/.claude/" in normalized else "codex"
    source_key = hashlib.sha256(
        os.path.normcase(os.path.realpath(path)).encode("utf-8")
    ).hexdigest()
    source = {
        "path": path,
        "agent": agent,
        "session_id": _safe_session_id(session_id),
        "stream_epoch": str(uuid.uuid4()),
        "cursor": 0,
        "prefix_bytes": min(os.path.getsize(path), 4096),
        "prefix_sha256": _source_prefix(
            path,
            min(os.path.getsize(path), 4096),
        ),
        "file_identity": _file_identity(path),
        "anchor_bytes": 0,
        "anchor_sha256": sha256_bytes(b""),
        "metadata": _bounded_metadata(metadata),
    }
    return source_key, source


def _refresh_source_identity(source, discovered, path):
    if source["path"] != discovered["path"]:
        raise ProducerError("producer source path identity changed")
    source.setdefault("file_identity", discovered["file_identity"])
    source.setdefault("anchor_bytes", 0)
    source.setdefault("anchor_sha256", sha256_bytes(b""))
    identity_changed = bool(
        source["file_identity"]
        and discovered["file_identity"]
        and source["file_identity"] != discovered["file_identity"]
    )
    if (
        source["agent"] != discovered["agent"]
        or source["session_id"] != discovered["session_id"]
        or identity_changed
    ):
        source.update(
            {
                "agent": discovered["agent"],
                "session_id": discovered["session_id"],
                "stream_epoch": str(uuid.uuid4()),
                "cursor": 0,
                "prefix_bytes": discovered["prefix_bytes"],
                "prefix_sha256": discovered["prefix_sha256"],
                "file_identity": discovered["file_identity"],
                "anchor_bytes": 0,
                "anchor_sha256": sha256_bytes(b""),
                "metadata": discovered["metadata"],
            }
        )
        return True
    else:
        source["metadata"] = discovered["metadata"]
        if not source["file_identity"]:
            source["file_identity"] = discovered["file_identity"]
    return False


def _source_prefix(path, length=4096):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read(max(0, int(length)))).hexdigest()


def _file_identity(path):
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ProducerError("transcript source is not a regular file")
    if not int(info.st_ino):
        return ""
    return f"{int(info.st_dev)}:{int(info.st_ino)}"


def _source_anchor(path, cursor, length=4096):
    anchor_bytes = min(max(0, int(cursor)), max(0, int(length)))
    if anchor_bytes == 0:
        return 0, sha256_bytes(b"")
    data = _read_source_range(
        path,
        int(cursor) - anchor_bytes,
        int(cursor),
    )
    return anchor_bytes, sha256_bytes(data)


def _update_source_anchor(source, path):
    anchor_bytes, anchor_sha256 = _source_anchor(path, source["cursor"])
    source["file_identity"] = _file_identity(path)
    source["anchor_bytes"] = anchor_bytes
    source["anchor_sha256"] = anchor_sha256


def _source_anchor_matches(path, source):
    anchor_bytes = int(source.get("anchor_bytes") or 0)
    if anchor_bytes == 0:
        return True
    cursor = int(source["cursor"])
    if anchor_bytes > cursor or os.path.getsize(path) < cursor:
        return False
    _bytes, digest = _source_anchor(path, cursor, anchor_bytes)
    return digest == source.get("anchor_sha256")


def _next_segment(path, start, *, max_chunk_bytes, max_gap_bytes):
    size = os.path.getsize(path)
    if start >= size:
        return None
    with open(path, "rb") as handle:
        handle.seek(start)
        chunk = handle.read(max_chunk_bytes)
        newline = chunk.rfind(b"\n")
        if newline >= 0:
            data = chunk[: newline + 1]
            return {
                "kind": "transcript.chunk",
                "start": start,
                "end": start + len(data),
                "sha256": sha256_bytes(data),
                "data": data,
            }

        handle.seek(start)
        digest = hashlib.sha256()
        total = 0
        while total < max_gap_bytes:
            piece = handle.read(min(64 * 1024, max_gap_bytes - total))
            if not piece:
                return None
            newline = piece.find(b"\n")
            if newline >= 0:
                selected = piece[: newline + 1]
                digest.update(selected)
                total += len(selected)
                return {
                    "kind": "transcript.gap",
                    "start": start,
                    "end": start + total,
                    "sha256": digest.hexdigest(),
                    "data": None,
                }
            digest.update(piece)
            total += len(piece)
    raise ProducerError("one transcript record exceeds max_gap_bytes")


def _allocate_event(
    config,
    state,
    source_key,
    source,
    segment,
    now,
    *,
    attachments=None,
):
    seq = state["next_seq"]
    payload = {
        "sha256": segment["sha256"],
        "bytes": segment["end"] - segment["start"],
        "media_type": "application/x-ndjson",
        "role": (
            "transcript-source"
            if segment["kind"] == "transcript.chunk"
            else "transcript-gap"
        ),
    }
    event = build_event(
        device_id=state["device_id"],
        producer_instance_id=state["producer_instance_id"],
        seq=seq,
        event_kind=segment["kind"],
        created_at=_iso_utc(now),
        agent=source["agent"],
        session_id=source["session_id"],
        stream_epoch=source["stream_epoch"],
        source_cursor={"start": segment["start"], "end": segment["end"]},
        metadata=source["metadata"],
        payload=payload,
    )
    prefix_bytes = min(segment["end"], 4096)
    anchor_bytes, anchor_sha256 = _source_anchor(
        source["path"],
        segment["end"],
    )
    state["pending_event"] = {
        "pending_type": "transcript",
        "source_key": source_key,
        "source_path": source["path"],
        "event": event,
        "file_identity": _file_identity(source["path"]),
        "prefix_bytes": prefix_bytes,
        "prefix_sha256": _source_prefix(source["path"], prefix_bytes),
        "anchor_bytes": anchor_bytes,
        "anchor_sha256": anchor_sha256,
        "attachments": list(attachments or []),
    }
    return event, segment["data"]


def _drain_attachment_queue(
    config,
    state,
    result,
    now,
    *,
    max_events,
    fault_point=None,
):
    while state["attachment_queue"] and result["emitted"] < max_events:
        item = state["attachment_queue"][0]
        event, payload_bytes = _allocate_attachment_event(
            config,
            state,
            item,
            now,
        )
        _stage_pending_payload(config, event, payload_bytes)
        _write_state(config, state)
        if fault_point == "after_attachment_allocation_state":
            raise ProducerError("injected failure after_attachment_allocation_state")
        _publish_pending(
            config,
            state,
            payload_bytes=payload_bytes,
            fault_point=fault_point,
        )
        result["emitted"] += 1
        result["attachments_emitted"] += 1


def _allocate_attachment_event(config, state, item, now):
    _validate_attachment_queue_item(
        item,
        producer_instance_id=state["producer_instance_id"],
    )
    payload = {
        "sha256": item["sha256"],
        "bytes": item["bytes"],
        "media_type": item["media_type"],
        "role": "attachment-source",
    }
    event = build_event(
        device_id=state["device_id"],
        producer_instance_id=state["producer_instance_id"],
        seq=state["next_seq"],
        event_kind="attachment.blob",
        created_at=_iso_utc(now),
        agent=item["agent"],
        session_id=item["session_id"],
        stream_epoch=item["stream_epoch"],
        source_cursor=dict(item["source_cursor"]),
        metadata=item["metadata"],
        payload=payload,
        extensions={
            "attachment": {
                "reference_id": item["reference_id"],
                "original_name": item["original_name"],
                "source_locator_sha256": item["source_locator_sha256"],
                "reference_kind": item["reference_kind"],
            }
        },
    )
    payload_bytes = read_bounded_regular_file(
        _attachment_cas_path(config, item["sha256"]),
        max_bytes=item["bytes"],
        root=_state_dir(config),
    )
    if len(payload_bytes) != item["bytes"] or sha256_bytes(payload_bytes) != item["sha256"]:
        raise ProducerError("queued attachment CAS object is corrupt")
    state["pending_event"] = {
        "pending_type": "attachment",
        "queue_id": item["reference_id"],
        "event": event,
    }
    return event, payload_bytes


def _is_legacy_attachment_event(event):
    return bool(
        isinstance(event, dict)
        and event.get("schema_version") == 1
        and event.get("event_kind") == "attachment.blob"
    )


def _legacy_event_bundle_name(event):
    validate_legacy_attachment_event(event)
    return f"{event['seq']:020d}-{event['event_id']}"


def _pending_bundle_candidates(outbox, event, *, legacy_attachment):
    producer_root = (
        Path(outbox)
        / "v1"
        / "events"
        / event["producer_instance_id"]
    )
    if legacy_attachment:
        return (producer_root / _legacy_event_bundle_name(event),)
    bundle_name = event_bundle_name(event)
    return (
        producer_root
        / event_sequence_directory_name(event["seq"])
        / bundle_name,
        producer_root / bundle_name,
    )


def _publish_pending(config, state, payload_bytes=None, fault_point=None):
    pending = state.get("pending_event")
    if not pending:
        return
    event = pending["event"]
    legacy_attachment = _is_legacy_attachment_event(event)
    if legacy_attachment:
        validate_legacy_attachment_event(event)
    else:
        validate_event(event)
    pending_type = pending.get("pending_type") or "transcript"
    source = None
    if pending_type == "transcript":
        source = state["sources"].get(pending["source_key"])
        if source is None:
            raise ProducerError("pending event source is missing")
    elif pending_type == "attachment":
        if (
            not state["attachment_queue"]
            or state["attachment_queue"][0]["reference_id"]
            != pending.get("queue_id")
        ):
            raise ProducerError("pending attachment no longer matches queue")
    else:
        raise ProducerError("pending event type is invalid")
    if event["seq"] != state["next_seq"]:
        raise ProducerError("pending event sequence conflicts with producer state")
    outbox = _outbox_dir(config)
    bundle_candidates = _pending_bundle_candidates(
        outbox,
        event,
        legacy_attachment=legacy_attachment,
    )
    for existing_bundle in bundle_candidates:
        if _durable_pending_bundle(
            existing_bundle,
            outbox,
            event,
            legacy_attachment=legacy_attachment,
        ):
            _commit_pending_state(config, state, pending, source)
            _discard_pending_payload(config)
            return
    bundle = bundle_candidates[0]
    if legacy_attachment:
        raise ProducerError(
            "legacy pending attachment bundle is no longer durable"
        )
    if event["payload"]["role"] in {"transcript-source", "attachment-source"}:
        payload_bytes = _load_pending_payload(config, event)
        if sha256_bytes(payload_bytes) != event["payload"]["sha256"]:
            raise ProducerError("staged pending payload conflicts with event")

    if event["payload"]["role"] in {"transcript-source", "attachment-source"}:
        write_immutable(
            bundle / "objects" / event["payload"]["sha256"],
            payload_bytes,
            root=outbox,
        )
    event_bytes = canonical_json_bytes(event)
    write_immutable(bundle / "event.json", event_bytes, root=outbox)
    ready = build_ready(event, event_bytes)
    write_immutable(bundle / "ready.json", canonical_json_bytes(ready), root=outbox)
    if fault_point == "after_ready":
        raise ProducerError("injected failure after_ready")

    _commit_pending_state(config, state, pending, source)
    _discard_pending_payload(config)


def _stage_pending_payload(config, event, payload_bytes):
    path = _state_dir(config) / "pending-payload.bin"
    if event["payload"]["role"] == "transcript-gap":
        _discard_pending_payload(config)
        return
    if not isinstance(payload_bytes, (bytes, bytearray)):
        raise ProducerError("pending transcript payload is missing")
    payload = bytes(payload_bytes)
    if (
        len(payload) != event["payload"]["bytes"]
        or sha256_bytes(payload) != event["payload"]["sha256"]
    ):
        raise ProducerError("pending transcript payload conflicts with event")
    portable_atomic_write(
        path,
        payload,
        root=_state_dir(config),
    )


def _load_pending_payload(config, event):
    state_dir = _state_dir(config)
    try:
        payload = read_bounded_regular_file(
            state_dir / "pending-payload.bin",
            max_bytes=event["payload"]["bytes"],
            root=state_dir,
        )
    except (FileNotFoundError, ProtocolError) as exc:
        raise ProducerError("staged pending payload is unavailable") from exc
    if len(payload) != event["payload"]["bytes"]:
        raise ProducerError("staged pending payload size conflicts with event")
    return payload


def _discard_pending_payload(config):
    state_dir = _state_dir(config)
    path = state_dir / "pending-payload.bin"
    if not os.path.lexists(path):
        return False
    try:
        return portable_unlink_regular(path, root=state_dir)
    except ProtocolError as exc:
        raise ProducerError("staged pending payload is unsafe") from exc


def _cleanup_attachment_cas(config, state):
    state_dir = _state_dir(config)
    cas_root = state_dir / "attachment-cas"
    journal_references = _attachment_capture_journal_references(
        config,
        state,
    )
    if not os.path.lexists(cas_root):
        if journal_references:
            raise ProducerError(
                "attachment capture journal references a missing CAS root"
            )
        return 0
    root_info = os.lstat(cas_root)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise ProducerError("attachment CAS root is unsafe")
    referenced = {
        item["sha256"] for item in state.get("attachment_queue") or []
    }
    referenced.update(journal_references)
    pending = state.get("pending_event")
    if isinstance(pending, dict):
        event = pending.get("event")
        if (
            isinstance(event, dict)
            and event.get("event_kind") == "attachment.blob"
            and isinstance(event.get("payload"), dict)
        ):
            referenced.add(event["payload"]["sha256"])
        for item in pending.get("attachments") or []:
            if isinstance(item, dict) and isinstance(item.get("sha256"), str):
                referenced.add(item["sha256"])
    removed = 0
    for current, directories, files in os.walk(cas_root, topdown=False):
        for filename in files:
            path = Path(current) / filename
            info = os.lstat(path)
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
            ):
                raise ProducerError("attachment CAS contains an unsafe entry")
            if filename not in referenced:
                portable_unlink_regular(
                    path,
                    root=state_dir,
                    expected_identity=info,
                )
                removed += 1
        for directory in directories:
            path = Path(current) / directory
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ProducerError("attachment CAS contains an unsafe directory")
            if not any(path.iterdir()):
                portable_rmdir_empty(
                    path,
                    root=state_dir,
                    expected_identity=info,
                )
    if cas_root.exists() and not any(cas_root.iterdir()):
        portable_rmdir_empty(
            cas_root,
            root=state_dir,
            expected_identity=os.lstat(cas_root),
        )
    return removed


def _durable_pending_bundle(
    bundle,
    outbox,
    event,
    *,
    legacy_attachment=False,
):
    ready_path = bundle / "ready.json"
    if not ready_path.is_file():
        return False
    try:
        event_bytes = read_bounded_regular_file(
            bundle / "event.json",
            max_bytes=128 * 1024,
            root=outbox,
        )
        if event_bytes != canonical_json_bytes(event):
            raise ProducerError("durable event bytes conflict with pending state")
        ready_bytes = read_bounded_regular_file(
            ready_path,
            max_bytes=16 * 1024,
            root=outbox,
        )
        ready = json.loads(ready_bytes)
        if canonical_json_bytes(ready) != ready_bytes:
            raise ProducerError("durable ready marker is not canonical")
        if legacy_attachment:
            validate_legacy_attachment_ready(ready, event, event_bytes)
        else:
            validate_ready(ready, event, event_bytes)
        if ready["object_count"]:
            payload = read_bounded_regular_file(
                bundle / "objects" / event["payload"]["sha256"],
                max_bytes=max(event["payload"]["bytes"], 1),
                root=outbox,
            )
            if (
                len(payload) != event["payload"]["bytes"]
                or sha256_bytes(payload) != event["payload"]["sha256"]
            ):
                raise ProducerError("durable payload conflicts with pending state")
        return True
    except FileNotFoundError:
        return False
    except (json.JSONDecodeError, ProtocolError) as exc:
        raise ProducerError("durable pending bundle is invalid") from exc


def _commit_pending_state(config, state, pending, source):
    event = pending["event"]
    pending_type = pending.get("pending_type") or "transcript"
    if pending_type == "transcript":
        source["cursor"] = event["source_cursor"]["end"]
        source["file_identity"] = str(
            pending.get("file_identity") or source.get("file_identity") or ""
        )
        if source["prefix_bytes"] == 0 and source["cursor"] > 0:
            source["prefix_bytes"] = int(
                pending.get("prefix_bytes") or min(source["cursor"], 4096)
            )
            source["prefix_sha256"] = str(
                pending.get("prefix_sha256") or source["prefix_sha256"]
            )
        source["anchor_bytes"] = int(pending.get("anchor_bytes") or 0)
        source["anchor_sha256"] = str(
            pending.get("anchor_sha256") or sha256_bytes(b"")
        )
        queued_ids = {
            item["reference_id"] for item in state["attachment_queue"]
        }
        for item in pending.get("attachments") or []:
            _validate_attachment_queue_item(
                item,
                producer_instance_id=state["producer_instance_id"],
            )
            if item["reference_id"] not in queued_ids:
                state["attachment_queue"].append(item)
                queued_ids.add(item["reference_id"])
    elif pending_type == "attachment":
        if (
            not state["attachment_queue"]
            or state["attachment_queue"][0]["reference_id"]
            != pending.get("queue_id")
        ):
            raise ProducerError("pending attachment no longer matches queue")
        state["attachment_queue"].pop(0)
    else:
        raise ProducerError("pending event type is invalid")
    state["next_seq"] = event["seq"] + 1
    state["pending_event"] = None
    _write_state(config, state)
    _cleanup_attachment_cas(config, state)


def _read_source_range(path, start, end):
    with open(path, "rb") as handle:
        handle.seek(start)
        data = handle.read(end - start)
    if len(data) != end - start:
        raise ProducerError("pending source range is no longer available")
    return data


def _hash_source_range(path, start, end):
    digest = hashlib.sha256()
    remaining = end - start
    with open(path, "rb") as handle:
        handle.seek(start)
        while remaining:
            data = handle.read(min(1024 * 1024, remaining))
            if not data:
                raise ProducerError("pending source range is no longer available")
            digest.update(data)
            remaining -= len(data)
    return digest.hexdigest()


def _bounded_metadata(metadata):
    output = {}
    for key, limit in (
        ("cwd", 1024),
        ("timestamp", 128),
        ("date", 10),
        ("source", 256),
    ):
        value = metadata.get(key)
        if value:
            text = "".join(
                character
                for character in str(value)
                if ord(character) >= 32 and ord(character) != 127
            )
            output[key] = text[:limit]
    output["is_subagent"] = bool(metadata.get("is_subagent"))
    return output


def _safe_session_id(value):
    text = "".join(
        character
        for character in str(value or "")
        if ord(character) >= 32 and ord(character) != 127
    ).strip()
    if not SAFE_SESSION_ID_RE.fullmatch(text):
        return "session-" + hashlib.sha256(text.encode("utf-8")).hexdigest()
    return text


def _validate_gc_receipt(receipt, event, event_bytes):
    required = {
        "protocol",
        "schema_version",
        "producer_instance_id",
        "seq",
        "event_id",
        "event_sha256",
        "status",
        "code",
        "canonical_generation",
        "generation_id",
        "gc_allowed",
        "processed_at",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise ProducerError("receipt fields do not match schema")
    if receipt["protocol"] != PROTOCOL_RECEIPT or receipt["schema_version"] != 1:
        raise ProducerError("receipt protocol is unsupported")
    for key in ("producer_instance_id", "seq", "event_id"):
        if receipt[key] != event[key]:
            raise ProducerError("receipt identity does not match event")
    if receipt["event_sha256"] != sha256_bytes(event_bytes):
        raise ProducerError("receipt event hash does not match event")
    if receipt["status"] not in {"applied", "noop", "rejected"}:
        raise ProducerError("receipt status does not authorize GC")
    expected_codes = {
        "applied": "applied",
        "noop": "noop",
        "rejected": "forbidden_event_kind",
    }
    if receipt["code"] != expected_codes[receipt["status"]]:
        raise ProducerError("receipt code does not match status")
    generation = receipt["canonical_generation"]
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
    ):
        raise ProducerError("receipt canonical generation is invalid")
    if not re.fullmatch(
        r"generation-[0-9a-f]{64}",
        str(receipt["generation_id"] or ""),
    ):
        raise ProducerError("receipt generation ID is invalid")
    _parse_utc(receipt["processed_at"])
    if receipt["gc_allowed"] is not True:
        raise ProducerError("receipt does not authorize GC")


def _validate_gc_generation(config, receipt):
    from beacon_sync_snapshot import (
        MaterializeError,
        inspect_received_generation,
        inspect_replica_state,
    )

    try:
        replica_config = {
            "state_dir": config.get("state_dir", ""),
            "received_published_dir": config.get(
                "received_published_dir",
                "",
            ),
            "replica_path": config.get("replica_path", ""),
            "max_replica_object_bytes": config.get(
                "max_replica_object_bytes",
                DEFAULT_MAX_CHUNK_BYTES * 4,
            ),
        }
        inspect_received_generation(
            replica_config,
            receipt["canonical_generation"],
            receipt["generation_id"],
        )
        active = inspect_replica_state(replica_config)
        if (
            not active.get("active")
            or int(active["generation"]) < receipt["canonical_generation"]
        ):
            raise ProducerError(
                "receipt generation is not active in the local replica"
            )
    except (MaterializeError, OSError, ProtocolError, ValueError) as exc:
        raise ProducerError("receipt generation is not sealed and verified") from exc


def _remove_bundle(bundle, outbox):
    bundle = Path(bundle)
    outbox = Path(outbox)
    sequence_directory = bundle.parent
    try:
        if os.path.commonpath([outbox.resolve(), bundle.resolve()]) != str(
            outbox.resolve()
        ):
            raise ProducerError("bundle resolves outside outbox")
    except ValueError as exc:
        raise ProducerError("bundle resolves outside outbox") from exc
    for current, directories, files in os.walk(bundle, topdown=False):
        for name in files:
            path = Path(current) / name
            info = os.lstat(path)
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
            ):
                raise ProducerError("outbox bundle contains an unsafe entry")
            portable_unlink_regular(
                path,
                root=outbox,
                expected_identity=info,
            )
        for name in directories:
            path = Path(current) / name
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ProducerError("outbox bundle contains an unsafe directory")
            portable_rmdir_empty(
                path,
                root=outbox,
                expected_identity=info,
            )
    bundle_info = os.lstat(bundle)
    if stat.S_ISLNK(bundle_info.st_mode) or not stat.S_ISDIR(bundle_info.st_mode):
        raise ProducerError("outbox bundle is not a safe directory")
    portable_rmdir_empty(
        bundle,
        root=outbox,
        expected_identity=bundle_info,
    )
    if is_event_sequence_directory_name(sequence_directory.name):
        try:
            with os.scandir(sequence_directory) as entries:
                if next(entries, None) is not None:
                    return
            sequence_info = os.lstat(sequence_directory)
            if (
                not stat.S_ISLNK(sequence_info.st_mode)
                and stat.S_ISDIR(sequence_info.st_mode)
            ):
                portable_rmdir_empty(
                    sequence_directory,
                    root=outbox,
                    expected_identity=sequence_info,
                )
        except FileNotFoundError:
            return


def _load_producer_progress(config):
    state_dir = _state_dir(config)
    path = state_dir / "producer-progress.json"
    if not os.path.lexists(path):
        return {
            "protocol": PRODUCER_PROGRESS_PROTOCOL,
            "schema_version": 1,
            "discovery_cursor": "",
            "gc_cursor": "",
        }
    try:
        data = read_bounded_regular_file(
            path,
            max_bytes=64 * 1024,
            root=state_dir,
        )
        progress = json.loads(data)
    except (
        OSError,
        ProtocolError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise ProducerError("producer progress is invalid") from exc
    if (
        not isinstance(progress, dict)
        or set(progress)
        != {
            "protocol",
            "schema_version",
            "discovery_cursor",
            "gc_cursor",
        }
        or progress["protocol"] != PRODUCER_PROGRESS_PROTOCOL
        or progress["schema_version"] != 1
        or not isinstance(progress["discovery_cursor"], str)
        or not isinstance(progress["gc_cursor"], str)
        or canonical_json_bytes(progress) != data
    ):
        raise ProducerError("producer progress is invalid")
    return progress


def _write_producer_progress(config, progress):
    state_dir = _state_dir(config)
    portable_atomic_write(
        state_dir / "producer-progress.json",
        canonical_json_bytes(progress),
        root=state_dir,
    )


def _state_dir(config):
    value = str(config.get("state_dir") or "").strip()
    if not value:
        raise ProducerError("beacon_sync.state_dir is required")
    return Path(os.path.abspath(os.path.expanduser(value)))


def _outbox_dir(config):
    value = str(config.get("outbox_dir") or "").strip()
    if not value:
        raise ProducerError("beacon_sync.outbox_dir is required")
    return Path(os.path.abspath(os.path.expanduser(value)))


def _positive_config_int(config, key, default):
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProducerError(f"beacon_sync.{key} must be a positive integer")
    return value


def _nonnegative_config_int(config, key, default):
    value = config.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProducerError(f"beacon_sync.{key} must be a non-negative integer")
    return value


def _utc_now(value):
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ProducerError("producer timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso_utc(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(value):
    text = str(value or "")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ProducerError("receipt timestamp is not timezone-aware")
    return parsed.astimezone(timezone.utc)
