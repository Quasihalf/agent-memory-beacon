import errno
import hashlib
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import beacon_sync_producer
from beacon_sync_producer import (
    ProducerError,
    collect_transcripts,
    garbage_collect_outbox,
    initialize_producer,
    load_producer_state,
)
from beacon_sync_protocol import ProtocolError, canonical_json_bytes, derive_event_id
from beacon_sync_snapshot import publish_generation
from beacon_sync_snapshot import materialize_generation


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


class BeaconSyncProducerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.attachments = self.root / "attachments"
        self.attachments.mkdir()
        self.state_dir = self.root / "state"
        self.outbox = self.root / "outbox"
        self.receipts = self.root / "received-published" / "v1" / "receipts"
        self.config = {
            "device_id": "windows-gpu-test",
            "state_dir": str(self.state_dir),
            "outbox_dir": str(self.outbox),
            "received_published_dir": str(self.receipts.parent.parent),
            "replica_path": str(self.root / "replica"),
            "transcript_paths": [str(self.sessions)],
            "codex_sessions_path": "",
            "claude_project_path": "",
            "attachment_roots": [str(self.attachments)],
            "max_chunk_bytes": 4096,
            "max_gap_bytes": 4096,
            "max_attachment_bytes": 4096,
            "max_events_per_run": 32,
            "gc_retention_seconds": 0,
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_first_normal_collection_baselines_existing_transcripts(self):
        transcript = self._write_codex_transcript("existing.jsonl", ["before install"])

        result = collect_transcripts(self.config, now=NOW)
        state = load_producer_state(self.config)

        self.assertEqual(result["emitted"], 0)
        self.assertEqual(result["baselined"], 1)
        source = next(iter(state["sources"].values()))
        self.assertEqual(source["cursor"], transcript.stat().st_size)
        self.assertEqual(list(self.outbox.rglob("ready.json")), [])

    def test_include_existing_emits_initial_event_and_ready_is_last_commit(self):
        transcript = self._write_codex_transcript("initial.jsonl", ["record me"])

        result = collect_transcripts(
            self.config,
            include_existing=True,
            now=NOW,
        )

        self.assertEqual(result["emitted"], 1)
        bundle = self._bundles()[0]
        event = json.loads((bundle / "event.json").read_text(encoding="utf-8"))
        ready = json.loads((bundle / "ready.json").read_text(encoding="utf-8"))
        payload_path = bundle / "objects" / event["payload"]["sha256"]
        self.assertTrue(payload_path.is_file())
        self.assertEqual(payload_path.stat().st_size, transcript.stat().st_size)
        self.assertEqual(ready["event_id"], event["event_id"])
        self.assertEqual(event["seq"], 1)
        self.assertEqual(bundle.parent.name, "seq-00000000000000000001")

    def test_append_emits_only_new_complete_records_with_next_sequence(self):
        transcript = self._write_codex_transcript("append.jsonl", ["baseline"])
        collect_transcripts(self.config, now=NOW)
        baseline = transcript.stat().st_size
        self._append_message(transcript, "after baseline")

        result = collect_transcripts(self.config, now=NOW + timedelta(minutes=1))
        event = self._events()[0]
        payload = (
            self._bundles()[0] / "objects" / event["payload"]["sha256"]
        ).read_bytes()

        self.assertEqual(result["emitted"], 1)
        self.assertEqual(event["source_cursor"]["start"], baseline)
        self.assertEqual(event["source_cursor"]["end"], transcript.stat().st_size)
        self.assertIn(b"after baseline", payload)
        self.assertEqual(event["seq"], 1)

    def test_new_transcript_after_baseline_is_emitted_from_zero(self):
        collect_transcripts(self.config, now=NOW)
        transcript = self._write_codex_transcript("new.jsonl", ["new session"])

        collect_transcripts(self.config, now=NOW + timedelta(seconds=1))

        event = self._events()[0]
        self.assertEqual(event["source_cursor"], {"start": 0, "end": transcript.stat().st_size})

    def test_retry_after_ready_before_state_commit_reuses_event_and_sequence(self):
        transcript = self._write_codex_transcript("retry.jsonl", ["retry me"])

        with self.assertRaisesRegex(ProducerError, "injected"):
            collect_transcripts(
                self.config,
                include_existing=True,
                now=NOW,
                fault_point="after_ready",
            )
        first_bundle = self._bundles()[0]
        first_event_bytes = (first_bundle / "event.json").read_bytes()

        result = collect_transcripts(
            self.config,
            include_existing=True,
            now=NOW,
        )

        self.assertEqual(result["emitted"], 0)
        self.assertEqual(len(self._bundles()), 1)
        self.assertEqual((first_bundle / "event.json").read_bytes(), first_event_bytes)
        state = load_producer_state(self.config)
        self.assertEqual(state["next_seq"], 2)
        self.assertIsNone(state["pending_event"])
        self.assertEqual(
            next(iter(state["sources"].values()))["cursor"],
            transcript.stat().st_size,
        )

    def test_ready_bundle_recovery_does_not_depend_on_mutable_source(self):
        transcript = self._write_codex_transcript("durable-ready.jsonl", ["durable"])

        with self.assertRaisesRegex(ProducerError, "injected"):
            collect_transcripts(
                self.config,
                include_existing=True,
                now=NOW,
                fault_point="after_ready",
            )
        transcript.unlink()

        recovered = collect_transcripts(
            self.config,
            include_existing=True,
            now=NOW + timedelta(seconds=1),
        )

        self.assertEqual(recovered["recovered"], 1)
        state = load_producer_state(self.config)
        self.assertEqual(state["next_seq"], 2)
        self.assertIsNone(state["pending_event"])
        self.assertEqual(len(self._bundles()), 1)

    def test_allocated_chunk_recovery_does_not_depend_on_source_file(self):
        transcript = self._write_codex_transcript(
            "allocated-chunk.jsonl",
            ["durable before pending state"],
        )

        with self.assertRaisesRegex(ProducerError, "after_allocation_state"):
            collect_transcripts(
                self.config,
                include_existing=True,
                now=NOW,
                fault_point="after_allocation_state",
            )
        transcript.unlink()

        recovered = collect_transcripts(
            self.config,
            include_existing=True,
            now=NOW + timedelta(seconds=1),
        )

        self.assertEqual(recovered["recovered"], 1)
        self.assertEqual(len(self._bundles()), 1)
        event = self._events()[0]
        self.assertIn(b"durable before pending state", self._payload_for(event))
        state = load_producer_state(self.config)
        self.assertEqual(state["next_seq"], 2)
        self.assertIsNone(state["pending_event"])

    def test_allocated_gap_recovery_does_not_depend_on_source_file(self):
        self.config["max_chunk_bytes"] = 128
        transcript = self._write_codex_transcript(
            "allocated-gap.jsonl",
            ["oversized-" * 100],
        )

        with self.assertRaisesRegex(ProducerError, "after_allocation_state"):
            collect_transcripts(
                self.config,
                include_existing=True,
                now=NOW,
                fault_point="after_allocation_state",
            )
        transcript.unlink()

        recovered = collect_transcripts(
            self.config,
            include_existing=True,
            now=NOW + timedelta(seconds=1),
        )

        self.assertEqual(recovered["recovered"], 1)
        event = self._events()[0]
        self.assertEqual(event["event_kind"], "transcript.gap")
        self.assertEqual(list(self._bundles()[0].glob("objects/*")), [])

    def test_truncation_creates_new_stream_epoch_without_cursor_regression(self):
        transcript = self._write_codex_transcript("truncate.jsonl", ["first content"])
        collect_transcripts(self.config, include_existing=True, now=NOW)
        first = self._events()[0]
        transcript.write_text("", encoding="utf-8")
        self._append_message(transcript, "replacement")

        collect_transcripts(self.config, now=NOW + timedelta(minutes=1))

        second = self._events()[1]
        self.assertEqual(second["seq"], 2)
        self.assertNotEqual(second["stream_epoch"], first["stream_epoch"])
        self.assertEqual(second["source_cursor"]["start"], 0)

    def test_empty_baseline_later_detects_same_path_replacement(self):
        transcript = self.sessions / "empty-then-replaced.jsonl"
        transcript.write_bytes(b"")
        collect_transcripts(self.config, now=NOW)

        self._append_message(transcript, "first append")
        collect_transcripts(self.config, now=NOW + timedelta(minutes=1))
        first = self._events()[0]
        first_size = transcript.stat().st_size

        replacement = (
            self._codex_record("replacement content is deliberately longer")
            + b"\n"
            + self._codex_record("second replacement record")
            + b"\n"
        )
        self.assertGreaterEqual(len(replacement), first_size)
        transcript.write_bytes(replacement)
        collect_transcripts(self.config, now=NOW + timedelta(minutes=2))

        second = self._events()[1]
        self.assertEqual(second["source_cursor"]["start"], 0)
        self.assertNotEqual(second["stream_epoch"], first["stream_epoch"])
        self.assertIn(b"replacement content", self._payload_for(second))

    def test_replacement_with_same_prefix_and_larger_size_starts_new_epoch(self):
        self.config["max_chunk_bytes"] = 16 * 1024
        self.config["max_gap_bytes"] = 16 * 1024
        prefix = "p" * 5000
        transcript = self._write_codex_transcript(
            "same-prefix.jsonl",
            [prefix + " original tail"],
        )
        collect_transcripts(self.config, include_existing=True, now=NOW)
        first = self._events()[0]
        original_size = transcript.stat().st_size

        replacement = self._write_codex_transcript(
            "same-prefix.jsonl",
            [prefix + " replacement tail " + ("x" * 1000)],
        )
        self.assertGreaterEqual(replacement.stat().st_size, original_size)
        collect_transcripts(self.config, now=NOW + timedelta(minutes=1))

        second = self._events()[1]
        self.assertEqual(second["source_cursor"]["start"], 0)
        self.assertNotEqual(second["stream_epoch"], first["stream_epoch"])
        self.assertIn(b"replacement tail", self._payload_for(second))

    def test_unicode_session_id_is_hashed_to_protocol_safe_ascii(self):
        transcript = self.sessions / "unicode-session.jsonl"
        records = [
            {
                "type": "session_meta",
                "timestamp": "2026-07-31T11:00:00Z",
                "payload": {
                    "id": "中文会话",
                    "cwd": "C:\\work\\demo",
                    "timestamp": "2026-07-31T11:00:00Z",
                },
            },
            self._message_record("record me"),
        ]
        transcript.write_bytes(
            b"".join(
                json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"
                for record in records
            )
        )

        collect_transcripts(self.config, include_existing=True, now=NOW)

        session_id = self._events()[0]["session_id"]
        self.assertTrue(session_id.startswith("session-"))
        self.assertTrue(session_id.isascii())

    def test_direct_producer_config_rejects_unicode_device_id(self):
        self.config["device_id"] = "设备-one"

        with self.assertRaisesRegex(ProducerError, "device_id"):
            initialize_producer(self.config, now=NOW)

    def test_load_producer_state_rejects_noncanonical_instance_id_aliases(self):
        initialize_producer(self.config, now=NOW)
        state_path = self.state_dir / "producer-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        canonical_id = "123e4567-e89b-12d3-a456-426614174000"
        aliases = {
            "uppercase": canonical_id.upper(),
            "compact": canonical_id.replace("-", ""),
        }

        for alias_kind, alias in aliases.items():
            with self.subTest(alias_kind=alias_kind):
                state["producer_instance_id"] = alias
                state_path.write_bytes(canonical_json_bytes(state))

                with self.assertRaisesRegex(ProducerError, "producer instance ID"):
                    load_producer_state(self.config)

    def test_initialize_rejects_noncanonical_outbox_identity_aliases(self):
        initialize_producer(self.config, now=NOW)
        identity_path = self.outbox / "v1" / "identity.json"
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        canonical_id = "123e4567-e89b-12d3-a456-426614174000"
        aliases = {
            "uppercase": canonical_id.upper(),
            "compact": canonical_id.replace("-", ""),
        }

        for alias_kind, alias in aliases.items():
            with self.subTest(alias_kind=alias_kind):
                identity["producer_instance_id"] = alias
                invalid_identity = canonical_json_bytes(identity)
                identity_path.write_bytes(invalid_identity)

                with self.assertRaisesRegex(
                    ProducerError,
                    "existing outbox identity is invalid",
                ):
                    initialize_producer(self.config, now=NOW)
                self.assertEqual(identity_path.read_bytes(), invalid_identity)

    def test_reinitialization_rotates_identity_without_discarding_old_outbox(self):
        self._write_codex_transcript("old-instance.jsonl", ["keep old event"])
        collect_transcripts(
            self.config,
            include_existing=True,
            now=NOW,
        )
        old_event = self._events()[0]
        (self.state_dir / "producer-state.json").unlink()

        initialized = initialize_producer(
            self.config,
            now=NOW + timedelta(seconds=1),
        )

        self.assertNotEqual(
            initialized["producer_instance_id"],
            old_event["producer_instance_id"],
        )
        current = json.loads(
            (self.outbox / "v1" / "identity.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            current["producer_instance_id"],
            initialized["producer_instance_id"],
        )
        identities = {
            path.stem
            for path in (self.outbox / "v1" / "identities").glob("*.json")
        }
        self.assertEqual(
            identities,
            {
                old_event["producer_instance_id"],
                initialized["producer_instance_id"],
            },
        )
        self.assertEqual(len(self._bundles()), 1)

    def test_multiple_transcripts_use_one_global_sequence(self):
        self._write_codex_transcript("a.jsonl", ["a"])
        self._write_codex_transcript("b.jsonl", ["b"])

        result = collect_transcripts(
            self.config,
            include_existing=True,
            now=NOW,
        )

        self.assertEqual(result["emitted"], 2)
        self.assertEqual([event["seq"] for event in self._events()], [1, 2])

    def test_chunk_ends_at_newline_and_remaining_bytes_wait(self):
        path = self.sessions / "partial.jsonl"
        complete = self._codex_record("complete") + b"\n"
        partial = self._codex_record("partial")
        path.write_bytes(complete + partial)

        collect_transcripts(self.config, include_existing=True, now=NOW)

        event = self._events()[0]
        self.assertEqual(event["source_cursor"]["end"], len(complete))
        self.assertEqual(
            next(iter(load_producer_state(self.config)["sources"].values()))["cursor"],
            len(complete),
        )
        self.assertNotIn(b"partial", self._payload_for(event))

    def test_oversized_record_emits_gap_without_transporting_content(self):
        self.config["max_chunk_bytes"] = 256
        huge_text = "x" * 500
        transcript = self._write_codex_transcript("huge.jsonl", [huge_text])
        huge_record = self._codex_record(huge_text) + b"\n"

        collect_transcripts(self.config, include_existing=True, now=NOW)

        event = next(
            item for item in self._events() if item["event_kind"] == "transcript.gap"
        )
        self.assertEqual(event["event_kind"], "transcript.gap")
        self.assertEqual(event["payload"]["bytes"], len(huge_record))
        self.assertEqual(
            event["payload"]["sha256"],
            hashlib.sha256(huge_record).hexdigest(),
        )
        bundle = next(
            path for path in self._bundles() if path.name.endswith(event["event_id"])
        )
        self.assertEqual(list(bundle.glob("objects/*")), [])

    def test_codex_user_attachment_emits_blob_after_transcript_event(self):
        image = self.attachments / "diagram.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\npayload")
        transcript = self.sessions / "attachment.jsonl"
        transcript.write_bytes(
            self._session_meta("attachment")
            + self._user_message_record(
                "请查看图片",
                local_images=[str(image)],
            )
        )

        result = collect_transcripts(
            self.config,
            include_existing=True,
            now=NOW,
        )
        events = self._events()
        attachment = events[1]

        self.assertEqual(result["emitted"], 2)
        self.assertEqual(result["attachments_emitted"], 1)
        self.assertEqual(
            [event["event_kind"] for event in events],
            ["transcript.chunk", "attachment.blob"],
        )
        self.assertEqual(attachment["payload"]["role"], "attachment-source")
        self.assertEqual(attachment["schema_version"], 2)
        self.assertEqual(self._payload_for(attachment), image.read_bytes())
        metadata = attachment["extensions"]["attachment"]
        self.assertEqual(metadata["original_name"], "diagram.png")
        self.assertEqual(metadata["reference_kind"], "codex.local_image")
        self.assertEqual(
            set(metadata),
            {
                "reference_id",
                "original_name",
                "source_locator_sha256",
                "reference_kind",
            },
        )
        bundle = next(
            path
            for path in self._bundles()
            if path.name.endswith(attachment["event_id"])
        )
        ready = json.loads((bundle / "ready.json").read_text(encoding="utf-8"))
        self.assertEqual(ready["schema_version"], 2)
        self.assertNotIn(str(image), canonical_json_bytes(attachment).decode("ascii"))

    def test_files_mentioned_block_is_captured_but_outside_root_is_rejected(self):
        document = self.attachments / "source.pdf"
        document.write_bytes(b"%PDF-1.7\ncontent")
        outside = self.root / "outside.pdf"
        outside.write_bytes(b"%PDF-1.7\noutside")
        message = (
            "# Files mentioned by the user:\n\n"
            f"## source.pdf: {document}\n"
            f"## outside.pdf: {outside}\n\n"
            "## My request for Codex:\n请阅读"
        )
        transcript = self.sessions / "files-mentioned.jsonl"
        transcript.write_bytes(
            self._session_meta("files-mentioned")
            + self._user_message_record(message)
        )

        result = collect_transcripts(
            self.config,
            include_existing=True,
            now=NOW,
        )
        attachments = [
            event for event in self._events()
            if event["event_kind"] == "attachment.blob"
        ]

        self.assertEqual(len(attachments), 1)
        self.assertEqual(
            attachments[0]["extensions"]["attachment"]["original_name"],
            "source.pdf",
        )
        self.assertEqual(
            attachments[0]["extensions"]["attachment"]["reference_kind"],
            "user.file_mention",
        )
        self.assertEqual(result["attachments_rejected"], 1)

    def test_duplicate_attachment_references_emit_one_blob_event(self):
        image = self.attachments / "same.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nsame")
        message = (
            "# Files mentioned by the user:\n\n"
            f"## same.png: {image}\n\n"
            "## My request for Codex:\n检查"
        )
        transcript = self.sessions / "dedupe-attachment.jsonl"
        transcript.write_bytes(
            self._session_meta("dedupe-attachment")
            + self._user_message_record(
                message,
                local_images=[str(image), str(image)],
            )
        )

        collect_transcripts(self.config, include_existing=True, now=NOW)

        attachments = [
            event for event in self._events()
            if event["event_kind"] == "attachment.blob"
        ]
        self.assertEqual(len(attachments), 1)

    def test_same_attachment_in_two_records_has_two_reference_events(self):
        image = self.attachments / "two-records.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\ntwo records")
        transcript = self.sessions / "two-records.jsonl"
        transcript.write_bytes(
            self._session_meta("two-records")
            + self._user_message_record("第一条", local_images=[str(image)])
            + self._user_message_record("第二条", local_images=[str(image)])
        )

        collect_transcripts(self.config, include_existing=True, now=NOW)

        attachments = [
            event
            for event in self._events()
            if event["event_kind"] == "attachment.blob"
        ]
        self.assertEqual(len(attachments), 2)
        self.assertEqual(
            len(
                {
                    event["extensions"]["attachment"]["reference_id"]
                    for event in attachments
                }
            ),
            2,
        )
        self.assertEqual(
            len(
                {
                    (
                        event["source_cursor"]["start"],
                        event["source_cursor"]["end"],
                    )
                    for event in attachments
                }
            ),
            2,
        )
        self.assertEqual(
            {event["payload"]["sha256"] for event in attachments},
            {hashlib.sha256(image.read_bytes()).hexdigest()},
        )

    def test_same_attachment_in_two_sessions_has_two_reference_events(self):
        image = self.attachments / "two-sessions.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\ntwo sessions")
        for session_id in ("session-a", "session-b"):
            transcript = self.sessions / f"{session_id}.jsonl"
            transcript.write_bytes(
                self._session_meta(session_id)
                + self._user_message_record(
                    "同一附件",
                    local_images=[str(image)],
                )
            )

        collect_transcripts(self.config, include_existing=True, now=NOW)

        attachments = [
            event
            for event in self._events()
            if event["event_kind"] == "attachment.blob"
        ]
        self.assertEqual(len(attachments), 2)
        self.assertEqual(
            len(
                {
                    event["extensions"]["attachment"]["reference_id"]
                    for event in attachments
                }
            ),
            2,
        )
        self.assertEqual(len({event["stream_id"] for event in attachments}), 2)

    def test_attachment_queue_survives_event_budget_and_source_deletion(self):
        image = self.attachments / "queued.png"
        image_bytes = b"\x89PNG\r\n\x1a\nqueued"
        image.write_bytes(image_bytes)
        transcript = self.sessions / "queued-attachment.jsonl"
        transcript.write_bytes(
            self._session_meta("queued-attachment")
            + self._user_message_record(
                "稍后发送",
                local_images=[str(image)],
            )
        )
        self.config["max_events_per_run"] = 1

        first = collect_transcripts(
            self.config,
            include_existing=True,
            now=NOW,
        )
        image.unlink()
        second = collect_transcripts(
            self.config,
            now=NOW + timedelta(seconds=1),
        )

        self.assertEqual(first["attachments_emitted"], 0)
        self.assertTrue(first["limited"])
        self.assertEqual(second["attachments_emitted"], 1)
        attachment = self._events()[1]
        self.assertEqual(attachment["event_kind"], "attachment.blob")
        self.assertEqual(self._payload_for(attachment), image_bytes)

    def test_attachment_capture_journal_survives_crash_before_state_commit(self):
        image = self.attachments / "capture-journal.png"
        image_bytes = b"\x89PNG\r\n\x1a\ncapture journal"
        image.write_bytes(image_bytes)
        transcript = self.sessions / "capture-journal.jsonl"
        transcript.write_bytes(
            self._session_meta("capture-journal")
            + self._user_message_record(
                "附件捕获后立即崩溃",
                local_images=[str(image)],
            )
        )

        with self.assertRaisesRegex(
            ProducerError,
            "after_attachment_capture_ready",
        ):
            collect_transcripts(
                self.config,
                include_existing=True,
                now=NOW,
                fault_point="after_attachment_capture_ready",
            )
        self.assertEqual(
            len(
                list(
                    (
                        self.state_dir
                        / "attachment-captures"
                        / "v1"
                    ).glob("*/ready.json")
                )
            ),
            1,
        )
        image.unlink()

        recovered = collect_transcripts(
            self.config,
            now=NOW + timedelta(seconds=1),
        )

        self.assertEqual(recovered["emitted"], 2)
        events = self._events()
        self.assertEqual(
            [event["event_kind"] for event in events],
            ["transcript.chunk", "attachment.blob"],
        )
        self.assertEqual(self._payload_for(events[1]), image_bytes)
        capture_root = self.state_dir / "attachment-captures"
        self.assertEqual(
            list(capture_root.rglob("ready.json"))
            if capture_root.is_dir()
            else [],
            [],
        )

    def test_attachment_persistence_oserror_aborts_transcript_and_recovers(self):
        for stage in ("payload", "capture", "cas", "ready"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                sessions = root / "sessions"
                sessions.mkdir()
                attachments = root / "attachments"
                attachments.mkdir()
                state_dir = root / "state"
                outbox = root / "outbox"
                config = {
                    **self.config,
                    "state_dir": str(state_dir),
                    "outbox_dir": str(outbox),
                    "received_published_dir": str(root / "received"),
                    "replica_path": str(root / "replica"),
                    "transcript_paths": [str(sessions)],
                    "attachment_roots": [str(attachments)],
                }
                image = attachments / f"{stage}.png"
                image_bytes = b"\x89PNG\r\n\x1a\n" + stage.encode("ascii")
                image.write_bytes(image_bytes)
                transcript = sessions / f"{stage}.jsonl"
                transcript.write_bytes(
                    self._session_meta(stage).replace(
                        str(self.attachments).encode("utf-8"),
                        str(attachments).encode("utf-8"),
                    )
                    + self._user_message_record(
                        "persistence failure",
                        local_images=[str(image)],
                    )
                )
                real_write_immutable = beacon_sync_producer.write_immutable
                failed = False

                def fail_after_durable_write(path, data, *, root, mode=0o600):
                    nonlocal failed
                    result = real_write_immutable(
                        path,
                        data,
                        root=root,
                        mode=mode,
                    )
                    candidate = Path(path)
                    capture_root = state_dir / "attachment-captures"
                    cas_root = state_dir / "attachment-cas"
                    matches = {
                        "payload": candidate.name == "payload.bin",
                        "capture": candidate.name == "capture.json",
                        "cas": cas_root in candidate.parents,
                        "ready": (
                            candidate.name == "ready.json"
                            and capture_root in candidate.parents
                        ),
                    }[stage]
                    if matches and not failed:
                        failed = True
                        raise OSError(f"injected {stage} persistence failure")
                    return result

                with patch.object(
                    beacon_sync_producer,
                    "write_immutable",
                    side_effect=fail_after_durable_write,
                ):
                    with self.assertRaisesRegex(OSError, stage):
                        collect_transcripts(
                            config,
                            include_existing=True,
                            now=NOW,
                        )

                state = load_producer_state(config)
                source = next(iter(state["sources"].values()))
                self.assertEqual(source["cursor"], 0)
                self.assertEqual(list(outbox.rglob("ready.json")), [])
                image.unlink()

                recovered = collect_transcripts(
                    config,
                    now=NOW + timedelta(seconds=1),
                )
                bundles = sorted(
                    path.parent for path in outbox.rglob("ready.json")
                )
                events = [
                    json.loads((bundle / "event.json").read_text("utf-8"))
                    for bundle in bundles
                ]
                attachment = next(
                    event
                    for event in events
                    if event["event_kind"] == "attachment.blob"
                )
                attachment_bundle = next(
                    bundle
                    for bundle in bundles
                    if bundle.name.endswith(attachment["event_id"])
                )

                self.assertEqual(recovered["emitted"], 2)
                self.assertEqual(
                    [event["event_kind"] for event in events],
                    ["transcript.chunk", "attachment.blob"],
                )
                self.assertEqual(
                    (
                        attachment_bundle
                        / "objects"
                        / attachment["payload"]["sha256"]
                    ).read_bytes(),
                    image_bytes,
                )

    def test_attachment_payload_journal_survives_process_crash(self):
        class SimulatedCrash(BaseException):
            pass

        image = self.attachments / "payload-crash.png"
        image_bytes = b"\x89PNG\r\n\x1a\npayload crash"
        image.write_bytes(image_bytes)
        transcript = self.sessions / "payload-crash.jsonl"
        transcript.write_bytes(
            self._session_meta("payload-crash")
            + self._user_message_record(
                "payload durable 后崩溃",
                local_images=[str(image)],
            )
        )
        real_write_immutable = beacon_sync_producer.write_immutable

        def crash_after_payload(path, data, *, root, mode=0o600):
            result = real_write_immutable(path, data, root=root, mode=mode)
            if Path(path).name == "payload.bin":
                raise SimulatedCrash("crash after attachment payload")
            return result

        with patch.object(
            beacon_sync_producer,
            "write_immutable",
            side_effect=crash_after_payload,
        ):
            with self.assertRaisesRegex(SimulatedCrash, "attachment payload"):
                collect_transcripts(
                    self.config,
                    include_existing=True,
                    now=NOW,
                )

        payloads = list(
            (
                self.state_dir
                / "attachment-captures"
                / "v1"
            ).glob("*/payload.bin")
        )
        self.assertEqual(len(payloads), 1)
        self.assertFalse((payloads[0].parent / "capture.json").exists())
        image.unlink()

        recovered = collect_transcripts(
            self.config,
            now=NOW + timedelta(seconds=1),
        )

        self.assertEqual(recovered["emitted"], 2)
        attachment = next(
            event
            for event in self._events()
            if event["event_kind"] == "attachment.blob"
        )
        self.assertEqual(self._payload_for(attachment), image_bytes)

    def test_attachment_source_infrastructure_io_failure_aborts_transcript(self):
        image = self.attachments / "source-eio.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nsource eio")
        transcript = self.sessions / "source-eio.jsonl"
        transcript.write_bytes(
            self._session_meta("source-eio")
            + self._user_message_record(
                "source read fails",
                local_images=[str(image)],
            )
        )
        real_read = beacon_sync_producer.read_bounded_regular_file

        def fail_source_read(path, **kwargs):
            if Path(path) == image:
                try:
                    raise OSError(errno.EIO, "injected attachment source I/O")
                except OSError as exc:
                    raise ProtocolError(str(exc)) from exc
            return real_read(path, **kwargs)

        with patch.object(
            beacon_sync_producer,
            "read_bounded_regular_file",
            side_effect=fail_source_read,
        ):
            with self.assertRaisesRegex(ProducerError, "source.*I/O"):
                collect_transcripts(
                    self.config,
                    include_existing=True,
                    now=NOW,
                )

        state = load_producer_state(self.config)
        self.assertEqual(next(iter(state["sources"].values()))["cursor"], 0)
        self.assertEqual(self._bundles(), [])

        recovered = collect_transcripts(
            self.config,
            now=NOW + timedelta(seconds=1),
        )

        self.assertEqual(recovered["emitted"], 2)

    def test_capture_intent_recovers_after_crash_before_cas_and_source_deletion(self):
        class SimulatedCrash(BaseException):
            pass

        image = self.attachments / "capture-intent.png"
        image_bytes = b"\x89PNG\r\n\x1a\ncapture intent"
        image.write_bytes(image_bytes)
        transcript = self.sessions / "capture-intent.jsonl"
        transcript.write_bytes(
            self._session_meta("capture-intent")
            + self._user_message_record(
                "capture intent durable 后崩溃",
                local_images=[str(image)],
            )
        )
        real_write_immutable = beacon_sync_producer.write_immutable

        def crash_after_capture(path, data, *, root, mode=0o600):
            result = real_write_immutable(path, data, root=root, mode=mode)
            if Path(path).name == "capture.json":
                raise SimulatedCrash("crash after capture intent")
            return result

        with patch.object(
            beacon_sync_producer,
            "write_immutable",
            side_effect=crash_after_capture,
        ):
            with self.assertRaisesRegex(SimulatedCrash, "capture intent"):
                collect_transcripts(
                    self.config,
                    include_existing=True,
                    now=NOW,
                )

        captures = list(
            (
                self.state_dir
                / "attachment-captures"
                / "v1"
            ).glob("*/capture.json")
        )
        self.assertEqual(len(captures), 1)
        capture = captures[0]
        self.assertTrue((capture.parent / "payload.bin").is_file())
        self.assertFalse((capture.parent / "ready.json").exists())
        image.unlink()

        recovered = collect_transcripts(
            self.config,
            now=NOW + timedelta(seconds=1),
        )

        self.assertEqual(recovered["emitted"], 2)
        attachment = next(
            event
            for event in self._events()
            if event["event_kind"] == "attachment.blob"
        )
        self.assertEqual(self._payload_for(attachment), image_bytes)

    def test_capture_cas_recovers_after_crash_before_ready_and_source_deletion(self):
        class SimulatedCrash(BaseException):
            pass

        image = self.attachments / "capture-cas.png"
        image_bytes = b"\x89PNG\r\n\x1a\ncapture cas"
        image.write_bytes(image_bytes)
        transcript = self.sessions / "capture-cas.jsonl"
        transcript.write_bytes(
            self._session_meta("capture-cas")
            + self._user_message_record(
                "CAS durable 后崩溃",
                local_images=[str(image)],
            )
        )
        real_write_immutable = beacon_sync_producer.write_immutable

        def crash_after_cas(path, data, *, root, mode=0o600):
            result = real_write_immutable(path, data, root=root, mode=mode)
            candidate = Path(path)
            cas_root = self.state_dir / "attachment-cas"
            if cas_root in candidate.parents:
                raise SimulatedCrash("crash after attachment CAS")
            return result

        with patch.object(
            beacon_sync_producer,
            "write_immutable",
            side_effect=crash_after_cas,
        ):
            with self.assertRaisesRegex(SimulatedCrash, "attachment CAS"):
                collect_transcripts(
                    self.config,
                    include_existing=True,
                    now=NOW,
                )

        captures = list(
            (
                self.state_dir
                / "attachment-captures"
                / "v1"
            ).glob("*/capture.json")
        )
        self.assertEqual(len(captures), 1)
        capture = captures[0]
        self.assertTrue((capture.parent / "payload.bin").is_file())
        self.assertFalse((capture.parent / "ready.json").exists())
        image.unlink()

        recovered = collect_transcripts(
            self.config,
            now=NOW + timedelta(seconds=1),
        )

        self.assertEqual(recovered["emitted"], 2)
        attachment = next(
            event
            for event in self._events()
            if event["event_kind"] == "attachment.blob"
        )
        self.assertEqual(self._payload_for(attachment), image_bytes)

    def test_capture_retirement_unlinks_ready_before_recursive_cleanup(self):
        class SimulatedCrash(BaseException):
            pass

        image = self.attachments / "retire-ready-first.png"
        image_bytes = b"\x89PNG\r\n\x1a\nretire ready first"
        image.write_bytes(image_bytes)
        transcript = self.sessions / "retire-ready-first.jsonl"
        transcript.write_bytes(
            self._session_meta("retire-ready-first")
            + self._user_message_record(
                "退休 capture journal",
                local_images=[str(image)],
            )
        )
        real_rmtree = beacon_sync_producer.portable_rmtree
        ready_states = []

        def crash_during_capture_cleanup(path, *, root):
            candidate = Path(path)
            capture_root = self.state_dir / "attachment-captures" / "v1"
            if capture_root in candidate.parents:
                ready_states.append((candidate / "ready.json").exists())
                raise SimulatedCrash("crash during capture retirement")
            return real_rmtree(path, root=root)

        with patch.object(
            beacon_sync_producer,
            "portable_rmtree",
            side_effect=crash_during_capture_cleanup,
        ):
            with self.assertRaisesRegex(SimulatedCrash, "capture retirement"):
                collect_transcripts(
                    self.config,
                    include_existing=True,
                    now=NOW,
                )

        self.assertEqual(ready_states, [False])
        capture_bundle = next(
            (
                self.state_dir
                / "attachment-captures"
                / "v1"
            ).glob("capture-*")
        )
        self.assertFalse((capture_bundle / "ready.json").exists())

        recovered = collect_transcripts(
            self.config,
            now=NOW + timedelta(seconds=1),
        )

        self.assertEqual(recovered["attachments_emitted"], 1)
        attachment = next(
            event
            for event in self._events()
            if event["event_kind"] == "attachment.blob"
        )
        self.assertEqual(self._payload_for(attachment), image_bytes)

    def test_ready_only_capture_retirement_is_cleaned_on_restart(self):
        image = self.attachments / "ready-only.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nready only")
        transcript = self.sessions / "ready-only.jsonl"
        transcript.write_bytes(
            self._session_meta("ready-only")
            + self._user_message_record(
                "构造历史 ready-only 退休中间态",
                local_images=[str(image)],
            )
        )

        with self.assertRaisesRegex(
            ProducerError,
            "after_attachment_capture_ready",
        ):
            collect_transcripts(
                self.config,
                include_existing=True,
                now=NOW,
                fault_point="after_attachment_capture_ready",
            )
        capture_bundle = next(
            (
                self.state_dir
                / "attachment-captures"
                / "v1"
            ).glob("capture-*")
        )
        (capture_bundle / "capture.json").unlink()
        (capture_bundle / "payload.bin").unlink()
        self.assertEqual(
            [path.name for path in capture_bundle.iterdir()],
            ["ready.json"],
        )

        try:
            initialize_producer(self.config, now=NOW + timedelta(seconds=1))
        except ProducerError as exc:
            self.fail(f"ready-only retirement blocked restart: {exc}")

        self.assertFalse(capture_bundle.exists())

    def test_unreplayable_capture_retires_after_source_truncation(self):
        transcript, digest = self._leave_ready_capture(
            "stale-truncation",
        )
        transcript.write_bytes(
            self._session_meta("stale-truncation")
            + self._codex_record("replacement after truncation")
            + b"\n"
        )

        collect_transcripts(
            self.config,
            now=NOW + timedelta(seconds=1),
        )

        self._assert_capture_storage_retired(digest)

    def test_unreplayable_capture_retires_after_source_inode_change(self):
        transcript, digest = self._leave_ready_capture(
            "stale-inode",
        )
        replacement = transcript.with_suffix(".replacement")
        replacement.write_bytes(
            self._session_meta("stale-inode")
            + self._codex_record("replacement inode")
            + b"\n"
        )
        os.replace(replacement, transcript)

        collect_transcripts(
            self.config,
            now=NOW + timedelta(seconds=1),
        )

        self._assert_capture_storage_retired(digest)

    def test_unreplayable_capture_retires_after_session_identity_change(self):
        transcript, digest = self._leave_ready_capture(
            "stale-session-before",
        )
        transcript.write_bytes(
            self._session_meta("stale-session-after")
            + self._codex_record("replacement session")
            + b"\n"
        )

        collect_transcripts(
            self.config,
            now=NOW + timedelta(seconds=1),
        )

        self._assert_capture_storage_retired(digest)

    def test_unreplayable_capture_retires_before_journal_limit_blocks(self):
        _transcript, digest = self._leave_ready_capture(
            "stale-before-limit",
        )
        state_path = self.state_dir / "producer-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        source = next(iter(state["sources"].values()))
        source["stream_epoch"] = "11111111-1111-4111-8111-111111111111"
        state_path.write_bytes(canonical_json_bytes(state))

        with patch.object(
            beacon_sync_producer,
            "MAX_ATTACHMENT_CAPTURE_JOURNALS",
            0,
        ):
            try:
                initialize_producer(
                    self.config,
                    now=NOW + timedelta(seconds=1),
                )
            except ProducerError as exc:
                self.fail(f"stale capture blocked bounded recovery: {exc}")

        self._assert_capture_storage_retired(digest)

    def test_allocated_attachment_recovers_without_original_file(self):
        image = self.attachments / "pending.png"
        image_bytes = b"\x89PNG\r\n\x1a\npending"
        image.write_bytes(image_bytes)
        transcript = self.sessions / "pending-attachment.jsonl"
        transcript.write_bytes(
            self._session_meta("pending-attachment")
            + self._user_message_record(
                "附件",
                local_images=[str(image)],
            )
        )

        with self.assertRaisesRegex(ProducerError, "after_attachment_allocation"):
            collect_transcripts(
                self.config,
                include_existing=True,
                now=NOW,
                fault_point="after_attachment_allocation_state",
            )
        image.unlink()

        recovered = collect_transcripts(
            self.config,
            now=NOW + timedelta(seconds=1),
        )

        self.assertEqual(recovered["recovered"], 1)
        attachment = next(
            event for event in self._events()
            if event["event_kind"] == "attachment.blob"
        )
        self.assertEqual(self._payload_for(attachment), image_bytes)

    def test_symlink_hardlink_and_oversized_attachments_are_not_queued(self):
        real = self.attachments / "real.png"
        real.write_bytes(b"\x89PNG\r\n\x1a\nreal")
        symlink = self.attachments / "link.png"
        try:
            symlink.symlink_to(real)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        hardlink = self.attachments / "hard.png"
        os.link(real, hardlink)
        oversized = self.attachments / "large.bin"
        oversized.write_bytes(b"x" * 5000)
        transcript = self.sessions / "unsafe-attachments.jsonl"
        transcript.write_bytes(
            self._session_meta("unsafe-attachments")
            + self._user_message_record(
                "拒绝不安全附件",
                local_images=[str(symlink), str(hardlink), str(oversized)],
            )
        )

        result = collect_transcripts(
            self.config,
            include_existing=True,
            now=NOW,
        )

        self.assertEqual(
            [event["event_kind"] for event in self._events()],
            ["transcript.chunk"],
        )
        self.assertEqual(result["attachments_rejected"], 3)

    def test_claude_user_attachment_uses_same_verified_blob_protocol(self):
        document = self.attachments / "claude.txt"
        document.write_bytes(b"claude attachment")
        claude_root = self.root / ".claude" / "projects"
        claude_root.mkdir(parents=True)
        transcript = claude_root / "claude-session.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "claude-session",
                    "cwd": str(self.attachments),
                    "timestamp": "2026-07-31T11:00:00Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "attachment",
                                "path": str(document),
                            }
                        ],
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        self.config["transcript_paths"] = [str(claude_root)]

        collect_transcripts(self.config, include_existing=True, now=NOW)

        attachment = next(
            event for event in self._events()
            if event["event_kind"] == "attachment.blob"
        )
        self.assertEqual(attachment["agent"], "claude")
        self.assertEqual(
            attachment["extensions"]["attachment"]["reference_kind"],
            "claude.attachment",
        )
        self.assertEqual(self._payload_for(attachment), document.read_bytes())

    def test_version_one_producer_state_migrates_without_identity_rotation(self):
        transcript = self._write_codex_transcript(
            "v1-pending.jsonl",
            ["pending transcript"],
        )
        with self.assertRaisesRegex(ProducerError, "after_allocation_state"):
            collect_transcripts(
                self.config,
                include_existing=True,
                now=NOW,
                fault_point="after_allocation_state",
            )
        state_path = self.state_dir / "producer-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        expected_identity = state["producer_instance_id"]
        expected_next_seq = state["next_seq"]
        expected_sources = json.loads(json.dumps(state["sources"]))
        expected_event = json.loads(json.dumps(state["pending_event"]["event"]))
        state["schema_version"] = 1
        state.pop("attachment_queue")
        state["pending_event"].pop("pending_type")
        state["pending_event"].pop("attachments")
        state_path.write_bytes(canonical_json_bytes(state))

        migrated = load_producer_state(self.config)
        initialize_producer(self.config, now=NOW)

        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(migrated["attachment_queue"], [])
        self.assertEqual(
            migrated["producer_instance_id"],
            expected_identity,
        )
        self.assertEqual(migrated["next_seq"], expected_next_seq)
        self.assertEqual(migrated["sources"], expected_sources)
        self.assertEqual(migrated["pending_event"]["event"], expected_event)
        self.assertEqual(
            next(iter(migrated["sources"].values()))["cursor"],
            0,
        )
        self.assertGreater(transcript.stat().st_size, 0)

    def test_version_two_pending_transcript_migrates_attachment_references(self):
        image = self.attachments / "v2-pending-transcript.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nv2 pending transcript")
        transcript = self.sessions / "v2-pending-transcript.jsonl"
        transcript.write_bytes(
            self._session_meta("v2-pending-transcript")
            + self._user_message_record("附件", local_images=[str(image)])
        )
        with self.assertRaisesRegex(ProducerError, "after_allocation_state"):
            collect_transcripts(
                self.config,
                include_existing=True,
                now=NOW,
                fault_point="after_allocation_state",
            )
        state_path = self.state_dir / "producer-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["schema_version"] = 2
        state["pending_event"]["attachments"] = [
            self._as_v2_queue_item(item)
            for item in state["pending_event"]["attachments"]
        ]
        expected_identity = state["producer_instance_id"]
        expected_next_seq = state["next_seq"]
        expected_sources = json.loads(json.dumps(state["sources"]))
        expected_event = json.loads(json.dumps(state["pending_event"]["event"]))
        state_path.write_bytes(canonical_json_bytes(state))

        migrated = load_producer_state(self.config)

        self.assertEqual(migrated["schema_version"], 3)
        self.assertEqual(migrated["producer_instance_id"], expected_identity)
        self.assertEqual(migrated["next_seq"], expected_next_seq)
        self.assertEqual(migrated["sources"], expected_sources)
        self.assertEqual(migrated["pending_event"]["event"], expected_event)
        attachment = migrated["pending_event"]["attachments"][0]
        self.assertIn("reference_id", attachment)
        self.assertIn("source_cursor", attachment)
        self.assertNotIn("attachment_id", attachment)
        self.assertNotIn("transcript_cursor", attachment)

    def test_version_two_pending_transcript_rejects_out_of_range_attachment(self):
        image = self.attachments / "v2-out-of-range.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nout of range")
        transcript = self.sessions / "v2-out-of-range.jsonl"
        transcript.write_bytes(
            self._session_meta("v2-out-of-range")
            + self._user_message_record("附件", local_images=[str(image)])
        )
        with self.assertRaisesRegex(ProducerError, "after_allocation_state"):
            collect_transcripts(
                self.config,
                include_existing=True,
                now=NOW,
                fault_point="after_allocation_state",
            )
        state_path = self.state_dir / "producer-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["schema_version"] = 2
        event_end = state["pending_event"]["event"]["source_cursor"]["end"]
        legacy_item = self._as_v2_queue_item(
            state["pending_event"]["attachments"][0]
        )
        legacy_item["transcript_cursor"] = {
            "start": event_end + 1,
            "end": event_end + 2,
        }
        state["pending_event"]["attachments"] = [legacy_item]
        state_path.write_bytes(canonical_json_bytes(state))

        with self.assertRaisesRegex(ProducerError, "pending transcript"):
            load_producer_state(self.config)

    def test_version_two_attachment_queue_migrates_to_reference_ids(self):
        initialized = initialize_producer(self.config, now=NOW)
        state_path = self.state_dir / "producer-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        item, _ = self._legacy_pending_attachment_fixture(
            initialized["producer_instance_id"],
        )
        state["schema_version"] = 2
        state["attachment_queue"] = [item]
        state_path.write_bytes(canonical_json_bytes(state))

        migrated = load_producer_state(self.config)

        self.assertEqual(migrated["schema_version"], 3)
        migrated_item = migrated["attachment_queue"][0]
        self.assertIn("reference_id", migrated_item)
        self.assertIn("source_cursor", migrated_item)
        self.assertNotIn("attachment_id", migrated_item)
        self.assertNotIn("transcript_cursor", migrated_item)

    def test_durable_v1_pending_attachment_is_committed_without_rewrite(self):
        initialized = initialize_producer(self.config, now=NOW)
        item, event = self._legacy_pending_attachment_fixture(
            initialized["producer_instance_id"],
        )
        self._write_v2_pending_attachment_state(item, event)
        bundle = self._write_legacy_attachment_bundle(event)
        paths = {
            "event": bundle / "event.json",
            "ready": bundle / "ready.json",
            "object": bundle / "objects" / event["payload"]["sha256"],
        }
        before = {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in paths.items()
        }

        result = collect_transcripts(self.config, now=NOW)

        after = {
            name: hashlib.sha256(path.read_bytes()).hexdigest()
            for name, path in paths.items()
        }
        state = load_producer_state(self.config)
        self.assertEqual(before, after)
        self.assertEqual(result["recovered"], 1)
        self.assertEqual(state["schema_version"], 3)
        self.assertEqual(state["next_seq"], event["seq"] + 1)
        self.assertIsNone(state["pending_event"])
        self.assertEqual(state["attachment_queue"], [])
        self.assertEqual(
            json.loads(paths["event"].read_text(encoding="utf-8"))[
                "schema_version"
            ],
            1,
        )

    def test_non_durable_v1_pending_attachment_is_reallocated_as_v2(self):
        initialized = initialize_producer(self.config, now=NOW)
        item, event = self._legacy_pending_attachment_fixture(
            initialized["producer_instance_id"],
        )
        self._write_v2_pending_attachment_state(item, event)

        result = collect_transcripts(self.config, now=NOW)

        attachments = [
            candidate
            for candidate in self._events()
            if candidate["event_kind"] == "attachment.blob"
        ]
        self.assertEqual(result["recovered"], 0)
        self.assertEqual(result["attachments_emitted"], 1)
        self.assertEqual(len(attachments), 1)
        self.assertEqual(attachments[0]["schema_version"], 2)
        self.assertEqual(attachments[0]["seq"], event["seq"])
        self.assertEqual(
            attachments[0]["source_cursor"],
            item["transcript_cursor"],
        )
        self.assertIn(
            "reference_id",
            attachments[0]["extensions"]["attachment"],
        )
        self.assertNotIn(
            "attachment_id",
            attachments[0]["extensions"]["attachment"],
        )

    def test_version_two_pending_attachment_rejects_mismatched_event(self):
        initialized = initialize_producer(self.config, now=NOW)
        item, event = self._legacy_pending_attachment_fixture(
            initialized["producer_instance_id"],
        )
        event["extensions"]["attachment"]["transcript_cursor"] = {
            "start": 303,
            "end": 404,
        }
        event["event_id"] = derive_event_id(event)
        self._write_v2_pending_attachment_state(item, event)

        with self.assertRaisesRegex(ProducerError, "pending attachment"):
            load_producer_state(self.config)

    def test_unknown_producer_state_schema_fails_closed(self):
        initialize_producer(self.config, now=NOW)
        state_path = self.state_dir / "producer-state.json"
        original = json.loads(state_path.read_text(encoding="utf-8"))

        for schema_version in (99, True):
            with self.subTest(schema_version=schema_version):
                state = dict(original)
                state["schema_version"] = schema_version
                state_path.write_bytes(canonical_json_bytes(state))
                with self.assertRaisesRegex(ProducerError, "schema"):
                    load_producer_state(self.config)

    @unittest.skipIf(os.name == "nt", "Mac authority snapshot fixture")
    def test_gc_requires_matching_receipt_hash_and_gc_permission(self):
        self._write_codex_transcript("gc.jsonl", ["gc"])
        collect_transcripts(self.config, include_existing=True, now=NOW)
        vault = self.root / "vault"
        vault.mkdir()
        (vault / "memory.md").write_text("sealed\n", encoding="utf-8")
        generation = publish_generation(
            {"vault_path": str(vault)},
            {
                "state_dir": str(self.root / "publisher-state"),
                "published_dir": str(self.receipts.parent.parent),
            },
            now=NOW,
        )
        bundle = self._bundles()[0]
        event_bytes = (bundle / "event.json").read_bytes()
        event = json.loads(event_bytes)
        receipt_path = (
            self.receipts
            / event["producer_instance_id"]
            / f"{event['seq']:020d}-{event['event_id']}.json"
        )
        receipt_path.parent.mkdir(parents=True)
        receipt = {
            "protocol": "agent-memory-beacon-sync-receipt",
            "schema_version": 1,
            "producer_instance_id": event["producer_instance_id"],
            "seq": event["seq"],
            "event_id": event["event_id"],
            "event_sha256": hashlib.sha256(event_bytes).hexdigest(),
            "status": "applied",
            "code": "applied",
            "canonical_generation": generation["generation"],
            "generation_id": generation["generation_id"],
            "gc_allowed": False,
            "processed_at": "2026-07-31T12:01:00Z",
        }
        receipt_path.write_bytes(canonical_json_bytes(receipt))

        denied = garbage_collect_outbox(self.config, now=NOW)
        self.assertEqual(denied["removed"], 0)
        receipt["gc_allowed"] = True
        receipt_path.write_bytes(canonical_json_bytes(receipt))
        materialize_generation(self.config, now=NOW, bootstrap=True)

        allowed = garbage_collect_outbox(self.config, now=NOW)
        self.assertEqual(allowed["removed"], 1)
        self.assertFalse(bundle.exists())

    def test_gc_denies_receipt_without_verified_sealed_generation(self):
        self._write_codex_transcript("forged-gc.jsonl", ["keep me"])
        collect_transcripts(self.config, include_existing=True, now=NOW)
        bundle = self._bundles()[0]
        event_bytes = (bundle / "event.json").read_bytes()
        event = json.loads(event_bytes)
        receipt_path = (
            self.receipts
            / event["producer_instance_id"]
            / f"{event['seq']:020d}-{event['event_id']}.json"
        )
        receipt_path.parent.mkdir(parents=True)
        receipt = {
            "protocol": "agent-memory-beacon-sync-receipt",
            "schema_version": 1,
            "producer_instance_id": event["producer_instance_id"],
            "seq": event["seq"],
            "event_id": event["event_id"],
            "event_sha256": hashlib.sha256(event_bytes).hexdigest(),
            "status": "applied",
            "code": "applied",
            "canonical_generation": 99,
            "generation_id": "generation-" + ("a" * 64),
            "gc_allowed": True,
            "processed_at": "2026-07-31T12:01:00Z",
        }
        receipt_path.write_bytes(canonical_json_bytes(receipt))

        denied = garbage_collect_outbox(self.config, now=NOW)

        self.assertEqual(denied["removed"], 0)
        self.assertEqual(denied["denied"], 1)
        self.assertTrue(bundle.is_dir())

    @unittest.skipIf(os.name == "nt", "Mac authority snapshot fixture")
    def test_gc_denies_sealed_receipt_until_replica_is_materialized(self):
        self._write_codex_transcript("not-materialized.jsonl", ["keep until replica"])
        collect_transcripts(self.config, include_existing=True, now=NOW)
        vault = self.root / "vault"
        vault.mkdir()
        (vault / "memory.md").write_text("sealed\n", encoding="utf-8")
        generation = publish_generation(
            {"vault_path": str(vault)},
            {
                "state_dir": str(self.root / "publisher-state"),
                "published_dir": str(self.receipts.parent.parent),
            },
            now=NOW,
        )
        bundle = self._bundles()[0]
        event_bytes = (bundle / "event.json").read_bytes()
        event = json.loads(event_bytes)
        receipt_path = (
            self.receipts
            / event["producer_instance_id"]
            / f"{event['seq']:020d}-{event['event_id']}.json"
        )
        receipt_path.parent.mkdir(parents=True)
        receipt_path.write_bytes(
            canonical_json_bytes(
                {
                    "protocol": "agent-memory-beacon-sync-receipt",
                    "schema_version": 1,
                    "producer_instance_id": event["producer_instance_id"],
                    "seq": event["seq"],
                    "event_id": event["event_id"],
                    "event_sha256": hashlib.sha256(event_bytes).hexdigest(),
                    "status": "applied",
                    "code": "applied",
                    "canonical_generation": generation["generation"],
                    "generation_id": generation["generation_id"],
                    "gc_allowed": True,
                    "processed_at": "2026-07-31T12:01:00Z",
                }
            )
        )

        denied = garbage_collect_outbox(self.config, now=NOW)

        self.assertEqual(denied["removed"], 0)
        self.assertTrue(bundle.is_dir())

    def test_transcript_discovery_is_bounded_and_fair_across_runs(self):
        transcripts = [
            self._write_codex_transcript(f"discover-{index}.jsonl", [str(index)])
            for index in range(5)
        ]
        self.config["transcript_paths"] = [str(path) for path in transcripts]

        with patch.object(
            beacon_sync_producer,
            "MAX_TRANSCRIPT_DISCOVERY_ITEMS_PER_RUN",
            2,
        ):
            results = [
                collect_transcripts(
                    self.config,
                    include_existing=True,
                    now=NOW + timedelta(seconds=index),
                )
                for index in range(3)
            ]

        self.assertEqual([result["emitted"] for result in results], [2, 2, 1])
        self.assertEqual([result["limited"] for result in results], [True, True, False])
        self.assertEqual(
            {event["session_id"] for event in self._events()},
            {f"discover-{index}" for index in range(5)},
        )

    def test_outbox_gc_is_bounded_and_fair_across_runs(self):
        for index in range(3):
            self._write_codex_transcript(f"gc-budget-{index}.jsonl", [str(index)])
        collect_transcripts(self.config, include_existing=True, now=NOW)
        self.receipts.mkdir(parents=True)
        real_read = beacon_sync_producer.read_bounded_regular_file
        examined_bundles = []

        def track_event_reads(path, **kwargs):
            candidate = Path(path)
            if candidate.name == "event.json":
                examined_bundles.append(candidate.parent.name)
            return real_read(path, **kwargs)

        with (
            patch.object(
                beacon_sync_producer,
                "MAX_GC_BUNDLES_PER_RUN",
                1,
            ),
            patch.object(
                beacon_sync_producer,
                "read_bounded_regular_file",
                side_effect=track_event_reads,
            ),
        ):
            results = [
                garbage_collect_outbox(
                    self.config,
                    now=NOW + timedelta(seconds=index),
                )
                for index in range(3)
            ]

        self.assertEqual([result["examined"] for result in results], [1, 1, 1])
        self.assertEqual([result["limited"] for result in results], [True, True, False])
        self.assertEqual([result["pending"] for result in results], [1, 1, 0])
        self.assertEqual(len(set(examined_bundles)), 3)

    def test_bundle_cleanup_cannot_follow_a_swapped_parent_directory(self):
        bundle = self.outbox / "v1" / "events" / "producer" / "bundle"
        bundle.mkdir(parents=True)
        victim = bundle / "event.json"
        victim.write_bytes(b"managed")
        outside = self.root / "outside"
        outside.mkdir()
        outside_victim = outside / victim.name
        outside_victim.write_bytes(b"outside")
        held = bundle.with_name("bundle-held")
        original_unlink = Path.unlink
        swapped = False

        def swap_parent_before_unlink(path, *args, **kwargs):
            nonlocal swapped
            if Path(path) == victim and not swapped:
                swapped = True
                bundle.rename(held)
                bundle.symlink_to(outside, target_is_directory=True)
            return original_unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", swap_parent_before_unlink):
            beacon_sync_producer._remove_bundle(bundle, self.outbox)

        self.assertTrue(outside_victim.is_file())
        self.assertEqual(outside_victim.read_bytes(), b"outside")
        self.assertFalse(bundle.exists())

    def _leave_ready_capture(self, session_id):
        image = self.attachments / f"{session_id}.png"
        image_bytes = b"\x89PNG\r\n\x1a\n" + session_id.encode("ascii")
        image.write_bytes(image_bytes)
        transcript = self.sessions / f"{session_id}.jsonl"
        transcript.write_bytes(
            self._session_meta(session_id)
            + self._user_message_record(
                "leave one ready capture",
                local_images=[str(image)],
            )
        )
        with self.assertRaisesRegex(
            ProducerError,
            "after_attachment_capture_ready",
        ):
            collect_transcripts(
                self.config,
                include_existing=True,
                now=NOW,
                fault_point="after_attachment_capture_ready",
            )
        return transcript, hashlib.sha256(image_bytes).hexdigest()

    def _assert_capture_storage_retired(self, digest):
        capture_root = self.state_dir / "attachment-captures" / "v1"
        self.assertEqual(
            list(capture_root.glob("capture-*"))
            if capture_root.is_dir()
            else [],
            [],
        )
        self.assertFalse(
            (
                self.state_dir
                / "attachment-cas"
                / digest[:2]
                / digest
            ).exists()
        )

    def _write_codex_transcript(self, name, messages):
        path = self.sessions / name
        records = [json.loads(self._session_meta(name.removesuffix(".jsonl")))]
        records.extend(self._message_record(message) for message in messages)
        path.write_bytes(
            b"".join(
                json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n"
                for record in records
            )
        )
        return path

    def _session_meta(self, session_id):
        return (
            json.dumps(
                {
                    "type": "session_meta",
                    "timestamp": "2026-07-31T11:00:00Z",
                    "payload": {
                        "id": session_id,
                        "cwd": str(self.attachments),
                        "timestamp": "2026-07-31T11:00:00Z",
                    },
                },
                ensure_ascii=False,
            ).encode("utf-8")
            + b"\n"
        )

    def _user_message_record(self, message, *, local_images=None, local_audio=None):
        return (
            json.dumps(
                {
                    "type": "event_msg",
                    "timestamp": "2026-07-31T11:00:01Z",
                    "payload": {
                        "type": "user_message",
                        "message": message,
                        "local_images": list(local_images or []),
                        "local_audio": list(local_audio or []),
                        "images": [],
                        "text_elements": [],
                        "client_id": "test",
                    },
                },
                ensure_ascii=False,
            ).encode("utf-8")
            + b"\n"
        )

    def _append_message(self, path, text):
        with open(path, "ab") as handle:
            handle.write(self._codex_record(text) + b"\n")

    def _codex_record(self, text):
        return json.dumps(
            self._message_record(text),
            ensure_ascii=False,
        ).encode("utf-8")

    def _message_record(self, text):
        return {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            },
        }

    def _as_v2_queue_item(self, item):
        legacy = dict(item)
        cursor = legacy.pop(
            "source_cursor",
            legacy.get("transcript_cursor"),
        )
        legacy["transcript_cursor"] = dict(cursor)
        if "reference_id" in legacy:
            legacy.pop("reference_id")
            identity = {
                "original_name": legacy["original_name"],
                "payload_sha256": legacy["sha256"],
                "source_locator_sha256": legacy["source_locator_sha256"],
            }
            legacy["attachment_id"] = "attachment-" + hashlib.sha256(
                canonical_json_bytes(identity)
            ).hexdigest()
        return legacy

    def _legacy_pending_attachment_fixture(self, producer_instance_id):
        data = b"\x89PNG\r\n\x1a\nlegacy pending"
        digest = hashlib.sha256(data).hexdigest()
        source_locator_sha256 = hashlib.sha256(
            str(self.attachments / "legacy.png").encode("utf-8")
        ).hexdigest()
        attachment_identity = {
            "original_name": "legacy.png",
            "payload_sha256": digest,
            "source_locator_sha256": source_locator_sha256,
        }
        attachment_id = "attachment-" + hashlib.sha256(
            canonical_json_bytes(attachment_identity)
        ).hexdigest()
        stream_epoch = "87654321-4321-4234-9234-cba987654321"
        session_id = "legacy-session"
        stream_id = "stream-" + hashlib.sha256(
            f"codex\0{session_id}".encode("utf-8")
        ).hexdigest()
        transcript_cursor = {"start": 101, "end": 202}
        item = {
            "attachment_id": attachment_id,
            "original_name": "legacy.png",
            "sha256": digest,
            "bytes": len(data),
            "media_type": "image/png",
            "source_locator_sha256": source_locator_sha256,
            "reference_kind": "codex.local_image",
            "transcript_cursor": transcript_cursor,
            "agent": "codex",
            "session_id": session_id,
            "stream_epoch": stream_epoch,
            "metadata": {"cwd": str(self.attachments), "is_subagent": False},
        }
        event = {
            "protocol": "agent-memory-beacon-sync-event",
            "schema_version": 1,
            "device_id": self.config["device_id"],
            "producer_instance_id": producer_instance_id,
            "seq": 1,
            "event_id": "",
            "event_kind": "attachment.blob",
            "created_at": "2026-07-31T12:00:00Z",
            "agent": "codex",
            "session_id": session_id,
            "stream_id": stream_id,
            "stream_epoch": stream_epoch,
            "logical_record_id": f"session:codex:{session_id}",
            "source_cursor": {"start": 0, "end": len(data)},
            "metadata": dict(item["metadata"]),
            "payload": {
                "sha256": digest,
                "bytes": len(data),
                "media_type": "image/png",
                "role": "attachment-source",
            },
            "extensions": {
                "attachment": {
                    "attachment_id": attachment_id,
                    "original_name": item["original_name"],
                    "source_locator_sha256": source_locator_sha256,
                    "reference_kind": item["reference_kind"],
                    "transcript_cursor": transcript_cursor,
                }
            },
        }
        event["event_id"] = derive_event_id(event)
        cas_path = self.state_dir / "attachment-cas" / digest[:2] / digest
        cas_path.parent.mkdir(parents=True, exist_ok=True)
        cas_path.write_bytes(data)
        return item, event

    def _write_v2_pending_attachment_state(self, item, event):
        state_path = self.state_dir / "producer-state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["schema_version"] = 2
        state["next_seq"] = event["seq"]
        state["attachment_queue"] = [item]
        state["pending_event"] = {
            "pending_type": "attachment",
            "queue_id": item["attachment_id"],
            "event": event,
        }
        state_path.write_bytes(canonical_json_bytes(state))

    def _write_legacy_attachment_bundle(self, event):
        bundle = (
            self.outbox
            / "v1"
            / "events"
            / event["producer_instance_id"]
            / f"{event['seq']:020d}-{event['event_id']}"
        )
        object_path = bundle / "objects" / event["payload"]["sha256"]
        object_path.parent.mkdir(parents=True, exist_ok=True)
        cas_path = (
            self.state_dir
            / "attachment-cas"
            / event["payload"]["sha256"][:2]
            / event["payload"]["sha256"]
        )
        object_path.write_bytes(cas_path.read_bytes())
        event_bytes = canonical_json_bytes(event)
        (bundle / "event.json").write_bytes(event_bytes)
        ready = {
            "protocol": "agent-memory-beacon-sync-ready",
            "schema_version": 1,
            "device_id": event["device_id"],
            "producer_instance_id": event["producer_instance_id"],
            "seq": event["seq"],
            "event_id": event["event_id"],
            "event_sha256": hashlib.sha256(event_bytes).hexdigest(),
            "object_count": 1,
            "object_bytes": event["payload"]["bytes"],
        }
        (bundle / "ready.json").write_bytes(canonical_json_bytes(ready))
        return bundle

    def _bundles(self):
        events_root = self.outbox / "v1" / "events"
        return sorted(path.parent for path in events_root.rglob("ready.json"))

    def _events(self):
        return [
            json.loads((bundle / "event.json").read_text(encoding="utf-8"))
            for bundle in self._bundles()
        ]

    def _payload_for(self, event):
        bundle = next(
            path
            for path in self._bundles()
            if path.name.endswith(event["event_id"])
        )
        return (bundle / "objects" / event["payload"]["sha256"]).read_bytes()


if __name__ == "__main__":
    unittest.main()
