import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import install_launchd
from branding import LEGACY_LAUNCHD_LABELS, NEW_LAUNCHD_LABELS
from install_launchd import (
    _restore_original_plists,
    build_harvest_plist,
    build_weekly_plist,
    install_launch_agents,
    job_paths,
    remove_legacy_job,
    write_plist_atomic,
)


class RecordingRunner:
    def __init__(self, fail_on=None, raise_on=None, failure_stderr="forced failure"):
        self.calls = []
        self.fail_on = fail_on
        self.raise_on = raise_on
        self.failure_stderr = failure_stderr

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))
        command = " ".join(str(item) for item in args)
        if self._matches(self.raise_on, command):
            raise TimeoutError("forced runner timeout")
        failed = self._matches(self.fail_on, command)
        return SimpleNamespace(
            returncode=1 if failed else 0,
            stdout="" if failed else "state = running",
            stderr=self.failure_stderr if failed else "",
        )

    def _matches(self, match, command):
        if callable(match):
            return match(command, self.calls)
        return match and match in command


class InitiallyUnloadedHarvestRunner:
    def __init__(self):
        self.calls = []
        self.weekly_bootstrap_failed = False

    def __call__(self, args, **_kwargs):
        command = [str(item) for item in args]
        self.calls.append(command)
        action = command[1] if len(command) > 1 else ""
        target = command[-1] if command else ""
        any_bootstrap = any(
            len(call) > 1 and call[1] == "bootstrap" for call in self.calls[:-1]
        )
        if action == "print" and not any_bootstrap:
            if target.endswith("/" + NEW_LAUNCHD_LABELS["harvest"]):
                return SimpleNamespace(
                    returncode=113,
                    stdout="",
                    stderr="Could not find service",
                )
            return SimpleNamespace(returncode=0, stdout="state = running", stderr="")
        if (
            action == "bootstrap"
            and target.endswith(f"{NEW_LAUNCHD_LABELS['weekly']}.plist")
            and not self.weekly_bootstrap_failed
        ):
            self.weekly_bootstrap_failed = True
            return SimpleNamespace(returncode=1, stdout="", stderr="forced failure")
        return SimpleNamespace(returncode=0, stdout="state = running", stderr="")


def create_legacy_plists(home):
    launch_agents = Path(home) / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True, exist_ok=True)
    paths = []
    for label in LEGACY_LAUNCHD_LABELS.values():
        path = launch_agents / f"{label}.plist"
        path.write_bytes(b"legacy plist fixture")
        paths.append(path)
    return paths


def write_current_plists(home, harvest=b"old harvest", weekly=b"old weekly"):
    paths = {
        "harvest": job_paths(home, "harvest")[0],
        "weekly": job_paths(home, "weekly")[0],
    }
    for kind, path in paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(harvest if kind == "harvest" else weekly)
    return paths


def tree_snapshot(root):
    root = Path(root)
    return {
        path.relative_to(root): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def fail_first_post_bootstrap_print(label):
    failed = False

    def predicate(command, calls):
        nonlocal failed
        if failed or "print gui/" not in command or label not in command:
            return False
        bootstrapped = any(
            "bootstrap" in " ".join(call) and label in " ".join(call)
            for call in calls[:-1]
        )
        if bootstrapped:
            failed = True
            return True
        return False

    return predicate


class LaunchdInstallerTests(unittest.TestCase):
    def test_failed_install_keeps_previously_unloaded_job_unloaded(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            current = write_current_plists(home)
            originals = {kind: path.read_bytes() for kind, path in current.items()}
            runner = InitiallyUnloadedHarvestRunner()

            with self.assertRaisesRegex(RuntimeError, "forced failure"):
                install_launch_agents(test_cfg(vault), home=home, command_runner=runner)

            harvest_bootstraps = [
                call
                for call in runner.calls
                if len(call) > 1
                and call[1] == "bootstrap"
                and call[-1].endswith(f"{NEW_LAUNCHD_LABELS['harvest']}.plist")
            ]
            self.assertEqual(len(harvest_bootstraps), 1)
            self.assertEqual(
                {kind: path.read_bytes() for kind, path in current.items()},
                originals,
            )

    def test_restore_absent_original_refuses_parent_symlink_swap(self):
        with tempfile.TemporaryDirectory() as home:
            path = job_paths(home, "harvest")[0]
            path.parent.mkdir(parents=True)
            path.write_bytes(b"managed replacement")
            held_parent = path.parent.with_name("LaunchAgents-held")
            path.parent.rename(held_parent)
            outside = Path(home) / "outside"
            outside.mkdir()
            outside_target = outside / path.name
            outside_target.write_bytes(b"outside sentinel")
            path.parent.symlink_to(outside, target_is_directory=True)
            jobs = [
                {
                    "kind": "harvest",
                    "path": path,
                    "label": NEW_LAUNCHD_LABELS["harvest"],
                }
            ]

            errors = _restore_original_plists(
                jobs,
                {path: None},
                reload_originals={},
                command_runner=None,
            )

            self.assertTrue(errors)
            self.assertIn("unlink failed", errors[0])
            self.assertEqual(outside_target.read_bytes(), b"outside sentinel")
            self.assertEqual(
                (held_parent / path.name).read_bytes(),
                b"managed replacement",
            )

    def test_plist_write_does_not_follow_planted_legacy_tmp_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "job.plist"
            sentinel = root / "outside.bin"
            sentinel.write_bytes(b"outside sentinel")
            target.with_suffix(".plist.tmp").symlink_to(sentinel)

            write_plist_atomic(target, {"Label": "safe.test"})

            self.assertEqual(sentinel.read_bytes(), b"outside sentinel")
            self.assertFalse(target.is_symlink())
            self.assertEqual(plistlib.loads(target.read_bytes())["Label"], "safe.test")

    def test_plist_restore_does_not_follow_planted_legacy_restore_symlink(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            current = write_current_plists(home)
            originals = {kind: path.read_bytes() for kind, path in current.items()}
            sentinel = Path(home) / "outside.bin"
            sentinel.write_bytes(b"outside sentinel")
            current["harvest"].with_suffix(".plist.restore").symlink_to(sentinel)
            runner = RecordingRunner(
                fail_on=fail_first_post_bootstrap_print(
                    NEW_LAUNCHD_LABELS["harvest"]
                ),
            )

            with self.assertRaisesRegex(RuntimeError, "launchctl print failed"):
                install_launch_agents(test_cfg(vault), home=home, command_runner=runner)

            self.assertEqual(sentinel.read_bytes(), b"outside sentinel")
            self.assertEqual(
                {kind: path.read_bytes() for kind, path in current.items()},
                originals,
            )

    def test_plists_use_agent_memory_beacon_labels(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)

            self.assertEqual(
                build_harvest_plist(cfg)["Label"],
                NEW_LAUNCHD_LABELS["harvest"],
            )
            self.assertEqual(
                build_weekly_plist(cfg)["Label"],
                NEW_LAUNCHD_LABELS["weekly"],
            )

    def test_legacy_harvest_plist_prevents_baseline_reset(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            launch_agents = os.path.join(home, "Library", "LaunchAgents")
            os.makedirs(launch_agents)
            legacy = os.path.join(
                launch_agents,
                LEGACY_LAUNCHD_LABELS["harvest"] + ".plist",
            )
            with open(legacy, "wb") as handle:
                handle.write(b"legacy")

            with patch("install_launchd.initialize_harvest_baseline") as initialize:
                install_launch_agents(
                    test_cfg(vault),
                    home=home,
                    include_weekly=False,
                    load=False,
                )

            initialize.assert_not_called()

    def test_first_harvest_install_initializes_existing_transcript_baseline(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)

            with patch(
                "install_launchd.initialize_harvest_baseline",
                return_value=7,
            ) as initialize:
                actions = install_launch_agents(
                    cfg,
                    home=home,
                    include_weekly=False,
                    load=False,
                )

            initialize.assert_called_once_with(cfg)
            self.assertIn("BASELINE existing transcripts: 7", actions)

    def test_reinstall_does_not_refresh_baseline_and_hide_pending_changes(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            install_launch_agents(
                cfg,
                home=home,
                include_weekly=False,
                load=False,
            )

            with patch("install_launchd.initialize_harvest_baseline") as initialize:
                install_launch_agents(
                    cfg,
                    home=home,
                    include_weekly=False,
                    load=False,
                )

            initialize.assert_not_called()

    def test_harvest_job_is_bounded_and_skips_deep_scanner(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)

            plist = build_harvest_plist(cfg)

            self.assertEqual(plist["StartInterval"], 300)
            self.assertIn("session_harvester.py", " ".join(plist["ProgramArguments"]))
            self.assertIn("--mode", plist["ProgramArguments"])
            self.assertIn("start", plist["ProgramArguments"])
            self.assertIn("--skip-scanner", plist["ProgramArguments"])
            self.assertIn("--skip-profile-check", plist["ProgramArguments"])
            self.assertTrue(plist["RunAtLoad"])
            self.assertEqual(plist["ProcessType"], "Standard")

    def test_weekly_job_uses_configured_launchd_calendar(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            cfg["scan"] = {"day": "MON", "hour": 10, "minute": 15}

            plist = build_weekly_plist(cfg)

            self.assertEqual(
                plist["StartCalendarInterval"],
                {"Weekday": 1, "Hour": 10, "Minute": 15},
            )
            self.assertIn("runner.py", " ".join(plist["ProgramArguments"]))
            self.assertNotIn("--full", plist["ProgramArguments"])
            self.assertEqual(len(plist["ProgramArguments"]), 2)
            self.assertEqual(plist["ProcessType"], "Background")

    def test_plists_can_bind_to_an_explicit_stable_scripts_directory(self):
        with tempfile.TemporaryDirectory() as vault, tempfile.TemporaryDirectory() as runtime:
            scripts = Path(runtime) / "scripts"
            scripts.mkdir()
            cfg = test_cfg(vault)

            harvest = build_harvest_plist(cfg, scripts_dir=scripts)
            weekly = build_weekly_plist(cfg, scripts_dir=scripts)

            self.assertEqual(harvest["WorkingDirectory"], str(scripts))
            self.assertEqual(weekly["WorkingDirectory"], str(scripts))
            self.assertEqual(
                harvest["ProgramArguments"][1],
                str(scripts / "session_harvester.py"),
            )
            self.assertEqual(
                weekly["ProgramArguments"][1],
                str(scripts / "runner.py"),
            )

    def test_successful_transaction_removes_legacy_only_after_new_print(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            legacy_paths = create_legacy_plists(home)
            runner = RecordingRunner()

            install_launch_agents(test_cfg(vault), home=home, command_runner=runner)

            self.assertFalse(any(path.exists() for path in legacy_paths))
            calls = [" ".join(call) for call in runner.calls]
            new_prints = [i for i, call in enumerate(calls) if "print gui/" in call]
            legacy_bootouts = [
                i
                for i, call in enumerate(calls)
                if "bootout" in call and "com.obsidian-knowledge-brain" in call
            ]
            self.assertLess(max(new_prints), min(legacy_bootouts))

    def test_failed_new_job_keeps_legacy_and_restores_new_plists(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            legacy_paths = create_legacy_plists(home)
            runner = RecordingRunner(fail_on="io.agent-memory-beacon.weekly")

            with self.assertRaisesRegex(RuntimeError, "forced failure"):
                install_launch_agents(test_cfg(vault), home=home, command_runner=runner)

            self.assertTrue(all(path.exists() for path in legacy_paths))
            current_paths = [
                job_paths(home, "harvest")[0],
                job_paths(home, "weekly")[0],
            ]
            self.assertFalse(any(path.exists() for path in current_paths))

    def test_no_load_keeps_legacy_plists(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            legacy_paths = create_legacy_plists(home)
            runner = RecordingRunner()

            install_launch_agents(
                test_cfg(vault),
                home=home,
                load=False,
                command_runner=runner,
            )

            self.assertTrue(all(path.exists() for path in legacy_paths))
            self.assertTrue(job_paths(home, "harvest")[0].exists())
            self.assertTrue(job_paths(home, "weekly")[0].exists())
            self.assertEqual(runner.calls, [])

    def test_failed_new_job_restores_preexisting_current_plist_bytes(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            current_path = job_paths(home, "harvest")[0]
            current_path.parent.mkdir(parents=True)
            original = b"previous current plist"
            current_path.write_bytes(original)
            runner = RecordingRunner(fail_on="io.agent-memory-beacon.weekly")

            with self.assertRaisesRegex(RuntimeError, "forced failure"):
                install_launch_agents(test_cfg(vault), home=home, command_runner=runner)

            self.assertEqual(current_path.read_bytes(), original)

    def test_first_print_failure_boots_out_attempted_job_and_restores_files(self):
        self._assert_print_failure_rolls_back(NEW_LAUNCHD_LABELS["harvest"])

    def test_second_print_failure_boots_out_attempted_job_and_restores_files(self):
        self._assert_print_failure_rolls_back(NEW_LAUNCHD_LABELS["weekly"])

    def test_first_legacy_bootout_failure_keeps_verified_replacements(self):
        self._assert_legacy_bootout_failure(LEGACY_LAUNCHD_LABELS["harvest"])

    def test_second_legacy_bootout_failure_keeps_verified_replacements(self):
        self._assert_legacy_bootout_failure(LEGACY_LAUNCHD_LABELS["weekly"])

    def test_first_legacy_unlink_failure_keeps_verified_replacements(self):
        self._assert_legacy_unlink_failure(LEGACY_LAUNCHD_LABELS["harvest"])

    def test_second_legacy_unlink_failure_keeps_verified_replacements(self):
        self._assert_legacy_unlink_failure(LEGACY_LAUNCHD_LABELS["weekly"])

    def test_program_validation_failure_has_no_mutations(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            runner = RecordingRunner(fail_on="--help")
            home_before = tree_snapshot(home)
            vault_before = tree_snapshot(vault)

            with patch("install_launchd.initialize_harvest_baseline") as initialize:
                with self.assertRaisesRegex(RuntimeError, "program validation failed"):
                    install_launch_agents(test_cfg(vault), home=home, command_runner=runner)

            self.assertEqual(tree_snapshot(home), home_before)
            self.assertEqual(tree_snapshot(vault), vault_before)
            self.assertEqual(len(runner.calls), 1)
            initialize.assert_not_called()

    def test_write_failure_restores_current_plists_without_runner_calls_when_not_loading(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            current = write_current_plists(home)
            originals = {kind: path.read_bytes() for kind, path in current.items()}
            runner = RecordingRunner()

            def fail_weekly(path, payload):
                if path == current["weekly"]:
                    raise OSError("forced write failure")
                path.write_bytes(b"staged harvest")

            with patch("install_launchd.write_plist_atomic", side_effect=fail_weekly):
                with self.assertRaisesRegex(OSError, "forced write failure"):
                    install_launch_agents(
                        test_cfg(vault),
                        home=home,
                        load=False,
                        command_runner=runner,
                    )

            self.assertEqual(
                {kind: path.read_bytes() for kind, path in current.items()},
                originals,
            )
            self.assertEqual(runner.calls, [])

    def test_runner_exception_during_rollback_still_restores_every_current_plist(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            create_legacy_plists(home)
            current = write_current_plists(home)
            originals = {kind: path.read_bytes() for kind, path in current.items()}
            bootstrap_failed = False

            def raise_on(command, calls):
                nonlocal bootstrap_failed
                if "bootstrap" in command and NEW_LAUNCHD_LABELS["harvest"] in command:
                    bootstrap_failed = True
                    return True
                return (
                    bootstrap_failed
                    and "bootout" in command
                    and NEW_LAUNCHD_LABELS["harvest"] in command
                )

            runner = RecordingRunner(raise_on=raise_on)

            with self.assertRaisesRegex(RuntimeError, "rollback incomplete") as raised:
                install_launch_agents(test_cfg(vault), home=home, command_runner=runner)

            self.assertIn("forced runner timeout", str(raised.exception))
            self.assertIn("rollback unload harvest", str(raised.exception))
            self.assertEqual(
                {kind: path.read_bytes() for kind, path in current.items()},
                originals,
            )

    def test_dry_run_leaves_tree_baseline_and_runner_unchanged(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            create_legacy_plists(home)
            runner = RecordingRunner()
            home_before = tree_snapshot(home)
            vault_before = tree_snapshot(vault)

            with patch("install_launchd.initialize_harvest_baseline") as initialize:
                actions = install_launch_agents(
                    test_cfg(vault),
                    home=home,
                    dry_run=True,
                    command_runner=runner,
                )

            self.assertEqual(tree_snapshot(home), home_before)
            self.assertEqual(tree_snapshot(vault), vault_before)
            self.assertEqual(runner.calls, [])
            initialize.assert_not_called()
            self.assertTrue(any("would remove legacy" in action for action in actions))

    def test_missing_legacy_service_result_allows_plist_removal(self):
        with tempfile.TemporaryDirectory() as home:
            legacy_path = job_paths(home, "harvest")[1]
            legacy_path.parent.mkdir(parents=True)
            legacy_path.write_bytes(b"legacy")
            runner = RecordingRunner(
                fail_on="bootout",
                failure_stderr="Could not find service",
            )

            action = remove_legacy_job(
                legacy_path,
                LEGACY_LAUNCHD_LABELS["harvest"],
                runner,
            )

            self.assertEqual(action, f"REMOVED legacy {LEGACY_LAUNCHD_LABELS['harvest']}")
            self.assertFalse(legacy_path.exists())

    def test_legacy_removal_refuses_regular_parent_swap_during_bootout(self):
        with tempfile.TemporaryDirectory() as home:
            legacy_path = job_paths(home, "harvest")[1]
            legacy_path.parent.mkdir(parents=True)
            legacy_path.write_bytes(b"legacy")
            held_parent = legacy_path.parent.with_name("LaunchAgents-held")
            replacement_target = legacy_path

            swapped = False

            def runner(_args, **_kwargs):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    legacy_path.parent.rename(held_parent)
                    legacy_path.parent.mkdir()
                    replacement_target.write_bytes(b"outside sentinel")
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with self.assertRaises(OSError):
                remove_legacy_job(
                    legacy_path,
                    LEGACY_LAUNCHD_LABELS["harvest"],
                    runner,
                )

            self.assertEqual(replacement_target.read_bytes(), b"outside sentinel")
            self.assertEqual(
                (held_parent / legacy_path.name).read_bytes(),
                b"legacy",
            )

    def test_unrelated_not_found_legacy_bootout_retains_plist_and_raises(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            legacy_paths = create_legacy_plists(home)
            failed_label = LEGACY_LAUNCHD_LABELS["harvest"]

            def fail_selected_legacy(command, calls):
                return "bootout" in command and failed_label in command

            runner = RecordingRunner(
                fail_on=fail_selected_legacy,
                failure_stderr="configuration path not found",
            )
            with self.assertRaisesRegex(RuntimeError, "legacy cleanup failed"):
                install_launch_agents(test_cfg(vault), home=home, command_runner=runner)

            failed_path = Path(home, "Library", "LaunchAgents", f"{failed_label}.plist")
            self.assertTrue(failed_path.exists())
            self.assertTrue(any(path.exists() for path in legacy_paths))

    def test_rollback_unload_uses_label_fallback_and_reports_incomplete_rollback(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            create_legacy_plists(home)
            current = write_current_plists(home)
            originals = {kind: path.read_bytes() for kind, path in current.items()}
            installation_failed = False
            weekly_label = NEW_LAUNCHD_LABELS["weekly"]

            def fail_install_then_weekly_unload(command, calls):
                nonlocal installation_failed
                if not installation_failed and "bootstrap" in command and weekly_label in command:
                    installation_failed = True
                    return True
                return (
                    installation_failed
                    and "bootout" in command
                    and weekly_label in command
                )

            runner = RecordingRunner(fail_on=fail_install_then_weekly_unload)
            with self.assertRaisesRegex(RuntimeError, "rollback incomplete") as raised:
                install_launch_agents(test_cfg(vault), home=home, command_runner=runner)

            calls = [" ".join(call) for call in runner.calls]
            label_target = f"gui/{os.getuid()}/{weekly_label}"
            self.assertTrue(any(call.endswith(label_target) for call in calls))
            self.assertIn("launchctl bootstrap failed", str(raised.exception))
            self.assertIn("rollback unload weekly", str(raised.exception))
            self.assertEqual(
                {kind: path.read_bytes() for kind, path in current.items()},
                originals,
            )

    def test_first_restore_failure_is_reported_after_second_restore_succeeds(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            create_legacy_plists(home)
            current = write_current_plists(home)
            originals = {kind: path.read_bytes() for kind, path in current.items()}
            weekly_original = current["weekly"].read_bytes()
            original_atomic_write = install_launchd.durable_atomic_write

            def fail_harvest_restore(path, data, **kwargs):
                if Path(path) == current["harvest"] and data == originals["harvest"]:
                    raise OSError("forced harvest restore write failure")
                return original_atomic_write(path, data, **kwargs)

            runner = RecordingRunner(
                fail_on=fail_first_post_bootstrap_print(
                    NEW_LAUNCHD_LABELS["harvest"]
                ),
            )
            with patch(
                "install_launchd.durable_atomic_write",
                side_effect=fail_harvest_restore,
            ):
                with self.assertRaisesRegex(RuntimeError, "rollback incomplete") as raised:
                    install_launch_agents(test_cfg(vault), home=home, command_runner=runner)

            self.assertIn("launchctl print failed", str(raised.exception))
            self.assertIn("restore harvest", str(raised.exception))
            self.assertEqual(current["weekly"].read_bytes(), weekly_original)

    def test_restore_reload_failure_is_reported_with_original_installation_failure(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            create_legacy_plists(home)
            current = write_current_plists(home)
            weekly_label = NEW_LAUNCHD_LABELS["weekly"]
            harvest_label = NEW_LAUNCHD_LABELS["harvest"]
            print_failed = False
            reload_failed = False

            def fail_print_then_harvest_reload(command, calls):
                nonlocal print_failed, reload_failed
                weekly_bootstrapped = any(
                    "bootstrap" in " ".join(call)
                    and weekly_label in " ".join(call)
                    for call in calls[:-1]
                )
                if (
                    not print_failed
                    and weekly_bootstrapped
                    and "print gui/" in command
                    and weekly_label in command
                ):
                    print_failed = True
                    return True
                if print_failed and not reload_failed and "bootstrap" in command and harvest_label in command:
                    reload_failed = True
                    return True
                return False

            runner = RecordingRunner(fail_on=fail_print_then_harvest_reload)
            with self.assertRaisesRegex(RuntimeError, "rollback incomplete") as raised:
                install_launch_agents(test_cfg(vault), home=home, command_runner=runner)

            self.assertIn("launchctl print failed", str(raised.exception))
            self.assertIn("reload harvest", str(raised.exception))
            self.assertTrue(current["weekly"].exists())

    def test_staging_publish_failure_removes_random_tmp_and_restores_current_plists(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            current = write_current_plists(home)
            originals = {kind: path.read_bytes() for kind, path in current.items()}
            original_replace = os.replace
            failed = False

            def fail_staging_replace(source, destination, *args, **kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise OSError("forced staging replace failure")
                return original_replace(source, destination, *args, **kwargs)

            with patch("safety.os.replace", side_effect=fail_staging_replace):
                with self.assertRaisesRegex(OSError, "forced staging replace failure"):
                    install_launch_agents(test_cfg(vault), home=home, load=False)

            self.assertEqual(
                {kind: path.read_bytes() for kind, path in current.items()},
                originals,
            )
            for path in current.values():
                self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_restore_write_failure_is_reported_and_restores_later_job(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            create_legacy_plists(home)
            current = write_current_plists(home)
            originals = {kind: path.read_bytes() for kind, path in current.items()}
            weekly_original = current["weekly"].read_bytes()
            original_atomic_write = install_launchd.durable_atomic_write

            def fail_harvest_restore(path, data, **kwargs):
                if Path(path) == current["harvest"] and data == originals["harvest"]:
                    raise OSError("forced harvest restore publication failure")
                return original_atomic_write(path, data, **kwargs)

            runner = RecordingRunner(
                fail_on=fail_first_post_bootstrap_print(
                    NEW_LAUNCHD_LABELS["harvest"]
                ),
            )
            with patch(
                "install_launchd.durable_atomic_write",
                side_effect=fail_harvest_restore,
            ):
                with self.assertRaisesRegex(RuntimeError, "rollback incomplete") as raised:
                    install_launch_agents(test_cfg(vault), home=home, command_runner=runner)

            self.assertIn("restore harvest", str(raised.exception))
            self.assertIn("forced harvest restore publication failure", str(raised.exception))
            self.assertEqual(current["weekly"].read_bytes(), weekly_original)

    def test_staging_publish_failure_removes_random_tmp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "job.plist"

            with patch(
                "safety.os.replace",
                side_effect=OSError("forced staging replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "forced staging replace failure"):
                    write_plist_atomic(path, {"Label": "test"})

            self.assertFalse(path.exists())
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_restore_publish_failure_removes_random_tmp_and_reports_rollback(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            create_legacy_plists(home)
            current = write_current_plists(home)
            weekly_original = current["weekly"].read_bytes()
            original_replace = os.replace
            harvest_publishes = 0

            def fail_harvest_restore_replace(source, destination, *args, **kwargs):
                nonlocal harvest_publishes
                if destination == current["harvest"].name:
                    harvest_publishes += 1
                if destination == current["harvest"].name and harvest_publishes == 2:
                    raise OSError("forced harvest restore replace failure")
                return original_replace(source, destination, *args, **kwargs)

            runner = RecordingRunner(
                fail_on=fail_first_post_bootstrap_print(
                    NEW_LAUNCHD_LABELS["harvest"]
                ),
            )
            with patch(
                "safety.os.replace",
                side_effect=fail_harvest_restore_replace,
            ):
                with self.assertRaisesRegex(RuntimeError, "rollback incomplete") as raised:
                    install_launch_agents(test_cfg(vault), home=home, command_runner=runner)

            self.assertIn("forced harvest restore replace failure", str(raised.exception))
            self.assertEqual(
                list(current["harvest"].parent.glob(f".{current['harvest'].name}.*.tmp")),
                [],
            )
            self.assertEqual(current["weekly"].read_bytes(), weekly_original)

    def _assert_print_failure_rolls_back(self, label):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            legacy_paths = create_legacy_plists(home)
            current = {
                "harvest": job_paths(home, "harvest")[0],
                "weekly": job_paths(home, "weekly")[0],
            }
            original_harvest = b"old harvest" if label == NEW_LAUNCHD_LABELS["weekly"] else None
            if original_harvest is not None:
                current["harvest"].parent.mkdir(parents=True, exist_ok=True)
                current["harvest"].write_bytes(original_harvest)
            failed = False

            def fail_once(command, calls):
                nonlocal failed
                if not failed and "print gui/" in command and label in command:
                    failed = True
                    return True
                return False

            runner = RecordingRunner(fail_on=fail_once)

            with self.assertRaisesRegex(RuntimeError, "launchctl print failed"):
                install_launch_agents(test_cfg(vault), home=home, command_runner=runner)

            calls = [" ".join(call) for call in runner.calls]
            failed_print = next(
                index
                for index, call in enumerate(calls)
                if "print gui/" in call and label in call
            )
            bootstrap = max(
                index
                for index, call in enumerate(calls[:failed_print])
                if "bootstrap" in call and label in call
            )
            bootout = next(
                index
                for index, call in enumerate(calls[failed_print + 1 :], failed_print + 1)
                if "bootout" in call and f"{label}.plist" in call
            )
            self.assertLess(bootstrap, failed_print)
            self.assertLess(failed_print, bootout)
            self.assertTrue(all(path.exists() for path in legacy_paths))
            if original_harvest is None:
                self.assertFalse(any(path.exists() for path in current.values()))
            else:
                self.assertEqual(current["harvest"].read_bytes(), original_harvest)
                self.assertFalse(current["weekly"].exists())

    def _assert_legacy_bootout_failure(self, failed_label):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            legacy_paths = create_legacy_plists(home)

            def fail_only_selected_legacy(command, calls):
                return "bootout" in command and failed_label in command

            runner = RecordingRunner(fail_on=fail_only_selected_legacy)
            with self.assertRaisesRegex(RuntimeError, "legacy cleanup failed"):
                install_launch_agents(test_cfg(vault), home=home, command_runner=runner)

            calls = [" ".join(call) for call in runner.calls]
            legacy_start = next(
                index for index, call in enumerate(calls) if "bootout" in call and failed_label in call
            )
            self.assertTrue(job_paths(home, "harvest")[0].exists())
            self.assertTrue(job_paths(home, "weekly")[0].exists())
            self.assertTrue(Path(home, "Library", "LaunchAgents", f"{failed_label}.plist").exists())
            self.assertFalse(
                any(
                    "bootout" in call and "io.agent-memory-beacon" in call
                    for call in calls[legacy_start:]
                )
            )
            self.assertTrue(any(path.exists() for path in legacy_paths))

    def _assert_legacy_unlink_failure(self, failed_label):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as vault:
            legacy_paths = create_legacy_plists(home)
            failed_path = Path(home, "Library", "LaunchAgents", f"{failed_label}.plist")
            original_unlink = install_launchd.durable_unlink

            def fail_selected_unlink(path, *args, **kwargs):
                if Path(path) == failed_path:
                    raise OSError("forced unlink failure")
                return original_unlink(path, *args, **kwargs)

            runner = RecordingRunner()
            with patch(
                "install_launchd.durable_unlink",
                side_effect=fail_selected_unlink,
            ):
                with self.assertRaisesRegex(RuntimeError, "legacy cleanup failed"):
                    install_launch_agents(test_cfg(vault), home=home, command_runner=runner)

            calls = [" ".join(call) for call in runner.calls]
            legacy_start = next(
                index for index, call in enumerate(calls) if "bootout" in call and failed_label in call
            )
            self.assertTrue(job_paths(home, "harvest")[0].exists())
            self.assertTrue(job_paths(home, "weekly")[0].exists())
            self.assertTrue(failed_path.exists())
            self.assertFalse(
                any(
                    "bootout" in call and "io.agent-memory-beacon" in call
                    for call in calls[legacy_start:]
                )
            )
            self.assertTrue(any(path.exists() for path in legacy_paths))


def test_cfg(vault):
    return {
        "vault_path": vault,
        "python_path": sys.executable,
        "transcript_agents": [],
        "transcript_paths": [],
        "harvest_interval_seconds": 300,
        "scan": {"day": "SUN", "hour": 15, "minute": 0},
    }


if __name__ == "__main__":
    unittest.main()
