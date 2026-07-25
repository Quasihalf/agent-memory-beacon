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
import fcntl
from dataclasses import dataclass
from time import monotonic
import knowledge_index as knowledge_index_module
from annotation_quality import (
    annotation_candidate_roots,
    partition_annotations,
    process_annotation_candidates,
)
from urllib.parse import unquote, urlparse
from datetime import datetime, timezone, timedelta
from config import load_config
from transcript_utils import (
    find_latest_transcript,
    find_recent_transcripts as find_recent_transcripts_from_config,
    get_transcript_roots,
    iter_transcript_files,
    make_zcode_locator,
    parse_transcript,
    parse_transcript_since,
    read_transcript_metadata,
    session_id_from_path,
    transcript_state_key,
    split_zcode_locator,
    transcript_cursor,
    transcript_snapshot,
    transcript_version,
)
from memory_judge import (
    candidate_revision as personal_candidate_revision,
    process_personal_memory,
)
from memory_effectiveness import write_effectiveness_report
from memory_promotion import refresh_promotion_proposals
from memory_schema import (
    RUNTIME_SCHEMA_VERSION,
    canonical_project,
    expected_formal_section_revision,
    memory_revision,
    merge_formal_records,
    normalize_formal_record,
)
from skill_preference_learner import (
    candidate_revision as skill_candidate_revision,
    process_skill_preferences,
)
from workflow_memory import (
    candidate_revision as workflow_candidate_revision,
    process_workflow_memory,
)
from insight_memory import load_formal_records as load_formal_insight_records
from insight_memory import process_insight_memory
from error_evidence import (
    ErrorEvidenceStateError,
    clear_error_evidence_dirty,
    error_evidence_dirty_token,
    load_candidate_records as load_error_evidence_candidates,
    process_error_evidence,
)
from knowledge_index import rebuild_vault_knowledge_indexes
from safety import (
    OBSIDIAN_IGNORE_FILTERS,
    VAULT_INTERNAL_DIR_NAMES,
    durable_atomic_write,
    durable_rmdir,
    durable_unlink,
    ensure_directory_tree,
    exclusive_file_lock,
    normalize_iso_date,
    normalize_project_slug as safe_project_slug,
    redact_sensitive,
    safe_filename,
    safe_vault_path,
    secure_list_directory,
    secure_open_file,
    secure_read_bytes,
    secure_walk,
    split_frontmatter_text,
    strip_markdown_code_blocks,
)

# ── Configuration ──────────────────────────────────────────────
SCANNER_DIR = os.path.dirname(os.path.abspath(__file__))

# Local timezone (China Standard Time)
CST = timezone(timedelta(hours=8))
UUID_SESSION_NAME = re.compile(
    r"\d{4}-\d{2}-\d{2}-"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)
_HARVEST_LOCK_DESCRIPTORS = {}
_CURSOR_EXPECTATION_UNSET = object()
MAX_HEARTBEAT_BYTES = 16 * 1024 * 1024
MAX_MANAGED_REPAIR_FILE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class TranscriptHarvestOutcome:
    transcript_path: str
    version: str
    cursor: str
    expected_cursor: object
    changed: bool
    needs_index_rebuild: bool
    project: str
    session_id: str


def _check_ownership(ownership_check):
    if ownership_check is not None:
        ownership_check()


def _cooperative_atomic_write(
    path,
    content,
    ownership_check,
    mutation_io=None,
    root=None,
):
    _check_ownership(ownership_check)
    if mutation_io is not None:
        mutation_io.atomic_write(path, content, encoding="utf-8")
    else:
        if root is None:
            raise ValueError("managed atomic write requires a pinned root")
        _check_ownership(ownership_check)
        durable_atomic_write(path, content, root=root)
    _check_ownership(ownership_check)


def _cooperative_ensure_directory(
    path,
    ownership_check,
    mutation_io=None,
    root=None,
):
    _check_ownership(ownership_check)
    if mutation_io is not None:
        mutation_io.ensure_directory(path)
    else:
        if root is None:
            raise ValueError("managed directory creation requires a pinned root")
        ensure_directory_tree(path, root)
    _check_ownership(ownership_check)


def _cooperative_remove_file(
    path,
    ownership_check,
    mutation_io=None,
    root=None,
):
    _check_ownership(ownership_check)
    if mutation_io is not None:
        mutation_io.remove_file(path)
    else:
        if root is None:
            raise ValueError("managed file removal requires a pinned root")
        durable_unlink(path, root=root)
    _check_ownership(ownership_check)


def _cooperative_remove_directory(
    path,
    ownership_check,
    mutation_io=None,
    root=None,
):
    _check_ownership(ownership_check)
    if mutation_io is not None:
        mutation_io.remove_directory(path)
    else:
        if root is None:
            raise ValueError("managed directory removal requires a pinned root")
        durable_rmdir(path, root=root)
    _check_ownership(ownership_check)


def _cooperative_read_text(
    path,
    ownership_check,
    mutation_io=None,
    root=None,
    limit=MAX_MANAGED_REPAIR_FILE_BYTES,
):
    _check_ownership(ownership_check)
    if root is None:
        raise ValueError("managed read requires a pinned root")
    data = secure_read_bytes(path, limit, root=root)
    if len(data) > limit:
        raise OSError(f"managed repair file exceeds {limit} bytes")
    content = data.decode("utf-8")
    _check_ownership(ownership_check)
    return content


def _cooperative_list_directory(
    path,
    ownership_check,
    mutation_io=None,
    root=None,
):
    _check_ownership(ownership_check)
    if root is None:
        raise ValueError("managed directory listing requires a pinned root")
    result = secure_list_directory(path, root)
    _check_ownership(ownership_check)
    return result


def _cooperative_walk(
    path,
    ownership_check,
    mutation_io=None,
    root=None,
    topdown=True,
    excluded_directory_names=(),
):
    _check_ownership(ownership_check)
    if root is None:
        raise ValueError("managed traversal requires a pinned root")
    result = secure_walk(
        path,
        root,
        topdown=topdown,
        excluded_directory_names=excluded_directory_names,
    )
    _check_ownership(ownership_check)
    return result


def _cooperative_file_is_empty(
    path,
    ownership_check,
    mutation_io=None,
    root=None,
):
    _check_ownership(ownership_check)
    if root is None:
        raise ValueError("managed file inspection requires a pinned root")
    empty = secure_read_bytes(path, 0, root=root) == b""
    _check_ownership(ownership_check)
    return empty


def _rebuild_vault_knowledge_indexes_cooperative(
    cfg,
    ownership_check,
    mutation_io=None,
):
    if ownership_check is None:
        return rebuild_vault_knowledge_indexes(cfg)
    vault = cfg.get("vault_path")
    if not vault or not os.path.isdir(vault):
        return {"keyword_terms": 0, "global_atoms": 0, "written": []}
    output_dir = os.path.join(vault, knowledge_index_module.DEFAULT_OUTPUT_DIR)
    _cooperative_ensure_directory(
        output_dir,
        ownership_check,
        mutation_io,
        root=vault,
    )

    notes = knowledge_index_module.collect_indexable_notes(
        vault,
        excluded_roots=(
            *knowledge_index_module.error_evidence_candidate_roots(cfg),
            *annotation_candidate_roots(cfg),
        ),
        additional_note_types=(
            knowledge_index_module.configured_adaptive_formal_paths(cfg)
        ),
    )
    keyword_index = knowledge_index_module.build_keyword_index(notes)
    atoms = knowledge_index_module.build_global_atoms(vault)
    recall_index = knowledge_index_module.build_recall_index(notes)
    memory_graph = knowledge_index_module.build_memory_graph(notes, recall_index)
    recall_path = knowledge_index_module.configured_recall_index_path(cfg)
    _cooperative_ensure_directory(
        os.path.dirname(recall_path),
        ownership_check,
        mutation_io,
        root=vault,
    )
    outputs = (
        (
            os.path.join(output_dir, "keyword-index.json"),
            json.dumps(keyword_index, ensure_ascii=False, indent=2) + "\n",
        ),
        (
            os.path.join(output_dir, "keyword-index.md"),
            knowledge_index_module.render_keyword_index_markdown(keyword_index),
        ),
        (
            os.path.join(output_dir, "global-atoms.json"),
            json.dumps(atoms, ensure_ascii=False, indent=2) + "\n",
        ),
        (
            os.path.join(output_dir, "global-atoms.md"),
            knowledge_index_module.render_global_atoms_markdown(atoms),
        ),
        (
            recall_path,
            json.dumps(recall_index, ensure_ascii=False, indent=2) + "\n",
        ),
        (
            os.path.join(output_dir, "memory-graph.json"),
            json.dumps(memory_graph, ensure_ascii=False, indent=2) + "\n",
        ),
        (
            os.path.join(output_dir, "recall-context.md"),
            knowledge_index_module.render_recall_context_markdown(
                recall_index,
                memory_graph,
            ),
        ),
    )
    written = []
    for path, content in outputs:
        _cooperative_atomic_write(
            path,
            content,
            ownership_check,
            mutation_io,
            root=vault,
        )
        written.append(path)
    return {
        "keyword_terms": len(keyword_index.get("keywords", {})),
        "global_atoms": len(atoms.get("atoms", [])),
        "recall_units": len(recall_index.get("units", [])),
        "graph_nodes": len(memory_graph.get("nodes", [])),
        "graph_edges": len(memory_graph.get("edges", [])),
        "written": written,
    }


# ── Main ───────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(description="Session Harvester")
    parser.add_argument("--mode", choices=["stop", "start", "index"], default="stop",
                       help="stop: harvest current transcript (Stop hook). "
                            "start: scan for unprocessed transcripts (SessionStart hook). "
                            "index: rebuild Obsidian memory index only.")
    parser.add_argument("--agent", choices=["codex", "claude", "zcode"],
                       help="override configured agent runtime for transcript discovery")
    parser.add_argument(
        "--skip-scanner",
        action="store_true",
        help="harvest transcripts without running the deeper scanner pipeline",
    )
    parser.add_argument(
        "--skip-profile-check",
        action="store_true",
        help="skip Codex profile drift checks for non-interactive background runs",
    )
    args = parser.parse_args()
    hook_input = read_hook_input()
    cfg = load_config()
    if args.skip_scanner:
        cfg["scan_on_start"] = False
    if args.skip_profile_check:
        cfg["codex_profile_check_on_start"] = False
    if args.agent:
        cfg["agent"] = args.agent
        cfg["transcript_agents"] = [args.agent]
    lock_path = safe_vault_path(
        cfg["vault_path"], "04-Feedback", "_logs", "harvester.lock"
    )
    if not acquire_harvest_lock(lock_path, root=cfg["vault_path"]):
        print("[harvester] Another harvest is running; this hook will be retried on SessionStart")
        return 0
    try:
        if args.mode == "index":
            ensure_obsidian_ignore_filters(cfg)
            rebuild_memory_index(cfg)
            return 0
        if args.mode == "start":
            return start_mode(cfg)
        return stop_mode(
            cfg,
            hook_input=hook_input,
            run_scanner=not args.skip_scanner,
        )
    finally:
        release_harvest_lock(lock_path)


def stop_mode(cfg, hook_input=None, run_scanner=True):
    """Stop hook: harvest the just-ended transcript, then trigger incremental scanner."""
    transcript_path = find_transcript(cfg, hook_input=hook_input)
    if not transcript_path:
        print("[harvester] No transcript found — nothing to harvest")
        return 0

    try:
        result = process_transcript(cfg, transcript_path)
    except Exception as exc:
        print(f"[harvester] WARNING: transcript harvest failed: {exc}")
        return 0
    if result and run_scanner:
        run_scanner_incremental(cfg)
    return 0


def start_mode(cfg):
    """SessionStart hook: find unprocessed transcripts and harvest them.
    Fast — no scanner trigger. The weekly launchd job handles deep analysis."""
    check_codex_profile_on_start(cfg)

    try:
        baseline_count = initialize_harvest_baseline(cfg)
    except ValueError as exc:
        print(f"[harvester:start] WARNING: harvest baseline not initialized: {exc}")
        return 0
    if baseline_count:
        print(
            "[harvester:start] Initialized existing transcript baseline: "
            f"{baseline_count}; no historical content was harvested"
        )
        return 0

    # Load heartbeat to find already-processed transcripts
    processed = load_processed_from_heartbeat(cfg)

    # Find all transcripts in agent memory modified in last 48 hours
    candidates = find_recent_transcripts_from_config(cfg, processed, hours=48)

    if not candidates:
        print("[harvester:start] No unprocessed transcripts found")
        return 0

    total_candidates = len(candidates)
    try:
        max_transcripts = int(cfg.get("harvest_start_max_transcripts", 32))
    except (TypeError, ValueError):
        max_transcripts = 32
    max_transcripts = max(1, max_transcripts)
    candidates = candidates[:max_transcripts]
    deferred = total_candidates - len(candidates)
    try:
        time_budget = float(cfg.get("harvest_start_time_budget_seconds", 180))
    except (TypeError, ValueError):
        time_budget = 180.0
    time_budget = max(1.0, time_budget)
    batch_started = monotonic()
    deadline = batch_started + time_budget

    print(f"[harvester:start] Found {total_candidates} unprocessed transcript(s)")
    if deferred:
        print(
            f"[harvester:start] Deferring {deferred} transcript(s) to a later batch "
            f"(limit={max_transcripts})"
        )
    harvested = 0
    committed = 0
    pending = []
    attempted = 0
    for tp in candidates:
        if attempted and monotonic() >= deadline:
            time_deferred = len(candidates) - attempted
            deferred += time_deferred
            print(
                f"[harvester:start] Time budget reached; deferring "
                f"{time_deferred} transcript(s)"
            )
            break
        attempted += 1
        try:
            outcome = prepare_transcript_harvest(cfg, tp)
            if outcome.needs_index_rebuild:
                pending.append(outcome)
            else:
                commit_transcript_harvest(cfg, outcome)
        except Exception as exc:
            print(f"[harvester:start] WARNING: could not process {tp}: {exc}")

    index_seconds = 0.0
    if pending:
        index_started = monotonic()
        try:
            rebuild_memory_index(cfg)
        except Exception as exc:
            print(f"[harvester:start] WARNING: could not rebuild memory index: {exc}")
        else:
            for outcome in pending:
                try:
                    commit_transcript_harvest(cfg, outcome)
                except Exception as exc:
                    print(
                        "[harvester:start] WARNING: could not commit transcript "
                        f"{outcome.transcript_path}: {exc}"
                    )
                    continue
                committed += 1
                if outcome.changed:
                    harvested += 1
                print(
                    f"[harvester] Done: project={outcome.project}, "
                    f"session={outcome.session_id}"
                )
            if committed:
                _refresh_effectiveness_report(cfg)
        finally:
            index_seconds = monotonic() - index_started

    total_seconds = monotonic() - batch_started
    processing_seconds = max(0.0, total_seconds - index_seconds)
    print(
        f"[harvester:start] Harvested {harvested}/{attempted} transcripts"
        + (f"; deferred={deferred}" if deferred else "")
    )
    print(
        "[harvester:start] Timing: "
        f"processing={processing_seconds:.2f}s, "
        f"index={index_seconds:.2f}s, total={total_seconds:.2f}s"
    )
    if harvested and cfg.get("scan_on_start", True):
        run_scanner_incremental(cfg)
    return 0


def acquire_harvest_lock(lock_path, root=None):
    """Hold an OS-backed nonblocking writer lock until explicit release."""
    lock_path = os.path.abspath(os.path.expanduser(str(lock_path)))
    if root is not None:
        ensure_directory_tree(os.path.dirname(lock_path), root)
    else:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    if lock_path in _HARVEST_LOCK_DESCRIPTORS:
        return False
    try:
        descriptor = secure_open_file(
            lock_path,
            os.O_RDWR | os.O_CREAT,
            0o600,
            root=root,
        )
    except OSError:
        return False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        os.close(descriptor)
        return False
    payload = json.dumps(
        {
            "pid": os.getpid(),
            "acquired_at": datetime.now(CST).isoformat(),
        },
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    try:
        os.ftruncate(descriptor, 0)
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, payload)
        os.fsync(descriptor)
    except OSError:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)
        return False
    _HARVEST_LOCK_DESCRIPTORS[lock_path] = (descriptor, root)
    return True


def release_harvest_lock(lock_path):
    lock_path = os.path.abspath(os.path.expanduser(str(lock_path)))
    lock_state = _HARVEST_LOCK_DESCRIPTORS.pop(lock_path, None)
    if lock_state is None:
        return
    descriptor, root = lock_state
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def process_transcript(cfg, transcript_path):
    """Harvest one transcript and expose its writes through the memory index."""
    outcome = prepare_transcript_harvest(cfg, transcript_path)
    if outcome.needs_index_rebuild:
        rebuild_memory_index(cfg)
    commit_transcript_harvest(cfg, outcome)
    if outcome.needs_index_rebuild:
        _refresh_effectiveness_report(cfg)
        print(
            f"[harvester] Done: project={outcome.project}, "
            f"session={outcome.session_id}"
        )
    return outcome.changed


def _refresh_effectiveness_report(cfg):
    try:
        write_effectiveness_report(cfg["vault_path"], cfg)
    except Exception as exc:
        print(
            "[harvester] WARNING: could not refresh effectiveness report: "
            f"{exc}"
        )
    try:
        refresh_promotion_proposals(cfg["vault_path"], cfg, apply=True)
    except Exception as exc:
        print(
            "[harvester] WARNING: could not refresh promotion proposals: "
            f"{exc}"
        )


def prepare_transcript_harvest(cfg, transcript_path):
    """Write one transcript without rebuilding the index or advancing its cursor."""
    print(f"[harvester] Processing: {transcript_path}")

    snapshot_version, snapshot_cursor = transcript_snapshot(transcript_path)
    tracking = load_harvest_tracking(cfg)
    path_session_id = transcript_state_key(transcript_path)
    legacy_path_session_id = session_id_from_path(transcript_path)
    adaptive_cursors = tracking.get("adaptive_cursors", {})
    prior_cursor = adaptive_cursors.get(path_session_id)
    if prior_cursor is None and legacy_path_session_id != path_session_id:
        prior_cursor = adaptive_cursors.get(legacy_path_session_id)
    expected_cursor = prior_cursor
    if (
        prior_cursor is None
        and tracking.get("harvest_baseline_initialized_at")
        and (
            path_session_id in tracking.get("harvested_sessions", {})
            or legacy_path_session_id in tracking.get("harvested_sessions", {})
        )
    ):
        prior_cursor = snapshot_cursor

    if prior_cursor is None:
        parsed = parse_transcript(transcript_path)
        adaptive_parsed = parsed
    else:
        adaptive_parsed = parse_transcript_since(
            transcript_path,
            prior_cursor,
            end_cursor=snapshot_cursor,
        )
        parsed = {
            **adaptive_parsed,
            "meta": read_transcript_metadata(transcript_path),
        }
    content = parsed["text"]
    harvest_text = "\n".join(
        message["text"]
        for message in adaptive_parsed.get("messages", [])
        if message.get("role") == "assistant"
    )
    decisions = dedupe_items(
        extract_decisions(harvest_text),
        ("text", "context", "project", "scope"),
    )
    errors = dedupe_items(
        extract_errors(harvest_text),
        ("type", "resolution", "project"),
    )
    summary = extract_session_summary(harvest_text)
    decisions, errors, summary = sanitize_harvested_content(
        cfg, decisions, errors, summary
    )
    annotation_partition = partition_annotations(decisions, errors)
    decisions = annotation_partition["decisions"]
    errors = annotation_partition["errors"]
    adaptive_errors = list(errors)
    meta = extract_meta(content)
    meta.update({k: v for k, v in parsed.get("meta", {}).items() if v})
    project = detect_project(cfg, content, meta, annotation_text=harvest_text)
    session_id = generate_session_id(transcript_path, meta)
    date_str = normalize_iso_date(
        meta.get("date"), datetime.now(CST).strftime("%Y-%m-%d")
    )
    project, date_str = reuse_existing_session_route(
        cfg, session_id, project, date_str
    )
    if meta.get("is_subagent"):
        annotation_quality_result = empty_annotation_quality_result()
        memory_result = empty_adaptive_result()
        skill_result = empty_adaptive_result()
        workflow_result = empty_adaptive_result()
        insight_result = empty_insight_result()
        error_evidence_result = empty_error_evidence_result()
    else:
        annotation_quality_result = process_annotation_candidates(
            cfg,
            annotation_partition["candidates"],
            project,
            session_id,
            date_str,
        )
        memory_result = process_personal_memory(
            cfg, adaptive_parsed, project, session_id, date_str
        )
        skill_result = process_skill_preferences(
            cfg, adaptive_parsed, project, session_id, date_str
        )
        workflow_result = process_workflow_memory(
            cfg, adaptive_parsed, project, session_id, date_str
        )
        if meta.get("agent") == "codex":
            insight_result = process_insight_memory(
                cfg, adaptive_parsed, project, session_id, date_str
            )
            error_evidence_result = process_error_evidence(
                cfg,
                adaptive_parsed,
                adaptive_errors,
                project,
                session_id,
                date_str,
            )
        else:
            insight_result = empty_insight_result()
            error_evidence_result = empty_error_evidence_result()

    error_evidence_changed = sum(
        int(error_evidence_result.get(key, 0) or 0)
        for key in ("candidates", "updated", "resolved")
    )
    error_evidence_index_dirty = bool(error_evidence_dirty_token(cfg))

    total_found = (
        len(decisions)
        + len(errors)
        + (1 if summary else 0)
        + annotation_quality_result.get("candidates", 0)
        + annotation_quality_result.get("updated", 0)
        + memory_result.get("candidates", 0)
        + memory_result.get("promoted", 0)
        + memory_result.get("formal", 0)
        + memory_result.get("updated", 0)
        + skill_result.get("candidates", 0)
        + skill_result.get("promoted", 0)
        + skill_result.get("formal", 0)
        + skill_result.get("updated", 0)
        + workflow_result.get("candidates", 0)
        + workflow_result.get("promoted", 0)
        + workflow_result.get("formal", 0)
        + workflow_result.get("updated", 0)
        + insight_result.get("candidates", 0)
        + insight_result.get("seeds", 0)
        + insight_result.get("reinforced", 0)
        + insight_result.get("updated", 0)
        + insight_result.get("proposals", 0)
        + int(bool(error_evidence_changed or error_evidence_index_dirty))
    )
    if total_found == 0:
        print("[harvester] No formal annotations or adaptive-memory candidates found")
        return TranscriptHarvestOutcome(
            transcript_path=transcript_path,
            version=snapshot_version,
            cursor=snapshot_cursor,
            expected_cursor=expected_cursor,
            changed=False,
            needs_index_rebuild=False,
            project=project,
            session_id=session_id,
        )

    ensure_obsidian_ignore_filters(cfg)
    print(f"[harvester] Found: {len(decisions)} decisions, {len(errors)} errors, "
          f"{'1 summary' if summary else 'no summary'}")
    if annotation_quality_result.get("candidates") or annotation_quality_result.get("updated"):
        print(
            "[annotation-quality] "
            f"{annotation_quality_result.get('candidates', 0)} candidate(s), "
            f"{annotation_quality_result.get('updated', 0)} updated, "
            f"{len(annotation_partition.get('rejected', []))} rejected"
        )
        print_annotation_quality_items(annotation_quality_result)
    if any(memory_result.values()):
        print(
            "[harvester] Personal memory: "
            f"{memory_result.get('candidates', 0)} candidate(s), "
            f"{memory_result.get('formal', 0)} formal write(s), "
            f"{memory_result.get('updated', 0)} update(s)"
        )
        print_personal_memory_items(memory_result)
    if any(skill_result.values()):
        print(
            "[skill-learner] Skill preferences: "
            f"{skill_result.get('candidates', 0)} candidate(s), "
            f"{skill_result.get('promoted', 0)} promoted, "
            f"{skill_result.get('formal', 0)} formal write(s), "
            f"{skill_result.get('updated', 0)} update(s)"
        )
        print_skill_preference_items(skill_result)
    if any(workflow_result.values()):
        print(
            "[workflow-learner] Workflow memory: "
            f"{workflow_result.get('candidates', 0)} candidate(s), "
            f"{workflow_result.get('promoted', 0)} promoted, "
            f"{workflow_result.get('formal', 0)} formal write(s), "
            f"{workflow_result.get('updated', 0)} update(s)"
        )
        print_workflow_memory_items(workflow_result)
    if any(insight_result.get(key, 0) for key in (
        "candidates", "seeds", "reinforced", "updated", "proposals"
    )):
        print(
            "[insight-learner] Insight memory: "
            f"{insight_result.get('candidates', 0)} candidate(s), "
            f"{insight_result.get('seeds', 0)} seed(s), "
            f"{insight_result.get('reinforced', 0)} reinforced, "
            f"{insight_result.get('updated', 0)} update(s), "
            f"{insight_result.get('proposals', 0)} proposal(s)"
        )
        print_insight_memory_items(insight_result)
    if error_evidence_changed:
        print(
            "[error-evidence] "
            f"{error_evidence_result.get('candidates', 0)} new, "
            f"{error_evidence_result.get('updated', 0)} updated, "
            f"{error_evidence_result.get('resolved', 0)} resolved, "
            f"{error_evidence_result.get('ignored', 0)} ignored"
        )
        print_error_evidence_items(error_evidence_result)
    elif error_evidence_index_dirty:
        print("[error-evidence] Repairing candidate index after an interrupted rebuild")

    written = 0
    if decisions or errors or summary:
        written = write_session_to_vault(cfg, session_id, date_str, project, meta,
                                         decisions, errors, summary)

    source_cursor = expected_cursor or "initial"
    if decisions:
        append_decisions(
            cfg,
            project,
            decisions,
            session_id,
            date_str,
            source_cursor=source_cursor,
        )
    if errors:
        append_errors_to_pitfalls(
            cfg,
            project,
            errors,
            session_id,
            date_str,
            source_cursor=source_cursor,
        )

    changed = (
        written > 0
        or any(annotation_quality_result.values())
        or any(memory_result.values())
        or any(skill_result.values())
        or any(workflow_result.values())
        or any(insight_result.get(key, 0) for key in (
            "candidates", "seeds", "reinforced", "updated", "proposals"
        ))
        or bool(error_evidence_changed)
        or error_evidence_index_dirty
    )
    return TranscriptHarvestOutcome(
        transcript_path=transcript_path,
        version=snapshot_version,
        cursor=snapshot_cursor,
        expected_cursor=expected_cursor,
        changed=changed,
        needs_index_rebuild=True,
        project=project,
        session_id=session_id,
    )


def commit_transcript_harvest(cfg, outcome):
    """Advance one transcript cursor after all required index writes succeed."""
    mark_transcript_harvested(
        cfg,
        outcome.transcript_path,
        version=outcome.version,
        adaptive_cursor=outcome.cursor,
        expected_cursor=outcome.expected_cursor,
    )


def empty_adaptive_result():
    return {
        "candidates": 0,
        "promoted": 0,
        "formal": 0,
        "updated": 0,
        "items": [],
    }


def empty_annotation_quality_result():
    return {"candidates": 0, "updated": 0, "items": []}


def empty_insight_result():
    return {
        "candidates": 0,
        "seeds": 0,
        "reinforced": 0,
        "formal": 0,
        "updated": 0,
        "proposals": 0,
        "items": [],
    }


def empty_error_evidence_result():
    return {
        "candidates": 0,
        "updated": 0,
        "resolved": 0,
        "ignored": 0,
        "items": [],
    }


def print_personal_memory_items(memory_result):
    """Print visible personal-memory records after a harvest."""
    labels = {
        "candidate": "CANDIDATE",
        "promoted": "PROMOTED",
        "updated": "UPDATED",
    }
    for item in memory_result.get("items", []):
        label = labels.get(item.get("action"), "MEMORY")
        title = item.get("title") or item.get("content") or "untitled"
        confidence = item.get("confidence", "")
        seen_count = item.get("seen_count", "")
        print(
            f"[harvester]   [{label}] {truncate_cell(title, 120)} "
            f"(confidence={confidence}, seen={seen_count})"
        )
        if item.get("content"):
            print(f"[harvester]       {truncate_cell(item['content'], 180)}")
        if item.get("path"):
            print(f"[harvester]       -> {item['path']}")


def print_annotation_quality_items(quality_result):
    for item in quality_result.get("items", []):
        reasons = ",".join(item.get("quality_reasons") or []) or "uncertain"
        print(
            f"[annotation-quality]   [{item.get('action', 'candidate').upper()}] "
            f"{item.get('annotation_type', '')}: "
            f"{truncate_cell(item.get('title', ''), 120)} "
            f"(score={item.get('quality_score', '')}, "
            f"seen={item.get('seen_count', '')}, reason={reasons})"
        )
        if item.get("path"):
            print(f"[annotation-quality]       -> {item['path']}")


def print_skill_preference_items(skill_result):
    """Print visible adaptive skill-learning records after a harvest."""
    labels = {
        "candidate": "CANDIDATE",
        "promoted": "PROMOTED",
        "updated": "UPDATED",
    }
    for item in skill_result.get("items", []):
        label = labels.get(item.get("action"), "SKILL")
        skill_name = item.get("skill_name") or "unknown"
        title = item.get("title") or skill_name
        confidence = item.get("confidence", "")
        seen_count = item.get("seen_count", "")
        print(
            f"[skill-learner] {label} {skill_name} "
            f"{truncate_cell(title, 120)} "
            f"(confidence={confidence}, seen={seen_count})"
        )
        if item.get("path"):
            print(f"[skill-learner]     -> {item['path']}")


def print_workflow_memory_items(workflow_result):
    """Print visible workflow-memory records after a harvest."""
    labels = {
        "candidate": "CANDIDATE",
        "promoted": "PROMOTED",
        "updated": "UPDATED",
    }
    for item in workflow_result.get("items", []):
        label = labels.get(item.get("action"), "WORKFLOW")
        rule_name = item.get("rule_name") or "unknown"
        title = item.get("title") or rule_name
        confidence = item.get("confidence", "")
        seen_count = item.get("seen_count", "")
        print(
            f"[workflow-learner] {label} {rule_name} "
            f"{truncate_cell(title, 120)} "
            f"(confidence={confidence}, seen={seen_count})"
        )
        if item.get("path"):
            print(f"[workflow-learner]     -> {item['path']}")


def print_insight_memory_items(insight_result):
    """Print Insight identities without exposing source evidence excerpts."""
    for item in insight_result.get("items", []):
        action = str(item.get("action") or "insight").upper()
        title = item.get("title") or item.get("id") or "untitled"
        print(
            f"[insight-learner] {action} {truncate_cell(title, 120)} "
            f"(maturity={item.get('maturity', '')}, "
            f"confidence={item.get('confidence', '')}, "
            f"source_count={item.get('source_count', 0)})"
        )
        if item.get("path"):
            print(f"[insight-learner]     -> {item['path']}")


def print_error_evidence_items(result):
    """Print candidate identities without exposing stored failure excerpts."""
    for item in result.get("items", []):
        action = str(item.get("action") or "candidate").upper()
        evidence_id = str(item.get("evidence_id") or "")
        short_id = evidence_id[-12:] if evidence_id else "unknown"
        severity = str(item.get("severity") or "error")
        print(f"[error-evidence]   [{action}] {severity} {short_id}")
        if item.get("path"):
            print(f"[error-evidence]       -> {item['path']}")


# ── SessionStart Helpers ────────────────────────────────────────

def check_codex_profile_on_start(cfg):
    """Warn when the current Codex account differs from the shared local profile.

    The hook must never mutate account files automatically. It only runs status
    and prints a visible prompt when manual review is useful. This is independent
    from cfg["agent"], which controls transcript discovery rather than Codex's
    local account profile.
    """
    if not cfg.get("codex_profile_check_on_start", True):
        return False

    profile_dir = cfg.get("codex_profile_path")
    if not profile_dir:
        return False

    try:
        from codex_profile_sync import status_profile

        status = status_profile(profile_dir, cfg.get("codex_home") or "~/.codex")
    except Exception as exc:
        print(f"[codex-profile] 检查失败: {exc}")
        return False

    if not status.get("profile_exists", True):
        return False

    issue_fields = [
        ("Missing skills", status.get("missing_skills", [])),
        ("Changed skills", status.get("changed_skills", [])),
        (
            "Shared AGENTS.md",
            ["missing"] if status.get("agents_missing")
            else ["changed"] if status.get("agents_changed")
            else [],
        ),
        ("Missing enabled plugins", status.get("missing_plugins", [])),
        ("Missing plugin cache", status.get("missing_plugin_cache", [])),
    ]
    issues = [(label, values) for label, values in issue_fields if values]
    if not issues:
        return False

    print("[codex-profile] 当前账号和共享 profile 不一致")
    for label, values in issues:
        print(f"[codex-profile] {label}: {', '.join(values)}")

    python_path = cfg.get("python_path") or sys.executable
    overwrite_arg = " --overwrite" if status.get("changed_skills") else ""
    print(
        "[codex-profile] 建议先预览: "
        f'cd "{SCANNER_DIR}" && "{python_path}" '
        f"codex_profile_sync.py apply --include-config{overwrite_arg} --dry-run"
    )
    print(
        "[codex-profile] 确认后再应用: "
        f'cd "{SCANNER_DIR}" && "{python_path}" '
        f"codex_profile_sync.py apply --include-config{overwrite_arg}"
    )
    return True

def load_harvest_tracking(cfg):
    """Load persisted transcript versions and adaptive high-water marks."""
    hb_path = safe_vault_path(
        cfg['vault_path'], "04-Feedback", "heartbeat.md"
    )
    if not os.path.exists(hb_path):
        return {}
    frontmatter, _body = read_heartbeat_document(
        hb_path,
        root=cfg['vault_path'],
    )
    return frontmatter


def read_heartbeat_document(path, root=None):
    """Read and validate heartbeat state without failing open."""
    try:
        content_bytes = secure_read_bytes(
            path,
            MAX_HEARTBEAT_BYTES,
            root=root,
        )
        if len(content_bytes) > MAX_HEARTBEAT_BYTES:
            raise OSError("heartbeat is oversized")
        content = content_bytes.decode('utf-8')
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"heartbeat cannot be read: {path}") from exc

    frontmatter_text, body = split_frontmatter_text(content)
    if frontmatter_text is None:
        raise ValueError(f"malformed heartbeat frontmatter: {path}")
    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid heartbeat YAML: {path}") from exc
    if not isinstance(frontmatter, dict):
        raise ValueError(f"heartbeat frontmatter is not an object: {path}")

    for field in ('harvested_sessions', 'adaptive_cursors', 'processed_sessions'):
        value = frontmatter.get(field)
        if value is not None and not isinstance(value, dict):
            raise ValueError(f"heartbeat {field} is not an object: {path}")
    if (
        frontmatter.get('harvest_baseline_initialized_at')
        and 'harvested_sessions' not in frontmatter
    ):
        raise ValueError(f"heartbeat baseline has no harvested_sessions: {path}")
    if (
        frontmatter.get('adaptive_cursor_initialized_at')
        and 'adaptive_cursors' not in frontmatter
    ):
        raise ValueError(f"heartbeat cursor marker has no adaptive_cursors: {path}")

    for session_id, cursor in (frontmatter.get('adaptive_cursors') or {}).items():
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError(f"heartbeat contains an invalid cursor session ID: {path}")
        if not re.fullmatch(r"(?:file-bytes|zcode-messages):\d+", str(cursor)):
            raise ValueError(f"heartbeat contains an invalid adaptive cursor: {path}")
    for field in ('harvested_sessions', 'processed_sessions'):
        for session_id in (frontmatter.get(field) or {}):
            if not isinstance(session_id, str) or not session_id.strip():
                raise ValueError(f"heartbeat {field} has an invalid session ID: {path}")

    return frontmatter, body.lstrip('\n')


def load_processed_from_heartbeat(cfg):
    """Load transcript versions successfully examined by the harvester."""
    processed = load_harvest_tracking(cfg).get('harvested_sessions', {})
    return processed if isinstance(processed, dict) else {}


def initialize_harvest_baseline(cfg):
    """Mark transcripts present before installation without reading their content."""
    hb_path = safe_vault_path(
        cfg['vault_path'], "04-Feedback", "heartbeat.md"
    )
    lock_path = safe_vault_path(
        cfg['vault_path'], "04-Feedback", "_logs", "heartbeat.lock"
    )
    ensure_directory_tree(os.path.dirname(hb_path), cfg['vault_path'])
    ensure_directory_tree(os.path.dirname(lock_path), cfg['vault_path'])
    with exclusive_file_lock(lock_path, root=cfg['vault_path']):
        return _initialize_harvest_baseline_unlocked(cfg, hb_path)


def _initialize_harvest_baseline_unlocked(cfg, hb_path):
    os.makedirs(os.path.dirname(hb_path), exist_ok=True)
    frontmatter = {}
    body = "# Scanner Heartbeat\n"
    if os.path.exists(hb_path):
        frontmatter, body = read_heartbeat_document(
            hb_path,
            root=cfg['vault_path'],
        )

    baseline_initialized = bool(frontmatter.get('harvest_baseline_initialized_at'))
    cursor_initialized = bool(frontmatter.get('adaptive_cursor_initialized_at'))
    if baseline_initialized and cursor_initialized:
        return 0

    harvested = frontmatter.get('harvested_sessions', {})
    if not isinstance(harvested, dict):
        harvested = {}
    adaptive_cursors = frontmatter.get('adaptive_cursors', {})
    if not isinstance(adaptive_cursors, dict):
        adaptive_cursors = {}
    discovered = 0
    for transcript_path in iter_transcript_files(get_transcript_roots(cfg)):
        try:
            session_id = transcript_state_key(transcript_path)
            version, cursor = transcript_snapshot(transcript_path)
            if not baseline_initialized:
                harvested[session_id] = version
            adaptive_cursors[session_id] = cursor
        except OSError:
            continue
        discovered += 1

    frontmatter['harvested_sessions'] = harvested
    frontmatter['adaptive_cursors'] = adaptive_cursors
    frontmatter.setdefault('processed_sessions', {})
    now = datetime.now(CST).isoformat()
    frontmatter.setdefault('harvest_baseline_initialized_at', now)
    frontmatter['adaptive_cursor_initialized_at'] = now
    write_heartbeat_document(
        hb_path,
        frontmatter,
        body,
        root=cfg['vault_path'],
    )
    return discovered


def write_heartbeat_document(path, frontmatter, body, root=None):
    fm_yaml = yaml.dump(
        frontmatter,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    content = f"---\n{fm_yaml}---\n\n{body.rstrip()}\n"
    durable_atomic_write(path, content, root=root)


def mark_transcript_harvested(
    cfg,
    transcript_path,
    version=None,
    adaptive_cursor=None,
    expected_cursor=_CURSOR_EXPECTATION_UNSET,
):
    """Persist a successful harvest version without touching backup state."""
    hb_path = safe_vault_path(
        cfg['vault_path'], "04-Feedback", "heartbeat.md"
    )
    lock_path = safe_vault_path(
        cfg['vault_path'], "04-Feedback", "_logs", "heartbeat.lock"
    )
    ensure_directory_tree(os.path.dirname(hb_path), cfg['vault_path'])
    ensure_directory_tree(os.path.dirname(lock_path), cfg['vault_path'])
    with exclusive_file_lock(lock_path, root=cfg['vault_path']):
        _mark_transcript_harvested_unlocked(
            hb_path,
            transcript_path,
            version=version,
            adaptive_cursor=adaptive_cursor,
            expected_cursor=expected_cursor,
            vault_root=cfg['vault_path'],
        )


def _mark_transcript_harvested_unlocked(
    hb_path,
    transcript_path,
    version=None,
    adaptive_cursor=None,
    expected_cursor=_CURSOR_EXPECTATION_UNSET,
    vault_root=None,
):
    frontmatter = {}
    body = "# Scanner Heartbeat\n"
    if os.path.exists(hb_path):
        frontmatter, body = read_heartbeat_document(
            hb_path,
            root=vault_root,
        )

    if (version is None) != (adaptive_cursor is None):
        raise ValueError("transcript version and adaptive cursor must be stored together")
    if version is None:
        version, adaptive_cursor = transcript_snapshot(transcript_path)

    harvested = frontmatter.get('harvested_sessions', {})
    if not isinstance(harvested, dict):
        harvested = {}
    session_id = transcript_state_key(transcript_path)
    legacy_session_id = session_id_from_path(transcript_path)
    if legacy_session_id != session_id:
        harvested.pop(legacy_session_id, None)
    harvested[session_id] = version
    frontmatter['harvested_sessions'] = harvested
    adaptive_cursors = frontmatter.get('adaptive_cursors', {})
    if not isinstance(adaptive_cursors, dict):
        adaptive_cursors = {}
    current_cursor = adaptive_cursors.get(session_id)
    if current_cursor is None and legacy_session_id != session_id:
        current_cursor = adaptive_cursors.get(legacy_session_id)
    if expected_cursor is not _CURSOR_EXPECTATION_UNSET:
        if current_cursor == adaptive_cursor:
            return
        if current_cursor != expected_cursor:
            raise RuntimeError("heartbeat cursor changed during harvest")
    _reject_cursor_regression(current_cursor, adaptive_cursor)
    if legacy_session_id != session_id:
        adaptive_cursors.pop(legacy_session_id, None)
    adaptive_cursors[session_id] = adaptive_cursor
    frontmatter['adaptive_cursors'] = adaptive_cursors
    frontmatter.setdefault('adaptive_cursor_initialized_at', datetime.now(CST).isoformat())
    frontmatter.setdefault('processed_sessions', {})

    write_heartbeat_document(hb_path, frontmatter, body, root=vault_root)


def _reject_cursor_regression(current, proposed):
    if current is None:
        return
    current_match = re.fullmatch(r"(file-bytes|zcode-messages):(\d+)", str(current))
    proposed_match = re.fullmatch(r"(file-bytes|zcode-messages):(\d+)", str(proposed))
    if not current_match or not proposed_match:
        raise RuntimeError("heartbeat cursor format is invalid")
    if current_match.group(1) != proposed_match.group(1):
        raise RuntimeError("heartbeat cursor source changed")
    if int(proposed_match.group(2)) < int(current_match.group(2)):
        raise RuntimeError("heartbeat cursor would regress")


def read_hook_input(stream=None):
    """Parse the JSON payload command hooks provide on stdin."""
    stream = stream or sys.stdin
    try:
        if stream is sys.stdin and stream.isatty():
            return {}
        raw = stream.read()
    except (AttributeError, OSError):
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        print("[harvester] WARNING: ignored malformed hook stdin JSON")
        return {}
    return payload if isinstance(payload, dict) else {}


# ── Transcript Discovery ───────────────────────────────────────
def find_transcript(cfg, hook_input=None):
    """Find the transcript file. Try hook env vars first, then scan agent memory."""
    hook_input = hook_input or {}
    hook_path = (
        hook_input.get("transcript_path")
        or hook_input.get("transcriptPath")
        or hook_input.get("session_file")
    )
    if hook_path:
        hook_path = os.path.expandvars(os.path.expanduser(str(hook_path)))
        db_path, _session_id = split_zcode_locator(hook_path)
        if (db_path and os.path.exists(db_path)) or os.path.exists(hook_path):
            print(f"[harvester] Found transcript via hook stdin: {hook_path}")
            return hook_path

    hook_db = hook_input.get("zcode_session_db") or hook_input.get("zcode_db_path")
    hook_session = hook_input.get("zcode_session_id") or hook_input.get("session_id")
    if hook_db and hook_session:
        hook_db = os.path.expandvars(os.path.expanduser(str(hook_db)))
        if os.path.exists(hook_db):
            path = make_zcode_locator(hook_db, str(hook_session))
            print(f"[harvester] Found zcode transcript via hook stdin: {path}")
            return path

    zcode_db = os.environ.get("ZCODE_SESSION_DB") or os.environ.get("ZCODE_DB_PATH")
    zcode_session = os.environ.get("ZCODE_SESSION_ID")
    if zcode_db and zcode_session and os.path.exists(zcode_db):
        path = make_zcode_locator(zcode_db, zcode_session)
        print(f"[harvester] Found zcode transcript via $ZCODE_SESSION_DB/$ZCODE_SESSION_ID: {path}")
        return path

    # Try all known env var names for the transcript path
    for varname in ["CODEX_TRANSCRIPT_PATH", "CODEX_SESSION_FILE",
                    "CLAUDE_TRANSCRIPT_PATH", "TRANSCRIPT_PATH",
                    "CLAUDE_SESSION_TRANSCRIPT", "CLAUDE_TRANSCRIPT",
                    "ZCODE_TRANSCRIPT_PATH"]:
        path = os.environ.get(varname)
        db_path, _session_id = split_zcode_locator(path)
        if db_path and os.path.exists(db_path):
            print(f"[harvester] Found transcript via ${varname}: {path}")
            return path
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
    text = strip_markdown_code_blocks(text)
    for raw in re.findall(
        r"^\s*\[DECISION:\s*(.*?)\]\s*$",
        str(text or ""),
        re.MULTILINE | re.IGNORECASE,
    ):
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
    text = strip_markdown_code_blocks(text)
    for raw in re.findall(
        r"^\s*\[ERROR:\s*(.*?)\]\s*$",
        str(text or ""),
        re.MULTILINE | re.IGNORECASE,
    ):
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
    return safe_project_slug(value)


def extract_session_summary(text):
    """Extract [SESSION_SUMMARY] block if present."""
    text = strip_markdown_code_blocks(text)
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
def canonicalize_explicit_project(cfg, value):
    """Map a safe explicit project alias only when its route is unambiguous."""
    explicit = safe_project_slug(value)
    if not explicit:
        return None

    project_keywords = build_project_keywords(cfg)
    if explicit in project_keywords:
        return explicit

    explicit_key = explicit.casefold()
    matches = {
        project
        for project, aliases in project_keywords.items()
        if any(
            safe_project_slug(alias).casefold() == explicit_key
            for alias in aliases
            if safe_project_slug(alias)
        )
    }
    return next(iter(matches)) if len(matches) == 1 else explicit


def detect_project(cfg, text, meta, annotation_text=None):
    """Determine which project this session belongs to."""
    trusted_annotations = text if annotation_text is None else annotation_text
    summary_project = canonicalize_explicit_project(
        cfg,
        project_from_session_summary(trusted_annotations),
    )
    if summary_project:
        return summary_project

    annotation_project = canonicalize_explicit_project(
        cfg,
        project_from_annotations(trusted_annotations),
    )
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
        return safe_project_slug(max(hints, key=hints.get)) or "Project-Infra"

    # Default: most recently active project
    projects = cfg.get("projects") or []
    if projects:
        first = projects[0]
        value = first.get("name") if isinstance(first, dict) else str(first)
        return safe_project_slug(value) or "Project-Infra"
    return "Project-Infra"


def project_from_session_summary(text):
    """Prefer explicit project metadata from a [SESSION_SUMMARY] block."""
    summary = extract_session_summary(text)
    if not summary:
        return None

    primary = re.search(r"^primary:\s*([A-Za-z0-9_.-]+)\s*$", summary, re.MULTILINE)
    if primary:
        return safe_project_slug(primary.group(1)) or None

    projects = re.search(r"^projects:\s*\[([^\]]+)\]", summary, re.MULTILINE)
    if projects:
        first = projects.group(1).split(",", 1)[0].strip().strip("'\"")
        if safe_project_slug(first):
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
        name = safe_project_slug(name)
        if name:
            keyword_map[name] = list(dict.fromkeys(k for k in keywords if k))

    for name, keywords in (cfg.get("project_keywords") or {}).items():
        safe_name = safe_project_slug(name)
        if safe_name:
            keyword_map[safe_name] = list(
                dict.fromkeys([safe_name, *(keywords or [])])
            )

    return keyword_map


# ── Session ID Generation ──────────────────────────────────────
def generate_session_id(transcript_path, meta):
    """Generate a stable, unique session ID."""
    if meta.get("session_id"):
        session_id = re.sub(r"[\x00-\x1f\x7f]", "", str(meta["session_id"])).strip()
        if session_id:
            return session_id[:200]

    # Use transcript filename as base
    basename = os.path.basename(transcript_path)
    session_id = basename.replace(".jsonl", "")

    # If it looks like a UUID already, use it
    if len(session_id) >= 32:
        return session_id

    # Otherwise, hash the path for stability
    date_str = meta.get("date", datetime.now(CST).strftime("%Y-%m-%d"))
    path_hash = hashlib.md5(
        transcript_path.encode(),
        usedforsecurity=False,
    ).hexdigest()[:8]
    return f"{date_str}-{path_hash}"


# ── Vault Writing ──────────────────────────────────────────────
def write_session_to_vault(cfg, session_id, date_str, project, meta,
                           decisions, errors, summary):
    """Write session summary .md to vault. Returns count of files written."""
    project = safe_project_slug(project) or "Project-Infra"
    date_str = normalize_iso_date(
        date_str, datetime.now(CST).strftime("%Y-%m-%d")
    )
    session_id = re.sub(r"[\x00-\x1f\x7f]", "", str(session_id or "")).strip()
    if not session_id:
        session_id = hashlib.sha256(
            f"{project}:{date_str}".encode("utf-8")
        ).hexdigest()[:16]
    project, date_str = reuse_existing_session_route(
        cfg, session_id, project, date_str
    )
    decisions, errors, summary = sanitize_harvested_content(
        cfg, decisions, errors, summary
    )
    sessions_dir = safe_vault_path(
        cfg['vault_path'], "01-Projects", project, "Memory", "sessions"
    )
    os.makedirs(sessions_dir, exist_ok=True)

    generated_title = generate_title(decisions, errors, summary)
    filepath = find_session_file_by_id(sessions_dir, session_id, date_str)
    if not filepath:
        filepath = os.path.join(sessions_dir, make_session_filename(sessions_dir, date_str, generated_title, session_id))
    harvested_at = datetime.now(CST).isoformat()
    first_harvested_at = harvested_at
    route_metadata_changed = False

    # Check if already exists (idempotent)
    if os.path.exists(filepath):
        print(f"[harvester] Session file already exists: {filepath} — appending new items only")
        # Read existing, merge new decisions/errors
        existing = read_existing_session(filepath)
        existing_summary = read_existing_session_summary(filepath)
        route_metadata_changed = not bool(existing.get("first_harvested_at"))
        first_harvested_at = str(
            existing.get("first_harvested_at")
            or existing.get("harvested_at")
            or harvested_at
        )
        existing_decisions = existing.get("decisions_made", [])
        existing_errors = existing.get("errors_encountered", [])
        new_decisions = merge_unique(decisions, existing_decisions, ("text", "context"))
        new_errors = merge_unique(errors, existing_errors, ("type", "resolution"))
        summary_changed = bool(summary) and normalize_session_summary(summary) != normalize_session_summary(existing_summary)
        if (
            not new_decisions
            and not new_errors
            and not summary_changed
            and not route_metadata_changed
        ):
            return 0
        decisions = existing_decisions + new_decisions
        errors = existing_errors + new_errors
        summary = summary if summary else existing_summary
        existing_title = clean_title_text(existing.get("ai_title", ""))
        if generated_title == "会话记忆" and existing_title:
            generated_title = existing_title

    decisions = normalize_session_memory_records(
        decisions,
        "decision",
        project,
        session_id,
        date_str,
    )
    errors = normalize_session_memory_records(
        errors,
        "error",
        project,
        session_id,
        date_str,
    )

    # Build frontmatter
    tags = list(set(
        tag for d in decisions for tag in extract_tags_from_decision(d)
    ))
    tags.extend([e["type"].split("_")[0] for e in errors])  # category as tag

    fm = {
        "session_id": session_id,
        "memory_schema_version": RUNTIME_SCHEMA_VERSION,
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
        "first_harvested_at": first_harvested_at,
        "harvested_at": harvested_at,
    }

    # Build body
    body_parts = [f"# {fm['ai_title']}\n"]
    body_parts.append(f"Session: {session_id} | Date: {date_str} | Project: {project}\n")
    body_parts.append("\n## Related\n")
    body_parts.append(f"- [[01-Projects/{project}/Memory/decisions|{project} decisions]]\n")
    body_parts.append(f"- [[01-Projects/{project}/Memory/pitfalls|{project} pitfalls]]\n")
    body_parts.append("- [[00-Inbox/Agent Memory Index|Agent Memory Index]]\n")
    body_parts.append("- [[03-Maps/timeline|Timeline]]\n")
    body_parts.append("- [[03-Maps/topic-index|Topic Index]]\n")

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
    filepath = rename_generic_session_file(
        filepath, sessions_dir, date_str, generated_title, session_id
    )

    print(f"[harvester] Wrote session: {filepath}")
    return 1


def normalize_session_memory_records(
    records,
    memory_type,
    project,
    session_id,
    date_str,
):
    """Serialize session evidence with the same schema as formal records."""
    normalized = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        formal = normalize_formal_record(
            record,
            memory_type=memory_type,
            default_project=project,
            source_ref=f"session:{session_id}",
            source_record_key=f"{memory_type}:{index}",
            date=date_str,
        )
        normalized.append(serialize_formal_project_record(formal, memory_type))
    return normalized


def rename_generic_session_file(filepath, sessions_dir, date_str, title, session_id):
    """Rename only generator placeholders when later content provides a title."""
    basename = os.path.basename(filepath)
    stem = basename[:-3] if basename.endswith(".md") else basename
    is_generic = bool(
        re.fullmatch(
            rf"{re.escape(date_str)}-会话记忆(?:-[0-9a-f]{{8}})?",
            stem,
        )
    )
    is_uuid_name = bool(UUID_SESSION_NAME.search(stem))
    if title == "会话记忆" or not (is_generic or is_uuid_name):
        return filepath

    target_name = make_session_filename(
        sessions_dir, date_str, title, session_id
    )
    target = safe_vault_path(sessions_dir, target_name)
    if target == filepath:
        return filepath
    os.replace(filepath, target)
    return target


def find_session_file_by_id(sessions_dir, session_id, date_str=None):
    """Find an existing session file by its stable transcript identity."""
    if not os.path.isdir(sessions_dir):
        return None
    matches = []
    for filename in os.listdir(sessions_dir):
        if not filename.endswith(".md") or filename.startswith("_"):
            continue
        path = os.path.join(sessions_dir, filename)
        fm = read_existing_session(path)
        if fm.get("session_id") == session_id:
            matches.append(path)
    if not matches:
        return None
    return sorted(matches)[0]


def reuse_existing_session_route(cfg, session_id, project, date_str):
    """Keep one session_id on its first persisted project and date route."""
    existing = find_session_file_in_vault(cfg.get("vault_path"), session_id)
    if not existing:
        return project, date_str

    rel = os.path.relpath(existing, cfg["vault_path"])
    parts = rel.split(os.sep)
    existing_project = safe_project_slug(parts[1] if len(parts) > 1 else "")
    fm = read_existing_session(existing)
    existing_date = normalize_iso_date(fm.get("date"), date_str)
    return existing_project or project, existing_date


def find_session_file_in_vault(vault, session_id):
    """Return one stable canonical note for a session_id across all projects."""
    if not vault or not session_id:
        return None
    projects_dir = safe_vault_path(vault, "01-Projects")
    if not os.path.isdir(projects_dir):
        return None

    matches = []
    for project_entry in os.scandir(projects_dir):
        if not project_entry.is_dir(follow_symlinks=False):
            continue
        try:
            sessions_dir = safe_vault_path(
                vault,
                "01-Projects",
                project_entry.name,
                "Memory",
                "sessions",
            )
        except ValueError:
            continue
        if not os.path.isdir(sessions_dir):
            continue
        for entry in os.scandir(sessions_dir):
            if (
                not entry.is_file(follow_symlinks=False)
                or not entry.name.endswith(".md")
                or entry.name.startswith("_")
            ):
                continue
            fm = read_existing_session(entry.path)
            if fm.get("session_id") != session_id:
                continue
            first_harvested_at = fm.get("first_harvested_at")
            if first_harvested_at:
                matches.append((0, str(first_harvested_at), entry.path))
            else:
                # Legacy notes have no immutable marker. Path order is stable,
                # and the selected note receives a marker on its next write.
                matches.append((1, "", entry.path))

    if not matches:
        return None
    return min(matches)[2]


def make_session_filename(sessions_dir, date_str, title, session_id):
    """Create a readable, collision-resistant session filename."""
    slug = filename_slug(title) or hashlib.sha256(
        str(session_id).encode("utf-8")
    ).hexdigest()[:12]
    filename = f"{date_str}-{slug}.md"
    filepath = os.path.join(sessions_dir, filename)
    if not os.path.exists(filepath):
        return filename
    session_hash = hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:8]
    return f"{date_str}-{slug}-{session_hash}.md"


def filename_slug(title, max_length=64):
    """Sanitize a title for macOS/Obsidian Markdown filenames."""
    return safe_filename(title, default="", max_length=max_length)


def read_existing_session(filepath):
    """Read existing session .md and return its frontmatter."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
        frontmatter_text, _body = split_frontmatter_text(content)
        if frontmatter_text is not None:
            return yaml.safe_load(frontmatter_text) or {}
    except Exception:
        pass
    return {}


def read_existing_session_summary(filepath):
    """Read the body Session Summary section from an existing session note."""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return None
    match = re.search(
        r"(?ms)^## Session Summary\s*\n(.*?)(?=^## |\Z)",
        content,
    )
    if not match:
        return None
    return match.group(1).strip() or None


def normalize_session_summary(summary):
    return re.sub(r"\s+", " ", str(summary or "").strip())


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


def generate_title(decisions, errors, summary=None):
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

    summary_text = summary_title(summary)
    if summary_text:
        return summary_text

    return "会话记忆"


def summary_title(summary):
    """Extract a readable title from a structured SESSION_SUMMARY block."""
    if not summary:
        return ""
    try:
        parsed = yaml.safe_load(summary)
    except yaml.YAMLError:
        parsed = None
    if isinstance(parsed, dict):
        for key in ("summary", "title", "primary_outcome"):
            value = parsed.get(key)
            if isinstance(value, str) and clean_title_text(value):
                return clean_title_text(value)
    match = re.search(r"(?m)^summary:\s*[\"']?(.*?)[\"']?\s*$", str(summary))
    return clean_title_text(match.group(1)) if match else ""


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
    value = redact_sensitive(value)
    value = re.sub(r"\s+", "_", str(value or "").strip())
    return re.sub(r"[^A-Za-z0-9_./-]", "", value)[:96] or "other"


def sanitize_obsidian_markdown(text, cfg):
    """Return Markdown that is safe to store inside an Obsidian vault."""
    if text is None:
        return ""
    text = redact_sensitive(text)
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
        if re.fullmatch(r"[.\u2026\s]+", target):
            return f"`{target}`"
        directory_target = resolve_vault_directory_target(target, vault)
        if directory_target:
            if label and label != target:
                return f"{label} (`{directory_target}`)"
            return f"`{directory_target}`"
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


def resolve_vault_directory_target(target, vault):
    """Return a relative vault directory without turning it into a graph node."""
    normalized = normalize_local_link_target(target).strip(os.sep)
    if not normalized or os.path.isabs(normalized):
        return None
    candidate = os.path.realpath(os.path.join(vault, normalized))
    real_vault = os.path.realpath(vault)
    try:
        if os.path.commonpath([real_vault, candidate]) != real_vault:
            return None
    except ValueError:
        return None
    if not os.path.isdir(candidate):
        return None
    return os.path.relpath(candidate, real_vault).replace(os.sep, "/")


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
def append_decisions(
    cfg,
    project,
    decisions,
    session_id,
    date_str,
    source_cursor="initial",
):
    """Append decisions to project's decisions.md."""
    bound = bind_batch_record_keys(decisions, "decision", source_cursor)
    for target, records in group_records_by_project(cfg, project, bound).items():
        append_decisions_for_project(
            cfg,
            target,
            records,
            session_id,
            date_str,
        )


def append_decisions_for_project(cfg, project, decisions, session_id, date_str):
    dec_path = safe_vault_path(
        cfg['vault_path'], "01-Projects", project, "Memory", "decisions.md"
    )
    os.makedirs(os.path.dirname(dec_path), exist_ok=True)
    existing = load_formal_project_records(dec_path, "decisions", "decision", project)
    incoming = [
        normalize_formal_record(
            decision,
            memory_type="decision",
            default_project=project,
            source_ref=f"session:{session_id}",
            source_record_key=(
                decision.get("source_record_key") or f"decision:initial:{index}"
            ),
            date=date_str,
        )
        for index, decision in enumerate(decisions)
        if isinstance(decision, dict)
        and (decision.get("text") or decision.get("title"))
    ]
    records = merge_formal_records([*existing, *incoming])
    if records == existing:
        return
    write_formal_project_records(dec_path, "decisions", project, records)


def append_errors_to_pitfalls(
    cfg,
    project,
    errors,
    session_id,
    date_str,
    source_cursor="initial",
):
    """Append errors to project's pitfalls.md."""
    bound = bind_batch_record_keys(errors, "error", source_cursor)
    for target, records in group_records_by_project(cfg, project, bound).items():
        append_errors_for_project(
            cfg,
            target,
            records,
            session_id,
            date_str,
        )


def append_errors_for_project(cfg, project, errors, session_id, date_str):
    pit_path = safe_vault_path(
        cfg['vault_path'], "01-Projects", project, "Memory", "pitfalls.md"
    )
    os.makedirs(os.path.dirname(pit_path), exist_ok=True)
    existing = load_formal_project_records(pit_path, "pitfalls", "error", project)
    incoming = [
        normalize_formal_record(
            error,
            memory_type="error",
            default_project=project,
            source_ref=f"session:{session_id}",
            source_record_key=(
                error.get("source_record_key") or f"error:initial:{index}"
            ),
            date=date_str,
        )
        for index, error in enumerate(errors)
        if isinstance(error, dict)
        and (error.get("resolution") or error.get("summary"))
    ]
    records = merge_formal_records([*existing, *incoming])
    if records == existing:
        return
    write_formal_project_records(pit_path, "pitfalls", project, records)


def group_records_by_project(cfg, default_project, records):
    fallback = canonical_project(default_project) or "Project-Infra"
    grouped = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        explicit = canonicalize_explicit_project(cfg, record.get("project"))
        project = canonical_project(explicit or fallback) or "Project-Infra"
        bound = {**record, "project": project, "scope": "project"}
        grouped.setdefault(project, []).append(bound)
    return grouped


def bind_batch_record_keys(records, memory_type, source_cursor):
    cursor = " ".join(str(source_cursor or "initial").split()) or "initial"
    return [
        {
            **record,
            "source_record_key": f"{memory_type}:{cursor}:{index}",
        }
        for index, record in enumerate(records)
        if isinstance(record, dict)
    ]


def load_formal_project_records(path, key, memory_type, project):
    if not os.path.exists(path):
        return []
    try:
        content = read_text_file(path)
        frontmatter_text, _body = split_frontmatter_text(content)
        frontmatter = (
            yaml.safe_load(frontmatter_text)
            if frontmatter_text is not None
            else {}
        )
    except (OSError, yaml.YAMLError):
        return []
    source_ref = "note:" + os.path.relpath(
        path,
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(path)))),
    ).replace(os.sep, "/").removesuffix(".md")
    return merge_formal_records(
        [
            normalize_formal_record(
                item,
                memory_type=memory_type,
                default_project=project,
                source_ref=source_ref,
                source_record_key=f"{key}:{index}",
                date=str((frontmatter or {}).get("last_updated") or "")[:10],
            )
            for index, item in enumerate((frontmatter or {}).get(key, []))
            if isinstance(item, dict)
        ]
    )


def write_formal_project_records(path, key, project, records):
    memory_type = "decision" if key == "decisions" else "error"
    serialized = [serialize_formal_project_record(item, memory_type) for item in records]
    frontmatter = {
        "project": project,
        "schema_version": RUNTIME_SCHEMA_VERSION,
        key: serialized,
        "last_updated": datetime.now(CST).isoformat(),
    }
    title = "Decisions" if key == "decisions" else "Pitfalls"
    lines = [
        f"# {title}",
        "",
        "## Related",
        "",
        "- [[00-Inbox/Agent Memory Index|Agent Memory Index]]",
        "- [[03-Maps/timeline|Timeline]]",
        "- [[03-Maps/topic-index|Topic Index]]",
        "",
        "## Formal Memory",
        "",
    ]
    for item in serialized:
        if memory_type == "decision":
            detail = f"context: {item.get('context', '')}"
            label = item.get("text", "")
        else:
            detail = f"resolution: {item.get('resolution', '')}"
            label = item.get("type", "")
        refs = ", ".join(f"`{ref}`" for ref in item.get("source_refs", [])) or "-"
        lines.append(
            f"- [{item.get('date', '') or '-'}] **{label}** | status: "
            f"`{item.get('status', '')}` | {detail} | sources: {refs}"
        )
    content = (
        "---\n"
        + yaml.dump(
            frontmatter,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        + "---\n\n"
        + "\n".join(lines).rstrip()
        + "\n"
    )
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(content)
    os.replace(tmp, path)


def serialize_formal_project_record(record, memory_type):
    item = {
        "id": record["id"],
        "revision": record["revision"],
    }
    if memory_type == "decision":
        item.update({"text": record.get("title", ""), "context": record.get("summary", "")})
    else:
        item.update({"type": record.get("title", ""), "resolution": record.get("summary", "")})
    item.update(
        {
            "status": record.get("status", "active"),
            "project": record.get("project", ""),
            "scope": record.get("scope", "project"),
            "date": record.get("date", ""),
            "source_refs": list(record.get("source_refs") or []),
            "aliases": list(record.get("aliases") or []),
        }
    )
    for key in (
        "superseded_by",
        "retracted_reason",
        "expired_reason",
        "expires_at",
    ):
        if record.get(key):
            item[key] = record[key]
    if record.get("requires"):
        item["requires"] = list(record["requires"])
    return item


def _rewrite_project_md(filepath, key, new_items, body, session_id):
    """Rewrite a project .md file with updated frontmatter list."""
    try:
        # Build frontmatter
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                old_content = f.read()
            frontmatter_text, _old_body = split_frontmatter_text(old_content)
            old_fm = (
                yaml.safe_load(frontmatter_text)
                if frontmatter_text is not None and frontmatter_text.strip()
                else {}
            )
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


def session_link_for(cfg, project, session_id, date_str):
    project = safe_project_slug(project) or "Project-Infra"
    vault = cfg["vault_path"]
    sessions_dir = safe_vault_path(
        vault, "01-Projects", project, "Memory", "sessions"
    )
    try:
        _directories, filenames = secure_list_directory(sessions_dir, vault)
    except FileNotFoundError:
        return session_id
    matches = []
    for filename in filenames:
        if not filename.endswith(".md") or filename.startswith("_"):
            continue
        path = os.path.join(sessions_dir, filename)
        data = secure_read_bytes(path, MAX_MANAGED_REPAIR_FILE_BYTES, root=vault)
        if len(data) > MAX_MANAGED_REPAIR_FILE_BYTES:
            raise OSError(
                f"managed repair file exceeds {MAX_MANAGED_REPAIR_FILE_BYTES} bytes"
            )
        frontmatter_text, _body = split_frontmatter_text(data.decode("utf-8"))
        if frontmatter_text is None:
            continue
        try:
            frontmatter = yaml.safe_load(frontmatter_text) or {}
        except yaml.YAMLError:
            continue
        if frontmatter.get("session_id") == session_id:
            matches.append(path)
    filepath = sorted(matches)[0] if matches else None
    if not filepath:
        return session_id
    rel_path = os.path.relpath(filepath, vault).replace(os.sep, "/")
    if rel_path.endswith(".md"):
        rel_path = rel_path[:-3]
    label = os.path.basename(rel_path)
    return obsidian_link(rel_path, label)


def ensure_obsidian_ignore_filters(cfg, ownership_check=None, mutation_io=None):
    """Hide machine-generated/internal folders that should not appear as notes."""
    vault = cfg.get("vault_path")
    if not vault:
        return
    _check_ownership(ownership_check)
    cleanup_bad_obsidian_path_artifacts(
        cfg,
        ownership_check=ownership_check,
        mutation_io=mutation_io,
    )
    _check_ownership(ownership_check)
    repair_obsidian_workspace(
        cfg,
        ownership_check=ownership_check,
        mutation_io=mutation_io,
    )
    _check_ownership(ownership_check)
    obsidian_dir = os.path.join(vault, ".obsidian")
    app_json = os.path.join(obsidian_dir, "app.json")
    filters = OBSIDIAN_IGNORE_FILTERS
    try:
        _cooperative_ensure_directory(
            obsidian_dir,
            ownership_check,
            mutation_io,
            root=vault,
        )
        data = {}
        app_exists = True
        try:
            data = json.loads(
                _cooperative_read_text(
                    app_json,
                    ownership_check,
                    mutation_io,
                    root=vault,
                )
            )
        except FileNotFoundError:
            app_exists = False
        if app_exists:
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
        if changed or not app_exists:
            content = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
            _cooperative_atomic_write(
                app_json,
                content,
                ownership_check,
                mutation_io,
                root=vault,
            )

        graph_json = os.path.join(obsidian_dir, "graph.json")
        graph = {}
        graph_exists = True
        try:
            graph = json.loads(
                _cooperative_read_text(
                    graph_json,
                    ownership_check,
                    mutation_io,
                    root=vault,
                )
            )
        except FileNotFoundError:
            graph_exists = False
        if graph_exists:
            if not isinstance(graph, dict):
                graph = {}
        search = str(graph.get("search") or "").strip()
        graph_changed = False
        for item in filters:
            query = f'-path:"{item.rstrip("/")}"'
            if query not in search:
                search = f"{search} {query}".strip()
                graph_changed = True
        graph["search"] = search
        if graph_changed or not graph_exists:
            content = json.dumps(graph, ensure_ascii=False, indent=2) + "\n"
            _cooperative_atomic_write(
                graph_json,
                content,
                ownership_check,
                mutation_io,
                root=vault,
            )
    except Exception as e:
        if mutation_io is not None:
            raise
        _check_ownership(ownership_check)
        print(f"[harvester] WARNING: Could not update Obsidian ignore filters: {e}")


def cleanup_bad_obsidian_path_artifacts(
    cfg,
    ownership_check=None,
    mutation_io=None,
):
    """Remove empty notes created when Obsidian opens malformed local paths."""
    vault = cfg.get("vault_path")
    if not vault:
        return
    bad_root = os.path.join(vault, "Users")
    try:
        try:
            rows = _cooperative_walk(
                bad_root,
                ownership_check,
                mutation_io,
                root=vault,
                topdown=False,
            )
        except FileNotFoundError:
            return
        for root, _dirs, files in rows:
            for filename in files:
                path = os.path.join(root, filename)
                try:
                    if filename.endswith(".md") and _cooperative_file_is_empty(
                        path,
                        ownership_check,
                        mutation_io,
                        root=vault,
                    ):
                        _cooperative_remove_file(
                            path,
                            ownership_check,
                            mutation_io,
                            root=vault,
                        )
                except OSError:
                    pass
            try:
                directories, remaining_files = _cooperative_list_directory(
                    root,
                    ownership_check,
                    mutation_io,
                    root=vault,
                )
                if not directories and not remaining_files:
                    _cooperative_remove_directory(
                        root,
                        ownership_check,
                        mutation_io,
                        root=vault,
                    )
            except OSError:
                pass
    except Exception as e:
        if mutation_io is not None:
            raise
        _check_ownership(ownership_check)
        print(f"[harvester] WARNING: Could not clean bad Obsidian path artifacts: {e}")


def repair_obsidian_workspace(cfg, ownership_check=None, mutation_io=None):
    """Remove bad local path references from Obsidian workspace state."""
    vault = cfg.get("vault_path")
    if not vault:
        return
    workspace = os.path.join(vault, ".obsidian", "workspace.json")
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
        try:
            old = _cooperative_read_text(
                workspace,
                ownership_check,
                mutation_io,
                root=vault,
            )
        except FileNotFoundError:
            return
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
            _cooperative_atomic_write(
                workspace,
                new,
                ownership_check,
                mutation_io,
                root=vault,
            )
            print("[harvester] Repaired unsafe local paths in Obsidian workspace")
    except Exception as e:
        if mutation_io is not None:
            raise
        _check_ownership(ownership_check)
        print(f"[harvester] WARNING: Could not repair Obsidian workspace: {e}")


def repair_generated_vault_markdown(cfg, ownership_check=None, mutation_io=None):
    """Sanitize generated memory files that may contain older unsafe links."""
    vault = cfg.get("vault_path")
    if not vault:
        return 0
    roots = [
        os.path.join(vault, "00-Inbox"),
        os.path.join(vault, "01-Projects"),
        os.path.join(vault, "04-Feedback", "_memory-candidates"),
        os.path.join(vault, "04-Feedback", "_skill-preferences"),
        os.path.join(vault, "04-Feedback", "_workflow-candidates"),
        os.path.join(vault, "04-Feedback", "_annotation-candidates"),
        os.path.join(vault, "05-Agent-Memory"),
    ]
    changed = 0
    for root in roots:
        try:
            rows = _cooperative_walk(
                root,
                ownership_check,
                mutation_io,
                root=vault,
                excluded_directory_names=VAULT_INTERNAL_DIR_NAMES,
            )
        except FileNotFoundError:
            continue
        except OSError as e:
            if mutation_io is not None:
                raise
            _check_ownership(ownership_check)
            print(f"[harvester] WARNING: Could not traverse {root}: {e}")
            continue
        for current, _dirs, files in rows:
            for filename in files:
                if not filename.endswith(".md"):
                    continue
                path = os.path.join(current, filename)
                try:
                    old = _cooperative_read_text(
                        path,
                        ownership_check,
                        mutation_io,
                        root=vault,
                    )
                    rel_path = os.path.relpath(path, vault).replace(os.sep, "/")
                    if not is_generated_memory_note(rel_path, old):
                        continue
                    new = sanitize_generated_memory_markdown(old, cfg, rel_path)
                    if new != old:
                        _cooperative_atomic_write(
                            path,
                            new,
                            ownership_check,
                            mutation_io,
                            root=vault,
                        )
                        changed += 1
                except Exception as e:
                    if mutation_io is not None:
                        raise
                    _check_ownership(ownership_check)
                    print(f"[harvester] WARNING: Could not sanitize {path}: {e}")
    if changed:
        print(f"[harvester] Sanitized unsafe local links in {changed} generated markdown file(s)")
    return changed


def sanitize_generated_memory_markdown(content, cfg, rel_path=""):
    """Sanitize generated Markdown without rewriting schema revision hashes."""
    protected = []
    revision_line = re.compile(
        r"(?m)^[ \t]*(?:-\s*)?revision:\s*[`'\"]?[0-9a-fA-F]{64}[`'\"]?[ \t]*$"
    )

    def protect(match):
        marker = f"\x00AMB_REVISION_{len(protected)}\x00"
        protected.append((marker, match.group(0)))
        return marker

    sanitized = sanitize_obsidian_markdown(revision_line.sub(protect, content), cfg)
    for marker, original in protected:
        sanitized = sanitized.replace(marker, original)
    return refresh_generated_memory_revisions(sanitized, rel_path)


def refresh_generated_memory_revisions(content, rel_path):
    fm, body = split_markdown_frontmatter(content)
    if body is None:
        return content
    changed = False
    aggregate = re.fullmatch(
        r"01-Projects/[^/]+/Memory/(decisions|pitfalls)\.md",
        rel_path,
    )
    if fm.get("schema_version") == RUNTIME_SCHEMA_VERSION and aggregate:
        key = aggregate.group(1)
        memory_type = "decision" if key == "decisions" else "error"
        for record in fm.get(key, []) or []:
            changed |= refresh_project_record_revision(record, memory_type)
    if fm.get("memory_schema_version") == RUNTIME_SCHEMA_VERSION:
        for record in fm.get("decisions_made", []) or []:
            changed |= refresh_project_record_revision(record, "decision")
        for record in fm.get("errors_encountered", []) or []:
            changed |= refresh_project_record_revision(record, "error")

    if fm.get("schema_version") == RUNTIME_SCHEMA_VERSION:
        candidate_revision = None
        if "/_memory-candidates/" in f"/{rel_path}":
            candidate_revision = personal_candidate_revision
        elif "/_skill-preferences/" in f"/{rel_path}":
            candidate_revision = skill_candidate_revision
        elif "/_workflow-candidates/" in f"/{rel_path}":
            candidate_revision = workflow_candidate_revision
        if candidate_revision is not None and fm.get("revision"):
            revision = candidate_revision(fm)
            if fm.get("revision") != revision:
                fm["revision"] = revision
                changed = True

    kind = None
    if rel_path == "05-Agent-Memory/personal-memory.md":
        kind = "personal"
    elif rel_path == "05-Agent-Memory/skill-routing-rules.md":
        kind = "skill"
    elif rel_path == "05-Agent-Memory/workflow-rules.md":
        kind = "workflow"
    if fm.get("schema_version") == RUNTIME_SCHEMA_VERSION and kind:
        refreshed_body = refresh_formal_body_revisions(body, kind)
        if refreshed_body != body:
            body = refreshed_body
            changed = True

    if not changed:
        return content
    return render_markdown_frontmatter(fm, body)


def refresh_project_record_revision(record, memory_type):
    if not isinstance(record, dict):
        return False
    required = ("id", "revision", "status", "project", "scope", "source_refs")
    if any(not record.get(key) for key in required):
        return False
    if memory_type == "decision":
        title = record.get("text")
        summary = record.get("context")
    elif memory_type == "error":
        title = record.get("type")
        summary = record.get("resolution")
    else:
        return False
    if not str(title or "").strip():
        return False
    revision = memory_revision(
        {
            "type": memory_type,
            "status": record.get("status"),
            "project": record.get("project"),
            "scope": record.get("scope"),
            "title": title,
            "summary": summary,
            "superseded_by": record.get("superseded_by", ""),
            "requires": record.get("requires") or [],
            "expires_at": record.get("expires_at", ""),
        }
    )
    if record.get("revision") == revision:
        return False
    record["revision"] = revision
    return True


def refresh_formal_body_revisions(body, kind):
    headings = list(re.finditer(r"^##\s+(.+?)\s*$", body, re.MULTILINE))
    updated = body
    for index in range(len(headings) - 1, -1, -1):
        match = headings[index]
        start = match.end()
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        section = body[start:end]
        revision = expected_formal_section_revision(
            match.group(1).strip(),
            section,
            kind,
        )
        if not revision:
            continue
        refreshed, count = re.subn(
            r"(?m)^(-\s*revision:\s*)`?[0-9a-fA-F]{64}`?\s*$",
            rf"\1`{revision}`",
            section,
            count=1,
        )
        if count == 1 and refreshed != section:
            updated = updated[:start] + refreshed + updated[end:]
    return updated


def render_markdown_frontmatter(fm, body):
    return (
        "---\n"
        + yaml.dump(
            fm,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        + "---\n\n"
        + body.rstrip()
        + "\n"
    )


def is_generated_memory_note(rel_path, content):
    """Limit repair rewrites to files owned by the knowledge-brain generator."""
    if rel_path == "00-Inbox/Agent Memory Index.md":
        return True
    if re.match(r"^01-Projects/[^/]+/Memory/(decisions|pitfalls)\.md$", rel_path):
        return True
    if re.match(r"^01-Projects/[^/]+/Memory/sessions/[^/]+\.md$", rel_path):
        return True
    fm, _ = split_markdown_frontmatter(content)
    return (
        fm.get("harvested_by") == "session_harvester.py"
        or fm.get("generated_by") in {
            "session_harvester.py",
            "memory_judge.py",
            "skill_preference_learner.py",
            "workflow_memory.py",
            "knowledge_index.py",
        }
    )


def repair_generated_graph_links(cfg, ownership_check=None, mutation_io=None):
    """Add real wiki links to generated notes so Obsidian graph can connect them."""
    vault = cfg.get("vault_path")
    if not vault:
        return 0
    changed = 0
    _check_ownership(ownership_check)
    changed += repair_project_memory_graph_links(
        cfg,
        ownership_check=ownership_check,
        mutation_io=mutation_io,
    )
    _check_ownership(ownership_check)
    changed += repair_personal_memory_graph_links(
        cfg,
        ownership_check=ownership_check,
        mutation_io=mutation_io,
    )
    _check_ownership(ownership_check)
    if changed:
        print(f"[harvester] Repaired graph links in {changed} generated note(s)")
    return changed


def repair_project_memory_graph_links(
    cfg,
    ownership_check=None,
    mutation_io=None,
):
    vault = cfg["vault_path"]
    projects_dir = os.path.join(vault, "01-Projects")
    try:
        projects, _files = _cooperative_list_directory(
            projects_dir,
            ownership_check,
            mutation_io,
            root=vault,
        )
    except FileNotFoundError:
        return 0
    changed = 0
    for project in projects:
        memory_dir = os.path.join(projects_dir, project, "Memory")
        try:
            _directories, memory_files = _cooperative_list_directory(
                memory_dir,
                ownership_check,
                mutation_io,
                root=vault,
            )
        except FileNotFoundError:
            continue
        for filename, kind in (("decisions.md", "decisions"), ("pitfalls.md", "pitfalls")):
            if filename not in memory_files:
                continue
            path = os.path.join(memory_dir, filename)
            old = _cooperative_read_text(
                path,
                ownership_check,
                mutation_io,
                root=vault,
            )
            fm, body = split_markdown_frontmatter(old)
            if body is None:
                continue
            new_fm = dict(fm)
            new_fm["project"] = project
            new_body = ensure_project_related_section(body, project, kind)
            new_body = relink_project_session_refs(new_body, cfg, project)
            if new_body != body or new_fm != fm:
                write_markdown_frontmatter(
                    path,
                    new_fm,
                    new_body,
                    ownership_check=ownership_check,
                    mutation_io=mutation_io,
                    root=vault,
                )
                changed += 1
    return changed


def repair_personal_memory_graph_links(
    cfg,
    ownership_check=None,
    mutation_io=None,
):
    vault = cfg["vault_path"]
    settings = cfg.get("personal_memory") or {}
    candidate_dir = safe_vault_path(
        vault, settings.get("candidate_dir", "04-Feedback/_memory-candidates")
    )
    formal_path = safe_vault_path(
        vault, settings.get("formal_path", "05-Agent-Memory/personal-memory.md")
    )
    changed = 0
    try:
        old = _cooperative_read_text(
            formal_path,
            ownership_check,
            mutation_io,
            root=vault,
        )
    except FileNotFoundError:
        old = None
    if old is not None:
        fm, body = split_markdown_frontmatter(old)
        if body is not None:
            new_body = ensure_personal_memory_related_section(body)

            def relink_project(match):
                project = match.group(1).strip()
                if project.casefold() in {"global", "unknown"}:
                    return match.group(0)
                return f"- project: {project_decision_link(project)}"

            new_body = re.sub(
                r"- project: `([^`]+)`",
                relink_project,
                new_body,
            )
            if new_body != body:
                write_markdown_frontmatter(
                    formal_path,
                    fm,
                    new_body,
                    ownership_check=ownership_check,
                    mutation_io=mutation_io,
                    root=vault,
                )
                changed += 1

    try:
        _directories, candidate_files = _cooperative_list_directory(
            candidate_dir,
            ownership_check,
            mutation_io,
            root=vault,
        )
    except FileNotFoundError:
        candidate_files = []
    for filename in candidate_files:
        if not filename.endswith(".md"):
            continue
        path = os.path.join(candidate_dir, filename)
        old = _cooperative_read_text(
            path,
            ownership_check,
            mutation_io,
            root=vault,
        )
        fm, body = split_markdown_frontmatter(old)
        if body is None:
            continue
        new_body = ensure_candidate_related_section(body, fm)
        if new_body != body:
            write_markdown_frontmatter(
                path,
                fm,
                new_body,
                ownership_check=ownership_check,
                mutation_io=mutation_io,
                root=vault,
            )
            changed += 1
    return changed


def ensure_project_related_section(body, project, kind):
    if "## Related" in body:
        return body
    related = [
        "## Related",
        "",
        "- [[00-Inbox/Agent Memory Index|Agent Memory Index]]",
        "- [[03-Maps/timeline|Timeline]]",
        "- [[03-Maps/topic-index|Topic Index]]",
    ]
    if kind == "decisions":
        related.append(f"- [[01-Projects/{project}/Memory/pitfalls|{project} pitfalls]]")
    else:
        related.append(f"- [[01-Projects/{project}/Memory/decisions|{project} decisions]]")
    return "\n".join(related) + "\n\n" + body.lstrip()


def relink_project_session_refs(body, cfg, project):
    lines = []
    for line in body.splitlines():
        if "| session: " not in line:
            lines.append(line)
            continue
        prefix, session_ref = line.rsplit("| session: ", 1)
        if "[[" in session_ref:
            lines.append(line)
            continue
        date_match = re.match(r"- \[(\d{4}-\d{2}-\d{2})\]", line)
        if not date_match:
            lines.append(line)
            continue
        session_id = session_ref.strip()
        session_link = session_link_for(cfg, project, session_id, date_match.group(1))
        lines.append(f"{prefix}| session: {session_link}")
    return "\n".join(lines) + ("\n" if body.endswith("\n") else "")


def ensure_personal_memory_related_section(body):
    if "## Related" in body:
        return body
    related = (
        "## Related\n\n"
        "- [[00-Inbox/Agent Memory Index|Agent Memory Index]]\n"
        "- [[03-Maps/timeline|Timeline]]\n"
        "- [[03-Maps/topic-index|Topic Index]]\n\n"
    )
    return body.replace(
        "Promoted memories from repeated or high-confidence conversations.\n",
        "Promoted memories from repeated or high-confidence conversations.\n\n"
        + related,
        1,
    )


def ensure_candidate_related_section(body, fm):
    if "## Related" in body:
        return body
    project = fm.get("project", "")
    links = [
        "- [[00-Inbox/Agent Memory Index|Agent Memory Index]]",
        "- [[05-Agent-Memory/personal-memory|Personal Memory]]",
    ]
    if project:
        links.extend(
            [
                f"- {project_decision_link(project)}",
                f"- [[01-Projects/{project}/Memory/pitfalls|{project} pitfalls]]",
            ]
        )
    related = "## Related\n\n" + "\n".join(links) + "\n\n"
    if "\n## Evidence\n" in body:
        return body.replace("\n## Evidence\n", "\n" + related + "## Evidence\n", 1)
    return body.rstrip() + "\n\n" + related


def project_decision_link(project):
    if not project:
        return "`unknown`"
    return f"[[01-Projects/{project}/Memory/decisions|{project}]]"


def read_text_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def split_markdown_frontmatter(content):
    frontmatter_text, body = split_frontmatter_text(content)
    if frontmatter_text is None:
        return {}, None
    try:
        fm = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, body.lstrip("\n")


def write_markdown_frontmatter(
    path,
    fm,
    body,
    ownership_check=None,
    mutation_io=None,
    root=None,
):
    content = "---\n"
    content += yaml.dump(
        fm,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    )
    content += "---\n\n" + body.rstrip() + "\n"
    _cooperative_atomic_write(
        path,
        content,
        ownership_check,
        mutation_io,
        root=root,
    )


# ── Global Memory Index ────────────────────────────────────────
def rebuild_memory_index(
    cfg,
    ownership_check=None,
    mutation_io=None,
    repair_generated=True,
):
    """Rebuild a visible Obsidian index of harvested sessions, decisions, and errors."""
    rebuild_started = monotonic()
    repair_started = rebuild_started
    _check_ownership(ownership_check)
    if repair_generated:
        ensure_obsidian_ignore_filters(
            cfg,
            ownership_check=ownership_check,
            mutation_io=mutation_io,
        )
        _check_ownership(ownership_check)
        repair_generated_vault_markdown(
            cfg,
            ownership_check=ownership_check,
            mutation_io=mutation_io,
        )
        _check_ownership(ownership_check)
        repair_generated_graph_links(
            cfg,
            ownership_check=ownership_check,
            mutation_io=mutation_io,
        )
        _check_ownership(ownership_check)
    repair_seconds = monotonic() - repair_started

    vault = cfg["vault_path"]
    error_evidence_dirty = (
        error_evidence_dirty_token(cfg) if mutation_io is None else ""
    )
    index_path = cfg.get("memory_index_path") or os.path.join(
        vault, "00-Inbox", "Agent Memory Index.md"
    )
    index_path = safe_vault_path(vault, index_path)
    collect_started = monotonic()
    sessions = collect_harvested_sessions(vault)
    personal_memory = collect_personal_memory(vault, cfg)
    skill_preferences = collect_skill_preferences(vault, cfg)
    workflow_memory = collect_workflow_memory(vault, cfg)
    insight_memory = collect_insight_memory(vault, cfg)
    annotation_candidates = collect_annotation_candidates(vault, cfg)
    error_evidence = collect_error_evidence(vault, cfg)
    _check_ownership(ownership_check)
    collect_seconds = monotonic() - collect_started

    knowledge_started = monotonic()
    if mutation_io is None:
        knowledge_indexes = _rebuild_vault_knowledge_indexes_cooperative(
            cfg,
            ownership_check,
        )
    else:
        knowledge_indexes = _rebuild_vault_knowledge_indexes_cooperative(
            cfg,
            ownership_check,
            mutation_io,
        )
    _check_ownership(ownership_check)
    knowledge_seconds = monotonic() - knowledge_started

    render_write_started = monotonic()
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
    candidate_count = len(personal_memory.get("candidates", []))
    formal_count = 1 if personal_memory.get("formal_exists") else 0
    skill_candidate_count = len(skill_preferences.get("candidates", []))
    skill_rule_count = 1 if skill_preferences.get("formal_exists") else 0
    workflow_candidate_count = len(workflow_memory.get("candidates", []))
    workflow_rule_count = 1 if workflow_memory.get("formal_exists") else 0
    formal_insight_count = len(insight_memory.get("formal", []))
    insight_candidate_count = len(insight_memory.get("candidates", []))
    annotation_candidate_count = len(annotation_candidates.get("candidates", []))
    error_evidence_candidate_count = len(error_evidence.get("candidates", []))
    updated_at = datetime.now(CST).isoformat()

    fm = {
        "title": "Agent Memory Index",
        "generated_by": "session_harvester.py",
        "updated_at": updated_at,
        "session_count": len(sessions),
        "decision_count": decision_count,
        "error_count": error_count,
        "personal_memory_candidates": candidate_count,
        "personal_memory_files": formal_count,
        "skill_preference_candidates": skill_candidate_count,
        "skill_routing_rule_files": skill_rule_count,
        "workflow_memory_candidates": workflow_candidate_count,
        "workflow_rule_files": workflow_rule_count,
        "formal_insights": formal_insight_count,
        "insight_candidates": insight_candidate_count,
        "annotation_quality_candidates": annotation_candidate_count,
        "error_evidence_candidates": error_evidence_candidate_count,
        "keyword_index_terms": knowledge_indexes.get("keyword_terms", 0),
        "global_atoms": knowledge_indexes.get("global_atoms", 0),
        "recall_units": knowledge_indexes.get("recall_units", 0),
        "graph_nodes": knowledge_indexes.get("graph_nodes", 0),
        "graph_edges": knowledge_indexes.get("graph_edges", 0),
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

    body.append("\n## Personal Memory\n")
    formal_rel = personal_memory.get("formal_rel")
    if formal_rel:
        body.append(f"- Formal memory: {obsidian_link(formal_rel, 'Personal Memory')}")
    else:
        body.append("- Formal memory: not created yet")
    body.append(f"- Candidates waiting for repetition: {candidate_count}")
    if personal_memory.get("candidates"):
        body.append("\n### Memory Candidates\n")
        body.append("| Seen | Confidence | Type | Candidate |")
        body.append("|---:|---:|---|---|")
        for record in personal_memory["candidates"][:20]:
            body.append(
                "| {seen} | {confidence} | `{kind}` | {link} |".format(
                    seen=record.get("seen_count", ""),
                    confidence=record.get("confidence", ""),
                    kind=escape_table_cell(record.get("type", "")),
                    link=obsidian_link(
                        record["rel_path"],
                        record.get("title") or record["filename"],
                    ),
                )
            )

    body.append("\n## Skill Routing Rules\n")
    skill_formal_rel = skill_preferences.get("formal_rel")
    if skill_formal_rel:
        body.append(f"- Formal rules: {obsidian_link(skill_formal_rel, 'Skill Routing Rules')}")
    else:
        body.append("- Formal rules: not created yet")
    body.append(f"- Candidates waiting for repetition: {skill_candidate_count}")
    if skill_preferences.get("candidates"):
        body.append("\n### Skill Preference Candidates\n")
        body.append("| Seen | Confidence | Skill | Candidate |")
        body.append("|---:|---:|---|---|")
        for record in skill_preferences["candidates"][:20]:
            body.append(
                "| {seen} | {confidence} | `{skill}` | {link} |".format(
                    seen=record.get("seen_count", ""),
                    confidence=record.get("confidence", ""),
                    skill=escape_table_cell(record.get("skill_name", "")),
                    link=obsidian_link(
                        record["rel_path"],
                        record.get("title") or record["filename"],
                    ),
                )
            )

    body.append("\n## Workflow Rules\n")
    workflow_formal_rel = workflow_memory.get("formal_rel")
    if workflow_formal_rel:
        body.append(f"- Formal rules: {obsidian_link(workflow_formal_rel, 'Workflow Rules')}")
    else:
        body.append("- Formal rules: not created yet")
    body.append(f"- Candidates waiting for repetition: {workflow_candidate_count}")
    if workflow_memory.get("candidates"):
        body.append("\n### Workflow Candidates\n")
        body.append("| Seen | Confidence | Rule | Candidate |")
        body.append("|---:|---:|---|---|")
        for record in workflow_memory["candidates"][:20]:
            body.append(
                "| {seen} | {confidence} | `{rule}` | {link} |".format(
                    seen=record.get("seen_count", ""),
                    confidence=record.get("confidence", ""),
                    rule=escape_table_cell(record.get("rule_name", "")),
                    link=obsidian_link(
                        record["rel_path"],
                        record.get("title") or record["filename"],
                    ),
                )
            )

    body.append("\n## Insights\n")
    insight_formal_rel = insight_memory.get("formal_rel")
    if insight_formal_rel:
        body.append(f"- Formal insights: {obsidian_link(insight_formal_rel, 'Insights')}")
    else:
        body.append("- Formal insights: not created yet")
    body.append(f"- Reusable formal insights: {formal_insight_count}")
    body.append(f"- Candidates waiting for confirmation: {insight_candidate_count}")
    if insight_memory.get("candidates"):
        body.append("\n### Insight Candidates\n")
        body.append("| Seen | Confidence | Reason | Candidate |")
        body.append("|---:|---:|---|---|")
        for record in insight_memory["candidates"][:20]:
            reasons = ", ".join(record.get("quality_reasons") or [])
            body.append(
                "| {seen} | {confidence} | {reasons} | {link} |".format(
                    seen=record.get("seen_count", ""),
                    confidence=record.get("confidence", ""),
                    reasons=escape_table_cell(reasons),
                    link=obsidian_link(
                        record["rel_path"],
                        record.get("title")
                        or record.get("candidate_id")
                        or record["filename"],
                    ),
                )
            )

    body.append("\n## Annotation Quality Candidates\n")
    body.append(
        f"- Explicit tags waiting for confirmation: {annotation_candidate_count}"
    )
    if annotation_candidates.get("candidates"):
        body.append("\n| Seen | Type | Score | Reason | Candidate |")
        body.append("|---:|---|---:|---|---|")
        for record in annotation_candidates["candidates"][:20]:
            reasons = ", ".join(record.get("quality_reasons") or [])
            body.append(
                "| {seen} | `{kind}` | {score} | {reasons} | {link} |".format(
                    seen=record.get("seen_count", ""),
                    kind=escape_table_cell(record.get("annotation_type", "")),
                    score=record.get("quality_score", ""),
                    reasons=escape_table_cell(reasons),
                    link=obsidian_link(
                        record["rel_path"],
                        record.get("title") or record["filename"],
                    ),
                )
            )

    body.append("\n## Error Evidence Candidates\n")
    body.append(
        f"- Unresolved tool/review evidence: {error_evidence_candidate_count}"
    )
    if error_evidence.get("candidates"):
        body.append("\n| Seen | Severity | Source | Candidate |")
        body.append("|---:|---|---|---|")
        for record in error_evidence["candidates"][:20]:
            source = record.get("operation") or record.get("kind") or "unknown"
            label = f"{record.get('severity', 'error')} {source}"
            body.append(
                "| {seen} | `{severity}` | `{source}` | {link} |".format(
                    seen=record.get("seen_count", ""),
                    severity=escape_table_cell(record.get("severity", "")),
                    source=escape_table_cell(source),
                    link=obsidian_link(record["rel_path"], label),
                )
            )

    body.append("\n## Machine Indexes\n")
    body.append(
        f"- Keyword index terms: `{knowledge_indexes.get('keyword_terms', 0)}` "
        "([[05-Agent-Memory/keyword-index|Keyword Index]])"
    )
    body.append(
        f"- Global atoms: `{knowledge_indexes.get('global_atoms', 0)}` "
        "([[05-Agent-Memory/global-atoms|Global Atoms]])"
    )
    body.append(
        f"- Recall units: `{knowledge_indexes.get('recall_units', 0)}` "
        "([[05-Agent-Memory/recall-context|Recall Context]])"
    )
    body.append(
        f"- Memory graph: `{knowledge_indexes.get('graph_nodes', 0)}` nodes / "
        f"`{knowledge_indexes.get('graph_edges', 0)}` edges "
        "(`05-Agent-Memory/memory-graph.json`)"
    )

    content = "---\n"
    content += yaml.dump(fm, allow_unicode=True, default_flow_style=False, sort_keys=False)
    content += "---\n\n"
    content += "\n".join(body).rstrip() + "\n"

    _cooperative_ensure_directory(
        os.path.dirname(index_path),
        ownership_check,
        mutation_io,
        root=vault,
    )
    _cooperative_atomic_write(
        index_path,
        content,
        ownership_check,
        mutation_io,
        root=vault,
    )
    render_write_seconds = monotonic() - render_write_started

    dirty_clear_started = monotonic()
    if error_evidence_dirty:
        if not clear_error_evidence_dirty(cfg, error_evidence_dirty):
            raise ErrorEvidenceStateError(
                "error evidence generation changed during index rebuild"
            )
    dirty_clear_seconds = monotonic() - dirty_clear_started
    total_seconds = monotonic() - rebuild_started
    print(f"[harvester] Rebuilt memory index: {index_path}")
    print(
        "[harvester:index] Timing: "
        f"repair={repair_seconds:.2f}s, "
        f"collect={collect_seconds:.2f}s, "
        f"knowledge={knowledge_seconds:.2f}s, "
        f"render_write={render_write_seconds:.2f}s, "
        f"dirty_clear={dirty_clear_seconds:.2f}s, "
        f"total={total_seconds:.2f}s"
    )


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


def collect_personal_memory(vault, cfg):
    """Collect personal-memory candidate/formal files for the visible index."""
    settings = cfg.get("personal_memory") or {}
    candidate_dir = safe_vault_path(
        vault, settings.get("candidate_dir", "04-Feedback/_memory-candidates")
    )
    formal_path = safe_vault_path(
        vault, settings.get("formal_path", "05-Agent-Memory/personal-memory.md")
    )
    candidates = []
    if os.path.isdir(candidate_dir):
        for filename in sorted(os.listdir(candidate_dir)):
            if not filename.endswith(".md"):
                continue
            path = os.path.join(candidate_dir, filename)
            fm = read_existing_session(path)
            if not fm or fm.get("status") == "promoted":
                continue
            rel_path = os.path.relpath(path, vault).replace(os.sep, "/")
            fm["filename"] = filename
            fm["rel_path"] = rel_path[:-3] if rel_path.endswith(".md") else rel_path
            candidates.append(fm)
    candidates.sort(
        key=lambda item: (
            int(item.get("seen_count") or 0),
            float(item.get("confidence") or 0),
            str(item.get("last_seen") or ""),
        ),
        reverse=True,
    )
    formal_rel = None
    if os.path.exists(formal_path):
        formal_rel = os.path.relpath(formal_path, vault).replace(os.sep, "/")
        if formal_rel.endswith(".md"):
            formal_rel = formal_rel[:-3]
    return {
        "formal_exists": bool(formal_rel),
        "formal_rel": formal_rel,
        "candidates": candidates,
    }


def collect_skill_preferences(vault, cfg):
    """Collect adaptive skill-learning candidate/formal files for the index."""
    settings = cfg.get("skill_preferences") or {}
    candidate_dir = safe_vault_path(
        vault, settings.get("candidate_dir", "04-Feedback/_skill-preferences")
    )
    formal_path = safe_vault_path(
        vault, settings.get("formal_path", "05-Agent-Memory/skill-routing-rules.md")
    )
    candidates = []
    if os.path.isdir(candidate_dir):
        for filename in sorted(os.listdir(candidate_dir)):
            if not filename.endswith(".md"):
                continue
            path = os.path.join(candidate_dir, filename)
            fm = read_existing_session(path)
            if not fm or fm.get("status") == "promoted":
                continue
            rel_path = os.path.relpath(path, vault).replace(os.sep, "/")
            fm["filename"] = filename
            fm["rel_path"] = rel_path[:-3] if rel_path.endswith(".md") else rel_path
            candidates.append(fm)
    candidates.sort(
        key=lambda item: (
            int(item.get("seen_count") or 0),
            float(item.get("confidence") or 0),
            str(item.get("last_seen") or ""),
        ),
        reverse=True,
    )
    formal_rel = None
    if os.path.exists(formal_path):
        formal_rel = os.path.relpath(formal_path, vault).replace(os.sep, "/")
        if formal_rel.endswith(".md"):
            formal_rel = formal_rel[:-3]
    return {
        "formal_exists": bool(formal_rel),
        "formal_rel": formal_rel,
        "candidates": candidates,
    }


def collect_workflow_memory(vault, cfg):
    """Collect adaptive workflow-memory candidate/formal files for the index."""
    settings = cfg.get("workflow_memory") or {}
    candidate_dir = safe_vault_path(
        vault, settings.get("candidate_dir", "04-Feedback/_workflow-candidates")
    )
    formal_path = safe_vault_path(
        vault, settings.get("formal_path", "05-Agent-Memory/workflow-rules.md")
    )
    candidates = []
    if os.path.isdir(candidate_dir):
        for filename in sorted(os.listdir(candidate_dir)):
            if not filename.endswith(".md"):
                continue
            path = os.path.join(candidate_dir, filename)
            fm = read_existing_session(path)
            if not fm or fm.get("status") == "promoted":
                continue
            rel_path = os.path.relpath(path, vault).replace(os.sep, "/")
            fm["filename"] = filename
            fm["rel_path"] = rel_path[:-3] if rel_path.endswith(".md") else rel_path
            candidates.append(fm)
    candidates.sort(
        key=lambda item: (
            int(item.get("seen_count") or 0),
            float(item.get("confidence") or 0),
            str(item.get("last_seen") or ""),
        ),
        reverse=True,
    )
    formal_rel = None
    if os.path.exists(formal_path):
        formal_rel = os.path.relpath(formal_path, vault).replace(os.sep, "/")
        if formal_rel.endswith(".md"):
            formal_rel = formal_rel[:-3]
    return {
        "formal_exists": bool(formal_rel),
        "formal_rel": formal_rel,
        "candidates": candidates,
    }


def collect_insight_memory(vault, cfg):
    """Collect formal Insights and isolated candidates for the visible index."""
    settings = cfg.get("insight_memory") or {}
    candidate_dir = safe_vault_path(
        vault,
        settings.get("candidate_dir", "04-Feedback/_insight-candidates"),
    )
    formal_path = safe_vault_path(
        vault,
        settings.get("formal_path", "05-Agent-Memory/insights.md"),
    )
    candidates = []
    if os.path.isdir(candidate_dir):
        for filename in sorted(os.listdir(candidate_dir)):
            if not filename.endswith(".md"):
                continue
            path = os.path.join(candidate_dir, filename)
            fm = read_existing_session(path)
            if not fm or fm.get("status") != "candidate":
                continue
            rel_path = os.path.relpath(path, vault).replace(os.sep, "/")
            fm["filename"] = filename
            fm["rel_path"] = rel_path[:-3] if rel_path.endswith(".md") else rel_path
            candidates.append(fm)
    candidates.sort(
        key=lambda item: (
            safe_int(item.get("seen_count")),
            float(item.get("confidence") or 0),
            str(item.get("last_seen") or ""),
        ),
        reverse=True,
    )
    formal = []
    formal_rel = None
    if os.path.exists(formal_path):
        formal = load_formal_insight_records(formal_path, vault)
        formal_rel = os.path.relpath(formal_path, vault).replace(os.sep, "/")
        formal_rel = formal_rel[:-3] if formal_rel.endswith(".md") else formal_rel
    return {
        "formal": formal,
        "formal_exists": bool(formal_rel),
        "formal_rel": formal_rel,
        "candidates": candidates,
    }


def collect_error_evidence(vault, cfg):
    """Collect unresolved evidence candidates for the visible memory index."""
    settings = cfg.get("error_evidence") or {}
    candidate_dir = safe_vault_path(
        vault,
        settings.get("candidate_dir", "04-Feedback/_error-candidates"),
    )
    candidates = []
    records, _blocked = load_error_evidence_candidates(
        candidate_dir,
        source_limit=settings.get("source_limit", 20),
        excerpt_limit=settings.get("excerpt_limit", 500),
        vault_root=vault,
        fail_on_invalid=True,
    )
    for evidence_id, stored in records.items():
        if stored.get("status") != "candidate":
            continue
        fm = dict(stored)
        path = fm.pop("_path")
        filename = f"{evidence_id}.md"
        rel_path = os.path.relpath(path, vault).replace(os.sep, "/")
        fm["filename"] = filename
        fm["rel_path"] = rel_path[:-3] if rel_path.endswith(".md") else rel_path
        candidates.append(fm)
    candidates.sort(
        key=lambda item: (
            safe_int(item.get("seen_count")),
            str(item.get("last_seen") or ""),
            str(item.get("evidence_id") or ""),
        ),
        reverse=True,
    )
    return {"candidates": candidates}


def collect_annotation_candidates(vault, cfg):
    """Collect uncertain explicit tags for the visible approval index."""
    settings = cfg.get("annotation_quality") or {}
    candidate_dir = safe_vault_path(
        vault,
        settings.get("candidate_dir", "04-Feedback/_annotation-candidates"),
    )
    candidates = []
    if not os.path.isdir(candidate_dir):
        return {"candidates": candidates}
    for filename in sorted(os.listdir(candidate_dir)):
        if not filename.endswith(".md"):
            continue
        path = safe_vault_path(candidate_dir, filename)
        fm = read_existing_session(path)
        if (
            not isinstance(fm, dict)
            or fm.get("type") != "annotation-candidate"
            or fm.get("status") != "candidate"
        ):
            continue
        rel_path = os.path.relpath(path, vault).replace(os.sep, "/")
        fm["filename"] = filename
        fm["rel_path"] = rel_path[:-3] if rel_path.endswith(".md") else rel_path
        candidates.append(fm)
    candidates.sort(
        key=lambda item: (
            safe_int(item.get("seen_count")),
            float(item.get("quality_score") or 0),
            str(item.get("last_seen") or ""),
        ),
        reverse=True,
    )
    return {"candidates": candidates}


def safe_int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[harvester] WARNING: hook failed safely: {exc}")
        sys.exit(0)
