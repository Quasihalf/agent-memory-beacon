"""Agent-neutral runtime for selective long-task memory refresh."""
from __future__ import annotations

import hashlib
import json
import math
import os
import posixpath
import re
import secrets
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Mapping

from knowledge_index import extract_terms
from memory_effectiveness import (
    build_exposure_event,
    build_feedback_event,
    is_valid_effectiveness_event,
)
from memory_recall import (
    GRAPH_FILENAME,
    infer_inspiration_intent,
    load_recall_index,
    prepare_recall_index,
    recall,
)
from memory_schema import canonical_project, is_valid_memory_id, runtime_source_path

try:
    import fcntl
except ImportError:  # pragma: no cover - Phase B targets macOS.
    fcntl = None


SHORT_CONFIRMATIONS = frozenset(
    {
        "好",
        "好的",
        "可以",
        "可以的",
        "行",
        "继续",
        "是",
        "是的",
        "嗯",
        "同意",
        "没问题",
        "谢谢",
        "完成",
        "ok",
        "okay",
        "yes",
        "continue",
        "sure",
    }
)
WEAK_TOPIC_TERMS = frozenset(
    {
        "一下",
        "这个",
        "那个",
        "帮我",
        "看看",
        "检查",
        "继续",
        "程序",
        "修改",
        "可以",
        "好的",
        "please",
        "check",
        "continue",
    }
)
RISK_OR_ERROR_PATTERN = re.compile(
    r"(?:"
    r"删除|清空|覆盖|迁移|回滚|提交|推送|发布|安装|卸载|"
    r"账号|权限|凭据|密码|数据库|修复|失败|报错|错误|异常|超时|"
    r"连接失败|无法|丢失|消失|崩溃|重连|"
    r"\b(?:delete|remove|drop|overwrite|migrate|rollback|commit|push|"
    r"publish|release|install|uninstall|credential|permission|database|"
    r"fail(?:ed|ure)?|error|exception|timeout|crash|reconnect)\b"
    r")",
    re.IGNORECASE,
)
RUNTIME_LABELS = {
    "workflow": "WORKFLOW",
    "skill": "SKILL",
    "preference": "PREFERENCE",
    "project_rule": "PREFERENCE",
    "environment": "PREFERENCE",
    "decision": "DECISION",
    "error": "ERROR",
    "insight": "INSIGHT",
}
RUNTIME_RELATIVE_SCORE_THRESHOLD = 0.8
MAX_INSIGHT_AUTO_RECALL = 2
MAX_INSIGHT_TOKEN_BUDGET = 400
LOW_CONFIDENCE_INSIGHT_THRESHOLD = 0.8
INSIGHT_EXPLORATION_TYPE_BOOST = 6
STATE_ALLOWED_FIELDS = frozenset(
    {
        "schema_version",
        "session_hash",
        "initialized_at",
        "last_seen_index_version",
        "pending_index_change",
        "last_substantive_at",
        "topic_term_weights",
        "recently_loaded",
        "last_evaluated_index_version",
        "last_refresh_attempt_at",
        "last_recalled_index_version",
        "last_recall_at",
        "pending_effectiveness",
    }
)
STATE_TIME_FIELDS = frozenset(
    {
        "initialized_at",
        "last_substantive_at",
        "last_refresh_attempt_at",
        "last_recall_at",
    }
)
STATE_VERSION_FIELDS = frozenset(
    {
        "last_seen_index_version",
        "last_evaluated_index_version",
        "last_recalled_index_version",
    }
)


@dataclass(frozen=True)
class PromptEvent:
    session_key: str
    prompt: str
    cwd: str = ""
    event_name: str = "UserPromptSubmit"
    agent: str = "codex"


@dataclass(frozen=True)
class RuntimePolicy:
    stale_after_minutes: int = 30
    duplicate_suppression_minutes: int = 60
    topic_similarity_threshold: float = 0.25
    topic_min_terms: int = 3
    max_first_prompt: int = 8
    max_refresh: int = 6
    max_risk_or_error: int = 10
    token_budget: int = 1500
    internal_deadline_ms: int = 1800

    @classmethod
    def from_config(cls, config: Mapping[str, object]) -> "RuntimePolicy":
        return cls(
            stale_after_minutes=int(config.get("stale_after_minutes", 30)),
            duplicate_suppression_minutes=int(
                config.get("duplicate_suppression_minutes", 60)
            ),
            topic_similarity_threshold=float(
                config.get("topic_similarity_threshold", 0.25)
            ),
            topic_min_terms=int(config.get("topic_min_terms", 3)),
            max_first_prompt=int(config.get("max_first_prompt", 8)),
            max_refresh=int(config.get("max_refresh", 6)),
            max_risk_or_error=int(config.get("max_risk_or_error", 10)),
            token_budget=int(config.get("token_budget", 1500)),
            internal_deadline_ms=int(config.get("internal_deadline_ms", 1800)),
        )


@dataclass(frozen=True)
class TriggerDecision:
    triggered: bool
    primary_reason: str = ""
    reasons: tuple[str, ...] = ()
    substantive: bool = False
    risk_or_error: bool = False
    topic_hashes: dict[str, float] = field(default_factory=dict)
    pending_index_change: bool = False


@dataclass(frozen=True)
class HookResult:
    additional_context: str = ""
    status: str = "silent"
    trigger: str = ""
    loaded: int = 0
    estimated_tokens: int = 0
    session_hash: str = ""


class StateLockUnavailable(RuntimeError):
    """Raised when a per-session state lock cannot be acquired in budget."""


class RuntimeDeadlineExceeded(RuntimeError):
    """Raised internally when the Hook's fail-open time budget is exhausted."""


class PinnedVaultDirectory:
    """Descriptor-pinned access to one directory below a trusted Vault root."""

    def __init__(self, vault_root, directory, *, create=False, private=False):
        self.root_path = os.path.abspath(os.path.expanduser(os.fspath(vault_root)))
        self.directory_path = os.path.abspath(os.path.expanduser(os.fspath(directory)))
        try:
            if os.path.commonpath([self.root_path, self.directory_path]) != self.root_path:
                raise ValueError("runtime path is outside the Vault")
        except ValueError as exc:
            raise ValueError("runtime path is outside the Vault") from exc
        root_metadata = os.stat(self.root_path, follow_symlinks=False)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise ValueError("Vault root must be a real directory")
        self.root_identity = (root_metadata.st_dev, root_metadata.st_ino)
        self.root_fd = -1
        self.fd = -1
        try:
            self.root_fd = os.open(self.root_path, self._directory_flags())
            opened_root_metadata = os.fstat(self.root_fd)
            if (
                not stat.S_ISDIR(opened_root_metadata.st_mode)
                or (opened_root_metadata.st_dev, opened_root_metadata.st_ino)
                != self.root_identity
            ):
                raise OSError("Vault root identity changed")
            relative = os.path.relpath(self.directory_path, self.root_path)
            self.parts = () if relative == "." else tuple(Path(relative).parts)
            if any(part in {"", ".", ".."} for part in self.parts):
                raise ValueError("runtime directory has unsafe components")
            self.fd = self._open_relative(self.parts, create=create)
            if private:
                os.fchmod(self.fd, 0o700)
            metadata = os.fstat(self.fd)
            self.identity = (metadata.st_dev, metadata.st_ino)
            self.verify()
        except Exception:
            self.close()
            raise

    @staticmethod
    def _directory_flags():
        return (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )

    def _open_relative(self, parts, *, create=False):
        current_fd = os.dup(self.root_fd)
        try:
            for part in parts:
                try:
                    next_fd = os.open(part, self._directory_flags(), dir_fd=current_fd)
                except FileNotFoundError:
                    if not create:
                        raise
                    os.mkdir(part, mode=0o700, dir_fd=current_fd)
                    next_fd = os.open(part, self._directory_flags(), dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            return current_fd
        except Exception:
            os.close(current_fd)
            raise

    def verify(self):
        root_metadata = os.stat(self.root_path, follow_symlinks=False)
        pinned_root_metadata = os.fstat(self.root_fd)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or (root_metadata.st_dev, root_metadata.st_ino) != self.root_identity
            or not stat.S_ISDIR(pinned_root_metadata.st_mode)
            or (pinned_root_metadata.st_dev, pinned_root_metadata.st_ino)
            != self.root_identity
        ):
            raise OSError("Vault root identity changed")
        current_fd = self._open_relative(self.parts, create=False)
        try:
            metadata = os.fstat(current_fd)
            if (metadata.st_dev, metadata.st_ino) != self.identity:
                raise OSError("runtime directory identity changed")
        finally:
            os.close(current_fd)

    def stat_file(self, name):
        self._validate_leaf(name)
        self.verify()
        metadata = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("runtime leaf must be a regular file")
        return metadata

    def read_json(self, name, *, max_bytes):
        descriptor = self.open_file(name, os.O_RDONLY)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > max_bytes:
                raise ValueError("runtime JSON file is invalid or oversized")
            chunks = []
            remaining = max_bytes + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > max_bytes:
                raise ValueError("runtime JSON file is oversized")
            return json.loads(payload.decode("utf-8"))
        finally:
            os.close(descriptor)

    def open_file(self, name, flags, mode=0o600):
        self._validate_leaf(name)
        self.verify()
        return os.open(
            name,
            flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            mode,
            dir_fd=self.fd,
        )

    def atomic_write_json(self, name, payload):
        self._validate_leaf(name)
        temporary = f".{name}.{secrets.token_hex(8)}.tmp"
        descriptor = self.open_file(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            os.fchmod(descriptor, 0o600)
            data = (
                json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
            ).encode("utf-8")
            view = memoryview(data)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
            self.verify()
            os.replace(
                temporary,
                name,
                src_dir_fd=self.fd,
                dst_dir_fd=self.fd,
            )
            final_descriptor = self.open_file(name, os.O_RDONLY)
            try:
                os.fchmod(final_descriptor, 0o600)
            finally:
                os.close(final_descriptor)
        finally:
            os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def replace(self, source, target):
        self._validate_leaf(source)
        self._validate_leaf(target)
        self.verify()
        os.replace(source, target, src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def unlink(self, name, *, missing_ok=False):
        self._validate_leaf(name)
        self.verify()
        try:
            os.unlink(name, dir_fd=self.fd)
        except FileNotFoundError:
            if not missing_ok:
                raise

    @staticmethod
    def _validate_leaf(name):
        if (
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
            or re.search(r"[\x00-\x1f\x7f]", name)
        ):
            raise ValueError("unsafe runtime leaf name")

    def close(self):
        if getattr(self, "fd", -1) >= 0:
            os.close(self.fd)
            self.fd = -1
        if getattr(self, "root_fd", -1) >= 0:
            os.close(self.root_fd)
            self.root_fd = -1

    def __del__(self):
        self.close()


class JsonStateStore:
    def __init__(
        self,
        state_dir: str | os.PathLike[str],
        *,
        max_state_bytes: int = 1024 * 1024,
        monotonic=time.monotonic,
        sleeper=time.sleep,
        vault_root=None,
    ):
        self.state_dir = Path(state_dir)
        self.max_state_bytes = int(max_state_bytes)
        self.monotonic = monotonic
        self.sleeper = sleeper
        self._state_pin = None
        self._lock_pin = None
        if vault_root is not None:
            self._state_pin = PinnedVaultDirectory(
                vault_root,
                self.state_dir,
                create=True,
                private=True,
            )
            self._lock_pin = PinnedVaultDirectory(
                vault_root,
                self.state_dir / ".locks",
                create=True,
                private=True,
            )
        else:
            self._ensure_private_directory(self.state_dir)

    def state_path(self, session_hash: str) -> Path:
        self._validate_session_hash(session_hash)
        return self.state_dir / f"{session_hash}.json"

    def load(self, session_hash: str) -> dict:
        path = self.state_path(session_hash)
        try:
            if self._state_pin is not None:
                state = self._state_pin.read_json(
                    path.name,
                    max_bytes=self.max_state_bytes,
                )
            else:
                if not path.exists() and not path.is_symlink():
                    return {}
                metadata = path.lstat()
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size > self.max_state_bytes
                ):
                    raise ValueError("invalid state file")
                with path.open("r", encoding="utf-8") as handle:
                    state = json.load(handle)
            _validate_state_payload(state, session_hash)
            return state
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
            self._quarantine(path)
            return {}

    def save(self, session_hash: str, state: Mapping[str, object]) -> None:
        path = self.state_path(session_hash)
        if not isinstance(state, Mapping):
            raise TypeError("state must be a mapping")
        payload = dict(state)
        _validate_state_payload(payload, session_hash)

        if self._state_pin is not None:
            self._state_pin.atomic_write_json(path.name, payload)
            return

        self._ensure_private_directory(self.state_dir)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{session_hash}.",
            suffix=".tmp",
            dir=self.state_dir,
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                descriptor = -1
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @contextmanager
    def locked(self, session_hash: str, *, deadline: float):
        self._validate_session_hash(session_hash)
        lock_dir = self.state_dir / ".locks"
        lock_path = lock_dir / f"{session_hash}.lock"
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            if self._lock_pin is not None:
                descriptor = self._lock_pin.open_file(lock_path.name, flags, 0o600)
            else:
                self._ensure_private_directory(lock_dir)
                descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise StateLockUnavailable("state lock is unavailable") from exc
        acquired = False
        try:
            os.fchmod(descriptor, 0o600)
            if fcntl is None:  # pragma: no cover - macOS production has fcntl.
                acquired = True
            else:
                while self.monotonic() < deadline:
                    try:
                        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except BlockingIOError:
                        self.sleeper(min(0.005, max(0.0, deadline - self.monotonic())))
            if not acquired:
                raise StateLockUnavailable("state lock deadline exceeded")
            yield
        finally:
            if acquired and fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _validate_session_hash(session_hash: str) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", str(session_hash or "")):
            raise ValueError("invalid session hash")

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise ValueError("state directory must be a real directory")
        os.chmod(path, 0o700)

    def _quarantine(self, path: Path) -> None:
        try:
            suffix = f".corrupt-{time.time_ns()}"
            target = path.stem + suffix
            if self._state_pin is not None:
                self._state_pin.replace(path.name, target)
            else:
                os.replace(path, path.with_name(target))
        except OSError:
            pass


class FileIndexStore:
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        vault_root=None,
        max_index_bytes=64 * 1024 * 1024,
        max_graph_bytes=64 * 1024 * 1024,
    ):
        self.path = Path(path)
        self.max_index_bytes = int(max_index_bytes)
        self.max_graph_bytes = int(max_graph_bytes)
        self._parent_pin = (
            PinnedVaultDirectory(vault_root, self.path.parent)
            if vault_root is not None
            else None
        )

    def version(self) -> tuple[int, int, int, int]:
        if self._parent_pin is not None:
            metadata = self._parent_pin.stat_file(self.path.name)
            return (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            )
        return index_version(self.path)

    def load(self) -> dict:
        if self._parent_pin is not None:
            data = self._parent_pin.read_json(
                self.path.name,
                max_bytes=self.max_index_bytes,
            )
            graph = None
            try:
                graph = self._parent_pin.read_json(
                    GRAPH_FILENAME,
                    max_bytes=self.max_graph_bytes,
                )
            except FileNotFoundError:
                pass
            return prepare_recall_index(data, path=self.path, graph=graph)
        return load_recall_index(self.path)


class PrivacyLogger:
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        max_bytes: int = 10 * 1024 * 1024,
        max_backups: int = 5,
        monotonic=time.monotonic,
        vault_root=None,
    ):
        self.path = Path(path)
        self.max_bytes = int(max_bytes)
        self.max_backups = int(max_backups)
        self.monotonic = monotonic
        self._parent_pin = (
            PinnedVaultDirectory(
                vault_root,
                self.path.parent,
                create=True,
                private=True,
            )
            if vault_root is not None
            else None
        )

    def append(self, record: Mapping[str, object], *, deadline: float) -> bool:
        if self.monotonic() >= deadline:
            return False
        if self._parent_pin is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_name(f".{self.path.name}.lock")
        lock_flags = os.O_RDWR | os.O_CREAT
        lock_flags |= getattr(os, "O_CLOEXEC", 0)
        lock_flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            if self._parent_pin is not None:
                lock_descriptor = self._parent_pin.open_file(
                    lock_path.name,
                    lock_flags,
                    0o600,
                )
            else:
                lock_descriptor = os.open(lock_path, lock_flags, 0o600)
        except (OSError, ValueError):
            return False
        acquired = False
        try:
            os.fchmod(lock_descriptor, 0o600)
            if fcntl is not None:
                try:
                    fcntl.flock(
                        lock_descriptor,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    acquired = True
                except BlockingIOError:
                    return False
            else:  # pragma: no cover - macOS production has fcntl.
                acquired = True
            if self.monotonic() >= deadline:
                return False
            line = (
                json.dumps(dict(record), ensure_ascii=False, sort_keys=True) + "\n"
            ).encode("utf-8")
            self._rotate_if_needed(len(line))
            flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            if self._parent_pin is not None:
                descriptor = self._parent_pin.open_file(
                    self.path.name,
                    flags,
                    0o600,
                )
            else:
                descriptor = os.open(self.path, flags, 0o600)
            try:
                os.fchmod(descriptor, 0o600)
                view = memoryview(line)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
            finally:
                os.close(descriptor)
            return True
        except (OSError, ValueError):
            return False
        finally:
            if acquired and fcntl is not None:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
            os.close(lock_descriptor)

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        if self._parent_pin is not None:
            self._rotate_pinned(incoming_bytes)
            return
        try:
            current_size = self.path.lstat().st_size
            if self.path.is_symlink() or not stat.S_ISREG(self.path.lstat().st_mode):
                raise OSError("log path is not a regular file")
        except FileNotFoundError:
            current_size = 0
        if current_size + incoming_bytes <= self.max_bytes:
            return
        if self.max_backups <= 0:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.max_backups}")
        try:
            oldest.unlink()
        except FileNotFoundError:
            pass
        for number in range(self.max_backups - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{number}")
            target = self.path.with_name(f"{self.path.name}.{number + 1}")
            try:
                os.replace(source, target)
            except FileNotFoundError:
                pass
        try:
            os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))
        except FileNotFoundError:
            pass

    def _rotate_pinned(self, incoming_bytes: int) -> None:
        try:
            current_size = self._parent_pin.stat_file(self.path.name).st_size
        except FileNotFoundError:
            current_size = 0
        if current_size + incoming_bytes <= self.max_bytes:
            return
        if self.max_backups <= 0:
            self._parent_pin.unlink(self.path.name, missing_ok=True)
            return
        self._parent_pin.unlink(
            f"{self.path.name}.{self.max_backups}",
            missing_ok=True,
        )
        for number in range(self.max_backups - 1, 0, -1):
            try:
                self._parent_pin.replace(
                    f"{self.path.name}.{number}",
                    f"{self.path.name}.{number + 1}",
                )
            except FileNotFoundError:
                pass
        try:
            self._parent_pin.replace(self.path.name, f"{self.path.name}.1")
        except FileNotFoundError:
            pass


def hash_session_key(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:32]


def _validate_state_payload(state: object, session_hash: str) -> None:
    if not isinstance(state, dict):
        raise ValueError("invalid state payload")
    if set(state) - STATE_ALLOWED_FIELDS:
        raise ValueError("state contains unsupported fields")
    if state.get("schema_version") != 1 or state.get("session_hash") != session_hash:
        raise ValueError("state identity does not match session hash")
    if not re.fullmatch(r"[0-9a-f]{32}", str(session_hash or "")):
        raise ValueError("invalid state session hash")

    for field in STATE_TIME_FIELDS:
        if field not in state:
            if field == "initialized_at":
                raise ValueError("state initialized_at is required")
            continue
        parsed = _parse_datetime(state.get(field))
        if parsed is None or parsed.utcoffset() is None:
            raise ValueError(f"state {field} must be a timezone-aware timestamp")

    for field in STATE_VERSION_FIELDS:
        if field not in state:
            continue
        version = state.get(field)
        if not (
            isinstance(version, list)
            and len(version) == 4
            and all(
                not isinstance(value, bool)
                and isinstance(value, int)
                and 0 <= value <= (2**64 - 1)
                for value in version
            )
        ):
            raise ValueError(f"state {field} must contain four bounded integers")

    if "pending_index_change" in state and not isinstance(
        state.get("pending_index_change"), bool
    ):
        raise ValueError("state pending_index_change must be a boolean")

    topic_weights = state.get("topic_term_weights")
    if not isinstance(topic_weights, dict) or len(topic_weights) > 64:
        raise ValueError("state topic_term_weights must be a bounded mapping")
    for term_hash, weight in topic_weights.items():
        if not re.fullmatch(r"[0-9a-f]{64}", str(term_hash or "")):
            raise ValueError("state topic keys must be SHA-256 hashes")
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or not math.isfinite(float(weight))
            or float(weight) < 0
        ):
            raise ValueError("state topic weights must be finite nonnegative numbers")

    recently_loaded = state.get("recently_loaded")
    if not isinstance(recently_loaded, dict) or len(recently_loaded) > 128:
        raise ValueError("state recently_loaded must be a bounded mapping")
    for memory_id, loaded in recently_loaded.items():
        if not is_valid_memory_id(memory_id):
            raise ValueError("state contains an invalid memory ID")
        if not isinstance(loaded, dict) or set(loaded) != {"revision", "loaded_at"}:
            raise ValueError("state contains an invalid suppression record")
        if not re.fullmatch(r"[0-9a-f]{64}", str(loaded.get("revision") or "")):
            raise ValueError("state contains an invalid memory revision")
        loaded_at = _parse_datetime(loaded.get("loaded_at"))
        if loaded_at is None or loaded_at.utcoffset() is None:
            raise ValueError("state contains an invalid suppression timestamp")

    pending_effectiveness = state.get("pending_effectiveness")
    if pending_effectiveness is not None:
        if not is_valid_effectiveness_event(
            pending_effectiveness,
            event_kind="exposure",
        ):
            raise ValueError("state contains an invalid pending effectiveness event")
        if pending_effectiveness.get("session_hash") != session_hash:
            raise ValueError("pending effectiveness session does not match state")
        if len(pending_effectiveness.get("memories") or []) > 12:
            raise ValueError("pending effectiveness event contains too many memories")
        if len(json.dumps(pending_effectiveness, ensure_ascii=False)) > 16 * 1024:
            raise ValueError("pending effectiveness event is too large")


def index_version(path: str | os.PathLike[str]) -> tuple[int, int, int, int]:
    stat = os.stat(Path(path))
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)


def topic_signature(prompt: str) -> dict[str, float]:
    terms = [
        term
        for term in extract_terms(prompt, limit=40)
        if term not in WEAK_TOPIC_TERMS
    ]
    return {
        hashlib.sha256(term.encode("utf-8")).hexdigest(): 1.0
        for term in terms
    }


def decide_trigger(
    event: PromptEvent,
    state: Mapping[str, object],
    current_index_version: tuple[int, int, int, int],
    policy: RuntimePolicy,
    now: datetime,
) -> TriggerDecision:
    prompt = str(event.prompt or "").strip()
    substantive = _is_substantive(prompt)
    previous_version = tuple(state.get("last_evaluated_index_version") or ())
    version_changed = bool(previous_version) and (
        previous_version != tuple(current_index_version)
    )
    pending_index_change = bool(state.get("pending_index_change")) or version_changed

    if not substantive:
        return TriggerDecision(
            triggered=False,
            substantive=False,
            pending_index_change=pending_index_change,
        )

    signature = topic_signature(prompt)
    risk_or_error = bool(RISK_OR_ERROR_PATTERN.search(prompt))
    reasons = []

    if not state.get("initialized_at"):
        reasons.append("first_prompt")
    elif pending_index_change:
        reasons.append("index_changed")

    if risk_or_error:
        reasons.append("risk_or_error")

    previous_signature = state.get("topic_term_weights") or {}
    if (
        state.get("initialized_at")
        and len(signature) >= policy.topic_min_terms
        and len(previous_signature) >= policy.topic_min_terms
        and _weighted_jaccard(signature, previous_signature)
        < policy.topic_similarity_threshold
    ):
        reasons.append("topic_changed")

    refreshed_at = _parse_datetime(state.get("last_refresh_attempt_at"))
    if (
        state.get("initialized_at")
        and (
            refreshed_at is None
            or now - refreshed_at >= timedelta(minutes=policy.stale_after_minutes)
        )
    ):
        reasons.append("stale_30m")

    priority = (
        "first_prompt",
        "index_changed",
        "risk_or_error",
        "topic_changed",
        "stale_30m",
    )
    primary_reason = next((reason for reason in priority if reason in reasons), "")
    return TriggerDecision(
        triggered=bool(primary_reason),
        primary_reason=primary_reason,
        reasons=tuple(reasons),
        substantive=True,
        risk_or_error=risk_or_error,
        topic_hashes=signature,
        pending_index_change=pending_index_change,
    )


def handle_prompt(
    event: PromptEvent,
    config: Mapping[str, object],
    *,
    clock=lambda: datetime.now().astimezone(),
    monotonic=time.monotonic,
    index_store=None,
    state_store=None,
) -> HookResult:
    runtime_config = config.get("memory_runtime") if isinstance(config, Mapping) else None
    if not isinstance(runtime_config, Mapping):
        return HookResult(status="config_error")
    if not runtime_config.get("enabled", True):
        return HookResult(status="disabled")
    if not isinstance(event, PromptEvent) or not str(event.session_key or "").strip():
        return HookResult(status="missing_session")

    session_hash = hash_session_key(event.session_key)
    started = monotonic()
    deadline = started + (
        int(runtime_config.get("internal_deadline_ms", 1800)) / 1000
    )
    now = clock()
    current_version = ()
    decision = TriggerDecision(False)
    candidates = []
    selected = []
    stage = "start"
    result = HookResult(status="silent", session_hash=session_hash)

    try:
        _check_deadline(monotonic, deadline)
        index_store = index_store or FileIndexStore(
            runtime_config["resolved_index_path"],
            vault_root=config.get("vault_path"),
        )
        stage = "index_version"
        current_version = tuple(index_store.version())
        if len(current_version) != 4:
            raise ValueError("invalid index version")
        _check_deadline(monotonic, deadline)

        policy = RuntimePolicy.from_config(runtime_config)
        state_store = state_store or JsonStateStore(
            runtime_config["resolved_state_dir"],
            monotonic=monotonic,
            vault_root=config.get("vault_path"),
        )
        stage = "state_lock"
        with state_store.locked(session_hash, deadline=deadline):
            stage = "state_load"
            state = state_store.load(session_hash)
            effectiveness_logger = _effectiveness_logger(
                config,
                monotonic=monotonic,
            )
            state = _close_pending_effectiveness(
                state,
                event.prompt,
                config,
                now,
                effectiveness_logger,
                deadline,
            )
            decision = decide_trigger(
                event,
                state,
                current_version,
                policy,
                now,
            )

            if not decision.substantive:
                if state:
                    updated = dict(state)
                    updated["last_seen_index_version"] = list(current_version)
                    updated["pending_index_change"] = decision.pending_index_change
                    _check_deadline(monotonic, deadline)
                    stage = "state_write"
                    state_store.save(session_hash, updated)
                result = HookResult(status="silent", session_hash=session_hash)
            elif not decision.triggered:
                updated = _observe_state(
                    state,
                    session_hash,
                    decision,
                    current_version,
                    now,
                )
                _check_deadline(monotonic, deadline)
                stage = "state_write"
                state_store.save(session_hash, updated)
                result = HookResult(status="silent", session_hash=session_hash)
            else:
                _check_deadline(monotonic, deadline)
                stage = "index_load"
                index = index_store.load()
                _check_deadline(monotonic, deadline)
                stage = "retrieve"
                candidates = retrieve_memories(
                    event,
                    index,
                    state,
                    decision,
                    policy,
                    config,
                    now=now,
                )
                _check_deadline(monotonic, deadline)
                rendered, selected = _render_refresh_details(
                    decision,
                    candidates,
                    policy.token_budget,
                )
                estimated_tokens = estimate_tokens(rendered) if rendered else 0
                updated = _evaluated_state(
                    state,
                    session_hash,
                    decision,
                    current_version,
                    now,
                    selected,
                    policy,
                )
                exposure = _append_effectiveness_exposure(
                    selected,
                    session_hash=session_hash,
                    trigger=decision.primary_reason,
                    timestamp=now,
                    duration_ms=max(0.0, monotonic() - started) * 1000,
                    estimated_tokens=estimated_tokens,
                    logger=effectiveness_logger,
                    deadline=deadline,
                )
                if exposure is not None:
                    updated["pending_effectiveness"] = exposure
                else:
                    updated.pop("pending_effectiveness", None)
                _check_deadline(monotonic, deadline)
                stage = "state_write"
                state_store.save(session_hash, updated)
                _check_deadline(monotonic, deadline)
                result = HookResult(
                    additional_context=rendered,
                    status="success" if rendered else "no_match",
                    trigger=decision.primary_reason,
                    loaded=len(selected),
                    estimated_tokens=estimated_tokens,
                    session_hash=session_hash,
                )
    except RuntimeDeadlineExceeded:
        result = HookResult(status="timeout", session_hash=session_hash)
    except StateLockUnavailable:
        result = HookResult(status="lock_busy", session_hash=session_hash)
    except Exception:
        status = "invalid_index" if stage in {"index_version", "index_load"} else "error"
        result = HookResult(status=status, session_hash=session_hash)

    _append_privacy_log(
        runtime_config,
        config.get("vault_path"),
        event,
        result,
        decision,
        current_version,
        candidates,
        selected,
        now,
        started,
        deadline,
        monotonic,
    )
    return result


def resolve_project(
    cwd: str,
    prompt: str,
    config: Mapping[str, object],
    *,
    known_projects=(),
) -> str:
    aliases_by_project: dict[str, set[str]] = {}
    configured_projects = config.get("projects") or []
    if isinstance(configured_projects, (list, tuple, set)):
        for value in configured_projects:
            raw_project = value.get("name") if isinstance(value, Mapping) else value
            project = canonical_project(raw_project)
            if project:
                aliases = aliases_by_project.setdefault(project, set())
                aliases.add(project.casefold())
                if isinstance(raw_project, str):
                    aliases.add(raw_project.casefold())
                raw_aliases = value.get("keywords", ()) if isinstance(value, Mapping) else ()
                if isinstance(raw_aliases, (list, tuple, set)):
                    aliases.update(
                        str(alias).strip().casefold()
                        for alias in raw_aliases
                        if str(alias).strip()
                    )
    for value in known_projects or ():
        project = canonical_project(value)
        if project:
            aliases_by_project.setdefault(project, set()).add(project.casefold())

    configured_keywords = config.get("project_keywords") or {}
    if isinstance(configured_keywords, Mapping):
        for raw_project, raw_aliases in configured_keywords.items():
            project = canonical_project(raw_project)
            if not project:
                continue
            aliases = aliases_by_project.setdefault(project, set())
            aliases.add(str(raw_project).casefold())
            aliases.add(project.casefold())
            if isinstance(raw_aliases, (list, tuple, set)):
                aliases.update(
                    str(alias).strip().casefold()
                    for alias in raw_aliases
                    if str(alias).strip()
                )

    cwd_matches = _matching_projects(
        str(cwd or ""),
        aliases_by_project,
        allow_single_character=True,
    )
    if len(cwd_matches) == 1:
        return next(iter(cwd_matches))
    if len(cwd_matches) > 1:
        return ""

    prompt_matches = _matching_projects(
        str(prompt or ""),
        aliases_by_project,
        allow_single_character=False,
    )
    return next(iter(prompt_matches)) if len(prompt_matches) == 1 else ""


def retrieve_memories(
    event: PromptEvent,
    index: Mapping[str, object],
    state: Mapping[str, object],
    trigger: TriggerDecision,
    policy: RuntimePolicy,
    config: Mapping[str, object],
    *,
    now: datetime,
) -> list[dict]:
    indexed_projects = set()
    project_index = index.get("projects") if isinstance(index, Mapping) else None
    if isinstance(project_index, Mapping):
        indexed_projects.update(project_index)
    units = index.get("units") if isinstance(index, Mapping) else []
    if isinstance(units, list):
        indexed_projects.update(
            unit.get("project")
            for unit in units
            if isinstance(unit, Mapping) and unit.get("project")
        )
    project = resolve_project(
        event.cwd,
        event.prompt,
        config,
        known_projects=indexed_projects,
    )
    allowed_projects = {project} if project else set()
    insight_config = config.get("insight_memory") or {}
    insight_enabled = bool(insight_config.get("enabled", True))
    allow_insights = insight_enabled and infer_inspiration_intent(event.prompt)
    if trigger.risk_or_error or "risk_or_error" in trigger.reasons:
        limit = policy.max_risk_or_error
        type_boosts = {"error": 8, "workflow": 3, "decision": 1}
    elif trigger.primary_reason == "first_prompt":
        limit = policy.max_first_prompt
        type_boosts = {"workflow": 3, "skill": 2}
    else:
        limit = policy.max_refresh
        type_boosts = {"workflow": 2, "skill": 1}
    if allow_insights:
        type_boosts["insight"] = INSIGHT_EXPLORATION_TYPE_BOOST

    search_limit = min(
        len(units) if isinstance(units, list) else limit,
        max(limit * 4, limit),
    )
    ranked = recall(
        event.prompt,
        index,
        limit=search_limit,
        allowed_projects=allowed_projects,
        type_boosts=type_boosts,
        relative_score_threshold=RUNTIME_RELATIVE_SCORE_THRESHOLD,
    )
    max_insights = min(
        MAX_INSIGHT_AUTO_RECALL,
        max(0, int(insight_config.get("max_auto_recall", MAX_INSIGHT_AUTO_RECALL))),
    )
    insight_budget = min(
        MAX_INSIGHT_TOKEN_BUDGET,
        max(1, int(insight_config.get("recall_token_budget", MAX_INSIGHT_TOKEN_BUDGET))),
    )
    authority_results = []
    other_results = []
    insight_results = []
    insight_tokens = 0
    low_confidence_seed_count = 0
    for item in ranked:
        if _recently_loaded(item, state, policy, now):
            continue
        if item.get("type") == "insight":
            if not allow_insights or len(insight_results) >= max_insights:
                continue
            low_confidence_seed = bool(
                item.get("maturity") == "seed"
                and float(item.get("confidence") or 0)
                < LOW_CONFIDENCE_INSIGHT_THRESHOLD
            )
            if low_confidence_seed and low_confidence_seed_count >= 1:
                continue
            rendered = _render_memory_line(item)
            rendered_tokens = estimate_tokens(rendered)
            if not rendered or insight_tokens + rendered_tokens > insight_budget:
                continue
            insight_results.append(item)
            insight_tokens += rendered_tokens
            low_confidence_seed_count += int(low_confidence_seed)
            continue
        if item.get("type") in {"workflow", "decision", "error"}:
            authority_results.append(item)
        else:
            other_results.append(item)

    non_insights = [*authority_results, *other_results]
    if insight_results:
        non_insights = non_insights[: max(0, limit - len(insight_results))]
    return [*non_insights, *insight_results][:limit]


def estimate_tokens(text: str) -> int:
    text = str(text or "")
    cjk_count = len(re.findall(r"[\u3400-\u9fff]", text))
    non_cjk = re.sub(r"[\u3400-\u9fff\s]", "", text)
    return cjk_count + math.ceil(len(non_cjk) / 4)


def render_refresh(
    trigger: TriggerDecision,
    memories: list[Mapping[str, object]],
    token_budget: int,
) -> str:
    return _render_refresh_details(trigger, memories, token_budget)[0]


def _render_refresh_details(
    trigger: TriggerDecision,
    memories: list[Mapping[str, object]],
    token_budget: int,
) -> tuple[str, list[Mapping[str, object]]]:
    safe_budget = max(1, math.floor(int(token_budget) * 0.9))
    reason = trigger.primary_reason if trigger.primary_reason in {
        "first_prompt",
        "index_changed",
        "risk_or_error",
        "topic_changed",
        "stale_30m",
    } else "refresh"
    lines = []
    selected = []
    for memory in memories:
        line = _render_memory_line(memory)
        if not line:
            continue
        candidate = _assemble_refresh(reason, [*lines, line])
        if estimate_tokens(candidate) > safe_budget:
            break
        lines.append(line)
        selected.append(memory)
    if not lines:
        return "", []
    return _assemble_refresh(reason, lines), selected


def _matching_projects(
    text: str,
    aliases_by_project: Mapping[str, set[str]],
    *,
    allow_single_character: bool,
) -> set[str]:
    normalized = str(text or "").casefold()
    matches = set()
    for project, aliases in aliases_by_project.items():
        if any(
            _bounded_alias_match(normalized, alias, allow_single_character)
            for alias in aliases
        ):
            matches.add(project)
    return matches


def _bounded_alias_match(text: str, alias: str, allow_single_character: bool) -> bool:
    alias = str(alias or "").strip().casefold()
    if not alias:
        return False
    effective = re.sub(r"[^\w]+", "", alias, flags=re.UNICODE)
    if not allow_single_character and len(effective) < 2:
        return False
    return bool(
        re.search(
            rf"(?<!\w){re.escape(alias)}(?!\w)",
            text,
            flags=re.UNICODE,
        )
    )


def _recently_loaded(
    item: Mapping[str, object],
    state: Mapping[str, object],
    policy: RuntimePolicy,
    now: datetime,
) -> bool:
    recent = state.get("recently_loaded") or {}
    if not isinstance(recent, Mapping):
        return False
    prior = recent.get(item.get("id"))
    if not isinstance(prior, Mapping):
        return False
    if prior.get("revision") != item.get("revision"):
        return False
    loaded_at = _parse_datetime(prior.get("loaded_at"))
    if loaded_at is None:
        return False
    try:
        age = now - loaded_at
    except TypeError:
        return False
    return age < timedelta(minutes=policy.duplicate_suppression_minutes)


def _render_memory_line(memory: Mapping[str, object]) -> str:
    label = RUNTIME_LABELS.get(str(memory.get("type") or ""))
    if not label:
        return ""
    title = _sanitize_memory_text(memory.get("title"), limit=160)
    summary = _sanitize_memory_text(
        memory.get("recall_summary") or memory.get("summary"),
        limit=360,
    )
    source = runtime_source_path(dict(memory))
    if not title or not summary or not source:
        return ""
    explanation = _render_memory_explanation(memory)
    if memory.get("type") == "insight":
        maturity = _sanitize_memory_text(memory.get("maturity"), limit=24)
        boundary = _sanitize_memory_text(memory.get("boundary"), limit=240)
        transfer = ", ".join(
            _sanitize_memory_text(item, limit=100)
            for item in memory.get("transfer") or []
            if _sanitize_memory_text(item, limit=100)
        )
        if not maturity or not boundary:
            return ""
        why_relevant = transfer or title
        return "\n".join(
            [
                "[INSIGHT]",
                f"idea: {summary}",
                f"maturity: {maturity}",
                f"why_relevant: {why_relevant}",
                f"boundary: {boundary}",
                *explanation,
                f"source: [[{source}]]",
                "[/INSIGHT]",
            ]
        )
    body = title if summary == title else f"{title} | {summary}"
    suffix = " | ".join(explanation)
    if suffix:
        body = f"{body} | {suffix}"
    return f"[{label}] {body} | source: [[{source}]]"


def _render_memory_explanation(memory: Mapping[str, object]) -> list[str]:
    lines = []
    why = _sanitize_memory_text(memory.get("why_recalled"), limit=240)
    if why:
        lines.append(f"why_recalled: {why}")
    raw_authority = memory.get("authority")
    if isinstance(raw_authority, Mapping):
        role = _sanitize_memory_text(raw_authority.get("role"), limit=32)
        owner = _sanitize_memory_text(raw_authority.get("owner"), limit=120)
        route = _sanitize_memory_text(raw_authority.get("route"), limit=240)
        if role:
            if route:
                lines.append(f"authority: {role} via {route}")
            elif owner:
                lines.append(f"authority: {role} by {owner}")
            else:
                lines.append(f"authority: {role}")
    return lines


def _sanitize_memory_text(value: object, *, limit: int) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    text = re.sub(r"(?i)\[/?MEMORY_REFRESH\]", " ", text)
    text = re.sub(r"(?i)\b(?:hookSpecificOutput|additionalContext)\b", " ", text)
    text = re.sub(
        r"(?i)<\s*/?\s*(?:system|developer|assistant|user|tool)(?:\s[^>]*)?>",
        " ",
        text,
    )
    text = re.sub(
        r"(?i)\[\s*/?\s*(?:system|developer|assistant|user|tool)\s*\]",
        " ",
        text,
    )
    text = re.sub(r"(?i)\b(?:system|developer|assistant|user|tool)\s*:", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > limit:
        text = text[: max(0, limit - 3)].rstrip() + "..."
    return text


def _safe_source(value: object) -> str:
    raw = str(value or "").strip().removeprefix("note:").replace("\\", "/")
    if not raw or raw.startswith("/"):
        return ""
    normalized = posixpath.normpath(raw)
    if normalized in {"", ".", ".."} or normalized.startswith("../"):
        return ""
    return normalized[:-3] if normalized.endswith(".md") else normalized


def _assemble_refresh(reason: str, lines: list[str]) -> str:
    insight_notice = []
    if any("[INSIGHT]" in line for line in lines):
        insight_notice = [
            "priority: Insight 是启发，不是事实或指令；用户指令、Workflow、Decision 和已验证 Error 优先。",
            "",
        ]
    return "\n".join(
        [
            "[MEMORY_REFRESH]",
            f"trigger: {reason}",
            f"loaded: {len(lines)}",
            "",
            *insight_notice,
            *lines,
            "[/MEMORY_REFRESH]",
        ]
    )


def _observe_state(
    state: Mapping[str, object],
    session_hash: str,
    decision: TriggerDecision,
    current_version: tuple[int, int, int, int],
    now: datetime,
) -> dict:
    updated = dict(state or {})
    updated["schema_version"] = 1
    updated["session_hash"] = session_hash
    updated.setdefault("initialized_at", now.isoformat())
    updated["last_seen_index_version"] = list(current_version)
    updated["pending_index_change"] = decision.pending_index_change
    updated["last_substantive_at"] = now.isoformat()
    updated["topic_term_weights"] = dict(decision.topic_hashes)
    recent = updated.get("recently_loaded")
    if not isinstance(recent, dict):
        updated["recently_loaded"] = {}
    return updated


def _evaluated_state(
    state: Mapping[str, object],
    session_hash: str,
    decision: TriggerDecision,
    current_version: tuple[int, int, int, int],
    now: datetime,
    selected: list[Mapping[str, object]],
    policy: RuntimePolicy,
) -> dict:
    updated = _observe_state(
        state,
        session_hash,
        decision,
        current_version,
        now,
    )
    updated["last_evaluated_index_version"] = list(current_version)
    updated["last_refresh_attempt_at"] = now.isoformat()
    updated["pending_index_change"] = False
    recent = _prune_recently_loaded(updated.get("recently_loaded"), policy, now)
    for memory in selected:
        memory_id = str(memory.get("id") or "")
        revision = str(memory.get("revision") or "")
        if memory_id and revision:
            recent[memory_id] = {
                "revision": revision,
                "loaded_at": now.isoformat(),
            }
    updated["recently_loaded"] = recent
    if selected:
        updated["last_recalled_index_version"] = list(current_version)
        updated["last_recall_at"] = now.isoformat()
    return updated


def _prune_recently_loaded(
    value: object,
    policy: RuntimePolicy,
    now: datetime,
) -> dict:
    if not isinstance(value, Mapping):
        return {}
    retained = {}
    for memory_id, record in value.items():
        if not isinstance(record, Mapping):
            continue
        loaded_at = _parse_datetime(record.get("loaded_at"))
        if loaded_at is None:
            continue
        try:
            age = now - loaded_at
        except TypeError:
            continue
        if age <= timedelta(minutes=policy.duplicate_suppression_minutes):
            retained[str(memory_id)] = {
                "revision": str(record.get("revision") or ""),
                "loaded_at": loaded_at.isoformat(),
            }
    ordered = sorted(
        retained.items(),
        key=lambda item: item[1]["loaded_at"],
        reverse=True,
    )[:128]
    return dict(ordered)


def _check_deadline(monotonic, deadline: float) -> None:
    if monotonic() >= deadline:
        raise RuntimeDeadlineExceeded("memory runtime deadline exceeded")


def _effectiveness_logger(config, *, monotonic):
    settings = config.get("memory_effectiveness") if isinstance(config, Mapping) else None
    if not isinstance(settings, Mapping) or not settings.get("enabled", True):
        return None
    path = settings.get("resolved_event_log_path")
    vault_root = config.get("vault_path")
    if not path or not vault_root:
        return None
    try:
        return PrivacyLogger(
            path,
            monotonic=monotonic,
            vault_root=vault_root,
        )
    except Exception:
        return None


def _close_pending_effectiveness(
    state,
    prompt,
    config,
    now,
    logger,
    deadline,
):
    updated = dict(state or {})
    pending = updated.pop("pending_effectiveness", None)
    if pending is None or logger is None:
        return updated
    try:
        settings = config.get("memory_effectiveness") or {}
        window = timedelta(minutes=int(settings.get("feedback_window_minutes", 15)))
        exposed_at = _parse_datetime(pending.get("timestamp"))
        age = now - exposed_at if exposed_at is not None else None
        feedback_prompt = prompt if age is not None and timedelta(0) <= age <= window else ""
        feedback = build_feedback_event(
            pending,
            feedback_prompt,
            timestamp=now.isoformat(),
        )
        logger.append(feedback, deadline=deadline)
    except Exception:
        pass
    return updated


def _append_effectiveness_exposure(
    selected,
    *,
    session_hash,
    trigger,
    timestamp,
    duration_ms,
    estimated_tokens,
    logger,
    deadline,
):
    if not selected or logger is None:
        return None
    try:
        event = build_exposure_event(
            timestamp=timestamp.isoformat(),
            session_hash=session_hash,
            trigger=trigger,
            memories=selected,
            duration_ms=duration_ms,
            estimated_tokens=estimated_tokens,
        )
        return event if logger.append(event, deadline=deadline) else None
    except Exception:
        return None


def _append_privacy_log(
    runtime_config: Mapping[str, object],
    vault_root,
    event: PromptEvent,
    result: HookResult,
    decision: TriggerDecision,
    current_version: tuple,
    candidates: list[Mapping[str, object]],
    selected: list[Mapping[str, object]],
    now: datetime,
    started: float,
    deadline: float,
    monotonic,
) -> None:
    try:
        log_path = runtime_config.get("resolved_log_path")
        if not log_path:
            return
        finished = monotonic()
        record = {
            "timestamp": now.isoformat(),
            "agent": event.agent,
            "session_hash": result.session_hash,
            "status": result.status,
            "trigger": result.trigger,
            "reasons": list(decision.reasons),
            "index_version": list(current_version),
            "duration_ms": round(max(0.0, finished - started) * 1000, 3),
            "candidate_count": len(candidates),
            "loaded_count": result.loaded,
            "estimated_tokens": result.estimated_tokens,
            "memories": [
                {
                    "id": str(memory.get("id") or ""),
                    "revision": str(memory.get("revision") or ""),
                    "type": str(memory.get("type") or ""),
                }
                for memory in selected
            ],
        }
        PrivacyLogger(
            log_path,
            monotonic=monotonic,
            vault_root=vault_root,
        ).append(
            record,
            deadline=deadline,
        )
    except Exception:
        return


def _is_substantive(prompt: str) -> bool:
    normalized = re.sub(r"[\s\W_]+", "", str(prompt or "").casefold())
    if not normalized or normalized in SHORT_CONFIRMATIONS:
        return False
    return bool(extract_terms(prompt, limit=3))


def _weighted_jaccard(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> float:
    keys = set(left) | set(right)
    if not keys:
        return 1.0
    numerator = sum(min(float(left.get(key, 0)), float(right.get(key, 0))) for key in keys)
    denominator = sum(max(float(left.get(key, 0)), float(right.get(key, 0))) for key in keys)
    return numerator / denominator if denominator else 1.0


def _parse_datetime(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed
