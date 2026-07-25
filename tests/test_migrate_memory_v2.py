import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import migrate_memory_v2


class MigrateMemoryV2CliTests(unittest.TestCase):
    def test_vault_override_rebases_vault_owned_paths_for_rebuild(self):
        with tempfile.TemporaryDirectory() as root:
            configured = os.path.join(root, "configured")
            selected = os.path.join(root, "selected")
            os.makedirs(configured)
            os.makedirs(selected)
            selected_resolved = os.path.realpath(selected)
            cfg = {
                "vault_path": configured,
                "agent_memory_path": os.path.join(configured, "05-Agent-Memory"),
                "memory_index_path": os.path.join(
                    configured,
                    "00-Inbox",
                    "Agent Memory Index.md",
                ),
                "codex_profile_path": os.path.join(
                    configured,
                    "05-Agent-Memory",
                    "codex-profile",
                ),
                "log_dir": os.path.join(configured, "04-Feedback", "_logs"),
                "context_targets": [os.path.join(root, "live", "AGENTS.md")],
                "claude_md_path": os.path.join(root, "live", "CLAUDE.md"),
            }
            plan = object()

            with (
                patch.object(sys, "argv", ["migrate_memory_v2.py", "--vault", selected, "--apply"]),
                patch.object(migrate_memory_v2, "load_config", return_value=cfg),
                patch.object(migrate_memory_v2, "build_migration_plan", return_value=plan),
                patch.object(migrate_memory_v2, "apply_migration", return_value={"status": "applied"}),
                patch.object(migrate_memory_v2, "rebuild_memory_index") as rebuild,
                patch.object(migrate_memory_v2, "compile_agent_context") as compile_context,
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(migrate_memory_v2.main(), 0)

            rebuilt_cfg = rebuild.call_args.args[0]
            compiled_cfg = compile_context.call_args.args[0]
            rebuild.assert_called_once_with(
                rebuilt_cfg,
                repair_generated=False,
            )
            self.assertIs(rebuilt_cfg, compiled_cfg)
            self.assertEqual(rebuilt_cfg["vault_path"], selected_resolved)
            self.assertEqual(
                rebuilt_cfg["agent_memory_path"],
                os.path.join(selected_resolved, "05-Agent-Memory"),
            )
            self.assertEqual(
                rebuilt_cfg["memory_index_path"],
                os.path.join(selected_resolved, "00-Inbox", "Agent Memory Index.md"),
            )
            self.assertEqual(
                rebuilt_cfg["codex_profile_path"],
                os.path.join(selected_resolved, "05-Agent-Memory", "codex-profile"),
            )
            self.assertEqual(
                rebuilt_cfg["log_dir"],
                os.path.join(selected_resolved, "04-Feedback", "_logs"),
            )
            self.assertEqual(rebuilt_cfg["context_targets"], [])
            self.assertEqual(rebuilt_cfg["claude_md_path"], "")

    def test_configured_vault_keeps_external_context_targets(self):
        with tempfile.TemporaryDirectory() as root:
            configured = os.path.join(root, "configured")
            os.makedirs(configured)
            target = os.path.join(root, "live", "AGENTS.md")
            cfg = {
                "vault_path": configured,
                "context_targets": [target],
                "claude_md_path": target,
            }

            updated = migrate_memory_v2.config_for_vault(cfg, configured)

            self.assertEqual(updated["context_targets"], [target])
            self.assertEqual(updated["claude_md_path"], target)


if __name__ == "__main__":
    unittest.main()
