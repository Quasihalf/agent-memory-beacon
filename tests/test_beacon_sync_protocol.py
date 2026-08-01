import hashlib
import json
import os
import pathlib
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import safety
import beacon_sync_protocol
from beacon_sync_protocol import (
    ProtocolError,
    build_event,
    build_ready,
    canonical_json_bytes,
    derive_event_id,
    event_bundle_name,
    event_sequence_directory_name,
    portable_atomic_write,
    portable_ensure_directory_tree,
    portable_file_lock,
    portable_rmtree,
    portable_unlink_regular,
    read_bounded_regular_file,
    validate_event,
    validate_ready,
    validate_replica_path,
    write_immutable,
)


class BeaconSyncProtocolTests(unittest.TestCase):
    def test_canonical_json_is_ascii_sorted_and_newline_terminated(self):
        payload = canonical_json_bytes({"z": "中文", "a": 1})

        self.assertEqual(payload, b'{"a":1,"z":"\\u4e2d\\u6587"}\n')

    def test_canonical_json_rejects_float_and_non_string_key(self):
        with self.assertRaisesRegex(ProtocolError, "float"):
            canonical_json_bytes({"score": 0.5})
        with self.assertRaisesRegex(ProtocolError, "string keys"):
            canonical_json_bytes({1: "bad"})

    def test_canonical_json_accepts_full_signed_64_bit_range(self):
        payload = canonical_json_bytes(
            {"maximum": 2**63 - 1, "minimum": -(2**63)}
        )

        self.assertEqual(
            payload,
            b'{"maximum":9223372036854775807,"minimum":-9223372036854775808}\n',
        )

    def test_bounded_json_decoder_accepts_signed_64_bit_boundaries(self):
        decoded = beacon_sync_protocol.decode_bounded_json(
            b'{"maximum":9223372036854775807,'
            b'"minimum":-9223372036854775808}\n',
            max_bytes=128,
        )

        self.assertEqual(decoded["maximum"], 2**63 - 1)
        self.assertEqual(decoded["minimum"], -(2**63))

    def test_bounded_json_decoder_rejects_oversize_integer_as_protocol_error(self):
        document = b'{"seq":' + (b"9" * 5000) + b"}\n"

        with self.assertRaisesRegex(ProtocolError, "JSON"):
            beacon_sync_protocol.decode_bounded_json(
                document,
                max_bytes=len(document),
            )

    def test_bounded_json_decoder_normalizes_recursion_error(self):
        document = (b"[" * 5000) + b"0" + (b"]" * 5000)

        with self.assertRaisesRegex(ProtocolError, "JSON"):
            beacon_sync_protocol.decode_bounded_json(
                document,
                max_bytes=len(document),
            )

    def test_bounded_json_decoder_enforces_byte_limit(self):
        with self.assertRaisesRegex(ProtocolError, "size"):
            beacon_sync_protocol.decode_bounded_json(
                b'{"value":1}\n',
                max_bytes=4,
            )

    def test_bounded_json_decoder_checks_size_before_copying_input(self):
        class OversizeBytearray(bytearray):
            def __bytes__(self):
                raise AssertionError("oversize input was copied")

        with self.assertRaisesRegex(ProtocolError, "size"):
            beacon_sync_protocol.decode_bounded_json(
                OversizeBytearray(b'{"value":1}\n'),
                max_bytes=4,
            )

    def test_build_event_derives_stable_event_id(self):
        fields = {
            "device_id": "windows-gpu-a1b2c3d4",
            "producer_instance_id": "12345678-1234-4234-9234-123456789abc",
            "seq": 7,
            "event_kind": "transcript.chunk",
            "created_at": "2026-07-31T12:00:00Z",
            "agent": "codex",
            "session_id": "019fa000-1111-7222-8333-123456789abc",
            "stream_epoch": "87654321-4321-4234-9234-cba987654321",
            "source_cursor": {"start": 10, "end": 20},
            "metadata": {"cwd": "C:\\work\\demo", "is_subagent": False},
            "payload": {
                "sha256": "a" * 64,
                "bytes": 10,
                "media_type": "application/x-ndjson",
                "role": "transcript-source",
            },
        }

        first = build_event(**fields)
        second = build_event(**fields)

        self.assertEqual(first, second)
        self.assertRegex(first["event_id"], r"^event-[0-9a-f]{64}$")
        self.assertRegex(first["stream_id"], r"^stream-[0-9a-f]{64}$")
        self.assertEqual(
            first["logical_record_id"],
            "session:codex:019fa000-1111-7222-8333-123456789abc",
        )

    def test_transcript_chunk_v1_golden_bytes_and_event_id_are_unchanged(self):
        event = self._golden_transcript_event("transcript.chunk")

        self.assertEqual(event["schema_version"], 1)
        self.assertEqual(
            event["event_id"],
            "event-1370af9a343d6ae876f5931a12ea6de06130bdb8bc6facaabcf414ae5f77d40f",
        )
        self.assertEqual(
            canonical_json_bytes(event),
            (
                b'{"agent":"codex","created_at":"2026-07-31T12:00:00Z",'
                b'"device_id":"windows-gpu-a1b2c3d4","event_id":'
                b'"event-1370af9a343d6ae876f5931a12ea6de06130bdb8bc6facaabcf414ae5f77d40f",'
                b'"event_kind":"transcript.chunk","extensions":{},'
                b'"logical_record_id":"session:codex:019fa000-1111-7222-8333-123456789abc",'
                b'"metadata":{"cwd":"C:\\\\work\\\\demo","is_subagent":false},'
                b'"payload":{"bytes":10,"media_type":"application/x-ndjson",'
                b'"role":"transcript-source",'
                b'"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},'
                b'"producer_instance_id":"12345678-1234-4234-9234-123456789abc",'
                b'"protocol":"agent-memory-beacon-sync-event","schema_version":1,'
                b'"seq":7,"session_id":"019fa000-1111-7222-8333-123456789abc",'
                b'"source_cursor":{"end":20,"start":10},'
                b'"stream_epoch":"87654321-4321-4234-9234-cba987654321",'
                b'"stream_id":"stream-3b0f33e16b51be6dbaab5cded1cf1919fc5d436c0e15bf56838a34481878cf13"}\n'
            ),
        )

    def test_transcript_gap_v1_golden_bytes_and_event_id_are_unchanged(self):
        event = self._golden_transcript_event("transcript.gap")

        self.assertEqual(event["schema_version"], 1)
        self.assertEqual(
            event["event_id"],
            "event-7f5307b5ad464394e46dc855dfed15ab1fc851d289c9681b343e7e90a1213a2f",
        )
        self.assertEqual(
            canonical_json_bytes(event),
            (
                b'{"agent":"codex","created_at":"2026-07-31T12:00:00Z",'
                b'"device_id":"windows-gpu-a1b2c3d4","event_id":'
                b'"event-7f5307b5ad464394e46dc855dfed15ab1fc851d289c9681b343e7e90a1213a2f",'
                b'"event_kind":"transcript.gap","extensions":{},'
                b'"logical_record_id":"session:codex:019fa000-1111-7222-8333-123456789abc",'
                b'"metadata":{"cwd":"C:\\\\work\\\\demo","is_subagent":false},'
                b'"payload":{"bytes":10,"media_type":"application/x-ndjson",'
                b'"role":"transcript-gap",'
                b'"sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},'
                b'"producer_instance_id":"12345678-1234-4234-9234-123456789abc",'
                b'"protocol":"agent-memory-beacon-sync-event","schema_version":1,'
                b'"seq":7,"session_id":"019fa000-1111-7222-8333-123456789abc",'
                b'"source_cursor":{"end":20,"start":10},'
                b'"stream_epoch":"87654321-4321-4234-9234-cba987654321",'
                b'"stream_id":"stream-3b0f33e16b51be6dbaab5cded1cf1919fc5d436c0e15bf56838a34481878cf13"}\n'
            ),
        )

    def test_validate_event_rejects_unknown_fields_and_lifecycle_kind(self):
        event = self._event()
        event["unexpected"] = True
        with self.assertRaisesRegex(ProtocolError, "unknown event fields"):
            validate_event(event)

        event = self._event()
        event["event_kind"] = "memory.retract"
        with self.assertRaisesRegex(ProtocolError, "event kind"):
            validate_event(event)

    def test_validate_event_binds_expected_device_and_cursor(self):
        event = self._event()
        with self.assertRaisesRegex(ProtocolError, "device"):
            validate_event(event, expected_device_id="another-device")

        event = self._event()
        event["source_cursor"] = {"start": 20, "end": 10}
        with self.assertRaisesRegex(ProtocolError, "cursor"):
            validate_event(event)

    def test_uuid_text_must_match_canonical_form_exactly(self):
        event = self._event()
        event["producer_instance_id"] = event["producer_instance_id"].upper()
        event["event_id"] = derive_event_id(event)

        with self.assertRaisesRegex(ProtocolError, "canonical"):
            validate_event(event)

    def test_malformed_enum_fields_are_protocol_errors(self):
        cases = []

        event_kind = self._event()
        event_kind["event_kind"] = []
        cases.append(("event_kind", event_kind))

        agent = self._event()
        agent["agent"] = {}
        cases.append(("agent", agent))

        reference_kind = self._attachment_event()
        reference_kind["extensions"]["attachment"]["reference_kind"] = []
        cases.append(("reference_kind", reference_kind))

        for name, event in cases:
            with self.subTest(name=name):
                with self.assertRaises(ProtocolError):
                    validate_event(event)

    def test_wire_fields_require_exact_types_before_semantic_validation(self):
        numeric_session = self._event()
        numeric_session["session_id"] = 7
        numeric_session["stream_id"] = "stream-" + hashlib.sha256(
            b"codex\x007"
        ).hexdigest()
        numeric_session["logical_record_id"] = "session:codex:7"
        numeric_session["event_id"] = derive_event_id(numeric_session)

        unknown_role = self._event()
        unknown_role["event_kind"] = "future.kind"
        unknown_role["payload"]["role"] = 7
        unknown_role["event_id"] = derive_event_id(unknown_role)

        unknown_media_type = self._event()
        unknown_media_type["event_kind"] = "future.kind"
        unknown_media_type["payload"]["media_type"] = ["application/json"]
        unknown_media_type["event_id"] = derive_event_id(unknown_media_type)

        for name, event, allow_unknown in (
            ("session_id", numeric_session, False),
            ("unknown_role", unknown_role, True),
            ("unknown_media_type", unknown_media_type, True),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ProtocolError):
                    validate_event(event, allow_unknown_kind=allow_unknown)

        event = self._event()
        event_bytes = canonical_json_bytes(event)
        ready = build_ready(event, event_bytes)
        for field in ("seq", "object_count"):
            malformed = dict(ready)
            malformed[field] = True
            with self.subTest(ready_field=field):
                with self.assertRaises(ProtocolError):
                    validate_ready(malformed, event, event_bytes)

    def test_validate_event_recomputes_stream_id_and_validates_created_at(self):
        event = self._event()
        event["stream_id"] = "stream-" + ("f" * 64)
        event["event_id"] = derive_event_id(event)
        with self.assertRaisesRegex(ProtocolError, "stream"):
            validate_event(event)

        event = self._event()
        event["created_at"] = {"not": "a timestamp"}
        with self.assertRaisesRegex(ProtocolError, "created_at"):
            validate_event(event)

        event = self._event()
        event["created_at"] = "2026-07-31 12:00:00"
        with self.assertRaisesRegex(ProtocolError, "created_at"):
            validate_event(event)

    def test_ready_binds_exact_event_bytes_and_payload_totals(self):
        event = self._event()
        event_bytes = canonical_json_bytes(event)
        ready = build_ready(event, event_bytes)

        validated = validate_ready(ready, event, event_bytes)
        self.assertEqual(validated["event_id"], event["event_id"])

        tampered = dict(ready)
        tampered["event_sha256"] = "f" * 64
        with self.assertRaisesRegex(ProtocolError, "event hash"):
            validate_ready(tampered, event, event_bytes)

    def test_attachment_event_is_content_addressed_without_exposing_source_path(self):
        event = self._attachment_event()
        event_bytes = canonical_json_bytes(event)
        ready = build_ready(event, event_bytes)

        self.assertEqual(validate_event(event), event)
        self.assertEqual(event["schema_version"], 2)
        self.assertEqual(ready["schema_version"], 2)
        self.assertEqual(ready["object_count"], 1)
        self.assertEqual(ready["object_bytes"], 8)
        self.assertEqual(event["source_cursor"], {"start": 20, "end": 120})
        self.assertNotIn("C:\\Users\\demo\\secret", event_bytes.decode("ascii"))
        self.assertEqual(
            event["extensions"]["attachment"]["source_locator_sha256"],
            "c" * 64,
        )

    def test_attachment_metadata_is_part_of_event_identity(self):
        first = self._attachment_event(original_name="diagram.png")
        second = self._attachment_event(original_name="renamed.png")

        self.assertNotEqual(first["event_id"], second["event_id"])
        self.assertNotEqual(
            first["extensions"]["attachment"]["reference_id"],
            second["extensions"]["attachment"]["reference_id"],
        )

    def test_attachment_reference_id_binds_record_and_stream_identity(self):
        event = self._attachment_event()
        attachment = event["extensions"]["attachment"]
        identity = {
            "original_name": attachment["original_name"],
            "payload_sha256": event["payload"]["sha256"],
            "producer_instance_id": event["producer_instance_id"],
            "source_cursor": event["source_cursor"],
            "source_locator_sha256": attachment["source_locator_sha256"],
            "stream_epoch": event["stream_epoch"],
            "stream_id": event["stream_id"],
        }

        self.assertEqual(
            attachment["reference_id"],
            "reference-"
            + hashlib.sha256(canonical_json_bytes(identity)).hexdigest(),
        )
        self.assertEqual(
            set(attachment),
            {
                "reference_id",
                "original_name",
                "source_locator_sha256",
                "reference_kind",
            },
        )

    def test_attachment_event_rejects_unsafe_name_metadata_and_media_type(self):
        unsafe_name = self._attachment_event()
        unsafe_name["extensions"]["attachment"]["original_name"] = "../escape.png"
        unsafe_name["event_id"] = derive_event_id(unsafe_name)
        with self.assertRaisesRegex(ProtocolError, "attachment"):
            validate_event(unsafe_name)

        unknown_field = self._attachment_event()
        unknown_field["extensions"]["attachment"]["source_path"] = "C:\\secret.png"
        unknown_field["event_id"] = derive_event_id(unknown_field)
        with self.assertRaisesRegex(ProtocolError, "attachment"):
            validate_event(unknown_field)

        bad_media_type = self._attachment_event()
        bad_media_type["payload"]["media_type"] = "text/html; charset=utf-8"
        bad_media_type["event_id"] = derive_event_id(bad_media_type)
        with self.assertRaisesRegex(ProtocolError, "media type"):
            validate_event(bad_media_type)

    def test_ready_rejects_noncanonical_event_bytes(self):
        event = self._event()
        canonical = canonical_json_bytes(event)
        ready = build_ready(event, canonical)
        noncanonical = json.dumps(event, indent=2).encode("utf-8") + b"\n"
        ready["event_sha256"] = __import__("hashlib").sha256(
            noncanonical
        ).hexdigest()

        with self.assertRaisesRegex(ProtocolError, "canonical"):
            validate_ready(ready, event, noncanonical)

    def test_v1_attachment_requires_explicit_legacy_read_validation(self):
        event = self._legacy_attachment_event()
        event_bytes = canonical_json_bytes(event)
        ready = self._legacy_attachment_ready(event, event_bytes)

        with self.assertRaisesRegex(ProtocolError, "schema"):
            validate_event(event)
        with self.assertRaisesRegex(ProtocolError, "schema"):
            validate_ready(ready, event, event_bytes)

        self.assertTrue(
            hasattr(
                beacon_sync_protocol,
                "validate_legacy_attachment_event",
            )
        )
        self.assertTrue(
            hasattr(
                beacon_sync_protocol,
                "validate_legacy_attachment_ready",
            )
        )
        self.assertEqual(
            beacon_sync_protocol.validate_legacy_attachment_event(event),
            event,
        )
        self.assertEqual(
            beacon_sync_protocol.validate_legacy_attachment_ready(
                ready,
                event,
                event_bytes,
            ),
            ready,
        )

    def test_unknown_event_and_ready_schema_fail_closed(self):
        event = self._attachment_event()
        event_bytes = canonical_json_bytes(event)
        ready = build_ready(event, event_bytes)

        unknown_event = dict(event)
        unknown_event["schema_version"] = 3
        with self.assertRaisesRegex(ProtocolError, "schema"):
            validate_event(unknown_event)

        unknown_ready = dict(ready)
        unknown_ready["schema_version"] = 3
        with self.assertRaisesRegex(ProtocolError, "schema"):
            validate_ready(unknown_ready, event, event_bytes)

        boolean_event = self._golden_transcript_event("transcript.chunk")
        boolean_event["schema_version"] = True
        with self.assertRaisesRegex(ProtocolError, "schema"):
            validate_event(boolean_event)

        legacy_event = self._legacy_attachment_event()
        legacy_event["schema_version"] = True
        with self.assertRaisesRegex(ProtocolError, "schema"):
            beacon_sync_protocol.validate_legacy_attachment_event(legacy_event)

    def test_bundle_name_is_sequence_sorted_and_path_safe(self):
        event = self._event(seq=42)
        name = event_bundle_name(event)

        self.assertTrue(name.startswith("00000000000000000042-event-"))
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertEqual(
            event_sequence_directory_name(event["seq"]),
            "seq-00000000000000000042",
        )

    def test_replica_path_rejects_traversal_reserved_names_and_trailing_dot(self):
        for candidate in (
            "../escape.md",
            "/absolute.md",
            "C:\\absolute.md",
            "notes/CON.txt",
            "notes/name.",
            "notes/a:b.md",
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(ProtocolError):
                    validate_replica_path(candidate)

        self.assertEqual(
            validate_replica_path("01-Projects/demo/Memory/decisions.md"),
            "01-Projects/demo/Memory/decisions.md",
        )

    def test_atomic_write_stays_under_root_and_rejects_symlink_destination(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "state" / "value.json"
            portable_atomic_write(destination, b"first\n", root=root)
            self.assertEqual(destination.read_bytes(), b"first\n")

            with self.assertRaisesRegex(ProtocolError, "outside"):
                portable_atomic_write(root.parent / "escape", b"x", root=root)

            link = root / "state" / "link.json"
            try:
                link.symlink_to(destination)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")
            with self.assertRaisesRegex(ProtocolError, "symlink"):
                portable_atomic_write(link, b"bad", root=root)

    def test_windows_atomic_write_keeps_temp_descriptor_open_through_replace(self):
        if os.name == "nt":
            self.skipTest("non-Windows branch simulation")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            destination = root / "state" / "value.json"
            temp_path = None

            def create_temp(path, *, root):
                nonlocal temp_path
                temp_path = path
                return os.open(
                    path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )

            def replace_temp(descriptor, published, *, root, mode):
                self.assertIsInstance(descriptor, int)
                self.assertEqual(os.fstat(descriptor).st_size, len(b"content\n"))
                self.assertEqual(published, destination)
                os.replace(temp_path, published)

            with (
                patch.object(beacon_sync_protocol.os, "name", "nt"),
                patch.object(beacon_sync_protocol, "Path", pathlib.PosixPath),
                patch.object(
                    beacon_sync_protocol,
                    "_windows_create_exclusive_temp",
                    side_effect=create_temp,
                ),
                patch.object(
                    beacon_sync_protocol,
                    "_windows_atomic_replace",
                    side_effect=replace_temp,
                ),
                patch.object(
                    beacon_sync_protocol,
                    "portable_unlink_regular",
                    side_effect=lambda path, **_kwargs: (
                        os.unlink(path) if os.path.lexists(path) else False
                    ),
                ),
                patch.object(
                    beacon_sync_protocol,
                    "_assert_supported_windows_atomic_filesystem",
                ),
            ):
                portable_atomic_write(
                    destination,
                    b"content\n",
                    root=root,
                )

            self.assertEqual(destination.read_bytes(), b"content\n")

    def test_windows_atomic_filesystem_requires_1809_or_server_2019(self):
        unsupported = type(
            "WindowsVersion",
            (),
            {"major": 10, "build": 17134},
        )()
        supported = type(
            "WindowsVersion",
            (),
            {"major": 10, "build": 17763},
        )()

        with self.assertRaisesRegex(ProtocolError, "17763"):
            beacon_sync_protocol._assert_supported_windows_atomic_filesystem(
                unsupported
            )
        beacon_sync_protocol._assert_supported_windows_atomic_filesystem(
            supported
        )

    def test_windows_rename_buffer_never_undersizes_native_structure(self):
        with self.assertRaisesRegex(ProtocolError, "rename buffer"):
            beacon_sync_protocol._windows_rename_buffer_size(0, 20, 24)
        self.assertEqual(
            beacon_sync_protocol._windows_rename_buffer_size(2, 20, 24),
            26,
        )
        self.assertEqual(
            beacon_sync_protocol._windows_rename_buffer_size(12, 20, 24),
            36,
        )

    def test_windows_portable_directory_tree_avoids_posix_dir_fd_api(self):
        if os.name == "nt":
            self.skipTest("non-Windows branch simulation")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "state" / "replica" / "staging"
            with (
                patch.object(beacon_sync_protocol.os, "name", "nt"),
                patch.object(beacon_sync_protocol, "Path", pathlib.PosixPath),
            ):
                result = portable_ensure_directory_tree(target, root=root)

            self.assertEqual(result, target)
            self.assertTrue(target.is_dir())

    def test_immutable_write_accepts_same_bytes_and_rejects_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "bundle" / "event.json"

            self.assertTrue(write_immutable(path, b"same\n", root=root))
            self.assertFalse(write_immutable(path, b"same\n", root=root))
            with self.assertRaisesRegex(ProtocolError, "immutable"):
                write_immutable(path, b"different\n", root=root)

    def test_bounded_reader_rejects_oversize_symlink_and_non_regular(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            file_path = root / "value"
            file_path.write_bytes(b"12345")
            self.assertEqual(
                read_bounded_regular_file(file_path, max_bytes=5, root=root),
                b"12345",
            )
            with self.assertRaisesRegex(ProtocolError, "size"):
                read_bounded_regular_file(file_path, max_bytes=4, root=root)
            with self.assertRaisesRegex(ProtocolError, "regular"):
                read_bounded_regular_file(root, max_bytes=10, root=root)

    def test_windows_bounded_reader_uses_one_pinned_verified_handle(self):
        if os.name == "nt":
            self.skipTest("non-Windows branch simulation")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "value"
            source.write_bytes(b"pinned")

            def open_verified(path, *, root):
                return os.open(path, os.O_RDONLY)

            with (
                patch.object(beacon_sync_protocol.os, "name", "nt"),
                patch.object(beacon_sync_protocol, "Path", pathlib.PosixPath),
                patch.object(
                    beacon_sync_protocol,
                    "_windows_open_regular_for_read",
                    side_effect=open_verified,
                    create=True,
                ) as opened,
            ):
                data = read_bounded_regular_file(
                    source,
                    max_bytes=16,
                    root=root,
                )

            self.assertEqual(data, b"pinned")
            opened.assert_called_once_with(source, root=root)

    def test_portable_primitives_reject_intermediate_and_final_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            target = real / "value"
            target.write_bytes(b"safe")
            directory_link = root / "linked-directory"
            lock_target = root / "real.lock"
            lock_target.write_bytes(b"")
            lock_link = root / "linked.lock"
            try:
                directory_link.symlink_to(real, target_is_directory=True)
                lock_link.symlink_to(lock_target)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")

            with self.assertRaisesRegex(ProtocolError, "symlink"):
                read_bounded_regular_file(
                    directory_link / "value",
                    max_bytes=16,
                    root=root,
                )
            with self.assertRaisesRegex(ProtocolError, "symlink"):
                with portable_file_lock(lock_link, root=root):
                    pass
            self.assertEqual(lock_target.read_bytes(), b"")

    def test_windows_portable_lock_uses_one_pinned_verified_handle(self):
        if os.name == "nt":
            self.skipTest("non-Windows branch simulation")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lock_path = root / "state" / "worker.lock"
            opened_descriptors = []

            def open_verified(path, *, root):
                descriptor = os.open(
                    path,
                    os.O_RDWR | os.O_CREAT,
                    0o600,
                )
                opened_descriptors.append(descriptor)
                return descriptor

            fake_msvcrt = type(
                "FakeMsvcrt",
                (),
                {
                    "LK_LOCK": 1,
                    "LK_NBLCK": 2,
                    "LK_UNLCK": 3,
                    "locking": staticmethod(
                        lambda descriptor, _mode, _bytes: os.fstat(descriptor)
                    ),
                },
            )
            with (
                patch.object(beacon_sync_protocol.os, "name", "nt"),
                patch.object(beacon_sync_protocol, "Path", pathlib.PosixPath),
                patch.object(
                    beacon_sync_protocol,
                    "_windows_open_lock_file",
                    side_effect=open_verified,
                    create=True,
                ) as opened,
                patch.dict(sys.modules, {"msvcrt": fake_msvcrt}),
            ):
                with portable_file_lock(lock_path, root=root):
                    self.assertTrue(lock_path.is_file())

            opened.assert_called_once_with(lock_path, root=root)
            self.assertTrue(opened_descriptors)

    def test_portable_unlink_pins_parent_during_intermediate_symlink_swap(self):
        if os.name == "nt":
            self.skipTest("POSIX descriptor-pinning test")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            managed = root / "managed"
            managed.mkdir()
            victim = managed / "value.json"
            victim.write_bytes(b"managed")
            outside = root / "outside"
            outside.mkdir()
            outside_victim = outside / victim.name
            outside_victim.write_bytes(b"outside")
            held = root / "managed-held"
            original_identity = safety._destination_identity
            swapped = False

            def swap_parent_after_identity(parent_fd, leaf):
                nonlocal swapped
                result = original_identity(parent_fd, leaf)
                if not swapped:
                    swapped = True
                    managed.rename(held)
                    managed.symlink_to(outside, target_is_directory=True)
                return result

            with patch.object(
                safety,
                "_destination_identity",
                side_effect=swap_parent_after_identity,
            ):
                with self.assertRaisesRegex(ProtocolError, "replaced"):
                    portable_unlink_regular(
                        victim,
                        root=root,
                        expected_identity=os.lstat(victim),
                    )

            self.assertTrue(outside_victim.is_file())
            self.assertEqual(outside_victim.read_bytes(), b"outside")
            self.assertFalse((held / victim.name).exists())

    def test_portable_rmtree_removes_only_real_tree_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tree = root / "tree"
            (tree / "nested").mkdir(parents=True)
            (tree / "nested" / "value").write_bytes(b"managed")

            self.assertTrue(portable_rmtree(tree, root=root))
            self.assertFalse(tree.exists())

            tree.mkdir()
            outside = root / "outside"
            outside.mkdir()
            outside_value = outside / "keep"
            outside_value.write_bytes(b"outside")
            link = tree / "linked"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlinks unavailable")

            with self.assertRaisesRegex(ProtocolError, "unsafe"):
                portable_rmtree(tree, root=root)

            self.assertTrue(outside_value.is_file())
            self.assertEqual(outside_value.read_bytes(), b"outside")

    def _event(self, seq=1):
        return build_event(
            device_id="windows-gpu-a1b2c3d4",
            producer_instance_id="12345678-1234-4234-9234-123456789abc",
            seq=seq,
            event_kind="transcript.chunk",
            created_at="2026-07-31T12:00:00Z",
            agent="codex",
            session_id="019fa000-1111-7222-8333-123456789abc",
            stream_epoch="87654321-4321-4234-9234-cba987654321",
            source_cursor={"start": 0, "end": 10},
            metadata={"cwd": "C:\\work\\demo", "is_subagent": False},
            payload={
                "sha256": "a" * 64,
                "bytes": 10,
                "media_type": "application/x-ndjson",
                "role": "transcript-source",
            },
        )

    def _golden_transcript_event(self, event_kind):
        role = (
            "transcript-source"
            if event_kind == "transcript.chunk"
            else "transcript-gap"
        )
        return build_event(
            device_id="windows-gpu-a1b2c3d4",
            producer_instance_id="12345678-1234-4234-9234-123456789abc",
            seq=7,
            event_kind=event_kind,
            created_at="2026-07-31T12:00:00Z",
            agent="codex",
            session_id="019fa000-1111-7222-8333-123456789abc",
            stream_epoch="87654321-4321-4234-9234-cba987654321",
            source_cursor={"start": 10, "end": 20},
            metadata={"cwd": "C:\\work\\demo", "is_subagent": False},
            payload={
                "sha256": "a" * 64,
                "bytes": 10,
                "media_type": "application/x-ndjson",
                "role": role,
            },
        )

    def _attachment_event(self, original_name="diagram.png"):
        stream_id = "stream-" + hashlib.sha256(
            b"codex\x00019fa000-1111-7222-8333-123456789abc"
        ).hexdigest()
        reference_identity = {
            "original_name": original_name,
            "payload_sha256": "b" * 64,
            "producer_instance_id": "12345678-1234-4234-9234-123456789abc",
            "source_cursor": {"start": 20, "end": 120},
            "source_locator_sha256": "c" * 64,
            "stream_epoch": "87654321-4321-4234-9234-cba987654321",
            "stream_id": stream_id,
        }
        reference_id = "reference-" + hashlib.sha256(
            canonical_json_bytes(reference_identity)
        ).hexdigest()
        return build_event(
            device_id="windows-gpu-a1b2c3d4",
            producer_instance_id="12345678-1234-4234-9234-123456789abc",
            seq=8,
            event_kind="attachment.blob",
            created_at="2026-07-31T12:00:00Z",
            agent="codex",
            session_id="019fa000-1111-7222-8333-123456789abc",
            stream_epoch="87654321-4321-4234-9234-cba987654321",
            source_cursor={"start": 20, "end": 120},
            metadata={"cwd": "C:\\work\\demo", "is_subagent": False},
            payload={
                "sha256": "b" * 64,
                "bytes": 8,
                "media_type": "image/png",
                "role": "attachment-source",
            },
            extensions={
                "attachment": {
                    "reference_id": reference_id,
                    "original_name": original_name,
                    "source_locator_sha256": "c" * 64,
                    "reference_kind": "codex.local_image",
                }
            },
        )

    def _legacy_attachment_event(self):
        stream_id = "stream-" + hashlib.sha256(
            b"codex\x00019fa000-1111-7222-8333-123456789abc"
        ).hexdigest()
        attachment_identity = {
            "original_name": "diagram.png",
            "payload_sha256": "b" * 64,
            "source_locator_sha256": "c" * 64,
        }
        event = {
            "protocol": "agent-memory-beacon-sync-event",
            "schema_version": 1,
            "device_id": "windows-gpu-a1b2c3d4",
            "producer_instance_id": "12345678-1234-4234-9234-123456789abc",
            "seq": 8,
            "event_id": "",
            "event_kind": "attachment.blob",
            "created_at": "2026-07-31T12:00:00Z",
            "agent": "codex",
            "session_id": "019fa000-1111-7222-8333-123456789abc",
            "stream_id": stream_id,
            "stream_epoch": "87654321-4321-4234-9234-cba987654321",
            "logical_record_id": (
                "session:codex:019fa000-1111-7222-8333-123456789abc"
            ),
            "source_cursor": {"start": 0, "end": 8},
            "metadata": {"cwd": "C:\\work\\demo", "is_subagent": False},
            "payload": {
                "sha256": "b" * 64,
                "bytes": 8,
                "media_type": "image/png",
                "role": "attachment-source",
            },
            "extensions": {
                "attachment": {
                    "attachment_id": "attachment-"
                    + hashlib.sha256(
                        canonical_json_bytes(attachment_identity)
                    ).hexdigest(),
                    "original_name": "diagram.png",
                    "source_locator_sha256": "c" * 64,
                    "reference_kind": "codex.local_image",
                    "transcript_cursor": {"start": 20, "end": 120},
                }
            },
        }
        event["event_id"] = derive_event_id(event)
        return event

    def _legacy_attachment_ready(self, event, event_bytes):
        return {
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


if __name__ == "__main__":
    unittest.main()
