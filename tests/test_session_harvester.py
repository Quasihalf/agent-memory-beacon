import glob
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import session_harvester

from conversation_summary import (
    extract_rolling_summary,
    summary_revision,
)
from session_harvester import (
    append_decisions,
    append_errors_to_pitfalls,
    check_codex_profile_on_start,
    cleanup_bad_obsidian_path_artifacts,
    detect_project,
    extract_decisions,
    extract_errors,
    extract_session_summary,
    find_transcript,
    ensure_obsidian_ignore_filters,
    initialize_harvest_baseline,
    load_formal_project_records,
    load_harvest_tracking,
    load_processed_from_heartbeat,
    mark_transcript_harvested,
    main,
    process_transcript,
    read_hook_input,
    rebuild_memory_index,
    repair_generated_graph_links,
    repair_generated_vault_markdown,
    repair_obsidian_workspace,
    repair_personal_memory_graph_links,
    repair_project_memory_graph_links,
    sanitize_obsidian_markdown,
    split_markdown_frontmatter,
    stop_mode,
    write_heartbeat_document,
    write_markdown_frontmatter,
    write_session_to_vault,
)
from memory_judge import extract_memory_candidates, render_formal_memory_entry
from memory_recall import load_recall_index
from memory_schema import memory_revision, parse_active_formal_section
from error_evidence import ErrorEvidenceStateError
from safety import split_frontmatter_text
from transcript_utils import find_recent_transcripts, transcript_state_key


class SessionHarvesterTests(unittest.TestCase):
    def test_explicit_annotation_projects_route_to_their_own_aggregates(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {
                "vault_path": vault,
                "projects": ["alpha", "beta"],
                "project_keywords": {},
            }
            append_decisions(
                cfg,
                "alpha",
                [
                    {
                        "text": "Alpha 决策",
                        "context": "属于 Alpha",
                        "project": "alpha",
                    },
                    {
                        "text": "Beta 决策",
                        "context": "属于 Beta",
                        "project": "beta",
                    },
                ],
                "session-routing",
                "2026-07-18",
            )
            append_errors_to_pitfalls(
                cfg,
                "alpha",
                [
                    {
                        "type": "path-filesystem",
                        "resolution": "改用 Beta 的真实路径",
                        "project": "beta",
                    }
                ],
                "session-routing",
                "2026-07-18",
            )

            alpha = yaml.safe_load(
                read_text(
                    os.path.join(
                        vault, "01-Projects/alpha/Memory/decisions.md"
                    )
                ).split("---", 2)[1]
            )
            beta_decisions = yaml.safe_load(
                read_text(
                    os.path.join(
                        vault, "01-Projects/beta/Memory/decisions.md"
                    )
                ).split("---", 2)[1]
            )
            beta_errors = yaml.safe_load(
                read_text(
                    os.path.join(vault, "01-Projects/beta/Memory/pitfalls.md")
                ).split("---", 2)[1]
            )

            self.assertEqual([item["text"] for item in alpha["decisions"]], ["Alpha 决策"])
            self.assertEqual(
                [item["text"] for item in beta_decisions["decisions"]],
                ["Beta 决策"],
            )
            self.assertEqual(beta_errors["pitfalls"][0]["project"], "beta")

    def test_process_transcript_routes_uncertain_annotations_to_hidden_candidates(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(vault)
            transcript = os.path.join(tmp, "quality-session.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "timestamp": "2026-07-18T01:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "sess-quality-1",
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-18T01:00:00Z",
                        },
                    },
                    {
                        "timestamp": "2026-07-18T01:01:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": (
                                "[DECISION:完成全部测试并更新文档| context:任务已经结束]\n"
                                "[ERROR:type=api_auth_failure| "
                                "resolution=API Key 无效，尚未替换凭据]"
                            ),
                        },
                    },
                ],
            )
            cfg = {
                "vault_path": vault,
                "projects": [{"name": "demo", "keywords": ["demo"]}],
                "personal_memory": {"enabled": False},
                "skill_preferences": {"enabled": False},
                "workflow_memory": {"enabled": False},
                "error_evidence": {"enabled": False},
                "annotation_quality": {
                    "enabled": True,
                    "candidate_dir": "04-Feedback/_annotation-candidates",
                },
            }

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                result = process_transcript(cfg, transcript)

            candidates = glob.glob(
                os.path.join(vault, "04-Feedback/_annotation-candidates/*.md")
            )
            self.assertTrue(result)
            self.assertEqual(len(candidates), 2)
            self.assertIn("[annotation-quality] 2 candidate", buffer.getvalue())
            self.assertFalse(
                os.path.exists(
                    os.path.join(vault, "01-Projects/demo/Memory/decisions.md")
                )
            )
            self.assertFalse(
                os.path.exists(
                    os.path.join(vault, "01-Projects/demo/Memory/pitfalls.md")
                )
            )
            app = json.loads(read_text(os.path.join(vault, ".obsidian/app.json")))
            self.assertIn(
                "04-Feedback/_annotation-candidates/",
                app["userIgnoreFilters"],
            )

    def test_new_and_updated_session_records_use_schema_v2(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}
            first_decision = {"text": "使用稳定记录", "context": "支持增量采集"}
            first_error = {"type": "data-format", "resolution": "补齐 schema"}

            write_session_to_vault(
                cfg,
                "sess-schema-v2",
                "2026-07-12",
                "proj",
                {},
                [first_decision],
                [first_error],
                None,
            )

            session_path = glob.glob(
                os.path.join(vault, "01-Projects/proj/Memory/sessions/*.md")
            )[0]
            initial = yaml.safe_load(read_text(session_path).split("---", 2)[1])
            self.assertEqual(initial["memory_schema_version"], "2.0")
            initial_identity = {}
            for key, label_field in (
                ("decisions_made", "text"),
                ("errors_encountered", "type"),
            ):
                record = initial[key][0]
                self.assertRegex(record["id"], r"^(decision|error)-[0-9a-f]{16}$")
                self.assertRegex(record["revision"], r"^[0-9a-f]{64}$")
                self.assertEqual(record["status"], "active")
                self.assertEqual(record["scope"], "project")
                self.assertEqual(record["project"], "proj")
                self.assertEqual(record["source_refs"], ["session:sess-schema-v2"])
                initial_identity[(key, record[label_field])] = (
                    record["id"],
                    record["revision"],
                )

            write_session_to_vault(
                cfg,
                "sess-schema-v2",
                "2026-07-12",
                "proj",
                {},
                [
                    first_decision,
                    {"text": "保留 lifecycle", "context": "避免更新退回旧格式"},
                ],
                [
                    first_error,
                    {"type": "other", "resolution": "验证更新路径"},
                ],
                None,
            )

            updated = yaml.safe_load(read_text(session_path).split("---", 2)[1])
            self.assertEqual(updated["memory_schema_version"], "2.0")
            self.assertEqual(len(updated["decisions_made"]), 2)
            self.assertEqual(len(updated["errors_encountered"]), 2)
            for key, label_field in (
                ("decisions_made", "text"),
                ("errors_encountered", "type"),
            ):
                first_record = updated[key][0]
                self.assertEqual(
                    (first_record["id"], first_record["revision"]),
                    initial_identity[(key, first_record[label_field])],
                )

    def test_project_aggregates_write_schema_v2_records(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}

            append_decisions(
                cfg,
                "github-obsidian-knowledge-brain",
                [
                    {
                        "text": "使用原子写入",
                        "context": "避免部分文件",
                        "operationalized_as": ["workflow-atomic-write"],
                    }
                ],
                "sess-1",
                "2026-07-12",
            )
            append_errors_to_pitfalls(
                cfg,
                "github-obsidian-knowledge-brain",
                [{"type": "path-filesystem", "resolution": "改用真实路径"}],
                "sess-1",
                "2026-07-12",
            )

            memory_dir = os.path.join(
                vault,
                "01-Projects/agent-memory-beacon/Memory",
            )
            decisions = yaml.safe_load(
                read_text(os.path.join(memory_dir, "decisions.md")).split("---", 2)[1]
            )
            pitfalls = yaml.safe_load(
                read_text(os.path.join(memory_dir, "pitfalls.md")).split("---", 2)[1]
            )
            self.assertEqual(decisions["schema_version"], "2.0")
            self.assertEqual(pitfalls["schema_version"], "2.0")
            self.assertEqual(
                decisions["decisions"][0]["operationalized_as"],
                ["workflow-atomic-write"],
            )
            for record in (decisions["decisions"][0], pitfalls["pitfalls"][0]):
                self.assertTrue(record["id"])
                self.assertTrue(record["revision"])
                self.assertEqual(record["status"], "active")
                self.assertEqual(record["scope"], "project")
                self.assertEqual(record["project"], "agent-memory-beacon")
                self.assertEqual(record["source_refs"], ["session:sess-1"])

            append_decisions(
                cfg,
                "agent-memory-beacon",
                [{"text": "使用原子写入", "context": "避免部分文件"}],
                "sess-1",
                "2026-07-12",
            )
            updated = yaml.safe_load(
                read_text(os.path.join(memory_dir, "decisions.md")).split("---", 2)[1]
            )
            self.assertEqual(len(updated["decisions"]), 1)

    def test_project_aggregate_reader_ignores_inline_delimiter_text(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}
            append_errors_to_pitfalls(
                cfg,
                "agent-memory-beacon",
                [
                    {
                        "type": "data-format",
                        "resolution": "before --- embedded --- after",
                    },
                    {
                        "type": "logic",
                        "resolution": "分隔符后的正式记录必须保留",
                    },
                ],
                "sess-delimiter",
                "2026-07-12",
            )
            append_errors_to_pitfalls(
                cfg,
                "agent-memory-beacon",
                [{"type": "other", "resolution": "后续增量记录"}],
                "sess-later",
                "2026-07-13",
            )
            path = os.path.join(
                vault,
                "01-Projects/agent-memory-beacon/Memory/pitfalls.md",
            )

            records = load_formal_project_records(
                path,
                "pitfalls",
                "error",
                "agent-memory-beacon",
            )

            self.assertEqual(len(records), 3)
            self.assertEqual(
                {record["summary"] for record in records},
                {
                    "before --- embedded --- after",
                    "分隔符后的正式记录必须保留",
                    "后续增量记录",
                },
            )

    def test_legacy_project_record_without_id_uses_source_position(self):
        with tempfile.TemporaryDirectory() as vault:
            path = os.path.join(
                vault,
                "01-Projects/demo/Memory/pitfalls.md",
            )
            os.makedirs(os.path.dirname(path), exist_ok=True)
            write_text(
                path,
                """---
project: demo
pitfalls:
- type: data-format
  resolution: 旧记录没有 ID
---
# Pitfalls
""",
            )

            records = load_formal_project_records(
                path,
                "pitfalls",
                "error",
                "demo",
            )

            self.assertEqual(len(records), 1)
            self.assertRegex(records[0]["id"], r"^error-[0-9a-f]{16}$")

    def test_explicit_project_routes_use_unique_aliases_with_exact_name_precedence(self):
        canonical = "agent-memory-beacon"
        legacy = "github-obsidian-knowledge-brain"
        cfg = {
            "projects": [canonical, "notes-counter"],
            "project_keywords": {canonical: [legacy, "shared-alias"]},
        }

        summary = (
            "[SESSION_SUMMARY]\n"
            f"projects: [{legacy}]\n"
            f"primary: {legacy}\n"
            "summary: migrated session\n"
            "[/SESSION_SUMMARY]"
        )
        annotation = (
            "[DECISION:Keep migrated routing| context:legacy clients remain valid| "
            f"project:{legacy}]"
        )
        self.assertEqual(
            detect_project(cfg, "", {}, annotation_text=summary),
            canonical,
        )
        self.assertEqual(
            detect_project(cfg, "", {}, annotation_text=annotation),
            canonical,
        )

        exact_cfg = {
            "projects": ["shared-alias", canonical],
            "project_keywords": {canonical: ["shared-alias"]},
        }
        self.assertEqual(
            detect_project(exact_cfg, "", {}, annotation_text=(
                "[ERROR:type=other| resolution:exact project wins| "
                "project:shared-alias]"
            )),
            "shared-alias",
        )

        ambiguous_cfg = {
            "projects": ["alpha", "beta"],
            "project_keywords": {
                "alpha": ["shared-alias"],
                "beta": ["shared-alias"],
            },
        }
        self.assertEqual(
            detect_project(ambiguous_cfg, "", {}, annotation_text=(
                "[DECISION:Keep ambiguity explicit| context:no arbitrary route| "
                "project:shared-alias]"
            )),
            "shared-alias",
        )
        self.assertEqual(
            detect_project(cfg, "", {}, annotation_text=(
                "[DECISION:Keep unknown route| context:preserve current behavior| "
                "project:unknown-project]"
            )),
            "unknown-project",
        )

    def test_rebuild_memory_index_routes_final_write_through_mutation_io(self):
        with tempfile.TemporaryDirectory() as vault:
            index_path = os.path.join(vault, "00-Inbox", "Agent Memory Index.md")

            class RecordingIO:
                def __init__(self):
                    self.writes = []

                def atomic_write(self, path, content, encoding="utf-8"):
                    self.writes.append(os.fspath(path))
                    write_text(path, content)

                def ensure_directory(self, path):
                    os.makedirs(path, exist_ok=True)

            mutation_io = RecordingIO()
            with (
                patch("session_harvester.ensure_obsidian_ignore_filters"),
                patch("session_harvester.repair_generated_vault_markdown"),
                patch("session_harvester.repair_generated_graph_links"),
                patch(
                    "session_harvester._rebuild_vault_knowledge_indexes_cooperative",
                    return_value={},
                ),
            ):
                rebuild_memory_index(
                    {"vault_path": vault, "memory_index_path": index_path},
                    mutation_io=mutation_io,
                )

            self.assertEqual(mutation_io.writes, [index_path])

    def test_cooperative_rebuild_writes_configured_recall_index(self):
        with tempfile.TemporaryDirectory() as vault:
            custom_recall = os.path.join(
                vault,
                "06-Custom",
                "runtime-recall.json",
            )
            custom_graph = os.path.join(
                vault,
                "06-Custom",
                "memory-graph.json",
            )
            cfg = {
                "vault_path": vault,
                "memory_runtime": {
                    "index_path": "06-Custom/runtime-recall.json",
                },
            }

            result = session_harvester._rebuild_vault_knowledge_indexes_cooperative(
                cfg,
                lambda: None,
            )

            self.assertIn(custom_recall, result["written"])
            self.assertIn(custom_graph, result["written"])
            self.assertTrue(os.path.exists(custom_recall))
            self.assertTrue(os.path.exists(custom_graph))
            loaded = load_recall_index(custom_recall)
            self.assertIn("_graph", loaded)
            self.assertEqual(result["graph_unbound_evidence"], 0)
            self.assertEqual(result["graph_missing_memory_nodes"], 0)
            self.assertEqual(
                result["graph_generation_id"],
                loaded["generation_id"],
            )
            self.assertFalse(
                os.path.exists(
                    os.path.join(
                        vault,
                        "05-Agent-Memory",
                        "recall-index.json",
                    )
                )
            )
            self.assertFalse(
                os.path.exists(
                    os.path.join(
                        vault,
                        "05-Agent-Memory",
                        "memory-graph.json",
                    )
                )
            )

    def test_cooperative_rebuild_keeps_graph_projection_current(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {
                "vault_path": vault,
                "projects": ["demo"],
            }
            append_decisions(
                cfg,
                "demo",
                [
                    {
                        "text": "协作重建同步图谱投影",
                        "context": "后台索引路径也必须生成可见节点",
                    }
                ],
                "session-cooperative-projection",
                "2026-07-31",
            )

            result = session_harvester._rebuild_vault_knowledge_indexes_cooperative(
                cfg,
                lambda: None,
            )

            self.assertGreaterEqual(result["graph_projection_nodes"], 1)
            projection_root = os.path.join(
                vault,
                "03-Maps",
                "_memory-nodes",
            )
            self.assertTrue(
                any(
                    filename.endswith(".md")
                    for _current, _dirs, files in os.walk(projection_root)
                    for filename in files
                )
            )

    def test_visible_index_reports_configured_memory_graph_path(self):
        with tempfile.TemporaryDirectory() as vault:
            index_path = os.path.join(
                vault,
                "00-Inbox",
                "Agent Memory Index.md",
            )
            rebuild_memory_index(
                {
                    "vault_path": vault,
                    "memory_runtime": {
                        "index_path": "06-Custom/runtime-recall.json",
                    },
                }
            )

            content = read_text(index_path)
            self.assertIn("`06-Custom/memory-graph.json`", content)
            self.assertNotIn("`05-Agent-Memory/memory-graph.json`", content)

    def test_cooperative_rebuild_excludes_configured_insight_candidates(self):
        with tempfile.TemporaryDirectory() as vault:
            candidate_dir = os.path.join(
                vault,
                "05-Agent-Memory",
                "private-insight-candidates",
            )
            os.makedirs(candidate_dir)
            write_text(
                os.path.join(candidate_dir, "private.md"),
                "---\ntype: insight-candidate\nstatus: candidate\n---\n\n"
                "# cooperativecandidateleaktoken\n",
            )
            cfg = {
                "vault_path": vault,
                "insight_memory": {
                    "candidate_dir": (
                        "05-Agent-Memory/private-insight-candidates"
                    ),
                },
            }

            result = session_harvester._rebuild_vault_knowledge_indexes_cooperative(
                cfg,
                lambda: None,
            )

            machine_text = "\n".join(
                read_text(path) for path in result["written"]
            )
            self.assertNotIn("cooperativecandidateleaktoken", machine_text)

    def test_rebuild_memory_index_reports_phase_timings(self):
        with tempfile.TemporaryDirectory() as vault:
            output = io.StringIO()
            with (
                patch("session_harvester.ensure_obsidian_ignore_filters"),
                patch("session_harvester.repair_generated_vault_markdown"),
                patch("session_harvester.repair_generated_graph_links"),
                patch(
                    "session_harvester._rebuild_vault_knowledge_indexes_cooperative",
                    return_value={},
                ),
                redirect_stdout(output),
            ):
                rebuild_memory_index({"vault_path": vault})

            timing_lines = [
                line
                for line in output.getvalue().splitlines()
                if line.startswith("[harvester:index] Timing:")
            ]
            self.assertEqual(len(timing_lines), 1)
            timing = timing_lines[0]
            for phase in (
                "repair=",
                "collect=",
                "knowledge=",
                "render_write=",
                "dirty_clear=",
                "total=",
            ):
                self.assertIn(phase, timing)

    def test_rebuild_memory_index_ownership_check_stops_before_final_index_write(self):
        with tempfile.TemporaryDirectory() as vault:
            index_path = os.path.join(vault, "00-Inbox", "Agent Memory Index.md")
            phase_marker = os.path.join(vault, "05-Agent-Memory", "phase-one.txt")
            os.makedirs(os.path.dirname(index_path), exist_ok=True)
            os.makedirs(os.path.dirname(phase_marker), exist_ok=True)
            write_text(index_path, "old index\n")

            def first_mutating_phase(_cfg):
                write_text(phase_marker, "phase one complete\n")
                return {}

            def ownership_check():
                if os.path.exists(phase_marker):
                    raise RuntimeError("ownership lost after knowledge indexes")

            with (
                patch("session_harvester.ensure_obsidian_ignore_filters"),
                patch("session_harvester.repair_generated_vault_markdown"),
                patch("session_harvester.repair_generated_graph_links"),
                patch(
                    "session_harvester._rebuild_vault_knowledge_indexes_cooperative",
                    side_effect=lambda cfg, _check: first_mutating_phase(cfg),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "ownership lost"):
                    rebuild_memory_index(
                        {"vault_path": vault},
                        ownership_check=ownership_check,
                    )

            self.assertEqual(read_text(index_path), "old index\n")

    def test_process_transcript_refreshes_effectiveness_report_after_success(self):
        outcome = session_harvester.TranscriptHarvestOutcome(
            transcript_path="session.jsonl",
            version="file-v1",
            cursor="file-bytes:10",
            expected_cursor=None,
            changed=True,
            needs_index_rebuild=True,
            project="agent-memory-beacon",
            session_id="session-one",
        )
        cfg = {"vault_path": "/tmp/vault"}
        with (
            patch("session_harvester.prepare_transcript_harvest", return_value=outcome),
            patch("session_harvester.rebuild_memory_index") as rebuild,
            patch("session_harvester.commit_transcript_harvest") as commit,
            patch("session_harvester.write_effectiveness_report") as report,
            patch("session_harvester.refresh_promotion_proposals") as promotion,
        ):
            result = session_harvester.process_transcript(cfg, "session.jsonl")

        self.assertTrue(result)
        rebuild.assert_called_once_with(cfg)
        commit.assert_called_once_with(cfg, outcome)
        report.assert_called_once_with(cfg["vault_path"], cfg)
        promotion.assert_called_once_with(cfg["vault_path"], cfg, apply=True)

    def test_effectiveness_report_failure_warns_without_rolling_back_harvest(self):
        outcome = session_harvester.TranscriptHarvestOutcome(
            transcript_path="session.jsonl",
            version="file-v1",
            cursor="file-bytes:10",
            expected_cursor=None,
            changed=True,
            needs_index_rebuild=True,
            project="agent-memory-beacon",
            session_id="session-one",
        )
        output = io.StringIO()
        cfg = {"vault_path": "/tmp/vault"}
        with (
            patch("session_harvester.prepare_transcript_harvest", return_value=outcome),
            patch("session_harvester.rebuild_memory_index"),
            patch("session_harvester.commit_transcript_harvest") as commit,
            patch(
                "session_harvester.write_effectiveness_report",
                side_effect=OSError("report unavailable"),
            ),
            patch(
                "session_harvester.refresh_promotion_proposals",
                side_effect=OSError("promotion unavailable"),
            ),
            redirect_stdout(output),
        ):
            result = session_harvester.process_transcript(cfg, "session.jsonl")

        self.assertTrue(result)
        commit.assert_called_once_with(cfg, outcome)
        self.assertIn("WARNING: could not refresh effectiveness report", output.getvalue())
        self.assertIn("WARNING: could not refresh promotion proposals", output.getvalue())

    def test_skip_scanner_cli_flag_is_forwarded_to_stop_mode(self):
        cfg = {"vault_path": "/tmp/test-vault"}
        argv = ["session_harvester.py", "--mode", "stop", "--skip-scanner"]
        with (
            patch.object(sys, "argv", argv),
            patch("session_harvester.read_hook_input", return_value=None),
            patch("session_harvester.load_config", return_value=cfg),
            patch("session_harvester.acquire_harvest_lock", return_value=True),
            patch("session_harvester.release_harvest_lock"),
            patch("session_harvester.stop_mode", return_value=0) as mocked_stop,
        ):
            main()

        mocked_stop.assert_called_once_with(
            cfg, hook_input=None, run_scanner=False
        )

    def test_stop_mode_can_harvest_without_running_scanner(self):
        with (
            patch("session_harvester.find_transcript", return_value="session.jsonl"),
            patch("session_harvester.process_transcript", return_value=True),
            patch("session_harvester.run_scanner_incremental") as mocked_scanner,
        ):
            stop_mode({}, run_scanner=False)

        mocked_scanner.assert_not_called()

    def test_incremental_scanner_disables_runtime_bytecode_writes(self):
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout="",
            stderr="",
        )
        with (
            patch("session_harvester.os.path.exists", return_value=True),
            patch("session_harvester.check_proxy", return_value=None),
            patch(
                "session_harvester.subprocess.run",
                return_value=completed,
            ) as runner,
        ):
            session_harvester.run_scanner_incremental(
                {"python_path": "/runtime/python"}
            )

        command = runner.call_args.args[0]
        self.assertEqual(command[:2], ["/runtime/python", "-B"])
        self.assertTrue(command[2].endswith("/runner.py"))

    def test_start_mode_rebuilds_index_once_for_multiple_changed_transcripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(vault)
            transcripts = []
            for index in range(2):
                transcript = os.path.join(tmp, f"session-{index}.jsonl")
                write_codex_transcript(
                    transcript,
                    [
                        {
                            "timestamp": f"2026-07-18T01:0{index}:00Z",
                            "type": "session_meta",
                            "payload": {
                                "id": f"batch-session-{index}",
                                "cwd": "/tmp/demo",
                                "timestamp": f"2026-07-18T01:0{index}:00Z",
                            },
                        },
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "assistant",
                                "content": (
                                    f"[DECISION:采用批次策略 {index}| "
                                    "context:避免每条记录重复执行完整索引重建]"
                                ),
                            },
                        },
                    ],
                )
                transcripts.append(transcript)

            cfg = {
                "vault_path": vault,
                "projects": [{"name": "demo", "keywords": ["demo"]}],
                "personal_memory": {"enabled": False},
                "skill_preferences": {"enabled": False},
                "workflow_memory": {"enabled": False},
                "error_evidence": {"enabled": False},
                "annotation_quality": {"enabled": False},
                "scan_on_start": False,
            }

            with (
                patch("session_harvester.check_codex_profile_on_start"),
                patch("session_harvester.initialize_harvest_baseline", return_value=0),
                patch("session_harvester.load_processed_from_heartbeat", return_value={}),
                patch(
                    "session_harvester.find_recent_transcripts_from_config",
                    return_value=transcripts,
                ),
                patch("session_harvester.rebuild_memory_index") as rebuild,
                patch("session_harvester.write_effectiveness_report") as report,
                patch("session_harvester.refresh_promotion_proposals") as promotion,
            ):
                session_harvester.start_mode(cfg)

            rebuild.assert_called_once_with(cfg)
            report.assert_called_once_with(cfg["vault_path"], cfg)
            promotion.assert_called_once_with(cfg["vault_path"], cfg, apply=True)

    def test_start_mode_does_not_advance_changed_cursor_when_index_rebuild_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(vault)
            transcript = os.path.join(tmp, "failed-index.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "timestamp": "2026-07-18T02:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "failed-index-session",
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-18T02:00:00Z",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": (
                                "[DECISION:采用批次索引提交策略| "
                                "context:多个 transcript 共享一次完整索引重建]"
                            ),
                        },
                    },
                ],
            )
            cfg = {
                "vault_path": vault,
                "projects": [{"name": "demo", "keywords": ["demo"]}],
                "personal_memory": {"enabled": False},
                "skill_preferences": {"enabled": False},
                "workflow_memory": {"enabled": False},
                "error_evidence": {"enabled": False},
                "annotation_quality": {"enabled": False},
                "scan_on_start": False,
            }

            with (
                patch("session_harvester.check_codex_profile_on_start"),
                patch("session_harvester.initialize_harvest_baseline", return_value=0),
                patch("session_harvester.load_processed_from_heartbeat", return_value={}),
                patch(
                    "session_harvester.find_recent_transcripts_from_config",
                    return_value=[transcript],
                ),
                patch(
                    "session_harvester.rebuild_memory_index",
                    side_effect=RuntimeError("index failed"),
                ) as rebuild,
            ):
                session_harvester.start_mode(cfg)

            rebuild.assert_called_once_with(cfg)
            tracking = load_harvest_tracking(cfg)
            self.assertNotIn(
                session_harvester.session_id_from_path(transcript),
                tracking.get("adaptive_cursors", {}),
            )

    def test_start_mode_caps_transcripts_processed_per_batch(self):
        cfg = {
            "vault_path": "/tmp/test-vault",
            "scan_on_start": False,
            "harvest_start_max_transcripts": 2,
        }
        transcripts = ["one.jsonl", "two.jsonl", "three.jsonl"]

        def outcome(_cfg, path):
            return session_harvester.TranscriptHarvestOutcome(
                transcript_path=path,
                version=f"version:{path}",
                cursor=f"file-bytes:{len(path)}",
                expected_cursor=None,
                changed=False,
                needs_index_rebuild=False,
                project="demo",
                session_id=path,
            )

        with (
            patch("session_harvester.check_codex_profile_on_start"),
            patch("session_harvester.initialize_harvest_baseline", return_value=0),
            patch("session_harvester.load_processed_from_heartbeat", return_value={}),
            patch(
                "session_harvester.find_recent_transcripts_from_config",
                return_value=transcripts,
            ),
            patch(
                "session_harvester.prepare_transcript_harvest",
                side_effect=outcome,
            ) as prepare,
            patch("session_harvester.commit_transcript_harvest"),
        ):
            session_harvester.start_mode(cfg)

        self.assertEqual(prepare.call_count, 2)

    def test_start_mode_stops_taking_new_transcripts_after_time_budget(self):
        cfg = {
            "vault_path": "/tmp/test-vault",
            "scan_on_start": False,
            "harvest_start_max_transcripts": 10,
            "harvest_start_time_budget_seconds": 5,
        }
        transcripts = ["one.jsonl", "two.jsonl", "three.jsonl"]

        def outcome(_cfg, path):
            return session_harvester.TranscriptHarvestOutcome(
                transcript_path=path,
                version=f"version:{path}",
                cursor=f"file-bytes:{len(path)}",
                expected_cursor=None,
                changed=False,
                needs_index_rebuild=False,
                project="demo",
                session_id=path,
            )

        with (
            patch("session_harvester.check_codex_profile_on_start"),
            patch("session_harvester.initialize_harvest_baseline", return_value=0),
            patch("session_harvester.load_processed_from_heartbeat", return_value={}),
            patch(
                "session_harvester.find_recent_transcripts_from_config",
                return_value=transcripts,
            ),
            patch(
                "session_harvester.prepare_transcript_harvest",
                side_effect=outcome,
            ) as prepare,
            patch("session_harvester.commit_transcript_harvest"),
            patch(
                "session_harvester.monotonic",
                side_effect=[0.0, 6.0, 7.0],
                create=True,
            ),
        ):
            session_harvester.start_mode(cfg)

        self.assertEqual(prepare.call_count, 1)

    def test_live_harvest_lock_is_never_stolen_by_age(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = os.path.join(root, "harvester.lock")
            self.assertTrue(session_harvester.acquire_harvest_lock(lock_path))
            try:
                os.utime(lock_path, (0, 0))
                self.assertFalse(
                    session_harvester.acquire_harvest_lock(lock_path)
                )
            finally:
                session_harvester.release_harvest_lock(lock_path)

            self.assertTrue(session_harvester.acquire_harvest_lock(lock_path))
            session_harvester.release_harvest_lock(lock_path)

    def test_harvest_lock_keeps_one_inode_across_release_and_reacquire(self):
        with tempfile.TemporaryDirectory() as root:
            lock_path = os.path.join(root, "harvester.lock")
            self.assertTrue(session_harvester.acquire_harvest_lock(lock_path))
            first = os.lstat(lock_path)

            session_harvester.release_harvest_lock(lock_path)

            self.assertTrue(os.path.exists(lock_path))
            released = os.lstat(lock_path)
            self.assertEqual((released.st_dev, released.st_ino), (first.st_dev, first.st_ino))
            self.assertTrue(session_harvester.acquire_harvest_lock(lock_path))
            reacquired = os.lstat(lock_path)
            self.assertEqual(
                (reacquired.st_dev, reacquired.st_ino),
                (first.st_dev, first.st_ino),
            )
            session_harvester.release_harvest_lock(lock_path)

    def test_heartbeat_cursor_commit_rejects_out_of_order_worker(self):
        with tempfile.TemporaryDirectory() as root:
            heartbeat = os.path.join(root, "heartbeat.md")
            transcript = os.path.join(root, "session.jsonl")
            write_text(transcript, "{}\n")
            write_heartbeat_document(
                heartbeat,
                {
                    "harvested_sessions": {"session": "new-version"},
                    "adaptive_cursors": {"session": "file-bytes:20"},
                    "processed_sessions": {},
                    "adaptive_cursor_initialized_at": "2026-07-13T00:00:00+08:00",
                },
                "# Scanner Heartbeat\n",
            )

            with self.assertRaisesRegex(RuntimeError, "cursor changed"):
                session_harvester._mark_transcript_harvested_unlocked(
                    heartbeat,
                    transcript,
                    version="old-version",
                    adaptive_cursor="file-bytes:10",
                    expected_cursor="file-bytes:0",
                )

            frontmatter = yaml.safe_load(read_text(heartbeat).split("---", 2)[1])
            self.assertEqual(frontmatter["adaptive_cursors"]["session"], "file-bytes:20")
            self.assertEqual(frontmatter["harvested_sessions"]["session"], "new-version")

    def test_heartbeat_cursor_commit_rejects_regression_even_with_current_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            heartbeat = os.path.join(root, "heartbeat.md")
            transcript = os.path.join(root, "session.jsonl")
            write_text(transcript, "{}\n")
            write_heartbeat_document(
                heartbeat,
                {
                    "harvested_sessions": {"session": "new-version"},
                    "adaptive_cursors": {"session": "file-bytes:20"},
                    "processed_sessions": {},
                    "adaptive_cursor_initialized_at": "2026-07-13T00:00:00+08:00",
                },
                "# Scanner Heartbeat\n",
            )

            with self.assertRaisesRegex(RuntimeError, "regress"):
                session_harvester._mark_transcript_harvested_unlocked(
                    heartbeat,
                    transcript,
                    version="old-version",
                    adaptive_cursor="file-bytes:10",
                    expected_cursor="file-bytes:20",
                )

    def test_rebuild_does_not_clear_or_commit_mismatched_dirty_generation(self):
        with tempfile.TemporaryDirectory() as vault:
            candidate_dir = os.path.join(vault, "04-Feedback", "_error-candidates")
            os.makedirs(candidate_dir)
            marker = os.path.join(candidate_dir, ".index-dirty")
            write_text(marker, ("c" * 64) + "\n")
            cfg = {
                "vault_path": vault,
                "error_evidence": {
                    "enabled": True,
                    "candidate_dir": "04-Feedback/_error-candidates",
                    "excerpt_limit": 500,
                    "source_limit": 20,
                },
            }

            with patch(
                "session_harvester.clear_error_evidence_dirty",
                return_value=False,
            ):
                with self.assertRaisesRegex(
                    ErrorEvidenceStateError,
                    "generation changed",
                ):
                    rebuild_memory_index(cfg, repair_generated=False)

            self.assertEqual(read_text(marker), ("c" * 64) + "\n")

    def test_harvest_baseline_marks_existing_transcripts_without_processing_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcripts = os.path.join(tmp, "transcripts")
            os.makedirs(os.path.join(vault, "04-Feedback"))
            os.makedirs(transcripts)
            first = os.path.join(transcripts, "first.jsonl")
            second = os.path.join(transcripts, "second.jsonl")
            write_text(first, "{}\n")
            write_text(second, "{}\n")
            heartbeat = os.path.join(vault, "04-Feedback", "heartbeat.md")
            write_text(
                heartbeat,
                """---
processed_sessions:
  backup-only: old
harvested_sessions:
  existing: old-version
---

# Custom Heartbeat Body
""",
            )
            cfg = {
                "vault_path": vault,
                "transcript_paths": [transcripts],
                "transcript_agents": [],
            }

            initialized = initialize_harvest_baseline(cfg)

            frontmatter = yaml.safe_load(read_text(heartbeat).split("---", 2)[1])
            self.assertEqual(initialized, 2)
            self.assertEqual(frontmatter["harvested_sessions"]["existing"], "old-version")
            first_key = transcript_state_key(first)
            second_key = transcript_state_key(second)
            self.assertIn(first_key, frontmatter["harvested_sessions"])
            self.assertIn(second_key, frontmatter["harvested_sessions"])
            self.assertIn("harvest_baseline_initialized_at", frontmatter)
            self.assertIn("adaptive_cursor_initialized_at", frontmatter)
            self.assertEqual(
                set(frontmatter["adaptive_cursors"]),
                {first_key, second_key},
            )
            self.assertIn("# Custom Heartbeat Body", read_text(heartbeat))
            self.assertEqual(
                find_recent_transcripts(
                    cfg,
                    load_processed_from_heartbeat(cfg),
                    hours=24,
                ),
                [],
            )
            self.assertEqual(initialize_harvest_baseline(cfg), 0)

    def test_heartbeat_write_does_not_follow_predictable_temp_symlink(self):
        with tempfile.TemporaryDirectory() as vault:
            feedback = os.path.join(vault, "04-Feedback")
            os.makedirs(feedback)
            heartbeat = os.path.join(feedback, "heartbeat.md")
            outside = os.path.join(vault, "outside.md")
            write_text(outside, "keep\n")
            os.symlink(outside, heartbeat + ".tmp")

            write_heartbeat_document(
                heartbeat,
                {
                    "harvested_sessions": {},
                    "adaptive_cursors": {},
                    "processed_sessions": {},
                },
                "# Scanner Heartbeat\n",
            )

            self.assertEqual(read_text(outside), "keep\n")
            self.assertFalse(os.path.islink(heartbeat))
            self.assertIn("# Scanner Heartbeat", read_text(heartbeat))

    def test_dirty_rebuild_refuses_malformed_canonical_error_candidate(self):
        with tempfile.TemporaryDirectory() as vault:
            candidate_dir = os.path.join(
                vault,
                "04-Feedback",
                "_error-candidates",
            )
            os.makedirs(candidate_dir)
            malformed = os.path.join(
                candidate_dir,
                "error-evidence-" + ("a" * 64) + ".md",
            )
            write_text(malformed, "---\nstatus: [\n---\n")
            marker = os.path.join(candidate_dir, ".index-dirty")
            write_text(marker, ("b" * 64) + "\n")
            cfg = {
                "vault_path": vault,
                "error_evidence": {
                    "enabled": True,
                    "candidate_dir": "04-Feedback/_error-candidates",
                    "excerpt_limit": 500,
                    "source_limit": 20,
                },
            }

            with self.assertRaises(ErrorEvidenceStateError):
                rebuild_memory_index(cfg, repair_generated=False)

            self.assertEqual(read_text(marker), ("b" * 64) + "\n")

    def test_harvest_baseline_uses_one_consistent_version_cursor_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcripts = os.path.join(tmp, "transcripts")
            os.makedirs(os.path.join(vault, "04-Feedback"))
            os.makedirs(transcripts)
            transcript = os.path.join(transcripts, "session.jsonl")
            write_text(transcript, "{}\n")
            cfg = {
                "vault_path": vault,
                "transcript_paths": [transcripts],
                "transcript_agents": [],
            }

            def version_then_append(path):
                stat = os.stat(path)
                version = f"file:{stat.st_size}:{stat.st_mtime_ns}"
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write("{}\n")
                return version

            with patch(
                "session_harvester.transcript_version",
                side_effect=version_then_append,
            ):
                initialize_harvest_baseline(cfg)

            heartbeat = os.path.join(vault, "04-Feedback", "heartbeat.md")
            frontmatter = yaml.safe_load(read_text(heartbeat).split("---", 2)[1])
            state_key = transcript_state_key(transcript)
            version_size = int(
                frontmatter["harvested_sessions"][state_key].split(":")[1]
            )
            cursor_size = int(
                frontmatter["adaptive_cursors"][state_key].split(":")[1]
            )
            self.assertEqual(version_size, cursor_size)

    def test_malformed_heartbeat_aborts_harvest_without_overwriting_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            feedback = os.path.join(vault, "04-Feedback")
            os.makedirs(feedback)
            heartbeat = os.path.join(feedback, "heartbeat.md")
            malformed = "---\nharvested_sessions: [\n---\n\n# Keep this body\n"
            write_text(heartbeat, malformed)
            transcript = os.path.join(tmp, "session.jsonl")
            write_text(transcript, "{}\n")

            with self.assertRaises(ValueError):
                process_transcript({"vault_path": vault}, transcript)

            self.assertEqual(read_text(heartbeat), malformed)

    def test_initialized_heartbeat_rejects_non_mapping_cursor_state(self):
        with tempfile.TemporaryDirectory() as vault:
            feedback = os.path.join(vault, "04-Feedback")
            os.makedirs(feedback)
            write_text(
                os.path.join(feedback, "heartbeat.md"),
                """---
harvest_baseline_initialized_at: '2026-07-01T00:00:00+08:00'
adaptive_cursor_initialized_at: '2026-07-01T00:00:00+08:00'
harvested_sessions: {}
adaptive_cursors: []
processed_sessions: {}
---

# Invalid cursor state
""",
            )

            with self.assertRaises(ValueError):
                load_harvest_tracking({"vault_path": vault})

    def test_legacy_baseline_backfills_cursors_without_hiding_changed_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcripts = os.path.join(tmp, "transcripts")
            os.makedirs(os.path.join(vault, "04-Feedback"))
            os.makedirs(transcripts)
            transcript = os.path.join(transcripts, "session.jsonl")
            write_text(transcript, "{}\n")
            heartbeat = os.path.join(vault, "04-Feedback", "heartbeat.md")
            write_text(
                heartbeat,
                """---
harvest_baseline_initialized_at: 2026-07-01T00:00:00+08:00
harvested_sessions:
  session: old-version
processed_sessions: {}
---

# Existing Heartbeat
""",
            )
            cfg = {
                "vault_path": vault,
                "transcript_paths": [transcripts],
                "transcript_agents": [],
            }

            self.assertEqual(initialize_harvest_baseline(cfg), 1)

            frontmatter = yaml.safe_load(read_text(heartbeat).split("---", 2)[1])
            self.assertEqual(frontmatter["harvested_sessions"]["session"], "old-version")
            self.assertIn(
                transcript_state_key(transcript),
                frontmatter["adaptive_cursors"],
            )
            self.assertIn("adaptive_cursor_initialized_at", frontmatter)
            self.assertEqual(initialize_harvest_baseline(cfg), 0)

    def test_appended_transcript_does_not_replay_pre_baseline_adaptive_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcripts = os.path.join(tmp, "transcripts")
            os.makedirs(os.path.join(vault, "04-Feedback"))
            os.makedirs(transcripts)
            transcript = os.path.join(transcripts, "long-session.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "long-session",
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-01T01:00:00Z",
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": (
                                "我希望以后默认用 $humanizer；分析 GitHub skill "
                                "要先查源码和 README，不要只看名字。"
                            ),
                        },
                    },
                ],
            )
            cfg = {
                "vault_path": vault,
                "transcript_paths": [transcripts],
                "transcript_agents": [],
                "projects": [{"name": "demo", "keywords": ["demo"]}],
                "personal_memory": {"enabled": True},
                "skill_preferences": {"enabled": True},
                "workflow_memory": {"enabled": True},
            }

            self.assertEqual(initialize_harvest_baseline(cfg), 1)
            with open(transcript, "a", encoding="utf-8") as handle:
                for record in (
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "我希望以后默认用中文解释复杂代码。",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": (
                                "[DECISION:采用增量游标只收割当前追加内容| "
                                "context:基线前内容不能进入自适应学习]"
                            ),
                        },
                    },
                ):
                    json.dump(record, handle, ensure_ascii=False)
                    handle.write("\n")

            self.assertTrue(process_transcript(cfg, transcript))
            memory_candidates = glob.glob(
                os.path.join(vault, "04-Feedback/_memory-candidates/*.md")
            )
            self.assertEqual(len(memory_candidates), 1)
            candidate_text = read_text(memory_candidates[0])
            self.assertIn("默认用中文解释复杂代码", candidate_text)
            self.assertNotIn("默认用 $humanizer", candidate_text)
            self.assertEqual(
                glob.glob(os.path.join(vault, "04-Feedback/_skill-preferences/*.md")),
                [],
            )
            self.assertEqual(
                glob.glob(os.path.join(vault, "04-Feedback/_workflow-candidates/*.md")),
                [],
            )

    def test_appended_transcript_does_not_replay_pre_baseline_annotations(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcripts = os.path.join(tmp, "transcripts")
            os.makedirs(os.path.join(vault, "04-Feedback"))
            os.makedirs(transcripts)
            transcript = os.path.join(transcripts, "long-annotations.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "long-annotations",
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-01T01:00:00Z",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "[DECISION:基线前旧决定| context:不得在安装后重放]",
                        },
                    },
                ],
            )
            cfg = {
                "vault_path": vault,
                "transcript_paths": [transcripts],
                "transcript_agents": [],
                "projects": ["demo"],
                "project_keywords": {},
                "personal_memory": {"enabled": False},
                "skill_preferences": {"enabled": False},
                "workflow_memory": {"enabled": False},
                "error_evidence": {"enabled": False},
            }

            self.assertEqual(initialize_harvest_baseline(cfg), 1)
            with open(transcript, "a", encoding="utf-8") as handle:
                json.dump(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": (
                                "[DECISION:采用游标只收割基线后的新决定| "
                                "context:避免安装前历史被后台重放]"
                            ),
                        },
                    },
                    handle,
                    ensure_ascii=False,
                )
                handle.write("\n")

            self.assertTrue(process_transcript(cfg, transcript))

            sessions = glob.glob(
                os.path.join(vault, "01-Projects/demo/Memory/sessions/*.md")
            )
            self.assertEqual(len(sessions), 1)
            content = read_text(sessions[0])
            self.assertIn("采用游标只收割基线后的新决定", content)
            self.assertNotIn("基线前旧决定", content)

    def test_plain_append_skips_repairs_and_index_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcript = os.path.join(tmp, "long-session.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "plain-append",
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-01T01:00:00Z",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": (
                                "[DECISION:采用增量游标记录首次收割位置| "
                                "context:避免后续普通追加触发全量解析]"
                            ),
                        },
                    },
                ],
            )
            cfg = {
                "vault_path": vault,
                "projects": ["demo"],
                "project_keywords": {},
                "personal_memory": {"enabled": False},
                "skill_preferences": {"enabled": False},
                "workflow_memory": {"enabled": False},
                "error_evidence": {"enabled": False},
            }
            self.assertTrue(process_transcript(cfg, transcript))
            with open(transcript, "a", encoding="utf-8") as handle:
                json.dump(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "这是一条没有任何长期记忆的普通追加回复。",
                        },
                    },
                    handle,
                    ensure_ascii=False,
                )
                handle.write("\n")

            with (
                patch("session_harvester.ensure_obsidian_ignore_filters") as repair,
                patch("session_harvester.rebuild_memory_index") as rebuild,
                patch(
                    "session_harvester.parse_transcript",
                    side_effect=AssertionError("incremental harvest used full parser"),
                ) as full_parse,
            ):
                self.assertFalse(process_transcript(cfg, transcript))

            repair.assert_not_called()
            rebuild.assert_not_called()
            full_parse.assert_not_called()

    def test_incremental_annotation_batches_receive_distinct_stable_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcript = os.path.join(tmp, "long-annotations.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "incremental-stable-ids",
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-18T01:00:00Z",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": (
                                "[DECISION:第一批采用原子写入| "
                                "context:避免中断留下半个聚合文件]\n"
                                "[ERROR:type=path-filesystem| "
                                "resolution=读取旧路径失败，定位真实路径后重试并验证读取成功]"
                            ),
                        },
                    },
                ],
            )
            cfg = {
                "vault_path": vault,
                "projects": ["demo"],
                "project_keywords": {},
                "personal_memory": {"enabled": False},
                "skill_preferences": {"enabled": False},
                "workflow_memory": {"enabled": False},
                "error_evidence": {"enabled": False},
            }

            self.assertTrue(process_transcript(cfg, transcript))
            with open(transcript, "a", encoding="utf-8") as handle:
                json.dump(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": (
                                "[DECISION:第二批采用快照预检| "
                                "context:避免逐动作重复扫描 Vault]\n"
                                "[ERROR:type=shell-cli| "
                                "resolution=git 命令执行失败，移除无效参数后重新运行并验证成功]"
                            ),
                        },
                    },
                    handle,
                    ensure_ascii=False,
                )
                handle.write("\n")

            self.assertTrue(process_transcript(cfg, transcript))

            memory_dir = os.path.join(vault, "01-Projects/demo/Memory")
            decisions = yaml.safe_load(
                read_text(os.path.join(memory_dir, "decisions.md")).split("---", 2)[1]
            )["decisions"]
            errors = yaml.safe_load(
                read_text(os.path.join(memory_dir, "pitfalls.md")).split("---", 2)[1]
            )["pitfalls"]
            self.assertEqual(len(decisions), 2)
            self.assertEqual(len(errors), 2)
            self.assertEqual(len({item["id"] for item in decisions}), 2)
            self.assertEqual(len({item["id"] for item in errors}), 2)

    def test_markdown_sanitizer_downgrades_directory_and_placeholder_wikilinks(self):
        with tempfile.TemporaryDirectory() as vault:
            os.makedirs(os.path.join(vault, "01-Projects"))
            os.makedirs(os.path.join(vault, "04-Feedback", "_raw-sessions"))

            cleaned = sanitize_obsidian_markdown(
                "查看 [[01-Projects]]、[[04-Feedback/_raw-sessions|原始记录]] 和 [[...]]",
                {"vault_path": vault},
            )

            self.assertIn("`01-Projects`", cleaned)
            self.assertIn("原始记录 (`04-Feedback/_raw-sessions`)", cleaned)
            self.assertIn("`...`", cleaned)
            self.assertNotIn("[[01-Projects]]", cleaned)
            self.assertNotIn("[[...]]", cleaned)

    def test_obsidian_internal_paths_are_hidden_from_vault_and_graph(self):
        with tempfile.TemporaryDirectory() as vault:
            obsidian_dir = os.path.join(vault, ".obsidian")
            os.makedirs(obsidian_dir)
            write_json(
                os.path.join(obsidian_dir, "app.json"),
                {"userIgnoreFilters": ["Custom/"]},
            )
            write_json(
                os.path.join(obsidian_dir, "graph.json"),
                {"search": "tag:#keep", "showTags": True},
            )

            ensure_obsidian_ignore_filters({"vault_path": vault})

            app = json.loads(read_text(os.path.join(obsidian_dir, "app.json")))
            graph = json.loads(read_text(os.path.join(obsidian_dir, "graph.json")))
            expected = {
                "04-Feedback/_raw-sessions/",
                "04-Feedback/_rollback/",
                "04-Feedback/_cleanup-backups/",
                "04-Feedback/_logs/",
                "05-Agent-Memory/codex-profile/",
                "Users/",
            }
            self.assertIn("Custom/", app["userIgnoreFilters"])
            self.assertTrue(expected.issubset(set(app["userIgnoreFilters"])))
            self.assertIn("tag:#keep", graph["search"])
            for path in expected:
                self.assertIn(f'-path:"{path.rstrip("/")}"', graph["search"])
            self.assertTrue(graph["showTags"])

    def test_obsidian_ignore_filter_repair_rejects_replaced_vault_root(self):
        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            os.makedirs(os.path.join(vault, ".obsidian"))
            os.rename(vault, os.path.join(root, "vault-held"))
            outside = os.path.join(root, "outside")
            outside_obsidian = os.path.join(outside, ".obsidian")
            os.makedirs(outside_obsidian)
            app_path = os.path.join(outside_obsidian, "app.json")
            write_json(app_path, {"userIgnoreFilters": ["keep/"]})
            original = read_text(app_path)
            os.symlink(outside, vault)

            ensure_obsidian_ignore_filters({"vault_path": vault})

            self.assertEqual(read_text(app_path), original)
            self.assertFalse(os.path.exists(os.path.join(outside_obsidian, "graph.json")))

    def test_obsidian_workspace_repair_rejects_intermediate_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            outside_obsidian = os.path.join(root, "outside-obsidian")
            os.makedirs(vault)
            os.makedirs(outside_obsidian)
            workspace = os.path.join(outside_obsidian, "workspace.json")
            write_json(workspace, {"lastOpenFiles": ["Users/a0000/outside.md"]})
            original = read_text(workspace)
            os.symlink(outside_obsidian, os.path.join(vault, ".obsidian"))
            opened = []
            real_open = open

            def tracking_open(path, *args, **kwargs):
                if os.fspath(path) == os.path.join(vault, ".obsidian", "workspace.json"):
                    opened.append(os.fspath(path))
                return real_open(path, *args, **kwargs)

            with patch("session_harvester.open", side_effect=tracking_open):
                repair_obsidian_workspace({"vault_path": vault})

            self.assertEqual(opened, [])
            self.assertEqual(read_text(workspace), original)

    def test_generated_markdown_repair_rejects_symlinked_managed_root(self):
        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            outside_projects = os.path.join(root, "outside-projects")
            generated = os.path.join(
                outside_projects,
                "proj/Memory/sessions/2026-07-13-outside.md",
            )
            os.makedirs(vault)
            os.makedirs(os.path.dirname(generated))
            write_text(
                generated,
                "---\nharvested_by: session_harvester.py\n---\n\n"
                "保留 /Users/example/outside.md\n",
            )
            original = read_text(generated)
            os.symlink(outside_projects, os.path.join(vault, "01-Projects"))

            changed = repair_generated_vault_markdown({"vault_path": vault})

            self.assertEqual(changed, 0)
            self.assertEqual(read_text(generated), original)

    def test_graph_link_repair_rejects_symlinked_projects_root(self):
        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            outside_projects = os.path.join(root, "outside-projects")
            decisions = os.path.join(outside_projects, "proj/Memory/decisions.md")
            os.makedirs(vault)
            os.makedirs(os.path.dirname(decisions))
            write_text(decisions, "---\ndecisions: []\n---\n\n# Decisions\n")
            original = read_text(decisions)
            os.symlink(outside_projects, os.path.join(vault, "01-Projects"))

            with self.assertRaises(OSError):
                repair_generated_graph_links({"vault_path": vault})

            self.assertEqual(read_text(decisions), original)

    def test_graph_link_repair_rejects_symlinked_session_lookup(self):
        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            memory_dir = os.path.join(vault, "01-Projects/proj/Memory")
            decisions = os.path.join(memory_dir, "decisions.md")
            outside_sessions = os.path.join(root, "outside-sessions")
            outside_session = os.path.join(outside_sessions, "2026-07-13-outside.md")
            os.makedirs(memory_dir)
            os.makedirs(outside_sessions)
            write_text(
                decisions,
                "---\nproject: proj\ndecisions: []\n---\n\n"
                "- [2026-07-13] 保留安全引用 | session: outside-session\n",
            )
            write_text(
                outside_session,
                "---\nsession_id: outside-session\n---\n\n外部会话\n",
            )
            original_decisions = read_text(decisions)
            original_session = read_text(outside_session)
            os.symlink(outside_sessions, os.path.join(memory_dir, "sessions"))

            with self.assertRaises((OSError, ValueError)):
                repair_project_memory_graph_links({"vault_path": vault})

            self.assertEqual(read_text(decisions), original_decisions)
            self.assertEqual(read_text(outside_session), original_session)

    def test_frontmatter_write_requires_vault_pinned_root(self):
        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            outside_projects = os.path.join(root, "outside-projects")
            decisions = os.path.join(outside_projects, "proj/Memory/decisions.md")
            os.makedirs(vault)
            os.makedirs(os.path.dirname(decisions))
            write_text(decisions, "keep-outside\n")
            os.symlink(outside_projects, os.path.join(vault, "01-Projects"))
            target = os.path.join(vault, "01-Projects/proj/Memory/decisions.md")

            with self.assertRaises(OSError):
                write_markdown_frontmatter(
                    target,
                    {"decisions": []},
                    "# Decisions",
                    root=vault,
                )

            self.assertEqual(read_text(decisions), "keep-outside\n")

    def test_bad_path_cleanup_rejects_symlinked_users_root(self):
        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            outside_users = os.path.join(root, "outside-users")
            empty_note = os.path.join(outside_users, "empty.md")
            os.makedirs(vault)
            os.makedirs(outside_users)
            write_text(empty_note, "")
            os.symlink(outside_users, os.path.join(vault, "Users"))

            cleanup_bad_obsidian_path_artifacts({"vault_path": vault})

            self.assertTrue(os.path.isfile(empty_note))

    def test_project_memory_repair_adds_missing_project_frontmatter(self):
        with tempfile.TemporaryDirectory() as vault:
            memory_dir = os.path.join(vault, "01-Projects/demo/Memory")
            os.makedirs(memory_dir)
            for filename, key in (("decisions.md", "decisions"), ("pitfalls.md", "pitfalls")):
                write_text(
                    os.path.join(memory_dir, filename),
                    f"---\n{key}: []\n---\n\n## Related\n\n- [[03-Maps/timeline|Timeline]]\n",
                )

            changed = repair_project_memory_graph_links({"vault_path": vault})

            self.assertEqual(changed, 2)
            for filename in ("decisions.md", "pitfalls.md"):
                frontmatter = yaml.safe_load(
                    read_text(os.path.join(memory_dir, filename)).split("---", 2)[1]
                )
                self.assertEqual(frontmatter["project"], "demo")

    def test_personal_memory_repair_preserves_global_scope_project_marker(self):
        with tempfile.TemporaryDirectory() as vault:
            formal = os.path.join(vault, "05-Agent-Memory/personal-memory.md")
            os.makedirs(os.path.dirname(formal))
            write_text(
                formal,
                """---
schema_version: '2.0'
---

# Personal Memory

## Global preference

- id: `global-memory`
- scope: `global`
- project: `global`

## Project rule

- id: `project-memory`
- scope: `project`
- project: `demo`
""",
            )

            changed = repair_personal_memory_graph_links({"vault_path": vault})

            self.assertEqual(changed, 1)
            content = read_text(formal)
            self.assertIn("- project: `global`", content)
            self.assertNotIn("01-Projects/global", content)
            self.assertIn(
                "- project: [[01-Projects/demo/Memory/decisions|demo]]",
                content,
            )

    def test_markdown_annotation_examples_are_not_harvested(self):
        text = """下面只是格式示例：

```text
[DECISION:示例决定| context:不应收割]
[ERROR:type=other| resolution:示例错误]
[SESSION_SUMMARY]
summary: 示例总结
[/SESSION_SUMMARY]
[FAVOR:示例偏好| context:不应收割| type:preference]
```

    [DECISION:缩进代码示例| context:也不应收割]
    [FAVOR:缩进偏好示例| context:也不应收割| type:preference]
"""

        self.assertEqual(extract_decisions(text), [])
        self.assertEqual(extract_errors(text), [])
        self.assertIsNone(extract_session_summary(text))
        self.assertEqual(
            extract_memory_candidates(
                [{"role": "assistant", "text": text}],
                "proj",
            ),
            [],
        )

    def test_backup_status_does_not_hide_unharvested_or_changed_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcript = os.path.join(tmp, "session.jsonl")
            feedback = os.path.join(vault, "04-Feedback")
            os.makedirs(feedback)
            write_text(transcript, "{}\n")
            write_text(
                os.path.join(feedback, "heartbeat.md"),
                """---
processed_sessions:
  session: legacy-backup-value
harvested_sessions: {}
---
""",
            )
            cfg = {
                "vault_path": vault,
                "agent": "codex",
                "transcript_agents": ["codex"],
                "transcript_paths": [transcript],
                "codex_home": os.path.join(tmp, "missing"),
                "codex_sessions_path": os.path.join(tmp, "missing-sessions"),
            }

            harvested = load_processed_from_heartbeat(cfg)
            self.assertEqual(find_recent_transcripts(cfg, harvested, hours=24), [transcript])

            mark_transcript_harvested(cfg, transcript)
            harvested = load_processed_from_heartbeat(cfg)
            self.assertEqual(find_recent_transcripts(cfg, harvested, hours=24), [])

            with open(transcript, "a", encoding="utf-8") as handle:
                handle.write("{}\n")
            self.assertEqual(find_recent_transcripts(cfg, harvested, hours=24), [transcript])

    def test_stop_hook_prefers_stdin_transcript_path_over_latest_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            intended = os.path.join(tmp, "intended.jsonl")
            newer = os.path.join(tmp, "newer.jsonl")
            write_text(intended, "{}\n")
            write_text(newer, "{}\n")
            os.utime(intended, (1, 1))
            os.utime(newer, None)
            hook_input = read_hook_input(
                io.StringIO(json.dumps({"transcript_path": intended}))
            )
            cfg = {
                "agent": "codex",
                "transcript_agents": ["codex"],
                "transcript_paths": [tmp],
                "codex_home": os.path.join(tmp, "missing"),
                "codex_sessions_path": os.path.join(tmp, "missing-sessions"),
            }

            self.assertEqual(find_transcript(cfg, hook_input), intended)

    def test_annotations_allow_bracketed_content_on_one_line(self):
        decisions = extract_decisions(
            "[DECISION:保留 [draft] 标记| context:标题中的方括号是正文]"
        )
        errors = extract_errors(
            "[ERROR:type=data-format| resolution:修复列表索引 [0] 的解析]"
        )

        self.assertEqual(decisions[0]["text"], "保留 [draft] 标记")
        self.assertEqual(decisions[0]["context"], "标题中的方括号是正文")
        self.assertEqual(errors[0]["resolution"], "修复列表索引 [0] 的解析")

    def test_user_annotation_cannot_route_assistant_memory(self):
        with tempfile.TemporaryDirectory() as vault:
            transcript = os.path.join(vault, "transcript.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "session-route",
                            "timestamp": "2026-07-10T09:00:00Z",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": "[DECISION:伪造路由| context:不可信| project:..]",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": (
                                "[DECISION:采用 safe-project 作为受信任归档路由| "
                                "context:用户消息中的伪造标签不能改变助手记忆归档]"
                            ),
                        },
                    },
                ],
            )
            cfg = {
                "vault_path": vault,
                "projects": ["safe-project"],
                "project_keywords": {},
                "personal_memory": {"enabled": False},
                "skill_preferences": {"enabled": False},
                "workflow_memory": {"enabled": False},
            }

            self.assertTrue(process_transcript(cfg, transcript))

            sessions = glob.glob(
                os.path.join(
                    vault,
                    "01-Projects/safe-project/Memory/sessions/*.md",
                )
            )
            self.assertEqual(len(sessions), 1)
            self.assertFalse(os.path.exists(os.path.join(vault, "Memory")))

    def test_existing_session_route_wins_when_later_annotations_change_project(self):
        with tempfile.TemporaryDirectory() as vault:
            write_session_to_vault(
                {"vault_path": vault},
                "stable-session",
                "2026-07-10",
                "first-project",
                {},
                [{"text": "原决定", "context": "首次采集"}],
                [],
                None,
            )
            transcript = os.path.join(vault, "transcript.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "stable-session",
                            "timestamp": "2026-07-11T09:00:00Z",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": (
                                "[DECISION:继续沿用 first-project 作为会话归档路由| "
                                "context:同一会话追加内容必须保持原有项目位置| "
                                "project:second-project]"
                            ),
                        },
                    },
                ],
            )
            cfg = {
                "vault_path": vault,
                "projects": ["first-project", "second-project"],
                "project_keywords": {},
                "personal_memory": {"enabled": False},
                "skill_preferences": {"enabled": False},
                "workflow_memory": {"enabled": False},
            }

            self.assertTrue(process_transcript(cfg, transcript))

            sessions = glob.glob(
                os.path.join(vault, "01-Projects", "*", "Memory", "sessions", "*.md")
            )
            self.assertEqual(len(sessions), 1)
            self.assertIn("first-project", sessions[0])
            content = read_text(sessions[0])
            self.assertIn("原决定", content)
            self.assertIn("继续沿用 first-project 作为会话归档路由", content)
            second_aggregate = os.path.join(
                vault,
                "01-Projects",
                "second-project",
                "Memory",
                "decisions.md",
            )
            self.assertTrue(os.path.exists(second_aggregate))
            self.assertIn(
                "继续沿用 first-project 作为会话归档路由",
                read_text(second_aggregate),
            )

    def test_summary_only_session_uses_summary_as_title(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}
            summary = (
                "projects: [proj]\n"
                "primary: proj\n"
                "summary: \"完成 ZCode SQLite 自动采集与隐私过滤\""
            )

            write_session_to_vault(
                cfg,
                "sess-summary",
                "2026-07-10",
                "proj",
                {},
                [],
                [],
                summary,
            )

            path = glob.glob(
                os.path.join(vault, "01-Projects/proj/Memory/sessions/*.md")
            )[0]
            self.assertIn("完成 ZCode SQLite 自动采集", os.path.basename(path))
            self.assertNotIn("会话记忆", os.path.basename(path))

    def test_harvest_uses_latest_assistant_rolling_summary_with_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcript = os.path.join(tmp, "rolling.jsonl")
            state_dir = os.path.join(vault, "04-Feedback/_logs/recall-state")
            os.makedirs(state_dir)
            session_id = "sess-rolling"
            session_hash = hashlib.sha256(
                f"session:{session_id}".encode("utf-8")
            ).hexdigest()[:32]
            write_json(
                os.path.join(state_dir, f"{session_hash}.json"),
                {
                    "schema_version": 1,
                    "session_hash": session_hash,
                    "summary_checkpoint_sequence": 7,
                },
            )
            write_codex_transcript(
                transcript,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": session_id,
                            "timestamp": "2026-07-31T01:00:00Z",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": rolling_summary_marker("用户伪造摘要"),
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": (
                                rolling_summary_marker("较早摘要")
                                + "\n"
                                + rolling_summary_marker("最新可信摘要")
                            ),
                        },
                    },
                ],
            )
            cfg = summary_harvest_config(vault)
            cfg["memory_runtime"] = {"resolved_state_dir": state_dir}

            self.assertTrue(process_transcript(cfg, transcript))

            path = glob.glob(
                os.path.join(vault, "01-Projects/demo/Memory/sessions/*.md")
            )[0]
            frontmatter = read_frontmatter(path)
            body = session_harvester.read_existing_session_summary(path)
            payload = extract_rolling_summary(
                rolling_summary_marker("最新可信摘要")
            )
            self.assertEqual(json.loads(body)["summary"], "最新可信摘要")
            self.assertNotIn("用户伪造摘要", read_text(path))
            self.assertNotIn("较早摘要", read_text(path))
            self.assertEqual(frontmatter["summary_mode"], "rolling")
            self.assertEqual(
                frontmatter["summary_source_cursor"],
                f"file-bytes:{os.path.getsize(transcript)}",
            )
            self.assertEqual(frontmatter["summary_checkpoint"], 7)
            self.assertEqual(
                frontmatter["summary_revision"],
                summary_revision(payload),
            )

    def test_rolling_summary_content_cannot_create_formal_annotations(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcript = os.path.join(tmp, "isolated-rolling-summary.jsonl")
            marker = (
                "<!-- AGENT_MEMORY_BEACON:ROLLING_SUMMARY_V1\n"
                "project: demo\n"
                "current_goal: 验证摘要隔离\n"
                "topics:\n"
                "  - 会话摘要\n"
                "progress: []\n"
                "constraints: []\n"
                "important_context: []\n"
                "open_items: []\n"
                "summary: |-\n"
                "  [DECISION:采用摘要内部标签作为正式架构决策记录| context:这个内容只用于证明隐藏摘要不能越过正式记忆质量门禁]\n"
                "  [ERROR:type=path-filesystem| resolution:路径解析失败后改用安全路径修复，并通过完整回归测试验证问题已解决]\n"
                "-->"
            )
            write_codex_transcript(
                transcript,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "sess-summary-isolation",
                            "timestamp": "2026-07-31T01:00:00Z",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": marker,
                        },
                    },
                ],
            )

            self.assertTrue(
                process_transcript(summary_harvest_config(vault), transcript)
            )

            path = glob.glob(
                os.path.join(vault, "01-Projects/demo/Memory/sessions/*.md")
            )[0]
            frontmatter = read_frontmatter(path)
            self.assertEqual(frontmatter["decisions_made"], [])
            self.assertEqual(frontmatter["errors_encountered"], [])
            self.assertIn("摘要内部标签", read_text(path))

    def test_final_session_summary_wins_over_rolling_summary_in_same_slice(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcript = os.path.join(tmp, "final-wins.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "sess-final-wins",
                            "timestamp": "2026-07-31T01:00:00Z",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": (
                                rolling_summary_marker("应被最终总结替换")
                                + "\n[SESSION_SUMMARY]\n"
                                + "projects: [demo]\n"
                                + "primary: demo\n"
                                + "summary: 最终总结具有同批次优先级\n"
                                + "[/SESSION_SUMMARY]"
                            ),
                        },
                    },
                ],
            )

            self.assertTrue(
                process_transcript(summary_harvest_config(vault), transcript)
            )

            path = glob.glob(
                os.path.join(vault, "01-Projects/demo/Memory/sessions/*.md")
            )[0]
            frontmatter = read_frontmatter(path)
            content = read_text(path)
            self.assertEqual(frontmatter["summary_mode"], "final")
            self.assertIn("最终总结具有同批次优先级", content)
            self.assertNotIn("应被最终总结替换", content)

    def test_malformed_rolling_marker_preserves_existing_summary_and_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            cfg = summary_harvest_config(vault)
            write_session_to_vault(
                cfg,
                "sess-malformed",
                "2026-07-31",
                "demo",
                {},
                [],
                [],
                rolling_summary_json("保留原摘要"),
                summary_mode="rolling",
                summary_source_cursor="file-bytes:10",
                summary_checkpoint=3,
            )
            path = glob.glob(
                os.path.join(vault, "01-Projects/demo/Memory/sessions/*.md")
            )[0]
            original_frontmatter = read_frontmatter(path)
            transcript = os.path.join(tmp, "malformed.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "sess-malformed",
                            "timestamp": "2026-07-31T01:00:00Z",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": (
                                "<!-- AGENT_MEMORY_BEACON:ROLLING_SUMMARY_V1\n"
                                "current_goal: broken\n"
                                "topics: [missing summary]\n"
                                "-->"
                            ),
                        },
                    },
                ],
            )

            self.assertFalse(process_transcript(cfg, transcript))

            frontmatter = read_frontmatter(path)
            self.assertEqual(
                session_harvester.read_existing_session_summary(path),
                rolling_summary_json("保留原摘要"),
            )
            for field in (
                "summary_mode",
                "summary_revision",
                "summary_updated_at",
                "summary_source_cursor",
                "summary_checkpoint",
            ):
                self.assertEqual(frontmatter[field], original_frontmatter[field])

    def test_rolling_summary_is_redacted_before_revision_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcript = os.path.join(tmp, "redacted-summary.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "sess-redacted-summary",
                            "timestamp": "2026-07-31T01:00:00Z",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": rolling_summary_marker(
                                "password=summary-secret-value"
                            ),
                        },
                    },
                ],
            )

            self.assertTrue(
                process_transcript(summary_harvest_config(vault), transcript)
            )

            path = glob.glob(
                os.path.join(vault, "01-Projects/demo/Memory/sessions/*.md")
            )[0]
            content = read_text(path)
            payload = json.loads(
                session_harvester.read_existing_session_summary(path)
            )
            self.assertNotIn("summary-secret-value", content)
            self.assertIn("[REDACTED]", content)
            self.assertEqual(
                read_frontmatter(path)["summary_revision"],
                summary_revision(payload),
            )

    def test_decision_only_update_preserves_summary_provenance(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = summary_harvest_config(vault)
            write_session_to_vault(
                cfg,
                "sess-summary-provenance",
                "2026-07-31",
                "demo",
                {},
                [],
                [],
                rolling_summary_json("已有摘要"),
                summary_mode="rolling",
                summary_source_cursor="file-bytes:20",
                summary_checkpoint=5,
            )
            path = glob.glob(
                os.path.join(vault, "01-Projects/demo/Memory/sessions/*.md")
            )[0]
            original = read_frontmatter(path)

            write_session_to_vault(
                cfg,
                "sess-summary-provenance",
                "2026-07-31",
                "demo",
                {},
                [{"text": "新增决定", "context": "不应改写摘要来源"}],
                [],
                None,
            )

            updated = read_frontmatter(path)
            self.assertIn("新增决定", read_text(path))
            for field in (
                "summary_mode",
                "summary_revision",
                "summary_updated_at",
                "summary_source_cursor",
                "summary_checkpoint",
            ):
                self.assertEqual(updated[field], original[field])

    def test_summary_replacement_requires_a_newer_source_cursor(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = summary_harvest_config(vault)
            write_session_to_vault(
                cfg,
                "sess-summary-cursor",
                "2026-07-31",
                "demo",
                {},
                [],
                [],
                rolling_summary_json("当前有效摘要"),
                summary_mode="rolling",
                summary_source_cursor="file-bytes:20",
                summary_checkpoint=2,
            )
            path = glob.glob(
                os.path.join(vault, "01-Projects/demo/Memory/sessions/*.md")
            )[0]
            original = read_frontmatter(path)

            write_session_to_vault(
                cfg,
                "sess-summary-cursor",
                "2026-07-31",
                "demo",
                {},
                [{"text": "保留新游标", "context": "旧批次只能追加正式注解"}],
                [],
                rolling_summary_json("旧批次摘要"),
                summary_mode="rolling",
                summary_source_cursor="file-bytes:10",
                summary_checkpoint=1,
            )
            stale = read_frontmatter(path)
            self.assertIn("保留新游标", read_text(path))
            self.assertEqual(
                session_harvester.read_existing_session_summary(path),
                rolling_summary_json("当前有效摘要"),
            )
            self.assertEqual(stale["summary_source_cursor"], "file-bytes:20")
            self.assertEqual(
                stale["summary_revision"],
                original["summary_revision"],
            )

            equal_result = write_session_to_vault(
                cfg,
                "sess-summary-cursor",
                "2026-07-31",
                "demo",
                {},
                [],
                [],
                rolling_summary_json("同游标冲突摘要"),
                summary_mode="rolling",
                summary_source_cursor="file-bytes:20",
                summary_checkpoint=2,
            )
            self.assertEqual(equal_result, 0)
            self.assertEqual(
                session_harvester.read_existing_session_summary(path),
                rolling_summary_json("当前有效摘要"),
            )

            write_session_to_vault(
                cfg,
                "sess-summary-cursor",
                "2026-07-31",
                "demo",
                {},
                [],
                [],
                rolling_summary_json("更新后的摘要"),
                summary_mode="rolling",
                summary_source_cursor="file-bytes:30",
                summary_checkpoint=3,
            )
            updated = read_frontmatter(path)
            self.assertEqual(
                session_harvester.read_existing_session_summary(path),
                rolling_summary_json("更新后的摘要"),
            )
            self.assertEqual(updated["summary_source_cursor"], "file-bytes:30")
            self.assertEqual(updated["summary_checkpoint"], 3)

    def test_untrusted_date_cannot_escape_session_directory(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}

            write_session_to_vault(
                cfg,
                "sess-date",
                "../../outside",
                "proj",
                {},
                [{"text": "安全日期", "context": "日期来自 transcript"}],
                [],
                None,
            )

            session_root = os.path.realpath(
                os.path.join(vault, "01-Projects/proj/Memory/sessions")
            )
            files = glob.glob(os.path.join(session_root, "*.md"))
            self.assertEqual(len(files), 1)
            self.assertEqual(
                os.path.commonpath([session_root, os.path.realpath(files[0])]),
                session_root,
            )

    def test_harvested_annotations_redact_credentials_and_payment_data(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}

            write_session_to_vault(
                cfg,
                "sess-secret",
                "2026-07-10",
                "proj",
                {},
                [
                    {
                        "text": "不要保存 password=super-secret-value",
                        "context": "银行卡号=4111111111111111",
                    }
                ],
                [],
                None,
            )

            path = glob.glob(
                os.path.join(vault, "01-Projects/proj/Memory/sessions/*.md")
            )[0]
            content = read_text(path)
            self.assertNotIn("super-secret-value", content)
            self.assertNotIn("4111111111111111", content)
            self.assertIn("[REDACTED]", content)

    def test_late_summary_is_added_to_existing_session(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}

            write_session_to_vault(
                cfg,
                "sess-1",
                "2026-07-04",
                "proj",
                {},
                [{"text": "决定A", "context": "原因A"}],
                [],
                None,
            )
            result = write_session_to_vault(
                cfg,
                "sess-1",
                "2026-07-04",
                "proj",
                {},
                [{"text": "决定A", "context": "原因A"}],
                [],
                "这是后来的总结",
            )

            path = glob.glob(
                os.path.join(vault, "01-Projects/proj/Memory/sessions/*.md")
            )[0]
            content = read_text(path)
            self.assertEqual(result, 1)
            self.assertIn("这是后来的总结", content)

    def test_same_session_id_is_reused_when_transcript_date_changes(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}

            write_session_to_vault(
                cfg,
                "sess-cross-day",
                "2026-07-10",
                "proj",
                {},
                [{"text": "第一天决定", "context": "首次采集"}],
                [],
                None,
            )
            write_session_to_vault(
                cfg,
                "sess-cross-day",
                "2026-07-11",
                "proj",
                {},
                [{"text": "第二天决定", "context": "续聊采集"}],
                [],
                None,
            )

            sessions = glob.glob(
                os.path.join(vault, "01-Projects/proj/Memory/sessions/*.md")
            )
            self.assertEqual(len(sessions), 1)
            self.assertTrue(os.path.basename(sessions[0]).startswith("2026-07-10-"))
            content = read_text(sessions[0])
            self.assertIn("第一天决定", content)
            self.assertIn("第二天决定", content)

    def test_immutable_first_harvest_marker_keeps_canonical_duplicate_route(self):
        with tempfile.TemporaryDirectory() as vault:
            first = os.path.join(
                vault,
                "01-Projects/alpha/Memory/sessions/2026-07-10-first.md",
            )
            second = os.path.join(
                vault,
                "01-Projects/beta/Memory/sessions/2026-07-10-second.md",
            )
            os.makedirs(os.path.dirname(first))
            os.makedirs(os.path.dirname(second))
            write_text(
                first,
                """---
session_id: duplicate-session
date: '2026-07-10'
project: alpha
first_harvested_at: '2026-07-01T00:00:00+08:00'
harvested_at: '2026-07-11T00:00:00+08:00'
decisions_made: []
errors_encountered: []
---

# First
""",
            )
            write_text(
                second,
                """---
session_id: duplicate-session
date: '2026-07-10'
project: beta
harvested_at: '2026-07-02T00:00:00+08:00'
decisions_made: []
errors_encountered: []
---

# Second
""",
            )

            write_session_to_vault(
                {"vault_path": vault},
                "duplicate-session",
                "2026-07-11",
                "beta",
                {},
                [{"text": "规范路径更新", "context": "不可变创建标记"}],
                [],
                None,
            )

            self.assertIn("规范路径更新", read_text(first))
            self.assertNotIn("规范路径更新", read_text(second))

    def test_legacy_session_adds_first_harvest_marker_without_new_content(self):
        with tempfile.TemporaryDirectory() as vault:
            session = os.path.join(
                vault,
                "01-Projects/proj/Memory/sessions/2026-07-10-existing.md",
            )
            os.makedirs(os.path.dirname(session))
            write_text(
                session,
                """---
session_id: legacy-session
date: '2026-07-10'
project: proj
projects: [proj]
ai_title: 已有决定
summary_status: draft
summary_type: session
decisions_made:
- text: 已有决定
  context: 已有原因
errors_encountered: []
tags: []
harvested_by: session_harvester.py
harvested_at: '2026-07-10T09:00:00+08:00'
---

# 已有决定
""",
            )

            result = write_session_to_vault(
                {"vault_path": vault},
                "legacy-session",
                "2026-07-10",
                "proj",
                {},
                [{"text": "已有决定", "context": "已有原因"}],
                [],
                None,
            )

            frontmatter = yaml.safe_load(read_text(session).split("---", 2)[1])
            self.assertEqual(result, 1)
            self.assertEqual(
                frontmatter["first_harvested_at"],
                "2026-07-10T09:00:00+08:00",
            )
            self.assertEqual(len(frontmatter["decisions_made"]), 1)

    def test_late_summary_renames_generic_session_file(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}
            decision = [{"text": "...", "context": "占位"}]
            write_session_to_vault(
                cfg,
                "sess-generic",
                "2026-07-04",
                "proj",
                {},
                decision,
                [],
                None,
            )

            write_session_to_vault(
                cfg,
                "sess-generic",
                "2026-07-04",
                "proj",
                {},
                decision,
                [],
                "summary: 完成会话标题修复",
            )

            files = glob.glob(
                os.path.join(vault, "01-Projects/proj/Memory/sessions/*.md")
            )
            self.assertEqual(len(files), 1)
            self.assertIn("完成会话标题修复", os.path.basename(files[0]))
            self.assertNotIn("会话记忆", os.path.basename(files[0]))

    def test_late_summary_renames_generic_session_file_after_name_collision(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}
            placeholder = [{"text": "...", "context": "占位"}]
            for session_id in ("sess-first", "sess-second"):
                write_session_to_vault(
                    cfg,
                    session_id,
                    "2026-07-04",
                    "proj",
                    {},
                    placeholder,
                    [],
                    None,
                )

            write_session_to_vault(
                cfg,
                "sess-second",
                "2026-07-04",
                "proj",
                {},
                placeholder,
                [],
                "summary: 完成冲突会话标题修复",
            )

            sessions_dir = os.path.join(
                vault, "01-Projects/proj/Memory/sessions"
            )
            files = glob.glob(os.path.join(sessions_dir, "*.md"))
            second = [
                path for path in files
                if "session_id: sess-second" in read_text(path)
            ]
            self.assertEqual(len(second), 1)
            self.assertIn("完成冲突会话标题修复", os.path.basename(second[0]))
            self.assertNotIn("会话记忆", os.path.basename(second[0]))

    def test_existing_summary_is_preserved_when_new_error_is_added(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}

            write_session_to_vault(
                cfg,
                "sess-1",
                "2026-07-04",
                "proj",
                {},
                [{"text": "决定A", "context": "原因A"}],
                [],
                "已有总结",
            )
            write_session_to_vault(
                cfg,
                "sess-1",
                "2026-07-04",
                "proj",
                {},
                [{"text": "决定A", "context": "原因A"}],
                [{"type": "path-filesystem", "resolution": "修复路径"}],
                None,
            )

            path = glob.glob(
                os.path.join(vault, "01-Projects/proj/Memory/sessions/*.md")
            )[0]
            content = read_text(path)
            self.assertIn("已有总结", content)
            self.assertIn("path-filesystem", content)

    def test_markdown_repair_does_not_rewrite_manual_project_notes(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}
            manual_path = os.path.join(vault, "01-Projects/proj/manual.md")
            generated_path = os.path.join(
                vault, "01-Projects/proj/Memory/sessions/2026-07-04-demo.md"
            )
            os.makedirs(os.path.dirname(manual_path), exist_ok=True)
            os.makedirs(os.path.dirname(generated_path), exist_ok=True)
            manual_content = "手写笔记 /Users/example/ObsidianBrain/foo.md\n"
            generated_content = (
                "---\n"
                "harvested_by: session_harvester.py\n"
                "---\n\n"
                "生成笔记 /Users/example/ObsidianBrain/foo.md\n"
            )
            write_text(manual_path, manual_content)
            write_text(generated_path, generated_content)

            changed = repair_generated_vault_markdown(cfg)

            self.assertEqual(read_text(manual_path), manual_content)
            self.assertIn("`/Users/example/ObsidianBrain/foo.md`", read_text(generated_path))
            self.assertEqual(changed, 1)

    def test_generated_markdown_repair_preserves_revision_but_redacts_card(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}
            generated = os.path.join(
                vault,
                "01-Projects/proj/Memory/decisions.md",
            )
            os.makedirs(os.path.dirname(generated), exist_ok=True)
            revision = "a" * 10 + "4111111111111111" + "b" * 38
            write_text(
                generated,
                "---\n"
                "project: proj\n"
                "schema_version: '2.0'\n"
                "decisions:\n"
                "- id: decision-test\n"
                f"  revision: {revision}\n"
                "  text: 测试隐私清洗\n"
                "  context: 普通上下文\n"
                "  status: active\n"
                "---\n\n"
                "测试卡号 4111 1111 1111 1111 必须隐藏\n",
            )

            changed = repair_generated_vault_markdown(cfg)

            self.assertEqual(changed, 1)
            content = read_text(generated)
            self.assertIn(f"revision: {revision}", content)
            self.assertIn("测试卡号 [REDACTED CARD] 必须隐藏", content)

    def test_generated_markdown_repair_refreshes_changed_runtime_revisions(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = {"vault_path": vault}
            decisions = os.path.join(
                vault,
                "01-Projects/proj/Memory/decisions.md",
            )
            personal = os.path.join(vault, "05-Agent-Memory/personal-memory.md")
            os.makedirs(os.path.dirname(decisions), exist_ok=True)
            os.makedirs(os.path.dirname(personal), exist_ok=True)
            decision = {
                "type": "decision",
                "status": "active",
                "project": "proj",
                "scope": "project",
                "title": "清洗敏感内容",
                "summary": "卡号 4111 1111 1111 1111 必须隐藏",
            }
            old_revision = memory_revision(decision)
            write_text(
                decisions,
                "---\n"
                "project: proj\n"
                "schema_version: '2.0'\n"
                "decisions:\n"
                "- id: decision-sensitive\n"
                f"  revision: {old_revision}\n"
                "  text: 清洗敏感内容\n"
                "  context: 卡号 4111 1111 1111 1111 必须隐藏\n"
                "  status: active\n"
                "  project: proj\n"
                "  scope: project\n"
                "  source_refs: [session:sensitive]\n"
                "---\n",
            )
            personal_section = render_formal_memory_entry(
                {
                    "memory_id": "preference-sensitive",
                    "title": "用户偏好: 隐藏支付信息",
                    "content": "卡号 4111 1111 1111 1111 必须隐藏",
                    "type": "preference",
                    "source_ids": ["sess-sensitive"],
                }
            )
            write_text(
                personal,
                "---\n"
                "generated_by: memory_judge.py\n"
                "schema_version: '2.0'\n"
                "---\n\n"
                + personal_section,
            )

            changed = repair_generated_vault_markdown(cfg)

            self.assertEqual(changed, 2)
            aggregate = yaml.safe_load(read_text(decisions).split("---", 2)[1])[
                "decisions"
            ][0]
            self.assertEqual(aggregate["context"], "卡号 [REDACTED CARD] 必须隐藏")
            self.assertNotEqual(aggregate["revision"], old_revision)
            self.assertEqual(
                aggregate["revision"],
                memory_revision(
                    {
                        "type": "decision",
                        "status": "active",
                        "project": "proj",
                        "scope": "project",
                        "title": aggregate["text"],
                        "summary": aggregate["context"],
                    }
                ),
            )
            personal_text = read_text(personal)
            title = "用户偏好: 隐藏支付信息"
            section = personal_text.split(f"## {title}", 1)[1]
            self.assertIsNotNone(
                parse_active_formal_section(title, section, "personal")
            )
            self.assertIn("- memory: 卡号 [REDACTED CARD] 必须隐藏", personal_text)

    def test_find_transcript_uses_zcode_session_env_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "db.sqlite")
            write_minimal_zcode_db(db_path, "sess-z")
            old_db = os.environ.get("ZCODE_SESSION_DB")
            old_session = os.environ.get("ZCODE_SESSION_ID")
            os.environ["ZCODE_SESSION_DB"] = db_path
            os.environ["ZCODE_SESSION_ID"] = "sess-z"
            try:
                self.assertEqual(find_transcript({}), db_path + "::sess-z")
            finally:
                restore_env("ZCODE_SESSION_DB", old_db)
                restore_env("ZCODE_SESSION_ID", old_session)

    def test_codex_profile_check_warns_about_missing_state_on_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            codex_home = os.path.join(tmp, "codex")
            os.makedirs(profile_dir, exist_ok=True)
            os.makedirs(codex_home, exist_ok=True)
            write_json(
                os.path.join(profile_dir, "skills-manifest.json"),
                {
                    "skills": [
                        {
                            "name": "custom",
                            "path": "skills/custom",
                            "digest": "expected",
                        }
                    ]
                },
            )
            write_json(
                os.path.join(profile_dir, "plugins-manifest.json"),
                {
                    "plugins": [
                        {
                            "id": "pensive@claude-night-market",
                            "enabled": True,
                        }
                    ]
                },
            )
            write_text(os.path.join(codex_home, "config.toml"), "")

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                changed = check_codex_profile_on_start(
                    {
                        "agent": "codex",
                        "codex_home": codex_home,
                        "codex_profile_path": profile_dir,
                    }
                )

            output = buffer.getvalue()
            self.assertTrue(changed)
            self.assertIn("[codex-profile] 当前账号和共享 profile 不一致", output)
            self.assertIn("Missing skills: custom", output)
            self.assertIn("Missing enabled plugins: pensive@claude-night-market", output)
            self.assertIn("codex_profile_sync.py apply --include-config --dry-run", output)

    def test_codex_profile_changed_skill_suggests_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            codex_home = os.path.join(tmp, "codex")
            write_json(
                os.path.join(profile_dir, "skills-manifest.json"),
                {
                    "skills": [
                        {
                            "name": "custom",
                            "path": "skills/custom",
                            "digest": "expected-digest",
                        }
                    ]
                },
            )
            write_json(
                os.path.join(profile_dir, "plugins-manifest.json"),
                {"plugins": []},
            )
            write_text(
                os.path.join(codex_home, "skills/custom/SKILL.md"),
                "---\nname: custom\n---\nold content\n",
            )

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                changed = check_codex_profile_on_start(
                    {
                        "codex_home": codex_home,
                        "codex_profile_path": profile_dir,
                    }
                )

            self.assertTrue(changed)
            self.assertIn("--include-config --overwrite --dry-run", buffer.getvalue())
            self.assertIn("--include-config --overwrite", buffer.getvalue())

    def test_codex_profile_check_runs_even_when_transcript_agent_is_zcode(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            codex_home = os.path.join(tmp, "codex")
            os.makedirs(profile_dir, exist_ok=True)
            os.makedirs(codex_home, exist_ok=True)
            write_json(
                os.path.join(profile_dir, "skills-manifest.json"),
                {"skills": [{"name": "custom", "path": "skills/custom"}]},
            )
            write_json(os.path.join(profile_dir, "plugins-manifest.json"), {"plugins": []})

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                changed = check_codex_profile_on_start(
                    {
                        "agent": "zcode",
                        "codex_home": codex_home,
                        "codex_profile_path": profile_dir,
                    }
                )

            self.assertTrue(changed)
            self.assertIn("Missing skills: custom", buffer.getvalue())

    def test_codex_profile_check_is_silent_when_clean_on_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            codex_home = os.path.join(tmp, "codex")
            os.makedirs(profile_dir, exist_ok=True)
            os.makedirs(codex_home, exist_ok=True)
            write_json(os.path.join(profile_dir, "skills-manifest.json"), {"skills": []})
            write_json(os.path.join(profile_dir, "plugins-manifest.json"), {"plugins": []})

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                changed = check_codex_profile_on_start(
                    {
                        "agent": "codex",
                        "codex_home": codex_home,
                        "codex_profile_path": profile_dir,
                    }
                )

            self.assertFalse(changed)
            self.assertEqual(buffer.getvalue(), "")

    def test_process_transcript_prints_skill_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(vault, exist_ok=True)
            transcript = os.path.join(tmp, "skill-session.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "timestamp": "2026-07-06T01:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "sess-skill-1",
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-06T01:00:00Z",
                        },
                    },
                    {
                        "timestamp": "2026-07-06T01:01:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "这段中文太像 AI 了，用 $humanizer 改自然一点。",
                        },
                    },
                ],
            )
            cfg = {
                "vault_path": vault,
                "projects": [{"name": "demo", "keywords": ["demo"]}],
                "personal_memory": {"enabled": False},
                "skill_preferences": {
                    "enabled": True,
                    "candidate_dir": "04-Feedback/_skill-preferences",
                    "formal_path": "05-Agent-Memory/skill-routing-rules.md",
                    "promote_seen_count": 2,
                    "similarity_threshold": 0.5,
                },
            }

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                result = process_transcript(cfg, transcript)

            output = buffer.getvalue()
            self.assertTrue(result)
            self.assertIn("[skill-learner] CANDIDATE humanizer", output)
            self.assertIn("04-Feedback/_skill-preferences", output)
            candidates = glob.glob(
                os.path.join(vault, "04-Feedback/_skill-preferences/*.md")
            )
            self.assertEqual(len(candidates), 1)
            self.assertIn("negative_signals", read_text(candidates[0]))

    def test_process_transcript_prints_favor_annotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(vault, exist_ok=True)
            transcript = os.path.join(tmp, "favor-session.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "timestamp": "2026-07-07T01:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "sess-favor-1",
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-07T01:00:00Z",
                        },
                    },
                    {
                        "timestamp": "2026-07-07T01:01:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": "[FAVOR:默认用中文解释复杂功能| context:用户看不懂英文输出| project:demo]",
                        },
                    },
                ],
            )
            cfg = {
                "vault_path": vault,
                "projects": [{"name": "demo", "keywords": ["demo"]}],
                "personal_memory": {
                    "enabled": True,
                    "candidate_dir": "04-Feedback/_memory-candidates",
                    "formal_path": "05-Agent-Memory/personal-memory.md",
                    "candidate_threshold": 0.45,
                    "direct_threshold": 0.85,
                    "promote_seen_count": 2,
                    "similarity_threshold": 0.5,
                },
                "skill_preferences": {"enabled": False},
                "workflow_memory": {"enabled": False},
            }

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                result = process_transcript(cfg, transcript)

            output = buffer.getvalue()
            self.assertTrue(result)
            self.assertIn("[harvester] Personal memory:", output)
            self.assertIn("[PROMOTED]", output)
            self.assertIn("默认用中文解释复杂功能", output)
            formal = read_text(os.path.join(vault, "05-Agent-Memory/personal-memory.md"))
            self.assertIn("默认用中文解释复杂功能", formal)

    def test_process_transcript_prints_workflow_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(vault, exist_ok=True)
            transcript = os.path.join(tmp, "workflow-session.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "timestamp": "2026-07-06T02:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "sess-workflow-1",
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-06T02:00:00Z",
                        },
                    },
                    {
                        "timestamp": "2026-07-06T02:01:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "你先去 GitHub 看一下原代码，不要根据名字猜，先看 README。",
                        },
                    },
                ],
            )
            cfg = {
                "vault_path": vault,
                "projects": [{"name": "demo", "keywords": ["demo"]}],
                "personal_memory": {"enabled": False},
                "skill_preferences": {"enabled": False},
                "workflow_memory": {
                    "enabled": True,
                    "candidate_dir": "04-Feedback/_workflow-candidates",
                    "formal_path": "05-Agent-Memory/workflow-rules.md",
                    "promote_seen_count": 2,
                    "similarity_threshold": 0.5,
                },
            }

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                result = process_transcript(cfg, transcript)

            output = buffer.getvalue()
            self.assertTrue(result)
            self.assertIn("[workflow-learner] CANDIDATE github_source_first", output)
            self.assertIn("04-Feedback/_workflow-candidates", output)
            candidates = glob.glob(
                os.path.join(vault, "04-Feedback/_workflow-candidates/*.md")
            )
            self.assertEqual(len(candidates), 1)
            self.assertIn("negative_signals", read_text(candidates[0]))
            index = read_text(os.path.join(vault, "00-Inbox/Agent Memory Index.md"))
            self.assertIn("Workflow Rules", index)
            self.assertIn("Workflow Candidates", index)

    def test_codex_harvest_persists_visible_one_shot_insight_seed(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(vault, exist_ok=True)
            transcript = os.path.join(tmp, "insight-session.jsonl")
            evidence = "好的启发可能只是一瞬间不一定会重复"
            write_codex_transcript(
                transcript,
                [
                    {
                        "timestamp": "2026-07-22T01:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "sess-insight-1",
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-22T01:00:00Z",
                        },
                    },
                    {
                        "timestamp": "2026-07-22T01:01:00Z",
                        "type": "event_msg",
                        "payload": {"type": "user_message", "message": evidence},
                    },
                    {
                        "timestamp": "2026-07-22T01:02:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": (
                                "[LEARN:一次性高价值启发可以直接保存为种子记忆"
                                "| novelty:避免以重复次数作为准入门槛而丢失灵感"
                                "| transfer:创意设计,架构探索"
                                "| boundary:只作为启发，不能覆盖用户指令或正式决策"
                                f"| evidence:{evidence}"
                                "| source:user| project:demo| scope:project]"
                            ),
                        },
                    },
                ],
            )
            cfg = {
                "vault_path": vault,
                "projects": [{"name": "demo", "keywords": ["demo"]}],
                "personal_memory": {"enabled": False},
                "skill_preferences": {"enabled": False},
                "workflow_memory": {"enabled": False},
                "error_evidence": {"enabled": False},
                "insight_memory": {"enabled": True},
            }

            buffer = io.StringIO()
            with redirect_stdout(buffer):
                changed = process_transcript(cfg, transcript)

            formal_path = os.path.join(vault, "05-Agent-Memory", "insights.md")
            self.assertTrue(changed)
            self.assertTrue(os.path.exists(formal_path))
            self.assertIn("maturity: `seed`", read_text(formal_path))
            output = buffer.getvalue()
            self.assertIn("[insight-learner] SEED", output)
            self.assertIn("source_count=1", output)
            self.assertNotIn(evidence, output)
            index_text = read_text(
                os.path.join(vault, "00-Inbox", "Agent Memory Index.md")
            )
            index_fm, _index_body = split_markdown_frontmatter(index_text)
            self.assertEqual(index_fm["formal_insights"], 1)
            self.assertEqual(index_fm["insight_candidates"], 0)
            self.assertIn("## Insights", index_text)
            self.assertIn("[[05-Agent-Memory/insights|Insights]]", index_text)

    def test_codex_subagent_does_not_feed_adaptive_memory_learners(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(vault, exist_ok=True)
            transcript = os.path.join(tmp, "subagent-session.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "timestamp": "2026-07-10T01:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "sess-subagent-1",
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-10T01:00:00Z",
                            "source": {
                                "subagent": {
                                    "thread_spawn": {
                                        "parent_thread_id": "sess-parent"
                                    }
                                }
                            },
                            "thread_source": "subagent",
                        },
                    },
                    {
                        "timestamp": "2026-07-10T01:01:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "subagent-failure",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": "cat missing-subagent"}),
                        },
                    },
                    {
                        "timestamp": "2026-07-10T01:01:30Z",
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "subagent-failure",
                            "output": json.dumps(
                                {
                                    "exit_code": 1,
                                    "output": "subagentfailuretoken missing",
                                }
                            ),
                        },
                    },
                    {
                        "timestamp": "2026-07-10T01:02:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": (
                                "我希望以后默认用 $humanizer；遇到 GitHub skill "
                                "先查源码和 README，不要只看名字。"
                            ),
                        },
                    },
                    {
                        "timestamp": "2026-07-10T01:03:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": (
                                "[LEARN:子代理内部想法不能写入正式启发记忆"
                                "| novelty:隔离非用户主任务的推测"
                                "| transfer:代理任务隔离"
                                "| boundary:仅限主任务"
                                "| evidence:我希望以后默认用 $humanizer"
                                "| source:user| project:demo| scope:project]"
                            ),
                        },
                    },
                    {
                        "timestamp": "2026-07-10T01:02:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": (
                                "[FAVOR:默认把审查提示词当成长期偏好| "
                                "context:子代理内部指令| type:preference]\n"
                                + rolling_summary_marker("子代理摘要不得持久化")
                            ),
                        },
                    },
                ],
            )
            cfg = {
                "vault_path": vault,
                "projects": [{"name": "demo", "keywords": ["demo"]}],
                "personal_memory": {"enabled": True},
                "skill_preferences": {"enabled": True},
                "workflow_memory": {"enabled": True},
                "insight_memory": {"enabled": True},
            }

            result = process_transcript(cfg, transcript)

            self.assertFalse(result)
            self.assertFalse(
                os.path.exists(os.path.join(vault, "04-Feedback/_memory-candidates"))
            )
            self.assertFalse(
                os.path.exists(os.path.join(vault, "04-Feedback/_skill-preferences"))
            )
            self.assertEqual(
                glob.glob(
                    os.path.join(
                        vault,
                        "01-Projects/demo/Memory/sessions/*.md",
                    )
                ),
                [],
            )
            self.assertFalse(
                os.path.exists(os.path.join(vault, "04-Feedback/_workflow-candidates"))
            )
            self.assertFalse(
                os.path.exists(os.path.join(vault, "04-Feedback/_error-candidates"))
            )
            self.assertFalse(
                os.path.exists(os.path.join(vault, "04-Feedback/_insight-candidates"))
            )
            self.assertFalse(
                os.path.exists(os.path.join(vault, "05-Agent-Memory/insights.md"))
            )

    def test_user_resumed_subagent_thread_can_persist_requested_rolling_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            outer_session_id = "019ee90f-5f61-7db2-9c12-3f2813b04e2e"
            nested_session_id = "019edfac-7fec-7c02-892b-0d4792f7f24a"
            transcript = os.path.join(
                tmp,
                "rollout-2026-06-21T15-22-39-" + outer_session_id + ".jsonl",
            )
            write_codex_transcript(
                transcript,
                [
                    {
                        "timestamp": "2026-07-10T01:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": outer_session_id,
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-10T01:00:00Z",
                            "source": {
                                "subagent": {
                                    "thread_spawn": {
                                        "parent_thread_id": "sess-parent"
                                    }
                                }
                            },
                            "thread_source": "subagent",
                        },
                    },
                    {
                        "timestamp": "2026-07-10T01:00:01Z",
                        "type": "session_meta",
                        "payload": {
                            "id": nested_session_id,
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-10T01:00:01Z",
                            "source": "vscode",
                        },
                    },
                    {
                        "timestamp": "2026-07-10T01:02:00Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": (
                                "[FAVOR:子代理内部偏好不得晋升| "
                                "context:验证摘要恢复不放宽自适应记忆隔离| "
                                "type:preference]\n"
                                + rolling_summary_marker("用户恢复任务后的最新摘要")
                            ),
                        },
                    },
                ],
            )
            cfg = summary_harvest_config(vault)
            cfg["personal_memory"] = {"enabled": True}
            cfg["skill_preferences"] = {"enabled": True}
            cfg["workflow_memory"] = {"enabled": True}
            cfg["insight_memory"] = {"enabled": True}
            state_dir = os.path.join(
                vault,
                "04-Feedback/_logs/recall-state",
            )
            os.makedirs(state_dir, exist_ok=True)
            cfg["memory_runtime"] = {"resolved_state_dir": state_dir}
            session_hash = hashlib.sha256(
                f"thread:{outer_session_id}".encode("utf-8")
            ).hexdigest()[:32]
            write_json(
                os.path.join(state_dir, f"{session_hash}.json"),
                {
                    "schema_version": 1,
                    "session_hash": session_hash,
                    "summary_checkpoint_sequence": 1,
                },
            )

            self.assertTrue(process_transcript(cfg, transcript))

            sessions = glob.glob(
                os.path.join(vault, "01-Projects/demo/Memory/sessions/*.md")
            )
            self.assertEqual(len(sessions), 1)
            frontmatter = read_frontmatter(sessions[0])
            self.assertEqual(frontmatter["session_id"], nested_session_id)
            self.assertEqual(frontmatter["summary_mode"], "rolling")
            self.assertEqual(frontmatter["summary_checkpoint"], 1)
            self.assertIn("用户恢复任务后的最新摘要", read_text(sessions[0]))
            self.assertFalse(
                os.path.exists(os.path.join(vault, "05-Agent-Memory/personal-memory.md"))
            )
            self.assertFalse(
                os.path.exists(os.path.join(vault, "04-Feedback/_error-candidates"))
            )
            self.assertFalse(
                os.path.exists(os.path.join(vault, "04-Feedback/_insight-candidates"))
            )
            self.assertFalse(
                os.path.exists(os.path.join(vault, "05-Agent-Memory/insights.md"))
            )

    def test_non_codex_transcript_does_not_create_error_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(vault)
            transcript = os.path.join(tmp, "claude-session.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "type": "assistant",
                        "message": {"role": "assistant", "content": "ordinary reply"},
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "claude-failure",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": "cat missing-claude"}),
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "claude-failure",
                            "output": json.dumps(
                                {"exit_code": 1, "output": "claudefailuretoken missing"}
                            ),
                        },
                    },
                ],
            )
            cfg = {
                "vault_path": vault,
                "personal_memory": {"enabled": False},
                "skill_preferences": {"enabled": False},
                "workflow_memory": {"enabled": False},
                "error_evidence": {"enabled": True},
            }

            with patch("session_harvester.process_insight_memory") as insight_learner:
                self.assertFalse(process_transcript(cfg, transcript))
            insight_learner.assert_not_called()
            self.assertFalse(
                os.path.exists(os.path.join(vault, "04-Feedback/_error-candidates"))
            )

    def test_error_evidence_uses_codex_delta_without_inflating_formal_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            transcripts = os.path.join(tmp, "transcripts")
            os.makedirs(vault)
            os.makedirs(transcripts)
            transcript = os.path.join(transcripts, "error-evidence.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "timestamp": "2026-07-13T01:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "sess-error-evidence",
                            "agent": "codex",
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-13T01:00:00Z",
                        },
                    }
                ],
            )
            cfg = {
                "vault_path": vault,
                "transcript_paths": [transcripts],
                "transcript_agents": [],
                "projects": [{"name": "demo", "keywords": ["demo"]}],
                "personal_memory": {"enabled": False},
                "skill_preferences": {"enabled": False},
                "workflow_memory": {"enabled": False},
                "error_evidence": {
                    "enabled": True,
                    "candidate_dir": "04-Feedback/_error-candidates",
                    "excerpt_limit": 500,
                    "source_limit": 20,
                },
            }
            self.assertEqual(initialize_harvest_baseline(cfg), 1)

            records = []

            def tool_result(call_id, command, exit_code, output, *, is_test=False):
                command = f"python -m unittest {command}" if is_test else command
                records.extend(
                    [
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "function_call",
                                "call_id": call_id,
                                "name": "exec_command",
                                "arguments": json.dumps({"cmd": command}),
                            },
                        },
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "function_call_output",
                                "call_id": call_id,
                                "output": json.dumps(
                                    {"exit_code": exit_code, "output": output}
                                ),
                            },
                        },
                    ]
                )

            tool_result(
                "red-fail",
                "tests.test_expected_red",
                1,
                "expectedredtoken AssertionError",
                is_test=True,
            )
            tool_result(
                "red-pass",
                "tests.test_expected_red",
                0,
                "expectedredtoken OK",
                is_test=True,
            )
            tool_result("retry-fail", "git status", 1, "retrytoken temporary failure")
            tool_result("retry-pass", "git status", 0, "retrytoken recovered")
            tool_result(
                "terminal-fail",
                "cat missing-terminal",
                1,
                "terminalevidencetoken file is still missing",
            )
            tool_result(
                "formal-fail",
                "publish atomic state",
                2,
                "formalmatchtoken lock ownership lost during atomic publish",
            )
            records.extend(
                [
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": (
                                '<subagent_notification>{"status":{"reviewer":'
                                '{"completed":"### Important: reviewevidencetoken '
                                '缺少发布前漂移检查"}}}</subagent_notification>'
                            ),
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "agent_message",
                            "message": (
                                "[ERROR:type=logic| resolution=formalmatchtoken atomic "
                                "publish 因 lock ownership lost 失败，修复锁所有权校验并重跑测试通过| "
                                "project:demo]"
                            ),
                        },
                    },
                ]
            )
            with open(transcript, "a", encoding="utf-8") as handle:
                for record in records:
                    json.dump(record, handle, ensure_ascii=False)
                    handle.write("\n")

            output = io.StringIO()
            with redirect_stdout(output):
                changed = process_transcript(cfg, transcript)

            self.assertTrue(changed)
            self.assertIn("[error-evidence]", output.getvalue())
            self.assertIn("1 errors", output.getvalue())
            candidate_paths = sorted(
                glob.glob(os.path.join(vault, "04-Feedback/_error-candidates/*.md"))
            )
            self.assertEqual(len(candidate_paths), 2)
            candidates = [read_frontmatter(path) for path in candidate_paths]
            self.assertEqual(
                sorted(record["status"] for record in candidates),
                ["candidate", "resolved"],
            )
            unresolved_text = "\n".join(
                read_text(path)
                for path, record in zip(candidate_paths, candidates)
                if record["status"] == "candidate"
            )
            all_candidate_text = "\n".join(read_text(path) for path in candidate_paths)
            self.assertIn("diagnostic=process_failure", unresolved_text)
            self.assertNotIn("terminalevidencetoken", all_candidate_text)
            self.assertNotIn("reviewevidencetoken", all_candidate_text)
            self.assertNotIn("expectedredtoken", all_candidate_text)
            self.assertNotIn("retrytoken", all_candidate_text)
            self.assertTrue(
                any(
                    record.get("status") == "resolved"
                    and "lock_ownership_lost" in record.get("excerpt", "")
                    for record in candidates
                )
            )

            sessions = glob.glob(
                os.path.join(vault, "01-Projects/demo/Memory/sessions/*.md")
            )
            self.assertEqual(len(sessions), 1)
            session = read_frontmatter(sessions[0])
            self.assertEqual(len(session["errors_encountered"]), 1)
            self.assertIn("formalmatchtoken", session["errors_encountered"][0]["resolution"])
            pitfalls = read_frontmatter(
                os.path.join(vault, "01-Projects/demo/Memory/pitfalls.md")
            )
            self.assertEqual(len(pitfalls["pitfalls"]), 1)

            memory_index = read_frontmatter(
                os.path.join(vault, "00-Inbox/Agent Memory Index.md")
            )
            self.assertEqual(memory_index["error_evidence_candidates"], 1)
            first_seen_counts = {
                record["evidence_id"]: record["seen_count"] for record in candidates
            }

            with redirect_stdout(io.StringIO()):
                process_transcript(cfg, transcript)

            replayed = [read_frontmatter(path) for path in candidate_paths]
            self.assertEqual(
                {record["evidence_id"]: record["seen_count"] for record in replayed},
                first_seen_counts,
            )

    def test_error_evidence_reconciles_failure_and_success_across_harvests(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(vault)
            transcript = os.path.join(tmp, "cross-harvest.jsonl")
            command = "cat missing-cross-harvest"
            write_codex_transcript(
                transcript,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "cross-harvest",
                            "agent": "codex",
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-13T01:00:00Z",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "first-failure",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": command}),
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "first-failure",
                            "output": json.dumps(
                                {
                                    "exit_code": 1,
                                    "output": "crossharvesttoken temporary failure",
                                }
                            ),
                        },
                    },
                ],
            )
            cfg = {
                "vault_path": vault,
                "projects": [{"name": "demo", "keywords": ["demo"]}],
                "personal_memory": {"enabled": False},
                "skill_preferences": {"enabled": False},
                "workflow_memory": {"enabled": False},
                "error_evidence": {"enabled": True},
            }

            self.assertTrue(process_transcript(cfg, transcript))
            self.assertEqual(
                len(glob.glob(os.path.join(vault, "04-Feedback/_error-candidates/*.md"))),
                1,
            )
            with open(transcript, "a", encoding="utf-8") as handle:
                for record in (
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "later-success",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": command}),
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "later-success",
                            "output": json.dumps(
                                {"exit_code": 0, "output": "crossharvesttoken recovered"}
                            ),
                        },
                    },
                ):
                    json.dump(record, handle)
                    handle.write("\n")

            failed_output = io.StringIO()
            with (
                redirect_stdout(failed_output),
                patch(
                    "session_harvester.rebuild_memory_index",
                    side_effect=RuntimeError("index rebuild interrupted"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "index rebuild interrupted"):
                    process_transcript(cfg, transcript)

            self.assertIn("[DISCARDED]", failed_output.getvalue())
            self.assertEqual(
                glob.glob(os.path.join(vault, "04-Feedback/_error-candidates/*.md")),
                [],
            )
            self.assertTrue(
                os.path.exists(
                    os.path.join(
                        vault,
                        "04-Feedback/_error-candidates/.index-dirty",
                    )
                )
            )

            retry_output = io.StringIO()
            with redirect_stdout(retry_output):
                changed = process_transcript(cfg, transcript)

            self.assertTrue(changed)
            self.assertIn("Repairing candidate index", retry_output.getvalue())
            memory_index = read_frontmatter(
                os.path.join(vault, "00-Inbox/Agent Memory Index.md")
            )
            self.assertEqual(memory_index["error_evidence_candidates"], 0)
            self.assertFalse(
                os.path.exists(
                    os.path.join(
                        vault,
                        "04-Feedback/_error-candidates/.index-dirty",
                    )
                )
            )

    def test_malformed_dirty_marker_forces_rebuild_before_cursor_advances(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            candidate_dir = os.path.join(
                vault,
                "04-Feedback",
                "_error-candidates",
            )
            os.makedirs(candidate_dir)
            marker = os.path.join(candidate_dir, ".index-dirty")
            with open(marker, "wb") as handle:
                handle.write(b"\xffmalformed-generation")
            transcript = os.path.join(tmp, "marker-repair.jsonl")
            write_codex_transcript(
                transcript,
                [
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "marker-repair",
                            "agent": "codex",
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-13T01:00:00Z",
                        },
                    }
                ],
            )
            cfg = {
                "vault_path": vault,
                "projects": [{"name": "demo", "keywords": ["demo"]}],
                "personal_memory": {"enabled": False},
                "skill_preferences": {"enabled": False},
                "workflow_memory": {"enabled": False},
                "error_evidence": {"enabled": True},
            }

            output = io.StringIO()
            with redirect_stdout(output):
                changed = process_transcript(cfg, transcript)

            self.assertTrue(changed)
            self.assertIn("Repairing candidate index", output.getvalue())
            self.assertFalse(os.path.exists(marker))
            memory_index = read_frontmatter(
                os.path.join(vault, "00-Inbox/Agent Memory Index.md")
            )
            self.assertEqual(memory_index["error_evidence_candidates"], 0)


def read_frontmatter(path):
    frontmatter_text, _body = split_frontmatter_text(read_text(path))
    if frontmatter_text is None:
        return {}
    return yaml.safe_load(frontmatter_text) or {}


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def write_text(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
        handle.write("\n")


def write_codex_transcript(path, records):
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            json.dump(record, handle, ensure_ascii=False)
            handle.write("\n")


def rolling_summary_marker(summary):
    return (
        "<!-- AGENT_MEMORY_BEACON:ROLLING_SUMMARY_V1\n"
        "project: demo\n"
        "current_goal: 完成滚动摘要采集\n"
        "topics:\n"
        "  - 会话摘要\n"
        "progress:\n"
        "  - 已验证采集路径\n"
        "constraints: []\n"
        "important_context: []\n"
        "open_items: []\n"
        f"summary: {summary}\n"
        "-->"
    )


def rolling_summary_json(summary):
    payload = extract_rolling_summary(rolling_summary_marker(summary))
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def summary_harvest_config(vault):
    return {
        "vault_path": vault,
        "projects": [{"name": "demo", "keywords": ["demo"]}],
        "project_keywords": {},
        "conversation_summary": {
            "enabled": True,
            "min_substantive_messages": 5,
            "message_interval": 10,
            "stale_after_minutes": 30,
            "retry_interval_messages": 2,
            "max_summary_bytes": 4096,
            "max_recall": 1,
            "token_budget": 400,
        },
        "personal_memory": {"enabled": False},
        "skill_preferences": {"enabled": False},
        "workflow_memory": {"enabled": False},
        "annotation_quality": {"enabled": False},
        "error_evidence": {"enabled": False},
    }


def write_minimal_zcode_db(db_path, session_id):
    conn = sqlite3.connect(db_path)
    conn.execute(
        "create table session (id text primary key, time_updated integer not null)"
    )
    conn.execute("insert into session(id, time_updated) values (?, ?)", (session_id, 1))
    conn.commit()
    conn.close()


def restore_env(name, value):
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
