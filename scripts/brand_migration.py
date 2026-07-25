"""Reversible Agent Memory Beacon Vault identity migration."""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import time
from types import SimpleNamespace

import yaml

from branding import CODE_PREFIX, LEGACY_PROJECT_SLUG, PROJECT_SLUG
from compiler import run as compile_agent_context
from link_validator import run as validate_links
from reporter import rebuild_maps
from safety import (
    VAULT_INTERNAL_DIR_NAMES,
    normalize_project_slug,
    safe_vault_path,
    strip_markdown_code_blocks,
)
from session_harvester import rebuild_memory_index


_CONTRACT_SHA256 = hashlib.sha256
MIGRATION_MANIFEST_SCHEMA_VERSION = 2


MIGRATION_MANAGED_ROOTS = (
    "00-Inbox",
    "00-Rules",
    "01-Projects",
    "03-Maps",
    "04-Feedback/_memory-candidates",
    "04-Feedback/_skill-preferences",
    "04-Feedback/_workflow-candidates",
    "05-Agent-Memory",
    "Users",
)
DEFAULT_MUTABLE_ROOTS = tuple(
    root for root in MIGRATION_MANAGED_ROOTS if root != "00-Rules"
)
OBSIDIAN_STATE_FILES = (
    ".obsidian/app.json",
    ".obsidian/graph.json",
    ".obsidian/workspace.json",
)
GENERATED_OUTPUT_FILES = (
    "03-Maps/topic-index.md",
    "03-Maps/timeline.md",
    "05-Agent-Memory/keyword-index.json",
    "05-Agent-Memory/keyword-index.md",
    "05-Agent-Memory/global-atoms.json",
    "05-Agent-Memory/global-atoms.md",
    "05-Agent-Memory/recall-index.json",
    "05-Agent-Memory/memory-graph.json",
    "05-Agent-Memory/recall-context.md",
)
MIGRATION_JOURNAL_DIRECTORY = "journal"
AGENT_MEMORY_ROOT_MARKERS = (
    ".agent-memory-beacon-root",
    ".obsidian-knowledge-brain-root",
)
PROJECT_KEYS = {
    "project",
    "projects",
    "primary",
    "project_slug",
    "source_project",
    "related_projects",
}
VAULT_REFERENCE_KEYS = {"source_note", "session_note", "path", "rel_path"}
WIKI_LINK = re.compile(r"\[\[([^\]\r\n]+)\]\]")
_DIR_FD_PRIMITIVES = (
    ("open", os.open),
    ("mkdir", os.mkdir),
    ("rename", os.rename),
    ("stat", os.stat),
    ("unlink", os.unlink),
    ("rmdir", os.rmdir),
)


@dataclass(frozen=True)
class TargetSpec:
    role: str
    key: str
    path: Path
    expected_state: str
    mutation_kind: str
    entry_kind: str
    inode: tuple[int, int] | None


@dataclass(frozen=True)
class MutationContract:
    target_specs: tuple[TargetSpec, ...]
    mutable_roots: tuple[Path, ...]
    excluded_mutable_subtrees: tuple[Path, ...]
    mutable_directories: tuple[Path, ...]
    absent_paths: tuple[Path, ...]
    absent_directories: tuple[Path, ...]
    mutable_files: tuple[Path, ...]


@dataclass(frozen=True)
class InputBinding:
    path: Path
    sha256: str
    inode: tuple[int, int]
    link_count: int
    mode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    expected_type: str


@dataclass(frozen=True)
class DirectoryBinding:
    path: Path
    inode: tuple[int, int]
    mode: int
    atime_ns: int
    mtime_ns: int
    expected_type: str


@dataclass(frozen=True)
class MigrationPlan:
    vault: Path
    old_slug: str
    new_slug: str
    source_project: Path
    destination_project: Path
    markdown_paths: tuple[Path, ...]
    config_path: Path | None
    mutation_contract: MutationContract
    mutation_contract_sha256: str
    input_bindings: tuple[InputBinding, ...]
    directory_bindings: tuple[DirectoryBinding, ...]
    observed_hashes: tuple[tuple[Path, str], ...]
    source_directories: tuple[Path, ...]
    broken_links_before: tuple[tuple[str, str], ...]
    memory_identity_count_before: int
    memory_identity_keys_before: tuple[str, ...]

    @property
    def backup_paths(self):
        return tuple(binding.path for binding in self.input_bindings)

    @property
    def input_hashes(self):
        return tuple(
            (binding.path, binding.sha256)
            for binding in self.input_bindings
        )

    @property
    def mutable_roots(self):
        return self.mutation_contract.mutable_roots

    @property
    def mutable_directories_before(self):
        return self.mutation_contract.mutable_directories

    @property
    def excluded_mutable_subtrees(self):
        return self.mutation_contract.excluded_mutable_subtrees

    @property
    def configured_targets(self):
        return tuple(
            spec.path
            for spec in self.mutation_contract.target_specs
            if spec.entry_kind in {"root", "file"}
        )

    @property
    def configured_target_states(self):
        return tuple(
            (spec.path, spec.expected_state)
            for spec in self.mutation_contract.target_specs
            if spec.entry_kind in {"root", "file"}
        )

    @property
    def absent_paths_before(self):
        return self.mutation_contract.absent_paths

    @property
    def public_paths_before(self):
        return tuple(
            path for path in self.backup_paths if path.is_relative_to(self.vault)
        )


def file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _input_stat_identity(stat_result):
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_nlink,
        stat_result.st_mode,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def _binding_stat_identity(binding):
    return (
        binding.inode[0],
        binding.inode[1],
        binding.link_count,
        binding.mode,
        binding.size,
        binding.mtime_ns,
        binding.ctime_ns,
    )


def _capture_input_binding(
    path,
    error_type=ValueError,
    hard_link_message="input has unlisted hard-link alias",
):
    path = Path(path)
    if path != path.resolve():
        raise error_type(f"migration input path is not canonical: {path}")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise error_type(f"cannot open migration input safely: {path}: {exc}") from exc
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise error_type(f"migration input is not a regular file: {path}")
        if before.st_nlink != 1:
            raise error_type(f"{hard_link_message}: {path}")
        digest = _hash_fd(fd)
        after = os.fstat(fd)
        if _input_stat_identity(after) != _input_stat_identity(before):
            raise error_type(f"migration input changed while hashing: {path}")
        if after.st_nlink != 1:
            raise error_type(f"{hard_link_message}: {path}")
        return InputBinding(
            path=path,
            sha256=digest,
            inode=_inode_from_stat(after),
            link_count=after.st_nlink,
            mode=after.st_mode,
            size=after.st_size,
            mtime_ns=after.st_mtime_ns,
            ctime_ns=after.st_ctime_ns,
            expected_type="regular_file",
        )
    finally:
        os.close(fd)


def _capture_input_bindings(paths):
    bindings = tuple(_capture_input_binding(path) for path in paths)
    seen_inodes = {}
    for binding in bindings:
        previous = seen_inodes.get(binding.inode)
        if previous is not None:
            raise ValueError(
                "duplicate migration input inode between "
                f"{previous} and {binding.path}"
            )
        seen_inodes[binding.inode] = binding.path
    return bindings


def _capture_directory_binding(path):
    path = Path(path)
    if path != path.resolve():
        raise ValueError(f"migration directory path is not canonical: {path}")
    fd = os.open(path, _directory_flags())
    try:
        before = os.fstat(fd)
        after = os.fstat(fd)
        if not stat.S_ISDIR(before.st_mode) or _inode_from_stat(before) != _inode_from_stat(after):
            raise ValueError(f"migration directory changed while binding: {path}")
        return DirectoryBinding(
            path=path,
            inode=_inode_from_stat(after),
            mode=after.st_mode,
            atime_ns=after.st_atime_ns,
            mtime_ns=after.st_mtime_ns,
            expected_type="directory",
        )
    finally:
        os.close(fd)


def _capture_directory_bindings(paths):
    bindings = tuple(_capture_directory_binding(path) for path in sorted(set(paths)))
    if len({binding.inode for binding in bindings}) != len(bindings):
        raise ValueError("migration directories contain duplicate inodes")
    return bindings


def _revalidate_directory_binding(binding):
    fd = os.open(binding.path, _directory_flags())
    try:
        current = os.fstat(fd)
        if (
            binding.expected_type != "directory"
            or not stat.S_ISDIR(current.st_mode)
            or _inode_from_stat(current) != binding.inode
            or current.st_mode != binding.mode
            or current.st_mtime_ns != binding.mtime_ns
        ):
            raise RuntimeError(
                f"migration directory changed after preflight: {binding.path}"
            )
    finally:
        os.close(fd)


def _revalidate_input_binding(binding, change_context="after preflight"):
    prefix = f"migration inputs changed {change_context}"
    try:
        current = _capture_input_binding(
            binding.path,
            error_type=RuntimeError,
            hard_link_message="input link count changed",
        )
    except RuntimeError as exc:
        raise RuntimeError(f"{prefix}: {exc}") from exc
    if current.expected_type != binding.expected_type:
        raise RuntimeError(f"{prefix}: input type changed: {binding.path}")
    if current.inode != binding.inode:
        raise RuntimeError(f"{prefix}: input inode changed: {binding.path}")
    if current.link_count != binding.link_count or current.link_count != 1:
        raise RuntimeError(f"{prefix}: input link count changed: {binding.path}")
    if current.mode != binding.mode:
        raise RuntimeError(f"{prefix}: input mode changed: {binding.path}")
    if current.size != binding.size:
        raise RuntimeError(f"{prefix}: input size changed: {binding.path}")
    if current.mtime_ns != binding.mtime_ns:
        raise RuntimeError(f"{prefix}: input mtime changed: {binding.path}")
    if current.ctime_ns != binding.ctime_ns:
        raise RuntimeError(f"{prefix}: input ctime changed: {binding.path}")
    if current.sha256 != binding.sha256:
        raise RuntimeError(f"{prefix}: input digest changed: {binding.path}")


def _read_utf8(path, kind="file"):
    path = Path(path)
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"non-UTF-8 {kind}: {path}") from exc


def _assert_no_symlink_below(anchor, path, label):
    anchor = Path(anchor)
    path = Path(path)
    try:
        relative = path.relative_to(anchor)
    except ValueError as exc:
        raise ValueError(f"{label} is outside its allowed root: {path}") from exc
    current = anchor
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label} contains symlink: {current}")


def _configured_external_path(raw_path, label):
    raw = Path(
        os.path.abspath(
            os.path.expandvars(os.path.expanduser(str(raw_path)))
        )
    )
    current = Path(raw.anchor)
    for part in raw.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"{label} contains symlink: {current}")
    resolved = raw.resolve(strict=False)
    if raw != resolved:
        raise ValueError(f"{label} contains retargetable path alias: {raw}")
    return resolved


def _vault_path(vault, raw_path, label):
    try:
        path = Path(safe_vault_path(vault, raw_path))
    except ValueError as exc:
        raise ValueError(f"{label} must stay inside the vault") from exc
    _assert_no_symlink_below(vault, path, label)
    return path


def _walk_tree(root, label, skip_internal=False, excluded_roots=()):
    root = Path(root)
    excluded_roots = {Path(path) for path in excluded_roots if path is not None}
    if any(root == path or root.is_relative_to(path) for path in excluded_roots):
        return (), ()
    if root.is_symlink():
        raise ValueError(f"{label} contains symlink: {root}")
    if not root.exists():
        return (), ()
    if not root.is_dir():
        raise ValueError(f"{label} must be a directory: {root}")
    directories = [root]
    files_found = []
    def raise_walk_error(error):
        raise error

    for current, dirs, files in os.walk(
        root,
        topdown=True,
        onerror=raise_walk_error,
        followlinks=False,
    ):
        current = Path(current)
        kept_dirs = []
        for name in dirs:
            path = current / name
            if path in excluded_roots:
                continue
            if path.is_symlink():
                raise ValueError(f"{label} contains symlink: {path}")
            if skip_internal and name in VAULT_INTERNAL_DIR_NAMES:
                continue
            directories.append(path)
            kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            path = current / name
            if path.is_symlink():
                raise ValueError(f"{label} contains symlink: {path}")
            files_found.append(path)
    return tuple(sorted(files_found)), tuple(sorted(directories))


def _iter_public_markdown(vault, excluded_root=None, excluded_roots=()):
    excluded = [Path(vault) / ".obsidian", *map(Path, excluded_roots)]
    if excluded_root is not None:
        excluded.append(Path(excluded_root))
    files, _directories = _walk_tree(
        vault,
        "public vault",
        skip_internal=True,
        excluded_roots=excluded,
    )
    yield from (path for path in files if path.suffix == ".md")


def _frontmatter(path):
    content = _read_utf8(path, "Markdown")
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return {}
    closing = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.rstrip("\r\n") == "---"
        ),
        None,
    )
    if closing is None:
        return {}
    parsed = yaml.safe_load("".join(lines[1:closing])) or {}
    return parsed if isinstance(parsed, dict) else {}


def _value_has_project_ref(value, old_slug, seen=None):
    seen = set() if seen is None else seen
    if isinstance(value, str):
        return value == old_slug or f"01-Projects/{old_slug}/" in value
    if not isinstance(value, (list, dict)):
        return False
    identity = id(value)
    if identity in seen:
        return False
    seen.add(identity)
    try:
        if isinstance(value, list):
            return any(
                _value_has_project_ref(item, old_slug, seen) for item in value
            )
        return _value_has_project_ref(value.get("name"), old_slug, seen)
    finally:
        seen.remove(identity)


def _mapping_has_project_ref(value, old_slug, seen=None):
    seen = set() if seen is None else seen
    if isinstance(value, list):
        identity = id(value)
        if identity in seen:
            return False
        seen.add(identity)
        try:
            return any(
                _mapping_has_project_ref(item, old_slug, seen) for item in value
            )
        finally:
            seen.remove(identity)
    if not isinstance(value, dict):
        return False
    identity = id(value)
    if identity in seen:
        return False
    seen.add(identity)
    try:
        for key, item in value.items():
            key = str(key)
            if key in PROJECT_KEYS and _value_has_project_ref(item, old_slug, seen):
                return True
            if (
                key in VAULT_REFERENCE_KEYS
                and isinstance(item, str)
                and f"01-Projects/{old_slug}/" in item
            ):
                return True
            if _mapping_has_project_ref(item, old_slug, seen):
                return True
        return False
    finally:
        seen.remove(identity)


def split_wikilink(raw_link: str) -> tuple[str, str | None]:
    """Return normalized target and alias for normal or table-escaped links."""
    raw_link = str(raw_link)
    separator = re.search(r"\\?\|", raw_link)
    if separator is None:
        return raw_link.strip(), None
    target = raw_link[:separator.start()].strip()
    alias = raw_link[separator.end():].strip()
    return target, alias


def _clone_yaml_value(value, memo):
    if not isinstance(value, (list, dict)):
        return value
    key = (id(value), "clone")
    if key in memo:
        return memo[key]
    if isinstance(value, list):
        cloned = []
        memo[key] = cloned
        cloned.extend(_clone_yaml_value(item, memo) for item in value)
        return cloned
    cloned = {}
    memo[key] = cloned
    for item_key, item in value.items():
        cloned[item_key] = _clone_yaml_value(item, memo)
    return cloned


def _rewrite_project_value_state(value, old_slug, new_slug, memo):
    if isinstance(value, str):
        if value == old_slug:
            return new_slug, True
        updated = _normalize_project_path(value, old_slug, new_slug)
        return updated, updated != value
    if not isinstance(value, (list, dict)):
        return value, False

    key = (id(value), "project")
    if key in memo:
        return memo[key], False
    if isinstance(value, list):
        updated = []
        memo[key] = updated
        changed = False
        for item in value:
            rewritten, item_changed = _rewrite_project_value_state(
                item,
                old_slug,
                new_slug,
                memo,
            )
            updated.append(rewritten)
            changed = changed or item_changed
        if not changed:
            memo[key] = value
            return value, False
        return updated, True

    name = value.get("name")
    if not isinstance(name, str) or name != old_slug:
        memo[key] = value
        return value, False

    updated = {}
    memo[key] = updated
    memo[(id(value), "clone")] = updated
    changed = False
    for item_key, item in value.items():
        key_text = str(item_key)
        if key_text in {"name", "keywords"}:
            rewritten, item_changed = _rewrite_project_value_state(
                item,
                old_slug,
                new_slug,
                memo,
            )
            updated[item_key] = rewritten
            changed = changed or item_changed
        else:
            updated[item_key] = _clone_yaml_value(item, memo)
    return updated, changed


def _rewrite_project_value(value, old_slug, new_slug, memo=None):
    rewritten, _changed = _rewrite_project_value_state(
        value,
        old_slug,
        new_slug,
        {} if memo is None else memo,
    )
    return rewritten


def _rewrite_project_fields_state(value, old_slug, new_slug, memo):
    if isinstance(value, list):
        key = (id(value), "fields")
        if key in memo:
            return memo[key], False
        updated = []
        memo[key] = updated
        changed = False
        for item in value:
            rewritten, item_changed = _rewrite_project_fields_state(
                item,
                old_slug,
                new_slug,
                memo,
            )
            updated.append(rewritten)
            changed = changed or item_changed
        if not changed:
            memo[key] = value
            return value, False
        return updated, True
    if not isinstance(value, dict):
        return value, False
    key = (id(value), "fields")
    if key in memo:
        return memo[key], False
    updated = {}
    memo[key] = updated
    changed = False
    for item_key, item in value.items():
        key_text = str(item_key)
        if key_text in PROJECT_KEYS:
            rewritten, item_changed = _rewrite_project_value_state(
                item,
                old_slug,
                new_slug,
                memo,
            )
        elif key_text in VAULT_REFERENCE_KEYS and isinstance(item, str):
            rewritten = _normalize_project_path(
                item,
                old_slug,
                new_slug,
            )
            item_changed = rewritten != item
        else:
            rewritten, item_changed = _rewrite_project_fields_state(
                item,
                old_slug,
                new_slug,
                memo,
            )
        updated[item_key] = rewritten
        changed = changed or item_changed
    if not changed:
        memo[key] = value
        return value, False
    return updated, True


def _rewrite_project_fields(value, old_slug, new_slug, memo=None):
    rewritten, _changed = _rewrite_project_fields_state(
        value,
        old_slug,
        new_slug,
        {} if memo is None else memo,
    )
    return rewritten


def _split_frontmatter_content(content):
    lines = str(content).splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return None, str(content)
    closing = next(
        (
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.rstrip("\r\n") == "---"
        ),
        None,
    )
    if closing is None:
        return None, str(content)
    return "".join(lines[1:closing]), "".join(lines[closing + 1:])


def _replace_wikilink_target(raw, old_slug, new_slug):
    separator = re.search(r"\\?\|", raw)
    target_end = separator.start() if separator is not None else len(raw)
    target_raw = raw[:target_end]
    target = target_raw.strip()
    if not _project_target_matches(target, old_slug):
        return raw
    leading = target_raw[:len(target_raw) - len(target_raw.lstrip())]
    trailing = target_raw[len(target_raw.rstrip()):]
    rewritten = _normalize_project_path(target, old_slug, new_slug)
    return leading + rewritten + trailing + raw[target_end:]


def _rewrite_wikilinks(body, old_slug, new_slug):
    lines = []
    fence_char = ""
    fence_length = 0
    for line in str(body).splitlines(keepends=True):
        if fence_char:
            lines.append(line)
            closing = re.match(
                rf"^[ ]{{0,3}}{re.escape(fence_char)}{{{fence_length},}}\s*$",
                line.rstrip("\r\n"),
            )
            if closing:
                fence_char = ""
                fence_length = 0
            continue

        opening = re.match(r"^[ ]{0,3}(`{3,}|~{3,})", line)
        if opening:
            fence = opening.group(1)
            fence_char = fence[0]
            fence_length = len(fence)
            lines.append(line)
            continue
        if line.startswith("\t") or re.match(r"^ {4,}\S", line):
            lines.append(line)
            continue
        line = WIKI_LINK.sub(
            lambda match: "[[" + _replace_wikilink_target(
                match.group(1), old_slug, new_slug
            ) + "]]",
            line,
        )
        lines.append(line)
    return "".join(lines)


def rewrite_markdown(content, old_slug, new_slug):
    frontmatter_text, body = _split_frontmatter_content(content)
    updated_body = _rewrite_wikilinks(body, old_slug, new_slug)
    if frontmatter_text is None:
        return updated_body, updated_body != content
    parsed = yaml.safe_load(frontmatter_text) or {}
    if not isinstance(parsed, dict):
        parsed = {}
    updated_frontmatter, frontmatter_changed = _rewrite_project_fields_state(
        parsed,
        old_slug,
        new_slug,
        {},
    )
    if not frontmatter_changed and updated_body == body:
        return content, False
    rendered = (
        yaml.safe_dump(
            updated_frontmatter,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        )
        if frontmatter_changed
        else frontmatter_text
    )
    separator = "" if updated_body.startswith(("\n", "\r")) else "\n"
    return f"---\n{rendered.rstrip()}\n---{separator}{updated_body}", True


def _project_target_matches(target, slug):
    base = str(target).strip().split("#", 1)[0].rstrip("/")
    root = f"01-Projects/{slug}"
    return base == root or base.startswith(root + "/")


def _markdown_has_structural_ref(path, old_slug):
    content = _read_utf8(path, "Markdown")
    if _mapping_has_project_ref(_frontmatter(path), old_slug):
        return True
    body = strip_markdown_code_blocks(content)
    for raw_link in WIKI_LINK.findall(body):
        target, _alias = split_wikilink(raw_link)
        if _project_target_matches(target, old_slug):
            return True
    return any(
        line.strip() in {
            f"# Decisions — {old_slug}",
            f"# Pitfalls — {old_slug}",
        }
        for line in body.splitlines()
    )


def memory_identity_keys(project_dir):
    project_dir = Path(project_dir)
    keys = []
    collection_fields = (
        "decisions",
        "decisions_made",
        "pitfalls",
        "errors_encountered",
    )
    for path in sorted(project_dir.rglob("*.md")):
        relative = path.relative_to(project_dir).as_posix()
        frontmatter = _frontmatter(path)
        session_id = str(frontmatter.get("session_id", "")).strip()
        if session_id:
            keys.append(f"session:{session_id}")
        for field in collection_fields:
            records = frontmatter.get(field) or []
            if not isinstance(records, list):
                continue
            for index, record in enumerate(records):
                explicit_id = (
                    str(record.get("id", "")).strip()
                    if isinstance(record, dict)
                    else ""
                )
                record_id = explicit_id or f"{relative}:{field}:{index}"
                keys.append(f"{field}:{record_id}")
    return tuple(keys)


def count_memory_identities(project_dir):
    return len(memory_identity_keys(project_dir))


def _normalize_project_path(value, old_slug, new_slug):
    value = str(value).strip()
    anchor = ""
    if "#" in value:
        value, raw_anchor = value.split("#", 1)
        anchor = "#" + raw_anchor
    old_root = f"01-Projects/{old_slug}"
    if value == old_root:
        value = f"01-Projects/{new_slug}"
    elif value.startswith(old_root + "/"):
        value = f"01-Projects/{new_slug}" + value[len(old_root):]
    return value + anchor


def _normalized_broken_links(
    vault,
    old_slug,
    new_slug,
    excluded_dir_names=(),
    excluded_roots=(),
    additional_markdown_paths=(),
):
    excluded_dir_names = {
        *excluded_dir_names,
        *(
            Path(path).name
            for path in excluded_roots
            if Path(path).is_relative_to(vault)
        ),
    }
    try:
        broken_items = validate_links(
            vault,
            excluded_dir_names={".obsidian", *excluded_dir_names},
            additional_markdown_paths=additional_markdown_paths,
        )
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"non-UTF-8 Markdown encountered by link validator: {exc}"
        ) from exc
    return tuple(
        sorted(
            (
                _normalize_project_path(item["source"], old_slug, new_slug),
                _normalize_project_path(item["target"], old_slug, new_slug),
            )
            for item in broken_items
        )
    )


def _path_state(path, frozen_absent_paths=()):
    path = Path(path)
    if path in frozen_absent_paths:
        return "absent"
    if not os.path.lexists(path):
        return "absent"
    if path.is_symlink():
        raise ValueError(f"configured target contains symlink: {path}")
    if path.is_file():
        return "file"
    if path.is_dir():
        return "directory"
    raise ValueError(f"configured target has unsupported type: {path}")


def _configured_vault_path(vault, raw_path, default, label):
    raw_path = raw_path or default
    return _vault_path(vault, raw_path, label)


def _configured_root(vault, raw_path, default, label):
    raw_path = raw_path or default
    expanded = os.path.expandvars(os.path.expanduser(str(raw_path)))
    if os.path.isabs(expanded):
        path = _configured_external_path(expanded, label)
        if path.is_relative_to(vault):
            _assert_no_symlink_below(vault, path, label)
        return path
    return _vault_path(vault, expanded, label)


def _add_unique_target(targets, seen, path, label):
    path = Path(path)
    previous = seen.get(path)
    if previous is not None:
        raise ValueError(
            "duplicate canonical mutation input: "
            f"{previous} and {label} both resolve to {path}"
        )
    seen[path] = label
    targets.append(path)


def _target_inode(path, state):
    if state == "absent":
        return None
    stat_result = os.stat(path, follow_symlinks=False)
    return (stat_result.st_dev, stat_result.st_ino)


def _validate_external_agent_root(agent_root, vault, protected_paths):
    agent_root = Path(agent_root)
    vault = Path(vault)
    repo_root = Path(__file__).resolve().parents[1]
    if agent_root.is_relative_to(vault):
        if agent_root == vault:
            raise ValueError("agent_memory_path must be a dedicated child directory")
        return
    home = Path.home().resolve()
    broad_home_roots = {home / "Desktop", home / "Downloads"}
    if agent_root in broad_home_roots:
        raise ValueError(
            "agent_memory_path cannot use broad home directory "
            f"{agent_root.name}"
        )
    dangerous = {
        Path(agent_root.anchor),
        home,
        vault,
        *vault.parents,
        repo_root,
        *repo_root.parents,
    }
    if agent_root in dangerous or len(agent_root.parts) < 4:
        raise ValueError("agent_memory_path must be a dedicated bounded directory")
    for protected in protected_paths:
        protected = Path(protected)
        if (
            agent_root == protected
            or agent_root == protected.parent
            or protected.is_relative_to(agent_root)
        ):
            raise ValueError(
                "agent_memory_path must be a dedicated non-overlapping directory"
            )
    if not os.path.lexists(agent_root):
        return
    if agent_root.is_symlink() or not agent_root.is_dir():
        raise ValueError("agent_memory_path must be a dedicated directory")
    with os.scandir(agent_root) as entries:
        if next(entries, None) is None:
            return
    markers = [agent_root / name for name in AGENT_MEMORY_ROOT_MARKERS]
    present_markers = [path for path in markers if os.path.lexists(path)]
    if not present_markers:
        raise ValueError(
            "non-empty external agent_memory_path requires an ownership marker"
        )
    for marker in present_markers:
        if marker.is_symlink() or not marker.is_file():
            raise ValueError(
                f"agent_memory_path ownership marker must be a regular file: {marker}"
            )


def _missing_parent_paths(path, stop=None, frozen_absent_paths=()):
    path = Path(path)
    stop = Path(stop) if stop is not None else None
    frozen_absent_paths = set(frozen_absent_paths)
    missing = []
    current = path.parent
    while current != current.parent and current != stop:
        if current in frozen_absent_paths:
            missing.append(current)
            current = current.parent
            continue
        if os.path.lexists(current):
            break
        missing.append(current)
        current = current.parent
    return tuple(reversed(missing))


def _mutation_contract(vault, config, parsed, frozen_absent_paths=()):
    vault = Path(vault)
    frozen_absent_paths = frozenset(Path(path) for path in frozen_absent_paths)
    specs = []
    specs_by_path = {}
    specs_by_identity = {}

    def add_spec(role, key, path, mutation_kind, entry_kind):
        path = Path(path)
        identity = (role, key)
        if identity in specs_by_identity:
            raise ValueError(f"duplicate mutation role/key: {role}/{key}")
        if path in specs_by_path:
            previous = specs_by_path[path]
            raise ValueError(
                "duplicate canonical mutation input: "
                f"{previous.role}/{previous.key} and {role}/{key} both resolve to {path}"
            )
        state = _path_state(path, frozen_absent_paths)
        spec = TargetSpec(
            role=role,
            key=key,
            path=path,
            expected_state=state,
            mutation_kind=mutation_kind,
            entry_kind=entry_kind,
            inode=_target_inode(path, state),
        )
        specs.append(spec)
        specs_by_path[path] = spec
        specs_by_identity[identity] = spec
        return spec

    context_targets = parsed.get("context_targets", []) or []
    if not isinstance(context_targets, list):
        raise ValueError("config context_targets must be a list")
    contexts = [
        _configured_external_path(raw, f"configured target context[{index}]")
        for index, raw in enumerate(context_targets)
        if raw
    ]
    legacy_context = None
    if parsed.get("claude_md_path"):
        legacy_context = _configured_external_path(
            parsed["claude_md_path"], "configured target claude_md_path"
        )
    agent_memory = _configured_root(
        vault,
        parsed.get("agent_memory_path"),
        "05-Agent-Memory",
        "agent_memory_path",
    )
    if parsed.get("codex_profile_path"):
        profile_root = _configured_external_path(
            parsed["codex_profile_path"],
            "configured target codex_profile_path",
        )
    else:
        profile_root = agent_memory / "codex-profile"
        if profile_root.is_relative_to(vault):
            _assert_no_symlink_below(
                vault, profile_root, "configured target codex_profile_path"
            )
        else:
            profile_root = _configured_external_path(
                profile_root,
                "configured target codex_profile_path",
            )
    profile_shared = profile_root / "AGENTS.shared.md"
    _assert_no_symlink_below(
        profile_root, profile_shared, "configured target AGENTS.shared.md"
    )

    sections = (
        (
            "personal_memory",
            "04-Feedback/_memory-candidates",
            "05-Agent-Memory/personal-memory.md",
        ),
        (
            "skill_preferences",
            "04-Feedback/_skill-preferences",
            "05-Agent-Memory/skill-routing-rules.md",
        ),
        (
            "workflow_memory",
            "04-Feedback/_workflow-candidates",
            "05-Agent-Memory/workflow-rules.md",
        ),
    )
    root_entries = [
        ("inbox", _vault_path(vault, "00-Inbox", "mutable root inbox")),
        ("projects", _vault_path(vault, "01-Projects", "mutable root projects")),
        ("maps", _vault_path(vault, "03-Maps", "mutable root maps")),
        ("users_cleanup", _vault_path(vault, "Users", "mutable root Users")),
        (
            "knowledge_indexes",
            _vault_path(vault, "05-Agent-Memory", "mutable root knowledge indexes"),
        ),
    ]
    explicit_files = []
    for section_name, default_candidate, default_formal in sections:
        settings = parsed.get(section_name) or {}
        if not isinstance(settings, dict):
            raise ValueError(f"config {section_name} must be a mapping")
        candidate = _configured_vault_path(
            vault,
            settings.get("candidate_dir"),
            default_candidate,
            f"{section_name}.candidate_dir",
        )
        formal = _configured_vault_path(
            vault,
            settings.get("formal_path"),
            default_formal,
            f"{section_name}.formal_path",
        )
        root_entries.append((f"{section_name}_candidates", candidate))
        explicit_files.append(
            (section_name, "formal", formal, "rewrite")
        )

    memory_index = _configured_vault_path(
        vault,
        parsed.get("memory_index_path"),
        "00-Inbox/Agent Memory Index.md",
        "memory_index_path",
    )
    protected_paths = [
        path
        for path in (
            config,
            *contexts,
            legacy_context,
            profile_shared
            if not profile_shared.is_relative_to(agent_memory)
            else None,
        )
        if path is not None
    ]
    _validate_external_agent_root(
        agent_memory, vault, protected_paths
    )
    root_entries.append(("agent_memory", agent_memory))

    if config is not None:
        explicit_files.append(("config_path", "config", config, "rewrite"))
    explicit_files.append(
        ("memory_index_path", "index", memory_index, "rewrite")
    )
    explicit_files.extend(
        ("context", f"context[{index}]", path, "rewrite")
        for index, path in enumerate(contexts)
    )
    if legacy_context is not None:
        explicit_files.append(
            ("context", "claude_md_path", legacy_context, "rewrite")
        )
    explicit_files.append(
        ("profile", "AGENTS.shared.md", profile_shared, "rewrite")
    )
    explicit_files.extend(
        (
            "obsidian_state",
            Path(relative).name,
            _vault_path(vault, relative, f"mutable target {relative}"),
            "rewrite",
        )
        for relative in OBSIDIAN_STATE_FILES
    )
    explicit_files.extend(
        (
            "generated_output",
            relative,
            _vault_path(vault, relative, f"mutable target {relative}"),
            "rewrite",
        )
        for relative in GENERATED_OUTPUT_FILES
    )

    grouped_roots = {}
    for key, path in root_entries:
        grouped_roots.setdefault(path, []).append(key)
    root_specs = []
    for path, keys in sorted(grouped_roots.items()):
        root_specs.append(
            add_spec(
                "mutable_root",
                "+".join(sorted(keys)),
                path,
                "rewrite_delete_create",
                "root",
            )
        )

    excluded_mutable_subtrees = tuple(
        sorted(
            {
                profile_root
                for root_spec in root_specs
                if profile_root == root_spec.path
                or profile_root.is_relative_to(root_spec.path)
            }
        )
    )

    primary_specs = []
    for role, key, path, mutation_kind in explicit_files:
        primary_specs.append(
            add_spec(role, key, path, mutation_kind, "file")
        )

    for primary in tuple(primary_specs):
        for suffix in (".tmp", ".restore"):
            sibling = Path(str(primary.path) + suffix)
            if sibling.is_relative_to(vault):
                _assert_no_symlink_below(
                    vault, sibling, f"temporary sibling {primary.role}/{primary.key}"
                )
            else:
                sibling = _configured_external_path(
                    sibling, f"temporary sibling {primary.role}/{primary.key}"
                )
            add_spec(
                "temporary",
                f"{primary.role}/{primary.key}{suffix}",
                sibling,
                "temporary",
                "temp_sibling",
            )

    parent_candidates = set()
    for spec in (*root_specs, *primary_specs):
        stop = vault if spec.path.is_relative_to(vault) else None
        parent_candidates.update(
            _missing_parent_paths(
                spec.path,
                stop=stop,
                frozen_absent_paths=frozen_absent_paths,
            )
        )
    for path in sorted(parent_candidates):
        if path in specs_by_path:
            continue
        if any(
            path == excluded or path.is_relative_to(excluded)
            for excluded in excluded_mutable_subtrees
        ):
            continue
        add_spec(
            "parent_directory",
            str(path),
            path,
            "create_remove",
            "parent_directory",
        )

    mutable_files = set()
    mutable_directories = set()
    for root_spec in root_specs:
        if root_spec.expected_state == "absent":
            continue
        if root_spec.expected_state != "directory":
            raise ValueError(f"mutable root must be a directory: {root_spec.path}")
        files, directories = _walk_tree(
            root_spec.path,
            f"mutable root {root_spec.path}",
            excluded_roots=excluded_mutable_subtrees,
        )
        mutable_directories.update(directories)
        for path in files:
            mutable_files.add(path)
            if path not in specs_by_path:
                relative = path.relative_to(root_spec.path).as_posix()
                add_spec(
                    "discovered_file",
                    f"{root_spec.key}/{relative}",
                    path,
                    "rewrite_delete",
                    "file",
                )

    for path in excluded_mutable_subtrees:
        if path.is_dir():
            mutable_directories.add(path)

    for spec in specs:
        if spec.expected_state == "file":
            mutable_files.add(spec.path)

    inode_owners = {}
    for spec in specs:
        if spec.expected_state != "file":
            continue
        owner = inode_owners.get(spec.inode)
        if owner is not None and owner.path != spec.path:
            raise ValueError(
                "hard-link alias between "
                f"{owner.role}/{owner.key} and {spec.role}/{spec.key}"
            )
        inode_owners[spec.inode] = spec

    target_specs = tuple(
        sorted(specs, key=lambda item: (item.role, item.key, str(item.path)))
    )
    absent_paths = tuple(
        sorted(spec.path for spec in target_specs if spec.expected_state == "absent")
    )
    absent_directories = tuple(
        sorted(
            spec.path
            for spec in target_specs
            if spec.expected_state == "absent"
            and spec.entry_kind in {"root", "parent_directory"}
        )
    )
    return MutationContract(
        target_specs=target_specs,
        mutable_roots=tuple(sorted(spec.path for spec in root_specs)),
        excluded_mutable_subtrees=excluded_mutable_subtrees,
        mutable_directories=tuple(sorted(mutable_directories)),
        absent_paths=absent_paths,
        absent_directories=absent_directories,
        mutable_files=tuple(sorted(mutable_files)),
    )


def _mutation_contract_sha256(contract):
    payload = {
        "target_specs": [
            {
                "role": spec.role,
                "key": spec.key,
                "path": str(spec.path),
                "expected_state": spec.expected_state,
                "mutation_kind": spec.mutation_kind,
                "entry_kind": spec.entry_kind,
                "inode": list(spec.inode) if spec.inode is not None else None,
            }
            for spec in contract.target_specs
        ],
        "mutable_roots": [str(path) for path in contract.mutable_roots],
        "excluded_mutable_subtrees": [
            str(path) for path in contract.excluded_mutable_subtrees
        ],
        "mutable_directories": [str(path) for path in contract.mutable_directories],
        "absent_paths": [str(path) for path in contract.absent_paths],
        "absent_directories": [str(path) for path in contract.absent_directories],
        "mutable_files": [str(path) for path in contract.mutable_files],
    }
    return _CONTRACT_SHA256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _profile_shared_markdown_paths(contract):
    return tuple(
        spec.path
        for spec in contract.target_specs
        if spec.role == "profile"
        and spec.key == "AGENTS.shared.md"
        and spec.expected_state == "file"
    )


def build_migration_plan(
    vault: str | os.PathLike[str],
    config_path: str | os.PathLike[str] | None = None,
) -> MigrationPlan:
    vault = Path(vault).expanduser().resolve()
    old_slug = normalize_project_slug(LEGACY_PROJECT_SLUG)
    new_slug = normalize_project_slug(PROJECT_SLUG)
    source = Path(safe_vault_path(vault, "01-Projects", old_slug))
    destination = Path(safe_vault_path(vault, "01-Projects", new_slug))
    _assert_no_symlink_below(vault, source, "source project")
    rollback_parent = Path(
        safe_vault_path(
            vault, "04-Feedback", "_rollback", "brand-migration"
        )
    )
    _assert_no_symlink_below(vault, rollback_parent, "rollback parent")
    if source.is_symlink():
        raise ValueError(f"source project is a symlink: {source}")
    if not source.is_dir():
        raise FileNotFoundError(f"source project does not exist: {source}")
    if os.path.lexists(destination):
        raise ValueError(f"destination already exists: {destination}")
    for path in source.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"source project contains symlink: {path}")

    config = None
    parsed = {}
    if config_path:
        config = _configured_external_path(
            config_path, "configured target config_path"
        )
        parsed = yaml.safe_load(_read_utf8(config, "config")) or {}
        if not isinstance(parsed, dict):
            raise ValueError("migration config must contain a YAML mapping")
        configured_vault = Path(
            os.path.expandvars(str(parsed.get("vault_path", "")))
        ).expanduser().resolve()
        if configured_vault != vault:
            raise ValueError("config vault_path does not match migration vault")

    contract = _mutation_contract(vault, config, parsed)
    profile_shared_markdown_paths = _profile_shared_markdown_paths(contract)
    public_markdown_paths = tuple(
        sorted(
            set(
                _iter_public_markdown(
                    vault,
                    excluded_roots=contract.excluded_mutable_subtrees,
                )
            )
            | set(profile_shared_markdown_paths)
        )
    )
    markdown_paths = tuple(
        path
        for path in public_markdown_paths
        if _markdown_has_structural_ref(path, old_slug)
    )
    backup_paths = tuple(
        sorted(set(contract.mutable_files) | set(markdown_paths))
    )
    input_bindings = _capture_input_bindings(backup_paths)
    input_digests = {
        binding.path: binding.sha256 for binding in input_bindings
    }

    broken = _normalized_broken_links(
        vault,
        old_slug,
        new_slug,
        excluded_roots=contract.excluded_mutable_subtrees,
        additional_markdown_paths=profile_shared_markdown_paths,
    )
    public_paths_before = tuple(
        path for path in backup_paths if path.is_relative_to(vault)
    )
    observed_paths = tuple(
        sorted(set(public_paths_before) | set(public_markdown_paths))
    )
    source_directories = (
        source,
        *(sorted(path for path in source.rglob("*") if path.is_dir())),
    )
    directory_bindings = _capture_directory_bindings(
        set(contract.mutable_directories) | set(source_directories)
    )
    identity_keys = memory_identity_keys(source)
    return MigrationPlan(
        vault=vault,
        old_slug=old_slug,
        new_slug=new_slug,
        source_project=source,
        destination_project=destination,
        markdown_paths=markdown_paths,
        config_path=config,
        mutation_contract=contract,
        mutation_contract_sha256=_mutation_contract_sha256(contract),
        input_bindings=input_bindings,
        directory_bindings=directory_bindings,
        observed_hashes=tuple(
            (path, input_digests.get(path) or file_sha256(path))
            for path in observed_paths
        ),
        source_directories=source_directories,
        broken_links_before=broken,
        memory_identity_count_before=len(identity_keys),
        memory_identity_keys_before=identity_keys,
    )


def _validate_contract_collections(contract):
    specs = contract.target_specs
    identities = [(spec.role, spec.key) for spec in specs]
    paths = [spec.path for spec in specs]
    if len(set(identities)) != len(identities):
        raise ValueError("mutation contract has duplicate role/key entries")
    if len(set(paths)) != len(paths):
        raise ValueError("mutation contract has duplicate canonical paths")
    expected_roots = tuple(
        sorted(spec.path for spec in specs if spec.entry_kind == "root")
    )
    expected_files = tuple(
        sorted(spec.path for spec in specs if spec.expected_state == "file")
    )
    expected_absent = tuple(
        sorted(spec.path for spec in specs if spec.expected_state == "absent")
    )
    expected_absent_directories = tuple(
        sorted(
            spec.path
            for spec in specs
            if spec.expected_state == "absent"
            and spec.entry_kind in {"root", "parent_directory"}
        )
    )
    if contract.mutable_roots != expected_roots:
        raise ValueError("mutation contract mutable roots are inconsistent")
    exclusions = contract.excluded_mutable_subtrees
    if (
        not isinstance(exclusions, tuple)
        or exclusions != tuple(sorted(set(exclusions)))
        or any(path != path.resolve(strict=False) for path in exclusions)
        or any(
            not any(path == root or path.is_relative_to(root) for root in expected_roots)
            for path in exclusions
        )
    ):
        raise ValueError(
            "mutation contract excluded subtree is outside mutable roots"
        )
    if contract.mutable_files != expected_files:
        raise ValueError("mutation contract mutable files are inconsistent")
    if contract.absent_paths != expected_absent:
        raise ValueError("mutation contract absent paths are inconsistent")
    if contract.absent_directories != expected_absent_directories:
        raise ValueError("mutation contract absent directories are inconsistent")
    if len(set(contract.mutable_directories)) != len(contract.mutable_directories):
        raise ValueError("mutation contract mutable directories are inconsistent")
    if any(
        path != excluded and path.is_relative_to(excluded)
        for path in contract.mutable_directories
        for excluded in exclusions
    ):
        raise ValueError(
            "mutation contract mutable directory is below an excluded subtree"
        )
    if any(
        spec.role == "discovered_file"
        and any(
            spec.path == excluded or spec.path.is_relative_to(excluded)
            for excluded in exclusions
        )
        for spec in specs
    ):
        raise ValueError(
            "mutation contract discovered file is below an excluded subtree"
        )
    inode_owners = {}
    for spec in specs:
        if spec.expected_state != "file" or spec.inode is None:
            continue
        owner = inode_owners.get(spec.inode)
        if owner is not None and owner.path != spec.path:
            raise ValueError(
                "mutation contract hard-link alias between "
                f"{owner.role}/{owner.key} and {spec.role}/{spec.key}"
            )
        inode_owners[spec.inode] = spec


def _validate_input_binding_collection(plan):
    bindings = plan.input_bindings
    if not isinstance(bindings, tuple):
        raise ValueError("migration plan input bindings must be a tuple")
    seen_paths = set()
    seen_inodes = set()
    for binding in bindings:
        if binding.path in seen_paths:
            raise ValueError("migration plan input bindings contain a duplicate path")
        seen_paths.add(binding.path)
        if binding.path != binding.path.resolve():
            raise ValueError(
                f"migration plan contains a backup path escape: {binding.path}"
            )
        if binding.expected_type != "regular_file":
            raise ValueError(
                f"migration plan input binding type is invalid: {binding.path}"
            )
        if binding.link_count != 1:
            raise ValueError(
                f"migration plan input binding link count is invalid: {binding.path}"
            )
        if not isinstance(binding.mode, int) or not stat.S_ISREG(binding.mode):
            raise ValueError(
                f"migration plan input binding mode is invalid: {binding.path}"
            )
        if not isinstance(binding.size, int) or binding.size < 0:
            raise ValueError(
                f"migration plan input binding size is invalid: {binding.path}"
            )
        if not isinstance(binding.mtime_ns, int):
            raise ValueError(
                f"migration plan input binding mtime is invalid: {binding.path}"
            )
        if not isinstance(binding.ctime_ns, int):
            raise ValueError(
                f"migration plan input binding ctime is invalid: {binding.path}"
            )
        if (
            not isinstance(binding.inode, tuple)
            or len(binding.inode) != 2
            or not all(isinstance(value, int) for value in binding.inode)
        ):
            raise ValueError(
                f"migration plan input binding inode is invalid: {binding.path}"
            )
        if binding.inode in seen_inodes:
            raise ValueError("migration plan input bindings contain a duplicate inode")
        seen_inodes.add(binding.inode)
        if (
            not isinstance(binding.sha256, str)
            or len(binding.sha256) != 64
            or any(character not in "0123456789abcdef" for character in binding.sha256)
        ):
            raise ValueError(
                f"migration plan input binding digest is invalid: {binding.path}"
            )

    expected_paths = tuple(
        sorted(
            set(plan.mutation_contract.mutable_files)
            | set(plan.markdown_paths)
        )
    )
    paths = tuple(binding.path for binding in bindings)
    if paths != expected_paths:
        raise ValueError("migration plan input binding paths do not match contract")

    binding_by_path = {binding.path: binding for binding in bindings}
    for spec in plan.mutation_contract.target_specs:
        if spec.expected_state != "file":
            continue
        binding = binding_by_path.get(spec.path)
        if binding is None or binding.inode != spec.inode:
            raise ValueError(
                "migration plan input bindings do not match mutation contract"
            )


def _validate_directory_binding_collection(plan):
    bindings = plan.directory_bindings
    if not isinstance(bindings, tuple):
        raise ValueError("migration plan directory bindings must be a tuple")
    expected_paths = tuple(
        sorted(
            set(plan.mutation_contract.mutable_directories)
            | set(plan.source_directories)
        )
    )
    if tuple(binding.path for binding in bindings) != expected_paths:
        raise ValueError("migration plan directory bindings do not match contract")
    if len({binding.inode for binding in bindings}) != len(bindings):
        raise ValueError("migration plan directory bindings contain duplicate inodes")
    for binding in bindings:
        if (
            binding.expected_type != "directory"
            or binding.path != binding.path.resolve()
            or not isinstance(binding.inode, tuple)
            or len(binding.inode) != 2
            or not all(isinstance(value, int) for value in binding.inode)
            or not isinstance(binding.mode, int)
            or not stat.S_ISDIR(binding.mode)
            or not isinstance(binding.atime_ns, int)
            or not isinstance(binding.mtime_ns, int)
        ):
            raise ValueError(
                f"migration plan directory binding is invalid: {binding.path}"
            )


def _spec_definition(spec):
    return (
        spec.role,
        spec.key,
        spec.path,
        spec.mutation_kind,
        spec.entry_kind,
    )


def _assert_plan_unchanged(
    plan,
    excluded_public_root=None,
    allowed_created_directories=None,
):
    allowed_created_directories = {
        Path(path): inode
        for path, inode in (allowed_created_directories or {}).items()
    }
    _validate_contract_collections(plan.mutation_contract)
    if plan.vault != plan.vault.resolve():
        raise ValueError("migration plan contains a path escape or alias")
    if plan.old_slug != LEGACY_PROJECT_SLUG:
        raise ValueError("migration plan old_slug does not match branding constant")
    if plan.new_slug != PROJECT_SLUG:
        raise ValueError("migration plan new_slug does not match branding constant")
    if normalize_project_slug(plan.old_slug) != plan.old_slug:
        raise ValueError("migration plan contains an invalid legacy slug")
    if normalize_project_slug(plan.new_slug) != plan.new_slug:
        raise ValueError("migration plan contains an invalid destination slug")
    expected_source = Path(
        safe_vault_path(plan.vault, "01-Projects", plan.old_slug)
    )
    expected_destination = Path(
        safe_vault_path(plan.vault, "01-Projects", plan.new_slug)
    )
    if plan.source_project != expected_source:
        raise ValueError("migration plan contains a source path escape")
    if plan.destination_project != expected_destination:
        raise ValueError("migration plan contains a destination path escape")
    rollback_parent = Path(
        safe_vault_path(
            plan.vault, "04-Feedback", "_rollback", "brand-migration"
        )
    )
    _assert_no_symlink_below(plan.vault, rollback_parent, "rollback parent")
    _validate_input_binding_collection(plan)
    _validate_directory_binding_collection(plan)
    if _mutation_contract_sha256(plan.mutation_contract) != plan.mutation_contract_sha256:
        raise ValueError("mutation contract does not match its frozen binding")
    binding_paths = {binding.path for binding in plan.input_bindings}
    if plan.config_path is not None and plan.config_path not in binding_paths:
        raise ValueError("mutation contract omits config_path from input bindings")
    for binding in plan.input_bindings:
        _revalidate_input_binding(binding)
    for binding in plan.directory_bindings:
        _revalidate_directory_binding(binding)

    if os.path.lexists(plan.destination_project):
        raise RuntimeError(
            f"migration inputs changed after preflight: {plan.destination_project}"
        )
    if not plan.source_project.is_dir() or plan.source_project.is_symlink():
        raise RuntimeError(
            f"migration inputs changed after preflight: {plan.source_project}"
        )
    if any(path.is_symlink() for path in plan.source_project.rglob("*")):
        raise RuntimeError("migration inputs changed after preflight: source symlink")

    current_mutable_files = set()
    current_mutable_directories = set()
    excluded_mutable_subtrees = plan.mutation_contract.excluded_mutable_subtrees
    for root in plan.mutation_contract.mutable_roots:
        if root.is_relative_to(plan.vault):
            _assert_no_symlink_below(plan.vault, root, f"mutable root {root}")
        else:
            _configured_external_path(root, f"mutable root {root}")
        if not root.exists():
            continue
        files, directories = _walk_tree(
            root,
            f"mutable root {root}",
            excluded_roots=excluded_mutable_subtrees,
        )
        current_mutable_files.update(files)
        current_mutable_directories.update(directories)

    frozen_absent_paths = set()
    for path in plan.mutation_contract.absent_paths:
        allowed_inode = allowed_created_directories.get(path)
        if allowed_inode is not None:
            if path.is_symlink() or not path.is_dir():
                raise RuntimeError(
                    f"migration backup infrastructure changed: {path}"
                )
            if _target_inode(path, "directory") != allowed_inode:
                raise RuntimeError(
                    f"migration backup infrastructure inode changed: {path}"
                )
            frozen_absent_paths.add(path)
            continue
        if os.path.lexists(path):
            raise RuntimeError(
                f"migration inputs changed after preflight: absent target appeared: {path}"
            )

    for spec in plan.mutation_contract.target_specs:
        path = spec.path
        expected_state = spec.expected_state
        if path.is_relative_to(plan.vault):
            _assert_no_symlink_below(plan.vault, path, f"configured target {path}")
        else:
            _configured_external_path(path, f"configured target {path}")
        current_state = _path_state(path, frozen_absent_paths)
        if current_state != expected_state:
            raise RuntimeError(
                "migration inputs changed after preflight: configured target "
                f"{path} changed from {expected_state} to {current_state}"
            )
        if expected_state != "absent":
            current_inode = _target_inode(path, current_state)
            if current_inode != spec.inode:
                raise RuntimeError(
                    f"migration inputs changed after preflight: inode changed: {path}"
                )

    planned_mutable_files = {
        path
        for path in plan.mutation_contract.mutable_files
        if any(
            path == root or path.is_relative_to(root)
            for root in plan.mutation_contract.mutable_roots
        )
        and not any(
            path == excluded or path.is_relative_to(excluded)
            for excluded in excluded_mutable_subtrees
        )
    }
    if current_mutable_files != planned_mutable_files:
        raise RuntimeError("migration inputs changed after preflight: mutable files")
    planned_walked_directories = tuple(
        path
        for path in plan.mutation_contract.mutable_directories
        if not any(
            path == excluded or path.is_relative_to(excluded)
            for excluded in excluded_mutable_subtrees
        )
    )
    if tuple(sorted(current_mutable_directories)) != planned_walked_directories:
        raise RuntimeError("migration inputs changed after preflight: mutable directories")

    current_directories = tuple(
        path
        for path in plan.mutation_contract.mutable_directories
        if path == plan.source_project or path.is_relative_to(plan.source_project)
    )
    public_exclusions = tuple(excluded_mutable_subtrees)
    if excluded_public_root is not None:
        public_exclusions += (Path(excluded_public_root),)
    profile_shared_markdown_paths = _profile_shared_markdown_paths(
        plan.mutation_contract
    )
    current_public_markdown = tuple(
        sorted(
            set(
                _iter_public_markdown(
                    plan.vault,
                    excluded_roots=public_exclusions,
                )
            )
            | set(profile_shared_markdown_paths)
        )
    )
    current_markdown_paths = tuple(
        sorted(
            path
            for path in current_public_markdown
            if _markdown_has_structural_ref(path, plan.old_slug)
        )
    )
    if current_directories != plan.source_directories:
        raise RuntimeError("migration inputs changed after preflight: source directories")
    if current_markdown_paths != plan.markdown_paths:
        raise RuntimeError("migration inputs changed after preflight: structural references")

    current_observed_paths = tuple(
        sorted(set(plan.public_paths_before) | set(current_public_markdown))
    )
    if tuple(path for path, _digest in plan.observed_hashes) != current_observed_paths:
        raise RuntimeError("migration inputs changed after preflight: observed paths")
    for path, expected_hash in plan.observed_hashes:
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"migration inputs changed after preflight: {path}")
        if file_sha256(path) != expected_hash:
            raise RuntimeError(f"migration inputs changed after preflight: {path}")

    current_broken_links = _normalized_broken_links(
        plan.vault,
        plan.old_slug,
        plan.new_slug,
        excluded_dir_names=(
            {Path(excluded_public_root).name}
            if excluded_public_root is not None
            else ()
        ),
        excluded_roots=excluded_mutable_subtrees,
        additional_markdown_paths=profile_shared_markdown_paths,
    )
    if current_broken_links != plan.broken_links_before:
        raise ValueError("migration plan broken-link baseline is inconsistent")
    if plan.memory_identity_count_before != len(plan.memory_identity_keys_before):
        raise ValueError("migration plan memory identity count is inconsistent")
    current_identity_keys = memory_identity_keys(plan.source_project)
    if current_identity_keys != plan.memory_identity_keys_before:
        raise ValueError("migration plan memory identity baseline is inconsistent")

def plan_summary(plan: MigrationPlan) -> dict[str, object]:
    _assert_plan_unchanged(plan)
    return {
        "status": "ready",
        "vault": str(plan.vault),
        "old_slug": plan.old_slug,
        "new_slug": plan.new_slug,
        "source_files": sum(
            1
            for path in plan.backup_paths
            if path.is_relative_to(plan.source_project)
        ),
        "structural_candidate_files": len(plan.markdown_paths),
        "backup_files": len(plan.backup_paths),
        "broken_links_before": len(plan.broken_links_before),
        "destination_exists": False,
    }


def _backup_entries(plan, backup_root):
    entries = []
    destinations = set()
    for source in plan.backup_paths:
        if source.is_relative_to(plan.vault):
            relative = source.relative_to(plan.vault)
            backup = Path(
                safe_vault_path(backup_root, "vault", *relative.parts)
            )
            restore_target = {"kind": "vault", "path": relative.as_posix()}
        else:
            external_name = (
                hashlib.sha256(str(source).encode("utf-8")).hexdigest()[:12]
                + "-"
                + source.name
            )
            backup = Path(
                safe_vault_path(backup_root, "external", external_name)
            )
            restore_target = {"kind": "external", "path": str(source)}
        if backup in destinations:
            raise ValueError(f"colliding backup destination: {backup}")
        destinations.add(backup)
        entries.append((source, backup, restore_target))
    return tuple(entries)


def _manifest_path_record(plan, path):
    path = Path(path)
    if path.is_relative_to(plan.vault):
        return {
            "kind": "vault",
            "path": path.relative_to(plan.vault).as_posix(),
        }
    return {"kind": "external", "path": str(path)}


@dataclass
class _PinnedDirectory:
    path: Path
    fd: int
    inode: tuple[int, int]
    parent_fd: int | None
    name: str | None
    created: bool


@dataclass(frozen=True)
class _SealedFile:
    relative: Path
    inode: tuple[int, int]
    link_count: int
    sha256: str
    mode: int
    label: str


@dataclass(frozen=True)
class _SealedDirectory:
    relative: Path
    inode: tuple[int, int]
    mode: int


def _require_secure_publication_primitives():
    missing = [
        name for name, function in _DIR_FD_PRIMITIVES
        if function not in os.supports_dir_fd
    ]
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        missing.append("O_NOFOLLOW/O_DIRECTORY")
    if os.stat not in os.supports_follow_symlinks:
        missing.append("stat(follow_symlinks=False)")
    if _exclusive_rename_variant() is None:
        missing.append("atomic exclusive directory rename")
    if missing:
        raise RuntimeError(
            "secure migration backup publication is unavailable: "
            + ", ".join(missing)
        )


def _directory_flags():
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _exclusive_rename_variant():
    libc = ctypes.CDLL(None, use_errno=True)
    if hasattr(libc, "renameatx_np"):
        return libc.renameatx_np, 0x00000004
    if hasattr(libc, "renameat2"):
        return libc.renameat2, 0x00000001
    return None


def _exchange_rename_variant():
    libc = ctypes.CDLL(None, use_errno=True)
    if hasattr(libc, "renameatx_np"):
        return libc.renameatx_np, 0x00000002
    if hasattr(libc, "renameat2"):
        return libc.renameat2, 0x00000002
    return None


def _call_atomic_rename(variant, source_fd, source_name, destination_fd, destination_name):
    if variant is None:
        raise RuntimeError("secure atomic rename primitive is unavailable")
    rename_function, flag = variant
    rename_function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename_function.restype = ctypes.c_int
    result = rename_function(
        source_fd,
        os.fsencode(source_name),
        destination_fd,
        os.fsencode(destination_name),
        flag,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    error_type = (
        FileExistsError
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}
        else OSError
    )
    raise error_type(
        error_number,
        os.strerror(error_number),
        destination_name,
    )


def _rename_exclusive(source_fd, source_name, destination_fd, destination_name):
    _call_atomic_rename(
        _exclusive_rename_variant(),
        source_fd,
        source_name,
        destination_fd,
        destination_name,
    )


def _rename_exchange(source_fd, source_name, destination_fd, destination_name):
    _call_atomic_rename(
        _exchange_rename_variant(),
        source_fd,
        source_name,
        destination_fd,
        destination_name,
    )


def _require_secure_apply_primitives():
    _require_secure_publication_primitives()
    if _exchange_rename_variant() is None:
        raise RuntimeError(
            "secure migration apply is unavailable: atomic exchange rename"
        )


def _publish_staging(
    source_name,
    destination_name,
    source_fd,
    destination_fd,
    staging_fd,
):
    variant = _exclusive_rename_variant()
    if variant is None:
        raise RuntimeError("atomic exclusive directory rename is unavailable")
    rename_function, exclusive_flag = variant
    rename_function.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    rename_function.restype = ctypes.c_int
    os.fchmod(staging_fd, 0o700)
    try:
        result = rename_function(
            source_fd,
            os.fsencode(source_name),
            destination_fd,
            os.fsencode(destination_name),
            exclusive_flag,
        )
    finally:
        os.fchmod(staging_fd, 0o500)
    if result == 0:
        return
    error_number = ctypes.get_errno()
    error_type = (
        FileExistsError
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}
        else OSError
    )
    raise error_type(
        error_number,
        os.strerror(error_number),
        destination_name,
    )


def _inode_from_stat(result):
    return (result.st_dev, result.st_ino)


def _verify_named_directory(pin):
    current = os.stat(pin.path, follow_symlinks=False)
    if not stat.S_ISDIR(current.st_mode) or _inode_from_stat(current) != pin.inode:
        raise RuntimeError(f"pinned directory path changed: {pin.path}")
    if _inode_from_stat(os.fstat(pin.fd)) != pin.inode:
        raise RuntimeError(f"pinned directory descriptor changed: {pin.path}")


def _open_vault_directory(vault):
    fd = os.open(vault, _directory_flags())
    pin = _PinnedDirectory(
        path=Path(vault),
        fd=fd,
        inode=_inode_from_stat(os.fstat(fd)),
        parent_fd=None,
        name=None,
        created=False,
    )
    try:
        _verify_named_directory(pin)
    except Exception:
        os.close(fd)
        raise
    return pin


@contextmanager
def _pinned_vault_directory(vault):
    pin = _open_vault_directory(vault)
    try:
        yield pin
    finally:
        os.close(pin.fd)


def _open_or_create_directory_chain(vault_pin, parts, pins=None):
    pins = [] if pins is None else pins
    parent = vault_pin
    for name in parts:
        created = False
        try:
            fd = os.open(name, _directory_flags(), dir_fd=parent.fd)
        except FileNotFoundError:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent.fd)
                created = True
            except FileExistsError:
                pass
            fd = os.open(name, _directory_flags(), dir_fd=parent.fd)
        pin = _PinnedDirectory(
            path=parent.path / name,
            fd=fd,
            inode=_inode_from_stat(os.fstat(fd)),
            parent_fd=parent.fd,
            name=name,
            created=created,
        )
        pins.append(pin)
        _verify_named_directory(pin)
        parent = pin
    return pins


def _create_staging_directory(vault_pin):
    for _attempt in range(128):
        name = f".agent-memory-beacon-brand-migration-{secrets.token_hex(8)}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=vault_pin.fd)
        except FileExistsError:
            continue
        try:
            fd = os.open(name, _directory_flags(), dir_fd=vault_pin.fd)
        except Exception as primary_error:
            try:
                os.rmdir(name, dir_fd=vault_pin.fd)
            except Exception as cleanup_error:
                raise RuntimeError(
                    "staging allocation failed: "
                    f"{primary_error}; cleanup failed: {cleanup_error}"
                ) from primary_error
            raise
        pin = _PinnedDirectory(
            path=vault_pin.path / name,
            fd=fd,
            inode=_inode_from_stat(os.fstat(fd)),
            parent_fd=vault_pin.fd,
            name=name,
            created=True,
        )
        _verify_named_directory(pin)
        return pin
    raise FileExistsError("could not allocate private migration staging directory")


def _open_relative_parent(root_fd, relative, create=False):
    parts = Path(relative).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe backup destination: {relative}")
    current_fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError:
                    pass
            next_fd = os.open(part, _directory_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd, parts[-1]
    except Exception:
        os.close(current_fd)
        raise


def _copy_backup_file(
    source,
    staging_fd,
    backup_relative,
    expected_hash,
    expected_binding,
):
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    parent_fd = None
    destination_fd = None
    created = False
    try:
        source_stat_before = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat_before.st_mode):
            raise ValueError(f"migration input is not a regular file: {source}")
        if source_stat_before.st_nlink != 1:
            raise RuntimeError(
                f"migration input link count changed during backup: {source}"
            )
        if (
            _input_stat_identity(source_stat_before)
            != _binding_stat_identity(expected_binding)
        ):
            raise RuntimeError(f"migration input changed before copying: {source}")
        parent_fd, leaf = _open_relative_parent(
            staging_fd, backup_relative, create=True
        )
        destination_fd = os.open(
            leaf,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        created = True
        digest = hashlib.sha256()
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        os.fsync(destination_fd)
        if digest.hexdigest() != expected_hash:
            raise RuntimeError(f"migration input changed during backup: {source}")
        source_stat_after = os.fstat(source_fd)
        if _input_stat_identity(source_stat_after) != _input_stat_identity(
            source_stat_before
        ):
            raise RuntimeError(f"migration input changed while copying: {source}")
        if source_stat_after.st_nlink != 1:
            raise RuntimeError(
                f"migration input link count changed during backup: {source}"
            )
    except Exception:
        if destination_fd is not None:
            os.close(destination_fd)
            destination_fd = None
        if created and parent_fd is not None:
            try:
                os.unlink(leaf, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        raise
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(source_fd)


def _hash_backup_file(staging_fd, relative):
    parent_fd, leaf = _open_relative_parent(staging_fd, relative)
    try:
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise RuntimeError(f"backup is not a regular file: {relative}")
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    return digest.hexdigest()
                digest.update(chunk)
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def _serialize_manifest_bytes(payload):
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    return (serialized + "\n").encode("utf-8")


def _hash_fd(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _write_all(fd, content):
    view = memoryview(content)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while preparing migration manifest")
        view = view[written:]


def _write_manifest_temp(staging_fd, payload):
    name = "manifest.json.tmp"
    content = _serialize_manifest_bytes(payload)
    expected_digest = hashlib.sha256(content).hexdigest()
    fd = os.open(
        name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=staging_fd,
    )
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode):
            raise RuntimeError("manifest temp is not a regular file")
        inode = _inode_from_stat(current)
        _write_all(fd, content)
        os.fsync(fd)
        if _hash_fd(fd) != expected_digest:
            raise RuntimeError("manifest digest changed during serialization")
    finally:
        os.close(fd)
    current = os.stat(name, dir_fd=staging_fd, follow_symlinks=False)
    if not stat.S_ISREG(current.st_mode) or _inode_from_stat(current) != inode:
        raise RuntimeError("manifest temp path was replaced during serialization")
    return _SealedFile(
        relative=Path(name),
        inode=inode,
        link_count=current.st_nlink,
        sha256=expected_digest,
        mode=0o600,
        label="manifest",
    )


def _stat_relative(root_fd, relative):
    parent_fd, leaf = _open_relative_parent(root_fd, relative)
    try:
        return os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    finally:
        os.close(parent_fd)


def _verify_file_binding(root_fd, binding):
    current = _stat_relative(root_fd, binding.relative)
    if (
        not stat.S_ISREG(current.st_mode)
        or _inode_from_stat(current) != binding.inode
    ):
        raise RuntimeError(f"{binding.label} path was replaced: {binding.relative}")
    if current.st_nlink != binding.link_count or current.st_nlink != 1:
        raise RuntimeError(
            f"sealed {binding.label} link count changed: {binding.relative}"
        )
    current_digest = _hash_backup_file(root_fd, binding.relative)
    if current_digest != binding.sha256:
        if binding.label == "manifest":
            raise RuntimeError("manifest digest changed")
        raise RuntimeError(f"backup hash changed: {binding.relative}")
    if stat.S_IMODE(current.st_mode) != binding.mode:
        raise RuntimeError(
            f"sealed {binding.label} mode changed: {binding.relative}"
        )


def _inventory_staging_tree(root_fd, relative=Path()):
    current_fd = os.dup(root_fd)
    try:
        files = []
        directories = [relative]
        for name in os.listdir(current_fd):
            child = relative / name
            child_stat = os.stat(
                name, dir_fd=current_fd, follow_symlinks=False
            )
            if stat.S_ISDIR(child_stat.st_mode):
                child_fd = os.open(name, _directory_flags(), dir_fd=current_fd)
                try:
                    child_files, child_directories = _inventory_staging_tree(
                        child_fd, child
                    )
                finally:
                    os.close(child_fd)
                files.extend(child_files)
                directories.extend(child_directories)
            elif stat.S_ISREG(child_stat.st_mode):
                files.append(child)
            else:
                raise RuntimeError(f"unsupported staging entry: {child}")
        return tuple(sorted(files)), tuple(sorted(directories))
    finally:
        os.close(current_fd)


def _open_staging_file(root_fd, relative):
    parent_fd, leaf = _open_relative_parent(root_fd, relative)
    try:
        return os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _validate_open_staging_files(open_files, source_inodes):
    seen_inodes = {}
    for fd, binding in open_files:
        current = os.fstat(fd)
        inode = _inode_from_stat(current)
        if not stat.S_ISREG(current.st_mode) or inode != binding.inode:
            raise RuntimeError(
                f"staging {binding.label} identity changed: {binding.relative}"
            )
        if current.st_nlink != 1:
            if inode in source_inodes:
                raise RuntimeError(
                    "staging hard-link alias to frozen input: "
                    f"{binding.relative}"
                )
            raise RuntimeError(
                f"staging file has multiple hard links: {binding.relative}"
            )
        previous = seen_inodes.get(inode)
        if previous is not None:
            raise RuntimeError(
                "duplicate staging inode between "
                f"{previous} and {binding.relative}"
            )
        if inode in source_inodes:
            raise RuntimeError(
                f"staging hard-link alias to frozen input: {binding.relative}"
            )
        seen_inodes[inode] = binding.relative
        if _hash_fd(fd) != binding.sha256:
            if binding.label == "manifest":
                raise RuntimeError("manifest digest changed")
            raise RuntimeError(f"backup hash changed: {binding.relative}")


def _inspect_staging_files(staging_fd, expected_files, source_inodes):
    open_files = []
    seen_inodes = {}
    try:
        for relative in sorted(
            expected_files,
            key=lambda path: (expected_files[path][1] == "manifest", str(path)),
        ):
            expected_digest, label = expected_files[relative]
            fd = _open_staging_file(staging_fd, relative)
            open_files.append((fd, None))
            current = os.fstat(fd)
            if not stat.S_ISREG(current.st_mode):
                raise RuntimeError(f"{label} is not a regular file: {relative}")
            inode = _inode_from_stat(current)
            if current.st_nlink != 1:
                if inode in source_inodes:
                    raise RuntimeError(
                        f"staging hard-link alias to frozen input: {relative}"
                    )
                raise RuntimeError(
                    f"staging file has multiple hard links: {relative}"
                )
            previous = seen_inodes.get(inode)
            if previous is not None:
                raise RuntimeError(
                    f"duplicate staging inode between {previous} and {relative}"
                )
            if inode in source_inodes:
                raise RuntimeError(
                    f"staging hard-link alias to frozen input: {relative}"
                )
            seen_inodes[inode] = relative
            digest = _hash_fd(fd)
            if digest != expected_digest:
                if label == "manifest":
                    raise RuntimeError("manifest digest changed")
                raise RuntimeError(f"backup hash changed: {relative}")
            open_files[-1] = (
                fd,
                _SealedFile(
                    relative=Path(relative),
                    inode=inode,
                    link_count=current.st_nlink,
                    sha256=expected_digest,
                    mode=0o400,
                    label=label,
                ),
            )
        _validate_open_staging_files(open_files, source_inodes)
        return open_files
    except Exception:
        for fd, _binding in open_files:
            os.close(fd)
        raise


def _open_relative_directory(root_fd, relative):
    if not Path(relative).parts:
        return os.dup(root_fd)
    current_fd = os.dup(root_fd)
    try:
        for part in Path(relative).parts:
            next_fd = os.open(part, _directory_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _seal_staging_tree(staging_fd, expected_files, source_inodes):
    current_files, current_directories = _inventory_staging_tree(staging_fd)
    if set(current_files) != set(expected_files):
        raise RuntimeError("staging file inventory changed before sealing")

    open_files = _inspect_staging_files(
        staging_fd, expected_files, source_inodes
    )
    try:
        _validate_open_staging_files(open_files, source_inodes)
        for fd, _binding in open_files:
            os.fchmod(fd, 0o400)
            os.fsync(fd)
        file_bindings = [binding for _fd, binding in open_files]
    finally:
        for fd, _binding in open_files:
            os.close(fd)

    directory_bindings = []
    for relative in sorted(
        current_directories,
        key=lambda path: (len(path.parts), str(path)),
        reverse=True,
    ):
        fd = _open_relative_directory(staging_fd, relative)
        try:
            current = os.fstat(fd)
            os.fchmod(fd, 0o500)
            os.fsync(fd)
            directory_bindings.append(
                _SealedDirectory(
                    relative=relative,
                    inode=_inode_from_stat(current),
                    mode=0o500,
                )
            )
        finally:
            os.close(fd)
    return tuple(file_bindings), tuple(directory_bindings)


def _verify_sealed_staging(
    staging_fd,
    file_bindings,
    directory_bindings,
    source_inodes,
):
    current_files, current_directories = _inventory_staging_tree(staging_fd)
    if current_files != tuple(sorted(binding.relative for binding in file_bindings)):
        raise RuntimeError("sealed staging file inventory changed")
    if current_directories != tuple(
        sorted(binding.relative for binding in directory_bindings)
    ):
        raise RuntimeError("sealed staging directory inventory changed")
    seen_inodes = {}
    for binding in file_bindings:
        current = _stat_relative(staging_fd, binding.relative)
        inode = _inode_from_stat(current)
        if current.st_nlink != 1:
            raise RuntimeError(
                f"sealed {binding.label} has multiple hard links: {binding.relative}"
            )
        previous = seen_inodes.get(inode)
        if previous is not None:
            raise RuntimeError(
                "duplicate sealed staging inode between "
                f"{previous} and {binding.relative}"
            )
        if inode in source_inodes:
            raise RuntimeError(
                f"sealed staging inode aliases frozen input: {binding.relative}"
            )
        seen_inodes[inode] = binding.relative
        _verify_file_binding(staging_fd, binding)
    for binding in directory_bindings:
        fd = _open_relative_directory(staging_fd, binding.relative)
        try:
            current = os.fstat(fd)
            if _inode_from_stat(current) != binding.inode:
                raise RuntimeError(
                    f"sealed staging directory replaced: {binding.relative}"
                )
            if stat.S_IMODE(current.st_mode) != binding.mode:
                raise RuntimeError(
                    f"sealed staging directory mode changed: {binding.relative}"
                )
        finally:
            os.close(fd)


def _remove_tree_at(parent_fd, name, expected_inode=None):
    current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if expected_inode is not None and _inode_from_stat(current) != expected_inode:
        raise RuntimeError(f"refusing to clean replaced backup path: {name}")
    if not stat.S_ISDIR(current.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return
    fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        os.fchmod(fd, 0o700)
        for child in os.listdir(fd):
            child_stat = os.stat(child, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(child_stat.st_mode):
                _remove_tree_at(fd, child, _inode_from_stat(child_stat))
            else:
                os.unlink(child, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def _remove_owned_tree_at(parent_fd, preferred_name, expected_inode):
    try:
        _remove_tree_at(parent_fd, preferred_name, expected_inode)
        return
    except FileNotFoundError:
        pass
    for candidate in os.listdir(parent_fd):
        current = os.stat(candidate, dir_fd=parent_fd, follow_symlinks=False)
        if _inode_from_stat(current) == expected_inode:
            _remove_tree_at(parent_fd, candidate, expected_inode)
            return
    raise RuntimeError(
        f"owned backup inode {expected_inode} was not found in pinned parent"
    )


def _cleanup_backup_failure(primary_error, staging_pin, published, brand_pin, pins):
    cleanup_errors = []
    if staging_pin is not None:
        try:
            parent_fd = brand_pin.fd if published else staging_pin.parent_fd
            name = published if published else staging_pin.name
            _remove_owned_tree_at(parent_fd, name, staging_pin.inode)
        except Exception as exc:
            cleanup_errors.append(exc)
    for pin in reversed(pins):
        if not pin.created:
            continue
        try:
            os.rmdir(pin.name, dir_fd=pin.parent_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            if exc.errno == errno.ENOTEMPTY:
                continue
            cleanup_errors.append(exc)
        except Exception as exc:
            cleanup_errors.append(exc)
    if cleanup_errors:
        detail = "; ".join(str(error) for error in cleanup_errors)
        raise RuntimeError(
            f"backup failed: {primary_error}; cleanup failed: {detail}"
        ) from primary_error


def _manifest_target_spec(plan, spec):
    return {
        "role": spec.role,
        "key": spec.key,
        **_manifest_path_record(plan, spec.path),
        "expected_state": spec.expected_state,
        "mutation_kind": spec.mutation_kind,
        "entry_kind": spec.entry_kind,
        "inode": list(spec.inode) if spec.inode is not None else None,
    }


def _manifest_input_binding(plan, binding):
    return {
        **_manifest_path_record(plan, binding.path),
        "sha256": binding.sha256,
        "inode": list(binding.inode),
        "link_count": binding.link_count,
        "mode": binding.mode,
        "size": binding.size,
        "mtime_ns": binding.mtime_ns,
        "ctime_ns": binding.ctime_ns,
        "expected_type": binding.expected_type,
    }


def _manifest_directory_binding(plan, binding):
    return {
        **_manifest_path_record(plan, binding.path),
        "inode": list(binding.inode),
        "mode": binding.mode,
        "atime_ns": binding.atime_ns,
        "mtime_ns": binding.mtime_ns,
        "expected_type": binding.expected_type,
    }


def _validate_manifest_records(plan, records):
    if len(records) != len(plan.input_bindings):
        raise ValueError("manifest files do not match frozen input bindings")
    for binding, record in zip(plan.input_bindings, records):
        expected_path = _manifest_path_record(plan, binding.path)
        if (
            record.get("kind") != expected_path["kind"]
            or record.get("path") != expected_path["path"]
            or record.get("sha256") != binding.sha256
        ):
            raise ValueError("manifest files do not match frozen input bindings")


def _migration_manifest(plan, migration_id, records):
    _validate_input_binding_collection(plan)
    _validate_manifest_records(plan, records)
    contract = plan.mutation_contract
    return {
        "schema_version": MIGRATION_MANIFEST_SCHEMA_VERSION,
        "generated_by": CODE_PREFIX,
        "migration_id": migration_id,
        "status": "prepared",
        "old_slug": plan.old_slug,
        "new_slug": plan.new_slug,
        "vault": str(plan.vault),
        "files": records,
        "input_bindings": [
            _manifest_input_binding(plan, binding)
            for binding in plan.input_bindings
        ],
        "directory_bindings": [
            _manifest_directory_binding(plan, binding)
            for binding in plan.directory_bindings
        ],
        "source_directories": [
            path.relative_to(plan.vault).as_posix()
            for path in plan.source_directories
        ],
        "public_paths_before": [
            path.relative_to(plan.vault).as_posix()
            for path in plan.public_paths_before
        ],
        "broken_links_before": [list(item) for item in plan.broken_links_before],
        "memory_identity_count_before": plan.memory_identity_count_before,
        "memory_identity_keys_before": list(plan.memory_identity_keys_before),
        "mutation_contract": {
            "target_specs": [
                _manifest_target_spec(plan, spec) for spec in contract.target_specs
            ],
            "mutable_roots": [
                _manifest_path_record(plan, path) for path in contract.mutable_roots
            ],
            "excluded_mutable_subtrees": [
                _manifest_path_record(plan, path)
                for path in contract.excluded_mutable_subtrees
            ],
            "mutable_directories": [
                _manifest_path_record(plan, path)
                for path in contract.mutable_directories
            ],
            "absent_paths": [
                _manifest_path_record(plan, path) for path in contract.absent_paths
            ],
            "absent_directories": [
                _manifest_path_record(plan, path)
                for path in contract.absent_directories
            ],
            "mutable_files": [
                _manifest_path_record(plan, path) for path in contract.mutable_files
            ],
        },
        "mutable_roots": [
            _manifest_path_record(plan, path) for path in plan.mutable_roots
        ],
        "excluded_mutable_subtrees": [
            _manifest_path_record(plan, path)
            for path in plan.excluded_mutable_subtrees
        ],
        "mutable_directories_before": [
            _manifest_path_record(plan, path)
            for path in plan.mutable_directories_before
        ],
        "configured_target_states": [
            {**_manifest_path_record(plan, path), "state": state}
            for path, state in plan.configured_target_states
        ],
        "absent_paths_before": [
            _manifest_path_record(plan, path) for path in plan.absent_paths_before
        ],
        "created_paths": [],
        "post_hashes": {},
    }


def create_migration_backup(
    plan: MigrationPlan,
    migration_id: str,
    allowed_created_directories=None,
) -> Path:
    normalized_id = normalize_project_slug(migration_id)
    if not normalized_id or normalized_id != migration_id:
        raise ValueError(f"invalid migration id: {migration_id}")
    backup_root = Path(
        safe_vault_path(
            plan.vault,
            "04-Feedback",
            "_rollback",
            "brand-migration",
            migration_id,
        )
    )
    _require_secure_publication_primitives()
    _assert_no_symlink_below(plan.vault, backup_root, "backup destination")
    if os.path.lexists(backup_root):
        raise FileExistsError(f"migration backup already exists: {backup_root}")
    _assert_plan_unchanged(
        plan, allowed_created_directories=allowed_created_directories
    )
    bindings_by_path = {
        binding.path: binding for binding in plan.input_bindings
    }
    expected_hashes = {
        binding.path: binding.sha256 for binding in plan.input_bindings
    }
    source_inodes = frozenset(
        binding.inode for binding in plan.input_bindings
    )
    vault_pin = None
    parent_pins = []
    staging_pin = None
    published_name = None
    try:
        vault_pin = _open_vault_directory(plan.vault)
        _open_or_create_directory_chain(
            vault_pin,
            ("04-Feedback", "_rollback", "brand-migration"),
            parent_pins,
        )
        brand_pin = parent_pins[-1]
        try:
            leaf = os.stat(
                migration_id, dir_fd=brand_pin.fd, follow_symlinks=False
            )
        except FileNotFoundError:
            leaf = None
        if leaf is not None:
            if stat.S_ISLNK(leaf.st_mode):
                raise ValueError(f"backup destination contains symlink: {backup_root}")
            raise FileExistsError(f"migration backup already exists: {backup_root}")

        staging_pin = _create_staging_directory(vault_pin)
        allowed_directories = {
            pin.path: pin.inode for pin in parent_pins if pin.created
        }
        allowed_directories.update(allowed_created_directories or {})
        _assert_plan_unchanged(
            plan,
            excluded_public_root=staging_pin.path,
            allowed_created_directories=allowed_directories,
        )
        entries = _backup_entries(plan, staging_pin.path)
        entry_relatives = {
            source: backup.relative_to(staging_pin.path)
            for source, backup, _restore_target in entries
        }
        records = []
        for source, backup, restore_target in entries:
            binding = bindings_by_path[source]
            expected_hash = expected_hashes[source]
            backup_relative = entry_relatives[source]
            _copy_backup_file(
                source,
                staging_pin.fd,
                backup_relative,
                expected_hash,
                binding,
            )
            _revalidate_input_binding(binding, "during backup")
            if _hash_backup_file(staging_pin.fd, backup_relative) != expected_hash:
                raise RuntimeError(f"backup hash changed during copy: {backup_relative}")
            records.append(
                {
                    **restore_target,
                    "backup": backup_relative.as_posix(),
                    "sha256": expected_hash,
                }
            )

        manifest_binding = _write_manifest_temp(
            staging_pin.fd,
            _migration_manifest(plan, migration_id, records),
        )
        _verify_file_binding(staging_pin.fd, manifest_binding)

        os.mkdir(
            MIGRATION_JOURNAL_DIRECTORY,
            mode=0o700,
            dir_fd=staging_pin.fd,
        )
        os.rename(
            "manifest.json.tmp",
            "manifest.json",
            src_dir_fd=staging_pin.fd,
            dst_dir_fd=staging_pin.fd,
        )
        manifest_binding = _SealedFile(
            relative=Path("manifest.json"),
            inode=manifest_binding.inode,
            link_count=manifest_binding.link_count,
            sha256=manifest_binding.sha256,
            mode=manifest_binding.mode,
            label=manifest_binding.label,
        )
        _verify_file_binding(staging_pin.fd, manifest_binding)

        expected_staging_files = {
            entry_relatives[source]: (expected_hashes[source], "backup")
            for source, _backup, _restore_target in entries
        }
        expected_staging_files[manifest_binding.relative] = (
            manifest_binding.sha256,
            "manifest",
        )
        sealed_files, sealed_directories = _seal_staging_tree(
            staging_pin.fd,
            expected_staging_files,
            source_inodes,
        )
        _assert_plan_unchanged(
            plan,
            excluded_public_root=staging_pin.path,
            allowed_created_directories=allowed_directories,
        )
        _verify_sealed_staging(
            staging_pin.fd,
            sealed_files,
            sealed_directories,
            source_inodes,
        )
        _verify_named_directory(vault_pin)
        _verify_named_directory(staging_pin)
        for pin in parent_pins:
            _verify_named_directory(pin)
        if os.path.lexists(backup_root):
            raise FileExistsError(f"migration backup already exists: {backup_root}")

        _publish_staging(
            staging_pin.name,
            migration_id,
            vault_pin.fd,
            brand_pin.fd,
            staging_pin.fd,
        )
        published_name = migration_id
        try:
            for pin in parent_pins:
                _verify_named_directory(pin)
            published = os.stat(
                migration_id, dir_fd=brand_pin.fd, follow_symlinks=False
            )
            if _inode_from_stat(published) != staging_pin.inode:
                raise RuntimeError("published backup inode changed")
            final = os.stat(backup_root, follow_symlinks=False)
            if (
                not stat.S_ISDIR(final.st_mode)
                or _inode_from_stat(final) != staging_pin.inode
            ):
                raise RuntimeError("published backup path changed")
        except Exception:
            raise
        _assert_plan_unchanged(
            plan,
            allowed_created_directories=allowed_directories,
        )
        _verify_sealed_staging(
            staging_pin.fd,
            sealed_files,
            sealed_directories,
            source_inodes,
        )
        return backup_root / "manifest.json"
    except Exception as primary_error:
        if vault_pin is not None:
            brand_pin = parent_pins[-1] if parent_pins else None
            if (
                published_name is None
                and staging_pin is not None
                and brand_pin is not None
            ):
                try:
                    possible_published = os.stat(
                        migration_id,
                        dir_fd=brand_pin.fd,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    possible_published = None
                if (
                    possible_published is not None
                    and stat.S_ISDIR(possible_published.st_mode)
                    and _inode_from_stat(possible_published) == staging_pin.inode
                ):
                    published_name = migration_id
            _cleanup_backup_failure(
                primary_error,
                staging_pin,
                published_name,
                brand_pin,
                parent_pins,
            )
        raise
    finally:
        if staging_pin is not None:
            os.close(staging_pin.fd)
        for pin in reversed(parent_pins):
            os.close(pin.fd)
        if vault_pin is not None:
            os.close(vault_pin.fd)


def _count_project_value_refs(value, old_slug, seen=None):
    seen = set() if seen is None else seen
    if isinstance(value, str):
        return int(value == old_slug) + int(
            _project_target_matches(value, old_slug)
        )
    if not isinstance(value, (list, dict)):
        return 0
    identity = id(value)
    if identity in seen:
        return 0
    seen.add(identity)
    try:
        if isinstance(value, list):
            return sum(
                _count_project_value_refs(item, old_slug, seen) for item in value
            )
        return _count_project_value_refs(value.get("name"), old_slug, seen)
    finally:
        seen.remove(identity)


def _count_project_field_refs(value, old_slug, seen=None):
    seen = set() if seen is None else seen
    if isinstance(value, list):
        identity = id(value)
        if identity in seen:
            return 0
        seen.add(identity)
        try:
            return sum(
                _count_project_field_refs(item, old_slug, seen) for item in value
            )
        finally:
            seen.remove(identity)
    if not isinstance(value, dict):
        return 0
    identity = id(value)
    if identity in seen:
        return 0
    seen.add(identity)
    try:
        count = 0
        for key, item in value.items():
            key_text = str(key)
            if key_text in PROJECT_KEYS:
                count += _count_project_value_refs(item, old_slug, seen)
            elif key_text in VAULT_REFERENCE_KEYS and isinstance(item, str):
                count += int(_project_target_matches(item, old_slug))
            else:
                count += _count_project_field_refs(item, old_slug, seen)
        return count
    finally:
        seen.remove(identity)


def _count_live_old_wikilinks(body, old_slug):
    count = 0
    for line in strip_markdown_code_blocks(body).splitlines():
        for raw in WIKI_LINK.findall(line):
            target, _alias = split_wikilink(raw)
            count += int(_project_target_matches(target, old_slug))
    return count


def count_structural_old_refs(plan):
    count = 0
    paths = set(
        _iter_public_markdown(
            plan.vault,
            excluded_roots=plan.mutation_contract.excluded_mutable_subtrees,
        )
    )
    paths.update(_profile_shared_markdown_paths(plan.mutation_contract))
    for path in sorted(paths):
        content = _read_utf8(path, "Markdown")
        frontmatter_text, body = _split_frontmatter_content(content)
        parsed = yaml.safe_load(frontmatter_text) if frontmatter_text is not None else {}
        if isinstance(parsed, dict):
            count += _count_project_field_refs(parsed, plan.old_slug)
        count += _count_live_old_wikilinks(body, plan.old_slug)
    if plan.config_path is not None:
        parsed = yaml.safe_load(_read_utf8(plan.config_path, "config")) or {}
        if isinstance(parsed, dict):
            count += _count_project_field_refs(parsed, plan.old_slug)
            keywords = parsed.get("project_keywords") or {}
            if isinstance(keywords, dict):
                count += int(plan.old_slug in keywords)
    return count


def validate_brand_migration(plan):
    source_missing = not os.path.lexists(plan.source_project)
    destination_present = (
        plan.destination_project.is_dir()
        and not plan.destination_project.is_symlink()
    )
    structural_old_refs = count_structural_old_refs(plan)
    broken_after = set(
        _normalized_broken_links(
            plan.vault,
            plan.old_slug,
            plan.new_slug,
            excluded_roots=plan.mutation_contract.excluded_mutable_subtrees,
            additional_markdown_paths=_profile_shared_markdown_paths(
                plan.mutation_contract
            ),
        )
    )
    new_broken = broken_after - set(plan.broken_links_before)
    identities = (
        memory_identity_keys(plan.destination_project)
        if destination_present
        else ()
    )
    duplicate_memories = len(identities) - len(set(identities))
    identity_keys_match = identities == plan.memory_identity_keys_before
    failures = []
    if not source_missing:
        failures.append("legacy project directory still exists")
    if not destination_present:
        failures.append("new project directory is missing")
    if structural_old_refs:
        failures.append(f"{structural_old_refs} structural legacy references remain")
    if new_broken:
        failures.append(f"{len(new_broken)} new broken links")
    if duplicate_memories:
        failures.append(f"{duplicate_memories} duplicate memory identities")
    if not identity_keys_match:
        failures.append("memory identity keys changed")
    return {
        "valid": not failures,
        "message": "; ".join(failures) if failures else "migration valid",
        "structural_old_refs": structural_old_refs,
        "new_broken_links": len(new_broken),
        "duplicate_memories": duplicate_memories,
        "memory_identity_count_before": len(plan.memory_identity_keys_before),
        "memory_identity_count_after": len(identities),
        "memory_identity_keys_match": identity_keys_match,
    }


MIGRATION_LOCK_RENEW_INTERVAL_SECONDS = 60.0


@dataclass
class _OwnedMigrationLock:
    path: Path
    parent_fd: int
    fd: int
    inode: tuple[int, int]
    token: str
    label: str
    persistent_flock: bool = False
    closed: bool = False
    _mutex: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def _named_stat(self):
        return os.stat(
            self.path.name,
            dir_fd=self.parent_fd,
            follow_symlinks=False,
        )

    def assert_owned(self):
        with self._mutex:
            if self.closed:
                raise RuntimeError(f"writer guard ownership lost: {self.label} closed")
            try:
                named = self._named_stat()
            except FileNotFoundError as exc:
                raise RuntimeError(
                    f"writer guard ownership lost: {self.label} lock disappeared"
                ) from exc
            opened = os.fstat(self.fd)
            if (
                not stat.S_ISREG(named.st_mode)
                or named.st_nlink != 1
                or _inode_from_stat(named) != self.inode
                or not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or _inode_from_stat(opened) != self.inode
            ):
                raise RuntimeError(
                    f"writer guard ownership lost: {self.label} lock replaced"
                )
            raw = _read_fd_bytes(self.fd)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(
                    f"writer guard ownership lost: {self.label} token unreadable"
                ) from exc
            if payload.get("token") != self.token:
                raise RuntimeError(
                    f"writer guard ownership lost: {self.label} token changed"
                )

    def _write_lease(self):
        with self._mutex:
            payload = _serialize_manifest_bytes(
                {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "owner": "agent-memory-beacon-brand-migration",
                    "pid": os.getpid(),
                    "token": self.token,
                }
            )
            os.ftruncate(self.fd, 0)
            os.lseek(self.fd, 0, os.SEEK_SET)
            _write_all(self.fd, payload)
            os.fsync(self.fd)

    def renew(self):
        with self._mutex:
            self.assert_owned()
            self._write_lease()
            self.assert_owned()

    def release(self):
        with self._mutex:
            self.assert_owned()
            release_name = f".{self.path.name}.release-{secrets.token_hex(16)}"
            _rename_exclusive(
                self.parent_fd,
                self.path.name,
                self.parent_fd,
                release_name,
            )
            moved = _named_stat(self.parent_fd, release_name)
            moved_inode = _inode_from_stat(moved) if moved is not None else None
            token_matches = False
            try:
                payload = json.loads(_read_fd_bytes(self.fd).decode("utf-8"))
                token_matches = payload.get("token") == self.token
            except (UnicodeDecodeError, json.JSONDecodeError):
                token_matches = False
            if moved_inode != self.inode or not token_matches:
                try:
                    _rename_exclusive(
                        self.parent_fd,
                        release_name,
                        self.parent_fd,
                        self.path.name,
                    )
                    restored = self._named_stat()
                    if _inode_from_stat(restored) != moved_inode:
                        raise RuntimeError("restored lock inode changed")
                except Exception as recovery_error:
                    raise RuntimeError(
                        f"writer guard ownership lost: {self.label} release recovery failed: "
                        f"{recovery_error}"
                    ) from recovery_error
                raise RuntimeError(
                    f"writer guard ownership lost: {self.label} lock replaced during release"
                )
            os.unlink(release_name, dir_fd=self.parent_fd)
            if _named_stat(self.parent_fd, release_name) is not None:
                raise RuntimeError(
                    f"writer guard ownership lost: {self.label} release cleanup raced"
                )
            self.close_without_unlink()

    def close_without_unlink(self):
        with self._mutex:
            if self.closed:
                return
            if self.persistent_flock:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            os.close(self.parent_fd)
            self.closed = True


def _acquire_migration_lock(
    path,
    label,
    root_fd=None,
    relative=None,
    persistent_flock=False,
):
    path = Path(path)
    if root_fd is None:
        create_directories = (path.parent, path.parent.parent)
        parent_fd, leaf = _open_absolute_parent(
            path,
            create_directories=create_directories,
        )
    else:
        if relative is None:
            raise ValueError("pinned migration lock requires a relative path")
        parent_fd, leaf = _open_relative_parent(root_fd, relative)
    token = secrets.token_hex(32)
    created_exclusive = False
    try:
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
        if not persistent_flock:
            flags |= os.O_EXCL
        fd = os.open(leaf, flags, 0o600, dir_fd=parent_fd)
        created_exclusive = not persistent_flock
    except FileExistsError as exc:
        os.close(parent_fd)
        raise RuntimeError(f"{label} is active; migration did not start") from exc
    if persistent_flock:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            os.close(fd)
            os.close(parent_fd)
            raise RuntimeError(
                f"{label} is active; migration did not start"
            ) from exc
    try:
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise RuntimeError(f"{label} lock is unsafe")
        lease = _OwnedMigrationLock(
            path=path,
            parent_fd=parent_fd,
            fd=fd,
            inode=_inode_from_stat(current),
            token=token,
            label=label,
            persistent_flock=persistent_flock,
        )
        lease._write_lease()
        lease.assert_owned()
        return lease
    except Exception:
        try:
            named = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
            if created_exclusive and _inode_from_stat(named) == _inode_from_stat(os.fstat(fd)):
                os.unlink(leaf, dir_fd=parent_fd)
        except (FileNotFoundError, OSError):
            pass
        if persistent_flock:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
        os.close(parent_fd)
        raise


class _MigrationWriterGuard:
    def __init__(self, leases, allowed_created_directories):
        self.leases = leases
        self.allowed_created_directories = allowed_created_directories
        self._stop = threading.Event()
        self._lost = None
        self._thread = None
        self._closed = False

    def start(self):
        self._thread = threading.Thread(
            target=self._renew_loop,
            name="brand-migration-lock-renewer",
            daemon=False,
        )
        self._thread.start()

    def _renew_loop(self):
        while not self._stop.wait(MIGRATION_LOCK_RENEW_INTERVAL_SECONDS):
            try:
                self.renew()
            except Exception as exc:
                self._lost = exc
                self._stop.set()
                return

    def assert_owned(self):
        if self._lost is not None:
            raise RuntimeError(f"writer guard ownership lost: {self._lost}") from self._lost
        try:
            for lease in self.leases:
                lease.assert_owned()
        except Exception as exc:
            if self._lost is None:
                self._lost = exc
            self._stop.set()
            raise

    def renew(self):
        for lease in self.leases:
            lease.renew()

    def close(self):
        if self._closed:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        if self._lost is not None:
            for lease in reversed(self.leases):
                lease.close_without_unlink()
            self._closed = True
            raise RuntimeError(f"writer guard ownership lost: {self._lost}") from self._lost
        errors = []
        for lease in reversed(self.leases):
            try:
                lease.release()
            except Exception as exc:
                errors.append(exc)
                lease.close_without_unlink()
        self._closed = True
        if errors:
            detail = "; ".join(str(error) for error in errors)
            raise RuntimeError(f"writer guard ownership lost: {detail}")


@contextmanager
def migration_writer_guard(vault, vault_pin=None):
    vault = Path(vault)
    harvest_lock = vault / "04-Feedback" / "_logs" / "harvester.lock"
    scanner_lock = vault / "04-Feedback" / "scanner.lock"
    guard_directories = (
        vault / "04-Feedback",
        vault / "04-Feedback" / "_logs",
    )
    guard_parent_pins = []
    if vault_pin is not None:
        if vault_pin.path != vault:
            raise ValueError("writer guard Vault does not match pinned Vault")
        _verify_named_directory(vault_pin)
        _open_or_create_directory_chain(
            vault_pin,
            ("04-Feedback", "_logs"),
            guard_parent_pins,
        )
        absent_before = {pin.path for pin in guard_parent_pins if pin.created}
    else:
        absent_before = {
            path for path in guard_directories if not os.path.lexists(path)
        }
    leases = []
    try:
        leases.append(
            _acquire_migration_lock(
                harvest_lock,
                "harvester",
                root_fd=vault_pin.fd if vault_pin is not None else None,
                relative=Path("04-Feedback/_logs/harvester.lock")
                if vault_pin is not None
                else None,
                persistent_flock=True,
            )
        )
        leases.append(
            _acquire_migration_lock(
                scanner_lock,
                "weekly scanner",
                root_fd=vault_pin.fd if vault_pin is not None else None,
                relative=Path("04-Feedback/scanner.lock")
                if vault_pin is not None
                else None,
            )
        )
        allowed_created = {}
        for path in absent_before:
            current = os.stat(path, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode):
                raise RuntimeError(f"writer guard parent changed type: {path}")
            allowed_created[path] = _inode_from_stat(current)
        guard = _MigrationWriterGuard(tuple(leases), allowed_created)
        guard.start()
        if vault_pin is not None:
            _verify_named_directory(vault_pin)
    except BaseException as primary_error:
        cleanup_errors = []
        for lease in reversed(leases):
            if lease.closed:
                continue
            try:
                lease.release()
            except Exception as cleanup_error:
                cleanup_errors.append(cleanup_error)
                lease.close_without_unlink()
        for pin in reversed(guard_parent_pins):
            try:
                os.close(pin.fd)
            except OSError as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            detail = "; ".join(str(error) for error in cleanup_errors)
            raise RuntimeError(
                f"{primary_error}; partial writer guard cleanup failed: {detail}"
            ) from primary_error
        raise

    primary_error = None
    try:
        yield guard
        guard.assert_owned()
        if vault_pin is not None:
            _verify_named_directory(vault_pin)
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        try:
            guard.close()
        except Exception as close_error:
            if primary_error is not None:
                raise RuntimeError(
                    f"{primary_error}; writer guard release failed: {close_error}"
                ) from primary_error
            raise
        finally:
            for pin in reversed(guard_parent_pins):
                os.close(pin.fd)


def _run_default_rebuilders(cfg, guard, mutation_io, checkpoint=None):
    phases = (
        ("memory-index", lambda: rebuild_memory_index(
            cfg,
            ownership_check=guard.assert_owned,
            mutation_io=mutation_io,
        )),
        ("maps", lambda: rebuild_maps(
            cfg["vault_path"],
            ownership_check=guard.assert_owned,
            mutation_io=mutation_io,
        )),
        ("compiler", lambda: compile_agent_context(
            cfg,
            ownership_check=guard.assert_owned,
            mutation_io=mutation_io,
        )),
    )
    for phase, rebuild in phases:
        guard.assert_owned()
        checkpoint_phase = f"rebuild:{phase}"
        mutation_io.begin_rebuild_phase(checkpoint_phase)
        try:
            rebuild()
        except Exception:
            mutation_io.abort_rebuild_phase()
            raise
        guard.assert_owned()
        mutation_io.finish_rebuild_phase()


def load_migration_config(plan):
    specs = plan.mutation_contract.target_specs

    def first_path(role, key=None):
        for spec in specs:
            if spec.role == role and (key is None or spec.key == key):
                return spec.path
        return None

    def root_path(key):
        for spec in specs:
            if spec.role == "mutable_root" and key in spec.key.split("+"):
                return spec.path
        raise ValueError(f"frozen rebuilder root is missing: {key}")

    cfg = {
        "vault_path": str(plan.vault),
        "skip_git_probe": True,
        "migration_paths_are_canonical": True,
        "agent_memory_path": str(root_path("agent_memory")),
        "memory_index_path": str(first_path("memory_index_path", "index")),
        "context_targets": [
            str(spec.path)
            for spec in specs
            if spec.role == "context"
        ],
    }
    for section in ("personal_memory", "skill_preferences", "workflow_memory"):
        formal = first_path(section, "formal")
        cfg[section] = {
            "candidate_dir": str(root_path(f"{section}_candidates")),
            "formal_path": str(formal),
        }
    profile_shared = first_path("profile", "AGENTS.shared.md")
    if profile_shared is not None:
        cfg["codex_profile_path"] = str(profile_shared.parent)
    return cfg


def _post_migration_path(path, old_root, new_root):
    path = Path(path)
    try:
        relative = path.relative_to(old_root)
    except ValueError:
        return path
    return new_root / relative


def _record_to_path(vault, record, label="manifest path"):
    if not isinstance(record, dict):
        raise ValueError(f"{label} must be an object")
    kind = record.get("kind")
    raw = record.get("path")
    if kind not in {"vault", "external"} or not isinstance(raw, str) or not raw:
        raise ValueError(f"{label} has invalid kind/path")
    if kind == "vault":
        relative = Path(raw)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError(f"{label} has unsafe Vault path")
        path = Path(safe_vault_path(vault, *relative.parts))
        if path.relative_to(vault).as_posix() != raw:
            raise ValueError(f"{label} is not canonical")
        return path
    path = Path(raw)
    if not path.is_absolute() or path != path.resolve(strict=False):
        raise ValueError(f"{label} has non-canonical external path")
    if path.is_relative_to(vault):
        raise ValueError(f"{label} has forged external kind")
    return path


def _manifest_contract_from_payload(payload, vault):
    raw = payload.get("mutation_contract")
    if not isinstance(raw, dict):
        raise ValueError("manifest mutation contract is missing")
    specs = []
    for item in raw.get("target_specs", ()):
        if not isinstance(item, dict):
            raise ValueError("manifest mutation contract target is invalid")
        path = _record_to_path(vault, item, "manifest mutation contract target")
        inode = item.get("inode")
        inode = tuple(inode) if inode is not None else None
        if (
            not isinstance(item.get("role"), str)
            or not item["role"]
            or not isinstance(item.get("key"), str)
            or not item["key"]
            or item.get("expected_state") not in {"absent", "file", "directory"}
            or item.get("mutation_kind") not in {
                "rewrite_delete_create",
                "rewrite",
                "temporary",
                "create_remove",
                "rewrite_delete",
            }
            or item.get("entry_kind") not in {
                "root",
                "file",
                "temp_sibling",
                "parent_directory",
            }
            or (item["expected_state"] == "absent") != (inode is None)
            or (
                inode is not None
                and (
                    len(inode) != 2
                    or not all(isinstance(value, int) for value in inode)
                )
            )
        ):
            raise ValueError("manifest mutation contract target is invalid")
        specs.append(
            TargetSpec(
                role=item.get("role"),
                key=item.get("key"),
                path=path,
                expected_state=item.get("expected_state"),
                mutation_kind=item.get("mutation_kind"),
                entry_kind=item.get("entry_kind"),
                inode=inode,
            )
        )

    def paths(key):
        values = raw.get(key)
        if not isinstance(values, list):
            raise ValueError(f"manifest mutation contract {key} is invalid")
        return tuple(
            _record_to_path(vault, item, f"manifest mutation contract {key}")
            for item in values
        )

    contract = MutationContract(
        target_specs=tuple(specs),
        mutable_roots=paths("mutable_roots"),
        excluded_mutable_subtrees=paths("excluded_mutable_subtrees"),
        mutable_directories=paths("mutable_directories"),
        absent_paths=paths("absent_paths"),
        absent_directories=paths("absent_directories"),
        mutable_files=paths("mutable_files"),
    )
    try:
        _validate_contract_collections(contract)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"manifest mutation contract is invalid: {exc}") from exc
    if contract.mutable_directories != tuple(sorted(contract.mutable_directories)):
        raise ValueError("manifest mutation contract directories are not sorted")
    if any(
        not any(path == root or path.is_relative_to(root) for root in contract.mutable_roots)
        for path in contract.mutable_directories
    ):
        raise ValueError("manifest mutation contract directory is outside mutable roots")
    return contract


def _manifest_bindings_from_payload(payload, vault):
    raw_bindings = payload.get("input_bindings")
    if not isinstance(raw_bindings, list):
        raise ValueError("manifest input bindings are missing")
    bindings = []
    for item in raw_bindings:
        try:
            binding = InputBinding(
                path=_record_to_path(vault, item, "manifest input binding"),
                sha256=item["sha256"],
                inode=tuple(item["inode"]),
                link_count=item["link_count"],
                mode=item["mode"],
                size=item["size"],
                mtime_ns=item["mtime_ns"],
                ctime_ns=item["ctime_ns"],
                expected_type=item["expected_type"],
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("manifest input binding is invalid") from exc
        if (
            binding.expected_type != "regular_file"
            or binding.link_count != 1
            or not isinstance(binding.mode, int)
            or not stat.S_ISREG(binding.mode)
            or not isinstance(binding.size, int)
            or binding.size < 0
            or not isinstance(binding.mtime_ns, int)
            or not isinstance(binding.ctime_ns, int)
            or len(binding.inode) != 2
            or not all(isinstance(value, int) for value in binding.inode)
            or not isinstance(binding.sha256, str)
            or len(binding.sha256) != 64
        ):
            raise ValueError("manifest input binding is invalid")
        bindings.append(binding)
    if len({item.path for item in bindings}) != len(bindings):
        raise ValueError("manifest input bindings contain duplicate paths")
    if len({item.inode for item in bindings}) != len(bindings):
        raise ValueError("manifest input bindings contain duplicate inodes")
    return tuple(bindings)


def _manifest_directory_bindings_from_payload(payload, vault):
    raw_bindings = payload.get("directory_bindings")
    if not isinstance(raw_bindings, list):
        raise ValueError("manifest directory bindings are missing")
    bindings = []
    for item in raw_bindings:
        try:
            binding = DirectoryBinding(
                path=_record_to_path(vault, item, "manifest directory binding"),
                inode=tuple(item["inode"]),
                mode=item["mode"],
                atime_ns=item["atime_ns"],
                mtime_ns=item["mtime_ns"],
                expected_type=item["expected_type"],
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("manifest directory binding is invalid") from exc
        if (
            binding.expected_type != "directory"
            or len(binding.inode) != 2
            or not all(isinstance(value, int) for value in binding.inode)
            or not isinstance(binding.mode, int)
            or not stat.S_ISDIR(binding.mode)
            or not isinstance(binding.atime_ns, int)
            or not isinstance(binding.mtime_ns, int)
        ):
            raise ValueError("manifest directory binding is invalid")
        bindings.append(binding)
    if (
        len({item.path for item in bindings}) != len(bindings)
        or len({item.inode for item in bindings}) != len(bindings)
    ):
        raise ValueError("manifest directory bindings are not unique")
    return tuple(bindings)


def _validate_manifest_payload(payload, expected_plan=None):
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != MIGRATION_MANIFEST_SCHEMA_VERSION
    ):
        raise ValueError("unsupported brand migration manifest schema")
    if payload.get("generated_by") != CODE_PREFIX:
        raise ValueError("invalid brand migration manifest generator")
    migration_id = payload.get("migration_id")
    if (
        not isinstance(migration_id, str)
        or not migration_id
        or normalize_project_slug(migration_id) != migration_id
    ):
        raise ValueError("invalid brand migration manifest id")
    vault = Path(str(payload.get("vault", "")))
    if not vault.is_absolute() or vault != vault.resolve(strict=False):
        raise ValueError("invalid brand migration manifest Vault")
    if payload.get("old_slug") != LEGACY_PROJECT_SLUG or payload.get("new_slug") != PROJECT_SLUG:
        raise ValueError("invalid brand migration manifest branding")
    contract = _manifest_contract_from_payload(payload, vault)
    bindings = _manifest_bindings_from_payload(payload, vault)
    directory_bindings = _manifest_directory_bindings_from_payload(payload, vault)
    files = payload.get("files")
    if not isinstance(files, list) or len(files) != len(bindings):
        raise ValueError("manifest files do not match frozen input bindings")
    seen_backups = set()
    for binding, item in zip(bindings, files):
        path = _record_to_path(vault, item, "manifest file")
        backup = item.get("backup")
        if (
            path != binding.path
            or item.get("sha256") != binding.sha256
            or not isinstance(backup, str)
        ):
            raise ValueError("manifest files do not match frozen input bindings")
        relative = Path(backup)
        if (
            relative.is_absolute()
            or any(part in {"", ".", ".."} for part in relative.parts)
            or relative.parts[0] not in {"vault", "external"}
            or backup in seen_backups
        ):
            raise ValueError("manifest backup path is unsafe")
        seen_backups.add(backup)
    if not set(contract.mutable_files).issubset({item.path for item in bindings}):
        raise ValueError("manifest mutation contract files are not backed up")
    source_directories = payload.get("source_directories")
    if not isinstance(source_directories, list) or not source_directories:
        raise ValueError("manifest source directory inventory is invalid")
    source_paths = tuple(
        _record_to_path(
            vault,
            {"kind": "vault", "path": item},
            "manifest source directory",
        )
        for item in source_directories
    )
    old_root = vault / "01-Projects" / payload["old_slug"]
    if (
        source_paths[0] != old_root
        or len(set(source_paths)) != len(source_paths)
        or any(
            path != old_root and not path.is_relative_to(old_root)
            for path in source_paths
        )
    ):
        raise ValueError("manifest source directory inventory is invalid")
    expected_directory_paths = tuple(
        sorted(set(contract.mutable_directories) | set(source_paths))
    )
    if tuple(item.path for item in directory_bindings) != expected_directory_paths:
        raise ValueError("manifest directory bindings do not match directory inventory")
    compatibility_roots = tuple(
        _record_to_path(vault, item, "manifest mutable root")
        for item in payload.get("mutable_roots", ())
    )
    raw_compatibility_exclusions = payload.get("excluded_mutable_subtrees")
    if not isinstance(raw_compatibility_exclusions, list):
        raise ValueError("manifest excluded mutable subtrees projection is invalid")
    compatibility_exclusions = tuple(
        _record_to_path(vault, item, "manifest excluded mutable subtree")
        for item in raw_compatibility_exclusions
    )
    compatibility_directories = tuple(
        _record_to_path(vault, item, "manifest mutable directory")
        for item in payload.get("mutable_directories_before", ())
    )
    compatibility_absent = tuple(
        _record_to_path(vault, item, "manifest absent path")
        for item in payload.get("absent_paths_before", ())
    )
    if (
        compatibility_roots != contract.mutable_roots
        or compatibility_exclusions != contract.excluded_mutable_subtrees
        or compatibility_directories != contract.mutable_directories
        or compatibility_absent != contract.absent_paths
    ):
        raise ValueError("manifest compatibility projections do not match contract")
    if not isinstance(payload.get("memory_identity_keys_before"), list):
        raise ValueError("manifest memory identity keys are invalid")
    if payload.get("memory_identity_count_before") != len(
        payload["memory_identity_keys_before"]
    ):
        raise ValueError("manifest memory identity count is invalid")
    status = payload.get("status")
    if status not in {"prepared", "applying", "applied", "rolled_back"}:
        raise ValueError("brand migration manifest status is invalid")
    if status == "applying":
        recovery = payload.get("recovery")
        if (
            not isinstance(payload.get("applying_at"), str)
            or recovery
            != {
                "mode": "manual",
                "reason": "writer-ownership-loss-or-interruption",
            }
        ):
            raise ValueError("brand migration applying journal is invalid")
    if expected_plan is not None:
        expected = _migration_manifest(expected_plan, migration_id, files)
        comparable = dict(payload)
        if status == "applying":
            comparable.pop("applying_at")
            comparable.pop("recovery")
            comparable["status"] = "prepared"
        if comparable != expected:
            raise ValueError("prepared manifest does not match frozen migration plan")
    return vault, contract, bindings


@dataclass
class _BackupHandle:
    path: Path
    parent_fd: int
    root_fd: int
    inode: tuple[int, int]
    payload: dict
    bindings: tuple[InputBinding, ...]
    manifest_inode: tuple[int, int] | None = None
    manifest_sha256: str | None = None
    vault_pin: _PinnedDirectory | None = None
    checkpoint_chain: list | None = None
    checkpoint_application_index: dict | None = None
    checkpoint_seen_phases: set[str] = field(default_factory=set)
    checkpoint_rollback_basis: dict | None = None
    checkpoint_head_directory_fd: int | None = None
    checkpoint_head_record_fd: int | None = None
    checkpoint_head_authority: tuple | None = None
    checkpoint_journal_authority: tuple | None = None
    checkpoint_journal_inventory: frozenset[str] | None = None
    journal_fd: int | None = None
    journal_inode: tuple[int, int] | None = None
    base_payload: dict | None = None
    base_vault: Path | None = None
    base_contract: MutationContract | None = None
    required_apply_phases: frozenset[str] | None = None
    validated_snapshot_digest: str | None = None


def _read_fd_bytes(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _expected_backup_directories(relative_files):
    directories = {Path(), Path(MIGRATION_JOURNAL_DIRECTORY)}
    for relative in relative_files:
        directories.update(relative.parents)
    return tuple(sorted(directories))


def _inspect_sealed_backup(
    handle,
    expected_plan=None,
    allow_journal_recovery=False,
):
    named = os.stat(handle.path.name, dir_fd=handle.parent_fd, follow_symlinks=False)
    current_root = os.fstat(handle.root_fd)
    if (
        not stat.S_ISDIR(named.st_mode)
        or _inode_from_stat(named) != handle.inode
        or _inode_from_stat(current_root) != handle.inode
        or stat.S_IMODE(current_root.st_mode) != 0o500
    ):
        raise RuntimeError("sealed backup root changed")
    manifest_fd = os.open(
        "manifest.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=handle.root_fd
    )
    try:
        before = os.fstat(manifest_fd)
        raw = _read_fd_bytes(manifest_fd)
        after = os.fstat(manifest_fd)
    finally:
        os.close(manifest_fd)
    if (
        _input_stat_identity(before) != _input_stat_identity(after)
        or not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or stat.S_IMODE(after.st_mode) != 0o400
    ):
        raise RuntimeError("sealed manifest changed while reading")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid brand migration manifest JSON") from exc
    if raw != _serialize_manifest_bytes(payload):
        raise ValueError("brand migration manifest bytes are not deterministic")
    manifest_inode = _inode_from_stat(after)
    manifest_sha256 = hashlib.sha256(raw).hexdigest()
    named_manifest = _stat_relative(handle.root_fd, Path("manifest.json"))
    if _inode_from_stat(named_manifest) != manifest_inode:
        raise RuntimeError("sealed manifest path changed while reading")
    if (
        handle.manifest_inode is not None
        and (
            manifest_inode != handle.manifest_inode
            or manifest_sha256 != handle.manifest_sha256
        )
    ):
        raise RuntimeError("manifest binding changed across writer guard")
    if handle.base_payload is None:
        vault, contract, bindings = _validate_manifest_payload(payload, expected_plan)
        handle.base_payload = payload
        handle.base_vault = vault
        handle.base_contract = contract
    else:
        if payload != handle.base_payload:
            raise RuntimeError("sealed manifest payload changed")
        vault = handle.base_vault
        contract = handle.base_contract
        bindings = handle.bindings
    expected_root = Path(
        safe_vault_path(
            vault,
            "04-Feedback",
            "_rollback",
            "brand-migration",
            payload["migration_id"],
        )
    )
    if handle.path != expected_root or handle.path != handle.path.resolve(strict=False):
        raise ValueError("brand migration manifest path does not match its Vault")
    expected_files = {Path("manifest.json")}
    source_inodes = {binding.inode for binding in bindings}
    backup_inodes = set()
    for item in payload["files"]:
        relative = Path(item["backup"])
        expected_files.add(relative)
        current = _stat_relative(handle.root_fd, relative)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or stat.S_IMODE(current.st_mode) != 0o400
            or _hash_backup_file(handle.root_fd, relative) != item["sha256"]
        ):
            raise RuntimeError(f"sealed backup payload changed: {relative}")
        inode = _inode_from_stat(current)
        if inode in source_inodes or inode in backup_inodes:
            raise RuntimeError(f"sealed backup inode is unsafe: {relative}")
        backup_inodes.add(inode)
    journal_fd = os.open(
        MIGRATION_JOURNAL_DIRECTORY,
        _directory_flags(),
        dir_fd=handle.root_fd,
    )
    journal_stat = os.fstat(journal_fd)
    named_journal = os.stat(
        MIGRATION_JOURNAL_DIRECTORY,
        dir_fd=handle.root_fd,
        follow_symlinks=False,
    )
    journal_inode = _inode_from_stat(journal_stat)
    allowed_journal_modes = {0o500, 0o700} if allow_journal_recovery else {0o500}
    if (
        not stat.S_ISDIR(named_journal.st_mode)
        or _inode_from_stat(named_journal) != journal_inode
        or stat.S_IMODE(journal_stat.st_mode) not in allowed_journal_modes
        or (
            handle.journal_inode is not None
            and journal_inode != handle.journal_inode
        )
    ):
        os.close(journal_fd)
        raise RuntimeError("sealed migration journal directory changed")
    if handle.journal_fd is None:
        handle.journal_fd = journal_fd
        handle.journal_inode = journal_inode
    else:
        os.close(journal_fd)
        if _inode_from_stat(os.fstat(handle.journal_fd)) != handle.journal_inode:
            raise RuntimeError("pinned migration journal descriptor changed")

    current_files, current_directories = _inventory_staging_tree(handle.root_fd)
    journal_prefix = Path(MIGRATION_JOURNAL_DIRECTORY)
    current_files = tuple(
        item for item in current_files if not item.is_relative_to(journal_prefix)
    )
    current_directories = tuple(
        item
        for item in current_directories
        if item == journal_prefix or not item.is_relative_to(journal_prefix)
    )
    if current_files != tuple(sorted(expected_files)):
        raise RuntimeError("sealed backup file inventory changed")
    expected_directories = _expected_backup_directories(expected_files)
    if current_directories != expected_directories:
        raise RuntimeError("sealed backup directory inventory changed")
    for relative in current_directories:
        fd = _open_relative_directory(handle.root_fd, relative)
        try:
            expected_modes = (
                allowed_journal_modes
                if relative == journal_prefix
                else {0o500}
            )
            if stat.S_IMODE(os.fstat(fd).st_mode) not in expected_modes:
                raise RuntimeError(f"sealed backup directory mode changed: {relative}")
        finally:
            os.close(fd)
    handle.payload = payload
    handle.bindings = bindings
    handle.manifest_inode = manifest_inode
    handle.manifest_sha256 = manifest_sha256


def _open_backup_handle(
    manifest_path,
    expected_plan=None,
    vault_pin=None,
    allow_journal_recovery=False,
):
    manifest_path = Path(manifest_path)
    if manifest_path.name != "manifest.json":
        raise ValueError("brand migration manifest path is invalid")
    backup_root = manifest_path.parent
    if vault_pin is not None:
        _verify_named_directory(vault_pin)
        try:
            relative_root = backup_root.relative_to(vault_pin.path)
        except ValueError as exc:
            raise ValueError("backup root is outside pinned Vault") from exc
        parent_fd, leaf = _open_relative_parent(
            vault_pin.fd,
            relative_root,
        )
    else:
        anchor_fd = os.open(backup_root.anchor, _directory_flags())
        try:
            parent_fd, leaf = _open_relative_parent(
                anchor_fd,
                Path(*backup_root.parts[1:]),
            )
        finally:
            os.close(anchor_fd)
    try:
        root_fd = os.open(leaf, _directory_flags(), dir_fd=parent_fd)
    except Exception:
        os.close(parent_fd)
        raise
    current = os.fstat(root_fd)
    handle = _BackupHandle(
        path=backup_root,
        parent_fd=parent_fd,
        root_fd=root_fd,
        inode=_inode_from_stat(current),
        payload={},
        bindings=(),
        vault_pin=vault_pin,
    )
    try:
        _inspect_sealed_backup(
            handle,
            expected_plan=expected_plan,
            allow_journal_recovery=allow_journal_recovery,
        )
    except Exception:
        if handle.journal_fd is not None:
            os.close(handle.journal_fd)
        os.close(root_fd)
        os.close(parent_fd)
        raise
    return handle


def _close_backup_handle(handle):
    if handle.checkpoint_head_record_fd is not None:
        os.close(handle.checkpoint_head_record_fd)
    if handle.checkpoint_head_directory_fd is not None:
        os.close(handle.checkpoint_head_directory_fd)
    if handle.journal_fd is not None:
        os.close(handle.journal_fd)
    os.close(handle.root_fd)
    os.close(handle.parent_fd)


def _vault_from_manifest_path(manifest_path):
    manifest_path = Path(manifest_path)
    if (
        not manifest_path.is_absolute()
        or manifest_path.name != "manifest.json"
        or len(manifest_path.parents) < 5
        or manifest_path.parents[1].name != "brand-migration"
        or manifest_path.parents[2].name != "_rollback"
        or manifest_path.parents[3].name != "04-Feedback"
    ):
        raise ValueError("brand migration manifest path is invalid")
    migration_id = manifest_path.parent.name
    if normalize_project_slug(migration_id) != migration_id:
        raise ValueError("brand migration manifest path has invalid migration id")
    return manifest_path.parents[4]


def _open_absolute_parent(path, create_directories=()):
    path = Path(path)
    if not path.is_absolute() or path != path.resolve(strict=False):
        raise ValueError(f"migration target path is not canonical: {path}")
    create_directories = {Path(item) for item in create_directories}
    current_path = Path(path.anchor)
    current_fd = os.open(current_path, _directory_flags())
    try:
        for part in path.parts[1:-1]:
            next_path = current_path / part
            try:
                next_fd = os.open(part, _directory_flags(), dir_fd=current_fd)
            except FileNotFoundError:
                if next_path not in create_directories:
                    raise RuntimeError(
                        f"migration target parent changed: {next_path}"
                    )
                try:
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                except FileExistsError as exc:
                    raise RuntimeError(
                        f"migration target parent appeared: {next_path}"
                    ) from exc
                next_fd = os.open(part, _directory_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
            current_path = next_path
        return current_fd, path.name
    except Exception:
        os.close(current_fd)
        raise


def _open_handle_parent(handle, path, create_directories=()):
    path = Path(path)
    vault_pin = getattr(handle, "vault_pin", None)
    if vault_pin is None:
        return _open_absolute_parent(path, create_directories)
    try:
        relative = path.relative_to(vault_pin.path)
    except ValueError:
        return _open_absolute_parent(path, create_directories)
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError(f"rollback target is unsafe below pinned Vault: {path}")
    allowed = {
        candidate.relative_to(vault_pin.path)
        for candidate in map(Path, create_directories)
        if candidate == vault_pin.path or candidate.is_relative_to(vault_pin.path)
    }
    current_fd = os.dup(vault_pin.fd)
    current_relative = Path()
    try:
        for part in relative.parts[:-1]:
            next_relative = current_relative / part
            try:
                next_fd = os.open(part, _directory_flags(), dir_fd=current_fd)
            except FileNotFoundError:
                if next_relative not in allowed:
                    raise RuntimeError(
                        f"rollback target parent changed below pinned Vault: {path}"
                    )
                os.mkdir(part, mode=0o700, dir_fd=current_fd)
                next_fd = os.open(part, _directory_flags(), dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
            current_relative = next_relative
        return current_fd, relative.parts[-1]
    except Exception:
        os.close(current_fd)
        raise


def _named_stat(parent_fd, name):
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _verify_regular_named(parent_fd, name, expected_inode=None):
    current = _named_stat(parent_fd, name)
    if current is None:
        raise RuntimeError(f"migration target disappeared: {name}")
    if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
        raise RuntimeError(f"migration target is not a single-link regular file: {name}")
    if expected_inode is not None and _inode_from_stat(current) != expected_inode:
        raise RuntimeError(f"migration target inode changed: {name}")
    return current


def _verify_named_input_binding(parent_fd, name, binding):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _input_stat_identity(before) != _binding_stat_identity(binding)
        ):
            raise RuntimeError(f"migration target binding changed: {binding.path}")
        digest = _hash_fd(fd)
        after = os.fstat(fd)
        if _input_stat_identity(after) != _input_stat_identity(before):
            raise RuntimeError(
                f"migration target changed while validating: {binding.path}"
            )
        if digest != binding.sha256:
            raise RuntimeError(f"migration target digest changed: {binding.path}")
        return after
    finally:
        os.close(fd)


def _verify_named_inode_binding(parent_fd, name, binding):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or _inode_from_stat(before) != binding.inode
        ):
            raise RuntimeError(f"migration target inode changed: {binding.path}")
        after = os.fstat(fd)
        if (
            not stat.S_ISREG(after.st_mode)
            or after.st_nlink != 1
            or _inode_from_stat(after) != binding.inode
        ):
            raise RuntimeError(
                f"migration target inode changed while validating: {binding.path}"
            )
        return after
    finally:
        os.close(fd)


def _write_new_file(parent_fd, name, content, mode=0o600):
    fd = os.open(
        name,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode,
        dir_fd=parent_fd,
    )
    try:
        _write_all(fd, content)
        os.fsync(fd)
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise RuntimeError(f"migration staging file is unsafe: {name}")
        return _inode_from_stat(current)
    finally:
        os.close(fd)


class _ConcurrentMutationError(RuntimeError):
    pass


class _ProjectRenameRace(_ConcurrentMutationError):
    def __init__(self, message, rename_occurred, recovered):
        super().__init__(message)
        self.rename_occurred = rename_occurred
        self.recovered = recovered


def _quarantine_remove_name(
    parent_fd,
    name,
    expected_inode,
    label,
    expected_binding=None,
):
    quarantine_name = f".brand-migration-quarantine-{secrets.token_hex(12)}"
    os.mkdir(quarantine_name, mode=0o700, dir_fd=parent_fd)
    quarantine_fd = None
    moved = False
    primary_error = None
    try:
        quarantine_fd = os.open(
            quarantine_name,
            _directory_flags(),
            dir_fd=parent_fd,
        )
        if expected_binding is not None:
            _verify_named_input_binding(parent_fd, name, expected_binding)
        _rename_exclusive(parent_fd, name, quarantine_fd, "entry")
        moved = True
        current = _named_stat(quarantine_fd, "entry")
        moved_inode = _inode_from_stat(current) if current is not None else None
        if (
            current is None
            or not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or moved_inode != expected_inode
        ):
            try:
                _rename_exclusive(quarantine_fd, "entry", parent_fd, name)
                moved = False
                restored = _named_stat(parent_fd, name)
                if (
                    restored is None
                    or _inode_from_stat(restored) != moved_inode
                    or stat.S_IFMT(restored.st_mode) != stat.S_IFMT(current.st_mode)
                ):
                    raise RuntimeError("restored inode changed")
            except Exception as recovery_error:
                raise RuntimeError(
                    f"concurrent {label}; quarantine recovery failed: {recovery_error}"
                ) from recovery_error
            raise _ConcurrentMutationError(
                f"concurrent {label}; unexpected inode was restored"
            )
        os.unlink("entry", dir_fd=quarantine_fd)
        moved = False
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_errors = []
        if quarantine_fd is not None:
            try:
                os.close(quarantine_fd)
            except OSError as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if not moved:
            try:
                os.rmdir(quarantine_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            detail = "; ".join(str(error) for error in cleanup_errors)
            if primary_error is not None:
                raise RuntimeError(
                    f"{primary_error}; quarantine cleanup failed: {detail}"
                ) from primary_error
            raise RuntimeError(f"quarantine cleanup failed: {detail}")


def _exchange_bound_name(
    source_fd,
    source_name,
    destination_fd,
    destination_name,
    expected_displaced_inode,
    installed_inode,
    label,
):
    _rename_exchange(
        source_fd,
        source_name,
        destination_fd,
        destination_name,
    )
    displaced = _named_stat(source_fd, source_name)
    installed = _named_stat(destination_fd, destination_name)
    displaced_inode = (
        _inode_from_stat(displaced) if displaced is not None else None
    )
    current_installed_inode = (
        _inode_from_stat(installed) if installed is not None else None
    )
    valid_displaced = (
        displaced is not None
        and stat.S_ISREG(displaced.st_mode)
        and displaced.st_nlink == 1
        and displaced_inode == expected_displaced_inode
    )
    valid_installed = (
        installed is not None
        and stat.S_ISREG(installed.st_mode)
        and installed.st_nlink == 1
        and current_installed_inode == installed_inode
    )
    if not valid_displaced or not valid_installed:
        try:
            _rename_exchange(
                source_fd,
                source_name,
                destination_fd,
                destination_name,
            )
            restored = _named_stat(destination_fd, destination_name)
            scratch = _verify_regular_named(
                source_fd,
                source_name,
                installed_inode,
            )
            if (
                restored is None
                or _inode_from_stat(restored) != displaced_inode
                or _inode_from_stat(scratch) != installed_inode
            ):
                raise RuntimeError("exchange-back inode verification failed")
            _quarantine_remove_name(
                source_fd,
                source_name,
                installed_inode,
                f"{label} scratch cleanup",
            )
        except Exception as recovery_error:
            raise RuntimeError(
                f"concurrent {label}; exchange-back recovery failed: {recovery_error}"
            ) from recovery_error
        raise _ConcurrentMutationError(
            f"concurrent {label}; interposed inode was restored"
        )
    _quarantine_remove_name(
        source_fd,
        source_name,
        expected_displaced_inode,
        f"{label} displaced cleanup",
    )


def _write_vault_target(
    handle,
    target,
    content,
    expected_binding,
    create_directories,
    allow_binding_drift=False,
):
    parent_fd, leaf = _open_handle_parent(handle, target, create_directories)
    scratch = f".apply-{secrets.token_hex(12)}"
    root_unsealed = False
    try:
        current = _named_stat(parent_fd, leaf)
        if expected_binding is None:
            if current is not None:
                raise RuntimeError(f"migration absent target appeared: {target}")
        else:
            verifier = (
                _verify_named_inode_binding
                if allow_binding_drift
                else _verify_named_input_binding
            )
            verifier(parent_fd, leaf, expected_binding)
        os.fchmod(handle.root_fd, 0o700)
        root_unsealed = True
        scratch_inode = _write_new_file(handle.root_fd, scratch, content)
        try:
            if expected_binding is None:
                _rename_exclusive(handle.root_fd, scratch, parent_fd, leaf)
            else:
                _exchange_bound_name(
                    handle.root_fd,
                    scratch,
                    parent_fd,
                    leaf,
                    expected_binding.inode,
                    scratch_inode,
                    "vault target",
                )
            installed = _verify_regular_named(parent_fd, leaf, scratch_inode)
            if _inode_from_stat(installed) != scratch_inode:
                raise RuntimeError(f"migration target replacement changed: {target}")
        except Exception:
            scratch_state = _named_stat(handle.root_fd, scratch)
            if (
                scratch_state is not None
                and _inode_from_stat(scratch_state) == scratch_inode
            ):
                try:
                    _quarantine_remove_name(
                        handle.root_fd,
                        scratch,
                        scratch_inode,
                        "vault target scratch cleanup",
                    )
                except OSError:
                    pass
            raise
    finally:
        if root_unsealed:
            os.fchmod(handle.root_fd, 0o500)
        os.close(parent_fd)
    _inspect_sealed_backup(handle)


def _temporary_spec_for_target(contract, target):
    target = Path(target)
    if str(target).endswith(".tmp"):
        candidates = (Path(str(target)[:-4] + ".restore"),)
    elif str(target).endswith(".restore"):
        candidates = (Path(str(target)[:-8] + ".tmp"),)
    else:
        candidates = (Path(str(target) + ".tmp"), Path(str(target) + ".restore"))
    specs = {spec.path: spec for spec in contract.target_specs}
    for candidate in candidates:
        spec = specs.get(candidate)
        if spec is not None:
            return spec
    if any(
        target == root or target.is_relative_to(root)
        for root in contract.mutable_roots
    ):
        return None
    raise RuntimeError(f"external target has no frozen temporary sibling: {target}")


def _write_external_target(
    contract,
    target,
    content,
    expected_binding,
    create_directories,
    allow_binding_drift=False,
    input_bindings=(),
):
    parent_fd, leaf = _open_absolute_parent(target, create_directories)
    temp_spec = _temporary_spec_for_target(contract, target)
    temp_leaf = (
        temp_spec.path.name
        if temp_spec is not None
        else f".{leaf}.migration-{secrets.token_hex(8)}"
    )
    preserved = None
    preserved_removed = False
    staging_inode = None

    def restore_preserved():
        nonlocal preserved_removed
        if not preserved_removed:
            return
        current = _named_stat(parent_fd, temp_leaf)
        if (
            current is not None
            and staging_inode is not None
            and _inode_from_stat(current) == staging_inode
        ):
            _quarantine_remove_name(
                parent_fd,
                temp_leaf,
                staging_inode,
                "external staging cleanup",
            )
            current = None
        if current is not None:
            raise RuntimeError(
                f"external temporary sibling changed during recovery: {temp_leaf}"
            )
        data, mode, mtime_ns = preserved
        _write_new_file(parent_fd, temp_leaf, data, mode=mode)
        os.utime(
            temp_leaf,
            ns=(mtime_ns, mtime_ns),
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        preserved_removed = False

    try:
        current = _named_stat(parent_fd, leaf)
        if expected_binding is None:
            if current is not None:
                raise RuntimeError(f"migration absent target appeared: {target}")
        else:
            verifier = (
                _verify_named_inode_binding
                if allow_binding_drift
                else _verify_named_input_binding
            )
            verifier(parent_fd, leaf, expected_binding)
        temp_current = _named_stat(parent_fd, temp_leaf)
        if temp_spec is not None and temp_spec.expected_state == "file":
            temp_binding = next(
                (
                    binding
                    for binding in input_bindings
                    if binding.path == temp_spec.path
                ),
                None,
            )
            if temp_binding is None:
                raise RuntimeError(
                    f"external temporary sibling has no authoritative binding: "
                    f"{temp_spec.path}"
                )
            _verify_named_input_binding(parent_fd, temp_leaf, temp_binding)
            temp_fd = os.open(
                temp_leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
            try:
                temp_stat = os.fstat(temp_fd)
                preserved = (
                    _read_fd_bytes(temp_fd),
                    stat.S_IMODE(temp_stat.st_mode),
                    temp_stat.st_mtime_ns,
                )
            finally:
                os.close(temp_fd)
            _quarantine_remove_name(
                parent_fd,
                temp_leaf,
                temp_spec.inode,
                "external temporary sibling removal",
                expected_binding=temp_binding,
            )
            preserved_removed = True
        elif temp_current is not None:
            label = temp_spec.path if temp_spec is not None else target.parent / temp_leaf
            raise RuntimeError(f"migration temporary target appeared: {label}")
        staging_inode = _write_new_file(parent_fd, temp_leaf, content)
        if expected_binding is None:
            _rename_exclusive(parent_fd, temp_leaf, parent_fd, leaf)
        else:
            _exchange_bound_name(
                parent_fd,
                temp_leaf,
                parent_fd,
                leaf,
                expected_binding.inode,
                staging_inode,
                "external target",
            )
        _verify_regular_named(parent_fd, leaf, staging_inode)
        if preserved is not None:
            restore_preserved()
    except Exception as primary_error:
        if preserved_removed:
            try:
                restore_preserved()
            except Exception as recovery_error:
                raise RuntimeError(
                    f"{primary_error}; external temporary sibling recovery failed: "
                    f"{recovery_error}"
                ) from primary_error
        raise
    finally:
        os.close(parent_fd)


def _binding_by_path(plan):
    return {binding.path: binding for binding in plan.input_bindings}


def _write_bound_target(plan, handle, original_path, content):
    original_path = Path(original_path)
    target = _post_migration_path(
        original_path, plan.source_project, plan.destination_project
    )
    binding = _binding_by_path(plan).get(original_path)
    create_directories = plan.mutation_contract.absent_directories
    if target.is_relative_to(plan.vault):
        _write_vault_target(
            handle, target, content, binding, create_directories
        )
    else:
        _write_external_target(
            plan.mutation_contract,
            target,
            content,
            binding,
            create_directories,
            input_bindings=plan.input_bindings,
        )


class _MigrationIO:
    """Descriptor-pinned mutation capability for the fixed migration phases."""

    def __init__(self, plan, handle, ownership_check, checkpoint):
        self.plan = plan
        self.handle = handle
        self.ownership_check = ownership_check
        self.checkpoint = checkpoint
        self.rebuild_phase = None
        self.rebuild_boundary = None
        state = _capture_post_state(plan)
        self.bindings = {binding.path: binding for binding in state["bindings"]}
        self.directories = {
            Path(item["path"] if item["kind"] == "external" else plan.vault / item["path"]): (
                tuple(item["inode"]),
                item["mode"],
                item["link_count"],
            )
            for item in state["directories"]
        }
        self.created_directories = set(state["created_directories"])
        contract = plan.mutation_contract
        self.original_files = {
            candidate
            for binding in plan.input_bindings
            for candidate in (
                binding.path,
                _post_migration_path(
                    binding.path,
                    plan.source_project,
                    plan.destination_project,
                ),
            )
        }
        self.original_directories = {
            candidate
            for binding in plan.directory_bindings
            for candidate in (
                binding.path,
                _post_migration_path(
                    binding.path,
                    plan.source_project,
                    plan.destination_project,
                ),
            )
        }
        self.allowed_directories = {
            *contract.absent_directories,
            *self.created_directories,
        }
        self.roots = tuple(
            sorted(
                {
                    root
                    for original in contract.mutable_roots
                    for root in (
                        original,
                        _post_migration_path(
                            original,
                            plan.source_project,
                            plan.destination_project,
                        ),
                    )
                }
            )
        )
        self.explicit = {
            candidate
            for spec in contract.target_specs
            for candidate in (
                spec.path,
                _post_migration_path(
                    spec.path,
                    plan.source_project,
                    plan.destination_project,
                ),
            )
        }
        self.explicit.update(
            candidate
            for original in plan.markdown_paths
            for candidate in (
                original,
                _post_migration_path(
                    original,
                    plan.source_project,
                    plan.destination_project,
                ),
            )
        )
        self.excluded = {
            candidate
            for excluded in contract.excluded_mutable_subtrees
            for candidate in (
                excluded,
                _post_migration_path(
                    excluded,
                    plan.source_project,
                    plan.destination_project,
                ),
            )
        }
        self.parent_pins = {}
        pin_candidates = self.explicit | set(self.bindings) | set(self.directories)
        try:
            for candidate in sorted(pin_candidates):
                parent = candidate.parent
                while not os.path.lexists(parent):
                    if parent == parent.parent:
                        raise RuntimeError(
                            f"migration mutation has no existing parent: {candidate}"
                        )
                    parent = parent.parent
                if parent in self.parent_pins:
                    continue
                self.parent_pins[parent] = _open_vault_directory(parent)
        except Exception:
            self.close()
            raise

    def close(self):
        for pin in reversed(tuple(self.parent_pins.values())):
            os.close(pin.fd)
        self.parent_pins.clear()

    def _verify_parent_pins(self, path):
        applicable = [
            pin
            for parent, pin in self.parent_pins.items()
            if path.parent == parent or path.parent.is_relative_to(parent)
        ]
        if not applicable:
            raise RuntimeError(f"migration mutation parent is not pinned: {path}")
        for pin in applicable:
            _verify_named_directory(pin)

    def _canonical_path(self, value, allow_pinned_parent=False):
        raw = os.fspath(value)
        path = Path(raw)
        if not path.is_absolute() or os.path.normpath(raw) != raw:
            raise ValueError(f"migration mutation path is not canonical: {raw}")
        if path not in self.explicit and any(
            path == excluded or path.is_relative_to(excluded)
            for excluded in self.excluded
        ):
            raise RuntimeError(
                f"migration mutation path is below an excluded mutable subtree: {path}"
            )
        if path not in self.explicit and not any(
            path == root or path.is_relative_to(root) for root in self.roots
        ):
            if (
                allow_pinned_parent
                and path in self.parent_pins
                and any(candidate.parent == path for candidate in self.explicit)
            ):
                _verify_named_directory(self.parent_pins[path])
                return path
            raise RuntimeError(f"migration mutation path is outside contract: {path}")
        return path

    def begin_rebuild_phase(self, phase):
        if self.rebuild_phase is not None:
            raise RuntimeError("migration rebuild phase is already active")
        self.rebuild_phase = phase
        self.rebuild_boundary = None

    def finish_rebuild_phase(self):
        if self.rebuild_phase is None:
            raise RuntimeError("migration rebuild phase is not active")
        if self.rebuild_boundary is None:
            self.checkpoint(
                self.rebuild_phase,
                "before",
                delta_override={},
            )
            self.checkpoint(
                self.rebuild_phase,
                "after",
                delta_override={},
            )
        elif self.rebuild_boundary != "after":
            raise RuntimeError("migration rebuild checkpoint state is invalid")
        self.rebuild_phase = None
        self.rebuild_boundary = None

    def abort_rebuild_phase(self):
        self.rebuild_phase = None
        self.rebuild_boundary = None

    def _before_mutation(self, intent):
        if self.rebuild_phase is None:
            return False
        if self.rebuild_boundary not in {None, "after"}:
            raise RuntimeError("migration rebuild checkpoint state is invalid")
        self.checkpoint(
            self.rebuild_phase,
            "before",
            mutation_intent=intent,
            delta_override={},
        )
        self.rebuild_boundary = "before"
        return True

    def _after_mutation(self, checkpointed, intent, delta):
        if not checkpointed:
            return
        if self.rebuild_phase is None or self.rebuild_boundary != "before":
            raise RuntimeError("migration rebuild checkpoint state is invalid")
        self.checkpoint(
            self.rebuild_phase,
            "after",
            mutation_intent=intent,
            delta_override=delta,
        )
        self.rebuild_boundary = "after"

    def _settle_mutation(self, checkpointed, intent, delta):
        if not checkpointed:
            return
        if self.rebuild_phase is None or self.rebuild_boundary != "after":
            raise RuntimeError("migration rebuild checkpoint state is invalid")
        self.checkpoint(
            f"cleanup:{self.rebuild_phase}",
            "after",
            mutation_intent=intent,
            delta_override=delta,
        )

    def _verify_directory_authority(self, path):
        pin = self.parent_pins.get(path)
        if pin is not None:
            _verify_named_directory(pin)
            return pin
        self._verify_parent_pins(path)
        return None

    def _capture_binding(self, path):
        return _capture_input_binding(path)

    def _stage_path(self, target):
        return target.parent / (
            f".{target.name}.agent-memory-beacon-stage-{secrets.token_hex(12)}"
        )

    def _target_binding_from_stage(self, target, stage):
        binding = _capture_input_binding(stage)
        return InputBinding(
            path=target,
            sha256=binding.sha256,
            inode=binding.inode,
            link_count=binding.link_count,
            mode=binding.mode,
            size=binding.size,
            mtime_ns=binding.mtime_ns,
            ctime_ns=binding.ctime_ns,
            expected_type=binding.expected_type,
        )

    def _file_intent(self, operation, target, staging, before, intended):
        return {
            "version": 1,
            "operation": operation,
            "target": _manifest_path_record(self.plan, target),
            "staging": _manifest_path_record(self.plan, staging),
            "before": (
                _serialize_post_binding(self.plan, before)
                if before is not None
                else None
            ),
            "intended": (
                _serialize_post_binding(self.plan, intended)
                if intended is not None
                else None
            ),
        }

    def _directory_intent(
        self,
        operation,
        target,
        staging,
        before_inode,
        intended_inode,
    ):
        return {
            "version": 1,
            "operation": operation,
            "target": _manifest_path_record(self.plan, target),
            "staging": _manifest_path_record(self.plan, staging),
            "before": (
                {"inode": list(before_inode)}
                if before_inode is not None
                else None
            ),
            "intended": (
                {"inode": list(intended_inode)}
                if intended_inode is not None
                else None
            ),
        }

    def _directory_record(self, path):
        current = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode):
            raise RuntimeError(f"migration directory changed type: {path}")
        return {
            **_manifest_path_record(self.plan, path),
            "inode": list(_inode_from_stat(current)),
            "mode": current.st_mode,
            "link_count": current.st_nlink,
            "atime_ns": current.st_atime_ns,
            "mtime_ns": current.st_mtime_ns,
            "ctime_ns": current.st_ctime_ns,
            "size": current.st_size,
        }

    def _target_delta(self, path, file_binding=None, directory_exists=None):
        before = self.handle.checkpoint_application_index
        after = {key: dict(values) for key, values in before.items()}
        identity = _manifest_path_record(self.plan, path)
        item_key = (identity["kind"], identity["path"])

        if directory_exists is None:
            if file_binding is None:
                after["post_bindings"].pop(item_key, None)
                after["created_files"].pop(item_key, None)
            else:
                after["post_bindings"][item_key] = _serialize_post_binding(
                    self.plan,
                    file_binding,
                )
                if path not in self.original_files:
                    after["created_files"][item_key] = identity
                else:
                    after["created_files"].pop(item_key, None)
        elif directory_exists:
            after["post_directories"][item_key] = self._directory_record(path)
            if path not in self.original_directories:
                after["created_directories"][item_key] = identity
            else:
                after["created_directories"].pop(item_key, None)
        else:
            after["post_directories"].pop(item_key, None)
            after["created_directories"].pop(item_key, None)

        parent_identity = _manifest_path_record(self.plan, path.parent)
        parent_key = (parent_identity["kind"], parent_identity["path"])
        if parent_key in after["post_directories"]:
            after["post_directories"][parent_key] = self._directory_record(
                path.parent
            )
        return _application_delta_indexes(before, after)

    def _verify_intended_file(self, intended, actual):
        if (
            actual.path != intended.path
            or actual.inode != intended.inode
            or actual.link_count != 1
            or actual.sha256 != intended.sha256
            or actual.size != intended.size
            or stat.S_IMODE(actual.mode) != stat.S_IMODE(intended.mode)
        ):
            raise _ConcurrentMutationError(
                f"concurrent migration target replacement: {intended.path}"
            )

    def _verify_temporary_siblings(self, target):
        specs = {spec.path: spec for spec in self.plan.mutation_contract.target_specs}
        for suffix in (".tmp", ".restore"):
            path = Path(str(target) + suffix)
            spec = specs.get(path)
            if spec is None:
                continue
            if spec.expected_state == "absent":
                if os.path.lexists(path):
                    raise _ConcurrentMutationError(
                        f"migration temporary target appeared: {path}"
                    )
                continue
            binding = self.bindings.get(path)
            if binding is None:
                raise RuntimeError(
                    f"migration temporary target has no binding: {path}"
                )
            _revalidate_input_binding(binding, "during migration")

    def atomic_write(self, path, content, encoding="utf-8"):
        path = self._canonical_path(path)
        if not isinstance(content, (str, bytes)):
            raise TypeError("migration atomic write content must be text or bytes")
        data = content.encode(encoding) if isinstance(content, str) else content
        self.ownership_check()
        self._verify_parent_pins(path)
        self._verify_temporary_siblings(path)
        current = self.bindings.get(path)
        if current is None and os.path.lexists(path):
            raise RuntimeError(f"migration target appeared outside current binding: {path}")
        parent_fd, leaf = _open_handle_parent(
            self.handle,
            path,
            self.allowed_directories | self.created_directories,
        )
        staging = self._stage_path(path)
        stage_name = staging.name
        intent_published = False
        try:
            if current is None:
                if _named_stat(parent_fd, leaf) is not None:
                    raise RuntimeError(
                        f"migration target appeared outside current binding: {path}"
                    )
            else:
                _verify_named_input_binding(parent_fd, leaf, current)
            _write_new_file(parent_fd, stage_name, data)
            intended = self._target_binding_from_stage(path, staging)
            intent = self._file_intent(
                "write-file",
                path,
                staging,
                current,
                intended,
            )
            checkpointed = self._before_mutation(intent)
            intent_published = checkpointed
            if current is None:
                _rename_exclusive(parent_fd, stage_name, parent_fd, leaf)
            else:
                _rename_exchange(parent_fd, stage_name, parent_fd, leaf)
                if not _named_file_matches(parent_fd, stage_name, current):
                    raise _ConcurrentMutationError(
                        f"concurrent migration staged file replacement: {staging}"
                    )
            installed = _verify_regular_named(parent_fd, leaf, intended.inode)
            if _inode_from_stat(installed) != intended.inode:
                raise _ConcurrentMutationError(
                    f"concurrent migration target replacement: {path}"
                )
            self.ownership_check()
            actual = self._capture_binding(path)
            self._verify_intended_file(intended, actual)
            self.bindings[path] = actual
            delta = self._target_delta(path, file_binding=actual)
            self._after_mutation(checkpointed, intent, delta)
            self.ownership_check()
            verified = self._capture_binding(path)
            self._verify_intended_file(actual, verified)
            if current is not None:
                _quarantine_remove_name(
                    parent_fd,
                    stage_name,
                    current.inode,
                    "migration staged displaced file cleanup",
                )
                settled = self._capture_binding(path)
                self._verify_intended_file(actual, settled)
                self.bindings[path] = settled
                self._settle_mutation(
                    checkpointed,
                    intent,
                    self._target_delta(path, file_binding=settled),
                )
        except BaseException:
            if not intent_published:
                staged = _named_stat(parent_fd, stage_name)
                if staged is not None and stat.S_ISREG(staged.st_mode):
                    _quarantine_remove_name(
                        parent_fd,
                        stage_name,
                        _inode_from_stat(staged),
                        "migration unpublished stage cleanup",
                    )
            raise
        finally:
            os.close(parent_fd)
        self.ownership_check()
        self._verify_parent_pins(path)
        self._verify_temporary_siblings(path)

    def ensure_directory(self, path):
        path = self._canonical_path(path, allow_pinned_parent=True)
        self.ownership_check()
        authority_pin = self._verify_directory_authority(path)
        existed = os.path.lexists(path)
        if not existed and path not in self.allowed_directories:
            raise RuntimeError(f"migration directory creation is not frozen: {path}")
        parent_fd, leaf = _open_handle_parent(
            self.handle,
            path,
            self.allowed_directories | self.created_directories,
        )
        fd = None
        staging = self._stage_path(path)
        stage_name = staging.name
        intent_published = False
        try:
            if existed:
                fd = os.open(leaf, _directory_flags(), dir_fd=parent_fd)
            else:
                if _named_stat(parent_fd, leaf) is not None:
                    raise RuntimeError(
                        f"migration absent directory appeared during creation: {path}"
                    )
                os.mkdir(stage_name, mode=0o700, dir_fd=parent_fd)
                stage_fd = os.open(stage_name, _directory_flags(), dir_fd=parent_fd)
                try:
                    intended_inode = _inode_from_stat(os.fstat(stage_fd))
                finally:
                    os.close(stage_fd)
                intent = self._directory_intent(
                    "create-directory",
                    path,
                    staging,
                    None,
                    intended_inode,
                )
                checkpointed = self._before_mutation(intent)
                intent_published = checkpointed
                _rename_exclusive(parent_fd, stage_name, parent_fd, leaf)
                fd = os.open(leaf, _directory_flags(), dir_fd=parent_fd)
            current = os.fstat(fd)
            if not stat.S_ISDIR(current.st_mode):
                raise RuntimeError(f"migration directory changed type: {path}")
            inode = _inode_from_stat(current)
            expected = self.directories.get(path)
            if expected is not None and inode != expected[0]:
                raise RuntimeError(f"migration directory inode changed: {path}")
            if (
                expected is None
                and existed
                and (authority_pin is None or inode != authority_pin.inode)
            ):
                raise RuntimeError(f"migration directory appeared without binding: {path}")
            self.directories[path] = (inode, current.st_mode, current.st_nlink)
            if not existed:
                if inode != intended_inode:
                    raise _ConcurrentMutationError(
                        f"concurrent migration directory replacement: {path}"
                    )
                self.created_directories.add(path)
                delta = self._target_delta(path, directory_exists=True)
                self._after_mutation(checkpointed, intent, delta)
                current_after = os.stat(path, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(current_after.st_mode)
                    or _inode_from_stat(current_after) != intended_inode
                ):
                    raise _ConcurrentMutationError(
                        f"concurrent migration directory replacement: {path}"
                    )
        except BaseException:
            if not intent_published:
                staged = _named_stat(parent_fd, stage_name)
                if staged is not None and stat.S_ISDIR(staged.st_mode):
                    os.rmdir(stage_name, dir_fd=parent_fd)
            raise
        finally:
            if fd is not None:
                os.close(fd)
            os.close(parent_fd)
        self.ownership_check()
        self._verify_directory_authority(path)

    def remove_file(self, path):
        path = self._canonical_path(path)
        self.ownership_check()
        self._verify_parent_pins(path)
        binding = self.bindings.get(path)
        if binding is None:
            raise RuntimeError(f"migration file removal is not bound: {path}")
        parent_fd, leaf = _open_handle_parent(self.handle, path)
        staging = self._stage_path(path)
        stage_name = staging.name
        try:
            _verify_named_input_binding(parent_fd, leaf, binding)
            intent = self._file_intent(
                "remove-file",
                path,
                staging,
                binding,
                None,
            )
            checkpointed = self._before_mutation(intent)
            _rename_exclusive(parent_fd, leaf, parent_fd, stage_name)
            if not _named_file_matches(parent_fd, stage_name, binding):
                raise _ConcurrentMutationError(
                    f"concurrent migration staged file replacement: {staging}"
                )
            if _named_stat(parent_fd, leaf) is not None:
                raise _ConcurrentMutationError(
                    f"concurrent migration file removal: {path}"
                )
            self.bindings.pop(path, None)
            delta = self._target_delta(path, file_binding=None)
            self._after_mutation(checkpointed, intent, delta)
            if _named_stat(parent_fd, leaf) is not None:
                raise _ConcurrentMutationError(
                    f"concurrent migration file removal: {path}"
                )
            _quarantine_remove_name(
                parent_fd,
                stage_name,
                binding.inode,
                "migration staged removed file cleanup",
            )
            self._settle_mutation(
                checkpointed,
                intent,
                self._target_delta(path, file_binding=None),
            )
        finally:
            os.close(parent_fd)
        self.ownership_check()
        self._verify_parent_pins(path)

    def remove_directory(self, path):
        path = self._canonical_path(path)
        self.ownership_check()
        self._verify_parent_pins(path)
        expected = self.directories.get(path)
        if expected is None:
            raise RuntimeError(f"migration directory removal is not bound: {path}")
        parent_fd, leaf = _open_handle_parent(self.handle, path)
        staging = self._stage_path(path)
        stage_name = staging.name
        fd = None
        try:
            fd = os.open(leaf, _directory_flags(), dir_fd=parent_fd)
            current = os.fstat(fd)
            if _inode_from_stat(current) != expected[0]:
                raise RuntimeError(f"migration directory inode changed: {path}")
            if os.listdir(fd):
                raise OSError(errno.ENOTEMPTY, os.strerror(errno.ENOTEMPTY), path)
            intent = self._directory_intent(
                "remove-directory",
                path,
                staging,
                expected[0],
                None,
            )
            checkpointed = self._before_mutation(intent)
            os.close(fd)
            fd = None
            _rename_exclusive(parent_fd, leaf, parent_fd, stage_name)
            staged = _named_stat(parent_fd, stage_name)
            if (
                staged is None
                or not stat.S_ISDIR(staged.st_mode)
                or _inode_from_stat(staged) != expected[0]
                or _named_stat(parent_fd, leaf) is not None
            ):
                raise _ConcurrentMutationError(
                    f"concurrent migration directory removal: {path}"
                )
            self.directories.pop(path, None)
            delta = self._target_delta(path, directory_exists=False)
            self._after_mutation(checkpointed, intent, delta)
            staged_fd = os.open(stage_name, _directory_flags(), dir_fd=parent_fd)
            try:
                if os.listdir(staged_fd):
                    raise _ConcurrentMutationError(
                        f"concurrent migration directory removal: {path}"
                    )
            finally:
                os.close(staged_fd)
            if _named_stat(parent_fd, leaf) is not None:
                raise _ConcurrentMutationError(
                    f"concurrent migration directory removal: {path}"
                )
            os.rmdir(stage_name, dir_fd=parent_fd)
            self._settle_mutation(
                checkpointed,
                intent,
                self._target_delta(path, directory_exists=False),
            )
        finally:
            if fd is not None:
                os.close(fd)
            os.close(parent_fd)
        self.ownership_check()
        self._verify_parent_pins(path)


@dataclass
class _ProjectSourcePin:
    parent_fd: int
    source_fd: int
    name: str
    inode: tuple[int, int]


def _pin_project_source(plan):
    parent_fd, source_name = _open_absolute_parent(plan.source_project)
    try:
        source_fd = os.open(source_name, _directory_flags(), dir_fd=parent_fd)
    except Exception:
        os.close(parent_fd)
        raise
    try:
        pin = _ProjectSourcePin(
            parent_fd=parent_fd,
            source_fd=source_fd,
            name=source_name,
            inode=_inode_from_stat(os.fstat(source_fd)),
        )
        named = os.stat(source_name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(named.st_mode) or _inode_from_stat(named) != pin.inode:
            raise RuntimeError("source project changed before pinning")
        return pin
    except Exception:
        os.close(source_fd)
        os.close(parent_fd)
        raise


def _close_project_source_pin(pin):
    os.close(pin.source_fd)
    os.close(pin.parent_fd)


def _project_rename_intent(plan, source_pin):
    return {
        "version": 1,
        "operation": "rename-directory",
        "target": _manifest_path_record(plan, plan.source_project),
        "staging": _manifest_path_record(plan, plan.destination_project),
        "before": {"inode": list(source_pin.inode)},
        "intended": {"inode": list(source_pin.inode)},
    }


def _rename_project_directory(plan, backup_handle, source_pin):
    _inspect_sealed_backup(backup_handle, expected_plan=plan)
    parent_fd = source_pin.parent_fd
    source_name = source_pin.name
    if _inode_from_stat(os.fstat(source_pin.source_fd)) != source_pin.inode:
        raise RuntimeError("source project descriptor changed before rename")
    named = os.stat(source_name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(named.st_mode) or _inode_from_stat(named) != source_pin.inode:
        raise RuntimeError("source project changed before rename")
    if _named_stat(parent_fd, plan.destination_project.name) is not None:
        raise RuntimeError("destination project appeared before rename")
    _rename_exclusive(
        parent_fd,
        source_name,
        parent_fd,
        plan.destination_project.name,
    )
    postcondition_error = None
    moved_inode = None
    try:
        moved = os.stat(
            plan.destination_project.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        moved_inode = _inode_from_stat(moved)
        if not stat.S_ISDIR(moved.st_mode) or moved_inode != source_pin.inode:
            raise RuntimeError("renamed source directory changed identity")
    except Exception as exc:
        postcondition_error = exc
    if postcondition_error is not None:
        source_current = _named_stat(parent_fd, source_name)
        destination_current = _named_stat(
            parent_fd,
            plan.destination_project.name,
        )
        source_is_original = (
            source_current is not None
            and stat.S_ISDIR(source_current.st_mode)
            and _inode_from_stat(source_current) == source_pin.inode
        )
        destination_is_original = (
            destination_current is not None
            and stat.S_ISDIR(destination_current.st_mode)
            and _inode_from_stat(destination_current) == source_pin.inode
        )
        if source_is_original and destination_current is None:
            recovered = True
        elif source_current is None and destination_is_original:
            try:
                _restore_pending_project_rename(
                    parent_fd,
                    source_name,
                    plan.destination_project.name,
                    source_pin.inode,
                )
                recovered = True
            except Exception as recovery_error:
                raise _ProjectRenameRace(
                    "concurrent source directory mutation; pinned recovery failed: "
                    f"{recovery_error}",
                    rename_occurred=True,
                    recovered=False,
                ) from recovery_error
        else:
            raise _ProjectRenameRace(
                "concurrent source directory mutation; original inode is not at "
                "an exclusive source or destination name",
                rename_occurred=True,
                recovered=False,
            ) from postcondition_error
        raise _ProjectRenameRace(
            "concurrent source directory mutation; moved directory was restored: "
            f"{postcondition_error}",
            rename_occurred=True,
            recovered=recovered,
        ) from postcondition_error


def _rewritten_config_bytes(plan):
    parsed = yaml.safe_load(_read_utf8(plan.config_path, "config")) or {}
    if not isinstance(parsed, dict):
        raise ValueError("migration config must contain a YAML mapping")
    projects = parsed.get("projects") or []
    if not isinstance(projects, list):
        raise ValueError("config projects must be a list")
    mapping_aliases = []
    for item in projects:
        if not isinstance(item, dict) or item.get("name") != plan.old_slug:
            continue
        aliases = item.get("keywords") or []
        if not isinstance(aliases, list):
            raise ValueError("mapping project keywords must be a list")
        mapping_aliases.extend(
            alias for alias in aliases if isinstance(alias, str)
        )
    parsed["projects"] = [
        _rewrite_project_value(item, plan.old_slug, plan.new_slug)
        for item in projects
    ]
    keywords = parsed.get("project_keywords") or {}
    if not isinstance(keywords, dict):
        raise ValueError("project_keywords must be a mapping")
    old_keywords = keywords.pop(plan.old_slug, []) or []
    existing = keywords.get(plan.new_slug, []) or []
    if not isinstance(old_keywords, list) or not isinstance(existing, list):
        raise ValueError("project keyword entries must be lists")
    keywords[plan.new_slug] = list(
        dict.fromkeys(
            [
                plan.new_slug,
                plan.old_slug,
                *existing,
                *mapping_aliases,
                *old_keywords,
            ]
        )
    )
    parsed["project_keywords"] = keywords
    return yaml.safe_dump(
        parsed,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).encode("utf-8")


def _select_rebuilder_mode(rebuilders):
    if rebuilders is None:
        return "default"
    if type(rebuilders) in {list, tuple} and len(rebuilders) == 0:
        return "none"
    raise ValueError(
        "custom rebuilders are unsupported; use the fixed default chain "
        "or an exact empty list/tuple"
    )


def _capture_post_state(plan):
    contract = plan.mutation_contract
    def transaction_paths(path):
        path = Path(path)
        mapped = _post_migration_path(
            path,
            plan.source_project,
            plan.destination_project,
        )
        return (path,) if mapped == path else (path, mapped)

    roots = tuple(
        sorted(
            {
                candidate
                for path in contract.mutable_roots
                for candidate in transaction_paths(path)
            }
        )
    )
    excluded_subtrees = {
        candidate
        for path in contract.excluded_mutable_subtrees
        for candidate in transaction_paths(path)
    }
    files = set()
    directories = set()
    for root in roots:
        if not os.path.lexists(root):
            continue
        current_files, current_directories = _walk_tree(
            root,
            f"post-migration mutable root {root}",
            excluded_roots=excluded_subtrees,
        )
        files.update(current_files)
        directories.update(current_directories)
    explicit_paths = (
        set(contract.mutable_files)
        | set(plan.markdown_paths)
        | set(contract.absent_paths)
        | set(contract.mutable_directories)
        | set(contract.absent_directories)
    )
    for original in explicit_paths:
        for path in transaction_paths(original):
            if any(
                path == root or path.is_relative_to(root) for root in roots
            ) and not any(
                path == excluded or path.is_relative_to(excluded)
                for excluded in excluded_subtrees
            ):
                continue
            state = _path_state(path)
            if state == "file":
                files.add(path)
            elif state == "directory":
                directories.add(path)
    bindings = _capture_input_bindings(sorted(files))
    expected_existing = {
        candidate
        for binding in plan.input_bindings
        for candidate in transaction_paths(binding.path)
    }
    expected_directories = {
        candidate
        for binding in plan.directory_bindings
        for candidate in transaction_paths(binding.path)
    }
    created_files = tuple(sorted(files - expected_existing))
    created_directories = tuple(sorted(directories - expected_directories))
    directory_states = []
    for path in sorted(directories):
        current = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode):
            raise RuntimeError(f"post-migration directory changed type: {path}")
        directory_states.append(
            {
                **_manifest_path_record(plan, path),
                "inode": list(_inode_from_stat(current)),
                "mode": current.st_mode,
                "link_count": current.st_nlink,
                "atime_ns": current.st_atime_ns,
                "mtime_ns": current.st_mtime_ns,
                "ctime_ns": current.st_ctime_ns,
                "size": current.st_size,
            }
        )
    return {
        "bindings": bindings,
        "directories": tuple(directory_states),
        "created_files": created_files,
        "created_directories": created_directories,
    }


def _serialize_post_binding(plan, binding):
    return _manifest_input_binding(plan, binding)


def _intent_plan(handle):
    vault = handle.base_vault
    contract = handle.base_contract
    source_project = vault / "01-Projects" / handle.payload["old_slug"]
    destination_project = vault / "01-Projects" / handle.payload["new_slug"]
    rewrite_paths = {
        Path(phase[len("rewrite:") :])
        for phase in _required_apply_checkpoint_phases(handle)
        if phase.startswith("rewrite:")
    }
    return SimpleNamespace(
        vault=vault,
        mutation_contract=contract,
        source_project=source_project,
        destination_project=destination_project,
        explicit_mutation_targets={
            candidate
            for path in rewrite_paths
            for candidate in (
                path,
                _post_migration_path(
                    path,
                    source_project,
                    destination_project,
                ),
            )
        },
    )


def _intent_binding(plan, item, label):
    if not isinstance(item, dict):
        raise ValueError(f"{label} is invalid")
    try:
        binding = InputBinding(
            path=_record_to_path(plan.vault, item, label),
            sha256=item["sha256"],
            inode=tuple(item["inode"]),
            link_count=item["link_count"],
            mode=item["mode"],
            size=item["size"],
            mtime_ns=item["mtime_ns"],
            ctime_ns=item["ctime_ns"],
            expected_type=item["expected_type"],
        )
    except (KeyError, TypeError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if (
        binding.expected_type != "regular_file"
        or binding.link_count != 1
        or not stat.S_ISREG(binding.mode)
        or len(binding.inode) != 2
        or not all(isinstance(value, int) for value in binding.inode)
        or not isinstance(binding.sha256, str)
        or len(binding.sha256) != 64
        or any(character not in "0123456789abcdef" for character in binding.sha256)
        or not isinstance(binding.size, int)
        or binding.size < 0
        or not isinstance(binding.mtime_ns, int)
        or not isinstance(binding.ctime_ns, int)
    ):
        raise ValueError(f"{label} is invalid")
    return binding


def _intent_directory_inode(item, label):
    if not isinstance(item, dict) or set(item) != {"inode"}:
        raise ValueError(f"{label} is invalid")
    inode = item["inode"]
    if (
        not isinstance(inode, list)
        or len(inode) != 2
        or not all(isinstance(value, int) for value in inode)
    ):
        raise ValueError(f"{label} is invalid")
    return tuple(inode)


def _parse_mutation_intent(plan, intent):
    if not isinstance(intent, dict) or set(intent) != {
        "version",
        "operation",
        "target",
        "staging",
        "before",
        "intended",
    }:
        raise ValueError("migration mutation intent is invalid")
    if intent["version"] != 1:
        raise ValueError("migration mutation intent version is invalid")
    operation = intent["operation"]
    if operation not in {
        "write-file",
        "remove-file",
        "create-directory",
        "remove-directory",
        "rename-directory",
    }:
        raise ValueError("migration mutation intent operation is invalid")
    target = _record_to_path(plan.vault, intent["target"], "mutation target")
    staging = _record_to_path(plan.vault, intent["staging"], "mutation staging")
    if operation == "rename-directory":
        if (
            target != plan.source_project
            or staging != plan.destination_project
            or staging.parent != target.parent
        ):
            raise ValueError("migration project rename intent path is invalid")
    elif (
        staging.parent != target.parent
        or not staging.name.startswith(
            f".{target.name}.agent-memory-beacon-stage-"
        )
    ):
        raise ValueError("migration mutation staging path is invalid")

    contract = plan.mutation_contract
    explicit = {
        candidate
        for spec in contract.target_specs
        for candidate in (
            spec.path,
            _post_migration_path(
                spec.path,
                plan.source_project,
                plan.destination_project,
            ),
        )
    }
    explicit.update(getattr(plan, "explicit_mutation_targets", ()))
    roots = {
        candidate
        for root in contract.mutable_roots
        for candidate in (
            root,
            _post_migration_path(
                root,
                plan.source_project,
                plan.destination_project,
            ),
        )
    }
    excluded = {
        candidate
        for root in contract.excluded_mutable_subtrees
        for candidate in (
            root,
            _post_migration_path(
                root,
                plan.source_project,
                plan.destination_project,
            ),
        )
    }
    if target not in explicit and not any(
        target == root or target.is_relative_to(root) for root in roots
    ):
        raise ValueError("migration mutation intent target is outside contract")
    if target not in explicit and any(
        target == root or target.is_relative_to(root) for root in excluded
    ):
        raise ValueError("migration mutation intent target is excluded")

    before = intent["before"]
    intended = intent["intended"]
    if operation == "write-file":
        before_binding = (
            None
            if before is None
            else _intent_binding(plan, before, "mutation before binding")
        )
        intended_binding = _intent_binding(
            plan, intended, "mutation intended binding"
        )
        if (
            intended_binding.path != target
            or (before_binding is not None and before_binding.path != target)
        ):
            raise ValueError("migration mutation intent binding path is invalid")
        return SimpleNamespace(
            operation=operation,
            target=target,
            staging=staging,
            before=before_binding,
            intended=intended_binding,
        )
    if operation == "remove-file":
        before_binding = _intent_binding(
            plan, before, "mutation before binding"
        )
        if before_binding.path != target or intended is not None:
            raise ValueError("migration remove-file intent is invalid")
        return SimpleNamespace(
            operation=operation,
            target=target,
            staging=staging,
            before=before_binding,
            intended=None,
        )
    before_inode = (
        None
        if before is None
        else _intent_directory_inode(before, "mutation before directory")
    )
    intended_inode = (
        None
        if intended is None
        else _intent_directory_inode(intended, "mutation intended directory")
    )
    if operation == "create-directory" and (
        before_inode is not None or intended_inode is None
    ):
        raise ValueError("migration create-directory intent is invalid")
    if operation == "remove-directory" and (
        before_inode is None or intended_inode is not None
    ):
        raise ValueError("migration remove-directory intent is invalid")
    if operation == "rename-directory" and (
        before_inode is None
        or intended_inode is None
        or before_inode != intended_inode
    ):
        raise ValueError("migration project rename intent is invalid")
    return SimpleNamespace(
        operation=operation,
        target=target,
        staging=staging,
        before=before_inode,
        intended=intended_inode,
    )


def _mutation_intent_requires_cleanup(parsed):
    return (
        parsed.operation in {"remove-file", "remove-directory"}
        or (
            parsed.operation == "write-file"
            and parsed.before is not None
        )
    )


def _validate_checkpoint_mutation_record(handle, record):
    delta = record.get("delta", {})
    intent = record.get("mutation_intent")
    if record["status"] == "applying" and record["boundary"] == "before":
        if delta:
            raise ValueError("migration apply before checkpoint delta is not empty")
    if intent is None:
        return None
    plan = _intent_plan(handle)
    parsed = _parse_mutation_intent(plan, intent)
    kind = _checkpoint_phase_kind(record["phase"])
    source_intent = (
        kind == "source" and parsed.operation == "rename-directory"
    )
    ordinary_intent = (
        parsed.operation != "rename-directory"
        and kind
        in {
            "rewrite",
            "config",
            "rebuild:memory-index",
            "rebuild:maps",
            "rebuild:compiler",
        }
    )
    rollback_reconciliation = (
        record["status"] == "rolling_back"
        and kind == "rollback-start"
        and record["boundary"] == "before"
    )
    applying_intent = (
        record["status"] == "applying"
        and (source_intent or ordinary_intent)
    )
    if not (applying_intent or rollback_reconciliation):
        raise ValueError("migration mutation intent phase is invalid")
    if record["boundary"] == "after":
        changed = _checkpoint_changed_items(delta)
        if source_intent:
            source_paths = {
                binding.path
                for binding in handle.bindings
                if binding.path == plan.source_project
                or binding.path.is_relative_to(plan.source_project)
            }
            source_paths.update(
                binding.path
                for binding in _manifest_directory_bindings_from_payload(
                    handle.base_payload,
                    handle.base_vault,
                )
                if binding.path == plan.source_project
                or binding.path.is_relative_to(plan.source_project)
            )
            source_paths.add(plan.source_project.parent)
            allowed_paths = {
                candidate
                for path in source_paths
                for candidate in (
                    path,
                    _post_migration_path(
                        path,
                        plan.source_project,
                        plan.destination_project,
                    ),
                )
            }
        else:
            allowed_paths = {parsed.target, parsed.target.parent}
        allowed = {
            (
                _manifest_path_record(plan, path)["kind"],
                _manifest_path_record(plan, path)["path"],
            )
            for path in allowed_paths
        }
        if any(paths - allowed for paths in changed.values()):
            raise ValueError("migration mutation checkpoint delta escaped target")
    return parsed


def _serialize_application(plan, state):
    return {
        "created_files": [
            _manifest_path_record(plan, path)
            for path in state["created_files"]
        ],
        "created_directories": [
            _manifest_path_record(plan, path)
            for path in state["created_directories"]
        ],
        "post_bindings": [
            _serialize_post_binding(plan, binding)
            for binding in state["bindings"]
        ],
        "post_directories": list(state["directories"]),
    }


CHECKPOINT_SCHEMA_VERSION = 3
_NO_DELTA_OVERRIDE = object()


class _CheckpointFailure(RuntimeError):
    pass


def _application_items_by_path(items):
    return {(item["kind"], item["path"]): item for item in items}


_APPLICATION_KEYS = (
    "created_files",
    "created_directories",
    "post_bindings",
    "post_directories",
)


def _sorted_application_items(items):
    validated = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("migration checkpoint application entry is invalid")
        identity = (item.get("kind"), item.get("path"))
        if not all(isinstance(value, str) and value for value in identity):
            raise ValueError("migration checkpoint application path is invalid")
        if identity in seen:
            raise ValueError("migration checkpoint application paths are duplicated")
        seen.add(identity)
        validated.append(item)
    return sorted(validated, key=lambda item: (item["kind"], item["path"]))


def _application_index(application):
    if not isinstance(application, dict) or set(application) != set(_APPLICATION_KEYS):
        raise ValueError("migration checkpoint application is invalid")
    index = {}
    for key in _APPLICATION_KEYS:
        values = application[key]
        if not isinstance(values, list):
            raise ValueError("migration checkpoint application entries are invalid")
        indexed = _application_items_by_path(_sorted_application_items(values))
        index[key] = indexed
    return index


def _materialize_application(index):
    return {
        key: sorted(
            index[key].values(),
            key=lambda item: (item["kind"], item["path"]),
        )
        for key in _APPLICATION_KEYS
    }


def _application_delta(previous, current):
    return _application_delta_indexes(
        _application_index(previous),
        _application_index(current),
    )


def _application_delta_indexes(before, after):
    delta = {}
    for key in _APPLICATION_KEYS:
        before_items = before[key]
        after_items = after[key]
        upsert = [
            item
            for item_key, item in after_items.items()
            if before_items.get(item_key) != item
        ]
        remove = [
            item
            for item_key, item in before_items.items()
            if item_key not in after_items
        ]
        if upsert or remove:
            delta[key] = {"upsert": upsert, "remove": remove}
    return delta


def _apply_application_delta(previous, delta):
    index = _application_index(previous)
    _apply_application_delta_index(index, delta)
    return _materialize_application(index)


def _apply_application_delta_index(index, delta):
    operations = _application_delta_operations(index, delta)
    for key, removed_keys, upsert in operations:
        current = index[key]
        for item_key in removed_keys:
            current.pop(item_key)
        for item in upsert:
            current[(item["kind"], item["path"])] = item


def _application_delta_operations(index, delta):
    if not isinstance(delta, dict):
        raise ValueError("migration checkpoint delta is invalid")
    if set(delta) - set(_APPLICATION_KEYS):
        raise ValueError("migration checkpoint delta has unknown fields")
    operations = []
    for key in _APPLICATION_KEYS:
        current = index[key]
        change = delta.get(key, {})
        if not isinstance(change, dict) or set(change) - {"upsert", "remove"}:
            raise ValueError("migration checkpoint delta operation is invalid")
        upsert = change.get("upsert", [])
        remove = change.get("remove", [])
        if not isinstance(upsert, list) or not isinstance(remove, list):
            raise ValueError("migration checkpoint delta entries are invalid")
        removed_keys = []
        for item in remove:
            item_key = (
                (item.get("kind"), item.get("path"))
                if isinstance(item, dict)
                else None
            )
            if item_key not in current or current[item_key] != item:
                raise ValueError("migration checkpoint delta removal is not exact")
            removed_keys.append(item_key)
        if len(set(removed_keys)) != len(removed_keys):
            raise ValueError("migration checkpoint delta removal is duplicated")
        ordered_upsert = _sorted_application_items(upsert)
        operations.append((key, tuple(removed_keys), tuple(ordered_upsert)))
    return operations


def _application_delta_from_index(index, current):
    return _application_delta_indexes(index, _application_index(current))


def _targeted_checkpoint_delta(plan, handle, phase, boundary):
    if boundary == "before" or phase == "rollback-complete":
        return {}, None
    kind = _checkpoint_phase_kind(phase)
    if kind in {
        "source",
        "rebuild:memory-index",
        "rebuild:maps",
        "rebuild:compiler",
        "validation",
    }:
        current = _serialize_application(plan, _capture_post_state(plan))
        return _application_delta_from_index(
            handle.checkpoint_application_index,
            current,
        ), current

    index = handle.checkpoint_application_index
    delta = {}

    def set_record(key, path, record):
        identity = _manifest_path_record(plan, path)
        item_key = (identity["kind"], identity["path"])
        existing = index[key].get(item_key)
        if record is None:
            if existing is not None:
                delta.setdefault(key, {}).setdefault("remove", []).append(existing)
            return
        if existing != record:
            delta.setdefault(key, {}).setdefault("upsert", []).append(record)

    def update_directory(path, force_tracked=False):
        identity = _manifest_path_record(plan, path)
        item_key = (identity["kind"], identity["path"])
        tracked = item_key in index["post_directories"]
        authorized = {
            candidate
            for original in (
                *plan.mutation_contract.mutable_directories,
                *plan.mutation_contract.absent_directories,
            )
            for candidate in (
                original,
                _post_migration_path(
                    original,
                    plan.source_project,
                    plan.destination_project,
                ),
            )
        }
        if not force_tracked and not tracked and path not in authorized:
            return
        if not os.path.lexists(path):
            set_record("post_directories", path, None)
            return
        current = os.stat(path, follow_symlinks=False)
        if not stat.S_ISDIR(current.st_mode):
            raise RuntimeError(f"checkpoint directory changed type: {path}")
        set_record(
            "post_directories",
            path,
            {
                **identity,
                "inode": list(_inode_from_stat(current)),
                "mode": current.st_mode,
                "link_count": current.st_nlink,
                "atime_ns": current.st_atime_ns,
                "mtime_ns": current.st_mtime_ns,
                "ctime_ns": current.st_ctime_ns,
                "size": current.st_size,
            },
        )

    if kind == "rollback-restore-directories":
        for binding in _manifest_directory_bindings_from_payload(
            handle.base_payload,
            handle.base_vault,
        ):
            update_directory(binding.path, force_tracked=True)
        return delta, None

    path = None
    if kind == "config":
        path = Path(plan.config_path)
    elif kind == "rewrite":
        original = Path(phase.split(":", 1)[1])
        path = _post_migration_path(
            original,
            plan.source_project,
            plan.destination_project,
        )
    else:
        prefixes = {
            "rollback-remove-file": "rollback-remove-file:",
            "rollback-remove-directory": "rollback-remove-directory:",
            "rollback-restore-file": "rollback-restore-file:",
            "rollback-directory-metadata": "rollback-directory-metadata:",
        }
        prefix = prefixes.get(kind)
        if prefix is not None:
            path = Path(phase[len(prefix) :])
    if path is None:
        return {}, None
    if kind in {"rewrite", "config", "rollback-restore-file"}:
        binding = _capture_input_binding(path) if os.path.lexists(path) else None
        set_record(
            "post_bindings",
            path,
            _serialize_post_binding(plan, binding) if binding is not None else None,
        )
    elif kind == "rollback-remove-file":
        set_record("post_bindings", path, None)
        set_record("created_files", path, None)
    elif kind == "rollback-remove-directory":
        set_record("post_directories", path, None)
        set_record("created_directories", path, None)
    elif kind == "rollback-directory-metadata":
        update_directory(path)
    if kind != "rollback-directory-metadata":
        update_directory(path.parent)
    return delta, None


def _checkpoint_prefix(handle):
    return "checkpoint-"


def _checkpoint_temp_prefix(handle):
    return "checkpoint-tmp-"


def _validate_checkpoint_application(handle, record):
    payload = dict(handle.base_payload or handle.payload)
    payload["status"] = "applied"
    payload["application"] = record.get("application")
    return _manifest_application(payload, handle.base_vault, handle.base_contract)


def _checkpoint_phase_kind(phase):
    if phase.startswith("cleanup:") and len(phase) > len("cleanup:"):
        nested = phase[len("cleanup:") :]
        if nested.startswith("cleanup:"):
            raise ValueError("brand migration cleanup phase is invalid")
        return _checkpoint_phase_kind(nested)
    if phase == "source-rename":
        return "source"
    if phase == "rewrite:config":
        return "config"
    if phase.startswith("rewrite:") and len(phase) > len("rewrite:"):
        return "rewrite"
    if phase in {"rebuild:memory-index", "rebuild:maps", "rebuild:compiler"}:
        return phase
    if phase == "validation-finalization":
        return "validation"
    if phase == "manual-recovery":
        return "manual-recovery"
    if phase == "rollback-start":
        return "rollback-start"
    for prefix, kind in (
        ("rollback-remove-file:", "rollback-remove-file"),
        ("rollback-remove-directory:", "rollback-remove-directory"),
        ("rollback-restore-file:", "rollback-restore-file"),
        ("rollback-directory-metadata:", "rollback-directory-metadata"),
    ):
        if phase.startswith(prefix) and len(phase) > len(prefix):
            return kind
    if phase == "rollback-restore-directories":
        return phase
    if phase == "rollback-complete":
        return phase
    raise ValueError("brand migration checkpoint phase is invalid")


def _required_apply_checkpoint_phases(handle):
    if handle.required_apply_phases is not None:
        return handle.required_apply_phases
    vault = handle.base_vault
    contract = handle.base_contract
    bindings = handle.bindings
    records_by_path = {
        _record_to_path(vault, item, "manifest file"): item
        for item in handle.payload["files"]
    }
    phases = {"source-rename"}
    for binding in bindings:
        if binding.path.suffix.lower() != ".md":
            continue
        item = records_by_path[binding.path]
        fd = _open_staging_file(handle.root_fd, Path(item["backup"]))
        try:
            data = _read_fd_bytes(fd)
        finally:
            os.close(fd)
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        _updated, changed = rewrite_markdown(
            content,
            handle.payload["old_slug"],
            handle.payload["new_slug"],
        )
        if changed:
            phases.add(f"rewrite:{binding.path}")
    if any(spec.role == "config_path" for spec in contract.target_specs):
        phases.add("rewrite:config")
    handle.required_apply_phases = frozenset(phases)
    return handle.required_apply_phases


def _validate_applied_checkpoint_program(handle, records, record):
    applying = [
        item[0]
        for item in (*records, (record, None, None, None))
        if item[0]["status"] in {"applying", "applied"}
    ]
    boundaries = {}
    for item in applying:
        boundaries.setdefault(item["phase"], []).append(item["boundary"])
    required = _required_apply_checkpoint_phases(handle)
    for phase in required:
        if boundaries.get(phase) != ["before", "after"]:
            raise ValueError("brand migration required apply phase was skipped")
    rebuild = {
        phase
        for phase in boundaries
        if phase.startswith("rebuild:")
    }
    expected_rebuild = {
        "rebuild:memory-index",
        "rebuild:maps",
        "rebuild:compiler",
    }
    if rebuild not in (set(), expected_rebuild):
        raise ValueError("brand migration fixed rebuild chain is incomplete")
    for phase in rebuild:
        phase_boundaries = boundaries.get(phase, [])
        if (
            not phase_boundaries
            or len(phase_boundaries) % 2
            or any(
                phase_boundaries[index : index + 2] != ["before", "after"]
                for index in range(0, len(phase_boundaries), 2)
            )
        ):
            raise ValueError("brand migration fixed rebuild boundary is incomplete")
    for index, item in enumerate(applying[:-1]):
        raw_intent = item.get("mutation_intent")
        if (
            raw_intent is None
            or item["boundary"] != "after"
            or item["phase"].startswith("cleanup:")
        ):
            continue
        parsed = _parse_mutation_intent(_intent_plan(handle), raw_intent)
        if not _mutation_intent_requires_cleanup(parsed):
            continue
        following = applying[index + 1]
        if not (
            following["status"] == "applying"
            and following["boundary"] == "after"
            and following["phase"] == f"cleanup:{item['phase']}"
            and following.get("mutation_intent") == raw_intent
        ):
            raise ValueError("required cleanup checkpoint is missing")


def _checkpoint_changed_items(delta):
    changed = {}
    for key, operation in delta.items():
        values = (*operation.get("remove", ()), *operation.get("upsert", ()))
        changed[key] = {
            (item["kind"], item["path"])
            for item in values
        }
    return changed


def _validate_rollback_phase_delta(handle, record):
    if record["status"] not in {"rolling_back", "rolled_back"}:
        return
    kind = _checkpoint_phase_kind(record["phase"])
    delta = record.get("delta", {})
    if kind == "rollback-complete":
        if delta:
            raise ValueError("rollback boundary delta is not empty")
        return
    changed = _checkpoint_changed_items(delta)
    vault = handle.base_vault
    plan = SimpleNamespace(vault=vault)

    def identity(path):
        item = _manifest_path_record(plan, path)
        return item["kind"], item["path"]

    if kind == "rollback-start":
        raw_intent = record.get("mutation_intent")
        if raw_intent is None:
            if delta:
                raise ValueError("rollback boundary delta is not empty")
            return
        if not delta:
            raise ValueError("rollback reconciliation delta is empty")
        parsed = _parse_mutation_intent(_intent_plan(handle), raw_intent)
        allowed_paths = {parsed.target, parsed.target.parent}
        allowed_identities = {identity(path) for path in allowed_paths}
        if any(paths - allowed_identities for paths in changed.values()):
            raise ValueError(
                "rollback reconciliation delta escaped mutation target"
            )
        return

    path = None
    for prefix in (
        "rollback-remove-file:",
        "rollback-remove-directory:",
        "rollback-restore-file:",
        "rollback-directory-metadata:",
    ):
        if record["phase"].startswith(prefix):
            path = Path(record["phase"][len(prefix) :])
            break
    allowed = {key: set() for key in _APPLICATION_KEYS}
    if kind == "rollback-remove-file":
        allowed["created_files"].add(identity(path))
        allowed["post_bindings"].add(identity(path))
        allowed["post_directories"].add(identity(path.parent))
    elif kind == "rollback-remove-directory":
        allowed["created_directories"].add(identity(path))
        allowed["post_directories"].update({identity(path), identity(path.parent)})
    elif kind == "rollback-restore-file":
        allowed["post_bindings"].add(identity(path))
        allowed["post_directories"].add(identity(path.parent))
    elif kind == "rollback-directory-metadata":
        allowed["post_directories"].add(identity(path))
    elif kind == "rollback-restore-directories":
        old_root = vault / "01-Projects" / handle.payload["old_slug"]
        new_root = vault / "01-Projects" / handle.payload["new_slug"]
        paths = {
            binding.path
            for binding in _manifest_directory_bindings_from_payload(
                handle.payload,
                vault,
            )
        }
        paths.update(handle.base_contract.mutable_directories)
        paths.update(
            _post_migration_path(item, old_root, new_root)
            for item in tuple(paths)
        )
        if handle.checkpoint_rollback_basis is not None:
            paths.update(
                _record_to_path(
                    vault,
                    item,
                    "rollback basis created directory",
                )
                for item in handle.checkpoint_rollback_basis[
                    "created_directories"
                ]
            )
        allowed["created_directories"].update(identity(item) for item in paths)
        allowed["post_directories"].update(
            identity(candidate)
            for item in paths
            for candidate in (item, item.parent)
        )
    for key, items in changed.items():
        if not items.issubset(allowed[key]):
            raise ValueError(
                "rollback checkpoint delta is not bound to phase: "
                f"{record['phase']}: {key}={sorted(items - allowed[key])}"
            )


def _assert_rolled_back_pre_state(handle, application):
    if application["created_files"]:
        raise ValueError(
            "rolled-back terminal does not match Task 6 pre-state: "
            f"created_files={application['created_files']}, "
            f"created_directories={application['created_directories']}"
        )
    expected_files = {
        (item["kind"], item["path"]): item
        for item in handle.base_payload["input_bindings"]
    }
    current_files = _application_items_by_path(application["post_bindings"])
    if set(current_files) != set(expected_files):
        raise ValueError("rolled-back terminal file paths do not match Task 6 pre-state")
    file_fields = (
        "kind",
        "path",
        "sha256",
        "link_count",
        "mode",
        "size",
        "mtime_ns",
        "expected_type",
    )
    plan = SimpleNamespace(vault=handle.base_vault)
    for item_key, expected in expected_files.items():
        current = current_files[item_key]
        if any(current.get(field) != expected.get(field) for field in file_fields):
            raise ValueError("rolled-back terminal file state is not Task 6 pre-state")
        path = _record_to_path(handle.base_vault, current, "rolled-back file")
        actual = _serialize_post_binding(plan, _capture_input_binding(path))
        if actual != current:
            raise ValueError("rolled-back terminal file does not match current state")

    expected_directories = {
        (item["kind"], item["path"]): item
        for item in handle.base_payload["directory_bindings"]
    }
    current_directories = _application_items_by_path(application["post_directories"])
    immutable_paths = {
        _record_to_path(handle.base_vault, item, "Task 6 pre-state path")
        for item in (
            *handle.base_payload["input_bindings"],
            *handle.base_payload["directory_bindings"],
        )
    }
    immutable_paths.add(handle.path)
    allowed_ancestor_paths = {
        parent
        for path in immutable_paths
        for parent in path.parents
        if parent == handle.base_vault or parent.is_relative_to(handle.base_vault)
    }
    created_directory_paths = {
        _record_to_path(handle.base_vault, item, "rolled-back created directory")
        for item in application["created_directories"]
    }
    current_directory_paths = {
        _record_to_path(handle.base_vault, item, "rolled-back directory")
        for item in application["post_directories"]
    }
    expected_directory_paths = {
        _record_to_path(handle.base_vault, item, "Task 6 directory")
        for item in handle.base_payload["directory_bindings"]
    }
    extra_directories = current_directory_paths - expected_directory_paths
    if (
        not expected_directory_paths.issubset(current_directory_paths)
        or extra_directories != created_directory_paths
        or not extra_directories.issubset(allowed_ancestor_paths)
    ):
        raise ValueError(
            "rolled-back terminal directory paths do not match Task 6 pre-state: "
            f"missing={sorted(expected_directory_paths - current_directory_paths)}, "
            f"extra={sorted(extra_directories)}, "
            f"created={sorted(created_directory_paths)}"
        )
    directory_fields = (
        "kind",
        "path",
        "mode",
        "atime_ns",
        "mtime_ns",
    )
    for item_key, expected in expected_directories.items():
        current = current_directories[item_key]
        if any(
            current.get(field) != expected.get(field)
            for field in directory_fields
        ):
            differences = {
                field: (expected.get(field), current.get(field))
                for field in directory_fields
                if current.get(field) != expected.get(field)
            }
            raise ValueError(
                "rolled-back terminal directory state is not Task 6 pre-state: "
                f"{item_key}: {differences}"
            )
        path = _record_to_path(handle.base_vault, current, "rolled-back directory")
        actual = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISDIR(actual.st_mode)
            or list(_inode_from_stat(actual)) != current["inode"]
            or actual.st_mode != current["mode"]
            or actual.st_nlink != current["link_count"]
            or actual.st_atime_ns != current["atime_ns"]
            or actual.st_mtime_ns != current["mtime_ns"]
            or actual.st_ctime_ns != current["ctime_ns"]
            or actual.st_size != current["size"]
        ):
            raise ValueError("rolled-back terminal directory does not match current state")
    for path in extra_directories:
        identity = _manifest_path_record(plan, path)
        current = current_directories[(identity["kind"], identity["path"])]
        actual = os.stat(path, follow_symlinks=False)
        if (
            not stat.S_ISDIR(actual.st_mode)
            or list(_inode_from_stat(actual)) != current["inode"]
            or actual.st_mode != current["mode"]
            or actual.st_nlink != current["link_count"]
            or actual.st_atime_ns != current["atime_ns"]
            or actual.st_mtime_ns != current["mtime_ns"]
            or actual.st_ctime_ns != current["ctime_ns"]
            or actual.st_size != current["size"]
        ):
            raise ValueError(
                "rolled-back terminal ancestor does not match current state"
            )


def _validate_rolled_back_checkpoint_program(
    handle,
    records,
    record,
    application,
):
    combined = (*records, (record, None, None, None))
    rollback_start = next(
        (
            index
            for index, item in enumerate(combined)
            if item[0]["status"] == "rolling_back"
        ),
        None,
    )
    if rollback_start is None or rollback_start == 0:
        raise ValueError("brand migration rollback progression has no basis")
    basis = handle.checkpoint_rollback_basis
    if basis is None:
        raise ValueError("brand migration rollback progression has no basis state")
    vault = handle.base_vault
    contract = handle.base_contract
    bindings = handle.bindings
    old_root = vault / "01-Projects" / handle.payload["old_slug"]
    new_root = vault / "01-Projects" / handle.payload["new_slug"]
    required = {"rollback-start", "rollback-restore-directories"}
    removal_files = {
        _record_to_path(vault, item, "checkpoint created file")
        for item in basis["created_files"]
    }
    for binding in bindings:
        if binding.path == old_root or binding.path.is_relative_to(old_root):
            removal_files.add(_post_migration_path(binding.path, old_root, new_root))
        required.add(f"rollback-restore-file:{binding.path}")
    required.update(f"rollback-remove-file:{path}" for path in removal_files)
    removal_directories = {
        _record_to_path(vault, item, "checkpoint created directory")
        for item in basis["created_directories"]
    }
    removal_directories.update(
        _post_migration_path(path, old_root, new_root)
        for path in contract.mutable_directories
        if path == old_root or path.is_relative_to(old_root)
    )
    required.update(
        f"rollback-remove-directory:{path}" for path in removal_directories
    )
    required.update(
        f"rollback-directory-metadata:{binding.path}"
        for binding in _manifest_directory_bindings_from_payload(
            handle.payload,
            vault,
        )
    )
    completed = {item[0]["phase"] for item in combined[rollback_start:]}
    if not required.issubset(completed):
        raise ValueError("brand migration rollback progression is incomplete")
    _assert_rolled_back_pre_state(handle, application)


def _validate_checkpoint_transition(
    handle,
    records,
    record,
    *,
    seen_phases=None,
    application=None,
):
    kind = _checkpoint_phase_kind(record["phase"])
    status = record["status"]
    boundary = record["boundary"]
    if not records:
        if not (
            status == "applying"
            and boundary == "before"
            and kind in {"source", "manual-recovery"}
        ):
            raise ValueError("brand migration checkpoint initial transition is invalid")
        return

    previous = records[-1][0]
    previous_kind = _checkpoint_phase_kind(previous["phase"])
    previous_status = previous["status"]
    previous_boundary = previous["boundary"]
    seen_phases = (
        {item[0]["phase"] for item in records}
        if seen_phases is None
        else seen_phases
    )
    if previous_status == "rolled_back":
        raise ValueError("brand migration checkpoint extends terminal state")
    if (
        status == "rolling_back"
        and kind == "rollback-start"
        and boundary == "before"
    ):
        reconciliation_intent = record.get("mutation_intent")
        if reconciliation_intent is not None and (
            previous_status != "applying"
            or previous["phase"].startswith("cleanup:")
            or previous.get("mutation_intent") != reconciliation_intent
            or not record.get("delta")
        ):
            raise ValueError("rollback reconciliation intent is invalid")
    if previous_status == "applied":
        if not (
            status == "rolling_back"
            and kind == "rollback-start"
            and boundary == "before"
        ):
            raise ValueError("brand migration checkpoint applied transition is invalid")
        return
    if previous_status == "applying":
        if (
            status == "rolling_back"
            and kind == "rollback-start"
            and boundary == "before"
        ):
            return
        if previous_kind == "manual-recovery":
            if not (
                status == "rolling_back"
                and kind == "rollback-start"
                and boundary == "before"
            ):
                raise ValueError("brand migration recovery transition is invalid")
            return
        previous_intent = previous.get("mutation_intent")
        current_intent = record.get("mutation_intent")
        cleanup_transition = (
            status == "applying"
            and boundary == "after"
            and previous_boundary == "after"
            and not previous["phase"].startswith("cleanup:")
            and record["phase"] == f"cleanup:{previous['phase']}"
            and current_intent is not None
            and current_intent == previous_intent
        )
        if cleanup_transition:
            return
        if (
            previous_boundary == "after"
            and previous_intent is not None
            and not previous["phase"].startswith("cleanup:")
            and status == "applying"
            and _mutation_intent_requires_cleanup(
                _parse_mutation_intent(
                    _intent_plan(handle),
                    previous_intent,
                )
            )
        ):
            raise ValueError("required cleanup checkpoint is missing")
        if previous_boundary == "before" and previous_intent is not None:
            if not (
                status == "applying"
                and boundary == "after"
                and record["phase"] == previous["phase"]
                and current_intent == previous_intent
            ):
                raise ValueError("migration mutation checkpoint pair is invalid")
        if current_intent is not None and boundary == "after" and (
            previous_boundary != "before"
            or previous_intent != current_intent
            or record["phase"] != previous["phase"]
        ):
            raise ValueError("migration mutation checkpoint pair is invalid")
        if previous_boundary == "before":
            if previous_kind == "validation":
                if not (
                    status == "applied"
                    and kind == "validation"
                    and boundary == "after"
                    and isinstance(record.get("validation"), dict)
                    and record["validation"].get("valid") is True
                ):
                    raise ValueError("brand migration applied transition is invalid")
                _validate_applied_checkpoint_program(handle, records, record)
            elif not (
                status == "applying"
                and record["phase"] == previous["phase"]
                and boundary == "after"
            ):
                raise ValueError("brand migration checkpoint boundary transition is invalid")
            return
        if (
            status == "applying"
            and boundary == "before"
            and record["phase"]
            in {
                previous["phase"],
                (
                    previous["phase"][len("cleanup:") :]
                    if previous["phase"].startswith("cleanup:")
                    else ""
                ),
            }
            and previous_kind
            in {"rebuild:memory-index", "rebuild:maps", "rebuild:compiler"}
        ):
            return
        if status != "applying" or boundary != "before":
            raise ValueError("brand migration applying transition is invalid")
        if record["phase"] in seen_phases:
            raise ValueError("brand migration checkpoint phase is duplicated")
        ranks = {
            "source": 0,
            "rewrite": 1,
            "config": 2,
            "rebuild:memory-index": 3,
            "rebuild:maps": 4,
            "rebuild:compiler": 5,
            "validation": 6,
        }
        if kind not in ranks or previous_kind not in ranks:
            raise ValueError("brand migration applying phase is invalid")
        if ranks[kind] < ranks[previous_kind]:
            raise ValueError("brand migration applying phase is reordered")
        if kind == "rebuild:maps" and previous_kind != "rebuild:memory-index":
            raise ValueError("brand migration rebuild phase was skipped")
        if kind == "rebuild:compiler" and previous_kind != "rebuild:maps":
            raise ValueError("brand migration rebuild phase was skipped")
        return

    if previous_status != "rolling_back":
        raise ValueError("brand migration checkpoint status transition is invalid")
    rollback_ranks = {
        "rollback-start": 0,
        "rollback-remove-file": 1,
        "rollback-remove-directory": 2,
        "rollback-restore-directories": 3,
        "rollback-restore-file": 4,
        "rollback-directory-metadata": 5,
        "rollback-complete": 6,
    }
    if kind not in rollback_ranks or previous_kind not in rollback_ranks:
        raise ValueError("brand migration rollback phase is invalid")
    if record["phase"] in seen_phases:
        raise ValueError("brand migration rollback phase is duplicated")
    if rollback_ranks[kind] < rollback_ranks[previous_kind]:
        raise ValueError("brand migration rollback phase is reordered")
    if kind == "rollback-complete":
        if status != "rolled_back" or boundary != "after":
            raise ValueError("brand migration rolled-back transition is invalid")
        if application is None:
            raise ValueError("rolled-back terminal application is missing")
        _validate_rolled_back_checkpoint_program(
            handle,
            records,
            record,
            application,
        )
    elif status != "rolling_back" or boundary != "after":
        raise ValueError("brand migration rollback boundary is invalid")


def _checkpoint_record_binding(item):
    return {
        "directory_stat": list(item[3]),
        "record_stat": list(item[4]),
        "digest": item[1],
    }


def _close_checkpoint_head(handle):
    if handle.checkpoint_head_record_fd is not None:
        os.close(handle.checkpoint_head_record_fd)
        handle.checkpoint_head_record_fd = None
    if handle.checkpoint_head_directory_fd is not None:
        os.close(handle.checkpoint_head_directory_fd)
        handle.checkpoint_head_directory_fd = None
    handle.checkpoint_head_authority = None


def _invalidate_checkpoint_caches(handle):
    _close_checkpoint_head(handle)
    handle.checkpoint_chain = None
    handle.checkpoint_application_index = None
    handle.checkpoint_seen_phases = set()
    handle.checkpoint_rollback_basis = None
    handle.checkpoint_journal_authority = None
    handle.checkpoint_journal_inventory = None
    handle.required_apply_phases = None
    handle.validated_snapshot_digest = None


def _checkpoint_journal_stat_identity(handle, *, allow_unsealed=False):
    journal = os.fstat(handle.journal_fd)
    named = os.stat(
        MIGRATION_JOURNAL_DIRECTORY,
        dir_fd=handle.root_fd,
        follow_symlinks=False,
    )
    allowed_modes = {0o500, 0o700} if allow_unsealed else {0o500}
    journal_identity = _input_stat_identity(journal)
    if (
        not stat.S_ISDIR(journal.st_mode)
        or _inode_from_stat(journal) != handle.journal_inode
        or _inode_from_stat(named) != handle.journal_inode
        or _input_stat_identity(named) != journal_identity
        or stat.S_IMODE(journal.st_mode) not in allowed_modes
    ):
        raise RuntimeError("migration checkpoint journal authority changed")
    return journal_identity


def _assert_checkpoint_journal_authority(
    handle,
    expected,
    inventory,
    *,
    allow_unsealed=False,
):
    current = _checkpoint_journal_stat_identity(
        handle,
        allow_unsealed=allow_unsealed,
    )
    if current != expected:
        raise RuntimeError("migration checkpoint journal namespace changed")
    if os.fstat(handle.journal_fd).st_nlink != len(inventory) + 2:
        raise RuntimeError("migration checkpoint journal inventory changed")
    return current


def _open_checkpoint_head(
    handle,
    item,
    journal_authority,
    journal_inventory,
):
    if journal_authority is None:
        raise RuntimeError("migration checkpoint journal authority is not pinned")
    _assert_checkpoint_journal_authority(
        handle,
        journal_authority,
        journal_inventory,
        allow_unsealed=stat.S_IMODE(os.fstat(handle.journal_fd).st_mode) == 0o700,
    )
    directory_fd = os.open(
        item[2],
        _directory_flags(),
        dir_fd=handle.journal_fd,
    )
    record_fd = None
    try:
        directory_stat = os.fstat(directory_fd)
        named_directory = os.stat(
            item[2],
            dir_fd=handle.journal_fd,
            follow_symlinks=False,
        )
        if (
            _input_stat_identity(directory_stat) != item[3]
            or _input_stat_identity(named_directory) != item[3]
            or os.listdir(directory_fd) != ["record.json"]
        ):
            raise RuntimeError("migration checkpoint head directory changed")
        record_fd = os.open(
            "record.json",
            os.O_RDONLY | os.O_NOFOLLOW,
            dir_fd=directory_fd,
        )
        record_stat = os.fstat(record_fd)
        named_record = os.stat(
            "record.json",
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        digest = _hash_fd(record_fd)
        final_record_stat = os.fstat(record_fd)
        final_named_record = os.stat(
            "record.json",
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        final_directory_stat = os.fstat(directory_fd)
        final_named_directory = os.stat(
            item[2],
            dir_fd=handle.journal_fd,
            follow_symlinks=False,
        )
        if (
            _input_stat_identity(record_stat) != item[4]
            or _input_stat_identity(named_record) != item[4]
            or _input_stat_identity(final_record_stat) != item[4]
            or _input_stat_identity(final_named_record) != item[4]
            or _input_stat_identity(final_directory_stat) != item[3]
            or _input_stat_identity(final_named_directory) != item[3]
            or digest != item[1]
        ):
            raise RuntimeError("migration checkpoint head record changed")
    except Exception:
        if record_fd is not None:
            os.close(record_fd)
        os.close(directory_fd)
        raise
    return directory_fd, record_fd


def _pin_checkpoint_head(
    handle,
    item=None,
    journal_authority=None,
    journal_inventory=None,
):
    item = (
        handle.checkpoint_chain[-1]
        if item is None and handle.checkpoint_chain
        else item
    )
    if item is None:
        _close_checkpoint_head(handle)
        return
    journal_authority = (
        handle.checkpoint_journal_authority
        if journal_authority is None
        else journal_authority
    )
    journal_inventory = (
        handle.checkpoint_journal_inventory or frozenset()
        if journal_inventory is None
        else journal_inventory
    )
    directory_fd, record_fd = _open_checkpoint_head(
        handle,
        item,
        journal_authority,
        journal_inventory,
    )
    _close_checkpoint_head(handle)
    handle.checkpoint_head_directory_fd = directory_fd
    handle.checkpoint_head_record_fd = record_fd
    handle.checkpoint_head_authority = item


def _revalidate_checkpoint_head(
    handle,
    ownership_check=None,
    *,
    allow_unsealed=False,
    check_journal=True,
):
    check_owned = ownership_check or (lambda: None)
    check_owned()
    journal_identity = _checkpoint_journal_stat_identity(
        handle,
        allow_unsealed=allow_unsealed,
    )
    if check_journal and handle.checkpoint_journal_authority is not None:
        if journal_identity != handle.checkpoint_journal_authority:
            raise RuntimeError("migration checkpoint journal authority changed")
        if os.fstat(handle.journal_fd).st_nlink != len(
            handle.checkpoint_journal_inventory or ()
        ) + 2:
            raise RuntimeError("migration checkpoint journal inventory changed")
    if not handle.checkpoint_chain:
        check_owned()
        return
    item = handle.checkpoint_chain[-1]
    if handle.checkpoint_head_authority != item:
        raise RuntimeError("migration checkpoint cached head changed")
    directory_stat = os.fstat(handle.checkpoint_head_directory_fd)
    named_directory = os.stat(
        item[2],
        dir_fd=handle.journal_fd,
        follow_symlinks=False,
    )
    record_stat = os.fstat(handle.checkpoint_head_record_fd)
    named_record = os.stat(
        "record.json",
        dir_fd=handle.checkpoint_head_directory_fd,
        follow_symlinks=False,
    )
    if (
        _input_stat_identity(directory_stat) != item[3]
        or _input_stat_identity(named_directory) != item[3]
        or os.listdir(handle.checkpoint_head_directory_fd) != ["record.json"]
        or _input_stat_identity(record_stat) != item[4]
        or _input_stat_identity(named_record) != item[4]
        or _hash_fd(handle.checkpoint_head_record_fd) != item[1]
    ):
        raise RuntimeError("migration checkpoint durable head record changed")
    check_owned()


def _load_checkpoint_chain(handle, ownership_check=None):
    if handle.checkpoint_chain is not None:
        _revalidate_checkpoint_head(handle, ownership_check)
        return handle.checkpoint_chain
    previous = (
        handle.checkpoint_application_index,
        handle.checkpoint_seen_phases,
        handle.checkpoint_rollback_basis,
        handle.checkpoint_journal_authority,
        handle.checkpoint_journal_inventory,
        handle.required_apply_phases,
        handle.validated_snapshot_digest,
    )
    try:
        return _load_checkpoint_chain_uncached(handle)
    except Exception:
        _close_checkpoint_head(handle)
        handle.checkpoint_chain = None
        (
            handle.checkpoint_application_index,
            handle.checkpoint_seen_phases,
            handle.checkpoint_rollback_basis,
            handle.checkpoint_journal_authority,
            handle.checkpoint_journal_inventory,
            handle.required_apply_phases,
            handle.validated_snapshot_digest,
        ) = previous
        raise


def _load_checkpoint_chain_uncached(handle):
    _inspect_sealed_backup(handle)
    prefix = _checkpoint_prefix(handle)
    temp_prefix = _checkpoint_temp_prefix(handle)
    reclaim_prefix = _checkpoint_reclaim_prefix()
    journal_before = _checkpoint_journal_stat_identity(handle)
    names = os.listdir(handle.journal_fd)
    inventory = frozenset(names)
    if len(inventory) != len(names):
        raise RuntimeError("migration checkpoint journal inventory is invalid")
    candidates_by_sequence = {}
    for name in names:
        if name.startswith(temp_prefix) or name.startswith(reclaim_prefix):
            continue
        if not name.startswith(prefix):
            raise ValueError("brand migration journal entry is invalid")
        match = re.fullmatch(
            re.escape(prefix) + r"(\d{8})-([0-9a-f]{64})",
            name,
        )
        if match is None:
            raise ValueError("brand migration checkpoint name is invalid")
        sequence = int(match.group(1))
        if sequence in candidates_by_sequence:
            raise ValueError("brand migration checkpoint sequence is duplicated")
        candidates_by_sequence[sequence] = (sequence, match.group(2), name)
    candidates = []
    for sequence in range(1, len(candidates_by_sequence) + 1):
        candidate = candidates_by_sequence.get(sequence)
        if candidate is None:
            raise ValueError("brand migration checkpoint sequence is invalid")
        candidates.append(candidate)
    records = []
    application_index = None
    previous_digest = None
    seen_phases = set()
    for sequence, expected_digest, name in candidates:
        directory_fd = os.open(name, _directory_flags(), dir_fd=handle.journal_fd)
        try:
            directory_stat = os.fstat(directory_fd)
            named = os.stat(name, dir_fd=handle.journal_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(named.st_mode)
                or _input_stat_identity(named)
                != _input_stat_identity(directory_stat)
                or stat.S_IMODE(directory_stat.st_mode) != 0o500
                or os.listdir(directory_fd) != ["record.json"]
            ):
                raise RuntimeError("sealed migration checkpoint directory changed")
            record_fd = os.open(
                "record.json",
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            try:
                before = os.fstat(record_fd)
                raw = _read_fd_bytes(record_fd)
                after = os.fstat(record_fd)
                named_record_after = os.stat(
                    "record.json",
                    dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                directory_after = os.fstat(directory_fd)
                named_directory_after = os.stat(
                    name,
                    dir_fd=handle.journal_fd,
                    follow_symlinks=False,
                )
                entries_after = os.listdir(directory_fd)
            finally:
                os.close(record_fd)
            digest = hashlib.sha256(raw).hexdigest()
            if (
                _input_stat_identity(before) != _input_stat_identity(after)
                or _input_stat_identity(named_record_after)
                != _input_stat_identity(after)
                or _input_stat_identity(directory_after)
                != _input_stat_identity(directory_stat)
                or _input_stat_identity(named_directory_after)
                != _input_stat_identity(directory_stat)
                or entries_after != ["record.json"]
                or not stat.S_ISREG(after.st_mode)
                or after.st_nlink != 1
                or stat.S_IMODE(after.st_mode) != 0o400
                or digest != expected_digest
            ):
                raise RuntimeError("sealed migration checkpoint record changed")
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid migration checkpoint JSON") from exc
            if raw != _serialize_manifest_bytes(record):
                raise ValueError("migration checkpoint bytes are not deterministic")
        finally:
            os.close(directory_fd)
        directory_identity = _input_stat_identity(directory_after)
        record_identity = _input_stat_identity(named_record_after)
        previous_binding = (
            _checkpoint_record_binding(records[-1])
            if records
            else None
        )
        if (
            record.get("schema_version") != CHECKPOINT_SCHEMA_VERSION
            or record.get("generated_by") != CODE_PREFIX
            or record.get("migration_id") != handle.payload.get("migration_id")
            or record.get("sequence") != sequence
            or record.get("previous_record_sha256") != previous_digest
            or record.get("previous_record_binding") != previous_binding
            or record.get("base_manifest_inode")
            != list(handle.manifest_inode)
            or record.get("base_manifest_sha256") != handle.manifest_sha256
            or record.get("status")
            not in {"applying", "applied", "rolling_back", "rolled_back"}
            or not isinstance(record.get("phase"), str)
            or record.get("boundary") not in {"before", "after"}
            or not isinstance(record.get("recorded_at"), str)
        ):
            raise ValueError("brand migration checkpoint record is invalid")
        _validate_checkpoint_mutation_record(handle, record)
        if sequence == 1:
            if "delta" in record or not isinstance(record.get("snapshot"), dict):
                raise ValueError("brand migration initial checkpoint snapshot is invalid")
            application_index = _application_index(record["snapshot"])
        else:
            if "snapshot" in record:
                raise ValueError("brand migration checkpoint repeats a full snapshot")
            _validate_rollback_phase_delta(handle, record)
            _apply_application_delta_index(
                application_index,
                record.get("delta"),
            )
            if (
                handle.checkpoint_rollback_basis is None
                and record["status"] == "rolling_back"
            ):
                handle.checkpoint_rollback_basis = _materialize_application(
                    application_index
                )
        effective = dict(record)
        if sequence == 1 and handle.validated_snapshot_digest != digest:
            effective["application"] = _materialize_application(
                application_index
            )
            _validate_checkpoint_application(handle, effective)
            effective.pop("application")
            handle.validated_snapshot_digest = digest
        terminal_application = (
            _materialize_application(application_index)
            if record["status"] == "rolled_back"
            else None
        )
        _validate_checkpoint_transition(
            handle,
            records,
            effective,
            seen_phases=seen_phases,
            application=terminal_application,
        )
        records.append(
            (
                effective,
                digest,
                name,
                directory_identity,
                record_identity,
            )
        )
        seen_phases.add(record["phase"])
        previous_digest = digest
    if records:
        application = _materialize_application(application_index)
        records[-1][0]["application"] = application
        _validate_checkpoint_application(handle, records[-1][0])
    names_after = os.listdir(handle.journal_fd)
    journal_after = _checkpoint_journal_stat_identity(handle)
    if (
        journal_after != journal_before
        or len(names_after) != len(inventory)
        or frozenset(names_after) != inventory
        or os.fstat(handle.journal_fd).st_nlink != len(inventory) + 2
    ):
        raise RuntimeError("migration checkpoint journal namespace changed during load")
    handle.checkpoint_chain = records
    handle.checkpoint_application_index = application_index
    handle.checkpoint_seen_phases = seen_phases
    handle.checkpoint_journal_authority = journal_before
    handle.checkpoint_journal_inventory = inventory
    try:
        _pin_checkpoint_head(handle)
    except Exception:
        handle.checkpoint_chain = None
        handle.checkpoint_application_index = None
        handle.checkpoint_seen_phases = set()
        handle.checkpoint_journal_authority = None
        handle.checkpoint_journal_inventory = None
        raise
    return handle.checkpoint_chain


def _checkpoint_reclaim_prefix():
    return ".checkpoint-reclaim-"


def _checkpoint_temp_candidates(handle):
    temp_prefix = _checkpoint_temp_prefix(handle)
    reclaim_prefix = _checkpoint_reclaim_prefix()
    return [
        name
        for name in os.listdir(handle.journal_fd)
        if name.startswith(temp_prefix) or name.startswith(reclaim_prefix)
    ]


def _inspect_checkpoint_temp(handle, name):
    named = os.stat(name, dir_fd=handle.journal_fd, follow_symlinks=False)
    if not stat.S_ISDIR(named.st_mode):
        raise RuntimeError("migration checkpoint temp has unexpected type")
    fd = os.open(name, _directory_flags(), dir_fd=handle.journal_fd)
    try:
        current = os.fstat(fd)
        if _inode_from_stat(named) != _inode_from_stat(current):
            raise RuntimeError("migration checkpoint temp inode changed")
        entries = os.listdir(fd)
        if entries not in ([], ["record.json"]):
            raise RuntimeError("migration checkpoint temp has unexpected contents")
        record_inode = None
        if entries:
            record = os.stat(
                "record.json",
                dir_fd=fd,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(record.st_mode)
                or record.st_nlink != 1
                or stat.S_IMODE(record.st_mode) not in {0o400, 0o600}
            ):
                raise RuntimeError("migration checkpoint temp record is unsafe")
            record_inode = _inode_from_stat(record)
        return _inode_from_stat(current), record_inode
    finally:
        os.close(fd)


def _reclaim_incomplete_checkpoint_temps(handle, ownership_check):
    candidates = _checkpoint_temp_candidates(handle)
    inspected = {
        name: _inspect_checkpoint_temp(handle, name)
        for name in candidates
    }
    for name in candidates:
        ownership_check()
        inode, record_inode = inspected[name]
        reclaim_name = name
        if not name.startswith(_checkpoint_reclaim_prefix()):
            reclaim_name = _checkpoint_reclaim_prefix() + secrets.token_hex(12)
            _rename_exclusive(
                handle.journal_fd,
                name,
                handle.journal_fd,
                reclaim_name,
            )
        fd = os.open(reclaim_name, _directory_flags(), dir_fd=handle.journal_fd)
        try:
            current = os.fstat(fd)
            named = os.stat(
                reclaim_name,
                dir_fd=handle.journal_fd,
                follow_symlinks=False,
            )
            if (
                _inode_from_stat(current) != inode
                or _inode_from_stat(named) != inode
            ):
                raise RuntimeError("migration checkpoint temp changed after quarantine")
            entries = os.listdir(fd)
            if record_inode is None:
                if entries:
                    raise RuntimeError(
                        "migration checkpoint temp changed after quarantine"
                    )
            else:
                if entries != ["record.json"]:
                    raise RuntimeError(
                        "migration checkpoint temp record changed after quarantine"
                    )
                ownership_check()
                record = os.stat(
                    "record.json",
                    dir_fd=fd,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(record.st_mode)
                    or record.st_nlink != 1
                    or _inode_from_stat(record) != record_inode
                ):
                    raise RuntimeError(
                        "migration checkpoint temp record changed after quarantine"
                    )
                ownership_check()
                if stat.S_IMODE(current.st_mode) != 0o700:
                    os.fchmod(fd, 0o700)
                    ownership_check()
                os.unlink("record.json", dir_fd=fd)
            ownership_check()
        finally:
            os.close(fd)
        ownership_check()
        os.rmdir(reclaim_name, dir_fd=handle.journal_fd)
    return tuple(candidates)


def _recover_interrupted_checkpoint_append(handle, ownership_check):
    ownership_check()
    journal = os.fstat(handle.journal_fd)
    named = os.stat(
        MIGRATION_JOURNAL_DIRECTORY,
        dir_fd=handle.root_fd,
        follow_symlinks=False,
    )
    mode = stat.S_IMODE(journal.st_mode)
    if (
        not stat.S_ISDIR(journal.st_mode)
        or _inode_from_stat(journal) != handle.journal_inode
        or _inode_from_stat(named) != handle.journal_inode
        or mode not in {0o500, 0o700}
    ):
        raise RuntimeError("recoverable migration journal binding changed")
    candidates = _checkpoint_temp_candidates(handle)
    for name in candidates:
        _inspect_checkpoint_temp(handle, name)
    if mode == 0o500 and not candidates:
        return
    if mode == 0o500:
        ownership_check()
        os.fchmod(handle.journal_fd, 0o700)
        ownership_check()
        os.fsync(handle.journal_fd)
    _reclaim_incomplete_checkpoint_temps(handle, ownership_check)
    ownership_check()
    os.fchmod(handle.journal_fd, 0o500)
    ownership_check()
    os.fsync(handle.journal_fd)
    ownership_check()
    handle.checkpoint_journal_authority = None
    handle.checkpoint_journal_inventory = None


def _publish_checkpoint(
    handle,
    plan,
    phase,
    boundary,
    status,
    ownership_check,
    validation=None,
    mutation_intent=None,
    delta_override=_NO_DELTA_OVERRIDE,
):
    ownership_lost = False

    def require_owned():
        nonlocal ownership_lost
        try:
            ownership_check()
        except Exception:
            ownership_lost = True
            raise

    def write_owned(fd, data):
        view = memoryview(data)
        while view:
            require_owned()
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("checkpoint write made no progress")
            view = view[written:]

    try:
        require_owned()
        chain = _load_checkpoint_chain(handle, require_owned)
        had_chain = bool(chain)
        if had_chain:
            if delta_override is _NO_DELTA_OVERRIDE:
                delta, captured_application = _targeted_checkpoint_delta(
                    plan,
                    handle,
                    str(phase),
                    boundary,
                )
            else:
                delta = delta_override
                captured_application = None
            snapshot = None
            _application_delta_operations(
                handle.checkpoint_application_index,
                delta,
            )
        else:
            snapshot = _serialize_application(plan, _capture_post_state(plan))
            delta = None
            captured_application = snapshot
        require_owned()
        sequence = len(chain) + 1
        previous_digest = chain[-1][1] if chain else None
        record = {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "generated_by": CODE_PREFIX,
            "migration_id": handle.payload["migration_id"],
            "base_manifest_inode": list(handle.manifest_inode),
            "base_manifest_sha256": handle.manifest_sha256,
            "sequence": sequence,
            "previous_record_sha256": previous_digest,
            "previous_record_binding": (
                _checkpoint_record_binding(chain[-1]) if chain else None
            ),
            "status": status,
            "phase": str(phase),
            "boundary": boundary,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        if had_chain:
            record["delta"] = delta
        else:
            record["snapshot"] = snapshot
        if validation is not None:
            record["validation"] = validation
        if mutation_intent is not None:
            record["mutation_intent"] = mutation_intent
        proposed = dict(record)
        _validate_checkpoint_mutation_record(handle, proposed)
        _validate_rollback_phase_delta(handle, proposed)
        terminal_application = None
        if status == "rolled_back":
            terminal_application = _materialize_application(
                handle.checkpoint_application_index
            )
        elif status == "applied":
            terminal_application = captured_application
            proposed["application"] = captured_application
            _validate_checkpoint_application(handle, proposed)
            proposed.pop("application")
        elif not had_chain:
            proposed["application"] = snapshot
            _validate_checkpoint_application(handle, proposed)
            proposed.pop("application")
        _validate_checkpoint_transition(
            handle,
            chain,
            proposed,
            seen_phases=handle.checkpoint_seen_phases,
            application=terminal_application,
        )
        content = _serialize_manifest_bytes(record)
        digest = hashlib.sha256(content).hexdigest()
        final_name = f"{_checkpoint_prefix(handle)}{sequence:08d}-{digest}"
        temp_name = _checkpoint_temp_prefix(handle) + secrets.token_hex(12)
        require_owned()
        temp_fd = None
        published = False
        journal_unsealed = False
        expected_inventory = set(handle.checkpoint_journal_inventory or ())
        working_authority = handle.checkpoint_journal_authority
        try:
            _revalidate_checkpoint_head(handle, require_owned)
            _assert_checkpoint_journal_authority(
                handle,
                working_authority,
                expected_inventory,
            )
            require_owned()
            os.fchmod(handle.journal_fd, 0o700)
            journal_unsealed = True
            require_owned()
            os.fsync(handle.journal_fd)
            working_authority = _checkpoint_journal_stat_identity(
                handle,
                allow_unsealed=True,
            )
            _assert_checkpoint_journal_authority(
                handle,
                working_authority,
                expected_inventory,
                allow_unsealed=True,
            )
            require_owned()
            reclaimed = _reclaim_incomplete_checkpoint_temps(handle, require_owned)
            expected_inventory.difference_update(reclaimed)
            working_authority = _checkpoint_journal_stat_identity(
                handle,
                allow_unsealed=True,
            )
            _assert_checkpoint_journal_authority(
                handle,
                working_authority,
                expected_inventory,
                allow_unsealed=True,
            )
            require_owned()
            os.mkdir(temp_name, mode=0o700, dir_fd=handle.journal_fd)
            expected_inventory.add(temp_name)
            working_authority = _checkpoint_journal_stat_identity(
                handle,
                allow_unsealed=True,
            )
            _assert_checkpoint_journal_authority(
                handle,
                working_authority,
                expected_inventory,
                allow_unsealed=True,
            )
            temp_fd = os.open(
                temp_name,
                _directory_flags(),
                dir_fd=handle.journal_fd,
            )
            require_owned()
            record_fd = os.open(
                "record.json",
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=temp_fd,
            )
            try:
                write_owned(record_fd, content)
                require_owned()
                os.fsync(record_fd)
                if _hash_fd(record_fd) != digest:
                    raise RuntimeError("migration checkpoint digest changed")
                require_owned()
                os.fchmod(record_fd, 0o400)
                require_owned()
                os.fsync(record_fd)
            finally:
                os.close(record_fd)
            require_owned()
            os.fchmod(temp_fd, 0o500)
            require_owned()
            os.fsync(temp_fd)
            require_owned()
            _revalidate_checkpoint_head(
                handle,
                require_owned,
                allow_unsealed=True,
                check_journal=False,
            )
            _assert_checkpoint_journal_authority(
                handle,
                working_authority,
                expected_inventory,
                allow_unsealed=True,
            )
            require_owned()
            _rename_exclusive(
                handle.journal_fd,
                temp_name,
                handle.journal_fd,
                final_name,
            )
            published = True
            expected_inventory.remove(temp_name)
            expected_inventory.add(final_name)
            working_authority = _checkpoint_journal_stat_identity(
                handle,
                allow_unsealed=True,
            )
            _assert_checkpoint_journal_authority(
                handle,
                working_authority,
                expected_inventory,
                allow_unsealed=True,
            )
            require_owned()
            _revalidate_checkpoint_head(
                handle,
                require_owned,
                allow_unsealed=True,
                check_journal=False,
            )
            require_owned()
            os.fsync(handle.journal_fd)
            _assert_checkpoint_journal_authority(
                handle,
                working_authority,
                expected_inventory,
                allow_unsealed=True,
            )
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            if not ownership_lost and journal_unsealed:
                require_owned()
                os.fchmod(handle.journal_fd, 0o500)
                require_owned()
                os.fsync(handle.journal_fd)
        require_owned()
        sealed_authority = _checkpoint_journal_stat_identity(handle)
        _assert_checkpoint_journal_authority(
            handle,
            sealed_authority,
            expected_inventory,
        )
        published_stat = os.stat(
            final_name,
            dir_fd=handle.journal_fd,
            follow_symlinks=False,
        )
        published_fd = os.open(
            final_name,
            _directory_flags(),
            dir_fd=handle.journal_fd,
        )
        try:
            published_record = os.stat(
                "record.json",
                dir_fd=published_fd,
                follow_symlinks=False,
            )
        finally:
            os.close(published_fd)
        effective = dict(record)
        latest = (
            effective,
            digest,
            final_name,
            _input_stat_identity(published_stat),
            _input_stat_identity(published_record),
        )
        durable_inventory = frozenset(expected_inventory)
        try:
            _pin_checkpoint_head(
                handle,
                latest,
                sealed_authority,
                durable_inventory,
            )
            handle.checkpoint_chain.append(latest)
            handle.checkpoint_journal_authority = sealed_authority
            handle.checkpoint_journal_inventory = durable_inventory
            if had_chain:
                _apply_application_delta_index(
                    handle.checkpoint_application_index,
                    delta,
                )
            else:
                handle.checkpoint_application_index = _application_index(snapshot)
            handle.checkpoint_seen_phases.add(record["phase"])
            if status == "rolling_back" and phase == "rollback-start":
                handle.checkpoint_rollback_basis = _materialize_application(
                    handle.checkpoint_application_index
                )
        except Exception:
            _invalidate_checkpoint_caches(handle)
            raise
        require_owned()
        return effective
    except _CheckpointFailure:
        raise
    except Exception as exc:
        raise _CheckpointFailure(
            f"migration checkpoint publication failed: {phase}/{boundary}: {exc}"
        ) from exc


def _finalize_applied_manifest(
    handle,
    plan,
    validation,
    post_state,
    ownership_check,
):
    _publish_checkpoint(
        handle,
        plan,
        "validation-finalization",
        "after",
        "applied",
        ownership_check,
        validation=validation,
    )
    return {
        **validation,
        "status": "applied",
        "manifest_path": str(handle.path / "manifest.json"),
    }


def _mark_manifest_applying(
    handle,
    plan,
    ownership_check,
    mutation_intent,
):
    return _publish_checkpoint(
        handle,
        plan,
        "source-rename",
        "before",
        "applying",
        ownership_check,
        mutation_intent=mutation_intent,
    )


def _recovery_plan_from_manifest(handle):
    vault, contract, bindings = _validate_manifest_payload(handle.payload)
    old_root = vault / "01-Projects" / handle.payload["old_slug"]
    new_root = vault / "01-Projects" / handle.payload["new_slug"]
    return SimpleNamespace(
        vault=vault,
        mutation_contract=contract,
        source_project=old_root,
        destination_project=new_root,
        input_bindings=bindings,
        directory_bindings=_manifest_directory_bindings_from_payload(
            handle.payload,
            vault,
        ),
        markdown_paths=tuple(
            binding.path for binding in bindings if binding.path.suffix == ".md"
        ),
    )


def _named_file_matches(parent_fd, name, binding):
    current = _named_stat(parent_fd, name)
    if (
        current is None
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or _inode_from_stat(current) != binding.inode
        or current.st_size != binding.size
        or stat.S_IMODE(current.st_mode) != stat.S_IMODE(binding.mode)
    ):
        return False
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
    try:
        return _hash_fd(fd) == binding.sha256
    finally:
        os.close(fd)


def _named_file_inode_matches(parent_fd, name, binding):
    current = _named_stat(parent_fd, name)
    return (
        current is not None
        and stat.S_ISREG(current.st_mode)
        and current.st_nlink == 1
        and _inode_from_stat(current) == binding.inode
    )


def _named_directory_matches(parent_fd, name, inode):
    current = _named_stat(parent_fd, name)
    return (
        current is not None
        and stat.S_ISDIR(current.st_mode)
        and _inode_from_stat(current) == inode
    )


def _remove_empty_named_directory(parent_fd, name, inode):
    if not _named_directory_matches(parent_fd, name, inode):
        raise _ConcurrentMutationError(
            f"concurrent migration staging directory replacement: {name}"
        )
    fd = os.open(name, _directory_flags(), dir_fd=parent_fd)
    try:
        if os.listdir(fd):
            raise _ConcurrentMutationError(
                f"concurrent migration staging directory mutation: {name}"
            )
    finally:
        os.close(fd)
    os.rmdir(name, dir_fd=parent_fd)


def _restore_pending_project_rename(
    parent_fd,
    source_name,
    destination_name,
    expected_inode,
):
    destination_fd = os.open(
        destination_name,
        _directory_flags(),
        dir_fd=parent_fd,
    )
    try:
        if _inode_from_stat(os.fstat(destination_fd)) != expected_inode:
            raise _ConcurrentMutationError(
                "concurrent mutation at pending project rename"
            )
        try:
            _rename_exclusive(
                parent_fd,
                destination_name,
                parent_fd,
                source_name,
            )
        except Exception as exc:
            raise _ConcurrentMutationError(
                "concurrent mutation while restoring pending project rename"
            ) from exc

        restored = _named_stat(parent_fd, source_name)
        restored_inode = (
            _inode_from_stat(restored) if restored is not None else None
        )
        if (
            restored is not None
            and stat.S_ISDIR(restored.st_mode)
            and restored_inode == expected_inode
            and _inode_from_stat(os.fstat(destination_fd)) == expected_inode
            and _named_stat(parent_fd, destination_name) is None
        ):
            return

        if (
            restored is not None
            and _named_stat(parent_fd, destination_name) is None
        ):
            try:
                _rename_exclusive(
                    parent_fd,
                    source_name,
                    parent_fd,
                    destination_name,
                )
                recovered = _named_stat(parent_fd, destination_name)
                if (
                    recovered is None
                    or _inode_from_stat(recovered) != restored_inode
                    or _named_stat(parent_fd, source_name) is not None
                ):
                    raise RuntimeError(
                        "interposed project destination recovery changed identity"
                    )
            except Exception as recovery_error:
                raise _ConcurrentMutationError(
                    "concurrent mutation while restoring pending project rename; "
                    f"recovery failed: {recovery_error}"
                ) from recovery_error
        raise _ConcurrentMutationError(
            "concurrent mutation while restoring pending project rename"
        )
    finally:
        os.close(destination_fd)


def _resolve_checkpoint_mutation(
    handle,
    record,
    ownership_check=None,
    allow_binding_drift=False,
):
    raw_intent = record.get("mutation_intent")
    if raw_intent is None:
        return
    check_owned = ownership_check or (lambda: None)
    plan = _intent_plan(handle)
    intent = _parse_mutation_intent(plan, raw_intent)
    check_owned()
    parent_fd, target_name = _open_handle_parent(handle, intent.target)
    stage_name = intent.staging.name
    try:
        boundary = record["boundary"]
        if intent.operation == "rename-directory":
            target_before = _named_directory_matches(
                parent_fd,
                target_name,
                intent.before,
            )
            target_absent = _named_stat(parent_fd, target_name) is None
            stage_intended = _named_directory_matches(
                parent_fd,
                stage_name,
                intent.intended,
            )
            stage_absent = _named_stat(parent_fd, stage_name) is None
            if boundary == "before":
                if target_before and stage_absent:
                    pass
                elif target_absent and stage_intended:
                    _restore_pending_project_rename(
                        parent_fd,
                        target_name,
                        stage_name,
                        intent.before,
                    )
                else:
                    raise _ConcurrentMutationError(
                        "concurrent mutation at pending project rename"
                    )
            elif not (target_absent and stage_intended):
                raise _ConcurrentMutationError(
                    "concurrent mutation at checkpointed project rename"
                )
        elif intent.operation == "write-file":
            target_before = (
                _named_stat(parent_fd, target_name) is None
                if intent.before is None
                else _named_file_matches(parent_fd, target_name, intent.before)
            )
            target_intended = _named_file_matches(
                parent_fd,
                target_name,
                intent.intended,
            )
            stage_before = (
                False
                if intent.before is None
                else _named_file_matches(parent_fd, stage_name, intent.before)
            )
            stage_intended = _named_file_matches(
                parent_fd,
                stage_name,
                intent.intended,
            )
            stage_absent = _named_stat(parent_fd, stage_name) is None
            if boundary == "before":
                if target_before and stage_intended:
                    _quarantine_remove_name(
                        parent_fd,
                        stage_name,
                        intent.intended.inode,
                        "migration unapplied file stage cleanup",
                    )
                elif target_intended and (
                    (intent.before is None and stage_absent)
                    or (intent.before is not None and stage_before)
                ):
                    if intent.before is None:
                        _quarantine_remove_name(
                            parent_fd,
                            target_name,
                            intent.intended.inode,
                            "migration uncheckpointed file rollback",
                        )
                    else:
                        _rename_exchange(
                            parent_fd,
                            stage_name,
                            parent_fd,
                            target_name,
                        )
                        if not _named_file_matches(
                            parent_fd, target_name, intent.before
                        ):
                            raise RuntimeError(
                                "migration intent file exchange-back failed"
                            )
                        _quarantine_remove_name(
                            parent_fd,
                            stage_name,
                            intent.intended.inode,
                            "migration exchanged file stage cleanup",
                        )
                else:
                    raise _ConcurrentMutationError(
                        f"concurrent mutation at pending target: {intent.target}"
                    )
            else:
                target_binding_accepted = target_intended or (
                    allow_binding_drift
                    and _named_file_inode_matches(
                        parent_fd,
                        target_name,
                        intent.intended,
                    )
                )
                if not target_binding_accepted:
                    raise _ConcurrentMutationError(
                        f"concurrent mutation at checkpointed target: {intent.target}"
                    )
                if stage_before:
                    _quarantine_remove_name(
                        parent_fd,
                        stage_name,
                        intent.before.inode,
                        "migration committed file stage cleanup",
                    )
                elif not stage_absent:
                    raise _ConcurrentMutationError(
                        f"concurrent mutation at checkpointed stage: {intent.staging}"
                    )
        elif intent.operation == "remove-file":
            target_before = _named_file_matches(
                parent_fd, target_name, intent.before
            )
            target_absent = _named_stat(parent_fd, target_name) is None
            stage_before = _named_file_matches(
                parent_fd, stage_name, intent.before
            )
            stage_absent = _named_stat(parent_fd, stage_name) is None
            if boundary == "before":
                if target_before and stage_absent:
                    pass
                elif target_absent and stage_before:
                    _rename_exclusive(
                        parent_fd,
                        stage_name,
                        parent_fd,
                        target_name,
                    )
                else:
                    raise _ConcurrentMutationError(
                        f"concurrent mutation at pending target: {intent.target}"
                    )
            else:
                if not target_absent:
                    raise _ConcurrentMutationError(
                        f"concurrent mutation at checkpointed target: {intent.target}"
                    )
                if stage_before:
                    _quarantine_remove_name(
                        parent_fd,
                        stage_name,
                        intent.before.inode,
                        "migration committed removed file cleanup",
                    )
                elif not stage_absent:
                    raise _ConcurrentMutationError(
                        f"concurrent mutation at checkpointed stage: {intent.staging}"
                    )
        elif intent.operation == "create-directory":
            target_absent = _named_stat(parent_fd, target_name) is None
            target_intended = _named_directory_matches(
                parent_fd, target_name, intent.intended
            )
            stage_absent = _named_stat(parent_fd, stage_name) is None
            stage_intended = _named_directory_matches(
                parent_fd, stage_name, intent.intended
            )
            if boundary == "before":
                if target_absent and stage_intended:
                    _remove_empty_named_directory(
                        parent_fd, stage_name, intent.intended
                    )
                elif target_intended and stage_absent:
                    _rename_exclusive(
                        parent_fd,
                        target_name,
                        parent_fd,
                        stage_name,
                    )
                    _remove_empty_named_directory(
                        parent_fd, stage_name, intent.intended
                    )
                else:
                    raise _ConcurrentMutationError(
                        f"concurrent mutation at pending directory: {intent.target}"
                    )
            elif not target_intended or not stage_absent:
                raise _ConcurrentMutationError(
                    f"concurrent mutation at checkpointed directory: {intent.target}"
                )
        else:
            target_before = _named_directory_matches(
                parent_fd, target_name, intent.before
            )
            target_absent = _named_stat(parent_fd, target_name) is None
            stage_before = _named_directory_matches(
                parent_fd, stage_name, intent.before
            )
            stage_absent = _named_stat(parent_fd, stage_name) is None
            if boundary == "before":
                if target_before and stage_absent:
                    pass
                elif target_absent and stage_before:
                    _rename_exclusive(
                        parent_fd,
                        stage_name,
                        parent_fd,
                        target_name,
                    )
                else:
                    raise _ConcurrentMutationError(
                        f"concurrent mutation at pending directory: {intent.target}"
                    )
            else:
                if not target_absent:
                    raise _ConcurrentMutationError(
                        f"concurrent mutation at checkpointed directory: {intent.target}"
                    )
                if stage_before:
                    _remove_empty_named_directory(
                        parent_fd, stage_name, intent.before
                    )
                elif not stage_absent:
                    raise _ConcurrentMutationError(
                        f"concurrent mutation at checkpointed stage: {intent.staging}"
                    )
        check_owned()
    finally:
        os.close(parent_fd)


def _application_directory_record(plan, path):
    current = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(current.st_mode):
        raise RuntimeError(f"migration directory changed type: {path}")
    return {
        **_manifest_path_record(plan, path),
        "inode": list(_inode_from_stat(current)),
        "mode": current.st_mode,
        "link_count": current.st_nlink,
        "atime_ns": current.st_atime_ns,
        "mtime_ns": current.st_mtime_ns,
        "ctime_ns": current.st_ctime_ns,
        "size": current.st_size,
    }


def _reconcile_mutation_application(handle, record, application):
    raw_intent = record.get("mutation_intent")
    if raw_intent is None:
        return application
    plan = _intent_plan(handle)
    intent = _parse_mutation_intent(plan, raw_intent)
    index = _application_index(application)
    target_identity = _manifest_path_record(plan, intent.target)
    target_key = (target_identity["kind"], target_identity["path"])
    state = _path_state(intent.target)
    if state == "file":
        binding = _capture_input_binding(intent.target)
        index["post_bindings"][target_key] = _serialize_post_binding(
            plan,
            binding,
        )
        original_files = {
            candidate
            for binding in handle.bindings
            for candidate in (
                binding.path,
                _post_migration_path(
                    binding.path,
                    plan.source_project,
                    plan.destination_project,
                ),
            )
        }
        if intent.target not in original_files:
            index["created_files"][target_key] = target_identity
        else:
            index["created_files"].pop(target_key, None)
        index["post_directories"].pop(target_key, None)
        index["created_directories"].pop(target_key, None)
    elif state == "directory":
        index["post_directories"][target_key] = _application_directory_record(
            plan,
            intent.target,
        )
        original_directories = {
            candidate
            for binding in _manifest_directory_bindings_from_payload(
                handle.base_payload,
                handle.base_vault,
            )
            for candidate in (
                binding.path,
                _post_migration_path(
                    binding.path,
                    plan.source_project,
                    plan.destination_project,
                ),
            )
        }
        if intent.target not in original_directories:
            index["created_directories"][target_key] = target_identity
        else:
            index["created_directories"].pop(target_key, None)
        index["post_bindings"].pop(target_key, None)
        index["created_files"].pop(target_key, None)
    elif state == "absent":
        index["post_bindings"].pop(target_key, None)
        index["created_files"].pop(target_key, None)
        index["post_directories"].pop(target_key, None)
        index["created_directories"].pop(target_key, None)
    else:
        raise _ConcurrentMutationError(
            f"concurrent mutation changed target type: {intent.target}"
        )

    parent_identity = _manifest_path_record(plan, intent.target.parent)
    parent_key = (parent_identity["kind"], parent_identity["path"])
    if parent_key in index["post_directories"]:
        index["post_directories"][parent_key] = _application_directory_record(
            plan,
            intent.target.parent,
        )
    return _materialize_application(index)


def _checkpoint_payload(
    handle,
    record,
    ownership_check=None,
    allow_binding_drift=False,
):
    applying_intent = record.get("status") == "applying"
    if applying_intent:
        _resolve_checkpoint_mutation(
            handle,
            record,
            ownership_check,
            allow_binding_drift=allow_binding_drift,
        )
    transient = dict(handle.payload)
    transient["status"] = "applied"
    application = record.get("application") or _materialize_application(
        handle.checkpoint_application_index
    )
    transient["application"] = (
        _reconcile_mutation_application(
            handle,
            record,
            application,
        )
        if applying_intent
        else application
    )
    return transient


def _checkpoint_payload_for_recovery(
    handle,
    plan,
    record,
    ownership_check,
    allow_binding_drift=False,
):
    transient = _checkpoint_payload(
        handle,
        record,
        ownership_check,
        allow_binding_drift=allow_binding_drift,
    )
    durable_application = _materialize_application(
        handle.checkpoint_application_index
    )
    reconciliation_delta = _application_delta(
        durable_application,
        transient["application"],
    )
    raw_intent = record.get("mutation_intent")
    requires_cleanup = False
    if (
        record.get("status") == "applying"
        and record.get("boundary") == "after"
        and not str(record.get("phase", "")).startswith("cleanup:")
        and raw_intent is not None
    ):
        requires_cleanup = _mutation_intent_requires_cleanup(
            _parse_mutation_intent(_intent_plan(handle), raw_intent)
        )
    if not requires_cleanup:
        reconciliation = (
            (raw_intent, reconciliation_delta)
            if raw_intent is not None and reconciliation_delta
            else None
        )
        return transient, record, reconciliation

    cleanup_record = _publish_checkpoint(
        handle,
        plan,
        f"cleanup:{record['phase']}",
        "after",
        "applying",
        ownership_check,
        mutation_intent=raw_intent,
        delta_override=reconciliation_delta,
    )
    transient = _checkpoint_payload(
        handle,
        cleanup_record,
        ownership_check,
        allow_binding_drift=allow_binding_drift,
    )
    return transient, cleanup_record, None


def _manifest_application(payload, vault, contract):
    application = payload.get("application")
    if payload.get("status") != "applied" or not isinstance(application, dict):
        raise ValueError("brand migration manifest is not in applied state")

    def record_paths(key):
        values = application.get(key)
        if not isinstance(values, list):
            raise ValueError(f"manifest application {key} is invalid")
        return tuple(
            _record_to_path(vault, item, f"manifest application {key}")
            for item in values
        )

    created_files = record_paths("created_files")
    created_directories = record_paths("created_directories")
    post_bindings = []
    raw_post_bindings = application.get("post_bindings")
    if not isinstance(raw_post_bindings, list):
        raise ValueError("manifest post bindings are invalid")
    for item in raw_post_bindings:
        try:
            binding = InputBinding(
                path=_record_to_path(vault, item, "manifest post binding"),
                sha256=item["sha256"],
                inode=tuple(item["inode"]),
                link_count=item["link_count"],
                mode=item["mode"],
                size=item["size"],
                mtime_ns=item["mtime_ns"],
                ctime_ns=item["ctime_ns"],
                expected_type=item["expected_type"],
            )
        except (KeyError, TypeError) as exc:
            raise ValueError("manifest post binding is invalid") from exc
        if (
            binding.expected_type != "regular_file"
            or binding.link_count != 1
            or not isinstance(binding.mode, int)
            or not stat.S_ISREG(binding.mode)
            or not isinstance(binding.size, int)
            or binding.size < 0
            or len(binding.inode) != 2
            or not all(isinstance(value, int) for value in binding.inode)
            or not isinstance(binding.sha256, str)
            or len(binding.sha256) != 64
            or not isinstance(binding.mtime_ns, int)
            or not isinstance(binding.ctime_ns, int)
        ):
            raise ValueError("manifest post binding is invalid")
        post_bindings.append(binding)
    if (
        len({item.path for item in post_bindings}) != len(post_bindings)
        or len({item.inode for item in post_bindings}) != len(post_bindings)
    ):
        raise ValueError("manifest post bindings are not unique")
    raw_directories = application.get("post_directories")
    if not isinstance(raw_directories, list):
        raise ValueError("manifest post directories are invalid")
    post_directories = []
    for item in raw_directories:
        path = _record_to_path(vault, item, "manifest post directory")
        inode = item.get("inode")
        mode = item.get("mode")
        link_count = item.get("link_count")
        atime_ns = item.get("atime_ns")
        mtime_ns = item.get("mtime_ns")
        ctime_ns = item.get("ctime_ns")
        size = item.get("size")
        if (
            not isinstance(inode, list)
            or len(inode) != 2
            or not all(isinstance(value, int) for value in inode)
            or not isinstance(mode, int)
            or not stat.S_ISDIR(mode)
            or not isinstance(link_count, int)
            or link_count < 1
            or not isinstance(atime_ns, int)
            or not isinstance(mtime_ns, int)
            or not isinstance(ctime_ns, int)
            or not isinstance(size, int)
            or size < 0
        ):
            raise ValueError("manifest post directory is invalid")
        post_directories.append(
            (
                path,
                tuple(inode),
                mode,
                link_count,
                atime_ns,
                mtime_ns,
                ctime_ns,
                size,
            )
        )
    old_root = vault / "01-Projects" / payload["old_slug"]
    new_root = vault / "01-Projects" / payload["new_slug"]
    roots = tuple(
        sorted(
            {
                candidate
                for root in contract.mutable_roots
                for candidate in (
                    root,
                    _post_migration_path(root, old_root, new_root),
                )
            }
        )
    )
    absent = {
        candidate
        for path in contract.absent_paths
        for candidate in (path, _post_migration_path(path, old_root, new_root))
    }
    explicit = {
        candidate
        for spec in contract.target_specs
        for candidate in (
            spec.path,
            _post_migration_path(spec.path, old_root, new_root),
        )
    }
    explicit.update(
        candidate
        for path in contract.mutable_directories
        for candidate in (path, _post_migration_path(path, old_root, new_root))
    )
    excluded = {
        candidate
        for path in contract.excluded_mutable_subtrees
        for candidate in (path, _post_migration_path(path, old_root, new_root))
    }
    for path in (*created_files, *created_directories):
        if path not in absent and not any(
            path == root or path.is_relative_to(root) for root in roots
        ):
            raise ValueError("manifest application created path is outside contract")
    post_paths = {binding.path for binding in post_bindings}
    post_directory_paths = {
        path for path, *_metadata in post_directories
    }
    if any(
        path not in explicit
        and any(
            path == excluded_root or path.is_relative_to(excluded_root)
            for excluded_root in excluded
        )
        for path in (
            *created_files,
            *created_directories,
            *post_paths,
            *post_directory_paths,
        )
    ):
        raise ValueError(
            "manifest application path is below an excluded mutable subtree"
        )
    if not set(created_files).issubset(post_paths):
        raise ValueError("manifest created files do not match post bindings")
    if not set(created_directories).issubset(post_directory_paths):
        raise ValueError("manifest created directories do not match post inventory")
    return {
        "created_files": created_files,
        "created_directories": created_directories,
        "post_bindings": tuple(post_bindings),
        "post_directories": tuple(post_directories),
    }


def _assert_no_post_migration_drift(
    application,
    contract,
    old_root=None,
    new_root=None,
):
    vault = old_root.parents[1] if old_root is not None else None
    guard_metadata_directories = (
        {vault / "04-Feedback", vault / "04-Feedback" / "_logs"}
        if vault is not None
        else set()
    )
    expected_files = {binding.path for binding in application["post_bindings"]}
    expected_directories = {
        path
        for path, *_metadata in application["post_directories"]
    }
    directory_stats_before_walk = {}
    for path in expected_directories:
        try:
            directory_stats_before_walk[path] = os.stat(
                path,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
    current_files = set()
    current_directories = set()
    roots = set(contract.mutable_roots)
    if old_root is not None and new_root is not None:
        roots.update(
            _post_migration_path(root, old_root, new_root)
            for root in contract.mutable_roots
        )
    roots = tuple(sorted(roots))
    excluded = set(contract.excluded_mutable_subtrees)
    if old_root is not None and new_root is not None:
        excluded.update(
            _post_migration_path(path, old_root, new_root)
            for path in contract.excluded_mutable_subtrees
        )
    for root in roots:
        if not os.path.lexists(root):
            continue
        files, directories = _walk_tree(
            root,
            f"rollback drift mutable root {root}",
            excluded_roots=excluded,
        )
        current_files.update(files)
        current_directories.update(directories)
    for path in expected_files:
        if (
            not any(
                path == root or path.is_relative_to(root)
                for root in roots
            )
            or any(
                path == excluded_root or path.is_relative_to(excluded_root)
                for excluded_root in excluded
            )
        ) and os.path.lexists(path):
            current_files.add(path)
    for path in expected_directories:
        if (
            not any(
                path == root or path.is_relative_to(root)
                for root in roots
            )
            or any(
                path == excluded_root or path.is_relative_to(excluded_root)
                for excluded_root in excluded
            )
        ) and os.path.lexists(path):
            current_directories.add(path)
    if current_files != expected_files or current_directories != expected_directories:
        raise RuntimeError(
            "rollback target changed after migration; "
            "rerun with force only after review"
        )
    for binding in application["post_bindings"]:
        try:
            _revalidate_input_binding(binding, "after migration")
        except RuntimeError as exc:
            raise RuntimeError(
                "rollback target changed after migration; "
                "rerun with force only after review"
            ) from exc
    for (
        path,
        inode,
        mode,
        link_count,
        atime_ns,
        mtime_ns,
        ctime_ns,
        size,
    ) in application["post_directories"]:
        current = directory_stats_before_walk.get(path)
        if current is None:
            raise RuntimeError(
                "rollback target changed after migration; "
                "rerun with force only after review"
            )
        if (
            not stat.S_ISDIR(current.st_mode)
            or _inode_from_stat(current) != inode
            or current.st_mode != mode
            or current.st_nlink != link_count
            or current.st_size != size
            or (
                path not in guard_metadata_directories
                and (
                    current.st_mtime_ns != mtime_ns
                    or current.st_ctime_ns != ctime_ns
                )
            )
        ):
            raise RuntimeError(
                "rollback target changed after migration; "
                f"rerun with force only after review: {path}; "
                f"inode={_inode_from_stat(current)} expected={inode}; "
                f"mode={current.st_mode} expected={mode}; "
                f"links={current.st_nlink} expected={link_count}; "
                f"atime={current.st_atime_ns} expected={atime_ns}; "
                f"mtime={current.st_mtime_ns} expected={mtime_ns}; "
                f"ctime={current.st_ctime_ns} expected={ctime_ns}; "
                f"size={current.st_size} expected={size}"
            )


def _quarantine_remove_file(handle, path, expected_inode):
    _inspect_sealed_backup(handle)
    parent_fd, leaf = _open_handle_parent(handle, path)
    try:
        _quarantine_remove_name(
            parent_fd,
            leaf,
            expected_inode,
            "rollback deletion",
        )
    finally:
        os.close(parent_fd)


def _remove_directory_if_empty(path, expected_inode=None, handle=None):
    parent_fd, leaf = (
        _open_handle_parent(handle, path)
        if handle is not None
        else _open_absolute_parent(path)
    )
    quarantine_name = f".brand-migration-directory-quarantine-{secrets.token_hex(12)}"
    quarantine_fd = None
    moved = False
    primary_error = None
    try:
        current = _named_stat(parent_fd, leaf)
        if current is None:
            return
        os.mkdir(quarantine_name, mode=0o700, dir_fd=parent_fd)
        quarantine_fd = os.open(
            quarantine_name,
            _directory_flags(),
            dir_fd=parent_fd,
        )
        _rename_exclusive(parent_fd, leaf, quarantine_fd, "entry")
        moved = True
        quarantined = _named_stat(quarantine_fd, "entry")
        moved_inode = (
            _inode_from_stat(quarantined) if quarantined is not None else None
        )
        if (
            quarantined is None
            or not stat.S_ISDIR(quarantined.st_mode)
            or (expected_inode is not None and moved_inode != expected_inode)
        ):
            try:
                _rename_exclusive(quarantine_fd, "entry", parent_fd, leaf)
                moved = False
                restored = _named_stat(parent_fd, leaf)
                if (
                    restored is None
                    or _inode_from_stat(restored) != moved_inode
                    or stat.S_IFMT(restored.st_mode)
                    != stat.S_IFMT(quarantined.st_mode)
                ):
                    raise RuntimeError("restored directory inode changed")
            except Exception as recovery_error:
                raise RuntimeError(
                    f"concurrent rollback directory deletion; recovery failed: "
                    f"{recovery_error}"
                ) from recovery_error
            raise _ConcurrentMutationError(
                "concurrent rollback directory deletion; unexpected entry was restored"
            )
        try:
            os.rmdir("entry", dir_fd=quarantine_fd)
            moved = False
        except OSError as exc:
            if exc.errno != errno.ENOTEMPTY:
                raise
            _rename_exclusive(quarantine_fd, "entry", parent_fd, leaf)
            moved = False
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_errors = []
        if quarantine_fd is not None:
            try:
                os.close(quarantine_fd)
            except OSError as cleanup_error:
                cleanup_errors.append(cleanup_error)
        if not moved:
            try:
                os.rmdir(quarantine_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            except OSError as cleanup_error:
                cleanup_errors.append(cleanup_error)
        try:
            os.close(parent_fd)
        except OSError as cleanup_error:
            cleanup_errors.append(cleanup_error)
        if cleanup_errors:
            detail = "; ".join(str(error) for error in cleanup_errors)
            if primary_error is not None:
                raise RuntimeError(
                    f"{primary_error}; directory quarantine cleanup failed: {detail}"
                ) from primary_error
            raise RuntimeError(f"directory quarantine cleanup failed: {detail}")


def _validate_restore_directory_inodes(
    handle,
    paths,
    post_directories,
    post_directory_links,
):
    authorized_recreation = set()
    for path in sorted(paths, key=lambda item: (len(item.parts), str(item))):
        expected_inode = post_directories.get(path)
        try:
            parent_fd, leaf = _open_handle_parent(handle, path)
        except (FileNotFoundError, RuntimeError):
            if expected_inode is not None:
                raise RuntimeError(f"rollback directory inode changed: {path}")
            authorized_recreation.add(path)
            continue
        try:
            current = _named_stat(parent_fd, leaf)
        finally:
            os.close(parent_fd)
        if expected_inode is None:
            if current is not None:
                raise RuntimeError(f"rollback directory inode changed: {path}")
            authorized_recreation.add(path)
            continue
        if (
            current is None
            or not stat.S_ISDIR(current.st_mode)
            or _inode_from_stat(current) != expected_inode
        ):
            raise RuntimeError(f"rollback directory inode changed: {path}")
        if current.st_nlink != post_directory_links[path]:
            raise RuntimeError(f"rollback directory link count changed: {path}")
    return authorized_recreation


def _ensure_restore_directories(
    handle,
    paths,
    post_directories,
    authorized_recreation,
):
    created = {}
    for path in sorted(paths, key=lambda item: (len(item.parts), str(item))):
        parent_fd, leaf = _open_handle_parent(handle, path)
        try:
            current = _named_stat(parent_fd, leaf)
            expected_inode = post_directories.get(path)
            if current is None:
                if path not in authorized_recreation:
                    raise RuntimeError(f"rollback directory disappeared: {path}")
                os.mkdir(leaf, mode=0o700, dir_fd=parent_fd)
                current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
                created[path] = _inode_from_stat(current)
            elif expected_inode is None:
                raise RuntimeError(f"rollback directory appeared: {path}")
            elif (
                not stat.S_ISDIR(current.st_mode)
                or _inode_from_stat(current) != expected_inode
            ):
                raise RuntimeError(f"rollback directory inode changed: {path}")
        finally:
            os.close(parent_fd)
    return created


def _read_backup_payload(handle, relative, expected_hash):
    fd = _open_staging_file(handle.root_fd, relative)
    try:
        current = os.fstat(fd)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_nlink != 1
            or stat.S_IMODE(current.st_mode) != 0o400
        ):
            raise RuntimeError(f"sealed rollback payload changed: {relative}")
        data = _read_fd_bytes(fd)
    finally:
        os.close(fd)
    if hashlib.sha256(data).hexdigest() != expected_hash:
        raise RuntimeError(f"sealed rollback payload digest changed: {relative}")
    return data


def _set_and_verify_restored_metadata(path, binding, handle=None):
    parent_fd, leaf = (
        _open_handle_parent(handle, path)
        if handle is not None
        else _open_absolute_parent(path)
    )
    fd = None
    try:
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        current = os.fstat(fd)
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1:
            raise RuntimeError(f"restored target is unsafe: {path}")
        os.fchmod(fd, stat.S_IMODE(binding.mode))
        os.utime(
            leaf,
            ns=(binding.mtime_ns, binding.mtime_ns),
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        os.fsync(fd)
        current = os.fstat(fd)
        digest = _hash_fd(fd)
        if (
            digest != binding.sha256
            or current.st_size != binding.size
            or stat.S_IMODE(current.st_mode) != stat.S_IMODE(binding.mode)
            or current.st_mtime_ns != binding.mtime_ns
        ):
            raise RuntimeError(f"restored target verification failed: {path}")
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def _restore_directory_metadata(binding, expected_inode=None, handle=None):
    parent_fd, leaf = (
        _open_handle_parent(handle, binding.path)
        if handle is not None
        else _open_absolute_parent(binding.path)
    )
    fd = None
    try:
        fd = os.open(leaf, _directory_flags(), dir_fd=parent_fd)
        current = os.fstat(fd)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (
                expected_inode is not None
                and _inode_from_stat(current) != expected_inode
            )
        ):
            raise RuntimeError(
                f"restored directory inode changed: {binding.path}"
            )
        os.fchmod(fd, stat.S_IMODE(binding.mode))
        os.utime(fd, ns=(binding.atime_ns, binding.mtime_ns))
        os.fsync(fd)
        restored = os.fstat(fd)
        if (
            not stat.S_ISDIR(restored.st_mode)
            or stat.S_IMODE(restored.st_mode) != stat.S_IMODE(binding.mode)
            or restored.st_atime_ns != binding.atime_ns
            or restored.st_mtime_ns != binding.mtime_ns
        ):
            raise RuntimeError(
                f"restored directory metadata verification failed: {binding.path}"
            )
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent_fd)


def _rollback_with_handle(
    handle,
    force=False,
    ownership_check=None,
    recovery_plan=None,
    recovery_reconciliation=None,
):
    check_owned = ownership_check or (lambda: None)
    check_owned()
    vault, contract, bindings = _validate_manifest_payload(handle.payload)
    current_application = _manifest_application(handle.payload, vault, contract)
    if not force:
        _assert_no_post_migration_drift(
            current_application,
            contract,
            vault / "01-Projects" / handle.payload["old_slug"],
            vault / "01-Projects" / handle.payload["new_slug"],
        )
    check_owned()
    if recovery_plan is None:
        raise ValueError("rollback requires an authoritative recovery plan")

    def checkpoint(
        phase,
        boundary,
        status="rolling_back",
        mutation_intent=None,
        delta_override=_NO_DELTA_OVERRIDE,
    ):
        return _publish_checkpoint(
            handle,
            recovery_plan,
            phase,
            boundary,
            status,
            check_owned,
            mutation_intent=mutation_intent,
            delta_override=delta_override,
        )

    chain = _load_checkpoint_chain(handle, check_owned)
    rollback_start = next(
        (
            index
            for index, item in enumerate(chain)
            if item[0]["status"] == "rolling_back"
        ),
        None,
    )
    if rollback_start is None:
        application = current_application
        completed_phases = set()
        if recovery_reconciliation is None:
            checkpoint("rollback-start", "before")
        else:
            reconciliation_intent, reconciliation_delta = (
                recovery_reconciliation
            )
            checkpoint(
                "rollback-start",
                "before",
                mutation_intent=reconciliation_intent,
                delta_override=reconciliation_delta,
            )
    else:
        if recovery_reconciliation is not None:
            raise ValueError(
                "rollback reconciliation cannot follow rollback-start"
            )
        if rollback_start == 0:
            raise ValueError("rollback journal has no authoritative basis")
        if handle.checkpoint_rollback_basis is None:
            raise ValueError("rollback journal has no authoritative basis state")
        basis_payload = dict(handle.payload)
        basis_payload["status"] = "applied"
        basis_payload["application"] = handle.checkpoint_rollback_basis
        application = _manifest_application(basis_payload, vault, contract)
        completed_phases = {
            item[0]["phase"]
            for item in chain[rollback_start + 1 :]
            if item[0]["status"] in {"rolling_back", "rolled_back"}
        }
    post_bindings = {binding.path: binding for binding in application["post_bindings"]}
    post_directories = {
        path: inode
        for path, inode, *_metadata in application["post_directories"]
    }
    current_post_directories = {
        path: inode
        for path, inode, *_metadata in current_application["post_directories"]
    }
    post_directory_links = {
        path: link_count
        for path, _inode, _mode, link_count, *_metadata in application["post_directories"]
    }
    current_post_directory_links = {
        path: link_count
        for path, _inode, _mode, link_count, *_metadata in current_application[
            "post_directories"
        ]
    }
    directory_bindings = _manifest_directory_bindings_from_payload(
        handle.payload,
        vault,
    )
    restore_directories = set(contract.mutable_directories)
    restore_directories.update(
        vault / Path(item) for item in handle.payload.get("source_directories", ())
    )
    if "rollback-restore-directories" in completed_phases:
        for path in restore_directories:
            if path in current_post_directories:
                post_directories[path] = current_post_directories[path]
                post_directory_links[path] = current_post_directory_links[path]
    authorized_recreation = _validate_restore_directory_inodes(
        handle,
        restore_directories,
        post_directories,
        post_directory_links,
    )
    old_root = vault / "01-Projects" / handle.payload["old_slug"]
    new_root = vault / "01-Projects" / handle.payload["new_slug"]
    removal_files = set(application["created_files"])
    for binding in bindings:
        if binding.path == old_root or binding.path.is_relative_to(old_root):
            removal_files.add(_post_migration_path(binding.path, old_root, new_root))
    for path in sorted(removal_files, key=lambda item: (len(item.parts), str(item)), reverse=True):
        phase = f"rollback-remove-file:{path}"
        if phase in completed_phases:
            continue
        check_owned()
        post = post_bindings.get(path)
        if post is None and not os.path.lexists(path):
            checkpoint(phase, "after")
            continue
        parent_fd, leaf = _open_handle_parent(handle, path)
        try:
            current = _named_stat(parent_fd, leaf)
        finally:
            os.close(parent_fd)
        if current is None:
            checkpoint(phase, "after")
            continue
        if post is None:
            raise RuntimeError(f"rollback removal is not bound: {path}")
        _quarantine_remove_file(handle, path, post.inode)
        check_owned()
        checkpoint(phase, "after")
    removal_directories = set(application["created_directories"])
    removal_directories.update(
        _post_migration_path(path, old_root, new_root)
        for path in contract.mutable_directories
        if path == old_root or path.is_relative_to(old_root)
    )
    for path in sorted(
        removal_directories,
        key=lambda item: (len(item.parts), str(item)),
        reverse=True,
    ):
        phase = f"rollback-remove-directory:{path}"
        if phase in completed_phases:
            continue
        check_owned()
        if post_directories.get(path) is None and not os.path.lexists(path):
            checkpoint(phase, "after")
            continue
        _remove_directory_if_empty(
            path,
            post_directories.get(path),
            handle=handle,
        )
        check_owned()
        checkpoint(phase, "after")

    recreated_directories = {}
    if "rollback-restore-directories" not in completed_phases:
        check_owned()
        recreated_directories = _ensure_restore_directories(
            handle,
            restore_directories,
            post_directories,
            authorized_recreation,
        )
        check_owned()
        checkpoint("rollback-restore-directories", "after")
    records_by_path = {
        _record_to_path(vault, record, "manifest file"): record
        for record in handle.payload["files"]
    }
    for binding in bindings:
        phase = f"rollback-restore-file:{binding.path}"
        if phase in completed_phases:
            continue
        check_owned()
        record = records_by_path[binding.path]
        data = _read_backup_payload(
            handle, Path(record["backup"]), binding.sha256
        )
        expected_current = post_bindings.get(binding.path)
        if binding.path.is_relative_to(vault):
            _write_vault_target(
                handle,
                binding.path,
                data,
                expected_current,
                restore_directories,
                allow_binding_drift=force,
            )
        else:
            _write_external_target(
                contract,
                binding.path,
                data,
                expected_current,
                restore_directories,
                allow_binding_drift=force,
                input_bindings=bindings,
            )
        _set_and_verify_restored_metadata(binding.path, binding, handle=handle)
        check_owned()
        checkpoint(phase, "after")
    for binding in sorted(
        directory_bindings,
        key=lambda item: (len(item.path.parts), str(item.path)),
        reverse=True,
    ):
        phase = f"rollback-directory-metadata:{binding.path}"
        if phase in completed_phases:
            continue
        check_owned()
        expected_inode = post_directories.get(binding.path)
        if expected_inode is None:
            expected_inode = recreated_directories.get(binding.path)
        if expected_inode is None:
            expected_inode = current_post_directories.get(binding.path)
        if expected_inode is None:
            raise RuntimeError(
                f"rollback directory recreation is not authorized: {binding.path}"
            )
        _restore_directory_metadata(
            binding,
            expected_inode=expected_inode,
            handle=handle,
        )
        check_owned()
        checkpoint(phase, "after")
    checkpoint("rollback-complete", "after", status="rolled_back")
    return {
        "status": "rolled_back",
        "manifest_path": str(handle.path / "manifest.json"),
    }


def apply_brand_migration(plan, migration_id, rebuilders=None):
    rebuilder_mode = _select_rebuilder_mode(rebuilders)
    _require_secure_apply_primitives()
    with (
        _pinned_vault_directory(plan.vault) as vault_pin,
        migration_writer_guard(plan.vault, vault_pin) as guard,
    ):
        guard_directories = guard.allowed_created_directories
        guard.assert_owned()
        _assert_plan_unchanged(
            plan, allowed_created_directories=guard_directories
        )
        manifest_path = create_migration_backup(
            plan,
            migration_id,
            allowed_created_directories=guard_directories,
        )
        guard.assert_owned()
        handle = _open_backup_handle(
            manifest_path,
            expected_plan=plan,
            vault_pin=vault_pin,
        )
        renamed = False
        try:
            def checkpoint(
                phase,
                boundary,
                status="applying",
                validation=None,
                mutation_intent=None,
                delta_override=_NO_DELTA_OVERRIDE,
            ):
                return _publish_checkpoint(
                    handle,
                    plan,
                    phase,
                    boundary,
                    status,
                    guard.assert_owned,
                    validation=validation,
                    mutation_intent=mutation_intent,
                    delta_override=delta_override,
                )

            _inspect_sealed_backup(handle, expected_plan=plan)
            _assert_plan_unchanged(
                plan, allowed_created_directories=guard_directories
            )
            _inspect_sealed_backup(handle, expected_plan=plan)
            guard.assert_owned()
            source_pin = _pin_project_source(plan)
            try:
                source_rename_intent = _project_rename_intent(
                    plan,
                    source_pin,
                )
                _mark_manifest_applying(
                    handle,
                    plan,
                    guard.assert_owned,
                    source_rename_intent,
                )
                guard.assert_owned()
                try:
                    _rename_project_directory(plan, handle, source_pin)
                except _ProjectRenameRace as exc:
                    renamed = exc.rename_occurred and not exc.recovered
                    raise
            finally:
                _close_project_source_pin(source_pin)
            renamed = True
            guard.assert_owned()
            checkpoint(
                "source-rename",
                "after",
                mutation_intent=source_rename_intent,
            )
            mutation_io = _MigrationIO(
                plan,
                handle,
                guard.assert_owned,
                checkpoint,
            )
            try:
                for original in plan.markdown_paths:
                    target = _post_migration_path(
                        original, plan.source_project, plan.destination_project
                    )
                    content = _read_utf8(target, "Markdown")
                    updated, changed = rewrite_markdown(
                        content, plan.old_slug, plan.new_slug
                    )
                    if not changed:
                        continue
                    phase = f"rewrite:{original}"
                    mutation_io.begin_rebuild_phase(phase)
                    try:
                        mutation_io.atomic_write(
                            target,
                            updated.encode("utf-8"),
                        )
                    except Exception:
                        mutation_io.abort_rebuild_phase()
                        raise
                    mutation_io.finish_rebuild_phase()
                if plan.config_path is not None:
                    mutation_io.begin_rebuild_phase("rewrite:config")
                    try:
                        mutation_io.atomic_write(
                            plan.config_path,
                            _rewritten_config_bytes(plan),
                        )
                    except Exception:
                        mutation_io.abort_rebuild_phase()
                        raise
                    mutation_io.finish_rebuild_phase()
                cfg = load_migration_config(plan)
                if rebuilder_mode == "default":
                    _run_default_rebuilders(
                        cfg,
                        guard,
                        mutation_io,
                        checkpoint=checkpoint,
                    )
            finally:
                mutation_io.close()
            if rebuilder_mode == "default":
                guard.assert_owned()
            checkpoint("validation-finalization", "before")
            validation = validate_brand_migration(plan)
            guard.assert_owned()
            if not validation["valid"]:
                raise RuntimeError(validation["message"])
            post_state = _capture_post_state(plan)
            guard.assert_owned()
            return _finalize_applied_manifest(
                handle,
                plan,
                validation,
                post_state,
                guard.assert_owned,
            )
        except Exception as primary_error:
            if isinstance(primary_error, _CheckpointFailure):
                raise RuntimeError(
                    f"migration checkpoint failed: {primary_error}; "
                    f"recovery required from sealed manifest: {manifest_path}"
                ) from primary_error
            try:
                guard.assert_owned()
            except Exception as guard_error:
                raise RuntimeError(
                    f"migration failed: {primary_error}; "
                    "writer guard ownership lost; automatic rollback was not attempted; "
                    f"recovery required from sealed manifest: {manifest_path}; "
                    f"{guard_error}"
                ) from primary_error
            if (
                isinstance(primary_error, _ConcurrentMutationError)
                and not (
                    isinstance(primary_error, _ProjectRenameRace)
                    and primary_error.rename_occurred
                    and not primary_error.recovered
                )
            ):
                raise
            if renamed:
                try:
                    chain = _load_checkpoint_chain(handle, guard.assert_owned)
                    if not chain:
                        raise RuntimeError("no durable applying checkpoint exists")
                    (
                        handle.payload,
                        _latest,
                        recovery_reconciliation,
                    ) = _checkpoint_payload_for_recovery(
                        handle,
                        plan,
                        chain[-1][0],
                        guard.assert_owned,
                    )
                    _rollback_with_handle(
                        handle,
                        force=True,
                        ownership_check=guard.assert_owned,
                        recovery_plan=plan,
                        recovery_reconciliation=recovery_reconciliation,
                    )
                except Exception as rollback_error:
                    raise RuntimeError(
                        f"migration failed: {primary_error}; "
                        f"automatic rollback failed: {rollback_error}"
                    ) from primary_error
            raise
        finally:
            _close_backup_handle(handle)


def rollback_brand_migration(manifest_path, force=False):
    _require_secure_apply_primitives()
    manifest_path = Path(
        os.path.abspath(os.path.expanduser(os.fspath(manifest_path)))
    )
    vault = _vault_from_manifest_path(manifest_path)
    vault_pin = _open_vault_directory(vault)
    handle = None
    try:
        handle = _open_backup_handle(
            manifest_path,
            vault_pin=vault_pin,
            allow_journal_recovery=True,
        )
        payload_vault = Path(str(handle.payload.get("vault", "")))
        if payload_vault != vault:
            raise ValueError("manifest content Vault does not match its canonical path")
        with migration_writer_guard(vault, vault_pin) as guard:
            def check_owned():
                guard.assert_owned()
                _verify_named_directory(vault_pin)

            check_owned()
            _recover_interrupted_checkpoint_append(handle, check_owned)
            _inspect_sealed_backup(handle)
            recovery_plan = _recovery_plan_from_manifest(handle)
            chain = _load_checkpoint_chain(handle, check_owned)
            if not chain:
                for binding in recovery_plan.input_bindings:
                    _revalidate_input_binding(binding, "before manual recovery")
                for binding in recovery_plan.directory_bindings:
                    _revalidate_directory_binding(binding)
                if recovery_plan.destination_project.exists():
                    raise RuntimeError(
                        "prepared migration has no checkpoint but destination exists"
                    )
                _publish_checkpoint(
                    handle,
                    recovery_plan,
                    "manual-recovery",
                    "before",
                    "applying",
                    check_owned,
                )
                chain = _load_checkpoint_chain(handle, check_owned)
            latest = chain[-1][0]
            (
                transient,
                latest,
                recovery_reconciliation,
            ) = _checkpoint_payload_for_recovery(
                handle,
                recovery_plan,
                latest,
                check_owned,
                allow_binding_drift=force,
            )
            handle.payload = transient
            application = _manifest_application(
                transient,
                vault,
                recovery_plan.mutation_contract,
            )
            if latest["status"] == "rolled_back":
                if not force:
                    _assert_no_post_migration_drift(
                        application,
                        recovery_plan.mutation_contract,
                        recovery_plan.source_project,
                        recovery_plan.destination_project,
                    )
                result = {
                    "status": "rolled_back",
                    "manifest_path": str(manifest_path),
                }
            else:
                result = _rollback_with_handle(
                    handle,
                    force=force,
                    ownership_check=check_owned,
                    recovery_plan=recovery_plan,
                    recovery_reconciliation=recovery_reconciliation,
                )
            check_owned()
            return result
    finally:
        if handle is not None:
            _close_backup_handle(handle)
        os.close(vault_pin.fd)


def atomic_write_json(
    path: str | os.PathLike[str], payload: dict[str, object]
) -> None:
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception as primary_error:
        try:
            if os.path.lexists(tmp):
                tmp.unlink()
        except Exception as cleanup_error:
            raise RuntimeError(
                f"atomic write failed: {primary_error}; cleanup failed: {cleanup_error}"
            ) from primary_error
        raise
