from pathlib import Path

import pytest
from pydantic import ValidationError

from aicf.production_settings import (
    MOTION_MODE_DISPLAY_NAMES,
    MOTION_MODE_VALUES,
    ORIENTATION_DISPLAY_NAMES,
    PLATFORM_DISPLAY_NAMES,
    ProductionSettings,
)


def test_defaults_target_douyin_portrait_video_mode() -> None:
    settings = ProductionSettings()
    assert settings.selected_platforms == ("douyin",)
    assert settings.jimeng_model == "seedance2.0fast"
    assert settings.video_resolution == "720p"
    assert settings.motion_mode == "video"
    assert settings.narration_voice == "kokoro:zm_yunyang"
    assert settings.orientation == "portrait"


def test_platform_validation_rejects_empty_selection() -> None:
    with pytest.raises(ValidationError):
        ProductionSettings(selected_platforms=())


def test_platform_validation_rejects_duplicates() -> None:
    with pytest.raises(ValidationError):
        ProductionSettings(selected_platforms=["douyin", "douyin"])


def test_1080p_requires_vip_model() -> None:
    with pytest.raises(ValidationError):
        ProductionSettings(video_resolution="1080p", jimeng_model="seedance2.0fast")
    ProductionSettings(video_resolution="1080p", jimeng_model="seedance2.0_vip")


def test_landscape_orientation_accepted() -> None:
    settings = ProductionSettings(orientation="landscape")
    assert settings.orientation == "landscape"


def test_image_mode_all_shots_are_images() -> None:
    settings = ProductionSettings(motion_mode="image")
    assert settings.motion_mode == "image"


def test_video_mode_all_shots_are_videos() -> None:
    settings = ProductionSettings(motion_mode="video")
    assert settings.motion_mode == "video"


def test_legacy_motion_mode_values_migrate() -> None:
    """旧版本的 economy/balanced/full_motion 自动迁移为 image/video。"""
    assert ProductionSettings(motion_mode="economy").motion_mode == "image"
    assert ProductionSettings(motion_mode="balanced").motion_mode == "video"
    assert ProductionSettings(motion_mode="full_motion").motion_mode == "video"


def test_chinese_display_maps_cover_all_values() -> None:
    assert MOTION_MODE_DISPLAY_NAMES == {"image": "图片模式", "video": "视频模式"}
    assert MOTION_MODE_VALUES == {"图片模式": "image", "视频模式": "video"}
    assert set(PLATFORM_DISPLAY_NAMES) == {
        "douyin",
        "xiaohongshu",
        "tiktok",
        "youtube_shorts",
        "youtube",
    }
    assert set(ORIENTATION_DISPLAY_NAMES) == {"portrait", "landscape"}


def test_freeze_for_job_is_idempotent(tmp_path: Path) -> None:
    settings = ProductionSettings(
        selected_platforms=["tiktok", "douyin"],
        jimeng_model="seedance2.0_vip",
        video_resolution="1080p",
        motion_mode="image",
        orientation="landscape",
    )
    first = settings.freeze_for_job(tmp_path)
    second = ProductionSettings().freeze_for_job(tmp_path)
    assert first == second
    assert second.selected_platforms == ("tiktok", "douyin")
    assert second.motion_mode == "image"
    assert second.orientation == "landscape"


def test_youtube_platform_and_landscape_orientation() -> None:
    settings = ProductionSettings(
        selected_platforms=["youtube"],
        orientation="landscape",
        motion_mode="video",
    )
    assert "youtube" in settings.selected_platforms
    assert settings.orientation == "landscape"
    assert settings.motion_mode == "video"
