import glob
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from beacon_sync_producer import collect_transcripts, garbage_collect_outbox
from beacon_sync_reducer import reduce_inboxes
from beacon_sync_snapshot import (
    materialize_generation,
    publish_generation,
    publish_pending_receipts,
)
from setup import create_vault_structure


class BeaconSyncEndToEndTests(unittest.TestCase):
    def test_remote_attachment_reaches_canonical_vault_replica_receipt_and_gc(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = root / "windows-sessions"
            attachments = root / "windows-attachments"
            sessions.mkdir()
            attachments.mkdir()
            image = attachments / "evidence.png"
            image_bytes = b"\x89PNG\r\n\x1a\nend-to-end-attachment"
            image.write_bytes(image_bytes)
            transcript = sessions / "attachment-session.jsonl"
            transcript.write_text(
                "\n".join(
                    json.dumps(record, ensure_ascii=False)
                    for record in (
                        {
                            "timestamp": "2026-07-31T02:00:00Z",
                            "type": "session_meta",
                            "payload": {
                                "id": "remote-attachment-e2e",
                                "cwd": str(attachments),
                                "timestamp": "2026-07-31T02:00:00Z",
                            },
                        },
                        {
                            "timestamp": "2026-07-31T02:01:00Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": "请保留这个项目附件",
                                "local_images": [str(image)],
                                "local_audio": [],
                                "images": [],
                                "text_elements": [],
                                "client_id": "e2e",
                            },
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            producer = {
                "device_id": "windows-attachment-e2e",
                "state_dir": str(root / "producer-state"),
                "outbox_dir": str(root / "outbox"),
                "received_published_dir": str(root / "received-published"),
                "replica_path": str(root / "replica"),
                "transcript_paths": [str(sessions)],
                "attachment_roots": [str(attachments)],
                "max_chunk_bytes": 1024 * 1024,
                "max_gap_bytes": 8 * 1024 * 1024,
                "max_attachment_bytes": 1024 * 1024,
                "max_events_per_run": 32,
                "max_event_json_bytes": 128 * 1024,
                "max_object_bytes": 32 * 1024 * 1024,
                "max_replica_object_bytes": 64 * 1024 * 1024,
                "gc_retention_seconds": 0,
            }
            now = datetime.now(timezone.utc)

            collected = collect_transcripts(
                producer,
                include_existing=True,
                now=now,
            )
            self.assertEqual(collected["emitted"], 2)
            self.assertEqual(collected["attachments_emitted"], 1)

            inbox = root / "authority-inbox"
            shutil.copytree(root / "outbox", inbox)
            vault = root / "vault"
            create_vault_structure(str(vault))
            cfg = harvester_config(vault)
            authority = {
                "state_dir": str(root / "authority-state"),
                "published_dir": str(root / "published"),
                "inboxes": [
                    {
                        "device_id": "windows-attachment-e2e",
                        "path": str(inbox),
                    }
                ],
                "max_events_per_run": 32,
                "max_event_json_bytes": 128 * 1024,
                "max_object_bytes": 32 * 1024 * 1024,
                "max_attachment_bytes": 1024 * 1024,
                "max_replica_object_bytes": 64 * 1024 * 1024,
            }

            reduced = reduce_inboxes(cfg, authority, now=now)
            self.assertEqual(reduced["applied"], 2)
            generation = publish_generation(cfg, authority, now=now)
            receipts = publish_pending_receipts(authority, generation, now=now)
            self.assertEqual(receipts["published"], 2)
            shutil.copytree(root / "published", root / "received-published")
            materialize_generation(producer, now=now, bootstrap=True)
            gc = garbage_collect_outbox(producer, now=now)
            self.assertEqual(gc["removed"], 2)

            canonical_blobs = list(
                (
                    vault
                    / "Attachments"
                    / "Agent-Memory-Beacon"
                    / "remote"
                    / "objects"
                ).rglob("*.png")
            )
            replica_blobs = list(
                (
                    root
                    / "replica"
                    / "Attachments"
                    / "Agent-Memory-Beacon"
                    / "remote"
                    / "objects"
                ).rglob("*.png")
            )
            replica_records = list(
                (
                    root
                    / "replica"
                    / "04-Feedback"
                    / "remote-attachments"
                ).rglob("*.md")
            )
            self.assertEqual(len(canonical_blobs), 1)
            self.assertEqual(len(replica_blobs), 1)
            self.assertEqual(len(replica_records), 1)
            self.assertEqual(canonical_blobs[0].read_bytes(), image_bytes)
            self.assertEqual(replica_blobs[0].read_bytes(), image_bytes)
            self.assertIn(
                "remote-attachment-e2e",
                replica_records[0].read_text(encoding="utf-8"),
            )
            self.assertEqual(
                stat.S_IMODE(replica_blobs[0].stat().st_mode) & stat.S_IWUSR,
                0,
            )

    def test_remote_codex_evidence_reaches_all_canonical_memory_and_replica(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sessions = root / "windows-sessions"
            sessions.mkdir()
            transcript = sessions / "remote-session.jsonl"
            evidence = "好的跨设备记忆应该传输原始证据，而不是传输未经授权的正式记忆变更"
            transcript.write_text(
                "\n".join(
                    json.dumps(record, ensure_ascii=False)
                    for record in (
                        {
                            "timestamp": "2026-07-31T01:00:00Z",
                            "type": "session_meta",
                            "payload": {
                                "id": "remote-sync-e2e",
                                "cwd": "/workspace/demo",
                                "timestamp": "2026-07-31T01:00:00Z",
                            },
                        },
                        {
                            "timestamp": "2026-07-31T01:01:00Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "user_message",
                                "message": evidence,
                            },
                        },
                        {
                            "timestamp": "2026-07-31T01:02:00Z",
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "assistant",
                                "content": (
                                    "[DECISION:采用 Windows 仅上传不可变 transcript evidence 的同步边界"
                                    "| context:Mac 保持唯一 canonical writer，避免生命周期权限跨设备扩散"
                                    "| project:demo| scope:project]\n"
                                    "[ERROR:type=path-filesystem"
                                    "| resolution:远程 transcript object 文件不存在导致归并停止；"
                                    "补齐并校验对象 SHA-256 后重新归并，端到端测试通过"
                                    "| project:demo]\n"
                                    "[FAVOR:跨设备记忆同步必须保持 Mac 单写"
                                    "| context:用户需要多端对话，但不接受多个设备直接改正式记忆"
                                    "| type:project_rule| project:demo]\n"
                                    "[LEARN:跨设备 Agent Memory 应同步来源证据而非同步写权限"
                                    "| novelty:把对话生产的 active-active 与 canonical 写入的 single-writer 分离"
                                    "| transfer:多设备 Agent,离线对话同步"
                                    "| boundary:不适用于必须由远端独立维护 canonical 状态的系统"
                                    f"| evidence:{evidence}"
                                    "| source:user| project:demo| scope:project]\n"
                                    + rolling_summary_marker()
                                ),
                            },
                        },
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            producer = {
                "device_id": "windows-e2e",
                "state_dir": str(root / "producer-state"),
                "outbox_dir": str(root / "outbox"),
                "received_published_dir": str(root / "received-published"),
                "replica_path": str(root / "replica"),
                "transcript_paths": [str(sessions)],
                "max_chunk_bytes": 1024 * 1024,
                "max_gap_bytes": 8 * 1024 * 1024,
                "max_events_per_run": 32,
                "max_event_json_bytes": 128 * 1024,
                "max_object_bytes": 32 * 1024 * 1024,
                "max_replica_object_bytes": 64 * 1024 * 1024,
                "gc_retention_seconds": 0,
            }
            now = datetime.now(timezone.utc)
            collected = collect_transcripts(
                producer,
                include_existing=True,
                now=now,
            )
            self.assertEqual(collected["emitted"], 1)

            inbox = root / "authority-inbox"
            shutil.copytree(root / "outbox", inbox)
            vault = root / "vault"
            create_vault_structure(str(vault))
            cfg = harvester_config(vault)
            authority = {
                "state_dir": str(root / "authority-state"),
                "published_dir": str(root / "published"),
                "inboxes": [{"device_id": "windows-e2e", "path": str(inbox)}],
                "max_events_per_run": 32,
                "max_event_json_bytes": 128 * 1024,
                "max_object_bytes": 32 * 1024 * 1024,
                "max_replica_object_bytes": 64 * 1024 * 1024,
            }

            reduced = reduce_inboxes(cfg, authority, now=now)
            self.assertEqual(reduced["applied"], 1)
            duplicate = reduce_inboxes(cfg, authority, now=now)
            self.assertEqual(duplicate["applied"], 0)
            self.assertEqual(duplicate["noop"], 0)

            generation = publish_generation(cfg, authority, now=now)
            receipts = publish_pending_receipts(authority, generation, now=now)
            self.assertEqual(receipts["published"], 1)
            shutil.copytree(root / "published", root / "received-published")
            materialized = materialize_generation(
                producer,
                now=now,
                bootstrap=True,
            )
            self.assertTrue(materialized["changed"])
            gc = garbage_collect_outbox(producer, now=now)
            self.assertEqual(gc["removed"], 1)

            decisions = (vault / "01-Projects/demo/Memory/decisions.md").read_text(
                encoding="utf-8"
            )
            pitfalls = (vault / "01-Projects/demo/Memory/pitfalls.md").read_text(
                encoding="utf-8"
            )
            personal = (vault / "05-Agent-Memory/personal-memory.md").read_text(
                encoding="utf-8"
            )
            insights = (vault / "05-Agent-Memory/insights.md").read_text(
                encoding="utf-8"
            )
            session_files = glob.glob(
                str(vault / "01-Projects/demo/Memory/sessions/*.md")
            )
            self.assertEqual(len(session_files), 1)
            session = Path(session_files[0]).read_text(encoding="utf-8")

            self.assertIn(
                "采用 Windows 仅上传不可变 transcript evidence 的同步边界",
                decisions,
            )
            self.assertIn("远程 transcript object 文件不存在", pitfalls)
            self.assertIn("跨设备记忆同步必须保持 Mac 单写", personal)
            self.assertIn("跨设备 Agent Memory 应同步来源证据", insights)
            self.assertIn("远程 Codex 对话已完成五类记忆闭环", session)
            self.assertIn("summary_mode: rolling", session)

            replica_decisions = (
                root / "replica/01-Projects/demo/Memory/decisions.md"
            )
            self.assertEqual(
                replica_decisions.read_text(encoding="utf-8"),
                decisions,
            )
            self.assertEqual(
                stat.S_IMODE(replica_decisions.stat().st_mode) & stat.S_IWUSR,
                0,
            )

            incremental_decision = (
                "副本增量物化采用严格父 generation 顺序"
            )
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "timestamp": "2026-07-31T01:03:00Z",
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "assistant",
                                "content": (
                                    f"[DECISION:{incremental_decision}"
                                    "| context:避免后台 run 跳过父代或接管未初始化副本"
                                    "| project:demo| scope:project]"
                                ),
                            },
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )

            incremental = collect_transcripts(producer, now=now)
            self.assertEqual(incremental["emitted"], 1)
            shutil.copytree(root / "outbox", inbox, dirs_exist_ok=True)
            reduced_incremental = reduce_inboxes(cfg, authority, now=now)
            self.assertEqual(reduced_incremental["applied"], 1)

            generation_2 = publish_generation(cfg, authority, now=now)
            self.assertEqual(generation_2["generation"], 2)
            receipts_2 = publish_pending_receipts(
                authority,
                generation_2,
                now=now,
            )
            self.assertEqual(receipts_2["published"], 1)
            shutil.copytree(
                root / "published",
                root / "received-published",
                dirs_exist_ok=True,
            )
            materialized_2 = materialize_generation(producer, now=now)
            self.assertEqual(materialized_2["generation"], 2)
            gc_2 = garbage_collect_outbox(producer, now=now)
            self.assertEqual(gc_2["removed"], 1)

            decisions_2 = (
                vault / "01-Projects/demo/Memory/decisions.md"
            ).read_text(encoding="utf-8")
            self.assertIn(incremental_decision, decisions_2)
            self.assertEqual(
                replica_decisions.read_text(encoding="utf-8"),
                decisions_2,
            )
            self.assertEqual(
                stat.S_IMODE(replica_decisions.stat().st_mode) & stat.S_IWUSR,
                0,
            )


def rolling_summary_marker():
    return (
        "<!-- AGENT_MEMORY_BEACON:ROLLING_SUMMARY_V1\n"
        "project: demo\n"
        "current_goal: 验证 Windows 到 Mac 的真实记忆归并\n"
        "topics:\n"
        "  - 跨设备证据同步\n"
        "progress:\n"
        "  - 已完成 Decision Error Favor Learn 与摘要采集\n"
        "constraints:\n"
        "  - Mac 是唯一 canonical writer\n"
        "important_context:\n"
        "  - Windows 只发送 transcript evidence\n"
        "open_items: []\n"
        "summary: 远程 Codex 对话已完成五类记忆闭环\n"
        "-->"
    )


def harvester_config(vault):
    return {
        "vault_path": str(vault),
        "projects": [{"name": "demo", "keywords": ["demo"]}],
        "project_keywords": {},
        "privacy": {
            "store_raw_transcripts": False,
            "store_transcript_metadata": True,
            "store_message_samples": False,
        },
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
        "insight_memory": {"enabled": True},
        "annotation_quality": {"enabled": False},
        "error_evidence": {"enabled": False},
        "memory_effectiveness": {"enabled": False},
        "memory_promotion": {"enabled": False},
        "graph_projection": {"enabled": False},
    }


if __name__ == "__main__":
    unittest.main()
