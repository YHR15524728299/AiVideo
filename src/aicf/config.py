from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

from aicf.models.contracts import SupportedPlatform
from aicf.path_utils import project_root
from aicf.secret_store import load_runtime_secrets

# 注意：.env 加载由应用入口点统一处理
_runtime_secrets_loaded = False

def _ensure_runtime_secrets() -> None:
    """确保运行时密钥已加载（幂等，只加载一次）。"""
    global _runtime_secrets_loaded
    if not _runtime_secrets_loaded:
        load_runtime_secrets()
        _runtime_secrets_loaded = True

DEFAULT_CONTENT_PLATFORMS: tuple[SupportedPlatform, ...] = (
    "douyin",
    "xiaohongshu",
    "youtube_shorts",
    "tiktok",
)


class VideoConfig(BaseModel):
    target_duration_seconds: int = Field(default=120, ge=1)
    min_duration_seconds: int | None = Field(default=None, ge=1)
    max_duration_seconds: int | None = Field(default=None, ge=1)
    aspect_ratio: str = "9:16"
    resolution: str = "1080x1920"
    fps: int = Field(default=30, ge=1)

    @model_validator(mode="after")
    def validate_bounds(self) -> "VideoConfig":
        # 自动计算min/max默认值：如果未显式设置，使用target的合理范围
        # 短视频（<=90s）：±25%，长视频：±50%，绝对边界30-300秒
        if self.min_duration_seconds is None:
            if self.target_duration_seconds <= 90:
                self.min_duration_seconds = max(30, int(self.target_duration_seconds * 0.75))
            else:
                self.min_duration_seconds = max(30, int(self.target_duration_seconds * 0.5))
        if self.max_duration_seconds is None:
            if self.target_duration_seconds <= 90:
                self.max_duration_seconds = min(300, int(self.target_duration_seconds * 1.25))
            else:
                self.max_duration_seconds = min(300, int(self.target_duration_seconds * 1.5))
        # 确保绝对边界
        if self.min_duration_seconds < 30:
            self.min_duration_seconds = 30
        if self.max_duration_seconds > 300:
            self.max_duration_seconds = 300
        # 验证约束
        if not self.min_duration_seconds <= self.target_duration_seconds <= self.max_duration_seconds:
            raise ValueError("视频时长必须满足 min <= target <= max")
        return self


class AutopilotConfig(BaseModel):
    enabled: bool = True
    require_topic_approval: bool = False
    require_script_approval: bool = False
    require_render_approval: bool = False
    auto_repair: bool = True
    max_repair_rounds: int = Field(default=2, ge=0, le=2)


class VisualProductionConfig(BaseModel):
    mode: Literal["economy", "balanced", "full_motion"] = "balanced"


class GenerationBudgetConfig(BaseModel):
    max_topic_candidates: int = Field(default=10, ge=1, le=12)
    max_llm_retries_per_stage: int = Field(default=3, ge=0)
    max_image_retries_per_scene: int = Field(default=2, ge=0)
    max_video_retries_per_scene: int = Field(default=3, ge=0)
    max_jimeng_concurrency: int = Field(default=1, ge=1)
    max_jimeng_images: int = Field(default=100, ge=0)
    max_jimeng_video_clips: int = Field(default=30, ge=0)
    max_jimeng_video_seconds_requested: int = Field(default=500, ge=0)
    enable_asset_cache: bool = True


class AppConfig(BaseModel):
    direction: str = Field(min_length=1)
    series_name: str = "AI生成内容真相"
    audience: str = "对 AI 内容生产感兴趣的创作者"
    content_goal: str = "输出有判断、有信息密度、有实际方法的短视频"
    content_pillars: list[str] = Field(default_factory=list)
    tone: list[str] = Field(default_factory=lambda: ["清晰", "直接", "有判断"])
    platforms: list[SupportedPlatform] = Field(
        default_factory=lambda: list(DEFAULT_CONTENT_PLATFORMS)
    )
    languages: list[str] = Field(default_factory=lambda: ["zh-CN"])
    batch_size: int = Field(default=1, ge=1)
    video: VideoConfig = Field(default_factory=VideoConfig)
    autopilot: AutopilotConfig = Field(default_factory=AutopilotConfig)
    visual_production: VisualProductionConfig = Field(default_factory=VisualProductionConfig)
    generation_budget: GenerationBudgetConfig = Field(default_factory=GenerationBudgetConfig)
    visual_style: str = ""
    avoid: list[str] = Field(default_factory=list)


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    return AppConfig.model_validate(data)
