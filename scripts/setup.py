#!/usr/bin/env python3
"""Interactive setup script for Agent Memory Beacon.

Creates the vault directory structure, prompts for paths and project names,
generates config.yaml, copies templates, and validates everything.
"""

import argparse
import os
import sys
import json
import stat
import yaml

from branding import (
    CODE_PREFIX,
    NEW_LAUNCHD_LABELS,
    PRODUCT_NAME,
    PRODUCT_VERSION,
    default_vault_path,
)
from safety import (
    OBSIDIAN_IGNORE_FILTERS,
    durable_atomic_write,
    ensure_directory_tree,
    normalize_project_slug,
    secure_read_bytes,
)
import shutil
from pathlib import Path


TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "templates" / "vault"
VAULT_TEMPLATE_MANIFEST = (
    ("00-Rules/_TEMPLATE.md", "00-Rules/_TEMPLATE.md"),
    ("00-Rules/_inbox/_TEMPLATE.md", "00-Rules/_inbox/_TEMPLATE.md"),
    (
        "01-Projects/project-alpha/Feedback/_TEMPLATE.md",
        "02-Templates/project/Feedback/_TEMPLATE.md",
    ),
    (
        "01-Projects/project-alpha/Memory/cross-project-links.md",
        "02-Templates/project/Memory/cross-project-links.md",
    ),
    (
        "01-Projects/project-alpha/Memory/decisions.md",
        "02-Templates/project/Memory/decisions.md",
    ),
    (
        "01-Projects/project-alpha/Memory/pitfalls.md",
        "02-Templates/project/Memory/pitfalls.md",
    ),
    (
        "01-Projects/project-alpha/Memory/sessions/_TEMPLATE.md",
        "02-Templates/project/Memory/sessions/_TEMPLATE.md",
    ),
    ("04-Feedback/growth-metrics.md", "04-Feedback/growth-metrics.md"),
    (
        "04-Feedback/weekly-reports/_TEMPLATE.md",
        "04-Feedback/weekly-reports/_TEMPLATE.md",
    ),
    ("用户手册.md", "用户手册.md"),
)
PROJECT_TEMPLATE_MANIFEST = (
    (
        "01-Projects/project-alpha/Feedback/_TEMPLATE.md",
        "Feedback/_TEMPLATE.md",
    ),
    (
        "01-Projects/project-alpha/Memory/cross-project-links.md",
        "Memory/cross-project-links.md",
    ),
    (
        "01-Projects/project-alpha/Memory/decisions.md",
        "Memory/decisions.md",
    ),
    (
        "01-Projects/project-alpha/Memory/pitfalls.md",
        "Memory/pitfalls.md",
    ),
    (
        "01-Projects/project-alpha/Memory/sessions/_TEMPLATE.md",
        "Memory/sessions/_TEMPLATE.md",
    ),
)


def expand_path(path_str):
    """Expand ~ and environment variables in a path."""
    return os.path.expandvars(os.path.expanduser(path_str))


def prompt(prompt_text, default=None):
    """Prompt with optional default value."""
    if default:
        result = input(f"{prompt_text} [{default}]: ").strip()
        return result if result else default
    return input(f"{prompt_text}: ").strip()


def prompt_required(prompt_text, validate_exists=False, default=None):
    """Prompt for a required value. Optionally validate that the path exists."""
    while True:
        value = prompt(prompt_text, default)
        if not value:
            print("  This field is required. Please enter a value.")
            continue
        if validate_exists:
            expanded = expand_path(value)
            if not os.path.exists(expanded):
                print(f"  Path not found: {expanded}")
                yn = prompt("  Create it? (y/n)", "y").lower()
                if yn == 'y':
                    os.makedirs(expanded, exist_ok=True)
                    print(f"  Created: {expanded}")
                    return value
                print("  Please enter a valid path.")
                continue
        return value


def detect_defaults():
    """Auto-detect default paths based on the current environment."""
    defaults = {}

    # Python path
    defaults['python_path'] = sys.executable

    # Codex session path (macOS/Linux default)
    home = os.path.expanduser("~")
    codex_home = os.path.join(home, ".codex")
    defaults['codex_home'] = codex_home
    defaults['codex_sessions_path'] = os.path.join(codex_home, "sessions")
    zcode_home = os.path.join(home, ".zcode")
    defaults['zcode_home'] = zcode_home
    defaults['zcode_db_path'] = os.path.join(zcode_home, "cli", "db", "db.sqlite")
    defaults['context_targets'] = [
        os.path.join(codex_home, "AGENTS.md"),
        os.path.join(home, ".claude", "CLAUDE.md"),
        os.path.join(zcode_home, "AGENTS.md"),
    ]

    # Claude project path (kept for compatibility)
    claude_projects = os.path.join(home, ".claude", "projects")
    if os.path.exists(claude_projects):
        # List available project dirs
        subdirs = [d for d in os.listdir(claude_projects)
                   if os.path.isdir(os.path.join(claude_projects, d))]
        if subdirs:
            print(f"\nDetected Claude projects at: {claude_projects}")
            print("Available project directories:")
            for i, d in enumerate(subdirs):
                print(f"  [{i+1}] {d}")
            # Default to first one
            defaults['claude_project_path'] = os.path.join(claude_projects, subdirs[0])
        else:
            defaults['claude_project_path'] = claude_projects
    else:
        defaults['claude_project_path'] = os.path.join(home, ".claude", "projects")

    # Claude settings.json
    settings_path = os.path.join(home, ".claude", "settings.json")
    if os.path.exists(settings_path):
        defaults['settings_json'] = settings_path

    # Vault path
    defaults["vault_path"] = str(default_vault_path(home))
    defaults["agent_memory_path"] = os.path.join(
        defaults["vault_path"],
        "05-Agent-Memory",
    )

    # CLAUDE.md / AGENTS.md
    for candidate in [
        os.path.join(home, "projects", "CLAUDE.md"),
        os.path.join(os.getcwd(), "CLAUDE.md"),
        os.path.join(os.getcwd(), "AGENTS.md"),
    ]:
        if os.path.exists(candidate):
            defaults['claude_md_path'] = candidate
            break
    if 'claude_md_path' not in defaults:
        defaults['claude_md_path'] = ""

    return defaults


def create_vault_structure(vault_path):
    """Create the full vault directory structure with README files."""
    vault = os.path.abspath(expand_path(vault_path))
    _ensure_real_directory(vault)

    dirs = [
        "00-Inbox",
        "00-Rules/_inbox/_rejected",
        "00-Rules/_archive",
        "01-Projects",
        "02-Templates",
        "03-Maps",
        "04-Feedback/_logs",
        "04-Feedback/_raw-sessions",
        "04-Feedback/_rollback",
        "04-Feedback/weekly-reports",
        "05-Agent-Memory",
    ]

    for d in dirs:
        dpath = os.path.join(vault, d)
        ensure_directory_tree(dpath, vault)

    install_vault_templates(vault)

    obsidian_dir = os.path.join(vault, ".obsidian")
    ensure_directory_tree(obsidian_dir, vault)
    app_json = os.path.join(obsidian_dir, "app.json")
    app_config = {}
    if os.path.lexists(app_json):
        try:
            app_config = json.loads(
                secure_read_bytes(app_json, 1_048_576, root=vault).decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError):
            app_config = {}
    if not isinstance(app_config.get("userIgnoreFilters"), list):
        app_config["userIgnoreFilters"] = []
    for ignore in OBSIDIAN_IGNORE_FILTERS:
        if ignore not in app_config["userIgnoreFilters"]:
            app_config["userIgnoreFilters"].append(ignore)
    durable_atomic_write(
        app_json,
        json.dumps(app_config, ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
        root=vault,
    )

    # Write vault README.md with frontmatter
    readme_content = f"""---
vault_version: "{PRODUCT_VERSION}"
created: {__import__('datetime').datetime.now().strftime('%Y-%m-%d')}
vault_name: "{PRODUCT_NAME}"
description: "User-owned Obsidian memory for local AI agents"
---

# {PRODUCT_NAME}

> User-owned long-term memory for Codex and compatible local agents.
> The vault stores reusable decisions, resolved errors, and session summaries instead of raw chat dumps.

## Structure

| Directory | Purpose |
|-----------|---------|
| `00-Inbox/` | Visible agent memory index and quick review entry points |
| `00-Rules/` | Active rules, inbox approval cards, archive |
| `01-Projects/` | One folder per project with Memory/sessions/ |
| `02-Templates/` | Markdown templates for sessions, decisions, pitfalls |
| `03-Maps/` | Auto-generated topic index, timeline, search index |
| `04-Feedback/` | Weekly reports, scanner logs, raw session backups |

## Getting Started

1. Project folders are created automatically by the scanner
2. Session summaries go in `01-Projects/<name>/Memory/sessions/`
3. Rules start in `00-Rules/_inbox/` as approval cards
4. The weekly scanner rebuilds `03-Maps/` automatically
"""
    write_if_missing(os.path.join(vault, "README.md"), readme_content, root=vault)

    today = __import__('datetime').datetime.now().strftime('%Y-%m-%d')
    map_placeholders = {
        "timeline.md": f"""---
title: "时间线 / Timeline"
updated: {today}
auto_generated: true
---

# 时间线 / Timeline

_首次周扫描后自动生成。_
""",
        "topic-index.md": f"""---
title: "主题索引 / Topic Index"
updated: {today}
auto_generated: true
---

# 主题索引 / Topic Index

_首次周扫描后自动生成。_
""",
    }
    for filename, content in map_placeholders.items():
        write_if_missing(
            os.path.join(vault, "03-Maps", filename),
            content,
            root=vault,
        )

    personal_memory = """---
title: Personal Memory
generated_by: memory_judge.py
---

# Personal Memory

Promoted memories from repeated or high-confidence conversations.

## Related

- [[00-Inbox/Agent Memory Index|Agent Memory Index]]
- [[03-Maps/timeline|Timeline]]
- [[03-Maps/topic-index|Topic Index]]
"""
    write_if_missing(
        os.path.join(vault, "05-Agent-Memory", "personal-memory.md"),
        personal_memory,
        root=vault,
    )

    # Keep fresh Vaults on the same taxonomy contract as the managed patch.
    taxonomy_content = read_template(
        "04-Feedback/error-taxonomy.md"
    ).replace(
        "{YYYY-MM-DD}",
        __import__('datetime').datetime.now().strftime('%Y-%m-%d'),
    )
    write_if_missing(
        os.path.join(vault, "04-Feedback", "error-taxonomy.md"),
        taxonomy_content,
        root=vault,
    )

    # Write heartbeat.md placeholder
    heartbeat_content = f"""---
last_scan: null
scan_status: never_run
sessions_processed: 0
processed_sessions: {{}}
backed_up_sessions: {{}}
harvested_sessions: {{}}
errors: []
script_version: "1.0.0"
---

# Scanner Heartbeat

The weekly scanner has not run yet. Run `runner.py` to start the first scan.
"""
    write_if_missing(
        os.path.join(vault, "04-Feedback", "heartbeat.md"),
        heartbeat_content,
        root=vault,
    )
    initialize_memory_indexes(vault)

    return vault


def install_vault_templates(vault):
    today = __import__('datetime').datetime.now().strftime('%Y-%m-%d')
    for source_name, destination_name in VAULT_TEMPLATE_MANIFEST:
        destination = os.path.join(vault, destination_name)
        ensure_directory_tree(os.path.dirname(destination), vault)
        content = read_template(source_name)
        if source_name.startswith("01-Projects/project-alpha/"):
            content = render_project_template(content, "{project}")
        content = content.replace("{YYYY-MM-DD}", today)
        write_if_missing(destination, content, root=vault)


def read_template(relative_path):
    source = TEMPLATE_ROOT / relative_path
    return secure_read_bytes(
        source,
        2 * 1024 * 1024,
        root=TEMPLATE_ROOT,
    ).decode("utf-8")


def render_project_template(content, project):
    return content.replace("{project-alpha}", project).replace(
        "project-alpha",
        project,
    )


def initialize_memory_indexes(vault_path):
    """Create the derived runtime indexes required by a fresh installation."""
    vault = expand_path(vault_path)
    recall_path = os.path.join(vault, "05-Agent-Memory", "recall-index.json")
    if os.path.lexists(recall_path):
        if os.path.islink(recall_path) or not os.path.isfile(recall_path):
            raise ValueError("recall index path must be a regular file")
        return False

    from knowledge_index import rebuild_vault_knowledge_indexes

    rebuild_vault_knowledge_indexes({"vault_path": vault})
    if os.path.islink(recall_path) or not os.path.isfile(recall_path):
        raise RuntimeError("fresh setup did not create a valid recall index file")
    return True


def write_if_missing(path, content, root=None):
    if os.path.lexists(path):
        secure_read_bytes(path, 0, root=root)
        return False
    durable_atomic_write(
        path,
        content,
        mode=0o644,
        root=root,
        preserve_existing_mode=False,
    )
    return True


def _ensure_real_directory(path):
    if os.path.lexists(path):
        current = os.lstat(path)
        if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
            raise ValueError(f"Vault root must be a real directory: {path}")
    else:
        os.makedirs(path, mode=0o700)
    ensure_directory_tree(path, path)
    return path


def create_project_folders(vault_path, project_names):
    """Create project directories with Memory/sessions/ subdirectories."""
    vault = os.path.abspath(expand_path(vault_path))
    _ensure_real_directory(vault)
    projects_dir = os.path.join(vault, "01-Projects")
    ensure_directory_tree(projects_dir, vault)
    created = []

    for name in project_names:
        name = name.strip().replace(" ", "-").lower()
        name = normalize_project_slug(name)
        if not name:
            continue
        proj_dir = os.path.join(projects_dir, name)
        today = __import__('datetime').datetime.now().strftime('%Y-%m-%d')
        for source_name, destination_name in PROJECT_TEMPLATE_MANIFEST:
            destination = os.path.join(proj_dir, destination_name)
            ensure_directory_tree(os.path.dirname(destination), vault)
            content = (
                render_project_template(read_template(source_name), name).replace(
                    "{YYYY-MM-DD}",
                    today,
                )
            )
            write_if_missing(destination, content, root=vault)

        created.append(name)

    return created


def add_projects_to_config(config_path, project_names):
    """Create project templates and atomically add their slugs to config."""
    path = os.path.abspath(expand_path(config_path))
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError(f"config must be a regular file: {path}")
    config_root = os.path.dirname(path)
    try:
        config = yaml.safe_load(
            secure_read_bytes(path, 4 * 1024 * 1024, root=config_root).decode(
                "utf-8"
            )
        ) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError("config.yaml is not valid UTF-8 YAML") from exc
    if not isinstance(config, dict):
        raise ValueError("config.yaml must contain a mapping")
    vault_path = str(config.get("vault_path") or "").strip()
    if not vault_path:
        raise ValueError("config.yaml vault_path is required")
    existing = config.get("projects") or []
    if not isinstance(existing, list):
        raise ValueError("config.yaml projects must be a list")

    created = create_project_folders(vault_path, project_names)
    updated = []
    for project in [*existing, *created]:
        project = str(project or "").strip()
        if project and project not in updated:
            updated.append(project)
    if updated != existing:
        config["projects"] = updated
        durable_atomic_write(
            path,
            yaml.safe_dump(
                config,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            ),
            mode=0o600,
            root=config_root,
        )
    return created


def validate_setup(vault_path, config_path):
    """Validate the vault structure and config."""
    vault = expand_path(vault_path)
    errors = []

    # Check required directories
    required_dirs = [
        "00-Rules/_inbox",
        "00-Rules/_archive",
        "01-Projects",
        "03-Maps",
        "04-Feedback/_logs",
        "04-Feedback/_raw-sessions",
        "04-Feedback/weekly-reports",
        "05-Agent-Memory",
    ]
    for d in required_dirs:
        dpath = os.path.join(vault, d)
        if not os.path.exists(dpath):
            errors.append(f"Missing directory: {d}")

    # Check required files
    required_files = [
        "README.md",
        "04-Feedback/error-taxonomy.md",
        "04-Feedback/heartbeat.md",
    ]
    for f in required_files:
        fpath = os.path.join(vault, f)
        if not os.path.exists(fpath):
            errors.append(f"Missing file: {f}")

    # Validate config.yaml
    if not os.path.exists(config_path):
        errors.append("config.yaml not found")
    else:
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        for key in ['vault_path', 'python_path']:
            if not cfg.get(key):
                errors.append(f"config.yaml: {key} is empty")

    return errors


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Initialize Agent Memory Beacon or add a project",
    )
    parser.add_argument(
        "--add-project",
        action="append",
        default=[],
        metavar="NAME",
        help="create a project from templates and add it to config.yaml",
    )
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"),
        help="config.yaml used by --add-project",
    )
    args = parser.parse_args(argv)
    if args.add_project:
        created = add_projects_to_config(args.config, args.add_project)
        if not created:
            parser.error("--add-project did not contain a valid project name")
        print("Projects ready: " + ", ".join(created))
        print(f"Config updated: {os.path.abspath(expand_path(args.config))}")
        return 0

    print("=" * 60)
    print(f"  {PRODUCT_NAME} — Setup")
    print("  User-owned long-term memory for Codex and compatible local agents")
    print("=" * 60)
    print()

    defaults = detect_defaults()

    # Step 1: Gather paths
    print("--- Path Configuration ---")
    print()

    vault_path = prompt_required(
        f"Vault path (where the Obsidian vault lives)",
        validate_exists=False,
        default=defaults["vault_path"],
    )

    agent = prompt(
        "Agent runtime (codex/claude/zcode)",
        "codex"
    ).lower()

    codex_sessions_path = prompt(
        "Codex sessions directory",
        defaults['codex_sessions_path']
    )

    zcode_db_path = prompt(
        "ZCode SQLite DB path",
        defaults.get('zcode_db_path', '')
    )

    claude_project_path = prompt(
        f"Claude projects directory (optional, for Claude Code JSONL sessions)",
        defaults.get('claude_project_path', '')
    )

    python_path = prompt(
        "Python 3 interpreter path",
        defaults['python_path']
    )

    additional_context_target = prompt(
        "Additional CLAUDE.md or AGENTS.md compile target (optional)",
        ""
    )
    context_targets = list(defaults['context_targets'])
    if additional_context_target:
        context_targets.append(additional_context_target)

    agent_memory_path = prompt(
        "Agent Memory markdown directory",
        os.path.join(expand_path(vault_path), "05-Agent-Memory")
    )

    settings_json = prompt(
        "Path to Claude settings.json (for API key, optional)",
        defaults.get('settings_json', '')
    )

    print()

    # Step 2: Project names
    print("--- Projects ---")
    print()
    print("Enter project names, separated by commas.")
    print('Example: "my-research, side-project, blog"')
    project_input = prompt("Project names", "")
    project_names = [p.strip() for p in project_input.split(",") if p.strip()] if project_input else []

    print()

    # Step 3: Scan schedule
    print("--- Scanner Schedule ---")
    print()
    scan_day = prompt("Day of week for scanner", "SUN").upper()
    scan_hour = int(prompt("Hour (0-23)", "15"))
    scan_minute = int(prompt("Minute (0-59)", "0"))
    print()

    # Step 4: Topic map (optional)
    print("--- Topic Map (Optional) ---")
    print()
    print("The topic map groups session tags into topics for the auto-generated index.")
    print("Leave empty to skip — you can add topics later in config.yaml.")
    topic_map_input = prompt(
        "Add a topic? Format: tag, Topic Name / 中文名称, Description",
        ""
    )
    topic_map = {}
    if topic_map_input:
        parts = [p.strip() for p in topic_map_input.split(",", 2)]
        if len(parts) >= 2:
            topic_map[parts[0]] = [parts[1], parts[2] if len(parts) > 2 else ""]

    print()

    # Step 5: Confirm
    print("--- Summary ---")
    print(f"  Vault path:        {vault_path}")
    print(f"  Agent runtime:     {agent}")
    print(f"  Codex sessions:    {codex_sessions_path}")
    print(f"  ZCode DB:          {zcode_db_path or '(not set)'}")
    print(f"  Claude projects:   {claude_project_path or '(not set)'}")
    print(f"  Python:            {python_path}")
    print(f"  Compile targets:   {context_targets}")
    print(f"  Agent Memory:      {agent_memory_path}")
    print(f"  API settings:      {settings_json or '(not set)'}")
    print(f"  Projects:          {project_names if project_names else '(none — add later)'}")
    print(f"  Scan schedule:     {scan_day} at {scan_hour:02d}:{scan_minute:02d}")
    print()

    confirm = prompt("Proceed with setup? (y/n)", "y").lower()
    if confirm != 'y':
        print("Setup cancelled.")
        sys.exit(0)

    # Step 6: Create vault structure
    print()
    print("Creating vault structure...")
    vault = create_vault_structure(vault_path)
    print(f"  Vault created at: {vault}")

    # Step 7: Create project folders
    if project_names:
        print("Creating project folders...")
        created = create_project_folders(vault_path, project_names)
        for name in created:
            print(f"  + {name}")

    # Step 8: Generate config.yaml
    config = {
        'version': PRODUCT_VERSION,
        'product_id': CODE_PREFIX,
        'agent': agent,
        'transcript_agents': ['codex', 'claude', 'zcode'],
        'vault_path': vault_path,
        'codex_home': defaults['codex_home'],
        'codex_sessions_path': codex_sessions_path,
        'zcode_home': defaults['zcode_home'],
        'zcode_db_path': zcode_db_path,
        'claude_project_path': claude_project_path,
        'claude_md_path': '',
        'context_targets': context_targets,
        'agent_memory_path': agent_memory_path,
        'codex_profile_path': os.path.join(agent_memory_path, 'codex-profile'),
        'python_path': python_path,
        'projects': created if project_names else [],
        'project_keywords': {},
        'scan_on_start': True,
        'privacy': {
            'store_raw_transcripts': False,
            'store_transcript_metadata': True,
            'store_message_samples': False,
        },
        'personal_memory': {
            'enabled': True,
            'candidate_dir': '04-Feedback/_memory-candidates',
            'formal_path': '05-Agent-Memory/personal-memory.md',
            'candidate_threshold': 0.45,
            'direct_threshold': 0.85,
            'promote_seen_count': 2,
            'similarity_threshold': 0.5,
        },
        'api': {
            'settings_json': settings_json,
            'base_url': None,
            'model': None,
            'temperature': 0.3,
            'max_tokens': 2000,
            'max_retries': 3,
            'retry_backoff_sec': [2, 4, 8],
        },
        'log_level': 'INFO',
        'log_dir': '',
        'scan': {
            'day': scan_day,
            'hour': scan_hour,
            'minute': scan_minute,
        },
        'topic_map': topic_map if topic_map else {},
    }

    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, "config.yaml")
    durable_atomic_write(
        config_path,
        yaml.safe_dump(
            config,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        ),
        mode=0o600,
        preserve_existing_mode=False,
    )
    print(f"  Config written to: {config_path}")

    # Step 9: Validate
    print()
    print("Validating setup...")
    errors = validate_setup(vault_path, config_path)
    if errors:
        print("  WARNINGS:")
        for e in errors:
            print(f"    - {e}")
    else:
        print("  All checks passed!")

    # Step 10: Next steps
    print()
    print("=" * 60)
    print("  Setup complete!")
    print("=" * 60)
    print()
    print("Next steps:")
    print()
    print(f"  1. Open Obsidian and load vault: {vault_path}")
    print(f"  2. Add more projects with: python setup.py --add-project NAME")
    print(f"  3. Customize error taxonomy: 04-Feedback/error-taxonomy.md")
    print(f"  4. Customize topic map: edit config.yaml -> topic_map")
    print(f"  5. Verify and install the stable Codex runtime:")
    print(f"     cd {script_dir}")
    print(f"     python install_runtime.py --dry-run")
    print(f"     python install_runtime.py --verify-release")
    print(f"     python install_runtime.py")
    print(f"  6. Optional collection-only compatibility:")
    print(f"     python install_claude.py")
    print(f"     python install_zcode.py --context-only")
    print(
        f'  7. Verify launchd jobs: launchctl print gui/$(id -u)/'
        f'{NEW_LAUNCHD_LABELS["harvest"]}'
    )
    print()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
