#!/usr/bin/env python3
"""Read-only health profiles for Agent Memory Beacon."""
from __future__ import annotations

import argparse
import csv
import contextlib
import io
import json
import os
import plistlib
import re
import shlex
import sqlite3
import stat
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import yaml

from branding import LEGACY_LAUNCHD_LABELS
from memory_graph import (
    graph_path_for_index,
    render_memory_graph_quality_markdown,
    validate_memory_graph,
)
from memory_recall import validate_recall_index
from memory_schema import is_valid_runtime_record


PROFILE_NAMES = ("quick", "ci", "live")
DEFAULT_RUNTIME_ROOT = "~/.local/share/agent-memory-beacon/runtime"
OWNED_HOOK_MARKER = "AGENT_MEMORY_BEACON_HOOK=1"
LAUNCHD_LABELS = (
    "io.agent-memory-beacon.harvest",
    "io.agent-memory-beacon.weekly",
)
LAUNCHD_SCRIPTS = {
    "io.agent-memory-beacon.harvest": "session_harvester.py",
    "io.agent-memory-beacon.weekly": "runner.py",
}
SYNC_LAUNCHD_LABEL = "io.agent-memory-beacon.sync"
SYNC_DELIVERY_SLO_SECONDS = 24 * 60 * 60
AUTHORITY_LEDGER_SCHEMA = {
    "metadata": (
        ("key", "TEXT", 0, None, 1),
        ("value", "TEXT", 1, None, 0),
    ),
    "producers": (
        ("producer_instance_id", "TEXT", 0, None, 1),
        ("device_id", "TEXT", 1, None, 0),
        ("next_seq", "INTEGER", 1, None, 0),
        ("blocked_code", "TEXT", 1, "''", 0),
        ("updated_at", "TEXT", 1, None, 0),
    ),
    "streams": (
        ("producer_instance_id", "TEXT", 1, None, 1),
        ("stream_id", "TEXT", 1, None, 2),
        ("stream_epoch", "TEXT", 1, None, 3),
        ("committed_cursor", "INTEGER", 1, None, 0),
        ("mirror_path", "TEXT", 1, None, 0),
        ("session_id", "TEXT", 1, None, 0),
        ("agent", "TEXT", 1, None, 0),
    ),
    "events": (
        ("producer_instance_id", "TEXT", 1, None, 1),
        ("seq", "INTEGER", 1, None, 2),
        ("event_id", "TEXT", 1, None, 0),
        ("event_sha256", "TEXT", 1, None, 0),
        ("device_id", "TEXT", 1, None, 0),
        ("status", "TEXT", 1, None, 0),
        ("code", "TEXT", 1, None, 0),
        ("bundle_path", "TEXT", 1, None, 0),
        ("event_kind", "TEXT", 1, None, 0),
        ("stream_id", "TEXT", 1, None, 0),
        ("stream_epoch", "TEXT", 1, None, 0),
        ("cursor_start", "INTEGER", 1, None, 0),
        ("cursor_end", "INTEGER", 1, None, 0),
        ("mirror_path", "TEXT", 1, "''", 0),
        ("mirror_before_size", "INTEGER", 0, None, 0),
        ("mirror_append_size", "INTEGER", 0, None, 0),
        ("mirror_append_sha256", "TEXT", 1, "''", 0),
        ("canonical_path", "TEXT", 1, "''", 0),
        ("metadata_path", "TEXT", 1, "''", 0),
        ("payload_sha256", "TEXT", 1, "''", 0),
        ("payload_bytes", "INTEGER", 1, "0", 0),
        ("metadata_sha256", "TEXT", 1, "''", 0),
        ("metadata_bytes", "INTEGER", 1, "0", 0),
        ("created_at", "TEXT", 1, None, 0),
        ("processed_at", "TEXT", 1, "''", 0),
        ("canonical_generation", "INTEGER", 0, None, 0),
        ("generation_id", "TEXT", 1, "''", 0),
    ),
}
PYTHON_COMPILE_CODE = (
    "import pathlib,sys;"
    "[compile(pathlib.Path(p).read_bytes(),p,'exec') for p in sys.argv[1:]]"
)
IMPORT_PROBE_CODE = (
    "import sys;sys.dont_write_bytecode=True;"
    "sys.path.insert(0,sys.argv[1]);"
    "import config,insight_memory,memory_schema,memory_recall,memory_runtime,"
    "memory_graph,memory_identity_repair,memory_lifecycle,session_harvester,"
    "beacon_sync_protocol,beacon_sync_producer,beacon_sync_reducer,"
    "beacon_sync_snapshot,beacon_sync,install_beacon_sync"
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    required: bool
    passed: bool
    details: str


@dataclass(frozen=True)
class DoctorReport:
    profile: str
    checks: tuple[DoctorCheck, ...]

    @property
    def passed(self):
        return all(check.passed for check in self.checks if check.required)

    def as_dict(self):
        return {
            "profile": self.profile,
            "status": "pass" if self.passed else "fail",
            "checks": [asdict(check) for check in self.checks],
        }


def run_profile(profile, *, repo_root, cfg=None, runner=None):
    """Run one deterministic, read-only doctor profile."""
    profile = str(profile or "").strip().lower()
    if profile not in PROFILE_NAMES:
        raise ValueError(f"unsupported doctor profile: {profile}")
    repo_root = os.path.abspath(os.path.expanduser(os.fspath(repo_root)))
    if cfg is None:
        from config import load_config

        cfg = load_config()
    runner = runner or _subprocess_runner
    sync_cfg = cfg.get("beacon_sync") if isinstance(cfg, dict) else {}
    producer_replica_profile = bool(
        profile in {"quick", "live"}
        and isinstance(sync_cfg, dict)
        and sync_cfg.get("enabled") is True
        and sync_cfg.get("role") == "producer-replica"
    )

    checks = [
        _configuration_check(
            cfg,
            repo_root,
            producer_replica=producer_replica_profile,
        ),
        _beacon_sync_check(cfg),
    ]
    if not producer_replica_profile:
        checks.extend(
            (
                _recall_index_schema_check(cfg),
                _memory_graph_schema_check(
                    cfg,
                    allow_legacy=profile != "live",
                ),
            )
        )
    checks.extend(
        (
            _command_check(
                "script-compilation",
                _script_compilation_command(repo_root),
                repo_root,
                60,
                runner,
            ),
            _command_check(
                "module-imports",
                (
                    sys.executable,
                    "-B",
                    "-c",
                    IMPORT_PROBE_CODE,
                    os.path.join(repo_root, "scripts"),
                ),
                repo_root,
                30,
                runner,
            ),
        )
    )

    if profile == "ci":
        source_check = _ci_source_checkout_check(repo_root)
        if not source_check.passed:
            checks.append(source_check)
            return DoctorReport(profile=profile, checks=tuple(checks))
        checks.extend(_ci_checks(repo_root, runner))
    elif profile == "live":
        checks.extend(
            _producer_replica_live_checks(
                repo_root,
                cfg,
                runner,
            )
            if producer_replica_profile
            else _live_checks(repo_root, cfg, runner)
        )
    return DoctorReport(profile=profile, checks=tuple(checks))


def _ci_source_checkout_check(repo_root):
    required = (
        os.path.join(repo_root, "tests"),
        os.path.join(repo_root, "tests", "fixtures", "memory_runtime"),
    )
    missing = [path for path in required if not os.path.isdir(path)]
    return DoctorCheck(
        name="source-checkout",
        required=True,
        passed=not missing,
        details=(
            "ok"
            if not missing
            else "ci profile requires a source checkout with tests and fixtures"
        ),
    )


def _configuration_check(cfg, repo_root, *, producer_replica=False):
    errors = []
    if not isinstance(cfg, dict):
        errors.append("configuration is not a mapping")
    elif not producer_replica:
        vault = _expanded(cfg.get("vault_path"))
        if not vault or not os.path.isabs(vault) or not os.path.isdir(vault):
            errors.append("vault_path is not an existing absolute directory")
    if not os.path.isdir(repo_root):
        errors.append("repository root does not exist")
    return DoctorCheck(
        name="configuration",
        required=True,
        passed=not errors,
        details="ok" if not errors else "; ".join(errors),
    )


def _beacon_sync_check(cfg):
    sync_cfg = cfg.get("beacon_sync") if isinstance(cfg, dict) else None
    if not sync_cfg or sync_cfg.get("enabled") is not True:
        return DoctorCheck("beacon-sync", True, True, "disabled")
    errors = []
    role = sync_cfg.get("role")
    try:
        if role == "producer-replica":
            _inspect_sync_producer(sync_cfg, errors)
        elif role == "authority":
            _inspect_sync_authority(cfg, sync_cfg, errors)
        else:
            errors.append("configured role is invalid")
    except Exception as exc:
        errors.append(str(exc))
    return DoctorCheck(
        "beacon-sync",
        True,
        not errors,
        f"ok ({role})" if not errors else "; ".join(errors),
    )


def _inspect_sync_producer(sync_cfg, errors):
    from beacon_sync_producer import (
        _validate_gc_receipt,
        load_producer_state,
    )
    from beacon_sync_protocol import (
        _assert_supported_windows_atomic_filesystem,
        read_bounded_regular_file,
        sha256_bytes,
        validate_event,
        validate_ready,
    )
    from beacon_sync_snapshot import (
        DEFAULT_MAX_OBJECT_BYTES,
        _load_received_generation,
        _validate_current,
        inspect_replica_state,
    )

    if os.name == "nt":
        _assert_supported_windows_atomic_filesystem()
    state_dir = _sync_root(sync_cfg, "state_dir")
    outbox = _sync_root(sync_cfg, "outbox_dir")
    state_path = state_dir / "producer-state.json"
    identity_path = outbox / "v1" / "identity.json"
    if not state_path.is_file():
        errors.append("producer state is missing")
        return
    if not identity_path.is_file():
        errors.append("producer identity is missing")
        return
    state = load_producer_state(sync_cfg)
    _inspect_producer_attachment_cas(
        sync_cfg,
        state,
        state_dir,
        errors,
    )
    try:
        identity = json.loads(
            read_bounded_regular_file(
                identity_path,
                max_bytes=16 * 1024,
                root=outbox,
            )
        )
    except Exception as exc:
        errors.append(f"producer identity is invalid: {exc}")
        return
    if (
        not isinstance(identity, dict)
        or identity.get("protocol") != "agent-memory-beacon-sync-identity"
        or identity.get("schema_version") != 1
        or identity.get("device_id") != state["device_id"]
        or identity.get("producer_instance_id")
        != state["producer_instance_id"]
    ):
        errors.append("producer identity does not match persisted state")

    stale_after = SYNC_DELIVERY_SLO_SECONDS
    receipt_root_text = str(sync_cfg.get("received_published_dir") or "")
    receipt_root = (
        Path(receipt_root_text) / "v1" / "receipts"
        if receipt_root_text
        else None
    )
    events_root = outbox / "v1" / "events"
    for ready_path in (
        sorted(events_root.glob("*/*/ready.json"))
        if events_root.is_dir()
        else ()
    ):
        bundle = ready_path.parent
        try:
            event_bytes = read_bounded_regular_file(
                bundle / "event.json",
                max_bytes=int(sync_cfg.get("max_event_json_bytes", 128 * 1024)),
                root=outbox,
            )
            event = json.loads(event_bytes)
            ready = json.loads(
                read_bounded_regular_file(
                    ready_path,
                    max_bytes=16 * 1024,
                    root=outbox,
                )
            )
            validate_event(event)
            validate_ready(ready, event, event_bytes)
            if ready["object_count"]:
                object_path = bundle / "objects" / event["payload"]["sha256"]
                data = read_bounded_regular_file(
                    object_path,
                    max_bytes=int(
                        sync_cfg.get("max_object_bytes", 32 * 1024 * 1024)
                    ),
                    root=outbox,
                )
                if (
                    len(data) != event["payload"]["bytes"]
                    or sha256_bytes(data) != event["payload"]["sha256"]
                ):
                    raise ValueError("event object hash or size does not match")
            receipt_path = (
                receipt_root
                / event["producer_instance_id"]
                / f"{event['seq']:020d}-{event['event_id']}.json"
                if receipt_root is not None
                else None
            )
            if receipt_path is not None and receipt_path.is_file():
                receipt = json.loads(
                    read_bounded_regular_file(
                        receipt_path,
                        max_bytes=64 * 1024,
                        root=receipt_root,
                    )
                )
                _validate_gc_receipt(receipt, event, event_bytes)
            elif (
                datetime.now(timezone.utc) - _doctor_parse_utc(event["created_at"])
            ).total_seconds() > stale_after:
                errors.append(
                    f"stale receipt for producer sequence {event['seq']}"
                )
        except FileNotFoundError as exc:
            errors.append(f"event object is missing: {exc}")
        except Exception as exc:
            errors.append(f"producer bundle is invalid: {exc}")
    active_path = state_dir / "replica" / "active-generation.json"
    received = _sync_root(sync_cfg, "received_published_dir")
    current_path = received / "v1" / "current.json"
    try:
        replica_state = inspect_replica_state(sync_cfg)
        if current_path.is_file():
            current = json.loads(
                read_bounded_regular_file(
                    current_path,
                    max_bytes=16 * 1024,
                    root=received,
                )
            )
            _validate_current(current)
            _load_received_generation(
                received,
                current,
                int(
                    sync_cfg.get(
                        "max_replica_object_bytes",
                        DEFAULT_MAX_OBJECT_BYTES,
                    )
                ),
            )
            if not replica_state.get("active"):
                errors.append("received generation is not materialized")
            elif replica_state["generation"] < current["generation"]:
                errors.append(
                    "replica is behind received generation "
                    f"({replica_state['generation']} < {current['generation']})"
                )
            elif (
                replica_state["generation"] != current["generation"]
                or replica_state["generation_id"] != current["generation_id"]
            ):
                errors.append("replica identity does not match received generation")
        elif active_path.is_file():
            errors.append("received current generation is missing for active replica")
    except Exception as exc:
        errors.append(f"replica drift or corruption: {exc}")


def _inspect_producer_attachment_cas(sync_cfg, state, state_dir, errors):
    from beacon_sync_protocol import (
        read_bounded_regular_file,
        sha256_bytes,
    )

    referenced = {}
    items = list(state.get("attachment_queue") or [])
    pending = state.get("pending_event")
    if isinstance(pending, dict):
        items.extend(pending.get("attachments") or [])
    for item in items:
        digest = str(item.get("sha256") or "")
        size = item.get("bytes")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            errors.append("producer attachment CAS reference hash is invalid")
            continue
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
        ):
            errors.append("producer attachment CAS reference size is invalid")
            continue
        previous = referenced.get(digest)
        if previous is not None and previous != size:
            errors.append("producer attachment CAS reference sizes conflict")
            continue
        referenced[digest] = size

    cas_root = state_dir / "attachment-cas"
    if not os.path.lexists(cas_root):
        if referenced:
            errors.append("producer attachment CAS root is missing")
        return
    root_info = os.lstat(cas_root)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        errors.append("producer attachment CAS root is unsafe")
        return

    seen = set()
    max_attachment = int(
        sync_cfg.get("max_attachment_bytes", 32 * 1024 * 1024)
    )
    for current, directories, files in os.walk(cas_root):
        safe_directories = []
        for directory in directories:
            path = Path(current) / directory
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                errors.append("producer attachment CAS contains an unsafe directory")
            else:
                safe_directories.append(directory)
        directories[:] = safe_directories
        for filename in files:
            path = Path(current) / filename
            try:
                data = read_bounded_regular_file(
                    path,
                    max_bytes=max_attachment,
                    root=state_dir,
                )
                digest = sha256_bytes(data)
                if filename != digest or path.parent.name != digest[:2]:
                    raise ValueError("CAS path does not match content hash")
                if digest not in referenced:
                    raise ValueError("CAS object is not referenced by producer state")
                if len(data) != referenced[digest]:
                    raise ValueError("CAS object size does not match producer state")
                seen.add(digest)
            except Exception as exc:
                errors.append(f"producer attachment CAS object is invalid: {exc}")
    for digest in sorted(set(referenced) - seen):
        errors.append(f"producer attachment CAS object is missing: {digest}")


def _inspect_sync_authority(cfg, sync_cfg, errors):
    from beacon_sync_reducer import LEDGER_SCHEMA_VERSION
    from beacon_sync_protocol import (
        canonical_json_bytes,
        read_bounded_regular_file,
    )
    from beacon_sync_snapshot import (
        _verify_receipt_generation,
        inspect_published_generation,
        receipt_document_for_row,
    )

    inspect_published_generation(sync_cfg)
    state_dir = _sync_root(sync_cfg, "state_dir")
    published = _sync_root(sync_cfg, "published_dir")
    ledger_path = state_dir / "ledger.sqlite3"
    if not ledger_path.exists():
        return
    info = os.lstat(ledger_path)
    if os.path.islink(ledger_path) or not os.path.isfile(ledger_path):
        errors.append("authority ledger is not a regular file")
        return
    connection = sqlite3.connect(
        ledger_path.resolve().as_uri() + "?mode=ro",
        uri=True,
        timeout=5,
    )
    connection.row_factory = sqlite3.Row
    try:
        schema_errors = _authority_ledger_schema_errors(
            connection,
            LEDGER_SCHEMA_VERSION,
        )
        if schema_errors:
            errors.extend(schema_errors)
            return
        tables = set(AUTHORITY_LEDGER_SCHEMA)
        if "producers" in tables:
            blocked = connection.execute(
                """
                select count(*) as count
                  from producers
                 where blocked_code is not null and blocked_code != ''
                """
            ).fetchone()["count"]
            if blocked:
                errors.append(f"{blocked} blocked producer sequence(s)")
        if "events" in tables:
            event_columns = {
                row["name"]
                for row in connection.execute("pragma table_info(events)")
            }
            required_event_columns = {
                "producer_instance_id",
                "seq",
                "event_id",
                "device_id",
                "status",
                "event_kind",
                "canonical_path",
                "metadata_path",
                "payload_sha256",
                "payload_bytes",
                "metadata_sha256",
                "metadata_bytes",
            }
            if not required_event_columns.issubset(event_columns):
                errors.append("authority ledger schema event columns are incomplete")
            if {
                "canonical_generation",
                "generation_id",
            }.issubset(event_columns):
                partial = connection.execute(
                    """
                    select count(*) as count
                      from events
                     where status in (
                        'applied_pending_publish',
                        'noop_pending_publish',
                        'rejected_pending_publish',
                        'applied',
                        'noop',
                        'rejected'
                     )
                       and (
                           (
                               canonical_generation is null
                               and coalesce(generation_id, '') != ''
                           )
                           or
                           (
                               canonical_generation is not null
                               and coalesce(generation_id, '') = ''
                           )
                       )
                    """
                ).fetchone()["count"]
                if partial:
                    errors.append(
                        f"{partial} pending receipt(s) have a partial "
                        "generation binding"
                    )
            receipt_columns = {
                "producer_instance_id",
                "seq",
                "event_id",
                "event_sha256",
                "status",
                "code",
                "processed_at",
                "canonical_generation",
                "generation_id",
            }
            if receipt_columns.issubset(event_columns):
                terminal = connection.execute(
                    """
                    select producer_instance_id, seq, event_id, event_sha256,
                           status, code, processed_at, canonical_generation,
                           generation_id
                      from events
                     where status in ('applied', 'noop', 'rejected')
                    """
                ).fetchall()
                verified_generations = {}
                max_object_bytes = int(
                    sync_cfg.get(
                        "max_replica_object_bytes",
                        sync_cfg.get("max_object_bytes", 64 * 1024 * 1024),
                    )
                )
                for row in terminal:
                    try:
                        _verify_receipt_generation(
                            published,
                            row["canonical_generation"],
                            row["generation_id"],
                            max_object_bytes,
                            verified_generations,
                        )
                        expected = canonical_json_bytes(
                            receipt_document_for_row(row)
                        )
                        receipt_path = (
                            published
                            / "v1"
                            / "receipts"
                            / row["producer_instance_id"]
                            / (
                                f"{int(row['seq']):020d}-"
                                f"{row['event_id']}.json"
                            )
                        )
                        actual = read_bounded_regular_file(
                            receipt_path,
                            max_bytes=128 * 1024,
                            root=published,
                        )
                        if actual != expected:
                            raise ValueError(
                                "receipt bytes do not match terminal ledger row"
                            )
                    except Exception as exc:
                        errors.append(
                            "terminal receipt is missing or invalid for "
                            f"{row['producer_instance_id']}:{row['seq']}: {exc}"
                        )
            rows = connection.execute(
                """
                select processed_at
                  from events
                 where status in (
                    'applied_pending_publish',
                    'noop_pending_publish',
                    'rejected_pending_publish'
                 )
                """
            ).fetchall()
            stale_after = SYNC_DELIVERY_SLO_SECONDS
            stale = sum(
                1
                for row in rows
                if (
                    datetime.now(timezone.utc)
                    - _doctor_parse_utc(row["processed_at"])
                ).total_seconds()
                > stale_after
            )
            if stale:
                errors.append(f"{stale} stale pending receipt(s)")
            if required_event_columns.issubset(event_columns):
                _inspect_authority_attachment_effects(
                    cfg,
                    connection,
                    errors,
                )
    finally:
        connection.close()


def _authority_ledger_schema_errors(connection, expected_version):
    tables = {
        row["name"]
        for row in connection.execute(
            """
            select name
              from sqlite_master
             where type = 'table' and name not like 'sqlite_%'
            """
        )
    }
    expected_tables = set(AUTHORITY_LEDGER_SCHEMA)
    missing = sorted(expected_tables - tables)
    unexpected = sorted(tables - expected_tables)
    if missing or unexpected:
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        return [
            "authority ledger schema tables are incompatible: "
            + "; ".join(details)
        ]

    errors = []
    for table, expected in AUTHORITY_LEDGER_SCHEMA.items():
        actual = tuple(
            (
                str(row["name"]),
                str(row["type"] or "").upper(),
                int(row["notnull"]),
                row["dflt_value"],
                int(row["pk"]),
            )
            for row in connection.execute(f"pragma table_info({table})")
        )
        if actual != expected:
            errors.append(
                f"authority ledger schema {table} columns are incomplete or incompatible"
            )
    if errors:
        return errors

    schema_row = connection.execute(
        "select value from metadata where key = 'schema_version'"
    ).fetchone()
    if schema_row is None or schema_row["value"] != str(expected_version):
        errors.append(
            "authority ledger schema is not current "
            f"(expected {expected_version})"
        )

    event_id_unique = False
    for index in connection.execute("pragma index_list(events)"):
        if not int(index["unique"]) or int(index["partial"]):
            continue
        columns = tuple(
            row["name"]
            for row in connection.execute(
                "select name from pragma_index_info(?) order by seqno",
                (index["name"],),
            )
        )
        if columns == ("event_id",):
            event_id_unique = True
            break
    if not event_id_unique:
        errors.append(
            "authority ledger schema event_id unique constraint is missing"
        )
    return errors


def _inspect_authority_attachment_effects(cfg, connection, errors):
    from beacon_sync_protocol import (
        read_bounded_regular_file,
        sha256_bytes,
        validate_replica_path,
    )

    vault_text = str(cfg.get("vault_path") or "").strip()
    if not vault_text:
        errors.append("canonical vault_path is unavailable for attachment checks")
        return
    vault = Path(os.path.abspath(os.path.expanduser(vault_text)))
    if not vault.is_dir():
        errors.append("canonical vault_path is unavailable for attachment checks")
        return
    rows = connection.execute(
        """
        select producer_instance_id, seq, event_id, device_id, status,
               canonical_path, metadata_path, payload_sha256, payload_bytes,
               metadata_sha256, metadata_bytes
          from events
         where event_kind = 'attachment.blob'
           and status != 'rejected'
           and status != 'rejected_pending_publish'
         order by producer_instance_id, seq
        """
    ).fetchall()
    for row in rows:
        label = f"{row['producer_instance_id']}:{row['seq']}"
        kind = "attachment blob"
        try:
            canonical_path = validate_replica_path(row["canonical_path"])
            metadata_path = validate_replica_path(row["metadata_path"])
            expected_metadata_path = (
                PurePosixPath("04-Feedback")
                / "remote-attachments"
                / row["device_id"]
                / row["producer_instance_id"]
                / f"{int(row['seq']):020d}-{row['event_id']}.md"
            ).as_posix()
            if metadata_path != expected_metadata_path:
                raise ValueError("attachment metadata path is not canonical")
            digest = str(row["payload_sha256"] or "")
            canonical = PurePosixPath(canonical_path)
            expected_parent = (
                PurePosixPath("Attachments")
                / "Agent-Memory-Beacon"
                / "remote"
                / "objects"
                / digest[:2]
            )
            if (
                canonical.parent != expected_parent
                or canonical.name.split(".", 1)[0] != digest
                or canonical.suffix
                not in {".bin", ".gif", ".jpg", ".pdf", ".png", ".txt", ".webp"}
            ):
                raise ValueError("attachment blob path is not canonical")
            blob = read_bounded_regular_file(
                vault.joinpath(*canonical.parts),
                max_bytes=max(
                    int(row["payload_bytes"]),
                    1,
                ),
                root=vault,
            )
            if (
                len(blob) != row["payload_bytes"]
                or sha256_bytes(blob) != digest
            ):
                raise ValueError("attachment blob hash or size does not match ledger")
            kind = "attachment metadata"
            metadata = read_bounded_regular_file(
                vault.joinpath(*PurePosixPath(metadata_path).parts),
                max_bytes=max(int(row["metadata_bytes"]), 1),
                root=vault,
            )
            if (
                len(metadata) != row["metadata_bytes"]
                or sha256_bytes(metadata) != row["metadata_sha256"]
            ):
                raise ValueError(
                    "attachment metadata hash or size does not match ledger"
                )
            frontmatter, body = _parse_attachment_metadata(metadata)
            expected = {
                "memory_type": "remote_attachment",
                "source_event_id": row["event_id"],
                "producer_instance_id": row["producer_instance_id"],
                "device_id": row["device_id"],
                "sha256": digest,
                "bytes": row["payload_bytes"],
            }
            if any(frontmatter.get(key) != value for key, value in expected.items()):
                raise ValueError("attachment metadata identity does not match ledger")
            reference_id = str(frontmatter.get("reference_id") or "")
            if not re.fullmatch(
                r"(?:reference|attachment)-[0-9a-f]{64}",
                reference_id,
            ):
                raise ValueError("attachment metadata reference ID is invalid")
            if f"[[{canonical_path}|" not in body:
                raise ValueError("attachment metadata link does not match blob")
        except Exception as exc:
            errors.append(f"{kind} is invalid for {label}: {exc}")


def _parse_attachment_metadata(data):
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("attachment metadata is not UTF-8") from exc
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        raise ValueError("attachment metadata frontmatter is missing")
    header, body = text[4:].split("\n---\n", 1)
    try:
        frontmatter = yaml.safe_load(header) or {}
    except yaml.YAMLError as exc:
        raise ValueError("attachment metadata frontmatter is invalid") from exc
    if not isinstance(frontmatter, dict):
        raise ValueError("attachment metadata frontmatter is invalid")
    return frontmatter, body


def _sync_root(sync_cfg, key):
    value = str(sync_cfg.get(key) or "").strip()
    if not value:
        raise ValueError(f"beacon_sync.{key} is empty")
    return Path(os.path.abspath(os.path.expanduser(value)))


def _doctor_parse_utc(value):
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("sync timestamp is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _recall_index_schema_check(cfg):
    try:
        index_path = _recall_index_path(cfg)
        if os.path.islink(index_path) or not os.path.isfile(index_path):
            raise ValueError(f"recall index is not a regular file: {index_path}")
        with open(index_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        validate_recall_index(payload)
        invalid = []
        seen = set()
        for index, unit in enumerate(payload["units"]):
            unit_id = (
                str(unit.get("id") or "").strip()
                if isinstance(unit, dict)
                else ""
            )
            label = unit_id or f"unit[{index}]"
            if not is_valid_runtime_record(unit):
                invalid.append(label)
                continue
            if unit_id in seen:
                invalid.append(f"duplicate:{unit_id}")
            seen.add(unit_id)
        if invalid:
            raise ValueError(
                "invalid recall runtime units: " + ", ".join(invalid[:10])
            )
        return DoctorCheck(
            "recall-index-schema",
            True,
            True,
            f"ok ({len(payload['units'])} runtime units)",
        )
    except Exception as exc:
        return DoctorCheck("recall-index-schema", True, False, str(exc))


def _memory_graph_schema_check(cfg, *, allow_legacy=False):
    try:
        index_path = _recall_index_path(cfg)
        graph_path = graph_path_for_index(index_path)
        if os.path.islink(graph_path) or not os.path.isfile(graph_path):
            raise ValueError(f"memory graph is not a regular file: {graph_path}")
        with open(index_path, "r", encoding="utf-8") as handle:
            index = json.load(handle)
        with open(graph_path, "r", encoding="utf-8") as handle:
            graph = json.load(handle)
        generation_id = (
            str(index.get("generation_id") or "")
            if isinstance(index, dict)
            else ""
        )
        if graph.get("schema_version") == "3.0" and not generation_id:
            if allow_legacy:
                if _is_upgradeable_pre_generation_v3(
                    graph,
                    index.get("units") if isinstance(index, dict) else (),
                ):
                    return DoctorCheck(
                        "memory-graph-schema",
                        True,
                        True,
                        "ok (pre-generation Graph v3 accepted for upgrade rebuild)",
                    )
                raise ValueError(
                    "pre-generation Graph v3 is malformed or not upgradeable"
                )
            raise ValueError("recall index generation_id is required for Graph v3")
        units = index.get("units") if isinstance(index, dict) else ()
        try:
            quality = validate_memory_graph(
                graph,
                units,
                allow_legacy=allow_legacy,
                expected_generation_id=generation_id,
            )
        except ValueError:
            if allow_legacy and _is_upgradeable_previous_v3(
                graph,
                units,
                generation_id,
            ):
                return DoctorCheck(
                    "memory-graph-schema",
                    True,
                    True,
                    "ok (previous Graph v3 accepted for revision-bound rebuild)",
                )
            raise
        if quality["legacy"]:
            return DoctorCheck(
                "memory-graph-schema",
                True,
                True,
                "ok (legacy Graph v2 accepted for pre-upgrade rebuild)",
            )
        vault = _expanded(cfg.get("vault_path"))
        quality_path = os.path.join(
            vault,
            "05-Agent-Memory",
            "memory-graph-quality.md",
        )
        if os.path.islink(quality_path) or not os.path.isfile(quality_path):
            raise ValueError(
                f"memory graph quality report is not a regular file: {quality_path}"
            )
        with open(quality_path, "r", encoding="utf-8") as handle:
            actual_report = handle.read()
        expected_report = render_memory_graph_quality_markdown(
            graph,
            index.get("units"),
        )
        if actual_report != expected_report:
            raise ValueError("memory graph quality report is stale or inconsistent")
        return DoctorCheck(
            "memory-graph-schema",
            True,
            True,
            f"ok ({quality['nodes']} nodes, {quality['edges']} edges)",
        )
    except Exception as exc:
        return DoctorCheck("memory-graph-schema", True, False, str(exc))


def _is_upgradeable_pre_generation_v3(graph, units=()):
    """Recognize the bounded Graph v3 shape emitted before generation binding."""
    if (
        not isinstance(graph, dict)
        or graph.get("schema_version") != "3.0"
        or graph.get("generation_id")
        or graph.get("generated_by") != "knowledge_index.py"
        or not isinstance(graph.get("nodes"), list)
        or not isinstance(graph.get("edges"), list)
    ):
        return False
    preview_generation = "pre-generation-upgrade-validation"
    candidate = dict(graph)
    candidate["generation_id"] = preview_generation
    try:
        validate_memory_graph(
            candidate,
            units,
            allow_legacy=False,
            expected_generation_id=preview_generation,
        )
    except (TypeError, ValueError):
        return False
    return True


def _is_upgradeable_previous_v3(graph, units, generation_id):
    """Allow only the prior v3 shape that omitted non-memory revisions."""
    if (
        not isinstance(graph, dict)
        or graph.get("schema_version") != "3.0"
        or graph.get("generated_by") != "knowledge_index.py"
        or not generation_id
        or graph.get("generation_id") != generation_id
        or not isinstance(graph.get("nodes"), list)
        or not isinstance(graph.get("edges"), list)
    ):
        return False

    candidate = json.loads(json.dumps(graph))
    node_by_id = {
        node.get("id"): node
        for node in candidate["nodes"]
        if isinstance(node, dict) and isinstance(node.get("id"), str)
    }
    edges_by_source = {}
    for edge in candidate["edges"]:
        if isinstance(edge, dict):
            edges_by_source.setdefault(edge.get("source"), []).append(edge)

    upgraded = False
    for source, edges in edges_by_source.items():
        node = node_by_id.get(source)
        if (
            not node
            or node.get("type") not in {"note", "experience"}
            or node.get("revision")
        ):
            continue
        evidence_items = [
            evidence
            for edge in edges
            for evidence in (edge.get("evidence") or [])
            if isinstance(evidence, dict)
        ]
        if (
            not evidence_items
            or any(evidence.get("source_revision") for evidence in evidence_items)
        ):
            continue
        synthetic_revision = "0" * 64
        node["revision"] = synthetic_revision
        for evidence in evidence_items:
            evidence["source_revision"] = synthetic_revision
        upgraded = True

    if not upgraded:
        return False
    try:
        validate_memory_graph(
            candidate,
            units,
            allow_legacy=False,
            expected_generation_id=generation_id,
        )
    except (TypeError, ValueError):
        return False
    return True


def _script_compilation_command(repo_root):
    scripts_dir = os.path.join(repo_root, "scripts")
    scripts = tuple(
        str(path)
        for path in sorted(Path(scripts_dir).glob("*.py"))
        if path.is_file()
    )
    return (sys.executable, "-B", "-c", PYTHON_COMPILE_CODE, *scripts)


def _ci_checks(repo_root, runner):
    python = sys.executable
    return [
        _command_check(
            "unit-tests",
            (
                python,
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                os.path.join(repo_root, "tests"),
            ),
            repo_root,
            600,
            runner,
        ),
        _command_check(
            "runtime-evaluation",
            (
                python,
                "-B",
                os.path.join(repo_root, "scripts", "evaluate_memory_runtime.py"),
                "--fixtures",
                os.path.join(repo_root, "tests", "fixtures", "memory_runtime"),
            ),
            repo_root,
            120,
            runner,
        ),
        _command_check(
            "git-diff-check",
            (_git_binary(), "diff", "--check"),
            repo_root,
            60,
            runner,
        ),
    ]


def _producer_replica_live_checks(repo_root, cfg, runner):
    if _scheduler_platform() != "windows":
        return [
            DoctorCheck(
                "windows-task",
                True,
                False,
                "producer-replica live profile requires Windows",
            )
        ]
    return [
        _runtime_release_check(cfg, require_versioned=True),
        _windows_task_check(
            cfg,
            repo_root,
            _runtime_root(cfg),
            runner,
        )
    ]


def _live_checks(repo_root, cfg, runner):
    vault = _expanded(cfg.get("vault_path"))
    python = sys.executable
    runtime_root = _runtime_root(cfg)
    scheduler_checks = (
        [_windows_task_check(cfg, repo_root, runtime_root, runner)]
        if _scheduler_platform() == "windows"
        else [
            _launchd_plists_check(cfg, runtime_root),
            _launchd_services_check(cfg, repo_root, runtime_root, runner),
        ]
    )
    checks = [
        _runtime_release_check(cfg),
        _command_check(
            "frontmatter",
            (
                python,
                "-B",
                os.path.join(repo_root, "scripts", "validate_frontmatter.py"),
                vault,
            ),
            repo_root,
            120,
            runner,
        ),
        _command_check(
            "wikilinks",
            (
                python,
                "-B",
                os.path.join(repo_root, "scripts", "link_validator.py"),
                vault,
            ),
            repo_root,
            120,
            runner,
        ),
        _candidate_isolation_check(cfg),
        _codex_hooks_check(cfg, runtime_root),
        *scheduler_checks,
        _prompt_hook_probe(repo_root, runtime_root, runner),
    ]
    return checks


def _runtime_release_check(cfg, *, require_versioned=False):
    try:
        from install_runtime import verify_installed_release

        value = cfg.get("runtime_root") or cfg.get("runtime_install_root")
        runtime_root = Path(_expanded(value or DEFAULT_RUNTIME_ROOT))
        release = verify_installed_release(runtime_root)
        release_id = release["release_id"]
        if require_versioned and (
            runtime_root.parent.name != "releases"
            or runtime_root.name != release_id
        ):
            raise ValueError(
                "runtime release_id does not match configured versioned runtime_root"
            )
    except Exception as exc:
        return DoctorCheck(
            "runtime-release",
            True,
            False,
            str(exc),
        )
    return DoctorCheck(
        "runtime-release",
        True,
        True,
        f"ok (release_id={release_id}, files={release['file_count']})",
    )


def _scheduler_platform():
    return "windows" if os.name == "nt" else "macos"


def _windows_task_user():
    from install_beacon_sync import _current_windows_user

    return _current_windows_user()


def _windows_task_check(cfg, repo_root, runtime_root, runner):
    from install_beacon_sync import (
        TASK_NAMESPACE,
        WINDOWS_TASK_NAME,
        _normalized_task_xml,
        build_windows_task_xml,
    )

    errors = []
    query = _invoke_runner(
        runner,
        (
            "schtasks.exe",
            "/Query",
            "/TN",
            WINDOWS_TASK_NAME,
            "/XML",
        ),
        cwd=repo_root,
        timeout=30,
    )
    if isinstance(query, DoctorCheck):
        errors.append(query.details)
    elif query.returncode:
        errors.append(f"task query failed: {_command_details(query)}")
    else:
        try:
            actual_root = ET.fromstring(str(query.stdout or "").encode("utf-8"))
            namespace = {"task": TASK_NAMESPACE}
            interval = actual_root.find(
                ".//task:CalendarTrigger/task:Repetition/task:Interval",
                namespace,
            )
            interval_text = str(interval.text if interval is not None else "")
            match = re.fullmatch(r"PT([1-9][0-9]*)M", interval_text)
            if match is None:
                raise ValueError("periodic trigger interval is invalid")
            expected = build_windows_task_xml(
                python_path=_expanded(cfg.get("python_path") or sys.executable),
                script_path=os.path.join(
                    runtime_root,
                    "scripts",
                    "beacon_sync.py",
                ),
                config_path=os.path.join(
                    runtime_root,
                    "scripts",
                    "config.yaml",
                ),
                user_id=_windows_task_user(),
                interval_minutes=int(match.group(1)),
            )
            if _normalized_task_xml(query.stdout) != _normalized_task_xml(expected):
                raise ValueError(
                    "task definition, ownership, user, triggers, or action differs"
                )
        except Exception as exc:
            errors.append(f"task definition is invalid: {exc}")

    status = _invoke_runner(
        runner,
        (
            "schtasks.exe",
            "/Query",
            "/TN",
            WINDOWS_TASK_NAME,
            "/FO",
            "CSV",
            "/NH",
            "/V",
        ),
        cwd=repo_root,
        timeout=30,
    )
    if isinstance(status, DoctorCheck):
        errors.append(status.details)
    elif status.returncode:
        errors.append(f"task status query failed: {_command_details(status)}")
    else:
        try:
            rows = [
                row
                for row in csv.reader(io.StringIO(str(status.stdout or "")))
                if any(str(value).strip() for value in row)
            ]
            if len(rows) != 1 or len(rows[0]) < 7:
                raise ValueError("task status output is incomplete")
            task_name = rows[0][1].strip().lstrip("\\")
            task_status = rows[0][3].strip()
            if task_name.casefold() != WINDOWS_TASK_NAME.casefold() or not task_status:
                raise ValueError("task status identity is invalid")
            last_result_text = rows[0][6].strip()
            try:
                last_result = int(
                    last_result_text,
                    16 if last_result_text.casefold().startswith("0x") else 10,
                )
            except ValueError as exc:
                raise ValueError("Task Scheduler Last Result is invalid") from exc
            if last_result not in {0, 267011}:
                raise ValueError(
                    "Task Scheduler Last Result indicates failure: "
                    + last_result_text
                )
        except Exception as exc:
            errors.append(f"task status is invalid: {exc}")
    return DoctorCheck(
        "windows-task",
        True,
        not errors,
        "ok" if not errors else "; ".join(errors),
    )


def _candidate_isolation_check(cfg):
    index_path = _recall_index_path(cfg)
    try:
        with open(index_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        units = payload.get("units")
        if not isinstance(units, list):
            raise ValueError("recall index units must be a list")
        forbidden = _candidate_roots(cfg)
        leaks = []
        for unit in units:
            if not isinstance(unit, dict):
                leaks.append("<invalid-unit>")
                continue
            haystack = " ".join(
                [
                    str(unit.get("path") or ""),
                    str(unit.get("source_note") or ""),
                    *(str(item) for item in unit.get("source_refs") or []),
                ]
            ).replace("\\", "/").casefold()
            if unit.get("status") not in (None, "active") or any(
                root in "/" + haystack.lstrip("/") for root in forbidden
            ):
                leaks.append(str(unit.get("id") or "<missing-id>"))
        if leaks:
            raise ValueError("candidate recall leakage: " + ", ".join(leaks[:10]))
        details = f"ok ({len(units)} runtime units)"
        return DoctorCheck("candidate-isolation", True, True, details)
    except Exception as exc:
        return DoctorCheck("candidate-isolation", True, False, str(exc))


def _candidate_roots(cfg):
    values = [
        (cfg.get("personal_memory") or {}).get(
            "candidate_dir", "04-Feedback/_memory-candidates"
        ),
        (cfg.get("skill_preferences") or {}).get(
            "candidate_dir", "04-Feedback/_skill-preferences"
        ),
        (cfg.get("workflow_memory") or {}).get(
            "candidate_dir", "04-Feedback/_workflow-candidates"
        ),
        (cfg.get("insight_memory") or {}).get(
            "candidate_dir", "04-Feedback/_insight-candidates"
        ),
        (cfg.get("error_evidence") or {}).get(
            "candidate_dir", "04-Feedback/_error-candidates"
        ),
        (cfg.get("annotation_quality") or {}).get(
            "candidate_dir", "04-Feedback/_annotation-candidates"
        ),
        (cfg.get("memory_lifecycle") or {}).get(
            "proposal_dir", "04-Feedback/_lifecycle-proposals"
        ),
        (cfg.get("memory_promotion") or {}).get(
            "proposal_dir", "04-Feedback/_promotion-proposals"
        ),
        "04-Feedback/_raw-sessions",
    ]
    return tuple(
        "/" + str(value).replace("\\", "/").strip("/").casefold() + "/"
        for value in values
        if value
    )


def _recall_index_path(cfg):
    vault = _expanded(cfg.get("vault_path"))
    if not vault or not os.path.isabs(vault):
        raise ValueError("vault_path is not an absolute path")
    runtime = cfg.get("memory_runtime") or {}
    value = runtime.get("resolved_index_path") or runtime.get("index_path")
    raw = os.path.expandvars(
        os.path.expanduser(str(value or "05-Agent-Memory/recall-index.json"))
    )
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    return os.path.abspath(os.path.join(vault, raw))


def _codex_hooks_check(cfg, runtime_root):
    hooks_path = os.path.join(
        _expanded(cfg.get("codex_home") or "~/.codex"),
        "hooks.json",
    )
    expected = {
        "SessionStart": ("session_harvester.py", ("--mode", "start"), 120),
        "UserPromptSubmit": ("codex_prompt_hook.py", (), 2),
        "Stop": (
            "session_harvester.py",
            ("--mode", "stop", "--agent", "codex"),
            120,
        ),
    }
    errors = []
    try:
        with open(hooks_path, "r", encoding="utf-8") as handle:
            hooks = json.load(handle)
        events = hooks.get("hooks")
        if not isinstance(events, dict):
            raise ValueError("hooks.json has no hooks mapping")
        for event, (script_name, arguments, expected_timeout) in expected.items():
            owned = []
            for group in events.get(event, []):
                if not isinstance(group, dict):
                    continue
                for hook in group.get("hooks", []):
                    command = str(hook.get("command") or "") if isinstance(hook, dict) else ""
                    if OWNED_HOOK_MARKER in command:
                        owned.append((hook, command))
            if len(owned) != 1:
                errors.append(f"{event} has {len(owned)} owned hooks")
                continue
            hook, command = owned[0]
            try:
                tokens = shlex.split(command)
            except ValueError:
                errors.append(f"{event} command is malformed")
                continue
            if (
                len(tokens) < 4
                or tokens[0] != OWNED_HOOK_MARKER
                or tokens[2] != "-B"
            ):
                errors.append(f"{event} command is malformed")
                continue
            python_path, script_path = tokens[1], tokens[3]
            for path in (python_path, script_path):
                if not _path_owned_by(path, runtime_root):
                    errors.append(f"{event} path outside stable runtime: {path}")
            if os.path.basename(script_path) != script_name:
                errors.append(f"{event} uses unexpected script: {script_path}")
            expected_python = os.path.join(runtime_root, ".venv", "bin", "python")
            expected_script = os.path.join(runtime_root, "scripts", script_name)
            exact_command = (
                len(tokens) == 4 + len(arguments)
                and tokens[4:] == list(arguments)
                and os.path.realpath(python_path) == os.path.realpath(expected_python)
                and os.path.realpath(script_path) == os.path.realpath(expected_script)
            )
            if not exact_command:
                errors.append(f"{event} does not use the exact command")
            if hook.get("type") != "command":
                errors.append(f"{event} hook type is not command")
            if hook.get("timeout") != expected_timeout:
                errors.append(f"{event} timeout is not {expected_timeout}")
    except Exception as exc:
        errors.append(str(exc))
    return DoctorCheck(
        "codex-hooks",
        True,
        not errors,
        "ok" if not errors else "; ".join(errors),
    )


def _launchd_plists_check(cfg, runtime_root):
    directory = _launch_agents_directory(cfg)
    errors = []
    jobs = _configured_launchd_scripts(cfg)
    expected_python = os.path.join(runtime_root, ".venv", "bin", "python")
    for label, expected_script in jobs.items():
        path = os.path.join(directory, label + ".plist")
        try:
            if os.path.islink(path):
                raise ValueError("plist is a symlink")
            with open(path, "rb") as handle:
                payload = plistlib.load(handle)
            if payload.get("Label") != label:
                errors.append(f"{label} has the wrong Label")
            arguments = payload.get("ProgramArguments")
            if not isinstance(arguments, list) or len(arguments) < 2:
                errors.append(f"{label} has invalid ProgramArguments")
                continue
            environment = payload.get("EnvironmentVariables")
            bytecode_disabled = (
                isinstance(environment, dict)
                and environment.get("PYTHONDONTWRITEBYTECODE") == "1"
            ) or "-B" in arguments[1:]
            if not bytecode_disabled:
                errors.append(f"{label} permits runtime bytecode writes")
            expected_script_path = os.path.join(
                runtime_root,
                "scripts",
                expected_script,
            )
            if (
                os.path.realpath(str(arguments[0]))
                != os.path.realpath(expected_python)
                or not _path_owned_by(arguments[0], runtime_root)
            ):
                errors.append(
                    f"{label} path outside stable runtime: {arguments[0]}"
                )
            script_matches = [
                value
                for value in arguments[1:]
                if isinstance(value, str)
                and os.path.isabs(value)
                and os.path.realpath(value)
                == os.path.realpath(expected_script_path)
            ]
            if len(script_matches) != 1 or not _path_owned_by(
                expected_script_path,
                runtime_root,
            ):
                errors.append(
                    f"{label} uses unexpected script"
                )
            if label == "io.agent-memory-beacon.weekly" and "--full" in arguments[2:]:
                errors.append(f"{label} must not schedule --full scans")
            if label == SYNC_LAUNCHD_LABEL:
                expected_config = os.path.join(
                    runtime_root,
                    "scripts",
                    "config.yaml",
                )
                exact_sync_command = (
                    len(arguments) == 10
                    and arguments[1:6] == ["-E", "-s", "-X", "utf8", "-B"]
                    and arguments[7] == "--config"
                    and arguments[9] == "run"
                    and os.path.realpath(str(arguments[0]))
                    == os.path.realpath(expected_python)
                    and os.path.realpath(str(arguments[6]))
                    == os.path.realpath(expected_script_path)
                    and os.path.realpath(str(arguments[8]))
                    == os.path.realpath(expected_config)
                )
                if not exact_sync_command:
                    errors.append(f"{label} does not use the exact command")
            working = payload.get("WorkingDirectory")
            if not _path_owned_by(working, runtime_root, require_file=False):
                errors.append(
                    f"{label} WorkingDirectory outside stable runtime: {working}"
                )
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    return DoctorCheck(
        "launchd-plists",
        True,
        not errors,
        "ok" if not errors else "; ".join(errors),
    )


def _launch_agents_directory(cfg):
    configured = cfg.get("launch_agents_dir")
    if configured:
        return _expanded(configured)
    home = _expanded(cfg.get("user_home") or "~")
    return os.path.join(home, "Library", "LaunchAgents")


def _launchd_services_check(cfg, repo_root, runtime_root, runner):
    errors = []
    domain = f"gui/{os.getuid()}"
    for label, script_name in _configured_launchd_scripts(cfg).items():
        result = _invoke_runner(
            runner,
            ("/bin/launchctl", "print", f"{domain}/{label}"),
            cwd=repo_root,
            timeout=15,
        )
        if isinstance(result, DoctorCheck):
            errors.append(f"{label}: {result.details}")
        elif result.returncode != 0:
            errors.append(f"{label}: {_command_details(result)}")
        else:
            expected_python = os.path.join(runtime_root, ".venv", "bin", "python")
            expected_script = os.path.join(
                runtime_root,
                "scripts",
                script_name,
            )
            program, loaded_paths = _launchd_loaded_paths(result.stdout)
            if (
                not program
                or os.path.realpath(program) != os.path.realpath(expected_python)
                or not _path_owned_by(program, runtime_root)
            ):
                errors.append(f"{label}: loaded program is outside stable runtime")
            if not any(
                os.path.realpath(path) == os.path.realpath(expected_script)
                for path in loaded_paths
            ):
                errors.append(f"{label}: loaded script is outside stable runtime")
    for label in LEGACY_LAUNCHD_LABELS.values():
        result = _invoke_runner(
            runner,
            ("/bin/launchctl", "print", f"{domain}/{label}"),
            cwd=repo_root,
            timeout=15,
        )
        if isinstance(result, DoctorCheck):
            errors.append(f"{label}: {result.details}")
        elif result.returncode == 0:
            errors.append(f"{label}: legacy service is still loaded")
        elif not _missing_launchd_service(result):
            errors.append(f"{label}: {_command_details(result)}")
    return DoctorCheck(
        "launchd-services",
        True,
        not errors,
        "ok" if not errors else "; ".join(errors),
    )


def _configured_launchd_scripts(cfg):
    jobs = dict(LAUNCHD_SCRIPTS)
    sync_cfg = cfg.get("beacon_sync") if isinstance(cfg, dict) else None
    if (
        isinstance(sync_cfg, dict)
        and sync_cfg.get("enabled") is True
        and sync_cfg.get("role") == "authority"
    ):
        jobs[SYNC_LAUNCHD_LABEL] = "beacon_sync.py"
    return jobs


def _launchd_loaded_paths(output):
    program = ""
    paths = []
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        key = ""
        value = line
        if "=" in line:
            key, value = (part.strip() for part in line.split("=", 1))
        value = value.strip().strip('"')
        if not os.path.isabs(value):
            continue
        value = os.path.abspath(value)
        paths.append(value)
        if key == "program":
            program = value
    return program, tuple(paths)


def _missing_launchd_service(result):
    if result.returncode == 0:
        return False
    detail = _command_details(result).lower()
    return any(
        marker in detail
        for marker in (
            "no such process",
            "no such service",
            "could not find service",
            "service is not loaded",
            "service not loaded",
        )
    )


def _prompt_hook_probe(repo_root, runtime_root, runner):
    python_path = os.path.join(runtime_root, ".venv", "bin", "python")
    script_path = os.path.join(runtime_root, "scripts", "codex_prompt_hook.py")
    if not all(_path_owned_by(path, runtime_root) for path in (python_path, script_path)):
        return DoctorCheck(
            "prompt-hook-probe", True, False, "runtime prompt hook is unavailable"
        )
    result = _invoke_runner(
        runner,
        (python_path, "-B", script_path),
        cwd=repo_root,
        timeout=5,
        input_text='{"hook_event_name":"DoctorProbe"}\n',
    )
    if isinstance(result, DoctorCheck):
        return DoctorCheck("prompt-hook-probe", True, False, result.details)
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        payload = None
    passed = result.returncode == 0 and payload == {}
    return DoctorCheck(
        "prompt-hook-probe",
        True,
        passed,
        "ok" if passed else _command_details(result) or "invalid hook JSON",
    )


def _command_check(name, args, cwd, timeout, runner, input_text=None):
    result = _invoke_runner(
        runner,
        args,
        cwd=cwd,
        timeout=timeout,
        input_text=input_text,
    )
    if isinstance(result, DoctorCheck):
        return DoctorCheck(name, True, False, result.details)
    passed = result.returncode == 0
    return DoctorCheck(
        name=name,
        required=True,
        passed=passed,
        details="ok" if passed else _command_details(result),
    )


def _invoke_runner(runner, args, *, cwd, timeout, input_text=None):
    if isinstance(args, str) or not isinstance(args, (tuple, list)):
        raise TypeError("doctor commands must be argument sequences")
    command = tuple(os.fspath(arg) for arg in args)
    try:
        value = runner(
            command,
            cwd=cwd,
            timeout=timeout,
            input_text=input_text,
        )
    except subprocess.TimeoutExpired:
        return DoctorCheck("command", True, False, f"timeout after {timeout}s")
    except Exception as exc:
        return DoctorCheck("command", True, False, f"runner error: {exc}")
    if isinstance(value, CommandResult):
        return value
    return CommandResult(
        returncode=int(value.returncode),
        stdout=str(value.stdout or ""),
        stderr=str(value.stderr or ""),
    )


def _subprocess_runner(args, *, cwd, timeout, input_text=None):
    completed = subprocess.run(
        list(args),
        cwd=cwd,
        timeout=timeout,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        shell=False,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _command_details(result):
    detail = str(result.stderr or result.stdout or "").strip()
    return detail[:1000] or f"exit {result.returncode}"


def _runtime_root(cfg):
    value = cfg.get("runtime_root") or cfg.get("runtime_install_root")
    return os.path.realpath(_expanded(value or DEFAULT_RUNTIME_ROOT))


def _path_owned_by(path, root, require_file=True):
    if not isinstance(path, (str, os.PathLike)) or not path:
        return False
    candidate = os.path.realpath(_expanded(path))
    root = os.path.realpath(_expanded(root))
    try:
        inside = os.path.commonpath([candidate, root]) == root
    except ValueError:
        return False
    if not inside or candidate == root:
        return False
    return os.path.isfile(candidate) if require_file else os.path.isdir(candidate)


def _expanded(value):
    return os.path.abspath(
        os.path.expandvars(os.path.expanduser(os.fspath(value or "")))
    ) if value else ""


def _git_binary():
    return "/usr/bin/git" if os.path.isfile("/usr/bin/git") else "git"


def render_report(report, json_output=False):
    if json_output:
        return json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    lines = [
        f"Doctor {report.profile}: {'通过' if report.passed else '失败'}"
    ]
    for check in report.checks:
        marker = "PASS" if check.passed else "FAIL"
        lines.append(f"[{marker}] {check.name}: {check.details}")
    return "\n".join(lines)


def main(argv=None, *, config_loader=None, runner=None):
    parser = argparse.ArgumentParser(description="Agent Memory Beacon health checks")
    parser.add_argument("--profile", choices=PROFILE_NAMES, default="quick")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    args = parser.parse_args(argv)
    if config_loader is None:
        from config import load_config

        config_loader = load_config
    try:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            cfg = config_loader()
        report = run_profile(
            args.profile,
            repo_root=args.repo_root,
            cfg=cfg,
            runner=runner,
        )
    except Exception as exc:
        report = DoctorReport(
            profile=args.profile,
            checks=(
                DoctorCheck(
                    "configuration",
                    True,
                    False,
                    f"{type(exc).__name__}: {exc}",
                ),
            ),
        )
    print(render_report(report, json_output=args.json))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
