import contextlib
import io
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from runner import acquire_lock, release_lock, results_have_errors
import runner


class RunnerTests(unittest.TestCase):
    def test_result_errors_include_nested_module_error_fields(self):
        self.assertTrue(results_have_errors({"backup": {"error": "failed"}}))
        self.assertTrue(
            results_have_errors({"compile": {"memory_error": "failed"}})
        )
        self.assertFalse(results_have_errors({"backup": {"new_sessions": 1}}))

    def test_scanner_lock_allows_only_one_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = os.path.join(tmp, "scanner.lock")

            self.assertTrue(acquire_lock(lock_path))
            self.assertFalse(acquire_lock(lock_path))
            release_lock(lock_path)
            self.assertTrue(acquire_lock(lock_path))
            release_lock(lock_path)

    def test_regular_weekly_scan_stays_incremental(self):
        now = datetime(2026, 7, 18, 12, 0, 0)
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat = self._write_heartbeat(tmp, now - timedelta(days=7))

            self.assertEqual(
                runner.resolve_scan_mode(heartbeat, requested_full=False, now=now),
                (False, 0),
            )

    def test_weekly_jitter_under_eight_complete_days_stays_incremental(self):
        now = datetime(2026, 7, 18, 12, 0, 0)
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat = self._write_heartbeat(
                tmp,
                now - timedelta(days=7, hours=23),
            )

            self.assertEqual(
                runner.resolve_scan_mode(heartbeat, requested_full=False, now=now),
                (False, 0),
            )

    def test_eight_complete_days_forces_full_catch_up(self):
        now = datetime(2026, 7, 18, 12, 0, 0)
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat = self._write_heartbeat(tmp, now - timedelta(days=8))

            self.assertEqual(
                runner.resolve_scan_mode(heartbeat, requested_full=False, now=now),
                (True, 1),
            )

    def test_missed_week_forces_full_catch_up(self):
        now = datetime(2026, 7, 18, 12, 0, 0)
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat = self._write_heartbeat(tmp, now - timedelta(days=15))

            self.assertEqual(
                runner.resolve_scan_mode(heartbeat, requested_full=False, now=now),
                (True, 2),
            )

    def test_explicit_full_scan_does_not_depend_on_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            heartbeat = os.path.join(tmp, "missing-heartbeat.md")

            self.assertEqual(
                runner.resolve_scan_mode(heartbeat, requested_full=True),
                (True, 0),
            )

    def test_missing_and_malformed_heartbeat_fail_open_to_incremental(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing-heartbeat.md")
            malformed = os.path.join(tmp, "malformed-heartbeat.md")
            with open(malformed, "w", encoding="utf-8") as handle:
                handle.write("---\nlast_scan: [unterminated\n---\n")

            self.assertEqual(runner.resolve_scan_mode(missing), (False, 0))
            self.assertEqual(runner.resolve_scan_mode(malformed), (False, 0))

    def test_yaml_timezone_datetime_can_trigger_full_catch_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            last_scan = datetime.now().astimezone() - timedelta(days=15)
            heartbeat = self._write_heartbeat(tmp, last_scan, quote=False)

            self.assertEqual(runner.resolve_scan_mode(heartbeat), (True, 2))

    def test_zulu_timestamp_string_can_trigger_full_catch_up(self):
        with tempfile.TemporaryDirectory() as tmp:
            last_scan = datetime.now(timezone.utc) - timedelta(days=15)
            heartbeat = self._write_heartbeat(
                tmp,
                last_scan.isoformat().replace("+00:00", "Z"),
            )

            self.assertEqual(runner.resolve_scan_mode(heartbeat), (True, 2))

    def test_main_routes_incremental_full_and_catch_up_modes_to_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            recent = datetime.now() - timedelta(days=7)
            calls = self._run_pipeline(tmp, last_scan=recent)
            self.assertFalse(calls["backup"]["full"])
            self.assertFalse(calls["analyze"]["full"])
            self.assertEqual(calls["report"]["missed_weeks"], 0)

        with tempfile.TemporaryDirectory() as tmp:
            calls = self._run_pipeline(tmp, cli_args=("--full",))
            self.assertTrue(calls["backup"]["full"])
            self.assertTrue(calls["analyze"]["full"])
            self.assertEqual(calls["report"]["missed_weeks"], 0)

        with tempfile.TemporaryDirectory() as tmp:
            stale = datetime.now() - timedelta(days=15)
            calls = self._run_pipeline(tmp, last_scan=stale)
            self.assertTrue(calls["backup"]["full"])
            self.assertTrue(calls["analyze"]["full"])
            self.assertEqual(calls["report"]["missed_weeks"], 2)

    def _run_pipeline(self, root, cli_args=(), last_scan=None):
        vault = os.path.join(root, "vault")
        feedback = os.path.join(vault, "04-Feedback")
        os.makedirs(feedback)
        if last_scan is not None:
            self._write_heartbeat(feedback, last_scan)
        calls = {}
        fake_modules = {}

        for module_name, step_name in (
            ("backup", "backup"),
            ("analyzer", "analyze"),
            ("maintainer", "maintain"),
            ("reporter", "report"),
            ("compiler", "compile"),
        ):
            module = types.ModuleType(module_name)

            def run(_cfg, _step=step_name, **kwargs):
                calls[_step] = kwargs
                return {}

            module.run = run
            fake_modules[module_name] = module

        cfg = {
            "vault_path": vault,
            "log_dir": os.path.join(feedback, "_logs"),
        }
        with patch.object(runner, "load_config", return_value=cfg), patch.object(
            sys,
            "argv",
            ["runner.py", *cli_args],
        ), patch.dict(sys.modules, fake_modules), contextlib.redirect_stdout(io.StringIO()):
            runner.main()
        return calls

    @staticmethod
    def _write_heartbeat(root, last_scan, quote=True):
        heartbeat = os.path.join(root, "heartbeat.md")
        value = last_scan.isoformat() if isinstance(last_scan, datetime) else str(last_scan)
        rendered = f"'{value}'" if quote else value
        with open(heartbeat, "w", encoding="utf-8") as handle:
            handle.write(
                "---\n"
                f"last_scan: {rendered}\n"
                "scan_status: ok\n"
                "---\n"
            )
        return heartbeat


if __name__ == "__main__":
    unittest.main()
