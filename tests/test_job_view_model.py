from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from aicf.job_view_model import (
    HealthStatus,
    JobViewModelPoller,
    JobViewModelBuilder,
    newer_view_model,
)
from aicf.process_identity import ProcessProbe, ProcessProbeStatus


def status(tmp_path: Path) -> SimpleNamespace:
    item = SimpleNamespace(
        job_id="JOB-1",
        version=3,
        output_dir=str(tmp_path / "jobs" / "JOB-1"),
        current_stage=SimpleNamespace(value="COMPLETED"),
        failed_stage=None,
        completed_stages=[SimpleNamespace(value="COMPLETED")],
        updated_at="2026-08-20T10:00:00+00:00",
    )
    item.model_dump = lambda mode="json": {
        "job_id": item.job_id,
        "version": item.version,
        "current_stage": getattr(item.current_stage, "value", item.current_stage),
        "failed_stage": getattr(item.failed_stage, "value", item.failed_stage),
        "completed_stages": [
            getattr(stage, "value", stage) for stage in item.completed_stages
        ],
        "updated_at": item.updated_at,
    }
    return item


class Repository:
    def __init__(self, item: SimpleNamespace) -> None:
        self.item = item

    def list_jobs(self) -> list[SimpleNamespace]:
        return [self.item]

    def get_job(self, job_id: str) -> SimpleNamespace:
        assert job_id == self.item.job_id
        return self.item


def builder(tmp_path: Path, **overrides: object) -> JobViewModelBuilder:
    item = status(tmp_path)
    job_dir = Path(item.output_dir)
    snapshot = (
        '{"job_id":"JOB-1","version":3,"current_stage":"COMPLETED",'
        '"failed_stage":null,"completed_stages":["COMPLETED"],'
        '"updated_at":"2026-08-20T10:00:00+00:00"}'
    )
    defaults = {
        "repository": Repository(item),
        "project_root": tmp_path,
        "read_text": lambda path: snapshot,
        "path_is_file": lambda path: path.name == "status.json",
        "path_stat": lambda path: SimpleNamespace(st_size=10, st_mtime=1),
        "worker_reader": lambda job_dir: None,
        "lock_probe": lambda path: False,
        "process_probe": lambda pid: ProcessProbe(
            status=ProcessProbeStatus.NOT_RUNNING
        ),
        "final_video_probe": lambda job_dir, output_dir: (
            job_dir / "delivery" / "video.mp4"
        ),
        "resume_planner": lambda job_id: None,
    }
    defaults.update(overrides)
    return JobViewModelBuilder(**defaults)


def test_collects_immutable_generation_view_model(tmp_path: Path) -> None:
    model = builder(tmp_path).collect(7, selected_job_id="JOB-1")

    assert model.generation == 7
    assert model.health is HealthStatus.HEALTHY
    assert model.selected_job_id == "JOB-1"
    assert model.actions.can_open_video is True
    assert model.jobs[0].snapshot_version == 3
    with pytest.raises(FrozenInstanceError):
        model.generation = 8  # type: ignore[misc]


def test_database_failure_is_unknown_and_fail_closed(tmp_path: Path) -> None:
    class BrokenRepository:
        def list_jobs(self) -> list[object]:
            raise OSError("database unavailable")

    model = builder(
        tmp_path,
        repository=BrokenRepository(),
    ).collect(1, selected_job_id="")

    assert model.health is HealthStatus.UNKNOWN
    assert model.actions.can_start is False
    assert model.actions.can_resume is False
    assert model.actions.can_stop is False
    assert model.issues[0].source == "repository"


def test_snapshot_failure_is_degraded_and_fail_closed(tmp_path: Path) -> None:
    def fail_snapshot(path: Path) -> str:
        raise PermissionError(f"cannot read {path.name}")

    model = builder(
        tmp_path,
        read_text=fail_snapshot,
    ).collect(2, selected_job_id="JOB-1")

    assert model.health is HealthStatus.DEGRADED
    assert model.jobs[0].health is HealthStatus.DEGRADED
    assert model.actions.can_open_video is False
    assert model.actions.can_resume is False
    assert any(issue.source == "snapshot" for issue in model.issues)


def test_process_probe_failure_is_unknown_and_fail_closed(tmp_path: Path) -> None:
    worker = SimpleNamespace(
        pid=123,
        finished_at=None,
        terminal_status=None,
        stop_requested_at=None,
        process_created_at_ns=456,
        process_executable="python.exe",
    )

    def fail_process(_pid: int) -> ProcessProbe:
        raise OSError("process table unavailable")

    model = builder(
        tmp_path,
        worker_reader=lambda job_dir: worker,
        process_probe=fail_process,
    ).collect(3, selected_job_id="JOB-1")

    assert model.health is HealthStatus.UNKNOWN
    assert model.actions.can_start is False
    assert any(issue.source == "process" for issue in model.issues)


def test_older_generation_is_ignored(tmp_path: Path) -> None:
    latest = builder(tmp_path).collect(9, selected_job_id="JOB-1")
    stale = builder(tmp_path).collect(8, selected_job_id="JOB-1")

    assert newer_view_model(latest, stale) is latest
    assert newer_view_model(stale, latest) is latest


@pytest.mark.parametrize(
    ("source", "overrides", "expected_health"),
    [
        (
            "worker",
            {"worker_reader": lambda _path: (_ for _ in ()).throw(OSError("worker"))},
            HealthStatus.UNKNOWN,
        ),
        (
            "lock",
            {"lock_probe": lambda _path: (_ for _ in ()).throw(OSError("lock"))},
            HealthStatus.UNKNOWN,
        ),
        (
            "delivery",
            {
                "final_video_probe": lambda _job, _output: (
                    _ for _ in ()
                ).throw(OSError("video"))
            },
            HealthStatus.DEGRADED,
        ),
        (
            "resume",
            {"resume_planner": lambda _job: (_ for _ in ()).throw(OSError("planner"))},
            HealthStatus.UNKNOWN,
        ),
    ],
)
def test_runtime_dependency_exceptions_fail_closed(
    tmp_path: Path,
    source: str,
    overrides: dict[str, object],
    expected_health: HealthStatus,
) -> None:
    model = builder(tmp_path, **overrides).collect(
        10, selected_job_id="JOB-1"
    )

    assert model.health is expected_health
    assert model.actions.can_start is False
    assert model.actions.can_resume is False
    assert model.actions.can_stop is False
    assert model.actions.can_open_video is False
    assert any(issue.source == source for issue in model.issues)


def test_log_io_exception_fails_closed(tmp_path: Path) -> None:
    model = builder(
        tmp_path,
        path_is_file=lambda path: path.name in {"status.json", "worker.log"},
        path_stat=lambda _path: (_ for _ in ()).throw(OSError("slow log failed")),
    ).collect(11, selected_job_id="JOB-1")

    assert model.health is HealthStatus.DEGRADED
    assert model.actions.can_open_video is False
    assert any(issue.source == "log" for issue in model.issues)


def test_research_file_exception_fails_closed(tmp_path: Path) -> None:
    item = status(tmp_path)
    item.current_stage = SimpleNamespace(value="FAILED_RETRYABLE")
    item.failed_stage = SimpleNamespace(value="RESEARCHED")
    snapshot = (
        '{"job_id":"JOB-1","version":3,"current_stage":"FAILED_RETRYABLE",'
        '"failed_stage":"RESEARCHED","completed_stages":[]}'
    )

    def read_text(path: Path) -> str:
        if path.name == "research_sources.json":
            raise OSError("research read failed")
        return snapshot

    model = builder(
        tmp_path,
        repository=Repository(item),
        read_text=read_text,
        path_is_file=lambda path: path.name in {
            "status.json",
            "research_sources.json",
        },
    ).collect(12, selected_job_id="JOB-1")

    assert model.health is HealthStatus.DEGRADED
    assert model.actions.can_retry_research is False
    assert any(issue.source == "research" for issue in model.issues)


def test_snapshot_version_exception_fails_closed(tmp_path: Path) -> None:
    model = builder(
        tmp_path,
        read_text=lambda _path: (
            '{"job_id":"JOB-1","version":99,"current_stage":"COMPLETED"}'
        ),
    ).collect(13, selected_job_id="JOB-1")

    assert model.health is HealthStatus.DEGRADED
    assert model.actions.can_open_video is False
    assert any(issue.source == "snapshot" for issue in model.issues)


def test_same_version_snapshot_semantic_drift_fails_closed(
    tmp_path: Path,
) -> None:
    model = builder(
        tmp_path,
        read_text=lambda _path: (
            '{"job_id":"JOB-1","version":3,"current_stage":"FAILED_RETRYABLE",'
            '"failed_stage":"RENDERED","completed_stages":[],'
            '"updated_at":"2026-08-20T10:00:00+00:00"}'
        ),
    ).collect(14, selected_job_id="JOB-1")

    assert model.health is HealthStatus.DEGRADED
    assert model.actions.can_resume is False
    assert any(
        issue.source == "snapshot" and "语义" in issue.message
        for issue in model.issues
    )


def test_active_lock_with_worker_read_exception_is_unknown(
    tmp_path: Path,
) -> None:
    model = builder(
        tmp_path,
        worker_reader=lambda _path: (_ for _ in ()).throw(
            OSError("worker record unreadable")
        ),
        lock_probe=lambda _path: True,
    ).collect(15, selected_job_id="JOB-1")

    assert model.health is HealthStatus.UNKNOWN
    assert model.jobs[0].lock_active is True
    assert model.actions.can_start is False
    assert model.actions.can_resume is False
    assert model.actions.can_stop is False
    assert any(issue.source == "worker" for issue in model.issues)


def test_active_lock_without_live_worker_record_is_unknown(
    tmp_path: Path,
) -> None:
    model = builder(
        tmp_path,
        worker_reader=lambda _path: None,
        lock_probe=lambda _path: True,
    ).collect(16, selected_job_id="JOB-1")

    assert model.health is HealthStatus.UNKNOWN
    assert model.jobs[0].lock_active is True
    assert model.jobs[0].running is False
    assert model.actions.can_start is False
    assert any(
        issue.source == "worker" and "运行锁" in issue.message
        for issue in model.issues
    )


def test_lock_probe_exception_is_unknown(tmp_path: Path) -> None:
    model = builder(
        tmp_path,
        lock_probe=lambda _path: (_ for _ in ()).throw(
            OSError("lock state unavailable")
        ),
    ).collect(16, selected_job_id="JOB-1")

    assert model.health is HealthStatus.UNKNOWN
    assert model.actions.can_start is False
    assert any(issue.source == "lock" for issue in model.issues)


def test_unselected_degraded_history_does_not_close_healthy_selection(
    tmp_path: Path,
) -> None:
    selected = status(tmp_path)
    selected.output_dir = str(tmp_path / "jobs" / "JOB-1")
    degraded = status(tmp_path)
    degraded.job_id = "OLD"
    degraded.output_dir = str(tmp_path / "jobs" / "OLD")

    class TwoJobs(Repository):
        def list_jobs(self) -> list[SimpleNamespace]:
            return [selected, degraded]

        def get_job(self, job_id: str) -> SimpleNamespace:
            return selected if job_id == "JOB-1" else degraded

    def read_text(path: Path) -> str:
        if path.parent.name == "OLD":
            raise OSError("old snapshot unavailable")
        return (
            '{"job_id":"JOB-1","version":3,"current_stage":"COMPLETED",'
            '"failed_stage":null,"completed_stages":["COMPLETED"]}'
        )

    model = builder(
        tmp_path,
        repository=TwoJobs(selected),
        read_text=read_text,
    ).collect(14, selected_job_id="JOB-1")

    assert model.health is HealthStatus.DEGRADED
    assert model.selected_job().health is HealthStatus.HEALTHY
    assert model.actions.can_open_video is True


def test_selected_actions_are_constrained_by_other_running_job(
    tmp_path: Path,
) -> None:
    selected = status(tmp_path)
    running = status(tmp_path)
    running.job_id = "RUNNING"
    running.output_dir = str(tmp_path / "jobs" / "RUNNING")
    running.current_stage = SimpleNamespace(value="RESEARCHED")
    running.completed_stages = [SimpleNamespace(value="DIRECTION_LOADED")]

    class TwoJobs(Repository):
        def list_jobs(self) -> list[SimpleNamespace]:
            return [selected, running]

    def read_text(path: Path) -> str:
        job_id = path.parent.name
        stage = "RESEARCHED" if job_id == "RUNNING" else "COMPLETED"
        completed = (
            '["DIRECTION_LOADED"]'
            if job_id == "RUNNING"
            else '["COMPLETED"]'
        )
        return (
            f'{{"job_id":"{job_id}","version":3,'
            f'"current_stage":"{stage}","failed_stage":null,'
            f'"completed_stages":{completed}}}'
        )

    worker = SimpleNamespace(
        pid=123,
        finished_at=None,
        terminal_status=None,
        process_created_at_ns=None,
        process_executable=None,
    )
    model = builder(
        tmp_path,
        repository=TwoJobs(selected),
        read_text=read_text,
        worker_reader=lambda job_dir: (
            worker if job_dir.name == "RUNNING" else None
        ),
        lock_probe=lambda path: path.parent.name == "RUNNING",
        final_video_probe=lambda job_dir, _output: (
            job_dir / "delivery" / "video.mp4"
            if job_dir.name == "JOB-1"
            else None
        ),
    ).collect(15, selected_job_id="JOB-1")

    assert model.running_job_id == "RUNNING"
    assert model.actions.can_start is False
    assert model.actions.can_resume is False
    assert model.actions.can_stop is True
    assert model.actions.can_open_video is True


def test_direction_exception_changes_health_before_actions_are_published(
    tmp_path: Path,
) -> None:
    snapshot = (
        '{"job_id":"JOB-1","version":3,"current_stage":"COMPLETED",'
        '"failed_stage":null,"completed_stages":["COMPLETED"]}'
    )

    def read_text(path: Path) -> str:
        if path.name == "direction.json":
            raise OSError("direction unavailable")
        return snapshot

    model = builder(
        tmp_path,
        read_text=read_text,
        path_is_file=lambda path: path.name in {
            "status.json",
            "direction.json",
        },
    ).collect(16, selected_job_id="JOB-1")

    assert model.health is HealthStatus.DEGRADED
    assert model.jobs[0].health is HealthStatus.DEGRADED
    assert model.actions.can_open_video is False
    assert any(issue.source == "direction" for issue in model.issues)


def test_poller_publishes_unknown_generation_then_recovers(
    tmp_path: Path,
) -> None:
    attempts = 0

    def factory() -> JobViewModelBuilder:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("top-level poll failure")
        return builder(tmp_path)

    poller = JobViewModelPoller(factory)
    failed = poller.next(selected_job_id="JOB-1")
    recovered = poller.next(selected_job_id="JOB-1")

    assert failed.generation == 1
    assert failed.health is HealthStatus.UNKNOWN
    assert failed.actions.can_resume is False
    assert recovered.generation == 2
    assert recovered.health is HealthStatus.HEALTHY
