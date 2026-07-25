import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from backup import run, source_fingerprint, sync_to_nutstore_atomic


class BackupTests(unittest.TestCase):
    def test_unchanged_transcript_is_not_reparsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcript = os.path.join(tmp, "session.jsonl")
            feedback = os.path.join(vault, "04-Feedback")
            os.makedirs(feedback)
            write_jsonl(transcript, [{"type": "user"}])
            fingerprint = source_fingerprint(transcript)
            with open(os.path.join(feedback, "heartbeat.md"), "w", encoding="utf-8") as handle:
                handle.write(
                    "---\nprocessed_sessions:\n"
                    f"  session: {fingerprint}\n"
                    "---\n"
                )
            cfg = {
                "vault_path": vault,
                "agent": "codex",
                "transcript_paths": [transcript],
                "codex_home": os.path.join(tmp, "missing-codex"),
                "codex_sessions_path": os.path.join(tmp, "missing-sessions"),
            }

            with patch("backup.parse_transcript") as parse:
                result = run(cfg)

            self.assertEqual(result["new_sessions"], 0)
            parse.assert_not_called()

    def test_full_scan_reparses_unchanged_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcript = os.path.join(tmp, "session.jsonl")
            feedback = os.path.join(vault, "04-Feedback")
            os.makedirs(feedback)
            write_jsonl(transcript, [{"type": "user"}])
            fingerprint = source_fingerprint(transcript)
            with open(os.path.join(feedback, "heartbeat.md"), "w", encoding="utf-8") as handle:
                handle.write(
                    "---\nprocessed_sessions:\n"
                    f"  session: {fingerprint}\n"
                    "---\n"
                )
            cfg = {
                "vault_path": vault,
                "agent": "codex",
                "transcript_paths": [transcript],
                "codex_home": os.path.join(tmp, "missing-codex"),
                "codex_sessions_path": os.path.join(tmp, "missing-sessions"),
            }

            with patch(
                "backup.parse_transcript",
                return_value={"meta": {}, "messages": []},
            ) as parse:
                result = run(cfg, full=True)

            self.assertEqual(result["new_sessions"], 1)
            parse.assert_called_once_with(transcript)

    def test_backup_destination_cannot_overlap_vault(self):
        with tempfile.TemporaryDirectory() as vault:
            marker = os.path.join(vault, "01-Projects", "keep.md")
            os.makedirs(os.path.dirname(marker))
            with open(marker, "w", encoding="utf-8") as handle:
                handle.write("keep")

            with self.assertRaises(ValueError):
                sync_to_nutstore_atomic(vault, vault)

            self.assertTrue(os.path.exists(marker))

    def test_backup_includes_agent_memory_and_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            backup = os.path.join(tmp, "backup")
            memory_dir = os.path.join(vault, "05-Agent-Memory")
            os.makedirs(memory_dir)
            write_text(os.path.join(memory_dir, "personal-memory.md"), "remember")
            secret = os.path.join(tmp, "outside-secret.txt")
            write_text(secret, "must-not-copy")
            os.symlink(secret, os.path.join(memory_dir, "outside-link.txt"))

            sync_to_nutstore_atomic(vault, backup)

            self.assertEqual(
                read_text(os.path.join(backup, "05-Agent-Memory", "personal-memory.md")),
                "remember",
            )
            self.assertFalse(
                os.path.lexists(
                    os.path.join(backup, "05-Agent-Memory", "outside-link.txt")
                )
            )

    def test_backup_restores_previous_copy_when_final_swap_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            backup = os.path.join(tmp, "backup")
            os.makedirs(os.path.join(vault, "01-Projects"))
            os.makedirs(backup)
            write_text(os.path.join(vault, "01-Projects", "new.md"), "new")
            old_marker = os.path.join(backup, "old.md")
            write_text(old_marker, "old")
            real_rename = os.rename

            def fail_new_backup(source, destination):
                if str(source).endswith(".tmp"):
                    raise OSError("simulated final swap failure")
                return real_rename(source, destination)

            with patch("backup.os.rename", side_effect=fail_new_backup):
                with self.assertRaisesRegex(OSError, "simulated"):
                    sync_to_nutstore_atomic(vault, backup)

            self.assertEqual(read_text(old_marker), "old")

    def test_default_backup_tracks_transcript_without_storing_raw_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcript = os.path.join(tmp, "session.jsonl")
            os.makedirs(vault)
            write_jsonl(
                transcript,
                [
                    {
                        "type": "user",
                        "message": {
                            "role": "user",
                            "content": "password=super-secret-value",
                        },
                    }
                ],
            )
            cfg = {
                "vault_path": vault,
                "agent": "codex",
                "transcript_paths": [transcript],
                "codex_home": os.path.join(tmp, "missing-codex"),
                "codex_sessions_path": os.path.join(tmp, "missing-sessions"),
            }

            result = run(cfg)

            raw_dir = os.path.join(vault, "04-Feedback", "_raw-sessions")
            self.assertEqual(result["new_sessions"], 1)
            self.assertFalse(
                any(name.endswith(".jsonl") for name in os.listdir(raw_dir))
            )
            all_text = "".join(
                read_text(os.path.join(raw_dir, name))
                for name in os.listdir(raw_dir)
            )
            self.assertNotIn("super-secret-value", all_text)

    def test_codex_subagent_is_skipped_from_backup_by_parsed_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcript = os.path.join(tmp, "ordinary-session-name.jsonl")
            os.makedirs(vault)
            write_jsonl(
                transcript,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "child-session",
                            "source": {"subagent": {"thread_spawn": {}}},
                            "thread_source": "subagent",
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "内部审查提示词不应进入备份统计",
                        },
                    },
                ],
            )
            cfg = {
                "vault_path": vault,
                "agent": "codex",
                "transcript_paths": [transcript],
                "codex_home": os.path.join(tmp, "missing-codex"),
                "codex_sessions_path": os.path.join(tmp, "missing-sessions"),
            }

            result = run(cfg)

            self.assertEqual(result["new_sessions"], 0)
            self.assertEqual(result["skipped_agent_sessions"], 1)
            self.assertIn("ordinary-session-name", result["processed_ids"])
            self.assertFalse(
                os.path.exists(os.path.join(vault, "04-Feedback", "_raw-sessions"))
            )

    def test_zcode_locator_is_tracked_without_treating_it_as_a_file_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            db_path = os.path.join(tmp, "db.sqlite")
            os.makedirs(vault)
            write_zcode_db(db_path)
            cfg = {
                "vault_path": vault,
                "agent": "zcode",
                "transcript_agents": ["zcode"],
                "zcode_db_path": db_path,
                "zcode_home": tmp,
            }

            result = run(cfg)

            self.assertEqual(result["new_sessions"], 1)
            self.assertIn("z-session", result["processed_ids"])


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            json.dump(record, handle, ensure_ascii=False)
            handle.write("\n")


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def write_zcode_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table session (
            id text primary key,
            directory text not null,
            title text not null,
            time_created integer not null,
            time_updated integer not null
        );
        create table message (
            id text primary key,
            session_id text not null,
            time_created integer not null,
            time_updated integer not null,
            data text not null
        );
        create table part (
            id text primary key,
            message_id text not null,
            session_id text not null,
            time_created integer not null,
            time_updated integer not null,
            data text not null
        );
        """
    )
    now = 1_800_000_000_000
    conn.execute(
        "insert into session values (?, ?, ?, ?, ?)",
        ("z-session", "/tmp/project", "ZCode test", now, now),
    )
    conn.execute(
        "insert into message values (?, ?, ?, ?, ?)",
        ("z-message", "z-session", now, now, json.dumps({"role": "user"})),
    )
    conn.execute(
        "insert into part values (?, ?, ?, ?, ?, ?)",
        (
            "z-part",
            "z-message",
            "z-session",
            now,
            now,
            json.dumps({"type": "text", "text": "测试正文"}, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()


if __name__ == "__main__":
    unittest.main()
