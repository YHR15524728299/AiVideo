from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from aicf.job_runtime import RuntimeLease, RuntimeLeaseError
from aicf.background_worker import (
    _commit_terminal_record,
    _recover_stale_runtime_lease,
    ProcessIdentity,
    ProcessProbe,
    ProcessProbeStatus,
    SleepInhibitor,
    StopRequestMonitor,
    WorkerIdentityError,
    WorkerLauncher,
    WorkerRecord,
    force_kill_worker,
    read_worker_record,
    stop_worker,
    run_worker,
)


def test_background_worker_reexports_infrastructure_symbols() -> None:
    from aicf import background_worker
    from aicf import process_identity
    from aicf import worker_stop_ipc

    assert background_worker.ProcessIdentity is process_identity.ProcessIdentity
    assert background_worker.ProcessProbe is process_identity.ProcessProbe
    assert (
        background_worker.ProcessProbeStatus
        is process_identity.ProcessProbeStatus
    )
    assert (
        background_worker.get_process_identity
        is process_identity.get_process_identity
    )
    assert (
        background_worker.probe_process_identity
        is process_identity.probe_process_identity
    )
    assert (
        background_worker.process_is_running
        is process_identity.process_is_running
    )
    assert (
        background_worker.process_identity_matches
        is process_identity.process_identity_matches
    )
    assert (
        background_worker.StopRequestMonitor
        is worker_stop_ipc.StopRequestMonitor
    )
    assert (
        background_worker.WorkerIdentityError
        is worker_stop_ipc.WorkerIdentityError
    )
    assert background_worker.stop_request_path is worker_stop_ipc.stop_request_path
    assert (
        background_worker.terminate_current_process_tree
        is worker_stop_ipc.terminate_current_process_tree
    )


def test_sleep_inhibitor_releases_after_success() -> None:
    calls: list[int] = []

    with SleepInhibitor(set_state=lambda flags: calls.append(flags) or flags):
        assert calls

    assert calls[-1] == SleepInhibitor.ES_CONTINUOUS


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

    result = launcher.start("JOB1", tmp_path, project_root=tmp_path)

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
        launcher.start("JOB1", tmp_path, project_root=tmp_path)

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
        launcher.start("JOB1", tmp_path, project_root=tmp_path)

    assert spawned == []


def test_launcher_rechecks_job_guard_inside_lifecycle_lock(
    tmp_path: Path,
) -> None:
    spawned: list[object] = []
    launcher = WorkerLauncher(
        python_executable="python",
        launch_guard=lambda: False,
        popen=lambda *args, **kwargs: spawned.append((args, kwargs)),
        ready_timeout=0,
    )

    with pytest.raises(WorkerIdentityError, match="任务状态"):
        launcher.start("JOB1", tmp_path, project_root=tmp_path)

    assert spawned == []


def test_launcher_recovers_stale_lease_inside_project_lifecycle_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lifecycle_lock_held = False
    real_lock = __import__(
        "aicf.background_worker",
        fromlist=["os_file_lock"],
    ).os_file_lock

    @contextmanager
    def tracked_lock(path: Path, **kwargs: object):
        nonlocal lifecycle_lock_held
        with real_lock(path, **kwargs):
            lifecycle_lock_held = True
            try:
                yield
            finally:
                lifecycle_lock_held = False

    def recover(*_args: object, **_kwargs: object) -> bool:
        assert lifecycle_lock_held
        raise RuntimeError("stale recovery checked")

    monkeypatch.setattr("aicf.background_worker.os_file_lock", tracked_lock)
    monkeypatch.setattr(
        "aicf.background_worker._recover_stale_runtime_lease",
        recover,
    )
    launcher = WorkerLauncher(
        python_executable="python",
        ready_timeout=0,
    )

    with pytest.raises(RuntimeError, match="stale recovery checked"):
        launcher.start("JOB1", tmp_path, project_root=tmp_path)


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
    result = launcher.start("JOB1", tmp_path, project_root=tmp_path)

    assert result.pid == 456
    assert result.reused is False
    saved = json.loads(record_path.read_text(encoding="utf-8"))
    assert saved["pid"] == 456


def test_launcher_forwards_research_strategy_to_worker_process(
    tmp_path: Path,
) -> None:
    class Process:
        pid = 456

    identity = ProcessIdentity(
        pid=456,
        created_at_ns=222,
        executable="python.exe",
    )
    commands: list[list[str]] = []

    def popen(command: list[str], **_kwargs: object) -> Process:
        commands.append(command)
        return Process()

    launcher = WorkerLauncher(
        python_executable="python",
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=identity,
        ),
        popen=popen,
        ready_timeout=0,
    )

    launcher.start(
        "JOB1",
        tmp_path,
        project_root=tmp_path,
        research_strategy="RETRY_SOURCES",
    )

    assert commands[0][-2:] == ["--research-strategy", "RETRY_SOURCES"]


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
        launcher.start("JOB1", tmp_path, project_root=tmp_path)

    assert terminated == [456]


def test_launcher_handshake_exception_cleans_process_and_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 456

    identity = ProcessIdentity(
        pid=456,
        created_at_ns=222,
        executable="python.exe",
    )
    cleaned: list[int] = []
    process_running = True

    def process_probe(_pid: int) -> ProcessProbe:
        return ProcessProbe(
            status=(
                ProcessProbeStatus.RUNNING
                if process_running
                else ProcessProbeStatus.NOT_RUNNING
            ),
            identity=identity if process_running else None,
        )

    def cleanup_spawn(process: Process) -> None:
        nonlocal process_running
        cleaned.append(process.pid)
        process_running = False

    monkeypatch.setattr(
        "aicf.background_worker.write_worker_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("record write failed")
        ),
    )
    launcher = WorkerLauncher(
        python_executable="python",
        process_probe=process_probe,
        popen=lambda *args, **kwargs: Process(),
        cleanup_spawn=cleanup_spawn,
        ready_timeout=0,
    )

    with pytest.raises(WorkerIdentityError, match="启动握手") as exc_info:
        launcher.start("JOB1", tmp_path, project_root=tmp_path)

    assert isinstance(exc_info.value.__cause__, OSError)
    assert cleaned == [456]
    assert RuntimeLease(tmp_path).read() is None


def test_launcher_cleanup_failure_keeps_lease_and_rejects_next_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 456

    identity = ProcessIdentity(
        pid=456,
        created_at_ns=222,
        executable="python.exe",
    )
    spawned: list[int] = []
    monkeypatch.setattr(
        "aicf.background_worker.write_worker_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("record write failed")
        ),
    )

    def popen(*_args: object, **_kwargs: object) -> Process:
        spawned.append(Process.pid)
        return Process()

    launcher = WorkerLauncher(
        python_executable="python",
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=identity,
        ),
        popen=popen,
        cleanup_spawn=lambda _process: (_ for _ in ()).throw(
            OSError("cleanup failed")
        ),
        ready_timeout=0,
    )

    with pytest.raises(
        WorkerIdentityError,
        match="子进程退出未确认，已保留项目Worker租约",
    ):
        launcher.start(
            "JOB1",
            tmp_path / "data" / "jobs" / "JOB1",
            project_root=tmp_path,
        )

    retained = RuntimeLease(tmp_path).read()
    assert retained is not None
    assert retained.job_id == "JOB1"
    assert retained.pid == 456

    with pytest.raises(WorkerIdentityError, match="项目已有运行中Worker"):
        launcher.start(
            "JOB2",
            tmp_path / "data" / "jobs" / "JOB2",
            project_root=tmp_path,
        )

    assert spawned == [456]


def test_launcher_unknown_exit_identity_keeps_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 456

    identity = ProcessIdentity(
        pid=456,
        created_at_ns=222,
        executable="python.exe",
    )
    probes = iter(
        (
            ProcessProbe(
                status=ProcessProbeStatus.RUNNING,
                identity=identity,
            ),
            ProcessProbe(status=ProcessProbeStatus.UNKNOWN),
        )
    )
    monkeypatch.setattr(
        "aicf.background_worker.write_worker_record",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("record write failed")
        ),
    )
    launcher = WorkerLauncher(
        python_executable="python",
        process_probe=lambda _pid: next(probes),
        popen=lambda *_args, **_kwargs: Process(),
        cleanup_spawn=lambda _process: None,
        ready_timeout=0,
    )

    with pytest.raises(
        WorkerIdentityError,
        match="子进程退出未确认，已保留项目Worker租约",
    ):
        launcher.start("JOB1", tmp_path, project_root=tmp_path)

    retained = RuntimeLease(tmp_path).read()
    assert retained is not None
    assert retained.instance_id
    assert retained.pid == 456


def test_launcher_rejects_other_job_when_project_lease_is_active(
    tmp_path: Path,
) -> None:
    identities = {
        456: ProcessIdentity(
            pid=456,
            created_at_ns=111,
            executable="python.exe",
        ),
        789: ProcessIdentity(
            pid=789,
            created_at_ns=222,
            executable="python.exe",
        ),
    }
    spawned = iter((456, 789))
    cleaned: list[int] = []

    class Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    launcher = WorkerLauncher(
        python_executable="python",
        process_probe=lambda pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=identities.get(pid),
        ),
        popen=lambda *args, **kwargs: Process(next(spawned)),
        cleanup_spawn=lambda process: cleaned.append(process.pid),
        ready_timeout=0,
    )

    launcher.start(
        "JOB1",
        tmp_path / "data" / "jobs" / "JOB1",
        project_root=tmp_path,
    )
    with pytest.raises(WorkerIdentityError, match="项目已有运行中Worker"):
        launcher.start(
            "JOB2",
            tmp_path / "data" / "jobs" / "JOB2",
            project_root=tmp_path,
        )

    assert cleaned == []


def test_legacy_two_argument_launcher_enforces_project_lease_concurrently(
    tmp_path: Path,
) -> None:
    identities = {
        456: ProcessIdentity(
            pid=456,
            created_at_ns=111,
            executable="python.exe",
        ),
        789: ProcessIdentity(
            pid=789,
            created_at_ns=222,
            executable="python.exe",
        ),
    }
    next_pid = iter(identities)
    pid_lock = threading.Lock()
    start_barrier = threading.Barrier(2)
    cleaned: list[int] = []

    class Process:
        def __init__(self, pid: int) -> None:
            self.pid = pid

    def popen(*args: object, **kwargs: object) -> Process:
        with pid_lock:
            return Process(next(next_pid))

    launcher = WorkerLauncher(
        python_executable="python",
        process_probe=lambda pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=identities[pid],
        ),
        popen=popen,
        cleanup_spawn=lambda process: cleaned.append(process.pid),
        ready_timeout=0,
    )
    results: list[object] = []
    errors: list[BaseException] = []

    def launch(job_id: str) -> None:
        start_barrier.wait()
        try:
            results.append(
                launcher.start(
                    job_id,
                    tmp_path / "data" / "jobs" / job_id,
                )
            )
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=launch, args=("JOB1",)),
        threading.Thread(target=launch, args=("JOB2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], WorkerIdentityError)
    assert "项目已有运行中Worker" in str(errors[0])
    assert cleaned == []


def test_legacy_two_argument_launcher_fails_closed_for_unknown_layout(
    tmp_path: Path,
) -> None:
    spawned: list[object] = []
    launcher = WorkerLauncher(
        python_executable="python",
        popen=lambda *args, **kwargs: spawned.append((args, kwargs)),
        ready_timeout=0,
    )

    with pytest.raises(WorkerIdentityError, match="项目根目录"):
        launcher.start("JOB1", tmp_path / "custom" / "JOB1")

    assert spawned == []


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


def test_run_worker_releases_matching_project_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = "instance-a"
    identity = ProcessIdentity(
        pid=os.getpid(),
        created_at_ns=111,
        executable="python.exe",
    )
    job_dir = tmp_path / "data" / "jobs" / "JOB1"
    record_path = job_dir / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
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
    lease = RuntimeLease(
        tmp_path,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=identity,
        ),
    )
    lease.acquire("JOB1", instance_id, identity)
    monkeypatch.setenv("AICF_WORKER_LAUNCHED", "1")
    monkeypatch.setenv("AICF_WORKER_INSTANCE_ID", instance_id)
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "aicf.background_worker.get_process_identity",
        lambda _pid: identity,
    )

    assert (
        run_worker(
            "JOB1",
            job_dir,
            run_autopilot=lambda _job_id: {"status": "READY_TO_PUBLISH"},
            inhibitor_factory=lambda: SleepInhibitor(
                set_state=lambda flags: flags
            ),
        )
        == 0
    )
    assert lease.read() is None


def test_run_worker_commits_failed_terminal_record_when_lease_acquire_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = "instance-a"
    identity = ProcessIdentity(
        pid=os.getpid(),
        created_at_ns=111,
        executable="python.exe",
    )
    record_path = tmp_path / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
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
    monkeypatch.setenv("AICF_WORKER_LAUNCHED", "1")
    monkeypatch.setenv("AICF_WORKER_INSTANCE_ID", instance_id)
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "aicf.background_worker.get_process_identity",
        lambda _pid: identity,
    )
    monkeypatch.setattr(
        RuntimeLease,
        "acquire",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("lease unavailable")
        ),
    )

    with pytest.raises(WorkerIdentityError, match="租约"):
        run_worker(
            "JOB1",
            tmp_path,
            run_autopilot=lambda _job_id: pytest.fail("不应开始业务处理"),
            inhibitor_factory=lambda: SleepInhibitor(
                set_state=lambda flags: flags
            ),
        )

    saved = WorkerRecord.model_validate_json(
        record_path.read_text(encoding="utf-8")
    )
    assert saved.finished_at is not None
    assert saved.terminal_status == "FAILED"
    assert "lease unavailable" in (saved.error or "")


def test_concurrent_launcher_starts_exactly_one_worker(tmp_path: Path) -> None:
    spawn_count = 0
    spawn_guard = threading.Lock()
    start_barrier = threading.Barrier(2)
    spawn_started = threading.Event()
    allow_spawn = threading.Event()

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
        spawn_started.set()
        assert allow_spawn.wait(timeout=1)
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
    errors: list[BaseException] = []

    def launch() -> None:
        start_barrier.wait()
        try:
            results.append(
                launcher.start("JOB1", tmp_path, project_root=tmp_path)
            )
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=launch) for _ in range(2)]
    for thread in threads:
        thread.start()
    assert spawn_started.wait(timeout=1)
    allow_spawn.set()
    for thread in threads:
        thread.join(timeout=5)

    assert spawn_count == 1
    assert errors == []
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


def test_force_kill_archives_reused_pid_without_terminating(tmp_path: Path) -> None:
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
    terminated: list[int] = []

    archived = force_kill_worker(
        tmp_path,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=ProcessIdentity(
                pid=456,
                created_at_ns=222,
                executable="browser.exe",
            ),
        ),
        terminate_process=lambda pid: terminated.append(pid),
    )

    assert terminated == []
    assert archived.terminal_status == "STALE_IDENTITY"
    assert archived.finished_at is not None


def test_force_kill_failure_does_not_commit_finished_at(tmp_path: Path) -> None:
    identity = ProcessIdentity(
        pid=456,
        created_at_ns=111,
        executable="python.exe",
    )
    record = WorkerRecord(
        job_id="JOB1",
        pid=identity.pid,
        started_at="now",
        log_path="worker.log",
        instance_id="instance-a",
        process_created_at_ns=identity.created_at_ns,
        process_executable=identity.executable,
    )
    record_path = tmp_path / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(record.model_dump_json(), encoding="utf-8")

    with pytest.raises(WorkerIdentityError, match="taskkill"):
        force_kill_worker(
            tmp_path,
            process_probe=lambda _pid: ProcessProbe(
                status=ProcessProbeStatus.RUNNING,
                identity=identity,
            ),
            terminate_process=lambda _pid: (_ for _ in ()).throw(
                WorkerIdentityError("taskkill返回非零")
            ),
        )

    saved = WorkerRecord.model_validate_json(
        record_path.read_text(encoding="utf-8")
    )
    assert saved.finished_at is None
    assert saved.terminal_status == "STOP_FAILED"


def test_force_kill_requires_confirmed_process_exit(tmp_path: Path) -> None:
    identity = ProcessIdentity(
        pid=456,
        created_at_ns=111,
        executable="python.exe",
    )
    record = WorkerRecord(
        job_id="JOB1",
        pid=identity.pid,
        started_at="now",
        log_path="worker.log",
        instance_id="instance-a",
        process_created_at_ns=identity.created_at_ns,
        process_executable=identity.executable,
    )
    record_path = tmp_path / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(record.model_dump_json(), encoding="utf-8")
    probes = iter(
        [
            ProcessProbe(status=ProcessProbeStatus.RUNNING, identity=identity),
            ProcessProbe(status=ProcessProbeStatus.RUNNING, identity=identity),
        ]
    )

    with pytest.raises(WorkerIdentityError, match="仍在运行"):
        force_kill_worker(
            tmp_path,
            process_probe=lambda _pid: next(probes),
            terminate_process=lambda _pid: None,
            confirmation_timeout=0,
        )

    saved = WorkerRecord.model_validate_json(
        record_path.read_text(encoding="utf-8")
    )
    assert saved.finished_at is None
    assert saved.terminal_status == "STOP_FAILED"


def test_force_kill_polls_until_process_exit_is_confirmed(tmp_path: Path) -> None:
    identity = ProcessIdentity(
        pid=456,
        created_at_ns=111,
        executable="python.exe",
    )
    record_path = tmp_path / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        WorkerRecord(
            job_id="JOB1",
            pid=identity.pid,
            started_at="now",
            log_path="worker.log",
            instance_id="instance-a",
            process_created_at_ns=identity.created_at_ns,
            process_executable=identity.executable,
        ).model_dump_json(),
        encoding="utf-8",
    )
    probes = iter(
        [
            ProcessProbe(status=ProcessProbeStatus.RUNNING, identity=identity),
            ProcessProbe(status=ProcessProbeStatus.RUNNING, identity=identity),
            ProcessProbe(status=ProcessProbeStatus.UNKNOWN),
            ProcessProbe(status=ProcessProbeStatus.NOT_RUNNING),
        ]
    )
    sleeps: list[float] = []

    stopped = force_kill_worker(
        tmp_path,
        process_probe=lambda _pid: next(probes),
        terminate_process=lambda _pid: None,
        confirmation_timeout=1,
        poll_interval=0.001,
        sleep=sleeps.append,
    )

    assert stopped.terminal_status == "FORCE_STOPPED"
    assert stopped.finished_at is not None
    assert sleeps == [0.001, 0.001]


def test_force_kill_preserves_existing_terminal_record(tmp_path: Path) -> None:
    record = WorkerRecord(
        job_id="JOB1",
        pid=456,
        started_at="now",
        log_path="worker.log",
        instance_id="instance-a",
        process_created_at_ns=111,
        process_executable="python.exe",
        finished_at="already-finished",
        terminal_status="COMPLETED",
        error=None,
    )
    record_path = tmp_path / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(record.model_dump_json(), encoding="utf-8")
    probes: list[int] = []
    terminated: list[int] = []

    result = force_kill_worker(
        tmp_path,
        process_probe=lambda pid: probes.append(pid) or ProcessProbe(
            status=ProcessProbeStatus.NOT_RUNNING
        ),
        terminate_process=terminated.append,
    )

    assert result == record
    assert probes == []
    assert terminated == []
    assert WorkerRecord.model_validate_json(
        record_path.read_text(encoding="utf-8")
    ) == record


def test_late_commit_preserves_completed_terminal_record(tmp_path: Path) -> None:
    record = WorkerRecord(
        job_id="JOB1",
        pid=456,
        started_at="now",
        log_path="worker.log",
        instance_id="instance-a",
        finished_at="already-finished",
        terminal_status="COMPLETED",
    )
    record_path = tmp_path / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(record.model_dump_json(), encoding="utf-8")

    _commit_terminal_record(
        tmp_path,
        "instance-a",
        terminal_status="FAILED",
        error="late failure",
    )

    assert WorkerRecord.model_validate_json(
        record_path.read_text(encoding="utf-8")
    ) == record


def test_late_commit_preserves_force_stopped_terminal_record(
    tmp_path: Path,
) -> None:
    record = WorkerRecord(
        job_id="JOB1",
        pid=456,
        started_at="now",
        log_path="worker.log",
        instance_id="instance-a",
        finished_at="already-finished",
        terminal_status="FORCE_STOPPED",
        error="用户强制停止",
    )
    record_path = tmp_path / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(record.model_dump_json(), encoding="utf-8")

    _commit_terminal_record(
        tmp_path,
        "instance-a",
        terminal_status="COMPLETED",
    )

    assert WorkerRecord.model_validate_json(
        record_path.read_text(encoding="utf-8")
    ) == record


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


def test_long_running_worker_stops_cooperatively_then_releases_lease(
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
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "aicf.background_worker.get_process_identity",
        lambda _pid: identity,
    )
    lease = RuntimeLease(
        tmp_path,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=identity,
        ),
    )
    lease.acquire("JOB1", instance_id, identity)
    events: list[str] = []
    original_release = RuntimeLease.release

    def release(
        runtime_lease: RuntimeLease,
        release_instance_id: str,
    ) -> bool:
        committed = WorkerRecord.model_validate_json(
            write_path.read_text(encoding="utf-8")
        )
        assert committed.terminal_status == "STOPPED"
        assert committed.finished_at is not None
        events.append("release")
        return original_release(runtime_lease, release_instance_id)

    monkeypatch.setattr(RuntimeLease, "release", release)
    monkeypatch.setattr(
        "aicf.background_worker.terminate_current_process_tree",
        lambda: pytest.fail("协作式停止不应强杀当前进程"),
        raising=False,
    )
    autopilot_started = threading.Event()
    keep_running = threading.Event()
    request_errors: list[BaseException] = []

    def long_running_autopilot(_job_id: str) -> dict[str, str]:
        autopilot_started.set()
        keep_running.wait(timeout=5)
        return {"status": "READY_TO_PUBLISH"}

    def request_stop() -> None:
        try:
            if not autopilot_started.wait(timeout=1):
                raise RuntimeError("autopilot did not start")
            request.write_text("stop", encoding="utf-8")
        except BaseException as error:
            request_errors.append(error)

    requester = threading.Thread(target=request_stop)
    requester.start()
    try:
        exit_code = run_worker(
            "JOB1",
            tmp_path,
            run_autopilot=long_running_autopilot,
            inhibitor_factory=lambda: SleepInhibitor(
                set_state=lambda flags: flags
            ),
        )
    finally:
        keep_running.set()
        requester.join(timeout=1)

    record = WorkerRecord.model_validate_json(
        write_path.read_text(encoding="utf-8")
    )
    assert exit_code == 130
    assert request_errors == []
    assert not requester.is_alive()
    assert record.finished_at is not None
    assert record.terminal_status == "STOPPED"
    assert events == ["release"]
    assert lease.read() is None


def test_run_worker_closes_failed_heartbeat_without_success_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = "instance-a"
    identity = ProcessIdentity(
        pid=os.getpid(),
        created_at_ns=111,
        executable="python.exe",
    )
    record_path = tmp_path / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
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
    monkeypatch.setenv("AICF_WORKER_LAUNCHED", "1")
    monkeypatch.setenv("AICF_WORKER_INSTANCE_ID", instance_id)
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "aicf.background_worker.get_process_identity",
        lambda _pid: identity,
    )
    lease = RuntimeLease(
        tmp_path,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=identity,
        ),
    )
    lease.acquire("JOB1", instance_id, identity)

    class FailingHeartbeat:
        error: RuntimeLeaseError | None = None

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> "FailingHeartbeat":
            return self

        def __exit__(
            self,
            _exc_type: object,
            _exc: object,
            _traceback: object,
        ) -> None:
            self.error = RuntimeLeaseError("heartbeat write failed")

    monkeypatch.setattr(
        "aicf.background_worker.RuntimeLeaseHeartbeat",
        FailingHeartbeat,
    )
    committed_statuses: list[str] = []
    original_commit = _commit_terminal_record

    def record_commit(
        job_dir: Path,
        commit_instance_id: str,
        *,
        terminal_status: str,
        error: str | None = None,
    ) -> bool:
        committed_statuses.append(terminal_status)
        return original_commit(
            job_dir,
            commit_instance_id,
            terminal_status=terminal_status,
            error=error,
        )

    monkeypatch.setattr(
        "aicf.background_worker._commit_terminal_record",
        record_commit,
    )

    exit_code = run_worker(
        "JOB1",
        tmp_path,
        run_autopilot=lambda _job_id: {"status": "READY_TO_PUBLISH"},
        inhibitor_factory=lambda: SleepInhibitor(
            set_state=lambda flags: flags
        ),
    )

    saved = WorkerRecord.model_validate_json(
        record_path.read_text(encoding="utf-8")
    )
    assert exit_code == 1
    assert saved.terminal_status == "FAILED"
    assert saved.finished_at is not None
    assert "heartbeat write failed" in (saved.error or "")
    assert committed_statuses == ["FAILED"]
    assert lease.read() is None


@pytest.mark.parametrize("failure", ["lock", "atomic_write"])
def test_terminal_commit_failure_keeps_lease_and_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    instance_id = "instance-a"
    identity = ProcessIdentity(
        pid=os.getpid(),
        created_at_ns=111,
        executable="python.exe",
    )
    job_dir = tmp_path / "data" / "jobs" / "JOB1"
    record_path = job_dir / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
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
    monkeypatch.setenv("AICF_WORKER_LAUNCHED", "1")
    monkeypatch.setenv("AICF_WORKER_INSTANCE_ID", instance_id)
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "aicf.background_worker.get_process_identity",
        lambda _pid: identity,
    )
    lease = RuntimeLease(
        tmp_path,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=identity,
        ),
    )
    lease.acquire("JOB1", instance_id, identity, job_dir=job_dir)

    if failure == "lock":
        monkeypatch.setattr(
            "aicf.background_worker.os_file_lock",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                TimeoutError("terminal lock failed")
            ),
        )
    else:
        writes = 0
        original_write = __import__(
            "aicf.background_worker",
            fromlist=["write_worker_record"],
        ).write_worker_record

        def fail_terminal_write(path: Path, record: WorkerRecord) -> None:
            nonlocal writes
            writes += 1
            if writes >= 2:
                raise OSError("terminal atomic write failed")
            original_write(path, record)

        monkeypatch.setattr(
            "aicf.background_worker.write_worker_record",
            fail_terminal_write,
        )

    with pytest.raises(BaseException):
        run_worker(
            "JOB1",
            job_dir,
            run_autopilot=lambda _job_id: {"status": "READY_TO_PUBLISH"},
            inhibitor_factory=lambda: SleepInhibitor(
                set_state=lambda flags: flags
            ),
        )

    saved = WorkerRecord.model_validate_json(
        record_path.read_text(encoding="utf-8")
    )
    assert saved.finished_at is None
    assert lease.read() is not None
    assert lease.read().instance_id == instance_id


def test_dead_process_lease_recovers_failed_terminal_then_releases(
    tmp_path: Path,
) -> None:
    instance_id = "dead-instance"
    identity = ProcessIdentity(
        pid=98765,
        created_at_ns=111,
        executable="python.exe",
    )
    job_dir = tmp_path / "data" / "jobs" / "JOB1"
    record_path = job_dir / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
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
    lease = RuntimeLease(
        tmp_path,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.NOT_RUNNING
        ),
    )
    lease.acquire("JOB1", instance_id, identity, job_dir=job_dir)

    assert _recover_stale_runtime_lease(
        tmp_path,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.NOT_RUNNING
        ),
    )

    saved = WorkerRecord.model_validate_json(
        record_path.read_text(encoding="utf-8")
    )
    assert saved.instance_id == instance_id
    assert saved.finished_at is not None
    assert saved.terminal_status == "FAILED"
    assert "进程已退出" in (saved.error or "")
    assert lease.read() is None


def test_real_heartbeat_thread_failure_commits_failed_then_releases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instance_id = "instance-a"
    identity = ProcessIdentity(
        pid=os.getpid(),
        created_at_ns=111,
        executable="python.exe",
    )
    job_dir = tmp_path / "data" / "jobs" / "JOB1"
    record_path = job_dir / "_work" / "runtime" / "worker.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
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
    monkeypatch.setenv("AICF_WORKER_LAUNCHED", "1")
    monkeypatch.setenv("AICF_WORKER_INSTANCE_ID", instance_id)
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(
        "aicf.background_worker.get_process_identity",
        lambda _pid: identity,
    )
    heartbeat_failed = threading.Event()
    real_heartbeat = __import__(
        "aicf.job_runtime",
        fromlist=["RuntimeLeaseHeartbeat"],
    ).RuntimeLeaseHeartbeat

    def heartbeat_factory(
        lease: RuntimeLease,
        heartbeat_instance_id: str,
    ) -> object:
        return real_heartbeat(
            lease,
            heartbeat_instance_id,
            interval=0.001,
            request_cancel=heartbeat_failed.set,
        )

    monkeypatch.setattr(
        "aicf.background_worker.RuntimeLeaseHeartbeat",
        heartbeat_factory,
    )
    original_heartbeat = RuntimeLease.heartbeat
    attempts = 0

    def fail_real_heartbeat(
        lease: RuntimeLease,
        heartbeat_instance_id: str,
    ) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts >= 2:
            raise RuntimeLeaseError("real heartbeat write failed")
        return original_heartbeat(lease, heartbeat_instance_id)

    monkeypatch.setattr(RuntimeLease, "heartbeat", fail_real_heartbeat)

    def wait_for_failure(_job_id: str) -> dict[str, str]:
        assert heartbeat_failed.wait(timeout=1)
        return {"status": "READY_TO_PUBLISH"}

    exit_code = run_worker(
        "JOB1",
        job_dir,
        run_autopilot=wait_for_failure,
        inhibitor_factory=lambda: SleepInhibitor(
            set_state=lambda flags: flags
        ),
    )

    saved = WorkerRecord.model_validate_json(
        record_path.read_text(encoding="utf-8")
    )
    assert exit_code == 1
    assert attempts >= 2
    assert saved.finished_at is not None
    assert saved.terminal_status == "FAILED"
    assert "real heartbeat write failed" in (saved.error or "")
    assert RuntimeLease(tmp_path).read() is None


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
    launch_record_requested = threading.Event()

    def publish_matching_record() -> None:
        assert launch_record_requested.wait(timeout=1)
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
            original_read = read_worker_record

            def read_worker_record_after_signal(
                job_dir: Path,
            ) -> WorkerRecord | None:
                record = original_read(job_dir)
                if record is not None and record.instance_id == "old-instance":
                    launch_record_requested.set()
                return record

            patch.setattr(
                "aicf.background_worker.get_process_identity",
                lambda _pid: identity,
            )
            patch.setattr(
                "aicf.background_worker.read_worker_record",
                read_worker_record_after_signal,
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
