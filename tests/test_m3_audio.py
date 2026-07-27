from __future__ import annotations

import json
import subprocess
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from aicf.engines.narration_engine import (
    NeedsScriptDurationRevision,
    NarrationPipeline,
    split_chinese_sentences,
)
from aicf.engines.subtitle_engine import build_ass
from aicf.cli import main
from aicf.doctor import Doctor
from aicf.providers.tts import (
    EdgeTtsProvider,
    FfmpegToolchain,
    SapiTtsProvider,
    TtsRequest,
    discover_ffmpeg_toolchain,
)


def _write_wav(
    path: Path,
    duration: float = 0.1,
    sample_rate: int = 48_000,
    channels: int = 2,
) -> None:
    frames = round(duration * sample_rate)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\0\0" * channels * frames)


def test_toolchain_discovery_skips_trimmed_path_pair_and_selects_full_pair(
    tmp_path: Path,
) -> None:
    trimmed = tmp_path / "trimmed"
    full = tmp_path / "full"
    for directory in (trimmed, full):
        directory.mkdir()
        (directory / "ffmpeg.exe").touch()
        (directory / "ffprobe.exe").touch()

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        executable = Path(command[0])
        if executable.parent == trimmed:
            output = "configuration: --disable-everything --enable-ffmpeg --enable-ffprobe"
        else:
            output = "configuration: --enable-gpl --enable-libmp3lame --enable-libopus"
        return subprocess.CompletedProcess(command, 0, output, "")

    selected = discover_ffmpeg_toolchain(
        candidates=[trimmed / "ffmpeg.exe", full / "ffmpeg.exe"],
        command_runner=fake_run,
    )

    assert selected == FfmpegToolchain(
        ffmpeg=str(full / "ffmpeg.exe"),
        ffprobe=str(full / "ffprobe.exe"),
    )


def test_doctor_reports_the_same_full_toolchain_pair() -> None:
    report = Doctor(audio_ffmpeg="C:/full/ffmpeg.exe").run()

    assert Path(report.checks["ffmpeg"].detail) == Path("C:/full/ffmpeg.exe")
    assert Path(report.checks["ffprobe"].detail) == Path("C:/full/ffprobe.exe")


def test_edge_and_sapi_outputs_are_converted_to_48k_stereo_pcm(tmp_path: Path) -> None:
    edge_commands: list[list[str]] = []
    sapi_commands: list[list[str]] = []

    class FakeCommunicate:
        def __init__(self, text: str, voice: str) -> None:
            pass

        async def save(self, path: str) -> None:
            Path(path).write_bytes(b"mp3")

    edge_output = tmp_path / "edge.wav"

    def edge_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        edge_commands.append(command)
        _write_wav(edge_output)
        return subprocess.CompletedProcess(command, 0, "", "")

    EdgeTtsProvider(
        communicate_factory=FakeCommunicate,
        command_runner=edge_run,
        ffmpeg_executable="full-ffmpeg",
    ).synthesize(TtsRequest("Edge", edge_output))

    sapi_output = tmp_path / "sapi.wav"

    def sapi_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        sapi_commands.append(command)
        if command[0] == "full-ffmpeg":
            _write_wav(sapi_output)
        return subprocess.CompletedProcess(command, 0, "", "")

    SapiTtsProvider(
        command_runner=sapi_run,
        ffmpeg_executable="full-ffmpeg",
    ).synthesize(TtsRequest("SAPI", sapi_output))

    for command in (edge_commands[-1], sapi_commands[-1]):
        assert command[0] == "full-ffmpeg"
        assert command[command.index("-ar") + 1] == "48000"
        assert command[command.index("-ac") + 1] == "2"
        assert command[command.index("-c:a") + 1] == "pcm_s16le"
    assert sapi_commands[0][0].lower().endswith("powershell.exe")


def test_chinese_sentence_split_preserves_punctuation_and_script_segment() -> None:
    assert split_chinese_sentences("第一句。第二句！还有半句，收尾？") == [
        ("第一句。", 0.35),
        ("第二句！", 0.35),
        ("还有半句，", 0.18),
        ("收尾？", 0.35),
    ]


def test_ass_uses_real_timeline_timestamps() -> None:
    content = build_ass(
        [{"text": "第一句", "start_seconds": 0.18, "end_seconds": 1.43}]
    )

    assert "Dialogue: 0,0:00:00.18,0:00:01.43" in content
    assert "第一句" in content


def test_batch_synthesize_creates_segments_voiceover_timeline_and_subtitles(
    tmp_path: Path,
) -> None:
    synthesized_texts: list[str] = []

    class FakeService:
        def synthesize(self, text: str, output_path: Path):
            synthesized_texts.append(text)
            _write_wav(output_path, duration=0.75)
            return type("Result", (), {"provider": "fake", "degraded": False})()

    ffmpeg_commands: list[list[str]] = []

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        ffmpeg_commands.append(command)
        if command[0] == "full-ffprobe":
            return subprocess.CompletedProcess(command, 0, "1.45\n", "")
        if "-af" in command:
            source = Path(command[command.index("-i") + 1])
            target = Path(command[-1])
            target.write_bytes(source.read_bytes())
        return subprocess.CompletedProcess(
            command,
            0,
            "",
            '{"input_i":"-20.0","input_tp":"-4.0","input_lra":"1.0",'
            '"input_thresh":"-30.0","target_offset":"0.0"}',
        )

    pipeline = NarrationPipeline(
        service=FakeService(),
        toolchain=FfmpegToolchain("full-ffmpeg", "full-ffprobe"),
        command_runner=fake_run,
    )
    result = pipeline.batch_synthesize(
        {
            "segments": [
                {"segment_id": "SEG001", "narration": "第一句。第二句！"},
                {"segment_id": "SEG002", "narration": "最后一句。"},
            ]
        },
        tmp_path,
        target_duration_seconds=1.0,
        min_duration_seconds=1.0,
        max_duration_seconds=2.0,
    )

    assert synthesized_texts == ["第一句。第二句！最后一句。"]
    assert len(list((tmp_path / "segments").glob("*.wav"))) == 1
    assert result.voiceover_path == tmp_path / "voiceover.wav"
    assert result.voiceover_path.exists()
    timeline = json.loads((tmp_path / "timeline.json").read_text(encoding="utf-8"))
    assert [item["script_segment_id"] for item in timeline] == [
        "SEG001",
        "SEG001",
        "SEG002",
    ]
    assert timeline[0]["duration_seconds"] > 0
    assert timeline[1]["start_seconds"] == pytest.approx(timeline[0]["end_seconds"])
    assert timeline[-1]["end_seconds"] == pytest.approx(1.45)
    assert (tmp_path / "subtitles.srt").exists()
    assert (tmp_path / "subtitles.ass").exists()
    assert any(
        "loudnorm=I=-16:TP=-1.5" in command[command.index("-af") + 1]
        for command in ffmpeg_commands
        if "-af" in command
    )


def test_batch_synthesize_compresses_to_target_and_rescales_timeline(
    tmp_path: Path,
) -> None:
    class FakeService:
        def synthesize(self, text: str, output_path: Path):
            _write_wav(output_path, duration=1.0)
            return SimpleNamespace(provider="fake", degraded=False)

    ffmpeg_commands: list[list[str]] = []
    probe_durations = iter(("8.0", "6.0"))

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        ffmpeg_commands.append(command)
        if command[0] == "full-ffprobe":
            return subprocess.CompletedProcess(command, 0, next(probe_durations), "")
        source = Path(command[command.index("-i") + 1])
        target = Path(command[-1])
        target.write_bytes(source.read_bytes())
        return subprocess.CompletedProcess(
            command,
            0,
            "",
            '{"input_i":"-20","input_tp":"-4","input_lra":"1",'
            '"input_thresh":"-30","target_offset":"0"}',
        )

    result = NarrationPipeline(
        service=FakeService(),
        toolchain=FfmpegToolchain("full-ffmpeg", "full-ffprobe"),
        command_runner=fake_run,
    ).batch_synthesize(
        {"segments": [{"segment_id": "S1", "narration": "第一句。第二句。"}]},
        tmp_path,
        target_duration_seconds=6.0,
        min_duration_seconds=5.0,
        max_duration_seconds=7.0,
    )

    atempo_command = next(
        command
        for command in ffmpeg_commands
        if "-af" in command and command[command.index("-af") + 1].startswith("atempo=")
    )
    assert atempo_command[atempo_command.index("-af") + 1] == "atempo=1.333333"
    assert result.voiceover_path.exists()
    timeline = json.loads(result.timeline_path.read_text(encoding="utf-8"))
    assert timeline[0]["duration_seconds"] == pytest.approx(3.0)
    assert timeline[1]["start_seconds"] == pytest.approx(3.0)
    assert timeline[1]["end_seconds"] == pytest.approx(6.0)
    assert "00:00:03,000" in result.srt_path.read_text(encoding="utf-8-sig")


def test_batch_synthesize_uses_max_when_target_would_exceed_atempo_limit(
    tmp_path: Path,
) -> None:
    class FakeService:
        def synthesize(self, text: str, output_path: Path):
            _write_wav(output_path)
            return SimpleNamespace(provider="fake", degraded=False)

    filters: list[str] = []
    probe_durations = iter(("90.0", "75.0"))

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "full-ffprobe":
            return subprocess.CompletedProcess(command, 0, next(probe_durations), "")
        source = Path(command[command.index("-i") + 1])
        target = Path(command[-1])
        target.write_bytes(source.read_bytes())
        if "-af" in command:
            filters.append(command[command.index("-af") + 1])
        return subprocess.CompletedProcess(
            command,
            0,
            "",
            '{"input_i":"-20","input_tp":"-4","input_lra":"1",'
            '"input_thresh":"-30","target_offset":"0"}',
        )

    NarrationPipeline(
        service=FakeService(),
        toolchain=FfmpegToolchain("full-ffmpeg", "full-ffprobe"),
        command_runner=fake_run,
    ).batch_synthesize(
        {"segments": [{"segment_id": "S1", "narration": "测试。"}]},
        tmp_path,
        target_duration_seconds=60.0,
        min_duration_seconds=45.0,
        max_duration_seconds=75.0,
    )

    assert "atempo=1.200000" in filters


@pytest.mark.parametrize(
    ("probed_duration", "message"),
    [("44.9", "低于最小时长"), ("102.0", "atempo")],
)
def test_batch_synthesize_requires_script_revision_instead_of_faking_duration(
    tmp_path: Path,
    probed_duration: str,
    message: str,
) -> None:
    class FakeService:
        def synthesize(self, text: str, output_path: Path):
            _write_wav(output_path)
            return SimpleNamespace(provider="fake", degraded=False)

    def fake_run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "full-ffprobe":
            return subprocess.CompletedProcess(command, 0, probed_duration, "")
        source = Path(command[command.index("-i") + 1])
        Path(command[-1]).write_bytes(source.read_bytes())
        return subprocess.CompletedProcess(
            command,
            0,
            "",
            '{"input_i":"-20","input_tp":"-4","input_lra":"1",'
            '"input_thresh":"-30","target_offset":"0"}',
        )

    with pytest.raises(NeedsScriptDurationRevision, match=message):
        NarrationPipeline(
            service=FakeService(),
            toolchain=FfmpegToolchain("full-ffmpeg", "full-ffprobe"),
            command_runner=fake_run,
        ).batch_synthesize(
            {"segments": [{"segment_id": "S1", "narration": "测试。"}]},
            tmp_path,
            target_duration_seconds=60.0,
            min_duration_seconds=45.0,
            max_duration_seconds=75.0,
        )


@pytest.mark.parametrize(
    ("actual", "expected_action", "expected_ratio"),
    [(30.0, "expand", 2.0), (100.0, "compress", 0.6)],
)
def test_duration_revision_error_exposes_bounds_target_and_ratio_advice(
    actual: float,
    expected_action: str,
    expected_ratio: float,
) -> None:
    error = NeedsScriptDurationRevision(
        actual_duration_seconds=actual,
        min_duration_seconds=45.0,
        max_duration_seconds=75.0,
        target_duration_seconds=60.0,
    )

    assert error.actual_duration_seconds == actual
    assert error.min_duration_seconds == 45.0
    assert error.max_duration_seconds == 75.0
    assert error.target_duration_seconds == 60.0
    assert error.suggested_action == expected_action
    assert error.target_ratio == pytest.approx(expected_ratio)
    assert expected_action in error.revision_instruction


def test_batch_synthesize_cli_reads_script_json_and_reports_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script_path = tmp_path / "script.json"
    script_path.write_text(
        json.dumps(
            {"segments": [{"segment_id": "SEG001", "narration": "测试。"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    output_dir = tmp_path / "audio"
    calls: list[tuple[dict[str, object], Path, dict[str, float]]] = []

    class FakePipeline:
        def batch_synthesize(
            self,
            script: dict[str, object],
            output: Path,
            **durations: float,
        ):
            calls.append((script, output, durations))
            return SimpleNamespace(
                voiceover_path=output / "voiceover.wav",
                timeline_path=output / "timeline.json",
                srt_path=output / "subtitles.srt",
                ass_path=output / "subtitles.ass",
                segment_paths=(output / "segments" / "AUD001_SEG001.wav",),
            )

    monkeypatch.setattr("aicf.cli.build_narration_pipeline", lambda: FakePipeline())
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "content_direction.yaml").write_text(
        "direction: 测试\nvideo:\n"
        "  target_duration_seconds: 60\n"
        "  min_duration_seconds: 45\n"
        "  max_duration_seconds: 75\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))

    exit_code = main(
        [
            "batch-synthesize",
            "--script",
            str(script_path),
            "--output-dir",
            str(output_dir),
        ]
    )

    assert exit_code == 0
    assert calls[0][0]["segments"][0]["narration"] == "测试。"
    assert calls[0][1] == output_dir
    assert calls[0][2] == {
        "target_duration_seconds": 60,
        "min_duration_seconds": 45,
        "max_duration_seconds": 75,
    }
    assert "voiceover.wav" in capsys.readouterr().out
