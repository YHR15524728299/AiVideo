from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .atomic_io import atomic_write_text
from .file_lock import os_file_lock
from .process_identity import (
    ProcessIdentity,
    ProcessProbe,
    ProcessProbeStatus,
    get_process_identity,
    probe_process_identity,
    process_is_running,
)
from .subprocess_utils import silent_popen
from .worker_stop_ipc import (
    StopRequestMonitor,
    WorkerIdentityError,
    stop_request_path,
    terminate_current_process_tree,
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


def _identity_matches(record: WorkerRecord, identity: ProcessIdentity | None) -> bool:
    if (
        identity is None
        or record.instance_id is None
        or record.process_created_at_ns is None
        or record.process_executable is None
    ):
        return False
    return (
        identity.pid == record.pid
        and identity.created_at_ns == record.process_created_at_ns
        and os.path.normcase(identity.executable)
        == os.path.normcase(record.process_executable)
    )


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
    ) -> WorkerStartResult:
        destination = Path(job_dir)
        runtime_dir = destination / "_work" / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        with os_file_lock(
            runtime_dir / "worker-start.lock",
            timeout=5.0,
            timeout_message=f"任务 {job_id} 正在由另一个启动请求处理",
        ):
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
                and _identity_matches(
                    existing,
                    existing_probe.identity,
                )
            ):
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
            if project_root is not None:
                resolved_root = Path(project_root).resolve()
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
            with log_path.open("a", encoding="utf-8", buffering=1) as log:
                process = self._popen(
                    [
                        worker_python,
                        "-m",
                        "aicf",
                        "worker-run",
                        "--job",
                        job_id,
                    ],
                    cwd=str(Path(project_root).resolve()) if project_root else None,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=environment,
                    creationflags=creationflags,
                    close_fds=True,
                )
            spawned_probe = self._process_probe(int(process.pid))
            if (
                spawned_probe.status != ProcessProbeStatus.RUNNING
                or spawned_probe.identity is None
            ):
                self._cleanup_spawn(process)
                raise WorkerIdentityError("无法读取新Worker进程身份，已拒绝登记")
            identity = spawned_probe.identity
            record = WorkerRecord(
                job_id=job_id,
                pid=identity.pid,
                started_at=_now(),
                log_path=str(log_path.resolve()),
                instance_id=instance_id,
                process_created_at_ns=identity.created_at_ns,
                process_executable=identity.executable,
            )
            write_worker_record(destination, record)
            deadline = time.monotonic() + self._ready_timeout
            while self._ready_timeout > 0 and time.monotonic() < deadline:
                current = read_worker_record(destination)
                if current and current.instance_id == instance_id and current.ready:
                    record = current
                    break
                time.sleep(0.02)
            if self._ready_timeout > 0 and not record.ready:
                atomic_write_text(
                    stop_request_path(destination, instance_id),
                    instance_id + "\n",
                )
                self._cleanup_spawn(process)
                raise WorkerIdentityError("Worker未在限定时间内完成安全握手")
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
    if existing is not None and not _identity_matches(existing, identity):
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
    terminal_committed = False
    try:
        with inhibitor_factory(), StopRequestMonitor(destination, instance_id):
            try:
                result = run_autopilot(job_id)
            except BaseException as error:
                stop_won = _commit_terminal_record(
                    destination,
                    instance_id,
                    terminal_status="FAILED",
                    error=str(error),
                )
                terminal_committed = True
                if stop_won:
                    terminate_current_process_tree()
                    raise WorkerIdentityError("Worker停止处理返回异常")
                raise
            terminal_status = str(result.get("status", "UNKNOWN"))
            stop_won = _commit_terminal_record(
                destination,
                instance_id,
                terminal_status=terminal_status,
            )
            terminal_committed = True
            if stop_won:
                terminate_current_process_tree()
                raise WorkerIdentityError("Worker停止处理返回异常")
        return 0 if terminal_status in {"READY_TO_PUBLISH", "COMPLETED"} else 1
    except BaseException as error:
        if not terminal_committed:
            _commit_terminal_record(
                destination,
                instance_id,
                terminal_status="FAILED",
                error=str(error),
            )
        raise


def _commit_terminal_record(
    job_dir: Path,
    instance_id: str,
    *,
    terminal_status: str,
    error: str | None = None,
) -> bool:
    runtime_dir = job_dir / "_work" / "runtime"
    with os_file_lock(
        runtime_dir / "worker-start.lock",
        timeout=5.0,
        timeout_message="Worker终态写入等待生命周期锁超时",
    ):
        current = read_worker_record(job_dir)
        if current is None or current.instance_id != instance_id:
            raise WorkerIdentityError("Worker终态记录实例身份不一致")
        request = stop_request_path(job_dir, instance_id)
        if request.is_file() or current.stop_requested_at is not None:
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
    runtime_dir = destination / "_work" / "runtime"
    with os_file_lock(
        runtime_dir / "worker-start.lock",
        timeout=5.0,
        timeout_message="Worker生命周期正在变更，请稍后重试停止",
    ):
        record = read_worker_record(destination)
        if record is None or record.finished_at is not None:
            raise WorkerIdentityError("没有可停止的运行中Worker")
        identity = process_identity(record.pid)
        if not _identity_matches(record, identity):
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


def force_kill_worker(job_dir: str | Path) -> WorkerRecord:
    """强制清理僵尸worker记录，用于进程异常退出、身份校验失败的情况。
    
    当正常停止失败（Worker进程身份校验失败）时使用此函数：
    1. 如果PID进程仍在运行，先尝试taskkill强制终止
    2. 更新worker.json标记为已强制停止
    3. 清理锁文件和stop请求文件
    """
    from .subprocess_utils import silent_run
    
    destination = Path(job_dir)
    runtime_dir = destination / "_work" / "runtime"
    
    with os_file_lock(
        runtime_dir / "worker-start.lock",
        timeout=5.0,
        timeout_message="Worker生命周期正在变更，请稍后重试",
    ):
        record = read_worker_record(destination)
        if record is None:
            raise WorkerIdentityError("没有找到Worker记录")
        
        # 如果进程还在运行，尝试强制终止
        if record.pid and process_is_running(record.pid):
            try:
                silent_run(
                    ["taskkill", "/PID", str(record.pid), "/T", "/F"],
                    capture_output=True,
                    timeout=10,
                )
            except Exception:
                pass  # 忽略终止错误，继续清理记录
        
        # 清理stop请求文件
        if runtime_dir.is_dir():
            for f in runtime_dir.glob("stop-*.request"):
                try:
                    f.unlink()
                except OSError:
                    pass
            for f in runtime_dir.glob("stop-*.ack"):
                try:
                    f.unlink()
                except OSError:
                    pass
            for f in runtime_dir.glob("stop-*.error"):
                try:
                    f.unlink()
                except OSError:
                    pass
        
        # 更新记录
        record.finished_at = _now()
        record.terminal_status = "FORCE_STOPPED"
        record.error = "用户强制停止"
        write_worker_record(destination, record)
        return record


def worker_status(job_dir: str | Path) -> dict[str, Any]:
    record = read_worker_record(job_dir)
    if record is None:
        return {"status": "NOT_STARTED"}
    running = record.finished_at is None and _identity_matches(
        record,
        get_process_identity(record.pid),
    )
    return {
        "status": "RUNNING" if running else (record.terminal_status or "STOPPED"),
        **record.model_dump(),
    }
