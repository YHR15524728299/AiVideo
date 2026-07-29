from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

from dotenv import load_dotenv

from .providers.tts import find_audio_ffmpeg
from .providers.openrouter import DEFAULT_FREE_MODEL

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
        # 检查视频服务至少一个可用
        has_video = False
        for k in ("jimeng", "kling"):
            c = self.checks.get(k)
            if c and c.available:
                has_video = True
                break
        if has_video and self.healthy:
            lines.append("总体状态: 就绪，可以运行完整流水线")
        elif self.healthy:
            lines.append("总体状态: 可运行 M0-M3（缺视频服务，M4+ 将失败）")
        else:
            lines.append("总体状态: 需要处理")
        return "\n".join(lines)


def _check_executable(path: str) -> Check:
    """检查可执行文件是否存在。完整路径直接检查文件，否则用shutil.which。"""
    if not path:
        return Check(False, "未配置")
    p = Path(path)
    if p.is_absolute():
        # 完整路径：检查文件是否存在
        if p.is_file():
            return Check(True, str(p))
        # 尝试加扩展名
        for ext in (".exe", ".cmd", ".bat"):
            pe = Path(str(p) + ext)
            if pe.is_file():
                return Check(True, str(pe))
        return Check(False, f"文件不存在: {path}")
    # 非完整路径：用shutil.which搜索
    found = shutil.which(path)
    return Check(bool(found), found or f"PATH 中未找到: {path}")


class Doctor:
    def __init__(
        self,
        jimeng_executable: str | None = None,
        kling_executable: str | None = None,
        edge_tts_available: bool | None = None,
        kokoro_available: bool | None = None,
        sapi_available: bool | None = None,
        audio_ffmpeg: str | None = None,
    ) -> None:
        self.jimeng_executable = jimeng_executable or os.getenv("JIMENG_CLI_EXECUTABLE", "")
        self.kling_executable = kling_executable or os.getenv("KLING_CLI_EXECUTABLE", "")
        self.edge_tts_available = (
            find_spec("edge_tts") is not None
            if edge_tts_available is None
            else edge_tts_available
        )
        # Kokoro 检测：检查包是否安装
        if kokoro_available is None:
            try:
                import importlib.util
                self.kokoro_available = importlib.util.find_spec("kokoro_onnx") is not None
            except Exception:
                self.kokoro_available = False
        else:
            self.kokoro_available = kokoro_available
        self.sapi_available = (
            sys.platform == "win32" if sapi_available is None else sapi_available
        )
        self.audio_ffmpeg = (
            find_audio_ffmpeg() if audio_ffmpeg is None else audio_ffmpeg
        )

    @staticmethod
    def _tool(name: str) -> Check:
        path = shutil.which(name)
        return Check(bool(path), path or f"PATH 中未找到: {name}", required=False)

    def _detect_kling(self) -> Check:
        """轻量检测可灵CLI是否存在（不做网络who_am_i调用避免卡顿）。"""
        kling_exe = self.kling_executable or shutil.which("kling")
        if not kling_exe:
            # 检查常见路径
            candidates = [
                Path(os.environ.get("APPDATA", "")) / "npm" / "kling.cmd",
                Path(os.environ.get("APPDATA", "")) / "TRAE SOLO CN" / "ModularData" / "ai-agent" / "vm" / "tools" / "node" / "kling.cmd",
            ]
            for c in candidates:
                if c.is_file():
                    kling_exe = str(c)
                    break
        if kling_exe:
            return Check(True, f"已安装（{kling_exe}），需登录后可用", required=False)
        return Check(False, "未安装（npm i -g @klingai/cli-cn 后 kling login）", required=False)

    def _detect_jimeng(self) -> Check:
        """轻量检测即梦CLI是否存在。"""
        if self.jimeng_executable:
            return _check_executable(self.jimeng_executable)
        # 搜索PATH
        found = shutil.which("dreamina")
        if found:
            return Check(True, f"已安装（{found}），需登录后可用", required=False)
        return Check(False, "未安装或未在PATH中", required=False)

    def run(self) -> DoctorReport:
        # 即梦检测（轻量）
        jimeng = self._detect_jimeng()

        # 可灵检测（轻量，不做网络请求）
        kling = self._detect_kling()

        # Kokoro TTS检测
        if self.kokoro_available:
            try:
                from .providers.tts import KokoroTtsProvider
                kp = KokoroTtsProvider()
                if kp.available():
                    kokoro_check = Check(True, "本地模型就绪（最高音质）", required=False)
                else:
                    kokoro_check = Check(True, "已安装但模型未下载（首次使用自动下载）", required=False)
            except Exception as e:
                kokoro_check = Check(True, f"已安装但初始化异常: {str(e)[:40]}", required=False)
        else:
            kokoro_check = Check(False, "未安装（uv add kokoro-onnx misaki[zh] soundfile，可选）", required=False)

        # TTS策略描述（Kokoro优先）
        available_tts = []
        if self.kokoro_available:
            available_tts.append("Kokoro本地TTS（首选）")
        if self.edge_tts_available:
            available_tts.append("Edge TTS（在线回退）")
        if self.sapi_available:
            available_tts.append("Windows SAPI（最终回退）")
        if available_tts:
            tts_strategy = Check(True, " → ".join(available_tts), required=False)
        else:
            tts_strategy = Check(False, "无可用TTS引擎", required=False)

        # FFmpeg检测
        ffmpeg_check = Check(
            bool(self.audio_ffmpeg),
            self.audio_ffmpeg or "未找到FFmpeg（winget install Gyan.FFmpeg）",
        )

        # ffprobe检测：在ffmpeg同目录查找
        ffprobe_path = ""
        if self.audio_ffmpeg:
            ffmpeg_dir = Path(self.audio_ffmpeg).parent
            for name in ("ffprobe.exe", "ffprobe"):
                candidate = ffmpeg_dir / name
                if candidate.is_file():
                    ffprobe_path = str(candidate)
                    break
        ffprobe_check = Check(
            bool(ffprobe_path),
            ffprobe_path or "未找到ffprobe（应与ffmpeg同目录）",
        )

        api_key = bool(os.getenv("OPENROUTER_API_KEY"))
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        py_ok = sys.version_info >= (3, 10)

        return DoctorReport(
            {
                "python": Check(py_ok, f"{sys.executable} (v{py_ver}){'，需 >= 3.10' if not py_ok else ''}"),
                "ffmpeg": ffmpeg_check,
                "ffprobe": ffprobe_check,
                "openrouter": Check(
                    api_key,
                    f"已配置（当前模型: {os.getenv('OPENROUTER_MODEL', DEFAULT_FREE_MODEL)}）" if api_key else "未配置 OPENROUTER_API_KEY",
                    required=False,
                ),
                "jimeng": jimeng,
                "kling": kling,
                "tts_kokoro": kokoro_check,
                "tts_edge": Check(
                    self.edge_tts_available,
                    "edge-tts 已安装" if self.edge_tts_available else "未安装（pip install edge-tts，可选）",
                    required=False,
                ),
                "tts_sapi": Check(
                    self.sapi_available,
                    "Windows SAPI 可用" if self.sapi_available else "仅支持Windows",
                    required=False,
                ),
                "tts_strategy": tts_strategy,
            }
        )
