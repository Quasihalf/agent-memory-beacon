import os
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import reporter
from reporter import rebuild_maps, update_heartbeat
from session_harvester import mark_transcript_harvested, transcript_state_key


class ReporterTests(unittest.TestCase):
    def test_rebuild_maps_routes_migration_writes_through_mutation_io(self):
        with tempfile.TemporaryDirectory() as vault:
            session = os.path.join(
                vault,
                "01-Projects/demo/Memory/sessions/2026-07-10-demo.md",
            )
            os.makedirs(os.path.dirname(session), exist_ok=True)
            os.makedirs(os.path.join(vault, "03-Maps"), exist_ok=True)
            write_text(
                session,
                "---\nsession_id: demo\ndate: '2026-07-10'\ntags: []\n---\n\n# Demo\n",
            )

            class RecordingIO:
                def __init__(self):
                    self.writes = []

                def atomic_write(self, path, content, encoding="utf-8"):
                    self.writes.append(os.fspath(path))
                    write_text(path, content)

            mutation_io = RecordingIO()
            rebuild_maps(vault, mutation_io=mutation_io)

            self.assertEqual(
                mutation_io.writes,
                [
                    os.path.join(vault, "03-Maps", "topic-index.md"),
                    os.path.join(vault, "03-Maps", "timeline.md"),
                ],
            )

    def test_rebuild_maps_ownership_check_stops_before_timeline_write(self):
        with tempfile.TemporaryDirectory() as vault:
            session = os.path.join(
                vault,
                "01-Projects/demo/Memory/sessions/2026-07-10-demo.md",
            )
            topic = os.path.join(vault, "03-Maps", "topic-index.md")
            timeline = os.path.join(vault, "03-Maps", "timeline.md")
            os.makedirs(os.path.dirname(session), exist_ok=True)
            os.makedirs(os.path.dirname(topic), exist_ok=True)
            write_text(
                session,
                "---\nsession_id: demo\ndate: '2026-07-10'\ntags: []\n---\n\n# Demo\n",
            )
            write_text(topic, "old topic\n")
            write_text(timeline, "old timeline\n")

            def ownership_check():
                if read_text(topic) != "old topic\n":
                    raise RuntimeError("ownership lost after topic map")

            with self.assertRaisesRegex(RuntimeError, "ownership lost"):
                rebuild_maps(vault, ownership_check=ownership_check)

            self.assertNotEqual(read_text(topic), "old topic\n")
            self.assertEqual(read_text(timeline), "old timeline\n")

    def test_malformed_heartbeat_is_not_overwritten_by_reporter(self):
        with tempfile.TemporaryDirectory() as vault:
            feedback = os.path.join(vault, "04-Feedback")
            os.makedirs(feedback)
            heartbeat = os.path.join(feedback, "heartbeat.md")
            malformed = "---\nharvested_sessions: [\n---\n\n# Preserve me\n"
            write_text(heartbeat, malformed)

            with self.assertRaises(ValueError):
                update_heartbeat(vault, sessions_processed=0)

            self.assertEqual(read_text(heartbeat), malformed)

    def test_maps_use_unambiguous_project_session_links(self):
        with tempfile.TemporaryDirectory() as vault:
            session = os.path.join(
                vault,
                "01-Projects/demo/Memory/sessions/2026-07-10-demo.md",
            )
            os.makedirs(os.path.dirname(session), exist_ok=True)
            os.makedirs(os.path.join(vault, "03-Maps"), exist_ok=True)
            write_text(
                session,
                """---
session_id: demo-session
date: '2026-07-10'
tags: []
decisions_made: []
errors_encountered: []
---

# Demo Session
""",
            )

            rebuild_maps(vault)

            expected = "[[01-Projects/demo/Memory/sessions/2026-07-10-demo\\|Demo Session]]"
            self.assertIn(expected, read_text(os.path.join(vault, "03-Maps/timeline.md")))
            self.assertIn(expected, read_text(os.path.join(vault, "03-Maps/topic-index.md")))

    def test_degraded_scan_preserves_previous_processed_sessions(self):
        with tempfile.TemporaryDirectory() as vault:
            feedback = os.path.join(vault, "04-Feedback")
            os.makedirs(feedback)
            heartbeat = os.path.join(feedback, "heartbeat.md")
            write_text(
                heartbeat,
                """---
last_scan: '2026-07-01T00:00:00'
scan_status: ok
sessions_processed: 1
processed_sessions:
  existing-session: old-fingerprint
harvested_sessions:
  harvested-session: harvested-fingerprint
errors: []
---

# Scanner Heartbeat
""",
            )

            update_heartbeat(
                vault,
                sessions_processed=0,
                processed_sessions=None,
                scan_status="degraded",
                errors=["backup failed"],
            )

            with open(heartbeat, "r", encoding="utf-8") as handle:
                frontmatter = yaml.safe_load(handle.read().split("---", 2)[1])
            self.assertEqual(frontmatter["scan_status"], "degraded")
            self.assertEqual(
                frontmatter["processed_sessions"],
                {"existing-session": "old-fingerprint"},
            )
            self.assertEqual(
                frontmatter["harvested_sessions"],
                {"harvested-session": "harvested-fingerprint"},
            )
            self.assertEqual(frontmatter["errors"], ["backup failed"])

    def test_concurrent_report_and_harvest_preserve_adaptive_cursor(self):
        with tempfile.TemporaryDirectory() as vault:
            feedback = os.path.join(vault, "04-Feedback")
            os.makedirs(feedback)
            heartbeat = os.path.join(feedback, "heartbeat.md")
            write_text(
                heartbeat,
                """---
harvest_baseline_initialized_at: 2026-07-01T00:00:00+08:00
harvested_sessions: {}
adaptive_cursors: {}
processed_sessions: {}
errors: []
---

# Scanner Heartbeat
""",
            )
            reporter_read = threading.Event()
            allow_reporter_write = threading.Event()
            harvester_finished = threading.Event()
            failures = []
            original_load = reporter.load_heartbeat_frontmatter
            transcript = os.path.join(vault, "new-session.jsonl")
            session_key = transcript_state_key(transcript)

            def paused_load(path):
                state = original_load(path)
                reporter_read.set()
                if not allow_reporter_write.wait(timeout=5):
                    raise TimeoutError("test did not release reporter heartbeat write")
                return state

            def run_reporter():
                try:
                    update_heartbeat(vault, sessions_processed=0)
                except Exception as exc:
                    failures.append(exc)

            def run_harvester():
                try:
                    mark_transcript_harvested(
                        {"vault_path": vault},
                        transcript,
                        version="file:20:1",
                        adaptive_cursor="file-bytes:20",
                    )
                except Exception as exc:
                    failures.append(exc)
                finally:
                    harvester_finished.set()

            with patch("reporter.load_heartbeat_frontmatter", side_effect=paused_load):
                reporter_thread = threading.Thread(target=run_reporter)
                reporter_thread.start()
                self.assertTrue(reporter_read.wait(timeout=5))
                harvester_thread = threading.Thread(target=run_harvester)
                harvester_thread.start()
                harvester_finished.wait(timeout=0.2)
                allow_reporter_write.set()
                reporter_thread.join(timeout=5)
                harvester_thread.join(timeout=5)

            self.assertFalse(reporter_thread.is_alive())
            self.assertFalse(harvester_thread.is_alive())
            self.assertEqual(failures, [])
            frontmatter = yaml.safe_load(read_text(heartbeat).split("---", 2)[1])
            self.assertEqual(
                frontmatter.get("adaptive_cursors", {}).get(session_key),
                "file-bytes:20",
            )
            self.assertEqual(
                frontmatter.get("harvested_sessions", {}).get(session_key),
                "file:20:1",
            )


def write_text(path, content):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


if __name__ == "__main__":
    unittest.main()
