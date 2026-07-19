from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path

from agentic_hil.config import (
    ConfigError,
    atomic_write_text,
    display_path,
    project_state_directory,
    safe_append_text,
    safe_configured_directory,
    safe_directory,
    safe_file_lock,
    safe_file_path,
    safe_read_text,
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


def append_jsonl(log_path: str, event: JsonObject) -> Exception | None:
    entry = dict(event)
    entry.setdefault("time", utc_now_iso())
    try:
        safe_append_text(log_path, json.dumps(entry) + "\n")
    except (ConfigError, OSError, ValueError) as error:
        return error
    return None


def safe_filename(value: str, fallback: str = "item") -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value) or fallback


def last_report_path(config: AgenticHILConfig) -> str:
    return str(Path(reports_directory(config)) / "last-report.json")


def last_failure_path(config: AgenticHILConfig) -> str:
    return str(Path(reports_directory(config)) / "last-failure.json")


def report_lock_path(config: AgenticHILConfig) -> str:
    return str(report_state_directory(config) / ".report-state.lock")


def report_state_path(config: AgenticHILConfig) -> str:
    return str(report_state_directory(config) / "report-state.json")


def report_state_directory(config: AgenticHILConfig) -> Path:
    return safe_directory(project_state_directory(config) / "reports")


def write_report(config: AgenticHILConfig, report: JsonObject) -> JsonObject:
    enriched = dict(report)
    enriched.setdefault("audit_ok", True)
    try:
        report_path = last_report_path(config)
        with safe_file_lock(report_lock_path(config)):
            state = load_or_initialize_report_state(config)
            snapshot_allowed = True
            if is_failure_report(enriched):
                try:
                    safe_write_text(config, last_failure_path(config), json.dumps(enriched, indent=2) + "\n")
                except (ConfigError, OSError, ValueError) as error:
                    enriched = mark_audit_failure(enriched, error)
                    snapshot_allowed = False
            if snapshot_allowed:
                snapshot = {**enriched, "report_path": display_path(config, report_path)}
                try:
                    safe_write_text(config, report_path, json.dumps(snapshot, indent=2) + "\n")
                except (ConfigError, OSError, ValueError) as error:
                    enriched = mark_audit_failure(enriched, error)
                else:
                    enriched = snapshot
            enriched["state_path"] = report_state_path(config)
            state["last_report"] = enriched
            if is_failure_report(enriched):
                state["last_failure"] = enriched
            write_report_state(config, state)
    except (ConfigError, OSError, ValueError) as error:
        return mark_audit_failure(enriched, error)
    return enriched


def read_last_report(config: AgenticHILConfig) -> JsonObject:
    return read_report_state_entry(config, "last_report", last_report_path, "get_last_report", "No Agentic HIL report has been written yet.")


def read_last_failure(config: AgenticHILConfig) -> JsonObject:
    return read_report_state_entry(config, "last_failure", last_failure_path, "classify_last_error", "No Agentic HIL failure has been recorded yet.")


def read_report_state_entry(
    config: AgenticHILConfig,
    key: str,
    legacy_path_factory: Callable[[AgenticHILConfig], str],
    tool: str,
    missing_summary: str,
) -> JsonObject:
    state_path = report_state_path(config)
    try:
        with safe_file_lock(report_lock_path(config)):
            state = read_report_state(config)
            if state is None:
                return {"ok": False, "tool": tool, "error_type": "report_not_found", "summary": missing_summary}
            report = state.get(key)
            if isinstance(report, dict):
                return report
            return {"ok": False, "tool": tool, "error_type": "report_not_found", "summary": missing_summary}
    except ConfigError as error:
        return {"tool": tool, **error.to_dict()}
    except (OSError, ValueError) as error:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "report_unreadable",
            "summary": "Agentic HIL report state could not be read.",
            "report_path": state_path,
            "backend_error": str(error),
        }


def read_report_state(config: AgenticHILConfig) -> JsonObject | None:
    path = report_state_path(config)
    try:
        text = safe_read_text(path)
    except FileNotFoundError:
        return None
    try:
        state = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError("config_invalid", "Agentic HIL report state is not valid JSON.", {"path": path}) from error
    if not isinstance(state, dict) or state.get("version") != 1:
        raise ConfigError("config_invalid", "Agentic HIL report state has an unsupported format.", {"path": path})
    for key in ("last_report", "last_failure"):
        if state.get(key) is not None and not isinstance(state.get(key), dict):
            raise ConfigError("config_invalid", "Agentic HIL report state contains an invalid entry.", {"path": path, "field": key})
    return state


def load_or_initialize_report_state(config: AgenticHILConfig) -> JsonObject:
    state = read_report_state(config)
    if state is not None:
        return state
    state = {
        "version": 1,
        "last_report": None,
        "last_failure": None,
    }
    write_report_state(config, state)
    return state


def read_legacy_report(config: AgenticHILConfig, path: str) -> JsonObject | None:
    try:
        text = safe_read_text(path, workspace=config.work_dir)
    except FileNotFoundError:
        return None
    try:
        report = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ConfigError("config_invalid", "Legacy Agentic HIL report is not valid JSON.", {"path": display_path(config, path)}) from error
    if not isinstance(report, dict):
        raise ConfigError("config_invalid", "Legacy Agentic HIL report root must be an object.", {"path": display_path(config, path)})
    return report


def write_report_state(config: AgenticHILConfig, state: JsonObject) -> None:
    atomic_write_text(report_state_path(config), json.dumps(state, indent=2) + "\n")


def read_report_file(config: AgenticHILConfig, path_factory: Callable[[AgenticHILConfig], str], tool: str, missing_summary: str) -> JsonObject:
    try:
        report_path = path_factory(config)
        text = safe_read_text(report_path, workspace=config.work_dir)
    except FileNotFoundError:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "report_not_found",
            "summary": missing_summary,
        }
    except ConfigError as error:
        return {"tool": tool, **error.to_dict()}
    except UnicodeDecodeError:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "config_invalid",
            "summary": "Agentic HIL report is not valid UTF-8 text.",
            "report_path": display_path(config, report_path),
        }
    except OSError as error:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "report_unreadable",
            "summary": "Agentic HIL report could not be read.",
            "backend_error": str(error),
        }
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "tool": tool,
            "error_type": "config_invalid",
            "summary": "Agentic HIL report is not valid JSON.",
            "report_path": display_path(config, report_path),
        }


def is_failure_report(report: JsonObject) -> bool:
    return not overall_success(report)


def overall_success(result: JsonObject) -> bool:
    return result.get("ok") is True and result.get("target_ok") is not False and result.get("audit_ok") is not False


def classify_failure_report(config: AgenticHILConfig, likely_causes: Callable[[str], list[str]]) -> JsonObject:
    report = read_last_failure(config)
    if report.get("ok") is not True and report.get("tool") == "classify_last_error" and report.get("error_type") not in {None, "report_not_found"}:
        return report
    if not report.get("ok") and report.get("error_type") == "report_not_found":
        return {"ok": False, "tool": "classify_last_error", "error_type": "report_not_found", "summary": "No Agentic HIL failure has been recorded yet."}
    if report.get("ok") is True and report.get("target_ok") is not False and report.get("audit_ok") is not False:
        return {"ok": True, "tool": "classify_last_error", "error_type": None, "summary": "Last Agentic HIL failure record did not contain an error."}
    error_type = str(report.get("target_error_type") or report.get("error_type") or ("audit_failed" if report.get("audit_ok") is False else "unknown_debugger_error"))
    result = {
        "ok": True,
        "tool": "classify_last_error",
        "error_type": error_type,
        "summary": report.get("summary", "Last Agentic HIL failure contained an error."),
        "likely_causes": report.get("likely_causes", likely_causes(error_type)),
        "report_path": report.get("report_path"),
        "log_path": report.get("log_path"),
        "source_tool": report.get("tool"),
    }
    if "backend_error_type" in report:
        result["backend_error_type"] = report["backend_error_type"]
    if "failed_step" in report:
        result["failed_step"] = report["failed_step"]
    if "step_error_type" in report:
        result["step_error_type"] = report["step_error_type"]
    return result


def ensure_audit_ready(config: AgenticHILConfig) -> None:
    for directory in (reports_directory(config), logs_directory(config)):
        probe = Path(directory) / f".audit-probe-{os.getpid()}"
        try:
            safe_write_text(config, probe, "")
        finally:
            with suppress(FileNotFoundError):
                probe.unlink()
    safe_file_path(last_report_path(config), config.work_dir)
    safe_file_path(last_failure_path(config), config.work_dir)
    safe_file_path(report_state_path(config))
    with safe_file_lock(report_lock_path(config)):
        state = load_or_initialize_report_state(config)
        write_report_state(config, state)


def audit_unavailable(tool: str, error: Exception) -> JsonObject:
    return {
        "ok": False,
        "tool": tool,
        "error_type": "audit_unavailable",
        "summary": "Hardware action was not started because audit output is unavailable.",
        "side_effect_committed": False,
        "audit_ok": False,
        "audit_error": error.to_dict()
        if isinstance(error, ConfigError)
        else {"error_type": type(error).__name__, "backend_error": str(error)},
    }


def mark_side_effect(result: JsonObject) -> JsonObject:
    if result.get("tool") not in {
        "flash_firmware", "reset_target", "debug_start_session", "debug_set_breakpoint", "debug_continue",
        "com_session_start", "com_write", "can_session_start", "can_send", "adapter_session_start",
        "adapter_set_value", "adapter_inject_fault",
    }:
        return result
    enriched = dict(result)
    if result.get("side_effect_status") in {"not_started", "committed", "partial", "unknown"}:
        return enriched
    if result.get("ok") is True or result.get("target_ok") is False:
        enriched.update({"side_effect_committed": True, "side_effect_status": "committed", "retry_safe": False})
    elif result.get("error_type") in {
        "permission_denied",
        "invalid_argument",
        "session_already_active",
        "session_not_active",
        "not_supported",
        "artifact_not_found",
        "artifact_validation_failed",
        "output_validation_failed",
        "resource_busy",
    }:
        enriched.update({"side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": True})
    elif result.get("error_type") == "resource_quarantined":
        enriched.update({"side_effect_committed": False, "side_effect_status": "not_started", "retry_safe": False})
    elif result.get("side_effect_committed") is False:
        enriched.update({"side_effect_status": "not_started", "retry_safe": True})
    elif result.get("side_effect_committed") is True:
        enriched.update({"side_effect_status": "partial", "retry_safe": False})
    else:
        enriched.update({"side_effect_status": "unknown", "retry_safe": False})
    return enriched


def merge_audit_status(result: JsonObject, *sources: JsonObject) -> JsonObject:
    failed = [source for source in sources if source.get("audit_ok") is False]
    if not failed:
        return result
    errors: list[JsonObject] = []
    for source in failed:
        for error in audit_errors(source):
            if error not in errors:
                errors.append(error)
    enriched = dict(result)
    enriched.update({"audit_ok": False, "retry_safe": False})
    if errors:
        enriched.update({"audit_error": errors[0], "audit_errors": errors})
    return enriched


def mark_audit_failure(result: JsonObject, error: Exception) -> JsonObject:
    enriched = dict(result)
    new_error = {
        "error_type": getattr(error, "error_type", type(error).__name__),
        "summary": getattr(error, "summary", "Audit output could not be written."),
        "backend_error": str(error),
    }
    errors = audit_errors(result)
    if new_error not in errors:
        errors.append(new_error)
    enriched["audit_ok"] = False
    enriched["audit_error"] = errors[0]
    enriched["audit_errors"] = errors
    if enriched.get("side_effect_committed") is True:
        enriched["retry_safe"] = False
    return enriched


def audit_errors(result: JsonObject) -> list[JsonObject]:
    nested = result.get("audit_errors")
    if isinstance(nested, list):
        return [item for item in nested if isinstance(item, dict)]
    single = result.get("audit_error")
    return [single] if isinstance(single, dict) else []
