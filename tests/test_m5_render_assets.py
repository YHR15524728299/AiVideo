from __future__ import annotations

import json
import math
import subprocess
import wave
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from aicf.engines.render_engine import FfmpegRenderer, probe_media
from aicf.providers.tts import FfmpegToolchain, discover_ffmpeg_toolchain


def _write_inputs(root: Path) -> tuple[Path, Path, Path, Path]:
    assets = root / "素材"
    assets.mkdir()
    image = assets / "渐变 图形.png"
    video = assets / "动态 镜头.mp4"
    image.write_bytes(b"png")
    video.write_bytes(b"mp4")
    audio = root / "旁白.wav"
    subtitles = root / "中文字幕.ass"
    audio.write_bytes(b"wav")
    subtitles.write_text("[Script Info]\n", encoding="utf-8")
    plan = root / "视觉计划.json"
    plan.write_text(
        json.dumps(
            {
                "title": "真实素材成片",
                "mode": "balanced",
                "total_duration_seconds": 3.0,
                "shots": [
                    {
                        "shot_id": "VIS001",
                        "script_segment_id": "SEG001",
                        "asset_type": "image",
                        "prompt": "中文渐变图形，竖屏9:16，无文字",
                        "expected_path": "素材/渐变 图形.png",
                        "start_seconds": 0.0,
                        "duration_seconds": 1.25,
                    },
                    {
                        "shot_id": "VIS002",
                        "script_segment_id": "SEG002",
                        "asset_type": "video",
                        "prompt": "动态测试镜头，竖屏9:16，无文字",
                        "expected_path": "素材/动态 镜头.mp4",
                        "start_seconds": 1.25,
                        "duration_seconds": 1.75,
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return plan, audio, subtitles, assets


def _probe_payload(*, width: int = 1080) -> str:
    return json.dumps(
        {
            "format": {"duration": "3.000", "size": "45678"},
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": width,
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
    )


def test_renderer_uses_real_assets_filter_script_and_atomically_publishes(
    tmp_path: Path,
) -> None:
    plan, audio, subtitles, _ = _write_inputs(tmp_path)
    final_dir = tmp_path / "中文 成片"
    master = final_dir / "master.mp4"
    commands: list[list[str]] = []
    filter_scripts: list[str] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[0] == "ffmpeg":
            script = Path(command[command.index("-filter_complex_script") + 1])
            filter_scripts.append(script.read_text(encoding="utf-8"))
            targets = [
                Path(token)
                for token in command
                if token.endswith(".mp4") and ".pending" in token
            ]
            for target in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"pending-video")
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, _probe_payload(), "")

    result, probes = FfmpegRenderer(
        FfmpegToolchain("ffmpeg", "ffprobe"),
        command_runner=fake_run,
    ).render_and_validate(
        visual_plan_path=plan,
        audio_path=audio,
        subtitle_path=subtitles,
        output_path=master,
        title="真实素材成片",
    )

    ffmpeg_command = commands[0]
    assert ffmpeg_command.count("-i") == 3
    assert "-filter_complex_script" in ffmpeg_command
    script = filter_scripts[0]
    assert "zoompan=" in script
    assert "trim=duration=1.750000" in script
    assert script.count("scale=1080:1920:force_original_aspect_ratio=increase") == 2
    assert script.count("crop=1080:1920") == 2
    assert "concat=n=2:v=1:a=0" in script
    assert "ass=filename=" in script
    assert "中文字幕.ass" in script
    assert result.master_output_path == master
    assert result.clean_output_path == final_dir / "clean.mp4"
    assert result.master_output_path.read_bytes() == b"pending-video"
    assert result.clean_output_path.read_bytes() == b"pending-video"
    assert set(probes) == {"master", "clean"}
    assert not (final_dir / ".pending" / "master.mp4").exists()


def test_renderer_keeps_previous_outputs_when_pending_probe_fails(
    tmp_path: Path,
) -> None:
    plan, audio, subtitles, _ = _write_inputs(tmp_path)
    final_dir = tmp_path / "final"
    final_dir.mkdir()
    master = final_dir / "master.mp4"
    clean = final_dir / "clean.mp4"
    master.write_bytes(b"old-master")
    clean.write_bytes(b"old-clean")

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "ffmpeg":
            targets = [
                Path(token)
                for token in command
                if token.endswith(".mp4") and ".pending" in token
            ]
            for target in targets:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b"invalid-new-video")
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(
            command,
            0,
            _probe_payload(width=720),
            "",
        )

    renderer = FfmpegRenderer(
        FfmpegToolchain("ffmpeg", "ffprobe"),
        command_runner=fake_run,
    )
    with pytest.raises(ValueError, match="1080x1920"):
        renderer.render_and_validate(
            visual_plan_path=plan,
            audio_path=audio,
            subtitle_path=subtitles,
            output_path=master,
            title="失败保旧",
        )

    assert master.read_bytes() == b"old-master"
    assert clean.read_bytes() == b"old-clean"
    result_pending = list((final_dir / ".pending").glob("*/master.mp4"))
    assert result_pending
    assert result_pending[0].read_bytes() == b"invalid-new-video"


def test_real_ffmpeg_renders_decodable_gradient_graphics_and_dynamic_video(
    tmp_path: Path,
) -> None:
    toolchain = discover_ffmpeg_toolchain()
    assets = tmp_path / "中文素材"
    assets.mkdir()
    image_paths = [assets / "渐变图形一.png", assets / "渐变图形二.png"]
    for image_index, image_path in enumerate(image_paths):
        image = Image.new("RGB", (640, 960))
        pixels = image.load()
        for y in range(image.height):
            for x in range(image.width):
                pixels[x, y] = (
                    (x * 255 // image.width + image_index * 30) % 256,
                    y * 255 // image.height,
                    (x + y) * 255 // (image.width + image.height),
                )
        draw = ImageDraw.Draw(image)
        draw.ellipse((100, 180, 540, 620), outline=(255, 255, 255), width=18)
        draw.rectangle((180, 680, 460, 820), fill=(240, 80, 40))
        image.save(image_path)
        extrema = image.getextrema()
        assert all(low < high for low, high in extrema)

    dynamic_video = assets / "动态测试视频.mp4"
    subprocess.run(
        [
            toolchain.ffmpeg,
            "-y",
            "-hide_banner",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x360:rate=30:duration=1.2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            dynamic_video,
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            toolchain.ffprobe,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,nb_frames",
            "-of",
            "json",
            dynamic_video,
        ],
        check=True,
        capture_output=True,
    )

    audio = tmp_path / "变化旁白.wav"
    rate = 48_000
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(rate)
        frames = bytearray()
        for index in range(rate * 3):
            sample = round(
                9000
                * (0.55 + 0.45 * math.sin(2 * math.pi * index / rate))
                * math.sin(2 * math.pi * 330 * index / rate)
            )
            frames.extend(int(sample).to_bytes(2, "little", signed=True) * 2)
        output.writeframes(frames)

    subtitles = tmp_path / "中文字幕 路径.ass"
    subtitles.write_text(
        "\n".join(
            [
                "[Script Info]",
                "ScriptType: v4.00+",
                "PlayResX: 1080",
                "PlayResY: 1920",
                "[V4+ Styles]",
                "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
                "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
                "Alignment, MarginL, MarginR, MarginV, Encoding",
                "Style: Default,Arial,72,&H00FFFFFF,&H000000FF,&H00000000,"
                "&H80000000,0,0,0,0,100,100,0,0,1,4,1,2,80,80,180,1",
                "[Events]",
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
                "MarginV, Effect, Text",
                "Dialogue: 0,0:00:00.20,0:00:02.80,Default,,0,0,0,,真实中文字幕",
            ]
        )
        + "\n",
        encoding="utf-8-sig",
    )
    plan = tmp_path / "真实视觉计划.json"
    shots = []
    kinds_and_paths = [
        ("image", image_paths[0]),
        ("video", dynamic_video),
        ("image", image_paths[1]),
        ("video", dynamic_video),
    ]
    for index, (kind, path) in enumerate(kinds_and_paths):
        shots.append(
            {
                "shot_id": f"VIS{index + 1:03d}",
                "script_segment_id": f"SEG{index + 1:03d}",
                "asset_type": kind,
                "prompt": "中文真实素材，竖屏9:16，无文字",
                "expected_path": str(path),
                "start_seconds": index * 0.75,
                "duration_seconds": 0.75,
            }
        )
    plan.write_text(
        json.dumps(
            {
                "title": "真实集成",
                "mode": "balanced",
                "total_duration_seconds": 3.0,
                "shots": shots,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    result, probes = FfmpegRenderer(toolchain).render_and_validate(
        visual_plan_path=plan,
        audio_path=audio,
        subtitle_path=subtitles,
        output_path=tmp_path / "中文输出" / "master.mp4",
        title="真实集成",
    )

    assert result.master_output_path.is_file()
    assert result.clean_output_path.is_file()
    assert result.master_output_path.stat().st_size > 10_000
    assert result.clean_output_path.stat().st_size > 10_000
    assert probes["master"].duration_seconds == pytest.approx(3.0, abs=0.15)
    assert probes["clean"].duration_seconds == pytest.approx(3.0, abs=0.15)
    assert probe_media(toolchain.ffprobe, result.master_output_path).width == 1080
