#!/usr/bin/env python3
"""Optional authority ownership metadata for formal memory records."""
from __future__ import annotations

import re
from datetime import date
from typing import Mapping
from urllib.parse import parse_qsl, unquote, urlsplit


AUTHORITY_FIELDS = (
    "authority_role",
    "authority_owner",
    "canonical_source",
    "enforced_by",
    "verification_refs",
    "verified_at",
    "freshness_policy",
)
AUTHORITY_ROLES = frozenset({"canonical", "rationale", "index", "operationalized"})
FRESHNESS_POLICIES = frozenset({"manual", "source-change", "weekly"})
LOCATOR_PREFIXES = frozenset(
    {"repo", "file", "test", "lint", "runbook", "system", "url", "note", "memory"}
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_SECRET = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|authorization|credential|signature)\s*[=:]"
)
_SECRET_QUERY_KEYS = re.compile(
    r"(?i)(?:password|passwd|secret|token|api[_-]?key|authorization|credential|signature)"
)
_ROLE_RANK = {"index": 1, "rationale": 2, "operationalized": 3, "canonical": 4}


def normalize_authority_metadata(record):
    """Return deterministic authority metadata or reject a partial contract."""
    source = record if isinstance(record, Mapping) else {}
    if not any(key in source for key in AUTHORITY_FIELDS):
        return {}

    role = _safe_scalar(source.get("authority_role"), "authority_role", 32)
    owner = _safe_scalar(source.get("authority_owner"), "authority_owner", 120)
    if role not in AUTHORITY_ROLES:
        raise ValueError("authority_role is invalid")
    if not owner:
        raise ValueError("authority_owner is required")

    canonical_source = _optional_locator(source.get("canonical_source"))
    enforced_by = _locator_list(source.get("enforced_by"), "enforced_by")
    verification_refs = _locator_list(
        source.get("verification_refs"),
        "verification_refs",
    )
    verified_at = _normalize_date(source.get("verified_at"))
    freshness_policy = _safe_scalar(
        source.get("freshness_policy"),
        "freshness_policy",
        32,
    )
    if freshness_policy and freshness_policy not in FRESHNESS_POLICIES:
        raise ValueError("freshness_policy is invalid")
    if role in {"canonical", "rationale", "index"} and not canonical_source:
        raise ValueError(f"{role} authority requires canonical_source")
    if role == "operationalized" and not enforced_by:
        raise ValueError("operationalized authority requires enforced_by")

    normalized = {
        "authority_role": role,
        "authority_owner": owner,
    }
    if canonical_source:
        normalized["canonical_source"] = canonical_source
    if enforced_by:
        normalized["enforced_by"] = enforced_by
    if verification_refs:
        normalized["verification_refs"] = verification_refs
    if verified_at:
        normalized["verified_at"] = verified_at
    if freshness_policy:
        normalized["freshness_policy"] = freshness_policy
    return normalized


def authority_revision_payload(record):
    """Return a fixed-shape payload only for authority-bearing records."""
    normalized = normalize_authority_metadata(record)
    if not normalized:
        return {}
    return {
        "authority_role": normalized["authority_role"],
        "authority_owner": normalized["authority_owner"],
        "canonical_source": normalized.get("canonical_source", ""),
        "enforced_by": list(normalized.get("enforced_by") or []),
        "verification_refs": list(normalized.get("verification_refs") or []),
        "verified_at": normalized.get("verified_at", ""),
        "freshness_policy": normalized.get("freshness_policy", ""),
    }


def authority_rank(record):
    """Return a tie-break rank; invalid metadata never gains authority."""
    try:
        role = normalize_authority_metadata(record).get("authority_role", "")
    except ValueError:
        return 0
    return _ROLE_RANK.get(role, 0)


def authority_route(record):
    """Return the strongest safe route for compact runtime explanations."""
    try:
        authority = normalize_authority_metadata(record)
    except ValueError:
        return ""
    if authority.get("authority_role") == "operationalized":
        enforced = authority.get("enforced_by") or []
        if enforced:
            return enforced[0]
    return authority.get("canonical_source") or authority.get("authority_owner", "")


def render_authority_markdown_lines(record):
    """Render normalized metadata in the adaptive formal-section syntax."""
    authority = normalize_authority_metadata(record)
    if not authority:
        return []
    lines = [
        f"- authority_role: `{authority['authority_role']}`",
        f"- authority_owner: `{authority['authority_owner']}`",
    ]
    if authority.get("canonical_source"):
        lines.append(f"- canonical_source: `{authority['canonical_source']}`")
    for key in ("enforced_by", "verification_refs"):
        if authority.get(key):
            lines.append(
                f"- {key}: " + ", ".join(f"`{item}`" for item in authority[key])
            )
    if authority.get("verified_at"):
        lines.append(f"- verified_at: `{authority['verified_at']}`")
    if authority.get("freshness_policy"):
        lines.append(f"- freshness_policy: `{authority['freshness_policy']}`")
    return lines


def _safe_scalar(value, field, limit):
    text = str(value or "").strip()
    if len(text) > limit or _CONTROL.search(text) or "`" in text or _SECRET.search(text):
        raise ValueError(f"{field} is unsafe")
    return text


def _optional_locator(value):
    raw = str(value or "").strip()
    return _normalize_locator(raw) if raw else ""


def _locator_list(value, field):
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > 16:
        raise ValueError(f"{field} must be a bounded locator list")
    normalized = [_normalize_locator(item) for item in value]
    if any(not item for item in normalized):
        raise ValueError(f"{field} contains an empty locator")
    return sorted(set(normalized))


def _normalize_locator(value):
    locator = str(value or "").strip()
    if (
        not locator
        or len(locator) > 512
        or _CONTROL.search(locator)
        or "`" in locator
        or "\\" in locator
        or _SECRET.search(locator)
    ):
        raise ValueError("authority locator is unsafe")
    prefix, separator, payload = locator.partition(":")
    if not separator or prefix not in LOCATOR_PREFIXES or not payload:
        raise ValueError("authority locator prefix is invalid")
    if prefix == "url":
        return _normalize_url_locator(payload)

    decoded = unquote(payload)
    if (
        payload.startswith(("/", "~"))
        or decoded.startswith(("/", "~"))
        or _WINDOWS_ABSOLUTE.match(payload)
        or _WINDOWS_ABSOLUTE.match(decoded)
    ):
        raise ValueError("authority locator must not contain an absolute path")
    path_part = decoded.split("#", 1)[0].split("?", 1)[0]
    if any(part in {"", ".", ".."} for part in path_part.split("/")):
        raise ValueError("authority locator contains path traversal")
    return f"{prefix}:{payload}"


def _normalize_url_locator(payload):
    parsed = urlsplit(payload)
    if (
        parsed.scheme not in {"https", "http"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("authority URL must be an uncredentialed HTTP(S) URL")
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        if _SECRET_QUERY_KEYS.fullmatch(key):
            raise ValueError("authority URL contains a secret-bearing query")
    decoded_path = unquote(parsed.path)
    if any(part == ".." for part in decoded_path.split("/")):
        raise ValueError("authority URL contains path traversal")
    return f"url:{payload}"


def _normalize_date(value):
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return date.fromisoformat(raw).isoformat()
    except ValueError as exc:
        raise ValueError("verified_at must be an ISO-8601 date") from exc
