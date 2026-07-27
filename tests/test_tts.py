from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from aicf.providers.tts import (
    EdgeTtsProvider,
    SapiTtsProvider,
    TtsAllProvidersFailed,
    TtsRequest,
    TtsService,
    build_default_tts_service,
    find_audio_ffmpeg,
)


class RecordingProvider:
    def __init__(self, name: str, error: Exception | None = None) -> None:
        self.name = name
        self.error = error
        self.calls: list[TtsRequest] = []

    def synthesize(self, request: TtsRequest) -> None:
        self.calls.append(request)
        if self.error:
            raise self.error
        request.output_path.write_bytes(b"RIFF-test-audio")


def test_edge_tts_is_used_first_without_degradation(tmp_path: Path) -> None:
    edge = RecordingProvider("edge_tts")
    sapi = RecordingProvider("windows_sapi")
    output = tmp_path / "speech.wav"

    result = TtsService([edge, sapi]).synthesize("你好，世界", output)

    assert result.provider == "edge_tts"
    assert result.degraded is False
    assert result.degradation_reason is None
    assert len(edge.calls) == 1
    assert sapi.calls == []
    assert output.read_bytes() == b"RIFF-test-audio"
    assert json.loads(result.metadata_path.read_text(encoding="utf-8")) == {
        "provider": "edge_tts",
        "degraded": False,
        "degradation_reason": None,
        "output_path": str(output),
    }


def test_sapi_is_used_when_edge_tts_fails_and_reason_is_recorded(tmp_path: Path) -> None:
    edge = RecordingProvider("edge_tts", RuntimeError("network unavailable"))
    sapi = RecordingProvider("windows_sapi")
    output = tmp_path / "speech.wav"

    result = TtsService([edge, sapi]).synthesize("自动降级测试", output)

    assert result.provider == "windows_sapi"
    assert result.degraded is True
    assert result.degradation_reason == "edge_tts: RuntimeError: network unavailable"
    assert len(edge.calls) == 1
    assert len(sapi.calls) == 1
    metadata = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert metadata["provider"] == "windows_sapi"
    assert metadata["degradation_reason"] == result.degradation_reason


def test_all_provider_failures_are_reported_without_stale_output(tmp_path: Path) -> None:
    edge = RecordingProvider("edge_tts", RuntimeError("edge down"))
    sapi = RecordingProvider("windows_sapi", OSError("voice missing"))
    output = tmp_path / "speech.wav"
    output.write_bytes(b"stale")

    with pytest.raises(TtsAllProvidersFailed) as exc_info:
        TtsService([edge, sapi]).synthesize("失败测试", output)

    assert not output.exists()
    assert exc_info.value.reasons == [
        "edge_tts: RuntimeError: edge down",
        "windows_sapi: OSError: voice missing",
    ]


def test_default_service_orders_kokoro_first() -> None:
    service = build_default_tts_service()

    provider_names = [provider.name for provider in service.providers]
    # Kokoro（本地神经网络TTS）优先，然后是 Edge TTS，最后是 Windows SAPI 回退
    assert provider_names[0] == "kokoro"
    assert "edge_tts" in provider_names
    assert "windows_sapi" in provider_names


def test_select_voice_routes_to_matching_provider_family() -> None:
    edge = EdgeTtsProvider()
    sapi = SapiTtsProvider()
    service = TtsService([edge, sapi])

    service.select_voice("Microsoft Huihui Desktop")
    assert service.providers[0] is sapi
    assert sapi.voice == "Microsoft Huihui Desktop"

    service.select_voice("zh-CN-YunxiNeural")
    assert service.providers[0] is edge
    assert edge.voice == "zh-CN-YunxiNeural"


def test_edge_provider_generates_audio_then_converts_to_wav(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    class FakeCommunicate:
        def __init__(self, text: str, voice: str) -> None:
            calls.append((text, voice))

        async def save(self, path: str) -> None:
            Path(path).write_bytes(b"fake-mp3")

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        Path(command[-1]).write_bytes(b"RIFF-edge")
        return subprocess.CompletedProcess(command, 0, "", "")

    output = tmp_path / "edge.wav"
    provider = EdgeTtsProvider(
        voice="zh-CN-XiaoxiaoNeural",
        communicate_factory=FakeCommunicate,
        command_runner=fake_run,
    )

    provider.synthesize(TtsRequest("Edge 测试", output))

    assert calls == [("Edge 测试", "zh-CN-XiaoxiaoNeural")]
    assert output.read_bytes() == b"RIFF-edge"
    assert not output.with_suffix(".edge.mp3").exists()


def test_edge_provider_times_out_and_cleans_temporary_file(tmp_path: Path) -> None:
    class HangingCommunicate:
        def __init__(self, text: str, voice: str) -> None:
            pass

        async def save(self, path: str) -> None:
            Path(path).write_bytes(b"partial")
            await asyncio.Event().wait()

    output = tmp_path / "edge.wav"
    provider = EdgeTtsProvider(
        communicate_factory=HangingCommunicate,
        request_timeout_seconds=0.01,
    )

    with pytest.raises(TimeoutError, match="Edge TTS 请求超时"):
        provider.synthesize(TtsRequest("超时测试", output))

    assert not output.with_suffix(".edge.mp3").exists()


def test_sapi_provider_uses_powershell_and_writes_wave_file(tmp_path: Path) -> None:
    commands: list[list[str]] = []
    output = tmp_path / "sapi.wav"

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        output.write_bytes(b"RIFF-sapi")
        return subprocess.CompletedProcess(command, 0, "", "")

    provider = SapiTtsProvider(
        voice="Microsoft Huihui Desktop",
        command_runner=fake_run,
    )
    provider.synthesize(TtsRequest("SAPI 测试", output))

    assert commands[0][0].lower().endswith("powershell.exe")
    assert "-EncodedCommand" in commands[0]
    assert output.read_bytes() == b"RIFF-sapi"


def test_audio_ffmpeg_selection_skips_video_only_build(tmp_path: Path) -> None:
    video_only = tmp_path / "video-only" / "ffmpeg.exe"
    full = tmp_path / "full" / "ffmpeg.exe"
    video_only.parent.mkdir()
    full.parent.mkdir()
    video_only.touch()
    full.touch()

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        formats = " D  mov\n E mp4\n"
        if Path(command[0]) == full:
            formats += " DE  mp3\n DE  wav\n"
        return subprocess.CompletedProcess(command, 0, formats, "")

    selected = find_audio_ffmpeg(
        candidates=[video_only, full],
        command_runner=fake_run,
    )

    assert selected == str(full)
