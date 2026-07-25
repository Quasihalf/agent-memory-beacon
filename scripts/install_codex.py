#!/usr/bin/env python3
"""Install macOS Codex integration without overwriting existing user config."""
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


HOOK_EVENTS = ("Stop", "SessionStart", "UserPromptSubmit")
HOOK_OWNER_MARKER = "AGENT_MEMORY_BEACON_HOOK=1"


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


def install_hooks(
    cfg,
    dry_run=False,
    scripts_dir=None,
    create_backups=True,
    migration_scripts_dir=None,
):
    hooks_path = Path(cfg.get("codex_home") or Path.home() / ".codex") / "hooks.json"
    hooks_path = hooks_path.expanduser()
    parent_identity = _existing_directory_identity(hooks_path.parent)
    python_path = validate_python_path(cfg.get("python_path") or sys.executable)
    hooks = load_hooks(hooks_path)
    scripts_dir = (
        Path(os.path.abspath(os.path.expanduser(os.fspath(scripts_dir))))
        if scripts_dir is not None
        else Path(__file__).resolve().parent
    )
    migration_scripts_dir = (
        Path(
            os.path.abspath(
                os.path.expanduser(os.fspath(migration_scripts_dir))
            )
        )
        if migration_scripts_dir is not None
        else None
    )
    harvester = scripts_dir / "session_harvester.py"
    prompt_hook = scripts_dir / "codex_prompt_hook.py"
    runtime_config = cfg.get("memory_runtime") or {}
    hook_timeout_ms = runtime_config.get("hook_timeout_ms", 2000)
    if hook_timeout_ms != 2000:
        raise ValueError("memory_runtime.hook_timeout_ms must be exactly 2000")
    specifications = (
        ("Stop", harvester, "--mode stop --agent codex", 120, "stop"),
        ("SessionStart", harvester, "--mode start", 120, "start"),
        ("UserPromptSubmit", prompt_hook, "", hook_timeout_ms // 1000, ""),
    )

    actions = []
    changed = False
    prompt_changed = False
    for event, script, arguments, timeout, mode in specifications:
        suffix = f" {arguments}" if arguments else ""
        command = f'{HOOK_OWNER_MARKER} "{python_path}" "{script}"{suffix}'
        event_hooks = hooks.setdefault("hooks", {}).setdefault(event, [])
        action = upsert_owned_hook(
            event_hooks,
            event=event,
            script_path=script,
            script_name=script.name,
            mode=mode,
            desired_command=command,
            desired_timeout=timeout,
            migration_script_path=(
                migration_scripts_dir / script.name
                if migration_scripts_dir is not None
                else None
            ),
        )
        if action == "current":
            actions.append(f"OK hooks.{event}: already installed")
            continue
        changed = True
        prompt_changed = prompt_changed or event == "UserPromptSubmit"
        verb = "ADD" if action == "added" else "UPDATE"
        location = "" if action == "added" else " in place"
        actions.append(f"{verb} hooks.{event}{location}: {command}")

    if changed and not dry_run:
        hooks_path.parent.mkdir(parents=True, exist_ok=True)
        current_parent_identity = _directory_identity(hooks_path.parent)
        if parent_identity is None:
            parent_identity = current_parent_identity
        elif current_parent_identity != parent_identity:
            raise OSError("hooks parent directory was replaced")
        if create_backups:
            backup_file(hooks_path)
        try:
            atomic_write_json(
                hooks_path,
                hooks,
                expected_parent_identity=parent_identity,
            )
        except NotADirectoryError as exc:
            raise OSError("hooks parent directory was replaced") from exc
        actions.append(f"WROTE {hooks_path}")
    elif changed:
        actions.append(f"DRY-RUN would write {hooks_path}")

    if prompt_changed:
        actions.append(
            "REVIEW Codex /hooks: enable and trust Agent Memory Beacon UserPromptSubmit"
        )

    return actions


def load_hooks(path):
    if not path.exists():
        return {"hooks": {}}
    if path.is_symlink():
        raise ValueError(f"existing hooks.json must not be a symlink: {path}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"existing hooks.json is malformed: {path}") from exc
    except OSError as exc:
        raise ValueError(f"existing hooks.json cannot be read: {path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"existing hooks.json must contain an object: {path}")
    if "hooks" in data and not isinstance(data["hooks"], dict):
        raise ValueError(f"existing hooks.json has a non-object hooks field: {path}")
    data.setdefault("hooks", {})
    validate_hook_shapes(data["hooks"], path)
    return data


def validate_hook_shapes(hooks, path):
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            raise ValueError(f"existing hooks.{event} must be a list: {path}")
        for group in groups:
            if not isinstance(group, dict):
                raise ValueError(f"existing hooks.{event} group must be an object: {path}")
            nested = group.get("hooks")
            if not isinstance(nested, list):
                raise ValueError(f"existing hooks.{event} group hooks must be a list: {path}")
            if not all(isinstance(hook, dict) for hook in nested):
                raise ValueError(f"existing hooks.{event} hooks must contain objects: {path}")


def upsert_owned_hook(
    event_hooks,
    *,
    event,
    script_path,
    script_name,
    mode,
    desired_command,
    desired_timeout,
    migration_script_path=None,
):
    matches = []
    for group_index, group in enumerate(event_hooks):
        for hook_index, hook in enumerate(group.get("hooks", [])):
            if _is_owned_command(
                hook,
                script_path=script_path,
                script_name=script_name,
                mode=mode,
                migration_script_path=migration_script_path,
            ):
                matches.append((group_index, hook_index, hook))
    if len(matches) > 1:
        raise ValueError(f"multiple owned hooks.{event} commands found")
    if not matches:
        event_hooks.append(
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": desired_command,
                        "timeout": desired_timeout,
                    }
                ]
            }
        )
        return "added"

    _, _, hook = matches[0]
    if (
        hook.get("command") == desired_command
        and hook.get("timeout") == desired_timeout
    ):
        return "current"
    hook["type"] = "command"
    hook["command"] = desired_command
    hook["timeout"] = desired_timeout
    return "updated"


def _is_owned_command(
    hook,
    *,
    script_path,
    script_name,
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
    if not os.path.isabs(script) or os.path.basename(script) != script_name:
        return False
    if not _matches_event_arguments(tokens[2:], mode):
        return False
    if marked:
        return True
    return _same_script_path(script, script_path) or (
        migration_script_path is not None
        and _same_script_path(script, migration_script_path)
    )


def _same_script_path(left, right):
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(
        os.path.abspath(os.fspath(right))
    )


def _matches_event_arguments(arguments, mode):
    if mode == "stop":
        return arguments in (
            ["--mode", "stop"],
            ["--mode", "stop", "--agent", "codex"],
        )
    if mode == "start":
        return arguments == ["--mode", "start"]
    return arguments == []


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


def install_agents_patch(
    agents_path,
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
    if agents_path.exists():
        existing = read_utf8_text_exact(agents_path)

    new_content, action = merge_managed_patch(existing, patch_text)
    if action == "current":
        return [f"OK {agents_path}: managed patch current"]
    if not dry_run:
        if agents_path.exists() and create_backups:
            backup_file(agents_path)
        agents_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(agents_path, new_content)
        verb = "UPDATED" if action == "updated" else "WROTE"
        return [f"{verb} {agents_path}"]
    return [f"DRY-RUN would {action} managed patch in {agents_path}"]


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


def _existing_directory_identity(path):
    if not os.path.lexists(path):
        return None
    return _directory_identity(path)


def _directory_identity(path):
    current = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(current.st_mode):
        raise OSError(f"hooks parent is not a directory: {path}")
    return current.st_dev, current.st_ino


def atomic_write_text(path, text):
    atomic_write_utf8_text_exact(path, text)


if __name__ == "__main__":
    main()
