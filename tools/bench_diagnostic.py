"""Read-only checks for the self-hosted Linux bench runner.

Probe discovery reuses the bench runner's USB vendor/product allowlist and
sysfs scan. This helper only reads sysfs and asks sudo whether it could
read/write exact sysfs attributes; it never writes an attribute or changes host
configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

RELEASE_SHA256 = "6a9e60a5a048c45eb3241f9bb66bdc2e6cbd0119fb2e42568dc059fc6167442a"
RELEASE_SIZE = 252_673_523
RELEASE_ARCHIVE_NAME = "SetupSTM32CubeProgrammer_linux_64.zip"
_DECIMAL_ID = re.compile(r"^[1-9][0-9]*$")


def validate_asset_id(asset_id: str) -> str:
    if not isinstance(asset_id, str) or not _DECIMAL_ID.fullmatch(asset_id):
        raise ValueError("asset id must be a positive decimal number")
    return asset_id


def _safe_urlopen(request: Request):
    """Follow HTTPS redirects, dropping credentials on every host change."""

    class RedirectHandler(HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            old = urlsplit(req.full_url)
            new = urlsplit(newurl)
            if new.scheme != "https" or not new.hostname:
                raise HTTPError(newurl, code, "refusing non-HTTPS asset redirect", headers, fp)
            redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
            if redirected is not None and (old.hostname or "").lower() != new.hostname.lower():
                redirected.remove_header("Authorization")
            return redirected

    initial = urlsplit(request.full_url)
    if initial.scheme != "https" or initial.hostname != "api.github.com":
        raise ValueError("asset download URL is not the GitHub API")
    return build_opener(RedirectHandler).open(request, timeout=120)


def _download_release_asset(asset_id: str, token: str, destination: Path) -> None:
    validate_asset_id(asset_id)
    if not token:
        raise ValueError("GitHub token is missing")
    asset_url = f"https://api.github.com/repos/agentic-hil/agentic-hil/releases/assets/{asset_id}"
    request = Request(
        asset_url,
        headers={
            "Accept": "application/octet-stream",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "agentic-hil-bench-diagnostic",
        },
    )
    with _safe_urlopen(request) as response, destination.open("wb") as output:
        while block := response.read(1024 * 1024):
            output.write(block)


def cache_cubeprogrammer_archive(
    asset_id: str,
    token: str,
    target: Path,
    *,
    expected_sha256: str = RELEASE_SHA256,
    expected_size: int | None = RELEASE_SIZE,
) -> dict[str, Any]:
    """Download to a sibling temporary file, verify, then atomically replace."""
    validate_asset_id(asset_id)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, partial_name = tempfile.mkstemp(prefix=".cubeprogrammer-", suffix=".part", dir=target.parent)
    os.close(descriptor)
    partial = Path(partial_name)
    try:
        _download_release_asset(asset_id, token, partial)
        digest = hashlib.sha256()
        size = 0
        with partial.open("rb") as source:
            while block := source.read(1024 * 1024):
                digest.update(block)
                size += len(block)
        actual_sha256 = digest.hexdigest()
        if actual_sha256.lower() != expected_sha256.lower():
            raise ValueError("CubeProgrammer asset SHA-256 does not match the pinned digest")
        if expected_size is not None and size != expected_size:
            raise ValueError("CubeProgrammer asset size does not match the pinned size")
        with partial.open("r+b") as source:
            os.fsync(source.fileno())
        os.replace(partial, target)
        return {"downloaded": True, "sha256": actual_sha256, "size_bytes": size}
    finally:
        partial.unlink(missing_ok=True)


def _run_quietly(command: list[str], *, timeout: int = 15) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def discover_bench_probes():
    """Reuse the bench runner's VID/PID allowlist and serial-aware sysfs scan."""
    tools_dir = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("_agentic_hil_bench_runner", tools_dir / "bench_in_container.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("bench runner discovery is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.discover_probes()


def match_stlink_sysfs(probes: list[Any]) -> dict[str, Any]:
    """Select one allowlisted bench probe; retain its serial only in memory."""
    matches = [probe for probe in probes if len(getattr(probe, "serial_numbers", ())) == 1 and probe.serial_numbers[0]]
    if len(matches) != 1:
        return {"status": "missing_or_ambiguous", "writes_performed": False}
    probe = matches[0]
    location = re.search(r"\(sysfs ([^)]+)\)$", getattr(probe, "where", ""))
    if location is None:
        return {"status": "sysfs_mapping_unavailable", "writes_performed": False}
    return {
        "status": "matched",
        "sysfs_name": location.group(1),
        "writes_performed": False,
    }


def _sudo_can_write(path: Path) -> bool | None:
    result = _run_quietly(["sudo", "-n", "test", "-w", str(path)], timeout=5)
    return None if result is None else result.returncode == 0


def inspect_kernel_permissions(sysfs_name: str) -> dict[str, Any]:
    device = Path("/sys/bus/usb/devices") / sysfs_name
    authorized = device / "authorized"
    try:
        authorized_value = authorized.read_text(encoding="ascii").strip()
        authorized_readable: bool | None = True
    except OSError:
        authorized_value = None
        authorized_readable = False
    drivers: list[dict[str, Any]] = []
    interface_root = device.parent
    try:
        interfaces = list(interface_root.glob(f"{sysfs_name}:*/driver"))
    except OSError:
        interfaces = []
    driver_links = [device / "driver", *interfaces]
    seen: set[str] = set()
    for link in driver_links:
        try:
            driver_name = link.resolve(strict=True).name
        except OSError:
            continue
        if driver_name in seen:
            continue
        seen.add(driver_name)
        driver_root = Path("/sys/bus/usb/drivers") / driver_name
        for operation in ("bind", "unbind"):
            attribute = driver_root / operation
            drivers.append({
                "operation": operation,
                "present": attribute.exists(),
                "user_writable": os.access(attribute, os.W_OK),
                "sudo_writable": _sudo_can_write(attribute) if attribute.exists() else None,
            })
    return {
        "authorized": authorized_value if authorized_value in {"0", "1"} else None,
        "authorized_readable": authorized_readable,
        "authorized_user_writable": os.access(authorized, os.W_OK),
        "authorized_sudo_writable": _sudo_can_write(authorized) if authorized.exists() else None,
        "driver_controls": drivers,
        "writes_performed": False,
    }


def probe_sudo_noprompt() -> dict[str, Any]:
    result = _run_quietly(["sudo", "-n", "id", "-u"], timeout=5)
    return {"available": result is not None and result.returncode == 0, "writes_performed": False}


def detect_rootless_runtimes() -> dict[str, bool | None]:
    result: dict[str, bool | None] = {"podman_rootless": None, "docker_rootless": None}
    podman = _run_quietly(["podman", "info", "--format", "{{.Host.Security.Rootless}}"], timeout=10)
    if podman is not None:
        result["podman_rootless"] = podman.returncode == 0 and podman.stdout.strip().lower() == "true"
    docker = _run_quietly(["docker", "info", "--format", "{{json .SecurityOptions}}"], timeout=10)
    if docker is not None and docker.returncode == 0:
        try:
            options = json.loads(docker.stdout)
            result["docker_rootless"] = any("rootless" in str(option).lower() for option in options)
        except (json.JSONDecodeError, TypeError):
            result["docker_rootless"] = False
    return result


def collect_read_only_diagnostic() -> dict[str, Any]:
    match = match_stlink_sysfs(discover_bench_probes())
    if match.get("status") == "matched":
        kernel = inspect_kernel_permissions(match["sysfs_name"])
        usb: dict[str, Any] = {
            "status": "matched",
            "authorized": kernel["authorized"],
            "authorized_readable": kernel["authorized_readable"],
            "authorized_user_writable": kernel["authorized_user_writable"],
            "authorized_sudo_writable": kernel["authorized_sudo_writable"],
            "driver_controls": kernel["driver_controls"],
            "writes_performed": False,
        }
    else:
        usb = match
    return {
        "probe_discovery": "one allowlisted ST-Link" if match.get("status") == "matched" else match["status"],
        "usb": usb,
        "sudo": probe_sudo_noprompt(),
        "runtimes": detect_rootless_runtimes(),
        "writes_performed": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-id", default="", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    report: dict[str, Any] = {"diagnostic": None, "cubeprogrammer_cache": None}
    status = 0
    try:
        report["diagnostic"] = collect_read_only_diagnostic()
    except Exception as exc:  # sanitized category only; subprocess detail can contain host identity
        report["diagnostic"] = {"ok": False, "error": type(exc).__name__, "writes_performed": False}
        status = 1
    if args.asset_id:
        try:
            token = os.environ.get("GH_TOKEN", "")
            target = Path.home() / ".cache" / "agentic-hil" / "toolchains" / "cubeprogrammer-2.23.0.zip"
            report["cubeprogrammer_cache"] = cache_cubeprogrammer_archive(args.asset_id, token, target)
        except Exception as exc:
            safe_error: dict[str, Any] = {"downloaded": False, "error": type(exc).__name__}
            if isinstance(exc, HTTPError):
                safe_error["http_status"] = exc.code
            elif isinstance(exc, URLError) and isinstance(exc.reason, HTTPError):
                safe_error["http_status"] = exc.reason.code
            report["cubeprogrammer_cache"] = safe_error
            status = 1
    print(json.dumps(report, sort_keys=True))
    return status


if __name__ == "__main__":
    sys.exit(main())
