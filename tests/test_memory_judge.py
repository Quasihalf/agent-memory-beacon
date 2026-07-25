import glob
import hashlib
import json
import os
import sys
import tempfile
import unittest

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from memory_judge import (
    append_formal_memory,
    classify_memory_type,
    extract_memory_candidates,
    process_personal_memory,
    extract_favor_annotations,
    find_similar_candidate,
    memory_id_for,
)
from memory_schema import memory_revision, parse_formal_section


class MemoryJudgeTests(unittest.TestCase):
    def test_project_rule_candidate_identity_and_similarity_are_project_scoped(self):
        alpha = {
            "memory_id": memory_id_for(
                "project_rule", "先查源码", project="alpha", scope="project"
            ),
            "type": "project_rule",
            "content": "分析 GitHub 项目前先查源码",
            "project": "alpha",
            "scope": "project",
        }
        beta = {
            **alpha,
            "memory_id": memory_id_for(
                "project_rule", "先查源码", project="beta", scope="project"
            ),
            "project": "beta",
        }

        self.assertNotEqual(alpha["memory_id"], beta["memory_id"])
        self.assertIsNone(
            find_similar_candidate(alpha, {beta["memory_id"]: beta}, threshold=0.1)
        )

    def test_favor_quality_gate_retypes_environment_and_defers_temporary_request(self):
        environment = extract_favor_annotations(
            "[FAVOR:主要电脑使用 macOS，配置目录是 ~/.codex| "
            "context:后续安装路径依赖该稳定环境| type:preference]",
            "demo",
        )[0]
        temporary = extract_favor_annotations(
            "[FAVOR:这次先不要修改文件| context:当前只是讨论| type:preference]",
            "demo",
        )

        self.assertEqual(environment["type"], "environment")
        self.assertTrue(environment["explicit"])
        self.assertEqual(environment["quality_status"], "formal")
        self.assertEqual(temporary, [])

    def test_existing_formal_note_is_upgraded_before_personal_memory_append(self):
        with tempfile.TemporaryDirectory() as vault:
            formal_path = os.path.join(vault, "personal-memory.md")
            with open(formal_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "---\n"
                    "title: Personal Memory\n"
                    "schema_version: '1.0'\n"
                    "custom_field: keep-me\n"
                    "---\n\n"
                    "# Personal Memory\n\n"
                    "legacy body must survive\n"
                )

            record = {
                "memory_id": "preference-upgrade",
                "title": "用户偏好: 中文输出",
                "content": "默认用中文解释",
                "type": "preference",
                "project": "demo",
                "source_ids": ["sess-upgrade"],
                "confidence": 0.9,
                "seen_count": 2,
            }
            changed = append_formal_memory(formal_path, record)

            self.assertTrue(changed)
            with open(formal_path, "r", encoding="utf-8") as handle:
                formal = handle.read()
            frontmatter = yaml.safe_load(formal.split("---", 2)[1])
            self.assertEqual(frontmatter["schema_version"], "2.0")
            self.assertEqual(frontmatter["custom_field"], "keep-me")
            self.assertIn("legacy body must survive", formal)
            self.assertIn("- source_refs:", formal)

            downgraded = formal.replace(
                "schema_version: '2.0'",
                "schema_version: '1.0'",
                1,
            )
            self.assertNotEqual(downgraded, formal)
            with open(formal_path, "w", encoding="utf-8") as handle:
                handle.write(downgraded)
            self.assertTrue(append_formal_memory(formal_path, record))
            with open(formal_path, "r", encoding="utf-8") as handle:
                replayed = handle.read()
            self.assertEqual(
                yaml.safe_load(replayed.split("---", 2)[1])["schema_version"],
                "2.0",
            )

    def test_active_personal_memory_update_preserves_lifecycle_metadata(self):
        with tempfile.TemporaryDirectory() as vault:
            formal_path = os.path.join(vault, "personal-memory.md")
            record = {
                "memory_id": "preference-lifecycle",
                "title": "用户偏好: 中文输出",
                "content": "默认用中文解释",
                "type": "preference",
                "project": "demo",
                "source_ids": ["sess-1"],
                "confidence": 0.9,
                "seen_count": 2,
            }
            self.assertTrue(append_formal_memory(formal_path, record))
            lifecycle = {
                "type": "preference",
                "status": "active",
                "project": "",
                "scope": "global",
                "title": record["title"],
                "summary": record["content"],
                "requires": ["preference-prerequisite"],
                "expires_at": "2026-08-01T12:00:00+08:00",
            }
            formal = read_text(formal_path)
            formal = formal.replace(
                "- status: `active`\n",
                "- status: `active`\n"
                "- requires: `preference-prerequisite`\n"
                "- expires_at: `2026-08-01T12:00:00+08:00`\n",
                1,
            )
            formal = formal.replace(
                f"- revision: `{memory_revision({**lifecycle, 'requires': [], 'expires_at': ''})}`",
                f"- revision: `{memory_revision(lifecycle)}`",
                1,
            )
            with open(formal_path, "w", encoding="utf-8") as handle:
                handle.write(formal)

            updated_record = {
                **record,
                "source_ids": ["sess-1", "sess-2"],
                "seen_count": 3,
            }
            self.assertTrue(append_formal_memory(formal_path, updated_record))

            updated = read_text(formal_path)
            section = updated[updated.index(f"## {record['title']}"):]
            parsed = parse_formal_section(record["title"], section, "personal")
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed["requires"], ["preference-prerequisite"])
            self.assertEqual(parsed["expires_at"], lifecycle["expires_at"])

    def test_one_off_github_username_change_is_not_personal_memory(self):
        candidates = extract_memory_candidates(
            [
                {
                    "role": "user",
                    "text": "你帮我改一下 GitHub 用户名，我想把它改成 Quasihalf。",
                }
            ],
            "proj",
        )

        self.assertEqual(candidates, [])

    def test_long_information_question_with_future_wording_is_not_preference(self):
        candidates = extract_memory_candidates(
            [
                {
                    "role": "user",
                    "text": "如果说我以后要完成读出电子学芯片中的 TDC 仿真和设计，需要用这些吗？",
                }
            ],
            "tcad",
        )

        self.assertEqual(candidates, [])

    def test_platform_injected_context_is_not_personal_memory(self):
        candidates = extract_memory_candidates(
            [
                {
                    "role": "user",
                    "text": (
                        "# AGENTS.md instructions\n"
                        "<INSTRUCTIONS>我希望以后默认用中文并永久记住这个偏好。</INSTRUCTIONS>\n"
                        "<environment_context><cwd>/tmp/demo</cwd></environment_context>\n"
                        "请解释一下当前功能。"
                    ),
                }
            ],
            "proj",
        )

        self.assertEqual(candidates, [])

    def test_temporary_task_constraint_is_not_personal_memory(self):
        candidates = extract_memory_candidates(
            [
                {
                    "role": "user",
                    "text": "这个项目本次只读审查，不要修改任何文件。",
                }
            ],
            "proj",
        )

        self.assertEqual(candidates, [])

    def test_temporary_favor_annotation_is_not_personal_memory(self):
        candidates = extract_memory_candidates(
            [
                {
                    "role": "assistant",
                    "text": (
                        "[FAVOR:当前任务只读审查，不要修改文件| "
                        "context:本次临时约束| type:preference]"
                    ),
                }
            ],
            "proj",
        )

        self.assertEqual(candidates, [])

    def test_high_scoring_inference_waits_for_confirmation(self):
        with tempfile.TemporaryDirectory() as vault:
            result = process_personal_memory(
                test_cfg(vault),
                {
                    "messages": [
                        {
                            "role": "user",
                            "text": (
                                "我希望这个程序现在自动读取 /Users/demo，"
                                "不要修改其他项目。"
                            ),
                        }
                    ]
                },
                "proj",
                "sess-once",
                "2026-07-10",
            )

            self.assertEqual(result["candidates"], 1)
            self.assertEqual(result["promoted"], 0)
            self.assertEqual(result["formal"], 0)
            self.assertFalse(
                os.path.exists(os.path.join(vault, "05-Agent-Memory/personal-memory.md"))
            )

    def test_personal_memory_redacts_credentials_and_payment_data(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {
                "vault_path": vault,
                "personal_memory": {
                    "enabled": True,
                    "candidate_threshold": 0.1,
                    "direct_threshold": 0.99,
                    "promote_seen_count": 2,
                },
            }
            parsed = {
                "messages": [
                    {
                        "role": "user",
                        "text": (
                            "我希望以后默认记住 password=super-secret-value "
                            "和银行卡号=4111111111111111"
                        ),
                    }
                ]
            }

            process_personal_memory(
                cfg,
                parsed,
                "proj",
                "session-secret",
                "2026-07-10",
            )

            candidate_paths = glob.glob(
                os.path.join(vault, "04-Feedback/_memory-candidates/*.md")
            )
            self.assertEqual(len(candidate_paths), 1)
            with open(candidate_paths[0], "r", encoding="utf-8") as handle:
                content = handle.read()
            self.assertNotIn("super-secret-value", content)
            self.assertNotIn("4111111111111111", content)
            self.assertIn("[REDACTED]", content)

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
            before = only_memory_candidate(vault)
            second = process_personal_memory(cfg, parsed, "proj", "sess-1", "2026-07-04")
            after = only_memory_candidate(vault)

            self.assertEqual(first["candidates"], 1)
            self.assertEqual(second["candidates"], 0)
            self.assertEqual(second["promoted"], 0)
            self.assertEqual(second["formal"], 0)
            self.assertEqual(second["updated"], 0)
            assert_candidate_schema_v2(self, before, "candidate")
            assert_candidate_schema_v2(self, after, "candidate")
            self.assertEqual(after["revision"], before["revision"])

    def test_replayed_session_id_cannot_increment_after_date_change_or_eviction(self):
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
            for index in range(11):
                process_personal_memory(
                    cfg,
                    parsed,
                    "proj",
                    f"sess-{index}",
                    f"2026-07-{index + 1:02d}",
                )

            before = only_memory_candidate(vault)
            process_personal_memory(
                cfg,
                parsed,
                "proj",
                "sess-0",
                "2026-08-01",
            )
            after = only_memory_candidate(vault)

            self.assertEqual(before["seen_count"], 11)
            self.assertEqual(after["seen_count"], 11)
            self.assertEqual(len(after["source_ids"]), 11)

    def test_renamed_candidate_is_updated_in_place(self):
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
            process_personal_memory(cfg, parsed, "proj", "sess-1", "2026-07-04")
            original = glob.glob(
                os.path.join(vault, "04-Feedback/_memory-candidates/*.md")
            )[0]
            renamed = os.path.join(os.path.dirname(original), "我手动改过的标题.md")
            os.replace(original, renamed)

            process_personal_memory(cfg, parsed, "proj", "sess-2", "2026-07-05")

            paths = glob.glob(
                os.path.join(vault, "04-Feedback/_memory-candidates/*.md")
            )
            self.assertEqual(paths, [renamed])
            self.assertEqual(only_memory_candidate(vault)["seen_count"], 2)

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
            candidate_revision = only_memory_candidate(vault)["revision"]
            result = process_personal_memory(cfg, second, "proj", "sess-2", "2026-07-05")

            self.assertEqual(result["promoted"], 1)
            self.assertEqual(result["formal"], 1)
            promoted = only_memory_candidate(vault)
            assert_candidate_schema_v2(self, promoted, "promoted")
            self.assertNotEqual(promoted["revision"], candidate_revision)
            self.assertTrue(
                os.path.exists(os.path.join(vault, "05-Agent-Memory/personal-memory.md"))
            )

    def test_promoted_personal_memory_updates_after_third_session(self):
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
            for index in range(3):
                process_personal_memory(
                    cfg,
                    parsed,
                    "proj",
                    f"sess-{index}",
                    f"2026-07-{index + 4:02d}",
                )

            formal_path = os.path.join(
                vault, "05-Agent-Memory/personal-memory.md"
            )
            with open(formal_path, "r", encoding="utf-8") as handle:
                formal = handle.read()
            self.assertIn("- seen_count: `3`", formal)

    def test_retracted_personal_memory_is_not_reactivated_by_later_evidence(self):
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
            process_personal_memory(cfg, parsed, "proj", "sess-1", "2026-07-04")
            process_personal_memory(cfg, parsed, "proj", "sess-2", "2026-07-05")
            formal_path = os.path.join(
                vault, "05-Agent-Memory/personal-memory.md"
            )
            retracted = read_text(formal_path).replace(
                "- status: `active`\n",
                "- status: `retracted`\n- retracted_reason: 用户明确撤回\n",
                1,
            )
            with open(formal_path, "w", encoding="utf-8") as handle:
                handle.write(retracted)

            process_personal_memory(cfg, parsed, "proj", "sess-3", "2026-07-06")

            self.assertEqual(read_text(formal_path), retracted)

    def test_promoted_personal_memory_absorbs_similar_later_wording(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            messages = [
                "我的想法是把不确定的内容先放到待确认文件夹。",
                "以后不确定的个人化内容先放到待确认文件夹。",
                "不确定的内容以后先放到待确认文件夹。",
            ]
            for index, text in enumerate(messages):
                process_personal_memory(
                    cfg,
                    {"messages": [{"role": "user", "text": text}]},
                    "proj",
                    f"sess-{index}",
                    f"2026-07-{index + 4:02d}",
                )

            candidates = glob.glob(
                os.path.join(vault, "04-Feedback/_memory-candidates/*.md")
            )
            formal = read_text(
                os.path.join(vault, "05-Agent-Memory/personal-memory.md")
            )
            self.assertEqual(len(candidates), 1)
            self.assertIn("- seen_count: `3`", formal)

    def test_same_topic_but_different_preferences_do_not_promote_each_other(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            cfg["personal_memory"]["direct_threshold"] = 0.99
            first = {
                "messages": [
                    {
                        "role": "user",
                        "text": "我希望以后这个程序自动记录有价值的对话到 Obsidian。",
                    }
                ]
            }
            second = {
                "messages": [
                    {
                        "role": "user",
                        "text": "我不希望以后自动上传任何项目到 GitHub。",
                    }
                ]
            }

            process_personal_memory(cfg, first, "proj", "sess-1", "2026-07-04")
            result = process_personal_memory(
                cfg, second, "proj", "sess-2", "2026-07-05"
            )

            self.assertEqual(result["promoted"], 0)
            self.assertEqual(result["formal"], 0)
            self.assertEqual(result["candidates"], 1)
            self.assertEqual(
                len(glob.glob(os.path.join(vault, "04-Feedback/_memory-candidates/*.md"))),
                2,
            )

    def test_opposite_polarity_preferences_do_not_merge(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            cfg["personal_memory"]["direct_threshold"] = 0.99
            positive = {
                "messages": [
                    {
                        "role": "user",
                        "text": "我希望以后默认自动处理代码审查结果。",
                    }
                ]
            }
            negative = {
                "messages": [
                    {
                        "role": "user",
                        "text": "我不希望以后默认自动处理代码审查结果。",
                    }
                ]
            }

            process_personal_memory(cfg, positive, "proj", "sess-1", "2026-07-04")
            result = process_personal_memory(
                cfg, negative, "proj", "sess-2", "2026-07-05"
            )

            self.assertEqual(result["promoted"], 0)
            self.assertEqual(result["candidates"], 1)
            self.assertEqual(
                len(glob.glob(os.path.join(vault, "04-Feedback/_memory-candidates/*.md"))),
                2,
            )

    def test_zcode_memory_hint_is_project_rule(self):
        self.assertEqual(
            classify_memory_type("以后 zcode 默认读取 SQLite 会话"),
            "project_rule",
        )

    def test_structured_specs_are_not_personal_memory_candidates(self):
        parsed = [
            {
                "role": "user",
                "text": (
                    "要求：\n"
                    "- 测试能识别 GitHub 源码优先查看类纠正。\n"
                    "- desired_behavior：以后应该主动怎么做。\n"
                    "| 2026-06-21 | github-obsidian-knowledge-brain | path-filesystem | 修复路径 |\n"
                    "3. 每次检测到用户手动调用 skill，不要只记录关键词。"
                ),
            }
        ]

        candidates = extract_memory_candidates(parsed, "proj")

        self.assertEqual(candidates, [])

    def test_information_seeking_questions_are_not_personal_memory_candidates(self):
        parsed = [
            {
                "role": "user",
                "text": "我想知道这个 decision error 等等之间是否有联系？codex 是否能读懂这些联系？",
            }
        ]

        candidates = extract_memory_candidates(parsed, "proj")

        self.assertEqual(candidates, [])

    def test_favor_annotation_from_assistant_becomes_personal_memory(self):
        parsed = [
            {
                "role": "assistant",
                "text": "[FAVOR:保留机器标签英文，内容用中文| context:用户明确希望可读内容用中文| type:preference]",
            }
        ]

        candidates = extract_memory_candidates(parsed, "proj")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["content"], "保留机器标签英文，内容用中文")
        self.assertEqual(candidates[0]["type"], "preference")
        self.assertGreaterEqual(candidates[0]["confidence"], 0.85)

    def test_favor_annotation_allows_bracketed_text_in_content(self):
        parsed = [
            {
                "role": "assistant",
                "text": (
                    "[FAVOR:新增个人偏好时在回复末尾用 `[FAVOR]` 明示给用户看| "
                    "context:用户希望每次对话都能看到添加了哪些个人偏好| type:preference]"
                ),
            }
        ]

        candidates = extract_memory_candidates(parsed, "proj")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0]["content"],
            "新增个人偏好时在回复末尾用 `[FAVOR]` 明示给用户看",
        )
        self.assertIn("用户希望每次对话都能看到", candidates[0]["evidence"])

    def test_favor_annotation_is_visible_and_written_to_formal_memory(self):
        with tempfile.TemporaryDirectory() as vault:
            result = process_personal_memory(
                test_cfg(vault),
                {
                    "messages": [
                        {
                            "role": "assistant",
                            "text": "[FAVOR:默认用中文解释复杂功能| context:用户看不懂英文输出| project:proj]",
                        }
                    ]
                },
                "proj",
                "sess-favor",
                "2026-07-07",
            )

            self.assertEqual(result["formal"], 1)
            self.assertEqual(result["promoted"], 1)
            self.assertEqual(result["items"][0]["action"], "promoted")
            self.assertIn("默认用中文解释复杂功能", result["items"][0]["content"])
            formal_path = os.path.join(vault, "05-Agent-Memory/personal-memory.md")
            with open(formal_path, "r", encoding="utf-8") as handle:
                formal = handle.read()
            self.assertIn("默认用中文解释复杂功能", formal)
            frontmatter = yaml.safe_load(formal.split("---", 2)[1])
            self.assertEqual(frontmatter["schema_version"], "2.0")
            self.assertRegex(formal, r"- revision: `[0-9a-f]{64}`")
            self.assertIn("- status: `active`", formal)
            self.assertIn("- scope: `global`", formal)


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


def only_memory_candidate(vault):
    paths = glob.glob(
        os.path.join(vault, "04-Feedback/_memory-candidates/*.md")
    )
    assert len(paths) == 1, paths
    with open(paths[0], "r", encoding="utf-8") as handle:
        parts = handle.read().split("---", 2)
    return yaml.safe_load(parts[1])


def assert_candidate_schema_v2(testcase, record, status):
    testcase.assertEqual(record["schema_version"], "2.0")
    testcase.assertEqual(record["status"], status)
    testcase.assertRegex(record["revision"], r"^[0-9a-f]{64}$")
    visible = {
        key: value
        for key, value in record.items()
        if key not in {"revision", "last_seen", "first_seen"}
    }
    expected = hashlib.sha256(
        json.dumps(visible, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    testcase.assertEqual(record["revision"], expected)


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


if __name__ == "__main__":
    unittest.main()
