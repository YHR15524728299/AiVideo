"""path_utils 模块测试。"""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import pytest

from aicf.path_utils import (
    describe_path,
    load_project_env,
    project_root,
    python_executable,
    reset_env_loaded_state,
    reset_project_root,
)


@pytest.fixture(autouse=True)
def _clean_project_root_env(monkeypatch):
    """每个测试前清除 AICF_PROJECT_ROOT 环境变量，避免测试间污染。"""
    monkeypatch.delenv("AICF_PROJECT_ROOT", raising=False)
    reset_project_root()
    reset_env_loaded_state()
    yield
    reset_project_root()
    reset_env_loaded_state()


class TestProjectRoot:
    """测试 project_root() 函数。"""

    def test_returns_existing_directory(self):
        """project_root() 应该返回一个存在的目录。"""
        root = project_root()
        assert root.exists()
        assert root.is_dir()

    def test_contains_src_directory(self):
        """project_root() 应该包含 src 目录。"""
        root = project_root()
        assert (root / "src").exists()

    def test_env_override(self):
        """AICF_PROJECT_ROOT 环境变量应该可以覆盖默认值。"""
        custom_path = Path("C:/custom/root")
        with mock.patch.dict(os.environ, {"AICF_PROJECT_ROOT": str(custom_path)}):
            assert project_root() == custom_path


class TestPythonExecutable:
    """测试 python_executable() 函数。"""

    def test_returns_existing_file(self):
        """python_executable() 应该返回一个存在的可执行文件。"""
        exe = python_executable()
        assert Path(exe).exists()

    def test_returns_python(self):
        """python_executable() 应该指向 Python 解释器。"""
        exe = python_executable()
        assert "python" in Path(exe).name.lower()


class TestDescribePath:
    """测试 describe_path() 路径脱敏函数。"""

    def test_project_file_returns_relative_path(self):
        """项目内的文件应该返回相对路径。"""
        root = project_root()
        test_path = root / "src" / "aicf" / "path_utils.py"
        result = describe_path(test_path)
        # Windows 使用 \，Unix 使用 /，统一为 / 比较
        result_normalized = result.replace("\\", "/")
        assert result_normalized == "src/aicf/path_utils.py"

    def test_handles_user_directory(self):
        """用户目录下的文件应该显示用户目录占位符。"""
        # 模拟用户目录路径
        test_path = Path("C:/Users/TestUser/Documents/file.txt")
        result = describe_path(test_path)
        assert "用户目录" in result

    def test_accepts_string_input(self):
        """应该接受字符串路径输入。"""
        root = project_root()
        test_path = str(root / "README.md")
        result = describe_path(test_path)
        assert "README.md" in result

    def test_accepts_path_input(self):
        """应该接受 Path 对象输入。"""
        root = project_root()
        test_path = root / "README.md"
        result = describe_path(test_path)
        assert "README.md" in result


class TestLoadProjectEnv:
    """测试 load_project_env() 环境加载函数。"""

    def test_is_idempotent(self):
        """多次调用 load_project_env()，第二次应该返回 False（已加载）。"""
        result1 = load_project_env()
        result2 = load_project_env()
        # 第一次调用返回 bool（True 或 False 取决于是否安装了 dotenv）
        assert isinstance(result1, bool)
        # 第二次调用应该返回 False，因为已经加载过了
        assert result2 is False

    def test_returns_bool(self):
        """应该返回布尔值表示是否加载了 .env。"""
        result = load_project_env()
        assert isinstance(result, bool)


class TestImports:
    """测试模块可以正常导入。"""

    def test_import_constants(self):
        """应该能正常导入 constants 模块。"""
        from aicf import constants
        assert hasattr(constants, "IMAGE_EXTENSIONS")
        assert hasattr(constants, "VIDEO_EXTENSIONS")

    def test_import_subprocess_utils(self):
        """应该能正常导入 subprocess_utils 模块。"""
        from aicf import subprocess_utils
        assert hasattr(subprocess_utils, "silent_run")
        assert hasattr(subprocess_utils, "silent_popen")

    def test_import_models(self):
        """应该能正常导入 models 包的所有导出。"""
        from aicf import models
        assert hasattr(models, "DirectionProfile")
        assert hasattr(models, "TopicCandidate")
        assert hasattr(models, "VisualPlan")
        assert hasattr(models, "VisualShot")
        assert hasattr(models, "PackageResult")
        assert hasattr(models, "ResearchResult")
        assert hasattr(models, "ScriptResult")
        assert hasattr(models, "ReviewResult")

    def test_models_supported_platforms(self):
        """SUPPORTED_PLATFORMS 应该是一个非空元组。"""
        from aicf.models import SUPPORTED_PLATFORMS
        assert isinstance(SUPPORTED_PLATFORMS, tuple)
        assert len(SUPPORTED_PLATFORMS) > 0
        assert "douyin" in SUPPORTED_PLATFORMS
