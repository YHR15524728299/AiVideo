import queue
import threading
import time
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from aicf.gui import (
    AicfGUI,
    UiMessage,
    build_production_settings,
    final_video_for_job,
    update_direction_config,
    worker_start_command,
)
from aicf.production_settings import ProductionSettings
from aicf.job_service import ResearchResumeStrategy
from aicf.job_actions import JobActionState
from aicf.job_view_model import HealthStatus, JobViewModel


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


def test_gui_worker_command_carries_research_strategy_without_marker_file() -> None:
    command = worker_start_command(
        "JOB001",
        ResearchResumeStrategy.RETRY_SOURCES,
    )

    assert command[-2:] == ["--research-strategy", "RETRY_SOURCES"]


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


def _view_model(generation: int, job_id: str = "JOB-1") -> JobViewModel:
    return JobViewModel(
        generation=generation,
        selected_job_id=job_id,
        health=HealthStatus.HEALTHY,
        actions=JobActionState(
            can_start=False,
            can_resume=True,
            can_stop=False,
            can_open_video=False,
            guidance="可恢复",
        ),
    )


def test_current_job_actions_only_reads_cached_view_model() -> None:
    class FakeTree:
        def selection(self) -> tuple[str, ...]:
            return ("JOB-1",)

    gui = SimpleNamespace(
        job_tree=FakeTree(),
        _job_view_model=_view_model(3),
        _polling_job_id="",
        running=False,
        _get_repo=lambda: (_ for _ in ()).throw(
            AssertionError("UI thread must not access repository")
        ),
    )

    actions = AicfGUI._current_job_actions(gui)

    assert actions.can_resume is True
    assert actions.guidance == "可恢复"


def test_ui_ignores_stale_view_model_generation() -> None:
    applied: list[int] = []
    gui = SimpleNamespace(
        _job_view_model=_view_model(5),
        _applied_view_generation=5,
        _render_job_view_model=lambda model: applied.append(model.generation),
    )

    AicfGUI._apply_job_view_model(gui, _view_model(4))
    AicfGUI._apply_job_view_model(gui, _view_model(6))

    assert applied == [6]
    assert gui._job_view_model.generation == 6
    assert gui._applied_view_generation == 6


def test_slow_repository_and_file_io_command_does_not_block_ui() -> None:
    started = threading.Event()
    release = threading.Event()
    gui = SimpleNamespace(
        _command_queue=queue.Queue(),
        ui_queue=queue.Queue(),
    )
    gui._submit_io_command = lambda *args, **kwargs: AicfGUI._submit_io_command(
        gui, *args, **kwargs
    )

    def slow_repository_and_file_io() -> str:
        started.set()
        release.wait(2)
        return "done"

    before = time.perf_counter()
    gui._submit_io_command("slow_io", slow_repository_and_file_io)
    elapsed = time.perf_counter() - before

    assert elapsed < 0.05
    name, operation, _success, _error = gui._command_queue.get_nowait()
    assert name == "slow_io"
    worker = threading.Thread(target=operation)
    worker.start()
    assert started.wait(0.2)
    release.set()
    worker.join(0.2)
    assert not worker.is_alive()


def test_real_background_command_keeps_ui_heartbeat_and_delivers_result() -> None:
    started = threading.Event()
    release = threading.Event()
    delivered: list[str] = []
    heartbeats: list[tuple[int, object]] = []
    gui = SimpleNamespace(
        _command_queue=queue.Queue(),
        ui_queue=queue.Queue(),
        _ui_message_generation=0,
        _handled_ui_generation=0,
        _ui_message_lock=threading.Lock(),
        log_queue=queue.Queue(),
        root=SimpleNamespace(
            after=lambda delay, callback: heartbeats.append((delay, callback))
        ),
        _poll_log_queue_inner=lambda: None,
    )
    gui._submit_io_command = lambda *args, **kwargs: AicfGUI._submit_io_command(
        gui, *args, **kwargs
    )
    gui._publish_ui = lambda *args, **kwargs: AicfGUI._publish_ui(
        gui, *args, **kwargs
    )
    gui._poll_progress = lambda: AicfGUI._poll_progress(gui)

    def slow_io() -> str:
        started.set()
        release.wait(2)
        return "done"

    AicfGUI._start_background_command_thread(gui)
    gui._submit_io_command("slow_io", slow_io, on_success=delivered.append)
    assert started.wait(0.2)

    before = time.perf_counter()
    AicfGUI._poll_progress(gui)
    assert time.perf_counter() - before < 0.05
    assert heartbeats and heartbeats[-1][0] == 100

    release.set()
    deadline = time.monotonic() + 1
    while gui.ui_queue.empty() and time.monotonic() < deadline:
        time.sleep(0.01)
    AicfGUI._poll_progress(gui)

    assert delivered == ["done"]


def test_ui_queue_messages_are_frozen_and_generation_is_monotonic() -> None:
    gui = SimpleNamespace(
        ui_queue=queue.Queue(),
        _ui_message_generation=0,
        _ui_message_lock=threading.Lock(),
    )

    first = AicfGUI._publish_ui(gui, "set_status", "first")
    second = AicfGUI._publish_ui(gui, "set_status", "second")

    assert (first.generation, second.generation) == (1, 2)
    assert gui.ui_queue.get_nowait() is first
    assert gui.ui_queue.get_nowait() is second
    with pytest.raises(FrozenInstanceError):
        first.kind = "legacy_tuple"  # type: ignore[misc]


def test_poll_progress_rejects_legacy_tuple_protocol() -> None:
    gui = SimpleNamespace(
        ui_queue=queue.Queue(),
        _handled_ui_generation=0,
        _poll_log_queue_inner=lambda: None,
        root=SimpleNamespace(after=lambda _delay, _callback: None),
    )
    gui.ui_queue.put(("set_status", "legacy", None))

    with pytest.raises(TypeError, match="UiMessage"):
        AicfGUI._poll_progress(gui)


def test_poll_progress_ignores_stale_ui_message_generation() -> None:
    statuses: list[str] = []
    gui = SimpleNamespace(
        ui_queue=queue.Queue(),
        _handled_ui_generation=0,
        _set_status=statuses.append,
        _poll_log_queue_inner=lambda: None,
        _poll_progress=lambda: None,
        root=SimpleNamespace(after=lambda _delay, _callback: None),
    )
    gui.ui_queue.put(UiMessage(2, "set_status", "new"))
    gui.ui_queue.put(UiMessage(1, "set_status", "stale"))

    AicfGUI._poll_progress(gui)

    assert statuses == ["new"]
    assert gui._handled_ui_generation == 2


def test_incremental_log_exception_degrades_view_model_and_fails_closed() -> None:
    model = _view_model(7)
    gui = SimpleNamespace(
        _display_job_id="JOB-1",
        running=False,
        _queue_incremental_worker_log=lambda _model: (
            _ for _ in ()
        ).throw(OSError("incremental log unreadable")),
    )
    poller = SimpleNamespace(
        next=lambda **_kwargs: model,
    )

    degraded = AicfGUI._poll_view_model_once(gui, poller)

    assert degraded.health is HealthStatus.DEGRADED
    assert degraded.actions.can_resume is False
    assert degraded.issues[-1].source == "log"
    assert degraded.issues[-1].job_id == "JOB-1"


def test_real_tk_aicfgui_mainloop_stays_responsive_during_slow_startup_io(
    monkeypatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    ui_tick = threading.Event()

    def slow_bootstrap(_self) -> None:
        started.set()
        release.wait(2)

    monkeypatch.setattr(AicfGUI, "_load_runtime_bootstrap", slow_bootstrap)
    monkeypatch.setattr(AicfGUI, "_start_background_poll_thread", lambda _self: None)
    monkeypatch.setattr(AicfGUI, "_stop_preview", lambda _self: None)
    monkeypatch.setattr(
        "aicf.gui.project_root",
        lambda: Path(__file__).resolve().parents[1],
    )

    app = AicfGUI()
    app.root.after(50, ui_tick.set)
    app.root.after(250, app._on_close)
    before = time.perf_counter()
    app.run()
    elapsed = time.perf_counter() - before
    release.set()

    assert started.wait(0.5)
    assert ui_tick.is_set()
    assert elapsed < 1.0


def test_button_state_uses_cached_api_availability(
    monkeypatch,
) -> None:
    class Button:
        def __init__(self) -> None:
            self.state: dict[str, str] = {}

        def configure(self, **kwargs: str) -> None:
            self.state.update(kwargs)

    monkeypatch.setattr(
        "aicf.gui._get_env_value",
        lambda _key: (_ for _ in ()).throw(
            AssertionError("UI thread must not read credentials")
        ),
    )
    actions = JobActionState(
        can_start=True,
        can_resume=False,
        can_stop=False,
        can_open_video=False,
        guidance="ready",
    )
    start_button = Button()
    gui = SimpleNamespace(
        _available_providers=["jimeng"],
        _api_configured=True,
        btn_start=start_button,
        btn_resume=Button(),
        btn_retry_research=Button(),
        btn_research_details=Button(),
        _current_job_actions=lambda: actions,
        job_tree=SimpleNamespace(selection=lambda: ()),
    )

    AicfGUI._update_button_states(gui)

    assert start_button.state["state"] == "normal"
    assert start_button.state["text"] == "▶ 开始生成"


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
