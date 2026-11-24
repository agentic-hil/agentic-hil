"""The bench stage's standard-library client of the product device locks.

Keep the digest, file lock and v1 holder record compatible with BenchMutex.
On Windows this client takes the file lock, which same-root product callers
check before their named pipe. Closing the descriptor releases the hold.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

_SAFE_OPEN = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)


class LockOccupied(Exception):
    """The OS refused a nonblocking device lock."""


def lock_path(root: Path, folded_key: str) -> Path:
    return root / (hashlib.sha256(folded_key.encode("utf-8")).hexdigest() + ".lock")


def _check_regular(status: os.stat_result) -> None:
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or getattr(status, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise OSError("Device lock files and holder records must be single-link regular files.")


def _open_regular(path: Path, flags: int, mode: int = 0o600) -> int:
    with suppress(FileNotFoundError):
        _check_regular(path.lstat())
    descriptor = os.open(path, flags | _SAFE_OPEN, mode)
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        _check_regular(opened)
        _check_regular(current)
        if not os.path.samestat(opened, current):
            raise OSError("Device lock file changed while it was opened.")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def take_lock(path: Path, *, shared: bool) -> int:
    mode = 0o660 if shared else 0o600
    descriptor = _open_regular(path, os.O_RDWR | os.O_CREAT, mode)
    try:
        status = os.fstat(descriptor)
        if os.name != "nt" and shared and status.st_uid == os.geteuid() and stat.S_IMODE(status.st_mode) != mode:
            os.fchmod(descriptor, mode)
        try:
            if os.name == "nt":
                import msvcrt

                if status.st_size == 0:
                    os.write(descriptor, b"0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK, errno.EDEADLK):
                raise LockOccupied() from error
            raise
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def holder_label(path: Path) -> str:
    """Describe the last published held record after an OS lock refusal."""
    try:
        descriptor = _open_regular(path.with_suffix(".holder.json"), os.O_RDONLY)
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            record = json.loads(stream.read(65536))
    except (OSError, ValueError):
        return "an unidentified holder"
    if isinstance(record, dict) and record.get("version") == 1 and record.get("state") == "held":
        owner = record.get("owner")
        if isinstance(owner, dict):
            if owner.get("label"):
                return str(owner["label"])
            if isinstance(owner.get("pid"), int):
                return f"process {owner['pid']}"
    return "an unidentified holder"


def write_holder(path: Path, record: dict[str, object], *, shared: bool) -> None:
    destination = path.with_suffix(".holder.json")
    with suppress(FileNotFoundError):
        _check_regular(destination.lstat())
    descriptor, temporary = tempfile.mkstemp(dir=destination.parent, prefix=f".{destination.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            if shared and os.name != "nt":
                os.fchmod(stream.fileno(), 0o660)
            json.dump(record, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, destination)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)
