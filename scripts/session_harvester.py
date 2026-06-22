#!/usr/bin/env python3
"""
Session Harvester — called by Codex or Claude Code hooks.
- Stop hook (--mode stop): harvest the just-ended transcript → trigger incremental scan
- SessionStart hook (--mode start): harvest unprocessed transcripts → Agent Memory updated before AI loads

Design principles:
- Never loses data: all writes are atomic (.tmp → rename)
- Never crashes the hook: every step has try/except
- Works without proxy: no network calls in harvest phase
- Works without transcript path: falls back to scanning agent memory
- Idempotent: running twice on the same transcript doesn't duplicate
"""
import os
import sys
import re
import json
import yaml
import subprocess
import hashlib
from urllib.parse import unquote, urlparse
from datetime import datetime, timezone, timedelta
from config import load_config
from transcript_utils import (
    find_latest_transcript,
    find_recent_transcripts as find_recent_transcripts_from_config,
    parse_transcript,
)

# ── Configuration ──────────────────────────────────────────────
SCANNER_DIR = os.path.dirname(os.path.abspath(__file__))

# Local timezone (China Standard Time)
CST = timezone(timedelta(hours=8))
UUID_SESSION_NAME = re.compile(
    r"\d{4}-\d{2}-\d{2}-"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)


# ── Main ───────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Session Harvester")
    parser.add_argument("--mode", choices=["stop", "start", "index"], default="stop",
                       help="stop: harvest current transcript (Stop hook). "
                            "start: scan for unprocessed transcripts (SessionStart hook). "
                            "index: rebuild Obsidian memory index only.")
    parser.add_argument("--agent", choices=["codex", "claude"],
                       help="override configured agent runtime for transcript discovery")
    args = parser.parse_args()
    cfg = load_config()
    if args.agent:
        cfg["agent"] = args.agent

    if args.mode == "index":
        ensure_obsidian_ignore_filters(cfg)
        rebuild_memory_index(cfg)
        return 0
    if args.mode == "start":
        return start_mode(cfg)
    else:
        return stop_mode(cfg)


def stop_mode(cfg):
    """Stop hook: harvest the just-ended transcript, then trigger incremental scanner."""
    transcript_path = find_transcript(cfg)
    if not transcript_path:
        print("[harvester] No transcript found — nothing to harvest")
        return 0

    result = process_transcript(cfg, transcript_path)
    if result:
        run_scanner_incremental(cfg)
    return 0


def start_mode(cfg):
    """SessionStart hook: find unprocessed transcripts and harvest them.
    Fast — no scanner trigger. The daily cron handles deep analysis."""
    # Load heartbeat to find already-processed transcripts
    processed = load_processed_from_heartbeat(cfg)

    # Find all transcripts in agent memory modified in last 48 hours
    candidates = find_recent_transcripts_from_config(cfg, processed, hours=48)

    if not candidates:
        print("[harvester:start] No unprocessed transcripts found")
        return 0

    print(f"[harvester:start] Found {len(candidates)} unprocessed transcript(s)")
    harvested = 0
    for tp in candidates:
        if process_transcript(cfg, tp):
            harvested += 1

    print(f"[harvester:start] Harvested {harvested}/{len(candidates)} transcripts")
    if harvested and cfg.get("scan_on_start", True):
        run_scanner_incremental(cfg)
    return 0


def process_transcript(cfg, transcript_path):
    """Harvest a single transcript: extract knowledge, write to vault.
    Returns True if anything was written."""
    print(f"[harvester] Processing: {transcript_path}")

    parsed = parse_transcript(transcript_path)
    content = parsed["text"]
    harvest_text = "\n".join(
        m["text"] for m in parsed.get("messages", [])
        if m.get("role") == "assistant"
    )
    decisions = dedupe_items(extract_decisions(harvest_text), ("text", "context"))
    errors = dedupe_items(extract_errors(harvest_text), ("type", "resolution"))
    summary = extract_session_summary(harvest_text)
    decisions, errors, summary = sanitize_harvested_content(
        cfg, decisions, errors, summary
    )
    meta = extract_meta(content)
    meta.update({k: v for k, v in parsed.get("meta", {}).items() if v})

    total_found = len(decisions) + len(errors) + (1 if summary else 0)
    if total_found == 0:
        print("[harvester] No [DECISION]/[ERROR]/[SESSION_SUMMARY] found")
        return False

    print(f"[harvester] Found: {len(decisions)} decisions, {len(errors)} errors, "
          f"{'1 summary' if summary else 'no summary'}")

    ensure_obsidian_ignore_filters(cfg)
    project = detect_project(cfg, content, meta)
    session_id = generate_session_id(transcript_path, meta)
    date_str = meta.get("date", datetime.now(CST).strftime("%Y-%m-%d"))

    written = write_session_to_vault(cfg, session_id, date_str, project, meta,
                                     decisions, errors, summary)

    if decisions:
        append_decisions(cfg, project, decisions, session_id, date_str)
    if errors:
        append_errors_to_pitfalls(cfg, project, errors, session_id, date_str)

    rebuild_memory_index(cfg)

    print(f"[harvester] Done: project={project}, session={session_id}")
    return written > 0


# ── SessionStart Helpers ────────────────────────────────────────

def load_processed_from_heartbeat(cfg):
    """Load set of already-processed transcript IDs from heartbeat."""
    hb_path = os.path.join(cfg['vault_path'], "04-Feedback", "heartbeat.md")
    if not os.path.exists(hb_path):
        return set()
    try:
        with open(hb_path, 'r', encoding='utf-8') as f:
            content = f.read()
        parts = content.split('---', 2)
        if len(parts) < 3:
            return set()
        fm = yaml.safe_load(parts[1]) or {}
        if not isinstance(fm, dict):
            return set()
        processed = fm.get('processed_sessions', {})
        return set(processed.keys())
    except Exception:
        return set()


# ── Transcript Discovery ───────────────────────────────────────
def find_transcript(cfg):
    """Find the transcript file. Try hook env vars first, then scan agent memory."""
    # Try all known env var names for the transcript path
    for varname in ["CODEX_TRANSCRIPT_PATH", "CODEX_SESSION_FILE",
                    "CLAUDE_TRANSCRIPT_PATH", "TRANSCRIPT_PATH",
                    "CLAUDE_SESSION_TRANSCRIPT", "CLAUDE_TRANSCRIPT"]:
        path = os.environ.get(varname)
        if path and os.path.exists(path):
            print(f"[harvester] Found transcript via ${varname}: {path}")
            return path

    latest = find_latest_transcript(cfg, hours=24)
    if latest:
        print(f"[harvester] Fallback: using most recent transcript: {latest}")
    return latest


# ── Content Extraction ─────────────────────────────────────────
def read_transcript(path):
    """Read JSONL transcript, returning raw text of all assistant + user messages."""
    return parse_transcript(path)["text"]


def extract_decisions(text):
    """Extract all [DECISION: ...] blocks from text.

    Supported forms:
    - [DECISION:summary| context:why]
    - [DECISION:summary| context:why| project:slug| scope:project]
    """
    decisions = []
    for raw in re.findall(r"\[DECISION:\s*(.*?)\]", text, re.DOTALL):
        summary, fields = parse_annotation_fields(raw)
        context = fields.get("context", "")
        if not summary and not context:
            continue
        item = {
            "text": normalize_annotation_text(summary),
            "context": normalize_annotation_text(context),
        }
        project = normalize_project_slug(fields.get("project", ""))
        if project:
            item["project"] = project
        scope = normalize_annotation_text(fields.get("scope", ""))
        if scope:
            item["scope"] = scope
        decisions.append(item)
    return decisions


def extract_errors(text):
    """Extract all [ERROR: ...] blocks from text.

    Supported forms:
    - [ERROR:type=path-filesystem| resolution=how fixed]
    - [ERROR:type:path-filesystem| resolution:how fixed| project:slug]
    """
    errors = []
    for raw in re.findall(r"\[ERROR:\s*(.*?)\]", text, re.DOTALL):
        leading, fields = parse_annotation_fields(raw)
        if leading:
            key, value = split_annotation_field(leading)
            if key:
                fields.setdefault(key, value)
        err_type = fields.get("type", "")
        resolution = fields.get("resolution", "")
        if not err_type and not resolution:
            continue
        item = {
            "type": normalize_annotation_text(err_type),
            "resolution": normalize_annotation_text(resolution),
        }
        project = normalize_project_slug(fields.get("project", ""))
        if project:
            item["project"] = project
        errors.append(item)
    return errors


def parse_annotation_fields(raw):
    """Split a pipe-delimited annotation into leading text and key/value fields."""
    parts = [p.strip() for p in str(raw or "").split("|")]
    leading = parts[0] if parts else ""
    fields = {}
    for part in parts[1:]:
        key, value = split_annotation_field(part)
        if key:
            fields[key] = value
    return leading, fields


def split_annotation_field(part):
    """Parse 'key:value' or 'key=value' fields used by annotation tags."""
    match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*[:=]\s*(.*?)\s*$", str(part or ""), re.DOTALL)
    if not match:
        return None, ""
    return match.group(1).strip().lower(), match.group(2).strip()


def normalize_annotation_text(value):
    """Keep annotation content single-line for YAML/frontmatter stability."""
    return re.sub(r"\s+", " ", str(value or "").strip())


def normalize_project_slug(value):
    """Return a safe project slug from optional annotation metadata."""
    value = normalize_annotation_text(value).strip("'\"")
    if re.match(r"^[A-Za-z0-9_.-]+$", value):
        return value
    return ""


def extract_session_summary(text):
    """Extract [SESSION_SUMMARY] block if present."""
    pattern = r"^\[SESSION_SUMMARY\]\s*(.*?)^\[/SESSION_SUMMARY\]"
    matches = re.findall(pattern, text, re.DOTALL | re.MULTILINE)
    if matches:
        return matches[-1].strip()
    return None


def extract_meta(text):
    """Extract basic metadata from transcript content."""
    meta = {}
    # Try to find a date in the first few lines
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", text[:500])
    if date_match:
        meta["date"] = date_match.group(1)
    else:
        meta["date"] = datetime.now(CST).strftime("%Y-%m-%d")

    return meta


# ── Project Detection ──────────────────────────────────────────
def detect_project(cfg, text, meta):
    """Determine which project this session belongs to."""
    summary_project = project_from_session_summary(text)
    if summary_project:
        return summary_project

    annotation_project = project_from_annotations(text)
    if annotation_project:
        return annotation_project

    # Use project_hints from meta if available
    hints = meta.get("project_hints", {})
    project_keywords = build_project_keywords(cfg)

    for proj, keywords in project_keywords.items():
        count = sum(1 for kw in keywords if kw and kw.lower() in text.lower())
        if count > 0:
            hints[proj] = hints.get(proj, 0) + count

    # File paths and Codex cwd are strong project signals.
    path_candidates = [meta.get("cwd", "")]
    path_candidates.extend(re.findall(r"(?:/Users|/home|/Volumes|[A-Za-z]:)[^\s\]\)\"']+", text))
    for candidate in path_candidates:
        for proj, keywords in project_keywords.items():
            if any(kw and kw.lower() in candidate.lower() for kw in keywords):
                hints[proj] = hints.get(proj, 0) + 3

    if hints:
        return max(hints, key=hints.get)

    # Default: most recently active project
    projects = cfg.get("projects") or []
    if projects:
        first = projects[0]
        return first.get("name") if isinstance(first, dict) else str(first)
    return "Project-Infra"


def project_from_session_summary(text):
    """Prefer explicit project metadata from a [SESSION_SUMMARY] block."""
    summary = extract_session_summary(text)
    if not summary:
        return None

    primary = re.search(r"^primary:\s*([A-Za-z0-9_.-]+)\s*$", summary, re.MULTILINE)
    if primary:
        return primary.group(1).strip()

    projects = re.search(r"^projects:\s*\[([^\]]+)\]", summary, re.MULTILINE)
    if projects:
        first = projects.group(1).split(",", 1)[0].strip().strip("'\"")
        if re.match(r"^[A-Za-z0-9_.-]+$", first):
            return first

    return None


def project_from_annotations(text):
    """Prefer explicit project fields from decision/error annotations."""
    projects = []
    projects.extend(d.get("project") for d in extract_decisions(text))
    projects.extend(e.get("project") for e in extract_errors(text))
    projects = [p for p in projects if p]
    if not projects:
        return None
    return max(set(projects), key=projects.count)


def build_project_keywords(cfg):
    """Build project keyword map from config.yaml."""
    keyword_map = {}

    for proj in cfg.get("projects", []) or []:
        if isinstance(proj, str):
            name = proj
            keywords = [name, name.replace("-", "_"), name.replace("_", "-")]
        elif isinstance(proj, dict):
            name = proj.get("name")
            keywords = [name, *proj.get("keywords", [])] if name else []
        else:
            continue
        if name:
            keyword_map[name] = list(dict.fromkeys(k for k in keywords if k))

    for name, keywords in (cfg.get("project_keywords") or {}).items():
        keyword_map[name] = list(dict.fromkeys([name, *(keywords or [])]))

    return keyword_map


# ── Session ID Generation ──────────────────────────────────────
def generate_session_id(transcript_path, meta):
    """Generate a stable, unique session ID."""
    if meta.get("session_id"):
        return meta["session_id"]

    # Use transcript filename as base
    basename = os.path.basename(transcript_path)
    session_id = basename.replace(".jsonl", "")

    # If it looks like a UUID already, use it
    if len(session_id) >= 32:
        return session_id

    # Otherwise, hash the path for stability
    date_str = meta.get("date", datetime.now(CST).strftime("%Y-%m-%d"))
    path_hash = hashlib.md5(transcript_path.encode()).hexdigest()[:8]
    return f"{date_str}-{path_hash}"


# ── Vault Writing ──────────────────────────────────────────────
def write_session_to_vault(cfg, session_id, date_str, project, meta,
                           decisions, errors, summary):
    """Write session summary .md to vault. Returns count of files written."""
    decisions, errors, summary = sanitize_harvested_content(
        cfg, decisions, errors, summary
    )
    sessions_dir = os.path.join(cfg['vault_path'], "01-Projects", project, "Memory", "sessions")
    os.makedirs(sessions_dir, exist_ok=True)

    generated_title = generate_title(decisions, errors)
    filepath = find_session_file_by_id(sessions_dir, session_id, date_str)
    if not filepath:
        filepath = os.path.join(sessions_dir, make_session_filename(sessions_dir, date_str, generated_title, session_id))

    # Check if already exists (idempotent)
    if os.path.exists(filepath):
        print(f"[harvester] Session file already exists: {filepath} — appending new items only")
        # Read existing, merge new decisions/errors
        existing = read_existing_session(filepath)
        existing_decisions = existing.get("decisions_made", [])
        existing_errors = existing.get("errors_encountered", [])
        new_decisions = merge_unique(decisions, existing_decisions, ("text", "context"))
        new_errors = merge_unique(errors, existing_errors, ("type", "resolution"))
        if not new_decisions and not new_errors:
            return 0
        decisions = existing_decisions + new_decisions
        errors = existing_errors + new_errors
        generated_title = existing.get("ai_title") or generated_title

    # Build frontmatter
    tags = list(set(
        tag for d in decisions for tag in extract_tags_from_decision(d)
    ))
    tags.extend([e["type"].split("_")[0] for e in errors])  # category as tag

    fm = {
        "session_id": session_id,
        "date": date_str,
        "project": project,
        "projects": [project],
        "ai_title": generated_title,
        "summary_status": "draft",
        "summary_type": "session",
        "decisions_made": decisions,
        "errors_encountered": errors,
        "tags": list(set(tags)),
        "harvested_by": "session_harvester.py",
        "harvested_at": datetime.now(CST).isoformat(),
    }

    # Build body
    body_parts = [f"# {fm['ai_title']}\n"]
    body_parts.append(f"Session: {session_id} | Date: {date_str} | Project: {project}\n")

    if decisions:
        body_parts.append("\n## Decisions\n")
        for i, d in enumerate(decisions, 1):
            body_parts.append(f"{i}. **{d['text']}**\n")
            body_parts.append(f"   - Context: {d['context']}\n")

    if errors:
        body_parts.append("\n## Errors Encountered\n")
        for i, e in enumerate(errors, 1):
            body_parts.append(f"{i}. `{e['type']}`\n")
            body_parts.append(f"   - Resolution: {e['resolution']}\n")

    if summary:
        body_parts.append("\n## Session Summary\n")
        body_parts.append(summary + "\n")

    body = "\n".join(body_parts)

    # Atomic write
    fm_yaml = yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
    content = f"---\n{fm_yaml}---\n\n{body}"

    tmp_path = filepath + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp_path, filepath)

    print(f"[harvester] Wrote session: {filepath}")
    return 1


def find_session_file_by_id(sessions_dir, session_id, date_str):
    """Find an existing renamed session file by frontmatter session_id and date."""
    if not os.path.isdir(sessions_dir):
        return None
    matches = []
    for filename in os.listdir(sessions_dir):
        if not filename.endswith(".md") or filename.startswith("_"):
            continue
        path = os.path.join(sessions_dir, filename)
        fm = read_existing_session(path)
        if fm.get("session_id") == session_id and str(fm.get("date", "")) == str(date_str):
            matches.append(path)
    if not matches:
        return None
    return sorted(matches)[0]


def make_session_filename(sessions_dir, date_str, title, session_id):
    """Create a readable, collision-resistant session filename."""
    slug = filename_slug(title) or session_id[:12]
    filename = f"{date_str}-{slug}.md"
    filepath = os.path.join(sessions_dir, filename)
    if not os.path.exists(filepath):
        return filename
    return f"{date_str}-{slug}-{session_id[:8]}.md"


def filename_slug(title, max_length=64):
    """Sanitize a title for macOS/Obsidian Markdown filenames."""
    text = re.sub(r"\s+", " ", str(title or "")).strip()
    text = re.sub(r'[\\/:*?"<>|#^\[\]]+', "", text)
    text = text.strip(" .")
    if len(text) <= max_length:
        return text
    return text[:max_length].rstrip(" .")


def read_existing_session(filepath):
    """Read existing session .md and return its frontmatter."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        parts = content.split("---", 2)
        if len(parts) >= 3:
            return yaml.safe_load(parts[1]) or {}
    except Exception:
        pass
    return {}


def merge_unique(new_items, existing_items, fields):
    """Return only new items whose composite key is absent from existing items."""
    existing_keys = {
        tuple(item.get(field, "") for field in fields)
        for item in existing_items
    }
    truly_new = [
        item for item in new_items
        if tuple(item.get(field, "") for field in fields) not in existing_keys
    ]
    return truly_new


def dedupe_items(items, fields):
    """Deduplicate extracted records while preserving order."""
    seen = set()
    unique = []
    for item in items:
        key = tuple(item.get(field, "") for field in fields)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def generate_title(decisions, errors):
    """Generate a compact human-readable content summary."""
    for decision in decisions:
        text = clean_title_text(decision.get("text", ""))
        if text:
            if len(decisions) > 1:
                return f"{text}等 {len(decisions)} 项决策"
            return text

    for error in errors:
        text = clean_title_text(error.get("resolution", "") or error.get("type", ""))
        if text:
            if len(errors) > 1:
                return f"{text}等 {len(errors)} 个问题"
            return text

    return "会话记忆"


def clean_title_text(text, max_length=36):
    """Drop placeholder extraction noise and keep titles readable."""
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    if not text or text in {"...", "内容"}:
        return ""
    if text.startswith("...]") or "[SESSION_SUMMARY]" in text:
        return ""
    if len(text) <= max_length:
        return text
    return text[:max_length - 1].rstrip() + "…"


def extract_tags_from_decision(decision):
    """Extract relevant tags from a decision text."""
    tags = []
    text = decision.get("text", "") + " " + decision.get("context", "")
    text_lower = text.lower()
    # Map common keywords to tags
    tag_map = {
        "r 4.5": "R-bug", "ggplot": "R-bug", "identity": "identity-fill",
        "python": "python-encoding", "encoding": "encoding", "gbk": "encoding",
        "zotero": "zotero", "citation": "citation",
        "gfw": "gfw", "proxy": "gfw", "ssl": "ssl",
        "ci": "infra", "test": "infra", "hook": "infra",
        "cbioportal": "cBioPortal", "gdc": "GDC", "api": "API",
        "figure": "figure", "color": "figure", "plot": "figure",
        "docx": "DOCX", "word": "DOCX",
        "patent": "patent",
        "module": "module",
    }
    for kw, tag in tag_map.items():
        if kw in text_lower:
            tags.append(tag)
    return tags


# ── Obsidian-safe Markdown Sanitization ─────────────────────────
def sanitize_harvested_content(cfg, decisions, errors, summary):
    """Normalize harvested text before writing it into Obsidian Markdown.

    Agent replies often contain local Markdown links such as
    [file.md](/Users/name/Vault/file.md). Obsidian treats some malformed
    absolute paths as vault-relative links and can create empty Users/... files
    when they are opened. Store vault-internal paths as wiki links and downgrade
    machine-local paths to plain code text.
    """
    decisions = [
        {
            **d,
            "text": sanitize_obsidian_markdown(d.get("text", ""), cfg),
            "context": sanitize_obsidian_markdown(d.get("context", ""), cfg),
        }
        for d in decisions
    ]
    errors = [
        {
            **e,
            "type": sanitize_error_type(e.get("type", "")),
            "resolution": sanitize_obsidian_markdown(e.get("resolution", ""), cfg),
        }
        for e in errors
    ]
    if summary:
        summary = sanitize_obsidian_markdown(summary, cfg)
    return decisions, errors, summary


def sanitize_error_type(value):
    """Keep error taxonomy values compact and non-Markdown."""
    return re.sub(r"\s+", "_", str(value or "").strip())


def sanitize_obsidian_markdown(text, cfg):
    """Return Markdown that is safe to store inside an Obsidian vault."""
    if text is None:
        return ""
    text = str(text)
    vault = os.path.abspath(cfg.get("vault_path") or "")
    if not vault:
        return text

    def replace_markdown_link(match):
        label = match.group(1).strip()
        target = match.group(2).strip()
        resolved = resolve_vault_link_target(target, vault)
        if resolved:
            safe_label = label
            if UUID_SESSION_NAME.search(label):
                safe_label = os.path.basename(resolved)
            return obsidian_link(resolved, safe_label or os.path.basename(resolved))
        if looks_like_local_path(target):
            return f"{label or os.path.basename(target)} (`{target}`)"
        return match.group(0)

    def replace_wiki_link(match):
        target = match.group(1).strip()
        label = (match.group(2) or "").strip()
        resolved = resolve_vault_link_target(target, vault) or target
        safe_label = label
        if UUID_SESSION_NAME.search(label) or UUID_SESSION_NAME.search(target):
            safe_label = os.path.basename(resolved)
        return obsidian_link(resolved, safe_label or os.path.basename(resolved))

    text = re.sub(
        r"\[([^\]\n]+)\]\(([^)\n]+)\)",
        replace_markdown_link,
        text,
    )
    text = re.sub(
        r"\[\[([^|\]\n]+)(?:\|([^\]\n]*))?\]\]",
        replace_wiki_link,
        text,
    )

    def replace_bare_path(match):
        raw = match.group(0).rstrip(".,;:")
        suffix = match.group(0)[len(raw):]
        resolved = resolve_vault_link_target(raw, vault)
        if resolved:
            label = os.path.basename(resolved) or resolved
            if label.endswith(".md"):
                label = label[:-3]
            return obsidian_link(resolved, label) + suffix
        if looks_like_local_path(raw):
            return f"`{raw}`{suffix}"
        return match.group(0)

    # Clean paths that came from copied CLI output or prior assistant links.
    text = re.sub(
        r"(?<![`(\[])(?:/Users/[^\s)\]]+|Users/[^\s)\]]+|[A-Za-z]:[\\/][^\s)\]]+)",
        replace_bare_path,
        text,
    )
    return text


def resolve_vault_link_target(target, vault):
    """Resolve a Markdown link target to a vault-relative path, if possible."""
    normalized = normalize_local_link_target(target)
    if not normalized:
        return None

    candidates = []
    if normalized.startswith(vault + os.sep) or normalized == vault:
        candidates.append(normalized)
    elif normalized.startswith(vault.lstrip(os.sep) + os.sep):
        candidates.append(os.sep + normalized)
    elif normalized.startswith("Users" + os.sep):
        candidates.append(os.sep + normalized)

    for candidate in candidates:
        rel = relpath_in_vault(candidate, vault)
        if not rel:
            continue
        resolved = resolve_existing_or_renamed_session(rel, vault)
        if resolved:
            return resolved
    return None


def normalize_local_link_target(target):
    """Strip URL wrappers/fragments and normalize separators."""
    target = str(target or "").strip().strip("<>")
    if not target:
        return ""
    parsed = urlparse(target)
    if parsed.scheme == "file":
        target = unquote(parsed.path)
    elif parsed.scheme:
        return ""
    else:
        target = target.split("#", 1)[0]
        target = target.split("?", 1)[0]
        target = unquote(target)
    return target.replace("\\", os.sep).strip()


def relpath_in_vault(path, vault):
    """Return a vault-relative path if path is inside vault."""
    path = os.path.abspath(path)
    try:
        common = os.path.commonpath([vault, path])
    except ValueError:
        return None
    if common != vault:
        return None
    rel = os.path.relpath(path, vault).replace(os.sep, "/")
    return None if rel.startswith("..") else rel


def resolve_existing_or_renamed_session(rel, vault):
    """Return a valid vault-relative path, following renamed session files."""
    abs_path = os.path.join(vault, rel)
    if os.path.exists(abs_path):
        return rel[:-3] if rel.endswith(".md") else rel
    if not rel.endswith(".md") and os.path.exists(abs_path + ".md"):
        return rel

    match = re.search(
        r"^(01-Projects/[^/]+/Memory/sessions)/"
        r"(\d{4}-\d{2}-\d{2})-"
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.md$",
        rel,
    )
    if not match:
        return None

    sessions_dir = os.path.join(vault, match.group(1))
    existing = find_session_file_by_id(sessions_dir, match.group(3), match.group(2))
    if not existing:
        return None
    resolved_rel = os.path.relpath(existing, vault).replace(os.sep, "/")
    return resolved_rel[:-3] if resolved_rel.endswith(".md") else resolved_rel


def looks_like_local_path(value):
    """Detect local machine paths that should not become Obsidian links."""
    value = str(value or "")
    return bool(
        value.startswith("/Users/")
        or value.startswith("Users/")
        or re.match(r"^[A-Za-z]:[\\/]", value)
    )


# ── Append to Project Files ────────────────────────────────────
def append_decisions(cfg, project, decisions, session_id, date_str):
    """Append decisions to project's decisions.md."""
    dec_path = os.path.join(cfg['vault_path'], "01-Projects", project, "Memory", "decisions.md")
    os.makedirs(os.path.dirname(dec_path), exist_ok=True)

    # Read existing decisions to check for duplicates
    existing_texts = set()
    if os.path.exists(dec_path):
        try:
            with open(dec_path, "r", encoding="utf-8") as f:
                content = f.read()
            # Extract existing decision texts from frontmatter
            parts = content.split("---", 2)
            if len(parts) >= 3:
                fm = yaml.safe_load(parts[1])
                for d in fm.get("decisions", []):
                    existing_texts.add(d.get("text", ""))
            existing_body = parts[2] if len(parts) > 2 else ""
        except Exception:
            existing_body = ""
    else:
        existing_body = ""

    # Filter out duplicates
    new_decisions = [d for d in decisions if d["text"] not in existing_texts]
    if not new_decisions:
        return

    # Append to body
    new_lines = []
    for d in new_decisions:
        new_lines.append(f"- [{date_str}] **{d['text']}** | context: {d['context']} | session: {session_id}")

    updated_body = existing_body.rstrip() + "\n" + "\n".join(new_lines) + "\n"

    # Rewrite file with updated frontmatter
    _rewrite_project_md(dec_path, "decisions", new_decisions, updated_body, session_id)


def append_errors_to_pitfalls(cfg, project, errors, session_id, date_str):
    """Append errors to project's pitfalls.md."""
    pit_path = os.path.join(cfg['vault_path'], "01-Projects", project, "Memory", "pitfalls.md")
    os.makedirs(os.path.dirname(pit_path), exist_ok=True)

    # Read existing errors
    existing_types = set()
    if os.path.exists(pit_path):
        try:
            with open(pit_path, "r", encoding="utf-8") as f:
                content = f.read()
            parts = content.split("---", 2)
            if len(parts) >= 3:
                fm = yaml.safe_load(parts[1])
                for p in fm.get("pitfalls", []):
                    existing_types.add((p.get("type", ""), p.get("resolution", "")))
            existing_body = parts[2] if len(parts) > 2 else ""
        except Exception:
            existing_body = ""
    else:
        existing_body = ""

    new_errors = [e for e in errors if (e["type"], e["resolution"]) not in existing_types]
    if not new_errors:
        return

    new_lines = []
    for e in new_errors:
        new_lines.append(f"- [{date_str}] **{e['type']}** → {e['resolution']} | session: {session_id}")

    updated_body = existing_body.rstrip() + "\n" + "\n".join(new_lines) + "\n"

    _rewrite_project_md(pit_path, "pitfalls", new_errors, updated_body, session_id)


def _rewrite_project_md(filepath, key, new_items, body, session_id):
    """Rewrite a project .md file with updated frontmatter list."""
    try:
        # Build frontmatter
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                old_content = f.read()
            parts = old_content.split("---", 2)
            old_fm = yaml.safe_load(parts[1]) if len(parts) >= 3 and parts[1].strip() else {}
        else:
            old_fm = {}

        existing = old_fm.get(key, [])
        existing.extend(new_items)
        old_fm[key] = existing
        old_fm["last_updated"] = datetime.now(CST).isoformat()

        fm_yaml = yaml.dump(old_fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
        content = f"---\n{fm_yaml}---\n\n{body}"

        tmp = filepath + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, filepath)
    except Exception as e:
        print(f"[harvester] WARNING: Could not update {filepath}: {e}")


def ensure_obsidian_ignore_filters(cfg):
    """Hide machine-generated/internal folders that should not appear as notes."""
    vault = cfg.get("vault_path")
    if not vault:
        return
    cleanup_bad_obsidian_path_artifacts(cfg)
    repair_obsidian_workspace(cfg)
    obsidian_dir = os.path.join(vault, ".obsidian")
    app_json = os.path.join(obsidian_dir, "app.json")
    filters = ["04-Feedback/_raw-sessions/", "04-Feedback/_rollback/", "Users/"]
    try:
        os.makedirs(obsidian_dir, exist_ok=True)
        data = {}
        if os.path.exists(app_json):
            with open(app_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                data = {}
        existing = data.get("userIgnoreFilters")
        if not isinstance(existing, list):
            existing = []
        changed = False
        for item in filters:
            if item not in existing:
                existing.append(item)
                changed = True
        data["userIgnoreFilters"] = existing
        if changed or not os.path.exists(app_json):
            tmp = app_json + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, app_json)
    except Exception as e:
        print(f"[harvester] WARNING: Could not update Obsidian ignore filters: {e}")


def cleanup_bad_obsidian_path_artifacts(cfg):
    """Remove empty notes created when Obsidian opens malformed local paths."""
    vault = cfg.get("vault_path")
    if not vault:
        return
    bad_root = os.path.join(vault, "Users")
    if not os.path.isdir(bad_root):
        return
    try:
        for root, dirs, files in os.walk(bad_root, topdown=False):
            for filename in files:
                path = os.path.join(root, filename)
                try:
                    if filename.endswith(".md") and os.path.getsize(path) == 0:
                        os.remove(path)
                except OSError:
                    pass
            try:
                if not os.listdir(root):
                    os.rmdir(root)
            except OSError:
                pass
    except Exception as e:
        print(f"[harvester] WARNING: Could not clean bad Obsidian path artifacts: {e}")


def repair_obsidian_workspace(cfg):
    """Remove bad local path references from Obsidian workspace state."""
    vault = cfg.get("vault_path")
    if not vault:
        return
    workspace = os.path.join(vault, ".obsidian", "workspace.json")
    if not os.path.exists(workspace):
        return
    index_file = "00-Inbox/Agent Memory Index.md"
    index_title = "Agent Memory Index"

    def bad_workspace_value(value):
        if not isinstance(value, str):
            return False
        normalized = normalize_local_link_target(value)
        return (
            value.startswith("Users/")
            or value.startswith("sessions/")
            or value.startswith(vault + os.sep)
            or normalized.startswith("Users" + os.sep)
            or normalized.startswith(vault + os.sep)
        )

    def clean(value, key=""):
        if isinstance(value, list):
            return [clean(item) for item in value if not bad_workspace_value(item)]
        if isinstance(value, dict):
            for item_key, item_value in list(value.items()):
                value[item_key] = clean(item_value, item_key)
            return value
        if bad_workspace_value(value):
            return index_title if key == "title" else index_file
        return value

    try:
        with open(workspace, "r", encoding="utf-8") as f:
            old = f.read()
        data = json.loads(old)
        data = clean(data)
        if isinstance(data.get("lastOpenFiles"), list):
            seen = set()
            data["lastOpenFiles"] = [index_file, *data["lastOpenFiles"]]
            data["lastOpenFiles"] = [
                item for item in data["lastOpenFiles"]
                if not (isinstance(item, str) and bad_workspace_value(item))
            ]
            data["lastOpenFiles"] = [
                item for item in data["lastOpenFiles"]
                if not isinstance(item, str)
                or not (item in seen or seen.add(item))
            ]
        new = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        if new != old:
            tmp = workspace + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(new)
            os.replace(tmp, workspace)
            print("[harvester] Repaired unsafe local paths in Obsidian workspace")
    except Exception as e:
        print(f"[harvester] WARNING: Could not repair Obsidian workspace: {e}")


def repair_generated_vault_markdown(cfg):
    """Sanitize generated memory files that may contain older unsafe links."""
    vault = cfg.get("vault_path")
    if not vault:
        return 0
    roots = [
        os.path.join(vault, "00-Inbox"),
        os.path.join(vault, "01-Projects"),
    ]
    changed = 0
    for root in roots:
        if not os.path.isdir(root):
            continue
        for current, dirs, files in os.walk(root):
            dirs[:] = [
                d for d in dirs
                if d not in {"_raw-sessions", "_rollback", "_logs", ".git"}
            ]
            for filename in files:
                if not filename.endswith(".md"):
                    continue
                path = os.path.join(current, filename)
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        old = f.read()
                    new = sanitize_obsidian_markdown(old, cfg)
                    if new != old:
                        tmp = path + ".tmp"
                        with open(tmp, "w", encoding="utf-8") as f:
                            f.write(new)
                        os.replace(tmp, path)
                        changed += 1
                except Exception as e:
                    print(f"[harvester] WARNING: Could not sanitize {path}: {e}")
    if changed:
        print(f"[harvester] Sanitized unsafe local links in {changed} generated markdown file(s)")
    return changed


# ── Global Memory Index ────────────────────────────────────────
def rebuild_memory_index(cfg):
    """Rebuild a visible Obsidian index of harvested sessions, decisions, and errors."""
    ensure_obsidian_ignore_filters(cfg)
    repair_generated_vault_markdown(cfg)
    vault = cfg["vault_path"]
    index_path = cfg.get("memory_index_path") or os.path.join(
        vault, "00-Inbox", "Agent Memory Index.md"
    )
    sessions = collect_harvested_sessions(vault)

    sessions.sort(
        key=lambda item: (
            str(item.get("date") or ""),
            str(item.get("harvested_at") or ""),
            str(item.get("session_id") or ""),
        ),
        reverse=True,
    )

    decision_count = sum(len(item.get("decisions_made", [])) for item in sessions)
    error_count = sum(len(item.get("errors_encountered", [])) for item in sessions)
    updated_at = datetime.now(CST).isoformat()

    fm = {
        "title": "Agent Memory Index",
        "generated_by": "session_harvester.py",
        "updated_at": updated_at,
        "session_count": len(sessions),
        "decision_count": decision_count,
        "error_count": error_count,
    }

    body = []
    body.append("# Agent Memory Index\n")
    body.append(f"Updated: {updated_at}\n")
    body.append("This file is rebuilt automatically after session harvesting.\n")
    body.append("## Recent Sessions\n")
    body.append("| Date | Project | Session | Decisions | Errors |")
    body.append("|---|---|---|---:|---:|")
    for item in sessions[:30]:
        body.append(
            "| {date} | {project} | {link} | {decisions} | {errors} |".format(
                date=item.get("date", ""),
                project=item.get("project", ""),
                link=obsidian_link(item["rel_path"], item.get("ai_title") or item["filename"]),
                decisions=len(item.get("decisions_made", [])),
                errors=len(item.get("errors_encountered", [])),
            )
        )

    body.append("\n## Recent Decisions\n")
    body.append("| Date | Project | Decision | Session |")
    body.append("|---|---|---|---|")
    decision_rows = []
    for item in sessions:
        for decision in item.get("decisions_made", []):
            decision_rows.append(
                (
                    item.get("date", ""),
                    item.get("project", ""),
                    truncate_cell(decision.get("text", ""), 140),
                    obsidian_link(item["rel_path"], item.get("ai_title") or item["filename"]),
                )
            )
    for date, project, text, link in decision_rows[:50]:
        body.append(f"| {date} | {project} | {escape_table_cell(text)} | {link} |")

    body.append("\n## Recent Errors\n")
    body.append("| Date | Project | Type | Resolution | Session |")
    body.append("|---|---|---|---|---|")
    error_rows = []
    for item in sessions:
        for error in item.get("errors_encountered", []):
            error_rows.append(
                (
                    item.get("date", ""),
                    item.get("project", ""),
                    error.get("type", ""),
                    truncate_cell(error.get("resolution", ""), 180),
                    obsidian_link(item["rel_path"], item.get("ai_title") or item["filename"]),
                )
            )
    for date, project, error_type, resolution, link in error_rows[:50]:
        body.append(
            f"| {date} | {project} | `{escape_table_cell(error_type)}` | "
            f"{escape_table_cell(resolution)} | {link} |"
        )

    content = "---\n"
    content += yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
    content += "---\n\n"
    content += "\n".join(body).rstrip() + "\n"

    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    tmp = index_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, index_path)
    print(f"[harvester] Rebuilt memory index: {index_path}")


def collect_harvested_sessions(vault):
    """Read harvested session frontmatter from all project memory folders."""
    projects_dir = os.path.join(vault, "01-Projects")
    if not os.path.isdir(projects_dir):
        return []

    sessions = []
    for project in sorted(os.listdir(projects_dir)):
        sessions_dir = os.path.join(projects_dir, project, "Memory", "sessions")
        if not os.path.isdir(sessions_dir):
            continue
        for filename in sorted(os.listdir(sessions_dir)):
            if not filename.endswith(".md") or filename.startswith("_"):
                continue
            path = os.path.join(sessions_dir, filename)
            fm = read_existing_session(path)
            if not fm:
                continue
            rel_path = os.path.relpath(path, vault).replace(os.sep, "/")
            fm.setdefault("project", project)
            fm["filename"] = filename
            fm["rel_path"] = rel_path[:-3] if rel_path.endswith(".md") else rel_path
            sessions.append(fm)
    return sessions


def obsidian_link(path_without_ext, label):
    """Build an Obsidian wiki link using a vault-relative path."""
    safe_label = truncate_cell(label or path_without_ext, 80)
    safe_label = safe_label.replace("|", "/").strip()
    return f"[[{path_without_ext}|{safe_label}]]"


def escape_table_cell(value):
    """Escape Markdown table delimiters and collapse whitespace."""
    text = str(value or "").replace("\n", " ")
    return text.replace("|", "\\|").strip()


def truncate_cell(value, max_length):
    """Keep generated index tables compact enough for Obsidian reading mode."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


# ── Scanner Trigger ────────────────────────────────────────────
def run_scanner_incremental(cfg):
    """Run scanner in incremental mode (analyze + maintain + report + compile)."""
    runner = os.path.join(SCANNER_DIR, "runner.py")
    if not os.path.exists(runner):
        print("[harvester] WARNING: runner.py not found, skipping incremental scan")
        return

    # Check proxy only when configured. Analyzer can still run offline.
    proxy_up = check_proxy(cfg)
    if proxy_up is False:
        print("[harvester] Proxy DOWN — running scanner in keyword-only mode (no LLM clustering)")
        # Still run — analyzer falls back when API key is empty or API fails.

    python = cfg.get("python_path") or sys.executable
    cmd = [python, runner]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                                cwd=SCANNER_DIR, env=os.environ.copy())
        if result.returncode != 0:
            print(f"[harvester] Scanner completed with warnings:\n{result.stderr[:500]}")
        else:
            print(f"[harvester] Incremental scanner completed successfully")
        # Print summary line
        for line in result.stdout.strip().split("\n")[-3:]:
            print(f"  {line}")
    except subprocess.TimeoutExpired:
        print("[harvester] WARNING: Scanner timed out after 120s")
    except Exception as e:
        print(f"[harvester] WARNING: Scanner failed: {e}")


def check_proxy(cfg):
    """Check if proxy is available."""
    proxy = cfg.get("proxy") or {}
    host = proxy.get("host")
    port = proxy.get("port")
    if not host or not port:
        return None

    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((host, int(port)))
        s.close()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
