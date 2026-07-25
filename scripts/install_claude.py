#!/usr/bin/env python3
"""Install Claude Code integration without overwriting existing user config."""
import argparse
import json
import os
import shlex
import shutil
import stat
import sys
from datetime import datetime
from pathlib import Path

from config import load_config
from context_install import (
    atomic_write_utf8_text_exact,
    load_managed_patch,
    merge_managed_patch,
    read_utf8_text_exact,
)
from safety import durable_atomic_write


HOOK_EVENTS = ("Stop", "SessionStart")
HOOK_OWNER_MARKER = "AGENT_MEMORY_BEACON_HOOK=1"


def main():
    parser = argparse.ArgumentParser(description="Install Claude Code hooks and CLAUDE.md patch")
    parser.add_argument("--hooks", action="store_true", help="Merge hooks into ~/.claude/settings.json")
    parser.add_argument("--claude-md", action="store_true", help="Append annotation patch to ~/.claude/CLAUDE.md")
    parser.add_argument("--claude-md-path", help="Path to CLAUDE.md. Defaults to ~/.claude/CLAUDE.md")
    parser.add_argument("--dry-run", action="store_true", help="Show actions without writing")
    args = parser.parse_args()

    if not args.hooks and not args.claude_md:
        args.hooks = True
        args.claude_md = True

    cfg = load_config()
    actions = []

    if args.hooks:
        actions.extend(install_hooks(cfg, dry_run=args.dry_run))

    if args.claude_md:
        claude_md_path = Path(args.claude_md_path or Path.home() / ".claude" / "CLAUDE.md")
        actions.extend(install_claude_patch(claude_md_path.expanduser(), dry_run=args.dry_run))

    for action in actions:
        print(action)


def install_hooks(
    cfg,
    dry_run=False,
    scripts_dir=None,
    create_backups=True,
    migration_scripts_dir=None,
):
    user_home = Path(cfg.get("user_home") or Path.home()).expanduser()
    settings_path = user_home / ".claude" / "settings.json"
    parent_identity = _existing_directory_identity(settings_path.parent)
    settings = load_settings(settings_path)

    python_path = validate_python_path(cfg.get("python_path") or sys.executable)
    scripts_dir = _absolute_path(scripts_dir or Path(__file__).resolve().parent)
    migration_scripts_dir = (
        _absolute_path(migration_scripts_dir)
        if migration_scripts_dir is not None
        else None
    )
    harvester = scripts_dir / "session_harvester.py"

    actions = []
    changed = False
    for event in HOOK_EVENTS:
        mode = "stop" if event == "Stop" else "start"
        agent_arg = " --agent claude" if event == "Stop" else ""
        command = (
            f'{HOOK_OWNER_MARKER} "{python_path}" "{harvester}" '
            f"--mode {mode}{agent_arg}"
        )
        event_hooks = settings.setdefault("hooks", {}).setdefault(event, [])
        action = upsert_owned_hook(
            event_hooks,
            event=event,
            script_path=harvester,
            mode=mode,
            desired_command=command,
            migration_script_path=(
                migration_scripts_dir / harvester.name
                if migration_scripts_dir is not None
                else None
            ),
        )
        if action == "current":
            actions.append(f"OK hooks.{event}: already installed")
            continue
        changed = True
        verb = "ADD" if action == "added" else "UPDATE"
        location = "" if action == "added" else " in place"
        actions.append(f"{verb} hooks.{event}{location}: {command}")

    if changed and not dry_run:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        current_parent_identity = _directory_identity(settings_path.parent)
        if parent_identity is None:
            parent_identity = current_parent_identity
        elif current_parent_identity != parent_identity:
            raise OSError("Claude settings parent directory was replaced")
        if create_backups:
            backup_file(settings_path)
        atomic_write_json(
            settings_path,
            settings,
            expected_parent_identity=parent_identity,
        )
        actions.append(f"WROTE {settings_path}")
    elif changed:
        actions.append(f"DRY-RUN would write {settings_path}")

    return actions


def load_settings(path):
    if not path.exists():
        return {"hooks": {}}
    if path.is_symlink():
        raise ValueError(f"existing settings.json must not be a symlink: {path}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"existing settings.json is malformed: {path}") from exc
    except OSError as exc:
        raise ValueError(f"existing settings.json cannot be read: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"existing settings.json must contain an object: {path}")
    if "hooks" in data and not isinstance(data["hooks"], dict):
        raise ValueError(f"existing settings.json has a non-object hooks field: {path}")
    data.setdefault("hooks", {})
    validate_hook_shapes(data["hooks"], path)
    return data


def validate_hook_shapes(hooks, path):
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise ValueError(f"existing hooks.{event} must be a list: {path}")
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError(
                    f"existing hooks.{event} group must be an object: {path}"
                )
            nested = group.get("hooks")
            if not isinstance(nested, list):
                raise ValueError(
                    f"existing hooks.{event} group hooks must be a list: {path}"
                )
            if not all(isinstance(hook, dict) for hook in nested):
                raise ValueError(
                    f"existing hooks.{event} hooks must contain objects: {path}"
                )


def upsert_owned_hook(
    event_hooks,
    *,
    event,
    script_path,
    mode,
    desired_command,
    migration_script_path=None,
):
    matches = []
    for group in event_hooks:
        for hook in group.get("hooks", []):
            if _is_owned_command(
                hook,
                script_path=script_path,
                mode=mode,
                migration_script_path=migration_script_path,
            ):
                matches.append(hook)
    if len(matches) > 1:
        raise ValueError(f"multiple owned hooks.{event} commands found")
    if not matches:
        event_hooks.append(
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": desired_command,
                        "timeout": 120,
                    }
                ],
            }
        )
        return "added"

    hook = matches[0]
    if hook.get("command") == desired_command and hook.get("timeout") == 120:
        return "current"
    hook["type"] = "command"
    hook["command"] = desired_command
    hook["timeout"] = 120
    return "updated"


def _is_owned_command(
    hook,
    *,
    script_path,
    mode,
    migration_script_path=None,
):
    if hook.get("type") != "command":
        return False
    try:
        tokens = shlex.split(str(hook.get("command") or ""))
    except ValueError:
        return False
    marked = bool(tokens and tokens[0] == HOOK_OWNER_MARKER)
    if marked:
        tokens = tokens[1:]
    if len(tokens) < 2:
        return False
    script = tokens[1]
    if not os.path.isabs(script) or os.path.basename(script) != script_path.name:
        return False
    if not _matches_event_arguments(tokens[2:], mode):
        return False
    if marked:
        return True
    return _same_script_path(script, script_path) or (
        migration_script_path is not None
        and _same_script_path(script, migration_script_path)
    )


def _matches_event_arguments(arguments, mode):
    if mode == "stop":
        return arguments == ["--mode", "stop", "--agent", "claude"]
    return arguments == ["--mode", "start"]


def _same_script_path(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(os.fspath(right))
    )


def validate_python_path(python_path):
    try:
        path = os.fspath(python_path)
    except TypeError as exc:
        raise ValueError("python_path must be an absolute executable file") from exc
    if not (
        os.path.isabs(path)
        and os.path.isfile(path)
        and os.access(path, os.X_OK)
    ):
        raise ValueError("python_path must be an absolute executable file")
    return path


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


def install_claude_patch(
    claude_md_path,
    dry_run=False,
    patch_path=None,
    create_backups=True,
):
    patch_text = (
        read_utf8_text_exact(Path(patch_path).expanduser()).strip()
        if patch_path is not None
        else load_managed_patch()
    )

    existing = ""
    if claude_md_path.exists():
        existing = read_utf8_text_exact(claude_md_path)

    new_content, action = merge_managed_patch(existing, patch_text)
    if action == "current":
        return [f"OK {claude_md_path}: managed patch current"]
    if not dry_run:
        if claude_md_path.exists() and create_backups:
            backup_file(claude_md_path)
        claude_md_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(claude_md_path, new_content)
        verb = "UPDATED" if action == "updated" else "WROTE"
        return [f"{verb} {claude_md_path}"]
    return [f"DRY-RUN would {action} managed patch in {claude_md_path}"]


def backup_file(path):
    if not path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, backup_path)


def atomic_write_json(path, data, expected_parent_identity=None):
    payload = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    durable_atomic_write(
        path,
        payload,
        mode=0o600,
        expected_parent_identity=expected_parent_identity,
        preserve_existing_mode=False,
    )


def _absolute_path(value):
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def _existing_directory_identity(path):
    if not os.path.lexists(path):
        return None
    return _directory_identity(path)


def _directory_identity(path):
    current = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(current.st_mode):
        raise OSError(f"Claude settings parent is not a directory: {path}")
    return current.st_dev, current.st_ino


def atomic_write_text(path, text):
    atomic_write_utf8_text_exact(path, text)


if __name__ == "__main__":
    main()
