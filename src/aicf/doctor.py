from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

from dotenv import load_dotenv

from .providers.tts import find_audio_ffmpeg

# 自动加载项目根目录 .env 文件
_PROJECT_ROOT = Path(__file__).parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=False)


@dataclass(frozen=True)
class Check:
    available: bool
    detail: str
    required: bool = True


@dataclass(frozen=True)
class DoctorReport:
    checks: dict[str, Check]

    @property
    def healthy(self) -> bool:
        return all(check.available for check in self.checks.values() if check.required)

    def to_text(self) -> str:
        lines = []
        for name, check in self.checks.items():
            state = "OK" if check.available else ("缺失" if check.required else "未配置")
            lines.append(f"{name}: {state} - {check.detail}")
        lines.append(f"总体状态: {'可运行 M0/M1' if self.healthy else '需要处理'}")
        return "\n".join(lines)


class Doctor:
    def __init__(
        self,
        jimeng_executable: str | None = None,
        edge_tts_available: bool | None = None,
        sapi_available: bool | None = None,
        audio_ffmpeg: str | None = None,
    ) -> None:
        configured_jimeng = jimeng_executable or os.getenv("JIMENG_CLI_EXECUTABLE", "")
        default_dreamina = Path.home() / "bin" / "dreamina.exe"
        self.jimeng_executable = (
            configured_jimeng
            or str(default_dreamina)
            if default_dreamina.is_file()
            else configured_jimeng
        )
        self.edge_tts_available = (
            find_spec("edge_tts") is not None
            if edge_tts_available is None
            else edge_tts_available
        )
        self.sapi_available = (
            sys.platform == "win32" if sapi_available is None else sapi_available
        )
        self.audio_ffmpeg = (
            find_audio_ffmpeg() if audio_ffmpeg is None else audio_ffmpeg
        )

    @staticmethod
    def _tool(name: str) -> Check:
        path = shutil.which(name)
        return Check(bool(path), path or "PATH 中未找到")

    def run(self) -> DoctorReport:
        jimeng = (
            self._tool(self.jimeng_executable)
            if self.jimeng_executable
            else Check(False, "设置 JIMENG_CLI_EXECUTABLE 后检测", required=False)
        )
        api_key = bool(os.getenv("OPENROUTER_API_KEY"))
        if self.edge_tts_available and self.audio_ffmpeg:
            tts_strategy = Check(
                True,
                f"首选 edge_tts（{self.audio_ffmpeg}）；运行失败时自动回退 windows_sapi",
                required=False,
            )
        elif self.sapi_available:
            reasons = []
            if not self.edge_tts_available:
                reasons.append("edge-tts 未安装")
            if not self.audio_ffmpeg:
                reasons.append("音频转码 FFmpeg 不可用")
            tts_strategy = Check(
                True,
                f"实际 Provider 将为 windows_sapi；降级原因：{'；'.join(reasons)}",
                required=False,
            )
        else:
            tts_strategy = Check(
                False,
                "edge-tts 未安装，且 Windows SAPI 不可用",
                required=False,
            )
        ffmpeg_check = Check(
            bool(self.audio_ffmpeg),
            self.audio_ffmpeg or "未找到完整 FFmpeg 工具链",
        )
        if self.audio_ffmpeg:
            ffmpeg_path = Path(self.audio_ffmpeg)
            probe_name = (
                "ffprobe.exe" if ffmpeg_path.suffix.lower() == ".exe" else "ffprobe"
            )
            ffprobe_path = str(ffmpeg_path.with_name(probe_name))
        else:
            ffprobe_path = ""
        ffprobe_check = Check(
            bool(ffprobe_path),
            ffprobe_path or "未找到与 FFmpeg 同目录的完整 ffprobe",
        )
        return DoctorReport(
            {
                "python": Check(True, sys.executable),
                "git": self._tool("git"),
                "ffmpeg": ffmpeg_check,
                "ffprobe": ffprobe_check,
                "jimeng": jimeng,
                "openrouter": Check(
                    api_key,
                    f"已配置（值已隐藏），默认免费模型：{os.getenv('OPENROUTER_MODEL', 'tencent/hy3:free')}" if api_key else "OPENROUTER_API_KEY 未配置",
                    required=False,
                ),
                "tts_edge": Check(
                    self.edge_tts_available,
                    "edge-tts 已安装"
                    if self.edge_tts_available
                    else "edge-tts 未安装",
                    required=False,
                ),
                "tts_sapi": Check(
                    self.sapi_available,
                    "Windows SAPI 可检测" if self.sapi_available else "仅支持 Windows",
                    required=False,
                ),
                "tts_audio_ffmpeg": Check(
                    bool(self.audio_ffmpeg),
                    self.audio_ffmpeg or "未找到同时支持 MP3 解码与 WAV 编码的 FFmpeg",
                    required=False,
                ),
                "tts_strategy": tts_strategy,
            }
        )
