import os
import re
import sys
import tempfile
import unittest
from unittest.mock import patch

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from compiler import (
    compile_projects_section,
    compile_rules_section,
    configured_context_targets,
    load_active_project_records,
    load_promoted_personal_memory,
    load_promoted_skill_rules,
    load_promoted_workflow_rules,
    run,
)
from memory_schema import memory_revision
from insight_memory import render_formal_record
from memory_schema import normalize_formal_record


class CompilerTests(unittest.TestCase):
    def test_learn_protocol_is_managed_and_insight_bodies_are_dynamic_only(self):
        required_protocol = (
            "### [LEARN]",
            "novelty:",
            "transfer:",
            "boundary:",
            "evidence:",
            "source:user",
            "一次性",
            "assistant",
        )
        for relative in ("AGENTS.md", "patches/AGENT_MEMORY_BEACON.md.patch"):
            content = read_text(os.path.join(REPO_ROOT, relative))
            with self.subTest(relative=relative):
                for marker in required_protocol:
                    self.assertIn(marker, content)

        with tempfile.TemporaryDirectory() as vault:
            os.makedirs(os.path.join(vault, "01-Projects", "demo", "Memory", "sessions"))
            path = os.path.join(vault, "05-Agent-Memory", "insights.md")
            os.makedirs(os.path.dirname(path))
            record = normalize_formal_record(
                {
                    "id": "insight-compiler-demo",
                    "type": "insight",
                    "status": "active",
                    "maturity": "seed",
                    "confidence": 0.76,
                    "origin": "user",
                    "project": "demo",
                    "scope": "project",
                    "title": "不可静态编译的启发正文",
                    "summary": "secretinsightbody 只能通过动态召回注入",
                    "novelty": "减少每个任务的固定 token 消耗",
                    "transfer": ["动态记忆召回"],
                    "boundary": "不能作为事实或指令",
                    "source_refs": ["session:compiler-insight"],
                },
                memory_type="insight",
                default_project="demo",
                source_ref="",
            )
            write_text(
                path,
                "---\nschema_version: '2.0'\nsummary_type: insights\n---\n\n"
                + render_formal_record(record),
            )

            compiled = compile_projects_section(vault)

            self.assertIn("Formal insights available dynamically: 1", compiled)
            self.assertIn("| demo | 0 | 0 | 1 |", compiled)
            self.assertNotIn("secretinsightbody", compiled)

    def test_custom_workflow_formal_path_is_compiled(self):
        with tempfile.TemporaryDirectory() as vault:
            custom = os.path.join(vault, "06-Custom", "workflows.md")
            os.makedirs(os.path.dirname(custom))
            write_text(
                custom,
                "---\ntitle: Custom Workflows\nschema_version: '2.0'\n---\n\n"
                + adaptive_section(
                    "workflow",
                    "workflow-custom-compiler",
                    "workflow",
                    "custom_source_first: 自定义仓库先查源码",
                    "避免根据名称猜测",
                    trigger="用户要求分析自定义仓库",
                    behavior="先读取自定义源码再给结论",
                    avoid="用户要求只看本地摘要",
                ),
            )
            cfg = {
                "workflow_memory": {"formal_path": "06-Custom/workflows.md"}
            }

            compiled = compile_projects_section(vault, cfg=cfg)

            self.assertIn("custom_source_first", compiled)
            self.assertIn("先读取自定义源码再给结论", compiled)

    def test_compiler_excludes_symlinked_projects_outside_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            projects = os.path.join(vault, "01-Projects")
            external = os.path.join(tmp, "external-project")
            memory_dir = os.path.join(external, "Memory")
            sessions_dir = os.path.join(memory_dir, "sessions")
            os.makedirs(projects)
            os.makedirs(sessions_dir)
            record = aggregate_record(
                "external-decision",
                "decision",
                "EXTERNAL PROJECT SECRET",
                "must never compile through a symlink",
                project="linked",
                date="2099-01-01",
            )
            write_text(
                os.path.join(memory_dir, "decisions.md"),
                "---\n"
                + yaml.safe_dump(
                    {
                        "project": "linked",
                        "schema_version": "2.0",
                        "decisions": [record],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                )
                + "---\n",
            )
            write_text(
                os.path.join(memory_dir, "pitfalls.md"),
                "---\nproject: linked\nschema_version: '2.0'\npitfalls: []\n---\n",
            )
            write_text(
                os.path.join(sessions_dir, "2099-01-01-external.md"),
                "---\nsession_id: external\ndate: '2099-01-01'\n---\n",
            )
            os.symlink(external, os.path.join(projects, "linked"))

            compiled = compile_projects_section(vault)

            self.assertNotIn("linked", compiled)
            self.assertNotIn("EXTERNAL PROJECT SECRET", compiled)
            self.assertNotIn("2099-01-01", compiled)

    def test_compiler_excludes_symlinked_fixed_vault_input_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            external_rules = os.path.join(tmp, "external-rules")
            external_memory = os.path.join(tmp, "external-memory")
            os.makedirs(os.path.join(vault, "01-Projects"))
            os.makedirs(external_rules)
            os.makedirs(external_memory)
            write_text(
                os.path.join(external_rules, "external.md"),
                "---\n"
                "rule_id: external-rule\n"
                "title: EXTERNAL RULE SECRET\n"
                "category: security\n"
                "applies_to: [all]\n"
                "status: active\n"
                "---\n",
            )
            write_text(
                os.path.join(external_memory, "personal-memory.md"),
                "---\nschema_version: '2.0'\n---\n\n"
                + adaptive_section(
                    "personal",
                    "preference-external",
                    "preference",
                    "External preference",
                    "EXTERNAL ADAPTIVE SECRET",
                    scope="global",
                    project="",
                )
                + "\n",
            )
            os.symlink(external_rules, os.path.join(vault, "00-Rules"))
            os.symlink(external_memory, os.path.join(vault, "05-Agent-Memory"))

            rules = compile_rules_section(vault)
            projects = compile_projects_section(vault)

            self.assertNotIn("EXTERNAL RULE SECRET", rules)
            self.assertNotIn("EXTERNAL ADAPTIVE SECRET", projects)

    def test_compiler_excludes_symlinked_project_memory_with_allowed_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            safe_memory = os.path.join(vault, "01-Projects", "safe", "Memory")
            linked_project = os.path.join(vault, "01-Projects", "linked")
            external_memory = os.path.join(tmp, "external-memory")
            os.makedirs(safe_memory)
            os.makedirs(linked_project)
            os.makedirs(os.path.join(external_memory, "sessions"))
            safe = aggregate_record(
                "shared-decision-id",
                "decision",
                "Safe internal decision",
                "Authoritative internal content",
                project="safe",
            )
            external = aggregate_record(
                "shared-decision-id",
                "decision",
                "EXTERNAL DUPLICATE SECRET",
                "Must not compile through a Memory symlink",
                project="linked",
                date="2099-01-01",
            )
            write_text(
                os.path.join(safe_memory, "decisions.md"),
                "---\n"
                + yaml.safe_dump(
                    {
                        "project": "safe",
                        "schema_version": "2.0",
                        "decisions": [safe],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                )
                + "---\n",
            )
            write_text(
                os.path.join(safe_memory, "pitfalls.md"),
                "---\nproject: safe\nschema_version: '2.0'\npitfalls: []\n---\n",
            )
            write_text(
                os.path.join(external_memory, "decisions.md"),
                "---\n"
                + yaml.safe_dump(
                    {
                        "project": "linked",
                        "schema_version": "2.0",
                        "decisions": [external],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                )
                + "---\n",
            )
            write_text(
                os.path.join(external_memory, "pitfalls.md"),
                "---\nproject: linked\nschema_version: '2.0'\npitfalls: []\n---\n",
            )
            write_text(
                os.path.join(external_memory, "sessions", "2099-01-01-secret.md"),
                "---\nsession_id: external\ndate: '2099-01-01'\n---\n",
            )
            os.symlink(external_memory, os.path.join(linked_project, "Memory"))

            compiled = compile_projects_section(vault)

            self.assertIn("Safe internal decision", compiled)
            self.assertNotIn("EXTERNAL DUPLICATE SECRET", compiled)
            self.assertNotIn("2099-01-01", compiled)

    def test_compiler_excludes_symlinked_project_aggregate_leaf(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            memory_dir = os.path.join(vault, "01-Projects", "demo", "Memory")
            external = os.path.join(tmp, "external-decisions.md")
            os.makedirs(memory_dir)
            record = aggregate_record(
                "external-decision",
                "decision",
                "EXTERNAL AGGREGATE SECRET",
                "Must not compile through an aggregate-file symlink",
                project="demo",
                date="2099-01-01",
            )
            write_text(
                external,
                "---\n"
                + yaml.safe_dump(
                    {
                        "project": "demo",
                        "schema_version": "2.0",
                        "decisions": [record],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                )
                + "---\n",
            )
            write_text(
                os.path.join(memory_dir, "pitfalls.md"),
                "---\nproject: demo\nschema_version: '2.0'\npitfalls: []\n---\n",
            )
            os.symlink(external, os.path.join(memory_dir, "decisions.md"))

            compiled = compile_projects_section(vault)

            self.assertIn("| demo | 0 | 0 | 0 | - |", compiled)
            self.assertNotIn("EXTERNAL AGGREGATE SECRET", compiled)
            self.assertNotIn("2099-01-01", compiled)

    def test_compiler_excludes_symlinked_sessions_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            memory_dir = os.path.join(vault, "01-Projects", "demo", "Memory")
            external_sessions = os.path.join(tmp, "external-sessions")
            os.makedirs(memory_dir)
            os.makedirs(external_sessions)
            record = aggregate_record(
                "internal-decision",
                "decision",
                "Internal decision",
                "Authoritative content",
                project="demo",
            )
            write_text(
                os.path.join(memory_dir, "decisions.md"),
                "---\n"
                + yaml.safe_dump(
                    {
                        "project": "demo",
                        "schema_version": "2.0",
                        "decisions": [record],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                )
                + "---\n",
            )
            write_text(
                os.path.join(memory_dir, "pitfalls.md"),
                "---\nproject: demo\nschema_version: '2.0'\npitfalls: []\n---\n",
            )
            write_text(
                os.path.join(external_sessions, "2099-01-01-external.md"),
                "---\nsession_id: external\ndate: '2099-01-01'\n---\n",
            )
            os.symlink(external_sessions, os.path.join(memory_dir, "sessions"))

            compiled = compile_projects_section(vault)

            self.assertIn("| demo | 1 | 0 | 0 | - |", compiled)
            self.assertNotIn("2099-01-01", compiled)

    def test_active_rule_sync_writes_rule_body_and_index(self):
        with tempfile.TemporaryDirectory() as vault:
            rules_dir = os.path.join(vault, "00-Rules")
            os.makedirs(rules_dir)
            os.makedirs(os.path.join(vault, "01-Projects"))
            write_text(
                os.path.join(rules_dir, "compiler-rule.md"),
                "---\n"
                "rule_id: RULE_COMPILER\n"
                "title: Compiler rule\n"
                "category: workflow\n"
                "applies_to: [codex]\n"
                "status: active\n"
                "---\n\n"
                "PRESERVED RULE BODY\n",
            )

            result = run(
                {
                    "vault_path": vault,
                    "agent_memory_path": os.path.join(vault, "05-Agent-Memory"),
                    "context_targets": [],
                }
            )

            self.assertNotIn("memory_error", result)
            self.assertEqual(result["memory_rules_written"], 1)
            self.assertTrue(result["memory_index_updated"])
            exported = read_text(
                os.path.join(vault, "05-Agent-Memory", "rule-compiler.md")
            )
            self.assertIn("PRESERVED RULE BODY", exported)
            self.assertIn(
                "rule-compiler",
                read_text(os.path.join(vault, "05-Agent-Memory", "MEMORY.md")),
            )

    def test_agent_memory_sync_rejects_vault_symlink_output_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            rules_dir = os.path.join(vault, "00-Rules")
            external = os.path.join(tmp, "external")
            os.makedirs(rules_dir)
            os.makedirs(os.path.join(vault, "01-Projects"))
            os.makedirs(external)
            write_text(
                os.path.join(rules_dir, "compiler-rule.md"),
                "---\n"
                "rule_id: RULE_COMPILER\n"
                "title: Compiler rule\n"
                "category: workflow\n"
                "applies_to: [codex]\n"
                "status: active\n"
                "---\n\nbody\n",
            )
            os.symlink(external, os.path.join(vault, "05-Agent-Memory"))

            result = run(
                {
                    "vault_path": vault,
                    "agent_memory_path": os.path.join(vault, "05-Agent-Memory"),
                    "context_targets": [],
                }
            )

            self.assertRegex(
                result.get("memory_error", ""),
                r"(?i)(outside|symlink|pinned root)",
            )
            self.assertFalse(os.path.exists(os.path.join(external, "rule-compiler.md")))

    def test_agent_memory_sync_rejects_symlink_to_another_vault_directory(self):
        with tempfile.TemporaryDirectory() as vault:
            rules_dir = os.path.join(vault, "00-Rules")
            internal_target = os.path.join(vault, "derived-memory-target")
            os.makedirs(rules_dir)
            os.makedirs(os.path.join(vault, "01-Projects"))
            os.makedirs(internal_target)
            write_text(
                os.path.join(rules_dir, "compiler-rule.md"),
                "---\n"
                "rule_id: RULE_COMPILER\n"
                "title: Compiler rule\n"
                "category: workflow\n"
                "applies_to: [codex]\n"
                "status: active\n"
                "---\n\nbody\n",
            )
            os.symlink(internal_target, os.path.join(vault, "05-Agent-Memory"))

            result = run(
                {
                    "vault_path": vault,
                    "agent_memory_path": os.path.join(vault, "05-Agent-Memory"),
                    "context_targets": [],
                }
            )

            self.assertRegex(
                result.get("memory_error", ""),
                r"(?i)(outside|symlink|pinned root|not a directory)",
            )
            self.assertFalse(
                os.path.exists(os.path.join(internal_target, "rule-compiler.md"))
            )

    def test_external_agent_memory_requires_marker_except_for_migration_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            rules_dir = os.path.join(vault, "00-Rules")
            ordinary_output = os.path.realpath(os.path.join(tmp, "ordinary-output"))
            migration_output = os.path.realpath(os.path.join(tmp, "migration-output"))
            os.makedirs(rules_dir)
            os.makedirs(os.path.join(vault, "01-Projects"))
            os.makedirs(ordinary_output)
            os.makedirs(migration_output)
            write_text(
                os.path.join(rules_dir, "compiler-rule.md"),
                "---\n"
                "rule_id: RULE_COMPILER\n"
                "title: Compiler rule\n"
                "category: workflow\n"
                "applies_to: [codex]\n"
                "status: active\n"
                "---\n\nbody\n",
            )

            rejected = run(
                {
                    "vault_path": vault,
                    "agent_memory_path": ordinary_output,
                    "context_targets": [],
                }
            )

            self.assertRegex(rejected.get("memory_error", ""), "ownership marker")
            self.assertFalse(
                os.path.exists(os.path.join(ordinary_output, "rule-compiler.md"))
            )

            write_text(
                os.path.join(ordinary_output, ".agent-memory-beacon-root"),
                "owned\n",
            )
            accepted = run(
                {
                    "vault_path": vault,
                    "agent_memory_path": ordinary_output,
                    "context_targets": [],
                }
            )

            self.assertNotIn("memory_error", accepted)
            self.assertTrue(
                os.path.isfile(os.path.join(ordinary_output, "rule-compiler.md"))
            )

            class RecordingIO:
                def __init__(self):
                    self.writes = []

                def atomic_write(self, path, content, encoding="utf-8"):
                    self.writes.append(os.fspath(path))
                    write_text(path, content)

                def ensure_directory(self, path):
                    os.makedirs(path, exist_ok=True)

            mutation_io = RecordingIO()
            migrated = run(
                {
                    "vault_path": vault,
                    "agent_memory_path": migration_output,
                    "context_targets": [],
                },
                mutation_io=mutation_io,
            )

            self.assertNotIn("memory_error", migrated)
            self.assertTrue(
                os.path.isfile(os.path.join(migration_output, "rule-compiler.md"))
            )
            self.assertTrue(mutation_io.writes)
            self.assertTrue(
                all(
                    os.path.commonpath([migration_output, path]) == migration_output
                    for path in mutation_io.writes
                )
            )

    def test_context_compile_preserves_lifecycle_authority_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(os.path.join(vault, "00-Rules"))
            os.makedirs(os.path.join(vault, "01-Projects"))
            target = os.path.join(tmp, "AGENTS.md")
            source_patch = read_text(
                os.path.join(REPO_ROOT, "patches", "AGENT_MEMORY_BEACON.md.patch")
            )
            write_text(target, source_patch)

            run(
                {
                    "vault_path": vault,
                    "agent_memory_path": os.path.join(vault, "05-Agent-Memory"),
                    "context_targets": [target],
                    "skip_git_probe": True,
                },
                sync_agent_memory=False,
            )

            compiled = read_text(target)
            self.assertIn("Formal Memory Lifecycle Authority", compiled)
            self.assertIn("explicit user instruction", compiled)
            self.assertIn("inferred conflict", compiled)

    def test_run_can_refresh_context_without_syncing_agent_memory_files(self):
        with tempfile.TemporaryDirectory() as vault:
            with patch(
                "compiler.sync_to_agent_memory",
                side_effect=AssertionError("agent memory sync must stay disabled"),
            ):
                result = run(
                    {
                        "vault_path": vault,
                        "agent_memory_path": os.path.join(vault, "05-Agent-Memory"),
                        "context_targets": [],
                    },
                    sync_agent_memory=False,
                )

            self.assertEqual(result["memory_sync_skipped"], "disabled")

    def test_compiler_uses_active_formal_memory_not_stale_session_state(self):
        with tempfile.TemporaryDirectory() as vault:
            memory_dir = os.path.join(vault, "01-Projects/demo/Memory")
            sessions_dir = os.path.join(memory_dir, "sessions")
            os.makedirs(sessions_dir)
            ready = aggregate_record(
                "decision-ready",
                "decision",
                "Phase A 最终状态为 Ready",
                "最终验证全部通过",
                date="2026-07-12",
            )
            not_ready = aggregate_record(
                "decision-not-ready",
                "decision",
                "Phase A 当前状态为 Not Ready",
                "当时仍有阻断问题",
                status="superseded",
                superseded_by="decision-ready",
                date="2026-07-12",
            )
            write_text(
                os.path.join(memory_dir, "decisions.md"),
                "---\n"
                + yaml.safe_dump(
                    {
                        "project": "demo",
                        "schema_version": "2.0",
                        "decisions": [ready, not_ready],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                )
                + "---\n\n# Decisions\n",
            )
            write_text(
                os.path.join(memory_dir, "pitfalls.md"),
                "---\nproject: demo\nschema_version: '2.0'\npitfalls: []\n---\n",
            )
            write_text(
                os.path.join(sessions_dir, "2026-07-12-state.md"),
                """---
session_id: sess-state
date: '2026-07-12'
project: demo
summary_type: session
decisions_made:
- text: Phase A 当前状态为 Not Ready
  context: 当时仍有阻断问题
---
""",
            )

            compiled = compile_projects_section(vault)

            self.assertIn("| demo | 1 | 0 | 0 | 2026-07-12 |", compiled)
            self.assertIn("Phase A 最终状态为 Ready", compiled)
            self.assertNotIn("Phase A 当前状态为 Not Ready", compiled)

    def test_compiler_applies_quality_suppression_and_duplicate_folding(self):
        with tempfile.TemporaryDirectory() as vault:
            memory_dir = os.path.join(vault, "01-Projects/demo/Memory")
            os.makedirs(memory_dir)
            decisions = [
                aggregate_record(
                    "decision-durable",
                    "decision",
                    "采用候选优先的标签质量门",
                    "避免不确定内容直接污染正式召回",
                    project="demo",
                ),
                aggregate_record(
                    "decision-review-outcome",
                    "decision",
                    "logic-reviewer 结论为 NEEDS REVISION",
                    "发现两个仍需修复的验证门缺陷",
                    project="demo",
                ),
            ]
            pitfalls = [
                aggregate_record(
                    "error-pdf-a",
                    "error",
                    "shell-cli",
                    "系统缺少 pdftotext，改用 pypdf 完成 PDF 文本校验",
                    project="demo",
                ),
                aggregate_record(
                    "error-pdf-b",
                    "error",
                    "path-filesystem",
                    "pdftotext 不在当前 PATH，改用 PyMuPDF 提取 PDF 文本并完成核对",
                    project="demo",
                ),
                aggregate_record(
                    "error-unresolved",
                    "error",
                    "api-network",
                    "API Key 无效或无权限，尚未替换凭据",
                    project="demo",
                ),
            ]
            write_text(
                os.path.join(memory_dir, "decisions.md"),
                "---\n"
                + yaml.safe_dump(
                    {
                        "project": "demo",
                        "schema_version": "2.0",
                        "decisions": decisions,
                    },
                    allow_unicode=True,
                    sort_keys=False,
                )
                + "---\n\n# Decisions\n",
            )
            write_text(
                os.path.join(memory_dir, "pitfalls.md"),
                "---\n"
                + yaml.safe_dump(
                    {
                        "project": "demo",
                        "schema_version": "2.0",
                        "pitfalls": pitfalls,
                    },
                    allow_unicode=True,
                    sort_keys=False,
                )
                + "---\n\n# Pitfalls\n",
            )

            compiled = compile_projects_section(vault)

            self.assertIn("| demo | 1 | 1 | 0 | - |", compiled)
            self.assertIn("采用候选优先的标签质量门", compiled)
            self.assertNotIn("logic-reviewer 结论", compiled)
            self.assertNotIn("API Key 无效", compiled)
            self.assertEqual(compiled.count("pdftotext"), 1)

    def test_project_aggregates_require_schema_2_and_complete_active_records(self):
        with tempfile.TemporaryDirectory() as vault:
            memory_dir = os.path.join(vault, "01-Projects/demo/Memory")
            os.makedirs(memory_dir)
            write_text(
                os.path.join(memory_dir, "decisions.md"),
                "---\n"
                + yaml.safe_dump(
                    {
                        "project": "demo",
                        "schema_version": "1.0",
                        "decisions": [
                            aggregate_record(
                                "legacy-decision",
                                "decision",
                                "Legacy aggregate decision",
                                "A complete record in the wrong schema is still untrusted",
                            )
                        ],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                )
                + "---\n",
            )
            valid_error = aggregate_record(
                "valid-error",
                "error",
                "path-filesystem",
                "Canonical active error",
            )
            write_text(
                os.path.join(memory_dir, "pitfalls.md"),
                "---\n"
                + yaml.safe_dump(
                    {
                        "project": "demo",
                        "schema_version": "2.0",
                        "pitfalls": [
                            valid_error,
                            {
                                "revision": "missing-id-revision",
                                "type": "missing-id",
                                "resolution": "Missing id must be rejected",
                                "status": "active",
                                "project": "demo",
                                "scope": "project",
                                "source_refs": ["session:missing-id"],
                            },
                            {
                                "id": "missing-revision",
                                "type": "missing-revision",
                                "resolution": "Missing revision must be rejected",
                                "status": "active",
                                "project": "demo",
                                "scope": "project",
                                "source_refs": ["session:missing-revision"],
                            },
                            {
                                "id": "missing-status",
                                "revision": "missing-status-revision",
                                "type": "missing-status",
                                "resolution": "Missing status must be rejected",
                                "project": "demo",
                                "scope": "project",
                                "source_refs": ["session:missing-status"],
                            },
                            {
                                "id": "missing-source-refs",
                                "revision": "missing-source-refs-revision",
                                "type": "missing-source-refs",
                                "resolution": "Missing source refs must be rejected",
                                "status": "active",
                                "project": "demo",
                                "scope": "project",
                            },
                            {
                                "id": "empty-source-refs",
                                "revision": "empty-source-refs-revision",
                                "type": "empty-source-refs",
                                "resolution": "Empty source refs must be rejected",
                                "status": "active",
                                "project": "demo",
                                "scope": "project",
                                "source_refs": [],
                            },
                            {
                                "id": "legacy-status",
                                "revision": "legacy-status-revision",
                                "type": "legacy-status",
                                "resolution": "Legacy status must be rejected",
                                "status": "legacy",
                                "project": "demo",
                                "scope": "project",
                                "source_refs": ["session:legacy-status"],
                            },
                        ],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                )
                + "---\n",
            )

            compiled = compile_projects_section(vault)

            self.assertIn("| demo | 0 | 1 | 0 | - |", compiled)
            self.assertIn("Canonical active error", compiled)
            self.assertNotIn("Legacy aggregate decision", compiled)
            self.assertNotIn("Missing id must be rejected", compiled)
            self.assertNotIn("Missing revision must be rejected", compiled)
            self.assertNotIn("Missing status must be rejected", compiled)
            self.assertNotIn("Missing source refs must be rejected", compiled)
            self.assertNotIn("Empty source refs must be rejected", compiled)
            self.assertNotIn("Legacy status must be rejected", compiled)

    def test_project_records_require_bound_content_scope_project_and_revision(self):
        with tempfile.TemporaryDirectory() as vault:
            memory_dir = os.path.join(vault, "01-Projects/demo/Memory")
            os.makedirs(memory_dir)

            def decision(memory_id, text, context, **overrides):
                canonical = {
                    "type": "decision",
                    "status": "active",
                    "project": "demo",
                    "scope": "project",
                    "title": text,
                    "summary": context,
                }
                canonical.update(overrides)
                return {
                    "id": memory_id,
                    "revision": memory_revision(canonical),
                    "text": text,
                    "context": context,
                    "status": canonical["status"],
                    "project": canonical["project"],
                    "scope": canonical["scope"],
                    "source_refs": [f"session:{memory_id}"],
                }

            valid = decision("valid", "Use canonical schema", "Prevents stale recall")
            forged = decision("forged", "Forged revision", "Must be rejected")
            forged["revision"] = "0" * 64
            wrong_project = decision(
                "wrong-project",
                "Wrong project",
                "Must not cross project boundaries",
                project="other",
            )
            wrong_scope = decision(
                "wrong-scope",
                "Wrong scope",
                "Project memory cannot claim global scope",
                scope="global",
            )
            empty_body = decision("empty-body", "", "Missing decision text")
            frontmatter = {
                "project": "demo",
                "schema_version": "2.0",
                "decisions": [valid, forged, wrong_project, wrong_scope, empty_body],
            }
            write_text(
                os.path.join(memory_dir, "decisions.md"),
                "---\n"
                + yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False)
                + "---\n",
            )

            loaded = load_active_project_records(
                os.path.join(memory_dir, "decisions.md"),
                "decisions",
                "demo",
            )

            self.assertEqual([item["id"] for item in loaded], ["valid"])

    def test_adaptive_loaders_require_scope_sources_and_matching_revision(self):
        with tempfile.TemporaryDirectory() as vault:
            cases = [
                (
                    "personal-memory.md",
                    load_promoted_personal_memory,
                    "preference",
                    "Use Chinese: 默认使用中文",
                    "默认使用中文",
                    "personal",
                ),
                (
                    "skill-routing-rules.md",
                    load_promoted_skill_rules,
                    "skill",
                    "humanizer: Natural prose",
                    "Matches naturalization tasks",
                    "skill",
                ),
                (
                    "workflow-rules.md",
                    load_promoted_workflow_rules,
                    "workflow",
                    "github_source_first: Read source first",
                    "Avoids guessing from names",
                    "workflow",
                ),
            ]
            for filename, loader, memory_type, title, summary, kind in cases:
                with self.subTest(filename=filename):
                    sections = [
                        adaptive_section(
                            kind,
                            "valid",
                            memory_type,
                            title,
                            summary,
                        ),
                        adaptive_section(
                            kind,
                            "missing-sources",
                            memory_type,
                            title + " missing sources",
                            summary,
                            include_sources=False,
                        ),
                        adaptive_section(
                            kind,
                            "wrong-scope",
                            memory_type,
                            title + " wrong scope",
                            summary,
                            scope="global",
                            project="demo",
                        ),
                        adaptive_section(
                            kind,
                            "forged",
                            memory_type,
                            title + " forged",
                            summary,
                            forged_revision=True,
                        ),
                    ]
                    path = os.path.join(vault, filename)
                    write_text(
                        path,
                        "---\nschema_version: '2.0'\n---\n\n"
                        + "\n\n".join(sections)
                        + "\n",
                    )

                    loaded = loader(path)

                    self.assertEqual(len(loaded), 1)

    def test_run_routes_migration_context_write_through_mutation_io(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(os.path.join(vault, "00-Rules"))
            os.makedirs(os.path.join(vault, "01-Projects"))
            target = os.path.join(tmp, "AGENTS.md")
            write_text(
                target,
                managed_agents_document("preamble", "old rules", "old projects"),
            )

            class RecordingIO:
                def __init__(self):
                    self.writes = []

                def atomic_write(self, path, content, encoding="utf-8"):
                    self.writes.append((os.fspath(path), content, encoding))
                    write_text(path, content)

                def ensure_directory(self, path):
                    os.makedirs(path, exist_ok=True)

            mutation_io = RecordingIO()
            run(
                {
                    "vault_path": vault,
                    "agent_memory_path": os.path.join(vault, "05-Agent-Memory"),
                    "context_targets": [target],
                    "migration_paths_are_canonical": True,
                },
                mutation_io=mutation_io,
            )

            self.assertEqual([item[0] for item in mutation_io.writes], [target])

    def test_migration_context_targets_are_already_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            literal = os.path.join(tmp, "$LATE", "AGENTS.md")
            previous = os.environ.get("LATE")
            os.environ["LATE"] = "redirected"
            try:
                targets = configured_context_targets(
                    {
                        "context_targets": [literal],
                        "migration_paths_are_canonical": True,
                    }
                )
            finally:
                if previous is None:
                    os.environ.pop("LATE", None)
                else:
                    os.environ["LATE"] = previous

            self.assertEqual(targets, [literal])

    def test_run_ownership_check_stops_before_later_context_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(os.path.join(vault, "00-Rules"))
            os.makedirs(os.path.join(vault, "01-Projects"))
            first = os.path.join(tmp, "first", "AGENTS.md")
            second = os.path.join(tmp, "second", "AGENTS.md")
            for target in (first, second):
                os.makedirs(os.path.dirname(target))
                write_text(
                    target,
                    managed_agents_document(
                        "preamble",
                        "old rules",
                        "old projects",
                    ),
                )

            def ownership_check():
                if "old rules" not in read_text(first):
                    raise RuntimeError("ownership lost after first target")

            with self.assertRaisesRegex(RuntimeError, "ownership lost"):
                run(
                    {
                        "vault_path": vault,
                        "agent_memory_path": os.path.join(vault, "05-Agent-Memory"),
                        "context_targets": [first, second],
                    },
                    ownership_check=ownership_check,
                )

            self.assertNotIn("old rules", read_text(first))
            self.assertIn("old rules", read_text(second))

    def test_run_compiles_all_agent_context_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(os.path.join(vault, "00-Rules"))
            os.makedirs(os.path.join(vault, "01-Projects"))
            targets = [
                os.path.join(tmp, ".codex", "AGENTS.md"),
                os.path.join(tmp, ".claude", "CLAUDE.md"),
                os.path.join(tmp, ".zcode", "AGENTS.md"),
            ]
            for target in targets:
                os.makedirs(os.path.dirname(target), exist_ok=True)
                write_text(
                    target,
                    """prefix
<!-- COMPILED:RULES_START -->
old rules
<!-- COMPILED:RULES_END -->
<!-- COMPILED:PROJECTS_START -->
old projects
<!-- COMPILED:PROJECTS_END -->
suffix
""",
                )

            result = run(
                {
                    "vault_path": vault,
                    "agent_memory_path": os.path.join(vault, "05-Agent-Memory"),
                    "context_targets": targets,
                }
            )

            self.assertEqual(result["context_targets_updated"], 3)
            for target in targets:
                content = read_text(target)
                self.assertNotIn("old rules", content)
                self.assertNotIn("old projects", content)
                self.assertIn("prefix", content)
                self.assertIn("suffix", content)

    def test_run_refreshes_only_managed_blocks_in_shared_codex_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(os.path.join(vault, "00-Rules"))
            os.makedirs(os.path.join(vault, "01-Projects"))
            codex_home = os.path.join(tmp, ".codex")
            target = os.path.join(codex_home, "AGENTS.md")
            profile_dir = os.path.join(vault, "05-Agent-Memory", "codex-profile")
            shared = os.path.join(profile_dir, "AGENTS.shared.md")
            os.makedirs(codex_home)
            os.makedirs(profile_dir)
            write_text(
                target,
                managed_agents_document(
                    "target preamble",
                    "old target rules",
                    "old target projects",
                    outer_managed=True,
                ),
            )
            write_text(
                shared,
                legacy_agents_document(
                    "profile preamble",
                    "old profile rules",
                    "old profile projects",
                ),
            )

            result = run(
                {
                    "vault_path": vault,
                    "agent_memory_path": os.path.join(vault, "05-Agent-Memory"),
                    "context_targets": [target],
                    "codex_home": codex_home,
                    "codex_profile_path": profile_dir,
                }
            )

            self.assertTrue(result.get("codex_profile_agents_updated", False))
            target_text = read_text(target)
            shared_text = read_text(shared)
            self.assertIn("profile preamble", shared_text)
            self.assertIn("profile suffix", shared_text)
            self.assertNotIn("target preamble", shared_text)
            self.assertNotIn("old profile rules", shared_text)
            self.assertIn(
                "<!-- AGENT_MEMORY_BEACON:MANAGED_START version=3 -->",
                shared_text,
            )
            self.assertEqual(managed_agents_blocks(shared_text), managed_agents_blocks(target_text))

    def test_run_refreshes_shared_profile_with_only_a_claude_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(os.path.join(vault, "00-Rules"))
            os.makedirs(os.path.join(vault, "01-Projects"))
            claude_target = os.path.join(tmp, ".claude", "CLAUDE.md")
            profile_dir = os.path.join(vault, "05-Agent-Memory", "codex-profile")
            shared = os.path.join(profile_dir, "AGENTS.shared.md")
            os.makedirs(os.path.dirname(claude_target))
            os.makedirs(profile_dir)
            write_text(
                claude_target,
                managed_agents_document(
                    "claude preamble",
                    "old claude rules",
                    "old claude projects",
                    outer_managed=True,
                ),
            )
            write_text(
                shared,
                legacy_agents_document(
                    "profile preamble",
                    "stale profile rules",
                    "stale profile projects",
                ),
            )

            result = run(
                {
                    "vault_path": vault,
                    "agent_memory_path": os.path.join(vault, "05-Agent-Memory"),
                    "context_targets": [claude_target],
                    "codex_profile_path": profile_dir,
                }
            )

            self.assertTrue(result.get("codex_profile_agents_updated", False))
            shared_text = read_text(shared)
            self.assertIn("profile preamble", shared_text)
            self.assertNotIn("stale profile rules", shared_text)
            self.assertEqual(
                managed_agents_blocks(shared_text),
                managed_agents_blocks(read_text(claude_target)),
            )

    def test_promoted_personal_skill_and_workflow_memory_is_compiled(self):
        with tempfile.TemporaryDirectory() as vault:
            os.makedirs(os.path.join(vault, "01-Projects", "demo", "Memory", "sessions"))
            memory_dir = os.path.join(vault, "05-Agent-Memory")
            os.makedirs(memory_dir)
            write_text(
                os.path.join(memory_dir, "personal-memory.md"),
                "---\ntitle: Personal Memory\nschema_version: '2.0'\n---\n\n"
                + adaptive_section(
                    "personal",
                    "preference-demo",
                    "preference",
                    "用户偏好: 中文说明",
                    "默认使用中文解释",
                )
                + "\n",
            )
            write_text(
                os.path.join(memory_dir, "skill-routing-rules.md"),
                "---\ntitle: Skill Routing Rules\nschema_version: '2.0'\n---\n\n"
                + adaptive_section(
                    "skill",
                    "skill-demo",
                    "skill",
                    "humanizer: 中文表达自然化",
                    "用于中文去 AI 味。",
                    when="用户要求说人话",
                    avoid="用户要求逐字引用",
                )
                + "\n",
            )
            write_text(
                os.path.join(memory_dir, "workflow-rules.md"),
                "---\ntitle: Workflow Rules\nschema_version: '2.0'\n---\n\n"
                + adaptive_section(
                    "workflow",
                    "workflow-demo",
                    "workflow",
                    "github_source_first: GitHub 项目先查源码",
                    "避免根据名称猜测",
                    trigger="用户要求分析 GitHub 项目。",
                    behavior="先阅读 README 和关键源码，再给结论。",
                    avoid="用户明确要求离线分析",
                )
                + "\n",
            )

            compiled = compile_projects_section(vault)

            self.assertIn("默认使用中文解释", compiled)
            self.assertIn("humanizer", compiled)
            self.assertIn("用户要求说人话", compiled)
            self.assertIn("github_source_first", compiled)
            self.assertIn("先阅读 README 和关键源码", compiled)

    def test_formal_note_loaders_require_schema_2_and_active_section_metadata(self):
        with tempfile.TemporaryDirectory() as vault:
            cases = [
                (
                    "personal-memory.md",
                    load_promoted_personal_memory,
                    "personal",
                    "preference",
                    "Active personal memory",
                    "active-personal-content",
                    "active-personal-content",
                ),
                (
                    "skill-routing-rules.md",
                    load_promoted_skill_rules,
                    "skill",
                    "skill",
                    "active_skill: Active skill",
                    "active-skill-fit",
                    "active_skill",
                ),
                (
                    "workflow-rules.md",
                    load_promoted_workflow_rules,
                    "workflow",
                    "workflow",
                    "active_workflow: Active workflow",
                    "active-workflow-reason",
                    "active_workflow",
                ),
            ]

            for (
                filename,
                loader,
                kind,
                memory_type,
                title,
                summary,
                active_marker,
            ) in cases:
                with self.subTest(filename=filename):
                    path = os.path.join(vault, filename)
                    active = adaptive_section(
                        kind,
                        f"{kind}-active",
                        memory_type,
                        title,
                        summary,
                        when="active-skill-trigger",
                        trigger="active-workflow-trigger",
                        behavior="active-workflow-behavior",
                    )
                    inactive = adaptive_section(
                        kind,
                        f"{kind}-inactive",
                        memory_type,
                        title.replace("active", "inactive"),
                        summary.replace("active", "inactive"),
                    ).replace("- status: `active`", "- status: `retracted`", 1)
                    legacy = adaptive_section(
                        kind,
                        f"{kind}-legacy",
                        memory_type,
                        title.replace("active", "legacy"),
                        summary.replace("active", "legacy"),
                    )
                    legacy = re.sub(r"(?m)^- revision:.*\n", "", legacy, count=1)
                    body = "\n\n".join([active, inactive, legacy])
                    write_text(
                        path,
                        "---\nschema_version: '2.0'\n---\n\n" + body,
                    )
                    loaded = loader(path)
                    self.assertEqual(len(loaded), 1)
                    self.assertIn(active_marker, str(loaded[0]))

                    write_text(
                        path,
                        "---\nschema_version: '1.0'\n---\n\n" + body,
                    )
                    self.assertEqual(loader(path), [])

    def test_compiler_excludes_active_records_with_inactive_required_memory(self):
        with tempfile.TemporaryDirectory() as vault:
            memory_dir = os.path.join(vault, "01-Projects/demo/Memory")
            os.makedirs(memory_dir)
            withdrawn = aggregate_record(
                "decision-withdrawn",
                "decision",
                "旧安装路径可继续使用",
                "该判断已被用户撤回",
                status="retracted",
            )
            dependent = aggregate_record(
                "decision-dependent",
                "decision",
                "继续依赖开发仓库",
                "只有旧安装路径有效时才成立",
                requires=["decision-withdrawn"],
            )
            independent = aggregate_record(
                "decision-independent",
                "decision",
                "使用稳定运行目录",
                "该决定有独立证据",
            )
            write_text(
                os.path.join(memory_dir, "decisions.md"),
                "---\n"
                + yaml.safe_dump(
                    {
                        "project": "demo",
                        "schema_version": "2.0",
                        "decisions": [withdrawn, dependent, independent],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                )
                + "---\n\n# Decisions\n",
            )
            write_text(
                os.path.join(memory_dir, "pitfalls.md"),
                "---\nproject: demo\nschema_version: '2.0'\npitfalls: []\n---\n",
            )

            compiled = compile_projects_section(vault)

            self.assertIn("使用稳定运行目录", compiled)
            self.assertNotIn("继续依赖开发仓库", compiled)
            self.assertNotIn("旧安装路径可继续使用", compiled)

def aggregate_record(
    memory_id,
    memory_type,
    title,
    summary,
    *,
    status="active",
    project="demo",
    scope="project",
    date="",
    superseded_by="",
    requires=None,
):
    canonical = {
        "type": memory_type,
        "status": status,
        "project": project,
        "scope": scope,
        "title": title,
        "summary": summary,
        "superseded_by": superseded_by,
    }
    if requires:
        canonical["requires"] = list(requires)
    record = {
        "id": memory_id,
        "revision": memory_revision(canonical),
        "status": status,
        "project": project,
        "scope": scope,
        "date": date,
        "source_refs": [f"session:{memory_id}"],
    }
    if memory_type == "decision":
        record.update({"text": title, "context": summary})
    else:
        record.update({"type": title, "resolution": summary})
    if superseded_by:
        record["superseded_by"] = superseded_by
    if requires:
        record["requires"] = list(requires)
    return record


def adaptive_section(
    kind,
    memory_id,
    memory_type,
    title,
    summary,
    *,
    scope="project",
    project="demo",
    include_sources=True,
    forged_revision=False,
    when="",
    trigger="",
    behavior="",
    avoid="no matching task",
):
    canonical = {
        "type": memory_type,
        "status": "active",
        "project": project,
        "scope": scope,
        "title": title,
        "summary": summary,
    }
    revision = "0" * 64 if forged_revision else memory_revision(canonical)
    project_value = (
        f"[[01-Projects/{project}/Memory/decisions|{project}]]"
        if project
        else "`global`"
    )
    lines = [
        f"## {title}",
        "",
        f"- id: `{memory_id}`",
        f"- revision: `{revision}`",
        "- status: `active`",
        f"- scope: `{scope}`",
        f"- project: {project_value}",
    ]
    if include_sources:
        lines.append(f"- source_refs: `session:{memory_id}`")
    if kind == "personal":
        lines.extend(
            [
                f"- type: `{memory_type}`",
                f"- memory: {summary}",
            ]
        )
    elif kind == "skill":
        skill_name = title.split(":", 1)[0]
        lines.extend(
            [
                f"- skill_name: `{skill_name}`",
                "",
                "### When to consider",
                "",
                f"- {when or title}",
                "",
                "### Why this skill fits",
                "",
                summary,
                "",
                "### Do not use when",
                "",
                f"- {avoid}",
            ]
        )
    else:
        rule_name = title.split(":", 1)[0]
        lines.extend(
            [
                f"- rule_name: `{rule_name}`",
                "",
                "### Trigger scene",
                "",
                trigger or title,
                "",
                "### Desired behavior",
                "",
                behavior or title.split(":", 1)[-1].strip(),
                "",
                "### Why this matters",
                "",
                summary,
                "",
                "### Do not apply when",
                "",
                f"- {avoid}",
            ]
        )
    return "\n".join(lines)


def write_text(path, content):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def managed_agents_document(preamble, rules, projects, outer_managed=False):
    start = (
        "<!-- AGENT_MEMORY_BEACON:MANAGED_START version=3 -->\n"
        if outer_managed
        else ""
    )
    end = "\n<!-- AGENT_MEMORY_BEACON:MANAGED_END -->" if outer_managed else ""
    return f"""{preamble}
{start}## Agent Memory Vault - Auto-Maintained Blocks
<!-- COMPILED:RULES_START -->
{rules}
<!-- COMPILED:RULES_END -->
<!-- COMPILED:PROJECTS_START -->
{projects}
<!-- COMPILED:PROJECTS_END -->
| Pitfalls log | `01-Projects/{{project}}/Memory/pitfalls.md` |
{end}
"""


def legacy_agents_document(preamble, rules, projects):
    return f"""{preamble}
## Obsidian Knowledge Brain - Auto-Maintained Blocks
<!-- COMPILED:RULES_START -->
{rules}
<!-- COMPILED:RULES_END -->
<!-- COMPILED:PROJECTS_START -->
{projects}
<!-- COMPILED:PROJECTS_END -->
| Pitfalls log | `01-Projects/{{project}}/Memory/pitfalls.md` |
profile suffix
"""


def managed_agents_blocks(content):
    blocks = []
    for start_marker, end_marker in (
        ("<!-- COMPILED:RULES_START -->", "<!-- COMPILED:RULES_END -->"),
        ("<!-- COMPILED:PROJECTS_START -->", "<!-- COMPILED:PROJECTS_END -->"),
    ):
        start = content.index(start_marker)
        end = content.index(end_marker, start) + len(end_marker)
        blocks.append(content[start:end])
    return tuple(blocks)


if __name__ == "__main__":
    unittest.main()
