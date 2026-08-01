"""Mac authority ingress ledger and transcript-mirror reducer."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import stat
import struct
import tempfile
import uuid
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path, PurePosixPath

from beacon_sync_protocol import (
    LEGACY_ATTACHMENT_SCHEMA_VERSION,
    MAX_EVENT_JSON_BYTES,
    PROTOCOL_EVENT,
    PROTOCOL_READY,
    ProtocolError,
    canonical_json_bytes,
    decode_bounded_json,
    event_bundle_name,
    event_sequence_directory_name,
    portable_atomic_write,
    portable_file_lock,
    portable_rmtree,
    portable_unlink_regular,
    read_bounded_regular_file,
    read_bounded_regular_file_with_identity,
    sha256_bytes,
    validate_event,
    validate_legacy_attachment_event,
    validate_legacy_attachment_ready,
    validate_replica_path,
    validate_ready,
    write_immutable,
)
from safety import exclusive_file_lock, secure_open_file


LEDGER_SCHEMA_VERSION = 4
GAP_PLACEHOLDER_BYTES = 65_537
MAX_IDENTITY_REGISTRY_ENTRIES = 256
MAX_BUNDLE_ENTRIES_PER_PRODUCER = 4096
MAX_LEDGER_BYTES = 512 * 1024 * 1024
MAX_LEDGER_SIDECAR_BYTES = 512 * 1024 * 1024
MAX_PENDING_ATTACHMENT_CURSOR_BYTES = 512
PENDING_ATTACHMENT_SCAN_CURSOR_KEY = "pending_attachment_scan_cursor"
PENDING_ATTACHMENT_SCAN_MULTIPLIER = 8
PRODUCER_ROTATION_CURSOR_KEY = "producer_rotation_cursor"
MAX_RECEIPT_EVENTS_PER_CALL = 256
MAX_RECEIPT_CURSOR_BYTES = 512
FINALIZED_RECEIPT_CURSOR_KEY = "finalized_receipt_repair_cursor"
PENDING_RECEIPT_BIND_CURSOR_KEY = "pending_receipt_bind_cursor"
ROLLBACK_JOURNAL_MAGIC = b"\xd9\xd5\x05\xf9\x20\xa1\x63\xd7"
WAL_MAGIC_VALUES = frozenset({0x377F0682, 0x377F0683})
WAL_FORMAT_VERSION = 3_007_000
TERMINAL_PENDING_STATUSES = frozenset(
    {
        "applied_pending_publish",
        "noop_pending_publish",
        "rejected_pending_publish",
    }
)
METADATA_COLUMNS = ("key", "value")
PRODUCER_COLUMNS = (
    "producer_instance_id",
    "device_id",
    "next_seq",
    "blocked_code",
    "updated_at",
)
STREAM_COLUMNS = (
    "producer_instance_id",
    "stream_id",
    "stream_epoch",
    "committed_cursor",
    "mirror_path",
    "session_id",
    "agent",
)
EVENT_COLUMNS_V1 = (
    "producer_instance_id",
    "seq",
    "event_id",
    "event_sha256",
    "device_id",
    "status",
    "code",
    "bundle_path",
    "event_kind",
    "stream_id",
    "stream_epoch",
    "cursor_start",
    "cursor_end",
    "mirror_path",
    "mirror_before_size",
    "mirror_append_size",
    "mirror_append_sha256",
    "created_at",
    "processed_at",
    "canonical_generation",
    "generation_id",
)
EVENT_COLUMNS_V3 = EVENT_COLUMNS_V1 + (
    "canonical_path",
    "metadata_path",
    "payload_sha256",
    "payload_bytes",
)
EVENT_COLUMNS_V4 = EVENT_COLUMNS_V3 + (
    "metadata_sha256",
    "metadata_bytes",
)
LEDGER_PRIMARY_KEYS = {
    "metadata": ("key",),
    "producers": ("producer_instance_id",),
    "streams": ("producer_instance_id", "stream_id", "stream_epoch"),
    "events": ("producer_instance_id", "seq"),
}
LEDGER_SCHEMA_CONTRACTS = {
    1: {
        "metadata": METADATA_COLUMNS,
        "producers": PRODUCER_COLUMNS,
        "streams": STREAM_COLUMNS,
        "events": EVENT_COLUMNS_V1,
    },
    2: {
        "metadata": METADATA_COLUMNS,
        "producers": PRODUCER_COLUMNS,
        "streams": STREAM_COLUMNS,
        "events": EVENT_COLUMNS_V1,
    },
    3: {
        "metadata": METADATA_COLUMNS,
        "producers": PRODUCER_COLUMNS,
        "streams": STREAM_COLUMNS,
        "events": EVENT_COLUMNS_V3,
    },
    4: {
        "metadata": METADATA_COLUMNS,
        "producers": PRODUCER_COLUMNS,
        "streams": STREAM_COLUMNS,
        "events": EVENT_COLUMNS_V4,
    },
}


class ReducerError(RuntimeError):
    """Authority ingress cannot continue without violating an invariant."""


class IngressDeferred(RuntimeError):
    """A valid-looking event cannot fit safely in the current ingress budget."""


class _LimitedRows(list):
    def __init__(self, rows=(), *, limited=False, cursor=None):
        super().__init__(rows)
        self.limited = bool(limited)
        self.cursor = cursor


class _LimitedCount(int):
    def __new__(cls, value, *, limited=False, cursor=None):
        instance = super().__new__(cls, value)
        instance.limited = bool(limited)
        instance.cursor = cursor
        return instance


class _LedgerConnection(sqlite3.Connection):
    """In-memory ledger holding one process lock and a publish CAS."""

    def configure_persistence(
        self,
        path,
        state_dir,
        snapshot,
        lock_context,
        image_digest,
    ):
        self._ledger_path = Path(path)
        self._ledger_state_dir = Path(state_dir)
        self._ledger_snapshot = snapshot
        self._ledger_lock_context = lock_context
        self._ledger_image_digest = image_digest
        self._ledger_closed = False

    def _publish_if_changed(self):
        image = self.serialize()
        image_digest = sha256_bytes(image)
        if image_digest == self._ledger_image_digest:
            return False
        try:
            _assert_ledger_snapshot_matches(
                self._ledger_path,
                self._ledger_state_dir,
                self._ledger_snapshot,
            )
            portable_atomic_write(
                self._ledger_path,
                image,
                root=self._ledger_state_dir,
            )
            for suffix in ("-wal", "-shm", "-journal"):
                component = self._ledger_snapshot.get(suffix)
                if component is None:
                    continue
                portable_unlink_regular(
                    Path(f"{self._ledger_path}{suffix}"),
                    root=self._ledger_state_dir,
                    expected_identity=component["identity"],
                )
            self._ledger_snapshot = _capture_ledger_snapshot(
                self._ledger_path,
                self._ledger_state_dir,
            )
            published = self._ledger_snapshot.get("")
            if published is None or published["digest"] != image_digest:
                raise ReducerError(
                    "authority ledger publish did not preserve committed bytes"
                )
            self._ledger_image_digest = image_digest
        except ProtocolError as exc:
            raise ReducerError(str(exc)) from exc
        return True

    def commit(self):
        super().commit()
        self._publish_if_changed()

    def __exit__(self, exc_type, exc_value, traceback):
        result = super().__exit__(exc_type, exc_value, traceback)
        if exc_type is None:
            self._publish_if_changed()
        return result

    def close(self):
        if getattr(self, "_ledger_closed", False):
            return
        self._ledger_closed = True
        try:
            super().close()
        finally:
            lock_context = getattr(self, "_ledger_lock_context", None)
            self._ledger_lock_context = None
            if lock_context is not None:
                lock_context.__exit__(None, None, None)


def reduce_inboxes(
    cfg,
    sync_cfg,
    *,
    harvest_adapter=None,
    now=None,
    fault_point=None,
):
    """Serialize one complete authority reduction against snapshot sealing."""
    state_dir = _state_dir(sync_cfg)
    state_dir.mkdir(parents=True, exist_ok=True)
    with portable_file_lock(
        state_dir / "authority-cycle.lock",
        root=state_dir,
    ):
        return _reduce_inboxes_locked(
            cfg,
            sync_cfg,
            harvest_adapter=harvest_adapter,
            now=now,
            fault_point=fault_point,
        )


def _reduce_inboxes_locked(
    cfg,
    sync_cfg,
    *,
    harvest_adapter=None,
    now=None,
    fault_point=None,
):
    """Validate and reduce the next contiguous event batch from trusted inboxes."""
    now = _utc_now(now)
    state_dir = _state_dir(sync_cfg)
    state_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "applied": 0,
        "noop": 0,
        "rejected": 0,
        "duplicates": 0,
        "deferred": 0,
        "blocked": 0,
        "quarantined": 0,
        "pending_publish": 0,
        "repaired_attachments": 0,
    }
    with portable_file_lock(state_dir / "reducer.lock", root=state_dir):
        connection = _open_ledger(state_dir)
        try:
            candidates, contexts = _discover_candidates(
                connection,
                sync_cfg,
                result,
                state_dir,
            )
            _record_duplicates_and_conflicts(connection, candidates, result, state_dir)
            batch = _next_contiguous_batch(connection, candidates, sync_cfg, result)
            historical_attachments = _discover_pending_attachment_candidates(
                connection,
                sync_cfg,
                contexts,
                candidates,
                result,
                state_dir,
            )
            repair_candidates = _pending_attachment_repair_candidates(
                connection,
                [*candidates, *historical_attachments],
            )
            if not batch and not repair_candidates:
                result["pending_publish"] = _pending_publish_count(connection)
                return result

            vault = Path(os.path.abspath(os.path.expanduser(str(cfg["vault_path"]))))
            lock_path = vault / "04-Feedback" / "_logs" / "harvester.lock"
            with exclusive_file_lock(lock_path, root=vault):
                result["repaired_attachments"] = (
                    _repair_pending_attachment_effects(
                        connection,
                        vault,
                        repair_candidates,
                    )
                )
                staged, rejected = ([], [])
                if batch:
                    staged, rejected = _stage_batch(
                        connection,
                        state_dir,
                        vault,
                        batch,
                        now,
                        fault_point=fault_point,
                    )
                changed_by_mirror = {}
                mirror_paths = tuple(
                    sorted(
                        {
                            item["mirror_path"]
                            for item in staged
                            if item["kind"] == "transcript"
                        }
                    )
                )
                if mirror_paths:
                    adapter = harvest_adapter or _default_harvest_adapter
                    changed_by_mirror = adapter(
                        cfg,
                        mirror_paths,
                    )
                    if not isinstance(changed_by_mirror, dict):
                        raise ReducerError("harvest adapter must return a path mapping")
                if batch:
                    _commit_batch(
                        connection,
                        batch,
                        staged,
                        rejected,
                        changed_by_mirror,
                        now,
                        result,
                    )
            result["pending_publish"] = _pending_publish_count(connection)
            return result
        finally:
            connection.close()


def list_ledger_events(sync_cfg):
    state_dir = _state_dir(sync_cfg)
    if not (state_dir / "ledger.sqlite3").exists():
        return []
    connection = _open_ledger(state_dir)
    try:
        rows = connection.execute(
            """
            select producer_instance_id, seq, event_id, event_sha256, device_id,
                   status, code, stream_id, stream_epoch, cursor_start,
                   cursor_end, mirror_path, canonical_path, metadata_path,
                   payload_sha256, payload_bytes, metadata_sha256,
                   metadata_bytes, event_kind,
                   canonical_generation, generation_id
              from events
             order by producer_instance_id, seq
            """
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def pending_receipt_events(sync_cfg, *, bound_only=False, limit=None):
    """Return events that may receive receipts after a sealed generation."""
    state_dir = _state_dir(sync_cfg)
    if not (state_dir / "ledger.sqlite3").exists():
        return _LimitedRows()
    bounded_limit = _receipt_event_limit(sync_cfg, limit)
    connection = _open_ledger(state_dir)
    try:
        binding_clause = (
            "and canonical_generation is not null and generation_id != ''"
            if bound_only
            else ""
        )
        rows = connection.execute(
            f"""
            select *
              from events
             where status in (
                 'applied_pending_publish',
                 'noop_pending_publish',
                 'rejected_pending_publish'
             )
               {binding_clause}
             order by producer_instance_id, seq
             limit ?
            """
            ,
            (bounded_limit + 1,),
        ).fetchall()
        limited = len(rows) > bounded_limit
        return _LimitedRows(
            (dict(row) for row in rows[:bounded_limit]),
            limited=limited,
        )
    finally:
        connection.close()


def bound_receipt_events(sync_cfg, *, limit=None):
    """Return pending and finalized receipt rows bound to sealed generations."""
    state_dir = _state_dir(sync_cfg)
    if not (state_dir / "ledger.sqlite3").exists():
        return _LimitedRows()
    bounded_limit = _receipt_event_limit(sync_cfg, limit)
    connection = _open_ledger(state_dir)
    try:
        pending_statuses = tuple(sorted(TERMINAL_PENDING_STATUSES))
        pending_placeholders = ", ".join("?" for _ in pending_statuses)
        pending = connection.execute(
            f"""
            select *
              from events
             where status in ({pending_placeholders})
               and canonical_generation is not null
               and generation_id != ''
             order by producer_instance_id, seq
             limit ?
            """,
            (*pending_statuses, bounded_limit + 1),
        ).fetchall()
        selected_pending = pending[:bounded_limit]
        pending_limited = len(pending) > bounded_limit
        remaining = bounded_limit - len(selected_pending)
        finalized = []
        finalized_limited = False
        finalized_cursor = _metadata_event_cursor(
            connection,
            FINALIZED_RECEIPT_CURSOR_KEY,
            max_bytes=MAX_RECEIPT_CURSOR_BYTES,
        )
        if remaining > 0:
            repair_rows = _finalized_receipt_rows(
                connection,
                finalized_cursor,
                remaining + 1,
            )
            finalized_limited = len(repair_rows) > remaining
            finalized = repair_rows[:remaining]
            if finalized:
                finalized_cursor = (
                    finalized[-1]["producer_instance_id"],
                    int(finalized[-1]["seq"]),
                )
                _set_metadata_event_cursor(
                    connection,
                    FINALIZED_RECEIPT_CURSOR_KEY,
                    finalized_cursor,
                )
                connection.commit()
        elif not pending_limited:
            finalized_limited = _has_finalized_receipt_rows(connection)
        return _LimitedRows(
            (
                dict(row)
                for row in [*selected_pending, *finalized]
            ),
            limited=pending_limited or finalized_limited,
            cursor=finalized_cursor,
        )
    finally:
        connection.close()


def bind_pending_receipt_generation(
    sync_cfg,
    generation,
    generation_id,
    files,
    *,
    limit=None,
):
    """Atomically freeze all currently pending events to one sealed generation."""
    state_dir = _state_dir(sync_cfg)
    if not (state_dir / "ledger.sqlite3").exists():
        return _LimitedCount(0)
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation <= 0
        or not isinstance(generation_id, str)
        or not re.fullmatch(r"generation-[0-9a-f]{64}", generation_id)
    ):
        raise ReducerError("receipt generation binding is invalid")
    bounded_limit = _receipt_event_limit(sync_cfg, limit)
    file_map = _sealed_file_map(files)
    connection = _open_ledger(state_dir)
    try:
        connection.execute("begin immediate")
        malformed = connection.execute(
            """
            select count(*) as count
              from events
             where status in (
                 'applied_pending_publish',
                 'noop_pending_publish',
                 'rejected_pending_publish'
             )
               and (
                   (canonical_generation is null and generation_id != '')
                   or
                   (canonical_generation is not null and generation_id = '')
               )
            """
        ).fetchone()
        if int(malformed["count"]):
            raise ReducerError("pending receipt has a partial generation binding")
        scan_cursor = _metadata_event_cursor(
            connection,
            PENDING_RECEIPT_BIND_CURSOR_KEY,
            max_bytes=MAX_RECEIPT_CURSOR_BYTES,
        )
        rows = _unbound_pending_receipt_rows(
            connection,
            scan_cursor,
            bounded_limit + 1,
        )
        inspected = rows[:bounded_limit]
        limited = len(rows) > bounded_limit
        bound = 0
        skipped = False
        for row in inspected:
            if (
                row["event_kind"] == "attachment.blob"
                and not _attachment_effect_is_sealed(row, file_map)
            ):
                skipped = True
                continue
            cursor = connection.execute(
                """
                update events
                   set canonical_generation = ?, generation_id = ?
                 where producer_instance_id = ? and seq = ?
                   and canonical_generation is null
                   and generation_id = ''
                """,
                (
                    int(generation),
                    str(generation_id),
                    row["producer_instance_id"],
                    int(row["seq"]),
                ),
            )
            if cursor.rowcount != 1:
                raise ReducerError(
                    "pending receipt changed during generation binding"
                )
            bound += 1
        if inspected:
            scan_cursor = (
                inspected[-1]["producer_instance_id"],
                int(inspected[-1]["seq"]),
            )
            _set_metadata_event_cursor(
                connection,
                PENDING_RECEIPT_BIND_CURSOR_KEY,
                scan_cursor,
            )
        connection.commit()
        return _LimitedCount(
            bound,
            limited=limited or skipped,
            cursor=scan_cursor,
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _receipt_event_limit(sync_cfg, requested):
    value = (
        sync_cfg.get("max_receipt_events_per_run", MAX_RECEIPT_EVENTS_PER_CALL)
        if requested is None
        else requested
    )
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReducerError("receipt event limit is invalid")
    return min(value, MAX_RECEIPT_EVENTS_PER_CALL)


def _finalized_receipt_rows(connection, cursor, limit):
    where = """
        status in ('applied', 'noop', 'rejected')
        and canonical_generation is not null
        and generation_id != ''
    """
    return _rotating_receipt_rows(connection, where, cursor, limit)


def _unbound_pending_receipt_rows(connection, cursor, limit):
    where = """
        status in (
            'applied_pending_publish',
            'noop_pending_publish',
            'rejected_pending_publish'
        )
        and canonical_generation is null
        and generation_id = ''
    """
    return _rotating_receipt_rows(connection, where, cursor, limit)


def _rotating_receipt_rows(connection, where, cursor, limit):
    if cursor is None:
        return connection.execute(
            f"""
            select *
              from events
             where {where}
             order by producer_instance_id, seq
             limit ?
            """,
            (limit,),
        ).fetchall()
    producer, seq = cursor
    rows = connection.execute(
        f"""
        select *
          from events
         where {where}
           and (
               producer_instance_id > ?
               or (producer_instance_id = ? and seq > ?)
           )
         order by producer_instance_id, seq
         limit ?
        """,
        (producer, producer, seq, limit),
    ).fetchall()
    remaining = limit - len(rows)
    if remaining <= 0:
        return rows
    wrapped = connection.execute(
        f"""
        select *
          from events
         where {where}
           and (
               producer_instance_id < ?
               or (producer_instance_id = ? and seq <= ?)
           )
         order by producer_instance_id, seq
         limit ?
        """,
        (producer, producer, seq, remaining),
    ).fetchall()
    return [*rows, *wrapped]


def _has_finalized_receipt_rows(connection):
    return (
        connection.execute(
            """
            select 1
              from events
             where status in ('applied', 'noop', 'rejected')
               and canonical_generation is not null
               and generation_id != ''
             limit 1
            """
        ).fetchone()
        is not None
    )


def _sealed_file_map(files):
    if not isinstance(files, list):
        raise ReducerError("sealed generation file manifest is invalid")
    mapped = {}
    for item in files:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(item.get("sha256") or ""))
            or isinstance(item.get("bytes"), bool)
            or not isinstance(item.get("bytes"), int)
            or item["bytes"] < 0
        ):
            raise ReducerError("sealed generation file manifest is invalid")
        path = validate_replica_path(item["path"])
        if path in mapped:
            raise ReducerError("sealed generation file manifest has duplicates")
        mapped[path] = {
            "sha256": item["sha256"],
            "bytes": item["bytes"],
        }
    return mapped


def _attachment_effect_is_sealed(row, files):
    canonical_path = str(row["canonical_path"] or "")
    metadata_path = str(row["metadata_path"] or "")
    if not canonical_path or not metadata_path:
        return False
    canonical = files.get(canonical_path)
    metadata = files.get(metadata_path)
    return bool(
        canonical
        and metadata
        and canonical["sha256"] == row["payload_sha256"]
        and canonical["bytes"] == row["payload_bytes"]
        and re.fullmatch(
            r"[0-9a-f]{64}",
            str(row["metadata_sha256"] or ""),
        )
        and metadata["sha256"] == row["metadata_sha256"]
        and metadata["bytes"] == row["metadata_bytes"]
        and row["metadata_bytes"] > 0
    )


def unbound_pending_receipt_count(sync_cfg):
    state_dir = _state_dir(sync_cfg)
    if not (state_dir / "ledger.sqlite3").exists():
        return 0
    connection = _open_ledger(state_dir)
    try:
        row = connection.execute(
            """
            select count(*) as count
              from events
             where status in (
                 'applied_pending_publish',
                 'noop_pending_publish',
                 'rejected_pending_publish'
             )
               and canonical_generation is null
               and generation_id = ''
            """
        ).fetchone()
        return int(row["count"])
    finally:
        connection.close()


def mark_receipts_published(sync_cfg, event_keys):
    state_dir = _state_dir(sync_cfg)
    connection = _open_ledger(state_dir)
    try:
        with connection:
            for (
                producer_instance_id,
                seq,
                final_status,
                generation,
                generation_id,
            ) in event_keys:
                pending_status = f"{final_status}_pending_publish"
                cursor = connection.execute(
                    """
                    update events
                       set status = ?
                     where producer_instance_id = ? and seq = ?
                       and status = ?
                       and canonical_generation = ?
                       and generation_id = ?
                    """,
                    (
                        final_status,
                        producer_instance_id,
                        int(seq),
                        pending_status,
                        int(generation),
                        str(generation_id),
                    ),
                )
                if cursor.rowcount != 1:
                    raise ReducerError(
                        "receipt ledger binding changed before finalization"
                    )
    finally:
        connection.close()


def _open_ledger(state_dir):
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "ledger.sqlite3"
    lock_context = portable_file_lock(
        state_dir / "ledger.lock",
        root=state_dir,
    )
    try:
        lock_context.__enter__()
    except Exception:
        raise
    connection = None
    lock_transferred = False
    try:
        snapshot = _capture_ledger_snapshot(path, state_dir)
        main = snapshot.get("")
        recovered_image = None
        if main is not None:
            recovered_image = _recover_ledger_image(snapshot, state_dir)
            _assert_ledger_snapshot_matches(path, state_dir, snapshot)

        connection = sqlite3.connect(
            ":memory:",
            timeout=30,
            factory=_LedgerConnection,
        )
        connection.row_factory = sqlite3.Row
        if recovered_image is not None:
            connection.deserialize(recovered_image)
        image_digest = sha256_bytes(connection.serialize())
        connection.configure_persistence(
            path,
            state_dir,
            snapshot,
            lock_context,
            image_digest,
        )
        lock_transferred = True

        if main is not None:
            version = validate_ledger_schema(connection)
        else:
            _create_current_ledger_schema(connection)
            connection.execute(
                "insert into metadata(key, value) values ('schema_version', ?)",
                (str(LEDGER_SCHEMA_VERSION),),
            )
            connection.commit()
            version = validate_ledger_schema(
                connection,
                expected_version=LEDGER_SCHEMA_VERSION,
            )
        migrations = {
            1: _migrate_ledger_v1_to_v2,
            2: _migrate_ledger_v2_to_v3,
            3: _migrate_ledger_v3_to_v4,
        }
        while version != LEDGER_SCHEMA_VERSION:
            migrate = migrations.get(version)
            if migrate is None:
                raise ReducerError("unsupported authority ledger schema")
            migrate(connection)
            version = validate_ledger_schema(
                connection,
                expected_version=version + 1,
            )
        connection.execute("pragma journal_mode = MEMORY")
        connection.execute("pragma synchronous = FULL")
        validate_ledger_schema(
            connection,
            expected_version=LEDGER_SCHEMA_VERSION,
        )
        _validate_ledger_integrity(connection)
        return connection
    except Exception:
        if connection is not None:
            connection.close()
        raise
    finally:
        if not lock_transferred:
            lock_context.__exit__(None, None, None)


def _capture_ledger_snapshot(path, state_dir):
    _assert_safe_ledger_files(path)
    snapshot = {}
    for suffix, max_bytes in (
        ("", MAX_LEDGER_BYTES),
        ("-wal", MAX_LEDGER_SIDECAR_BYTES),
        ("-shm", MAX_LEDGER_SIDECAR_BYTES),
        ("-journal", MAX_LEDGER_SIDECAR_BYTES),
    ):
        candidate = Path(f"{path}{suffix}")
        if not os.path.lexists(candidate):
            continue
        try:
            data, identity = read_bounded_regular_file_with_identity(
                candidate,
                max_bytes=max_bytes,
                root=state_dir,
            )
        except FileNotFoundError as exc:
            raise ReducerError(
                "authority ledger files changed during pinned read"
            ) from exc
        except ProtocolError as exc:
            raise ReducerError(str(exc)) from exc
        snapshot[suffix] = {
            "bytes": data,
            "digest": sha256_bytes(data),
            "identity": identity,
        }
    if "" not in snapshot and snapshot:
        raise ReducerError("authority ledger sidecar exists without main database")
    _validate_ledger_sidecars(snapshot)
    return snapshot


def _assert_ledger_snapshot_matches(path, state_dir, expected):
    current = _capture_ledger_snapshot(path, state_dir)
    if set(current) != set(expected):
        raise ReducerError("authority ledger files changed before publish")
    for suffix, component in expected.items():
        observed = current[suffix]
        if (
            observed["identity"] != component["identity"]
            or observed["digest"] != component["digest"]
        ):
            raise ReducerError("authority ledger changed before publish")


def _validate_ledger_sidecars(snapshot):
    wal = snapshot.get("-wal")
    journal = snapshot.get("-journal")
    wal_active = _validate_wal_sidecar(wal["bytes"]) if wal else False
    journal_hot = (
        _validate_journal_sidecar(journal["bytes"]) if journal else False
    )
    shm = snapshot.get("-shm")
    if shm and shm["bytes"]:
        if len(shm["bytes"]) % 32_768:
            raise ReducerError("authority ledger SHM sidecar is malformed")
        if not wal_active:
            raise ReducerError("authority ledger SHM sidecar has no active WAL")
    if wal_active and journal_hot:
        raise ReducerError("authority ledger has conflicting hot sidecars")


def _validate_wal_sidecar(data):
    if not data:
        return False
    if len(data) < 32:
        raise ReducerError("authority ledger WAL sidecar header is truncated")
    try:
        header = struct.unpack(">8I", data[:32])
    except struct.error as exc:
        raise ReducerError("authority ledger WAL sidecar is malformed") from exc
    magic, version, page_size = header[:3]
    if magic not in WAL_MAGIC_VALUES or version != WAL_FORMAT_VERSION:
        raise ReducerError("authority ledger WAL sidecar header is invalid")
    if page_size == 1:
        page_size = 65_536
    if (
        page_size < 512
        or page_size > 65_536
        or page_size & (page_size - 1)
    ):
        raise ReducerError("authority ledger WAL page size is invalid")
    if (len(data) - 32) % (24 + page_size):
        raise ReducerError("authority ledger WAL sidecar frame is truncated")
    return True


def _validate_journal_sidecar(data):
    if not data:
        return False
    if data[:8] == b"\0" * min(8, len(data)):
        return False
    if len(data) < 28 or data[:8] != ROLLBACK_JOURNAL_MAGIC:
        raise ReducerError("authority ledger rollback journal header is invalid")
    try:
        _pages, _nonce, _database_pages, sector_size, page_size = (
            struct.unpack(">5I", data[8:28])
        )
    except struct.error as exc:
        raise ReducerError(
            "authority ledger rollback journal is malformed"
        ) from exc
    if (
        sector_size < 512
        or sector_size > 65_536
        or sector_size & (sector_size - 1)
        or len(data) < sector_size
    ):
        raise ReducerError(
            "authority ledger rollback journal sector size is invalid"
        )
    if page_size == 1:
        page_size = 65_536
    if (
        page_size < 512
        or page_size > 65_536
        or page_size & (page_size - 1)
    ):
        raise ReducerError(
            "authority ledger rollback journal page size is invalid"
        )
    return True


def _recover_ledger_image(snapshot, state_dir):
    recovery_dir = Path(
        tempfile.mkdtemp(prefix=".ledger-recovery-", dir=state_dir)
    )
    os.chmod(recovery_dir, 0o700)
    try:
        private_main = recovery_dir / "ledger.sqlite3"
        for suffix, component in snapshot.items():
            portable_atomic_write(
                Path(f"{private_main}{suffix}"),
                component["bytes"],
                root=recovery_dir,
            )
        return _recover_private_ledger(private_main)
    finally:
        portable_rmtree(recovery_dir, root=state_dir)


def _recover_private_ledger(private_main):
    disk = None
    memory = None
    try:
        disk = sqlite3.connect(private_main, timeout=30)
        disk.row_factory = sqlite3.Row
        disk.execute("pragma busy_timeout = 0")
        disk.execute("pragma schema_version").fetchone()
        if os.path.lexists(Path(f"{private_main}-wal")):
            checkpoint = disk.execute(
                "pragma wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise ReducerError("authority ledger WAL checkpoint is busy")
            journal_mode = disk.execute(
                "pragma journal_mode = DELETE"
            ).fetchone()
            if journal_mode is None or str(journal_mode[0]).lower() != "delete":
                raise ReducerError(
                    "authority ledger WAL could not be normalized"
                )
        _validate_ledger_integrity(disk)
        validate_ledger_schema(disk)
        memory = sqlite3.connect(":memory:")
        memory.row_factory = sqlite3.Row
        disk.backup(memory)
        _validate_ledger_integrity(memory)
        validate_ledger_schema(memory)
        return memory.serialize()
    except ReducerError:
        raise
    except sqlite3.Error as exc:
        raise ReducerError("authority ledger private recovery failed") from exc
    finally:
        if memory is not None:
            memory.close()
        if disk is not None:
            disk.close()


def _validate_ledger_integrity(connection):
    row = connection.execute("pragma integrity_check(1)").fetchone()
    if row is None or str(row[0]).lower() != "ok":
        raise ReducerError("authority ledger integrity check failed")


def _create_current_ledger_schema(connection):
    connection.executescript(
        """
        create table metadata (
            key text primary key,
            value text not null
        );
        create table producers (
            producer_instance_id text primary key,
            device_id text not null,
            next_seq integer not null,
            blocked_code text not null default '',
            updated_at text not null
        );
        create table streams (
            producer_instance_id text not null,
            stream_id text not null,
            stream_epoch text not null,
            committed_cursor integer not null,
            mirror_path text not null,
            session_id text not null,
            agent text not null,
            primary key (producer_instance_id, stream_id, stream_epoch)
        );
        create table events (
            producer_instance_id text not null,
            seq integer not null,
            event_id text not null unique,
            event_sha256 text not null,
            device_id text not null,
            status text not null,
            code text not null,
            bundle_path text not null,
            event_kind text not null,
            stream_id text not null,
            stream_epoch text not null,
            cursor_start integer not null,
            cursor_end integer not null,
            mirror_path text not null default '',
            mirror_before_size integer,
            mirror_append_size integer,
            mirror_append_sha256 text not null default '',
            canonical_path text not null default '',
            metadata_path text not null default '',
            payload_sha256 text not null default '',
            payload_bytes integer not null default 0,
            metadata_sha256 text not null default '',
            metadata_bytes integer not null default 0,
            created_at text not null,
            processed_at text not null default '',
            canonical_generation integer,
            generation_id text not null default '',
            primary key (producer_instance_id, seq)
        );
        """
    )


def validate_ledger_schema(connection, expected_version=None):
    """Validate one ledger schema without creating or altering any object."""
    tables = {
        str(row[0])
        for row in connection.execute(
            """
            select name
              from sqlite_master
             where type = 'table' and name not like 'sqlite_%'
            """
        ).fetchall()
    }
    if "metadata" not in tables:
        raise ReducerError("authority ledger schema is missing metadata")
    metadata_info = connection.execute("pragma table_info(metadata)").fetchall()
    if (
        {str(row[1]) for row in metadata_info} != set(METADATA_COLUMNS)
        or _primary_key_columns(metadata_info) != LEDGER_PRIMARY_KEYS["metadata"]
    ):
        raise ReducerError("authority ledger metadata schema is invalid")
    row = connection.execute(
        "select value from metadata where key = 'schema_version'"
    ).fetchone()
    if row is None:
        raise ReducerError("authority ledger schema version is missing")
    raw_version = row[0]
    try:
        version = int(raw_version)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ReducerError("authority ledger schema version is invalid") from exc
    if str(raw_version) != str(version) or version not in LEDGER_SCHEMA_CONTRACTS:
        raise ReducerError("unsupported authority ledger schema")
    if expected_version is not None and version != int(expected_version):
        raise ReducerError("authority ledger schema version changed unexpectedly")

    contract = LEDGER_SCHEMA_CONTRACTS[version]
    if tables != set(contract):
        raise ReducerError("authority ledger schema tables are incomplete")
    for table, expected_columns in contract.items():
        info = connection.execute(f"pragma table_info({table})").fetchall()
        columns = {str(column[1]) for column in info}
        if columns != set(expected_columns) or len(info) != len(expected_columns):
            raise ReducerError(
                f"authority ledger schema columns are invalid for {table}"
            )
        if _primary_key_columns(info) != LEDGER_PRIMARY_KEYS[table]:
            raise ReducerError(
                f"authority ledger schema primary key is invalid for {table}"
            )
    if not _events_have_unique_event_id(connection):
        raise ReducerError("authority ledger schema event_id index is invalid")
    return version


def _primary_key_columns(table_info):
    keyed = sorted(
        (int(row[5]), str(row[1]))
        for row in table_info
        if int(row[5]) > 0
    )
    return tuple(name for _position, name in keyed)


def _events_have_unique_event_id(connection):
    for index in connection.execute("pragma index_list(events)").fetchall():
        if not int(index[2]) or (len(index) > 4 and int(index[4])):
            continue
        name = str(index[1]).replace("'", "''")
        columns = tuple(
            str(row[2])
            for row in connection.execute(
                f"pragma index_info('{name}')"
            ).fetchall()
        )
        if columns == ("event_id",):
            return True
    return False


def _assert_safe_ledger_files(path):
    for candidate, label in (
        (path, "authority ledger"),
        (Path(f"{path}-wal"), "authority ledger WAL sidecar"),
        (Path(f"{path}-shm"), "authority ledger SHM sidecar"),
        (Path(f"{path}-journal"), "authority ledger rollback journal"),
    ):
        if not os.path.lexists(candidate):
            continue
        info = os.lstat(candidate)
        attributes = int(getattr(info, "st_file_attributes", 0) or 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(info.st_mode):
            raise ReducerError(f"{label} is a symlink")
        if attributes & reparse_flag:
            raise ReducerError(f"{label} is a reparse point")
        if not stat.S_ISREG(info.st_mode):
            raise ReducerError(f"{label} is not a regular file")
        if int(getattr(info, "st_nlink", 1)) != 1:
            raise ReducerError(f"{label} uses a hard link")


def _migrate_ledger_v1_to_v2(connection):
    try:
        connection.execute("begin immediate")
        validate_ledger_schema(connection, expected_version=1)
        for table in ("streams", "events"):
            rows = connection.execute(
                f"""
                select rowid, mirror_path
                  from {table}
                 where coalesce(mirror_path, '') != ''
                """
            ).fetchall()
            for row in rows:
                locator = _normalize_mirror_locator(row["mirror_path"])
                connection.execute(
                    f"update {table} set mirror_path = ? where rowid = ?",
                    (locator, int(row["rowid"])),
                )
        connection.execute(
            "update metadata set value = ? where key = 'schema_version'",
            ("2",),
        )
        validate_ledger_schema(connection, expected_version=2)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _migrate_ledger_v2_to_v3(connection):
    try:
        connection.execute("begin immediate")
        validate_ledger_schema(connection, expected_version=2)
        additions = (
            ("canonical_path", "text not null default ''"),
            ("metadata_path", "text not null default ''"),
            ("payload_sha256", "text not null default ''"),
            ("payload_bytes", "integer not null default 0"),
        )
        for name, declaration in additions:
            connection.execute(
                f"alter table events add column {name} {declaration}"
            )
        connection.execute(
            "update metadata set value = ? where key = 'schema_version'",
            ("3",),
        )
        validate_ledger_schema(connection, expected_version=3)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _migrate_ledger_v3_to_v4(connection):
    try:
        connection.execute("begin immediate")
        validate_ledger_schema(connection, expected_version=3)
        additions = (
            ("metadata_sha256", "text not null default ''"),
            ("metadata_bytes", "integer not null default 0"),
        )
        for name, declaration in additions:
            connection.execute(
                f"alter table events add column {name} {declaration}"
            )
        connection.execute(
            "update metadata set value = ? where key = 'schema_version'",
            ("4",),
        )
        validate_ledger_schema(connection, expected_version=4)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _discover_candidates(connection, sync_cfg, result, state_dir):
    candidates = []
    contexts = []
    inboxes = sync_cfg.get("inboxes") or []
    if not isinstance(inboxes, list):
        raise ReducerError("beacon_sync.inboxes must be a list")
    for binding in inboxes:
        if not isinstance(binding, dict) or set(binding) != {"device_id", "path"}:
            raise ReducerError("each authority inbox requires device_id and path")
        expected_device = str(binding["device_id"] or "").strip()
        inbox_text = str(binding["path"] or "").strip()
        if not expected_device or not inbox_text:
            continue
        inbox = Path(os.path.abspath(os.path.expanduser(inbox_text)))
        if not inbox.is_dir():
            result["deferred"] += 1
            continue
        trusted_producers = _identity_producers(inbox, expected_device)
        if trusted_producers is None:
            result["quarantined"] += 1
            _write_quarantine(
                state_dir,
                "device-binding",
                {
                    "code": "device_binding_mismatch",
                    "device_id": expected_device,
                    "inbox": str(inbox),
                },
            )
            continue
        events_root = inbox / "v1" / "events"
        unexpected_entry = _unexpected_producer_entry(
            events_root,
            trusted_producers,
        )
        if unexpected_entry is not None:
            result["quarantined"] += 1
            _write_quarantine(
                state_dir,
                "producer-binding",
                {
                    "code": "producer_binding_mismatch",
                    "producer_instance_ids": sorted(trusted_producers),
                    "unexpected_entry": unexpected_entry[:200],
                    "inbox": str(inbox),
                },
            )
            continue
        for expected_producer in sorted(trusted_producers):
            producer_root = events_root / expected_producer
            if not producer_root.is_dir():
                continue
            row = connection.execute(
                """
                select device_id, next_seq, blocked_code
                  from producers
                 where producer_instance_id = ?
                """,
                (expected_producer,),
            ).fetchone()
            if row is not None and row["device_id"] != expected_device:
                result["quarantined"] += 1
                _write_quarantine(
                    state_dir,
                    f"{expected_producer}-{expected_device}",
                    {
                        "code": "producer_device_binding_conflict",
                        "producer_instance_id": expected_producer,
                        "owner_device_id": row["device_id"],
                        "claimed_device_id": expected_device,
                        "inbox": str(inbox),
                    },
                )
                continue
            if row is not None and row["blocked_code"]:
                result["blocked"] += 1
                continue
            contexts.append(
                {
                    "inbox": inbox,
                    "producer_root": producer_root,
                    "device_id": expected_device,
                    "producer": expected_producer,
                    "next_seq": int(row["next_seq"]) if row is not None else 1,
                    "stopped": False,
                }
            )

    contexts.sort(key=lambda item: item["producer"])
    rotation_cursor = _producer_rotation_cursor(connection)
    if contexts and rotation_cursor is not None:
        split = next(
            (
                index
                for index, context in enumerate(contexts)
                if context["producer"] > rotation_cursor
            ),
            0,
        )
        contexts = [*contexts[split:], *contexts[:split]]

    max_events = int(sync_cfg.get("max_events_per_run", 32))
    max_object = int(sync_cfg.get("max_object_bytes", 32 * 1024 * 1024))
    byte_budget = max(64 * 1024 * 1024, max_object * 2)
    object_bytes = 0
    sequence_groups = 0
    last_scheduled_producer = None
    while sequence_groups < max_events:
        made_progress = False
        for context in contexts:
            if context["stopped"] or sequence_groups >= max_events:
                continue
            seq = context["next_seq"]
            try:
                matches, has_entries = _sequence_ready_paths(
                    context["producer_root"],
                    seq,
                )
            except (IngressDeferred, OSError) as exc:
                result["deferred"] += 1
                result["quarantined"] += 1
                context["stopped"] = True
                _write_quarantine(
                    state_dir,
                    f"{context['producer']}-bundle-scan",
                    {
                        "code": "producer_bundle_scan_deferred",
                        "producer_instance_id": context["producer"],
                        "seq": seq,
                        "reason": str(exc)[:500],
                        "inbox": str(context["inbox"]),
                    },
                )
                continue
            if not matches:
                if has_entries:
                    result["deferred"] += 1
                context["stopped"] = True
                continue
            made_progress = True
            sequence_groups += 1
            last_scheduled_producer = context["producer"]
            if len(matches) > 2:
                result["blocked"] += 1
                context["stopped"] = True
                _write_quarantine(
                    state_dir,
                    f"{context['producer']}-{seq}",
                    {
                        "code": "too_many_sequence_candidates",
                        "producer_instance_id": context["producer"],
                        "seq": seq,
                    },
                )
                continue

            group = []
            failed = False
            for ready_path in matches:
                try:
                    candidate = _load_candidate(
                        context["inbox"],
                        ready_path.parent,
                        context["device_id"],
                        context["producer"],
                        sync_cfg,
                        max_payload_bytes=byte_budget - object_bytes,
                    )
                except IngressDeferred:
                    result["deferred"] += 1
                    failed = True
                    break
                except FileNotFoundError:
                    result["deferred"] += 1
                    failed = True
                    break
                except ProtocolError as exc:
                    message = str(exc)
                    counter = (
                        "quarantined"
                        if any(
                            marker in message
                            for marker in (
                                "device",
                                "producer",
                                "identity",
                                "symlink",
                            )
                        )
                        else "blocked"
                    )
                    result[counter] += 1
                    _write_quarantine(
                        state_dir,
                        ready_path.parent.name,
                        {
                            "code": "invalid_bundle",
                            "reason": message[:500],
                            "device_id": context["device_id"],
                        },
                    )
                    failed = True
                    break
                group.append(candidate)
                object_bytes += (
                    len(candidate["payload_bytes"])
                    if candidate["payload_bytes"] is not None
                    else 0
                )
            if failed:
                context["stopped"] = True
                continue
            candidates.extend(group)
            if len(group) != 1:
                context["stopped"] = True
            else:
                context["next_seq"] += 1
        if not made_progress:
            break
    if last_scheduled_producer is not None:
        _store_producer_rotation_cursor(
            connection,
            last_scheduled_producer,
        )
    candidates.sort(
        key=lambda item: (item["event"]["producer_instance_id"], item["event"]["seq"])
    )
    return candidates, contexts


def _unexpected_producer_entry(events_root, trusted_producers):
    if not events_root.is_dir():
        return None
    with os.scandir(events_root) as entries:
        for entry in islice(entries, MAX_IDENTITY_REGISTRY_ENTRIES + 1):
            if entry.name not in trusted_producers:
                return entry.name
    return None


def _bounded_bundle_entries(producer_root):
    limit = int(MAX_BUNDLE_ENTRIES_PER_PRODUCER)
    if limit <= 0:
        raise IngressDeferred("producer bundle entry limit is invalid")
    initial = os.stat(producer_root, follow_symlinks=False)
    if not stat.S_ISDIR(initial.st_mode):
        raise OSError("producer bundle root is not a directory")
    with os.scandir(producer_root) as scanner:
        sampled = list(islice(scanner, limit + 1))
    if len(sampled) > limit:
        raise IngressDeferred("producer bundle entry scan is incomplete")
    current = os.stat(producer_root, follow_symlinks=False)
    if (
        (int(current.st_dev), int(current.st_ino))
        != (int(initial.st_dev), int(initial.st_ino))
        or int(current.st_mtime_ns) != int(initial.st_mtime_ns)
    ):
        raise IngressDeferred("producer bundle directory changed during scan")
    return tuple(
        sorted((Path(entry.path) for entry in sampled), key=lambda path: path.name)
    )


def _sequence_ready_paths(producer_root, seq):
    sequence_prefix = f"{seq:020d}-event-"
    sequence_root = producer_root / event_sequence_directory_name(seq)
    if os.path.lexists(sequence_root):
        initial = os.lstat(sequence_root)
        attributes = int(getattr(initial, "st_file_attributes", 0) or 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            stat.S_ISLNK(initial.st_mode)
            or attributes & reparse_flag
            or not stat.S_ISDIR(initial.st_mode)
        ):
            raise OSError("producer sequence root is not a safe directory")
        with os.scandir(sequence_root) as scanner:
            sampled = list(islice(scanner, 3))
        current = os.lstat(sequence_root)
        if (
            (int(current.st_dev), int(current.st_ino))
            != (int(initial.st_dev), int(initial.st_ino))
            or int(current.st_mtime_ns) != int(initial.st_mtime_ns)
        ):
            raise IngressDeferred(
                "producer sequence directory changed during scan"
            )
        matches = [
            Path(entry.path) / "ready.json"
            for entry in sampled
            if entry.name.startswith(sequence_prefix)
            and os.path.lexists(Path(entry.path) / "ready.json")
        ]
        return matches, True

    bundle_entries = _bounded_bundle_entries(producer_root)
    matches = [
        bundle / "ready.json"
        for bundle in bundle_entries
        if bundle.name.startswith(sequence_prefix)
        and os.path.lexists(bundle / "ready.json")
    ][:3]
    return matches, bool(bundle_entries)


def _producer_rotation_cursor(connection):
    row = connection.execute(
        "select value from metadata where key = ?",
        (PRODUCER_ROTATION_CURSOR_KEY,),
    ).fetchone()
    if row is None or not isinstance(row["value"], str):
        return None
    value = row["value"]
    try:
        if str(uuid.UUID(value)) != value:
            return None
    except (AttributeError, TypeError, ValueError):
        return None
    return value


def _store_producer_rotation_cursor(connection, producer):
    try:
        if str(uuid.UUID(producer)) != producer:
            raise ValueError("non-canonical producer UUID")
    except (AttributeError, TypeError, ValueError) as exc:
        raise ReducerError("producer rotation cursor is invalid") from exc
    connection.execute(
        """
        insert into metadata(key, value)
        values (?, ?)
        on conflict(key) do update set value = excluded.value
        """,
        (PRODUCER_ROTATION_CURSOR_KEY, producer),
    )
    connection.commit()


def _discover_pending_attachment_candidates(
    connection,
    sync_cfg,
    contexts,
    candidates,
    result,
    state_dir,
):
    """Reload consumed attachment evidence needed to repair an unbound effect."""
    existing = {
        (item["event"]["producer_instance_id"], item["event"]["seq"])
        for item in candidates
    }
    trusted = {
        (context["device_id"], context["producer"]): context
        for context in contexts
    }
    limit = max(1, int(sync_cfg.get("max_events_per_run", 32)))
    scan_limit = min(4096, max(limit, limit * PENDING_ATTACHMENT_SCAN_MULTIPLIER))
    cursor = _pending_attachment_scan_cursor(connection)
    rows = _pending_attachment_scan_rows(connection, cursor, scan_limit)
    max_object = int(sync_cfg.get("max_object_bytes", 32 * 1024 * 1024))
    byte_budget = max(64 * 1024 * 1024, max_object * 2)
    loaded_bytes = 0
    loaded = []
    inspected_cursor = None
    for row in rows:
        if len(loaded) >= limit:
            break
        identity = (row["producer_instance_id"], int(row["seq"]))
        if identity in existing:
            inspected_cursor = identity
            continue
        context = trusted.get((row["device_id"], row["producer_instance_id"]))
        if context is None:
            inspected_cursor = identity
            continue
        if (
            identity[1] <= 0
            or not re.fullmatch(r"event-[0-9a-f]{64}", str(row["event_id"] or ""))
        ):
            result["blocked"] += 1
            _write_quarantine(
                state_dir,
                f"{identity[0]}-{identity[1]}",
                {
                    "code": "invalid_pending_attachment_identity",
                    "producer_instance_id": identity[0],
                    "seq": identity[1],
                },
            )
            inspected_cursor = identity
            continue
        try:
            candidate = None
            bundle_name = f"{identity[1]:020d}-{row['event_id']}"
            bundle_paths = (
                context["producer_root"]
                / event_sequence_directory_name(identity[1])
                / bundle_name,
                context["producer_root"] / bundle_name,
            )
            for bundle in bundle_paths:
                try:
                    candidate = _load_candidate(
                        context["inbox"],
                        bundle,
                        context["device_id"],
                        context["producer"],
                        sync_cfg,
                        max_payload_bytes=byte_budget - loaded_bytes,
                    )
                    break
                except FileNotFoundError:
                    continue
            if candidate is None:
                raise FileNotFoundError(bundle_name)
        except FileNotFoundError:
            result["deferred"] += 1
            inspected_cursor = identity
            continue
        except IngressDeferred:
            result["deferred"] += 1
            break
        except ProtocolError as exc:
            result["blocked"] += 1
            _write_quarantine(
                state_dir,
                f"{identity[0]}-{identity[1]}",
                {
                    "code": "invalid_pending_attachment_bundle",
                    "producer_instance_id": identity[0],
                    "seq": identity[1],
                    "reason": str(exc)[:500],
                },
            )
            inspected_cursor = identity
            continue
        if candidate["event_sha256"] != row["event_sha256"]:
            result["blocked"] += 1
            _block_producer(
                connection,
                identity[0],
                row["device_id"],
                "sequence_hash_conflict",
                _iso_utc(datetime.now(timezone.utc)),
            )
            _write_quarantine(
                state_dir,
                f"{identity[0]}-{identity[1]}",
                {
                    "code": "sequence_hash_conflict",
                    "producer_instance_id": identity[0],
                    "seq": identity[1],
                },
            )
            inspected_cursor = identity
            continue
        loaded.append(candidate)
        loaded_bytes += len(candidate["payload_bytes"] or b"")
        inspected_cursor = identity
    if inspected_cursor is not None:
        _store_pending_attachment_scan_cursor(connection, inspected_cursor)
    elif not rows:
        _store_pending_attachment_scan_cursor(connection, None)
    return loaded


def _pending_attachment_scan_cursor(connection):
    return _metadata_event_cursor(
        connection,
        PENDING_ATTACHMENT_SCAN_CURSOR_KEY,
        max_bytes=MAX_PENDING_ATTACHMENT_CURSOR_BYTES,
    )


def _metadata_event_cursor(connection, key, *, max_bytes):
    row = connection.execute(
        "select value from metadata where key = ?",
        (key,),
    ).fetchone()
    if row is None:
        return None
    try:
        raw_value = row["value"]
        if (
            not isinstance(raw_value, str)
            or len(raw_value) > max_bytes
            or not raw_value.isascii()
        ):
            return None
        value = decode_bounded_json(
            raw_value.encode("ascii"),
            max_bytes=max_bytes,
        )
    except (ProtocolError, UnicodeEncodeError):
        return None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[0], str)
        or not value[0]
        or isinstance(value[1], bool)
        or not isinstance(value[1], int)
        or value[1] <= 0
    ):
        return None
    try:
        if str(uuid.UUID(value[0])) != value[0]:
            return None
    except (AttributeError, TypeError, ValueError):
        return None
    return value[0], value[1]


def _pending_attachment_scan_rows(connection, cursor, limit):
    where = """
        event_kind = 'attachment.blob'
        and status in (
            'applied_pending_publish',
            'noop_pending_publish'
        )
        and canonical_generation is null
        and generation_id = ''
    """
    if cursor is None:
        return connection.execute(
            f"""
            select *
              from events
             where {where}
             order by producer_instance_id, seq
             limit ?
            """,
            (limit,),
        ).fetchall()
    producer, seq = cursor
    rows = connection.execute(
        f"""
        select *
          from events
         where {where}
           and (
               producer_instance_id > ?
               or (producer_instance_id = ? and seq > ?)
           )
         order by producer_instance_id, seq
         limit ?
        """,
        (producer, producer, seq, limit),
    ).fetchall()
    remaining = limit - len(rows)
    if remaining <= 0:
        return rows
    wrapped = connection.execute(
        f"""
        select *
          from events
         where {where}
           and (
               producer_instance_id < ?
               or (producer_instance_id = ? and seq <= ?)
           )
         order by producer_instance_id, seq
         limit ?
        """,
        (producer, producer, seq, remaining),
    ).fetchall()
    return [*rows, *wrapped]


def _store_pending_attachment_scan_cursor(connection, cursor):
    with connection:
        _set_metadata_event_cursor(
            connection,
            PENDING_ATTACHMENT_SCAN_CURSOR_KEY,
            cursor,
        )


def _set_metadata_event_cursor(connection, key, cursor):
    value = "" if cursor is None else json.dumps(list(cursor), separators=(",", ":"))
    connection.execute(
        """
        insert into metadata(key, value)
        values (?, ?)
        on conflict(key) do update set value = excluded.value
        """,
        (key, value),
    )


def _identity_producers(inbox, expected_device):
    current_path = inbox / "v1" / "identity.json"
    if not current_path.is_file():
        return None
    try:
        current = _load_identity_document(
            current_path,
            inbox,
            expected_device,
        )
        registry = inbox / "v1" / "identities"
        if not registry.exists():
            return {current}
        registry_info = os.lstat(registry)
        if stat.S_ISLNK(registry_info.st_mode) or not stat.S_ISDIR(
            registry_info.st_mode
        ):
            return None
        with os.scandir(registry) as scanner:
            entries = list(
                islice(scanner, MAX_IDENTITY_REGISTRY_ENTRIES + 1)
            )
        if len(entries) > MAX_IDENTITY_REGISTRY_ENTRIES:
            return None
        entries.sort(key=lambda entry: entry.name)
        producers = set()
        for entry in entries:
            entry_path = Path(entry.path)
            if entry_path.suffix != ".json":
                return None
            producer = _load_identity_document(
                entry_path,
                inbox,
                expected_device,
            )
            if entry_path.stem != producer:
                return None
            producers.add(producer)
        if current not in producers:
            return None
        return producers
    except (
        json.JSONDecodeError,
        UnicodeDecodeError,
        ProtocolError,
        OSError,
        TypeError,
        ValueError,
    ):
        return None


def _load_identity_document(path, inbox, expected_device):
    identity_bytes = read_bounded_regular_file(
        path,
        max_bytes=16 * 1024,
        root=inbox,
    )
    identity = decode_bounded_json(identity_bytes, max_bytes=16 * 1024)
    required = {
        "protocol",
        "schema_version",
        "device_id",
        "producer_instance_id",
    }
    if (
        not isinstance(identity, dict)
        or set(identity) != required
        or identity["protocol"] != "agent-memory-beacon-sync-identity"
        or identity["schema_version"] != 1
        or identity["device_id"] != expected_device
        or canonical_json_bytes(identity) != identity_bytes
    ):
        raise ProtocolError("producer identity is invalid")
    producer = identity["producer_instance_id"]
    if not isinstance(producer, str) or str(uuid.UUID(producer)) != producer:
        raise ProtocolError("producer identity is invalid")
    return producer


def _load_candidate(
    inbox,
    bundle,
    expected_device,
    expected_producer,
    sync_cfg,
    *,
    max_payload_bytes=None,
):
    max_event = int(sync_cfg.get("max_event_json_bytes", MAX_EVENT_JSON_BYTES))
    max_object = int(sync_cfg.get("max_object_bytes", 32 * 1024 * 1024))
    event_bytes = read_bounded_regular_file(
        bundle / "event.json",
        max_bytes=max_event,
        root=inbox,
    )
    ready_bytes = read_bounded_regular_file(
        bundle / "ready.json",
        max_bytes=16 * 1024,
        root=inbox,
    )
    event = decode_bounded_json(event_bytes, max_bytes=max_event)
    ready = decode_bounded_json(ready_bytes, max_bytes=16 * 1024)
    if not isinstance(event, dict):
        raise ProtocolError("event must be an object")
    if canonical_json_bytes(event) != event_bytes:
        raise ProtocolError("event JSON is not canonical")
    if canonical_json_bytes(ready) != ready_bytes:
        raise ProtocolError("ready JSON is not canonical")
    if event.get("protocol") != PROTOCOL_EVENT:
        raise ProtocolError("unsupported event protocol or schema")
    legacy_attachment = bool(
        event.get("event_kind") == "attachment.blob"
        and event.get("schema_version") == LEGACY_ATTACHMENT_SCHEMA_VERSION
    )
    if legacy_attachment:
        validate_legacy_attachment_event(
            event,
            expected_device_id=expected_device,
        )
    else:
        validate_event(
            event,
            expected_device_id=expected_device,
            allow_unknown_kind=True,
        )
    if event["producer_instance_id"] != expected_producer:
        raise ProtocolError("event producer does not match inbox identity")
    flat_layout = bundle.parent.name == expected_producer
    sharded_layout = (
        bundle.parent.parent.name == expected_producer
        and bundle.parent.name
        == event_sequence_directory_name(event["seq"])
    )
    if not (flat_layout or sharded_layout):
        raise ProtocolError("bundle producer directory does not match inbox identity")
    if legacy_attachment:
        validate_legacy_attachment_ready(ready, event, event_bytes)
    else:
        validate_ready(
            ready,
            event,
            event_bytes,
            allow_unknown_kind=True,
        )
    if bundle.name != event_bundle_name_allow_unknown(event):
        raise ProtocolError("bundle name does not match event identity")

    payload_size = int(event["payload"]["bytes"])
    max_chunk = int(sync_cfg.get("max_chunk_bytes", 16 * 1024 * 1024))
    max_gap = int(sync_cfg.get("max_gap_bytes", 512 * 1024 * 1024))
    max_attachment = int(
        sync_cfg.get("max_attachment_bytes", 32 * 1024 * 1024)
    )
    if max_chunk <= 0 or max_gap <= 0 or max_attachment <= 0:
        raise ProtocolError("authority payload limits are invalid")
    if (
        event["event_kind"] == "transcript.chunk"
        and payload_size > min(max_chunk, max_object)
    ):
        raise ProtocolError("transcript chunk exceeds authority limit")
    if event["event_kind"] == "transcript.gap" and payload_size > max_gap:
        raise ProtocolError("transcript gap exceeds authority limit")
    if (
        event["event_kind"] == "attachment.blob"
        and payload_size > min(max_attachment, max_object)
    ):
        raise ProtocolError("attachment blob exceeds authority limit")

    payload_bytes = None
    if ready["object_count"]:
        if (
            max_payload_bytes is not None
            and event["payload"]["bytes"] > max(0, int(max_payload_bytes))
        ):
            raise IngressDeferred("event exceeds the current ingress byte budget")
        object_path = bundle / "objects" / event["payload"]["sha256"]
        payload_bytes = read_bounded_regular_file(
            object_path,
            max_bytes=max_object,
            root=inbox,
        )
        if len(payload_bytes) != event["payload"]["bytes"]:
            raise ProtocolError("payload size does not match event")
        if sha256_bytes(payload_bytes) != event["payload"]["sha256"]:
            raise ProtocolError("payload hash does not match event")
    return {
        "inbox": str(inbox),
        "bundle": str(bundle),
        "event": event,
        "event_bytes": event_bytes,
        "event_sha256": sha256_bytes(event_bytes),
        "payload_bytes": payload_bytes,
    }


def event_bundle_name_allow_unknown(event):
    return f"{event['seq']:020d}-{event['event_id']}"


def _record_duplicates_and_conflicts(connection, candidates, result, state_dir):
    current_groups = {}
    for candidate in candidates:
        event = candidate["event"]
        identity = (event["producer_instance_id"], event["seq"])
        current_groups.setdefault(identity, []).append(candidate)
    for (producer, seq), group in current_groups.items():
        hashes = {candidate["event_sha256"] for candidate in group}
        if len(hashes) > 1:
            for candidate in group:
                candidate["conflict"] = True
            result["blocked"] += 1
            event = group[0]["event"]
            _block_producer(
                connection,
                producer,
                event["device_id"],
                "sequence_hash_conflict",
                _iso_utc(datetime.now(timezone.utc)),
            )
            _write_quarantine(
                state_dir,
                f"{producer}-{seq}",
                {
                    "code": "sequence_hash_conflict",
                    "producer_instance_id": producer,
                    "seq": seq,
                    "event_sha256": sorted(hashes),
                },
            )
        elif len(group) > 1:
            for candidate in group[1:]:
                candidate["duplicate"] = True
                result["duplicates"] += 1

    for candidate in candidates:
        if candidate.get("duplicate") or candidate.get("conflict"):
            continue
        event = candidate["event"]
        row = connection.execute(
            """
            select event_sha256, status
              from events
             where producer_instance_id = ? and seq = ?
            """,
            (event["producer_instance_id"], event["seq"]),
        ).fetchone()
        if row is None:
            continue
        if (
            row["event_sha256"] == candidate["event_sha256"]
            and row["status"]
            not in {"validated", "mirror_staged", "attachment_staged"}
        ):
            candidate["duplicate"] = True
            result["duplicates"] += 1
        elif row["event_sha256"] != candidate["event_sha256"]:
            candidate["conflict"] = True
            result["blocked"] += 1
            _block_producer(
                connection,
                event["producer_instance_id"],
                event["device_id"],
                "sequence_hash_conflict",
                _iso_utc(datetime.now(timezone.utc)),
            )
            _write_quarantine(
                state_dir,
                f"{event['producer_instance_id']}-{event['seq']}",
                {
                    "code": "sequence_hash_conflict",
                    "producer_instance_id": event["producer_instance_id"],
                    "seq": event["seq"],
                },
            )


def _pending_attachment_repair_candidates(connection, candidates):
    repairs = []
    seen = set()
    for candidate in candidates:
        if candidate.get("conflict"):
            continue
        event = candidate["event"]
        identity = (event["producer_instance_id"], event["seq"])
        if identity in seen or event["event_kind"] != "attachment.blob":
            continue
        row = connection.execute(
            """
            select *
              from events
             where producer_instance_id = ? and seq = ?
            """,
            identity,
        ).fetchone()
        if (
            row is None
            or row["event_sha256"] != candidate["event_sha256"]
            or row["status"]
            not in {
                "applied_pending_publish",
                "noop_pending_publish",
            }
            or row["canonical_generation"] is not None
            or row["generation_id"]
        ):
            continue
        repairs.append((candidate, dict(row)))
        seen.add(identity)
    return repairs


def _repair_pending_attachment_effects(connection, vault, repairs):
    repaired = 0
    for candidate, row in repairs:
        effect = _write_attachment_effects(vault, candidate)
        changed = bool(
            effect["blob_created"]
            or effect["metadata_created"]
            or row["canonical_path"] != effect["canonical_path"]
            or row["metadata_path"] != effect["metadata_path"]
            or row["metadata_sha256"] != effect["metadata_sha256"]
            or row["metadata_bytes"] != effect["metadata_bytes"]
        )
        with connection:
            cursor = connection.execute(
                """
                update events
                   set canonical_path = ?,
                       metadata_path = ?,
                       metadata_sha256 = ?,
                       metadata_bytes = ?
                 where producer_instance_id = ? and seq = ?
                   and event_sha256 = ?
                   and status in (
                       'applied_pending_publish',
                       'noop_pending_publish'
                   )
                   and canonical_generation is null
                   and generation_id = ''
                """,
                (
                    effect["canonical_path"],
                    effect["metadata_path"],
                    effect["metadata_sha256"],
                    effect["metadata_bytes"],
                    candidate["event"]["producer_instance_id"],
                    candidate["event"]["seq"],
                    candidate["event_sha256"],
                ),
            )
            if cursor.rowcount != 1:
                raise ReducerError(
                    "pending attachment changed during effect repair"
                )
        repaired += int(changed)
    return repaired


def _next_contiguous_batch(connection, candidates, sync_cfg, result):
    grouped = {}
    for candidate in candidates:
        if candidate.get("duplicate") or candidate.get("conflict"):
            continue
        producer = candidate["event"]["producer_instance_id"]
        grouped.setdefault(producer, {})[candidate["event"]["seq"]] = candidate
    max_events = int(sync_cfg.get("max_events_per_run", 32))
    batch = []
    for producer in sorted(grouped):
        by_seq = grouped[producer]
        first = by_seq[min(by_seq)]
        row = connection.execute(
            """
            select device_id, next_seq, blocked_code
              from producers
             where producer_instance_id = ?
            """,
            (producer,),
        ).fetchone()
        if row is None:
            next_seq = 1
            blocked_code = ""
        else:
            if row["device_id"] != first["event"]["device_id"]:
                result["blocked"] += 1
                continue
            next_seq = int(row["next_seq"])
            blocked_code = row["blocked_code"]
        if blocked_code:
            result["blocked"] += 1
            continue
        for seq in sorted(by_seq):
            if seq < next_seq:
                result["blocked"] += 1
            elif seq > next_seq:
                result["deferred"] += 1
        while next_seq in by_seq and len(batch) < max_events:
            batch.append(by_seq[next_seq])
            next_seq += 1
        if len(batch) >= max_events:
            break
    return batch


def _stage_batch(
    connection,
    state_dir,
    vault,
    batch,
    now,
    *,
    fault_point=None,
):
    staged = []
    rejected = []
    stream_cursors = {}
    for candidate in batch:
        event = candidate["event"]
        if event["event_kind"] not in {
            "transcript.chunk",
            "transcript.gap",
            "attachment.blob",
        }:
            _upsert_event_row(
                connection,
                candidate,
                status="validated",
                code="forbidden_event_kind",
                mirror_path="",
                created_at=event["created_at"],
            )
            rejected.append(candidate)
            continue
        if event["event_kind"] == "attachment.blob":
            row = _upsert_event_row(
                connection,
                candidate,
                status="validated",
                code="validated",
                mirror_path="",
                created_at=event["created_at"],
            )
            canonical_path, metadata_path = _stage_attachment(
                connection,
                vault,
                candidate,
                row,
                fault_point=fault_point,
            )
            staged.append(
                {
                    "kind": "attachment",
                    "candidate": candidate,
                    "canonical_path": canonical_path,
                    "metadata_path": metadata_path,
                    "changed": True,
                }
            )
            continue

        stream_key = (
            event["producer_instance_id"],
            event["stream_id"],
            event["stream_epoch"],
        )
        stream = connection.execute(
            """
            select committed_cursor, mirror_path
              from streams
             where producer_instance_id = ? and stream_id = ? and stream_epoch = ?
            """,
            stream_key,
        ).fetchone()
        if stream_key not in stream_cursors:
            stream_cursors[stream_key] = (
                int(stream["committed_cursor"])
                if stream is not None
                else event["source_cursor"]["start"]
            )
        if event["source_cursor"]["start"] != stream_cursors[stream_key]:
            raise ReducerError(
                "remote stream cursor is non-contiguous; refusing to guess"
            )
        mirror_locator = (
            _normalize_mirror_locator(stream["mirror_path"])
            if stream is not None
            else _mirror_locator(event)
        )
        mirror_path = _resolve_mirror_path(state_dir, mirror_locator)
        _ensure_mirror(state_dir, mirror_path, event)
        append_bytes = _event_append_bytes(candidate)
        row = _upsert_event_row(
            connection,
            candidate,
            status="validated",
            code="validated",
            mirror_path=mirror_locator,
            created_at=event["created_at"],
        )
        _stage_mirror_append(
            connection,
            state_dir,
            candidate,
            row,
            mirror_path,
            append_bytes,
            fault_point=fault_point,
        )
        staged.append(
            {
                "kind": "transcript",
                "candidate": candidate,
                "mirror_path": str(mirror_path),
                "mirror_locator": mirror_locator,
                "stream_key": stream_key,
            }
        )
        stream_cursors[stream_key] = event["source_cursor"]["end"]
    connection.commit()
    return staged, rejected


def _upsert_event_row(
    connection,
    candidate,
    *,
    status,
    code,
    mirror_path,
    created_at,
):
    event = candidate["event"]
    existing = connection.execute(
        """
        select *
          from events
         where producer_instance_id = ? and seq = ?
        """,
        (event["producer_instance_id"], event["seq"]),
    ).fetchone()
    if existing is not None:
        if existing["event_sha256"] != candidate["event_sha256"]:
            raise ReducerError("ledger event hash changed during staging")
        return existing
    connection.execute(
        """
        insert into events(
            producer_instance_id, seq, event_id, event_sha256, device_id,
            status, code, bundle_path, event_kind, stream_id, stream_epoch,
            cursor_start, cursor_end, mirror_path, payload_sha256,
            payload_bytes, created_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event["producer_instance_id"],
            event["seq"],
            event["event_id"],
            candidate["event_sha256"],
            event["device_id"],
            status,
            code,
            candidate["bundle"],
            event["event_kind"],
            event["stream_id"],
            event["stream_epoch"],
            event["source_cursor"]["start"],
            event["source_cursor"]["end"],
            mirror_path,
            event["payload"]["sha256"],
            event["payload"]["bytes"],
            created_at,
        ),
    )
    return connection.execute(
        """
        select *
          from events
         where producer_instance_id = ? and seq = ?
        """,
        (event["producer_instance_id"], event["seq"]),
    ).fetchone()


def _mirror_locator(event):
    session = re.sub(r"[^A-Za-z0-9_.-]", "_", event["session_id"])[:80]
    filename = f"{event['stream_epoch']}-{session}-remote.jsonl"
    return (
        PurePosixPath("mirrors")
        / event["producer_instance_id"]
        / event["stream_id"]
        / filename
    ).as_posix()


def _normalize_mirror_locator(value):
    text = str(value or "")
    if not text:
        raise ReducerError("transcript mirror locator is empty")
    path = Path(text)
    if path.is_absolute():
        parts = path.parts
        try:
            index = len(parts) - 1 - tuple(reversed(parts)).index("mirrors")
        except ValueError as exc:
            raise ReducerError(
                "legacy transcript mirror path has no mirrors root"
            ) from exc
        text = PurePosixPath(*parts[index:]).as_posix()
    else:
        text = text.replace("\\", "/")
    try:
        normalized = validate_replica_path(text)
    except ProtocolError as exc:
        raise ReducerError("transcript mirror locator is invalid") from exc
    parts = PurePosixPath(normalized).parts
    if (
        len(parts) != 4
        or parts[0] != "mirrors"
        or not parts[-1].endswith("-remote.jsonl")
    ):
        raise ReducerError("transcript mirror locator is invalid")
    return normalized


def _resolve_mirror_path(state_dir, locator):
    locator = _normalize_mirror_locator(locator)
    return Path(state_dir).joinpath(*PurePosixPath(locator).parts)


def _ensure_mirror(state_dir, mirror_path, event):
    if mirror_path.exists():
        descriptor = _open_mirror(mirror_path, state_dir, os.O_RDONLY)
        os.close(descriptor)
        return
    metadata = event["metadata"]
    payload = {
        "id": event["session_id"],
        "cwd": metadata.get("cwd", ""),
        "timestamp": metadata.get("timestamp") or event["created_at"],
        "source": (
            "subagent"
            if metadata.get("is_subagent")
            else "beacon-sync-remote"
        ),
    }
    header = {
        "type": "session_meta",
        "timestamp": metadata.get("timestamp") or event["created_at"],
        "payload": payload,
    }
    portable_atomic_write(
        mirror_path,
        json.dumps(
            header,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n",
        root=state_dir,
    )


def _event_append_bytes(candidate):
    event = candidate["event"]
    if event["event_kind"] == "transcript.chunk":
        return candidate["payload_bytes"]
    return (b" " * GAP_PLACEHOLDER_BYTES) + b"\n"


def _stage_attachment(
    connection,
    vault,
    candidate,
    row,
    *,
    fault_point=None,
):
    event = candidate["event"]
    effect = _write_attachment_effects(vault, candidate)
    if (
        row["canonical_path"]
        and row["canonical_path"] != effect["canonical_path"]
    ):
        raise ReducerError("staged attachment canonical path changed")
    if row["metadata_path"] and row["metadata_path"] != effect["metadata_path"]:
        raise ReducerError("staged attachment metadata path changed")
    if (
        row["metadata_sha256"]
        and row["metadata_sha256"] != effect["metadata_sha256"]
    ):
        raise ReducerError("staged attachment metadata hash changed")
    if (
        row["metadata_bytes"]
        and row["metadata_bytes"] != effect["metadata_bytes"]
    ):
        raise ReducerError("staged attachment metadata size changed")
    if fault_point == "after_attachment_write":
        raise ReducerError("injected failure after_attachment_write")
    connection.execute(
        """
        update events
           set status = 'attachment_staged',
               code = 'attachment_staged',
               canonical_path = ?,
               metadata_path = ?,
               metadata_sha256 = ?,
               metadata_bytes = ?
         where producer_instance_id = ? and seq = ?
        """,
        (
            effect["canonical_path"],
            effect["metadata_path"],
            effect["metadata_sha256"],
            effect["metadata_bytes"],
            event["producer_instance_id"],
            event["seq"],
        ),
    )
    return effect["canonical_path"], effect["metadata_path"]


def _write_attachment_effects(vault, candidate):
    event = candidate["event"]
    payload_bytes = candidate["payload_bytes"]
    if payload_bytes is None:
        raise ReducerError("attachment object is missing")
    digest = event["payload"]["sha256"]
    suffix = _safe_attachment_suffix(payload_bytes)
    canonical_path = (
        PurePosixPath("Attachments")
        / "Agent-Memory-Beacon"
        / "remote"
        / "objects"
        / digest[:2]
        / f"{digest}{suffix}"
    ).as_posix()
    metadata_path = (
        PurePosixPath("04-Feedback")
        / "remote-attachments"
        / event["device_id"]
        / event["producer_instance_id"]
        / f"{event['seq']:020d}-{event['event_id']}.md"
    ).as_posix()
    validate_replica_path(canonical_path)
    validate_replica_path(metadata_path)
    metadata_bytes = _attachment_metadata_bytes(event, canonical_path)
    try:
        blob_created = write_immutable(
            vault.joinpath(*PurePosixPath(canonical_path).parts),
            payload_bytes,
            root=vault,
        )
        metadata_created = write_immutable(
            vault.joinpath(*PurePosixPath(metadata_path).parts),
            metadata_bytes,
            root=vault,
        )
    except ProtocolError as exc:
        raise ReducerError(str(exc)) from exc
    return {
        "canonical_path": canonical_path,
        "metadata_path": metadata_path,
        "metadata_sha256": sha256_bytes(metadata_bytes),
        "metadata_bytes": len(metadata_bytes),
        "blob_created": blob_created,
        "metadata_created": metadata_created,
    }


def _safe_attachment_suffix(data):
    value = bytes(data)
    if value.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if value.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if value.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if (
        len(value) >= 12
        and value[:4] == b"RIFF"
        and value[8:12] == b"WEBP"
    ):
        return ".webp"
    if value.startswith(b"%PDF-"):
        return ".pdf"
    if b"\x00" not in value:
        try:
            value.decode("utf-8")
            return ".txt"
        except UnicodeDecodeError:
            pass
    return ".bin"


def _attachment_metadata_bytes(event, canonical_path):
    attachment = event["extensions"]["attachment"]
    original_name = attachment["original_name"]
    reference_id = attachment.get("reference_id") or attachment.get(
        "attachment_id"
    )
    if not reference_id:
        raise ReducerError("attachment reference identity is missing")
    quoted = lambda value: json.dumps(  # noqa: E731
        str(value),
        ensure_ascii=False,
    )
    lines = [
        "---",
        "memory_type: remote_attachment",
        f"reference_id: {quoted(reference_id)}",
        f"source_event_id: {quoted(event['event_id'])}",
        f"device_id: {quoted(event['device_id'])}",
        f"producer_instance_id: {quoted(event['producer_instance_id'])}",
        f"agent: {quoted(event['agent'])}",
        f"session_id: {quoted(event['session_id'])}",
        f"created_at: {quoted(event['created_at'])}",
        f"sha256: {quoted(event['payload']['sha256'])}",
        f"bytes: {event['payload']['bytes']}",
        f"media_type: {quoted(event['payload']['media_type'])}",
        f"original_name: {quoted(original_name)}",
        "source_locator_sha256: "
        f"{quoted(attachment['source_locator_sha256'])}",
        f"reference_kind: {quoted(attachment['reference_kind'])}",
        f"source_cursor_start: {event['source_cursor']['start']}",
        f"source_cursor_end: {event['source_cursor']['end']}",
        "tags:",
        "  - agent-memory/remote-attachment",
        "---",
        "",
        "# 远程附件",
        "",
        f"- 原始名称：`{_markdown_code(original_name)}`",
        f"- 来源会话：`{_markdown_code(event['session_id'])}`",
        f"- 文件：[[{canonical_path}|打开附件]]",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _markdown_code(value):
    return str(value).replace("`", "'").replace("\r", " ").replace("\n", " ")


def _stage_mirror_append(
    connection,
    state_dir,
    candidate,
    row,
    mirror_path,
    append_bytes,
    *,
    fault_point=None,
):
    event = candidate["event"]
    descriptor = _open_mirror(mirror_path, state_dir, os.O_RDWR)
    try:
        before = row["mirror_before_size"]
        append_size = row["mirror_append_size"]
        append_hash = row["mirror_append_sha256"]
        if before is None:
            before = os.fstat(descriptor).st_size
            append_size = len(append_bytes)
            append_hash = sha256_bytes(append_bytes)
            connection.execute(
                """
                update events
                   set mirror_before_size = ?, mirror_append_size = ?,
                       mirror_append_sha256 = ?
                 where producer_instance_id = ? and seq = ?
                """,
                (
                    before,
                    append_size,
                    append_hash,
                    event["producer_instance_id"],
                    event["seq"],
                ),
            )
        elif (
            append_size != len(append_bytes)
            or append_hash != sha256_bytes(append_bytes)
        ):
            raise ReducerError("staged mirror append bytes changed during retry")

        connection.commit()
        current_size = os.fstat(descriptor).st_size
        expected_size = before + append_size
        if current_size == expected_size:
            os.lseek(descriptor, before, os.SEEK_SET)
            existing = _read_exact_descriptor(descriptor, append_size)
            if sha256_bytes(existing) != append_hash:
                raise ReducerError(
                    "completed mirror append hash does not match journal"
                )
        else:
            if current_size < before or current_size > expected_size:
                raise ReducerError("mirror has an unexpected third-state size")
            if current_size != before:
                os.ftruncate(descriptor, before)
                os.fsync(descriptor)
            os.lseek(descriptor, before, os.SEEK_SET)
            _write_all_descriptor(descriptor, append_bytes)
            os.fsync(descriptor)
            if fault_point == "after_mirror_fsync":
                raise ReducerError("injected failure after_mirror_fsync")
    finally:
        os.close(descriptor)
    connection.execute(
        """
        update events
           set status = 'mirror_staged', code = 'mirror_staged'
         where producer_instance_id = ? and seq = ?
        """,
        (event["producer_instance_id"], event["seq"]),
    )


def _open_mirror(path, state_dir, flags):
    try:
        descriptor = secure_open_file(
            path,
            flags,
            root=state_dir,
        )
        current = os.fstat(descriptor)
        if not stat.S_ISREG(current.st_mode):
            raise ReducerError("transcript mirror is not a regular file")
        if current.st_nlink != 1:
            raise ReducerError("transcript mirror has an unexpected hard link")
        return descriptor
    except ReducerError:
        if "descriptor" in locals():
            os.close(descriptor)
        raise
    except (OSError, ValueError) as exc:
        if "descriptor" in locals():
            os.close(descriptor)
        raise ReducerError("transcript mirror cannot be opened safely") from exc


def _read_exact_descriptor(descriptor, length):
    chunks = []
    remaining = int(length)
    while remaining:
        chunk = os.read(descriptor, min(65_536, remaining))
        if not chunk:
            raise ReducerError("transcript mirror append is truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _write_all_descriptor(descriptor, data):
    view = memoryview(data)
    written = 0
    while written < len(view):
        count = os.write(descriptor, view[written:])
        if count <= 0:
            raise ReducerError("transcript mirror append made no progress")
        written += count


def _commit_batch(
    connection,
    batch,
    staged,
    rejected,
    changed_by_mirror,
    now,
    result,
):
    staged_by_identity = {
        (
            item["candidate"]["event"]["producer_instance_id"],
            item["candidate"]["event"]["seq"],
        ): item
        for item in staged
    }
    rejected_ids = {
        (item["event"]["producer_instance_id"], item["event"]["seq"])
        for item in rejected
    }
    producer_last = {}
    stream_last = {}
    with connection:
        for candidate in batch:
            event = candidate["event"]
            identity = (event["producer_instance_id"], event["seq"])
            if identity in rejected_ids:
                status = "rejected_pending_publish"
                code = "forbidden_event_kind"
                result["rejected"] += 1
            else:
                staged_item = staged_by_identity[identity]
                if staged_item["kind"] == "attachment":
                    changed = bool(staged_item["changed"])
                else:
                    changed = bool(
                        changed_by_mirror.get(staged_item["mirror_path"], False)
                    )
                status = (
                    "applied_pending_publish"
                    if changed
                    else "noop_pending_publish"
                )
                code = "applied" if changed else "noop"
                result["applied" if changed else "noop"] += 1
                if staged_item["kind"] == "transcript":
                    stream_last[staged_item["stream_key"]] = (
                        event["source_cursor"]["end"],
                        staged_item["mirror_locator"],
                        event["session_id"],
                        event["agent"],
                    )
            connection.execute(
                """
                update events
                   set status = ?, code = ?, processed_at = ?
                 where producer_instance_id = ? and seq = ?
                """,
                (
                    status,
                    code,
                    _iso_utc(now),
                    event["producer_instance_id"],
                    event["seq"],
                ),
            )
            producer_last[event["producer_instance_id"]] = (
                event["seq"] + 1,
                event["device_id"],
            )
        for (producer, stream_id, epoch), values in stream_last.items():
            cursor, mirror_path, session_id, agent = values
            connection.execute(
                """
                insert into streams(
                    producer_instance_id, stream_id, stream_epoch,
                    committed_cursor, mirror_path, session_id, agent
                ) values (?, ?, ?, ?, ?, ?, ?)
                on conflict(producer_instance_id, stream_id, stream_epoch)
                do update set committed_cursor = excluded.committed_cursor,
                              mirror_path = excluded.mirror_path,
                              session_id = excluded.session_id,
                              agent = excluded.agent
                """,
                (
                    producer,
                    stream_id,
                    epoch,
                    cursor,
                    mirror_path,
                    session_id,
                    agent,
                ),
            )
        for producer, (next_seq, device_id) in producer_last.items():
            owner = connection.execute(
                """
                select device_id
                  from producers
                 where producer_instance_id = ?
                """,
                (producer,),
            ).fetchone()
            if owner is not None and owner["device_id"] != device_id:
                raise ReducerError("producer device binding changed during commit")
            connection.execute(
                """
                insert into producers(
                    producer_instance_id, device_id, next_seq,
                    blocked_code, updated_at
                ) values (?, ?, ?, '', ?)
                on conflict(producer_instance_id)
                do update set next_seq = excluded.next_seq,
                              blocked_code = '',
                              updated_at = excluded.updated_at
                """,
                (producer, device_id, next_seq, _iso_utc(now)),
            )


def _default_harvest_adapter(cfg, mirror_paths):
    from session_harvester import (
        _refresh_effectiveness_report,
        commit_transcript_harvest,
        prepare_transcript_harvest,
        rebuild_memory_index,
    )

    outcomes = [prepare_transcript_harvest(cfg, path) for path in mirror_paths]
    if any(outcome.needs_index_rebuild for outcome in outcomes):
        rebuild_memory_index(cfg)
    for outcome in outcomes:
        commit_transcript_harvest(cfg, outcome)
    if any(outcome.changed for outcome in outcomes):
        _refresh_effectiveness_report(cfg)
    return {
        str(outcome.transcript_path): bool(outcome.changed)
        for outcome in outcomes
    }


def _block_producer(connection, producer, device_id, code, now_text):
    with connection:
        connection.execute(
            """
            insert into producers(
                producer_instance_id, device_id, next_seq,
                blocked_code, updated_at
            ) values (?, ?, 1, ?, ?)
            on conflict(producer_instance_id)
            do update set blocked_code = excluded.blocked_code,
                          updated_at = excluded.updated_at
            """,
            (producer, device_id, code, now_text),
        )


def _pending_publish_count(connection):
    row = connection.execute(
        """
        select count(*) as count
          from events
         where status in (
             'applied_pending_publish',
             'noop_pending_publish',
             'rejected_pending_publish'
         )
        """
    ).fetchone()
    return int(row["count"])


def _write_quarantine(state_dir, key, payload):
    safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", str(key))[:120] or "bundle"
    digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()[:16]
    portable_atomic_write(
        state_dir / "quarantine" / f"{safe_key}-{digest}.json",
        canonical_json_bytes(payload),
        root=state_dir,
    )


def _state_dir(sync_cfg):
    value = str(sync_cfg.get("state_dir") or "").strip()
    if not value:
        raise ReducerError("beacon_sync.state_dir is required for authority")
    return Path(os.path.abspath(os.path.expanduser(value)))


def _utc_now(value):
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ReducerError("authority timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso_utc(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
