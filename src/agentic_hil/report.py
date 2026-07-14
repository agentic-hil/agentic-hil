from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from agentic_hil.config import (
    display_path,
    safe_append_text,
    safe_configured_directory,
    safe_file_path,
    safe_write_text,
)
from agentic_hil.types import AgenticHILConfig, JsonObject


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def timestamp_for_filename() -> str:
    return utc_now_iso().replace("-", "").replace(":", "").replace(".", "")


def reports_directory(config: AgenticHILConfig) -> str:
    return safe_configured_directory(config, config.reports.directory, "reports.directory")


def logs_directory(config: AgenticHILConfig) -> str:
    return safe_configured_directory(config, config.logs.directory, "logs.directory")


def append_jsonl(log_path: str, event: JsonObject) -> None:
    entry = dict(event)
    entry.setdefault("time", utc_now_iso())
    safe_append_text(log_path, json.dumps(entry) + "\n")


def safe_filename(value: str, fallback: str = "item") -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value) or fallback


def last_report_path(config: AgenticHILConfig) -> str:
    return str(Path(reports_directory(config)) / "last-report.json")


def write_report(config: AgenticHILConfig, report: JsonObject) -> JsonObject:
    report_path = last_report_path(config)
    enriched = dict(report)
    enriched.setdefault("report_path", display_path(config, report_path))
    safe_write_text(config, report_path, json.dumps(enriched, indent=2) + "\n")
    return enriched


def read_last_report(config: AgenticHILConfig) -> JsonObject:
    report_path = last_report_path(config)
    path = safe_file_path(report_path, config.work_dir)
    if not path.exists():
        return {
            "ok": False,
            "tool": "get_last_report",
            "error_type": "report_not_found",
            "summary": "No Agentic HIL report has been written yet.",
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "ok": False,
            "tool": "get_last_report",
            "error_type": "config_invalid",
            "summary": "Last Agentic HIL report is not valid JSON.",
            "report_path": display_path(config, report_path),
        }
