from pathlib import Path
from types import SimpleNamespace

from aicf.gui import (
    AicfGUI,
    build_production_settings,
    final_video_for_job,
    update_direction_config,
    worker_start_command,
)
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


def test_final_video_prefers_flat_user_delivery(tmp_path: Path) -> None:
    job_dir = tmp_path / "data" / "jobs" / "JOB001"
    user_dir = tmp_path / "outputs" / "JOB001"
    user_dir.mkdir(parents=True)
    final = user_dir / "最终视频.mp4"
    final.write_bytes(b"video")

    assert final_video_for_job(job_dir, user_dir) == final


def test_gui_starts_detached_worker_command() -> None:
    command = worker_start_command("JOB001")
    assert command[-4:] == ["aicf", "worker-start", "--job", "JOB001"]


def test_focusing_job_id_exits_history_selection_before_refresh() -> None:
    calls: list[object] = []

    class FakeTree:
        def selection(self) -> tuple[str, ...]:
            return ("OLD_JOB",)

        def selection_remove(self, item: str) -> None:
            calls.append(("selection_remove", item))

        def focus(self, item: str) -> None:
            calls.append(("focus", item))

    gui = SimpleNamespace(
        job_tree=FakeTree(),
        _display_job_id="OLD_JOB",
        _user_selected_job=True,
        _highlight_selected_job=lambda: calls.append("highlight"),
        _reset_stages=lambda: calls.append("reset"),
        _update_button_states=lambda: calls.append("buttons"),
        root=SimpleNamespace(
            after_idle=lambda callback: (calls.append("after_idle"), callback())
        ),
    )
    gui._enter_job_id_edit_mode = lambda: AicfGUI._enter_job_id_edit_mode(gui)

    AicfGUI._on_job_id_focus(gui, SimpleNamespace())

    assert gui._display_job_id == ""
    assert gui._user_selected_job is False
    assert ("selection_remove", "OLD_JOB") in calls
    assert ("focus", "") in calls
    assert "reset" in calls


def test_update_direction_config_preserves_other_settings(tmp_path: Path) -> None:
    config_path = tmp_path / "content_direction.yaml"
    config_path.write_text(
        "direction: 原方向\n"
        "series_name: 系列名\n"
        "video:\n"
        "  target_duration_seconds: 30\n"
        "  min_duration_seconds: 20\n"
        "  max_duration_seconds: 40\n"
        "generation_budget:\n"
        "  max_jimeng_video_clips: 3\n",
        encoding="utf-8",
    )

    update_direction_config(config_path, "新方向\n第二行")

    text = config_path.read_text(encoding="utf-8")
    assert "direction: |-" in text
    assert "新方向" in text
    assert "第二行" in text
    assert "series_name: 系列名" in text
    assert "target_duration_seconds: 30" in text
    assert "max_jimeng_video_clips: 3" in text
