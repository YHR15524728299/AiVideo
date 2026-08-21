from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .atomic_io import atomic_write_text
from .file_lock import os_file_lock
from .job_runtime import (
    RuntimeLease,
    RuntimeLeaseError,
    RuntimeLeaseHeartbeat,
    RuntimeLeaseRecord,
)
from .process_identity import (
    ProcessIdentity,
    ProcessProbe,
    ProcessProbeStatus,
    get_process_identity,
    probe_process_identity,
    process_identity_matches,
    process_is_running,
)
from .subprocess_utils import silent_popen
from .worker_stop_ipc import (
    StopRequestMonitor,
    WorkerIdentityError,
    stop_request_path,
    terminate_current_process_tree,
    terminate_process_tree,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkerRecord(BaseModel):
    job_id: str
    pid: int
    started_at: str
    log_path: str
    instance_id: str | None = None
    process_created_at_ns: int | None = None
    process_executable: str | None = None
    research_strategy: str | None = None
    ready: bool = False
    stop_requested_at: str | None = None
    finished_at: str | None = None
    terminal_status: str | None = None
    error: str | None = None


class WorkerStartResult(BaseModel):
    job_id: str
    pid: int
    reused: bool
    log_path: str


class SleepInhibitor(AbstractContextManager["SleepInhibitor"]):
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001

    def __init__(self, set_state: Callable[[int], int] | None = None) -> None:
        self._set_state = set_state or self._windows_set_state
        self._active = False

    @staticmethod
    def _windows_set_state(flags: int) -> int:
        if os.name != "nt":
            return flags
        return int(ctypes.windll.kernel32.SetThreadExecutionState(flags))

    def __enter__(self) -> "SleepInhibitor":
        result = self._set_state(self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED)
        if result == 0:
            raise OSError("无法请求Windows在后台任务期间阻止系统睡眠")
        self._active = True
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._active:
            self._set_state(self.ES_CONTINUOUS)
            self._active = False


def worker_record_path(job_dir: str | Path) -> Path:
    return Path(job_dir) / "_work" / "runtime" / "worker.json"


def read_worker_record(job_dir: str | Path) -> WorkerRecord | None:
    path = worker_record_path(job_dir)
    if not path.is_file():
        return None
    try:
        return WorkerRecord.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def write_worker_record(job_dir: str | Path, record: WorkerRecord) -> None:
    atomic_write_text(
        worker_record_path(job_dir),
        record.model_dump_json(indent=2) + "\n",
    )


def authorize_current_worker_process(
    job_id: str,
    job_dir: str | Path,
    *,
    requested_research_strategy: str | None = None,
) -> WorkerRecord:
    """确认当前进程就是Launcher登记的Worker，并绑定其研究策略。"""
    if os.environ.get("AICF_WORKER_LAUNCHED") != "1":
        raise WorkerIdentityError("content-run只能由WorkerLauncher安全启动")
    instance_id = os.environ.get("AICF_WORKER_INSTANCE_ID")
    if not instance_id:
        raise WorkerIdentityError("缺少Worker启动实例ID，已拒绝content-run")
    record = read_worker_record(job_dir)
    if record is None:
        raise WorkerIdentityError("Worker运行记录缺失或不可读，已拒绝content-run")
    if record.job_id != job_id or record.instance_id != instance_id:
        raise WorkerIdentityError("Worker实例身份与启动记录不一致")
    if record.finished_at is not None:
        raise WorkerIdentityError("Worker实例已经结束，已拒绝content-run")
    identity = get_process_identity(os.getpid())
    if not process_identity_matches(
        identity,
        pid=record.pid,
        created_at_ns=record.process_created_at_ns,
        executable=record.process_executable,
    ):
        raise WorkerIdentityError("当前Worker进程身份与worker.json不一致")
    if (
        requested_research_strategy is not None
        and requested_research_strategy != record.research_strategy
    ):
        raise WorkerIdentityError(
            "请求的研究策略与Worker启动记录不一致，已拒绝content-run"
        )
    return record


def _resolve_project_root(
    job_id: str,
    job_dir: Path,
    project_root: str | Path | None,
) -> Path:
    if project_root is not None:
        return Path(project_root).resolve()
    destination = job_dir.resolve()
    if (
        destination.name == job_id
        and destination.parent.name == "jobs"
        and destination.parent.parent.name == "data"
    ):
        return destination.parent.parent.parent
    raise WorkerIdentityError(
        "无法从任务目录可靠推导项目根目录，已拒绝绕过项目Worker租约"
    )


def _lease_job_dir(project_root: Path, record: RuntimeLeaseRecord) -> Path:
    if record.job_dir:
        return Path(record.job_dir)
    canonical = project_root / "data" / "jobs" / record.job_id
    if worker_record_path(canonical).is_file():
        return canonical
    return project_root


def _lifecycle_lock_path(
    job_dir: Path,
    project_root: str | Path | None = None,
) -> Path:
    if project_root is None:
        project_root = os.environ.get("AICF_PROJECT_ROOT")
    if project_root is not None:
        root = Path(project_root).resolve()
    else:
        destination = job_dir.resolve()
        root = (
            destination.parent.parent.parent
            if destination.parent.name == "jobs"
            and destination.parent.parent.name == "data"
            else destination
        )
    return root / "_work" / "runtime" / "worker-start.lock"


def _recover_stale_runtime_lease(
    project_root: str | Path,
    *,
    process_probe: Callable[[int], ProcessProbe] = probe_process_identity,
    lifecycle_locked: bool = False,
) -> bool:
    """提交死Worker的FAILED终态；仅确认落盘后释放其租约。"""
    root = Path(project_root).resolve()
    lease = RuntimeLease(root, process_probe=process_probe)
    if not lifecycle_locked:
        with os_file_lock(
            lease.lifecycle_lock_path,
            timeout=5.0,
            timeout_message="项目Worker正在恢复过期运行状态",
        ):
            return _recover_stale_runtime_lease(
                root,
                process_probe=process_probe,
                lifecycle_locked=True,
            )
    current = lease.read_locked()
    if current is None:
        return False
    probe = process_probe(current.pid)
    if probe.status == ProcessProbeStatus.UNKNOWN:
        raise WorkerIdentityError("无法确认现有项目Worker租约是否仍然活跃")
    if (
        probe.status == ProcessProbeStatus.RUNNING
        and process_identity_matches(
            probe.identity,
            pid=current.pid,
            created_at_ns=current.process_created_at_ns,
            executable=current.process_executable,
        )
    ):
        return False

    job_dir = _lease_job_dir(root, current)
    _commit_terminal_record(
        job_dir,
        current.instance_id,
        terminal_status="FAILED",
        error="Worker进程已退出且终态未提交，已由项目租约安全恢复",
        project_root=root,
        lifecycle_locked=True,
    )
    committed = read_worker_record(job_dir)
    if (
        committed is None
        or committed.instance_id != current.instance_id
        or committed.finished_at is None
    ):
        raise WorkerIdentityError("死Worker终态恢复未确认落盘，已保留项目租约")
    if not lease.release_locked(current.instance_id):
        raise WorkerIdentityError("死Worker终态已恢复，但项目租约未能按实例清理")
    return True


class WorkerLauncher:
    def __init__(
        self,
        *,
        python_executable: str,
        process_probe: Callable[[int], ProcessProbe] = probe_process_identity,
        popen: Callable[..., subprocess.Popen[Any]] = silent_popen,
        cleanup_spawn: Callable[[Any], None] | None = None,
        launch_guard: Callable[[], bool] | None = None,
        ready_timeout: float = 2.0,
    ) -> None:
        self.python_executable = python_executable
        self._process_probe = process_probe
        self._popen = popen
        self._cleanup_spawn = cleanup_spawn or self._cleanup_spawn_process
        self._launch_guard = launch_guard
        self._ready_timeout = ready_timeout

    @staticmethod
    def _cleanup_spawn_process(process: Any) -> None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def start(
        self,
        job_id: str,
        job_dir: str | Path,
        *,
        project_root: str | Path | None = None,
        research_strategy: str | None = None,
    ) -> WorkerStartResult:
        destination = Path(job_dir)
        runtime_dir = destination / "_work" / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        resolved_root = _resolve_project_root(
            job_id,
            destination,
            project_root,
        )
        with os_file_lock(
            resolved_root / "_work" / "runtime" / "worker-start.lock",
            timeout=5.0,
            timeout_message="项目Worker正在由另一个启动请求处理",
        ):
            _recover_stale_runtime_lease(
                resolved_root,
                process_probe=self._process_probe,
                lifecycle_locked=True,
            )
            existing = read_worker_record(destination)
            if existing is None and worker_record_path(destination).is_file():
                raise WorkerIdentityError(
                    "Worker运行记录损坏或不可读，已拒绝启动"
                )
            existing_probe = (
                self._process_probe(existing.pid)
                if existing
                else ProcessProbe(status=ProcessProbeStatus.NOT_RUNNING)
            )
            if (
                existing
                and existing.finished_at is None
                and existing_probe.status == ProcessProbeStatus.UNKNOWN
            ):
                raise WorkerIdentityError(
                    "无法确认现有Worker是否仍在运行，已拒绝启动"
                )
            if (
                existing
                and existing.finished_at is None
                and existing_probe.status == ProcessProbeStatus.RUNNING
                and (
                    existing.instance_id is None
                    or existing.process_created_at_ns is None
                    or existing.process_executable is None
                )
            ):
                raise WorkerIdentityError(
                    "检测到仍在运行的旧版Worker记录，身份无法安全确认；"
                    "请等待旧任务结束后再启动"
                )
            if (
                existing
                and existing.finished_at is None
                and existing_probe.status == ProcessProbeStatus.RUNNING
                and process_identity_matches(
                    existing_probe.identity,
                    pid=existing.pid,
                    created_at_ns=existing.process_created_at_ns,
                    executable=existing.process_executable,
                )
            ):
                assert existing.instance_id is not None
                assert existing_probe.identity is not None
                try:
                    RuntimeLease(
                        resolved_root,
                        process_probe=self._process_probe,
                    ).acquire_locked(
                        job_id,
                        existing.instance_id,
                        existing_probe.identity,
                        job_dir=destination,
                    )
                except RuntimeLeaseError as error:
                    raise WorkerIdentityError(str(error)) from error
                return WorkerStartResult(
                    job_id=job_id,
                    pid=existing.pid,
                    reused=True,
                    log_path=existing.log_path,
                )
            if (
                existing
                and existing.finished_at is None
                and existing_probe.status == ProcessProbeStatus.RUNNING
            ):
                raise WorkerIdentityError(
                    "现有Worker进程身份与运行记录不一致，已拒绝启动"
                )
            active_lease = RuntimeLease(
                resolved_root,
                process_probe=self._process_probe,
            ).read_locked()
            if active_lease is not None:
                raise WorkerIdentityError(
                    f"项目已有运行中Worker：{active_lease.job_id}"
                )
            if self._launch_guard is not None and not self._launch_guard():
                raise WorkerIdentityError(
                    "任务状态已变化，为避免重复处理，已拒绝启动Worker"
                )

            log_path = runtime_dir / "worker.log"
            instance_id = uuid.uuid4().hex
            environment = os.environ.copy()
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["PYTHONUTF8"] = "1"
            environment["AICF_WORKER_LAUNCHED"] = "1"
            environment["AICF_WORKER_INSTANCE_ID"] = instance_id
            environment["AICF_PROJECT_ROOT"] = str(resolved_root)
            source_root = str(resolved_root / "src")
            virtualenv_site_packages = str(
                Path(self.python_executable).resolve().parent.parent
                / "Lib"
                / "site-packages"
            )
            existing_python_path = environment.get("PYTHONPATH", "")
            environment["PYTHONPATH"] = os.pathsep.join(
                value
                for value in (
                    source_root,
                    virtualenv_site_packages,
                    existing_python_path,
                )
                if value
            )
            worker_python = str(
                Path(getattr(sys, "_base_executable", self.python_executable))
                .resolve()
            )
            creationflags = 0
            if os.name == "nt":
                creationflags = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    | getattr(subprocess, "DETACHED_PROCESS", 0)
                )
            process: Any | None = None
            try:
                with log_path.open("a", encoding="utf-8", buffering=1) as log:
                    command = [
                        worker_python,
                        "-m",
                        "aicf",
                        "worker-run",
                        "--job",
                        job_id,
                    ]
                    if research_strategy is not None:
                        command.extend([
                            "--research-strategy",
                            research_strategy,
                        ])
                    process = self._popen(
                        command,
                        cwd=str(resolved_root),
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        env=environment,
                        creationflags=creationflags,
                        close_fds=True,
                    )
            except BaseException as error:
                if process is not None:
                    try:
                        self._cleanup_spawn(process)
                    except Exception:
                        pass
                raise WorkerIdentityError(
                    f"Worker启动握手失败：{error}"
                ) from error
            runtime_lease = RuntimeLease(
                resolved_root,
                process_probe=self._process_probe,
            )
            lease_acquired = False
            try:
                spawned_probe = self._process_probe(int(process.pid))
                if (
                    spawned_probe.status != ProcessProbeStatus.RUNNING
                    or spawned_probe.identity is None
                ):
                    raise WorkerIdentityError(
                        "无法读取新Worker进程身份，已拒绝登记"
                    )
                identity = spawned_probe.identity
                runtime_lease.acquire_locked(
                    job_id,
                    instance_id,
                    identity,
                    job_dir=destination,
                )
                lease_acquired = True
                record = WorkerRecord(
                    job_id=job_id,
                    pid=identity.pid,
                    started_at=_now(),
                    log_path=str(log_path.resolve()),
                    instance_id=instance_id,
                    process_created_at_ns=identity.created_at_ns,
                    process_executable=identity.executable,
                    research_strategy=research_strategy,
                )
                write_worker_record(destination, record)
                deadline = time.monotonic() + self._ready_timeout
                while self._ready_timeout > 0 and time.monotonic() < deadline:
                    current = read_worker_record(destination)
                    if (
                        current
                        and current.instance_id == instance_id
                        and current.ready
                    ):
                        record = current
                        break
                    time.sleep(0.02)
                if self._ready_timeout > 0 and not record.ready:
                    raise WorkerIdentityError(
                        "Worker未在限定时间内完成安全握手"
                    )
            except BaseException as error:
                try:
                    atomic_write_text(
                        stop_request_path(destination, instance_id),
                        instance_id + "\n",
                    )
                except Exception:
                    pass
                cleanup_failed = False
                try:
                    self._cleanup_spawn(process)
                except Exception:
                    cleanup_failed = True
                exit_confirmed = False
                if not cleanup_failed:
                    try:
                        exit_confirmed = (
                            self._process_probe(int(process.pid)).status
                            == ProcessProbeStatus.NOT_RUNNING
                        )
                    except Exception:
                        exit_confirmed = False
                lease_retained = lease_acquired and not exit_confirmed
                if lease_acquired and exit_confirmed:
                    try:
                        lease_retained = not runtime_lease.release_locked(
                            instance_id
                        )
                    except RuntimeLeaseError:
                        lease_retained = True
                retention_message = (
                    "；子进程退出未确认，已保留项目Worker租约"
                    if lease_retained
                    else ""
                )
                raise WorkerIdentityError(
                    f"Worker启动握手失败：{error}{retention_message}"
                ) from error
            return WorkerStartResult(
                job_id=job_id,
                pid=record.pid,
                reused=False,
                log_path=record.log_path,
            )


def run_worker(
    job_id: str,
    job_dir: str | Path,
    *,
    run_autopilot: Callable[[str], dict[str, Any]],
    inhibitor_factory: Callable[[], AbstractContextManager[Any]] = SleepInhibitor,
    require_launch_token: bool = True,
) -> int:
    destination = Path(job_dir)
    launched = os.environ.get("AICF_WORKER_LAUNCHED") == "1"
    launch_instance_id = os.environ.get("AICF_WORKER_INSTANCE_ID")
    if require_launch_token and (not launched or not launch_instance_id):
        raise WorkerIdentityError("worker-run只能由WorkerLauncher安全启动")
    instance_id = launch_instance_id or uuid.uuid4().hex
    if launched:
        deadline = time.monotonic() + 2.0
        while True:
            launch_record = read_worker_record(destination)
            if launch_record and launch_record.instance_id == instance_id:
                break
            if time.monotonic() >= deadline:
                raise WorkerIdentityError("等待本次Worker启动记录超时")
            time.sleep(0.02)
    existing = read_worker_record(destination)
    if existing is not None and existing.instance_id not in {None, instance_id}:
        raise WorkerIdentityError("Worker实例身份与启动记录不一致")
    identity = get_process_identity(os.getpid())
    if identity is None:
        raise WorkerIdentityError("无法读取当前Worker进程身份")
    if existing is not None and not process_identity_matches(
        identity,
        pid=existing.pid,
        created_at_ns=existing.process_created_at_ns,
        executable=existing.process_executable,
    ):
        raise WorkerIdentityError("Worker进程身份与Launcher预登记记录不一致")
    record = existing or WorkerRecord(
        job_id=job_id,
        pid=identity.pid,
        started_at=_now(),
        log_path=str(
            (destination / "_work" / "runtime" / "worker.log").resolve()
        ),
    )
    record.pid = identity.pid
    record.instance_id = instance_id
    record.process_created_at_ns = identity.created_at_ns
    record.process_executable = identity.executable
    record.ready = True
    write_worker_record(destination, record)

    project_root = os.environ.get("AICF_PROJECT_ROOT")
    runtime_lease = RuntimeLease(project_root) if project_root else None
    lease_acquired = False
    if runtime_lease is not None:
        try:
            runtime_lease.acquire(
                job_id,
                instance_id,
                identity,
                job_dir=destination,
            )
            lease_acquired = True
        except Exception as error:
            _commit_terminal_record(
                destination,
                instance_id,
                terminal_status="FAILED",
                error=f"Worker租约获取失败：{error}",
            )
            committed = read_worker_record(destination)
            if (
                committed is not None
                and committed.instance_id == instance_id
                and committed.finished_at is not None
            ):
                runtime_lease.release(instance_id)
            raise WorkerIdentityError(
                f"Worker租约获取失败：{error}"
            ) from error

    terminal_committed = False
    stop_after_release = False
    terminal_status = "UNKNOWN"
    try:
        heartbeat = (
            RuntimeLeaseHeartbeat(runtime_lease, instance_id)
            if runtime_lease is not None
            else nullcontext()
        )
        with (
            inhibitor_factory(),
            StopRequestMonitor(destination, instance_id),
        ):
            try:
                with heartbeat:
                    result = run_autopilot(job_id)
            except BaseException as error:
                heartbeat_error = getattr(heartbeat, "error", None)
                failure = heartbeat_error or error
                stop_won = _commit_terminal_record(
                    destination,
                    instance_id,
                    terminal_status="FAILED",
                    error=str(failure),
                )
                terminal_status = "FAILED"
                terminal_committed = True
                if stop_won:
                    stop_after_release = True
                elif heartbeat_error is None:
                    raise
            else:
                heartbeat_error = getattr(heartbeat, "error", None)
                if heartbeat_error is not None:
                    terminal_status = "FAILED"
                    stop_won = _commit_terminal_record(
                        destination,
                        instance_id,
                        terminal_status=terminal_status,
                        error=str(heartbeat_error),
                    )
                else:
                    terminal_status = str(result.get("status", "UNKNOWN"))
                    stop_won = _commit_terminal_record(
                        destination,
                        instance_id,
                        terminal_status=terminal_status,
                    )
                terminal_committed = True
                if stop_won:
                    stop_after_release = True
    except BaseException as error:
        if not terminal_committed:
            stop_after_release = _commit_terminal_record(
                destination,
                instance_id,
                terminal_status="FAILED",
                error=str(error),
            )
        if not stop_after_release:
            raise
    finally:
        if runtime_lease is not None and lease_acquired:
            committed = read_worker_record(destination)
            if (
                committed is not None
                and committed.instance_id == instance_id
                and committed.finished_at is not None
            ):
                runtime_lease.release(instance_id)
    if stop_after_release:
        return 130
    return 0 if terminal_status in {"READY_TO_PUBLISH", "COMPLETED"} else 1


def _commit_terminal_record(
    job_dir: Path,
    instance_id: str,
    *,
    terminal_status: str,
    error: str | None = None,
    project_root: str | Path | None = None,
    lifecycle_locked: bool = False,
) -> bool:
    runtime_dir = job_dir / "_work" / "runtime"
    lock = (
        nullcontext()
        if lifecycle_locked
        else os_file_lock(
            _lifecycle_lock_path(job_dir, project_root),
            timeout=5.0,
            timeout_message="Worker终态写入等待生命周期锁超时",
        )
    )
    with lock:
        current = read_worker_record(job_dir)
        if current is None or current.instance_id != instance_id:
            raise WorkerIdentityError("Worker终态记录实例身份不一致")
        if current.finished_at is not None:
            return current.terminal_status in {"STOPPED", "FORCE_STOPPED"}
        request = stop_request_path(job_dir, instance_id)
        if request.is_file() or current.stop_requested_at is not None:
            current.terminal_status = "STOPPED"
            current.error = "用户停止"
            current.finished_at = _now()
            write_worker_record(job_dir, current)
            request.unlink(missing_ok=True)
            request.with_suffix(".error").unlink(missing_ok=True)
            request.with_suffix(".ack").unlink(missing_ok=True)
            return True
        current.terminal_status = terminal_status
        current.error = error
        current.finished_at = _now()
        write_worker_record(job_dir, current)
        request.unlink(missing_ok=True)
        request.with_suffix(".error").unlink(missing_ok=True)
        request.with_suffix(".ack").unlink(missing_ok=True)
        return False


def stop_worker(
    job_dir: str | Path,
    *,
    process_identity: Callable[[int], ProcessIdentity | None] = get_process_identity,
) -> WorkerRecord:
    destination = Path(job_dir)
    with os_file_lock(
        _lifecycle_lock_path(destination),
        timeout=5.0,
        timeout_message="Worker生命周期正在变更，请稍后重试停止",
    ):
        record = read_worker_record(destination)
        if record is None or record.finished_at is not None:
            raise WorkerIdentityError("没有可停止的运行中Worker")
        identity = process_identity(record.pid)
        if not process_identity_matches(
            identity,
            pid=record.pid,
            created_at_ns=record.process_created_at_ns,
            executable=record.process_executable,
        ):
            raise WorkerIdentityError("Worker进程身份校验失败，已拒绝停止以避免误杀")
        assert record.instance_id is not None
        atomic_write_text(
            stop_request_path(destination, record.instance_id),
            record.instance_id + "\n",
        )
        record.stop_requested_at = _now()
        record.terminal_status = "STOP_REQUESTED"
        write_worker_record(destination, record)
        return record


def force_kill_worker(
    job_dir: str | Path,
    *,
    process_probe: Callable[[int], ProcessProbe] = probe_process_identity,
    terminate_process: Callable[[int], None] = terminate_process_tree,
    confirmation_timeout: float = 5.0,
    poll_interval: float = 0.05,
    sleep: Callable[[float], None] = time.sleep,
) -> WorkerRecord:
    """安全终止匹配身份的Worker，并仅在确认退出后提交停止终态。"""
    destination = Path(job_dir)
    runtime_dir = destination / "_work" / "runtime"

    with os_file_lock(
        _lifecycle_lock_path(destination),
        timeout=5.0,
        timeout_message="Worker生命周期正在变更，请稍后重试",
    ):
        record = read_worker_record(destination)
        if record is None:
            raise WorkerIdentityError("没有找到Worker记录")
        if record.finished_at is not None:
            return record

        probe = process_probe(record.pid)
        if probe.status == ProcessProbeStatus.UNKNOWN:
            record.terminal_status = "STOP_FAILED"
            record.error = "无法确认Worker进程身份"
            write_worker_record(destination, record)
            raise WorkerIdentityError(record.error)
        if (
            probe.status == ProcessProbeStatus.RUNNING
            and not process_identity_matches(
                probe.identity,
                pid=record.pid,
                created_at_ns=record.process_created_at_ns,
                executable=record.process_executable,
            )
        ):
            record.finished_at = _now()
            record.terminal_status = "STALE_IDENTITY"
            record.error = "Worker记录已封存：PID已被其他进程复用"
            write_worker_record(destination, record)
            _cleanup_stop_requests(runtime_dir)
            return record
        if probe.status == ProcessProbeStatus.RUNNING:
            try:
                terminate_process(record.pid)
            except Exception as error:
                record.terminal_status = "STOP_FAILED"
                record.error = str(error)
                write_worker_record(destination, record)
                raise WorkerIdentityError(
                    f"Worker强制终止失败：{error}"
                ) from error
            deadline = time.monotonic() + max(0.0, confirmation_timeout)
            while True:
                confirmed = process_probe(record.pid)
                still_same_process = (
                    confirmed.status == ProcessProbeStatus.RUNNING
                    and process_identity_matches(
                        confirmed.identity,
                        pid=record.pid,
                        created_at_ns=record.process_created_at_ns,
                        executable=record.process_executable,
                    )
                )
                if (
                    confirmed.status == ProcessProbeStatus.NOT_RUNNING
                    or (
                        confirmed.status == ProcessProbeStatus.RUNNING
                        and not still_same_process
                    )
                ):
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    record.terminal_status = "STOP_FAILED"
                    record.error = (
                        "taskkill后Worker仍在运行"
                        if still_same_process
                        else "taskkill后无法确认Worker是否退出"
                    )
                    write_worker_record(destination, record)
                    raise WorkerIdentityError(record.error)
                sleep(min(max(0.0, poll_interval), remaining))

        record.finished_at = _now()
        record.terminal_status = "FORCE_STOPPED"
        record.error = "用户强制停止"
        write_worker_record(destination, record)
        _cleanup_stop_requests(runtime_dir)
        return record


def _cleanup_stop_requests(runtime_dir: Path) -> None:
    if not runtime_dir.is_dir():
        return
    for pattern in ("stop-*.request", "stop-*.ack", "stop-*.error"):
        for path in runtime_dir.glob(pattern):
            try:
                path.unlink()
            except OSError:
                continue


def worker_status(job_dir: str | Path) -> dict[str, Any]:
    record = read_worker_record(job_dir)
    if record is None:
        return {"status": "NOT_STARTED"}
    running = record.finished_at is None and process_identity_matches(
        get_process_identity(record.pid),
        pid=record.pid,
        created_at_ns=record.process_created_at_ns,
        executable=record.process_executable,
    )
    return {
        "status": "RUNNING" if running else (record.terminal_status or "STOPPED"),
        **record.model_dump(),
    }
