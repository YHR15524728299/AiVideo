from __future__ import annotations

from enum import Enum


class FailureKind(str, Enum):
    """持久化失败分类；恢复授权不得从错误文本反推。"""

    UNKNOWN = "UNKNOWN"
    TRANSIENT_EXTERNAL = "TRANSIENT_EXTERNAL"
    PERMANENT_EXTERNAL = "PERMANENT_EXTERNAL"
    LOCAL_ENVIRONMENT = "LOCAL_ENVIRONMENT"
    INVALID_ARTIFACT = "INVALID_ARTIFACT"
    USER_ACTION_REQUIRED = "USER_ACTION_REQUIRED"


class PipelineStage(str, Enum):
    DIRECTION_LOADED = "DIRECTION_LOADED"
    DIRECTION_ANALYZED = "DIRECTION_ANALYZED"
    TOPICS_GENERATED = "TOPICS_GENERATED"
    TOPIC_SELECTED = "TOPIC_SELECTED"
    RESEARCHED = "RESEARCHED"
    SCRIPT_GENERATED = "SCRIPT_GENERATED"
    SCRIPT_REVIEWED = "SCRIPT_REVIEWED"
    CONTENT_PACKAGED = "CONTENT_PACKAGED"
    AUDIO_GENERATED = "AUDIO_GENERATED"
    NARRATION_TIMELINE_CREATED = "NARRATION_TIMELINE_CREATED"
    STORYBOARD_GENERATED = "STORYBOARD_GENERATED"
    CLIP_PLAN_CREATED = "CLIP_PLAN_CREATED"
    KEYFRAMES_GENERATED = "KEYFRAMES_GENERATED"
    VIDEO_CLIPS_GENERATED = "VIDEO_CLIPS_GENERATED"
    SUBTITLES_GENERATED = "SUBTITLES_GENERATED"
    MASTER_TIMELINE_ASSEMBLED = "MASTER_TIMELINE_ASSEMBLED"
    RENDERED = "RENDERED"
    QA_CHECKED = "QA_CHECKED"
    AUTO_REPAIRED = "AUTO_REPAIRED"
    PACKAGED = "PACKAGED"
    COMPLETED = "COMPLETED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_NEEDS_ATTENTION = "FAILED_NEEDS_ATTENTION"


ORDERED_STAGES = [
    stage
    for stage in PipelineStage
    if stage not in {PipelineStage.FAILED_RETRYABLE, PipelineStage.FAILED_NEEDS_ATTENTION}
]

# 统一终态定义：任务已结束，不会再自动推进
TERMINAL_STAGES: frozenset[PipelineStage] = frozenset({
    PipelineStage.COMPLETED,
    PipelineStage.FAILED_RETRYABLE,
    PipelineStage.FAILED_NEEDS_ATTENTION,
})

# 非运行状态：终态 + INIT（初始态）
NON_RUNNING_STAGES: frozenset[PipelineStage] = TERMINAL_STAGES | frozenset({
    # INIT 不在枚举中，用字符串判断
})


def is_terminal_stage(stage: str | PipelineStage | None) -> bool:
    """判断阶段是否为终态（不会再自动推进）"""
    if stage is None:
        return False
    stage_str = stage.value if isinstance(stage, PipelineStage) else str(stage)
    return stage_str in {s.value for s in TERMINAL_STAGES}


def is_stage_running(stage: str | PipelineStage | None, failed_stage: str | PipelineStage | None = None) -> bool:
    """判断阶段状态是否表示任务可能在运行中（非终态、无失败阶段）"""
    if failed_stage:
        return False
    return not is_terminal_stage(stage) and stage not in (None, "", "INIT")


class TransitionError(ValueError):
    pass


class StateMachine:
    def next_stage(self, current: PipelineStage) -> PipelineStage | None:
        if current not in ORDERED_STAGES:
            return None
        index = ORDERED_STAGES.index(current)
        return ORDERED_STAGES[index + 1] if index + 1 < len(ORDERED_STAGES) else None

    def validate_transition(self, current: PipelineStage, target: PipelineStage) -> None:
        if current == PipelineStage.COMPLETED:
            raise TransitionError("COMPLETED 是终态，不能继续转换")
        if target in {PipelineStage.FAILED_RETRYABLE, PipelineStage.FAILED_NEEDS_ATTENTION}:
            return
        if current == PipelineStage.QA_CHECKED and target == PipelineStage.PACKAGED:
            return
        if self.next_stage(current) != target:
            raise TransitionError(f"非法状态转换: {current.value} -> {target.value}")
