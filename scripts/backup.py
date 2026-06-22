"""Step 1: Backup new JSONL sessions to vault."""
import os
import shutil
from transcript_utils import get_transcript_roots, iter_transcript_files, parse_transcript, session_id_from_path

def run(cfg, dry_run=False, full=False):
    vault = cfg['vault_path']
    raw_dir = os.path.join(vault, '04-Feedback', '_raw-sessions')
    heartbeat_path = os.path.join(vault, '04-Feedback', 'heartbeat.md')

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
            continue

        file_size = os.path.getsize(fp)

        if full or session_id not in processed or processed[session_id] != file_size:
            new_sessions.append((fp, session_id, file_size))

    processed_count = 0
    for fp, session_id, file_size in new_sessions:
        if not dry_run:
            # Atomic copy: copy to .tmp then rename
            dst = os.path.join(raw_dir, f"{session_id}.jsonl")
            os.makedirs(raw_dir, exist_ok=True)
            tmp_dst = dst + '.tmp'
            shutil.copy2(fp, tmp_dst)
            os.replace(tmp_dst, dst)
            # Generate Markdown metadata summary (atomic)
            md_path = os.path.join(raw_dir, f"{session_id}.md")
            tmp_md = md_path + '.tmp'
            generate_md_summary_to_path(fp, tmp_md)
            os.replace(tmp_md, md_path)
        processed[session_id] = file_size
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
    parts = content.split('---', 2)
    if len(parts) < 3:
        return {}
    import yaml
    fm = yaml.safe_load(parts[1]) or {}
    if not isinstance(fm, dict):
        return {}
    return fm.get('processed_sessions', {})

def generate_md_summary_to_path(jsonl_path, md_path):
    """Generate a lightweight Markdown summary from JSONL metadata.
    Writes to the given path (caller handles atomicity)."""
    parsed = parse_transcript(jsonl_path)
    meta = parsed.get('meta', {})
    messages = parsed.get('messages', [])

    first_ts = meta.get('timestamp')
    title = meta.get('thread_name') or meta.get('session_id') or 'Untitled'
    user_msgs = [
        m['text'][:200]
        for m in messages
        if m.get('role') in ('user', 'event', 'user_message')
    ][:5]

    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(f"---\n")
        f.write(f"date: {first_ts[:10] if first_ts else 'unknown'}\n")
        f.write(f"title: \"{title or 'Untitled'}\"\n")
        f.write(f"messages_sampled: {len(user_msgs)}\n")
        f.write(f"---\n\n")
        f.write(f"# {title or 'Untitled Session'}\n\n")
        f.write(f"Date: {first_ts[:10] if first_ts else 'unknown'}\n\n")
        f.write(f"## User Messages (first 200 chars each)\n\n")
        for i, msg in enumerate(user_msgs[:5]):
            f.write(f"{i+1}. {msg}\n")

def generate_md_summary(jsonl_path, md_path):
    """DEPRECATED: use generate_md_summary_to_path instead."""
    generate_md_summary_to_path(jsonl_path, md_path)

def sync_to_nutstore_atomic(vault_path, backup_path):
    """Copy key vault files to Nutstore backup directory atomically.
    Uses .tmp directory approach to avoid sync gaps."""
    import shutil
    key_dirs = ['00-Rules', '01-Projects', '03-Maps', '04-Feedback']
    tmp_backup = backup_path + '.tmp'

    # Remove stale tmp if exists
    if os.path.exists(tmp_backup):
        shutil.rmtree(tmp_backup)

    os.makedirs(tmp_backup, exist_ok=True)

    for d in key_dirs:
        src = os.path.join(vault_path, d)
        dst = os.path.join(tmp_backup, d)
        if os.path.exists(src):
            shutil.copytree(src, dst)

    # Atomic swap: remove old, rename new
    if os.path.exists(backup_path):
        shutil.rmtree(backup_path)
    os.rename(tmp_backup, backup_path)

def sync_to_nutstore(vault_path, backup_path):
    """DEPRECATED: use sync_to_nutstore_atomic instead."""
    sync_to_nutstore_atomic(vault_path, backup_path)
