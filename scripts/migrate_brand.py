#!/usr/bin/env python3
import argparse
import json

from brand_migration import (
    apply_brand_migration,
    build_migration_plan,
    plan_summary,
    rollback_brand_migration,
)


def main():
    parser = argparse.ArgumentParser(
        description="Preview or apply the Agent Memory Beacon identity migration"
    )
    parser.add_argument("--vault")
    parser.add_argument("--config")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--migration-id")
    parser.add_argument("--rollback")
    parser.add_argument("--force-rollback", action="store_true")
    args = parser.parse_args()

    if args.rollback:
        if args.apply or args.vault or args.config or args.migration_id:
            parser.error("--rollback cannot be combined with preview/apply options")
        result = rollback_brand_migration(
            args.rollback,
            force=args.force_rollback,
        )
    else:
        if args.force_rollback:
            parser.error("--force-rollback requires --rollback")
        if not args.vault:
            parser.error("--vault is required for preview or apply")
        plan = build_migration_plan(args.vault, config_path=args.config)
        if args.apply:
            if not args.migration_id:
                parser.error("--migration-id is required with --apply")
            result = apply_brand_migration(plan, args.migration_id)
        else:
            result = plan_summary(plan)
            result["writes_performed"] = False
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
