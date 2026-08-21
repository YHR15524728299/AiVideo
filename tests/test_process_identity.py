from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import Mock

import aicf.process_identity as process_identity
from aicf.process_identity import (
    ProcessIdentity,
    ProcessProbe,
    ProcessProbeStatus,
    get_process_identity,
    probe_process_identity,
    process_identity_matches,
    process_is_running,
)


def _fake_kernel32(*, handle: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        OpenProcess=Mock(return_value=handle),
        GetExitCodeProcess=Mock(return_value=1),
        CloseHandle=Mock(return_value=1),
        GetProcessTimes=Mock(return_value=1),
        QueryFullProcessImageNameW=Mock(return_value=1),
    )


def _use_fake_windows(monkeypatch, kernel32: SimpleNamespace) -> None:
    monkeypatch.setattr(process_identity, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        process_identity.ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: kernel32,
        raising=False,
    )


def test_invalid_pid_is_not_running() -> None:
    probe = probe_process_identity(0)
    assert probe == ProcessProbe(status=ProcessProbeStatus.NOT_RUNNING)


def test_current_process_has_complete_identity() -> None:
    probe = probe_process_identity(os.getpid())
    assert probe.status == ProcessProbeStatus.RUNNING
    assert probe.identity is not None
    assert probe.identity.pid == os.getpid()
    assert probe.identity.created_at_ns > 0
    assert probe.identity.executable


def test_posix_probe_reports_missing_proc_directory(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        process_identity,
        "os",
        SimpleNamespace(name="posix"),
    )
    monkeypatch.setattr(
        process_identity,
        "Path",
        lambda _value: tmp_path / "missing-proc-directory",
    )

    probe = probe_process_identity(123)

    assert probe == ProcessProbe(status=ProcessProbeStatus.NOT_RUNNING)


def test_posix_probe_reads_complete_identity(monkeypatch, tmp_path) -> None:
    proc_dir = tmp_path / "proc-entry"
    proc_dir.mkdir()
    stat_fields = ["123", "(python)", "R", *(["0"] * 18), "987654"]
    (proc_dir / "stat").write_text(" ".join(stat_fields), encoding="utf-8")
    executable = proc_dir / "exe"
    executable.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        process_identity,
        "os",
        SimpleNamespace(name="posix"),
    )
    monkeypatch.setattr(process_identity, "Path", lambda _value: proc_dir)

    probe = probe_process_identity(123)

    assert probe == ProcessProbe(
        status=ProcessProbeStatus.RUNNING,
        identity=ProcessIdentity(
            pid=123,
            created_at_ns=987654,
            executable=str(executable.resolve()),
        ),
    )


def test_posix_probe_reports_read_failure_as_unknown(monkeypatch, tmp_path) -> None:
    proc_dir = tmp_path / "unreadable-proc-entry"
    proc_dir.mkdir()
    monkeypatch.setattr(process_identity, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(process_identity, "Path", lambda _value: proc_dir)

    probe = probe_process_identity(123)

    assert probe == ProcessProbe(status=ProcessProbeStatus.UNKNOWN)


def test_windows_probe_reports_error_87_as_not_running(monkeypatch) -> None:
    kernel32 = _fake_kernel32(handle=None)
    _use_fake_windows(monkeypatch, kernel32)
    monkeypatch.setattr(
        process_identity.ctypes,
        "get_last_error",
        lambda: 87,
        raising=False,
    )

    probe = probe_process_identity(123)

    assert probe == ProcessProbe(status=ProcessProbeStatus.NOT_RUNNING)


def test_windows_probe_reports_other_open_error_as_unknown(monkeypatch) -> None:
    kernel32 = _fake_kernel32(handle=None)
    _use_fake_windows(monkeypatch, kernel32)
    monkeypatch.setattr(
        process_identity.ctypes,
        "get_last_error",
        lambda: 5,
        raising=False,
    )

    probe = probe_process_identity(123)

    assert probe == ProcessProbe(status=ProcessProbeStatus.UNKNOWN)


def test_windows_probe_reports_exited_process_as_not_running(monkeypatch) -> None:
    kernel32 = _fake_kernel32(handle=42)

    def set_exit_code(_handle, exit_code) -> int:
        exit_code._obj.value = 0
        return 1

    kernel32.GetExitCodeProcess.side_effect = set_exit_code
    _use_fake_windows(monkeypatch, kernel32)

    probe = probe_process_identity(123)

    assert probe == ProcessProbe(status=ProcessProbeStatus.NOT_RUNNING)
    kernel32.CloseHandle.assert_called_once_with(42)


def test_windows_probe_reads_complete_running_identity(monkeypatch) -> None:
    kernel32 = _fake_kernel32(handle=42)

    def set_exit_code(_handle, exit_code) -> int:
        exit_code._obj.value = 259
        return 1

    def set_creation_time(
        _handle,
        creation,
        _exit_time,
        _kernel_time,
        _user_time,
    ) -> int:
        creation._obj.low = 7
        creation._obj.high = 2
        return 1

    def set_image_name(_handle, _flags, image, _size) -> int:
        image.value = r"C:\Python\python.exe"
        return 1

    kernel32.GetExitCodeProcess.side_effect = set_exit_code
    kernel32.GetProcessTimes.side_effect = set_creation_time
    kernel32.QueryFullProcessImageNameW.side_effect = set_image_name
    _use_fake_windows(monkeypatch, kernel32)
    monkeypatch.setattr(
        process_identity,
        "Path",
        lambda _value: SimpleNamespace(
            resolve=lambda: r"C:\resolved\python.exe",
        ),
    )

    probe = probe_process_identity(123)

    assert probe == ProcessProbe(
        status=ProcessProbeStatus.RUNNING,
        identity=ProcessIdentity(
            pid=123,
            created_at_ns=((2 << 32) | 7) * 100,
            executable=r"C:\resolved\python.exe",
        ),
    )
    kernel32.CloseHandle.assert_called_once_with(42)


def test_identity_helpers_only_accept_running_probe(monkeypatch) -> None:
    identity = ProcessIdentity(
        pid=123,
        created_at_ns=456,
        executable="python.exe",
    )
    monkeypatch.setattr(
        "aicf.process_identity.probe_process_identity",
        lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=identity,
        ),
    )
    assert get_process_identity(123) == identity
    assert process_is_running(123) is True

    monkeypatch.setattr(
        "aicf.process_identity.probe_process_identity",
        lambda _pid: ProcessProbe(status=ProcessProbeStatus.UNKNOWN),
    )
    assert get_process_identity(123) is None
    assert process_is_running(123) is False

    monkeypatch.setattr(
        "aicf.process_identity.probe_process_identity",
        lambda _pid: ProcessProbe(status=ProcessProbeStatus.NOT_RUNNING),
    )
    assert get_process_identity(123) is None
    assert process_is_running(123) is False


def test_process_identity_matches_complete_normalized_identity() -> None:
    identity = ProcessIdentity(
        pid=123,
        created_at_ns=456,
        executable=r"C:\Python\PYTHON.EXE",
    )

    assert process_identity_matches(
        identity,
        pid=123,
        created_at_ns=456,
        executable=r"c:\python\python.exe",
    )
    assert not process_identity_matches(
        identity,
        pid=123,
        created_at_ns=999,
        executable=r"c:\python\python.exe",
    )
    assert not process_identity_matches(
        None,
        pid=123,
        created_at_ns=456,
        executable=r"c:\python\python.exe",
    )
