from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from aicf.background_worker import (
    SleepInhibitor,
    WorkerLauncher,
    WorkerRecord,
    run_worker,
    process_is_running,
)


def test_sleep_inhibitor_releases_after_success() -> None:
    calls: list[int] = []

    with SleepInhibitor(set_state=lambda flags: calls.append(flags) or flags):
        assert calls

    assert calls[-1] == SleepInhibitor.ES_CONTINUOUS


def test_process_probe_does_not_terminate_current_process() -> None:
    assert process_is_running(os.getpid()) is True
    assert process_is_running(os.getpid()) is True


def test_sleep_inhibitor_releases_after_exception() -> None:
    calls: list[int] = []

    with pytest.raises(RuntimeError, match="boom"):
        with SleepInhibitor(set_state=lambda flags: calls.append(flags) or flags):
            raise RuntimeError("boom")

    assert calls[-1] == SleepInhibitor.ES_CONTINUOUS


def test_sleep_inhibitor_fails_closed_when_request_is_rejected() -> None:
    with pytest.raises(OSError, match="阻止系统睡眠"):
        with SleepInhibitor(set_state=lambda _flags: 0):
            pass


def test_launcher_reuses_running_worker(tmp_path: Path) -> None:
    record_path = tmp_path / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        WorkerRecord(job_id="JOB1", pid=123, started_at="now", log_path="x.log")
        .model_dump_json(),
        encoding="utf-8",
    )
    spawned: list[object] = []
    launcher = WorkerLauncher(
        python_executable="python",
        process_is_running=lambda pid: pid == 123,
        popen=lambda *args, **kwargs: spawned.append((args, kwargs)),
    )

    result = launcher.start("JOB1", tmp_path)

    assert result.pid == 123
    assert result.reused is True
    assert spawned == []


def test_launcher_replaces_stale_record(tmp_path: Path) -> None:
    record_path = tmp_path / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        WorkerRecord(job_id="JOB1", pid=123, started_at="old", log_path="old.log")
        .model_dump_json(),
        encoding="utf-8",
    )

    class Process:
        pid = 456

    launcher = WorkerLauncher(
        python_executable="python",
        process_is_running=lambda _pid: False,
        popen=lambda *args, **kwargs: Process(),
    )
    result = launcher.start("JOB1", tmp_path)

    assert result.pid == 456
    assert result.reused is False
    saved = json.loads(record_path.read_text(encoding="utf-8"))
    assert saved["pid"] == 456


def test_run_worker_releases_power_and_records_terminal_result(tmp_path: Path) -> None:
    calls: list[int] = []
    results = [{"status": "READY_TO_PUBLISH"}]

    exit_code = run_worker(
        "JOB1",
        tmp_path,
        run_autopilot=lambda _job_id: results.pop(0),
        inhibitor_factory=lambda: SleepInhibitor(
            set_state=lambda flags: calls.append(flags) or flags
        ),
    )

    assert exit_code == 0
    assert calls[-1] == SleepInhibitor.ES_CONTINUOUS
    record = json.loads(
        (tmp_path / "_work" / "runtime" / "worker.json").read_text(encoding="utf-8")
    )
    assert record["terminal_status"] == "READY_TO_PUBLISH"
    assert record["finished_at"]
