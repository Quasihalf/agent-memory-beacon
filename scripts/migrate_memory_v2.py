#!/usr/bin/env python3
"""Preview, apply, or roll back the legacy memory schema v2 migration."""
import argparse
import json
from pathlib import Path

from compiler import run as compile_agent_context
from config import load_config
from legacy_memory_migration import (
    apply_migration,
    build_migration_plan,
    rollback_migration,
)
from session_harvester import rebuild_memory_index


def main():
    parser = argparse.ArgumentParser(description="Migrate legacy memory to schema 2.0")
    parser.add_argument("--vault", default="", help="Vault path; defaults to config.yaml")
    parser.add_argument("--apply", action="store_true", help="Back up and apply the migration")
    parser.add_argument("--migration-id", default="", help="Stable backup directory name")
    parser.add_argument("--rollback", default="", help="Rollback manifest path")
    args = parser.parse_args()

    cfg = load_config()
    vault = args.vault or cfg["vault_path"]
    cfg = config_for_vault(cfg, vault)
    vault = cfg["vault_path"]
    if args.rollback:
        result = rollback_migration(vault, args.rollback)
        rebuild_memory_index(cfg, repair_generated=False)
        compile_agent_context(cfg)
    else:
        plan = build_migration_plan(vault)
        if args.apply:
            result = apply_migration(
                plan,
                migration_id=args.migration_id or None,
            )
            rebuild_memory_index(cfg, repair_generated=False)
            compile_agent_context(cfg)
        else:
            result = plan.preview()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def config_for_vault(cfg, vault):
    """Rebase config paths owned by the configured Vault onto an override."""
    updated = dict(cfg)
    selected = Path(vault).expanduser().resolve()
    configured_value = cfg.get("vault_path")
    configured = (
        Path(configured_value).expanduser().resolve()
        if configured_value
        else None
    )
    if configured is not None and selected != configured:
        updated["context_targets"] = []
        updated["claude_md_path"] = ""
    if configured is not None:
        for key in (
            "agent_memory_path",
            "memory_index_path",
            "codex_profile_path",
            "log_dir",
        ):
            value = cfg.get(key)
            if not value:
                continue
            try:
                relative = Path(value).expanduser().resolve().relative_to(configured)
            except ValueError:
                continue
            updated[key] = str(selected / relative)
    updated["vault_path"] = str(selected)
    return updated


if __name__ == "__main__":
    raise SystemExit(main())
