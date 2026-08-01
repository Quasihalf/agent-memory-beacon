#!/usr/bin/env python3
"""Single portable CLI for Agent Memory Beacon filesystem synchronization."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from config import CONFIG_PATH, load_beacon_sync_config, load_config


PRODUCER_COMMANDS = frozenset({"init", "collect", "materialize", "gc"})
AUTHORITY_COMMANDS = frozenset({"reduce", "publish"})
CANONICAL_SIDE_EFFECT_COMMANDS = AUTHORITY_COMMANDS | {"run"}
ALL_COMMANDS = (
    "init",
    "collect",
    "reduce",
    "publish",
    "materialize",
    "gc",
    "run",
    "doctor",
)


class CliError(RuntimeError):
    """A command is incompatible with the configured synchronization role."""


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise CliError(str(message))


def dispatch(
    command,
    sync_cfg,
    *,
    full_cfg=None,
    include_existing=False,
    bootstrap=False,
):
    """Dispatch one already-validated command and return JSON-safe state."""
    if command not in ALL_COMMANDS:
        raise CliError(f"unsupported sync command: {command}")
    if command == "doctor":
        return sync_status(sync_cfg, full_cfg=full_cfg)
    if not sync_cfg.get("enabled"):
        raise CliError("beacon sync is disabled")
    role = sync_cfg.get("role")
    if role not in {"authority", "producer-replica"}:
        raise CliError("beacon sync role is invalid")
    if (
        os.name == "nt"
        and role == "authority"
        and command in CANONICAL_SIDE_EFFECT_COMMANDS
    ):
        raise CliError("Windows hosts cannot run authority side effects")
    if (
        sys.platform != "darwin"
        and role == "authority"
        and command in CANONICAL_SIDE_EFFECT_COMMANDS
    ):
        raise CliError("macOS is required for authority side effects")
    if command in PRODUCER_COMMANDS and role != "producer-replica":
        raise CliError(f"{command} requires producer-replica role")
    if command in AUTHORITY_COMMANDS and role != "authority":
        raise CliError(f"{command} requires authority role")

    if command == "init":
        return _initialize(sync_cfg)
    if command == "collect":
        return _collect(sync_cfg, include_existing=include_existing)
    if command == "materialize":
        return _materialize(sync_cfg, bootstrap=bootstrap)
    if command == "gc":
        return _gc(sync_cfg)
    if role == "authority" and full_cfg is None:
        raise CliError("authority command requires canonical configuration")
    if command == "reduce":
        return _reduce(full_cfg, sync_cfg)
    if command == "publish":
        generation = _publish_generation(full_cfg, sync_cfg)
        receipts = _publish_receipts(sync_cfg, generation)
        return {"generation": generation, "receipts": receipts}
    if command == "run" and role == "producer-replica":
        return {
            "collect": _collect(
                sync_cfg,
                include_existing=include_existing,
            ),
            "materialize": _materialize(sync_cfg, bootstrap=False),
            "gc": _gc(sync_cfg),
        }
    if command == "run":
        reduced = _reduce(full_cfg, sync_cfg)
        generation = _publish_generation(full_cfg, sync_cfg)
        receipts = _publish_receipts(sync_cfg, generation)
        return {
            "reduce": reduced,
            "generation": generation,
            "receipts": receipts,
        }
    raise CliError(f"command was not dispatched: {command}")


def sync_status(sync_cfg, *, full_cfg=None):
    """Return the bounded, read-only deep synchronization health check."""
    if not sync_cfg.get("enabled"):
        return {"status": "disabled", "enabled": False, "role": ""}
    from doctor import _beacon_sync_check

    cfg = dict(full_cfg) if isinstance(full_cfg, dict) else {}
    cfg["beacon_sync"] = sync_cfg
    check = _beacon_sync_check(cfg)
    return {
        "status": "ok" if check.passed else "error",
        "enabled": True,
        "role": sync_cfg.get("role"),
        "healthy": check.passed,
        "details": check.details,
    }


def _initialize(sync_cfg):
    from beacon_sync_producer import initialize_producer

    return initialize_producer(sync_cfg)


def _collect(sync_cfg, include_existing=False):
    from beacon_sync_producer import collect_transcripts

    return collect_transcripts(
        sync_cfg,
        include_existing=include_existing,
    )


def _gc(sync_cfg):
    from beacon_sync_producer import garbage_collect_outbox

    return garbage_collect_outbox(sync_cfg)


def _materialize(sync_cfg, *, bootstrap=False):
    from beacon_sync_snapshot import materialize_generation

    return materialize_generation(sync_cfg, bootstrap=bootstrap)


def _reduce(full_cfg, sync_cfg):
    from beacon_sync_reducer import reduce_inboxes

    return reduce_inboxes(full_cfg, sync_cfg)


def _publish_generation(full_cfg, sync_cfg):
    from beacon_sync_snapshot import publish_generation

    return publish_generation(full_cfg, sync_cfg)


def _publish_receipts(sync_cfg, generation):
    from beacon_sync_snapshot import publish_pending_receipts

    return publish_pending_receipts(sync_cfg, generation)


def build_parser():
    parser = JsonArgumentParser(
        description="Agent Memory Beacon verified filesystem synchronization"
    )
    parser.add_argument(
        "--config",
        default=CONFIG_PATH,
        help="Path to Agent Memory Beacon config.yaml",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ALL_COMMANDS:
        child = subparsers.add_parser(command)
        if command in {"collect", "run"}:
            child.add_argument(
                "--include-existing",
                action="store_true",
                help="Explicitly transport transcripts present before baseline",
            )
        if command == "materialize":
            child.add_argument(
                "--bootstrap",
                action="store_true",
                help="Explicitly initialize an empty replica from published state",
            )
    return parser


def main(argv=None):
    try:
        args = build_parser().parse_args(argv)
        sync_cfg = load_beacon_sync_config(args.config)
        full_cfg = None
        if (
            sync_cfg.get("enabled")
            and sync_cfg.get("role") == "authority"
            and args.command in {"reduce", "publish", "run", "doctor"}
        ):
            full_cfg = load_config(args.config)
        result = dispatch(
            args.command,
            sync_cfg,
            full_cfg=full_cfg,
            include_existing=bool(getattr(args, "include_existing", False)),
            bootstrap=bool(getattr(args, "bootstrap", False)),
        )
        print(
            json.dumps(
                result,
                ensure_ascii=False,
                sort_keys=True,
                indent=2 if args.pretty else None,
                separators=None if args.pretty else (",", ":"),
            )
        )
        return 0
    except Exception as exc:
        error = {
            "status": "error",
            "error_type": type(exc).__name__,
            "message": str(exc),
        }
        print(
            json.dumps(error, ensure_ascii=False, sort_keys=True),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
