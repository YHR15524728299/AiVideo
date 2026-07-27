from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aicf.atomic_io import atomic_replace


Platform = Literal["douyin", "xiaohongshu", "tiktok", "youtube_shorts", "youtube"]
JimengModel = Literal[
    "seedance2.0",
    "seedance2.0fast",
    "seedance2.0_vip",
    "seedance2.0fast_vip",
    "seedance2.0mini",
]
VideoResolution = Literal["720p", "1080p"]
MotionMode = Literal["image", "video"]
Orientation = Literal["portrait", "landscape"]
NarrationVoice = Literal[
    # Kokoro 本地神经网络 TTS（推荐，音质最好）
    "kokoro:zf_xiaobei",
    "kokoro:zf_xiaoni",
    "kokoro:zf_xiaoxiao",
    "kokoro:zf_xiaoyi",
    "kokoro:zm_yunjian",
    "kokoro:zm_yunxi",
    "kokoro:zm_yunxia",
    "kokoro:zm_yunyang",
    # Edge TTS（微软在线TTS）
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunyangNeural",
    # Windows SAPI（系统回退）
    "Microsoft Huihui Desktop",
]

# 中文显示名称映射
MOTION_MODE_DISPLAY_NAMES: dict[str, str] = {
    "image": "图片模式",
    "video": "视频模式",
}

MOTION_MODE_VALUES: dict[str, str] = {v: k for k, v in MOTION_MODE_DISPLAY_NAMES.items()}

# 旧版本 motion_mode 值到新值的迁移映射
_LEGACY_MOTION_MODE_MAP: dict[str, str] = {
    "economy": "image",
    "balanced": "video",
    "full_motion": "video",
}

ORIENTATION_DISPLAY_NAMES: dict[str, str] = {
    "portrait": "竖屏 (9:16)",
    "landscape": "横屏 (16:9)",
}

PLATFORM_DISPLAY_NAMES: dict[str, str] = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
    "tiktok": "TikTok",
    "youtube_shorts": "YouTube Shorts",
    "youtube": "YouTube",
}

# 旁白音色中文显示名称
VOICE_DISPLAY_NAMES: dict[str, str] = {
    "kokoro:zf_xiaobei": "Kokoro·小北（女·本地）",
    "kokoro:zf_xiaoni": "Kokoro·小妮（女·本地）",
    "kokoro:zf_xiaoxiao": "Kokoro·晓晓（女·本地）",
    "kokoro:zf_xiaoyi": "Kokoro·小伊（女·本地）",
    "kokoro:zm_yunjian": "Kokoro·云健（男·本地）",
    "kokoro:zm_yunxi": "Kokoro·云希（男·本地）",
    "kokoro:zm_yunxia": "Kokoro·云夏（男·本地）",
    "kokoro:zm_yunyang": "Kokoro·云扬（男·本地·推荐）",
    "zh-CN-XiaoxiaoNeural": "Edge·晓晓（女·在线）",
    "zh-CN-YunxiNeural": "Edge·云希（男·在线）",
    "zh-CN-YunyangNeural": "Edge·云扬（男·在线）",
    "Microsoft Huihui Desktop": "系统·慧慧（女·回退）",
}

VOICE_GROUP_ORDER: list[str] = [
    "kokoro:zm_yunyang",
    "kokoro:zm_yunjian",
    "kokoro:zm_yunxi",
    "kokoro:zm_yunxia",
    "kokoro:zf_xiaobei",
    "kokoro:zf_xiaoni",
    "kokoro:zf_xiaoxiao",
    "kokoro:zf_xiaoyi",
    "zh-CN-YunyangNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-XiaoxiaoNeural",
    "Microsoft Huihui Desktop",
]


class ProductionSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    selected_platforms: tuple[Platform, ...] = Field(default=("douyin",))
    jimeng_model: JimengModel = "seedance2.0fast"
    video_resolution: VideoResolution = "720p"
    motion_mode: MotionMode = "video"
    narration_voice: NarrationVoice = "kokoro:zm_yunyang"
    orientation: Orientation = "portrait"

    @field_validator("motion_mode", mode="before")
    @classmethod
    def migrate_legacy_motion_mode(cls, v: object) -> object:
        """迁移旧版本的 motion_mode 值到新的 image/video 模式。"""
        if isinstance(v, str) and v in _LEGACY_MOTION_MODE_MAP:
            return _LEGACY_MOTION_MODE_MAP[v]
        return v

    @model_validator(mode="after")
    def validate_selection(self) -> "ProductionSettings":
        if not self.selected_platforms:
            raise ValueError("至少选择一个平台")
        if self.video_resolution == "1080p" and self.jimeng_model != "seedance2.0_vip":
            raise ValueError(f"{self.jimeng_model} 不支持 1080p")
        if len(set(self.selected_platforms)) != len(self.selected_platforms):
            raise ValueError("平台不能重复选择")
        return self

    def save_for_job(self, job_dir: str | Path) -> Path:
        target = Path(job_dir) / "production_settings.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(
            self.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
        atomic_replace(temporary, target)
        return target

    def freeze_for_job(self, job_dir: str | Path) -> "ProductionSettings":
        target = Path(job_dir) / "production_settings.json"
        if target.is_file():
            return self.load_for_job(job_dir)
        self.save_for_job(job_dir)
        return self

    @classmethod
    def load_for_job(cls, job_dir: str | Path) -> "ProductionSettings":
        path = Path(job_dir) / "production_settings.json"
        if not path.is_file():
            return cls()
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return cls.model_validate(value)
