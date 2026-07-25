"""Step 5: Compile vault memory into managed agent context blocks."""
import os
import re
import subprocess
import yaml
from datetime import datetime

from annotation_quality import collapse_runtime_duplicates, filter_runtime_quality
from knowledge_index import configured_adaptive_formal_paths
from memory_schema import (
    RUNTIME_SCHEMA_VERSION,
    canonical_project,
    is_valid_active_project_record,
    parse_active_formal_section,
    suppress_unmet_dependencies,
)
from safety import (
    durable_atomic_write,
    ensure_directory_tree,
    redact_sensitive,
    safe_filename,
    safe_vault_path,
    secure_list_directory,
    secure_read_bytes,
    split_frontmatter_text,
)

RULES_START = "<!-- COMPILED:RULES_START -->"
RULES_END = "<!-- COMPILED:RULES_END -->"
PROJECTS_START = "<!-- COMPILED:PROJECTS_START -->"
PROJECTS_END = "<!-- COMPILED:PROJECTS_END -->"
MAX_COMPILER_FILE_BYTES = 64 * 1024 * 1024
AGENT_MEMORY_ROOT_MARKER = ".agent-memory-beacon-root"

def _check_ownership(ownership_check):
    if ownership_check is not None:
        ownership_check()


def _atomic_write(path, content, ownership_check, mutation_io, root=None):
    _check_ownership(ownership_check)
    if mutation_io is not None:
        mutation_io.atomic_write(path, content, encoding="utf-8")
    else:
        durable_atomic_write(path, content, root=root)
    _check_ownership(ownership_check)


def _ensure_directory(path, ownership_check, mutation_io, root=None):
    _check_ownership(ownership_check)
    if mutation_io is not None:
        mutation_io.ensure_directory(path)
    elif root is not None:
        ensure_directory_tree(path, root)
    else:
        os.makedirs(path, exist_ok=True)
    _check_ownership(ownership_check)


def _sync_profile_compiled_blocks(
    profile_dir,
    rules_text,
    projects_text,
    dry_run,
    ownership_check,
    mutation_io,
):
    from codex_profile_sync import (
        MANAGED_AGENT_BLOCKS,
        SHARED_AGENTS,
        _marked_block,
        _normalized_source_managed_block,
        _replace_marked_block,
        _replace_or_install_managed_block,
        extract_managed_patch,
        sync_profile_agents_compiled_blocks,
    )

    if ownership_check is None:
        return sync_profile_agents_compiled_blocks(
            profile_dir,
            rules_text,
            projects_text,
            dry_run=dry_run,
        )
    shared_agents = os.path.join(profile_dir, SHARED_AGENTS)
    if not os.path.exists(shared_agents):
        return False
    with open(shared_agents, "r", encoding="utf-8") as handle:
        shared = handle.read()
    updated = shared
    for (start, end), body in zip(
        MANAGED_AGENT_BLOCKS,
        (rules_text, projects_text),
    ):
        if _marked_block(updated, start, end) is None:
            return False
        updated = _replace_marked_block(
            updated,
            start,
            end,
            "\n" + str(body).strip("\n") + "\n",
        )
    if extract_managed_patch(updated) is None:
        return False
    updated = _replace_or_install_managed_block(
        shared,
        _normalized_source_managed_block(updated),
    )
    if updated == shared or dry_run:
        return False
    _atomic_write(shared_agents, updated, ownership_check, mutation_io)
    return True


def run(
    cfg,
    dry_run=False,
    step_results=None,
    ownership_check=None,
    mutation_io=None,
    sync_agent_memory=True,
):
    _check_ownership(ownership_check)
    vault = cfg['vault_path']
    memory_dir = cfg.get('agent_memory_path') or os.path.join(vault, "05-Agent-Memory")
    results = {"rules_compiled": 0, "projects_compiled": 0, "dirty": False,
               "memory_rules_written": 0, "memory_index_updated": False,
               "context_targets_updated": 0, "context_targets_skipped": []}

    # ── Part A: agent context compilation ──
    targets = configured_context_targets(cfg)
    if not targets:
        results["claude_md_skipped"] = "context_targets not configured"
    else:
        rules_text = compile_rules_section(vault)
        projects_text = compile_projects_section(vault, cfg=cfg)
        results["rules_compiled"] = rules_text.count('\n')
        results["projects_compiled"] = projects_text.count('\n')
        target_errors = []
        for target in targets:
            try:
                if not os.path.exists(target):
                    results["context_targets_skipped"].append(
                        f"not found: {target}"
                    )
                    continue
                if (
                    not cfg.get("skip_git_probe", False)
                    and has_uncommitted_changes(target)
                ):
                    results["dirty"] = True
                    target_errors.append(
                        f"manual git changes detected: {target}"
                    )
                    continue
                with open(target, 'r', encoding='utf-8') as handle:
                    content = handle.read()
                missing = [
                    marker
                    for marker in (RULES_START, RULES_END, PROJECTS_START, PROJECTS_END)
                    if marker not in content
                ]
                if missing:
                    target_errors.append(
                        f"missing marker {missing[0]} in {target}"
                    )
                    continue
                new_content = replace_block(
                    content, RULES_START, RULES_END, rules_text
                )
                new_content = replace_block(
                    new_content, PROJECTS_START, PROJECTS_END, projects_text
                )
                if not dry_run and new_content != content:
                    _atomic_write(
                        target,
                        new_content,
                        ownership_check,
                        mutation_io,
                    )
                results["context_targets_updated"] += 1
            except Exception as exc:
                if mutation_io is not None:
                    raise
                target_errors.append(f"{target}: {exc}")
        if target_errors:
            results["context_target_errors"] = target_errors

        profile_dir = cfg.get("codex_profile_path")
        if profile_dir:
            _check_ownership(ownership_check)
            try:
                results["codex_profile_agents_updated"] = (
                    _sync_profile_compiled_blocks(
                        profile_dir,
                        rules_text,
                        projects_text,
                        dry_run,
                        ownership_check,
                        mutation_io,
                    )
                )
            except Exception as exc:
                if mutation_io is not None:
                    raise
                results["codex_profile_agents_error"] = str(exc)
            _check_ownership(ownership_check)

    # ── Part B: Agent Memory sync ──
    if sync_agent_memory:
        _check_ownership(ownership_check)
        try:
            mem_result = sync_to_agent_memory(
                vault,
                memory_dir,
                dry_run,
                ownership_check=ownership_check,
                mutation_io=mutation_io,
            )
            results.update(mem_result)
        except Exception as e:
            if mutation_io is not None:
                raise
            _check_ownership(ownership_check)
            results["memory_error"] = str(e)
    else:
        results["memory_sync_skipped"] = "disabled"

    _check_ownership(ownership_check)
    return results


def configured_context_targets(cfg):
    """Return de-duplicated Codex, Claude, and ZCode instruction targets."""
    targets = cfg.get("context_targets") or []
    if not isinstance(targets, list):
        raise TypeError("config context_targets must be a list")
    targets = list(targets)
    if cfg.get("claude_md_path"):
        targets.append(cfg["claude_md_path"])
    if cfg.get("migration_paths_are_canonical") is True:
        canonical = [str(path) for path in targets if path]
        if any(not os.path.isabs(path) for path in canonical):
            raise ValueError("migration context target is not canonical")
        return list(dict.fromkeys(canonical))
    expanded = [
        os.path.abspath(os.path.expandvars(os.path.expanduser(str(path))))
        for path in targets
        if path
    ]
    return list(dict.fromkeys(expanded))


def _agent_memory_output_root(vault, memory_dir, mutation_io):
    raw = os.path.expandvars(os.path.expanduser(os.fspath(memory_dir or "")))
    candidate = os.path.abspath(raw if os.path.isabs(raw) else os.path.join(vault, raw))
    try:
        inside_vault = os.path.commonpath([vault, candidate]) == vault
    except ValueError as exc:
        raise ValueError("agent_memory_path is invalid") from exc
    if inside_vault:
        return safe_vault_path(vault, candidate), vault
    if mutation_io is not None:
        return candidate, candidate
    if (
        os.path.realpath(candidate) != candidate
        or os.path.islink(candidate)
        or not os.path.isdir(candidate)
    ):
        raise ValueError(
            "external agent_memory_path must be a real owned directory"
        )
    marker = os.path.join(candidate, AGENT_MEMORY_ROOT_MARKER)
    try:
        marker_text = _secure_text(marker, candidate)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(
            "external agent_memory_path has no valid ownership marker"
        ) from exc
    if not marker_text.strip():
        raise ValueError("external agent_memory_path ownership marker is empty")
    return candidate, candidate


def _secure_text(path, root):
    data = secure_read_bytes(path, MAX_COMPILER_FILE_BYTES, root=root)
    if len(data) > MAX_COMPILER_FILE_BYTES:
        raise ValueError("compiler input exceeds the file-size limit")
    return data.decode("utf-8")


# ── Agent Memory Sync ────────────────────────────────────────
def sync_to_agent_memory(
    vault,
    memory_dir,
    dry_run,
    ownership_check=None,
    mutation_io=None,
):
    """Write new/updated rules to Agent Memory so they load next session.
    Rules marked 'active' or 'beta' in 00-Rules/ get a memory file.
    """
    vault = os.path.abspath(os.path.expanduser(os.fspath(vault)))
    memory_dir, memory_root = _agent_memory_output_root(
        vault,
        memory_dir,
        mutation_io,
    )
    rules_dir = safe_vault_path(vault, '00-Rules')
    try:
        _rule_directories, rule_files = secure_list_directory(rules_dir, vault)
    except (OSError, ValueError):
        return {"memory_rules_written": 0}

    if not dry_run:
        _ensure_directory(
            memory_dir,
            ownership_check,
            mutation_io,
            root=memory_root,
        )

    written = 0
    updated_index = False

    for f in rule_files:
        if not f.endswith('.md') or f.startswith('_'):
            continue

        fp = safe_vault_path(vault, '00-Rules', f)
        try:
            rule_content = _secure_text(fp, vault)
            frontmatter_text, body = split_frontmatter_text(rule_content)
            if frontmatter_text is None:
                continue
            fm = yaml.safe_load(frontmatter_text)
            if not isinstance(fm, dict):
                continue
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError):
            continue

        status = fm.get('status', '')
        if status not in ('active', 'beta'):
            continue

        rule_id = fm.get('rule_id', f.replace('.md', ''))
        title = fm.get('title', rule_id)
        category = fm.get('category', 'unknown')
        applies_to = fm.get('applies_to', [])
        if not isinstance(applies_to, list):
            applies_to = []
        applies_to = [str(item) for item in applies_to]

        # Generate memory slug
        slug = safe_filename(
            str(rule_id).lower().replace('_', '-'),
            default='rule',
            max_length=80,
        )

        # Build memory file content
        memory_md = f"""---
name: {slug}
description: {title} — {category}
metadata:
  type: reference
  rule_id: {rule_id}
  status: {status}
  applies_to: {applies_to}
  compiled_at: {datetime.now().isoformat()}
---

# {title}

Rule ID: `{rule_id}`
Category: {category}
Applies to: {', '.join(applies_to)}
Status: {status}

## Rule Content

{body.strip()[:1000]}
"""

        mem_path = os.path.join(memory_dir, f"{slug}.md")

        # Check if existing memory needs update
        should_write = True
        if os.path.exists(mem_path):
            try:
                existing = _secure_text(mem_path, memory_root)
                if existing.strip() == memory_md.strip():
                    should_write = False  # No change
            except (OSError, UnicodeDecodeError, ValueError):
                pass

        if should_write and not dry_run:
            _check_ownership(ownership_check)
            try:
                _atomic_write(
                    mem_path,
                    memory_md,
                    ownership_check,
                    mutation_io,
                    root=memory_root,
                )
                written += 1
            except Exception as e:
                if mutation_io is not None:
                    raise
                _check_ownership(ownership_check)
                print(f"    WARNING: Cannot write memory {slug}: {e}")
            _check_ownership(ownership_check)

    # Update MEMORY.md index if new rules were written
    if written > 0 and not dry_run:
        _check_ownership(ownership_check)
        try:
            rebuild_memory_index(
                memory_dir,
                ownership_check=ownership_check,
                mutation_io=mutation_io,
                root=memory_root,
            )
            updated_index = True
        except Exception as e:
            if mutation_io is not None:
                raise
            _check_ownership(ownership_check)
            print(f"    WARNING: Cannot rebuild memory index: {e}")
        _check_ownership(ownership_check)

    return {"memory_rules_written": written, "memory_index_updated": updated_index}


def rebuild_memory_index(
    memory_dir,
    ownership_check=None,
    mutation_io=None,
    root=None,
):
    """Rebuild MEMORY.md index from all memory files."""
    entries = []
    root = root or memory_dir
    memory_index = os.path.join(memory_dir, "MEMORY.md")
    _directories, files = secure_list_directory(memory_dir, root)
    for f in files:
        if not f.endswith('.md') or f == 'MEMORY.md' or f.startswith('DEPRECATED'):
            continue
        fp = os.path.join(memory_dir, f)
        try:
            content = _secure_text(fp, root)
            frontmatter_text, _body = split_frontmatter_text(content)
            if frontmatter_text is None:
                continue
            fm = yaml.safe_load(frontmatter_text)
            if not isinstance(fm, dict):
                continue
            name = fm.get('name', f.replace('.md', ''))
            description = fm.get('description', '')
            entries.append((name, description))
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError):
            continue

    lines = []
    for name, desc in sorted(entries):
        lines.append(f"- [{name}]({name}.md) — {desc}")

    index_content = '\n'.join(lines) + '\n'

    _atomic_write(
        memory_index,
        index_content,
        ownership_check,
        mutation_io,
        root=root,
    )

def has_uncommitted_changes(filepath):
    """Check dirty state with the fixed system Git, never inherited PATH."""
    git = "/usr/bin/git"
    if not os.path.isfile(git) or not os.access(git, os.X_OK):
        return False
    try:
        cwd = os.path.dirname(filepath)
        relpath = os.path.basename(filepath)
        result = subprocess.run(
            [git, "diff", "HEAD", "--name-only", "--", relpath],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
        )
        if relpath in result.stdout.splitlines():
            return True
        result = subprocess.run(
            [git, "diff", "--name-only", "--", relpath],
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=10,
        )
        return relpath in result.stdout.splitlines()
    except Exception:
        return False

def compile_rules_section(vault):
    """Generate rules table from 00-Rules/*.md active rules."""
    lines = ["| Rule ID | Title | Category | Applies To | Status |",
             "|---------|-------|----------|------------|--------|"]
    try:
        rules_dir = safe_vault_path(vault, '00-Rules')
        _directories, files = secure_list_directory(rules_dir, vault)
    except (OSError, ValueError):
        return '\n'.join(lines)

    for f in files:
        if not f.endswith('.md') or f.startswith('_'):
            continue
        fp = safe_vault_path(vault, '00-Rules', f)
        try:
            content = _secure_text(fp, vault)
            frontmatter_text, _body = split_frontmatter_text(content)
            if frontmatter_text is None:
                continue
            fm = yaml.safe_load(frontmatter_text)
            if not isinstance(fm, dict):
                continue
            if fm.get('status') in ('active', 'beta'):
                rule_id = fm.get('rule_id', '?')
                title = fm.get('title', '?')
                category = fm.get('category', '?')
                applies = ', '.join(fm.get('applies_to', []))
                status = fm.get('status', '?')
                lines.append(f"| {rule_id} | {title} | {category} | {applies} | {status} |")
        except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError, IndexError):
            continue

    return '\n'.join(lines)

def compile_projects_section(vault, cfg=None):
    """Generate project status and recent active formal memory."""
    lines = ["| Project | Decisions | Pitfalls | Insights | Last Session |",
             "|---------|-----------|----------|----------|-------------|"]
    recent = []
    allowed_ids = compilation_allowed_memory_ids(vault, cfg=cfg)
    adaptive_paths = _configured_adaptive_paths(vault, cfg)
    insights = load_promoted_insights(
        adaptive_paths['insights'],
        vault=vault,
    )
    insights = [item for item in insights if item.get('id') in allowed_ids]

    for proj, proj_dir in project_directories(vault):
        memory_dir = os.path.join(proj_dir, 'Memory')
        decisions_path = os.path.join(memory_dir, 'decisions.md')
        pitfalls_path = os.path.join(memory_dir, 'pitfalls.md')
        sessions_dir = os.path.join(memory_dir, 'sessions')
        try:
            memory_directories, memory_files = secure_list_directory(
                memory_dir,
                vault,
            )
        except (OSError, ValueError):
            memory_directories, memory_files = [], []
        decisions = (
            load_active_project_records(
                decisions_path,
                'decisions',
                proj,
                vault=vault,
            )
            if 'decisions.md' in memory_files
            else []
        )
        pitfalls = (
            load_active_project_records(
                pitfalls_path,
                'pitfalls',
                proj,
                vault=vault,
            )
            if 'pitfalls.md' in memory_files
            else []
        )
        decisions = [item for item in decisions if item.get('id') in allowed_ids]
        pitfalls = [item for item in pitfalls if item.get('id') in allowed_ids]
        last_session = (
            get_latest_session(sessions_dir, vault=vault)
            if 'sessions' in memory_directories
            else '-'
        )

        lines.append(
            f"| {proj} | {len(decisions)} | {len(pitfalls)} | "
            f"{sum(item.get('project') == proj for item in insights)} | {last_session} |"
        )
        recent.extend(
            {
                'kind': 'decision',
                'date': item.get('date', ''),
                'project': item.get('project') or proj,
                'text': item.get('text') or item.get('title', ''),
                'context': item.get('context') or item.get('summary', ''),
                'id': item.get('id', ''),
            }
            for item in decisions
        )
        recent.extend(
            {
                'kind': 'error',
                'date': item.get('date', ''),
                'project': item.get('project') or proj,
                'type': item.get('type') or item.get('error_type', ''),
                'resolution': item.get('resolution') or item.get('summary', ''),
                'id': item.get('id', ''),
            }
            for item in pitfalls
        )

    recent.sort(
        key=lambda item: (
            str(item.get('date') or ''),
            str(item.get('id') or ''),
        ),
        reverse=True,
    )

    lines.extend([
        "",
        "## Recent Project Memory",
        "",
        "These are compact facts compiled from Obsidian so new agent sessions can use prior decisions and resolved errors without scanning the vault first.",
        "",
        "### Recent Decisions",
        "",
        "| Date | Project | Decision | Context |",
        "|------|---------|----------|---------|",
    ])
    decision_rows = [
        (
            item.get('date', ''),
            item.get('project', ''),
            truncate_table_cell(item.get('text', ''), 100),
            truncate_table_cell(item.get('context', ''), 140),
        )
        for item in recent
        if item.get('kind') == 'decision'
    ]
    if decision_rows:
        for date, project, text, context in decision_rows[:20]:
            lines.append(f"| {date} | {project} | {text} | {context} |")
    else:
        lines.append("| - | - | - | - |")

    lines.extend([
        "",
        "### Recent Resolved Errors",
        "",
        "| Date | Project | Type | Resolution |",
        "|------|---------|------|------------|",
    ])
    error_rows = [
        (
            item.get('date', ''),
            item.get('project', ''),
            truncate_table_cell(item.get('type', ''), 50),
            truncate_table_cell(item.get('resolution', ''), 160),
        )
        for item in recent
        if item.get('kind') == 'error'
    ]
    if error_rows:
        for date, project, error_type, resolution in error_rows[:20]:
            lines.append(f"| {date} | {project} | `{error_type}` | {resolution} |")
    else:
        lines.append("| - | - | - | - |")

    lines.extend(
        compile_adaptive_memory_section(
            vault,
            allowed_ids=allowed_ids,
            cfg=cfg,
        )
    )
    return '\n'.join(lines)


def compilation_allowed_memory_ids(vault, cfg=None):
    """Return the same quality-safe formal IDs used by runtime recall."""
    records = []
    for project, project_dir in project_directories(vault):
        memory_dir = os.path.join(project_dir, 'Memory')
        try:
            _directories, files = secure_list_directory(memory_dir, vault)
        except (OSError, ValueError):
            continue
        decisions_path = os.path.join(memory_dir, 'decisions.md')
        pitfalls_path = os.path.join(memory_dir, 'pitfalls.md')
        if 'decisions.md' in files:
            records.extend(
                load_active_project_records(
                    decisions_path,
                    'decisions',
                    project,
                    vault=vault,
                )
            )
        if 'pitfalls.md' in files:
            records.extend(
                load_active_project_records(
                    pitfalls_path,
                    'pitfalls',
                    project,
                    vault=vault,
                )
            )
    adaptive_paths = _configured_adaptive_paths(vault, cfg)
    records.extend(
        load_promoted_personal_memory(
            adaptive_paths['personal-memory'],
            vault=vault,
        )
    )
    records.extend(
        load_promoted_skill_rules(
            adaptive_paths['skill-routing-rules'],
            vault=vault,
        )
    )
    records.extend(
        load_promoted_workflow_rules(
            adaptive_paths['workflow-rules'],
            vault=vault,
        )
    )
    records.extend(
        load_promoted_insights(
            adaptive_paths['insights'],
            vault=vault,
        )
    )
    normalized_records = [_compilation_quality_record(item) for item in records]
    quality_eligible, _suppressed_quality = filter_runtime_quality(
        normalized_records
    )
    eligible, _suppressed_dependencies = suppress_unmet_dependencies(
        quality_eligible
    )
    eligible, _duplicate_groups = collapse_runtime_duplicates(eligible)
    return {str(item.get('id') or '') for item in eligible}


def _compilation_quality_record(item):
    """Adapt compiler loader shapes to the canonical quality-gate shape."""
    record = dict(item or {})
    if "text" in record:
        record.update(
            {
                "type": "decision",
                "title": record.get("text", ""),
                "summary": record.get("context", ""),
            }
        )
    elif "resolution" in record:
        error_type = record.get("type", "")
        record.update(
            {
                "type": "error",
                "title": error_type,
                "summary": record.get("resolution", ""),
            }
        )
    elif "content" in record:
        record["summary"] = record.get("content", "")
    return record


def project_directories(vault):
    try:
        projects_dir = safe_vault_path(vault, '01-Projects')
        directories, _files = secure_list_directory(projects_dir, vault)
    except (OSError, ValueError):
        return []
    return [
        (name, os.path.join(projects_dir, name))
        for name in directories
        if canonical_project(name) == name
    ]


def directory_without_symlink(path):
    return os.path.isdir(path) and not os.path.islink(path)


def regular_file_without_symlink(path):
    return os.path.isfile(path) and not os.path.islink(path)


def load_active_project_records(path, key, default_project, vault=None):
    frontmatter, _body = load_schema_2_document(path, root=vault)
    if frontmatter is None:
        return []
    expected_project = canonical_project(default_project)
    frontmatter_project = str(frontmatter.get('project') or '').strip()
    if (
        frontmatter_project != expected_project
        or canonical_project(frontmatter_project) != expected_project
    ):
        return []
    items = frontmatter.get(key)
    if not isinstance(items, list):
        return []
    records = []
    for item in items:
        if not is_active_project_record(item, key, expected_project):
            continue
        records.append(dict(item))
    return records


def is_active_project_record(item, key, expected_project):
    if key == 'decisions':
        memory_type = 'decision'
    elif key == 'pitfalls':
        memory_type = 'error'
    else:
        return False
    return is_valid_active_project_record(item, memory_type, expected_project)


def compile_adaptive_memory_section(vault, allowed_ids=None, cfg=None):
    """Compile promoted memory only; candidates never become agent instructions."""
    adaptive_paths = _configured_adaptive_paths(vault, cfg)
    personal = load_promoted_personal_memory(
        adaptive_paths['personal-memory'],
        vault=vault,
    )
    skills = load_promoted_skill_rules(
        adaptive_paths['skill-routing-rules'],
        vault=vault,
    )
    workflows = load_promoted_workflow_rules(
        adaptive_paths['workflow-rules'],
        vault=vault,
    )
    insights = load_promoted_insights(
        adaptive_paths['insights'],
        vault=vault,
    )
    if allowed_ids is not None:
        personal = [item for item in personal if item.get('id') in allowed_ids]
        skills = [item for item in skills if item.get('id') in allowed_ids]
        workflows = [item for item in workflows if item.get('id') in allowed_ids]
        insights = [item for item in insights if item.get('id') in allowed_ids]

    lines = [
        "",
        "## Promoted Adaptive Memory",
        "",
        "Only repeated or explicit high-confidence memory is compiled here; candidate files are excluded.",
        "",
        "### Personal Preferences And Project Rules",
        "",
    ]
    if personal:
        for item in personal[:12]:
            project = f"[{item['project']}] " if item.get('project') else ""
            lines.append(f"- {project}{truncate_table_cell(item['content'], 180)}")
    else:
        lines.append("- -")

    lines.extend(["", "### Skill Routing Rules", ""])
    if skills:
        for item in skills[:12]:
            lines.append(
                f"- `{item['name']}`: when {truncate_table_cell(item['when'], 150)}; "
                f"avoid when {truncate_table_cell(item['avoid'], 100)}"
            )
    else:
        lines.append("- -")

    lines.extend(["", "### Workflow Rules", ""])
    if workflows:
        for item in workflows[:12]:
            lines.append(
                f"- `{item['name']}`: trigger {truncate_table_cell(item['trigger'], 140)}; "
                f"do {truncate_table_cell(item['behavior'], 180)}; "
                f"do not apply when {truncate_table_cell(item['avoid'], 100)}"
            )
    else:
        lines.append("- -")
    lines.extend(
        [
            "",
            "### Insight Memory",
            "",
            f"- Formal insights available dynamically: {len(insights)}",
            "- Insight bodies are not compiled here; the runtime injects only relevant exploration context.",
        ]
    )
    return lines


def _configured_adaptive_paths(vault, cfg=None):
    effective_cfg = dict(cfg or {})
    effective_cfg['vault_path'] = vault
    paths = {
        'personal-memory': '',
        'skill-routing-rules': '',
        'workflow-rules': '',
        'insights': '',
    }
    paths.update(
        {
            note_type: path
            for path, note_type in configured_adaptive_formal_paths(
                effective_cfg
            ).items()
        }
    )
    return paths


def load_promoted_personal_memory(path, vault=None):
    _frontmatter, content = load_schema_2_document(path, root=vault)
    if _frontmatter is None:
        return []
    items = []
    for title, section in markdown_sections(content):
        metadata = active_formal_section_metadata(title, section, 'personal')
        if metadata is None:
            continue
        items.append({
            'id': metadata['id'],
            'type': metadata['type'],
            'title': redact_sensitive(title),
            'content': redact_sensitive(metadata['summary']),
            'project': metadata['project'],
            'scope': metadata['scope'],
            'requires': metadata.get('requires', []),
        })
    return items


def load_promoted_skill_rules(path, vault=None):
    _frontmatter, content = load_schema_2_document(path, root=vault)
    if _frontmatter is None:
        return []
    items = []
    for title, section in markdown_sections(content):
        metadata = active_formal_section_metadata(title, section, 'skill')
        if metadata is None:
            continue
        items.append({
            'id': metadata['id'],
            'type': 'skill',
            'name': redact_sensitive(metadata['name']),
            'when': redact_sensitive(metadata['when']),
            'avoid': redact_sensitive(metadata['avoid']),
            'project': metadata.get('project', ''),
            'scope': metadata.get('scope', 'global'),
            'requires': metadata.get('requires', []),
        })
    return items


def load_promoted_workflow_rules(path, vault=None):
    _frontmatter, content = load_schema_2_document(path, root=vault)
    if _frontmatter is None:
        return []
    items = []
    for title, section in markdown_sections(content):
        metadata = active_formal_section_metadata(title, section, 'workflow')
        if metadata is None:
            continue
        items.append({
            'id': metadata['id'],
            'type': 'workflow',
            'name': redact_sensitive(metadata['name']),
            'trigger': redact_sensitive(metadata['trigger']),
            'behavior': redact_sensitive(metadata['behavior']),
            'avoid': redact_sensitive(metadata['avoid']),
            'project': metadata.get('project', ''),
            'scope': metadata.get('scope', 'global'),
            'requires': metadata.get('requires', []),
        })
    return items


def load_promoted_insights(path, vault=None):
    """Load formal Insight metadata for quality gating and counts only."""
    _frontmatter, content = load_schema_2_document(path, root=vault)
    if _frontmatter is None:
        return []
    items = []
    for title, section in markdown_sections(content):
        metadata = active_formal_section_metadata(title, section, 'insight')
        if metadata is None:
            continue
        items.append(
            {
                'id': metadata['id'],
                'type': 'insight',
                'title': redact_sensitive(title),
                'summary': redact_sensitive(metadata['summary']),
                'project': metadata.get('project', ''),
                'scope': metadata.get('scope', 'global'),
                'maturity': metadata.get('maturity', 'seed'),
                'requires': metadata.get('requires', []),
            }
        )
    return items


def load_schema_2_document(path, root=None):
    content = read_text(path, root=root)
    match = re.match(
        r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
        content,
        re.DOTALL,
    )
    if not match:
        return None, ""
    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None, ""
    if (
        not isinstance(frontmatter, dict)
        or frontmatter.get('schema_version') != RUNTIME_SCHEMA_VERSION
    ):
        return None, ""
    return frontmatter, content[match.end():]


def active_formal_section_metadata(title, section, kind):
    return parse_active_formal_section(title, section, kind)


def read_text(path, root=None):
    try:
        return _secure_text(path, root or os.path.dirname(os.path.abspath(path)))
    except (OSError, UnicodeDecodeError, ValueError):
        return ""


def markdown_sections(content):
    headings = list(re.finditer(r"^##\s+(.+?)\s*$", content, re.MULTILINE))
    for index, match in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
        yield match.group(1).strip(), content[match.end():end].strip()


def load_recent_session_memories(project, sessions_dir, vault=None):
    memories = []
    root = vault or sessions_dir
    try:
        _directories, files = secure_list_directory(sessions_dir, root)
    except (OSError, ValueError):
        return []
    for filename in reversed(files):
        if not filename.endswith('.md') or filename.startswith('_'):
            continue
        path = os.path.join(sessions_dir, filename)
        try:
            content = _secure_text(path, root)
            frontmatter_text, _body = split_frontmatter_text(content)
            if frontmatter_text is None:
                continue
            fm = yaml.safe_load(frontmatter_text) or {}
            if not isinstance(fm, dict):
                continue
            fm.setdefault('project', project)
            memories.append(fm)
        except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError):
            continue
    return memories


def truncate_table_cell(value, max_length):
    text = ' '.join(str(value or '').split())
    text = text.replace('|', '\\|')
    if len(text) <= max_length:
        return text
    return text[:max_length - 1].rstrip() + '…'

def replace_block(content, start_marker, end_marker, new_content):
    """Replace content between start and end markers."""
    before = content.split(start_marker)[0]
    after = content.split(end_marker)[1]
    return before + start_marker + '\n' + new_content + '\n' + end_marker + after

def count_frontmatter_items(path, key, root=None):
    try:
        content = _secure_text(
            path,
            root or os.path.dirname(os.path.abspath(path)),
        )
        frontmatter_text, _body = split_frontmatter_text(content)
        if frontmatter_text is None:
            return 0
        fm = yaml.safe_load(frontmatter_text)
        return len(fm.get(key, []))
    except (OSError, UnicodeDecodeError, ValueError, yaml.YAMLError, IndexError):
        return 0

def get_latest_session(sessions_dir, vault=None):
    try:
        _directories, names = secure_list_directory(
            sessions_dir,
            vault or sessions_dir,
        )
    except (OSError, ValueError):
        return '-'
    files = [f for f in names if f.endswith('.md') and not f.startswith('_')]
    if not files:
        return '-'
    return sorted(files)[-1][:10]  # First 10 chars = date
