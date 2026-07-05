import json
import os
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from codex_profile_sync import apply_profile, export_profile, status_profile


class CodexProfileSyncTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
