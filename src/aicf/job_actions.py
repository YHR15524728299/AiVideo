"""根据任务状态推导用户当前可执行的安全操作。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Collection, Mapping

from .job_service import ResumeAction, ResumeDecision, ResumeMode
from .state_machine import is_terminal_stage


@dataclass(frozen=True)
class JobActionState:
    can_start: bool
    can_resume: bool
    can_stop: bool
    can_open_video: bool
    guidance: str
    can_retry_research: bool = False
    can_view_research_failure: bool = False
    resume_mode: ResumeMode | None = None


def constrain_actions_for_running_job(
    actions: JobActionState,
    *,
    selected_job_id: str,
    running_job_id: str,
) -> JobActionState:
    """叠加应用级运行约束，同时保留当前选择可安全执行的只读动作。"""
    if not running_job_id:
        return actions
    is_selected_running = selected_job_id == running_job_id
    return JobActionState(
        can_start=False,
        can_resume=False,
        can_stop=True,
        can_open_video=actions.can_open_video,
        guidance=(
            "任务运行中，可关闭窗口；需要中止时点击“停止”。"
            if is_selected_running
            else "已有任务正在后台运行；可查看历史任务，停止操作仍针对后台任务。"
        ),
    )


def should_recover_zombie_job(
    *,
    current_stage: str,
    failed_stage: str,
    completed_stages: Collection[str],
    worker_record: Any | None = None,
) -> bool:
    """仅按持久化状态判断任务是否可能需要僵尸恢复。

    增加 worker_record 检查：如果 worker 已正常结束（有 finished_at），不做僵尸恢复。
    """
    # Worker 已正常结束，不需要恢复
    if worker_record is not None:
        finished_at = getattr(worker_record, "finished_at", None)
        terminal_status = getattr(worker_record, "terminal_status", None)
        stop_requested_at = getattr(worker_record, "stop_requested_at", None)
        if finished_at is not None or stop_requested_at is not None or terminal_status in ("COMPLETED", "STOP_REQUESTED", "FORCE_STOPPED", "FAILED", "EMERGENCY_EXIT"):
            return False

    return bool(
        current_stage
        and not is_terminal_stage(current_stage)
        and not failed_stage
        and current_stage not in {"", "INIT"}
        and current_stage not in completed_stages
    )


def derive_job_actions(
    *,
    existing_job: bool,
    current_stage: str = "",
    failed_stage: str = "",
    job_is_running: bool = False,
    app_has_running_job: bool = False,
    has_final_video: bool = False,
    research_failure_summary: str = "",
    resume_decision: ResumeDecision | None = None,
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
        decision = resume_decision
        if decision is None:
            return JobActionState(
                can_start=False,
                can_resume=False,
                can_stop=False,
                can_open_video=can_open_video,
                guidance="任务恢复状态未知，已安全禁用恢复操作。",
            )
        return JobActionState(
            can_start=False,
            can_resume=decision.permits(ResumeAction.START_WORKER),
            can_stop=False,
            can_open_video=can_open_video,
            guidance=decision.reason or (
                "临时服务错误已可重试，点击“继续/恢复”。"
                if decision.permits(ResumeAction.START_WORKER)
                else "任务需要人工确认并重开，当前不会直接启动。"
            ),
            resume_mode=decision.mode,
        )

    if (
        current_stage == "FAILED_RETRYABLE"
        and failed_stage == "RESEARCHED"
    ):
        decision = resume_decision
        return JobActionState(
            can_start=False,
            can_resume=bool(
                decision and decision.permits(ResumeAction.START_WORKER)
            ),
            can_stop=False,
            can_open_video=can_open_video,
            guidance=(
                research_failure_summary
                or "资料研究失败，可点击「重新搜索资料」换一批来源，或直接点击「继续/恢复」用内部知识模式重试。"
            ),
            can_retry_research=True,
            can_view_research_failure=True,
            resume_mode=decision.mode if decision else None,
        )

    can_resume = bool(
        resume_decision
        and resume_decision.permits(ResumeAction.START_WORKER)
    )
    if can_resume:
        reason = resume_decision.reason or (
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
            resume_mode=resume_decision.mode,
        )

    return JobActionState(
        can_start=False,
        can_resume=False,
        can_stop=False,
        can_open_video=can_open_video,
        guidance="该任务已初始化；如需重新制作，请点击“新建任务”。",
    )


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


def summarize_research_failure(
    evidence: list[Mapping[str, Any]],
) -> str:
    failed = [
        item for item in evidence
        if item.get("claim_supported") is not True and item.get("category")
    ]
    sentinel = next(
        (
            item for item in failed
            if "verified" in item and "total" in item
        ),
        None,
    )
    source_failures = [
        item for item in failed
        if item is not sentinel
    ]
    counts: dict[str, int] = {}
    for item in source_failures:
        category = str(item["category"])
        counts[category] = counts.get(category, 0) + 1
    total = (
        int(sentinel["total"])
        if sentinel is not None
        else len(source_failures)
    )
    parts: list[str] = []
    labels = (
        ("PERMANENT_SOURCE_FAILURE", "个网页不存在"),
        ("TEMPORARY_SOURCE_FAILURE", "个网页暂时无法访问"),
        ("UNSUPPORTED_CLAIM", "条内容无法证明相关说法"),
        ("INSUFFICIENT_FRESHNESS", "条资料时效不足"),
        ("INSUFFICIENT_EVIDENCE", "项资料数量不足"),
    )
    for category, label in labels:
        count = counts.get(category, 0)
        if count:
            parts.append(f"{count} {label}")
    if sentinel is not None:
        parts.append(
            f"资料验证通过 {int(sentinel['verified'])}/{total}，"
            "未达到质量门槛"
        )
    detail = "，".join(parts) if parts else "没有找到可验证资料"
    return f"资料研究失败：{total} 条资料中 {detail}。"
