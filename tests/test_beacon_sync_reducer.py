import hashlib
import json
import multiprocessing
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from beacon_sync_producer import collect_transcripts, initialize_producer
from beacon_sync_protocol import (
    build_ready,
    canonical_json_bytes,
    derive_event_id,
)
from beacon_sync_reducer import (
    ReducerError,
    list_ledger_events,
    reduce_inboxes,
)
import beacon_sync_reducer
import beacon_sync_snapshot
from beacon_sync_snapshot import (
    publish_generation,
    publish_pending_receipts,
)


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


def _ledger_writer_worker(state_dir, key, opened, release, results):
    connection = None
    try:
        connection = beacon_sync_reducer._open_ledger(Path(state_dir))
        opened.set()
        if not release.wait(timeout=10):
            raise TimeoutError("ledger writer was not released")
        connection.execute(
            "insert into metadata(key, value) values (?, ?)",
            (key, "written"),
        )
        connection.commit()
        results.put(("ok", key))
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))
    finally:
        if connection is not None:
            connection.close()


def _first_create_worker(
    state_dir,
    key,
    reached_publish,
    release_publish,
    results,
):
    connection = None
    real_atomic_write = beacon_sync_reducer.portable_atomic_write
    blocked = False

    def blocked_first_publish(path, data, *, root, mode=0o600):
        nonlocal blocked
        if Path(path).name == "ledger.sqlite3" and not blocked:
            blocked = True
            reached_publish.set()
            if not release_publish.wait(timeout=10):
                raise TimeoutError("first ledger publish was not released")
        return real_atomic_write(path, data, root=root, mode=mode)

    try:
        with patch.object(
            beacon_sync_reducer,
            "portable_atomic_write",
            side_effect=blocked_first_publish,
        ):
            connection = beacon_sync_reducer._open_ledger(Path(state_dir))
            connection.execute(
                "insert into metadata(key, value) values (?, ?)",
                (key, "created"),
            )
            connection.commit()
        results.put(("ok", key))
    except Exception as exc:
        results.put(("error", type(exc).__name__, str(exc)))
    finally:
        if connection is not None:
            connection.close()


def _leave_wal_worker(path):
    connection = sqlite3.connect(path)
    connection.execute("pragma journal_mode = WAL")
    connection.execute("pragma wal_autocheckpoint = 0")
    connection.execute(
        "insert into metadata(key, value) values ('wal-visible', 'yes')"
    )
    connection.commit()
    os._exit(0)


def _leave_hot_journal_worker(path):
    connection = sqlite3.connect(path)
    connection.execute("pragma journal_mode = DELETE")
    connection.execute("pragma synchronous = FULL")
    connection.execute("pragma cache_size = 1")
    connection.execute("pragma cache_spill = ON")
    connection.execute("begin immediate")
    connection.execute(
        "update metadata set value = '999' where key = 'schema_version'"
    )
    for index in range(512):
        connection.execute(
            "update metadata set value = ? where key = ?",
            ("y" * 1024, f"seed-{index:04d}"),
        )
    connection.execute(
        "insert into metadata(key, value) values ('uncommitted', 'yes')"
    )
    os._exit(0)


class CountingScandir:
    def __init__(self, iterator, counter):
        self.iterator = iterator
        self.counter = counter

    def __enter__(self):
        self.iterator.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self.iterator.__exit__(exc_type, exc_value, traceback)

    def __iter__(self):
        return self

    def __next__(self):
        entry = next(self.iterator)
        self.counter["yielded"] += 1
        return entry


class RecordingHarvester:
    def __init__(self, changed=True, fail_times=0):
        self.changed = changed
        self.fail_times = fail_times
        self.calls = []

    def __call__(self, cfg, mirror_paths):
        self.calls.append(
            {
                str(path): Path(path).read_bytes()
                for path in sorted(mirror_paths, key=str)
            }
        )
        if self.fail_times:
            self.fail_times -= 1
            raise RuntimeError("injected index rebuild failure")
        return {str(path): self.changed for path in mirror_paths}


class BeaconSyncReducerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.attachments = self.root / "attachments"
        self.attachments.mkdir()
        self.producer_state = self.root / "producer-state"
        self.outbox = self.root / "outbox"
        self.authority_state = self.root / "authority-state"
        self.vault = self.root / "vault"
        (self.vault / "04-Feedback" / "_logs").mkdir(parents=True)
        self.producer_config = {
            "device_id": "windows-gpu-test",
            "state_dir": str(self.producer_state),
            "outbox_dir": str(self.outbox),
            "transcript_paths": [str(self.sessions)],
            "codex_sessions_path": "",
            "claude_project_path": "",
            "attachment_roots": [str(self.attachments)],
            "max_chunk_bytes": 4096,
            "max_gap_bytes": 4096,
            "max_attachment_bytes": 4096,
            "max_events_per_run": 32,
        }
        self.sync_config = {
            "state_dir": str(self.authority_state),
            "inboxes": [
                {
                    "device_id": "windows-gpu-test",
                    "path": str(self.outbox),
                }
            ],
            "max_event_json_bytes": 128 * 1024,
            "max_object_bytes": 32 * 1024 * 1024,
            "max_attachment_bytes": 4096,
            "max_events_per_run": 32,
        }
        self.cfg = {"vault_path": str(self.vault)}

    def tearDown(self):
        self.temp.cleanup()

    def test_applies_trusted_event_and_records_pending_publish(self):
        self._produce(["[DECISION:使用单一 authority| context:避免冲突]"])
        harvester = RecordingHarvester()

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )
        rows = list_ledger_events(self.sync_config)

        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["pending_publish"], 1)
        self.assertEqual(len(harvester.calls), 1)
        mirror_bytes = next(iter(harvester.calls[0].values()))
        self.assertIn(b"session_meta", mirror_bytes)
        self.assertIn(b"DECISION", mirror_bytes)
        self.assertEqual(rows[0]["status"], "applied_pending_publish")
        self.assertEqual(rows[0]["seq"], 1)

    def test_duplicate_ready_event_does_not_reharvest(self):
        self._produce(["same event"])
        harvester = RecordingHarvester()
        reduce_inboxes(self.cfg, self.sync_config, harvest_adapter=harvester, now=NOW)

        second = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )

        self.assertEqual(second["applied"], 0)
        self.assertEqual(second["noop"], 0)
        self.assertEqual(len(harvester.calls), 1)

    def test_higher_sequence_waits_for_missing_sequence(self):
        transcript = self._produce(["first"])
        self._append(transcript, "second")
        collect_transcripts(self.producer_config, now=NOW)
        bundles = self._bundles()
        first_ready = bundles[0] / "ready.json"
        hidden = bundles[0] / "ready.hidden"
        first_ready.rename(hidden)
        harvester = RecordingHarvester()

        deferred = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )
        self.assertEqual(deferred["deferred"], 1)
        self.assertEqual(harvester.calls, [])

        hidden.rename(first_ready)
        applied = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )
        self.assertEqual(applied["applied"], 2)
        self.assertEqual(len(harvester.calls), 1)

    def test_missing_sequence_does_not_read_unbounded_future_bundles(self):
        self._produce(["sequence one"])
        producer = json.loads(
            (self.outbox / "v1" / "identity.json").read_text(encoding="utf-8")
        )["producer_instance_id"]
        shutil.rmtree(self._bundles()[0])
        producer_root = self.outbox / "v1" / "events" / producer
        for seq in range(2, 202):
            bundle = producer_root / (
                f"{seq:020d}-event-{seq:064x}"
            )
            bundle.mkdir(parents=True)
            (bundle / "ready.json").write_text("{}\n", encoding="utf-8")

        with patch(
            "beacon_sync_reducer._load_candidate",
            side_effect=FileNotFoundError,
        ) as loader:
            result = reduce_inboxes(
                self.cfg,
                self.sync_config,
                harvest_adapter=RecordingHarvester(),
                now=NOW,
            )

        self.assertLessEqual(loader.call_count, 1)
        self.assertGreaterEqual(result["deferred"], 1)

    def test_missing_object_stays_deferred_without_advancing_sequence(self):
        self._produce(["object pending"])
        bundle = self._bundles()[0]
        event = self._event(bundle)
        (bundle / "objects" / event["payload"]["sha256"]).unlink()

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )

        self.assertEqual(result["deferred"], 1)
        self.assertEqual(list_ledger_events(self.sync_config), [])

    def test_payload_hash_conflict_blocks_stream_and_never_harvests(self):
        self._produce(["hash conflict"])
        bundle = self._bundles()[0]
        event = self._event(bundle)
        payload = bundle / "objects" / event["payload"]["sha256"]
        payload.write_bytes(b"tampered\n")
        harvester = RecordingHarvester()

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )

        self.assertEqual(result["blocked"], 1)
        self.assertEqual(harvester.calls, [])
        self.assertTrue(list(self.authority_state.rglob("*.json")))

    def test_wrong_device_is_quarantined(self):
        self._produce(["wrong device"])
        self.sync_config["inboxes"][0]["device_id"] = "another-device"

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )

        self.assertEqual(result["quarantined"], 1)
        self.assertEqual(list_ledger_events(self.sync_config), [])

    def test_event_producer_must_match_inbox_identity(self):
        self._produce(["wrong producer instance"])
        identity_path = self.outbox / "v1" / "identity.json"
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        identity["producer_instance_id"] = str(uuid.uuid4())
        identity_path.write_bytes(canonical_json_bytes(identity))
        harvester = RecordingHarvester()

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )

        self.assertEqual(result["quarantined"], 1)
        self.assertEqual(harvester.calls, [])
        self.assertEqual(list_ledger_events(self.sync_config), [])

    def test_producer_uuid_remains_bound_to_first_device(self):
        transcript = self._produce(["owner sequence one"])
        harvester = RecordingHarvester()
        reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )
        producer = list_ledger_events(self.sync_config)[0][
            "producer_instance_id"
        ]
        self._append(transcript, "owner sequence two")
        collect_transcripts(self.producer_config, now=NOW)

        foreign_outbox = self.root / "foreign-outbox"
        shutil.copytree(self.outbox, foreign_outbox)
        self._retarget_outbox_device(
            foreign_outbox,
            producer,
            "windows-gpu-foreign",
            seq=2,
        )
        self.sync_config["inboxes"].append(
            {
                "device_id": "windows-gpu-foreign",
                "path": str(foreign_outbox),
            }
        )

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )
        rows = list_ledger_events(self.sync_config)
        connection = sqlite3.connect(self.authority_state / "ledger.sqlite3")
        owner = connection.execute(
            """
            select device_id, next_seq, blocked_code
              from producers
             where producer_instance_id = ?
            """,
            (producer,),
        ).fetchone()
        connection.close()

        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["quarantined"], 1)
        self.assertEqual([row["seq"] for row in rows], [1, 2])
        self.assertTrue(
            all(row["device_id"] == "windows-gpu-test" for row in rows)
        )
        self.assertEqual(owner, ("windows-gpu-test", 3, ""))

    def test_producer_uuid_case_alias_cannot_bind_second_device(self):
        transcript = self._produce(["canonical owner"])
        harvester = RecordingHarvester()
        reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )
        producer = list_ledger_events(self.sync_config)[0][
            "producer_instance_id"
        ]
        self._append(transcript, "canonical continuation")
        collect_transcripts(self.producer_config, now=NOW)

        alias = producer.upper()
        foreign_outbox = self.root / "foreign-case-alias"
        shutil.copytree(self.outbox, foreign_outbox)
        identity_paths = [foreign_outbox / "v1" / "identity.json"]
        registry = foreign_outbox / "v1" / "identities"
        identity_paths.extend(sorted(registry.glob("*.json")))
        for identity_path in identity_paths:
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
            identity["device_id"] = "windows-gpu-foreign"
            identity["producer_instance_id"] = alias
            identity_path.write_bytes(canonical_json_bytes(identity))
            if identity_path.parent == registry:
                identity_path.rename(registry / f"{alias}.json")

        events_root = foreign_outbox / "v1" / "events"
        producer_root = events_root / producer
        alias_root = events_root / alias
        producer_root.rename(alias_root)
        for bundle in sorted(alias_root.iterdir()):
            event_path = bundle / "event.json"
            ready_path = bundle / "ready.json"
            if not event_path.is_file() or not ready_path.is_file():
                continue
            event = json.loads(event_path.read_text(encoding="utf-8"))
            event["device_id"] = "windows-gpu-foreign"
            event["producer_instance_id"] = alias
            event["event_id"] = derive_event_id(event)
            event_bytes = canonical_json_bytes(event)
            event_path.write_bytes(event_bytes)
            ready = json.loads(ready_path.read_text(encoding="utf-8"))
            ready["device_id"] = "windows-gpu-foreign"
            ready["producer_instance_id"] = alias
            ready["event_id"] = event["event_id"]
            ready["event_sha256"] = hashlib.sha256(event_bytes).hexdigest()
            ready_path.write_bytes(canonical_json_bytes(ready))
            bundle.rename(
                alias_root / f"{event['seq']:020d}-{event['event_id']}"
            )

        self.sync_config["inboxes"].append(
            {
                "device_id": "windows-gpu-foreign",
                "path": str(foreign_outbox),
            }
        )
        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )
        rows = list_ledger_events(self.sync_config)

        self.assertEqual(result["applied"], 1)
        self.assertEqual(result["quarantined"], 1)
        self.assertEqual({row["producer_instance_id"] for row in rows}, {producer})
        self.assertEqual([row["seq"] for row in rows], [1, 2])

    def test_identity_registry_scan_stops_at_configured_limit(self):
        self._produce(["identity registry limit"])
        registry = self.outbox / "v1" / "identities"
        for index in range(20):
            (registry / f"extra-{index:03d}.json").write_text(
                "{}\n",
                encoding="utf-8",
            )
        counter = {"yielded": 0}
        real_scandir = os.scandir

        def counted_scandir(path):
            iterator = real_scandir(path)
            if Path(path) == registry:
                return CountingScandir(iterator, counter)
            return iterator

        with (
            patch.object(
                beacon_sync_reducer,
                "MAX_IDENTITY_REGISTRY_ENTRIES",
                8,
                create=True,
            ),
            patch.object(
                beacon_sync_reducer.os,
                "scandir",
                side_effect=counted_scandir,
            ),
        ):
            producers = beacon_sync_reducer._identity_producers(
                self.outbox,
                "windows-gpu-test",
            )

        self.assertIsNone(producers)
        self.assertLessEqual(counter["yielded"], 9)

    def test_unexpected_producer_scan_does_not_materialize_events_root(self):
        self._produce(["unexpected producer limit"])
        events_root = self.outbox / "v1" / "events"
        for index in range(20):
            (events_root / f"unexpected-{index:03d}").mkdir()
        counter = {"yielded": 0}
        real_scandir = os.scandir

        def counted_scandir(path):
            iterator = real_scandir(path)
            if Path(path) == events_root:
                return CountingScandir(iterator, counter)
            return iterator

        with (
            patch.object(
                beacon_sync_reducer,
                "MAX_IDENTITY_REGISTRY_ENTRIES",
                8,
            ),
            patch.object(
                beacon_sync_reducer.os,
                "scandir",
                side_effect=counted_scandir,
            ),
        ):
            result = reduce_inboxes(
                self.cfg,
                self.sync_config,
                harvest_adapter=RecordingHarvester(),
                now=NOW,
            )

        self.assertEqual(result["quarantined"], 1)
        self.assertLessEqual(counter["yielded"], 9)
        self.assertEqual(list_ledger_events(self.sync_config), [])

    def test_partial_bundle_scan_stops_at_limit_and_quarantines_backlog(self):
        self._produce(["partial bundle limit"])
        producer = self._event(self._bundles()[0])["producer_instance_id"]
        first_bundle = self._bundles()[0]
        sequence_root = first_bundle.parent
        shutil.rmtree(first_bundle)
        if sequence_root.name.startswith("seq-"):
            sequence_root.rmdir()
        producer_root = self.outbox / "v1" / "events" / producer
        for seq in range(2, 22):
            (producer_root / f"{seq:020d}-event-{seq:064x}").mkdir()
        counter = {"yielded": 0}
        real_scandir = os.scandir

        def counted_scandir(path):
            iterator = real_scandir(path)
            if Path(path) == producer_root:
                return CountingScandir(iterator, counter)
            return iterator

        with (
            patch.object(
                beacon_sync_reducer,
                "MAX_BUNDLE_ENTRIES_PER_PRODUCER",
                8,
                create=True,
            ),
            patch.object(
                beacon_sync_reducer.os,
                "scandir",
                side_effect=counted_scandir,
            ),
        ):
            result = reduce_inboxes(
                self.cfg,
                self.sync_config,
                harvest_adapter=RecordingHarvester(),
                now=NOW,
            )

        self.assertEqual(result["deferred"], 1)
        self.assertEqual(result["quarantined"], 1)
        self.assertLessEqual(counter["yielded"], 9)
        self.assertEqual(list_ledger_events(self.sync_config), [])

    def test_bundle_backlog_over_scan_limit_eventually_advances(self):
        transcript = self._produce(["backlog sequence 1"])
        for seq in range(2, 7):
            self._append(transcript, f"backlog sequence {seq}")
            collect_transcripts(self.producer_config, now=NOW)

        results = []
        with patch.object(
            beacon_sync_reducer,
            "MAX_BUNDLE_ENTRIES_PER_PRODUCER",
            2,
        ):
            for _round in range(6):
                result = reduce_inboxes(
                    self.cfg,
                    self.sync_config,
                    harvest_adapter=RecordingHarvester(),
                    now=NOW,
                )
                results.append(result)
                if result["applied"]:
                    break

        self.assertTrue(any(result["applied"] for result in results))
        self.assertEqual(
            [row["seq"] for row in list_ledger_events(self.sync_config)],
            [1, 2, 3, 4, 5, 6],
        )

    def test_malformed_event_in_one_inbox_does_not_abort_other_inbox(self):
        self._produce(["valid peer inbox"])
        good_outbox = self.root / "good-outbox"
        shutil.copytree(self.outbox, good_outbox)
        bundle = self._bundles()[0]
        event = self._event(bundle)
        event["event_kind"] = []
        event_bytes = canonical_json_bytes(event)
        (bundle / "event.json").write_bytes(event_bytes)
        ready_path = bundle / "ready.json"
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        ready["event_sha256"] = hashlib.sha256(event_bytes).hexdigest()
        ready_path.write_bytes(canonical_json_bytes(ready))
        self.sync_config["inboxes"].append(
            {
                "device_id": "windows-gpu-test",
                "path": str(good_outbox),
            }
        )

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )

        self.assertEqual(result["blocked"], 1)
        self.assertEqual(result["applied"], 1)
        self.assertEqual(len(list_ledger_events(self.sync_config)), 1)

    def test_unknown_kind_with_non_string_wire_fields_does_not_block_good_inbox(self):
        self._produce(["valid peer inbox"])
        good_outbox = self.root / "good-unknown-wire-outbox"
        shutil.copytree(self.outbox, good_outbox)
        bundle = self._bundles()[0]
        event = self._event(bundle)
        event["event_kind"] = "future.kind"
        event["payload"]["role"] = 7
        event["payload"]["media_type"] = ["application/json"]
        event["event_id"] = derive_event_id(event)
        event_bytes = canonical_json_bytes(event)
        (bundle / "event.json").write_bytes(event_bytes)
        ready = json.loads(
            (bundle / "ready.json").read_text(encoding="utf-8")
        )
        ready.update(
            {
                "event_id": event["event_id"],
                "event_sha256": hashlib.sha256(event_bytes).hexdigest(),
                "object_count": 0,
                "object_bytes": 0,
            }
        )
        (bundle / "ready.json").write_bytes(canonical_json_bytes(ready))
        bundle.rename(
            bundle.with_name(f"{event['seq']:020d}-{event['event_id']}")
        )
        self.sync_config["inboxes"].append(
            {
                "device_id": "windows-gpu-test",
                "path": str(good_outbox),
            }
        )

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )

        self.assertEqual(result["blocked"], 1)
        self.assertEqual(result["applied"], 1)
        rows = list_ledger_events(self.sync_config)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_kind"], "transcript.chunk")

    def test_registered_historical_and_current_producers_share_one_inbox(self):
        first_transcript = self._produce(["historical producer"])
        old_producer = self._event(self._bundles()[0])["producer_instance_id"]
        first_transcript.unlink()
        (self.producer_state / "producer-state.json").unlink()
        current = initialize_producer(self.producer_config, now=NOW)
        self.assertNotEqual(current["producer_instance_id"], old_producer)
        second = self.sessions / "session-b.jsonl"
        second.write_bytes(
            json.dumps(
                {
                    "type": "session_meta",
                    "timestamp": "2026-07-31T11:05:00Z",
                    "payload": {
                        "id": "session-b",
                        "cwd": "C:\\work\\demo",
                    },
                }
            ).encode("utf-8")
            + b"\n"
            + json.dumps(self._message_record("current producer")).encode("utf-8")
            + b"\n"
        )
        collect_transcripts(
            self.producer_config,
            include_existing=True,
            now=NOW,
        )
        harvester = RecordingHarvester()

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )

        self.assertEqual(result["applied"], 2)
        rows = list_ledger_events(self.sync_config)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row["producer_instance_id"] for row in rows},
            {old_producer, current["producer_instance_id"]},
        )

    def test_producer_rotation_cursor_is_fair_across_reducer_restarts(self):
        first_transcript = self._produce(["first producer sequence one"])
        first_producer = self._event(self._bundles()[0])[
            "producer_instance_id"
        ]
        for index in (2, 3):
            self._append(first_transcript, f"first producer sequence {index}")
            collect_transcripts(self.producer_config, now=NOW)

        first_transcript.unlink()
        (self.producer_state / "producer-state.json").unlink()
        second_identity = initialize_producer(self.producer_config, now=NOW)
        second_producer = second_identity["producer_instance_id"]
        second_transcript = self.sessions / "session-b.jsonl"
        second_transcript.write_bytes(
            json.dumps(
                {
                    "type": "session_meta",
                    "timestamp": "2026-07-31T11:05:00Z",
                    "payload": {
                        "id": "session-b",
                        "cwd": "C:\\work\\demo",
                    },
                }
            ).encode("utf-8")
            + b"\n"
            + json.dumps(
                self._message_record("second producer sequence one")
            ).encode("utf-8")
            + b"\n"
        )
        collect_transcripts(
            self.producer_config,
            include_existing=True,
            now=NOW,
        )
        for index in (2, 3):
            self._append(second_transcript, f"second producer sequence {index}")
            collect_transcripts(self.producer_config, now=NOW)

        self.assertNotEqual(first_producer, second_producer)
        self.sync_config["max_events_per_run"] = 1
        observed = []
        known = set()
        for _round in range(4):
            result = reduce_inboxes(
                self.cfg,
                self.sync_config,
                harvest_adapter=RecordingHarvester(),
                now=NOW,
            )
            self.assertEqual(result["applied"], 1)
            current = {
                (row["producer_instance_id"], row["seq"])
                for row in list_ledger_events(self.sync_config)
            }
            new_key = (current - known).pop()
            known = current
            observed.append(new_key[0])

        self.assertEqual(set(observed), {first_producer, second_producer})
        self.assertNotEqual(observed[0], observed[1])
        self.assertEqual(observed[0], observed[2])
        self.assertEqual(observed[1], observed[3])

    def test_unknown_major_schema_blocks_without_receipt_or_sequence_advance(self):
        self._produce(["future schema"])
        bundle = self._bundles()[0]
        event = self._event(bundle)
        event["schema_version"] = 2
        self._rewrite_event_and_ready(bundle, event)

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )

        self.assertEqual(result["blocked"], 1)
        self.assertEqual(list_ledger_events(self.sync_config), [])

    def test_noncanonical_ready_json_is_rejected_before_harvest(self):
        self._produce(["noncanonical ready"])
        ready_path = self._bundles()[0] / "ready.json"
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        ready_path.write_text(
            json.dumps(ready, indent=2) + "\n",
            encoding="utf-8",
        )
        harvester = RecordingHarvester()

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )

        self.assertGreaterEqual(result["blocked"], 1)
        self.assertEqual(harvester.calls, [])
        self.assertEqual(list_ledger_events(self.sync_config), [])

    def test_oversize_json_integer_is_quarantined_as_protocol_error(self):
        self._produce(["oversize integer"])
        bundle = self._bundles()[0]
        event_path = bundle / "event.json"
        event_bytes = event_path.read_bytes().replace(
            b'"seq":1,',
            b'"seq":' + (b"9" * 5000) + b",",
            1,
        )
        self.assertIn(b"9" * 5000, event_bytes)
        event_path.write_bytes(event_bytes)
        ready_path = bundle / "ready.json"
        ready = json.loads(ready_path.read_text(encoding="utf-8"))
        ready["event_sha256"] = hashlib.sha256(event_bytes).hexdigest()
        ready_path.write_bytes(canonical_json_bytes(ready))

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )

        self.assertEqual(result["blocked"], 1)
        self.assertEqual(result["applied"], 0)
        self.assertEqual(list_ledger_events(self.sync_config), [])

    def test_lifecycle_event_is_rejected_and_advances_sequence_without_harvest(self):
        self._produce(["source"])
        bundle = self._bundles()[0]
        event = self._event(bundle)
        event["event_kind"] = "memory.retract"
        event["event_id"] = derive_event_id(event)
        self._rewrite_event_and_ready(bundle, event, allow_unknown_kind=True)
        harvester = RecordingHarvester()

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )
        row = list_ledger_events(self.sync_config)[0]

        self.assertEqual(result["rejected"], 1)
        self.assertEqual(harvester.calls, [])
        self.assertEqual(row["status"], "rejected_pending_publish")
        self.assertEqual(row["code"], "forbidden_event_kind")

    def test_gap_creates_bounded_skipped_record_not_original_content(self):
        self.producer_config["max_chunk_bytes"] = 128
        huge = "secret-content-" * 100
        self._produce([huge])
        harvester = RecordingHarvester()

        reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )

        mirror = next(iter(harvester.calls[0].values()))
        self.assertNotIn(huge.encode("utf-8"), mirror)
        self.assertGreater(len(mirror), 65_536)

    def test_gap_larger_than_authority_limit_is_blocked_without_advancing(self):
        self.producer_config["max_chunk_bytes"] = 128
        self._produce(["oversized-gap-" * 100])
        bundle = self._bundles()[0]
        event = self._event(bundle)
        self.assertEqual(event["event_kind"], "transcript.gap")
        event["source_cursor"]["end"] = 2**62
        event["payload"]["bytes"] = 2**62
        event["event_id"] = derive_event_id(event)
        self._rewrite_event_and_ready(bundle, event)
        self.sync_config["max_gap_bytes"] = 4096

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )

        self.assertGreaterEqual(result["blocked"], 1)
        self.assertEqual(result["applied"], 0)
        self.assertEqual(list_ledger_events(self.sync_config), [])

    def test_attachment_writes_fixed_blob_and_auditable_metadata(self):
        image, _transcript = self._produce_attachment(
            "diagram.png",
            b"\x89PNG\r\n\x1a\nremote-image",
        )
        harvester = RecordingHarvester()

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )
        attachment_event = next(
            event for event in self._events()
            if event["event_kind"] == "attachment.blob"
        )
        digest = attachment_event["payload"]["sha256"]
        blob = (
            self.vault
            / "Attachments"
            / "Agent-Memory-Beacon"
            / "remote"
            / "objects"
            / digest[:2]
            / f"{digest}.png"
        )
        record = (
            self.vault
            / "04-Feedback"
            / "remote-attachments"
            / attachment_event["device_id"]
            / attachment_event["producer_instance_id"]
            / (
                f"{attachment_event['seq']:020d}-"
                f"{attachment_event['event_id']}.md"
            )
        )

        self.assertEqual(result["applied"], 2)
        self.assertEqual(len(harvester.calls), 1)
        self.assertEqual(blob.read_bytes(), image.read_bytes())
        metadata = record.read_text(encoding="utf-8")
        self.assertIn(attachment_event["event_id"], metadata)
        self.assertIn(attachment_event["session_id"], metadata)
        self.assertIn(digest, metadata)
        self.assertNotIn(str(image), metadata)
        row = next(
            row for row in list_ledger_events(self.sync_config)
            if row["event_kind"] == "attachment.blob"
        )
        self.assertEqual(
            row["canonical_path"],
            blob.relative_to(self.vault).as_posix(),
        )
        self.assertEqual(
            row["metadata_path"],
            record.relative_to(self.vault).as_posix(),
        )

    def test_attachment_write_crash_retries_without_duplicate_or_reharvest(self):
        self._produce_attachment(
            "retry.png",
            b"\x89PNG\r\n\x1a\nretry-image",
        )
        harvester = RecordingHarvester()

        with self.assertRaisesRegex(ReducerError, "after_attachment_write"):
            reduce_inboxes(
                self.cfg,
                self.sync_config,
                harvest_adapter=harvester,
                now=NOW,
                fault_point="after_attachment_write",
            )

        recovered = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )

        self.assertEqual(recovered["applied"], 2)
        self.assertEqual(len(harvester.calls), 1)
        self.assertEqual(
            len(
                list(
                    (
                        self.vault
                        / "Attachments"
                        / "Agent-Memory-Beacon"
                        / "remote"
                        / "objects"
                    ).rglob("*.*")
                )
            ),
            1,
        )
        self.assertEqual(
            len(
                list(
                    (
                        self.vault
                        / "04-Feedback"
                        / "remote-attachments"
                    ).rglob("*.md")
                )
            ),
            1,
        )

    def test_attachment_missing_before_seal_is_repaired_before_receipt(self):
        self._produce_attachment(
            "receipt.png",
            b"\x89PNG\r\n\x1a\nreceipt-image",
        )
        self.sync_config["published_dir"] = str(self.root / "published")
        reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )
        attachment_row = next(
            row
            for row in list_ledger_events(self.sync_config)
            if row["event_kind"] == "attachment.blob"
        )
        ledger = sqlite3.connect(self.authority_state / "ledger.sqlite3")
        ledger.execute(
            """
            update events
               set bundle_path = ?
             where producer_instance_id = ? and seq = ?
            """,
            (
                str(self.root / "outside-ledger-path"),
                attachment_row["producer_instance_id"],
                attachment_row["seq"],
            ),
        )
        ledger.commit()
        ledger.close()
        (self.vault / attachment_row["canonical_path"]).unlink()

        first_generation = publish_generation(
            self.cfg,
            self.sync_config,
            now=NOW,
        )
        first_receipts = publish_pending_receipts(
            self.sync_config,
            first_generation,
            now=NOW,
        )
        attachment_row = next(
            row
            for row in list_ledger_events(self.sync_config)
            if row["event_kind"] == "attachment.blob"
        )

        self.assertEqual(first_receipts["published"], 1)
        self.assertIsNone(attachment_row["canonical_generation"])
        self.assertEqual(attachment_row["generation_id"], "")

        repaired = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )
        second_generation = publish_generation(
            self.cfg,
            self.sync_config,
            now=NOW,
        )
        second_receipts = publish_pending_receipts(
            self.sync_config,
            second_generation,
            now=NOW,
        )
        attachment_row = next(
            row
            for row in list_ledger_events(self.sync_config)
            if row["event_kind"] == "attachment.blob"
        )

        self.assertEqual(repaired["repaired_attachments"], 1)
        self.assertEqual(second_receipts["published"], 1)
        self.assertEqual(attachment_row["status"], "applied")
        self.assertEqual(
            attachment_row["canonical_generation"],
            second_generation["generation"],
        )

    def test_missing_old_attachment_bundle_does_not_starve_later_repair(self):
        self._produce_attachment(
            "missing.png",
            b"\x89PNG\r\n\x1a\nmissing-bundle",
        )
        self._produce_attachment(
            "repairable.png",
            b"\x89PNG\r\n\x1a\nrepairable-bundle",
        )
        reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )
        attachments = [
            row
            for row in list_ledger_events(self.sync_config)
            if row["event_kind"] == "attachment.blob"
        ]
        self.assertEqual(len(attachments), 2)
        first, second = attachments
        first_bundle = next(
            bundle
            for bundle in self._bundles()
            if self._event(bundle)["event_id"] == first["event_id"]
        )
        shutil.rmtree(first_bundle)
        (self.vault / first["canonical_path"]).unlink()
        second_path = self.vault / second["canonical_path"]
        second_path.unlink()
        self.sync_config["max_events_per_run"] = 1

        repaired = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )

        self.assertEqual(repaired["repaired_attachments"], 1)
        self.assertFalse((self.vault / first["canonical_path"]).exists())
        self.assertTrue(second_path.is_file())

    def test_pending_attachment_repair_cursor_crosses_missing_window(self):
        for index in range(10):
            self._produce_attachment(
                f"repair-{index}.png",
                b"\x89PNG\r\n\x1a\n" + str(index).encode("ascii"),
            )
        reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )
        attachments = [
            row
            for row in list_ledger_events(self.sync_config)
            if row["event_kind"] == "attachment.blob"
        ]
        self.assertEqual(len(attachments), 10)
        bundles = {
            self._event(bundle)["event_id"]: bundle for bundle in self._bundles()
        }
        for row in attachments:
            (self.vault / row["canonical_path"]).unlink()
        for row in attachments[:-1]:
            shutil.rmtree(bundles[row["event_id"]])
        final_path = self.vault / attachments[-1]["canonical_path"]
        self.sync_config["max_events_per_run"] = 1

        first = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )
        second = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )

        self.assertEqual(first["repaired_attachments"], 0)
        self.assertEqual(second["repaired_attachments"], 1)
        self.assertTrue(final_path.is_file())

    def test_pending_attachment_scan_cursor_has_bounded_parser(self):
        connection = beacon_sync_reducer._open_ledger(self.authority_state)
        producer = "12345678-1234-4234-9234-123456789abc"
        cases = {
            "int64 overflow": f'["{producer}",9223372036854775808]',
            "excessive depth": ("[" * 1000) + "0" + ("]" * 1000),
            "excessive bytes": json.dumps(["x" * 1024, 1]),
        }
        try:
            for name, value in cases.items():
                with self.subTest(name=name):
                    connection.execute(
                        """
                        insert into metadata(key, value) values (?, ?)
                        on conflict(key) do update set value = excluded.value
                        """,
                        (
                            beacon_sync_reducer.PENDING_ATTACHMENT_SCAN_CURSOR_KEY,
                            value,
                        ),
                    )
                    connection.commit()
                    self.assertIsNone(
                        beacon_sync_reducer._pending_attachment_scan_cursor(
                            connection
                        )
                    )

            valid = json.dumps([producer, 2**63 - 1], separators=(",", ":"))
            connection.execute(
                """
                update metadata set value = ? where key = ?
                """,
                (
                    valid,
                    beacon_sync_reducer.PENDING_ATTACHMENT_SCAN_CURSOR_KEY,
                ),
            )
            connection.commit()
            self.assertEqual(
                beacon_sync_reducer._pending_attachment_scan_cursor(connection),
                (producer, 2**63 - 1),
            )
        finally:
            connection.close()

    def test_pending_attachment_cursor_rejects_character_oversize_before_encode(self):
        class EncodeMustNotRun(str):
            def encode(self, *_args, **_kwargs):
                raise AssertionError("oversize cursor was encoded")

        class FakeConnection:
            @staticmethod
            def execute(*_args, **_kwargs):
                value = EncodeMustNotRun(
                    "x"
                    * (
                        beacon_sync_reducer.MAX_PENDING_ATTACHMENT_CURSOR_BYTES
                        + 1
                    )
                )
                return SimpleNamespace(
                    fetchone=lambda: {"value": value}
                )

        self.assertIsNone(
            beacon_sync_reducer._pending_attachment_scan_cursor(
                FakeConnection()
            )
        )

    def test_read_only_ledger_open_does_not_replace_or_publish_main_file(self):
        connection = beacon_sync_reducer._open_ledger(self.authority_state)
        connection.close()
        path = self.authority_state / "ledger.sqlite3"
        before_info = os.stat(path, follow_symlinks=False)
        before_bytes = path.read_bytes()

        self.assertEqual(list_ledger_events(self.sync_config), [])

        after_info = os.stat(path, follow_symlinks=False)
        self.assertEqual(path.read_bytes(), before_bytes)
        self.assertEqual(
            (after_info.st_dev, after_info.st_ino),
            (before_info.st_dev, before_info.st_ino),
        )

    def test_two_process_ledger_writers_serialize_from_open_through_publish(self):
        initial = beacon_sync_reducer._open_ledger(self.authority_state)
        initial.close()
        context = multiprocessing.get_context("spawn")
        first_opened = context.Event()
        second_opened = context.Event()
        release_first = context.Event()
        release_second = context.Event()
        results = context.Queue()
        first = context.Process(
            target=_ledger_writer_worker,
            args=(
                str(self.authority_state),
                "writer-one",
                first_opened,
                release_first,
                results,
            ),
        )
        second = context.Process(
            target=_ledger_writer_worker,
            args=(
                str(self.authority_state),
                "writer-two",
                second_opened,
                release_second,
                results,
            ),
        )
        first.start()
        self.assertTrue(first_opened.wait(timeout=10))
        second.start()
        try:
            self.assertFalse(second_opened.wait(timeout=0.5))
            release_first.set()
            self.assertTrue(second_opened.wait(timeout=10))
            release_second.set()
        finally:
            release_first.set()
            release_second.set()
            first.join(timeout=15)
            second.join(timeout=15)
            if first.is_alive():
                first.terminate()
            if second.is_alive():
                second.terminate()

        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)
        outcomes = [results.get(timeout=2), results.get(timeout=2)]
        self.assertTrue(all(item[0] == "ok" for item in outcomes), outcomes)
        connection = beacon_sync_reducer._open_ledger(self.authority_state)
        try:
            keys = {
                row[0]
                for row in connection.execute(
                    "select key from metadata where key like 'writer-%'"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertEqual(keys, {"writer-one", "writer-two"})

    def test_first_ledger_creation_competition_uses_the_same_process_lock(self):
        context = multiprocessing.get_context("spawn")
        first_publish = context.Event()
        second_publish = context.Event()
        release = context.Event()
        results = context.Queue()
        first = context.Process(
            target=_first_create_worker,
            args=(
                str(self.authority_state),
                "creator-one",
                first_publish,
                release,
                results,
            ),
        )
        second = context.Process(
            target=_first_create_worker,
            args=(
                str(self.authority_state),
                "creator-two",
                second_publish,
                release,
                results,
            ),
        )
        first.start()
        self.assertTrue(first_publish.wait(timeout=10))
        second.start()
        try:
            self.assertFalse(second_publish.wait(timeout=0.5))
        finally:
            release.set()
            first.join(timeout=15)
            second.join(timeout=15)
            if first.is_alive():
                first.terminate()
            if second.is_alive():
                second.terminate()

        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)
        outcomes = [results.get(timeout=2), results.get(timeout=2)]
        self.assertTrue(all(item[0] == "ok" for item in outcomes), outcomes)
        connection = beacon_sync_reducer._open_ledger(self.authority_state)
        try:
            keys = {
                row[0]
                for row in connection.execute(
                    "select key from metadata where key like 'creator-%'"
                ).fetchall()
            }
        finally:
            connection.close()
        self.assertEqual(keys, {"creator-one", "creator-two"})

    def test_first_ledger_creation_does_not_serialize_empty_database(self):
        real_serialize = beacon_sync_reducer._LedgerConnection.serialize

        def reject_empty_database(connection, *args, **kwargs):
            table_count = connection.execute(
                "select count(*) from sqlite_master where type = 'table'"
            ).fetchone()[0]
            if table_count == 0:
                raise sqlite3.OperationalError("unable to serialize 'main'")
            return real_serialize(connection, *args, **kwargs)

        with patch.object(
            beacon_sync_reducer._LedgerConnection,
            "serialize",
            reject_empty_database,
        ):
            connection = beacon_sync_reducer._open_ledger(self.authority_state)
            try:
                version = connection.execute(
                    "select value from metadata where key = 'schema_version'"
                ).fetchone()[0]
            finally:
                connection.close()

        self.assertEqual(
            version,
            str(beacon_sync_reducer.LEDGER_SCHEMA_VERSION),
        )
        self.assertTrue((self.authority_state / "ledger.sqlite3").is_file())

    def test_ledger_commit_cas_rejects_regular_path_exchange(self):
        connection = beacon_sync_reducer._open_ledger(self.authority_state)
        ledger = self.authority_state / "ledger.sqlite3"
        displaced = self.authority_state / "ledger.displaced.sqlite3"
        replacement_state = self.root / "replacement-ledger"
        replacement = beacon_sync_reducer._open_ledger(replacement_state)
        replacement.execute(
            "insert into metadata(key, value) values ('external', 'replacement')"
        )
        replacement.commit()
        replacement.close()
        ledger.rename(displaced)
        shutil.copyfile(replacement_state / "ledger.sqlite3", ledger)
        replacement_bytes = ledger.read_bytes()

        try:
            connection.execute(
                "insert into metadata(key, value) values ('local', 'stale')"
            )
            with self.assertRaisesRegex(ReducerError, "changed|conflict"):
                connection.commit()
        finally:
            connection.close()

        self.assertEqual(ledger.read_bytes(), replacement_bytes)

    def test_wal_frames_are_recovered_privately_without_touching_authority_files(self):
        connection = beacon_sync_reducer._open_ledger(self.authority_state)
        connection.close()
        ledger = self.authority_state / "ledger.sqlite3"
        context = multiprocessing.get_context("spawn")
        worker = context.Process(target=_leave_wal_worker, args=(str(ledger),))
        worker.start()
        worker.join(timeout=15)
        self.assertEqual(worker.exitcode, 0)
        wal = Path(f"{ledger}-wal")
        self.assertTrue(wal.is_file())
        self.assertGreater(wal.stat().st_size, 32)
        before = {
            path.name: path.read_bytes()
            for path in self.authority_state.iterdir()
            if path.name.startswith("ledger.sqlite3")
        }

        recovered = beacon_sync_reducer._open_ledger(self.authority_state)
        try:
            row = recovered.execute(
                "select value from metadata where key = 'wal-visible'"
            ).fetchone()
            self.assertEqual(row[0], "yes")
        finally:
            recovered.close()

        after = {
            path.name: path.read_bytes()
            for path in self.authority_state.iterdir()
            if path.name.startswith("ledger.sqlite3")
        }
        self.assertEqual(after, before)

    def test_wal_header_only_is_opened_on_the_private_recovery_path(self):
        connection = beacon_sync_reducer._open_ledger(self.authority_state)
        connection.close()
        ledger = self.authority_state / "ledger.sqlite3"
        context = multiprocessing.get_context("spawn")
        worker = context.Process(target=_leave_wal_worker, args=(str(ledger),))
        worker.start()
        worker.join(timeout=15)
        self.assertEqual(worker.exitcode, 0)
        wal = Path(f"{ledger}-wal")
        wal.write_bytes(wal.read_bytes()[:32])
        shm = Path(f"{ledger}-shm")
        if shm.exists():
            shm.unlink()
        before_main = ledger.read_bytes()
        before_wal = wal.read_bytes()
        observed = []
        real_recover = beacon_sync_reducer._recover_private_ledger

        def inspect_private_recovery(private_main):
            private_wal = Path(f"{private_main}-wal")
            observed.append(private_wal.read_bytes())
            return real_recover(private_main)

        with patch.object(
            beacon_sync_reducer,
            "_recover_private_ledger",
            side_effect=inspect_private_recovery,
        ):
            recovered = beacon_sync_reducer._open_ledger(self.authority_state)
            recovered.close()

        self.assertEqual(observed, [before_wal])
        self.assertEqual(ledger.read_bytes(), before_main)
        self.assertEqual(wal.read_bytes(), before_wal)

    def test_hot_rollback_journal_recovers_v1_through_v4_before_migration(self):
        context = multiprocessing.get_context("spawn")
        for version in range(1, 5):
            with self.subTest(version=version):
                state_dir = self.root / f"hot-journal-v{version}"
                state_dir.mkdir()
                ledger = state_dir / "ledger.sqlite3"
                self._create_ledger_version(ledger, version)
                worker = context.Process(
                    target=_leave_hot_journal_worker,
                    args=(str(ledger),),
                )
                worker.start()
                worker.join(timeout=15)
                self.assertEqual(worker.exitcode, 0)
                journal = Path(f"{ledger}-journal")
                self.assertTrue(journal.is_file())
                self.assertGreater(journal.stat().st_size, 512)

                recovered = beacon_sync_reducer._open_ledger(state_dir)
                try:
                    schema = recovered.execute(
                        "select value from metadata where key = 'schema_version'"
                    ).fetchone()[0]
                    leaked = recovered.execute(
                        "select count(*) from metadata where key = 'uncommitted'"
                    ).fetchone()[0]
                    changed_seed = recovered.execute(
                        "select count(*) from metadata "
                        "where key like 'seed-%' and value != ?",
                        ("x" * 1024,),
                    ).fetchone()[0]
                finally:
                    recovered.close()

                self.assertEqual(
                    schema,
                    str(beacon_sync_reducer.LEDGER_SCHEMA_VERSION),
                )
                self.assertEqual(leaked, 0)
                self.assertEqual(changed_seed, 0)

    def test_corrupt_and_oversize_ledger_sidecars_fail_closed(self):
        cases = (
            ("-wal", b"not-a-valid-wal-header"),
            ("-journal", b"not-a-valid-journal-header" * 32),
        )
        for suffix, payload in cases:
            with self.subTest(suffix=suffix):
                state_dir = self.root / f"corrupt-{suffix[1:]}"
                connection = beacon_sync_reducer._open_ledger(state_dir)
                connection.close()
                ledger = state_dir / "ledger.sqlite3"
                sidecar = Path(f"{ledger}{suffix}")
                sidecar.write_bytes(payload)
                before_main = ledger.read_bytes()
                before_sidecar = sidecar.read_bytes()

                with self.assertRaisesRegex(ReducerError, "sidecar|WAL|journal"):
                    beacon_sync_reducer._open_ledger(state_dir)

                self.assertEqual(ledger.read_bytes(), before_main)
                self.assertEqual(sidecar.read_bytes(), before_sidecar)

        state_dir = self.root / "oversize-sidecar"
        connection = beacon_sync_reducer._open_ledger(state_dir)
        connection.close()
        ledger = state_dir / "ledger.sqlite3"
        wal = Path(f"{ledger}-wal")
        wal.write_bytes(b"x" * 65)
        with (
            patch.object(
                beacon_sync_reducer,
                "MAX_LEDGER_SIDECAR_BYTES",
                64,
                create=True,
            ),
            self.assertRaisesRegex(ReducerError, "size"),
        ):
            beacon_sync_reducer._open_ledger(state_dir)

    def test_private_recovery_failure_preserves_authority_and_releases_lock(self):
        connection = beacon_sync_reducer._open_ledger(self.authority_state)
        connection.close()
        ledger = self.authority_state / "ledger.sqlite3"
        context = multiprocessing.get_context("spawn")
        worker = context.Process(target=_leave_wal_worker, args=(str(ledger),))
        worker.start()
        worker.join(timeout=15)
        self.assertEqual(worker.exitcode, 0)
        before = {
            path.name: path.read_bytes()
            for path in self.authority_state.iterdir()
            if path.name.startswith("ledger.sqlite3")
        }

        with patch.object(
            beacon_sync_reducer,
            "_recover_private_ledger",
            side_effect=RuntimeError("injected private recovery crash"),
            create=True,
        ):
            with self.assertRaisesRegex(RuntimeError, "recovery crash"):
                beacon_sync_reducer._open_ledger(self.authority_state)

        after = {
            path.name: path.read_bytes()
            for path in self.authority_state.iterdir()
            if path.name.startswith("ledger.sqlite3")
        }
        self.assertEqual(after, before)
        self.assertEqual(
            list(self.authority_state.glob(".ledger-recovery-*")),
            [],
        )
        reopened = beacon_sync_reducer._open_ledger(self.authority_state)
        reopened.close()

    def test_ledger_migration_resumes_at_exact_intermediate_version(self):
        self.authority_state.mkdir()
        path = self.authority_state / "ledger.sqlite3"
        self._create_v1_ledger(path)
        producer = "12345678-1234-4234-9234-123456789abc"
        legacy_mirror = (
            f"/legacy/state/mirrors/{producer}/stream-test/session-remote.jsonl"
        )
        connection = sqlite3.connect(path)
        connection.execute(
            "insert into producers values (?, ?, ?, ?, ?)",
            (producer, "windows-gpu-test", 2, "", "2026-07-31T12:00:00Z"),
        )
        connection.execute(
            "insert into streams values (?, ?, ?, ?, ?, ?, ?)",
            (
                producer,
                "stream-test",
                "87654321-4321-4234-9234-cba987654321",
                1,
                legacy_mirror,
                "session-test",
                "codex",
            ),
        )
        connection.execute(
            """
            insert into events values (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?
            )
            """,
            (
                producer,
                1,
                "event-" + ("a" * 64),
                "b" * 64,
                "windows-gpu-test",
                "applied",
                "applied",
                "/legacy/bundle",
                "transcript.chunk",
                "stream-test",
                "87654321-4321-4234-9234-cba987654321",
                0,
                1,
                legacy_mirror,
                0,
                1,
                "c" * 64,
                "2026-07-31T12:00:00Z",
                "2026-07-31T12:00:01Z",
                None,
                "",
            ),
        )
        connection.commit()
        connection.close()

        with patch.object(
            beacon_sync_reducer,
            "_migrate_ledger_v2_to_v3",
            side_effect=RuntimeError("injected migration interruption"),
        ):
            with self.assertRaisesRegex(RuntimeError, "migration interruption"):
                beacon_sync_reducer._open_ledger(self.authority_state)

        connection = sqlite3.connect(self.authority_state / "ledger.sqlite3")
        version = connection.execute(
            "select value from metadata where key = 'schema_version'"
        ).fetchone()[0]
        connection.close()
        self.assertEqual(version, "2")

        recovered = beacon_sync_reducer._open_ledger(self.authority_state)
        final_version = recovered.execute(
            "select value from metadata where key = 'schema_version'"
        ).fetchone()[0]
        columns = {
            row["name"]
            for row in recovered.execute("pragma table_info(events)")
        }
        stream_mirror = recovered.execute(
            "select mirror_path from streams"
        ).fetchone()[0]
        event_mirror = recovered.execute(
            "select mirror_path from events"
        ).fetchone()[0]
        recovered.close()

        self.assertEqual(
            final_version,
            str(beacon_sync_reducer.LEDGER_SCHEMA_VERSION),
        )
        self.assertIn("metadata_sha256", columns)
        self.assertIn("metadata_bytes", columns)
        self.assertEqual(
            stream_mirror,
            f"mirrors/{producer}/stream-test/session-remote.jsonl",
        )
        self.assertEqual(event_mirror, stream_mirror)

    def test_incomplete_ledger_schema_is_rejected_without_upgrade(self):
        self.authority_state.mkdir()
        path = self.authority_state / "ledger.sqlite3"
        connection = sqlite3.connect(path)
        connection.executescript(
            """
            create table metadata (
                key text primary key,
                value text not null
            );
            insert into metadata values ('schema_version', '1');
            """
        )
        connection.commit()
        connection.close()
        before = path.read_bytes()

        with self.assertRaisesRegex(ReducerError, "schema"):
            beacon_sync_reducer._open_ledger(self.authority_state)

        self.assertEqual(path.read_bytes(), before)

    def test_ledger_schema_requires_primary_keys_and_unique_event_id(self):
        for missing in ("events_primary_key", "event_id_unique"):
            with self.subTest(missing=missing):
                state_dir = self.root / missing
                state_dir.mkdir()
                path = state_dir / "ledger.sqlite3"
                self._create_v1_ledger(path, missing=missing)
                before = path.read_bytes()

                with self.assertRaisesRegex(ReducerError, "schema"):
                    beacon_sync_reducer._open_ledger(state_dir)

                self.assertEqual(path.read_bytes(), before)

    def test_ledger_hardlink_is_rejected_without_modifying_peer_database(self):
        source_state = self.root / "source-ledger"
        source = beacon_sync_reducer._open_ledger(source_state)
        source.close()
        peer = source_state / "ledger.sqlite3"
        before = peer.read_bytes()
        self.authority_state.mkdir()
        os.link(peer, self.authority_state / "ledger.sqlite3")
        opened = None

        try:
            with self.assertRaisesRegex(ReducerError, "hard link"):
                opened = beacon_sync_reducer._open_ledger(
                    self.authority_state
                )
        finally:
            if opened is not None:
                opened.close()

        self.assertEqual(peer.read_bytes(), before)

    def test_ledger_link_swap_before_connect_leaves_peer_byte_identical(self):
        self.authority_state.mkdir()
        ledger_path = self.authority_state / "ledger.sqlite3"
        self._create_v1_ledger(ledger_path)
        peer_state = self.root / "peer-ledger"
        peer_state.mkdir()
        peer = peer_state / "ledger.sqlite3"
        self._create_v1_ledger(peer)
        peer_before = peer.read_bytes()
        displaced = self.authority_state / "ledger.original.sqlite3"
        real_connect = sqlite3.connect
        swapped = False

        def swap_before_connect(database, *args, **kwargs):
            nonlocal swapped
            if not swapped:
                swapped = True
                ledger_path.rename(displaced)
                try:
                    ledger_path.symlink_to(peer)
                except (OSError, NotImplementedError):
                    self.skipTest("symlinks unavailable")
            return real_connect(database, *args, **kwargs)

        with patch.object(
            beacon_sync_reducer.sqlite3,
            "connect",
            side_effect=swap_before_connect,
        ):
            with self.assertRaisesRegex(ReducerError, "symlink"):
                beacon_sync_reducer._open_ledger(self.authority_state)

        self.assertTrue(swapped)
        self.assertEqual(peer.read_bytes(), peer_before)

    def test_ledger_sidecars_reject_symlink_and_hardlink_without_peer_write(self):
        for suffix in ("-wal", "-shm", "-journal"):
            for link_kind in ("symlink", "hardlink"):
                with self.subTest(suffix=suffix, link_kind=link_kind):
                    state_dir = self.root / f"sidecar-{suffix[1:]}-{link_kind}"
                    connection = beacon_sync_reducer._open_ledger(state_dir)
                    connection.close()
                    peer = self.root / f"peer-{suffix[1:]}-{link_kind}"
                    peer.write_bytes(b"peer-sidecar-must-not-change")
                    sidecar = state_dir / f"ledger.sqlite3{suffix}"
                    if link_kind == "symlink":
                        try:
                            sidecar.symlink_to(peer)
                        except (OSError, NotImplementedError):
                            self.skipTest("symlinks unavailable")
                    else:
                        os.link(peer, sidecar)
                    before = peer.read_bytes()
                    opened = None

                    try:
                        with self.assertRaisesRegex(
                            ReducerError,
                            "symlink|hard link",
                        ):
                            opened = beacon_sync_reducer._open_ledger(state_dir)
                    finally:
                        if opened is not None:
                            opened.close()

                    self.assertEqual(peer.read_bytes(), before)

    def test_ledger_reparse_point_is_rejected_before_sqlite_open(self):
        connection = beacon_sync_reducer._open_ledger(self.authority_state)
        connection.close()
        ledger_path = self.authority_state / "ledger.sqlite3"
        real_lstat = os.lstat
        actual = real_lstat(ledger_path)
        reparse = SimpleNamespace(
            st_mode=actual.st_mode,
            st_nlink=1,
            st_file_attributes=getattr(
                stat,
                "FILE_ATTRIBUTE_REPARSE_POINT",
                0x400,
            ),
        )

        def reparse_lstat(path):
            if Path(path) == ledger_path:
                return reparse
            return real_lstat(path)

        with patch.object(
            beacon_sync_reducer.os,
            "lstat",
            side_effect=reparse_lstat,
        ):
            with self.assertRaisesRegex(ReducerError, "reparse"):
                beacon_sync_reducer._open_ledger(self.authority_state)

    def test_attachment_larger_than_authority_limit_is_blocked_before_vault_write(self):
        self._produce_attachment("large.bin", b"x" * 64)
        self.sync_config["max_attachment_bytes"] = 32

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )

        self.assertGreaterEqual(result["blocked"], 1)
        self.assertFalse(
            (
                self.vault
                / "Attachments"
                / "Agent-Memory-Beacon"
                / "remote"
            ).exists()
        )
        rows = list_ledger_events(self.sync_config)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_kind"], "transcript.chunk")

    def test_existing_attachment_blob_hardlink_is_rejected_without_modifying_peer(self):
        image, _transcript = self._produce_attachment(
            "hardlink.png",
            b"\x89PNG\r\n\x1a\nhardlink-image",
        )
        attachment_event = next(
            event for event in self._events()
            if event["event_kind"] == "attachment.blob"
        )
        digest = attachment_event["payload"]["sha256"]
        blob = (
            self.vault
            / "Attachments"
            / "Agent-Memory-Beacon"
            / "remote"
            / "objects"
            / digest[:2]
            / f"{digest}.png"
        )
        blob.parent.mkdir(parents=True)
        outside = self.root / "outside-blob"
        outside.write_bytes(image.read_bytes())
        os.link(outside, blob)
        before = outside.read_bytes()

        with self.assertRaisesRegex((ReducerError, ValueError), "hard link"):
            reduce_inboxes(
                self.cfg,
                self.sync_config,
                harvest_adapter=RecordingHarvester(),
                now=NOW,
            )

        self.assertEqual(outside.read_bytes(), before)

    def test_failed_harvest_retries_without_duplicate_mirror_append(self):
        self._produce(["retry canonical"])
        harvester = RecordingHarvester(fail_times=1)

        with self.assertRaisesRegex(RuntimeError, "injected"):
            reduce_inboxes(
                self.cfg,
                self.sync_config,
                harvest_adapter=harvester,
                now=NOW,
            )
        first_mirror = next(iter(harvester.calls[0].values()))

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )
        second_mirror = next(iter(harvester.calls[1].values()))

        self.assertEqual(result["applied"], 1)
        self.assertEqual(second_mirror, first_mirror)
        self.assertEqual(second_mirror.count(b"retry canonical"), 1)

    def test_multiple_contiguous_events_for_one_stream_harvest_once(self):
        transcript = self._produce(["first"])
        self._append(transcript, "second")
        collect_transcripts(self.producer_config, now=NOW)
        harvester = RecordingHarvester()

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )

        self.assertEqual(result["applied"], 2)
        self.assertEqual(len(harvester.calls), 1)
        self.assertEqual(len(harvester.calls[0]), 1)
        mirror = next(iter(harvester.calls[0].values()))
        self.assertIn(b"first", mirror)
        self.assertIn(b"second", mirror)

    def test_crash_after_mirror_fsync_recovers_without_duplicate_append(self):
        self._produce(["exactly once marker"])
        harvester = RecordingHarvester()

        with self.assertRaisesRegex(ReducerError, "injected"):
            reduce_inboxes(
                self.cfg,
                self.sync_config,
                harvest_adapter=harvester,
                now=NOW,
                fault_point="after_mirror_fsync",
            )

        recovered = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )
        mirror = next(self.authority_state.rglob("*-remote.jsonl"))
        self.assertEqual(mirror.read_bytes().count(b"exactly once marker"), 1)
        self.assertEqual(recovered["applied"], 1)
        self.assertEqual(len(harvester.calls), 1)

    def test_moved_authority_state_rebases_relative_mirror_locator(self):
        transcript = self._produce(["before move"])
        reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )
        old_mirror = next(self.authority_state.rglob("*-remote.jsonl"))
        old_bytes = old_mirror.read_bytes()
        moved_state = self.root / "moved-authority-state"
        shutil.copytree(self.authority_state, moved_state)
        self.sync_config["state_dir"] = str(moved_state)
        self._append(transcript, "after move")
        collect_transcripts(self.producer_config, now=NOW)
        harvester = RecordingHarvester()

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )

        self.assertEqual(result["applied"], 1)
        self.assertEqual(old_mirror.read_bytes(), old_bytes)
        moved_mirror = next(moved_state.rglob("*-remote.jsonl"))
        self.assertIn(b"after move", moved_mirror.read_bytes())
        rows = list_ledger_events(self.sync_config)
        self.assertTrue(rows[-1]["mirror_path"].startswith("mirrors/"))
        self.assertFalse(Path(rows[-1]["mirror_path"]).is_absolute())

    def test_mirror_hardlink_is_rejected_without_modifying_other_file(self):
        transcript = self._produce(["before hardlink"])
        reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )
        mirror = next(self.authority_state.rglob("*-remote.jsonl"))
        outside = self.root / "outside-mirror.jsonl"
        outside.write_bytes(mirror.read_bytes())
        mirror.unlink()
        os.link(outside, mirror)
        before = outside.read_bytes()
        self._append(transcript, "must not escape")
        collect_transcripts(self.producer_config, now=NOW)

        with self.assertRaisesRegex(ReducerError, "hard link"):
            reduce_inboxes(
                self.cfg,
                self.sync_config,
                harvest_adapter=RecordingHarvester(),
                now=NOW,
            )

        self.assertEqual(outside.read_bytes(), before)

    def test_receipt_retry_keeps_generation_assigned_before_file_publish(self):
        self.sync_config["published_dir"] = str(self.root / "published")
        self._produce(["generation one"])
        reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )
        first = publish_generation(
            self.cfg,
            self.sync_config,
            now=NOW,
        )

        with patch.object(
            beacon_sync_snapshot,
            "mark_receipts_published",
            side_effect=RuntimeError("injected ledger commit crash"),
        ):
            with self.assertRaisesRegex(RuntimeError, "ledger commit crash"):
                publish_pending_receipts(
                    self.sync_config,
                    first,
                    now=NOW,
                )

        pending = list_ledger_events(self.sync_config)[0]
        self.assertEqual(
            pending["canonical_generation"],
            first["generation"],
        )
        self.assertEqual(pending["generation_id"], first["generation_id"])
        (self.vault / "note.md").write_text("generation two\n", encoding="utf-8")
        second = publish_generation(
            self.cfg,
            self.sync_config,
            now=NOW,
        )
        self.assertNotEqual(second["generation_id"], first["generation_id"])

        retried = publish_pending_receipts(
            self.sync_config,
            second,
            now=NOW,
        )
        row = list_ledger_events(self.sync_config)[0]
        receipt_path = next(
            (self.root / "published" / "v1" / "receipts").rglob("*.json")
        )
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))

        self.assertEqual(retried["published"], 1)
        self.assertEqual(receipt["canonical_generation"], first["generation"])
        self.assertEqual(receipt["generation_id"], first["generation_id"])
        self.assertEqual(row["canonical_generation"], first["generation"])
        self.assertEqual(row["status"], "applied")

    def test_receipt_queries_are_limited_and_finalized_repairs_rotate_persistently(self):
        transcript = self._produce(["receipt sequence one"])
        for index in range(2, 6):
            self._append(transcript, f"receipt sequence {index}")
            collect_transcripts(self.producer_config, now=NOW)
        reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )

        pending = beacon_sync_reducer.pending_receipt_events(
            self.sync_config,
            limit=2,
        )
        self.assertEqual(len(pending), 2)
        self.assertTrue(pending.limited)
        generation_id = "generation-" + ("a" * 64)
        bound = beacon_sync_reducer.bind_pending_receipt_generation(
            self.sync_config,
            1,
            generation_id,
            [],
            limit=2,
        )
        self.assertEqual(bound, 2)
        self.assertTrue(bound.limited)

        connection = beacon_sync_reducer._open_ledger(self.authority_state)
        try:
            connection.execute(
                """
                update events
                   set status = 'applied',
                       canonical_generation = 1,
                       generation_id = ?
                """,
                (generation_id,),
            )
            connection.commit()
        finally:
            connection.close()

        first = beacon_sync_reducer.bound_receipt_events(
            self.sync_config,
            limit=2,
        )
        second = beacon_sync_reducer.bound_receipt_events(
            self.sync_config,
            limit=2,
        )
        first_keys = {
            (row["producer_instance_id"], row["seq"]) for row in first
        }
        second_keys = {
            (row["producer_instance_id"], row["seq"]) for row in second
        }
        self.assertEqual(len(first), 2)
        self.assertEqual(len(second), 2)
        self.assertTrue(first.limited)
        self.assertTrue(second.limited)
        self.assertTrue(first_keys.isdisjoint(second_keys))

        connection = beacon_sync_reducer._open_ledger(self.authority_state)
        try:
            connection.execute(
                """
                update events
                   set status = 'applied_pending_publish'
                 where seq = (select max(seq) from events)
                """
            )
            connection.commit()
        finally:
            connection.close()
        prioritized = beacon_sync_reducer.bound_receipt_events(
            self.sync_config,
            limit=2,
        )
        self.assertEqual(prioritized[0]["status"], "applied_pending_publish")

    def test_event_reduced_after_seal_is_not_receipted_by_older_generation(self):
        self.sync_config["published_dir"] = str(self.root / "published")
        transcript = self._produce(["before seal"])
        reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )
        sealed = publish_generation(
            self.cfg,
            self.sync_config,
            now=NOW,
        )

        self._append(transcript, "after seal")
        collect_transcripts(self.producer_config, now=NOW)
        reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )
        result = publish_pending_receipts(
            self.sync_config,
            sealed,
            now=NOW,
        )
        rows = list_ledger_events(self.sync_config)

        self.assertEqual(result["published"], 1)
        self.assertEqual(rows[0]["status"], "applied")
        self.assertEqual(
            rows[0]["canonical_generation"],
            sealed["generation"],
        )
        self.assertEqual(rows[1]["status"], "applied_pending_publish")
        self.assertIsNone(rows[1]["canonical_generation"])
        self.assertEqual(rows[1]["generation_id"], "")

    def test_authority_cycle_blocks_reduction_between_snapshot_and_binding(self):
        self.sync_config["published_dir"] = str(self.root / "published")
        transcript = self._produce(["first"])
        reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=RecordingHarvester(),
            now=NOW,
        )
        self._append(transcript, "second")
        collect_transcripts(self.producer_config, now=NOW)
        scan_started = threading.Event()
        release_scan = threading.Event()
        reducer_started = threading.Event()
        reducer_done = threading.Event()
        failures = []
        real_collect = beacon_sync_snapshot._collect_vault_files

        def block_after_snapshot_scan(*args, **kwargs):
            files = real_collect(*args, **kwargs)
            scan_started.set()
            if not release_scan.wait(timeout=5):
                raise TimeoutError("test did not release snapshot scan")
            return files

        def publish_worker():
            try:
                publish_generation(
                    self.cfg,
                    self.sync_config,
                    now=NOW,
                )
            except Exception as exc:
                failures.append(exc)

        def reduce_worker():
            reducer_started.set()
            try:
                reduce_inboxes(
                    self.cfg,
                    self.sync_config,
                    harvest_adapter=RecordingHarvester(),
                    now=NOW,
                )
            except Exception as exc:
                failures.append(exc)
            finally:
                reducer_done.set()

        with patch.object(
            beacon_sync_snapshot,
            "_collect_vault_files",
            side_effect=block_after_snapshot_scan,
        ):
            publisher = threading.Thread(target=publish_worker)
            publisher.start()
            self.assertTrue(scan_started.wait(timeout=5))
            reducer = threading.Thread(target=reduce_worker)
            reducer.start()
            self.assertTrue(reducer_started.wait(timeout=5))
            self.assertFalse(reducer_done.wait(timeout=0.2))
            release_scan.set()
            publisher.join(timeout=5)
            reducer.join(timeout=5)

        self.assertFalse(publisher.is_alive())
        self.assertFalse(reducer.is_alive())
        self.assertEqual(failures, [])
        rows = list_ledger_events(self.sync_config)
        self.assertEqual(rows[0]["canonical_generation"], 1)
        self.assertIsNone(rows[1]["canonical_generation"])

    def test_same_batch_sequence_conflict_blocks_before_harvest(self):
        self._produce(["first candidate"])
        original = self._bundles()[0]
        conflicting = original.with_name("conflicting-copy")
        shutil.copytree(original, conflicting)
        event = self._event(conflicting)
        event["source_cursor"] = {
            "start": event["source_cursor"]["start"],
            "end": event["source_cursor"]["end"] + 1,
        }
        event["payload"]["bytes"] += 1
        conflicting_payload = b"x" * event["payload"]["bytes"]
        event["payload"]["sha256"] = hashlib.sha256(
            conflicting_payload
        ).hexdigest()
        event["event_id"] = derive_event_id(event)
        self._rewrite_event_and_ready(conflicting, event)
        payload = conflicting.with_name(
            f"{event['seq']:020d}-{event['event_id']}"
        ) / "objects" / event["payload"]["sha256"]
        payload.parent.mkdir(exist_ok=True)
        payload.write_bytes(conflicting_payload)
        harvester = RecordingHarvester()

        result = reduce_inboxes(
            self.cfg,
            self.sync_config,
            harvest_adapter=harvester,
            now=NOW,
        )

        self.assertGreaterEqual(result["blocked"], 1)
        self.assertEqual(result["applied"], 0)
        self.assertEqual(harvester.calls, [])
        self.assertEqual(list_ledger_events(self.sync_config), [])

    def _retarget_outbox_device(self, outbox, producer, device_id, *, seq):
        identity_paths = [outbox / "v1" / "identity.json"]
        identity_paths.extend(
            sorted((outbox / "v1" / "identities").glob("*.json"))
        )
        for identity_path in identity_paths:
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
            identity["device_id"] = device_id
            identity_path.write_bytes(canonical_json_bytes(identity))

        producer_root = outbox / "v1" / "events" / producer
        bundle = next(
            path.parent
            for path in producer_root.rglob("ready.json")
            if json.loads(
                (path.parent / "event.json").read_text(encoding="utf-8")
            )["seq"]
            == seq
        )
        event = json.loads((bundle / "event.json").read_text(encoding="utf-8"))
        event["device_id"] = device_id
        event["event_id"] = derive_event_id(event)
        event_bytes = canonical_json_bytes(event)
        (bundle / "event.json").write_bytes(event_bytes)
        (bundle / "ready.json").write_bytes(
            canonical_json_bytes(build_ready(event, event_bytes))
        )
        bundle.rename(
            bundle.with_name(f"{event['seq']:020d}-{event['event_id']}")
        )

    def _create_v1_ledger(self, path, *, missing=None):
        event_id = (
            "event_id text not null"
            if missing == "event_id_unique"
            else "event_id text not null unique"
        )
        events_primary_key = (
            ""
            if missing == "events_primary_key"
            else ", primary key (producer_instance_id, seq)"
        )
        connection = sqlite3.connect(path)
        connection.executescript(
            f"""
            create table metadata (
                key text primary key,
                value text not null
            );
            insert into metadata values ('schema_version', '1');
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
                {event_id},
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
                created_at text not null,
                processed_at text not null default '',
                canonical_generation integer,
                generation_id text not null default ''
                {events_primary_key}
            );
            """
        )
        connection.commit()
        connection.close()

    def _create_ledger_version(self, path, version):
        self._create_v1_ledger(path)
        connection = sqlite3.connect(path)
        try:
            if version >= 3:
                for name, declaration in (
                    ("canonical_path", "text not null default ''"),
                    ("metadata_path", "text not null default ''"),
                    ("payload_sha256", "text not null default ''"),
                    ("payload_bytes", "integer not null default 0"),
                ):
                    connection.execute(
                        f"alter table events add column {name} {declaration}"
                    )
            if version >= 4:
                for name, declaration in (
                    ("metadata_sha256", "text not null default ''"),
                    ("metadata_bytes", "integer not null default 0"),
                ):
                    connection.execute(
                        f"alter table events add column {name} {declaration}"
                    )
            connection.execute(
                "update metadata set value = ? where key = 'schema_version'",
                (str(version),),
            )
            connection.executemany(
                "insert into metadata(key, value) values (?, ?)",
                (
                    (f"seed-{index:04d}", "x" * 1024)
                    for index in range(512)
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def _produce(self, messages):
        path = self.sessions / "session-a.jsonl"
        records = [
            {
                "type": "session_meta",
                "timestamp": "2026-07-31T11:00:00Z",
                "payload": {
                    "id": "session-a",
                    "cwd": "C:\\work\\demo",
                    "timestamp": "2026-07-31T11:00:00Z",
                },
            }
        ]
        records.extend(self._message_record(message) for message in messages)
        path.write_bytes(
            b"".join(
                json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"
                for record in records
            )
        )
        collect_transcripts(
            self.producer_config,
            include_existing=True,
            now=NOW,
        )
        return path

    def _produce_attachment(self, name, data):
        attachment = self.attachments / name
        attachment.write_bytes(data)
        path = self.sessions / "attachment-session.jsonl"
        records = [
            {
                "type": "session_meta",
                "timestamp": "2026-07-31T11:00:00Z",
                "payload": {
                    "id": "attachment-session",
                    "cwd": str(self.attachments),
                    "timestamp": "2026-07-31T11:00:00Z",
                },
            },
            {
                "type": "event_msg",
                "timestamp": "2026-07-31T11:00:01Z",
                "payload": {
                    "type": "user_message",
                    "message": "请读取附件",
                    "local_images": [str(attachment)],
                    "local_audio": [],
                    "images": [],
                    "text_elements": [],
                    "client_id": "test",
                },
            },
        ]
        path.write_bytes(
            b"".join(
                json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"
                for record in records
            )
        )
        collect_transcripts(
            self.producer_config,
            include_existing=True,
            now=NOW,
        )
        return attachment, path

    def _append(self, path, text):
        with open(path, "ab") as handle:
            handle.write(
                json.dumps(
                    self._message_record(text),
                    ensure_ascii=False,
                ).encode("utf-8")
                + b"\n"
            )

    def _message_record(self, text):
        return {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        }

    def _bundles(self):
        return sorted(
            path.parent
            for path in (self.outbox / "v1" / "events").rglob("ready.json")
        )

    def _event(self, bundle):
        return json.loads((bundle / "event.json").read_text(encoding="utf-8"))

    def _events(self):
        return [self._event(bundle) for bundle in self._bundles()]

    def _rewrite_event_and_ready(self, bundle, event, allow_unknown_kind=False):
        event_bytes = canonical_json_bytes(event)
        (bundle / "event.json").write_bytes(event_bytes)
        if event.get("schema_version") == 1:
            ready = build_ready(
                event,
                event_bytes,
                allow_unknown_kind=allow_unknown_kind,
            )
        else:
            ready = json.loads(
                (bundle / "ready.json").read_text(encoding="utf-8")
            )
            ready["event_sha256"] = hashlib.sha256(event_bytes).hexdigest()
        (bundle / "ready.json").write_bytes(canonical_json_bytes(ready))
        renamed = bundle.with_name(f"{event['seq']:020d}-{event['event_id']}")
        if renamed != bundle:
            bundle.rename(renamed)


if __name__ == "__main__":
    unittest.main()
