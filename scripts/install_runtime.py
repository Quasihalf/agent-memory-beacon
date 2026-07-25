#!/usr/bin/env python3
"""Stage and transactionally install the stable Agent Memory Beacon runtime."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import secrets
import stat
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from branding import LEGACY_LAUNCHD_LABELS, NEW_LAUNCHD_LABELS
from codex_profile_sync import SHARED_AGENTS
from install_claude import (
    install_claude_patch,
    install_hooks as install_claude_hooks,
)
from install_codex import install_agents_patch, install_hooks
from install_launchd import install_launch_agents
from safety import (
    durable_atomic_write,
    durable_rmtree,
    durable_unlink,
    ensure_directory_tree,
    exclusive_file_lock,
    safe_vault_path,
)


DEFAULT_INSTALL_ROOT = Path("~/.local/share/agent-memory-beacon/runtime").expanduser()
MINIMUM_PYTHON = (3, 11)
MANIFEST_SCHEMA_VERSION = 2
SUPPORTED_ROLLBACK_SCHEMA_VERSIONS = frozenset({1, MANIFEST_SCHEMA_VERSION})
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
RUNTIME_ROOT_FILES = ("LICENSE",)
RUNTIME_SCRIPT_FILES = (
    "__init__.py",
    "analyzer.py",
    "annotation_quality.py",
    "backup.py",
    "branding.py",
    "codex_profile_sync.py",
    "codex_prompt_hook.py",
    "compiler.py",
    "config.py",
    "context_install.py",
    "doctor.py",
    "error_evidence.py",
    "evaluate_annotation_quality.py",
    "evaluate_memory_comparison.py",
    "experience_memory.py",
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


def build_release_plan(source_root, install_root, cfg):
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
    runtime_cfg = _runtime_config(cfg, install_root, source_root=source_root)
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


def stage_runtime(plan, command_runner=None):
    """Create and validate a complete runtime beside its final destination."""
    _require_supported_python()
    _validate_plan(plan)
    _validate_install_root(plan.install_root)
    source_python = _validated_executable(plan.source_python_path)
    _run_checked(
        command_runner,
        (source_python, "-B", "-c", RUNTIME_PYTHON_CHECK_CODE),
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

        _run_json_preflight(
            command_runner,
            (
                source_python,
                "-B",
                str(plan.source_root / "scripts" / "doctor.py"),
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
        _run_checked(
            command_runner,
            (source_python, "-m", "venv", "--copies", str(stage / ".venv")),
            cwd=stage,
            timeout=180,
            label="runtime virtual environment creation",
        )
        staged_python = _validated_executable(stage / ".venv" / "bin" / "python")
        _run_checked(
            command_runner,
            (
                staged_python,
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--requirement",
                str(stage / "scripts" / "requirements.lock"),
            ),
            cwd=stage,
            timeout=600,
            label="runtime dependency installation",
        )
        _run_json_preflight(
            command_runner,
            (
                staged_python,
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
        _verify_staged_files(plan, stage)
        return StagedRuntime(stage, plan.release_id, manifest_path)
    except Exception:
        _remove_tree(stage, expected_parent_identity=stage_parent_identity)
        raise


def verify_release(plan, command_runner=None):
    """Build and validate a fresh runtime without switching live bindings."""
    staged = stage_runtime(plan, command_runner=command_runner)
    stage_parent = staged.root.parent.lstat()
    stage_parent_identity = (stage_parent.st_dev, stage_parent.st_ino)
    try:
        _validate_staged_runtime(plan, staged)
        return ReleaseVerification(
            action="verified",
            release_id=plan.release_id,
            file_count=len(plan.files),
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
    with _runtime_transaction_locks(plan.cfg, plan.install_root):
        return _apply_runtime_locked(plan, staged, command_runner)


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
        "schema_version": MANIFEST_SCHEMA_VERSION,
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
            os.replace(plan.install_root, previous_path)
            manifest["status"] = "previous_runtime_staged"
            _atomic_write_json(manifest_path, manifest, mode=0o600)
        os.replace(staged.root, plan.install_root)
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


def _runtime_config(cfg, install_root, source_root=None):
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
    rendered["python_path"] = str(install_root / ".venv" / "bin" / "python")
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


def _validate_staged_runtime(plan, staged):
    if not isinstance(staged, StagedRuntime) or staged.release_id != plan.release_id:
        raise ValueError("staged runtime does not match release plan")
    _assert_path_under(staged.root, plan.install_root.parent)
    if staged.root.is_symlink() or not staged.root.is_dir():
        raise ValueError("staged runtime is missing or symlinked")
    _verify_staged_files(plan, staged.root)
    _validated_executable(staged.root / ".venv" / "bin" / "python")


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
    for kind in ("harvest", "weekly", "legacy_harvest", "legacy_weekly"):
        label = (
            NEW_LAUNCHD_LABELS[kind]
            if kind in NEW_LAUNCHD_LABELS
            else LEGACY_LAUNCHD_LABELS[kind.removeprefix("legacy_")]
        )
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
        label = (
            NEW_LAUNCHD_LABELS[kind]
            if kind in NEW_LAUNCHD_LABELS
            else LEGACY_LAUNCHD_LABELS[kind.removeprefix("legacy_")]
        )
        raise RuntimeError(
            f"launchd service {label} is loaded but its plist is missing: {path}"
        )


def _run_live_preflight(install_root, command_runner=None):
    python = _validated_executable(install_root / ".venv" / "bin" / "python")
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
    python = _validated_executable(install_root / ".venv" / "bin" / "python")
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
    python = _validated_executable(install_root / ".venv" / "bin" / "python")
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
            lambda _resuming: _bootout_managed_jobs(command_runner),
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


def _bootout_managed_jobs(command_runner):
    errors = []
    domain = f"gui/{os.getuid()}"
    for label in NEW_LAUNCHD_LABELS.values():
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
        os.replace(install_root, failed)
    try:
        if previous_existed:
            if previous.is_symlink() or not previous.is_dir():
                raise RuntimeError("previous runtime is missing")
            os.replace(previous, install_root)
    except Exception:
        if failed.exists() and not install_root.exists():
            os.replace(failed, install_root)
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
        label = (
            NEW_LAUNCHD_LABELS[kind]
            if kind in NEW_LAUNCHD_LABELS
            else LEGACY_LAUNCHD_LABELS[kind.removeprefix("legacy_")]
        )
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
        if os.path.lexists(current) and stat.S_ISLNK(current.lstat().st_mode):
            allowed_target = SYSTEM_PATH_ALIASES.get(current)
            if allowed_target is None or Path(os.path.realpath(current)) != allowed_target:
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
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def _atomic_write_json(path, payload, mode=0o600):
    _atomic_write_bytes(path, _pretty_json(payload), mode)


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
    return call(
        tuple(os.fspath(item) for item in args),
        cwd=os.fspath(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
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
