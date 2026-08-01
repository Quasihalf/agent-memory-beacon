import contextlib
import hashlib
import io
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from branding import LEGACY_LAUNCHD_LABELS
import beacon_sync_protocol
import doctor
import install_runtime
from doctor import (
    CommandResult,
    DoctorCheck,
    DoctorReport,
    _beacon_sync_check,
    _live_checks,
    main,
    run_profile,
)
from beacon_sync_producer import collect_transcripts, initialize_producer
from beacon_sync_reducer import reduce_inboxes
from beacon_sync_snapshot import (
    materialize_generation,
    publish_generation,
    publish_pending_receipts,
)
from memory_graph import render_memory_graph_quality_markdown
import install_beacon_sync


QUICK_CHECKS = (
    "configuration",
    "beacon-sync",
    "recall-index-schema",
    "memory-graph-schema",
    "script-compilation",
    "module-imports",
)
CI_CHECKS = QUICK_CHECKS + (
    "unit-tests",
    "runtime-evaluation",
    "git-diff-check",
)
LIVE_CHECKS = QUICK_CHECKS + (
    "runtime-release",
    "frontmatter",
    "wikilinks",
    "candidate-isolation",
    "codex-hooks",
    "launchd-plists",
    "launchd-services",
    "prompt-hook-probe",
)


class RecordingRunner:
    def __init__(self, timeout_on=""):
        self.calls = []
        self.timeout_on = timeout_on

    def __call__(self, args, *, cwd, timeout, input_text=None):
        self.calls.append(
            {
                "args": args,
                "cwd": cwd,
                "timeout": timeout,
                "input_text": input_text,
            }
        )
        if self.timeout_on and self.timeout_on in " ".join(args):
            raise subprocess.TimeoutExpired(args, timeout)
        stdout = "{}\n" if "codex_prompt_hook.py" in " ".join(args) else ""
        return CommandResult(returncode=0, stdout=stdout, stderr="")


class LiveRecordingRunner(RecordingRunner):
    def __init__(
        self,
        cfg,
        *,
        stale_label="",
        loaded_legacy="",
        missing_label="",
    ):
        super().__init__()
        self.cfg = cfg
        self.stale_label = stale_label
        self.loaded_legacy = loaded_legacy
        self.missing_label = missing_label

    def __call__(self, args, *, cwd, timeout, input_text=None):
        if tuple(args[:2]) != ("/bin/launchctl", "print"):
            return super().__call__(
                args,
                cwd=cwd,
                timeout=timeout,
                input_text=input_text,
            )
        self.calls.append(
            {
                "args": args,
                "cwd": cwd,
                "timeout": timeout,
                "input_text": input_text,
            }
        )
        label = args[-1].rsplit("/", 1)[-1]
        if label == self.missing_label:
            return CommandResult(returncode=113, stderr="Could not find service")
        if label in set(LEGACY_LAUNCHD_LABELS.values()) and label != self.loaded_legacy:
            return CommandResult(returncode=113, stderr="Could not find service")
        runtime = (
            os.path.join(os.path.dirname(self.cfg["runtime_root"]), "stale-runtime")
            if label == self.stale_label
            else self.cfg["runtime_root"]
        )
        if label.endswith("harvest"):
            script = "session_harvester.py"
        elif label.endswith("sync"):
            script = "beacon_sync.py"
        else:
            script = "runner.py"
        python_path = os.path.join(runtime, ".venv", "bin", "python")
        script_path = os.path.join(runtime, "scripts", script)
        return CommandResult(
            returncode=0,
            stdout=(
                f"program = {python_path}\n"
                "arguments = {\n"
                f"    {python_path}\n"
                f"    {script_path}\n"
                "}\n"
            ),
        )


class WindowsTaskRecordingRunner(RecordingRunner):
    def __init__(self, task_xml, *, last_result="0"):
        super().__init__()
        self.task_xml = task_xml
        self.last_result = last_result

    def __call__(self, args, *, cwd, timeout, input_text=None):
        if args and args[0] == "/bin/launchctl":
            raise AssertionError("Windows live Doctor must not invoke launchctl")
        if args and args[0].lower().endswith("schtasks.exe"):
            self.calls.append(
                {
                    "args": args,
                    "cwd": cwd,
                    "timeout": timeout,
                    "input_text": input_text,
                }
            )
            if "/XML" in args:
                return CommandResult(returncode=0, stdout=self.task_xml)
            return CommandResult(
                returncode=0,
                stdout=(
                    '"WINDOWS-HOST","\\Agent Memory Beacon Sync",'
                    '"2026-08-01 10:00:00","Ready","Interactive only",'
                    f'"2026-07-31 10:00:00","{self.last_result}"\n'
                ),
            )
        return super().__call__(
            args,
            cwd=cwd,
            timeout=timeout,
            input_text=input_text,
        )


class DoctorTests(unittest.TestCase):
    def test_profiles_have_deterministic_order_and_scope(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}
            for profile, expected in (
                ("quick", QUICK_CHECKS),
                ("ci", CI_CHECKS),
                ("live", LIVE_CHECKS),
            ):
                with self.subTest(profile=profile):
                    report = run_profile(
                        profile,
                        repo_root=REPO_ROOT,
                        cfg=cfg,
                        runner=RecordingRunner(),
                    )
                    self.assertEqual(
                        tuple(check.name for check in report.checks),
                        expected,
                    )

    def test_quick_profile_rejects_wrong_recall_schema(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            index_path = os.path.join(
                cfg["vault_path"], "05-Agent-Memory", "recall-index.json"
            )
            write_json(index_path, {"schema_version": "1.0", "units": []})

            report = run_profile(
                "quick",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=RecordingRunner(),
            )

            check = check_named(report, "recall-index-schema")
            self.assertFalse(check.passed)
            self.assertIn("schema", check.details)
            self.assertFalse(report.passed)

    def test_quick_and_ci_do_not_check_installed_runtime_release(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            Path(cfg["runtime_root"], "release-manifest.json").unlink()

            quick = run_profile(
                "quick",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=RecordingRunner(),
            )
            ci = run_profile(
                "ci",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=RecordingRunner(),
            )
            self.assertTrue(quick.passed)
            self.assertTrue(ci.passed)
            self.assertNotIn(
                "runtime-release",
                {check.name for check in quick.checks},
            )
            self.assertNotIn(
                "runtime-release",
                {check.name for check in ci.checks},
            )

    def test_authority_live_checks_installed_runtime_release(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)

            live = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )

            release = check_named(live, "runtime-release")
            self.assertTrue(release.passed, release.details)

    def test_authority_live_rejects_missing_runtime_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            Path(cfg["runtime_root"], "release-manifest.json").unlink()

            report = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )

            release = check_named(report, "runtime-release")
            self.assertFalse(release.passed)
            self.assertIn("manifest is missing", release.details)

    def test_authority_live_rejects_corrupt_runtime_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            Path(cfg["runtime_root"], "release-manifest.json").write_text(
                "{not-json\n",
                encoding="utf-8",
            )

            report = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )

            release = check_named(report, "runtime-release")
            self.assertFalse(release.passed)
            self.assertIn("manifest is invalid", release.details)

    def test_authority_live_rejects_runtime_file_hash_drift(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            Path(cfg["runtime_root"], "scripts", "config.yaml").write_text(
                "runtime_root: changed\n",
                encoding="utf-8",
            )

            report = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )

            release = check_named(report, "runtime-release")
            self.assertFalse(release.passed)
            self.assertIn("release file changed", release.details)


class BeaconSyncDoctorTests(unittest.TestCase):
    def test_disabled_sync_is_explicitly_healthy(self):
        check = _beacon_sync_check(
            {"beacon_sync": {"enabled": False, "role": ""}}
        )
        self.assertTrue(check.passed)
        self.assertIn("disabled", check.details)

    def test_windows_producer_reports_unsupported_atomic_filesystem_api(self):
        producer = {
            "enabled": True,
            "role": "producer-replica",
        }
        with (
            patch.object(doctor.os, "name", "nt"),
            patch.object(
                beacon_sync_protocol,
                "_assert_supported_windows_atomic_filesystem",
                side_effect=beacon_sync_protocol.ProtocolError(
                    "Windows build 17763 or newer is required"
                ),
            ),
        ):
            check = _beacon_sync_check({"beacon_sync": producer})

        self.assertFalse(check.passed)
        self.assertIn("17763", check.details)

    def test_healthy_producer_and_authority_are_accepted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            producer = {
                "enabled": True,
                "role": "producer-replica",
                "device_id": "windows-test",
                "state_dir": str(root / "producer-state"),
                "outbox_dir": str(root / "outbox"),
                "received_published_dir": str(root / "received"),
                "replica_path": str(root / "replica"),
                "gc_retention_seconds": 604800,
            }
            initialize_producer(producer)
            self.assertTrue(
                _beacon_sync_check({"beacon_sync": producer}).passed
            )

            vault = root / "vault"
            vault.mkdir()
            (vault / "memory.md").write_text("memory\n", encoding="utf-8")
            authority = {
                "enabled": True,
                "role": "authority",
                "state_dir": str(root / "authority-state"),
                "published_dir": str(root / "published"),
                "inboxes": [],
                "max_replica_object_bytes": 64 * 1024 * 1024,
            }
            publish_generation({"vault_path": str(vault)}, authority)
            self.assertTrue(
                _beacon_sync_check(
                    {
                        "vault_path": str(vault),
                        "beacon_sync": authority,
                    }
                ).passed
            )

    def test_missing_queued_attachment_cas_object_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            producer = make_attachment_producer(root, max_events_per_run=1)
            cas_object = next(
                path
                for path in (
                    root / "producer-state" / "attachment-cas"
                ).rglob("*")
                if path.is_file()
            )
            cas_object.unlink()

            check = _beacon_sync_check({"beacon_sync": producer})

            self.assertFalse(check.passed)
            self.assertIn("attachment CAS", check.details)

    def test_authority_requires_current_ledger_schema(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "vault"
            vault.mkdir()
            (vault / "memory.md").write_text("memory\n", encoding="utf-8")
            sync = {
                "enabled": True,
                "role": "authority",
                "state_dir": str(root / "state"),
                "published_dir": str(root / "published"),
                "inboxes": [],
                "max_replica_object_bytes": 64 * 1024 * 1024,
            }
            publish_generation({"vault_path": str(vault)}, sync)
            state = root / "state"
            state.mkdir(exist_ok=True)
            ledger = sqlite3.connect(state / "ledger.sqlite3")
            ledger.executescript(
                """
                create table metadata (
                    key text primary key,
                    value text not null
                );
                insert into metadata values ('schema_version', '2');
                """
            )
            ledger.commit()
            ledger.close()

            check = _beacon_sync_check(
                {
                    "vault_path": str(vault),
                    "beacon_sync": sync,
                }
            )

            self.assertFalse(check.passed)
            self.assertIn("ledger schema", check.details)

    def test_authority_rejects_current_version_metadata_only_ledger(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, sync = make_authority_doctor_fixture(root)
            state = Path(sync["state_dir"])
            state.mkdir(exist_ok=True)
            ledger = sqlite3.connect(state / "ledger.sqlite3")
            ledger.executescript(
                """
                create table metadata (
                    key text primary key,
                    value text not null
                );
                insert into metadata values ('schema_version', '4');
                """
            )
            ledger.commit()
            ledger.close()

            check = _beacon_sync_check(
                {"vault_path": str(vault), "beacon_sync": sync}
            )

            self.assertFalse(check.passed)
            self.assertIn("ledger schema", check.details)
            self.assertIn("tables", check.details)

    def test_authority_rejects_ledger_with_missing_current_column(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, sync = make_authority_doctor_fixture(root)
            reduce_inboxes({"vault_path": str(vault)}, sync)
            ledger = sqlite3.connect(Path(sync["state_dir"]) / "ledger.sqlite3")
            ledger.execute("alter table events drop column metadata_bytes")
            ledger.commit()
            ledger.close()

            check = _beacon_sync_check(
                {"vault_path": str(vault), "beacon_sync": sync}
            )

            self.assertFalse(check.passed)
            self.assertIn("events columns", check.details)

    def test_authority_rejects_ledger_without_unique_event_id(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, sync = make_authority_doctor_fixture(root)
            reduce_inboxes({"vault_path": str(vault)}, sync)
            ledger = sqlite3.connect(Path(sync["state_dir"]) / "ledger.sqlite3")
            table_sql = ledger.execute(
                "select sql from sqlite_master where type = 'table' and name = 'events'"
            ).fetchone()[0]
            without_unique = table_sql.replace(
                "event_id text not null unique",
                "event_id text not null",
            )
            self.assertNotEqual(without_unique, table_sql)
            ledger.execute("alter table events rename to events_with_unique")
            ledger.execute(without_unique)
            ledger.execute("drop table events_with_unique")
            ledger.commit()
            ledger.close()

            check = _beacon_sync_check(
                {"vault_path": str(vault), "beacon_sync": sync}
            )

            self.assertFalse(check.passed)
            self.assertIn("event_id unique", check.details)

    def test_authority_rejects_ledger_with_unexpected_table(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, sync = make_authority_doctor_fixture(root)
            reduce_inboxes({"vault_path": str(vault)}, sync)
            ledger = sqlite3.connect(Path(sync["state_dir"]) / "ledger.sqlite3")
            ledger.execute("create table unexpected_state (value text)")
            ledger.commit()
            ledger.close()

            check = _beacon_sync_check(
                {"vault_path": str(vault), "beacon_sync": sync}
            )

            self.assertFalse(check.passed)
            self.assertIn("ledger schema tables", check.details)

    def test_authority_rejects_partial_unique_event_id_index(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault, sync = make_authority_doctor_fixture(root)
            reduce_inboxes({"vault_path": str(vault)}, sync)
            ledger = sqlite3.connect(Path(sync["state_dir"]) / "ledger.sqlite3")
            table_sql = ledger.execute(
                "select sql from sqlite_master where type = 'table' and name = 'events'"
            ).fetchone()[0]
            without_unique = table_sql.replace(
                "event_id text not null unique",
                "event_id text not null",
            )
            self.assertNotEqual(without_unique, table_sql)
            ledger.execute("alter table events rename to events_with_unique")
            ledger.execute(without_unique)
            ledger.execute("drop table events_with_unique")
            ledger.execute(
                "create unique index partial_event_id on events(event_id) "
                "where status != ''"
            )
            ledger.commit()
            ledger.close()

            check = _beacon_sync_check(
                {"vault_path": str(vault), "beacon_sync": sync}
            )

            self.assertFalse(check.passed)
            self.assertIn("event_id unique", check.details)

    def test_missing_canonical_attachment_blob_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _producer, authority, vault = reduce_attachment_to_authority(root)
            blob = next(
                (
                    vault
                    / "Attachments"
                    / "Agent-Memory-Beacon"
                    / "remote"
                    / "objects"
                ).rglob("*.*")
            )
            blob.unlink()

            check = _beacon_sync_check(
                {
                    "vault_path": str(vault),
                    "beacon_sync": authority,
                }
            )

            self.assertFalse(check.passed)
            self.assertIn("attachment blob", check.details)

    def test_corrupt_canonical_attachment_metadata_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _producer, authority, vault = reduce_attachment_to_authority(root)
            metadata = next(
                (
                    vault
                    / "04-Feedback"
                    / "remote-attachments"
                ).rglob("*.md")
            )
            metadata.write_text("corrupt metadata\n", encoding="utf-8")

            check = _beacon_sync_check(
                {
                    "vault_path": str(vault),
                    "beacon_sync": authority,
                }
            )

            self.assertFalse(check.passed)
            self.assertIn("attachment metadata", check.details)

    def test_missing_finalized_receipt_is_reported_immediately(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = root / "sessions"
            sessions.mkdir()
            (sessions / "session.jsonl").write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {"id": "session", "cwd": "/demo"},
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"text": "[DECISION:同步| context:测试]"}],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            outbox = root / "outbox"
            producer = {
                "device_id": "windows-test",
                "state_dir": str(root / "producer-state"),
                "outbox_dir": str(outbox),
                "transcript_paths": [str(sessions)],
                "max_chunk_bytes": 4096,
                "max_gap_bytes": 4096,
                "max_events_per_run": 32,
            }
            collect_transcripts(producer, include_existing=True)
            vault = root / "vault"
            (vault / "04-Feedback" / "_logs").mkdir(parents=True)
            (vault / "memory.md").write_text("memory\n", encoding="utf-8")
            authority = {
                "enabled": True,
                "role": "authority",
                "state_dir": str(root / "authority-state"),
                "published_dir": str(root / "published"),
                "inboxes": [
                    {"device_id": "windows-test", "path": str(outbox)}
                ],
                "max_replica_object_bytes": 64 * 1024 * 1024,
            }
            reduce_inboxes(
                {"vault_path": str(vault)},
                authority,
                harvest_adapter=lambda _cfg, paths: {
                    str(path): True for path in paths
                },
            )
            generation = publish_generation(
                {"vault_path": str(vault)},
                authority,
            )
            publish_pending_receipts(authority, generation)
            receipt = next(
                (root / "published" / "v1" / "receipts").rglob("*.json")
            )
            receipt.unlink()

            check = _beacon_sync_check({"beacon_sync": authority})

            self.assertFalse(check.passed)
            self.assertIn("receipt", check.details)

    def test_missing_object_and_corrupt_current_are_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "vault"
            vault.mkdir()
            (vault / "memory.md").write_text("memory\n", encoding="utf-8")
            sync = {
                "enabled": True,
                "role": "authority",
                "state_dir": str(root / "state"),
                "published_dir": str(root / "published"),
                "inboxes": [],
                "max_replica_object_bytes": 64 * 1024 * 1024,
            }
            result = publish_generation({"vault_path": str(vault)}, sync)
            snapshot = json.loads(
                (
                    root
                    / "published"
                    / "v1"
                    / "snapshots"
                    / f"{result['generation']:020d}"
                    / "snapshot.json"
                ).read_text(encoding="utf-8")
            )
            item = snapshot["files"][0]
            (
                root
                / "published"
                / "v1"
                / "objects"
                / item["sha256"][:2]
                / item["sha256"]
            ).unlink()
            missing = _beacon_sync_check({"beacon_sync": sync})
            self.assertFalse(missing.passed)
            self.assertIn("object", missing.details)

            current = root / "published" / "v1" / "current.json"
            current.write_text("{broken", encoding="utf-8")
            corrupt = _beacon_sync_check({"beacon_sync": sync})
            self.assertFalse(corrupt.passed)
            self.assertIn("current", corrupt.details)

    def test_blocked_sequence_and_stale_pending_receipt_are_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "vault"
            vault.mkdir()
            (vault / "memory.md").write_text("memory\n", encoding="utf-8")
            sync = {
                "enabled": True,
                "role": "authority",
                "state_dir": str(root / "state"),
                "published_dir": str(root / "published"),
                "inboxes": [],
                "max_replica_object_bytes": 64 * 1024 * 1024,
                "gc_retention_seconds": 60,
            }
            publish_generation({"vault_path": str(vault)}, sync)
            reduce_inboxes({"vault_path": str(vault)}, sync)
            state = root / "state"
            ledger = sqlite3.connect(state / "ledger.sqlite3")
            ledger.execute(
                """
                insert into producers(
                    producer_instance_id, device_id, next_seq,
                    blocked_code, updated_at
                ) values (?, ?, ?, ?, ?)
                """,
                (
                    "producer",
                    "windows-test",
                    1,
                    "sequence_conflict",
                    "2000-01-01T00:00:00Z",
                ),
            )
            insert_doctor_event(
                ledger,
                status="applied_pending_publish",
                processed_at="2000-01-01T00:00:00Z",
            )
            ledger.commit()
            ledger.close()

            check = _beacon_sync_check({"beacon_sync": sync})

            self.assertFalse(check.passed)
            self.assertIn("blocked", check.details)
            self.assertIn("stale", check.details)

    def test_replica_drift_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "vault"
            vault.mkdir()
            (vault / "memory.md").write_text("memory\n", encoding="utf-8")
            authority = {
                "state_dir": str(root / "authority-state"),
                "published_dir": str(root / "published"),
            }
            publish_generation({"vault_path": str(vault)}, authority)
            received = root / "received"
            shutil.copytree(root / "published", received)
            producer = {
                "enabled": True,
                "role": "producer-replica",
                "device_id": "windows-test",
                "state_dir": str(root / "producer-state"),
                "outbox_dir": str(root / "outbox"),
                "received_published_dir": str(received),
                "replica_path": str(root / "replica"),
                "max_replica_object_bytes": 64 * 1024 * 1024,
                "gc_retention_seconds": 604800,
            }
            initialize_producer(producer)
            materialize_generation(producer, bootstrap=True)
            managed = root / "replica" / "memory.md"
            os.chmod(managed, 0o600)
            managed.write_text("local drift\n", encoding="utf-8")

            check = _beacon_sync_check({"beacon_sync": producer})

            self.assertFalse(check.passed)
            self.assertIn("drift", check.details)

    def test_received_current_requires_materialized_matching_replica(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "vault"
            vault.mkdir()
            memory = vault / "memory.md"
            memory.write_text("generation one\n", encoding="utf-8")
            authority = {
                "state_dir": str(root / "authority-state"),
                "published_dir": str(root / "published"),
                "max_replica_object_bytes": 64 * 1024 * 1024,
            }
            publish_generation({"vault_path": str(vault)}, authority)
            received = root / "received"
            shutil.copytree(root / "published", received)
            producer = {
                "enabled": True,
                "role": "producer-replica",
                "device_id": "windows-test",
                "state_dir": str(root / "producer-state"),
                "outbox_dir": str(root / "outbox"),
                "received_published_dir": str(received),
                "replica_path": str(root / "replica"),
                "max_replica_object_bytes": 64 * 1024 * 1024,
                "gc_retention_seconds": 604800,
            }
            initialize_producer(producer)

            missing = _beacon_sync_check({"beacon_sync": producer})

            self.assertFalse(missing.passed)
            self.assertIn("not materialized", missing.details)

            materialize_generation(producer, bootstrap=True)
            memory.write_text("generation two\n", encoding="utf-8")
            publish_generation({"vault_path": str(vault)}, authority)
            shutil.copytree(root / "published", received, dirs_exist_ok=True)

            behind = _beacon_sync_check({"beacon_sync": producer})

            self.assertFalse(behind.passed)
            self.assertIn("behind", behind.details)

    def test_producer_replica_live_profile_skips_canonical_mac_checks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = make_windows_producer_live_fixture(root)
            with (
                patch.object(doctor, "_scheduler_platform", return_value="windows"),
                patch.object(
                    doctor,
                    "_windows_task_check",
                    return_value=DoctorCheck(
                        "windows-task",
                        True,
                        True,
                        "ok",
                    ),
                ),
            ):
                report = run_profile(
                    "live",
                    repo_root=REPO_ROOT,
                    cfg=cfg,
                    runner=RecordingRunner(),
                )

            names = {check.name for check in report.checks}
            self.assertTrue(report.passed)
            self.assertIn("beacon-sync", names)
            self.assertIn("runtime-release", names)
            self.assertIn("windows-task", names)
            for skipped in (
                "recall-index-schema",
                "memory-graph-schema",
                "frontmatter",
                "wikilinks",
                "candidate-isolation",
                "codex-hooks",
                "launchd-plists",
                "launchd-services",
                "prompt-hook-probe",
            ):
                self.assertNotIn(skipped, names)

    def test_windows_producer_live_rejects_release_id_path_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = make_windows_producer_live_fixture(root)
            manifest_path = Path(cfg["runtime_root"], "release-manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["release_id"] = "f" * 16
            write_json(manifest_path, manifest)

            with (
                patch.object(doctor, "_scheduler_platform", return_value="windows"),
                patch.object(
                    doctor,
                    "_windows_task_check",
                    return_value=DoctorCheck("windows-task", True, True, "ok"),
                ),
            ):
                report = run_profile(
                    "live",
                    repo_root=REPO_ROOT,
                    cfg=cfg,
                    runner=RecordingRunner(),
                )

            release = check_named(report, "runtime-release")
            self.assertFalse(release.passed)
            self.assertIn("identity does not match directory", release.details)

    def test_windows_producer_live_rejects_config_runtime_root_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            cfg = make_windows_producer_live_fixture(root)
            manifest_path = Path(cfg["runtime_root"], "release-manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["install_root"] = str(root / "different-runtime")
            write_json(manifest_path, manifest)

            with (
                patch.object(doctor, "_scheduler_platform", return_value="windows"),
                patch.object(
                    doctor,
                    "_windows_task_check",
                    return_value=DoctorCheck("windows-task", True, True, "ok"),
                ),
            ):
                report = run_profile(
                    "live",
                    repo_root=REPO_ROOT,
                    cfg=cfg,
                    runner=RecordingRunner(),
                )

            release = check_named(report, "runtime-release")
            self.assertFalse(release.passed)
            self.assertIn("manifest identity is invalid", release.details)

    def test_windows_producer_live_rejects_missing_corrupt_or_drifted_release(self):
        cases = (
            (
                "missing-manifest",
                lambda runtime: (runtime / "release-manifest.json").unlink(),
                "manifest is missing",
            ),
            (
                "corrupt-manifest",
                lambda runtime: (runtime / "release-manifest.json").write_text(
                    "{not-json\n",
                    encoding="utf-8",
                ),
                "manifest is invalid",
            ),
            (
                "file-hash-drift",
                lambda runtime: (runtime / "scripts" / "config.yaml").write_text(
                    "runtime_root: changed\n",
                    encoding="utf-8",
                ),
                "release file changed",
            ),
        )
        for label, damage, expected in cases:
            with self.subTest(case=label), tempfile.TemporaryDirectory() as temp:
                cfg = make_windows_producer_live_fixture(Path(temp))
                damage(Path(cfg["runtime_root"]))
                with (
                    patch.object(
                        doctor,
                        "_scheduler_platform",
                        return_value="windows",
                    ),
                    patch.object(
                        doctor,
                        "_windows_task_check",
                        return_value=DoctorCheck(
                            "windows-task",
                            True,
                            True,
                            "ok",
                        ),
                    ),
                ):
                    report = run_profile(
                        "live",
                        repo_root=REPO_ROOT,
                        cfg=cfg,
                        runner=RecordingRunner(),
                    )

                release = check_named(report, "runtime-release")
                self.assertFalse(release.passed)
                self.assertFalse(report.passed)
                self.assertIn(expected, release.details)

    def test_pending_receipt_delivery_slo_is_24_hours_not_gc_retention(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "vault"
            vault.mkdir()
            (vault / "memory.md").write_text("memory\n", encoding="utf-8")
            sync = {
                "enabled": True,
                "role": "authority",
                "state_dir": str(root / "state"),
                "published_dir": str(root / "published"),
                "inboxes": [],
                "max_replica_object_bytes": 64 * 1024 * 1024,
                "gc_retention_seconds": 7 * 24 * 60 * 60,
            }
            publish_generation({"vault_path": str(vault)}, sync)
            state = root / "state"
            state.mkdir(exist_ok=True)
            processed_at = (
                datetime.now(timezone.utc) - timedelta(hours=25)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            reduce_inboxes({"vault_path": str(vault)}, sync)
            ledger = sqlite3.connect(state / "ledger.sqlite3")
            insert_doctor_event(
                ledger,
                status="applied_pending_publish",
                processed_at=processed_at,
            )
            ledger.commit()
            ledger.close()

            check = _beacon_sync_check({"beacon_sync": sync})

            self.assertFalse(check.passed)
            self.assertIn("stale pending receipt", check.details)

    def test_partial_pending_receipt_generation_binding_is_reported_immediately(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "vault"
            vault.mkdir()
            (vault / "memory.md").write_text("memory\n", encoding="utf-8")
            sync = {
                "enabled": True,
                "role": "authority",
                "state_dir": str(root / "state"),
                "published_dir": str(root / "published"),
                "inboxes": [],
                "max_replica_object_bytes": 64 * 1024 * 1024,
            }
            publish_generation({"vault_path": str(vault)}, sync)
            reduce_inboxes({"vault_path": str(vault)}, sync)
            state = root / "state"
            ledger = sqlite3.connect(state / "ledger.sqlite3")
            insert_doctor_event(
                ledger,
                status="applied_pending_publish",
                processed_at="2099-01-01T00:00:00Z",
                canonical_generation=1,
                generation_id="",
            )
            ledger.commit()
            ledger.close()

            check = _beacon_sync_check({"beacon_sync": sync})

            self.assertFalse(check.passed)
            self.assertIn("partial generation binding", check.details)

    def test_quick_profile_rejects_invalid_unit_from_vault_relative_index(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            cfg["memory_runtime"] = {
                "index_path": "05-Agent-Memory/recall-index.json"
            }
            index_path = os.path.join(
                cfg["vault_path"], "05-Agent-Memory", "recall-index.json"
            )
            write_json(
                index_path,
                {
                    "schema_version": "2.0",
                    "units": [{"id": "incomplete-runtime-unit"}],
                },
            )

            report = run_profile(
                "quick",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=RecordingRunner(),
            )

            check = check_named(report, "recall-index-schema")
            self.assertFalse(check.passed)
            self.assertIn("incomplete-runtime-unit", check.details)

    def test_quick_profile_rejects_invalid_memory_graph_edge(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            index_path = os.path.join(
                cfg["vault_path"],
                "05-Agent-Memory",
                "recall-index.json",
            )
            graph_path = os.path.join(
                cfg["vault_path"],
                "05-Agent-Memory",
                "memory-graph.json",
            )
            write_json(
                index_path,
                {
                    "schema_version": "2.0",
                    "units": [],
                },
            )
            write_json(
                graph_path,
                {
                    "schema_version": "3.0",
                    "generated_by": "knowledge_index.py",
                    "generated_at": "2026-07-26T10:00:00+08:00",
                    "nodes": [],
                    "edges": [
                        {
                            "source": "missing:source",
                            "target": "missing:target",
                            "relation": "supports",
                            "confidence": 1.0,
                            "evidence": [],
                        }
                    ],
                },
            )

            report = run_profile(
                "quick",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=RecordingRunner(),
            )

            self.assertIn(
                "memory-graph-schema",
                {item.name for item in report.checks},
            )
            check = check_named(report, "memory-graph-schema")
            self.assertFalse(check.passed)
            self.assertIn("pre-generation", check.details)
            self.assertFalse(report.passed)

    def test_quick_accepts_legacy_graph_but_live_requires_v3(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            graph_path = os.path.join(
                cfg["vault_path"],
                "05-Agent-Memory",
                "memory-graph.json",
            )
            write_json(
                graph_path,
                {
                    "schema_version": "2.0",
                    "nodes": [],
                    "edges": [],
                },
            )
            os.unlink(
                os.path.join(
                    cfg["vault_path"],
                    "05-Agent-Memory",
                    "memory-graph-quality.md",
                )
            )

            quick = run_profile(
                "quick",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=RecordingRunner(),
            )
            live = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )

            self.assertTrue(check_named(quick, "memory-graph-schema").passed)
            self.assertFalse(check_named(live, "memory-graph-schema").passed)
            self.assertIn(
                "schema must be 3.0",
                check_named(live, "memory-graph-schema").details,
            )

    def test_quick_rejects_legacy_graph_with_dangling_or_unknown_relation(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            graph_path = os.path.join(
                cfg["vault_path"],
                "05-Agent-Memory",
                "memory-graph.json",
            )
            write_json(
                graph_path,
                {
                    "schema_version": "2.0",
                    "nodes": [
                        {
                            "id": "decision:legacy",
                            "type": "decision",
                            "label": "Legacy decision",
                            "path": "01-Projects/demo/Memory/decisions",
                            "project": "demo",
                        }
                    ],
                    "edges": [
                        {
                            "source": "decision:legacy",
                            "target": "workflow:missing",
                            "relation": "invented_relation",
                        }
                    ],
                },
            )

            quick = run_profile(
                "quick",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=RecordingRunner(),
            )

            check = check_named(quick, "memory-graph-schema")
            self.assertFalse(check.passed)
            self.assertIn("invalid_graph_shape", check.details)

    def test_quick_accepts_pre_generation_v3_for_upgrade_but_live_rejects_it(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            index_path = os.path.join(
                cfg["vault_path"],
                "05-Agent-Memory",
                "recall-index.json",
            )
            graph_path = os.path.join(
                cfg["vault_path"],
                "05-Agent-Memory",
                "memory-graph.json",
            )
            write_json(
                index_path,
                {
                    "schema_version": "2.0",
                    "units": [],
                },
            )
            write_json(
                graph_path,
                {
                    "schema_version": "3.0",
                    "generated_by": "knowledge_index.py",
                    "generated_at": "2026-07-25T10:00:00+08:00",
                    "nodes": [],
                    "edges": [],
                },
            )
            os.unlink(
                os.path.join(
                    cfg["vault_path"],
                    "05-Agent-Memory",
                    "memory-graph-quality.md",
                )
            )

            quick = run_profile(
                "quick",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=RecordingRunner(),
            )
            live = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )

            quick_check = check_named(quick, "memory-graph-schema")
            live_check = check_named(live, "memory-graph-schema")
            self.assertTrue(quick_check.passed)
            self.assertIn("pre-generation Graph v3", quick_check.details)
            self.assertFalse(live_check.passed)
            self.assertIn("generation_id", live_check.details)

    def test_quick_accepts_previous_v3_missing_only_nonmemory_revisions(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            index_path = os.path.join(
                cfg["vault_path"],
                "05-Agent-Memory",
                "recall-index.json",
            )
            graph_path = os.path.join(
                cfg["vault_path"],
                "05-Agent-Memory",
                "memory-graph.json",
            )
            write_json(
                index_path,
                {
                    "schema_version": "2.0",
                    "generation_id": "previous-v3-generation",
                    "units": [],
                },
            )
            write_json(
                graph_path,
                {
                    "schema_version": "3.0",
                    "generated_by": "knowledge_index.py",
                    "generated_at": "2026-07-25T10:00:00+08:00",
                    "generation_id": "previous-v3-generation",
                    "nodes": [
                        {
                            "id": "note:demo",
                            "type": "note",
                            "kind": "session",
                            "label": "Demo",
                            "path": "01-Projects/demo/Memory/sessions/demo",
                            "project": "demo",
                            "date": "2026-07-25",
                            "revision": "",
                            "source_refs": ["note:demo"],
                            "resolved": True,
                        },
                        {
                            "id": "project:demo",
                            "type": "project",
                            "kind": "project",
                            "label": "demo",
                            "path": "",
                            "project": "demo",
                            "date": "",
                            "revision": "",
                            "source_refs": ["note:demo"],
                            "resolved": True,
                        },
                    ],
                    "edges": [
                        {
                            "source": "note:demo",
                            "target": "project:demo",
                            "relation": "belongs_to",
                            "confidence": 1.0,
                            "evidence": [
                                {
                                    "source_ref": "note:demo",
                                    "source_revision": "",
                                    "observed_at": "2026-07-25",
                                    "derivation": "note-frontmatter",
                                }
                            ],
                        }
                    ],
                },
            )
            os.unlink(
                os.path.join(
                    cfg["vault_path"],
                    "05-Agent-Memory",
                    "memory-graph-quality.md",
                )
            )

            quick = run_profile(
                "quick",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=RecordingRunner(),
            )
            live = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )

            quick_check = check_named(quick, "memory-graph-schema")
            live_check = check_named(live, "memory-graph-schema")
            self.assertTrue(quick_check.passed)
            self.assertIn("previous Graph v3", quick_check.details)
            self.assertFalse(live_check.passed)
            self.assertIn("missing_evidence=1", live_check.details)

    def test_quick_rejects_malformed_pre_generation_v3(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            index_path = os.path.join(
                cfg["vault_path"],
                "05-Agent-Memory",
                "recall-index.json",
            )
            graph_path = os.path.join(
                cfg["vault_path"],
                "05-Agent-Memory",
                "memory-graph.json",
            )
            write_json(
                index_path,
                {
                    "schema_version": "2.0",
                    "units": [],
                },
            )
            write_json(
                graph_path,
                {
                    "schema_version": "3.0",
                    "generated_by": "knowledge_index.py",
                    "generated_at": "2026-07-25T10:00:00+08:00",
                    "nodes": [
                        {
                            "id": "note:demo",
                            "type": "note",
                            "kind": "session",
                            "label": "Demo",
                            "path": "01-Projects/demo/Memory/sessions/demo",
                            "project": "demo",
                            "date": "2026-07-25",
                            "revision": "a" * 64,
                            "source_refs": ["note:demo"],
                            "resolved": True,
                        },
                        {
                            "id": "project:demo",
                            "type": "project",
                            "kind": "project",
                            "label": "demo",
                            "path": "",
                            "project": "demo",
                            "date": "",
                            "revision": "",
                            "source_refs": ["note:demo"],
                            "resolved": True,
                        },
                    ],
                    "edges": [
                        {
                            "source": "note:demo",
                            "target": "project:demo",
                            "relation": "belongs_to",
                            "confidence": 1.0,
                            "evidence": [],
                        }
                    ],
                },
            )

            quick = run_profile(
                "quick",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=RecordingRunner(),
            )

            check = check_named(quick, "memory-graph-schema")
            self.assertFalse(check.passed)
            self.assertIn("pre-generation", check.details)

    def test_quick_profile_rejects_stale_graph_quality_report(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            quality_path = os.path.join(
                cfg["vault_path"],
                "05-Agent-Memory",
                "memory-graph-quality.md",
            )
            write_text(quality_path, "# stale quality report\n")

            report = run_profile(
                "quick",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=RecordingRunner(),
            )

            check = check_named(report, "memory-graph-schema")
            self.assertFalse(check.passed)
            self.assertIn("quality report", check.details)

    def test_quick_profile_uses_graph_beside_custom_recall_index(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            default_dir = os.path.join(cfg["vault_path"], "05-Agent-Memory")
            custom_dir = os.path.join(cfg["vault_path"], "06-Custom")
            os.makedirs(custom_dir)
            os.replace(
                os.path.join(default_dir, "recall-index.json"),
                os.path.join(custom_dir, "runtime-recall.json"),
            )
            os.replace(
                os.path.join(default_dir, "memory-graph.json"),
                os.path.join(custom_dir, "memory-graph.json"),
            )
            cfg["memory_runtime"] = {
                "index_path": "06-Custom/runtime-recall.json"
            }

            report = run_profile(
                "quick",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=RecordingRunner(),
            )

            self.assertTrue(check_named(report, "memory-graph-schema").passed)

    def test_subprocesses_use_argument_sequences_and_bounded_timeouts(self):
        with tempfile.TemporaryDirectory(prefix="doctor;touch-pwned;") as root:
            repo = os.path.join(root, "repo;echo-injected")
            vault = os.path.join(root, "vault")
            os.makedirs(os.path.join(repo, "scripts"))
            os.makedirs(os.path.join(repo, "tests", "fixtures", "memory_runtime"))
            os.makedirs(vault)
            write_text(os.path.join(repo, "scripts", "probe.py"), "x = 1\n")
            runner = RecordingRunner()

            run_profile(
                "ci",
                repo_root=repo,
                cfg={"vault_path": vault},
                runner=runner,
            )

            self.assertTrue(runner.calls)
            for call in runner.calls:
                self.assertIsInstance(call["args"], tuple)
                self.assertTrue(all(isinstance(arg, str) for arg in call["args"]))
                self.assertGreater(call["timeout"], 0)
                self.assertLessEqual(call["timeout"], 600)
                self.assertEqual(call["cwd"], repo)
            self.assertFalse(os.path.exists(os.path.join(root, "pwned")))

    def test_timeout_is_reported_as_a_failed_required_check(self):
        with tempfile.TemporaryDirectory() as vault:
            report = run_profile(
                "ci",
                repo_root=REPO_ROOT,
                cfg={"vault_path": vault},
                runner=RecordingRunner(timeout_on="unittest"),
            )

            check = next(item for item in report.checks if item.name == "unit-tests")
            self.assertFalse(check.passed)
            self.assertIn("timeout", check.details.lower())
            self.assertFalse(report.passed)

    def test_ci_profile_requires_source_checkout_artifacts(self):
        with tempfile.TemporaryDirectory() as root:
            runtime = os.path.join(root, "runtime")
            vault = os.path.join(root, "vault")
            os.makedirs(os.path.join(runtime, "scripts"))
            os.makedirs(vault)
            runner = RecordingRunner()

            report = run_profile(
                "ci",
                repo_root=runtime,
                cfg={"vault_path": vault},
                runner=runner,
            )

            check = check_named(report, "source-checkout")
            self.assertFalse(check.passed)
            self.assertIn("source checkout", check.details)
            self.assertFalse(
                any("unittest" in call["args"] for call in runner.calls)
            )

    def test_quick_cli_loads_sync_only_producer_config(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            producer = {
                "enabled": True,
                "role": "producer-replica",
                "device_id": "windows-quick-doctor",
                "state_dir": str(root / "producer-state"),
                "outbox_dir": str(root / "outbox"),
                "received_published_dir": str(root / "received"),
                "replica_path": str(root / "replica"),
                "gc_retention_seconds": 604800,
            }
            initialize_producer(producer)
            config_path = root / "config.yaml"
            config_path.write_text(
                json.dumps({"beacon_sync": producer}),
                encoding="utf-8",
            )
            output = io.StringIO()
            runner = RecordingRunner()

            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "--profile",
                        "quick",
                        "--config",
                        str(config_path),
                        "--repo-root",
                        REPO_ROOT,
                        "--json",
                    ],
                    runner=runner,
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0, payload)
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(
                tuple(check["name"] for check in payload["checks"]),
                (
                    "configuration",
                    "beacon-sync",
                    "script-compilation",
                    "module-imports",
                ),
            )
            import_probe = next(
                call["args"][3]
                for call in runner.calls
                if len(call["args"]) > 3
                and call["args"][2] == "-c"
                and "sys.path.insert" in call["args"][3]
            )
            for module in (
                "beacon_sync_protocol",
                "beacon_sync_producer",
                "beacon_sync_snapshot",
                "beacon_sync",
                "install_beacon_sync",
                "install_runtime",
            ):
                self.assertIn(module, import_probe)
            self.assertNotIn("session_harvester", import_probe)

    def test_live_cli_preserves_sync_runtime_release_bindings(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = make_windows_producer_live_fixture(Path(temp))
            config_path = Path(cfg["runtime_root"], "scripts", "config.yaml")
            output = io.StringIO()

            with (
                patch.object(doctor, "_scheduler_platform", return_value="windows"),
                patch.object(
                    doctor,
                    "_windows_task_check",
                    return_value=DoctorCheck("windows-task", True, True, "ok"),
                ),
                contextlib.redirect_stdout(output),
            ):
                code = main(
                    [
                        "--profile",
                        "live",
                        "--config",
                        str(config_path),
                        "--repo-root",
                        REPO_ROOT,
                        "--json",
                    ],
                    runner=RecordingRunner(),
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0, payload)
            self.assertEqual(payload["status"], "pass")
            self.assertEqual(
                next(
                    check["passed"]
                    for check in payload["checks"]
                    if check["name"] == "runtime-release"
                ),
                True,
            )

    def test_quick_cli_does_not_treat_other_missing_vault_config_as_sync_only(self):
        with tempfile.TemporaryDirectory() as temp:
            config_path = Path(temp) / "config.yaml"
            config_path.write_text("{}\n", encoding="utf-8")
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "--profile",
                        "quick",
                        "--config",
                        str(config_path),
                        "--repo-root",
                        REPO_ROOT,
                        "--json",
                    ],
                    runner=RecordingRunner(),
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(payload["status"], "fail")
            self.assertEqual(len(payload["checks"]), 1)
            self.assertEqual(payload["checks"][0]["name"], "configuration")
            self.assertIn("vault_path", payload["checks"][0]["details"])

    def test_json_cli_output_is_machine_readable_and_exit_tracks_status(self):
        report = DoctorReport(
            profile="quick",
            checks=(
                DoctorCheck(
                    name="configuration",
                    required=True,
                    passed=True,
                    details="ok",
                ),
            ),
        )
        output = io.StringIO()
        with patch("doctor.run_profile", return_value=report):
            with contextlib.redirect_stdout(output):
                code = main(
                    ["--profile", "quick", "--json"],
                    config_loader=lambda: {"vault_path": "/tmp/vault"},
                )

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["profile"], "quick")
        self.assertEqual(payload["status"], "pass")
        self.assertEqual(payload["checks"][0]["name"], "configuration")

    def test_live_profile_accepts_only_runtime_owned_hooks_and_launchd_jobs(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            runner = LiveRecordingRunner(cfg)
            passing = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=runner,
            )
            self.assertTrue(check_named(passing, "codex-hooks").passed)
            self.assertTrue(check_named(passing, "launchd-plists").passed)
            prompt_probe = next(
                call["args"]
                for call in runner.calls
                if call["input_text"]
                == '{"hook_event_name":"DoctorProbe"}\n'
            )
            self.assertEqual(prompt_probe[1], "-B")

            hooks_path = os.path.join(cfg["codex_home"], "hooks.json")
            hooks = json.loads(read_text(hooks_path))
            hooks["hooks"]["Stop"][0]["hooks"][0]["command"] = (
                'AGENT_MEMORY_BEACON_HOOK=1 "/usr/bin/python3" -B '
                '"/tmp/outside/session_harvester.py" --mode stop --agent codex'
            )
            write_text(hooks_path, json.dumps(hooks))

            failing = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )
            self.assertFalse(check_named(failing, "codex-hooks").passed)
            self.assertIn("outside stable runtime", check_named(failing, "codex-hooks").details)

    def test_live_profile_rejects_loaded_service_bound_to_stale_runtime(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            label = "io.agent-memory-beacon.harvest"

            report = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg, stale_label=label),
            )

            check = check_named(report, "launchd-services")
            self.assertFalse(check.passed)
            self.assertIn("stable runtime", check.details)

    def test_live_profile_rejects_loaded_legacy_service(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            legacy = LEGACY_LAUNCHD_LABELS["weekly"]

            report = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg, loaded_legacy=legacy),
            )

            check = check_named(report, "launchd-services")
            self.assertFalse(check.passed)
            self.assertIn(legacy, check.details)
            self.assertIn("still loaded", check.details)

    def test_live_profile_requires_enabled_sync_plist_and_loaded_service(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            runtime_scripts = Path(os.path.realpath(cfg["runtime_root"])) / "scripts"
            write_text(runtime_scripts / "beacon_sync.py", "# sync fixture\n")
            config_path = runtime_scripts / "config.yaml"
            write_text(config_path, "beacon_sync:\n  enabled: true\n")
            sync_cfg = {
                "enabled": True,
                "role": "authority",
                "state_dir": str(Path(root) / "sync-state"),
                "published_dir": str(Path(root) / "sync-published"),
                "inboxes": [],
                "max_replica_object_bytes": 64 * 1024 * 1024,
            }
            cfg["beacon_sync"] = sync_cfg
            publish_generation(cfg, sync_cfg)

            missing_plist = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )
            self.assertFalse(check_named(missing_plist, "launchd-plists").passed)
            self.assertIn(
                install_beacon_sync.LAUNCHD_LABEL,
                check_named(missing_plist, "launchd-plists").details,
            )

            payload = install_beacon_sync.build_launchd_plist(
                python_path=cfg["python_path"],
                script_path=runtime_scripts / "beacon_sync.py",
                config_path=config_path,
                log_dir=Path(cfg["vault_path"]) / "04-Feedback" / "_logs",
            )
            plist_path = (
                Path(cfg["launch_agents_dir"])
                / f"{install_beacon_sync.LAUNCHD_LABEL}.plist"
            )
            plist_path.write_bytes(plistlib.dumps(payload))

            missing_service = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(
                    cfg,
                    missing_label=install_beacon_sync.LAUNCHD_LABEL,
                ),
            )
            self.assertTrue(check_named(missing_service, "launchd-plists").passed)
            self.assertFalse(
                check_named(missing_service, "launchd-services").passed
            )

            healthy = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )
            self.assertTrue(check_named(healthy, "launchd-plists").passed)
            self.assertTrue(check_named(healthy, "launchd-services").passed)

    def test_windows_live_profile_validates_owned_task_without_launchd(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            runtime_scripts = Path(os.path.realpath(cfg["runtime_root"])) / "scripts"
            write_text(runtime_scripts / "beacon_sync.py", "# sync fixture\n")
            write_text(runtime_scripts / "config.yaml", "beacon_sync:\n  enabled: true\n")
            task_xml = install_beacon_sync.build_windows_task_xml(
                python_path=cfg["python_path"],
                script_path=runtime_scripts / "beacon_sync.py",
                config_path=runtime_scripts / "config.yaml",
                user_id="WORKSTATION\\demo",
                interval_minutes=5,
            )
            runner = WindowsTaskRecordingRunner(task_xml)

            with patch(
                "doctor._scheduler_platform",
                return_value="windows",
                create=True,
            ), patch(
                "doctor._windows_task_user",
                return_value="WORKSTATION\\demo",
                create=True,
            ):
                checks = _live_checks(REPO_ROOT, cfg, runner)

            task = next(item for item in checks if item.name == "windows-task")
            self.assertTrue(task.passed, task.details)
            self.assertFalse(
                any(call["args"][0] == "/bin/launchctl" for call in runner.calls)
            )
            task_calls = [
                call["args"]
                for call in runner.calls
                if call["args"][0].lower().endswith("schtasks.exe")
            ]
            self.assertEqual(len(task_calls), 2)
            self.assertIn("/XML", task_calls[0])
            self.assertIn("/V", task_calls[1])

    def test_windows_task_accepts_success_and_never_run_last_results(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            runtime_scripts = Path(cfg["runtime_root"]) / "scripts"
            task_xml = install_beacon_sync.build_windows_task_xml(
                python_path=cfg["python_path"],
                script_path=runtime_scripts / "beacon_sync.py",
                config_path=runtime_scripts / "config.yaml",
                user_id="S-1-5-21-100-200-300-1001",
            )

            for last_result in ("0", "267011", "0x41303"):
                with self.subTest(last_result=last_result), patch(
                    "doctor._windows_task_user",
                    return_value="S-1-5-21-100-200-300-1001",
                ):
                    check = doctor._windows_task_check(
                        cfg,
                        REPO_ROOT,
                        cfg["runtime_root"],
                        WindowsTaskRecordingRunner(
                            task_xml,
                            last_result=last_result,
                        ),
                    )
                self.assertTrue(check.passed, check.details)

    def test_windows_task_rejects_failed_last_result(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            runtime_scripts = Path(cfg["runtime_root"]) / "scripts"
            task_xml = install_beacon_sync.build_windows_task_xml(
                python_path=cfg["python_path"],
                script_path=runtime_scripts / "beacon_sync.py",
                config_path=runtime_scripts / "config.yaml",
                user_id="S-1-5-21-100-200-300-1001",
            )
            with patch(
                "doctor._windows_task_user",
                return_value="S-1-5-21-100-200-300-1001",
            ):
                check = doctor._windows_task_check(
                    cfg,
                    REPO_ROOT,
                    cfg["runtime_root"],
                    WindowsTaskRecordingRunner(task_xml, last_result="1"),
                )

            self.assertFalse(check.passed)
            self.assertIn("Last Result", check.details)

    def test_windows_live_profile_rejects_unowned_or_wrong_task(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            runtime_scripts = Path(cfg["runtime_root"]) / "scripts"
            write_text(runtime_scripts / "beacon_sync.py", "# sync fixture\n")
            write_text(runtime_scripts / "config.yaml", "beacon_sync:\n  enabled: true\n")
            task_xml = install_beacon_sync.build_windows_task_xml(
                python_path=cfg["python_path"],
                script_path=runtime_scripts / "beacon_sync.py",
                config_path=runtime_scripts / "config.yaml",
                user_id="WORKSTATION\\demo",
            ).replace(
                install_beacon_sync.WINDOWS_TASK_OWNER_DESCRIPTION,
                "Third-party task using the same name",
            )
            runner = WindowsTaskRecordingRunner(task_xml)

            with patch(
                "doctor._scheduler_platform",
                return_value="windows",
                create=True,
            ), patch(
                "doctor._windows_task_user",
                return_value="WORKSTATION\\demo",
                create=True,
            ):
                checks = _live_checks(REPO_ROOT, cfg, runner)

            task = next(item for item in checks if item.name == "windows-task")
            self.assertFalse(task.passed)
            self.assertIn("definition", task.details)

    def test_live_profile_rejects_owned_hook_with_appended_shell_command(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            hooks_path = os.path.join(cfg["codex_home"], "hooks.json")
            hooks = json.loads(read_text(hooks_path))
            hook = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]
            hook["command"] += " ; /usr/bin/false"
            write_json(hooks_path, hooks)

            report = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )

            check = check_named(report, "codex-hooks")
            self.assertFalse(check.passed)
            self.assertIn("exact command", check.details)

    def test_live_profile_rejects_owned_hook_with_wrong_type(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            hooks_path = os.path.join(cfg["codex_home"], "hooks.json")
            hooks = json.loads(read_text(hooks_path))
            hooks["hooks"]["Stop"][0]["hooks"][0]["type"] = "prompt"
            write_json(hooks_path, hooks)

            report = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )

            check = check_named(report, "codex-hooks")
            self.assertFalse(check.passed)
            self.assertIn("type is not command", check.details)

    def test_live_profile_rejects_full_weekly_job(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            path = os.path.join(
                cfg["launch_agents_dir"],
                "io.agent-memory-beacon.weekly.plist",
            )
            with open(path, "rb") as handle:
                payload = plistlib.load(handle)
            payload["ProgramArguments"].append("--full")
            with open(path, "wb") as handle:
                plistlib.dump(payload, handle)

            report = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )

            check = check_named(report, "launchd-plists")
            self.assertFalse(check.passed)
            self.assertIn("--full", check.details)

    def test_live_profile_derives_launch_agents_from_configured_user_home(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            configured_home = os.path.join(root, "configured-home")
            derived_directory = os.path.join(
                configured_home,
                "Library",
                "LaunchAgents",
            )
            os.makedirs(os.path.dirname(derived_directory), exist_ok=True)
            os.replace(cfg["launch_agents_dir"], derived_directory)
            cfg["user_home"] = configured_home
            cfg.pop("launch_agents_dir")

            report = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )

            self.assertTrue(check_named(report, "launchd-plists").passed)

    def test_live_profile_rejects_wrong_weekly_script(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            path = os.path.join(
                cfg["launch_agents_dir"],
                "io.agent-memory-beacon.weekly.plist",
            )
            with open(path, "rb") as handle:
                payload = plistlib.load(handle)
            payload["ProgramArguments"][1] = os.path.join(
                cfg["runtime_root"],
                "scripts",
                "session_harvester.py",
            )
            with open(path, "wb") as handle:
                plistlib.dump(payload, handle)

            report = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )

            check = check_named(report, "launchd-plists")
            self.assertFalse(check.passed)
            self.assertIn("unexpected script", check.details)

    def test_live_profile_rejects_candidate_path_leaking_into_recall_index(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            cfg["insight_memory"] = {
                "candidate_dir": "05-Agent-Memory/private-insight-candidates"
            }
            index_path = os.path.join(
                cfg["vault_path"], "05-Agent-Memory", "recall-index.json"
            )
            write_json(
                index_path,
                {
                    "schema_version": "2.0",
                    "units": [
                        {
                            "id": "candidate-leak",
                            "status": "active",
                            "path": "05-Agent-Memory/private-insight-candidates/leak",
                        }
                    ],
                },
            )

            report = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )

            check = check_named(report, "candidate-isolation")
            self.assertFalse(check.passed)
            self.assertIn("candidate-leak", check.details)

    def test_live_profile_rejects_promotion_proposal_leaking_into_recall_index(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            cfg["memory_promotion"] = {
                "proposal_dir": "05-Agent-Memory/private-promotion-proposals"
            }
            index_path = os.path.join(
                cfg["vault_path"], "05-Agent-Memory", "recall-index.json"
            )
            write_json(
                index_path,
                {
                    "schema_version": "2.0",
                    "units": [
                        {
                            "id": "promotion-proposal-leak",
                            "status": "active",
                            "path": "05-Agent-Memory/private-promotion-proposals/leak",
                        }
                    ],
                },
            )

            report = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )

            check = check_named(report, "candidate-isolation")
            self.assertFalse(check.passed)
            self.assertIn("promotion-proposal-leak", check.details)

    def test_default_profiles_do_not_mutate_vault_or_binding_files(self):
        with tempfile.TemporaryDirectory() as root:
            cfg = make_live_fixture(root)
            before = tree_digest(root)

            run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )

            self.assertEqual(tree_digest(root), before)


def make_authority_doctor_fixture(root):
    root = Path(root)
    vault = root / "vault"
    vault.mkdir()
    (vault / "memory.md").write_text("memory\n", encoding="utf-8")
    sync = {
        "enabled": True,
        "role": "authority",
        "state_dir": str(root / "state"),
        "published_dir": str(root / "published"),
        "inboxes": [],
        "max_replica_object_bytes": 64 * 1024 * 1024,
    }
    publish_generation({"vault_path": str(vault)}, sync)
    return vault, sync


def insert_doctor_event(
    connection,
    *,
    status,
    processed_at,
    canonical_generation=None,
    generation_id="",
):
    connection.execute(
        """
        insert into events(
            producer_instance_id, seq, event_id, event_sha256, device_id,
            status, code, bundle_path, event_kind, stream_id, stream_epoch,
            cursor_start, cursor_end, created_at, processed_at,
            canonical_generation, generation_id
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "producer",
            1,
            "event-doctor-1",
            "a" * 64,
            "windows-test",
            status,
            "test",
            "bundle",
            "transcript.chunk",
            "stream",
            "epoch",
            0,
            1,
            processed_at,
            processed_at,
            canonical_generation,
            generation_id,
        ),
    )


def make_attachment_producer(root, *, max_events_per_run):
    root = Path(root)
    sessions = root / "sessions"
    attachments = root / "attachments"
    sessions.mkdir()
    attachments.mkdir()
    image = attachments / "doctor-evidence.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\ndoctor-attachment")
    transcript = sessions / "attachment-session.jsonl"
    transcript.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "doctor-attachment-session",
                            "cwd": str(attachments),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "检查附件",
                            "local_images": [str(image)],
                            "local_audio": [],
                            "images": [],
                            "text_elements": [],
                            "client_id": "doctor-test",
                        },
                    }
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )
    producer = {
        "enabled": True,
        "role": "producer-replica",
        "device_id": "windows-doctor",
        "state_dir": str(root / "producer-state"),
        "outbox_dir": str(root / "outbox"),
        "received_published_dir": str(root / "received"),
        "replica_path": str(root / "replica"),
        "transcript_paths": [str(sessions)],
        "attachment_roots": [str(attachments)],
        "max_chunk_bytes": 4096,
        "max_gap_bytes": 4096,
        "max_attachment_bytes": 4096,
        "max_object_bytes": 4096,
        "max_events_per_run": max_events_per_run,
        "gc_retention_seconds": 604800,
    }
    collect_transcripts(producer, include_existing=True)
    return producer


def reduce_attachment_to_authority(root):
    root = Path(root)
    producer = make_attachment_producer(root, max_events_per_run=32)
    vault = root / "vault"
    (vault / "04-Feedback" / "_logs").mkdir(parents=True)
    authority = {
        "enabled": True,
        "role": "authority",
        "state_dir": str(root / "authority-state"),
        "published_dir": str(root / "published"),
        "inboxes": [
            {
                "device_id": producer["device_id"],
                "path": producer["outbox_dir"],
            }
        ],
        "max_attachment_bytes": 4096,
        "max_object_bytes": 4096,
        "max_replica_object_bytes": 64 * 1024 * 1024,
    }
    cfg = {"vault_path": str(vault)}
    reduce_inboxes(
        cfg,
        authority,
        harvest_adapter=lambda _cfg, paths: {
            str(path): True for path in paths
        },
    )
    generation = publish_generation(cfg, authority)
    publish_pending_receipts(authority, generation)
    return producer, authority, vault


def materialize_runtime_release(plan, *, windows=False):
    install_root = plan.install_root
    if windows:
        install_root = plan.install_root.parent / ".doctor.staging-fixture"
    for item in plan.files:
        path = install_root / item.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item.content)
        path.chmod(item.mode)
    (install_root / "release-manifest.json").write_bytes(
        plan.manifest_bytes
    )
    python_path = install_root / ".venv" / (
        Path("Scripts/python.exe") if windows else Path("bin/python")
    )
    if windows:
        base_python = plan.install_root.parent / ".doctor-base-python"
        base_python.mkdir(parents=True)
        (base_python / "python.exe").write_bytes(b"MZ doctor base python\n")
        (base_python / "python3.dll").write_bytes(b"MZ doctor Python ABI\n")
        (base_python / "Lib").mkdir()
        (base_python / "DLLs").mkdir()
        python_path.parent.mkdir(parents=True, exist_ok=True)
        python_path.write_bytes(b"MZ doctor venv launcher\n")
        python_path.chmod(0o700)
        site_packages = install_root / ".venv" / "Lib" / "site-packages"
        site_packages.mkdir(parents=True)
        (install_root / ".venv" / "pyvenv.cfg").write_text(
            "\n".join(
                (
                    f"home = {base_python}",
                    "include-system-site-packages = false",
                    "version = 3.13.0",
                    f"executable = {base_python / 'python.exe'}",
                    (
                        f"command = {base_python / 'python.exe'} -m venv "
                        f"--copies --without-pip {install_root / '.venv'}"
                    ),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        finalized = install_runtime._finalize_windows_staged_runtime(
            plan,
            install_root,
        )
        install_root.rename(finalized.install_root)
        return finalized
    else:
        write_text(python_path, "#!/bin/sh\nexit 0\n")
    python_path.chmod(0o700)
    return plan


def make_windows_producer_live_fixture(root):
    root = Path(root)
    producer = {
        "enabled": True,
        "role": "producer-replica",
        "device_id": "windows-live",
        "state_dir": str(root / "producer-state"),
        "outbox_dir": str(root / "outbox"),
        "received_published_dir": str(root / "received"),
        "replica_path": str(root / "replica"),
        "gc_retention_seconds": 604800,
    }
    initialize_producer(producer)
    plan = install_runtime.build_windows_sync_release_plan(
        REPO_ROOT,
        root / "windows-runtime",
        {
            "python_path": str(Path(sys.executable).resolve()),
            "beacon_sync": producer,
        },
    )
    plan = materialize_runtime_release(plan, windows=True)
    return plan.cfg


def make_live_fixture(root):
    vault = os.path.join(root, "vault")
    runtime = os.path.join(root, "runtime")
    codex_home = os.path.join(root, ".codex")
    launch_agents = os.path.join(root, "LaunchAgents")
    python_path = os.path.join(runtime, ".venv", "bin", "python")
    scripts = os.path.join(runtime, "scripts")
    os.makedirs(os.path.dirname(python_path), exist_ok=True)
    os.makedirs(scripts, exist_ok=True)
    os.makedirs(codex_home, exist_ok=True)
    os.makedirs(launch_agents, exist_ok=True)
    os.makedirs(os.path.join(vault, "05-Agent-Memory"), exist_ok=True)
    write_text(python_path, "#!/bin/sh\n")
    for filename in ("session_harvester.py", "codex_prompt_hook.py", "runner.py"):
        write_text(os.path.join(scripts, filename), "# runtime fixture\n")
    plan = install_runtime.build_release_plan(
        REPO_ROOT,
        runtime,
        {
            "python_path": sys.executable,
            "vault_path": vault,
        },
    )
    materialize_runtime_release(plan)
    generation_id = "doctor-fixture-generation"
    index_payload = {
        "schema_version": "2.0",
        "generation_id": generation_id,
        "units": [],
    }
    graph_payload = {
        "schema_version": "3.0",
        "generated_by": "test",
        "generated_at": "2026-07-26T10:00:00+08:00",
        "generation_id": generation_id,
        "nodes": [],
        "edges": [],
    }
    write_json(
        os.path.join(vault, "05-Agent-Memory", "recall-index.json"),
        index_payload,
    )
    write_json(
        os.path.join(vault, "05-Agent-Memory", "memory-graph.json"),
        graph_payload,
    )
    write_text(
        os.path.join(vault, "05-Agent-Memory", "memory-graph-quality.md"),
        render_memory_graph_quality_markdown(graph_payload, []),
    )

    def command(script, suffix=""):
        return (
            f'AGENT_MEMORY_BEACON_HOOK=1 "{python_path}" -B '
            f'"{os.path.join(scripts, script)}"{suffix}'
        )

    hooks = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "all",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command("session_harvester.py", " --mode start"),
                            "timeout": 120,
                        }
                    ],
                }
            ],
            "UserPromptSubmit": [
                {
                    "matcher": "all",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command("codex_prompt_hook.py"),
                            "timeout": 2,
                        }
                    ],
                }
            ],
            "Stop": [
                {
                    "matcher": "all",
                    "hooks": [
                        {
                            "type": "command",
                            "command": command(
                                "session_harvester.py", " --mode stop --agent codex"
                            ),
                            "timeout": 120,
                        }
                    ],
                }
            ],
        }
    }
    write_json(os.path.join(codex_home, "hooks.json"), hooks)

    jobs = (
        (
            "io.agent-memory-beacon.harvest",
            [python_path, os.path.join(scripts, "session_harvester.py"), "--mode", "start"],
        ),
        (
            "io.agent-memory-beacon.weekly",
            [python_path, os.path.join(scripts, "runner.py")],
        ),
    )
    for label, arguments in jobs:
        path = os.path.join(launch_agents, label + ".plist")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            plistlib.dump(
                {
                    "Label": label,
                    "ProgramArguments": arguments,
                    "WorkingDirectory": scripts,
                    "EnvironmentVariables": {
                        "PYTHONDONTWRITEBYTECODE": "1",
                    },
                },
                handle,
            )
    return {
        "vault_path": vault,
        "codex_home": codex_home,
        "runtime_root": runtime,
        "launch_agents_dir": launch_agents,
        "python_path": python_path,
    }


def check_named(report, name):
    matches = [item for item in report.checks if item.name == name]
    if not matches:
        raise AssertionError(f"missing Doctor check: {name}")
    if len(matches) != 1:
        raise AssertionError(f"duplicate Doctor check: {name}")
    return matches[0]


def write_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode("utf-8"))
            digest.update(path.read_bytes())
    return digest.hexdigest()


if __name__ == "__main__":
    unittest.main()
