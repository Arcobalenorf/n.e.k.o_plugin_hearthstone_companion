from __future__ import annotations

import os
from pathlib import Path


def hearthstone_executable_paths() -> tuple[Path, ...]:
    """Return running Hearthstone executable paths without spawning helper processes."""

    if os.name != "nt":
        return ()

    try:
        return _windows_hearthstone_executable_paths()
    except (OSError, ValueError):
        return ()


def _windows_hearthstone_executable_paths() -> tuple[Path, ...]:
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")

    paths: list[Path] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        found = bool(kernel32.Process32FirstW(snapshot, ctypes.byref(entry)))
        while found:
            if str(entry.szExeFile).casefold() == "hearthstone.exe":
                process = kernel32.OpenProcess(0x1000, False, entry.th32ProcessID)
                if process:
                    try:
                        capacity = wintypes.DWORD(32768)
                        buffer = ctypes.create_unicode_buffer(capacity.value)
                        if kernel32.QueryFullProcessImageNameW(
                            process, 0, buffer, ctypes.byref(capacity)
                        ):
                            path = Path(buffer.value)
                            if path.name.casefold() == "hearthstone.exe":
                                paths.append(path)
                    finally:
                        kernel32.CloseHandle(process)
            if len(paths) >= 8:
                break
            found = bool(kernel32.Process32NextW(snapshot, ctypes.byref(entry)))
    finally:
        kernel32.CloseHandle(snapshot)

    return tuple(dict.fromkeys(path.resolve() for path in paths))
