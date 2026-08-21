from __future__ import annotations

import threading
import time

import pytest

from aicf.job_runtime import (
    RuntimeLease,
    RuntimeLeaseError,
    RuntimeLeaseHeartbeat,
)
from aicf.process_identity import (
    ProcessIdentity,
    ProcessProbe,
    ProcessProbeStatus,
)


def _running(identity: ProcessIdentity) -> ProcessProbe:
    return ProcessProbe(status=ProcessProbeStatus.RUNNING, identity=identity)


def test_runtime_lease_allows_only_one_job(tmp_path) -> None:
    first = ProcessIdentity(
        pid=101,
        created_at_ns=1001,
        executable="python.exe",
    )
    second = ProcessIdentity(
        pid=202,
        created_at_ns=2002,
        executable="python.exe",
    )
    identities = {first.pid: first, second.pid: second}
    barrier = threading.Barrier(2)
    acquired: list[str] = []
    rejected: list[str] = []

    def claim(job_id: str, instance_id: str, identity: ProcessIdentity) -> None:
        lease = RuntimeLease(
            tmp_path,
            process_probe=lambda pid: _running(identities[pid]),
        )
        barrier.wait()
        try:
            lease.acquire(job_id, instance_id, identity)
        except RuntimeLeaseError:
            rejected.append(job_id)
        else:
            acquired.append(job_id)

    threads = [
        threading.Thread(target=claim, args=("JOB1", "instance-a", first)),
        threading.Thread(target=claim, args=("JOB2", "instance-b", second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert len(acquired) == 1
    assert len(rejected) == 1


def test_runtime_lease_release_is_conditioned_on_instance_id(tmp_path) -> None:
    identity = ProcessIdentity(
        pid=101,
        created_at_ns=1001,
        executable="python.exe",
    )
    lease = RuntimeLease(
        tmp_path,
        process_probe=lambda _pid: _running(identity),
    )
    lease.acquire("JOB1", "instance-a", identity)

    assert lease.release("instance-b") is False
    assert lease.read() is not None
    assert lease.release("instance-a") is True
    assert lease.read() is None
    assert lease.lifecycle_lock_path == (
        tmp_path / "_work" / "runtime" / "worker-start.lock"
    )
    assert not (tmp_path / "_work" / "runtime" / "worker-lease.lock").exists()


def test_runtime_lease_refuses_to_overwrite_dead_instance_before_recovery(
    tmp_path,
) -> None:
    first = ProcessIdentity(
        pid=101,
        created_at_ns=1001,
        executable="python.exe",
    )
    second = ProcessIdentity(
        pid=202,
        created_at_ns=2002,
        executable="python.exe",
    )
    lease = RuntimeLease(
        tmp_path,
        process_probe=lambda _pid: ProcessProbe(
            status=ProcessProbeStatus.NOT_RUNNING
        ),
    )
    lease.acquire("JOB1", "instance-a", first)

    with pytest.raises(RuntimeLeaseError, match="恢复其终态"):
        lease.acquire("JOB2", "instance-b", second)

    retained = lease.read()
    assert retained is not None
    assert retained.job_id == "JOB1"
    assert retained.instance_id == "instance-a"


def test_runtime_lease_heartbeat_refreshes_active_instance(tmp_path) -> None:
    identity = ProcessIdentity(
        pid=101,
        created_at_ns=1001,
        executable="python.exe",
    )
    lease = RuntimeLease(
        tmp_path,
        process_probe=lambda _pid: _running(identity),
    )
    original = lease.acquire("JOB1", "instance-a", identity).heartbeat_at

    with RuntimeLeaseHeartbeat(
        lease,
        "instance-a",
        interval=0.01,
    ):
        deadline = time.monotonic() + 1
        current = lease.read()
        while (
            current is not None
            and current.heartbeat_at == original
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            current = lease.read()

    assert current is not None
    assert current.heartbeat_at != original


def test_runtime_lease_serializes_read_with_release(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = ProcessIdentity(
        pid=101,
        created_at_ns=1001,
        executable="python.exe",
    )
    lease = RuntimeLease(
        tmp_path,
        process_probe=lambda _pid: _running(identity),
    )
    lease.acquire("JOB1", "instance-a", identity)
    reader_started = threading.Event()
    release_attempted = threading.Event()
    released = threading.Event()
    original_read_text = type(lease.path).read_text
    snapshots = []
    errors: list[BaseException] = []

    def delayed_read_text(path, *args, **kwargs):
        if path == lease.path and threading.current_thread().name == "lease-reader":
            reader_started.set()
            if not release_attempted.wait(timeout=1):
                raise RuntimeError("release thread did not attempt release")
            released.wait(timeout=0.1)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(lease.path), "read_text", delayed_read_text)

    def read_lease() -> None:
        try:
            snapshots.append(lease.read())
        except BaseException as error:
            errors.append(error)

    def release_lease() -> None:
        try:
            if not reader_started.wait(timeout=1):
                raise RuntimeError("reader thread did not start")
            release_attempted.set()
            lease.release("instance-a")
            released.set()
        except BaseException as error:
            errors.append(error)

    reader = threading.Thread(target=read_lease, name="lease-reader")
    releaser = threading.Thread(target=release_lease, name="lease-releaser")
    reader.start()
    releaser.start()
    reader.join(timeout=2)
    releaser.join(timeout=2)

    assert not reader.is_alive()
    assert not releaser.is_alive()
    assert errors == []
    assert len(snapshots) == 1
    assert snapshots[0] is not None
    assert snapshots[0].instance_id == "instance-a"
    assert lease.read() is None


def test_runtime_lease_repeated_concurrent_reads_and_heartbeats(tmp_path) -> None:
    identity = ProcessIdentity(
        pid=101,
        created_at_ns=1001,
        executable="python.exe",
    )
    lease = RuntimeLease(
        tmp_path,
        process_probe=lambda _pid: _running(identity),
    )
    lease.acquire("JOB1", "instance-a", identity)
    start = threading.Barrier(5)
    errors: list[BaseException] = []

    def read_repeatedly() -> None:
        start.wait()
        try:
            for _ in range(25):
                record = lease.read()
                if record is None or record.instance_id != "instance-a":
                    raise RuntimeError("lease record changed during read")
        except BaseException as error:
            errors.append(error)

    def write_repeatedly() -> None:
        start.wait()
        try:
            for _ in range(25):
                if not lease.heartbeat("instance-a"):
                    raise RuntimeError("lease heartbeat was unexpectedly lost")
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=read_repeatedly)
        for _ in range(4)
    ] + [threading.Thread(target=write_repeatedly)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []


@pytest.mark.parametrize("operation", ["acquire", "heartbeat", "release"])
def test_runtime_lease_wraps_storage_failures_in_one_error_contract(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    identity = ProcessIdentity(
        pid=101,
        created_at_ns=1001,
        executable="python.exe",
    )
    lease = RuntimeLease(
        tmp_path,
        process_probe=lambda _pid: _running(identity),
    )
    lease.acquire("JOB1", "instance-a", identity)

    if operation == "acquire":
        monkeypatch.setattr(
            lease,
            "_process_probe",
            lambda _pid: (_ for _ in ()).throw(OSError("probe failed")),
        )
        call = lambda: lease.acquire("JOB2", "instance-b", identity)
    elif operation == "heartbeat":
        monkeypatch.setattr(
            lease,
            "_write",
            lambda _record: (_ for _ in ()).throw(OSError("write failed")),
        )
        call = lambda: lease.heartbeat("instance-a")
    else:
        monkeypatch.setattr(
            type(lease.path),
            "unlink",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("unlink failed")
            ),
        )
        call = lambda: lease.release("instance-a")

    with pytest.raises(RuntimeLeaseError) as exc_info:
        call()

    assert isinstance(exc_info.value.__cause__, OSError)


@pytest.mark.parametrize("failure", ["lost", "exception"])
def test_runtime_lease_heartbeat_notifies_cancellation_on_failure(
    failure: str,
) -> None:
    attempted = threading.Event()
    cancelled = threading.Event()

    class FailingLease:
        def heartbeat(self, _instance_id: str) -> bool:
            attempted.set()
            if failure == "exception":
                raise RuntimeLeaseError("heartbeat failed")
            return False

    heartbeat = RuntimeLeaseHeartbeat(
        FailingLease(),  # type: ignore[arg-type]
        "instance-a",
        interval=0.001,
        request_cancel=cancelled.set,
    )
    with heartbeat:
        assert cancelled.wait(timeout=1)

    assert attempted.is_set()
    assert isinstance(heartbeat.error, RuntimeLeaseError)
