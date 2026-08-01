import os
import sys
import unittest


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(ROOT, "scripts")
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

from experience_memory import build_experience_bundles
from knowledge_index import build_memory_graph, extract_terms
from memory_recall import recall
from memory_schema import normalize_formal_record


class ExperienceMemoryTests(unittest.TestCase):
    def test_bundle_requires_two_records_and_two_memory_types(self):
        one_record = [formal_unit("demo-d1", "decision", "session:one")]
        one_type = [
            formal_unit("demo-d1", "decision", "session:two"),
            formal_unit("demo-d2", "decision", "session:two"),
        ]

        self.assertEqual(build_experience_bundles(one_record), [])
        self.assertEqual(build_experience_bundles(one_type), [])

    def test_bundle_keeps_exact_revision_members_without_copying_content(self):
        decision = formal_unit("demo-d1", "decision", "session:shared")
        error = formal_unit("demo-e1", "error", "session:shared")

        bundles = build_experience_bundles([error, decision])

        self.assertEqual(len(bundles), 1)
        bundle = bundles[0]
        self.assertEqual(bundle["project"], "demo")
        self.assertEqual(bundle["session_ref"], "session:shared")
        self.assertEqual(
            {(item["id"], item["revision"]) for item in bundle["members"]},
            {
                (decision["id"], decision["revision"]),
                (error["id"], error["revision"]),
            },
        )
        self.assertEqual({item["type"] for item in bundle["members"]}, {"decision", "error"})
        self.assertFalse(
            {"title", "summary", "recall_summary", "body", "text"}
            & set(bundle)
        )
        for member in bundle["members"]:
            self.assertFalse(
                {"title", "summary", "recall_summary", "body", "text"}
                & set(member)
            )

    def test_bundle_is_project_isolated_and_id_is_stable(self):
        first = formal_unit("demo-d1", "decision", "session:stable")
        second = formal_unit("demo-e1", "error", "session:stable")
        other_project = formal_unit(
            "other-w1",
            "workflow",
            "session:stable",
            project="other",
        )

        first_build = build_experience_bundles([first, second, other_project])
        reordered = build_experience_bundles([second, first, other_project])
        changed = formal_unit(
            "demo-d1",
            "decision",
            "session:stable",
            title="更新后的决策",
        )
        revised = build_experience_bundles([changed, second, other_project])

        self.assertEqual(len(first_build), 1)
        self.assertEqual(first_build[0]["id"], reordered[0]["id"])
        self.assertEqual(first_build[0]["id"], revised[0]["id"])
        self.assertEqual({item["project"] for item in first_build[0]["members"]}, {"demo"})

    def test_inactive_and_candidate_records_are_not_bundled(self):
        active = formal_unit("demo-d1", "decision", "session:filtered")
        inactive = formal_unit(
            "demo-e1",
            "error",
            "session:filtered",
            status="retracted",
        )
        candidate = formal_unit(
            "demo-w1",
            "workflow",
            "session:filtered",
            path="04-Feedback/_annotation-candidates/demo-w1",
            source_kind="workflow-rules",
        )

        self.assertEqual(build_experience_bundles([active, inactive, candidate]), [])

    def test_graph_adds_part_of_experience_edges_without_formalizing_bundle(self):
        units = [
            formal_unit("demo-d1", "decision", "session:graph"),
            formal_unit("demo-e1", "error", "session:graph"),
        ]
        bundles = build_experience_bundles(units)

        graph = build_memory_graph([], {"units": units, "experience_bundles": bundles})

        bundle = bundles[0]
        node = next(item for item in graph["nodes"] if item["id"] == bundle["id"])
        self.assertEqual(node["type"], "experience")
        self.assertEqual(
            {
                (edge["source"], edge["relation"], edge["target"])
                for edge in graph["edges"]
                if edge["relation"] == "part_of_experience"
            },
            {
                ("demo-d1", "part_of_experience", bundle["id"]),
                ("demo-e1", "part_of_experience", bundle["id"]),
            },
        )
        self.assertNotIn(bundle["id"], {unit["id"] for unit in units})

    def test_explicit_anchored_experience_query_adds_companion_memory(self):
        decision = formal_unit(
            "demo-d1",
            "decision",
            "session:recall",
            title="下载采用断点续传",
            summary="大文件下载从断点继续并维护分片校验",
            terms=["下载", "断点续传", "大文件", "分片校验"],
        )
        error = formal_unit(
            "demo-e1",
            "error",
            "session:recall",
            title="连接中断后校验分片",
            summary="网络恢复后执行分片校验再继续",
            terms=["网络", "分片校验"],
        )
        index = recall_index([decision, error])

        results = recall(
            "以前怎么处理下载断点续传的完整过程",
            index,
            project="demo",
            limit=8,
        )

        by_id = {item["id"]: item for item in results}
        self.assertIn(decision["id"], by_id)
        self.assertIn(error["id"], by_id)
        self.assertIn("experience", by_id[error["id"]]["retrieval_channels"])
        self.assertEqual(
            by_id[error["id"]]["related_experience"],
            index["experience_bundles"][0]["id"],
        )

    def test_inventory_and_unanchored_queries_do_not_expand_experience(self):
        decision = formal_unit(
            "demo-d1",
            "decision",
            "session:suppress",
            title="下载采用断点续传",
            summary="大文件下载从断点继续",
            terms=["下载", "断点续传"],
        )
        error = formal_unit(
            "demo-e1",
            "error",
            "session:suppress",
            title="连接中断后校验分片",
            summary="恢复后校验分片",
            terms=["网络", "校验", "分片"],
        )
        index = recall_index([decision, error])

        inventory = recall("下载断点续传有哪些完整过程", index, project="demo")
        unanchored = recall("以前怎么处理完整过程", index, project="demo")

        self.assertNotIn(error["id"], {item["id"] for item in inventory})
        self.assertEqual(unanchored, [])

    def test_expansion_uses_at_most_two_companions_from_one_bundle(self):
        seed = formal_unit(
            "demo-d1",
            "decision",
            ["session:first", "session:second"],
            title="渲染采用离线管线",
            summary="离线渲染视频并保留帧序列",
            terms=["离线渲染", "视频", "帧序列"],
        )
        units = [
            seed,
            formal_unit("demo-e1", "error", "session:first", terms=["帧序列", "编码器"]),
            formal_unit("demo-w1", "workflow", "session:first", terms=["帧序列", "验收"]),
            formal_unit("demo-i1", "insight", "session:first", terms=["帧序列", "镜头"]),
            formal_unit("demo-e2", "error", "session:second", terms=["帧序列", "色彩"]),
        ]
        index = recall_index(units)

        results = recall("以前怎么完成离线渲染视频的完整过程", index, project="demo", limit=8)

        companions = [
            item for item in results if "experience" in item.get("retrieval_channels", [])
        ]
        self.assertLessEqual(len(companions), 2)
        self.assertEqual(len({item["related_experience"] for item in companions}), 1)

    def test_recall_rejects_stale_or_content_bearing_experience_bundle(self):
        from memory_recall import prepare_recall_index

        units = [
            formal_unit("demo-d1", "decision", "session:validate"),
            formal_unit("demo-e1", "error", "session:validate"),
        ]
        index = recall_index(units)
        index["experience_bundles"][0]["members"][0]["revision"] = "f" * 64
        index["experience_bundles"][0]["members"][0]["summary"] = "不应复制正文"

        with self.assertRaisesRegex(ValueError, "experience bundle"):
            prepare_recall_index(index)

    def test_explicit_experience_companions_survive_a_crowded_direct_result_set(self):
        seed = formal_unit(
            "demo-d-seed",
            "decision",
            "session:crowded",
            title="二维 Poisson 模型使用有限差分",
            summary="Poisson 模型采用稳定离散方案",
            terms=["poisson", "二维", "模型"],
        )
        error = formal_unit(
            "demo-e-companion",
            "error",
            "session:crowded",
            title="网格不收敛时降低松弛系数",
            summary="Poisson 网格通过残差验证收敛",
            terms=["poisson", "网格", "收敛", "残差"],
        )
        workflow = formal_unit(
            "demo-w-companion",
            "workflow",
            "session:crowded",
            title="先跑小网格再扩大模型",
            summary="Poisson 模型先验证边界条件再做完整计算",
            terms=["poisson", "边界", "验证", "小网格"],
        )
        competitors = [
            formal_unit(
                f"demo-d-direct-{number}",
                "decision",
                f"session:direct-{number}",
                title=f"Poisson 二维模型直接结果 {number}",
                summary="Poisson 二维模型的直接内容命中",
                terms=["poisson", "二维", "模型", f"结果{number}"],
            )
            for number in range(6)
        ]
        index = recall_index([seed, error, workflow, *competitors])

        results = recall(
            "以前怎么完成 Poisson 二维模型的完整过程",
            index,
            project="demo",
            limit=4,
            relative_score_threshold=0.5,
        )

        companions = [
            item for item in results if "experience" in item.get("retrieval_channels", [])
        ]
        self.assertEqual(len(results), 4)
        self.assertEqual(
            {item["id"] for item in companions},
            {error["id"], workflow["id"]},
        )
        self.assertTrue(any("lexical" in item["retrieval_channels"] for item in results))

    def test_same_session_companion_without_a_content_bridge_is_suppressed(self):
        seed = formal_unit(
            "demo-d-poisson",
            "decision",
            "session:mixed-task",
            title="二维 Poisson 模型输出电势和电场",
            summary="Poisson 仿真验证边缘电场",
            terms=["poisson", "二维", "电势", "电场"],
        )
        relevant = formal_unit(
            "demo-e-poisson",
            "error",
            "session:mixed-task",
            title="接触边界不能作为空间电荷",
            summary="修正电势边界后恢复收敛",
            terms=["电势", "边界", "收敛"],
        )
        unrelated = formal_unit(
            "demo-e-layout",
            "error",
            "session:mixed-task",
            title="强制分页造成空白页",
            summary="取消分页并绑定下一节标题",
            terms=["分页", "空白页", "标题"],
        )
        index = recall_index([seed, relevant, unrelated])

        results = recall(
            "以前怎么完成 Poisson 二维模型的完整过程",
            index,
            project="demo",
            limit=5,
        )

        result_ids = {item["id"] for item in results}
        self.assertIn(relevant["id"], result_ids)
        self.assertNotIn(unrelated["id"], result_ids)


def recall_index(units):
    return {
        "schema_version": "2.0",
        "units": units,
        "experience_bundles": build_experience_bundles(units),
    }


def formal_unit(
    memory_id,
    memory_type,
    source_refs,
    *,
    project="demo",
    status="active",
    title="正式记忆",
    summary="可复用的正式记忆",
    terms=None,
    path="",
    source_kind="",
):
    if isinstance(source_refs, str):
        source_refs = [source_refs]
    if not path:
        if memory_type in {"decision", "error"}:
            filename = "decisions" if memory_type == "decision" else "pitfalls"
            path = f"01-Projects/{project}/Memory/{filename}"
        else:
            path = {
                "workflow": "05-Agent-Memory/workflow-rules",
                "insight": "05-Agent-Memory/insights",
                "skill": "05-Agent-Memory/skill-routing-rules",
            }.get(memory_type, "05-Agent-Memory/personal-memory")
    raw = {
        "id": memory_id,
        "type": memory_type,
        "status": status,
        "project": project,
        "scope": "project",
        "title": title,
        "summary": summary,
        "date": "2026-07-22",
        "source_refs": list(source_refs),
        "path": path,
        "source_note": f"note:{path}",
        "source_kind": source_kind,
    }
    if memory_type == "insight":
        raw.update(
            {
                "maturity": "seed",
                "confidence": 0.8,
                "origin": "user",
                "novelty": "跨步骤保留任务经验",
                "transfer": ["复杂任务"],
                "boundary": "只在明确询问类似经验时使用",
            }
        )
    record = normalize_formal_record(
        raw,
        memory_type=memory_type,
        default_project=project,
        source_ref="",
    )
    record.update(
        {
            "path": path,
            "source_note": f"note:{path}",
            "source_kind": source_kind,
            "recall_summary": summary,
            "terms": list(terms or extract_terms(f"{title} {summary}", limit=60)),
        }
    )
    return record


if __name__ == "__main__":
    unittest.main()
