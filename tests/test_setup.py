import os
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from branding import PRODUCT_NAME, PRODUCT_VERSION, default_vault_path
from memory_recall import validate_recall_index
from setup import (
    add_projects_to_config,
    create_project_folders,
    create_vault_structure,
    main,
    prompt_required,
)


class SetupTests(unittest.TestCase):
    def test_add_project_cli_creates_templates_and_updates_config(self):
        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            config_path = os.path.join(root, "config.yaml")
            create_vault_structure(vault)
            write_text(
                config_path,
                yaml.safe_dump(
                    {
                        "vault_path": vault,
                        "projects": ["existing"],
                        "custom_setting": {"preserve": True},
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
            )

            exit_code = main(
                [
                    "--add-project",
                    "New Project",
                    "--config",
                    config_path,
                ]
            )

            updated = yaml.safe_load(read_text(config_path))
            self.assertEqual(exit_code, 0)
            self.assertEqual(updated["projects"], ["existing", "new-project"])
            self.assertEqual(updated["custom_setting"], {"preserve": True})
            self.assertTrue(
                os.path.isfile(
                    os.path.join(
                        vault,
                        "01-Projects/new-project/Memory/decisions.md",
                    )
                )
            )

    def test_add_projects_to_config_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            config_path = os.path.join(root, "config.yaml")
            create_vault_structure(vault)
            write_text(
                config_path,
                yaml.safe_dump(
                    {"vault_path": vault, "projects": ["demo"]},
                    sort_keys=False,
                ),
            )

            first = add_projects_to_config(config_path, ["Demo", "Second"])
            second = add_projects_to_config(config_path, ["Second"])

            updated = yaml.safe_load(read_text(config_path))
            self.assertEqual(first, ["demo", "second"])
            self.assertEqual(second, ["second"])
            self.assertEqual(updated["projects"], ["demo", "second"])

    def test_prompt_required_accepts_beacon_default_on_empty_input(self):
        default = str(default_vault_path("/tmp/agent-memory-beacon-home"))

        with patch("builtins.input", return_value=""):
            self.assertEqual(
                prompt_required("Vault path", default=default),
                default,
            )

    def test_fresh_vault_uses_beacon_identity(self):
        with tempfile.TemporaryDirectory() as vault:
            create_vault_structure(vault)
            readme = read_text(os.path.join(vault, "README.md"))
            self.assertIn(f'vault_version: "{PRODUCT_VERSION}"', readme)
            self.assertIn(f'vault_name: "{PRODUCT_NAME}"', readme)
            self.assertIn(f"# {PRODUCT_NAME}", readme)

    def test_fresh_setup_uses_repository_error_taxonomy(self):
        with tempfile.TemporaryDirectory() as vault:
            create_vault_structure(vault)

            taxonomy = read_text(
                os.path.join(vault, "04-Feedback", "error-taxonomy.md")
            )
            self.assertIn('name: "path-filesystem"', taxonomy)
            self.assertIn('name: "data-format"', taxonomy)
            frontmatter = yaml.safe_load(taxonomy.split("---", 2)[1])
            self.assertEqual(len(frontmatter["categories"]), 11)
            self.assertEqual(
                sum(
                    len(category.get("subcategories", []))
                    for category in frontmatter["categories"]
                ),
                32,
            )

    def test_fresh_setup_creates_valid_map_link_targets(self):
        with tempfile.TemporaryDirectory() as vault:
            create_vault_structure(vault)

            self.assertTrue(os.path.isfile(os.path.join(vault, "03-Maps", "timeline.md")))
            self.assertTrue(os.path.isfile(os.path.join(vault, "03-Maps", "topic-index.md")))
            self.assertTrue(
                os.path.isfile(
                    os.path.join(vault, "05-Agent-Memory", "personal-memory.md")
                )
            )

    def test_fresh_setup_installs_complete_reusable_template_manifest(self):
        with tempfile.TemporaryDirectory() as vault:
            create_vault_structure(vault)

            expected = (
                "00-Rules/_TEMPLATE.md",
                "00-Rules/_inbox/_TEMPLATE.md",
                "02-Templates/project/Feedback/_TEMPLATE.md",
                "02-Templates/project/Memory/cross-project-links.md",
                "02-Templates/project/Memory/decisions.md",
                "02-Templates/project/Memory/pitfalls.md",
                "02-Templates/project/Memory/sessions/_TEMPLATE.md",
                "04-Feedback/growth-metrics.md",
                "04-Feedback/weekly-reports/_TEMPLATE.md",
                "用户手册.md",
            )
            for rel_path in expected:
                self.assertTrue(os.path.isfile(os.path.join(vault, rel_path)), rel_path)
            self.assertFalse(
                os.path.exists(os.path.join(vault, "01-Projects", "project-alpha"))
            )

    def test_rerunning_setup_preserves_user_modified_template(self):
        with tempfile.TemporaryDirectory() as vault:
            create_vault_structure(vault)
            template = os.path.join(vault, "00-Rules", "_TEMPLATE.md")
            write_text(template, "user-owned template\n")

            create_vault_structure(vault)

            self.assertEqual(read_text(template), "user-owned template\n")

    def test_fresh_setup_creates_valid_recall_index_for_runtime_install(self):
        with tempfile.TemporaryDirectory() as vault:
            create_vault_structure(vault)

            index_path = os.path.join(
                vault,
                "05-Agent-Memory",
                "recall-index.json",
            )
            self.assertTrue(os.path.isfile(index_path))
            with open(index_path, "r", encoding="utf-8") as handle:
                index = json.load(handle)

            validate_recall_index(index)
            self.assertEqual(index["units"], [])

    def test_rerunning_setup_preserves_existing_vault_control_files(self):
        with tempfile.TemporaryDirectory() as vault:
            existing = {
                "README.md": "custom readme\n",
                "04-Feedback/error-taxonomy.md": "custom taxonomy\n",
                "04-Feedback/heartbeat.md": "custom heartbeat\n",
            }
            for rel_path, content in existing.items():
                path = os.path.join(vault, rel_path)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                write_text(path, content)

            create_vault_structure(vault)

            for rel_path, content in existing.items():
                self.assertEqual(read_text(os.path.join(vault, rel_path)), content)

    def test_fresh_setup_hides_internal_machine_directories(self):
        with tempfile.TemporaryDirectory() as vault:
            create_vault_structure(vault)

            with open(
                os.path.join(vault, ".obsidian", "app.json"),
                "r",
                encoding="utf-8",
            ) as handle:
                app = json.load(handle)
            self.assertTrue(
                {
                    "04-Feedback/_raw-sessions/",
                    "04-Feedback/_rollback/",
                    "04-Feedback/_cleanup-backups/",
                    "04-Feedback/_logs/",
                    "05-Agent-Memory/codex-profile/",
                    "Users/",
                }.issubset(set(app["userIgnoreFilters"]))
            )

    def test_fresh_setup_refuses_symlinked_obsidian_directory(self):
        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            outside = os.path.join(root, "outside")
            os.makedirs(vault)
            os.makedirs(outside)
            sentinel = os.path.join(outside, "app.json")
            write_text(sentinel, '{"outside": true}\n')
            os.symlink(outside, os.path.join(vault, ".obsidian"))

            with self.assertRaises((OSError, ValueError)):
                create_vault_structure(vault)

            self.assertEqual(read_text(sentinel), '{"outside": true}\n')

    def test_fresh_setup_refuses_symlinked_control_file(self):
        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            os.makedirs(vault)
            sentinel = os.path.join(root, "outside.md")
            write_text(sentinel, "outside sentinel\n")
            os.symlink(sentinel, os.path.join(vault, "README.md"))

            with self.assertRaises((OSError, ValueError)):
                create_vault_structure(vault)

            self.assertEqual(read_text(sentinel), "outside sentinel\n")

    def test_project_names_cannot_escape_projects_directory(self):
        with tempfile.TemporaryDirectory() as vault:
            created = create_project_folders(vault, ["../../outside", "valid-project"])

            self.assertEqual(created, ["valid-project"])
            self.assertFalse(os.path.exists(os.path.join(vault, "outside")))
            self.assertTrue(
                os.path.isdir(
                    os.path.join(
                        vault,
                        "01-Projects/valid-project/Memory/sessions",
                    )
                )
            )

    def test_project_setup_renders_templates_only_for_real_project(self):
        with tempfile.TemporaryDirectory() as vault:
            create_vault_structure(vault)

            created = create_project_folders(vault, ["Real Project"])

            self.assertEqual(created, ["real-project"])
            project = os.path.join(vault, "01-Projects", "real-project")
            expected = (
                "Feedback/_TEMPLATE.md",
                "Memory/cross-project-links.md",
                "Memory/decisions.md",
                "Memory/pitfalls.md",
                "Memory/sessions/_TEMPLATE.md",
            )
            for rel_path in expected:
                self.assertTrue(os.path.isfile(os.path.join(project, rel_path)), rel_path)
            decisions = read_text(os.path.join(project, "Memory", "decisions.md"))
            pitfalls = read_text(os.path.join(project, "Memory", "pitfalls.md"))
            session_template = read_text(
                os.path.join(project, "Memory", "sessions", "_TEMPLATE.md")
            )
            self.assertIn('schema_version: "2.0"', decisions)
            self.assertIn('schema_version: "2.0"', pitfalls)
            self.assertIn('project: "real-project"', decisions)
            self.assertIn('projects: ["real-project"]', session_template)
            self.assertNotIn("project-alpha", decisions + pitfalls + session_template)

    def test_project_setup_refuses_symlinked_project_directory(self):
        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            projects = os.path.join(vault, "01-Projects")
            outside = os.path.join(root, "outside-project")
            os.makedirs(projects)
            os.makedirs(outside)
            os.symlink(outside, os.path.join(projects, "linked-project"))

            with self.assertRaises((OSError, ValueError)):
                create_project_folders(vault, ["linked-project"])

            self.assertEqual(os.listdir(outside), [])


def write_text(path, content):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


if __name__ == "__main__":
    unittest.main()
