from __future__ import annotations

from enum import Enum


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
