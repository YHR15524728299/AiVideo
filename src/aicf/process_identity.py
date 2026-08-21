from __future__ import annotations

import ctypes
import os
from enum import Enum
from pathlib import Path

from pydantic import BaseModel


class ProcessIdentity(BaseModel):
    pid: int
    created_at_ns: int
    executable: str


class ProcessProbeStatus(str, Enum):
    RUNNING = "RUNNING"
    NOT_RUNNING = "NOT_RUNNING"
    UNKNOWN = "UNKNOWN"


class ProcessProbe(BaseModel):
    status: ProcessProbeStatus
    identity: ProcessIdentity | None = None


def process_identity_matches(
    identity: ProcessIdentity | None,
    *,
    pid: int,
    created_at_ns: int | None,
    executable: str | None,
) -> bool:
    """Return whether a complete recorded identity matches a live process."""
    return (
        identity is not None
        and created_at_ns is not None
        and executable is not None
        and identity.pid == pid
        and identity.created_at_ns == created_at_ns
        and os.path.normcase(identity.executable)
        == os.path.normcase(executable)
    )


def probe_process_identity(pid: int) -> ProcessProbe:
    if pid <= 0:
        return ProcessProbe(status=ProcessProbeStatus.NOT_RUNNING)
    if os.name == "nt":
        return _probe_windows_process(pid)
    return _probe_proc_process(pid)


def _probe_windows_process(pid: int) -> ProcessProbe:
    process_query_limited_information = 0x1000
    still_active = 259

    class FileTime(ctypes.Structure):
        _fields_ = [
            ("low", ctypes.c_ulong),
            ("high", ctypes.c_ulong),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.GetExitCodeProcess.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.GetExitCodeProcess.restype = ctypes.c_int
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_int
    kernel32.GetProcessTimes.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
        ctypes.POINTER(FileTime),
    ]
    kernel32.GetProcessTimes.restype = ctypes.c_int
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    kernel32.QueryFullProcessImageNameW.restype = ctypes.c_int
    handle = kernel32.OpenProcess(
        process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        error_code = ctypes.get_last_error()
        status = (
            ProcessProbeStatus.NOT_RUNNING
            if error_code == 87
            else ProcessProbeStatus.UNKNOWN
        )
        return ProcessProbe(status=status)
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return ProcessProbe(status=ProcessProbeStatus.UNKNOWN)
        if exit_code.value != still_active:
            return ProcessProbe(status=ProcessProbeStatus.NOT_RUNNING)
        creation = FileTime()
        exit_time = FileTime()
        kernel_time = FileTime()
        user_time = FileTime()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return ProcessProbe(status=ProcessProbeStatus.UNKNOWN)
        size = ctypes.c_ulong(32768)
        image = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            image,
            ctypes.byref(size),
        ):
            return ProcessProbe(status=ProcessProbeStatus.UNKNOWN)
        created_at_ns = ((creation.high << 32) | creation.low) * 100
        return ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=ProcessIdentity(
                pid=pid,
                created_at_ns=created_at_ns,
                executable=str(Path(image.value).resolve()),
            ),
        )
    finally:
        kernel32.CloseHandle(handle)


def _probe_proc_process(pid: int) -> ProcessProbe:
    proc_dir = Path(f"/proc/{pid}")
    if not proc_dir.exists():
        return ProcessProbe(status=ProcessProbeStatus.NOT_RUNNING)
    try:
        stat_fields = (proc_dir / "stat").read_text(encoding="utf-8").split()
        executable = str((proc_dir / "exe").resolve(strict=True))
    except (OSError, SystemError):
        return ProcessProbe(status=ProcessProbeStatus.UNKNOWN)
    return ProcessProbe(
        status=ProcessProbeStatus.RUNNING,
        identity=ProcessIdentity(
            pid=pid,
            created_at_ns=int(stat_fields[21]),
            executable=executable,
        ),
    )


def get_process_identity(pid: int) -> ProcessIdentity | None:
    probe = probe_process_identity(pid)
    return probe.identity if probe.status == ProcessProbeStatus.RUNNING else None


def process_is_running(pid: int) -> bool:
    return probe_process_identity(pid).status == ProcessProbeStatus.RUNNING
