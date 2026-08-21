from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from aicf.background_worker import (
    WorkerRecord,
    read_worker_record,
    write_worker_record,
)
from aicf.database import JobRepository
from aicf.job_lifecycle import (
    FORCE_INTERRUPT_REASON,
    JobDeletionOutcome,
    JobLifecycleConflictError,
    JobLifecycleCoordinator,
    JobLifecycleError,
    JobLifecycleIdentityError,
    JobLifecycleOutcome,
    JobLifecyclePersistenceError,
    JobLifecycleProcessUnknownError,
    JobLifecycleTerminationError,
)
from aicf.job_runtime import RuntimeLease, RuntimeLeaseError
from aicf.process_identity import (
    ProcessIdentity,
    ProcessProbe,
    ProcessProbeStatus,
)
from aicf.state_machine import PipelineStage


def _setup_running_job(
    root: Path,
) -> tuple[JobRepository, Path, ProcessIdentity, WorkerRecord]:
    repository = JobRepository(root / "data" / "content.db")
    job_dir = root / "data" / "jobs" / "JOB1"
    repository.create_job("JOB1", job_dir)
    repository.start_stage("JOB1", PipelineStage.DIRECTION_LOADED)
    identity = ProcessIdentity(
        pid=456,
        created_at_ns=123456,
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
    write_worker_record(job_dir, record)
    RuntimeLease(
        root,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=identity,
        ),
    ).acquire("JOB1", "instance-a", identity, job_dir=job_dir)
    (job_dir / ".autopilot.lock").write_text("legacy-lock", encoding="utf-8")
    return repository, job_dir, identity, record


def test_repository_interruption_uses_explicit_cas_without_worker_file(
    tmp_path: Path,
) -> None:
    repository, job_dir, _identity, _record = _setup_running_job(tmp_path)
    before = repository.get_job("JOB1")
    (job_dir / "_work" / "runtime" / "worker.json").unlink()

    result = repository.mark_interrupted(
        "JOB1",
        PipelineStage.DIRECTION_LOADED,
        "用户强制停止",
        "instance-a",
        expected_version=before.version,
    )

    assert result.version == before.version + 1
    assert result.current_stage == PipelineStage.FAILED_RETRYABLE
    assert (
        result.stages["DIRECTION_LOADED"]["worker_instance_id"]
        == "instance-a"
    )


def test_force_interrupt_keeps_three_owners_consistent(
    tmp_path: Path,
) -> None:
    repository, job_dir, _identity, _record = _setup_running_job(tmp_path)
    coordinator = JobLifecycleCoordinator(
        tmp_path,
        repository,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.NOT_RUNNING
        ),
    )

    result = coordinator.force_interrupt("JOB1", "用户强制停止")

    database_status = repository.get_job("JOB1")
    snapshot = json.loads(
        (job_dir / "status.json").read_text(encoding="utf-8")
    )
    worker = read_worker_record(job_dir)
    assert (
        result.job_status.version
        == database_status.version
        == snapshot["version"]
    )
    assert result.outcome == JobLifecycleOutcome.COMPLETED
    assert database_status.current_stage == PipelineStage.FAILED_RETRYABLE
    assert snapshot["current_stage"] == "FAILED_RETRYABLE"
    assert snapshot["failed_stage"] == "DIRECTION_LOADED"
    assert worker is not None
    assert worker.instance_id == "instance-a"
    assert worker.finished_at is not None
    assert worker.terminal_status == "FORCE_STOPPED"
    assert worker.error == FORCE_INTERRUPT_REASON
    assert (
        database_status.stages["DIRECTION_LOADED"]["error"]
        == FORCE_INTERRUPT_REASON
    )
    assert RuntimeLease(tmp_path).read() is None
    assert (job_dir / ".autopilot.lock").exists()


def test_force_interrupt_returns_repair_result_after_worker_commit_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, job_dir, _identity, _record = _setup_running_job(tmp_path)
    coordinator = JobLifecycleCoordinator(
        tmp_path,
        repository,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.NOT_RUNNING
        ),
    )
    original_write = coordinator._write_current_record
    monkeypatch.setattr(
        coordinator,
        "_write_current_record",
        lambda *_args: (_ for _ in ()).throw(OSError("worker write failed")),
    )

    partial = coordinator.force_interrupt("JOB1")

    assert partial.outcome == JobLifecycleOutcome.COMMITTED_NEEDS_REPAIR
    assert partial.repair_reason is not None
    assert "Worker终态写入失败" in partial.repair_reason
    assert (
        repository.get_job("JOB1").current_stage
        == PipelineStage.FAILED_RETRYABLE
    )
    assert read_worker_record(job_dir).finished_at is None
    assert RuntimeLease(tmp_path).read() is not None

    monkeypatch.setattr(coordinator, "_write_current_record", original_write)
    repaired = coordinator.force_interrupt("JOB1")

    assert repaired.outcome == JobLifecycleOutcome.COMPLETED
    assert repaired.repair_reason is None
    assert read_worker_record(job_dir).finished_at is not None
    assert RuntimeLease(tmp_path).read() is None


def test_force_interrupt_returns_repair_result_after_lease_release_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, job_dir, _identity, _record = _setup_running_job(tmp_path)
    coordinator = JobLifecycleCoordinator(
        tmp_path,
        repository,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.NOT_RUNNING
        ),
    )
    original_release = RuntimeLease.release_locked
    monkeypatch.setattr(
        RuntimeLease,
        "release_locked",
        lambda *_args: (_ for _ in ()).throw(
            RuntimeLeaseError("lease write failed")
        ),
    )

    partial = coordinator.force_interrupt("JOB1")

    assert partial.outcome == JobLifecycleOutcome.COMMITTED_NEEDS_REPAIR
    assert partial.repair_reason is not None
    assert "租约清理失败" in partial.repair_reason
    assert read_worker_record(job_dir).finished_at is not None
    assert RuntimeLease(tmp_path).read() is not None

    monkeypatch.setattr(RuntimeLease, "release_locked", original_release)
    repaired = coordinator.force_interrupt("JOB1")

    assert repaired.outcome == JobLifecycleOutcome.COMPLETED
    assert repaired.repair_reason is None
    assert RuntimeLease(tmp_path).read() is None


def test_force_interrupt_snapshot_failure_retains_worker_and_lease_for_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, job_dir, _identity, original = _setup_running_job(tmp_path)
    monkeypatch.setattr(
        repository,
        "_write_status_locked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("snapshot write failed")
        ),
    )
    coordinator = JobLifecycleCoordinator(
        tmp_path,
        repository,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.NOT_RUNNING
        ),
    )

    partial = coordinator.force_interrupt("JOB1")

    assert partial.outcome == JobLifecycleOutcome.COMMITTED_NEEDS_REPAIR
    assert partial.repair_reason is not None
    assert "状态快照同步失败" in partial.repair_reason
    assert partial.job_status.snapshot_dirty is True
    assert read_worker_record(job_dir) == original
    retained = RuntimeLease(tmp_path).read()
    assert retained is not None
    assert retained.instance_id == original.instance_id


def test_force_interrupt_retries_snapshot_repair_before_idempotent_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, job_dir, _identity, original = _setup_running_job(tmp_path)
    original_write_locked = repository._write_status_locked
    monkeypatch.setattr(
        repository,
        "_write_status_locked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("snapshot write failed")
        ),
    )
    coordinator = JobLifecycleCoordinator(
        tmp_path,
        repository,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.NOT_RUNNING
        ),
    )
    first = coordinator.force_interrupt("JOB1")
    assert first.outcome == JobLifecycleOutcome.COMMITTED_NEEDS_REPAIR

    rebuild_attempts: list[int] = []

    def fail_rebuild(_path: Path, status: object) -> None:
        rebuild_attempts.append(status.version)  # type: ignore[attr-defined]
        raise OSError("snapshot still unavailable")

    monkeypatch.setattr(repository, "_write_status_locked", fail_rebuild)
    still_dirty = coordinator.force_interrupt("JOB1")

    assert rebuild_attempts == [still_dirty.job_status.version]
    assert still_dirty.outcome == JobLifecycleOutcome.COMMITTED_NEEDS_REPAIR
    assert still_dirty.repair_reason is not None
    assert "状态快照同步失败" in still_dirty.repair_reason
    assert still_dirty.job_status.snapshot_dirty is True
    assert read_worker_record(job_dir) == original
    assert RuntimeLease(tmp_path).read() is not None

    monkeypatch.setattr(repository, "_write_status_locked", original_write_locked)
    completed = coordinator.force_interrupt("JOB1")

    database_status = repository.get_job("JOB1")
    snapshot = json.loads(
        (job_dir / "status.json").read_text(encoding="utf-8")
    )
    worker = read_worker_record(job_dir)
    assert completed.outcome == JobLifecycleOutcome.COMPLETED
    assert completed.repair_reason is None
    assert completed.job_status.snapshot_dirty is False
    assert database_status.snapshot_dirty is False
    assert database_status.version == snapshot["version"]
    assert snapshot["snapshot_dirty"] is False
    assert database_status.current_stage == PipelineStage.FAILED_RETRYABLE
    assert snapshot["current_stage"] == "FAILED_RETRYABLE"
    assert database_status.failed_stage == PipelineStage.DIRECTION_LOADED
    assert snapshot["failed_stage"] == "DIRECTION_LOADED"
    assert (
        database_status.stages["DIRECTION_LOADED"]["error"]
        == snapshot["stages"]["DIRECTION_LOADED"]["error"]
        == FORCE_INTERRUPT_REASON
    )
    assert worker is not None
    assert worker.instance_id == original.instance_id
    assert worker.finished_at is not None
    assert worker.terminal_status == "FORCE_STOPPED"
    assert worker.error == FORCE_INTERRUPT_REASON
    assert RuntimeLease(tmp_path).read() is None


def test_force_interrupt_termination_failure_changes_no_owner(
    tmp_path: Path,
) -> None:
    repository, job_dir, identity, original = _setup_running_job(tmp_path)
    before = repository.get_job("JOB1")
    coordinator = JobLifecycleCoordinator(
        tmp_path,
        repository,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=identity,
        ),
        terminate_process=lambda _pid: (_ for _ in ()).throw(
            OSError("access denied")
        ),
    )

    with pytest.raises(JobLifecycleTerminationError) as exc_info:
        coordinator.force_interrupt("JOB1", "用户强制停止")

    assert exc_info.value.code == "worker_termination_failed"
    assert repository.get_job("JOB1").version == before.version
    assert read_worker_record(job_dir) == original
    assert RuntimeLease(tmp_path).read() is not None
    assert (job_dir / ".autopilot.lock").exists()


def test_force_interrupt_detects_worker_replacement_before_cas(
    tmp_path: Path,
) -> None:
    repository, job_dir, identity, _record = _setup_running_job(tmp_path)
    before = repository.get_job("JOB1")
    replacement = WorkerRecord(
        job_id="JOB1",
        pid=789,
        started_at="new",
        log_path="new.log",
        instance_id="instance-b",
        process_created_at_ns=999,
        process_executable="python.exe",
    )
    probes = iter(
        (
            ProcessProbe(status=ProcessProbeStatus.RUNNING, identity=identity),
            ProcessProbe(status=ProcessProbeStatus.NOT_RUNNING),
        )
    )

    def replace_during_termination(_pid: int) -> None:
        write_worker_record(job_dir, replacement)

    coordinator = JobLifecycleCoordinator(
        tmp_path,
        repository,
        process_probe=lambda _pid: next(probes),
        terminate_process=replace_during_termination,
    )

    with pytest.raises(JobLifecycleConflictError, match="终止过程中"):
        coordinator.force_interrupt("JOB1", "过期停止")

    assert repository.get_job("JOB1").version == before.version
    assert read_worker_record(job_dir) == replacement
    retained = RuntimeLease(tmp_path).read()
    assert retained is not None
    assert retained.instance_id == "instance-a"


def test_force_interrupt_repository_failure_retains_runtime_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, job_dir, _identity, original = _setup_running_job(tmp_path)
    before = repository.get_job("JOB1")
    monkeypatch.setattr(
        repository,
        "mark_interrupted",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("database unavailable")
        ),
    )
    coordinator = JobLifecycleCoordinator(
        tmp_path,
        repository,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.NOT_RUNNING
        ),
    )

    with pytest.raises(JobLifecyclePersistenceError):
        coordinator.force_interrupt("JOB1", "用户强制停止")

    assert repository.get_job("JOB1").version == before.version
    assert read_worker_record(job_dir) == original
    assert RuntimeLease(tmp_path).read() is not None
    assert (job_dir / ".autopilot.lock").exists()


def test_force_interrupt_rejects_pid_reuse_without_termination(
    tmp_path: Path,
) -> None:
    repository, job_dir, _identity, original = _setup_running_job(tmp_path)
    terminated: list[int] = []
    coordinator = JobLifecycleCoordinator(
        tmp_path,
        repository,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.RUNNING,
            identity=ProcessIdentity(
                pid=456,
                created_at_ns=999,
                executable="browser.exe",
            ),
        ),
        terminate_process=terminated.append,
    )

    with pytest.raises(JobLifecycleIdentityError):
        coordinator.force_interrupt("JOB1", "用户强制停止")

    assert terminated == []
    assert read_worker_record(job_dir) == original
    assert repository.get_job("JOB1").current_stage == PipelineStage.DIRECTION_LOADED


def test_competing_force_interrupts_are_idempotent_and_terminate_once(
    tmp_path: Path,
) -> None:
    repository, job_dir, identity, _record = _setup_running_job(tmp_path)
    running = True
    calls: list[int] = []
    state_lock = threading.Lock()
    barrier = threading.Barrier(2)
    results = []
    errors: list[BaseException] = []

    def probe(_pid: int) -> ProcessProbe:
        with state_lock:
            if running:
                return ProcessProbe(
                    status=ProcessProbeStatus.RUNNING,
                    identity=identity,
                )
        return ProcessProbe(status=ProcessProbeStatus.NOT_RUNNING)

    def terminate(pid: int) -> None:
        nonlocal running
        with state_lock:
            calls.append(pid)
            running = False

    def execute() -> None:
        barrier.wait()
        try:
            results.append(
                JobLifecycleCoordinator(
                    tmp_path,
                    repository,
                    process_probe=probe,
                    terminate_process=terminate,
                ).force_interrupt("JOB1", "用户强制停止")
            )
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=execute) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert {
        result.outcome for result in results
    } == {JobLifecycleOutcome.COMPLETED}
    assert calls == [456]
    status = repository.get_job("JOB1")
    worker = read_worker_record(job_dir)
    assert status.current_stage == PipelineStage.FAILED_RETRYABLE
    assert worker is not None and worker.finished_at is not None
    assert RuntimeLease(tmp_path).read() is None


def test_stable_domain_errors_expose_machine_readable_codes() -> None:
    assert JobLifecycleError.code == "job_lifecycle_error"
    assert JobLifecycleConflictError.code == "job_lifecycle_conflict"
    assert JobLifecycleIdentityError.code == "worker_identity_mismatch"


@pytest.mark.parametrize(
    "stored_reason",
    [None, "", "旧GUI自定义原因"],
)
def test_force_interrupt_uses_one_canonical_reason(
    tmp_path: Path,
    stored_reason: str | None,
) -> None:
    repository, job_dir, _identity, _record = _setup_running_job(tmp_path)
    coordinator = JobLifecycleCoordinator(
        tmp_path,
        repository,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.NOT_RUNNING
        ),
    )

    result = coordinator.force_interrupt("JOB1", stored_reason)

    assert result.outcome == JobLifecycleOutcome.COMPLETED
    status = repository.get_job("JOB1")
    worker = read_worker_record(job_dir)
    assert status.stages["DIRECTION_LOADED"]["error"] == FORCE_INTERRUPT_REASON
    assert worker is not None
    assert worker.error == FORCE_INTERRUPT_REASON


def test_delete_job_closes_database_workdir_delivery_and_lease(
    tmp_path: Path,
) -> None:
    repository, job_dir, _identity, record = _setup_running_job(tmp_path)
    output_dir = tmp_path / "outputs" / "JOB1"
    output_dir.mkdir(parents=True)
    (output_dir / "最终视频.mp4").write_bytes(b"video")
    terminal = read_worker_record(job_dir)
    assert terminal is not None
    terminal.finished_at = "done"
    terminal.terminal_status = "COMPLETED"
    write_worker_record(job_dir, terminal)

    result = JobLifecycleCoordinator(
        tmp_path,
        repository,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.NOT_RUNNING
        ),
    ).delete_job("JOB1")

    assert result.outcome is JobDeletionOutcome.COMPLETED
    assert result.database_deleted is True
    assert result.cleanup_errors == ()
    with pytest.raises(KeyError):
        repository.get_job("JOB1")
    assert not job_dir.exists()
    assert not output_dir.exists()
    assert RuntimeLease(tmp_path).read() is None
    assert record.instance_id == "instance-a"


def test_delete_job_retains_everything_when_lease_release_is_denied_then_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, job_dir, _identity, _record = _setup_running_job(tmp_path)
    output_dir = tmp_path / "outputs" / "JOB1"
    output_dir.mkdir(parents=True)
    (output_dir / "最终视频.mp4").write_bytes(b"video")
    terminal = read_worker_record(job_dir)
    assert terminal is not None
    terminal.finished_at = "done"
    terminal.terminal_status = "COMPLETED"
    write_worker_record(job_dir, terminal)

    lease = RuntimeLease(tmp_path)
    lease_path = lease.path
    path_type = type(lease_path)
    original_unlink = path_type.unlink
    release_attempts = 0

    def deny_first_lease_release(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal release_attempts
        if path == lease_path and release_attempts == 0:
            release_attempts += 1
            raise PermissionError("lease file is locked")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(path_type, "unlink", deny_first_lease_release)
    coordinator = JobLifecycleCoordinator(
        tmp_path,
        repository,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.NOT_RUNNING
        ),
    )

    with pytest.raises(JobLifecyclePersistenceError) as exc_info:
        coordinator.delete_job("JOB1")

    assert isinstance(exc_info.value.__cause__, RuntimeLeaseError)
    assert isinstance(exc_info.value.__cause__.__cause__, PermissionError)
    assert repository.get_job("JOB1").job_id == "JOB1"
    assert job_dir.exists()
    assert output_dir.exists()
    assert RuntimeLease(tmp_path).read() is not None

    result = coordinator.delete_job("JOB1")

    assert result.outcome is JobDeletionOutcome.COMPLETED
    with pytest.raises(KeyError):
        repository.get_job("JOB1")
    assert not job_dir.exists()
    assert not output_dir.exists()
    assert RuntimeLease(tmp_path).read() is None


def test_delete_job_fails_closed_when_worker_probe_raises(
    tmp_path: Path,
) -> None:
    repository, job_dir, _identity, _record = _setup_running_job(tmp_path)
    coordinator = JobLifecycleCoordinator(
        tmp_path,
        repository,
        process_probe=lambda _pid: (_ for _ in ()).throw(
            OSError("process table unavailable")
        ),
    )

    with pytest.raises(JobLifecycleProcessUnknownError):
        coordinator.delete_job("JOB1")

    assert repository.get_job("JOB1").job_id == "JOB1"
    assert job_dir.exists()
    assert RuntimeLease(tmp_path).read() is not None


def test_delete_job_reports_filesystem_cleanup_after_database_commit(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "data" / "content.db")
    job_dir = tmp_path / "data" / "jobs" / "JOB-DELETE-PARTIAL"
    repository.create_job("JOB-DELETE-PARTIAL", job_dir)

    result = JobLifecycleCoordinator(
        tmp_path,
        repository,
        cleanup_tree=lambda _path: (_ for _ in ()).throw(
            PermissionError("directory busy")
        ),
    ).delete_job("JOB-DELETE-PARTIAL")

    assert result.outcome is JobDeletionOutcome.COMMITTED_NEEDS_REPAIR
    assert result.database_deleted is True
    assert len(result.cleanup_errors) == 1
    assert "directory busy" in result.cleanup_errors[0]
    with pytest.raises(KeyError):
        repository.get_job("JOB-DELETE-PARTIAL")
