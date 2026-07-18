from __future__ import annotations

import os
import signal
import subprocess
import time
from contextlib import suppress

CHILD_REAP_TIMEOUT_S = 5.0


def process_group_kwargs() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def register_process_group(child: subprocess.Popen) -> subprocess.Popen:
    if os.name == "nt":
        job_handle = _create_windows_kill_job(child)
        if job_handle is not None:
            child._agentic_hil_job_handle = job_handle
    else:
        try:
            child_pgid = os.getpgid(child.pid)
        except ProcessLookupError:
            child_pgid = child.pid
        child._agentic_hil_pgid = child_pgid
    return child


def terminate_process_tree(child: subprocess.Popen, timeout_s: float) -> None:
    if os.name == "nt":
        job_handle = getattr(child, "_agentic_hil_job_handle", None)
        if isinstance(job_handle, int):
            _close_windows_handle(job_handle)
            child._agentic_hil_job_handle = None
        elif child.poll() is None:
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        with suppress(subprocess.TimeoutExpired):
            child.wait(timeout=max(0.1, timeout_s))
        if child.poll() is None:
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            child.wait(timeout=max(0.1, timeout_s))
        return

    child_pgid = _child_process_group(child)
    if child_pgid is None or child_pgid == os.getpgrp():
        _terminate_single_child(child, timeout_s)
        return

    with suppress(ProcessLookupError):
        os.killpg(child_pgid, signal.SIGTERM)
    if _wait_for_process_group(child, child_pgid, timeout_s):
        return
    with suppress(ProcessLookupError):
        os.killpg(child_pgid, signal.SIGKILL)
    if not _wait_for_process_group(child, child_pgid, timeout_s):
        _terminate_single_child(child, timeout_s)
        if _process_group_exists(child_pgid):
            raise RuntimeError("Process group remained active after SIGKILL.")


def _child_process_group(child: subprocess.Popen) -> int | None:
    stored = getattr(child, "_agentic_hil_pgid", None)
    if isinstance(stored, int):
        return stored
    if child.poll() is not None:
        return child.pid
    try:
        return os.getpgid(child.pid)
    except ProcessLookupError:
        return None


def _terminate_single_child(child: subprocess.Popen, timeout_s: float) -> None:
    if child.poll() is None:
        with suppress(ProcessLookupError):
            child.terminate()
    try:
        child.wait(timeout=max(0.1, timeout_s))
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            child.kill()
        child.wait(timeout=max(0.1, timeout_s))


def _wait_for_process_group(child: subprocess.Popen, pgid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.1, timeout_s)
    while True:
        with suppress(subprocess.TimeoutExpired):
            child.wait(timeout=0)
        if child.poll() is not None and not _process_group_exists(pgid):
            return True
        if time.monotonic() >= deadline:
            return child.poll() is not None and not _process_group_exists(pgid)
        time.sleep(0.02)


def _process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _create_windows_kill_job(child: subprocess.Popen) -> int | None:
    import ctypes
    from ctypes import wintypes

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BasicLimitInformation),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_job = kernel32.CreateJobObjectW
    create_job.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    create_job.restype = wintypes.HANDLE
    set_information = kernel32.SetInformationJobObject
    set_information.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    set_information.restype = wintypes.BOOL
    assign_process = kernel32.AssignProcessToJobObject
    assign_process.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    assign_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    job = create_job(None, None)
    if not job:
        return None
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00002000
    configured = set_information(job, 9, ctypes.byref(information), ctypes.sizeof(information))
    assigned = configured and assign_process(job, wintypes.HANDLE(child._handle))
    if not assigned:
        close_handle(job)
        return None
    return int(job)


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not close_handle(wintypes.HANDLE(handle)):
        raise ctypes.WinError(ctypes.get_last_error())
