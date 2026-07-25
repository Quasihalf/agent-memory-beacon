#!/usr/bin/env python3
"""Read-only health profiles for Agent Memory Beacon."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import plistlib
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from branding import LEGACY_LAUNCHD_LABELS
from memory_recall import validate_recall_index
from memory_schema import is_valid_runtime_record


PROFILE_NAMES = ("quick", "ci", "live")
DEFAULT_RUNTIME_ROOT = "~/.local/share/agent-memory-beacon/runtime"
OWNED_HOOK_MARKER = "AGENT_MEMORY_BEACON_HOOK=1"
LAUNCHD_LABELS = (
    "io.agent-memory-beacon.harvest",
    "io.agent-memory-beacon.weekly",
)
LAUNCHD_SCRIPTS = {
    "io.agent-memory-beacon.harvest": "session_harvester.py",
    "io.agent-memory-beacon.weekly": "runner.py",
}
PYTHON_COMPILE_CODE = (
    "import pathlib,sys;"
    "[compile(pathlib.Path(p).read_bytes(),p,'exec') for p in sys.argv[1:]]"
)
IMPORT_PROBE_CODE = (
    "import sys;sys.dont_write_bytecode=True;"
    "sys.path.insert(0,sys.argv[1]);"
    "import config,insight_memory,memory_schema,memory_recall,memory_runtime,"
    "memory_identity_repair,memory_lifecycle,session_harvester"
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    required: bool
    passed: bool
    details: str


@dataclass(frozen=True)
class DoctorReport:
    profile: str
    checks: tuple[DoctorCheck, ...]

    @property
    def passed(self):
        return all(check.passed for check in self.checks if check.required)

    def as_dict(self):
        return {
            "profile": self.profile,
            "status": "pass" if self.passed else "fail",
            "checks": [asdict(check) for check in self.checks],
        }


def run_profile(profile, *, repo_root, cfg=None, runner=None):
    """Run one deterministic, read-only doctor profile."""
    profile = str(profile or "").strip().lower()
    if profile not in PROFILE_NAMES:
        raise ValueError(f"unsupported doctor profile: {profile}")
    repo_root = os.path.abspath(os.path.expanduser(os.fspath(repo_root)))
    if cfg is None:
        from config import load_config

        cfg = load_config()
    runner = runner or _subprocess_runner

    checks = [
        _configuration_check(cfg, repo_root),
        _recall_index_schema_check(cfg),
        _command_check(
            "script-compilation",
            _script_compilation_command(repo_root),
            repo_root,
            60,
            runner,
        ),
        _command_check(
            "module-imports",
            (
                sys.executable,
                "-B",
                "-c",
                IMPORT_PROBE_CODE,
                os.path.join(repo_root, "scripts"),
            ),
            repo_root,
            30,
            runner,
        ),
    ]

    if profile == "ci":
        source_check = _ci_source_checkout_check(repo_root)
        if not source_check.passed:
            checks.append(source_check)
            return DoctorReport(profile=profile, checks=tuple(checks))
        checks.extend(_ci_checks(repo_root, runner))
    elif profile == "live":
        checks.extend(_live_checks(repo_root, cfg, runner))
    return DoctorReport(profile=profile, checks=tuple(checks))


def _ci_source_checkout_check(repo_root):
    required = (
        os.path.join(repo_root, "tests"),
        os.path.join(repo_root, "tests", "fixtures", "memory_runtime"),
    )
    missing = [path for path in required if not os.path.isdir(path)]
    return DoctorCheck(
        name="source-checkout",
        required=True,
        passed=not missing,
        details=(
            "ok"
            if not missing
            else "ci profile requires a source checkout with tests and fixtures"
        ),
    )


def _configuration_check(cfg, repo_root):
    errors = []
    if not isinstance(cfg, dict):
        errors.append("configuration is not a mapping")
    else:
        vault = _expanded(cfg.get("vault_path"))
        if not vault or not os.path.isabs(vault) or not os.path.isdir(vault):
            errors.append("vault_path is not an existing absolute directory")
    if not os.path.isdir(repo_root):
        errors.append("repository root does not exist")
    return DoctorCheck(
        name="configuration",
        required=True,
        passed=not errors,
        details="ok" if not errors else "; ".join(errors),
    )


def _recall_index_schema_check(cfg):
    try:
        index_path = _recall_index_path(cfg)
        if os.path.islink(index_path) or not os.path.isfile(index_path):
            raise ValueError(f"recall index is not a regular file: {index_path}")
        with open(index_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        validate_recall_index(payload)
        invalid = []
        seen = set()
        for index, unit in enumerate(payload["units"]):
            unit_id = (
                str(unit.get("id") or "").strip()
                if isinstance(unit, dict)
                else ""
            )
            label = unit_id or f"unit[{index}]"
            if not is_valid_runtime_record(unit):
                invalid.append(label)
                continue
            if unit_id in seen:
                invalid.append(f"duplicate:{unit_id}")
            seen.add(unit_id)
        if invalid:
            raise ValueError(
                "invalid recall runtime units: " + ", ".join(invalid[:10])
            )
        return DoctorCheck(
            "recall-index-schema",
            True,
            True,
            f"ok ({len(payload['units'])} runtime units)",
        )
    except Exception as exc:
        return DoctorCheck("recall-index-schema", True, False, str(exc))


def _script_compilation_command(repo_root):
    scripts_dir = os.path.join(repo_root, "scripts")
    scripts = tuple(
        str(path)
        for path in sorted(Path(scripts_dir).glob("*.py"))
        if path.is_file()
    )
    return (sys.executable, "-B", "-c", PYTHON_COMPILE_CODE, *scripts)


def _ci_checks(repo_root, runner):
    python = sys.executable
    return [
        _command_check(
            "unit-tests",
            (
                python,
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                os.path.join(repo_root, "tests"),
            ),
            repo_root,
            600,
            runner,
        ),
        _command_check(
            "runtime-evaluation",
            (
                python,
                "-B",
                os.path.join(repo_root, "scripts", "evaluate_memory_runtime.py"),
                "--fixtures",
                os.path.join(repo_root, "tests", "fixtures", "memory_runtime"),
            ),
            repo_root,
            120,
            runner,
        ),
        _command_check(
            "git-diff-check",
            (_git_binary(), "diff", "--check"),
            repo_root,
            60,
            runner,
        ),
    ]


def _live_checks(repo_root, cfg, runner):
    vault = _expanded(cfg.get("vault_path"))
    python = sys.executable
    runtime_root = _runtime_root(cfg)
    checks = [
        _command_check(
            "frontmatter",
            (
                python,
                "-B",
                os.path.join(repo_root, "scripts", "validate_frontmatter.py"),
                vault,
            ),
            repo_root,
            120,
            runner,
        ),
        _command_check(
            "wikilinks",
            (
                python,
                "-B",
                os.path.join(repo_root, "scripts", "link_validator.py"),
                vault,
            ),
            repo_root,
            120,
            runner,
        ),
        _candidate_isolation_check(cfg),
        _codex_hooks_check(cfg, runtime_root),
        _launchd_plists_check(cfg, runtime_root),
        _launchd_services_check(cfg, repo_root, runtime_root, runner),
        _prompt_hook_probe(repo_root, runtime_root, runner),
    ]
    return checks


def _candidate_isolation_check(cfg):
    index_path = _recall_index_path(cfg)
    try:
        with open(index_path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        units = payload.get("units")
        if not isinstance(units, list):
            raise ValueError("recall index units must be a list")
        forbidden = _candidate_roots(cfg)
        leaks = []
        for unit in units:
            if not isinstance(unit, dict):
                leaks.append("<invalid-unit>")
                continue
            haystack = " ".join(
                [
                    str(unit.get("path") or ""),
                    str(unit.get("source_note") or ""),
                    *(str(item) for item in unit.get("source_refs") or []),
                ]
            ).replace("\\", "/").casefold()
            if unit.get("status") not in (None, "active") or any(
                root in "/" + haystack.lstrip("/") for root in forbidden
            ):
                leaks.append(str(unit.get("id") or "<missing-id>"))
        if leaks:
            raise ValueError("candidate recall leakage: " + ", ".join(leaks[:10]))
        details = f"ok ({len(units)} runtime units)"
        return DoctorCheck("candidate-isolation", True, True, details)
    except Exception as exc:
        return DoctorCheck("candidate-isolation", True, False, str(exc))


def _candidate_roots(cfg):
    values = [
        (cfg.get("personal_memory") or {}).get(
            "candidate_dir", "04-Feedback/_memory-candidates"
        ),
        (cfg.get("skill_preferences") or {}).get(
            "candidate_dir", "04-Feedback/_skill-preferences"
        ),
        (cfg.get("workflow_memory") or {}).get(
            "candidate_dir", "04-Feedback/_workflow-candidates"
        ),
        (cfg.get("insight_memory") or {}).get(
            "candidate_dir", "04-Feedback/_insight-candidates"
        ),
        (cfg.get("error_evidence") or {}).get(
            "candidate_dir", "04-Feedback/_error-candidates"
        ),
        (cfg.get("annotation_quality") or {}).get(
            "candidate_dir", "04-Feedback/_annotation-candidates"
        ),
        (cfg.get("memory_lifecycle") or {}).get(
            "proposal_dir", "04-Feedback/_lifecycle-proposals"
        ),
        (cfg.get("memory_promotion") or {}).get(
            "proposal_dir", "04-Feedback/_promotion-proposals"
        ),
        "04-Feedback/_raw-sessions",
    ]
    return tuple(
        "/" + str(value).replace("\\", "/").strip("/").casefold() + "/"
        for value in values
        if value
    )


def _recall_index_path(cfg):
    vault = _expanded(cfg.get("vault_path"))
    if not vault or not os.path.isabs(vault):
        raise ValueError("vault_path is not an absolute path")
    runtime = cfg.get("memory_runtime") or {}
    value = runtime.get("resolved_index_path") or runtime.get("index_path")
    raw = os.path.expandvars(
        os.path.expanduser(str(value or "05-Agent-Memory/recall-index.json"))
    )
    if os.path.isabs(raw):
        return os.path.abspath(raw)
    return os.path.abspath(os.path.join(vault, raw))


def _codex_hooks_check(cfg, runtime_root):
    hooks_path = os.path.join(
        _expanded(cfg.get("codex_home") or "~/.codex"),
        "hooks.json",
    )
    expected = {
        "SessionStart": ("session_harvester.py", ("--mode", "start"), 120),
        "UserPromptSubmit": ("codex_prompt_hook.py", (), 2),
        "Stop": (
            "session_harvester.py",
            ("--mode", "stop", "--agent", "codex"),
            120,
        ),
    }
    errors = []
    try:
        with open(hooks_path, "r", encoding="utf-8") as handle:
            hooks = json.load(handle)
        events = hooks.get("hooks")
        if not isinstance(events, dict):
            raise ValueError("hooks.json has no hooks mapping")
        for event, (script_name, arguments, expected_timeout) in expected.items():
            owned = []
            for group in events.get(event, []):
                if not isinstance(group, dict):
                    continue
                for hook in group.get("hooks", []):
                    command = str(hook.get("command") or "") if isinstance(hook, dict) else ""
                    if OWNED_HOOK_MARKER in command:
                        owned.append((hook, command))
            if len(owned) != 1:
                errors.append(f"{event} has {len(owned)} owned hooks")
                continue
            hook, command = owned[0]
            try:
                tokens = shlex.split(command)
            except ValueError:
                errors.append(f"{event} command is malformed")
                continue
            if len(tokens) < 3 or tokens[0] != OWNED_HOOK_MARKER:
                errors.append(f"{event} command is malformed")
                continue
            python_path, script_path = tokens[1:3]
            for path in (python_path, script_path):
                if not _path_owned_by(path, runtime_root):
                    errors.append(f"{event} path outside stable runtime: {path}")
            if os.path.basename(script_path) != script_name:
                errors.append(f"{event} uses unexpected script: {script_path}")
            expected_python = os.path.join(runtime_root, ".venv", "bin", "python")
            expected_script = os.path.join(runtime_root, "scripts", script_name)
            exact_command = (
                len(tokens) == 3 + len(arguments)
                and tokens[3:] == list(arguments)
                and os.path.realpath(python_path) == os.path.realpath(expected_python)
                and os.path.realpath(script_path) == os.path.realpath(expected_script)
            )
            if not exact_command:
                errors.append(f"{event} does not use the exact command")
            if hook.get("type") != "command":
                errors.append(f"{event} hook type is not command")
            if hook.get("timeout") != expected_timeout:
                errors.append(f"{event} timeout is not {expected_timeout}")
    except Exception as exc:
        errors.append(str(exc))
    return DoctorCheck(
        "codex-hooks",
        True,
        not errors,
        "ok" if not errors else "; ".join(errors),
    )


def _launchd_plists_check(cfg, runtime_root):
    directory = _launch_agents_directory(cfg)
    errors = []
    for label in LAUNCHD_LABELS:
        path = os.path.join(directory, label + ".plist")
        try:
            if os.path.islink(path):
                raise ValueError("plist is a symlink")
            with open(path, "rb") as handle:
                payload = plistlib.load(handle)
            if payload.get("Label") != label:
                errors.append(f"{label} has the wrong Label")
            arguments = payload.get("ProgramArguments")
            if not isinstance(arguments, list) or len(arguments) < 2:
                errors.append(f"{label} has invalid ProgramArguments")
                continue
            for value in arguments[:2]:
                if not _path_owned_by(value, runtime_root):
                    errors.append(f"{label} path outside stable runtime: {value}")
            expected_script = LAUNCHD_SCRIPTS[label]
            if os.path.basename(arguments[1]) != expected_script:
                errors.append(
                    f"{label} uses unexpected script: {arguments[1]}"
                )
            if label == "io.agent-memory-beacon.weekly" and "--full" in arguments[2:]:
                errors.append(f"{label} must not schedule --full scans")
            working = payload.get("WorkingDirectory")
            if not _path_owned_by(working, runtime_root, require_file=False):
                errors.append(
                    f"{label} WorkingDirectory outside stable runtime: {working}"
                )
        except Exception as exc:
            errors.append(f"{label}: {exc}")
    return DoctorCheck(
        "launchd-plists",
        True,
        not errors,
        "ok" if not errors else "; ".join(errors),
    )


def _launch_agents_directory(cfg):
    configured = cfg.get("launch_agents_dir")
    if configured:
        return _expanded(configured)
    home = _expanded(cfg.get("user_home") or "~")
    return os.path.join(home, "Library", "LaunchAgents")


def _launchd_services_check(cfg, repo_root, runtime_root, runner):
    errors = []
    domain = f"gui/{os.getuid()}"
    for label in LAUNCHD_LABELS:
        result = _invoke_runner(
            runner,
            ("/bin/launchctl", "print", f"{domain}/{label}"),
            cwd=repo_root,
            timeout=15,
        )
        if isinstance(result, DoctorCheck):
            errors.append(f"{label}: {result.details}")
        elif result.returncode != 0:
            errors.append(f"{label}: {_command_details(result)}")
        else:
            expected_python = os.path.join(runtime_root, ".venv", "bin", "python")
            expected_script = os.path.join(
                runtime_root,
                "scripts",
                LAUNCHD_SCRIPTS[label],
            )
            program, loaded_paths = _launchd_loaded_paths(result.stdout)
            if (
                not program
                or os.path.realpath(program) != os.path.realpath(expected_python)
                or not _path_owned_by(program, runtime_root)
            ):
                errors.append(f"{label}: loaded program is outside stable runtime")
            if not any(
                os.path.realpath(path) == os.path.realpath(expected_script)
                for path in loaded_paths
            ):
                errors.append(f"{label}: loaded script is outside stable runtime")
    for label in LEGACY_LAUNCHD_LABELS.values():
        result = _invoke_runner(
            runner,
            ("/bin/launchctl", "print", f"{domain}/{label}"),
            cwd=repo_root,
            timeout=15,
        )
        if isinstance(result, DoctorCheck):
            errors.append(f"{label}: {result.details}")
        elif result.returncode == 0:
            errors.append(f"{label}: legacy service is still loaded")
        elif not _missing_launchd_service(result):
            errors.append(f"{label}: {_command_details(result)}")
    return DoctorCheck(
        "launchd-services",
        True,
        not errors,
        "ok" if not errors else "; ".join(errors),
    )


def _launchd_loaded_paths(output):
    program = ""
    paths = []
    for raw_line in str(output or "").splitlines():
        line = raw_line.strip()
        key = ""
        value = line
        if "=" in line:
            key, value = (part.strip() for part in line.split("=", 1))
        value = value.strip().strip('"')
        if not os.path.isabs(value):
            continue
        value = os.path.abspath(value)
        paths.append(value)
        if key == "program":
            program = value
    return program, tuple(paths)


def _missing_launchd_service(result):
    if result.returncode == 0:
        return False
    detail = _command_details(result).lower()
    return any(
        marker in detail
        for marker in (
            "no such process",
            "no such service",
            "could not find service",
            "service is not loaded",
            "service not loaded",
        )
    )


def _prompt_hook_probe(repo_root, runtime_root, runner):
    python_path = os.path.join(runtime_root, ".venv", "bin", "python")
    script_path = os.path.join(runtime_root, "scripts", "codex_prompt_hook.py")
    if not all(_path_owned_by(path, runtime_root) for path in (python_path, script_path)):
        return DoctorCheck(
            "prompt-hook-probe", True, False, "runtime prompt hook is unavailable"
        )
    result = _invoke_runner(
        runner,
        (python_path, script_path),
        cwd=repo_root,
        timeout=5,
        input_text='{"hook_event_name":"DoctorProbe"}\n',
    )
    if isinstance(result, DoctorCheck):
        return DoctorCheck("prompt-hook-probe", True, False, result.details)
    try:
        payload = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError):
        payload = None
    passed = result.returncode == 0 and payload == {}
    return DoctorCheck(
        "prompt-hook-probe",
        True,
        passed,
        "ok" if passed else _command_details(result) or "invalid hook JSON",
    )


def _command_check(name, args, cwd, timeout, runner, input_text=None):
    result = _invoke_runner(
        runner,
        args,
        cwd=cwd,
        timeout=timeout,
        input_text=input_text,
    )
    if isinstance(result, DoctorCheck):
        return DoctorCheck(name, True, False, result.details)
    passed = result.returncode == 0
    return DoctorCheck(
        name=name,
        required=True,
        passed=passed,
        details="ok" if passed else _command_details(result),
    )


def _invoke_runner(runner, args, *, cwd, timeout, input_text=None):
    if isinstance(args, str) or not isinstance(args, (tuple, list)):
        raise TypeError("doctor commands must be argument sequences")
    command = tuple(os.fspath(arg) for arg in args)
    try:
        value = runner(
            command,
            cwd=cwd,
            timeout=timeout,
            input_text=input_text,
        )
    except subprocess.TimeoutExpired:
        return DoctorCheck("command", True, False, f"timeout after {timeout}s")
    except Exception as exc:
        return DoctorCheck("command", True, False, f"runner error: {exc}")
    if isinstance(value, CommandResult):
        return value
    return CommandResult(
        returncode=int(value.returncode),
        stdout=str(value.stdout or ""),
        stderr=str(value.stderr or ""),
    )


def _subprocess_runner(args, *, cwd, timeout, input_text=None):
    completed = subprocess.run(
        list(args),
        cwd=cwd,
        timeout=timeout,
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
        shell=False,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def _command_details(result):
    detail = str(result.stderr or result.stdout or "").strip()
    return detail[:1000] or f"exit {result.returncode}"


def _runtime_root(cfg):
    value = cfg.get("runtime_root") or cfg.get("runtime_install_root")
    return os.path.realpath(_expanded(value or DEFAULT_RUNTIME_ROOT))


def _path_owned_by(path, root, require_file=True):
    if not isinstance(path, (str, os.PathLike)) or not path:
        return False
    candidate = os.path.realpath(_expanded(path))
    root = os.path.realpath(_expanded(root))
    try:
        inside = os.path.commonpath([candidate, root]) == root
    except ValueError:
        return False
    if not inside or candidate == root:
        return False
    return os.path.isfile(candidate) if require_file else os.path.isdir(candidate)


def _expanded(value):
    return os.path.abspath(
        os.path.expandvars(os.path.expanduser(os.fspath(value or "")))
    ) if value else ""


def _git_binary():
    return "/usr/bin/git" if os.path.isfile("/usr/bin/git") else "git"


def render_report(report, json_output=False):
    if json_output:
        return json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    lines = [
        f"Doctor {report.profile}: {'通过' if report.passed else '失败'}"
    ]
    for check in report.checks:
        marker = "PASS" if check.passed else "FAIL"
        lines.append(f"[{marker}] {check.name}: {check.details}")
    return "\n".join(lines)


def main(argv=None, *, config_loader=None, runner=None):
    parser = argparse.ArgumentParser(description="Agent Memory Beacon health checks")
    parser.add_argument("--profile", choices=PROFILE_NAMES, default="quick")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent.parent))
    args = parser.parse_args(argv)
    if config_loader is None:
        from config import load_config

        config_loader = load_config
    try:
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            cfg = config_loader()
        report = run_profile(
            args.profile,
            repo_root=args.repo_root,
            cfg=cfg,
            runner=runner,
        )
    except Exception as exc:
        report = DoctorReport(
            profile=args.profile,
            checks=(
                DoctorCheck(
                    "configuration",
                    True,
                    False,
                    f"{type(exc).__name__}: {exc}",
                ),
            ),
        )
    print(render_report(report, json_output=args.json))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
