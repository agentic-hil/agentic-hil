"""Report sanitized Nano ESP32-S3 demo results."""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile

from agentic_hil.report import overall_success

DEVICE = "esp32s3"
QUALIFICATION = "nano-esp32s3-shared-bench"
REFUSALS = {f"nano-esp32s3-console-refuses-{action}": action for action in ("flash", "reset")}
IMAGES = {"banner_a.bin": "A", "banner_b.bin": "B"}
ACTIONS = ("flash", "reset", "uart_open", "uart_read", "uart_read", "uart_close")
CYCLES = 20
SHA256 = r"[0-9a-f]{64}"

# These evidence fields can hold raw identifiers and are never published.
# Keep checking nested fields, even under a recognized object such as identity.
IDENTITY_FIELDS = {
    "submitted_by", "host", "serial_number", "expected_serial_number", "found_serial_number",
    "probe_id", "device", "stable_device", "executable", "executable_path", "resource", "resources",
    "lock_key", "lock_keys", "declared_devices", "owner_id", "lease_id", "quarantine_id",
}
PATH_FIELDS = {"path", "test_config_path", "report_path", "canonical_report_path", "log_path"}
PRIVATE_FIELDS = IDENTITY_FIELDS | PATH_FIELDS | {
    "command", "stdout", "stderr", "summary", "remediation", "do_not", "next_step", "backend_error", "backend_warnings",
}
# Follow recognized report and bundle containers from the root. A familiar
# field name inside an unfamiliar object is still an unrecognized field.
EVIDENCE_CONTAINERS = {
    "": {"steps", "cleanup", "cleanup_errors", "config_in_force", "recovery", "plan", "bench", "tools"},
    "steps": {"iterations", "result"},
    "iterations": {"steps"},
    "cleanup": {"result"},
    "cleanup_errors": {"result"},
    "result": {"config_in_force", "artifact", "identity", "port_identity", "session", "holder", "comparator"},
    "recovery": {"reset"},
    "bench": {"runner"},
}
PRIVATE_SHAPE = re.compile(
    r"(?i)(?<![0-9a-z])(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}(?![0-9a-z])"
    r"|\bCOM[0-9]+\b|(?<![\w./\\-])(?:[A-Z]:[\\/]|\\\\|/)[^\s/\\]+"
    r"|\b(?:[\w-]+\.)+(?:local|localhost|internal|lan|home|corp|example|com|net|org)\b"
)
ADDRESS = re.compile(r"(?<![\w.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![\w.])|[0-9a-fA-F:.]*:[0-9a-fA-F:.]+")
HOME_USER = re.compile(r"(?:^|[/\\])(?:home|users)[/\\]([^/\\]+)", re.I)
IDENTITY_KEY = re.compile(r"(?:^|[_-])(?:serial(?:_number)?|mac|host(?:name)?|user(?:name)?|ip(?:_address)?|device_path)(?:$|[_-])", re.I)


class InvalidEvidence(ValueError):
    """An evidence error whose message is safe to publish."""


def require(valid: bool, message: str) -> None:
    if not valid:
        raise InvalidEvidence(message)


def fields(document, path=()):
    """Yield text values and their field paths across nested receipts."""
    if isinstance(document, dict):
        for key, value in document.items():
            yield from fields(value, (*path, key))
    elif isinstance(document, list):
        for item in document:
            yield from fields(item, path)
    elif isinstance(document, str):
        yield path, document


def private_text(value: str, identities: set[str]) -> bool:
    folded = value.casefold()
    if PRIVATE_SHAPE.search(value) or any(item in folded for item in identities):
        return True
    for match in ADDRESS.finditer(value):
        try:
            ipaddress.ip_address(match[0])
        except ValueError:
            continue
        return True
    return False


def recognized_container(path: tuple[str, ...]) -> bool:
    parent = ""
    for key in path[:-1]:
        if key not in EVIDENCE_CONTAINERS.get(parent, ()):
            return False
        parent = key
    return True


def check_privacy(*documents: dict) -> None:
    values = [(path, value, recognized_container(path)) for document in documents for path, value in fields(document)]
    identities = set()
    for path, value, recognized in values:
        if not recognized:
            continue
        is_identity = path[-1] in IDENTITY_FIELDS or path[-2:] == ("runner", "name")
        if is_identity and len(value) >= 3 and not value.startswith(("<", "[")):
            identities.add(value.casefold())
        if path[-1] in PATH_FIELDS and (user := HOME_USER.search(value)):
            identities.add(user[1].casefold())
    for path, value, recognized in values:
        if recognized and (path[-1] in PRIVATE_FIELDS or path[-2:] in {("runner", "name"), ("comparator", "pattern")}):
            continue
        require(not private_text(value, identities) and not IDENTITY_KEY.search(path[-1]), "Evidence contains a private identifier in an unrecognized field.")


def basename(value: str) -> str:
    return PurePosixPath(value.replace("\\", "/")).name


def esptool_members(archive: ZipFile) -> dict[str, dict]:
    logs = {}
    for member in archive.infolist():
        path = PurePosixPath(member.filename)
        if path.parent == PurePosixPath("logs") and path.match("esptool-*.log"):
            require(path.name not in logs, "The evidence bundle contains duplicate esptool logs.")
            logs[path.name] = json.loads(archive.read(member))
    return logs


def public_header(report: dict, summary: dict, archive_name: str) -> dict:
    plan = summary["plan"]
    job_id = report["job_id"]
    config = summary["bench"]["config_digest"]
    product_version = summary["tools"]["agentic_hil"]
    require(bool(re.fullmatch(r"job-[0-9a-f]+", job_id)) and archive_name == f"{job_id}-evidence.zip", "The report and evidence bundle must identify the same job.")
    require(plan["name"] == report["name"] and bool(re.fullmatch(SHA256, plan["sha256"]))
            and plan["sha256"] == report["test_config_sha256"] and not plan.get("sha256_mismatch"), "The executed plan does not match the evidence summary.")
    require(bool(re.fullmatch(rf"(?:sha256:)?{SHA256}", config)) and config == report["config_in_force"]["digest"], "The configuration digest does not match the report.")
    require(bool(re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[a-z]+[0-9]+)?", product_version)), "The evidence lacks a valid Agentic HIL version.")
    return {
        "job_id": job_id,
        "plan": {"name": plan["name"], "sha256": plan["sha256"]},
        "agentic_hil": product_version,
        "config_digest": config,
    }


def image_records(manifest: dict) -> dict:
    images = manifest["images"]
    require(manifest["chip"] == DEVICE and manifest["flash_address"] == "0x0" and images.keys() == IMAGES.keys(), "The manifest must describe both Nano ESP32-S3 images at 0x0.")
    public = {}
    for name, letter in IMAGES.items():
        record = images[name]
        require(record["image"] == letter and bool(re.fullmatch(SHA256, record["sha256"]))
                and type(record["size"]) is int and record["size"] > 0, "An image manifest record is invalid.")
        public[name] = {"image": letter, "sha256": record["sha256"], "size": record["size"]}
    return public


def step_results(steps: list[dict], actions: tuple[str, ...], start: int = 1) -> list[dict]:
    require(len(steps) == len(actions), "The report is missing step receipts.")
    for index, (step, action) in enumerate(zip(steps, actions, strict=True), start):
        require(step["index"] == index and step["action"] == action and step["route"] == DEVICE, "A step receipt is out of sequence or has the wrong device.")
    return [step["result"] for step in steps]


def successful(result: dict) -> bool:
    return result.get("ok") is True and overall_success(result)


def console_lines(result: dict, letters: str, ticks: str = r"[0-9]+") -> str:
    text = result["matched_text"]["text"]
    require(not result.get("matched_text_truncated"), "The console match is truncated.")
    require(bool(re.fullmatch(rf"agentic-hil esp32s3 image [{letters}] tick {ticks}\r?\n", text)), "The console did not record the required image and tick.")
    return text.rstrip("\r\n")


def debugger_output(result: dict, logs: dict[str, dict], used: set[str]) -> str:
    name = basename(result["log_path"])
    require(name in logs and name not in used, "A debugger step lacks its own esptool log.")
    used.add(name)
    log = logs[name]
    require(log["returncode"] == 0 and log["timed_out"] is False, "An esptool log does not confirm a completed action.")
    return log["stdout"] + "\n" + log["stderr"]


def capture(output: str, pattern: str, message: str) -> str:
    match = re.search(pattern, output, re.MULTILINE)
    if match is None:
        raise InvalidEvidence(message)
    return match[1]


def half_cycle(steps: list[dict], iteration: int, name: str, image: dict, logs: dict[str, dict], used: set[str]) -> dict:
    start = 1 if image["image"] == "A" else len(ACTIONS) + 1
    flash, reset, opened, first, second, closed = results = step_results(steps, ACTIONS, start)
    require(all(successful(result) for result in results), "A qualification step failed.")
    artifact = flash["artifact"]
    require(flash["verify"] is True and flash["reset_after_flash"] is False
            and basename(artifact["path"]) == name and artifact["sha256"] == image["sha256"], "A verified flash does not match its manifest image.")
    require(reset["mode"] == "run", "A reset did not run the flashed image.")
    require(opened["session"]["session_active"] is True and closed["session"]["session_active"] is False, "The console receipts do not show a completed session.")
    letter = image["image"]
    first_line = console_lines(first, letter)
    pair = second["matched_text"]["text"]
    require(not second.get("matched_text_truncated") and bool(re.fullmatch(
        rf"agentic-hil esp32s3 image {letter} tick 50\r?\nagentic-hil esp32s3 image {letter} tick 51\r?\n", pair,
    )), "The console lacks the consecutive ticks for the flashed image.")
    durations = [step["elapsed_ms"] for step in steps]
    require(all(type(value) is int and value >= 0 for value in durations), "The report lacks valid step timings.")

    flash_text = debugger_output(flash, logs, used)
    reset_text = debugger_output(reset, logs, used)
    chip = capture(flash_text, r"^\s*Chip type:[ \t]*(ESP32-S3(?: \(QFN56\))?(?: \(revision v[0-9]+\.[0-9]+\))?)[ \t\r]*$", "The flash log does not identify an ESP32-S3.")
    usb = capture(flash_text, r"^\s*USB mode:[ \t]*(USB-Serial/JTAG)[ \t\r]*$", "The flash log does not identify USB-Serial/JTAG mode.")
    reset_line = capture(reset_text, r"^\s*(Hard resetting (?:with a watchdog|via RTS pin)\.\.\.)[ \t\r]*$", "The reset log does not show a run reset.")
    return {
        "iteration": iteration,
        "image": letter,
        "artifact_sha256": artifact["sha256"],
        "matches_manifest": True,
        "verify": True,
        "reset": {"ok": True, "mode": "run"},
        "console_lines": [first_line, *pair.splitlines()],
        "durations_ms": durations,
        "chip": chip,
        "usb_mode": usb,
        "reset_line": reset_line,
    }


def qualification(report: dict, summary: dict, images: dict, logs: dict[str, dict]) -> dict:
    require(report["name"] == QUALIFICATION and successful(report) and summary["outcome"] == "success", "The qualification plan did not pass.")
    require(report["cleanup"] == [], "The qualification plan did not close its own console sessions.")
    repeat, = report["steps"]
    result = repeat["result"]
    iterations = repeat["iterations"]
    require(repeat["action"] == "repeat" and repeat["index"] == 1 and successful(result)
            and result["count"] == result["iterations_run"] == len(iterations) == CYCLES
            and result["exit_reason"] == "count", "The report lacks 20 complete A/B iterations.")
    cycles = []
    used: set[str] = set()
    for iteration, record in enumerate(iterations, 1):
        steps = record["steps"]
        require(record["iteration"] == iteration and len(steps) == 2 * len(ACTIONS), "An A/B iteration lacks both complete half-cycles.")
        for half, (name, image) in enumerate(images.items()):
            start = half * len(ACTIONS)
            cycles.append(half_cycle(steps[start:start + len(ACTIONS)], iteration, name, image, logs, used))
    return {"images": images, "cycles": cycles}


def refusal_holder(opened: dict, refused: dict) -> dict:
    if "held_by_com_session" in refused:
        require(refused["held_by_com_session"] == DEVICE, "The refusal names a different console.")
        return {"held_by_com_session": DEVICE}

    # On a bench with several debuggers the debugger service sees the lock
    # holder but has no UART session table. Tie that lock to this job's open.
    resource = refused.get("resource")
    require(refused.get("holder_is_this_process") is True
            and isinstance(resource, str) and resource in opened["resources"]
            and opened["port_id"] == DEVICE and opened["session"]["session_active"] is True,
            "The refusal does not identify the Nano console as the holder.")
    return {"holder_is_this_process": True, "port_id": DEVICE}


def refusal(report: dict, summary: dict, logs: dict[str, dict]) -> dict:
    action = REFUSALS[report["name"]]
    run = summary["run"]
    require(report["ok"] is False and summary["outcome"] == "failure"
            and report["failed_step"] == run["failed_step"] == 3
            and report["error_type"] == report["step_error_type"] == run["error_type"] == "device_busy", "The job did not fail at the expected console refusal.")
    opened, read, refused = step_results(report["steps"], ("uart_open", "uart_read", action))
    require(successful(opened) and successful(read), "The job did not establish console ownership before the refusal.")
    console_lines(read, "AB")
    require(refused["ok"] is False and refused["error_type"] == "device_busy"
            and refused.get("side_effect_committed") is False,
            "The final step does not show a pre-contact refusal by the Nano console.")
    holder = refusal_holder(opened, refused)
    cleanup, = report["cleanup"]
    require(cleanup["port_id"] == DEVICE and cleanup["action"] == "uart_close"
            and successful(cleanup["result"]), "The refusal evidence does not confirm console cleanup.")

    # The bundle is made from the report's log references. Attribute a recovery
    # invocation using its own receipt, and refuse any unexplained esptool log.
    recovery_logs = {basename(value) for path, value in fields(report.get("recovery")) if path[-1] == "log_path"}
    refused_logs = {basename(value) for path, value in fields(refused) if path[-1] == "log_path"}
    require(not refused_logs and logs.keys() <= recovery_logs, "The refusal evidence contains an esptool log for the action or an unattributed invocation.")
    return {"failed_step": 3, "action": action, "error_type": "device_busy", **holder, "esptool_log_present": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path, help="canonical report.json")
    parser.add_argument("--evidence", required=True, type=Path, help="job-named evidence ZIP")
    parser.add_argument("--manifest", required=True, type=Path, help="demo image manifest")
    parser.add_argument("--refusal", action="store_true", help="report a supplemental console refusal")
    args = parser.parse_args(argv)
    try:
        report = json.loads(args.report.read_bytes())
        manifest = json.loads(args.manifest.read_bytes())
        with ZipFile(args.evidence) as archive:
            summary = json.loads(archive.read("run-summary.json"))
            logs = esptool_members(archive)
        check_privacy(report, summary, manifest, *logs.values())
        public = public_header(report, summary, args.evidence.name)
        require(report["cleanup_ok"] is True and report["audit_ok"] is True and report["cleanup_errors"] == []
                and summary["run"]["cleanup_ok"] is True and summary["run"]["audit_ok"] is True, "The job lacks clean audit and cleanup receipts.")
        images = image_records(manifest)
        public.update(refusal(report, summary, logs) if args.refusal else qualification(report, summary, images, logs))
    except InvalidEvidence as error:
        print(error, file=sys.stderr)
        return 1
    except (BadZipFile, OSError, KeyError, TypeError, ValueError, AttributeError, RuntimeError):
        # A path, input field name or input value could be private.
        print("Could not read the required Nano ESP32-S3 evidence.", file=sys.stderr)
        return 1
    print(json.dumps(public, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
