import json
import hashlib
import io
import inspect
import os
import re
import shutil
import subprocess
import stat
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import FrozenInstanceError, replace
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from brand_migration import (
    _count_project_field_refs,
    _rewritten_config_bytes,
    apply_brand_migration,
    atomic_write_json,
    build_migration_plan,
    create_migration_backup,
    memory_identity_keys,
    migration_writer_guard,
    plan_summary,
    rewrite_markdown,
    rollback_brand_migration,
    split_wikilink,
    validate_brand_migration,
)


class BrandMigrationTests(unittest.TestCase):
    def test_mapping_project_values_are_counted_rewritten_and_cycle_safe(self):
        old = "github-obsidian-knowledge-brain"
        new = "agent-memory-beacon"
        with tempfile.TemporaryDirectory() as raw_tmp:
            config = Path(raw_tmp) / "config.yaml"
            config.write_text(
                f"""projects:
  - &legacy
    name: {old}
    keywords:
      - {old}
      - legacy-checkout
    self: *legacy
  - name: unrelated
    keywords: [unrelated]
    description: mentions {old} as prose only
project_keywords:
  {old}: [legacy-top-level]
  {new}: [current-top-level]
""",
                encoding="utf-8",
            )
            parsed = yaml.safe_load(config.read_text(encoding="utf-8"))

            self.assertEqual(_count_project_field_refs(parsed, old), 1)

            migrated = yaml.safe_load(
                _rewritten_config_bytes(
                    SimpleNamespace(
                        config_path=config,
                        old_slug=old,
                        new_slug=new,
                    )
                )
            )
            legacy_mapping, unrelated = migrated["projects"]
            self.assertEqual(legacy_mapping["name"], new)
            self.assertNotIn(old, legacy_mapping["keywords"])
            self.assertIn(new, legacy_mapping["keywords"])
            self.assertIs(legacy_mapping["self"], legacy_mapping)
            self.assertEqual(unrelated["name"], "unrelated")
            self.assertEqual(
                unrelated["description"],
                f"mentions {old} as prose only",
            )
            aliases = migrated["project_keywords"][new]
            self.assertEqual(aliases[:2], [new, old])
            self.assertTrue(
                {
                    "legacy-checkout",
                    "legacy-top-level",
                    "current-top-level",
                }.issubset(aliases)
            )

    def test_mapping_project_config_applies_and_manual_rollback_restores_exact_bytes(self):
        old = "github-obsidian-knowledge-brain"
        new = "agent-memory-beacon"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            config = tmp / "config.yaml"
            original = (
                f"vault_path: {vault}\n"
                "projects:\n"
                f"  - name: {old}\n"
                "    keywords:\n"
                f"      - {old}\n"
                "      - legacy-checkout\n"
                "  - name: unrelated\n"
                f"    description: keep {old} in unrelated prose\n"
                "project_keywords:\n"
                f"  {old}: [legacy-top-level]\n"
            ).encode("utf-8")
            config.write_bytes(original)
            plan = build_migration_plan(vault, config_path=config)

            result = apply_brand_migration(
                plan,
                "mapping-project-apply",
                rebuilders=[],
            )

            self.assertTrue(result["valid"])
            migrated = yaml.safe_load(config.read_text(encoding="utf-8"))
            self.assertEqual(migrated["projects"][0]["name"], new)
            self.assertNotIn(old, migrated["projects"][0]["keywords"])
            self.assertEqual(migrated["projects"][1]["name"], "unrelated")
            self.assertIn(old, migrated["projects"][1]["description"])

            rollback_brand_migration(result["manifest_path"])

            self.assertEqual(config.read_bytes(), original)
            self.assertTrue((vault / "01-Projects" / old).is_dir())
            self.assertFalse((vault / "01-Projects" / new).exists())

    def test_final_validation_rejects_stale_mapping_project_identity(self):
        old = "github-obsidian-knowledge-brain"
        new = "agent-memory-beacon"
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            config = tmp / "config.yaml"
            original = (
                f"vault_path: {vault}\n"
                "projects:\n"
                f"  - name: {old}\n"
                "    keywords: [legacy-checkout]\n"
                "project_keywords:\n"
                f"  {new}: [{old}, legacy-checkout]\n"
            ).encode("utf-8")
            config.write_bytes(original)
            plan = build_migration_plan(vault, config_path=config)

            with patch(
                "brand_migration._rewritten_config_bytes",
                return_value=original,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "structural legacy references remain",
                ):
                    apply_brand_migration(
                        plan,
                        "mapping-project-final-validation",
                        rebuilders=[],
                    )

            self.assertEqual(config.read_bytes(), original)
            self.assertTrue((vault / "01-Projects" / old).is_dir())
            self.assertFalse((vault / "01-Projects" / new).exists())

    def test_rewrite_preserves_all_preflight_code_regions_byte_for_byte(self):
        old = "github-obsidian-knowledge-brain"
        new = "agent-memory-beacon"
        live = f"[[01-Projects/{old}/Memory/decisions|Live]]\n"
        backtick_block = (
            "````markdown\n"
            f"[[01-Projects/{old}/Memory/decisions|Backtick example]]\n"
            "```\n"
            f"[[01-Projects/{old}/Memory/pitfalls|Still code]]\n"
            "````\n"
        )
        tilde_block = (
            "~~~~~markdown\n"
            f"[[01-Projects/{old}/Memory/decisions|Tilde example]]\n"
            "~~~\n"
            f"[[01-Projects/{old}/Memory/pitfalls|Still tilde code]]\n"
            "~~~~~\n"
        )
        indented = (
            f"    [[01-Projects/{old}/Memory/decisions|Indented example]]\n"
            f"\t[[01-Projects/{old}/Memory/pitfalls|Tabbed example]]\n"
        )
        content = live + backtick_block + tilde_block + indented

        updated, changed = rewrite_markdown(content, old, new)

        self.assertTrue(changed)
        self.assertTrue(updated.startswith(live.replace(old, new)))
        self.assertIn(backtick_block, updated)
        self.assertIn(tilde_block, updated)
        self.assertIn(indented, updated)

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            candidate = vault / "00-Rules" / "mixed-code.md"
            write_text(candidate, content)
            plan = build_migration_plan(vault)
            self.assertIn(candidate, plan.markdown_paths)
            result = apply_brand_migration(
                plan,
                "markdown-code-regions",
                rebuilders=[],
            )
            self.assertTrue(result["valid"])
            self.assertEqual(candidate.read_text(encoding="utf-8"), updated)
            rollback_brand_migration(result["manifest_path"])
            self.assertEqual(candidate.read_text(encoding="utf-8"), content)

    def test_rollback_restores_authoritative_directory_metadata(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            empty = vault / "Users" / "nested" / "empty"
            empty.mkdir(parents=True)
            os.chmod(empty, 0o751)
            os.utime(
                empty,
                ns=(1_700_000_000_123_456_789, 1_700_000_001_987_654_321),
            )
            source_sessions = (
                vault
                / "01-Projects"
                / "github-obsidian-knowledge-brain"
                / "Memory"
                / "sessions"
            )
            os.chmod(source_sessions, 0o750)
            os.utime(
                source_sessions,
                ns=(1_700_000_002_123_456_789, 1_700_000_003_987_654_321),
            )
            plan = build_migration_plan(vault)
            expected = {
                path: directory_metadata(path)
                for path in (empty, source_sessions)
            }

            def mutate_directories(_cfg, *_args, **_kwargs):
                os.chmod(empty, 0o700)
                os.utime(empty, ns=(1_710_000_000_000_000_000,) * 2)

            with patch(
                "brand_migration._run_default_rebuilders",
                side_effect=mutate_directories,
            ):
                result = apply_brand_migration(plan, "directory-metadata")

            manifest = json.loads(
                Path(result["manifest_path"]).read_text(encoding="utf-8")
            )
            self.assertTrue(manifest["directory_bindings"])

            rollback_brand_migration(result["manifest_path"])

            self.assertEqual(directory_metadata(empty), expected[empty])
            self.assertEqual(
                directory_metadata(source_sessions),
                expected[source_sessions],
            )

    def test_directory_metadata_restore_rejects_parent_symlink_redirection(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            target = vault / "Users" / "nested" / "empty"
            target.mkdir(parents=True)
            plan = build_migration_plan(vault)
            binding = next(
                item for item in plan.directory_bindings if item.path == target
            )
            held_parent = target.parent.with_name("nested-held")
            target.parent.rename(held_parent)
            outside_parent = tmp / "outside"
            outside_target = outside_parent / "empty"
            outside_target.mkdir(parents=True)
            os.chmod(outside_target, 0o711)
            outside_before = directory_metadata(outside_target)
            target.parent.symlink_to(outside_parent, target_is_directory=True)

            with self.assertRaises((RuntimeError, ValueError, OSError)):
                brand_migration._restore_directory_metadata(binding)

            self.assertEqual(directory_metadata(outside_target), outside_before)

    def test_force_rollback_restores_in_place_external_config_drift(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "projects": ["github-obsidian-knowledge-brain"],
                    },
                    sort_keys=False,
                ),
            )
            original = config.read_bytes()
            plan = build_migration_plan(vault, config_path=config)
            result = apply_brand_migration(
                plan,
                "force-external-drift",
                rebuilders=[],
            )
            applied_inode = config.stat().st_ino
            with open(config, "r+b") as handle:
                handle.seek(0)
                handle.write(b"user edit after migration\n")
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
            self.assertEqual(config.stat().st_ino, applied_inode)

            with self.assertRaisesRegex(RuntimeError, "rollback target changed"):
                rollback_brand_migration(result["manifest_path"])

            rollback_brand_migration(result["manifest_path"], force=True)

            self.assertEqual(config.read_bytes(), original)
            self.assertTrue(plan.source_project.is_dir())

    def test_force_rollback_refuses_replaced_surviving_mutable_directory(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            empty = vault / "Users" / "empty-survivor"
            empty.mkdir(parents=True)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "force-directory-inode",
                rebuilders=[],
            )
            displaced = vault / "empty-survivor-displaced"
            empty.rename(displaced)
            empty.mkdir()
            os.chmod(empty, 0o711)
            replacement_inode = (empty.stat().st_dev, empty.stat().st_ino)
            replacement_metadata = directory_metadata(empty)
            before = snapshot_public_tree(vault)

            with self.assertRaisesRegex(RuntimeError, "directory inode changed"):
                rollback_brand_migration(result["manifest_path"], force=True)

            self.assertEqual(
                (empty.stat().st_dev, empty.stat().st_ino),
                replacement_inode,
            )
            self.assertEqual(directory_metadata(empty), replacement_metadata)
            self.assertEqual(snapshot_public_tree(vault), before)
            self.assertFalse(plan.source_project.exists())
            self.assertTrue(plan.destination_project.is_dir())

    def test_non_force_rollback_rejects_in_place_directory_timestamp_drift(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            empty = vault / "Users" / "timestamp-bound"
            empty.mkdir(parents=True)
            plan = build_migration_plan(vault)
            original = directory_metadata(empty)
            result = apply_brand_migration(
                plan,
                "directory-timestamp-drift",
                rebuilders=[],
            )
            applied = empty.stat()
            changed = applied.st_mtime_ns + 10_000_000_000
            os.utime(empty, ns=(changed, changed))
            drifted = directory_metadata(empty)

            with self.assertRaisesRegex(RuntimeError, "rollback target changed"):
                rollback_brand_migration(result["manifest_path"])

            self.assertEqual(directory_metadata(empty), drifted)
            rollback_brand_migration(result["manifest_path"], force=True)
            self.assertEqual(directory_metadata(empty), original)

    def test_force_rollback_refuses_surviving_directory_link_count_drift(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            empty = vault / "Users" / "empty-link-bound"
            empty.mkdir(parents=True)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "force-directory-links",
                rebuilders=[],
            )
            inode = (empty.stat().st_dev, empty.stat().st_ino)
            before_links = empty.stat().st_nlink
            (empty / "interposed-child").mkdir()
            self.assertGreater(empty.stat().st_nlink, before_links)
            before = snapshot_public_tree(vault)

            with self.assertRaisesRegex(RuntimeError, "directory link count changed"):
                rollback_brand_migration(result["manifest_path"], force=True)

            self.assertEqual((empty.stat().st_dev, empty.stat().st_ino), inode)
            self.assertTrue((empty / "interposed-child").is_dir())
            self.assertEqual(snapshot_public_tree(vault), before)

    def test_manual_rollback_rejects_manifest_inode_replacement_during_guard_acquire(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault-a"
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(plan, "bootstrap-inode", rebuilders=[])
            manifest = Path(result["manifest_path"])
            replacement = tmp / "replacement.json"
            replacement.write_bytes(manifest.read_bytes())
            os.chmod(replacement, 0o400)
            original_guard = brand_migration.migration_writer_guard

            @contextmanager
            def replace_during_acquire(guard_vault, *args, **kwargs):
                with original_guard(guard_vault, *args, **kwargs) as guard:
                    os.chmod(manifest.parent, 0o700)
                    os.replace(replacement, manifest)
                    os.chmod(manifest.parent, 0o500)
                    yield guard

            with patch(
                "brand_migration.migration_writer_guard",
                replace_during_acquire,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "manifest.*changed across writer guard",
                ):
                    rollback_brand_migration(manifest)

            self.assertFalse(plan.source_project.exists())
            self.assertTrue(plan.destination_project.is_dir())

    def test_manual_rollback_derives_guard_vault_from_manifest_location(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault_a = tmp / "vault-a"
            vault_b = tmp / "vault-b"
            make_vault(vault_a)
            make_vault(vault_b)
            plan = build_migration_plan(vault_a)
            result = apply_brand_migration(plan, "bootstrap-vault", rebuilders=[])
            manifest = Path(result["manifest_path"])
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["vault"] = str(vault_b)
            replacement = tmp / "vault-b-payload.json"
            replacement.write_bytes(
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
                + b"\n"
            )
            os.chmod(replacement, 0o400)
            guarded = []
            original_guard = brand_migration.migration_writer_guard

            @contextmanager
            def replace_during_acquire(guard_vault, *args, **kwargs):
                guarded.append(Path(guard_vault))
                with original_guard(guard_vault, *args, **kwargs) as guard:
                    os.chmod(manifest.parent, 0o700)
                    os.replace(replacement, manifest)
                    os.chmod(manifest.parent, 0o500)
                    yield guard

            with patch(
                "brand_migration.migration_writer_guard",
                replace_during_acquire,
            ):
                with self.assertRaisesRegex(
                    (RuntimeError, ValueError),
                    "manifest.*changed across writer guard|does not match its Vault",
                ):
                    rollback_brand_migration(manifest)

            self.assertEqual(guarded, [vault_a])
            self.assertFalse(plan.source_project.exists())
            self.assertTrue(plan.destination_project.is_dir())

    def test_manual_rollback_rejects_whole_vault_inode_swap_before_mutation(self):
        import brand_migration

        for force in (False, True):
            with self.subTest(force=force), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp).resolve()
                vault = tmp / "vault"
                make_vault(vault)
                plan = build_migration_plan(vault)
                result = apply_brand_migration(
                    plan,
                    f"vault-ancestor-swap-{force}",
                    rebuilders=[],
                )
                moved_vault = tmp / "vault-moved"
                replacement_marker = vault / "replacement-marker.txt"
                original_guard = brand_migration.migration_writer_guard
                swapped = []

                @contextmanager
                def swap_during_guard(guard_vault, *args, **kwargs):
                    vault.rename(moved_vault)
                    vault.mkdir()
                    write_text(replacement_marker, "replacement vault\n")
                    swapped.append(True)
                    with original_guard(guard_vault, *args, **kwargs) as guard:
                        yield guard

                with patch(
                    "brand_migration.migration_writer_guard",
                    swap_during_guard,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "Vault.*inode|pinned directory path changed",
                    ):
                        rollback_brand_migration(
                            result["manifest_path"],
                            force=force,
                        )

                self.assertTrue(swapped)
                self.assertEqual(
                    replacement_marker.read_text(encoding="utf-8"),
                    "replacement vault\n",
                )
                self.assertFalse(
                    vault.joinpath("01-Projects", plan.old_slug).exists()
                )
                self.assertFalse(
                    moved_vault.joinpath("01-Projects", plan.old_slug).exists()
                )
                self.assertTrue(
                    moved_vault.joinpath("01-Projects", plan.new_slug).is_dir()
                )

    def test_vault_exchange_restores_interposed_inode_without_unlinking_it(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            manifest = create_migration_backup(plan, "vault-exchange-race")
            handle = brand_migration._open_backup_handle(manifest, expected_plan=plan)
            target = plan.source_project / "Memory" / "decisions.md"
            binding = next(item for item in plan.input_bindings if item.path == target)
            original_bytes = target.read_bytes()
            hold = target.with_name("decisions.original-hold.md")
            original_exchange = brand_migration._rename_exchange
            injected = []

            def race_exchange(source_fd, source_name, destination_fd, destination_name):
                if destination_name == target.name and not injected:
                    injected.append(True)
                    os.rename(
                        destination_name,
                        hold.name,
                        src_dir_fd=destination_fd,
                        dst_dir_fd=destination_fd,
                    )
                    fd = os.open(
                        destination_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=destination_fd,
                    )
                    os.write(fd, b"interposed vault file\n")
                    os.close(fd)
                return original_exchange(
                    source_fd, source_name, destination_fd, destination_name
                )

            try:
                with patch(
                    "brand_migration._rename_exchange",
                    side_effect=race_exchange,
                ):
                    with self.assertRaisesRegex(RuntimeError, "concurrent.*vault target"):
                        brand_migration._write_vault_target(
                            handle,
                            target,
                            b"replacement\n",
                            binding,
                            (),
                        )
            finally:
                brand_migration._close_backup_handle(handle)

            self.assertEqual(target.read_bytes(), b"interposed vault file\n")
            self.assertEqual(hold.read_bytes(), original_bytes)
            self.assertTrue(injected)

    def test_external_exchange_restores_interposed_inode_without_unlinking_it(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            config = tmp / "config.yaml"
            write_text(config, yaml.safe_dump({"vault_path": str(vault)}))
            config_tmp = Path(str(config) + ".tmp")
            write_text(config_tmp, "preserved temporary sibling\n")
            os.chmod(config_tmp, 0o640)
            os.utime(config_tmp, ns=(1_700_000_000_123_456_789,) * 2)
            temp_before = file_metadata(config_tmp)
            original_bytes = config.read_bytes()
            plan = build_migration_plan(vault, config_path=config)
            binding = next(item for item in plan.input_bindings if item.path == config)
            hold = tmp / "config.original-hold.yaml"
            original_exchange = brand_migration._rename_exchange
            injected = []

            def race_exchange(source_fd, source_name, destination_fd, destination_name):
                if destination_name == config.name and not injected:
                    injected.append(True)
                    os.rename(
                        destination_name,
                        hold.name,
                        src_dir_fd=destination_fd,
                        dst_dir_fd=destination_fd,
                    )
                    fd = os.open(
                        destination_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=destination_fd,
                    )
                    os.write(fd, b"interposed external file\n")
                    os.close(fd)
                return original_exchange(
                    source_fd, source_name, destination_fd, destination_name
                )

            with patch(
                "brand_migration._rename_exchange",
                side_effect=race_exchange,
            ):
                with self.assertRaisesRegex(RuntimeError, "concurrent.*external target"):
                    brand_migration._write_external_target(
                        plan.mutation_contract,
                        config,
                        b"replacement\n",
                        binding,
                        plan.mutation_contract.absent_directories,
                        input_bindings=plan.input_bindings,
                    )

            self.assertEqual(config.read_bytes(), b"interposed external file\n")
            self.assertEqual(hold.read_bytes(), original_bytes)
            self.assertEqual(config_tmp.read_bytes(), b"preserved temporary sibling\n")
            self.assertEqual(file_metadata(config_tmp), temp_before)
            self.assertTrue(injected)

    def test_existing_external_temp_same_inode_drift_is_rejected_and_preserved(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            config = tmp / "config.yaml"
            write_text(config, yaml.safe_dump({"vault_path": str(vault)}))
            config_tmp = Path(str(config) + ".tmp")
            write_text(config_tmp, "preserved temporary sibling\n")
            plan = build_migration_plan(vault, config_path=config)
            manifest = create_migration_backup(plan, "temp-binding-drift")
            handle = brand_migration._open_backup_handle(manifest, expected_plan=plan)
            original_config = config.read_bytes()
            original_inode = config_tmp.stat().st_ino
            edited = bytearray(config_tmp.read_bytes())
            edited[0] = ord("P")
            with open(config_tmp, "r+b") as stream:
                stream.write(edited)
                stream.flush()
                os.fsync(stream.fileno())
            self.assertEqual(config_tmp.stat().st_ino, original_inode)

            try:
                with self.assertRaisesRegex(RuntimeError, "binding changed|digest"):
                    brand_migration._write_bound_target(
                        plan,
                        handle,
                        config,
                        b"vault_path: changed\n",
                    )
            finally:
                brand_migration._close_backup_handle(handle)

            self.assertEqual(config.read_bytes(), original_config)
            self.assertEqual(config_tmp.read_bytes(), bytes(edited))

    def test_vault_exchange_restores_interposed_symlink_without_following_it(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            outside = tmp / "outside.txt"
            write_text(outside, "outside unchanged\n")
            plan = build_migration_plan(vault)
            manifest = create_migration_backup(plan, "vault-symlink-race")
            handle = brand_migration._open_backup_handle(manifest, expected_plan=plan)
            target = plan.source_project / "Memory" / "decisions.md"
            binding = next(item for item in plan.input_bindings if item.path == target)
            hold = target.with_name("decisions.symlink-race-hold.md")
            original_exchange = brand_migration._rename_exchange
            injected = []

            def race_exchange(source_fd, source_name, destination_fd, destination_name):
                if destination_name == target.name and not injected:
                    injected.append(True)
                    os.rename(
                        destination_name,
                        hold.name,
                        src_dir_fd=destination_fd,
                        dst_dir_fd=destination_fd,
                    )
                    os.symlink(outside, destination_name, dir_fd=destination_fd)
                return original_exchange(
                    source_fd, source_name, destination_fd, destination_name
                )

            try:
                with patch(
                    "brand_migration._rename_exchange",
                    side_effect=race_exchange,
                ):
                    with self.assertRaisesRegex(RuntimeError, "concurrent.*vault target"):
                        brand_migration._write_vault_target(
                            handle,
                            target,
                            b"replacement\n",
                            binding,
                            (),
                        )
            finally:
                brand_migration._close_backup_handle(handle)

            self.assertTrue(target.is_symlink())
            self.assertEqual(os.readlink(target), str(outside))
            self.assertEqual(outside.read_text(encoding="utf-8"), "outside unchanged\n")
            self.assertTrue(hold.is_file())
            self.assertTrue(injected)

    def test_source_directory_rename_refuses_interposed_directory(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            manifest = create_migration_backup(plan, "source-inner-race")
            handle = brand_migration._open_backup_handle(manifest, expected_plan=plan)
            pin = brand_migration._pin_project_source(plan)
            hold = plan.source_project.with_name("source-original-hold")
            original_rename = brand_migration._rename_exclusive
            injected = []

            def race_rename(source_fd, source_name, destination_fd, destination_name):
                if source_name == plan.source_project.name and not injected:
                    injected.append(True)
                    os.rename(
                        source_name,
                        hold.name,
                        src_dir_fd=source_fd,
                        dst_dir_fd=source_fd,
                    )
                    os.mkdir(source_name, dir_fd=source_fd)
                    child_fd = os.open(
                        source_name,
                        os.O_RDONLY | os.O_DIRECTORY,
                        dir_fd=source_fd,
                    )
                    try:
                        fd = os.open(
                            "interposed.txt",
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=child_fd,
                        )
                        os.write(fd, b"interposed directory\n")
                        os.close(fd)
                    finally:
                        os.close(child_fd)
                return original_rename(
                    source_fd, source_name, destination_fd, destination_name
                )

            try:
                with patch(
                    "brand_migration._rename_exclusive",
                    side_effect=race_rename,
                ):
                    with self.assertRaises(
                        brand_migration._ProjectRenameRace
                    ) as caught:
                        brand_migration._rename_project_directory(
                            plan,
                            handle,
                            pin,
                        )
            finally:
                brand_migration._close_project_source_pin(pin)
                brand_migration._close_backup_handle(handle)

            self.assertTrue(caught.exception.rename_occurred)
            self.assertFalse(caught.exception.recovered)
            self.assertFalse(plan.source_project.exists())
            self.assertTrue(hold.is_dir())
            self.assertTrue(
                (plan.destination_project / "interposed.txt").is_file()
            )
            self.assertTrue(injected)

    def test_source_rename_post_stat_failure_enters_pinned_recovery(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            manifest = create_migration_backup(plan, "source-post-stat-race")
            handle = brand_migration._open_backup_handle(manifest, expected_plan=plan)
            pin = brand_migration._pin_project_source(plan)
            real_stat = os.stat
            injected = []
            destination_stats = []

            def fail_first_destination_stat(path, *args, **kwargs):
                if (
                    path == plan.destination_project.name
                    and kwargs.get("dir_fd") == pin.parent_fd
                ):
                    destination_stats.append(True)
                    if len(destination_stats) == 2:
                        injected.append(True)
                        raise FileNotFoundError(path)
                return real_stat(path, *args, **kwargs)

            try:
                with patch(
                    "brand_migration.os.stat",
                    side_effect=fail_first_destination_stat,
                ):
                    with self.assertRaisesRegex(RuntimeError, "concurrent.*source directory"):
                        brand_migration._rename_project_directory(plan, handle, pin)
            finally:
                brand_migration._close_project_source_pin(pin)
                brand_migration._close_backup_handle(handle)

            self.assertTrue(plan.source_project.is_dir())
            self.assertFalse(plan.destination_project.exists())
            self.assertTrue(injected)

    def test_source_rename_recovery_refuses_replaced_destination(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            manifest = create_migration_backup(
                plan,
                "source-post-rename-replacement",
            )
            handle = brand_migration._open_backup_handle(
                manifest,
                expected_plan=plan,
            )
            pin = brand_migration._pin_project_source(plan)
            real_stat = os.stat
            destination_stats = []
            original_hold = plan.destination_project.with_name(
                "source-post-rename-original-hold"
            )

            def replace_first_post_rename_stat(path, *args, **kwargs):
                if (
                    path == plan.destination_project.name
                    and kwargs.get("dir_fd") == pin.parent_fd
                ):
                    destination_stats.append(True)
                    if len(destination_stats) == 2:
                        os.rename(
                            path,
                            original_hold.name,
                            src_dir_fd=pin.parent_fd,
                            dst_dir_fd=pin.parent_fd,
                        )
                        os.mkdir(path, dir_fd=pin.parent_fd)
                        external_fd = os.open(
                            path,
                            os.O_RDONLY | os.O_DIRECTORY,
                            dir_fd=pin.parent_fd,
                        )
                        try:
                            file_fd = os.open(
                                "external.txt",
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                0o600,
                                dir_fd=external_fd,
                            )
                            try:
                                os.write(file_fd, b"external directory\n")
                            finally:
                                os.close(file_fd)
                        finally:
                            os.close(external_fd)
                return real_stat(path, *args, **kwargs)

            try:
                with patch(
                    "brand_migration.os.stat",
                    side_effect=replace_first_post_rename_stat,
                ):
                    with self.assertRaises(
                        brand_migration._ProjectRenameRace
                    ) as caught:
                        brand_migration._rename_project_directory(
                            plan,
                            handle,
                            pin,
                        )
            finally:
                brand_migration._close_project_source_pin(pin)
                brand_migration._close_backup_handle(handle)

            self.assertTrue(caught.exception.rename_occurred)
            self.assertFalse(caught.exception.recovered)
            self.assertFalse(plan.source_project.exists())
            self.assertEqual(
                (plan.destination_project / "external.txt").read_text(
                    encoding="utf-8"
                ),
                "external directory\n",
            )
            self.assertTrue(original_hold.is_dir())

    def test_rollback_quarantine_restores_interposed_file_before_delete(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            manifest = create_migration_backup(plan, "rollback-delete-race")
            handle = brand_migration._open_backup_handle(manifest, expected_plan=plan)
            target = plan.source_project / "Memory" / "decisions.md"
            binding = next(item for item in plan.input_bindings if item.path == target)
            hold = target.with_name("decisions.rollback-hold.md")
            original_rename = brand_migration._rename_exclusive
            injected = []

            def race_quarantine(source_fd, source_name, destination_fd, destination_name):
                if source_name == target.name and not injected:
                    injected.append(True)
                    os.rename(
                        source_name,
                        hold.name,
                        src_dir_fd=source_fd,
                        dst_dir_fd=source_fd,
                    )
                    fd = os.open(
                        source_name,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=source_fd,
                    )
                    os.write(fd, b"interposed rollback file\n")
                    os.close(fd)
                return original_rename(
                    source_fd, source_name, destination_fd, destination_name
                )

            try:
                with patch(
                    "brand_migration._rename_exclusive",
                    side_effect=race_quarantine,
                ):
                    with self.assertRaisesRegex(RuntimeError, "concurrent.*rollback deletion"):
                        brand_migration._quarantine_remove_file(
                            handle,
                            target,
                            binding.inode,
                        )
            finally:
                brand_migration._close_backup_handle(handle)

            self.assertEqual(target.read_bytes(), b"interposed rollback file\n")
            self.assertTrue(hold.is_file())
            self.assertTrue(injected)

    def test_rollback_quarantine_restores_interposed_symlink_and_cleans_scratch(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            outside = tmp / "outside.txt"
            write_text(outside, "outside unchanged\n")
            plan = build_migration_plan(vault)
            manifest = create_migration_backup(plan, "rollback-symlink-race")
            handle = brand_migration._open_backup_handle(manifest, expected_plan=plan)
            target = plan.source_project / "Memory" / "decisions.md"
            binding = next(item for item in plan.input_bindings if item.path == target)
            hold = target.with_name("decisions.symlink-hold.md")
            original_rename = brand_migration._rename_exclusive
            injected = []

            def race_quarantine(source_fd, source_name, destination_fd, destination_name):
                if source_name == target.name and not injected:
                    injected.append(True)
                    os.rename(
                        source_name,
                        hold.name,
                        src_dir_fd=source_fd,
                        dst_dir_fd=source_fd,
                    )
                    os.symlink(outside, source_name, dir_fd=source_fd)
                return original_rename(
                    source_fd, source_name, destination_fd, destination_name
                )

            try:
                with patch(
                    "brand_migration._rename_exclusive",
                    side_effect=race_quarantine,
                ):
                    with self.assertRaisesRegex(RuntimeError, "concurrent.*rollback deletion"):
                        brand_migration._quarantine_remove_file(
                            handle,
                            target,
                            binding.inode,
                        )
            finally:
                brand_migration._close_backup_handle(handle)

            self.assertTrue(target.is_symlink())
            self.assertEqual(os.readlink(target), str(outside))
            self.assertTrue(hold.is_file())
            self.assertEqual(
                list(target.parent.glob(".brand-migration-quarantine-*")),
                [],
            )

    def test_rollback_quarantine_restores_interposed_directory_and_cleans_scratch(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            manifest = create_migration_backup(plan, "rollback-directory-entry-race")
            handle = brand_migration._open_backup_handle(manifest, expected_plan=plan)
            target = plan.source_project / "Memory" / "decisions.md"
            binding = next(item for item in plan.input_bindings if item.path == target)
            hold = target.with_name("decisions.directory-hold.md")
            original_rename = brand_migration._rename_exclusive
            injected = []

            def race_quarantine(source_fd, source_name, destination_fd, destination_name):
                if source_name == target.name and not injected:
                    injected.append(True)
                    os.rename(
                        source_name,
                        hold.name,
                        src_dir_fd=source_fd,
                        dst_dir_fd=source_fd,
                    )
                    os.mkdir(source_name, dir_fd=source_fd)
                    child_fd = os.open(source_name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=source_fd)
                    try:
                        write_fd = os.open(
                            "interposed.txt",
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=child_fd,
                        )
                        os.write(write_fd, b"interposed directory\n")
                        os.close(write_fd)
                    finally:
                        os.close(child_fd)
                return original_rename(
                    source_fd, source_name, destination_fd, destination_name
                )

            try:
                with patch(
                    "brand_migration._rename_exclusive",
                    side_effect=race_quarantine,
                ):
                    with self.assertRaisesRegex(RuntimeError, "concurrent.*rollback deletion"):
                        brand_migration._quarantine_remove_file(
                            handle,
                            target,
                            binding.inode,
                        )
            finally:
                brand_migration._close_backup_handle(handle)

            self.assertEqual(
                (target / "interposed.txt").read_bytes(),
                b"interposed directory\n",
            )
            self.assertTrue(hold.is_file())
            self.assertEqual(
                list(target.parent.glob(".brand-migration-quarantine-*")),
                [],
            )

    def test_quarantine_reports_primary_and_cleanup_failures_without_masking(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            target = tmp / "target.txt"
            outside = tmp / "outside.txt"
            write_text(target, "original\n")
            write_text(outside, "outside\n")
            expected = (target.stat().st_dev, target.stat().st_ino)
            hold = tmp / "original-hold.txt"
            parent_fd = os.open(tmp, os.O_RDONLY | os.O_DIRECTORY)
            original_rename = brand_migration._rename_exclusive
            original_rmdir = os.rmdir
            injected = []

            def interpose(source_fd, source_name, destination_fd, destination_name):
                if source_name == target.name and not injected:
                    injected.append(True)
                    os.rename(
                        source_name,
                        hold.name,
                        src_dir_fd=source_fd,
                        dst_dir_fd=source_fd,
                    )
                    os.symlink(outside, source_name, dir_fd=source_fd)
                return original_rename(
                    source_fd,
                    source_name,
                    destination_fd,
                    destination_name,
                )

            def fail_private_cleanup(path, *args, **kwargs):
                if str(path).startswith(".brand-migration-quarantine-"):
                    raise OSError("forced quarantine cleanup failure")
                return original_rmdir(path, *args, **kwargs)

            try:
                with patch(
                    "brand_migration._rename_exclusive",
                    side_effect=interpose,
                ), patch(
                    "brand_migration.os.rmdir",
                    side_effect=fail_private_cleanup,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "unexpected inode was restored.*cleanup failed.*forced quarantine",
                    ):
                        brand_migration._quarantine_remove_name(
                            parent_fd,
                            target.name,
                            expected,
                            "test cleanup aggregation",
                        )
            finally:
                os.close(parent_fd)

            self.assertTrue(target.is_symlink())
            self.assertEqual(os.readlink(target), str(outside))
            self.assertTrue(hold.is_file())

    def test_rollback_directory_quarantine_restores_last_check_interposition(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            target = vault / "Users" / "empty-created"
            target.mkdir(parents=True)
            expected_inode = (target.stat().st_dev, target.stat().st_ino)
            hold = target.with_name("empty-created-original-hold")
            replacement_inode = []
            original_named_stat = brand_migration._named_stat
            injected = []

            def race_after_named_check(parent_fd, name):
                current = original_named_stat(parent_fd, name)
                if name == target.name and not injected:
                    injected.append(True)
                    os.rename(
                        name,
                        hold.name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    os.mkdir(name, dir_fd=parent_fd)
                    replacement = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    replacement_inode.append((replacement.st_dev, replacement.st_ino))
                return current

            with patch(
                "brand_migration._named_stat",
                side_effect=race_after_named_check,
            ):
                with self.assertRaisesRegex(RuntimeError, "concurrent.*directory"):
                    brand_migration._remove_directory_if_empty(
                        target,
                        expected_inode,
                    )

            self.assertEqual(
                (target.stat().st_dev, target.stat().st_ino),
                replacement_inode[0],
            )
            self.assertTrue(hold.is_dir())
            self.assertEqual(
                list(target.parent.glob(".brand-migration-directory-quarantine-*")),
                [],
            )

    def test_custom_rebuilder_with_delayed_thread_is_rejected_before_start(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            plan = build_migration_plan(vault)
            outside = tmp / "delayed-thread.txt"
            release = threading.Event()
            threads = []

            def delayed(_cfg):
                thread = threading.Thread(
                    target=lambda: (release.wait(), outside.write_text("bad")),
                )
                threads.append(thread)
                thread.start()

            try:
                with self.assertRaisesRegex(ValueError, "custom rebuilders are unsupported"):
                    apply_brand_migration(
                        plan,
                        "reject-thread-rebuilder",
                        rebuilders=[delayed],
                    )
            finally:
                release.set()
                for thread in threads:
                    thread.join()

            self.assertEqual(threads, [])
            self.assertFalse(outside.exists())
            self.assertFalse(
                vault.joinpath(
                    "04-Feedback/_rollback/brand-migration/reject-thread-rebuilder"
                ).exists()
            )

    def test_custom_rebuilder_with_preopened_descriptor_is_rejected(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            plan = build_migration_plan(vault)
            outside = tmp / "preopened.txt"
            fd = os.open(outside, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            called = []

            def write_preopened(_cfg):
                called.append(True)
                os.write(fd, b"bad")

            try:
                with self.assertRaisesRegex(ValueError, "custom rebuilders are unsupported"):
                    apply_brand_migration(
                        plan,
                        "reject-preopened-rebuilder",
                        rebuilders=[write_preopened],
                    )
            finally:
                os.close(fd)

            self.assertEqual(called, [])
            self.assertEqual(outside.read_bytes(), b"")

    def test_custom_git_output_probe_is_rejected_before_execution(self):
        git = shutil.which("git")
        if git is None:
            self.skipTest("git is unavailable")
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            plan = build_migration_plan(vault)
            outside = tmp / "git-output.txt"

            def git_output(_cfg):
                subprocess.run(
                    [git, "diff", "--name-only", f"--output={outside}"],
                    cwd=REPO_ROOT,
                    check=True,
                )

            with self.assertRaisesRegex(ValueError, "custom rebuilders are unsupported"):
                apply_brand_migration(
                    plan,
                    "reject-git-output",
                    rebuilders=[git_output],
                )

            self.assertFalse(outside.exists())

    def test_custom_rebuilder_symlink_redirection_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            plan = build_migration_plan(vault)
            outside = tmp / "outside.txt"
            write_text(outside, "original\n")
            link = vault / "Users" / "redirect"

            def redirect(_cfg):
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(outside)
                link.write_text("bad\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "custom rebuilders are unsupported"):
                apply_brand_migration(
                    plan,
                    "reject-symlink-rebuilder",
                    rebuilders=[redirect],
                )

            self.assertEqual(outside.read_text(encoding="utf-8"), "original\n")
            self.assertFalse(link.exists())

    def test_concurrent_custom_rebuilder_rejections_are_isolated(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            plans = []
            for name in ("one", "two"):
                vault = tmp / name
                make_vault(vault)
                plans.append(build_migration_plan(vault))
            called = []
            outcomes = []

            def unsupported(_cfg):
                called.append(True)

            def run(plan, migration_id):
                try:
                    apply_brand_migration(
                        plan,
                        migration_id,
                        rebuilders=[unsupported],
                    )
                except Exception as exc:
                    outcomes.append((type(exc), str(exc)))

            threads = [
                threading.Thread(target=run, args=(plan, f"reject-{index}"))
                for index, plan in enumerate(plans)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(called, [])
            self.assertEqual(len(outcomes), 2)
            self.assertTrue(all(kind is ValueError for kind, _message in outcomes))
            self.assertTrue(
                all("custom rebuilders are unsupported" in message for _kind, message in outcomes)
            )

    def test_writer_guard_renews_both_leases_before_stale_takeover(self):
        from runner import acquire_lock
        from session_harvester import acquire_harvest_lock

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            harvest = vault / "04-Feedback" / "_logs" / "harvester.lock"
            scanner = vault / "04-Feedback" / "scanner.lock"

            with migration_writer_guard(vault) as guard:
                stale = time.time() - 8_000
                os.utime(harvest, (stale, stale))
                os.utime(scanner, (stale, stale))
                guard.renew()

                self.assertFalse(acquire_harvest_lock(str(harvest)))
                self.assertFalse(acquire_lock(str(scanner), force=False))

    def test_writer_guard_reuses_unlocked_persistent_harvester_lock(self):
        from session_harvester import acquire_harvest_lock, release_harvest_lock

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            harvest = vault / "04-Feedback" / "_logs" / "harvester.lock"
            harvest.parent.mkdir(parents=True, exist_ok=True)
            write_text(harvest, '{"pid": 0, "state": "idle"}\n')
            original_inode = harvest.stat().st_ino

            with migration_writer_guard(vault):
                self.assertFalse(acquire_harvest_lock(str(harvest)))

            self.assertFalse(harvest.exists())
            self.assertTrue(acquire_harvest_lock(str(harvest)))
            release_harvest_lock(str(harvest))

    def test_scanner_busy_releases_partially_acquired_harvester_lease(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            harvest = vault / "04-Feedback" / "_logs" / "harvester.lock"
            scanner = vault / "04-Feedback" / "scanner.lock"
            write_text(scanner, '{"token": "existing-scanner"}\n')
            scanner_inode = scanner.stat().st_ino

            with self.assertRaisesRegex(RuntimeError, "weekly scanner is active"):
                with migration_writer_guard(vault):
                    self.fail("guard must not yield while scanner is active")

            self.assertFalse(harvest.exists())
            self.assertEqual(scanner.stat().st_ino, scanner_inode)
            self.assertIn("existing-scanner", scanner.read_text(encoding="utf-8"))

    def test_writer_guard_detects_replacement_and_preserves_new_owner_lock(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            harvest = vault / "04-Feedback" / "_logs" / "harvester.lock"

            with self.assertRaisesRegex(RuntimeError, "writer guard ownership lost"):
                with migration_writer_guard(vault) as guard:
                    harvest.unlink()
                    write_text(harvest, '{"token": "replacement-owner"}\n')
                    replacement_inode = harvest.stat().st_ino
                    guard.assert_owned()

            self.assertTrue(harvest.exists())
            self.assertEqual(harvest.stat().st_ino, replacement_inode)
            self.assertIn("replacement-owner", harvest.read_text(encoding="utf-8"))

    def test_writer_guard_release_race_never_unlinks_replacement_owner_lock(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            harvest = vault / "04-Feedback" / "_logs" / "harvester.lock"
            hold = harvest.with_name("harvester.original-hold")
            lease = brand_migration._acquire_migration_lock(harvest, "harvester")
            original_assert = lease.assert_owned
            replacement_inode = []

            def replace_after_owner_check():
                original_assert()
                if not replacement_inode:
                    harvest.rename(hold)
                    write_text(harvest, '{"token": "replacement-release"}\n')
                    replacement_inode.append(harvest.stat().st_ino)

            try:
                with patch.object(
                    lease,
                    "assert_owned",
                    side_effect=replace_after_owner_check,
                ):
                    with self.assertRaisesRegex(RuntimeError, "ownership lost"):
                        lease.release()
            finally:
                lease.close_without_unlink()

            self.assertEqual(harvest.stat().st_ino, replacement_inode[0])
            self.assertIn("replacement-release", harvest.read_text(encoding="utf-8"))
            self.assertTrue(hold.is_file())

    def test_apply_renews_both_writer_leases_during_long_rebuilder(self):
        from runner import acquire_lock
        from session_harvester import acquire_harvest_lock
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            takeover_results = []

            original_validate = brand_migration.validate_brand_migration

            def delayed_validation(active_plan):
                harvest = vault / "04-Feedback" / "_logs" / "harvester.lock"
                scanner = vault / "04-Feedback" / "scanner.lock"
                stale = time.time() - 8_000
                os.utime(harvest, (stale, stale))
                os.utime(scanner, (stale, stale))
                time.sleep(0.08)
                takeover_results.extend(
                    [
                        acquire_harvest_lock(str(harvest)),
                        acquire_lock(str(scanner), force=False),
                    ]
                )
                return original_validate(active_plan)

            with patch(
                "brand_migration.MIGRATION_LOCK_RENEW_INTERVAL_SECONDS",
                0.01,
                create=True,
            ), patch(
                "brand_migration.validate_brand_migration",
                side_effect=delayed_validation,
            ):
                result = apply_brand_migration(plan, "renew-apply", rebuilders=[])

            self.assertEqual(takeover_results, [False, False])
            rollback_brand_migration(result["manifest_path"])

    def test_manual_rollback_renews_both_writer_leases(self):
        from runner import acquire_lock
        from session_harvester import acquire_harvest_lock
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(plan, "renew-rollback", rebuilders=[])
            takeover_results = []
            original = brand_migration._rollback_with_handle

            def delayed_rollback(
                handle,
                force=False,
                ownership_check=None,
                recovery_plan=None,
                recovery_reconciliation=None,
            ):
                harvest = vault / "04-Feedback" / "_logs" / "harvester.lock"
                scanner = vault / "04-Feedback" / "scanner.lock"
                stale = time.time() - 8_000
                os.utime(harvest, (stale, stale))
                os.utime(scanner, (stale, stale))
                time.sleep(0.08)
                takeover_results.extend(
                    [
                        acquire_harvest_lock(str(harvest)),
                        acquire_lock(str(scanner), force=False),
                    ]
                )
                return original(
                    handle,
                    force=force,
                    ownership_check=ownership_check,
                    recovery_plan=recovery_plan,
                    recovery_reconciliation=recovery_reconciliation,
                )

            with patch(
                "brand_migration.MIGRATION_LOCK_RENEW_INTERVAL_SECONDS",
                0.01,
                create=True,
            ), patch(
                "brand_migration._rollback_with_handle",
                side_effect=delayed_rollback,
            ):
                rollback_brand_migration(result["manifest_path"])

            self.assertEqual(takeover_results, [False, False])

    def test_apply_detects_guard_ownership_loss_and_preserves_replacement_lock(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            harvest = vault / "04-Feedback" / "_logs" / "harvester.lock"
            replacement_inode = []
            original_validate = brand_migration.validate_brand_migration

            def replace_before_validation(active_plan):
                harvest.unlink()
                write_text(harvest, '{"token": "replacement-apply"}\n')
                replacement_inode.append(harvest.stat().st_ino)
                return original_validate(active_plan)

            with patch(
                "brand_migration.validate_brand_migration",
                side_effect=replace_before_validation,
            ):
                with self.assertRaisesRegex(RuntimeError, "writer guard ownership lost"):
                    apply_brand_migration(plan, "ownership-loss-apply", rebuilders=[])

            self.assertTrue(plan.destination_project.is_dir())
            self.assertFalse(plan.source_project.exists())
            self.assertEqual(harvest.stat().st_ino, replacement_inode[0])
            self.assertIn("replacement-apply", harvest.read_text(encoding="utf-8"))

    def test_apply_has_no_supervised_worker_surface(self):
        import inspect
        import brand_migration

        source = inspect.getsource(brand_migration)
        for forbidden in (
            "subprocess.Popen",
            "python -c",
            "start_new_session",
            "killpg",
            "_TRUSTED_REBUILDER_WORKER",
            "_terminate_rebuilder_process",
        ):
            self.assertNotIn(forbidden, source)

    def test_manual_rollback_stops_before_mutation_after_guard_ownership_loss(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "ownership-loss-rollback",
                rebuilders=[],
            )
            harvest = vault / "04-Feedback" / "_logs" / "harvester.lock"
            destination_file = plan.destination_project / "Memory" / "decisions.md"
            applied_bytes = destination_file.read_bytes()
            replacement_inode = []
            original_drift_check = brand_migration._assert_no_post_migration_drift

            def replace_after_drift_check(*args, **kwargs):
                original_drift_check(*args, **kwargs)
                harvest.unlink()
                write_text(harvest, '{"token": "replacement-rollback"}\n')
                replacement_inode.append(harvest.stat().st_ino)

            with patch(
                "brand_migration._assert_no_post_migration_drift",
                side_effect=replace_after_drift_check,
            ):
                with self.assertRaisesRegex(RuntimeError, "writer guard ownership lost"):
                    rollback_brand_migration(result["manifest_path"])

            self.assertFalse(plan.source_project.exists())
            self.assertEqual(destination_file.read_bytes(), applied_bytes)
            self.assertEqual(harvest.stat().st_ino, replacement_inode[0])
            self.assertIn("replacement-rollback", harvest.read_text(encoding="utf-8"))

    def test_default_chain_runs_in_process_without_popen(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            (vault / "00-Rules").mkdir()
            plan = build_migration_plan(vault)

            with patch(
                "subprocess.Popen",
                side_effect=AssertionError("Popen must not be used by apply"),
            ):
                result = apply_brand_migration(plan, "no-worker-process")

            self.assertEqual(result["status"], "applied")
            rollback_brand_migration(result["manifest_path"])

    def test_fixed_chain_uses_only_frozen_canonical_environment_paths(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            (vault / "00-Rules").mkdir()
            agent_root = tmp / "canonical-agent-memory"
            context = tmp / "canonical-context" / "AGENTS.md"
            write_text(
                agent_root / ".agent-memory-beacon-root",
                "owned by agent-memory-beacon\n",
            )
            write_text(
                context,
                "preamble\n<!-- COMPILED:RULES_START -->\nold\n"
                "<!-- COMPILED:RULES_END -->\n"
                "<!-- COMPILED:PROJECTS_START -->\nold\n"
                "<!-- COMPILED:PROJECTS_END -->\n",
            )
            config = tmp / "config.yaml"
            write_text(
                config,
                "vault_path: $MIGRATION_VAULT\n"
                "agent_memory_path: $MIGRATION_AGENT_ROOT\n"
                "context_targets:\n"
                "  - $MIGRATION_CONTEXT\n",
            )
            environment = {
                "MIGRATION_VAULT": str(vault),
                "MIGRATION_AGENT_ROOT": str(agent_root),
                "MIGRATION_CONTEXT": str(context),
            }
            with patch.dict(os.environ, environment, clear=False):
                plan = build_migration_plan(vault, config_path=config)

            hostile_cwd = tmp / "hostile-cwd"
            hostile_cwd.mkdir()
            original_cwd = os.getcwd()
            try:
                os.chdir(hostile_cwd)
                with patch.dict(os.environ, {}, clear=True):
                    result = apply_brand_migration(plan, "canonical-fixed-chain")
            finally:
                os.chdir(original_cwd)

            self.assertFalse((hostile_cwd / "$MIGRATION_AGENT_ROOT").exists())
            self.assertFalse((hostile_cwd / "$MIGRATION_CONTEXT").exists())
            self.assertIn(
                "<!-- COMPILED:RULES_START -->",
                context.read_text(encoding="utf-8"),
            )
            rollback_brand_migration(result["manifest_path"])

    def test_fixed_chain_does_not_reexpand_late_defined_literal_context(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            (vault / "00-Rules").mkdir()
            literal = tmp / "$LATE_CONTEXT" / "AGENTS.md"
            redirected = tmp / "redirected" / "AGENTS.md"
            document = (
                "preamble\n<!-- COMPILED:RULES_START -->\nold\n"
                "<!-- COMPILED:RULES_END -->\n"
                "<!-- COMPILED:PROJECTS_START -->\nold\n"
                "<!-- COMPILED:PROJECTS_END -->\n"
            )
            write_text(literal, document)
            write_text(redirected, "replacement writer target\n")
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "context_targets": [str(literal)],
                    },
                    sort_keys=False,
                ),
            )
            with patch.dict(os.environ, {}, clear=True):
                plan = build_migration_plan(vault, config_path=config)
            redirected_before = redirected.read_bytes()

            with patch.dict(
                os.environ,
                {"LATE_CONTEXT": "redirected"},
                clear=False,
            ):
                result = apply_brand_migration(plan, "literal-late-context")

            self.assertNotEqual(literal.read_text(encoding="utf-8"), document)
            self.assertEqual(redirected.read_bytes(), redirected_before)
            rollback_brand_migration(result["manifest_path"])
            self.assertEqual(literal.read_text(encoding="utf-8"), document)

    def test_rebuilder_config_projects_only_frozen_canonical_paths(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            agent_root = tmp / "agent-memory"
            context = tmp / "context" / "AGENTS.md"
            write_text(
                agent_root / ".agent-memory-beacon-root",
                "owned by agent-memory-beacon\n",
            )
            write_text(context, "context\n")
            config = tmp / "config.yaml"
            write_text(
                config,
                f"vault_path: {vault}\n"
                "agent_memory_path: $BOUND_AGENT_ROOT\n"
                "context_targets:\n"
                "  - $BOUND_CONTEXT\n",
            )
            with patch.dict(
                os.environ,
                {
                    "BOUND_AGENT_ROOT": str(agent_root),
                    "BOUND_CONTEXT": str(context),
                },
                clear=False,
            ):
                plan = build_migration_plan(vault, config_path=config)

            with patch.dict(os.environ, {}, clear=True):
                cfg = brand_migration.load_migration_config(plan)

            self.assertEqual(cfg["agent_memory_path"], str(agent_root))
            self.assertEqual(cfg["context_targets"], [str(context)])
            self.assertNotIn("$BOUND_AGENT_ROOT", json.dumps(cfg))
            self.assertNotIn("$BOUND_CONTEXT", json.dumps(cfg))

    def test_apply_publishes_sealed_append_only_checkpoint_chain(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "append-only-journal",
                rebuilders=[],
            )
            manifest = Path(result["manifest_path"])
            base = json.loads(manifest.read_text(encoding="utf-8"))
            records = read_checkpoint_records(manifest)

            self.assertEqual(base["status"], "prepared")
            self.assertGreaterEqual(len(records), 4)
            previous = None
            for sequence, digest, record, directory in records:
                self.assertEqual(record["sequence"], sequence)
                self.assertEqual(record["previous_record_sha256"], previous)
                self.assertEqual(record["base_manifest_sha256"], hashlib.sha256(manifest.read_bytes()).hexdigest())
                self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o500)
                self.assertEqual(
                    stat.S_IMODE((directory / "record.json").stat().st_mode),
                    0o400,
                )
                previous = digest
            self.assertEqual(records[-1][2]["status"], "applied")

            rollback_brand_migration(manifest)
            rolled_back = read_checkpoint_records(manifest)
            self.assertGreater(len(rolled_back), len(records))
            self.assertEqual(rolled_back[-1][2]["status"], "rolled_back")

            retry = rollback_brand_migration(manifest)
            self.assertEqual(retry["status"], "rolled_back")
            self.assertEqual(read_checkpoint_records(manifest), rolled_back)

    def test_checkpoint_recovery_boundaries_are_exact_and_retryable(self):
        import brand_migration

        cases = (
            ("before-first-publication", lambda phase, boundary: phase == "source-rename" and boundary == "before", True),
            ("post-rename", lambda phase, boundary: phase == "source-rename" and boundary == "after", False),
            ("mid-rewrite", lambda phase, boundary: phase.startswith("rewrite:") and boundary == "after", False),
            ("pre-finalization", lambda phase, boundary: phase == "validation-finalization" and boundary == "before", False),
        )
        for label, matches, before_publish in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw_vault:
                vault = Path(raw_vault).resolve()
                make_vault(vault)
                plan = build_migration_plan(vault)
                before = snapshot_public_tree(vault)
                original_publish = brand_migration._publish_checkpoint
                interrupted = []

                def interrupt(handle, active_plan, phase, boundary, *args, **kwargs):
                    if not interrupted and before_publish and matches(phase, boundary):
                        interrupted.append((phase, boundary))
                        raise brand_migration._CheckpointFailure("simulated crash before publication")
                    result = original_publish(
                        handle,
                        active_plan,
                        phase,
                        boundary,
                        *args,
                        **kwargs,
                    )
                    if not interrupted and matches(phase, boundary):
                        interrupted.append((phase, boundary))
                        raise brand_migration._CheckpointFailure("simulated crash after publication")
                    return result

                with patch(
                    "brand_migration._publish_checkpoint",
                    side_effect=interrupt,
                ):
                    with self.assertRaisesRegex(RuntimeError, "recovery required"):
                        apply_brand_migration(
                            plan,
                            f"checkpoint-{label}",
                            rebuilders=[],
                        )

                self.assertEqual(len(interrupted), 1)
                manifest = (
                    vault
                    / "04-Feedback/_rollback/brand-migration"
                    / f"checkpoint-{label}"
                    / "manifest.json"
                )
                rollback_brand_migration(manifest)
                self.assertEqual(snapshot_public_tree(vault), before)
                retry = rollback_brand_migration(manifest)
                self.assertEqual(retry["status"], "rolled_back")

    def test_post_rebuild_checkpoint_supports_exact_manual_recovery(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            (vault / "00-Rules").mkdir()
            plan = build_migration_plan(vault)
            before = snapshot_public_tree(vault)
            original_publish = brand_migration._publish_checkpoint
            interrupted = []

            def interrupt(handle, active_plan, phase, boundary, *args, **kwargs):
                result = original_publish(
                    handle,
                    active_plan,
                    phase,
                    boundary,
                    *args,
                    **kwargs,
                )
                if (
                    not interrupted
                    and phase == "rebuild:memory-index"
                    and boundary == "after"
                ):
                    interrupted.append(True)
                    raise brand_migration._CheckpointFailure("simulated post-rebuild crash")
                return result

            with patch(
                "brand_migration._publish_checkpoint",
                side_effect=interrupt,
            ):
                with self.assertRaisesRegex(RuntimeError, "recovery required"):
                    apply_brand_migration(plan, "post-rebuild-recovery")

            manifest = (
                vault
                / "04-Feedback/_rollback/brand-migration/post-rebuild-recovery"
                / "manifest.json"
            )
            rollback_brand_migration(manifest)
            self.assertEqual(snapshot_public_tree(vault), before)

    def test_ownership_loss_at_major_phase_stops_all_later_writes(self):
        import brand_migration

        cases = (
            ("source-before", lambda phase, boundary: phase == "source-rename" and boundary == "before"),
            ("source-after", lambda phase, boundary: phase == "source-rename" and boundary == "after"),
            ("rewrite-after", lambda phase, boundary: phase.startswith("rewrite:") and boundary == "after"),
            ("validation-before", lambda phase, boundary: phase == "validation-finalization" and boundary == "before"),
        )
        for label, matches in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw_vault:
                vault = Path(raw_vault).resolve()
                make_vault(vault)
                plan = build_migration_plan(vault)
                harvest = vault / "04-Feedback/_logs/harvester.lock"
                scanner = vault / "04-Feedback/scanner.lock"
                original_publish = brand_migration._publish_checkpoint
                takeover = []

                def lose_ownership(handle, active_plan, phase, boundary, *args, **kwargs):
                    record = original_publish(
                        handle,
                        active_plan,
                        phase,
                        boundary,
                        *args,
                        **kwargs,
                    )
                    if not takeover and matches(phase, boundary):
                        held = []
                        for lock in (harvest, scanner):
                            old = lock.with_name(lock.name + ".old-owner")
                            lock.rename(old)
                            held.append(old)
                            write_text(lock, '{"token": "replacement"}\n')
                        target = (
                            plan.destination_project / "Memory/decisions.md"
                            if plan.destination_project.exists()
                            else plan.source_project / "Memory/decisions.md"
                        )
                        with target.open("r+", encoding="utf-8") as stream:
                            stream.seek(0)
                            stream.write(f"replacement writer {label}\n")
                            stream.truncate()
                            stream.flush()
                            os.fsync(stream.fileno())
                        takeover.append((target, tuple(held), len(read_checkpoint_records(handle.path / "manifest.json"))))
                    return record

                with patch(
                    "brand_migration._publish_checkpoint",
                    side_effect=lose_ownership,
                ):
                    with self.assertRaisesRegex(RuntimeError, "recovery required"):
                        apply_brand_migration(
                            plan,
                            f"ownership-{label}",
                            rebuilders=[],
                        )

                target, held, checkpoint_count = takeover[0]
                self.assertEqual(
                    target.read_text(encoding="utf-8"),
                    f"replacement writer {label}\n",
                )
                manifest = (
                    vault
                    / "04-Feedback/_rollback/brand-migration"
                    / f"ownership-{label}"
                    / "manifest.json"
                )
                self.assertEqual(len(read_checkpoint_records(manifest)), checkpoint_count)
                for lock in (harvest, scanner):
                    lock.unlink()
                for old in held:
                    old.unlink()
                rollback_brand_migration(manifest, force=True)
                self.assertTrue(plan.source_project.is_dir())
                self.assertFalse(plan.destination_project.exists())

    def test_interrupted_rollback_resumes_from_last_exact_checkpoint(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            before = snapshot_public_tree(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "interrupted-rollback",
                rebuilders=[],
            )
            original_publish = brand_migration._publish_checkpoint
            interrupted = []

            def interrupt(handle, active_plan, phase, boundary, *args, **kwargs):
                record = original_publish(
                    handle,
                    active_plan,
                    phase,
                    boundary,
                    *args,
                    **kwargs,
                )
                if (
                    not interrupted
                    and phase.startswith("rollback-restore-file:")
                    and boundary == "after"
                ):
                    interrupted.append(True)
                    raise brand_migration._CheckpointFailure("simulated rollback crash")
                return record

            with patch(
                "brand_migration._publish_checkpoint",
                side_effect=interrupt,
            ):
                with self.assertRaisesRegex(RuntimeError, "rollback crash"):
                    rollback_brand_migration(result["manifest_path"])

            rollback_brand_migration(result["manifest_path"])
            self.assertEqual(snapshot_public_tree(vault), before)

    def test_checkpoint_loader_ignores_temp_and_rejects_forged_complete_record(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(plan, "checkpoint-security", rebuilders=[])
            manifest = Path(result["manifest_path"])
            parent = manifest.parent / "journal"
            os.chmod(parent, 0o700)
            incomplete = parent / "checkpoint-tmp-incomplete"
            incomplete.mkdir(mode=0o500)
            os.chmod(parent, 0o500)

            rollback_brand_migration(manifest)
            self.assertFalse(incomplete.exists())

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(plan, "checkpoint-temp-symlink", rebuilders=[])
            manifest = Path(result["manifest_path"])
            journal = manifest.parent / "journal"
            outside = tmp / "outside-temp-target"
            outside.mkdir()
            os.chmod(journal, 0o700)
            unexpected = journal / "checkpoint-tmp-unexpected"
            unexpected.symlink_to(outside, target_is_directory=True)
            os.chmod(journal, 0o500)

            with self.assertRaisesRegex(
                (ValueError, RuntimeError),
                "checkpoint|journal|temp",
            ):
                rollback_brand_migration(manifest, force=True)

            self.assertTrue(unexpected.is_symlink())
            self.assertEqual(list(outside.iterdir()), [])

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(plan, "checkpoint-forgery", rebuilders=[])
            manifest = Path(result["manifest_path"])
            forged = (
                manifest.parent
                / "journal"
                / ("checkpoint-99999999-" + "0" * 64)
            )
            os.chmod(forged.parent, 0o700)
            forged.mkdir(mode=0o500)
            os.chmod(forged.parent, 0o500)

            with self.assertRaisesRegex(
                (ValueError, RuntimeError),
                "checkpoint",
            ):
                rollback_brand_migration(manifest, force=True)

    def test_checkpoint_loader_rejects_valid_digest_forged_terminal_extension(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "valid-terminal-forgery",
                rebuilders=[],
            )
            manifest = Path(result["manifest_path"])
            records = read_checkpoint_records(manifest)
            _sequence, previous_digest, latest, _directory = records[-1]
            forged = dict(latest)
            forged.update(
                {
                    "sequence": latest["sequence"] + 1,
                    "previous_record_sha256": previous_digest,
                    "status": "rolled_back",
                    "phase": "rollback-complete",
                    "boundary": "after",
                    "recorded_at": "2026-07-12T00:00:00+00:00",
                }
            )
            raw = (json.dumps(
                forged,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ) + "\n").encode("utf-8")
            digest = hashlib.sha256(raw).hexdigest()
            forged_dir = (
                manifest.parent
                / "journal"
                / f"checkpoint-{forged['sequence']:08d}-{digest}"
            )
            os.chmod(forged_dir.parent, 0o700)
            forged_dir.mkdir(mode=0o700)
            (forged_dir / "record.json").write_bytes(raw)
            os.chmod(forged_dir / "record.json", 0o400)
            os.chmod(forged_dir, 0o500)
            os.chmod(forged_dir.parent, 0o500)

            with self.assertRaisesRegex(
                (ValueError, RuntimeError),
                "checkpoint|transition|journal",
            ):
                rollback_brand_migration(manifest, force=True)

            self.assertFalse(plan.source_project.exists())
            self.assertTrue(plan.destination_project.is_dir())

    def test_checkpoint_loader_rejects_skipped_required_cleanup(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            original_publish = brand_migration._publish_checkpoint
            interrupted = []

            def interrupt_after_mutation_checkpoint(
                handle,
                active_plan,
                phase,
                boundary,
                *args,
                **kwargs,
            ):
                record = original_publish(
                    handle,
                    active_plan,
                    phase,
                    boundary,
                    *args,
                    **kwargs,
                )
                if (
                    not interrupted
                    and phase.startswith("rewrite:")
                    and boundary == "after"
                ):
                    interrupted.append(True)
                    raise brand_migration._CheckpointFailure(
                        "failure after mutation checkpoint"
                    )
                return record

            with patch(
                "brand_migration._publish_checkpoint",
                side_effect=interrupt_after_mutation_checkpoint,
            ):
                with self.assertRaisesRegex(RuntimeError, "recovery required"):
                    apply_brand_migration(
                        plan,
                        "skipped-required-cleanup",
                        rebuilders=[],
                    )

            manifest = (
                vault
                / "04-Feedback/_rollback/brand-migration"
                / "skipped-required-cleanup"
                / "manifest.json"
            )
            records = read_checkpoint_records(manifest)
            sequence, previous_digest, latest, directory = records[-1]
            self.assertEqual(latest["boundary"], "after")
            self.assertEqual(latest["mutation_intent"]["operation"], "write-file")
            self.assertIsNotNone(latest["mutation_intent"]["before"])
            staging = checkpoint_record_path(
                vault,
                latest["mutation_intent"]["staging"],
            )
            self.assertTrue(staging.is_file())

            forged = {
                key: value
                for key, value in latest.items()
                if key
                not in {
                    "snapshot",
                    "delta",
                    "validation",
                    "mutation_intent",
                }
            }
            forged.update(
                {
                    "sequence": sequence + 1,
                    "previous_record_sha256": previous_digest,
                    "previous_record_binding": checkpoint_test_record_binding(
                        directory
                    ),
                    "status": "applying",
                    "phase": "validation-finalization",
                    "boundary": "before",
                    "recorded_at": "2026-07-12T00:00:00+00:00",
                    "delta": {},
                }
            )
            raw = (
                json.dumps(
                    forged,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode("utf-8")
            digest = hashlib.sha256(raw).hexdigest()
            journal = manifest.parent / "journal"
            forged_dir = journal / f"checkpoint-{sequence + 1:08d}-{digest}"
            os.chmod(journal, 0o700)
            forged_dir.mkdir(mode=0o700)
            (forged_dir / "record.json").write_bytes(raw)
            os.chmod(forged_dir / "record.json", 0o400)
            os.chmod(forged_dir, 0o500)
            os.chmod(journal, 0o500)

            with self.assertRaisesRegex(
                (ValueError, RuntimeError),
                "required cleanup checkpoint is missing",
            ):
                rollback_brand_migration(manifest, force=True)

            self.assertTrue(staging.is_file())

    def test_reconciled_cleanup_survives_rollback_start_crash(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            before = snapshot_public_tree(vault)
            plan = build_migration_plan(vault)
            original_publish = brand_migration._publish_checkpoint
            apply_interrupted = []

            def interrupt_after_mutation_checkpoint(
                handle,
                active_plan,
                phase,
                boundary,
                *args,
                **kwargs,
            ):
                record = original_publish(
                    handle,
                    active_plan,
                    phase,
                    boundary,
                    *args,
                    **kwargs,
                )
                if (
                    not apply_interrupted
                    and phase.startswith("rewrite:")
                    and boundary == "after"
                ):
                    apply_interrupted.append(True)
                    raise brand_migration._CheckpointFailure(
                        "failure after mutation checkpoint"
                    )
                return record

            with patch(
                "brand_migration._publish_checkpoint",
                side_effect=interrupt_after_mutation_checkpoint,
            ):
                with self.assertRaisesRegex(RuntimeError, "recovery required"):
                    apply_brand_migration(
                        plan,
                        "reconciled-cleanup-rollback-retry",
                        rebuilders=[],
                    )

            manifest = (
                vault
                / "04-Feedback/_rollback/brand-migration"
                / "reconciled-cleanup-rollback-retry"
                / "manifest.json"
            )
            rollback_interrupted = []

            def interrupt_after_rollback_start(
                handle,
                active_plan,
                phase,
                boundary,
                *args,
                **kwargs,
            ):
                record = original_publish(
                    handle,
                    active_plan,
                    phase,
                    boundary,
                    *args,
                    **kwargs,
                )
                if not rollback_interrupted and phase == "rollback-start":
                    rollback_interrupted.append(True)
                    raise brand_migration._CheckpointFailure(
                        "failure after rollback-start publication"
                    )
                return record

            with patch(
                "brand_migration._publish_checkpoint",
                side_effect=interrupt_after_rollback_start,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "rollback-start publication",
                ):
                    rollback_brand_migration(manifest, force=False)

            self.assertEqual(rollback_interrupted, [True])
            result = rollback_brand_migration(manifest, force=False)
            self.assertEqual(result["status"], "rolled_back")
            self.assertEqual(snapshot_public_tree(vault), before)

    def test_before_intent_reconciliation_survives_rollback_start_crash(self):
        import brand_migration

        cases = (
            (
                "source-before",
                lambda phase, boundary: (
                    phase == "source-rename" and boundary == "after"
                ),
            ),
            (
                "write-before",
                lambda phase, boundary: (
                    phase.startswith("rewrite:") and boundary == "after"
                ),
            ),
        )
        for label, matches in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as raw_vault:
                vault = Path(raw_vault).resolve()
                make_vault(vault)
                before = snapshot_public_tree(vault)
                plan = build_migration_plan(vault)
                original_publish = brand_migration._publish_checkpoint
                apply_interrupted = []

                def interrupt_before_after_checkpoint(
                    handle,
                    active_plan,
                    phase,
                    boundary,
                    *args,
                    **kwargs,
                ):
                    if not apply_interrupted and matches(phase, boundary):
                        apply_interrupted.append(True)
                        raise brand_migration._CheckpointFailure(
                            "failure before after-checkpoint publication"
                        )
                    return original_publish(
                        handle,
                        active_plan,
                        phase,
                        boundary,
                        *args,
                        **kwargs,
                    )

                migration_id = f"before-intent-rollback-retry-{label}"
                with patch(
                    "brand_migration._publish_checkpoint",
                    side_effect=interrupt_before_after_checkpoint,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "recovery required",
                    ):
                        apply_brand_migration(
                            plan,
                            migration_id,
                            rebuilders=[],
                        )

                manifest = (
                    vault
                    / "04-Feedback/_rollback/brand-migration"
                    / migration_id
                    / "manifest.json"
                )
                rollback_interrupted = []

                def interrupt_after_rollback_start(
                    handle,
                    active_plan,
                    phase,
                    boundary,
                    *args,
                    **kwargs,
                ):
                    record = original_publish(
                        handle,
                        active_plan,
                        phase,
                        boundary,
                        *args,
                        **kwargs,
                    )
                    if not rollback_interrupted and phase == "rollback-start":
                        rollback_interrupted.append(True)
                        raise brand_migration._CheckpointFailure(
                            "failure after rollback-start publication"
                        )
                    return record

                with patch(
                    "brand_migration._publish_checkpoint",
                    side_effect=interrupt_after_rollback_start,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "rollback-start publication",
                    ):
                        rollback_brand_migration(manifest, force=False)

                self.assertEqual(rollback_interrupted, [True])
                fresh_handle = brand_migration._open_backup_handle(manifest)
                try:
                    brand_migration._load_checkpoint_chain(fresh_handle)
                    self.assertEqual(
                        fresh_handle.checkpoint_rollback_basis,
                        reconstruct_checkpoint_application(
                            read_checkpoint_records(manifest)
                        ),
                    )
                finally:
                    brand_migration._close_backup_handle(fresh_handle)
                result = rollback_brand_migration(manifest, force=False)
                self.assertEqual(result["status"], "rolled_back")
                self.assertEqual(snapshot_public_tree(vault), before)

    def test_checkpoint_loader_rejects_complete_noop_rollback_program(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "complete-noop-rollback-forgery",
                rebuilders=[],
            )
            manifest = Path(result["manifest_path"])
            records = read_checkpoint_records(manifest)
            application = reconstruct_checkpoint_application(records)
            previous_digest = records[-1][1]
            previous_binding = checkpoint_test_record_binding(records[-1][3])
            sequence = records[-1][0]
            template = records[-1][2]

            def mapped(path):
                try:
                    return plan.destination_project / path.relative_to(
                        plan.source_project
                    )
                except ValueError:
                    return path

            removal_files = {
                checkpoint_record_path(vault, item)
                for item in application["created_files"]
            }
            removal_files.update(
                mapped(binding.path)
                for binding in plan.input_bindings
                if binding.path == plan.source_project
                or binding.path.is_relative_to(plan.source_project)
            )
            removal_directories = {
                checkpoint_record_path(vault, item)
                for item in application["created_directories"]
            }
            removal_directories.update(
                mapped(path)
                for path in plan.mutation_contract.mutable_directories
                if path == plan.source_project
                or path.is_relative_to(plan.source_project)
            )
            phases = ["rollback-start"]
            phases.extend(
                f"rollback-remove-file:{path}"
                for path in sorted(removal_files, reverse=True)
            )
            phases.extend(
                f"rollback-remove-directory:{path}"
                for path in sorted(removal_directories, reverse=True)
            )
            phases.append("rollback-restore-directories")
            phases.extend(
                f"rollback-restore-file:{binding.path}"
                for binding in plan.input_bindings
            )
            phases.extend(
                f"rollback-directory-metadata:{binding.path}"
                for binding in sorted(
                    plan.directory_bindings,
                    key=lambda item: (len(item.path.parts), str(item.path)),
                    reverse=True,
                )
            )
            phases.append("rollback-complete")

            journal = manifest.parent / "journal"
            os.chmod(journal, 0o700)
            for phase in phases:
                sequence += 1
                status = "rolled_back" if phase == "rollback-complete" else "rolling_back"
                boundary = "before" if phase == "rollback-start" else "after"
                previous_digest, previous_binding = append_forged_checkpoint(
                    journal,
                    template,
                    sequence,
                    previous_digest,
                    previous_binding,
                    phase,
                    boundary,
                    status,
                    {},
                )
            os.chmod(journal, 0o500)

            with self.assertRaisesRegex(
                (ValueError, RuntimeError),
                "terminal.*Task 6 pre-state|terminal.*pre-state",
            ):
                rollback_brand_migration(manifest, force=True)

            self.assertFalse(plan.source_project.exists())
            self.assertTrue(plan.destination_project.is_dir())

    def test_checkpoint_append_rejects_same_digest_record_inode_replacement(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "record-inode-replacement",
                rebuilders=[],
            )
            manifest = Path(result["manifest_path"])
            handle = brand_migration._open_backup_handle(manifest)
            original_rename = brand_migration._rename_exclusive
            replaced = []
            try:
                chain = brand_migration._load_checkpoint_chain(handle)
                head = manifest.parent / "journal" / chain[-1][2]
                original_record_inode = os.stat(
                    head / "record.json",
                    follow_symlinks=False,
                ).st_ino

                def replace_record_before_publish(
                    source_fd,
                    source_name,
                    destination_fd,
                    destination_name,
                ):
                    if (
                        not replaced
                        and source_fd == handle.journal_fd
                        and destination_fd == handle.journal_fd
                        and source_name.startswith("checkpoint-tmp-")
                        and destination_name.startswith("checkpoint-")
                    ):
                        raw = (head / "record.json").read_bytes()
                        os.chmod(head, 0o700)
                        swap = head / "record.swap"
                        swap.write_bytes(raw)
                        os.chmod(swap, 0o400)
                        os.replace(swap, head / "record.json")
                        os.chmod(head, 0o500)
                        replaced.append(True)
                    return original_rename(
                        source_fd,
                        source_name,
                        destination_fd,
                        destination_name,
                    )

                with patch(
                    "brand_migration._rename_exclusive",
                    side_effect=replace_record_before_publish,
                ):
                    with self.assertRaisesRegex(
                        brand_migration._CheckpointFailure,
                        "record|head|authority|checkpoint",
                    ):
                        brand_migration._publish_checkpoint(
                            handle,
                            plan,
                            "rollback-start",
                            "before",
                            "rolling_back",
                            lambda: None,
                        )

                self.assertTrue(replaced)
                self.assertNotEqual(
                    os.stat(head / "record.json", follow_symlinks=False).st_ino,
                    original_record_inode,
                )
            finally:
                brand_migration._close_backup_handle(handle)

    def test_interrupted_append_is_recoverable_and_lease_loss_stops_writes(self):
        import brand_migration

        with self.subTest(case="observed-lease-loss"), tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "append-lease-loss",
                rebuilders=[],
            )
            manifest = Path(result["manifest_path"])
            journal = manifest.parent / "journal"
            before_records = len(read_checkpoint_records(manifest))
            handle = brand_migration._open_backup_handle(manifest)
            calls = 0
            journal_unsealed = False
            original_fchmod = os.fchmod

            def lose_during_append():
                nonlocal calls
                calls += 1
                if journal_unsealed:
                    raise RuntimeError("simulated writer guard ownership lost")

            def observe_unseal(fd, mode):
                nonlocal journal_unsealed
                result = original_fchmod(fd, mode)
                if fd == handle.journal_fd and mode == 0o700:
                    journal_unsealed = True
                return result

            try:
                with patch("brand_migration.os.fchmod", side_effect=observe_unseal):
                    with self.assertRaisesRegex(
                        brand_migration._CheckpointFailure,
                        "ownership lost",
                    ):
                        brand_migration._publish_checkpoint(
                            handle,
                            plan,
                            "rollback-start",
                            "before",
                            "rolling_back",
                            lose_during_append,
                        )
            finally:
                brand_migration._close_backup_handle(handle)

            self.assertGreaterEqual(calls, 4)
            self.assertEqual(len(read_checkpoint_records(manifest)), before_records)
            self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o700)

        with self.subTest(case="durable-crash-temp"), tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            before = snapshot_public_tree(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "append-crash-recovery",
                rebuilders=[],
            )
            manifest = Path(result["manifest_path"])
            journal = manifest.parent / "journal"
            latest_raw = (read_checkpoint_records(manifest)[-1][3] / "record.json").read_bytes()
            os.chmod(journal, 0o700)
            partial = journal / "checkpoint-tmp-partial"
            partial.mkdir(mode=0o700)
            (partial / "record.json").write_bytes(b'{"schema_version":')
            os.chmod(partial / "record.json", 0o600)
            complete = journal / "checkpoint-tmp-complete"
            complete.mkdir(mode=0o700)
            (complete / "record.json").write_bytes(latest_raw)
            os.chmod(complete / "record.json", 0o400)
            os.chmod(complete, 0o500)

            rollback_brand_migration(manifest, force=True)

            self.assertEqual(snapshot_public_tree(vault), before)
            self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o500)
            self.assertFalse(partial.exists())
            self.assertFalse(complete.exists())

    def test_checkpoint_append_revalidates_deleted_cached_head(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "cached-head-deletion",
                rebuilders=[],
            )
            manifest = Path(result["manifest_path"])
            handle = brand_migration._open_backup_handle(manifest)
            try:
                chain = brand_migration._load_checkpoint_chain(handle)
                head = manifest.parent / "journal" / chain[-1][2]
                original_load = brand_migration._load_checkpoint_chain
                loads = 0

                def delete_after_load(active_handle):
                    nonlocal loads
                    current = original_load(active_handle)
                    loads += 1
                    if loads == 1:
                        os.chmod(head.parent, 0o700)
                        os.chmod(head, 0o700)
                        os.chmod(head / "record.json", 0o600)
                        (head / "record.json").unlink()
                        head.rmdir()
                        os.chmod(head.parent, 0o500)
                    return current

                with patch(
                    "brand_migration._load_checkpoint_chain",
                    side_effect=delete_after_load,
                ):
                    with self.assertRaisesRegex(
                        brand_migration._CheckpointFailure,
                        "checkpoint|journal|sequence|transition",
                    ):
                        brand_migration._publish_checkpoint(
                            handle,
                            plan,
                            "rollback-start",
                            "before",
                            "rolling_back",
                            lambda: None,
                        )
            finally:
                brand_migration._close_backup_handle(handle)

    def test_checkpoint_load_rejects_namespace_injection_after_enumeration(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            before = snapshot_public_tree(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "load-namespace-injection",
                rebuilders=[],
            )
            manifest = Path(result["manifest_path"])
            applied = snapshot_public_tree(vault)
            records = read_checkpoint_records(manifest)
            head_raw = (records[-1][3] / "record.json").read_bytes()
            head_digest = hashlib.sha256(head_raw).hexdigest()
            journal = manifest.parent / "journal"
            injected_name = f"checkpoint-99999998-{head_digest}"
            injected = journal / injected_name
            handle = brand_migration._open_backup_handle(manifest)
            original_application_index = brand_migration._application_index
            injected_once = False

            def inject_after_complete_enumeration(application):
                nonlocal injected_once
                if not injected_once:
                    os.chmod(journal, 0o700)
                    injected.mkdir(mode=0o700)
                    (injected / "record.json").write_bytes(head_raw)
                    os.chmod(injected / "record.json", 0o400)
                    os.chmod(injected, 0o500)
                    os.chmod(journal, 0o500)
                    injected_once = True
                return original_application_index(application)

            try:
                with patch(
                    "brand_migration._application_index",
                    side_effect=inject_after_complete_enumeration,
                ):
                    with self.assertRaisesRegex(
                        (RuntimeError, ValueError),
                        "journal|namespace|checkpoint|authority",
                    ):
                        brand_migration._load_checkpoint_chain(handle)
                self.assertTrue(injected_once)
                self.assertIsNone(handle.checkpoint_chain)
                self.assertIsNone(handle.checkpoint_application_index)
                self.assertIsNone(handle.checkpoint_journal_authority)
                self.assertIsNone(handle.checkpoint_journal_inventory)
                self.assertIsNone(handle.checkpoint_rollback_basis)
                self.assertIsNone(handle.validated_snapshot_digest)
                self.assertEqual(snapshot_public_tree(vault), applied)
                self.assertTrue(injected.is_dir())
            finally:
                brand_migration._close_backup_handle(handle)
                if injected.exists():
                    os.chmod(journal, 0o700)
                    os.chmod(injected, 0o700)
                    os.chmod(injected / "record.json", 0o600)
                    (injected / "record.json").unlink()
                    injected.rmdir()
                    os.chmod(journal, 0o500)

            rollback_brand_migration(manifest, force=True)
            self.assertEqual(snapshot_public_tree(vault), before)

    def test_checkpoint_load_rejects_non_head_record_replacement_after_read(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "inner-record-drift",
                rebuilders=[],
            )
            manifest = Path(result["manifest_path"])
            records = read_checkpoint_records(manifest)
            target_directory = records[0][3]
            target_record = target_directory / "record.json"
            original_raw = target_record.read_bytes()
            original_inode = os.stat(target_record, follow_symlinks=False).st_ino
            original_fstat = os.fstat
            target_fstats = 0
            replaced = False
            handle = brand_migration._open_backup_handle(manifest)

            def replace_after_second_descriptor_stat(fd):
                nonlocal target_fstats, replaced
                current = original_fstat(fd)
                if current.st_ino == original_inode:
                    target_fstats += 1
                    if target_fstats == 2:
                        os.chmod(target_directory, 0o700)
                        replacement = target_directory / "record.replacement"
                        replacement.write_bytes(original_raw)
                        os.chmod(replacement, 0o400)
                        os.replace(replacement, target_record)
                        os.chmod(target_directory, 0o500)
                        replaced = True
                return current

            try:
                with patch(
                    "brand_migration.os.fstat",
                    side_effect=replace_after_second_descriptor_stat,
                ):
                    with self.assertRaisesRegex(
                        (RuntimeError, ValueError),
                        "checkpoint.*record|checkpoint.*directory|sealed.*changed",
                    ):
                        brand_migration._load_checkpoint_chain(handle)
                self.assertTrue(replaced)
                self.assertNotEqual(
                    os.stat(target_record, follow_symlinks=False).st_ino,
                    original_inode,
                )
                self.assertIsNone(handle.checkpoint_chain)
                self.assertIsNone(handle.checkpoint_application_index)
                self.assertIsNone(handle.checkpoint_head_authority)
            finally:
                brand_migration._close_backup_handle(handle)

    def test_checkpoint_append_rejects_complete_fork_injected_before_rename(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            before = snapshot_public_tree(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "append-namespace-injection",
                rebuilders=[],
            )
            manifest = Path(result["manifest_path"])
            applied = snapshot_public_tree(vault)
            journal = manifest.parent / "journal"
            handle = brand_migration._open_backup_handle(manifest)
            original_rename = brand_migration._rename_exclusive
            injected = None
            chain_before = None
            application_before = None
            try:
                chain = brand_migration._load_checkpoint_chain(handle)
                chain_before = list(chain)
                application_before = brand_migration._materialize_application(
                    handle.checkpoint_application_index
                )
                head_raw = (
                    journal / chain[-1][2] / "record.json"
                ).read_bytes()
                head_digest = hashlib.sha256(head_raw).hexdigest()
                injected = journal / (
                    f"checkpoint-{len(chain) + 1:08d}-{head_digest}"
                )

                def inject_fork_before_publish(
                    source_fd,
                    source_name,
                    destination_fd,
                    destination_name,
                ):
                    if (
                        not injected.exists()
                        and source_fd == handle.journal_fd
                        and destination_fd == handle.journal_fd
                        and source_name.startswith("checkpoint-tmp-")
                        and destination_name.startswith("checkpoint-")
                    ):
                        injected.mkdir(mode=0o700)
                        (injected / "record.json").write_bytes(head_raw)
                        os.chmod(injected / "record.json", 0o400)
                        os.chmod(injected, 0o500)
                    return original_rename(
                        source_fd,
                        source_name,
                        destination_fd,
                        destination_name,
                    )

                with patch(
                    "brand_migration._rename_exclusive",
                    side_effect=inject_fork_before_publish,
                ):
                    with self.assertRaisesRegex(
                        brand_migration._CheckpointFailure,
                        "journal|namespace|checkpoint|authority|inventory",
                    ):
                        brand_migration._publish_checkpoint(
                            handle,
                            plan,
                            "rollback-start",
                            "before",
                            "rolling_back",
                            lambda: None,
                        )

                self.assertTrue(injected.is_dir())
                self.assertEqual(handle.checkpoint_chain, chain_before)
                self.assertEqual(
                    brand_migration._materialize_application(
                        handle.checkpoint_application_index
                    ),
                    application_before,
                )
                self.assertEqual(snapshot_public_tree(vault), applied)
            finally:
                brand_migration._close_backup_handle(handle)
                if injected is not None and injected.exists():
                    os.chmod(journal, 0o700)
                    os.chmod(injected, 0o700)
                    os.chmod(injected / "record.json", 0o600)
                    (injected / "record.json").unlink()
                    injected.rmdir()
                    os.chmod(journal, 0o500)

            rollback_brand_migration(manifest, force=True)
            self.assertEqual(snapshot_public_tree(vault), before)

    def test_failed_checkpoint_publication_does_not_mutate_application_cache(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            before = snapshot_public_tree(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "failed-publication-cache",
                rebuilders=[],
            )
            manifest = Path(result["manifest_path"])
            handle = brand_migration._open_backup_handle(manifest)
            try:
                brand_migration._load_checkpoint_chain(handle)
                brand_migration._publish_checkpoint(
                    handle,
                    plan,
                    "rollback-start",
                    "before",
                    "rolling_back",
                    lambda: None,
                )
                chain_before = list(handle.checkpoint_chain)
                application_before = brand_migration._materialize_application(
                    handle.checkpoint_application_index
                )
                original = application_before["post_bindings"][0]
                changed = dict(original)
                changed["sha256"] = "f" * 64
                target = checkpoint_record_path(vault, original)
                delta = {"post_bindings": {"upsert": [changed], "remove": []}}

                with patch(
                    "brand_migration._targeted_checkpoint_delta",
                    return_value=(delta, None),
                ), patch(
                    "brand_migration._rename_exclusive",
                    side_effect=RuntimeError("simulated publication failure"),
                ):
                    with self.assertRaisesRegex(
                        brand_migration._CheckpointFailure,
                        "simulated publication failure",
                    ):
                        brand_migration._publish_checkpoint(
                            handle,
                            plan,
                            f"rollback-remove-file:{target}",
                            "after",
                            "rolling_back",
                            lambda: None,
                        )

                self.assertEqual(handle.checkpoint_chain, chain_before)
                self.assertEqual(
                    brand_migration._materialize_application(
                        handle.checkpoint_application_index
                    ),
                    application_before,
                )
            finally:
                brand_migration._close_backup_handle(handle)

            rollback_brand_migration(manifest, force=True)
            self.assertEqual(snapshot_public_tree(vault), before)

    def test_post_publication_head_pin_failure_invalidates_cache_and_reloads(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            before = snapshot_public_tree(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "post-publication-pin-failure",
                rebuilders=[],
            )
            manifest = Path(result["manifest_path"])
            handle = brand_migration._open_backup_handle(manifest)
            original_open = os.open
            pin_failed = False
            try:
                chain = brand_migration._load_checkpoint_chain(handle)
                record_count = len(chain)

                def fail_new_head_directory_open(path, flags, *args, **kwargs):
                    nonlocal pin_failed
                    if (
                        not pin_failed
                        and isinstance(path, str)
                        and path.startswith("checkpoint-")
                        and any(
                            frame.function == "_pin_checkpoint_head"
                            for frame in inspect.stack()
                        )
                    ):
                        pin_failed = True
                        raise OSError("simulated descriptor exhaustion during head pin")
                    return original_open(path, flags, *args, **kwargs)

                with patch(
                    "brand_migration.os.open",
                    side_effect=fail_new_head_directory_open,
                ):
                    with self.assertRaisesRegex(
                        brand_migration._CheckpointFailure,
                        "descriptor exhaustion during head pin",
                    ):
                        brand_migration._publish_checkpoint(
                            handle,
                            plan,
                            "rollback-start",
                            "before",
                            "rolling_back",
                            lambda: None,
                        )

                self.assertTrue(pin_failed)
                self.assertEqual(
                    len(read_checkpoint_records(manifest)),
                    record_count + 1,
                )
                self.assertIsNone(handle.checkpoint_chain)
                self.assertIsNone(handle.checkpoint_application_index)
                self.assertEqual(handle.checkpoint_seen_phases, set())
                self.assertIsNone(handle.checkpoint_rollback_basis)
                self.assertIsNone(handle.checkpoint_journal_authority)
                self.assertIsNone(handle.checkpoint_journal_inventory)
                self.assertIsNone(handle.checkpoint_head_directory_fd)
                self.assertIsNone(handle.checkpoint_head_record_fd)
                self.assertIsNone(handle.checkpoint_head_authority)

                reloaded = brand_migration._load_checkpoint_chain(handle)
                self.assertEqual(len(reloaded), record_count + 1)
                self.assertEqual(reloaded[-1][0]["phase"], "rollback-start")
                self.assertEqual(reloaded[-1][0]["status"], "rolling_back")
            finally:
                brand_migration._close_backup_handle(handle)

            rollback_brand_migration(manifest, force=True)
            self.assertEqual(snapshot_public_tree(vault), before)

    def test_checkpoint_storage_and_hash_visits_scale_linearly(self):
        import brand_migration

        measurements = []
        for count in (3, 6):
            with tempfile.TemporaryDirectory() as raw_vault:
                vault = Path(raw_vault).resolve()
                source = make_vault(vault)
                for index in range(count):
                    write_text(
                        source / "Memory" / f"scale-{index}.md",
                        "[[01-Projects/github-obsidian-knowledge-brain/Memory/decisions]]\n",
                    )
                plan = build_migration_plan(vault)
                visits = 0
                original_capture = brand_migration._capture_input_binding

                def count_capture(*args, **kwargs):
                    nonlocal visits
                    visits += 1
                    return original_capture(*args, **kwargs)

                with patch(
                    "brand_migration._capture_input_binding",
                    side_effect=count_capture,
                ):
                    result = apply_brand_migration(
                        plan,
                        f"linear-checkpoints-{count}",
                        rebuilders=[],
                    )
                records = read_checkpoint_records(Path(result["manifest_path"]))
                serialized = sum(
                    (directory / "record.json").stat().st_size
                    for _sequence, _digest, _record, directory in records
                )
                measurements.append((visits, serialized))

        small, large = measurements
        self.assertLessEqual(large[0], small[0] * 2.75)
        self.assertLessEqual(large[1], small[1] * 2.75)

    def test_checkpoint_reload_full_application_validation_is_bounded(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(
                plan,
                "bounded-chain-validation",
                rebuilders=[],
            )
            handle = brand_migration._open_backup_handle(
                Path(result["manifest_path"])
            )
            original_validate = brand_migration._validate_checkpoint_application
            validations = 0

            def count_validation(*args, **kwargs):
                nonlocal validations
                validations += 1
                return original_validate(*args, **kwargs)

            try:
                with patch(
                    "brand_migration._validate_checkpoint_application",
                    side_effect=count_validation,
                ):
                    chain = brand_migration._load_checkpoint_chain(handle)
                self.assertGreater(len(chain), 10)
                self.assertLessEqual(validations, 2)
            finally:
                brand_migration._close_backup_handle(handle)

    def test_checkpoint_application_sort_and_replay_work_are_practical(self):
        import brand_migration

        self.assertFalse(
            hasattr(brand_migration, "_canonical_application_items"),
            "custom byte-trie canonicalization must not return",
        )
        self.assertIn(
            "sorted(",
            inspect.getsource(brand_migration._sorted_application_items),
        )

        item_count = 4000
        large = {
            key: [
                {
                    "kind": "vault",
                    "path": f"legitimate/deep/path/{index:05d}/" + "x" * 160,
                    "value": index,
                }
                for index in range(item_count)
            ]
            for key in brand_migration._APPLICATION_KEYS
        }
        updated = {
            key: [
                {**item, "value": item["value"] + 1}
                for item in values
            ]
            for key, values in large.items()
        }
        forward = brand_migration._application_delta(large, updated)
        reverse = brand_migration._application_delta(
            {key: list(reversed(value)) for key, value in large.items()},
            {key: list(reversed(value)) for key, value in updated.items()},
        )
        self.assertEqual(
            brand_migration._serialize_manifest_bytes(forward),
            brand_migration._serialize_manifest_bytes(reverse),
        )
        self.assertEqual(
            brand_migration._materialize_application(
                brand_migration._application_index(large)
            ),
            brand_migration._materialize_application(
                brand_migration._application_index(
                    {key: list(reversed(value)) for key, value in large.items()}
                )
            ),
        )

        measurements = []
        for count in (3, 12):
            with tempfile.TemporaryDirectory() as raw_vault:
                vault = Path(raw_vault).resolve()
                source = make_vault(vault)
                for index in range(count):
                    write_text(
                        source / "Memory" / f"work-{index}.md",
                        "[[01-Projects/github-obsidian-knowledge-brain/Memory/decisions]]\n",
                    )
                plan = build_migration_plan(vault)
                work = 0
                original_index = brand_migration._application_index
                original_materialize = brand_migration._materialize_application
                original_delta_indexes = brand_migration._application_delta_indexes
                original_replay = brand_migration._apply_application_delta_index
                original_listdir = os.listdir

                def count_index(application):
                    nonlocal work
                    work += sum(
                        len(application[key])
                        for key in brand_migration._APPLICATION_KEYS
                    )
                    return original_index(application)

                def count_materialize(index):
                    nonlocal work
                    work += sum(len(index[key]) for key in brand_migration._APPLICATION_KEYS)
                    return original_materialize(index)

                def count_delta_indexes(previous, current):
                    nonlocal work
                    work += sum(
                        len(previous[key]) + len(current[key])
                        for key in brand_migration._APPLICATION_KEYS
                    )
                    return original_delta_indexes(previous, current)

                def count_replay(index, delta):
                    nonlocal work
                    work += sum(
                        len(operation.get("remove", ()))
                        + len(operation.get("upsert", ()))
                        for operation in delta.values()
                    )
                    return original_replay(index, delta)

                def count_candidates(target):
                    nonlocal work
                    entries = original_listdir(target)
                    if sys._getframe(1).f_code.co_name in {
                        "_load_checkpoint_chain",
                        "_load_checkpoint_chain_uncached",
                    }:
                        work += len(entries)
                    return entries

                with patch(
                    "brand_migration._application_index",
                    side_effect=count_index,
                ), patch(
                    "brand_migration._materialize_application",
                    side_effect=count_materialize,
                ), patch(
                    "brand_migration._application_delta_indexes",
                    side_effect=count_delta_indexes,
                ), patch(
                    "brand_migration._apply_application_delta_index",
                    side_effect=count_replay,
                ), patch(
                    "brand_migration.os.listdir",
                    side_effect=count_candidates,
                ):
                    result = apply_brand_migration(
                        plan,
                        f"linear-journal-work-{count}",
                        rebuilders=[],
                    )
                measurements.append(work)

        small_work, large_work = measurements
        self.assertGreater(small_work, 0)
        self.assertLessEqual(large_work, small_work * 5.5)

    def test_non_force_recovery_preserves_replacement_writer_bound_edit(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            context = tmp / "AGENTS.md"
            write_text(
                context,
                "preamble\n<!-- COMPILED:RULES_START -->\nold\n"
                "<!-- COMPILED:RULES_END -->\n"
                "<!-- COMPILED:PROJECTS_START -->\nold\n"
                "<!-- COMPILED:PROJECTS_END -->\n",
            )
            original = context.read_bytes()
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "context_targets": [str(context)],
                    },
                    sort_keys=False,
                ),
            )
            plan = build_migration_plan(vault, config_path=config)
            harvest = vault / "04-Feedback" / "_logs" / "harvester.lock"
            scanner = vault / "04-Feedback" / "scanner.lock"
            old_locks = []

            def replacement_writer(
                _cfg,
                ownership_check=None,
                mutation_io=None,
            ):
                for lock in (harvest, scanner):
                    held = lock.with_name(lock.name + ".old-owner")
                    lock.rename(held)
                    old_locks.append(held)
                    write_text(lock, '{"token": "replacement"}\n')
                with context.open("r+", encoding="utf-8") as stream:
                    stream.seek(0)
                    stream.write("replacement writer bytes\n")
                    stream.truncate()
                    stream.flush()
                    os.fsync(stream.fileno())
                ownership_check()

            with patch(
                "brand_migration.rebuild_memory_index",
                side_effect=replacement_writer,
                create=True,
            ):
                with self.assertRaisesRegex(RuntimeError, "recovery required"):
                    apply_brand_migration(plan, "replacement-writer-recovery")

            for lock in (harvest, scanner):
                lock.unlink()
            for held in old_locks:
                held.unlink()
            manifest = (
                vault
                / "04-Feedback"
                / "_rollback"
                / "brand-migration"
                / "replacement-writer-recovery"
                / "manifest.json"
            )
            replacement = context.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "rollback target changed"):
                rollback_brand_migration(manifest, force=False)
            self.assertEqual(context.read_bytes(), replacement)

            rollback_brand_migration(manifest, force=True)
            self.assertEqual(context.read_bytes(), original)

    def test_rewrite_changes_structural_fields_and_live_wikilinks_only(self):
        old = "github-obsidian-knowledge-brain"
        content = f"""---
project: {old}
projects: [{old}, notes-counter]
source_note: 01-Projects/{old}/Memory/decisions
summary: Historical text mentions {old} and stays unchanged.
---

[[ 01-Projects/{old} | {old} ]]
[[01-Projects/{old}/Memory/decisions#section\\| Decisions ]]
[[Other|{old}]]
# Decisions — {old}
Ordinary prose mentions {old} and stays unchanged.

~~~markdown
[[01-Projects/{old}/Memory/decisions|Documentation example]]
~~~
"""

        updated, changed = rewrite_markdown(content, old, "agent-memory-beacon")

        self.assertTrue(changed)
        parsed = yaml.safe_load(updated.split("---", 2)[1])
        self.assertEqual(parsed["project"], "agent-memory-beacon")
        self.assertEqual(parsed["projects"], ["agent-memory-beacon", "notes-counter"])
        self.assertEqual(
            parsed["source_note"],
            "01-Projects/agent-memory-beacon/Memory/decisions",
        )
        self.assertIn(
            f"[[ 01-Projects/agent-memory-beacon | {old} ]]",
            updated,
        )
        self.assertIn(
            "[[01-Projects/agent-memory-beacon/Memory/decisions#section\\| Decisions ]]",
            updated,
        )
        self.assertIn(f"[[Other|{old}]]", updated)
        self.assertIn(f"# Decisions — {old}", updated)
        self.assertIn(f"Historical text mentions {old}", updated)
        self.assertIn(f"Ordinary prose mentions {old}", updated)
        self.assertIn(
            f"[[01-Projects/{old}/Memory/decisions|Documentation example]]",
            updated,
        )

    def test_apply_validates_and_manual_rollback_restores_public_bytes(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            before = snapshot_public_tree(vault)
            calls = []
            plan = build_migration_plan(vault)

            with patch(
                "brand_migration._run_default_rebuilders",
                side_effect=lambda _cfg, *_args, **_kwargs: calls.append("default-chain"),
            ):
                result = apply_brand_migration(plan, "successful-migration")

            self.assertEqual(result["status"], "applied")
            self.assertEqual(calls, ["default-chain"])
            self.assertFalse(plan.source_project.exists())
            self.assertTrue(plan.destination_project.is_dir())
            self.assertEqual(result["new_broken_links"], 0)
            self.assertEqual(result["duplicate_memories"], 0)

            rollback_brand_migration(result["manifest_path"])

            self.assertEqual(snapshot_public_tree(vault), before)

    def test_rebuilder_failure_rolls_back_automatically(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            before = snapshot_public_tree(vault)
            plan = build_migration_plan(vault)

            def fail_rebuild(_cfg, *_args, **_kwargs):
                raise RuntimeError("forced rebuild failure")

            with patch(
                "brand_migration._run_default_rebuilders",
                side_effect=fail_rebuild,
            ):
                with self.assertRaisesRegex(RuntimeError, "forced rebuild failure"):
                    apply_brand_migration(plan, "failed-migration")

            self.assertEqual(snapshot_public_tree(vault), before)

    def test_rollback_restores_external_users_obsidian_temps_and_empty_directories(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            users_empty = vault / "Users" / "nested" / "empty"
            users_empty.mkdir(parents=True)
            write_text(vault / "Users" / "nested" / "keep.bin", "keep\n")
            write_text(vault / ".obsidian" / "app.json", "{\"old\": true}\n")
            external = tmp / "external-agent-memory"
            write_text(
                external / ".agent-memory-beacon-root",
                "owned by agent-memory-beacon\n",
            )
            write_text(external / "existing.md", "external before\n")
            context = tmp / "missing" / "AGENTS.md"
            context_tmp = Path(str(context) + ".tmp")
            context_restore = Path(str(context) + ".restore")
            write_text(context_tmp, "existing tmp\n")
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "agent_memory_path": str(external),
                        "context_targets": [str(context)],
                    },
                    sort_keys=False,
                ),
            )
            before = snapshot_without_migration_audit(tmp)
            directories_before = snapshot_directories_without_migration_audit(tmp)
            plan = build_migration_plan(vault, config_path=config)

            def mutate(cfg, *_args, **_kwargs):
                shutil.rmtree(vault / "Users")
                write_text(vault / ".obsidian" / "app.json", "{\"new\": true}\n")
                shutil.rmtree(Path(cfg["agent_memory_path"]))
                write_text(Path(cfg["agent_memory_path"]) / "new.md", "new\n")
                write_text(context, "created context\n")
                context_tmp.unlink()
                write_text(context_restore, "created restore\n")

            with patch(
                "brand_migration._run_default_rebuilders", side_effect=mutate
            ):
                result = apply_brand_migration(plan, "full-contract-rollback")
            rollback_brand_migration(result["manifest_path"])

            self.assertEqual(snapshot_without_migration_audit(tmp), before)
            self.assertEqual(
                snapshot_directories_without_migration_audit(tmp),
                directories_before,
            )

    def test_validation_rejects_same_count_different_memory_identities(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            plan.source_project.rename(plan.destination_project)
            decisions = plan.destination_project / "Memory" / "decisions.md"
            content = decisions.read_text(encoding="utf-8")
            decisions.write_text(
                content.replace("decision-1", "different-decision"),
                encoding="utf-8",
            )

            validation = validate_brand_migration(plan)

            self.assertFalse(validation["valid"])
            self.assertIn("memory identity keys changed", validation["message"])

    def test_validation_normalizes_moved_broken_link_sources_and_targets(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            source = make_vault(vault)
            write_text(
                source / "missing.md",
                "[[01-Projects/github-obsidian-knowledge-brain/missing-target]]\n",
            )
            plan = build_migration_plan(vault)
            source.rename(plan.destination_project)
            for path in plan.destination_project.rglob("*.md"):
                updated, _changed = rewrite_markdown(
                    path.read_text(encoding="utf-8"),
                    plan.old_slug,
                    plan.new_slug,
                )
                path.write_text(updated, encoding="utf-8")

            validation = validate_brand_migration(plan)

            self.assertEqual(validation["new_broken_links"], 0)

    def test_apply_rejects_unexpected_rebuilder_write_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            before = snapshot_public_tree(vault)
            plan = build_migration_plan(vault)

            def escape_contract(_cfg):
                write_text(vault / "unexpected-output.txt", "outside contract\n")

            with self.assertRaisesRegex(ValueError, "custom rebuilders are unsupported"):
                apply_brand_migration(
                    plan,
                    "unexpected-write",
                    rebuilders=[escape_contract],
                )

            self.assertEqual(snapshot_public_tree(vault), before)

    def test_apply_rejects_rebuilder_subprocess_write_outside_contract(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            outside = tmp / "subprocess-escape.txt"
            plan = build_migration_plan(vault)

            def escape_in_child(_cfg):
                subprocess.run(
                    [sys.executable, "-c", f"open({str(outside)!r}, 'w').write('bad')"],
                    check=True,
                )

            with self.assertRaisesRegex(ValueError, "custom rebuilders are unsupported"):
                apply_brand_migration(
                    plan,
                    "subprocess-write",
                    rebuilders=[escape_in_child],
                )

            self.assertFalse(outside.exists())
            self.assertTrue(plan.source_project.is_dir())

    def test_rebuilder_generator_is_rejected_without_advancing(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            plan = build_migration_plan(vault)
            marker = tmp / "generator-advanced.txt"

            def side_effecting_generator():
                write_text(marker, "advanced\n")
                yield lambda _cfg: None

            with self.assertRaisesRegex(ValueError, "custom rebuilders are unsupported"):
                apply_brand_migration(
                    plan,
                    "reject-generator",
                    rebuilders=side_effecting_generator(),
                )

            self.assertFalse(marker.exists())
            self.assertTrue(plan.source_project.is_dir())

    def test_empty_yield_generator_is_rejected_without_advancing(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            plan = build_migration_plan(vault)
            marker = tmp / "empty-generator-advanced.txt"

            def empty_side_effecting_generator():
                write_text(marker, "advanced\n")
                if False:
                    yield None

            with self.assertRaisesRegex(ValueError, "custom rebuilders are unsupported"):
                apply_brand_migration(
                    plan,
                    "reject-empty-generator",
                    rebuilders=empty_side_effecting_generator(),
                )

            self.assertFalse(marker.exists())
            self.assertTrue(plan.source_project.is_dir())

    def test_rebuilder_iter_protocol_is_rejected_without_invocation(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            plan = build_migration_plan(vault)
            marker = tmp / "iter-invoked.txt"

            class SideEffectingIterable:
                def __iter__(self):
                    write_text(marker, "iterated\n")
                    return iter(())

            with self.assertRaisesRegex(ValueError, "custom rebuilders are unsupported"):
                apply_brand_migration(
                    plan,
                    "reject-iter-protocol",
                    rebuilders=SideEffectingIterable(),
                )

            self.assertFalse(marker.exists())
            self.assertTrue(plan.source_project.is_dir())

    def test_empty_builtin_subclass_is_rejected_without_protocol_invocation(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            plan = build_migration_plan(vault)
            marker = tmp / "subclass-protocol.txt"

            class SideEffectingList(list):
                def __iter__(self):
                    write_text(marker, "iterated\n")
                    return super().__iter__()

                def __len__(self):
                    write_text(marker, "measured\n")
                    return super().__len__()

            with self.assertRaisesRegex(ValueError, "custom rebuilders are unsupported"):
                apply_brand_migration(
                    plan,
                    "reject-list-subclass",
                    rebuilders=SideEffectingList(),
                )

            self.assertFalse(marker.exists())
            self.assertTrue(plan.source_project.is_dir())

    def test_manual_rollback_rejects_forged_contract_and_path_kind(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(plan, "forged-manifest", rebuilders=[])
            manifest_path = Path(result["manifest_path"])
            before = snapshot_public_tree(vault)
            os.chmod(manifest_path.parent, 0o700)
            os.chmod(manifest_path, 0o600)
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            payload["mutation_contract"]["target_specs"][0]["kind"] = "external"
            manifest_path.write_bytes(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                ).encode("utf-8") + b"\n"
            )
            os.chmod(manifest_path, 0o400)
            os.chmod(manifest_path.parent, 0o500)

            with self.assertRaisesRegex(ValueError, "manifest mutation contract"):
                rollback_brand_migration(manifest_path, force=True)

            self.assertEqual(snapshot_public_tree(vault), before)

    def test_force_rollback_preserves_unrelated_user_file(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(plan, "force-preserves", rebuilders=[])
            unrelated = plan.destination_project / "user-note.txt"
            write_text(unrelated, "keep me\n")

            with self.assertRaisesRegex(RuntimeError, "rollback target changed"):
                rollback_brand_migration(result["manifest_path"])
            rollback_brand_migration(result["manifest_path"], force=True)

            self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep me\n")

    def test_apply_fails_closed_when_secure_descriptor_primitives_are_unavailable(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            before = snapshot_tree(vault)
            plan = build_migration_plan(vault)

            with patch("brand_migration._exclusive_rename_variant", return_value=None):
                with self.assertRaisesRegex(RuntimeError, "unavailable"):
                    apply_brand_migration(
                        plan,
                        "missing-primitives",
                        rebuilders=[],
                    )

            self.assertEqual(snapshot_tree(vault), before)

    def test_cli_preview_is_read_only_and_apply_requires_migration_id(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            before = snapshot_tree(vault)
            cli = Path(REPO_ROOT) / "scripts" / "migrate_brand.py"

            preview = subprocess.run(
                [sys.executable, str(cli), "--vault", str(vault)],
                check=True,
                capture_output=True,
                text=True,
            )
            payload = json.loads(preview.stdout)
            self.assertFalse(payload["writes_performed"])
            self.assertEqual(snapshot_tree(vault), before)

            apply_without_id = subprocess.run(
                [sys.executable, str(cli), "--vault", str(vault), "--apply"],
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(apply_without_id.returncode, 0)
            self.assertIn("--migration-id is required", apply_without_id.stderr)

    def test_active_harvester_aborts_before_backup_or_public_write(self):
        from session_harvester import acquire_harvest_lock, release_harvest_lock

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            active_lock = vault / "04-Feedback" / "_logs" / "harvester.lock"
            self.assertTrue(acquire_harvest_lock(str(active_lock)))
            plan = build_migration_plan(vault)
            before = snapshot_tree(vault)

            try:
                with self.assertRaisesRegex(RuntimeError, "harvester is active"):
                    apply_brand_migration(plan, "lock-conflict", rebuilders=[])

                self.assertEqual(snapshot_tree(vault), before)
                self.assertFalse(
                    vault.joinpath(
                        "04-Feedback/_rollback/brand-migration/lock-conflict"
                    ).exists()
                )
            finally:
                release_harvest_lock(str(active_lock))

    def test_manual_rollback_refuses_in_place_post_migration_drift(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            result = apply_brand_migration(plan, "rollback-drift", rebuilders=[])
            changed = plan.destination_project / "Memory" / "decisions.md"
            changed.write_text("user changed this after migration\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "rollback target changed"):
                rollback_brand_migration(result["manifest_path"])

            rollback_brand_migration(result["manifest_path"], force=True)
            self.assertTrue(plan.source_project.is_dir())

    def test_source_and_destination_races_fail_before_project_rename(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)

            def create_destination(active_plan, *rest):
                active_plan.destination_project.mkdir()
                return original(active_plan, *rest)

            import brand_migration

            original = brand_migration._rename_project_directory
            with patch(
                "brand_migration._rename_project_directory",
                side_effect=create_destination,
            ):
                with self.assertRaisesRegex(RuntimeError, "destination project appeared"):
                    apply_brand_migration(
                        plan,
                        "destination-race",
                        rebuilders=[],
                    )

            self.assertTrue(plan.source_project.is_dir())
            self.assertTrue(plan.destination_project.is_dir())

    def test_manifest_race_is_rejected_before_project_rename(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)

            def tamper_manifest(active_plan, *rest):
                backup_root = (
                    rest[0].path
                    if rest
                    else active_plan.vault
                    / "04-Feedback/_rollback/brand-migration/manifest-race"
                )
                manifest = backup_root / "manifest.json"
                os.chmod(backup_root, 0o700)
                os.chmod(manifest, 0o600)
                data = manifest.read_bytes().replace(b'"prepared"', b'"tampered"')
                manifest.write_bytes(data)
                os.chmod(manifest, 0o400)
                os.chmod(backup_root, 0o500)
                return original(active_plan, *rest)

            import brand_migration

            original = brand_migration._rename_project_directory
            with patch(
                "brand_migration._rename_project_directory",
                side_effect=tamper_manifest,
            ):
                with self.assertRaisesRegex(
                    (RuntimeError, ValueError), "manifest|backup"
                ):
                    apply_brand_migration(plan, "manifest-race", rebuilders=[])

            self.assertTrue(plan.source_project.is_dir())
            self.assertFalse(plan.destination_project.exists())

    def test_source_inode_swap_is_rejected_before_project_rename(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            displaced = plan.source_project.with_name("displaced-source")

            def swap_source(active_plan, *rest):
                active_plan.source_project.rename(displaced)
                active_plan.source_project.mkdir()
                return original(active_plan, *rest)

            import brand_migration

            original = brand_migration._rename_project_directory
            with patch(
                "brand_migration._rename_project_directory",
                side_effect=swap_source,
            ):
                with self.assertRaisesRegex(RuntimeError, "source project changed"):
                    apply_brand_migration(plan, "source-race", rebuilders=[])

            self.assertTrue(displaced.is_dir())
            self.assertFalse(plan.destination_project.exists())

    def test_source_disappearance_during_pin_closes_both_descriptors(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)

            import brand_migration

            real_open = os.open
            real_close = os.close
            parent_fd = real_open(plan.source_project.parent, os.O_RDONLY)
            opened_source_fds = []
            closed_fds = []

            def record_open(path, flags, *args, **kwargs):
                fd = real_open(path, flags, *args, **kwargs)
                if (
                    path == plan.source_project.name
                    and kwargs.get("dir_fd") == parent_fd
                ):
                    opened_source_fds.append(fd)
                return fd

            def disappear_at_named_stat(path, *args, **kwargs):
                if (
                    path == plan.source_project.name
                    and kwargs.get("dir_fd") == parent_fd
                ):
                    raise FileNotFoundError(path)
                return os.stat(path, *args, **kwargs)

            def record_close(fd):
                closed_fds.append(fd)
                return real_close(fd)

            with patch(
                "brand_migration._open_absolute_parent",
                return_value=(parent_fd, plan.source_project.name),
            ), patch(
                "brand_migration.os.open", side_effect=record_open
            ), patch(
                "brand_migration.os.stat", side_effect=disappear_at_named_stat
            ), patch(
                "brand_migration.os.close", side_effect=record_close
            ):
                with self.assertRaises(FileNotFoundError):
                    brand_migration._pin_project_source(plan)

            self.assertEqual(len(opened_source_fds), 1)
            self.assertIn(opened_source_fds[0], closed_fds)
            self.assertIn(parent_fd, closed_fds)

    def test_external_target_race_is_rejected_before_replacement(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "projects": ["github-obsidian-knowledge-brain"],
                    },
                    sort_keys=False,
                ),
            )
            plan = build_migration_plan(vault, config_path=config)

            def race_external(active_io, target, content, encoding="utf-8"):
                if Path(target) == config:
                    config.write_text("vault_path: changed-in-race\n", encoding="utf-8")
                return original_atomic_write(
                    active_io,
                    target,
                    content,
                    encoding=encoding,
                )

            import brand_migration

            original_atomic_write = brand_migration._MigrationIO.atomic_write
            with patch.object(
                brand_migration._MigrationIO,
                "atomic_write",
                new=race_external,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed|digest|size"):
                    apply_brand_migration(
                        plan,
                        "external-race",
                        rebuilders=[],
                    )

            self.assertTrue(plan.source_project.is_dir())
            self.assertFalse(plan.destination_project.exists())

    def test_apply_runs_real_default_rebuilders_within_frozen_contract(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "projects": ["github-obsidian-knowledge-brain"],
                        "project_keywords": {
                            "github-obsidian-knowledge-brain": ["knowledge-brain"]
                        },
                    },
                    sort_keys=False,
                ),
            )
            plan = build_migration_plan(vault, config_path=config)

            with redirect_stdout(io.StringIO()), patch(
                "compiler.has_uncommitted_changes", return_value=False
            ):
                result = apply_brand_migration(
                    plan,
                    "default-rebuilders",
                )

            self.assertEqual(result["status"], "applied")
            self.assertTrue(plan.destination_project.is_dir())
            rollback_brand_migration(result["manifest_path"])
            self.assertTrue(plan.source_project.is_dir())

    def test_apply_default_rebuilders_verify_existing_obsidian_parent(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            obsidian = (vault / ".obsidian").resolve()
            write_text(obsidian / "app.json", "{}\n")
            write_text(obsidian / "graph.json", "{}\n")
            write_text(
                obsidian / "workspace.json",
                '{"lastOpenFiles": []}\n',
            )
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "projects": ["github-obsidian-knowledge-brain"],
                    },
                    sort_keys=False,
                ),
            )
            plan = build_migration_plan(vault, config_path=config)
            before_inode = (
                os.stat(obsidian, follow_symlinks=False).st_dev,
                os.stat(obsidian, follow_symlinks=False).st_ino,
            )

            self.assertNotIn(obsidian, plan.mutation_contract.mutable_roots)
            self.assertNotIn(
                obsidian,
                {spec.path for spec in plan.mutation_contract.target_specs},
            )
            with redirect_stdout(io.StringIO()), patch(
                "compiler.has_uncommitted_changes", return_value=False
            ):
                result = apply_brand_migration(
                    plan,
                    "existing-obsidian-parent",
                )

            self.assertEqual(result["status"], "applied")
            after_inode = (
                os.stat(obsidian, follow_symlinks=False).st_dev,
                os.stat(obsidian, follow_symlinks=False).st_ino,
            )
            self.assertEqual(after_inode, before_inode)
            rollback_brand_migration(result["manifest_path"])
            self.assertTrue(plan.source_project.is_dir())

    def test_default_rebuilder_failure_after_internal_write_rolls_back(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            obsidian = (vault / ".obsidian").resolve()
            write_text(obsidian / "app.json", "{}\n")
            write_text(obsidian / "graph.json", "{}\n")
            workspace = obsidian / "workspace.json"
            write_text(workspace, '{"lastOpenFiles": []}\n')
            workspace_before = workspace.read_bytes()
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump({"vault_path": str(vault)}, sort_keys=False),
            )
            plan = build_migration_plan(vault, config_path=config)

            original_ensure = brand_migration._MigrationIO.ensure_directory

            def fail_after_workspace_write(active_io, path):
                if Path(path) == obsidian:
                    raise RuntimeError("forced mid-rebuilder failure")
                return original_ensure(active_io, path)

            with redirect_stdout(io.StringIO()), patch(
                "compiler.has_uncommitted_changes", return_value=False
            ), patch.object(
                brand_migration._MigrationIO,
                "ensure_directory",
                new=fail_after_workspace_write,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "^forced mid-rebuilder failure$",
                ):
                    apply_brand_migration(
                        plan,
                        "mid-rebuilder-rollback",
                    )

            self.assertTrue(plan.source_project.is_dir())
            self.assertFalse(plan.destination_project.exists())
            self.assertEqual(workspace.read_bytes(), workspace_before)

    def test_internal_after_checkpoint_failure_is_exactly_recoverable(self):
        import brand_migration

        for existing_parent in (True, False):
            with self.subTest(existing_parent=existing_parent), tempfile.TemporaryDirectory() as raw_tmp:
                vault = Path(raw_tmp).resolve()
                make_vault(vault)
                obsidian = vault / ".obsidian"
                workspace = obsidian / "workspace.json"
                workspace_before = None
                if existing_parent:
                    write_text(obsidian / "app.json", "{}\n")
                    write_text(obsidian / "graph.json", "{}\n")
                    write_text(workspace, '{"lastOpenFiles": []}\n')
                    workspace_before = workspace.read_bytes()

                plan = build_migration_plan(vault)
                original_publish = brand_migration._publish_checkpoint
                interrupted = []

                def fail_before_internal_after(
                    handle,
                    active_plan,
                    phase,
                    boundary,
                    *args,
                    **kwargs,
                ):
                    if (
                        not interrupted
                        and phase == "rebuild:memory-index"
                        and boundary == "after"
                    ):
                        interrupted.append(True)
                        raise brand_migration._CheckpointFailure(
                            "failure before internal after publication"
                        )
                    return original_publish(
                        handle,
                        active_plan,
                        phase,
                        boundary,
                        *args,
                        **kwargs,
                    )

                migration_id = f"checkpoint-gap-{existing_parent}"
                with redirect_stdout(io.StringIO()), patch(
                    "brand_migration._publish_checkpoint",
                    side_effect=fail_before_internal_after,
                ), patch(
                    "compiler.has_uncommitted_changes",
                    return_value=False,
                ):
                    with self.assertRaisesRegex(RuntimeError, "recovery required"):
                        apply_brand_migration(plan, migration_id)

                self.assertEqual(interrupted, [True])
                manifest = (
                    vault
                    / "04-Feedback/_rollback/brand-migration"
                    / migration_id
                    / "manifest.json"
                )
                records = read_checkpoint_records(manifest)
                pending = records[-1][2]
                self.assertEqual(pending["boundary"], "before")
                self.assertEqual(pending.get("delta"), {})
                self.assertEqual(
                    pending["mutation_intent"]["target"]["path"],
                    (
                        ".obsidian/workspace.json"
                        if existing_parent
                        else ".obsidian"
                    ),
                )
                if existing_parent:
                    original_binding = next(
                        binding
                        for binding in plan.input_bindings
                        if binding.path == workspace
                    )
                    self.assertNotEqual(
                        os.stat(workspace, follow_symlinks=False).st_ino,
                        original_binding.inode[1],
                    )
                result = rollback_brand_migration(manifest, force=True)
                self.assertEqual(result["status"], "rolled_back")
                self.assertTrue(plan.source_project.is_dir())
                self.assertFalse(plan.destination_project.exists())
                if existing_parent:
                    self.assertEqual(workspace.read_bytes(), workspace_before)
                else:
                    self.assertFalse(obsidian.exists())

    def test_internal_checkpoint_refuses_concurrent_target_replacement(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            vault = Path(raw_tmp).resolve()
            make_vault(vault)
            obsidian = vault / ".obsidian"
            write_text(obsidian / "app.json", "{}\n")
            write_text(obsidian / "graph.json", "{}\n")
            workspace = obsidian / "workspace.json"
            write_text(workspace, '{"lastOpenFiles": []}\n')
            external = b'{"externalWriter": true}\n'
            plan = build_migration_plan(vault)
            original_publish = brand_migration._publish_checkpoint
            replaced = []

            def replace_before_capture(
                handle,
                active_plan,
                phase,
                boundary,
                *args,
                **kwargs,
            ):
                if (
                    not replaced
                    and phase == "rebuild:memory-index"
                    and boundary == "after"
                ):
                    replacement = workspace.with_name("workspace.external")
                    replacement.write_bytes(external)
                    os.replace(replacement, workspace)
                    replaced.append(True)
                return original_publish(
                    handle,
                    active_plan,
                    phase,
                    boundary,
                    *args,
                    **kwargs,
                )

            with redirect_stdout(io.StringIO()), patch(
                "brand_migration._publish_checkpoint",
                side_effect=replace_before_capture,
            ), patch(
                "compiler.has_uncommitted_changes",
                return_value=False,
            ):
                with self.assertRaisesRegex(RuntimeError, "concurrent|changed"):
                    apply_brand_migration(plan, "checkpoint-capture-race")

            self.assertEqual(replaced, [True])
            self.assertEqual(workspace.read_bytes(), external)
            manifest = (
                vault
                / "04-Feedback/_rollback/brand-migration/checkpoint-capture-race"
                / "manifest.json"
            )
            with self.assertRaisesRegex(RuntimeError, "concurrent|changed|drift"):
                rollback_brand_migration(manifest, force=True)
            self.assertEqual(workspace.read_bytes(), external)

    def test_apply_before_checkpoint_rejects_nonempty_delta(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            vault = Path(raw_tmp).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            manifest = create_migration_backup(plan, "before-delta-rejected")
            handle = brand_migration._open_backup_handle(
                manifest,
                expected_plan=plan,
            )
            try:
                record = {
                    "status": "applying",
                    "phase": "rebuild:memory-index",
                    "boundary": "before",
                    "delta": {
                        "created_files": {
                            "upsert": [
                                {
                                    "kind": "vault",
                                    "path": "forged.md",
                                }
                            ],
                            "remove": [],
                        }
                    },
                }
                with self.assertRaisesRegex(
                    ValueError,
                    "before checkpoint delta is not empty",
                ):
                    brand_migration._validate_checkpoint_mutation_record(
                        handle,
                        record,
                    )
            finally:
                brand_migration._close_backup_handle(handle)

    def test_top_level_rewrite_after_checkpoint_failure_is_recoverable(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            vault = Path(raw_tmp).resolve()
            make_vault(vault)
            before = snapshot_public_tree(vault)
            plan = build_migration_plan(vault)
            original_publish = brand_migration._publish_checkpoint
            interrupted = []

            def fail_before_rewrite_after(
                handle,
                active_plan,
                phase,
                boundary,
                *args,
                **kwargs,
            ):
                if (
                    not interrupted
                    and phase.startswith("rewrite:")
                    and boundary == "after"
                ):
                    interrupted.append(True)
                    raise brand_migration._CheckpointFailure(
                        "failure before rewrite after publication"
                    )
                return original_publish(
                    handle,
                    active_plan,
                    phase,
                    boundary,
                    *args,
                    **kwargs,
                )

            with patch(
                "brand_migration._publish_checkpoint",
                side_effect=fail_before_rewrite_after,
            ):
                with self.assertRaisesRegex(RuntimeError, "recovery required"):
                    apply_brand_migration(
                        plan,
                        "top-level-rewrite-gap",
                        rebuilders=[],
                    )

            self.assertEqual(interrupted, [True])
            manifest = (
                vault
                / "04-Feedback/_rollback/brand-migration/top-level-rewrite-gap"
                / "manifest.json"
            )
            result = rollback_brand_migration(manifest, force=True)
            self.assertEqual(result["status"], "rolled_back")
            self.assertEqual(snapshot_public_tree(vault), before)

    def test_cleanup_checkpoint_boundaries_are_exactly_recoverable(self):
        import brand_migration

        for after_publication in (False, True):
            with self.subTest(
                after_publication=after_publication
            ), tempfile.TemporaryDirectory() as raw_tmp:
                vault = Path(raw_tmp).resolve()
                make_vault(vault)
                before = snapshot_public_tree(vault)
                plan = build_migration_plan(vault)
                original_publish = brand_migration._publish_checkpoint
                interrupted = []

                def interrupt_cleanup(
                    handle,
                    active_plan,
                    phase,
                    boundary,
                    *args,
                    **kwargs,
                ):
                    matches = (
                        not interrupted
                        and phase.startswith("cleanup:")
                        and boundary == "after"
                    )
                    if matches and not after_publication:
                        interrupted.append((phase, "before-publication"))
                        raise brand_migration._CheckpointFailure(
                            "failure before cleanup publication"
                        )
                    result = original_publish(
                        handle,
                        active_plan,
                        phase,
                        boundary,
                        *args,
                        **kwargs,
                    )
                    if matches:
                        interrupted.append((phase, "after-publication"))
                        raise brand_migration._CheckpointFailure(
                            "failure after cleanup publication"
                        )
                    return result

                migration_id = f"cleanup-gap-{after_publication}"
                with patch(
                    "brand_migration._publish_checkpoint",
                    side_effect=interrupt_cleanup,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "recovery required",
                    ):
                        apply_brand_migration(
                            plan,
                            migration_id,
                            rebuilders=[],
                        )

                self.assertEqual(len(interrupted), 1)
                manifest = (
                    vault
                    / "04-Feedback/_rollback/brand-migration"
                    / migration_id
                    / "manifest.json"
                )
                result = rollback_brand_migration(manifest, force=True)
                self.assertEqual(result["status"], "rolled_back")
                self.assertEqual(snapshot_public_tree(vault), before)

    def test_top_level_rewrite_refuses_concurrent_target_replacement(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            vault = Path(raw_tmp).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            original_publish = brand_migration._publish_checkpoint
            external = b"external concurrent rewrite\n"
            replaced = []

            def replace_before_rewrite_capture(
                handle,
                active_plan,
                phase,
                boundary,
                *args,
                **kwargs,
            ):
                if (
                    not replaced
                    and phase.startswith("rewrite:")
                    and boundary == "after"
                ):
                    original = Path(phase.split(":", 1)[1])
                    target = brand_migration._post_migration_path(
                        original,
                        active_plan.source_project,
                        active_plan.destination_project,
                    )
                    replacement = target.with_name(target.name + ".external")
                    replacement.write_bytes(external)
                    os.replace(replacement, target)
                    replaced.append(target)
                return original_publish(
                    handle,
                    active_plan,
                    phase,
                    boundary,
                    *args,
                    **kwargs,
                )

            with patch(
                "brand_migration._publish_checkpoint",
                side_effect=replace_before_rewrite_capture,
            ):
                with self.assertRaisesRegex(RuntimeError, "concurrent|changed"):
                    apply_brand_migration(
                        plan,
                        "top-level-rewrite-race",
                        rebuilders=[],
                    )

            self.assertEqual(len(replaced), 1)
            self.assertEqual(replaced[0].read_bytes(), external)
            manifest = (
                vault
                / "04-Feedback/_rollback/brand-migration/top-level-rewrite-race"
                / "manifest.json"
            )
            with self.assertRaisesRegex(RuntimeError, "concurrent|changed|drift"):
                rollback_brand_migration(manifest, force=True)
            self.assertEqual(replaced[0].read_bytes(), external)

    def test_config_rewrite_uses_paired_mutation_intent(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            config = tmp / "config.yaml"
            original = (
                f"vault_path: {vault}\n"
                "projects:\n"
                "  - github-obsidian-knowledge-brain\n"
            ).encode("utf-8")
            config.write_bytes(original)
            plan = build_migration_plan(vault, config_path=config)

            result = apply_brand_migration(
                plan,
                "config-intent-pair",
                rebuilders=[],
            )
            records = [
                record
                for _sequence, _digest, record, _directory
                in read_checkpoint_records(Path(result["manifest_path"]))
                if record["phase"] == "rewrite:config"
            ]

            self.assertEqual(
                [record["boundary"] for record in records],
                ["before", "after"],
            )
            self.assertEqual(records[0]["delta"], {})
            self.assertEqual(
                records[0]["mutation_intent"],
                records[1]["mutation_intent"],
            )
            self.assertEqual(
                records[0]["mutation_intent"]["target"]["path"],
                str(config),
            )
            rollback_brand_migration(result["manifest_path"])
            self.assertEqual(config.read_bytes(), original)

    def test_source_rename_after_checkpoint_failure_is_recoverable(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            vault = Path(raw_tmp).resolve()
            make_vault(vault)
            before = snapshot_public_tree(vault)
            plan = build_migration_plan(vault)
            original_publish = brand_migration._publish_checkpoint
            interrupted = []

            def fail_before_source_after(
                handle,
                active_plan,
                phase,
                boundary,
                *args,
                **kwargs,
            ):
                if (
                    not interrupted
                    and phase == "source-rename"
                    and boundary == "after"
                ):
                    interrupted.append(True)
                    raise brand_migration._CheckpointFailure(
                        "failure before source after publication"
                    )
                return original_publish(
                    handle,
                    active_plan,
                    phase,
                    boundary,
                    *args,
                    **kwargs,
                )

            with patch(
                "brand_migration._publish_checkpoint",
                side_effect=fail_before_source_after,
            ):
                with self.assertRaisesRegex(RuntimeError, "recovery required"):
                    apply_brand_migration(
                        plan,
                        "source-rename-gap",
                        rebuilders=[],
                    )

            self.assertEqual(interrupted, [True])
            manifest = (
                vault
                / "04-Feedback/_rollback/brand-migration/source-rename-gap"
                / "manifest.json"
            )
            result = rollback_brand_migration(manifest, force=True)
            self.assertEqual(result["status"], "rolled_back")
            self.assertEqual(snapshot_public_tree(vault), before)

    def test_source_rename_uses_paired_mutation_intent(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            vault = Path(raw_tmp).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)

            result = apply_brand_migration(
                plan,
                "source-rename-intent-pair",
                rebuilders=[],
            )
            records = [
                record
                for _sequence, _digest, record, _directory
                in read_checkpoint_records(Path(result["manifest_path"]))
                if record["phase"] == "source-rename"
            ]

            self.assertEqual(
                [record["boundary"] for record in records],
                ["before", "after"],
            )
            self.assertEqual(
                records[0]["mutation_intent"],
                records[1]["mutation_intent"],
            )
            intent = records[0]["mutation_intent"]
            self.assertEqual(intent["operation"], "rename-directory")
            self.assertEqual(
                checkpoint_record_path(vault, intent["target"]),
                plan.source_project,
            )
            self.assertEqual(
                checkpoint_record_path(vault, intent["staging"]),
                plan.destination_project,
            )

    def test_pending_source_rename_refuses_concurrent_destination_replacement(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            vault = Path(raw_tmp).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            original_publish = brand_migration._publish_checkpoint
            interrupted = []

            def fail_before_source_after(
                handle,
                active_plan,
                phase,
                boundary,
                *args,
                **kwargs,
            ):
                if (
                    not interrupted
                    and phase == "source-rename"
                    and boundary == "after"
                ):
                    interrupted.append(True)
                    raise brand_migration._CheckpointFailure(
                        "failure before source after publication"
                    )
                return original_publish(
                    handle,
                    active_plan,
                    phase,
                    boundary,
                    *args,
                    **kwargs,
                )

            with patch(
                "brand_migration._publish_checkpoint",
                side_effect=fail_before_source_after,
            ):
                with self.assertRaisesRegex(RuntimeError, "recovery required"):
                    apply_brand_migration(
                        plan,
                        "source-rename-concurrent-replacement",
                        rebuilders=[],
                    )

            manifest = (
                vault
                / "04-Feedback/_rollback/brand-migration"
                / "source-rename-concurrent-replacement"
                / "manifest.json"
            )
            pending = read_checkpoint_records(manifest)[-1][2]
            self.assertEqual(
                pending["mutation_intent"]["operation"],
                "rename-directory",
            )

            original_hold = plan.destination_project.with_name(
                "agent-memory-beacon-original-hold"
            )
            plan.destination_project.rename(original_hold)
            plan.destination_project.mkdir()
            write_text(
                plan.destination_project / "external.txt",
                "external concurrent directory\n",
            )

            with self.assertRaisesRegex(
                RuntimeError,
                "concurrent mutation at pending project rename",
            ):
                rollback_brand_migration(manifest, force=True)

            self.assertFalse(plan.source_project.exists())
            self.assertEqual(
                (plan.destination_project / "external.txt").read_text(
                    encoding="utf-8"
                ),
                "external concurrent directory\n",
            )
            self.assertTrue(original_hold.is_dir())

    def test_pending_source_rename_restores_interposed_destination(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            vault = Path(raw_tmp).resolve()
            make_vault(vault)
            plan = build_migration_plan(vault)
            original_publish = brand_migration._publish_checkpoint
            interrupted = []

            def fail_before_source_after(
                handle,
                active_plan,
                phase,
                boundary,
                *args,
                **kwargs,
            ):
                if (
                    not interrupted
                    and phase == "source-rename"
                    and boundary == "after"
                ):
                    interrupted.append(True)
                    raise brand_migration._CheckpointFailure(
                        "failure before source after publication"
                    )
                return original_publish(
                    handle,
                    active_plan,
                    phase,
                    boundary,
                    *args,
                    **kwargs,
                )

            with patch(
                "brand_migration._publish_checkpoint",
                side_effect=fail_before_source_after,
            ):
                with self.assertRaisesRegex(RuntimeError, "recovery required"):
                    apply_brand_migration(
                        plan,
                        "source-rename-interposed-destination",
                        rebuilders=[],
                    )

            manifest = (
                vault
                / "04-Feedback/_rollback/brand-migration"
                / "source-rename-interposed-destination"
                / "manifest.json"
            )
            original_hold = plan.destination_project.with_name(
                "agent-memory-beacon-race-original-hold"
            )
            original_rename = brand_migration._rename_exclusive
            injected = []

            def interpose_destination(
                source_fd,
                source_name,
                destination_fd,
                destination_name,
            ):
                if (
                    not injected
                    and source_name == plan.destination_project.name
                    and destination_name == plan.source_project.name
                ):
                    injected.append(True)
                    os.rename(
                        source_name,
                        original_hold.name,
                        src_dir_fd=source_fd,
                        dst_dir_fd=source_fd,
                    )
                    os.mkdir(source_name, dir_fd=source_fd)
                    external_fd = os.open(
                        source_name,
                        os.O_RDONLY | os.O_DIRECTORY,
                        dir_fd=source_fd,
                    )
                    try:
                        file_fd = os.open(
                            "external.txt",
                            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                            0o600,
                            dir_fd=external_fd,
                        )
                        try:
                            os.write(file_fd, b"external concurrent directory\n")
                        finally:
                            os.close(file_fd)
                    finally:
                        os.close(external_fd)
                return original_rename(
                    source_fd,
                    source_name,
                    destination_fd,
                    destination_name,
                )

            with patch(
                "brand_migration._rename_exclusive",
                side_effect=interpose_destination,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "concurrent mutation while restoring pending project rename",
                ):
                    rollback_brand_migration(manifest, force=True)

            self.assertEqual(injected, [True])
            self.assertFalse(plan.source_project.exists())
            self.assertEqual(
                (plan.destination_project / "external.txt").read_text(
                    encoding="utf-8"
                ),
                "external concurrent directory\n",
            )
            self.assertTrue(original_hold.is_dir())

    def test_fixed_modules_reject_descriptor_pinned_write_races(self):
        import brand_migration

        cases = (
            "harvester-parent-symlink",
            "reporter-parent-swap",
            "compiler-temp-symlink",
            "compiler-temp-replacement",
        )
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw_tmp:
                tmp = Path(raw_tmp).resolve()
                vault = tmp / "vault"
                make_vault(vault)
                (vault / "00-Rules").mkdir()
                context = tmp / "context" / "AGENTS.md"
                write_text(
                    context,
                    "preamble\n<!-- COMPILED:RULES_START -->\nold\n"
                    "<!-- COMPILED:RULES_END -->\n"
                    "<!-- COMPILED:PROJECTS_START -->\nold\n"
                    "<!-- COMPILED:PROJECTS_END -->\n",
                )
                config = tmp / "config.yaml"
                write_text(
                    config,
                    yaml.safe_dump(
                        {
                            "vault_path": str(vault),
                            "context_targets": [str(context)],
                        },
                        sort_keys=False,
                    ),
                )
                plan = build_migration_plan(vault, config_path=config)
                outside = tmp / "outside"
                outside.mkdir()
                outside_file = outside / "outside.txt"
                write_text(outside_file, "outside before\n")
                outside_before = outside_file.read_bytes()
                interposed = []
                original_write = brand_migration._MigrationIO.atomic_write

                def race_write(active_io, path, content, encoding="utf-8"):
                    target = Path(path)
                    if not interposed:
                        if case == "harvester-parent-symlink" and target.name == "Agent Memory Index.md":
                            held = target.parent.with_name(target.parent.name + "-held")
                            target.parent.rename(held)
                            target.parent.symlink_to(outside, target_is_directory=True)
                            interposed.append(target.parent)
                        elif case == "reporter-parent-swap" and target.name == "topic-index.md":
                            held = target.parent.with_name(target.parent.name + "-held")
                            target.parent.rename(held)
                            target.parent.mkdir()
                            write_text(target.parent / "replacement.txt", "replacement\n")
                            interposed.append(target.parent / "replacement.txt")
                        elif case == "compiler-temp-symlink" and target == context:
                            temp_target = Path(str(context) + ".tmp")
                            temp_target.symlink_to(outside_file)
                            interposed.append(temp_target)
                        elif case == "compiler-temp-replacement" and target == context:
                            temp_target = Path(str(context) + ".tmp")
                            write_text(temp_target, "replacement temp\n")
                            interposed.append(temp_target)
                    return original_write(active_io, path, content, encoding=encoding)

                with patch.object(
                    brand_migration._MigrationIO,
                    "atomic_write",
                    new=race_write,
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "migration|recovery|required|parent|temporary",
                    ):
                        apply_brand_migration(plan, f"mutation-io-{case}")

                self.assertTrue(interposed)
                self.assertEqual(outside_file.read_bytes(), outside_before)
                self.assertTrue(interposed[0].exists() or interposed[0].is_symlink())
                manifest = (
                    vault
                    / "04-Feedback/_rollback/brand-migration"
                    / f"mutation-io-{case}"
                    / "manifest.json"
                )
                self.assertTrue(manifest.is_file())

    def test_migration_io_rejects_absent_directory_appearance_during_create(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            (vault / "00-Rules").mkdir()
            plan = build_migration_plan(vault)
            inbox = vault / "00-Inbox"
            original_open_parent = brand_migration._open_handle_parent
            interposed = []

            def appear_during_create(handle, path, create_directories=()):
                path = Path(path)
                if (
                    not interposed
                    and path in {inbox, inbox / ".migration-directory-probe"}
                ):
                    inbox.mkdir()
                    write_text(inbox / "replacement.txt", "replacement\n")
                    interposed.append(inbox / "replacement.txt")
                return original_open_parent(handle, path, create_directories)

            with patch(
                "brand_migration._open_handle_parent",
                side_effect=appear_during_create,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "appeared|creation|recovery|migration",
                ):
                    apply_brand_migration(plan, "absent-directory-race")

            self.assertEqual(interposed[0].read_text(encoding="utf-8"), "replacement\n")

    def test_split_wikilink_normalizes_whitespace_escaped_alias_and_anchor(self):
        self.assertEqual(
            split_wikilink(" 01-Projects/demo/Memory/note#section \\| Label "),
            ("01-Projects/demo/Memory/note#section", "Label"),
        )
        self.assertEqual(split_wikilink(" Other "), ("Other", None))

    def test_migration_plan_is_frozen(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)

            with self.assertRaises(FrozenInstanceError):
                plan.old_slug = "changed"

    def test_plan_summary_is_read_only(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            before = snapshot_tree(vault)

            summary = plan_summary(plan)

            self.assertEqual(summary["status"], "ready")
            self.assertEqual(summary["backup_files"], len(plan.backup_paths))
            self.assertEqual(snapshot_tree(vault), before)

    def test_preflight_is_read_only_and_inventories_structural_refs(self):
        with tempfile.TemporaryDirectory() as vault:
            source = make_vault(vault)
            before = snapshot_tree(vault)

            plan = build_migration_plan(vault)

            self.assertEqual(plan.source_project, source)
            self.assertEqual(plan.old_slug, "github-obsidian-knowledge-brain")
            self.assertEqual(plan.new_slug, "agent-memory-beacon")
            self.assertGreaterEqual(len(plan.markdown_paths), 2)
            self.assertEqual(snapshot_tree(vault), before)

    def test_preflight_ignores_prose_and_code_only_legacy_mentions(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            prose = Path(vault) / "00-Rules" / "history.md"
            write_text(
                prose,
                "Historical prose: github-obsidian-knowledge-brain\n"
                "```markdown\n"
                "[[01-Projects/github-obsidian-knowledge-brain/Memory/decisions]]\n"
                "```\n",
            )

            plan = build_migration_plan(vault)

            self.assertNotIn(prose.resolve(), plan.markdown_paths)

    def test_structural_wikilinks_handle_root_whitespace_anchors_and_alias_scope(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            old = "github-obsidian-knowledge-brain"
            root_link = Path(vault) / "00-Rules" / "root.md"
            anchored = Path(vault) / "00-Rules" / "anchored.md"
            unrelated = Path(vault) / "00-Rules" / "unrelated.md"
            fenced = Path(vault) / "00-Rules" / "fenced.md"
            write_text(root_link, f"[[ 01-Projects/{old} | {old} ]]\n")
            write_text(
                anchored,
                f"[[01-Projects/{old}/Memory/decisions#section\\| Decisions ]]\n",
            )
            write_text(unrelated, f"[[Other|{old}]]\n")
            write_text(
                fenced,
                f"```markdown\n[[01-Projects/{old}/Memory/decisions]]\n```\n",
            )

            plan = build_migration_plan(vault)

            self.assertIn(root_link.resolve(), plan.markdown_paths)
            self.assertIn(anchored.resolve(), plan.markdown_paths)
            self.assertNotIn(unrelated.resolve(), plan.markdown_paths)
            self.assertNotIn(fenced.resolve(), plan.markdown_paths)

    def test_frontmatter_requires_line_delimited_closing_marker(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            malformed = Path(vault) / "00-Rules" / "malformed.md"
            write_text(
                malformed,
                "---\nproject: github-obsidian-knowledge-brain\n"
                "--- trailing text is not a delimiter\n# Body\n",
            )

            plan = build_migration_plan(vault)

            self.assertNotIn(malformed.resolve(), plan.markdown_paths)

    def test_cyclic_yaml_anchor_terminates_without_false_reference(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            cyclic = Path(vault) / "00-Rules" / "cyclic.md"
            write_text(
                cyclic,
                "---\nloop: &loop\n  child: *loop\n"
                "summary: github-obsidian-knowledge-brain\n---\n# Cyclic\n",
            )

            plan = build_migration_plan(vault)

            self.assertNotIn(cyclic.resolve(), plan.markdown_paths)

    def test_shared_yaml_anchor_under_project_key_remains_structural(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            shared = Path(vault) / "00-Rules" / "shared-anchor.md"
            write_text(
                shared,
                "---\nshared: &projects\n"
                "  - github-obsidian-knowledge-brain\n"
                "summary: *projects\nproject: *projects\n---\n# Shared\n",
            )

            plan = build_migration_plan(vault)

            self.assertIn(shared.resolve(), plan.markdown_paths)

    def test_non_utf8_markdown_fails_preflight_with_path(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            invalid = Path(vault) / "00-Rules" / "invalid.md"
            invalid.parent.mkdir()
            invalid.write_bytes(b"\xff\xfe")

            with self.assertRaisesRegex(ValueError, rf"non-UTF-8 Markdown: {invalid.resolve()}"):
                build_migration_plan(vault)

    def test_non_utf8_config_fails_preflight_with_path(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            config = tmp / "config.yaml"
            config.write_bytes(b"\xff\xfe")

            with self.assertRaisesRegex(ValueError, rf"non-UTF-8 config: {config}"):
                build_migration_plan(vault, config_path=config)

    def test_plan_summary_fails_closed_on_drift(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            plan.source_project.joinpath("late.bin").write_bytes(b"late")

            with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                plan_summary(plan)

    def test_broken_link_baseline_normalizes_renamed_source_and_target(self):
        with tempfile.TemporaryDirectory() as vault:
            source = make_vault(vault)
            old = "github-obsidian-knowledge-brain"
            new = "agent-memory-beacon"
            broken = source / "Memory" / "broken.md"
            write_text(
                broken,
                f"[[01-Projects/{old}/Memory/missing|Missing]]\n",
            )

            plan = build_migration_plan(vault)

            self.assertIn(
                (
                    f"01-Projects/{new}/Memory/broken.md",
                    f"01-Projects/{new}/Memory/missing",
                ),
                plan.broken_links_before,
            )

    def test_plan_and_manifest_store_exact_memory_identity_keys(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            self.assertEqual(
                len(plan.memory_identity_keys_before),
                plan.memory_identity_count_before,
            )
            original = plan.memory_identity_keys_before

            manifest_path = create_migration_backup(plan, "identity-keys")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["memory_identity_keys_before"], list(original))
            decisions = plan.source_project / "Memory" / "decisions.md"
            decisions.write_text(
                decisions.read_text(encoding="utf-8").replace(
                    "decision-1", "different-id"
                ),
                encoding="utf-8",
            )
            changed_keys = memory_identity_keys(plan.source_project)
            self.assertEqual(len(changed_keys), len(original))
            self.assertNotEqual(changed_keys, original)
            with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                plan_summary(plan)

    def test_forged_plan_rejects_non_branding_slugs(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)

            for field, value in (
                ("old_slug", "other-legacy-project"),
                ("new_slug", "other-destination-project"),
            ):
                with self.subTest(field=field):
                    forged = replace(plan, **{field: value})
                    with self.assertRaisesRegex(ValueError, "branding constant"):
                        plan_summary(forged)

    def test_forged_plan_rejects_changed_broken_link_baseline(self):
        with tempfile.TemporaryDirectory() as vault:
            source = make_vault(vault)
            write_text(source / "Memory" / "broken.md", "[[missing-note]]\n")
            plan = build_migration_plan(vault)
            self.assertTrue(plan.broken_links_before)
            forged = replace(plan, broken_links_before=())

            with self.assertRaisesRegex(ValueError, "broken-link baseline"):
                create_migration_backup(forged, "forged-broken-links")

            assert_failed_backup_absent(vault, "forged-broken-links")

    def test_forged_plan_rejects_changed_memory_identity_baseline(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            forged_keys = tuple(f"forged:{index}" for index, _ in enumerate(
                plan.memory_identity_keys_before
            ))
            forged = replace(
                plan,
                memory_identity_keys_before=forged_keys,
                memory_identity_count_before=len(forged_keys),
            )

            with self.assertRaisesRegex(ValueError, "memory identity baseline"):
                create_migration_backup(forged, "forged-identities")

            assert_failed_backup_absent(vault, "forged-identities")

    def test_forged_plan_rejects_inconsistent_memory_identity_count(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            forged = replace(
                plan,
                memory_identity_count_before=plan.memory_identity_count_before + 1,
            )

            with self.assertRaisesRegex(ValueError, "memory identity count"):
                plan_summary(forged)

    def test_preflight_rejects_existing_destination(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            os.makedirs(os.path.join(vault, "01-Projects", "agent-memory-beacon"))

            with self.assertRaisesRegex(ValueError, "destination already exists"):
                build_migration_plan(vault)

    def test_preflight_rejects_dangling_destination_symlink(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            destination = Path(vault) / "01-Projects" / "agent-memory-beacon"
            destination.symlink_to(destination.parent / "missing")

            with self.assertRaisesRegex(ValueError, "destination already exists"):
                build_migration_plan(vault)

    def test_preflight_rejects_symlink_inside_source_project(self):
        with tempfile.TemporaryDirectory() as vault, tempfile.TemporaryDirectory() as outside:
            source = make_vault(vault)
            os.symlink(outside, source / "external-link")

            with self.assertRaisesRegex(ValueError, "symlink"):
                build_migration_plan(vault)

    def test_preflight_rejects_symlink_source_project(self):
        with tempfile.TemporaryDirectory() as vault:
            source = make_vault(vault)
            real_source = source.with_name("legacy-real")
            source.rename(real_source)
            os.symlink(real_source.name, source, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "source project.*symlink"):
                build_migration_plan(vault)

    def test_preflight_inventories_current_and_legacy_context_targets(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            current = Path(tmp) / "AGENTS.md"
            legacy = Path(tmp) / "CLAUDE.md"
            current.write_text("current context\n", encoding="utf-8")
            legacy.write_text("legacy context\n", encoding="utf-8")
            config = Path(tmp) / "config.yaml"
            write_text(
                config,
                f"vault_path: {vault}\n"
                "context_targets:\n"
                f"  - {current}\n"
                f"claude_md_path: {legacy}\n",
            )
            before = {
                path.resolve(): path.read_bytes()
                for path in (config, current, legacy)
            }

            plan = build_migration_plan(vault, config_path=config)

            self.assertIn(current.resolve(), plan.backup_paths)
            self.assertIn(legacy.resolve(), plan.backup_paths)
            self.assertEqual(
                {path: path.read_bytes() for path in before},
                before,
            )

    def test_plan_covers_real_default_rebuilder_mutation_inventory(self):
        from compiler import run as compile_context
        from reporter import rebuild_maps
        from session_harvester import rebuild_memory_index

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            other = vault / "01-Projects" / "other-project" / "Memory"
            write_text(
                other / "decisions.md",
                "---\nproject: other-project\ndecisions: []\n---\n\n# Decisions\n",
            )
            write_text(
                other / "pitfalls.md",
                "---\nproject: other-project\npitfalls: []\n---\n\n# Pitfalls\n",
            )
            write_text(
                other / "sessions" / "2026-07-12-other.md",
                "---\nsession_id: other\ndate: 2026-07-12\ntags: []\n---\n\n# Other\n",
            )
            write_text(
                vault / "00-Rules" / "active-rule.md",
                "---\nrule_id: active-rule\ntitle: Active\ncategory: test\n"
                "status: active\napplies_to: []\n---\n\nRule body\n",
            )
            write_text(vault / "00-Inbox" / "Agent Memory Index.md", "old index\n")
            write_text(
                vault / "04-Feedback" / "_memory-candidates" / "candidate.md",
                "---\ngenerated_by: memory_judge.py\nproject: other-project\n---\n"
                "\n# Candidate\n\n## Evidence\n- sample\n",
            )
            write_text(vault / "03-Maps" / "topic-index.md", "old topic\n")
            write_text(vault / "03-Maps" / "timeline.md", "old timeline\n")
            write_text(vault / "05-Agent-Memory" / "keyword-index.md", "old keyword\n")
            users_empty = vault / "Users" / "nested" / "empty.md"
            users_keep = vault / "Users" / "nested" / "keep.bin"
            write_text(users_empty, "")
            users_keep.write_bytes(b"keep")
            obsidian = vault / ".obsidian"
            write_text(obsidian / "app.json", "{}\n")
            write_text(obsidian / "graph.json", "{}\n")
            write_text(
                obsidian / "workspace.json",
                '{"lastOpenFiles": ["Users/bad.md"]}\n',
            )
            custom_index = vault / "06-Custom" / "memory-index.md"
            write_text(custom_index, "old custom index\n")
            custom_agent = tmp / "custom-agent-memory"
            write_text(
                custom_agent / ".agent-memory-beacon-root",
                "owned by agent-memory-beacon\n",
            )
            write_text(custom_agent / "existing.md", "old agent memory\n")
            context = tmp / "AGENTS.md"
            write_text(
                context,
                "preamble\n<!-- COMPILED:RULES_START -->\nold\n"
                "<!-- COMPILED:RULES_END -->\n"
                "<!-- COMPILED:PROJECTS_START -->\nold\n"
                "<!-- COMPILED:PROJECTS_END -->\n",
            )
            absent_legacy = tmp / "CLAUDE.md"
            profile = tmp / "profile"
            shared = profile / "AGENTS.shared.md"
            write_text(shared, "shared profile\n")
            config = tmp / "config.yaml"
            config_payload = {
                "vault_path": str(vault),
                "memory_index_path": str(custom_index),
                "agent_memory_path": str(custom_agent),
                "context_targets": [str(context)],
                "claude_md_path": str(absent_legacy),
                "codex_profile_path": str(profile),
            }
            write_text(config, yaml.safe_dump(config_payload, sort_keys=False))

            plan = build_migration_plan(vault, config_path=config)
            before = snapshot_tree(tmp)
            with redirect_stdout(io.StringIO()), patch(
                "compiler.has_uncommitted_changes", return_value=False
            ):
                rebuild_memory_index(dict(config_payload))
                rebuild_maps(str(vault))
                compile_context(dict(config_payload))
            after = snapshot_tree(tmp)

            changed_existing = {
                (tmp / relative).resolve()
                for relative, digest in before.items()
                if relative not in after or after[relative] != digest
            }
            self.assertTrue(changed_existing)
            self.assertTrue(changed_existing.issubset(set(plan.backup_paths)))
            expected_existing = {
                (other / "decisions.md").resolve(),
                (other / "pitfalls.md").resolve(),
                (other / "sessions" / "2026-07-12-other.md").resolve(),
                users_empty.resolve(),
                users_keep.resolve(),
                (obsidian / "app.json").resolve(),
                (obsidian / "graph.json").resolve(),
                (obsidian / "workspace.json").resolve(),
                custom_index.resolve(),
                (custom_agent / "existing.md").resolve(),
                context.resolve(),
                shared.resolve(),
            }
            self.assertTrue(expected_existing.issubset(set(plan.backup_paths)))
            self.assertIn(absent_legacy, plan.absent_paths_before)
            self.assertIn(custom_agent, plan.mutable_roots)
            self.assertIn((vault / "Users" / "nested").resolve(), plan.mutable_directories_before)

            created = {
                (tmp / relative).resolve()
                for relative in set(after) - set(before)
            }
            frozen_targets = set(plan.configured_targets)
            for path in created:
                self.assertTrue(
                    path in frozen_targets
                    or any(path.is_relative_to(root) for root in plan.mutable_roots),
                    path,
                )

    def test_preflight_freezes_absent_custom_targets_and_roots(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            absent_context = tmp / "missing" / "AGENTS.md"
            absent_agent_root = tmp / "missing-agent-memory"
            absent_index = vault / "06-Custom" / "missing-index.md"
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "context_targets": [str(absent_context)],
                        "agent_memory_path": str(absent_agent_root),
                        "memory_index_path": str(absent_index),
                    },
                    sort_keys=False,
                ),
            )

            plan = build_migration_plan(vault, config_path=config)

            self.assertIn(absent_context, plan.configured_targets)
            self.assertIn(absent_agent_root, plan.mutable_roots)
            self.assertIn(absent_context, plan.absent_paths_before)
            self.assertIn(absent_agent_root, plan.absent_paths_before)
            self.assertIn(absent_index, plan.absent_paths_before)

    def test_named_mutation_contract_covers_temp_siblings_and_missing_parents(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            context = tmp / "AGENTS.md"
            write_text(context, "context\n")
            context_tmp = Path(str(context) + ".tmp")
            context_restore = Path(str(context) + ".restore")
            write_text(context_tmp, "existing context temp\n")
            write_text(context_restore, "existing context restore\n")
            profile = tmp / "profile"
            shared = profile / "AGENTS.shared.md"
            shared_tmp = Path(str(shared) + ".tmp")
            shared_restore = Path(str(shared) + ".restore")
            write_text(shared, "shared\n")
            write_text(shared_tmp, "existing profile temp\n")
            write_text(shared_restore, "existing profile restore\n")
            custom_index = vault / "missing-parent" / "nested" / "index.md"
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "context_targets": [str(context)],
                        "codex_profile_path": str(profile),
                        "memory_index_path": str(custom_index),
                    },
                    sort_keys=False,
                ),
            )
            config_tmp = Path(str(config) + ".tmp")
            config_restore = Path(str(config) + ".restore")
            write_text(config_tmp, "existing config temp\n")
            write_text(config_restore, "existing config restore\n")

            plan = build_migration_plan(vault, config_path=config)
            contract = plan.mutation_contract

            self.assertTrue(getattr(contract, "target_specs"))
            roles = {(spec.role, spec.key) for spec in contract.target_specs}
            self.assertEqual(len(roles), len(contract.target_specs))
            self.assertIn(context_tmp, contract.mutable_files)
            self.assertIn(context_restore, contract.mutable_files)
            self.assertIn(config_tmp, contract.mutable_files)
            self.assertIn(config_restore, contract.mutable_files)
            self.assertIn(shared_tmp, contract.mutable_files)
            self.assertIn(shared_restore, contract.mutable_files)
            self.assertIn(
                (vault / ".obsidian").resolve(),
                contract.absent_directories,
            )
            self.assertIn(
                (vault / "missing-parent").resolve(),
                contract.absent_directories,
            )
            self.assertIn(
                (vault / "missing-parent" / "nested").resolve(),
                contract.absent_directories,
            )
            temp_specs = [
                spec
                for spec in contract.target_specs
                if spec.entry_kind == "temp_sibling"
            ]
            self.assertTrue(temp_specs)
            self.assertTrue(
                all(spec.mutation_kind == "temporary" for spec in temp_specs)
            )
            manifest_path = create_migration_backup(plan, "temp-contract")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            frozen_sources = {
                plan.vault / item["path"]
                if item["kind"] == "vault"
                else Path(item["path"])
                for item in manifest["files"]
            }
            self.assertTrue(
                {
                    context_tmp,
                    context_restore,
                    config_tmp,
                    config_restore,
                    shared_tmp,
                    shared_restore,
                }.issubset(frozen_sources)
            )

    def test_default_codex_profile_excludes_assets_but_registers_shared_context(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            profile = vault / "05-Agent-Memory" / "codex-profile"
            shared = profile / "AGENTS.shared.md"
            shared_tmp = Path(str(shared) + ".tmp")
            shared_restore = Path(str(shared) + ".restore")
            asset = profile / "skills" / "example" / "SKILL.md"
            asset_dir = asset.parent
            write_text(shared, "shared context\n")
            write_text(shared_tmp, "shared temp\n")
            write_text(shared_restore, "shared restore\n")
            write_text(asset, "copied skill asset\n")

            plan = build_migration_plan(vault)
            contract = plan.mutation_contract
            cfg = brand_migration.load_migration_config(plan)
            target_paths = {spec.path for spec in contract.target_specs}

            self.assertEqual(contract.excluded_mutable_subtrees, (profile,))
            self.assertEqual(cfg["codex_profile_path"], str(profile))
            self.assertTrue(
                {shared, shared_tmp, shared_restore}.issubset(target_paths)
            )
            self.assertTrue(
                {shared, shared_tmp, shared_restore}.issubset(
                    set(contract.mutable_files)
                )
            )
            self.assertTrue(
                {shared, shared_tmp, shared_restore}.issubset(
                    set(plan.backup_paths)
                )
            )
            self.assertNotIn(asset, target_paths)
            self.assertNotIn(asset, contract.mutable_files)
            self.assertNotIn(asset_dir, contract.mutable_directories)
            self.assertNotIn(asset, {binding.path for binding in plan.input_bindings})
            self.assertNotIn(asset, plan.backup_paths)

    def test_profile_shared_markdown_is_rewritten_inside_vault_while_assets_are_untouched(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            profile = vault / "05-Agent-Memory" / "codex-profile"
            shared = profile / "AGENTS.shared.md"
            asset = profile / "skills" / "example" / "SKILL.md"
            old = "github-obsidian-knowledge-brain"
            shared_before = (
                f"---\nproject: {old}\n---\n"
                f"[[01-Projects/{old}/Memory/decisions|Decision]]\n"
            )
            asset_before = (
                f"[[01-Projects/{old}/Memory/decisions|Copied asset]]\n"
            )
            write_text(shared, shared_before)
            write_text(asset, asset_before)

            plan = build_migration_plan(vault)

            self.assertIn(shared, plan.markdown_paths)
            self.assertIn(shared, {path for path, _digest in plan.observed_hashes})
            self.assertNotIn(asset, plan.markdown_paths)
            result = apply_brand_migration(
                plan,
                "profile-shared-markdown-internal",
                rebuilders=[],
            )

            self.assertTrue(result["valid"])
            self.assertNotIn(old, shared.read_text(encoding="utf-8"))
            self.assertEqual(asset.read_text(encoding="utf-8"), asset_before)

    def test_profile_shared_markdown_is_rewritten_outside_vault(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            agent_root = tmp / "agent-memory"
            write_text(
                agent_root / ".agent-memory-beacon-root",
                "owned by agent-memory-beacon\n",
            )
            profile = agent_root / "codex-profile"
            shared = profile / "AGENTS.shared.md"
            asset = profile / "skills" / "example" / "SKILL.md"
            old = "github-obsidian-knowledge-brain"
            shared_before = (
                f"---\nproject: {old}\n---\n"
                f"[[01-Projects/{old}/Memory/decisions|Decision]]\n"
            )
            asset_before = (
                f"[[01-Projects/{old}/Memory/decisions|Copied asset]]\n"
            )
            write_text(shared, shared_before)
            write_text(asset, asset_before)
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "agent_memory_path": str(agent_root),
                    },
                    sort_keys=False,
                ),
            )

            plan = build_migration_plan(vault, config_path=config)

            self.assertIn(shared, plan.markdown_paths)
            self.assertIn(shared, {path for path, _digest in plan.observed_hashes})
            self.assertNotIn(asset, plan.markdown_paths)
            result = apply_brand_migration(
                plan,
                "profile-shared-markdown-external",
                rebuilders=[],
            )

            self.assertTrue(result["valid"])
            self.assertNotIn(old, shared.read_text(encoding="utf-8"))
            self.assertEqual(asset.read_text(encoding="utf-8"), asset_before)

    def test_external_profile_shared_broken_links_are_baselined_and_validated(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            agent_root = tmp / "agent-memory"
            write_text(
                agent_root / ".agent-memory-beacon-root",
                "owned by agent-memory-beacon\n",
            )
            shared = agent_root / "codex-profile" / "AGENTS.shared.md"
            old = "github-obsidian-knowledge-brain"
            write_text(
                shared,
                f"[[01-Projects/{old}/Memory/missing-before|Missing before]]\n",
            )
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "agent_memory_path": str(agent_root),
                    },
                    sort_keys=False,
                ),
            )
            plan = build_migration_plan(vault, config_path=config)

            self.assertTrue(
                any(
                    source == str(shared)
                    and target.endswith("/Memory/missing-before")
                    for source, target in plan.broken_links_before
                )
            )
            plan.source_project.rename(plan.destination_project)
            for original in plan.markdown_paths:
                target = (
                    plan.destination_project / original.relative_to(plan.source_project)
                    if original.is_relative_to(plan.source_project)
                    else original
                )
                updated, _changed = rewrite_markdown(
                    target.read_text(encoding="utf-8"),
                    plan.old_slug,
                    plan.new_slug,
                )
                target.write_text(updated, encoding="utf-8")
            with shared.open("a", encoding="utf-8") as handle:
                handle.write("[[missing-after-plan]]\n")

            validation = validate_brand_migration(plan)

            self.assertFalse(validation["valid"])
            self.assertEqual(validation["new_broken_links"], 1)

    def test_absent_profile_root_is_not_an_explicit_directory_target(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            profile = vault / "05-Agent-Memory" / "codex-profile"
            shared = profile / "AGENTS.shared.md"
            plan = build_migration_plan(vault)

            target_paths = {spec.path for spec in plan.mutation_contract.target_specs}
            self.assertNotIn(profile, target_paths)
            self.assertTrue(
                {
                    shared,
                    Path(str(shared) + ".tmp"),
                    Path(str(shared) + ".restore"),
                }.issubset(target_paths)
            )

            def create_profile(_cfg, _guard, mutation_io, checkpoint=None):
                mutation_io.ensure_directory(profile)

            with patch(
                "brand_migration._run_default_rebuilders",
                side_effect=create_profile,
            ):
                with self.assertRaisesRegex(RuntimeError, "excluded mutable subtree"):
                    apply_brand_migration(plan, "reject-absent-profile-root")

            self.assertFalse(profile.exists())
            self.assertTrue(plan.source_project.is_dir())
            self.assertFalse(plan.destination_project.exists())

    def test_default_profile_shared_context_rebuilds_and_rolls_back_exactly(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            profile = vault / "05-Agent-Memory" / "codex-profile"
            shared = profile / "AGENTS.shared.md"
            asset = profile / "skills" / "example" / "asset.bin"
            write_text(shared, "original shared context\n")
            asset.parent.mkdir(parents=True)
            asset.write_bytes(b"\x00copied-profile-asset\xff")
            before_shared = shared.read_bytes()
            before_asset = asset.read_bytes()
            plan = build_migration_plan(vault)

            def rebuild_shared(_cfg, _guard, mutation_io, checkpoint=None):
                mutation_io.atomic_write(shared, "rebuilt shared context\n")

            with patch(
                "brand_migration._run_default_rebuilders",
                side_effect=rebuild_shared,
            ):
                result = apply_brand_migration(plan, "default-profile-rollback")

            self.assertEqual(shared.read_text(encoding="utf-8"), "rebuilt shared context\n")
            self.assertEqual(asset.read_bytes(), before_asset)
            rollback_brand_migration(result["manifest_path"])
            self.assertEqual(shared.read_bytes(), before_shared)
            self.assertEqual(asset.read_bytes(), before_asset)
            self.assertTrue(plan.source_project.is_dir())
            self.assertFalse(plan.destination_project.exists())

    def test_non_explicit_profile_asset_write_is_rejected_and_auto_rolled_back(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            profile = vault / "05-Agent-Memory" / "codex-profile"
            shared = profile / "AGENTS.shared.md"
            asset = profile / "skills" / "example" / "SKILL.md"
            write_text(shared, "original shared context\n")
            write_text(asset, "original copied skill\n")
            before_shared = shared.read_bytes()
            before_asset = asset.read_bytes()
            plan = build_migration_plan(vault)

            def write_asset(_cfg, _guard, mutation_io, checkpoint=None):
                mutation_io.atomic_write(asset, "unauthorized mutation\n")

            with patch(
                "brand_migration._run_default_rebuilders",
                side_effect=write_asset,
            ):
                with self.assertRaisesRegex(RuntimeError, "excluded mutable subtree"):
                    apply_brand_migration(plan, "reject-profile-asset")

            self.assertEqual(shared.read_bytes(), before_shared)
            self.assertEqual(asset.read_bytes(), before_asset)
            self.assertTrue(plan.source_project.is_dir())
            self.assertFalse(plan.destination_project.exists())

    def test_profile_asset_drift_is_ignored_but_shared_context_drift_is_bound(self):
        with tempfile.TemporaryDirectory() as raw_vault:
            vault = Path(raw_vault).resolve()
            make_vault(vault)
            profile = vault / "05-Agent-Memory" / "codex-profile"
            shared = profile / "AGENTS.shared.md"
            asset = profile / "skills" / "example" / "SKILL.md"
            write_text(shared, "shared context\n")
            write_text(asset, "copied skill v1\n")

            asset_plan = build_migration_plan(vault)
            write_text(asset, "copied skill v2\n")
            create_migration_backup(asset_plan, "ignored-profile-asset-drift")

            shared_plan = build_migration_plan(vault)
            write_text(shared, "changed shared context\n")
            with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                create_migration_backup(shared_plan, "bound-profile-shared-drift")

    def test_profile_exclusion_manifest_round_trip_rejects_tampering_and_preserves_external_root(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "workspace" / "vault"
            make_vault(vault)
            agent_root = tmp / "external" / "agent-memory"
            marker = agent_root / ".agent-memory-beacon-root"
            ordinary = agent_root / "existing.md"
            profile = agent_root / "codex-profile"
            shared = profile / "AGENTS.shared.md"
            asset = profile / "skills" / "example" / "SKILL.md"
            write_text(marker, "owned by agent-memory-beacon\n")
            write_text(ordinary, "ordinary agent memory\n")
            write_text(shared, "shared context\n")
            write_text(asset, "copied skill asset\n")
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "agent_memory_path": str(agent_root),
                    },
                    sort_keys=False,
                ),
            )
            plan = build_migration_plan(vault, config_path=config)

            self.assertIn(marker, plan.backup_paths)
            self.assertIn(ordinary, plan.backup_paths)
            self.assertIn(shared, plan.backup_paths)
            self.assertNotIn(asset, plan.backup_paths)
            manifest_path = create_migration_backup(plan, "profile-exclusion-manifest")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(brand_migration.MIGRATION_MANIFEST_SCHEMA_VERSION, 2)
            self.assertEqual(manifest["schema_version"], 2)
            _vault, parsed_contract, _bindings = (
                brand_migration._validate_manifest_payload(manifest)
            )
            self.assertEqual(parsed_contract.excluded_mutable_subtrees, (profile,))
            self.assertEqual(
                manifest["excluded_mutable_subtrees"],
                manifest["mutation_contract"]["excluded_mutable_subtrees"],
            )

            alternate = {"kind": "external", "path": str(agent_root / "alternate")}
            for field in ("mutation_contract", "compatibility"):
                with self.subTest(tampered_projection=field):
                    tampered = json.loads(json.dumps(manifest))
                    if field == "mutation_contract":
                        tampered["mutation_contract"]["excluded_mutable_subtrees"] = [
                            alternate
                        ]
                    else:
                        tampered["excluded_mutable_subtrees"] = [alternate]
                    with self.assertRaisesRegex(
                        ValueError,
                        "compatibility projections do not match contract",
                    ):
                        brand_migration._validate_manifest_payload(tampered)

            outside_root = json.loads(json.dumps(manifest))
            outside_record = {"kind": "vault", "path": "00-Rules"}
            outside_root["mutation_contract"]["excluded_mutable_subtrees"] = [
                outside_record
            ]
            outside_root["excluded_mutable_subtrees"] = [outside_record]
            with self.assertRaisesRegex(ValueError, "excluded.*mutable root"):
                brand_migration._validate_manifest_payload(outside_root)

            schema_one = json.loads(json.dumps(manifest))
            schema_one["schema_version"] = 1
            with self.assertRaisesRegex(
                ValueError,
                "unsupported brand migration manifest schema",
            ):
                brand_migration._validate_manifest_payload(schema_one)

    def test_schema_two_requires_empty_top_level_exclusion_projection(self):
        import brand_migration

        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            profile = tmp / "standalone-profile"
            write_text(profile / "AGENTS.shared.md", "shared context\n")
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "codex_profile_path": str(profile),
                    },
                    sort_keys=False,
                ),
            )
            plan = build_migration_plan(vault, config_path=config)

            self.assertEqual(plan.mutation_contract.excluded_mutable_subtrees, ())
            manifest_path = create_migration_backup(
                plan,
                "empty-profile-exclusion-projection",
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["excluded_mutable_subtrees"], [])
            self.assertEqual(
                manifest["mutation_contract"]["excluded_mutable_subtrees"],
                [],
            )

            del manifest["excluded_mutable_subtrees"]

            with self.assertRaisesRegex(
                ValueError,
                "excluded mutable subtrees projection is invalid",
            ):
                brand_migration._validate_manifest_payload(manifest)

    def test_external_agent_memory_rejects_broad_or_ancestor_roots_before_scan(self):
        candidates = []
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            workspace = tmp / "workspace"
            vault = workspace / "vault"
            make_vault(vault)
            candidates.extend(
                [
                    Path(vault.anchor),
                    Path.home().resolve(),
                    vault.resolve(),
                    workspace.resolve(),
                    tmp,
                ]
            )
            real_walk = __import__("brand_migration")._walk_tree

            for index, candidate in enumerate(candidates):
                with self.subTest(candidate=candidate):
                    config = tmp / f"config-{index}.yaml"
                    write_text(
                        config,
                        yaml.safe_dump(
                            {
                                "vault_path": str(vault),
                                "agent_memory_path": str(candidate),
                            },
                            sort_keys=False,
                        ),
                    )

                    def refuse_unsafe_scan(root, *args, **kwargs):
                        if Path(root) == candidate:
                            raise AssertionError(f"unsafe root scanned: {candidate}")
                        return real_walk(root, *args, **kwargs)

                    with patch(
                        "brand_migration._walk_tree",
                        side_effect=refuse_unsafe_scan,
                    ):
                        with self.assertRaisesRegex(
                            ValueError,
                            "agent_memory_path.*dedicated",
                        ):
                            build_migration_plan(vault, config_path=config)

    def test_external_agent_memory_rejects_desktop_and_downloads_before_scan(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            synthetic_home = tmp / "home" / "person"
            vault = tmp / "workspace" / "vault"
            make_vault(vault)
            real_walk = __import__("brand_migration")._walk_tree

            for name in ("Desktop", "Downloads"):
                candidate = synthetic_home / name
                candidate.mkdir(parents=True)
                if name == "Downloads":
                    write_text(
                        candidate / ".agent-memory-beacon-root", "owned\n"
                    )
                config = tmp / f"config-{name}.yaml"
                write_text(
                    config,
                    yaml.safe_dump(
                        {
                            "vault_path": str(vault),
                            "agent_memory_path": str(candidate),
                        },
                        sort_keys=False,
                    ),
                )

                def reject_scan(root, *args, **kwargs):
                    if Path(root) == candidate:
                        raise AssertionError(f"broad root scanned: {candidate}")
                    return real_walk(root, *args, **kwargs)

                with self.subTest(name=name), patch.object(
                    Path, "home", return_value=synthetic_home
                ), patch(
                    "brand_migration._walk_tree", side_effect=reject_scan
                ):
                    with self.assertRaisesRegex(ValueError, "Desktop|Downloads"):
                        build_migration_plan(vault, config_path=config)

    def test_nonempty_external_agent_memory_requires_marker_before_scan(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "workspace" / "vault"
            make_vault(vault)
            agent_root = tmp / "external" / "agent-memory"
            write_text(agent_root / "unrelated-secret.txt", "secret\n")
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "agent_memory_path": str(agent_root),
                    },
                    sort_keys=False,
                ),
            )
            real_walk = __import__("brand_migration")._walk_tree

            def reject_recursive_scan(root, *args, **kwargs):
                if Path(root) == agent_root:
                    raise AssertionError("unowned external root recursively scanned")
                return real_walk(root, *args, **kwargs)

            with patch(
                "brand_migration._walk_tree", side_effect=reject_recursive_scan
            ):
                with self.assertRaisesRegex(ValueError, "ownership marker"):
                    build_migration_plan(vault, config_path=config)

    def test_external_agent_memory_accepts_empty_or_nonexistent_root(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "workspace" / "vault"
            make_vault(vault)
            roots = (
                tmp / "external" / "empty-agent-memory",
                tmp / "external" / "missing-agent-memory",
            )
            roots[0].mkdir(parents=True)

            for index, agent_root in enumerate(roots):
                config = tmp / f"config-{index}.yaml"
                write_text(
                    config,
                    yaml.safe_dump(
                        {
                            "vault_path": str(vault),
                            "agent_memory_path": str(agent_root),
                        },
                        sort_keys=False,
                    ),
                )
                with self.subTest(agent_root=agent_root):
                    plan = build_migration_plan(vault, config_path=config)
                    self.assertIn(agent_root, plan.mutation_contract.mutable_roots)

    def test_external_agent_memory_accepts_dedicated_nonoverlapping_root(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "workspace" / "vault"
            make_vault(vault)
            agent_root = tmp / "external" / "agent-memory"
            marker = agent_root / ".agent-memory-beacon-root"
            write_text(marker, "owned by agent-memory-beacon\n")
            write_text(agent_root / "existing.md", "existing\n")
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "agent_memory_path": str(agent_root),
                    },
                    sort_keys=False,
                ),
            )

            plan = build_migration_plan(vault, config_path=config)

            self.assertIn(agent_root, plan.mutation_contract.mutable_roots)
            self.assertIn(marker, plan.backup_paths)

    def test_external_agent_memory_accepts_legacy_ownership_marker(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "workspace" / "vault"
            make_vault(vault)
            agent_root = tmp / "external" / "legacy-agent-memory"
            marker = agent_root / ".obsidian-knowledge-brain-root"
            write_text(marker, "legacy owned root\n")
            write_text(agent_root / "existing.md", "existing\n")
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "agent_memory_path": str(agent_root),
                    },
                    sort_keys=False,
                ),
            )

            plan = build_migration_plan(vault, config_path=config)

            self.assertIn(marker, plan.backup_paths)

    def test_preflight_propagates_inventory_walk_errors(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            real_walk = os.walk
            injected = False

            def permission_denied_walk(top, *args, **kwargs):
                nonlocal injected
                if not injected:
                    injected = True
                    kwargs["onerror"](PermissionError("inventory denied"))
                    return iter(())
                return real_walk(top, *args, **kwargs)

            with patch("brand_migration.os.walk", side_effect=permission_denied_walk):
                with self.assertRaisesRegex(PermissionError, "inventory denied"):
                    build_migration_plan(vault)

    def test_preflight_rejects_hard_link_alias_between_config_and_context(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            config = tmp / "config.yaml"
            context = tmp / "AGENTS.md"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "context_targets": [str(context)],
                    },
                    sort_keys=False,
                ),
            )
            os.link(config, context)

            with self.assertRaisesRegex(ValueError, "hard-link.*config_path.*context"):
                build_migration_plan(vault, config_path=config)

    def test_forged_plan_cannot_remove_external_context_profile_or_config(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            context = tmp / "AGENTS.md"
            profile = tmp / "profile"
            shared = profile / "AGENTS.shared.md"
            write_text(context, "context\n")
            write_text(shared, "profile\n")
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "context_targets": [str(context)],
                        "codex_profile_path": str(profile),
                    },
                    sort_keys=False,
                ),
            )
            plan = build_migration_plan(vault, config_path=config)

            for role in ("context", "profile", "config_path"):
                with self.subTest(role=role):
                    removed = {
                        spec.path
                        for spec in plan.mutation_contract.target_specs
                        if spec.role == role
                    }
                    forged_contract = replace(
                        plan.mutation_contract,
                        target_specs=tuple(
                            spec
                            for spec in plan.mutation_contract.target_specs
                            if spec.path not in removed
                        ),
                        mutable_files=tuple(
                            path
                            for path in plan.mutation_contract.mutable_files
                            if path not in removed
                        ),
                        absent_paths=tuple(
                            path
                            for path in plan.mutation_contract.absent_paths
                            if path not in removed
                        ),
                    )
                    forged = replace(
                        plan,
                        mutation_contract=forged_contract,
                        input_bindings=tuple(
                            binding
                            for binding in plan.input_bindings
                            if binding.path not in removed
                        ),
                    )

                    with self.assertRaisesRegex(ValueError, "mutation contract"):
                        plan_summary(forged)

    def test_forged_plan_cannot_change_target_role_path_or_state(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            original = next(
                spec
                for spec in plan.mutation_contract.target_specs
                if spec.entry_kind == "file" and spec.expected_state == "file"
            )
            replacements = (
                replace(original, role="forged-role"),
                replace(original, path=original.path.with_name("forged.md")),
                replace(original, expected_state="absent", inode=None),
            )

            for forged_spec in replacements:
                with self.subTest(spec=forged_spec):
                    forged_contract = replace(
                        plan.mutation_contract,
                        target_specs=tuple(
                            forged_spec if spec == original else spec
                            for spec in plan.mutation_contract.target_specs
                        ),
                    )
                    forged = replace(plan, mutation_contract=forged_contract)
                    with self.assertRaisesRegex(ValueError, "mutation contract"):
                        plan_summary(forged)

    def test_forged_plan_cannot_omit_root_file_or_absence(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            root = plan.mutation_contract.mutable_roots[0]
            mutable_file = plan.mutation_contract.mutable_files[0]
            absent = plan.mutation_contract.absent_paths[0]
            forged_contract = replace(
                plan.mutation_contract,
                target_specs=tuple(
                    spec
                    for spec in plan.mutation_contract.target_specs
                    if spec.path not in {root, mutable_file, absent}
                ),
                mutable_roots=tuple(
                    path for path in plan.mutation_contract.mutable_roots if path != root
                ),
                mutable_files=tuple(
                    path
                    for path in plan.mutation_contract.mutable_files
                    if path != mutable_file
                ),
                absent_paths=tuple(
                    path
                    for path in plan.mutation_contract.absent_paths
                    if path != absent
                ),
            )
            forged = replace(
                plan,
                mutation_contract=forged_contract,
                input_bindings=tuple(
                    binding
                    for binding in plan.input_bindings
                    if binding.path != mutable_file
                ),
            )

            with self.assertRaisesRegex(ValueError, "mutation contract"):
                plan_summary(forged)

    def test_forged_plan_rejects_inconsistent_input_binding_collection(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            forged = replace(plan, input_bindings=plan.input_bindings[1:])

            with self.assertRaisesRegex(ValueError, "input binding paths"):
                plan_summary(forged)

    def test_structural_input_same_bytes_new_inode_fails_revalidation(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            structural = make_structural_rule(vault)
            plan = build_migration_plan(vault)
            self.assertIn(structural, plan.markdown_paths)
            self.assertNotIn(structural, plan.mutation_contract.mutable_files)
            original_bytes = structural.read_bytes()
            original_inode = structural.stat().st_ino
            replacement = structural.with_name("replacement.tmp")
            replacement.write_bytes(original_bytes)
            os.replace(replacement, structural)
            self.assertNotEqual(structural.stat().st_ino, original_inode)

            with self.assertRaisesRegex(RuntimeError, "input inode changed"):
                plan_summary(plan)
            with self.assertRaisesRegex(RuntimeError, "input inode changed"):
                create_migration_backup(plan, "same-bytes-new-inode")

            assert_failed_backup_absent(vault, "same-bytes-new-inode")

    def test_structural_input_with_unlisted_hard_link_fails_preflight(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            structural = make_structural_rule(vault)
            os.link(structural, tmp / "unlisted-rule-alias.md")

            with self.assertRaisesRegex(
                ValueError, "input has unlisted hard-link alias"
            ):
                build_migration_plan(vault)

    def test_preflight_rejects_same_size_write_during_descriptor_hashing(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            structural = make_structural_rule(vault)
            original = structural.read_bytes()
            changed = original.replace(b"Project", b"Changed", 1)
            self.assertEqual(len(changed), len(original))
            target_inode = (structural.stat().st_dev, structural.stat().st_ino)
            module = __import__("brand_migration")
            real_hash_fd = module._hash_fd
            mutated = False

            def hash_then_mutate(fd):
                nonlocal mutated
                digest = real_hash_fd(fd)
                current = os.fstat(fd)
                if (current.st_dev, current.st_ino) == target_inode and not mutated:
                    structural.write_bytes(changed)
                    os.utime(
                        structural,
                        ns=(current.st_atime_ns, current.st_mtime_ns + 1_000_000_000),
                    )
                    mutated = True
                return digest

            with patch("brand_migration._hash_fd", side_effect=hash_then_mutate):
                with self.assertRaisesRegex(ValueError, "changed while hashing"):
                    build_migration_plan(vault)

            self.assertTrue(mutated)
            self.assertEqual(structural.read_bytes(), changed)

    def test_copy_rejects_same_size_write_between_read_and_second_fstat(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            source = plan.backup_paths[0]
            original = source.read_bytes()
            changed = bytes([original[0] ^ 1]) + original[1:]
            self.assertEqual(len(changed), len(original))
            real_fsync = os.fsync
            mutated = False

            def fsync_then_mutate(fd):
                nonlocal mutated
                result = real_fsync(fd)
                if not mutated:
                    before = source.stat()
                    source.write_bytes(changed)
                    os.utime(
                        source,
                        ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
                    )
                    mutated = True
                return result

            with patch("brand_migration.os.fsync", side_effect=fsync_then_mutate):
                with self.assertRaisesRegex(RuntimeError, "changed while copying"):
                    create_migration_backup(plan, "copy-metadata-drift")

            self.assertTrue(mutated)
            assert_no_published_or_staged_backup(vault, "copy-metadata-drift")

    def test_input_bindings_and_manifest_freeze_write_metadata(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            binding = plan.input_bindings[0]
            current = binding.path.stat()

            self.assertEqual(binding.mode, current.st_mode)
            self.assertEqual(binding.size, current.st_size)
            self.assertEqual(binding.mtime_ns, current.st_mtime_ns)
            self.assertEqual(binding.ctime_ns, current.st_ctime_ns)

            manifest_path = create_migration_backup(plan, "binding-metadata")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            serialized = manifest["input_bindings"][0]
            self.assertEqual(serialized["mode"], binding.mode)
            self.assertEqual(serialized["size"], binding.size)
            self.assertEqual(serialized["mtime_ns"], binding.mtime_ns)
            self.assertEqual(serialized["ctime_ns"], binding.ctime_ns)

    def test_added_unlisted_hard_link_fails_exact_revalidation(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            structural = make_structural_rule(vault)
            plan = build_migration_plan(vault)
            os.link(structural, tmp / "late-rule-alias.md")

            with self.assertRaisesRegex(RuntimeError, "input link count changed"):
                plan_summary(plan)
            with self.assertRaisesRegex(RuntimeError, "input link count changed"):
                create_migration_backup(plan, "late-input-hard-link")

            assert_failed_backup_absent(vault, "late-input-hard-link")

    def test_forged_input_bindings_cannot_bypass_backup(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            first = plan.input_bindings[0]
            for label, bindings in (
                ("removed", plan.input_bindings[1:]),
                ("duplicate", (first, *plan.input_bindings)),
                (
                    "digest",
                    (replace(first, sha256="0" * 64), *plan.input_bindings[1:]),
                ),
                (
                    "inode",
                    (
                        replace(first, inode=(first.inode[0], first.inode[1] + 1)),
                        *plan.input_bindings[1:],
                    ),
                ),
                (
                    "link-count",
                    (replace(first, link_count=2), *plan.input_bindings[1:]),
                ),
                (
                    "mode",
                    (replace(first, mode=first.mode ^ 0o100), *plan.input_bindings[1:]),
                ),
                (
                    "size",
                    (replace(first, size=first.size + 1), *plan.input_bindings[1:]),
                ),
                (
                    "mtime",
                    (
                        replace(first, mtime_ns=first.mtime_ns + 1),
                        *plan.input_bindings[1:],
                    ),
                ),
                (
                    "ctime",
                    (
                        replace(first, ctime_ns=first.ctime_ns + 1),
                        *plan.input_bindings[1:],
                    ),
                ),
            ):
                with self.subTest(label=label):
                    forged = replace(plan, input_bindings=tuple(bindings))
                    with self.assertRaisesRegex(
                        (ValueError, RuntimeError),
                        "input binding|input (?:inode|digest|link|mode|size|mtime|ctime)",
                    ):
                        plan_summary(forged)
                    with self.assertRaisesRegex(
                        (ValueError, RuntimeError),
                        "input binding|input (?:inode|digest|link|mode|size|mtime|ctime)",
                    ):
                        create_migration_backup(forged, f"forged-binding-{label}")
                    assert_failed_backup_absent(vault, f"forged-binding-{label}")

    def test_preflight_rejects_duplicate_canonical_configured_targets(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            target = tmp / "AGENTS.md"
            write_text(target, "context\n")
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "context_targets": [str(target)],
                        "claude_md_path": str(target),
                    },
                    sort_keys=False,
                ),
            )

            with self.assertRaisesRegex(ValueError, "duplicate canonical mutation input"):
                build_migration_plan(vault, config_path=config)

    def test_preflight_rejects_memory_index_outside_vault(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "memory_index_path": str(tmp / "outside-index.md"),
                    },
                    sort_keys=False,
                ),
            )

            with self.assertRaisesRegex(ValueError, "memory_index_path.*vault"):
                build_migration_plan(vault, config_path=config)

    def test_preflight_rejects_public_markdown_file_symlink_without_reading_target(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            outside = tmp / "outside.md"
            outside.write_bytes(b"\xffnot utf8")
            linked = vault / "00-Rules" / "linked.md"
            linked.parent.mkdir()
            linked.symlink_to(outside)

            with self.assertRaisesRegex(ValueError, "symlink"):
                build_migration_plan(vault)

    def test_preflight_rejects_public_directory_symlink(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            outside = tmp / "outside"
            write_text(outside / "note.md", "# Outside\n")
            linked = vault / "00-Rules" / "linked"
            linked.parent.mkdir()
            linked.symlink_to(outside, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "symlink"):
                build_migration_plan(vault)

    def test_preflight_excludes_obsidian_plugin_markdown_and_symlinks(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            outside = tmp / "plugin-readme.md"
            outside.write_bytes(b"\xffplugin")
            plugin = vault / ".obsidian" / "plugins" / "demo" / "README.md"
            plugin.parent.mkdir(parents=True)
            plugin.symlink_to(outside)

            plan = build_migration_plan(vault)

            self.assertNotIn(plugin, plan.markdown_paths)
            self.assertNotIn(plugin, tuple(path for path, _hash in plan.observed_hashes))

    def test_preflight_rejects_configured_target_symlink(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            real_target = tmp / "real-AGENTS.md"
            write_text(real_target, "context\n")
            linked_target = tmp / "AGENTS.md"
            linked_target.symlink_to(real_target)
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "context_targets": [str(linked_target)],
                    },
                    sort_keys=False,
                ),
            )

            with self.assertRaisesRegex(ValueError, "configured target.*symlink"):
                build_migration_plan(vault, config_path=config)

    def test_preflight_rejects_config_path_intermediate_symlink(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            real_dir = tmp / "real-config"
            config = real_dir / "config.yaml"
            write_text(
                config,
                yaml.safe_dump({"vault_path": str(vault)}, sort_keys=False),
            )
            alias = tmp / "config-alias"
            alias.symlink_to(real_dir, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "config_path.*symlink"):
                build_migration_plan(vault, config_path=alias / "config.yaml")

    def test_preflight_rejects_rollback_parent_symlink_inside_vault(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            real_parent = vault / "04-Feedback" / "real-rollback"
            real_parent.mkdir(parents=True)
            rollback = vault / "04-Feedback" / "_rollback"
            rollback.symlink_to(real_parent, target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "rollback.*symlink"):
                build_migration_plan(vault)

    def test_backup_manifest_covers_every_mutated_input(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)

            manifest_path = create_migration_backup(plan, "test-migration")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(manifest["status"], "prepared")
            self.assertEqual(manifest["generated_by"], "agent_memory_beacon")
            self.assertEqual(manifest["old_slug"], plan.old_slug)
            self.assertTrue(manifest["files"])
            for item in manifest["files"]:
                self.assertEqual(len(item["sha256"]), 64)

    def test_backup_preserves_binary_config_context_and_profile_inputs(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            source = make_vault(vault)
            binary = source / "assets" / "memory.bin"
            binary.parent.mkdir()
            binary.write_bytes(b"\x00\xff\x80agent-memory\x00")
            current = Path(tmp) / "AGENTS.md"
            legacy = Path(tmp) / "CLAUDE.md"
            profile = Path(tmp) / "profile"
            shared = profile / "AGENTS.shared.md"
            current.write_bytes(b"current context\n")
            legacy.write_bytes(b"legacy context\n")
            profile.mkdir()
            shared.write_bytes(b"shared profile context\n")
            config = Path(tmp) / "config.yaml"
            write_text(
                config,
                f"vault_path: {vault}\n"
                "context_targets:\n"
                f"  - {current}\n"
                f"claude_md_path: {legacy}\n"
                f"codex_profile_path: {profile}\n",
            )
            plan = build_migration_plan(vault, config_path=config)
            before = {path: path.read_bytes() for path in plan.backup_paths}

            manifest_path = create_migration_backup(plan, "byte-exact")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            restored_sources = set()
            backup_paths = set()
            for item in manifest["files"]:
                source_path = (
                    plan.vault / item["path"]
                    if item["kind"] == "vault"
                    else Path(item["path"])
                )
                backup_path = manifest_path.parent / item["backup"]
                restored_sources.add(source_path)
                backup_paths.add(backup_path)
                self.assertEqual(backup_path.read_bytes(), before[source_path])
                self.assertEqual(
                    item["sha256"],
                    hashlib.sha256(before[source_path]).hexdigest(),
                )
            self.assertEqual(restored_sources, set(plan.backup_paths))
            self.assertEqual(len(backup_paths), len(plan.backup_paths))
            self.assertEqual(
                {path: path.read_bytes() for path in plan.backup_paths},
                before,
            )
            self.assertFalse(manifest_path.with_suffix(".json.tmp").exists())

    def test_backup_rejects_unsafe_migration_id(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)

            with self.assertRaisesRegex(ValueError, "invalid migration id"):
                create_migration_backup(plan, "../../escape")

    def test_backup_rejects_empty_migration_id(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)

            with self.assertRaisesRegex(ValueError, "invalid migration id"):
                create_migration_backup(plan, "")

    def test_backup_rejects_source_drift_before_writing(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            plan.source_project.joinpath("Memory", "decisions.md").write_text(
                "changed after preflight\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                create_migration_backup(plan, "source-drift")

            self.assertFalse(
                Path(vault)
                .joinpath(
                    "04-Feedback",
                    "_rollback",
                    "brand-migration",
                    "source-drift",
                )
                .exists()
            )

    def test_backup_rejects_destination_created_after_preflight(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            plan.destination_project.mkdir()

            with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                create_migration_backup(plan, "destination-drift")

            self.assertFalse(
                Path(vault)
                .joinpath(
                    "04-Feedback",
                    "_rollback",
                    "brand-migration",
                    "destination-drift",
                )
                .exists()
            )

    def test_backup_rejects_new_source_file_after_preflight(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            plan.source_project.joinpath("new.bin").write_bytes(b"new")

            with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                create_migration_backup(plan, "inventory-drift")

            self.assertFalse(
                Path(vault)
                .joinpath(
                    "04-Feedback",
                    "_rollback",
                    "brand-migration",
                    "inventory-drift",
                )
                .exists()
            )

    def test_backup_rejects_external_context_drift_after_preflight(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            context = Path(tmp) / "AGENTS.md"
            context.write_text("before\n", encoding="utf-8")
            config = Path(tmp) / "config.yaml"
            write_text(
                config,
                f"vault_path: {vault}\ncontext_targets:\n  - {context}\n",
            )
            plan = build_migration_plan(vault, config_path=config)
            context.write_text("after\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                create_migration_backup(plan, "context-drift")

            self.assertFalse(
                vault.joinpath(
                    "04-Feedback",
                    "_rollback",
                    "brand-migration",
                    "context-drift",
                ).exists()
            )

    def test_backup_rejects_observed_public_markdown_drift(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            observed = Path(vault) / "00-Rules" / "stable-rule.md"
            write_text(observed, "# Stable rule\n")
            plan = build_migration_plan(vault)
            self.assertNotIn(observed.resolve(), plan.backup_paths)
            observed.write_text("# Changed rule\n", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                create_migration_backup(plan, "observed-drift")

            self.assertFalse(
                Path(vault)
                .joinpath(
                    "04-Feedback",
                    "_rollback",
                    "brand-migration",
                    "observed-drift",
                )
                .exists()
            )

    def test_backup_rejects_lexical_input_path_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "vault"
            make_vault(vault)
            outside = Path(tmp) / "outside.bin"
            outside.write_bytes(b"outside")
            plan = build_migration_plan(vault)
            escaped = vault / ".." / outside.name
            original = next(
                spec
                for spec in plan.mutation_contract.target_specs
                if spec.expected_state == "file"
            )
            forged_spec = replace(
                original,
                path=escaped,
                inode=(outside.stat().st_dev, outside.stat().st_ino),
            )
            forged_specs = tuple(
                forged_spec if spec == original else spec
                for spec in plan.mutation_contract.target_specs
            )
            forged_files = tuple(
                sorted(
                    escaped if path == original.path else path
                    for path in plan.mutation_contract.mutable_files
                )
            )
            forged_contract = replace(
                plan.mutation_contract,
                target_specs=forged_specs,
                mutable_files=forged_files,
            )
            original_binding = next(
                binding
                for binding in plan.input_bindings
                if binding.path == original.path
            )
            forged_bindings = tuple(
                sorted(
                    [
                        replace(
                            original_binding,
                            path=escaped,
                            sha256=hashlib.sha256(b"outside").hexdigest(),
                            inode=(outside.stat().st_dev, outside.stat().st_ino),
                        )
                        if binding == original_binding
                        else binding
                        for binding in plan.input_bindings
                    ],
                    key=lambda binding: binding.path,
                )
            )
            forged = replace(
                plan,
                mutation_contract=forged_contract,
                input_bindings=forged_bindings,
            )

            with self.assertRaisesRegex(ValueError, "path escape"):
                create_migration_backup(forged, "path-escape")

            self.assertFalse(
                vault.joinpath(
                    "04-Feedback",
                    "_rollback",
                    "brand-migration",
                    "path-escape",
                ).exists()
            )

    def test_backup_rejects_duplicate_inputs_before_writing(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            source = plan.input_bindings[0].path
            forged_contract = replace(
                plan.mutation_contract,
                mutable_files=(source, *plan.mutation_contract.mutable_files),
            )
            forged = replace(
                plan,
                mutation_contract=forged_contract,
                input_bindings=(
                    plan.input_bindings[0],
                    *plan.input_bindings,
                ),
            )

            with self.assertRaisesRegex(ValueError, "mutation contract"):
                create_migration_backup(forged, "duplicate-input")

            self.assertFalse(
                Path(vault)
                .joinpath(
                    "04-Feedback",
                    "_rollback",
                    "brand-migration",
                    "duplicate-input",
                )
                .exists()
            )

    def test_backup_rejects_colliding_destinations_before_writing(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            first = Path(tmp) / "one" / "shared.bin"
            second = Path(tmp) / "two" / "shared.bin"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            first = first.resolve()
            second = second.resolve()
            config = Path(tmp) / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "context_targets": [str(first), str(second)],
                    },
                    sort_keys=False,
                ),
            )
            plan = build_migration_plan(vault, config_path=config)
            real_sha256 = hashlib.sha256

            def colliding_sha256(value=b""):
                if value:
                    digest = unittest.mock.Mock()
                    digest.hexdigest.return_value = "f" * 64
                    return digest
                return real_sha256()

            with patch("brand_migration.hashlib.sha256", side_effect=colliding_sha256):
                with self.assertRaisesRegex(ValueError, "colliding backup destination"):
                    create_migration_backup(plan, "destination-collision")

            self.assertFalse(
                vault.joinpath(
                    "04-Feedback",
                    "_rollback",
                    "brand-migration",
                    "destination-collision",
                ).exists()
            )

    def test_backup_rejects_copy_time_drift_and_removes_partial_backup(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            changed_source = plan.backup_paths[0]
            real_copy = __import__("brand_migration")._copy_backup_file
            mutated = False

            def copy_then_mutate(*args, **kwargs):
                nonlocal mutated
                result = real_copy(*args, **kwargs)
                source = args[0]
                if Path(source) == changed_source and not mutated:
                    Path(source).write_bytes(b"changed during backup")
                    mutated = True
                return result

            with patch(
                "brand_migration._copy_backup_file",
                side_effect=copy_then_mutate,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed during backup"):
                    create_migration_backup(plan, "copy-time-drift")

            self.assertFalse(
                Path(vault)
                .joinpath(
                    "04-Feedback",
                    "_rollback",
                    "brand-migration",
                    "copy-time-drift",
                )
                .exists()
            )

    def test_backup_rejects_late_drift_of_already_copied_input(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            real_copy = __import__("brand_migration")._copy_backup_file
            copied = []

            def mutate_first_after_second(*args, **kwargs):
                result = real_copy(*args, **kwargs)
                source = args[0]
                copied.append(Path(source))
                if len(copied) == 2:
                    copied[0].write_bytes(b"late source drift")
                return result

            with patch(
                "brand_migration._copy_backup_file",
                side_effect=mutate_first_after_second,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                    create_migration_backup(plan, "late-copied-input")

            assert_failed_backup_absent(vault, "late-copied-input")

    def test_backup_rejects_late_observed_only_drift(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            observed = Path(vault) / "00-Rules" / "observed.md"
            write_text(observed, "# Observed\n")
            plan = build_migration_plan(vault)
            self.assertNotIn(observed.resolve(), plan.backup_paths)
            real_copy = __import__("brand_migration")._copy_backup_file
            copies = 0

            def mutate_observed_late(*args, **kwargs):
                nonlocal copies
                result = real_copy(*args, **kwargs)
                copies += 1
                if copies == 2:
                    observed.write_text("# Late drift\n", encoding="utf-8")
                return result

            with patch(
                "brand_migration._copy_backup_file",
                side_effect=mutate_observed_late,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                    create_migration_backup(plan, "late-observed")

            assert_failed_backup_absent(vault, "late-observed")

    def test_backup_rejects_late_new_source_file(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            real_copy = __import__("brand_migration")._copy_backup_file
            copies = 0

            def add_source_late(*args, **kwargs):
                nonlocal copies
                result = real_copy(*args, **kwargs)
                copies += 1
                if copies == 2:
                    plan.source_project.joinpath("late.bin").write_bytes(b"late")
                return result

            with patch(
                "brand_migration._copy_backup_file",
                side_effect=add_source_late,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                    create_migration_backup(plan, "late-source-file")

            assert_failed_backup_absent(vault, "late-source-file")

    def test_backup_rejects_late_destination_creation(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            real_copy = __import__("brand_migration")._copy_backup_file
            copies = 0

            def create_destination_late(*args, **kwargs):
                nonlocal copies
                result = real_copy(*args, **kwargs)
                copies += 1
                if copies == 2:
                    plan.destination_project.mkdir()
                return result

            with patch(
                "brand_migration._copy_backup_file",
                side_effect=create_destination_late,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                    create_migration_backup(plan, "late-destination")

            assert_failed_backup_absent(vault, "late-destination")

    def test_backup_rejects_late_appearance_of_absent_configured_target(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            make_vault(vault)
            absent = tmp / "missing" / "AGENTS.md"
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "context_targets": [str(absent)],
                    },
                    sort_keys=False,
                ),
            )
            plan = build_migration_plan(vault, config_path=config)
            real_copy = __import__("brand_migration")._copy_backup_file
            copies = 0

            def create_absent_late(*args, **kwargs):
                nonlocal copies
                result = real_copy(*args, **kwargs)
                copies += 1
                if copies == 2:
                    write_text(absent, "appeared\n")
                return result

            with patch(
                "brand_migration._copy_backup_file",
                side_effect=create_absent_late,
            ):
                with self.assertRaisesRegex(RuntimeError, "absent target appeared"):
                    create_migration_backup(plan, "late-absent-target")

            assert_failed_backup_absent(vault, "late-absent-target")

    def test_backup_rechecks_all_backup_hashes_before_manifest(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            real_copy = __import__("brand_migration")._copy_backup_file
            copied_destinations = []

            def tamper_first_backup_after_second(*args, **kwargs):
                result = real_copy(*args, **kwargs)
                copied_destinations.append(Path(args[2]))
                if len(copied_destinations) == 2:
                    staging = find_staging_root(vault)
                    staging.joinpath(copied_destinations[0]).write_bytes(
                        b"tampered backup"
                    )
                return result

            with patch(
                "brand_migration._copy_backup_file",
                side_effect=tamper_first_backup_after_second,
            ):
                with self.assertRaisesRegex(RuntimeError, "backup hash changed"):
                    create_migration_backup(plan, "late-backup-tamper")

            assert_failed_backup_absent(vault, "late-backup-tamper")

    def test_backup_rejects_rollback_parent_symlink_created_after_plan(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            real_parent = Path(vault) / "04-Feedback" / "real-rollback"
            real_parent.mkdir(parents=True)
            rollback = Path(vault) / "04-Feedback" / "_rollback"
            rollback.symlink_to(real_parent, target_is_directory=True)

            with self.assertRaisesRegex(
                ValueError,
                "(?:rollback|backup destination).*symlink",
            ):
                create_migration_backup(plan, "late-rollback-alias")

            self.assertFalse((real_parent / "brand-migration").exists())

    def test_backup_rejects_dangling_symlink_at_migration_leaf(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            parent = Path(vault).resolve() / "04-Feedback" / "_rollback" / "brand-migration"
            parent.mkdir(parents=True)
            leaf = parent / "dangling-leaf"
            leaf.symlink_to(parent / "missing-target", target_is_directory=True)

            with self.assertRaisesRegex(ValueError, "backup destination.*symlink"):
                create_migration_backup(plan, "dangling-leaf")

    def test_backup_fails_closed_without_secure_dir_fd_primitives(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)

            with patch(
                "brand_migration.os.supports_dir_fd",
                frozenset(),
            ):
                with self.assertRaisesRegex(RuntimeError, "publication is unavailable"):
                    create_migration_backup(plan, "unsupported-platform")

            assert_no_published_or_staged_backup(vault, "unsupported-platform")

    def test_backup_cleanup_failure_reports_primary_and_cleanup_errors(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)

            with patch(
                "brand_migration._copy_backup_file",
                side_effect=OSError("primary copy failure"),
            ), patch(
                "brand_migration._remove_tree_at",
                side_effect=OSError("cleanup failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "primary copy failure.*cleanup failure",
                ):
                    create_migration_backup(plan, "cleanup-failure")

    def test_moved_staging_inode_reports_primary_and_cleanup_failure(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            quarantine = Path(vault).resolve() / "quarantine"
            quarantine.mkdir()
            plan = build_migration_plan(vault)
            real_copy = __import__("brand_migration")._copy_backup_file
            moved = quarantine / "moved-staging"
            moved_once = False

            def move_staging_then_fail(*args, **kwargs):
                nonlocal moved_once
                result = real_copy(*args, **kwargs)
                if not moved_once:
                    find_staging_root(vault).rename(moved)
                    moved_once = True
                    raise OSError("primary moved staging")
                return result

            with patch(
                "brand_migration._copy_backup_file",
                side_effect=move_staging_then_fail,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "primary moved staging.*cleanup failed.*owned backup inode",
                ):
                    create_migration_backup(plan, "moved-staging")

            self.assertTrue(moved.exists())
            self.assertFalse((moved / "manifest.json").exists())

    def test_post_publish_reseal_failure_cleans_published_inode(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            final_root = (
                Path(vault).resolve()
                / "04-Feedback"
                / "_rollback"
                / "brand-migration"
                / "reseal-failure"
            )
            real_fchmod = os.fchmod
            observed_unsealed_mode = []

            def fail_after_published_reseal(fd, mode):
                if mode == 0o500 and final_root.exists():
                    observed_unsealed_mode.append(
                        stat.S_IMODE(os.fstat(fd).st_mode)
                    )
                    raise OSError("post-publish reseal failure")
                return real_fchmod(fd, mode)

            with patch(
                "brand_migration.os.fchmod",
                side_effect=fail_after_published_reseal,
            ):
                with self.assertRaisesRegex(
                    OSError, "post-publish reseal failure"
                ):
                    create_migration_backup(plan, "reseal-failure")

            self.assertEqual(observed_unsealed_mode, [0o700])
            assert_no_published_or_staged_backup(vault, "reseal-failure")

    def test_staging_open_failure_removes_private_directory(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            real_open = os.open

            def fail_staging_open(path, *args, **kwargs):
                if str(path).startswith(
                    ".agent-memory-beacon-brand-migration-"
                ):
                    raise OSError("staging open failure")
                return real_open(path, *args, **kwargs)

            with patch("brand_migration.os.open", side_effect=fail_staging_open):
                with self.assertRaisesRegex(OSError, "staging open failure"):
                    create_migration_backup(plan, "staging-open-failure")

            assert_no_published_or_staged_backup(vault, "staging-open-failure")

    def test_atomic_manifest_writer_removes_temp_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"

            with patch(
                "brand_migration.os.replace",
                side_effect=OSError("replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "replace failure"):
                    atomic_write_json(path, {"status": "prepared"})

            self.assertFalse(path.exists())
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_manifest_serialization_input_drift_prevents_publication(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            changed = plan.backup_paths[0]
            module = __import__("brand_migration")
            real_serialize = module._serialize_manifest_bytes

            def serialize_then_mutate(payload):
                result = real_serialize(payload)
                changed.write_bytes(b"changed during manifest serialization")
                return result

            with patch(
                "brand_migration._serialize_manifest_bytes",
                side_effect=serialize_then_mutate,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                    create_migration_backup(plan, "serialize-input-drift")

            assert_no_published_or_staged_backup(vault, "serialize-input-drift")

    def test_manifest_serialization_observed_drift_prevents_publication(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            observed = Path(vault) / "00-Rules" / "observed.md"
            write_text(observed, "# Observed\n")
            plan = build_migration_plan(vault)
            self.assertNotIn(observed.resolve(), plan.backup_paths)
            module = __import__("brand_migration")
            real_serialize = module._serialize_manifest_bytes

            def serialize_then_mutate(payload):
                result = real_serialize(payload)
                observed.write_text("# Changed\n", encoding="utf-8")
                return result

            with patch(
                "brand_migration._serialize_manifest_bytes",
                side_effect=serialize_then_mutate,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                    create_migration_backup(plan, "serialize-observed-drift")

            assert_no_published_or_staged_backup(vault, "serialize-observed-drift")

    def test_manifest_serialization_backup_tamper_prevents_publication(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            module = __import__("brand_migration")
            real_serialize = module._serialize_manifest_bytes

            def serialize_then_tamper(payload):
                result = real_serialize(payload)
                staging = find_staging_root(vault)
                backup = staging / payload["files"][0]["backup"]
                backup.write_bytes(b"tampered during serialization")
                return result

            with patch(
                "brand_migration._serialize_manifest_bytes",
                side_effect=serialize_then_tamper,
            ):
                with self.assertRaisesRegex(RuntimeError, "backup hash changed"):
                    create_migration_backup(plan, "serialize-backup-tamper")

            assert_no_published_or_staged_backup(vault, "serialize-backup-tamper")

    def test_staging_hard_link_to_source_is_rejected_before_sealing(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            module = __import__("brand_migration")
            real_serialize = module._serialize_manifest_bytes
            source_before = {
                path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                for path in plan.backup_paths
            }
            aliased_source = None

            def serialize_after_aliasing_payload(payload):
                nonlocal aliased_source
                item = payload["files"][0]
                aliased_source = (
                    plan.vault / item["path"]
                    if item["kind"] == "vault"
                    else Path(item["path"])
                )
                backup = find_staging_root(vault) / item["backup"]
                backup.unlink()
                os.link(aliased_source, backup)
                self.assertEqual(
                    (backup.stat().st_dev, backup.stat().st_ino),
                    (aliased_source.stat().st_dev, aliased_source.stat().st_ino),
                )
                return real_serialize(payload)

            with patch(
                "brand_migration._serialize_manifest_bytes",
                side_effect=serialize_after_aliasing_payload,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "staging hard-link alias.*frozen input",
                ):
                    create_migration_backup(plan, "source-hard-link-alias")

            self.assertIsNotNone(aliased_source)
            self.assertEqual(
                {
                    path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
                    for path in plan.backup_paths
                },
                source_before,
            )
            self.assertEqual(aliased_source.stat().st_nlink, 1)
            assert_no_published_or_staged_backup(vault, "source-hard-link-alias")

    def test_manifest_temp_inode_swap_prevents_publication(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            real_rename = os.rename

            def swap_then_rename(source, destination, *args, **kwargs):
                if source == "manifest.json.tmp":
                    temp = find_staging_root(vault) / source
                    temp.unlink()
                    temp.write_text("{}\n", encoding="utf-8")
                return real_rename(source, destination, *args, **kwargs)

            with patch("brand_migration.os.rename", side_effect=swap_then_rename):
                with self.assertRaisesRegex(RuntimeError, "manifest.*replaced"):
                    create_migration_backup(plan, "manifest-temp-swap")

            assert_no_published_or_staged_backup(vault, "manifest-temp-swap")

    def test_manifest_same_inode_modification_after_internal_rename_is_rejected(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            real_rename = os.rename

            def rename_then_modify(source, destination, *args, **kwargs):
                result = real_rename(source, destination, *args, **kwargs)
                if destination == "manifest.json":
                    manifest = find_staging_root(vault) / destination
                    inode = manifest.stat().st_ino
                    with open(manifest, "r+b") as handle:
                        handle.seek(0)
                        handle.write(b"CORRUPTED")
                    self.assertEqual(manifest.stat().st_ino, inode)
                return result

            with patch("brand_migration.os.rename", side_effect=rename_then_modify):
                with self.assertRaisesRegex(RuntimeError, "manifest digest changed"):
                    create_migration_backup(plan, "manifest-same-inode-modify")

            assert_no_published_or_staged_backup(
                vault, "manifest-same-inode-modify"
            )

    def test_manifest_same_inode_truncation_at_final_publication_is_rejected(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            real_publish = __import__("brand_migration")._publish_staging

            def truncate_then_publish(source, destination, *args, **kwargs):
                manifest = find_staging_root(vault) / "manifest.json"
                inode = manifest.stat().st_ino
                manifest.chmod(0o600)
                with open(manifest, "wb"):
                    pass
                self.assertEqual(manifest.stat().st_ino, inode)
                return real_publish(source, destination, *args, **kwargs)

            with patch(
                "brand_migration._publish_staging",
                side_effect=truncate_then_publish,
            ):
                with self.assertRaisesRegex(RuntimeError, "manifest digest changed"):
                    create_migration_backup(plan, "manifest-same-inode-truncate")

            assert_no_published_or_staged_backup(
                vault, "manifest-same-inode-truncate"
            )

    def test_final_publish_input_drift_removes_published_backup(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            changed = plan.backup_paths[0]
            real_publish = __import__("brand_migration")._publish_staging

            def mutate_then_publish(source, destination, *args, **kwargs):
                if destination == "publish-input-drift":
                    changed.write_bytes(b"changed at publication")
                return real_publish(source, destination, *args, **kwargs)

            with patch(
                "brand_migration._publish_staging",
                side_effect=mutate_then_publish,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                    create_migration_backup(plan, "publish-input-drift")

            assert_no_published_or_staged_backup(vault, "publish-input-drift")

    def test_final_publish_observed_drift_removes_published_backup(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            observed = Path(vault) / "00-Rules" / "observed.md"
            write_text(observed, "# Observed\n")
            plan = build_migration_plan(vault)
            real_publish = __import__("brand_migration")._publish_staging

            def mutate_then_publish(source, destination, *args, **kwargs):
                if destination == "publish-observed-drift":
                    observed.write_text("# Changed\n", encoding="utf-8")
                return real_publish(source, destination, *args, **kwargs)

            with patch(
                "brand_migration._publish_staging",
                side_effect=mutate_then_publish,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed after preflight"):
                    create_migration_backup(plan, "publish-observed-drift")

            assert_no_published_or_staged_backup(vault, "publish-observed-drift")

    def test_final_publish_backup_tamper_removes_published_backup(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            real_publish = __import__("brand_migration")._publish_staging

            def tamper_then_publish(source, destination, *args, **kwargs):
                if destination == "publish-backup-tamper":
                    staging = find_staging_root(vault)
                    backup = next(
                        path
                        for path in staging.rglob("*")
                        if path.is_file() and path.name != "manifest.json"
                    )
                    backup.write_bytes(b"tampered at publication")
                return real_publish(source, destination, *args, **kwargs)

            with patch(
                "brand_migration._publish_staging",
                side_effect=tamper_then_publish,
            ):
                with self.assertRaisesRegex(PermissionError, "Permission denied"):
                    create_migration_backup(plan, "publish-backup-tamper")

            assert_no_published_or_staged_backup(vault, "publish-backup-tamper")

    def test_sealed_payload_rejects_tamper_after_earlier_final_hash(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            module = __import__("brand_migration")
            real_hash = module._hash_backup_file
            final_root = (
                Path(vault).resolve()
                / "04-Feedback"
                / "_rollback"
                / "brand-migration"
                / "sealed-final-race"
            )
            checked = []

            def hash_then_tamper_earlier(staging_fd, relative):
                result = real_hash(staging_fd, relative)
                if final_root.exists():
                    checked.append(Path(relative))
                    if len(checked) == 2:
                        final_root.joinpath(checked[0]).write_bytes(
                            b"late sequential tamper"
                        )
                return result

            with patch(
                "brand_migration._hash_backup_file",
                side_effect=hash_then_tamper_earlier,
            ):
                with self.assertRaisesRegex(PermissionError, "Permission denied"):
                    create_migration_backup(plan, "sealed-final-race")

            assert_no_published_or_staged_backup(vault, "sealed-final-race")

    def test_rollback_swap_after_initial_validation_cannot_redirect_writes(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            real_assert = __import__("brand_migration")._assert_plan_unchanged
            calls = 0

            def validate_then_swap(*args, **kwargs):
                nonlocal calls
                result = real_assert(*args, **kwargs)
                calls += 1
                if calls == 1:
                    swap_rollback_parent(vault, "after-validation")
                return result

            with patch(
                "brand_migration._assert_plan_unchanged",
                side_effect=validate_then_swap,
            ):
                with self.assertRaisesRegex(
                    NotADirectoryError, "Not a directory"
                ):
                    create_migration_backup(plan, "swap-after-validation")

            assert_swap_left_no_payload(vault, "swap-after-validation")

    def test_rollback_swap_after_first_copy_cannot_redirect_writes(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            module = __import__("brand_migration")
            real_copy = module._copy_backup_file
            calls = 0

            def copy_then_swap(*args, **kwargs):
                nonlocal calls
                result = real_copy(*args, **kwargs)
                calls += 1
                if calls == 1:
                    swap_rollback_parent(vault, "after-copy")
                return result

            with patch(
                "brand_migration._copy_backup_file",
                side_effect=copy_then_swap,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "rollback parent.*symlink.*cleanup failed",
                ):
                    create_migration_backup(plan, "swap-after-copy")

            assert_swap_left_no_payload(vault, "swap-after-copy")

    def test_rollback_swap_during_manifest_serialization_cannot_redirect_writes(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            module = __import__("brand_migration")
            real_serialize = module._serialize_manifest_bytes

            def serialize_then_swap(payload):
                result = real_serialize(payload)
                swap_rollback_parent(vault, "during-serialization")
                return result

            with patch(
                "brand_migration._serialize_manifest_bytes",
                side_effect=serialize_then_swap,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "rollback parent.*symlink.*cleanup failed",
                ):
                    create_migration_backup(plan, "swap-during-serialization")

            assert_swap_left_no_payload(vault, "swap-during-serialization")

    def test_rollback_swap_immediately_before_publish_is_removed(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            real_publish = __import__("brand_migration")._publish_staging

            def swap_then_rename(source, destination, *args, **kwargs):
                if destination == "swap-before-publish":
                    swap_rollback_parent(vault, "before-publish")
                return real_publish(source, destination, *args, **kwargs)

            with patch(
                "brand_migration._publish_staging",
                side_effect=swap_then_rename,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "pinned directory path changed.*cleanup failed",
                ):
                    create_migration_backup(plan, "swap-before-publish")

            assert_swap_left_no_payload(vault, "swap-before-publish")

    def test_destination_created_inside_publish_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)
            module = __import__("brand_migration")
            real_publish = module._publish_staging
            destination = Path(vault).resolve() / "04-Feedback" / "_rollback"
            destination = destination / "brand-migration" / "publish-collision"
            attacker_inode = None

            def create_destination_then_publish(source, target, *args, **kwargs):
                nonlocal attacker_inode
                destination.mkdir()
                attacker_inode = destination.stat().st_ino
                return real_publish(source, target, *args, **kwargs)

            with patch(
                "brand_migration._publish_staging",
                side_effect=create_destination_then_publish,
            ):
                with self.assertRaises(FileExistsError):
                    create_migration_backup(plan, "publish-collision")

            self.assertEqual(destination.stat().st_ino, attacker_inode)
            self.assertFalse((destination / "manifest.json").exists())
            self.assertFalse(
                list(
                    Path(vault).resolve().glob(
                        ".agent-memory-beacon-brand-migration-*"
                    )
                )
            )

    def test_backup_root_mode_is_private_and_manifest_freezes_new_contract(self):
        with tempfile.TemporaryDirectory() as vault:
            make_vault(vault)
            plan = build_migration_plan(vault)

            manifest_path = create_migration_backup(plan, "private-mode")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

            self.assertEqual(
                stat.S_IMODE(manifest_path.parent.stat().st_mode),
                0o500,
            )
            for path in manifest_path.parent.rglob("*"):
                expected_mode = 0o500 if path.is_dir() else 0o400
                self.assertEqual(
                    stat.S_IMODE(path.stat().st_mode),
                    expected_mode,
                    path,
                )
            self.assertTrue(manifest["mutable_roots"])
            self.assertEqual(
                len(manifest["mutable_directories_before"]),
                len(plan.mutable_directories_before),
            )
            self.assertEqual(
                len(manifest["configured_target_states"]),
                len(plan.configured_target_states),
            )
            self.assertEqual(
                len(manifest["absent_paths_before"]),
                len(plan.absent_paths_before),
            )
            serialized = manifest["mutation_contract"]
            self.assertEqual(
                [(item["role"], item["key"]) for item in serialized["target_specs"]],
                [(spec.role, spec.key) for spec in plan.mutation_contract.target_specs],
            )
            self.assertEqual(
                len(serialized["mutable_files"]),
                len(plan.mutation_contract.mutable_files),
            )
            self.assertEqual(
                len(serialized["absent_directories"]),
                len(plan.mutation_contract.absent_directories),
            )
            self.assertEqual(
                [
                    (
                        item["path"],
                        item["sha256"],
                        tuple(item["inode"]),
                        item["link_count"],
                        item["expected_type"],
                    )
                    for item in manifest["input_bindings"]
                ],
                [
                    (
                        (
                            binding.path.relative_to(plan.vault).as_posix()
                            if binding.path.is_relative_to(plan.vault)
                            else str(binding.path)
                        ),
                        binding.sha256,
                        binding.inode,
                        binding.link_count,
                        binding.expected_type,
                    )
                    for binding in plan.input_bindings
                ],
            )


def write_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def assert_failed_backup_absent(vault, migration_id):
    root = Path(vault).resolve().joinpath(
        "04-Feedback",
        "_rollback",
        "brand-migration",
        migration_id,
    )
    if root.exists():
        raise AssertionError(f"partial backup still exists: {root}")
    if root.joinpath("manifest.json").exists():
        raise AssertionError(f"trusted manifest was published: {root}")


def find_staging_root(vault):
    roots = list(
        Path(vault).resolve().glob(".agent-memory-beacon-brand-migration-*")
    )
    if len(roots) != 1:
        raise AssertionError(f"expected one staging root, found {roots}")
    return roots[0]


def assert_no_published_or_staged_backup(vault, migration_id):
    assert_failed_backup_absent(vault, migration_id)
    staging = list(
        Path(vault).resolve().glob(".agent-memory-beacon-brand-migration-*")
    )
    if staging:
        raise AssertionError(f"staging backup still exists: {staging}")


def swap_rollback_parent(vault, label):
    vault = Path(vault).resolve()
    feedback = vault / "04-Feedback"
    rollback = feedback / "_rollback"
    attacker = vault / f"attacker-{label}"
    attacker.mkdir(parents=True, exist_ok=True)
    if rollback.is_symlink():
        return attacker
    if rollback.exists():
        displaced = feedback / f"_rollback-displaced-{label}"
        rollback.rename(displaced)
    else:
        feedback.mkdir(parents=True, exist_ok=True)
    rollback.symlink_to(attacker, target_is_directory=True)
    return attacker


def assert_swap_left_no_payload(vault, migration_id):
    vault = Path(vault).resolve()
    for attacker in vault.glob("attacker-*"):
        if any(path.is_file() for path in attacker.rglob("*")):
            raise AssertionError(f"payload redirected to attacker path: {attacker}")
    for displaced in (vault / "04-Feedback").glob("_rollback-displaced-*"):
        if displaced.joinpath("brand-migration", migration_id).exists():
            raise AssertionError(f"trusted backup remained under displaced parent: {displaced}")
    assert_no_published_or_staged_backup(vault, migration_id)


def make_vault(vault):
    vault = Path(vault)
    old = "github-obsidian-knowledge-brain"
    source = vault / "01-Projects" / old
    memory = source / "Memory"
    write_text(
        memory / "decisions.md",
        f"""---
project: {old}
decisions:
- id: decision-1
  text: Keep Obsidian as primary storage
  context: User-owned Markdown is auditable
---

# Decisions
""",
    )
    write_text(
        memory / "pitfalls.md",
        f"""---
project: {old}
pitfalls:
- id: error-1
  type: path-filesystem
  resolution: Validate the Vault path before writing
---

# Pitfalls
""",
    )
    write_text(
        memory / "sessions" / "2026-07-12-brand-test.md",
        f"""---
session_id: brand-test-session
date: 2026-07-12
project: {old}
projects: [{old}]
decisions_made:
- id: session-decision-1
  text: Use a reversible migration
errors_encountered: []
---

# Brand migration test
""",
    )
    write_text(
        vault / "03-Maps" / "project-graph.md",
        f"[[01-Projects/{old}/Memory/decisions|Old project decisions]]\n",
    )
    write_text(
        vault / "05-Agent-Memory" / "personal-memory.md",
        f"""---
title: Personal Memory
---

- project: [[01-Projects/{old}/Memory/decisions|{old}]]
""",
    )
    return source.resolve()


def make_structural_rule(vault):
    old = "github-obsidian-knowledge-brain"
    path = Path(vault).resolve() / "00-Rules" / "structural-reference.md"
    write_text(
        path,
        f"[[01-Projects/{old}/Memory/decisions|Project decisions]]\n",
    )
    return path


def snapshot_tree(root):
    root = Path(root)
    result = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        result[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def snapshot_public_tree(root):
    return {
        path: digest
        for path, digest in snapshot_tree(root).items()
        if not path.startswith("04-Feedback/_rollback/brand-migration/")
    }


def snapshot_directories(root):
    root = Path(root)
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_dir()
        and not path.is_relative_to(
            root / "vault" / "04-Feedback" / "_rollback" / "brand-migration"
        )
    }


def directory_metadata(path):
    current = os.stat(path, follow_symlinks=False)
    return (
        stat.S_IMODE(current.st_mode),
        current.st_atime_ns,
        current.st_mtime_ns,
    )


def read_checkpoint_records(manifest):
    manifest = Path(manifest)
    journal = manifest.parent / "journal"
    prefix = "checkpoint-"
    records = []
    for directory in sorted(journal.glob(prefix + "*")):
        if directory.name.startswith("checkpoint-tmp-"):
            continue
        suffix = directory.name[len(prefix):]
        sequence_text, digest = suffix.split("-", 1)
        raw = (directory / "record.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            raise AssertionError(f"checkpoint digest mismatch: {directory}")
        records.append(
            (
                int(sequence_text),
                digest,
                json.loads(raw.decode("utf-8")),
                directory,
            )
        )
    return records


def checkpoint_record_path(vault, item):
    if item["kind"] == "vault":
        return Path(vault) / item["path"]
    return Path(item["path"])


def reconstruct_checkpoint_application(records):
    application = None
    for sequence, _digest, record, _directory in records:
        if sequence == 1:
            application = record["snapshot"]
        else:
            current = {
                key: {
                    (item["kind"], item["path"]): item
                    for item in application[key]
                }
                for key in (
                    "created_files",
                    "created_directories",
                    "post_bindings",
                    "post_directories",
                )
            }
            for key, change in record.get("delta", {}).items():
                for item in change.get("remove", []):
                    current[key].pop((item["kind"], item["path"]))
                for item in change.get("upsert", []):
                    current[key][(item["kind"], item["path"])] = item
            application = {
                key: [current[key][item] for item in sorted(current[key])]
                for key in current
            }
    return application


def append_forged_checkpoint(
    journal,
    template,
    sequence,
    previous_digest,
    previous_binding,
    phase,
    boundary,
    status,
    delta,
):
    record = {
        key: value
        for key, value in template.items()
        if key not in {"snapshot", "delta", "validation"}
    }
    record.update(
        {
            "sequence": sequence,
            "previous_record_sha256": previous_digest,
            "previous_record_binding": previous_binding,
            "status": status,
            "phase": phase,
            "boundary": boundary,
            "recorded_at": f"2026-07-12T00:00:{sequence:02d}+00:00",
            "delta": delta,
        }
    )
    raw = (
        json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    directory = journal / f"checkpoint-{sequence:08d}-{digest}"
    directory.mkdir(mode=0o700)
    (directory / "record.json").write_bytes(raw)
    os.chmod(directory / "record.json", 0o400)
    os.chmod(directory, 0o500)
    return digest, checkpoint_test_record_binding(directory)


def checkpoint_test_stat_identity(current):
    return [
        current.st_dev,
        current.st_ino,
        current.st_nlink,
        current.st_mode,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    ]


def checkpoint_test_record_binding(directory):
    raw = (directory / "record.json").read_bytes()
    return {
        "directory_stat": checkpoint_test_stat_identity(
            os.stat(directory, follow_symlinks=False)
        ),
        "record_stat": checkpoint_test_stat_identity(
            os.stat(directory / "record.json", follow_symlinks=False)
        ),
        "digest": hashlib.sha256(raw).hexdigest(),
    }


def file_metadata(path):
    current = os.stat(path, follow_symlinks=False)
    return (
        stat.S_IMODE(current.st_mode),
        current.st_mtime_ns,
    )


def _is_migration_audit_relative(relative):
    parts = Path(relative).parts
    for index in range(len(parts) - 2):
        if parts[index:index + 3] == (
            "04-Feedback",
            "_rollback",
            "brand-migration",
        ):
            return True
    return False


def snapshot_without_migration_audit(root):
    return {
        path: digest
        for path, digest in snapshot_tree(root).items()
        if not _is_migration_audit_relative(path)
    }


def snapshot_directories_without_migration_audit(root):
    return {
        path
        for path in snapshot_directories(root)
        if not _is_migration_audit_relative(path)
        and not path.endswith("04-Feedback")
        and not path.endswith("04-Feedback/_rollback")
        and not path.endswith("04-Feedback/_logs")
    }
