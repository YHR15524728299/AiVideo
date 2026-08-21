from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from uuid import uuid4

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
from aicf.job_service import ResearchResumeStrategy
from aicf.models.contracts import (
    DirectionProfile,
    PackageResult,
    ResearchResult,
    ReviewResult,
    ScriptResult,
)
from aicf.providers.openrouter import OpenRouterHTTPError, UpstreamRateLimitError
from aicf.research_policy import (
    ResearchPolicy,
    SourceFailureKind,
    derive_freshness,
)
from aicf.source_discovery import SourceDiscovery
from aicf.source_verifier import SourceVerificationError, SourceVerifier
from aicf.state_machine import FailureKind, PipelineStage


class M2ContentRunner:
    def __init__(
        self,
        client: StructuredClient,
        repository: JobRepository,
        outputs_root: str | Path,
        *,
        source_verifier: object | None = None,
        source_discovery: SourceDiscovery | None = None,
        research_policy: ResearchPolicy | None = None,
        research_strategy: ResearchResumeStrategy | None = None,
    ) -> None:
        self.client = client
        self.repository = repository
        self.outputs_root = Path(outputs_root)
        self.source_verifier = source_verifier or SourceVerifier()
        self.research_strategy = research_strategy
        self.source_discovery = (
            None
            if research_strategy == ResearchResumeStrategy.INTERNAL_KNOWLEDGE
            else source_discovery
        )
        if (
            research_strategy == ResearchResumeStrategy.RETRY_SOURCES
            and self.source_discovery is None
        ):
            raise ValueError("RETRY_SOURCES 必须启用 source discovery")
        self.research_policy = research_policy or ResearchPolicy()
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
        try:
            status = self.repository.get_job(job_id)
            output_dir = Path(status.output_dir)
        except KeyError:
            output_dir = self.outputs_root / job_id
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
            research_attempt_id = uuid4().hex
            attempt_reason = (
                self.research_strategy.value
                if (
                    status.failed_stage == PipelineStage.RESEARCHED
                    and self.research_strategy is not None
                )
                else (
                    "automatic_retry"
                    if status.failed_stage == PipelineStage.RESEARCHED
                    else "initial"
                )
            )
            self._write_json(
                output_dir / "research_attempt.json",
                {
                    "attempt_id": research_attempt_id,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "reason": attempt_reason,
                },
            )
            research, research_sources = self._stage(
                job_id,
                PipelineStage.RESEARCHED,
                lambda: self._run_research(
                    profile=profile,
                    selected=selected,
                    direction=config.direction,
                    output_dir=output_dir,
                    research_attempt_id=research_attempt_id,
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
        review = None
        revision_rounds = 0
        if PipelineStage.SCRIPT_REVIEWED in reusable_stages:
            review = self._read_reusable_review(output_dir / "review.json")
            if review is None or not review.passed:
                self.repository.invalidate_from(
                    job_id,
                    PipelineStage.SCRIPT_REVIEWED,
                )
                reusable_stages.discard(PipelineStage.SCRIPT_REVIEWED)
        if PipelineStage.SCRIPT_REVIEWED not in reusable_stages:
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

        assert review is not None
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

    def _read_reusable_review(self, path: Path) -> ReviewResult | None:
        try:
            return ReviewResult.model_validate(self._read_json(path))
        except (OSError, ValueError, ValidationError):
            return None

    def _run_research(
        self,
        *,
        profile: DirectionProfile,
        selected: dict[str, object],
        direction: str,
        output_dir: Path,
        research_attempt_id: str,
    ) -> tuple[ResearchResult, list[dict[str, Any]]]:
        if self.source_discovery is None:
            return self.research_engine.research_verified(
                profile,
                selected,
                self.source_verifier,
                research_attempt_id=research_attempt_id,
            )

        freshness = derive_freshness(
            f"{direction}\n{selected.get('title', '')}\n"
            f"{selected.get('core_question', '')}",
            today=datetime.now(timezone.utc).date(),
        )
        rejection_path = output_dir / "research_rejections.json"
        rejected_urls: set[str] = set()
        if rejection_path.exists():
            loaded = self._read_json(rejection_path)
            if isinstance(loaded, dict):
                rows = loaded.get("urls", [])
                if isinstance(rows, list):
                    rejected_urls = {
                        str(row.get("url"))
                        for row in rows
                        if isinstance(row, dict) and row.get("url")
                    }
        # 构建搜索query：加入领域限定词避免歧义，多query组合提高召回
        title = str(selected.get("title") or "").strip()
        core_question = str(selected.get("core_question") or "").strip()
        
        # 提取领域限定词
        domain_terms: list[str] = []
        if profile.series_name:
            domain_terms.append(str(profile.series_name))
        # 从allowed_topic_types提取金融相关关键词
        for topic_type in profile.allowed_topic_types or []:
            topic_str = str(topic_type)
            if any(kw in topic_str for kw in ["美联储", "FOMC", "利率", "通胀", "非农", "就业"]):
                # 提取核心金融术语
                for kw in ["美联储", "FOMC", "利率决议", "通胀数据", "非农就业"]:
                    if kw in topic_str and kw not in domain_terms:
                        domain_terms.append(kw)
                break
        
        # 通用财经限定词
        general_terms = ["财经", "2026"]
        if "美联储" in title or "降息" in title or "加息" in title or "点阵图" in title:
            general_terms.append("美联储")
            general_terms.append("FOMC")
        
        queries: list[str] = []
        
        # Query 1: 清理标点后的标题 + 领域限定词
        if title:
            title_clean = re.sub(r'[？?！!。，,、：:；;""''（）()\[\]【】]', '', title)
            queries.append(f"{title_clean} {' '.join(general_terms)}")
        
        # Query 2: 标题前15字 + 核心领域词
        if title and len(title) > 10:
            title_short = title[:15]
            domain_str = ' '.join(domain_terms[:2]) if domain_terms else '财经新闻'
            queries.append(f"{title_short} {domain_str}")
        
        # Query 3: 核心问题中的关键词 + 限定词
        if core_question:
            question_clean = re.sub(r'[？?！!。，,、：:；;""''（）()\[\]【】]', '', core_question)
            queries.append(f"{question_clean[:20]} 分析")
        
        # 去重并过滤过短的query
        queries = list(dict.fromkeys(q.strip() for q in queries if len(q.strip()) > 8))
        discovery = self.source_discovery.discover(
            queries=queries,
            freshness=freshness,
            rejected_urls=rejected_urls,
            limit=12,
        )
        candidates = discovery.candidates
        self._persist_research_rejections(
            output_dir,
            [dict(item) for item in discovery.rejections],
        )
        self._write_json(
            output_dir / "research_candidates.json",
            [
                {
                    "url": item.url,
                    "title": item.title,
                    "published_at": (
                        item.published_at.isoformat()
                        if item.published_at
                        else None
                    ),
                    "source_type": item.source_type,
                    "query": item.query,
                    "core_eligible": item.core_eligible,
                }
                for item in candidates
            ],
        )
        if len(candidates) < self.research_policy.minimum_verified_facts:
            raise SourceVerificationError(
                f"可用候选资料不足：找到 {len(candidates)} 条，"
                f"至少需要 {self.research_policy.minimum_verified_facts} 条",
                evidence=[{
                    "claim_supported": False,
                    "category": SourceFailureKind.INSUFFICIENT_EVIDENCE.value,
                    "verified": 0,
                    "total": len(candidates),
                }],
            )
        return self.research_engine.research_verified(
            profile,
            selected,
            self.source_verifier,
            research_attempt_id=research_attempt_id,
            source_candidates=candidates,
            freshness=freshness,
            policy=self.research_policy,
        )

    def revise_for_duration(
        self,
        job_id: str,
        error: NeedsScriptDurationRevision,
        round_number: int,
    ) -> dict[str, object]:
        status = self.repository.get_job(job_id)
        output_dir = Path(status.output_dir)
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
        review_attempt_id = uuid4().hex
        review = self.review_engine.review(
            profile,
            research,
            script,
            review_attempt_id=review_attempt_id,
        )
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
            review = self.review_engine.review(
                profile,
                research,
                script,
                review_attempt_id=review_attempt_id,
            )
            self._write_json(
                output_dir / f"review_{rounds}.json",
                review.model_dump(mode="json"),
            )
        if not review.passed:
            self._write_script(output_dir, script)
            self._write_json(
                output_dir / "review.json",
                review.model_dump(mode="json"),
            )
            summary = "；".join(review.issues[:3])
            raise ValueError(f"脚本审核未通过：{summary}")
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
                self._persist_research_rejections(
                    output_dir,
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
            if isinstance(
                error,
                (URLError, TimeoutError, UpstreamRateLimitError),
            ) or (
                isinstance(error, OpenRouterHTTPError)
                and (error.status_code == 429 or error.status_code >= 500)
            ):
                failure_kind = FailureKind.TRANSIENT_EXTERNAL
            elif isinstance(error, OpenRouterHTTPError):
                failure_kind = FailureKind.PERMANENT_EXTERNAL
            elif isinstance(error, OSError):
                failure_kind = FailureKind.LOCAL_ENVIRONMENT
            elif isinstance(error, (ValueError, ValidationError)):
                failure_kind = FailureKind.INVALID_ARTIFACT
            else:
                failure_kind = FailureKind.UNKNOWN
            recovery_command = (
                f"python -m aicf retry --job {job_id} --stage {stage.value}"
                if retryable
                else f"python -m aicf resume --job {job_id}"
            )
            self.repository.fail_stage(
                job_id,
                stage,
                str(error),
                retryable=retryable,
                failure_kind=failure_kind,
                recovery_command=recovery_command,
            )
            self._sync_usage(job_id)
            raise
        self.repository.complete_stage(job_id, stage)
        self._sync_usage(job_id)
        return result

    def _persist_research_rejections(
        self,
        output_dir: Path,
        evidence: list[dict[str, object]],
    ) -> None:
        path = output_dir / "research_rejections.json"
        rows: list[dict[str, object]] = []
        if path.exists():
            loaded = self._read_json(path)
            if isinstance(loaded, dict) and isinstance(loaded.get("urls"), list):
                rows = [
                    dict(item)
                    for item in loaded["urls"]
                    if isinstance(item, dict)
                ]
        by_url = {
            str(item.get("url")): item
            for item in rows
            if item.get("url")
        }
        for item in evidence:
            if (
                item.get("category")
                != SourceFailureKind.PERMANENT_SOURCE_FAILURE.value
            ):
                continue
            url = str(
                item.get("url")
                or item.get("original_url")
                or item.get("final_url")
                or ""
            )
            if not url:
                continue
            by_url[url] = {
                "url": url,
                "category": item["category"],
                "reason": str(item.get("error") or "永久失效来源"),
            }
        if by_url:
            self._write_json(path, {"urls": list(by_url.values())})

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
