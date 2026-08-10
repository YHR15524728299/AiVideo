from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from aicf.models.contracts import VisualPlan, VisualShot
from aicf.artifact_commit import JournaledFileGroup
from aicf.production_settings import get_resolution
from aicf.providers.tts import FfmpegToolchain
from aicf.subprocess_utils import silent_run


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _fps(value: str) -> float:
    numerator, separator, denominator = value.partition("/")
    if not separator:
        return float(value)
    return float(numerator) / float(denominator)


@dataclass(frozen=True)
class MediaProbe:
    duration_seconds: float
    size_bytes: int
    video_codec: str
    width: int
    height: int
    pixel_format: str
    fps: float
    audio_codec: str
    sample_rate: int
    channels: int

    def assert_vertical_delivery(
        self,
        expected_duration_seconds: float,
        *,
        orientation: str = "portrait",
        expected_resolution: tuple[int, int] | None = None,
    ) -> None:
        errors: list[str] = []
        expected_width, expected_height = (
            expected_resolution
            if expected_resolution is not None
            else get_resolution(orientation)
        )
        if (self.width, self.height) != (expected_width, expected_height):
            errors.append(
                f"分辨率应为 {expected_width}x{expected_height}，"
                f"实际 {self.width}x{self.height}"
            )
        if self.video_codec != "h264":
            errors.append(f"视频编码应为 h264，实际 {self.video_codec}")
        if self.pixel_format != "yuv420p":
            errors.append(f"像素格式应为 yuv420p，实际 {self.pixel_format}")
        if abs(self.fps - 30.0) > 0.01:
            errors.append(f"帧率应为 30，实际 {self.fps}")
        if self.audio_codec != "aac":
            errors.append(f"音频编码应为 aac，实际 {self.audio_codec}")
        if self.sample_rate != 48_000 or self.channels != 2:
            errors.append(
                f"音频应为 48000Hz/双声道，实际 {self.sample_rate}Hz/{self.channels}声道"
            )
        if abs(self.duration_seconds - expected_duration_seconds) > 0.15:
            errors.append(
                f"成片时长偏差过大，期望 {expected_duration_seconds:.3f}s，"
                f"实际 {self.duration_seconds:.3f}s"
            )
        if errors:
            raise ValueError("；".join(errors))


@dataclass(frozen=True)
class RenderResult:
    master_output_path: Path
    clean_output_path: Path
    manifest_path: Path
    pending_master_path: Path
    pending_clean_path: Path
    filter_script_path: Path

    @property
    def output_path(self) -> Path:
        return self.master_output_path


def probe_media(
    ffprobe_executable: str,
    media_path: str | Path,
    command_runner: CommandRunner = silent_run,
) -> MediaProbe:
    media = Path(media_path)
    completed = command_runner(
        [
            ffprobe_executable,
            "-v",
            "error",
            "-show_entries",
            (
                "format=duration,size:"
                "stream=codec_type,codec_name,width,height,pix_fmt,"
                "r_frame_rate,sample_rate,channels"
            ),
            "-of",
            "json",
            str(media),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    payload = json.loads(completed.stdout)
    streams = payload.get("streams", [])
    video = next(item for item in streams if item.get("codec_type") == "video")
    audio = next(item for item in streams if item.get("codec_type") == "audio")
    media_format = payload["format"]
    return MediaProbe(
        duration_seconds=float(media_format["duration"]),
        size_bytes=int(media_format["size"]),
        video_codec=str(video["codec_name"]),
        width=int(video["width"]),
        height=int(video["height"]),
        pixel_format=str(video["pix_fmt"]),
        fps=_fps(str(video["r_frame_rate"])),
        audio_codec=str(audio["codec_name"]),
        sample_rate=int(audio["sample_rate"]),
        channels=int(audio["channels"]),
    )


class FfmpegRenderer:
    def __init__(
        self,
        toolchain: FfmpegToolchain,
        command_runner: CommandRunner = silent_run,
    ) -> None:
        self.toolchain = toolchain
        self._command_runner = command_runner

    def render(
        self,
        *,
        visual_plan_path: str | Path,
        audio_path: str | Path,
        subtitle_path: str | Path,
        output_path: str | Path,
        title: str,
        orientation: str = "portrait",
    ) -> RenderResult:
        plan_path = Path(visual_plan_path).resolve()
        audio = Path(audio_path).resolve()
        subtitles = Path(subtitle_path).resolve()
        master_output = Path(output_path)
        clean_output = master_output.with_name("clean.mp4")
        if not plan_path.is_file() or not audio.is_file() or not subtitles.is_file():
            raise FileNotFoundError("渲染所需的视觉计划、旁白或字幕不存在")
        plan = VisualPlan.model_validate_json(
            plan_path.read_text(encoding="utf-8-sig")
        )
        assets = [self._resolve_asset(plan_path, shot) for shot in plan.shots]
        missing = [str(path) for path in assets if not path.is_file()]
        if missing:
            raise FileNotFoundError("视觉素材不存在: " + "、".join(missing))

        master_output.parent.mkdir(parents=True, exist_ok=True)
        pending_dir = master_output.parent / ".pending"
        pending_dir.mkdir(parents=True, exist_ok=True)
        transaction_dir = pending_dir / uuid.uuid4().hex
        transaction_dir.mkdir(parents=True)
        pending_master = transaction_dir / master_output.name
        pending_clean = transaction_dir / clean_output.name
        out_width, out_height = get_resolution(orientation)
        filter_script = transaction_dir / "m5_filter_complex.txt"
        filter_script.write_text(
            self._build_filter_script(
                plan, subtitles, width=out_width, height=out_height
            ),
            encoding="utf-8",
        )

        command = [
            self.toolchain.ffmpeg,
            "-y",
            "-hide_banner",
        ]
        for shot, asset in zip(plan.shots, assets):
            if shot.asset_type == "image":
                command.extend(
                    [
                        "-loop",
                        "1",
                        "-framerate",
                        "30",
                        "-t",
                        f"{shot.duration_seconds:.6f}",
                    ]
                )
            command.extend(["-i", str(asset)])
        audio_index = len(assets)
        command.extend(
            [
                "-i",
                str(audio),
                "-filter_complex_script",
                str(filter_script),
            ]
        )
        self._append_output(
            command,
            video_label="cleanv",
            audio_index=audio_index,
            duration_seconds=plan.total_duration_seconds,
            output=pending_clean,
        )
        self._append_output(
            command,
            video_label="masterv",
            audio_index=audio_index,
            duration_seconds=plan.total_duration_seconds,
            output=pending_master,
        )
        self._command_runner(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        manifest_path = master_output.with_suffix(".render.json")
        pending_manifest = transaction_dir / manifest_path.name
        pending_manifest.write_text(
            json.dumps(
                {
                    "title": title,
                    "inputs": {
                        "visual_plan": str(plan_path),
                        "assets": [str(path) for path in assets],
                        "audio": str(audio),
                        "subtitles": str(subtitles),
                    },
                    "outputs": {
                        "master": str(master_output),
                        "clean": str(clean_output),
                    },
                    "render": {
                        "width": out_width,
                        "height": out_height,
                        "fps": 30,
                        "video_codec": "h264",
                        "pixel_format": "yuv420p",
                        "audio_codec": "aac",
                        "sample_rate": 48000,
                        "channels": 2,
                        "duration_seconds": plan.total_duration_seconds,
                        "shot_count": len(plan.shots),
                        "orientation": orientation,
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return RenderResult(
            master_output_path=master_output,
            clean_output_path=clean_output,
            manifest_path=manifest_path,
            pending_master_path=pending_master,
            pending_clean_path=pending_clean,
            filter_script_path=filter_script,
        )

    def render_and_validate(
        self,
        **kwargs: object,
    ) -> tuple[RenderResult, dict[str, MediaProbe]]:
        output_path = Path(kwargs["output_path"])
        committer = JournaledFileGroup(output_path.parent)
        committer.recover()
        orientation = str(kwargs.get("orientation", "portrait"))
        result = self.render(**kwargs)
        plan = VisualPlan.model_validate_json(
            Path(kwargs["visual_plan_path"]).read_text(encoding="utf-8-sig")
        )
        probes = {
            "clean": probe_media(
                self.toolchain.ffprobe,
                result.pending_clean_path,
                self._command_runner,
            ),
            "master": probe_media(
                self.toolchain.ffprobe,
                result.pending_master_path,
                self._command_runner,
            ),
        }
        for probe in probes.values():
            probe.assert_vertical_delivery(
                plan.total_duration_seconds, orientation=orientation
            )

        pending_dir = result.pending_master_path.parent
        for name, probe in probes.items():
            (pending_dir / f"{name}.ffprobe.json").write_text(
                json.dumps(asdict(probe), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        committer.commit(
            pending_dir,
            [
                result.clean_output_path.name,
                result.master_output_path.name,
                result.manifest_path.name,
                "clean.ffprobe.json",
                "master.ffprobe.json",
            ],
        )
        return result, probes

    @staticmethod
    def _resolve_asset(plan_path: Path, shot: VisualShot) -> Path:
        asset = Path(shot.expected_path)
        if not asset.is_absolute():
            asset = plan_path.parent / asset
        return asset.resolve()

    @classmethod
    def _build_filter_script(
        cls, plan: VisualPlan, subtitles: Path, *, width: int, height: int
    ) -> str:
        chains: list[str] = []
        labels: list[str] = []
        scale_crop = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
        zoompan_size = f"{width}x{height}"
        for index, shot in enumerate(plan.shots):
            label = f"shot{index}"
            labels.append(f"[{label}]")
            if shot.asset_type == "image":
                chain = (
                    f"[{index}:v]{scale_crop},"
                    "zoompan="
                    "z='min(zoom+0.001,1.08)':"
                    "x='iw/2-(iw/zoom/2)':"
                    "y='ih/2-(ih/zoom/2)':"
                    f"d=1:s={zoompan_size}:fps=30,"
                    f"trim=duration={shot.duration_seconds:.6f},"
                    f"setpts=PTS-STARTPTS,setsar=1,format=yuv420p[{label}]"
                )
            else:
                chain = (
                    f"[{index}:v]trim=duration={shot.duration_seconds:.6f},"
                    f"setpts=PTS-STARTPTS,{scale_crop},fps=30,"
                    f"setsar=1,format=yuv420p[{label}]"
                )
            chains.append(chain)
        chains.append(
            "".join(labels)
            + f"concat=n={len(labels)}:v=1:a=0,split=2[cleanv][subtitlebase]"
        )
        escaped_subtitles = cls._escape_filter_path(subtitles.resolve())
        chains.append(
            f"[subtitlebase]ass=filename='{escaped_subtitles}'[masterv]"
        )
        return ";\n".join(chains) + "\n"

    @staticmethod
    def _escape_filter_path(path: Path) -> str:
        return (
            path.as_posix()
            .replace("\\", r"\\")
            .replace(":", r"\:")
            .replace("'", r"\'")
        )

    @staticmethod
    def _append_output(
        command: list[str],
        *,
        video_label: str,
        audio_index: int,
        duration_seconds: float,
        output: Path,
    ) -> None:
        command.extend(
            [
                "-map",
                f"[{video_label}]",
                "-map",
                f"{audio_index}:a:0",
                "-c:v",
                "libx264",
                "-preset",
                "medium",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "30",
                "-c:a",
                "aac",
                "-b:a",
                "192k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-t",
                f"{duration_seconds:.6f}",
                "-movflags",
                "+faststart",
                str(output),
            ]
        )
