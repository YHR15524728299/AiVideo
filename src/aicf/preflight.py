"""
启动前健康检查模块 - 在GUI启动或任务开始前自动检测关键依赖和配置。

防止出现P0级阻断问题：
- API Key未配置
- 模型不可用/不是免费模型
- FFmpeg缺失
- 目录权限问题
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from aicf.constants import (
    OPENROUTER_API_BASE_URL,
    OPENROUTER_DEFAULT_MODEL,
    OPENROUTER_FALLBACK_MODELS,
)
from aicf.path_utils import project_root
from aicf.subprocess_utils import silent_run


@dataclass
class HealthCheckIssue:
    level: str  # "error" | "warning"
    category: str
    message: str
    fix_hint: str | None = None


@dataclass
class HealthCheckResult:
    ok: bool
    issues: list[HealthCheckIssue] = field(default_factory=list)
    
    def errors(self) -> list[HealthCheckIssue]:
        return [i for i in self.issues if i.level == "error"]
    
    def warnings(self) -> list[HealthCheckIssue]:
        return [i for i in self.issues if i.level == "warning"]
    
    def summary(self) -> str:
        lines = []
        errors = self.errors()
        warnings = self.warnings()
        if errors:
            lines.append(f"发现 {len(errors)} 个严重问题：")
            for i, err in enumerate(errors, 1):
                lines.append(f"  {i}. [{err.category}] {err.message}")
                if err.fix_hint:
                    lines.append(f"     修复: {err.fix_hint}")
        if warnings:
            if lines:
                lines.append("")
            lines.append(f"发现 {len(warnings)} 个警告：")
            for i, warn in enumerate(warnings, 1):
                lines.append(f"  {i}. [{warn.category}] {warn.message}")
                if warn.fix_hint:
                    lines.append(f"     提示: {warn.fix_hint}")
        return "\n".join(lines)


def run_preflight_checks(
    *,
    check_model_reachability: bool = True,
    check_ffmpeg: bool = True,
) -> HealthCheckResult:
    """运行所有启动前健康检查。
    
    Args:
        check_model_reachability: 是否实际测试OpenRouter模型连通性（可能需要几秒钟）
        check_ffmpeg: 是否检查FFmpeg可用性
    """
    issues: list[HealthCheckIssue] = []
    
    # 1. 检查OpenRouter API Key
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        issues.append(HealthCheckIssue(
            level="error",
            category="配置",
            message="OPENROUTER_API_KEY 未配置",
            fix_hint="请在 .env 文件中设置 OPENROUTER_API_KEY 或在系统环境变量中配置",
        ))
    elif len(api_key) < 20:
        issues.append(HealthCheckIssue(
            level="warning",
            category="配置",
            message="OPENROUTER_API_KEY 格式可能不正确",
            fix_hint="请检查 API Key 是否完整",
        ))
    
    # 2. 检查配置的模型
    configured_model = os.getenv("OPENROUTER_MODEL", OPENROUTER_DEFAULT_MODEL).strip()
    if not configured_model.endswith(":free"):
        issues.append(HealthCheckIssue(
            level="error",
            category="模型配置",
            message=f"当前配置的模型 '{configured_model}' 不是免费模型",
            fix_hint="请将 OPENROUTER_MODEL 设置为以 ':free' 结尾的免费模型，"
                    f"推荐使用: {OPENROUTER_DEFAULT_MODEL}",
        ))
    
    # 3. 检查模型可用性（如果API Key已配置）
    if api_key and check_model_reachability and configured_model.endswith(":free"):
        model_ok = _verify_model_available(api_key, configured_model)
        if not model_ok:
            # 尝试fallback模型
            fallback_found = None
            for fb_model in OPENROUTER_FALLBACK_MODELS:
                if fb_model == configured_model:
                    continue
                if _verify_model_available(api_key, fb_model, timeout=10):
                    fallback_found = fb_model
                    break
            
            if fallback_found:
                issues.append(HealthCheckIssue(
                    level="warning",
                    category="模型可用性",
                    message=f"配置的模型 '{configured_model}' 当前不可用，"
                            f"运行时将自动尝试备用模型 '{fallback_found}'",
                    fix_hint=f"建议在 .env 中将 OPENROUTER_MODEL 改为 '{fallback_found}'",
                ))
            else:
                issues.append(HealthCheckIssue(
                    level="error",
                    category="模型可用性",
                    message=f"配置的模型 '{configured_model}' 不可用，且未找到可用的备用模型",
                    fix_hint="请检查网络连接或稍后重试，OpenRouter免费模型可能临时不可用",
                ))
    
    # 4. 检查FFmpeg
    if check_ffmpeg:
        ffmpeg_ok = _check_ffmpeg_available()
        if not ffmpeg_ok:
            issues.append(HealthCheckIssue(
                level="error",
                category="依赖工具",
                message="未找到 FFmpeg/FFprobe",
                fix_hint="请安装 FFmpeg 并确保 ffmpeg/ffprobe 在 PATH 中，"
                        "或运行 scripts/doctor.ps1 进行诊断",
            ))
    
    # 5. 检查关键目录权限
    root = project_root()
    dirs_to_check = [
        root / "data",
        root / "data" / "jobs",
        root / "outputs",
    ]
    for d in dirs_to_check:
        try:
            d.mkdir(parents=True, exist_ok=True)
            # 测试写入权限
            test_file = d / ".write_test"
            test_file.write_text("ok", encoding="utf-8")
            test_file.unlink()
        except OSError as e:
            issues.append(HealthCheckIssue(
                level="error",
                category="文件系统",
                message=f"目录 {d} 不可写: {e}",
                fix_hint="请检查目录权限或关闭占用该目录的程序",
            ))
    
    return HealthCheckResult(
        ok=len([i for i in issues if i.level == "error"]) == 0,
        issues=issues,
    )


def _verify_model_available(api_key: str, model: str, *, timeout: float = 15.0) -> bool:
    """快速验证模型是否可用（发送一个极小的请求）。"""
    try:
        body = json.dumps({
            "model": model,
            "messages": [{"role": "user", "content": "OK"}],
            "max_tokens": 1,
        }).encode("utf-8")
        
        req = Request(
            f"{OPENROUTER_API_BASE_URL}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        
        with urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                return True
            return False
    except (HTTPError, URLError, TimeoutError, OSError):
        return False
    except Exception:
        return False


def _check_ffmpeg_available() -> bool:
    """检查FFmpeg和FFprobe是否在PATH中可用。"""
    try:
        result = silent_run(
            ["ffprobe", "-version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def get_recommended_model() -> str:
    """获取推荐的可用免费模型。"""
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return OPENROUTER_DEFAULT_MODEL
    
    # 先尝试默认模型
    if _verify_model_available(api_key, OPENROUTER_DEFAULT_MODEL, timeout=10):
        return OPENROUTER_DEFAULT_MODEL
    
    # 再尝试fallback
    for model in OPENROUTER_FALLBACK_MODELS:
        if _verify_model_available(api_key, model, timeout=10):
            return model
    
    return OPENROUTER_DEFAULT_MODEL  # 都不行就返回默认
