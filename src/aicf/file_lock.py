from __future__ import annotations

import errno
import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Callable, Iterator


_LOCK_BUSY_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
_LOCK_PROTOCOL = "aicf-lock-v1"
_METADATA_SIZE = 512
_LOCK_BYTE_OFFSET = 0
_METADATA_OFFSET = 1


def _write_metadata(handle: BinaryIO, metadata: dict[str, object]) -> None:
    payload = json.dumps(
        metadata,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > _METADATA_SIZE:
        raise ValueError("文件锁元数据过大")
    handle.seek(_METADATA_OFFSET)
    handle.write(payload.ljust(_METADATA_SIZE, b" "))
    handle.flush()
    os.fsync(handle.fileno())


def read_lock_metadata(path: Path) -> dict[str, object] | None:
    for _attempt in range(3):
        try:
            with path.open("rb") as handle:
                handle.seek(_METADATA_OFFSET)
                raw = handle.read(_METADATA_SIZE).rstrip(b" \0")
            value = json.loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            time.sleep(0.005)
    return None


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def lock_is_active(
    path: Path,
    *,
    stale_after: float = 120.0,
    now: Callable[[], float] = time.time,
    process_exists: Callable[[int], bool] = _process_exists,
) -> bool:
    metadata = read_lock_metadata(path)
    if (
        metadata is None
        or metadata.get("protocol") != _LOCK_PROTOCOL
        or metadata.get("active") is not True
    ):
        return False
    try:
        pid = int(metadata["pid"])
        heartbeat_at = float(metadata["heartbeat_at"])
    except (KeyError, TypeError, ValueError):
        return False
    return now() - heartbeat_at <= stale_after and process_exists(pid)


def _try_lock(handle: BinaryIO) -> bool:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(_LOCK_BYTE_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            handle.seek(0)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in _LOCK_BUSY_ERRNOS:
            return False
        raise
    return True


def _unlock(handle: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(_LOCK_BYTE_OFFSET)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        handle.seek(0)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def os_file_lock(
    path: Path,
    *,
    timeout: float,
    timeout_message: str,
    poll_interval: float = 0.01,
    heartbeat_interval: float = 5.0,
) -> Iterator[None]:
    if timeout < 0:
        raise ValueError("文件锁 timeout 不能为负数")
    if heartbeat_interval <= 0:
        raise ValueError("文件锁 heartbeat_interval 必须大于零")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(exist_ok=True)
    deadline = time.monotonic() + timeout
    with path.open("r+b") as handle:
        handle.seek(0, os.SEEK_END)
        required_size = _METADATA_OFFSET + _METADATA_SIZE
        if handle.tell() < required_size:
            handle.write(b" " * (required_size - handle.tell()))
            handle.flush()
            os.fsync(handle.fileno())
        while not _try_lock(handle):
            if time.monotonic() >= deadline:
                raise TimeoutError(timeout_message)
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))
        acquired_at = time.time()
        metadata: dict[str, object] = {
            "protocol": _LOCK_PROTOCOL,
            "pid": os.getpid(),
            "acquired_at": acquired_at,
            "heartbeat_at": acquired_at,
            "active": True,
        }
        _write_metadata(handle, metadata)
        stop_heartbeat = threading.Event()

        def heartbeat() -> None:
            while not stop_heartbeat.wait(heartbeat_interval):
                metadata["heartbeat_at"] = time.time()
                _write_metadata(handle, metadata)

        heartbeat_thread = threading.Thread(
            target=heartbeat,
            name=f"aicf-lock-heartbeat-{path.name}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            yield
        finally:
            stop_heartbeat.set()
            heartbeat_thread.join(timeout=heartbeat_interval + 1.0)
            metadata["heartbeat_at"] = time.time()
            metadata["active"] = False
            metadata["released_at"] = metadata["heartbeat_at"]
            _write_metadata(handle, metadata)
            _unlock(handle)
