#!/usr/bin/env python3
"""Install only the Agent Memory Beacon synchronization scheduler and hooks."""
from __future__ import annotations

import argparse
import copy
import json
import ntpath
import os
import plistlib
import shlex
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from beacon_sync_protocol import (
    ProtocolError,
    _assert_supported_windows_atomic_filesystem,
    portable_atomic_write,
    portable_file_lock,
    portable_unlink_regular,
)
from config import CONFIG_PATH, load_beacon_sync_config, load_config


LAUNCHD_LABEL = "io.agent-memory-beacon.sync"
LAUNCHD_OWNER_ENV = "AGENT_MEMORY_BEACON_SYNC_OWNER"
LAUNCHD_OWNER_VALUE = "v1"
WINDOWS_TASK_NAME = "Agent Memory Beacon Sync"
TASK_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"
WINDOWS_TASK_OWNER_URI = r"\Agent Memory Beacon Sync"
WINDOWS_TASK_OWNER_DESCRIPTION = (
    "Agent Memory Beacon synchronization task "
    "[owner=agent-memory-beacon-sync:v1]"
)
HOOK_OWNER_MARKER = "agent_memory_beacon_sync_hook=1"
HOOK_EVENTS = ("Stop", "SessionStart")
MAX_HOOK_FILE_BYTES = 4 * 1024 * 1024
MAX_TASK_XML_BYTES = 4 * 1024 * 1024
_TASK_XML_UNORDERED_CONTAINERS = frozenset(
    ("Task", "RegistrationInfo", "Settings")
)
WINDOWS_TASK_MISSING_MARKERS = (
    "cannot find the file specified",
    "specified task name does not exist",
    "the task does not exist",
    "error_file_not_found",
    "0x80070002",
    "系统找不到指定的文件",
    "指定的任务名称不存在",
    "任务不存在",
)
_UNSET = object()


class InstallerError(RuntimeError):
    """A scheduler or hook installation failed without a safe recovery."""


def build_launchd_plist(
    *,
    python_path,
    script_path,
    config_path,
    log_dir,
    interval_seconds=60,
):
    interval = _positive_int(interval_seconds, "interval_seconds")
    if interval < 60:
        raise ValueError("launchd interval_seconds must be at least 60")
    return {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [
            str(python_path),
            "-E",
            "-s",
            "-X",
            "utf8",
            "-B",
            str(script_path),
            "--config",
            str(config_path),
            "run",
        ],
        "WorkingDirectory": str(Path(script_path).parent),
        "ProcessType": "Background",
        "EnvironmentVariables": {
            LAUNCHD_OWNER_ENV: LAUNCHD_OWNER_VALUE,
        },
        "StandardOutPath": str(Path(log_dir) / f"{LAUNCHD_LABEL}.log"),
        "StandardErrorPath": str(Path(log_dir) / f"{LAUNCHD_LABEL}.error.log"),
        "RunAtLoad": True,
        "StartInterval": interval,
        "KeepAlive": False,
        "ThrottleInterval": 10,
    }


def build_windows_task_xml(
    *,
    python_path,
    script_path,
    config_path,
    user_id,
    interval_minutes=1,
    task_name=WINDOWS_TASK_NAME,
):
    interval = _positive_int(interval_minutes, "interval_minutes")
    user_id = str(user_id or "").strip()
    if not user_id:
        raise ValueError("Windows task user_id is required")
    ET.register_namespace("", TASK_NAMESPACE)
    task = ET.Element(_tag("Task"), {"version": "1.4"})
    registration = ET.SubElement(task, _tag("RegistrationInfo"))
    ET.SubElement(
        registration,
        _tag("Description"),
    ).text = WINDOWS_TASK_OWNER_DESCRIPTION
    ET.SubElement(registration, _tag("URI")).text = _windows_task_uri(task_name)
    triggers = ET.SubElement(task, _tag("Triggers"))
    logon = ET.SubElement(triggers, _tag("LogonTrigger"))
    ET.SubElement(logon, _tag("Enabled")).text = "true"
    ET.SubElement(logon, _tag("UserId")).text = user_id
    calendar = ET.SubElement(triggers, _tag("CalendarTrigger"))
    ET.SubElement(calendar, _tag("StartBoundary")).text = "2000-01-01T00:00:00"
    ET.SubElement(calendar, _tag("Enabled")).text = "true"
    repetition = ET.SubElement(calendar, _tag("Repetition"))
    ET.SubElement(repetition, _tag("Interval")).text = f"PT{interval}M"
    ET.SubElement(repetition, _tag("Duration")).text = "P1D"
    ET.SubElement(repetition, _tag("StopAtDurationEnd")).text = "false"
    schedule = ET.SubElement(calendar, _tag("ScheduleByDay"))
    ET.SubElement(schedule, _tag("DaysInterval")).text = "1"

    principals = ET.SubElement(task, _tag("Principals"))
    principal = ET.SubElement(principals, _tag("Principal"), {"id": "Author"})
    ET.SubElement(principal, _tag("UserId")).text = user_id
    ET.SubElement(principal, _tag("LogonType")).text = "InteractiveToken"

    settings = ET.SubElement(task, _tag("Settings"))
    for name, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("AllowHardTerminate", "true"),
        ("StartWhenAvailable", "true"),
        ("RunOnlyIfNetworkAvailable", "false"),
        ("Enabled", "true"),
        ("Hidden", "false"),
        ("WakeToRun", "false"),
        ("ExecutionTimeLimit", "PT10M"),
        ("Priority", "7"),
    ):
        ET.SubElement(settings, _tag(name)).text = value

    actions = ET.SubElement(task, _tag("Actions"), {"Context": "Author"})
    execute = ET.SubElement(actions, _tag("Exec"))
    ET.SubElement(execute, _tag("Command")).text = str(python_path)
    ET.SubElement(execute, _tag("Arguments")).text = subprocess.list2cmdline(
        [
            "-E",
            "-s",
            "-X",
            "utf8",
            "-B",
            str(script_path),
            "--config",
            str(config_path),
            "run",
        ]
    )
    ET.SubElement(execute, _tag("WorkingDirectory")).text = str(
        ntpath.dirname(str(script_path))
    )
    return ET.tostring(task, encoding="unicode", xml_declaration=True)


def merge_collector_hooks(document, command, *, uninstall=False):
    """Merge or remove owned collector hooks without reordering other entries."""
    if not isinstance(document, dict):
        raise ValueError("hook document must contain an object")
    merged = copy.deepcopy(document)
    hooks = merged.get("hooks")
    if hooks is None:
        if uninstall:
            return merged, False
        hooks = {}
        merged["hooks"] = hooks
    if not isinstance(hooks, dict):
        raise ValueError("hook document hooks must contain an object")
    changed = False
    for event in HOOK_EVENTS:
        groups = hooks.get(event)
        if groups is None:
            if uninstall:
                continue
            groups = []
            hooks[event] = groups
        if not isinstance(groups, list):
            raise ValueError(f"hooks.{event} must be a list")
        matches = []
        for group_index, group in enumerate(groups):
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"hooks.{event} contains an invalid group")
            for hook_index, hook in enumerate(group["hooks"]):
                if not isinstance(hook, dict):
                    raise ValueError(f"hooks.{event} contains an invalid hook")
                if _owned_collector_hook(hook):
                    matches.append((group_index, hook_index, hook))
        if len(matches) > 1:
            raise ValueError(f"hooks.{event} contains multiple owned sync hooks")
        if uninstall:
            if matches:
                group_index, hook_index, _hook = matches[0]
                del groups[group_index]["hooks"][hook_index]
                if not groups[group_index]["hooks"]:
                    del groups[group_index]
                changed = True
            continue
        desired = {"type": "command", "command": str(command), "timeout": 30}
        if not matches:
            groups.append({"hooks": [desired]})
            changed = True
            continue
        _group_index, _hook_index, hook = matches[0]
        if any(hook.get(key) != value for key, value in desired.items()):
            hook.update(desired)
            changed = True
    return merged, changed


def install_collector_hook_file(path, command, *, dry_run=False, uninstall=False):
    snapshot = _snapshot_hook_file(path)
    plan = _plan_collector_hook(snapshot, command, uninstall=uninstall)
    if plan["changed"] and not dry_run:
        _apply_hook_plan(snapshot, plan)
    return _public_hook_result(plan, dry_run=dry_run)


def install_macos_scheduler(
    *,
    python_path,
    script_path,
    config_path,
    log_dir,
    home=None,
    interval_seconds=60,
    dry_run=False,
    uninstall=False,
    command_runner=None,
):
    home = Path(home or Path.home()).expanduser()
    launch_agents = home / "Library" / "LaunchAgents"
    path = launch_agents / f"{LAUNCHD_LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    if uninstall:
        has_plist = os.path.lexists(path)
        if has_plist:
            _require_regular(path, "LaunchAgent")
            if not _owned_launchd_plist(path.read_bytes()):
                raise InstallerError(
                    f"LaunchAgent is not owned by Agent Memory Beacon: {path}"
                )
        was_loaded = _launchd_is_loaded(command_runner, domain)
        changed = has_plist or was_loaded
        if not dry_run:
            if was_loaded:
                _run_optional(
                    command_runner,
                    [
                        "/bin/launchctl",
                        "bootout",
                        f"{domain}/{LAUNCHD_LABEL}",
                    ],
                )
                _verify_launchd_state(
                    command_runner,
                    domain,
                    expected_loaded=False,
                )
            if has_plist:
                _durable_unlink(path)
        return {
            "changed": changed,
            "path": str(path),
            "dry_run": bool(dry_run),
        }

    payload = build_launchd_plist(
        python_path=python_path,
        script_path=script_path,
        config_path=config_path,
        log_dir=log_dir,
        interval_seconds=interval_seconds,
    )
    data = plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)
    existing = None
    existing_mode = None
    if os.path.lexists(path):
        _require_regular(path, "LaunchAgent")
        existing_mode = stat.S_IMODE(os.lstat(path).st_mode)
        existing = path.read_bytes()
        if not _owned_launchd_plist(existing):
            raise InstallerError(
                f"LaunchAgent is not owned by Agent Memory Beacon: {path}"
            )
    file_changed = existing != data
    was_loaded = _launchd_is_loaded(command_runner, domain)
    if dry_run:
        return {
            "changed": file_changed or not was_loaded,
            "path": str(path),
            "dry_run": True,
        }

    if not file_changed and was_loaded:
        return {"changed": False, "path": str(path), "dry_run": False}

    if file_changed:
        launch_agents.mkdir(parents=True, exist_ok=True)
        portable_atomic_write(path, data, root=launch_agents, mode=0o600)
    try:
        if was_loaded:
            _run_optional(
                command_runner,
                [
                    "/bin/launchctl",
                    "bootout",
                    f"{domain}/{LAUNCHD_LABEL}",
                ],
            )
        _run_optional(
            command_runner,
            ["/bin/launchctl", "bootstrap", domain, str(path)],
        )
        _verify_launchd_state(
            command_runner,
            domain,
            expected_loaded=True,
        )
    except Exception as install_error:
        rollback_errors = []
        try:
            _run_optional(
                command_runner,
                [
                    "/bin/launchctl",
                    "bootout",
                    f"{domain}/{LAUNCHD_LABEL}",
                ],
                allow_failure=True,
            )
            if file_changed:
                if existing is None:
                    _require_regular(path, "LaunchAgent")
                    _durable_unlink(path)
                else:
                    portable_atomic_write(
                        path,
                        existing,
                        root=launch_agents,
                        mode=existing_mode,
                    )
            if was_loaded:
                if existing is None:
                    raise InstallerError(
                        "cannot restore an orphaned launchd service without its plist"
                    )
                _run_optional(
                    command_runner,
                    ["/bin/launchctl", "bootstrap", domain, str(path)],
                )
                _verify_launchd_state(
                    command_runner,
                    domain,
                    expected_loaded=True,
                )
            else:
                _verify_launchd_state(
                    command_runner,
                    domain,
                    expected_loaded=False,
                )
        except Exception as rollback_error:
            rollback_errors.append(str(rollback_error))
        if rollback_errors:
            raise InstallerError(
                "scheduler installation failed and launchd rollback failed: "
                + "; ".join(rollback_errors)
            ) from install_error
        raise
    return {"changed": True, "path": str(path), "dry_run": False}


def install_windows_scheduler(
    *,
    python_path,
    script_path,
    config_path,
    user_id,
    interval_minutes=1,
    task_name=WINDOWS_TASK_NAME,
    dry_run=False,
    uninstall=False,
    command_runner=None,
):
    existing_xml = _query_windows_task(command_runner, task_name)
    plan = _install_windows_scheduler_from_state(
        python_path=python_path,
        script_path=script_path,
        config_path=config_path,
        user_id=user_id,
        interval_minutes=interval_minutes,
        task_name=task_name,
        dry_run=True,
        uninstall=uninstall,
        command_runner=command_runner,
        existing_xml=existing_xml,
    )
    if dry_run:
        return plan
    if not plan["changed"]:
        result = dict(plan)
        result["dry_run"] = False
        return result
    current_xml = _query_windows_task(command_runner, task_name)
    _assert_windows_task_unchanged(existing_xml, current_xml, task_name)
    expected_xml = (
        None
        if uninstall
        else build_windows_task_xml(
            python_path=python_path,
            script_path=script_path,
            config_path=config_path,
            user_id=user_id,
            interval_minutes=interval_minutes,
            task_name=task_name,
        )
    )
    try:
        return _install_windows_scheduler_from_state(
            python_path=python_path,
            script_path=script_path,
            config_path=config_path,
            user_id=user_id,
            interval_minutes=interval_minutes,
            task_name=task_name,
            dry_run=False,
            uninstall=uninstall,
            command_runner=command_runner,
            existing_xml=current_xml,
        )
    except Exception as install_error:
        try:
            _restore_windows_task(
                command_runner,
                task_name,
                existing_xml,
                expected_current=expected_xml,
            )
        except Exception as rollback_error:
            raise InstallerError(
                f"Windows scheduler installation failed: {install_error}; "
                f"rollback failed: {rollback_error}"
            ) from install_error
        raise


def _install_windows_scheduler_from_state(
    *,
    python_path,
    script_path,
    config_path,
    user_id,
    interval_minutes,
    task_name,
    dry_run,
    uninstall,
    command_runner,
    existing_xml,
):
    if existing_xml is not None and not _owned_windows_task_xml(
        existing_xml,
        task_name=task_name,
    ):
        raise InstallerError(
            f"Windows task is not owned by Agent Memory Beacon: {task_name}"
        )
    if uninstall:
        changed = existing_xml is not None
        if changed and not dry_run:
            _delete_windows_task(command_runner, task_name)
        return {
            "changed": changed,
            "task_name": task_name,
            "dry_run": bool(dry_run),
        }
    xml = build_windows_task_xml(
        python_path=python_path,
        script_path=script_path,
        config_path=config_path,
        user_id=user_id,
        interval_minutes=interval_minutes,
        task_name=task_name,
    )
    if (
        existing_xml is not None
        and _normalized_task_xml(existing_xml) == _normalized_task_xml(xml)
    ):
        return {
            "changed": False,
            "task_name": task_name,
            "dry_run": bool(dry_run),
        }
    if not dry_run:
        _create_windows_task(
            command_runner,
            task_name,
            xml,
            replace=existing_xml is not None,
        )
        _verify_windows_task_state(
            command_runner,
            task_name,
            xml,
            operation="creation",
        )
    return {
        "changed": True,
        "task_name": task_name,
        "dry_run": bool(dry_run),
    }


def install_windows_components(
    *,
    python_path,
    script_path,
    config_path,
    user_id,
    hook_paths=(),
    interval_minutes=1,
    task_name=WINDOWS_TASK_NAME,
    dry_run=False,
    uninstall=False,
    command_runner=None,
):
    paths = tuple(Path(path).expanduser() for path in hook_paths)
    if len(set(paths)) != len(paths):
        raise InstallerError("Windows hook paths must be distinct")

    existing_xml = _query_windows_task(command_runner, task_name)
    scheduler_plan = _install_windows_scheduler_from_state(
        python_path=python_path,
        script_path=script_path,
        config_path=config_path,
        user_id=user_id,
        interval_minutes=interval_minutes,
        task_name=task_name,
        dry_run=True,
        uninstall=uninstall,
        command_runner=command_runner,
        existing_xml=existing_xml,
    )
    command = build_collector_command(
        python_path,
        script_path,
        config_path,
    )
    hook_snapshots = [_snapshot_hook_file(path) for path in paths]
    hook_plans = [
        _plan_collector_hook(snapshot, command, uninstall=uninstall)
        for snapshot in hook_snapshots
    ]
    if dry_run:
        return [
            scheduler_plan,
            *(_public_hook_result(plan, dry_run=True) for plan in hook_plans),
        ]

    current_xml = _query_windows_task(command_runner, task_name)
    _assert_windows_task_unchanged(existing_xml, current_xml, task_name)
    scheduler_expected_xml = (
        None
        if uninstall
        else build_windows_task_xml(
            python_path=python_path,
            script_path=script_path,
            config_path=config_path,
            user_id=user_id,
            interval_minutes=interval_minutes,
            task_name=task_name,
        )
    )
    attempted_hook_plans = []
    try:
        scheduler_result = _install_windows_scheduler_from_state(
            python_path=python_path,
            script_path=script_path,
            config_path=config_path,
            user_id=user_id,
            interval_minutes=interval_minutes,
            task_name=task_name,
            dry_run=False,
            uninstall=uninstall,
            command_runner=command_runner,
            existing_xml=current_xml,
        )
        results = [scheduler_result]
        for snapshot, hook_plan in zip(hook_snapshots, hook_plans):
            if hook_plan["changed"]:
                _assert_hook_snapshot_current(snapshot)
                attempted_hook_plans.append((snapshot, hook_plan))
                _write_hook_plan(snapshot, hook_plan)
            results.append(_public_hook_result(hook_plan, dry_run=False))
        return results
    except Exception as transaction_error:
        rollback_errors = []
        for snapshot, hook_plan in reversed(attempted_hook_plans):
            try:
                _restore_hook_file(
                    snapshot,
                    expected_data=hook_plan["data"],
                    expected_mode=hook_plan["mode"],
                )
            except Exception as rollback_error:
                rollback_errors.append(
                    f"{snapshot['path']}: {rollback_error}"
                )
        if scheduler_plan["changed"]:
            try:
                _restore_windows_task(
                    command_runner,
                    task_name,
                    existing_xml,
                    expected_current=scheduler_expected_xml,
                )
            except Exception as rollback_error:
                rollback_errors.append(f"{task_name}: {rollback_error}")
        if rollback_errors:
            raise InstallerError(
                "Windows installation transaction failed and rollback failed: "
                + "; ".join(rollback_errors)
            ) from transaction_error
        raise InstallerError(
            f"Windows installation transaction failed: {transaction_error}"
        ) from transaction_error


def build_collector_command(python_path, script_path, config_path):
    return subprocess.list2cmdline(
        [
            str(python_path),
            "-E",
            "-s",
            "-X",
            "utf8",
            "-X",
            HOOK_OWNER_MARKER,
            "-B",
            str(script_path),
            "--config",
            str(config_path),
            "collect",
        ]
    )


def _owned_collector_hook(hook):
    if hook.get("type") != "command":
        return False
    try:
        tokens = shlex.split(str(hook.get("command") or ""), posix=False)
    except ValueError:
        return False
    tokens = [token.strip('"') for token in tokens]
    marker_indexes = [
        index
        for index, token in enumerate(tokens[:-1])
        if token == "-X" and tokens[index + 1] == HOOK_OWNER_MARKER
    ]
    if len(marker_indexes) != 1 or not tokens or tokens[-1] != "collect":
        return False
    return any(ntpath.basename(token).lower() == "beacon_sync.py" for token in tokens)


def _query_windows_task(command_runner, task_name):
    query = _run_optional(
        command_runner,
        ["schtasks.exe", "/Query", "/TN", task_name, "/XML"],
        allow_failure=True,
    )
    if getattr(query, "returncode", 0) != 0:
        if _windows_task_is_missing(query):
            return None
        message = _command_result_message(query)
        raise InstallerError(f"Windows task query failed: {message}")
    xml = getattr(query, "stdout", "")
    if not str(xml or "").strip():
        raise InstallerError("Windows task query failed: empty XML response")
    return xml


def _snapshot_hook_file(path):
    path = Path(path).expanduser()
    if not os.path.lexists(path):
        return {
            "path": path,
            "data": None,
            "mode": None,
            "identity": None,
        }
    _require_regular(path, "hook configuration")
    info = os.lstat(path)
    if info.st_size > MAX_HOOK_FILE_BYTES:
        raise InstallerError(f"hook configuration exceeds size limit: {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise InstallerError(f"hook configuration cannot be read: {path}") from exc
    after = os.lstat(path)
    identity = _file_identity(info)
    if identity != _file_identity(after) or len(data) != after.st_size:
        raise InstallerError(f"hook configuration changed while reading: {path}")
    return {
        "path": path,
        "data": data,
        "mode": stat.S_IMODE(after.st_mode),
        "identity": identity,
    }


def _plan_collector_hook(snapshot, command, *, uninstall):
    path = snapshot["path"]
    data = snapshot["data"]
    if data is None:
        if uninstall:
            return {
                "changed": False,
                "path": path,
                "data": None,
                "mode": 0o600,
            }
        document = {"hooks": {}}
    else:
        try:
            document = json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise InstallerError(f"hook configuration is invalid: {path}") from exc
    try:
        merged, changed = merge_collector_hooks(
            document,
            command,
            uninstall=uninstall,
        )
    except ValueError as exc:
        raise InstallerError(f"hook configuration is invalid: {path}") from exc
    desired_data = data
    if changed:
        desired_data = (
            json.dumps(
                merged,
                ensure_ascii=False,
                indent=2,
                sort_keys=False,
            )
            + "\n"
        ).encode("utf-8")
    return {
        "changed": changed,
        "path": path,
        "data": desired_data,
        "mode": snapshot["mode"] if snapshot["mode"] is not None else 0o600,
    }


def _public_hook_result(plan, *, dry_run):
    return {
        "changed": bool(plan["changed"]),
        "path": str(plan["path"]),
        "dry_run": bool(dry_run),
    }


def _apply_hook_plan(snapshot, plan):
    if not plan["changed"]:
        return
    _assert_hook_snapshot_current(snapshot)
    _write_hook_plan(snapshot, plan)


def _write_hook_plan(snapshot, plan):
    path = snapshot["path"]
    path.parent.mkdir(parents=True, exist_ok=True)
    portable_atomic_write(
        path,
        plan["data"],
        root=path.parent,
        mode=plan["mode"],
    )


def _assert_hook_snapshot_current(snapshot):
    path = snapshot["path"]
    if snapshot["data"] is None:
        if os.path.lexists(path):
            raise InstallerError(
                f"hook configuration changed after preflight: {path}"
            )
        return
    if not os.path.lexists(path):
        raise InstallerError(f"hook configuration changed after preflight: {path}")
    _require_regular(path, "hook configuration")
    current = os.lstat(path)
    if (
        _file_identity(current) != snapshot["identity"]
        or stat.S_IMODE(current.st_mode) != snapshot["mode"]
    ):
        raise InstallerError(f"hook configuration changed after preflight: {path}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise InstallerError(f"hook configuration cannot be read: {path}") from exc
    after = os.lstat(path)
    if (
        _file_identity(after) != snapshot["identity"]
        or data != snapshot["data"]
    ):
        raise InstallerError(f"hook configuration changed after preflight: {path}")


def _restore_hook_file(snapshot, *, expected_data, expected_mode):
    path = snapshot["path"]
    data = snapshot["data"]
    if _hook_matches_snapshot(snapshot):
        return
    if not _hook_matches_expected(path, expected_data, expected_mode):
        raise InstallerError(
            f"hook configuration changed after installer write: {path}"
        )
    if data is None:
        _durable_unlink(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    portable_atomic_write(
        path,
        data,
        root=path.parent,
        mode=snapshot["mode"],
    )


def _restore_windows_task(
    command_runner,
    task_name,
    previous_xml,
    *,
    expected_current=_UNSET,
):
    current_xml = _query_windows_task(command_runner, task_name)
    if current_xml is not None and not _owned_windows_task_xml(
        current_xml,
        task_name=task_name,
    ):
        raise InstallerError(
            f"refusing to overwrite foreign Windows task during rollback: {task_name}"
        )
    if _same_windows_task_xml(current_xml, previous_xml):
        return
    if previous_xml is None:
        _delete_windows_task(
            command_runner,
            task_name,
            operation="rollback",
        )
        return
    _create_windows_task(
        command_runner,
        task_name,
        previous_xml,
        replace=current_xml is not None,
    )
    _verify_windows_task_state(
        command_runner,
        task_name,
        previous_xml,
        operation="rollback",
    )


def _delete_windows_task(command_runner, task_name, *, operation="deletion"):
    _run_optional(
        command_runner,
        ["schtasks.exe", "/Delete", "/TN", task_name, "/F"],
    )
    _verify_windows_task_state(
        command_runner,
        task_name,
        None,
        operation=operation,
    )


def _verify_windows_task_state(
    command_runner,
    task_name,
    expected_xml,
    *,
    operation,
):
    actual_xml = _query_windows_task(command_runner, task_name)
    if not _same_windows_task_xml(actual_xml, expected_xml):
        difference = _windows_task_xml_difference(actual_xml, expected_xml)
        raise InstallerError(
            f"Windows task {operation} verification failed: {task_name}; "
            f"{difference}"
        )


def _create_windows_task(command_runner, task_name, xml, *, replace):
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            suffix=".xml",
            delete=False,
        ) as handle:
            handle.write(_task_xml_scheduler_bytes(xml))
            temporary_path = handle.name
        arguments = [
            "schtasks.exe",
            "/Create",
            "/TN",
            task_name,
            "/XML",
            temporary_path,
        ]
        if replace:
            arguments.append("/F")
        _run_optional(command_runner, arguments)
    finally:
        if temporary_path:
            try:
                _durable_unlink(temporary_path)
            except FileNotFoundError:
                pass


def _owned_launchd_plist(data):
    try:
        payload = plistlib.loads(bytes(data))
    except (ValueError, TypeError):
        return False
    if not isinstance(payload, dict) or payload.get("Label") != LAUNCHD_LABEL:
        return False
    environment = payload.get("EnvironmentVariables")
    if (
        isinstance(environment, dict)
        and environment.get(LAUNCHD_OWNER_ENV) == LAUNCHD_OWNER_VALUE
    ):
        return True
    arguments = payload.get("ProgramArguments")
    return isinstance(arguments, list) and any(
        Path(str(argument)).name == "beacon_sync.py"
        for argument in arguments
    )


def _launchd_is_loaded(command_runner, domain):
    result = _run_optional(
        command_runner,
        [
            "/bin/launchctl",
            "print",
            f"{domain}/{LAUNCHD_LABEL}",
        ],
        allow_failure=True,
    )
    return getattr(result, "returncode", 0) == 0


def _verify_launchd_state(command_runner, domain, *, expected_loaded):
    loaded = _launchd_is_loaded(command_runner, domain)
    if loaded != expected_loaded:
        expected = "loaded" if expected_loaded else "unloaded"
        raise InstallerError(
            f"launchd service verification failed: expected {expected}"
        )


def _run_optional(command_runner, arguments, *, allow_failure=False):
    runner = command_runner or subprocess.run
    if command_runner is None:
        result = runner(
            arguments,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    else:
        result = runner(arguments)
    returncode = getattr(result, "returncode", 0)
    if returncode and not allow_failure:
        message = (
            getattr(result, "stderr", "")
            or getattr(result, "stdout", "")
            or f"exit {returncode}"
        )
        raise InstallerError(
            f"scheduler command failed: {str(message).strip()}"
        )
    return result


def _command_result_message(result):
    message = (
        getattr(result, "stderr", "")
        or getattr(result, "stdout", "")
        or f"exit {getattr(result, 'returncode', 'unknown')}"
    )
    return str(message).strip()


def _windows_task_is_missing(result):
    message = " ".join(
        str(value or "")
        for value in (
            getattr(result, "stdout", ""),
            getattr(result, "stderr", ""),
        )
    ).casefold()
    return any(marker.casefold() in message for marker in WINDOWS_TASK_MISSING_MARKERS)


def _assert_windows_task_unchanged(expected_xml, current_xml, task_name):
    if not _same_windows_task_xml(expected_xml, current_xml):
        raise InstallerError(f"Windows task changed after preflight: {task_name}")


def _same_windows_task_xml(left, right):
    if left is None or right is None:
        return left is None and right is None
    return _normalized_task_xml(left) == _normalized_task_xml(right)


def _normalized_task_xml(value):
    root = _normalized_task_xml_root(value)
    return ET.tostring(root, encoding="utf-8")


def _normalized_task_xml_root(value):
    root = _task_xml_root(value)
    for element in root.iter():
        if element.text is not None and not element.text.strip():
            element.text = None
        if element.tail is not None and not element.tail.strip():
            element.tail = None
        if element.attrib:
            attributes = sorted(element.attrib.items())
            element.attrib.clear()
            element.attrib.update(attributes)
        name = element.tag.rsplit("}", 1)[-1]
        if name in _TASK_XML_UNORDERED_CONTAINERS:
            element[:] = sorted(element, key=lambda child: child.tag)
    return root


def _windows_task_xml_difference(actual, expected):
    if actual is None or expected is None:
        return f"expected task present={expected is not None}, got {actual is not None}"
    return _windows_task_element_difference(
        _normalized_task_xml_root(actual),
        _normalized_task_xml_root(expected),
        "/Task",
    ) or "normalized XML differs"


def _windows_task_element_difference(actual, expected, path):
    if actual.tag != expected.tag:
        return f"{path}: expected tag {expected.tag!r}, got {actual.tag!r}"
    expected_attributes = tuple(sorted(expected.attrib.items()))
    actual_attributes = tuple(sorted(actual.attrib.items()))
    if actual_attributes != expected_attributes:
        return (
            f"{path}: expected attributes {expected_attributes!r}, "
            f"got {actual_attributes!r}"
        )
    expected_text = str(expected.text or "").strip()
    actual_text = str(actual.text or "").strip()
    if actual_text != expected_text:
        return f"{path}: expected text {expected_text!r}, got {actual_text!r}"

    actual_children = list(actual)
    expected_children = list(expected)
    expected_names = [
        child.tag.rsplit("}", 1)[-1] for child in expected_children
    ]
    actual_names = [
        child.tag.rsplit("}", 1)[-1] for child in actual_children
    ]
    if actual_names != expected_names:
        return (
            f"{path}: expected child elements {expected_names!r}, "
            f"got {actual_names!r}"
        )
    for index, (actual_child, expected_child) in enumerate(
        zip(actual_children, expected_children),
        start=1,
    ):
        child_name = expected_child.tag.rsplit("}", 1)[-1]
        difference = _windows_task_element_difference(
            actual_child,
            expected_child,
            f"{path}/{child_name}[{index}]",
        )
        if difference:
            return difference
    return None


def _owned_windows_task_xml(value, *, task_name=WINDOWS_TASK_NAME):
    root = _task_xml_root(value)
    return (
        root.findtext(_tag("RegistrationInfo") + "/" + _tag("URI"))
        == _windows_task_uri(task_name)
        and root.findtext(
            _tag("RegistrationInfo") + "/" + _tag("Description")
        )
        == WINDOWS_TASK_OWNER_DESCRIPTION
    )


def _task_xml_input(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        size = len(data)
    else:
        data = str(value or "")
        size = len(data.encode("utf-8"))
    if not data or size > MAX_TASK_XML_BYTES:
        raise InstallerError("existing Windows task XML is missing or oversized")
    return data


def _task_xml_root(value):
    data = _task_xml_input(value)
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise InstallerError("existing Windows task XML is invalid") from exc


def _task_xml_scheduler_bytes(value):
    return ET.tostring(
        _task_xml_root(value),
        encoding="utf-16",
        xml_declaration=True,
    )


def _require_regular(path, name):
    info = os.lstat(path)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise InstallerError(f"{name} is not a regular file: {path}")


def _file_identity(info):
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _hook_matches_snapshot(snapshot):
    path = snapshot["path"]
    if snapshot["data"] is None:
        return not os.path.lexists(path)
    return _hook_matches_expected(path, snapshot["data"], snapshot["mode"])


def _hook_matches_expected(path, expected_data, expected_mode):
    if expected_data is None:
        return not os.path.lexists(path)
    if not os.path.lexists(path):
        return False
    _require_regular(path, "hook configuration")
    info = os.lstat(path)
    if not _hook_mode_matches(info.st_mode, expected_mode):
        return False
    if info.st_size != len(expected_data):
        return False
    try:
        return path.read_bytes() == expected_data
    except OSError as exc:
        raise InstallerError(f"hook configuration cannot be read: {path}") from exc


def _hook_mode_matches(actual_mode, expected_mode):
    if os.name == "nt":
        return bool(actual_mode & stat.S_IWUSR) == bool(
            expected_mode & stat.S_IWUSR
        )
    return stat.S_IMODE(actual_mode) == expected_mode


def _durable_unlink(path):
    path = Path(path)
    _require_regular(path, "installer-managed file")
    info = os.lstat(path)
    try:
        portable_unlink_regular(
            path,
            root=path.parent,
            expected_identity=info,
        )
    except (OSError, ProtocolError, ValueError) as exc:
        raise InstallerError(
            f"cannot durably remove installer-managed file: {path}"
        ) from exc


def _tag(name):
    return f"{{{TASK_NAMESPACE}}}{name}"


def _windows_task_uri(task_name):
    name = str(task_name or "").strip()
    if not name:
        raise ValueError("Windows task_name is required")
    return name if name.startswith("\\") else f"\\{name}"


def _positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _current_windows_user():
    if os.name != "nt":
        raise InstallerError("Windows process token SID resolution requires Windows")
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_current_process = kernel32.GetCurrentProcess
    get_current_process.argtypes = ()
    get_current_process.restype = wintypes.HANDLE
    open_process_token = advapi32.OpenProcessToken
    open_process_token.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    open_process_token.restype = wintypes.BOOL
    get_token_information = advapi32.GetTokenInformation
    get_token_information.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    get_token_information.restype = wintypes.BOOL

    convert = advapi32.ConvertSidToStringSidW
    convert.argtypes = (wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR))
    convert.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = (wintypes.HLOCAL,)
    local_free.restype = wintypes.HLOCAL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not open_process_token(get_current_process(), 0x0008, ctypes.byref(token)):
        error = ctypes.get_last_error()
        raise InstallerError(
            f"Windows process token open failed: {ctypes.WinError(error)}"
        )
    try:
        required_size = wintypes.DWORD()
        get_token_information(
            token,
            1,
            None,
            0,
            ctypes.byref(required_size),
        )
        error = ctypes.get_last_error()
        if error != 122 or required_size.value <= 0:
            raise InstallerError(
                "Windows process token user query failed: "
                f"{ctypes.WinError(error)}"
            )
        token_user = ctypes.create_string_buffer(required_size.value)
        if not get_token_information(
            token,
            1,
            token_user,
            required_size.value,
            ctypes.byref(required_size),
        ):
            error = ctypes.get_last_error()
            raise InstallerError(
                "Windows process token user read failed: "
                f"{ctypes.WinError(error)}"
            )
        sid_pointer = ctypes.cast(
            token_user,
            ctypes.POINTER(wintypes.LPVOID),
        )[0]
        if not sid_pointer:
            raise InstallerError("Windows process token returned an invalid SID")
        string_sid = wintypes.LPWSTR()
        if not convert(sid_pointer, ctypes.byref(string_sid)):
            error = ctypes.get_last_error()
            raise InstallerError(
                "Windows process token SID conversion failed: "
                f"{ctypes.WinError(error)}"
            )
        try:
            sid = str(string_sid.value or "")
        finally:
            local_free(ctypes.cast(string_sid, wintypes.HLOCAL))
    except BaseException:
        close_handle(token)
        raise
    if not close_handle(token):
        error = ctypes.get_last_error()
        raise InstallerError(
            f"Windows process token close failed: {ctypes.WinError(error)}"
        )
    if not sid.startswith("S-"):
        raise InstallerError("Windows process token returned an invalid SID string")
    return sid


def _default_windows_runtime_root():
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return ntpath.join(local_app_data, "AgentMemoryBeacon", "runtime")
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    return ntpath.join(
        home,
        "AppData",
        "Local",
        "AgentMemoryBeacon",
        "runtime",
    )


def prepare_windows_runtime(
    *,
    config_path,
    runtime_root,
    source_python,
    dry_run=False,
    command_runner=None,
):
    """Build or verify one immutable manifest-owned Windows runtime release."""
    from install_runtime import (
        _durable_replace,
        _ensure_private_directory,
        _remove_tree,
        _validate_staged_runtime,
        build_windows_sync_release_plan,
        stage_runtime,
        verify_installed_release,
    )

    runtime_root = _ensure_private_directory(Path(runtime_root).expanduser())
    with portable_file_lock(
        runtime_root / ".install.lock",
        root=runtime_root,
    ):
        portable = dict(load_beacon_sync_config(config_path))
        cfg = {
            key: portable.pop(key)
            for key in (
                "transcript_paths",
                "codex_sessions_path",
                "claude_project_path",
            )
        }
        cfg["beacon_sync"] = portable
        cfg["python_path"] = str(source_python)
        source_root = Path(__file__).resolve().parent.parent
        plan = build_windows_sync_release_plan(
            source_root,
            runtime_root,
            cfg,
        )
        effective_plan = plan
        if not dry_run:
            if plan.install_root.exists():
                verification = verify_installed_release(plan.install_root)
                if verification["release_id"] != plan.release_id:
                    raise InstallerError(
                        "installed Windows runtime release identity changed"
                    )
            else:
                staged = stage_runtime(plan, command_runner=command_runner)
                effective_plan = getattr(staged, "final_plan", None) or plan
                destination = effective_plan.install_root
                stage_parent = staged.root.parent.lstat()
                stage_parent_identity = (stage_parent.st_dev, stage_parent.st_ino)
                staged_info = staged.root.lstat()
                staged_identity = (staged_info.st_dev, staged_info.st_ino)
                published_here = False
                try:
                    _validate_staged_runtime(plan, staged)
                    if destination.exists():
                        verification = verify_installed_release(destination)
                    else:
                        try:
                            _durable_replace(staged.root, destination)
                        finally:
                            if destination.exists():
                                published = destination.lstat()
                                published_here = (
                                    published.st_dev,
                                    published.st_ino,
                                ) == staged_identity
                        verification = verify_installed_release(destination)
                    if verification["release_id"] != effective_plan.release_id:
                        raise InstallerError(
                            "published Windows runtime release identity changed"
                        )
                except Exception as publish_error:
                    rollback_error = None
                    if published_here and destination.exists():
                        current = destination.lstat()
                        if (current.st_dev, current.st_ino) == staged_identity:
                            try:
                                _remove_tree(
                                    destination,
                                    expected_parent_identity=stage_parent_identity,
                                )
                            except Exception as exc:
                                rollback_error = exc
                    if rollback_error is not None:
                        raise InstallerError(
                            "Windows runtime publication failed and rollback failed: "
                            f"{rollback_error}"
                        ) from publish_error
                    raise
                finally:
                    if staged.root.exists():
                        current = staged.root.lstat()
                        if (current.st_dev, current.st_ino) == staged_identity:
                            _remove_tree(
                                staged.root,
                                expected_parent_identity=stage_parent_identity,
                            )
    return {
        "release_id": effective_plan.release_id,
        "python_path": str(_windows_runtime_python(effective_plan.install_root)),
        "script_path": str(effective_plan.install_root / "scripts" / "beacon_sync.py"),
        "config_path": str(effective_plan.install_root / "scripts" / "config.yaml"),
        "manifest_path": str(effective_plan.install_root / "release-manifest.json"),
        "dry_run": bool(dry_run),
    }


def _windows_runtime_python(runtime_root):
    return Path(runtime_root) / ".venv" / "Scripts" / "python.exe"


def build_parser():
    parser = argparse.ArgumentParser(
        description="Install Agent Memory Beacon synchronization only"
    )
    parser.add_argument("--config", default=CONFIG_PATH)
    parser.add_argument("--python", dest="python_path", default=sys.executable)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--uninstall", action="store_true")
    parser.add_argument("--codex-hooks", action="store_true")
    parser.add_argument("--claude-hooks", action="store_true")
    parser.add_argument("--interval", type=int)
    parser.add_argument(
        "--runtime-root",
        default=_default_windows_runtime_root(),
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    sync_cfg = load_beacon_sync_config(args.config)
    if not sync_cfg.get("enabled") and not args.uninstall:
        raise InstallerError("beacon_sync must be enabled before installation")
    script_path = Path(__file__).resolve().parent / "beacon_sync.py"
    results = []
    if sys.platform == "darwin":
        if sync_cfg.get("role") != "authority" and not args.uninstall:
            raise InstallerError("macOS scheduler requires authority role")
        full_cfg = load_config(args.config)
        log_dir = Path(full_cfg["vault_path"]) / "04-Feedback" / "_logs"
        results.append(
            install_macos_scheduler(
                python_path=args.python_path,
                script_path=script_path,
                config_path=args.config,
                log_dir=log_dir,
                interval_seconds=args.interval or 60,
                dry_run=args.dry_run,
                uninstall=args.uninstall,
            )
        )
    elif os.name == "nt":
        if sync_cfg.get("role") != "producer-replica" and not args.uninstall:
            raise InstallerError("Windows scheduler requires producer-replica role")
        if not args.uninstall:
            try:
                _assert_supported_windows_atomic_filesystem()
            except ProtocolError as exc:
                raise InstallerError(str(exc)) from exc
        home = Path.home()
        hook_paths = []
        if args.codex_hooks:
            hook_paths.append(home / ".codex" / "hooks.json")
        if args.claude_hooks:
            hook_paths.append(home / ".claude" / "settings.json")
        runtime = None
        if not args.uninstall:
            runtime = prepare_windows_runtime(
                config_path=args.config,
                runtime_root=args.runtime_root,
                source_python=args.python_path,
                dry_run=args.dry_run,
            )
        python_path = (
            runtime["python_path"] if runtime is not None else args.python_path
        )
        sync_script_path = (
            runtime["script_path"] if runtime is not None else script_path
        )
        config_path = (
            runtime["config_path"] if runtime is not None else args.config
        )
        results.extend(
            install_windows_components(
                python_path=python_path,
                script_path=sync_script_path,
                config_path=config_path,
                user_id=_current_windows_user(),
                hook_paths=hook_paths,
                interval_minutes=args.interval or 1,
                dry_run=args.dry_run,
                uninstall=args.uninstall,
            )
        )
        if runtime is not None:
            results.insert(0, {"runtime": runtime})
    else:
        raise InstallerError("sync scheduler supports macOS and Windows only")
    print(json.dumps({"actions": results}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
