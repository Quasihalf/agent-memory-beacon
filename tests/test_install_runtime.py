import hashlib
import json
import os
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import install_runtime
from branding import LEGACY_LAUNCHD_LABELS, NEW_LAUNCHD_LABELS
from install_beacon_sync import (
    LAUNCHD_LABEL as SYNC_LAUNCHD_LABEL,
    build_launchd_plist as build_sync_launchd_plist,
)
from safety import durable_atomic_write
from install_runtime import (
    _external_paths,
    _remove_tree,
    _restore_external_files,
    _service_states,
    _validate_rollback_manifest,
    apply_runtime,
    build_release_plan,
    rollback_runtime,
    stage_runtime,
    verify_release,
)


EXPECTED_RUNTIME_FILES = {
    "LICENSE",
    "patches/AGENT_MEMORY_BEACON.md.patch",
    "scripts/__init__.py",
    "scripts/analyzer.py",
    "scripts/annotation_quality.py",
    "scripts/backup.py",
    "scripts/beacon_sync.py",
    "scripts/beacon_sync_producer.py",
    "scripts/beacon_sync_protocol.py",
    "scripts/beacon_sync_reducer.py",
    "scripts/beacon_sync_snapshot.py",
    "scripts/branding.py",
    "scripts/codex_profile_sync.py",
    "scripts/codex_prompt_hook.py",
    "scripts/compiler.py",
    "scripts/config.py",
    "scripts/conversation_summary.py",
    "scripts/config.yaml",
    "scripts/context_install.py",
    "scripts/doctor.py",
    "scripts/error_evidence.py",
    "scripts/evaluate_annotation_quality.py",
    "scripts/evaluate_memory_comparison.py",
    "scripts/experience_memory.py",
    "scripts/graph_projection.py",
    "scripts/install_claude.py",
    "scripts/install_codex.py",
    "scripts/install_beacon_sync.py",
    "scripts/install_launchd.py",
    "scripts/install_runtime.py",
    "scripts/insight_memory.py",
    "scripts/knowledge_index.py",
    "scripts/link_validator.py",
    "scripts/maintainer.py",
    "scripts/memory_judge.py",
    "scripts/memory_authority.py",
    "scripts/memory_effectiveness.py",
    "scripts/memory_identity_repair.py",
    "scripts/memory_lifecycle.py",
    "scripts/memory_lifecycle_batch.py",
    "scripts/memory_graph.py",
    "scripts/memory_relation_batch.py",
    "scripts/memory_promotion.py",
    "scripts/memory_quality_audit.py",
    "scripts/memory_recall.py",
    "scripts/memory_runtime.py",
    "scripts/memory_schema.py",
    "scripts/reporter.py",
    "scripts/requirements.lock",
    "scripts/requirements.txt",
    "scripts/runner.py",
    "scripts/safety.py",
    "scripts/score_sessions.py",
    "scripts/session_harvester.py",
    "scripts/setup.py",
    "scripts/skill_preference_learner.py",
    "scripts/transcript_utils.py",
    "scripts/validate_frontmatter.py",
    "scripts/workflow_memory.py",
    "templates/vault/00-Rules/_TEMPLATE.md",
    "templates/vault/00-Rules/_inbox/_TEMPLATE.md",
    "templates/vault/01-Projects/project-alpha/Feedback/_TEMPLATE.md",
    "templates/vault/01-Projects/project-alpha/Memory/cross-project-links.md",
    "templates/vault/01-Projects/project-alpha/Memory/decisions.md",
    "templates/vault/01-Projects/project-alpha/Memory/pitfalls.md",
    "templates/vault/01-Projects/project-alpha/Memory/sessions/_TEMPLATE.md",
    "templates/vault/04-Feedback/error-taxonomy.md",
    "templates/vault/04-Feedback/growth-metrics.md",
    "templates/vault/04-Feedback/heartbeat.md",
    "templates/vault/04-Feedback/weekly-reports/_TEMPLATE.md",
    "templates/vault/README.md",
    "templates/vault/用户手册.md",
}
WINDOWS_TEST_LAUNCHER_BYTES = b"MZ synthetic CPython venvlauncher fixture\n"
WINDOWS_TEST_BASE_EXE_BYTES = b"MZ synthetic base python fixture\n"
WINDOWS_TEST_ABI_DLL_BYTES = b"MZ synthetic python3 ABI DLL fixture\n"
WINDOWS_TEST_VERSION_DLL_BYTES = b"MZ synthetic python313 DLL fixture\n"


class RecordingRunner:
    def __init__(self, fail_match="", initial_service_queries=5):
        self.calls = []
        self.fail_match = fail_match
        self.initial_queries = 0
        self.initial_service_queries = initial_service_queries

    def __call__(self, args, **kwargs):
        command = tuple(os.fspath(item) for item in args)
        self.calls.append((command, dict(kwargs)))
        if "-m" in command and command[command.index("-m") + 1] == "venv":
            python = Path(command[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            python.chmod(0o755)
        if (
            len(command) >= 3
            and command[1] == "print"
            and self.initial_queries < self.initial_service_queries
        ):
            self.initial_queries += 1
            return SimpleNamespace(
                returncode=113,
                stdout="",
                stderr="Could not find service",
            )
        joined = " ".join(command)
        failed = bool(self.fail_match and self.fail_match in joined)
        if "doctor.py" in joined and "--json" in command:
            stdout = json.dumps({"status": "fail" if failed else "pass"})
        else:
            stdout = "state = running"
        return SimpleNamespace(
            returncode=1 if failed else 0,
            stdout=stdout,
            stderr="forced failure" if failed else "",
        )


class WindowsRuntimeRunner(RecordingRunner):
    def __init__(self, base_python_root):
        super().__init__()
        self.base_python_root = Path(base_python_root)

    def __call__(self, args, **kwargs):
        result = super().__call__(args, **kwargs)
        command = tuple(os.fspath(item) for item in args)
        if "-m" in command and command[command.index("-m") + 1] == "venv":
            write_windows_venv_fixture(
                Path(command[-1]).parent,
                self.base_python_root,
            )
        return result


class ArtifactWindowsRuntimeRunner(WindowsRuntimeRunner):
    def __init__(self, base_python_root, artifact_bytes):
        super().__init__(base_python_root)
        self.artifact_bytes = artifact_bytes

    def __call__(self, args, **kwargs):
        result = super().__call__(args, **kwargs)
        command = tuple(os.fspath(item) for item in args)
        if "-m" in command:
            module_index = command.index("-m")
            if command[module_index + 1 : module_index + 3] == ("pip", "install"):
                stage = Path(kwargs["cwd"])
                artifact = (
                    stage
                    / ".venv"
                    / "Lib"
                    / "site-packages"
                    / "installed-artifact.py"
                )
                artifact.parent.mkdir(parents=True, exist_ok=True)
                artifact.write_bytes(self.artifact_bytes)
        return result


class RealContextCompileRunner(RecordingRunner):
    def __call__(self, args, **kwargs):
        command = tuple(os.fspath(item) for item in args)
        if len(command) >= 4 and any(
            "from compiler import run" in item for item in command
        ):
            self.calls.append((command, dict(kwargs)))
            return subprocess.run(
                (sys.executable, *command[1:]),
                **kwargs,
            )
        return super().__call__(args, **kwargs)


class InitialServiceStateRunner(RecordingRunner):
    def __init__(self, states):
        super().__init__()
        self.states = dict(states)

    def __call__(self, args, **kwargs):
        command = tuple(os.fspath(item) for item in args)
        if len(command) >= 3 and command[1] == "print" and self.initial_queries < 5:
            self.calls.append((command, dict(kwargs)))
            self.initial_queries += 1
            label = command[-1].rsplit("/", 1)[-1]
            state = self.states.get(label, "missing")
            if state == "loaded":
                return SimpleNamespace(returncode=0, stdout="state = running", stderr="")
            if state == "missing":
                return SimpleNamespace(
                    returncode=113,
                    stdout="",
                    stderr="Could not find service",
                )
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="Operation not permitted",
            )
        return super().__call__(args, **kwargs)


class FirstPrintMissingRunner(RecordingRunner):
    """Report each launchd label missing once, then report it loaded."""

    def __init__(self):
        super().__init__(initial_service_queries=0)
        self.initial_labels = set()

    def __call__(self, args, **kwargs):
        command = tuple(os.fspath(item) for item in args)
        if len(command) >= 3 and command[1] == "print":
            label = command[-1].rsplit("/", 1)[-1]
            if label not in self.initial_labels:
                self.calls.append((command, dict(kwargs)))
                self.initial_labels.add(label)
                self.initial_queries += 1
                return SimpleNamespace(
                    returncode=113,
                    stdout="",
                    stderr="Could not find service",
                )
        return super().__call__(args, **kwargs)


def test_config(root, vault):
    codex_home = root / ".codex"
    codex_home.mkdir(parents=True, exist_ok=True)
    return {
        "version": "6.0.0",
        "product_id": "agent_memory_beacon",
        "user_home": str(root),
        "vault_path": str(vault),
        "codex_home": str(codex_home),
        "codex_sessions_path": str(codex_home / "sessions"),
        "python_path": sys.executable,
        "harvest_interval_seconds": 300,
        "memory_runtime": {"hook_timeout_ms": 2000},
        "scan": {"day": "SUN", "hour": 15, "minute": 0},
        "api": {"key": "do-not-publish", "settings_json": ""},
    }


def create_plan(root, vault):
    install_root = root / ".local" / "share" / "agent-memory-beacon" / "runtime"
    cfg = test_config(root, vault)
    return build_release_plan(REPO_ROOT, install_root, cfg)


def windows_sync_test_config(root, vault):
    cfg = test_config(root, vault)
    cfg["beacon_sync"] = {
        "enabled": True,
        "role": "producer-replica",
    }
    return cfg


def stage_for_test(plan, runner=None):
    return stage_runtime(plan, command_runner=runner or RecordingRunner())


def materialize_windows_release_for_test(plan, base_python_root=None):
    stage = plan.install_root.parent / (
        f".{plan.install_root.name}.staging-fixture-"
        f"{next(tempfile._get_candidate_names())}"
    )
    for item in plan.files:
        path = stage / item.relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item.content)
        path.chmod(item.mode)
    (stage / "release-manifest.json").write_bytes(
        plan.manifest_bytes
    )
    base_python_root = base_python_root or seed_windows_base_python(
        plan.install_root.parent / f".fixture-base-{next(tempfile._get_candidate_names())}"
    )
    write_windows_venv_fixture(stage, base_python_root)
    finalized = install_runtime._finalize_windows_staged_runtime(plan, stage)
    stage.rename(finalized.install_root)
    return finalized


def seed_windows_base_python(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "python.exe": WINDOWS_TEST_BASE_EXE_BYTES,
        "python3.dll": WINDOWS_TEST_ABI_DLL_BYTES,
        "python313.dll": WINDOWS_TEST_VERSION_DLL_BYTES,
    }
    for name, content in files.items():
        path = root / name
        path.write_bytes(content)
        path.chmod(0o755)
    (root / "Lib" / "json").mkdir(parents=True)
    (root / "Lib" / "json" / "__init__.py").write_bytes(b"# stdlib fixture\n")
    (root / "Lib" / "site-packages").mkdir()
    (root / "Lib" / "site-packages" / "base-only.py").write_bytes(
        b"raise AssertionError('base site-packages must stay outside the closure')\n"
    )
    (root / "DLLs").mkdir()
    (root / "DLLs" / "_ssl.pyd").write_bytes(b"MZ synthetic _ssl extension\n")
    return root


def write_windows_venv_fixture(
    runtime_root,
    base_python_root,
    *,
    prompt=None,
    caller_python=None,
    command_python=None,
    include_copies=True,
    include_without_pip=True,
):
    runtime_root = Path(runtime_root)
    base_python_root = Path(base_python_root)
    launcher = runtime_root / ".venv" / "Scripts" / "python.exe"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_bytes(WINDOWS_TEST_LAUNCHER_BYTES)
    launcher.chmod(0o755)
    site_packages = runtime_root / ".venv" / "Lib" / "site-packages"
    (site_packages / "yaml").mkdir(parents=True, exist_ok=True)
    (site_packages / "yaml" / "__init__.py").write_bytes(b"# yaml fixture\n")
    (site_packages / "fixture.pth").write_bytes(b"# empty but executable-format fixture\n")
    (site_packages / "fixture.pyd").write_bytes(b"MZ extension fixture\n")
    (site_packages / "fixture-1.0.dist-info").mkdir(exist_ok=True)
    (site_packages / "fixture-1.0.dist-info" / "METADATA").write_bytes(
        b"Name: fixture\nVersion: 1.0\n"
    )
    caller_python = Path(caller_python or (base_python_root / "python.exe"))
    command_python = Path(command_python or caller_python)
    rows = [
        f"home = {base_python_root}",
        "include-system-site-packages = false",
        "version = 3.13.0",
    ]
    if prompt is not None:
        rows.append(f"prompt = {prompt!r}")
    rows.extend(
        (
            f"executable = {caller_python}",
            (
                f"command = {command_python} -m venv "
                f"{'--copies ' if include_copies else ''}"
                f"{'--without-pip ' if include_without_pip else ''}"
                f"{runtime_root / '.venv'}"
            ),
        )
    )
    (runtime_root / ".venv" / "pyvenv.cfg").write_text(
        "\n".join(rows) + "\n",
        encoding="utf-8",
    )


def external_paths(root):
    codex_home = root / ".codex"
    launch_agents = root / "Library" / "LaunchAgents"
    return {
        "hooks": codex_home / "hooks.json",
        "agents": codex_home / "AGENTS.md",
        "harvest": launch_agents / f"{NEW_LAUNCHD_LABELS['harvest']}.plist",
        "weekly": launch_agents / f"{NEW_LAUNCHD_LABELS['weekly']}.plist",
    }


def seed_external_files(root):
    paths = external_paths(root)
    paths["hooks"].parent.mkdir(parents=True, exist_ok=True)
    paths["hooks"].write_text('{"hooks": {}}\n', encoding="utf-8")
    paths["agents"].write_bytes(b"user agents bytes\r\n")
    for kind in ("harvest", "weekly"):
        paths[kind].parent.mkdir(parents=True, exist_ok=True)
        paths[kind].write_bytes(f"old {kind} bytes".encode("ascii"))
    return paths, {name: path.read_bytes() for name, path in paths.items()}


class RuntimeInstallerTests(unittest.TestCase):
    def setUp(self):
        self.windows_runtime_probe = patch.object(
            install_runtime,
            "_windows_runtime_identity_for_plan",
            return_value={
                "runtime_python": {
                    "path": ".venv/Scripts/python.exe",
                    "size": len(WINDOWS_TEST_LAUNCHER_BYTES),
                    "sha256": hashlib.sha256(
                        WINDOWS_TEST_LAUNCHER_BYTES
                    ).hexdigest(),
                },
            },
        )
        self.windows_runtime_probe.start()
        self.addCleanup(self.windows_runtime_probe.stop)

    def test_install_runtime_import_does_not_require_posix_fcntl(self):
        code = "\n".join(
            (
                "import builtins, sys",
                "real_import = builtins.__import__",
                "def guarded_import(name, *args, **kwargs):",
                "    if name == 'fcntl':",
                "        raise ImportError('fcntl is unavailable')",
                "    return real_import(name, *args, **kwargs)",
                "builtins.__import__ = guarded_import",
                "sys.path.insert(0, sys.argv[1])",
                "import install_runtime",
            )
        )

        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", code, str(SCRIPTS_DIR)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_legacy_v1_rollback_manifest_without_sync_path_remains_valid(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            result = apply_runtime(
                plan,
                stage_for_test(plan),
                command_runner=RecordingRunner(),
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 1
            for key in (
                "authority_sync_enabled",
                "context_targets",
                "codex_profile_path",
                "transcript_agents",
            ):
                manifest.pop(key, None)
            manifest["external_before"] = [
                row for row in manifest["external_before"] if row["name"] != "sync"
            ]
            manifest["external_after"] = [
                row for row in manifest["external_after"] if row["name"] != "sync"
            ]
            manifest["services_before"].pop("sync", None)

            _validate_rollback_manifest(manifest, result.manifest_path)

    def test_legacy_v2_rollback_manifest_without_sync_path_remains_valid(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            result = apply_runtime(
                plan,
                stage_for_test(plan),
                command_runner=RecordingRunner(),
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            manifest["schema_version"] = 2
            manifest.pop("authority_sync_enabled", None)
            manifest["external_before"] = [
                row for row in manifest["external_before"] if row["name"] != "sync"
            ]
            manifest["external_after"] = [
                row for row in manifest["external_after"] if row["name"] != "sync"
            ]
            manifest["services_before"].pop("sync", None)

            _validate_rollback_manifest(manifest, result.manifest_path)

    def test_new_rollback_manifest_uses_sync_aware_schema(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))

            result = apply_runtime(
                plan,
                stage_for_test(plan),
                command_runner=RecordingRunner(),
            )

            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 3)
            self.assertIn("authority_sync_enabled", manifest)
            self.assertIn(
                "sync",
                {row["name"] for row in manifest["external_before"]},
            )

    def test_stable_runtime_rejects_windows_reparse_point_in_path_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "junction"
            target.mkdir()
            original_lstat = Path.lstat

            def fake_lstat(path):
                info = original_lstat(path)
                if path == target:
                    return SimpleNamespace(
                        st_mode=info.st_mode,
                        st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT,
                    )
                return info

            with patch.object(Path, "lstat", fake_lstat):
                with self.assertRaisesRegex(ValueError, "reparse"):
                    install_runtime._assert_no_symlink_chain(target)

    def test_windows_durable_replace_uses_write_through_move_without_directory_fsync(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            destination = root / "destination"
            source.write_bytes(b"release")

            def absolute(value):
                text = os.fspath(value)
                if text == str(source):
                    return source
                if text == str(destination):
                    return destination
                raise AssertionError(f"unexpected path: {value}")

            def write_through_move(actual_source, actual_destination):
                self.assertEqual(actual_source, source)
                self.assertEqual(actual_destination, destination)
                os.replace(actual_source, actual_destination)

            with (
                patch.object(install_runtime.os, "name", "nt"),
                patch.object(install_runtime, "_absolute_path", side_effect=absolute),
                patch.object(
                    install_runtime,
                    "_windows_move_file_write_through",
                    side_effect=write_through_move,
                    create=True,
                ) as move,
                patch.object(
                    install_runtime.os,
                    "open",
                    side_effect=AssertionError("Windows directory fsync path was used"),
                ),
            ):
                install_runtime._durable_replace(source, destination)

            move.assert_called_once_with(source, destination)
            self.assertEqual(destination.read_bytes(), b"release")

    def test_custom_codex_home_does_not_relocate_launchd_jobs(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            user_home = root / "user-home"
            codex_home = root / "state" / "codex-home"
            user_home.mkdir()
            codex_home.mkdir(parents=True)
            cfg = test_config(root, Path(vault))
            cfg["user_home"] = str(user_home)
            cfg["codex_home"] = str(codex_home)

            paths = _external_paths(cfg)

            self.assertEqual(paths["hooks"], codex_home / "hooks.json")
            self.assertEqual(paths["agents"], codex_home / "AGENTS.md")
            self.assertEqual(
                paths["harvest"].parent,
                user_home / "Library" / "LaunchAgents",
            )

    def test_custom_codex_home_install_can_be_rolled_back(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            user_home = root / "user-home"
            codex_home = root / "state" / "codex-home"
            user_home.mkdir()
            codex_home.mkdir(parents=True)
            cfg = test_config(root, Path(vault))
            cfg["user_home"] = str(user_home)
            cfg["codex_home"] = str(codex_home)
            install_root = (
                user_home / ".local" / "share" / "agent-memory-beacon" / "runtime"
            )
            plan = build_release_plan(REPO_ROOT, install_root, cfg)
            result = apply_runtime(
                plan,
                stage_for_test(plan),
                command_runner=RecordingRunner(),
            )

            rolled_back = rollback_runtime(
                result.manifest_path,
                command_runner=RecordingRunner(),
            )

            self.assertEqual(rolled_back.action, "rolled-back")
            self.assertFalse(plan.install_root.exists())
            self.assertFalse((codex_home / "hooks.json").exists())
            self.assertFalse((codex_home / "AGENTS.md").exists())

    def test_rollback_manifest_rejects_duplicate_external_rows(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            result = apply_runtime(
                plan,
                stage_for_test(plan),
                command_runner=RecordingRunner(),
            )
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            duplicate = dict(manifest["external_before"][0])
            duplicate["path"] = str(root / "outside-target")
            manifest["external_before"].insert(0, duplicate)

            with self.assertRaisesRegex(ValueError, "duplicate"):
                _validate_rollback_manifest(manifest, result.manifest_path)

    def test_external_restore_refuses_regular_parent_swap_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / ".codex" / "hooks.json"
            target.parent.mkdir()
            target.write_bytes(b"managed replacement")
            parent_stat = target.parent.stat()
            parent_identity = [parent_stat.st_dev, parent_stat.st_ino]
            backup = root / "snapshots" / "hooks.bin"
            backup.parent.mkdir()
            backup.write_bytes(b"original hooks")
            held_parent = root / ".codex-held"
            replacement_parent = target.parent
            outside_target = replacement_parent / target.name
            swapped = False

            def swap_parent_then_write(path, content, **kwargs):
                nonlocal swapped
                swapped = True
                replacement_parent.rename(held_parent)
                replacement_parent.mkdir()
                outside_target.write_bytes(b"outside sentinel")
                return durable_atomic_write(path, content, **kwargs)

            manifest = {
                "external_before": [
                    {
                        "name": "hooks",
                        "path": str(target),
                        "existed": True,
                        "parent_identity": parent_identity,
                        "mode": 0o600,
                        "sha256": hashlib.sha256(b"original hooks").hexdigest(),
                        "backup": "snapshots/hooks.bin",
                    }
                ],
                "external_after": [
                    {
                        "name": "hooks",
                        "path": str(target),
                        "existed": True,
                        "parent_identity": parent_identity,
                        "mode": 0o600,
                        "sha256": hashlib.sha256(b"managed replacement").hexdigest(),
                    }
                ],
            }
            with patch(
                "install_runtime.durable_atomic_write",
                side_effect=swap_parent_then_write,
                create=True,
            ):
                with self.assertRaises(OSError):
                    _restore_external_files(manifest, root / "manifest.json")

            self.assertTrue(swapped)
            self.assertEqual(outside_target.read_bytes(), b"outside sentinel")
            self.assertEqual(
                (held_parent / target.name).read_bytes(),
                b"managed replacement",
            )

    def test_external_restore_refuses_parent_swap_before_unlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / ".codex" / "hooks.json"
            target.parent.mkdir()
            target.write_bytes(b"managed replacement")
            held_parent = root / ".codex-held"
            outside = root / "outside"
            outside.mkdir()
            outside_target = outside / target.name
            outside_target.write_bytes(b"outside sentinel")
            original_is_regular = stat.S_ISREG
            swapped = False

            def swap_parent_then_check(mode):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    target.parent.rename(held_parent)
                    target.parent.symlink_to(outside, target_is_directory=True)
                return original_is_regular(mode)

            manifest = {
                "external_before": [
                    {
                        "name": "hooks",
                        "path": str(target),
                        "existed": False,
                        "parent_identity": [
                            target.parent.stat().st_dev,
                            target.parent.stat().st_ino,
                        ],
                    }
                ]
            }
            with patch(
                "install_runtime.stat.S_ISREG",
                side_effect=swap_parent_then_check,
            ):
                with self.assertRaises(OSError):
                    _restore_external_files(manifest, root / "manifest.json")

            self.assertEqual(outside_target.read_bytes(), b"outside sentinel")
            self.assertEqual(
                (held_parent / target.name).read_bytes(),
                b"managed replacement",
            )

    def test_remove_tree_refuses_symlinked_parent_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "managed"
            target = parent / "staging"
            target.mkdir(parents=True)
            (target / "managed.txt").write_bytes(b"managed")
            parent_stat = parent.stat()
            expected_parent = (parent_stat.st_dev, parent_stat.st_ino)
            held_parent = root / "managed-held"
            parent.rename(held_parent)
            outside = root / "outside"
            outside_target = outside / target.name
            outside_target.mkdir(parents=True)
            (outside_target / "sentinel.txt").write_bytes(b"outside sentinel")
            parent.symlink_to(outside, target_is_directory=True)

            try:
                _remove_tree(target, expected_parent_identity=expected_parent)
            except OSError:
                pass
            except TypeError as exc:
                self.fail(f"remove-tree parent identity is unsupported: {exc}")
            else:
                self.fail("symlinked replacement parent was accepted")

            self.assertEqual(
                (outside_target / "sentinel.txt").read_bytes(),
                b"outside sentinel",
            )
            self.assertEqual(
                (held_parent / target.name / "managed.txt").read_bytes(),
                b"managed",
            )

    def test_stage_rejects_runtime_python_older_than_311(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            plan = create_plan(Path(tmp), Path(vault))
            runner = RecordingRunner(fail_match="requires Python 3.11")

            with self.assertRaisesRegex(RuntimeError, "Python version"):
                stage_runtime(plan, command_runner=runner)

    def test_installer_python_subprocess_isolated_from_polluted_environment(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            marker = root / "pollution-executed"
            (root / "yaml.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('yaml')\n",
                encoding="utf-8",
            )
            fake_venv = root / "venv"
            fake_venv.mkdir()
            (fake_venv / "__init__.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('venv')\n",
                encoding="utf-8",
            )
            (root / "startup.py").write_text(
                f"from pathlib import Path\nPath({str(marker)!r}).write_text('startup')\n",
                encoding="utf-8",
            )
            code = (
                "import json, os, venv, yaml; "
                "print(json.dumps({"
                "'python_env': sorted(k for k in os.environ if k.upper().startswith('PYTHON')), "
                "'venv': venv.__file__, 'yaml': yaml.__file__}, sort_keys=True))"
            )
            pollution = {
                "PYTHONHOME": str(root),
                "PYTHONPATH": str(root),
                "PYTHONSTARTUP": str(root / "startup.py"),
                "PYTHONUSERBASE": str(root / "userbase"),
                "PYTHONINSPECT": "1",
                "pythonwarnings": "error",
            }

            with patch.dict(os.environ, pollution, clear=False):
                result = install_runtime._invoke(
                    None,
                    (sys.executable, "-I", "-B", "-c", code),
                    cwd=root,
                    timeout=30,
                )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["python_env"], [])
            self.assertNotIn(str(root), payload["venv"])
            self.assertNotIn(str(root), payload["yaml"])
            self.assertFalse(marker.exists())

    def test_stage_isolates_every_python_bootstrap_and_script_entry(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            base_python = seed_windows_base_python(root / "base-python")
            plan = install_runtime.build_windows_sync_release_plan(
                REPO_ROOT,
                root / "windows-runtime",
                windows_sync_test_config(root, Path(vault)),
            )
            runner = WindowsRuntimeRunner(base_python)

            stage_runtime(plan, command_runner=runner)

            for command, kwargs in runner.calls:
                if not command or not str(command[0]).lower().endswith(
                    ("python", "python3", "python.exe")
                ):
                    continue
                environment = kwargs.get("env")
                self.assertIsNotNone(environment, command)
                self.assertFalse(
                    any(key.upper().startswith("PYTHON") for key in environment),
                    command,
                )
                if "-m" in command:
                    module = command[command.index("-m") + 1]
                    if module in {"venv", "ensurepip", "pip", "unittest"}:
                        self.assertIn("-I", command[1 : command.index("-m")], command)
                if any(
                    str(item).endswith(("beacon_sync.py", "doctor.py"))
                    for item in command
                ) and "-I" not in command:
                    self.assertIn("-E", command[1:], command)
                    self.assertIn("-s", command[1:], command)
                    self.assertIn("-B", command[1:], command)
                    self.assertIn("-X", command[1:], command)
                    self.assertIn("utf8", command[1:], command)

    def test_remove_tree_refuses_replaced_regular_parent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            parent = root / "managed"
            target = parent / "staging"
            target.mkdir(parents=True)
            (target / "managed.txt").write_bytes(b"managed")
            parent_stat = parent.stat()
            expected_parent = (parent_stat.st_dev, parent_stat.st_ino)
            held_parent = root / "managed-held"
            parent.rename(held_parent)
            replacement = parent / target.name
            replacement.mkdir(parents=True)
            (replacement / "sentinel.txt").write_bytes(b"outside sentinel")

            try:
                _remove_tree(target, expected_parent_identity=expected_parent)
            except OSError:
                pass
            except TypeError as exc:
                self.fail(f"remove-tree parent identity is unsupported: {exc}")
            else:
                self.fail("regular replacement parent was accepted")

            self.assertEqual(
                (replacement / "sentinel.txt").read_bytes(),
                b"outside sentinel",
            )
            self.assertEqual(
                (held_parent / target.name / "managed.txt").read_bytes(),
                b"managed",
            )

    def test_release_plan_has_exact_allowlist_and_excludes_repository_state(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            plan = create_plan(Path(tmp), Path(vault))

            actual = {item.relative_path for item in plan.files}

            self.assertEqual(actual, EXPECTED_RUNTIME_FILES)
            forbidden = (".git/", "tests/", ".planning/", "docs/", "references/")
            self.assertFalse(any(path.startswith(forbidden) for path in actual))
            self.assertFalse(any("__pycache__" in path or ".venv" in path for path in actual))
            self.assertFalse(any(path.endswith((".db", ".sqlite", ".log")) for path in actual))

    def test_release_plan_and_manifest_are_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            first = create_plan(root, Path(vault))
            second = create_plan(root, Path(vault))

            self.assertEqual(first.release_id, second.release_id)
            self.assertEqual(first.manifest_bytes, second.manifest_bytes)
            payload = json.loads(first.manifest_bytes)
            paths = [item["path"] for item in payload["files"]]
            self.assertEqual(paths, sorted(paths))
            self.assertEqual(payload["release_id"], first.release_id)

    def test_windows_ci_covers_311_and_313_with_separate_release_verification(self):
        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        windows_job = workflow.split("  sync-windows:", 1)[1]

        self.assertIn('python-version: ["3.11", "3.13"]', windows_job)
        self.assertIn("matrix.python-version", windows_job)
        self.assertIn("Verify movable Windows runtime release", windows_job)
        self.assertIn("AGENT_MEMORY_BEACON_VERIFY_RELEASE", windows_job)
        self.assertIn("Verify Windows Task transaction", windows_job)

    def test_windows_ci_uses_only_producer_replica_and_installer_targets(self):
        expected = (
            "tests.test_beacon_sync_protocol",
            "tests.test_beacon_sync_producer",
            "tests.test_beacon_sync_cli",
            "tests.test_install_beacon_sync",
            "tests.test_beacon_sync_windows",
        )
        self.assertEqual(install_runtime.WINDOWS_SYNC_TEST_MODULES, expected)

        workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        matrix_step = workflow.split(
            "- name: Run Windows synchronization test matrix",
            1,
        )[1].split("- name: Verify movable Windows runtime release", 1)[0]
        for target in expected:
            self.assertIn(target, matrix_step)
        for authority_target in (
            "tests.test_beacon_sync_reducer",
            "tests.test_beacon_sync_snapshot",
            "tests.test_beacon_sync_end_to_end",
            "tests.test_config",
            "tests.test_install_runtime",
        ):
            self.assertNotIn(authority_target, matrix_step)

    def test_generated_config_uses_stable_python_and_redacts_inline_secrets(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            plan = create_plan(Path(tmp), Path(vault))
            config_file = next(
                item for item in plan.files if item.relative_path == "scripts/config.yaml"
            )
            rendered = yaml.safe_load(config_file.content)

            self.assertEqual(
                rendered["python_path"],
                str(plan.install_root / ".venv" / "bin" / "python"),
            )
            self.assertEqual(rendered["runtime_root"], str(plan.install_root))
            self.assertNotIn("key", rendered["api"])
            self.assertNotIn(b"do-not-publish", config_file.content)
            self.assertNotIn("source_python_path", rendered)
            self.assertNotIn(str(REPO_ROOT), config_file.content.decode("utf-8"))

    def test_windows_sync_release_is_versioned_and_uses_scripts_python(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            cfg = windows_sync_test_config(root, Path(vault))
            runtime_root = root / "windows-runtime"

            first = install_runtime.build_windows_sync_release_plan(
                REPO_ROOT,
                runtime_root,
                cfg,
            )
            second = install_runtime.build_windows_sync_release_plan(
                REPO_ROOT,
                runtime_root,
                cfg,
            )
            rendered = yaml.safe_load(
                next(
                    item.content
                    for item in first.files
                    if item.relative_path == "scripts/config.yaml"
                )
            )
            manifest = json.loads(first.manifest_bytes)

            self.assertEqual(first.release_id, second.release_id)
            self.assertEqual(first.manifest_bytes, second.manifest_bytes)
            self.assertEqual(
                first.install_root,
                runtime_root / "releases" / first.release_id,
            )
            self.assertEqual(rendered["runtime_root"], str(first.install_root))
            self.assertEqual(
                rendered["python_path"],
                str(
                    first.install_root
                    / ".venv"
                    / "Scripts"
                    / "python.exe"
                ),
            )
            self.assertEqual(manifest["release_kind"], "windows-sync")

    def test_windows_release_plan_binds_probed_launcher_not_source_python(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            source_python = root / "python.exe"
            source_python.write_bytes(b"MZ base python bytes\n")
            source_python.chmod(0o755)
            launcher_bytes = b"MZ CPython venvlauncher bytes\n"
            launcher_identity = {
                "path": ".venv/Scripts/python.exe",
                "size": len(launcher_bytes),
                "sha256": hashlib.sha256(launcher_bytes).hexdigest(),
            }
            cfg = windows_sync_test_config(root, Path(vault))
            cfg["python_path"] = str(source_python)

            with patch.object(
                install_runtime,
                "_windows_runtime_identity_for_plan",
                return_value={"runtime_python": launcher_identity},
            ) as probe:
                plan = install_runtime.build_windows_sync_release_plan(
                    REPO_ROOT,
                    root / "windows-runtime",
                    cfg,
                )

            manifest = json.loads(plan.manifest_bytes)
            probe.assert_called_once_with(str(source_python))
            self.assertRegex(manifest["source_release_id"], r"^[0-9a-f]{64}$")
            self.assertEqual(manifest["runtime_python"], launcher_identity)
            self.assertNotEqual(
                manifest["runtime_python"]["sha256"],
                hashlib.sha256(source_python.read_bytes()).hexdigest(),
            )

    def test_windows_release_manifest_binds_pyvenv_and_base_python_closure(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            runtime_identity = {
                "runtime_python": {
                    "path": ".venv/Scripts/python.exe",
                    "size": len(WINDOWS_TEST_LAUNCHER_BYTES),
                    "sha256": hashlib.sha256(
                        WINDOWS_TEST_LAUNCHER_BYTES
                    ).hexdigest(),
                },
                "runtime_environment": {
                    "pyvenv_config": {
                        "path": ".venv/pyvenv.cfg",
                        "size": 128,
                        "sha256": "1" * 64,
                    },
                    "base_python": {
                        "executable": {
                            "name": "python.exe",
                            "size": len(WINDOWS_TEST_BASE_EXE_BYTES),
                            "sha256": hashlib.sha256(
                                WINDOWS_TEST_BASE_EXE_BYTES
                            ).hexdigest(),
                        },
                        "dlls": [
                            {
                                "name": "python313.dll",
                                "size": len(WINDOWS_TEST_VERSION_DLL_BYTES),
                                "sha256": hashlib.sha256(
                                    WINDOWS_TEST_VERSION_DLL_BYTES
                                ).hexdigest(),
                            }
                        ],
                    },
                },
            }
            with patch.object(
                install_runtime,
                "_windows_runtime_identity_for_plan",
                return_value=runtime_identity,
                create=True,
            ) as probe:
                plan = install_runtime.build_windows_sync_release_plan(
                    REPO_ROOT,
                    root / "windows-runtime",
                    windows_sync_test_config(root, Path(vault)),
                )

            manifest = json.loads(plan.manifest_bytes)
            probe.assert_called_once_with(plan.source_python_path)
            self.assertEqual(
                manifest["runtime_environment"],
                runtime_identity["runtime_environment"],
            )
            self.assertEqual(
                plan.release_id,
                install_runtime._windows_release_id(
                    manifest["source_release_id"],
                    manifest["runtime_python"],
                    manifest["runtime_environment"],
                ),
            )

    def test_windows_runtime_identity_is_probe_path_independent_and_private(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            base_python = seed_windows_base_python(root / "base-python")
            (base_python / "python314.dll").write_bytes(
                b"MZ unrelated Python DLL name\n"
            )
            first_probe = root / "private-probe-a"
            second_probe = root / "private-probe-b"
            write_windows_venv_fixture(first_probe, base_python)
            write_windows_venv_fixture(second_probe, base_python)

            first_identity = install_runtime._windows_runtime_environment_identity(
                first_probe
            )
            second_identity = install_runtime._windows_runtime_environment_identity(
                second_probe
            )
            self.assertEqual(first_identity, second_identity)

            with patch.object(
                install_runtime,
                "_windows_runtime_identity_for_plan",
                return_value=first_identity,
            ):
                plan = install_runtime.build_windows_sync_release_plan(
                    REPO_ROOT,
                    root / "windows-runtime",
                    windows_sync_test_config(root, Path(vault)),
                )

            manifest_text = plan.manifest_bytes.decode("utf-8")
            self.assertNotIn(str(first_probe), manifest_text)
            self.assertNotIn(str(second_probe), manifest_text)
            self.assertNotIn(str(base_python), manifest_text)
            manifest = json.loads(manifest_text)
            dll_names = {
                row["name"]
                for row in manifest["runtime_environment"]["base_python"]["dlls"]
            }
            self.assertEqual(dll_names, {"python3.dll", "python313.dll"})

    def test_windows_runtime_identity_covers_actual_import_trees_and_limits(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base_python = seed_windows_base_python(root / "base-python")
            runtime = root / "runtime-stage"
            write_windows_venv_fixture(runtime, base_python)
            (base_python / "python313t.dll").write_bytes(b"MZ free-threaded DLL\n")
            (base_python / "python313t_d.dll").write_bytes(
                b"MZ debug free-threaded DLL\n"
            )
            (base_python / "python3t.dll").write_bytes(
                b"MZ stable ABI free-threaded DLL\n"
            )
            (base_python / "python3t_d.dll").write_bytes(
                b"MZ debug stable ABI free-threaded DLL\n"
            )

            identity = install_runtime._windows_runtime_environment_identity(runtime)

            environment = identity["runtime_environment"]
            closure = environment["execution_closure"]
            self.assertEqual(
                set(closure["limits"]),
                {"max_files", "max_total_bytes", "max_file_bytes"},
            )
            trees = {tree["name"]: tree for tree in closure["trees"]}
            self.assertEqual(
                set(trees),
                {"venv-site-packages", "base-stdlib", "base-dlls"},
            )
            site_paths = {row["path"] for row in trees["venv-site-packages"]["files"]}
            self.assertIn("fixture.pth", site_paths)
            self.assertIn("fixture.pyd", site_paths)
            self.assertIn("fixture-1.0.dist-info/METADATA", site_paths)
            stdlib_paths = {row["path"] for row in trees["base-stdlib"]["files"]}
            self.assertIn("json/__init__.py", stdlib_paths)
            self.assertNotIn("site-packages/base-only.py", stdlib_paths)
            dll_paths = {row["path"] for row in trees["base-dlls"]["files"]}
            self.assertEqual(dll_paths, {"_ssl.pyd"})
            dll_names = {
                row["name"]
                for row in environment["base_python"]["dlls"]
            }
            self.assertIn("python313t.dll", dll_names)
            self.assertIn("python313t_d.dll", dll_names)
            self.assertIn("python3t.dll", dll_names)
            self.assertIn("python3t_d.dll", dll_names)

    def test_windows_runtime_closure_rejects_hardlinks_and_size_overflow(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base_python = seed_windows_base_python(root / "base-python")
            runtime = root / "runtime-stage"
            write_windows_venv_fixture(runtime, base_python)
            source = base_python / "Lib" / "json" / "__init__.py"
            alias = base_python / "Lib" / "json" / "alias.py"
            try:
                os.link(source, alias)
            except OSError:
                self.skipTest("filesystem does not support hard links")

            with self.assertRaisesRegex(ValueError, "hard link"):
                install_runtime._windows_runtime_environment_identity(runtime)

            alias.unlink()
            pyvenv = runtime / ".venv" / "pyvenv.cfg"
            pyvenv_alias = runtime / ".venv" / "pyvenv-copy.cfg"
            os.link(pyvenv, pyvenv_alias)
            with self.assertRaisesRegex(ValueError, "hard link"):
                install_runtime._windows_runtime_environment_identity(runtime)
            pyvenv_alias.unlink()
            with patch.object(
                install_runtime,
                "WINDOWS_CLOSURE_MAX_FILE_BYTES",
                4,
                create=True,
            ):
                with self.assertRaisesRegex(ValueError, "size limit"):
                    install_runtime._windows_runtime_environment_identity(runtime)

    def test_pyvenv_identity_accepts_default_windows_copies_and_requires_no_pip(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            base_python = seed_windows_base_python(root / "base-python")
            callers = []
            for name in ("caller-a", "caller-b"):
                caller = root / name / "python.exe"
                caller.parent.mkdir()
                caller.write_bytes(WINDOWS_TEST_BASE_EXE_BYTES)
                caller.chmod(0o755)
                callers.append(caller)
            command_caller = root / "command-caller" / "python.exe"
            command_caller.parent.mkdir()
            command_caller.write_bytes(WINDOWS_TEST_LAUNCHER_BYTES)
            command_caller.chmod(0o755)
            first = root / "stage-a"
            second = root / "stage-b"
            write_windows_venv_fixture(
                first,
                base_python,
                caller_python=callers[0],
                command_python=command_caller,
            )
            write_windows_venv_fixture(second, base_python, caller_python=callers[1])

            default_copies = root / "stage-default-copies"
            write_windows_venv_fixture(
                default_copies,
                base_python,
                include_copies=False,
            )

            first_identity = install_runtime._windows_runtime_environment_identity(first)
            second_identity = install_runtime._windows_runtime_environment_identity(second)
            default_copies_identity = (
                install_runtime._windows_runtime_environment_identity(default_copies)
            )

            self.assertEqual(first_identity, second_identity)
            self.assertEqual(first_identity, default_copies_identity)
            invalid = root / "invalid-stage"
            write_windows_venv_fixture(
                invalid,
                base_python,
                include_without_pip=False,
            )
            with self.assertRaisesRegex(ValueError, "probe-equivalent"):
                install_runtime._windows_runtime_environment_identity(invalid)

    def test_windows_remove_tree_uses_portable_recursive_delete(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            parent = root / "runtime"
            target = parent / "failed-release"
            target.mkdir(parents=True)
            (target / "managed.txt").write_bytes(b"managed")
            parent_info = parent.stat()
            expected_parent = (parent_info.st_dev, parent_info.st_ino)

            with (
                patch.object(install_runtime.sys, "platform", "win32"),
                patch(
                    "beacon_sync_protocol.portable_rmtree",
                    return_value=True,
                ) as portable_rmtree,
            ):
                _remove_tree(
                    target,
                    expected_parent_identity=expected_parent,
                )

            portable_rmtree.assert_called_once_with(target, root=parent)
            self.assertTrue(target.exists())

    def test_windows_release_id_is_finalized_from_installed_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            base_python = seed_windows_base_python(root / "base-python")
            runtime_root = root / "windows-runtime"
            first_plan = install_runtime.build_windows_sync_release_plan(
                REPO_ROOT,
                runtime_root,
                windows_sync_test_config(root, Path(vault)),
            )
            second_plan = install_runtime.build_windows_sync_release_plan(
                REPO_ROOT,
                runtime_root,
                windows_sync_test_config(root, Path(vault)),
            )

            first = stage_runtime(
                first_plan,
                command_runner=ArtifactWindowsRuntimeRunner(base_python, b"first\n"),
            )
            second = stage_runtime(
                second_plan,
                command_runner=ArtifactWindowsRuntimeRunner(base_python, b"second\n"),
            )

            self.addCleanup(
                lambda: first.root.exists()
                and install_runtime._remove_tree(
                    first.root,
                    expected_parent_identity=(
                        first.root.parent.stat().st_dev,
                        first.root.parent.stat().st_ino,
                    ),
                )
            )
            self.addCleanup(
                lambda: second.root.exists()
                and install_runtime._remove_tree(
                    second.root,
                    expected_parent_identity=(
                        second.root.parent.stat().st_dev,
                        second.root.parent.stat().st_ino,
                    ),
                )
            )
            self.assertNotEqual(first.release_id, second.release_id)
            for staged in (first, second):
                manifest = json.loads(staged.manifest_path.read_text(encoding="utf-8"))
                self.assertEqual(manifest["release_id"], staged.release_id)
                self.assertIn(
                    "execution_closure",
                    manifest["runtime_environment"],
                )
                self.assertNotIn(".staging-", staged.manifest_path.read_text(encoding="utf-8"))

    def test_windows_release_verification_rejects_site_package_add_replace_delete(self):
        for mutation in ("add", "replace", "delete"):
            with (
                self.subTest(mutation=mutation),
                tempfile.TemporaryDirectory() as tmp,
                tempfile.TemporaryDirectory() as vault,
            ):
                root = Path(tmp)
                base_python = seed_windows_base_python(root / "base-python")
                plan = install_runtime.build_windows_sync_release_plan(
                    REPO_ROOT,
                    root / "windows-runtime",
                    windows_sync_test_config(root, Path(vault)),
                )
                staged = stage_runtime(
                    plan,
                    command_runner=WindowsRuntimeRunner(base_python),
                )
                destination = plan.install_root.parent / staged.release_id
                staged.root.rename(destination)
                site_packages = destination / ".venv" / "Lib" / "site-packages"
                target = site_packages / "yaml" / "__init__.py"
                if mutation == "add":
                    (site_packages / "extra.py").write_bytes(
                        b"unexpected importable file\n"
                    )
                elif mutation == "replace":
                    target.write_bytes(b"replaced dependency artifact\n")
                else:
                    target.unlink()
                changed_identity = (
                    install_runtime._windows_runtime_environment_identity(destination)
                )
                manifest_path = destination / "release-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["runtime_python"] = changed_identity["runtime_python"]
                manifest["runtime_environment"] = changed_identity[
                    "runtime_environment"
                ]
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                with self.assertRaisesRegex(ValueError, "release identity"):
                    install_runtime.verify_installed_release(destination)

    def test_windows_release_verification_rejects_rewritten_environment_drift(self):
        cases = ("home", "pyvenv", "base-executable", "base-dll")
        for case in cases:
            with (
                self.subTest(case=case),
                tempfile.TemporaryDirectory() as tmp,
                tempfile.TemporaryDirectory() as vault,
            ):
                root = Path(tmp)
                base_python = seed_windows_base_python(root / "base-python")
                other_base = seed_windows_base_python(root / "other-base-python")
                probe = root / "probe"
                write_windows_venv_fixture(probe, base_python)
                identity = install_runtime._windows_runtime_environment_identity(
                    probe
                )
                with patch.object(
                    install_runtime,
                    "_windows_runtime_identity_for_plan",
                    return_value=identity,
                ):
                    plan = install_runtime.build_windows_sync_release_plan(
                        REPO_ROOT,
                        root / "windows-runtime",
                        windows_sync_test_config(root, Path(vault)),
                    )
                plan = materialize_windows_release_for_test(plan, base_python)
                install_runtime.verify_installed_release(plan.install_root)

                if case == "home":
                    write_windows_venv_fixture(plan.install_root, other_base)
                elif case == "pyvenv":
                    pyvenv = plan.install_root / ".venv" / "pyvenv.cfg"
                    pyvenv.write_text(
                        pyvenv.read_text(encoding="utf-8").replace(
                            "version = 3.13.0",
                            "version = 3.13.1",
                        ),
                        encoding="utf-8",
                    )
                elif case == "base-executable":
                    (base_python / "python.exe").write_bytes(
                        WINDOWS_TEST_BASE_EXE_BYTES + b"changed"
                    )
                else:
                    (base_python / "python313.dll").write_bytes(
                        WINDOWS_TEST_VERSION_DLL_BYTES + b"changed"
                    )

                changed_identity = (
                    install_runtime._windows_runtime_environment_identity(
                        plan.install_root
                    )
                )
                manifest_path = plan.install_root / "release-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["runtime_python"] = changed_identity["runtime_python"]
                manifest["runtime_environment"] = changed_identity[
                    "runtime_environment"
                ]
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

                with self.assertRaisesRegex(ValueError, "release identity"):
                    install_runtime.verify_installed_release(plan.install_root)

    def test_windows_stage_uses_probe_equivalent_pyvenv_configuration(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            base_python = seed_windows_base_python(root / "base-python")
            probe = root / "probe"
            write_windows_venv_fixture(probe, base_python)
            identity = install_runtime._windows_runtime_environment_identity(probe)
            with patch.object(
                install_runtime,
                "_windows_runtime_identity_for_plan",
                return_value=identity,
            ):
                plan = install_runtime.build_windows_sync_release_plan(
                    REPO_ROOT,
                    root / "windows-runtime",
                    windows_sync_test_config(root, Path(vault)),
                )
            runner = WindowsRuntimeRunner(base_python)

            staged = stage_runtime(plan, command_runner=runner)

            commands = [command for command, _kwargs in runner.calls]
            venv_index = next(
                index
                for index, command in enumerate(commands)
                if "-m" in command
                and command[command.index("-m") + 1] == "venv"
            )
            ensurepip_index = next(
                index
                for index, command in enumerate(commands)
                if "-m" in command
                and command[command.index("-m") + 1] == "ensurepip"
            )
            pip_index = next(
                index
                for index, command in enumerate(commands)
                if "-m" in command
                and command[command.index("-m") + 1 : command.index("-m") + 3]
                == ("pip", "install")
            )
            self.assertIn("--without-pip", commands[venv_index])
            ensurepip_module = commands[ensurepip_index].index("-m")
            self.assertEqual(
                commands[ensurepip_index][ensurepip_module + 2 :],
                ("--upgrade",),
            )
            self.assertLess(venv_index, ensurepip_index)
            self.assertLess(ensurepip_index, pip_index)
            install_runtime._verify_staged_files(staged.final_plan, staged.root)

    def test_windows_release_rejects_changed_script_with_rewritten_manifest_row(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = install_runtime.build_windows_sync_release_plan(
                REPO_ROOT,
                root / "windows-runtime",
                windows_sync_test_config(root, Path(vault)),
            )
            plan = materialize_windows_release_for_test(plan)
            script_path = plan.install_root / "scripts" / "beacon_sync.py"
            changed = script_path.read_bytes() + b"\n# rewritten after release\n"
            script_path.write_bytes(changed)
            manifest_path = plan.install_root / "release-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            row = next(
                item
                for item in manifest["files"]
                if item["path"] == "scripts/beacon_sync.py"
            )
            row["size"] = len(changed)
            row["sha256"] = hashlib.sha256(changed).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "source release identity"):
                install_runtime.verify_installed_release(plan.install_root)

    def test_windows_release_rejects_changed_runtime_python_with_rewritten_identity_row(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = install_runtime.build_windows_sync_release_plan(
                REPO_ROOT,
                root / "windows-runtime",
                windows_sync_test_config(root, Path(vault)),
            )
            plan = materialize_windows_release_for_test(plan)
            python_path = plan.install_root / ".venv" / "Scripts" / "python.exe"
            changed = b"MZ replaced runtime python\n"
            python_path.write_bytes(changed)
            python_path.chmod(0o755)
            manifest_path = plan.install_root / "release-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if "runtime_python" in manifest:
                manifest["runtime_python"]["size"] = len(changed)
                manifest["runtime_python"]["sha256"] = hashlib.sha256(
                    changed
                ).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "release identity|runtime Python"):
                install_runtime.verify_installed_release(plan.install_root)

    def test_windows_release_rejects_tampered_manifest_file_row(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = install_runtime.build_windows_sync_release_plan(
                REPO_ROOT,
                root / "windows-runtime",
                windows_sync_test_config(root, Path(vault)),
            )
            plan = materialize_windows_release_for_test(plan)
            manifest_path = plan.install_root / "release-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            row = next(
                item
                for item in manifest["files"]
                if item["path"] == "scripts/beacon_sync.py"
            )
            row["sha256"] = "0" * 64
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "file changed"):
                install_runtime.verify_installed_release(plan.install_root)

    def test_windows_release_rejects_extra_script_that_can_shadow_dependency(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = install_runtime.build_windows_sync_release_plan(
                REPO_ROOT,
                root / "windows-runtime",
                windows_sync_test_config(root, Path(vault)),
            )
            plan = materialize_windows_release_for_test(plan)
            (plan.install_root / "scripts" / "yaml.py").write_bytes(
                b"raise AssertionError('shadowed dependency')\n"
            )

            with self.assertRaisesRegex(ValueError, "file set"):
                install_runtime.verify_installed_release(plan.install_root)

    def test_windows_sync_release_requires_enabled_producer_replica_role(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            cfg = test_config(root, Path(vault))
            cfg["beacon_sync"] = {
                "enabled": True,
                "role": "authority",
            }

            with self.assertRaisesRegex(ValueError, "producer-replica"):
                install_runtime.build_windows_sync_release_plan(
                    REPO_ROOT,
                    root / "windows-runtime",
                    cfg,
                )

    def test_producer_replica_sync_detection_is_role_specific_and_fail_closed(self):
        self.assertTrue(
            install_runtime._producer_replica_sync_enabled(
                {
                    "beacon_sync": {
                        "enabled": True,
                        "role": "producer-replica",
                    }
                }
            )
        )
        self.assertFalse(
            install_runtime._producer_replica_sync_enabled(
                {"beacon_sync": {"enabled": True, "role": "authority"}}
            )
        )
        self.assertFalse(
            install_runtime._producer_replica_sync_enabled(
                {"beacon_sync": {"enabled": False, "role": "producer-replica"}}
            )
        )
        with self.assertRaisesRegex(ValueError, "mapping"):
            install_runtime._producer_replica_sync_enabled(
                {"beacon_sync": ["producer-replica"]}
            )

    def test_windows_release_verification_rejects_missing_allowlisted_file_row(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = install_runtime.build_windows_sync_release_plan(
                REPO_ROOT,
                root / "windows-runtime",
                windows_sync_test_config(root, Path(vault)),
            )
            plan = materialize_windows_release_for_test(plan)
            omitted = "scripts/analyzer.py"
            manifest_path = plan.install_root / "release-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["files"] = [
                row for row in manifest["files"] if row["path"] != omitted
            ]
            manifest_path.write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )
            (plan.install_root / omitted).unlink()

            with self.assertRaisesRegex(ValueError, "file set"):
                install_runtime.verify_installed_release(plan.install_root)

    def test_windows_release_verification_binds_release_id_to_directory(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = install_runtime.build_windows_sync_release_plan(
                REPO_ROOT,
                root / "windows-runtime",
                windows_sync_test_config(root, Path(vault)),
            )
            plan = materialize_windows_release_for_test(plan)
            manifest_path = plan.install_root / "release-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["release_id"] = "0" * 16
            manifest_path.write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "release identity"):
                install_runtime.verify_installed_release(plan.install_root)

    def test_windows_release_verification_binds_runtime_config_to_release(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = install_runtime.build_windows_sync_release_plan(
                REPO_ROOT,
                root / "windows-runtime",
                windows_sync_test_config(root, Path(vault)),
            )
            plan = materialize_windows_release_for_test(plan)
            config_path = plan.install_root / "scripts" / "config.yaml"
            rendered = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            rendered["runtime_root"] = str(root / "other-runtime")
            config_bytes = yaml.safe_dump(
                rendered,
                allow_unicode=True,
                sort_keys=True,
            ).encode("utf-8")
            config_path.write_bytes(config_bytes)
            manifest_path = plan.install_root / "release-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            config_row = next(
                row
                for row in manifest["files"]
                if row["path"] == "scripts/config.yaml"
            )
            config_row["size"] = len(config_bytes)
            config_row["sha256"] = hashlib.sha256(config_bytes).hexdigest()
            manifest_path.write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "configuration"):
                install_runtime.verify_installed_release(plan.install_root)

    def test_windows_release_verification_rejects_symlinked_file_parent(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = install_runtime.build_windows_sync_release_plan(
                REPO_ROOT,
                root / "windows-runtime",
                windows_sync_test_config(root, Path(vault)),
            )
            plan = materialize_windows_release_for_test(plan)
            scripts = plan.install_root / "scripts"
            held = plan.install_root / "scripts-held"
            scripts.rename(held)
            try:
                scripts.symlink_to(held, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is unavailable")

            with self.assertRaisesRegex(ValueError, "symlink"):
                install_runtime.verify_installed_release(plan.install_root)

    @unittest.skipIf(os.name == "nt", "POSIX modes are not stable on Windows")
    def test_windows_release_verification_rejects_manifest_mode_drift(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = install_runtime.build_windows_sync_release_plan(
                REPO_ROOT,
                root / "windows-runtime",
                windows_sync_test_config(root, Path(vault)),
            )
            plan = materialize_windows_release_for_test(plan)
            config_path = plan.install_root / "scripts" / "config.yaml"
            config_path.chmod(0o644)

            with self.assertRaisesRegex(ValueError, "mode"):
                install_runtime.verify_installed_release(plan.install_root)

    def test_generated_config_preserves_effectiveness_and_promotion_settings(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            cfg = test_config(root, Path(vault))
            cfg["memory_effectiveness"] = {
                "enabled": True,
                "event_log_path": "04-Feedback/_logs/custom-effectiveness.jsonl",
                "report_path": "04-Feedback/custom-effectiveness.md",
                "feedback_window_minutes": 12,
                "max_report_items": 55,
                "resolved_report_path": "/private/path/must-not-leak",
            }
            cfg["memory_promotion"] = {
                "enabled": True,
                "proposal_dir": "04-Feedback/_custom-promotion-proposals",
                "min_source_count": 4,
                "min_exposure_count": 3,
                "max_proposals_per_run": 7,
                "resolved_proposal_dir": "/private/path/must-not-leak",
            }

            plan = build_release_plan(
                REPO_ROOT,
                root / ".local/share/agent-memory-beacon/runtime",
                cfg,
            )
            rendered = yaml.safe_load(
                next(
                    item.content
                    for item in plan.files
                    if item.relative_path == "scripts/config.yaml"
                )
            )

            self.assertEqual(rendered["memory_effectiveness"]["feedback_window_minutes"], 12)
            self.assertEqual(rendered["memory_promotion"]["min_source_count"], 4)
            self.assertNotIn("resolved_report_path", rendered["memory_effectiveness"])
            self.assertNotIn("resolved_proposal_dir", rendered["memory_promotion"])
            self.assertNotIn("/private/path", str(rendered))

    def test_generated_config_preserves_summary_and_graph_projection_settings(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            cfg = test_config(root, Path(vault))
            cfg["conversation_summary"] = {
                "enabled": True,
                "min_substantive_messages": 7,
                "message_interval": 12,
                "stale_after_minutes": 45,
                "retry_interval_messages": 3,
                "max_summary_bytes": 3072,
                "max_recall": 1,
                "token_budget": 320,
            }
            cfg["graph_projection"] = {
                "enabled": False,
                "output_dir": "03-Maps/_custom-memory-nodes",
                "max_nodes": 2400,
                "resolved_output_dir": "/private/path/must-not-leak",
            }

            plan = build_release_plan(
                REPO_ROOT,
                root / ".local/share/agent-memory-beacon/runtime",
                cfg,
            )
            rendered = yaml.safe_load(
                next(
                    item.content
                    for item in plan.files
                    if item.relative_path == "scripts/config.yaml"
                )
            )

            self.assertEqual(
                rendered["conversation_summary"],
                cfg["conversation_summary"],
            )
            self.assertEqual(
                rendered["graph_projection"],
                {
                    "enabled": False,
                    "output_dir": "03-Maps/_custom-memory-nodes",
                    "max_nodes": 2400,
                },
            )
            self.assertNotIn("/private/path", str(rendered))

    def test_generated_config_preserves_only_declared_beacon_sync_settings(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            cfg = test_config(root, Path(vault))
            cfg["beacon_sync"] = {
                "enabled": True,
                "role": "authority",
                "device_id": "",
                "state_dir": "/sync/state",
                "outbox_dir": "",
                "published_dir": "/sync/published",
                "replica_path": "",
                "received_published_dir": "",
                "inboxes": [
                    {"device_id": "windows-one", "path": "/sync/inbox"}
                ],
                "attachment_roots": ["/source/attachments"],
                "max_chunk_bytes": 1024,
                "max_gap_bytes": 4096,
                "max_attachment_bytes": 8192,
                "max_events_per_run": 8,
                "max_event_json_bytes": 8192,
                "max_object_bytes": 16384,
                "max_replica_object_bytes": 32768,
                "gc_retention_seconds": 60,
                "resolved_state_dir": "/private/must-not-leak",
            }

            plan = build_release_plan(
                REPO_ROOT,
                root / ".local/share/agent-memory-beacon/runtime",
                cfg,
            )
            rendered = yaml.safe_load(
                next(
                    item.content
                    for item in plan.files
                    if item.relative_path == "scripts/config.yaml"
                )
            )

            self.assertEqual(
                rendered["beacon_sync"],
                {
                    key: value
                    for key, value in cfg["beacon_sync"].items()
                    if key != "resolved_state_dir"
                },
            )
            self.assertNotIn("/private/must-not-leak", str(rendered))

    def test_generated_config_redacts_prefixed_and_nested_secret_keys(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            cfg = test_config(root, Path(vault))
            cfg["env"] = {
                "OPENAI_API_KEY": "openai-secret",
                "ANTHROPIC_AUTH_TOKEN": "anthropic-secret",
            }
            cfg["proxy"] = {
                "proxy_password": "proxy-secret",
                "client_secret": "client-secret",
            }
            cfg["headers"] = {"Authorization": "Bearer header-secret"}
            cfg["private_key"] = "private-key-secret"
            cfg["service_url"] = "https://url-user:url-secret@example.test/path"
            cfg["api"]["base_url"] = "https://api.example.test/v1"
            install_root = root / ".local/share/agent-memory-beacon/runtime"

            plan = build_release_plan(REPO_ROOT, install_root, cfg)
            content = next(
                item.content
                for item in plan.files
                if item.relative_path == "scripts/config.yaml"
            )

            for secret in (
                b"openai-secret",
                b"anthropic-secret",
                b"proxy-secret",
                b"client-secret",
                b"header-secret",
                b"private-key-secret",
                b"url-secret",
            ):
                self.assertNotIn(secret, content)
            rendered = yaml.safe_load(content)
            self.assertNotIn("headers", rendered)
            self.assertNotIn("private_key", rendered)
            self.assertNotIn("service_url", rendered)
            self.assertEqual(rendered["api"]["base_url"], "https://api.example.test/v1")

    def test_generated_config_rejects_nested_shapes_and_credential_urls(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            cases = {
                "projects mapping": lambda cfg: cfg.update(
                    {"projects": [{"headers": {"Authorization": "nested-secret"}}]}
                ),
                "project keyword mapping": lambda cfg: cfg.update(
                    {"project_keywords": {"alpha": {"private_key": "nested-secret"}}}
                ),
                "topic map mapping": lambda cfg: cfg.update(
                    {"topic_map": {"topic": {"private_key": "nested-secret"}}}
                ),
                "settings credential URL": lambda cfg: cfg["api"].update(
                    {
                        "settings_json": (
                            "https://settings-user:settings-secret@example.test/config"
                        )
                    }
                ),
                "base credential URL": lambda cfg: cfg["api"].update(
                    {"base_url": "https://api-user:api-secret@example.test/v1"}
                ),
                "path credential URL": lambda cfg: cfg.update(
                    {"backup_path": "https://backup-user:backup-secret@example.test/vault"}
                ),
                "scheme relative credential URL": lambda cfg: cfg["api"].update(
                    {"base_url": "//api-user:relative-secret@example.test/v1?token=x"}
                ),
                "absolute scheme query URL": lambda cfg: cfg["api"].update(
                    {"base_url": "https:api.example.test/v1?token=retained"}
                ),
            }

            for name, mutate in cases.items():
                with self.subTest(case=name):
                    cfg = test_config(root, Path(vault))
                    mutate(cfg)
                    with self.assertRaisesRegex(ValueError, "runtime configuration"):
                        build_release_plan(
                            REPO_ROOT,
                            root / ".local/share/agent-memory-beacon/runtime",
                            cfg,
                        )

    def test_generated_config_preserves_typed_compatibility_fields(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            cfg = test_config(root, Path(vault))
            cfg.update(
                {
                    "harvest_start_max_transcripts": 17,
                    "harvest_start_time_budget_seconds": 91,
                    "session_paths": [str(root / "legacy-sessions")],
                    "skip_git_probe": True,
                    "projects": [
                        "alpha",
                        {
                            "name": "beta",
                            "keywords": ["structured", "project"],
                            "headers": {"Authorization": "nested-project-secret"},
                        },
                    ],
                    "project_keywords": {
                        "alpha": "memory",
                        "beta": ["beacon"],
                    },
                    "topic_map": {"memory": ["alpha", "Memory"]},
                }
            )

            try:
                plan = build_release_plan(
                    REPO_ROOT,
                    root / ".local/share/agent-memory-beacon/runtime",
                    cfg,
                )
            except ValueError as exc:
                self.fail(f"valid typed runtime configuration was rejected: {exc}")
            content = next(
                item.content
                for item in plan.files
                if item.relative_path == "scripts/config.yaml"
            )
            rendered = yaml.safe_load(content)

            self.assertEqual(rendered.get("session_paths"), cfg["session_paths"])
            self.assertIs(rendered.get("skip_git_probe"), True)
            self.assertEqual(rendered.get("harvest_start_max_transcripts"), 17)
            self.assertEqual(
                rendered.get("harvest_start_time_budget_seconds"),
                91,
            )
            self.assertEqual(
                rendered["projects"],
                [
                    "alpha",
                    {
                        "name": "beta",
                        "keywords": ["structured", "project"],
                    },
                ],
            )
            self.assertEqual(
                rendered["project_keywords"],
                {"alpha": ["memory"], "beta": ["beacon"]},
            )
            self.assertEqual(rendered["topic_map"], {"memory": ["alpha", "Memory"]})
            self.assertNotIn("launch_agents_dir", rendered)
            self.assertNotIn(b"nested-project-secret", content)

    def test_release_plan_rejects_symlink_install_target_or_parent(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            real = root / "real"
            real.mkdir()
            target = root / "runtime"
            target.symlink_to(real, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink|reparse point"):
                build_release_plan(REPO_ROOT, target, test_config(root, Path(vault)))

            target.unlink()
            parent = root / "alias"
            parent.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink|reparse point"):
                build_release_plan(
                    REPO_ROOT,
                    parent / "runtime",
                    test_config(root, Path(vault)),
                )

    def test_stage_rewrites_config_and_runs_ci_venv_install_and_quick_preflight(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            plan = create_plan(Path(tmp), Path(vault))
            runner = RecordingRunner()

            staged = stage_for_test(plan, runner)

            config = yaml.safe_load(
                (staged.root / "scripts" / "config.yaml").read_text(encoding="utf-8")
            )
            self.assertEqual(config["runtime_root"], str(plan.install_root))
            self.assertTrue((staged.root / ".venv" / "bin" / "python").is_file())
            self.assertEqual(
                json.loads((staged.root / "release-manifest.json").read_text()),
                json.loads(plan.manifest_bytes),
            )
            commands = [" ".join(call) for call, _kwargs in runner.calls]
            self.assertTrue(
                any("doctor.py" in call and "--profile ci" in call for call in commands)
            )
            self.assertTrue(any("-m venv" in call for call in commands))
            self.assertTrue(any("-m pip install" in call for call in commands))
            pip_command = next(
                command
                for command, _kwargs in runner.calls
                if "-m" in command
                and command[command.index("-m") + 1 : command.index("-m") + 3]
                == ("pip", "install")
            )
            self.assertTrue(pip_command[-1].endswith("requirements.lock"))
            self.assertTrue(
                any("doctor.py" in call and "--profile quick" in call for call in commands)
            )
            self.assertTrue(all(not kwargs.get("shell") for _call, kwargs in runner.calls))

    def test_stage_copies_python_launcher_into_stable_runtime(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            plan = create_plan(Path(tmp), Path(vault))
            runner = RecordingRunner()

            stage_for_test(plan, runner)

            venv_command = next(
                command
                for command, _kwargs in runner.calls
                if "-m" in command
                and command[command.index("-m") + 1] == "venv"
            )
            self.assertIn("--copies", venv_command)

    def test_stage_failure_removes_incomplete_staging_tree(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            plan = create_plan(Path(tmp), Path(vault))

            with self.assertRaisesRegex(RuntimeError, "preflight"):
                stage_for_test(plan, RecordingRunner(fail_match="--profile quick"))

            self.assertFalse(any(plan.install_root.parent.glob(".runtime.staging-*")))

    def test_verify_release_discards_stage_without_switching_live_bindings(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            runner = RecordingRunner()

            result = verify_release(plan, command_runner=runner)

            self.assertEqual(result.action, "verified")
            self.assertEqual(result.release_id, plan.release_id)
            self.assertEqual(result.file_count, len(plan.files))
            self.assertFalse(plan.install_root.exists())
            self.assertFalse(any(plan.install_root.parent.glob(".runtime.staging-*")))
            for path in external_paths(root).values():
                self.assertFalse(path.exists())
            self.assertFalse(
                any(command[0] == "/bin/launchctl" for command, _kwargs in runner.calls)
            )

    def test_first_install_switches_all_live_bindings_to_stable_runtime(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            staged = stage_for_test(plan)

            result = apply_runtime(plan, staged, command_runner=RecordingRunner())

            self.assertEqual(result.action, "installed")
            self.assertTrue(plan.install_root.is_dir())
            hooks = (root / ".codex" / "hooks.json").read_text(encoding="utf-8")
            self.assertEqual(hooks.count("AGENT_MEMORY_BEACON_HOOK=1"), 3)
            self.assertIn(str(plan.install_root / "scripts"), hooks)
            agents = (root / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn("AGENT_MEMORY_BEACON:MANAGED_START", agents)
            for kind in ("harvest", "weekly"):
                path = external_paths(root)[kind]
                with path.open("rb") as handle:
                    payload = plistlib.load(handle)
                self.assertEqual(
                    payload["WorkingDirectory"],
                    str(plan.install_root / "scripts"),
                )
                self.assertTrue(
                    all(
                        str(plan.install_root) in value
                        for value in payload["ProgramArguments"][:2]
                    )
                )
            self.assertTrue(result.trust_review_required)
            self.assertTrue(Path(result.manifest_path).is_file())

    def test_first_authority_sync_install_includes_sync_launch_agent_transactionally(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            cfg = test_config(root, Path(vault))
            cfg["beacon_sync"] = {
                "enabled": True,
                "role": "authority",
                "state_dir": str(root / "sync-state"),
                "published_dir": str(root / "published"),
                "inboxes": [],
            }
            plan = build_release_plan(
                REPO_ROOT,
                root / ".local" / "share" / "agent-memory-beacon" / "runtime",
                cfg,
            )
            runner = FirstPrintMissingRunner()

            result = apply_runtime(
                plan,
                stage_for_test(plan),
                command_runner=runner,
            )

            sync_path = (
                root / "Library" / "LaunchAgents" / f"{SYNC_LAUNCHD_LABEL}.plist"
            )
            with sync_path.open("rb") as handle:
                payload = plistlib.load(handle)
            self.assertEqual(payload["Label"], SYNC_LAUNCHD_LABEL)
            self.assertEqual(
                payload["ProgramArguments"][:7],
                [
                    str(plan.install_root / ".venv" / "bin" / "python"),
                    "-E",
                    "-s",
                    "-X",
                    "utf8",
                    "-B",
                    str(plan.install_root / "scripts" / "beacon_sync.py"),
                ],
            )
            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
            self.assertIn("sync", manifest["services_before"])
            self.assertIn(
                str(sync_path),
                {row["path"] for row in manifest["external_before"]},
            )

    def test_sync_scheduler_failure_rolls_back_runtime_and_sync_plist(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            cfg = test_config(root, Path(vault))
            cfg["beacon_sync"] = {
                "enabled": True,
                "role": "authority",
                "state_dir": str(root / "sync-state"),
                "published_dir": str(root / "published"),
                "inboxes": [],
            }
            plan = build_release_plan(
                REPO_ROOT,
                root / ".local" / "share" / "agent-memory-beacon" / "runtime",
                cfg,
            )
            plan.install_root.mkdir(parents=True)
            (plan.install_root / "old-release.txt").write_bytes(b"old release")
            sync_path = (
                root / "Library" / "LaunchAgents" / f"{SYNC_LAUNCHD_LABEL}.plist"
            )
            sync_path.parent.mkdir(parents=True)
            sync_path.write_bytes(b"old sync plist")

            def mutate_and_fail(**kwargs):
                sync_path.write_bytes(b"new partial sync plist")
                raise RuntimeError("forced sync scheduler failure")

            with patch(
                "install_runtime.install_macos_scheduler",
                side_effect=mutate_and_fail,
                create=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "forced sync scheduler failure"):
                    apply_runtime(
                        plan,
                        stage_for_test(plan),
                        command_runner=FirstPrintMissingRunner(),
                    )

            self.assertEqual(sync_path.read_bytes(), b"old sync plist")
            self.assertEqual(
                (plan.install_root / "old-release.txt").read_bytes(),
                b"old release",
            )
            manifest_path = next(
                plan.install_root.parent.glob("rollback/*/manifest.json")
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "rolled_back")
            self.assertIn("sync", manifest["services_before"])

    def test_disabled_authority_sync_uninstalls_owned_scheduler_transactionally(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            cfg = test_config(root, Path(vault))
            cfg["beacon_sync"] = {
                "enabled": False,
                "role": "",
                "state_dir": "",
                "published_dir": "",
                "inboxes": [],
            }
            plan = build_release_plan(
                REPO_ROOT,
                root / ".local" / "share" / "agent-memory-beacon" / "runtime",
                cfg,
            )
            sync_path = (
                root / "Library" / "LaunchAgents" / f"{SYNC_LAUNCHD_LABEL}.plist"
            )
            sync_path.parent.mkdir(parents=True)
            sync_path.write_bytes(
                plistlib.dumps(
                    build_sync_launchd_plist(
                        python_path="/old/python",
                        script_path="/old/beacon_sync.py",
                        config_path="/old/config.yaml",
                        log_dir="/old/logs",
                    )
                )
            )

            def uninstall_sync(**kwargs):
                self.assertTrue(kwargs["uninstall"])
                sync_path.unlink()
                return {
                    "changed": True,
                    "path": str(sync_path),
                    "dry_run": False,
                }

            with patch(
                "install_runtime.install_macos_scheduler",
                side_effect=uninstall_sync,
            ) as scheduler:
                result = apply_runtime(
                    plan,
                    stage_for_test(plan),
                    command_runner=RecordingRunner(),
                )

            scheduler.assert_called_once()
            self.assertFalse(sync_path.exists())
            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
            self.assertIn("sync", manifest["services_before"])
            self.assertIn(
                str(sync_path),
                {row["path"] for row in manifest["external_before"]},
            )

    def test_install_rebuilds_index_and_context_before_live_preflight(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            staged = stage_for_test(plan)
            runner = RecordingRunner()

            result = apply_runtime(plan, staged, command_runner=runner)
            commands = [" ".join(command) for command, _kwargs in runner.calls]
            rebuilds = [
                index
                for index, command in enumerate(commands)
                if "rebuild_memory_index" in command
            ]
            compiles = [
                index
                for index, command in enumerate(commands)
                if "from compiler import run" in command
            ]
            live = [
                index
                for index, command in enumerate(commands)
                if "doctor.py --profile live" in command
            ]

            self.assertEqual(len(rebuilds), 1)
            self.assertIn("_refresh_effectiveness_report", commands[rebuilds[0]])
            self.assertEqual(len(compiles), 1)
            self.assertEqual(len(live), 1)
            self.assertLess(rebuilds[0], compiles[0])
            self.assertLess(compiles[0], live[0])
            self.assertNotIn("--mode index", commands[rebuilds[0]])
            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "installed")

    def test_index_rebuild_failure_rolls_back_before_installed_status(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            staged = stage_for_test(plan)
            runner = RecordingRunner(fail_match="rebuild_memory_index")

            with self.assertRaisesRegex(RuntimeError, "memory index rebuild"):
                apply_runtime(plan, staged, command_runner=runner)

            manifests = list(plan.install_root.parent.glob("rollback/*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "rolled_back")
            self.assertFalse(plan.install_root.exists())
            commands = [" ".join(command) for command, _kwargs in runner.calls]
            self.assertFalse(any("doctor.py --profile live" in item for item in commands))

    def test_context_compile_failure_rolls_back_before_live_preflight(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            staged = stage_for_test(plan)
            runner = RecordingRunner(fail_match="from compiler import run")

            with self.assertRaisesRegex(RuntimeError, "agent context compilation"):
                apply_runtime(plan, staged, command_runner=runner)

            manifests = list(plan.install_root.parent.glob("rollback/*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "rolled_back")
            self.assertFalse(plan.install_root.exists())
            commands = [" ".join(command) for command, _kwargs in runner.calls]
            self.assertFalse(any("doctor.py --profile live" in item for item in commands))

    def test_partial_real_context_compile_failure_restores_every_target_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            vault_path = Path(vault)
            (vault_path / "00-Rules").mkdir()
            (vault_path / "01-Projects").mkdir()
            claude = root / ".claude" / "CLAUDE.md"
            zcode = root / ".zcode" / "AGENTS.md"
            invalid = root / "custom-agent" / "INSTRUCTIONS.md"
            profile = vault_path / "05-Agent-Memory" / "codex-profile"
            shared = profile / "AGENTS.shared.md"
            managed = (
                REPO_ROOT / "patches" / "AGENT_MEMORY_BEACON.md.patch"
            ).read_bytes()
            originals = {
                claude: b"claude-prefix\r\n" + managed + b"\r\nclaude-suffix\r\n",
                zcode: b"zcode-prefix\n" + managed + b"\nzcode-suffix\n",
                invalid: b"custom file without compiler markers\r\n",
                shared: b"profile-prefix\r\n" + managed + b"\r\nprofile-suffix\r\n",
            }
            for index, (path, content) in enumerate(originals.items()):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
                path.chmod(0o640 + index)
            original_modes = {
                path: stat.S_IMODE(path.stat().st_mode) for path in originals
            }
            cfg = test_config(root, vault_path)
            cfg.update(
                {
                    "context_targets": [str(claude), str(zcode), str(invalid)],
                    "codex_profile_path": str(profile),
                    "skip_git_probe": True,
                }
            )
            plan = build_release_plan(
                REPO_ROOT,
                root / ".local" / "share" / "agent-memory-beacon" / "runtime",
                cfg,
            )
            staged = stage_for_test(plan)
            runner = RealContextCompileRunner()

            with self.assertRaisesRegex(RuntimeError, "agent context compilation"):
                apply_runtime(plan, staged, command_runner=runner)

            for path, content in originals.items():
                self.assertEqual(path.read_bytes(), content, path)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), original_modes[path], path)
            manifest_path = next(
                plan.install_root.parent.glob("rollback/*/manifest.json")
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "rolled_back")
            snapshotted = {row["path"] for row in manifest["external_before"]}
            self.assertTrue({str(path) for path in originals}.issubset(snapshotted))

    def test_release_plan_rejects_context_target_inside_source_checkout(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            cfg = test_config(root, Path(vault))
            cfg["context_targets"] = [str(REPO_ROOT / "AGENTS.md")]

            with self.assertRaisesRegex(ValueError, "source checkout"):
                build_release_plan(
                    REPO_ROOT,
                    root / ".local" / "share" / "agent-memory-beacon" / "runtime",
                    cfg,
                )

    def test_apply_removes_only_exact_unmarked_source_checkout_hook_bindings(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            staged = stage_for_test(plan)
            hooks_path = root / ".codex" / "hooks.json"
            source_scripts = plan.source_root / "scripts"
            hooks_path.write_text(
                json.dumps(
                    {
                        "hooks": {
                            "Stop": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                f'"{sys.executable}" '
                                                f'"{source_scripts / "session_harvester.py"}" '
                                                "--mode stop --agent codex"
                                            ),
                                            "timeout": 120,
                                        }
                                    ]
                                }
                            ],
                            "SessionStart": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                f'"{sys.executable}" '
                                                f'"{source_scripts / "session_harvester.py"}" '
                                                "--mode start"
                                            ),
                                            "timeout": 120,
                                        }
                                    ]
                                }
                            ],
                            "UserPromptSubmit": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                f'"{sys.executable}" '
                                                f'"{source_scripts / "codex_prompt_hook.py"}"'
                                            ),
                                            "timeout": 2,
                                        }
                                    ]
                                }
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )

            apply_runtime(plan, staged, command_runner=RecordingRunner())
            installed = hooks_path.read_text(encoding="utf-8")

            self.assertNotIn(str(plan.source_root), installed)
            self.assertEqual(installed.count("AGENT_MEMORY_BEACON_HOOK=1"), 3)
            self.assertEqual(installed.count(str(plan.install_root / "scripts")), 3)

    def test_stable_install_migrates_claude_bindings_off_source_checkout(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            vault_path = Path(vault)
            cfg = test_config(root, vault_path)
            cfg["transcript_agents"] = ["codex", "claude"]
            claude_home = root / ".claude"
            claude_home.mkdir()
            claude_md = claude_home / "CLAUDE.md"
            claude_md.write_text("user claude instructions\n", encoding="utf-8")
            cfg["context_targets"] = [str(claude_md)]
            settings = claude_home / "settings.json"
            source_harvester = REPO_ROOT / "scripts" / "session_harvester.py"
            settings.write_text(
                json.dumps(
                    {
                        "custom": {"preserve": True},
                        "hooks": {
                            "Stop": [
                                {
                                    "matcher": "owned-position",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": (
                                                f'"{sys.executable}" "{source_harvester}" '
                                                "--mode stop --agent claude"
                                            ),
                                            "timeout": 120,
                                        }
                                    ],
                                }
                            ]
                        },
                    }
                ),
                encoding="utf-8",
            )
            plan = build_release_plan(
                REPO_ROOT,
                root / ".local" / "share" / "agent-memory-beacon" / "runtime",
                cfg,
            )

            result = apply_runtime(
                plan,
                stage_for_test(plan),
                command_runner=RecordingRunner(),
            )

            installed = settings.read_text(encoding="utf-8")
            self.assertIn(str(plan.install_root / "scripts"), installed)
            self.assertNotIn(str(REPO_ROOT / "scripts"), installed)
            self.assertIn("AGENT_MEMORY_BEACON_HOOK=1", installed)
            self.assertTrue(json.loads(installed)["custom"]["preserve"])
            self.assertIn(
                "AGENT_MEMORY_BEACON:MANAGED_START",
                claude_md.read_text(encoding="utf-8"),
            )
            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))
            snapshotted = {row["path"] for row in manifest["external_before"]}
            self.assertIn(str(settings), snapshotted)
            self.assertIn(str(claude_md), snapshotted)

    def test_service_state_query_classifies_loaded_and_confirmed_missing(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            runner = InitialServiceStateRunner(
                {
                    NEW_LAUNCHD_LABELS["harvest"]: "loaded",
                    NEW_LAUNCHD_LABELS["weekly"]: "missing",
                    LEGACY_LAUNCHD_LABELS["harvest"]: "missing",
                    LEGACY_LAUNCHD_LABELS["weekly"]: "loaded",
                }
            )

            states = _service_states(_external_paths(plan.cfg), runner)

            self.assertEqual(
                states,
                {
                    "harvest": True,
                    "weekly": False,
                    "sync": False,
                    "legacy_harvest": False,
                    "legacy_weekly": True,
                },
            )
            self.assertEqual(runner.initial_queries, 5)

    def test_loaded_service_without_plist_aborts_before_any_install_mutation(self):
        for orphaned_label in (
            NEW_LAUNCHD_LABELS["harvest"],
            LEGACY_LAUNCHD_LABELS["weekly"],
        ):
            with self.subTest(label=orphaned_label):
                with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
                    root = Path(tmp)
                    plan = create_plan(root, Path(vault))
                    staged = stage_for_test(plan)
                    states = {
                        NEW_LAUNCHD_LABELS["harvest"]: "missing",
                        NEW_LAUNCHD_LABELS["weekly"]: "missing",
                        LEGACY_LAUNCHD_LABELS["harvest"]: "missing",
                        LEGACY_LAUNCHD_LABELS["weekly"]: "missing",
                    }
                    states[orphaned_label] = "loaded"
                    runner = InitialServiceStateRunner(states)

                    with self.assertRaisesRegex(RuntimeError, "loaded.*plist.*missing"):
                        apply_runtime(plan, staged, command_runner=runner)

                    self.assertFalse(plan.install_root.exists())
                    self.assertFalse((plan.install_root.parent / "rollback").exists())
                    self.assertTrue(staged.root.exists())

    def test_service_query_error_aborts_before_any_install_mutation(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            staged = stage_for_test(plan)
            paths, before = seed_external_files(root)
            runtime_paths = _external_paths(plan.cfg)
            for name in ("legacy_harvest", "legacy_weekly"):
                runtime_paths[name].write_bytes(f"old {name}".encode("ascii"))
            legacy_before = {
                name: runtime_paths[name].read_bytes()
                for name in ("legacy_harvest", "legacy_weekly")
            }
            runner = InitialServiceStateRunner(
                {
                    NEW_LAUNCHD_LABELS["harvest"]: "error",
                    NEW_LAUNCHD_LABELS["weekly"]: "missing",
                    LEGACY_LAUNCHD_LABELS["harvest"]: "missing",
                    LEGACY_LAUNCHD_LABELS["weekly"]: "missing",
                }
            )

            with self.assertRaisesRegex(RuntimeError, "launchd state query failed"):
                apply_runtime(plan, staged, command_runner=runner)

            self.assertTrue(staged.root.is_dir())
            self.assertFalse(plan.install_root.exists())
            self.assertFalse((plan.install_root.parent / "rollback").exists())
            self.assertEqual(
                {name: path.read_bytes() for name, path in paths.items()},
                before,
            )
            self.assertEqual(
                {
                    name: runtime_paths[name].read_bytes()
                    for name in ("legacy_harvest", "legacy_weekly")
                },
                legacy_before,
            )

    def test_apply_and_manual_rollback_use_the_same_installation_lock(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            staged = stage_for_test(plan)
            lock_path = plan.install_root.parent / ".install.lock"

            with patch("install_runtime.exclusive_file_lock") as lock:
                result = apply_runtime(plan, staged, command_runner=RecordingRunner())

            harvester_lock = Path(vault) / "04-Feedback" / "_logs" / "harvester.lock"
            self.assertEqual(
                lock.call_args_list,
                [call(lock_path), call(harvester_lock, root=str(Path(vault)))],
            )
            with patch("install_runtime.exclusive_file_lock") as lock:
                rollback_runtime(result.manifest_path, command_runner=RecordingRunner())

            self.assertEqual(
                lock.call_args_list,
                [call(lock_path), call(harvester_lock, root=str(Path(vault)))],
            )

    def test_upgrade_preserves_previous_runtime_for_manual_rollback(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            plan.install_root.mkdir(parents=True)
            (plan.install_root / "old-release.txt").write_bytes(b"old release")
            staged = stage_for_test(plan)

            result = apply_runtime(plan, staged, command_runner=RecordingRunner())
            manifest = json.loads(Path(result.manifest_path).read_text(encoding="utf-8"))

            self.assertEqual(result.action, "upgraded")
            previous = Path(manifest["previous_runtime_path"])
            self.assertEqual((previous / "old-release.txt").read_bytes(), b"old release")
            self.assertFalse((plan.install_root / "old-release.txt").exists())

    def test_runtime_publish_and_rollback_use_durable_directory_renames(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            plan.install_root.mkdir(parents=True)
            (plan.install_root / "old-release.txt").write_bytes(b"old release")
            staged = stage_for_test(plan)

            with patch(
                "install_runtime._durable_replace",
                wraps=install_runtime._durable_replace,
            ) as durable_replace:
                result = apply_runtime(
                    plan,
                    staged,
                    command_runner=RecordingRunner(),
                )
                manifest = json.loads(
                    Path(result.manifest_path).read_text(encoding="utf-8")
                )
                previous = Path(manifest["previous_runtime_path"])
                self.assertIn(
                    call(plan.install_root, previous),
                    durable_replace.call_args_list,
                )
                self.assertIn(
                    call(staged.root, plan.install_root),
                    durable_replace.call_args_list,
                )

                rollback_runtime(
                    result.manifest_path,
                    command_runner=RecordingRunner(),
                )

                self.assertIn(
                    call(previous, plan.install_root),
                    durable_replace.call_args_list,
                )

    def test_atomic_manifest_publish_uses_durable_directory_rename(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"

            with patch(
                "install_runtime._durable_replace",
                wraps=install_runtime._durable_replace,
            ) as durable_replace:
                install_runtime._atomic_write_json(path, {"status": "prepared"})

            self.assertEqual(json.loads(path.read_text()), {"status": "prepared"})
            self.assertEqual(len(durable_replace.call_args_list), 1)
            source, destination = durable_replace.call_args.args
            self.assertEqual(destination, path)
            self.assertEqual(source.parent, path.parent)

    def test_each_switch_failure_restores_runtime_and_external_bytes(self):
        boundaries = ("hooks", "agents", "launchd", "live")
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
                    root = Path(tmp)
                    plan = create_plan(root, Path(vault))
                    plan.install_root.mkdir(parents=True)
                    (plan.install_root / "old-release.txt").write_bytes(b"old release")
                    paths, before = seed_external_files(root)
                    staged = stage_for_test(plan)

                    patchers = failure_patchers(boundary, paths)
                    with patchers[0], patchers[1], patchers[2], patchers[3]:
                        with self.assertRaisesRegex(RuntimeError, "forced"):
                            apply_runtime(plan, staged, command_runner=RecordingRunner())

                    self.assertEqual(
                        {name: path.read_bytes() for name, path in paths.items()},
                        before,
                    )
                    self.assertEqual(
                        (plan.install_root / "old-release.txt").read_bytes(),
                        b"old release",
                    )
                    manifests = list(plan.install_root.parent.glob("rollback/*/manifest.json"))
                    self.assertEqual(len(manifests), 1)
                    self.assertEqual(
                        json.loads(manifests[0].read_text())["status"],
                        "rolled_back",
                    )

    def test_each_runtime_publish_failure_restores_the_previous_runtime(self):
        boundaries = ("move_previous", "publish_staged")
        for boundary in boundaries:
            with self.subTest(boundary=boundary):
                with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
                    root = Path(tmp)
                    plan = create_plan(root, Path(vault))
                    plan.install_root.mkdir(parents=True)
                    (plan.install_root / "old-release.txt").write_bytes(b"old release")
                    paths, before = seed_external_files(root)
                    staged = stage_for_test(plan)
                    original_replace = os.replace

                    def fail_selected(source, destination, *args, **kwargs):
                        source = Path(source)
                        destination = Path(destination)
                        moving_previous = source == plan.install_root
                        publishing_staged = source == staged.root
                        if (
                            boundary == "move_previous" and moving_previous
                        ) or (
                            boundary == "publish_staged" and publishing_staged
                        ):
                            raise OSError(f"forced {boundary} failure")
                        return original_replace(
                            source,
                            destination,
                            *args,
                            **kwargs,
                        )

                    with patch("install_runtime.os.replace", side_effect=fail_selected):
                        with self.assertRaisesRegex(RuntimeError, "forced"):
                            apply_runtime(plan, staged, command_runner=RecordingRunner())

                    self.assertEqual(
                        (plan.install_root / "old-release.txt").read_bytes(),
                        b"old release",
                    )
                    self.assertEqual(
                        {name: path.read_bytes() for name, path in paths.items()},
                        before,
                    )
                    manifest = next(
                        plan.install_root.parent.glob("rollback/*/manifest.json")
                    )
                    self.assertEqual(
                        json.loads(manifest.read_text())["status"],
                        "rolled_back",
                    )

    def test_manual_rollback_restores_previous_runtime_files_modes_and_absence(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            plan.install_root.mkdir(parents=True)
            (plan.install_root / "old-release.txt").write_bytes(b"old release")
            paths, before = seed_external_files(root)
            paths["agents"].chmod(0o640)
            before_mode = stat.S_IMODE(paths["agents"].stat().st_mode)
            staged = stage_for_test(plan)
            result = apply_runtime(plan, staged, command_runner=RecordingRunner())

            rolled_back = rollback_runtime(
                result.manifest_path,
                command_runner=RecordingRunner(),
            )

            self.assertEqual(rolled_back.action, "rolled-back")
            self.assertEqual(
                {name: path.read_bytes() for name, path in paths.items()},
                before,
            )
            self.assertEqual(stat.S_IMODE(paths["agents"].stat().st_mode), before_mode)
            self.assertEqual(
                (plan.install_root / "old-release.txt").read_bytes(),
                b"old release",
            )

    def test_manual_rollback_refuses_to_overwrite_post_install_user_changes(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            staged = stage_for_test(plan)
            result = apply_runtime(plan, staged, command_runner=RecordingRunner())
            agents = root / ".codex" / "AGENTS.md"
            agents.write_text("user changed after install\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "changed after installation"):
                rollback_runtime(result.manifest_path, command_runner=RecordingRunner())

            self.assertTrue(plan.install_root.is_dir())

    def test_interrupted_runtime_published_manifest_can_be_rolled_back(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            staged = stage_for_test(plan)

            with patch(
                "install_runtime.install_hooks",
                side_effect=KeyboardInterrupt("forced process interruption"),
            ):
                with self.assertRaisesRegex(KeyboardInterrupt, "forced process interruption"):
                    apply_runtime(plan, staged, command_runner=RecordingRunner())

            manifests = list(plan.install_root.parent.glob("rollback/*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            self.assertEqual(
                json.loads(manifests[0].read_text(encoding="utf-8"))["status"],
                "runtime_published",
            )

            try:
                result = rollback_runtime(manifests[0], command_runner=RecordingRunner())
            except Exception as exc:
                self.fail(f"interrupted rollback should be retryable: {exc}")

            self.assertEqual(result.action, "rolled-back")
            self.assertFalse(plan.install_root.exists())
            self.assertEqual(
                json.loads(manifests[0].read_text(encoding="utf-8"))["status"],
                "rolled_back_manual",
            )

    def test_rollback_failed_manifest_can_retry_only_unfinished_steps(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            staged = stage_for_test(plan)

            with patch(
                "install_runtime._run_live_preflight",
                side_effect=RuntimeError("forced live failure"),
            ), patch(
                "install_runtime._restore_loaded_services",
                side_effect=RuntimeError("forced service restore failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "automatic rollback incomplete"):
                    apply_runtime(plan, staged, command_runner=RecordingRunner())

            manifests = list(plan.install_root.parent.glob("rollback/*/manifest.json"))
            self.assertEqual(len(manifests), 1)
            failed = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(failed["status"], "rollback_failed")
            self.assertFalse(plan.install_root.exists())

            with patch(
                "install_runtime._restore_external_files",
                side_effect=AssertionError("completed external restore repeated"),
            ), patch(
                "install_runtime._restore_runtime_tree",
                side_effect=AssertionError("completed runtime restore repeated"),
            ):
                try:
                    result = rollback_runtime(
                        manifests[0],
                        command_runner=RecordingRunner(),
                    )
                except Exception as exc:
                    self.fail(f"failed rollback should be retryable: {exc}")

            self.assertEqual(result.action, "rolled-back")
            retried = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(retried["status"], "rolled_back_manual")

    def test_upgrade_retry_reconciles_runtime_restored_before_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            plan.install_root.mkdir(parents=True)
            (plan.install_root / "old-release.txt").write_bytes(b"old release")
            staged = stage_for_test(plan)
            original_atomic_write = install_runtime._atomic_write_json
            interrupted = False

            def interrupt_runtime_checkpoint(path, payload, mode=0o600):
                nonlocal interrupted
                state = (payload.get("rollback_progress") or {}).get(
                    "runtime_restored"
                )
                if not interrupted and state in (True, "complete"):
                    interrupted = True
                    raise KeyboardInterrupt("forced runtime checkpoint interruption")
                return original_atomic_write(path, payload, mode=mode)

            with patch(
                "install_runtime._run_live_preflight",
                side_effect=RuntimeError("forced live failure"),
            ), patch(
                "install_runtime._atomic_write_json",
                side_effect=interrupt_runtime_checkpoint,
            ):
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "runtime checkpoint interruption",
                ):
                    apply_runtime(plan, staged, command_runner=RecordingRunner())

            self.assertEqual(
                (plan.install_root / "old-release.txt").read_bytes(),
                b"old release",
            )
            manifest_path = next(plan.install_root.parent.glob("rollback/*/manifest.json"))

            try:
                result = rollback_runtime(
                    manifest_path,
                    command_runner=RecordingRunner(),
                )
            except Exception as exc:
                self.fail(f"runtime checkpoint interruption was not retryable: {exc}")

            self.assertEqual(result.action, "rolled-back")
            self.assertEqual(
                (plan.install_root / "old-release.txt").read_bytes(),
                b"old release",
            )

    def test_installed_retry_skips_drift_after_rollback_has_started(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            root = Path(tmp)
            plan = create_plan(root, Path(vault))
            result = apply_runtime(
                plan,
                stage_for_test(plan),
                command_runner=RecordingRunner(),
            )
            original_atomic_write = install_runtime._atomic_write_json
            interrupted = False

            def interrupt_external_checkpoint(path, payload, mode=0o600):
                nonlocal interrupted
                state = (payload.get("rollback_progress") or {}).get(
                    "external_restored"
                )
                if not interrupted and state in (True, "complete"):
                    interrupted = True
                    raise KeyboardInterrupt("forced external checkpoint interruption")
                return original_atomic_write(path, payload, mode=mode)

            with patch(
                "install_runtime._atomic_write_json",
                side_effect=interrupt_external_checkpoint,
            ):
                with self.assertRaisesRegex(
                    KeyboardInterrupt,
                    "external checkpoint interruption",
                ):
                    rollback_runtime(
                        result.manifest_path,
                        command_runner=RecordingRunner(),
                    )

            try:
                retried = rollback_runtime(
                    result.manifest_path,
                    command_runner=RecordingRunner(),
                )
            except Exception as exc:
                self.fail(f"started installed rollback was not retryable: {exc}")

            self.assertEqual(retried.action, "rolled-back")
            self.assertFalse(plan.install_root.exists())


def failure_patchers(boundary, paths):
    def mutate_and_fail(path, payload):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(payload)
        raise RuntimeError(f"forced {boundary} failure")

    hooks = (
        patch(
            "install_runtime.install_hooks",
            side_effect=lambda cfg, **kwargs: mutate_and_fail(paths["hooks"], b"new hooks"),
        )
        if boundary == "hooks"
        else patch("install_runtime._NO_PATCH_TARGET", create=True)
    )
    agents = (
        patch(
            "install_runtime.install_agents_patch",
            side_effect=lambda path, **kwargs: mutate_and_fail(paths["agents"], b"new agents"),
        )
        if boundary == "agents"
        else patch("install_runtime._NO_PATCH_TARGET", create=True)
    )
    launchd = (
        patch(
            "install_runtime.install_launch_agents",
            side_effect=lambda cfg, **kwargs: mutate_and_fail(
                paths["harvest"], b"new launchd"
            ),
        )
        if boundary == "launchd"
        else patch("install_runtime._NO_PATCH_TARGET", create=True)
    )
    live = (
        patch(
            "install_runtime._run_live_preflight",
            side_effect=RuntimeError("forced live failure"),
        )
        if boundary == "live"
        else patch("install_runtime._NO_PATCH_TARGET", create=True)
    )
    return hooks, agents, launchd, live


if __name__ == "__main__":
    unittest.main()
