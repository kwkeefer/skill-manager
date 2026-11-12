"""Shared validation, atomic filesystem changes, and cooperative process locks."""

import errno
import fcntl
import hashlib
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile, mkdtemp
from typing import Self

import yaml


def is_identifier(value: str) -> bool:
    return (
        bool(value.strip())
        and value not in {".", ".."}
        and not any(character in value for character in ("/", "\\", "\0", "\n", "\r"))
    )


def digest(value: object) -> str:
    """Hash Python model data with stable set ordering, preserving list order."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=_digest_default).encode()
    ).hexdigest()


def _digest_default(value: object) -> str | list[object]:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"cannot fingerprint value of type {type(value).__name__}")


def yaml_bytes(value: object) -> bytes:
    return yaml.safe_dump(value, sort_keys=True).encode()


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Serialize cooperating writers; keep the inode after releasing its lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_write(path: Path, content: bytes, mode: int = 0o644) -> None:
    """Publish a complete file using a uniquely owned temporary file."""
    with NamedTemporaryFile(prefix=".skm-", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary.chmod(mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def atomic_symlink(path: Path, target: str) -> None:
    temporary_dir = Path(mkdtemp(prefix=".skm-", dir=path.parent))
    temporary = temporary_dir / "link"
    try:
        temporary.symlink_to(target, target_is_directory=True)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
        temporary_dir.rmdir()


class EntryKind(StrEnum):
    MISSING = "missing"
    FILE = "file"
    LINK = "link"


@dataclass(frozen=True)
class PreviousEntry:
    path: Path
    kind: EntryKind
    content: bytes | str = b""
    mode: int = 0o644


class Transaction:
    """Restore links and metadata on ordinary failures, including interrupts.

    This is not a crash-recovery journal. Callers hold their project/exclude locks
    and preflight ownership before entering the transaction.
    """

    def __init__(self) -> None:
        self._previous: dict[Path, PreviousEntry] = {}
        self._directories: list[Path] = []

    def _remember(self, path: Path) -> None:
        if path in self._previous:
            return
        if path.is_symlink():
            entry = PreviousEntry(path, EntryKind.LINK, os.readlink(path))
        elif path.is_file():
            entry = PreviousEntry(
                path,
                EntryKind.FILE,
                path.read_bytes(),
                stat.S_IMODE(path.stat().st_mode),
            )
        elif os.path.lexists(path):
            raise IsADirectoryError(f"refusing to replace non-file entry: {path}")
        else:
            entry = PreviousEntry(path, EntryKind.MISSING)
        self._previous[path] = entry

    def _mkdir(self, path: Path) -> None:
        if path.is_dir():
            return
        self._mkdir(path.parent)
        try:
            path.mkdir()
        except FileExistsError:
            # Another project can create a shared bookkeeping parent concurrently.
            if not path.is_dir():
                raise
        else:
            self._directories.append(path)

    def write(self, path: Path, content: bytes) -> None:
        if path.is_symlink():
            raise OSError(f"refusing to write metadata through a symlink: {path}")
        if path.is_file() and path.read_bytes() == content:
            return
        self._remember(path)
        self._mkdir(path.parent)
        atomic_write(path, content, self._previous[path].mode)

    def symlink(self, path: Path, target: str) -> None:
        if path.is_symlink() and os.readlink(path) == target:
            return
        self._remember(path)
        self._mkdir(path.parent)
        atomic_symlink(path, target)

    def remove(self, path: Path) -> None:
        if not os.path.lexists(path):
            return
        self._remember(path)
        path.unlink()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: object,
    ) -> None:
        if error is None:
            return
        failures: list[BaseException] = []
        for entry in reversed(tuple(self._previous.values())):
            try:
                if entry.kind is EntryKind.MISSING:
                    entry.path.unlink(missing_ok=True)
                elif entry.kind is EntryKind.LINK:
                    assert isinstance(entry.content, str)
                    atomic_symlink(entry.path, entry.content)
                else:
                    assert isinstance(entry.content, bytes)
                    atomic_write(entry.path, entry.content, entry.mode)
            except OSError as rollback_error:
                failures.append(rollback_error)
        for directory in reversed(self._directories):
            try:
                directory.rmdir()
            except OSError as rollback_error:
                # Leave directories another writer has populated in the meantime.
                if rollback_error.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                    failures.append(rollback_error)
        if failures:
            raise BaseExceptionGroup(
                "installation failed and rollback could not fully restore state",
                [error, *failures],
            ) from error
