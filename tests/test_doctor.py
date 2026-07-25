import contextlib
import hashlib
import io
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from branding import LEGACY_LAUNCHD_LABELS
from doctor import CommandResult, DoctorCheck, DoctorReport, main, run_profile


QUICK_CHECKS = (
    "configuration",
    "recall-index-schema",
    "script-compilation",
    "module-imports",
)
CI_CHECKS = QUICK_CHECKS + (
    "unit-tests",
    "runtime-evaluation",
    "git-diff-check",
)
LIVE_CHECKS = QUICK_CHECKS + (
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
    def __init__(self, cfg, *, stale_label="", loaded_legacy=""):
        super().__init__()
        self.cfg = cfg
        self.stale_label = stale_label
        self.loaded_legacy = loaded_legacy

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
        if label in set(LEGACY_LAUNCHD_LABELS.values()) and label != self.loaded_legacy:
            return CommandResult(returncode=113, stderr="Could not find service")
        runtime = (
            os.path.join(os.path.dirname(self.cfg["runtime_root"]), "stale-runtime")
            if label == self.stale_label
            else self.cfg["runtime_root"]
        )
        script = "session_harvester.py" if label.endswith("harvest") else "runner.py"
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
            passing = run_profile(
                "live",
                repo_root=REPO_ROOT,
                cfg=cfg,
                runner=LiveRecordingRunner(cfg),
            )
            self.assertTrue(check_named(passing, "codex-hooks").passed)
            self.assertTrue(check_named(passing, "launchd-plists").passed)

            hooks_path = os.path.join(cfg["codex_home"], "hooks.json")
            hooks = json.loads(read_text(hooks_path))
            hooks["hooks"]["Stop"][0]["hooks"][0]["command"] = (
                'AGENT_MEMORY_BEACON_HOOK=1 "/usr/bin/python3" '
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
    write_json(
        os.path.join(vault, "05-Agent-Memory", "recall-index.json"),
        {"schema_version": "2.0", "units": []},
    )

    def command(script, suffix=""):
        return (
            f'AGENT_MEMORY_BEACON_HOOK=1 "{python_path}" '
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
    return next(item for item in report.checks if item.name == name)


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
