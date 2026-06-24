# Copyright 2026 Hannes Pauli
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AIHILConfig, display_path, resolve_work_path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp_for_filename() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def reports_directory(config: AIHILConfig) -> Path:
    path = resolve_work_path(config, config.reports.directory)
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_directory(config: AIHILConfig) -> Path:
    path = resolve_work_path(config, config.logs.directory)
    path.mkdir(parents=True, exist_ok=True)
    return path


def last_report_path(config: AIHILConfig) -> Path:
    return reports_directory(config) / "last-report.json"


def write_report(config: AIHILConfig, report: dict[str, Any]) -> dict[str, Any]:
    path = last_report_path(config)
    enriched = dict(report)
    enriched.setdefault("report_path", display_path(config, path))
    path.write_text(json.dumps(enriched, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return enriched


def read_last_report(config: AIHILConfig) -> dict[str, Any]:
    path = last_report_path(config)
    if not path.exists():
        return {
            "ok": False,
            "tool": "aihil_get_last_report",
            "error_type": "report_not_found",
            "summary": "No AI-HIL report has been written yet.",
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "ok": False,
            "tool": "aihil_get_last_report",
            "error_type": "config_invalid",
            "summary": "Last AI-HIL report is not valid JSON.",
            "report_path": display_path(config, path),
        }
