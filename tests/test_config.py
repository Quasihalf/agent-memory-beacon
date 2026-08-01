import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import config


class ConversationSummaryConfigTests(unittest.TestCase):
    def test_conversation_summary_defaults_are_loaded(self):
        with self.config_fixture({}) as config_path:
            with patch.object(config, "CONFIG_PATH", str(config_path)):
                settings = config.load_config()["conversation_summary"]

        self.assertEqual(
            settings,
            {
                "enabled": True,
                "min_substantive_messages": 5,
                "message_interval": 10,
                "stale_after_minutes": 30,
                "retry_interval_messages": 2,
                "max_summary_bytes": 4096,
                "max_recall": 1,
                "token_budget": 400,
            },
        )

    def test_conversation_summary_rejects_invalid_settings(self):
        invalid_settings = (
            {"min_substantive_messages": True},
            {"message_interval": 0},
            {"stale_after_minutes": -1},
            {"retry_interval_messages": 0},
            {"message_interval": 2, "retry_interval_messages": 3},
            {"max_summary_bytes": 4097},
            {"token_budget": 401},
            {"unexpected": 1},
        )
        for settings in invalid_settings:
            with self.subTest(settings=settings), self.config_fixture(settings) as config_path:
                with patch.object(config, "CONFIG_PATH", str(config_path)):
                    with self.assertRaises((TypeError, ValueError)):
                        config.load_config()

    def config_fixture(self, conversation_summary):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        vault = root / "vault"
        sessions = root / "sessions"
        vault.mkdir()
        sessions.mkdir()
        config_path = root / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "vault_path": str(vault),
                    "python_path": sys.executable,
                    "codex_sessions_path": str(sessions),
                    "transcript_agents": ["codex"],
                    "conversation_summary": conversation_summary,
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return _TemporaryConfig(temporary, config_path)


class BeaconSyncConfigTests(unittest.TestCase):
    def test_beacon_sync_defaults_are_disabled_and_paths_stay_inactive(self):
        with self.config_fixture({}) as config_path:
            with patch.object(config, "CONFIG_PATH", str(config_path)):
                settings = config.load_config()["beacon_sync"]

        self.assertFalse(settings["enabled"])
        self.assertEqual(settings["role"], "")
        for key in (
            "state_dir",
            "outbox_dir",
            "published_dir",
            "replica_path",
            "received_published_dir",
        ):
            self.assertEqual(settings[key], "")
        self.assertEqual(settings["inboxes"], [])
        self.assertEqual(settings["attachment_roots"], [])

    def test_portable_loader_expands_both_environment_styles(self):
        with self.config_fixture(
            {
                "enabled": True,
                "role": "producer-replica",
                "device_id": "windows-gpu",
                "state_dir": "${SYNC_ROOT}/state",
                "outbox_dir": "%SYNC_ROOT%/outbox",
                "received_published_dir": "%SYNC_ROOT%/published",
                "replica_path": "${SYNC_ROOT}/vault",
                "attachment_roots": [
                    "%USERPROFILE%/Downloads",
                    "${SYNC_ROOT}/attachments",
                ],
            },
            codex_sessions_path="%SYNC_ROOT%/codex",
            claude_project_path="${SYNC_ROOT}/claude",
        ) as config_path:
            loaded = config.load_beacon_sync_config(
                str(config_path),
                environ={"SYNC_ROOT": "C:/Beacon", "USERPROFILE": "C:/Users/demo"},
            )

        self.assertEqual(loaded["state_dir"], "C:/Beacon/state")
        self.assertEqual(loaded["outbox_dir"], "C:/Beacon/outbox")
        self.assertEqual(
            loaded["transcript_paths"],
            ["C:/Beacon/codex", "C:/Beacon/claude"],
        )
        self.assertEqual(
            loaded["attachment_roots"],
            ["C:/Users/demo/Downloads", "C:/Beacon/attachments"],
        )

    def test_authority_inbox_binding_drops_empty_and_validates_device(self):
        settings = {
            "enabled": True,
            "role": "authority",
            "state_dir": "/sync/state",
            "published_dir": "/sync/published",
            "inboxes": [
                {"device_id": "windows-one", "path": "/sync/inbox"},
                {"device_id": "not-active", "path": ""},
            ],
        }
        with self.config_fixture(settings) as config_path:
            loaded = config.load_beacon_sync_config(str(config_path))
        self.assertEqual(
            loaded["inboxes"],
            [{"device_id": "windows-one", "path": "/sync/inbox"}],
        )

        settings["inboxes"][0]["device_id"] = "../unsafe"
        with self.config_fixture(settings) as config_path:
            with self.assertRaisesRegex(ValueError, "device_id"):
                config.load_beacon_sync_config(str(config_path))

    def test_enabled_role_and_limits_are_strict(self):
        invalid = (
            {"enabled": True, "role": "writer"},
            {"enabled": "yes"},
            {"enabled": True, "role": "producer-replica", "max_chunk_bytes": 0},
            {
                "enabled": False,
                "max_attachment_bytes": 33,
                "max_object_bytes": 32,
            },
            {
                "enabled": False,
                "max_attachment_bytes": 33,
                "max_object_bytes": 64,
                "max_replica_object_bytes": 32,
                "max_chunk_bytes": 32,
                "max_gap_bytes": 64,
            },
            {"enabled": False, "attachment_roots": "C:/Users/demo/Downloads"},
            {"enabled": False, "attachment_roots": ["relative/attachments"]},
            {"enabled": False, "unexpected": True},
        )
        for settings in invalid:
            with self.subTest(settings=settings), self.config_fixture(
                settings
            ) as config_path:
                with self.assertRaises((TypeError, ValueError)):
                    config.load_beacon_sync_config(str(config_path))

    def test_device_ids_are_ascii_protocol_identifiers(self):
        settings = {
            "enabled": True,
            "role": "producer-replica",
            "device_id": "设备-one",
            "state_dir": "/sync/state",
            "outbox_dir": "/sync/outbox",
            "received_published_dir": "/sync/published",
            "replica_path": "/sync/replica",
        }

        with self.config_fixture(settings) as config_path:
            with self.assertRaisesRegex(ValueError, "device_id"):
                config.load_beacon_sync_config(str(config_path))

    def test_sync_paths_reject_relative_and_unresolved_environment_values(self):
        invalid_paths = (
            "relative/state",
            "$MISSING/state",
            "${MISSING}/state",
            "%MISSING%/state",
            "~/state",
        )
        for value in invalid_paths:
            with self.subTest(path=value):
                settings = {
                    "enabled": True,
                    "role": "authority",
                    "state_dir": value,
                    "published_dir": "/sync/published",
                }
                with self.config_fixture(settings) as config_path:
                    with self.assertRaisesRegex(
                        ValueError,
                        "absolute|unresolved",
                    ):
                        config.load_beacon_sync_config(
                            str(config_path),
                            environ={"HOME": "", "USERPROFILE": ""},
                        )

    def test_sync_paths_reject_equal_or_nested_storage_roots(self):
        root_names = (
            "state_dir",
            "outbox_dir",
            "published_dir",
            "received_published_dir",
            "replica_path",
            "inbox",
            "vault",
            "attachment_a",
            "attachment_b",
        )
        for left_index, left_name in enumerate(root_names):
            for right_name in root_names[left_index + 1 :]:
                with self.subTest(left=left_name, right=right_name):
                    roots = {
                        "state_dir": "/sync/state",
                        "outbox_dir": "/sync/outbox",
                        "published_dir": "/sync/published",
                        "received_published_dir": "/sync/received",
                        "replica_path": "/sync/replica",
                        "inbox": "/sync/inbox",
                        "vault": "/sync/vault",
                        "attachment_a": "/source/attachments-a",
                        "attachment_b": "/source/attachments-b",
                    }
                    roots[right_name] = roots[left_name] + "/nested"
                    settings = {
                        "enabled": True,
                        "role": "authority",
                        "state_dir": roots["state_dir"],
                        "outbox_dir": roots["outbox_dir"],
                        "published_dir": roots["published_dir"],
                        "received_published_dir": roots["received_published_dir"],
                        "replica_path": roots["replica_path"],
                        "inboxes": [
                            {
                                "device_id": "windows-one",
                                "path": roots["inbox"],
                            }
                        ],
                        "attachment_roots": [
                            roots["attachment_a"],
                            roots["attachment_b"],
                        ],
                    }
                    with self.config_fixture(
                        settings,
                        vault_path=roots["vault"],
                    ) as config_path:
                        with self.assertRaisesRegex(ValueError, "overlap"):
                            config.load_beacon_sync_config(str(config_path))

    def test_windows_sync_path_overlap_is_case_insensitive(self):
        settings = {
            "enabled": True,
            "role": "producer-replica",
            "device_id": "windows-one",
            "state_dir": "C:/Beacon/State",
            "outbox_dir": "c:\\beacon\\state\\outbox",
            "received_published_dir": "D:/Beacon/Published",
            "replica_path": "D:/Beacon/Replica",
        }
        with self.config_fixture(
            settings,
            vault_path="E:/Canonical/Vault",
        ) as config_path:
            with self.assertRaisesRegex(ValueError, "overlap"):
                config.load_beacon_sync_config(str(config_path))

    def test_distinct_absolute_sync_siblings_are_allowed(self):
        settings = {
            "enabled": True,
            "role": "authority",
            "state_dir": "/sync/state",
            "outbox_dir": "/sync/state-old",
            "published_dir": "/sync/published",
            "received_published_dir": "/sync/received",
            "replica_path": "/sync/replica",
            "inboxes": [
                {"device_id": "windows-one", "path": "/sync/inbox"},
            ],
        }
        with self.config_fixture(
            settings,
            vault_path="/sync/vault",
        ) as config_path:
            loaded = config.load_beacon_sync_config(str(config_path))

        self.assertEqual(loaded["state_dir"], "/sync/state")

    def test_existing_symlink_cannot_hide_sync_path_inside_vault(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            vault = root / "vault"
            nested_state = vault / "sync-state"
            vault.mkdir()
            nested_state.mkdir()
            state_alias = root / "state-alias"
            state_alias.symlink_to(nested_state, target_is_directory=True)
            settings = {
                "enabled": True,
                "role": "authority",
                "state_dir": str(state_alias),
                "published_dir": str(root / "published"),
            }

            with self.config_fixture(
                settings,
                vault_path=str(vault),
            ) as config_path:
                with self.assertRaisesRegex(ValueError, "overlap"):
                    config.load_beacon_sync_config(str(config_path))

    def config_fixture(
        self,
        beacon_sync,
        *,
        codex_sessions_path=None,
        claude_project_path="",
        vault_path=None,
    ):
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        vault = Path(vault_path) if vault_path is not None else root / "vault"
        sessions = root / "sessions"
        if vault_path is None:
            vault.mkdir()
        sessions.mkdir()
        config_path = root / "config.yaml"
        config_path.write_text(
            yaml.safe_dump(
                {
                    "vault_path": str(vault),
                    "python_path": sys.executable,
                    "codex_sessions_path": (
                        codex_sessions_path
                        if codex_sessions_path is not None
                        else str(sessions)
                    ),
                    "claude_project_path": claude_project_path,
                    "transcript_agents": ["codex"],
                    "beacon_sync": beacon_sync,
                },
                allow_unicode=True,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        return _TemporaryConfig(temporary, config_path)


class _TemporaryConfig:
    def __init__(self, temporary, config_path):
        self.temporary = temporary
        self.config_path = config_path

    def __enter__(self):
        return self.config_path

    def __exit__(self, exc_type, exc_value, traceback):
        self.temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
