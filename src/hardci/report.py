from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from hardci.config import display_path, resolve_work_path
from hardci.types import HardCIConfig, JsonObject


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def timestamp_for_filename() -> str:
    return utc_now_iso().replace("-", "").replace(":", "").replace(".", "")


def reports_directory(config: HardCIConfig) -> str:
    directory = Path(resolve_work_path(config, config.reports.directory))
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory)


def logs_directory(config: HardCIConfig) -> str:
    directory = Path(resolve_work_path(config, config.logs.directory))
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory)


def append_jsonl(log_path: str, event: JsonObject) -> None:
    entry = dict(event)
    entry.setdefault("time", utc_now_iso())
    with Path(log_path).open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry) + "\n")


def safe_filename(value: str, fallback: str = "item") -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value) or fallback


def last_report_path(config: HardCIConfig) -> str:
    return str(Path(reports_directory(config)) / "last-report.json")


def write_report(config: HardCIConfig, report: JsonObject) -> JsonObject:
    report_path = last_report_path(config)
    enriched = dict(report)
    enriched.setdefault("report_path", display_path(config, report_path))
    Path(report_path).write_text(json.dumps(enriched, indent=2) + "\n", encoding="utf-8")
    return enriched


def read_last_report(config: HardCIConfig) -> JsonObject:
    report_path = last_report_path(config)
    path = Path(report_path)
    if not path.exists():
        return {
            "ok": False,
            "tool": "hardci_get_last_report",
            "error_type": "report_not_found",
            "summary": "No HardCI report has been written yet.",
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "ok": False,
            "tool": "hardci_get_last_report",
            "error_type": "config_invalid",
            "summary": "Last HardCI report is not valid JSON.",
            "report_path": display_path(config, report_path),
        }
