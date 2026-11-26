"""Turn a fetched serve job into a shareable CubeProgrammer recording."""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from tests.support import CUBEPROGRAMMER_SN_ARGUMENT, CUBEPROGRAMMER_SN_BANNER

_PRIVATE_PATH = re.compile(
    r"(?<![\w\]])(?:[a-z]:[/\\]|\\\\|~[/\\]|/(?:home|users|root|tmp|temp|var/(?:tmp|folders)|private|dev|sys|proc|run|mnt|media)(?:[/\\]|$))"
    r"|(?<!\w)COM[1-9]\d*\b",
    re.IGNORECASE,
)
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\w)")
_PROGRAMMER_ACTIONS = {"flash", "reset", "read_symbol"}


def _map_strings(value, transform: Callable[[str], str]):
    if isinstance(value, dict):
        return {key: _map_strings(item, transform) for key, item in value.items()}
    if isinstance(value, list):
        return [_map_strings(item, transform) for item in value]
    return transform(value) if isinstance(value, str) else value


def check_host_privacy(text: str) -> str:
    if _PRIVATE_PATH.search(text) or any(
        all(int(octet) <= 255 for octet in candidate.split(".")) for candidate in _IPV4.findall(text)
    ):
        raise ValueError("Recording contains a private host path or IPv4 address")
    return text


def _zip_log(path: str) -> str:
    return "logs/" + PurePosixPath(path.replace("\\", "/")).name


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _uart_lines(archive: ZipFile, members: dict[str, None]) -> Iterator[tuple[datetime, str]]:
    for member in members:
        if member not in archive.namelist():
            continue
        partial = ""
        for line in archive.read(member).splitlines():
            event = json.loads(line)
            if event.get("direction") != "rx":
                continue
            *complete, partial = (partial + event["text"]).split("\n")
            for text in complete:
                yield _time(event["time"]), text.removesuffix("\r")


def _recorded_command(log: dict) -> list[str]:
    # command_for_log escapes double quotes and backslashes inside quoted
    # arguments. Its unquoted values may contain literal apostrophes or '#'.
    lexer = shlex.shlex(log["command"], posix=True)
    lexer.whitespace_split = True
    lexer.quotes = '"'
    lexer.commenters = ""
    return list(lexer)


def _probe_serial(logs: dict[str, dict]) -> str:
    serials = set()
    for log in logs.values():
        for field in ("command", "stdout", "stderr"):
            for pattern in (CUBEPROGRAMMER_SN_ARGUMENT, CUBEPROGRAMMER_SN_BANNER):
                for match in pattern.finditer(log[field]):
                    value = match[1].strip().strip('"')
                    if value and value.casefold() != "[redacted]":
                        serials.add(value.casefold())
    if len(serials) != 1:
        raise ValueError("Evidence must identify exactly one probe serial")
    return serials.pop()


def _stage_directory(argv: list[str], action: str) -> str | None:
    if action == "flash" and "-w" in argv:
        operand = argv[argv.index("-w") + 1]
    elif action == "read_symbol" and "-r" in argv:
        operand = argv[argv.index("-r") + 3]
    else:
        return None
    return re.split(r"[/\\][^/\\]+$", operand)[0] if re.search(r"[/\\]", operand) else None


def _programmer_event(step: dict, log: dict | None, *, last_flash: bool) -> dict:
    action, result = step["action"], step["result"]
    event = {"step": action}
    if action == "flash":
        artifact = result["artifact"]
        event.update(image=PurePosixPath(artifact["path"].replace("\\", "/")).name, image_sha256=artifact["sha256"])
    if action == "read_symbol":
        event.update(symbol=result["symbol"], value=f"{result['value_unsigned']:#010x}")
    if log is not None:
        event.update(argv=_recorded_command(log), returncode=log["returncode"])
        if last_flash:
            event.update({field: log[field] for field in ("timed_out", "stdout", "stderr")})
    return event


def _collect_events(
    steps: list[dict], logs: dict[str, dict], uart: list[tuple[datetime, str]]
) -> tuple[list[dict], set[str]]:
    events: list[dict] = []
    staging = set()
    calls = [step for step in steps if step["action"] in _PROGRAMMER_ACTIONS]
    last_flash = next((step for step in reversed(calls) if step["action"] == "flash"), None)
    uart_index = 0
    for step in calls:
        action, result = step["action"], step["result"]
        log = logs.get(_zip_log(result.get("log_path", "")))
        if log is None and action != "read_symbol":
            raise ValueError(f"Evidence has no programmer log for {action}")
        if started_at := result.get("started_at"):
            start = _time(started_at)
            first = uart_index
            while uart_index < len(uart) and uart[uart_index][0] < start:
                uart_index += 1
            if uart_index > first:
                events.append({"uart": uart[uart_index - 1][1]})
        event = _programmer_event(step, log, last_flash=step is last_flash)
        if directory := _stage_directory(event.get("argv", []), action):
            staging.add(directory)
        events.append(event)
    if uart_index < len(uart):
        events.append({"uart": uart[-1][1]})
    return events, staging


def record_job(fetched_dir: Path) -> dict:
    """Return the recording for one directory written by remote fetch."""
    bundles = sorted(fetched_dir.glob("*evidence.zip"))
    if len(bundles) != 1:
        raise ValueError("Expected exactly one evidence ZIP")
    report = json.loads((fetched_dir / "report.json").read_bytes())
    steps = report["steps"]
    with ZipFile(bundles[0]) as archive:
        summary = json.loads(archive.read("run-summary.json"))
        logs = {
            name: json.loads(archive.read(name))
            for name in archive.namelist()
            if name.startswith("logs/stlink-") and name.endswith(".log") and not name.startswith("logs/stlink-server-")
        }
        uart_members = dict.fromkeys(
            _zip_log(path)
            for step in steps
            if step["action"] in ("uart_open", "uart_close")
            and (path := (step["result"].get("session") or {}).get("log_path"))
        )
        uart = sorted(_uart_lines(archive, uart_members), key=lambda line: line[0])

    serial = _probe_serial(logs)
    events, staging = _collect_events(steps, logs, uart)
    recording = {
        "job_id": report["job_id"],
        "agentic_hil": summary["tools"]["agentic_hil"],
        "bench_config": summary["bench"]["config_digest"],
        "plan_sha256": summary["plan"]["sha256"],
        "outcome": summary["outcome"],
        "events": events,
    }

    def sanitize(text: str) -> str:
        for directory in sorted(staging, key=len, reverse=True):
            text = re.sub(re.escape(directory) + r"[/\\]", "[staging]/", text)
        return check_host_privacy(re.sub(re.escape(serial), "[redacted]", text, flags=re.IGNORECASE))

    return _map_strings(recording, sanitize)
