from __future__ import annotations

import errno
import json
import os
import re
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from agentic_hil.config import display_path, resolve_work_path
from agentic_hil.types import AgenticHILConfig, JsonObject


class AuditWriteError(OSError):
    """An audit record (report, session log, JSONL event) could not be written.

    completion_state describes the underlying hardware operation, never the audit
    write itself: "confirmed" only when the hardware side was already known
    complete when the audit write failed. The default "unknown" is treated as
    unconfirmed by the tool service.
    """

    def __init__(
        self,
        message: str,
        operation_result: JsonObject | None = None,
        completion_state: str = "unknown",
    ):
        super().__init__(message)
        self.operation_result = operation_result
        self.completion_state = completion_state


def audit_completion_state(result: JsonObject) -> str:
    if result.get("ok") is True or result.get("completion_confirmed") is True:
        return "confirmed"
    return "unknown"


def annotate_audit_error(error: AuditWriteError, operation_result: JsonObject) -> AuditWriteError:
    if error.operation_result is None:
        error.operation_result = operation_result
    if error.completion_state == "unknown":
        error.completion_state = audit_completion_state(operation_result)
    return error


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def timestamp_for_filename() -> str:
    return utc_now_iso().replace("-", "").replace(":", "").replace(".", "")


def reports_directory(config: AgenticHILConfig) -> str:
    directory = Path(resolve_work_path(config, config.reports.directory))
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory)


def logs_directory(config: AgenticHILConfig) -> str:
    directory = Path(resolve_work_path(config, config.logs.directory))
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory)


def append_jsonl(log_path: str, event: JsonObject) -> None:
    entry = dict(event)
    entry.setdefault("time", utc_now_iso())
    try:
        with Path(log_path).open("a", encoding="utf-8") as file:
            file.write(json.dumps(entry) + "\n")
            file.flush()
    except OSError as error:
        raise AuditWriteError(str(error), entry) from error


def safe_filename(value: str, fallback: str = "item") -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value) or fallback


def last_report_path(config: AgenticHILConfig) -> str:
    return str(Path(reports_directory(config)) / "last-report.json")


def write_report(config: AgenticHILConfig, report: JsonObject) -> JsonObject:
    report_path = last_report_path(config)
    enriched = dict(report)
    enriched.setdefault("report_path", display_path(config, report_path))
    try:
        atomic_write_text(Path(report_path), json.dumps(enriched, indent=2) + "\n")
    except OSError as error:
        raise AuditWriteError(str(error), enriched) from error
    return enriched


def write_audit_json(path: str | Path, payload: JsonObject) -> None:
    try:
        atomic_write_text(Path(path), json.dumps(payload, indent=2) + "\n")
    except OSError as error:
        raise AuditWriteError(str(error), payload) from error


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("w", encoding=encoding) as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        fsync_directory(path.parent)
    except BaseException:
        with suppress(OSError):
            temp_path.unlink()
        raise


def fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    flags = getattr(os, "O_DIRECTORY", 0) | os.O_RDONLY
    descriptor: int | None = None
    try:
        descriptor = os.open(directory, flags)
        os.fsync(descriptor)
    except OSError as error:
        unsupported = {errno.EINVAL, getattr(errno, "ENOTSUP", errno.EINVAL), getattr(errno, "EOPNOTSUPP", errno.EINVAL)}
        if error.errno not in unsupported:
            raise
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def read_last_report(config: AgenticHILConfig) -> JsonObject:
    report_path = last_report_path(config)
    path = Path(report_path)
    if not path.exists():
        return {
            "ok": False,
            "tool": "get_last_report",
            "error_type": "report_not_found",
            "summary": "No Agentic HIL report has been written yet.",
        }
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {
            "ok": False,
            "tool": "get_last_report",
            "error_type": "report_not_found",
            "summary": "No Agentic HIL report has been written yet.",
        }
    except OSError as error:
        return {
            "ok": False,
            "tool": "get_last_report",
            "error_type": "report_unreadable",
            "summary": "Last Agentic HIL report could not be read.",
            "backend_error": str(error),
            "report_path": display_path(config, report_path),
        }
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {
            "ok": False,
            "tool": "get_last_report",
            "error_type": "config_invalid",
            "summary": "Last Agentic HIL report is not valid JSON.",
            "report_path": display_path(config, report_path),
        }
    if not isinstance(loaded, dict):
        return {
            "ok": False,
            "tool": "get_last_report",
            "error_type": "config_invalid",
            "summary": "Last Agentic HIL report is not a JSON object.",
            "report_path": display_path(config, report_path),
        }
    return loaded
