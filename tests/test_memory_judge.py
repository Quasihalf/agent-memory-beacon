import os
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from memory_judge import classify_memory_type, process_personal_memory


class MemoryJudgeTests(unittest.TestCase):
    def test_same_session_repeated_chunks_do_not_promote(self):
        with tempfile.TemporaryDirectory() as vault:
            result = process_personal_memory(
                test_cfg(vault),
                {
                    "messages": [
                        {
                            "role": "user",
                            "text": (
                                "我的想法是把不确定的内容先放到待确认文件夹。\n"
                                "如果以后重复出现类似内容，再把它加到正式记录里。"
                            ),
                        }
                    ]
                },
                "proj",
                "sess-1",
                "2026-07-04",
            )

            self.assertEqual(result["candidates"], 1)
            self.assertEqual(result["promoted"], 0)
            self.assertEqual(result["formal"], 0)
            self.assertFalse(
                os.path.exists(os.path.join(vault, "05-Agent-Memory/personal-memory.md"))
            )

    def test_reprocessing_same_session_is_idempotent(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            parsed = {
                "messages": [
                    {
                        "role": "user",
                        "text": "我的想法是把不确定的内容先放到待确认文件夹。",
                    }
                ]
            }

            first = process_personal_memory(cfg, parsed, "proj", "sess-1", "2026-07-04")
            second = process_personal_memory(cfg, parsed, "proj", "sess-1", "2026-07-04")

            self.assertEqual(first["candidates"], 1)
            self.assertEqual(second["candidates"], 0)
            self.assertEqual(second["promoted"], 0)
            self.assertEqual(second["formal"], 0)
            self.assertEqual(second["updated"], 0)

    def test_second_session_promotes_repeated_candidate(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            first = {
                "messages": [
                    {
                        "role": "user",
                        "text": "我的想法是把不确定的内容先放到待确认文件夹。",
                    }
                ]
            }
            second = {
                "messages": [
                    {
                        "role": "user",
                        "text": "以后不确定的个人化内容先放到待确认文件夹。",
                    }
                ]
            }

            process_personal_memory(cfg, first, "proj", "sess-1", "2026-07-04")
            result = process_personal_memory(cfg, second, "proj", "sess-2", "2026-07-05")

            self.assertEqual(result["promoted"], 1)
            self.assertEqual(result["formal"], 1)
            self.assertTrue(
                os.path.exists(os.path.join(vault, "05-Agent-Memory/personal-memory.md"))
            )

    def test_zcode_memory_hint_is_project_rule(self):
        self.assertEqual(
            classify_memory_type("以后 zcode 默认读取 SQLite 会话"),
            "project_rule",
        )


def test_cfg(vault):
    return {
        "vault_path": vault,
        "personal_memory": {
            "enabled": True,
            "candidate_threshold": 0.45,
            "direct_threshold": 0.85,
            "promote_seen_count": 2,
            "similarity_threshold": 0.5,
        },
    }


if __name__ == "__main__":
    unittest.main()
