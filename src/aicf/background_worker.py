from __future__ import annotations

import ctypes
import json
import os
import subprocess
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .atomic_io import atomic_write_text


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkerRecord(BaseModel):
    job_id: str
    pid: int
    started_at: str
    log_path: str
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


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        kernel32.GetExitCodeProcess.restype = ctypes.c_int
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except (OSError, SystemError):
        return False
    return True


class WorkerLauncher:
    def __init__(
        self,
        *,
        python_executable: str,
        process_is_running: Callable[[int], bool] = process_is_running,
        popen: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
    ) -> None:
        self.python_executable = python_executable
        self._process_is_running = process_is_running
        self._popen = popen

    def start(
        self,
        job_id: str,
        job_dir: str | Path,
        *,
        project_root: str | Path | None = None,
    ) -> WorkerStartResult:
        destination = Path(job_dir)
        existing = read_worker_record(destination)
        if existing and existing.finished_at is None and self._process_is_running(
            existing.pid
        ):
            return WorkerStartResult(
                job_id=job_id,
                pid=existing.pid,
                reused=True,
                log_path=existing.log_path,
            )

        runtime_dir = destination / "_work" / "runtime"
        runtime_dir.mkdir(parents=True, exist_ok=True)
        log_path = runtime_dir / "worker.log"
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "utf-8"
        environment["PYTHONUTF8"] = "1"
        environment["AICF_WORKER_LAUNCHED"] = "1"
        if project_root is not None:
            environment["AICF_PROJECT_ROOT"] = str(Path(project_root).resolve())
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
                    self.python_executable,
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
        record = WorkerRecord(
            job_id=job_id,
            pid=int(process.pid),
            started_at=_now(),
            log_path=str(log_path.resolve()),
        )
        write_worker_record(destination, record)
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
) -> int:
    destination = Path(job_dir)
    if os.environ.get("AICF_WORKER_LAUNCHED") == "1":
        deadline = time.monotonic() + 2.0
        while not worker_record_path(destination).is_file():
            if time.monotonic() >= deadline:
                break
            time.sleep(0.02)
    record = read_worker_record(destination) or WorkerRecord(
        job_id=job_id,
        pid=os.getpid(),
        started_at=_now(),
        log_path=str(
            (destination / "_work" / "runtime" / "worker.log").resolve()
        ),
    )
    record.pid = os.getpid()
    write_worker_record(destination, record)
    try:
        with inhibitor_factory():
            result = run_autopilot(job_id)
        terminal_status = str(result.get("status", "UNKNOWN"))
        record.terminal_status = terminal_status
        record.finished_at = _now()
        write_worker_record(destination, record)
        return 0 if terminal_status in {"READY_TO_PUBLISH", "COMPLETED"} else 1
    except BaseException as error:
        record.terminal_status = "FAILED"
        record.error = str(error)
        record.finished_at = _now()
        write_worker_record(destination, record)
        raise


def worker_status(job_dir: str | Path) -> dict[str, Any]:
    record = read_worker_record(job_dir)
    if record is None:
        return {"status": "NOT_STARTED"}
    running = record.finished_at is None and process_is_running(record.pid)
    return {
        "status": "RUNNING" if running else (record.terminal_status or "STOPPED"),
        **record.model_dump(),
    }
