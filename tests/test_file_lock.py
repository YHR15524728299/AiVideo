from __future__ import annotations

import json
import os
import time
from pathlib import Path

from aicf.file_lock import lock_is_active, os_file_lock, read_lock_metadata


def test_lock_metadata_uses_shared_pid_time_and_heartbeat_protocol(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / ".autopilot.lock"

    with os_file_lock(
        lock_path,
        timeout=0,
        timeout_message="busy",
        heartbeat_interval=0.01,
    ):
        first = read_lock_metadata(lock_path)
        time.sleep(0.03)
        second = read_lock_metadata(lock_path)

        assert first is not None
        assert second is not None
        assert first["protocol"] == "aicf-lock-v1"
        assert first["pid"] == os.getpid()
        assert first["acquired_at"] <= first["heartbeat_at"]
        assert second["heartbeat_at"] >= first["heartbeat_at"]
        assert lock_is_active(lock_path, stale_after=1.0) is True

    released = read_lock_metadata(lock_path)
    assert released is not None
    assert released["active"] is False
    assert lock_is_active(lock_path, stale_after=1.0) is False


def test_stale_lock_is_not_deleted_when_inspected(tmp_path: Path) -> None:
    lock_path = tmp_path / ".autopilot.lock"
    lock_path.write_text(
        json.dumps(
            {
                "protocol": "aicf-lock-v1",
                "pid": 999999,
                "acquired_at": 1.0,
                "heartbeat_at": 1.0,
                "active": True,
            }
        ),
        encoding="utf-8",
    )

    assert lock_is_active(
        lock_path,
        stale_after=1.0,
        now=lambda: 10.0,
        process_exists=lambda _pid: False,
    ) is False
    assert lock_path.exists()
