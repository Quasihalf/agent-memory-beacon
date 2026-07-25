import os
import re
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import safety

from safety import (
    safe_vault_path,
    split_frontmatter_text,
    strip_platform_injected_context,
)


class SafetyTests(unittest.TestCase):
    def test_frontmatter_parser_preserves_inline_delimiter_text(self):
        content = (
            "---\n"
            "title: Example\n"
            "---\n"
            "正文中的参数 a---b 不能截断。\n"
            "\n"
            "---\n"
            "后续分隔线也属于正文。\n"
        )

        frontmatter, body = split_frontmatter_text(content)

        self.assertEqual(frontmatter, "title: Example")
        self.assertEqual(
            body,
            "正文中的参数 a---b 不能截断。\n\n---\n后续分隔线也属于正文。\n",
        )

    def test_production_scripts_do_not_split_frontmatter_as_plain_text(self):
        unsafe = []
        split_pattern = re.compile(r"\.split\(\s*(['\"])---\1")
        for filename in sorted(os.listdir(SCRIPTS_DIR)):
            if not filename.endswith(".py"):
                continue
            path = os.path.join(SCRIPTS_DIR, filename)
            with open(path, "r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if split_pattern.search(line):
                        unsafe.append(f"{filename}:{line_number}")

        self.assertEqual(
            unsafe,
            [],
            "frontmatter must use safety.split_frontmatter_text: "
            + ", ".join(unsafe),
        )

    def test_subagent_notifications_are_removed_from_user_context(self):
        text = (
            "真实用户要求。\n"
            "<subagent_notification>{\"status\":{\"completed\":\"用 $AppStorage\"}}"
            "</subagent_notification>\n"
            "<subagent_notification>未闭合的平台尾部"
        )

        cleaned = strip_platform_injected_context(text)

        self.assertEqual(cleaned, "真实用户要求。")

    def test_empty_vault_path_is_rejected(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "empty"):
                    safe_vault_path(value, "04-Feedback", "heartbeat.md")

    def test_durable_atomic_write_rejects_destination_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            outside = os.path.join(root, "outside.md")
            target = os.path.join(root, "target.md")
            with open(outside, "w", encoding="utf-8") as handle:
                handle.write("keep\n")
            os.symlink(outside, target)

            with self.assertRaisesRegex(OSError, "symlink"):
                safety.durable_atomic_write(target, "replace\n")

            with open(outside, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "keep\n")
            self.assertTrue(os.path.islink(target))

    def test_durable_atomic_write_ignores_predictable_temp_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            outside = os.path.join(root, "outside.md")
            target = os.path.join(root, "target.md")
            with open(outside, "w", encoding="utf-8") as handle:
                handle.write("keep\n")
            os.symlink(outside, target + ".tmp")

            safety.durable_atomic_write(target, "published\n")

            with open(outside, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "keep\n")
            with open(target, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "published\n")
            self.assertFalse(os.path.islink(target))

    def test_durable_atomic_write_preserves_existing_mode_despite_umask(self):
        with tempfile.TemporaryDirectory() as root:
            target = os.path.join(root, "target.md")
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("before\n")
            os.chmod(target, 0o664)

            previous_umask = os.umask(0o077)
            try:
                safety.durable_atomic_write(target, "after\n")
            finally:
                os.umask(previous_umask)

            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o664)
            with open(target, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "after\n")

    def test_vault_root_pinning_rejects_intermediate_symlink_swap(self):
        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            feedback = os.path.join(vault, "04-Feedback")
            candidate_dir = os.path.join(feedback, "_error-candidates")
            os.makedirs(candidate_dir)
            target = safe_vault_path(
                vault,
                "04-Feedback",
                "_error-candidates",
                "state.md",
            )
            held = feedback + "-held"
            os.rename(feedback, held)
            outside_feedback = os.path.join(root, "outside", "04-Feedback")
            outside_candidates = os.path.join(
                outside_feedback,
                "_error-candidates",
            )
            os.makedirs(outside_candidates)
            outside_target = os.path.join(outside_candidates, "state.md")
            with open(outside_target, "w", encoding="utf-8") as handle:
                handle.write("keep-outside\n")
            os.symlink(outside_feedback, feedback)

            with self.assertRaises(OSError):
                safety.durable_atomic_write(
                    target,
                    "must-not-escape\n",
                    root=vault,
                )

            with open(outside_target, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "keep-outside\n")
            with self.assertRaises(OSError):
                safety.secure_read_bytes(target, 100, root=vault)
            with self.assertRaises(OSError):
                safety.durable_unlink(target, root=vault)
            lock_path = os.path.join(candidate_dir, "state.lock")
            with self.assertRaises(OSError):
                with safety.exclusive_file_lock(lock_path, root=vault):
                    self.fail("symlink-swapped lock must not be acquired")
            self.assertFalse(os.path.exists(os.path.join(outside_candidates, "state.lock")))
            with open(outside_target, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "keep-outside\n")

    def test_exclusive_lock_retries_when_named_inode_changes_while_waiting(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = os.path.join(root, "state.lock")
            with open(lock_path, "w", encoding="utf-8"):
                pass
            exclusive_calls = 0

            def replace_on_first_lock(_descriptor, operation):
                nonlocal exclusive_calls
                if operation != safety.fcntl.LOCK_EX:
                    return
                exclusive_calls += 1
                if exclusive_calls == 1:
                    os.unlink(lock_path)
                    with open(lock_path, "w", encoding="utf-8"):
                        pass

            with patch.object(safety.fcntl, "flock", side_effect=replace_on_first_lock):
                with safety.exclusive_file_lock(lock_path):
                    pass

            self.assertEqual(exclusive_calls, 2)

    def test_exclusive_lock_serializes_threads_across_realpath_aliases(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = os.path.join(root, "state.lock")
            canonical_lock_path = os.path.realpath(lock_path)
            first_entered = threading.Event()
            second_attempting = threading.Event()
            second_entered = threading.Event()
            release_first = threading.Event()
            failures = []

            def hold_first_lock():
                try:
                    with safety.exclusive_file_lock(lock_path):
                        first_entered.set()
                        if not release_first.wait(timeout=5):
                            raise TimeoutError("test did not release first lock")
                except Exception as exc:
                    failures.append(exc)

            def enter_second_lock():
                try:
                    second_attempting.set()
                    with safety.exclusive_file_lock(canonical_lock_path):
                        second_entered.set()
                except Exception as exc:
                    failures.append(exc)

            with patch.object(safety.fcntl, "flock", return_value=None):
                first = threading.Thread(target=hold_first_lock)
                second = threading.Thread(target=enter_second_lock)
                first.start()
                self.assertTrue(first_entered.wait(timeout=5))
                second.start()
                self.assertTrue(second_attempting.wait(timeout=5))
                entered_while_first_held = second_entered.wait(timeout=0.2)
                release_first.set()
                first.join(timeout=5)
                second.join(timeout=5)

            self.assertFalse(entered_while_first_held)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertTrue(second_entered.is_set())
            self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
