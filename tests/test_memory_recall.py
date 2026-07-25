import json
import os
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from knowledge_index import rebuild_vault_knowledge_indexes
import memory_recall
from memory_recall import format_results, load_recall_index, recall
from memory_schema import memory_revision, normalize_formal_record

try:
    from test_knowledge_index import write_fixture_vault
except ModuleNotFoundError:
    from tests.test_knowledge_index import write_fixture_vault


class MemoryRecallTests(unittest.TestCase):
    def test_insight_inventory_and_concrete_exploration_are_content_safe(self):
        fusion = insight_unit(
            "insight:fusion",
            "互补弱通道通过融合形成稳定系统",
            "多个不可靠审查器可以通过排名融合组合证据",
            ["审查器", "证据", "融合", "排名"],
            transfer=["审查证据聚合", "记忆召回"],
        )
        unrelated = insight_unit(
            "insight:unrelated",
            "界面动画采用分层时间轴",
            "多个动画阶段使用独立时间轴控制节奏",
            ["动画", "时间轴", "节奏"],
            transfer=["动效设计"],
        )
        index = {"schema_version": "2.0", "units": [fusion, unrelated]}

        inventory = recall("我有哪些启发", index, project="demo")
        concrete = recall(
            "设计多个不可靠审查器时怎样组合证据",
            index,
            project="demo",
        )
        vague = recall("给我一个思路", index, project="demo")

        self.assertEqual({item["id"] for item in inventory}, {fusion["id"], unrelated["id"]})
        self.assertEqual([item["id"] for item in concrete], [fusion["id"]])
        self.assertEqual(vague, [])

    def test_relevant_seed_outranks_shallow_reinforced_insight(self):
        seed = insight_unit(
            "insight:seed-relevant",
            "按独立通道融合审查证据",
            "审查器输出先独立评分再进行排名融合",
            ["审查器", "证据", "独立", "评分", "排名", "融合"],
            maturity="seed",
            confidence=0.74,
            transfer=["审查证据聚合"],
        )
        reinforced = insight_unit(
            "insight:reinforced-shallow",
            "审查器界面使用固定色板",
            "审查器结果用固定色板显示",
            ["审查器", "界面", "色板"],
            maturity="reinforced",
            confidence=0.92,
            transfer=["界面设计"],
        )

        results = recall(
            "设计审查器时如何融合独立评分和证据",
            {"schema_version": "2.0", "units": [reinforced, seed]},
            project="demo",
        )

        self.assertEqual(results[0]["id"], seed["id"])

    def test_inspiration_intent_requires_exploration_and_concrete_content(self):
        self.assertTrue(
            memory_recall.infer_inspiration_intent(
                "请设计多路审查证据融合的替代方案"
            )
        )
        self.assertTrue(memory_recall.infer_inspiration_intent("我有哪些启发"))
        self.assertFalse(memory_recall.infer_inspiration_intent("给我一个思路"))
        self.assertFalse(memory_recall.infer_inspiration_intent("修复一下这个问题"))

    def test_recall_finds_related_decision_for_query(self):
        with tempfile.TemporaryDirectory() as vault:
            write_fixture_vault(vault)
            rebuild_vault_knowledge_indexes({"vault_path": vault})
            index = load_recall_index(vault)

            self.assertIn("_graph", index)

            results = recall("Obsidian 中文 主存储", index, project="demo", limit=3)

            self.assertTrue(results)
            self.assertEqual(results[0]["type"], "decision")
            self.assertIn("Obsidian Markdown", results[0]["title"])
            self.assertGreater(results[0]["score"], 0)

    def test_recall_expands_to_memory_in_explicitly_linked_note(self):
        index = {
            "schema_version": "2.0",
            "units": [
                recall_unit(
                    "decision:direct",
                    "01-Projects/demo/Memory/decisions",
                    source_note="note:01-Projects/demo/Memory/decisions",
                ),
                recall_unit(
                    "error:linked",
                    "01-Projects/demo/Memory/pitfalls",
                    memory_type="error",
                    title="修复隐藏的文件定位问题",
                    summary="路径定位失败后改用真实目录",
                    terms=["路径", "定位"],
                    source_note="note:01-Projects/demo/Memory/pitfalls",
                ),
            ],
            "_graph": {
                "nodes": [],
                "edges": [
                    {
                        "source": "decision:direct",
                        "target": "note:01-Projects/demo/Memory/decisions",
                        "relation": "recorded_in",
                    },
                    {
                        "source": "note:01-Projects/demo/Memory/decisions",
                        "target": "note:01-Projects/demo/Memory/pitfalls",
                        "relation": "links_to",
                    },
                    {
                        "source": "error:linked",
                        "target": "note:01-Projects/demo/Memory/pitfalls",
                        "relation": "recorded_in",
                    },
                ],
            },
        }

        results = recall(
            "Obsidian 中文 主存储",
            index,
            project="demo",
            limit=5,
        )

        self.assertEqual(results[0]["id"], "decision:direct")
        linked = next(item for item in results if item["id"] == "error:linked")
        self.assertGreater(linked["score"], 0)
        self.assertEqual(linked["match_kind"], "graph")

    def test_project_filter_excludes_other_projects(self):
        with tempfile.TemporaryDirectory() as vault:
            write_fixture_vault(vault)
            rebuild_vault_knowledge_indexes({"vault_path": vault})
            index = load_recall_index(vault)

            results = recall("Obsidian 中文 主存储", index, project="other", limit=3)

            self.assertEqual(results, [])

    def test_recall_deduplicates_same_memory_from_multiple_sources(self):
        index = {
            "schema_version": "2.0",
            "units": [
                recall_unit("decision:1", "01-Projects/demo/Memory/decisions"),
                recall_unit("decision:2", "05-Agent-Memory/imported-decisions"),
            ]
        }

        results = recall("Obsidian 中文 主存储", index, project="demo", limit=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["path"], "01-Projects/demo/Memory/decisions")

    def test_unrelated_typed_memory_is_not_returned_without_graph_link(self):
        index = {
            "schema_version": "2.0",
            "units": [
                recall_unit(
                    "error:unrelated",
                    "01-Projects/demo/Memory/pitfalls",
                    memory_type="error",
                    title="网络超时",
                    summary="远程 API 请求超时",
                    terms=["网络", "超时", "api"],
                )
            ]
        }

        results = recall("Obsidian 中文 主存储", index, project="demo")

        self.assertEqual(results, [])

    def test_recall_rejects_candidate_path_type_and_inactive_status(self):
        index = {
            "schema_version": "2.0",
            "units": [
                {
                    **recall_unit(
                        "memory-candidate:bad",
                        "04-Feedback/_memory-candidates/用户名",
                    ),
                    "type": "memory-candidate",
                    "status": "active",
                },
                {
                    **recall_unit(
                        "decision:candidate-path",
                        "04-Feedback/_memory-candidates/伪装决定",
                    ),
                    "status": "active",
                },
                {
                    **recall_unit(
                        "decision:superseded",
                        "01-Projects/demo/Memory/decisions",
                    ),
                    "status": "superseded",
                },
                recall_unit(
                    "decision:active",
                    "01-Projects/demo/Memory/decisions",
                ),
                recall_unit(
                    "decision:cleanup",
                    "04-Feedback/_cleanup-backups/伪装决定",
                    title="Obsidian cleanup backup lure",
                    summary="Obsidian 中文 主存储 cleanup lure",
                ),
                recall_unit(
                    "decision:profile",
                    "05-Agent-Memory/codex-profile/伪装决定",
                    title="Obsidian profile lure",
                    summary="Obsidian 中文 主存储 profile lure",
                ),
            ],
        }

        results = recall("Obsidian 中文 主存储", index, project="demo", limit=8)

        self.assertEqual([item["id"] for item in results], ["decision:active"])

    def test_recall_rejects_session_paths_from_old_or_malicious_indexes(self):
        direct_session = recall_unit(
            "decision:session-direct",
            "01-Projects/demo/Memory/sessions/old-session",
            title="Old session result",
            summary="Obsidian 中文 主存储 from stale session evidence",
        )
        graph_session = recall_unit(
            "decision:session-graph",
            "01-Projects\\demo\\Memory\\sessions\\linked-session",
            title="Graph-only stale session result",
            summary="This must not be graph-expanded",
            terms=["unrelated"],
            source_note="note:session-graph",
        )
        aggregate = recall_unit(
            "decision:aggregate",
            "01-Projects/demo/Memory/decisions",
        )
        index = {
            "schema_version": "2.0",
            "units": [direct_session, graph_session, aggregate],
            "_graph": {
                "edges": [
                    {
                        "source": "note:01-Projects/demo/Memory/decisions",
                        "target": "note:session-graph",
                        "relation": "links_to",
                    }
                ]
            },
        }

        results = recall("Obsidian 中文 主存储", index, project="demo", limit=8)

        self.assertEqual([item["id"] for item in results], ["decision:aggregate"])

    def test_load_recall_index_requires_schema_2_and_list_units(self):
        invalid_payloads = (
            {"schema_version": "1.0", "units": []},
            {"schema_version": "2.0", "units": {}},
            {"units": []},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as tmp:
                    path = os.path.join(tmp, "recall-index.json")
                    with open(path, "w", encoding="utf-8") as handle:
                        json.dump(payload, handle)

                    with self.assertRaisesRegex(ValueError, "schema|units"):
                        load_recall_index(path)

    def test_recall_rejects_incomplete_or_forged_runtime_units(self):
        valid = recall_unit(
            "decision:valid-runtime",
            "01-Projects/demo/Memory/decisions",
            title="Valid runtime record",
            summary="runtimeprobe verified formal memory",
            terms=["runtimeprobe"],
        )
        missing_refs = {
            **recall_unit(
                "decision:missing-refs",
                "01-Projects/demo/Memory/decisions",
                title="Missing source refs",
                summary="runtimeprobe incomplete source evidence",
                terms=["runtimeprobe"],
            ),
            "source_refs": [],
        }
        forged_revision = {
            **recall_unit(
                "decision:forged",
                "01-Projects/demo/Memory/decisions",
                title="Forged revision",
                summary="runtimeprobe forged record",
                terms=["runtimeprobe"],
            ),
            "revision": "0" * 64,
        }
        inconsistent_scope = recall_unit(
            "decision:scope",
            "01-Projects/demo/Memory/decisions",
            title="Inconsistent scope",
            summary="runtimeprobe inconsistent scope",
            terms=["runtimeprobe"],
        )
        inconsistent_scope["scope"] = "global"
        inconsistent_scope["revision"] = memory_revision(inconsistent_scope)

        results = recall(
            "runtimeprobe",
            {
                "schema_version": "2.0",
                "units": [
                    missing_refs,
                    forged_revision,
                    inconsistent_scope,
                    valid,
                ],
            },
            project="demo",
            limit=10,
        )

        self.assertEqual([item["id"] for item in results], [valid["id"]])

    def test_project_filter_canonicalizes_legacy_alias(self):
        unit = recall_unit(
            "decision:canonical-project",
            "01-Projects/agent-memory-beacon/Memory/decisions",
            title="Canonical project route",
            summary="aliasprobe uses the canonical project slug",
            terms=["aliasprobe"],
            project="agent-memory-beacon",
        )

        results = recall(
            "aliasprobe",
            {"schema_version": "2.0", "units": [unit]},
            project="github-obsidian-knowledge-brain",
        )

        self.assertEqual([item["id"] for item in results], [unit["id"]])

    def test_equal_score_results_have_deterministic_order(self):
        first = recall_unit(
            "decision:a",
            "01-Projects/demo/Memory/decisions",
            title="Deterministic result",
            summary="orderprobe alpha detail",
            terms=["orderprobe"],
        )
        second = recall_unit(
            "decision:b",
            "01-Projects/demo/Memory/decisions",
            title="Deterministic result",
            summary="orderprobe beta detail",
            terms=["orderprobe"],
        )

        forward = recall(
            "orderprobe",
            {"schema_version": "2.0", "units": [first, second]},
            project="demo",
        )
        reverse = recall(
            "orderprobe",
            {"schema_version": "2.0", "units": [second, first]},
            project="demo",
        )

        self.assertEqual(
            [item["id"] for item in forward],
            [item["id"] for item in reverse],
        )
        self.assertEqual({item["id"] for item in forward}, {first["id"], second["id"]})

    def test_relative_score_threshold_drops_shallow_same_project_matches(self):
        strong = recall_unit(
            "error:strong",
            "01-Projects/demo/Memory/pitfalls",
            memory_type="error",
            title="path-filesystem",
            summary="pathprobe 路径文件验证失败",
            terms=["pathprobe", "路径", "文件", "验证"],
        )
        shallow = recall_unit(
            "error:shallow",
            "01-Projects/demo/Memory/pitfalls",
            memory_type="error",
            title="other",
            summary="另一个问题修复完成",
            terms=["修复"],
        )

        results = recall(
            "pathprobe 路径文件失败怎么修复",
            {"schema_version": "2.0", "units": [shallow, strong]},
            project="demo",
            type_boosts={"error": 8},
            relative_score_threshold=0.8,
        )

        self.assertEqual([item["id"] for item in results], [strong["id"]])

    def test_relative_threshold_applies_after_graph_expansion(self):
        direct = recall_unit(
            "decision:direct-strong",
            "01-Projects/demo/Memory/decisions",
            title="Strong direct result",
            summary="graphprobe exact direct memory",
            terms=["graphprobe", "exact", "direct"],
        )
        graph_only = recall_unit(
            "decision:graph-weak",
            "01-Projects/demo/Memory/pitfalls",
            memory_type="error",
            title="Weak graph result",
            summary="unrelated graph memory",
            terms=["unrelated"],
        )
        index = {
            "schema_version": "2.0",
            "units": [direct, graph_only],
            "_graph": {
                "edges": [
                    {
                        "source": "note:01-Projects/demo/Memory/decisions",
                        "target": "note:01-Projects/demo/Memory/pitfalls",
                        "relation": "links_to",
                    }
                ]
            },
        }

        results = recall(
            "graphprobe exact direct",
            index,
            project="demo",
            relative_score_threshold=0.8,
        )

        self.assertEqual([item["id"] for item in results], [direct["id"]])

    def test_rare_specific_term_survives_common_term_distractors(self):
        reconnect = recall_unit(
            "error:reconnect",
            "01-Projects/demo/Memory/pitfalls",
            memory_type="error",
            title="api-network",
            summary="WebSocket reconnect 循环已通过关闭不兼容传输修复",
            terms=["reconnect", "websocket", "传输"],
        )
        distractors = [
            recall_unit(
                f"error:generic-{index}",
                "01-Projects/demo/Memory/pitfalls",
                memory_type="error",
                title=f"generic-{index}",
                summary=f"Hook JSON 常规检查记录 {index}",
                terms=["hook", "json"],
            )
            for index in range(5)
        ]

        results = recall(
            "reconnect 后 Hook JSON 报错",
            {"schema_version": "2.0", "units": [*distractors, reconnect]},
            project="demo",
            type_boosts={"error": 8},
            relative_score_threshold=0.8,
        )

        self.assertIn(reconnect["id"], [item["id"] for item in results])

    def test_compound_machine_term_matches_its_query_component(self):
        workflow = recall_unit(
            "workflow:pensive",
            "05-Agent-Memory/workflow-rules",
            memory_type="workflow",
            title="pensive_review_then_fix",
            summary="发现问题后直接修复并验证",
            terms=["pensive_review_then_fix", "直接修复", "验证"],
        )
        skill = recall_unit(
            "skill:pensive",
            "05-Agent-Memory/skill-routing-rules",
            memory_type="skill",
            title="pensive",
            summary="用于代码审查",
            terms=["pensive", "审查"],
        )

        results = recall(
            "pensive 检查发现问题后怎么办",
            {"schema_version": "2.0", "units": [skill, workflow]},
            project="demo",
            type_boosts={"workflow": 3, "skill": 2},
            relative_score_threshold=0.8,
        )

        self.assertEqual(results[0]["id"], workflow["id"])

    def test_hybrid_recall_uses_type_and_time_for_latest_error_query(self):
        older_error = recall_unit(
            "error:older",
            "01-Projects/demo/Memory/pitfalls",
            memory_type="error",
            title="path-filesystem",
            summary="错误：路径定位失败后改用真实目录",
            terms=["错误", "失败", "路径", "定位"],
            date="2026-06-01",
        )
        latest_error = recall_unit(
            "error:latest",
            "01-Projects/demo/Memory/pitfalls",
            memory_type="error",
            title="api-network",
            summary="错误：连接复位后重新建立传输",
            terms=["错误", "连接", "传输"],
            date="2026-07-18",
        )
        newer_decision = recall_unit(
            "decision:newer",
            "01-Projects/demo/Memory/decisions",
            title="保留本地索引",
            summary="继续使用本地确定性索引",
            terms=["本地", "索引"],
            date="2026-07-19",
        )

        results = recall(
            "最近一次错误是什么",
            {
                "schema_version": "2.0",
                "units": [newer_decision, older_error, latest_error],
            },
            project="demo",
        )

        self.assertEqual([item["id"] for item in results], [latest_error["id"]])
        self.assertEqual(results[0]["retrieval_channels"], ["type", "temporal"])
        self.assertEqual(results[0]["retrieval_evidence"]["temporal"]["mode"], "latest")

    def test_hybrid_recall_rejects_repair_request_without_specific_subject(self):
        decision = recall_unit(
            "decision:generic-repair",
            "01-Projects/demo/Memory/decisions",
            title="修复已知问题",
            summary="遇到问题后完成修复并验证",
            terms=["修复", "问题", "验证"],
        )
        error = recall_unit(
            "error:generic-repair",
            "01-Projects/demo/Memory/pitfalls",
            memory_type="error",
            title="问题修复记录",
            summary="另一个问题已经修复",
            terms=["修复", "问题"],
        )

        results = recall(
            "修复一下这个问题",
            {"schema_version": "2.0", "units": [decision, error]},
            project="demo",
        )

        self.assertEqual(results, [])

    def test_hybrid_recall_limits_latest_specific_error_to_matching_subject(self):
        older_path_error = recall_unit(
            "error:path-older",
            "01-Projects/demo/Memory/pitfalls",
            memory_type="error",
            title="pathprobe old failure",
            summary="pathprobe 错误使用了旧路径",
            terms=["pathprobe", "错误", "路径"],
            date="2026-07-10",
        )
        latest_path_error = recall_unit(
            "error:path-latest",
            "01-Projects/demo/Memory/pitfalls",
            memory_type="error",
            title="pathprobe current failure",
            summary="pathprobe 错误已定位真实路径",
            terms=["pathprobe", "错误", "路径"],
            date="2026-07-18",
        )
        unrelated_newer_error = recall_unit(
            "error:network-newer",
            "01-Projects/demo/Memory/pitfalls",
            memory_type="error",
            title="api-network",
            summary="网络错误已恢复连接",
            terms=["网络", "错误", "连接"],
            date="2026-07-19",
        )

        results = recall(
            "最近一次 pathprobe 错误",
            {
                "schema_version": "2.0",
                "units": [
                    unrelated_newer_error,
                    older_path_error,
                    latest_path_error,
                ],
            },
            project="demo",
        )

        self.assertEqual([item["id"] for item in results], [latest_path_error["id"]])
        self.assertEqual(
            results[0]["retrieval_channels"],
            ["lexical", "structured", "type", "temporal"],
        )

    def test_hybrid_recall_inventory_query_returns_only_requested_memory_type(self):
        preference = recall_unit(
            "preference:language",
            "05-Agent-Memory/personal-memory",
            memory_type="preference",
            title="输出语言",
            summary="复杂说明默认使用中文",
            terms=["中文", "说明"],
            project="",
            scope="global",
        )
        decision = recall_unit(
            "decision:storage",
            "01-Projects/demo/Memory/decisions",
            title="主存储选择",
            summary="我决定使用 Markdown 保存正式事实",
            terms=["markdown", "存储"],
        )

        results = recall(
            "我有哪些个人偏好",
            {"schema_version": "2.0", "units": [decision, preference]},
            allowed_projects={"demo"},
        )

        self.assertEqual([item["id"] for item in results], [preference["id"]])
        self.assertEqual(results[0]["retrieval_channels"], ["type"])

    def test_hybrid_recall_exposes_direct_and_graph_rank_evidence(self):
        direct = recall_unit(
            "decision:hybrid-direct",
            "01-Projects/demo/Memory/decisions",
            title="保留 Obsidian Markdown",
            summary="Obsidian 中文主存储",
            terms=["obsidian", "markdown", "中文", "主存储"],
        )
        linked = recall_unit(
            "error:hybrid-linked",
            "01-Projects/demo/Memory/pitfalls",
            memory_type="error",
            title="frontmatter-parse",
            summary="修复元数据解析",
            terms=["元数据", "解析"],
        )
        index = {
            "schema_version": "2.0",
            "units": [linked, direct],
            "_graph": {
                "edges": [
                    {"source": direct["id"], "target": direct["source_note"], "relation": "recorded_in"},
                    {"source": direct["source_note"], "target": linked["source_note"], "relation": "links_to"},
                    {"source": linked["id"], "target": linked["source_note"], "relation": "recorded_in"},
                ]
            },
        }

        results = recall("Obsidian 中文 主存储", index, project="demo")

        by_id = {item["id"]: item for item in results}
        self.assertIn("lexical", by_id[direct["id"]]["retrieval_channels"])
        self.assertEqual(by_id[direct["id"]]["retrieval_evidence"]["lexical"]["rank"], 1)
        self.assertEqual(by_id[linked["id"]]["retrieval_channels"], ["graph"])
        self.assertEqual(
            by_id[linked["id"]]["retrieval_evidence"]["graph"]["via"],
            linked["source_note"],
        )

    def test_hybrid_fusion_rewards_multi_channel_match(self):
        matching_error = recall_unit(
            "error:multi-channel",
            "01-Projects/demo/Memory/pitfalls",
            memory_type="error",
            title="pathprobe failure",
            summary="pathprobe 失败后改用规范路径",
            terms=["pathprobe", "失败", "路径"],
            date="2026-07-18",
        )
        lexical_only = recall_unit(
            "decision:lexical-only",
            "01-Projects/demo/Memory/decisions",
            title="pathprobe pathprobe pathprobe",
            summary="pathprobe 路径方案",
            terms=["pathprobe", "路径", "方案"],
            date="2026-07-19",
        )

        results = recall(
            "最近的 pathprobe 错误",
            {"schema_version": "2.0", "units": [lexical_only, matching_error]},
            project="demo",
        )

        self.assertEqual(results[0]["id"], matching_error["id"])
        self.assertEqual(
            results[0]["retrieval_channels"],
            ["lexical", "structured", "type", "temporal"],
        )
        self.assertEqual(results[1]["retrieval_channels"], ["lexical", "structured"])

    def test_human_readable_results_explain_retrieval_channels(self):
        unit = recall_unit(
            "workflow:explain",
            "05-Agent-Memory/workflow-rules",
            memory_type="workflow",
            title="pensive_review_then_fix",
            summary="pensive 检查后直接修复",
            terms=["pensive", "检查", "修复"],
        )
        results = recall(
            "pensive 检查后怎么办",
            {"schema_version": "2.0", "units": [unit]},
            project="demo",
        )

        rendered = format_results(results)

        self.assertIn("channels: lexical, structured", rendered)

    def test_results_explain_why_they_were_recalled_and_their_authority(self):
        from memory_schema import memory_revision

        unit = recall_unit(
            "decision:explain",
            "01-Projects/demo/Memory/decisions",
            title="Obsidian 使用 Markdown 主存储",
            summary="中文笔记继续使用 Markdown",
            terms=["obsidian", "markdown", "中文"],
        )
        unit.update(
            {
                "authority_role": "canonical",
                "authority_owner": "memory architecture",
                "canonical_source": "repo:references/architecture.md",
            }
        )
        unit["revision"] = memory_revision(unit)

        results = recall(
            "Obsidian Markdown 中文",
            {"schema_version": "2.0", "units": [unit]},
            project="demo",
        )

        self.assertIn("内容关键词匹配", results[0]["why_recalled"])
        self.assertEqual(results[0]["authority"]["role"], "canonical")
        self.assertEqual(
            results[0]["authority"]["route"],
            "repo:references/architecture.md",
        )
        rendered = format_results(results)
        self.assertIn("why_recalled: 内容关键词匹配", rendered)
        self.assertIn("authority: canonical", rendered)

    def test_authority_breaks_only_equal_relevance_ties(self):
        from memory_schema import memory_revision

        plain_equal = recall_unit(
            "decision:plain-equal",
            "01-Projects/demo/Memory/decisions",
            title="缓存使用内容哈希",
            summary="缓存键使用内容哈希",
            terms=["缓存", "内容哈希"],
        )
        canonical_equal = recall_unit(
            "decision:canonical-equal",
            "01-Projects/demo/Memory/decisions",
            title="缓存使用内容哈希",
            summary="缓存键使用内容哈希",
            terms=["缓存", "内容哈希"],
        )
        canonical_equal.update(
            {
                "authority_role": "canonical",
                "authority_owner": "cache contract",
                "canonical_source": "repo:references/cache-contract.md",
            }
        )
        canonical_equal["revision"] = memory_revision(canonical_equal)

        tied = recall(
            "缓存 内容哈希",
            {"schema_version": "2.0", "units": [plain_equal, canonical_equal]},
            project="demo",
        )
        self.assertEqual(tied[0]["id"], canonical_equal["id"])

        strongly_relevant = recall_unit(
            "decision:strong-content",
            "01-Projects/demo/Memory/decisions",
            title="缓存失效使用内容哈希和版本号",
            summary="缓存失效时同时校验内容哈希和版本号",
            terms=["缓存", "失效", "内容哈希", "版本号"],
        )
        weak_authority = recall_unit(
            "decision:weak-authority",
            "01-Projects/demo/Memory/decisions",
            title="缓存设置",
            summary="缓存使用默认设置",
            terms=["缓存"],
        )
        weak_authority.update(
            {
                "authority_role": "canonical",
                "authority_owner": "cache settings",
                "canonical_source": "repo:references/cache-settings.md",
            }
        )
        weak_authority["revision"] = memory_revision(weak_authority)

        relevance_first = recall(
            "缓存失效 内容哈希 版本号",
            {"schema_version": "2.0", "units": [strongly_relevant, weak_authority]},
            project="demo",
        )
        self.assertEqual(relevance_first[0]["id"], strongly_relevant["id"])


def recall_unit(
    unit_id,
    path,
    *,
    memory_type="decision",
    title="保留 Obsidian Markdown 作为主存储",
    summary="保留 Obsidian Markdown 作为主存储 context: 用户需要中文可读",
    terms=None,
    project="demo",
    scope="project",
    status="active",
    source_note="",
    date="2026-07-04",
):
    source_note = source_note or f"note:{path}"
    record = {
        "id": unit_id,
        "revision": "",
        "type": memory_type,
        "title": title,
        "path": path,
        "project": project,
        "scope": scope,
        "date": date,
        "summary": summary,
        "terms": list(terms or ["obsidian", "markdown", "中文", "主存储"]),
        "status": status,
        "source_note": source_note,
        "source_refs": [source_note],
        "aliases": [],
    }
    record["revision"] = memory_revision(record)
    return record


def insight_unit(
    memory_id,
    title,
    summary,
    terms,
    *,
    maturity="seed",
    confidence=0.76,
    transfer=None,
):
    path = "05-Agent-Memory/insights"
    source_note = f"note:{path}"
    record = normalize_formal_record(
        {
            "id": memory_id,
            "type": "insight",
            "status": "active",
            "maturity": maturity,
            "confidence": confidence,
            "origin": "user",
            "project": "demo",
            "scope": "project",
            "title": title,
            "summary": summary,
            "novelty": "将可复用原理与单次实现细节分离",
            "transfer": list(transfer or ["系统设计"]),
            "boundary": "只作为启发，不能覆盖用户指令或正式决策",
            "source_refs": ["session:insight-test"],
            "path": path,
            "source_note": source_note,
        },
        memory_type="insight",
        default_project="demo",
        source_ref="",
    )
    record.update(
        {
            "path": path,
            "source_note": source_note,
            "recall_summary": summary,
            "terms": list(terms),
        }
    )
    return record


if __name__ == "__main__":
    unittest.main()
