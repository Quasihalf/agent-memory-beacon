"""Classify bounded Codex failure evidence into non-recallable candidates."""
import hashlib
import json
import os
import re
import secrets
from datetime import date

import yaml
from memory_schema import canonical_project

from safety import (
    durable_atomic_write,
    durable_unlink,
    ensure_directory_tree,
    exclusive_file_lock,
    normalize_iso_date,
    normalize_project_slug,
    redact_sensitive,
    safe_vault_path,
    secure_read_bytes,
    split_frontmatter_text,
    strip_platform_injected_context,
)


DEFAULT_CANDIDATE_DIR = "04-Feedback/_error-candidates"
DEFAULT_EXCERPT_LIMIT = 500
DEFAULT_SOURCE_LIMIT = 20
MAX_EXCERPT_LIMIT = 2000
MAX_SOURCE_LIMIT = 100
MAX_OBSERVATIONS = 4096
INDEX_DIRTY_MARKER = ".index-dirty"
MAX_DIRTY_MARKER_BYTES = 4096
MAX_CANDIDATE_BYTES = 131_072
SCHEMA_VERSION = "2.0"
_OPERATION_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EVIDENCE_ID = re.compile(r"^error-evidence-[0-9a-f]{64}$")
_FORMAL_ERROR_TYPE = re.compile(r"^[a-z][a-z0-9_-]{0,79}$")
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_MATCH_TOKEN = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
ALLOWED_TOOL_OPERATIONS = frozenset(
    {
        "apply_patch",
        "exec",
        "exec_command",
        "function.exec_command",
        "functions.exec_command",
        "write_stdin",
    }
)


class ErrorEvidenceStateError(RuntimeError):
    """Raised when operational evidence state cannot be read safely."""


def process_error_evidence(cfg, parsed, formal_errors, project, session_id, date_str):
    """Persist unresolved observation candidates and close formal-error matches."""
    settings = error_evidence_settings(cfg)
    if not settings["enabled"]:
        return empty_result()

    vault = cfg["vault_path"]
    candidate_dir = safe_vault_path(vault, settings["candidate_dir"])
    observations = parsed.get("observations", []) if isinstance(parsed, dict) else []
    normalized_project = normalize_project_slug(project)
    if not normalized_project:
        return empty_result(ignored=len(observations) if isinstance(observations, list) else 1)
    candidates, ignored = classify_observations(observations, project, settings["excerpt_limit"])
    terminal_successes = terminal_success_operation_hashes(
        observations,
        settings["excerpt_limit"],
    )
    formal_errors = normalized_formal_errors(formal_errors, normalized_project)
    if not candidates and not formal_errors and not terminal_successes:
        return empty_result(ignored=ignored)
    if (
        terminal_successes
        and not candidates
        and not formal_errors
        and not os.path.isdir(candidate_dir)
    ):
        return empty_result(ignored=ignored)

    ensure_directory_tree(candidate_dir, vault)
    lock_path = safe_vault_path(candidate_dir, ".error-evidence.lock")
    with exclusive_file_lock(lock_path, root=vault):
        existing, blocked_ids = load_candidate_records(
            candidate_dir,
            source_limit=settings["source_limit"],
            excerpt_limit=settings["excerpt_limit"],
            vault_root=vault,
        )
        result = empty_result(ignored=ignored)
        dirty_generation = ""

        def ensure_index_dirty():
            nonlocal dirty_generation
            if not dirty_generation:
                dirty_generation = mark_index_dirty(candidate_dir, vault_root=vault)

        for evidence_id, candidate in candidates.items():
            if evidence_id in blocked_ids:
                result["ignored"] += 1
                continue
            current = existing.get(evidence_id)
            record, changed, source_added = merge_candidate(
                candidate,
                current,
                session_id,
                date_str,
                settings["source_limit"],
            )
            formal = matching_formal_error(record, formal_errors)
            became_resolved = False
            if formal and record.get("status") != "resolved":
                record["status"] = "resolved"
                record["formal_error_type"] = formal["type"]
                record["formal_error_ref"] = formal["reference"]
                became_resolved = True
                changed = True

            if changed:
                ensure_index_dirty()
                path = write_candidate_record(
                    candidate_dir,
                    record,
                    source_limit=settings["source_limit"],
                    excerpt_limit=settings["excerpt_limit"],
                    vault_root=vault,
                )
                existing[evidence_id] = dict(record, _path=path)
                if became_resolved:
                    result["resolved"] += 1
                    result["items"].append(result_item(record, "resolved", path, vault))
                elif current is None:
                    result["candidates"] += 1
                    result["items"].append(result_item(record, "candidate", path, vault))
                elif source_added:
                    result["updated"] += 1
                    result["items"].append(result_item(record, "updated", path, vault))

        for evidence_id, current in list(existing.items()):
            if (
                evidence_id in candidates
                or current.get("status") == "resolved"
                or normalize_project_slug(current.get("project")) != normalized_project
            ):
                continue
            formal = matching_formal_error(current, formal_errors)
            if not formal:
                continue
            record = dict(current)
            record.pop("_path", None)
            record["status"] = "resolved"
            record["formal_error_type"] = formal["type"]
            record["formal_error_ref"] = formal["reference"]
            ensure_index_dirty()
            path = write_candidate_record(
                candidate_dir,
                record,
                source_limit=settings["source_limit"],
                excerpt_limit=settings["excerpt_limit"],
                vault_root=vault,
            )
            existing[evidence_id] = dict(record, _path=path)
            result["resolved"] += 1
            result["items"].append(result_item(record, "resolved", path, vault))

        reconcile_terminal_successes(
            existing,
            terminal_successes,
            normalized_project,
            session_id,
            candidate_dir,
            vault,
            result,
            ensure_index_dirty,
            settings["source_limit"],
            settings["excerpt_limit"],
        )

        return result


def error_evidence_settings(cfg):
    raw = cfg.get("error_evidence") or {}
    settings = {
        "enabled": raw.get("enabled", True),
        "candidate_dir": raw.get("candidate_dir", DEFAULT_CANDIDATE_DIR),
        "excerpt_limit": raw.get("excerpt_limit", DEFAULT_EXCERPT_LIMIT),
        "source_limit": raw.get("source_limit", DEFAULT_SOURCE_LIMIT),
    }
    if (
        isinstance(settings["excerpt_limit"], bool)
        or not isinstance(settings["excerpt_limit"], int)
        or not 0 < settings["excerpt_limit"] <= MAX_EXCERPT_LIMIT
    ):
        raise ValueError("invalid error evidence excerpt limit")
    if (
        isinstance(settings["source_limit"], bool)
        or not isinstance(settings["source_limit"], int)
        or not 0 < settings["source_limit"] <= MAX_SOURCE_LIMIT
    ):
        raise ValueError("invalid error evidence source limit")
    return settings


def empty_result(ignored=0):
    return {"candidates": 0, "updated": 0, "resolved": 0, "ignored": ignored, "items": []}


def classify_observations(observations, project, excerpt_limit):
    if not isinstance(observations, list):
        return {}, 1
    if len(observations) > MAX_OBSERVATIONS:
        return {}, len(observations)
    normalized = []
    ignored = 0
    for position, observation in enumerate(observations):
        item = normalize_observation(observation, position, excerpt_limit)
        if item is None:
            ignored += 1
            continue
        normalized.append(item)
    normalized.sort(key=lambda item: item["event_index"])

    terminal_tool_results = {}
    for item in normalized:
        if item["kind"] == "tool_result":
            terminal_tool_results[item["operation_hash"]] = item

    candidates = {}
    normalized_project = normalize_project_slug(project)
    for item in normalized:
        if item["kind"] == "tool_result":
            if item["success"]:
                continue
            if terminal_tool_results.get(item["operation_hash"]) is not item:
                ignored += 1
                continue
            if terminal_tool_results[item["operation_hash"]]["success"]:
                ignored += 1
                continue
            kind = "tool_failure"
            severity = "error"
        else:
            kind = "review_finding"
            severity = item["severity"]

        evidence_id = evidence_id_for(
            normalized_project, kind, item["operation_hash"], item["excerpt"]
        )
        candidates.setdefault(
            evidence_id,
            {
                "evidence_id": evidence_id,
                "schema_version": SCHEMA_VERSION,
                "status": "candidate",
                "type": "error-evidence-candidate",
                "classification": "unresolved_finding",
                "project": normalized_project,
                "source_agent": "codex",
                "source_event": item["event_index"],
                "kind": kind,
                "operation": item["operation"],
                "operation_hash": item["operation_hash"],
                "severity": severity,
                "excerpt": item["excerpt"],
            },
        )
    return candidates, ignored


def terminal_success_operation_hashes(observations, excerpt_limit):
    if not isinstance(observations, list):
        return set()
    if len(observations) > MAX_OBSERVATIONS:
        return set()
    terminal = {}
    for position, observation in enumerate(observations):
        item = normalize_observation(observation, position, excerpt_limit)
        if item is not None and item["kind"] == "tool_result":
            terminal[item["operation_hash"]] = item
    return {
        operation_hash
        for operation_hash, item in terminal.items()
        if item["success"]
    }


def reconcile_terminal_successes(
    existing,
    terminal_successes,
    project,
    session_id,
    candidate_dir,
    vault,
    result,
    ensure_index_dirty,
    source_limit,
    excerpt_limit,
):
    session_id = normalize_source_session(session_id)
    if not session_id or not terminal_successes:
        return
    for evidence_id, current in list(existing.items()):
        if (
            current.get("status") != "candidate"
            or current.get("kind") != "tool_failure"
            or current.get("operation_hash") not in terminal_successes
            or normalize_project_slug(current.get("project")) != project
        ):
            continue
        sources = normalized_sources(current.get("sources"))
        if not any(source.get("session_id") == session_id for source in sources):
            continue
        record = {key: value for key, value in current.items() if key != "_path"}
        record["sources"] = [
            source
            for source in sources
            if source.get("session_id") != session_id
        ]
        record.pop("source_replay_hashes", None)
        record["seen_count"] = max(
            0,
            safe_nonnegative_int(record.get("seen_count")) - 1,
        )
        path = current.get("_path") or safe_vault_path(
            candidate_dir,
            f"{evidence_id}.md",
        )
        ensure_index_dirty()
        if record["seen_count"] == 0:
            durable_unlink(path, root=vault)
            existing.pop(evidence_id, None)
        else:
            path = write_candidate_record(
                candidate_dir,
                record,
                source_limit=source_limit,
                excerpt_limit=excerpt_limit,
                vault_root=vault,
            )
            existing[evidence_id] = dict(record, _path=path)
        result["updated"] += 1
        result["items"].append(result_item(record, "discarded", path, vault))


def mark_index_dirty(candidate_dir, vault_root=None):
    token = secrets.token_hex(32)
    marker = safe_vault_path(candidate_dir, INDEX_DIRTY_MARKER)
    atomic_write(marker, token + "\n", root=vault_root)
    return token


def error_evidence_dirty_token(cfg):
    settings = error_evidence_settings(cfg)
    try:
        candidate_dir = safe_vault_path(
            cfg["vault_path"],
            settings["candidate_dir"],
        )
        marker = safe_vault_path(candidate_dir, INDEX_DIRTY_MARKER)
    except ValueError as exc:
        raise ErrorEvidenceStateError(
            "cannot read error evidence dirty marker"
        ) from exc
    return read_dirty_marker_generation(marker, root=cfg["vault_path"])


def clear_error_evidence_dirty(cfg, expected_token):
    if not expected_token:
        return False
    settings = error_evidence_settings(cfg)
    candidate_dir = safe_vault_path(cfg["vault_path"], settings["candidate_dir"])
    if not os.path.isdir(candidate_dir):
        return False
    lock_path = safe_vault_path(candidate_dir, ".error-evidence.lock")
    marker = safe_vault_path(candidate_dir, INDEX_DIRTY_MARKER)
    with exclusive_file_lock(lock_path, root=cfg["vault_path"]):
        current = read_dirty_marker_generation(marker, root=cfg["vault_path"])
        if current != expected_token:
            return False
        durable_unlink(marker, root=cfg["vault_path"])
        return True


def read_dirty_marker_generation(marker, root=None):
    try:
        data = secure_read_bytes(
            marker,
            MAX_DIRTY_MARKER_BYTES,
            root=root,
        )
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise ErrorEvidenceStateError(
            "cannot read error evidence dirty marker"
        ) from exc
    if len(data) > MAX_DIRTY_MARKER_BYTES:
        raise ErrorEvidenceStateError("error evidence dirty marker is oversized")
    try:
        token = data.decode("ascii").strip()
    except UnicodeDecodeError:
        token = ""
    if re.fullmatch(r"[0-9a-f]{64}", token):
        return token
    return "malformed-" + hashlib.sha256(data).hexdigest()


def normalize_observation(observation, fallback_index, excerpt_limit):
    if not isinstance(observation, dict):
        return None
    kind = observation.get("kind")
    operation = str(observation.get("operation") or "")
    operation_hash = str(observation.get("operation_hash") or "").strip()
    if not _OPERATION_NAME.fullmatch(operation) or not _SHA256.fullmatch(operation_hash):
        return None
    event_index = observation.get("event_index", fallback_index)
    if isinstance(event_index, bool) or not isinstance(event_index, int) or event_index < 0:
        return None

    if kind == "tool_result":
        if operation not in ALLOWED_TOOL_OPERATIONS:
            return None
        success = observation.get("success")
        if not isinstance(success, bool):
            return None
        excerpt = normalize_excerpt(observation.get("excerpt", ""), excerpt_limit)
        if not success and not excerpt:
            return None
        return {
            "event_index": event_index,
            "kind": kind,
            "operation": operation,
            "operation_hash": operation_hash,
            "success": success,
            "excerpt": excerpt,
        }

    if kind == "review_finding":
        if operation != "subagent_review":
            return None
        severity = str(observation.get("severity") or "").lower()
        excerpt = normalize_excerpt(observation.get("excerpt", ""), excerpt_limit)
        if severity not in {"critical", "important"} or not excerpt:
            return None
        return {
            "event_index": event_index,
            "kind": kind,
            "operation": operation,
            "operation_hash": operation_hash,
            "severity": severity,
            "excerpt": excerpt,
        }
    return None


def normalize_excerpt(value, limit):
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        return ""
    limit = min(limit, MAX_EXCERPT_LIMIT)
    raw = str(value or "")
    text = strip_platform_injected_context(
        raw[: max(2048, min(limit * 4, 8192))]
    )
    text = redact_sensitive(text)
    text = _CONTROL_CHARS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def evidence_id_for(project, kind, operation_hash, excerpt):
    payload = {
        "project": normalize_project_slug(project),
        "kind": str(kind),
        "operation_hash": str(operation_hash),
        "excerpt": normalize_for_identity(excerpt),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"error-evidence-{digest}"


def normalize_for_identity(text):
    text = _CONTROL_CHARS.sub(" ", str(text or "")).lower()
    return re.sub(r"\s+", " ", text).strip()


def merge_candidate(candidate, existing, session_id, date_str, source_limit):
    session_id = normalize_source_session(session_id)
    source = {"session_id": session_id, "date": normalize_iso_date(date_str)}
    if not existing:
        record = dict(candidate)
        record.update(
            {
                "first_seen": source["date"],
                "last_seen": source["date"],
                "seen_count": 1,
                "sources": [source] if session_id else [],
            }
        )
        return record, True, bool(session_id)

    record = {key: value for key, value in existing.items() if key != "_path"}
    sources = normalized_sources(record.get("sources"))
    known_source = any(source.get("session_id") == session_id for source in sources)
    if known_source or not session_id:
        return record, False, False
    sources.append(source)
    record["sources"] = sources[-source_limit:]
    record.pop("source_replay_hashes", None)
    record.pop("source_filter", None)
    record["seen_count"] = safe_nonnegative_int(record.get("seen_count")) + 1
    record["last_seen"] = source["date"]
    return record, True, True


def normalize_source_session(value):
    text = _CONTROL_CHARS.sub("", str(value or "")).strip()
    return redact_sensitive(text)[:200]


def normalized_sources(raw_sources):
    sources = []
    for source in raw_sources or []:
        if not isinstance(source, dict):
            continue
        session_id = normalize_source_session(source.get("session_id"))
        if session_id:
            normalized = {
                "session_id": session_id,
                "date": normalize_iso_date(source.get("date")),
            }
            if normalized not in sources:
                sources.append(normalized)
    return sources


def safe_nonnegative_int(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def load_candidate_records(
    candidate_dir,
    source_limit=DEFAULT_SOURCE_LIMIT,
    excerpt_limit=DEFAULT_EXCERPT_LIMIT,
    vault_root=None,
    fail_on_invalid=False,
):
    records = {}
    blocked_ids = set()
    if not os.path.isdir(candidate_dir):
        return records, blocked_ids
    for filename in sorted(os.listdir(candidate_dir)):
        if not filename.endswith(".md"):
            continue
        filename_id = filename[:-3]
        if not _EVIDENCE_ID.fullmatch(filename_id):
            continue
        path = os.path.join(candidate_dir, filename)
        try:
            record = validate_candidate_record(
                read_frontmatter(path, vault_root=vault_root),
                source_limit=source_limit,
                excerpt_limit=excerpt_limit,
            )
            evidence_id = record["evidence_id"]
            if filename != f"{evidence_id}.md":
                raise ValueError("candidate filename does not match evidence ID")
        except (ErrorEvidenceStateError, ValueError) as exc:
            if fail_on_invalid:
                raise ErrorEvidenceStateError(
                    f"invalid error evidence candidate: {filename}"
                ) from exc
            blocked_ids.add(filename_id)
            continue
        record["_path"] = path
        records[evidence_id] = record
    return records, blocked_ids


def read_frontmatter(path, vault_root=None):
    try:
        content_bytes = secure_read_bytes(
            path,
            MAX_CANDIDATE_BYTES,
            root=vault_root,
        )
        content = content_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ErrorEvidenceStateError("candidate cannot be read safely") from exc
    if len(content) > MAX_CANDIDATE_BYTES:
        raise ErrorEvidenceStateError("candidate is oversized")
    frontmatter, _body = split_frontmatter_text(content)
    if frontmatter is None:
        raise ValueError("candidate frontmatter is missing")
    try:
        value = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError as exc:
        raise ValueError("candidate frontmatter is invalid YAML") from exc
    if not isinstance(value, dict):
        raise ValueError("candidate frontmatter is not an object")
    return value


def write_candidate_record(
    candidate_dir,
    record,
    source_limit=DEFAULT_SOURCE_LIMIT,
    excerpt_limit=DEFAULT_EXCERPT_LIMIT,
    vault_root=None,
):
    evidence_id = str(record.get("evidence_id") or "")
    if not _EVIDENCE_ID.fullmatch(evidence_id):
        raise ValueError("invalid error evidence ID")
    path = safe_vault_path(candidate_dir, f"{evidence_id}.md")
    record = sanitize_record(record)
    validate_candidate_record(
        record,
        source_limit=source_limit,
        excerpt_limit=excerpt_limit,
    )
    body = "# Error Evidence Candidate\n\n## Evidence\n\n" + record["excerpt"] + "\n"
    content = "---\n" + yaml.safe_dump(
        record, allow_unicode=True, default_flow_style=False, sort_keys=False
    ) + "---\n\n" + body
    atomic_write(path, content, root=vault_root)
    return path


def validate_candidate_record(
    record,
    source_limit=DEFAULT_SOURCE_LIMIT,
    excerpt_limit=DEFAULT_EXCERPT_LIMIT,
):
    if not isinstance(record, dict):
        raise ValueError("candidate must be an object")
    status = record.get("status")
    base_fields = {
        "evidence_id",
        "schema_version",
        "status",
        "type",
        "classification",
        "project",
        "source_agent",
        "source_event",
        "kind",
        "operation",
        "operation_hash",
        "severity",
        "excerpt",
        "first_seen",
        "last_seen",
        "seen_count",
        "sources",
    }
    expected_fields = set(base_fields)
    if status == "resolved":
        expected_fields.update({"formal_error_type", "formal_error_ref"})
    if set(record) != expected_fields:
        raise ValueError("candidate fields do not match the managed schema")
    if record.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("candidate schema version is invalid")
    if status not in {"candidate", "resolved"}:
        raise ValueError("candidate status is invalid")
    if record.get("type") != "error-evidence-candidate":
        raise ValueError("candidate type is invalid")
    if record.get("classification") != "unresolved_finding":
        raise ValueError("candidate classification is invalid")

    project = str(record.get("project") or "")
    if not project or normalize_project_slug(project) != project:
        raise ValueError("candidate project is invalid")
    if record.get("source_agent") != "codex":
        raise ValueError("candidate source agent is invalid")
    source_event = record.get("source_event")
    if isinstance(source_event, bool) or not isinstance(source_event, int) or source_event < 0:
        raise ValueError("candidate source event is invalid")

    kind = record.get("kind")
    operation = str(record.get("operation") or "")
    severity = record.get("severity")
    if kind == "tool_failure":
        if operation not in ALLOWED_TOOL_OPERATIONS or severity != "error":
            raise ValueError("candidate tool identity is invalid")
    elif kind == "review_finding":
        if operation != "subagent_review" or severity not in {"critical", "important"}:
            raise ValueError("candidate review identity is invalid")
    else:
        raise ValueError("candidate kind is invalid")

    operation_hash = str(record.get("operation_hash") or "")
    if not _SHA256.fullmatch(operation_hash):
        raise ValueError("candidate operation hash is invalid")
    excerpt = str(record.get("excerpt") or "")
    if (
        isinstance(excerpt_limit, bool)
        or not isinstance(excerpt_limit, int)
        or not 0 < excerpt_limit <= MAX_EXCERPT_LIMIT
        or not excerpt
        or normalize_excerpt(excerpt, excerpt_limit) != excerpt
    ):
        raise ValueError("candidate excerpt is invalid")
    evidence_id = str(record.get("evidence_id") or "")
    if evidence_id != evidence_id_for(project, kind, operation_hash, excerpt):
        raise ValueError("candidate evidence ID is invalid")

    first_seen = _strict_iso_date(record.get("first_seen"))
    last_seen = _strict_iso_date(record.get("last_seen"))
    if last_seen < first_seen:
        raise ValueError("candidate dates are out of order")
    seen_count = record.get("seen_count")
    if isinstance(seen_count, bool) or not isinstance(seen_count, int) or seen_count <= 0:
        raise ValueError("candidate seen count is invalid")
    if (
        isinstance(source_limit, bool)
        or not isinstance(source_limit, int)
        or not 0 < source_limit <= MAX_SOURCE_LIMIT
    ):
        raise ValueError("candidate source limit is invalid")
    sources = record.get("sources")
    if not isinstance(sources, list) or len(sources) > source_limit:
        raise ValueError("candidate sources are invalid")
    if normalized_sources(sources) != sources:
        raise ValueError("candidate sources are not canonical")
    if any(set(source) != {"session_id", "date"} for source in sources):
        raise ValueError("candidate source fields are invalid")

    if status == "resolved":
        formal_type = str(record.get("formal_error_type") or "")
        formal_ref = str(record.get("formal_error_ref") or "")
        if normalize_formal_error_type(formal_type) != formal_type:
            raise ValueError("candidate formal error type is invalid")
        if not formal_ref or normalize_excerpt(formal_ref, 200) != formal_ref:
            raise ValueError("candidate formal error reference is invalid")
    return record


def _strict_iso_date(value):
    text = str(value or "")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("candidate date is invalid") from exc
    if parsed.isoformat() != text:
        raise ValueError("candidate date is not canonical")
    return parsed


def sanitize_record(record):
    cleaned = {key: value for key, value in record.items() if key != "_path"}
    # Hashes and session identifiers are opaque identity fields. Redacting them
    # can mutate a SHA-256 substring that happens to pass a card-number check.
    cleaned["excerpt"] = normalize_excerpt(cleaned.get("excerpt", ""), DEFAULT_EXCERPT_LIMIT)
    cleaned.pop("source_filter", None)
    cleaned.pop("source_replay_hashes", None)
    if "formal_error_type" in cleaned:
        formal_type = normalize_formal_error_type(cleaned.get("formal_error_type"))
        if formal_type:
            cleaned["formal_error_type"] = formal_type
        else:
            cleaned.pop("formal_error_type", None)
            cleaned.pop("formal_error_ref", None)
    if cleaned.get("formal_error_ref"):
        cleaned["formal_error_ref"] = normalize_excerpt(
            cleaned["formal_error_ref"],
            200,
        )
    cleaned["sources"] = normalized_sources(cleaned.get("sources"))
    return cleaned


def atomic_write(path, content, root=None):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    durable_atomic_write(path, content, root=root)


def normalized_formal_errors(formal_errors, project):
    normalized = []
    for error in formal_errors or []:
        if not isinstance(error, dict):
            continue
        raw_project = str(error.get("project") or "").strip()
        explicit_project = canonical_project(raw_project)
        if raw_project and not explicit_project:
            continue
        if explicit_project and explicit_project != project:
            continue
        error_type = normalize_formal_error_type(error.get("type"))
        resolution = normalize_excerpt(error.get("resolution", ""), DEFAULT_EXCERPT_LIMIT)
        operation_hash = str(error.get("operation_hash") or "").strip()
        if not _FORMAL_ERROR_TYPE.fullmatch(error_type) or not resolution:
            continue
        normalized.append(
            {
                "type": error_type,
                "resolution": resolution,
                "operation_hash": operation_hash,
                "reference": normalize_excerpt(
                    error.get("id") or f"type:{error_type}",
                    200,
                ),
            }
        )
    return normalized


def normalize_formal_error_type(value):
    normalized = normalize_for_identity(value)
    if redact_sensitive(normalized) != normalized:
        return ""
    return normalized if _FORMAL_ERROR_TYPE.fullmatch(normalized) else ""


def matching_formal_error(record, formal_errors):
    excerpt_tokens = token_set(record.get("excerpt", ""))
    diagnostic_categories = set(
        re.findall(
            r"diagnostic=([a-z_]+)",
            str(record.get("excerpt") or "").lower(),
        )
    )
    for formal in formal_errors:
        if formal["operation_hash"] and formal["operation_hash"] == record.get("operation_hash"):
            return formal
        normalized_resolution = normalize_for_identity(formal["resolution"])
        if any(
            category.replace("_", " ") in normalized_resolution
            for category in diagnostic_categories
        ):
            return formal
        resolution_tokens = token_set(formal["resolution"])
        overlap = excerpt_tokens & resolution_tokens
        if len(overlap) >= 3 and len(overlap) / min(len(excerpt_tokens), len(resolution_tokens)) >= 0.5:
            return formal
    return None


def token_set(value):
    return set(_MATCH_TOKEN.findall(normalize_for_identity(value)))


def result_item(record, action, path, vault):
    return {
        "action": action,
        "evidence_id": record.get("evidence_id", ""),
        "severity": record.get("severity", ""),
        "path": os.path.relpath(path, vault).replace(os.sep, "/"),
    }
