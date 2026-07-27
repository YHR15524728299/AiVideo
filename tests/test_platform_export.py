import json
import subprocess
from pathlib import Path

import pytest

from aicf.platform_export import PLATFORM_TEMPLATES, PlatformExporter
from aicf.providers.tts import FfmpegToolchain


def _integration_ffmpeg() -> tuple[str, str] | None:
    candidates = subprocess.run(
        ["where.exe", "ffmpeg"],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    for candidate in candidates:
        demuxers = subprocess.run(
            [candidate, "-hide_banner", "-demuxers"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        ffprobe = str(Path(candidate).with_name("ffprobe.exe"))
        if "lavfi" in demuxers.stdout and Path(ffprobe).is_file():
            return candidate, ffprobe
    return None


def test_exports_only_selected_platforms_and_writes_selected_copy(
    tmp_path: Path,
) -> None:
    master = tmp_path / "master.mp4"
    master.write_bytes(b"master-video")
    output = tmp_path / "delivery"
    package = {
        "douyin": {"title": "抖音", "description": "简介", "hashtags": ["AI"]},
        "tiktok": {"title": "TikTok", "description": "copy", "hashtags": ["AI"]},
        "youtube": {"title": "YouTube", "description": "desc", "hashtags": ["AI"]},
    }

    def matching_probe(
        command: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                {
                    "width": 1080,
                    "height": 1920,
                    "fps": 30.0,
                    "bit_rate": 8_000_000,
                }
            ),
            "",
        )

    entries = PlatformExporter(
        FfmpegToolchain("ffmpeg", "ffprobe"),
        command_runner=matching_probe,
    ).export(
        master,
        output,
        package,
        selected_platforms=("tiktok",),
    )

    assert set(entries) == {"tiktok"}
    assert (output / "tiktok" / PLATFORM_TEMPLATES["tiktok"].filename).read_bytes() == b"master-video"
    assert "TikTok" in (output / "tiktok" / "publish.md").read_text(encoding="utf-8")
    assert not (output / "douyin").exists()
    assert not (output / "youtube").exists()


def test_platform_templates_define_delivery_contracts() -> None:
    """竖屏平台都是 1080x1920，YouTube 是 1920x1080 横屏。"""
    assert set(PLATFORM_TEMPLATES) == {
        "douyin",
        "xiaohongshu",
        "tiktok",
        "youtube_shorts",
        "youtube",
    }
    # 竖屏平台（Short 类）
    vertical_platforms = {"douyin", "xiaohongshu", "tiktok", "youtube_shorts"}
    for p in vertical_platforms:
        t = PLATFORM_TEMPLATES[p]
        assert t.width == 1080
        assert t.height == 1920
        assert t.fps == 30
        assert t.video_bitrate
    # YouTube 横屏
    yt = PLATFORM_TEMPLATES["youtube"]
    assert yt.width == 1920
    assert yt.height == 1080
    assert yt.fps == 30
    assert yt.video_bitrate == "12M"


def test_youtube_landscape_transcoding(tmp_path: Path) -> None:
    """测试 YouTube 横屏转码使用正确的分辨率。"""
    master = tmp_path / "master.mp4"
    master.write_bytes(b"master")
    probes = iter(
        [
            {"width": 1080, "height": 1920, "fps": 30.0, "bit_rate": 8_000_000},
            {"width": 1920, "height": 1080, "fps": 30.0, "bit_rate": 12_000_000},
        ]
    )
    commands: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(next(probes)),
                "",
            )
        Path(command[-1]).write_bytes(b"transcoded-landscape")
        return subprocess.CompletedProcess(command, 0, "", "")

    PlatformExporter(
        FfmpegToolchain("ffmpeg", "ffprobe"),
        command_runner=run,
    ).export(
        master,
        tmp_path / "delivery",
        {"youtube": {"title": "YT", "description": "desc"}},
        selected_platforms=("youtube",),
    )

    ffmpeg = next(command for command in commands if command[0] == "ffmpeg")
    assert ffmpeg[ffmpeg.index("-s") + 1] == "1920x1080"
    assert ffmpeg[ffmpeg.index("-b:v") + 1] == "12M"


def test_probes_master_transcodes_mismatch_and_validates_export(
    tmp_path: Path,
) -> None:
    master = tmp_path / "master.mp4"
    master.write_bytes(b"master")
    probes = iter(
        [
            {"width": 720, "height": 1280, "fps": 25.0, "bit_rate": 4_000_000},
            {"width": 1080, "height": 1920, "fps": 30.0, "bit_rate": 8_000_000},
        ]
    )
    commands: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[0] == "ffprobe":
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps(next(probes)),
                "",
            )
        Path(command[-1]).write_bytes(b"transcoded")
        return subprocess.CompletedProcess(command, 0, "", "")

    PlatformExporter(
        FfmpegToolchain("ffmpeg", "ffprobe"),
        command_runner=run,
    ).export(
        master,
        tmp_path / "delivery",
        {"douyin": {}},
        selected_platforms=("douyin",),
    )

    ffmpeg = next(command for command in commands if command[0] == "ffmpeg")
    assert ffmpeg[ffmpeg.index("-s") + 1] == "1080x1920"
    assert ffmpeg[ffmpeg.index("-r") + 1] == "30"
    assert ffmpeg[ffmpeg.index("-b:v") + 1] == "8M"
    assert len([command for command in commands if command[0] == "ffprobe"]) == 2


def test_bitrate_contract_accepts_normal_encoder_variance() -> None:
    template = PLATFORM_TEMPLATES["douyin"]

    assert PlatformExporter._matches(
        {"width": 1080, "height": 1920, "fps": 30.0, "bit_rate": 5_000_000},
        template,
    )
    assert not PlatformExporter._matches(
        {"width": 1080, "height": 1920, "fps": 30.0, "bit_rate": 100_000},
        template,
    )
    assert not PlatformExporter._matches(
        {"width": 1080, "height": 1920, "fps": 30.0, "bit_rate": 30_000_000},
        template,
    )


def test_same_platform_specs_share_one_transcode_and_hash_streams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    master = tmp_path / "master.mp4"
    master.write_bytes(b"master")
    commands: list[list[str]] = []

    def run(command: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[0] == "ffprobe":
            path = Path(command[-1])
            probe = (
                {"width": 720, "height": 1280, "fps": 25.0, "bit_rate": 2_000_000}
                if path == master
                else {
                    "width": 1080,
                    "height": 1920,
                    "fps": 30.0,
                    "bit_rate": 6_000_000,
                }
            )
            return subprocess.CompletedProcess(command, 0, json.dumps(probe), "")
        Path(command[-1]).write_bytes(b"shared-transcode")
        return subprocess.CompletedProcess(command, 0, "", "")

    def forbid_read_bytes(_path: Path) -> bytes:
        raise AssertionError("导出 hash 必须流式读取，不得 Path.read_bytes()")

    monkeypatch.setattr(Path, "read_bytes", forbid_read_bytes)
    entries = PlatformExporter(
        FfmpegToolchain("ffmpeg", "ffprobe"),
        command_runner=run,
    ).export(
        master,
        tmp_path / "delivery",
        {
            platform: {"title": platform, "description": "copy"}
            for platform in ("douyin", "xiaohongshu", "tiktok")
        },
        selected_platforms=("douyin", "xiaohongshu", "tiktok"),
    )

    assert len([command for command in commands if command[0] == "ffmpeg"]) == 1
    assert len({entry["sha256"] for entry in entries.values()}) == 1


def test_real_ffmpeg_export_satisfies_platform_contract(tmp_path: Path) -> None:
    toolchain = _integration_ffmpeg()
    if toolchain is None:
        pytest.skip("需要支持 lavfi 的真实 ffmpeg/ffprobe")
    ffmpeg, ffprobe = toolchain
    master = tmp_path / "source.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=360x640:rate=24:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(master),
        ],
        check=True,
        capture_output=True,
    )

    entries = PlatformExporter(
        FfmpegToolchain(ffmpeg, ffprobe)
    ).export(
        master,
        tmp_path / "delivery",
        {"douyin": {"title": "标题", "description": "简介"}},
        selected_platforms=("douyin",),
    )

    assert entries["douyin"]["video"] == "douyin/video.mp4"
