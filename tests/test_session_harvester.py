import glob
import os
import sqlite3
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from session_harvester import find_transcript, repair_generated_vault_markdown, write_session_to_vault


class SessionHarvesterTests(unittest.TestCase):
    def test_late_summary_is_added_to_existing_session(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}

            write_session_to_vault(
                cfg,
                "sess-1",
                "2026-07-04",
                "proj",
                {},
                [{"text": "决定A", "context": "原因A"}],
                [],
                None,
            )
            result = write_session_to_vault(
                cfg,
                "sess-1",
                "2026-07-04",
                "proj",
                {},
                [{"text": "决定A", "context": "原因A"}],
                [],
                "这是后来的总结",
            )

            path = glob.glob(
                os.path.join(vault, "01-Projects/proj/Memory/sessions/*.md")
            )[0]
            content = read_text(path)
            self.assertEqual(result, 1)
            self.assertIn("这是后来的总结", content)

    def test_existing_summary_is_preserved_when_new_error_is_added(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}

            write_session_to_vault(
                cfg,
                "sess-1",
                "2026-07-04",
                "proj",
                {},
                [{"text": "决定A", "context": "原因A"}],
                [],
                "已有总结",
            )
            write_session_to_vault(
                cfg,
                "sess-1",
                "2026-07-04",
                "proj",
                {},
                [{"text": "决定A", "context": "原因A"}],
                [{"type": "path-filesystem", "resolution": "修复路径"}],
                None,
            )

            path = glob.glob(
                os.path.join(vault, "01-Projects/proj/Memory/sessions/*.md")
            )[0]
            content = read_text(path)
            self.assertIn("已有总结", content)
            self.assertIn("path-filesystem", content)

    def test_markdown_repair_does_not_rewrite_manual_project_notes(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}
            manual_path = os.path.join(vault, "01-Projects/proj/manual.md")
            generated_path = os.path.join(
                vault, "01-Projects/proj/Memory/sessions/2026-07-04-demo.md"
            )
            os.makedirs(os.path.dirname(manual_path), exist_ok=True)
            os.makedirs(os.path.dirname(generated_path), exist_ok=True)
            manual_content = "手写笔记 /Users/a0000/ObsidianBrain/foo.md\n"
            generated_content = (
                "---\n"
                "harvested_by: session_harvester.py\n"
                "---\n\n"
                "生成笔记 /Users/a0000/ObsidianBrain/foo.md\n"
            )
            write_text(manual_path, manual_content)
            write_text(generated_path, generated_content)

            changed = repair_generated_vault_markdown(cfg)

            self.assertEqual(read_text(manual_path), manual_content)
            self.assertIn("`/Users/a0000/ObsidianBrain/foo.md`", read_text(generated_path))
            self.assertEqual(changed, 1)

    def test_find_transcript_uses_zcode_session_env_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "db.sqlite")
            write_minimal_zcode_db(db_path, "sess-z")
            old_db = os.environ.get("ZCODE_SESSION_DB")
            old_session = os.environ.get("ZCODE_SESSION_ID")
            os.environ["ZCODE_SESSION_DB"] = db_path
            os.environ["ZCODE_SESSION_ID"] = "sess-z"
            try:
                self.assertEqual(find_transcript({}), db_path + "::sess-z")
            finally:
                restore_env("ZCODE_SESSION_DB", old_db)
                restore_env("ZCODE_SESSION_ID", old_session)


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write_text(path, content):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def write_minimal_zcode_db(db_path, session_id):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "create table session (id text primary key, time_updated integer not null)"
    )
    conn.execute("insert into session(id, time_updated) values (?, ?)", (session_id, 1))
    conn.commit()
    conn.close()


def restore_env(name, value):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
