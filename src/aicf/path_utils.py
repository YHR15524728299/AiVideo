"""项目路径和环境初始化工具。

统一管理项目根路径计算、Python 可执行文件路径、路径脱敏和 .env 文件加载，避免多处重复实现。

注意：此模块不应该在导入时自动加载 .env，环境加载由应用入口点（__main__.py 或 gui.launch()）显式调用。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_PROJECT_ROOT: Path | None = None
_env_loaded = False


def project_root() -> Path:
    """返回项目根目录（aicf 包的父目录的父目录）。
    
    支持通过 AICF_PROJECT_ROOT 环境变量覆盖。
    基于文件位置计算，比 cwd() 更可靠。
    
    注意：环境变量 AICF_PROJECT_ROOT 的变化会立即生效，不会永久缓存。
    """
    global _PROJECT_ROOT
    
    # 每次调用都检查环境变量（允许测试中 monkeypatch 生效）
    configured = os.getenv("AICF_PROJECT_ROOT")
    if configured:
        _PROJECT_ROOT = Path(configured)
        return _PROJECT_ROOT
    
    # 没有环境变量时使用文件位置计算（缓存）
    if _PROJECT_ROOT is not None:
        return _PROJECT_ROOT
    
    # src/aicf/path_utils.py -> src/aicf -> src -> project root
    _PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
    return _PROJECT_ROOT


def reset_project_root() -> None:
    """重置项目根目录缓存（仅用于测试）。"""
    global _PROJECT_ROOT
    _PROJECT_ROOT = None


def python_executable() -> str:
    """返回当前 Python 环境的可执行文件路径。
    
    在 Windows 上，如果是 pythonw.exe（无控制台），自动替换为 python.exe。
    """
    exe = sys.executable
    if sys.platform == "win32" and exe.lower().endswith("pythonw.exe"):
        exe = exe[:-len("pythonw.exe")] + "python.exe"
    return exe


def describe_path(path: str | Path) -> str:
    """将绝对路径转换为友好的位置描述，不暴露个人信息。
    
    用于日志和诊断输出，避免泄露用户目录等敏感路径信息。
    """
    p = Path(str(path))
    path_str = str(p)
    name = p.name
    root = project_root()

    # 项目内文件 → 相对路径
    try:
        rel = p.resolve().relative_to(root.resolve())
        return str(rel)
    except (ValueError, OSError):
        pass

    # 用户目录 (C:/Users/xxx/...)
    if "Users" in path_str or "users" in path_str:
        # 提取用户目录后的部分
        parts = p.parts
        if len(parts) >= 3 and parts[1].lower() == "users":
            # C:/Users/Username/... → 用户目录/...
            rest = "/".join(parts[3:]) if len(parts) > 3 else name
            return f"用户目录/{rest}" if rest else "用户目录"

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


def load_project_env(override: bool = False) -> bool:
    """加载项目根目录下的 .env 文件（幂等，只加载一次）。
    
    Args:
        override: 是否覆盖已存在的环境变量，默认 False。
    
    Returns:
        bool: 如果成功加载了 .env 文件返回 True，否则（已加载过或 dotenv 未安装）返回 False。
    """
    global _env_loaded
    if _env_loaded:
        return False
    
    loaded = False
    try:
        from dotenv import load_dotenv
        load_dotenv(project_root() / ".env", override=override)
        loaded = True
    except ImportError:
        # python-dotenv 未安装时静默跳过
        pass
    
    _env_loaded = True
    return loaded


def reset_env_loaded_state() -> None:
    """重置环境加载状态（仅用于测试）。"""
    global _env_loaded
    _env_loaded = False
