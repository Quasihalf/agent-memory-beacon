import os
import sys
import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import weekly_upstream_watch
import config as config_module
from branding import LEGACY_LAUNCHD_LABELS, LEGACY_PROJECT_SLUG, NEW_LAUNCHD_LABELS


PRODUCT_FILES = (
    "README.md",
    "SKILL.md",
    "references/architecture.md",
    "references/workflow.md",
    "scripts/config.example.yaml",
    "templates/vault/README.md",
    "templates/vault/用户手册.md",
)


class BrandingAuditTests(unittest.TestCase):
    def test_repository_agents_template_contains_no_compiled_user_memory(self):
        content = read_text(os.path.join(REPO_ROOT, "AGENTS.md"))

        for start, end in (
            (
                "<!-- COMPILED:RULES_START -->",
                "<!-- COMPILED:RULES_END -->",
            ),
            (
                "<!-- COMPILED:PROJECTS_START -->",
                "<!-- COMPILED:PROJECTS_END -->",
            ),
        ):
            compiled = content.split(start, 1)[1].split(end, 1)[0]
            self.assertEqual(compiled.strip(), "")

    def test_setup_and_manual_copy_use_current_operational_interfaces(self):
        setup_source = read_text(os.path.join(REPO_ROOT, "scripts", "setup.py"))
        self.assertIn(
            '"""Interactive setup script for Agent Memory Beacon.',
            setup_source,
        )
        self.assertIn('NEW_LAUNCHD_LABELS["harvest"]', setup_source)
        self.assertNotIn(LEGACY_LAUNCHD_LABELS["harvest"], setup_source)
        self.assertIn("install_runtime.py --verify-release", setup_source)
        self.assertIn("install_runtime.py", setup_source)
        self.assertNotIn('print(f"     python install_codex.py")', setup_source)
        self.assertNotIn('print(f"     python install_zcode.py")', setup_source)

        for relative in ("README.md", "templates/vault/用户手册.md"):
            with self.subTest(path=relative):
                content = read_text(os.path.join(REPO_ROOT, relative))
                self.assertNotIn("patches/CLAUDE.md.patch", content)
                self.assertNotIn("`CLAUDE.md.patch`", content)
                self.assertIn("patches/AGENT_MEMORY_BEACON.md.patch", content)

    def test_operational_examples_do_not_emit_legacy_project_slug(self):
        readme = read_text(os.path.join(REPO_ROOT, "README.md"))
        before_compat, remainder = readme.split("### 迁移兼容性", 1)
        compatibility, after_compat = remainder.split("### ZCode compatibility", 1)
        operational_readme = before_compat + after_compat

        self.assertIn(LEGACY_PROJECT_SLUG, compatibility)
        self.assertIn(LEGACY_LAUNCHD_LABELS["harvest"], compatibility)
        self.assertNotIn(LEGACY_PROJECT_SLUG, operational_readme)
        self.assertNotIn(
            LEGACY_PROJECT_SLUG,
            read_text(
                os.path.join(
                    REPO_ROOT,
                    "patches",
                    "AGENT_MEMORY_BEACON.md.patch",
                )
            ),
        )

    def test_product_files_use_current_name(self):
        for relative in PRODUCT_FILES:
            with self.subTest(path=relative):
                content = read_text(os.path.join(REPO_ROOT, relative))
                self.assertIn("Agent Memory Beacon", content)

    def test_readme_documents_legacy_compatibility_without_old_product_title(self):
        content = read_text(os.path.join(REPO_ROOT, "README.md"))
        self.assertTrue(content.startswith("# Agent Memory Beacon v0.5.0\n"))
        self.assertIn("~/AgentMemoryBeacon", content)
        self.assertIn("existing `~/ObsidianBrain`", content)
        self.assertIn("Tubo2333/obsidian-knowledge-brain", content)
        self.assertIn("Quasihalf/agent-memory-beacon", content)
        self.assertNotIn("# Agent Memory Vault", content)

    def test_skill_manifest_uses_current_product_contract(self):
        content = read_text(os.path.join(REPO_ROOT, "SKILL.md"))
        self.assertIn("name: agent-memory-beacon", content)
        self.assertIn("# Agent Memory Beacon v0.5.0", content)
        self.assertIn("patches/AGENT_MEMORY_BEACON.md.patch", content)
        self.assertNotIn("Agent Memory Vault", content)
        self.assertNotIn("patches/CLAUDE.md.patch", content)

    def test_upstream_watch_uses_explicit_vault_path(self):
        with tempfile.TemporaryDirectory() as vault:
            out_dir, state_path = weekly_upstream_watch.watch_paths(vault)
            self.assertEqual(out_dir, Path(vault) / "04-Feedback" / "upstream-watch")
            self.assertEqual(state_path, out_dir / "state.json")

    def test_upstream_watch_uses_existing_configured_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            configured_path = Path(tmp) / "configured-vault"
            config_path.write_text("vault_path: configured\n", encoding="utf-8")

            with patch.object(weekly_upstream_watch, "CONFIG_PATH", config_path), patch.object(
                weekly_upstream_watch,
                "load_config",
                return_value={"vault_path": str(configured_path)},
            ):
                self.assertEqual(weekly_upstream_watch.configured_vault(), configured_path)
                self.assertEqual(
                    weekly_upstream_watch.watch_paths(),
                    (
                        configured_path / "04-Feedback" / "upstream-watch",
                        configured_path / "04-Feedback" / "upstream-watch" / "state.json",
                    ),
                )

    def test_upstream_watch_uses_default_only_without_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_config = Path(tmp) / "config.yaml"
            default_path = Path(tmp) / "AgentMemoryBeacon"

            with patch.object(weekly_upstream_watch, "CONFIG_PATH", missing_config), patch.object(
                weekly_upstream_watch,
                "default_vault_path",
                return_value=default_path,
            ), patch.object(weekly_upstream_watch, "load_config") as load_config:
                self.assertEqual(weekly_upstream_watch.configured_vault(), default_path)
                load_config.assert_not_called()

    def test_upstream_watch_propagates_existing_config_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            config_path.write_text("vault_path: broken\n", encoding="utf-8")

            with patch.object(weekly_upstream_watch, "CONFIG_PATH", config_path), patch.object(
                weekly_upstream_watch,
                "load_config",
                side_effect=ValueError("invalid configured vault"),
            ):
                with self.assertRaisesRegex(ValueError, "invalid configured vault"):
                    weekly_upstream_watch.configured_vault()

    def test_upstream_watch_propagates_presence_probe_access_errors(self):
        config_path = Path("/inaccessible/config.yaml")

        with patch.object(weekly_upstream_watch, "CONFIG_PATH", config_path), patch.object(
            Path,
            "lstat",
            side_effect=PermissionError("config presence probe denied"),
        ), patch.object(
            weekly_upstream_watch, "default_vault_path"
        ) as default_vault_path, patch.object(
            weekly_upstream_watch, "load_config"
        ) as load_config:
            with self.assertRaisesRegex(PermissionError, "presence probe denied"):
                weekly_upstream_watch.configured_vault()

        default_vault_path.assert_not_called()
        load_config.assert_not_called()

    def test_upstream_watch_main_threads_one_resolved_path_pair(self):
        with tempfile.TemporaryDirectory() as vault:
            out_dir, state_path = weekly_upstream_watch.watch_paths(vault)
            state = {"last_seen_upstream": "old-revision"}

            with patch.object(weekly_upstream_watch, "watch_paths", return_value=(out_dir, state_path)), patch.object(
                weekly_upstream_watch, "fetch_upstream"
            ), patch.object(
                weekly_upstream_watch, "rev", return_value="new-revision"
            ), patch.object(
                weekly_upstream_watch, "load_state", return_value=state
            ) as load_state, patch.object(
                weekly_upstream_watch, "commit_rows", return_value=[]
            ), patch.object(
                weekly_upstream_watch, "changed_files", return_value=[]
            ), patch.object(
                weekly_upstream_watch, "diff_stat", return_value=""
            ), patch.object(
                weekly_upstream_watch, "render_report", return_value="# report\n"
            ), patch.object(
                weekly_upstream_watch, "write_report", return_value=out_dir / "report.md"
            ) as write_report, patch.object(
                weekly_upstream_watch, "save_state"
            ) as save_state, patch.object(sys, "argv", ["weekly_upstream_watch.py"]):
                self.assertEqual(weekly_upstream_watch.main(), 0)

            load_state.assert_called_once_with(state_path)
            write_report.assert_called_once_with("# report\n", out_dir)
            save_state.assert_called_once()
            args, _ = save_state.call_args
            self.assertEqual(args[1:], (out_dir, state_path))

    def test_upstream_watch_main_writes_only_to_configured_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured_vault = root / "configured-vault"
            transcript_root = root / "sessions"
            default_home = root / "default-home"
            default_vault = default_home / "AgentMemoryBeacon"
            config_path = root / "config.yaml"
            configured_vault.mkdir()
            transcript_root.mkdir()
            config_path.write_text(
                "\n".join(
                    (
                        f'vault_path: "{configured_vault}"',
                        f'codex_sessions_path: "{transcript_root}"',
                        "",
                    )
                ),
                encoding="utf-8",
            )

            with patch.object(
                weekly_upstream_watch, "CONFIG_PATH", config_path
            ), patch.object(
                config_module, "CONFIG_PATH", str(config_path)
            ), patch.dict(
                os.environ, {"HOME": str(default_home)}
            ), patch.object(
                weekly_upstream_watch, "fetch_upstream"
            ), patch.object(
                weekly_upstream_watch, "rev", return_value="new-revision"
            ), patch.object(
                weekly_upstream_watch, "commit_rows", return_value=[]
            ), patch.object(
                weekly_upstream_watch, "changed_files", return_value=[]
            ), patch.object(
                weekly_upstream_watch, "diff_stat", return_value=""
            ), patch.object(sys, "argv", ["weekly_upstream_watch.py"]):
                self.assertEqual(weekly_upstream_watch.main(), 0)

            configured_out = configured_vault / "04-Feedback" / "upstream-watch"
            state_path = configured_out / "state.json"
            reports = list(configured_out.glob("*-upstream-update.md"))
            self.assertEqual(len(reports), 1)
            self.assertTrue(state_path.is_file())
            self.assertEqual(
                json.loads(state_path.read_text(encoding="utf-8"))[
                    "last_seen_upstream"
                ],
                "new-revision",
            )
            self.assertFalse(
                (default_vault / "04-Feedback" / "upstream-watch").exists()
            )

    def test_upstream_watch_risk_copy_uses_configured_vault_term(self):
        _, risks = weekly_upstream_watch.compare_notes(["project-local storage"], [])

        self.assertIn("配置的 Obsidian Vault", risks[0])
        self.assertNotIn("ObsidianBrain vault", risks[0])


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


if __name__ == "__main__":
    unittest.main()
