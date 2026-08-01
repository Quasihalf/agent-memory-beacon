"""Stable product identity and legacy compatibility identifiers."""
from pathlib import Path


PRODUCT_NAME = "Agent Memory Beacon"
PRODUCT_SLUG = "agent-memory-beacon"
PRODUCT_VERSION = "0.7.0"
CODE_PREFIX = "agent_memory_beacon"
DEFAULT_VAULT_DIRNAME = "AgentMemoryBeacon"

NEW_MANAGED_NAMESPACE = "AGENT_MEMORY_BEACON"
NEW_MANAGED_VERSION = 3
LEGACY_MANAGED_NAMESPACES = ("KNOWLEDGE_BRAIN",)

NEW_LAUNCHD_LABELS = {
    "harvest": "io.agent-memory-beacon.harvest",
    "weekly": "io.agent-memory-beacon.weekly",
}
LEGACY_LAUNCHD_LABELS = {
    "harvest": "com.obsidian-knowledge-brain.harvest",
    "weekly": "com.obsidian-knowledge-brain.weekly",
}

LEGACY_PROJECT_SLUG = "github-obsidian-knowledge-brain"
PROJECT_SLUG = PRODUCT_SLUG


def default_vault_path(home=None):
    root = Path(home).expanduser() if home is not None else Path.home()
    return root / DEFAULT_VAULT_DIRNAME
