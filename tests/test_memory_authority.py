import os
import sys
import unittest


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "scripts")
sys.path.insert(0, SCRIPTS_DIR)

from memory_authority import (
    authority_rank,
    authority_revision_payload,
    authority_route,
    normalize_authority_metadata,
)


class MemoryAuthorityTests(unittest.TestCase):
    def test_normalization_is_deterministic_and_preserves_safe_metadata(self):
        normalized = normalize_authority_metadata(
            {
                "authority_role": "canonical",
                "authority_owner": "runtime maintainers",
                "canonical_source": "repo:scripts/memory_runtime.py",
                "enforced_by": [
                    "test:tests/test_memory_runtime.py",
                    "lint:doctor/live",
                    "test:tests/test_memory_runtime.py",
                ],
                "verification_refs": [
                    "runbook:release/verify",
                    "test:tests/test_install_runtime.py",
                ],
                "verified_at": "2026-07-22",
                "freshness_policy": "source-change",
            }
        )

        self.assertEqual(normalized["authority_role"], "canonical")
        self.assertEqual(normalized["authority_owner"], "runtime maintainers")
        self.assertEqual(
            normalized["enforced_by"],
            ["lint:doctor/live", "test:tests/test_memory_runtime.py"],
        )
        self.assertEqual(
            normalized["verification_refs"],
            ["runbook:release/verify", "test:tests/test_install_runtime.py"],
        )

    def test_unsafe_locators_are_rejected(self):
        unsafe = (
            "/Users/private/file.py",
            "file:/Users/private/file.py",
            "file:../private/file.py",
            "repo:scripts/../../private",
            "file:C:\\Users\\private\\file.py",
            "unknown:somewhere",
            "repo:safe\x00unsafe",
            "url:https://user:password@example.com/private",
            "url:https://example.com/path?token=secret",
        )
        for locator in unsafe:
            with self.subTest(locator=locator):
                with self.assertRaises(ValueError):
                    normalize_authority_metadata(
                        {
                            "authority_role": "canonical",
                            "authority_owner": "owner",
                            "canonical_source": locator,
                        }
                    )

    def test_partial_invalid_role_policy_and_date_are_rejected(self):
        invalid = (
            {"authority_role": "canonical"},
            {"authority_role": "canonical", "authority_owner": "owner"},
            {"authority_role": "operationalized", "authority_owner": "owner"},
            {
                "authority_role": "unknown",
                "authority_owner": "owner",
                "canonical_source": "repo:README.md",
            },
            {
                "authority_role": "canonical",
                "authority_owner": "owner",
                "canonical_source": "repo:README.md",
                "freshness_policy": "hourly",
            },
            {
                "authority_role": "canonical",
                "authority_owner": "owner",
                "canonical_source": "repo:README.md",
                "verified_at": "22/07/2026",
            },
        )
        for record in invalid:
            with self.subTest(record=record):
                with self.assertRaises(ValueError):
                    normalize_authority_metadata(record)

    def test_revision_payload_is_empty_for_legacy_and_fixed_for_authority(self):
        self.assertEqual(authority_revision_payload({}), {})

        payload = authority_revision_payload(
            {
                "authority_role": "operationalized",
                "authority_owner": "launchd",
                "enforced_by": ["system:launchd/com.agent-memory-beacon.harvester"],
            }
        )

        self.assertEqual(
            set(payload),
            {
                "authority_role",
                "authority_owner",
                "canonical_source",
                "enforced_by",
                "verification_refs",
                "verified_at",
                "freshness_policy",
            },
        )
        self.assertEqual(payload["canonical_source"], "")
        self.assertEqual(payload["verification_refs"], [])

    def test_rank_and_route_are_conservative(self):
        records = {
            "canonical": {
                "authority_role": "canonical",
                "authority_owner": "repository",
                "canonical_source": "repo:docs/contract.md",
            },
            "operationalized": {
                "authority_role": "operationalized",
                "authority_owner": "runtime",
                "enforced_by": ["system:launchd/job"],
            },
            "rationale": {
                "authority_role": "rationale",
                "authority_owner": "architecture notes",
                "canonical_source": "repo:docs/contract.md",
            },
            "index": {
                "authority_role": "index",
                "authority_owner": "Obsidian",
                "canonical_source": "note:03-Maps/topic-index",
            },
        }

        self.assertGreater(authority_rank(records["canonical"]), authority_rank(records["rationale"]))
        self.assertGreater(authority_rank(records["operationalized"]), authority_rank(records["index"]))
        self.assertEqual(authority_rank({}), 0)
        self.assertEqual(authority_route(records["canonical"]), "repo:docs/contract.md")
        self.assertEqual(authority_route(records["operationalized"]), "system:launchd/job")


if __name__ == "__main__":
    unittest.main()
