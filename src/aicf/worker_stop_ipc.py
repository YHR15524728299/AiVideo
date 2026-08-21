from __future__ import annotations

import _thread
import os
import re
import subprocess
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from .subprocess_utils import silent_run


class WorkerIdentityError(RuntimeError):
    pass


def stop_request_path(job_dir: str | Path, instance_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", instance_id):
        raise WorkerIdentityError("Worker实例令牌格式无效")
    return (
        Path(job_dir)
        / "_work"
        / "runtime"
        / f"stop-{instance_id}.request"
    )


def terminate_process_tree(
    pid: int,
    *,
    run: Callable[..., subprocess.CompletedProcess[Any]] = silent_run,
    platform_name: str | None = None,
) -> None:
    platform = platform_name or os.name
    if platform == "nt":
        result = run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            suffix = f"：{detail}" if detail else ""
            raise WorkerIdentityError(
                f"taskkill返回非零状态 {result.returncode}{suffix}"
            )
        return
    os.kill(pid, 15)


def terminate_current_process_tree() -> None:
    """Backward-compatible alias for terminating the current process tree."""
    terminate_process_tree(os.getpid())


class StopRequestMonitor(AbstractContextManager["StopRequestMonitor"]):
    def __init__(
        self,
        job_dir: str | Path,
        instance_id: str,
        *,
        request_cancel: Callable[[], None] | None = None,
        terminate_self: Callable[[], None] | None = None,
        poll_interval: float = 0.2,
        wait_for_next_poll: Callable[[threading.Event, float], bool] | None = None,
    ) -> None:
        if request_cancel is not None and terminate_self is not None:
            raise TypeError("request_cancel与terminate_self不能同时指定")
        self._request_path = stop_request_path(job_dir, instance_id)
        self._request_cancel = (
            request_cancel or terminate_self or _thread.interrupt_main
        )
        self._poll_interval = poll_interval
        self._wait_for_next_poll = (
            wait_for_next_poll
            or (lambda stopped, interval: stopped.wait(interval))
        )
        self._stop = threading.Event()
        self._requested = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def stop_requested(self) -> bool:
        return self._requested.is_set()

    def __enter__(self) -> "StopRequestMonitor":
        def watch() -> None:
            while not self._stop.is_set():
                if self._request_path.is_file():
                    self._requested.set()
                    self._request_cancel()
                    return
                if self._wait_for_next_poll(self._stop, self._poll_interval):
                    return

        self._thread = threading.Thread(
            target=watch,
            name="aicf-worker-stop-monitor",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval + 1.0)
