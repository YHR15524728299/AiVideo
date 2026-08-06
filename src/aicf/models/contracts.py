from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


SupportedPlatform = Literal[
    "douyin",
    "xiaohongshu",
    "youtube_shorts",
    "tiktok",
    "youtube",
]
SUPPORTED_PLATFORMS: tuple[SupportedPlatform, ...] = (
    "douyin",
    "xiaohongshu",
    "youtube_shorts",
    "tiktok",
    "youtube",
)
NonBlankText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class DirectionProfile(BaseModel):
    series_name: NonBlankText
    core_direction: NonBlankText
    audience: NonBlankText
    audience_problems: list[NonBlankText] = Field(default_factory=list)
    content_goal: NonBlankText
    content_pillars: list[NonBlankText] = Field(default_factory=list)
    tone: list[NonBlankText] = Field(default_factory=list)
    visual_style: NonBlankText
    allowed_topic_types: list[NonBlankText] = Field(default_factory=list)
    forbidden_topic_types: list[NonBlankText] = Field(default_factory=list)
    default_video_structure: list[NonBlankText] = Field(default_factory=list)
    differentiation: list[NonBlankText] = Field(default_factory=list)
    repetition_risks: list[NonBlankText] = Field(default_factory=list)
    fact_risk_level: Literal["low", "medium", "high"] = "low"


class TopicCandidate(BaseModel):
    topic_id: NonBlankText
    title: NonBlankText
    hook: NonBlankText
    core_question: NonBlankText
    core_claim: NonBlankText
    content_pillar: NonBlankText
    audience_problem: NonBlankText
    direction_relevance: float = Field(ge=0, le=100)
    hook_strength: float = Field(ge=0, le=100)
    visual_potential: float = Field(ge=0, le=100)
    novelty: float = Field(ge=0, le=100)
    evidence_availability: float = Field(ge=0, le=100)
    production_difficulty: float = Field(ge=0, le=100)
    fact_risk: float = Field(ge=0, le=100)
    overall_score: float = Field(ge=0, le=100)
    selection_reason: NonBlankText


class TopicCandidates(BaseModel):
    candidates: list[TopicCandidate]

    @field_validator("candidates")
    @classmethod
    def require_eight_to_ten_candidates(
        cls,
        value: list[TopicCandidate],
    ) -> list[TopicCandidate]:
        if not 8 <= len(value) <= 10:
            raise ValueError("候选选题数量必须为 8 到 10")
        return value


class ResearchFact(BaseModel):
    claim: NonBlankText
    source_title: NonBlankText
    source_url: NonBlankText
    confidence: float = Field(ge=0, le=1)
    published_at: date | None = None
    source_type: str | None = None


class ResearchResult(BaseModel):
    summary: NonBlankText
    facts: list[ResearchFact] = Field(default_factory=list)
    unknowns: list[NonBlankText] = Field(default_factory=list)


class ScriptSegment(BaseModel):
    segment_id: NonBlankText
    purpose: NonBlankText
    narration: NonBlankText
    visual_brief: NonBlankText
    fact_refs: list[int] = Field(default_factory=list)


class ScriptResult(BaseModel):
    title: NonBlankText
    hook: NonBlankText
    segments: list[ScriptSegment] = Field(min_length=1)
    call_to_action: NonBlankText
    estimated_duration_seconds: float = Field(gt=0)


class VisualShot(BaseModel):
    shot_id: NonBlankText
    script_segment_id: NonBlankText
    asset_type: Literal["image", "video"]
    prompt: NonBlankText
    expected_path: NonBlankText
    start_seconds: float = Field(ge=0)
    duration_seconds: float = Field(gt=0)

    @property
    def end_seconds(self) -> float:
        return self.start_seconds + self.duration_seconds


class VisualPlan(BaseModel):
    title: NonBlankText
    mode: Literal["balanced"]
    total_duration_seconds: float = Field(gt=0)
    shots: list[VisualShot] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_timeline(self) -> "VisualPlan":
        expected_start = 0.0
        for shot in self.shots:
            if abs(shot.start_seconds - expected_start) > 0.001:
                raise ValueError("视觉镜头时间线必须连续并从 0 开始")
            expected_start = shot.end_seconds
        if abs(expected_start - self.total_duration_seconds) > 0.001:
            raise ValueError("视觉镜头总时长必须等于权威音频时长")
        return self


class ReviewScores(BaseModel):
    direction_fit: float = Field(ge=0, le=100)
    hook: float = Field(ge=0, le=100)
    clarity: float = Field(ge=0, le=100)
    evidence: float = Field(ge=0, le=100)
    safety: float = Field(ge=0, le=100)


class ReviewResult(BaseModel):
    passed: bool
    scores: ReviewScores
    issues: list[NonBlankText] = Field(default_factory=list)
    revision_instructions: list[NonBlankText] = Field(default_factory=list)

    @field_validator("issues", "revision_instructions")
    @classmethod
    def require_readable_feedback(cls, values: list[str]) -> list[str]:
        for value in values:
            if not any(character.isalnum() for character in value):
                raise ValueError("审核意见必须包含可读文字，不能只有标点或格式符号")
        return values

    @model_validator(mode="before")
    @classmethod
    def copy_revision_instructions_to_missing_failed_issues(
        cls,
        value: object,
    ) -> object:
        if not isinstance(value, dict):
            return value
        issues = value.get("issues")
        readable_issues = (
            isinstance(issues, list)
            and any(
                isinstance(item, str)
                and any(character.isalnum() for character in item)
                for item in issues
            )
        )
        instructions = value.get("revision_instructions")
        readable_instructions = (
            isinstance(instructions, list)
            and [
                item
                for item in instructions
                if isinstance(item, str)
                and any(character.isalnum() for character in item)
            ]
        )
        if (
            value.get("passed") is False
            and not readable_issues
            and readable_instructions
        ):
            return {
                **value,
                "issues": readable_instructions,
            }
        return value

    @model_validator(mode="after")
    def validate_passed_matches_issues(self) -> "ReviewResult":
        if self.passed and self.issues:
            raise ValueError("审核通过时 issues 必须为空")
        if self.passed and self.revision_instructions:
            raise ValueError("审核通过时 revision_instructions 必须为空")
        if not self.passed and not self.issues:
            raise ValueError("审核未通过时 issues 不能为空")
        return self


class PlatformCopy(BaseModel):
    title: str
    description: str
    hashtags: list[str] = Field(min_length=1)

    @field_validator("title", "description")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("标题和简介不能为空白")
        return value.strip()

    @field_validator("hashtags")
    @classmethod
    def reject_blank_hashtags(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("hashtag 不能为空白")
        return [item.strip() for item in value]


class PackageResult(BaseModel):
    douyin: PlatformCopy
    xiaohongshu: PlatformCopy
    youtube_shorts: PlatformCopy
    tiktok: PlatformCopy
    youtube: PlatformCopy | None = None
