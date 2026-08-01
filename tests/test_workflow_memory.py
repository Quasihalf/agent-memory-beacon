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

from workflow_memory import (
    extract_workflow_candidates,
    find_similar_candidate,
    memory_id_for,
    process_workflow_memory,
    upsert_formal_rule,
)
from memory_schema import memory_revision, parse_formal_section


class WorkflowMemoryTests(unittest.TestCase):
    def test_candidate_identity_and_similarity_are_project_scoped(self):
        alpha = {
            "memory_id": memory_id_for(
                "github_source_first", project="alpha", scope="project"
            ),
            "rule_name": "github_source_first",
            "project": "alpha",
            "scope": "project",
            "trigger_scene": "分析 GitHub skill",
            "user_correction": "先看源码",
        }
        beta = {
            **alpha,
            "memory_id": memory_id_for(
                "github_source_first", project="beta", scope="project"
            ),
            "project": "beta",
        }

        self.assertNotEqual(alpha["memory_id"], beta["memory_id"])
        self.assertIsNone(
            find_similar_candidate(alpha, {beta["memory_id"]: beta}, threshold=0.1)
        )

    def test_existing_formal_note_is_upgraded_before_workflow_rule_append(self):
        with tempfile.TemporaryDirectory() as vault:
            formal_path = os.path.join(vault, "workflow-rules.md")
            with open(formal_path, "w", encoding="utf-8") as handle:
                handle.write(
                    "---\n"
                    "title: Workflow Rules\n"
                    "schema_version: '1.0'\n"
                    "custom_field: keep-me\n"
                    "---\n\n"
                    "# Workflow Rules\n\n"
                    "legacy body must survive\n"
                )

            record = {
                "memory_id": "workflow-upgrade",
                "rule_name": "github_source_first",
                "title": "GitHub 源码优先",
                "trigger_scene": "用户要求分析 GitHub 项目",
                "desired_behavior": "先阅读源码",
                "why_it_matters": "避免根据名称猜测",
                "positive_signals": ["GitHub 项目"],
                "negative_signals": ["用户要求离线"],
                "project": "demo",
                "source_ids": ["sess-upgrade"],
                "confidence": 0.9,
                "seen_count": 2,
            }
            changed = upsert_formal_rule(formal_path, record)

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
            self.assertTrue(upsert_formal_rule(formal_path, record))
            replayed = read_text(formal_path)
            self.assertEqual(
                yaml.safe_load(replayed.split("---", 2)[1])["schema_version"],
                "2.0",
            )

    def test_active_workflow_rule_update_preserves_lifecycle_metadata(self):
        with tempfile.TemporaryDirectory() as vault:
            formal_path = os.path.join(vault, "workflow-rules.md")
            record = {
                "memory_id": "workflow-lifecycle",
                "rule_name": "github_source_first",
                "title": "GitHub 源码优先",
                "trigger_scene": "用户要求分析 GitHub 项目",
                "desired_behavior": "先阅读源码",
                "why_it_matters": "避免根据名称猜测",
                "positive_signals": ["GitHub 项目"],
                "negative_signals": ["用户要求离线"],
                "project": "demo",
                "source_ids": ["sess-1"],
                "confidence": 0.9,
                "seen_count": 2,
            }
            self.assertTrue(upsert_formal_rule(formal_path, record))
            title = "github_source_first: 先阅读源码"
            lifecycle = {
                "type": "workflow",
                "status": "active",
                "project": "demo",
                "scope": "project",
                "title": title,
                "summary": record["why_it_matters"],
                "name": record["rule_name"],
                "trigger": record["trigger_scene"],
                "behavior": record["desired_behavior"],
                "avoid": " ".join(record["negative_signals"]),
                "requires": ["workflow-prerequisite"],
                "expires_at": "2026-08-01T12:00:00+08:00",
                "supports": ["project_rule-source-first"],
            }
            formal = read_text(formal_path)
            formal = formal.replace(
                "- status: `active`\n",
                "- status: `active`\n"
                "- requires: `workflow-prerequisite`\n"
                "- expires_at: `2026-08-01T12:00:00+08:00`\n"
                "- supports: `project_rule-source-first`\n",
                1,
            )
            formal = formal.replace(
                f"- revision: `{memory_revision({**lifecycle, 'requires': [], 'expires_at': '', 'supports': []})}`",
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
            self.assertTrue(upsert_formal_rule(formal_path, updated_record))

            updated = read_text(formal_path)
            section = updated[updated.index(f"## {title}"):]
            parsed = parse_formal_section(title, section, "workflow")
            self.assertIsNotNone(parsed)
            self.assertEqual(parsed["requires"], ["workflow-prerequisite"])
            self.assertEqual(parsed["expires_at"], lifecycle["expires_at"])
            self.assertEqual(parsed["supports"], ["project_rule-source-first"])

    def test_platform_injected_context_does_not_create_workflow_memory(self):
        messages = [
            {
                "role": "user",
                "text": (
                    "# AGENTS.md instructions\n"
                    "<INSTRUCTIONS>遇到 GitHub skill 必须先看 README 和源码，不要根据名字猜。</INSTRUCTIONS>\n"
                    "<recommended_plugins>Check GitHub source first.</recommended_plugins>\n"
                    "我只是问一个普通概念。"
                ),
            }
        ]

        self.assertEqual(extract_workflow_candidates(messages, "demo"), [])

    def test_read_only_review_does_not_become_direct_fix_rule(self):
        candidates = extract_workflow_candidates(
            [
                {
                    "role": "user",
                    "text": "只读审查仓库，不要修改任何文件，只报告问题并给最小修复建议。",
                }
            ],
            "proj",
        )

        self.assertEqual(candidates, [])

    def test_direct_fix_rule_allows_an_explicit_read_only_boundary(self):
        candidates = extract_workflow_candidates(
            [
                {
                    "role": "user",
                    "text": (
                        "代码审查发现可修缺陷时继续改到验证通过；"
                        "用户明确要求只读时不要修改。"
                    ),
                }
            ],
            "proj",
        )

        self.assertEqual(
            [item["rule_name"] for item in candidates],
            ["pensive_review_then_fix"],
        )

    def test_extracts_github_source_first_and_pensive_direct_fix_corrections(self):
        messages = [
            {
                "role": "user",
                "text": "你先去 GitHub 看一下原代码，不要根据名字猜，先看 README 和 manifest。",
            },
            {
                "role": "user",
                "text": "pensive 检查出来的问题你不用只告诉我，直接修复。",
            },
        ]

        candidates = extract_workflow_candidates(messages, "demo")
        names = [item["rule_name"] for item in candidates]

        self.assertIn("github_source_first", names)
        self.assertIn("pensive_review_then_fix", names)
        github = next(item for item in candidates if item["rule_name"] == "github_source_first")
        self.assertIn("GitHub", github["trigger_scene"])
        self.assertIn("README", github["desired_behavior"])
        self.assertIn("不要根据名字猜", github["user_correction"])
        self.assertIn("不要联网", " ".join(github["negative_signals"]))
        pensive = next(item for item in candidates if item["rule_name"] == "pensive_review_then_fix")
        self.assertIn("pensive", pensive["trigger_scene"])
        self.assertIn("直接修复", pensive["desired_behavior"])
        self.assertIn("只审查", " ".join(pensive["negative_signals"]))

    def test_extracts_pensive_fix_corrections_with_modify_and_self_correct_wording(self):
        messages = [
            {
                "role": "user",
                "text": "[@pensive](plugin://pensive@claude-night-market) 检查一下程序，如果发现问题就自行改正。",
            },
            {
                "role": "user",
                "text": "pensive 检查一下项目是否达标，发现问题那你修改一下。",
            },
        ]

        candidates = extract_workflow_candidates(messages, "demo")
        names = [item["rule_name"] for item in candidates]

        self.assertIn("pensive_review_then_fix", names)

    def test_first_session_writes_candidate_not_formal_rule(self):
        with tempfile.TemporaryDirectory() as vault:
            result = process_workflow_memory(
                test_cfg(vault),
                parsed_with_user("以后遇到 GitHub skill 截图，默认先查源码和 README，不要只看名字猜。"),
                "demo",
                "sess-1",
                "2026-07-06",
            )

            self.assertEqual(result["candidates"], 1)
            self.assertEqual(result["promoted"], 0)
            self.assertEqual(result["formal"], 0)
            candidate = only_candidate(vault)
            self.assertEqual(candidate["rule_name"], "github_source_first")
            self.assertEqual(candidate["seen_count"], 1)
            self.assertIn("trigger_scene", candidate)
            self.assertIn("user_correction", candidate)
            self.assertIn("desired_behavior", candidate)
            self.assertIn("why_it_matters", candidate)
            self.assertIn("positive_signals", candidate)
            self.assertIn("negative_signals", candidate)
            self.assertIn("evidence_excerpt", candidate)
            self.assertEqual(candidate["source_session"], "sess-1")
            self.assertFalse(
                os.path.exists(os.path.join(vault, "05-Agent-Memory/workflow-rules.md"))
            )

    def test_same_session_replay_does_not_promote_or_increment(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            parsed = parsed_with_user("检查出来就直接改，不用只告诉我问题。")

            first = process_workflow_memory(cfg, parsed, "demo", "sess-1", "2026-07-06")
            before = only_candidate(vault)
            second = process_workflow_memory(cfg, parsed, "demo", "sess-1", "2026-07-06")

            self.assertEqual(first["candidates"], 1)
            self.assertEqual(second["candidates"], 0)
            self.assertEqual(second["promoted"], 0)
            candidate = only_candidate(vault)
            self.assertEqual(candidate["seen_count"], 1)
            assert_candidate_schema_v2(self, before, "candidate")
            assert_candidate_schema_v2(self, candidate, "candidate")
            self.assertEqual(candidate["revision"], before["revision"])

    def test_replayed_session_id_cannot_increment_after_date_change_or_eviction(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            parsed = parsed_with_user(
                "以后遇到 GitHub skill 截图，默认先查源码和 README，不要只看名字猜。"
            )
            for index in range(11):
                process_workflow_memory(
                    cfg,
                    parsed,
                    "demo",
                    f"sess-{index}",
                    f"2026-07-{index + 1:02d}",
                )

            before = only_candidate(vault)
            process_workflow_memory(
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
                "以后遇到 GitHub skill 截图，默认先查源码和 README，不要只看名字猜。"
            )
            process_workflow_memory(cfg, parsed, "demo", "sess-1", "2026-07-06")
            original = glob.glob(
                os.path.join(vault, "04-Feedback/_workflow-candidates/*.md")
            )[0]
            renamed = os.path.join(os.path.dirname(original), "我手动改过的标题.md")
            os.replace(original, renamed)

            process_workflow_memory(cfg, parsed, "demo", "sess-2", "2026-07-07")

            paths = glob.glob(
                os.path.join(vault, "04-Feedback/_workflow-candidates/*.md")
            )
            self.assertEqual(paths, [renamed])
            self.assertEqual(only_candidate(vault)["seen_count"], 2)

    def test_second_similar_session_promotes_specific_formal_rule_with_boundaries(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            process_workflow_memory(
                cfg,
                parsed_with_user("你先去 GitHub 看一下原代码，不要根据名字猜，先看 README。"),
                "demo",
                "sess-1",
                "2026-07-06",
            )
            candidate_revision = only_candidate(vault)["revision"]
            result = process_workflow_memory(
                cfg,
                parsed_with_user("以后遇到这种 GitHub 插件，默认先查源码和 manifest 再分析。"),
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
            formal = read_text(os.path.join(vault, "05-Agent-Memory/workflow-rules.md"))
            self.assertIn("github_source_first", formal)
            self.assertIn("When to apply", formal)
            self.assertIn("Do not apply when", formal)
            self.assertIn("不要联网", formal)
            self.assertIn("README", formal)
            frontmatter = yaml.safe_load(formal.split("---", 2)[1])
            self.assertEqual(frontmatter["schema_version"], "2.0")
            self.assertRegex(formal, r"- revision: `[0-9a-f]{64}`")
            self.assertIn("- status: `active`", formal)
            self.assertIn("- scope: `project`", formal)

    def test_promoted_formal_rule_updates_after_later_repeated_session(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            process_workflow_memory(
                cfg,
                parsed_with_user("检查出来就直接改，不用只告诉我问题。"),
                "demo",
                "sess-1",
                "2026-07-06",
            )
            process_workflow_memory(
                cfg,
                parsed_with_user("pensive 发现可修复问题时，后面你直接修复并跑测试。"),
                "demo",
                "sess-2",
                "2026-07-07",
            )
            process_workflow_memory(
                cfg,
                parsed_with_user("这类本地代码审查发现问题后，别停在报告，继续改到测试通过。"),
                "demo",
                "sess-3",
                "2026-07-08",
            )

            formal = read_text(os.path.join(vault, "05-Agent-Memory/workflow-rules.md"))
            self.assertIn("- seen_count: `3`", formal)
            self.assertIn("继续改到测试通过", formal)

    def test_retracted_workflow_rule_is_not_reactivated_by_later_evidence(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = test_cfg(vault)
            parsed = parsed_with_user(
                "检查出来就直接改，不用只告诉我问题。"
            )
            process_workflow_memory(cfg, parsed, "demo", "sess-1", "2026-07-06")
            process_workflow_memory(cfg, parsed, "demo", "sess-2", "2026-07-07")
            formal_path = os.path.join(
                vault, "05-Agent-Memory/workflow-rules.md"
            )
            retracted = read_text(formal_path).replace(
                "- status: `active`\n",
                "- status: `retracted`\n- retracted_reason: 用户明确撤回\n",
                1,
            )
            with open(formal_path, "w", encoding="utf-8") as handle:
                handle.write(retracted)

            process_workflow_memory(cfg, parsed, "demo", "sess-3", "2026-07-08")

            self.assertEqual(read_text(formal_path), retracted)

    def test_sensitive_content_is_redacted_from_candidate(self):
        with tempfile.TemporaryDirectory() as vault:
            process_workflow_memory(
                test_cfg(vault),
                parsed_with_user(
                    "以后遇到 GitHub 项目先查源码，OPENAI_API_KEY=abcd1234secret、"
                    "token=sk-1234567890abcdef 和 Bearer abcdefghijk123456 都不要记录。"
                ),
                "demo",
                "sess-1",
                "2026-07-06",
            )

            content = read_text(glob.glob(os.path.join(vault, "04-Feedback/_workflow-candidates/*.md"))[0])
            self.assertNotIn("abcd1234secret", content)
            self.assertNotIn("sk-1234567890abcdef", content)
            self.assertNotIn("abcdefghijk123456", content)
            self.assertIn("[REDACTED]", content)


def parsed_with_user(text):
    return {"messages": [{"role": "user", "text": text}]}


def test_cfg(vault):
    return {
        "vault_path": vault,
        "workflow_memory": {
            "enabled": True,
            "candidate_dir": "04-Feedback/_workflow-candidates",
            "formal_path": "05-Agent-Memory/workflow-rules.md",
            "promote_seen_count": 2,
            "similarity_threshold": 0.5,
            "initial_confidence": 0.58,
            "repeat_increment": 0.18,
        },
    }


def only_candidate(vault):
    paths = glob.glob(os.path.join(vault, "04-Feedback/_workflow-candidates/*.md"))
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
