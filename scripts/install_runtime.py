#!/usr/bin/env python3
"""Stage and transactionally install the stable Agent Memory Beacon runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import ntpath
import os
import plistlib
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml

from branding import LEGACY_LAUNCHD_LABELS, NEW_LAUNCHD_LABELS
from codex_profile_sync import SHARED_AGENTS
from install_claude import (
    install_claude_patch,
    install_hooks as install_claude_hooks,
)
from install_codex import install_agents_patch, install_hooks
from install_beacon_sync import (
    LAUNCHD_LABEL as SYNC_LAUNCHD_LABEL,
    install_macos_scheduler,
)
from safety import (
    durable_atomic_write,
    durable_rmtree,
    durable_unlink,
    ensure_directory_tree,
    exclusive_file_lock,
    safe_vault_path,
)


def install_launch_agents(*args, **kwargs):
    from install_launchd import install_launch_agents as implementation

    return implementation(*args, **kwargs)


DEFAULT_INSTALL_ROOT = Path("~/.local/share/agent-memory-beacon/runtime").expanduser()
MINIMUM_PYTHON = (3, 11)
MANIFEST_SCHEMA_VERSION = 3
ROLLBACK_SCHEMA_VERSION = 3
SUPPORTED_ROLLBACK_SCHEMA_VERSIONS = frozenset({1, 2, ROLLBACK_SCHEMA_VERSION})
WINDOWS_RELEASE_IDENTITY_ROOT = "_release-identity"
WINDOWS_RUNTIME_PYTHON_PATH = ".venv/Scripts/python.exe"
WINDOWS_PYVENV_CONFIG_PATH = ".venv/pyvenv.cfg"
WINDOWS_PYVENV_CONFIG_MAX_BYTES = 64 * 1024
WINDOWS_BASE_PYTHON_DLL_LIMIT = 8
WINDOWS_CLOSURE_MAX_FILES = 64 * 1024
WINDOWS_CLOSURE_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
WINDOWS_CLOSURE_MAX_FILE_BYTES = 256 * 1024 * 1024
WINDOWS_RELEASE_MANIFEST_MAX_BYTES = 32 * 1024 * 1024
WINDOWS_BASE_PYTHON_DLL_PATTERN = re.compile(
    r"python3(?:\d{1,3}t?|t)?(?:_d)?\.dll",
    re.IGNORECASE,
)
ROLLBACK_RECOVERABLE_STATUSES = frozenset(
    {
        "prepared",
        "publishing_runtime",
        "previous_runtime_staged",
        "runtime_published",
        "rollback_failed",
    }
)
ROLLBACK_PROGRESS_STEPS = (
    "managed_jobs_unloaded",
    "external_restored",
    "runtime_restored",
    "services_restored",
)
INDEX_REBUILD_CODE = "\n".join(
    (
        "import sys",
        "sys.dont_write_bytecode = True",
        "sys.path.insert(0, sys.argv[1])",
        "from config import load_config",
        "from session_harvester import rebuild_memory_index, _refresh_effectiveness_report",
        "cfg = load_config()",
        "rebuild_memory_index(cfg, repair_generated=False)",
        "_refresh_effectiveness_report(cfg)",
    )
)
CONTEXT_COMPILE_CODE = "\n".join(
    (
        "import json",
        "import sys",
        "sys.dont_write_bytecode = True",
        "sys.path.insert(0, sys.argv[1])",
        "from config import load_config",
        "from compiler import run",
        "result = run(load_config(), sync_agent_memory=False)",
        "errors = list(result.get('context_target_errors') or [])",
        "profile_error = result.get('codex_profile_agents_error')",
        "errors.extend([profile_error] if profile_error else [])",
        "if errors: raise SystemExit(json.dumps({'errors': errors}, ensure_ascii=False))",
        "print(json.dumps(result, ensure_ascii=False, sort_keys=True))",
    )
)
RUNTIME_PYTHON_CHECK_CODE = (
    "import sys; "
    "raise SystemExit("
    "'Agent Memory Beacon requires Python 3.11+' "
    "if sys.version_info < (3, 11) else 0)"
)
_ISOLATED_SCRIPT_CODE = (
    "import runpy,sys; "
    "script=sys.argv.pop(1); import_root=sys.argv.pop(1); "
    "sys.path.insert(0, import_root); sys.argv[0]=script; "
    "runpy.run_path(script, run_name='__main__')"
)
_ISOLATED_MODULE_CODE = (
    "import runpy,sys; "
    "module=sys.argv.pop(1); import_root=sys.argv.pop(1); "
    "sys.path.insert(0, import_root); sys.argv[0]=module; "
    "runpy.run_module(module, run_name='__main__')"
)
WINDOWS_SYNC_TEST_MODULES = (
    "tests.test_beacon_sync_protocol",
    "tests.test_beacon_sync_producer",
    "tests.test_beacon_sync_reducer",
    "tests.test_beacon_sync_snapshot",
    "tests.test_beacon_sync_cli",
    "tests.test_beacon_sync_end_to_end",
    "tests.test_beacon_sync_windows",
    "tests.test_config",
    "tests.test_install_beacon_sync",
)
RUNTIME_ROOT_FILES = ("LICENSE",)
RUNTIME_SCRIPT_FILES = (
    "__init__.py",
    "analyzer.py",
    "annotation_quality.py",
    "backup.py",
    "beacon_sync.py",
    "beacon_sync_producer.py",
    "beacon_sync_protocol.py",
    "beacon_sync_reducer.py",
    "beacon_sync_snapshot.py",
    "branding.py",
    "codex_profile_sync.py",
    "codex_prompt_hook.py",
    "compiler.py",
    "config.py",
    "conversation_summary.py",
    "context_install.py",
    "doctor.py",
    "error_evidence.py",
    "evaluate_annotation_quality.py",
    "evaluate_memory_comparison.py",
    "experience_memory.py",
    "graph_projection.py",
    "install_beacon_sync.py",
    "install_claude.py",
    "install_codex.py",
    "install_launchd.py",
    "install_runtime.py",
    "insight_memory.py",
    "knowledge_index.py",
    "link_validator.py",
    "maintainer.py",
    "memory_judge.py",
    "memory_authority.py",
    "memory_effectiveness.py",
    "memory_identity_repair.py",
    "memory_lifecycle.py",
    "memory_lifecycle_batch.py",
    "memory_graph.py",
    "memory_relation_batch.py",
    "memory_promotion.py",
    "memory_quality_audit.py",
    "memory_recall.py",
    "memory_runtime.py",
    "memory_schema.py",
    "reporter.py",
    "runner.py",
    "safety.py",
    "score_sessions.py",
    "session_harvester.py",
    "setup.py",
    "skill_preference_learner.py",
    "transcript_utils.py",
    "validate_frontmatter.py",
    "workflow_memory.py",
)
RUNTIME_TEMPLATE_FILES = (
    "00-Rules/_TEMPLATE.md",
    "00-Rules/_inbox/_TEMPLATE.md",
    "01-Projects/project-alpha/Feedback/_TEMPLATE.md",
    "01-Projects/project-alpha/Memory/cross-project-links.md",
    "01-Projects/project-alpha/Memory/decisions.md",
    "01-Projects/project-alpha/Memory/pitfalls.md",
    "01-Projects/project-alpha/Memory/sessions/_TEMPLATE.md",
    "04-Feedback/error-taxonomy.md",
    "04-Feedback/growth-metrics.md",
    "04-Feedback/heartbeat.md",
    "04-Feedback/weekly-reports/_TEMPLATE.md",
    "README.md",
    "用户手册.md",
)
RUNTIME_CONFIG_SCALAR_FIELDS = frozenset(
    {
        "agent",
        "agent_memory_path",
        "backup_path",
        "claude_md_path",
        "claude_project_path",
        "codex_home",
        "codex_profile_check_on_start",
        "codex_profile_path",
        "codex_sessions_path",
        "harvest_interval_seconds",
        "harvest_start_max_transcripts",
        "harvest_start_time_budget_seconds",
        "log_dir",
        "log_level",
        "memory_index_path",
        "product_id",
        "scan_on_start",
        "skip_git_probe",
        "user_home",
        "vault_path",
        "version",
        "zcode_db_path",
        "zcode_home",
    }
)
RUNTIME_CONFIG_STRING_LIST_FIELDS = frozenset(
    {
        "context_targets",
        "session_paths",
        "transcript_agents",
        "transcript_paths",
    }
)
RUNTIME_CONFIG_STRING_MAP_FIELDS = frozenset({"project_keywords", "topic_map"})
RUNTIME_CONFIG_SECTION_FIELDS = {
    "annotation_quality": frozenset({"candidate_dir", "enabled", "report_path"}),
    "beacon_sync": frozenset(
        {
            "device_id",
            "enabled",
            "gc_retention_seconds",
            "inboxes",
            "attachment_roots",
            "max_attachment_bytes",
            "max_chunk_bytes",
            "max_event_json_bytes",
            "max_events_per_run",
            "max_gap_bytes",
            "max_object_bytes",
            "max_replica_object_bytes",
            "outbox_dir",
            "published_dir",
            "received_published_dir",
            "replica_path",
            "role",
            "state_dir",
        }
    ),
    "conversation_summary": frozenset(
        {
            "enabled",
            "max_recall",
            "max_summary_bytes",
            "message_interval",
            "min_substantive_messages",
            "retry_interval_messages",
            "stale_after_minutes",
            "token_budget",
        }
    ),
    "api": frozenset(
        {
            "base_url",
            "max_retries",
            "max_tokens",
            "model",
            "retry_backoff_sec",
            "settings_json",
            "temperature",
        }
    ),
    "error_evidence": frozenset(
        {"candidate_dir", "enabled", "excerpt_limit", "source_limit"}
    ),
    "graph_projection": frozenset({"enabled", "max_nodes", "output_dir"}),
    "memory_lifecycle": frozenset({"audit_path", "proposal_dir", "rollback_dir"}),
    "memory_effectiveness": frozenset(
        {
            "enabled",
            "event_log_path",
            "feedback_window_minutes",
            "max_report_items",
            "report_path",
        }
    ),
    "memory_promotion": frozenset(
        {
            "enabled",
            "max_proposals_per_run",
            "min_exposure_count",
            "min_source_count",
            "proposal_dir",
        }
    ),
    "memory_runtime": frozenset(
        {
            "duplicate_suppression_minutes",
            "enabled",
            "hook_timeout_ms",
            "index_path",
            "internal_deadline_ms",
            "log_path",
            "max_first_prompt",
            "max_refresh",
            "max_risk_or_error",
            "stale_after_minutes",
            "state_dir",
            "token_budget",
            "topic_min_terms",
            "topic_similarity_threshold",
        }
    ),
    "insight_memory": frozenset(
        {
            "candidate_dir",
            "direct_seed_threshold",
            "enabled",
            "formal_path",
            "max_auto_recall",
            "recall_token_budget",
            "reinforce_source_count",
            "similarity_threshold",
        }
    ),
    "personal_memory": frozenset(
        {
            "candidate_dir",
            "candidate_threshold",
            "direct_threshold",
            "enabled",
            "formal_path",
            "promote_seen_count",
            "similarity_threshold",
        }
    ),
    "privacy": frozenset(
        {"store_message_samples", "store_raw_transcripts", "store_transcript_metadata"}
    ),
    "proxy": frozenset({"host", "port"}),
    "scan": frozenset({"day", "hour", "minute"}),
    "skill_preferences": frozenset(
        {
            "candidate_dir",
            "enabled",
            "formal_path",
            "initial_confidence",
            "promote_seen_count",
            "repeat_increment",
            "similarity_threshold",
        }
    ),
    "workflow_memory": frozenset(
        {
            "candidate_dir",
            "enabled",
            "formal_path",
            "initial_confidence",
            "promote_seen_count",
            "repeat_increment",
            "similarity_threshold",
        }
    ),
}
SYSTEM_PATH_ALIASES = {
    Path("/etc"): Path("/private/etc"),
    Path("/tmp"): Path("/private/tmp"),
    Path("/var"): Path("/private/var"),
}


@dataclass(frozen=True)
class ReleaseFile:
    relative_path: str
    content: bytes
    mode: int

    @property
    def sha256(self):
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class ReleasePlan:
    source_root: Path
    install_root: Path
    cfg: dict
    source_python_path: str
    files: tuple[ReleaseFile, ...]
    release_id: str
    manifest_bytes: bytes


@dataclass(frozen=True)
class StagedRuntime:
    root: Path
    release_id: str
    manifest_path: Path
    final_plan: ReleasePlan | None = None


@dataclass(frozen=True)
class ReleaseVerification:
    action: str
    release_id: str
    file_count: int


@dataclass(frozen=True)
class ReleaseResult:
    action: str
    install_root: Path
    manifest_path: Path
    release_id: str
    trust_review_required: bool = False
    actions: tuple[str, ...] = ()


def _venv_python(runtime_root, platform_name=None):
    runtime_root = Path(runtime_root)
    platform_name = platform_name or ("windows" if os.name == "nt" else "posix")
    if platform_name == "windows":
        return runtime_root / ".venv" / "Scripts" / "python.exe"
    return runtime_root / ".venv" / "bin" / "python"


def build_release_plan(
    source_root,
    install_root,
    cfg,
    *,
    venv_layout=None,
):
    """Build a deterministic, secret-scrubbed runtime release plan."""
    _require_supported_python()
    source_root = _absolute_path(source_root)
    install_root = _absolute_path(install_root)
    if not source_root.is_dir():
        raise ValueError(f"source root is not a directory: {source_root}")
    _assert_no_symlink_chain(source_root)
    _validate_install_root(install_root)
    if _is_within(install_root, source_root) or _is_within(source_root, install_root):
        raise ValueError("source and install roots must not contain one another")
    if not isinstance(cfg, dict):
        raise TypeError("runtime configuration must be a mapping")

    source_python = _validated_executable(cfg.get("python_path") or sys.executable)
    runtime_cfg = _runtime_config(
        cfg,
        install_root,
        source_root=source_root,
        venv_layout=venv_layout,
    )
    files = []
    for name in RUNTIME_ROOT_FILES:
        files.append(
            _source_release_file(source_root, source_root / name, name)
        )
    for name in RUNTIME_SCRIPT_FILES:
        source = source_root / "scripts" / name
        files.append(_source_release_file(source_root, source, f"scripts/{name}"))
    for name in ("requirements.txt", "requirements.lock"):
        files.append(
            _source_release_file(
                source_root,
                source_root / "scripts" / name,
                f"scripts/{name}",
            )
        )
    files.append(
        ReleaseFile(
            relative_path="scripts/config.yaml",
            content=yaml.safe_dump(
                runtime_cfg,
                allow_unicode=True,
                sort_keys=True,
            ).encode("utf-8"),
            mode=0o600,
        )
    )
    files.append(
        _source_release_file(
            source_root,
            source_root / "patches" / "AGENT_MEMORY_BEACON.md.patch",
            "patches/AGENT_MEMORY_BEACON.md.patch",
        )
    )
    for name in RUNTIME_TEMPLATE_FILES:
        files.append(
            _source_release_file(
                source_root,
                source_root / "templates" / "vault" / name,
                f"templates/vault/{name}",
            )
        )
    files = tuple(sorted(files, key=lambda item: item.relative_path))
    base_manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "generated_by": "agent_memory_beacon",
        "install_root": str(install_root),
        "files": [
            {
                "path": item.relative_path,
                "mode": f"{item.mode:04o}",
                "size": len(item.content),
                "sha256": item.sha256,
            }
            for item in files
        ],
    }
    release_id = hashlib.sha256(_canonical_json(base_manifest)).hexdigest()[:16]
    manifest = {**base_manifest, "release_id": release_id}
    manifest_bytes = _pretty_json(manifest)
    return ReleasePlan(
        source_root=source_root,
        install_root=install_root,
        cfg=runtime_cfg,
        source_python_path=source_python,
        files=files,
        release_id=release_id,
        manifest_bytes=manifest_bytes,
    )


def build_windows_sync_release_plan(source_root, runtime_root, cfg):
    """Build one immutable versioned release for the Windows sync runtime."""
    if not _producer_replica_sync_enabled(cfg):
        raise ValueError(
            "Windows sync runtime requires enabled producer-replica role"
        )
    runtime_root = _absolute_path(runtime_root)
    releases = runtime_root / "releases"
    identity_plan = build_release_plan(
        source_root,
        releases / WINDOWS_RELEASE_IDENTITY_ROOT,
        cfg,
        venv_layout="windows",
    )
    source_release_id = _windows_source_release_id(identity_plan.files)
    runtime_identity = _windows_runtime_identity_for_plan(
        identity_plan.source_python_path
    )
    runtime_python = runtime_identity["runtime_python"]
    runtime_environment = runtime_identity.get("runtime_environment")
    release_id = _windows_release_id(
        source_release_id,
        runtime_python,
        runtime_environment,
    )
    release_root = releases / release_id
    actual = build_release_plan(
        source_root,
        release_root,
        cfg,
        venv_layout="windows",
    )
    manifest = json.loads(actual.manifest_bytes)
    manifest["release_id"] = release_id
    manifest["release_kind"] = "windows-sync"
    manifest["source_release_id"] = source_release_id
    manifest["runtime_python"] = runtime_python
    if runtime_environment is not None:
        manifest["runtime_environment"] = runtime_environment
    return ReleasePlan(
        source_root=actual.source_root,
        install_root=actual.install_root,
        cfg=actual.cfg,
        source_python_path=actual.source_python_path,
        files=actual.files,
        release_id=release_id,
        manifest_bytes=_pretty_json(manifest),
    )


def _windows_source_release_id(files):
    identity = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "release_kind": "windows-sync-source",
        "files": [
            {
                "path": item.relative_path,
                "size": len(item.content),
                "sha256": item.sha256,
            }
            for item in sorted(files, key=lambda item: item.relative_path)
        ],
    }
    return hashlib.sha256(_canonical_json(identity)).hexdigest()


def _windows_release_id(
    source_release_id,
    runtime_python,
    runtime_environment=None,
):
    identity = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "release_kind": "windows-sync",
        "source_release_id": source_release_id,
        "runtime_python": runtime_python,
    }
    if runtime_environment is not None:
        identity["runtime_environment"] = runtime_environment
    return hashlib.sha256(_canonical_json(identity)).hexdigest()[:16]


def _windows_runtime_python_identity(path):
    size, digest = _bounded_closure_file_identity(path, "runtime Python")
    return {
        "path": WINDOWS_RUNTIME_PYTHON_PATH,
        "size": size,
        "sha256": digest,
    }


def _windows_runtime_identity_for_plan(source_python):
    if os.name != "nt":
        return {
            "runtime_python": _windows_runtime_python_identity(source_python),
        }
    with tempfile.TemporaryDirectory(
        prefix="agent-memory-beacon-venv-probe-"
    ) as temp:
        probe_root = Path(temp) / ".venv"
        _run_checked(
            None,
            (
                source_python,
                "-I",
                "-B",
                "-m",
                "venv",
                "--copies",
                "--without-pip",
                probe_root,
            ),
            cwd=Path(temp),
            timeout=120,
            label="Windows runtime launcher probe",
        )
        return _windows_runtime_environment_identity(probe_root.parent)


def _windows_runtime_python_for_plan(source_python):
    return _windows_runtime_identity_for_plan(source_python)["runtime_python"]


def _windows_runtime_environment_identity(runtime_root):
    runtime_root = Path(runtime_root)
    pyvenv_values, pyvenv_identity = _windows_pyvenv_config_identity(
        runtime_root
    )
    base_python = _windows_base_python_identity(pyvenv_values)
    return {
        "runtime_python": _windows_runtime_python_identity(
            _venv_python(runtime_root, platform_name="windows")
        ),
        "runtime_environment": {
            "pyvenv_config": pyvenv_identity,
            "base_python": base_python,
            "execution_closure": _windows_execution_closure(
                runtime_root,
                pyvenv_values,
            ),
        },
    }


def _windows_pyvenv_config_identity(runtime_root):
    runtime_root = Path(runtime_root)
    config_path = runtime_root / WINDOWS_PYVENV_CONFIG_PATH
    _assert_no_symlink_chain(config_path, stop=runtime_root)
    config_info = config_path.lstat()
    if getattr(config_info, "st_nlink", 1) != 1:
        raise ValueError("Windows pyvenv configuration has a hard link")
    content = _read_regular_file_bounded(
        config_path,
        "Windows pyvenv configuration",
        WINDOWS_PYVENV_CONFIG_MAX_BYTES,
    )
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Windows pyvenv configuration is not UTF-8") from exc
    values = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if "=" not in line:
            raise ValueError("Windows pyvenv configuration row is invalid")
        key, value = (part.strip() for part in line.split("=", 1))
        if (
            not re.fullmatch(r"[a-z][a-z0-9-]*", key)
            or key in values
            or not value
        ):
            raise ValueError("Windows pyvenv configuration row is invalid")
        values[key] = value
    required = {
        "home",
        "include-system-site-packages",
        "version",
        "executable",
        "command",
    }
    if not required.issubset(values):
        raise ValueError("Windows pyvenv configuration is incomplete")
    include_system = values["include-system-site-packages"].lower()
    if include_system != "false":
        raise ValueError("Windows pyvenv site-packages setting is invalid")
    marker_match = re.search(r"\s+-m\s+venv\s+", values["command"])
    if marker_match is None:
        raise ValueError("Windows pyvenv command is invalid")
    command = values["command"]
    command_python = command[: marker_match.start()].strip().strip('"')
    command_arguments = command[marker_match.end() :].strip()
    expected_prefixes = (
        "--copies --without-pip ",
        "--without-pip --copies ",
    )
    prefix = next(
        (item for item in expected_prefixes if command_arguments.startswith(item)),
        None,
    )
    if prefix is None:
        raise ValueError("Windows pyvenv command is not probe-equivalent")
    environment_path = command_arguments[len(prefix) :].strip().strip('"')
    if not environment_path:
        raise ValueError("Windows pyvenv command target is invalid")
    normalized_executable = _normalized_runtime_path(values["executable"])
    normalized_command_python = _normalized_runtime_path(command_python)
    if any(
        ntpath.basename(path).casefold() not in {"python.exe", "python_d.exe"}
        for path in (normalized_executable, normalized_command_python)
    ):
        raise ValueError("Windows pyvenv command executable is invalid")
    normalized_target = _normalized_runtime_path(environment_path)
    expected_target = _normalized_runtime_path(
        runtime_root / WINDOWS_PYVENV_CONFIG_PATH.rsplit("/", 1)[0]
    )
    if normalized_target != expected_target:
        target = Path(environment_path)
        stage_parent = target.parent
        if (
            target.name.casefold() != ".venv"
            or stage_parent.parent != runtime_root.parent
            or ".staging-" not in stage_parent.name
        ):
            raise ValueError("Windows pyvenv command target is invalid")
    version_match = re.fullmatch(r"\d+\.\d+(?:\.\d+)?", values["version"])
    if version_match is None:
        raise ValueError("Windows base Python version is invalid")
    semantic_fields = {
        "home": _normalized_runtime_path(values["home"]),
        "include-system-site-packages": include_system,
        "version": values["version"],
        "target": WINDOWS_PYVENV_CONFIG_PATH.rsplit("/", 1)[0],
    }
    canonical = _canonical_json({"fields": semantic_fields})
    return values, {
        "path": WINDOWS_PYVENV_CONFIG_PATH,
        "size": len(canonical),
        "sha256": hashlib.sha256(canonical).hexdigest(),
    }


def _normalized_runtime_path(value):
    value = str(value or "").strip()
    if not value:
        raise ValueError("Windows Python configuration path is empty")
    if ntpath.isabs(value):
        return ntpath.normcase(ntpath.normpath(value))
    if os.path.isabs(value):
        return os.path.normcase(os.path.normpath(value))
    raise ValueError("Windows Python configuration path is not absolute")


def _windows_base_python_identity(pyvenv_values):
    home_value = pyvenv_values["home"].strip()
    if not (ntpath.isabs(home_value) or os.path.isabs(home_value)):
        raise ValueError("Windows base Python home is not absolute")
    home = Path(home_value)
    _assert_no_symlink_chain(home)
    if not home.is_dir():
        raise ValueError(f"Windows base Python home is missing: {home}")
    executable_name = ntpath.basename(pyvenv_values["executable"]).casefold()
    if executable_name not in {"python.exe", "python_d.exe"}:
        raise ValueError("Windows base Python executable name is invalid")
    executable = home / executable_name
    _assert_no_symlink_chain(executable, stop=home.parent)
    executable_size, executable_sha256 = _bounded_closure_file_identity(
        executable,
        "Windows base Python executable",
    )
    version_match = re.fullmatch(
        r"(\d+)\.(\d+)(?:\.\d+)?",
        pyvenv_values["version"],
    )
    if version_match is None:
        raise ValueError("Windows base Python version is invalid")
    version_tag = "".join(version_match.groups()[:2])
    allowed_dll_names = {
        "python3.dll",
        "python3_d.dll",
        "python3t.dll",
        "python3t_d.dll",
        f"python{version_tag}.dll",
        f"python{version_tag}_d.dll",
        f"python{version_tag}t.dll",
        f"python{version_tag}t_d.dll",
    }
    dll_paths = [
        path
        for path in home.iterdir()
        if path.name.casefold() in allowed_dll_names
    ]
    dll_paths.sort(key=lambda path: path.name.casefold())
    if not dll_paths or len(dll_paths) > WINDOWS_BASE_PYTHON_DLL_LIMIT:
        raise ValueError("Windows base Python DLL closure is invalid")
    dlls = []
    seen = set()
    for path in dll_paths:
        name = path.name.casefold()
        if name in seen:
            raise ValueError("Windows base Python DLL closure has duplicate names")
        seen.add(name)
        _assert_no_symlink_chain(path, stop=home.parent)
        size, digest = _bounded_closure_file_identity(
            path,
            f"Windows base Python DLL {name}",
        )
        dlls.append({"name": name, "size": size, "sha256": digest})
    return {
        "executable": {
            "name": executable_name,
            "size": executable_size,
            "sha256": executable_sha256,
        },
        "dlls": dlls,
    }


def _windows_execution_closure(runtime_root, pyvenv_values):
    runtime_root = Path(runtime_root)
    home = Path(pyvenv_values["home"].strip())
    specifications = (
        (
            "venv-site-packages",
            runtime_root / ".venv" / "Lib" / "site-packages",
            ".venv/Lib/site-packages",
            frozenset(),
        ),
        (
            "base-stdlib",
            home / "Lib",
            "<base>/Lib",
            frozenset({"site-packages"}),
        ),
        (
            "base-dlls",
            home / "DLLs",
            "<base>/DLLs",
            frozenset(),
        ),
    )
    trees = []
    file_count = 0
    total_bytes = 0
    for name, root, locator, excluded in specifications:
        tree = _snapshot_windows_closure_tree(
            root,
            name=name,
            locator=locator,
            excluded_top_level=excluded,
        )
        file_count += tree["file_count"]
        total_bytes += tree["total_bytes"]
        if file_count > WINDOWS_CLOSURE_MAX_FILES:
            raise ValueError("Windows runtime closure exceeds file count limit")
        if total_bytes > WINDOWS_CLOSURE_MAX_TOTAL_BYTES:
            raise ValueError("Windows runtime closure exceeds total size limit")
        trees.append(tree)
    return {
        "limits": {
            "max_files": WINDOWS_CLOSURE_MAX_FILES,
            "max_total_bytes": WINDOWS_CLOSURE_MAX_TOTAL_BYTES,
            "max_file_bytes": WINDOWS_CLOSURE_MAX_FILE_BYTES,
        },
        "file_count": file_count,
        "total_bytes": total_bytes,
        "trees": trees,
    }


def _snapshot_windows_closure_tree(
    root,
    *,
    name,
    locator,
    excluded_top_level=frozenset(),
):
    root = Path(root)
    _assert_no_symlink_chain(root)
    if not root.is_dir():
        raise ValueError(f"Windows runtime closure tree is missing: {name}")
    rows = []
    total_bytes = 0

    def visit(directory, relative_parts):
        nonlocal total_bytes
        _assert_no_symlink_chain(directory, stop=root.parent)
        directory_info = directory.lstat()
        if (
            getattr(directory_info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            or stat.S_ISLNK(directory_info.st_mode)
            or not stat.S_ISDIR(directory_info.st_mode)
        ):
            raise ValueError(f"Windows runtime closure contains reparse path: {directory}")
        entries = sorted(
            os.scandir(directory),
            key=lambda item: (item.name.casefold(), item.name),
        )
        folded_names = [entry.name.casefold() for entry in entries]
        if len(folded_names) != len(set(folded_names)):
            raise ValueError(f"Windows runtime closure has duplicate names: {directory}")
        for entry in entries:
            if not relative_parts and entry.name.casefold() in excluded_top_level:
                continue
            path = Path(entry.path)
            info = entry.stat(follow_symlinks=False)
            if (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                or stat.S_ISLNK(info.st_mode)
            ):
                raise ValueError(f"Windows runtime closure contains reparse path: {path}")
            child_parts = (*relative_parts, entry.name)
            if stat.S_ISDIR(info.st_mode):
                visit(path, child_parts)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Windows runtime closure contains special file: {path}")
            relative = PurePosixPath(*child_parts).as_posix()
            size, digest = _bounded_closure_file_identity(
                path,
                f"Windows runtime closure file {name}/{relative}",
            )
            rows.append({"path": relative, "size": size, "sha256": digest})
            total_bytes += size
            if len(rows) > WINDOWS_CLOSURE_MAX_FILES:
                raise ValueError("Windows runtime closure exceeds file count limit")
            if total_bytes > WINDOWS_CLOSURE_MAX_TOTAL_BYTES:
                raise ValueError("Windows runtime closure exceeds total size limit")

    visit(root, ())
    rows.sort(key=lambda row: (row["path"].casefold(), row["path"]))
    return {
        "name": name,
        "root": locator,
        "excluded": sorted(excluded_top_level),
        "file_count": len(rows),
        "total_bytes": total_bytes,
        "files": rows,
    }


def _bounded_closure_file_identity(path, label):
    path = Path(path)
    _assert_no_symlink_chain(path)
    before = path.lstat()
    if (
        getattr(before, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        or stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
    ):
        raise ValueError(f"{label} is not a regular non-reparse file: {path}")
    if getattr(before, "st_nlink", 1) != 1:
        raise ValueError(f"{label} has a hard link: {path}")
    if before.st_size > WINDOWS_CLOSURE_MAX_FILE_BYTES:
        raise ValueError(f"{label} exceeds single-file size limit: {path}")
    size, digest = _regular_file_size_sha256(path, label)
    after = path.lstat()
    if getattr(after, "st_nlink", 1) != 1:
        raise ValueError(f"{label} has a hard link: {path}")
    return size, digest


def _valid_sized_hash_identity(row, locator, expected=None):
    if (
        not isinstance(row, dict)
        or set(row) != {locator, "size", "sha256"}
        or not isinstance(row.get(locator), str)
        or not row[locator]
        or (expected is not None and row[locator] != expected)
        or isinstance(row.get("size"), bool)
        or not isinstance(row.get("size"), int)
        or row["size"] < 0
        or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256") or ""))
    ):
        return False
    return True


def _valid_windows_runtime_environment(value):
    if not isinstance(value, dict) or set(value) != {
        "pyvenv_config",
        "base_python",
        "execution_closure",
    }:
        return False
    if not _valid_sized_hash_identity(
        value.get("pyvenv_config"),
        "path",
        WINDOWS_PYVENV_CONFIG_PATH,
    ):
        return False
    base_python = value.get("base_python")
    if not isinstance(base_python, dict) or set(base_python) != {
        "executable",
        "dlls",
    }:
        return False
    executable = base_python.get("executable")
    if (
        not _valid_sized_hash_identity(executable, "name")
        or executable["name"] not in {"python.exe", "python_d.exe"}
    ):
        return False
    dlls = base_python.get("dlls")
    if (
        not isinstance(dlls, list)
        or not dlls
        or len(dlls) > WINDOWS_BASE_PYTHON_DLL_LIMIT
        or any(not _valid_sized_hash_identity(row, "name") for row in dlls)
    ):
        return False
    names = [row["name"] for row in dlls]
    dlls_valid = (
        names == sorted(names, key=str.casefold)
        and len(names) == len(set(names))
        and all(
            name == name.casefold()
            and WINDOWS_BASE_PYTHON_DLL_PATTERN.fullmatch(name)
            for name in names
        )
    )
    return dlls_valid and _valid_windows_execution_closure(
        value.get("execution_closure")
    )


def _valid_windows_execution_closure(value):
    expected_limits = {
        "max_files": WINDOWS_CLOSURE_MAX_FILES,
        "max_total_bytes": WINDOWS_CLOSURE_MAX_TOTAL_BYTES,
        "max_file_bytes": WINDOWS_CLOSURE_MAX_FILE_BYTES,
    }
    if (
        not isinstance(value, dict)
        or set(value) != {"limits", "file_count", "total_bytes", "trees"}
        or value.get("limits") != expected_limits
        or isinstance(value.get("file_count"), bool)
        or not isinstance(value.get("file_count"), int)
        or value["file_count"] < 0
        or value["file_count"] > WINDOWS_CLOSURE_MAX_FILES
        or isinstance(value.get("total_bytes"), bool)
        or not isinstance(value.get("total_bytes"), int)
        or value["total_bytes"] < 0
        or value["total_bytes"] > WINDOWS_CLOSURE_MAX_TOTAL_BYTES
        or not isinstance(value.get("trees"), list)
    ):
        return False
    expected_trees = (
        ("venv-site-packages", ".venv/Lib/site-packages", []),
        ("base-stdlib", "<base>/Lib", ["site-packages"]),
        ("base-dlls", "<base>/DLLs", []),
    )
    if len(value["trees"]) != len(expected_trees):
        return False
    total_files = 0
    total_bytes = 0
    for tree, (name, root, excluded) in zip(value["trees"], expected_trees):
        if (
            not isinstance(tree, dict)
            or set(tree)
            != {"name", "root", "excluded", "file_count", "total_bytes", "files"}
            or tree.get("name") != name
            or tree.get("root") != root
            or tree.get("excluded") != excluded
            or isinstance(tree.get("file_count"), bool)
            or not isinstance(tree.get("file_count"), int)
            or tree["file_count"] < 0
            or isinstance(tree.get("total_bytes"), bool)
            or not isinstance(tree.get("total_bytes"), int)
            or tree["total_bytes"] < 0
            or not isinstance(tree.get("files"), list)
            or tree["file_count"] != len(tree["files"])
        ):
            return False
        paths = []
        tree_bytes = 0
        for row in tree["files"]:
            if not _valid_sized_hash_identity(row, "path"):
                return False
            relative = row["path"]
            if (
                "\\" in relative
                or relative.startswith("/")
                or ".." in PurePosixPath(relative).parts
                or row["size"] > WINDOWS_CLOSURE_MAX_FILE_BYTES
            ):
                return False
            paths.append(relative)
            tree_bytes += row["size"]
        if (
            paths != sorted(paths, key=lambda item: (item.casefold(), item))
            or len({path.casefold() for path in paths}) != len(paths)
            or tree_bytes != tree["total_bytes"]
        ):
            return False
        total_files += tree["file_count"]
        total_bytes += tree["total_bytes"]
    return (
        total_files == value["file_count"]
        and total_bytes == value["total_bytes"]
        and total_files <= WINDOWS_CLOSURE_MAX_FILES
        and total_bytes <= WINDOWS_CLOSURE_MAX_TOTAL_BYTES
    )


def verify_installed_release(runtime_root):
    """Verify every manifest-owned source byte in one installed runtime."""
    runtime_root = _absolute_path(runtime_root)
    _assert_no_symlink_chain(runtime_root)
    manifest_path = runtime_root / "release-manifest.json"
    _assert_no_symlink_chain(manifest_path, stop=runtime_root)
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("runtime release manifest is missing")
    manifest_info = manifest_path.lstat()
    if getattr(manifest_info, "st_nlink", 1) != 1:
        raise ValueError("runtime release manifest has a hard link")
    try:
        manifest_bytes = _read_regular_file_bounded(
            manifest_path,
            "runtime release manifest",
            WINDOWS_RELEASE_MANIFEST_MAX_BYTES,
        )
        payload = json.loads(manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("runtime release manifest is invalid") from exc
    release_kind = payload.get("release_kind") if isinstance(payload, dict) else None
    has_runtime_environment = (
        isinstance(payload, dict) and "runtime_environment" in payload
    )
    expected_manifest_fields = {
        "schema_version",
        "generated_by",
        "install_root",
        "release_id",
        "files",
    }
    if release_kind == "windows-sync":
        expected_manifest_fields.update(
            {"release_kind", "source_release_id", "runtime_python"}
        )
        expected_manifest_fields.add("runtime_environment")
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_manifest_fields
        or payload.get("generated_by") != "agent_memory_beacon"
        or payload.get("schema_version") != MANIFEST_SCHEMA_VERSION
        or payload.get("install_root") != str(runtime_root)
        or not re.fullmatch(r"[0-9a-f]{16}", str(payload.get("release_id") or ""))
        or release_kind not in {None, "windows-sync"}
        or not isinstance(payload.get("files"), list)
    ):
        raise ValueError("runtime release manifest identity is invalid")
    if release_kind == "windows-sync":
        runtime_python_row = payload.get("runtime_python")
        if (
            not re.fullmatch(
                r"[0-9a-f]{64}",
                str(payload.get("source_release_id") or ""),
            )
            or not isinstance(runtime_python_row, dict)
            or set(runtime_python_row) != {"path", "size", "sha256"}
            or runtime_python_row.get("path") != WINDOWS_RUNTIME_PYTHON_PATH
            or isinstance(runtime_python_row.get("size"), bool)
            or not isinstance(runtime_python_row.get("size"), int)
            or runtime_python_row["size"] < 0
            or not re.fullmatch(
                r"[0-9a-f]{64}",
                str(runtime_python_row.get("sha256") or ""),
            )
        ):
            raise ValueError("Windows runtime release identity is invalid")
        if not has_runtime_environment or not _valid_windows_runtime_environment(
            payload.get("runtime_environment")
        ):
            raise ValueError("Windows runtime environment identity is invalid")
    if (
        release_kind == "windows-sync"
        and runtime_root.name != payload["release_id"]
    ):
        raise ValueError("Windows runtime release identity does not match directory")
    seen = set()
    verified_contents = {}
    for row in payload["files"]:
        if not isinstance(row, dict) or set(row) != {
            "path",
            "mode",
            "size",
            "sha256",
        }:
            raise ValueError("runtime release manifest file row is invalid")
        relative = str(row["path"] or "")
        if (
            not relative
            or relative in seen
            or "\\" in relative
            or relative.startswith("/")
            or ".." in PurePosixPath(relative).parts
        ):
            raise ValueError("runtime release manifest path is invalid")
        if (
            isinstance(row["size"], bool)
            or not isinstance(row["size"], int)
            or row["size"] < 0
            or not re.fullmatch(r"[0-9a-f]{64}", str(row["sha256"] or ""))
            or not re.fullmatch(r"[0-7]{4}", str(row["mode"] or ""))
            or row["size"] > WINDOWS_CLOSURE_MAX_FILE_BYTES
        ):
            raise ValueError("runtime release manifest file row is invalid")
        seen.add(relative)
        path = runtime_root.joinpath(*PurePosixPath(relative).parts)
        _assert_path_under(path, runtime_root)
        _assert_no_symlink_chain(path, stop=runtime_root)
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"runtime release file is missing: {relative}")
        if getattr(path.lstat(), "st_nlink", 1) != 1:
            raise ValueError(f"runtime release file has a hard link: {relative}")
        if (
            os.name != "nt"
            and stat.S_IMODE(path.stat().st_mode) != int(row["mode"], 8)
        ):
            raise ValueError(f"runtime release file mode changed: {relative}")
        content = path.read_bytes()
        if (
            len(content) != row["size"]
            or hashlib.sha256(content).hexdigest() != row["sha256"]
        ):
            raise ValueError(f"runtime release file changed: {relative}")
        verified_contents[relative] = content
    if seen != _runtime_release_file_paths():
        raise ValueError("runtime release manifest file set is incomplete")
    if _actual_runtime_release_file_paths(runtime_root) != seen:
        raise ValueError("runtime release file set contains extra or missing files")
    layout = "windows" if release_kind == "windows-sync" else None
    expected_python = _venv_python(runtime_root, platform_name=layout)
    _assert_no_symlink_chain(expected_python, stop=runtime_root)
    _validated_executable(expected_python)
    runtime_python_identity = None
    runtime_environment_identity = None
    if release_kind == "windows-sync":
        try:
            actual_runtime_identity = _windows_runtime_environment_identity(
                runtime_root
            )
        except ValueError as exc:
            raise ValueError(
                "Windows runtime environment cannot be verified"
            ) from exc
        runtime_python_identity = actual_runtime_identity["runtime_python"]
        runtime_environment_identity = actual_runtime_identity[
            "runtime_environment"
        ]
        if runtime_python_identity != payload["runtime_python"]:
            raise ValueError("Windows runtime Python identity changed")
        expected_environment = payload["runtime_environment"]
        if (
            runtime_environment_identity["pyvenv_config"]
            != expected_environment["pyvenv_config"]
        ):
            raise ValueError("Windows runtime pyvenv configuration changed")
        if (
            runtime_environment_identity["base_python"]
            != expected_environment["base_python"]
        ):
            raise ValueError("Windows base Python closure changed")
        if (
            runtime_environment_identity["execution_closure"]
            != expected_environment["execution_closure"]
        ):
            raise ValueError("Windows runtime execution closure changed")
    try:
        runtime_cfg = yaml.safe_load(
            (runtime_root / "scripts" / "config.yaml").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError("runtime release configuration is invalid") from exc
    if (
        not isinstance(runtime_cfg, dict)
        or runtime_cfg.get("runtime_root") != str(runtime_root)
        or runtime_cfg.get("python_path") != str(expected_python)
        or (
            release_kind == "windows-sync"
            and not _producer_replica_sync_enabled(runtime_cfg)
        )
    ):
        raise ValueError("runtime release configuration binding is invalid")
    if release_kind == "windows-sync":
        identity_root = runtime_root.parent / WINDOWS_RELEASE_IDENTITY_ROOT
        normalized_cfg = dict(runtime_cfg)
        normalized_cfg["runtime_root"] = str(identity_root)
        normalized_cfg["python_path"] = str(
            _venv_python(identity_root, platform_name="windows")
        )
        normalized_config = yaml.safe_dump(
            normalized_cfg,
            allow_unicode=True,
            sort_keys=True,
        ).encode("utf-8")
        identity_files = tuple(
            ReleaseFile(
                relative_path=relative,
                content=(
                    normalized_config
                    if relative == "scripts/config.yaml"
                    else verified_contents[relative]
                ),
                mode=0,
            )
            for relative in sorted(verified_contents)
        )
        source_release_id = _windows_source_release_id(identity_files)
        if source_release_id != payload["source_release_id"]:
            raise ValueError("Windows source release identity changed")
        release_id = _windows_release_id(
            source_release_id,
            runtime_python_identity,
            runtime_environment_identity,
        )
        if release_id != payload["release_id"]:
            raise ValueError("Windows runtime release identity changed")
    return {
        "release_id": payload["release_id"],
        "file_count": len(seen),
        "manifest_path": str(manifest_path),
    }


def _runtime_release_file_paths():
    return frozenset(
        (
            *RUNTIME_ROOT_FILES,
            *(f"scripts/{name}" for name in RUNTIME_SCRIPT_FILES),
            "scripts/requirements.txt",
            "scripts/requirements.lock",
            "scripts/config.yaml",
            "patches/AGENT_MEMORY_BEACON.md.patch",
            *(f"templates/vault/{name}" for name in RUNTIME_TEMPLATE_FILES),
        )
    )


def _actual_runtime_release_file_paths(runtime_root):
    runtime_root = Path(runtime_root)
    expected_files = _runtime_release_file_paths()
    expected_directories = set()
    for relative in expected_files:
        parts = PurePosixPath(relative).parts[:-1]
        for index in range(1, len(parts) + 1):
            expected_directories.add(PurePosixPath(*parts[:index]).as_posix())
    managed_roots = {path.split("/", 1)[0] for path in expected_directories}
    allowed_root_entries = {
        *RUNTIME_ROOT_FILES,
        *managed_roots,
        ".venv",
        "release-manifest.json",
    }
    actual_files = set()

    root_entries = sorted(
        os.scandir(runtime_root),
        key=lambda item: (item.name.casefold(), item.name),
    )
    folded_root_names = [entry.name.casefold() for entry in root_entries]
    if len(folded_root_names) != len(set(folded_root_names)):
        raise ValueError("runtime release root contains duplicate names")
    for entry in root_entries:
        if entry.name not in allowed_root_entries:
            raise ValueError(f"runtime release file set contains extra path: {entry.name}")
        info = entry.stat(follow_symlinks=False)
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        ):
            raise ValueError(f"runtime release file set contains a link: {entry.path}")

    def visit(directory, relative_directory):
        _assert_no_symlink_chain(directory, stop=runtime_root)
        entries = sorted(
            os.scandir(directory),
            key=lambda item: (item.name.casefold(), item.name),
        )
        folded_names = [entry.name.casefold() for entry in entries]
        if len(folded_names) != len(set(folded_names)):
            raise ValueError(f"runtime release tree has duplicate names: {directory}")
        for entry in entries:
            relative = PurePosixPath(relative_directory, entry.name).as_posix()
            info = entry.stat(follow_symlinks=False)
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise ValueError(f"runtime release tree contains a link: {entry.path}")
            if stat.S_ISDIR(info.st_mode):
                if relative not in expected_directories:
                    raise ValueError(
                        f"runtime release file set contains extra directory: {relative}"
                    )
                visit(Path(entry.path), relative)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(
                    f"runtime release tree contains a special file: {relative}"
                )
            if getattr(info, "st_nlink", 1) != 1:
                raise ValueError(f"runtime release tree contains a hard link: {relative}")
            actual_files.add(relative)

    for managed_root in sorted(managed_roots):
        visit(runtime_root / managed_root, managed_root)
    for name in RUNTIME_ROOT_FILES:
        path = runtime_root / name
        if path.is_file():
            actual_files.add(name)
    return frozenset(actual_files)


def _finalize_windows_staged_runtime(plan, stage):
    stage = Path(stage)
    provisional_manifest = json.loads(plan.manifest_bytes)
    if provisional_manifest.get("release_kind") != "windows-sync":
        raise ValueError("Windows runtime finalization requires a Windows release plan")
    _remove_windows_staged_bytecode(stage)
    runtime_identity = _windows_runtime_environment_identity(stage)
    source_release_id = provisional_manifest.get("source_release_id")
    if not re.fullmatch(r"[0-9a-f]{64}", str(source_release_id or "")):
        raise ValueError("Windows source release identity is invalid")
    release_id = _windows_release_id(
        source_release_id,
        runtime_identity["runtime_python"],
        runtime_identity["runtime_environment"],
    )
    final_root = plan.install_root.parent / release_id
    source_cfg = dict(plan.cfg)
    source_cfg.pop("runtime_root", None)
    source_cfg["python_path"] = plan.source_python_path
    actual = build_release_plan(
        plan.source_root,
        final_root,
        source_cfg,
        venv_layout="windows",
    )
    for item in actual.files:
        path = stage / item.relative_path
        if path.read_bytes() != item.content:
            _atomic_write_bytes(path, item.content, item.mode)
    manifest = json.loads(actual.manifest_bytes)
    manifest.update(
        {
            "release_id": release_id,
            "release_kind": "windows-sync",
            "source_release_id": source_release_id,
            "runtime_python": runtime_identity["runtime_python"],
            "runtime_environment": runtime_identity["runtime_environment"],
        }
    )
    manifest_bytes = _pretty_json(manifest)
    if len(manifest_bytes) > WINDOWS_RELEASE_MANIFEST_MAX_BYTES:
        raise ValueError("Windows runtime release manifest exceeds size limit")
    manifest_path = stage / "release-manifest.json"
    _atomic_write_bytes(manifest_path, manifest_bytes, 0o600)
    return ReleasePlan(
        source_root=actual.source_root,
        install_root=actual.install_root,
        cfg=actual.cfg,
        source_python_path=actual.source_python_path,
        files=actual.files,
        release_id=release_id,
        manifest_bytes=manifest_bytes,
    )


def _remove_windows_staged_bytecode(stage):
    site_packages = Path(stage) / ".venv" / "Lib" / "site-packages"
    if not site_packages.is_dir():
        raise ValueError("Windows runtime site-packages tree is missing")
    cache_directories = []
    for current, directories, files in os.walk(site_packages, topdown=True):
        current_path = Path(current)
        _assert_no_symlink_chain(current_path, stop=site_packages.parent)
        for name in directories:
            directory = current_path / name
            info = directory.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise ValueError(
                    f"Windows runtime site-packages contains a reparse point: {directory}"
                )
            if name == "__pycache__":
                cache_directories.append(directory)
        for name in files:
            if not name.endswith(".pyc"):
                continue
            path = current_path / name
            info = path.lstat()
            if (
                not stat.S_ISREG(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_nlink", 1) != 1
            ):
                raise ValueError(
                    f"Windows runtime bytecode is not a private regular file: {path}"
                )
            path.unlink()
    for directory in sorted(cache_directories, key=lambda item: len(item.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            pass


def stage_runtime(plan, command_runner=None):
    """Create and validate a complete runtime beside its final destination."""
    _require_supported_python()
    _validate_plan(plan)
    _validate_install_root(plan.install_root)
    manifest = json.loads(plan.manifest_bytes)
    windows_sync_release = manifest.get("release_kind") == "windows-sync"
    venv_layout = "windows" if windows_sync_release else None
    source_python = _validated_executable(plan.source_python_path)
    _run_checked(
        command_runner,
        (source_python, "-I", "-B", "-c", RUNTIME_PYTHON_CHECK_CODE),
        cwd=plan.source_root,
        timeout=30,
        label="runtime Python version check",
    )
    _ensure_private_directory(plan.install_root.parent)
    stage_parent = plan.install_root.parent.lstat()
    stage_parent_identity = (stage_parent.st_dev, stage_parent.st_ino)
    stage = plan.install_root.parent / (
        f".{plan.install_root.name}.staging-{plan.release_id}-{secrets.token_hex(4)}"
    )
    _assert_absent_path(stage)
    stage.mkdir(mode=0o700)
    try:
        for item in plan.files:
            target = stage / Path(item.relative_path)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            _write_new_file(target, item.content, item.mode)
        manifest_path = stage / "release-manifest.json"
        _write_new_file(manifest_path, plan.manifest_bytes, 0o600)

        if os.name == "nt" and _producer_replica_sync_enabled(plan.cfg):
            _run_checked(
                command_runner,
                (
                    source_python,
                    "-I",
                    "-B",
                    "-c",
                    _ISOLATED_MODULE_CODE,
                    "unittest",
                    str(plan.source_root),
                    *WINDOWS_SYNC_TEST_MODULES,
                ),
                cwd=plan.source_root,
                timeout=900,
                label="source Windows sync preflight",
            )
        else:
            _run_json_preflight(
                command_runner,
                (
                    source_python,
                    "-I",
                    "-B",
                    "-c",
                    _ISOLATED_SCRIPT_CODE,
                    str(plan.source_root / "scripts" / "doctor.py"),
                    str(plan.source_root / "scripts"),
                    "--profile",
                    "ci",
                    "--repo-root",
                    str(plan.source_root),
                    "--json",
                ),
                cwd=plan.source_root,
                timeout=900,
                label="source CI preflight",
            )
        venv_command = [
            source_python,
            "-I",
            "-B",
            "-m",
            "venv",
            "--copies",
        ]
        if windows_sync_release:
            venv_command.append("--without-pip")
        venv_command.append(stage / ".venv")
        _run_checked(
            command_runner,
            tuple(venv_command),
            cwd=stage,
            timeout=180,
            label="runtime virtual environment creation",
        )
        staged_python = _validated_executable(
            _venv_python(stage, platform_name=venv_layout)
        )
        if windows_sync_release:
            _run_checked(
                command_runner,
                (staged_python, "-I", "-B", "-m", "ensurepip", "--upgrade"),
                cwd=stage,
                timeout=180,
                label="runtime pip bootstrap",
            )
        _run_checked(
            command_runner,
            (
                staged_python,
                "-I",
                "-B",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-compile",
                "--requirement",
                str(stage / "scripts" / "requirements.lock"),
            ),
            cwd=stage,
            timeout=600,
            label="runtime dependency installation",
        )
        _run_checked(
            command_runner,
            (
                staged_python,
                "-I",
                "-B",
                "-m",
                "pip",
                "check",
            ),
            cwd=stage,
            timeout=120,
            label="runtime dependency consistency check",
        )
        effective_plan = plan
        if windows_sync_release:
            effective_plan = _finalize_windows_staged_runtime(plan, stage)
            manifest_path = stage / "release-manifest.json"
        if os.name == "nt" and _producer_replica_sync_enabled(effective_plan.cfg):
            _run_checked(
                command_runner,
                (
                    staged_python,
                    "-E",
                    "-s",
                    "-X",
                    "utf8",
                    "-B",
                    str(stage / "scripts" / "beacon_sync.py"),
                    "--config",
                    str(stage / "scripts" / "config.yaml"),
                    "init",
                ),
                cwd=stage,
                timeout=60,
                label="staged producer initialization",
            )
        _run_json_preflight(
            command_runner,
            (
                staged_python,
                "-E",
                "-s",
                "-X",
                "utf8",
                "-B",
                str(stage / "scripts" / "doctor.py"),
                "--profile",
                "quick",
                "--repo-root",
                str(stage),
                "--json",
            ),
            cwd=stage,
            timeout=180,
            label="staged quick preflight",
        )
        _verify_staged_files(effective_plan, stage)
        return StagedRuntime(
            stage,
            effective_plan.release_id,
            manifest_path,
            effective_plan,
        )
    except Exception:
        _remove_tree(stage, expected_parent_identity=stage_parent_identity)
        raise


def verify_release(plan, command_runner=None):
    """Build and validate a fresh runtime without switching live bindings."""
    staged = stage_runtime(plan, command_runner=command_runner)
    stage_parent = staged.root.parent.lstat()
    stage_parent_identity = (stage_parent.st_dev, stage_parent.st_ino)
    try:
        effective_plan = staged.final_plan or plan
        _validate_staged_runtime(plan, staged)
        return ReleaseVerification(
            action="verified",
            release_id=effective_plan.release_id,
            file_count=len(effective_plan.files),
        )
    finally:
        _remove_tree(
            staged.root,
            expected_parent_identity=stage_parent_identity,
        )


def apply_runtime(plan, staged, command_runner=None):
    """Serialize one install transaction against apply and manual rollback."""
    _require_supported_python()
    _validate_plan(plan)
    effective_plan = staged.final_plan if isinstance(staged, StagedRuntime) else None
    effective_plan = effective_plan or plan
    with _runtime_transaction_locks(
        effective_plan.cfg,
        effective_plan.install_root,
    ):
        return _apply_runtime_locked(effective_plan, staged, command_runner)


def _apply_runtime_locked(plan, staged, command_runner=None):
    """Publish a staged runtime and switch every live binding transactionally."""
    _validate_plan(plan)
    _validate_staged_runtime(plan, staged)
    _validate_install_root(plan.install_root)
    install_parent = plan.install_root.parent.lstat()
    install_parent_identity = (install_parent.st_dev, install_parent.st_ino)
    external = _external_paths(plan.cfg)
    for path in external.values():
        _assert_external_target(path)
    services_before = _service_states(external, command_runner)
    _assert_no_orphaned_services(external, services_before)
    required_parent_names = {
        "hooks",
        "agents",
        "claude_settings",
        "harvest",
        "sync",
        "weekly",
        "legacy_harvest",
        "legacy_weekly",
    }
    for parent in sorted(
        {path.parent for name, path in external.items() if name in required_parent_names}
    ):
        _ensure_private_directory(parent)
    operation_id = _operation_id()
    rollback_root = plan.install_root.parent / "rollback" / operation_id
    _ensure_private_directory(rollback_root)
    snapshot_root = rollback_root / "snapshots"
    snapshot_root.mkdir(mode=0o700)
    snapshots = _snapshot_external_files(external, snapshot_root)
    previous_path = plan.install_root.parent / f".{plan.install_root.name}.previous-{operation_id}"
    _assert_absent_path(previous_path)
    manifest_path = rollback_root / "manifest.json"
    manifest = {
        "schema_version": ROLLBACK_SCHEMA_VERSION,
        "generated_by": "agent_memory_beacon",
        "operation_id": operation_id,
        "status": "prepared",
        "release_id": plan.release_id,
        "install_root": str(plan.install_root),
        "user_home": str(_user_home(plan.cfg)),
        "codex_home": str(_codex_home(plan.cfg)),
        "vault_path": str(_absolute_path(plan.cfg["vault_path"])),
        "context_targets": [
            str(path) for path in _configured_context_paths(plan.cfg)
        ],
        "codex_profile_path": str(_configured_profile_path(plan.cfg) or ""),
        "transcript_agents": list(plan.cfg.get("transcript_agents") or []),
        "authority_sync_enabled": _authority_sync_enabled(plan.cfg),
        "previous_runtime_path": str(previous_path),
        "previous_runtime_existed": plan.install_root.exists(),
        "install_parent_identity": list(install_parent_identity),
        "external_before": snapshots,
        "services_before": services_before,
        "external_after": [],
        "rollback_progress": {},
    }
    _atomic_write_json(manifest_path, manifest, mode=0o600)
    prior_existed = bool(manifest["previous_runtime_existed"])
    actions = []
    try:
        manifest["status"] = "publishing_runtime"
        _atomic_write_json(manifest_path, manifest, mode=0o600)
        if prior_existed:
            _durable_replace(plan.install_root, previous_path)
            manifest["status"] = "previous_runtime_staged"
            _atomic_write_json(manifest_path, manifest, mode=0o600)
        _durable_replace(staged.root, plan.install_root)
        manifest["status"] = "runtime_published"
        _atomic_write_json(manifest_path, manifest, mode=0o600)

        scripts_dir = plan.install_root / "scripts"
        hook_actions = install_hooks(
            plan.cfg,
            scripts_dir=scripts_dir,
            create_backups=False,
            migration_scripts_dir=plan.source_root / "scripts",
        )
        actions.extend(hook_actions)
        if _claude_collection_enabled(plan.cfg):
            actions.extend(
                install_claude_hooks(
                    plan.cfg,
                    scripts_dir=scripts_dir,
                    create_backups=False,
                    migration_scripts_dir=plan.source_root / "scripts",
                )
            )
            actions.extend(
                install_claude_patch(
                    external["claude_context"],
                    patch_path=plan.install_root
                    / "patches"
                    / "AGENT_MEMORY_BEACON.md.patch",
                    create_backups=False,
                )
            )
        actions.extend(
            install_agents_patch(
                external["agents"],
                patch_path=plan.install_root
                / "patches"
                / "AGENT_MEMORY_BEACON.md.patch",
                create_backups=False,
            )
        )
        actions.extend(
            install_launch_agents(
                plan.cfg,
                home=_user_home(plan.cfg),
                command_runner=command_runner,
                scripts_dir=scripts_dir,
                initialize_baseline=False,
            )
        )
        if _authority_sync_enabled(plan.cfg):
            sync_result = install_macos_scheduler(
                python_path=_venv_python(plan.install_root),
                script_path=scripts_dir / "beacon_sync.py",
                config_path=scripts_dir / "config.yaml",
                log_dir=Path(plan.cfg["vault_path"]) / "04-Feedback" / "_logs",
                home=_user_home(plan.cfg),
                interval_seconds=60,
                command_runner=command_runner,
            )
            actions.append(
                "{} sync scheduler {}".format(
                    "UPDATED" if sync_result["changed"] else "UNCHANGED",
                    sync_result["path"],
                )
            )
        else:
            if services_before.get("sync") or os.path.lexists(external["sync"]):
                sync_result = install_macos_scheduler(
                    python_path=_venv_python(plan.install_root),
                    script_path=scripts_dir / "beacon_sync.py",
                    config_path=scripts_dir / "config.yaml",
                    log_dir=Path(plan.cfg["vault_path"])
                    / "04-Feedback"
                    / "_logs",
                    home=_user_home(plan.cfg),
                    interval_seconds=60,
                    uninstall=True,
                    command_runner=command_runner,
                )
            else:
                sync_result = {
                    "changed": False,
                    "path": str(external["sync"]),
                }
            actions.append(
                "{} disabled sync scheduler {}".format(
                    "REMOVED" if sync_result["changed"] else "ABSENT",
                    sync_result["path"],
                )
            )
        _run_index_rebuild(plan.install_root, command_runner)
        _run_context_compile(plan.install_root, command_runner)
        _run_live_preflight(plan.install_root, command_runner)
        manifest["external_after"] = _current_external_state(external)
        manifest["status"] = "installed"
        _atomic_write_json(manifest_path, manifest, mode=0o600)
        trust_review = any(
            action.startswith(("ADD hooks.", "UPDATE hooks."))
            for action in hook_actions
        )
        return ReleaseResult(
            action="upgraded" if prior_existed else "installed",
            install_root=plan.install_root,
            manifest_path=manifest_path,
            release_id=plan.release_id,
            trust_review_required=trust_review,
            actions=tuple(actions),
        )
    except Exception as exc:
        rollback_errors = _restore_transaction(
            manifest,
            manifest_path,
            command_runner,
            verify_current=False,
        )
        manifest["status"] = "rollback_failed" if rollback_errors else "rolled_back"
        manifest["failure"] = str(exc)
        if rollback_errors:
            manifest["rollback_errors"] = rollback_errors
        _atomic_write_json(manifest_path, manifest, mode=0o600)
        if rollback_errors:
            raise RuntimeError(
                f"runtime installation failed: {exc}; automatic rollback incomplete: "
                + "; ".join(rollback_errors)
            ) from exc
        raise RuntimeError(f"runtime installation failed: {exc}") from exc


def rollback_runtime(manifest_path, command_runner=None):
    """Serialize rollback and revalidate its manifest after acquiring the lock."""
    _require_supported_python()
    manifest_path = _absolute_path(manifest_path)
    manifest = _load_rollback_manifest(manifest_path)
    _validate_rollback_manifest(manifest, manifest_path)
    expected_install_root = _absolute_path(manifest["install_root"])
    lock_cfg = {"vault_path": manifest["vault_path"]}
    with _runtime_transaction_locks(lock_cfg, expected_install_root):
        return _rollback_runtime_locked(
            manifest_path,
            command_runner,
            expected_install_root=expected_install_root,
        )


def _rollback_runtime_locked(
    manifest_path,
    command_runner=None,
    expected_install_root=None,
):
    """Restore the exact pre-install runtime and external binding bytes."""
    manifest_path = _absolute_path(manifest_path)
    manifest = _load_rollback_manifest(manifest_path)
    _validate_rollback_manifest(manifest, manifest_path)
    install_root = _absolute_path(manifest["install_root"])
    if expected_install_root is not None and install_root != expected_install_root:
        raise RuntimeError("rollback manifest changed while acquiring installation lock")
    status = manifest.get("status")
    if status != "installed" and status not in ROLLBACK_RECOVERABLE_STATUSES:
        raise RuntimeError(
            f"rollback manifest is not recoverable: {status}"
        )
    rollback_started = bool(manifest.get("rollback_progress"))
    if status == "installed" and not rollback_started:
        drift = _external_drift(manifest)
        if drift:
            raise RuntimeError(
                "external file changed after installation: " + "; ".join(drift)
            )
        release_manifest = install_root / "release-manifest.json"
        try:
            current_release = json.loads(release_manifest.read_text(encoding="utf-8"))
        except Exception as exc:
            raise RuntimeError("installed runtime changed after installation") from exc
        if current_release.get("release_id") != manifest.get("release_id"):
            raise RuntimeError("installed runtime changed after installation")

    errors = _restore_transaction(
        manifest,
        manifest_path,
        command_runner,
        verify_current=True,
    )
    manifest["status"] = "rollback_failed" if errors else "rolled_back_manual"
    if errors:
        manifest["rollback_errors"] = errors
    else:
        manifest.pop("rollback_errors", None)
    _atomic_write_json(manifest_path, manifest, mode=0o600)
    if errors:
        raise RuntimeError("manual rollback incomplete: " + "; ".join(errors))
    return ReleaseResult(
        action="rolled-back",
        install_root=install_root,
        manifest_path=manifest_path,
        release_id=str(manifest["release_id"]),
    )


def _runtime_config(
    cfg,
    install_root,
    source_root=None,
    *,
    venv_layout=None,
):
    rendered = {}
    for key in RUNTIME_CONFIG_SCALAR_FIELDS:
        if key in cfg:
            rendered[key] = _runtime_scalar(cfg[key], key)
    for key in RUNTIME_CONFIG_STRING_LIST_FIELDS:
        if key in cfg:
            rendered[key] = _runtime_string_list(cfg[key], key)
    for key in RUNTIME_CONFIG_STRING_MAP_FIELDS:
        if key in cfg:
            rendered[key] = _runtime_string_map(cfg[key], key)
    if "projects" in cfg:
        rendered["projects"] = _runtime_projects(cfg["projects"])
    for section, allowed_fields in RUNTIME_CONFIG_SECTION_FIELDS.items():
        if section not in cfg:
            continue
        source = cfg[section]
        if not isinstance(source, dict):
            raise ValueError(f"runtime configuration {section} must be a mapping")
        rendered[section] = {}
        for key in allowed_fields:
            if key not in source:
                continue
            field = f"{section}.{key}"
            value = source[key]
            if section == "beacon_sync" and key == "inboxes":
                rendered[section][key] = _runtime_sync_inboxes(value, field)
            else:
                rendered[section][key] = (
                    _runtime_scalar_list(value, field)
                    if isinstance(value, (list, tuple))
                    else _runtime_scalar(value, field)
                )
    _reject_credential_urls(rendered)
    if source_root is not None:
        source_root = _absolute_path(source_root)
        checkout_bindings = [*_configured_context_paths(rendered)]
        profile_path = _configured_profile_path(rendered)
        if profile_path is not None:
            checkout_bindings.append(profile_path)
        if any(_is_within(path, source_root) for path in checkout_bindings):
            raise ValueError(
                "stable runtime context/profile target must not be inside the source checkout"
            )
    rendered["python_path"] = str(
        _venv_python(install_root, platform_name=venv_layout)
    )
    rendered["runtime_root"] = str(install_root)
    return rendered


def _runtime_scalar(value, field):
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError(f"runtime configuration {field} must be a scalar")


def _runtime_scalar_list(value, field):
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"runtime configuration {field} must be a list")
    return [
        _runtime_scalar(item, f"{field}[{index}]")
        for index, item in enumerate(value)
    ]


def _runtime_sync_inboxes(value, field):
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"runtime configuration {field} must be a list")
    rendered = []
    seen = set()
    for index, item in enumerate(value):
        if not isinstance(item, dict) or set(item) != {"device_id", "path"}:
            raise ValueError(
                f"runtime configuration {field}[{index}] is invalid"
            )
        device_id = item["device_id"]
        path = item["path"]
        if (
            not isinstance(device_id, str)
            or not device_id
            or not isinstance(path, str)
        ):
            raise ValueError(
                f"runtime configuration {field}[{index}] is invalid"
            )
        if device_id in seen:
            raise ValueError(
                f"runtime configuration {field} has duplicate device_id"
            )
        seen.add(device_id)
        rendered.append({"device_id": device_id, "path": path})
    return rendered


def _runtime_string_list(value, field):
    if not isinstance(value, (list, tuple)) or not all(
        isinstance(item, str) for item in value
    ):
        raise ValueError(f"runtime configuration {field} must be a string list")
    return list(value)


def _runtime_string_map(value, field):
    if not isinstance(value, dict):
        raise ValueError(f"runtime configuration {field} must be a mapping")
    rendered = {}
    for key, child in value.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"runtime configuration {field} has an invalid key")
        child_field = f"{field}.{key}"
        if isinstance(child, str):
            rendered[key] = [child]
        elif isinstance(child, (list, tuple)) and all(
            isinstance(item, str) for item in child
        ):
            rendered[key] = list(child)
        else:
            raise ValueError(
                f"runtime configuration {child_field} must be a string or string list"
            )
    return rendered


def _runtime_projects(value):
    if not isinstance(value, (list, tuple)):
        raise ValueError("runtime configuration projects must be a list")
    rendered = []
    for index, item in enumerate(value):
        field = f"projects[{index}]"
        if isinstance(item, str):
            rendered.append(item)
            continue
        if not isinstance(item, dict):
            raise ValueError(
                f"runtime configuration {field} must be a string or project record"
            )
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"runtime configuration {field}.name must be a string")
        keywords = item.get("keywords", [])
        if not isinstance(keywords, (list, tuple)) or not all(
            isinstance(keyword, str) for keyword in keywords
        ):
            raise ValueError(
                f"runtime configuration {field}.keywords must be a string list"
            )
        rendered.append({"name": name, "keywords": list(keywords)})
    return rendered


def _reject_credential_urls(value, field=""):
    if isinstance(value, dict):
        for key, child in value.items():
            child_field = f"{field}.{key}" if field else str(key)
            _reject_credential_urls(child, child_field)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _reject_credential_urls(child, f"{field}[{index}]")
        return
    if not isinstance(value, str):
        return
    try:
        parsed = urlsplit(value)
    except ValueError:
        if "://" in value or value.startswith("//"):
            raise ValueError(
                f"runtime configuration {field} contains an invalid URL"
            ) from None
        return
    if not parsed.scheme and not value.startswith("//"):
        return
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            f"runtime configuration {field} contains a credential-bearing URL"
        )


def _load_rollback_manifest(manifest_path):
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("rollback manifest must be a regular non-symlink file")
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("rollback manifest cannot be read") from exc


@contextmanager
def _runtime_transaction_locks(cfg, install_root):
    vault = str(_absolute_path(cfg["vault_path"]))
    harvester_lock = Path(
        safe_vault_path(vault, "04-Feedback", "_logs", "harvester.lock")
    )
    ensure_directory_tree(harvester_lock.parent, vault)
    install_lock = install_root.parent / ".install.lock"
    with exclusive_file_lock(install_lock):
        with exclusive_file_lock(harvester_lock, root=vault):
            yield


def _source_release_file(source_root, source, relative_path):
    _assert_path_under(source, source_root)
    _assert_no_symlink_chain(source, stop=source_root.parent)
    current = source.lstat()
    if not stat.S_ISREG(current.st_mode):
        raise ValueError(f"runtime source is not a regular file: {source}")
    content = source.read_bytes()
    mode = stat.S_IMODE(current.st_mode) & 0o755
    if not mode:
        mode = 0o644
    return ReleaseFile(relative_path, content, mode)


def _regular_file_size_sha256(path, label):
    path = Path(path)
    digest = hashlib.sha256()
    total = 0
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise ValueError(f"{label} is not a regular file: {path}")
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise ValueError(f"{label} cannot be read: {path}") from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_identity != after_identity or total != after.st_size:
        raise ValueError(f"{label} changed while reading: {path}")
    return total, digest.hexdigest()


def _read_regular_file_bounded(path, label, max_bytes):
    path = Path(path)
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size > max_bytes
            ):
                raise ValueError(f"{label} is not a bounded regular file: {path}")
            content = handle.read(max_bytes + 1)
            after = os.fstat(handle.fileno())
    except OSError as exc:
        raise ValueError(f"{label} cannot be read: {path}") from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if (
        before_identity != after_identity
        or len(content) != after.st_size
        or len(content) > max_bytes
    ):
        raise ValueError(f"{label} changed while reading: {path}")
    return content


def _validate_plan(plan):
    if not isinstance(plan, ReleasePlan):
        raise TypeError("release plan has an invalid type")
    expected = json.loads(plan.manifest_bytes)
    if expected.get("release_id") != plan.release_id:
        raise ValueError("release plan manifest does not match release ID")
    if [item.relative_path for item in plan.files] != sorted(
        item.relative_path for item in plan.files
    ):
        raise ValueError("release files are not ordered")


def _verify_staged_files(plan, stage):
    for item in plan.files:
        path = stage / item.relative_path
        _assert_path_under(path, stage)
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"staged runtime file missing: {item.relative_path}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item.sha256:
            raise RuntimeError(f"staged runtime file changed: {item.relative_path}")
    if (stage / "release-manifest.json").read_bytes() != plan.manifest_bytes:
        raise RuntimeError("staged release manifest changed")
    manifest = json.loads(plan.manifest_bytes)
    if manifest.get("release_kind") == "windows-sync":
        try:
            if "runtime_environment" in manifest:
                actual_runtime_identity = (
                    _windows_runtime_environment_identity(stage)
                )
            else:
                actual_runtime_identity = {
                    "runtime_python": _windows_runtime_python_identity(
                        _venv_python(stage, platform_name="windows")
                    )
                }
        except ValueError as exc:
            raise RuntimeError("staged runtime Python cannot be verified") from exc
        if (
            actual_runtime_identity["runtime_python"]
            != manifest.get("runtime_python")
        ):
            raise RuntimeError("staged runtime Python changed")
        if (
            "runtime_environment" in manifest
            and actual_runtime_identity.get("runtime_environment")
            != manifest["runtime_environment"]
        ):
            raise RuntimeError("staged Windows runtime environment changed")


def _validate_staged_runtime(plan, staged):
    if not isinstance(staged, StagedRuntime):
        raise ValueError("staged runtime does not match release plan")
    effective_plan = staged.final_plan or plan
    if staged.release_id != effective_plan.release_id:
        raise ValueError("staged runtime does not match release plan")
    if staged.final_plan is not None and (
        staged.final_plan.source_root != plan.source_root
        or staged.final_plan.install_root.parent != plan.install_root.parent
    ):
        raise ValueError("finalized staged runtime does not match release plan")
    _assert_path_under(staged.root, effective_plan.install_root.parent)
    if staged.root.is_symlink() or not staged.root.is_dir():
        raise ValueError("staged runtime is missing or symlinked")
    _verify_staged_files(effective_plan, staged.root)
    manifest = json.loads(effective_plan.manifest_bytes)
    layout = "windows" if manifest.get("release_kind") == "windows-sync" else None
    _validated_executable(
        _venv_python(staged.root, platform_name=layout)
    )


def _external_paths(cfg):
    home = _user_home(cfg)
    codex_home = _codex_home(cfg)
    launch_agents = home / "Library" / "LaunchAgents"
    paths = {
        "hooks": codex_home / "hooks.json",
        "agents": codex_home / "AGENTS.md",
    }
    if _claude_collection_enabled(cfg):
        paths["claude_settings"] = home / ".claude" / "settings.json"
        paths["claude_context"] = _configured_claude_context_path(cfg)
    for kind in ("harvest", "weekly"):
        paths[kind] = launch_agents / f"{NEW_LAUNCHD_LABELS[kind]}.plist"
        paths[f"legacy_{kind}"] = launch_agents / f"{LEGACY_LAUNCHD_LABELS[kind]}.plist"
    paths["sync"] = launch_agents / f"{SYNC_LAUNCHD_LABEL}.plist"
    seen = set(paths.values())
    context_index = 0
    for target in _configured_context_paths(cfg):
        if target in seen:
            continue
        paths[f"context_{context_index:03d}"] = target
        context_index += 1
        seen.add(target)
    profile_path = _configured_profile_path(cfg)
    if profile_path is not None:
        shared_agents = profile_path / SHARED_AGENTS
        if shared_agents not in seen:
            paths["profile_agents"] = shared_agents
    return paths


def _authority_sync_enabled(cfg):
    section = cfg.get("beacon_sync") or {}
    if not isinstance(section, dict):
        raise ValueError("runtime beacon_sync must be a mapping")
    return bool(section.get("enabled")) and section.get("role") == "authority"


def _producer_replica_sync_enabled(cfg):
    section = cfg.get("beacon_sync") or {}
    if not isinstance(section, dict):
        raise ValueError("runtime beacon_sync must be a mapping")
    return (
        bool(section.get("enabled"))
        and section.get("role") == "producer-replica"
    )


def _service_label(kind):
    if kind == "sync":
        return SYNC_LAUNCHD_LABEL
    if kind in NEW_LAUNCHD_LABELS:
        return NEW_LAUNCHD_LABELS[kind]
    if kind.startswith("legacy_"):
        legacy_kind = kind.removeprefix("legacy_")
        if legacy_kind in LEGACY_LAUNCHD_LABELS:
            return LEGACY_LAUNCHD_LABELS[legacy_kind]
    raise ValueError(f"unknown managed launchd service kind: {kind}")


def _managed_service_kinds(paths):
    ordered = ("harvest", "weekly", "sync", "legacy_harvest", "legacy_weekly")
    return tuple(kind for kind in ordered if kind in paths)


def _claude_collection_enabled(cfg):
    agents = cfg.get("transcript_agents") or []
    if not isinstance(agents, (list, tuple)):
        raise ValueError("runtime transcript_agents must be a list")
    return "claude" in agents


def _configured_claude_context_path(cfg):
    value = cfg.get("claude_md_path")
    if value:
        if not isinstance(value, str):
            raise ValueError("runtime claude_md_path must be a string")
        return _absolute_path(value)
    return _user_home(cfg) / ".claude" / "CLAUDE.md"


def _configured_context_paths(cfg):
    configured = cfg.get("context_targets") or []
    if not isinstance(configured, (list, tuple)):
        raise ValueError("runtime context_targets must be a list")
    values = [*configured]
    if cfg.get("claude_md_path"):
        values.append(cfg["claude_md_path"])
    paths = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise ValueError("runtime context target must be a non-empty string")
        path = _absolute_path(value)
        if path not in paths:
            paths.append(path)
    return paths


def _configured_profile_path(cfg):
    value = cfg.get("codex_profile_path")
    if not value:
        return None
    if not isinstance(value, str):
        raise ValueError("runtime codex_profile_path must be a string")
    return _absolute_path(value)


def _user_home(cfg):
    return _absolute_path(cfg.get("user_home") or Path.home())


def _codex_home(cfg):
    return _absolute_path(cfg.get("codex_home") or _user_home(cfg) / ".codex")


def _external_parent_identity(path):
    parent = Path(path).parent
    if not os.path.lexists(parent):
        return None
    current = parent.lstat()
    if not stat.S_ISDIR(current.st_mode):
        raise ValueError(f"external target parent is not a directory: {parent}")
    return [current.st_dev, current.st_ino]


def _snapshot_external_files(paths, snapshot_root):
    rows = []
    for index, (name, path) in enumerate(sorted(paths.items())):
        _assert_external_target(path)
        existed = os.path.lexists(path)
        row = {
            "name": name,
            "path": str(path),
            "existed": bool(existed),
            "parent_identity": _external_parent_identity(path),
        }
        if existed:
            current = path.lstat()
            if not stat.S_ISREG(current.st_mode):
                raise ValueError(f"external target is not a regular file: {path}")
            data = path.read_bytes()
            backup = snapshot_root / f"external-{index:02d}.bin"
            _write_new_file(backup, data, 0o600)
            row.update(
                {
                    "mode": stat.S_IMODE(current.st_mode),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "backup": str(backup.relative_to(snapshot_root.parent)),
                }
            )
        rows.append(row)
    return rows


def _current_external_state(paths):
    rows = []
    for name, path in sorted(paths.items()):
        _assert_external_target(path)
        existed = os.path.lexists(path)
        row = {
            "name": name,
            "path": str(path),
            "existed": bool(existed),
            "parent_identity": _external_parent_identity(path),
        }
        if existed:
            current = path.lstat()
            if not stat.S_ISREG(current.st_mode):
                raise ValueError(f"external target is not a regular file: {path}")
            data = path.read_bytes()
            row.update(
                {
                    "mode": stat.S_IMODE(current.st_mode),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        rows.append(row)
    return rows


def _service_states(paths, command_runner):
    states = {}
    for kind in _managed_service_kinds(paths):
        label = _service_label(kind)
        try:
            result = _invoke(
                command_runner,
                ("/bin/launchctl", "print", f"gui/{os.getuid()}/{label}"),
                cwd=Path.home(),
                timeout=15,
            )
        except Exception as exc:
            raise RuntimeError(
                f"launchd state query failed for {label}: {exc}"
            ) from exc
        if result.returncode == 0:
            states[kind] = True
            continue
        if _missing_service(result):
            states[kind] = False
            continue
        raise RuntimeError(
            f"launchd state query failed for {label}: {_result_detail(result)}"
        )
    return states


def _assert_no_orphaned_services(paths, states):
    for kind, loaded in states.items():
        path = paths[kind]
        if not loaded or os.path.lexists(path):
            continue
        label = _service_label(kind)
        raise RuntimeError(
            f"launchd service {label} is loaded but its plist is missing: {path}"
        )


def _run_live_preflight(install_root, command_runner=None):
    python = _validated_executable(_venv_python(install_root))
    _run_json_preflight(
        command_runner,
        (
            python,
            "-B",
            str(install_root / "scripts" / "doctor.py"),
            "--profile",
            "live",
            "--repo-root",
            str(install_root),
            "--json",
        ),
        cwd=install_root,
        timeout=300,
        label="live preflight",
    )


def _run_index_rebuild(install_root, command_runner=None):
    python = _validated_executable(_venv_python(install_root))
    _run_checked(
        command_runner,
        (
            python,
            "-B",
            "-c",
            INDEX_REBUILD_CODE,
            str(install_root / "scripts"),
        ),
        cwd=install_root,
        timeout=300,
        label="memory index rebuild",
    )


def _run_context_compile(install_root, command_runner=None):
    python = _validated_executable(_venv_python(install_root))
    _run_checked(
        command_runner,
        (
            python,
            "-B",
            "-c",
            CONTEXT_COMPILE_CODE,
            str(install_root / "scripts"),
        ),
        cwd=install_root,
        timeout=300,
        label="agent context compilation",
    )


def _restore_transaction(manifest, manifest_path, command_runner, verify_current):
    progress = manifest.setdefault("rollback_progress", {})
    errors = []
    steps = (
        (
            "managed_jobs_unloaded",
            "unload current launchd jobs",
            lambda _resuming: _bootout_managed_jobs(manifest, command_runner),
        ),
        (
            "external_restored",
            "restore external files",
            lambda _resuming: _restore_external_files(manifest, manifest_path),
        ),
        (
            "runtime_restored",
            "restore runtime",
            lambda resuming: _restore_runtime_tree(
                manifest,
                reconcile_started=resuming,
            ),
        ),
        (
            "services_restored",
            "restore launchd services",
            lambda _resuming: _restore_loaded_services(manifest, command_runner),
        ),
    )
    for step, label, operation in steps:
        state = progress.get(step)
        if state is True or state == "complete":
            continue
        resuming = state == "started"
        if not resuming:
            try:
                progress[step] = "started"
                _atomic_write_json(manifest_path, manifest, mode=0o600)
            except Exception as exc:
                progress.pop(step, None)
                errors.append(f"checkpoint {label}: {exc}")
                continue
        try:
            operation(resuming)
            progress[step] = "complete"
            _atomic_write_json(manifest_path, manifest, mode=0o600)
        except Exception as exc:
            progress[step] = "started"
            errors.append(f"{label}: {exc}")
    return errors


def _bootout_managed_jobs(manifest, command_runner):
    errors = []
    domain = f"gui/{os.getuid()}"
    for kind in manifest["services_before"]:
        if kind.startswith("legacy_"):
            continue
        label = _service_label(kind)
        result = _invoke(
            command_runner,
            ("/bin/launchctl", "bootout", f"{domain}/{label}"),
            cwd=Path.home(),
            timeout=15,
        )
        if result.returncode and not _missing_service(result):
            errors.append(_result_detail(result))
    if errors:
        raise RuntimeError("; ".join(errors))


def _restore_external_files(manifest, manifest_path):
    after = {
        row.get("name"): row
        for row in manifest.get("external_after", [])
        if isinstance(row, dict)
    }
    for row in manifest["external_before"]:
        path = Path(row["path"])
        _assert_external_target(path)
        expected_parent_identity = (
            after.get(row["name"], {}).get("parent_identity")
            or row.get("parent_identity")
        )
        if row["existed"]:
            backup = manifest_path.parent / row["backup"]
            _assert_path_under(backup, manifest_path.parent)
            if backup.is_symlink() or not backup.is_file():
                raise RuntimeError(f"rollback snapshot missing: {backup}")
            data = backup.read_bytes()
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise RuntimeError(f"rollback snapshot changed: {backup}")
            if expected_parent_identity is None:
                raise RuntimeError(
                    f"rollback parent identity is unavailable: {path.parent}"
                )
            durable_atomic_write(
                path,
                data,
                mode=int(row["mode"]),
                expected_parent_identity=expected_parent_identity,
                preserve_existing_mode=False,
            )
        elif os.path.lexists(path):
            current = path.lstat()
            if not stat.S_ISREG(current.st_mode):
                raise RuntimeError(f"refusing to remove non-file external target: {path}")
            if expected_parent_identity is None:
                raise RuntimeError(
                    f"rollback parent identity is unavailable: {path.parent}"
                )
            durable_unlink(
                path,
                expected_identity=(current.st_dev, current.st_ino),
                expected_parent_identity=expected_parent_identity,
            )


def _restore_runtime_tree(manifest, reconcile_started=False):
    install_root = Path(manifest["install_root"])
    previous = Path(manifest["previous_runtime_path"])
    _validate_install_root(install_root)
    _assert_path_under(previous, install_root.parent)
    failed = install_root.parent / f".{install_root.name}.failed-{manifest['operation_id']}"
    parent_identity = tuple(manifest["install_parent_identity"])
    failed_available = os.path.lexists(failed)
    if failed_available and not reconcile_started:
        raise RuntimeError(f"failed-runtime quarantine already exists: {failed}")
    previous_existed = bool(manifest["previous_runtime_existed"])
    previous_available = os.path.lexists(previous)
    current_available = os.path.lexists(install_root)
    if previous_existed and not previous_available:
        if reconcile_started and current_available:
            if install_root.is_symlink() or not install_root.is_dir():
                raise RuntimeError("restored runtime is not a regular directory")
            if failed_available:
                _remove_tree(failed, expected_parent_identity=parent_identity)
            return
        if current_available and manifest.get("status") in {
            "prepared",
            "publishing_runtime",
        }:
            return
        raise RuntimeError("previous runtime is missing")
    if current_available and failed_available:
        raise RuntimeError("runtime rollback paths are ambiguous")
    if current_available:
        if install_root.is_symlink() or not install_root.is_dir():
            raise RuntimeError("installed runtime is not a regular directory")
        _durable_replace(install_root, failed)
    try:
        if previous_existed:
            if previous.is_symlink() or not previous.is_dir():
                raise RuntimeError("previous runtime is missing")
            _durable_replace(previous, install_root)
    except Exception:
        if failed.exists() and not install_root.exists():
            _durable_replace(failed, install_root)
        raise
    if os.path.lexists(failed):
        _remove_tree(failed, expected_parent_identity=parent_identity)


def _restore_loaded_services(manifest, command_runner):
    before = {row["name"]: row for row in manifest["external_before"]}
    errors = []
    domain = f"gui/{os.getuid()}"
    for kind, loaded in manifest["services_before"].items():
        if not loaded:
            continue
        row = before[kind]
        if not row["existed"]:
            continue
        label = _service_label(kind)
        query = _invoke(
            command_runner,
            ("/bin/launchctl", "print", f"{domain}/{label}"),
            cwd=Path.home(),
            timeout=15,
        )
        if query.returncode == 0:
            continue
        if not _missing_service(query):
            errors.append(_result_detail(query))
            continue
        result = _invoke(
            command_runner,
            ("/bin/launchctl", "bootstrap", domain, row["path"]),
            cwd=Path(row["path"]).parent,
            timeout=15,
        )
        if result.returncode:
            errors.append(_result_detail(result))
    if errors:
        raise RuntimeError("; ".join(errors))


def _external_drift(manifest):
    expected = {row["name"]: row for row in manifest.get("external_after", [])}
    errors = []
    for name, row in expected.items():
        path = Path(row["path"])
        _assert_external_target(path)
        exists = os.path.lexists(path)
        if exists != row["existed"]:
            errors.append(f"{name} existence changed")
            continue
        if exists:
            current = path.lstat()
            if not stat.S_ISREG(current.st_mode):
                errors.append(f"{name} type changed")
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != row["sha256"]:
                errors.append(f"{name} content changed")
    return errors


def _validate_rollback_manifest(manifest, manifest_path):
    required = {
        "schema_version",
        "generated_by",
        "operation_id",
        "status",
        "release_id",
        "install_root",
        "user_home",
        "codex_home",
        "vault_path",
        "previous_runtime_path",
        "previous_runtime_existed",
        "install_parent_identity",
        "external_before",
        "services_before",
        "external_after",
    }
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise ValueError("rollback manifest is incomplete")
    schema_version = manifest.get("schema_version")
    if (
        schema_version not in SUPPORTED_ROLLBACK_SCHEMA_VERSIONS
        or manifest["generated_by"] != "agent_memory_beacon"
    ):
        raise ValueError("rollback manifest identity is invalid")
    if schema_version >= 2 and not {
        "context_targets",
        "codex_profile_path",
    }.issubset(manifest):
        raise ValueError("rollback manifest is incomplete")
    if schema_version >= 3 and "authority_sync_enabled" not in manifest:
        raise ValueError("rollback manifest is incomplete")
    install_root = _absolute_path(manifest["install_root"])
    user_home = _absolute_path(manifest["user_home"])
    codex_home = _absolute_path(manifest["codex_home"])
    vault = _absolute_path(manifest["vault_path"])
    if not vault.is_dir():
        raise ValueError("rollback manifest vault path is invalid")
    safe_vault_path(str(vault), "04-Feedback", "_logs", "harvester.lock")
    expected_manifest = (
        install_root.parent
        / "rollback"
        / str(manifest["operation_id"])
        / "manifest.json"
    )
    if manifest_path != expected_manifest:
        raise ValueError("rollback manifest path is outside its operation directory")
    path_cfg = {
        "user_home": str(user_home),
        "codex_home": str(codex_home),
    }
    authority_sync_enabled = (
        manifest.get("authority_sync_enabled") if schema_version >= 3 else False
    )
    if type(authority_sync_enabled) is not bool:
        raise ValueError("rollback manifest sync binding is invalid")
    if authority_sync_enabled:
        path_cfg["beacon_sync"] = {
            "enabled": True,
            "role": "authority",
        }
    if schema_version >= 2:
        context_targets = manifest.get("context_targets")
        profile_path = manifest.get("codex_profile_path")
        transcript_agents = manifest.get("transcript_agents")
        if transcript_agents is None:
            transcript_agents = (
                ["claude"]
                if any(
                    isinstance(row, dict)
                    and row.get("name") == "claude_settings"
                    for row in manifest.get("external_before", [])
                )
                else []
            )
        if (
            not isinstance(context_targets, list)
            or not all(
                isinstance(path, str)
                and path
                and os.path.isabs(path)
                and str(_absolute_path(path)) == path
                for path in context_targets
            )
            or not isinstance(profile_path, str)
            or not isinstance(transcript_agents, list)
            or not all(
                isinstance(agent, str) and agent
                for agent in transcript_agents
            )
            or (profile_path and (
                not os.path.isabs(profile_path)
                or str(_absolute_path(profile_path)) != profile_path
            ))
        ):
            raise ValueError("rollback manifest context binding is invalid")
        path_cfg["context_targets"] = context_targets
        path_cfg["codex_profile_path"] = profile_path
        path_cfg["transcript_agents"] = transcript_agents
    expected_paths = _external_paths(path_cfg)
    if schema_version < 3:
        expected_paths.pop("sync", None)
    status = manifest.get("status")
    if status not in {"installed", *ROLLBACK_RECOVERABLE_STATUSES}:
        raise ValueError("rollback manifest status is invalid")
    before = _validate_external_manifest_rows(
        manifest["external_before"],
        expected_paths,
        manifest_path,
        snapshot=True,
        require_complete=True,
    )
    after = _validate_external_manifest_rows(
        manifest["external_after"],
        expected_paths,
        manifest_path,
        snapshot=False,
        require_complete=status == "installed",
    )
    for name in expected_paths:
        before_parent = before[name].get("parent_identity")
        after_parent = after.get(name, {}).get("parent_identity")
        if (
            before_parent is not None
            and after_parent is not None
            and before_parent != after_parent
        ):
            raise ValueError("rollback manifest external parent changed")
    progress = manifest.get("rollback_progress", {})
    if (
        not isinstance(progress, dict)
        or not set(progress).issubset(ROLLBACK_PROGRESS_STEPS)
        or not all(
            value is True or value in {"started", "complete"}
            for value in progress.values()
        )
    ):
        raise ValueError("rollback manifest progress is invalid")
    previous = _absolute_path(manifest["previous_runtime_path"])
    if previous.parent != install_root.parent or not previous.name.startswith(
        f".{install_root.name}.previous-"
    ):
        raise ValueError("rollback manifest previous runtime path is invalid")
    parent_identity = manifest["install_parent_identity"]
    if (
        not isinstance(parent_identity, list)
        or len(parent_identity) != 2
        or not all(isinstance(value, int) for value in parent_identity)
    ):
        raise ValueError("rollback manifest install parent identity is invalid")


def _validate_external_manifest_rows(
    rows,
    expected_paths,
    manifest_path,
    *,
    snapshot,
    require_complete,
):
    if not isinstance(rows, list):
        raise ValueError("rollback manifest external rows must be a list")
    names = [row.get("name") for row in rows if isinstance(row, dict)]
    if len(names) != len(rows):
        raise ValueError("rollback manifest external row is invalid")
    if any(not isinstance(name, str) for name in names):
        raise ValueError("rollback manifest external row name is invalid")
    if len(names) != len(set(names)):
        raise ValueError("rollback manifest has duplicate external rows")
    if require_complete and set(names) != set(expected_paths):
        raise ValueError("rollback manifest external path set is invalid")
    if not set(names).issubset(expected_paths):
        raise ValueError("rollback manifest external path set is invalid")

    validated = {}
    backups = set()
    for row in rows:
        name = row["name"]
        existed = row.get("existed")
        if not isinstance(name, str) or type(existed) is not bool:
            raise ValueError("rollback manifest external row type is invalid")
        expected_keys = {"name", "path", "existed", "parent_identity"}
        if existed:
            expected_keys.update({"mode", "sha256"})
            if snapshot:
                expected_keys.add("backup")
        if set(row) != expected_keys:
            raise ValueError("rollback manifest external row schema is invalid")
        if row.get("path") != str(expected_paths[name]):
            raise ValueError("rollback manifest external binding is invalid")
        parent_identity = row.get("parent_identity")
        if parent_identity is not None and not _valid_identity(parent_identity):
            raise ValueError("rollback manifest external parent identity is invalid")
        if existed and parent_identity is None:
            raise ValueError("rollback manifest external parent identity is missing")
        if existed:
            mode = row.get("mode")
            digest = row.get("sha256")
            if (
                isinstance(mode, bool)
                or not isinstance(mode, int)
                or not 0 <= mode <= 0o7777
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise ValueError("rollback manifest external file metadata is invalid")
            if snapshot:
                backup = row.get("backup")
                if not isinstance(backup, str) or not backup:
                    raise ValueError("rollback manifest external backup is invalid")
                backup_path = manifest_path.parent / backup
                snapshot_root = manifest_path.parent / "snapshots"
                _assert_path_under(backup_path, snapshot_root)
                if backup in backups:
                    raise ValueError("rollback manifest has duplicate external backups")
                backups.add(backup)
        validated[name] = row
    return validated


def _valid_identity(value):
    return (
        isinstance(value, list)
        and len(value) == 2
        and all(
            isinstance(item, int) and not isinstance(item, bool)
            for item in value
        )
    )


def _validate_install_root(path):
    if path.name in {"", ".", ".."}:
        raise ValueError("install root is invalid")
    _assert_no_symlink_chain(path)
    if os.path.lexists(path):
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode):
            raise ValueError(f"install root must not be a symlink: {path}")
        if not stat.S_ISDIR(current.st_mode):
            raise ValueError(f"install root must be a directory: {path}")
        if hasattr(os, "getuid") and current.st_uid != os.getuid():
            raise ValueError(f"install root is not owned by the current user: {path}")
    nearest = path
    while not os.path.lexists(nearest):
        nearest = nearest.parent
    current = nearest.lstat()
    if hasattr(os, "getuid") and current.st_uid != os.getuid() and nearest != Path("/"):
        raise ValueError(f"install parent is not owned by the current user: {nearest}")


def _assert_external_target(path):
    path = _absolute_path(path)
    _assert_no_symlink_chain(path)
    if os.path.lexists(path):
        current = path.lstat()
        if stat.S_ISLNK(current.st_mode):
            raise ValueError(f"external target must not be a symlink: {path}")
        if not stat.S_ISREG(current.st_mode):
            raise ValueError(f"external target must be a regular file: {path}")


def _ensure_private_directory(path):
    path = _absolute_path(path)
    _assert_no_symlink_chain(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _assert_no_symlink_chain(path)
    if hasattr(os, "getuid") and path.stat().st_uid != os.getuid():
        raise ValueError(f"directory is not owned by the current user: {path}")
    return path


def _assert_no_symlink_chain(path, stop=None):
    path = _absolute_path(path)
    stop = _absolute_path(stop) if stop is not None else None
    current = path
    while True:
        if os.path.lexists(current):
            info = current.lstat()
            if (
                getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise ValueError(f"path contains a reparse point: {current}")
            if stat.S_ISLNK(info.st_mode):
                allowed_target = SYSTEM_PATH_ALIASES.get(current)
                if (
                    allowed_target is None
                    or Path(os.path.realpath(current)) != allowed_target
                ):
                    raise ValueError(f"path contains a symlink: {current}")
        if current == current.parent or current == stop:
            break
        current = current.parent


def _assert_path_under(path, root):
    path = _absolute_path(path)
    root = _absolute_path(root)
    try:
        inside = os.path.commonpath([path, root]) == str(root)
    except ValueError:
        inside = False
    if not inside or path == root:
        raise ValueError(f"path escapes managed root: {path}")


def _assert_absent_path(path):
    _assert_no_symlink_chain(path)
    if os.path.lexists(path):
        raise FileExistsError(path)


def _is_within(path, root):
    try:
        return os.path.commonpath([path, root]) == str(root)
    except ValueError:
        return False


def _write_new_file(path, data, mode):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, mode)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _atomic_write_bytes(path, data, mode=0o600):
    path = _absolute_path(path)
    _assert_no_symlink_chain(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
    try:
        _write_new_file(temporary, data, mode)
        _durable_replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def _atomic_write_json(path, payload, mode=0o600):
    _atomic_write_bytes(path, _pretty_json(payload), mode)


def _durable_replace(source, destination):
    """Rename one path and durably publish both affected directory entries."""
    source = _absolute_path(source)
    destination = _absolute_path(destination)
    if os.name == "nt":
        _windows_move_file_write_through(source, destination)
        return
    parents = []
    for parent in (source.parent, destination.parent):
        identity = os.path.realpath(parent)
        if all(os.path.realpath(existing) != identity for existing in parents):
            parents.append(parent)
    descriptors = []
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        for parent in parents:
            descriptors.append(os.open(parent, flags))
        os.replace(source, destination)
        for descriptor in descriptors:
            os.fsync(descriptor)
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def _windows_move_file_write_through(source, destination):
    import ctypes
    from ctypes import wintypes

    move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file.restype = wintypes.BOOL
    movefile_replace_existing = 0x1
    movefile_write_through = 0x8
    if not move_file(
        str(source),
        str(destination),
        movefile_replace_existing | movefile_write_through,
    ):
        error = ctypes.get_last_error()
        raise OSError(
            error,
            ctypes.FormatError(error),
            f"{source} -> {destination}",
        )


def _remove_tree(path, *, expected_parent_identity):
    if not os.path.lexists(path):
        return
    durable_rmtree(path, expected_parent_identity=expected_parent_identity)


def _require_supported_python():
    if sys.version_info < MINIMUM_PYTHON:
        required = ".".join(str(item) for item in MINIMUM_PYTHON)
        raise RuntimeError(
            f"Agent Memory Beacon stable installation requires Python {required}+"
        )


def _validated_executable(value):
    path = os.fspath(value or "")
    if not os.path.isabs(path) or not os.path.isfile(path) or not os.access(path, os.X_OK):
        raise ValueError(f"Python executable is invalid: {path}")
    return path


def _run_checked(runner, args, *, cwd, timeout, label):
    result = _invoke(runner, args, cwd=cwd, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{label} failed: {_result_detail(result)}")
    return result


def _run_json_preflight(runner, args, *, cwd, timeout, label):
    result = _run_checked(runner, args, cwd=cwd, timeout=timeout, label=label)
    try:
        payload = json.loads(result.stdout or "")
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} returned invalid JSON") from exc
    if payload.get("status") != "pass":
        raise RuntimeError(f"{label} reported failure")
    return payload


def _invoke(runner, args, *, cwd, timeout):
    call = runner or subprocess.run
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("PYTHON")
    }
    return call(
        tuple(os.fspath(item) for item in args),
        cwd=os.fspath(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
        env=environment,
    )


def _missing_service(result):
    detail = _result_detail(result).lower()
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


def _result_detail(result):
    return str(result.stderr or result.stdout or f"exit {result.returncode}").strip()


def _absolute_path(value):
    return Path(os.path.abspath(os.path.expandvars(os.path.expanduser(os.fspath(value)))))


def _operation_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{stamp}-{secrets.token_hex(4)}"


def _canonical_json(payload):
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _pretty_json(payload):
    return (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")


def _dry_run_payload(plan):
    return {
        "action": "dry-run",
        "release_id": plan.release_id,
        "source_root": str(plan.source_root),
        "install_root": str(plan.install_root),
        "files": [item.relative_path for item in plan.files],
        "external_paths": {
            name: str(path) for name, path in _external_paths(plan.cfg).items()
        },
        "preflight": ["source doctor ci", "staged doctor quick", "live doctor"],
        "hook_trust": "Codex /hooks review may be required after command path changes",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Install the stable Agent Memory Beacon runtime"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--verify-release", action="store_true")
    mode.add_argument("--rollback-manifest")
    parser.add_argument("--install-root", default=str(DEFAULT_INSTALL_ROOT))
    args = parser.parse_args(argv)
    if args.rollback_manifest:
        result = rollback_runtime(args.rollback_manifest)
        print(
            json.dumps(
                {
                    "action": result.action,
                    "install_root": str(result.install_root),
                    "manifest_path": str(result.manifest_path),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    from config import load_config

    cfg = load_config()
    source_root = Path(__file__).resolve().parent.parent
    plan = build_release_plan(source_root, args.install_root, cfg)
    if args.dry_run:
        print(json.dumps(_dry_run_payload(plan), ensure_ascii=False, indent=2))
        return 0
    if args.verify_release:
        result = verify_release(plan)
        print(
            json.dumps(
                {
                    "action": result.action,
                    "release_id": result.release_id,
                    "file_count": result.file_count,
                    "live_bindings_changed": False,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    staged = stage_runtime(plan)
    result = apply_runtime(plan, staged)
    print(
        json.dumps(
            {
                "action": result.action,
                "release_id": result.release_id,
                "install_root": str(result.install_root),
                "manifest_path": str(result.manifest_path),
                "trust_review_required": result.trust_review_required,
                "actions": list(result.actions),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
