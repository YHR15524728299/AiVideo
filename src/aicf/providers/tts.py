from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence


@dataclass(frozen=True)
class TtsRequest:
    text: str
    output_path: Path


@dataclass(frozen=True)
class TtsResult:
    provider: str
    degraded: bool
    degradation_reason: str | None
    output_path: Path
    metadata_path: Path


class TtsProvider(Protocol):
    name: str

    def synthesize(self, request: TtsRequest) -> None: ...


class TtsAllProvidersFailed(RuntimeError):
    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("；".join(reasons))


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True)
class FfmpegToolchain:
    ffmpeg: str
    ffprobe: str


def _default_ffmpeg_candidates() -> list[Path]:
    executable = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    candidates: list[Path] = []
    configured = os.getenv("AICF_FFMPEG_DIR")
    if configured:
        candidates.append(Path(configured) / executable)
    candidates.extend(
        Path(entry) / executable
        for entry in os.getenv("PATH", "").split(os.pathsep)
        if entry
    )
    discovered = shutil.which("ffmpeg")
    if discovered:
        candidates.append(Path(discovered))
    if os.name == "nt":
        winget = Path(os.getenv("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
        if winget.is_dir():
            candidates.extend(winget.glob("**/bin/ffmpeg.exe"))
    return list(dict.fromkeys(path for path in candidates if path.is_file()))


def discover_ffmpeg_toolchain(
    candidates: Sequence[Path] | None = None,
    command_runner: CommandRunner = subprocess.run,
) -> FfmpegToolchain:
    probe_name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    for ffmpeg in candidates or _default_ffmpeg_candidates():
        ffprobe = ffmpeg.with_name(probe_name)
        if not ffprobe.is_file():
            continue
        try:
            ffmpeg_version = command_runner(
                [str(ffmpeg), "-version"],
                check=True,
                capture_output=True,
                text=True,
            )
            ffprobe_version = command_runner(
                [str(ffprobe), "-version"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        version_text = (
            ffmpeg_version.stdout
            + ffmpeg_version.stderr
            + ffprobe_version.stdout
            + ffprobe_version.stderr
        )
        if "--disable-everything" in version_text:
            continue
        return FfmpegToolchain(str(ffmpeg), str(ffprobe))
    raise FileNotFoundError("未找到同目录且非精简构建的完整 ffmpeg + ffprobe")


def find_audio_ffmpeg(
    candidates: Sequence[Path] | None = None,
    command_runner: CommandRunner = subprocess.run,
) -> str:
    if candidates is None:
        try:
            return discover_ffmpeg_toolchain(command_runner=command_runner).ffmpeg
        except FileNotFoundError:
            return "ffmpeg"
    for candidate in candidates:
        try:
            completed = command_runner(
                [str(candidate), "-hide_banner", "-formats"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        formats = f"{completed.stdout}\n{completed.stderr}"
        capabilities = {}
        for line in formats.splitlines():
            columns = line.split()
            if len(columns) >= 2 and columns[0].strip(".") in {"D", "E", "DE"}:
                capabilities[columns[1]] = columns[0]
        if "D" in capabilities.get("mp3", "") and "E" in capabilities.get("wav", ""):
            return str(candidate)
    return "ffmpeg"


class EdgeTtsProvider:
    name = "edge_tts"

    def __init__(
        self,
        voice: str = "zh-CN-XiaoxiaoNeural",
        communicate_factory: Callable[[str, str], Any] | None = None,
        command_runner: CommandRunner = subprocess.run,
        ffmpeg_executable: str = "ffmpeg",
    ) -> None:
        self.voice = voice
        self._communicate_factory = communicate_factory
        self._command_runner = command_runner
        self.ffmpeg_executable = ffmpeg_executable

    def synthesize(self, request: TtsRequest) -> None:
        communicate_factory = self._communicate_factory
        if communicate_factory is None:
            try:
                from edge_tts import Communicate
            except ImportError as error:
                raise RuntimeError("edge-tts 未安装") from error
            communicate_factory = Communicate

        temporary = request.output_path.with_suffix(".edge.mp3")
        temporary.unlink(missing_ok=True)
        try:
            asyncio.run(
                communicate_factory(request.text, self.voice).save(str(temporary))
            )
            self._command_runner(
                [
                    self.ffmpeg_executable,
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(temporary),
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-c:a",
                    "pcm_s16le",
                    str(request.output_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        finally:
            temporary.unlink(missing_ok=True)


class SapiTtsProvider:
    name = "windows_sapi"

    def __init__(
        self,
        voice: str = "Microsoft Huihui Desktop",
        command_runner: CommandRunner = subprocess.run,
        ffmpeg_executable: str = "ffmpeg",
    ) -> None:
        self.voice = voice
        self._command_runner = command_runner
        self.ffmpeg_executable = ffmpeg_executable

    def synthesize(self, request: TtsRequest) -> None:
        if request.output_path.suffix.lower() != ".wav":
            raise ValueError("Windows SAPI 回退仅支持 .wav 输出")
        encoded_text = base64.b64encode(request.text.encode("utf-8")).decode("ascii")
        encoded_voice = base64.b64encode(self.voice.encode("utf-8")).decode("ascii")
        temporary = request.output_path.with_suffix(".sapi.wav")
        temporary.unlink(missing_ok=True)
        encoded_path = base64.b64encode(str(temporary.resolve()).encode("utf-8")).decode(
            "ascii"
        )
        script = f"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_text}'))
$voice = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_voice}'))
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded_path}'))
$synth = [System.Speech.Synthesis.SpeechSynthesizer]::new()
try {{
    if ($voice) {{ $synth.SelectVoice($voice) }}
    $synth.SetOutputToWaveFile($path)
    $synth.Speak($text)
}} finally {{
    $synth.Dispose()
}}
""".strip()
        encoded_script = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
        try:
            self._command_runner(
                [
                    "powershell.exe",
                    "-NoLogo",
                    "-NoProfile",
                    "-NonInteractive",
                    "-EncodedCommand",
                    encoded_script,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            self._command_runner(
                [
                    self.ffmpeg_executable,
                    "-y",
                    "-loglevel",
                    "error",
                    "-i",
                    str(temporary),
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-c:a",
                    "pcm_s16le",
                    str(request.output_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        finally:
            temporary.unlink(missing_ok=True)


def build_default_tts_service() -> "TtsService":
    try:
        ffmpeg = discover_ffmpeg_toolchain().ffmpeg
    except FileNotFoundError:
        ffmpeg = find_audio_ffmpeg()
    return TtsService(
        [
            EdgeTtsProvider(ffmpeg_executable=ffmpeg),
            SapiTtsProvider(ffmpeg_executable=ffmpeg),
        ]
    )


class TtsService:
    def __init__(self, providers: Sequence[TtsProvider]) -> None:
        self.providers = list(providers)

    def synthesize(self, text: str, output_path: str | Path) -> TtsResult:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.unlink(missing_ok=True)
        reasons: list[str] = []
        request = TtsRequest(text=text, output_path=target)
        for provider in self.providers:
            try:
                provider.synthesize(request)
                if not target.is_file() or target.stat().st_size == 0:
                    raise RuntimeError("Provider 未生成有效音频文件")
            except Exception as error:
                target.unlink(missing_ok=True)
                reasons.append(f"{provider.name}: {type(error).__name__}: {error}")
                continue

            metadata_path = target.with_suffix(target.suffix + ".tts.json")
            result = TtsResult(
                provider=provider.name,
                degraded=bool(reasons),
                degradation_reason="；".join(reasons) if reasons else None,
                output_path=target,
                metadata_path=metadata_path,
            )
            metadata_path.write_text(
                json.dumps(
                    {
                        "provider": result.provider,
                        "degraded": result.degraded,
                        "degradation_reason": result.degradation_reason,
                        "output_path": str(result.output_path),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            return result
        raise TtsAllProvidersFailed(reasons)
