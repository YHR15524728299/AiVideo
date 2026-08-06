from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from aicf.config import AppConfig
from aicf.database import JobRepository
from aicf.m2_runner import M2ContentRunner
from aicf.providers.openrouter import StructuredResult, TokenUsage
from aicf.research_policy import classify_source_error
from aicf.source_verifier import SourceVerificationError
from aicf.state_machine import PipelineStage


class SequencedClient:
    def __init__(self, responses: dict[str, list[dict[str, object]]]) -> None:
        self.responses = responses
        self.calls: list[str] = []
        self.call_details: list[dict[str, object]] = []
        self.usage = TokenUsage()

    def call_structured(self, **kwargs: object) -> StructuredResult:
        stage = str(kwargs["stage"])
        self.calls.append(stage)
        self.call_details.append(kwargs)
        self.usage = self.usage + TokenUsage(10, 5, 15)
        return StructuredResult(
            data=self.responses[stage].pop(0),
            usage=TokenUsage(10, 5, 15),
            cached=False,
            model="test/model:free",
        )


class StubSourceVerifier:
    def __init__(self, failures: list[list[str]] | None = None) -> None:
        self.failures = list(failures or [])
        self.calls: list[object] = []

    def verify_research(self, research: object) -> list[dict[str, object]]:
        self.calls.append(research)
        if self.failures:
            errors = self.failures.pop(0)
            raise SourceVerificationError(
                errors,
                evidence=[{
                    "fact_index": 0,
                    "original_url": research.facts[0].source_url,
                    "final_url": research.facts[0].source_url,
                    "title": research.facts[0].source_title,
                    "body_summary": research.facts[0].claim,
                    "fetched_at": "2026-07-20T12:00:00+00:00",
                    "sha256": "f" * 64,
                    "claim_supported": False,
                    "error": errors[0],
                    "category": classify_source_error(errors[0]).value,
                }],
            )
        fact = research.facts[0]
        return [{
            "original_url": fact.source_url,
            "final_url": fact.source_url,
            "title": fact.source_title,
            "body_summary": fact.claim,
            "fetched_at": "2026-07-20T12:00:00+00:00",
            "sha256": "a" * 64,
            "claim_supported": True,
        }]


def _runner(
    client: object,
    repo: JobRepository,
    outputs_root: Path,
    verifier: StubSourceVerifier | None = None,
) -> M2ContentRunner:
    return M2ContentRunner(
        client,
        repo,
        outputs_root,
        source_verifier=verifier or StubSourceVerifier(),
    )


def _topic(index: int, score: float = 80) -> dict[str, object]:
    return {
        "topic_id": f"T{index:03d}",
        "title": f"选题 {index}",
        "hook": f"钩子 {index}",
        "core_question": f"问题 {index}",
        "core_claim": f"判断 {index}",
        "content_pillar": "AI视频质量",
        "audience_problem": "结果不稳定",
        "direction_relevance": score,
        "hook_strength": score,
        "visual_potential": score,
        "novelty": score,
        "evidence_availability": score,
        "production_difficulty": 20,
        "fact_risk": 10,
        "overall_score": score,
        "selection_reason": "综合评分高",
    }


def _direction() -> dict[str, object]:
    return {
        "series_name": "AI生成内容真相",
        "core_direction": "拆解 AI 内容生产",
        "audience": "创作者",
        "audience_problems": ["结果不稳定"],
        "content_goal": "给出可执行判断",
        "content_pillars": ["AI视频质量"],
        "tone": ["清晰"],
        "visual_style": "电影化知识短片",
        "allowed_topic_types": ["方法拆解"],
        "forbidden_topic_types": ["虚假数据"],
        "default_video_structure": ["钩子", "解释", "结论"],
        "differentiation": ["强调流程"],
        "repetition_risks": ["重复盘点"],
        "fact_risk_level": "medium",
    }


def _research() -> dict[str, object]:
    return {
        "summary": "稳定性来自可验证工作流。",
        "facts": [{
            "claim": "分阶段校验可降低返工",
            "source_title": "Workflow",
            "source_url": "https://example.com/workflow",
            "confidence": 0.9,
        }],
        "unknowns": [],
    }


def _script(title: str = "初稿") -> dict[str, object]:
    return {
        "title": title,
        "hook": "问题可能不在模型。",
        "segments": [{
            "segment_id": "SEG001",
            "purpose": "hook",
            "narration": "问题可能不在模型。",
            "visual_brief": "失败画面",
            "fact_refs": [],
        }],
        "call_to_action": "先做稳流程。",
        "estimated_duration_seconds": 55,
    }


def _review(passed: bool) -> dict[str, object]:
    return {
        "passed": passed,
        "scores": {
            "direction_fit": 90,
            "hook": 90,
            "clarity": 90,
            "evidence": 90 if passed else 40,
            "safety": 95,
        },
        "issues": [] if passed else ["证据不足"],
        "revision_instructions": [] if passed else ["仅保留有来源事实"],
    }


def _package() -> dict[str, object]:
    copy = {"title": "标题", "description": "简介", "hashtags": ["AI视频"]}
    return {
        "douyin": copy,
        "xiaohongshu": copy,
        "youtube_shorts": copy,
        "tiktok": copy,
    }


def _config() -> AppConfig:
    return AppConfig.model_validate({
        "direction": "拆解 AI 内容生产",
        "generation_budget": {"max_topic_candidates": 10},
        "autopilot": {"max_repair_rounds": 2},
    })


def test_m2_runner_executes_candidates_selection_two_repairs_and_packages(
    tmp_path: Path,
) -> None:
    responses = {
        "direction": [_direction()],
        "topics": [{"candidates": [_topic(i, 99 if i == 7 else 80) for i in range(1, 11)]}],
        "research": [_research()],
        "script": [_script()],
        "review": [_review(False), _review(False), _review(True)],
        "script_revision": [_script("一改"), _script("二改")],
        "package": [_package()],
    }
    client = SequencedClient(responses)
    repo = JobRepository(tmp_path / "data" / "content.db")

    manifest = _runner(client, repo, tmp_path / "outputs").run(
        "M2JOB001",
        _config(),
    )

    assert manifest["status"] == "ready_to_publish"
    assert manifest["topic_id"] == "T007"
    assert manifest["revision_rounds"] == 2
    assert len(json.loads((tmp_path / "outputs" / "M2JOB001" / "topics.json").read_text(
        encoding="utf-8"
    ))) == 10
    assert set(json.loads((tmp_path / "outputs" / "M2JOB001" / "package.json").read_text(
        encoding="utf-8"
    ))) == {"douyin", "xiaohongshu", "youtube_shorts", "tiktok"}
    status = repo.get_job("M2JOB001")
    assert status.current_stage == PipelineStage.CONTENT_PACKAGED
    assert status.topic_id == "T007"
    assert status.usage["llm_calls"] == 10
    assert status.usage["llm_input_tokens"] == 100
    assert status.usage["llm_output_tokens"] == 50
    output_dir = tmp_path / "outputs" / "M2JOB001"
    assert json.loads((output_dir / "script_revision_1.json").read_text(
        encoding="utf-8"
    ))["title"] == "一改"
    assert json.loads((output_dir / "review_1.json").read_text(
        encoding="utf-8"
    ))["passed"] is False
    assert json.loads((output_dir / "script_revision_2.json").read_text(
        encoding="utf-8"
    ))["title"] == "二改"
    assert json.loads((output_dir / "review_2.json").read_text(
        encoding="utf-8"
    ))["passed"] is True


def test_m2_runner_uses_revision_instructions_as_revision_engine_input(
    tmp_path: Path,
) -> None:
    failed_review = _review(False)
    failed_review["issues"] = ["概括问题"]
    failed_review["revision_instructions"] = ["执行指令"]
    responses = {
        "direction": [_direction()],
        "topics": [{"candidates": [_topic(i) for i in range(1, 9)]}],
        "research": [_research()],
        "script": [_script()],
        "review": [failed_review, _review(True)],
        "script_revision": [_script("已按指令修订")],
        "package": [_package()],
    }
    client = SequencedClient(responses)
    repo = JobRepository(tmp_path / "data" / "content.db")

    manifest = _runner(client, repo, tmp_path / "outputs").run(
        "M2JOB006",
        _config(),
    )

    revision_call = next(
        call for call in client.call_details if call["stage"] == "script_revision"
    )
    assert revision_call["user_payload"]["revision_instructions"] == ["执行指令"]
    assert "review" not in revision_call["user_payload"]
    assert manifest["status"] == "ready_to_publish"


def test_m2_runner_rejects_candidate_count_outside_eight_to_ten(tmp_path: Path) -> None:
    invalid_topics = {"candidates": [_topic(i) for i in range(1, 8)]}
    responses = {
        "direction": [_direction()],
        "topics": [invalid_topics, invalid_topics, invalid_topics],
    }
    client = SequencedClient(responses)
    repo = JobRepository(tmp_path / "data" / "content.db")

    try:
        _runner(client, repo, tmp_path / "outputs").run("M2JOB002", _config())
    except ValueError as error:
        assert "8 到 10" in str(error)
    else:
        raise AssertionError("候选数不足必须失败")

    status = repo.get_job("M2JOB002")
    assert status.current_stage == PipelineStage.FAILED_RETRYABLE
    assert status.failed_stage == PipelineStage.TOPICS_GENERATED


def test_m2_runner_stops_after_two_failed_repair_rounds(tmp_path: Path) -> None:
    responses = {
        "direction": [_direction()],
        "topics": [{"candidates": [_topic(i) for i in range(1, 9)]}],
        "research": [_research()],
        "script": [_script()],
        "review": [_review(False), _review(False), _review(False)],
        "script_revision": [_script("一改"), _script("二改")],
    }
    client = SequencedClient(responses)
    repo = JobRepository(tmp_path / "data" / "content.db")

    manifest = _runner(client, repo, tmp_path / "outputs").run(
        "M2JOB003",
        _config(),
    )

    assert manifest["status"] == "needs_revision"
    assert manifest["revision_rounds"] == 2
    assert "package" not in client.calls
    assert repo.get_job("M2JOB003").current_stage == PipelineStage.SCRIPT_REVIEWED


def test_m2_runner_persists_generated_script_before_review_validation_failure(
    tmp_path: Path,
) -> None:
    invalid_review = _review(False)
    invalid_review["issues"] = []
    invalid_review["revision_instructions"] = []
    responses = {
        "direction": [_direction()],
        "topics": [{"candidates": [_topic(i) for i in range(1, 9)]}],
        "research": [_research()],
        "script": [_script("已生成脚本")],
        "review": [invalid_review, invalid_review, invalid_review],
    }
    repo = JobRepository(tmp_path / "data" / "content.db")

    with pytest.raises(ValidationError):
        _runner(
            SequencedClient(responses),
            repo,
            tmp_path / "outputs",
        ).run("M2JOB004", _config())

    script_path = tmp_path / "outputs" / "M2JOB004" / "script.json"
    assert json.loads(script_path.read_text(encoding="utf-8"))["title"] == "已生成脚本"
    assert repo.get_job("M2JOB004").failed_stage == PipelineStage.SCRIPT_REVIEWED


def test_m2_runner_resumes_failed_stage_and_reuses_completed_artifacts(
    tmp_path: Path,
) -> None:
    invalid_review = _review(False)
    invalid_review["issues"] = []
    invalid_review["revision_instructions"] = []
    initial_responses = {
        "direction": [_direction()],
        "topics": [{"candidates": [_topic(i) for i in range(1, 9)]}],
        "research": [_research()],
        "script": [_script("可复用脚本")],
        "review": [invalid_review, invalid_review, invalid_review],
    }
    repo = JobRepository(tmp_path / "data" / "content.db")
    outputs_root = tmp_path / "outputs"
    job_id = "M2JOB005"

    with pytest.raises(ValidationError):
        _runner(
            SequencedClient(initial_responses),
            repo,
            outputs_root,
        ).run(job_id, _config())

    output_dir = outputs_root / job_id
    reusable_paths = [
        output_dir / "direction.json",
        output_dir / "topics.json",
        output_dir / "topic.json",
        output_dir / "research.json",
        output_dir / "script.json",
    ]
    original_artifacts = {path.name: path.read_bytes() for path in reusable_paths}
    resume_client = SequencedClient({
        "review": [_review(True)],
        "package": [_package()],
    })

    manifest = _runner(resume_client, repo, outputs_root).run(job_id, _config())

    assert manifest["status"] == "ready_to_publish"
    assert resume_client.calls == ["review", "package"]
    assert {
        path.name: path.read_bytes() for path in reusable_paths
    } == original_artifacts
    status = repo.get_job(job_id)
    assert status.current_stage == PipelineStage.CONTENT_PACKAGED
    assert status.failed_stage is None


def test_m2_runner_accumulates_only_each_run_usage_delta_across_resume(
    tmp_path: Path,
) -> None:
    invalid_review = _review(False)
    invalid_review["issues"] = []
    invalid_review["revision_instructions"] = []
    repo = JobRepository(tmp_path / "data" / "content.db")
    outputs_root = tmp_path / "outputs"
    job_id = "M2USAGE001"
    initial = SequencedClient({
        "direction": [_direction()],
        "topics": [{"candidates": [_topic(i) for i in range(1, 9)]}],
        "research": [_research()],
        "script": [_script()],
        "review": [invalid_review, invalid_review, invalid_review],
    })

    with pytest.raises(ValidationError):
        _runner(initial, repo, outputs_root).run(job_id, _config())

    first_usage = dict(repo.get_job(job_id).usage)
    assert first_usage["llm_calls"] == 7
    assert first_usage["llm_input_tokens"] == 70
    assert first_usage["llm_output_tokens"] == 35

    resumed = SequencedClient({
        "review": [_review(True)],
        "package": [_package()],
    })
    manifest = _runner(resumed, repo, outputs_root).run(job_id, _config())

    assert repo.get_job(job_id).usage["llm_calls"] == 9
    assert manifest["usage"] == {
        "prompt_tokens": 90,
        "completion_tokens": 45,
        "total_tokens": 135,
    }
    assert json.loads(
        (outputs_root / job_id / "usage.json").read_text(encoding="utf-8")
    ) == manifest["usage"]


def test_m2_runner_cache_hit_does_not_overwrite_historical_usage(
    tmp_path: Path,
) -> None:
    repo = JobRepository(tmp_path / "data" / "content.db")
    output_dir = tmp_path / "outputs" / "M2CACHE001"
    repo.create_job("M2CACHE001", output_dir)
    repo.increment_m2_usage(
        "M2CACHE001",
        llm_calls=4,
        llm_input_tokens=400,
        llm_output_tokens=200,
    )

    class CachedClient:
        usage = TokenUsage()
        logical_calls = 0

    runner = _runner(CachedClient(), repo, tmp_path / "outputs")
    runner._sync_usage("M2CACHE001")

    assert repo.get_job("M2CACHE001").usage["llm_calls"] == 4
    assert repo.get_job("M2CACHE001").usage["llm_input_tokens"] == 400
    assert repo.get_job("M2CACHE001").usage["llm_output_tokens"] == 200


def test_m2_runner_repairs_research_from_specific_source_errors_and_saves_evidence(
    tmp_path: Path,
) -> None:
    client = SequencedClient({
        "direction": [_direction()],
        "topics": [{"candidates": [_topic(i) for i in range(1, 9)]}],
        "research": [_research(), _research()],
        "script": [_script()],
        "review": [_review(True)],
        "package": [_package()],
    })
    verifier = StubSourceVerifier([["facts[0] URL HTTP 404"]])
    repo = JobRepository(tmp_path / "data" / "content.db")

    manifest = _runner(
        client,
        repo,
        tmp_path / "outputs",
        verifier,
    ).run("M2SOURCE001", _config())

    research_calls = [
        detail for detail in client.call_details if detail["stage"] == "research"
    ]
    assert len(research_calls) == 2
    assert research_calls[1]["user_payload"]["source_verification_errors"] == [
        "facts[0] URL HTTP 404"
    ]
    assert research_calls[1]["user_payload"]["repair_round"] == 1
    sources = json.loads(
        (
            tmp_path
            / "outputs"
            / "M2SOURCE001"
            / "research_sources.json"
        ).read_text(encoding="utf-8")
    )
    assert sources[0]["sha256"] == "a" * 64
    assert manifest["status"] == "ready_to_publish"


def test_m2_runner_blocks_after_two_source_support_repairs(tmp_path: Path) -> None:
    client = SequencedClient({
        "direction": [_direction()],
        "topics": [{"candidates": [_topic(i) for i in range(1, 9)]}],
        "research": [_research(), _research(), _research()],
    })
    verifier = StubSourceVerifier([
        ["facts[0] 中文关键词支持度不足"],
        ["facts[0] URL 不可达"],
        ["facts[0] 最终 URL 禁止访问私网"],
    ])
    repo = JobRepository(tmp_path / "data" / "content.db")

    with pytest.raises(SourceVerificationError, match="最终 URL"):
        _runner(
            client,
            repo,
            tmp_path / "outputs",
            verifier,
        ).run("M2SOURCE002", _config())

    assert client.calls.count("research") == 3
    assert "script" not in client.calls
    status = repo.get_job("M2SOURCE002")
    assert status.current_stage == PipelineStage.FAILED_RETRYABLE
    assert status.failed_stage == PipelineStage.RESEARCHED
    failed_sources = json.loads(
        (
            tmp_path
            / "outputs"
            / "M2SOURCE002"
            / "research_sources.json"
        ).read_text(encoding="utf-8")
    )
    assert failed_sources[0]["claim_supported"] is False
    assert failed_sources[0]["sha256"] == "f" * 64


def test_research_resume_uses_new_attempt_id_without_repeating_earlier_stages(
    tmp_path: Path,
) -> None:
    first_client = SequencedClient({
        "direction": [_direction()],
        "topics": [{"candidates": [_topic(i) for i in range(1, 9)]}],
        "research": [_research(), _research(), _research()],
    })
    repo = JobRepository(tmp_path / "data" / "content.db")
    outputs_root = tmp_path / "outputs"
    failing_verifier = StubSourceVerifier([
        ["facts[0] URL HTTP 404"],
        ["facts[0] URL HTTP 404"],
        ["facts[0] URL HTTP 404"],
    ])

    with pytest.raises(SourceVerificationError):
        _runner(
            first_client,
            repo,
            outputs_root,
            failing_verifier,
        ).run("M2ATTEMPT001", _config())

    rejections = json.loads(
        (
            outputs_root
            / "M2ATTEMPT001"
            / "research_rejections.json"
        ).read_text(encoding="utf-8")
    )
    assert rejections["urls"][0]["category"] == "PERMANENT_SOURCE_FAILURE"

    first_research_calls = [
        detail for detail in first_client.call_details
        if detail["stage"] == "research"
    ]
    assert all(
        "research_attempt_id" in detail["user_payload"]
        or (
            "original_request" in detail["user_payload"]
            and "research_attempt_id"
            in detail["user_payload"]["original_request"]
        )
        for detail in first_research_calls
    )
    first_ids = {
        detail["user_payload"]["research_attempt_id"]
        if "research_attempt_id" in detail["user_payload"]
        else detail["user_payload"]["original_request"]["research_attempt_id"]
        for detail in first_research_calls
    }
    assert len(first_ids) == 1

    resumed_client = SequencedClient({
        "research": [_research()],
        "script": [_script()],
        "review": [_review(True)],
        "package": [_package()],
    })
    _runner(
        resumed_client,
        repo,
        outputs_root,
        StubSourceVerifier(),
    ).run("M2ATTEMPT001", _config())

    resumed_research = next(
        detail for detail in resumed_client.call_details
        if detail["stage"] == "research"
    )
    second_id = resumed_research["user_payload"]["research_attempt_id"]
    assert second_id not in first_ids
    assert "direction" not in resumed_client.calls
    assert "topics" not in resumed_client.calls

    attempt = json.loads(
        (
            outputs_root
            / "M2ATTEMPT001"
            / "research_attempt.json"
        ).read_text(encoding="utf-8")
    )
    assert attempt["attempt_id"] == second_id
    assert attempt["reason"] == "automatic_retry"
