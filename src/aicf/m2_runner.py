from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError

from pydantic import ValidationError

from aicf.config import AppConfig
from aicf.database import JobRepository
from aicf.engines.direction_engine import DirectionEngine
from aicf.engines.llm_engine import StructuredClient
from aicf.engines.narration_engine import NeedsScriptDurationRevision
from aicf.engines.package_engine import PackageCopyEngine
from aicf.engines.research_engine import ResearchEngine
from aicf.engines.review_engine import ReviewEngine
from aicf.engines.script_engine import (
    ScriptEngine,
    ScriptRevisionEngine,
    render_script_markdown,
)
from aicf.engines.topic_engine import TopicGenerationEngine, rank_topics, select_topic
from aicf.models.contracts import (
    DirectionProfile,
    PackageResult,
    ResearchResult,
    ReviewResult,
    ScriptResult,
)
from aicf.providers.openrouter import OpenRouterHTTPError, UpstreamRateLimitError
from aicf.source_verifier import SourceVerificationError, SourceVerifier
from aicf.state_machine import PipelineStage


class M2ContentRunner:
    def __init__(
        self,
        client: StructuredClient,
        repository: JobRepository,
        outputs_root: str | Path,
        *,
        source_verifier: object | None = None,
    ) -> None:
        self.client = client
        self.repository = repository
        self.outputs_root = Path(outputs_root)
        self.source_verifier = source_verifier or SourceVerifier()
        self._run_start_counters = self._client_counters()
        self._synced_run_counters = {"calls": 0, "prompt": 0, "completion": 0}
        self.direction_engine = DirectionEngine(client)
        self.topic_engine = TopicGenerationEngine(client)
        self.research_engine = ResearchEngine(client)
        self.script_engine = ScriptEngine(client)
        self.revision_engine = ScriptRevisionEngine(client)
        self.review_engine = ReviewEngine(client)
        self.package_engine = PackageCopyEngine(client)

    def run(self, job_id: str, config: AppConfig) -> dict[str, Any]:
        self._run_start_counters = self._client_counters()
        self._synced_run_counters = {"calls": 0, "prompt": 0, "completion": 0}
        output_dir = self.outputs_root / job_id
        try:
            status = self.repository.get_job(job_id)
        except KeyError:
            status = self.repository.create_job(job_id, output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        reusable_stages = set(status.completed_stages)

        direction_input = config.model_dump(mode="json")
        if PipelineStage.DIRECTION_LOADED not in reusable_stages:
            self._stage(
                job_id,
                PipelineStage.DIRECTION_LOADED,
                lambda: self._write_json(output_dir / "direction_input.json", direction_input),
            )
        if PipelineStage.DIRECTION_ANALYZED in reusable_stages:
            profile = DirectionProfile.model_validate(
                self._read_json(output_dir / "direction.json")
            )
        else:
            profile = self._stage(
                job_id,
                PipelineStage.DIRECTION_ANALYZED,
                lambda: self.direction_engine.analyze(direction_input),
            )
            self._write_json(output_dir / "direction.json", profile.model_dump(mode="json"))

        if PipelineStage.TOPICS_GENERATED in reusable_stages:
            ranked = self._read_json(output_dir / "topics.json")
        else:
            candidates = self._stage(
                job_id,
                PipelineStage.TOPICS_GENERATED,
                lambda: self.topic_engine.generate_candidates(
                    profile,
                    count=config.generation_budget.max_topic_candidates,
                ),
            )
            ranked = rank_topics(
                [candidate.model_dump(mode="json") for candidate in candidates],
                recent_history=[],
            )
            self._write_json(output_dir / "topics.json", ranked)
        if not isinstance(ranked, list) or not ranked:
            raise ValueError("topics.json 必须包含至少一个候选选题")

        if PipelineStage.TOPIC_SELECTED in reusable_stages:
            selected = self._read_json(output_dir / "topic.json")
        else:
            selected = select_topic(ranked, config.direction)
            self._stage(
                job_id,
                PipelineStage.TOPIC_SELECTED,
                lambda: self._write_json(output_dir / "topic.json", selected),
            )
            self.repository.update_m2_metadata(
                job_id,
                topic_id=str(selected["topic_id"]),
            )
        if not isinstance(selected, dict):
            raise ValueError("topic.json 必须是对象")

        if PipelineStage.RESEARCHED in reusable_stages:
            research = ResearchResult.model_validate(
                self._read_json(output_dir / "research.json")
            )
            sources_path = output_dir / "research_sources.json"
            if sources_path.exists():
                research_sources = self._read_json(sources_path)
            else:
                research_sources = self.source_verifier.verify_research(research)
                self._write_json(sources_path, research_sources)
        else:
            research, research_sources = self._stage(
                job_id,
                PipelineStage.RESEARCHED,
                lambda: self.research_engine.research_verified(
                    profile,
                    selected,
                    self.source_verifier,
                ),
            )
            self._write_json(output_dir / "research.json", research.model_dump(mode="json"))
            self._write_json(output_dir / "research_sources.json", research_sources)
        if PipelineStage.SCRIPT_GENERATED in reusable_stages:
            script = ScriptResult.model_validate(
                self._read_json(output_dir / "script.json")
            )
        else:
            script = self._stage(
                job_id,
                PipelineStage.SCRIPT_GENERATED,
                lambda: self.script_engine.write(profile, selected, research),
            )
            self._write_script(output_dir, script)
        if PipelineStage.SCRIPT_REVIEWED in reusable_stages:
            review = ReviewResult.model_validate(
                self._read_json(output_dir / "review.json")
            )
            revision_rounds = 0
        else:
            script, review, revision_rounds = self._stage(
                job_id,
                PipelineStage.SCRIPT_REVIEWED,
                lambda: self._review_and_revise(
                    profile,
                    research,
                    script,
                    config.autopilot.max_repair_rounds,
                    output_dir,
                ),
            )

        self._write_script(output_dir, script)
        self._write_json(output_dir / "review.json", review.model_dump(mode="json"))
        if not review.passed:
            manifest = {
                "status": "needs_revision",
                "topic_id": selected["topic_id"],
                "revision_rounds": revision_rounds,
                "issues": review.issues,
                "usage": self._cumulative_usage(job_id),
            }
            self._finish(output_dir, job_id, manifest)
            return manifest

        if PipelineStage.CONTENT_PACKAGED in reusable_stages:
            package = PackageResult.model_validate(
                self._read_json(output_dir / "package.json")
            )
        else:
            package = self._stage(
                job_id,
                PipelineStage.CONTENT_PACKAGED,
                lambda: self.package_engine.package(
                    script,
                    review,
                    list(config.platforms),
                ),
            )
        # youtube 是兼容长视频发布链路的可选扩展；旧版 M2 默认平台集合仍为四个平台。
        # Pydantic 默认会把未提供的可选字段序列化为 null，必须排除它，避免悄悄
        # 扩大 package.json / manifest 对外声明的平台集合。
        package_data = package.model_dump(mode="json", exclude_none=True)
        self._write_json(output_dir / "package.json", package_data)
        manifest = {
            "status": "ready_to_publish",
            "topic_id": selected["topic_id"],
            "revision_rounds": revision_rounds,
            "usage": self._cumulative_usage(job_id),
            "platforms": list(package_data),
        }
        self._finish(output_dir, job_id, manifest)
        return manifest

    def revise_for_duration(
        self,
        job_id: str,
        error: NeedsScriptDurationRevision,
        round_number: int,
    ) -> dict[str, object]:
        output_dir = self.outputs_root / job_id
        profile = DirectionProfile.model_validate(
            self._read_json(output_dir / "direction.json")
        )
        research = ResearchResult.model_validate(
            self._read_json(output_dir / "research.json")
        )
        script = ScriptResult.model_validate(
            self._read_json(output_dir / "script.json")
        )
        revised = self.revision_engine.revise_for_duration(
            profile,
            research,
            script,
            actual_duration_seconds=error.actual_duration_seconds,
            min_duration_seconds=error.min_duration_seconds,
            max_duration_seconds=error.max_duration_seconds,
            target_duration_seconds=error.target_duration_seconds,
            suggested_action=error.suggested_action,
        )
        self._write_json(
            output_dir / f"duration_revision_{round_number}.json",
            revised.model_dump(mode="json"),
        )
        review = self.review_engine.review(profile, research, revised)
        self._write_json(
            output_dir / f"review_duration_{round_number}.json",
            review.model_dump(mode="json"),
        )
        self._write_script(output_dir, revised)
        self._write_json(
            output_dir / "review.json",
            review.model_dump(mode="json"),
        )
        self._sync_usage(job_id)
        return {
            "passed": review.passed,
            "round": round_number,
            "issues": list(review.issues),
        }

    def _review_and_revise(
        self,
        profile: object,
        research: object,
        script: object,
        max_rounds: int,
        output_dir: Path,
    ) -> tuple[Any, Any, int]:
        rounds = 0
        review = self.review_engine.review(profile, research, script)
        while not review.passed and rounds < min(max_rounds, 2):
            rounds += 1
            script = self.revision_engine.revise(
                profile,
                research,
                script,
                list(review.revision_instructions),
            )
            self._write_json(
                output_dir / f"script_revision_{rounds}.json",
                script.model_dump(mode="json"),
            )
            review = self.review_engine.review(profile, research, script)
            self._write_json(
                output_dir / f"review_{rounds}.json",
                review.model_dump(mode="json"),
            )
        return script, review, rounds

    def _stage(
        self,
        job_id: str,
        stage: PipelineStage,
        action: Callable[[], Any],
    ) -> Any:
        self.repository.start_stage(job_id, stage)
        try:
            result = action()
        except Exception as error:
            if stage == PipelineStage.RESEARCHED and isinstance(
                error,
                SourceVerificationError,
            ):
                output_dir = self.outputs_root / job_id
                if error.research is not None:
                    self._write_json(output_dir / "research.json", error.research)
                self._write_json(
                    output_dir / "research_sources.json",
                    error.evidence,
                )
            # M2 阶段中的 ValueError / ValidationError 来自结构化生成结果，而非静态
            # 用户配置；重跑该阶段会重新生成，因此属于可恢复失败。
            retryable = (
                isinstance(
                    error,
                    (
                        ValueError,
                        ValidationError,
                        URLError,
                        TimeoutError,
                        OSError,
                        UpstreamRateLimitError,
                        SourceVerificationError,
                    ),
                )
                or (
                    isinstance(error, OpenRouterHTTPError)
                    and (error.status_code == 429 or error.status_code >= 500)
                )
            )
            self.repository.fail_stage(
                job_id,
                stage,
                str(error),
                retryable=retryable,
                recovery_command=f"python -m aicf content-run --job {job_id}",
            )
            self._sync_usage(job_id)
            raise
        self.repository.complete_stage(job_id, stage)
        self._sync_usage(job_id)
        return result

    def _finish(
        self,
        output_dir: Path,
        job_id: str,
        manifest: dict[str, Any],
    ) -> None:
        self._sync_usage(job_id)
        manifest["usage"] = self._cumulative_usage(job_id)
        self._write_json(output_dir / "usage.json", manifest["usage"])
        self._write_json(output_dir / "manifest.json", manifest)

    def _sync_usage(self, job_id: str) -> None:
        current = self._client_counters()
        run_counters = {
            key: max(0, current[key] - self._run_start_counters[key])
            for key in current
        }
        delta = {
            key: max(0, run_counters[key] - self._synced_run_counters[key])
            for key in run_counters
        }
        status = self.repository.increment_m2_usage(
            job_id,
            llm_calls=delta["calls"],
            llm_input_tokens=delta["prompt"],
            llm_output_tokens=delta["completion"],
        )
        self._synced_run_counters = run_counters
        self._write_json(
            Path(status.output_dir) / "usage.json",
            self._usage_from_status(status.usage),
        )

    def _client_counters(self) -> dict[str, int]:
        usage = getattr(self.client, "usage", None)
        recorded_calls = getattr(self.client, "logical_calls", None)
        if recorded_calls is None:
            client_calls = getattr(self.client, "calls", None)
            recorded_calls = len(client_calls) if isinstance(client_calls, list) else 0
        return {
            "calls": int(recorded_calls),
            "prompt": int(getattr(usage, "prompt_tokens", 0)),
            "completion": int(getattr(usage, "completion_tokens", 0)),
        }

    def _cumulative_usage(self, job_id: str) -> dict[str, int]:
        return self._usage_from_status(self.repository.get_job(job_id).usage)

    @staticmethod
    def _usage_from_status(usage: dict[str, int]) -> dict[str, int]:
        prompt = int(usage.get("llm_input_tokens", 0))
        completion = int(usage.get("llm_output_tokens", 0))
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        }

    @staticmethod
    def _write_script(output_dir: Path, script: Any) -> None:
        M2ContentRunner._write_json(
            output_dir / "script.json",
            script.model_dump(mode="json"),
        )
        (output_dir / "script.md").write_text(
            render_script_markdown(script),
            encoding="utf-8",
        )

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _read_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))
