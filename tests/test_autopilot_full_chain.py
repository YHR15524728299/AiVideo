from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from aicf.autopilot import Autopilot
from aicf.config import AppConfig
from aicf.database import JobRepository
from aicf.engines.narration_engine import NeedsScriptDurationRevision
from aicf.file_lock import os_file_lock
from aicf.production_settings import ProductionSettings
from aicf.providers.openrouter import UpstreamRateLimitError
from aicf.state_machine import ORDERED_STAGES, PipelineStage
from aicf.voice_validation import VoiceValidator


CONTENT_PACKAGED = getattr(PipelineStage, "CONTENT_PACKAGED", "CONTENT_PACKAGED")
M2_STAGES = [
    PipelineStage.DIRECTION_LOADED,
    PipelineStage.DIRECTION_ANALYZED,
    PipelineStage.TOPICS_GENERATED,
    PipelineStage.TOPIC_SELECTED,
    PipelineStage.RESEARCHED,
    PipelineStage.SCRIPT_GENERATED,
    PipelineStage.SCRIPT_REVIEWED,
    CONTENT_PACKAGED,
]


class FakeContentRunner:
    def __init__(self, repository: JobRepository, output_dir: Path) -> None:
        self.repository = repository
        self.output_dir = output_dir
        self.calls = 0
        self.duration_revisions: list[NeedsScriptDurationRevision] = []

    def run(self, job_id: str, _config: AppConfig) -> dict[str, object]:
        self.calls += 1
        completed = set(self.repository.get_job(job_id).completed_stages)
        for stage in M2_STAGES:
            if stage in completed:
                continue
            self.repository.start_stage(job_id, stage)
            self.repository.complete_stage(job_id, stage)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        script_path = self.output_dir / "script.json"
        if not script_path.exists():
            script_path.write_text(
                json.dumps(
                    {
                        "title": "Fake 全链",
                        "hook": "先验证全链。",
                        "segments": [
                            {
                                "segment_id": "SEG001",
                                "purpose": "hook",
                                "narration": "先验证全链。",
                                "visual_brief": "测试画面",
                                "fact_refs": [],
                            }
                        ],
                        "call_to_action": "完成。",
                        "key_phrases": ["全链"],
                        "estimated_duration_seconds": 3,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        (self.output_dir / "package.json").write_text(
            '{"douyin":{"title":"标题","description":"简介","hashtags":["测试"]}}',
            encoding="utf-8",
        )
        return {"status": "ready_to_publish"}

    def revise_for_duration(
        self,
        job_id: str,
        error: NeedsScriptDurationRevision,
        round_number: int,
    ) -> dict[str, object]:
        self.duration_revisions.append(error)
        script = json.loads(
            (self.output_dir / "script.json").read_text(encoding="utf-8")
        )
        script["segments"][0]["narration"] = (
            f"{error.suggested_action}-round-{round_number}"
        )
        script["estimated_duration_seconds"] = error.target_duration_seconds
        revision = self.output_dir / f"duration_revision_{round_number}.json"
        review = self.output_dir / f"review_duration_{round_number}.json"
        payload = json.dumps(script, ensure_ascii=False)
        revision.write_text(payload, encoding="utf-8")
        (self.output_dir / "script.json").write_text(payload, encoding="utf-8")
        review.write_text(
            '{"passed":true,"scores":{"direction_fit":90,"hook":90,'
            '"clarity":90,"evidence":90,"safety":90},'
            '"issues":[],"revision_instructions":[]}',
            encoding="utf-8",
        )
        return {"passed": True, "round": round_number}


class TransientDurationRevisionRunner(FakeContentRunner):
    def __init__(
        self,
        repository: JobRepository,
        output_dir: Path,
        failures: int,
    ) -> None:
        super().__init__(repository, output_dir)
        self.failures = failures
        self.revision_attempts = 0

    def revise_for_duration(
        self,
        job_id: str,
        error: NeedsScriptDurationRevision,
        round_number: int,
    ) -> dict[str, object]:
        self.revision_attempts += 1
        if self.revision_attempts <= self.failures:
            raise UpstreamRateLimitError(502, "temporary capacity")
        return super().revise_for_duration(job_id, error, round_number)


class FakeNarration:
    def __init__(self, actual_durations: list[float] | None = None) -> None:
        self.calls = 0
        self.actual_durations = list(actual_durations or [])

    def batch_synthesize(
        self,
        _script: object,
        output: Path,
        **durations: object,
    ) -> object:
        self.calls += 1
        if self.actual_durations:
            actual = self.actual_durations.pop(0)
            raise NeedsScriptDurationRevision(
                actual_duration_seconds=actual,
                min_duration_seconds=float(durations["min_duration_seconds"]),
                max_duration_seconds=float(durations["max_duration_seconds"]),
                target_duration_seconds=float(durations["target_duration_seconds"]),
            )
        output.mkdir(parents=True, exist_ok=True)
        voiceover = output / "voiceover.wav"
        timeline = output / "timeline.json"
        srt = output / "subtitles.srt"
        ass = output / "subtitles.ass"
        voiceover.write_bytes(b"fake-wav")
        timeline.write_text(
            '[{"script_segment_id":"SEG001","start_seconds":0,"end_seconds":3}]',
            encoding="utf-8",
        )
        srt.write_text("fake-srt", encoding="utf-8")
        ass.write_text("fake-ass", encoding="utf-8")
        return SimpleNamespace(
            voiceover_path=voiceover,
            timeline_path=timeline,
            srt_path=srt,
            ass_path=ass,
        )


class FakeVisualPlan:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, *, output_dir: Path, **_: object) -> object:
        self.calls += 1
        plan = output_dir / "visual_plan.json"
        manifest = output_dir / "asset_manifest.json"
        plan.write_text(
            json.dumps(
                {
                    "title": "Fake 全链",
                    "mode": "balanced",
                    "total_duration_seconds": 3,
                    "shots": [
                        {
                            "shot_id": "VIS001",
                            "script_segment_id": "SEG001",
                            "asset_type": "image",
                            "prompt": "测试画面",
                            "expected_path": "assets/VIS001.png",
                            "start_seconds": 0,
                            "duration_seconds": 3,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest.write_text('{"assets":[]}', encoding="utf-8")
        return SimpleNamespace(visual_plan_path=plan, asset_manifest_path=manifest)


class FakeAssets:
    def __init__(
        self,
        *,
        wait_once: bool = False,
        unknown_submission: bool = False,
    ) -> None:
        self.calls = 0
        self.submissions = 0
        self.wait_once = wait_once
        self.unknown_submission = unknown_submission
        self.received_usage_recorder = False
        self.received_budget_guard = False

    def run(
        self,
        visual_plan_path: Path,
        *,
        resume: bool,
        usage_recorder: object | None = None,
        budget_guard: object | None = None,
    ) -> dict[str, object]:
        self.calls += 1
        self.received_usage_recorder = callable(usage_recorder)
        self.received_budget_guard = callable(budget_guard)
        if self.unknown_submission:
            return {
                "status": "FAILED_NEEDS_ATTENTION",
                "reason": "UNKNOWN_REMOTE_SUBMISSION",
                "recovery_command": "人工核对远端提交",
            }
        tasks = visual_plan_path.parent / "assets" / "tasks.json"
        tasks.parent.mkdir(parents=True, exist_ok=True)
        if not tasks.exists():
            self.submissions += 1
            tasks.write_text('{"submit_id":"SUB001"}', encoding="utf-8")
        if self.wait_once:
            self.wait_once = False
            return {
                "status": "WAITING_EXTERNAL",
                "recovery_command": "python -m aicf resume --job FAKE001",
            }
        (tasks.parent / "VIS001.png").write_bytes(b"fake-image")
        (visual_plan_path.parent / "asset_manifest.json").write_text(
            '{"assets":[{"sha256":"fake"}]}',
            encoding="utf-8",
        )
        return {"status": "COMPLETED", "resume": resume}


class FakeRenderer:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    def render_and_validate(self, *, output_path: Path, **_: object) -> object:
        self.calls += 1
        if self.error:
            raise self.error
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"master")
        clean = output_path.with_name("clean.mp4")
        clean.write_bytes(b"clean")
        return SimpleNamespace(output_path=output_path, clean_output_path=clean), {}


class FakeM6:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, *, output_dir: Path, **_: object) -> dict[str, object]:
        self.calls += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "publish_manifest.json").write_text(
            '{"status":"READY_TO_PUBLISH"}',
            encoding="utf-8",
        )
        return {"status": "READY_TO_PUBLISH", "repair_rounds": 0}

    def verify_delivery(self, output_dir: Path) -> list[str]:
        manifest_path = output_dir / "publish_manifest.json"
        if not manifest_path.is_file():
            return ["publish_manifest.json 不存在"]
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        return list(manifest.get("verification_issues", []))


def _autopilot(
    tmp_path: Path,
    *,
    wait_once: bool = False,
    unknown_submission: bool = False,
    renderer_error: Exception | None = None,
    narration_durations: list[float] | None = None,
    config: AppConfig | None = None,
) -> tuple[JobRepository, Autopilot, dict[str, object]]:
    repository = JobRepository(tmp_path / "content.db")
    output = tmp_path / "outputs" / "FAKE001"
    repository.create_job("FAKE001", output)
    dependencies = {
        "content": FakeContentRunner(repository, output),
        "narration": FakeNarration(narration_durations),
        "visual": FakeVisualPlan(),
        "assets": FakeAssets(
            wait_once=wait_once,
            unknown_submission=unknown_submission,
        ),
        "renderer": FakeRenderer(renderer_error),
        "m6": FakeM6(),
    }
    autopilot = Autopilot(
        repository,
        content_runner=dependencies["content"],
        narration_pipeline=dependencies["narration"],
        visual_plan_runner=dependencies["visual"],
        asset_runner=dependencies["assets"],
        renderer=dependencies["renderer"],
        m6_pipeline=dependencies["m6"],
        config=config or AppConfig(direction="Fake"),
    )
    autopilot.sleep = lambda _: None
    return repository, autopilot, dependencies


def test_content_packaged_is_between_script_reviewed_and_audio() -> None:
    assert hasattr(PipelineStage, "CONTENT_PACKAGED")
    assert ORDERED_STAGES.index(CONTENT_PACKAGED) == (
        ORDERED_STAGES.index(PipelineStage.SCRIPT_REVIEWED) + 1
    )
    assert ORDERED_STAGES.index(PipelineStage.AUDIO_GENERATED) == (
        ORDERED_STAGES.index(CONTENT_PACKAGED) + 1
    )
    assert ORDERED_STAGES.index(PipelineStage.PACKAGED) > ORDERED_STAGES.index(
        PipelineStage.QA_CHECKED
    )


def test_autopilot_hashes_content_from_separate_output_root(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "content.db")
    job_dir = tmp_path / "data" / "jobs" / "SEPARATE001"
    content_root = tmp_path / "outputs"
    content_dir = content_root / "SEPARATE001"
    repository.create_job("SEPARATE001", job_dir)
    content = FakeContentRunner(repository, content_dir)
    autopilot = Autopilot(
        repository,
        content_runner=content,
        config=AppConfig(direction="测试"),
        content_output_root=content_root,
    )

    result = autopilot._ensure_content("SEPARATE001", job_dir)

    assert result is None
    status = repository.get_job("SEPARATE001")
    hashes = status.stages[CONTENT_PACKAGED.value]["artifact_hashes"]
    assert str(content_dir / "script.json") in hashes
    assert str(content_dir / "package.json") in hashes


def test_autopilot_consumes_content_from_separate_output_root(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "content.db")
    job_id = "CONSUME001"
    job_dir = tmp_path / "data" / "jobs" / job_id
    content_root = tmp_path / "outputs"
    content_dir = content_root / job_id
    repository.create_job(job_id, job_dir)
    content_dir.mkdir(parents=True)
    script = {
        "title": "内容目录标题",
        "hook": "开场",
        "segments": [
            {
                "segment_id": "SEG001",
                "narration": "来自内容目录。",
            }
        ],
        "call_to_action": "结束",
        "key_phrases": ["内容目录"],
    }
    package = {"douyin": {"title": "内容目录发布标题"}}
    (content_dir / "script.json").write_text(
        json.dumps(script, ensure_ascii=False),
        encoding="utf-8",
    )
    (content_dir / "package.json").write_text(
        json.dumps(package, ensure_ascii=False),
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class CapturingNarration(FakeNarration):
        def batch_synthesize(
            self,
            received_script: object,
            output: Path,
            **durations: object,
        ) -> object:
            captured["narration_script"] = received_script
            return super().batch_synthesize(
                received_script,
                output,
                **durations,
            )

    class CapturingVisual(FakeVisualPlan):
        def run(self, *, script_path: Path, **kwargs: object) -> object:
            captured["visual_script_path"] = script_path
            return super().run(**kwargs)

    class CapturingRenderer(FakeRenderer):
        def render_and_validate(self, *, title: str, **kwargs: object) -> object:
            captured["render_title"] = title
            return super().render_and_validate(**kwargs)

    class CapturingM6(FakeM6):
        def run(
            self,
            *,
            script: object,
            package: object,
            **kwargs: object,
        ) -> dict[str, object]:
            captured["m6_script"] = script
            captured["m6_package"] = package
            return super().run(**kwargs)

    autopilot = Autopilot(
        repository,
        narration_pipeline=CapturingNarration(),
        visual_plan_runner=CapturingVisual(),
        renderer=CapturingRenderer(),
        m6_pipeline=CapturingM6(),
        config=AppConfig(direction="测试"),
        content_output_root=content_root,
    )

    autopilot._invoke(
        job_id,
        PipelineStage.AUDIO_GENERATED,
        job_dir,
        continuing=False,
    )
    autopilot._invoke(
        job_id,
        PipelineStage.STORYBOARD_GENERATED,
        job_dir,
        continuing=False,
    )
    autopilot._invoke(
        job_id,
        PipelineStage.RENDERED,
        job_dir,
        continuing=False,
    )
    autopilot._invoke(
        job_id,
        PipelineStage.QA_CHECKED,
        job_dir,
        continuing=False,
    )

    assert captured == {
        "narration_script": script,
        "visual_script_path": content_dir / "script.json",
        "render_title": "内容目录标题",
        "m6_script": script,
        "m6_package": package,
    }


def test_autopilot_builds_only_job_selected_asset_provider(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "content.db")
    job_dir = tmp_path / "data" / "jobs" / "PROVIDER001"
    repository.create_job("PROVIDER001", job_dir)
    ProductionSettings(video_provider="jimeng").save_for_job(job_dir)
    selected: list[str] = []

    class FakeSelectedRunner:
        def run(self, _plan_path: Path, **_kwargs: object) -> dict[str, object]:
            return {"status": "COMPLETED"}

    autopilot = Autopilot(
        repository,
        asset_runner_factory=lambda provider: (
            selected.append(provider) or FakeSelectedRunner()
        ),
        config=AppConfig(direction="测试"),
    )

    result = autopilot._invoke(
        "PROVIDER001",
        PipelineStage.KEYFRAMES_GENERATED,
        job_dir,
        continuing=False,
    )

    assert result["status"] == "COMPLETED"
    assert selected == ["jimeng"]


def test_fake_autopilot_runs_real_m2_to_m6_stage_chain(tmp_path: Path) -> None:
    repository, autopilot, dependencies = _autopilot(tmp_path)

    result = autopilot.run("FAKE001")

    status = repository.get_job("FAKE001")
    assert result["status"] == "READY_TO_PUBLISH"
    assert status.current_stage == PipelineStage.COMPLETED
    assert status.failed_stage is None
    assert CONTENT_PACKAGED in status.completed_stages
    assert PipelineStage.PACKAGED in status.completed_stages
    assert PipelineStage.COMPLETED in status.completed_stages
    assert PipelineStage.AUTO_REPAIRED not in status.completed_stages
    for dependency in dependencies.values():
        assert dependency.calls == 1
    assert all(
        status.stages[stage.value].get("artifact_hashes")
        for stage in (
            CONTENT_PACKAGED,
            PipelineStage.AUDIO_GENERATED,
            PipelineStage.NARRATION_TIMELINE_CREATED,
            PipelineStage.STORYBOARD_GENERATED,
            PipelineStage.CLIP_PLAN_CREATED,
            PipelineStage.KEYFRAMES_GENERATED,
            PipelineStage.VIDEO_CLIPS_GENERATED,
            PipelineStage.SUBTITLES_GENERATED,
            PipelineStage.RENDERED,
            PipelineStage.QA_CHECKED,
            PipelineStage.PACKAGED,
        )
    )


@pytest.mark.parametrize(
    ("actual_duration", "expected_action"),
    [(30.0, "expand"), (110.0, "compress")],
)
def test_autopilot_revises_duration_then_invalidates_and_reruns_full_chain(
    tmp_path: Path,
    actual_duration: float,
    expected_action: str,
) -> None:
    repository, autopilot, dependencies = _autopilot(
        tmp_path,
        narration_durations=[actual_duration],
    )

    result = autopilot.run("FAKE001")

    output = tmp_path / "outputs" / "FAKE001"
    status = repository.get_job("FAKE001")
    assert result["status"] == "READY_TO_PUBLISH"
    assert status.current_stage == PipelineStage.COMPLETED
    assert dependencies["content"].calls == 2
    assert dependencies["narration"].calls == 2
    assert dependencies["visual"].calls == 1
    assert (output / "duration_revision_1.json").is_file()
    assert (output / "review_duration_1.json").is_file()
    revised = json.loads((output / "script.json").read_text(encoding="utf-8"))
    assert revised["segments"][0]["narration"] == f"{expected_action}-round-1"
    assert dependencies["content"].duration_revisions[0].suggested_action == (
        expected_action
    )
    packaged_hashes = status.stages[CONTENT_PACKAGED.value]["artifact_hashes"]
    assert packaged_hashes[str(output / "script.json")]


def test_autopilot_stops_after_two_duration_revisions_with_reopen_command(
    tmp_path: Path,
) -> None:
    repository, autopilot, dependencies = _autopilot(
        tmp_path,
        narration_durations=[20.0, 25.0, 30.0],
    )

    result = autopilot.run("FAKE001")

    output = tmp_path / "outputs" / "FAKE001"
    status = repository.get_job("FAKE001")
    assert result["status"] == "FAILED_NEEDS_ATTENTION"
    assert status.failed_stage == PipelineStage.AUDIO_GENERATED
    assert dependencies["narration"].calls == 3
    assert len(dependencies["content"].duration_revisions) == 2
    assert (output / "duration_revision_2.json").is_file()
    assert (output / "review_duration_2.json").is_file()
    assert result["recovery_command"] == (
        "python -m aicf reopen --job FAKE001 --confirm-artifacts-fixed"
    )
    assert "resume" not in result["recovery_command"]


def test_duration_revision_round_ignores_unreviewed_candidate(
    tmp_path: Path,
) -> None:
    output = tmp_path / "outputs" / "FAKE001"
    output.mkdir(parents=True)
    (output / "duration_revision_1.json").write_text("{}", encoding="utf-8")
    (output / "review_duration_1.json").write_text("{}", encoding="utf-8")
    (output / "duration_revision_2.json").write_text("{}", encoding="utf-8")

    assert Autopilot._next_duration_revision_round(output) == 2


def test_duration_revision_retries_transient_upstream_error(
    tmp_path: Path,
) -> None:
    repository, autopilot, dependencies = _autopilot(
        tmp_path,
        narration_durations=[110.0],
    )
    output = tmp_path / "outputs" / "FAKE001"
    runner = TransientDurationRevisionRunner(repository, output, failures=1)
    dependencies["content"] = runner
    autopilot.content_runner = runner
    autopilot.sleep = lambda _: None

    result = autopilot.run("FAKE001")

    assert result["status"] == "READY_TO_PUBLISH"
    assert runner.revision_attempts == 2


def test_asr_failure_stops_before_visual_generation(tmp_path: Path) -> None:
    repository, autopilot, dependencies = _autopilot(tmp_path)

    class MissingContentAsr:
        def transcribe(self, _audio_path: Path) -> tuple[str, str]:
            return "无法识别关键内容", "zh-CN"

    autopilot.voice_validator = VoiceValidator(MissingContentAsr())
    result = autopilot.run("FAKE001")

    assert result["status"] == "FAILED_NEEDS_ATTENTION"
    assert "旁白 ASR 验收失败" in result["reason"]
    assert dependencies["visual"].calls == 0


def test_formal_script_derives_key_phrases_without_non_contract_field() -> None:
    script = {
        "title": "正式标题",
        "hook": "问题可能根本不在模型。",
        "segments": [
            {
                "segment_id": "SEG001",
                "purpose": "hook",
                "narration": "问题可能根本不在模型。",
                "visual_brief": "画面",
                "fact_refs": [],
            },
            {
                "segment_id": "SEG002",
                "purpose": "call_to_action",
                "narration": "关注内容工厂。",
                "visual_brief": "画面",
                "fact_refs": [],
            },
        ],
        "call_to_action": "关注内容工厂。",
        "estimated_duration_seconds": 3,
    }

    assert Autopilot._key_phrases(script) == (
        "问题可能根本不在模型。",
        "关注内容工厂。",
    )


def test_completed_job_revalidates_manifest_and_transitions_to_attention(
    tmp_path: Path,
) -> None:
    repository, autopilot, _ = _autopilot(tmp_path)
    assert autopilot.run("FAKE001")["status"] == "READY_TO_PUBLISH"
    manifest_path = (
        tmp_path / "outputs" / "FAKE001" / "delivery" / "publish_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["verification_issues"] = ["douyin/video.mp4 SHA256 不匹配"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = autopilot.run("FAKE001")

    status = repository.get_job("FAKE001")
    assert result["status"] == "FAILED_NEEDS_ATTENTION"
    assert "SHA256" in result["reason"]
    assert status.current_stage == PipelineStage.FAILED_NEEDS_ATTENTION
    assert status.failed_stage == PipelineStage.COMPLETED
    for invalidated in (
        PipelineStage.QA_CHECKED,
        PipelineStage.AUTO_REPAIRED,
        PipelineStage.PACKAGED,
        PipelineStage.COMPLETED,
    ):
        assert invalidated not in status.completed_stages
        if invalidated != PipelineStage.COMPLETED:
            assert invalidated.value not in status.stages
    assert status.next_resume_command == (
        "python -m aicf reopen --job FAKE001 --confirm-artifacts-fixed"
    )

    reopened = repository.reopen_failed_attention(
        "FAKE001",
        artifacts_fixed=True,
    )
    assert reopened.current_stage == PipelineStage.RENDERED
    assert autopilot.run("FAKE001")["status"] == "READY_TO_PUBLISH"


def test_completed_job_rejects_manifest_that_no_longer_matches_database_hash(
    tmp_path: Path,
) -> None:
    repository, autopilot, _ = _autopilot(tmp_path)
    assert autopilot.run("FAKE001")["status"] == "READY_TO_PUBLISH"
    manifest_path = (
        tmp_path / "outputs" / "FAKE001" / "delivery" / "publish_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["untracked_change"] = True
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = autopilot.run("FAKE001")

    assert result["status"] == "FAILED_NEEDS_ATTENTION"
    assert "数据库记录的 manifest hash 不匹配" in result["reason"]
    assert repository.get_job("FAKE001").failed_stage == PipelineStage.COMPLETED


def test_waiting_external_stays_on_m4_and_resume_does_not_resubmit(
    tmp_path: Path,
) -> None:
    repository, autopilot, dependencies = _autopilot(tmp_path, wait_once=True)

    waiting = autopilot.run("FAKE001")

    status = repository.get_job("FAKE001")
    assert waiting["status"] == "WAITING_EXTERNAL"
    assert status.current_stage == PipelineStage.KEYFRAMES_GENERATED
    assert status.failed_stage is None
    assert status.next_resume_command == "python -m aicf resume --job FAKE001"
    assert dependencies["assets"].submissions == 1

    completed = autopilot.run("FAKE001")

    assert completed["status"] == "READY_TO_PUBLISH"
    assert dependencies["assets"].submissions == 1
    assert dependencies["content"].calls == 1
    assert dependencies["narration"].calls == 1
    assert dependencies["visual"].calls == 1


def test_failure_is_recorded_against_actual_stage(tmp_path: Path) -> None:
    repository, autopilot, _ = _autopilot(
        tmp_path,
        renderer_error=RuntimeError("编码器失败"),
    )

    result = autopilot.run("FAKE001")

    status = repository.get_job("FAKE001")
    assert result["status"] == "FAILED_RETRYABLE"
    assert status.failed_stage == PipelineStage.RENDERED
    assert "编码器失败" in status.stages[PipelineStage.RENDERED.value]["error"]
    assert status.next_resume_command == "python -m aicf resume --job FAKE001"

    autopilot.renderer.error = None
    resumed = autopilot.run("FAKE001")

    assert resumed["status"] == "READY_TO_PUBLISH"
    assert repository.get_job("FAKE001").current_stage == PipelineStage.COMPLETED


def test_autopilot_sanitizes_persisted_and_returned_failure_reason(
    tmp_path: Path,
) -> None:
    secret_path = tmp_path / "private" / "render.mp4"
    repository, autopilot, _ = _autopilot(
        tmp_path,
        renderer_error=RuntimeError(
            f"Bearer persisted-secret token=db-secret path={secret_path}"
        ),
    )

    result = autopilot.run("FAKE001")
    persisted = repository.get_job("FAKE001").stages[
        PipelineStage.RENDERED.value
    ]["error"]

    for rendered in (str(result["reason"]), str(persisted)):
        assert "persisted-secret" not in rendered
        assert "db-secret" not in rendered
        assert str(secret_path) not in rendered
        assert "***REDACTED***" in rendered


def test_repository_migrates_legacy_m2_packaged_job_to_content_packaged(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "content.db")
    output = tmp_path / "outputs" / "M2REAL001"
    repository.create_job("M2REAL001", output)
    for stage in M2_STAGES[:-1]:
        repository.start_stage("M2REAL001", stage)
        repository.complete_stage("M2REAL001", stage)
    legacy = repository.get_job("M2REAL001").model_dump(mode="json")
    legacy["current_stage"] = PipelineStage.PACKAGED.value
    legacy["completed_stages"].append(PipelineStage.PACKAGED.value)
    legacy["stages"][PipelineStage.PACKAGED.value] = {
        "started_at": legacy["updated_at"],
        "completed_at": legacy["updated_at"],
    }
    with repository._connect() as connection:
        connection.execute(
            "UPDATE jobs SET status_json = ? WHERE job_id = ?",
            (json.dumps(legacy, ensure_ascii=False), "M2REAL001"),
        )

    migrated = JobRepository(tmp_path / "content.db").get_job("M2REAL001")

    assert migrated.current_stage == CONTENT_PACKAGED
    assert CONTENT_PACKAGED in migrated.completed_stages
    assert PipelineStage.PACKAGED not in migrated.completed_stages
    assert CONTENT_PACKAGED.value in migrated.stages
    assert PipelineStage.PACKAGED.value not in migrated.stages


def test_repository_records_confirmed_m4_submission_atomically_and_idempotently(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "content.db")
    repository.create_job("USAGE001", tmp_path / "outputs" / "USAGE001")

    for _ in range(2):
        repository.record_m4_submission(
            "USAGE001",
            request_id="request-001",
            jimeng_images=0,
            jimeng_video_clips=1,
            jimeng_video_seconds_requested=5,
        )

    usage = repository.get_job("USAGE001").usage
    assert usage["jimeng_images"] == 0
    assert usage["jimeng_video_clips"] == 1
    assert usage["jimeng_video_seconds_requested"] == 5


def test_autopilot_injects_usage_recorder_and_budget_guard_into_m4(
    tmp_path: Path,
) -> None:
    _, autopilot, dependencies = _autopilot(tmp_path)

    result = autopilot.run("FAKE001")

    assert result["status"] == "READY_TO_PUBLISH"
    assert dependencies["assets"].received_usage_recorder is True
    assert dependencies["assets"].received_budget_guard is True


def test_unknown_remote_submission_marks_m4_failed_needs_attention(
    tmp_path: Path,
) -> None:
    repository, autopilot, dependencies = _autopilot(
        tmp_path,
        unknown_submission=True,
    )

    result = autopilot.run("FAKE001")

    status = repository.get_job("FAKE001")
    assert result["status"] == "FAILED_NEEDS_ATTENTION"
    assert result["reason"] == "UNKNOWN_REMOTE_SUBMISSION"
    assert status.current_stage == PipelineStage.FAILED_NEEDS_ATTENTION
    assert status.failed_stage == PipelineStage.KEYFRAMES_GENERATED
    assert status.stages[PipelineStage.KEYFRAMES_GENERATED.value]["recoverable"] is False
    assert dependencies["assets"].calls == 1


def test_configured_jimeng_budget_blocks_before_remote_submission(
    tmp_path: Path,
) -> None:
    config = AppConfig.model_validate(
        {
            "direction": "Fake",
            "generation_budget": {
                "max_jimeng_images": 0,
                "max_jimeng_video_clips": 10,
                "max_jimeng_video_seconds_requested": 60,
            },
        }
    )
    repository, autopilot, _ = _autopilot(tmp_path, config=config)
    guard = autopilot._m4_budget_guard("FAKE001")

    with pytest.raises(Exception, match="即梦图片预算上限"):
        guard(
            jimeng_images=1,
            jimeng_video_clips=0,
            jimeng_video_seconds_requested=0,
        )

    assert repository.get_job("FAKE001").usage["jimeng_images"] == 0


def test_second_autopilot_process_returns_already_running_without_commit(
    tmp_path: Path,
) -> None:
    repository, autopilot, dependencies = _autopilot(tmp_path)
    before = repository.get_job("FAKE001")
    lock_path = Path(before.output_dir) / ".autopilot.lock"

    with os_file_lock(lock_path, timeout=0, timeout_message="test lock busy"):
        result = autopilot.run("FAKE001")

    after = repository.get_job("FAKE001")
    assert result == {
        "status": "JOB_ALREADY_RUNNING",
        "job_id": "FAKE001",
    }
    assert after.version == before.version
    assert after.current_stage == before.current_stage
    assert dependencies["content"].calls == 0


def test_m4_reservation_atomically_persists_intent_usage_and_budget(
    tmp_path: Path,
) -> None:
    repository = JobRepository(tmp_path / "content.db")
    repository.create_job("RESERVE001", tmp_path / "outputs" / "RESERVE001")
    limits = {
        "jimeng_images": 1,
        "jimeng_video_clips": 10,
        "jimeng_video_seconds_requested": 60,
    }

    reserved = repository.reserve_m4_submission(
        "RESERVE001",
        request_id="intent-001",
        limits=limits,
        jimeng_images=1,
    )

    assert reserved.usage["jimeng_images"] == 1
    with repository._connect() as connection:
        row = connection.execute(
            "SELECT job_id, intent_status FROM m4_usage_events WHERE request_id = ?",
            ("intent-001",),
        ).fetchone()
    assert dict(row) == {
        "job_id": "RESERVE001",
        "intent_status": "reserved",
    }

    with pytest.raises(Exception, match="即梦图片预算上限"):
        repository.reserve_m4_submission(
            "RESERVE001",
            request_id="intent-002",
            limits=limits,
            jimeng_images=1,
        )

    assert repository.get_job("RESERVE001").usage["jimeng_images"] == 1
