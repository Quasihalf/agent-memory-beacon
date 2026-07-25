import glob
import hashlib
import copy
import os
import sys
import tempfile
import unittest
from unittest import mock

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

import error_evidence

from error_evidence import (
    ErrorEvidenceStateError,
    error_evidence_dirty_token,
    evidence_id_for,
    process_error_evidence,
)


def tool_result(index, operation_hash, success, excerpt, is_test=False):
    return {
        "event_index": index,
        "kind": "tool_result",
        "operation": "exec_command",
        "operation_hash": hash_label(operation_hash),
        "is_test": is_test,
        "success": success,
        "excerpt": excerpt,
    }


def review_finding(index, severity, excerpt):
    return {
        "event_index": index,
        "kind": "review_finding",
        "operation": "subagent_review",
        "operation_hash": hash_label("review-" + severity),
        "is_test": False,
        "success": False,
        "severity": severity,
        "excerpt": excerpt,
    }


def cfg_for(vault, **settings):
    return {
        "vault_path": vault,
        "error_evidence": {
            "enabled": True,
            "candidate_dir": "04-Feedback/_error-candidates",
            "excerpt_limit": 500,
            "source_limit": 20,
            **settings,
        },
    }


def candidate_paths(vault):
    return glob.glob(os.path.join(vault, "04-Feedback", "_error-candidates", "*.md"))


def hash_label(value):
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def read_candidate(path):
    with open(path, "r", encoding="utf-8") as handle:
        content = handle.read()
    return yaml.safe_load(content.split("---", 2)[1]), content


class ErrorEvidenceTests(unittest.TestCase):
    def test_expected_test_red_and_retry_success_are_not_persisted(self):
        with tempfile.TemporaryDirectory() as vault:
            parsed = {
                "observations": [
                    tool_result(1, "test-op", False, "pytest failed", is_test=True),
                    tool_result(2, "test-op", True, "pytest passed", is_test=True),
                    tool_result(3, "retry-op", False, "network temporarily unavailable"),
                    tool_result(4, "retry-op", True, "retry completed"),
                ]
            }

            result = process_error_evidence(
                cfg_for(vault), parsed, [], "demo", "session-1", "2026-07-13"
            )

            self.assertEqual(result, empty_result(ignored=2))
            self.assertEqual(candidate_paths(vault), [])

    def test_success_before_failure_keeps_the_terminal_failure(self):
        with tempfile.TemporaryDirectory() as vault:
            parsed = {
                "observations": [
                    tool_result(1, "terminal-order", True, "first attempt passed"),
                    tool_result(2, "terminal-order", False, "later attempt failed"),
                ]
            }

            result = process_error_evidence(
                cfg_for(vault), parsed, [], "demo", "session-1", "2026-07-13"
            )

            self.assertEqual(result["candidates"], 1)
            record, _content = read_candidate(candidate_paths(vault)[0])
            self.assertIn("later attempt failed", record["excerpt"])

    def test_later_success_removes_only_the_current_session_failure(self):
        with tempfile.TemporaryDirectory() as vault:
            failure = {
                "observations": [
                    tool_result(1, "cross-harvest", False, "temporary terminal failure")
                ]
            }
            success = {
                "observations": [
                    tool_result(2, "cross-harvest", True, "later retry passed")
                ]
            }
            cfg = cfg_for(vault)
            process_error_evidence(
                cfg, failure, [], "demo", "session-1", "2026-07-13"
            )
            process_error_evidence(
                cfg, failure, [], "demo", "session-2", "2026-07-13"
            )

            result = process_error_evidence(
                cfg, success, [], "demo", "session-2", "2026-07-13"
            )
            record, _content = read_candidate(candidate_paths(vault)[0])

            self.assertEqual(result["updated"], 1)
            self.assertEqual(record["seen_count"], 1)
            self.assertEqual(
                [source["session_id"] for source in record["sources"]],
                ["session-1"],
            )

            removed = process_error_evidence(
                cfg, success, [], "demo", "session-1", "2026-07-13"
            )

            self.assertEqual(removed["updated"], 1)
            self.assertEqual(candidate_paths(vault), [])

    def test_terminal_failure_creates_schema_v2_candidate_without_raw_command(self):
        with tempfile.TemporaryDirectory() as vault:
            parsed = {
                "observations": [
                    tool_result(
                        8,
                        "build-operation-hash",
                        False,
                        "build failed because dependency is unavailable",
                    )
                ]
            }

            result = process_error_evidence(
                cfg_for(vault), parsed, [], "demo", "session-1", "2026-07-13"
            )

            self.assertEqual(result["candidates"], 1)
            self.assertEqual(result["updated"], 0)
            record, content = read_candidate(candidate_paths(vault)[0])
            self.assertEqual(record["schema_version"], "2.0")
            self.assertEqual(record["status"], "candidate")
            self.assertEqual(record["type"], "error-evidence-candidate")
            self.assertEqual(record["classification"], "unresolved_finding")
            self.assertEqual(record["project"], "demo")
            self.assertNotIn("source_session", record)
            self.assertEqual(record["sources"], [{"session_id": "session-1", "date": "2026-07-13"}])
            self.assertEqual(record["operation"], "exec_command")
            self.assertEqual(record["operation_hash"], hash_label("build-operation-hash"))
            self.assertEqual(record["severity"], "error")
            self.assertNotIn("raw command", content.lower())

    def test_critical_and_important_review_findings_become_candidates(self):
        with tempfile.TemporaryDirectory() as vault:
            parsed = {
                "observations": [
                    review_finding(1, "critical", "Critical: rollback deletes current data"),
                    review_finding(2, "important", "Important: concurrent writer is unchecked"),
                    review_finding(3, "minor", "Minor: not actionable"),
                ]
            }

            result = process_error_evidence(
                cfg_for(vault), parsed, [], "demo", "session-1", "2026-07-13"
            )

            self.assertEqual(result["candidates"], 2)
            self.assertEqual(result["ignored"], 1)
            records = [read_candidate(path)[0] for path in candidate_paths(vault)]
            self.assertEqual({record["severity"] for record in records}, {"critical", "important"})

    def test_replay_is_idempotent_and_evidence_id_is_stable_across_sessions(self):
        with tempfile.TemporaryDirectory() as vault:
            parsed = {
                "observations": [
                    tool_result(1, "stable-operation", False, "compiler cannot find package")
                ]
            }
            cfg = cfg_for(vault)

            first = process_error_evidence(cfg, parsed, [], "demo", "session-1", "2026-07-13")
            record, _content = read_candidate(candidate_paths(vault)[0])
            evidence_id = record["evidence_id"]
            replay = process_error_evidence(cfg, parsed, [], "demo", "session-1", "2026-07-13")
            second = process_error_evidence(cfg, parsed, [], "demo", "session-2", "2026-07-14")
            record, _content = read_candidate(candidate_paths(vault)[0])

            self.assertEqual(first["candidates"], 1)
            self.assertEqual(replay, empty_result())
            self.assertEqual(second["updated"], 1)
            self.assertEqual(record["evidence_id"], evidence_id)
            self.assertEqual(record["seen_count"], 2)
            self.assertEqual([source["session_id"] for source in record["sources"]], ["session-1", "session-2"])

    def test_source_rows_are_bounded_and_recent_replay_is_idempotent(self):
        with tempfile.TemporaryDirectory() as vault:
            parsed = {
                "observations": [
                    tool_result(1, "bounded-source", False, "terminal failure")
                ]
            }
            cfg = cfg_for(vault, source_limit=3)
            for number in range(5):
                process_error_evidence(
                    cfg,
                    parsed,
                    [],
                    "demo",
                    f"session-{number}",
                    "2026-07-13",
                )

            replay = process_error_evidence(
                cfg,
                parsed,
                [],
                "demo",
                "session-4",
                "2026-07-13",
            )
            record, _content = read_candidate(candidate_paths(vault)[0])

            self.assertEqual(replay, empty_result())
            self.assertEqual(record["seen_count"], 5)
            self.assertEqual(len(record["sources"]), 3)
            self.assertNotIn("source_replay_hashes", record)

    def test_redacts_credentials_limits_excerpt_and_bounds_sources(self):
        with tempfile.TemporaryDirectory() as vault:
            parsed = {
                "observations": [
                    tool_result(
                        1,
                        "private-operation",
                        False,
                        "password=plain-secret sk-abcdefghijklmnopqrstuvwxyz failure " + "x" * 800,
                    )
                ]
            }
            cfg = cfg_for(vault, excerpt_limit=80, source_limit=3)
            for number in range(5):
                process_error_evidence(
                    cfg, parsed, [], "demo", f"session-{number}", "2026-07-13"
                )

            record, content = read_candidate(candidate_paths(vault)[0])
            self.assertLessEqual(len(record["excerpt"]), 80)
            self.assertNotIn("plain-secret", content)
            self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", content)
            self.assertIn("[REDACTED]", content)
            self.assertEqual(record["seen_count"], 5)
            self.assertEqual(len(record["sources"]), 3)
            self.assertEqual(record["sources"][0]["session_id"], "session-2")

    def test_malformed_observations_are_ignored_without_writes(self):
        with tempfile.TemporaryDirectory() as vault:
            parsed = {
                "observations": [
                    None,
                    {"kind": "tool_result", "success": False},
                    {"kind": "review_finding", "severity": "critical"},
                    {"kind": "unexpected", "excerpt": "ignored"},
                ]
            }

            result = process_error_evidence(
                cfg_for(vault), parsed, [], "demo", "session-1", "2026-07-13"
            )

            self.assertEqual(result, empty_result(ignored=4))
            self.assertEqual(candidate_paths(vault), [])

    def test_oversized_observation_batch_is_skipped_as_one_untrusted_unit(self):
        observation = tool_result(1, "oversized-batch", False, "failure")
        observations = [dict(observation, event_index=index) for index in range(4_097)]

        candidates, ignored = error_evidence.classify_observations(
            observations,
            "demo",
            500,
        )

        self.assertEqual(candidates, {})
        self.assertEqual(ignored, 4_097)

    def test_unknown_tool_family_is_ignored_without_persisting_raw_operation(self):
        with tempfile.TemporaryDirectory() as vault:
            parsed = {
                "observations": [
                    {
                        **tool_result(1, "unknown-operation", False, "failure"),
                        "operation": "untrusted_tool_name",
                    }
                ]
            }

            result = process_error_evidence(
                cfg_for(vault), parsed, [], "demo", "session-1", "2026-07-13"
            )

            self.assertEqual(result, empty_result(ignored=1))
            self.assertEqual(candidate_paths(vault), [])

    def test_untrusted_operation_hash_cannot_bypass_redaction(self):
        with tempfile.TemporaryDirectory() as vault:
            observation = tool_result(
                1,
                "password=raw-secret-not-a-hash",
                False,
                "ordinary failure",
            )
            observation["operation_hash"] = "password=raw-secret-not-a-hash"

            result = process_error_evidence(
                cfg_for(vault),
                {"observations": [observation]},
                [],
                "demo",
                "session-1",
                "2026-07-13",
            )

            self.assertEqual(result, empty_result(ignored=1))
            self.assertEqual(candidate_paths(vault), [])

    def test_malformed_candidate_identity_cannot_escape_candidate_directory(self):
        with tempfile.TemporaryDirectory() as vault:
            candidate_dir = os.path.join(
                vault,
                "04-Feedback",
                "_error-candidates",
            )
            os.makedirs(candidate_dir)
            malicious = os.path.join(candidate_dir, "malicious.md")
            record = {
                "evidence_id": "../../outside",
                "schema_version": "2.0",
                "status": "candidate",
                "type": "error-evidence-candidate",
                "classification": "unresolved_finding",
                "project": "demo",
                "operation": "exec_command",
                "operation_hash": "a" * 64,
                "severity": "error",
                "excerpt": "target manifest is missing during migration",
                "seen_count": 1,
                "sources": [],
            }
            with open(malicious, "w", encoding="utf-8") as handle:
                handle.write("---\n")
                yaml.safe_dump(record, handle, allow_unicode=True, sort_keys=False)
                handle.write("---\n")
            with open(malicious, "r", encoding="utf-8") as handle:
                original = handle.read()

            result = process_error_evidence(
                cfg_for(vault),
                {"observations": []},
                [
                    {
                        "type": "path-filesystem",
                        "resolution": (
                            "target manifest is missing during migration; "
                            "recreated and verified"
                        ),
                    }
                ],
                "demo",
                "session-1",
                "2026-07-13",
            )

            self.assertEqual(result, empty_result())
            self.assertFalse(os.path.exists(os.path.join(vault, "outside.md")))
            with open(malicious, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), original)

    def test_explicit_formal_error_resolves_matching_candidate(self):
        with tempfile.TemporaryDirectory() as vault:
            parsed = {
                "observations": [
                    tool_result(
                        1,
                        "formal-operation",
                        False,
                        "migration failed because target manifest is missing",
                    )
                ]
            }
            formal_errors = [
                {
                    "id": "sk-formal-reference-secret-value",
                    "type": "path-filesystem",
                    "resolution": "target manifest is missing; recreated it and verification passed",
                }
            ]

            result = process_error_evidence(
                cfg_for(vault),
                parsed,
                formal_errors,
                "demo",
                "password=source-session-secret",
                "2026-07-13",
            )

            self.assertEqual(result["resolved"], 1)
            record, content = read_candidate(candidate_paths(vault)[0])
            self.assertEqual(record["status"], "resolved")
            self.assertEqual(record["formal_error_type"], "path-filesystem")
            self.assertNotIn("source-session-secret", content)
            self.assertNotIn("formal-reference-secret", content)

    def test_formal_error_cannot_resolve_candidate_from_another_project(self):
        with tempfile.TemporaryDirectory() as vault:
            parsed = {
                "observations": [
                    tool_result(
                        1,
                        "project-isolation",
                        False,
                        "target manifest is missing during migration",
                    )
                ]
            }
            cfg = cfg_for(vault)
            process_error_evidence(
                cfg, parsed, [], "project-a", "session-a", "2026-07-13"
            )

            result = process_error_evidence(
                cfg,
                {"observations": []},
                [
                    {
                        "type": "path-filesystem",
                        "resolution": (
                            "target manifest is missing during migration; fixed"
                        ),
                    }
                ],
                "project-b",
                "session-b",
                "2026-07-13",
            )

            self.assertEqual(result, empty_result())
            record, _content = read_candidate(candidate_paths(vault)[0])
            self.assertEqual(record["project"], "project-a")
            self.assertEqual(record["status"], "candidate")

    def test_invalid_project_and_formal_type_are_rejected(self):
        with tempfile.TemporaryDirectory() as vault:
            parsed = {
                "observations": [
                    tool_result(
                        1,
                        "invalid-identity",
                        False,
                        "target manifest is missing during migration",
                    )
                ]
            }
            cfg = cfg_for(vault)

            invalid_project = process_error_evidence(
                cfg,
                parsed,
                [],
                "../../invalid",
                "session-invalid",
                "2026-07-13",
            )
            self.assertEqual(invalid_project, empty_result(ignored=1))
            self.assertEqual(candidate_paths(vault), [])

            process_error_evidence(
                cfg,
                parsed,
                [],
                "demo",
                "session-valid",
                "2026-07-13",
            )
            malicious_type = process_error_evidence(
                cfg,
                {"observations": []},
                [
                    {
                        "type": "sk-formal-type-secret-value",
                        "resolution": (
                            "target manifest is missing during migration; fixed"
                        ),
                    }
                ],
                "demo",
                "session-valid",
                "2026-07-13",
            )

            self.assertEqual(malicious_type, empty_result())
            record, content = read_candidate(candidate_paths(vault)[0])
            self.assertEqual(record["status"], "candidate")
            self.assertNotIn("formal-type-secret", content)

            invalid_formal_project = process_error_evidence(
                cfg,
                {"observations": []},
                [
                    {
                        "type": "path-filesystem",
                        "project": "../../invalid",
                        "resolution": (
                            "target manifest is missing during migration; fixed"
                        ),
                    }
                ],
                "demo",
                "session-valid",
                "2026-07-13",
            )
            self.assertEqual(invalid_formal_project, empty_result())
            record, _content = read_candidate(candidate_paths(vault)[0])
            self.assertEqual(record["status"], "candidate")

    def test_invalid_utf8_candidate_does_not_block_other_evidence(self):
        with tempfile.TemporaryDirectory() as vault:
            candidate_dir = os.path.join(
                vault,
                "04-Feedback",
                "_error-candidates",
            )
            os.makedirs(candidate_dir)
            with open(os.path.join(candidate_dir, "broken.md"), "wb") as handle:
                handle.write(b"\xff\xfe\x00")

            result = process_error_evidence(
                cfg_for(vault),
                {
                    "observations": [
                        tool_result(1, "valid-after-broken", False, "valid failure")
                    ]
                },
                [],
                "demo",
                "session-1",
                "2026-07-13",
            )

            self.assertEqual(result["candidates"], 1)
            self.assertEqual(len(candidate_paths(vault)), 2)

    def test_corrupt_canonical_candidate_is_preserved_and_only_that_item_is_blocked(self):
        with tempfile.TemporaryDirectory() as vault:
            blocked_hash = hash_label("blocked-canonical")
            blocked_excerpt = "blocked canonical failure"
            blocked_id = evidence_id_for(
                "demo",
                "tool_failure",
                blocked_hash,
                blocked_excerpt,
            )
            candidate_dir = os.path.join(
                vault,
                "04-Feedback",
                "_error-candidates",
            )
            os.makedirs(candidate_dir)
            blocked_path = os.path.join(candidate_dir, f"{blocked_id}.md")
            original = b"\xff\xfecorrupt-user-data"
            with open(blocked_path, "wb") as handle:
                handle.write(original)

            result = process_error_evidence(
                cfg_for(vault),
                {
                    "observations": [
                        {
                            **tool_result(
                                1,
                                "blocked-canonical",
                                False,
                                blocked_excerpt,
                            ),
                            "operation_hash": blocked_hash,
                        },
                        tool_result(
                            2,
                            "unrelated-valid",
                            False,
                            "unrelated valid failure",
                        ),
                    ]
                },
                [],
                "demo",
                "session-1",
                "2026-07-13",
            )

            self.assertEqual(result["candidates"], 1)
            self.assertEqual(result["ignored"], 1)
            with open(blocked_path, "rb") as handle:
                self.assertEqual(handle.read(), original)
            self.assertEqual(len(candidate_paths(vault)), 2)

    def test_invalid_canonical_candidate_field_is_preserved_and_blocked(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = cfg_for(vault)
            parsed = {
                "observations": [
                    tool_result(1, "invalid-canonical-field", False, "terminal failure")
                ]
            }
            process_error_evidence(
                cfg, parsed, [], "demo", "session-1", "2026-07-13"
            )
            path = candidate_paths(vault)[0]
            record, content = read_candidate(path)
            record["operation_hash"] = "not-a-sha256"
            _frontmatter, body = content.split("---", 2)[1:]
            tampered = (
                "---\n"
                + yaml.safe_dump(record, allow_unicode=True, sort_keys=False)
                + "---"
                + body
            )
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(tampered)

            result = process_error_evidence(
                cfg, parsed, [], "demo", "session-2", "2026-07-14"
            )

            self.assertEqual(result, empty_result(ignored=1))
            with open(path, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), tampered)

    def test_strict_candidate_validator_rejects_every_identity_and_state_field(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = cfg_for(vault)
            process_error_evidence(
                cfg,
                {
                    "observations": [
                        tool_result(1, "strict-validator", False, "terminal failure")
                    ]
                },
                [],
                "demo",
                "session-1",
                "2026-07-13",
            )
            valid, _content = read_candidate(candidate_paths(vault)[0])
            self.assertEqual(
                error_evidence.validate_candidate_record(valid, source_limit=20),
                valid,
            )

            mutations = {
                "schema_version": lambda value: value.update(schema_version="1.0"),
                "type": lambda value: value.update(type="error"),
                "status": lambda value: value.update(status="active"),
                "classification": lambda value: value.update(classification="resolved"),
                "project": lambda value: value.update(project="../escape"),
                "source_agent": lambda value: value.update(source_agent="claude"),
                "source_event": lambda value: value.update(source_event=True),
                "kind": lambda value: value.update(kind="review_finding"),
                "operation": lambda value: value.update(operation="unknown"),
                "operation_hash": lambda value: value.update(operation_hash="bad"),
                "severity": lambda value: value.update(severity="critical"),
                "excerpt": lambda value: value.update(excerpt=""),
                "evidence_id": lambda value: value.update(
                    evidence_id="error-evidence-" + ("0" * 64)
                ),
                "first_seen": lambda value: value.update(first_seen="not-a-date"),
                "last_seen": lambda value: value.update(last_seen="2026-07-12"),
                "seen_count": lambda value: value.update(seen_count=0),
                "sources": lambda value: value.update(
                    sources=[{"session_id": f"s-{index}", "date": "2026-07-13"} for index in range(21)]
                ),
                "unknown_field": lambda value: value.update(unknown="data"),
            }
            for field, mutate in mutations.items():
                with self.subTest(field=field):
                    invalid = copy.deepcopy(valid)
                    mutate(invalid)
                    with self.assertRaises(ValueError):
                        error_evidence.validate_candidate_record(
                            invalid,
                            source_limit=20,
                        )

    def test_atomic_write_does_not_follow_precreated_tmp_symlink(self):
        with tempfile.TemporaryDirectory() as vault:
            operation_hash = hash_label("atomic-symlink")
            excerpt = "atomic candidate failure"
            evidence_id = evidence_id_for(
                "demo",
                "tool_failure",
                operation_hash,
                excerpt,
            )
            candidate_dir = os.path.join(
                vault,
                "04-Feedback",
                "_error-candidates",
            )
            os.makedirs(candidate_dir)
            outside = os.path.join(vault, "outside.txt")
            with open(outside, "w", encoding="utf-8") as handle:
                handle.write("keep\n")
            os.symlink(
                outside,
                os.path.join(candidate_dir, f"{evidence_id}.md.tmp"),
            )

            result = process_error_evidence(
                cfg_for(vault),
                {
                    "observations": [
                        {
                            **tool_result(
                                1,
                                "atomic-symlink",
                                False,
                                excerpt,
                            ),
                            "operation_hash": operation_hash,
                        }
                    ]
                },
                [],
                "demo",
                "session-1",
                "2026-07-13",
            )

            self.assertEqual(result["candidates"], 1)
            with open(outside, "r", encoding="utf-8") as handle:
                self.assertEqual(handle.read(), "keep\n")
            candidate = os.path.join(candidate_dir, f"{evidence_id}.md")
            self.assertTrue(os.path.isfile(candidate))
            self.assertFalse(os.path.islink(candidate))

    def test_dirty_generation_is_published_before_candidate_mutation(self):
        with tempfile.TemporaryDirectory() as vault:
            parsed = {
                "observations": [
                    tool_result(1, "marker-first", False, "marker ordering failure")
                ]
            }
            cfg = cfg_for(vault)

            with mock.patch(
                "error_evidence.mark_index_dirty",
                side_effect=OSError("marker publication failed"),
            ):
                with self.assertRaisesRegex(OSError, "marker publication failed"):
                    process_error_evidence(
                        cfg,
                        parsed,
                        [],
                        "demo",
                        "session-1",
                        "2026-07-13",
                    )

            self.assertEqual(candidate_paths(vault), [])
            self.assertFalse(
                os.path.exists(
                    os.path.join(
                        vault,
                        "04-Feedback/_error-candidates/.index-dirty",
                    )
                )
            )

            retry = process_error_evidence(
                cfg,
                parsed,
                [],
                "demo",
                "session-1",
                "2026-07-13",
            )
            self.assertEqual(retry["candidates"], 1)
            self.assertEqual(len(candidate_paths(vault)), 1)

    def test_dirty_marker_read_error_fails_closed(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = cfg_for(vault)
            candidate_dir = os.path.join(
                vault,
                "04-Feedback",
                "_error-candidates",
            )
            os.makedirs(candidate_dir)
            marker = os.path.join(candidate_dir, ".index-dirty")
            with open(marker, "w", encoding="ascii") as handle:
                handle.write("a" * 64 + "\n")

            with mock.patch(
                "error_evidence.secure_read_bytes",
                side_effect=PermissionError("marker denied"),
            ):
                with self.assertRaisesRegex(
                    ErrorEvidenceStateError,
                    "cannot read error evidence dirty marker",
                ):
                    error_evidence_dirty_token(cfg)

    def test_dirty_marker_symlink_is_rejected_without_reading_target(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = cfg_for(vault)
            candidate_dir = os.path.join(
                vault,
                "04-Feedback",
                "_error-candidates",
            )
            os.makedirs(candidate_dir)
            outside = os.path.join(vault, "outside.md")
            with open(outside, "w", encoding="ascii") as handle:
                handle.write(("d" * 64) + "\n")
            os.symlink(outside, os.path.join(candidate_dir, ".index-dirty"))

            with self.assertRaisesRegex(
                ErrorEvidenceStateError,
                "cannot read error evidence dirty marker",
            ):
                error_evidence_dirty_token(cfg)

            with open(outside, "r", encoding="ascii") as handle:
                self.assertEqual(handle.read(), ("d" * 64) + "\n")

    def test_legacy_project_alias_can_resolve_canonical_candidate(self):
        with tempfile.TemporaryDirectory() as vault:
            cfg = cfg_for(vault)
            parsed = {
                "observations": [
                    tool_result(
                        1,
                        "alias-resolution",
                        False,
                        "target manifest missing during migration",
                    )
                ]
            }
            process_error_evidence(
                cfg,
                parsed,
                [],
                "agent-memory-beacon",
                "session-1",
                "2026-07-13",
            )

            result = process_error_evidence(
                cfg,
                {"observations": []},
                [
                    {
                        "type": "path-filesystem",
                        "project": "github-obsidian-knowledge-brain",
                        "resolution": "target manifest missing during migration and was repaired",
                    }
                ],
                "agent-memory-beacon",
                "session-1",
                "2026-07-13",
            )

            self.assertEqual(result["resolved"], 1)
            record, _content = read_candidate(candidate_paths(vault)[0])
            self.assertEqual(record["status"], "resolved")

    def test_disabled_configuration_writes_nothing(self):
        with tempfile.TemporaryDirectory() as vault:
            result = process_error_evidence(
                cfg_for(vault, enabled=False),
                {"observations": [tool_result(1, "op", False, "failure")]},
                [],
                "demo",
                "session-1",
                "2026-07-13",
            )

            self.assertEqual(result, empty_result())
            self.assertEqual(candidate_paths(vault), [])

    def test_config_defaults_are_validated_and_candidate_path_is_vault_relative(self):
        import config

        with tempfile.TemporaryDirectory() as root:
            vault = os.path.join(root, "vault")
            os.makedirs(vault)
            config_path = os.path.join(root, "config.yaml")
            with open(config_path, "w", encoding="utf-8") as handle:
                yaml.safe_dump({"vault_path": vault}, handle)
            with mock.patch.object(config, "CONFIG_PATH", config_path):
                loaded = config.load_config()
            self.assertEqual(
                loaded["error_evidence"],
                {
                    "enabled": True,
                    "candidate_dir": "04-Feedback/_error-candidates",
                    "excerpt_limit": 500,
                    "source_limit": 20,
                },
            )

            with open(config_path, "w", encoding="utf-8") as handle:
                yaml.safe_dump(
                    {
                        "vault_path": vault,
                        "error_evidence": {"enabled": "yes", "candidate_dir": "../outside"},
                    },
                    handle,
                )
            with mock.patch.object(config, "CONFIG_PATH", config_path):
                with self.assertRaises(TypeError):
                    config.load_config()

            with open(config_path, "w", encoding="utf-8") as handle:
                yaml.safe_dump(
                    {
                        "vault_path": vault,
                        "error_evidence": {
                            "enabled": True,
                            "candidate_dir": "../outside",
                        },
                    },
                    handle,
                )
            with mock.patch.object(config, "CONFIG_PATH", config_path):
                with self.assertRaises(ValueError):
                    config.load_config()

            for field, value in (("excerpt_limit", 2_001), ("source_limit", 101)):
                with self.subTest(field=field):
                    with open(config_path, "w", encoding="utf-8") as handle:
                        yaml.safe_dump(
                            {
                                "vault_path": vault,
                                "error_evidence": {field: value},
                            },
                            handle,
                        )
                    with mock.patch.object(config, "CONFIG_PATH", config_path):
                        with self.assertRaises(ValueError):
                            config.load_config()


def empty_result(ignored=0):
    return {
        "candidates": 0,
        "updated": 0,
        "resolved": 0,
        "ignored": ignored,
        "items": [],
    }


if __name__ == "__main__":
    unittest.main()
