from __future__ import annotations

import errno
import hashlib
import os
import tempfile
import threading
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any


class HardwareLockError(Exception):
    pass


class ProjectHardwareLock:
    _owners: dict[str, str] = {}
    _owners_guard = threading.Lock()

    def __init__(self, config_path: str):
        project_key = hashlib.sha256(os.path.normcase(str(Path(config_path).resolve())).encode("utf-8")).hexdigest()
        self.path = Path(tempfile.gettempdir()) / "agentic-hil" / f"hardware-{project_key}.lock"
        self.owner_token = uuid.uuid4().hex
        self.handle: Any = None

    def acquire(self) -> bool:
        if self.handle is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = self.path.open("a+b")
            if os.name == "nt" and os.fstat(self.handle.fileno()).st_size == 0:
                self.handle.seek(0)
                self.handle.write(b"0")
                self.handle.flush()
        except OSError as error:
            self._close_handle()
            raise HardwareLockError(str(error)) from error

        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self._close_handle()
            if error.errno in {errno.EACCES, errno.EAGAIN}:
                return False
            raise HardwareLockError(str(error)) from error

        with self._owners_guard:
            self._owners[str(self.path)] = self.owner_token
        return True

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            with suppress(OSError):
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            with self._owners_guard:
                if self._owners.get(str(self.path)) == self.owner_token:
                    self._owners.pop(str(self.path), None)
            self._close_handle()

    @classmethod
    def owner_is_active(cls, config_path: str, owner_token: str) -> bool:
        lock = cls(config_path)
        with cls._owners_guard:
            return cls._owners.get(str(lock.path)) == owner_token

    def _close_handle(self) -> None:
        if self.handle is not None:
            with suppress(OSError):
                self.handle.close()
            self.handle = None
