from pathlib import Path

from aicf.gui import build_production_settings, final_video_for_job
from aicf.production_settings import ProductionSettings


def test_gui_values_default_to_douyin_portrait_video_mode() -> None:
    settings = build_production_settings(
        {
            "douyin": True,
            "xiaohongshu": False,
            "tiktok": False,
            "youtube_shorts": False,
            "youtube": False,
        },
        jimeng_model="seedance2.0fast",
        video_resolution="720p",
        motion_mode="video",
        narration_voice="kokoro:zm_yunyang",
        orientation="portrait",
    )

    assert settings == ProductionSettings()
    assert settings.motion_mode == "video"


def test_gui_image_mode_and_landscape_youtube_settings() -> None:
    settings = build_production_settings(
        {
            "douyin": False,
            "xiaohongshu": False,
            "tiktok": False,
            "youtube_shorts": False,
            "youtube": True,
        },
        jimeng_model="seedance2.0_vip",
        video_resolution="1080p",
        motion_mode="image",
        narration_voice="zh-CN-YunxiNeural",
        orientation="landscape",
    )

    assert settings.selected_platforms == ("youtube",)
    assert settings.orientation == "landscape"
    assert settings.motion_mode == "image"
    assert settings.jimeng_model == "seedance2.0_vip"
    assert settings.video_resolution == "1080p"


def test_final_video_uses_frozen_platform_order(tmp_path: Path) -> None:
    job_dir = tmp_path / "JOB001"
    ProductionSettings(
        selected_platforms=["tiktok", "douyin"],
    ).save_for_job(job_dir)
    douyin = job_dir / "delivery" / "douyin" / "video.mp4"
    douyin.parent.mkdir(parents=True)
    douyin.write_bytes(b"video")

    assert final_video_for_job(job_dir) == douyin
