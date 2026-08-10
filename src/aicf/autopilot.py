from __future__ import annotations

import json
import hashlib
import subprocess
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError

from aicf.config import AppConfig
from aicf.database import JobRepository
from aicf.delivery_view import finalize_user_delivery
from aicf.engines.narration_engine import NeedsScriptDurationRevision
from aicf.file_lock import os_file_lock
from aicf.logging_utils import sanitize_error
from aicf.providers.openrouter import OpenRouterHTTPError, UpstreamRateLimitError
from aicf.production_settings import ProductionSettings
from aicf.state_machine import PipelineStage
from aicf.voice_validation import VoiceValidator


class NeedsAttention(RuntimeError):
    def __init__(self, message: str, recovery_command: str) -> None:
        super().__init__(message)
        self.recovery_command = recovery_command


class Autopilot:
    def __init__(
        self,
        repository: JobRepository,
        m6_pipeline: object | None = None,
        *,
        content_runner: object | None = None,
        narration_pipeline: object | None = None,
        visual_plan_runner: object | None = None,
        asset_runner: object | None = None,
        renderer: object | None = None,
        config: AppConfig | None = None,
        voice_validator: VoiceValidator | None = None,
        user_output_root: str | Path | None = None,
        content_output_root: str | Path | None = None,
        asset_runner_factory: Callable[[str], object] | None = None,
    ) -> None:
        self.repository = repository
        self.m6_pipeline = m6_pipeline
        self.content_runner = content_runner
        self.narration_pipeline = narration_pipeline
        self.visual_plan_runner = visual_plan_runner
        self.asset_runner = asset_runner
        self.asset_runner_factory = asset_runner_factory
        self.renderer = renderer
        self.config = config
        self.voice_validator = voice_validator or VoiceValidator()
        self.user_output_root = (
            Path(user_output_root) if user_output_root is not None else None
        )
        self.content_output_root = (
            Path(content_output_root) if content_output_root is not None else None
        )
        self.sleep = time.sleep

    def run(self, job_id: str) -> dict[str, Any]:
        status = self.repository.get_job(job_id)
        lock = os_file_lock(
            Path(status.output_dir) / ".autopilot.lock",
            timeout=0,
            timeout_message=f"Job {job_id} 已在运行",
        )
        try:
            lock.__enter__()
        except TimeoutError:
            return {"status": "JOB_ALREADY_RUNNING", "job_id": job_id}
        try:
            return self._run_locked(job_id)
        finally:
            lock.__exit__(None, None, None)

    def _run_locked(self, job_id: str) -> dict[str, Any]:
        max_auto_retries = 3
        for retry_attempt in range(max_auto_retries + 1):
            status = self.repository.get_job(job_id)
            job_dir = Path(status.output_dir)
            if status.current_stage == PipelineStage.COMPLETED:
                manifest = self._completed_manifest(job_id, job_dir)
                self._finalize_user_delivery(job_id, job_dir, manifest)
                return manifest

            content_result = self._ensure_content(job_id, job_dir)
            if content_result is not None:
                if content_result.get("status") == "FAILED_RETRYABLE" and retry_attempt < max_auto_retries:
                    wait = min(60, 10 * (2**retry_attempt))  # 10s, 20s, 40s
                    print(f"[autopilot] 内容阶段可重试失败，{wait}s 后自动重试 (第{retry_attempt+1}次)", flush=True)
                    self.sleep(wait)
                    # 重置失败阶段状态以重试
                    self._reset_failed_stage(job_id)
                    continue
                return content_result

            manifest: dict[str, Any] | None = None
            stages = [
                PipelineStage.AUDIO_GENERATED,
                PipelineStage.NARRATION_TIMELINE_CREATED,
                PipelineStage.STORYBOARD_GENERATED,
                PipelineStage.CLIP_PLAN_CREATED,
                PipelineStage.KEYFRAMES_GENERATED,
                PipelineStage.VIDEO_CLIPS_GENERATED,
                PipelineStage.SUBTITLES_GENERATED,
                PipelineStage.MASTER_TIMELINE_ASSEMBLED,
                PipelineStage.RENDERED,
                PipelineStage.QA_CHECKED,
            ]
            failed_retryable = False
            start = self._resume_index(job_id, stages)
            for stage in stages[start:]:
                try:
                    result = self._run_stage(job_id, job_dir, stage)
                except NeedsScriptDurationRevision as error:
                    return self._handle_duration_revision(job_id, job_dir, error)
                if result.get("status") == "WAITING_EXTERNAL":
                    return result
                if result.get("status") == "FAILED_NEEDS_ATTENTION":
                    return result
                if result.get("status") == "FAILED_RETRYABLE":
                    failed_retryable = True
                    break
                if stage == PipelineStage.QA_CHECKED:
                    manifest = result

            if failed_retryable:
                if retry_attempt < max_auto_retries:
                    wait = min(60, 10 * (2**retry_attempt))
                    failed_stage_info = self.repository.get_job(job_id)
                    failed_name = failed_stage_info.failed_stage.value if failed_stage_info.failed_stage else "未知"
                    print(f"[autopilot] 阶段 {failed_name} 可重试失败，{wait}s 后自动重试 (第{retry_attempt+1}次)", flush=True)
                    self.sleep(wait)
                    self._reset_failed_stage(job_id)
                    continue
                # 超过最大重试次数，返回 FAILED_RETRYABLE 让用户手动决定
                status = self.repository.get_job(job_id)
                failed = status.failed_stage
                reason = (
                    str(status.stages.get(failed.value, {}).get("error", ""))
                    if failed is not None
                    else "自动重试耗尽"
                )
                return {
                    "status": "FAILED_RETRYABLE",
                    "reason": reason,
                    "recovery_command": f"python -m aicf resume --job {job_id}",
                }

            if manifest is None:
                manifest = self._load_delivery_manifest(job_dir)
            if int(manifest.get("repair_rounds", 0)) > 0:
                repair = self._complete_marker_stage(
                    job_id,
                    job_dir,
                    PipelineStage.AUTO_REPAIRED,
                    [job_dir / "delivery" / "publish_manifest.json"],
                )
                if repair:
                    if repair.get("status") == "FAILED_RETRYABLE" and retry_attempt < max_auto_retries:
                        wait = min(60, 10 * (2**retry_attempt))
                        self.sleep(wait)
                        self._reset_failed_stage(job_id)
                        continue
                    return repair
            packaged = self._complete_marker_stage(
                job_id,
                job_dir,
                PipelineStage.PACKAGED,
                [job_dir / "delivery" / "publish_manifest.json"],
            )
            if packaged:
                if packaged.get("status") == "FAILED_RETRYABLE" and retry_attempt < max_auto_retries:
                    wait = min(60, 10 * (2**retry_attempt))
                    self.sleep(wait)
                    self._reset_failed_stage(job_id)
                    continue
                return packaged
            completed = self._complete_marker_stage(
                job_id,
                job_dir,
                PipelineStage.COMPLETED,
                [job_dir / "delivery" / "publish_manifest.json"],
            )
            result = completed or manifest
            self._finalize_user_delivery(job_id, job_dir, result)
            return result
        # 理论上不会到这里
        return {"status": "FAILED_RETRYABLE", "reason": "重试耗尽", "recovery_command": f"python -m aicf resume --job {job_id}"}

    def _reset_failed_stage(self, job_id: str) -> None:
        """重置失败阶段，使其可以重新运行。"""
        status = self.repository.get_job(job_id)
        failed = status.failed_stage
        if failed is None:
            return
        # 把失败阶段从 stages 记录中移除，重置到该阶段之前
        # 使用 reopen 机制
        try:
            self.repository.reopen_failed_attention(
                job_id,
                recoverable_reason="auto_retry",
            )
        except Exception:
            pass

    def _ensure_content(
        self,
        job_id: str,
        job_dir: Path,
    ) -> dict[str, Any] | None:
        status = self.repository.get_job(job_id)
        content_dir = self._content_dir(job_id, job_dir)
        if PipelineStage.CONTENT_PACKAGED in status.completed_stages:
            hashes = self._artifact_hashes(
                [content_dir / "script.json", content_dir / "package.json"]
            )
            recorded = status.stages.get(
                PipelineStage.CONTENT_PACKAGED.value, {}
            ).get("artifact_hashes")
            if recorded is not None and recorded != hashes:
                return self._fail_existing_stage(
                    job_id,
                    PipelineStage.CONTENT_PACKAGED,
                    "内容包产物哈希已变化，拒绝复用",
                )
            if recorded is None:
                self.repository.record_artifact_hashes(
                    job_id,
                    PipelineStage.CONTENT_PACKAGED,
                    hashes,
                )
            return None
        if self.content_runner is None or self.config is None:
            return self._missing_handler(job_id, PipelineStage.CONTENT_PACKAGED)
        try:
            result = self.content_runner.run(job_id, self.config)
            if result.get("status") != "ready_to_publish":
                raise NeedsAttention(
                    "M2 内容审核未通过",
                    f"python -m aicf resume --job {job_id}",
                )
            hashes = self._artifact_hashes(
                [content_dir / "script.json", content_dir / "package.json"]
            )
            self.repository.record_artifact_hashes(
                job_id,
                PipelineStage.CONTENT_PACKAGED,
                hashes,
            )
            return None
        except Exception as error:
            status = self.repository.get_job(job_id)
            if status.failed_stage is not None:
                result = self._failure_result(status.next_resume_command, error)
                result["status"] = status.current_stage.value
                return result
            return self._fail_existing_stage(
                job_id,
                PipelineStage.CONTENT_PACKAGED,
                str(error),
            )

    def _run_stage(
        self,
        job_id: str,
        job_dir: Path,
        stage: PipelineStage,
    ) -> dict[str, Any]:
        status = self.repository.get_job(job_id)
        record = status.stages.get(stage.value, {})
        continuing = (
            status.current_stage == stage
            and stage not in status.completed_stages
            and status.failed_stage is None
        )
        if not continuing:
            self.repository.start_stage(job_id, stage)
        try:
            result = self._invoke(
                job_id,
                stage,
                job_dir,
                continuing=continuing,
            )
            if result.get("status") == "WAITING_EXTERNAL":
                recovery = f"python -m aicf resume --job {job_id}"
                self.repository.wait_stage(job_id, stage, recovery)
                return {**result, "recovery_command": recovery}
            if result.get("status") == "FAILED_NEEDS_ATTENTION":
                raise NeedsAttention(
                    str(result.get("reason") or "阶段需要人工处理"),
                    str(
                        result.get("recovery_command")
                        or f"python -m aicf resume --job {job_id}"
                    ),
                )
            if stage == PipelineStage.QA_CHECKED and result.get("status") != (
                "READY_TO_PUBLISH"
            ):
                raise NeedsAttention(
                    "M6 QA 未通过，自动修复已达到两轮上限",
                    str(
                        result.get(
                            "recovery_command",
                            f"python -m aicf resume --job {job_id}",
                        )
                    ).replace("<JOB_ID>", job_id),
                )
            hashes = self._artifact_hashes(self._artifacts(stage, job_dir))
            self.repository.complete_stage(
                job_id,
                stage,
                artifact_hashes=hashes,
            )
            return result
        except NeedsScriptDurationRevision:
            raise
        except Exception as error:
            recovery = (
                error.recovery_command
                if isinstance(error, NeedsAttention) and error.recovery_command
                else f"python -m aicf resume --job {job_id}"
            )
            details = self._error_details(error)
            # 只有明确的临时性/网络错误才可重试；ffmpeg 失败、文件缺失、配置错误、NeedsAttention 等不可重试
            retryable = isinstance(
                error,
                (URLError, TimeoutError, OSError, UpstreamRateLimitError),
            ) or (
                isinstance(error, OpenRouterHTTPError)
                and (error.status_code == 429 or error.status_code >= 500)
            ) or (
                stage == PipelineStage.RENDERED
                and isinstance(
                    error,
                    (RuntimeError, subprocess.CalledProcessError),
                )
            ) or (
                stage == PipelineStage.QA_CHECKED
                and isinstance(error, subprocess.CalledProcessError)
            )
            self.repository.fail_stage(
                job_id,
                stage,
                details,
                retryable=retryable,
                recovery_command=recovery,
            )
            return {
                "status": (
                    "FAILED_RETRYABLE"
                    if retryable
                    else "FAILED_NEEDS_ATTENTION"
                ),
                "reason": details,
                "recovery_command": recovery,
            }

    def _handle_duration_revision(
        self,
        job_id: str,
        job_dir: Path,
        error: NeedsScriptDurationRevision,
    ) -> dict[str, Any]:
        round_number = self._next_duration_revision_round(job_dir)
        recovery = (
            f"python -m aicf reopen --job {job_id} "
            "--confirm-artifacts-fixed"
        )
        if round_number > 2:
            reason = (
                "脚本真实旁白时长连续两轮修订后仍超限；"
                f"actual={error.actual_duration_seconds:.3f}s, "
                f"min={error.min_duration_seconds:.3f}s, "
                f"max={error.max_duration_seconds:.3f}s, "
                f"target={error.target_duration_seconds:.3f}s。"
                "请人工修改 script.json 并确认产物已修复后 reopen。"
            )
            self.repository.fail_stage(
                job_id,
                PipelineStage.AUDIO_GENERATED,
                reason,
                retryable=False,
                recovery_command=recovery,
            )
            return {
                "status": "FAILED_NEEDS_ATTENTION",
                "reason": reason,
                "recovery_command": recovery,
            }
        reviser = getattr(self.content_runner, "revise_for_duration", None)
        if not callable(reviser):
            reason = "内容处理器不支持基于真实时长自动修稿"
            self.repository.fail_stage(
                job_id,
                PipelineStage.AUDIO_GENERATED,
                reason,
                retryable=False,
                recovery_command=recovery,
            )
            return self._failure_result(recovery, RuntimeError(reason))
        revision: dict[str, object] | None = None
        for retry_attempt in range(4):
            try:
                revision = reviser(job_id, error, round_number)
                break
            except (
                URLError,
                TimeoutError,
                OSError,
                UpstreamRateLimitError,
            ):
                if retry_attempt >= 3:
                    raise
                wait = min(60, 10 * (2**retry_attempt))
                print(
                    "[autopilot] 时长修订遇到临时上游错误，"
                    f"{wait}s 后自动重试 (第{retry_attempt + 1}次)",
                    flush=True,
                )
                self.sleep(wait)
            except OpenRouterHTTPError as upstream_error:
                retryable = (
                    upstream_error.status_code == 429
                    or upstream_error.status_code >= 500
                )
                if not retryable or retry_attempt >= 3:
                    raise
                wait = min(60, 10 * (2**retry_attempt))
                print(
                    "[autopilot] 时长修订遇到临时上游错误，"
                    f"{wait}s 后自动重试 (第{retry_attempt + 1}次)",
                    flush=True,
                )
                self.sleep(wait)
        assert revision is not None
        if not bool(revision.get("passed")):
            if round_number < 2:
                return self._handle_duration_revision(job_id, job_dir, error)
            reason = "两轮时长修订均未通过脚本复审，请人工修复 script.json"
            self.repository.fail_stage(
                job_id,
                PipelineStage.AUDIO_GENERATED,
                reason,
                retryable=False,
                recovery_command=recovery,
            )
            return self._failure_result(recovery, RuntimeError(reason))
        self.repository.invalidate_from(
            job_id,
            PipelineStage.CONTENT_PACKAGED,
        )
        return self._run_locked(job_id)

    @staticmethod
    def _next_duration_revision_round(job_dir: Path) -> int:
        rounds: list[int] = []
        for path in job_dir.glob("review_duration_*.json"):
            suffix = path.stem.removeprefix("review_duration_")
            if suffix.isdigit():
                rounds.append(int(suffix))
        return max(rounds, default=0) + 1

    def _invoke(
        self,
        job_id: str,
        stage: PipelineStage,
        job_dir: Path,
        *,
        continuing: bool,
    ) -> dict[str, Any]:
        content_dir = self._content_dir(job_id, job_dir)
        if stage == PipelineStage.AUDIO_GENERATED:
            if self.narration_pipeline is None or self.config is None:
                raise NeedsAttention("缺少 M3 旁白处理器", "")
            script = self._read_json(content_dir / "script.json")
            production = ProductionSettings.load_for_job(job_dir)
            service = getattr(self.narration_pipeline, "service", None)
            select_voice = getattr(service, "select_voice", None)
            if callable(select_voice):
                select_voice(production.narration_voice)
            video = self.config.video
            self.narration_pipeline.batch_synthesize(
                script,
                job_dir / "audio",
                target_duration_seconds=video.target_duration_seconds,
                min_duration_seconds=video.min_duration_seconds,
                max_duration_seconds=video.max_duration_seconds,
            )
            validation = self.voice_validator.validate(
                job_dir / "audio" / "voiceover.wav",
                expected_text=self._narration_text(script),
                key_phrases=self._key_phrases(script),
            )
            (job_dir / "audio" / "voice_validation.json").write_text(
                json.dumps(
                    validation.as_dict(),
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            if validation.available and not validation.passed:
                raise NeedsAttention(
                    "旁白 ASR 验收失败：关键数字、短语或语种不匹配",
                    f"python -m aicf resume --job {job_id}",
                )
            return {"status": "COMPLETED"}
        if stage == PipelineStage.STORYBOARD_GENERATED:
            if self.visual_plan_runner is None:
                raise NeedsAttention("缺少 storyboard/visual plan 处理器", "")
            production = ProductionSettings.load_for_job(job_dir)
            self.visual_plan_runner.run(
                script_path=content_dir / "script.json",
                timeline_path=job_dir / "audio" / "timeline.json",
                output_dir=job_dir,
                orientation=production.orientation,
            )
            return {"status": "COMPLETED"}
        if stage == PipelineStage.KEYFRAMES_GENERATED:
            asset_runner = self.asset_runner
            if self.asset_runner_factory is not None:
                provider = ProductionSettings.load_for_job(job_dir).video_provider
                asset_runner = self.asset_runner_factory(provider)
            if asset_runner is None:
                raise NeedsAttention("缺少 M4 素材处理器", "")
            return asset_runner.run(
                job_dir / "visual_plan.json",
                resume=continuing,
                usage_recorder=self._m4_usage_recorder(job_id),
                budget_guard=self._m4_budget_guard(job_id),
            )
        if stage == PipelineStage.RENDERED:
            if self.renderer is None:
                raise NeedsAttention("缺少 M5 渲染处理器", "")
            script = self._read_json(content_dir / "script.json")
            production = ProductionSettings.load_for_job(job_dir)
            self.renderer.render_and_validate(
                visual_plan_path=job_dir / "visual_plan.json",
                audio_path=job_dir / "audio" / "voiceover.wav",
                subtitle_path=job_dir / "audio" / "subtitles.ass",
                output_path=job_dir / "final" / "master.mp4",
                title=str(script["title"]),
                orientation=production.orientation,
            )
            return {"status": "COMPLETED"}
        if stage == PipelineStage.QA_CHECKED:
            if self.m6_pipeline is None:
                raise NeedsAttention("缺少 M6 QA/package 处理器", "")
            production = ProductionSettings.load_for_job(job_dir)
            return self.m6_pipeline.run(
                **self._load_inputs(job_id, job_dir),
                selected_platforms=production.selected_platforms,
                orientation=production.orientation,
            )
        return {"status": "COMPLETED"}

    def _m4_usage_recorder(self, job_id: str) -> object:
        def record(**usage: object) -> None:
            self.repository.record_m4_submission(
                job_id,
                request_id=str(usage["request_id"]),
                jimeng_images=int(usage.get("jimeng_images", 0)),
                jimeng_video_clips=int(usage.get("jimeng_video_clips", 0)),
                jimeng_video_seconds_requested=int(
                    usage.get("jimeng_video_seconds_requested", 0)
                ),
            )

        return record

    def _m4_budget_guard(self, job_id: str) -> object:
        def guard(**requested: object) -> None:
            if self.config is None:
                raise NeedsAttention("缺少 generation_budget 配置", "")
            request_id = str(requested.pop("request_id", "")).strip()
            budget = self.config.generation_budget
            limits = {
                "jimeng_images": budget.max_jimeng_images,
                "jimeng_video_clips": budget.max_jimeng_video_clips,
                "jimeng_video_seconds_requested": (
                    budget.max_jimeng_video_seconds_requested
                ),
            }
            if request_id:
                try:
                    self.repository.reserve_m4_submission(
                        job_id,
                        request_id=request_id,
                        limits=limits,
                        jimeng_images=int(requested.get("jimeng_images", 0)),
                        jimeng_video_clips=int(
                            requested.get("jimeng_video_clips", 0)
                        ),
                        jimeng_video_seconds_requested=int(
                            requested.get(
                                "jimeng_video_seconds_requested",
                                0,
                            )
                        ),
                    )
                except ValueError as error:
                    raise NeedsAttention(str(error), "") from error
                return
            usage = self.repository.get_job(job_id).usage
            checks = (
                (
                    "jimeng_images",
                    "即梦图片",
                    budget.max_jimeng_images,
                ),
                (
                    "jimeng_video_clips",
                    "即梦视频片段",
                    budget.max_jimeng_video_clips,
                ),
                (
                    "jimeng_video_seconds_requested",
                    "即梦请求视频秒数",
                    budget.max_jimeng_video_seconds_requested,
                ),
            )
            for key, label, limit in checks:
                current = int(usage.get(key, 0))
                delta = int(requested.get(key, 0))
                if current + delta > limit:
                    raise NeedsAttention(
                        f"{label}预算上限 {limit}，当前 {current}，本次请求 {delta}",
                        "",
                    )

        return guard

    def _resume_index(
        self,
        job_id: str,
        stages: list[PipelineStage],
    ) -> int:
        status = self.repository.get_job(job_id)
        if status.failed_stage in stages:
            return stages.index(status.failed_stage)
        current = status.current_stage
        if current in stages:
            index = stages.index(current)
            if current in status.completed_stages:
                self._verify_completed_hashes(status, current)
                return index + 1
            return index
        return 0

    def _verify_completed_hashes(
        self,
        status: object,
        stage: PipelineStage,
    ) -> None:
        job_dir = Path(status.output_dir)
        actual = self._artifact_hashes(self._artifacts(stage, job_dir))
        record = status.stages.get(stage.value, {})
        recorded = record.get("artifact_hashes")
        if recorded is None:
            self.repository.record_artifact_hashes(status.job_id, stage, actual)
        elif recorded != actual:
            raise ValueError(f"{stage.value} 产物哈希已变化，拒绝复用")

    def _complete_marker_stage(
        self,
        job_id: str,
        job_dir: Path,
        stage: PipelineStage,
        paths: list[Path],
    ) -> dict[str, Any] | None:
        status = self.repository.get_job(job_id)
        if stage in status.completed_stages:
            return None
        try:
            self.repository.start_stage(job_id, stage)
            self.repository.complete_stage(
                job_id,
                stage,
                artifact_hashes=self._artifact_hashes(paths),
            )
            return None
        except Exception as error:
            return self._fail_existing_stage(job_id, stage, str(error))

    def _fail_existing_stage(
        self,
        job_id: str,
        stage: PipelineStage,
        reason: str,
    ) -> dict[str, Any]:
        status = self.repository.get_job(job_id)
        if status.current_stage != stage:
            try:
                self.repository.start_stage(job_id, stage)
            except Exception:
                return self._failure_result(
                    f"python -m aicf resume --job {job_id}",
                    RuntimeError(reason),
                )
        recovery = f"python -m aicf resume --job {job_id}"
        self.repository.fail_stage(
            job_id,
            stage,
            reason,
            retryable=False,
            recovery_command=recovery,
        )
        return self._failure_result(recovery, RuntimeError(reason))

    def _missing_handler(
        self,
        job_id: str,
        stage: PipelineStage,
    ) -> dict[str, Any]:
        return self._fail_existing_stage(
            job_id,
            stage,
            f"缺少 {stage.value} 真实处理器",
        )

    @staticmethod
    def _failure_result(recovery: str, error: Exception) -> dict[str, Any]:
        return {
            "status": "FAILED_NEEDS_ATTENTION",
            "reason": sanitize_error(error),
            "recovery_command": recovery,
        }

    @staticmethod
    def _error_details(error: Exception) -> str:
        if isinstance(error, subprocess.CalledProcessError):
            return sanitize_error(error.stderr or error.stdout or error).strip()
        return sanitize_error(error)

    def _artifacts(self, stage: PipelineStage, job_dir: Path) -> list[Path]:
        mapping = {
            PipelineStage.AUDIO_GENERATED: [job_dir / "audio" / "voiceover.wav"],
            PipelineStage.NARRATION_TIMELINE_CREATED: [
                job_dir / "audio" / "timeline.json"
            ],
            PipelineStage.STORYBOARD_GENERATED: [job_dir / "visual_plan.json"],
            PipelineStage.CLIP_PLAN_CREATED: [job_dir / "visual_plan.json"],
            PipelineStage.KEYFRAMES_GENERATED: [
                job_dir / "assets" / "tasks.json"
            ],
            PipelineStage.VIDEO_CLIPS_GENERATED: [
                job_dir / "asset_manifest.json"
            ],
            PipelineStage.SUBTITLES_GENERATED: [
                job_dir / "audio" / "subtitles.srt",
                job_dir / "audio" / "subtitles.ass",
            ],
            PipelineStage.MASTER_TIMELINE_ASSEMBLED: [
                job_dir / "audio" / "timeline.json",
                job_dir / "visual_plan.json",
            ],
            PipelineStage.RENDERED: [
                job_dir / "final" / "master.mp4",
                job_dir / "final" / "clean.mp4",
            ],
            PipelineStage.QA_CHECKED: [
                job_dir / "delivery" / "publish_manifest.json"
            ],
        }
        return mapping[stage]

    @staticmethod
    def _artifact_hashes(paths: list[Path]) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(f"阶段产物不存在: {path}")
            hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        return hashes

    def _completed_manifest(self, job_id: str, job_dir: Path) -> dict[str, Any]:
        delivery_dir = job_dir / "delivery"
        status = self.repository.get_job(job_id)
        issues = (
            list(self.m6_pipeline.verify_delivery(delivery_dir))
            if self.m6_pipeline is not None
            else ["缺少 M6 交付验证器"]
        )
        manifest_path = delivery_dir / "publish_manifest.json"
        recorded_hashes = status.stages.get(
            PipelineStage.COMPLETED.value,
            {},
        ).get("artifact_hashes")
        expected_hash = None
        if isinstance(recorded_hashes, dict):
            expected_hash = next(
                (
                    str(value)
                    for path, value in recorded_hashes.items()
                    if Path(str(path)).name == "publish_manifest.json"
                ),
                None,
            )
        actual_hash = (
            hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            if manifest_path.is_file()
            else None
        )
        if expected_hash is None or actual_hash != expected_hash:
            issues.append("数据库记录的 manifest hash 不匹配")
        if issues:
            reason = "COMPLETED 交付重新验证失败: " + "；".join(issues)
            self.repository.invalidate_completed_delivery(job_id, reason)
            return self._failure_result(
                f"python -m aicf autopilot --job {job_id}",
                RuntimeError(reason),
            )
        return self._load_delivery_manifest(job_dir)

    def _load_delivery_manifest(self, job_dir: Path) -> dict[str, Any]:
        return self._read_json(job_dir / "delivery" / "publish_manifest.json")

    def _finalize_user_delivery(
        self,
        job_id: str,
        job_dir: Path,
        manifest: dict[str, Any],
    ) -> None:
        if (
            self.user_output_root is None
            or manifest.get("status") != "READY_TO_PUBLISH"
        ):
            return
        finalize_user_delivery(job_dir, self.user_output_root / job_id)

    def _load_inputs(self, job_id: str, job_dir: Path) -> dict[str, object]:
        content_dir = self._content_dir(job_id, job_dir)
        final_dir = job_dir / "final"
        master = self._first_existing(
            final_dir / "master.mp4",
            final_dir / "final.mp4",
            final_dir / "integration_sample.mp4",
        )
        clean = self._first_existing(
            final_dir / "clean.mp4",
            final_dir / "master_clean.mp4",
        )
        subtitles = self._first_existing(
            job_dir / "audio" / "subtitles.ass",
            job_dir / "subtitles.ass",
        )
        timeline = self._first_existing(
            job_dir / "audio" / "timeline.json",
            job_dir / "timeline.json",
        )
        missing: list[str] = []
        if master is None:
            missing.append("带字幕成片 final/master.mp4")
        if clean is None:
            missing.append("clean 成片 final/clean.mp4")
        if subtitles is None:
            missing.append("字幕 audio/subtitles.ass")
        if timeline is None:
            missing.append("时间线 audio/timeline.json")
        for name in ("script.json", "package.json"):
            if not (content_dir / name).is_file():
                missing.append(name)
        if missing:
            raise NeedsAttention(
                "缺少 M6 输入产物: " + "、".join(missing),
                f"python -m aicf resume --job {job_id}",
            )
        duration = self._expected_duration(job_dir, content_dir)
        production = ProductionSettings.load_for_job(job_dir)
        return {
            "master_video": master,
            "clean_video": clean,
            "subtitle_path": subtitles,
            "timeline_path": timeline,
            "script": self._read_json(content_dir / "script.json"),
            "package": self._read_json(content_dir / "package.json"),
            "output_dir": job_dir / "delivery",
            "expected_duration_seconds": duration,
            "repair_context": {
                "visual_plan_path": job_dir / "visual_plan.json",
                "audio_path": job_dir / "audio" / "voiceover.wav",
                "title": str(
                    self._read_json(content_dir / "script.json").get("title", "")
                ),
                "orientation": production.orientation,
            },
        }

    @staticmethod
    def _narration_text(script: dict[str, object]) -> str:
        segments = script.get("segments", [])
        if not isinstance(segments, list):
            return ""
        return "".join(
            str(segment.get("narration", ""))
            for segment in segments
            if isinstance(segment, dict)
        )

    @staticmethod
    def _key_phrases(script: dict[str, object]) -> tuple[str, ...]:
        value = script.get("key_phrases", [])
        if isinstance(value, list) and value:
            return tuple(str(item) for item in value if str(item).strip())
        narration = Autopilot._narration_text(script)
        candidates = (script.get("hook"), script.get("call_to_action"))
        return tuple(
            text
            for candidate in candidates
            if (text := str(candidate or "").strip()) and text in narration
        )

    @staticmethod
    def _first_existing(*paths: Path) -> Path | None:
        return next((path for path in paths if path.is_file()), None)

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError(f"{path.name} 顶层必须是对象")
        return value

    def _expected_duration(self, job_dir: Path, content_dir: Path) -> float:
        timeline_path = job_dir / "audio" / "timeline.json"
        if timeline_path.is_file():
            value = json.loads(timeline_path.read_text(encoding="utf-8-sig"))
            entries = value.get("segments", []) if isinstance(value, dict) else value
            if isinstance(entries, list) and entries:
                last = entries[-1]
                if isinstance(last, dict):
                    duration = last.get("end_seconds", last.get("end"))
                    if duration is not None and float(duration) > 0:
                        return float(duration)
        for render_path in (job_dir / "final").glob("*.render.json"):
            value = self._read_json(render_path)
            render = value.get("render")
            if isinstance(render, dict) and float(render.get("duration_seconds", 0)) > 0:
                return float(render["duration_seconds"])
        raise NeedsAttention(
            "无法确定成片期望时长",
            f"python -m aicf batch-synthesize --script "
            f'"{content_dir / "script.json"}" --output-dir "{job_dir / "audio"}"',
        )

    def _content_dir(self, job_id: str, job_dir: Path) -> Path:
        if self.content_output_root is None:
            return job_dir
        return self.content_output_root / job_id
