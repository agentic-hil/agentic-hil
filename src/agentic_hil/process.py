from __future__ import annotations

import os
import signal
import subprocess
import time
from contextlib import suppress

CHILD_REAP_TIMEOUT_S = 5.0


def process_group_kwargs() -> dict[str, object]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | 0x00000004}
    return {"start_new_session": True}


def register_process_group(child: subprocess.Popen) -> subprocess.Popen:
    if os.name == "nt":
        job_handle: int | None = None
        try:
            job_handle = _create_windows_kill_job(child)
            if not isinstance(job_handle, int):
                raise OSError("Windows Job Object setup returned no containment handle.")
            child._agentic_hil_job_handle = job_handle
            _resume_windows_process(child)
        except BaseException:
            _abort_windows_registration(child, job_handle)
            raise
    else:
        try:
            child_pgid = os.getpgid(child.pid)
        except ProcessLookupError:
            child_pgid = child.pid
        child._agentic_hil_pgid = child_pgid
    return child


def terminate_process_tree(child: subprocess.Popen, timeout_s: float) -> None:
    if os.name == "nt":
        if getattr(child, "_agentic_hil_tree_reaped", False) is True:
            return
        job_handle = getattr(child, "_agentic_hil_job_handle", None)
        if isinstance(job_handle, int):
            _terminate_windows_job(job_handle)
            if not _wait_for_windows_job(job_handle, timeout_s):
                raise RuntimeError("Windows Job Object retained active processes after termination.")
            _close_windows_handle(job_handle)
            child._agentic_hil_job_handle = None
            child.wait(timeout=max(0.1, timeout_s))
            child._agentic_hil_tree_reaped = True
            return
        if child.poll() is not None:
            raise RuntimeError("Windows process tree was not registered before its leader exited.")
        subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        try:
            child.wait(timeout=max(0.1, timeout_s))
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            child.wait(timeout=max(0.1, timeout_s))
        if child.poll() is None:
            raise RuntimeError("Windows process tree remained active after taskkill.")
        child._agentic_hil_tree_reaped = True
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


def _create_windows_kill_job(child: subprocess.Popen) -> int:
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
        raise ctypes.WinError(ctypes.get_last_error())
    information = ExtendedLimitInformation()
    information.BasicLimitInformation.LimitFlags = 0x00002000
    configured = set_information(job, 9, ctypes.byref(information), ctypes.sizeof(information))
    if not configured:
        error = ctypes.WinError(ctypes.get_last_error())
        close_handle(job)
        raise error
    if not assign_process(job, wintypes.HANDLE(child._handle)):
        error = ctypes.WinError(ctypes.get_last_error())
        close_handle(job)
        raise error
    return int(job)


def _resume_windows_process(child: subprocess.Popen) -> None:
    import ctypes
    from ctypes import wintypes

    class ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    create_snapshot.restype = wintypes.HANDLE
    thread_first = kernel32.Thread32First
    thread_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry32)]
    thread_first.restype = wintypes.BOOL
    thread_next = kernel32.Thread32Next
    thread_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry32)]
    thread_next.restype = wintypes.BOOL
    open_thread = kernel32.OpenThread
    open_thread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_thread.restype = wintypes.HANDLE
    resume_thread = kernel32.ResumeThread
    resume_thread.argtypes = [wintypes.HANDLE]
    resume_thread.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    snapshot = create_snapshot(0x00000004, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    thread_handle = None
    try:
        entry = ThreadEntry32()
        entry.dwSize = ctypes.sizeof(entry)
        found = thread_first(snapshot, ctypes.byref(entry))
        while found:
            if entry.th32OwnerProcessID == child.pid:
                thread_handle = open_thread(0x0002, False, entry.th32ThreadID)
                break
            found = thread_next(snapshot, ctypes.byref(entry))
        if not thread_handle:
            raise ctypes.WinError(ctypes.get_last_error() or 1168)
        if resume_thread(thread_handle) == 0xFFFFFFFF:
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        if thread_handle:
            close_handle(thread_handle)
        close_handle(snapshot)


def _abort_windows_registration(child: subprocess.Popen, job_handle: int | None) -> None:
    if job_handle is not None:
        with suppress(OSError):
            _terminate_windows_job(job_handle)
        with suppress(OSError):
            _close_windows_handle(job_handle)
        child._agentic_hil_job_handle = None
    else:
        with suppress(OSError):
            child.kill()
    with suppress(subprocess.TimeoutExpired):
        child.wait(timeout=CHILD_REAP_TIMEOUT_S)


def _terminate_windows_job(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    terminate_job = ctypes.WinDLL("kernel32", use_last_error=True).TerminateJobObject
    terminate_job.argtypes = [wintypes.HANDLE, wintypes.UINT]
    terminate_job.restype = wintypes.BOOL
    if not terminate_job(wintypes.HANDLE(handle), 1):
        raise ctypes.WinError(ctypes.get_last_error())


def _wait_for_windows_job(handle: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.1, timeout_s)
    while True:
        if _windows_job_active_processes(handle) == 0:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _windows_job_active_processes(handle: int) -> int:
    import ctypes
    from ctypes import wintypes

    class BasicAccountingInformation(ctypes.Structure):
        _fields_ = [
            ("TotalUserTime", ctypes.c_longlong),
            ("TotalKernelTime", ctypes.c_longlong),
            ("ThisPeriodTotalUserTime", ctypes.c_longlong),
            ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
            ("TotalPageFaultCount", wintypes.DWORD),
            ("TotalProcesses", wintypes.DWORD),
            ("ActiveProcesses", wintypes.DWORD),
            ("TotalTerminatedProcesses", wintypes.DWORD),
        ]

    query_job = ctypes.WinDLL("kernel32", use_last_error=True).QueryInformationJobObject
    query_job.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    query_job.restype = wintypes.BOOL
    information = BasicAccountingInformation()
    returned = wintypes.DWORD()
    if not query_job(wintypes.HANDLE(handle), 1, ctypes.byref(information), ctypes.sizeof(information), ctypes.byref(returned)):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(information.ActiveProcesses)


def _close_windows_handle(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    if not close_handle(wintypes.HANDLE(handle)):
        raise ctypes.WinError(ctypes.get_last_error())
