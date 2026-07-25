import glob
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from brand_migration import (
    apply_brand_migration,
    build_migration_plan,
    rollback_brand_migration,
)
from link_validator import run as validate_links
from session_harvester import initialize_harvest_baseline, process_transcript
from setup import create_vault_structure
from transcript_utils import parse_transcript_since


class AgentEndToEndTests(unittest.TestCase):
    def test_incremental_codex_parse_exposes_bounded_user_evidence_without_replay(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            transcript = Path(raw_tmp) / "context-window.jsonl"
            first = {
                "type": "event_msg",
                "payload": {
                    "type": "user_message",
                    "message": "好的启发可能只是一瞬间，不一定会重复。",
                },
            }
            write_jsonl(transcript, [first])
            cursor = f"file-bytes:{transcript.stat().st_size}"
            with transcript.open("a", encoding="utf-8") as handle:
                json.dump(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": "[LEARN:启发价值与重复次数应分开判断]",
                        },
                    },
                    handle,
                    ensure_ascii=False,
                )
                handle.write("\n")

            parsed = parse_transcript_since(
                str(transcript),
                cursor,
                end_cursor=f"file-bytes:{transcript.stat().st_size}",
            )

            self.assertEqual(len(parsed["messages"]), 1)
            self.assertEqual(parsed["messages"][0]["role"], "assistant")
            self.assertNotIn("好的启发可能只是一瞬间", parsed["text"])
            self.assertEqual(
                parsed["context_messages"],
                [
                    {
                        "role": "user",
                        "text": "好的启发可能只是一瞬间，不一定会重复。",
                    }
                ],
            )

    def test_post_migration_stale_annotation_harvests_only_to_canonical_project(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            create_vault_structure(str(vault))
            old = "github-obsidian-knowledge-brain"
            new = "agent-memory-beacon"
            memory = vault / "01-Projects" / old / "Memory"
            write_text(
                memory / "decisions.md",
                f"---\nproject: {old}\ndecisions: []\n---\n\n# Decisions\n",
            )
            write_text(
                memory / "pitfalls.md",
                f"---\nproject: {old}\npitfalls: []\n---\n\n# Pitfalls\n",
            )
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "projects": [old],
                        "project_keywords": {old: ["legacy-checkout"]},
                        "personal_memory": {"enabled": False},
                        "skill_preferences": {"enabled": False},
                        "workflow_memory": {"enabled": False},
                    },
                    sort_keys=False,
                ),
            )
            plan = build_migration_plan(vault, config_path=config)
            result = apply_brand_migration(
                plan,
                "stale-annotation-routing",
                rebuilders=[],
            )
            self.assertTrue(result["valid"])

            transcript = tmp / "post-migration.jsonl"
            write_jsonl(
                transcript,
                [
                    {
                        "timestamp": "2026-07-12T01:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "post-migration-stale-annotation",
                            "cwd": f"/tmp/{old}",
                            "timestamp": "2026-07-12T01:00:00Z",
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": (
                                "[DECISION:将旧项目标签统一路由到 agent-memory-beacon| "
                                "context:迁移后的别名必须保持兼容且不能重建旧项目| "
                                f"project:{old}]"
                            ),
                        },
                    },
                ],
            )
            migrated_cfg = yaml.safe_load(config.read_text(encoding="utf-8"))

            self.assertTrue(process_transcript(migrated_cfg, str(transcript)))

            self.assertFalse((vault / "01-Projects" / old).exists())
            sessions = list(
                (vault / "01-Projects" / new / "Memory" / "sessions").glob("*.md")
            )
            harvested = [
                path
                for path in sessions
                if read_frontmatter(path).get("session_id")
                == "post-migration-stale-annotation"
            ]
            self.assertEqual(len(harvested), 1)
            self.assertEqual(read_frontmatter(harvested[0])["project"], new)

    def test_brand_migration_apply_validate_and_rollback_in_synthetic_vault(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = Path(raw_tmp).resolve()
            vault = tmp / "vault"
            create_vault_structure(str(vault))
            old = "github-obsidian-knowledge-brain"
            new = "agent-memory-beacon"
            memory = vault / "01-Projects" / old / "Memory"
            write_text(
                memory / "decisions.md",
                f"---\nproject: {old}\ndecisions:\n"
                "- id: migration-e2e\n  text: Preserve identity\n"
                "---\n\n# Decisions\n",
            )
            write_text(
                memory / "pitfalls.md",
                f"---\nproject: {old}\npitfalls: []\n---\n\n# Pitfalls\n",
            )
            write_text(
                memory / "sessions" / "2026-07-12-migration.md",
                f"---\nsession_id: migration-e2e\ndate: 2026-07-12\n"
                f"project: {old}\ndecisions_made: []\nerrors_encountered: []\n"
                "---\n\n# Migration\n",
            )
            write_text(
                vault / "03-Maps" / "project-graph.md",
                f"[[01-Projects/{old}/Memory/decisions|Legacy decisions]]\n",
            )
            write_text(
                vault / "00-Inbox" / "Agent Memory Index.md",
                "# Agent Memory Index\n",
            )
            config = tmp / "config.yaml"
            write_text(
                config,
                yaml.safe_dump(
                    {
                        "vault_path": str(vault),
                        "projects": [old],
                        "project_keywords": {old: ["knowledge-brain"]},
                    },
                    sort_keys=False,
                ),
            )
            plan = build_migration_plan(vault, config_path=config)

            result = apply_brand_migration(
                plan,
                "agent-e2e-migration",
                rebuilders=[],
            )

            self.assertEqual(result["status"], "applied")
            self.assertFalse((vault / "01-Projects" / old).exists())
            self.assertTrue((vault / "01-Projects" / new).is_dir())
            self.assertEqual(validate_links(vault), [])
            migrated = yaml.safe_load(config.read_text(encoding="utf-8"))
            self.assertEqual(migrated["projects"], [new])
            self.assertIn(new, migrated["project_keywords"])

            rollback_brand_migration(result["manifest_path"])

            self.assertTrue((vault / "01-Projects" / old).is_dir())
            self.assertFalse((vault / "01-Projects" / new).exists())
            restored = yaml.safe_load(config.read_text(encoding="utf-8"))
            self.assertEqual(restored["projects"], [old])

    def test_codex_claude_and_zcode_harvest_into_one_valid_vault(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            create_vault_structure(vault)
            cfg = {
                "vault_path": vault,
                "projects": ["demo"],
                "project_keywords": {},
                "personal_memory": {"enabled": True},
                "skill_preferences": {"enabled": True},
                "workflow_memory": {"enabled": True},
            }

            codex = os.path.join(tmp, "codex.jsonl")
            write_jsonl(
                codex,
                [
                    {
                        "timestamp": "2026-07-11T01:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": "codex-e2e",
                            "cwd": "/tmp/demo",
                            "timestamp": "2026-07-11T01:00:00Z",
                        },
                    },
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": (
                                "<INSTRUCTIONS>Always use $old-hand.</INSTRUCTIONS>"
                                "验证 Codex 采集。"
                            ),
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": (
                                "[DECISION:采用 Codex JSONL 解析器作为采集入口| "
                                "context:需要从结构化消息稳定提取正式记忆]\n"
                                "[ERROR:type=data-format| resolution:Codex JSONL 曾因消息字段格式解析失败，"
                                "修复字段解析后重跑端到端测试通过]"
                            ),
                        },
                    },
                ],
            )

            claude = os.path.join(tmp, "claude.jsonl")
            write_jsonl(
                claude,
                [
                    {
                        "timestamp": "2026-07-11T01:01:00Z",
                        "type": "user",
                        "sessionId": "claude-e2e",
                        "cwd": "/tmp/demo",
                        "message": {"role": "user", "content": "验证 Claude 采集。"},
                    },
                    {
                        "timestamp": "2026-07-11T01:02:00Z",
                        "type": "assistant",
                        "sessionId": "claude-e2e",
                        "cwd": "/tmp/demo",
                        "message": {
                            "role": "assistant",
                            "content": (
                                "[DECISION:采用 Claude JSONL 解析器作为采集入口| "
                                "context:需要兼容 Claude 的消息结构并归入同一 Vault]\n"
                                "[ERROR:type=shell-cli| resolution:Claude JSONL 采集命令曾因字段路径错误失败，"
                                "修复读取路径后重跑端到端测试通过]"
                            ),
                        },
                    },
                ],
            )

            zcode_db = os.path.join(tmp, "zcode.sqlite")
            write_zcode_fixture(zcode_db)

            self.assertTrue(process_transcript(cfg, codex))
            self.assertTrue(process_transcript(cfg, claude))
            self.assertTrue(process_transcript(cfg, zcode_db + "::zcode-e2e"))

            sessions = glob.glob(
                os.path.join(vault, "01-Projects/demo/Memory/sessions/*.md")
            )
            self.assertEqual(len(sessions), 3)
            self.assertFalse(any("codex-e2e" in os.path.basename(path) for path in sessions))
            self.assertFalse(any("claude-e2e" in os.path.basename(path) for path in sessions))
            self.assertFalse(any("zcode-e2e" in os.path.basename(path) for path in sessions))

            decisions = read_frontmatter(
                os.path.join(vault, "01-Projects/demo/Memory/decisions.md")
            )
            pitfalls = read_frontmatter(
                os.path.join(vault, "01-Projects/demo/Memory/pitfalls.md")
            )
            self.assertEqual(len(decisions["decisions"]), 3)
            self.assertEqual(len(pitfalls["pitfalls"]), 3)
            self.assertEqual(validate_links(vault), [])
            self.assertEqual(
                glob.glob(os.path.join(vault, "04-Feedback/_skill-preferences/*.md")),
                [],
            )

    def test_zcode_append_does_not_replay_pre_baseline_adaptive_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            create_vault_structure(vault)
            zcode_db = os.path.join(tmp, "zcode.sqlite")
            write_zcode_adaptive_fixture(zcode_db)
            cfg = {
                "vault_path": vault,
                "transcript_agents": ["zcode"],
                "zcode_db_path": zcode_db,
                "zcode_home": os.path.join(tmp, "missing-zcode-home"),
                "projects": ["demo"],
                "project_keywords": {},
                "personal_memory": {"enabled": True},
                "skill_preferences": {"enabled": True},
                "workflow_memory": {"enabled": True},
            }

            self.assertEqual(initialize_harvest_baseline(cfg), 1)
            append_zcode_message(
                zcode_db,
                "zcode-adaptive",
                "z-new-user",
                "user",
                "继续检查当前实现。",
                1_783_700_000_010,
            )
            append_zcode_message(
                zcode_db,
                "zcode-adaptive",
                "z-new-assistant",
                "assistant",
                "[DECISION:采用消息计数游标增量收割 ZCode 追加内容| "
                "context:避免旧用户消息被重复学习]",
                1_783_700_000_011,
            )

            self.assertTrue(
                process_transcript(cfg, zcode_db + "::zcode-adaptive")
            )
            self.assertEqual(
                glob.glob(os.path.join(vault, "04-Feedback/_memory-candidates/*.md")),
                [],
            )
            self.assertEqual(
                glob.glob(os.path.join(vault, "04-Feedback/_skill-preferences/*.md")),
                [],
            )
            self.assertEqual(
                glob.glob(os.path.join(vault, "04-Feedback/_workflow-candidates/*.md")),
                [],
            )


def write_jsonl(path, records):
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            json.dump(record, handle, ensure_ascii=False)
            handle.write("\n")


def write_text(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def write_zcode_fixture(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table session (
            id text primary key,
            directory text not null,
            title text not null,
            time_created integer not null,
            time_updated integer not null
        );
        create table message (
            id text primary key,
            session_id text not null,
            time_created integer not null,
            time_updated integer not null,
            data text not null
        );
        create table part (
            id text primary key,
            message_id text not null,
            session_id text not null,
            time_created integer not null,
            time_updated integer not null,
            data text not null
        );
        """
    )
    timestamp = 1_783_700_000_000
    conn.execute(
        "insert into session values (?, ?, ?, ?, ?)",
        ("zcode-e2e", "/tmp/demo", "ZCode E2E", timestamp, timestamp),
    )
    messages = [
        ("z-user", "user", "验证 ZCode 采集。"),
        (
            "z-assistant",
            "assistant",
            "[DECISION:采用 SQLite message/part 作为 ZCode 采集入口| "
            "context:完整正文位于数据库而不是运行日志]\n"
            "[ERROR:type=path-filesystem| resolution:ZCode 采集曾因数据库路径不存在失败，"
            "修复数据库定位后重跑端到端测试通过]",
        ),
    ]
    for index, (message_id, role, text) in enumerate(messages):
        created = timestamp + index
        conn.execute(
            "insert into message values (?, ?, ?, ?, ?)",
            (
                message_id,
                "zcode-e2e",
                created,
                created,
                json.dumps({"role": role}),
            ),
        )
        conn.execute(
            "insert into part values (?, ?, ?, ?, ?, ?)",
            (
                message_id + "-part",
                message_id,
                "zcode-e2e",
                created,
                created,
                json.dumps({"type": "text", "text": text}, ensure_ascii=False),
            ),
        )
    conn.commit()
    conn.close()


def write_zcode_adaptive_fixture(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        create table session (
            id text primary key,
            directory text not null,
            title text not null,
            time_created integer not null,
            time_updated integer not null
        );
        create table message (
            id text primary key,
            session_id text not null,
            time_created integer not null,
            time_updated integer not null,
            data text not null
        );
        create table part (
            id text primary key,
            message_id text not null,
            session_id text not null,
            time_created integer not null,
            time_updated integer not null,
            data text not null
        );
        """
    )
    timestamp = 1_783_700_000_000
    conn.execute(
        "insert into session values (?, ?, ?, ?, ?)",
        ("zcode-adaptive", "/tmp/demo", "ZCode Adaptive", timestamp, timestamp),
    )
    conn.commit()
    conn.close()
    append_zcode_message(
        path,
        "zcode-adaptive",
        "z-old-user",
        "user",
        (
            "我希望以后默认用 $humanizer；分析 GitHub skill "
            "要先查源码和 README，不要只看名字。"
        ),
        timestamp,
    )


def append_zcode_message(path, session_id, message_id, role, text, timestamp):
    conn = sqlite3.connect(path)
    conn.execute(
        "insert into message values (?, ?, ?, ?, ?)",
        (message_id, session_id, timestamp, timestamp, json.dumps({"role": role})),
    )
    conn.execute(
        "insert into part values (?, ?, ?, ?, ?, ?)",
        (
            message_id + "-part",
            message_id,
            session_id,
            timestamp,
            timestamp,
            json.dumps({"type": "text", "text": text}, ensure_ascii=False),
        ),
    )
    conn.execute(
        "update session set time_updated = ? where id = ?",
        (timestamp, session_id),
    )
    conn.commit()
    conn.close()


def read_frontmatter(path):
    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle.read().split("---", 2)[1])


if __name__ == "__main__":
    unittest.main()
