from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from aicf.models.contracts import SUPPORTED_PLATFORMS
from aicf.providers.tts import FfmpegToolchain
from aicf.subprocess_utils import silent_run


@dataclass(frozen=True)
class PlatformTemplate:
    filename: str
    width: int
    height: int
    fps: int
    video_bitrate: str
    min_video_bitrate: int
    max_video_bitrate: int


PLATFORM_TEMPLATES: dict[str, PlatformTemplate] = {
    "douyin": PlatformTemplate(
        "video.mp4", 1080, 1920, 30, "8M", 500_000, 12_000_000
    ),
    "xiaohongshu": PlatformTemplate(
        "video.mp4", 1080, 1920, 30, "8M", 500_000, 12_000_000
    ),
    "tiktok": PlatformTemplate(
        "video.mp4", 1080, 1920, 30, "8M", 500_000, 12_000_000
    ),
    "youtube_shorts": PlatformTemplate(
        "video.mp4", 1080, 1920, 30, "10M", 500_000, 15_000_000
    ),
    "youtube": PlatformTemplate(
        "video.mp4", 1920, 1080, 30, "12M", 800_000, 20_000_000
    ),
}


class PlatformExporter:
    def __init__(
        self,
        toolchain: FfmpegToolchain,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = silent_run,
    ) -> None:
        self.toolchain = toolchain
        self.command_runner = command_runner

    def export(
        self,
        master_video: str | Path,
        output_dir: str | Path,
        package: Mapping[str, object],
        *,
        selected_platforms: Sequence[str],
    ) -> dict[str, dict[str, str]]:
        master = Path(master_video)
        destination = Path(output_dir)
        invalid = set(selected_platforms) - set(SUPPORTED_PLATFORMS)
        if invalid:
            raise ValueError(f"不支持的平台: {sorted(invalid)}")
        if not selected_platforms:
            raise ValueError("至少选择一个导出平台")

        master_probe = self._probe(master)
        grouped: dict[tuple[object, ...], list[str]] = {}
        for platform in selected_platforms:
            template = PLATFORM_TEMPLATES[platform]
            grouped.setdefault(self._spec_key(template), []).append(platform)

        entries: dict[str, dict[str, str]] = {}
        for platforms in grouped.values():
            canonical_platform = platforms[0]
            template = PLATFORM_TEMPLATES[canonical_platform]
            canonical_dir = destination / canonical_platform
            canonical_dir.mkdir(parents=True, exist_ok=True)
            canonical_video = canonical_dir / template.filename
            if self._matches(master_probe, template):
                shutil.copy2(master, canonical_video)
            else:
                self.command_runner(
                    [
                        self.toolchain.ffmpeg,
                        "-y",
                        "-hide_banner",
                        "-i",
                        str(master),
                        "-s",
                        f"{template.width}x{template.height}",
                        "-r",
                        str(template.fps),
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        "-b:v",
                        template.video_bitrate,
                        "-minrate",
                        template.video_bitrate,
                        "-maxrate",
                        template.video_bitrate,
                        "-bufsize",
                        self._double_bitrate(template.video_bitrate),
                        "-x264-params",
                        "nal-hrd=cbr:force-cfr=1",
                        "-c:a",
                        "aac",
                        "-ar",
                        "48000",
                        "-ac",
                        "2",
                        str(canonical_video),
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            exported_probe = self._probe(canonical_video)
            if not self._matches(exported_probe, template):
                raise RuntimeError(
                    f"{canonical_platform} 导出不符合模板: {exported_probe}"
                )
            for platform in platforms:
                platform_dir = destination / platform
                platform_dir.mkdir(parents=True, exist_ok=True)
                video = platform_dir / template.filename
                if video != canonical_video:
                    shutil.copy2(canonical_video, video)
                copy_path = platform_dir / "publish.md"
                copy_path.write_text(
                    self._publish_markdown(platform, package.get(platform)),
                    encoding="utf-8",
                )
                entries[platform] = {
                    "video": video.relative_to(destination).as_posix(),
                    "copy": copy_path.relative_to(destination).as_posix(),
                    "sha256": self._sha256(video),
                }
        return entries

    def _probe(self, path: Path) -> dict[str, float | int]:
        completed = self.command_runner(
            [
                self.toolchain.ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=bit_rate:stream=codec_type,width,height,r_frame_rate,bit_rate",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        payload = json.loads(completed.stdout)
        if {"width", "height", "fps", "bit_rate"}.issubset(payload):
            return {
                "width": int(payload["width"]),
                "height": int(payload["height"]),
                "fps": float(payload["fps"]),
                "bit_rate": int(payload["bit_rate"]),
            }
        streams = payload.get("streams", [])
        video = next(
            item for item in streams if item.get("codec_type") == "video"
        )
        numerator, _, denominator = str(video["r_frame_rate"]).partition("/")
        fps = float(numerator) / float(denominator or 1)
        return {
            "width": int(video["width"]),
            "height": int(video["height"]),
            "fps": fps,
            "bit_rate": int(
                video.get("bit_rate") or payload["format"]["bit_rate"]
            ),
        }

    @staticmethod
    def _matches(
        probe: Mapping[str, float | int],
        template: PlatformTemplate,
    ) -> bool:
        return (
            (int(probe["width"]), int(probe["height"]))
            == (template.width, template.height)
            and abs(float(probe["fps"]) - template.fps) <= 0.01
            and template.min_video_bitrate
            <= int(probe["bit_rate"])
            <= template.max_video_bitrate
        )

    @staticmethod
    def _double_bitrate(value: str) -> str:
        match = re.fullmatch(r"(\d+)([kKmM]?)", value.strip())
        if match is None:
            raise ValueError(f"无效视频码率: {value}")
        return f"{int(match.group(1)) * 2}{match.group(2)}"

    @staticmethod
    def _spec_key(template: PlatformTemplate) -> tuple[object, ...]:
        return (
            template.width,
            template.height,
            template.fps,
            template.video_bitrate,
            template.min_video_bitrate,
            template.max_video_bitrate,
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _publish_markdown(platform: str, metadata: object) -> str:
        value = metadata if isinstance(metadata, dict) else {}
        hashtags = value.get("hashtags", [])
        hashtag_text = " ".join(
            f"#{tag}" for tag in hashtags if isinstance(tag, str) and tag.strip()
        )
        return (
            f"# {value.get('title', '')}\n\n"
            f"{value.get('description', '')}\n\n"
            f"{hashtag_text}\n\n"
            f"平台：{platform}\n"
        )
