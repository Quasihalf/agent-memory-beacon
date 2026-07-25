import json
import os
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from score_sessions import score_session


class ScoreSessionsTests(unittest.TestCase):
    def test_codex_assistant_annotations_are_scored(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = os.path.join(tmp, "codex.jsonl")
            write_jsonl(
                transcript,
                [
                    {
                        "type": "session_meta",
                        "payload": {"id": "codex-score"},
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": (
                                "demo-project 完成。"
                                "[DECISION:复用统一解析器| context:避免三端评分分叉]"
                            ),
                        },
                    },
                ],
            )

            result = score_session(
                transcript,
                {"demo": [r"demo-project"]},
            )

            self.assertEqual(result["n_assistant"], 1)
            self.assertEqual(result["n_decisions"], 1)
            self.assertEqual(result["detected_projects"], ["demo"])
            self.assertEqual(result["project_coverage"], 1.0)

    def test_project_coverage_is_zero_without_a_detected_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = os.path.join(tmp, "session.jsonl")
            write_jsonl(
                transcript,
                [
                    {
                        "type": "assistant",
                        "message": {"role": "assistant", "content": "普通回复"},
                    }
                ],
            )

            result = score_session(
                transcript,
                {"demo": [r"demo-project"]},
            )

            self.assertEqual(result["detected_projects"], [])
            self.assertEqual(result["project_coverage"], 0.0)


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            json.dump(record, handle, ensure_ascii=False)
            handle.write("\n")


if __name__ == "__main__":
    unittest.main()
