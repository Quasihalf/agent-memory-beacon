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

from skill_preference_learner import (
    append_formal_rule,
    extract_skill_invocations,
    find_similar_candidate,
    memory_id_for,
    process_skill_preferences,
    scene_profile_for,
)
from memory_schema import memory_revision, parse_formal_section


class SkillPreferenceLearnerTests(unittest.TestCase):
    def test_candidate_identity_and_similarity_are_project_scoped(self):
        alpha = {
            "memory_id": memory_id_for(
                "humanizer", "natural", project="alpha", scope="project"
            ),
            "skill_name": "humanizer",
            "scene_key": "natural",
            "project": "alpha",
            "scope": "project",
            "task_intent": "让中文表达更自然",
            "pain_point": "文本模板感明显",
        }
        beta = {
            **alpha,
            "memory_id": memory_id_for(
                "humanizer", "natural", project="beta", scope="project"
            ),
            "project": "beta",
        }

        self.assertNotEqual(alpha["memory_id"], beta["memory_id"])
        self.assertIsNone(
            find_similar_candidate(alpha, {beta["memory_id"]: beta}, threshold=0.1)
        )

    def test_existing_formal_note_is_upgraded_before_skill_rule_append(self):
        with tempfile.TemporaryDirectory() as vault:
            formal_path = os.path.join(vault, "skill-routing-rules.md")
            with open(formal_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "---\n"
                    "title: Skill Routing Rules\n"
                    "schema_version: '1.0'\n"
                    "custom_field: keep-me\n"
                    "---\n\n"
                    "# Skill Routing Rules\n\n"
                    "legacy body must survive\n"
                )

            record = {
                "memory_id": "skill-upgrade",
                "skill_name": "humanizer",
                "task_intent": "让中文更自然",
                "why_skill_fits": "适合去除模板感",
                "positive_signals": ["说人话"],
                "negative_signals": ["逐字引用"],
                "project": "demo",
                "source_ids": ["sess-upgrade"],
                "confidence": 0.9,
                "seen_count": 2,
            }
            changed = append_formal_rule(formal_path, record)

            self.assertTrue(changed)
            formal = read_text(formal_path)
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
            self.assertTrue(append_formal_rule(formal_path, record))
            replayed = read_text(formal_path)
            self.assertEqual(
                yaml.safe_load(replayed.split("---", 2)[1])["schema_version"],
                "2.0",
            )

    def test_active_skill_rule_update_preserves_lifecycle_metadata(self):
        with tempfile.TemporaryDirectory() as vault:
            formal_path = os.path.join(vault, "skill-routing-rules.md")
            record = {
                "memory_id": "skill-lifecycle",
                "skill_name": "humanizer",
                "task_intent": "让中文更自然",
                "why_skill_fits": "适合去除模板感",
                "positive_signals": ["说人话"],
                "negative_signals": ["逐字引用"],
                "project": "demo",
                "source_ids": ["sess-1"],
                "confidence": 0.9,
                "seen_count": 2,
            }
            self.assertTrue(append_formal_rule(formal_path, record))
            title = "humanizer: 让中文更自然"
            lifecycle = {
                "type": "skill",
                "status": "active",
                "project": "demo",
                "scope": "project",
                "title": title,
                "summary": record["why_skill_fits"],
                "name": record["skill_name"],
                "when": " ".join(record["positive_signals"]),
                "avoid": " ".join(record["negative_signals"]),
                "requires": ["skill-prerequisite"],
                "expires_at": "2026-08-01T12:00:00+08:00",
            }
            formal = read_text(formal_path)
            formal = formal.replace(
                "- status: `active`\n",
                "- status: `active`\n"
                "- requires: `skill-prerequisite`\n"
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
            self.assertTrue(append_formal_rule(formal_path, updated_record))

            updated = read_text(formal_path)
            section = updated[updated.index(f"## {title}"):]
            parsed = parse_formal_section(title, section, "skill")
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed["requires"], ["skill-prerequisite"])
            self.assertEqual(parsed["expires_at"], lifecycle["expires_at"])

    def test_subagent_output_and_uppercase_code_symbol_are_not_skill_calls(self):
        messages = [
            {
                "role": "user",
                "text": (
                    "请继续检查。\n"
                    "<subagent_notification>{\"completed\":\"使用 $AppStorage 修复\"}"
                    "</subagent_notification>"
                ),
            }
        ]

        self.assertEqual(extract_skill_invocations(messages), [])

    def test_compare_only_plugin_and_skill_mentions_are_not_invocations(self):
        messages = [
            {
                "role": "user",
                "text": (
                    "[@hyperframes](plugin://hyperframes@openai-curated-remote) "
                    "[$hyperframes:hyperframes](/tmp/SKILL.md) 这两个有什么区别？"
                ),
            }
        ]

        self.assertEqual(extract_skill_invocations(messages), [])

    def test_platform_injected_context_does_not_count_as_skill_invocation(self):
        messages = [
            {
                "role": "user",
                "text": (
                    "# AGENTS.md instructions\n"
                    "<INSTRUCTIONS>For every coding task use $old-hand and $pensive.</INSTRUCTIONS>\n"
                    "<recommended_plugins>Use a plugin when useful.</recommended_plugins>\n"
                    "<environment_context><current_date>2026-07-11</current_date></environment_context>\n"
                    "请只解释这个概念。"
                ),
            }
        ]

        self.assertEqual(extract_skill_invocations(messages), [])

    def test_platform_context_cannot_change_bare_superpowers_routing(self):
        messages = [
            {
                "role": "user",
                "text": (
                    "<INSTRUCTIONS>Always debug every bug with systematic-debugging.</INSTRUCTIONS>\n"
                    "请使用 $superpowers 处理这个普通编排任务。"
                ),
            }
        ]

        invocations = extract_skill_invocations(messages)

        self.assertEqual([item["skill_name"] for item in invocations], ["superpowers"])
        self.assertNotIn("INSTRUCTIONS", invocations[0]["message_text"])

    def test_negated_examples_and_code_blocks_are_not_skill_invocations(self):
        invocations = extract_skill_invocations(
            [
                {
                    "role": "user",
                    "text": (
                        "示例：不要使用 `$humanizer`。\n"
                        "```text\n$manual-memory-capture\n```\n"
                        "> 引用：用 $supercheck 检查\n"
                    ),
                }
            ]
        )

        self.assertEqual(invocations, [])

    def test_extracts_explicit_skill_invocations(self):
        messages = [
            {
                "role": "user",
                "text": (
                    "用 $humanizer 改自然一点，再用 $manual-memory-capture 记一下。\n"
                    "然后调用 $superpowers:systematic-debugging。\n"
                    "也让 [@pensive](plugin://pensive@claude-night-market) 检查一下。\n"
                    "下次调用 superpowers 的调试 skill。"
                ),
            }
        ]

        names = [item["skill_name"] for item in extract_skill_invocations(messages)]

        self.assertIn("humanizer", names)
        self.assertIn("manual-memory-capture", names)
        self.assertIn("superpowers:systematic-debugging", names)
        self.assertIn("pensive", names)
        self.assertEqual(names.count("superpowers:systematic-debugging"), 1)

    def test_ordinary_tool_names_are_not_natural_language_skill_invocations(self):
        messages = [
            {"role": "user", "text": "请使用 Python 和 ReportLab 生成中文 PDF。"},
            {"role": "user", "text": "调用 git status 看一下当前仓库状态。"},
        ]

        self.assertEqual(extract_skill_invocations(messages), [])

    def test_scene_profile_is_specific_for_humanizer(self):
        messages = [
            {
                "role": "user",
                "text": "这段中文回复太模板化，像 AI 写的，我想让它更自然。",
            },
            {"role": "assistant", "text": "我先帮你改一版。"},
            {"role": "user", "text": "用 $humanizer 处理一下，说人话一点。"},
        ]
        invocation = extract_skill_invocations(messages)[0]

        profile = scene_profile_for(
            invocation,
            messages,
            project="demo",
            session_id="sess-1",
            date_str="2026-07-06",
        )

        self.assertEqual(profile["skill_name"], "humanizer")
        self.assertIn("自然", profile["task_intent"])
        self.assertIn("中文", profile["artifact_type"])
        self.assertIn("AI", profile["pain_point"])
        self.assertIn("humanizer", profile["why_skill_fits"])
        self.assertIn("说人话", profile["positive_signals"])
        self.assertTrue(any("正式学术" in item for item in profile["negative_signals"]))
        self.assertLessEqual(len(profile["evidence_excerpt"]), 220)

    def test_scene_context_uses_only_recent_user_messages(self):
        messages = [
            {"role": "user", "text": "请检查这个程序的实现。"},
            {
                "role": "assistant",
                "text": "交接摘要：把 old-hand 当成中文自然化 skill，处理 AI 味。",
            },
            {"role": "user", "text": "用 $old-hand 继续检查。"},
        ]
        invocation = extract_skill_invocations(messages)[0]

        profile = scene_profile_for(
            invocation,
            messages,
            project="demo",
            session_id="sess-1",
            date_str="2026-07-06",
        )

        self.assertEqual(profile["scene_key"], "generic_manual_skill_invocation")
        self.assertIn("请检查这个程序", profile["evidence_excerpt"])
        self.assertNotIn("交接摘要", profile["evidence_excerpt"])

    def test_unknown_skill_is_not_reclassified_from_context_keywords(self):
        messages = [
            {
                "role": "user",
                "text": "用 $vision 检查 humanizer 处理后为什么仍有 AI 味和失败提示。",
            }
        ]
        invocation = extract_skill_invocations(messages)[0]

        profile = scene_profile_for(
            invocation,
            messages,
            project="demo",
            session_id="sess-1",
            date_str="2026-07-06",
        )

        self.assertEqual(profile["skill_name"], "vision")
        self.assertEqual(profile["scene_key"], "generic_manual_skill_invocation")

    def test_first_session_writes_candidate_not_formal_rule(self):
        with tempfile.TemporaryDirectory() as vault:
            result = process_skill_preferences(
                test_cfg(vault),
                parsed_with_user("这段中文太像 AI 了，用 $humanizer 改自然一点。"),
                "demo",
                "sess-1",
                "2026-07-06",
            )

            self.assertEqual(result["candidates"], 1)
            self.assertEqual(result["promoted"], 0)
            self.assertEqual(result["formal"], 0)
            candidate = only_candidate(vault)
            self.assertEqual(candidate["skill_name"], "humanizer")
            self.assertEqual(candidate["seen_count"], 1)
            self.assertIn("task_intent", candidate)
            self.assertIn("artifact_type", candidate)
            self.assertIn("pain_point", candidate)
            self.assertIn("why_skill_fits", candidate)
            self.assertIn("positive_signals", candidate)
            self.assertIn("negative_signals", candidate)
            self.assertIn("evidence_excerpt", candidate)
            self.assertEqual(candidate["source_session"], "sess-1")
            self.assertFalse(
                os.path.exists(os.path.join(vault, "05-Agent-Memory/skill-routing-rules.md"))
            )

    def test_same_session_replay_does_not_promote_or_increment(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            parsed = parsed_with_user("这段中文太像 AI 了，用 $humanizer 改自然一点。")

            first = process_skill_preferences(cfg, parsed, "demo", "sess-1", "2026-07-06")
            before = only_candidate(vault)
            second = process_skill_preferences(cfg, parsed, "demo", "sess-1", "2026-07-06")

            self.assertEqual(first["candidates"], 1)
            self.assertEqual(second["candidates"], 0)
            self.assertEqual(second["promoted"], 0)
            self.assertEqual(second["formal"], 0)
            candidate = only_candidate(vault)
            self.assertEqual(candidate["seen_count"], 1)
            assert_candidate_schema_v2(self, before, "candidate")
            assert_candidate_schema_v2(self, candidate, "candidate")
            self.assertEqual(candidate["revision"], before["revision"])

    def test_replayed_session_id_cannot_increment_after_date_change_or_eviction(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            parsed = parsed_with_user(
                "这段中文太像 AI 了，用 $humanizer 改自然一点。"
            )
            for index in range(11):
                process_skill_preferences(
                    cfg,
                    parsed,
                    "demo",
                    f"sess-{index}",
                    f"2026-07-{index + 1:02d}",
                )

            before = only_candidate(vault)
            process_skill_preferences(
                cfg,
                parsed,
                "demo",
                "sess-0",
                "2026-08-01",
            )
            after = only_candidate(vault)

            self.assertEqual(before["seen_count"], 11)
            self.assertEqual(after["seen_count"], 11)
            self.assertEqual(len(after["source_ids"]), 11)

    def test_renamed_candidate_is_updated_in_place(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            parsed = parsed_with_user(
                "这段中文太像 AI 了，用 $humanizer 改自然一点。"
            )
            process_skill_preferences(cfg, parsed, "demo", "sess-1", "2026-07-06")
            original = glob.glob(
                os.path.join(vault, "04-Feedback/_skill-preferences/*.md")
            )[0]
            renamed = os.path.join(os.path.dirname(original), "我手动改过的标题.md")
            os.replace(original, renamed)

            process_skill_preferences(cfg, parsed, "demo", "sess-2", "2026-07-07")

            paths = glob.glob(
                os.path.join(vault, "04-Feedback/_skill-preferences/*.md")
            )
            self.assertEqual(paths, [renamed])
            self.assertEqual(only_candidate(vault)["seen_count"], 2)

    def test_initial_confidence_can_be_configured(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            cfg["skill_preferences"]["initial_confidence"] = 0.7

            process_skill_preferences(
                cfg,
                parsed_with_user("这段中文太像 AI 了，用 $humanizer 改自然一点。"),
                "demo",
                "sess-1",
                "2026-07-06",
            )

            candidate = only_candidate(vault)
            self.assertEqual(candidate["confidence"], 0.7)

    def test_second_similar_session_promotes_and_writes_negative_signals(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            process_skill_preferences(
                cfg,
                parsed_with_user("这段中文太像 AI 了，用 $humanizer 改自然一点。"),
                "demo",
                "sess-1",
                "2026-07-06",
            )
            candidate_revision = only_candidate(vault)["revision"]
            result = process_skill_preferences(
                cfg,
                parsed_with_user("用户要去掉模板感，用 humanizer 说人话。"),
                "demo",
                "sess-2",
                "2026-07-07",
            )

            self.assertEqual(result["promoted"], 1)
            self.assertEqual(result["formal"], 1)
            candidate = only_candidate(vault)
            self.assertEqual(candidate["status"], "promoted")
            self.assertEqual(candidate["seen_count"], 2)
            assert_candidate_schema_v2(self, candidate, "promoted")
            self.assertNotEqual(candidate["revision"], candidate_revision)
            formal = read_text(os.path.join(vault, "05-Agent-Memory/skill-routing-rules.md"))
            self.assertIn("humanizer", formal)
            self.assertIn("When to consider", formal)
            self.assertIn("Do not use when", formal)
            self.assertIn("正式学术", formal)
            frontmatter = yaml.safe_load(formal.split("---", 2)[1])
            self.assertEqual(frontmatter["schema_version"], "2.0")
            self.assertRegex(formal, r"- revision: `[0-9a-f]{64}`")
            self.assertIn("- status: `active`", formal)
            self.assertIn("- scope: `project`", formal)

    def test_promoted_formal_rule_updates_after_later_repeated_session(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            process_skill_preferences(
                cfg,
                parsed_with_user("这段中文太像 AI 了，用 $humanizer 改自然一点。"),
                "demo",
                "sess-1",
                "2026-07-06",
            )
            process_skill_preferences(
                cfg,
                parsed_with_user("用户要去掉模板感，用 humanizer 说人话。"),
                "demo",
                "sess-2",
                "2026-07-07",
            )
            process_skill_preferences(
                cfg,
                parsed_with_user("这段说明还是不像真人，用 humanizer 去 AI 味。"),
                "demo",
                "sess-3",
                "2026-07-08",
            )

            formal = read_text(os.path.join(vault, "05-Agent-Memory/skill-routing-rules.md"))
            self.assertIn("- seen_count: `3`", formal)
            self.assertIn("不像真人", formal)

    def test_retracted_skill_rule_is_not_reactivated_by_later_evidence(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            parsed = parsed_with_user(
                "这段中文太像 AI 了，用 $humanizer 改自然一点。"
            )
            process_skill_preferences(cfg, parsed, "demo", "sess-1", "2026-07-06")
            process_skill_preferences(cfg, parsed, "demo", "sess-2", "2026-07-07")
            formal_path = os.path.join(
                vault, "05-Agent-Memory/skill-routing-rules.md"
            )
            retracted = read_text(formal_path).replace(
                "- status: `active`\n",
                "- status: `retracted`\n- retracted_reason: 用户明确撤回\n",
                1,
            )
            with open(formal_path, "w", encoding="utf-8") as handle:
                handle.write(retracted)

            process_skill_preferences(cfg, parsed, "demo", "sess-3", "2026-07-08")

            self.assertEqual(read_text(formal_path), retracted)

    def test_sensitive_content_is_redacted_from_candidate(self):
        with tempfile.TemporaryDirectory() as vault:
            process_skill_preferences(
                test_cfg(vault),
                parsed_with_user(
                    "用 $manual-memory-capture 记录 token=sk-1234567890abcdef、"
                    "password=secret-value、Bearer abcdefghijk123456、"
                    "OPENAI_API_KEY=abcd1234secret 和 ANTHROPIC_AUTH_TOKEN=tokenvalue123456"
                ),
                "demo",
                "sess-1",
                "2026-07-06",
            )

            content = read_text(glob.glob(os.path.join(vault, "04-Feedback/_skill-preferences/*.md"))[0])
            self.assertNotIn("sk-1234567890abcdef", content)
            self.assertNotIn("secret-value", content)
            self.assertNotIn("abcdefghijk123456", content)
            self.assertNotIn("abcd1234secret", content)
            self.assertNotIn("tokenvalue123456", content)
            self.assertIn("[REDACTED]", content)


def parsed_with_user(text):
    return {"messages": [{"role": "user", "text": text}]}


def test_cfg(vault):
    return {
        "vault_path": vault,
        "skill_preferences": {
            "enabled": True,
            "candidate_dir": "04-Feedback/_skill-preferences",
            "formal_path": "05-Agent-Memory/skill-routing-rules.md",
            "promote_seen_count": 2,
            "similarity_threshold": 0.5,
        },
    }


def only_candidate(vault):
    paths = glob.glob(os.path.join(vault, "04-Feedback/_skill-preferences/*.md"))
    assert len(paths) == 1, paths
    with open(paths[0], "r", encoding="utf-8") as handle:
        content = handle.read()
    parts = content.split("---", 2)
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
