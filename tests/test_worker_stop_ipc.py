from __future__ import annotations

import subprocess
import threading

import pytest

from aicf.worker_stop_ipc import (
    StopRequestMonitor,
    WorkerIdentityError,
    stop_request_path,
    terminate_current_process_tree,
    terminate_process_tree,
)


def test_stop_request_path_rejects_unsafe_instance_id(tmp_path) -> None:
    for value in ("", "../escape", "a/b", "x" * 65):
        with pytest.raises(WorkerIdentityError, match="令牌"):
            stop_request_path(tmp_path, value)


def test_terminate_process_tree_reports_windows_taskkill_failure() -> None:
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=7,
            stdout="",
            stderr="access denied",
        )

    with pytest.raises(WorkerIdentityError) as exc_info:
        terminate_process_tree(456, run=fake_run, platform_name="nt")

    message = str(exc_info.value)
    assert "7" in message
    assert "access denied" in message


def test_monitor_ignores_other_instance(tmp_path) -> None:
    cancelled = threading.Event()
    first_scan_complete = threading.Barrier(2)
    continue_polling = threading.Event()
    own = stop_request_path(tmp_path, "instance-a")
    other = stop_request_path(tmp_path, "instance-b")
    other.parent.mkdir(parents=True)
    other.write_text("stop", encoding="utf-8")

    def wait_for_next_poll(
        stopped: threading.Event,
        _poll_interval: float,
    ) -> bool:
        first_scan_complete.wait(timeout=1)
        while not stopped.is_set():
            if continue_polling.wait(timeout=1):
                return False
        return True

    with StopRequestMonitor(
        tmp_path,
        "instance-a",
        request_cancel=cancelled.set,
        wait_for_next_poll=wait_for_next_poll,
    ) as monitor:
        first_scan_complete.wait(timeout=1)
        assert monitor.stop_requested is False
        assert cancelled.is_set() is False
        own.write_text("stop", encoding="utf-8")
        continue_polling.set()
        assert cancelled.wait(timeout=1)

    assert other.is_file()
    assert monitor.stop_requested is True


def test_monitor_handles_existing_request_and_writes_ack(tmp_path) -> None:
    request = stop_request_path(tmp_path, "instance-a")
    request.parent.mkdir(parents=True)
    request.write_text("stop", encoding="utf-8")
    cancelled = threading.Event()

    with StopRequestMonitor(
        tmp_path,
        "instance-a",
        request_cancel=cancelled.set,
    ) as monitor:
        assert cancelled.wait(timeout=1)

    assert monitor.stop_requested
    assert request.is_file()
    assert not request.with_suffix(".ack").exists()


def test_monitor_only_observes_request_without_terminating_process(
    tmp_path,
) -> None:
    request = stop_request_path(tmp_path, "instance-a")
    request.parent.mkdir(parents=True)
    request.write_text("stop", encoding="utf-8")
    cancelled = threading.Event()

    with StopRequestMonitor(
        tmp_path,
        "instance-a",
        request_cancel=cancelled.set,
    ) as monitor:
        assert cancelled.wait(timeout=1)

    assert monitor.stop_requested
    assert request.is_file()
    assert not request.with_suffix(".error").exists()


def test_monitor_accepts_legacy_terminate_self_parameter(tmp_path) -> None:
    request = stop_request_path(tmp_path, "instance-a")
    request.parent.mkdir(parents=True)
    request.write_text("stop", encoding="utf-8")
    cancelled = threading.Event()

    with StopRequestMonitor(
        tmp_path,
        "instance-a",
        terminate_self=cancelled.set,
    ) as monitor:
        assert cancelled.wait(timeout=1)

    assert monitor.stop_requested


def test_legacy_terminate_current_process_tree_delegates_current_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        "aicf.worker_stop_ipc.terminate_process_tree",
        calls.append,
    )

    terminate_current_process_tree()

    assert len(calls) == 1
    assert calls[0] > 0
