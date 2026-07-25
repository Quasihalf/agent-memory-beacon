#!/usr/bin/env python3
"""Install macOS launchd jobs for harvesting and weekly maintenance."""
import argparse
import os
import plistlib
import stat
import subprocess
from pathlib import Path

from branding import LEGACY_LAUNCHD_LABELS, NEW_LAUNCHD_LABELS
from config import load_config
from safety import durable_atomic_write, durable_unlink
from session_harvester import initialize_harvest_baseline


SCRIPT_DIR = Path(__file__).resolve().parent
LAUNCHCTL = "/bin/launchctl"
HARVEST_LABEL = NEW_LAUNCHD_LABELS["harvest"]
WEEKLY_LABEL = NEW_LAUNCHD_LABELS["weekly"]
WEEKDAYS = {
    "SUN": 0,
    "MON": 1,
    "TUE": 2,
    "WED": 3,
    "THU": 4,
    "FRI": 5,
    "SAT": 6,
}


def build_harvest_plist(cfg, scripts_dir=None):
    scripts_dir = _scripts_dir(scripts_dir)
    interval = int(cfg.get("harvest_interval_seconds", 300))
    if interval < 60:
        raise ValueError("harvest_interval_seconds must be at least 60")
    return base_plist(
        cfg,
        HARVEST_LABEL,
        [
            python_path(cfg),
            str(scripts_dir / "session_harvester.py"),
            "--mode",
            "start",
            "--skip-scanner",
            "--skip-profile-check",
        ],
        {
            "StartInterval": interval,
            "RunAtLoad": True,
        },
        scripts_dir=scripts_dir,
        process_type="Standard",
    )


def build_weekly_plist(cfg, scripts_dir=None):
    scripts_dir = _scripts_dir(scripts_dir)
    scan = cfg.get("scan") or {}
    day = str(scan.get("day", "SUN")).upper()
    if day not in WEEKDAYS:
        raise ValueError(f"unsupported scan day: {day}")
    hour = int(scan.get("hour", 15))
    minute = int(scan.get("minute", 0))
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("scan hour/minute is outside the valid range")
    return base_plist(
        cfg,
        WEEKLY_LABEL,
        [
            python_path(cfg),
            str(scripts_dir / "runner.py"),
        ],
        {
            "StartCalendarInterval": {
                "Weekday": WEEKDAYS[day],
                "Hour": hour,
                "Minute": minute,
            },
        },
        scripts_dir=scripts_dir,
        process_type="Background",
    )


def base_plist(
    cfg,
    label,
    arguments,
    schedule,
    scripts_dir=None,
    process_type="Background",
):
    scripts_dir = _scripts_dir(scripts_dir)
    log_dir = Path(cfg["vault_path"]).expanduser() / "04-Feedback" / "_logs"
    payload = {
        "Label": label,
        "ProgramArguments": arguments,
        "WorkingDirectory": str(scripts_dir),
        "ProcessType": process_type,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(log_dir / f"{label}.log"),
        "StandardErrorPath": str(log_dir / f"{label}.error.log"),
    }
    payload.update(schedule)
    return payload


def _scripts_dir(value):
    if value is None:
        return SCRIPT_DIR
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def python_path(cfg):
    return os.path.expanduser(str(cfg.get("python_path") or "python3"))


def job_paths(home, kind):
    launch_agents = Path(home or Path.home()).expanduser() / "Library" / "LaunchAgents"
    return (
        launch_agents / f"{NEW_LAUNCHD_LABELS[kind]}.plist",
        launch_agents / f"{LEGACY_LAUNCHD_LABELS[kind]}.plist",
    )


def _run(command_runner, args, timeout=15):
    runner = command_runner or subprocess.run
    return runner(
        args,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _result_message(result):
    return result.stderr.strip() or result.stdout.strip()


def _is_missing_service_result(result):
    message = _result_message(result).lower()
    return result.returncode != 0 and any(
        marker in message
        for marker in (
            "no such process",
            "no such service",
            "could not find service",
            "service is not loaded",
            "service not loaded",
        )
    )


def _command_failure(result):
    return _result_message(result) or f"exit {result.returncode}"


def _remove_temp_file(path):
    try:
        path.unlink(missing_ok=True)
    except Exception as exc:
        return f"temporary cleanup failed for {path}: {exc}"
    return None


def _rollback_unload(item, command_runner=None):
    domain = f"gui/{os.getuid()}"
    path_error = None
    try:
        result = _run(
            command_runner,
            [LAUNCHCTL, "bootout", domain, str(item["path"])],
        )
        if result.returncode == 0 or _is_missing_service_result(result):
            return None
        path_error = _command_failure(result)
    except Exception as exc:
        path_error = str(exc)

    try:
        result = _run(
            command_runner,
            [LAUNCHCTL, "bootout", f"{domain}/{item['label']}"]
        )
        if result.returncode == 0 or _is_missing_service_result(result):
            return None
        fallback_error = _command_failure(result)
    except Exception as exc:
        fallback_error = str(exc)
    return (
        f"rollback unload {item['kind']} {item['path']}: {path_error}; "
        f"label fallback {item['label']}: {fallback_error}"
    )


def _restore_original_plists(jobs, originals, reload_originals, command_runner):
    errors = []
    for item in jobs:
        path = item["path"]
        original = originals[path]
        if original is None:
            try:
                if os.path.lexists(path):
                    durable_unlink(path)
            except Exception as exc:
                errors.append(f"restore {item['kind']} {path}: unlink failed: {exc}")
            continue

        try:
            durable_atomic_write(path, original, mode=0o644)
        except Exception as exc:
            errors.append(f"restore {item['kind']} {path}: write failed: {exc}")
            continue
        if reload_originals.get(path, False):
            try:
                load_and_verify(path, item["label"], command_runner)
            except Exception as exc:
                errors.append(f"reload {item['kind']} {path}: {exc}")
    return errors


def _service_loaded(label, command_runner=None):
    domain = f"gui/{os.getuid()}"
    result = _run(command_runner, [LAUNCHCTL, "print", f"{domain}/{label}"])
    if result.returncode == 0:
        return True
    if _is_missing_service_result(result):
        return False
    raise RuntimeError(
        f"launchctl state query failed for {label}: {_command_failure(result)}"
    )


def validate_program(payload, command_runner=None):
    arguments = list(payload["ProgramArguments"])
    probe = arguments[:2] + ["--help"]
    result = _run(command_runner, probe)
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"launchd program validation failed: {message}")


def load_and_verify(path, label, command_runner=None):
    domain = f"gui/{os.getuid()}"
    _run(command_runner, [LAUNCHCTL, "bootout", domain, str(path)])
    result = _run(command_runner, [LAUNCHCTL, "bootstrap", domain, str(path)])
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"launchctl bootstrap failed for {path}: {message}")
    result = _run(command_runner, [LAUNCHCTL, "print", f"{domain}/{label}"])
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"launchctl print failed for {label}: {message}")


def remove_legacy_job(path, label, command_runner=None):
    path = Path(path)
    original_identity = None
    parent_identity = None
    if os.path.lexists(path):
        current = path.lstat()
        if not stat.S_ISREG(current.st_mode):
            raise OSError(f"legacy plist is not a regular file: {path}")
        parent = path.parent.stat(follow_symlinks=False)
        if not stat.S_ISDIR(parent.st_mode):
            raise OSError(f"legacy plist parent is not a directory: {path.parent}")
        original_identity = (current.st_dev, current.st_ino)
        parent_identity = (parent.st_dev, parent.st_ino)
    domain = f"gui/{os.getuid()}"
    result = _run(command_runner, [LAUNCHCTL, "bootout", domain, str(path)])
    if result.returncode != 0 and not _is_missing_service_result(result):
        message = _result_message(result)
        raise RuntimeError(f"launchctl bootout failed for legacy {label}: {message}")
    if original_identity is not None:
        durable_unlink(
            path,
            expected_identity=original_identity,
            expected_parent_identity=parent_identity,
        )
    return f"REMOVED legacy {label}"


def selected_jobs(
    cfg,
    home,
    include_harvest=True,
    include_weekly=True,
    scripts_dir=None,
):
    specs = []
    if include_harvest:
        specs.append(("harvest", build_harvest_plist(cfg, scripts_dir=scripts_dir)))
    if include_weekly:
        specs.append(("weekly", build_weekly_plist(cfg, scripts_dir=scripts_dir)))

    jobs = []
    for kind, payload in specs:
        path, legacy_path = job_paths(home, kind)
        jobs.append(
            {
                "kind": kind,
                "path": path,
                "legacy_path": legacy_path,
                "label": NEW_LAUNCHD_LABELS[kind],
                "legacy_label": LEGACY_LAUNCHD_LABELS[kind],
                "payload": payload,
            }
        )
    return jobs


def install_launch_agents(
    cfg,
    home=None,
    include_harvest=True,
    include_weekly=True,
    dry_run=False,
    load=True,
    command_runner=None,
    scripts_dir=None,
    initialize_baseline=True,
):
    jobs = selected_jobs(
        cfg,
        home,
        include_harvest,
        include_weekly,
        scripts_dir=scripts_dir,
    )

    actions = []
    if dry_run:
        for item in jobs:
            actions.append(f"DRY-RUN would write {item['path']}")
            if item["legacy_path"].exists() and load:
                actions.append(
                    f"DRY-RUN would remove legacy {item['legacy_label']} "
                    "after validation"
                )
        return actions

    if load:
        for item in jobs:
            validate_program(item["payload"], command_runner)

    originals = {
        item["path"]: item["path"].read_bytes() if item["path"].exists() else None
        for item in jobs
    }
    loaded_before = {
        item["path"]: (
            _service_loaded(item["label"], command_runner)
            if load and originals[item["path"]] is not None
            else False
        )
        for item in jobs
    }

    harvest = next((item for item in jobs if item["kind"] == "harvest"), None)
    if (
        harvest is not None
        and not harvest["path"].exists()
        and not harvest["legacy_path"].exists()
        and initialize_baseline
    ):
        baseline_count = initialize_harvest_baseline(cfg)
        actions.append(f"BASELINE existing transcripts: {baseline_count}")

    attempted = []
    try:
        for item in jobs:
            item["path"].parent.mkdir(parents=True, exist_ok=True)
            Path(item["payload"]["StandardOutPath"]).parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            write_plist_atomic(item["path"], item["payload"])
            actions.append(f"WROTE {item['path']}")
        if load:
            for item in jobs:
                attempted.append(item)
                load_and_verify(item["path"], item["label"], command_runner)
                actions.append(f"VERIFIED {item['label']}")
    except Exception as exc:
        rollback_errors = []
        for item in reversed(attempted):
            error = _rollback_unload(item, command_runner)
            if error is not None:
                rollback_errors.append(error)
        rollback_errors.extend(
            _restore_original_plists(
                jobs,
                originals,
                {
                    item["path"]: loaded_before[item["path"]]
                    for item in attempted
                },
                command_runner,
            )
        )
        if rollback_errors:
            details = "; ".join(rollback_errors)
            raise RuntimeError(
                f"installation rollback incomplete: original installation failure: {exc}; "
                f"{details}"
            ) from exc
        raise

    if not load:
        return actions

    cleanup_errors = []
    for item in jobs:
        if not item["legacy_path"].exists():
            continue
        try:
            actions.append(
                remove_legacy_job(
                    item["legacy_path"],
                    item["legacy_label"],
                    command_runner,
                )
            )
        except Exception as exc:
            cleanup_errors.append(f"{item['legacy_label']}: {exc}")
    if cleanup_errors:
        raise RuntimeError(f"legacy cleanup failed: {'; '.join(cleanup_errors)}")
    return actions


def write_plist_atomic(path, payload):
    durable_atomic_write(
        path,
        plistlib.dumps(payload, sort_keys=True),
        mode=0o644,
    )


def main():
    parser = argparse.ArgumentParser(description="Install Agent Memory Beacon launchd jobs")
    parser.add_argument("--harvest-only", action="store_true")
    parser.add_argument("--weekly-only", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-load", action="store_true")
    args = parser.parse_args()
    if args.harvest_only and args.weekly_only:
        parser.error("--harvest-only and --weekly-only are mutually exclusive")

    cfg = load_config()
    actions = install_launch_agents(
        cfg,
        include_harvest=not args.weekly_only,
        include_weekly=not args.harvest_only,
        dry_run=args.dry_run,
        load=not args.no_load,
    )
    for action in actions:
        print(action)


if __name__ == "__main__":
    main()
