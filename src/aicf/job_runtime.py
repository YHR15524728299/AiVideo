from __future__ import annotations

import _thread
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from .atomic_io import atomic_write_text
from .file_lock import os_file_lock
from .process_identity import (
    ProcessIdentity,
    ProcessProbe,
    ProcessProbeStatus,
    probe_process_identity,
    process_identity_matches,
)


class RuntimeLeaseError(RuntimeError):
    pass


class RuntimeLeaseRecord(BaseModel):
    job_id: str
    instance_id: str
    pid: int
    process_created_at_ns: int
    process_executable: str
    acquired_at: str
    heartbeat_at: str
    job_dir: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RuntimeLease:
    def __init__(
        self,
        project_root: str | Path,
        *,
        process_probe: Callable[[int], ProcessProbe] = probe_process_identity,
    ) -> None:
        runtime_dir = Path(project_root) / "_work" / "runtime"
        self.path = runtime_dir / "worker-lease.json"
        self.lifecycle_lock_path = runtime_dir / "worker-start.lock"
        self._lock_path = self.lifecycle_lock_path
        self._process_probe = process_probe

    def read(self) -> RuntimeLeaseRecord | None:
        try:
            with os_file_lock(
                self.lifecycle_lock_path,
                timeout=5.0,
                timeout_message="项目Worker租约读取等待超时",
            ):
                return self.read_locked()
        except RuntimeLeaseError:
            raise
        except Exception as error:
            raise RuntimeLeaseError("项目Worker租约读取失败") from error

    def read_locked(self) -> RuntimeLeaseRecord | None:
        """Read while the caller holds the project lifecycle lock."""
        return self._read_unlocked()

    def _read_unlocked(self) -> RuntimeLeaseRecord | None:
        if not self.path.is_file():
            return None
        try:
            return RuntimeLeaseRecord.model_validate_json(
                self.path.read_text(encoding="utf-8-sig")
            )
        except (OSError, ValueError) as error:
            raise RuntimeLeaseError("项目Worker租约损坏或不可读") from error

    def acquire(
        self,
        job_id: str,
        instance_id: str,
        identity: ProcessIdentity,
        *,
        job_dir: str | Path | None = None,
    ) -> RuntimeLeaseRecord:
        try:
            with os_file_lock(
                self.lifecycle_lock_path,
                timeout=5.0,
                timeout_message="项目Worker租约正在变更，请稍后重试",
            ):
                return self.acquire_locked(
                    job_id,
                    instance_id,
                    identity,
                    job_dir=job_dir,
                )
        except RuntimeLeaseError:
            raise
        except Exception as error:
            raise RuntimeLeaseError("项目Worker租约获取失败") from error

    def acquire_locked(
        self,
        job_id: str,
        instance_id: str,
        identity: ProcessIdentity,
        *,
        job_dir: str | Path | None = None,
    ) -> RuntimeLeaseRecord:
        """Acquire while the caller holds the project lifecycle lock."""
        current = self._read_unlocked()
        if current is not None:
            if (
                current.instance_id == instance_id
                and process_identity_matches(
                    identity,
                    pid=current.pid,
                    created_at_ns=current.process_created_at_ns,
                    executable=current.process_executable,
                )
            ):
                current.heartbeat_at = _now()
                self._write(current)
                return current
            probe = self._process_probe(current.pid)
            if probe.status == ProcessProbeStatus.UNKNOWN:
                raise RuntimeLeaseError(
                    "无法确认现有项目Worker租约是否仍然活跃"
                )
            if (
                probe.status == ProcessProbeStatus.RUNNING
                and process_identity_matches(
                    probe.identity,
                    pid=current.pid,
                    created_at_ns=current.process_created_at_ns,
                    executable=current.process_executable,
                )
            ):
                raise RuntimeLeaseError(
                    f"项目已有运行中Worker：{current.job_id}"
                )
            raise RuntimeLeaseError(
                "检测到死Worker租约，必须先按实例恢复其终态"
            )
        now = _now()
        record = RuntimeLeaseRecord(
            job_id=job_id,
            instance_id=instance_id,
            pid=identity.pid,
            process_created_at_ns=identity.created_at_ns,
            process_executable=identity.executable,
            acquired_at=now,
            heartbeat_at=now,
            job_dir=(
                str(Path(job_dir).resolve())
                if job_dir is not None
                else None
            ),
        )
        self._write(record)
        return record

    def heartbeat(self, instance_id: str) -> bool:
        try:
            with os_file_lock(
                self.lifecycle_lock_path,
                timeout=5.0,
                timeout_message="项目Worker租约心跳等待超时",
            ):
                return self.heartbeat_locked(instance_id)
        except RuntimeLeaseError:
            raise
        except Exception as error:
            raise RuntimeLeaseError("项目Worker租约心跳失败") from error

    def heartbeat_locked(self, instance_id: str) -> bool:
        """Refresh while the caller holds the project lifecycle lock."""
        current = self._read_unlocked()
        if current is None or current.instance_id != instance_id:
            return False
        current.heartbeat_at = _now()
        self._write(current)
        return True

    def release(self, instance_id: str) -> bool:
        try:
            with os_file_lock(
                self.lifecycle_lock_path,
                timeout=5.0,
                timeout_message="项目Worker租约释放等待超时",
            ):
                return self.release_locked(instance_id)
        except RuntimeLeaseError:
            raise
        except Exception as error:
            raise RuntimeLeaseError("项目Worker租约释放失败") from error

    def release_locked(self, instance_id: str) -> bool:
        """Release while the caller holds the project lifecycle lock."""
        try:
            current = self._read_unlocked()
            if current is None or current.instance_id != instance_id:
                return False
            self.path.unlink(missing_ok=True)
            return True
        except RuntimeLeaseError:
            raise
        except OSError as error:
            raise RuntimeLeaseError("项目Worker租约释放失败") from error

    def _write(self, record: RuntimeLeaseRecord) -> None:
        atomic_write_text(self.path, record.model_dump_json(indent=2) + "\n")


class RuntimeLeaseHeartbeat(
    AbstractContextManager["RuntimeLeaseHeartbeat"]
):
    def __init__(
        self,
        lease: RuntimeLease,
        instance_id: str,
        *,
        interval: float = 5.0,
        request_cancel: Callable[[], None] = _thread.interrupt_main,
    ) -> None:
        if interval <= 0:
            raise ValueError("租约心跳间隔必须大于零")
        self._lease = lease
        self._instance_id = instance_id
        self._interval = interval
        self._request_cancel = request_cancel
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._error: RuntimeLeaseError | None = None

    @property
    def error(self) -> RuntimeLeaseError | None:
        return self._error

    def __enter__(self) -> "RuntimeLeaseHeartbeat":
        def refresh() -> None:
            while not self._stop.wait(self._interval):
                try:
                    refreshed = self._lease.heartbeat(self._instance_id)
                    if not refreshed:
                        raise RuntimeLeaseError("项目Worker租约心跳已丢失")
                except Exception as error:
                    self._error = (
                        error
                        if isinstance(error, RuntimeLeaseError)
                        else RuntimeLeaseError("项目Worker租约心跳失败")
                    )
                    try:
                        self._request_cancel()
                    except BaseException:
                        pass
                    return

        self._thread = threading.Thread(
            target=refresh,
            name="aicf-runtime-lease-heartbeat",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 1.0)
