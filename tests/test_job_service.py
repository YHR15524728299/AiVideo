from types import SimpleNamespace

import pytest

from aicf.database import JobRepository
from aicf.job_actions import derive_job_actions
from aicf.job_service import (
    JobService,
    ResearchResumeStrategy,
    ResumeAction,
    ResumeMode,
)
from aicf.state_machine import FailureKind, PipelineStage, TransitionError


class FakeRepository:
    def __init__(self, status: SimpleNamespace) -> None:
        self.status = status
        self.reopen_calls: list[tuple[str, str]] = []

    def get_job(self, job_id: str) -> SimpleNamespace:
        assert job_id == self.status.job_id
        return self.status

    def reopen_failed_attention(
        self,
        job_id: str,
        *,
        recoverable_reason: str,
    ) -> SimpleNamespace:
        self.reopen_calls.append((job_id, recoverable_reason))
        self.status.current_stage = PipelineStage.DIRECTION_LOADED
        self.status.failed_stage = None
        return self.status


def make_status(
    current_stage: PipelineStage | None,
    *,
    failed_stage: PipelineStage | None = None,
    error: str = "",
    failure_kind: FailureKind = FailureKind.UNKNOWN,
) -> SimpleNamespace:
    stages = (
        {failed_stage.value: {"error": error}}
        if failed_stage is not None
        else {}
    )
    return SimpleNamespace(
        job_id="JOB-1",
        current_stage=current_stage,
        failed_stage=failed_stage,
        failure_kind=failure_kind,
        stages=stages,
        next_resume_command=(
            "python -m aicf reopen --job JOB-1 --confirm-artifacts-fixed"
        ),
    )


@pytest.mark.parametrize(
    ("status", "expected_mode"),
    [
        (
            make_status(
                PipelineStage.FAILED_RETRYABLE,
                failed_stage=PipelineStage.RENDERED,
            ),
            ResumeMode.RETRY_FAILED_STAGE,
        ),
        (
            make_status(PipelineStage.RENDERED),
            ResumeMode.CONTINUE,
        ),
        (
            make_status(None),
            ResumeMode.CONTINUE,
        ),
        (
            make_status(
                PipelineStage.FAILED_RETRYABLE,
                failed_stage=PipelineStage.RESEARCHED,
            ),
            ResumeMode.RETRY_FAILED_STAGE,
        ),
    ],
)
def test_resume_starts_retryable_interrupted_and_legacy_init_jobs(
    status: SimpleNamespace,
    expected_mode: ResumeMode,
) -> None:
    repository = FakeRepository(status)
    starts: list[str] = []

    result = JobService(repository).resume_job(
        "JOB-1",
        start=lambda job_id, _strategy: (
            starts.append(job_id) or {"status": "STARTED"}
        ),
    )

    assert result.mode == expected_mode
    assert result.started is True
    assert result.value == {"status": "STARTED"}
    assert starts == ["JOB-1"]
    assert repository.reopen_calls == []


def test_resume_auto_reopens_marked_attention_failure_before_start() -> None:
    repository = FakeRepository(
        make_status(
            PipelineStage.FAILED_NEEDS_ATTENTION,
            failed_stage=PipelineStage.RESEARCHED,
            error="HTTP 503: upstream unavailable",
            failure_kind=FailureKind.TRANSIENT_EXTERNAL,
        )
    )
    starts: list[str] = []

    result = JobService(repository).resume_job(
        "JOB-1",
        start=lambda job_id, _strategy: starts.append(job_id),
    )

    assert result.mode == ResumeMode.AUTO_REOPEN
    assert result.started is True
    assert repository.reopen_calls == [
        ("JOB-1", "external_service_retry")
    ]
    assert starts == ["JOB-1"]
    assert result.decision.permits(ResumeAction.REOPEN_FAILED_ATTENTION)
    assert result.decision.permits(ResumeAction.START_WORKER)


def test_resume_attention_failure_requires_confirmation_and_does_not_start() -> None:
    repository = FakeRepository(
        make_status(
            PipelineStage.FAILED_NEEDS_ATTENTION,
            failed_stage=PipelineStage.QA_CHECKED,
            error="产物哈希不一致",
        )
    )
    starts: list[str] = []

    result = JobService(repository).resume_job(
        "JOB-1",
        start=lambda job_id, _strategy: starts.append(job_id),
    )

    assert result.mode == ResumeMode.REQUIRES_CONFIRMATION
    assert result.started is False
    assert result.recovery_command.endswith("--confirm-artifacts-fixed")
    assert starts == []
    assert repository.reopen_calls == []


def test_resume_completed_job_is_disabled_and_does_not_start() -> None:
    repository = FakeRepository(make_status(PipelineStage.COMPLETED))
    starts: list[str] = []

    result = JobService(repository).resume_job(
        "JOB-1",
        start=lambda job_id, _strategy: starts.append(job_id),
    )

    assert result.mode is None
    assert result.started is False
    assert "已完成" in result.reason
    assert starts == []


def test_plan_resume_is_the_single_research_strategy_decision() -> None:
    repository = FakeRepository(
        make_status(
            PipelineStage.FAILED_RETRYABLE,
            failed_stage=PipelineStage.RESEARCHED,
        )
    )
    service = JobService(repository)

    internal = service.plan_resume("JOB-1")
    retry_sources = service.plan_resume(
        "JOB-1",
        research_strategy=ResearchResumeStrategy.RETRY_SOURCES,
    )

    assert internal.allowed is True
    assert internal.mode == ResumeMode.RETRY_FAILED_STAGE
    assert internal.research_strategy == ResearchResumeStrategy.INTERNAL_KNOWLEDGE
    assert retry_sources.allowed is True
    assert retry_sources.research_strategy == ResearchResumeStrategy.RETRY_SOURCES


@pytest.mark.parametrize(
    "error",
    [
        "HTTP 404: source permanently removed",
        "permission denied while writing final artifact",
        "disk space is exhausted",
        "provider 任务失败",
        "invalid data in persisted artifact",
    ],
)
def test_plan_resume_does_not_auto_reopen_permanent_or_local_errors(
    error: str,
) -> None:
    repository = FakeRepository(
        make_status(
            PipelineStage.FAILED_NEEDS_ATTENTION,
            failed_stage=PipelineStage.RESEARCHED,
            error=error,
        )
    )

    decision = JobService(repository).plan_resume("JOB-1")

    assert decision.allowed is False
    assert decision.mode == ResumeMode.REQUIRES_CONFIRMATION
    assert decision.requires_reopen is False


def test_transient_text_without_persisted_failure_kind_requires_confirmation() -> None:
    repository = FakeRepository(
        make_status(
            PipelineStage.FAILED_NEEDS_ATTENTION,
            failed_stage=PipelineStage.RESEARCHED,
            error="HTTP 503: upstream unavailable",
            failure_kind=FailureKind.UNKNOWN,
        )
    )

    decision = JobService(repository).plan_resume("JOB-1")

    assert decision.allowed is False
    assert decision.mode == ResumeMode.REQUIRES_CONFIRMATION
    assert decision.actions == frozenset()


def test_transient_external_failure_outside_auto_reopen_whitelist_is_denied() -> None:
    repository = FakeRepository(
        make_status(
            PipelineStage.FAILED_NEEDS_ATTENTION,
            failed_stage=PipelineStage.QA_CHECKED,
            failure_kind=FailureKind.TRANSIENT_EXTERNAL,
        )
    )

    decision = JobService(repository).plan_resume("JOB-1")

    assert decision.allowed is False
    assert decision.mode == ResumeMode.REQUIRES_CONFIRMATION
    assert decision.actions == frozenset()


def test_resume_passes_explicit_research_strategy_to_start() -> None:
    repository = FakeRepository(
        make_status(
            PipelineStage.FAILED_RETRYABLE,
            failed_stage=PipelineStage.RESEARCHED,
        )
    )
    starts: list[tuple[str, ResearchResumeStrategy | None]] = []

    result = JobService(repository).resume_job(
        "JOB-1",
        research_strategy=ResearchResumeStrategy.RETRY_SOURCES,
        start=lambda job_id, strategy: starts.append((job_id, strategy)),
    )

    assert result.started is True
    assert starts == [
        ("JOB-1", ResearchResumeStrategy.RETRY_SOURCES),
    ]


def test_retry_requires_exact_persisted_failed_stage_before_start() -> None:
    repository = FakeRepository(
        make_status(
            PipelineStage.FAILED_RETRYABLE,
            failed_stage=PipelineStage.RENDERED,
        )
    )
    starts: list[str] = []

    result = JobService(repository).resume_job(
        "JOB-1",
        expected_failed_stage=PipelineStage.AUDIO_GENERATED,
        start=lambda job_id, _strategy: starts.append(job_id),
    )

    assert result.started is False
    assert result.mode == ResumeMode.REQUIRES_CONFIRMATION
    assert "RENDERED" in result.reason
    assert "AUDIO_GENERATED" in result.reason
    assert starts == []
    assert repository.reopen_calls == []
    assert repository.status.current_stage == PipelineStage.FAILED_RETRYABLE
    assert repository.status.failed_stage == PipelineStage.RENDERED


def test_retry_exact_stage_starts_without_mutating_failed_state_first() -> None:
    repository = FakeRepository(
        make_status(
            PipelineStage.FAILED_RETRYABLE,
            failed_stage=PipelineStage.RENDERED,
        )
    )
    observed: list[tuple[PipelineStage, PipelineStage | None]] = []

    result = JobService(repository).resume_job(
        "JOB-1",
        expected_failed_stage=PipelineStage.RENDERED,
        start=lambda _job_id, _strategy: observed.append(
            (
                repository.status.current_stage,
                repository.status.failed_stage,
            )
        ),
    )

    assert result.started is True
    assert observed == [
        (PipelineStage.FAILED_RETRYABLE, PipelineStage.RENDERED)
    ]
    assert repository.reopen_calls == []


def test_auto_reopen_failure_is_visible_and_prevents_start() -> None:
    repository = FakeRepository(
        make_status(
            PipelineStage.FAILED_NEEDS_ATTENTION,
            failed_stage=PipelineStage.RESEARCHED,
            failure_kind=FailureKind.TRANSIENT_EXTERNAL,
        )
    )
    starts: list[str] = []

    def fail_reopen(
        _job_id: str,
        *,
        recoverable_reason: str,
    ) -> None:
        assert recoverable_reason == "external_service_retry"
        raise OSError("snapshot write failed")

    repository.reopen_failed_attention = fail_reopen  # type: ignore[method-assign]

    with pytest.raises(OSError, match="snapshot write failed"):
        JobService(repository).resume_job(
            "JOB-1",
            start=lambda job_id, _strategy: starts.append(job_id),
        )

    assert starts == []


def test_sqlite_decision_maps_to_same_gui_action_contract(tmp_path) -> None:
    repository = JobRepository(tmp_path / "data" / "content.db")
    repository.create_job("JOB-SQLITE", tmp_path / "jobs" / "JOB-SQLITE")
    repository.start_stage("JOB-SQLITE", PipelineStage.DIRECTION_LOADED)
    repository.fail_stage(
        "JOB-SQLITE",
        PipelineStage.DIRECTION_LOADED,
        "产物哈希不一致",
        retryable=False,
        recovery_command=(
            "python -m aicf reopen --job JOB-SQLITE "
            "--confirm-artifacts-fixed"
        ),
    )

    decision = JobService(repository).plan_resume("JOB-SQLITE")
    actions = derive_job_actions(
        existing_job=True,
        current_stage=PipelineStage.FAILED_NEEDS_ATTENTION.value,
        failed_stage=PipelineStage.DIRECTION_LOADED.value,
        resume_decision=decision,
    )

    assert decision.allowed is False
    assert actions.can_resume is decision.allowed
    assert actions.resume_mode is decision.mode
    assert actions.guidance == decision.reason


def test_failure_kind_persists_in_sqlite_and_snapshot(tmp_path) -> None:
    repository = JobRepository(tmp_path / "data" / "content.db")
    job_dir = tmp_path / "jobs" / "JOB-FAILURE-KIND"
    repository.create_job("JOB-FAILURE-KIND", job_dir)
    repository.start_stage("JOB-FAILURE-KIND", PipelineStage.DIRECTION_LOADED)

    status = repository.fail_stage(
        "JOB-FAILURE-KIND",
        PipelineStage.DIRECTION_LOADED,
        "HTTP 503",
        retryable=False,
        failure_kind=FailureKind.TRANSIENT_EXTERNAL,
    )

    persisted = repository.get_job("JOB-FAILURE-KIND")
    payload = (job_dir / "status.json").read_text(encoding="utf-8")
    assert status.failure_kind == FailureKind.TRANSIENT_EXTERNAL
    assert persisted.failure_kind == FailureKind.TRANSIENT_EXTERNAL
    assert '"failure_kind": "TRANSIENT_EXTERNAL"' in payload


@pytest.mark.parametrize(
    "invalid_reason",
    ["auto_retry", "transient_error", "user_requested_retry", "typo"],
)
def test_repository_rejects_uncontrolled_reopen_reason_without_mutation(
    tmp_path,
    invalid_reason: str,
) -> None:
    repository = JobRepository(tmp_path / "data" / "content.db")
    job_dir = tmp_path / "jobs" / "JOB-REASON"
    repository.create_job("JOB-REASON", job_dir)
    repository.start_stage("JOB-REASON", PipelineStage.DIRECTION_LOADED)
    before = repository.fail_stage(
        "JOB-REASON",
        PipelineStage.DIRECTION_LOADED,
        "needs attention",
        retryable=False,
    )
    snapshot_before = (job_dir / "status.json").read_bytes()

    with pytest.raises(TransitionError, match="允许的可恢复原因"):
        repository.reopen_failed_attention(
            "JOB-REASON",
            recoverable_reason=invalid_reason,
        )

    after = repository.get_job("JOB-REASON")
    assert after.version == before.version
    assert after.current_stage == PipelineStage.FAILED_NEEDS_ATTENTION
    assert after.failed_stage == PipelineStage.DIRECTION_LOADED
    assert (job_dir / "status.json").read_bytes() == snapshot_before
