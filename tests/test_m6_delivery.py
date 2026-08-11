from __future__ import annotations

import json
import hashlib
import subprocess
from pathlib import Path

import pytest

from aicf.autopilot import Autopilot, NeedsAttention
from aicf.database import JobRepository
from aicf.engines.m6_engine import M6Pipeline, RepairEngine, TechnicalQA
from aicf.engines.render_engine import MediaProbe
from aicf.providers.tts import FfmpegToolchain, discover_ffmpeg_toolchain
from aicf.state_machine import ORDERED_STAGES, PipelineStage


def test_m6_rejects_explicit_empty_platform_selection(tmp_path: Path) -> None:
    pipeline = M6Pipeline(FfmpegToolchain("ffmpeg", "ffprobe"))

    with pytest.raises(ValueError, match="至少选择一个"):
        pipeline.run(
            master_video=tmp_path / "missing-master.mp4",
            clean_video=tmp_path / "missing-clean.mp4",
            subtitle_path=tmp_path / "missing.ass",
            timeline_path=tmp_path / "missing.json",
            script={},
            package={},
            output_dir=tmp_path / "delivery",
            expected_duration_seconds=1.0,
            selected_platforms=(),
        )


def _advance_to_rendered(repository: JobRepository, job_id: str) -> None:
    for stage in ORDERED_STAGES:
        repository.start_stage(job_id, stage)
        repository.complete_stage(job_id, stage)
        if stage == PipelineStage.RENDERED:
            return


def _media_files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    master = tmp_path / "master.mp4"
    clean = tmp_path / "clean.mp4"
    subtitle = tmp_path / "subtitles.ass"
    timeline = tmp_path / "timeline.json"
    master.write_bytes(b"master")
    clean.write_bytes(b"clean")
    subtitle.write_text(
        "[Script Info]\n"
        "PlayResX: 1080\n"
        "PlayResY: 1920\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Alignment, MarginL, MarginR, MarginV, Outline, Shadow\n"
        "Style: Default,Microsoft YaHei,64,&H00FFFFFF,&H00101010,&H80000000,-1,2,80,80,180,3,1\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        "Dialogue: 0,0:00:00.00,0:00:01.50,Default,,0,0,0,,第一句\n"
        "Dialogue: 0,0:00:01.50,0:00:03.00,Default,,0,0,0,,第二句\n",
        encoding="utf-8",
    )
    timeline.write_text(
        json.dumps(
            [
                {"start_seconds": 0.0, "end_seconds": 1.5, "text": "第一句"},
                {"start_seconds": 1.5, "end_seconds": 3.0, "text": "第二句"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return master, clean, subtitle, timeline


def _qa_runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
    if "-show_entries" in command:
        payload = {
            "format": {"duration": "3.0", "size": "1024"},
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
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
    filter_value = command[command.index("-af") + 1] if "-af" in command else ""
    if "silencedetect" in filter_value:
        return subprocess.CompletedProcess(command, 0, "", "")
    if "loudnorm" in filter_value:
        return subprocess.CompletedProcess(
            command,
            0,
            "",
            '[Parsed_loudnorm_0] {"input_i":"-15.8","input_tp":"-1.2","input_lra":"5.0"}',
        )
    return subprocess.CompletedProcess(command, 0, "", "")


def test_technical_qa_runs_all_checks_and_accepts_valid_delivery(tmp_path: Path) -> None:
    master, clean, subtitle, timeline = _media_files(tmp_path)
    qa = TechnicalQA(
        FfmpegToolchain("ffmpeg-real", "ffprobe-real"),
        command_runner=_qa_runner,
    )

    report = qa.run(
        master,
        clean,
        subtitle,
        timeline,
        expected_duration_seconds=3.0,
    )

    assert report["passed"] is True
    assert set(report["checks"]) == {
        "ffprobe",
        "blackdetect",
        "silencedetect",
        "loudness",
        "timeline",
        "subtitles",
    }
    assert report["checks"]["ffprobe"]["master"]["passed"] is True
    assert report["checks"]["ffprobe"]["clean"]["passed"] is True
    assert report["checks"]["ffprobe"]["duration_delta_seconds"] == 0.0
    assert report["checks"]["loudness"]["integrated_lufs"] == -15.8
    assert report["checks"]["subtitles"]["cue_count"] == 2
    assert report["checks"]["subtitles"]["safe_zone_passed"] is True


def test_technical_qa_uses_long_silence_threshold_above_natural_pause(
    tmp_path: Path,
) -> None:
    master, clean, subtitle, timeline = _media_files(tmp_path)
    commands: list[list[str]] = []

    def runner(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return _qa_runner(command, **kwargs)

    TechnicalQA(
        FfmpegToolchain("ffmpeg-real", "ffprobe-real"),
        command_runner=runner,
    ).run(
        master,
        clean,
        subtitle,
        timeline,
        expected_duration_seconds=3.0,
    )

    silence_command = next(
        command
        for command in commands
        if "-af" in command
        and "silencedetect" in command[command.index("-af") + 1]
    )
    assert "d=2.0" in silence_command[silence_command.index("-af") + 1]


def test_technical_qa_rejects_timeline_overlap_and_incomplete_unsafe_ass(
    tmp_path: Path,
) -> None:
    master, clean, subtitle, timeline = _media_files(tmp_path)
    timeline.write_text(
        json.dumps(
            [
                {"start_seconds": 0.0, "end_seconds": 1.7, "text": "第一句"},
                {"start_seconds": 1.5, "end_seconds": 3.0, "text": "第二句"},
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    subtitle.write_text(
        "[Script Info]\nPlayResX: 1080\nPlayResY: 1920\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Alignment, MarginL, MarginR, MarginV, Outline, Shadow\n"
        "Style: Default,Microsoft YaHei,64,&H00FFFFFF,&H00101010,&H80000000,-1,2,10,10,20,3,1\n"
        "[Events]\n"
        "Dialogue: 0,0:00:00.00,0:00:01.50,Default,,0,0,0,,第一句\n",
        encoding="utf-8",
    )

    report = TechnicalQA(
        FfmpegToolchain("ffmpeg-real", "ffprobe-real"),
        command_runner=_qa_runner,
    ).run(master, clean, subtitle, timeline, expected_duration_seconds=3.0)

    assert report["passed"] is False
    assert report["checks"]["timeline"]["overlaps"]
    assert report["checks"]["subtitles"]["event_count_matches_timeline"] is False
    assert report["checks"]["subtitles"]["safe_zone_passed"] is False


def test_technical_qa_probes_master_and_clean_and_rejects_duration_delta(
    tmp_path: Path,
) -> None:
    master, clean, subtitle, timeline = _media_files(tmp_path)
    probed: list[str] = []

    def mismatched_clean_runner(
        command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        completed = _qa_runner(command, **kwargs)
        if "-show_entries" not in command:
            return completed
        probed.append(command[-1])
        payload = json.loads(completed.stdout)
        if command[-1] == str(clean):
            payload["format"]["duration"] = "2.7"
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    report = TechnicalQA(
        FfmpegToolchain("ffmpeg-real", "ffprobe-real"),
        command_runner=mismatched_clean_runner,
    ).run(master, clean, subtitle, timeline, expected_duration_seconds=3.0)

    assert probed == [str(master), str(clean)]
    assert report["passed"] is False
    assert round(report["checks"]["ffprobe"]["duration_delta_seconds"], 3) == 0.3
    assert report["checks"]["ffprobe"]["clean"]["passed"] is False


def test_m6_pipeline_repairs_at_most_twice_and_writes_complete_delivery(
    tmp_path: Path,
) -> None:
    master, clean, subtitle, timeline = _media_files(tmp_path)
    attempts = 0

    class FlakyQA:
        def run(self, *_: object, **__: object) -> dict[str, object]:
            nonlocal attempts
            attempts += 1
            return {
                "passed": attempts >= 3,
                "checks": {"ffprobe": {"passed": True}},
                "issues": [] if attempts >= 3 else [f"第{attempts}轮问题"],
            }

    def artifact_runner(
        command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        if command[0] == "ffprobe-real":
            bitrate = (
                10_000_000
                if "youtube_shorts" in str(command[-1])
                else 8_000_000
            )
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(
                    {
                        "width": 1080,
                        "height": 1920,
                        "fps": 30.0,
                        "bit_rate": bitrate,
                    }
                ),
                "",
            )
        filter_value = command[command.index("-vf") + 1] if "-vf" in command else ""
        if "blackframe" in filter_value:
            assert "amount=0" in filter_value
            stderr = "\n".join(
                f"[Parsed_blackframe_0] frame:{index} pblack:0"
                for index in range(9)
            )
            return subprocess.CompletedProcess(command, 0, "", stderr)
        target = Path(command[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"artifact")
        return subprocess.CompletedProcess(command, 0, "", "")

    repaired: list[int] = []
    package = {
        platform: {
            "title": f"{platform}标题",
            "description": "简介",
            "hashtags": ["AI", "视频"],
        }
        for platform in ["douyin", "xiaohongshu", "youtube_shorts", "tiktok"]
    }
    pipeline = M6Pipeline(
        FfmpegToolchain("ffmpeg-real", "ffprobe-real"),
        technical_qa=FlakyQA(),
        command_runner=artifact_runner,
        repair_engine=type(
            "Repair",
            (),
            {
                "repair": lambda _self, *, issues, **_kwargs: (
                    repair(len(repaired) + 1, issues) or ["rerender"]
                )
            },
        )(),
    )
    stale_qa = tmp_path / "delivery" / "qa"
    stale_qa.mkdir(parents=True)
    (stale_qa / "technical_round_9.json").write_text("{}", encoding="utf-8")

    def repair(round_number: int, _issues: list[object]) -> None:
        repaired.append(round_number)
        master.write_bytes(master.read_bytes() + f"-repair-{round_number}".encode())

    manifest = pipeline.run(
        master_video=master,
        clean_video=clean,
        subtitle_path=subtitle,
        timeline_path=timeline,
        script={"title": "统一标题", "segments": [{"narration": "第一句"}, {"narration": "第二句"}]},
        package=package,
        output_dir=tmp_path / "delivery",
        expected_duration_seconds=3.0,
    )

    assert attempts == 3
    assert repaired == [1, 2]
    assert manifest["status"] == "READY_TO_PUBLISH"
    assert (tmp_path / "delivery" / "qa" / "repair_round_1.json").is_file()
    assert (tmp_path / "delivery" / "qa" / "repair_round_2.json").is_file()
    assert not (tmp_path / "delivery" / "qa" / "technical_round_9.json").exists()
    assert (tmp_path / "delivery" / "contact_sheet.jpg").is_file()
    assert (tmp_path / "delivery" / "cover.jpg").is_file()
    assert (tmp_path / "delivery" / "clean_cover.jpg").is_file()
    assert (tmp_path / "delivery" / "master.mp4").is_file()
    assert (tmp_path / "delivery" / "clean.mp4").is_file()
    assert (tmp_path / "delivery" / "preview_540x960.mp4").is_file()
    for platform in package:
        assert (tmp_path / "delivery" / platform / "video.mp4").is_file()
        assert (tmp_path / "delivery" / platform / "publish.md").is_file()
        assert "#AI #视频" in (
            tmp_path / "delivery" / platform / "publish.md"
        ).read_text(encoding="utf-8")
    assert not (tmp_path / "delivery" / "bilibili").exists()
    assert not (tmp_path / "delivery" / "wechat_channels").exists()
    persisted = json.loads(
        (tmp_path / "delivery" / "publish_manifest.json").read_text(encoding="utf-8")
    )
    assert persisted["repair_rounds"] == 2
    assert persisted["repair_status"] == "AUTO_REPAIRED"
    assert persisted["contact_sheet_frame_count"] == 9
    assert len(persisted["platforms"]) == 4
    listed = persisted["files"]
    actual = {
        path.relative_to(tmp_path / "delivery").as_posix()
        for path in (tmp_path / "delivery").rglob("*")
        if path.is_file() and path.name != "publish_manifest.json"
    }
    assert set(listed) == actual
    for relative, metadata in listed.items():
        payload = (tmp_path / "delivery" / relative).read_bytes()
        assert metadata["sha256"] == hashlib.sha256(payload).hexdigest()
        assert metadata["size_bytes"] == len(payload)


def test_verify_delivery_rejects_missing_hash_mismatch_and_invalid_media(
    tmp_path: Path,
) -> None:
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    media = delivery / "video.mp4"
    media.write_bytes(b"valid")
    missing = delivery / "copy.md"
    missing.write_text("copy", encoding="utf-8")
    manifest = {
        "status": "READY_TO_PUBLISH",
        "expected_duration_seconds": 3.0,
        "files": {
            "video.mp4": {
                "sha256": hashlib.sha256(media.read_bytes()).hexdigest(),
                "size_bytes": media.stat().st_size,
                "media": True,
            },
            "copy.md": {
                "sha256": hashlib.sha256(missing.read_bytes()).hexdigest(),
                "size_bytes": missing.stat().st_size,
                "media": False,
            },
        },
    }
    (delivery / "publish_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    missing.unlink()

    class RejectingProbe:
        def assert_vertical_delivery(self, _duration: float) -> None:
            raise ValueError("媒体损坏")

    pipeline = M6Pipeline(
        FfmpegToolchain("ffmpeg", "ffprobe"),
        media_probe=lambda *_: RejectingProbe(),
    )
    issues = pipeline.verify_delivery(delivery)

    assert any("copy.md 不存在" in issue for issue in issues)
    assert any("video.mp4 媒体验证失败" in issue for issue in issues)


def test_verify_delivery_enforces_required_and_exact_manifest_file_set(
    tmp_path: Path,
) -> None:
    delivery = tmp_path / "delivery"
    required = {
        "master.mp4",
        "clean.mp4",
        "preview_540x960.mp4",
        "cover.jpg",
        "clean_cover.jpg",
        "contact_sheet.jpg",
        "qa/content_qa.json",
        "qa/technical_round_0.json",
    }
    for platform in ("douyin", "xiaohongshu", "youtube_shorts", "tiktok"):
        required.add(f"{platform}/video.mp4")
        required.add(f"{platform}/publish.md")
    for relative in required:
        path = delivery / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"payload:{relative}".encode())
    files = {
        relative: {
            "sha256": hashlib.sha256((delivery / relative).read_bytes()).hexdigest(),
            "size_bytes": (delivery / relative).stat().st_size,
            "media": relative.endswith(".mp4"),
        }
        for relative in sorted(required)
    }
    manifest = {
        "status": "READY_TO_PUBLISH",
        "expected_duration_seconds": 3.0,
        "repair_rounds": 0,
        "technical_qa": "qa/technical_round_0.json",
        "content_qa": "qa/content_qa.json",
        "files": files,
    }
    manifest_path = delivery / "publish_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    class AcceptingProbe:
        def assert_vertical_delivery(self, _duration: float) -> None:
            return None

    pipeline = M6Pipeline(
        FfmpegToolchain("ffmpeg", "ffprobe"),
        media_probe=lambda *_: AcceptingProbe(),
    )
    assert pipeline.verify_delivery(delivery) == []

    del manifest["files"]["master.mp4"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    issues = pipeline.verify_delivery(delivery)
    assert any("必需交付文件未列入 manifest: master.mp4" in issue for issue in issues)
    assert any("实际文件集合与 manifest 不一致" in issue for issue in issues)

    manifest["files"] = files
    extra = delivery / "unlisted.txt"
    extra.write_text("bypass", encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    issues = pipeline.verify_delivery(delivery)
    assert any("实际文件集合与 manifest 不一致" in issue for issue in issues)


def test_verify_landscape_delivery_accepts_960x540_preview(
    tmp_path: Path,
) -> None:
    delivery = tmp_path / "delivery"
    required = {
        "master.mp4",
        "clean.mp4",
        "preview_960x540.mp4",
        "cover.jpg",
        "clean_cover.jpg",
        "contact_sheet.jpg",
        "qa/content_qa.json",
        "qa/technical_round_0.json",
        "youtube/video.mp4",
        "youtube/publish.md",
    }
    for relative in required:
        path = delivery / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"payload:{relative}".encode())
    files = {
        relative: {
            "sha256": hashlib.sha256((delivery / relative).read_bytes()).hexdigest(),
            "size_bytes": (delivery / relative).stat().st_size,
            "media": relative.endswith(".mp4"),
        }
        for relative in sorted(required)
    }
    (delivery / "publish_manifest.json").write_text(
        json.dumps(
            {
                "status": "READY_TO_PUBLISH",
                "expected_duration_seconds": 55.872,
                "repair_rounds": 0,
                "orientation": "landscape",
                "platforms": {"youtube": {}},
                "files": files,
            }
        ),
        encoding="utf-8",
    )

    def probe(_ffprobe: str, path: Path, _runner: object) -> MediaProbe:
        width, height = (960, 540) if path.name.startswith("preview_") else (1920, 1080)
        return MediaProbe(
            duration_seconds=55.872,
            size_bytes=path.stat().st_size,
            video_codec="h264",
            width=width,
            height=height,
            pixel_format="yuv420p",
            fps=30.0,
            audio_codec="aac",
            sample_rate=48_000,
            channels=2,
        )

    pipeline = M6Pipeline(
        FfmpegToolchain("ffmpeg", "ffprobe"),
        media_probe=probe,
    )

    assert pipeline.verify_delivery(delivery) == []


def test_m6_pipeline_fails_after_two_noop_repairs_without_packaging(
    tmp_path: Path,
) -> None:
    master, clean, subtitle, timeline = _media_files(tmp_path)

    class AlwaysFailingQA:
        def run(self, *_: object, **__: object) -> dict[str, object]:
            return {
                "passed": False,
                "checks": {"ffprobe": {"passed": False}},
                "issues": ["ffprobe"],
            }

    repaired: list[int] = []
    package = {
        platform: {"title": "标题", "description": "简介", "hashtags": []}
        for platform in ["douyin", "xiaohongshu", "youtube_shorts", "tiktok"]
    }
    manifest = M6Pipeline(
        FfmpegToolchain("ffmpeg-real", "ffprobe-real"),
        technical_qa=AlwaysFailingQA(),
        max_repair_rounds=2,
        repair_engine=type(
            "NoRepair",
            (),
            {"repair": lambda _self, **_kwargs: []},
        )(),
    ).run(
        master_video=master,
        clean_video=clean,
        subtitle_path=subtitle,
        timeline_path=timeline,
        script={"title": "标题"},
        package=package,
        output_dir=tmp_path / "delivery",
        expected_duration_seconds=3.0,
    )

    assert repaired == []
    assert manifest["status"] == "FAILED"
    assert manifest["repair_status"] == "FAILED"
    assert manifest["repair_rounds"] == 0
    assert manifest["repair_attempts"] == []
    assert not (tmp_path / "delivery" / "contact_sheet.jpg").exists()


def test_failed_delivery_build_preserves_previous_ready_directory(
    tmp_path: Path,
) -> None:
    master, clean, subtitle, timeline = _media_files(tmp_path)
    delivery = tmp_path / "delivery"
    delivery.mkdir()
    (delivery / "publish_manifest.json").write_text(
        '{"status":"READY_TO_PUBLISH","version":"old"}',
        encoding="utf-8",
    )

    class FailingQA:
        def run(self, *_: object, **__: object) -> dict[str, object]:
            return {"passed": False, "checks": {}, "issues": ["ffprobe"]}

    result = M6Pipeline(
        FfmpegToolchain("ffmpeg", "ffprobe"),
        technical_qa=FailingQA(),
    ).run(
        master_video=master,
        clean_video=clean,
        subtitle_path=subtitle,
        timeline_path=timeline,
        script={"title": "标题"},
        package={},
        output_dir=delivery,
        expected_duration_seconds=3.0,
    )

    persisted = json.loads(
        (delivery / "publish_manifest.json").read_text(encoding="utf-8")
    )
    assert result["status"] == "FAILED"
    assert persisted == {"status": "READY_TO_PUBLISH", "version": "old"}


def test_repair_engine_dispatches_deterministic_actions(tmp_path: Path) -> None:
    master, clean, subtitle, timeline = _media_files(tmp_path)
    audio = tmp_path / "voiceover.wav"
    visual_plan = tmp_path / "visual_plan.json"
    audio.write_bytes(b"audio")
    visual_plan.write_text("{}", encoding="utf-8")
    commands: list[list[str]] = []
    renders: list[dict[str, object]] = []

    def runner(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        Path(command[-1]).write_bytes(b"repaired")
        return subprocess.CompletedProcess(command, 0, "", "")

    class Renderer:
        def render_and_validate(self, **kwargs: object) -> None:
            renders.append(kwargs)
            Path(kwargs["output_path"]).write_bytes(b"rerendered")
            Path(kwargs["output_path"]).with_name("clean.mp4").write_bytes(b"clean")

    engine = RepairEngine(
        FfmpegToolchain("ffmpeg", "ffprobe"),
        renderer=Renderer(),
        command_runner=runner,
    )
    actions = engine.repair(
        issues=["silencedetect", "subtitles", "ffprobe"],
        report={"checks": {}},
        master=master,
        clean=clean,
        subtitles=subtitle,
        timeline=timeline,
        context={
            "audio_path": audio,
            "visual_plan_path": visual_plan,
            "title": "标题",
        },
    )

    assert actions == ["remix_audio", "reburn_subtitles", "rerender_m5"]
    assert any("-map" in command and str(audio) in command for command in commands)
    assert len(renders) == 1


def test_real_ffmpeg_remix_and_trim_keep_mp4_container_suffix(
    tmp_path: Path,
) -> None:
    toolchain = discover_ffmpeg_toolchain()
    master = tmp_path / "master.mp4"
    clean = tmp_path / "clean.mp4"
    audio = tmp_path / "voiceover.wav"
    subprocess.run(
        [
            toolchain.ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1080x1920:rate=30:duration=1.2",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(master),
        ],
        check=True,
        capture_output=True,
    )
    clean.write_bytes(master.read_bytes())
    subprocess.run(
        [
            toolchain.ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1.2",
            "-c:a",
            "pcm_s16le",
            str(audio),
        ],
        check=True,
        capture_output=True,
    )
    commands: list[list[str]] = []

    def real_runner(command: list[str], **kwargs: object):
        commands.append(command)
        return subprocess.run(command, **kwargs)

    engine = RepairEngine(
        toolchain,
        renderer=object(),
        command_runner=real_runner,
    )
    engine._remix_audio(master, clean, audio)
    trimmed = engine._trim_boundary_black(
        {
            "checks": {
                "blackdetect": {
                    "segments": [{"start": 0.0, "end": 0.1}]
                },
                "ffprobe": {
                    "master": {
                        "probe": {"duration_seconds": 1.2}
                    }
                },
            }
        },
        master,
        clean,
    )

    assert trimmed is True
    generated_outputs = [Path(command[-1]) for command in commands]
    assert generated_outputs
    assert all(path.suffix == ".mp4" for path in generated_outputs)
    for video in (master, clean):
        probe = subprocess.run(
            [
                toolchain.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=format_name,duration",
                "-of",
                "json",
                str(video),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(probe.stdout)
        assert "mp4" in payload["format"]["format_name"]
        assert float(payload["format"]["duration"]) > 0


def test_m6_pipeline_rejects_black_contact_sheet_frames(tmp_path: Path) -> None:
    master, clean, subtitle, timeline = _media_files(tmp_path)

    class PassingQA:
        def run(self, *_: object, **__: object) -> dict[str, object]:
            return {"passed": True, "checks": {}, "issues": []}

    def black_frame_runner(
        command: list[str], **_: object
    ) -> subprocess.CompletedProcess[str]:
        filter_value = command[command.index("-vf") + 1] if "-vf" in command else ""
        if "blackframe" in filter_value:
            stderr = "\n".join(
                f"[Parsed_blackframe_0] frame:{index} pblack:100"
                for index in range(9)
            )
            return subprocess.CompletedProcess(command, 0, "", stderr)
        return subprocess.CompletedProcess(command, 0, "", "")

    package = {
        platform: {"title": "标题", "description": "简介", "hashtags": []}
        for platform in ["douyin", "xiaohongshu", "youtube_shorts", "tiktok"]
    }
    pipeline = M6Pipeline(
        FfmpegToolchain("ffmpeg-real", "ffprobe-real"),
        technical_qa=PassingQA(),
        command_runner=black_frame_runner,
    )

    try:
        pipeline.run(
            master_video=master,
            clean_video=clean,
            subtitle_path=subtitle,
            timeline_path=timeline,
            script={"title": "标题"},
            package=package,
            output_dir=tmp_path / "delivery",
            expected_duration_seconds=3.0,
        )
    except ValueError as error:
        assert "黑帧" in str(error)
    else:
        raise AssertionError("黑色 contact sheet 帧必须阻止交付")


def test_autopilot_marks_missing_external_capability_and_records_recovery(
    tmp_path: Path,
) -> None:
    repo = JobRepository(tmp_path / "content.db")
    output = tmp_path / "outputs" / "JOB-M6"
    repo.create_job("JOB-M6", output)
    _advance_to_rendered(repo, "JOB-M6")
    (output / "final").mkdir()
    (output / "audio").mkdir()
    (output / "final" / "master.mp4").write_bytes(b"master")
    (output / "final" / "clean.mp4").write_bytes(b"clean")
    (output / "audio" / "subtitles.ass").write_text("[Events]\n", encoding="utf-8")
    (output / "audio" / "timeline.json").write_text(
        '[{"end_seconds": 3.0}]',
        encoding="utf-8",
    )
    (output / "script.json").write_text('{"title": "标题"}', encoding="utf-8")
    (output / "package.json").write_text("{}", encoding="utf-8")

    class MissingPipeline:
        def run(self, **_: object) -> dict[str, object]:
            raise NeedsAttention(
                "缺少 FFmpeg 外部能力",
                "powershell -File scripts/doctor.ps1",
            )

    result = Autopilot(repo, MissingPipeline()).run("JOB-M6")

    status = repo.get_job("JOB-M6")
    assert result["status"] == "FAILED_NEEDS_ATTENTION"
    assert status.current_stage == PipelineStage.FAILED_NEEDS_ATTENTION
    assert status.failed_stage == PipelineStage.QA_CHECKED
    assert (
        status.stages[PipelineStage.QA_CHECKED.value]["next_resume_command"]
        == "powershell -File scripts/doctor.ps1"
    )


def test_autopilot_persists_unexpected_pipeline_failure_for_recovery(
    tmp_path: Path,
) -> None:
    repo = JobRepository(tmp_path / "content.db")
    output = tmp_path / "outputs" / "JOB-M6-ERROR"
    repo.create_job("JOB-M6-ERROR", output)
    _advance_to_rendered(repo, "JOB-M6-ERROR")
    (output / "final").mkdir()
    (output / "audio").mkdir()
    (output / "final" / "master.mp4").write_bytes(b"master")
    (output / "final" / "clean.mp4").write_bytes(b"clean")
    (output / "audio" / "subtitles.ass").write_text("[Events]\n", encoding="utf-8")
    (output / "audio" / "timeline.json").write_text(
        '[{"end_seconds": 3.0}]',
        encoding="utf-8",
    )
    (output / "script.json").write_text('{"title": "标题"}', encoding="utf-8")
    (output / "package.json").write_text("{}", encoding="utf-8")

    class BrokenPipeline:
        def run(self, **_: object) -> dict[str, object]:
            raise subprocess.CalledProcessError(1, ["ffmpeg"], stderr="编码失败")

    result = Autopilot(repo, BrokenPipeline()).run("JOB-M6-ERROR")

    status = repo.get_job("JOB-M6-ERROR")
    assert result["status"] == "FAILED_RETRYABLE"
    assert "编码失败" in result["reason"]
    assert status.current_stage == PipelineStage.FAILED_RETRYABLE
    assert status.failed_stage == PipelineStage.QA_CHECKED
    assert status.next_resume_command == "python -m aicf resume --job JOB-M6-ERROR"


def test_run_autopilot_script_invokes_real_cli() -> None:
    script = Path(__file__).parents[1] / "run_autopilot.ps1"
    text = script.read_text(encoding="utf-8-sig")

    assert "-m aicf autopilot" in text
    assert "当前未实现" not in text
