from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import aicf.gui as gui_module
from aicf.background_worker import WorkerRecord, write_worker_record
from aicf.database import JobRepository
from aicf.gui import AicfGUI
from aicf.job_lifecycle import JobLifecycleError, JobLifecycleOutcome
from aicf.job_view_model import HealthStatus
from aicf.state_machine import PipelineStage, TransitionError


def _running_job(
    tmp_path: Path,
    *,
    job_id: str = "JOB-INTERRUPT",
    instance_id: str = "worker-a",
) -> tuple[JobRepository, Path]:
    repository = JobRepository(tmp_path / "content.db")
    job_dir = tmp_path / job_id
    repository.create_job(job_id, job_dir)
    repository.start_stage(job_id, PipelineStage.DIRECTION_LOADED)
    write_worker_record(
        job_dir,
        WorkerRecord(
            job_id=job_id,
            pid=123,
            started_at="now",
            log_path="worker.log",
            instance_id=instance_id,
        ),
    )
    return repository, job_dir


def test_mark_interrupted_database_failure_leaves_snapshot_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, job_dir = _running_job(tmp_path)
    snapshot = job_dir / "status.json"
    before_snapshot = snapshot.read_bytes()
    before_status = repository.get_job("JOB-INTERRUPT")

    def fail_save(_connection: object, _status: object) -> None:
        raise sqlite3.OperationalError("database write failed")

    monkeypatch.setattr(repository, "_save_in_transaction", fail_save)

    with pytest.raises(sqlite3.OperationalError, match="database write failed"):
        repository.mark_interrupted(
            "JOB-INTERRUPT",
            PipelineStage.DIRECTION_LOADED,
            "用户强制停止",
            "worker-a",
        )

    assert snapshot.read_bytes() == before_snapshot
    after_status = repository.get_job("JOB-INTERRUPT")
    assert after_status.version == before_status.version
    assert after_status.current_stage == PipelineStage.DIRECTION_LOADED


def test_mark_interrupted_commits_matching_database_and_snapshot(
    tmp_path: Path,
) -> None:
    repository, job_dir = _running_job(tmp_path)
    before_version = repository.get_job("JOB-INTERRUPT").version

    interrupted = repository.mark_interrupted(
        "JOB-INTERRUPT",
        PipelineStage.DIRECTION_LOADED,
        "用户强制停止",
        "worker-a",
    )

    saved = json.loads((job_dir / "status.json").read_text(encoding="utf-8"))
    assert interrupted.version == before_version + 1
    assert interrupted.current_stage == PipelineStage.FAILED_RETRYABLE
    assert interrupted.failed_stage == PipelineStage.DIRECTION_LOADED
    assert saved["version"] == interrupted.version
    assert saved["current_stage"] == interrupted.current_stage.value
    assert saved["failed_stage"] == interrupted.failed_stage.value
    assert saved["stages"]["DIRECTION_LOADED"]["error"] == "用户强制停止"
    assert (
        saved["stages"]["DIRECTION_LOADED"]["worker_instance_id"]
        == "worker-a"
    )


@pytest.mark.parametrize(
    ("expected_stage", "expected_version"),
    [
        (PipelineStage.DIRECTION_ANALYZED, None),
        (PipelineStage.DIRECTION_LOADED, 999),
    ],
)
def test_mark_interrupted_rejects_stale_stage_or_version(
    tmp_path: Path,
    expected_stage: PipelineStage,
    expected_version: int | None,
) -> None:
    repository, job_dir = _running_job(tmp_path)
    before_snapshot = (job_dir / "status.json").read_bytes()

    with pytest.raises(TransitionError):
        repository.mark_interrupted(
            "JOB-INTERRUPT",
            expected_stage,
            "过期的停止请求",
            "worker-a",
            expected_version=expected_version,
        )

    assert (job_dir / "status.json").read_bytes() == before_snapshot
    assert (
        repository.get_job("JOB-INTERRUPT").current_stage
        == PipelineStage.DIRECTION_LOADED
    )


class _RepositorySpy:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def get_job(self, job_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            job_id=job_id,
            current_stage=PipelineStage.DIRECTION_LOADED,
            failed_stage=None,
        )

    def mark_interrupted(
        self,
        job_id: str,
        expected_stage: PipelineStage,
        reason: str,
        worker_instance_id: str,
    ) -> None:
        self.calls.append(
            (job_id, expected_stage, reason, worker_instance_id)
        )


class _LifecycleSpy:
    def __init__(
        self,
        *,
        stop_error: bool = False,
        outcome: JobLifecycleOutcome = JobLifecycleOutcome.COMPLETED,
    ) -> None:
        self.stop_error = stop_error
        self.outcome = outcome
        self.calls: list[tuple[str, ...]] = []

    def request_stop(self, job_id: str) -> None:
        self.calls.append(("request_stop", job_id))
        if self.stop_error:
            raise JobLifecycleError("stale")

    def force_interrupt(self, job_id: str, reason: str) -> SimpleNamespace:
        self.calls.append(("force_interrupt", job_id, reason))
        return SimpleNamespace(
            outcome=self.outcome,
            repair_reason="租约清理失败",
        )

    def delete_job(self, job_id: str) -> SimpleNamespace:
        self.calls.append(("delete_job", job_id))
        return SimpleNamespace(cleanup_errors=())


def _gui_stub(
    job_dir: Path,
    repository: _RepositorySpy,
    lifecycle: _LifecycleSpy,
) -> SimpleNamespace:
    def submit(
        _name: str,
        operation: object,
        *,
        on_success: object = None,
        on_error: object = None,
    ) -> None:
        try:
            result = operation()
        except BaseException as error:
            if on_error is not None:
                on_error(error)
        else:
            if on_success is not None:
                on_success(result)

    gui = SimpleNamespace(
        _polling_job_id="JOB-GUI",
        _current_job_id=lambda: "JOB-GUI",
        _get_job_dir=lambda _job_id: job_dir,
        _get_repo=lambda: repository,
        _get_lifecycle_coordinator=lambda: lifecycle,
        _log=lambda *_args: None,
        _refresh_job_list=lambda: None,
        _update_button_states=lambda: None,
        _is_job_really_running=lambda *_args, **_kwargs: False,
        _submit_io_command=submit,
        _force_refresh_event=SimpleNamespace(set=lambda: None),
    )
    gui._mark_job_interrupted = (
        lambda job_id, worker_instance_id, reason: AicfGUI._mark_job_interrupted(
            gui,
            job_id,
            worker_instance_id,
            reason,
        )
    )
    return gui


def test_gui_force_stop_delegates_business_state_to_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _RepositorySpy()
    lifecycle = _LifecycleSpy(stop_error=True)
    gui = _gui_stub(tmp_path, repository, lifecycle)
    (tmp_path / "status.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gui_module.messagebox, "askyesno", lambda *_a, **_k: True)
    monkeypatch.setattr(gui_module.messagebox, "showinfo", lambda *_a, **_k: None)
    monkeypatch.setattr(
        gui_module,
        "atomic_write_text",
        lambda *_a, **_k: pytest.fail("GUI不得直接写status.json"),
    )

    AicfGUI._stop_job(gui)

    assert lifecycle.calls == [
        ("request_stop", "JOB-GUI"),
        (
            "force_interrupt",
            "JOB-GUI",
            "用户强制停止，可点击继续/恢复",
        ),
    ]
    assert repository.calls == []


def test_gui_force_clean_delegates_business_state_to_repository(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _RepositorySpy()
    lifecycle = _LifecycleSpy()
    gui = _gui_stub(tmp_path, repository, lifecycle)
    (tmp_path / "status.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gui_module.messagebox, "askyesno", lambda *_a, **_k: True)
    monkeypatch.setattr(gui_module.messagebox, "showinfo", lambda *_a, **_k: None)
    monkeypatch.setattr(
        gui_module,
        "atomic_write_text",
        lambda *_a, **_k: pytest.fail("GUI不得直接写status.json"),
    )

    AicfGUI._force_clean_job(gui)

    assert lifecycle.calls == [
        (
            "force_interrupt",
            "JOB-GUI",
            "用户强制停止，可点击继续/恢复",
        ),
    ]
    assert repository.calls == []


def test_gui_reports_committed_interrupt_that_still_needs_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _RepositorySpy()
    lifecycle = _LifecycleSpy(
        outcome=JobLifecycleOutcome.COMMITTED_NEEDS_REPAIR
    )
    gui = _gui_stub(tmp_path, repository, lifecycle)
    warnings: list[tuple[object, ...]] = []
    monkeypatch.setattr(gui_module.messagebox, "askyesno", lambda *_a, **_k: True)
    monkeypatch.setattr(
        gui_module.messagebox,
        "showwarning",
        lambda *args, **_kwargs: warnings.append(args),
    )

    AicfGUI._force_clean_job(gui)

    assert warnings
    assert "已提交" in str(warnings[0])
    assert "租约清理失败" in str(warnings[0])


def test_gui_delete_is_thin_lifecycle_coordinator_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = _RepositorySpy()
    lifecycle = _LifecycleSpy()
    gui = _gui_stub(tmp_path, repository, lifecycle)
    gui._job_view_model = SimpleNamespace(
        jobs=(
            SimpleNamespace(
                job_id="JOB-GUI",
                health=HealthStatus.HEALTHY,
                running=False,
            ),
        ),
    )
    gui._reset_stages = lambda: None
    gui._set_status = lambda _value: None
    gui._display_job_id = "JOB-GUI"
    gui._user_selected_job = True
    monkeypatch.setattr(gui_module.messagebox, "askyesno", lambda *_a, **_k: True)
    monkeypatch.setattr(gui_module.messagebox, "showwarning", lambda *_a, **_k: None)

    AicfGUI._delete_selected_job(gui)

    assert lifecycle.calls == [("delete_job", "JOB-GUI")]
    assert repository.calls == []
