#!/usr/bin/env python
"""AI Content Factory 标准自测脚本

运行此脚本验证代码重构和修复是否正确。
检查项包括：
1. 模块导入是否正常
2. 模块级副作用是否已消除
3. 常量定义是否统一
4. 工具函数是否正常工作
5. 单元测试是否通过
6. CLI 基本功能是否可用
"""
from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path


# 添加项目 src 目录到路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))


class SelfTestResult:
    """自测结果收集器。"""

    def __init__(self):
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.warnings: list[str] = []

    def ok(self, name: str):
        self.passed.append(name)
        print(f"  ✓ {name}")

    def fail(self, name: str, reason: str):
        self.failed.append((name, reason))
        print(f"  ✗ {name}: {reason}")

    def warn(self, message: str):
        self.warnings.append(message)
        print(f"  ⚠ {message}")

    @property
    def total(self) -> int:
        return len(self.passed) + len(self.failed)

    def summary(self) -> bool:
        print("\n" + "=" * 60)
        print(f"自测结果: {len(self.passed)}/{self.total} 通过")
        if self.failed:
            print(f"\n失败项 ({len(self.failed)}):")
            for name, reason in self.failed:
                print(f"  - {name}: {reason}")
        if self.warnings:
            print(f"\n警告 ({len(self.warnings)}):")
            for w in self.warnings:
                print(f"  - {w}")
        print("=" * 60)
        return len(self.failed) == 0


def test_imports(result: SelfTestResult):
    """测试所有核心模块可以正常导入。"""
    print("\n[1/6] 测试模块导入...")

    modules = [
        "aicf",
        "aicf.constants",
        "aicf.path_utils",
        "aicf.subprocess_utils",
        "aicf.logging_utils",
        "aicf.config",
        "aicf.models",
        "aicf.database",
        "aicf.state_machine",
        "aicf.doctor",
        "aicf.providers.openrouter",
        "aicf.providers.jimeng",
        "aicf.providers.kling",
        "aicf.providers.tts",
        "aicf.autopilot",
    ]

    for mod_name in modules:
        try:
            importlib.import_module(mod_name)
            result.ok(f"导入 {mod_name}")
        except Exception as e:
            result.fail(f"导入 {mod_name}", str(e))


def test_no_module_level_side_effects(result: SelfTestResult):
    """测试没有模块级 load_dotenv 副作用。"""
    print("\n[2/6] 测试模块级副作用消除...")

    # 检查模块代码中是否有模块级 load_dotenv 调用（不在函数内）
    src_dir = PROJECT_ROOT / "src" / "aicf"
    modules_to_check = [
        "config.py",
        "doctor.py",
        "providers/openrouter.py",
        "providers/jimeng.py",
        "providers/kling.py",
    ]

    # 允许的入口点文件（这些文件可以调用 load_dotenv，但应该在函数内）
    entry_points = {"__main__.py", "gui.py", "cli.py"}

    for mod_path in modules_to_check:
        full_path = src_dir / mod_path
        if not full_path.exists():
            result.warn(f"文件不存在: {mod_path}")
            continue

        content = full_path.read_text(encoding="utf-8")
        lines = content.split("\n")

        # 检查是否有模块级 load_dotenv 调用（不在函数/方法内）
        in_function = False
        found_module_level_load = False

        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # 简单的缩进检测：顶层代码缩进为0
            if stripped.startswith("def ") or stripped.startswith("class "):
                in_function = True
            elif not stripped.startswith(" ") and not stripped.startswith("\t") and stripped:
                in_function = False

            if "load_dotenv" in stripped and not in_function and not stripped.startswith("#"):
                # 允许导入语句
                if "from dotenv import" in stripped or "import dotenv" in stripped:
                    continue
                found_module_level_load = True
                break

        if found_module_level_load:
            result.fail(f"{mod_path} 无模块级 load_dotenv", f"第 {i} 行发现模块级 load_dotenv 调用")
        else:
            result.ok(f"{mod_path} 无模块级 load_dotenv")


def test_constants_unified(result: SelfTestResult):
    """测试常量是否统一从 constants 模块导入。"""
    print("\n[3/6] 测试常量统一...")

    from aicf import constants

    # 检查关键常量是否存在
    expected_constants = [
        "IMAGE_EXTENSIONS",
        "VIDEO_EXTENSIONS",
        "COMMON_PENDING_STATES",
        "COMMON_SUCCESS_STATES",
        "COMMON_FAILURE_STATES",
        "OPENROUTER_API_BASE_URL",
        "OPENROUTER_MODELS_URL",
        "AUTOPILOT_MAX_RETRIES",
    ]

    for const_name in expected_constants:
        if hasattr(constants, const_name):
            result.ok(f"constants.{const_name} 存在")
        else:
            result.fail(f"constants.{const_name}", "常量不存在")

    # 检查状态集合互不相交
    if (
        constants.COMMON_PENDING_STATES & constants.COMMON_SUCCESS_STATES == set()
        and constants.COMMON_PENDING_STATES & constants.COMMON_FAILURE_STATES == set()
        and constants.COMMON_SUCCESS_STATES & constants.COMMON_FAILURE_STATES == set()
    ):
        result.ok("状态集合互不相交")
    else:
        result.fail("状态集合互不相交", "pending/success/failure 状态集合有重叠")


def test_path_utils(result: SelfTestResult):
    """测试 path_utils 工具函数。"""
    print("\n[4/6] 测试 path_utils 功能...")

    from aicf.path_utils import (
        project_root,
        python_executable,
        describe_path,
        load_project_env,
    )

    # project_root
    root = project_root()
    if root.exists() and (root / "src").exists():
        result.ok("project_root() 返回正确路径")
    else:
        result.fail("project_root()", "返回的路径不正确")

    # python_executable
    exe = python_executable()
    if Path(exe).exists() and "python" in Path(exe).name.lower():
        result.ok("python_executable() 返回有效路径")
    else:
        result.fail("python_executable()", "返回的路径无效")

    # describe_path - 项目内文件
    test_file = root / "src" / "aicf" / "path_utils.py"
    desc = describe_path(test_file)
    # Windows 使用 \，Unix 使用 /，都接受
    expected_parts = ["src", "aicf", "path_utils.py"]
    desc_normalized = desc.replace("\\", "/")
    if all(part in desc_normalized for part in expected_parts):
        result.ok("describe_path() 项目内文件脱敏正确")
    else:
        result.fail("describe_path() 项目内文件", f"返回: {desc}")

    # describe_path - 用户目录
    user_path = Path("C:/Users/TestUser/Documents/test.txt")
    user_desc = describe_path(user_path)
    if "用户目录" in user_desc:
        result.ok("describe_path() 用户目录脱敏正确")
    else:
        result.fail("describe_path() 用户目录", f"返回: {user_desc}")

    # load_project_env 返回 bool
    from aicf.path_utils import reset_env_loaded_state
    reset_env_loaded_state()
    env_result = load_project_env()
    reset_env_loaded_state()
    if isinstance(env_result, bool):
        result.ok("load_project_env() 返回 bool")
    else:
        result.fail("load_project_env()", f"返回类型: {type(env_result)}")


def test_models_exports(result: SelfTestResult):
    """测试 models 包导出完整。"""
    print("\n[5/6] 测试 models 导出...")

    from aicf import models

    expected_exports = [
        "DirectionProfile",
        "TopicCandidate",
        "TopicCandidates",
        "ResearchFact",
        "ResearchResult",
        "ScriptSegment",
        "ScriptResult",
        "VisualShot",
        "VisualPlan",
        "ReviewScores",
        "ReviewResult",
        "PlatformCopy",
        "PackageResult",
        "SUPPORTED_PLATFORMS",
    ]

    for export_name in expected_exports:
        if hasattr(models, export_name):
            result.ok(f"models.{export_name} 已导出")
        else:
            result.fail(f"models.{export_name}", "未在 __init__.py 中导出")


def test_unit_tests(result: SelfTestResult):
    """运行单元测试。"""
    print("\n[6/6] 运行单元测试...")

    test_files = [
        "tests/test_constants.py",
        "tests/test_path_utils.py",
    ]

    os.chdir(PROJECT_ROOT)
    venv_python = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"

    for test_file in test_files:
        if not (PROJECT_ROOT / test_file).exists():
            result.warn(f"测试文件不存在: {test_file}")
            continue

        cmd = [str(venv_python), "-m", "pytest", test_file, "-v", "--tb=short"]
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=str(PROJECT_ROOT))

        if proc.returncode == 0:
            result.ok(f"{test_file} 通过")
        else:
            result.fail(f"{test_file}", f"退出码 {proc.returncode}\n{proc.stdout[-500:]}\n{proc.stderr[-500:]}")


def test_subprocess_utils(result: SelfTestResult):
    """测试 subprocess_utils 存在且可用。"""
    print("\n[额外] 测试 subprocess_utils...")

    try:
        from aicf.subprocess_utils import silent_run, silent_popen, CREATE_NO_WINDOW
        result.ok("subprocess_utils.silent_run 存在")
        result.ok("subprocess_utils.silent_popen 存在")

        # 测试 silent_run 能执行简单命令
        proc = silent_run(
            [sys.executable, "-c", "print('hello')"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and "hello" in proc.stdout:
            result.ok("silent_run 能执行命令")
        else:
            result.fail("silent_run 执行命令", f"返回码: {proc.returncode}")
    except Exception as e:
        result.fail("subprocess_utils 导入/执行", str(e))


def test_preflight_module(result: SelfTestResult):
    """测试 preflight 健康检查模块可导入并正常工作。"""
    print("\n[额外] 测试 preflight 健康检查模块...")
    
    try:
        from aicf.preflight import run_preflight_checks, HealthCheckResult, HealthCheckIssue
        result.ok("preflight 模块可导入")
        
        # 运行检查（不测试模型连通性，避免网络依赖）
        check_result = run_preflight_checks(check_model_reachability=False, check_ffmpeg=True)
        if isinstance(check_result, HealthCheckResult):
            result.ok("run_preflight_checks 返回 HealthCheckResult")
            
            # 错误应该只包含配置类问题（如API Key未配置是正常的）
            errors = check_result.errors()
            non_config_errors = [e for e in errors if e.category != "配置"]
            if len(non_config_errors) == 0:
                result.ok("preflight 检查无系统性错误（API Key未配置属正常）")
            else:
                for err in non_config_errors:
                    result.fail(f"preflight: {err.category}", err.message)
        else:
            result.fail("run_preflight_checks", f"返回类型错误: {type(check_result)}")
    except Exception as e:
        result.fail("preflight 模块导入/执行", str(e))


def test_path_consistency(result: SelfTestResult):
    """检查代码中是否存在路径硬编码不一致问题。"""
    print("\n[额外] 检查路径硬编码一致性...")
    
    import re
    
    src_dir = PROJECT_ROOT / "src" / "aicf"
    issues_found = []
    
    # 扫描所有Python文件
    for py_file in src_dir.rglob("*.py"):
        try:
            content = py_file.read_text(encoding="utf-8")
            rel_path = py_file.relative_to(PROJECT_ROOT)
            
            # 检查是否有直接硬编码 "outputs" / job_id 的路径（可能导致不一致）
            # 排除已知正确的位置：
            # - user_output_root = root / "outputs" (最终用户输出目录，正确)
            # - final_video_for_job 的 user_output_dir 参数 (正确)
            # - 删除任务时同时清理两个目录 (正确)
            # - 打开输出文件夹功能 (正确)
            
            lines = content.split("\n")
            for i, line in enumerate(lines, 1):
                # 跳过注释
                if line.strip().startswith("#"):
                    continue
                
                # 检查是否有错误地将中间产物写到 outputs 的模式
                # 模式: "outputs" / job_id / xxx.json (非最终视频文件)
                if '/ "outputs"' in line and '/ job_id' in line:
                    # 检查是不是已知的正确位置
                    is_user_output = (
                        "final_video_for_job" in line 
                        or "_open_outputs_folder" in line 
                        or "_delete_selected_job" in line
                        or "output_dir =" in line and "最终视频" in lines[max(0,i-5):i+5]
                        or "user_output_root" in line
                        or "_open_final_video" in lines[max(0,i-10):i+2]
                    )
                    if not is_user_output:
                        # 进一步检查是不是.json等中间产物文件
                        if any(ext in line for ext in [".json", "_request", "_sources", "research_"]):
                            issues_found.append(f"{rel_path}:{i}: 可能错误地将中间产物路径硬编码到 outputs 目录")
        except (OSError, UnicodeDecodeError):
            continue
    
    if issues_found:
        for issue in issues_found[:5]:  # 最多显示5个
            result.fail("路径一致性检查", issue)
        if len(issues_found) > 5:
            result.warn(f"还有 {len(issues_found)-5} 个类似路径问题")
    else:
        result.ok("未发现明显的路径硬编码不一致问题")


def main():
    print("=" * 60)
    print("AI Content Factory 标准自测")
    print("=" * 60)

    result = SelfTestResult()

    test_imports(result)
    test_no_module_level_side_effects(result)
    test_constants_unified(result)
    test_path_utils(result)
    test_models_exports(result)
    test_subprocess_utils(result)
    test_preflight_module(result)
    test_path_consistency(result)
    test_unit_tests(result)

    success = result.summary()

    if success:
        print("\n所有自测通过！代码重构和修复验证成功。")
        return 0
    else:
        print("\n存在失败项，请检查上述错误。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
