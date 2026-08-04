"""根据任务状态推导用户当前可执行的安全操作。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


AUTO_REOPEN_ERROR_MARKERS = (
    "http ",
    "url ",
    "timeout",
    "connection",
    "不可达",
    "403",
    "429",
    "502",
    "503",
    "504",
    "拦截",
    "验证",
)


@dataclass(frozen=True)
class JobActionState:
    can_start: bool
    can_resume: bool
    can_stop: bool
    can_open_video: bool
    guidance: str


def derive_job_actions(
    *,
    existing_job: bool,
    current_stage: str = "",
    failed_stage: str = "",
    recoverable: bool = False,
    job_is_running: bool = False,
    app_has_running_job: bool = False,
    has_final_video: bool = False,
) -> JobActionState:
    """把后台状态转换为互斥、面向用户的下一步操作。"""
    can_open_video = existing_job and has_final_video
    if app_has_running_job or job_is_running:
        guidance = (
            "任务运行中，可关闭窗口；需要中止时点击“停止”。"
            if job_is_running
            else "已有任务正在后台运行；可查看历史任务，停止操作仍针对后台任务。"
        )
        return JobActionState(
            can_start=False,
            can_resume=False,
            can_stop=True,
            can_open_video=can_open_video,
            guidance=guidance,
        )

    if not existing_job:
        return JobActionState(
            can_start=True,
            can_resume=False,
            can_stop=False,
            can_open_video=False,
            guidance="新任务已就绪，填写内容方向后点击“开始生成”。",
        )

    if current_stage == "COMPLETED":
        guidance = (
            "任务已完成，可打开最终视频。"
            if can_open_video
            else "任务已完成，最终视频尚未找到，请打开输出目录检查。"
        )
        return JobActionState(
            can_start=False,
            can_resume=False,
            can_stop=False,
            can_open_video=can_open_video,
            guidance=guidance,
        )

    if current_stage == "FAILED_NEEDS_ATTENTION":
        if recoverable:
            return JobActionState(
                can_start=False,
                can_resume=True,
                can_stop=False,
                can_open_video=can_open_video,
                guidance="临时服务错误已可重试，点击“继续/恢复”。",
            )
        return JobActionState(
            can_start=False,
            can_resume=False,
            can_stop=False,
            can_open_video=can_open_video,
            guidance="任务失败且需要人工处理，请查看日志中的修复提示。",
        )

    can_resume = (
        current_stage == "FAILED_RETRYABLE"
        or bool(failed_stage and recoverable)
        or current_stage not in {"", "INIT"}
    )
    if can_resume:
        reason = (
            "任务可恢复，点击“继续/恢复”。"
            if current_stage == "FAILED_RETRYABLE" or failed_stage
            else "任务异常中断，点击“继续/恢复”从断点继续。"
        )
        return JobActionState(
            can_start=False,
            can_resume=True,
            can_stop=False,
            can_open_video=can_open_video,
            guidance=reason,
        )

    return JobActionState(
        can_start=False,
        can_resume=False,
        can_stop=False,
        can_open_video=can_open_video,
        guidance="该任务已初始化；如需重新制作，请点击“新建任务”。",
    )


def failed_attention_can_auto_reopen(
    failed_stage: str,
    stages: Mapping[str, Any],
) -> bool:
    """与 CLI 共用的临时外部服务错误判定。"""
    record = stages.get(failed_stage)
    if not isinstance(record, Mapping):
        return False
    error_message = str(record.get("error", "")).lower()
    return any(marker in error_message for marker in AUTO_REOPEN_ERROR_MARKERS)


def job_storage_exists(job_dir: Path, output_dir: Path) -> bool:
    """数据库之外存在任务或交付目录时，也视为已有任务。"""
    return job_dir.exists() or output_dir.exists()


def first_available_job_id(
    base: str,
    is_taken: Callable[[str], bool],
) -> str:
    """返回稳定、可读且不冲突的任务 ID。"""
    if not is_taken(base):
        return base
    suffix = 2
    while is_taken(f"{base}-{suffix}"):
        suffix += 1
    return f"{base}-{suffix}"
