from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import weakref
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


@dataclass(slots=True)
class _OwnedProcessState:
    windows_job_handle: int | None = None
    posix_group_id: int | None = None
    stop_result: bool | None = None
    stop_lock: threading.Lock = field(default_factory=threading.Lock)


_OWNED_PROCESSES: weakref.WeakKeyDictionary[
    subprocess.Popen[Any], _OwnedProcessState
] = weakref.WeakKeyDictionary()
_OWNED_PROCESSES_LOCK = threading.RLock()
_WINDOWS_JOB_BOOTSTRAP = (
    "import subprocess,sys\n"
    "if sys.stdin.buffer.read(1) != b'1': raise SystemExit(125)\n"
    "child=subprocess.Popen(sys.argv[1:], stdin=subprocess.DEVNULL)\n"
    "raise SystemExit(child.wait())\n"
)


def process_group_options() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}


def _assign_windows_kill_job(process: subprocess.Popen[Any]) -> int | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
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
    create_job.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    create_job.restype = wintypes.HANDLE
    set_job = kernel32.SetInformationJobObject
    set_job.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    set_job.restype = wintypes.BOOL
    assign = kernel32.AssignProcessToJobObject
    assign.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    assign.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_job(None, None)
    if not handle:
        return None
    info = ExtendedLimitInformation()
    info.BasicLimitInformation.LimitFlags = 0x00002000
    if not set_job(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        close_handle(handle)
        return None
    process_handle = wintypes.HANDLE(int(getattr(process, "_handle", 0) or 0))
    if not process_handle or not assign(handle, process_handle):
        close_handle(handle)
        return None
    return int(handle)


def spawn_owned_process(
    command: list[str],
    **kwargs: Any,
) -> subprocess.Popen[Any]:
    if os.name != "nt":
        process = subprocess.Popen(command, **kwargs)
        with _OWNED_PROCESSES_LOCK:
            _OWNED_PROCESSES[process] = _OwnedProcessState(
                posix_group_id=process.pid,
            )
        return process

    # The bootstrap starts the target only after it belongs to the Job Object.
    # This closes the spawn-to-assignment race for fast descendant creation.
    kwargs.pop("stdin", None)
    # A Windows venv executable can be a launcher that creates the real Python
    # process immediately. Starting the bootstrap through that launcher would
    # reopen the spawn-to-Job-assignment race that the handshake closes.
    bootstrap_executable = str(getattr(sys, "_base_executable", "") or sys.executable)
    process = subprocess.Popen(
        [bootstrap_executable, "-c", _WINDOWS_JOB_BOOTSTRAP, *command],
        stdin=subprocess.PIPE,
        **kwargs,
    )
    job_handle = _assign_windows_kill_job(process)
    if job_handle is None:
        process.terminate()
        process.wait(timeout=10.0)
        raise OSError("windows_job_assignment_failed")
    with _OWNED_PROCESSES_LOCK:
        _OWNED_PROCESSES[process] = _OwnedProcessState(
            windows_job_handle=job_handle,
        )
    try:
        if process.stdin is None:
            raise OSError("windows_job_bootstrap_unavailable")
        text_mode = bool(
            kwargs.get("text")
            or kwargs.get("universal_newlines")
            or kwargs.get("encoding")
        )
        process.stdin.write("1" if text_mode else b"1")
        process.stdin.flush()
        process.stdin.close()
        process.stdin = None
    except BaseException:
        stop_owned_process_tree(process)
        raise
    return process


def _terminate_windows_job(job_handle: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    terminate_job = kernel32.TerminateJobObject
    terminate_job.argtypes = (wintypes.HANDLE, wintypes.UINT)
    terminate_job.restype = wintypes.BOOL
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_for_single_object.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = wintypes.HANDLE(job_handle)
    terminated = bool(terminate_job(handle, 1))
    wait_result = wait_for_single_object(handle, 10_000) if terminated else 0xFFFFFFFF
    closed = bool(close_handle(handle))
    return terminated and wait_result == 0x00000000 and closed


def _open_windows_process_handles(
    process_ids: set[int],
) -> tuple[dict[int, int], set[int]]:
    """Pin owned process identities before their PIDs can be reused."""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE

    access = 0x0001 | 0x00100000  # PROCESS_TERMINATE | SYNCHRONIZE
    handles: dict[int, int] = {}
    unavailable: set[int] = set()
    for process_id in sorted(process_ids):
        handle = open_process(access, False, process_id)
        if handle:
            handles[process_id] = int(handle)
        elif process_id in windows_process_snapshot():
            unavailable.add(process_id)
    return handles, unavailable


def _terminate_windows_process_handles(handles: Mapping[int, int]) -> bool:
    """Terminate and wait for the exact processes represented by handles."""

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    terminate_process = kernel32.TerminateProcess
    terminate_process.argtypes = (wintypes.HANDLE, wintypes.UINT)
    terminate_process.restype = wintypes.BOOL
    wait_for_single_object = kernel32.WaitForSingleObject
    wait_for_single_object.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    wait_for_single_object.restype = wintypes.DWORD
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    wait_timeout = 0x00000102
    success = True
    native_handles = [wintypes.HANDLE(value) for value in handles.values()]
    try:
        for handle in native_handles:
            if wait_for_single_object(handle, 0) == wait_timeout:
                terminate_process(handle, 1)
        for handle in native_handles:
            if wait_for_single_object(handle, 10_000) != 0x00000000:
                success = False
    finally:
        for handle in native_handles:
            if not close_handle(handle):
                success = False
    return success


def windows_process_snapshot() -> dict[int, int]:
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    create_snapshot.restype = wintypes.HANDLE
    process_first = kernel32.Process32FirstW
    process_first.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W))
    process_first.restype = wintypes.BOOL
    process_next = kernel32.Process32NextW
    process_next.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W))
    process_next.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = create_snapshot(0x00000002, 0)
    if handle == wintypes.HANDLE(-1).value:
        return {}
    processes: dict[int, int] = {}
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        if not process_first(handle, ctypes.byref(entry)):
            return {}
        while True:
            processes[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            if not process_next(handle, ctypes.byref(entry)):
                break
    finally:
        close_handle(handle)
    return processes


def _windows_process_tree_pids(root_pid: int) -> set[int]:
    snapshot = windows_process_snapshot()
    discovered = {int(root_pid)}
    frontier = [int(root_pid)]
    while frontier:
        parent = frontier.pop()
        children = {
            pid
            for pid, parent_pid in snapshot.items()
            if parent_pid == parent and pid not in discovered
        }
        discovered.update(children)
        frontier.extend(children)
    return {pid for pid in discovered if pid in snapshot}


def _posix_group_stopped(group_id: int) -> bool:
    try:
        os.killpg(group_id, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def stop_owned_process_tree(process: subprocess.Popen[Any] | None) -> bool:
    if process is None:
        return True
    with _OWNED_PROCESSES_LOCK:
        state = _OWNED_PROCESSES.get(process)
    if state is None:
        # A Popen instance that was not created here has no trustworthy tree
        # identity. Its PID may already have been reused, so fail closed.
        return False

    with state.stop_lock:
        if state.stop_result is not None:
            return state.stop_result

        if os.name == "nt":
            job_handle = state.windows_job_handle
            stopped = bool(
                job_handle is not None and _terminate_windows_job(job_handle)
            )
            state.windows_job_handle = None
            if stopped:
                try:
                    process.wait(timeout=10.0)
                except (OSError, subprocess.TimeoutExpired):
                    stopped = False
            state.stop_result = stopped
            return stopped

        group_id = state.posix_group_id
        if group_id is None:
            state.stop_result = False
            return False
        try:
            os.killpg(group_id, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if _posix_group_stopped(group_id):
                break
            time.sleep(0.05)
        if not _posix_group_stopped(group_id):
            try:
                os.killpg(group_id, signal.SIGKILL)
            except OSError:
                pass
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline and not _posix_group_stopped(group_id):
                time.sleep(0.05)
        try:
            process.wait(timeout=10.0)
        except (OSError, subprocess.TimeoutExpired):
            state.stop_result = False
            return False
        state.stop_result = _posix_group_stopped(group_id)
        return state.stop_result


def remove_owned_directory(
    directory: Path,
    *,
    required_prefix: str,
    timeout: float = 10.0,
) -> bool:
    """Remove one explicitly owned directory without following a symlink."""

    try:
        resolved = directory.resolve(strict=True)
    except OSError:
        return not directory.exists()
    if not required_prefix or not resolved.name.startswith(required_prefix):
        return False
    if resolved.is_symlink() or not resolved.is_dir():
        return False
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            shutil.rmtree(resolved)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)


def communicate_with_tree_cleanup(
    process: subprocess.Popen[Any],
    *,
    timeout: float,
) -> tuple[Any, Any]:
    try:
        return process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_owned_process_tree(process)
        raise
