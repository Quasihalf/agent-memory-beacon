#!/usr/bin/env python3
"""Install macOS Codex integration without overwriting existing user config."""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

from config import load_config


HOOK_EVENTS = ("Stop", "SessionStart")
PATCH_MARKERS = ("Agent Memory Vault", "Obsidian Knowledge Brain")


def main():
    parser = argparse.ArgumentParser(description="Install Codex hooks and global AGENTS.md patch")
    parser.add_argument("--hooks", action="store_true", help="Merge hooks into ~/.codex/hooks.json")
    parser.add_argument("--agents", action="store_true", help="Append annotation patch to Codex AGENTS.md")
    parser.add_argument("--agents-path", help="Path to AGENTS.md. Defaults to ~/.codex/AGENTS.md")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without writing")
    args = parser.parse_args()

    if not args.hooks and not args.agents:
        args.hooks = True
        args.agents = True

    cfg = load_config()
    actions = []

    if args.hooks:
        actions.extend(install_hooks(cfg, dry_run=args.dry_run))

    if args.agents:
        default_agents_path = Path(cfg.get("codex_home") or Path.home() / ".codex") / "AGENTS.md"
        agents_path = Path(args.agents_path).expanduser() if args.agents_path else default_agents_path.expanduser()
        actions.extend(install_agents_patch(agents_path, dry_run=args.dry_run))

    for action in actions:
        print(action)


def install_hooks(cfg, dry_run=False):
    hooks_path = Path(cfg.get("codex_home") or Path.home() / ".codex") / "hooks.json"
    hooks_path = hooks_path.expanduser()
    hooks = load_hooks(hooks_path)

    python_path = cfg.get("python_path") or sys.executable
    harvester = Path(__file__).resolve().parent / "session_harvester.py"

    actions = []
    changed = False
    for event in HOOK_EVENTS:
        mode = "stop" if event == "Stop" else "start"
        command = f'"{python_path}" "{harvester}" --mode {mode}'
        entry = {
            "hooks": [{
                "type": "command",
                "command": command,
                "timeout": 120,
            }]
        }
        event_hooks = hooks.setdefault("hooks", {}).setdefault(event, [])
        removed = remove_stale_own_hooks(event_hooks, str(harvester), mode, command)
        if removed:
            changed = True
            actions.append(f"REMOVE hooks.{event}: {removed} stale install(s)")
        if hook_exists(event_hooks, command):
            actions.append(f"OK hooks.{event}: already installed")
            continue
        event_hooks.append(entry)
        changed = True
        actions.append(f"ADD hooks.{event}: {command}")

    if changed and not dry_run:
        backup_file(hooks_path)
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(hooks_path, hooks)
        actions.append(f"WROTE {hooks_path}")
    elif changed:
        actions.append(f"DRY-RUN would write {hooks_path}")

    return actions


def load_hooks(path):
    if not path.exists():
        return {"hooks": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (json.JSONDecodeError, OSError):
        return {"hooks": {}}
    if not isinstance(data, dict):
        return {"hooks": {}}
    data.setdefault("hooks", {})
    return data


def hook_exists(event_hooks, command):
    for group in event_hooks:
        for hook in group.get("hooks", []):
            if hook.get("type") == "command" and hook.get("command") == command:
                return True
    return False


def remove_stale_own_hooks(event_hooks, harvester_path, mode, desired_command):
    removed = 0
    for group in list(event_hooks):
        hooks_list = group.get("hooks", [])
        kept = []
        for hook in hooks_list:
            command = hook.get("command", "")
            is_ours = (
                hook.get("type") == "command"
                and harvester_path in command
                and f"--mode {mode}" in command
            )
            if is_ours and command != desired_command:
                removed += 1
                continue
            kept.append(hook)
        group["hooks"] = kept

    event_hooks[:] = [group for group in event_hooks if group.get("hooks")]
    return removed


def install_agents_patch(agents_path, dry_run=False):
    patch_path = Path(__file__).resolve().parent.parent / "patches" / "CLAUDE.md.patch"
    patch_text = patch_path.read_text(encoding="utf-8").strip()

    existing = ""
    if agents_path.exists():
        existing = agents_path.read_text(encoding="utf-8")

    if any(marker in existing for marker in PATCH_MARKERS):
        return [f"OK {agents_path}: patch already present"]

    new_content = existing.rstrip() + "\n\n" + patch_text + "\n"
    if not dry_run:
        if agents_path.exists():
            backup_file(agents_path)
        agents_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(agents_path, new_content)
        return [f"WROTE {agents_path}"]
    return [f"DRY-RUN would append patch to {agents_path}"]


def backup_file(path):
    if not path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, backup_path)


def atomic_write_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)


def atomic_write_text(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


if __name__ == "__main__":
    main()
