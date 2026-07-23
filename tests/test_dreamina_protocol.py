from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from aicf.providers.jimeng import (
    DreaminaTaskFailed,
    JimengCliAdapter,
    detect_jimeng_cli,
)


HELP = """
Generator Commands:
  text2image
  text2video
Built-in Commands:
  query_result
"""
IMAGE_HELP = """
dreamina text2image --prompt string --ratio string --model_version string --poll int
"""
VIDEO_HELP = """
dreamina text2video --prompt string --duration int --ratio string
  --model_version string --poll int
duration 4-15
"""
QUERY_HELP = """
dreamina query_result --submit_id string --download_dir string
"""


def completed(payload: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=json.dumps(payload, ensure_ascii=False),
        stderr="",
    )


def capabilities():
    responses = {
        ("dreamina", "--help"): completed({"help": HELP}),
        ("dreamina", "text2image", "--help"): completed({"help": IMAGE_HELP}),
        ("dreamina", "text2video", "--help"): completed({"help": VIDEO_HELP}),
        ("dreamina", "query_result", "--help"): completed({"help": QUERY_HELP}),
    }

    def runner(command: list[str], **_: object):
        response = responses[tuple(command)]
        response.stdout = response.stdout.removeprefix('{"help": "').removesuffix('"}').replace("\\n", "\n")
        return response

    return detect_jimeng_cli([["dreamina"]], command_runner=runner)


def write_png(path: Path, size: tuple[int, int] = (576, 1024)) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (20, 40, 80)).save(path)


def test_detects_dreamina_1411_async_protocol_and_4_to_15_second_video() -> None:
    detected = capabilities()

    assert detected.image_command == [
        "text2image",
        "--prompt",
        "{prompt}",
        "--ratio",
        "{ratio}",
        "--model_version",
        "{model}",
        "--poll",
        "0",
    ]
    assert detected.video_command[0] == "text2video"
    assert detected.status_command == [
        "query_result",
        "--submit_id",
        "{submit_id}",
    ]
    assert detected.download_command[-2:] == ["--download_dir", "{download_dir}"]
    assert detected.supported_durations == [float(value) for value in range(4, 16)]
    assert detected.supports_async_task is True


def test_image_submission_parses_submit_id_polls_and_downloads_to_chinese_path(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []
    query_count = 0

    def runner(command: list[str], **_: object):
        nonlocal query_count
        calls.append(command)
        if command[1] == "text2image":
            return completed({"submit_id": "提交-123", "gen_status": "pending"})
        if "--download_dir" not in command:
            query_count += 1
            return completed(
                {
                    "submit_id": "提交-123",
                    "gen_status": "querying" if query_count == 1 else "success",
                }
            )
        download_dir = Path(command[command.index("--download_dir") + 1])
        write_png(download_dir / "生成图.png")
        return completed({"submit_id": "提交-123", "gen_status": "success"})

    adapter = JimengCliAdapter(
        "dreamina",
        capabilities(),
        command_runner=runner,
        sleep=lambda _: None,
        poll_interval_seconds=0.01,
    )
    target = tmp_path / "中文输出" / "竖屏图片.png"

    result = adapter.generate_image(
        "赛博朋克上海夜景",
        target,
        model="4.1",
        ratio="9:16",
    )

    assert result.output_path == target
    assert target.is_file()
    with Image.open(target) as image:
        assert image.size == (576, 1024)
    assert calls[0] == [
        "dreamina",
        "text2image",
        "--prompt",
        "赛博朋克上海夜景",
        "--ratio",
        "9:16",
        "--model_version",
        "4.1",
        "--poll",
        "0",
    ]
    assert calls[-1][-2] == "--download_dir"


def test_failure_state_stops_polling_with_server_reason(tmp_path: Path) -> None:
    def runner(command: list[str], **_: object):
        if command[1] == "text2image":
            return completed({"submit_id": "bad-task"})
        return completed(
            {
                "submit_id": "bad-task",
                "gen_status": "failure",
                "fail_reason": "内容审核失败",
            }
        )

    adapter = JimengCliAdapter(
        "dreamina",
        capabilities(),
        command_runner=runner,
        sleep=lambda _: None,
    )

    with pytest.raises(DreaminaTaskFailed, match="内容审核失败"):
        adapter.generate_image("失败提示词", tmp_path / "bad.png")


def test_failure_reason_uses_shared_sanitizer() -> None:
    reason = JimengCliAdapter.failure_reason(
        {
            "fail_reason": (
                "Bearer dreamina-secret "
                "cookie=session-secret "
                r"path=C:\Users\Alice\private.png"
            )
        }
    )

    assert "dreamina-secret" not in reason
    assert "session-secret" not in reason
    assert "Alice" not in reason
    assert "***REDACTED***" in reason


def test_only_idempotent_timeout_is_retried_once(tmp_path: Path) -> None:
    query_attempts = 0

    def query_retry_runner(command: list[str], **_: object):
        nonlocal query_attempts
        if command[1] == "text2image":
            return completed({"submit_id": "retry-task"})
        query_attempts += 1
        if query_attempts == 1:
            raise subprocess.TimeoutExpired(command, 1)
        if "--download_dir" in command:
            download_dir = Path(command[command.index("--download_dir") + 1])
            write_png(download_dir / "ok.png")
        return completed({"submit_id": "retry-task", "gen_status": "success"})

    adapter = JimengCliAdapter(
        "dreamina",
        capabilities(),
        command_runner=query_retry_runner,
        retry_count=1,
        sleep=lambda _: None,
    )
    adapter.generate_image("可重试", tmp_path / "retry.png")
    assert query_attempts == 3

    submit_attempts = 0

    def submit_timeout(command: list[str], **_: object):
        nonlocal submit_attempts
        submit_attempts += 1
        raise subprocess.TimeoutExpired(command, 1)

    unsafe_adapter = JimengCliAdapter(
        "dreamina",
        capabilities(),
        command_runner=submit_timeout,
        retry_count=1,
    )
    with pytest.raises(subprocess.TimeoutExpired):
        unsafe_adapter.generate_image("不能重复扣费", tmp_path / "no.png")
    assert submit_attempts == 1


def test_cache_key_changes_with_prompt_model_ratio_and_duration() -> None:
    base = JimengCliAdapter._cache_key("提示词", "4.1", "9:16", None)

    assert base != JimengCliAdapter._cache_key("另一提示词", "4.1", "9:16", None)
    assert base != JimengCliAdapter._cache_key("提示词", "4.0", "9:16", None)
    assert base != JimengCliAdapter._cache_key("提示词", "4.1", "16:9", None)
    assert (
        JimengCliAdapter._cache_key("视频", "seedance2.0fast", "9:16", 4)
        != JimengCliAdapter._cache_key("视频", "seedance2.0fast", "9:16", 5)
    )


def test_pillow_rejects_corrupt_image(tmp_path: Path) -> None:
    invalid = tmp_path / "corrupt.png"
    invalid.write_bytes(b"not-an-image")

    assert JimengCliAdapter.validate_image(invalid) is False


def test_ffprobe_accepts_video_stream_and_rejects_audio_only(tmp_path: Path) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"container")

    valid_adapter = JimengCliAdapter(
        "dreamina",
        capabilities(),
        command_runner=lambda *_args, **_kwargs: completed(
            {"streams": [{"codec_type": "video", "codec_name": "h264"}]}
        ),
        ffprobe_executable="ffprobe",
    )
    assert valid_adapter.validate_video(video) is True

    invalid_adapter = JimengCliAdapter(
        "dreamina",
        capabilities(),
        command_runner=lambda *_args, **_kwargs: completed(
            {"streams": [{"codec_type": "audio", "codec_name": "aac"}]}
        ),
        ffprobe_executable="ffprobe",
    )
    assert invalid_adapter.validate_video(video) is False


@pytest.mark.parametrize("duration", [3, 16])
def test_video_duration_is_limited_to_4_through_15(
    tmp_path: Path,
    duration: int,
) -> None:
    adapter = JimengCliAdapter("dreamina", capabilities())

    with pytest.raises(ValueError, match="4-15"):
        adapter.generate_video("越界", duration, tmp_path / "bad.mp4")


def test_video_failure_falls_back_to_image_and_ken_burns(tmp_path: Path) -> None:
    def runner(command: list[str], **_: object):
        if command[1] == "text2video":
            return completed({"submit_id": "video-task"})
        if command[1] == "query_result" and "--download_dir" not in command:
            submit_id = command[command.index("--submit_id") + 1]
            if submit_id == "image-task":
                return completed(
                    {"submit_id": "image-task", "gen_status": "success"}
                )
            return completed(
                {
                    "submit_id": "video-task",
                    "gen_status": "failure",
                    "fail_reason": "视频生成失败",
                }
            )
        if command[1] == "text2image":
            return completed({"submit_id": "image-task"})
        if "--download_dir" in command:
            download_dir = Path(command[command.index("--download_dir") + 1])
            write_png(download_dir / "fallback.png")
        return completed({"submit_id": "image-task", "gen_status": "success"})

    adapter = JimengCliAdapter(
        "dreamina",
        capabilities(),
        command_runner=runner,
        sleep=lambda _: None,
    )

    result = adapter.generate_video(
        "需要降级",
        5,
        tmp_path / "中文" / "镜头.mp4",
        model="seedance2.0fast",
        ratio="9:16",
    )

    assert result.degraded is True
    assert result.output_path.suffix == ".png"
    assert result.ken_burns_plan_path is not None
    plan = json.loads(result.ken_burns_plan_path.read_text(encoding="utf-8"))
    assert plan["duration_seconds"] == 5
    assert plan["source"].endswith(".png")


def test_atomic_submit_query_download_methods_support_durable_runner(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    def runner(command: list[str], **_: object):
        calls.append(command)
        if command[1] == "text2image":
            return completed({"submit_id": "durable-task"})
        if "--download_dir" in command:
            download_dir = Path(command[command.index("--download_dir") + 1])
            write_png(download_dir / "durable.png")
        return completed({"submit_id": "durable-task", "gen_status": "success"})

    adapter = JimengCliAdapter(
        "dreamina",
        capabilities(),
        command_runner=runner,
    )

    submit_id = adapter.submit_image("可恢复提交", model="4.1", ratio="9:16")

    assert submit_id == "durable-task"
    assert len(calls) == 1
    assert adapter.query(submit_id)["gen_status"] == "success"
    target = tmp_path / "durable.png"
    adapter.download(submit_id, target, kind="image")
    assert target.is_file()
    assert adapter.failure_reason({"fail_reason": "审核失败"}) == "审核失败"
