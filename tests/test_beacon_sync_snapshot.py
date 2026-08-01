import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from beacon_sync_producer import collect_transcripts
from beacon_sync_protocol import canonical_json_bytes, sha256_bytes
from beacon_sync_reducer import list_ledger_events, reduce_inboxes
import beacon_sync_snapshot
from beacon_sync_snapshot import (
    MaterializeError,
    PublisherError,
    materialize_generation,
    publish_generation,
    publish_pending_receipts,
)


NOW = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)


class BeaconSyncSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.publisher_state = self.root / "authority-state"
        self.published = self.root / "published"
        self.received = self.root / "received-published"
        self.replica = self.root / "replica"
        self.replica_state = self.root / "replica-state"
        self.cfg = {"vault_path": str(self.vault)}
        self.authority_sync = {
            "state_dir": str(self.publisher_state),
            "published_dir": str(self.published),
        }
        self.replica_sync = {
            "state_dir": str(self.replica_state),
            "received_published_dir": str(self.received),
            "replica_path": str(self.replica),
            "max_replica_object_bytes": 64 * 1024 * 1024,
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_publisher_excludes_volatile_and_credentials_and_marks_skill_source(self):
        self._write("01-Projects/demo/Memory/decisions.md", "decision\n")
        self._write("04-Feedback/_logs/private.log", "log\n")
        self._write("05-Agent-Memory/codex-profile/auth.json", "secret\n")
        self._write(
            ".obsidian/plugins/example/data.json",
            '{"access_token":"plugin-secret"}\n',
        )
        self._write(
            "01-Projects/demo/config.json",
            '{"api_key":"project-secret"}\n',
        )
        self._write(
            "05-Agent-Memory/codex-profile/skills/demo/settings.yaml",
            "token: skill-secret\n",
        )
        self._write(
            "05-Agent-Memory/codex-profile/skills/demo/SKILL.md",
            "# Demo\n",
        )
        self._write(
            "05-Agent-Memory/codex-profile/skills/demo/tool.py",
            "print('not executable by replica')\n",
        )

        result = publish_generation(self.cfg, self.authority_sync, now=NOW)
        snapshot = self._published_snapshot(result["generation"])
        by_path = {item["path"]: item for item in snapshot["files"]}

        self.assertTrue(result["changed"])
        self.assertIn("01-Projects/demo/Memory/decisions.md", by_path)
        self.assertNotIn("04-Feedback/_logs/private.log", by_path)
        self.assertNotIn("05-Agent-Memory/codex-profile/auth.json", by_path)
        self.assertNotIn(".obsidian/plugins/example/data.json", by_path)
        self.assertNotIn("01-Projects/demo/config.json", by_path)
        self.assertNotIn(
            "05-Agent-Memory/codex-profile/skills/demo/settings.yaml",
            by_path,
        )
        self.assertEqual(
            by_path[
                "05-Agent-Memory/codex-profile/skills/demo/SKILL.md"
            ]["content_class"],
            "skill-source",
        )
        self.assertEqual(
            by_path[
                "05-Agent-Memory/codex-profile/skills/demo/tool.py"
            ]["content_class"],
            "skill-source",
        )

    def test_publisher_rejects_embedded_credential_before_object_publication(self):
        credential = "sk-live-abcdefghijklmnopqrstuvwxyz012345"
        self._write(
            "01-Projects/demo/Memory/private-note.md",
            f"临时值: {credential}\n",
        )

        with self.assertRaisesRegex(PublisherError, "credential"):
            publish_generation(self.cfg, self.authority_sync, now=NOW)

        object_root = self.published / "v1" / "objects"
        for path in object_root.rglob("*") if object_root.exists() else ():
            if path.is_file():
                self.assertNotIn(credential.encode("utf-8"), path.read_bytes())
        self.assertFalse((self.published / "v1" / "current.json").exists())

    def test_publisher_scans_vault_under_shared_harvester_lock(self):
        self._write("note.md", "locked\n")
        calls = []

        @contextmanager
        def recording_lock(path, *, root):
            calls.append(("enter", Path(path), Path(root)))
            yield
            calls.append(("exit", Path(path), Path(root)))

        with patch.object(
            beacon_sync_snapshot,
            "exclusive_file_lock",
            recording_lock,
            create=True,
        ):
            publish_generation(self.cfg, self.authority_sync, now=NOW)

        expected = self.vault / "04-Feedback" / "_logs" / "harvester.lock"
        self.assertEqual(calls[0], ("enter", expected, self.vault))
        self.assertEqual(calls[-1], ("exit", expected, self.vault))

    def test_unchanged_vault_reuses_current_generation(self):
        self._write("note.md", "same\n")
        first = publish_generation(self.cfg, self.authority_sync, now=NOW)

        second = publish_generation(self.cfg, self.authority_sync, now=NOW)

        self.assertFalse(second["changed"])
        self.assertEqual(second["generation"], first["generation"])
        self.assertEqual(second["generation_id"], first["generation_id"])
        self.assertEqual(
            len(list((self.published / "v1" / "snapshots").iterdir())),
            1,
        )

    def test_empty_initial_vault_publishes_generation_one(self):
        first = publish_generation(
            self.cfg,
            self.authority_sync,
            now=NOW,
        )
        second = publish_generation(
            self.cfg,
            self.authority_sync,
            now=NOW,
        )
        snapshot = self._published_snapshot(1)
        complete = json.loads(
            (
                self.published
                / "v1"
                / "snapshots"
                / f"{1:020d}"
                / "complete.json"
            ).read_text(encoding="utf-8")
        )

        self.assertTrue(first["changed"])
        self.assertEqual(first["generation"], 1)
        self.assertEqual(snapshot["files"], [])
        self.assertEqual(snapshot["tombstones"], [])
        self.assertEqual(complete["file_count"], 0)
        self.assertEqual(complete["object_bytes"], 0)
        self.assertFalse(second["changed"])
        self.assertEqual(second["generation_id"], first["generation_id"])

    def test_retry_adopts_complete_orphan_when_vault_is_unchanged(self):
        self._write("note.md", "one\n")

        with self._crash_before_current():
            with self.assertRaisesRegex(PublisherError, "injected current crash"):
                publish_generation(self.cfg, self.authority_sync, now=NOW)

        orphan = self.published / "v1" / "snapshots" / f"{1:020d}"
        self.assertTrue((orphan / "snapshot.json").is_file())
        self.assertTrue((orphan / "complete.json").is_file())
        self.assertFalse((self.published / "v1" / "current.json").exists())

        recovered = publish_generation(self.cfg, self.authority_sync, now=NOW)

        self.assertTrue(recovered["changed"])
        self.assertEqual(recovered["generation"], 1)
        self.assertEqual(
            len(list((self.published / "v1" / "snapshots").iterdir())),
            1,
        )
        self.assertEqual(
            json.loads(
                (self.published / "v1" / "current.json").read_text(
                    encoding="utf-8"
                )
            )["generation_id"],
            recovered["generation_id"],
        )

    def test_retry_publishes_after_changed_complete_orphan_and_continues(self):
        self._write("note.md", "one\n")

        with self._crash_before_current():
            with self.assertRaisesRegex(PublisherError, "injected current crash"):
                publish_generation(self.cfg, self.authority_sync, now=NOW)

        self._write("note.md", "two\n")
        second = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._write("note.md", "three\n")
        third = publish_generation(self.cfg, self.authority_sync, now=NOW)

        second_snapshot = self._published_snapshot(second["generation"])
        third_snapshot = self._published_snapshot(third["generation"])
        self.assertEqual(second["generation"], 2)
        self.assertEqual(second_snapshot["parent_generation"], 1)
        self.assertEqual(third["generation"], 3)
        self.assertEqual(third_snapshot["parent_generation"], 2)
        self.assertEqual(
            json.loads(
                (self.published / "v1" / "current.json").read_text(
                    encoding="utf-8"
                )
            )["generation"],
            3,
        )

    def test_retry_reclaims_incomplete_orphan_before_reusing_generation(self):
        self._write("note.md", "one\n")

        with self._crash_before_complete():
            with self.assertRaisesRegex(PublisherError, "injected complete crash"):
                publish_generation(self.cfg, self.authority_sync, now=NOW)

        incomplete = self.published / "v1" / "snapshots" / f"{1:020d}"
        self.assertTrue((incomplete / "snapshot.json").is_file())
        self.assertFalse((incomplete / "complete.json").exists())
        self._write("note.md", "two\n")

        recovered = publish_generation(self.cfg, self.authority_sync, now=NOW)

        self.assertEqual(recovered["generation"], 1)
        self.assertIn(1, recovered["cleanup"]["removed_generations"])
        snapshot = self._published_snapshot(1)
        note = next(item for item in snapshot["files"] if item["path"] == "note.md")
        self.assertEqual(
            (
                self.published
                / "v1"
                / "objects"
                / note["sha256"][:2]
                / note["sha256"]
            ).read_text(encoding="utf-8"),
            "two\n",
        )

    def test_publisher_uses_configured_limit_for_previous_snapshot_manifest(self):
        self._write("empty.md", "")
        first = publish_generation(self.cfg, self.authority_sync, now=NOW)
        current_path = self.published / "v1" / "current.json"
        snapshot_path = (
            self.published
            / "v1"
            / "snapshots"
            / f"{first['generation']:020d}"
            / "snapshot.json"
        )
        self.assertLess(current_path.stat().st_size, snapshot_path.stat().st_size)
        self.authority_sync["max_replica_object_bytes"] = (
            current_path.stat().st_size + snapshot_path.stat().st_size
        ) // 2

        with self.assertRaisesRegex(PublisherError, "size limit"):
            publish_generation(self.cfg, self.authority_sync, now=NOW)

        self.assertEqual(
            json.loads(
                (self.published / "v1" / "current.json").read_text(
                    encoding="utf-8"
                )
            )["generation"],
            first["generation"],
        )

    def test_publisher_wraps_invalid_previous_generation_as_publisher_error(self):
        self._write("note.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        (self.published / "v1" / "current.json").write_bytes(
            canonical_json_bytes({})
        )

        with self.assertRaises(PublisherError):
            publish_generation(self.cfg, self.authority_sync, now=NOW)

    def test_change_and_delete_create_new_generation_and_tombstone(self):
        path = self._write("notes/a.md", "one\n")
        first = publish_generation(self.cfg, self.authority_sync, now=NOW)
        path.write_text("two\n", encoding="utf-8")
        second = publish_generation(self.cfg, self.authority_sync, now=NOW)
        path.unlink()
        third = publish_generation(self.cfg, self.authority_sync, now=NOW)
        snapshot = self._published_snapshot(third["generation"])

        self.assertEqual(
            [first["generation"], second["generation"], third["generation"]],
            [1, 2, 3],
        )
        self.assertEqual(snapshot["parent_generation"], 2)
        self.assertEqual(
            snapshot["tombstones"],
            [{"deleted_at_generation": 3, "path": "notes/a.md"}],
        )

    def test_materializer_supports_file_to_directory_shape_transition(self):
        original = self._write("notes/topic.md", "old file\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        original.unlink()
        self._write("notes/topic.md/child.md", "new child\n")
        second = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()

        result = materialize_generation(self.replica_sync, now=NOW)

        self.assertEqual(result["generation"], second["generation"])
        self.assertTrue((self.replica / "notes/topic.md").is_dir())
        self.assertEqual(
            (self.replica / "notes/topic.md/child.md").read_text(),
            "new child\n",
        )

    def test_materializer_supports_directory_to_file_shape_transition(self):
        old = self._write("notes/topic/child.md", "old child\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        old.unlink()
        old.parent.rmdir()
        self._write("notes/topic", "new file\n")
        second = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()

        result = materialize_generation(self.replica_sync, now=NOW)

        self.assertEqual(result["generation"], second["generation"])
        self.assertTrue((self.replica / "notes/topic").is_file())
        self.assertEqual(
            (self.replica / "notes/topic").read_text(),
            "new file\n",
        )

    def test_shape_transition_failure_restores_original_file(self):
        original = self._write("notes/topic.md", "old file\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        original.unlink()
        self._write("notes/topic.md/child.md", "new child\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()

        with self.assertRaisesRegex(MaterializeError, "injected"):
            materialize_generation(
                self.replica_sync,
                now=NOW,
                fault_point="after_first_apply",
            )

        restored = self.replica / "notes/topic.md"
        self.assertTrue(restored.is_file())
        self.assertEqual(restored.read_text(), "old file\n")

    def test_shape_transition_collision_preserves_unmanaged_local_file(self):
        old = self._write("notes/topic/child.md", "old child\n")
        first = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        local = self.replica / "notes/topic/local.txt"
        local.write_text("keep local\n", encoding="utf-8")
        old.unlink()
        old.parent.rmdir()
        self._write("notes/topic", "new file\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()

        with self.assertRaises(MaterializeError):
            materialize_generation(self.replica_sync, now=NOW)

        self.assertEqual(local.read_text(), "keep local\n")
        self.assertEqual(
            (self.replica / "notes/topic/child.md").read_text(),
            "old child\n",
        )
        self.assertEqual(self._active()["generation"], first["generation"])
        self.assertFalse(
            (
                self.replica_state
                / "replica"
                / "apply-journal.json"
            ).exists()
        )

    def test_current_references_only_complete_snapshot_and_objects(self):
        self._write("note.md", "content\n")
        result = publish_generation(self.cfg, self.authority_sync, now=NOW)
        current = json.loads(
            (self.published / "v1" / "current.json").read_text(encoding="utf-8")
        )
        generation_dir = (
            self.published
            / "v1"
            / "snapshots"
            / f"{result['generation']:020d}"
        )
        snapshot = json.loads(
            (generation_dir / "snapshot.json").read_text(encoding="utf-8")
        )
        complete = json.loads(
            (generation_dir / "complete.json").read_text(encoding="utf-8")
        )
        item = snapshot["files"][0]

        self.assertEqual(current["generation_id"], snapshot["generation_id"])
        self.assertEqual(
            complete["snapshot_sha256"],
            current["snapshot_sha256"],
        )
        self.assertTrue(
            (
                self.published
                / "v1"
                / "objects"
                / item["sha256"][:2]
                / item["sha256"]
            ).is_file()
        )

    def test_case_colliding_vault_paths_fail_before_current_changes(self):
        upper = self._write("Notes/A.md", "A\n")
        lower = self._write("notes/a.md", "a\n")
        if upper.samefile(lower):
            self.skipTest("filesystem does not permit case-colliding paths")

        with self.assertRaisesRegex(PublisherError, "case"):
            publish_generation(self.cfg, self.authority_sync, now=NOW)

        self.assertFalse((self.published / "v1" / "current.json").exists())

    def test_publisher_rejects_over_4mib_apply_operations_before_current(self):
        files = self._operation_heavy_files()
        self.assertGreater(
            len(self._bootstrap_apply_journal_bytes(files)),
            beacon_sync_snapshot.MAX_APPLY_JOURNAL_BYTES,
        )

        with patch.object(
            beacon_sync_snapshot,
            "_collect_vault_files",
            return_value=files,
        ), patch.object(
            beacon_sync_snapshot,
            "_cleanup_published_storage",
        ), patch.object(
            beacon_sync_snapshot,
            "bind_pending_receipt_generation",
            return_value=0,
        ):
            with self.assertRaisesRegex(
                PublisherError,
                "apply journal exceeds",
            ):
                publish_generation(self.cfg, self.authority_sync, now=NOW)

        self.assertFalse((self.published / "v1" / "current.json").exists())

    def test_materializer_bootstraps_verified_generation_and_sets_read_only(self):
        self._write("notes/a.md", "hello\n")
        published = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()

        result = materialize_generation(
            self.replica_sync,
            now=NOW,
            bootstrap=True,
        )

        destination = self.replica / "notes" / "a.md"
        self.assertTrue(result["changed"])
        self.assertEqual(destination.read_text(encoding="utf-8"), "hello\n")
        self.assertEqual(
            stat.S_IMODE(destination.stat().st_mode) & stat.S_IWUSR,
            0,
        )
        active = json.loads(
            (
                self.replica_state
                / "replica"
                / "active-generation.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(active["generation"], published["generation"])
        staging_root = self.replica_state / "replica" / "staging"
        self.assertEqual(
            list(staging_root.iterdir()) if staging_root.is_dir() else [],
            [],
        )

    def test_same_generation_rechecks_read_only_replica_mode(self):
        self._write("a.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        destination = self.replica / "a.md"
        os.chmod(destination, 0o644)

        with self.assertRaisesRegex(MaterializeError, "drift"):
            materialize_generation(self.replica_sync, now=NOW)

    def test_materializer_applies_all_available_intermediate_generations(self):
        self._write("a.md", "one\n")
        first = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)

        self._write("a.md", "two\n")
        second = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._write("a.md", "three\n")
        third = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()

        result = materialize_generation(self.replica_sync, now=NOW)

        self.assertTrue(result["changed"])
        self.assertEqual(result["generation"], third["generation"])
        self.assertEqual((self.replica / "a.md").read_text(), "three\n")
        self.assertEqual(self._active()["generation"], third["generation"])
        active_root = self.replica_state / "replica" / "active"
        self.assertFalse(
            (active_root / f"{second['generation']:020d}.json").is_file()
        )
        self.assertTrue(
            (active_root / f"{third['generation']:020d}.json").is_file()
        )
        self.assertIn(
            second["generation"],
            result["cleanup"]["removed_active_snapshots"],
        )
        self.assertEqual(first["generation"], 1)

    def test_materializer_bounds_generation_work_and_advances_fairly(self):
        self._write("a.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        published = []
        for value in ("two\n", "three\n", "four\n"):
            self._write("a.md", value)
            published.append(
                publish_generation(self.cfg, self.authority_sync, now=NOW)
            )
        self._sync_published()

        with patch.object(
            beacon_sync_snapshot,
            "MAX_GENERATIONS_PER_MATERIALIZE_RUN",
            1,
        ):
            results = [
                materialize_generation(self.replica_sync, now=NOW)
                for _ in range(3)
            ]

        self.assertEqual(
            [result["generation"] for result in results],
            [item["generation"] for item in published],
        )
        self.assertEqual([result["limited"] for result in results], [True, True, False])
        self.assertEqual(
            [result["pending_generations"] for result in results],
            [2, 1, 0],
        )
        self.assertEqual((self.replica / "a.md").read_text(), "four\n")

    def test_materializer_uses_configured_limit_for_active_snapshot_manifest(self):
        self._write("empty.md", "")
        published = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        current_path = self.received / "v1" / "current.json"
        active_snapshot = (
            self.replica_state
            / "replica"
            / "active"
            / f"{published['generation']:020d}.json"
        )
        self.assertLess(
            current_path.stat().st_size,
            active_snapshot.stat().st_size,
        )
        self.replica_sync["max_replica_object_bytes"] = (
            current_path.stat().st_size + active_snapshot.stat().st_size
        ) // 2

        with self.assertRaisesRegex(MaterializeError, "size limit"):
            materialize_generation(self.replica_sync, now=NOW)

    def test_missing_or_corrupt_object_keeps_previous_generation_active(self):
        self._write("notes/a.md", "one\n")
        first = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        self._write("notes/a.md", "two\n")
        second = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        snapshot = self._received_snapshot(second["generation"])
        changed = next(item for item in snapshot["files"] if item["path"] == "notes/a.md")
        object_path = (
            self.received
            / "v1"
            / "objects"
            / changed["sha256"][:2]
            / changed["sha256"]
        )
        object_path.unlink()

        with self.assertRaisesRegex(MaterializeError, "object"):
            materialize_generation(self.replica_sync, now=NOW)

        self.assertEqual((self.replica / "notes/a.md").read_text(), "one\n")
        active = self._active()
        self.assertEqual(active["generation"], first["generation"])

    def test_parent_generation_mismatch_does_not_skip_history(self):
        self._write("a.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._write("a.md", "two\n")
        second = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        fake_active_dir = self.replica_state / "replica" / "active"
        fake_active_dir.mkdir(parents=True)
        (fake_active_dir / "00000000000000000099.json").write_text(
            json.dumps({"files": [], "generation": 99}),
            encoding="utf-8",
        )
        (self.replica_state / "replica" / "active-generation.json").write_bytes(
            canonical_json_bytes(
                {
                    "protocol": "agent-memory-beacon-sync-active",
                    "schema_version": 1,
                    "generation": 99,
                    "generation_id": "generation-" + ("f" * 64),
                    "snapshot_sha256": "f" * 64,
                }
            )
        )

        with self.assertRaisesRegex(MaterializeError, "parent"):
            materialize_generation(self.replica_sync, now=NOW)

    def test_materializer_rejects_tombstones_that_do_not_match_transition(self):
        self._write("keep.md", "one\n")
        self._write("remove.md", "remove\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        (self.vault / "remove.md").unlink()
        second = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        snapshot = self._received_snapshot(second["generation"])
        snapshot["tombstones"] = []
        self._rewrite_received_snapshot(snapshot)

        with self.assertRaisesRegex(MaterializeError, "tombstones.*transition"):
            materialize_generation(self.replica_sync, now=NOW)

        self.assertTrue((self.replica / "remove.md").is_file())
        self.assertEqual(self._active()["generation"], 1)

    def test_materializer_rejects_tombstones_in_initial_generation(self):
        self._write("keep.md", "one\n")
        first = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        snapshot = self._received_snapshot(first["generation"])
        snapshot["tombstones"] = [
            {
                "path": "notes/ghost.md",
                "deleted_at_generation": first["generation"],
            }
        ]
        self._rewrite_received_snapshot(snapshot)

        with self.assertRaisesRegex(MaterializeError, "initial.*tombstones"):
            materialize_generation(
                self.replica_sync,
                now=NOW,
                bootstrap=True,
            )

    def test_materializer_rejects_disallowed_received_path(self):
        self._write("note.md", "safe\n")
        generation = publish_generation(
            self.cfg,
            self.authority_sync,
            now=NOW,
        )
        self._sync_published()
        snapshot = self._received_snapshot(generation["generation"])
        snapshot["files"][0]["path"] = ".obsidian/plugins/evil/main.js"
        self._rewrite_received_snapshot(snapshot)

        with self.assertRaisesRegex(MaterializeError, "not allowed"):
            materialize_generation(
                self.replica_sync,
                now=NOW,
                bootstrap=True,
            )

        self.assertEqual(list(self.replica.iterdir()), [])

    def test_materializer_rejects_content_class_path_mismatch(self):
        self._write("note.md", "safe\n")
        generation = publish_generation(
            self.cfg,
            self.authority_sync,
            now=NOW,
        )
        self._sync_published()
        snapshot = self._received_snapshot(generation["generation"])
        snapshot["files"][0]["content_class"] = "skill-source"
        self._rewrite_received_snapshot(snapshot)

        with self.assertRaisesRegex(MaterializeError, "content class"):
            materialize_generation(
                self.replica_sync,
                now=NOW,
                bootstrap=True,
            )

    def test_materializer_rejects_file_directory_prefix_conflict(self):
        self._write("notes/source.md", "safe\n")
        generation = publish_generation(
            self.cfg,
            self.authority_sync,
            now=NOW,
        )
        self._sync_published()
        snapshot = self._received_snapshot(generation["generation"])
        first = dict(snapshot["files"][0])
        first["path"] = "notes/Foo.md"
        second = dict(first)
        second["path"] = "notes/foo.md/child.md"
        snapshot["files"] = [first, second]
        self._rewrite_received_snapshot(snapshot)

        with self.assertRaisesRegex(MaterializeError, "prefix conflict"):
            materialize_generation(
                self.replica_sync,
                now=NOW,
                bootstrap=True,
            )

    def test_materializer_rejects_tombstone_outside_replica_policy(self):
        self._write("note.md", "safe\n")
        generation = publish_generation(
            self.cfg,
            self.authority_sync,
            now=NOW,
        )
        self._sync_published()
        snapshot = self._received_snapshot(generation["generation"])
        snapshot["tombstones"] = [
            {
                "path": ".obsidian/plugins/evil/data.json",
                "deleted_at_generation": generation["generation"],
            }
        ]
        self._rewrite_received_snapshot(snapshot)

        with self.assertRaisesRegex(MaterializeError, "not allowed"):
            materialize_generation(
                self.replica_sync,
                now=NOW,
                bootstrap=True,
            )

    def test_local_managed_drift_is_not_overwritten(self):
        self._write("a.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        destination = self.replica / "a.md"
        os.chmod(destination, 0o600)
        destination.write_text("local edit\n", encoding="utf-8")
        self._write("a.md", "two\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()

        with self.assertRaisesRegex(MaterializeError, "drift"):
            materialize_generation(self.replica_sync, now=NOW)

        self.assertEqual(destination.read_text(), "local edit\n")

    def test_local_edit_during_staging_is_detected_before_apply(self):
        self._write("a.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        self._write("a.md", "two\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        destination = self.replica / "a.md"
        real_stage = beacon_sync_snapshot._stage_generation

        def stage_then_edit(*args, **kwargs):
            staging = real_stage(*args, **kwargs)
            destination.chmod(0o600)
            destination.write_text("edit during staging\n", encoding="utf-8")
            return staging

        with patch.object(
            beacon_sync_snapshot,
            "_stage_generation",
            side_effect=stage_then_edit,
        ):
            with self.assertRaisesRegex(MaterializeError, "drift"):
                materialize_generation(self.replica_sync, now=NOW)

        self.assertEqual(destination.read_text(), "edit during staging\n")
        self.assertEqual(self._active()["generation"], 1)
        staging_root = self.replica_state / "replica" / "staging"
        self.assertEqual(
            list(staging_root.iterdir()) if staging_root.is_dir() else [],
            [],
        )

    def test_tampered_staging_bytes_are_rejected_before_replica_write(self):
        self._write("a.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        self._write("a.md", "two\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        destination = self.replica / "a.md"
        replica_writes = []
        real_stage = beacon_sync_snapshot._stage_generation
        real_atomic_write = beacon_sync_snapshot.portable_atomic_write

        def stage_then_tamper(*args, **kwargs):
            staging = real_stage(*args, **kwargs)
            staged = next(path for path in staging.rglob("*") if path.is_file())
            staged.chmod(0o600)
            staged.write_bytes(b"bad\n")
            return staging

        def record_replica_write(path, data, *, root, mode=0o600):
            if Path(path) == destination:
                replica_writes.append(bytes(data))
            return real_atomic_write(path, data, root=root, mode=mode)

        with patch.object(
            beacon_sync_snapshot,
            "_stage_generation",
            side_effect=stage_then_tamper,
        ), patch.object(
            beacon_sync_snapshot,
            "portable_atomic_write",
            side_effect=record_replica_write,
        ):
            with self.assertRaisesRegex(
                MaterializeError,
                "staging object is corrupt",
            ):
                materialize_generation(self.replica_sync, now=NOW)

        self.assertNotIn(b"bad\n", replica_writes)
        self.assertEqual(destination.read_text(encoding="utf-8"), "one\n")
        self.assertEqual(self._active()["generation"], 1)

    def test_materializer_stages_only_changed_objects(self):
        self._write("a.md", "one\n")
        self._write("b.md", "unchanged\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        self._write("a.md", "two\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        staged_files = []
        real_stage = beacon_sync_snapshot._stage_generation

        def inspect_stage(*args, **kwargs):
            staging = real_stage(*args, **kwargs)
            staged_files.extend(
                path
                for path in staging.rglob("*")
                if path.is_file()
            )
            return staging

        with patch.object(
            beacon_sync_snapshot,
            "_stage_generation",
            side_effect=inspect_stage,
        ):
            materialize_generation(self.replica_sync, now=NOW)

        self.assertEqual(len(staged_files), 1)

    def test_materializer_removes_stale_staging_without_apply_journal(self):
        self._write("a.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        stale = (
            self.replica_state
            / "replica"
            / "staging"
            / ("generation-" + ("a" * 64))
            / "aa"
            / ("a" * 64)
        )
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"stale")

        result = materialize_generation(self.replica_sync, now=NOW)

        self.assertFalse(result["changed"])
        self.assertFalse(stale.exists())

    def test_materializer_rejects_snapshot_file_count_over_protocol_limit(self):
        self._write("a.md", "one\n")
        self._write("b.md", "two\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()

        with patch.object(
            beacon_sync_snapshot,
            "MAX_REPLICA_FILE_COUNT",
            1,
            create=True,
        ):
            with self.assertRaisesRegex(MaterializeError, "file count"):
                materialize_generation(
                    self.replica_sync,
                    now=NOW,
                    bootstrap=True,
                )

    def test_materializer_rejects_snapshot_bytes_over_protocol_limit(self):
        self._write("a.md", "12345")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()

        with patch.object(
            beacon_sync_snapshot,
            "MAX_REPLICA_TOTAL_BYTES",
            4,
            create=True,
        ):
            with self.assertRaisesRegex(MaterializeError, "total bytes"):
                materialize_generation(
                    self.replica_sync,
                    now=NOW,
                    bootstrap=True,
                )

    def test_replica_update_never_chmods_a_swapped_symlink_target(self):
        self._write("a.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        self._write("a.md", "two\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        destination = self.replica / "a.md"
        outside = self.root / "outside-update.md"
        outside.write_bytes(b"outside")
        outside.chmod(0o640)
        outside_mode = stat.S_IMODE(outside.stat().st_mode)
        original_chmod = os.chmod
        swapped = False

        def swap_before_chmod(path, mode):
            nonlocal swapped
            if Path(path) == destination and not swapped:
                swapped = True
                destination.unlink()
                destination.symlink_to(outside)
            return original_chmod(path, mode)

        with patch.object(
            beacon_sync_snapshot.os,
            "chmod",
            side_effect=swap_before_chmod,
        ):
            result = materialize_generation(self.replica_sync, now=NOW)

        self.assertTrue(result["changed"])
        self.assertEqual(destination.read_text(encoding="utf-8"), "two\n")
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), outside_mode)

    def test_injected_apply_failure_rolls_back_previous_replica(self):
        self._write("a.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        self._write("a.md", "two\n")
        self._write("b.md", "new\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()

        with self.assertRaisesRegex(MaterializeError, "injected"):
            materialize_generation(
                self.replica_sync,
                now=NOW,
                fault_point="after_first_apply",
            )

        self.assertEqual((self.replica / "a.md").read_text(), "one\n")
        self.assertFalse((self.replica / "b.md").exists())

        completed = materialize_generation(self.replica_sync, now=NOW)
        self.assertTrue(completed["changed"])
        self.assertEqual((self.replica / "a.md").read_text(), "two\n")
        self.assertEqual((self.replica / "b.md").read_text(), "new\n")

    def test_backup_build_failure_cleans_partial_rollback_and_staging(self):
        self._write("a.md", "one\n")
        self._write("b.md", "one\n")
        first = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        self._write("a.md", "two\n")
        self._write("b.md", "two\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        real_atomic_write = beacon_sync_snapshot.portable_atomic_write
        rollback_root = self.replica_state / "replica" / "rollback"
        backup_writes = 0

        def fail_second_backup(path, data, *, root, mode=0o600):
            nonlocal backup_writes
            candidate = Path(path)
            if rollback_root in candidate.parents:
                backup_writes += 1
                if backup_writes == 2:
                    raise OSError("injected backup failure")
            return real_atomic_write(path, data, root=root, mode=mode)

        with patch.object(
            beacon_sync_snapshot,
            "portable_atomic_write",
            side_effect=fail_second_backup,
        ):
            with self.assertRaisesRegex(
                MaterializeError,
                "injected backup failure",
            ):
                materialize_generation(self.replica_sync, now=NOW)

        self.assertEqual((self.replica / "a.md").read_text(), "one\n")
        self.assertEqual((self.replica / "b.md").read_text(), "one\n")
        self.assertEqual(self._active()["generation"], first["generation"])
        staging_root = self.replica_state / "replica" / "staging"
        self.assertEqual(
            list(staging_root.rglob("*")) if staging_root.is_dir() else [],
            [],
        )
        self.assertEqual(
            list(rollback_root.rglob("*")) if rollback_root.is_dir() else [],
            [],
        )
        self.assertFalse(
            (self.replica_state / "replica" / "apply-journal.json").exists()
        )

    def test_materializer_removes_stale_rollback_without_apply_journal(self):
        self._write("a.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        stale = (
            self.replica_state
            / "replica"
            / "rollback"
            / ("generation-" + ("a" * 64))
            / "a.md"
        )
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b"stale backup")

        result = materialize_generation(self.replica_sync, now=NOW)

        self.assertFalse(result["changed"])
        self.assertFalse(stale.exists())

    def test_materializer_bounds_stale_rollback_recovery_scan(self):
        self._write("a.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        rollback_root = self.replica_state / "replica" / "rollback"
        for suffix in ("a", "b"):
            stale = rollback_root / ("generation-" + (suffix * 64)) / "a.md"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"stale backup")

        with patch.object(
            beacon_sync_snapshot,
            "MAX_STALE_ROLLBACK_GENERATIONS",
            1,
            create=True,
        ):
            with self.assertRaisesRegex(
                MaterializeError,
                "stale rollback generation count",
            ):
                materialize_generation(self.replica_sync, now=NOW)

    def test_oversized_apply_journal_is_rejected_before_replica_changes(self):
        self._write("a.md", "one\n")
        first = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        self._write("a.md", "two\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        journal = self.replica_state / "replica" / "apply-journal.json"

        with patch.object(
            beacon_sync_snapshot,
            "MAX_APPLY_JOURNAL_BYTES",
            128,
            create=True,
        ):
            with self.assertRaisesRegex(MaterializeError, "journal.*size"):
                materialize_generation(self.replica_sync, now=NOW)

        self.assertEqual((self.replica / "a.md").read_text(), "one\n")
        self.assertEqual(self._active()["generation"], first["generation"])
        self.assertFalse(journal.exists())
        rollback = self.replica_state / "replica" / "rollback"
        self.assertEqual(
            list(rollback.rglob("*")) if rollback.is_dir() else [],
            [],
        )

    def test_materializer_preflights_over_4mib_operations_before_staging(self):
        files = self._operation_heavy_files()
        self.assertGreater(
            len(self._bootstrap_apply_journal_bytes(files)),
            beacon_sync_snapshot.MAX_APPLY_JOURNAL_BYTES,
        )
        self._write_received_initial_generation(files)
        stage_calls = []
        backup_calls = []
        real_stage = beacon_sync_snapshot._stage_generation
        real_build_backups = beacon_sync_snapshot._build_apply_operations

        def record_stage(*args, **kwargs):
            stage_calls.append(True)
            return real_stage(*args, **kwargs)

        def record_backup_build(*args, **kwargs):
            backup_calls.append(True)
            return real_build_backups(*args, **kwargs)

        with patch.object(
            beacon_sync_snapshot,
            "_stage_generation",
            side_effect=record_stage,
        ), patch.object(
            beacon_sync_snapshot,
            "_build_apply_operations",
            side_effect=record_backup_build,
        ):
            with self.assertRaisesRegex(
                MaterializeError,
                "apply journal exceeds",
            ):
                materialize_generation(
                    self.replica_sync,
                    now=NOW,
                    bootstrap=True,
                )

        self.assertEqual(stage_calls, [])
        self.assertEqual(backup_calls, [])
        self.assertEqual(list(self.replica.rglob("*")), [])

    def test_interrupted_rollback_preserves_concurrent_file_at_unapplied_path(self):
        class SimulatedCrash(BaseException):
            pass

        self._write("a.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        self._write("a.md", "two\n")
        self._write("b.md", "authority\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        destination_a = self.replica / "a.md"
        destination_b = self.replica / "b.md"
        real_atomic_write = beacon_sync_snapshot.portable_atomic_write

        def crash_after_first_replica_write(path, data, *, root, mode=0o600):
            result = real_atomic_write(path, data, root=root, mode=mode)
            if Path(path) == destination_a:
                raise SimulatedCrash("injected process crash")
            return result

        with patch.object(
            beacon_sync_snapshot,
            "portable_atomic_write",
            crash_after_first_replica_write,
        ):
            with self.assertRaisesRegex(SimulatedCrash, "injected process crash"):
                materialize_generation(self.replica_sync, now=NOW)

        destination_b.write_text("concurrent local file\n", encoding="utf-8")

        with self.assertRaisesRegex(MaterializeError, "drift"):
            materialize_generation(self.replica_sync, now=NOW)

        self.assertEqual(
            destination_b.read_text(encoding="utf-8"),
            "concurrent local file\n",
        )
        self.assertTrue(
            (self.replica_state / "replica" / "apply-journal.json").is_file()
        )

    def test_active_marker_post_commit_error_does_not_roll_back_committed_files(self):
        self._write("a.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        self._write("a.md", "two\n")
        second = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        active_path = (
            self.replica_state / "replica" / "active-generation.json"
        )
        real_atomic_write = beacon_sync_snapshot.portable_atomic_write
        injected = False

        def fail_after_active_commit(path, data, *, root, mode=0o600):
            nonlocal injected
            result = real_atomic_write(path, data, root=root, mode=mode)
            if Path(path) == active_path and not injected:
                value = json.loads(bytes(data).decode("utf-8"))
                if value.get("generation") == second["generation"]:
                    injected = True
                    raise MaterializeError("injected active marker fsync failure")
            return result

        with patch.object(
            beacon_sync_snapshot,
            "portable_atomic_write",
            fail_after_active_commit,
        ):
            with self.assertRaisesRegex(MaterializeError, "injected"):
                materialize_generation(self.replica_sync, now=NOW)

        self.assertEqual((self.replica / "a.md").read_text(), "two\n")
        self.assertEqual(self._active()["generation"], second["generation"])
        self.assertTrue(
            (self.replica_state / "replica" / "apply-journal.json").is_file()
        )

        recovered = materialize_generation(self.replica_sync, now=NOW)

        self.assertFalse(recovered["changed"])
        self.assertEqual((self.replica / "a.md").read_text(), "two\n")
        self.assertFalse(
            (self.replica_state / "replica" / "apply-journal.json").exists()
        )

    def test_post_commit_replica_drift_blocks_success_and_gc_progress(self):
        self._write("a.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        self._write("a.md", "two\n")
        second = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        active_path = (
            self.replica_state / "replica" / "active-generation.json"
        )
        destination = self.replica / "a.md"
        real_atomic_write = beacon_sync_snapshot.portable_atomic_write
        injected = False

        def drift_after_active_commit(path, data, *, root, mode=0o600):
            nonlocal injected
            result = real_atomic_write(path, data, root=root, mode=mode)
            if Path(path) == active_path and not injected:
                value = json.loads(bytes(data).decode("utf-8"))
                if value.get("generation") == second["generation"]:
                    injected = True
                    destination.chmod(0o600)
                    destination.write_text("concurrent drift\n", encoding="utf-8")
            return result

        with patch.object(
            beacon_sync_snapshot,
            "portable_atomic_write",
            drift_after_active_commit,
        ):
            with self.assertRaisesRegex(MaterializeError, "committed replica drift"):
                materialize_generation(self.replica_sync, now=NOW)

        self.assertEqual(self._active()["generation"], second["generation"])
        self.assertEqual(destination.read_text(), "concurrent drift\n")
        journal = self.replica_state / "replica" / "apply-journal.json"
        self.assertTrue(journal.is_file())
        with self.assertRaisesRegex(MaterializeError, "committed replica drift"):
            materialize_generation(self.replica_sync, now=NOW)
        self.assertTrue(journal.is_file())

    def test_committed_recovery_rejects_resurrected_deleted_file(self):
        class SimulatedCrash(BaseException):
            pass

        self._write("keep.md", "one\n")
        self._write("remove.md", "delete me\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        (self.vault / "remove.md").unlink()
        self._write("keep.md", "two\n")
        second = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        active_path = self.replica_state / "replica" / "active-generation.json"
        real_atomic_write = beacon_sync_snapshot.portable_atomic_write

        def crash_after_active_commit(path, data, *, root, mode=0o600):
            result = real_atomic_write(path, data, root=root, mode=mode)
            if Path(path) == active_path:
                value = json.loads(bytes(data).decode("utf-8"))
                if value.get("generation") == second["generation"]:
                    raise SimulatedCrash("injected process crash")
            return result

        with patch.object(
            beacon_sync_snapshot,
            "portable_atomic_write",
            crash_after_active_commit,
        ):
            with self.assertRaisesRegex(SimulatedCrash, "injected"):
                materialize_generation(self.replica_sync, now=NOW)

        resurrected = self.replica / "remove.md"
        resurrected.write_text("resurrected after crash\n", encoding="utf-8")
        journal = self.replica_state / "replica" / "apply-journal.json"

        with self.assertRaisesRegex(MaterializeError, "drift"):
            materialize_generation(self.replica_sync, now=NOW)

        self.assertEqual(
            resurrected.read_text(encoding="utf-8"),
            "resurrected after crash\n",
        )
        self.assertTrue(journal.is_file())

    def test_replica_delete_never_chmods_a_swapped_symlink_target(self):
        self.replica.mkdir()
        victim = self.replica / "victim.md"
        victim.write_bytes(b"managed")
        victim.chmod(0o444)
        outside = self.root / "outside.md"
        outside.write_bytes(b"outside")
        outside.chmod(0o640)
        outside_mode = stat.S_IMODE(outside.stat().st_mode)
        original_chmod = os.chmod
        swapped = False

        def swap_before_chmod(path, mode):
            nonlocal swapped
            if Path(path) == victim and not swapped:
                swapped = True
                victim.unlink()
                victim.symlink_to(outside)
            return original_chmod(path, mode)

        with patch.object(
            beacon_sync_snapshot.os,
            "chmod",
            side_effect=swap_before_chmod,
        ):
            beacon_sync_snapshot._unlink_regular(
                victim,
                self.replica,
            )

        self.assertTrue(outside.is_file())
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), outside_mode)
        self.assertFalse(os.path.lexists(victim))

    def test_materializer_requires_explicit_bootstrap_and_rejects_stale_replica(self):
        self._write("old.md", "old\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()

        with self.assertRaisesRegex(MaterializeError, "bootstrap"):
            materialize_generation(self.replica_sync, now=NOW)

        materialize_generation(
            self.replica_sync,
            now=NOW,
            bootstrap=True,
        )
        (self.vault / "old.md").unlink()
        self._write("new.md", "new\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        shutil.rmtree(self.replica_state / "replica")

        with self.assertRaisesRegex(MaterializeError, "bootstrap"):
            materialize_generation(self.replica_sync, now=NOW)
        with self.assertRaisesRegex(MaterializeError, "not empty"):
            materialize_generation(
                self.replica_sync,
                now=NOW,
                bootstrap=True,
            )

        self.assertEqual((self.replica / "old.md").read_text(), "old\n")
        self.assertFalse((self.replica / "new.md").exists())

    def test_rollback_uses_recorded_backup_size_not_default_object_limit(self):
        replica_state = self.replica_state / "replica"
        backup = replica_state / "rollback" / "generation-test" / "a.md"
        backup.parent.mkdir(parents=True)
        original = b"original-content"
        backup.write_bytes(original)
        self.replica.mkdir()
        destination = self.replica / "a.md"
        destination.write_bytes(b"changed")
        journal = {
            "operations": [
                {
                    "path": "a.md",
                    "action": "write",
                    "target_bytes": len(b"changed"),
                    "target_sha256": sha256_bytes(b"changed"),
                    "before_identity": beacon_sync_snapshot._stat_identity(
                        destination.stat()
                    ),
                    "existed": True,
                    "backup": "rollback/generation-test/a.md",
                    "mode": 0o444,
                    "bytes": len(original),
                    "sha256": sha256_bytes(original),
                }
            ]
        }

        with patch.object(
            beacon_sync_snapshot,
            "DEFAULT_MAX_OBJECT_BYTES",
            8,
        ):
            beacon_sync_snapshot._rollback_journal(
                self.replica,
                replica_state,
                journal,
            )

        self.assertEqual(destination.read_bytes(), original)

    def test_equal_size_tampered_rollback_backup_fails_before_replica_overwrite(self):
        class SimulatedCrash(BaseException):
            pass

        self._write("a.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        materialize_generation(self.replica_sync, now=NOW, bootstrap=True)
        self._write("a.md", "two\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._sync_published()
        destination = self.replica / "a.md"
        real_atomic_write = beacon_sync_snapshot.portable_atomic_write

        def crash_after_replica_write(path, data, *, root, mode=0o600):
            result = real_atomic_write(path, data, root=root, mode=mode)
            if Path(path) == destination:
                raise SimulatedCrash("injected process crash")
            return result

        with patch.object(
            beacon_sync_snapshot,
            "portable_atomic_write",
            crash_after_replica_write,
        ):
            with self.assertRaisesRegex(SimulatedCrash, "injected process crash"):
                materialize_generation(self.replica_sync, now=NOW)

        journal_path = (
            self.replica_state / "replica" / "apply-journal.json"
        )
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        operation = next(
            item for item in journal["operations"] if item["path"] == "a.md"
        )
        self.assertEqual(operation["sha256"], sha256_bytes(b"one\n"))
        backup = self.replica_state / "replica" / operation["backup"]
        backup.write_bytes(b"evil")
        before_recovery = destination.read_bytes()

        with self.assertRaisesRegex(MaterializeError, "hash"):
            materialize_generation(self.replica_sync, now=NOW)

        self.assertEqual(destination.read_bytes(), before_recovery)
        self.assertTrue(journal_path.is_file())
        self.assertTrue(
            (
                self.replica_state
                / "replica"
                / "active"
                / f"{1:020d}.json"
            ).is_file()
        )

    def test_cleanup_removes_only_unpublished_and_unreferenced_storage(self):
        self._write("note.md", "one\n")
        first = publish_generation(self.cfg, self.authority_sync, now=NOW)
        self._write("note.md", "two\n")
        second = publish_generation(self.cfg, self.authority_sync, now=NOW)
        first_snapshot = self._published_snapshot(first["generation"])
        second_snapshot = self._published_snapshot(second["generation"])
        referenced = {
            item["sha256"]
            for snapshot in (first_snapshot, second_snapshot)
            for item in snapshot["files"]
        }
        incomplete = (
            self.published / "v1" / "snapshots" / f"{3:020d}"
        )
        incomplete.mkdir(parents=True)
        (incomplete / "snapshot.json").write_bytes(b"partial")
        stray_digest = sha256_bytes(b"unreferenced")
        stray = (
            self.published
            / "v1"
            / "objects"
            / stray_digest[:2]
            / stray_digest
        )
        stray.parent.mkdir(parents=True)
        stray.write_bytes(b"unreferenced")

        result = publish_generation(self.cfg, self.authority_sync, now=NOW)

        self.assertFalse(result["changed"])
        self.assertFalse(incomplete.exists())
        self.assertFalse(stray.exists())
        self.assertEqual(result["cleanup"]["removed_generations"], [3])
        self.assertEqual(result["cleanup"]["removed_objects"], [stray_digest])
        self.assertEqual(result["cleanup"]["retained_generations"], [1, 2])
        self.assertTrue(
            any(
                "acknowledgement authority" in item
                for item in result["cleanup"]["deferred"]
            )
        )
        for digest in referenced:
            self.assertTrue(
                (
                    self.published
                    / "v1"
                    / "objects"
                    / digest[:2]
                    / digest
                ).is_file()
            )
        self.assertTrue((self.published / "v1" / "current.json").is_file())

    def test_cleanup_defers_receipt_referenced_unknown_generation_and_objects(self):
        self._write("note.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        unknown_generation = 99
        unknown = (
            self.published
            / "v1"
            / "snapshots"
            / f"{unknown_generation:020d}"
        )
        unknown.mkdir(parents=True)
        (unknown / "snapshot.json").write_bytes(b"partial")
        possible_digest = sha256_bytes(b"possibly referenced")
        possible_object = (
            self.published
            / "v1"
            / "objects"
            / possible_digest[:2]
            / possible_digest
        )
        possible_object.parent.mkdir(parents=True)
        possible_object.write_bytes(b"possibly referenced")
        self._write_receipt_reference(unknown_generation)

        result = publish_generation(self.cfg, self.authority_sync, now=NOW)

        self.assertTrue(unknown.is_dir())
        self.assertTrue(possible_object.is_file())
        self.assertIn(
            unknown_generation,
            result["cleanup"]["retained_generations"],
        )
        self.assertTrue(
            any("receipt" in item for item in result["cleanup"]["deferred"])
        )

    def test_cleanup_preserves_objects_when_receipt_manifest_is_missing(self):
        self._write("note.md", "one\n")
        publish_generation(self.cfg, self.authority_sync, now=NOW)
        missing_generation = 99
        possible_digest = sha256_bytes(b"possibly referenced")
        possible_object = (
            self.published
            / "v1"
            / "objects"
            / possible_digest[:2]
            / possible_digest
        )
        possible_object.parent.mkdir(parents=True)
        possible_object.write_bytes(b"possibly referenced")
        self._write_receipt_reference(missing_generation)

        result = publish_generation(self.cfg, self.authority_sync, now=NOW)

        self.assertTrue(possible_object.is_file())
        self.assertTrue(
            any(
                f"receipt generation {missing_generation}" in item
                for item in result["cleanup"]["deferred"]
            )
        )

    def test_receipt_reference_scan_is_bounded_and_continues_across_runs(self):
        self._write("note.md", "one\n")
        generation = publish_generation(self.cfg, self.authority_sync, now=NOW)
        receipt_root = self.published / "v1" / "receipts" / "producer-test"
        receipt_root.mkdir(parents=True)
        for seq in range(1, 4):
            receipt = {
                "protocol": "agent-memory-beacon-sync-receipt",
                "schema_version": 1,
                "producer_instance_id": "producer-test",
                "seq": seq,
                "event_id": "event-" + f"{seq:064x}",
                "event_sha256": f"{seq + 10:064x}",
                "status": "applied",
                "code": "applied",
                "canonical_generation": generation["generation"],
                "generation_id": generation["generation_id"],
                "gc_allowed": True,
                "processed_at": "2026-07-31T12:00:00Z",
            }
            (receipt_root / f"{seq:020d}.json").write_bytes(
                canonical_json_bytes(receipt)
            )
        real_read = beacon_sync_snapshot.read_bounded_regular_file
        reads = []

        def track_receipt_reads(path, **kwargs):
            candidate = Path(path)
            if receipt_root in candidate.parents:
                reads.append(candidate.name)
            return real_read(path, **kwargs)

        results = []
        per_run_reads = []
        with (
            patch.object(
                beacon_sync_snapshot,
                "MAX_RECEIPT_REFERENCES_PER_PUBLISH_RUN",
                1,
            ),
            patch.object(
                beacon_sync_snapshot,
                "read_bounded_regular_file",
                side_effect=track_receipt_reads,
            ),
        ):
            for _ in range(3):
                before = len(reads)
                results.append(
                    publish_generation(self.cfg, self.authority_sync, now=NOW)
                )
                per_run_reads.append(len(reads) - before)

        self.assertEqual(per_run_reads, [1, 1, 1])
        self.assertEqual([result["limited"] for result in results], [True, True, False])
        self.assertEqual(
            [result["pending_receipt_references"] for result in results],
            [1, 1, 0],
        )
        self.assertEqual(len(set(reads)), 3)

    def test_pending_reducer_event_receipt_binds_sealed_generation(self):
        sessions = self.root / "sessions"
        sessions.mkdir()
        outbox = self.root / "outbox"
        producer_state = self.root / "producer"
        transcript = sessions / "session.jsonl"
        transcript.write_text(
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
        producer_cfg = {
            "device_id": "windows-test",
            "state_dir": str(producer_state),
            "outbox_dir": str(outbox),
            "transcript_paths": [str(sessions)],
            "max_chunk_bytes": 4096,
            "max_gap_bytes": 4096,
            "max_events_per_run": 32,
        }
        collect_transcripts(producer_cfg, include_existing=True, now=NOW)
        (self.vault / "04-Feedback" / "_logs").mkdir(parents=True)
        authority = {
            **self.authority_sync,
            "inboxes": [{"device_id": "windows-test", "path": str(outbox)}],
        }
        reduce_inboxes(
            self.cfg,
            authority,
            harvest_adapter=lambda _cfg, paths: {str(path): True for path in paths},
            now=NOW,
        )
        self._write("canonical.md", "sealed\n")
        generation = publish_generation(self.cfg, authority, now=NOW)

        receipt_result = publish_pending_receipts(
            authority,
            generation,
            now=NOW,
        )
        rows = list_ledger_events(authority)
        receipts = list((self.published / "v1" / "receipts").rglob("*.json"))
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))

        self.assertEqual(receipt_result["published"], 1)
        self.assertEqual(receipt["canonical_generation"], generation["generation"])
        self.assertEqual(receipt["generation_id"], generation["generation_id"])
        self.assertTrue(receipt["gc_allowed"])
        self.assertEqual(rows[0]["status"], "applied")

    def test_finalized_receipt_is_recreated_when_file_is_missing(self):
        sessions = self.root / "sessions"
        sessions.mkdir()
        transcript = sessions / "session.jsonl"
        transcript.write_text(
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
        outbox = self.root / "outbox"
        producer_cfg = {
            "device_id": "windows-test",
            "state_dir": str(self.root / "producer"),
            "outbox_dir": str(outbox),
            "transcript_paths": [str(sessions)],
            "max_chunk_bytes": 4096,
            "max_gap_bytes": 4096,
            "max_events_per_run": 32,
        }
        collect_transcripts(producer_cfg, include_existing=True, now=NOW)
        (self.vault / "04-Feedback" / "_logs").mkdir(parents=True)
        authority = {
            **self.authority_sync,
            "inboxes": [{"device_id": "windows-test", "path": str(outbox)}],
        }
        reduce_inboxes(
            self.cfg,
            authority,
            harvest_adapter=lambda _cfg, paths: {
                str(path): True for path in paths
            },
            now=NOW,
        )
        self._write("canonical.md", "sealed\n")
        generation = publish_generation(self.cfg, authority, now=NOW)
        publish_pending_receipts(authority, generation, now=NOW)
        receipt = next((self.published / "v1" / "receipts").rglob("*.json"))
        expected = receipt.read_bytes()
        receipt.unlink()

        repaired = publish_pending_receipts(authority, generation, now=NOW)

        self.assertEqual(repaired["published"], 1)
        self.assertEqual(receipt.read_bytes(), expected)
        self.assertEqual(list_ledger_events(authority)[0]["status"], "applied")

    def _operation_heavy_files(self, count=5000):
        digest = sha256_bytes(b"x")
        component = "x" * 240
        return [
            {
                "path": (
                    f"notes/{component}/{component}/{component}/"
                    f"{index:05d}.md"
                ),
                "bytes": 1,
                "sha256": digest,
                "content_class": "canonical-memory",
            }
            for index in range(count)
        ]

    def _bootstrap_apply_journal_bytes(self, files):
        operations = [
            {
                "path": item["path"],
                "action": "write",
                "target_bytes": item["bytes"],
                "target_sha256": item["sha256"],
                "before_identity": [],
                "existed": False,
                "backup": "",
                "mode": 0,
                "bytes": 0,
                "sha256": "",
            }
            for item in files
        ]
        return canonical_json_bytes(
            {
                "protocol": beacon_sync_snapshot.JOURNAL_PROTOCOL,
                "schema_version": 1,
                "target_generation": 1,
                "target_generation_id": "generation-" + ("a" * 64),
                "target_snapshot_sha256": "b" * 64,
                "operations": operations,
            }
        )

    def _write_received_initial_generation(self, files):
        identity = {
            "parent_generation_id": "",
            "files": files,
            "deleted_paths": [],
        }
        generation_id = "generation-" + sha256_bytes(
            canonical_json_bytes(identity)
        )
        snapshot = {
            "protocol": beacon_sync_snapshot.PROTOCOL_SNAPSHOT,
            "schema_version": 1,
            "generation": 1,
            "generation_id": generation_id,
            "parent_generation": 0,
            "parent_generation_id": "",
            "files": files,
            "tombstones": [],
        }
        snapshot_bytes = canonical_json_bytes(snapshot)
        snapshot_sha256 = sha256_bytes(snapshot_bytes)
        generation_dir = self.received / "v1" / "snapshots" / f"{1:020d}"
        generation_dir.mkdir(parents=True)
        (generation_dir / "snapshot.json").write_bytes(snapshot_bytes)
        (generation_dir / "complete.json").write_bytes(
            canonical_json_bytes(
                {
                    "protocol": beacon_sync_snapshot.PROTOCOL_COMPLETE,
                    "schema_version": 1,
                    "generation": 1,
                    "generation_id": generation_id,
                    "snapshot_sha256": snapshot_sha256,
                    "file_count": len(files),
                    "object_bytes": sum(item["bytes"] for item in files),
                }
            )
        )
        current = {
            "protocol": beacon_sync_snapshot.PROTOCOL_CURRENT,
            "schema_version": 1,
            "generation": 1,
            "generation_id": generation_id,
            "snapshot_sha256": snapshot_sha256,
        }
        current_path = self.received / "v1" / "current.json"
        current_path.parent.mkdir(parents=True, exist_ok=True)
        current_path.write_bytes(canonical_json_bytes(current))
        digest = files[0]["sha256"]
        object_path = self.received / "v1" / "objects" / digest[:2] / digest
        object_path.parent.mkdir(parents=True)
        object_path.write_bytes(b"x")
        return snapshot

    def _write(self, relative, content):
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            os.chmod(path, 0o600)
        path.write_text(content, encoding="utf-8")
        return path

    def _published_snapshot(self, generation):
        return json.loads(
            (
                self.published
                / "v1"
                / "snapshots"
                / f"{generation:020d}"
                / "snapshot.json"
            ).read_text(encoding="utf-8")
        )

    def _received_snapshot(self, generation):
        return json.loads(
            (
                self.received
                / "v1"
                / "snapshots"
                / f"{generation:020d}"
                / "snapshot.json"
            ).read_text(encoding="utf-8")
        )

    def _sync_published(self):
        if self.received.exists():
            shutil.rmtree(self.received)
        shutil.copytree(self.published, self.received)

    def _rewrite_received_snapshot(self, snapshot):
        generation = int(snapshot["generation"])
        identity = {
            "parent_generation_id": snapshot["parent_generation_id"],
            "files": snapshot["files"],
            "deleted_paths": [
                item["path"] for item in snapshot["tombstones"]
            ],
        }
        snapshot["generation_id"] = "generation-" + sha256_bytes(
            canonical_json_bytes(identity)
        )
        snapshot_path = (
            self.received
            / "v1"
            / "snapshots"
            / f"{generation:020d}"
            / "snapshot.json"
        )
        complete_path = snapshot_path.parent / "complete.json"
        current_path = self.received / "v1" / "current.json"
        snapshot_bytes = canonical_json_bytes(snapshot)
        snapshot_path.write_bytes(snapshot_bytes)
        snapshot_hash = sha256_bytes(snapshot_bytes)
        complete = json.loads(complete_path.read_text(encoding="utf-8"))
        complete["generation_id"] = snapshot["generation_id"]
        complete["snapshot_sha256"] = snapshot_hash
        complete["file_count"] = len(snapshot["files"])
        complete["object_bytes"] = sum(
            int(item["bytes"]) for item in snapshot["files"]
        )
        complete_path.write_bytes(canonical_json_bytes(complete))
        current = json.loads(current_path.read_text(encoding="utf-8"))
        current["generation_id"] = snapshot["generation_id"]
        current["snapshot_sha256"] = snapshot_hash
        current_path.write_bytes(canonical_json_bytes(current))

    def _active(self):
        return json.loads(
            (
                self.replica_state
                / "replica"
                / "active-generation.json"
            ).read_text(encoding="utf-8")
        )

    def _make_replica_writable(self):
        for path in self.replica.rglob("*"):
            if path.is_file():
                os.chmod(path, 0o600)

    def _write_receipt_reference(self, generation):
        receipt = {
            "protocol": "agent-memory-beacon-sync-receipt",
            "schema_version": 1,
            "producer_instance_id": "producer-test",
            "seq": 1,
            "event_id": "event-" + ("a" * 64),
            "event_sha256": "b" * 64,
            "status": "applied",
            "code": "applied",
            "canonical_generation": generation,
            "generation_id": "generation-" + ("c" * 64),
            "gc_allowed": True,
            "processed_at": "2026-07-31T12:00:00Z",
        }
        receipt_path = (
            self.published
            / "v1"
            / "receipts"
            / "producer-test"
            / "receipt.json"
        )
        receipt_path.parent.mkdir(parents=True)
        receipt_path.write_bytes(canonical_json_bytes(receipt))
        return receipt_path

    @contextmanager
    def _crash_before_current(self):
        current = self.published / "v1" / "current.json"
        real_atomic_write = beacon_sync_snapshot.portable_atomic_write

        def injected(path, data, *, root, mode=0o600):
            if Path(path) == current:
                raise OSError("injected current crash")
            return real_atomic_write(path, data, root=root, mode=mode)

        with patch.object(
            beacon_sync_snapshot,
            "portable_atomic_write",
            injected,
        ):
            yield

    @contextmanager
    def _crash_before_complete(self):
        real_write_immutable = beacon_sync_snapshot.write_immutable

        def injected(path, data, *, root, mode=0o600):
            if Path(path).name == "complete.json":
                raise OSError("injected complete crash")
            return real_write_immutable(path, data, root=root, mode=mode)

        with patch.object(
            beacon_sync_snapshot,
            "write_immutable",
            injected,
        ):
            yield


if __name__ == "__main__":
    unittest.main()
