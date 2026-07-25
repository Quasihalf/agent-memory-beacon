import json
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

import codex_profile_sync
import config as config_module
from codex_profile_sync import (
    apply_profile,
    export_profile,
    status_profile,
    sync_profile_agents_compiled_blocks,
    sync_profile_agents_managed_blocks,
)


class CodexProfileSyncTests(unittest.TestCase):
    def test_actual_managed_profile_sync_preserves_crlf_for_update_and_add(self):
        source_document = managed_document(
            "source preamble\n",
            "fresh source rules",
            "fresh source projects",
            "\nsource suffix\n",
            current=True,
        )
        cases = (
            (
                "update",
                old_managed_document(
                    "target preamble  \n\n",
                    "stale rules",
                    "stale projects",
                    "\n\ntarget suffix  \n",
                ).replace("\n", "\r\n"),
                "target preamble  \r\n\r\n",
                "\r\n\r\ntarget suffix  \r\n",
            ),
            (
                "add",
                "target preamble  \r\n\r\ntarget suffix at EOF  ",
                "target preamble  \r\n\r\ntarget suffix at EOF  ",
                None,
            ),
        )

        for mode, existing, expected_prefix, expected_suffix in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / "AGENTS.md"
                profile = root / "profile"
                shared = profile / "AGENTS.shared.md"
                profile.mkdir()
                source.write_bytes(source_document.encode("utf-8"))
                shared.write_bytes(existing.encode("utf-8"))

                self.assertTrue(sync_profile_agents_managed_blocks(source, profile))
                first = shared.read_bytes()
                start, end = managed_block_span_bytes(first)
                if expected_suffix is not None:
                    self.assertEqual(
                        first[:start],
                        expected_prefix.encode("utf-8"),
                    )
                    self.assertEqual(first[end:], expected_suffix.encode("utf-8"))
                else:
                    self.assertTrue(first.startswith(existing.encode("utf-8")))
                self.assertEqual(first.count(CURRENT_MANAGED_START.encode()), 1)
                self.assertEqual(first.count(CURRENT_MANAGED_END.encode()), 1)

                self.assertFalse(sync_profile_agents_managed_blocks(source, profile))
                self.assertEqual(shared.read_bytes(), first)

    def test_actual_compiled_profile_sync_preserves_crlf_for_update_and_add(self):
        cases = (
            (
                "update",
                old_managed_document(
                    "target preamble  \n\n",
                    "stale rules",
                    "stale projects",
                    "\n\ntarget suffix  \n",
                ).replace("\n", "\r\n"),
                "target preamble  \r\n\r\n",
                "\r\n\r\ntarget suffix  \r\n",
            ),
            (
                "add",
                "target preamble  \r\n\r\ntarget suffix at EOF  ",
                "target preamble  \r\n\r\ntarget suffix at EOF  ",
                None,
            ),
        )

        for mode, existing, expected_prefix, expected_suffix in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                profile = Path(tmp) / "profile"
                shared = profile / "AGENTS.shared.md"
                profile.mkdir()
                shared.write_bytes(existing.encode("utf-8"))

                self.assertTrue(
                    sync_profile_agents_compiled_blocks(
                        profile,
                        "fresh compiled rules",
                        "fresh compiled projects",
                    )
                )
                first = shared.read_bytes()
                start, end = managed_block_span_bytes(first)
                if expected_suffix is not None:
                    self.assertEqual(
                        first[:start],
                        expected_prefix.encode("utf-8"),
                    )
                    self.assertEqual(first[end:], expected_suffix.encode("utf-8"))
                else:
                    self.assertTrue(first.startswith(existing.encode("utf-8")))
                self.assertIn(b"fresh compiled rules", first)
                self.assertIn(b"fresh compiled projects", first)
                self.assertEqual(first.count(CURRENT_MANAGED_START.encode()), 1)
                self.assertEqual(first.count(CURRENT_MANAGED_END.encode()), 1)

                self.assertFalse(
                    sync_profile_agents_compiled_blocks(
                        profile,
                        "fresh compiled rules",
                        "fresh compiled projects",
                    )
                )
                self.assertEqual(shared.read_bytes(), first)

    def test_actual_profile_export_preserves_crlf_target_bytes_on_two_runs(self):
        source_document = managed_document(
            "source preamble\n",
            "fresh export rules",
            "fresh export projects",
            "\nsource suffix\n",
            current=True,
        )
        cases = (
            (
                "update",
                old_managed_document(
                    "target preamble  \n\n",
                    "stale rules",
                    "stale projects",
                    "\n\ntarget suffix  \n",
                ).replace("\n", "\r\n"),
            ),
            ("add", "target preamble  \r\n\r\ntarget suffix at EOF  "),
        )

        for mode, existing in cases:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                codex_home = root / "codex-home"
                profile = root / "profile"
                shared = profile / "AGENTS.shared.md"
                codex_home.mkdir()
                profile.mkdir()
                (codex_home / "AGENTS.md").write_bytes(
                    source_document.encode("utf-8")
                )
                shared.write_bytes(existing.encode("utf-8"))

                export_profile(codex_home, profile)
                first = shared.read_bytes()
                if mode == "update":
                    start, end = managed_block_span_bytes(first)
                    expected_prefix = "target preamble  \r\n\r\n".encode()
                    expected_suffix = "\r\n\r\ntarget suffix  \r\n".encode()
                    self.assertEqual(first[:start], expected_prefix)
                    self.assertEqual(first[end:], expected_suffix)
                else:
                    self.assertTrue(first.startswith(existing.encode("utf-8")))

                export_profile(codex_home, profile)
                self.assertEqual(shared.read_bytes(), first)

    def test_actual_profile_apply_copies_exact_crlf_bytes_without_second_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = root / "profile"
            codex_home = root / "codex-home"
            profile.mkdir()
            shared = profile / "AGENTS.shared.md"
            exact = managed_document(
                "profile preamble  \n\n",
                "profile rules",
                "profile projects",
                "\n\nprofile suffix  \n",
                current=True,
            ).replace("\n", "\r\n").encode("utf-8")
            shared.write_bytes(exact)

            apply_profile(profile, codex_home)
            target = codex_home / "AGENTS.md"
            self.assertEqual(target.read_bytes(), exact)
            backups = sorted(codex_home.glob("AGENTS.md.bak-*"))

            apply_profile(profile, codex_home)
            self.assertEqual(target.read_bytes(), exact)
            self.assertEqual(sorted(codex_home.glob("AGENTS.md.bak-*")), backups)

    def test_absent_runtime_config_uses_current_default_vault_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-config.yaml"
            with patch.object(config_module, "CONFIG_PATH", str(missing)), patch.dict(
                os.environ,
                {"HOME": tmp},
            ):
                self.assertEqual(
                    codex_profile_sync.default_profile_dir(),
                    Path(tmp)
                    / "AgentMemoryBeacon"
                    / "05-Agent-Memory"
                    / "codex-profile",
                )

    def test_runtime_config_permission_error_fails_before_profile_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            vault.mkdir()
            config_path = root / "config.yaml"
            config_path.write_text(f"vault_path: {vault}\n", encoding="utf-8")
            profile = vault / "05-Agent-Memory" / "codex-profile"

            with patch.object(
                config_module,
                "CONFIG_PATH",
                str(config_path),
            ), patch.dict(
                os.environ,
                {"HOME": tmp},
            ), patch.object(
                Path,
                "lstat",
                side_effect=PermissionError("config presence probe denied"),
            ), patch.object(
                sys,
                "argv",
                ["codex_profile_sync.py", "export"],
            ):
                with self.assertRaisesRegex(PermissionError, "presence probe denied"):
                    codex_profile_sync.main()

            self.assertFalse(profile.exists())

    def test_malformed_runtime_config_fails_before_profile_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.yaml"
            config_path.write_text("vault_path: [\n", encoding="utf-8")

            with patch.object(
                config_module,
                "CONFIG_PATH",
                str(config_path),
            ), patch.dict(
                os.environ,
                {"HOME": tmp},
            ), patch.object(
                sys,
                "argv",
                ["codex_profile_sync.py", "export"],
            ):
                with self.assertRaises(yaml.YAMLError):
                    codex_profile_sync.main()

            self.assertFalse((root / "AgentMemoryBeacon").exists())
            self.assertFalse((root / "ObsidianBrain").exists())

    def test_dangling_runtime_config_symlink_fails_before_profile_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.yaml"
            config_path.symlink_to(root / "missing-target.yaml")

            with patch.object(
                config_module,
                "CONFIG_PATH",
                str(config_path),
            ), patch.dict(
                os.environ,
                {"HOME": tmp},
            ), patch.object(
                sys,
                "argv",
                ["codex_profile_sync.py", "export"],
            ):
                with self.assertRaises(FileNotFoundError):
                    codex_profile_sync.main()

            self.assertFalse((root / "AgentMemoryBeacon").exists())
            self.assertFalse((root / "ObsidianBrain").exists())

    def test_runtime_config_missing_required_key_fails_before_profile_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.yaml"
            config_path.write_text("codex_home: ~/.codex\n", encoding="utf-8")

            with patch.object(
                config_module,
                "CONFIG_PATH",
                str(config_path),
            ), patch.dict(
                os.environ,
                {"HOME": tmp},
            ), patch.object(
                sys,
                "argv",
                ["codex_profile_sync.py", "export"],
            ):
                with self.assertRaisesRegex(KeyError, "vault_path"):
                    codex_profile_sync.main()

            self.assertFalse((root / "AgentMemoryBeacon").exists())
            self.assertFalse((root / "ObsidianBrain").exists())

    def test_valid_runtime_config_preserves_explicit_profile_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "vault"
            vault.mkdir()
            profile = root / "configured-profile"
            sessions = root / "sessions"
            sessions.mkdir()
            config_path = root / "config.yaml"
            config_path.write_text(
                f"vault_path: {vault}\n"
                f"codex_profile_path: {profile}\n"
                f"codex_sessions_path: {sessions}\n",
                encoding="utf-8",
            )

            with patch.object(
                config_module,
                "CONFIG_PATH",
                str(config_path),
            ), patch.dict(
                os.environ,
                {"HOME": tmp},
            ):
                self.assertEqual(codex_profile_sync.default_profile_dir(), profile)

    def test_export_profile_preflights_ambiguous_source_before_creating_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_home = os.path.join(tmp, "source")
            profile_dir = os.path.join(tmp, "profile")
            write_fake_codex_home(source_home)
            write_text(
                os.path.join(source_home, "AGENTS.md"),
                ambiguous_agents_document(),
            )

            with self.assertRaisesRegex(ValueError, "multiple managed blocks"):
                export_profile(source_home, profile_dir, include_config=True)

            self.assertFalse(os.path.exists(profile_dir))

    def test_export_profile_preflights_ambiguous_destination_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_home = os.path.join(tmp, "source")
            profile_dir = os.path.join(tmp, "profile")
            write_fake_codex_home(source_home)
            sentinel_paths = {
                "skill": os.path.join(profile_dir, "skills", "sentinel", "SKILL.md"),
                "agents": os.path.join(profile_dir, "AGENTS.shared.md"),
                "config": os.path.join(profile_dir, "config.toml"),
                "skills_manifest": os.path.join(profile_dir, "skills-manifest.json"),
                "plugins_manifest": os.path.join(profile_dir, "plugins-manifest.json"),
            }
            write_text(sentinel_paths["skill"], "sentinel skill\n")
            write_text(sentinel_paths["agents"], ambiguous_agents_document())
            write_text(sentinel_paths["config"], "sentinel config\n")
            write_text(sentinel_paths["skills_manifest"], "sentinel skills manifest\n")
            write_text(sentinel_paths["plugins_manifest"], "sentinel plugins manifest\n")
            before = {name: read_text(path) for name, path in sentinel_paths.items()}

            with self.assertRaisesRegex(ValueError, "multiple managed blocks"):
                export_profile(source_home, profile_dir, include_config=True)

            for name, path in sentinel_paths.items():
                self.assertEqual(read_text(path), before[name])

    def test_compiled_profile_sync_preserves_outer_text_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = os.path.join(tmp, "profile")
            shared = os.path.join(profile, "AGENTS.shared.md")
            os.makedirs(profile)

            for current_target in (False, True):
                with self.subTest(current_target=current_target):
                    existing = managed_document(
                        "target preamble  \n\n",
                        "stale rules",
                        "stale projects",
                        "\n\ntarget suffix  \n",
                        current=current_target,
                    )
                    write_text(shared, existing)

                    self.assertFalse(
                        sync_profile_agents_compiled_blocks(
                            profile,
                            "fresh rules",
                            "fresh projects",
                            dry_run=True,
                        )
                    )
                    self.assertEqual(read_text(shared), existing)

                    self.assertTrue(
                        sync_profile_agents_compiled_blocks(
                            profile,
                            "fresh rules",
                            "fresh projects",
                        )
                    )
                    updated = read_text(shared)
                    assert_managed_outside_preserved(self, existing, updated)
                    self.assertEqual(updated.count(CURRENT_MANAGED_START), 1)
                    self.assertNotIn(OLD_MANAGED_START, updated)
                    self.assertIn("fresh rules", updated)
                    self.assertIn("fresh projects", updated)
                    self.assertNotIn("stale rules", updated)
                    self.assertNotIn("stale projects", updated)

                    self.assertFalse(
                        sync_profile_agents_compiled_blocks(
                            profile,
                            "fresh rules",
                            "fresh projects",
                        )
                    )
                    self.assertEqual(read_text(shared), updated)

    def test_export_profile_upgrades_old_source_and_preserves_existing_profile_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_home = os.path.join(tmp, "source")
            profile_dir = os.path.join(tmp, "profile")
            shared = os.path.join(profile_dir, "AGENTS.shared.md")
            write_fake_codex_home(source_home)
            source_text = old_managed_document(
                "source preamble\n",
                "source rules",
                "source projects",
                "\nsource suffix\n",
            )
            existing = old_managed_document(
                "profile preamble\n\n",
                "stale rules",
                "stale projects",
                "\n\nprofile suffix\n",
            )
            write_text(os.path.join(source_home, "AGENTS.md"), source_text)
            write_text(shared, existing)

            export_profile(source_home, profile_dir)

            updated = read_text(shared)
            assert_managed_outside_preserved(self, existing, updated)
            self.assertEqual(updated.count(CURRENT_MANAGED_START), 1)
            self.assertNotIn(OLD_MANAGED_START, updated)
            self.assertIn("source rules", updated)
            self.assertIn("source projects", updated)
            self.assertNotIn("stale rules", updated)
            self.assertNotIn("stale projects", updated)

            export_profile(source_home, profile_dir)
            self.assertEqual(read_text(shared), updated)

    def test_export_profile_normalizes_unwrapped_source_without_changing_prose(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_home = os.path.join(tmp, "source")
            profile_dir = os.path.join(tmp, "profile")
            source_text = "source prose\n\nwith an exact suffix"
            write_fake_codex_home(source_home)
            write_text(os.path.join(source_home, "AGENTS.md"), source_text)

            export_profile(source_home, profile_dir)

            shared = read_text(os.path.join(profile_dir, "AGENTS.shared.md"))
            self.assertTrue(shared.startswith(source_text))
            self.assertEqual(shared.count(CURRENT_MANAGED_START), 1)
            self.assertNotIn(OLD_MANAGED_START, shared)

    def test_profile_sync_source_bodies_replace_old_and_current_shared_wrappers(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "AGENTS.md")
            profile = os.path.join(tmp, "profile")
            shared = os.path.join(profile, "AGENTS.shared.md")
            os.makedirs(profile)
            write_text(
                source,
                old_managed_document(
                    "source preamble\n",
                    "source rules",
                    "source projects",
                    "\nsource suffix\n",
                ),
            )

            for current_target in (False, True):
                with self.subTest(current_target=current_target):
                    existing = managed_document(
                        "target preamble\n\n",
                        "stale rules",
                        "stale projects",
                        "\n\ntarget suffix\n",
                        current=current_target,
                    )
                    write_text(shared, existing)

                    changed = sync_profile_agents_managed_blocks(source, profile)

                    self.assertTrue(changed)
                    updated = read_text(shared)
                    assert_managed_outside_preserved(self, existing, updated)
                    self.assertEqual(updated.count(CURRENT_MANAGED_START), 1)
                    self.assertNotIn(OLD_MANAGED_START, updated)
                    self.assertIn("source rules", updated)
                    self.assertIn("source projects", updated)
                    self.assertNotIn("stale rules", updated)
                    self.assertNotIn("stale projects", updated)

                    self.assertFalse(sync_profile_agents_managed_blocks(source, profile))
                    self.assertEqual(read_text(shared), updated)

    def test_profile_sync_dry_run_leaves_pending_update_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "AGENTS.md")
            profile = os.path.join(tmp, "profile")
            shared = os.path.join(profile, "AGENTS.shared.md")
            os.makedirs(profile)
            write_text(
                source,
                old_managed_document(
                    "source preamble\n",
                    "source rules",
                    "source projects",
                    "\nsource suffix\n",
                ),
            )
            existing = managed_document(
                "target preamble\n",
                "stale rules",
                "stale projects",
                "\ntarget suffix\n",
                current=True,
            )
            write_text(shared, existing)

            self.assertFalse(sync_profile_agents_managed_blocks(source, profile, dry_run=True))
            self.assertEqual(read_text(shared), existing)

    def test_profile_sync_upgrades_old_outer_marker_without_losing_preamble(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "AGENTS.md")
            profile = os.path.join(tmp, "profile")
            shared = os.path.join(profile, "AGENTS.shared.md")
            os.makedirs(profile)
            write_text(
                source,
                """source preamble
<!-- KNOWLEDGE_BRAIN:MANAGED_START version=2 -->
## Agent Memory Vault - Auto-Maintained Blocks
<!-- COMPILED:RULES_START -->source rules<!-- COMPILED:RULES_END -->
<!-- COMPILED:PROJECTS_START -->source projects<!-- COMPILED:PROJECTS_END -->
<!-- KNOWLEDGE_BRAIN:MANAGED_END -->
""",
            )
            write_text(shared, "profile preamble\nprofile suffix\n")

            changed = sync_profile_agents_managed_blocks(source, profile)

            self.assertTrue(changed)
            content = read_text(shared)
            self.assertIn("profile preamble", content)
            self.assertIn("profile suffix", content)
            self.assertIn("AGENT_MEMORY_BEACON:MANAGED_START version=3", content)
            self.assertIn("source rules", content)
            self.assertNotIn("KNOWLEDGE_BRAIN:MANAGED_START", content)

    def test_export_profile_does_not_follow_skill_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = os.path.join(tmp, "codex-home")
            profile_dir = os.path.join(tmp, "profile")
            outside_file = os.path.join(tmp, "outside-secret.txt")
            outside_skill = os.path.join(tmp, "outside-skill")
            write_fake_codex_home(codex_home)
            write_text(outside_file, "do-not-export")
            write_text(
                os.path.join(outside_skill, "SKILL.md"),
                "---\nname: outside\n---\n",
            )
            os.symlink(
                outside_file,
                os.path.join(codex_home, "skills", "custom", "linked-secret.txt"),
            )
            os.symlink(
                outside_skill,
                os.path.join(codex_home, "skills", "linked-skill"),
            )

            export_profile(codex_home, profile_dir)

            self.assertFalse(
                os.path.lexists(
                    os.path.join(profile_dir, "skills", "custom", "linked-secret.txt")
                )
            )
            self.assertFalse(
                os.path.exists(os.path.join(profile_dir, "skills", "linked-skill"))
            )
    def test_export_profile_copies_safe_local_state_and_manifests(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = os.path.join(tmp, "codex-home")
            profile_dir = os.path.join(tmp, "profile")
            write_fake_codex_home(codex_home)

            result = export_profile(codex_home, profile_dir, include_config=True)

            self.assertEqual(result["skills_exported"], 1)
            self.assertTrue(
                os.path.exists(os.path.join(profile_dir, "skills", "custom", "SKILL.md"))
            )
            self.assertFalse(os.path.exists(os.path.join(profile_dir, "skills", ".system")))
            self.assertTrue(os.path.exists(os.path.join(profile_dir, "AGENTS.shared.md")))
            self.assertTrue(os.path.exists(os.path.join(profile_dir, "config.toml")))
            self.assertFalse(os.path.exists(os.path.join(profile_dir, "auth.json")))

            skills = load_json(os.path.join(profile_dir, "skills-manifest.json"))
            plugins = load_json(os.path.join(profile_dir, "plugins-manifest.json"))
            self.assertEqual(skills["skills"][0]["name"], "custom")
            self.assertEqual(plugins["plugins"][0]["id"], "pensive@claude-night-market")
            self.assertTrue(plugins["plugins"][0]["enabled"])

    def test_apply_profile_installs_skills_and_agents_without_auth(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_home = os.path.join(tmp, "source")
            target_home = os.path.join(tmp, "target")
            profile_dir = os.path.join(tmp, "profile")
            write_fake_codex_home(source_home)
            export_profile(source_home, profile_dir, include_config=True)
            os.makedirs(target_home, exist_ok=True)
            write_text(os.path.join(target_home, "auth.json"), '{"token":"keep"}\n')

            result = apply_profile(profile_dir, target_home, include_config=False)

            self.assertEqual(result["skills_applied"], 1)
            self.assertTrue(
                os.path.exists(os.path.join(target_home, "skills", "custom", "SKILL.md"))
            )
            self.assertTrue(os.path.exists(os.path.join(target_home, "AGENTS.md")))
            self.assertFalse(os.path.exists(os.path.join(target_home, "config.toml")))
            self.assertEqual(read_text(os.path.join(target_home, "auth.json")), '{"token":"keep"}\n')

    def test_status_reports_missing_skill_and_plugin(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_home = os.path.join(tmp, "source")
            target_home = os.path.join(tmp, "target")
            profile_dir = os.path.join(tmp, "profile")
            write_fake_codex_home(source_home)
            export_profile(source_home, profile_dir, include_config=True)
            os.makedirs(target_home, exist_ok=True)
            write_text(os.path.join(target_home, "config.toml"), "")

            result = status_profile(profile_dir, target_home)

            self.assertEqual(result["missing_skills"], ["custom"])
            self.assertEqual(result["missing_plugins"], ["pensive@claude-night-market"])
            self.assertIn("authorization", result["notes"][0])

    def test_apply_profile_merges_plugin_config_without_dropping_target_preamble(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_home = os.path.join(tmp, "source")
            target_home = os.path.join(tmp, "target")
            profile_dir = os.path.join(tmp, "profile")
            write_fake_codex_home(source_home)
            export_profile(source_home, profile_dir, include_config=True)
            write_text(
                os.path.join(target_home, "config.toml"),
                'model = "gpt-5-codex"\n\n[features]\nweb_search = true\n',
            )

            apply_profile(profile_dir, target_home, include_config=True)

            config = read_text(os.path.join(target_home, "config.toml"))
            self.assertIn('model = "gpt-5-codex"', config)
            self.assertIn("[features]", config)
            self.assertIn('[plugins."pensive@claude-night-market"]', config)
            self.assertIn("enabled = true", config)

    def test_export_profile_rejects_codex_home_as_profile_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = os.path.join(tmp, "codex-home")
            write_fake_codex_home(codex_home)

            with self.assertRaises(ValueError):
                export_profile(codex_home, codex_home, include_config=True)

            self.assertTrue(os.path.exists(os.path.join(codex_home, "skills", "custom", "SKILL.md")))

    def test_apply_profile_rejects_destructive_path_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = os.path.join(tmp, "codex-home")
            write_fake_codex_home(codex_home)

            with self.assertRaises(ValueError):
                apply_profile(
                    codex_home,
                    codex_home,
                    overwrite=True,
                )

            self.assertTrue(
                os.path.exists(
                    os.path.join(codex_home, "skills", "custom", "SKILL.md")
                )
            )

    def test_export_profile_filters_sensitive_skill_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = os.path.join(tmp, "codex-home")
            profile_dir = os.path.join(tmp, "profile")
            write_fake_codex_home(codex_home)
            write_text(os.path.join(codex_home, "skills", "custom", ".env"), "TOKEN=secret\n")
            write_text(os.path.join(codex_home, "skills", "custom", ".env.example"), "TOKEN=\n")
            write_text(os.path.join(codex_home, "skills", "custom", "id_rsa"), "private\n")
            write_text(os.path.join(codex_home, "skills", "custom", "private.pem"), "private\n")
            write_text(os.path.join(codex_home, "skills", "custom", "notes.txt"), "keep\n")

            export_profile(codex_home, profile_dir)

            exported_skill = os.path.join(profile_dir, "skills", "custom")
            self.assertFalse(os.path.exists(os.path.join(exported_skill, ".env")))
            self.assertFalse(os.path.exists(os.path.join(exported_skill, ".env.example")))
            self.assertFalse(os.path.exists(os.path.join(exported_skill, "id_rsa")))
            self.assertFalse(os.path.exists(os.path.join(exported_skill, "private.pem")))
            self.assertTrue(os.path.exists(os.path.join(exported_skill, "notes.txt")))

    def test_status_reports_missing_profile_instead_of_clean_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = status_profile(os.path.join(tmp, "missing-profile"), os.path.join(tmp, "codex"))

            self.assertFalse(result["profile_exists"])
            self.assertIn("profile directory does not exist", result["notes"][0])

    def test_export_profile_records_enabled_plugin_without_cache_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = os.path.join(tmp, "codex-home")
            profile_dir = os.path.join(tmp, "profile")
            write_text(
                os.path.join(codex_home, "config.toml"),
                '[plugins."missing@example-market"]\nenabled = true\n',
            )

            export_profile(codex_home, profile_dir, include_config=True)

            plugins = load_json(os.path.join(profile_dir, "plugins-manifest.json"))
            self.assertEqual(plugins["plugins"][0]["id"], "missing@example-market")
            self.assertTrue(plugins["plugins"][0]["enabled"])
            self.assertFalse(plugins["plugins"][0]["cached"])

    def test_status_reports_changed_skill_with_same_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_home = os.path.join(tmp, "source")
            target_home = os.path.join(tmp, "target")
            profile_dir = os.path.join(tmp, "profile")
            write_fake_codex_home(source_home)
            export_profile(source_home, profile_dir)
            write_text(
                os.path.join(target_home, "skills", "custom", "SKILL.md"),
                "---\nname: custom\n---\n# Old Custom\n",
            )

            result = status_profile(profile_dir, target_home)

            self.assertEqual(result["missing_skills"], [])
            self.assertEqual(result["changed_skills"], ["custom"])

    def test_status_detects_agents_drift_and_overwrite_apply_converges(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_home = os.path.join(tmp, "source")
            target_home = os.path.join(tmp, "target")
            profile_dir = os.path.join(tmp, "profile")
            write_fake_codex_home(source_home)
            export_profile(source_home, profile_dir)
            write_text(
                os.path.join(target_home, "skills", "custom", "SKILL.md"),
                "---\nname: custom\n---\n# Old Custom\n",
            )
            write_text(os.path.join(target_home, "AGENTS.md"), "old rules\n")

            before = status_profile(profile_dir, target_home)
            self.assertEqual(before["changed_skills"], ["custom"])
            self.assertTrue(before["agents_changed"])

            apply_profile(profile_dir, target_home, overwrite=True)
            after = status_profile(profile_dir, target_home)

            self.assertEqual(after["changed_skills"], [])
            self.assertFalse(after["agents_changed"])

    def test_apply_profile_dry_run_does_not_write_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_home = os.path.join(tmp, "source")
            target_home = os.path.join(tmp, "target")
            profile_dir = os.path.join(tmp, "profile")
            write_fake_codex_home(source_home)
            export_profile(source_home, profile_dir, include_config=True)

            result = apply_profile(profile_dir, target_home, include_config=True, dry_run=True)

            self.assertTrue(result["dry_run"])
            self.assertEqual(result["skills_applied"], 1)
            self.assertFalse(os.path.exists(os.path.join(target_home, "skills", "custom", "SKILL.md")))
            self.assertFalse(os.path.exists(os.path.join(target_home, "AGENTS.md")))
            self.assertFalse(os.path.exists(os.path.join(target_home, "config.toml")))


def write_fake_codex_home(codex_home):
    write_text(
        os.path.join(codex_home, "skills", "custom", "SKILL.md"),
        "---\nname: custom\n---\n# Custom\n",
    )
    write_text(
        os.path.join(codex_home, "skills", ".system", "SKILL.md"),
        "---\nname: system\n---\n# System\n",
    )
    write_text(os.path.join(codex_home, "AGENTS.md"), "shared rules\n")
    write_text(os.path.join(codex_home, "auth.json"), '{"token":"secret"}\n')
    write_text(
        os.path.join(codex_home, "config.toml"),
        '[plugins."pensive@claude-night-market"]\nenabled = true\n',
    )
    write_text(
        os.path.join(
            codex_home,
            "plugins",
            "cache",
            "claude-night-market",
            "pensive",
            "1.0.0",
            ".claude-plugin",
            "plugin.json",
        ),
        json.dumps(
            {
                "name": "pensive",
                "version": "1.0.0",
                "description": "Review tools",
            }
        ),
    )


def write_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


CURRENT_MANAGED_START = "<!-- AGENT_MEMORY_BEACON:MANAGED_START version=3 -->"
CURRENT_MANAGED_END = "<!-- AGENT_MEMORY_BEACON:MANAGED_END -->"
OLD_MANAGED_START = "<!-- KNOWLEDGE_BRAIN:MANAGED_START version=2 -->"
OLD_MANAGED_END = "<!-- KNOWLEDGE_BRAIN:MANAGED_END -->"


def old_managed_document(preamble, rules, projects, suffix):
    return managed_document(preamble, rules, projects, suffix, current=False)


def managed_document(preamble, rules, projects, suffix, current):
    start = CURRENT_MANAGED_START if current else OLD_MANAGED_START
    end = CURRENT_MANAGED_END if current else OLD_MANAGED_END
    return f"""{preamble}{start}
## Agent Memory Vault - Auto-Maintained Blocks
<!-- COMPILED:RULES_START -->
{rules}
<!-- COMPILED:RULES_END -->
<!-- COMPILED:PROJECTS_START -->
{projects}
<!-- COMPILED:PROJECTS_END -->
{end}{suffix}"""


def ambiguous_agents_document():
    return "\n".join(
        (
            old_managed_document("", "old rules", "old projects", ""),
            managed_document("", "current rules", "current projects", "", current=True),
        )
    )


def assert_managed_outside_preserved(test_case, existing, updated):
    old_start, old_end = managed_block_span(existing)
    new_start, new_end = managed_block_span(updated)
    test_case.assertEqual(updated[:new_start], existing[:old_start])
    test_case.assertEqual(updated[new_end:], existing[old_end:])


def managed_block_span(content):
    for start_marker, end_marker in (
        (CURRENT_MANAGED_START, CURRENT_MANAGED_END),
        (OLD_MANAGED_START, OLD_MANAGED_END),
    ):
        start = content.find(start_marker)
        if start >= 0:
            end = content.index(end_marker, start) + len(end_marker)
            return start, end
    raise AssertionError("managed block missing")


def managed_block_span_bytes(content):
    for start_marker, end_marker in (
        (CURRENT_MANAGED_START.encode(), CURRENT_MANAGED_END.encode()),
        (OLD_MANAGED_START.encode(), OLD_MANAGED_END.encode()),
    ):
        start = content.find(start_marker)
        if start >= 0:
            end = content.index(end_marker, start) + len(end_marker)
            return start, end
    raise AssertionError("managed block missing")


if __name__ == "__main__":
    unittest.main()
