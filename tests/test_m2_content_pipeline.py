from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import pytest

from aicf.content_orchestrator import ContentOrchestrator
from aicf.engines.script_engine import ScriptRevisionEngine
from aicf.m2_promotion import M2PromotionManager
from aicf.models.contracts import DirectionProfile, ResearchResult, ScriptResult
from aicf.providers.openrouter import StructuredResult, TokenUsage


def _hold_promotion_os_lock(
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


class FakeStructuredClient:
    def __init__(self, responses: dict[str, dict[str, object]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []
        self.usage = TokenUsage()

    def call_structured(self, **kwargs: object) -> StructuredResult:
        self.calls.append(kwargs)
        stage = str(kwargs["stage"])
        self.usage = self.usage + TokenUsage(10, 5, 15)
        return StructuredResult(
            data=self.responses[stage],
            usage=TokenUsage(10, 5, 15),
            cached=False,
            request_id=f"fake-{stage}",
            model="fake/model",
        )


def _responses() -> dict[str, dict[str, object]]:
    return {
        "direction": {
            "series_name": "AI生成内容真相",
            "core_direction": "拆解 AI 内容生产",
            "audience": "创作者",
            "audience_problems": ["结果不稳定"],
            "content_goal": "给出可执行判断",
            "content_pillars": ["AI视频质量"],
            "tone": ["清晰", "直接"],
            "visual_style": "电影化知识短片",
            "allowed_topic_types": ["方法拆解"],
            "forbidden_topic_types": ["虚假数据"],
            "default_video_structure": ["钩子", "解释", "结论"],
            "differentiation": ["强调流程"],
            "repetition_risks": ["重复工具盘点"],
            "fact_risk_level": "medium",
        },
        "research": {
            "summary": "稳定性来自可验证的工作流。",
            "facts": [
                {
                    "claim": "分阶段校验可降低返工",
                    "source_title": "Internal workflow study",
                    "source_url": "https://example.com/workflow",
                    "confidence": 0.9,
                }
            ],
            "unknowns": ["不同模型的具体失败率"],
        },
        "script": {
            "title": "AI视频不稳定，先别怪模型",
            "hook": "问题可能根本不在模型。",
            "segments": [
                {
                    "segment_id": "SEG001",
                    "purpose": "hook",
                    "narration": "AI视频不稳定，问题可能根本不在模型。",
                    "visual_brief": "失败画面快速切换",
                    "fact_refs": [],
                },
                {
                    "segment_id": "SEG002",
                    "purpose": "explain",
                    "narration": "真正决定稳定性的，是每一步有没有校验。",
                    "visual_brief": "工作流节点逐个点亮",
                    "fact_refs": [0],
                },
            ],
            "call_to_action": "先把流程做稳，再追模型。",
            "estimated_duration_seconds": 55,
        },
        "review": {
            "passed": True,
            "scores": {
                "direction_fit": 92,
                "hook": 88,
                "clarity": 90,
                "evidence": 85,
                "safety": 95,
            },
            "issues": [],
            "revision_instructions": [],
        },
        "package": {
            "douyin": {
                "title": "AI视频不稳定，先别怪模型",
                "description": "从工作流看 AI 视频稳定性。",
                "hashtags": ["AI视频", "内容生产"],
            },
            "xiaohongshu": {
                "title": "AI视频稳定性的真相",
                "description": "两步看懂为什么总返工。",
                "hashtags": ["AI创作", "工作流"],
            },
            "youtube_shorts": {
                "title": "Why AI Video Workflows Fail",
                "description": "A practical workflow breakdown.",
                "hashtags": ["AIVideo", "Workflow"],
            },
            "tiktok": {
                "title": "Stop Blaming the AI Model",
                "description": "Your workflow may be the real problem.",
                "hashtags": ["AITools", "CreatorTips"],
            },
        },
    }


def test_script_revision_engine_sends_real_duration_ratio_and_action() -> None:
    responses = _responses()
    client = FakeStructuredClient({"script_revision": responses["script"]})
    engine = ScriptRevisionEngine(client)

    revised = engine.revise_for_duration(
        DirectionProfile.model_validate(responses["direction"]),
        ResearchResult.model_validate(responses["research"]),
        ScriptResult.model_validate(responses["script"]),
        actual_duration_seconds=30.0,
        min_duration_seconds=45.0,
        max_duration_seconds=75.0,
        target_duration_seconds=60.0,
        suggested_action="expand",
    )

    assert revised.title == responses["script"]["title"]
    payload = client.calls[0]["user_payload"]
    assert payload["duration_revision"] == {
        "actual_duration_seconds": 30.0,
        "min_duration_seconds": 45.0,
        "max_duration_seconds": 75.0,
        "target_duration_seconds": 60.0,
        "suggested_action": "expand",
        "target_ratio": 2.0,
    }


def test_direction_engine_validates_typed_result() -> None:
    from aicf.engines.direction_engine import DirectionEngine

    client = FakeStructuredClient(_responses())
    profile = DirectionEngine(client).analyze(
        {
            "direction": "拆解 AI 内容生产",
            "series_name": "AI生成内容真相",
            "audience": "创作者",
        }
    )

    assert isinstance(profile, DirectionProfile)
    assert profile.fact_risk_level == "medium"
    assert client.calls[0]["stage"] == "direction"


def test_content_orchestrator_runs_direction_to_publish_and_writes_contract_files(
    tmp_path: Path,
) -> None:
    client = FakeStructuredClient(_responses())
    output_dir = tmp_path / "outputs" / "M2E2E001"
    orchestrator = ContentOrchestrator(client=client, output_dir=output_dir)

    manifest = orchestrator.run(
        direction={
            "direction": "拆解 AI 内容生产",
            "series_name": "AI生成内容真相",
            "audience": "创作者",
            "content_goal": "给出可执行判断",
            "content_pillars": ["AI视频质量"],
            "tone": ["清晰", "直接"],
            "platforms": ["douyin", "xiaohongshu", "youtube_shorts", "tiktok"],
            "visual_style": "电影化知识短片",
            "avoid": ["虚假数据"],
        },
        selected_topic={
            "topic_id": "T001",
            "title": "AI视频为什么不稳定",
            "hook": "先别怪模型",
            "core_question": "失败来自哪里",
            "core_claim": "工作流决定稳定性",
        },
    )

    expected_files = {
        "direction.json",
        "topic.json",
        "research.json",
        "script.json",
        "script.md",
        "review.json",
        "package.json",
        "publish.json",
        "usage.json",
        "manifest.json",
        "m2_runs",
    }
    assert {path.name for path in output_dir.iterdir()} == expected_files
    assert [call["stage"] for call in client.calls] == [
        "direction",
        "research",
        "script",
        "review",
        "package",
    ]
    assert manifest["status"] == "ready_to_publish"
    assert manifest["usage"]["total_tokens"] == 75
    publish = json.loads((output_dir / "publish.json").read_text(encoding="utf-8"))
    assert publish["status"] == "ready_to_publish"
    assert set(publish["platforms"]) == {
        "douyin",
        "xiaohongshu",
        "youtube_shorts",
        "tiktok",
    }
    script_md = (output_dir / "script.md").read_text(encoding="utf-8")
    assert "# AI视频不稳定，先别怪模型" in script_md
    assert "SEG001" in script_md


def test_review_failure_stops_before_package(tmp_path: Path) -> None:
    responses = _responses()
    responses["review"] = {
        "passed": False,
        "scores": {
            "direction_fit": 90,
            "hook": 70,
            "clarity": 90,
            "evidence": 40,
            "safety": 90,
        },
        "issues": ["证据不足"],
        "revision_instructions": ["删除未经支持的数字"],
    }
    client = FakeStructuredClient(responses)
    output_dir = tmp_path / "failed"

    manifest = ContentOrchestrator(client, output_dir).run(
        direction={
            "direction": "AI 内容",
            "platforms": ["douyin", "xiaohongshu", "youtube_shorts", "tiktok"],
        },
        selected_topic={
            "topic_id": "T001",
            "title": "标题",
            "hook": "钩子",
            "core_question": "问题",
            "core_claim": "判断",
        },
    )

    assert manifest["status"] == "needs_revision"
    assert not (output_dir / "package.json").exists()
    assert not (output_dir / "publish.json").exists()
    assert [call["stage"] for call in client.calls][-1] == "review"
    run_statuses = list((output_dir / "m2_runs").glob("*/run.json"))
    assert len(run_statuses) == 1
    run_status = json.loads(run_statuses[0].read_text(encoding="utf-8"))
    assert run_status["status"] == "needs_revision"
    assert run_status["publishable"] is False


def test_failed_rerun_keeps_previous_publish_and_isolates_review_artifacts(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "rerun"
    output_dir.mkdir()
    (output_dir / "publish.json").write_text('{"status":"old"}', encoding="utf-8")
    (output_dir / "package.json").write_text('{"status":"old"}', encoding="utf-8")
    responses = _responses()
    responses["review"] = {
        "passed": False,
        "scores": {
            "direction_fit": 90,
            "hook": 70,
            "clarity": 90,
            "evidence": 40,
            "safety": 90,
        },
        "issues": ["证据不足"],
        "revision_instructions": ["补充证据"],
    }

    manifest = ContentOrchestrator(FakeStructuredClient(responses), output_dir).run(
        direction={
            "direction": "AI 内容",
            "platforms": ["douyin", "xiaohongshu", "youtube_shorts", "tiktok"],
        },
        selected_topic={
            "topic_id": "T001",
            "title": "标题",
            "hook": "钩子",
            "core_question": "问题",
            "core_claim": "判断",
        },
    )

    assert manifest["status"] == "needs_revision"
    assert (output_dir / "publish.json").read_text(encoding="utf-8") == (
        '{"status":"old"}'
    )
    assert (output_dir / "package.json").read_text(encoding="utf-8") == (
        '{"status":"old"}'
    )
    run_directories = list((output_dir / "m2_runs").iterdir())
    assert len(run_directories) == 1
    assert (run_directories[0] / "review.json").exists()
    assert not (run_directories[0] / "publish.json").exists()
    run_status = json.loads(
        (run_directories[0] / "run.json").read_text(encoding="utf-8")
    )
    assert run_status["publishable"] is False
    assert not list(tmp_path.glob(".rerun.staging-*"))


def test_successful_rerun_replaces_only_m2_managed_files(tmp_path: Path) -> None:
    output_dir = tmp_path / "rerun-success"
    output_dir.mkdir()
    (output_dir / "package.json").write_text('{"status":"old"}', encoding="utf-8")
    (output_dir / "publish.json").write_text('{"status":"old"}', encoding="utf-8")
    (output_dir / "status.json").write_text('{"stage":"RENDERED"}', encoding="utf-8")
    for directory in ("logs", "audio", "final", "delivery"):
        preserved = output_dir / directory / "preserved.txt"
        preserved.parent.mkdir()
        preserved.write_text(directory, encoding="utf-8")

    ContentOrchestrator(FakeStructuredClient(_responses()), output_dir).run(
        direction={
            "direction": "AI 内容",
            "platforms": ["douyin", "xiaohongshu", "youtube_shorts", "tiktok"],
        },
        selected_topic={
            "topic_id": "T001",
            "title": "标题",
            "hook": "钩子",
            "core_question": "问题",
            "core_claim": "判断",
        },
    )

    package = json.loads((output_dir / "package.json").read_text(encoding="utf-8"))
    assert set(package) == {"douyin", "xiaohongshu", "youtube_shorts", "tiktok"}
    assert (output_dir / "status.json").read_text(encoding="utf-8") == (
        '{"stage":"RENDERED"}'
    )
    for directory in ("logs", "audio", "final", "delivery"):
        assert (output_dir / directory / "preserved.txt").read_text(
            encoding="utf-8"
        ) == directory


def test_exceptional_rerun_keeps_old_publish_and_records_failed_run(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "exception"
    output_dir.mkdir()
    (output_dir / "publish.json").write_text('{"status":"old"}', encoding="utf-8")
    (output_dir / "package.json").write_text('{"status":"old"}', encoding="utf-8")
    (output_dir / "status.json").write_text('{"stage":"RENDERED"}', encoding="utf-8")
    client = FakeStructuredClient(_responses())

    def fail_call(**_: object) -> StructuredResult:
        raise RuntimeError("模型失败")

    client.call_structured = fail_call  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="模型失败"):
        ContentOrchestrator(client, output_dir).run(
            direction={
                "direction": "AI 内容",
                "platforms": [
                    "douyin",
                    "xiaohongshu",
                    "youtube_shorts",
                    "tiktok",
                ],
            },
            selected_topic={
                "topic_id": "T001",
                "title": "标题",
                "hook": "钩子",
                "core_question": "问题",
                "core_claim": "判断",
            },
        )

    assert (output_dir / "publish.json").read_text(encoding="utf-8") == (
        '{"status":"old"}'
    )
    assert (output_dir / "package.json").read_text(encoding="utf-8") == (
        '{"status":"old"}'
    )
    assert (output_dir / "status.json").exists()
    run_statuses = list((output_dir / "m2_runs").glob("*/run.json"))
    assert len(run_statuses) == 1
    run_status = json.loads(run_statuses[0].read_text(encoding="utf-8"))
    assert run_status["status"] == "generation_failed"
    assert run_status["publishable"] is False
    assert not list(tmp_path.glob(".exception.staging-*"))


def test_content_orchestrator_rejects_unsupported_platform_before_generation(
    tmp_path: Path,
) -> None:
    client = FakeStructuredClient(_responses())

    with pytest.raises(ValueError, match="不支持的平台"):
        ContentOrchestrator(client, tmp_path / "unsupported").run(
            direction={"direction": "AI 内容", "platforms": ["bilibili"]},
            selected_topic={
                "topic_id": "T001",
                "title": "标题",
                "hook": "钩子",
                "core_question": "问题",
                "core_claim": "判断",
            },
        )

    assert client.calls == []


def test_content_orchestrator_rejects_empty_platforms_before_generation(
    tmp_path: Path,
) -> None:
    client = FakeStructuredClient(_responses())

    with pytest.raises(ValueError, match="必须精确包含"):
        ContentOrchestrator(client, tmp_path / "empty-platforms").run(
            direction={"direction": "AI 内容", "platforms": []},
            selected_topic={
                "topic_id": "T001",
                "title": "标题",
                "hook": "钩子",
                "core_question": "问题",
                "core_claim": "判断",
            },
        )

    assert client.calls == []


def test_content_orchestrator_requires_the_exact_supported_platform_set(
    tmp_path: Path,
) -> None:
    client = FakeStructuredClient(_responses())

    with pytest.raises(ValueError, match="必须精确包含"):
        ContentOrchestrator(client, tmp_path / "missing-platform").run(
            direction={
                "direction": "AI 内容",
                "platforms": ["douyin", "xiaohongshu", "youtube_shorts"],
            },
            selected_topic={
                "topic_id": "T001",
                "title": "标题",
                "hook": "钩子",
                "core_question": "问题",
                "core_claim": "判断",
            },
        )

    assert client.calls == []


def test_content_orchestrator_accepts_supported_platforms_in_any_order(
    tmp_path: Path,
) -> None:
    client = FakeStructuredClient(_responses())

    manifest = ContentOrchestrator(client, tmp_path / "reordered-platforms").run(
        direction={
            "direction": "AI 内容",
            "platforms": [
                "tiktok",
                "douyin",
                "youtube_shorts",
                "xiaohongshu",
            ],
        },
        selected_topic={
            "topic_id": "T001",
            "title": "标题",
            "hook": "钩子",
            "core_question": "问题",
            "core_claim": "判断",
        },
    )

    assert manifest["status"] == "ready_to_publish"


def test_promotion_lock_times_out_on_os_lock_even_with_stale_pid_payload(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "promotion-os-lock"
    lock_path = tmp_path / ".promotion-os-lock.promotion.lock"
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    release = context.Event()
    holder = context.Process(
        target=_hold_promotion_os_lock,
        args=(str(lock_path), acquired, release),
    )
    holder.start()
    assert acquired.wait(timeout=5)

    try:
        manager = M2PromotionManager(
            output_dir,
            {"publish.json"},
            lock_timeout=0.2,
        )
        with pytest.raises(TimeoutError, match="M2 promotion 文件锁超时"):
            with manager._lock():
                pass
    finally:
        release.set()
        holder.join(timeout=5)

    assert holder.exitcode == 0


def test_promotion_lock_never_unlinks_a_stale_pid_lock_file(tmp_path: Path) -> None:
    output_dir = tmp_path / "persistent-promotion-lock"
    manager = M2PromotionManager(output_dir, {"publish.json"})
    manager.lock_path.write_text('{"pid": -1}', encoding="utf-8")

    with manager._lock():
        assert manager.lock_path.exists()

    assert manager.lock_path.exists()


def test_startup_recovers_interrupted_promotion_from_journal(tmp_path: Path) -> None:
    class SimulatedCrash(BaseException):
        pass

    output_dir = tmp_path / "recover"
    output_dir.mkdir()
    old_publish = '{"status":"old"}'
    old_package = '{"status":"old"}'
    (output_dir / "publish.json").write_text(old_publish, encoding="utf-8")
    (output_dir / "package.json").write_text(old_package, encoding="utf-8")

    def crash(point: str) -> None:
        if point == "after_current_backed_up":
            raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        ContentOrchestrator(
            FakeStructuredClient(_responses()),
            output_dir,
            fault_injector=crash,
        ).run(
            direction={
                "direction": "AI 内容",
                "platforms": [
                    "douyin",
                    "xiaohongshu",
                    "youtube_shorts",
                    "tiktok",
                ],
            },
            selected_topic={
                "topic_id": "T001",
                "title": "标题",
                "hook": "钩子",
                "core_question": "问题",
                "core_claim": "判断",
            },
        )

    journal = tmp_path / ".recover.promotion.journal.json"
    assert journal.exists()
    assert not (output_dir / "publish.json").exists()

    ContentOrchestrator(FakeStructuredClient(_responses()), output_dir)

    assert (output_dir / "publish.json").read_text(encoding="utf-8") == old_publish
    assert (output_dir / "package.json").read_text(encoding="utf-8") == old_package
    assert not journal.exists()
    assert not list(tmp_path.glob(".recover.backup-*"))


def test_startup_recovers_crash_between_target_replace_and_journal_update(
    tmp_path: Path,
) -> None:
    class SimulatedCrash(BaseException):
        pass

    output_dir = tmp_path / "recover-mid-file"
    output_dir.mkdir()
    (output_dir / "publish.json").write_text('{"status":"old"}', encoding="utf-8")
    (output_dir / "package.json").write_text('{"status":"old"}', encoding="utf-8")

    def crash(point: str) -> None:
        if point == "after_target_replaced_before_journal":
            raise SimulatedCrash()

    with pytest.raises(SimulatedCrash):
        ContentOrchestrator(
            FakeStructuredClient(_responses()),
            output_dir,
            fault_injector=crash,
        ).run(
            direction={
                "direction": "AI 内容",
                "platforms": [
                    "douyin",
                    "xiaohongshu",
                    "youtube_shorts",
                    "tiktok",
                ],
            },
            selected_topic={
                "topic_id": "T001",
                "title": "标题",
                "hook": "钩子",
                "core_question": "问题",
                "core_claim": "判断",
            },
        )

    ContentOrchestrator(FakeStructuredClient(_responses()), output_dir)

    remaining_managed = {
        path.name
        for path in output_dir.iterdir()
        if path.name in {
            "direction.json",
            "topic.json",
            "research.json",
            "script.json",
            "script.md",
            "review.json",
            "package.json",
            "publish.json",
            "usage.json",
            "manifest.json",
        }
    }
    assert remaining_managed == {"package.json", "publish.json"}


def test_committed_promotion_survives_crash_between_journal_and_backup_cleanup(
    tmp_path: Path,
) -> None:
    class SimulatedCrash(BaseException):
        pass

    output_dir = tmp_path / "cleanup-crash"
    staging = tmp_path / "staging"
    output_dir.mkdir()
    staging.mkdir()
    (output_dir / "publish.json").write_text("old", encoding="utf-8")
    (staging / "publish.json").write_text("new", encoding="utf-8")

    def crash(point: str) -> None:
        if point == "between_journal_and_backup_cleanup":
            raise SimulatedCrash()

    manager = M2PromotionManager(
        output_dir,
        {"publish.json"},
        fault_injector=crash,
    )

    with pytest.raises(SimulatedCrash):
        manager.promote(staging)

    M2PromotionManager(output_dir, {"publish.json"}).recover()

    assert (output_dir / "publish.json").read_text(encoding="utf-8") == "new"
    assert not manager.journal_path.exists()
