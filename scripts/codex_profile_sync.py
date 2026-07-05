#!/usr/bin/env python3
"""Safely share local Codex skills and plugin preferences across accounts."""
import argparse
import hashlib
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path


SKILLS_MANIFEST = "skills-manifest.json"
PLUGINS_MANIFEST = "plugins-manifest.json"
SHARED_AGENTS = "AGENTS.shared.md"
SAFE_CONFIG = "config.toml"
PLUGIN_METADATA_DIRS = (".codex-plugin", ".claude-plugin")
SAFE_CONFIG_PREFIXES = ("marketplaces.", "plugins.")
SENSITIVE_FILENAMES = {
    "auth.json",
    "credentials.json",
    "tokens.json",
    "token.json",
    "id_rsa",
    "id_ed25519",
    "private_key",
    ".ds_store",
}
SENSITIVE_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
)
SENSITIVE_NAME_FRAGMENTS = (
    "token",
    "secret",
    "credential",
    "password",
    "private-key",
    "private_key",
)
SKIP_SKILL_DIRS = {
    ".system",
    "__pycache__",
}


def export_profile(codex_home, profile_dir, include_config=False):
    """Export safe, file-based Codex profile state into profile_dir."""
    codex_home = _path(codex_home)
    profile_dir = _path(profile_dir)
    _ensure_profile_dir_safe(codex_home, profile_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)

    skills = _export_skills(codex_home / "skills", profile_dir / "skills")
    agents_exported = _copy_agents(codex_home / "AGENTS.md", profile_dir / SHARED_AGENTS)

    config_text = ""
    config_exported = False
    config_path = codex_home / "config.toml"
    if include_config and config_path.exists():
        config_text = _extract_safe_config(config_path.read_text(encoding="utf-8"))
        _atomic_write_text(profile_dir / SAFE_CONFIG, config_text)
        config_exported = True
    elif (profile_dir / SAFE_CONFIG).exists():
        (profile_dir / SAFE_CONFIG).unlink()

    enabled_plugins = _parse_enabled_plugin_ids(
        config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    )
    plugins = _collect_plugins(codex_home / "plugins" / "cache", enabled_plugins)

    _atomic_write_json(
        profile_dir / SKILLS_MANIFEST,
        {
            "schema_version": "1.0",
            "generated_by": "codex_profile_sync.py",
            "skills": skills,
        },
    )
    _atomic_write_json(
        profile_dir / PLUGINS_MANIFEST,
        {
            "schema_version": "1.0",
            "generated_by": "codex_profile_sync.py",
            "plugins": plugins,
        },
    )

    return {
        "profile_dir": str(profile_dir),
        "skills_exported": len(skills),
        "agents_exported": agents_exported,
        "config_exported": config_exported,
        "plugins_found": len(plugins),
        "plugins_enabled": len([plugin for plugin in plugins if plugin.get("enabled")]),
    }


def apply_profile(profile_dir, codex_home, include_config=False, overwrite=False, dry_run=False):
    """Apply a safe Codex profile to a target Codex home."""
    profile_dir = _path(profile_dir)
    codex_home = _path(codex_home)
    if not dry_run:
        codex_home.mkdir(parents=True, exist_ok=True)

    skills_applied = _apply_skills(
        profile_dir / "skills",
        codex_home / "skills",
        overwrite,
        dry_run=dry_run,
    )
    agents_applied = _apply_agents(
        profile_dir / SHARED_AGENTS,
        codex_home / "AGENTS.md",
        dry_run=dry_run,
    )

    config_applied = False
    if include_config and (profile_dir / SAFE_CONFIG).exists():
        target_config = codex_home / "config.toml"
        existing = target_config.read_text(encoding="utf-8") if target_config.exists() else ""
        shared = (profile_dir / SAFE_CONFIG).read_text(encoding="utf-8")
        merged = _merge_safe_config(existing, shared)
        if not dry_run:
            _backup_if_changed(target_config, merged)
            _atomic_write_text(target_config, merged)
        config_applied = True

    return {
        "codex_home": str(codex_home),
        "skills_applied": skills_applied,
        "agents_applied": agents_applied,
        "config_applied": config_applied,
        "dry_run": dry_run,
    }


def status_profile(profile_dir, codex_home):
    """Report which exported skills/plugins are missing from a Codex home."""
    profile_dir = _path(profile_dir)
    codex_home = _path(codex_home)
    if not profile_dir.exists():
        return {
            "profile_dir": str(profile_dir),
            "codex_home": str(codex_home),
            "profile_exists": False,
            "missing_skills": [],
            "present_skills": [],
            "changed_skills": [],
            "missing_plugins": [],
            "present_plugins": [],
            "missing_plugin_cache": [],
            "notes": [
                "profile directory does not exist; run export before status/apply.",
                "Plugin/app authorization is account-specific; re-authorize connectors and remote plugins after switching accounts.",
            ],
        }

    skills_manifest = _load_json(profile_dir / SKILLS_MANIFEST, {"skills": []})
    plugins_manifest = _load_json(profile_dir / PLUGINS_MANIFEST, {"plugins": []})

    missing_skills = []
    present_skills = []
    changed_skills = []
    for skill in skills_manifest.get("skills", []):
        rel_path = Path(skill.get("path") or f"skills/{skill.get('name', '')}")
        target_dir = codex_home / rel_path
        target = target_dir / "SKILL.md"
        if not target.exists():
            missing_skills.append(skill.get("name"))
        elif skill.get("digest") and _skill_digest(target_dir) != skill.get("digest"):
            present_skills.append(skill.get("name"))
            changed_skills.append(skill.get("name"))
        else:
            present_skills.append(skill.get("name"))

    target_config = codex_home / "config.toml"
    enabled_plugins = _parse_enabled_plugin_ids(
        target_config.read_text(encoding="utf-8") if target_config.exists() else ""
    )
    cached_plugins = _collect_cached_plugins(codex_home / "plugins" / "cache")
    required_plugins = [
        plugin.get("id")
        for plugin in plugins_manifest.get("plugins", [])
        if plugin.get("enabled") and plugin.get("id")
    ]
    missing_plugins = [plugin_id for plugin_id in required_plugins if plugin_id not in enabled_plugins]
    present_plugins = [plugin_id for plugin_id in required_plugins if plugin_id in enabled_plugins]
    missing_plugin_cache = [
        plugin_id for plugin_id in present_plugins if plugin_id not in cached_plugins
    ]

    return {
        "profile_dir": str(profile_dir),
        "codex_home": str(codex_home),
        "profile_exists": True,
        "missing_skills": missing_skills,
        "present_skills": present_skills,
        "changed_skills": changed_skills,
        "missing_plugins": missing_plugins,
        "present_plugins": present_plugins,
        "missing_plugin_cache": missing_plugin_cache,
        "notes": [
            "Plugin/app authorization is account-specific; re-authorize connectors and remote plugins after switching accounts.",
            "auth.json, token files, sessions, logs, and plugin cache payloads are intentionally not applied.",
        ],
    }


def default_codex_home():
    cfg = _load_runtime_config()
    return Path(cfg.get("codex_home") or Path.home() / ".codex").expanduser()


def default_profile_dir():
    cfg = _load_runtime_config()
    if cfg.get("codex_profile_path"):
        return Path(cfg["codex_profile_path"]).expanduser()
    if cfg.get("agent_memory_path"):
        return Path(cfg["agent_memory_path"]).expanduser() / "codex-profile"
    if cfg.get("vault_path"):
        return Path(cfg["vault_path"]).expanduser() / "05-Agent-Memory" / "codex-profile"
    return Path.home() / "ObsidianBrain" / "05-Agent-Memory" / "codex-profile"


def _export_skills(source_dir, target_dir):
    if _same_path(source_dir, target_dir):
        raise ValueError("profile skills directory must be different from Codex skills directory")
    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    skills = []
    if not source_dir.exists():
        return skills

    for skill_dir in sorted(path for path in source_dir.iterdir() if path.is_dir()):
        if not _is_exportable_skill(skill_dir):
            continue
        destination = target_dir / skill_dir.name
        shutil.copytree(skill_dir, destination, ignore=_ignore_export_files)
        skills.append(
            {
                "name": _read_skill_name(skill_dir) or skill_dir.name,
                "path": f"skills/{skill_dir.name}",
                "has_agents": (skill_dir / "AGENTS.md").exists(),
                "digest": _skill_digest(skill_dir),
            }
        )
    return skills


def _apply_skills(source_dir, target_dir, overwrite, dry_run=False):
    if not source_dir.exists():
        return 0

    if not dry_run:
        target_dir.mkdir(parents=True, exist_ok=True)
    applied = 0
    for skill_dir in sorted(path for path in source_dir.iterdir() if path.is_dir()):
        if not _is_exportable_skill(skill_dir):
            continue
        destination = target_dir / skill_dir.name
        if destination.exists():
            if not overwrite:
                continue
            if not dry_run:
                _backup_path(destination)
                shutil.rmtree(destination)
        if not dry_run:
            shutil.copytree(skill_dir, destination, ignore=_ignore_export_files)
        applied += 1
    return applied


def _is_exportable_skill(skill_dir):
    if skill_dir.name in SKIP_SKILL_DIRS or skill_dir.name.startswith("."):
        return False
    return (skill_dir / "SKILL.md").exists()


def _ignore_export_files(_directory, names):
    ignored = set()
    for name in names:
        if _should_ignore_export_name(name):
            ignored.add(name)
    return ignored


def _should_ignore_export_name(name):
    lowered = name.lower()
    if lowered.startswith(".env"):
        return True
    if lowered in SENSITIVE_FILENAMES or lowered == "__pycache__":
        return True
    if lowered.endswith((".pyc", ".sqlite", ".sqlite3", ".db", ".log", *SENSITIVE_SUFFIXES)):
        return True
    return any(fragment in lowered for fragment in SENSITIVE_NAME_FRAGMENTS)


def _copy_agents(source, destination):
    if not source.exists():
        if destination.exists():
            destination.unlink()
        return False
    _atomic_write_text(destination, source.read_text(encoding="utf-8"))
    return True


def _apply_agents(source, destination, dry_run=False):
    if not source.exists():
        return False
    content = source.read_text(encoding="utf-8")
    if not dry_run:
        _backup_if_changed(destination, content)
        _atomic_write_text(destination, content)
    return True


def _collect_plugins(cache_dir, enabled_plugins):
    plugins_by_id = {}

    if cache_dir.exists():
        for metadata_path in sorted(cache_dir.glob("*/*/*/*/plugin.json")):
            if metadata_path.parent.name not in PLUGIN_METADATA_DIRS:
                continue
            version_dir = metadata_path.parent.parent
            plugin_name = version_dir.parent.name
            marketplace = version_dir.parent.parent.name
            plugin_id = f"{plugin_name}@{marketplace}"
            metadata = _load_json(metadata_path, {})
            plugins_by_id[plugin_id] = {
                "id": plugin_id,
                "name": metadata.get("name") or plugin_name,
                "marketplace": marketplace,
                "version": metadata.get("version") or version_dir.name,
                "description": metadata.get("description") or "",
                "enabled": plugin_id in enabled_plugins,
                "cached": True,
            }

    for plugin_id in sorted(enabled_plugins):
        if plugin_id in plugins_by_id:
            continue
        name, marketplace = _split_plugin_id(plugin_id)
        plugins_by_id[plugin_id] = {
            "id": plugin_id,
            "name": name,
            "marketplace": marketplace,
            "version": "",
            "description": "",
            "enabled": True,
            "cached": False,
        }

    return [plugins_by_id[key] for key in sorted(plugins_by_id)]


def _collect_cached_plugins(cache_dir):
    return {
        plugin["id"]
        for plugin in _collect_plugins(cache_dir, set())
        if plugin.get("cached") and plugin.get("id")
    }


def _split_plugin_id(plugin_id):
    if "@" not in plugin_id:
        return plugin_id, ""
    name, marketplace = plugin_id.rsplit("@", 1)
    return name, marketplace


def _parse_enabled_plugin_ids(config_text):
    enabled = set()
    for header, body in _iter_toml_sections(config_text):
        match = re.fullmatch(r'plugins\."([^"]+)"', header)
        if not match:
            continue
        if re.search(r"(?m)^\s*enabled\s*=\s*true\s*(?:#.*)?$", body):
            enabled.add(match.group(1))
    return enabled


def _extract_safe_config(config_text):
    sections = []
    for header, body in _iter_toml_sections(config_text):
        if not header.startswith(SAFE_CONFIG_PREFIXES):
            continue
        safe_body = _sanitize_safe_section(header, body)
        if safe_body.strip():
            sections.append(f"[{header}]\n{safe_body.rstrip()}\n")
    if not sections:
        return ""
    return "\n".join(sections).rstrip() + "\n"


def _sanitize_safe_section(header, body):
    safe_lines = []
    if header.startswith("plugins."):
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if re.match(r"^enabled\s*=\s*(true|false)\s*(?:#.*)?$", stripped):
                safe_lines.append(stripped)
        return "\n".join(safe_lines) + ("\n" if safe_lines else "")

    if header.startswith("marketplaces."):
        for line in body.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if re.match(r"^(enabled|type|source|url|registry)\s*=", stripped):
                if _looks_sensitive(stripped):
                    continue
                safe_lines.append(stripped)
        return "\n".join(safe_lines) + ("\n" if safe_lines else "")

    return ""


def _merge_safe_config(existing, shared):
    shared_sections = {header: body for header, body in _iter_toml_sections(shared)}
    if not shared_sections:
        return existing

    merged_parts = []
    preamble = _toml_preamble(existing)
    if preamble:
        merged_parts.append(preamble)

    kept_sections = []
    for header, body in _iter_toml_sections(existing):
        if header in shared_sections and header.startswith(SAFE_CONFIG_PREFIXES):
            continue
        kept_sections.append(f"[{header}]\n{body.rstrip()}\n")

    if kept_sections:
        merged_parts.append("\n".join(part.rstrip() for part in kept_sections).rstrip())
    merged_parts.append(shared.rstrip())
    return "\n\n".join(part for part in merged_parts if part).rstrip() + "\n"


def _toml_preamble(config_text):
    lines = []
    for line in config_text.splitlines():
        if re.match(r"^\s*\[[^\]]+\]\s*$", line):
            break
        lines.append(line)
    return "\n".join(lines).rstrip()


def _iter_toml_sections(config_text):
    current_header = None
    current_body = []
    for line in config_text.splitlines():
        match = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
        if match:
            if current_header is not None:
                yield current_header, "\n".join(current_body) + ("\n" if current_body else "")
            current_header = match.group(1)
            current_body = []
            continue
        if current_header is not None:
            current_body.append(line)
    if current_header is not None:
        yield current_header, "\n".join(current_body) + ("\n" if current_body else "")


def _looks_sensitive(line):
    lowered = line.lower()
    return any(word in lowered for word in ("token", "secret", "key", "password", "credential"))


def _read_skill_name(skill_dir):
    skill_file = skill_dir / "SKILL.md"
    try:
        text = skill_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    match = re.search(r"(?m)^name:\s*[\"']?([^\"'\n]+)[\"']?\s*$", text)
    return match.group(1).strip() if match else ""


def _skill_digest(skill_dir):
    digest = hashlib.sha256()
    if not skill_dir.exists():
        return ""
    for path in sorted(item for item in skill_dir.rglob("*") if item.is_file()):
        rel_parts = path.relative_to(skill_dir).parts
        if any(_should_ignore_export_name(part) for part in rel_parts):
            continue
        rel = "/".join(rel_parts)
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            continue
        digest.update(b"\0")
    return digest.hexdigest()


def _load_json(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return fallback


def _atomic_write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def _atomic_write_text(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _backup_if_changed(path, new_content):
    if not path.exists():
        return
    try:
        if path.read_text(encoding="utf-8") == new_content:
            return
    except OSError:
        return
    _backup_path(path)


def _backup_path(path):
    if not path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(f"{path.name}.bak-{stamp}")
    if path.is_dir():
        shutil.copytree(path, backup_path)
    else:
        shutil.copy2(path, backup_path)


def _load_runtime_config():
    try:
        from config import load_config

        return load_config()
    except Exception:
        return {}


def _path(value):
    return Path(value).expanduser()


def _ensure_profile_dir_safe(codex_home, profile_dir):
    codex_home = _resolved(codex_home)
    profile_dir = _resolved(profile_dir)
    if profile_dir == codex_home or _is_relative_to(profile_dir, codex_home):
        raise ValueError("profile_dir must be outside codex_home to avoid destructive overlap")


def _same_path(left, right):
    return _resolved(left) == _resolved(right)


def _resolved(path):
    return Path(path).expanduser().resolve(strict=False)


def _is_relative_to(path, parent):
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def main():
    parser = argparse.ArgumentParser(description="Sync safe local Codex profile state across accounts")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("export", "apply", "status"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--codex-home", default=str(default_codex_home()))
        sub.add_argument("--profile-dir", default=str(default_profile_dir()))
        sub.add_argument("--json", action="store_true", help="Print machine-readable JSON")
        if command in ("export", "apply"):
            sub.add_argument("--include-config", action="store_true", help="Sync safe plugin config only")
        if command == "apply":
            sub.add_argument("--overwrite", action="store_true", help="Replace existing skills after backing them up")
            sub.add_argument("--dry-run", action="store_true", help="Preview apply without writing files")

    args = parser.parse_args()
    if args.command == "export":
        result = export_profile(args.codex_home, args.profile_dir, include_config=args.include_config)
    elif args.command == "apply":
        result = apply_profile(
            args.profile_dir,
            args.codex_home,
            include_config=args.include_config,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )
    else:
        result = status_profile(args.profile_dir, args.codex_home)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        _print_human(args.command, result)


def _print_human(command, result):
    if command == "export":
        print(f"Exported {result['skills_exported']} skill(s) to {result['profile_dir']}")
        print(f"Plugin manifest: {result['plugins_enabled']} enabled / {result['plugins_found']} found")
        print(f"Shared AGENTS.md: {'yes' if result['agents_exported'] else 'no'}")
        print(f"Safe plugin config: {'yes' if result['config_exported'] else 'no'}")
    elif command == "apply":
        verb = "Would apply" if result.get("dry_run") else "Applied"
        print(f"{verb} {result['skills_applied']} skill(s) to {result['codex_home']}")
        print(f"Shared AGENTS.md: {'yes' if result['agents_applied'] else 'no'}")
        print(f"Safe plugin config: {'yes' if result['config_applied'] else 'no'}")
    else:
        print(f"Profile: {result['profile_dir']}")
        print(f"Codex home: {result['codex_home']}")
        if not result.get("profile_exists", True):
            print("Profile status: missing")
        print(f"Missing skills: {', '.join(result['missing_skills']) or 'none'}")
        print(f"Changed skills: {', '.join(result.get('changed_skills', [])) or 'none'}")
        print(f"Missing enabled plugins: {', '.join(result['missing_plugins']) or 'none'}")
        print(f"Missing plugin cache: {', '.join(result.get('missing_plugin_cache', [])) or 'none'}")
        for note in result["notes"]:
            print(f"Note: {note}")


if __name__ == "__main__":
    main()
