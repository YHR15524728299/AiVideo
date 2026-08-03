from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from pydantic import BaseModel, Field

from .atomic_io import atomic_replace
from .file_lock import os_file_lock
from .logging_utils import sanitize_error
from .state_machine import PipelineStage, StateMachine, TransitionError


_INVALID_JOB_ID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStatus(BaseModel):
    job_id: str
    version: int = Field(default=0, ge=0)
    snapshot_dirty: bool = False
    topic_id: str = ""
    current_stage: PipelineStage | None = None
    completed_stages: list[PipelineStage] = Field(default_factory=list)
    failed_stage: PipelineStage | None = None
    retry_count: dict[str, int] = Field(default_factory=dict)
    started_at: str
    updated_at: str
    output_dir: str
    stages: dict[str, dict] = Field(default_factory=dict)
    usage: dict[str, int] = Field(
        default_factory=lambda: {
            "llm_calls": 0,
            "llm_input_tokens": 0,
            "llm_output_tokens": 0,
            "jimeng_images": 0,
            "jimeng_video_clips": 0,
            "jimeng_video_seconds_requested": 0,
        }
    )

    @property
    def next_resume_command(self) -> str:
        if self.failed_stage:
            record = self.stages.get(self.failed_stage.value, {})
            configured = record.get("next_resume_command")
            if isinstance(configured, str) and configured.strip():
                return configured
        if self.current_stage:
            record = self.stages.get(self.current_stage.value, {})
            configured = record.get("next_resume_command")
            if isinstance(configured, str) and configured.strip():
                return configured
        return f"python -m aicf resume --job {self.job_id}"


class JobRepository:
    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        migrated: list[JobStatus] = []
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    output_dir TEXT NOT NULL,
                    status_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS m4_usage_events (
                    request_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    intent_status TEXT NOT NULL DEFAULT 'confirmed',
                    recorded_at TEXT NOT NULL,
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                )
                """
            )
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(m4_usage_events)"
                ).fetchall()
            }
            if "intent_status" not in columns:
                connection.execute(
                    "ALTER TABLE m4_usage_events "
                    "ADD COLUMN intent_status TEXT NOT NULL DEFAULT 'confirmed'"
                )
            rows = connection.execute("SELECT status_json FROM jobs").fetchall()
            for row in rows:
                status = JobStatus.model_validate_json(row["status_json"])
                if self._migrate_legacy_m2_status(status):
                    status.version += 1
                    self._save_in_transaction(connection, status)
                    migrated.append(status)
        for status in migrated:
            self._sync_snapshot(status)

    def create_job(self, job_id: str, output_dir: str | Path) -> JobStatus:
        from aicf.production_settings import ProductionSettings

        self._validate_job_id(job_id)
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        ProductionSettings().freeze_for_job(destination)
        now = utc_now()
        status = JobStatus(
            job_id=job_id,
            version=1,
            started_at=now,
            updated_at=now,
            output_dir=str(destination),
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO jobs(job_id, output_dir, status_json, updated_at) VALUES (?, ?, ?, ?)",
                (job_id, str(destination), status.model_dump_json(), now),
            )
        return self._sync_snapshot(status)

    def get_job(self, job_id: str) -> JobStatus:
        self._validate_job_id(job_id)
        with self._connect() as connection:
            return self._get_job_in_transaction(connection, job_id)

    def list_jobs(self) -> list[JobStatus]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status_json FROM jobs ORDER BY updated_at DESC"
            ).fetchall()
        return [JobStatus.model_validate_json(row["status_json"]) for row in rows]

    def relocate_output_dir(
        self,
        job_id: str,
        output_dir: str | Path,
    ) -> JobStatus:
        destination = Path(output_dir).resolve()
        if not destination.is_dir():
            raise FileNotFoundError(f"新任务工作目录不存在: {destination}")

        def mutate(status: JobStatus) -> None:
            status.output_dir = str(destination)

        return self._mutate(job_id, mutate)

    def start_stage(self, job_id: str, stage: PipelineStage) -> JobStatus:
        def mutate(status: JobStatus) -> None:
            self._assert_not_completed(status)
            if stage in {
                PipelineStage.FAILED_RETRYABLE,
                PipelineStage.FAILED_NEEDS_ATTENTION,
            }:
                raise TransitionError(f"不能直接启动状态阶段: {stage.value}")
            if (
                status.current_stage is None
                and stage != PipelineStage.DIRECTION_LOADED
            ):
                raise TransitionError("新 Job 首次只能启动 DIRECTION_LOADED")
            if status.current_stage in {
                PipelineStage.FAILED_RETRYABLE,
                PipelineStage.FAILED_NEEDS_ATTENTION,
            }:
                if status.failed_stage != stage:
                    failed = status.failed_stage.value if status.failed_stage else "未知"
                    raise TransitionError(f"只能重试失败阶段 {failed}，不能启动 {stage.value}")
                failed_record = status.stages.get(stage.value, {})
                if not failed_record.get("recoverable", False):
                    raise TransitionError(f"失败阶段 {stage.value} 不可重试")
            elif status.current_stage is not None:
                if status.current_stage not in status.completed_stages:
                    raise TransitionError(f"阶段 {status.current_stage.value} 尚未完成")
                StateMachine().validate_transition(status.current_stage, stage)

            record = status.stages.setdefault(stage.value, {})
            record.update(
                {
                    "started_at": utc_now(),
                    "completed_at": None,
                    "call_count": int(record.get("call_count", 0)) + 1,
                    "retry_count": int(record.get("retry_count", 0)),
                    "error": None,
                    "recoverable": True,
                    "log_path": f"logs/{stage.value.lower()}.log",
                }
            )
            status.current_stage = stage
            status.failed_stage = None

        return self._mutate(job_id, mutate)

    def complete_stage(
        self,
        job_id: str,
        stage: PipelineStage,
        *,
        artifact_hashes: dict[str, str] | None = None,
    ) -> JobStatus:
        def mutate(status: JobStatus) -> None:
            self._assert_not_completed(status)
            if status.current_stage != stage or status.failed_stage is not None:
                raise TransitionError(f"只能完成当前运行阶段: {stage.value}")
            record = status.stages.get(stage.value)
            if not record or not record.get("started_at"):
                raise TransitionError(f"阶段尚未启动: {stage.value}")
            record["completed_at"] = utc_now()
            record["error"] = None
            record["waiting_external"] = False
            if artifact_hashes is not None:
                record["artifact_hashes"] = dict(artifact_hashes)
            if stage not in status.completed_stages:
                status.completed_stages.append(stage)
            status.current_stage = stage
            status.failed_stage = None

        return self._mutate(job_id, mutate)

    def record_artifact_hashes(
        self,
        job_id: str,
        stage: PipelineStage,
        artifact_hashes: dict[str, str],
    ) -> JobStatus:
        def mutate(status: JobStatus) -> None:
            if stage not in status.completed_stages:
                raise TransitionError(f"阶段尚未完成: {stage.value}")
            status.stages.setdefault(stage.value, {})["artifact_hashes"] = dict(
                artifact_hashes
            )

        return self._mutate(job_id, mutate)

    def wait_stage(
        self,
        job_id: str,
        stage: PipelineStage,
        recovery_command: str,
    ) -> JobStatus:
        def mutate(status: JobStatus) -> None:
            if status.current_stage != stage or status.failed_stage is not None:
                raise TransitionError(f"只能等待当前运行阶段: {stage.value}")
            record = status.stages.get(stage.value)
            if not record or not record.get("started_at"):
                raise TransitionError(f"阶段尚未启动: {stage.value}")
            record.update(
                {
                    "waiting_external": True,
                    "recoverable": True,
                    "next_resume_command": recovery_command,
                }
            )

        return self._mutate(job_id, mutate)

    def update_m2_metadata(
        self,
        job_id: str,
        *,
        topic_id: str | None = None,
        llm_calls: int | None = None,
        llm_input_tokens: int | None = None,
        llm_output_tokens: int | None = None,
    ) -> JobStatus:
        def mutate(status: JobStatus) -> None:
            if topic_id is not None:
                status.topic_id = topic_id
            updates = {
                "llm_calls": llm_calls,
                "llm_input_tokens": llm_input_tokens,
                "llm_output_tokens": llm_output_tokens,
            }
            for key, value in updates.items():
                if value is not None:
                    status.usage[key] = int(value)

        return self._mutate(job_id, mutate)

    def increment_m2_usage(
        self,
        job_id: str,
        *,
        llm_calls: int = 0,
        llm_input_tokens: int = 0,
        llm_output_tokens: int = 0,
    ) -> JobStatus:
        deltas = {
            "llm_calls": int(llm_calls),
            "llm_input_tokens": int(llm_input_tokens),
            "llm_output_tokens": int(llm_output_tokens),
        }
        if any(value < 0 for value in deltas.values()):
            raise ValueError("usage 增量不能为负数")

        def mutate(status: JobStatus) -> None:
            for key, delta in deltas.items():
                status.usage[key] = int(status.usage.get(key, 0)) + delta

        return self._mutate(job_id, mutate)

    def record_m4_submission(
        self,
        job_id: str,
        *,
        request_id: str,
        jimeng_images: int = 0,
        jimeng_video_clips: int = 0,
        jimeng_video_seconds_requested: int = 0,
    ) -> JobStatus:
        self._validate_job_id(job_id)
        if not request_id.strip():
            raise ValueError("request_id 不能为空")
        deltas = {
            "jimeng_images": int(jimeng_images),
            "jimeng_video_clips": int(jimeng_video_clips),
            "jimeng_video_seconds_requested": int(
                jimeng_video_seconds_requested
            ),
        }
        if any(value < 0 for value in deltas.values()):
            raise ValueError("usage 增量不能为负数")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            status = self._get_job_in_transaction(connection, job_id)
            existing = connection.execute(
                "SELECT job_id FROM m4_usage_events WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if existing["job_id"] != job_id:
                    raise ValueError("request_id 已属于其他 Job")
                connection.execute(
                    "UPDATE m4_usage_events SET intent_status = 'confirmed' "
                    "WHERE request_id = ?",
                    (request_id,),
                )
                return status
            for key, delta in deltas.items():
                status.usage[key] = int(status.usage.get(key, 0)) + delta
            status.version += 1
            status.snapshot_dirty = False
            self._save_in_transaction(connection, status)
            connection.execute(
                """
                INSERT INTO m4_usage_events(
                    request_id, job_id, intent_status, recorded_at
                )
                VALUES (?, ?, 'confirmed', ?)
                """,
                (request_id, job_id, utc_now()),
            )
        return self._sync_snapshot(status)

    def reserve_m4_submission(
        self,
        job_id: str,
        *,
        request_id: str,
        limits: dict[str, int],
        jimeng_images: int = 0,
        jimeng_video_clips: int = 0,
        jimeng_video_seconds_requested: int = 0,
    ) -> JobStatus:
        self._validate_job_id(job_id)
        if not request_id.strip():
            raise ValueError("request_id 不能为空")
        deltas = {
            "jimeng_images": int(jimeng_images),
            "jimeng_video_clips": int(jimeng_video_clips),
            "jimeng_video_seconds_requested": int(
                jimeng_video_seconds_requested
            ),
        }
        if any(value < 0 for value in deltas.values()):
            raise ValueError("usage 增量不能为负数")
        labels = {
            "jimeng_images": "即梦图片",
            "jimeng_video_clips": "即梦视频片段",
            "jimeng_video_seconds_requested": "即梦请求视频秒数",
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            status = self._get_job_in_transaction(connection, job_id)
            existing = connection.execute(
                "SELECT job_id FROM m4_usage_events WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if existing is not None:
                if existing["job_id"] != job_id:
                    raise ValueError("request_id 已属于其他 Job")
                return status
            for key, delta in deltas.items():
                current = int(status.usage.get(key, 0))
                limit = int(limits[key])
                if current + delta > limit:
                    raise ValueError(
                        f"{labels[key]}预算上限 {limit}，"
                        f"当前 {current}，本次请求 {delta}"
                    )
            for key, delta in deltas.items():
                status.usage[key] = int(status.usage.get(key, 0)) + delta
            status.version += 1
            status.snapshot_dirty = False
            self._save_in_transaction(connection, status)
            connection.execute(
                """
                INSERT INTO m4_usage_events(
                    request_id, job_id, intent_status, recorded_at
                )
                VALUES (?, ?, 'reserved', ?)
                """,
                (request_id, job_id, utc_now()),
            )
        return self._sync_snapshot(status)

    def fail_stage(
        self,
        job_id: str,
        stage: PipelineStage,
        reason: str,
        *,
        retryable: bool,
        recovery_command: str | None = None,
    ) -> JobStatus:
        safe_reason = sanitize_error(reason)

        def mutate(status: JobStatus) -> None:
            self._assert_not_completed(status)
            if status.current_stage != stage or status.failed_stage is not None:
                raise TransitionError(f"只能标记当前运行阶段失败: {stage.value}")
            record = status.stages.get(stage.value)
            if not record or not record.get("started_at"):
                raise TransitionError(f"阶段尚未启动: {stage.value}")
            count = status.retry_count.get(stage.value, 0) + 1
            status.retry_count[stage.value] = count
            record.update(
                {
                    "error": safe_reason,
                    "retry_count": count,
                    "recoverable": retryable,
                    "next_resume_command": recovery_command or status.next_resume_command,
                }
            )
            status.failed_stage = stage
            status.current_stage = (
                PipelineStage.FAILED_RETRYABLE
                if retryable
                else PipelineStage.FAILED_NEEDS_ATTENTION
            )

        return self._mutate(job_id, mutate)

    def invalidate_completed_delivery(self, job_id: str, reason: str) -> JobStatus:
        safe_reason = sanitize_error(reason)

        def mutate(status: JobStatus) -> None:
            if status.current_stage != PipelineStage.COMPLETED:
                raise TransitionError("只能失效已完成 Job 的交付")
            count = status.retry_count.get(PipelineStage.COMPLETED.value, 0) + 1
            invalidated = {
                PipelineStage.QA_CHECKED,
                PipelineStage.AUTO_REPAIRED,
                PipelineStage.PACKAGED,
                PipelineStage.COMPLETED,
            }
            status.completed_stages = [
                stage
                for stage in status.completed_stages
                if stage not in invalidated
            ]
            for stage in invalidated:
                status.stages.pop(stage.value, None)
                status.retry_count.pop(stage.value, None)
            record = status.stages.setdefault(PipelineStage.COMPLETED.value, {})
            status.retry_count[PipelineStage.COMPLETED.value] = count
            record.update(
                {
                    "error": safe_reason,
                    "retry_count": count,
                    "recoverable": False,
                    "next_resume_command": (
                        f"python -m aicf reopen --job {status.job_id} "
                        "--confirm-artifacts-fixed"
                    ),
                }
            )
            status.failed_stage = PipelineStage.COMPLETED
            status.current_stage = PipelineStage.FAILED_NEEDS_ATTENTION

        return self._mutate(job_id, mutate)

    def invalidate_from(
        self,
        job_id: str,
        stage: PipelineStage,
    ) -> JobStatus:
        def mutate(status: JobStatus) -> None:
            ordered = [
                item
                for item in PipelineStage
                if item
                not in {
                    PipelineStage.FAILED_RETRYABLE,
                    PipelineStage.FAILED_NEEDS_ATTENTION,
                }
            ]
            start = ordered.index(stage)
            invalidated = set(ordered[start:])
            status.completed_stages = [
                item
                for item in status.completed_stages
                if item not in invalidated
            ]
            for item in invalidated:
                status.stages.pop(item.value, None)
                status.retry_count.pop(item.value, None)
            status.failed_stage = None
            status.current_stage = (
                status.completed_stages[-1]
                if status.completed_stages
                else None
            )

        return self._mutate(job_id, mutate)

    def reopen_failed_attention(
        self,
        job_id: str,
        *,
        artifacts_fixed: bool = False,
        recoverable_reason: str | None = None,
    ) -> JobStatus:
        allowed_reasons = {
            "credentials_restored",
            "external_service_restored",
            "dependency_restored",
        }

        def mutate(status: JobStatus) -> None:
            if (
                status.current_stage != PipelineStage.FAILED_NEEDS_ATTENTION
                or status.failed_stage is None
            ):
                raise TransitionError("只有 FAILED_NEEDS_ATTENTION Job 可以 reopen")
            if not artifacts_fixed and recoverable_reason not in allowed_reasons:
                raise TransitionError(
                    "reopen 需要用户确认产物已修复，或提供允许的可恢复原因"
                )
            failed = status.failed_stage
            ordered = [
                item
                for item in PipelineStage
                if item
                not in {
                    PipelineStage.FAILED_RETRYABLE,
                    PipelineStage.FAILED_NEEDS_ATTENTION,
                }
            ]
            invalidated = set(ordered[ordered.index(failed) :])
            status.completed_stages = [
                item
                for item in status.completed_stages
                if item not in invalidated
            ]
            for item in invalidated:
                status.stages.pop(item.value, None)
                status.retry_count.pop(item.value, None)
            status.current_stage = (
                status.completed_stages[-1]
                if status.completed_stages
                else None
            )
            status.failed_stage = None

        return self._mutate(job_id, mutate)

    def _mutate(
        self,
        job_id: str,
        mutation: Callable[[JobStatus], None],
    ) -> JobStatus:
        self._validate_job_id(job_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            status = self._get_job_in_transaction(connection, job_id)
            mutation(status)
            status.version += 1
            status.snapshot_dirty = False
            self._save_in_transaction(connection, status)
        return self._sync_snapshot(status)

    def rebuild_snapshot(self, job_id: str) -> JobStatus:
        self._validate_job_id(job_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            status = self._get_job_in_transaction(connection, job_id)
            status.snapshot_dirty = False
            self._save_in_transaction(connection, status)
        return self._sync_snapshot(status, force=True)

    def _sync_snapshot(
        self,
        status: JobStatus,
        *,
        force: bool = False,
    ) -> JobStatus:
        try:
            if force:
                self._write_status(status, force=True)
            else:
                self._write_status(status)
        except OSError:
            return self._mark_snapshot_dirty(status.job_id, status.version)
        return status

    def _mark_snapshot_dirty(self, job_id: str, version: int) -> JobStatus:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            status = self._get_job_in_transaction(connection, job_id)
            if status.version == version:
                status.snapshot_dirty = True
                self._save_in_transaction(connection, status)
        return status

    @staticmethod
    def _get_job_in_transaction(
        connection: sqlite3.Connection,
        job_id: str,
    ) -> JobStatus:
        row = connection.execute(
            "SELECT status_json FROM jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Job 不存在: {job_id}")
        return JobStatus.model_validate_json(row["status_json"])

    @staticmethod
    def _save_in_transaction(
        connection: sqlite3.Connection,
        status: JobStatus,
    ) -> None:
        status.updated_at = utc_now()
        payload = status.model_dump_json()
        connection.execute(
            "UPDATE jobs SET output_dir = ?, status_json = ?, updated_at = ? WHERE job_id = ?",
            (status.output_dir, payload, status.updated_at, status.job_id),
        )

    @staticmethod
    def _assert_not_completed(status: JobStatus) -> None:
        if PipelineStage.COMPLETED in status.completed_stages:
            raise TransitionError("COMPLETED 是终态，不能再修改 Job 状态")

    @staticmethod
    def _validate_job_id(job_id: str) -> None:
        if (
            not job_id
            or job_id != job_id.strip()
            or job_id in {".", ".."}
            or _INVALID_JOB_ID.search(job_id)
        ):
            raise ValueError(f"非法 Job ID: {job_id!r}")

    @staticmethod
    def _migrate_legacy_m2_status(status: JobStatus) -> bool:
        downstream = {
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
            PipelineStage.AUTO_REPAIRED,
            PipelineStage.COMPLETED,
        }
        completed = set(status.completed_stages)
        is_legacy_m2 = (
            status.current_stage == PipelineStage.PACKAGED
            and PipelineStage.PACKAGED in completed
            and PipelineStage.SCRIPT_REVIEWED in completed
            and not (completed & downstream)
        )
        if not is_legacy_m2:
            return False
        status.completed_stages = [
            PipelineStage.CONTENT_PACKAGED
            if stage == PipelineStage.PACKAGED
            else stage
            for stage in status.completed_stages
        ]
        status.current_stage = PipelineStage.CONTENT_PACKAGED
        packaged = status.stages.pop(PipelineStage.PACKAGED.value, {})
        status.stages[PipelineStage.CONTENT_PACKAGED.value] = packaged
        return True

    @staticmethod
    def _write_status(status: JobStatus, *, force: bool = False) -> bool:
        path = Path(status.output_dir) / "status.json"
        with JobRepository._snapshot_lock(path):
            return JobRepository._write_status_locked(path, status, force=force)

    @staticmethod
    def _write_status_locked(
        path: Path,
        status: JobStatus,
        *,
        force: bool,
    ) -> bool:
        if not force and path.exists():
            try:
                current = json.loads(path.read_text(encoding="utf-8"))
                current_version = int(current.get("version", 0))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                current_version = -1
            if current_version > status.version:
                return False
        temporary = path.with_name(
            f".{path.name}.v{status.version}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(
                    status.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            atomic_replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise
        return True

    @staticmethod
    @contextmanager
    def _snapshot_lock(path: Path, timeout: float = 10.0) -> Iterator[None]:
        lock_path = path.with_name(f".{path.name}.lock")
        with os_file_lock(
            lock_path,
            timeout=timeout,
            timeout_message=f"status 快照文件锁超时: {lock_path}",
        ):
            yield
