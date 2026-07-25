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
    "scripts/branding.py",
    "scripts/codex_profile_sync.py",
    "scripts/codex_prompt_hook.py",
    "scripts/compiler.py",
    "scripts/config.py",
    "scripts/config.yaml",
    "scripts/context_install.py",
    "scripts/doctor.py",
    "scripts/error_evidence.py",
    "scripts/evaluate_annotation_quality.py",
    "scripts/evaluate_memory_comparison.py",
    "scripts/experience_memory.py",
    "scripts/install_claude.py",
    "scripts/install_codex.py",
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


class RecordingRunner:
    def __init__(self, fail_match=""):
        self.calls = []
        self.fail_match = fail_match
        self.initial_queries = 0

    def __call__(self, args, **kwargs):
        command = tuple(os.fspath(item) for item in args)
        self.calls.append((command, dict(kwargs)))
        if command[1:3] == ("-m", "venv"):
            python = Path(command[-1]) / "bin" / "python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            python.chmod(0o755)
        if (
            len(command) >= 3
            and command[1] == "print"
            and self.initial_queries < 4
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
        if len(command) >= 3 and command[1] == "print" and self.initial_queries < 4:
            self.calls.append((command, dict(kwargs)))
            self.initial_queries += 1
            label = command[-1].rsplit("/", 1)[-1]
            state = self.states[label]
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
        "python_path": "/usr/bin/python3",
        "harvest_interval_seconds": 300,
        "memory_runtime": {"hook_timeout_ms": 2000},
        "scan": {"day": "SUN", "hour": 15, "minute": 0},
        "api": {"key": "do-not-publish", "settings_json": ""},
    }


def create_plan(root, vault):
    install_root = root / ".local" / "share" / "agent-memory-beacon" / "runtime"
    cfg = test_config(root, vault)
    return build_release_plan(REPO_ROOT, install_root, cfg)


def stage_for_test(plan, runner=None):
    return stage_runtime(plan, command_runner=runner or RecordingRunner())


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

            with self.assertRaisesRegex(ValueError, "symlink"):
                build_release_plan(REPO_ROOT, target, test_config(root, Path(vault)))

            target.unlink()
            parent = root / "alias"
            parent.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
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
            self.assertTrue(any("doctor.py --profile ci" in call for call in commands))
            self.assertTrue(any("-m venv" in call for call in commands))
            self.assertTrue(any("-m pip install" in call for call in commands))
            pip_command = next(
                command
                for command, _kwargs in runner.calls
                if command[1:4] == ("-m", "pip", "install")
            )
            self.assertTrue(pip_command[-1].endswith("requirements.lock"))
            self.assertTrue(any("doctor.py --profile quick" in call for call in commands))
            self.assertTrue(all(not kwargs.get("shell") for _call, kwargs in runner.calls))

    def test_stage_copies_python_launcher_into_stable_runtime(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as vault:
            plan = create_plan(Path(tmp), Path(vault))
            runner = RecordingRunner()

            stage_for_test(plan, runner)

            venv_command = next(
                command
                for command, _kwargs in runner.calls
                if command[1:3] == ("-m", "venv")
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
                    "legacy_harvest": False,
                    "legacy_weekly": True,
                },
            )
            self.assertEqual(runner.initial_queries, 4)

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
