import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import beacon_sync


class BeaconSyncCliTests(unittest.TestCase):
    def test_disabled_config_allows_doctor_but_rejects_mutating_command(self):
        sync_cfg = {"enabled": False, "role": ""}

        doctor = beacon_sync.dispatch("doctor", sync_cfg)
        with self.assertRaisesRegex(beacon_sync.CliError, "disabled"):
            beacon_sync.dispatch("collect", sync_cfg)

        self.assertEqual(doctor["status"], "disabled")

    def test_doctor_reports_deep_health_failure_instead_of_configured_marker(self):
        sync_cfg = {
            "enabled": True,
            "role": "producer-replica",
            "device_id": "windows-test",
            "state_dir": "/missing/state",
            "outbox_dir": "/missing/outbox",
            "received_published_dir": "/missing/received",
            "replica_path": "/missing/replica",
        }

        result = beacon_sync.dispatch("doctor", sync_cfg)

        self.assertEqual(result["status"], "error")
        self.assertFalse(result["healthy"])
        self.assertIn("producer state", result["details"])

    def test_producer_run_materializes_before_receipt_authorized_gc(self):
        sync_cfg = {"enabled": True, "role": "producer-replica"}
        calls = []

        with (
            patch.object(
                beacon_sync,
                "_collect",
                side_effect=lambda cfg, include_existing=False: calls.append(
                    ("collect", include_existing)
                )
                or {"emitted": 1},
            ),
            patch.object(
                beacon_sync,
                "_gc",
                side_effect=lambda cfg: calls.append(("gc", False))
                or {"removed": 1},
            ),
            patch.object(
                beacon_sync,
                "_materialize",
                side_effect=lambda cfg, bootstrap=False: calls.append(
                    ("materialize", bootstrap)
                )
                or {"changed": True},
            ),
        ):
            result = beacon_sync.dispatch("run", sync_cfg)

        self.assertEqual(
            calls,
            [("collect", False), ("materialize", False), ("gc", False)],
        )
        self.assertEqual(result["collect"]["emitted"], 1)

    def test_materialize_bootstrap_is_explicit_and_not_used_by_background_run(self):
        sync_cfg = {"enabled": True, "role": "producer-replica"}
        calls = []

        with (
            patch.object(
                beacon_sync,
                "_collect",
                return_value={"emitted": 0},
            ),
            patch.object(
                beacon_sync,
                "_gc",
                return_value={"removed": 0},
            ),
            patch.object(
                beacon_sync,
                "_materialize",
                side_effect=lambda cfg, bootstrap=False: calls.append(bootstrap)
                or {"changed": True},
            ),
        ):
            beacon_sync.dispatch("materialize", sync_cfg, bootstrap=True)
            beacon_sync.dispatch("run", sync_cfg)

        self.assertEqual(calls, [True, False])

    def test_parser_exposes_bootstrap_only_for_manual_materialize(self):
        parser = beacon_sync.build_parser()

        materialize = parser.parse_args(["materialize", "--bootstrap"])
        self.assertTrue(materialize.bootstrap)
        with self.assertRaisesRegex(beacon_sync.CliError, "unrecognized arguments"):
            parser.parse_args(["run", "--bootstrap"])

    def test_authority_run_seals_before_receipts(self):
        sync_cfg = {"enabled": True, "role": "authority"}
        full_cfg = {"vault_path": "/vault"}
        calls = []
        generation = {
            "generation": 7,
            "generation_id": "generation-" + ("a" * 64),
            "snapshot_sha256": "b" * 64,
        }
        with (
            patch.object(beacon_sync.os, "name", "posix"),
            patch.object(beacon_sync.sys, "platform", "darwin"),
            patch.object(
                beacon_sync,
                "_reduce",
                side_effect=lambda cfg, sync: calls.append("reduce")
                or {"applied": 1},
            ),
            patch.object(
                beacon_sync,
                "_publish_generation",
                side_effect=lambda cfg, sync: calls.append("publish")
                or generation,
            ),
            patch.object(
                beacon_sync,
                "_publish_receipts",
                side_effect=lambda sync, sealed: calls.append(
                    ("receipts", sealed["generation"])
                )
                or {"published": 1},
            ),
        ):
            result = beacon_sync.dispatch(
                "run",
                sync_cfg,
                full_cfg=full_cfg,
            )

        self.assertEqual(calls, ["reduce", "publish", ("receipts", 7)])
        self.assertEqual(result["receipts"]["published"], 1)

    def test_main_initializes_producer_and_emits_machine_readable_json(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.yaml"
            config_path.write_text(
                yaml.safe_dump(
                    {
                        "beacon_sync": {
                            "enabled": True,
                            "role": "producer-replica",
                            "device_id": "windows-test",
                            "state_dir": str(root / "state"),
                            "outbox_dir": str(root / "outbox"),
                            "received_published_dir": str(root / "received"),
                            "replica_path": str(root / "replica"),
                        }
                    }
                ),
                encoding="utf-8",
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exit_code = beacon_sync.main(
                    ["--config", str(config_path), "init"]
                )

            payload = json.loads(output.getvalue())
            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["device_id"], "windows-test")
            self.assertTrue((root / "outbox" / "v1" / "identity.json").is_file())

    def test_role_mismatch_is_rejected_without_side_effects(self):
        sync_cfg = {"enabled": True, "role": "authority"}
        with self.assertRaisesRegex(beacon_sync.CliError, "role"):
            beacon_sync.dispatch("collect", sync_cfg, full_cfg={"vault_path": "/v"})

    def test_windows_rejects_authority_commands_before_side_effects(self):
        sync_cfg = {"enabled": True, "role": "authority"}
        full_cfg = {"vault_path": "/canonical"}
        calls = []

        with (
            patch.object(beacon_sync.os, "name", "nt"),
            patch.object(
                beacon_sync,
                "_reduce",
                side_effect=lambda *_args: calls.append("reduce"),
            ),
            patch.object(
                beacon_sync,
                "_publish_generation",
                side_effect=lambda *_args: calls.append("publish"),
            ),
            patch.object(
                beacon_sync,
                "_publish_receipts",
                side_effect=lambda *_args: calls.append("receipts"),
            ),
        ):
            for command in ("reduce", "publish", "run"):
                with self.subTest(command=command):
                    with self.assertRaisesRegex(
                        beacon_sync.CliError,
                        "Windows.*authority",
                    ):
                        beacon_sync.dispatch(
                            command,
                            sync_cfg,
                            full_cfg=full_cfg,
                        )

        self.assertEqual(calls, [])

    def test_non_macos_rejects_authority_side_effects(self):
        sync_cfg = {"enabled": True, "role": "authority"}
        with (
            patch.object(beacon_sync.os, "name", "posix"),
            patch.object(beacon_sync.sys, "platform", "linux"),
            patch.object(beacon_sync, "_reduce") as reduce,
        ):
            with self.assertRaisesRegex(
                beacon_sync.CliError,
                "macOS.*authority",
            ):
                beacon_sync.dispatch(
                    "reduce",
                    sync_cfg,
                    full_cfg={"vault_path": "/canonical"},
                )

        reduce.assert_not_called()

    def test_main_serializes_runtime_failures_without_traceback(self):
        stderr = io.StringIO()
        with (
            patch.object(
                beacon_sync,
                "load_beacon_sync_config",
                return_value={"enabled": True, "role": "producer-replica"},
            ),
            patch.object(
                beacon_sync,
                "_initialize",
                side_effect=RuntimeError("injected scheduler failure"),
            ),
            contextlib.redirect_stderr(stderr),
        ):
            exit_code = beacon_sync.main(["--config", "unused.yaml", "init"])

        payload = json.loads(stderr.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertEqual(payload["status"], "error")
        self.assertEqual(payload["error_type"], "RuntimeError")
        self.assertIn("injected scheduler failure", payload["message"])

    def test_main_serializes_database_and_argument_failures_as_json(self):
        for argv, side_effect in (
            (["--config", "unused.yaml", "init"], sqlite3.OperationalError("locked")),
            ([], None),
        ):
            with self.subTest(argv=argv):
                stderr = io.StringIO()
                patches = [
                    patch.object(
                        beacon_sync,
                        "load_beacon_sync_config",
                        return_value={
                            "enabled": True,
                            "role": "producer-replica",
                        },
                    )
                ]
                if side_effect is not None:
                    patches.append(
                        patch.object(
                            beacon_sync,
                            "_initialize",
                            side_effect=side_effect,
                        )
                    )
                with (
                    patches[0],
                    patches[1] if len(patches) > 1 else contextlib.nullcontext(),
                    contextlib.redirect_stderr(stderr),
                ):
                    exit_code = beacon_sync.main(argv)

                payload = json.loads(stderr.getvalue())
                self.assertEqual(exit_code, 2)
                self.assertEqual(payload["status"], "error")


if __name__ == "__main__":
    unittest.main()
