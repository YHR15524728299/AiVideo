from __future__ import annotations

import os
import re
import subprocess
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path

from .atomic_io import atomic_write_text
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


def terminate_current_process_tree() -> None:
    if os.name == "nt":
        silent_run(
            ["taskkill", "/PID", str(os.getpid()), "/T", "/F"],
            capture_output=True,
            timeout=10,
        )
        os._exit(130)
    os.kill(os.getpid(), 15)


class StopRequestMonitor(AbstractContextManager["StopRequestMonitor"]):
    def __init__(
        self,
        job_dir: str | Path,
        instance_id: str,
        *,
        terminate_self: Callable[[], None] = terminate_current_process_tree,
        poll_interval: float = 0.2,
    ) -> None:
        self._request_path = stop_request_path(job_dir, instance_id)
        self._terminate_self = terminate_self
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "StopRequestMonitor":
        def watch() -> None:
            while not self._stop.is_set():
                if self._request_path.is_file():
                    try:
                        self._terminate_self()
                    except BaseException as error:
                        atomic_write_text(
                            self._request_path.with_suffix(".error"),
                            f"{type(error).__name__}: {error}\n",
                        )
                    else:
                        atomic_write_text(
                            self._request_path.with_suffix(".ack"),
                            "stop signal handled\n",
                        )
                        return
                self._stop.wait(self._poll_interval)

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
