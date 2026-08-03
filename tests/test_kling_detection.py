from __future__ import annotations

import json
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

import aicf.providers.kling as kling
from aicf.settings_dialog import _VideoPage


@pytest.fixture(autouse=True)
def clear_detection_cache() -> None:
    kling._clear_detection_cache()
    yield
    kling._clear_detection_cache()


def _completed(payload: dict[str, object], returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["kling", "who_am_i"],
        returncode,
        json.dumps(payload),
        "",
    )


def test_concurrent_kling_detection_uses_single_who_am_i_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kling, "_find_kling_cli", lambda: "kling.cmd")
    calls = 0
    lock = threading.Lock()

    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        with lock:
            calls += 1
        return _completed(
            {
                "ok": True,
                "status": 200,
                "body": {
                    "user": {"userId": 1},
                    "availableModels": {},
                    "authMode": "oauth",
                },
            }
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda _: kling.detect_kling_cli(command_runner=runner),
                range(12),
            )
        )

    assert calls == 1
    assert all(result.authentication_state == "authenticated" for result in results)
    assert all(result.supports_async_task for result in results)


def test_kling_detection_retries_rate_limit_instead_of_reporting_logged_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kling, "_find_kling_cli", lambda: "kling.cmd")
    responses = [
        _completed(
            {
                "ok": False,
                "status": 400,
                "body": "Kling MCP is handling too many requests right now. "
                "Please slow down and retry shortly.",
            },
            returncode=1,
        ),
        _completed(
            {
                "ok": True,
                "status": 200,
                "body": {
                    "user": {"userId": 1},
                    "availableModels": {},
                    "authMode": "oauth",
                },
            }
        ),
    ]
    sleeps: list[float] = []

    result = kling.detect_kling_cli(
        command_runner=lambda *_args, **_kwargs: responses.pop(0),
        sleep=sleeps.append,
    )

    assert result.authentication_state == "authenticated"
    assert result.detection_error is None
    assert sleeps == [0.5]


def test_rate_limit_without_known_success_is_unknown_not_logged_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kling, "_find_kling_cli", lambda: "kling.cmd")
    response = _completed(
        {
            "ok": False,
            "status": 400,
            "body": "Kling MCP is handling too many requests right now.",
        },
        returncode=1,
    )

    result = kling.detect_kling_cli(
        command_runner=lambda *_args, **_kwargs: response,
        sleep=lambda _delay: None,
    )

    assert result.authentication_state == "unknown"
    assert result.supports_async_task is False
    assert "服务繁忙" in (result.detection_error or "")


def test_explicit_auth_failure_is_reported_as_not_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(kling, "_find_kling_cli", lambda: "kling.cmd")
    response = _completed(
        {"ok": False, "status": 401, "body": "Please login first"},
        returncode=1,
    )

    result = kling.detect_kling_cli(
        command_runner=lambda *_args, **_kwargs: response,
    )

    assert result.authentication_state == "not_authenticated"
    assert result.supports_async_task is False


def test_submit_video_disables_kling_audio_and_passes_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []
    adapter = kling.KlingCliAdapter(
        "kling.cmd",
        kling.KlingCliCapabilities(
            cli_path="kling.cmd",
            default_video_model="kling-video-v2_6",
        ),
    )

    def fake_run(args: list[str], *, timeout=None) -> dict[str, object]:
        captured.extend(args)
        return {"generationId": "gen-1"}

    monkeypatch.setattr(adapter, "_run_cli", fake_run)

    generation_id = adapter.submit_video(
        "测试视频",
        5,
        model="kling-video-v2_6",
        ratio="16:9",
        resolution="720p",
    )

    assert generation_id == "gen-1"
    assert captured == [
        "text_to_video",
        "--model",
        "kling-video-v2_6",
        "--aspectRatio",
        "16:9",
        "--duration",
        "5",
        "--resolution",
        "720p",
        "--enableAudio",
        "false",
        "测试视频",
    ]


def test_noisy_kling_error_uses_final_json_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = kling.KlingCliAdapter("kling.cmd", retry_count=0)
    output = (
        "[kling] 提交 arguments：包含很长的调试信息\n"
        '{"ok":false,"status":400,"body":"Insufficient credits. Please top up."}'
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["kling"],
            1,
            output,
            "",
        ),
    )

    with pytest.raises(kling.KlingCliError) as captured:
        adapter._run_cli(["text_to_video", "测试"])

    message = str(captured.value)
    assert "Insufficient credits" in message
    assert "提交 arguments" not in message


def test_noisy_kling_success_parses_final_json_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = kling.KlingCliAdapter("kling.cmd", retry_count=0)
    output = (
        "[kling] 提交成功\n"
        '{"ok":true,"status":200,"body":{"generationId":"gen-2"}}'
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["kling"],
            0,
            output,
            "",
        ),
    )

    assert adapter._run_cli(["text_to_video", "测试"]) == {
        "generationId": "gen-2"
    }


class _CardSpy:
    def __init__(self) -> None:
        self.state = ""
        self.message = ""

    def ok(self, message: str) -> None:
        self.state = "ok"
        self.message = message

    def warn(self, message: str) -> None:
        self.state = "warn"
        self.message = message

    def err(self, message: str, *, show_manual: bool = False) -> None:
        del show_manual
        self.state = "error"
        self.message = message


def test_settings_page_does_not_call_unknown_kling_state_logged_out() -> None:
    page = object.__new__(_VideoPage)
    page.kl = _CardSpy()
    page._notify = lambda: None

    page._kl_ok(
        kling.KlingCliCapabilities(
            cli_path="kling.cmd",
            supports_async_task=False,
            detection_error="可灵服务繁忙，登录状态暂时无法确认",
            authentication_state="unknown",
        )
    )

    assert page.kl.state == "warn"
    assert "服务繁忙" in page.kl.message
    assert "需要登录" not in page.kl.message


def test_settings_page_shows_authenticated_kling_as_ready() -> None:
    page = object.__new__(_VideoPage)
    page.kl = _CardSpy()
    page._notify = lambda: None

    page._kl_ok(
        kling.KlingCliCapabilities(
            cli_path="kling.cmd",
            supports_async_task=True,
            authentication_state="authenticated",
        )
    )

    assert page.kl.state == "ok"
    assert "已登录就绪" in page.kl.message
