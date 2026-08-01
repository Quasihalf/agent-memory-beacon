"""Shared trust-boundary helpers for transcript-derived data."""
import os
import re
import secrets
import shutil
import stat
import threading
from contextlib import contextmanager
from datetime import date

try:
    import fcntl
except ImportError:  # pragma: no cover - macOS/Linux production path uses fcntl.
    fcntl = None


PROJECT_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
OBSIDIAN_IGNORE_FILTERS = (
    "04-Feedback/_raw-sessions/",
    "04-Feedback/_rollback/",
    "04-Feedback/_cleanup-backups/",
    "04-Feedback/_logs/",
    "04-Feedback/_lifecycle-proposals/",
    "04-Feedback/_annotation-candidates/",
    "05-Agent-Memory/codex-profile/",
    "Users/",
)
VAULT_INTERNAL_DIR_NAMES = frozenset(
    {
        ".git",
        "_cleanup-backups",
        "_logs",
        "_raw-sessions",
        "_rollback",
        "codex-profile",
    }
)
PLATFORM_CONTEXT_TAGS = (
    "INSTRUCTIONS",
    "permissions instructions",
    "app-context",
    "collaboration_mode",
    "skills_instructions",
    "apps_instructions",
    "plugins_instructions",
    "recommended_plugins",
    "environment_context",
)
FRONTMATTER_BLOCK = re.compile(
    r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
_THREAD_LOCK_REGISTRY = {}
_THREAD_LOCK_REGISTRY_GUARD = threading.Lock()


@contextmanager
def _in_process_file_lock(path):
    key = os.path.normcase(os.path.realpath(path))
    with _THREAD_LOCK_REGISTRY_GUARD:
        entry = _THREAD_LOCK_REGISTRY.get(key)
        if entry is None:
            entry = [threading.RLock(), 0]
            _THREAD_LOCK_REGISTRY[key] = entry
        entry[1] += 1

    acquired = False
    try:
        entry[0].acquire()
        acquired = True
        yield
    finally:
        if acquired:
            entry[0].release()
        with _THREAD_LOCK_REGISTRY_GUARD:
            entry[1] -= 1
            if entry[1] == 0 and _THREAD_LOCK_REGISTRY.get(key) is entry:
                del _THREAD_LOCK_REGISTRY[key]


@contextmanager
def exclusive_file_lock(path, root=None):
    """Serialize one local state file across threads and local processes."""
    path = os.path.abspath(os.path.expanduser(str(path)))
    if root is None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    with _in_process_file_lock(path):
        with _pinned_parent(path, root=root) as pin:
            for _attempt in range(16):
                descriptor = os.open(
                    pin.leaf,
                    os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=pin.parent_fd,
                )
                locked = False
                try:
                    pin.assert_named()
                    if not _descriptor_matches_named_file(pin, descriptor):
                        continue
                    if fcntl is not None:
                        fcntl.flock(descriptor, fcntl.LOCK_EX)
                        locked = True
                    pin.assert_named()
                    if not _descriptor_matches_named_file(pin, descriptor):
                        continue
                    yield
                    return
                finally:
                    if locked:
                        fcntl.flock(descriptor, fcntl.LOCK_UN)
                    os.close(descriptor)
            raise OSError("lock inode changed repeatedly while acquiring it")


def _descriptor_matches_named_file(pin, descriptor):
    current = os.fstat(descriptor)
    if not stat.S_ISREG(current.st_mode):
        raise OSError("lock is not a regular file")
    try:
        named = os.stat(
            pin.leaf,
            dir_fd=pin.parent_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        return False
    return (
        stat.S_ISREG(named.st_mode)
        and (named.st_dev, named.st_ino) == (current.st_dev, current.st_ino)
    )


def durable_atomic_write(
    path,
    content,
    encoding="utf-8",
    mode=0o600,
    root=None,
    expected_parent_identity=None,
    preserve_existing_mode=True,
):
    """Publish one regular file without following temp or destination symlinks."""
    path = os.path.abspath(os.path.expanduser(os.fspath(path)))
    leaf = os.path.basename(path)
    if not leaf or leaf in {".", ".."}:
        raise ValueError("atomic destination has no valid filename")
    temp_name = ""
    temp_fd = None
    with _pinned_parent(path, root=root) as pin:
        parent_fd = pin.parent_fd
        try:
            pin.assert_named()
            _assert_expected_parent_identity(parent_fd, expected_parent_identity)
            destination_identity, destination_mode = _destination_identity(
                parent_fd,
                leaf,
            )
            if destination_mode is not None and preserve_existing_mode:
                mode = destination_mode

            for _attempt in range(16):
                temp_name = f".{leaf}.{secrets.token_hex(16)}.tmp"
                try:
                    temp_fd = os.open(
                        temp_name,
                        os.O_WRONLY
                        | os.O_CREAT
                        | os.O_EXCL
                        | getattr(os, "O_NOFOLLOW", 0),
                        mode,
                        dir_fd=parent_fd,
                    )
                    os.fchmod(temp_fd, mode)
                    break
                except FileExistsError:
                    temp_name = ""
            if temp_fd is None:
                raise FileExistsError("cannot allocate exclusive atomic temp file")

            if isinstance(content, bytes):
                with os.fdopen(temp_fd, "wb") as handle:
                    temp_fd = None
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            else:
                with os.fdopen(temp_fd, "w", encoding=encoding, newline="") as handle:
                    temp_fd = None
                    handle.write(str(content))
                    handle.flush()
                    os.fsync(handle.fileno())

            pin.assert_named()
            _assert_expected_parent_identity(parent_fd, expected_parent_identity)
            current_identity, _current_mode = _destination_identity(parent_fd, leaf)
            if current_identity != destination_identity:
                raise OSError("atomic destination changed before publish")
            os.replace(
                temp_name,
                leaf,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            temp_name = ""
            os.fsync(parent_fd)
            pin.assert_named()
        finally:
            if temp_fd is not None:
                os.close(temp_fd)
            if temp_name:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass


def durable_unlink(
    path,
    root=None,
    expected_identity=None,
    expected_parent_identity=None,
):
    """Remove one regular file through a pinned parent and durably publish deletion."""
    path = os.path.abspath(os.path.expanduser(os.fspath(path)))
    with _pinned_parent(path, root=root) as pin:
        pin.assert_named()
        _assert_expected_parent_identity(
            pin.parent_fd,
            expected_parent_identity,
        )
        destination_identity, destination_mode = _destination_identity(
            pin.parent_fd,
            pin.leaf,
        )
        if destination_identity is None:
            raise FileNotFoundError(path)
        if (
            expected_identity is not None
            and destination_identity[:2] != tuple(expected_identity)
        ):
            raise OSError("unlink destination changed")
        os.unlink(pin.leaf, dir_fd=pin.parent_fd)
        os.fsync(pin.parent_fd)
        pin.assert_named()
        _assert_expected_parent_identity(
            pin.parent_fd,
            expected_parent_identity,
        )


def durable_rmdir(path, root=None, expected_identity=None):
    """Remove one empty directory without following managed-path symlinks."""
    path = os.path.abspath(os.path.expanduser(os.fspath(path)))
    with _pinned_parent(path, root=root) as pin:
        pin.assert_named()
        descriptor = os.open(
            pin.leaf,
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=pin.parent_fd,
        )
        try:
            current = os.fstat(descriptor)
            named = os.stat(pin.leaf, dir_fd=pin.parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISDIR(named.st_mode)
                or _directory_identity(named) != _directory_identity(current)
            ):
                raise OSError("rmdir destination changed")
            if (
                expected_identity is not None
                and _directory_identity(current) != tuple(expected_identity)
            ):
                raise OSError("rmdir destination changed")
            if os.listdir(descriptor):
                raise OSError("rmdir destination is not empty")
        finally:
            os.close(descriptor)
        pin.assert_named()
        os.rmdir(pin.leaf, dir_fd=pin.parent_fd)
        os.fsync(pin.parent_fd)
        pin.assert_named()


def durable_rmtree(path, root=None, expected_parent_identity=None):
    """Recursively remove one directory through a descriptor-pinned parent."""
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise OSError("secure recursive removal is unavailable on this platform")
    path = os.path.abspath(os.path.expanduser(os.fspath(path)))
    if (
        not isinstance(expected_parent_identity, (list, tuple))
        or len(expected_parent_identity) != 2
        or not all(isinstance(value, int) for value in expected_parent_identity)
    ):
        raise ValueError("rmtree expected parent identity is invalid")
    with _pinned_parent(path, root=root) as pin:
        pin.assert_named()
        parent_identity = _directory_identity(os.fstat(pin.parent_fd))
        if parent_identity != tuple(expected_parent_identity):
            raise OSError("rmtree parent was replaced")
        named = os.stat(pin.leaf, dir_fd=pin.parent_fd, follow_symlinks=False)
        if not stat.S_ISDIR(named.st_mode):
            raise OSError("rmtree destination is not a directory")
        shutil.rmtree(pin.leaf, dir_fd=pin.parent_fd)
        os.fsync(pin.parent_fd)
        pin.assert_named()


def secure_open_file(path, flags, mode=0o600, root=None):
    """Open one file relative to a descriptor-pinned managed parent."""
    with _pinned_parent(path, root=root) as pin:
        descriptor = os.open(
            pin.leaf,
            flags | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=pin.parent_fd,
        )
        try:
            pin.assert_named()
            return descriptor
        except Exception:
            os.close(descriptor)
            raise


def secure_read_bytes(path, limit, root=None):
    """Read one bounded regular file without following any managed-path symlink."""
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("read limit must be a nonnegative integer")
    with _pinned_parent(path, root=root) as pin:
        pin.assert_named()
        descriptor = os.open(
            pin.leaf,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=pin.parent_fd,
        )
        try:
            current = os.fstat(descriptor)
            if not stat.S_ISREG(current.st_mode):
                raise OSError("read target is not a regular file")
            data = b""
            while len(data) <= limit:
                chunk = os.read(descriptor, min(65_536, limit + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
            pin.assert_named()
            return data
        finally:
            os.close(descriptor)


def secure_list_directory(path, root):
    """List regular files and real directories through a pinned Vault chain."""
    path = os.path.abspath(os.path.expanduser(os.fspath(path)))
    marker = os.path.join(path, ".agent-memory-directory-pin")
    with _pinned_parent(marker, root=root) as pin:
        pin.assert_named()
        directories = []
        files = []
        for name in sorted(os.listdir(pin.parent_fd)):
            current = os.stat(
                name,
                dir_fd=pin.parent_fd,
                follow_symlinks=False,
            )
            if stat.S_ISDIR(current.st_mode):
                directories.append(name)
            elif stat.S_ISREG(current.st_mode):
                files.append(name)
        pin.assert_named()
        return directories, files


def secure_walk(path, root, topdown=True, excluded_directory_names=()):
    """Return an os.walk-like snapshot without following any symlink entry."""
    path = os.path.abspath(os.path.expanduser(os.fspath(path)))
    excluded = frozenset(str(name) for name in excluded_directory_names)
    rows = []

    def visit(current):
        directories, files = secure_list_directory(current, root)
        directories = [name for name in directories if name not in excluded]
        row = (current, list(directories), list(files))
        if topdown:
            rows.append(row)
        for name in directories:
            visit(os.path.join(current, name))
        if not topdown:
            rows.append(row)

    visit(path)
    return rows


def ensure_directory_tree(path, root, mode=0o700):
    """Create/open a Vault-contained directory chain without following symlinks."""
    root_path, target_path, parts = _relative_components(path, root, include_leaf=True)
    descriptors = []
    root_fd = os.open(
        root_path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptors.append(root_fd)
    current_fd = root_fd
    try:
        for part in parts:
            try:
                child_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_fd,
                )
            except FileNotFoundError:
                os.mkdir(part, mode=mode, dir_fd=current_fd)
                os.fsync(current_fd)
                child_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_fd,
                )
            descriptors.append(child_fd)
            current_fd = child_fd
        return target_path
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def assert_no_symlink_components(path, root):
    """Reject an existing symlink anywhere below a trusted root."""
    root_path, _target_path, parts = _relative_components(path, root, include_leaf=True)
    current_fd = os.open(
        root_path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        for part in parts:
            try:
                child_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=current_fd,
                )
            except FileNotFoundError:
                return
            except OSError as exc:
                raise ValueError("configured path contains a symlink or non-directory") from exc
            os.close(current_fd)
            current_fd = child_fd
    finally:
        os.close(current_fd)


class _PinnedParent:
    def __init__(self, root_path, descriptors, links, leaf):
        self.root_path = root_path
        self.descriptors = descriptors
        self.links = links
        self.root_fd = descriptors[0]
        self.parent_fd = descriptors[-1]
        self.leaf = leaf
        self.root_identity = _directory_identity(os.fstat(self.root_fd))

    def assert_named(self):
        try:
            root_stat = os.stat(self.root_path, follow_symlinks=False)
        except OSError as exc:
            raise OSError("pinned root was replaced") from exc
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or _directory_identity(root_stat) != self.root_identity
        ):
            raise OSError("pinned root was replaced")
        for parent_fd, name, child_fd, identity in self.links:
            try:
                named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except OSError as exc:
                raise OSError("pinned path component was replaced") from exc
            if (
                not stat.S_ISDIR(named.st_mode)
                or _directory_identity(named) != identity
                or _directory_identity(os.fstat(child_fd)) != identity
            ):
                raise OSError("pinned path component was replaced")

    def close(self):
        for descriptor in reversed(self.descriptors):
            os.close(descriptor)


@contextmanager
def _pinned_parent(path, root=None):
    path = os.path.abspath(os.path.expanduser(os.fspath(path)))
    parent = os.path.dirname(path)
    leaf = os.path.basename(path)
    root_path, _target, parts = _relative_components(
        parent,
        root or parent,
        include_leaf=True,
    )
    descriptors = []
    links = []
    current_fd = os.open(
        root_path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    descriptors.append(current_fd)
    try:
        for part in parts:
            child_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current_fd,
            )
            identity = _directory_identity(os.fstat(child_fd))
            links.append((current_fd, part, child_fd, identity))
            descriptors.append(child_fd)
            current_fd = child_fd
        pin = _PinnedParent(root_path, descriptors, links, leaf)
        pin.assert_named()
        yield pin
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _relative_components(path, root, include_leaf):
    root_path = os.path.abspath(os.path.expanduser(os.fspath(root)))
    target_path = os.path.abspath(os.path.expanduser(os.fspath(path)))
    try:
        if os.path.commonpath([root_path, target_path]) != root_path:
            raise ValueError("path is outside the pinned root")
    except ValueError as exc:
        raise ValueError("path is outside the pinned root") from exc
    relative = os.path.relpath(target_path, root_path)
    if relative == ".":
        parts = []
    else:
        parts = relative.split(os.sep)
    if not include_leaf and parts:
        parts = parts[:-1]
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("pinned path contains an invalid component")
    return root_path, target_path, parts


def _directory_identity(value):
    return value.st_dev, value.st_ino


def _assert_expected_parent_identity(parent_fd, expected):
    if expected is None:
        return
    if (
        not isinstance(expected, (list, tuple))
        or len(expected) != 2
        or not all(isinstance(value, int) for value in expected)
    ):
        raise ValueError("expected parent identity is invalid")
    if _directory_identity(os.fstat(parent_fd)) != tuple(expected):
        raise OSError("managed parent was replaced")


def _path_matches_directory_fd(parent, expected):
    try:
        current = os.stat(parent, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(current.st_mode) and _directory_identity(current) == expected


def _destination_identity(parent_fd, leaf):
    try:
        current = os.stat(leaf, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None, None
    if stat.S_ISLNK(current.st_mode):
        raise OSError("atomic destination is a symlink")
    if not stat.S_ISREG(current.st_mode):
        raise OSError("atomic destination is not a regular file")
    return (
        current.st_dev,
        current.st_ino,
        current.st_mode,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    ), stat.S_IMODE(current.st_mode)


def normalize_project_slug(value):
    """Return a filesystem-safe project slug, or an empty string."""
    value = re.sub(r"\s+", " ", str(value or "")).strip().strip("'\"")
    if value in {".", ".."} or not PROJECT_SLUG.fullmatch(value):
        return ""
    return value


def normalize_iso_date(value, fallback=None):
    """Accept only a real YYYY-MM-DD date for use in filenames/frontmatter."""
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return fallback or date.today().isoformat()


def safe_filename(value, default="item", max_length=80):
    """Create one filename component without path or control characters."""
    text = CONTROL_CHARS.sub("", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r'[\\/:*?"<>|#^\[\]]+', "", text).strip(" .")
    if text in {"", ".", ".."}:
        text = default
    return text[:max_length].rstrip(" .") or default


def safe_vault_path(vault, *parts):
    """Join a vault path and reject lexical or symlink escapes."""
    raw_root = str(vault or "")
    if not raw_root.strip():
        raise ValueError("vault path is empty")
    root = os.path.abspath(os.path.expanduser(raw_root))
    candidate = os.path.abspath(os.path.join(root, *(str(part) for part in parts)))
    real_root = os.path.realpath(root)
    real_candidate = os.path.realpath(candidate)
    try:
        lexical_common = os.path.commonpath([root, candidate])
        resolved_common = os.path.commonpath([real_root, real_candidate])
    except ValueError as exc:
        raise ValueError("path is outside the vault") from exc
    if lexical_common != root or resolved_common != real_root:
        raise ValueError("path is outside the vault")
    return candidate


def split_frontmatter_text(content):
    """Return raw YAML and body using delimiter-only frontmatter boundaries."""
    text = str(content or "")
    match = FRONTMATTER_BLOCK.match(text)
    if not match:
        return None, None
    return match.group(1), text[match.end():]


def strip_markdown_code_blocks(text):
    """Remove fenced and indented Markdown code before parsing machine tags."""
    output = []
    fence_char = ""
    fence_length = 0
    for line in str(text or "").splitlines(keepends=True):
        if fence_char:
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
            continue
        if line.startswith("\t") or re.match(r"^ {4,}\S", line):
            continue
        output.append(line)
    return "".join(output)


def strip_platform_injected_context(text):
    """Remove agent/runtime metadata embedded in transcript user messages."""
    cleaned = str(text or "")
    cleaned = re.sub(
        r"<\s*subagent_notification(?:\s[^>]*)?>.*?(?:</\s*subagent_notification\s*>|\Z)",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )
    for tag in PLATFORM_CONTEXT_TAGS:
        escaped = re.escape(tag)
        cleaned = re.sub(
            rf"<\s*{escaped}(?:\s[^>]*)?>.*?</\s*{escaped}\s*>",
            "",
            cleaned,
            flags=re.IGNORECASE | re.DOTALL,
        )
    cleaned = re.sub(
        r"(?im)^\s*#\s*(?:AGENTS\.md|CLAUDE\.md)\s+instructions\s*$",
        "",
        cleaned,
    )
    return cleaned.strip()


def stable_source_ids(record):
    """Return every persisted session identity, including legacy source rows."""
    source_ids = [
        str(value).strip()
        for value in (record.get("source_ids") or [])
        if str(value).strip()
    ]
    for source in record.get("sources") or []:
        if not isinstance(source, dict):
            continue
        session_id = str(source.get("session_id") or "").strip()
        if session_id:
            source_ids.append(session_id)
    return list(dict.fromkeys(source_ids))


def has_stable_source(record, session_id):
    session_id = str(session_id or "").strip()
    return bool(session_id) and session_id in stable_source_ids(record)


def add_stable_source(record, session_id, date_str, display_limit=10):
    """Persist durable source IDs while bounding the human-facing source rows."""
    session_id = str(session_id or "").strip()
    source_ids = stable_source_ids(record)
    if session_id and session_id not in source_ids:
        source_ids.append(session_id)
    record["source_ids"] = source_ids

    sources = [
        source
        for source in (record.get("sources") or [])
        if isinstance(source, dict)
    ]
    if session_id and not any(
        str(source.get("session_id") or "") == session_id
        for source in sources
    ):
        sources.append({"session_id": session_id, "date": str(date_str or "")})
    record["sources"] = sources[-int(display_limit or 10):]
    return record


def redact_sensitive(text):
    """Redact common credentials, private keys, and payment-card values."""
    text = str(text or "")
    text = re.sub(
        r"-----BEGIN [^-\n]*PRIVATE KEY-----.*?-----END [^-\n]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    label = (
        r"(?:[A-Za-z0-9_.-]*(?:api[ _-]?key|auth[ _-]?token|oauth|token|"
        r"password|passwd|secret|authorization|credential|cvv|cvc)[A-Za-z0-9_.-]*|"
        r"api\s+key|access\s+key|密码|口令|令牌|密钥|验证码|支付密码|"
        r"银行卡号|信用卡号|卡号)"
    )
    value = r'(?:"[^"\n]*"|\'[^\'\n]*\'|[^\s,，。;；、]+)'
    assignment = re.compile(
        rf"(?P<label>{label})(?P<sep>\s*(?::|=|：|\bis\b|是)\s*)(?P<value>{value})",
        re.IGNORECASE,
    )
    text = assignment.sub(
        lambda match: f"{match.group('label')}{match.group('sep')}[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}",
        "Bearer [REDACTED]",
        text,
    )
    text = re.sub(
        r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
        r"github_pat_[A-Za-z0-9_]{12,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
        r"AKIA[0-9A-Z]{16})\b",
        "[REDACTED]",
        text,
    )
    text = re.sub(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
        "[REDACTED JWT]",
        text,
    )
    return _redact_luhn_card_numbers(text)


def _redact_luhn_card_numbers(text):
    pattern = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")

    def replace(match):
        digits = re.sub(r"\D", "", match.group(0))
        if 13 <= len(digits) <= 19 and _passes_luhn(digits):
            return "[REDACTED CARD]"
        return match.group(0)

    return pattern.sub(replace, text)


def _passes_luhn(digits):
    total = 0
    parity = len(digits) % 2
    for index, char in enumerate(digits):
        number = int(char)
        if index % 2 == parity:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0
