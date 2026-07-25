import glob
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

import yaml


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
FIXTURE = os.path.join(
    REPO_ROOT,
    "tests",
    "fixtures",
    "error_evidence",
    "long-task.jsonl",
)
FIXTURE_SHA256 = "6b1f7c5bc3138d7b98955ec62142a2c7f105bd0a8d56de309cf9ee1b21b80e8b"
sys.path.insert(0, SCRIPTS_DIR)

from error_evidence import classify_observations
from safety import split_frontmatter_text
from session_harvester import process_transcript
from transcript_utils import parse_transcript


class ErrorEvidenceEvaluationTests(unittest.TestCase):
    def test_long_task_fixture_captures_unresolved_without_formal_or_runtime_pollution(self):
        with open(FIXTURE, "rb") as handle:
            fixture_bytes = handle.read()
        self.assertEqual(hashlib.sha256(fixture_bytes).hexdigest(), FIXTURE_SHA256)

        parsed = parse_transcript(FIXTURE)
        candidates, ignored = classify_observations(
            parsed["observations"],
            "eval-project",
            500,
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(ignored, 2)

        with tempfile.TemporaryDirectory() as tmp:
            vault = os.path.join(tmp, "vault")
            os.makedirs(vault)
            transcript = os.path.join(tmp, "long-task.jsonl")
            shutil.copyfile(FIXTURE, transcript)
            cfg = {
                "vault_path": vault,
                "projects": [
                    {
                        "name": "eval-project",
                        "keywords": ["error-evidence-eval"],
                    }
                ],
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

            stdout = io.StringIO()
            with redirect_stdout(stdout):
                changed = process_transcript(cfg, transcript)

            candidate_paths = sorted(
                glob.glob(os.path.join(vault, "04-Feedback/_error-candidates/*.md"))
            )
            persisted = "\n".join(read_text(path) for path in candidate_paths)
            index_dir = os.path.join(vault, "05-Agent-Memory")
            keyword = read_json(os.path.join(index_dir, "keyword-index.json"))
            recall = read_json(os.path.join(index_dir, "recall-index.json"))
            graph = read_json(os.path.join(index_dir, "memory-graph.json"))
            machine_indexes = json.dumps(
                {"keyword": keyword, "recall": recall, "graph": graph},
                ensure_ascii=False,
                sort_keys=True,
            )
            memory_index = read_frontmatter(
                os.path.join(vault, "00-Inbox/Agent Memory Index.md")
            )
            formal_sessions = glob.glob(
                os.path.join(vault, "01-Projects/*/Memory/sessions/*.md")
            )
            formal_pitfalls = glob.glob(
                os.path.join(vault, "01-Projects/*/Memory/pitfalls.md")
            )

            metrics = {
                "unresolved_captured": len(candidate_paths),
                "expected_or_transient_ignored": ignored,
                "formal_error_pollution": len(formal_sessions) + len(formal_pitfalls),
                "candidate_runtime_leaks": sum(
                    token in machine_indexes
                    for token in ("terminalfixturetoken", "reviewfixturetoken")
                ),
                "raw_evidence_leaks": sum(
                    token in persisted
                    for token in ("terminalfixturetoken", "reviewfixturetoken")
                ),
                "persisted_sensitive_strings": sum(
                    secret in persisted
                    for secret in (
                        "fixture-secret",
                        "terminal-secret",
                        "review-secret",
                    )
                ),
            }

            self.assertTrue(changed)
            self.assertEqual(metrics["unresolved_captured"], 1)
            self.assertEqual(metrics["expected_or_transient_ignored"], 2)
            self.assertEqual(metrics["formal_error_pollution"], 0)
            self.assertEqual(metrics["candidate_runtime_leaks"], 0)
            self.assertEqual(metrics["raw_evidence_leaks"], 0)
            self.assertEqual(metrics["persisted_sensitive_strings"], 0)
            self.assertIn("diagnostic=process_failure", persisted)
            self.assertNotIn("expectedredfixturetoken", persisted)
            self.assertNotIn("transientfixturetoken", persisted)
            self.assertIn("[error-evidence] 1 new", stdout.getvalue())
            self.assertIn("2 ignored", stdout.getvalue())
            self.assertEqual(memory_index["error_count"], 0)
            self.assertEqual(memory_index["error_evidence_candidates"], 1)
            self.assertEqual(recall["units"], [])


def read_text(path):
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def read_frontmatter(path):
    frontmatter, _body = split_frontmatter_text(read_text(path))
    return yaml.safe_load(frontmatter) if frontmatter is not None else {}


if __name__ == "__main__":
    unittest.main()
