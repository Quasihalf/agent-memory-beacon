"""Step 1: Track transcript versions and optionally store redacted artifacts."""
import hashlib
import json
import os
import shutil
from transcript_utils import (
    get_transcript_roots,
    iter_transcript_files,
    parse_transcript,
    session_id_from_path,
    transcript_version,
)
from safety import (
    redact_sensitive,
    safe_filename,
    safe_vault_path,
    split_frontmatter_text,
)

def run(cfg, dry_run=False, full=False):
    vault = cfg['vault_path']
    raw_dir = os.path.join(vault, '04-Feedback', '_raw-sessions')
    heartbeat_path = os.path.join(vault, '04-Feedback', 'heartbeat.md')
    privacy = cfg.get('privacy') or {}
    store_raw = bool(privacy.get('store_raw_transcripts', False))
    store_metadata = bool(privacy.get('store_transcript_metadata', True))
    store_samples = bool(privacy.get('store_message_samples', False))

    # Load processed sessions from heartbeat
    processed = load_processed_sessions(heartbeat_path)

    # Find new/changed sessions
    new_sessions = []
    skipped_agent = 0
    for fp in iter_transcript_files(get_transcript_roots(cfg)):
        session_id = session_id_from_path(fp)

        # Filter: skip agent sub-sessions (inflate counts, share parent context)
        if session_id.startswith('agent-') or 'subagent' in fp.lower():
            skipped_agent += 1
            processed[session_id] = source_fingerprint(fp)
            continue

        fingerprint = source_fingerprint(fp)

        if full or session_id not in processed or processed[session_id] != fingerprint:
            parsed = parse_transcript(fp)
            if parsed.get('meta', {}).get('is_subagent'):
                skipped_agent += 1
                processed[session_id] = fingerprint
                continue
            new_sessions.append((fp, session_id, fingerprint, parsed))

    processed_count = 0
    for fp, session_id, fingerprint, parsed in new_sessions:
        if not dry_run:
            if store_raw or store_metadata:
                os.makedirs(raw_dir, exist_ok=True)
            artifact_id = transcript_artifact_id(session_id, fp)
            if store_raw:
                archive_path = safe_vault_path(
                    vault, '04-Feedback', '_raw-sessions', f"{artifact_id}.jsonl"
                )
                tmp_archive = archive_path + '.tmp'
                write_redacted_transcript(parsed, tmp_archive)
                os.replace(tmp_archive, archive_path)
            if store_metadata:
                md_path = safe_vault_path(
                    vault, '04-Feedback', '_raw-sessions', f"{artifact_id}.md"
                )
                tmp_md = md_path + '.tmp'
                generate_md_summary_from_parsed(
                    parsed, tmp_md, include_samples=store_samples
                )
                os.replace(tmp_md, md_path)
        processed[session_id] = fingerprint
        processed_count += 1

    # Nutstore backup — atomic via tmp directory
    backup_vault = cfg.get('backup_path')
    if backup_vault and os.path.exists(os.path.dirname(backup_vault)):
        if not dry_run:
            sync_to_nutstore_atomic(vault, backup_vault)

    return {
        "new_sessions": processed_count,
        "total_tracked": len(processed),
        "processed_ids": dict(processed),
        "skipped_agent_sessions": skipped_agent
    }

def load_processed_sessions(heartbeat_path):
    """Extract processed_sessions from heartbeat frontmatter."""
    if not os.path.exists(heartbeat_path):
        return {}
    with open(heartbeat_path, 'r', encoding='utf-8') as f:
        content = f.read()
    frontmatter_text, _body = split_frontmatter_text(content)
    if frontmatter_text is None:
        return {}
    import yaml
    fm = yaml.safe_load(frontmatter_text) or {}
    if not isinstance(fm, dict):
        return {}
    backed_up = fm.get('backed_up_sessions')
    if isinstance(backed_up, dict):
        return backed_up
    processed = fm.get('processed_sessions', {})
    return processed if isinstance(processed, dict) else {}

def generate_md_summary_to_path(jsonl_path, md_path):
    """Generate a lightweight Markdown summary from JSONL metadata.
    Writes to the given path (caller handles atomicity)."""
    parsed = parse_transcript(jsonl_path)
    generate_md_summary_from_parsed(parsed, md_path, include_samples=False)


def generate_md_summary_from_parsed(parsed, md_path, include_samples=False):
    """Write redacted metadata; message samples are opt-in and always redacted."""
    meta = parsed.get('meta', {})
    messages = parsed.get('messages', [])

    first_ts = meta.get('timestamp')
    title = redact_sensitive(
        meta.get('thread_name') or meta.get('title') or meta.get('session_id') or 'Untitled'
    )
    user_msgs = []
    if include_samples:
        user_msgs = [
            redact_sensitive(m['text'])[:200]
            for m in messages
            if m.get('role') in ('user', 'event', 'user_message')
        ][:5]

    frontmatter = {
        'date': first_ts[:10] if first_ts else 'unknown',
        'title': title or 'Untitled',
        'messages_total': len(messages),
        'messages_sampled': len(user_msgs),
        'content_storage': 'redacted-samples' if include_samples else 'metadata-only',
    }

    with open(md_path, 'w', encoding='utf-8') as f:
        import yaml
        f.write("---\n")
        yaml.dump(frontmatter, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        f.write("---\n\n")
        f.write(f"# {title or 'Untitled Session'}\n\n")
        f.write(f"Date: {first_ts[:10] if first_ts else 'unknown'}\n\n")
        f.write(f"Messages: {len(messages)}\n")
        if user_msgs:
            f.write("\n## Redacted User Message Samples\n\n")
            for i, msg in enumerate(user_msgs[:5]):
                f.write(f"{i+1}. {msg}\n")

def generate_md_summary(jsonl_path, md_path):
    """DEPRECATED: use generate_md_summary_to_path instead."""
    generate_md_summary_to_path(jsonl_path, md_path)


def transcript_fingerprint(parsed):
    payload = {
        'meta': parsed.get('meta') or {},
        'messages': parsed.get('messages') or [],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def source_fingerprint(path):
    """Cheap change detector; parse content only after this value changes."""
    return transcript_version(path)


def transcript_artifact_id(session_id, source_path):
    slug = safe_filename(session_id, default='session', max_length=64)
    source_hash = hashlib.sha256(str(source_path).encode('utf-8')).hexdigest()[:8]
    return f"{slug}-{source_hash}"


def write_redacted_transcript(parsed, path):
    """Write normalized roles/text only; omit reasoning, tools, and raw records."""
    with open(path, 'w', encoding='utf-8') as handle:
        for message in parsed.get('messages', []):
            record = {
                'role': message.get('role', ''),
                'text': redact_sensitive(message.get('text', '')),
            }
            json.dump(record, handle, ensure_ascii=False)
            handle.write('\n')

def sync_to_nutstore_atomic(vault_path, backup_path):
    """Copy key vault files to Nutstore backup directory atomically.
    Uses .tmp directory approach to avoid sync gaps."""
    import shutil
    vault_real = os.path.realpath(os.path.abspath(vault_path))
    backup_real = os.path.realpath(os.path.abspath(backup_path))
    if _paths_overlap(vault_real, backup_real):
        raise ValueError("backup_path must not equal, contain, or be inside vault_path")
    key_dirs = [
        '00-Rules',
        '00-Inbox',
        '01-Projects',
        '02-Areas',
        '03-Maps',
        '04-Feedback',
        '05-Agent-Memory',
    ]
    tmp_backup = backup_path + '.tmp'
    previous_backup = backup_path + '.previous'

    # Remove stale tmp if exists
    if os.path.exists(tmp_backup):
        shutil.rmtree(tmp_backup)
    if os.path.exists(previous_backup):
        shutil.rmtree(previous_backup)

    os.makedirs(tmp_backup, exist_ok=True)

    for d in key_dirs:
        src = os.path.join(vault_path, d)
        dst = os.path.join(tmp_backup, d)
        if os.path.exists(src) and not os.path.islink(src):
            shutil.copytree(src, dst, ignore=backup_ignore)

    # Keep the previous complete copy until the new directory is in place.
    if os.path.exists(backup_path):
        os.rename(backup_path, previous_backup)
    try:
        os.rename(tmp_backup, backup_path)
    except Exception:
        if os.path.exists(previous_backup) and not os.path.exists(backup_path):
            os.rename(previous_backup, backup_path)
        if os.path.exists(tmp_backup):
            shutil.rmtree(tmp_backup)
        raise
    if os.path.exists(previous_backup):
        shutil.rmtree(previous_backup)


def backup_ignore(directory, names):
    """Exclude links and volatile/private scanner artifacts from cloud backup."""
    volatile = {'_raw-sessions', '_logs', '_rollback', '_cleanup-backups', '.git'}
    return [
        name
        for name in names
        if name in volatile or os.path.islink(os.path.join(directory, name))
    ]

def sync_to_nutstore(vault_path, backup_path):
    """DEPRECATED: use sync_to_nutstore_atomic instead."""
    sync_to_nutstore_atomic(vault_path, backup_path)


def _paths_overlap(left, right):
    try:
        common = os.path.commonpath([left, right])
    except ValueError:
        return False
    return common in {left, right}
