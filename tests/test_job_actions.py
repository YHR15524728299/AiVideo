from pathlib import Path

from aicf import job_actions
from aicf.job_actions import (
    derive_job_actions,
    failed_attention_can_auto_reopen,
    first_available_job_id,
    job_storage_exists,
    summarize_research_failure,
)


def test_completed_current_stage_is_not_a_zombie_candidate() -> None:
    assert job_actions.should_recover_zombie_job(
        current_stage="RESEARCHED",
        failed_stage="",
        completed_stages=["DIRECTION_LOADED", "RESEARCHED"],
    ) is False


def test_incomplete_nonterminal_stage_is_a_zombie_candidate() -> None:
    assert job_actions.should_recover_zombie_job(
        current_stage="RESEARCHED",
        failed_stage="",
        completed_stages=["DIRECTION_LOADED"],
    ) is True


def test_new_job_exposes_only_start_action() -> None:
    actions = derive_job_actions(existing_job=False)

    assert actions.can_start is True
    assert actions.can_resume is False
    assert actions.can_stop is False
    assert actions.can_open_video is False
    assert "开始生成" in actions.guidance


def test_completed_job_exposes_video_but_not_start_or_resume() -> None:
    actions = derive_job_actions(
        existing_job=True,
        current_stage="COMPLETED",
        has_final_video=True,
    )

    assert actions.can_start is False
    assert actions.can_resume is False
    assert actions.can_open_video is True
    assert "打开最终视频" in actions.guidance


def test_completed_job_without_video_explains_next_step() -> None:
    actions = derive_job_actions(
        existing_job=True,
        current_stage="COMPLETED",
    )

    assert actions.can_open_video is False
    assert "打开输出目录" in actions.guidance


def test_recoverable_failure_exposes_resume_action() -> None:
    actions = derive_job_actions(
        existing_job=True,
        current_stage="FAILED_RETRYABLE",
        failed_stage="KEYFRAMES_GENERATED",
        recoverable=True,
    )

    assert actions.can_resume is True
    assert actions.can_start is False
    assert "继续/恢复" in actions.guidance


def test_research_failure_exposes_dedicated_retry_and_plain_summary() -> None:
    summary = summarize_research_failure([
        {"category": "PERMANENT_SOURCE_FAILURE"} for _ in range(7)
    ] + [{"category": "UNSUPPORTED_CLAIM"}])
    actions = derive_job_actions(
        existing_job=True,
        current_stage="FAILED_RETRYABLE",
        failed_stage="RESEARCHED",
        recoverable=True,
        research_failure_summary=summary,
    )

    assert actions.can_retry_research is True
    assert actions.can_view_research_failure is True
    assert actions.can_resume is False
    assert actions.guidance == (
        "资料研究失败：8 条资料中 7 个网页不存在，"
        "1 条内容无法证明相关说法。"
    )


def test_research_failure_summary_ignores_success_evidence_and_sentinel() -> None:
    summary = summarize_research_failure([
        {"claim_supported": True},
        {"claim_supported": True},
        {
            "claim_supported": False,
            "category": "UNSUPPORTED_CLAIM",
        },
        {
            "claim_supported": False,
            "category": "INSUFFICIENT_EVIDENCE",
            "verified": 2,
            "total": 3,
        },
    ])

    assert summary == (
        "资料研究失败：3 条资料中 1 条内容无法证明相关说法，"
        "资料验证通过 2/3，未达到质量门槛。"
    )


def test_interrupted_job_exposes_resume_action() -> None:
    actions = derive_job_actions(
        existing_job=True,
        current_stage="RENDERED",
        job_is_running=False,
    )

    assert actions.can_resume is True
    assert "异常中断" in actions.guidance


def test_attention_failure_requires_manual_handling() -> None:
    actions = derive_job_actions(
        existing_job=True,
        current_stage="FAILED_NEEDS_ATTENTION",
        failed_stage="QA_CHECKED",
    )

    assert actions.can_resume is False
    assert "人工处理" in actions.guidance


def test_attention_network_failure_exposes_resume_action() -> None:
    actions = derive_job_actions(
        existing_job=True,
        current_stage="FAILED_NEEDS_ATTENTION",
        failed_stage="RESEARCHED",
        recoverable=True,
    )

    assert actions.can_resume is True
    assert "继续/恢复" in actions.guidance


def test_failed_attention_auto_reopen_uses_error_contract() -> None:
    stages = {
        "RESEARCHED": {
            "error": "HTTP 503: upstream unavailable",
            "recoverable": False,
        }
    }

    assert failed_attention_can_auto_reopen("RESEARCHED", stages) is True
    assert failed_attention_can_auto_reopen(
        "RESEARCHED",
        {"RESEARCHED": {"error": "产物哈希不一致"}},
    ) is False


def test_legacy_packaging_failure_from_review_can_reopen() -> None:
    stages = {
        "CONTENT_PACKAGED": {
            "error": "M2 内容审核未通过",
            "recoverable": False,
        }
    }

    assert failed_attention_can_auto_reopen("CONTENT_PACKAGED", stages) is True


def test_initialized_job_requires_new_task_instead_of_resume() -> None:
    actions = derive_job_actions(
        existing_job=True,
        current_stage="INIT",
    )

    assert actions.can_start is False
    assert actions.can_resume is False
    assert "新建任务" in actions.guidance


def test_running_job_exposes_only_stop_action() -> None:
    actions = derive_job_actions(
        existing_job=True,
        current_stage="RESEARCHED",
        job_is_running=True,
        app_has_running_job=True,
    )

    assert actions.can_start is False
    assert actions.can_resume is False
    assert actions.can_stop is True
    assert "运行中" in actions.guidance


def test_other_running_job_blocks_new_start() -> None:
    actions = derive_job_actions(
        existing_job=False,
        app_has_running_job=True,
    )

    assert actions.can_start is False
    assert actions.can_stop is True
    assert "已有任务正在后台运行" in actions.guidance


def test_other_running_job_blocks_selected_job_resume() -> None:
    actions = derive_job_actions(
        existing_job=True,
        current_stage="FAILED_RETRYABLE",
        failed_stage="KEYFRAMES_GENERATED",
        recoverable=True,
        app_has_running_job=True,
    )

    assert actions.can_resume is False
    assert actions.can_stop is True


def test_job_storage_exists_detects_orphaned_job_or_delivery(
    tmp_path: Path,
) -> None:
    job_dir = tmp_path / "data" / "jobs" / "JOB1"
    output_dir = tmp_path / "outputs" / "JOB1"

    assert job_storage_exists(job_dir, output_dir) is False
    job_dir.mkdir(parents=True)
    assert job_storage_exists(job_dir, output_dir) is True
    job_dir.rmdir()
    output_dir.mkdir(parents=True)
    assert job_storage_exists(job_dir, output_dir) is True


def test_first_available_job_id_adds_suffix_on_collision() -> None:
    taken = {"VIDEO0804213000", "VIDEO0804213000-2"}

    assert first_available_job_id(
        "VIDEO0804213000",
        taken.__contains__,
    ) == "VIDEO0804213000-3"
