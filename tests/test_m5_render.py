from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from aicf.cli import main
from aicf.engines.render_engine import FfmpegRenderer, MediaProbe, probe_media
from aicf.providers.tts import FfmpegToolchain


def test_renderer_builds_vertical_h264_aac_command_and_writes_manifest(
    tmp_path: Path,
) -> None:
    asset = tmp_path / "visual.png"
    plan = tmp_path / "visual_plan.json"
    audio = tmp_path / "voiceover.wav"
    subtitles = tmp_path / "字幕.ass"
    output = tmp_path / "成片" / "final.mp4"
    asset.write_bytes(b"png")
    plan.write_text(
        json.dumps(
            {
                "title": "AI视频不稳定，先别怪模型",
                "mode": "balanced",
                "total_duration_seconds": 10.166,
                "shots": [
                    {
                        "shot_id": "VIS001",
                        "script_segment_id": "SEG001",
                        "asset_type": "image",
                        "prompt": "中文主视觉，竖屏9:16，无文字",
                        "expected_path": "visual.png",
                        "start_seconds": 0,
                        "duration_seconds": 10.166,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    audio.write_bytes(b"wav")
    subtitles.write_text("[Script Info]\n", encoding="utf-8")
    commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"mp4")
        return subprocess.CompletedProcess(command, 0, "", "")

    result = FfmpegRenderer(
        FfmpegToolchain("full-ffmpeg", "full-ffprobe"),
        command_runner=fake_run,
    ).render(
        visual_plan_path=plan,
        audio_path=audio,
        subtitle_path=subtitles,
        output_path=output,
        title="AI视频不稳定，先别怪模型",
    )

    command = commands[0]
    assert command[0] == "full-ffmpeg"
    assert "-filter_complex_script" in command
    assert "30" in command
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-ar") + 1] == "48000"
    assert command[command.index("-ac") + 1] == "2"
    assert command[command.index("-movflags") + 1] == "+faststart"
    assert result.output_path == output
    pending_manifest = result.pending_master_path.parent / result.manifest_path.name
    manifest = json.loads(pending_manifest.read_text(encoding="utf-8"))
    assert manifest["title"] == "AI视频不稳定，先别怪模型"
    assert manifest["render"]["width"] == 1080
    assert manifest["render"]["height"] == 1920
    assert manifest["render"]["fps"] == 30


def test_probe_media_parses_ffprobe_json_and_rejects_noncompliant_video(
    tmp_path: Path,
) -> None:
    media = tmp_path / "final.mp4"
    media.write_bytes(b"mp4")
    compliant_payload = {
        "format": {"duration": "10.166", "size": "123456"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1080,
                "height": 1920,
                "pix_fmt": "yuv420p",
                "r_frame_rate": "30/1",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
            },
        ],
    }

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(compliant_payload), "")

    probe = probe_media("full-ffprobe", media, command_runner=fake_run)

    assert probe == MediaProbe(
        duration_seconds=pytest.approx(10.166),
        size_bytes=123456,
        video_codec="h264",
        width=1080,
        height=1920,
        pixel_format="yuv420p",
        fps=pytest.approx(30.0),
        audio_codec="aac",
        sample_rate=48000,
        channels=2,
    )
    probe.assert_vertical_delivery(expected_duration_seconds=10.166)

    invalid = MediaProbe(
        duration_seconds=10.166,
        size_bytes=123,
        video_codec="h264",
        width=1920,
        height=1080,
        pixel_format="yuv420p",
        fps=30.0,
        audio_codec="aac",
        sample_rate=48000,
        channels=2,
    )
    with pytest.raises(ValueError, match="1080x1920"):
        invalid.assert_vertical_delivery(expected_duration_seconds=10.166)


def test_render_cli_uses_real_inputs_and_reports_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    audio = tmp_path / "voiceover.wav"
    subtitles = tmp_path / "subtitles.ass"
    plan = tmp_path / "visual_plan.json"
    output = tmp_path / "final.mp4"
    audio.write_bytes(b"wav")
    subtitles.write_text("[Script Info]\n", encoding="utf-8")
    plan.write_text("{}", encoding="utf-8")
    calls: list[dict[str, object]] = []

    class FakeRenderer:
        def render_and_validate(self, **kwargs: object):
            calls.append(kwargs)
            output.write_bytes(b"mp4")
            result = type(
                "Result",
                (),
                {
                    "output_path": output,
                    "clean_output_path": output.with_name("clean.mp4"),
                    "manifest_path": output.with_suffix(".render.json"),
                },
            )()
            probe = MediaProbe(
                10.166,
                123456,
                "h264",
                1080,
                1920,
                "yuv420p",
                30.0,
                "aac",
                48000,
                2,
            )
            return result, {"master": probe, "clean": probe}

    monkeypatch.setattr("aicf.cli.build_renderer", lambda: FakeRenderer())

    exit_code = main(
        [
            "render",
            "--visual-plan",
            str(plan),
            "--audio",
            str(audio),
            "--subtitles",
            str(subtitles),
            "--output",
            str(output),
            "--title",
            "真实集成样片",
        ]
    )

    assert exit_code == 0
    assert calls[0]["audio_path"] == audio
    assert calls[0]["subtitle_path"] == subtitles
    assert calls[0]["visual_plan_path"] == plan
    rendered = capsys.readouterr().out
    assert "1080x1920" in rendered
    assert "h264/yuv420p" in rendered
    assert "aac/48000Hz/2ch" in rendered
