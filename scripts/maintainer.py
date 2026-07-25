"""Step 3: Rule maintenance — smart cards, rule merging, expiration, cleanup.

Philosophy: Every card must contain a CONCRETE rule, not "TBD".
Before creating a new rule, check if existing rules already cover it.
When rules overlap, suggest merging — don't pile up.
"""
import os
import yaml
import shutil
from datetime import datetime, timedelta

from memory_effectiveness import write_effectiveness_report
from memory_lifecycle import sweep_expired
from memory_promotion import refresh_promotion_proposals
from safety import (
    redact_sensitive,
    safe_filename,
    safe_vault_path,
    split_frontmatter_text,
)

def run(cfg, dry_run=False, step_results=None):
    vault = cfg['vault_path']
    learnings = step_results.get('analyze', {}).get('learnings', []) if step_results else []
    patterns = step_results.get('analyze', {}).get('patterns', []) if step_results else []

    actions = []
    cards_generated = 0
    merges_suggested = 0

    # ── Process learnings (from deep analysis) ──
    for l in learnings:
        action = l.get('action', 'review')
        if action == 'new_rule':
            card_id = generate_learning_card(vault, l, dry_run)
            if card_id:
                cards_generated += 1
                actions.append(f"New rule card: {card_id}")
        elif action == 'merge':
            card_id = generate_merge_card(vault, l, dry_run)
            if card_id:
                merges_suggested += 1
                actions.append(f"Merge card: {card_id}")
        elif action == 'reinforce':
            # Update last_triggered on the existing rule
            rule_id = l.get('suggested_rule_id', '')
            if rule_id:
                touch_rule(vault, rule_id, dry_run)
                actions.append(f"Reinforced: {rule_id}")

    # Fallback: if no learnings (LLM+heuristic both failed), use raw patterns
    if not learnings:
        for p in patterns:
            if len(p.get('projects', [])) >= 3:
                card_id = generate_pattern_card(vault, p, dry_run)
                if card_id:
                    cards_generated += 1
                    actions.append(f"Pattern card: {card_id}")

    # ── Rule maintenance ──
    rules_dir = os.path.join(vault, '00-Rules')
    archived = expire_rules(rules_dir, dry_run)
    promoted = promote_beta_rules(rules_dir, dry_run)
    actions.extend(archived)
    actions.extend(promoted)

    # Clean _rejected/ (30+ days)
    cleaned = clean_rejected(vault, dry_run)
    actions.extend(cleaned)

    # Update trigger_counts from session summaries
    updated = update_trigger_counts(vault, dry_run)
    actions.extend(updated)

    lifecycle_results = sweep_expired(cfg, apply=not dry_run)
    for item in lifecycle_results:
        verb = "Expired" if item.applied else "Would expire"
        actions.append(f"{verb} formal memory: {item.memory_id}")
    expired_memories = sum(bool(item.applied) for item in lifecycle_results)

    effectiveness_refreshed = False
    if not dry_run:
        try:
            effectiveness_refreshed = bool(
                write_effectiveness_report(vault, cfg).get("written")
            )
        except Exception as exc:
            actions.append(f"Effectiveness report warning: {exc}")

    promotion_result = {"proposals": 0, "written": 0, "paths": []}
    try:
        promotion_result = refresh_promotion_proposals(
            vault,
            cfg,
            apply=not dry_run,
        )
    except Exception as exc:
        actions.append(f"Promotion proposal warning: {exc}")

    return {
        "cards_generated": cards_generated,
        "merges_suggested": merges_suggested,
        "rules_archived": len(archived),
        "rules_promoted": len(promoted),
        "rejected_cleaned": len(cleaned),
        "formal_memories_due": len(lifecycle_results),
        "formal_memories_expired": expired_memories,
        "effectiveness_report_refreshed": effectiveness_refreshed,
        "promotion_proposals": int(promotion_result.get("proposals") or 0),
        "promotion_proposals_written": int(promotion_result.get("written") or 0),
        "actions": actions
    }


# ── Smart Card Generation (from deep learnings) ────────────

def generate_learning_card(vault, learning, dry_run):
    """Create an _inbox/ approval card with CONCRETE rule text from a learning.
    Returns card_id or None if already exists."""
    inbox_dir = safe_vault_path(vault, '00-Rules', '_inbox')
    rule_id = redact_sensitive(learning.get('suggested_rule_id', ''))
    if not rule_id:
        return None

    card_id = "inbox-" + safe_filename(
        rule_id.lower(), default='rule', max_length=80
    )
    card_path = safe_vault_path(inbox_dir, f"{card_id}.md")

    if os.path.exists(card_path) and not dry_run:
        return None

    root_cause = redact_sensitive(learning.get('root_cause', 'Unknown'))
    principle = redact_sensitive(learning.get('principle', ''))
    rule_text = redact_sensitive(learning.get('suggested_rule_text', ''))
    impact = redact_sensitive(learning.get('impact', 'medium'))
    total_occ = learning.get('total_occurrences', 0)
    projects = [redact_sensitive(item) for item in learning.get('projects_affected', [])]
    title = redact_sensitive(learning.get('suggested_rule_title', rule_id))

    # If the LLM didn't provide rule text, synthesize from principle
    if not rule_text or rule_text.strip() == '':
        rule_text = f"原则: {principle}\n\n具体操作: (需要人工补充)"

    frontmatter = {
        'status': 'pending',
        'proposed': datetime.now().strftime('%Y-%m-%d'),
        'proposed_by': 'scanner',
        'rule_id': rule_id,
        'one_liner': title,
        'root_cause': root_cause,
        'principle': principle,
        'impact': impact,
        'total_occurrences': total_occ,
        'affected_projects': projects,
        'priority': 'high' if impact == 'high' else 'medium',
        'review_deadline': (
            datetime.now() + timedelta(days=14 if impact == 'high' else 30)
        ).strftime('%Y-%m-%d'),
    }
    body = f"""# Proposed Rule: {title}

## Root Cause
{root_cause}

## Principle
{principle}

## Evidence
- {total_occ} occurrences across {len(projects)} project(s): {', '.join(projects)}
- Impact: {impact}

## Rule Text
{rule_text}

## Affected Errors
{', '.join(redact_sensitive(item) for item in learning.get('affected_errors', []))}

---
Approve: Move this file from _inbox/ to 00-Rules/ and set status: active
Reject: Move to _inbox/_rejected/
"""
    content = render_markdown(frontmatter, body)

    if not dry_run:
        os.makedirs(inbox_dir, exist_ok=True)
        if os.path.exists(card_path):
            backup_pre_modification(vault, card_path)
        tmp_path = card_path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp_path, card_path)

    return card_id


def render_markdown(frontmatter, body):
    fm_yaml = yaml.dump(
        frontmatter,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    return f"---\n{fm_yaml}---\n\n{body.rstrip()}\n"


def generate_merge_card(vault, learning, dry_run):
    """Create a card suggesting merging of overlapping rules."""
    inbox_dir = safe_vault_path(vault, '00-Rules', '_inbox')
    related = [
        redact_sensitive(item)
        for item in learning.get('related_existing_rules', [])
    ]
    if len(related) < 2:
        return None

    card_id = "merge-" + safe_filename(
        '-'.join(r.lower().replace('_', '-') for r in related[:3]),
        default='rules',
        max_length=80,
    )
    card_path = safe_vault_path(inbox_dir, f"{card_id}.md")

    if os.path.exists(card_path) and not dry_run:
        return None

    merge_text = redact_sensitive(learning.get('merge_suggestion', ''))
    projects = [
        redact_sensitive(item)
        for item in learning.get('projects_affected', [])
    ]
    one_liner = f"Merge related rules: {' + '.join(related)}"
    frontmatter = {
        'status': 'pending',
        'proposed': datetime.now().strftime('%Y-%m-%d'),
        'proposed_by': 'scanner',
        'type': 'merge',
        'one_liner': one_liner,
        'affected_projects': projects,
        'priority': 'medium',
        'rules_to_merge': related,
        'rationale': merge_text,
        'review_deadline': (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d'),
    }
    body = f"""# Merge Suggestion: {' + '.join(related)}

## Rationale
{merge_text}

## Rules to Merge
{chr(10).join(f'- {r}' for r in related)}

## Suggested Action
1. Review each rule's content
2. Create a new unified rule
3. Archive the old rules
"""
    content = render_markdown(frontmatter, body)
    if not dry_run:
        os.makedirs(inbox_dir, exist_ok=True)
        tmp_path = card_path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp_path, card_path)
    return card_id


def generate_pattern_card(vault, pattern, dry_run):
    """Fallback: generate a card from a raw pattern (no deep analysis available)."""
    inbox_dir = safe_vault_path(vault, '00-Rules', '_inbox')
    projects = [redact_sensitive(item) for item in pattern['projects']]
    etype = redact_sensitive(pattern['error_type'])
    n_projects = len(projects)
    n_occurrences = pattern['count']

    confidence_auto = min(0.95, 0.3 * (n_projects ** 0.5) + 0.05 * n_occurrences)

    card_id = "inbox-" + safe_filename(etype, default='pattern', max_length=80)
    card_path = safe_vault_path(inbox_dir, f"{card_id}.md")
    if os.path.exists(card_path) and not dry_run:
        return None

    resolutions = list(set(
        redact_sensitive(c['resolution'])
        for c in pattern['contexts'] if c['resolution']
    ))

    frontmatter = {
        'status': 'pending',
        'proposed': datetime.now().strftime('%Y-%m-%d'),
        'proposed_by': 'scanner (pattern fallback - no deep analysis)',
        'confidence_auto': round(confidence_auto, 2),
        'one_liner': f"Error pattern: {etype} across {', '.join(projects)}",
        'evidence': f"{n_projects} projects, {n_occurrences} occurrences - {', '.join(resolutions[:2])}",
        'affected_projects': projects,
        'priority': 'high' if confidence_auto > 0.7 else 'medium',
        'review_deadline': (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d'),
    }
    contexts = [
        {
            **context,
            'project': redact_sensitive(context.get('project', '')),
            'session': redact_sensitive(context.get('session', '')),
            'resolution': redact_sensitive(context.get('resolution', '')),
        }
        for context in pattern['contexts']
    ]
    body = f"""# Proposed Rule: {etype}

## Pattern
Error type `{etype}` detected in {n_projects} projects ({n_occurrences} total).

## Evidence
{chr(10).join(f'- [{c["project"]}] {c["session"]}: {c["resolution"][:100]}' for c in contexts[:5])}

## Suggested Rule
⚠️ This card was generated from raw patterns without deep analysis.
A human should review and write the specific rule text.
"""
    content = render_markdown(frontmatter, body)
    if not dry_run:
        os.makedirs(inbox_dir, exist_ok=True)
        tmp_path = card_path + '.tmp'
        with open(tmp_path, 'w', encoding='utf-8') as f:
            f.write(content)
        os.replace(tmp_path, card_path)
    return card_id


def touch_rule(vault, rule_id, dry_run):
    """Update last_triggered timestamp on an existing rule."""
    filename = safe_filename(rule_id, default='rule', max_length=80) + '.md'
    rule_path = safe_vault_path(vault, '00-Rules', filename)
    if not os.path.exists(rule_path):
        return
    if dry_run:
        return
    try:
        with open(rule_path, 'r', encoding='utf-8') as f:
            content = f.read()
        frontmatter_text, body = split_frontmatter_text(content)
        if frontmatter_text is None:
            return
        fm = yaml.safe_load(frontmatter_text)
        fm['last_triggered'] = datetime.now().strftime('%Y-%m-%d')
        new_fm = yaml.dump(fm, allow_unicode=True, default_flow_style=False)
        new_content = f"---\n{new_fm}---\n{body}"
        tmp = rule_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(new_content)
        os.replace(tmp, rule_path)
    except Exception:
        pass


def expire_rules(rules_dir, dry_run):
    """Archive rules based on expires field and last_triggered age."""
    actions = []
    archive_dir = os.path.join(rules_dir, '_archive')
    vault = os.path.dirname(os.path.abspath(rules_dir))
    today = datetime.now()

    for f in os.listdir(rules_dir):
        if not f.endswith('.md') or f.startswith('_'):
            continue
        fp = os.path.join(rules_dir, f)
        try:
            with open(fp, 'r', encoding='utf-8') as fh:
                content = fh.read()
            frontmatter_text, _body = split_frontmatter_text(content)
            if frontmatter_text is None:
                continue
            fm = yaml.safe_load(frontmatter_text)
        except (yaml.YAMLError, IndexError):
            continue
        if not isinstance(fm, dict):
            continue

        should_archive = False
        reason = ""

        # Hard expiry
        if fm.get('expires'):
            try:
                expires_date = datetime.fromisoformat(str(fm['expires']))
            except (TypeError, ValueError):
                continue
            if today > expires_date:
                should_archive = True
                reason = f"expired {fm['expires']}"

        # Soft expiry: >=90 days since last_triggered
        if not should_archive and fm.get('last_triggered'):
            try:
                last_trig = datetime.fromisoformat(str(fm['last_triggered']))
            except (TypeError, ValueError):
                continue
            if (today - last_trig).days >= 90:
                should_archive = True
                reason = f"unused for {(today - last_trig).days} days"

        if should_archive:
            if not dry_run:
                backup_pre_modification(vault, fp)
                fm['status'] = 'archived'
                fm['archived_at'] = today.strftime('%Y-%m-%d')
                fm['archived_by'] = 'script'
                fm['archival_reason'] = reason
                rewrite_rule_frontmatter(fp, content, fm)
                os.makedirs(archive_dir, exist_ok=True)
                os.replace(fp, os.path.join(archive_dir, f))
            actions.append(f"Archived rule {f}: {reason}")

    return actions

def promote_beta_rules(rules_dir, dry_run):
    """Promote beta rules to active after 30-day observation."""
    actions = []
    vault = os.path.dirname(os.path.abspath(rules_dir))
    today = datetime.now()

    for f in os.listdir(rules_dir):
        if not f.endswith('.md') or f.startswith('_'):
            continue
        fp = os.path.join(rules_dir, f)
        try:
            with open(fp, 'r', encoding='utf-8') as fh:
                content = fh.read()
            frontmatter_text, _body = split_frontmatter_text(content)
            if frontmatter_text is None:
                continue
            fm = yaml.safe_load(frontmatter_text)
        except (yaml.YAMLError, IndexError):
            continue
        if not isinstance(fm, dict):
            continue

        if fm.get('status') == 'beta' and fm.get('beta_since'):
            try:
                beta_since = datetime.fromisoformat(str(fm['beta_since']))
            except (TypeError, ValueError):
                continue
            if (today - beta_since).days >= 30:
                if not dry_run:
                    backup_pre_modification(vault, fp)
                    fm['status'] = 'active'
                    rewrite_rule_frontmatter(fp, content, fm)
                actions.append(f"Promoted rule {f}: beta->active")

    return actions


def rewrite_rule_frontmatter(path, content, frontmatter):
    """Persist updated rule metadata without changing its Markdown body."""
    frontmatter_text, body = split_frontmatter_text(content)
    if frontmatter_text is None:
        raise ValueError("malformed Markdown frontmatter")
    new_content = (
        "---\n"
        + yaml.dump(
            frontmatter,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        + "---\n"
        + body.lstrip('\n')
    )
    tmp_path = path + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as handle:
        handle.write(new_content)
    os.replace(tmp_path, path)

def clean_rejected(vault, dry_run):
    """Delete rejected cards older than 30 days."""
    actions = []
    rejected_dir = os.path.join(vault, '00-Rules', '_inbox', '_rejected')
    if not os.path.exists(rejected_dir):
        return actions
    today = datetime.now()

    for f in os.listdir(rejected_dir):
        fp = os.path.join(rejected_dir, f)
        mtime = datetime.fromtimestamp(os.path.getmtime(fp))
        if (today - mtime).days >= 30:
            if not dry_run:
                os.remove(fp)
            actions.append(f"Cleaned rejected: {f}")

    return actions

def update_trigger_counts(vault, dry_run):
    """Update rule trigger_count from weekly session summaries."""
    actions = []
    # This runs after analyzer has processed all sessions
    # For now, placeholder — detailed implementation in Phase 2 iteration
    return actions

def backup_pre_modification(vault, filepath):
    """Copy file to _rollback before modification."""
    rollback_dir = os.path.join(vault, '04-Feedback', '_rollback', datetime.now().strftime('%Y-%m-%d'))
    os.makedirs(rollback_dir, exist_ok=True)
    rel = os.path.relpath(filepath, vault)
    dst = os.path.join(rollback_dir, rel)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(filepath, dst)
