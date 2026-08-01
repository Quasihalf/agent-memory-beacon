import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

from branding import (
    CODE_PREFIX,
    DEFAULT_VAULT_DIRNAME,
    LEGACY_LAUNCHD_LABELS,
    LEGACY_MANAGED_NAMESPACES,
    LEGACY_PROJECT_SLUG,
    NEW_LAUNCHD_LABELS,
    NEW_MANAGED_NAMESPACE,
    NEW_MANAGED_VERSION,
    PRODUCT_NAME,
    PRODUCT_SLUG,
    PRODUCT_VERSION,
    PROJECT_SLUG,
    default_vault_path,
)


class BrandingTests(unittest.TestCase):
    def test_current_identity_is_centralized(self):
        self.assertEqual(PRODUCT_NAME, "Agent Memory Beacon")
        self.assertEqual(PRODUCT_SLUG, "agent-memory-beacon")
        self.assertEqual(PRODUCT_VERSION, "0.7.0")
        self.assertEqual(CODE_PREFIX, "agent_memory_beacon")
        self.assertEqual(DEFAULT_VAULT_DIRNAME, "AgentMemoryBeacon")
        self.assertEqual(NEW_MANAGED_NAMESPACE, "AGENT_MEMORY_BEACON")
        self.assertEqual(NEW_MANAGED_VERSION, 3)
        self.assertEqual(LEGACY_MANAGED_NAMESPACES, ("KNOWLEDGE_BRAIN",))
        self.assertEqual(PROJECT_SLUG, "agent-memory-beacon")
        self.assertEqual(LEGACY_PROJECT_SLUG, "github-obsidian-knowledge-brain")
        self.assertEqual(
            NEW_LAUNCHD_LABELS,
            {
                "harvest": "io.agent-memory-beacon.harvest",
                "weekly": "io.agent-memory-beacon.weekly",
            },
        )
        self.assertEqual(
            LEGACY_LAUNCHD_LABELS,
            {
                "harvest": "com.obsidian-knowledge-brain.harvest",
                "weekly": "com.obsidian-knowledge-brain.weekly",
            },
        )

    def test_default_vault_path_only_controls_new_installs(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(
                default_vault_path(home),
                Path(home) / "AgentMemoryBeacon",
            )


if __name__ == "__main__":
    unittest.main()
