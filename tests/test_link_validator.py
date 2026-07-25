import os
import sys
import tempfile
import unittest
from unittest.mock import patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from link_validator import run


class LinkValidatorTests(unittest.TestCase):
    def test_table_alias_and_sessions_suffix_resolve_to_existing_note(self):
        with tempfile.TemporaryDirectory() as vault:
            write_text(
                os.path.join(
                    vault,
                    "01-Projects/demo/Memory/sessions/2026-07-10-demo.md",
                ),
                "# Demo\n",
            )
            write_text(
                os.path.join(vault, "03-Maps/timeline.md"),
                (
                    "| Project | Session |\n"
                    "|---|---|\n"
                    "| demo | [[sessions/2026-07-10-demo\\|Demo session]] |\n"
                ),
            )

            self.assertEqual(run(vault), [])

    def test_unclosed_multiline_markup_and_fenced_examples_are_ignored(self):
        with tempfile.TemporaryDirectory() as vault:
            write_text(
                os.path.join(vault, "note.md"),
                (
                    "This is unfinished markup: [[not-a-link\n"
                    "and the next line ends with brackets ]]\n\n"
                    "```markdown\n"
                    "[[example-that-does-not-exist]]\n"
                    "```\n"
                ),
            )

            self.assertEqual(run(vault), [])

    def test_internal_machine_and_backup_directories_are_not_scanned(self):
        with tempfile.TemporaryDirectory() as vault:
            internal_notes = [
                "05-Agent-Memory/codex-profile/skills/demo/SKILL.md",
                "04-Feedback/_cleanup-backups/old.md",
                "04-Feedback/_logs/run.md",
                "04-Feedback/_raw-sessions/raw.md",
            ]
            for rel_path in internal_notes:
                write_text(os.path.join(vault, rel_path), "[[missing-internal-note]]\n")

            self.assertEqual(run(vault), [])

    def test_single_string_exclusion_is_one_directory_name(self):
        with tempfile.TemporaryDirectory() as vault:
            write_text(
                os.path.join(vault, ".obsidian", "plugin.md"),
                "[[missing-plugin-target]]\n",
            )
            write_text(
                os.path.join(vault, ".", "visible.md"),
                "[[missing-visible-target]]\n",
            )

            broken = run(vault, excluded_dir_names=".obsidian")

            self.assertEqual([item["source"] for item in broken], ["visible.md"])

    def test_walk_errors_are_propagated(self):
        with tempfile.TemporaryDirectory() as vault:
            real_walk = os.walk
            injected = False

            def permission_denied_walk(top, *args, **kwargs):
                nonlocal injected
                if not injected:
                    injected = True
                    kwargs["onerror"](PermissionError("link inventory denied"))
                    return iter(())
                return real_walk(top, *args, **kwargs)

            with patch("link_validator.os.walk", side_effect=permission_denied_walk):
                with self.assertRaisesRegex(
                    PermissionError, "link inventory denied"
                ):
                    run(vault)

    def test_additional_markdown_path_is_validated_against_vault_index(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            vault = os.path.join(raw_tmp, "vault")
            external = os.path.join(raw_tmp, "profile", "AGENTS.shared.md")
            write_text(os.path.join(vault, "existing.md"), "# Existing\n")
            write_text(
                external,
                "[[existing]]\n[[missing-from-shared]]\n",
            )

            broken = run(vault, additional_markdown_paths=[external])

            self.assertEqual(
                broken,
                [
                    {
                        "source": external,
                        "target": "missing-from-shared",
                        "reason": "file not found",
                    }
                ],
            )

    def test_template_placeholder_links_are_ignored_but_template_is_indexed(self):
        with tempfile.TemporaryDirectory() as vault:
            template = os.path.join(
                vault,
                "02-Templates/project/Memory/sessions/_TEMPLATE.md",
            )
            write_text(template, "[[sessions/{file}]]\n")
            write_text(
                os.path.join(vault, "visible.md"),
                "[[02-Templates/project/Memory/sessions/_TEMPLATE]]\n",
            )

            self.assertEqual(run(vault), [])

    def test_template_can_be_explicitly_validated(self):
        with tempfile.TemporaryDirectory() as vault:
            template = os.path.join(
                vault,
                "02-Templates/project/Memory/sessions/_TEMPLATE.md",
            )
            write_text(template, "[[sessions/{file}]]\n")

            self.assertEqual(
                run(vault, additional_markdown_paths=[template]),
                [
                    {
                        "source": (
                            "02-Templates/project/Memory/sessions/_TEMPLATE.md"
                        ),
                        "target": "sessions/{file}",
                        "reason": "file not found",
                    }
                ],
            )

    def test_named_template_outside_template_directory_is_not_a_source(self):
        with tempfile.TemporaryDirectory() as vault:
            template = os.path.join(vault, "00-Rules/_TEMPLATE.md")
            write_text(template, "[[00-Rules/_inbox/{PROPOSAL-ID}]]\n")
            write_text(
                os.path.join(vault, "visible.md"),
                "[[00-Rules/_TEMPLATE]]\n",
            )

            self.assertEqual(run(vault), [])


def write_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


if __name__ == "__main__":
    unittest.main()
