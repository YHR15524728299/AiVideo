"""全局公共常量。

集中管理跨模块重复使用的常量，避免多处重复定义导致不一致。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 媒体文件扩展名
# ---------------------------------------------------------------------------

IMAGE_EXTENSIONS: frozenset[str] = frozenset({".png", ".jpg", ".jpeg", ".webp"})
VIDEO_EXTENSIONS: frozenset[str] = frozenset({".mp4", ".mov", ".mkv", ".webm"})


def is_image_path(path: str) -> bool:
    """判断路径是否指向图片文件。"""
    from pathlib import Path
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def is_video_path(path: str) -> bool:
    """判断路径是否指向视频文件。"""
    from pathlib import Path
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


# ---------------------------------------------------------------------------
# 异步任务状态（各 Provider 通用状态）
# ---------------------------------------------------------------------------

# 等待中/处理中状态（两个 Provider 共有）
COMMON_PENDING_STATES: frozenset[str] = frozenset({
    "queued",
    "queueing",
    "processing",
    "generating",
    "waiting",
})

# 成功状态
COMMON_SUCCESS_STATES: frozenset[str] = frozenset({
    "success",
    "succeeded",
    "completed",
    "done",
})

# 失败状态（包含两种拼写）
COMMON_FAILURE_STATES: frozenset[str] = frozenset({
    "failure",
    "failed",
    "error",
    "cancelled",
    "canceled",  # 美式拼写
})


# ---------------------------------------------------------------------------
# 自动驾驶重试配置
# ---------------------------------------------------------------------------

AUTOPILOT_MAX_RETRIES: int = 5
AUTOPILOT_RETRY_BACKOFF_BASE_SECONDS: float = 5.0
AUTOPILOT_RETRY_MAX_WAIT_SECONDS: float = 120.0


# ---------------------------------------------------------------------------
# OpenRouter API 配置
# ---------------------------------------------------------------------------

OPENROUTER_API_BASE_URL: str = "https://openrouter.ai/api/v1"
OPENROUTER_MODELS_URL: str = f"{OPENROUTER_API_BASE_URL}/models"

# 默认免费模型（经过验证可用）
OPENROUTER_DEFAULT_MODEL: str = "nvidia/nemotron-3-super-120b-a12b:free"

# 模型fallback列表：当主模型返回provider错误/404时按顺序尝试
# 按能力从强到弱排序，都是经过验证可用的免费模型
OPENROUTER_FALLBACK_MODELS: tuple[str, ...] = (
    "nvidia/nemotron-3-super-120b-a12b:free",
    "google/gemma-4-26b-a4b-it:free",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "nvidia/nemotron-3-nano-30b-a3b:free",
)
