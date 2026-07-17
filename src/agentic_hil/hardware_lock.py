from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
import threading
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentic_hil.types import JsonObject


class HardwareLockError(Exception):
    pass


class HardwareQuarantinedError(HardwareLockError):
    def __init__(self, details: JsonObject):
        self.details = details
        super().__init__("Project hardware state is quarantined.")


class ProjectHardwareLock:
    _owners: dict[str, str] = {}
    _owners_guard = threading.Lock()

    def __init__(self, config_path: str):
        self.project_key = hashlib.sha256(os.path.normcase(str(Path(config_path).resolve())).encode("utf-8")).hexdigest()
        self.path = Path(tempfile.gettempdir()) / "agentic-hil" / f"hardware-{self.project_key}.lock"
        self.quarantine_path = self.path.with_suffix(".quarantine.json")
        self.owner_token = uuid.uuid4().hex
        self.handle: Any = None

    def acquire(self, ignore_quarantine: bool = False) -> bool:
        quarantine = None if ignore_quarantine else self.quarantine_info()
        if quarantine is not None:
            raise HardwareQuarantinedError(quarantine)
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

    def quarantine_info(self) -> JsonObject | None:
        try:
            raw = json.loads(self.quarantine_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as error:
            return {"version": 1, "project_key": self.project_key, "reason": "quarantine_unreadable", "source": "hardware_lock", "active_resources": [], "inspection_errors": [{"type": "quarantine", "error": str(error)}]}
        return raw if isinstance(raw, dict) else {"version": 1, "project_key": self.project_key, "reason": "quarantine_invalid", "source": "hardware_lock", "active_resources": [], "inspection_errors": [{"type": "quarantine", "error": "Quarantine marker is not a JSON object."}]}

    def mark_quarantined(
        self,
        *,
        reason: str,
        source: str,
        active_resources: list[JsonObject],
        inspection_errors: list[JsonObject],
    ) -> JsonObject:
        marker: JsonObject = {
            "version": 1,
            "project_key": self.project_key,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "reason": reason,
            "source": source,
            "active_resources": active_resources,
            "inspection_errors": inspection_errors,
        }
        self.quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.quarantine_path.with_name(f"{self.quarantine_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(marker, handle, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                with suppress(OSError):
                    temp_path.chmod(0o600)
            os.replace(temp_path, self.quarantine_path)
        except OSError as error:
            with suppress(OSError):
                temp_path.unlink()
            raise HardwareLockError(str(error)) from error
        return marker

    def clear_quarantine(self) -> None:
        try:
            self.quarantine_path.unlink()
        except FileNotFoundError:
            return
        except OSError as error:
            raise HardwareLockError(str(error)) from error

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
