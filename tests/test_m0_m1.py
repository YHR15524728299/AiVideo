from __future__ import annotations

import json
import multiprocessing
import os
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

import aicf.database as database_module
from aicf.cache import FileCache
from aicf.config import AppConfig, load_config
from aicf.database import JobRepository
from aicf.doctor import Doctor
from aicf.models.contracts import (
    DirectionProfile,
    PackageResult,
    PlatformCopy,
    ResearchResult,
    ReviewResult,
    ScriptResult,
    TopicCandidate,
)
from aicf.state_machine import (
    ORDERED_STAGES,
    PipelineStage,
    StateMachine,
    TransitionError,
)


def _hold_os_file_lock(
    lock_path: str,
    acquired: object,
    release: object,
) -> None:
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0)
        handle.write(b'{"pid": -1}')
        handle.flush()
        os.fsync(handle.fileno())
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        acquired.set()  # type: ignore[attr-defined]
        try:
            release.wait(timeout=10)  # type: ignore[attr-defined]
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _write_snapshot_in_child(
    database_path: str,
    job_id: str,
    started: object,
    finished: object,
) -> None:
    repository = JobRepository(database_path)
    started.set()  # type: ignore[attr-defined]
    repository._write_status(repository.get_job(job_id))
    finished.set()  # type: ignore[attr-defined]


def _advance_job_through(
    repository: JobRepository,
    job_id: str,
    target: PipelineStage,
) -> None:
    for stage in ORDERED_STAGES:
        repository.start_stage(job_id, stage)
        repository.complete_stage(job_id, stage)
        if stage == target:
            return


def test_config_requires_direction(tmp_path: Path) -> None:
    config_file = tmp_path / "content_direction.yaml"
    config_file.write_text("series_name: test\n", encoding="utf-8")

    with pytest.raises(ValidationError):
        load_config(config_file)


def test_config_applies_defaults_and_preserves_overrides(tmp_path: Path) -> None:
    config_file = tmp_path / "content_direction.yaml"
    config_file.write_text(
        "direction: 测试 AI 内容\nvideo:\n  target_duration_seconds: 55\n",
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert isinstance(config, AppConfig)
    assert config.video.target_duration_seconds == 55
    assert config.video.min_duration_seconds == 45
    assert config.visual_production.mode == "balanced"
    assert config.autopilot.max_repair_rounds == 2


def test_config_rejects_invalid_duration_bounds(tmp_path: Path) -> None:
    config_file = tmp_path / "content_direction.yaml"
    config_file.write_text(
        "direction: 测试\nvideo:\n  min_duration_seconds: 70\n"
        "  target_duration_seconds: 60\n  max_duration_seconds: 65\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_config(config_file)


def test_direction_profile_and_topic_contracts_validate() -> None:
    profile = DirectionProfile(
        series_name="系列",
        core_direction="AI 内容",
        audience="创作者",
        content_goal="提供实用判断",
        visual_style="知识短片",
        fact_risk_level="medium",
    )
    topic = TopicCandidate(
        topic_id="T001",
        title="AI 视频为什么不稳定",
        hook="问题不只在模型",
        core_question="失败来自哪里",
        core_claim="工作流比单次生成更重要",
        content_pillar="AI视频质量",
        audience_problem="结果不可控",
        direction_relevance=90,
        hook_strength=88,
        visual_potential=80,
        novelty=75,
        evidence_availability=70,
        production_difficulty=30,
        fact_risk=20,
        overall_score=84,
        selection_reason="高相关且风险可控",
    )

    assert profile.fact_risk_level == "medium"
    assert topic.overall_score == 84


def test_topic_score_must_be_between_zero_and_one_hundred() -> None:
    with pytest.raises(ValidationError):
        TopicCandidate(
            topic_id="T001",
            title="标题",
            hook="钩子",
            core_question="问题",
            core_claim="判断",
            content_pillar="支柱",
            audience_problem="痛点",
            direction_relevance=101,
            hook_strength=0,
            visual_potential=0,
            novelty=0,
            evidence_availability=0,
            production_difficulty=0,
            fact_risk=0,
            overall_score=0,
            selection_reason="原因",
        )


@pytest.mark.parametrize(
    "invalid_package",
    [
        {
            "douyin": {"title": "标题", "description": "简介", "hashtags": ["AI"]},
            "xiaohongshu": {"title": "标题", "description": "简介", "hashtags": ["AI"]},
            "youtube_shorts": {
                "title": "Title",
                "description": "Description",
                "hashtags": ["AI"],
            },
        },
        {
            "douyin": {"title": "标题", "description": "简介", "hashtags": []},
            "xiaohongshu": {"title": "标题", "description": "简介", "hashtags": ["AI"]},
            "youtube_shorts": {
                "title": "Title",
                "description": "Description",
                "hashtags": ["AI"],
            },
            "tiktok": {
                "title": "Title",
                "description": "Description",
                "hashtags": ["AI"],
            },
        },
    ],
)
def test_package_result_requires_all_platforms_with_non_empty_hashtags(
    invalid_package: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        PackageResult.model_validate(invalid_package)


@pytest.mark.parametrize(
    "copy",
    [
        {"title": "   ", "description": "简介", "hashtags": ["AI"]},
        {"title": "标题", "description": "\t", "hashtags": ["AI"]},
        {"title": "标题", "description": "简介", "hashtags": ["  "]},
    ],
)
def test_platform_copy_rejects_blank_title_description_or_hashtag(
    copy: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        PlatformCopy.model_validate(copy)


@pytest.mark.parametrize(
    ("passed", "issues"),
    [
        (True, ["仍有问题"]),
        (False, []),
    ],
)
def test_review_result_rejects_passed_issues_contradiction(
    passed: bool,
    issues: list[str],
) -> None:
    with pytest.raises(ValidationError):
        ReviewResult.model_validate(
            {
                "passed": passed,
                "scores": {
                    "direction_fit": 90,
                    "hook": 90,
                    "clarity": 90,
                    "evidence": 90,
                    "safety": 90,
                },
                "issues": issues,
                "revision_instructions": [],
            }
        )


@pytest.mark.parametrize(
    ("passed", "issues", "revision_instructions"),
    [
        (True, [], ["不应存在修订指令"]),
        (False, ["   "], ["修复问题"]),
        (False, ["问题"], ["\t"]),
    ],
)
def test_review_result_rejects_invalid_revision_state_or_blank_items(
    passed: bool,
    issues: list[str],
    revision_instructions: list[str],
) -> None:
    with pytest.raises(ValidationError):
        ReviewResult.model_validate(
            {
                "passed": passed,
                "scores": {
                    "direction_fit": 90,
                    "hook": 90,
                    "clarity": 90,
                    "evidence": 90,
                    "safety": 90,
                },
                "issues": issues,
                "revision_instructions": revision_instructions,
            }
        )


def test_review_result_copies_revision_instructions_to_missing_failed_issues() -> None:
    review = ReviewResult.model_validate(
        {
            "passed": False,
            "scores": {
                "direction_fit": 90,
                "hook": 90,
                "clarity": 90,
                "evidence": 40,
                "safety": 90,
            },
            "issues": [],
            "revision_instructions": ["补齐事实引用", "移除无来源数字"],
        }
    )

    assert review.issues == ["补齐事实引用", "移除无来源数字"]
    assert review.revision_instructions == ["补齐事实引用", "移除无来源数字"]


@pytest.mark.parametrize(
    "field",
    ["series_name", "core_direction", "audience", "content_goal", "visual_style"],
)
def test_direction_profile_rejects_blank_scalar_strings(field: str) -> None:
    payload: dict[str, object] = {
        "series_name": "系列",
        "core_direction": "方向",
        "audience": "受众",
        "content_goal": "目标",
        "visual_style": "风格",
    }
    payload[field] = " \t "

    with pytest.raises(ValidationError):
        DirectionProfile.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    [
        "audience_problems",
        "content_pillars",
        "tone",
        "allowed_topic_types",
        "forbidden_topic_types",
        "default_video_structure",
        "differentiation",
        "repetition_risks",
    ],
)
def test_direction_profile_rejects_blank_list_items(field: str) -> None:
    payload: dict[str, object] = {
        "series_name": "系列",
        "core_direction": "方向",
        "audience": "受众",
        "content_goal": "目标",
        field: ["  "],
    }

    with pytest.raises(ValidationError):
        DirectionProfile.model_validate(payload)


@pytest.mark.parametrize(
    "field",
    [
        "topic_id",
        "title",
        "hook",
        "core_question",
        "core_claim",
        "content_pillar",
        "audience_problem",
        "selection_reason",
    ],
)
def test_topic_candidate_rejects_blank_strings(field: str) -> None:
    payload: dict[str, object] = {
        "topic_id": "T001",
        "title": "标题",
        "hook": "钩子",
        "core_question": "问题",
        "core_claim": "判断",
        "content_pillar": "支柱",
        "audience_problem": "痛点",
        "direction_relevance": 90,
        "hook_strength": 90,
        "visual_potential": 90,
        "novelty": 90,
        "evidence_availability": 90,
        "production_difficulty": 10,
        "fact_risk": 10,
        "overall_score": 90,
        "selection_reason": "原因",
    }
    payload[field] = "\n"

    with pytest.raises(ValidationError):
        TopicCandidate.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "fact_field"),
    [
        ("summary", None),
        ("facts", "claim"),
        ("facts", "source_title"),
        ("facts", "source_url"),
        ("unknowns", None),
    ],
)
def test_research_result_rejects_blank_strings(
    field: str,
    fact_field: str | None,
) -> None:
    payload: dict[str, object] = {
        "summary": "摘要",
        "facts": [
            {
                "claim": "事实",
                "source_title": "来源",
                "source_url": "https://example.com",
                "confidence": 1,
            }
        ],
        "unknowns": ["未知项"],
    }
    if fact_field is not None:
        payload["facts"][0][fact_field] = " "  # type: ignore[index]
    elif field == "unknowns":
        payload[field] = [" "]
    else:
        payload[field] = " "

    with pytest.raises(ValidationError):
        ResearchResult.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "segment_field"),
    [
        ("title", None),
        ("hook", None),
        ("call_to_action", None),
        ("segments", "segment_id"),
        ("segments", "purpose"),
        ("segments", "narration"),
        ("segments", "visual_brief"),
    ],
)
def test_script_result_rejects_blank_strings(
    field: str,
    segment_field: str | None,
) -> None:
    payload: dict[str, object] = {
        "title": "标题",
        "hook": "钩子",
        "segments": [
            {
                "segment_id": "S001",
                "purpose": "解释",
                "narration": "旁白",
                "visual_brief": "画面",
                "fact_refs": [],
            }
        ],
        "call_to_action": "行动",
        "estimated_duration_seconds": 30,
    }
    if segment_field is not None:
        payload["segments"][0][segment_field] = " "  # type: ignore[index]
    else:
        payload[field] = " "

    with pytest.raises(ValidationError):
        ScriptResult.model_validate(payload)


def test_state_machine_accepts_next_stage_and_rejects_skips() -> None:
    machine = StateMachine()

    assert machine.next_stage(PipelineStage.DIRECTION_LOADED) == PipelineStage.DIRECTION_ANALYZED
    machine.validate_transition(
        PipelineStage.DIRECTION_LOADED,
        PipelineStage.DIRECTION_ANALYZED,
    )
    with pytest.raises(TransitionError):
        machine.validate_transition(
            PipelineStage.DIRECTION_LOADED,
            PipelineStage.SCRIPT_GENERATED,
        )


def test_repository_persists_stage_records_and_resume_command(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "content.db")
    output_dir = tmp_path / "中文输出" / "JOB001"
    repository.create_job("JOB001", output_dir)
    repository.start_stage("JOB001", PipelineStage.DIRECTION_LOADED)
    repository.complete_stage("JOB001", PipelineStage.DIRECTION_LOADED)

    status = repository.get_job("JOB001")

    assert status.current_stage == PipelineStage.DIRECTION_LOADED
    assert status.completed_stages == [PipelineStage.DIRECTION_LOADED]
    assert status.next_resume_command == "python -m aicf resume --job JOB001"
    saved = json.loads((output_dir / "status.json").read_text(encoding="utf-8"))
    assert saved["job_id"] == "JOB001"
    assert saved["stages"]["DIRECTION_LOADED"]["call_count"] == 1
    assert saved["version"] == status.version
    assert status.version == 3
    assert status.snapshot_dirty is False


def test_delete_job_removes_database_record_and_usage_events(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "content.db")
    job_dir = tmp_path / "jobs" / "JOB-DELETE"
    repository.create_job("JOB-DELETE", job_dir)
    repository.record_m4_submission(
        "JOB-DELETE",
        request_id="REQ-DELETE",
        jimeng_images=1,
    )

    repository.delete_job("JOB-DELETE")

    with pytest.raises(KeyError):
        repository.get_job("JOB-DELETE")
    assert repository.list_jobs() == []
    with repository._connect() as connection:
        remaining = connection.execute(
            "SELECT COUNT(*) AS count FROM m4_usage_events "
            "WHERE job_id = ?",
            ("JOB-DELETE",),
        ).fetchone()
    assert remaining["count"] == 0
    assert job_dir.is_dir()


def test_repository_keeps_sqlite_authoritative_when_snapshot_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = JobRepository(tmp_path / "content.db")

    def fail_snapshot(_status: object) -> bool:
        raise OSError("磁盘已满")

    monkeypatch.setattr(repository, "_write_status", fail_snapshot)
    created = repository.create_job("JOB-DIRTY", tmp_path / "JOB-DIRTY")

    assert created.version == 1
    assert created.snapshot_dirty is True
    assert repository.get_job("JOB-DIRTY").snapshot_dirty is True
    assert not (tmp_path / "JOB-DIRTY" / "status.json").exists()

    monkeypatch.undo()
    rebuilt = repository.rebuild_snapshot("JOB-DIRTY")

    assert rebuilt.snapshot_dirty is False
    saved = json.loads(
        (tmp_path / "JOB-DIRTY" / "status.json").read_text(encoding="utf-8")
    )
    assert saved["version"] == 1
    assert saved["snapshot_dirty"] is False


def test_status_snapshot_uses_unique_tmp_and_stale_version_cannot_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replaced_sources: list[str] = []
    original_replace = database_module.atomic_replace

    def record_replace(source: Path, target: Path) -> None:
        if target.name == "status.json":
            replaced_sources.append(source.name)
        original_replace(source, target)

    monkeypatch.setattr(database_module, "atomic_replace", record_replace)
    repository = JobRepository(tmp_path / "content.db")
    stale = repository.create_job("JOB-VERSION", tmp_path / "JOB-VERSION")
    current = repository.start_stage(
        "JOB-VERSION",
        PipelineStage.DIRECTION_LOADED,
    )

    assert current.version == 2
    assert len(replaced_sources) == 2
    assert len(set(replaced_sources)) == 2

    repository._write_status(stale)

    saved = json.loads(
        (tmp_path / "JOB-VERSION" / "status.json").read_text(encoding="utf-8")
    )
    assert saved["version"] == 2


def test_status_snapshot_lock_serializes_version_check_and_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = JobRepository(tmp_path / "content.db")
    stale = repository.create_job("JOB-LOCKED", tmp_path / "JOB-LOCKED")
    current = repository.start_stage("JOB-LOCKED", PipelineStage.DIRECTION_LOADED)
    snapshot = tmp_path / "JOB-LOCKED" / "status.json"
    snapshot.unlink()

    stale_at_replace = threading.Event()
    release_stale = threading.Event()
    current_replaced = threading.Event()
    original_replace = database_module.atomic_replace

    def pause_stale_replace(source: Path, target: Path) -> None:
        if target.name == "status.json" and ".v1." in source.name:
            stale_at_replace.set()
            assert release_stale.wait(timeout=2)
        if target.name == "status.json" and ".v2." in source.name:
            current_replaced.set()
        original_replace(source, target)

    monkeypatch.setattr(database_module, "atomic_replace", pause_stale_replace)
    stale_writer = threading.Thread(target=repository._write_status, args=(stale,))
    current_writer = threading.Thread(
        target=repository._write_status,
        args=(current,),
    )

    stale_writer.start()
    assert stale_at_replace.wait(timeout=2)
    current_writer.start()
    replaced_before_stale_finished = current_replaced.wait(timeout=0.2)
    release_stale.set()
    stale_writer.join(timeout=2)
    current_writer.join(timeout=2)

    assert not replaced_before_stale_finished
    assert not stale_writer.is_alive()
    assert not current_writer.is_alive()
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert saved["version"] == current.version


def test_status_snapshot_lock_blocks_a_separate_process(tmp_path: Path) -> None:
    database_path = tmp_path / "content.db"
    repository = JobRepository(database_path)
    repository.create_job("JOB-PROCESS-LOCK", tmp_path / "JOB-PROCESS-LOCK")
    snapshot = tmp_path / "JOB-PROCESS-LOCK" / "status.json"
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    finished = context.Event()
    writer = context.Process(
        target=_write_snapshot_in_child,
        args=(
            str(database_path),
            "JOB-PROCESS-LOCK",
            started,
            finished,
        ),
    )

    with repository._snapshot_lock(snapshot):
        writer.start()
        assert started.wait(timeout=5)
        assert not finished.wait(timeout=0.2)

    assert finished.wait(timeout=15)
    writer.join(timeout=5)
    assert writer.exitcode == 0


def test_status_snapshot_lock_times_out_on_os_lock_even_with_stale_pid_payload(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "JOB-OS-LOCK" / "status.json"
    lock_path = snapshot.with_name(".status.json.lock")
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_os_file_lock,
        args=(str(lock_path), acquired, release),
    )
    holder.start()
    assert acquired.wait(timeout=5)

    try:
        with pytest.raises(TimeoutError, match="status 快照文件锁超时"):
            with JobRepository._snapshot_lock(snapshot, timeout=0.2):
                pass
    finally:
        release.set()
        holder.join(timeout=5)

    assert holder.exitcode == 0


def test_status_snapshot_lock_never_unlinks_a_stale_pid_lock_file(
    tmp_path: Path,
) -> None:
    snapshot = tmp_path / "JOB-PERSISTENT-LOCK" / "status.json"
    lock_path = snapshot.with_name(".status.json.lock")
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text('{"pid": -1}', encoding="utf-8")

    with JobRepository._snapshot_lock(snapshot, timeout=0.2):
        assert lock_path.exists()

    assert lock_path.exists()


def test_rebuild_snapshot_forces_sqlite_version_over_snapshot_version(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "content.db")
    authoritative = repository.create_job("JOB-FORCE", tmp_path / "JOB-FORCE")
    snapshot = tmp_path / "JOB-FORCE" / "status.json"
    forged = authoritative.model_dump(mode="json")
    forged["version"] = 999
    snapshot.write_text(json.dumps(forged), encoding="utf-8")

    rebuilt = repository.rebuild_snapshot("JOB-FORCE")

    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert rebuilt.snapshot_dirty is False
    assert saved["version"] == authoritative.version


def test_repository_records_retryable_failure_without_losing_completed_stages(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "content.db")
    repository.create_job("JOB002", tmp_path / "JOB002")
    repository.start_stage("JOB002", PipelineStage.DIRECTION_LOADED)
    repository.complete_stage("JOB002", PipelineStage.DIRECTION_LOADED)
    repository.start_stage("JOB002", PipelineStage.DIRECTION_ANALYZED)
    repository.fail_stage(
        "JOB002",
        PipelineStage.DIRECTION_ANALYZED,
        "临时错误",
        retryable=True,
    )

    status = repository.get_job("JOB002")

    assert status.failed_stage == PipelineStage.DIRECTION_ANALYZED
    assert status.current_stage == PipelineStage.FAILED_RETRYABLE
    assert status.completed_stages == [PipelineStage.DIRECTION_LOADED]
    assert status.retry_count["DIRECTION_ANALYZED"] == 1


def test_repository_rejects_path_traversal_job_id_before_creating_output(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "content.db")
    escaped = tmp_path / "escaped"

    with pytest.raises(ValueError, match="Job ID"):
        repository.create_job("../escaped", escaped)

    assert not escaped.exists()


def test_repository_enforces_transition_in_same_transaction(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "content.db")
    repository.create_job("JOB-TRANSITION", tmp_path / "JOB-TRANSITION")
    repository.start_stage("JOB-TRANSITION", PipelineStage.DIRECTION_LOADED)
    repository.complete_stage("JOB-TRANSITION", PipelineStage.DIRECTION_LOADED)

    with pytest.raises(TransitionError):
        repository.start_stage("JOB-TRANSITION", PipelineStage.SCRIPT_GENERATED)

    status = repository.get_job("JOB-TRANSITION")
    assert status.current_stage == PipelineStage.DIRECTION_LOADED
    assert status.completed_stages == [PipelineStage.DIRECTION_LOADED]
    assert PipelineStage.SCRIPT_GENERATED.value not in status.stages


def test_repository_new_job_can_only_start_direction_loaded(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "content.db")
    repository.create_job("JOB-FIRST-STAGE", tmp_path / "JOB-FIRST-STAGE")

    with pytest.raises(TransitionError, match="首次"):
        repository.start_stage("JOB-FIRST-STAGE", PipelineStage.DIRECTION_ANALYZED)

    status = repository.get_job("JOB-FIRST-STAGE")
    assert status.current_stage is None
    assert status.stages == {}


def test_repository_completed_is_terminal(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "content.db")
    repository.create_job("JOB-DONE", tmp_path / "JOB-DONE")
    _advance_job_through(repository, "JOB-DONE", PipelineStage.COMPLETED)

    with pytest.raises(TransitionError, match="终态"):
        repository.start_stage("JOB-DONE", PipelineStage.COMPLETED)
    with pytest.raises(TransitionError, match="终态"):
        repository.fail_stage(
            "JOB-DONE",
            PipelineStage.COMPLETED,
            "不应覆盖完成态",
            retryable=True,
        )


def test_repository_retry_only_allows_recorded_retryable_failed_stage(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "content.db")
    repository.create_job("JOB-RETRY", tmp_path / "JOB-RETRY")
    repository.start_stage("JOB-RETRY", PipelineStage.DIRECTION_LOADED)
    repository.fail_stage(
        "JOB-RETRY",
        PipelineStage.DIRECTION_LOADED,
        "临时错误",
        retryable=True,
    )

    with pytest.raises(TransitionError, match="失败阶段"):
        repository.start_stage("JOB-RETRY", PipelineStage.DIRECTION_ANALYZED)

    status = repository.start_stage("JOB-RETRY", PipelineStage.DIRECTION_LOADED)
    assert status.current_stage == PipelineStage.DIRECTION_LOADED
    assert status.failed_stage is None


def test_repository_does_not_retry_non_retryable_failure(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "content.db")
    repository.create_job("JOB-NO-RETRY", tmp_path / "JOB-NO-RETRY")
    repository.start_stage("JOB-NO-RETRY", PipelineStage.DIRECTION_LOADED)
    repository.fail_stage(
        "JOB-NO-RETRY",
        PipelineStage.DIRECTION_LOADED,
        "人工处理",
        retryable=False,
    )

    with pytest.raises(TransitionError, match="不可重试"):
        repository.start_stage("JOB-NO-RETRY", PipelineStage.DIRECTION_LOADED)


def test_failed_job_exposes_the_recorded_recovery_command(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "content.db")
    repository.create_job("JOB-RECOVERY", tmp_path / "JOB-RECOVERY")
    _advance_job_through(repository, "JOB-RECOVERY", PipelineStage.RENDERED)
    repository.start_stage("JOB-RECOVERY", PipelineStage.QA_CHECKED)
    repository.fail_stage(
        "JOB-RECOVERY",
        PipelineStage.QA_CHECKED,
        "缺少外部能力",
        retryable=False,
        recovery_command="powershell -File scripts/doctor.ps1",
    )

    status = repository.get_job("JOB-RECOVERY")

    assert status.next_resume_command == "powershell -File scripts/doctor.ps1"


def test_file_cache_is_deterministic_and_invalidates_on_input_change(tmp_path: Path) -> None:
    cache = FileCache(tmp_path / "cache")
    key_a = cache.make_key("planner", {"direction": "A"}, "model-x", "prompt-v1")
    key_b = cache.make_key("planner", {"direction": "B"}, "model-x", "prompt-v1")
    cache.set(key_a, {"result": "ok"})

    assert cache.get(key_a) == {"result": "ok"}
    assert cache.get(key_b) is None
    assert key_a != key_b


def test_doctor_reports_required_tools_and_redacts_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret-value")
    monkeypatch.setattr("shutil.which", lambda name: f"C:/tools/{name}.exe")
    doctor = Doctor(jimeng_executable="jimeng")

    report = doctor.run()
    rendered = report.to_text()

    assert report.checks["python"].available
    assert report.checks["ffmpeg"].available
    assert report.checks["ffprobe"].available
    assert report.checks["jimeng"].available
    assert report.checks["openrouter"].available
    assert "secret-value" not in rendered
    assert "已配置" in rendered


def test_doctor_reports_tts_provider_selection_and_fallback_reason() -> None:
    report = Doctor(
        edge_tts_available=False,
        sapi_available=True,
        audio_ffmpeg="C:/tools/ffmpeg.exe",
    ).run()

    assert report.checks["tts_edge"].available is False
    assert report.checks["tts_sapi"].available is True
    assert report.checks["tts_strategy"].available is True
    assert "windows_sapi" in report.checks["tts_strategy"].detail
    assert "edge-tts 未安装" in report.checks["tts_strategy"].detail


def test_doctor_reports_sapi_strategy_when_audio_ffmpeg_is_unavailable() -> None:
    report = Doctor(
        edge_tts_available=True,
        sapi_available=True,
        audio_ffmpeg="",
    ).run()

    assert report.checks["tts_audio_ffmpeg"].available is False
    assert "windows_sapi" in report.checks["tts_strategy"].detail
    assert "音频转码 FFmpeg 不可用" in report.checks["tts_strategy"].detail
