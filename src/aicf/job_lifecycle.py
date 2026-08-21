"""Application-level owner for coordinated Job/Worker lifecycle changes."""

from __future__ import annotations

import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

from .atomic_io import atomic_write_text
from .background_worker import (
    WorkerRecord,
    read_worker_record,
    worker_record_path,
    write_worker_record,
)
from .database import JobRepository, JobStatus
from .file_lock import lock_is_active, os_file_lock
from .job_runtime import RuntimeLease, RuntimeLeaseError
from .process_identity import (
    ProcessProbe,
    ProcessProbeStatus,
    probe_process_identity,
    process_identity_matches,
)
from .state_machine import PipelineStage, TransitionError, is_terminal_stage
from .worker_stop_ipc import stop_request_path, terminate_process_tree


class JobLifecycleError(RuntimeError):
    """Stable base error exposed to GUI and other application adapters."""

    code = "job_lifecycle_error"


class JobLifecycleNotRunningError(JobLifecycleError):
    code = "job_not_running"


class JobLifecycleIdentityError(JobLifecycleError):
    code = "worker_identity_mismatch"


class JobLifecycleProcessUnknownError(JobLifecycleError):
    code = "worker_process_unknown"


class JobLifecycleTerminationError(JobLifecycleError):
    code = "worker_termination_failed"


class JobLifecycleConflictError(JobLifecycleError):
    code = "job_lifecycle_conflict"


class JobLifecyclePersistenceError(JobLifecycleError):
    code = "job_lifecycle_persistence_failed"


FORCE_INTERRUPT_REASON = "用户强制停止，可点击继续/恢复"


class JobLifecycleOutcome(str, Enum):
    COMPLETED = "COMPLETED"
    COMMITTED_NEEDS_REPAIR = "COMMITTED_NEEDS_REPAIR"


class JobDeletionOutcome(str, Enum):
    COMPLETED = "COMPLETED"
    COMMITTED_NEEDS_REPAIR = "COMMITTED_NEEDS_REPAIR"


@dataclass(frozen=True)
class JobLifecycleResult:
    job_status: JobStatus
    worker_record: WorkerRecord
    lease_released: bool
    outcome: JobLifecycleOutcome = JobLifecycleOutcome.COMPLETED
    repair_reason: str | None = None


@dataclass(frozen=True)
class JobDeletionResult:
    job_id: str
    database_deleted: bool
    cleanup_errors: tuple[str, ...] = ()

    @property
    def outcome(self) -> JobDeletionOutcome:
        return (
            JobDeletionOutcome.COMMITTED_NEEDS_REPAIR
            if self.cleanup_errors
            else JobDeletionOutcome.COMPLETED
        )


class JobLifecycleCoordinator:
    """Serialize runtime checks, process stop and business-state CAS.

    All destructive decisions are made while holding the same project-level
    lifecycle lock used by Worker startup.  This closes the check/use gap
    between observing ``worker.json`` and terminating its process.
    """

    def __init__(
        self,
        project_root: str | Path,
        repository: JobRepository,
        *,
        process_probe: Callable[[int], ProcessProbe] = probe_process_identity,
        terminate_process: Callable[[int], None] = terminate_process_tree,
        confirmation_timeout: float = 5.0,
        poll_interval: float = 0.05,
        sleep: Callable[[float], None] = time.sleep,
        lock_probe: Callable[[Path], bool] = lambda path: lock_is_active(
            path, stale_after=120.0
        ),
        cleanup_tree: Callable[[Path], None] = shutil.rmtree,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.repository = repository
        self._process_probe = process_probe
        self._terminate_process = terminate_process
        self._confirmation_timeout = confirmation_timeout
        self._poll_interval = poll_interval
        self._sleep = sleep
        self._lock_probe = lock_probe
        self._cleanup_tree = cleanup_tree

    @property
    def lifecycle_lock_path(self) -> Path:
        return self.project_root / "_work" / "runtime" / "worker-start.lock"

    def request_stop(self, job_id: str) -> WorkerRecord:
        """Request a cooperative stop after validating the current instance."""
        with self._lifecycle_lock():
            status = self._get_status(job_id)
            record = self._read_current_record(status)
            self._validate_runtime_owner(job_id, record)
            probe = self._probe_process(record.pid)
            self._require_matching_running_process(record, probe)
            assert record.instance_id is not None
            atomic_write_text(
                stop_request_path(status.output_dir, record.instance_id),
                record.instance_id + "\n",
            )
            record.stop_requested_at = self._now()
            record.terminal_status = "STOP_REQUESTED"
            try:
                self._write_current_record(status, record)
            except OSError as error:
                raise JobLifecyclePersistenceError(
                    "停止请求已创建，但Worker记录更新失败"
                ) from error
            return record

    def delete_job(self, job_id: str) -> JobDeletionResult:
        """Delete a stopped Job and all owned files under the lifecycle lock."""
        with self._lifecycle_lock():
            status = self._get_status(job_id)
            job_dir = Path(status.output_dir)
            worker = read_worker_record(job_dir)
            if worker is None and worker_record_path(job_dir).is_file():
                raise JobLifecycleProcessUnknownError(
                    "Worker记录存在但不可读，无法确认任务已停止"
                )

            try:
                active_lock = self._lock_probe(job_dir / ".autopilot.lock")
            except Exception as error:
                raise JobLifecycleProcessUnknownError(
                    "无法确认任务运行锁状态"
                ) from error

            lease = RuntimeLease(
                self.project_root,
                process_probe=self._process_probe,
            )
            try:
                lease_record = lease.read_locked()
            except RuntimeLeaseError as error:
                raise JobLifecyclePersistenceError(
                    "项目Worker租约读取失败"
                ) from error

            runtime_records = [
                record
                for record in (worker, lease_record)
                if record is not None
                and getattr(record, "job_id", None) == job_id
                and getattr(record, "finished_at", None) is None
            ]
            for record in runtime_records:
                probe = self._probe_process(record.pid)
                if probe.status is ProcessProbeStatus.UNKNOWN:
                    raise JobLifecycleProcessUnknownError(
                        "无法确认待删除任务的Worker进程状态"
                    )
                if (
                    probe.status is ProcessProbeStatus.RUNNING
                    and process_identity_matches(
                        probe.identity,
                        pid=record.pid,
                        created_at_ns=record.process_created_at_ns,
                        executable=record.process_executable,
                    )
                ):
                    raise JobLifecycleConflictError(
                        "任务仍在运行，请先停止后再删除"
                    )
            if active_lock:
                raise JobLifecycleConflictError(
                    "任务运行锁仍然活跃，请先停止后再删除"
                )

            if lease_record is not None and lease_record.job_id == job_id:
                try:
                    released = lease.release_locked(lease_record.instance_id)
                except RuntimeLeaseError as error:
                    raise JobLifecyclePersistenceError(
                        "项目Worker租约释放失败，任务未删除"
                    ) from error
                if not released:
                    raise JobLifecycleConflictError(
                        "项目Worker租约实例已变化，任务未删除"
                    )

            try:
                deleted = self.repository.delete_job(job_id)
            except Exception as error:
                raise JobLifecyclePersistenceError(
                    "任务权威记录删除失败"
                ) from error
            if not deleted:
                raise JobLifecycleConflictError("任务已被其他操作删除")

            cleanup_errors: list[str] = []
            candidates = (
                job_dir,
                self.project_root / "outputs" / job_id,
            )
            seen: set[Path] = set()
            for path in candidates:
                resolved = path.resolve()
                if resolved in seen or not path.exists():
                    continue
                seen.add(resolved)
                try:
                    self._cleanup_tree(path)
                except OSError as error:
                    cleanup_errors.append(f"{path}: {error}")
            return JobDeletionResult(
                job_id=job_id,
                database_deleted=True,
                cleanup_errors=tuple(cleanup_errors),
            )

    def force_interrupt(
        self,
        job_id: str,
        reason: str | None = None,
    ) -> JobLifecycleResult:
        """Force-stop and commit, returning repair state after the commit point."""
        del reason
        canonical_reason = FORCE_INTERRUPT_REASON
        with self._lifecycle_lock():
            status = self._get_status(job_id)
            record = self._read_current_record(status)
            self._validate_runtime_owner(job_id, record)
            assert record.instance_id is not None
            if record.finished_at is not None and not self._is_committed_interrupt(
                status,
                record,
            ):
                raise JobLifecycleNotRunningError("Worker已经结束")
            if self._is_committed_interrupt(status, record) and status.snapshot_dirty:
                try:
                    status = self.repository.rebuild_snapshot(job_id)
                except Exception as error:
                    return self._snapshot_repair_pending(
                        status,
                        record,
                        f"状态快照重建失败: {error}",
                    )
                if status.snapshot_dirty:
                    return self._snapshot_repair_pending(status, record)
            lease = RuntimeLease(
                self.project_root,
                process_probe=self._process_probe,
            )
            lease_record = self._read_matching_lease(
                lease,
                job_id,
                record.instance_id,
            )

            probe = self._probe_process(record.pid)
            self._stop_and_confirm(record, probe)
            self._assert_same_worker_instance(status, record)

            interrupted = self._mark_or_confirm_interrupted(
                status,
                record.instance_id,
                canonical_reason,
            )
            if interrupted.snapshot_dirty:
                return self._snapshot_repair_pending(interrupted, record)
            terminal = record
            released = False
            repair_errors: list[str] = []
            try:
                terminal = self._commit_force_stopped(
                    interrupted,
                    record.instance_id,
                    canonical_reason,
                )
            except JobLifecycleError as error:
                repair_errors.append(str(error))
            except Exception as error:
                repair_errors.append(f"Worker终态写入失败: {error}")

            if lease_record is not None and not repair_errors:
                try:
                    released = lease.release_locked(record.instance_id)
                except RuntimeLeaseError as error:
                    repair_errors.append(
                        f"任务已中断，但项目Worker租约清理失败: {error}"
                    )
                else:
                    if not released:
                        repair_errors.append(
                            "项目Worker租约实例已变化，拒绝清理新实例"
                        )

            return JobLifecycleResult(
                job_status=interrupted,
                worker_record=terminal,
                lease_released=released,
                outcome=(
                    JobLifecycleOutcome.COMMITTED_NEEDS_REPAIR
                    if repair_errors
                    else JobLifecycleOutcome.COMPLETED
                ),
                repair_reason="；".join(repair_errors) or None,
            )

    @staticmethod
    def _snapshot_repair_pending(
        status: JobStatus,
        record: WorkerRecord,
        reason: str = "任务中断已提交，但状态快照同步失败，等待修复",
    ) -> JobLifecycleResult:
        return JobLifecycleResult(
            job_status=status,
            worker_record=record,
            lease_released=False,
            outcome=JobLifecycleOutcome.COMMITTED_NEEDS_REPAIR,
            repair_reason=reason,
        )

    @contextmanager
    def _lifecycle_lock(self) -> Iterator[None]:
        try:
            with os_file_lock(
                self.lifecycle_lock_path,
                timeout=5.0,
                timeout_message="项目Worker生命周期正在变更，请稍后重试",
            ):
                yield
        except JobLifecycleError:
            raise
        except TimeoutError as error:
            raise JobLifecycleConflictError(str(error)) from error
        except OSError as error:
            raise JobLifecyclePersistenceError(
                "项目Worker生命周期锁访问失败"
            ) from error

    def _get_status(self, job_id: str) -> JobStatus:
        try:
            return self.repository.get_job(job_id)
        except KeyError as error:
            raise JobLifecycleNotRunningError(f"任务不存在: {job_id}") from error
        except Exception as error:
            raise JobLifecyclePersistenceError("任务状态读取失败") from error

    @staticmethod
    def _now() -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).isoformat()

    def _read_current_record(self, status: JobStatus) -> WorkerRecord:
        job_dir = Path(status.output_dir)
        record = read_worker_record(job_dir)
        if record is None:
            detail = (
                "Worker记录损坏或不可读"
                if worker_record_path(job_dir).is_file()
                else "没有找到Worker记录"
            )
            raise JobLifecycleNotRunningError(detail)
        return record

    @staticmethod
    def _validate_runtime_owner(job_id: str, record: WorkerRecord) -> None:
        if record.job_id != job_id:
            raise JobLifecycleIdentityError("Worker记录不属于目标任务")
        if not record.instance_id:
            raise JobLifecycleIdentityError("Worker记录缺少实例身份")
        if record.process_created_at_ns is None or not record.process_executable:
            raise JobLifecycleIdentityError("Worker记录缺少完整进程身份")

    @staticmethod
    def _require_matching_running_process(
        record: WorkerRecord,
        probe: ProcessProbe,
    ) -> None:
        if record.finished_at is not None:
            raise JobLifecycleNotRunningError("Worker已经结束")
        if probe.status == ProcessProbeStatus.UNKNOWN:
            raise JobLifecycleProcessUnknownError("无法确认Worker进程状态")
        if probe.status != ProcessProbeStatus.RUNNING:
            raise JobLifecycleNotRunningError("Worker进程已经退出")
        if not process_identity_matches(
            probe.identity,
            pid=record.pid,
            created_at_ns=record.process_created_at_ns,
            executable=record.process_executable,
        ):
            raise JobLifecycleIdentityError(
                "Worker进程身份校验失败，已拒绝停止以避免误杀"
            )

    def _read_matching_lease(
        self,
        lease: RuntimeLease,
        job_id: str,
        instance_id: str,
    ):
        try:
            current = lease.read_locked()
        except RuntimeLeaseError as error:
            raise JobLifecyclePersistenceError("项目Worker租约读取失败") from error
        if current is not None and (
            current.job_id != job_id or current.instance_id != instance_id
        ):
            raise JobLifecycleConflictError(
                "项目Worker租约已属于其他任务或实例"
            )
        return current

    def _probe_process(self, pid: int) -> ProcessProbe:
        try:
            return self._process_probe(pid)
        except Exception as error:
            raise JobLifecycleProcessUnknownError(
                "Worker进程探测失败"
            ) from error

    def _stop_and_confirm(
        self,
        record: WorkerRecord,
        initial_probe: ProcessProbe,
    ) -> None:
        if record.finished_at is not None:
            return
        if initial_probe.status == ProcessProbeStatus.UNKNOWN:
            raise JobLifecycleProcessUnknownError("无法确认Worker进程状态")
        if initial_probe.status == ProcessProbeStatus.RUNNING:
            if not process_identity_matches(
                initial_probe.identity,
                pid=record.pid,
                created_at_ns=record.process_created_at_ns,
                executable=record.process_executable,
            ):
                raise JobLifecycleIdentityError(
                    "Worker进程身份校验失败，已拒绝强制终止"
                )
            try:
                self._terminate_process(record.pid)
            except Exception as error:
                raise JobLifecycleTerminationError(
                    f"Worker强制终止失败: {error}"
                ) from error

            deadline = time.monotonic() + max(0.0, self._confirmation_timeout)
            while True:
                confirmed = self._probe_process(record.pid)
                if confirmed.status == ProcessProbeStatus.NOT_RUNNING:
                    return
                if (
                    confirmed.status == ProcessProbeStatus.RUNNING
                    and not process_identity_matches(
                        confirmed.identity,
                        pid=record.pid,
                        created_at_ns=record.process_created_at_ns,
                        executable=record.process_executable,
                    )
                ):
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if confirmed.status == ProcessProbeStatus.UNKNOWN:
                        raise JobLifecycleProcessUnknownError(
                            "强制终止后无法确认Worker是否退出"
                        )
                    raise JobLifecycleTerminationError(
                        "强制终止后Worker仍在运行"
                    )
                self._sleep(min(max(0.0, self._poll_interval), remaining))

    def _mark_or_confirm_interrupted(
        self,
        status: JobStatus,
        instance_id: str,
        reason: str,
    ) -> JobStatus:
        if self._is_committed_interrupt(status, instance_id):
            return status
        if status.current_stage is None or is_terminal_stage(status.current_stage):
            raise JobLifecycleConflictError("当前任务没有可中断的运行阶段")
        try:
            return self.repository.mark_interrupted(
                status.job_id,
                status.current_stage,
                reason,
                instance_id,
                expected_version=status.version,
            )
        except TransitionError as error:
            raise JobLifecycleConflictError(str(error)) from error
        except Exception as error:
            raise JobLifecyclePersistenceError("任务中断状态提交失败") from error

    @staticmethod
    def _is_committed_interrupt(
        status: JobStatus,
        worker: WorkerRecord | str,
    ) -> bool:
        instance_id = (
            worker.instance_id if isinstance(worker, WorkerRecord) else worker
        )
        return bool(
            status.current_stage == PipelineStage.FAILED_RETRYABLE
            and status.failed_stage is not None
            and status.stages.get(status.failed_stage.value, {}).get(
                "worker_instance_id"
            )
            == instance_id
        )

    def _assert_same_worker_instance(
        self,
        status: JobStatus,
        expected: WorkerRecord,
    ) -> None:
        current = self._read_current_record(status)
        if (
            current.instance_id != expected.instance_id
            or current.pid != expected.pid
            or current.process_created_at_ns != expected.process_created_at_ns
            or current.process_executable != expected.process_executable
        ):
            raise JobLifecycleConflictError(
                "Worker实例在终止过程中已变化，拒绝提交过期状态"
            )

    def _commit_force_stopped(
        self,
        status: JobStatus,
        instance_id: str,
        reason: str,
    ) -> WorkerRecord:
        current = self._read_current_record(status)
        if current.instance_id != instance_id:
            raise JobLifecycleConflictError(
                "Worker实例已变化，拒绝覆盖新实例终态"
            )
        if current.finished_at is None:
            current.finished_at = self._now()
            current.terminal_status = "FORCE_STOPPED"
            current.error = reason
            try:
                self._write_current_record(status, current)
            except OSError as error:
                raise JobLifecyclePersistenceError(
                    "任务已中断，但Worker终态写入失败"
                ) from error
        return current

    @staticmethod
    def _write_current_record(status: JobStatus, record: WorkerRecord) -> None:
        current = read_worker_record(status.output_dir)
        if current is None or current.instance_id != record.instance_id:
            raise JobLifecycleConflictError(
                "Worker实例已变化，拒绝写入过期记录"
            )
        write_worker_record(status.output_dir, record)
