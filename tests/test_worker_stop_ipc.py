from __future__ import annotations

import threading
import time

import pytest

from aicf.worker_stop_ipc import (
    StopRequestMonitor,
    WorkerIdentityError,
    stop_request_path,
)


def test_stop_request_path_rejects_unsafe_instance_id(tmp_path) -> None:
    for value in ("", "../escape", "a/b", "x" * 65):
        with pytest.raises(WorkerIdentityError, match="令牌"):
            stop_request_path(tmp_path, value)


def test_monitor_ignores_other_instance(tmp_path) -> None:
    terminated = threading.Event()
    own = stop_request_path(tmp_path, "instance-a")
    other = stop_request_path(tmp_path, "instance-b")
    other.parent.mkdir(parents=True)
    other.write_text("stop", encoding="utf-8")

    with StopRequestMonitor(
        tmp_path,
        "instance-a",
        terminate_self=lambda: terminated.set(),
        poll_interval=0.01,
    ):
        time.sleep(0.03)
        assert terminated.is_set() is False
        own.write_text("stop", encoding="utf-8")
        assert terminated.wait(timeout=1)

    assert other.is_file()


def test_monitor_handles_existing_request_and_writes_ack(tmp_path) -> None:
    request = stop_request_path(tmp_path, "instance-a")
    request.parent.mkdir(parents=True)
    request.write_text("stop", encoding="utf-8")
    terminated = threading.Event()

    with StopRequestMonitor(
        tmp_path,
        "instance-a",
        terminate_self=lambda: terminated.set(),
        poll_interval=0.5,
    ):
        assert terminated.wait(timeout=0.2)

    assert request.is_file()
    assert request.with_suffix(".ack").is_file()


def test_monitor_preserves_request_and_writes_error_on_failure(tmp_path) -> None:
    request = stop_request_path(tmp_path, "instance-a")
    request.parent.mkdir(parents=True)
    request.write_text("stop", encoding="utf-8")

    def fail() -> None:
        raise OSError("stop failed")

    with StopRequestMonitor(
        tmp_path,
        "instance-a",
        terminate_self=fail,
        poll_interval=0.01,
    ):
        deadline = time.monotonic() + 1
        while not request.with_suffix(".error").is_file():
            assert time.monotonic() < deadline
            time.sleep(0.01)

    assert request.is_file()
    assert "stop failed" in request.with_suffix(".error").read_text(
        encoding="utf-8"
    )
