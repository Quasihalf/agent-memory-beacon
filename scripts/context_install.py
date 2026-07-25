"""Shared managed-block installation for agent instruction files."""
import re
from pathlib import Path

from branding import NEW_MANAGED_NAMESPACE, NEW_MANAGED_VERSION
from safety import durable_atomic_write


MANAGED_START = (
    f"<!-- {NEW_MANAGED_NAMESPACE}:MANAGED_START version={NEW_MANAGED_VERSION} -->"
)
MANAGED_END = f"<!-- {NEW_MANAGED_NAMESPACE}:MANAGED_END -->"
OUTER_BLOCK_PATTERNS = (
    re.compile(
        r"(?ms)^<!-- AGENT_MEMORY_BEACON:MANAGED_START version=[0-9]+ -->"
        r"[ \t]*(?:\r\n|\n)"
        r".*?^<!-- AGENT_MEMORY_BEACON:MANAGED_END -->"
        r"(?=[ \t]*(?:\r?\n|\Z))"
    ),
    re.compile(
        r"(?ms)^<!-- KNOWLEDGE_BRAIN:MANAGED_START version=[0-9]+ -->"
        r"[ \t]*(?:\r\n|\n)"
        r".*?^<!-- KNOWLEDGE_BRAIN:MANAGED_END -->"
        r"(?=[ \t]*(?:\r?\n|\Z))"
    ),
)
MANAGED_MARKER_PREFIX = re.compile(
    r"<!--[ \t]*(?:AGENT_MEMORY_BEACON|KNOWLEDGE_BRAIN):"
    r"MANAGED_(?:START|END)\b"
)
MANAGED_MARKER_TOKEN = re.compile(
    r"(?m)<!--[ \t]*"
    r"(?:AGENT_MEMORY_BEACON|KNOWLEDGE_BRAIN):MANAGED_(?:START|END)\b"
    r"[^\r\n]*?(?:-->|(?=\r?$))"
)
VALID_MANAGED_MARKER = re.compile(
    r"<!-- (?P<namespace>AGENT_MEMORY_BEACON|KNOWLEDGE_BRAIN):MANAGED_"
    r"(?:(?P<start>START) version=[0-9]+|(?P<end>END)) -->"
)
COMPILED_BLOCKS = (
    ("<!-- COMPILED:RULES_START -->", "<!-- COMPILED:RULES_END -->"),
    ("<!-- COMPILED:PROJECTS_START -->", "<!-- COMPILED:PROJECTS_END -->"),
)
LEGACY_BLOCK = re.compile(
    r"(?ms)^## (?:Agent Memory Vault|Obsidian Knowledge Brain)[^\r\n]*(?:\r\n|\n)"
    r".*?^\| Pitfalls log \|[^\r\n]*\|[ \t]*(?=\r?$)"
)


def load_managed_patch():
    path = Path(__file__).resolve().parent.parent / "patches" / "AGENT_MEMORY_BEACON.md.patch"
    return read_utf8_text_exact(path).strip()


def read_utf8_text_exact(path):
    """Read UTF-8 text without universal-newline translation."""
    with Path(path).open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def atomic_write_utf8_text_exact(path, content):
    """Atomically write UTF-8 text without newline translation."""
    durable_atomic_write(path, content, encoding="utf-8", mode=0o600)


def _managed_matches(content):
    outer = []
    for pattern in OUTER_BLOCK_PATTERNS:
        outer.extend(pattern.finditer(content))
    _validate_managed_marker_tokens(content, outer)
    standalone_legacy = []
    for legacy in LEGACY_BLOCK.finditer(content):
        contained = any(
            match.start() <= legacy.start() and legacy.end() <= match.end()
            for match in outer
        )
        if not contained:
            standalone_legacy.append(legacy)
    return sorted([*outer, *standalone_legacy], key=lambda match: match.start())


def _validate_managed_marker_tokens(content, outer_matches):
    prefixes = list(MANAGED_MARKER_PREFIX.finditer(content))
    tokens = list(MANAGED_MARKER_TOKEN.finditer(content))
    if any(
        not any(token.start() <= prefix.start() < token.end() for token in tokens)
        for prefix in prefixes
    ):
        raise ValueError("managed marker token is malformed")

    parsed_tokens = []
    for token in tokens:
        parsed = VALID_MANAGED_MARKER.fullmatch(token.group(0))
        if parsed is None:
            raise ValueError("managed marker token is malformed")
        parsed_tokens.append(parsed)

    accounted = set()
    for outer in outer_matches:
        contained = [
            index
            for index, token in enumerate(tokens)
            if outer.start() <= token.start() and token.end() <= outer.end()
        ]
        if len(contained) != 2:
            raise ValueError("managed marker tokens are nested or malformed")
        start, end = (parsed_tokens[index] for index in contained)
        if (
            start.group("start") is None
            or end.group("end") is None
            or start.group("namespace") != end.group("namespace")
        ):
            raise ValueError("managed marker tokens cross namespaces")
        accounted.update(contained)

    if len(accounted) != len(tokens):
        raise ValueError("managed marker token is unmatched")


def extract_managed_patch(content):
    matches = _managed_matches(str(content or ""))
    if len(matches) > 1:
        raise ValueError("multiple managed blocks found; refusing ambiguous update")
    return matches[0].group(0).strip() if matches else None


def _block_body(content, start, end):
    start_at = content.find(start)
    end_at = content.find(end, start_at + len(start))
    if start_at < 0 or end_at < 0:
        return None
    return content[start_at + len(start):end_at]


def _replace_body(content, start, end, body):
    start_at = content.index(start) + len(start)
    end_at = content.index(end, start_at)
    return content[:start_at] + body + content[end_at:]


def _preserve_compiled_bodies(source, patch_text):
    updated = patch_text
    for start, end in COMPILED_BLOCKS:
        body = _block_body(source, start, end)
        if body is not None and _block_body(updated, start, end) is not None:
            updated = _replace_body(updated, start, end, body)
    return updated


def merge_managed_patch(existing, patch_text):
    existing = str(existing or "")
    patch_text = str(patch_text or "").strip()
    if MANAGED_START not in patch_text or MANAGED_END not in patch_text:
        raise ValueError("Agent Memory Beacon patch is missing current managed markers")

    matches = _managed_matches(existing)
    if len(matches) > 1:
        raise ValueError("multiple managed blocks found; refusing ambiguous update")
    match = matches[0] if matches else None
    if match:
        replacement = _preserve_compiled_bodies(match.group(0), patch_text)
        if match.group(0).strip() == replacement.strip():
            return existing, "current"
        return replace_match(existing, match, replacement), "updated"

    separator = _append_separator(existing)
    return existing + separator + patch_text + "\n", "added"


def replace_match(existing, match, patch_text):
    return existing[:match.start()] + patch_text + existing[match.end():]


def _append_separator(existing):
    if not existing:
        return ""
    line_endings = re.findall(r"\r\n|\n|\r", existing)
    newline = line_endings[-1] if line_endings else "\n"
    trailing = re.search(r"(?:(?:\r\n|\n|\r))+$", existing)
    if trailing is None:
        return newline * 2
    trailing_count = len(re.findall(r"\r\n|\n|\r", trailing.group(0)))
    return "" if trailing_count >= 2 else newline
