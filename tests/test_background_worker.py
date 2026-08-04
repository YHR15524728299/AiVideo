from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from aicf.background_worker import (
    ProcessIdentity,
    ProcessProbe,
    ProcessProbeStatus,
    SleepInhibitor,
    StopRequestMonitor,
    WorkerIdentityError,
    WorkerLauncher,
    WorkerRecord,
    stop_worker,
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
    identity = ProcessIdentity(
        pid=123,
        created_at_ns=111,
        executable="python.exe",
    )
    record_path = tmp_path / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        WorkerRecord(
            job_id="JOB1",
            pid=123,
            started_at="now",
            log_path="x.log",
            instance_id="instance-a",
            process_created_at_ns=identity.created_at_ns,
            process_executable=identity.executable,
        ).model_dump_json(),
        encoding="utf-8",
    )
    spawned: list[object] = []
    launcher = WorkerLauncher(
        python_executable="python",
        process_probe=lambda pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=identity if pid == 123 else None,
        ),
        popen=lambda *args, **kwargs: spawned.append((args, kwargs)),
        ready_timeout=0,
    )

    result = launcher.start("JOB1", tmp_path)

    assert result.pid == 123
    assert result.reused is True
    assert spawned == []


def test_launcher_refuses_active_legacy_record(tmp_path: Path) -> None:
    record_path = tmp_path / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        WorkerRecord(
            job_id="JOB1",
            pid=123,
            started_at="old",
            log_path="old.log",
        ).model_dump_json(),
        encoding="utf-8",
    )
    spawned: list[object] = []
    launcher = WorkerLauncher(
        python_executable="python",
        process_probe=lambda pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=ProcessIdentity(
                pid=pid,
                created_at_ns=111,
                executable="python.exe",
            ),
        ),
        popen=lambda *args, **kwargs: spawned.append((args, kwargs)),
        ready_timeout=0,
    )

    with pytest.raises(WorkerIdentityError, match="旧版"):
        launcher.start("JOB1", tmp_path)

    assert spawned == []


def test_launcher_refuses_unknown_process_probe(tmp_path: Path) -> None:
    record_path = tmp_path / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        WorkerRecord(
            job_id="JOB1",
            pid=123,
            started_at="old",
            log_path="old.log",
            instance_id="instance-a",
            process_created_at_ns=111,
            process_executable="python.exe",
        ).model_dump_json(),
        encoding="utf-8",
    )
    spawned: list[object] = []
    launcher = WorkerLauncher(
        python_executable="python",
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.UNKNOWN
        ),
        popen=lambda *args, **kwargs: spawned.append((args, kwargs)),
        ready_timeout=0,
    )

    with pytest.raises(WorkerIdentityError, match="无法确认"):
        launcher.start("JOB1", tmp_path)

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

    identity = ProcessIdentity(
        pid=456,
        created_at_ns=222,
        executable="python.exe",
    )
    launcher = WorkerLauncher(
        python_executable="python",
        process_probe=lambda pid: ProcessProbe(
            status=(
                ProcessProbeStatus.RUNNING
                if pid == 456
                else ProcessProbeStatus.NOT_RUNNING
            ),
            identity=identity if pid == 456 else None,
        ),
        popen=lambda *args, **kwargs: Process(),
        ready_timeout=0,
    )
    result = launcher.start("JOB1", tmp_path)

    assert result.pid == 456
    assert result.reused is False
    saved = json.loads(record_path.read_text(encoding="utf-8"))
    assert saved["pid"] == 456


def test_launcher_terminates_unidentifiable_spawn(tmp_path: Path) -> None:
    class Process:
        pid = 456

    terminated: list[int] = []
    launcher = WorkerLauncher(
        python_executable="python",
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.UNKNOWN
        ),
        popen=lambda *args, **kwargs: Process(),
        cleanup_spawn=lambda process: terminated.append(process.pid),
        ready_timeout=0,
    )

    with pytest.raises(WorkerIdentityError, match="身份"):
        launcher.start("JOB1", tmp_path)

    assert terminated == [456]


def test_launcher_base_python_inherits_virtualenv_site_packages(
    tmp_path: Path,
) -> None:
    captured_environment: dict[str, str] = {}

    class Process:
        pid = 456

    identity = ProcessIdentity(
        pid=456,
        created_at_ns=222,
        executable="python.exe",
    )

    def popen(*args: object, **kwargs: object) -> Process:
        captured_environment.update(kwargs["env"])
        return Process()

    launcher = WorkerLauncher(
        python_executable=str(tmp_path / ".venv" / "Scripts" / "python.exe"),
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=identity,
        ),
        popen=popen,
        ready_timeout=0,
    )

    launcher.start("JOB1", tmp_path, project_root=tmp_path)

    python_path = captured_environment["PYTHONPATH"].split(os.pathsep)
    assert str(tmp_path / ".venv" / "Lib" / "site-packages") in python_path
    assert str(tmp_path / "src") in python_path


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
        require_launch_token=False,
    )

    assert exit_code == 0
    assert calls[-1] == SleepInhibitor.ES_CONTINUOUS
    record = json.loads(
        (tmp_path / "_work" / "runtime" / "worker.json").read_text(encoding="utf-8")
    )
    assert record["terminal_status"] == "READY_TO_PUBLISH"
    assert record["finished_at"]


def test_concurrent_launcher_starts_exactly_one_worker(tmp_path: Path) -> None:
    spawn_count = 0
    spawn_guard = threading.Lock()
    start_barrier = threading.Barrier(2)

    class Process:
        pid = 456

    identity = ProcessIdentity(
        pid=456,
        created_at_ns=123456,
        executable="python.exe",
    )

    def popen(*args: object, **kwargs: object) -> Process:
        nonlocal spawn_count
        with spawn_guard:
            spawn_count += 1
        time.sleep(0.05)
        return Process()

    launcher = WorkerLauncher(
        python_executable="python",
        process_probe=lambda pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=identity if pid == 456 else None,
        ),
        popen=popen,
        ready_timeout=0,
    )
    results: list[object] = []

    def launch() -> None:
        start_barrier.wait()
        results.append(launcher.start("JOB1", tmp_path))

    threads = [threading.Thread(target=launch) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert spawn_count == 1
    assert sorted(result.reused for result in results) == [False, True]


def test_stop_worker_refuses_reused_pid_identity(tmp_path: Path) -> None:
    record = WorkerRecord(
        job_id="JOB1",
        pid=456,
        started_at="now",
        log_path="worker.log",
        instance_id="instance-a",
        process_created_at_ns=111,
        process_executable="python.exe",
    )
    record_path = tmp_path / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(record.model_dump_json(), encoding="utf-8")
    with pytest.raises(WorkerIdentityError, match="身份"):
        stop_worker(
            tmp_path,
            process_identity=lambda _pid: ProcessIdentity(
                pid=456,
                created_at_ns=222,
                executable="browser.exe",
            ),
        )

    assert not list((tmp_path / "_work" / "runtime").glob("stop-*.request"))


def test_stop_worker_requests_stop_for_matching_instance(tmp_path: Path) -> None:
    identity = ProcessIdentity(
        pid=456,
        created_at_ns=111,
        executable="python.exe",
    )
    record = WorkerRecord(
        job_id="JOB1",
        pid=456,
        started_at="now",
        log_path="worker.log",
        instance_id="instance-a",
        process_created_at_ns=identity.created_at_ns,
        process_executable=identity.executable,
    )
    record_path = tmp_path / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(record.model_dump_json(), encoding="utf-8")
    stopped = stop_worker(
        tmp_path,
        process_identity=lambda _pid: identity,
    )

    request = (
        tmp_path
        / "_work"
        / "runtime"
        / f"stop-{record.instance_id}.request"
    )
    assert request.is_file()
    assert stopped.stop_requested_at is not None


def test_stop_monitor_terminates_only_its_instance(tmp_path: Path) -> None:
    terminated = threading.Event()
    own_request = (
        tmp_path / "_work" / "runtime" / "stop-instance-a.request"
    )
    other_request = (
        tmp_path / "_work" / "runtime" / "stop-instance-b.request"
    )
    other_request.parent.mkdir(parents=True)
    other_request.write_text("stop", encoding="utf-8")

    with StopRequestMonitor(
        tmp_path,
        "instance-a",
        terminate_self=lambda: terminated.set(),
        poll_interval=0.01,
    ):
        time.sleep(0.03)
        assert terminated.is_set() is False
        own_request.write_text("stop", encoding="utf-8")
        assert terminated.wait(timeout=1)


def test_stop_monitor_handles_request_present_before_start(tmp_path: Path) -> None:
    terminated = threading.Event()
    own_request = (
        tmp_path / "_work" / "runtime" / "stop-instance-a.request"
    )
    own_request.parent.mkdir(parents=True)
    own_request.write_text("stop", encoding="utf-8")

    with StopRequestMonitor(
        tmp_path,
        "instance-a",
        terminate_self=lambda: terminated.set(),
        poll_interval=0.5,
    ):
        assert terminated.wait(timeout=0.2)


def test_worker_run_requires_launcher_token(tmp_path: Path) -> None:
    with pytest.raises(WorkerIdentityError, match="Launcher"):
        run_worker(
            "JOB1",
            tmp_path,
            run_autopilot=lambda _job_id: {"status": "READY_TO_PUBLISH"},
            inhibitor_factory=lambda: SleepInhibitor(
                set_state=lambda flags: flags
            ),
        )


def test_worker_run_rejects_same_token_from_different_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = "instance-a"
    launcher_identity = ProcessIdentity(
        pid=999,
        created_at_ns=999,
        executable="launcher-python.exe",
    )
    current_identity = ProcessIdentity(
        pid=os.getpid(),
        created_at_ns=111,
        executable="different-python.exe",
    )
    write_path = tmp_path / "_work" / "runtime" / "worker.json"
    write_path.parent.mkdir(parents=True)
    write_path.write_text(
        WorkerRecord(
            job_id="JOB1",
            pid=launcher_identity.pid,
            started_at="now",
            log_path="worker.log",
            instance_id=instance_id,
            process_created_at_ns=launcher_identity.created_at_ns,
            process_executable=launcher_identity.executable,
        ).model_dump_json(),
        encoding="utf-8",
    )
    monkeypatch.setenv("AICF_WORKER_LAUNCHED", "1")
    monkeypatch.setenv("AICF_WORKER_INSTANCE_ID", instance_id)
    monkeypatch.setattr(
        "aicf.background_worker.get_process_identity",
        lambda _pid: current_identity,
    )

    with pytest.raises(WorkerIdentityError, match="进程身份"):
        run_worker(
            "JOB1",
            tmp_path,
            run_autopilot=lambda _job_id: {"status": "READY_TO_PUBLISH"},
            inhibitor_factory=lambda: SleepInhibitor(
                set_state=lambda flags: flags
            ),
        )


def test_stop_request_wins_before_terminal_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = "instance-a"
    identity = ProcessIdentity(
        pid=os.getpid(),
        created_at_ns=111,
        executable="python.exe",
    )
    write_path = tmp_path / "_work" / "runtime" / "worker.json"
    write_path.parent.mkdir(parents=True)
    write_path.write_text(
        WorkerRecord(
            job_id="JOB1",
            pid=identity.pid,
            started_at="now",
            log_path="worker.log",
            instance_id=instance_id,
            process_created_at_ns=identity.created_at_ns,
            process_executable=identity.executable,
        ).model_dump_json(),
        encoding="utf-8",
    )
    request = (
        tmp_path / "_work" / "runtime" / f"stop-{instance_id}.request"
    )
    monkeypatch.setenv("AICF_WORKER_LAUNCHED", "1")
    monkeypatch.setenv("AICF_WORKER_INSTANCE_ID", instance_id)
    monkeypatch.setattr(
        "aicf.background_worker.get_process_identity",
        lambda _pid: identity,
    )

    def stop_monitor(
        job_dir: str | Path,
        monitor_instance_id: str,
    ) -> StopRequestMonitor:
        return StopRequestMonitor(
            job_dir,
            monitor_instance_id,
            terminate_self=lambda: None,
        )

    monkeypatch.setattr(
        "aicf.background_worker.StopRequestMonitor",
        stop_monitor,
    )
    monkeypatch.setattr(
        "aicf.background_worker.terminate_current_process_tree",
        lambda: (_ for _ in ()).throw(SystemExit(130)),
    )

    def finish_with_pending_stop(_job_id: str) -> dict[str, str]:
        request.write_text("stop", encoding="utf-8")
        return {"status": "READY_TO_PUBLISH"}

    with pytest.raises(SystemExit):
        run_worker(
            "JOB1",
            tmp_path,
            run_autopilot=finish_with_pending_stop,
            inhibitor_factory=lambda: SleepInhibitor(
                set_state=lambda flags: flags
            ),
        )

    record = WorkerRecord.model_validate_json(
        write_path.read_text(encoding="utf-8")
    )
    assert record.finished_at is None
    assert record.terminal_status != "READY_TO_PUBLISH"
    assert request.is_file()


def test_launched_worker_waits_for_its_instance_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ProcessIdentity(
        pid=os.getpid(),
        created_at_ns=111,
        executable="python.exe",
    )
    stale = WorkerRecord(
        job_id="JOB1",
        pid=999,
        started_at="old",
        log_path="old.log",
        instance_id="old-instance",
        process_created_at_ns=999,
        process_executable="old.exe",
    )
    write_path = tmp_path / "_work" / "runtime" / "worker.json"
    write_path.parent.mkdir(parents=True)
    write_path.write_text(stale.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("AICF_WORKER_LAUNCHED", "1")
    monkeypatch.setenv("AICF_WORKER_INSTANCE_ID", "new-instance")

    def publish_matching_record() -> None:
        time.sleep(0.05)
        matching = WorkerRecord(
            job_id="JOB1",
            pid=identity.pid,
            started_at="now",
            log_path="worker.log",
            instance_id="new-instance",
            process_created_at_ns=identity.created_at_ns,
            process_executable=identity.executable,
        )
        write_path.write_text(matching.model_dump_json(), encoding="utf-8")

    updater = threading.Thread(target=publish_matching_record)
    updater.start()
    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(
                "aicf.background_worker.get_process_identity",
                lambda _pid: identity,
            )
            exit_code = run_worker(
                "JOB1",
                tmp_path,
                run_autopilot=lambda _job_id: {"status": "READY_TO_PUBLISH"},
                inhibitor_factory=lambda: SleepInhibitor(
                    set_state=lambda flags: flags
                ),
            )
    finally:
        updater.join(timeout=1)

    assert exit_code == 0
