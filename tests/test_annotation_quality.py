import os
import glob
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from annotation_quality import (
    QUALITY_CANDIDATE,
    QUALITY_FORMAL,
    QUALITY_REJECTED,
    assess_decision,
    assess_error,
    assess_favor,
    collapse_runtime_duplicates,
    filter_runtime_quality,
    partition_annotations,
    process_annotation_candidates,
)
from memory_schema import memory_revision


class AnnotationQualityTests(unittest.TestCase):
    def test_durable_decision_with_tradeoff_is_formal(self):
        result = assess_decision(
            {
                "text": "保留 DECISION、ERROR、FAVOR 作为主要可见标签",
                "context": "现有解析稳定，主要缺陷来自语义判断和重复记忆",
                "project": "agent-memory-beacon",
            }
        )

        self.assertEqual(result.status, QUALITY_FORMAL)
        self.assertGreaterEqual(result.score, 0.65)

    def test_completion_report_is_not_a_durable_decision(self):
        result = assess_decision(
            {
                "text": "完成全部测试并更新文档",
                "context": "任务已经结束",
            }
        )

        self.assertEqual(result.status, QUALITY_CANDIDATE)
        self.assertIn("outcome_without_choice", result.reasons)

    def test_temporary_decision_stays_candidate(self):
        result = assess_decision(
            {
                "text": "这次先不要操作 GitHub",
                "context": "用户等会还要确认账号",
            }
        )

        self.assertEqual(result.status, QUALITY_CANDIDATE)
        self.assertIn("temporary_or_one_off", result.reasons)

    def test_scope_decision_with_review_status_is_still_formal(self):
        result = assess_decision(
            {
                "text": "将视觉结论限定为本地 PASS，不替代公开发布验收",
                "context": "外部发布仍需要真实 Codex 宿主和远程 CI 证据",
            }
        )

        self.assertEqual(result.status, QUALITY_FORMAL)
        self.assertNotIn("evaluation_outcome", result.reasons)

    def test_review_outcome_wording_variants_are_not_formal_decisions(self):
        cases = (
            "引用专项结论为 NEEDS REVISION，记录 1 项 MAJOR 与 1 项 MINOR CITATION",
            "将审查结论定为 NEEDS REVISION",
            "answer-correctness 审查结论为 NEEDS REVISION",
            "OCR 与布局专项结论为 PASS",
            "Adversarial synthesis 判定为 BLOCKED",
            "将公式完整性门的双向语义错误评为 MAJOR PROCESS",
            "将锁原子性、报告发布事务和 manifest 摘要筛选列为已复现的主要缺陷",
            "逻辑修复通过本地确定性验收，但公开发布验收仍待外部完成门槛",
            "当前 Acceptance B 不足以作为原定 clean-pass 标准下的第二次通过，变更暂不应提交",
            "Acceptance B 暂不能作为第二次完整通过或能力晋升依据",
        )

        for text in cases:
            with self.subTest(text=text):
                result = assess_decision(
                    {
                        "text": text,
                        "context": "这是一次审查运行产生的结果，不是可复用的技术选择",
                    }
                )
                self.assertEqual(result.status, QUALITY_CANDIDATE)
                self.assertIn("evaluation_outcome", result.reasons)

    def test_review_status_used_as_a_release_gate_is_still_formal(self):
        cases = (
            (
                "将审查 PASS 作为发布门槛",
                "只有独立审查通过后才能发布稳定版本",
            ),
            (
                "将 Acceptance PASS 作为发布门槛",
                "只有验收通过后才能发布稳定版本",
            ),
            (
                "验收 A/B 只提升逐项具有完整后置条件和独立产物证据的能力",
                "能力晋升规则需要可复算的完整验收证据，不能从单项结果跨行推断",
            ),
        )

        for text, context in cases:
            with self.subTest(text=text):
                result = assess_decision({"text": text, "context": context})
                self.assertEqual(result.status, QUALITY_FORMAL)
                self.assertNotIn("evaluation_outcome", result.reasons)

    def test_term_benlun_in_rationale_does_not_make_decision_temporary(self):
        result = assess_decision(
            {
                "text": "将 turn-ended 通知文案改为 Codex 停止输出",
                "context": "notify 表示本轮输出结束，不能代表整个任务完成",
            }
        )

        self.assertEqual(result.status, QUALITY_FORMAL)
        self.assertNotIn("temporary_or_one_off", result.reasons)

    def test_resolved_reusable_error_is_formal(self):
        result = assess_error(
            {
                "type": "tool_binary_not_found",
                "resolution": (
                    "系统缺少 pdftotext，改用 PyMuPDF 提取文本，"
                    "并完成页数和关键词校验"
                ),
            }
        )

        self.assertEqual(result.status, QUALITY_FORMAL)
        self.assertGreaterEqual(result.score, 0.65)

    def test_successful_setup_without_a_failure_is_not_a_formal_error(self):
        result = assess_error(
            {
                "type": "shell-cli",
                "resolution": "安装 jq 并使用它完成 JSON 校验，测试通过",
            }
        )

        self.assertEqual(result.status, QUALITY_CANDIDATE)
        self.assertIn("missing_failure_signal", result.reasons)

    def test_unresolved_error_stays_candidate(self):
        result = assess_error(
            {
                "type": "api_auth_failure",
                "resolution": "API Key 无效或无权限，尚未替换凭据",
            }
        )

        self.assertEqual(result.status, QUALITY_CANDIDATE)
        self.assertIn("unresolved", result.reasons)

    def test_expected_tdd_red_is_rejected(self):
        result = assess_error(
            {
                "type": "logic_assumption_violated",
                "resolution": "TDD RED 阶段的预期失败，随后开始实现",
            }
        )

        self.assertEqual(result.status, QUALITY_REJECTED)
        self.assertIn("expected_failure", result.reasons)

    def test_error_triggered_phrase_is_not_mistaken_for_a_ui_misclick(self):
        result = assess_error(
            {
                "type": "other",
                "resolution": (
                    "Sub2API 模型管理器错误触发日志重试循环，"
                    "删除膨胀日志并创建守护任务后恢复正常"
                ),
            }
        )

        self.assertNotIn("one_off_interaction", result.reasons)

    def test_durable_favor_is_formal_and_environment_is_retyped(self):
        preference = assess_favor(
            {
                "content": "保留机器标签英文，说明内容使用中文",
                "context": "用户需要直接读懂 Obsidian 记忆",
                "type": "preference",
            }
        )
        environment = assess_favor(
            {
                "content": "主要电脑使用 macOS，Codex 配置目录是 ~/.codex",
                "context": "后续安装和路径选择都依赖这个稳定环境事实",
                "type": "preference",
            }
        )

        self.assertEqual(preference.status, QUALITY_FORMAL)
        self.assertEqual(preference.suggested_type, "preference")
        self.assertEqual(environment.status, QUALITY_FORMAL)
        self.assertEqual(environment.suggested_type, "environment")

    def test_temporary_favor_is_rejected(self):
        result = assess_favor(
            {
                "content": "这次先不要修改文件",
                "context": "当前只是讨论",
                "type": "preference",
            }
        )

        self.assertEqual(result.status, QUALITY_REJECTED)
        self.assertIn("temporary_or_one_off", result.reasons)

    def test_trigger_shaped_chinese_rules_are_durable_favors(self):
        cases = (
            (
                "新增个人偏好时在回复末尾用 [FAVOR] 明示",
                "用户需要看见本轮新增了哪些个人偏好",
                "preference",
            ),
            (
                "Skill 调用场景要具体到任务根因、对象、失败模式和边界",
                "避免只靠表面关键词随意调用 Skill",
                "project_rule",
            ),
            (
                "分析 GitHub skill、插件或仓库前先查看上游 README 和源码",
                "避免根据名称猜测项目用途",
                "project_rule",
            ),
        )

        for content, context, favor_type in cases:
            with self.subTest(content=content):
                result = assess_favor(
                    {"content": content, "context": context, "type": favor_type}
                )
                self.assertEqual(result.status, QUALITY_FORMAL)
                self.assertNotIn("durability_unclear", result.reasons)

    def test_near_duplicate_errors_collapse_only_in_runtime_view(self):
        first = runtime_record(
            "error-pdf-a",
            "error",
            "tcad",
            "shell-cli",
            "本机缺少 pdftotext，改用 pypdf 完成最终文本完整性校验",
        )
        second = runtime_record(
            "error-pdf-b",
            "error",
            "tcad",
            "shell-cli",
            "系统没有 pdftotext，改用 PyMuPDF 提取 PDF 文本并完成核对",
        )

        collapsed, duplicate_groups = collapse_runtime_duplicates([first, second])

        self.assertEqual(len(collapsed), 1)
        self.assertEqual(len(duplicate_groups), 1)
        group = duplicate_groups[0]
        self.assertEqual(set(group["member_ids"]), {first["id"], second["id"]})
        self.assertIn(group["representative_id"], group["member_ids"])
        self.assertEqual(
            set(collapsed[0]["duplicate_ids"]),
            {first["id"], second["id"]} - {collapsed[0]["id"]},
        )
        self.assertEqual(first["status"], "active")
        self.assertEqual(second["status"], "active")

    def test_truncated_duplicate_decision_collapses_only_in_runtime_view(self):
        first = runtime_record(
            "decision-graph-short",
            "decision",
            "agent-memory-beacon",
            "用正文 wiki link 连接个人记忆和项目记忆节点",
            "Obsidian 图谱主要依赖 Markdown 正文中的 `[[...",
        )
        second = runtime_record(
            "decision-graph-complete",
            "decision",
            "agent-memory-beacon",
            "用正文 wiki link 连接个人记忆和项目记忆节点",
            "Obsidian 图谱主要依赖 Markdown 正文中的链接，"
            "frontmatter 元数据不足以让用户偏好和 decision 节点产生可见连线",
        )

        collapsed, duplicate_groups = collapse_runtime_duplicates([first, second])

        self.assertEqual(len(collapsed), 1)
        self.assertEqual(collapsed[0]["id"], second["id"])
        self.assertEqual(len(duplicate_groups), 1)
        self.assertEqual(
            set(duplicate_groups[0]["member_ids"]),
            {first["id"], second["id"]},
        )
        self.assertEqual(first["summary"], "Obsidian 图谱主要依赖 Markdown 正文中的 `[[...")

    def test_same_decision_title_with_distinct_complete_summaries_stays_separate(self):
        first = runtime_record(
            "decision-cache-read",
            "decision",
            "demo",
            "缓存策略",
            "读取路径使用十分钟内存缓存，避免重复解析同一份索引",
        )
        second = runtime_record(
            "decision-cache-write",
            "decision",
            "demo",
            "缓存策略",
            "写入路径禁用缓存并执行原子替换，避免提交旧 revision",
        )

        collapsed, duplicate_groups = collapse_runtime_duplicates([first, second])

        self.assertEqual(len(collapsed), 2)
        self.assertEqual(duplicate_groups, [])

    def test_truncated_summary_exception_does_not_merge_workflow_records(self):
        first = runtime_record(
            "workflow-github-short",
            "workflow",
            "demo",
            "GitHub 源码检查",
            "分析 GitHub 项目前先读取 README 和关键源代码...",
        )
        second = runtime_record(
            "workflow-github-complete",
            "workflow",
            "demo",
            "GitHub 源码检查",
            "分析 GitHub 项目前先读取 README 和关键源代码，"
            "但用户只问通用概念或明确要求离线时不执行网络访问",
        )

        collapsed, duplicate_groups = collapse_runtime_duplicates([first, second])

        self.assertEqual(len(collapsed), 2)
        self.assertEqual(duplicate_groups, [])

    def test_duplicate_collapse_does_not_cross_projects_or_failure_modes(self):
        records = [
            runtime_record(
                "error-pdf-a",
                "error",
                "tcad",
                "shell-cli",
                "系统缺少 pdftotext，改用 PyMuPDF 完成文本核对",
            ),
            runtime_record(
                "error-pdf-other-project",
                "error",
                "agent-memory-beacon",
                "shell-cli",
                "系统缺少 pdftotext，改用 PyMuPDF 完成文本核对",
            ),
            runtime_record(
                "error-pdf-parse",
                "error",
                "tcad",
                "shell-cli",
                "pdftotext 可以运行但输出乱码，改用 OCR 恢复中文内容",
            ),
        ]

        collapsed, duplicate_groups = collapse_runtime_duplicates(records)

        self.assertEqual(len(collapsed), 3)
        self.assertEqual(duplicate_groups, [])

    def test_skill_rules_with_different_boundaries_do_not_collapse(self):
        first = runtime_record(
            "skill-humanizer-formal",
            "skill",
            "demo",
            "humanizer: 中文自然化",
            "降低模板感",
        )
        first.update(
            {
                "name": "humanizer",
                "when": "用户要求说人话",
                "avoid": "用户要求逐字引用",
            }
        )
        second = {
            **first,
            "id": "skill-humanizer-academic",
            "avoid": "用户要求保持正式学术语气",
        }

        collapsed, duplicate_groups = collapse_runtime_duplicates(
            [first, second]
        )

        self.assertEqual(len(collapsed), 2)
        self.assertEqual(duplicate_groups, [])

    def test_same_missing_dependency_collapses_despite_different_fallback_wording(self):
        first = runtime_record(
            "error-yaml-a",
            "error",
            "demo",
            "shell-cli",
            "系统 Python 缺少 PyYAML，改用仓库 scripts/.venv 后测试正常运行",
        )
        second = runtime_record(
            "error-yaml-b",
            "error",
            "demo",
            "shell-cli",
            "默认 Python 缺少 PyYAML，切换到 /Users/example/venv/bin/python 完成测试验证",
        )

        collapsed, duplicate_groups = collapse_runtime_duplicates([first, second])

        self.assertEqual(len(collapsed), 1)
        self.assertEqual(len(duplicate_groups), 1)

    def test_same_error_collapses_across_legacy_error_type_labels(self):
        first = runtime_record(
            "error-pdf-shell",
            "error",
            "demo",
            "shell-cli",
            "系统缺少 pdftotext，改用 pypdf 完成 PDF 文本校验",
        )
        second = runtime_record(
            "error-pdf-path",
            "error",
            "demo",
            "path-filesystem",
            "pdftotext 不在当前 PATH，改用 PyMuPDF 提取 PDF 文本并完成核对",
        )

        collapsed, duplicate_groups = collapse_runtime_duplicates([first, second])

        self.assertEqual(len(collapsed), 1)
        self.assertEqual(len(duplicate_groups), 1)

    def test_transport_timeout_does_not_collapse_with_post_timeout_bytes_bug(self):
        transport_timeout = runtime_record(
            "error-clone-timeout",
            "error",
            "demo",
            "api-network",
            "GitHub git clone 连接超时，改用 GitHub API 读取源码并完成核查",
        )
        bytes_bug = runtime_record(
            "error-timeout-stderr-bytes",
            "error",
            "demo",
            "api-network",
            "git clone 超时后 TimeoutExpired 返回的 stderr 是 bytes，"
            "统一转成文本后重跑成功",
        )

        collapsed, duplicate_groups = collapse_runtime_duplicates(
            [transport_timeout, bytes_bug]
        )

        self.assertEqual(len(collapsed), 2)
        self.assertEqual(duplicate_groups, [])

    def test_compound_error_with_an_independent_failure_stays_separate(self):
        missing_yaml = runtime_record(
            "error-pyyaml",
            "error",
            "demo",
            "shell-cli",
            "系统 Python 缺少 PyYAML，改用仓库虚拟环境后测试正常运行",
        )
        compound_runtime_error = runtime_record(
            "error-pyyaml-and-runtime",
            "error",
            "demo",
            "shell-cli",
            "系统 Python 缺少 PyYAML 且 shutil.rmtree 不支持 dir_fd，"
            "改用 Python 3.14 虚拟环境后完成测试",
        )

        collapsed, duplicate_groups = collapse_runtime_duplicates(
            [missing_yaml, compound_runtime_error]
        )

        self.assertEqual(len(collapsed), 2)
        self.assertEqual(duplicate_groups, [])

    def test_secondary_missing_dependency_keeps_compound_error_separate(self):
        missing_tool = runtime_record(
            "error-pdftotext",
            "error",
            "demo",
            "shell-cli",
            "系统没有 pdftotext，改用 PyMuPDF 完成 PDF 文本核对",
        )
        missing_tool_and_fallback = runtime_record(
            "error-pdftotext-and-pypdf",
            "error",
            "demo",
            "shell-cli",
            "系统缺少 pdftotext 且 Python 环境无 pypdf/PyPDF2，"
            "改用 Ghostscript txtwrite 提取 PDF 文本并完成核对",
        )

        collapsed, duplicate_groups = collapse_runtime_duplicates(
            [missing_tool, missing_tool_and_fallback]
        )

        self.assertEqual(len(collapsed), 2)
        self.assertEqual(duplicate_groups, [])

    def test_read_only_review_wording_is_not_a_permission_failure(self):
        first = runtime_record(
            "error-openclaw-a",
            "error",
            "demo",
            "shell-cli",
            "本机没有 openclaw Obsidian 辅助命令，"
            "改用 Vault 文件系统完成只读审查",
        )
        second = runtime_record(
            "error-openclaw-b",
            "error",
            "demo",
            "shell-cli",
            "本机没有 openclaw CLI，改为直接核对 Vault 文件和 Obsidian 配置完成验证",
        )

        collapsed, duplicate_groups = collapse_runtime_duplicates([first, second])

        self.assertEqual(len(collapsed), 1)
        self.assertEqual(len(duplicate_groups), 1)

    def test_high_confidence_non_memories_are_suppressed_from_runtime_only(self):
        review_outcome = runtime_record(
            "decision-review-outcome",
            "decision",
            "demo",
            "logic-reviewer 结论为 NEEDS REVISION",
            "发现两个仍需修复的验证门缺陷",
        )
        durable = runtime_record(
            "decision-durable",
            "decision",
            "demo",
            "采用候选优先的质量门",
            "避免不确定内容进入正式召回",
        )
        unresolved = runtime_record(
            "error-unresolved",
            "error",
            "demo",
            "api-network",
            "API Key 无效或无权限，尚未替换凭据",
        )

        eligible, suppressed = filter_runtime_quality(
            [review_outcome, durable, unresolved]
        )

        self.assertEqual([item["id"] for item in eligible], [durable["id"]])
        self.assertEqual(
            set(suppressed),
            {review_outcome["id"], unresolved["id"]},
        )
        self.assertIn("evaluation_outcome", suppressed[review_outcome["id"]])
        self.assertIn("unresolved", suppressed[unresolved["id"]])
        self.assertEqual(review_outcome["status"], "active")
        self.assertEqual(unresolved["status"], "active")

    def test_duplicate_required_by_another_memory_is_not_suppressed(self):
        first = runtime_record(
            "error-pdf-a",
            "error",
            "tcad",
            "shell-cli",
            "本机缺少 pdftotext，改用 pypdf 完成文本校验",
        )
        second = runtime_record(
            "error-pdf-b",
            "error",
            "tcad",
            "shell-cli",
            "系统没有 pdftotext，改用 PyMuPDF 完成文本核对",
        )
        dependent = runtime_record(
            "decision-pdf-flow",
            "decision",
            "tcad",
            "PDF 校验使用可用工具链",
            "确保提取和渲染均有验证证据",
            requires=[second["id"]],
        )

        collapsed, duplicate_groups = collapse_runtime_duplicates(
            [first, second, dependent]
        )

        self.assertEqual(len(collapsed), 3)
        self.assertEqual(duplicate_groups, [])

    def test_partition_keeps_only_formal_annotations_on_formal_path(self):
        result = partition_annotations(
            [
                {
                    "text": "采用候选优先的标签质量门",
                    "context": "避免不确定内容直接污染正式召回",
                },
                {
                    "text": "完成全部测试并更新文档",
                    "context": "任务已经结束",
                },
            ],
            [
                {
                    "type": "tool_binary_not_found",
                    "resolution": (
                        "系统缺少 pdftotext，改用 PyMuPDF 提取文本并完成核对"
                    ),
                },
                {
                    "type": "api_auth_failure",
                    "resolution": "API Key 无效，尚未替换凭据",
                },
                {
                    "type": "logic_assumption_violated",
                    "resolution": "TDD RED 阶段的预期失败",
                },
            ],
        )

        self.assertEqual(len(result["decisions"]), 1)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(len(result["candidates"]), 2)
        self.assertEqual(len(result["rejected"]), 1)

    def test_annotation_candidates_are_idempotent_and_session_counted(self):
        candidate = partition_annotations(
            [],
            [
                {
                    "type": "api_auth_failure",
                    "resolution": "API Key 无效，尚未替换凭据",
                }
            ],
        )["candidates"]
        with tempfile.TemporaryDirectory() as vault:
            cfg = {
                "vault_path": vault,
                "annotation_quality": {
                    "enabled": True,
                    "candidate_dir": "04-Feedback/_annotation-candidates",
                },
            }

            first = process_annotation_candidates(
                cfg, candidate, "demo", "session-1", "2026-07-18"
            )
            replay = process_annotation_candidates(
                cfg, candidate, "demo", "session-1", "2026-07-18"
            )
            repeated = process_annotation_candidates(
                cfg, candidate, "demo", "session-2", "2026-07-19"
            )

            paths = glob.glob(
                os.path.join(vault, "04-Feedback/_annotation-candidates/*.md")
            )
            self.assertEqual(first["candidates"], 1)
            self.assertEqual(replay["updated"], 0)
            self.assertEqual(repeated["updated"], 1)
            self.assertEqual(len(paths), 1)
            with open(paths[0], encoding="utf-8") as handle:
                content = handle.read()
            self.assertIn("seen_count: 2", content)
            self.assertIn("unresolved", content)


def runtime_record(
    memory_id,
    memory_type,
    project,
    title,
    summary,
    *,
    requires=None,
):
    path_kind = "decisions" if memory_type == "decision" else "pitfalls"
    record = {
        "id": memory_id,
        "revision": "",
        "type": memory_type,
        "status": "active",
        "project": project,
        "scope": "project",
        "title": title,
        "summary": summary,
        "date": "2026-07-12",
        "source_refs": [f"session:{memory_id}"],
        "aliases": [],
        "path": f"01-Projects/{project}/Memory/{path_kind}",
        "source_note": f"note:01-Projects/{project}/Memory/{path_kind}",
    }
    if requires:
        record["requires"] = list(requires)
    record["revision"] = memory_revision(record)
    return record


if __name__ == "__main__":
    unittest.main()
