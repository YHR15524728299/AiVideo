"""公共常量模块测试。"""
from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _clean_project_root_env(monkeypatch):
    """每个测试前清除 AICF_PROJECT_ROOT 环境变量，避免测试间污染。"""
    # 保存并清除环境变量
    monkeypatch.delenv("AICF_PROJECT_ROOT", raising=False)
    # 重置 project_root 缓存
    import aicf.path_utils as pu
    original_cache = pu._PROJECT_ROOT
    pu._PROJECT_ROOT = None
    yield
    pu._PROJECT_ROOT = original_cache


class TestMediaExtensions:
    """测试媒体文件扩展名常量。"""

    def test_image_extensions_contains_common_formats(self):
        from aicf.constants import IMAGE_EXTENSIONS
        assert ".png" in IMAGE_EXTENSIONS
        assert ".jpg" in IMAGE_EXTENSIONS
        assert ".jpeg" in IMAGE_EXTENSIONS
        assert ".webp" in IMAGE_EXTENSIONS

    def test_video_extensions_contains_common_formats(self):
        from aicf.constants import VIDEO_EXTENSIONS
        assert ".mp4" in VIDEO_EXTENSIONS
        assert ".mov" in VIDEO_EXTENSIONS
        assert ".mkv" in VIDEO_EXTENSIONS
        assert ".webm" in VIDEO_EXTENSIONS

    def test_extensions_are_lowercase(self):
        from aicf.constants import IMAGE_EXTENSIONS, VIDEO_EXTENSIONS
        for ext in IMAGE_EXTENSIONS | VIDEO_EXTENSIONS:
            assert ext == ext.lower(), f"扩展名 {ext} 应该是小写"
            assert ext.startswith("."), f"扩展名 {ext} 应该以点开头"


class TestTaskStates:
    """测试任务状态常量。"""

    def test_pending_states_exist(self):
        from aicf.constants import COMMON_PENDING_STATES
        assert "queued" in COMMON_PENDING_STATES
        assert "generating" in COMMON_PENDING_STATES
        assert "processing" in COMMON_PENDING_STATES

    def test_success_states_exist(self):
        from aicf.constants import COMMON_SUCCESS_STATES
        assert "success" in COMMON_SUCCESS_STATES
        assert "succeeded" in COMMON_SUCCESS_STATES
        assert "completed" in COMMON_SUCCESS_STATES
        assert "done" in COMMON_SUCCESS_STATES

    def test_failure_states_exist(self):
        from aicf.constants import COMMON_FAILURE_STATES
        assert "failed" in COMMON_FAILURE_STATES
        assert "failure" in COMMON_FAILURE_STATES
        assert "error" in COMMON_FAILURE_STATES
        assert "cancelled" in COMMON_FAILURE_STATES
        assert "canceled" in COMMON_FAILURE_STATES  # 两种拼写都支持

    def test_states_are_disjoint(self):
        """pending/success/failure 状态集合应该互不相交。"""
        from aicf.constants import COMMON_PENDING_STATES, COMMON_SUCCESS_STATES, COMMON_FAILURE_STATES
        assert COMMON_PENDING_STATES & COMMON_SUCCESS_STATES == set()
        assert COMMON_PENDING_STATES & COMMON_FAILURE_STATES == set()
        assert COMMON_SUCCESS_STATES & COMMON_FAILURE_STATES == set()


class TestPublicAPI:
    """测试公共 API 函数。"""

    def test_describe_path_exists(self):
        from aicf.path_utils import describe_path
        assert callable(describe_path)

    def test_project_root_returns_path(self):
        from aicf.path_utils import project_root
        root = project_root()
        assert isinstance(root, Path)
        assert (root / "src" / "aicf").is_dir()

    def test_python_executable_returns_string(self):
        from aicf.path_utils import python_executable
        exe = python_executable()
        assert isinstance(exe, str)
        assert len(exe) > 0


class TestAutoPilotConstants:
    """测试自动驾驶常量。"""

    def test_max_retries_is_positive(self):
        from aicf.constants import AUTOPILOT_MAX_RETRIES
        assert AUTOPILOT_MAX_RETRIES > 0
