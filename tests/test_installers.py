import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import install_codex
from install_claude import (
    install_claude_patch,
    install_hooks as install_claude_hooks,
    load_settings,
)
from install_codex import (
    install_agents_patch,
    install_hooks as install_codex_hooks,
    load_hooks,
)
from install_zcode import install_zcode_context
from context_install import (
    MANAGED_END,
    MANAGED_START,
    atomic_write_utf8_text_exact,
    extract_managed_patch,
    merge_managed_patch,
)


class InstallerTests(unittest.TestCase):
    def test_context_atomic_write_does_not_follow_planted_legacy_tmp_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "AGENTS.md"
            sentinel = root / "outside.txt"
            sentinel.write_text("outside sentinel", encoding="utf-8")
            target.with_suffix(".md.tmp").symlink_to(sentinel)

            atomic_write_utf8_text_exact(target, "managed content\r\n")

            self.assertEqual(sentinel.read_text(encoding="utf-8"), "outside sentinel")
            self.assertFalse(target.is_symlink())
            self.assertEqual(target.read_bytes(), b"managed content\r\n")

    def test_managed_patch_enforces_formal_memory_lifecycle_authority(self):
        content = read_text(
            os.path.join(REPO_ROOT, "patches", "AGENT_MEMORY_BEACON.md.patch")
        )

        self.assertIn("Formal Memory Lifecycle Authority", content)
        self.assertIn("explicit user instruction", content)
        self.assertIn("inferred conflict", content)
        self.assertIn("lifecycle-proposals", content)
        self.assertIn("--expected-revision", content)
        self.assertIn("--apply", content)
        self.assertIn("must not physically delete", content)
        self.assertIn(
            "~/.local/share/agent-memory-beacon/runtime/.venv/bin/python",
            content,
        )
        self.assertNotIn("may automatically retract", content)

    def test_managed_patch_requires_durable_high_quality_annotations(self):
        content = read_text(
            os.path.join(REPO_ROOT, "patches", "AGENT_MEMORY_BEACON.md.patch")
        )

        self.assertIn("annotation is a memory proposal", content)
        self.assertIn("Expected TDD RED", content)
        self.assertIn("root cause, corrective action, and verification", content)
        self.assertIn("one annotation per distinct durable fact", content)
        self.assertNotIn("Appended to EVERY technical decision", content)
        self.assertNotIn("resolving ANY error", content)

    def test_bare_marker_identifiers_are_preserved_in_plain_prose(self):
        patch_text = f"{MANAGED_START}\nfresh managed body\n{MANAGED_END}"
        identifiers = (
            "AGENT_MEMORY_BEACON:MANAGED_START",
            "AGENT_MEMORY_BEACON:MANAGED_END",
            "KNOWLEDGE_BRAIN:MANAGED_START",
            "KNOWLEDGE_BRAIN:MANAGED_END",
        )
        variants = (
            ("lf", "\n", "\n", "\n"),
            ("crlf", "\r\n", "\r\n", "\r\n"),
            ("eof", "\n", "", "\n\n"),
        )

        for identifier in identifiers:
            for variant, newline, ending, separator in variants:
                with self.subTest(identifier=identifier, variant=variant):
                    existing = (
                        f"plain preface{newline}"
                        f"<!-- documentation names {identifier} -->{newline}"
                        f"documentation names {identifier}{ending}"
                    )

                    updated, action = merge_managed_patch(existing, patch_text)

                    self.assertEqual(action, "added")
                    self.assertEqual(
                        updated,
                        existing + separator + patch_text + "\n",
                    )
                    self.assertEqual(updated[:len(existing)], existing)

                    repeated, repeated_action = merge_managed_patch(
                        updated,
                        patch_text,
                    )
                    self.assertEqual(repeated_action, "current")
                    self.assertEqual(repeated, updated)

    def test_bare_marker_identifiers_are_preserved_inside_managed_and_compiled_bodies(self):
        identifiers = (
            "AGENT_MEMORY_BEACON:MANAGED_START",
            "AGENT_MEMORY_BEACON:MANAGED_END",
            "KNOWLEDGE_BRAIN:MANAGED_START",
            "KNOWLEDGE_BRAIN:MANAGED_END",
        )
        managed_prose = "\n".join(
            f"managed documentation names {identifier}"
            for identifier in identifiers
        )
        patch_text = (
            f"{MANAGED_START}\n"
            f"{managed_prose}\n"
            "<!-- COMPILED:RULES_START -->\n"
            "fresh rules\n"
            "<!-- COMPILED:RULES_END -->\n"
            "<!-- COMPILED:PROJECTS_START -->\n"
            "fresh projects\n"
            "<!-- COMPILED:PROJECTS_END -->\n"
            f"{MANAGED_END}"
        )
        namespaces = (
            (
                "current",
                MANAGED_START,
                MANAGED_END,
            ),
            (
                "legacy",
                "<!-- KNOWLEDGE_BRAIN:MANAGED_START version=2 -->",
                "<!-- KNOWLEDGE_BRAIN:MANAGED_END -->",
            ),
        )
        variants = (
            ("lf", "\n", "\nsuffix hard break  \n"),
            ("crlf", "\r\n", "\r\nsuffix hard break  \r\n"),
            ("eof", "\n", ""),
        )

        for namespace, start, end in namespaces:
            for variant, newline, suffix in variants:
                with self.subTest(namespace=namespace, variant=variant):
                    compiled_rules_body = (
                        newline
                        + newline.join(
                            (
                                *identifiers,
                                (
                                    "<!-- documentation names "
                                    "AGENT_MEMORY_BEACON:MANAGED_START -->"
                                ),
                            )
                        )
                        + newline
                    )
                    compiled_projects_body = (
                        newline
                        + newline.join(reversed(identifiers))
                        + newline
                    )
                    prefix = f"prefix hard break  {newline}{newline}"
                    existing = (
                        prefix
                        + start
                        + newline
                        + "stale managed prose"
                        + newline
                        + "<!-- COMPILED:RULES_START -->"
                        + compiled_rules_body
                        + "<!-- COMPILED:RULES_END -->"
                        + newline
                        + "<!-- COMPILED:PROJECTS_START -->"
                        + compiled_projects_body
                        + "<!-- COMPILED:PROJECTS_END -->"
                        + newline
                        + end
                        + suffix
                    )

                    updated, action = merge_managed_patch(existing, patch_text)

                    self.assertEqual(action, "updated")
                    self.assertTrue(updated.startswith(prefix + MANAGED_START))
                    self.assertTrue(updated.endswith(suffix))
                    self.assertIn(managed_prose, updated)
                    self.assertEqual(
                        body_between(
                            updated,
                            "<!-- COMPILED:RULES_START -->",
                            "<!-- COMPILED:RULES_END -->",
                        ),
                        compiled_rules_body,
                    )
                    self.assertEqual(
                        body_between(
                            updated,
                            "<!-- COMPILED:PROJECTS_START -->",
                            "<!-- COMPILED:PROJECTS_END -->",
                        ),
                        compiled_projects_body,
                    )

                    repeated, repeated_action = merge_managed_patch(
                        updated,
                        patch_text,
                    )
                    self.assertEqual(repeated_action, "current")
                    self.assertEqual(repeated, updated)

    def test_end_marker_horizontal_whitespace_is_outside_one_stable_match(self):
        patch_text = f"{MANAGED_START}\nfresh managed body\n{MANAGED_END}"
        namespaces = (
            (
                "current",
                MANAGED_START,
                MANAGED_END,
            ),
            (
                "legacy",
                "<!-- KNOWLEDGE_BRAIN:MANAGED_START version=2 -->",
                "<!-- KNOWLEDGE_BRAIN:MANAGED_END -->",
            ),
        )
        variants = (
            ("lf-spaces", "\n", "  ", "\nsentinel suffix  \n"),
            ("crlf-tab", "\r\n", "\t", "\r\nsentinel suffix  \r\n"),
            ("eof-spaces", "\n", "  ", ""),
        )

        for namespace, start, end in namespaces:
            for variant, newline, trailing, tail in variants:
                with self.subTest(namespace=namespace, variant=variant):
                    prefix = f"prefix hard break  {newline}{newline}"
                    suffix = trailing + tail
                    existing = (
                        prefix
                        + start
                        + newline
                        + "stale managed body"
                        + newline
                        + end
                        + suffix
                    )

                    updated, action = merge_managed_patch(existing, patch_text)

                    self.assertEqual(action, "updated")
                    self.assertEqual(updated, prefix + patch_text + suffix)
                    self.assertEqual(updated.count(MANAGED_START), 1)
                    self.assertEqual(updated.count(MANAGED_END), 1)
                    self.assertEqual(extract_managed_patch(updated), patch_text)

                    repeated, repeated_action = merge_managed_patch(
                        updated,
                        patch_text,
                    )
                    self.assertEqual(repeated_action, "current")
                    self.assertEqual(repeated, updated)

    def test_unmatched_malformed_cross_namespace_and_nested_markers_fail_closed(self):
        patch_text = f"{MANAGED_START}\nfresh managed body\n{MANAGED_END}"
        legacy_start = "<!-- KNOWLEDGE_BRAIN:MANAGED_START version=2 -->"
        legacy_end = "<!-- KNOWLEDGE_BRAIN:MANAGED_END -->"
        cases = {
            "unmatched-current-start": f"prefix\n{MANAGED_START}\nbody\nsuffix\n",
            "unmatched-current-end": f"prefix\n{MANAGED_END}\nsuffix\n",
            "cross-namespace": (
                f"{MANAGED_START}\nbody\n{legacy_end}\n"
            ),
            "nested-current": (
                f"{MANAGED_START}\n{MANAGED_START}\nbody\n"
                f"{MANAGED_END}\n{MANAGED_END}\n"
            ),
            "malformed-start": (
                "<!-- AGENT_MEMORY_BEACON:MANAGED_START version=3\nbody\n"
            ),
            "malformed-end": (
                f"{legacy_start}\nbody\n"
                "<!-- KNOWLEDGE_BRAIN:MANAGED_END extra -->\n"
            ),
        }

        for name, existing in cases.items():
            with self.subTest(name=name):
                original = existing
                with self.assertRaisesRegex(ValueError, "managed marker"):
                    merge_managed_patch(existing, patch_text)
                self.assertEqual(existing, original)

    def test_actual_installers_preserve_bytes_and_backups_across_two_runs(self):
        legacy_start = "<!-- KNOWLEDGE_BRAIN:MANAGED_START version=2 -->"
        legacy_end = "<!-- KNOWLEDGE_BRAIN:MANAGED_END -->"
        update_variants = (
            ("lf-spaces", "\n", "  ", "\nsentinel suffix  \n"),
            ("crlf-tab", "\r\n", "\t", "\r\nsentinel suffix  \r\n"),
            ("eof-spaces", "\n", "  ", ""),
        )
        add_variants = (
            ("lf", b"plain prefix hard break  \n\nsentinel\n"),
            ("crlf", b"plain prefix hard break  \r\n\r\nsentinel\r\n"),
            ("eof", b"plain content at EOF  "),
        )

        def invoke(installer, target):
            if installer == "codex":
                return install_agents_patch(target)
            if installer == "claude":
                return install_claude_patch(target)
            return install_zcode_context(
                {"zcode_home": str(target.parent)},
                target=target,
            )

        for installer in ("codex", "claude", "zcode"):
            for variant, newline, trailing, tail in update_variants:
                with self.subTest(
                    installer=installer,
                    mode="update",
                    variant=variant,
                ), tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp) / f"{installer}-update.md"
                    prefix = f"prefix hard break  {newline}{newline}"
                    suffix = trailing + tail
                    original = (
                        prefix
                        + legacy_start
                        + newline
                        + "legacy managed body"
                        + newline
                        + legacy_end
                        + suffix
                    ).encode("utf-8")
                    target.write_bytes(original)

                    invoke(installer, target)
                    first = target.read_bytes()
                    marker_start = first.index(MANAGED_START.encode("utf-8"))
                    marker_end = (
                        first.index(MANAGED_END.encode("utf-8"), marker_start)
                        + len(MANAGED_END.encode("utf-8"))
                    )
                    self.assertEqual(first[:marker_start], prefix.encode("utf-8"))
                    self.assertEqual(first[marker_end:], suffix.encode("utf-8"))
                    self.assertEqual(first.count(MANAGED_START.encode("utf-8")), 1)
                    self.assertEqual(first.count(MANAGED_END.encode("utf-8")), 1)
                    backups = sorted(target.parent.glob(target.name + ".bak-*"))
                    self.assertEqual(len(backups), 1)
                    self.assertEqual(backups[0].read_bytes(), original)

                    invoke(installer, target)
                    self.assertEqual(target.read_bytes(), first)
                    self.assertEqual(
                        sorted(target.parent.glob(target.name + ".bak-*")),
                        backups,
                    )

            for variant, original in add_variants:
                with self.subTest(
                    installer=installer,
                    mode="add",
                    variant=variant,
                ), tempfile.TemporaryDirectory() as tmp:
                    target = Path(tmp) / f"{installer}-add.md"
                    target.write_bytes(original)

                    invoke(installer, target)
                    first = target.read_bytes()
                    self.assertTrue(first.startswith(original))
                    self.assertEqual(first.count(MANAGED_START.encode("utf-8")), 1)
                    self.assertEqual(first.count(MANAGED_END.encode("utf-8")), 1)
                    backups = sorted(target.parent.glob(target.name + ".bak-*"))
                    self.assertEqual(len(backups), 1)
                    self.assertEqual(backups[0].read_bytes(), original)

                    invoke(installer, target)
                    self.assertEqual(target.read_bytes(), first)
                    self.assertEqual(
                        sorted(target.parent.glob(target.name + ".bak-*")),
                        backups,
                    )

    def test_current_outer_update_preserves_exact_user_prefix_and_suffix_bytes(self):
        patch_text = f"{MANAGED_START}\nfresh managed body\n{MANAGED_END}"
        prefix = "before hard break  \r\n\r\n\r\n"
        managed = (
            f"{MANAGED_START}\r\nstale managed body\r\n{MANAGED_END}"
        )
        suffix = "\r\n\r\n\r\nafter hard break  \r\n"
        existing = prefix + managed + suffix

        updated, action = merge_managed_patch(existing, patch_text)

        self.assertEqual(action, "updated")
        self.assertEqual(updated, prefix + patch_text + suffix)

    def test_legacy_outer_update_preserves_exact_user_bytes_at_eof(self):
        patch_text = f"{MANAGED_START}\nfresh managed body\n{MANAGED_END}"
        legacy_start = "<!-- KNOWLEDGE_BRAIN:MANAGED_START version=2 -->"
        legacy_end = "<!-- KNOWLEDGE_BRAIN:MANAGED_END -->"
        prefix = "before hard break  \n\n\n"
        managed = f"{legacy_start}\nlegacy managed body\n{legacy_end}"
        suffix = "\n\n\nafter hard break at EOF  "
        existing = prefix + managed + suffix

        updated, action = merge_managed_patch(existing, patch_text)

        self.assertEqual(action, "updated")
        self.assertEqual(updated, prefix + patch_text + suffix)

    def test_add_mode_preserves_existing_bytes_and_adds_only_minimal_separator(self):
        patch_text = f"{MANAGED_START}\nfresh managed body\n{MANAGED_END}"
        cases = (
            ("content-at-eof", "user content at EOF  ", "\n\n"),
            ("single-crlf", "user hard break  \r\n", "\r\n"),
            ("multiple-crlf", "user content\r\n\r\n\r\n", ""),
        )

        for name, existing, separator in cases:
            with self.subTest(name=name):
                updated, action = merge_managed_patch(existing, patch_text)

                self.assertEqual(action, "added")
                self.assertEqual(
                    updated,
                    existing + separator + patch_text + "\n",
                )

    def test_zcode_installer_writes_user_agents_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            actions = install_zcode_context(
                {"zcode_home": os.path.join(tmp, ".zcode")}
            )
            target = os.path.join(tmp, ".zcode", "AGENTS.md")

            self.assertTrue(os.path.exists(target))
            self.assertIn("AGENT_MEMORY_BEACON:MANAGED_START", read_text(target))
            self.assertTrue(any("WROTE" in action for action in actions))

    def test_installer_upgrades_legacy_patch_and_preserves_surrounding_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents_path = Path(tmp) / "AGENTS.md"
            write_text(
                agents_path,
                """custom prefix

## Agent Memory Vault — Auto-Maintained Blocks

legacy protocol

| Template | Path |
|----------|------|
| Pitfalls log | `01-Projects/{project}/Memory/pitfalls.md` |

custom suffix
""",
            )

            actions = install_agents_patch(agents_path)
            content = read_text(agents_path)

            self.assertTrue(any("UPDATED" in action for action in actions))
            self.assertIn("AGENT_MEMORY_BEACON:MANAGED_START", content)
            self.assertIn("[FAVOR:", content)
            self.assertIn("custom prefix", content)
            self.assertIn("custom suffix", content)
            self.assertNotIn("legacy protocol", content)

    def test_old_outer_marker_upgrades_and_keeps_compiled_memory(self):
        existing = """prefix
<!-- KNOWLEDGE_BRAIN:MANAGED_START version=2 -->
## Agent Memory Vault - Auto-Maintained Blocks
<!-- COMPILED:RULES_START -->
old rule body
<!-- COMPILED:RULES_END -->
<!-- COMPILED:PROJECTS_START -->
old project body
<!-- COMPILED:PROJECTS_END -->
<!-- KNOWLEDGE_BRAIN:MANAGED_END -->
suffix
"""
        patch = read_text(
            os.path.join(REPO_ROOT, "patches", "AGENT_MEMORY_BEACON.md.patch")
        )

        updated, action = merge_managed_patch(existing, patch)

        self.assertEqual(action, "updated")
        self.assertIn(MANAGED_START, updated)
        self.assertIn(MANAGED_END, updated)
        self.assertIn("old rule body", updated)
        self.assertIn("old project body", updated)
        self.assertIn("prefix", updated)
        self.assertIn("suffix", updated)
        self.assertNotIn("KNOWLEDGE_BRAIN:MANAGED_START", updated)

    def test_mixed_old_and_new_outer_blocks_are_rejected(self):
        existing = """<!-- KNOWLEDGE_BRAIN:MANAGED_START version=2 -->
legacy
<!-- KNOWLEDGE_BRAIN:MANAGED_END -->
<!-- AGENT_MEMORY_BEACON:MANAGED_START version=3 -->
current
<!-- AGENT_MEMORY_BEACON:MANAGED_END -->
"""
        patch = read_text(
            os.path.join(REPO_ROOT, "patches", "AGENT_MEMORY_BEACON.md.patch")
        )

        with self.assertRaisesRegex(ValueError, "multiple managed blocks"):
            merge_managed_patch(existing, patch)

    def test_installers_reject_mixed_blocks_without_writing_or_backing_up(self):
        existing = """<!-- KNOWLEDGE_BRAIN:MANAGED_START version=2 -->
legacy
<!-- KNOWLEDGE_BRAIN:MANAGED_END -->
<!-- AGENT_MEMORY_BEACON:MANAGED_START version=3 -->
current
<!-- AGENT_MEMORY_BEACON:MANAGED_END -->
"""
        installers = (
            ("codex", install_agents_patch, "AGENTS.md"),
            ("claude", install_claude_patch, "CLAUDE.md"),
        )

        for name, installer, filename in installers:
            with self.subTest(installer=name), tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / filename
                write_text(target, existing)
                before = target.read_bytes()

                with self.assertRaisesRegex(ValueError, "multiple managed blocks"):
                    installer(target)

                self.assertEqual(target.read_bytes(), before)
                self.assertEqual(list(target.parent.glob(f"{filename}.bak-*")), [])

    def test_duplicate_current_outer_blocks_are_rejected(self):
        existing = """<!-- AGENT_MEMORY_BEACON:MANAGED_START version=3 -->
one
<!-- AGENT_MEMORY_BEACON:MANAGED_END -->
<!-- AGENT_MEMORY_BEACON:MANAGED_START version=3 -->
two
<!-- AGENT_MEMORY_BEACON:MANAGED_END -->
"""
        patch = read_text(
            os.path.join(REPO_ROOT, "patches", "AGENT_MEMORY_BEACON.md.patch")
        )

        with self.assertRaisesRegex(ValueError, "multiple managed blocks"):
            merge_managed_patch(existing, patch)

    def test_duplicate_legacy_outer_blocks_are_rejected(self):
        existing = """<!-- KNOWLEDGE_BRAIN:MANAGED_START version=2 -->
one
<!-- KNOWLEDGE_BRAIN:MANAGED_END -->
<!-- KNOWLEDGE_BRAIN:MANAGED_START version=2 -->
two
<!-- KNOWLEDGE_BRAIN:MANAGED_END -->
"""
        patch = read_text(
            os.path.join(REPO_ROOT, "patches", "AGENT_MEMORY_BEACON.md.patch")
        )

        with self.assertRaisesRegex(ValueError, "multiple managed blocks"):
            merge_managed_patch(existing, patch)

    def test_claude_session_start_keeps_all_configured_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch("install_claude.Path.home", return_value=Path(tmp)):
                actions = install_claude_hooks(
                    {"python_path": "/usr/bin/python3"},
                    dry_run=True,
                )

            start = next(
                action for action in actions
                if "ADD hooks.SessionStart" in action
            )
            stop = next(
                action for action in actions
                if "ADD hooks.Stop" in action
            )
            self.assertIn("--mode start", start)
            self.assertNotIn("--agent", start)
            self.assertIn("--mode stop --agent claude", stop)

    def test_claude_hooks_migrate_to_stable_runtime_and_survive_source_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_home = root / ".claude"
            source_scripts = root / "source-checkout" / "scripts"
            stable_scripts = root / "stable-runtime" / "scripts"
            marker = root / "stable-hook-ran"
            claude_home.mkdir()
            source_scripts.mkdir(parents=True)
            stable_scripts.mkdir(parents=True)
            old_harvester = source_scripts / "session_harvester.py"
            stable_harvester = stable_scripts / "session_harvester.py"
            old_harvester.write_text("# old source\n", encoding="utf-8")
            stable_harvester.write_text(
                "from pathlib import Path\n"
                f"Path({str(marker)!r}).write_text('ok', encoding='utf-8')\n",
                encoding="utf-8",
            )
            third_party = {
                "matcher": "third-party",
                "hooks": [{"type": "command", "command": "node /third.js"}],
            }
            old_owned = {
                "matcher": "owned-position",
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            f'"{sys.executable}" "{old_harvester}" '
                            "--mode stop --agent claude"
                        ),
                        "timeout": 120,
                        "keep": "metadata",
                    }
                ],
            }
            settings = claude_home / "settings.json"
            write_json(
                settings,
                {
                    "custom": {"preserve": True},
                    "hooks": {"Stop": [third_party, old_owned]},
                },
            )

            install_claude_hooks(
                {
                    "user_home": str(root),
                    "python_path": sys.executable,
                },
                scripts_dir=stable_scripts,
                migration_scripts_dir=source_scripts,
                create_backups=False,
            )

            installed = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual(installed["custom"], {"preserve": True})
            self.assertEqual(installed["hooks"]["Stop"][0], third_party)
            migrated = installed["hooks"]["Stop"][1]
            self.assertEqual(migrated["matcher"], "owned-position")
            self.assertEqual(migrated["hooks"][0]["keep"], "metadata")
            command = migrated["hooks"][0]["command"]
            self.assertIn("AGENT_MEMORY_BEACON_HOOK=1", command)
            self.assertIn(str(stable_harvester), command)
            self.assertNotIn(str(source_scripts), command)
            self.assertEqual(shlex.split(command)[2], "-B")

            shutil.rmtree(source_scripts.parent)
            subprocess.run(command, check=True, shell=True)
            self.assertEqual(marker.read_text(encoding="utf-8"), "ok")

    def test_codex_hooks_append_prompt_group_without_reordering_third_party(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".codex"
            home.mkdir()
            hooks_path = home / "hooks.json"
            config_toml = home / "config.toml"
            third_party = {
                "matcher": "all",
                "hooks": [
                    {
                        "type": "command",
                        "command": "node /third/prompt.js --label codex_prompt_hook.py",
                        "timeout": 7,
                        "third_party": True,
                    }
                ],
            }
            pre_tool = {
                "hooks": [{"type": "command", "command": "node /third/pre.js"}]
            }
            original = {
                "custom": {"preserve": [1, 2, 3]},
                "hooks": {
                    "UserPromptSubmit": [third_party],
                    "PreToolUse": [pre_tool],
                },
            }
            write_json(hooks_path, original)
            os.chmod(hooks_path, 0o600)
            config_toml.write_text("model = 'local'\n", encoding="utf-8")

            actions = install_codex_hooks(
                {"codex_home": str(home), "python_path": "/usr/bin/python3"}
            )
            installed = json.loads(hooks_path.read_text(encoding="utf-8"))
            groups = installed["hooks"]["UserPromptSubmit"]
            backups = list(home.glob("hooks.json.bak-*"))

            self.assertEqual(installed["custom"], original["custom"])
            self.assertEqual(installed["hooks"]["PreToolUse"], [pre_tool])
            self.assertEqual(groups[0], third_party)
            self.assertEqual(len(groups), 2)
            own = groups[1]["hooks"][0]
            self.assertEqual(own["type"], "command")
            self.assertIn("codex_prompt_hook.py", own["command"])
            self.assertEqual(shlex.split(own["command"])[2], "-B")
            self.assertEqual(own["timeout"], 2)
            self.assertEqual(stat.S_IMODE(hooks_path.stat().st_mode), 0o600)
            self.assertEqual(config_toml.read_text(encoding="utf-8"), "model = 'local'\n")
            self.assertEqual(len(backups), 1)
            self.assertTrue(any("REVIEW Codex /hooks" in action for action in actions))

            second_actions = install_codex_hooks(
                {"codex_home": str(home), "python_path": "/usr/bin/python3"}
            )

            self.assertEqual(
                json.loads(hooks_path.read_text(encoding="utf-8")),
                installed,
            )
            self.assertEqual(len(list(home.glob("hooks.json.bak-*"))), 1)
            self.assertTrue(
                any("OK hooks.UserPromptSubmit" in action for action in second_actions)
            )

    def test_codex_stale_owned_hooks_are_updated_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".codex"
            home.mkdir()
            hooks_path = home / "hooks.json"
            prompt_groups = [
                {"hooks": [{"type": "command", "command": "node /first.js"}]},
                {
                    "matcher": "owned-position",
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'AGENT_MEMORY_BEACON_HOOK=1 "/old/python" "/old/repo/scripts/codex_prompt_hook.py"',
                            "timeout": 9,
                            "keep": "metadata",
                        }
                    ],
                },
                {"hooks": [{"type": "command", "command": "node /last.js"}]},
            ]
            stop_groups = [
                {"hooks": [{"type": "command", "command": "node /stop-first.js"}]},
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": 'AGENT_MEMORY_BEACON_HOOK=1 "/old/python" "/old/repo/scripts/session_harvester.py" --mode stop --agent codex',
                            "timeout": 30,
                        }
                    ]
                },
                {"hooks": [{"type": "command", "command": "node /stop-last.js"}]},
            ]
            write_json(
                hooks_path,
                {
                    "hooks": {
                        "UserPromptSubmit": prompt_groups,
                        "Stop": stop_groups,
                    }
                },
            )

            install_codex_hooks(
                {"codex_home": str(home), "python_path": "/usr/bin/python3"}
            )
            installed = json.loads(hooks_path.read_text(encoding="utf-8"))
            prompt_after = installed["hooks"]["UserPromptSubmit"]
            stop_after = installed["hooks"]["Stop"]

            self.assertEqual(len(prompt_after), 3)
            self.assertEqual(prompt_after[0], prompt_groups[0])
            self.assertEqual(prompt_after[2], prompt_groups[2])
            self.assertEqual(prompt_after[1]["matcher"], "owned-position")
            self.assertEqual(prompt_after[1]["hooks"][0]["keep"], "metadata")
            self.assertIn(
                "codex_prompt_hook.py",
                prompt_after[1]["hooks"][0]["command"],
            )
            self.assertEqual(prompt_after[1]["hooks"][0]["timeout"], 2)
            self.assertEqual(len(stop_after), 3)
            self.assertEqual(stop_after[0], stop_groups[0])
            self.assertEqual(stop_after[2], stop_groups[2])
            self.assertIn(
                "session_harvester.py",
                stop_after[1]["hooks"][0]["command"],
            )

    def test_codex_hooks_with_custom_absolute_executable_are_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".codex"
            home.mkdir()
            runtime = Path(tmp) / "cpython-runtime"
            runtime.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            runtime.chmod(0o700)

            install_codex_hooks(
                {"codex_home": str(home), "python_path": str(runtime)}
            )
            first = (home / "hooks.json").read_bytes()
            second_actions = install_codex_hooks(
                {"codex_home": str(home), "python_path": str(runtime)}
            )

            self.assertEqual((home / "hooks.json").read_bytes(), first)
            self.assertEqual(len(list(home.glob("hooks.json.bak-*"))), 0)
            self.assertTrue(
                all(
                    any(f"OK hooks.{event}" in action for action in second_actions)
                    for event in ("Stop", "SessionStart", "UserPromptSubmit")
                )
            )

    def test_codex_unmarked_exact_source_checkout_hooks_migrate_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".codex"
            home.mkdir()
            legacy_scripts = Path(tmp) / "obsidian-knowledge-brain" / "scripts"
            stable_scripts = Path(tmp) / "stable-runtime" / "scripts"
            old_harvester = legacy_scripts / "session_harvester.py"
            old_prompt = legacy_scripts / "codex_prompt_hook.py"
            before = {"hooks": [{"type": "command", "command": "node /before.js"}]}
            legacy = {
                "matcher": "legacy-owner-position",
                "hooks": [
                    {
                        "type": "command",
                        "command": f'"{sys.executable}" "{old_harvester}" --mode stop',
                        "timeout": 120,
                        "keep": "metadata",
                    }
                ],
            }
            after = {"hooks": [{"type": "command", "command": "node /after.js"}]}
            write_json(
                home / "hooks.json",
                {
                    "hooks": {
                        "Stop": [before, legacy, after],
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": (
                                            f'"{sys.executable}" "{old_harvester}" '
                                            "--mode start"
                                        ),
                                    }
                                ]
                            }
                        ],
                        "UserPromptSubmit": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f'"{sys.executable}" "{old_prompt}"',
                                    }
                                ]
                            }
                        ],
                    }
                },
            )

            install_codex_hooks(
                {"codex_home": str(home), "python_path": sys.executable},
                scripts_dir=stable_scripts,
                migration_scripts_dir=legacy_scripts,
            )
            installed = json.loads((home / "hooks.json").read_text(encoding="utf-8"))

            stop = installed["hooks"]["Stop"]
            self.assertEqual(stop[0], before)
            self.assertEqual(stop[2], after)
            self.assertEqual(stop[1]["matcher"], "legacy-owner-position")
            self.assertEqual(stop[1]["hooks"][0]["keep"], "metadata")
            self.assertIn(
                "AGENT_MEMORY_BEACON_HOOK=1",
                stop[1]["hooks"][0]["command"],
            )
            self.assertIn(str(stable_scripts), stop[1]["hooks"][0]["command"])
            self.assertNotIn(str(legacy_scripts), stop[1]["hooks"][0]["command"])
            self.assertIn("--mode stop --agent codex", stop[1]["hooks"][0]["command"])
            self.assertEqual(len(stop), 3)
            self.assertEqual(len(installed["hooks"]["SessionStart"]), 1)
            self.assertIn(
                "AGENT_MEMORY_BEACON_HOOK=1",
                installed["hooks"]["SessionStart"][0]["hooks"][0]["command"],
            )
            self.assertEqual(len(installed["hooks"]["UserPromptSubmit"]), 1)
            self.assertIn(
                "AGENT_MEMORY_BEACON_HOOK=1",
                installed["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"],
            )

    def test_codex_preserves_unmarked_third_party_in_legacy_named_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".codex"
            home.mkdir()
            third_party_script = (
                Path(tmp)
                / "vendor"
                / "obsidian-knowledge-brain"
                / "scripts"
                / "codex_prompt_hook.py"
            )
            third_party = {
                "matcher": "third-party-position",
                "hooks": [
                    {
                        "type": "command",
                        "command": f'"{sys.executable}" "{third_party_script}"',
                        "timeout": 17,
                        "owner": "third-party",
                    }
                ],
            }
            write_json(
                home / "hooks.json",
                {
                    "hooks": {
                        "UserPromptSubmit": [
                            {"hooks": [{"type": "command", "command": "node /before.js"}]},
                            third_party,
                            {"hooks": [{"type": "command", "command": "node /after.js"}]},
                        ]
                    }
                },
            )

            install_codex_hooks(
                {"codex_home": str(home), "python_path": sys.executable}
            )
            groups = json.loads((home / "hooks.json").read_text(encoding="utf-8"))["hooks"][
                "UserPromptSubmit"
            ]

            self.assertEqual(groups[1], third_party)
            self.assertEqual(len(groups), 4)
            self.assertIn(
                "AGENT_MEMORY_BEACON_HOOK=1",
                groups[3]["hooks"][0]["command"],
            )

    def test_codex_unmarked_current_checkout_hooks_upgrade_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".codex"
            home.mkdir()
            harvester = Path(SCRIPTS_DIR) / "session_harvester.py"
            prompt_hook = Path(SCRIPTS_DIR) / "codex_prompt_hook.py"
            write_json(
                home / "hooks.json",
                {
                    "hooks": {
                        "Stop": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": (
                                            f'"{sys.executable}" "{harvester}" '
                                            "--mode stop --agent codex"
                                        ),
                                        "timeout": 120,
                                    }
                                ]
                            }
                        ],
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": (
                                            f'"{sys.executable}" "{harvester}" '
                                            "--mode start"
                                        ),
                                        "timeout": 120,
                                    }
                                ]
                            }
                        ],
                        "UserPromptSubmit": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": f'"{sys.executable}" "{prompt_hook}"',
                                        "timeout": 2,
                                    }
                                ]
                            }
                        ],
                    }
                },
            )

            install_codex_hooks(
                {"codex_home": str(home), "python_path": sys.executable}
            )
            installed = json.loads((home / "hooks.json").read_text(encoding="utf-8"))

            for event in ("Stop", "SessionStart", "UserPromptSubmit"):
                self.assertEqual(len(installed["hooks"][event]), 1)
                self.assertIn(
                    "AGENT_MEMORY_BEACON_HOOK=1",
                    installed["hooks"][event][0]["hooks"][0]["command"],
                )

    def test_codex_installer_rejects_relative_or_nonexecutable_python_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".codex"
            home.mkdir()
            nonexecutable = Path(tmp) / "not-executable"
            nonexecutable.write_text("#!/bin/sh\n", encoding="utf-8")

            for python_path in ("relative-python", str(nonexecutable)):
                with self.subTest(python_path=python_path):
                    with self.assertRaisesRegex(ValueError, "absolute executable"):
                        install_codex_hooks(
                            {"codex_home": str(home), "python_path": python_path}
                        )
                    self.assertFalse((home / "hooks.json").exists())

    def test_codex_installer_preserves_unmarked_same_basename_third_party_hooks(self):
        third_party_commands = (
            '"/usr/bin/python3" "/opt/third-party/codex_prompt_hook.py"',
            '"/opt/bin/python-wrapper" "/opt/third-party/codex_prompt_hook.py"',
        )
        for command in third_party_commands:
            with self.subTest(command=command), tempfile.TemporaryDirectory() as tmp:
                home = Path(tmp) / ".codex"
                home.mkdir()
                hooks_path = home / "hooks.json"
                third_party = {
                    "hooks": [
                        {
                            "type": "command",
                            "command": command,
                            "timeout": 17,
                            "owner": "third-party",
                        }
                    ]
                }
                write_json(
                    hooks_path,
                    {"hooks": {"UserPromptSubmit": [third_party]}},
                )

                install_codex_hooks(
                    {"codex_home": str(home), "python_path": "/usr/bin/python3"}
                )
                groups = json.loads(hooks_path.read_text(encoding="utf-8"))["hooks"][
                    "UserPromptSubmit"
                ]

                self.assertEqual(groups[0], third_party)
                self.assertEqual(len(groups), 2)
                self.assertIn(
                    "AGENT_MEMORY_BEACON_HOOK=1",
                    groups[1]["hooks"][0]["command"],
                )

    def test_codex_installer_rejects_timeout_divergence_from_two_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".codex"
            home.mkdir()

            with self.assertRaisesRegex(ValueError, "2000"):
                install_codex_hooks(
                    {
                        "codex_home": str(home),
                        "python_path": "/usr/bin/python3",
                        "memory_runtime": {"hook_timeout_ms": 10000},
                    }
                )

            self.assertFalse((home / "hooks.json").exists())

    def test_codex_hook_dry_run_does_not_write_or_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / ".codex"
            home.mkdir()
            hooks_path = home / "hooks.json"
            write_json(hooks_path, {"hooks": {"UserPromptSubmit": []}})
            before = hooks_path.read_bytes()

            actions = install_codex_hooks(
                {"codex_home": str(home), "python_path": "/usr/bin/python3"},
                dry_run=True,
            )

            self.assertEqual(hooks_path.read_bytes(), before)
            self.assertEqual(list(home.glob("hooks.json.bak-*")), [])
            self.assertTrue(any("DRY-RUN" in action for action in actions))

    def test_codex_runtime_install_uses_explicit_scripts_and_skips_side_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".codex"
            scripts = root / "runtime" / "scripts"
            scripts.mkdir(parents=True)
            for name in ("session_harvester.py", "codex_prompt_hook.py"):
                (scripts / name).write_text("# fixture\n", encoding="utf-8")
            home.mkdir()
            hooks_path = home / "hooks.json"
            write_json(hooks_path, {"hooks": {}})

            install_codex_hooks(
                {"codex_home": str(home), "python_path": "/usr/bin/python3"},
                scripts_dir=scripts,
                create_backups=False,
            )

            commands = hooks_path.read_text(encoding="utf-8")
            self.assertIn(str(scripts / "session_harvester.py"), commands)
            self.assertIn(str(scripts / "codex_prompt_hook.py"), commands)
            self.assertEqual(list(home.glob("hooks.json.bak-*")), [])

    def test_codex_hook_write_refuses_parent_directory_swap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / ".codex"
            held_home = root / ".codex-held"
            outside = root / "outside"
            home.mkdir()
            outside.mkdir()
            hooks_path = home / "hooks.json"
            outside_hooks = outside / "hooks.json"
            write_json(hooks_path, {"hooks": {}})
            outside_hooks.write_bytes(b"outside sentinel")
            original_atomic_write = install_codex.atomic_write_json
            swapped = False

            def swap_parent_then_write(path, data, **kwargs):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    home.rename(held_home)
                    home.symlink_to(outside, target_is_directory=True)
                return original_atomic_write(path, data, **kwargs)

            with patch(
                "install_codex.atomic_write_json",
                side_effect=swap_parent_then_write,
            ):
                with self.assertRaisesRegex(OSError, "parent|symlink|replaced"):
                    install_codex_hooks(
                        {
                            "codex_home": str(home),
                            "python_path": "/usr/bin/python3",
                        },
                        create_backups=False,
                    )

            self.assertTrue(swapped)
            self.assertEqual(outside_hooks.read_bytes(), b"outside sentinel")
            self.assertEqual(
                (held_home / "hooks.json").read_text(encoding="utf-8"),
                json.dumps({"hooks": {}}, ensure_ascii=False, indent=2) + "\n",
            )

    def test_codex_runtime_install_uses_explicit_patch_without_side_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            agents = root / "AGENTS.md"
            patch_path = root / "AGENT_MEMORY_BEACON.md.patch"
            agents.write_text("user content\n", encoding="utf-8")
            patch_path.write_text(
                f"{MANAGED_START}\nruntime patch\n{MANAGED_END}\n",
                encoding="utf-8",
            )

            install_agents_patch(
                agents,
                patch_path=patch_path,
                create_backups=False,
            )

            self.assertIn("runtime patch", agents.read_text(encoding="utf-8"))
            self.assertEqual(list(root.glob("AGENTS.md.bak-*")), [])

    def test_codex_installer_rejects_malformed_nested_hook_shapes(self):
        malformed = (
            {"hooks": {"UserPromptSubmit": {}}},
            {"hooks": {"UserPromptSubmit": ["not-a-group"]}},
            {"hooks": {"UserPromptSubmit": [{"hooks": {}}]}},
            {"hooks": {"UserPromptSubmit": [{"hooks": ["not-a-hook"]}]}},
        )
        for payload in malformed:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp) / ".codex"
                    home.mkdir()
                    hooks_path = home / "hooks.json"
                    write_json(hooks_path, payload)
                    before = hooks_path.read_bytes()

                    with self.assertRaisesRegex(ValueError, "hooks"):
                        install_codex_hooks(
                            {
                                "codex_home": str(home),
                                "python_path": "/usr/bin/python3",
                            }
                        )

                    self.assertEqual(hooks_path.read_bytes(), before)
                    self.assertEqual(list(home.glob("hooks.json.bak-*")), [])

    def test_codex_installer_rejects_malformed_existing_hooks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "hooks.json")
            write_text(path, "{not-json")

            with self.assertRaisesRegex(ValueError, "malformed"):
                load_hooks(path_obj(path))

            self.assertEqual(read_text(path), "{not-json")

    def test_claude_installer_rejects_malformed_existing_settings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "settings.json")
            write_text(path, "{not-json")

            with self.assertRaisesRegex(ValueError, "malformed"):
                load_settings(path_obj(path))

            self.assertEqual(read_text(path), "{not-json")


def path_obj(path):
    from pathlib import Path

    return Path(path)


def write_text(path, content):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def write_json(path, payload):
    Path(path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def body_between(content, start, end):
    start_at = content.index(start) + len(start)
    end_at = content.index(end, start_at)
    return content[start_at:end_at]


if __name__ == "__main__":
    unittest.main()
