import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from beacon_sync_protocol import (
    PROTOCOL_COMPLETE,
    PROTOCOL_CURRENT,
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
)
from beacon_sync_snapshot import materialize_generation
import install_beacon_sync
import install_runtime


@unittest.skipUnless(
    os.name == "nt" and os.environ.get("AGENT_MEMORY_BEACON_VERIFY_RELEASE") == "1",
    "explicit Windows runtime release integration test",
)
class WindowsRuntimeReleaseIntegrationTests(unittest.TestCase):
    def test_moved_release_executes_from_manifest_owned_dependency_closure(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root = root / "runtime"
            plan = install_runtime.build_windows_sync_release_plan(
                REPO_ROOT,
                runtime_root,
                {
                    "python_path": sys.executable,
                    "codex_sessions_path": str(root / "sessions"),
                    "beacon_sync": {
                        "enabled": True,
                        "role": "producer-replica",
                        "device_id": "windows-native-release",
                        "state_dir": str(root / "sync-state"),
                        "outbox_dir": str(root / "outbox"),
                        "received_published_dir": str(root / "received"),
                        "replica_path": str(root / "replica"),
                    },
                },
            )
            with mock.patch.dict(os.environ):
                os.environ.pop("AGENT_MEMORY_BEACON_VERIFY_RELEASE", None)
                staged = install_runtime.stage_runtime(plan)
            final_plan = staged.final_plan
            self.assertIsNotNone(final_plan)
            install_runtime._durable_replace(staged.root, final_plan.install_root)

            verification = install_runtime.verify_installed_release(
                final_plan.install_root
            )

            self.assertEqual(verification["release_id"], final_plan.release_id)
            pollution = root / "pollution"
            pollution.mkdir()
            marker = pollution / "executed"
            (pollution / "yaml.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('fake')\n",
                encoding="utf-8",
            )
            environment = dict(os.environ)
            environment.update(
                {
                    "PYTHONHOME": str(pollution),
                    "PYTHONPATH": str(pollution),
                    "PYTHONSTARTUP": str(pollution / "yaml.py"),
                    "PYTHONUSERBASE": str(pollution / "userbase"),
                }
            )
            launcher = final_plan.install_root / ".venv" / "Scripts" / "python.exe"
            probe = subprocess.run(
                (
                    str(launcher),
                    "-E",
                    "-s",
                    "-X",
                    "utf8",
                    "-B",
                    "-c",
                    (
                        "import json,requests,sys,yaml; "
                        "print(json.dumps({'sys_executable':sys.executable,"
                        "'base_prefix':sys.base_prefix,'prefix':sys.prefix,"
                        "'requests':requests.__file__,'yaml':yaml.__file__},sort_keys=True))"
                    ),
                ),
                cwd=final_plan.install_root / "scripts",
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)
            payload = json.loads(probe.stdout)
            self.assertEqual(
                os.path.normcase(os.path.abspath(payload["sys_executable"])),
                os.path.normcase(os.path.abspath(launcher)),
            )
            self.assertNotEqual(
                os.path.normcase(payload["base_prefix"]),
                os.path.normcase(payload["prefix"]),
            )
            site_packages = final_plan.install_root / ".venv" / "Lib" / "site-packages"
            for dependency in (payload["requests"], payload["yaml"]):
                self.assertEqual(
                    os.path.commonpath(
                        [os.path.abspath(dependency), os.path.abspath(site_packages)]
                    ),
                    os.path.abspath(site_packages),
                )
            self.assertFalse(marker.exists())


@unittest.skipUnless(
    os.name == "nt" and os.environ.get("AGENT_MEMORY_BEACON_VERIFY_TASK") == "1",
    "explicit Windows Task integration test",
)
class WindowsTaskIntegrationTests(unittest.TestCase):
    def test_unique_task_create_query_run_restore_and_delete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "task-marker.txt"
            old_script = root / "old task.py"
            new_script = root / "new task.py"
            old_script.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('old', encoding='utf-8')\n",
                encoding="utf-8",
            )
            new_script.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('new', encoding='utf-8')\n",
                encoding="utf-8",
            )
            config = root / "unused config.yaml"
            config.write_text("{}\n", encoding="utf-8")
            task_name = (
                "Agent Memory Beacon CI "
                f"{os.getpid()}-{secrets.token_hex(6)}"
            )
            user_id = install_beacon_sync._current_windows_user()
            deleted = False
            try:
                install_beacon_sync.install_windows_scheduler(
                    python_path=sys.executable,
                    script_path=old_script,
                    config_path=config,
                    user_id=user_id,
                    task_name=task_name,
                )
                old_xml = install_beacon_sync._query_windows_task(None, task_name)
                expected_old = install_beacon_sync.build_windows_task_xml(
                    python_path=sys.executable,
                    script_path=old_script,
                    config_path=config,
                    user_id=user_id,
                    task_name=task_name,
                )
                self.assertTrue(
                    install_beacon_sync._same_windows_task_xml(old_xml, expected_old)
                )

                install_beacon_sync.install_windows_scheduler(
                    python_path=sys.executable,
                    script_path=new_script,
                    config_path=config,
                    user_id=user_id,
                    task_name=task_name,
                )
                expected_new = install_beacon_sync.build_windows_task_xml(
                    python_path=sys.executable,
                    script_path=new_script,
                    config_path=config,
                    user_id=user_id,
                    task_name=task_name,
                )
                self.assertTrue(
                    install_beacon_sync._same_windows_task_xml(
                        install_beacon_sync._query_windows_task(None, task_name),
                        expected_new,
                    )
                )
                install_beacon_sync._restore_windows_task(
                    None,
                    task_name,
                    old_xml,
                    expected_current=expected_new,
                )
                self.assertTrue(
                    install_beacon_sync._same_windows_task_xml(
                        install_beacon_sync._query_windows_task(None, task_name),
                        old_xml,
                    )
                )

                run = subprocess.run(
                    ("schtasks.exe", "/Run", "/TN", task_name),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=30,
                )
                self.assertEqual(run.returncode, 0, run.stderr or run.stdout)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and not marker.exists():
                    time.sleep(0.25)
                self.assertTrue(marker.exists(), "scheduled Task did not write marker")
                self.assertEqual(marker.read_text(encoding="utf-8"), "old")

                install_beacon_sync.install_windows_scheduler(
                    python_path=sys.executable,
                    script_path=old_script,
                    config_path=config,
                    user_id=user_id,
                    task_name=task_name,
                    uninstall=True,
                )
                self.assertIsNone(
                    install_beacon_sync._query_windows_task(None, task_name)
                )
                deleted = True
            finally:
                if not deleted:
                    subprocess.run(
                        ("schtasks.exe", "/Delete", "/TN", task_name, "/F"),
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=30,
                    )


@unittest.skipUnless(os.name == "nt", "Windows-only synchronization smoke tests")
class BeaconSyncWindowsTests(unittest.TestCase):
    def test_current_task_user_is_resolved_to_sid(self):
        self.assertRegex(
            install_beacon_sync._current_windows_user(),
            r"^S-[0-9]+(?:-[0-9]+)+$",
        )

    def test_current_task_user_ignores_account_environment(self):
        expected = install_beacon_sync._current_windows_user()
        with mock.patch.dict(
            os.environ,
            {
                "LOGNAME": "wrong-logname",
                "USER": "wrong-user",
                "USERNAME": "wrong-username",
                "USERDOMAIN": "WRONG-DOMAIN",
                "COMPUTERNAME": "WRONG-COMPUTER",
            },
            clear=True,
        ):
            actual = install_beacon_sync._current_windows_user()

        self.assertEqual(actual, expected)

    def test_release_plan_matches_native_cpython_venv_launcher(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime_root = root / "runtime"
            plan = install_runtime.build_windows_sync_release_plan(
                REPO_ROOT,
                runtime_root,
                {
                    "python_path": sys.executable,
                    "codex_sessions_path": str(root / "sessions"),
                    "beacon_sync": {
                        "enabled": True,
                        "role": "producer-replica",
                        "device_id": "windows-native-test",
                    },
                },
            )
            stage = root / "native-stage"
            for item in plan.files:
                path = stage / item.relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(item.content)
            (stage / "release-manifest.json").write_bytes(plan.manifest_bytes)
            subprocess.run(
                (
                    sys.executable,
                    "-I",
                    "-B",
                    "-m",
                    "venv",
                    "--copies",
                    "--without-pip",
                    str(stage / ".venv"),
                ),
                check=True,
                cwd=stage,
                timeout=120,
            )

            launcher = stage / ".venv" / "Scripts" / "python.exe"
            self.assertNotEqual(launcher.read_bytes(), Path(sys.executable).read_bytes())
            manifest = json.loads(plan.manifest_bytes)
            self.assertIn("runtime_environment", manifest)
            self.assertNotIn("agent-memory-beacon-venv-probe-", plan.manifest_bytes.decode())
            self.assertTrue(
                manifest["runtime_environment"]["base_python"]["dlls"]
            )
            install_runtime._verify_staged_files(plan, stage)

    def test_runtime_publish_uses_native_durable_replace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "staged"
            destination = root / "published"
            source.mkdir()
            (source / "release.txt").write_bytes(b"release")

            install_runtime._durable_replace(source, destination)

            self.assertFalse(source.exists())
            self.assertEqual(
                (destination / "release.txt").read_bytes(),
                b"release",
            )

    def test_handle_pinned_filesystem_primitives(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "state" / "a"
            portable_atomic_write(target, b"one\n", root=root, mode=0o444)
            portable_atomic_write(target, b"two\n", root=root, mode=0o444)

            self.assertEqual(
                read_bounded_regular_file(target, max_bytes=16, root=root),
                b"two\n",
            )
            self.assertTrue(
                os.stat(target).st_file_attributes
                & stat.FILE_ATTRIBUTE_READONLY
            )

            lock = root / "locks" / "worker.lock"
            with portable_file_lock(lock, root=root):
                self.assertTrue(lock.is_file())

            empty = root / "trees" / "empty"
            portable_ensure_directory_tree(empty, root=root)
            self.assertTrue(portable_rmdir_empty(empty, root=root))

            blocked = root / "trees" / "blocked"
            portable_ensure_directory_tree(blocked, root=root)
            (blocked / "local.txt").write_bytes(b"local")
            os.chmod(blocked, stat.S_IREAD)
            original_attributes = os.stat(blocked).st_file_attributes
            with self.assertRaises(ProtocolError):
                portable_rmdir_empty(blocked, root=root)
            self.assertEqual(
                os.stat(blocked).st_file_attributes,
                original_attributes,
            )
            os.chmod(blocked, stat.S_IWRITE)
            (blocked / "local.txt").unlink()
            blocked.rmdir()

            tree = root / "trees" / "managed"
            portable_ensure_directory_tree(tree / "nested", root=root)
            portable_atomic_write(
                tree / "nested" / "value",
                b"managed",
                root=root,
            )
            self.assertTrue(portable_rmtree(tree, root=root))
            self.assertFalse(tree.exists())
            self.assertTrue(
                portable_unlink_regular(target, root=root)
            )
            self.assertFalse(target.exists())

    def test_bounded_reader_rejects_reparse_point_when_available(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            outside = root / "outside"
            outside.write_bytes(b"outside")
            link = root / "link"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("Windows runner cannot create symlinks")

            with self.assertRaises(ProtocolError):
                read_bounded_regular_file(link, max_bytes=16, root=root)

    def test_stable_runtime_rejects_directory_reparse_point_when_available(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "target"
            target.mkdir()
            link = root / "junction"
            try:
                link.symlink_to(target, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("Windows runner cannot create directory symlinks")

            with self.assertRaisesRegex(ValueError, "reparse"):
                install_runtime._assert_no_symlink_chain(link)

    def test_materializer_bootstrap_and_shape_transitions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            received = root / "received"
            state = root / "state"
            replica = root / "replica"
            cfg = {
                "state_dir": str(state),
                "received_published_dir": str(received),
                "replica_path": str(replica),
                "max_replica_object_bytes": 8 * 1024 * 1024,
            }

            first = self._write_generation(
                received,
                generation=1,
                parent=None,
                files={"notes/topic": b"old file\n"},
            )
            materialize_generation(cfg, bootstrap=True)
            self.assertEqual(
                (replica / "notes/topic").read_bytes(),
                b"old file\n",
            )

            second = self._write_generation(
                received,
                generation=2,
                parent=first,
                files={"notes/topic/child.md": b"new child\n"},
            )
            materialize_generation(cfg)
            self.assertEqual(
                (replica / "notes/topic/child.md").read_bytes(),
                b"new child\n",
            )

            third = self._write_generation(
                received,
                generation=3,
                parent=second,
                files={"notes/topic": b"new file\n"},
            )
            result = materialize_generation(cfg)
            self.assertEqual(result["generation"], 3)
            self.assertEqual(
                (replica / "notes/topic").read_bytes(),
                b"new file\n",
            )

    def _write_generation(self, received, *, generation, parent, files):
        objects = received / "v1" / "objects"
        snapshots = received / "v1" / "snapshots"
        file_rows = []
        for relative, data in sorted(files.items()):
            digest = sha256_bytes(data)
            object_path = objects / digest[:2] / digest
            object_path.parent.mkdir(parents=True, exist_ok=True)
            object_path.write_bytes(data)
            file_rows.append(
                {
                    "path": relative,
                    "bytes": len(data),
                    "sha256": digest,
                    "content_class": "canonical-memory",
                }
            )
        parent_paths = {
            item["path"] for item in (parent or {}).get("files", [])
        }
        current_paths = {item["path"] for item in file_rows}
        deleted_paths = sorted(parent_paths - current_paths)
        generation_id = "generation-" + sha256_bytes(
            canonical_json_bytes(
                {
                    "parent_generation_id": (
                        parent["generation_id"] if parent else ""
                    ),
                    "files": file_rows,
                    "deleted_paths": deleted_paths,
                }
            )
        )
        snapshot = {
            "protocol": PROTOCOL_SNAPSHOT,
            "schema_version": 1,
            "generation": generation,
            "generation_id": generation_id,
            "parent_generation": parent["generation"] if parent else 0,
            "parent_generation_id": parent["generation_id"] if parent else "",
            "files": file_rows,
            "tombstones": [
                {
                    "path": relative,
                    "deleted_at_generation": generation,
                }
                for relative in deleted_paths
            ],
        }
        snapshot_bytes = canonical_json_bytes(snapshot)
        complete = {
            "protocol": PROTOCOL_COMPLETE,
            "schema_version": 1,
            "generation": generation,
            "generation_id": generation_id,
            "snapshot_sha256": sha256_bytes(snapshot_bytes),
            "file_count": len(file_rows),
            "object_bytes": sum(item["bytes"] for item in file_rows),
        }
        current = {
            "protocol": PROTOCOL_CURRENT,
            "schema_version": 1,
            "generation": generation,
            "generation_id": generation_id,
            "snapshot_sha256": complete["snapshot_sha256"],
        }
        generation_dir = snapshots / f"{generation:020d}"
        generation_dir.mkdir(parents=True, exist_ok=True)
        (generation_dir / "snapshot.json").write_bytes(snapshot_bytes)
        (generation_dir / "complete.json").write_bytes(
            canonical_json_bytes(complete)
        )
        current_path = received / "v1" / "current.json"
        current_path.parent.mkdir(parents=True, exist_ok=True)
        current_path.write_bytes(canonical_json_bytes(current))
        return snapshot


if __name__ == "__main__":
    unittest.main()
