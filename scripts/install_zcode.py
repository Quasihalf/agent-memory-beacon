#!/usr/bin/env python3
"""Install ZCode user context and macOS background harvesting."""
import argparse
from pathlib import Path

from config import load_config
from install_codex import install_agents_patch
from install_launchd import install_launch_agents


def install_zcode_context(cfg, target=None, dry_run=False):
    zcode_home = Path(cfg.get("zcode_home") or Path.home() / ".zcode").expanduser()
    agents_path = Path(target).expanduser() if target else zcode_home / "AGENTS.md"
    return install_agents_patch(agents_path, dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(
        description="Install ZCode AGENTS.md and Agent Memory Beacon background jobs"
    )
    parser.add_argument("--agents-path")
    parser.add_argument("--context-only", action="store_true")
    parser.add_argument("--scheduler-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-load", action="store_true")
    args = parser.parse_args()
    if args.context_only and args.scheduler_only:
        parser.error("--context-only and --scheduler-only are mutually exclusive")

    cfg = load_config()
    actions = []
    if not args.scheduler_only:
        actions.extend(
            install_zcode_context(
                cfg,
                target=args.agents_path,
                dry_run=args.dry_run,
            )
        )
    if not args.context_only:
        actions.extend(
            install_launch_agents(
                cfg,
                dry_run=args.dry_run,
                load=not args.no_load,
            )
        )
    for action in actions:
        print(action)


if __name__ == "__main__":
    main()
