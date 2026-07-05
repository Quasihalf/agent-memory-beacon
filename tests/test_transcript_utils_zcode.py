import json
import os
import sqlite3
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from transcript_utils import (
    find_recent_transcripts,
    get_transcript_roots,
    iter_transcript_files,
    parse_transcript,
    session_id_from_path,
)


class ZCodeTranscriptTests(unittest.TestCase):
    def test_zcode_agent_adds_default_sqlite_root(self):
        cfg = {"agent": "zcode", "zcode_home": "/tmp/example-zcode"}

        roots = get_transcript_roots(cfg)

        self.assertIn("/tmp/example-zcode/cli/db/db.sqlite", roots)

    def test_iter_transcript_files_yields_one_locator_per_zcode_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "db.sqlite")
            write_zcode_db(db_path, [
                ("sess-a", "第一个", 1_800_000_000_000),
                ("sess-b", "第二个", 1_800_000_010_000),
            ])

            locators = list(iter_transcript_files([db_path]))

            self.assertEqual(len(locators), 2)
            self.assertTrue(all(locator.startswith(db_path + "::") for locator in locators))
            self.assertEqual(session_id_from_path(locators[0]), "sess-b")
            self.assertEqual(session_id_from_path(locators[1]), "sess-a")

    def test_parse_transcript_reads_zcode_sqlite_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "db.sqlite")
            write_zcode_db(db_path, [("sess-a", "测试会话", 1_800_000_000_000)])
            add_zcode_message(
                db_path,
                "sess-a",
                "msg-user",
                "user",
                [
                    {"type": "text", "text": "请记录这个决定"},
                ],
            )
            add_zcode_message(
                db_path,
                "sess-a",
                "msg-assistant",
                "assistant",
                [
                    {"type": "reasoning", "text": "内部推理不应进入 transcript"},
                    {
                        "type": "text",
                        "text": "[DECISION:适配 zcode SQLite 会话| context:ZCode 正文在 message/part 表中]",
                    },
                    {"type": "tool", "tool": "Bash", "state": {"output": "ignore"}},
                ],
            )

            parsed = parse_transcript(db_path + "::sess-a")

            self.assertEqual(parsed["meta"]["agent"], "zcode")
            self.assertEqual(parsed["meta"]["session_id"], "sess-a")
            self.assertEqual(parsed["meta"]["cwd"], "/tmp/zcode-project")
            self.assertEqual(parsed["meta"]["date"], "2027-01-15")
            self.assertEqual([m["role"] for m in parsed["messages"]], ["user", "assistant"])
            self.assertIn("请记录这个决定", parsed["text"])
            self.assertIn("[DECISION:适配 zcode SQLite 会话", parsed["text"])
            self.assertNotIn("内部推理", parsed["text"])
            self.assertNotIn("Bash", parsed["text"])

    def test_find_recent_transcripts_filters_zcode_sessions_by_processed_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "db.sqlite")
            now_ms = 1_800_000_000_000
            write_zcode_db(db_path, [
                ("sess-old", "旧会话", now_ms - 10 * 3600 * 1000),
                ("sess-new", "新会话", now_ms),
            ])

            with patched_now(now_ms / 1000):
                recent = find_recent_transcripts(
                    {"agent": "zcode", "zcode_db_path": db_path},
                    processed_ids={"sess-new"},
                    hours=24,
                )

            self.assertEqual(recent, [db_path + "::sess-old"])


def write_zcode_db(db_path, sessions):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path)
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
    for session_id, title, updated in sessions:
        conn.execute(
            "insert into session(id, directory, title, time_created, time_updated) values (?, ?, ?, ?, ?)",
            (session_id, "/tmp/zcode-project", title, updated, updated),
        )
    conn.commit()
    conn.close()


def add_zcode_message(db_path, session_id, message_id, role, parts):
    conn = sqlite3.connect(db_path)
    time_created = 1_800_000_000_000 + len(parts)
    conn.execute(
        "insert into message(id, session_id, time_created, time_updated, data) values (?, ?, ?, ?, ?)",
        (
            message_id,
            session_id,
            time_created,
            time_created,
            json.dumps({"role": role, "agent": "zcode-agent"}),
        ),
    )
    for index, part in enumerate(parts):
        conn.execute(
            "insert into part(id, message_id, session_id, time_created, time_updated, data) values (?, ?, ?, ?, ?, ?)",
            (
                f"{message_id}-part-{index}",
                message_id,
                session_id,
                time_created + index,
                time_created + index,
                json.dumps(part, ensure_ascii=False),
            ),
        )
    conn.commit()
    conn.close()


class patched_now:
    def __init__(self, timestamp):
        self.timestamp = timestamp
        self.original = None

    def __enter__(self):
        import transcript_utils

        class FakeDatetime:
            @classmethod
            def now(cls):
                class FakeNow:
                    def timestamp(inner_self):
                        return self.timestamp

                return FakeNow()

            @classmethod
            def fromtimestamp(cls, *args, **kwargs):
                return self.original.fromtimestamp(*args, **kwargs)

        self.original = transcript_utils.datetime
        transcript_utils.datetime = FakeDatetime

    def __exit__(self, exc_type, exc, tb):
        import transcript_utils

        transcript_utils.datetime = self.original


if __name__ == "__main__":
    unittest.main()
