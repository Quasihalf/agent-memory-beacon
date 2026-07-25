import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from memory_promotion import (
    refresh_promotion_proposals,
    scan_promotion_opportunities,
    write_proposals,
)


class MemoryPromotionTests(unittest.TestCase):
    def test_repeated_sources_or_positive_effectiveness_are_eligible(self):
        source_unit = unit(
            "decision-source-evidence",
            "decision",
            source_refs=["session:a", "session:b", "session:c", "note:formal"],
        )
        effect_unit = unit(
            "error-effect-evidence",
            "error",
            source_refs=["session:one"],
        )
        effectiveness = aggregate(
            effect_unit,
            exposures=2,
            accepted=1,
        )

        proposals = scan_promotion_opportunities(
            {"schema_version": "2.0", "units": [source_unit, effect_unit]},
            effectiveness,
            settings(),
        )

        self.assertEqual(
            {item["memory_id"] for item in proposals},
            {source_unit["id"], effect_unit["id"]},
        )
        by_id = {item["memory_id"]: item for item in proposals}
        self.assertEqual(by_id[source_unit["id"]]["source_count"], 3)
        self.assertEqual(by_id[effect_unit["id"]]["exposure_count"], 2)
        self.assertTrue(by_id[source_unit["id"]]["recommended_surface"].startswith("repo:"))
        self.assertTrue(by_id[effect_unit["id"]]["recommended_surface"].startswith("test:"))

    def test_ineligible_types_existing_execution_negative_and_candidate_sources_are_excluded(self):
        records = [
            unit("preference-no-promote", "preference", source_refs=["session:a", "session:b", "session:c"]),
            unit("insight-no-promote", "insight", source_refs=["session:a", "session:b", "session:c"]),
            unit(
                "workflow-already-enforced",
                "workflow",
                source_refs=["session:a", "session:b", "session:c"],
                enforced_by=["system:codex/UserPromptSubmit"],
            ),
            unit(
                "decision-candidate-source",
                "decision",
                source_refs=["session:a", "session:b", "session:c"],
                path="05-Agent-Memory/private-candidates/leak",
            ),
            unit("decision-negative", "decision", source_refs=["session:a"]),
        ]
        effectiveness = aggregate(
            records[-1],
            exposures=5,
            accepted=2,
            corrected=1,
        )

        proposals = scan_promotion_opportunities(
            {"schema_version": "2.0", "units": records},
            effectiveness,
            settings(),
        )

        self.assertEqual(proposals, [])

    def test_stale_effectiveness_revision_does_not_qualify(self):
        record = unit("decision-stale-effect", "decision", source_refs=["session:a"])
        stale = dict(record)
        stale["revision"] = "f" * 64

        proposals = scan_promotion_opportunities(
            {"schema_version": "2.0", "units": [record]},
            aggregate(stale, exposures=10, accepted=5),
            settings(),
        )

        self.assertEqual(proposals, [])

    def test_scan_is_stable_capped_and_workflow_surface_is_runbook(self):
        records = [
            unit(
                f"workflow-cap-{index}",
                "workflow",
                source_refs=["session:a", "session:b", "session:c"],
            )
            for index in range(4)
        ]
        config = settings(max_proposals_per_run=2)

        first = scan_promotion_opportunities(
            {"schema_version": "2.0", "units": records},
            {"memories": {}},
            config,
        )
        second = scan_promotion_opportunities(
            {"schema_version": "2.0", "units": list(reversed(records))},
            {"memories": {}},
            config,
        )

        self.assertEqual(first, second)
        self.assertEqual(len(first), 2)
        self.assertTrue(all(item["recommended_surface"].startswith("runbook:") for item in first))
        self.assertTrue(all(len(item["proposal_digest"]) == 64 for item in first))

    def test_writer_is_preview_first_idempotent_and_rejects_forged_digest(self):
        record = unit(
            "decision-write-proposal",
            "decision",
            source_refs=["session:a", "session:b", "session:c"],
        )
        proposals = scan_promotion_opportunities(
            {"schema_version": "2.0", "units": [record]},
            {"memories": {}},
            settings(),
        )

        with tempfile.TemporaryDirectory() as vault:
            preview = write_proposals(vault, proposals, apply=False)
            self.assertEqual(preview["written"], 0)
            self.assertFalse(os.path.exists(os.path.join(vault, "04-Feedback")))

            first = write_proposals(vault, proposals, apply=True)
            path = first["paths"][0]
            before = Path(path).read_bytes()
            second = write_proposals(vault, proposals, apply=True)
            after = Path(path).read_bytes()

            self.assertEqual(first["written"], 1)
            self.assertEqual(second["written"], 0)
            self.assertEqual(before, after)
            self.assertIn(b"status: candidate", before)

            forged = [dict(proposals[0])]
            forged[0]["proposal_digest"] = "0" * 64
            with self.assertRaises(ValueError):
                write_proposals(vault, forged, apply=True)

    def test_refresh_reads_runtime_index_and_writes_only_isolated_proposals(self):
        record = unit(
            "decision-refresh-proposal",
            "decision",
            source_refs=["session:a", "session:b", "session:c"],
        )
        with tempfile.TemporaryDirectory() as vault:
            index_path = Path(vault, "05-Agent-Memory", "recall-index.json")
            index_path.parent.mkdir(parents=True)
            index_path.write_text(
                json.dumps({"schema_version": "2.0", "units": [record]}),
                encoding="utf-8",
            )
            config = {
                "vault_path": vault,
                "memory_runtime": {"index_path": "05-Agent-Memory/recall-index.json"},
                "memory_effectiveness": {
                    "event_log_path": "04-Feedback/_logs/memory-effectiveness.jsonl"
                },
                "memory_promotion": settings(),
            }

            preview = refresh_promotion_proposals(vault, config, apply=False)
            applied = refresh_promotion_proposals(vault, config, apply=True)

            self.assertEqual(preview["proposals"], 1)
            self.assertEqual(preview["written"], 0)
            self.assertEqual(applied["written"], 1)
            self.assertTrue(applied["paths"][0].startswith(vault + os.sep))
            self.assertIn(
                os.path.join("04-Feedback", "_promotion-proposals"),
                applied["paths"][0],
            )

    def test_refresh_without_index_is_a_non_mutating_noop(self):
        with tempfile.TemporaryDirectory() as vault:
            result = refresh_promotion_proposals(
                vault,
                {
                    "memory_promotion": settings(),
                    "memory_runtime": {"index_path": "05-Agent-Memory/missing.json"},
                },
                apply=True,
            )

            self.assertEqual(result, {"proposals": 0, "written": 0, "paths": []})
            self.assertFalse(Path(vault, "04-Feedback").exists())


def settings(**overrides):
    value = {
        "enabled": True,
        "proposal_dir": "04-Feedback/_promotion-proposals",
        "min_source_count": 3,
        "min_exposure_count": 2,
        "max_proposals_per_run": 10,
    }
    value.update(overrides)
    return value


def unit(memory_id, memory_type, *, source_refs, path="", enforced_by=None):
    record = {
        "id": memory_id,
        "revision": (memory_id.encode("utf-8").hex() + "0" * 64)[:64],
        "type": memory_type,
        "status": "active",
        "project": "demo",
        "source_refs": list(source_refs),
        "path": path or "01-Projects/demo/Memory/decisions",
    }
    if enforced_by:
        record["enforced_by"] = list(enforced_by)
    return record


def aggregate(record, *, exposures, accepted=0, corrected=0, misleading=0):
    return {
        "memories": {
            f"{record['id']}@{record['revision']}": {
                "id": record["id"],
                "revision": record["revision"],
                "exposures": exposures,
                "accepted": accepted,
                "corrected": corrected,
                "manual_helpful": 0,
                "manual_misleading": misleading,
            }
        }
    }


if __name__ == "__main__":
    unittest.main()
