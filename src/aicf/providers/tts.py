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

from aicf.subprocess_utils import silent_run


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
    command_runner: CommandRunner = silent_run,
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
    command_runner: CommandRunner = silent_run,
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


def _default_kokoro_paths() -> tuple[Path, Path]:
    """返回 Kokoro 模型和音色文件的默认路径。"""
    base = Path.home() / ".cache" / "hyperframes" / "tts"
    model = base / "models" / "kokoro-v1.0.onnx"
    voices = base / "voices" / "voices-v1.0.bin"
    return model, voices


class KokoroTtsProvider:
    """Kokoro 本地神经网络 TTS，使用 misaki 做中文 G2P。"""

    name = "kokoro"

    def __init__(
        self,
        voice: str = "zm_yunyang",
        speed: float = 1.0,
        model_path: Path | None = None,
        voices_path: Path | None = None,
        ffmpeg_executable: str = "ffmpeg",
    ) -> None:
        # 去掉 "kokoro:" 前缀
        self.voice = voice.removeprefix("kokoro:")
        self.speed = speed
        self._model_path = model_path
        self._voices_path = voices_path
        self._kokoro: Any = None
        self._g2p: Any = None
        self.ffmpeg_executable = ffmpeg_executable

    def _ensure_loaded(self) -> None:
        if self._kokoro is not None:
            return
        try:
            from kokoro_onnx import Kokoro
        except ImportError as error:
            raise RuntimeError("kokoro-onnx 未安装，请运行: uv add kokoro-onnx misaki[zh] soundfile") from error

        model_path = self._model_path
        voices_path = self._voices_path
        if model_path is None or voices_path is None:
            m, v = _default_kokoro_paths()
            model_path = model_path or m
            voices_path = voices_path or v

        if not model_path.is_file():
            raise FileNotFoundError(
                f"Kokoro 模型文件不存在: {model_path}，"
                "请将 kokoro-v1.0.onnx 放到 ~/.cache/hyperframes/tts/models/"
            )
        if not voices_path.is_file():
            raise FileNotFoundError(
                f"Kokoro 音色文件不存在: {voices_path}，"
                "请将 voices-v1.0.bin 放到 ~/.cache/hyperframes/tts/voices/"
            )

        self._kokoro = Kokoro(str(model_path), str(voices_path))

        # 初始化中文 G2P
        try:
            from misaki import zh as misaki_zh
            self._g2p = misaki_zh.ZHG2P()
        except ImportError as error:
            raise RuntimeError("misaki[zh] 未安装，请运行: uv add misaki[zh]") from error

    def available(self) -> bool:
        """检查依赖和模型是否都就绪。"""
        try:
            model_path = self._model_path or _default_kokoro_paths()[0]
            voices_path = self._voices_path or _default_kokoro_paths()[1]
            if not model_path.is_file() or not voices_path.is_file():
                return False
            import kokoro_onnx  # noqa: F401
            import misaki.zh  # noqa: F401
            return True
        except ImportError:
            return False

    def list_voices(self) -> list[str]:
        """返回可用的中文音色列表。"""
        self._ensure_loaded()
        return [v for v in self._kokoro.get_voices() if v.startswith("z")]

    def synthesize(self, request: TtsRequest) -> None:
        self._ensure_loaded()
        import soundfile as sf

        # 中文 G2P
        phonemes, _ = self._g2p(request.text)
        if not phonemes:
            raise RuntimeError("中文 G2P 结果为空")

        temporary = request.output_path.with_suffix(".kokoro.wav")
        temporary.unlink(missing_ok=True)
        try:
            samples, sample_rate = self._kokoro.create(
                phonemes,
                voice=self.voice,
                speed=self.speed,
                is_phonemes=True,
            )
            # Kokoro 输出 24000Hz，保存为临时文件再用 ffmpeg 转 48000Hz
            sf.write(str(temporary), samples, sample_rate)

            silent_run(
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


class EdgeTtsProvider:
    name = "edge_tts"

    def __init__(
        self,
        voice: str = "zh-CN-YunyangNeural",
        communicate_factory: Callable[[str, str], Any] | None = None,
        command_runner: CommandRunner = silent_run,
        ffmpeg_executable: str = "ffmpeg",
        request_timeout_seconds: float | None = None,
    ) -> None:
        self.voice = voice
        self._communicate_factory = communicate_factory
        self._command_runner = command_runner
        self.ffmpeg_executable = ffmpeg_executable
        self.request_timeout_seconds = (
            request_timeout_seconds
            if request_timeout_seconds is not None
            else float(os.getenv("EDGE_TTS_TIMEOUT_SECONDS", "45"))
        )

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
            async def save_with_timeout() -> None:
                try:
                    await asyncio.wait_for(
                        communicate_factory(request.text, self.voice).save(
                            str(temporary)
                        ),
                        timeout=self.request_timeout_seconds,
                    )
                except asyncio.TimeoutError as error:
                    raise TimeoutError(
                        f"Edge TTS 请求超时（{self.request_timeout_seconds:g} 秒）"
                    ) from error

            asyncio.run(save_with_timeout())
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
        command_runner: CommandRunner = silent_run,
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

    # Kokoro 始终优先；模型或依赖缺失时，TtsService 会自动降级。
    kokoro = KokoroTtsProvider(ffmpeg_executable=ffmpeg)
    providers: list[TtsProvider] = [
        kokoro,
        EdgeTtsProvider(ffmpeg_executable=ffmpeg),
        SapiTtsProvider(ffmpeg_executable=ffmpeg),
    ]

    return TtsService(providers)


class TtsService:
    def __init__(self, providers: Sequence[TtsProvider]) -> None:
        self.providers = list(providers)

    def select_voice(self, voice: str) -> None:
        """根据音色 ID 将对应 provider 排到最前，并设置其 voice 属性。"""
        if voice.startswith("kokoro:") or voice.startswith(("zm_", "zf_")):
            # Kokoro 音色
            voice_id = voice.removeprefix("kokoro:")
            target_name = "kokoro"
            for p in self.providers:
                if p.name == "kokoro":
                    p.voice = voice_id  # type: ignore[attr-defined]
        elif voice.startswith("Microsoft "):
            target_name = "windows_sapi"
            for p in self.providers:
                if p.name == "windows_sapi":
                    p.voice = voice  # type: ignore[attr-defined]
        else:
            # Edge TTS 音色（zh-CN-* 格式）
            target_name = "edge_tts"
            for p in self.providers:
                if p.name == "edge_tts":
                    p.voice = voice  # type: ignore[attr-defined]

        self.providers.sort(key=lambda p: p.name != target_name)

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

    def preview(self, text: str, output_path: str | Path, voice: str) -> TtsResult:
        """试听指定音色，不改变当前 service 的 provider 排序。"""
        # 创建临时 service 用于试听
        original_providers = list(self.providers)
        try:
            self.select_voice(voice)
            return self.synthesize(text, output_path)
        finally:
            self.providers = original_providers
