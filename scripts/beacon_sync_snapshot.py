"""Canonical Vault snapshot publication, receipts, and read-only replicas."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import unicodedata
from pathlib import Path

from beacon_sync_protocol import (
    PROTOCOL_COMPLETE,
    PROTOCOL_CURRENT,
    PROTOCOL_RECEIPT,
    PROTOCOL_SNAPSHOT,
    ProtocolError,
    canonical_json_bytes,
    portable_atomic_write,
    portable_ensure_directory_tree,
    portable_file_lock,
    portable_rmdir_empty,
    portable_rmtree,
    portable_unlink_regular,
    read_bounded_regular_file,
    sha256_bytes,
    validate_replica_path,
    write_immutable,
)
from beacon_sync_reducer import (
    ReducerError,
    bind_pending_receipt_generation,
    bound_receipt_events,
    mark_receipts_published,
    unbound_pending_receipt_count,
)
from safety import ensure_directory_tree, exclusive_file_lock


SNAPSHOT_FIELDS = frozenset(
    {
        "protocol",
        "schema_version",
        "generation",
        "generation_id",
        "parent_generation",
        "parent_generation_id",
        "files",
        "tombstones",
    }
)
COMPLETE_FIELDS = frozenset(
    {
        "protocol",
        "schema_version",
        "generation",
        "generation_id",
        "snapshot_sha256",
        "file_count",
        "object_bytes",
    }
)
CURRENT_FIELDS = frozenset(
    {
        "protocol",
        "schema_version",
        "generation",
        "generation_id",
        "snapshot_sha256",
    }
)
RECEIPT_FIELDS = frozenset(
    {
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
)
FILE_FIELDS = frozenset({"path", "bytes", "sha256", "content_class"})
TOMBSTONE_FIELDS = frozenset({"path", "deleted_at_generation"})
ACTIVE_PROTOCOL = "agent-memory-beacon-sync-active"
JOURNAL_PROTOCOL = "agent-memory-beacon-sync-apply-journal"
JOURNAL_FIELDS = frozenset(
    {
        "protocol",
        "schema_version",
        "target_generation",
        "target_generation_id",
        "target_snapshot_sha256",
        "operations",
    }
)
JOURNAL_OPERATION_FIELDS = frozenset(
    {
        "path",
        "action",
        "target_bytes",
        "target_sha256",
        "before_identity",
        "existed",
        "backup",
        "mode",
        "bytes",
        "sha256",
    }
)
DEFAULT_MAX_OBJECT_BYTES = 64 * 1024 * 1024
MAX_RECEIPT_BYTES = 128 * 1024
MAX_APPLY_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_REPLICA_FILE_COUNT = 250_000
MAX_REPLICA_TOTAL_BYTES = 256 * 1024 * 1024 * 1024
MAX_STALE_STAGING_GENERATIONS = 4096
MAX_STALE_ROLLBACK_GENERATIONS = 4096
MAX_GENERATIONS_PER_MATERIALIZE_RUN = 32
MAX_RECEIPT_REFERENCES_PER_PUBLISH_RUN = 1024
SIGNED_IDENTITY_MAX = (1 << 63) - 1
UNSIGNED_IDENTITY_MAX = (1 << 128) - 1
UNSIGNED_IDENTITY_RE = re.compile(r"^u128:[0-9a-f]{32}$")
RECEIPT_REFERENCE_SCAN_PROTOCOL = (
    "agent-memory-beacon-sync-receipt-reference-scan"
)
EXCLUDED_DIRECTORY_PARTS = frozenset(
    {
        ".git",
        "__pycache__",
        ".cache",
        "_logs",
        "_rollback",
        "_cleanup-backups",
        "_raw-sessions",
    }
)
ALLOWED_VAULT_ROOTS = frozenset(
    {
        "00-inbox",
        "00-rules",
        "01-projects",
        "02-areas",
        "02-templates",
        "03-maps",
        "04-feedback",
        "05-agent-memory",
        "attachments",
        "notes",
        "sessions",
    }
)
ALLOWED_ROOT_FILE_SUFFIXES = frozenset(
    {".md", ".base", ".canvas", ".pdf", ".txt"}
)
EXCLUDED_EXACT_PATHS = frozenset(
    {
        ".obsidian/workspace",
        ".obsidian/workspace.json",
        ".obsidian/workspace-mobile.json",
    }
)
EXCLUDED_SECRET_NAMES = frozenset(
    {
        "auth.json",
        "credentials.json",
        "credential.json",
        "secrets.json",
        "secret.json",
        "tokens.json",
        "token.json",
        ".env",
        "config.json",
        "config.yaml",
        "config.yml",
        "data.json",
        "preferences.json",
        "settings.json",
        "settings.yaml",
        "settings.yml",
    }
)
EXCLUDED_SUFFIXES = (
    ".lock",
    ".log",
    ".tmp",
    ".temp",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".db-wal",
    ".db-shm",
    ".wal",
    ".shm",
    "~",
)
HIGH_CONFIDENCE_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-(?:live|proj|test)-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgh[opusr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)
SECRET_SCANNED_SUFFIXES = frozenset(
    {
        ".md",
        ".txt",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".env",
        ".py",
        ".sh",
        ".js",
        ".ts",
    }
)


class PublisherError(RuntimeError):
    """A canonical generation could not be published safely."""


class MaterializeError(RuntimeError):
    """A received generation could not be verified or applied safely."""


def publish_generation(cfg, sync_cfg, now=None):
    """Seal and bind one authority generation under the shared cycle lock."""
    state_dir = _required_root(sync_cfg, "state_dir", PublisherError)
    state_dir.mkdir(parents=True, exist_ok=True)
    with portable_file_lock(
        state_dir / "authority-cycle.lock",
        root=state_dir,
    ):
        return _publish_generation_locked(cfg, sync_cfg, now=now)


def _publish_generation_locked(cfg, sync_cfg, now=None):
    """Seal the current canonical Vault as one content-addressed generation."""
    del now
    vault = _required_root(cfg, "vault_path", PublisherError)
    state_dir = _required_root(sync_cfg, "state_dir", PublisherError)
    published = _required_root(sync_cfg, "published_dir", PublisherError)
    max_object_bytes = _configured_max_object_bytes(sync_cfg, PublisherError)
    if not vault.is_dir():
        raise PublisherError("canonical Vault path is not a directory")
    state_dir.mkdir(parents=True, exist_ok=True)
    published.mkdir(parents=True, exist_ok=True)
    with portable_file_lock(state_dir / "publisher.lock", root=state_dir):
        try:
            cleanup = _new_published_cleanup_report()
            current, previous = _load_published_current(
                published,
                max_object_bytes,
            )
            (
                receipt_references,
                receipt_scan_complete,
                pending_receipt_references,
            ) = (
                _load_receipt_generation_references(
                    published,
                    state_dir,
                    cleanup,
                )
            )
            head, previous = _recover_publication_head(
                published,
                current,
                previous,
                max_object_bytes,
                receipt_references,
                receipt_scan_complete,
                cleanup,
            )
            harvester_lock = (
                vault / "04-Feedback" / "_logs" / "harvester.lock"
            )
            ensure_directory_tree(harvester_lock.parent, vault)
            with exclusive_file_lock(harvester_lock, root=vault):
                files = _collect_vault_files(
                    vault,
                    state_dir,
                    published,
                    sync_cfg,
                )
            previous_files = previous["files"] if previous else []
            if _file_identity(files) == _file_identity(previous_files):
                if head is not None:
                    adopted = current is None or any(
                        current[key] != head[key]
                        for key in (
                            "generation",
                            "generation_id",
                            "snapshot_sha256",
                        )
                    )
                    if adopted:
                        portable_atomic_write(
                            published / "v1" / "current.json",
                            canonical_json_bytes(head),
                            root=published,
                        )
                    _cleanup_published_storage(
                        published,
                        max_object_bytes,
                        receipt_references,
                        receipt_scan_complete,
                        cleanup,
                    )
                    bound_receipts = bind_pending_receipt_generation(
                        sync_cfg,
                        head["generation"],
                        head["generation_id"],
                        files,
                    )
                    return {
                        "changed": adopted,
                        "generation": head["generation"],
                        "generation_id": head["generation_id"],
                        "snapshot_sha256": head["snapshot_sha256"],
                        "bound_receipts": bound_receipts,
                        "cleanup": cleanup,
                        "limited": pending_receipt_references > 0,
                        "pending_receipt_references": pending_receipt_references,
                    }

            generation = 1 if head is None else head["generation"] + 1
            parent_generation = 0 if head is None else head["generation"]
            parent_generation_id = "" if head is None else head["generation_id"]
            previous_paths = {item["path"] for item in previous_files}
            current_paths = {item["path"] for item in files}
            tombstones = [
                {"path": path, "deleted_at_generation": generation}
                for path in sorted(previous_paths - current_paths)
            ]
            identity = {
                "parent_generation_id": parent_generation_id,
                "files": files,
                "deleted_paths": [item["path"] for item in tombstones],
            }
            generation_id = "generation-" + sha256_bytes(
                canonical_json_bytes(identity)
            )
            snapshot = {
                "protocol": PROTOCOL_SNAPSHOT,
                "schema_version": 1,
                "generation": generation,
                "generation_id": generation_id,
                "parent_generation": parent_generation,
                "parent_generation_id": parent_generation_id,
                "files": files,
                "tombstones": tombstones,
            }
            _assert_apply_journal_materializable(
                previous,
                snapshot,
                PublisherError,
            )
            snapshot_bytes = canonical_json_bytes(snapshot)
            snapshot_sha256 = sha256_bytes(snapshot_bytes)
            complete = {
                "protocol": PROTOCOL_COMPLETE,
                "schema_version": 1,
                "generation": generation,
                "generation_id": generation_id,
                "snapshot_sha256": snapshot_sha256,
                "file_count": len(files),
                "object_bytes": sum(item["bytes"] for item in files),
            }
            current_value = _current_value(snapshot, snapshot_bytes)
            generation_dir = (
                published / "v1" / "snapshots" / f"{generation:020d}"
            )
            write_immutable(
                generation_dir / "snapshot.json",
                snapshot_bytes,
                root=published,
            )
            write_immutable(
                generation_dir / "complete.json",
                canonical_json_bytes(complete),
                root=published,
            )
            portable_atomic_write(
                published / "v1" / "current.json",
                canonical_json_bytes(current_value),
                root=published,
            )
            _cleanup_published_storage(
                published,
                max_object_bytes,
                receipt_references,
                receipt_scan_complete,
                cleanup,
            )
            bound_receipts = bind_pending_receipt_generation(
                sync_cfg,
                generation,
                generation_id,
                files,
            )
            return {
                "changed": True,
                "generation": generation,
                "generation_id": generation_id,
                "snapshot_sha256": snapshot_sha256,
                "bound_receipts": bound_receipts,
                "cleanup": cleanup,
                "limited": pending_receipt_references > 0,
                "pending_receipt_references": pending_receipt_references,
            }
        except PublisherError:
            raise
        except (
            MaterializeError,
            OSError,
            ProtocolError,
            ReducerError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            raise PublisherError(str(exc)) from exc


def publish_pending_receipts(sync_cfg, generation_info=None, now=None):
    """Idempotently publish only ledger rows already bound to sealed history."""
    del now
    state_dir = _required_root(sync_cfg, "state_dir", PublisherError)
    published = _required_root(sync_cfg, "published_dir", PublisherError)
    max_object_bytes = _configured_max_object_bytes(sync_cfg, PublisherError)
    state_dir.mkdir(parents=True, exist_ok=True)
    with portable_file_lock(state_dir / "publisher.lock", root=state_dir):
        verified_generations = {}
        if generation_info is not None:
            if not isinstance(generation_info, dict):
                raise PublisherError("receipt generation information is invalid")
            generation = generation_info.get("generation")
            generation_id = generation_info.get("generation_id")
            snapshot, snapshot_bytes = _verify_receipt_generation(
                published,
                generation,
                generation_id,
                max_object_bytes,
                verified_generations,
            )
            if (
                generation_info.get("snapshot_sha256")
                != sha256_bytes(snapshot_bytes)
            ):
                raise PublisherError(
                    "receipt generation snapshot hash does not match sealed history"
                )
            if snapshot["generation"] != generation:
                raise PublisherError("receipt generation information is invalid")

        rows = bound_receipt_events(sync_cfg)
        published_keys = []
        action_count = 0
        for row in rows:
            row_status = row["status"]
            is_pending = row_status.endswith("_pending_publish")
            status = row_status.removesuffix("_pending_publish")
            if status not in {"applied", "noop", "rejected"}:
                raise PublisherError("receipt ledger row has an unsupported status")
            generation = row["canonical_generation"]
            generation_id = row["generation_id"]
            _verify_receipt_generation(
                published,
                generation,
                generation_id,
                max_object_bytes,
                verified_generations,
            )
            receipt = receipt_document_for_row(row)
            receipt_path = (
                published
                / "v1"
                / "receipts"
                / row["producer_instance_id"]
                / f"{int(row['seq']):020d}-{row['event_id']}.json"
            )
            try:
                receipt_bytes = canonical_json_bytes(receipt)
                if len(receipt_bytes) > MAX_RECEIPT_BYTES:
                    raise PublisherError("receipt exceeds the size limit")
                created = write_immutable(
                    receipt_path,
                    receipt_bytes,
                    root=published,
                )
            except (OSError, ProtocolError) as exc:
                raise PublisherError(str(exc)) from exc
            if is_pending:
                published_keys.append(
                    (
                        row["producer_instance_id"],
                        int(row["seq"]),
                        status,
                        int(generation),
                        generation_id,
                    )
                )
                action_count += 1
            elif created:
                action_count += 1
        if published_keys:
            try:
                mark_receipts_published(sync_cfg, published_keys)
            except (OSError, ReducerError, sqlite3.Error) as exc:
                raise PublisherError(str(exc)) from exc
        return {
            "published": action_count,
            "verified": len(rows),
            "deferred_unbound": unbound_pending_receipt_count(sync_cfg),
        }


def receipt_document_for_row(row):
    row_status = str(row["status"])
    status = row_status.removesuffix("_pending_publish")
    if status not in {"applied", "noop", "rejected"}:
        raise PublisherError("receipt ledger row has an unsupported status")
    return {
        "protocol": PROTOCOL_RECEIPT,
        "schema_version": 1,
        "producer_instance_id": row["producer_instance_id"],
        "seq": int(row["seq"]),
        "event_id": row["event_id"],
        "event_sha256": row["event_sha256"],
        "status": status,
        "code": row["code"],
        "canonical_generation": int(row["canonical_generation"]),
        "generation_id": row["generation_id"],
        "gc_allowed": True,
        "processed_at": row["processed_at"],
    }


def _verify_receipt_generation(
    published,
    generation,
    generation_id,
    max_object_bytes,
    cache,
):
    if (
        not _positive_int(generation)
        or not _is_generation_id(generation_id)
    ):
        raise PublisherError("pending receipt generation binding is invalid")
    key = (int(generation), str(generation_id))
    if key in cache:
        return cache[key]
    try:
        snapshot, snapshot_bytes = _load_received_generation_number(
            published,
            int(generation),
            max_object_bytes,
        )
    except (
        MaterializeError,
        OSError,
        ProtocolError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise PublisherError(
            "pending receipt generation is not sealed and verifiable"
        ) from exc
    if snapshot["generation_id"] != generation_id:
        raise PublisherError(
            "pending receipt generation identity does not match sealed history"
        )
    cache[key] = (snapshot, snapshot_bytes)
    return cache[key]


def materialize_generation(
    sync_cfg,
    now=None,
    fault_point=None,
    *,
    bootstrap=False,
):
    """Verify and atomically apply the latest sealed read-only generation."""
    del now
    state_dir = _required_root(sync_cfg, "state_dir", MaterializeError)
    received = _required_root(
        sync_cfg, "received_published_dir", MaterializeError
    )
    replica = _required_root(sync_cfg, "replica_path", MaterializeError)
    max_object_bytes = _configured_max_object_bytes(
        sync_cfg,
        MaterializeError,
    )
    state_dir.mkdir(parents=True, exist_ok=True)
    replica.mkdir(parents=True, exist_ok=True)
    replica_state = state_dir / "replica"
    replica_state.mkdir(parents=True, exist_ok=True)
    with portable_file_lock(replica_state / "materializer.lock", root=state_dir):
        try:
            _cleanup_stale_staging(replica_state)
            _cleanup_stale_rollback(replica_state)
            _recover_interrupted_apply(
                replica,
                replica_state,
                max_object_bytes,
            )
            current_path = received / "v1" / "current.json"
            if not current_path.is_file():
                return {
                    "changed": False,
                    "reason": "no_current",
                    "cleanup": _new_replica_cleanup_report(None),
                    "limited": False,
                    "pending_generations": 0,
                }
            current = _load_json(
                current_path,
                max_bytes=_metadata_read_limit(
                    max_object_bytes,
                    16 * 1024,
                ),
                root=received,
                name="current",
            )
            _validate_current(current)
            active = _load_active_marker(
                replica_state,
                max_object_bytes,
            )
            if active and active["generation"] > current["generation"]:
                raise MaterializeError(
                    "snapshot parent history is older than active generation"
                )
            previous = (
                _load_active_snapshot(
                    replica_state,
                    active,
                    max_object_bytes,
                )
                if active
                else None
            )
            if active is None:
                if not bootstrap:
                    raise MaterializeError(
                        "replica is not initialized; explicit bootstrap is required"
                    )
                _assert_bootstrap_replica_safe(replica)
            elif bootstrap:
                raise MaterializeError(
                    "replica is already initialized; bootstrap is not allowed"
                )
            if active and active["generation"] == current["generation"]:
                if (
                    active["generation_id"] != current["generation_id"]
                    or active["snapshot_sha256"] != current["snapshot_sha256"]
                ):
                    raise MaterializeError("active generation identity conflict")
                _verify_replica_drift(
                    replica,
                    previous,
                    previous,
                    max_object_bytes,
                )
                cleanup = _cleanup_inactive_active_snapshots(
                    replica_state,
                    active["generation"],
                )
                return {
                    "changed": False,
                    "generation": active["generation"],
                    "generation_id": active["generation_id"],
                    "cleanup": cleanup,
                    "limited": False,
                    "pending_generations": 0,
                }

            if active is None:
                generations = (current["generation"],)
                pending_generations = 0
            else:
                generation_limit = int(MAX_GENERATIONS_PER_MATERIALIZE_RUN)
                if generation_limit <= 0:
                    raise MaterializeError(
                        "generation materialization limit is invalid"
                    )
                final_generation = min(
                    current["generation"],
                    active["generation"] + generation_limit,
                )
                generations = range(
                    active["generation"] + 1,
                    final_generation + 1,
                )
                pending_generations = current["generation"] - final_generation
            snapshot = None
            for generation in generations:
                if generation == current["generation"]:
                    snapshot, snapshot_bytes = _load_received_generation(
                        received,
                        current,
                        max_object_bytes,
                    )
                else:
                    snapshot, snapshot_bytes = (
                        _load_received_generation_number(
                            received,
                            generation,
                            max_object_bytes,
                        )
                    )
                if previous and (
                    snapshot["parent_generation"] != previous["generation"]
                    or snapshot["parent_generation_id"]
                    != previous["generation_id"]
                ):
                    raise MaterializeError(
                        "snapshot parent does not match active generation"
                    )
                _validate_snapshot_transition(previous, snapshot)
                _assert_apply_journal_materializable(
                    previous,
                    snapshot,
                    MaterializeError,
                )
                staging = None
                try:
                    staging = _stage_generation(
                        received,
                        replica_state,
                        previous,
                        snapshot,
                        max_object_bytes,
                    )
                    _verify_replica_drift(
                        replica,
                        previous,
                        snapshot,
                        max_object_bytes,
                    )
                    _apply_generation(
                        replica,
                        replica_state,
                        snapshot,
                        snapshot_bytes,
                        previous,
                        staging,
                        max_object_bytes,
                        fault_point=fault_point,
                    )
                except Exception:
                    if (
                        staging is not None
                        and not os.path.lexists(
                            replica_state / "apply-journal.json"
                        )
                    ):
                        _discard_managed_tree(staging, replica_state)
                    raise
                previous = snapshot
            if snapshot is None:
                raise MaterializeError("no generation was selected for materialization")
            cleanup = _cleanup_inactive_active_snapshots(
                replica_state,
                snapshot["generation"],
            )
            return {
                "changed": True,
                "generation": snapshot["generation"],
                "generation_id": snapshot["generation_id"],
                "cleanup": cleanup,
                "limited": pending_generations > 0,
                "pending_generations": pending_generations,
            }
        except MaterializeError:
            raise
        except (OSError, ProtocolError, ValueError, json.JSONDecodeError) as exc:
            raise MaterializeError(str(exc)) from exc


def inspect_published_generation(sync_cfg):
    """Verify the sealed published generation without changing publisher state."""
    published = _required_root(sync_cfg, "published_dir", MaterializeError)
    max_object_bytes = _configured_max_object_bytes(
        sync_cfg,
        MaterializeError,
    )
    current_path = published / "v1" / "current.json"
    if not current_path.is_file():
        raise MaterializeError("published current generation is missing")
    current = _load_json(
        current_path,
        max_bytes=_metadata_read_limit(max_object_bytes, 16 * 1024),
        root=published,
        name="current",
    )
    _validate_current(current)
    snapshot, _snapshot_bytes = _load_received_generation(
        published,
        current,
        max_object_bytes,
    )
    return {
        "generation": current["generation"],
        "generation_id": current["generation_id"],
        "files": len(snapshot["files"]),
    }


def inspect_replica_state(sync_cfg):
    """Verify an existing active replica marker, manifest, and managed files."""
    state_dir = _required_root(sync_cfg, "state_dir", MaterializeError)
    replica = _required_root(sync_cfg, "replica_path", MaterializeError)
    replica_state = state_dir / "replica"
    max_object_bytes = _configured_max_object_bytes(
        sync_cfg,
        MaterializeError,
    )
    active = _load_active_marker(replica_state, max_object_bytes)
    if active is None:
        return {"active": False}
    snapshot = _load_active_snapshot(
        replica_state,
        active,
        max_object_bytes,
    )
    _verify_replica_drift(
        replica,
        snapshot,
        snapshot,
        max_object_bytes,
    )
    return {
        "active": True,
        "generation": active["generation"],
        "generation_id": active["generation_id"],
        "files": len(snapshot["files"]),
    }


def inspect_received_generation(
    sync_cfg,
    generation,
    generation_id,
):
    """Verify one sealed received generation, including every referenced object."""
    received = _required_root(
        sync_cfg,
        "received_published_dir",
        MaterializeError,
    )
    if not _positive_int(generation) or not _is_generation_id(generation_id):
        raise MaterializeError("received generation identity is invalid")
    snapshot, _snapshot_bytes = _load_received_generation_number(
        received,
        int(generation),
        _configured_max_object_bytes(sync_cfg, MaterializeError),
    )
    if snapshot["generation_id"] != generation_id:
        raise MaterializeError("received generation ID does not match receipt")
    return {
        "generation": snapshot["generation"],
        "generation_id": snapshot["generation_id"],
        "files": len(snapshot["files"]),
    }


def _collect_vault_files(vault, state_dir, published, sync_cfg):
    max_bytes = _configured_max_object_bytes(sync_cfg, PublisherError)
    cache_path = state_dir / "snapshot-hash-cache.json"
    cache = _load_hash_cache(cache_path, state_dir)
    next_cache = {}
    files = []
    case_paths = {}
    for directory, names, filenames in os.walk(vault, topdown=True, followlinks=False):
        directory_path = Path(directory)
        kept_names = []
        for name in sorted(names):
            child = directory_path / name
            relative = child.relative_to(vault).as_posix()
            if _excluded_path(relative, is_directory=True):
                continue
            if child.is_symlink():
                raise PublisherError(f"Vault contains a directory symlink: {relative}")
            kept_names.append(name)
        names[:] = kept_names
        for filename in sorted(filenames):
            path = directory_path / filename
            relative = path.relative_to(vault).as_posix()
            if _excluded_path(relative, is_directory=False):
                continue
            validate_replica_path(relative)
            folded = relative.casefold()
            if folded in case_paths and case_paths[folded] != relative:
                raise PublisherError(
                    f"case-colliding Vault paths: {case_paths[folded]} and {relative}"
                )
            case_paths[folded] = relative
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise PublisherError(f"Vault entry is not a regular file: {relative}")
            if info.st_size > max_bytes:
                raise PublisherError(f"Vault object exceeds size limit: {relative}")
            stat_key = _stat_cache_key(info)
            cached = cache.get(relative)
            if (
                isinstance(cached, dict)
                and cached.get("stat") == stat_key
                and cached.get("safe_for_sync") is True
            ):
                digest = cached.get("sha256", "")
                if not _is_sha256(digest):
                    digest = ""
            else:
                digest = ""
            if not digest:
                data = _read_stable_file(path, info, max_bytes)
                digest = sha256_bytes(data)
            else:
                data = None
            if data is None:
                object_path = published / "v1" / "objects" / digest[:2] / digest
                if not object_path.is_file():
                    data = _read_stable_file(path, info, max_bytes)
                    if sha256_bytes(data) != digest:
                        digest = sha256_bytes(data)
                        info = os.lstat(path)
                        stat_key = _stat_cache_key(info)
            if data is not None:
                _assert_no_embedded_credential(relative, data)
            if data is None:
                object_path = published / "v1" / "objects" / digest[:2] / digest
                existing = read_bounded_regular_file(
                    object_path,
                    max_bytes=max_bytes,
                    root=published,
                )
                if len(existing) != info.st_size or sha256_bytes(existing) != digest:
                    raise PublisherError("content-addressed object is corrupt")
            else:
                object_path = published / "v1" / "objects" / digest[:2] / digest
                write_immutable(object_path, data, root=published)
            next_cache[relative] = {
                "stat": stat_key,
                "sha256": digest,
                "safe_for_sync": True,
            }
            files.append(
                {
                    "path": relative,
                    "bytes": int(info.st_size),
                    "sha256": digest,
                    "content_class": _content_class(relative),
                }
            )
    files.sort(key=lambda item: item["path"])
    _validate_path_tree(
        [item["path"] for item in files],
        PublisherError,
    )
    portable_atomic_write(
        cache_path,
        canonical_json_bytes({"schema_version": 1, "files": next_cache}),
        root=state_dir,
    )
    return files


def _excluded_path(relative, *, is_directory):
    parts = relative.split("/")
    lowered = [part.casefold() for part in parts]
    if not _allowed_snapshot_path(relative, is_directory=is_directory):
        return True
    if lowered[0] == ".obsidian":
        return True
    if any(part in EXCLUDED_DIRECTORY_PARTS for part in lowered):
        return True
    if relative in EXCLUDED_EXACT_PATHS:
        return True
    if relative.startswith(".obsidian/workspace"):
        return True
    if any(part in {"node_modules", ".venv", "venv"} for part in parts):
        return True
    if not is_directory:
        name = lowered[-1]
        if name in EXCLUDED_SECRET_NAMES:
            return True
        if any(
            marker in name
            for marker in ("credential", "access-token", "refresh-token", "api-key")
        ):
            return True
        if name.startswith(".") and name not in {".gitignore"}:
            return True
        if name.endswith(EXCLUDED_SUFFIXES):
            return True
    return False


def _allowed_snapshot_path(relative, *, is_directory):
    parts = relative.casefold().split("/")
    if not parts or not parts[0]:
        return False
    if parts[0] in ALLOWED_VAULT_ROOTS:
        return True
    if len(parts) == 1 and not is_directory:
        return Path(parts[0]).suffix in ALLOWED_ROOT_FILE_SUFFIXES
    return False


def _assert_no_embedded_credential(relative, data):
    if Path(relative).suffix.casefold() not in SECRET_SCANNED_SUFFIXES:
        return
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        return
    for pattern in HIGH_CONFIDENCE_SECRET_PATTERNS:
        if pattern.search(text):
            raise PublisherError(
                f"possible credential detected in Vault file: {relative}"
            )


def _content_class(relative):
    parts = relative.casefold().split("/")
    if (
        len(parts) >= 3
        and parts[:3] == ["05-agent-memory", "codex-profile", "skills"]
    ):
        return "skill-source"
    if (
        "05-agent-memory" in parts
        and "skills" in parts
        and relative.casefold().endswith((".md", ".json", ".yaml", ".yml"))
    ):
        return "skill-source"
    return "canonical-memory"


def _validate_snapshot_replica_path(relative):
    try:
        relative = validate_replica_path(relative)
    except ProtocolError as exc:
        raise MaterializeError(str(exc)) from exc
    if _excluded_path(relative, is_directory=False):
        raise MaterializeError(
            f"snapshot path is not allowed in a replica: {relative}"
        )
    return relative


def _portable_component_key(value):
    return unicodedata.normalize("NFC", str(value)).casefold()


def _portable_path_key(value):
    return tuple(
        _portable_component_key(part)
        for part in str(value).split("/")
    )


def _validate_path_tree(paths, error_type):
    root = {"children": {}, "terminal": ""}
    for relative in sorted(str(path) for path in paths):
        node = root
        components = relative.split("/")
        for index, component in enumerate(components):
            if node["terminal"]:
                raise error_type(
                    "snapshot path prefix conflict: "
                    f"{node['terminal']} and {relative}"
                )
            normalized = unicodedata.normalize("NFC", component)
            key = normalized.casefold()
            child = node["children"].get(key)
            if child is None:
                child = {
                    "children": {},
                    "terminal": "",
                    "component": normalized,
                }
                node["children"][key] = child
            elif (
                child["terminal"]
                and index < len(components) - 1
            ):
                raise error_type(
                    "snapshot path prefix conflict: "
                    f"{child['terminal']} and {relative}"
                )
            elif child["component"] != normalized:
                raise error_type(
                    f"snapshot contains a case path collision: {relative}"
                )
            node = child
        if node["terminal"]:
            raise error_type(
                f"snapshot contains a duplicate path: {relative}"
            )
        if node["children"]:
            descendant = _first_terminal_path(node)
            raise error_type(
                "snapshot path prefix conflict: "
                f"{relative} and {descendant}"
            )
        node["terminal"] = relative


def _first_terminal_path(node):
    if node["terminal"]:
        return node["terminal"]
    for key in sorted(node["children"]):
        found = _first_terminal_path(node["children"][key])
        if found:
            return found
    return ""


def _new_published_cleanup_report():
    return {
        "removed_generations": [],
        "removed_objects": [],
        "retained_generations": [],
        "deferred": [],
        "receipt_scan_pending": 0,
    }


def _new_replica_cleanup_report(active_generation):
    return {
        "removed_active_snapshots": [],
        "retained_active_generation": active_generation,
        "deferred": [],
    }


def _defer_cleanup(report, message):
    if message not in report["deferred"]:
        report["deferred"].append(message)


def _load_receipt_generation_references(published, state_dir, report):
    receipt_root = published / "v1" / "receipts"
    scan_state = _load_receipt_reference_scan_state(state_dir)
    references = {
        int(generation): set(generation_ids)
        for generation, generation_ids in scan_state["references"].items()
    }
    if not os.path.lexists(receipt_root):
        scan_state["cursor"] = ""
        scan_state["pass_incomplete"] = False
        _write_receipt_reference_scan_state(state_dir, scan_state)
        return references, True, 0
    root_info = os.lstat(receipt_root)
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        _defer_cleanup(
            report,
            "receipt references could not be scanned safely",
        )
        return references, False, 0

    scan_health = {"complete": True}
    limit = int(MAX_RECEIPT_REFERENCES_PER_PUBLISH_RUN)
    if limit <= 0:
        raise PublisherError("receipt reference scan limit is invalid")
    selected, next_cursor, cursor_missing = _receipt_continuation_page(
        _iter_receipt_reference_paths(receipt_root, scan_health),
        scan_state["cursor"],
        limit,
        receipt_root,
    )
    if cursor_missing:
        scan_state["pass_incomplete"] = True
    pending = int(bool(next_cursor) or cursor_missing)
    for path in selected:
        try:
            data = read_bounded_regular_file(
                path,
                max_bytes=MAX_RECEIPT_BYTES,
                root=published,
            )
            receipt = _decode_canonical(data, "receipt")
        except (
            MaterializeError,
            OSError,
            ProtocolError,
            ValueError,
            json.JSONDecodeError,
        ):
            scan_state["pass_incomplete"] = True
            continue
        if (
            not isinstance(receipt, dict)
            or set(receipt) != RECEIPT_FIELDS
            or receipt["protocol"] != PROTOCOL_RECEIPT
            or receipt["schema_version"] != 1
            or not _positive_int(receipt["canonical_generation"])
            or not _is_generation_id(receipt["generation_id"])
        ):
            scan_state["pass_incomplete"] = True
            continue
        references.setdefault(
            receipt["canonical_generation"],
            set(),
        ).add(receipt["generation_id"])
    complete = (
        scan_health["complete"]
        and not scan_state["pass_incomplete"]
        and pending == 0
    )
    scan_state["references"] = {
        str(generation): sorted(generation_ids)
        for generation, generation_ids in sorted(references.items())
    }
    if pending:
        scan_state["cursor"] = next_cursor
    else:
        scan_state["cursor"] = ""
        scan_state["pass_incomplete"] = False
    _write_receipt_reference_scan_state(state_dir, scan_state)
    report["receipt_scan_pending"] = pending
    if pending:
        _defer_cleanup(
            report,
            "receipt reference scan is bounded and will continue next run",
        )
    elif not complete:
        _defer_cleanup(
            report,
            "receipt references were incomplete; destructive cleanup was deferred",
        )
    return references, complete, pending


def _receipt_continuation_page(items, cursor, limit, receipt_root):
    selected = []
    started = not cursor
    for item in items:
        item_key = item.relative_to(receipt_root).as_posix()
        if not started:
            if item_key != cursor:
                continue
            started = True
        selected.append(item)
        if len(selected) > limit:
            break
    if cursor and not started:
        return [], "", True
    next_cursor = (
        selected[limit].relative_to(receipt_root).as_posix()
        if len(selected) > limit
        else ""
    )
    return selected[:limit], next_cursor, False


def _iter_receipt_reference_paths(receipt_root, scan_health):
    try:
        producers = os.scandir(receipt_root)
    except OSError:
        scan_health["complete"] = False
        return
    with producers:
        for producer in producers:
            try:
                producer_info = producer.stat(follow_symlinks=False)
            except OSError:
                scan_health["complete"] = False
                continue
            if (
                stat.S_ISLNK(producer_info.st_mode)
                or not stat.S_ISDIR(producer_info.st_mode)
            ):
                scan_health["complete"] = False
                continue
            try:
                receipts = os.scandir(producer.path)
            except OSError:
                scan_health["complete"] = False
                continue
            with receipts:
                for receipt in receipts:
                    try:
                        receipt_info = receipt.stat(follow_symlinks=False)
                    except OSError:
                        scan_health["complete"] = False
                        continue
                    if (
                        stat.S_ISLNK(receipt_info.st_mode)
                        or not stat.S_ISREG(receipt_info.st_mode)
                        or receipt_info.st_nlink != 1
                    ):
                        scan_health["complete"] = False
                        continue
                    yield Path(receipt.path)


def _load_receipt_reference_scan_state(state_dir):
    path = state_dir / "receipt-reference-scan.json"
    if not os.path.lexists(path):
        return {
            "protocol": RECEIPT_REFERENCE_SCAN_PROTOCOL,
            "schema_version": 1,
            "cursor": "",
            "references": {},
            "pass_incomplete": False,
        }
    try:
        data = read_bounded_regular_file(
            path,
            max_bytes=16 * 1024 * 1024,
            root=state_dir,
        )
        value = json.loads(data)
    except (
        OSError,
        ProtocolError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ) as exc:
        raise PublisherError("receipt reference scan state is invalid") from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "protocol",
            "schema_version",
            "cursor",
            "references",
            "pass_incomplete",
        }
        or value["protocol"] != RECEIPT_REFERENCE_SCAN_PROTOCOL
        or value["schema_version"] != 1
        or not isinstance(value["cursor"], str)
        or not isinstance(value["references"], dict)
        or not isinstance(value["pass_incomplete"], bool)
        or canonical_json_bytes(value) != data
    ):
        raise PublisherError("receipt reference scan state is invalid")
    for generation, generation_ids in value["references"].items():
        if (
            not str(generation).isdigit()
            or int(generation) <= 0
            or not isinstance(generation_ids, list)
            or generation_ids != sorted(set(generation_ids))
            or any(not _is_generation_id(item) for item in generation_ids)
        ):
            raise PublisherError("receipt reference scan state is invalid")
    return value


def _write_receipt_reference_scan_state(state_dir, value):
    portable_atomic_write(
        state_dir / "receipt-reference-scan.json",
        canonical_json_bytes(value),
        root=state_dir,
    )


def _generation_directories(published, report):
    snapshot_root = published / "v1" / "snapshots"
    if not os.path.lexists(snapshot_root):
        return {}
    try:
        root_info = os.lstat(snapshot_root)
    except OSError:
        _defer_cleanup(report, "snapshot generation root could not be inspected")
        return {}
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        _defer_cleanup(report, "snapshot generation root is unsafe")
        return {}

    generations = {}
    try:
        entries = sorted(os.scandir(snapshot_root), key=lambda item: item.name)
    except OSError:
        _defer_cleanup(report, "snapshot generation root could not be listed")
        return {}
    for entry in entries:
        if not re.fullmatch(r"[0-9]{20}", entry.name):
            _defer_cleanup(
                report,
                f"unknown snapshot entry retained: {entry.name}",
            )
            continue
        generation = int(entry.name)
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            _defer_cleanup(
                report,
                f"snapshot generation {generation} could not be inspected",
            )
            continue
        if (
            generation <= 0
            or stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
        ):
            _defer_cleanup(
                report,
                f"snapshot generation {generation} is unsafe",
            )
            continue
        generations[generation] = Path(entry.path)
    return generations


def _generation_seal_state(generation_dir):
    for name in ("snapshot.json", "complete.json"):
        path = generation_dir / name
        if not os.path.lexists(path):
            return False, "seal is incomplete"
        info = os.lstat(path)
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
        ):
            return False, f"{name} is unsafe"
    return True, ""


def _remove_unsealed_generation(generation_dir, published):
    info = os.lstat(generation_dir)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    entries = list(os.scandir(generation_dir))
    for entry in entries:
        entry_info = entry.stat(follow_symlinks=False)
        if (
            stat.S_ISLNK(entry_info.st_mode)
            or not stat.S_ISREG(entry_info.st_mode)
            or entry_info.st_nlink != 1
        ):
            return False
    for entry in entries:
        portable_unlink_regular(
            entry.path,
            root=published,
            expected_identity=entry.stat(follow_symlinks=False),
        )
    portable_rmdir_empty(
        generation_dir,
        root=published,
        expected_identity=info,
    )
    return True


def _recover_publication_head(
    published,
    current,
    previous,
    max_object_bytes,
    receipt_references,
    receipt_scan_complete,
    report,
):
    generations = _generation_directories(published, report)
    current_generation = current["generation"] if current else 0
    for generation, generation_dir in sorted(generations.items()):
        if generation <= current_generation:
            continue
        sealed, reason = _generation_seal_state(generation_dir)
        if sealed:
            continue
        if generation in receipt_references:
            _defer_cleanup(
                report,
                f"receipt retains unsealed generation {generation}",
            )
            continue
        if not receipt_scan_complete:
            _defer_cleanup(
                report,
                (
                    f"unsealed generation {generation} retained because "
                    "receipts are uncertain"
                ),
            )
            continue
        try:
            removed = _remove_unsealed_generation(generation_dir, published)
        except OSError:
            removed = False
        if removed:
            report["removed_generations"].append(generation)
        else:
            _defer_cleanup(
                report,
                f"unsealed generation {generation} retained: {reason}",
            )

    generations = _generation_directories(published, report)
    head = current
    head_snapshot = previous
    next_generation = current_generation + 1
    while next_generation in generations:
        generation_dir = generations[next_generation]
        sealed, reason = _generation_seal_state(generation_dir)
        if not sealed:
            raise PublisherError(
                f"generation {next_generation} blocks publication: {reason}"
            )
        try:
            candidate, candidate_bytes = _load_received_generation_number(
                published,
                next_generation,
                max_object_bytes,
            )
        except (
            MaterializeError,
            OSError,
            ProtocolError,
            ValueError,
            json.JSONDecodeError,
        ) as exc:
            _defer_cleanup(
                report,
                f"complete generation {next_generation} could not be verified",
            )
            raise PublisherError(
                f"complete orphan generation {next_generation} is invalid: {exc}"
            ) from exc
        expected_parent = 0 if head is None else head["generation"]
        expected_parent_id = "" if head is None else head["generation_id"]
        if (
            candidate["parent_generation"] != expected_parent
            or candidate["parent_generation_id"] != expected_parent_id
        ):
            raise PublisherError(
                f"complete orphan generation {next_generation} has a parent conflict"
            )
        _assert_apply_journal_materializable(
            head_snapshot,
            candidate,
            PublisherError,
        )
        referenced_ids = receipt_references.get(next_generation, set())
        if referenced_ids and referenced_ids != {candidate["generation_id"]}:
            raise PublisherError(
                f"receipt identity conflicts with generation {next_generation}"
            )
        head = _current_value(candidate, candidate_bytes)
        head_snapshot = candidate
        next_generation += 1

    for generation in sorted(generations):
        if generation >= next_generation:
            if generation in receipt_references:
                _defer_cleanup(
                    report,
                    f"receipt retains generation {generation}",
                )
            elif generation > (head["generation"] if head else 0):
                _defer_cleanup(
                    report,
                    (
                        f"non-contiguous generation {generation} retained "
                        "for manual review"
                    ),
                )
    report["retained_generations"] = sorted(generations)
    if head and any(
        generation < head["generation"] for generation in generations
    ):
        _defer_cleanup(
            report,
            (
                "published history retained because replica acknowledgement "
                "authority is unavailable"
            ),
        )
    report["removed_generations"] = sorted(
        set(report["removed_generations"])
    )
    return head, head_snapshot


def _current_value(snapshot, snapshot_bytes):
    return {
        "protocol": PROTOCOL_CURRENT,
        "schema_version": 1,
        "generation": snapshot["generation"],
        "generation_id": snapshot["generation_id"],
        "snapshot_sha256": sha256_bytes(snapshot_bytes),
    }


def _cleanup_published_storage(
    published,
    max_object_bytes,
    receipt_references,
    receipt_scan_complete,
    report,
):
    generations = _generation_directories(published, report)
    report["retained_generations"] = sorted(generations)
    if not receipt_scan_complete:
        return
    missing_receipt_generations = sorted(
        set(receipt_references) - set(generations)
    )
    if missing_receipt_generations:
        for generation in missing_receipt_generations:
            _defer_cleanup(
                report,
                (
                    f"receipt generation {generation} has no retained "
                    "manifest; objects were retained"
                ),
            )
        return
    referenced_objects = set()
    for generation in sorted(generations):
        try:
            snapshot, _snapshot_bytes = _load_received_generation_number(
                published,
                generation,
                max_object_bytes,
            )
        except (
            MaterializeError,
            OSError,
            ProtocolError,
            ValueError,
            json.JSONDecodeError,
        ):
            _defer_cleanup(
                report,
                (
                    f"objects retained because generation {generation} "
                    "is not fully verifiable"
                ),
            )
            return
        referenced_ids = receipt_references.get(generation, set())
        if referenced_ids and referenced_ids != {snapshot["generation_id"]}:
            _defer_cleanup(
                report,
                (
                    f"receipt generation {generation} has an identity "
                    "conflict; objects were retained"
                ),
            )
            return
        referenced_objects.update(item["sha256"] for item in snapshot["files"])

    object_root = published / "v1" / "objects"
    if not os.path.lexists(object_root):
        return
    try:
        root_info = os.lstat(object_root)
    except OSError:
        _defer_cleanup(report, "published object root could not be inspected")
        return
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        _defer_cleanup(report, "published object root is unsafe")
        return
    try:
        prefixes = sorted(os.scandir(object_root), key=lambda item: item.name)
    except OSError:
        _defer_cleanup(report, "published object root could not be listed")
        return
    for prefix in prefixes:
        try:
            prefix_info = prefix.stat(follow_symlinks=False)
        except OSError:
            _defer_cleanup(report, f"object prefix {prefix.name} is unreadable")
            continue
        if (
            not re.fullmatch(r"[0-9a-f]{2}", prefix.name)
            or stat.S_ISLNK(prefix_info.st_mode)
            or not stat.S_ISDIR(prefix_info.st_mode)
        ):
            _defer_cleanup(
                report,
                f"unknown object entry retained: {prefix.name}",
            )
            continue
        try:
            objects = sorted(
                os.scandir(prefix.path),
                key=lambda item: item.name,
            )
        except OSError:
            _defer_cleanup(
                report,
                f"object prefix {prefix.name} could not be listed",
            )
            continue
        for entry in objects:
            digest = entry.name
            if (
                not _is_sha256(digest)
                or digest[:2] != prefix.name
                or digest in referenced_objects
            ):
                if not _is_sha256(digest) or digest[:2] != prefix.name:
                    _defer_cleanup(
                        report,
                        f"unknown object entry retained: {prefix.name}/{digest}",
                    )
                continue
            path = Path(entry.path)
            try:
                before = os.lstat(path)
                data = read_bounded_regular_file(
                    path,
                    max_bytes=max_object_bytes,
                    root=published,
                )
                after = os.lstat(path)
            except (OSError, ProtocolError):
                _defer_cleanup(
                    report,
                    f"unreferenced object {digest} could not be verified",
                )
                continue
            if (
                _stat_identity(after) != _stat_identity(before)
                or sha256_bytes(data) != digest
            ):
                _defer_cleanup(
                    report,
                    f"unreferenced object {digest} has an invalid hash",
                )
                continue
            try:
                portable_unlink_regular(
                    path,
                    root=published,
                    expected_identity=after,
                )
            except (OSError, ProtocolError):
                _defer_cleanup(
                    report,
                    f"unreferenced object {digest} could not be removed",
                )
                continue
            report["removed_objects"].append(digest)
        try:
            portable_rmdir_empty(
                prefix.path,
                root=published,
                expected_identity=prefix_info,
            )
        except (OSError, ProtocolError):
            pass
    report["removed_objects"] = sorted(set(report["removed_objects"]))


def _cleanup_inactive_active_snapshots(replica_state, active_generation):
    report = _new_replica_cleanup_report(active_generation)
    journal_path = replica_state / "apply-journal.json"
    if os.path.lexists(journal_path):
        _defer_cleanup(
            report,
            "active snapshots retained while an apply or rollback journal exists",
        )
        return report
    active_root = replica_state / "active"
    if not os.path.lexists(active_root):
        return report
    info = os.lstat(active_root)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        _defer_cleanup(report, "active snapshot root is unsafe")
        return report
    try:
        entries = sorted(os.scandir(active_root), key=lambda item: item.name)
    except OSError:
        _defer_cleanup(report, "active snapshot root could not be listed")
        return report
    expected_name = f"{active_generation:020d}.json"
    for entry in entries:
        if entry.name == expected_name:
            continue
        match = re.fullmatch(r"([0-9]{20})\.json", entry.name)
        try:
            entry_info = entry.stat(follow_symlinks=False)
        except OSError:
            entry_info = None
        if (
            match is None
            or entry_info is None
            or stat.S_ISLNK(entry_info.st_mode)
            or not stat.S_ISREG(entry_info.st_mode)
            or entry_info.st_nlink != 1
        ):
            _defer_cleanup(
                report,
                f"unknown active snapshot entry retained: {entry.name}",
            )
            continue
        try:
            portable_unlink_regular(
                entry.path,
                root=replica_state,
                expected_identity=entry_info,
            )
        except (OSError, ProtocolError):
            _defer_cleanup(
                report,
                f"inactive snapshot {entry.name} could not be removed",
            )
            continue
        report["removed_active_snapshots"].append(int(match.group(1)))
    report["removed_active_snapshots"].sort()
    return report


def _load_published_current(published, max_object_bytes):
    path = published / "v1" / "current.json"
    if not path.exists():
        return None, None
    current = _load_json(
        path,
        max_bytes=_metadata_read_limit(max_object_bytes, 16 * 1024),
        root=published,
        name="current",
    )
    _validate_current(current)
    snapshot, _bytes = _load_received_generation(
        published,
        current,
        max_object_bytes,
    )
    return current, snapshot


def _load_received_generation(root, current, max_object_bytes):
    return _load_received_generation_number(
        root,
        int(current["generation"]),
        max_object_bytes,
        expected=current,
    )


def _load_received_generation_number(
    root,
    generation,
    max_object_bytes,
    *,
    expected=None,
):
    generation_dir = (
        root / "v1" / "snapshots" / f"{int(generation):020d}"
    )
    snapshot_bytes = read_bounded_regular_file(
        generation_dir / "snapshot.json",
        max_bytes=max_object_bytes,
        root=root,
    )
    snapshot_sha256 = sha256_bytes(snapshot_bytes)
    snapshot = _decode_canonical(snapshot_bytes, "snapshot")
    _validate_snapshot(snapshot)
    complete = _load_json(
        generation_dir / "complete.json",
        max_bytes=_metadata_read_limit(max_object_bytes, 16 * 1024),
        root=root,
        name="complete",
    )
    _validate_complete(complete)
    if expected is not None:
        for key in ("generation", "generation_id", "snapshot_sha256"):
            if complete[key] != expected[key]:
                raise MaterializeError("complete marker does not match current")
    if (
        snapshot["generation"] != int(generation)
        or snapshot["generation"] != complete["generation"]
        or snapshot["generation_id"] != complete["generation_id"]
        or snapshot_sha256 != complete["snapshot_sha256"]
    ):
        raise MaterializeError("snapshot identity does not match complete marker")
    if complete["file_count"] != len(snapshot["files"]):
        raise MaterializeError("complete file count does not match snapshot")
    if complete["object_bytes"] != sum(item["bytes"] for item in snapshot["files"]):
        raise MaterializeError("complete object bytes do not match snapshot")
    if complete["file_count"] > MAX_REPLICA_FILE_COUNT:
        raise MaterializeError("snapshot file count exceeds protocol limit")
    if complete["object_bytes"] > MAX_REPLICA_TOTAL_BYTES:
        raise MaterializeError("snapshot total bytes exceed protocol limit")
    for item in snapshot["files"]:
        if item["bytes"] > max_object_bytes:
            raise MaterializeError(
                f"snapshot object exceeds size limit: {item['path']}"
            )
        object_path = root / "v1" / "objects" / item["sha256"][:2] / item["sha256"]
        try:
            data = read_bounded_regular_file(
                object_path,
                max_bytes=max_object_bytes,
                root=root,
            )
        except FileNotFoundError as exc:
            raise MaterializeError(f"snapshot object is missing: {item['path']}") from exc
        if len(data) != item["bytes"] or sha256_bytes(data) != item["sha256"]:
            raise MaterializeError(f"snapshot object is corrupt: {item['path']}")
    return snapshot, snapshot_bytes


def _apply_journal_size_upper_bound(previous, snapshot):
    previous_by_path = {
        item["path"]: item for item in (previous or {}).get("files", [])
    }
    target_by_path = {item["path"]: item for item in snapshot["files"]}
    changed = [
        path
        for path, item in target_by_path.items()
        if path not in previous_by_path
        or previous_by_path[path]["sha256"] != item["sha256"]
    ]
    deleted = sorted(set(previous_by_path) - set(target_by_path))
    touched = list(deleted) + sorted(changed)
    journal = {
        "protocol": JOURNAL_PROTOCOL,
        "schema_version": 1,
        "target_generation": snapshot["generation"],
        "target_generation_id": snapshot["generation_id"],
        "target_snapshot_sha256": "f" * 64,
        "operations": [],
    }
    total = len(canonical_json_bytes(journal))
    numeric_identity_size_sentinel = -(1 << 63)
    file_identity_size_sentinel = "u128:" + ("f" * 32)
    operation_count = 0
    for relative in touched:
        previous_item = previous_by_path.get(relative)
        target_item = target_by_path.get(relative)
        action = "write" if target_item is not None else "delete"
        operation = {
            "path": relative,
            "action": action,
            "target_bytes": (
                int(target_item["bytes"])
                if target_item is not None
                else 0
            ),
            "target_sha256": (
                target_item["sha256"]
                if target_item is not None
                else ""
            ),
            "before_identity": [],
            "existed": False,
            "backup": "",
            "mode": 0,
            "bytes": 0,
            "sha256": "",
        }
        if previous_item is not None:
            operation.update(
                {
                    "before_identity": [
                        file_identity_size_sentinel,
                        file_identity_size_sentinel,
                        numeric_identity_size_sentinel,
                        numeric_identity_size_sentinel,
                        numeric_identity_size_sentinel,
                        numeric_identity_size_sentinel,
                    ],
                    "existed": True,
                    "backup": (
                        f"rollback/{snapshot['generation_id']}/{relative}"
                    ),
                    "mode": 0o7777,
                    "bytes": int(previous_item["bytes"]),
                    "sha256": previous_item["sha256"],
                }
            )
        total += len(canonical_json_bytes(operation))
        if operation_count:
            total += 1
        operation_count += 1
    return total


def _assert_apply_journal_materializable(previous, snapshot, error_type):
    if len(snapshot["files"]) > MAX_REPLICA_FILE_COUNT:
        raise error_type("snapshot file count exceeds protocol limit")
    if sum(item["bytes"] for item in snapshot["files"]) > MAX_REPLICA_TOTAL_BYTES:
        raise error_type("snapshot total bytes exceed protocol limit")
    if _apply_journal_size_upper_bound(previous, snapshot) > MAX_APPLY_JOURNAL_BYTES:
        raise error_type("apply journal exceeds the recovery size limit")


def _stage_generation(
    received,
    replica_state,
    previous,
    snapshot,
    max_object_bytes,
):
    staging = replica_state / "staging" / snapshot["generation_id"]
    previous_by_path = {
        item["path"]: item for item in (previous or {}).get("files", [])
    }
    changed_by_digest = {}
    for item in snapshot["files"]:
        prior = previous_by_path.get(item["path"])
        if prior is None or prior["sha256"] != item["sha256"]:
            changed_by_digest.setdefault(item["sha256"], item)
    changed = list(changed_by_digest.values())
    if len(changed) > MAX_REPLICA_FILE_COUNT:
        raise MaterializeError("staging file count exceeds protocol limit")
    if sum(item["bytes"] for item in changed) > MAX_REPLICA_TOTAL_BYTES:
        raise MaterializeError("staging total bytes exceed protocol limit")
    try:
        portable_ensure_directory_tree(staging, root=replica_state)
        for item in changed:
            source = (
                received
                / "v1"
                / "objects"
                / item["sha256"][:2]
                / item["sha256"]
            )
            data = read_bounded_regular_file(
                source,
                max_bytes=max_object_bytes,
                root=received,
            )
            if (
                len(data) != item["bytes"]
                or sha256_bytes(data) != item["sha256"]
            ):
                raise MaterializeError(
                    f"staging object is corrupt: {item['path']}"
                )
            write_immutable(
                staging / item["sha256"][:2] / item["sha256"],
                data,
                root=replica_state,
            )
    except Exception:
        _discard_managed_tree(staging, replica_state)
        raise
    return staging


def _assert_bootstrap_replica_safe(replica):
    try:
        entries = list(os.scandir(replica))
    except OSError as exc:
        raise MaterializeError("replica bootstrap directory cannot be inspected") from exc
    for entry in entries:
        if entry.name != ".obsidian":
            raise MaterializeError(
                "replica bootstrap directory is not empty; "
                f"unexpected entry: {entry.name}"
            )
        info = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise MaterializeError(
                "replica bootstrap directory is not empty; "
                ".obsidian must be a local directory"
            )


def _verify_replica_drift(
    replica,
    previous,
    target,
    max_object_bytes,
):
    previous_by_path = {
        item["path"]: item for item in (previous or {}).get("files", [])
    }
    target_paths = {item["path"] for item in target["files"]}
    for relative, item in previous_by_path.items():
        path = replica / relative
        if not os.path.lexists(path):
            raise MaterializeError(f"managed replica drift: missing {relative}")
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise MaterializeError(f"managed replica drift: invalid {relative}")
        if stat.S_IMODE(info.st_mode) & (
            stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
        ):
            raise MaterializeError(f"managed replica drift: writable {relative}")
        if info.st_size != item["bytes"]:
            raise MaterializeError(f"managed replica drift: changed {relative}")
        if item["bytes"] > max_object_bytes:
            raise MaterializeError(
                f"managed replica exceeds size limit: {relative}"
            )
        data = read_bounded_regular_file(
            path,
            max_bytes=max_object_bytes,
            root=replica,
        )
        if len(data) != item["bytes"] or sha256_bytes(data) != item["sha256"]:
            raise MaterializeError(f"managed replica drift: changed {relative}")
    for item in target["files"]:
        relative = item["path"]
        if relative not in previous_by_path and os.path.lexists(replica / relative):
            if not _expected_previous_directory(
                replica,
                relative,
                previous_by_path,
            ):
                raise MaterializeError(
                    f"managed replica drift: collision at {relative}"
                )
    for relative in previous_by_path:
        if relative not in target_paths:
            validate_replica_path(relative)


def _read_expected_replica_file(
    replica,
    relative,
    expected,
    max_object_bytes,
):
    path = replica / relative
    if not os.path.lexists(path):
        raise MaterializeError(f"managed replica drift: missing {relative}")
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise MaterializeError(f"managed replica drift: invalid {relative}")
    if stat.S_IMODE(info.st_mode) & (
        stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    ):
        raise MaterializeError(f"managed replica drift: writable {relative}")
    if info.st_size != expected["bytes"]:
        raise MaterializeError(f"managed replica drift: changed {relative}")
    data = read_bounded_regular_file(
        path,
        max_bytes=max_object_bytes,
        root=replica,
    )
    after = os.lstat(path)
    if (
        _stat_identity(after) != _stat_identity(info)
        or len(data) != expected["bytes"]
        or sha256_bytes(data) != expected["sha256"]
    ):
        raise MaterializeError(f"managed replica drift: changed {relative}")
    return after, data


def _assert_apply_precondition(replica, operation, max_object_bytes):
    relative = operation["path"]
    destination = replica / relative
    if operation["existed"] is False:
        if os.path.lexists(destination):
            raise MaterializeError(
                f"managed replica drift before apply: collision at {relative}"
            )
        return
    if not os.path.lexists(destination):
        raise MaterializeError(
            f"managed replica drift before apply: missing {relative}"
        )
    info = os.lstat(destination)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or _stat_identity(info) != operation["before_identity"]
    ):
        raise MaterializeError(
            f"managed replica drift before apply: changed {relative}"
        )
    data = read_bounded_regular_file(
        destination,
        max_bytes=max_object_bytes,
        root=replica,
    )
    after = os.lstat(destination)
    if (
        _stat_identity(after) != operation["before_identity"]
        or len(data) != operation["bytes"]
        or sha256_bytes(data) != operation["sha256"]
    ):
        raise MaterializeError(
            f"managed replica drift before apply: changed {relative}"
        )


def _stat_identity(info):
    return [
        _portable_file_identity_component(info.st_dev),
        _portable_file_identity_component(info.st_ino),
        int(info.st_mode),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    ]


def _portable_file_identity_component(value):
    value = int(value)
    if 0 <= value <= SIGNED_IDENTITY_MAX:
        return value
    if 0 <= value <= UNSIGNED_IDENTITY_MAX:
        return f"u128:{value:032x}"
    raise MaterializeError("file identity is outside the supported range")


def _valid_journal_identity(value):
    if not isinstance(value, list) or len(value) != 6:
        return False
    for component in value[:2]:
        if (
            isinstance(component, int)
            and not isinstance(component, bool)
            and 0 <= component <= SIGNED_IDENTITY_MAX
        ):
            continue
        if isinstance(component, str) and UNSIGNED_IDENTITY_RE.fullmatch(component):
            continue
        return False
    return all(
        isinstance(component, int)
        and not isinstance(component, bool)
        and 0 <= component <= SIGNED_IDENTITY_MAX
        for component in value[2:]
    )


def _expected_previous_directory(replica, relative, previous_by_path):
    path = replica / relative
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        return False
    prefix = relative.rstrip("/") + "/"
    return any(path.startswith(prefix) for path in previous_by_path)


def _build_apply_operations(
    replica,
    replica_state,
    previous_by_path,
    target_by_path,
    touched,
    backup_root,
    max_object_bytes,
):
    operations = []
    for relative in touched:
        destination = replica / relative
        previous_item = previous_by_path.get(relative)
        target_item = target_by_path.get(relative)
        action = "write" if target_item is not None else "delete"
        target_bytes = int(target_item["bytes"]) if target_item is not None else 0
        target_sha256 = target_item["sha256"] if target_item is not None else ""
        if previous_item is not None:
            info, data = _read_expected_replica_file(
                replica,
                relative,
                previous_item,
                max_object_bytes,
            )
            backup_path = backup_root / relative
            portable_atomic_write(backup_path, data, root=replica_state)
            operations.append(
                {
                    "path": relative,
                    "action": action,
                    "target_bytes": target_bytes,
                    "target_sha256": target_sha256,
                    "before_identity": _stat_identity(info),
                    "existed": True,
                    "backup": backup_path.relative_to(replica_state).as_posix(),
                    "mode": stat.S_IMODE(info.st_mode),
                    "bytes": int(info.st_size),
                    "sha256": sha256_bytes(data),
                }
            )
        else:
            if (
                os.path.lexists(destination)
                and not _expected_previous_directory(
                    replica,
                    relative,
                    previous_by_path,
                )
            ):
                raise MaterializeError(
                    f"managed replica drift: collision at {relative}"
                )
            operations.append(
                {
                    "path": relative,
                    "action": action,
                    "target_bytes": target_bytes,
                    "target_sha256": target_sha256,
                    "before_identity": [],
                    "existed": False,
                    "backup": "",
                    "mode": 0,
                    "bytes": 0,
                    "sha256": "",
                }
            )
    return operations


def _apply_generation(
    replica,
    replica_state,
    snapshot,
    snapshot_bytes,
    previous,
    staging,
    max_object_bytes,
    *,
    fault_point,
):
    previous_by_path = {
        item["path"]: item for item in (previous or {}).get("files", [])
    }
    target_by_path = {item["path"]: item for item in snapshot["files"]}
    changed = [
        path
        for path, item in target_by_path.items()
        if path not in previous_by_path
        or previous_by_path[path]["sha256"] != item["sha256"]
    ]
    deleted = sorted(set(previous_by_path) - set(target_by_path))
    touched = list(deleted) + sorted(changed)
    transaction = snapshot["generation_id"]
    backup_root = replica_state / "rollback" / transaction
    journal_path = replica_state / "apply-journal.json"
    try:
        operations = _build_apply_operations(
            replica,
            replica_state,
            previous_by_path,
            target_by_path,
            touched,
            backup_root,
            max_object_bytes,
        )
        journal = {
            "protocol": JOURNAL_PROTOCOL,
            "schema_version": 1,
            "target_generation": snapshot["generation"],
            "target_generation_id": snapshot["generation_id"],
            "target_snapshot_sha256": sha256_bytes(snapshot_bytes),
            "operations": operations,
        }
        journal_bytes = canonical_json_bytes(journal)
        if len(journal_bytes) > MAX_APPLY_JOURNAL_BYTES:
            raise MaterializeError(
                "apply journal exceeds the recovery size limit"
            )
        portable_atomic_write(
            journal_path,
            journal_bytes,
            root=replica_state,
        )
    except Exception:
        if not os.path.lexists(journal_path):
            _discard_managed_tree(staging, replica_state)
            _discard_managed_tree(backup_root, replica_state)
        raise
    operations_by_path = {item["path"]: item for item in operations}
    applied = 0
    try:
        for relative in deleted:
            _assert_apply_precondition(
                replica,
                operations_by_path[relative],
                max_object_bytes,
            )
            destination = replica / relative
            _unlink_regular(
                destination,
                replica,
                expected_identity=operations_by_path[relative][
                    "before_identity"
                ],
            )
            _prune_empty_parent_directories(destination.parent, replica)
            applied += 1
            if fault_point == "after_first_apply" and applied == 1:
                raise MaterializeError("injected apply failure")
        for relative in sorted(changed):
            item = target_by_path[relative]
            source = staging / item["sha256"][:2] / item["sha256"]
            data = read_bounded_regular_file(
                source,
                max_bytes=max_object_bytes,
                root=replica_state,
            )
            if len(data) != item["bytes"] or sha256_bytes(data) != item["sha256"]:
                raise MaterializeError(
                    f"staging object is corrupt: {item['path']}"
                )
            destination = replica / relative
            _assert_apply_precondition(
                replica,
                operations_by_path[relative],
                max_object_bytes,
            )
            portable_atomic_write(destination, data, root=replica, mode=0o444)
            applied += 1
            if fault_point == "after_first_apply" and applied == 1:
                raise MaterializeError("injected apply failure")
        portable_rmtree(staging, root=replica_state)
        _verify_applied_generation(
            replica,
            replica_state,
            snapshot,
            journal,
            max_object_bytes,
        )
        active_snapshot = (
            replica_state
            / "active"
            / f"{snapshot['generation']:020d}.json"
        )
        write_immutable(
            active_snapshot,
            snapshot_bytes,
            root=replica_state,
        )
        active = {
            "protocol": ACTIVE_PROTOCOL,
            "schema_version": 1,
            "generation": snapshot["generation"],
            "generation_id": snapshot["generation_id"],
            "snapshot_sha256": sha256_bytes(snapshot_bytes),
        }
        portable_atomic_write(
            replica_state / "active-generation.json",
            canonical_json_bytes(active),
            root=replica_state,
        )
        _verify_applied_generation(
            replica,
            replica_state,
            snapshot,
            journal,
            max_object_bytes,
        )
    except Exception:
        if _active_marker_matches_target(
            replica_state,
            snapshot,
            snapshot_bytes,
            max_object_bytes,
        ):
            raise
        _rollback_journal(replica, replica_state, journal)
        _remove_if_regular(journal_path, replica_state)
        _discard_managed_tree(backup_root, replica_state)
        raise
    _remove_if_regular(journal_path, replica_state)
    _discard_managed_tree(backup_root, replica_state)


def _recover_interrupted_apply(
    replica,
    replica_state,
    max_object_bytes,
):
    journal_path = replica_state / "apply-journal.json"
    if not journal_path.exists():
        return
    journal = _load_json(
        journal_path,
        max_bytes=MAX_APPLY_JOURNAL_BYTES,
        root=replica_state,
        name="apply journal",
    )
    _validate_apply_journal(journal)
    active = _load_active_marker(replica_state, max_object_bytes)
    backup_root = (
        replica_state
        / "rollback"
        / journal["target_generation_id"]
    )
    if active and (
        active["generation"] == journal["target_generation"]
        and active["generation_id"] == journal["target_generation_id"]
        and active["snapshot_sha256"] == journal["target_snapshot_sha256"]
    ):
        _load_active_snapshot(replica_state, active, max_object_bytes)
        _verify_committed_journal_targets(replica, replica_state, journal)
        _remove_if_regular(journal_path, replica_state)
        _discard_managed_tree(backup_root, replica_state)
        return
    if active and active["generation"] >= journal["target_generation"]:
        raise MaterializeError(
            "active generation conflicts with interrupted apply journal"
        )
    _rollback_journal(replica, replica_state, journal)
    _remove_if_regular(journal_path, replica_state)
    _discard_managed_tree(backup_root, replica_state)


def _cleanup_stale_rollback(replica_state):
    journal_path = replica_state / "apply-journal.json"
    preserved_generation_id = None
    if os.path.lexists(journal_path):
        journal = _load_json(
            journal_path,
            max_bytes=MAX_APPLY_JOURNAL_BYTES,
            root=replica_state,
            name="apply journal",
        )
        _validate_apply_journal(journal)
        preserved_generation_id = journal["target_generation_id"]

    rollback_root = replica_state / "rollback"
    if not os.path.lexists(rollback_root):
        return
    info = os.lstat(rollback_root)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise MaterializeError("replica rollback root is unsafe")
    with os.scandir(rollback_root) as iterator:
        entries = list(iterator)
    if len(entries) > MAX_STALE_ROLLBACK_GENERATIONS:
        raise MaterializeError(
            "stale rollback generation count exceeds recovery limit"
        )
    for entry in sorted(entries, key=lambda candidate: candidate.name):
        if not _is_generation_id(entry.name):
            raise MaterializeError("replica rollback entry is invalid")
        entry_info = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(entry_info.st_mode) or not stat.S_ISDIR(entry_info.st_mode):
            raise MaterializeError("replica rollback entry is unsafe")
        if entry.name != preserved_generation_id:
            _discard_managed_tree(Path(entry.path), replica_state)
    if os.path.lexists(rollback_root) and not any(rollback_root.iterdir()):
        portable_rmdir_empty(
            rollback_root,
            root=replica_state,
            expected_identity=os.lstat(rollback_root),
        )


def _cleanup_stale_staging(replica_state):
    staging_root = replica_state / "staging"
    if not os.path.lexists(staging_root):
        return 0
    info = os.lstat(staging_root)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise MaterializeError("replica staging root is unsafe")
    entries = []
    with os.scandir(staging_root) as iterator:
        for entry in iterator:
            entries.append(entry)
            if len(entries) > MAX_STALE_STAGING_GENERATIONS:
                raise MaterializeError(
                    "stale staging generation count exceeds recovery limit"
                )
    removed = 0
    for entry in sorted(entries, key=lambda candidate: candidate.name):
        if not re.fullmatch(r"generation-[0-9a-f]{64}", entry.name):
            raise MaterializeError("replica staging entry is invalid")
        entry_info = entry.stat(follow_symlinks=False)
        if stat.S_ISLNK(entry_info.st_mode) or not stat.S_ISDIR(
            entry_info.st_mode
        ):
            raise MaterializeError("replica staging entry is unsafe")
        portable_rmtree(entry.path, root=replica_state)
        removed += 1
    if os.path.lexists(staging_root) and not any(staging_root.iterdir()):
        portable_rmdir_empty(
            staging_root,
            root=replica_state,
            expected_identity=os.lstat(staging_root),
        )
    return removed


def _discard_managed_tree(path, root):
    try:
        return portable_rmtree(path, root=root)
    except (OSError, ProtocolError, ValueError):
        return False


def _active_marker_matches_target(
    replica_state,
    snapshot,
    snapshot_bytes,
    max_object_bytes,
):
    try:
        active = _load_active_marker(replica_state, max_object_bytes)
        if active is None:
            return False
        active_snapshot = _load_active_snapshot(
            replica_state,
            active,
            max_object_bytes,
        )
    except (MaterializeError, OSError, ProtocolError, ValueError, json.JSONDecodeError):
        return False
    return (
        active["generation"] == snapshot["generation"]
        and active["generation_id"] == snapshot["generation_id"]
        and active["snapshot_sha256"] == sha256_bytes(snapshot_bytes)
        and active_snapshot == snapshot
    )


def _rollback_journal(replica, replica_state, journal):
    prepared = _prepare_rollback_operations(replica_state, journal)
    original_directories = _rollback_original_directory_paths(prepared)
    for operation, relative, data in prepared:
        destination = replica / relative
        if relative in original_directories and _is_real_directory(destination):
            destination_state = "original"
        else:
            destination_state = _classify_rollback_destination(
                replica,
                operation,
                relative,
            )
        if destination_state == "original":
            continue
        if destination_state != "target":
            raise MaterializeError("rollback destination drift is invalid")
        if operation["existed"] is True:
            portable_atomic_write(
                destination,
                data,
                root=replica,
                mode=int(operation.get("mode", 0o444)),
            )
        elif operation["existed"] is False:
            if os.path.lexists(destination):
                _unlink_regular(destination, replica)
                _prune_empty_parent_directories(
                    destination.parent,
                    replica,
                )
        else:
            raise MaterializeError("rollback journal operation is invalid")


def _rollback_original_directory_paths(prepared):
    deleted_originals = [
        relative
        for operation, relative, _data in prepared
        if operation["existed"] is True and operation["action"] == "delete"
    ]
    return {
        relative
        for operation, relative, _data in prepared
        if (
            operation["existed"] is False
            and operation["action"] == "write"
            and any(
                descendant.startswith(relative.rstrip("/") + "/")
                for descendant in deleted_originals
            )
        )
    }


def _is_real_directory(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    return not stat.S_ISLNK(info.st_mode) and stat.S_ISDIR(info.st_mode)


def _verify_committed_journal_targets(replica, replica_state, journal):
    prepared = _prepare_rollback_operations(replica_state, journal)
    target_writes = {
        operation["path"]
        for operation, _relative, _data in prepared
        if operation["action"] == "write"
    }
    for operation, relative, _data in prepared:
        destination = replica / relative
        prefix = relative.rstrip("/") + "/"
        if (
            operation["action"] == "delete"
            and any(path.startswith(prefix) for path in target_writes)
            and os.path.lexists(destination)
        ):
            info = os.lstat(destination)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise MaterializeError(
                    f"committed replica drift: target state changed at {relative}"
                )
            continue
        try:
            state = _classify_rollback_destination(
                replica,
                operation,
                relative,
            )
        except MaterializeError as exc:
            raise MaterializeError(
                f"committed replica drift: target state changed at {relative}"
            ) from exc
        if state != "target":
            raise MaterializeError(
                f"committed replica drift: target state changed at {relative}"
            )


def _verify_applied_generation(
    replica,
    replica_state,
    snapshot,
    journal,
    max_object_bytes,
):
    _verify_committed_journal_targets(replica, replica_state, journal)
    try:
        _verify_replica_drift(
            replica,
            snapshot,
            snapshot,
            max_object_bytes,
        )
    except MaterializeError as exc:
        raise MaterializeError(f"committed replica drift: {exc}") from exc


def _prune_empty_parent_directories(path, root):
    root = Path(os.path.abspath(os.path.expanduser(os.fspath(root))))
    current = Path(path)
    while current != root:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            current = current.parent
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return
        try:
            removed = portable_rmdir_empty(
                current,
                root=root,
                expected_identity=info,
            )
        except (OSError, ProtocolError, ValueError):
            return
        if not removed:
            return
        current = current.parent


def _validate_apply_journal(journal):
    if (
        not isinstance(journal, dict)
        or set(journal) != JOURNAL_FIELDS
        or journal["protocol"] != JOURNAL_PROTOCOL
        or journal["schema_version"] != 1
        or not _positive_int(journal["target_generation"])
        or not _is_generation_id(journal["target_generation_id"])
        or not _is_sha256(journal["target_snapshot_sha256"])
        or not isinstance(journal["operations"], list)
    ):
        raise MaterializeError("interrupted apply journal is invalid")


def _prepare_rollback_operations(replica_state, journal):
    operations = journal.get("operations")
    if not isinstance(operations, list):
        raise MaterializeError("rollback journal operations are invalid")
    prepared = []
    seen_paths = set()
    seen_backups = set()
    for operation in reversed(operations):
        if (
            not isinstance(operation, dict)
            or set(operation) != JOURNAL_OPERATION_FIELDS
        ):
            raise MaterializeError("rollback journal operation is invalid")
        relative = validate_replica_path(operation["path"])
        folded = relative.casefold()
        if folded in seen_paths:
            raise MaterializeError("rollback journal path is duplicated")
        seen_paths.add(folded)
        mode = operation["mode"]
        backup_bytes = operation["bytes"]
        target_bytes = operation["target_bytes"]
        action = operation["action"]
        before_identity = operation["before_identity"]
        if (
            isinstance(mode, bool)
            or not isinstance(mode, int)
            or mode < 0
            or mode > 0o7777
            or isinstance(backup_bytes, bool)
            or not isinstance(backup_bytes, int)
            or backup_bytes < 0
            or isinstance(target_bytes, bool)
            or not isinstance(target_bytes, int)
            or target_bytes < 0
            or (
                before_identity != []
                and not _valid_journal_identity(before_identity)
            )
        ):
            raise MaterializeError("rollback journal metadata is invalid")
        if action == "write":
            if not _is_sha256(operation["target_sha256"]):
                raise MaterializeError("rollback target hash is invalid")
        elif action == "delete":
            if target_bytes != 0 or operation["target_sha256"] != "":
                raise MaterializeError("rollback delete target is invalid")
        else:
            raise MaterializeError("rollback action is invalid")
        if operation["existed"] is True:
            if not _valid_journal_identity(before_identity):
                raise MaterializeError("rollback before identity is invalid")
            backup_text = validate_replica_path(operation["backup"])
            if not backup_text.startswith("rollback/"):
                raise MaterializeError("rollback journal backup is invalid")
            if backup_text.casefold() in seen_backups:
                raise MaterializeError("rollback journal backup is duplicated")
            seen_backups.add(backup_text.casefold())
            expected_hash = operation["sha256"]
            if not _is_sha256(expected_hash):
                raise MaterializeError("rollback journal backup hash is invalid")
            backup = replica_state / backup_text
            data = read_bounded_regular_file(
                backup,
                max_bytes=max(backup_bytes, 1),
                root=replica_state,
            )
            if len(data) != backup_bytes:
                raise MaterializeError("rollback journal backup size changed")
            if sha256_bytes(data) != expected_hash:
                raise MaterializeError("rollback journal backup hash changed")
        elif operation["existed"] is False:
            if (
                operation["backup"] != ""
                or mode != 0
                or backup_bytes != 0
                or operation["sha256"] != ""
                or action != "write"
                or before_identity != []
            ):
                raise MaterializeError("rollback journal new-file metadata is invalid")
            data = None
        else:
            raise MaterializeError("rollback journal operation is invalid")
        prepared.append((operation, relative, data))

    return prepared


def _classify_rollback_destination(replica, operation, relative):
    destination = replica / relative
    if not os.path.lexists(destination):
        if operation["existed"] is False:
            return "original"
        if operation["action"] == "delete":
            return "target"
        raise MaterializeError(f"rollback destination drift: missing {relative}")

    info = os.lstat(destination)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise MaterializeError(f"rollback destination drift: unsafe {relative}")
    original_size = operation["bytes"] if operation["existed"] is True else None
    target_size = (
        operation["target_bytes"] if operation["action"] == "write" else None
    )
    possible_sizes = {size for size in (original_size, target_size) if size is not None}
    if info.st_size not in possible_sizes:
        raise MaterializeError(f"rollback destination drift: changed {relative}")
    data = read_bounded_regular_file(
        destination,
        max_bytes=max(possible_sizes | {1}),
        root=replica,
    )
    digest = sha256_bytes(data)
    if (
        operation["existed"] is True
        and len(data) == operation["bytes"]
        and digest == operation["sha256"]
    ):
        return "original"
    if (
        operation["action"] == "write"
        and len(data) == operation["target_bytes"]
        and digest == operation["target_sha256"]
    ):
        return "target"
    raise MaterializeError(f"rollback destination drift: changed {relative}")


def _load_active_marker(replica_state, max_object_bytes):
    path = replica_state / "active-generation.json"
    if not path.exists():
        return None
    value = _load_json(
        path,
        max_bytes=_metadata_read_limit(max_object_bytes, 16 * 1024),
        root=replica_state,
        name="active generation",
    )
    required = {
        "protocol",
        "schema_version",
        "generation",
        "generation_id",
        "snapshot_sha256",
    }
    if (
        not isinstance(value, dict)
        or set(value) != required
        or value["protocol"] != ACTIVE_PROTOCOL
        or value["schema_version"] != 1
        or not _positive_int(value["generation"])
        or not _is_generation_id(value["generation_id"])
        or not _is_sha256(value["snapshot_sha256"])
    ):
        raise MaterializeError("active generation marker is invalid")
    return value


def _load_active_snapshot(
    replica_state,
    active,
    max_object_bytes,
):
    path = replica_state / "active" / f"{active['generation']:020d}.json"
    data = read_bounded_regular_file(
        path,
        max_bytes=max_object_bytes,
        root=replica_state,
    )
    if sha256_bytes(data) != active["snapshot_sha256"]:
        raise MaterializeError("active snapshot hash is invalid")
    value = _decode_canonical(data, "active snapshot")
    _validate_snapshot(value)
    for item in value["files"]:
        if item["bytes"] > max_object_bytes:
            raise MaterializeError(
                f"active snapshot object exceeds size limit: {item['path']}"
            )
    if (
        value["generation"] != active["generation"]
        or value["generation_id"] != active["generation_id"]
    ):
        raise MaterializeError("active snapshot identity is invalid")
    return value


def _validate_snapshot(value):
    if (
        not isinstance(value, dict)
        or set(value) != SNAPSHOT_FIELDS
        or value["protocol"] != PROTOCOL_SNAPSHOT
        or value["schema_version"] != 1
        or not _positive_int(value["generation"])
        or not isinstance(value["parent_generation"], int)
        or isinstance(value["parent_generation"], bool)
        or value["parent_generation"] < 0
        or value["parent_generation"] >= value["generation"]
        or not _is_generation_id(value["generation_id"])
        or not isinstance(value["files"], list)
        or not isinstance(value["tombstones"], list)
    ):
        raise MaterializeError("snapshot fields are invalid")
    if value["parent_generation"] == 0:
        if value["parent_generation_id"] != "":
            raise MaterializeError("initial snapshot parent identity is invalid")
    elif not _is_generation_id(value["parent_generation_id"]):
        raise MaterializeError("snapshot parent identity is invalid")
    seen = {}
    total = 0
    file_paths = []
    for item in value["files"]:
        if (
            not isinstance(item, dict)
            or set(item) != FILE_FIELDS
            or not isinstance(item["bytes"], int)
            or isinstance(item["bytes"], bool)
            or item["bytes"] < 0
            or not _is_sha256(item["sha256"])
            or item["content_class"] not in {"canonical-memory", "skill-source"}
        ):
            raise MaterializeError("snapshot file entry is invalid")
        relative = _validate_snapshot_replica_path(item["path"])
        expected_class = _content_class(relative)
        if item["content_class"] != expected_class:
            raise MaterializeError(
                f"snapshot content class does not match path: {relative}"
            )
        folded = _portable_path_key(relative)
        if folded in seen:
            raise MaterializeError("snapshot contains a case path collision")
        seen[folded] = relative
        file_paths.append(relative)
        total += item["bytes"]
        if total > 2**63 - 1:
            raise MaterializeError("snapshot object total is too large")
    if file_paths != sorted(file_paths):
        raise MaterializeError("snapshot file entries are not sorted")
    _validate_path_tree(
        [item["path"] for item in value["files"]],
        MaterializeError,
    )
    tombstone_paths = set()
    tombstone_values = []
    for item in value["tombstones"]:
        if (
            not isinstance(item, dict)
            or set(item) != TOMBSTONE_FIELDS
            or item["deleted_at_generation"] != value["generation"]
        ):
            raise MaterializeError("snapshot tombstone is invalid")
        relative = _validate_snapshot_replica_path(item["path"])
        folded = _portable_path_key(relative)
        if folded in tombstone_paths or folded in seen:
            raise MaterializeError("snapshot tombstone path conflicts")
        tombstone_paths.add(folded)
        tombstone_values.append(relative)
    if tombstone_values != sorted(tombstone_values):
        raise MaterializeError("snapshot tombstone entries are not sorted")
    _validate_path_tree(tombstone_values, MaterializeError)
    expected_identity = {
        "parent_generation_id": value["parent_generation_id"],
        "files": value["files"],
        "deleted_paths": [item["path"] for item in value["tombstones"]],
    }
    if value["generation_id"] != "generation-" + sha256_bytes(
        canonical_json_bytes(expected_identity)
    ):
        raise MaterializeError("snapshot generation ID is invalid")


def _validate_snapshot_transition(previous, snapshot):
    actual = [item["path"] for item in snapshot["tombstones"]]
    if previous is None:
        if snapshot["parent_generation"] == 0 and actual:
            raise MaterializeError(
                "initial snapshot cannot contain tombstones"
            )
        return
    previous_paths = {item["path"] for item in previous["files"]}
    target_paths = {item["path"] for item in snapshot["files"]}
    expected = sorted(previous_paths - target_paths)
    if actual != expected:
        raise MaterializeError(
            "snapshot tombstones do not match the parent transition"
        )


def _validate_complete(value):
    if (
        not isinstance(value, dict)
        or set(value) != COMPLETE_FIELDS
        or value["protocol"] != PROTOCOL_COMPLETE
        or value["schema_version"] != 1
        or not _positive_int(value["generation"])
        or not _is_generation_id(value["generation_id"])
        or not _is_sha256(value["snapshot_sha256"])
        or not isinstance(value["file_count"], int)
        or isinstance(value["file_count"], bool)
        or value["file_count"] < 0
        or not isinstance(value["object_bytes"], int)
        or isinstance(value["object_bytes"], bool)
        or value["object_bytes"] < 0
    ):
        raise MaterializeError("complete marker fields are invalid")


def _validate_current(value):
    if (
        not isinstance(value, dict)
        or set(value) != CURRENT_FIELDS
        or value["protocol"] != PROTOCOL_CURRENT
        or value["schema_version"] != 1
        or not _positive_int(value["generation"])
        or not _is_generation_id(value["generation_id"])
        or not _is_sha256(value["snapshot_sha256"])
    ):
        raise MaterializeError("current marker fields are invalid")


def _load_hash_cache(path, root):
    if not path.exists():
        return {}
    try:
        value = _load_json(
            path,
            max_bytes=16 * 1024 * 1024,
            root=root,
            name="snapshot hash cache",
        )
    except (OSError, ProtocolError, ValueError, json.JSONDecodeError):
        return {}
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or not isinstance(value.get("files"), dict)
    ):
        return {}
    return value["files"]


def _read_stable_file(path, before, max_bytes):
    data = read_bounded_regular_file(path, max_bytes=max_bytes, root=path.parent)
    after = os.lstat(path)
    if _stat_cache_key(before) != _stat_cache_key(after):
        raise PublisherError(f"Vault file changed while hashing: {path}")
    return data


def _stat_cache_key(info):
    return [
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    ]


def _load_json(path, *, max_bytes, root, name):
    data = read_bounded_regular_file(path, max_bytes=max_bytes, root=root)
    return _decode_canonical(data, name)


def _decode_canonical(data, name):
    try:
        value = json.loads(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MaterializeError(f"{name} JSON is malformed") from exc
    if canonical_json_bytes(value) != data:
        raise MaterializeError(f"{name} JSON is not canonical")
    return value


def _file_identity(files):
    return [
        (item["path"], item["bytes"], item["sha256"], item["content_class"])
        for item in files
    ]


def _configured_max_object_bytes(mapping, error_type):
    value = mapping.get(
        "max_replica_object_bytes",
        DEFAULT_MAX_OBJECT_BYTES,
    )
    if isinstance(value, bool):
        raise error_type("max_replica_object_bytes must be a positive integer")
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise error_type(
            "max_replica_object_bytes must be a positive integer"
        ) from exc
    if limit <= 0:
        raise error_type("max_replica_object_bytes must be a positive integer")
    return limit


def _metadata_read_limit(max_object_bytes, protocol_limit):
    return min(int(max_object_bytes), int(protocol_limit))


def _required_root(mapping, key, error_type):
    value = str(mapping.get(key) or "").strip()
    if not value:
        raise error_type(f"{key} is required")
    return Path(os.path.abspath(os.path.expanduser(value)))


def _is_sha256(value):
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _is_generation_id(value):
    return (
        isinstance(value, str)
        and value.startswith("generation-")
        and _is_sha256(value[len("generation-") :])
    )


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _unlink_regular(path, root, expected_identity=None):
    path = Path(path)
    if not os.path.lexists(path):
        return
    info = os.lstat(path)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
    ):
        raise MaterializeError(f"refusing to remove non-regular path: {path}")
    if (
        expected_identity is not None
        and _stat_identity(info) != list(expected_identity)
    ):
        raise MaterializeError(f"refusing to remove changed path: {path}")
    try:
        portable_unlink_regular(
            path,
            root=root,
            expected_identity=info,
        )
    except (OSError, ProtocolError, ValueError) as exc:
        raise MaterializeError(str(exc)) from exc


def _remove_if_regular(path, root):
    path = Path(path)
    if not os.path.lexists(path):
        return
    info = os.lstat(path)
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
    ):
        raise MaterializeError(f"cleanup path is not a regular file: {path}")
    try:
        portable_unlink_regular(
            path,
            root=root,
            expected_identity=info,
        )
    except (OSError, ProtocolError, ValueError) as exc:
        raise MaterializeError(str(exc)) from exc
