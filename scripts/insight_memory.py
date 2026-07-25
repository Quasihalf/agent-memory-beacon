#!/usr/bin/env python3
"""Source-grounded generative Insight learning for Codex transcripts."""
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone

import yaml

from memory_authority import render_authority_markdown_lines
from memory_lifecycle import create_proposal
from memory_schema import (
    RUNTIME_SCHEMA_VERSION,
    active_formal_lifecycle_metadata,
    canonical_project,
    formal_memory_update_allowed,
    normalize_formal_record,
    parse_formal_section,
    upgrade_formal_note_frontmatter,
)
from safety import (
    durable_atomic_write,
    ensure_directory_tree,
    redact_sensitive,
    safe_filename,
    safe_vault_path,
    secure_read_bytes,
    split_frontmatter_text,
    strip_markdown_code_blocks,
    strip_platform_injected_context,
)


CST = timezone(timedelta(hours=8))
LEARN_PATTERN = re.compile(
    r"^\s*\[LEARN:\s*(.*?)\]\s*$",
    re.MULTILINE | re.IGNORECASE,
)
DEFAULT_CANDIDATE_DIR = "04-Feedback/_insight-candidates"
DEFAULT_FORMAL_PATH = "05-Agent-Memory/insights.md"
DEFAULT_SIMILARITY_THRESHOLD = 0.58
DEFAULT_DIRECT_SEED_THRESHOLD = 0.72
DEFAULT_REINFORCE_SOURCE_COUNT = 2
MAX_FORMAL_BYTES = 8 * 1024 * 1024
MAX_CANDIDATE_BYTES = 1024 * 1024
MAX_SOURCE_REFS = 20
VALID_ORIGINS = {"user", "jointly_validated"}
CORE_FIELDS = ("summary", "novelty", "transfer", "boundary", "project", "scope")


def process_insight_memory(cfg, parsed, project, session_id, date_str):
    """Persist new Insight candidates, seeds, and evidence-only reinforcement."""
    settings = insight_memory_settings(cfg)
    result = empty_result()
    if not settings["enabled"]:
        return result
    candidates = extract_learn_annotations(
        parsed.get("messages", []),
        parsed.get("context_messages", []),
        project,
    )
    if not candidates:
        return result

    vault = os.path.abspath(os.path.expanduser(str(cfg.get("vault_path") or "")))
    if not vault or not os.path.isdir(vault):
        raise ValueError("insight memory requires a configured Vault")
    candidate_dir = safe_vault_path(vault, settings["candidate_dir"])
    formal_path = safe_vault_path(vault, settings["formal_path"])
    ensure_directory_tree(candidate_dir, vault)
    ensure_directory_tree(os.path.dirname(formal_path), vault)
    formal_records = load_formal_records(formal_path, vault)
    seen_annotations = set()

    for candidate in candidates:
        annotation_key = (
            candidate["memory_id"],
            candidate.get("evidence", ""),
            candidate.get("boundary", ""),
        )
        if annotation_key in seen_annotations:
            continue
        seen_annotations.add(annotation_key)
        candidate["confidence"] = admission_score(candidate)
        match = find_formal_match(
            candidate,
            formal_records,
            settings["similarity_threshold"],
        )
        source_ref = f"session:{str(session_id or '').strip()}"

        if match:
            if source_ref in (match.get("source_refs") or []):
                continue
            if not qualifies_as_seed(candidate, settings["direct_seed_threshold"]):
                candidate_path, changed = write_candidate(
                    candidate_dir,
                    candidate,
                    session_id,
                    date_str,
                    vault,
                    reasons=candidate_reasons(
                        candidate,
                        settings["direct_seed_threshold"],
                    ),
                )
                if changed:
                    result["candidates"] += 1
                    result["items"].append(
                        result_item(candidate, "candidate", candidate_path, vault)
                    )
                continue
            if core_conflicts(match, candidate):
                candidate_path, changed = write_candidate(
                    candidate_dir,
                    candidate,
                    session_id,
                    date_str,
                    vault,
                    reasons=["formal_core_conflict"],
                )
                if changed:
                    result["candidates"] += 1
                    result["items"].append(
                        result_item(candidate, "candidate", candidate_path, vault)
                    )
                proposal_path = create_proposal(
                    cfg,
                    action="retract",
                    memory_id=match["id"],
                    expected_revision=match["revision"],
                    reason="相似 Insight 的核心含义或适用边界出现冲突，需人工确认",
                    evidence_refs=[
                        f"candidate:{os.path.relpath(candidate_path, vault).replace(os.sep, '/')}",
                        source_ref,
                    ],
                )
                result["proposals"] += 1
                result["items"].append(
                    result_item(
                        candidate,
                        "proposal",
                        proposal_path,
                        vault,
                    )
                )
                continue

            reinforced = reinforce_formal_record(
                match,
                source_ref,
                candidate["confidence"],
                settings["reinforce_source_count"],
            )
            if upsert_formal_record(formal_path, reinforced, vault):
                action = (
                    "reinforced"
                    if match.get("maturity") != reinforced.get("maturity")
                    else "updated"
                )
                result[action] += 1
                result["formal"] += 1
                result["items"].append(
                    result_item(reinforced, action, formal_path, vault)
                )
                replace_record(formal_records, reinforced)
            continue

        if qualifies_as_seed(candidate, settings["direct_seed_threshold"]):
            record = formal_record_from_candidate(
                candidate,
                formal_path,
                vault,
                source_ref,
            )
            if upsert_formal_record(formal_path, record, vault):
                result["seeds"] += 1
                result["formal"] += 1
                result["items"].append(
                    result_item(record, "seed", formal_path, vault)
                )
                formal_records.append(record)
                promoted_path = mark_candidate_promoted(
                    candidate_dir,
                    candidate["memory_id"],
                    record["id"],
                    vault,
                )
                if promoted_path:
                    result["updated"] += 1
                    result["items"].append(
                        result_item(
                            record,
                            "candidate_promoted",
                            promoted_path,
                            vault,
                        )
                    )
            continue

        reasons = candidate_reasons(candidate, settings["direct_seed_threshold"])
        candidate_path, changed = write_candidate(
            candidate_dir,
            candidate,
            session_id,
            date_str,
            vault,
            reasons=reasons,
        )
        if changed:
            result["candidates"] += 1
            result["items"].append(
                result_item(candidate, "candidate", candidate_path, vault)
            )
    return result


def insight_memory_settings(cfg):
    raw = cfg.get("insight_memory") or {}
    return {
        "enabled": raw.get("enabled", True),
        "candidate_dir": raw.get("candidate_dir", DEFAULT_CANDIDATE_DIR),
        "formal_path": raw.get("formal_path", DEFAULT_FORMAL_PATH),
        "similarity_threshold": float(
            raw.get("similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD)
        ),
        "direct_seed_threshold": float(
            raw.get("direct_seed_threshold", DEFAULT_DIRECT_SEED_THRESHOLD)
        ),
        "reinforce_source_count": int(
            raw.get("reinforce_source_count", DEFAULT_REINFORCE_SOURCE_COUNT)
        ),
    }


def empty_result():
    return {
        "candidates": 0,
        "seeds": 0,
        "reinforced": 0,
        "formal": 0,
        "updated": 0,
        "proposals": 0,
        "items": [],
    }


def extract_learn_annotations(messages, context_messages, default_project):
    """Extract assistant annotations and ground user evidence before admission."""
    grounded_user_messages = [
        strip_platform_injected_context(item.get("text", ""))
        for item in context_messages or []
        if item.get("role") == "user"
    ]
    candidates = []
    for message in messages or []:
        role = str(message.get("role") or "").strip().lower()
        text = str(message.get("text") or "")
        if role == "user":
            grounded_user_messages.append(strip_platform_injected_context(text))
            continue
        if role != "assistant":
            continue
        for raw in LEARN_PATTERN.findall(strip_markdown_code_blocks(text)):
            candidate = parse_learn_annotation(
                raw,
                grounded_user_messages,
                default_project,
            )
            if candidate:
                candidates.append(candidate)
    return candidates


def parse_learn_annotation(raw, user_messages, default_project):
    parts = [
        part.strip()
        for part in re.split(
            r"\|\s*(?=[A-Za-z_][A-Za-z0-9_-]*\s*[:=])",
            str(raw or ""),
        )
    ]
    principle = one_line(parts[0] if parts else "")
    fields = {}
    for part in parts[1:]:
        match = re.match(
            r"^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*[:=]\s*(.*?)\s*$",
            part,
            re.DOTALL,
        )
        if match:
            fields[match.group(1).lower()] = one_line(match.group(2))
    if not principle or len(normalize_text(principle)) < 8:
        return None
    origin = fields.get("source", "").lower()
    if origin not in VALID_ORIGINS:
        return None
    novelty = fields.get("novelty", "")
    transfer = split_values(fields.get("transfer", ""))
    boundary = fields.get("boundary", "")
    evidence = fields.get("evidence", "")
    values = [principle, novelty, *transfer, boundary, evidence]
    redacted = [redact_sensitive(value) for value in values]
    if any(original != safe for original, safe in zip(values, redacted)):
        return None
    evidence_verified = origin == "user" and evidence_in_messages(
        evidence,
        user_messages,
    )
    project = canonical_project(fields.get("project") or default_project)
    scope = fields.get("scope", "project" if project else "global").lower()
    if scope not in {"project", "global"}:
        return None
    if scope == "global":
        project = ""
    elif not project:
        return None
    memory_id = insight_id(principle, project, scope)
    return {
        "memory_id": memory_id,
        "id": memory_id,
        "type": "insight",
        "status": "candidate",
        "maturity": "seed",
        "project": project,
        "scope": scope,
        "title": make_title(principle),
        "summary": principle,
        "novelty": novelty,
        "transfer": transfer,
        "boundary": boundary,
        "origin": origin,
        "evidence": evidence,
        "evidence_verified": evidence_verified,
        "supports": split_memory_ids(fields.get("supports", "")),
        "operationalized_as": split_memory_ids(
            fields.get("operationalized_as", "")
        ),
        "related_to": split_memory_ids(fields.get("related_to", "")),
    }


def admission_score(candidate):
    score = 0.14 if candidate.get("summary") else 0.0
    score += 0.28 if candidate.get("evidence_verified") else 0.0
    score += 0.18 if candidate.get("novelty") else 0.0
    score += 0.22 if candidate.get("transfer") else 0.0
    score += 0.18 if candidate.get("boundary") else 0.0
    return round(min(score, 1.0), 2)


def qualifies_as_seed(candidate, threshold):
    return bool(
        candidate.get("origin") == "user"
        and candidate.get("evidence_verified")
        and candidate.get("novelty")
        and candidate.get("transfer")
        and candidate.get("boundary")
        and float(candidate.get("confidence") or 0) >= float(threshold)
    )


def candidate_reasons(candidate, threshold):
    reasons = []
    if not candidate.get("evidence_verified"):
        reasons.append("unverified_evidence")
    for key in ("novelty", "transfer", "boundary"):
        if not candidate.get(key):
            reasons.append(f"missing_{key}")
    if float(candidate.get("confidence") or 0) < float(threshold):
        reasons.append("below_seed_threshold")
    return reasons or ["uncertain"]


def formal_record_from_candidate(candidate, formal_path, vault, source_ref):
    relative = os.path.relpath(formal_path, vault).replace(os.sep, "/")
    relative = relative.removesuffix(".md")
    return normalize_formal_record(
        {
            **candidate,
            "id": candidate["memory_id"],
            "status": "active",
            "maturity": "seed",
            "confidence": float(candidate["confidence"]),
            "source_refs": [source_ref],
            "path": relative,
            "source_note": f"note:{relative}",
        },
        memory_type="insight",
        default_project=candidate.get("project", ""),
        source_ref=source_ref,
    )


def reinforce_formal_record(record, source_ref, candidate_confidence, threshold):
    updated = dict(record)
    refs = list(updated.get("source_refs") or [])
    if source_ref not in refs:
        refs.append(source_ref)
    refs = list(dict.fromkeys(refs))[-MAX_SOURCE_REFS:]
    source_count = len([item for item in refs if str(item).startswith("session:")])
    updated["source_refs"] = sorted(refs)
    updated["maturity"] = (
        "reinforced" if source_count >= int(threshold) else "seed"
    )
    updated["confidence"] = round(
        min(
            0.95,
            max(
                float(updated.get("confidence") or 0),
                float(candidate_confidence or 0),
            )
            + 0.08,
        ),
        2,
    )
    normalized = normalize_formal_record(
        updated,
        memory_type="insight",
        default_project=updated.get("project", ""),
        source_ref="",
    )
    for key in ("path", "source_note"):
        if record.get(key):
            normalized[key] = record[key]
    return normalized


def load_formal_records(path, vault):
    if not os.path.exists(path):
        return []
    content = read_text(path, vault, MAX_FORMAL_BYTES)
    _frontmatter, body = split_frontmatter_text(content)
    if body is None:
        return []
    records = []
    headings = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", body))
    relative = os.path.relpath(path, vault).replace(os.sep, "/").removesuffix(".md")
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(body)
        section = body[heading.end():end].strip()
        record = parse_formal_section(heading.group(1).strip(), section, "insight")
        if not record:
            continue
        record["path"] = relative
        record["source_note"] = f"note:{relative}"
        records.append(record)
    return records


def find_formal_match(candidate, records, threshold):
    for record in records:
        if record.get("id") == candidate.get("memory_id"):
            return record
    best = None
    best_score = 0.0
    for record in records:
        if (record.get("project"), record.get("scope")) != (
            candidate.get("project"),
            candidate.get("scope"),
        ):
            continue
        score = text_similarity(record.get("summary"), candidate.get("summary"))
        if score > best_score:
            best = record
            best_score = score
    return best if best and best_score >= float(threshold) else None


def core_conflicts(existing, proposed):
    if existing.get("id") == proposed.get("memory_id"):
        return any(
            normalize_value(existing.get(key)) != normalize_value(proposed.get(key))
            for key in ("novelty", "transfer", "boundary", "project", "scope")
        )
    return not (
        text_similarity(existing.get("boundary"), proposed.get("boundary")) >= 0.8
        and set(normalize_value(existing.get("transfer")))
        == set(normalize_value(proposed.get("transfer")))
    )


def upsert_formal_record(path, record, vault):
    existing = ""
    if os.path.exists(path):
        existing = read_text(path, vault, MAX_FORMAL_BYTES)
        if not formal_memory_update_allowed(existing, record.get("id")):
            return False
    if not existing.strip():
        existing = initial_formal_note()
    before_upgrade = existing
    existing = upgrade_formal_note_frontmatter(
        existing,
        {
            "title": "Insights",
            "generated_by": "insight_memory.py",
            "summary_type": "insights",
        },
    )
    upgraded = existing != before_upgrade
    has_record = f"- id: `{record['id']}`" in existing
    lifecycle = {}
    if has_record:
        lifecycle = active_formal_lifecycle_metadata(existing, record["id"], "insight")
        if lifecycle is None:
            return False
    entry = render_formal_record(record, lifecycle)
    if has_record:
        updated = replace_formal_entry(existing, record["id"], entry)
        if updated == existing and not upgraded:
            return False
    else:
        updated = existing.rstrip() + "\n\n" + entry.rstrip() + "\n"
    durable_atomic_write(path, updated.rstrip() + "\n", root=vault)
    return True


def initial_formal_note():
    return (
        "---\n"
        "title: Insights\n"
        "generated_by: insight_memory.py\n"
        "summary_type: insights\n"
        f"schema_version: '{RUNTIME_SCHEMA_VERSION}'\n"
        "---\n\n"
        "# Insights\n\n"
        "Source-grounded reusable ideas. Seeds are inspiration, not authority.\n\n"
        "## Related\n\n"
        "- [[00-Inbox/Agent Memory Index|Agent Memory Index]]\n"
        "- [[05-Agent-Memory/workflow-rules|Workflow Rules]]\n"
        "- [[03-Maps/topic-index|Topic Index]]\n"
    )


def render_formal_record(record, lifecycle=None):
    raw = {**record, **dict(lifecycle or {})}
    formal = normalize_formal_record(
        raw,
        memory_type="insight",
        default_project=record.get("project", ""),
        source_ref="",
    )
    lines = [
        f"## {formal['title']}",
        "",
        f"- id: `{formal['id']}`",
        f"- revision: `{formal['revision']}`",
        "- status: `active`",
    ]
    if formal.get("requires"):
        lines.append("- requires: " + render_code_list(formal["requires"]))
    if formal.get("expires_at"):
        lines.append(f"- expires_at: `{formal['expires_at']}`")
    lines.extend(render_authority_markdown_lines(formal))
    lines.extend(
        [
            f"- scope: `{formal['scope']}`",
            f"- maturity: `{formal['maturity']}`",
            f"- confidence: `{formal['confidence']:.2f}`",
            f"- origin: `{formal['origin']}`",
            f"- project: {project_link(formal.get('project', ''))}",
            f"- source_refs: {render_code_list(formal['source_refs'])}",
        ]
    )
    for key in ("supports", "operationalized_as", "related_to"):
        if formal.get(key):
            lines.append(f"- {key}: {render_code_list(formal[key])}")
    lines.extend(["", "### Insight", "", formal["summary"]])
    lines.extend(["", "### Novelty", "", formal["novelty"]])
    lines.extend(["", "### Transfer", ""])
    lines.extend(f"- {item}" for item in formal["transfer"])
    lines.extend(["", "### Boundary", "", formal["boundary"]])
    return "\n".join(lines).rstrip() + "\n"


def replace_formal_entry(content, memory_id, new_entry):
    headings = list(re.finditer(r"(?m)^##\s+.+$", content))
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
        section = content[heading.start():end]
        if f"- id: `{memory_id}`" not in section:
            continue
        return (
            content[:heading.start()].rstrip()
            + "\n\n"
            + new_entry.rstrip()
            + "\n"
            + content[end:].lstrip("\n")
        )
    return content


def write_candidate(directory, candidate, session_id, date_str, vault, reasons):
    path = candidate_path(directory, candidate["memory_id"])
    existing = read_candidate(path, vault)
    source_ref = f"session:{str(session_id or '').strip()}"
    refs = list(existing.get("source_refs") or [])
    if source_ref in refs:
        return path, False
    refs.append(source_ref)
    refs = list(dict.fromkeys(refs))[-MAX_SOURCE_REFS:]
    first_seen = existing.get("first_seen") or datetime.now(CST).isoformat()
    payload = {
        "schema_version": "1.0",
        "type": "insight-candidate",
        "status": "candidate",
        "candidate_id": candidate["memory_id"],
        "project": candidate.get("project", ""),
        "scope": candidate.get("scope", "global"),
        "confidence": float(candidate.get("confidence") or 0),
        "source_refs": refs,
        "seen_count": len(refs),
        "first_seen": first_seen,
        "last_seen": datetime.now(CST).isoformat(),
        "date": str(date_str or ""),
        "quality_reasons": sorted(set(reasons)),
        "revision": candidate_revision(candidate, refs, reasons),
    }
    body = "\n".join(
        [
            f"# 待确认启发: {candidate['title']}",
            "",
            "该文件是候选，不会进入 Agent 运行时召回。",
            "",
            "## Insight",
            "",
            candidate.get("summary", ""),
            "",
            "## Novelty",
            "",
            candidate.get("novelty", "") or "-",
            "",
            "## Transfer",
            "",
            *(f"- {item}" for item in candidate.get("transfer") or []),
            "",
            "## Boundary",
            "",
            candidate.get("boundary", "") or "-",
        ]
    ).rstrip() + "\n"
    rendered = (
        "---\n"
        + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + body
    )
    durable_atomic_write(path, rendered, root=vault)
    return path, True


def mark_candidate_promoted(directory, memory_id, promoted_to, vault):
    """Keep candidate evidence while removing a completed item from the inbox."""
    path = candidate_path(directory, memory_id)
    if not os.path.exists(path):
        return ""
    content = read_text(path, vault, MAX_CANDIDATE_BYTES)
    frontmatter, body = split_frontmatter_text(content)
    if frontmatter is None or body is None:
        return ""
    try:
        payload = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError:
        return ""
    if (
        not isinstance(payload, dict)
        or payload.get("type") != "insight-candidate"
        or payload.get("candidate_id") != memory_id
    ):
        return ""
    if payload.get("status") == "promoted" and payload.get("promoted_to") == promoted_to:
        return ""
    payload["status"] = "promoted"
    payload["promoted_to"] = promoted_to
    payload["promoted_at"] = datetime.now(CST).isoformat()
    rendered = (
        "---\n"
        + yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)
        + "---\n\n"
        + body.lstrip("\n")
    )
    durable_atomic_write(path, rendered.rstrip() + "\n", root=vault)
    return path


def candidate_path(directory, memory_id):
    return safe_vault_path(
        directory,
        safe_filename(memory_id, default="insight-candidate", max_length=100)
        + ".md",
    )


def read_candidate(path, vault):
    if not os.path.exists(path):
        return {}
    content = read_text(path, vault, MAX_CANDIDATE_BYTES)
    frontmatter, _body = split_frontmatter_text(content)
    if frontmatter is None:
        return {}
    try:
        payload = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError:
        return {}
    if not isinstance(payload, dict) or payload.get("type") != "insight-candidate":
        return {}
    return payload


def candidate_revision(candidate, refs, reasons):
    return hashlib.sha256(
        json.dumps(
            {
                "id": candidate.get("memory_id"),
                "summary": candidate.get("summary"),
                "novelty": candidate.get("novelty"),
                "transfer": candidate.get("transfer") or [],
                "boundary": candidate.get("boundary"),
                "source_refs": refs,
                "reasons": sorted(set(reasons)),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def read_text(path, vault, limit):
    data = secure_read_bytes(path, limit + 1, root=vault)
    if len(data) > limit:
        raise ValueError("insight memory file exceeds size limit")
    return data.decode("utf-8")


def evidence_in_messages(evidence, messages):
    needle = normalize_evidence(evidence)
    if len(needle) < 6:
        return False
    return any(needle in normalize_evidence(message) for message in messages or [])


def insight_id(principle, project, scope):
    digest = hashlib.sha256(
        "\x1f".join([scope, project, normalize_text(principle)]).encode("utf-8")
    ).hexdigest()[:16]
    return f"insight-{digest}"


def text_similarity(left, right):
    left_atoms = text_atoms(left)
    right_atoms = text_atoms(right)
    if not left_atoms or not right_atoms:
        return 0.0
    return len(left_atoms & right_atoms) / len(left_atoms | right_atoms)


def text_atoms(value):
    text = normalize_text(value)
    atoms = set(re.findall(r"[a-z0-9]{2,}|[\u3400-\u9fff]", text))
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", text))
    atoms.update(cjk[index:index + 2] for index in range(max(0, len(cjk) - 1)))
    return {item for item in atoms if item}


def normalize_text(value):
    text = str(value or "").casefold()
    text = re.sub(r"[^a-z0-9\u3400-\u9fff]+", "", text)
    return text


def normalize_evidence(value):
    return normalize_text(strip_platform_injected_context(str(value or "")))


def normalize_value(value):
    if isinstance(value, list):
        return [normalize_text(item) for item in value]
    return normalize_text(value)


def split_values(value):
    return list(
        dict.fromkeys(
            item
            for item in (
                one_line(part)
                for part in re.split(r"[,，、;；]", str(value or ""))
            )
            if item
        )
    )


def split_memory_ids(value):
    values = split_values(value)
    return [item for item in values if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}", item)]


def one_line(value):
    return " ".join(str(value or "").split()).strip()


def make_title(principle):
    return one_line(principle)[:80]


def project_link(project):
    project = canonical_project(project)
    if not project:
        return "`global`"
    return f"[[01-Projects/{project}/Memory/decisions|{project}]]"


def render_code_list(values):
    return ", ".join(f"`{item}`" for item in values)


def replace_record(records, updated):
    for index, record in enumerate(records):
        if record.get("id") == updated.get("id"):
            records[index] = updated
            return
    records.append(updated)


def result_item(record, action, path, vault):
    relative = os.path.relpath(path, vault).replace(os.sep, "/")
    return {
        "action": action,
        "id": record.get("id") or record.get("memory_id"),
        "title": record.get("title") or record.get("summary", ""),
        "maturity": record.get("maturity", ""),
        "confidence": record.get("confidence", ""),
        "source_count": len(record.get("source_refs") or []),
        "path": relative,
    }
