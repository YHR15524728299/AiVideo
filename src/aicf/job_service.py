"""GUI 与 CLI 共用的任务恢复用例。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, Generic, Protocol, TypeVar

from .state_machine import FailureKind, PipelineStage


T = TypeVar("T")


class JobRepositoryProtocol(Protocol):
    def get_job(self, job_id: str) -> Any: ...

    def reopen_failed_attention(
        self,
        job_id: str,
        *,
        recoverable_reason: str,
    ) -> Any: ...


class ResumeMode(str, Enum):
    CONTINUE = "CONTINUE"
    RETRY_FAILED_STAGE = "RETRY_FAILED_STAGE"
    AUTO_REOPEN = "AUTO_REOPEN"
    REQUIRES_CONFIRMATION = "REQUIRES_CONFIRMATION"


class ResearchResumeStrategy(str, Enum):
    RETRY_SOURCES = "RETRY_SOURCES"
    INTERNAL_KNOWLEDGE = "INTERNAL_KNOWLEDGE"


class ResumeAction(str, Enum):
    START_WORKER = "START_WORKER"
    REOPEN_FAILED_ATTENTION = "REOPEN_FAILED_ATTENTION"


@dataclass(frozen=True)
class ResumeDecision:
    allowed: bool
    mode: ResumeMode | None
    reason: str = ""
    recovery_command: str = ""
    requires_reopen: bool = False
    research_strategy: ResearchResumeStrategy | None = None
    actions: frozenset[ResumeAction] = frozenset()

    def permits(self, action: ResumeAction) -> bool:
        return action in self.actions


@dataclass(frozen=True)
class ResumeJobResult(Generic[T]):
    decision: ResumeDecision
    started: bool
    value: T | None = None

    @property
    def mode(self) -> ResumeMode | None:
        return self.decision.mode

    @property
    def reason(self) -> str:
        return self.decision.reason

    @property
    def recovery_command(self) -> str:
        return self.decision.recovery_command


_AUTO_REOPEN_STAGES = frozenset({
    PipelineStage.DIRECTION_ANALYZED,
    PipelineStage.TOPICS_GENERATED,
    PipelineStage.RESEARCHED,
    PipelineStage.SCRIPT_GENERATED,
    PipelineStage.SCRIPT_REVIEWED,
    PipelineStage.CONTENT_PACKAGED,
    PipelineStage.AUDIO_GENERATED,
    PipelineStage.STORYBOARD_GENERATED,
    PipelineStage.CLIP_PLAN_CREATED,
    PipelineStage.KEYFRAMES_GENERATED,
    PipelineStage.VIDEO_CLIPS_GENERATED,
    PipelineStage.AUTO_REPAIRED,
})


class JobService:
    def __init__(self, repository: JobRepositoryProtocol) -> None:
        self._repository = repository

    def resume_job(
        self,
        job_id: str,
        *,
        start: Callable[[str, ResearchResumeStrategy | None], T],
        research_strategy: ResearchResumeStrategy | None = None,
        expected_failed_stage: PipelineStage | None = None,
    ) -> ResumeJobResult[T]:
        """按持久化状态决定恢复方式，并且仅在合同允许时启动。"""
        decision = self.plan_resume(
            job_id,
            research_strategy=research_strategy,
            expected_failed_stage=expected_failed_stage,
        )
        if not decision.allowed:
            return ResumeJobResult(
                decision=decision,
                started=False,
            )
        if decision.requires_reopen:
            if not decision.permits(ResumeAction.REOPEN_FAILED_ATTENTION):
                raise PermissionError("恢复决策未授权重开失败任务")
            self._repository.reopen_failed_attention(
                job_id,
                recoverable_reason="external_service_retry",
            )
        if not decision.permits(ResumeAction.START_WORKER):
            raise PermissionError("恢复决策未授权启动Worker")
        return ResumeJobResult(
            decision=decision,
            started=True,
            value=start(job_id, decision.research_strategy),
        )

    def authorize_worker(
        self,
        job_id: str,
        *,
        requested_strategy: ResearchResumeStrategy | None = None,
        expected_failed_stage: PipelineStage | None = None,
    ) -> ResumeDecision:
        """在Worker进程边界再次校验服务层授予的动作与研究策略。"""
        decision = self.plan_resume(
            job_id,
            research_strategy=requested_strategy,
            expected_failed_stage=expected_failed_stage,
        )
        if not decision.allowed or decision.requires_reopen:
            return decision
        if (
            requested_strategy is not None
            and requested_strategy != decision.research_strategy
        ):
            return ResumeDecision(
                allowed=False,
                mode=ResumeMode.REQUIRES_CONFIRMATION,
                reason="请求的研究策略未由当前恢复状态授权，已拒绝启动Worker。",
                recovery_command=decision.recovery_command,
            )
        if not decision.permits(ResumeAction.START_WORKER):
            return ResumeDecision(
                allowed=False,
                mode=ResumeMode.REQUIRES_CONFIRMATION,
                reason="当前恢复决策未授权启动Worker。",
                recovery_command=decision.recovery_command,
            )
        return decision

    def plan_resume(
        self,
        job_id: str,
        *,
        research_strategy: ResearchResumeStrategy | None = None,
        expected_failed_stage: PipelineStage | None = None,
    ) -> ResumeDecision:
        """只读取持久化状态，生成所有入口共享的唯一恢复决策。"""
        status = self._repository.get_job(job_id)
        current_stage = status.current_stage

        if expected_failed_stage is not None and (
            current_stage != PipelineStage.FAILED_RETRYABLE
            or status.failed_stage != expected_failed_stage
        ):
            actual = (
                status.failed_stage.value
                if status.failed_stage is not None
                else "无"
            )
            return ResumeDecision(
                allowed=False,
                mode=ResumeMode.REQUIRES_CONFIRMATION,
                reason=(
                    f"请求重试阶段 {expected_failed_stage.value} 与持久化失败阶段 "
                    f"{actual} 不一致，已拒绝启动。"
                ),
                recovery_command=status.next_resume_command,
            )

        if current_stage == PipelineStage.COMPLETED:
            return ResumeDecision(
                allowed=False,
                mode=None,
                reason="任务已完成，不能继续恢复；请使用新任务ID重新制作。",
            )

        if current_stage == PipelineStage.FAILED_NEEDS_ATTENTION:
            failed_stage = status.failed_stage
            can_auto_reopen = (
                status.failure_kind == FailureKind.TRANSIENT_EXTERNAL
                and failed_stage in _AUTO_REOPEN_STAGES
            )
            if not can_auto_reopen:
                return ResumeDecision(
                    allowed=False,
                    mode=ResumeMode.REQUIRES_CONFIRMATION,
                    reason=(
                        "该 Job 需要人工确认后才能重开；"
                        "恢复操作不会直接启动不可恢复阶段。"
                    ),
                    recovery_command=status.next_resume_command,
                )
            return ResumeDecision(
                allowed=True,
                mode=ResumeMode.AUTO_REOPEN,
                requires_reopen=True,
                actions=frozenset({
                    ResumeAction.REOPEN_FAILED_ATTENTION,
                    ResumeAction.START_WORKER,
                }),
            )

        mode = (
            ResumeMode.RETRY_FAILED_STAGE
            if current_stage == PipelineStage.FAILED_RETRYABLE
            else ResumeMode.CONTINUE
        )
        selected_strategy = None
        if (
            current_stage == PipelineStage.FAILED_RETRYABLE
            and status.failed_stage == PipelineStage.RESEARCHED
        ):
            selected_strategy = (
                research_strategy
                or ResearchResumeStrategy.INTERNAL_KNOWLEDGE
            )
        return ResumeDecision(
            allowed=True,
            mode=mode,
            research_strategy=selected_strategy,
            actions=frozenset({ResumeAction.START_WORKER}),
        )
