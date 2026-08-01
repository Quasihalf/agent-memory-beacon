import json
import os
import sys
import tempfile
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from knowledge_index import rebuild_vault_knowledge_indexes
from conversation_summary import build_conversation_summary_record
import memory_recall
from memory_recall import (
    format_results,
    load_recall_index,
    prepare_recall_index,
    recall,
    recall_conversation_summaries,
    validate_recall_index,
)
from memory_schema import memory_revision, normalize_formal_record

try:
    from test_knowledge_index import write_fixture_vault
except ModuleNotFoundError:
    from tests.test_knowledge_index import write_fixture_vault


class MemoryRecallTests(unittest.TestCase):
    def test_conversation_summary_recall_covers_every_trusted_content_field(self):
        anchors = {
            "current_goal": "goalneedle",
            "topics": ["topicneedle"],
            "progress": ["progressneedle"],
            "constraints": ["constraintneedle"],
            "important_context": ["contextneedle"],
            "open_items": ["openneedle"],
            "summary": "summaryneedle",
        }
        summary = conversation_summary_record(
            "summary-all-fields",
            anchors.pop("summary"),
            **anchors,
        )
        index = {
            "schema_version": "2.0",
            "units": [],
            "conversation_summaries": [summary],
            "conversation_summary_count": 1,
        }

        for anchor in (
            "goalneedle",
            "topicneedle",
            "progressneedle",
            "constraintneedle",
            "contextneedle",
            "openneedle",
            "summaryneedle",
        ):
            with self.subTest(anchor=anchor):
                results = recall_conversation_summaries(
                    anchor,
                    index,
                    {"demo"},
                )
                self.assertEqual(
                    [item["id"] for item in results],
                    [summary["id"]],
                )

    def test_conversation_summary_recall_covers_last_allowed_list_items(self):
        tail_anchors = {
            "topics": "omegaqztp",
            "progress": "betaxypr",
            "constraints": "gammauvcs",
            "important_context": "deltamnct",
            "open_items": "epsilonop",
        }
        fields = {
            field: [
                *(f"{field}unique{index}" for index in range(7)),
                anchor,
            ]
            for field, anchor in tail_anchors.items()
        }
        summary = conversation_summary_record(
            "summary-tail-items",
            "Tail item lexical coverage",
            **fields,
        )
        index = {
            "schema_version": "2.0",
            "units": [],
            "conversation_summaries": [summary],
            "conversation_summary_count": 1,
        }

        self.assertLessEqual(len(summary["search_terms"]), 24)
        self.assertNotIn("epsilonop", summary["search_terms"])
        for anchor in tail_anchors.values():
            with self.subTest(anchor=anchor):
                results = recall_conversation_summaries(
                    anchor,
                    index,
                    {"demo"},
                )
                self.assertEqual(
                    [item["id"] for item in results],
                    [summary["id"]],
                )

    def test_conversation_summary_recall_requires_concrete_lexical_content(self):
        summary = conversation_summary_record(
            "summary-concrete",
            "用内容哈希绑定滚动摘要代次",
            topics=["内容哈希", "代次绑定"],
        )
        index = {"schema_version": "2.0", "units": [], "conversation_summaries": [summary],
                 "conversation_summary_count": 1}

        self.assertEqual(
            recall_conversation_summaries("给我最近的会话摘要", index, {"demo"}),
            [],
        )
        self.assertEqual(
            recall_conversation_summaries("我们之前聊了什么", index, {"demo"}),
            [],
        )
        status = conversation_summary_record(
            "summary-generic-status",
            "The latest summary status remains active",
            topics=["retrieval status"],
        )
        self.assertEqual(
            recall_conversation_summaries(
                "latest summary status",
                {
                    "schema_version": "2.0",
                    "units": [],
                    "conversation_summaries": [status],
                    "conversation_summary_count": 1,
                },
                {"demo"},
            ),
            [],
        )
        english = conversation_summary_record(
            "summary-type-only",
            "Build deterministic summaries for long conversations",
            topics=["conversation summaries"],
        )
        self.assertEqual(
            recall_conversation_summaries(
                "show summaries",
                {
                    "schema_version": "2.0",
                    "units": [],
                    "conversation_summaries": [english],
                    "conversation_summary_count": 1,
                },
                {"demo"},
            ),
            [],
        )
        results = recall_conversation_summaries(
            "内容哈希如何绑定摘要代次",
            index,
            {"demo"},
        )
        self.assertEqual([item["id"] for item in results], [summary["id"]])
        english = conversation_summary_record(
            "summary-english-domain",
            "Quartzcheckpoint binds content hash generations",
            topics=["quartzcheckpoint"],
        )
        english_results = recall_conversation_summaries(
            "How does quartzcheckpoint bind generations?",
            {
                "schema_version": "2.0",
                "units": [],
                "conversation_summaries": [english],
                "conversation_summary_count": 1,
            },
            {"demo"},
        )
        self.assertEqual(
            [item["id"] for item in english_results],
            [english["id"]],
        )

    def test_conversation_summary_generic_filter_preserves_domain_compounds(self):
        generic = conversation_summary_record(
            "summary-generic-language",
            "Latest summary status overview and 最新会话摘要状态进度",
            topics=["retrieval overview", "摘要更新"],
        )
        generic_index = {
            "schema_version": "2.0",
            "units": [],
            "conversation_summaries": [generic],
            "conversation_summary_count": 1,
        }
        for query in (
            "latest summary status",
            "show latest summary overview",
            "最新会话摘要状态进度",
            "请给我最近的会话摘要更新",
        ):
            with self.subTest(generic_query=query):
                self.assertEqual(
                    recall_conversation_summaries(
                        query,
                        generic_index,
                        {"demo"},
                    ),
                    [],
                )

        concrete = conversation_summary_record(
            "summary-concrete-language",
            (
                "状态机驱动进度条，信息论约束工作流；"
                "statechart drives progressbar in a workflow"
            ),
            topics=[
                "状态机",
                "进度条",
                "信息论",
                "工作流",
                "statechart",
                "progressbar",
                "workflow",
            ],
        )
        concrete_index = {
            "schema_version": "2.0",
            "units": [],
            "conversation_summaries": [concrete],
            "conversation_summary_count": 1,
        }
        for query in (
            "状态机摘要",
            "进度条会话摘要",
            "信息论上下文",
            "工作流摘要",
            "statechart summary",
            "progressbar conversation summary",
            "workflow summary",
        ):
            with self.subTest(concrete_query=query):
                self.assertEqual(
                    [
                        item["id"]
                        for item in recall_conversation_summaries(
                            query,
                            concrete_index,
                            {"demo"},
                        )
                    ],
                    [concrete["id"]],
                )

    def test_conversation_summary_recall_returns_at_most_one_isolated_result(self):
        first = conversation_summary_record(
            "summary-first",
            "会话摘要使用独立词法通道",
            topics=["独立词法通道"],
        )
        second = conversation_summary_record(
            "summary-second",
            "独立词法通道不能触发图谱扩展",
            topics=["独立词法通道", "图谱隔离"],
        )
        index = {
            "schema_version": "2.0",
            "units": [],
            "conversation_summaries": [first, second],
            "conversation_summary_count": 2,
            "experience_bundles": [],
            "_graph": {
                "nodes": [],
                "edges": [
                    {
                        "source": first["id"],
                        "target": second["id"],
                        "relation": "related_to",
                    }
                ],
            },
        }

        results = recall_conversation_summaries(
            "独立词法通道图谱隔离",
            index,
            {"demo"},
            limit=8,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["retrieval_channels"], ["conversation_summary"])
        self.assertEqual(
            set(results[0]["retrieval_evidence"]),
            {"conversation_summary"},
        )
        self.assertEqual(results[0]["match_kind"], "conversation_summary")
        self.assertNotIn("related_path", results[0])
        self.assertNotIn("related_experience", results[0])

    def test_conversation_summary_recall_enforces_allowed_projects(self):
        demo = conversation_summary_record(
            "summary-demo",
            "隔离项目的滚动摘要",
            topics=["项目隔离"],
        )
        other = conversation_summary_record(
            "summary-other",
            "隔离项目的滚动摘要",
            project="other",
            topics=["项目隔离"],
        )
        index = {
            "schema_version": "2.0",
            "units": [],
            "conversation_summaries": [demo, other],
            "conversation_summary_count": 2,
        }

        results = recall_conversation_summaries(
            "项目隔离滚动摘要",
            index,
            {"other"},
        )

        self.assertEqual([item["project"] for item in results], ["other"])

    def test_conversation_summary_collection_is_optional_but_strict_when_present(self):
        validate_recall_index({"schema_version": "2.0", "units": []})
        valid = conversation_summary_record(
            "summary-validation",
            "严格验证派生搜索词",
            topics=["派生搜索词"],
        )
        poisoned = dict(valid)
        poisoned["search_terms"] = [*valid["search_terms"], "caller-controlled-anchor"]
        malformed = {
            "schema_version": "2.0",
            "units": [],
            "conversation_summaries": [poisoned],
            "conversation_summary_count": 1,
        }

        with self.assertRaisesRegex(ValueError, "conversation summary"):
            validate_recall_index(malformed)
        with self.assertRaisesRegex(ValueError, "conversation summary"):
            recall_conversation_summaries(
                "caller-controlled-anchor",
                malformed,
                {"demo"},
            )

    def test_conversation_summary_ids_cannot_collide_with_formal_units(self):
        summary = conversation_summary_record(
            "summary-id-collision",
            "独立集合也必须共享 ID 命名空间",
            topics=["ID 命名空间"],
        )
        index = {
            "schema_version": "2.0",
            "units": [{"id": summary["id"]}],
            "conversation_summaries": [summary],
            "conversation_summary_count": 1,
        }

        with self.assertRaisesRegex(ValueError, "collision"):
            validate_recall_index(index)

    def test_recall_rejects_duplicate_runtime_unit_ids(self):
        matching = recall_unit(
            "decision:duplicate",
            "01-Projects/demo/Memory/decisions",
            title="duplicateprobe matching record",
            summary="duplicateprobe should identify this record",
            terms=["duplicateprobe"],
        )
        substitute = recall_unit(
            "decision:duplicate",
            "01-Projects/demo/Memory/decisions",
            title="unrelated substitute",
            summary="this record must not inherit another record's score",
            terms=["unrelated"],
        )
        index = {
            "schema_version": "2.0",
            "units": [matching, substitute],
        }

        with self.assertRaisesRegex(ValueError, "duplicate recall unit ID"):
            validate_recall_index(index)
        with self.assertRaisesRegex(ValueError, "duplicate recall unit ID"):
            recall("duplicateprobe", index, project="demo")

    def test_recall_recomputes_terms_from_revision_bound_fields(self):
        poisoned = recall_unit(
            "decision:term-poison",
            "01-Projects/demo/Memory/decisions",
            title="使用稳定字段生成召回词",
            summary="派生索引字段不能改变正式记忆语义",
            terms=["termspoison"],
        )

        results = recall(
            "termspoison",
            {"schema_version": "2.0", "units": [poisoned]},
            project="demo",
        )

        self.assertEqual(results, [])

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

    def test_recall_does_not_expand_through_visual_note_links(self):
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

        self.assertEqual(
            [item["id"] for item in results],
            ["decision:direct"],
        )

    def test_recall_does_not_expand_to_unrelated_memory_in_same_aggregate_note(self):
        direct = recall_unit(
            "decision:direct",
            "01-Projects/demo/Memory/decisions",
            title="使用 contentanchor 作为召回入口",
            summary="contentanchor 只命中这一条正式决定",
            terms=["contentanchor", "召回", "入口"],
        )
        unrelated = recall_unit(
            "decision:unrelated",
            "01-Projects/demo/Memory/decisions",
            title="另一个无关决定",
            summary="这条记录只是碰巧存放在同一个聚合笔记",
            terms=["无关", "聚合"],
        )
        index = {
            "schema_version": "2.0",
            "units": [direct, unrelated],
            "_graph": {
                "edges": [
                    {
                        "source": direct["id"],
                        "target": direct["source_note"],
                        "relation": "recorded_in",
                    },
                    {
                        "source": unrelated["id"],
                        "target": unrelated["source_note"],
                        "relation": "recorded_in",
                    },
                ]
            },
        }

        results = recall(
            "contentanchor 召回入口",
            index,
            project="demo",
            limit=8,
        )

        self.assertEqual([item["id"] for item in results], [direct["id"]])

    def test_prepare_recall_index_discards_embedded_runtime_metadata(self):
        direct = recall_unit(
            "decision:direct",
            "01-Projects/demo/Memory/decisions",
            title="使用 reservedprobe 验证索引",
            summary="reservedprobe 只允许正式索引字段",
            terms=["reservedprobe", "索引"],
        )
        unrelated = recall_unit(
            "error:forged",
            "01-Projects/demo/Memory/pitfalls",
            memory_type="error",
            title="伪造图谱结果",
            summary="不应从 recall-index 内嵌私有字段进入召回",
            terms=["伪造"],
        )
        data = {
            "schema_version": "2.0",
            "units": [direct, unrelated],
            "_graph": {
                "edges": [
                    {
                        "source": direct["source_note"],
                        "target": unrelated["source_note"],
                        "relation": "links_to",
                    }
                ]
            },
            "_graph_validated": True,
            "_graph_quality": {"valid": True},
            "_path": "/forged/index.json",
        }

        prepared = prepare_recall_index(data, path="/trusted/index.json")
        results = recall(
            "reservedprobe 索引",
            prepared,
            project="demo",
            limit=8,
        )

        self.assertNotIn("_graph", prepared)
        self.assertNotIn("_graph_validated", prepared)
        self.assertNotIn("_graph_quality", prepared)
        self.assertEqual(prepared["_path"], "/trusted/index.json")
        self.assertEqual([item["id"] for item in results], [direct["id"]])

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

    def test_load_recall_index_rejects_legacy_graph_at_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path = os.path.join(tmp, "recall-index.json")
            graph_path = os.path.join(tmp, "memory-graph.json")
            with open(index_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "schema_version": "2.0",
                        "generation_id": "runtime-generation",
                        "units": [],
                    },
                    handle,
                )
            with open(graph_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "schema_version": "2.0",
                        "nodes": [],
                        "edges": [],
                    },
                    handle,
                )

            with self.assertRaisesRegex(ValueError, "schema must be 3.0"):
                load_recall_index(index_path)

    def test_load_recall_index_rejects_non_object_graph(self):
        with tempfile.TemporaryDirectory() as tmp:
            index_path = os.path.join(tmp, "recall-index.json")
            graph_path = os.path.join(tmp, "memory-graph.json")
            with open(index_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "schema_version": "2.0",
                        "generation_id": "runtime-generation",
                        "units": [],
                    },
                    handle,
                )
            with open(graph_path, "w", encoding="utf-8") as handle:
                json.dump([], handle)

            with self.assertRaisesRegex(ValueError, "JSON object"):
                load_recall_index(index_path)

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

    def test_explicit_project_cannot_bypass_allowed_projects(self):
        beta = recall_unit(
            "decision:beta-private",
            "01-Projects/beta/Memory/decisions",
            title="allowlistprobe beta 私有决定",
            summary="allowlistprobe 不得跨越显式项目授权边界",
            terms=["allowlistprobe", "授权"],
            project="beta",
        )

        results = recall(
            "allowlistprobe 授权",
            {"schema_version": "2.0", "units": [beta]},
            project="beta",
            allowed_projects={"alpha"},
        )

        self.assertEqual(results, [])

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
        graph = semantic_graph(
            [direct, linked],
            [semantic_edge(direct, "depends_on", linked)],
        )
        index = {
            "schema_version": "2.0",
            "units": [linked, direct],
            "_graph": graph,
        }

        results = recall("Obsidian 中文 主存储", index, project="demo")

        by_id = {item["id"]: item for item in results}
        self.assertIn("lexical", by_id[direct["id"]]["retrieval_channels"])
        self.assertEqual(by_id[direct["id"]]["retrieval_evidence"]["lexical"]["rank"], 1)
        self.assertEqual(by_id[linked["id"]]["retrieval_channels"], ["graph"])
        self.assertEqual(
            by_id[linked["id"]]["retrieval_evidence"]["graph"]["via"],
            "depends_on",
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

    def test_semantic_graph_recall_uses_at_most_two_revision_bound_hops(self):
        direct = recall_unit(
            "decision:graph-anchor",
            "01-Projects/demo/Memory/decisions",
            title="使用关系合同约束记忆图谱",
            summary="graphcontract 图谱关系必须有明确类型",
            terms=["graphcontract", "图谱", "关系", "合同"],
        )
        middle = recall_unit(
            "workflow:graph-validator",
            "05-Agent-Memory/workflow-rules",
            memory_type="workflow",
            title="写入前验证关系",
            summary="每条边写入前检查类型和来源",
            terms=["验证", "边", "来源"],
        )
        target = recall_unit(
            "error:graph-provenance",
            "01-Projects/demo/Memory/pitfalls",
            memory_type="error",
            title="关系缺少来源版本",
            summary="补齐 revision 绑定后恢复可信召回",
            terms=["revision", "来源", "可信"],
        )
        too_far = recall_unit(
            "personal:graph-third-hop",
            "05-Agent-Memory/personal-memory",
            memory_type="preference",
            title="第三跳无关偏好",
            summary="不应被两跳召回扩展返回",
            terms=["第三跳", "无关"],
        )

        units = [direct, middle, target, too_far]
        graph = semantic_graph(
            units,
            [
                semantic_edge(
                    direct,
                    "supports",
                    middle,
                ),
                semantic_edge(
                    middle,
                    "depends_on",
                    target,
                ),
                semantic_edge(
                    target,
                    "supports",
                    too_far,
                ),
            ],
        )

        results = recall(
            "graphcontract 图谱关系合同",
            {
                "schema_version": "2.0",
                "units": units,
                "_graph": graph,
            },
            project="demo",
            limit=8,
        )

        by_id = {item["id"]: item for item in results}
        self.assertIn(middle["id"], by_id)
        self.assertIn(target["id"], by_id)
        self.assertNotIn(too_far["id"], by_id)
        self.assertEqual(
            [step["relation"] for step in by_id[target["id"]]["related_path"]],
            ["supports", "depends_on"],
        )
        self.assertIn("关系路径", by_id[target["id"]]["why_recalled"])

    def test_high_confidence_semantic_edge_survives_runtime_relative_gate(self):
        direct = recall_unit(
            "decision:semantic-anchor",
            "01-Projects/demo/Memory/decisions",
            title="采用 semanticanchor 关系合同",
            summary="semanticanchor 建立直接内容锚点",
            terms=["semanticanchor", "关系", "合同"],
        )
        related = recall_unit(
            "workflow:semantic-related",
            "05-Agent-Memory/workflow-rules",
            memory_type="workflow",
            title="写入前验证图谱证据",
            summary="显式 supports 关系应参与运行时召回",
            terms=["图谱", "证据"],
        )
        graph = semantic_graph(
            [direct, related],
            [
                semantic_edge(
                    direct,
                    "supports",
                    related,
                    confidence=1.0,
                )
            ],
        )

        results = recall(
            "semanticanchor 关系合同",
            {
                "schema_version": "2.0",
                "units": [direct, related],
                "_graph": graph,
            },
            project="demo",
            limit=8,
            relative_score_threshold=0.8,
        )

        self.assertEqual(
            {item["id"] for item in results},
            {direct["id"], related["id"]},
        )
        by_id = {item["id"]: item for item in results}
        self.assertEqual(by_id[related["id"]]["retrieval_channels"], ["graph"])
        self.assertEqual(by_id[related["id"]]["related_seed"], direct["id"])

    def test_semantic_recall_selects_the_strongest_content_anchor(self):
        weak = recall_unit(
            "decision:a-weak-anchor",
            "01-Projects/demo/Memory/decisions",
            title="multiseedanchor weak",
            summary="only the common anchor matches",
            terms=["multiseedanchor"],
        )
        strong = recall_unit(
            "decision:z-strong-anchor",
            "01-Projects/demo/Memory/decisions",
            title="multiseedanchor precise contract",
            summary="multiseedanchor precise contract is the direct source",
            terms=["multiseedanchor", "precise", "contract"],
        )
        target = recall_unit(
            "workflow:shared-target",
            "05-Agent-Memory/workflow-rules",
            memory_type="workflow",
            title="验证共享关系目标",
            summary="只通过显式 supports 关系进入召回",
            terms=["关系", "目标"],
        )
        graph = semantic_graph(
            [weak, strong, target],
            [
                semantic_edge(weak, "supports", target),
                semantic_edge(strong, "supports", target),
            ],
        )

        results = recall(
            "multiseedanchor precise contract",
            {
                "schema_version": "2.0",
                "units": [weak, strong, target],
                "_graph": graph,
            },
            project="demo",
            relative_score_threshold=0.8,
        )

        by_id = {item["id"]: item for item in results}
        self.assertIn(target["id"], by_id)
        self.assertEqual(by_id[target["id"]]["related_seed"], strong["id"])

    def test_semantic_recall_cannot_traverse_disallowed_project(self):
        seed = recall_unit(
            "decision:alpha-seed",
            "01-Projects/alpha/Memory/decisions",
            title="isolationprobe 项目隔离入口",
            summary="isolationprobe 只允许 alpha 项目召回",
            terms=["isolationprobe", "隔离"],
            project="alpha",
        )
        bridge = recall_unit(
            "workflow:beta-secret-bridge",
            "05-Agent-Memory/workflow-rules",
            memory_type="workflow",
            title="beta 私有中间关系",
            summary="未授权项目不能成为图遍历中间节点",
            terms=["beta", "私有"],
            project="beta",
        )
        target = recall_unit(
            "workflow:alpha-target",
            "05-Agent-Memory/workflow-rules",
            memory_type="workflow",
            title="alpha 关系目标",
            summary="只有穿过 beta 项目才能到达",
            terms=["alpha", "目标"],
            project="alpha",
        )
        graph = semantic_graph(
            [seed, bridge, target],
            [
                semantic_edge(seed, "supports", bridge),
                semantic_edge(bridge, "supports", target),
            ],
        )

        results = recall(
            "isolationprobe 项目隔离入口",
            {
                "schema_version": "2.0",
                "units": [seed, bridge, target],
                "_graph": graph,
            },
            project="alpha",
            allowed_projects={"alpha"},
        )

        self.assertEqual([item["id"] for item in results], [seed["id"]])

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


def conversation_summary_record(
    session_id,
    summary,
    *,
    project="demo",
    topics=None,
    current_goal="完成滚动会话摘要召回",
    progress=None,
    constraints=None,
    important_context=None,
    open_items=None,
):
    record = build_conversation_summary_record(
        {
            "session_id": session_id,
            "date": "2026-07-31",
            "ai_title": "滚动会话摘要",
            "source_note": (
                f"01-Projects/{project}/Memory/sessions/{session_id}"
            ),
            "conversation_summary": {
                "project": project,
                "current_goal": current_goal,
                "topics": list(topics or ["滚动摘要"]),
                "progress": list(progress or []),
                "constraints": list(constraints or []),
                "important_context": list(important_context or []),
                "open_items": list(open_items or []),
                "summary": summary,
            },
        }
    )
    if record is None:
        raise AssertionError("conversation summary fixture must be valid")
    return record


def semantic_edge(source, relation, target, *, confidence=1.0):
    return {
        "source": source["id"],
        "target": target["id"],
        "relation": relation,
        "confidence": confidence,
        "evidence": [
            {
                "source_ref": source["source_refs"][0],
                "source_revision": source["revision"],
                "observed_at": "2026-07-26",
                "derivation": "formal-record",
            }
        ],
    }


def semantic_graph(units, edges):
    units_by_id = {unit["id"]: unit for unit in units}
    relation_fields = {
        "contradicts": "contradicts",
        "depends_on": "requires",
        "operationalized_as": "operationalized_as",
        "related_to": "related_to",
        "superseded_by": "superseded_by",
        "supports": "supports",
    }
    for edge in edges:
        source = units_by_id[edge["source"]]
        field = relation_fields[edge["relation"]]
        if field == "superseded_by":
            source[field] = edge["target"]
        else:
            values = source.setdefault(field, [])
            if edge["target"] not in values:
                values.append(edge["target"])
    for unit in units:
        unit["revision"] = memory_revision(unit)
    for edge in edges:
        source = units_by_id[edge["source"]]
        for evidence in edge["evidence"]:
            evidence["source_revision"] = source["revision"]
    return {
        "schema_version": "3.0",
        "generated_by": "test",
        "generated_at": "2026-07-26T10:00:00+08:00",
        "generation_id": "test-generation",
        "nodes": [
            {
                "id": unit["id"],
                "type": "memory",
                "kind": unit["type"],
                "label": unit["title"],
                "path": unit["path"],
                "project": unit["project"],
                "date": unit["date"],
                "revision": unit["revision"],
                "source_refs": unit["source_refs"],
                "resolved": True,
            }
            for unit in units
        ],
        "edges": edges,
    }


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
