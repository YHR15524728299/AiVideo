from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path

from dotenv import load_dotenv

from .logging_utils import sanitize_error
from .providers.tts import find_audio_ffmpeg
from .providers.openrouter import DEFAULT_FREE_MODEL

# 自动加载项目根目录 .env 文件
_PROJECT_ROOT = Path(__file__).parent.parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=False)


def _describe_path(path: str | Path) -> str:
    """将绝对路径转换为友好的位置描述，不暴露个人信息。"""
    p = Path(str(path))
    path_str = str(p)
    name = p.name

    # 项目内文件 → 相对路径
    try:
        rel = p.resolve().relative_to(_PROJECT_ROOT.resolve())
        return str(rel)
    except ValueError:
        pass

    # Python解释器
    if name.lower().startswith("python") and name.lower().endswith((".exe", "")):
        # 检测是否是虚拟环境
        if ".venv" in path_str or "venv" in path_str or "virtualenv" in path_str.lower():
            return "项目虚拟环境"
        # 检测是否是Trae/IDE内置Python
        if "trae" in path_str.lower() or ".trae-cn" in path_str:
            return "IDE内置环境"
        return f"系统 Python ({name})"

    # npm全局安装的CLI工具
    if "npm" in path_str.lower() or "node_modules" in path_str.lower():
        return f"npm全局 ({name})"

    # Winget安装的工具
    if "winget" in path_str.lower() or "WinGet" in path_str:
        return f"系统安装 ({name})"

    # PATH中找到的（shutil.which返回的）
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)
    for pd in path_dirs:
        try:
            if pd and p.resolve().parent == Path(pd).resolve():
                return f"PATH中 ({name})"
        except Exception:
            pass

    # AppData下的工具
    if "AppData" in path_str:
        if "Roaming" in path_str:
            return f"用户目录 ({name})"
        if "Local" in path_str:
            return f"本地安装 ({name})"

    # Program Files
    if "Program Files" in path_str:
        return f"系统程序 ({name})"

    # 用户手动指定的其他路径 → 只显示文件名
    return f"已配置 ({name})"


def _safe_path(path: str | Path) -> str:
    """对路径进行脱敏处理（备用，保留原有调用）。"""
    return _describe_path(path)


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
            return Check(True, _safe_path(p))
        # 尝试加扩展名
        for ext in (".exe", ".cmd", ".bat"):
            pe = Path(str(p) + ext)
            if pe.is_file():
                return Check(True, _safe_path(pe))
        return Check(False, f"文件不存在: {_safe_path(path)}")
    # 非完整路径：用shutil.which搜索
    found = shutil.which(path)
    return Check(bool(found), _safe_path(found) if found else f"PATH 中未找到: {path}")


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
        return Check(bool(path), _safe_path(path) if path else f"PATH 中未找到: {name}", required=False)

    def _detect_kling(self) -> Check:
        """检测可灵CLI是否可用，包括登录状态验证。"""
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
        if not kling_exe:
            return Check(False, "未安装（npm i -g @klingai/cli-cn 后 kling login）", required=False)

        # 尝试调用 detect_kling_cli 验证登录状态（短超时）
        try:
            from .providers.kling import detect_kling_cli
            caps = detect_kling_cli(timeout_seconds=8)
            if caps.authentication_state == "authenticated":
                return Check(True, f"已登录可用（{_safe_path(kling_exe)}）", required=False)
            if caps.authentication_state == "not_authenticated":
                return Check(
                    True,
                    f"已安装（{_safe_path(kling_exe)}），需登录后可用",
                    required=False,
                )
            if caps.detection_error:
                return Check(
                    True,
                    f"已安装（{_safe_path(kling_exe)}），{caps.detection_error}",
                    required=False,
                )
        except Exception:
            pass
        return Check(True, f"已安装（{_safe_path(kling_exe)}），需登录后可用", required=False)

    def _detect_jimeng(self) -> Check:
        """轻量检测即梦CLI是否存在。"""
        if self.jimeng_executable:
            return _check_executable(self.jimeng_executable)
        # 搜索PATH
        found = shutil.which("dreamina")
        if found:
            return Check(True, f"已安装（{_safe_path(found)}），需登录后可用", required=False)
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

        # TTS 策略使用稳定的 provider 标识，便于 CLI/GUI 和自动诊断消费；
        # 同时保留不可用原因，避免只展示一个笼统的回退链。
        available_tts = []
        if self.kokoro_available:
            available_tts.append("kokoro（首选）")
        if self.edge_tts_available and self.audio_ffmpeg:
            available_tts.append("edge_tts（在线回退）")
        if self.sapi_available:
            available_tts.append("windows_sapi（最终回退）")
        unavailable_reasons = []
        if not self.edge_tts_available:
            unavailable_reasons.append("edge-tts 未安装")
        elif not self.audio_ffmpeg:
            unavailable_reasons.append("音频转码 FFmpeg 不可用")
        if available_tts:
            strategy_detail = " → ".join(available_tts)
            if unavailable_reasons:
                strategy_detail += f"（{'；'.join(unavailable_reasons)}）"
            tts_strategy = Check(True, strategy_detail, required=False)
        else:
            detail = "无可用TTS引擎"
            if unavailable_reasons:
                detail += f"（{'；'.join(unavailable_reasons)}）"
            tts_strategy = Check(False, detail, required=False)

        # FFmpeg检测
        ffmpeg_check = Check(
            bool(self.audio_ffmpeg),
            str(Path(self.audio_ffmpeg))
            if self.audio_ffmpeg
            else "未找到FFmpeg（winget install Gyan.FFmpeg）",
        )

        # audio_ffmpeg 由完整工具链发现器或调用方显式注入；两种情况下都保持
        # ffmpeg/ffprobe 同目录配对展示，不再用脱敏文案破坏机器可读契约。
        ffprobe_path = ""
        if self.audio_ffmpeg:
            ffmpeg_dir = Path(self.audio_ffmpeg).parent
            suffix = Path(self.audio_ffmpeg).suffix
            ffprobe_path = str(ffmpeg_dir / f"ffprobe{suffix}")
        ffprobe_check = Check(
            bool(ffprobe_path),
            str(Path(ffprobe_path))
            if ffprobe_path
            else "未找到ffprobe（应与ffmpeg同目录）",
        )
        tts_audio_ffmpeg = Check(
            bool(self.audio_ffmpeg),
            str(Path(self.audio_ffmpeg))
            if self.audio_ffmpeg
            else "音频转码 FFmpeg 不可用",
            required=False,
        )

        api_key = bool(os.getenv("OPENROUTER_API_KEY"))
        py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        py_ok = sys.version_info >= (3, 10)

        return DoctorReport(
            {
                "python": Check(py_ok, f"{_safe_path(sys.executable)} (v{py_ver}){'，需 >= 3.10' if not py_ok else ''}"),
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
                "tts_audio_ffmpeg": tts_audio_ffmpeg,
                "tts_strategy": tts_strategy,
            }
        )
