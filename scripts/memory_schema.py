"""Canonical Agent Memory Beacon runtime record schema."""
import hashlib
import json
import re
from datetime import datetime

import yaml

from memory_authority import (
    AUTHORITY_FIELDS,
    authority_rank,
    authority_revision_payload,
    normalize_authority_metadata,
)
from safety import normalize_project_slug


RUNTIME_SCHEMA_VERSION = "2.0"
MEMORY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")
RUNTIME_MEMORY_TYPES = frozenset(
    {
        "decision",
        "error",
        "preference",
        "project_rule",
        "environment",
        "skill",
        "workflow",
        "insight",
    }
)
MEMORY_STATUSES = frozenset(
    {
        "active",
        "candidate",
        "promoted",
        "superseded",
        "retracted",
        "expired",
        "rejected",
    }
)
FORMAL_MEMORY_STATUSES = frozenset(
    {"active", "superseded", "retracted", "expired"}
)
OPERATIONAL_MEMORY_FIELDS = (
    "name",
    "when",
    "avoid",
    "trigger",
    "behavior",
)
INSIGHT_MATURITIES = frozenset({"seed", "reinforced"})
INSIGHT_ORIGINS = frozenset({"user", "jointly_validated"})
INSIGHT_SCALAR_FIELDS = ("maturity", "novelty", "boundary", "origin")
INSIGHT_LIST_FIELDS = (
    "transfer",
    "supports",
    "operationalized_as",
    "related_to",
)
INSIGHT_RELATION_FIELDS = ("supports", "operationalized_as", "related_to")
MEMORY_RELATION_FIELDS = (
    "supports",
    "operationalized_as",
    "related_to",
    "contradicts",
)
DEFAULT_PROJECT_ALIASES = {
    "github-obsidian-knowledge-brain": "agent-memory-beacon",
    "obsidian-knowledge-brain": "agent-memory-beacon",
}
_MARKDOWN_FRONTMATTER = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)


def canonical_project(value, aliases=None):
    """Return the canonical safe project slug, or an empty global project."""
    raw = str(value or "").strip().strip("'\"")
    if not raw or raw.casefold() == "global":
        return ""
    mapping = dict(DEFAULT_PROJECT_ALIASES)
    mapping.update(aliases or {})
    return normalize_project_slug(mapping.get(raw, raw))


def is_valid_memory_id(value):
    """Return whether value is a valid persisted runtime memory identifier."""
    return isinstance(value, str) and bool(MEMORY_ID_PATTERN.fullmatch(value))


def upgrade_formal_note_frontmatter(content, defaults):
    """Upgrade a formal memory note to schema 2.0 without changing its body."""
    text = str(content or "")
    match = _MARKDOWN_FRONTMATTER.match(text)
    if match:
        try:
            frontmatter = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError as exc:
            raise ValueError("malformed formal memory frontmatter") from exc
        if not isinstance(frontmatter, dict):
            raise ValueError("formal memory frontmatter must be a mapping")
        body = text[match.end():]
    else:
        if text.startswith("---"):
            raise ValueError("malformed formal memory frontmatter")
        frontmatter = {}
        body = "\n" + text if text else ""
    for key, value in dict(defaults or {}).items():
        frontmatter.setdefault(key, value)
    frontmatter["schema_version"] = RUNTIME_SCHEMA_VERSION
    return (
        "---\n"
        + yaml.dump(
            frontmatter,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        + "---\n"
        + body
    )


def normalize_fact_text(value):
    """Normalize fact text for identity comparison without losing semantics."""
    text = str(value or "").strip().casefold()
    text = re.sub(r"\s+", " ", text)
    return re.sub(r"\s*([,，。.!！?？:：;；|])\s*", r"\1", text)


def stable_memory_id(memory_type, project, source_note, source_record_key):
    """Create an ID from immutable source identity, never visible content."""
    source_note = str(source_note or "").strip()
    source_record_key = str(source_record_key or "").strip()
    if not source_note or not source_record_key:
        raise ValueError("stable source identity requires source_note and source_record_key")
    digest = hashlib.sha256(
        "\x1f".join(
            [
                str(memory_type or ""),
                canonical_project(project),
                source_note,
                source_record_key,
            ]
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"{memory_type}-{digest}"


def memory_revision(record):
    """Hash the visible, behavior-affecting state of one formal memory."""
    return _revision_digest(_memory_revision_fields(record, operational=True))


def _legacy_memory_revision(record):
    """Return the pre-operational-fields schema 2.0 revision."""
    return _revision_digest(_memory_revision_fields(record, operational=False))


def _memory_revision_fields(record, *, operational):
    fields = [
        record.get("type", ""),
        record.get("status", ""),
        record.get("project", ""),
        record.get("scope", ""),
        record.get("title", ""),
        record.get("summary", ""),
        record.get("superseded_by", ""),
    ]
    requires = record.get("requires") or []
    expires_at = str(record.get("expires_at") or "").strip()
    if requires:
        fields.extend(
            [
                "requires",
                json.dumps(
                    sorted(str(item).strip() for item in requires),
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            ]
        )
    if expires_at:
        fields.extend(["expires_at", expires_at])
    operational_values = [record.get(key, "") for key in OPERATIONAL_MEMORY_FIELDS]
    if operational and any(str(value or "").strip() for value in operational_values):
        fields.extend(["operational-v1", *operational_values])
    if record.get("type") == "insight":
        fields.extend(
            [
                "insight-v1",
                json.dumps(
                    {
                        **{
                            key: str(record.get(key) or "").strip()
                            for key in INSIGHT_SCALAR_FIELDS
                        },
                        "confidence": record.get("confidence", ""),
                        **{
                            key: list(record.get(key) or [])
                            for key in INSIGHT_LIST_FIELDS
                        },
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
    relation_payload = {
        key: sorted(str(item).strip() for item in record.get(key) or [])
        for key in MEMORY_RELATION_FIELDS
        if record.get(key)
        and (record.get("type") != "insight" or key not in INSIGHT_RELATION_FIELDS)
    }
    if relation_payload:
        fields.extend(
            [
                "semantic-relations-v1",
                json.dumps(
                    relation_payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
    authority = authority_revision_payload(record)
    if authority:
        fields.extend(
            [
                "authority-v1",
                json.dumps(
                    authority,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
    return fields


def _revision_digest(fields):
    return hashlib.sha256(
        "\x1f".join(normalize_fact_text(item) for item in fields).encode("utf-8")
    ).hexdigest()


def normalize_requires(value, memory_id=""):
    """Return a deterministic list of valid hard memory dependencies."""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("requires must be a list of memory IDs")
    normalized = []
    for item in value:
        dependency = str(item or "").strip()
        if not is_valid_memory_id(dependency):
            raise ValueError("requires contains an invalid memory ID")
        if dependency == str(memory_id or "").strip():
            raise ValueError("memory cannot require itself")
        if dependency in normalized:
            raise ValueError("requires contains a duplicate memory ID")
        normalized.append(dependency)
    return sorted(normalized)


def normalize_memory_relations(value, memory_id="", field="relation"):
    """Return a deterministic list of validated declared memory targets."""
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of memory IDs")
    normalized = []
    for item in value:
        target = str(item or "").strip()
        if not is_valid_memory_id(target):
            raise ValueError(f"{field} contains an invalid memory ID")
        if target == str(memory_id or "").strip():
            raise ValueError(f"memory cannot declare {field} to itself")
        if target in normalized:
            raise ValueError(f"{field} contains a duplicate memory ID")
        normalized.append(target)
    return normalized


def normalize_expires_at(value):
    """Return a timezone-aware ISO timestamp or an empty value."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("expires_at must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("expires_at must include a timezone")
    return parsed.isoformat()


def suppress_unmet_dependencies(records):
    """Recursively omit records whose hard requirements are not eligible."""
    items = [dict(item) for item in records or [] if isinstance(item, dict)]
    active_ids = {
        str(item.get("id") or "").strip()
        for item in items
        if is_valid_memory_id(item.get("id"))
    }
    suppressed = {}
    changed = True
    while changed:
        changed = False
        for item in items:
            memory_id = str(item.get("id") or "").strip()
            if memory_id not in active_ids:
                continue
            try:
                requires = normalize_requires(
                    item.get("requires"),
                    memory_id=memory_id,
                )
            except ValueError:
                requires = ["<invalid-requires>"]
            unmet = [dependency for dependency in requires if dependency not in active_ids]
            if not unmet:
                continue
            active_ids.remove(memory_id)
            suppressed[memory_id] = unmet
            changed = True
    return (
        [item for item in items if item.get("id") in active_ids],
        suppressed,
    )


def _valid_lifecycle_metadata(record):
    try:
        requires = normalize_requires(
            record.get("requires"),
            memory_id=record.get("id", ""),
        )
        expires_at = normalize_expires_at(record.get("expires_at"))
    except ValueError:
        return False
    if "requires" in record and record.get("requires") != requires:
        return False
    if "expires_at" in record and str(record.get("expires_at") or "") != expires_at:
        return False
    return True


def _valid_relation_metadata(record):
    for field in MEMORY_RELATION_FIELDS:
        try:
            normalized = normalize_memory_relations(
                record.get(field),
                memory_id=record.get("id", ""),
                field=field,
            )
        except ValueError:
            return False
        if field in record and record.get(field) != normalized:
            return False
    return True


def _valid_authority_metadata(record):
    try:
        normalized = normalize_authority_metadata(record)
    except ValueError:
        return False
    for key in AUTHORITY_FIELDS:
        if key in record and (key not in normalized or record.get(key) != normalized[key]):
            return False
    return True


def is_valid_active_project_record(record, memory_type, expected_project):
    """Return whether an aggregate record is a complete bound runtime record."""
    return is_valid_formal_project_record(
        record,
        memory_type,
        expected_project,
        active_only=True,
    )


def is_valid_formal_project_record(
    record,
    memory_type,
    expected_project,
    *,
    active_only=False,
):
    """Validate one active or inactive project aggregate record."""
    if not isinstance(record, dict):
        return False
    status = record.get("status")
    if status not in FORMAL_MEMORY_STATUSES or (active_only and status != "active"):
        return False
    if not all(_nonempty_string(record.get(key)) for key in ("id", "revision")):
        return False
    if not _valid_lifecycle_metadata(record):
        return False
    if not _valid_relation_metadata(record):
        return False
    if not _valid_authority_metadata(record):
        return False
    source_refs = record.get("source_refs")
    if not (
        isinstance(source_refs, list)
        and source_refs
        and all(_nonempty_string(item) for item in source_refs)
    ):
        return False
    expected_project = canonical_project(expected_project)
    project = str(record.get("project") or "").strip()
    if (
        not expected_project
        or record.get("scope") != "project"
        or project != expected_project
        or canonical_project(project) != expected_project
    ):
        return False
    if memory_type == "decision":
        title = record.get("text")
        summary = record.get("context")
    elif memory_type == "error":
        title = record.get("type")
        summary = record.get("resolution")
    else:
        return False
    if not _nonempty_string(title) or not _nonempty_string(summary):
        return False
    expected_revision = memory_revision(
        {
            "type": memory_type,
            "status": status,
            "project": expected_project,
            "scope": "project",
            "title": title,
            "summary": summary,
            "superseded_by": record.get("superseded_by", ""),
            "requires": record.get("requires") or [],
            "expires_at": record.get("expires_at", ""),
            **{
                key: record.get(key) or []
                for key in MEMORY_RELATION_FIELDS
                if record.get(key)
            },
            **normalize_authority_metadata(record),
        }
    )
    return record.get("revision") == expected_revision


def parse_active_formal_section(title, section, kind):
    """Parse one schema 2.0 adaptive section, rejecting any contract drift."""
    return _parse_formal_section(
        title,
        section,
        kind,
        verify_revision=True,
        active_only=True,
    )


def parse_formal_section(title, section, kind):
    """Parse one active or inactive schema 2.0 adaptive section."""
    return _parse_formal_section(
        title,
        section,
        kind,
        verify_revision=True,
        active_only=False,
    )


def formal_memory_update_allowed(content, memory_id):
    """Allow learners to add missing memory or update one active formal record."""
    if not is_valid_memory_id(memory_id):
        return False
    id_pattern = re.compile(
        rf"(?m)^-\s*id:\s*`{re.escape(str(memory_id))}`\s*$"
    )
    id_matches = list(id_pattern.finditer(str(content or "")))
    if not id_matches:
        return True
    if len(id_matches) != 1:
        return False

    headings = list(re.finditer(r"(?m)^##\s+.+?\s*$", str(content or "")))
    position = id_matches[0].start()
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
        if heading.start() <= position < end:
            section = content[heading.start():end]
            return _raw_markdown_field(section, "status") == "active"
    return False


def active_formal_lifecycle_metadata(content, memory_id, kind):
    """Return validated metadata that learner rewrites must preserve."""
    if not is_valid_memory_id(memory_id):
        return None
    marker = f"- id: `{memory_id}`"
    headings = list(re.finditer(r"(?m)^##\s+.+?\s*$", str(content or "")))
    matches = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(content)
        section = content[heading.start():end]
        if marker not in section:
            continue
        title = heading.group(0).removeprefix("##").strip()
        record = parse_formal_section(title, section, kind)
        if not record or record.get("id") != memory_id or record.get("status") != "active":
            return None
        matches.append(record)
    if len(matches) != 1:
        return None
    record = matches[0]
    return {
        **({"requires": list(record["requires"])} if record.get("requires") else {}),
        **({"expires_at": record["expires_at"]} if record.get("expires_at") else {}),
        **{
            key: list(record[key])
            for key in MEMORY_RELATION_FIELDS
            if record.get(key)
        },
    }


def expected_formal_section_revision(title, section, kind):
    """Return the revision implied by current formal content, if structurally valid."""
    record = _parse_formal_section(
        title,
        section,
        kind,
        verify_revision=False,
        active_only=False,
    )
    return record.get("revision", "") if record else ""


def _parse_formal_section(title, section, kind, verify_revision, active_only):
    fields = {}
    for key in ("id", "revision", "status", "scope"):
        fields[key] = _raw_markdown_field(section, key)
        if not fields[key]:
            return None
    if (
        fields["status"] not in FORMAL_MEMORY_STATUSES
        or (active_only and fields["status"] != "active")
        or fields["scope"] not in {"global", "project"}
    ):
        return None
    project = _formal_project_value(section)
    if project is None:
        return None
    if (
        (fields["scope"] == "global" and project)
        or (fields["scope"] == "project" and not project)
        or (project and canonical_project(project) != project)
    ):
        return None
    source_refs = _formal_source_refs(section)
    if not source_refs:
        return None
    requires = _formal_id_list(section, "requires")
    if requires is None:
        return None
    relations = {}
    for key in MEMORY_RELATION_FIELDS:
        values = _formal_id_list(section, key)
        if values is None:
            return None
        try:
            values = normalize_memory_relations(
                values,
                memory_id=fields["id"],
                field=key,
            )
        except ValueError:
            return None
        if values:
            relations[key] = values
    expires_at = _raw_markdown_field(section, "expires_at")
    try:
        requires = normalize_requires(requires, memory_id=fields["id"])
        expires_at = normalize_expires_at(expires_at)
    except ValueError:
        return None
    superseded_by = _raw_markdown_field(section, "superseded_by")
    retracted_reason = _raw_markdown_field(
        section,
        "retracted_reason",
        allow_backticks=True,
    )
    expired_reason = _raw_markdown_field(
        section,
        "expired_reason",
        allow_backticks=True,
    )
    if fields["status"] == "superseded" and (
        not is_valid_memory_id(superseded_by)
        or superseded_by == fields["id"]
    ):
        return None

    authority_raw = {}
    for key in (
        "authority_role",
        "authority_owner",
        "canonical_source",
        "verified_at",
        "freshness_policy",
    ):
        has_field = bool(
            re.search(rf"(?m)^-\s*{re.escape(key)}:\s*", str(section or ""))
        )
        value = _raw_markdown_field(section, key)
        if has_field and not value:
            return None
        if value:
            authority_raw[key] = value
    for key in ("enforced_by", "verification_refs"):
        has_field = bool(
            re.search(rf"(?m)^-\s*{re.escape(key)}:\s*", str(section or ""))
        )
        values = _formal_id_list(section, key)
        if has_field and not values:
            return None
        if values:
            authority_raw[key] = values
    try:
        authority = normalize_authority_metadata(authority_raw)
    except ValueError:
        return None

    metadata = {}
    if kind == "personal":
        memory_type = _raw_markdown_field(section, "type")
        summary = _raw_markdown_field(section, "memory", allow_backticks=True)
        if memory_type not in {"preference", "project_rule", "environment"}:
            return None
    elif kind == "skill":
        memory_type = "skill"
        name = _raw_markdown_field(section, "skill_name")
        summary = _raw_markdown_section(section, "Why this skill fits")
        when = _raw_markdown_section(section, "When to consider")
        if not name or not when or not str(title).startswith(f"{name}:"):
            return None
        metadata.update(
            {
                "name": name,
                "when": when,
                "avoid": _raw_markdown_section(section, "Do not use when"),
            }
        )
    elif kind == "workflow":
        memory_type = "workflow"
        name = _raw_markdown_field(section, "rule_name")
        summary = _raw_markdown_section(section, "Why this matters")
        trigger = _raw_markdown_section(section, "Trigger scene")
        behavior = _raw_markdown_section(section, "Desired behavior")
        if (
            not name
            or not trigger
            or not behavior
            or not str(title).startswith(f"{name}:")
        ):
            return None
        metadata.update(
            {
                "name": name,
                "trigger": trigger,
                "behavior": behavior,
                "avoid": _raw_markdown_section(section, "Do not apply when"),
            }
        )
    elif kind == "insight":
        memory_type = "insight"
        summary = _raw_markdown_section(section, "Insight")
        maturity = _raw_markdown_field(section, "maturity")
        origin = _raw_markdown_field(section, "origin")
        raw_confidence = _raw_markdown_field(section, "confidence")
        novelty = _raw_markdown_section(section, "Novelty")
        transfer = _raw_markdown_list_section(section, "Transfer")
        boundary = _raw_markdown_section(section, "Boundary")
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            return None
        if (
            maturity not in INSIGHT_MATURITIES
            or origin not in INSIGHT_ORIGINS
            or not 0 <= confidence <= 1
            or not summary
            or not novelty
            or not transfer
            or not boundary
        ):
            return None
        metadata.update(
            {
                "maturity": maturity,
                "origin": origin,
                "confidence": confidence,
                "novelty": novelty,
                "transfer": transfer,
                "boundary": boundary,
            }
        )
    else:
        return None
    if not _nonempty_string(title) or not _nonempty_string(summary):
        return None
    revision_record = {
        "type": memory_type,
        "status": fields["status"],
        "project": project,
        "scope": fields["scope"],
        "title": title,
        "summary": summary,
        "superseded_by": superseded_by,
        "requires": requires,
        "expires_at": expires_at,
        **authority,
        **metadata,
        **relations,
    }
    expected_revision = memory_revision(revision_record)
    compatible_revisions = {expected_revision}
    if memory_type != "insight":
        compatible_revisions.add(_legacy_memory_revision(revision_record))
    if verify_revision and fields["revision"] not in compatible_revisions:
        return None
    return {
        "id": fields["id"],
        "revision": expected_revision,
        "type": memory_type,
        "status": fields["status"],
        "project": project,
        "scope": fields["scope"],
        "title": str(title).strip(),
        "summary": str(summary).strip(),
        "source_refs": source_refs,
        **({"requires": requires} if requires else {}),
        **({"expires_at": expires_at} if expires_at else {}),
        **({"superseded_by": superseded_by} if superseded_by else {}),
        **({"retracted_reason": retracted_reason} if retracted_reason else {}),
        **({"expired_reason": expired_reason} if expired_reason else {}),
        **authority,
        **metadata,
        **relations,
    }


def _nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def _raw_markdown_field(section, key, allow_backticks=False):
    matches = re.findall(
        rf"(?m)^-\s*{re.escape(key)}:\s*(.*?)\s*$",
        str(section or ""),
    )
    if len(matches) != 1:
        return ""
    value = matches[0].strip()
    if value.startswith("`") and value.endswith("`") and value.count("`") == 2:
        value = value[1:-1].strip()
    elif not allow_backticks and "`" in value:
        return ""
    return value


def _formal_source_refs(section):
    matches = re.findall(
        r"(?m)^-\s*source_refs:\s*(.*?)\s*$",
        str(section or ""),
    )
    if len(matches) != 1:
        return []
    raw = matches[0].strip()
    refs = [item.strip() for item in re.findall(r"`([^`]+)`", raw)]
    residue = re.sub(r"`[^`]+`", "", raw).replace(",", "").strip()
    if residue or not refs or any(not item for item in refs):
        return []
    return refs


def _formal_id_list(section, key):
    matches = re.findall(
        rf"(?m)^-\s*{re.escape(key)}:\s*(.*?)\s*$",
        str(section or ""),
    )
    if not matches:
        return []
    if len(matches) != 1:
        return None
    raw = matches[0].strip()
    refs = [item.strip() for item in re.findall(r"`([^`]+)`", raw)]
    residue = re.sub(r"`[^`]+`", "", raw).replace(",", "").strip()
    if residue or not refs or any(not item for item in refs):
        return None
    return refs


def _formal_project_value(section):
    matches = re.findall(r"(?m)^-\s*project:\s*(.*?)\s*$", str(section or ""))
    if len(matches) != 1:
        return None
    raw = matches[0].strip()
    link = re.fullmatch(
        r"\[\[01-Projects/([^/\]]+)/Memory/(?:decisions|pitfalls)(?:\|[^\]]+)?\]\]",
        raw,
    )
    if link:
        return link.group(1).strip()
    if raw.strip("`").strip().casefold() in {"global", "unknown", ""}:
        return ""
    return None


def _raw_markdown_section(section, heading):
    match = re.search(
        rf"(?ms)^###\s+{re.escape(heading)}\s*$\n(.*?)(?=^###\s+|\Z)",
        str(section or ""),
    )
    if not match:
        return ""
    text = re.sub(r"(?m)^\s*[-*]\s+", "", match.group(1))
    return " ".join(text.split())


def _raw_markdown_list_section(section, heading):
    match = re.search(
        rf"(?ms)^###\s+{re.escape(heading)}\s*$\n(.*?)(?=^###\s+|\Z)",
        str(section or ""),
    )
    if not match:
        return []
    values = []
    for raw_line in match.group(1).splitlines():
        line = raw_line.strip()
        if not line:
            continue
        item = re.fullmatch(r"[-*]\s+(.+?)\s*", line)
        if not item:
            return []
        value = " ".join(item.group(1).split())
        if not value:
            return []
        values.append(value)
    return list(dict.fromkeys(values))


def normalize_formal_record(
    record,
    *,
    memory_type,
    default_project="",
    source_ref="",
    source_record_key="",
    date="",
    project_aliases=None,
):
    """Normalize a legacy/formal record into the complete schema 2.0 shape."""
    raw = dict(record or {})
    project = canonical_project(
        raw.get("project", default_project),
        project_aliases,
    )
    if memory_type == "decision":
        title = str(raw.get("title") or raw.get("text") or "").strip()
        summary = str(raw.get("summary") or raw.get("context") or "").strip()
    elif memory_type == "error":
        title = str(raw.get("title") or raw.get("error_type") or raw.get("type") or "").strip()
        summary = str(raw.get("summary") or raw.get("resolution") or "").strip()
    else:
        title = str(raw.get("title") or raw.get("memory") or raw.get("content") or "").strip()
        summary = str(raw.get("summary") or raw.get("memory") or raw.get("content") or "").strip()

    status = str(raw.get("status") or "active").strip().lower()
    if status not in MEMORY_STATUSES:
        status = "retracted"
    scope = str(raw.get("scope") or ("project" if project else "global")).strip().lower()
    if scope not in {"global", "project"}:
        scope = "project" if project else "global"
    if scope == "global":
        project = ""

    explicit_id = str(raw.get("id") or raw.get("memory_id") or "").strip()
    stable_source_note = str(
        raw.get("source_note") or source_ref or ""
    ).strip()
    stable_record_key = str(
        source_record_key
        or raw.get("source_record_key")
        or raw.get("record_key")
        or ""
    ).strip()
    memory_id = explicit_id or stable_memory_id(
        memory_type,
        project,
        stable_source_note,
        stable_record_key,
    )
    requires = normalize_requires(raw.get("requires"), memory_id=memory_id)
    expires_at = normalize_expires_at(raw.get("expires_at"))
    refs = [
        str(item).strip()
        for item in (raw.get("source_refs") or [])
        if str(item).strip()
    ]
    if source_ref and str(source_ref).strip() not in refs:
        refs.append(str(source_ref).strip())
    aliases = [
        str(item).strip()
        for item in (raw.get("aliases") or [])
        if str(item).strip() and str(item).strip() != memory_id
    ]

    normalized = {
        "id": memory_id,
        "revision": "",
        "type": memory_type,
        "status": status,
        "project": project,
        "scope": scope,
        "title": title,
        "summary": summary,
        "date": str(raw.get("date") or date or ""),
        "source_refs": sorted(set(refs)),
        "aliases": sorted(set(aliases)),
        **normalize_authority_metadata(raw),
    }
    for key in (
        "path",
        "source_note",
        "superseded_by",
        "retracted_reason",
        "expired_reason",
    ):
        if raw.get(key):
            normalized[key] = str(raw[key]).strip()
    for key in OPERATIONAL_MEMORY_FIELDS:
        if raw.get(key):
            normalized[key] = str(raw[key]).strip()
    if memory_type == "insight":
        for key in INSIGHT_SCALAR_FIELDS:
            if raw.get(key):
                normalized[key] = str(raw[key]).strip()
        transfer = _normalize_string_list(raw.get("transfer"), "transfer")
        if transfer:
            normalized["transfer"] = transfer
        if raw.get("confidence") not in (None, ""):
            confidence = float(raw["confidence"])
            if not 0 <= confidence <= 1:
                raise ValueError("confidence must be between 0 and 1")
            normalized["confidence"] = confidence
    for key in MEMORY_RELATION_FIELDS:
        values = normalize_memory_relations(
            raw.get(key),
            memory_id=memory_id,
            field=key,
        )
        if values:
            normalized[key] = values
    if requires:
        normalized["requires"] = requires
    if expires_at:
        normalized["expires_at"] = expires_at
    normalized["revision"] = memory_revision(normalized)
    return normalized


def formal_identity_key(record):
    return (
        str(record.get("type") or ""),
        canonical_project(record.get("project")),
        normalize_fact_text(record.get("title")),
        normalize_fact_text(record.get("summary")),
        *(normalize_fact_text(record.get(key)) for key in OPERATIONAL_MEMORY_FIELDS),
        *(
            normalize_fact_text(record.get(key))
            for key in INSIGHT_SCALAR_FIELDS
        ),
        normalize_fact_text(record.get("confidence")),
        *(
            normalize_fact_text(
                json.dumps(record.get(key) or [], ensure_ascii=False)
            )
            for key in INSIGHT_LIST_FIELDS
        ),
        normalize_fact_text(
            json.dumps(record.get("contradicts") or [], ensure_ascii=False)
        ),
    )


def merge_formal_records(records):
    """Merge durable IDs and exact facts while retaining every source."""
    items = [
        dict(record)
        for record in records or []
        if isinstance(record, dict) and record.get("id")
    ]
    parents = list(range(len(items)))

    def find(index):
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left, right):
        left = find(left)
        right = find(right)
        if left != right:
            parents[right] = left

    seen_ids = {}
    seen_facts = {}
    for index, item in enumerate(items):
        for seen, key in (
            (seen_ids, str(item["id"])),
            (seen_facts, formal_identity_key(item)),
        ):
            previous = seen.setdefault(key, index)
            union(index, previous)

    groups = {}
    for index, item in enumerate(items):
        groups.setdefault(find(index), []).append(item)

    merged = []
    for group in sorted(
        groups.values(),
        key=lambda group: min(
            (formal_identity_key(item), str(item["id"])) for item in group
        ),
    ):
        group.sort(
            key=lambda item: (
                item.get("status") == "active",
                -authority_rank(item),
                -_source_rank(item),
                str(item["id"]),
            )
        )
        canonical = dict(group[0])
        refs = set(canonical.get("source_refs") or [])
        aliases = set(canonical.get("aliases") or [])
        dates = [str(canonical.get("date") or "")]
        for item in group[1:]:
            refs.update(item.get("source_refs") or [])
            aliases.update(item.get("aliases") or [])
            if item.get("id") != canonical.get("id"):
                aliases.add(item["id"])
            dates.append(str(item.get("date") or ""))
        aliases.discard(canonical.get("id"))
        canonical["source_refs"] = sorted(refs)
        canonical["aliases"] = sorted(aliases)
        canonical["date"] = max(dates)
        canonical["revision"] = memory_revision(canonical)
        merged.append(canonical)
    return merged


def is_valid_runtime_record(record):
    """Validate the complete formal record contract at every read boundary."""
    if not isinstance(record, dict):
        return False
    if record.get("type") not in RUNTIME_MEMORY_TYPES:
        return False
    if record.get("status") != "active":
        return False
    if not is_valid_memory_id(record.get("id")):
        return False
    if not _valid_lifecycle_metadata(record):
        return False
    if not _valid_relation_metadata(record):
        return False
    if not _valid_authority_metadata(record):
        return False
    revision = record.get("revision")
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{64}", revision):
        return False
    if not _nonempty_string(record.get("title")):
        return False
    if not _nonempty_string(record.get("summary")):
        return False
    if record.get("type") == "insight" and not _valid_insight_metadata(record):
        return False

    source_refs = record.get("source_refs")
    if not (
        isinstance(source_refs, list)
        and source_refs
        and all(_nonempty_string(item) for item in source_refs)
    ):
        return False

    scope = record.get("scope")
    project = str(record.get("project") or "").strip()
    if scope == "global":
        if project:
            return False
    elif scope == "project":
        if not project or canonical_project(project) != project:
            return False
    else:
        return False
    if not runtime_source_path(record):
        return False
    return revision == memory_revision(record)


def is_runtime_record(record):
    return is_valid_runtime_record(record)


def runtime_source_path(record):
    """Return the canonical allowed source path for one runtime record."""
    if not isinstance(record, dict):
        return ""
    path = str(record.get("path") or "").strip()
    source_note = str(record.get("source_note") or "").strip()
    if (
        not path
        or source_note != f"note:{path}"
        or re.search(r"[\x00-\x1f\x7f\\\[\]<>]", path)
        or path.startswith("/")
        or path.endswith(".md")
        or "//" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        return ""

    memory_type = record.get("type")
    project = str(record.get("project") or "").strip()
    project_match = re.fullmatch(
        r"01-Projects/([^/]+)/Memory/(decisions|pitfalls)",
        path,
    )
    if project_match:
        source_project, source_kind = project_match.groups()
        if canonical_project(source_project) != source_project or source_project != project:
            return ""
        expected_kind = "decisions" if memory_type == "decision" else "pitfalls"
        return path if source_kind == expected_kind and memory_type in {"decision", "error"} else ""

    allowed_global_notes = {
        "05-Agent-Memory/personal-memory": {
            "preference",
            "project_rule",
            "environment",
        },
        "05-Agent-Memory/skill-routing-rules": {"skill"},
        "05-Agent-Memory/workflow-rules": {"workflow"},
        "05-Agent-Memory/insights": {"insight"},
    }
    if memory_type in allowed_global_notes.get(path, set()):
        return path
    allowed_source_kinds = {
        "personal-memory": {"preference", "project_rule", "environment"},
        "skill-routing-rules": {"skill"},
        "workflow-rules": {"workflow"},
        "insights": {"insight"},
    }
    source_kind = str(record.get("source_kind") or "")
    return path if memory_type in allowed_source_kinds.get(source_kind, set()) else ""


def _normalize_string_list(value, field):
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    normalized = []
    for item in value:
        text = " ".join(str(item or "").split())
        if not text:
            raise ValueError(f"{field} contains an empty value")
        if text not in normalized:
            normalized.append(text)
    return normalized


def _valid_insight_metadata(record):
    if record.get("maturity") not in INSIGHT_MATURITIES:
        return False
    if record.get("origin") not in INSIGHT_ORIGINS:
        return False
    confidence = record.get("confidence")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        return False
    if not all(_nonempty_string(record.get(key)) for key in ("novelty", "boundary")):
        return False
    transfer = record.get("transfer")
    if not (
        isinstance(transfer, list)
        and transfer
        and all(_nonempty_string(item) for item in transfer)
    ):
        return False
    return True


def _source_rank(record):
    values = [
        str(record.get("path") or ""),
        str(record.get("source_note") or ""),
        *(str(item) for item in record.get("source_refs") or []),
    ]
    joined = " ".join(values)
    if "/Memory/decisions" in joined or "/Memory/pitfalls" in joined:
        return 3
    if "05-Agent-Memory/" in joined:
        return 2
    if "/sessions/" in joined or "session:" in joined:
        return 1
    return 0
