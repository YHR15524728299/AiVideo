"""后台任务状态聚合与GUI不可变内存视图。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from .background_worker import WorkerRecord, worker_record_path
from .database import normalized_snapshot_semantics
from .file_lock import lock_is_active
from .job_actions import (
    JobActionState,
    constrain_actions_for_running_job,
    derive_job_actions,
    summarize_research_failure,
)
from .job_service import JobService
from .logging_utils import log_state_exception
from .process_identity import (
    ProcessProbe,
    ProcessProbeStatus,
    probe_process_identity,
    process_identity_matches,
)


class HealthStatus(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class HealthIssue:
    source: str
    message: str
    job_id: str = ""


@dataclass(frozen=True)
class JobListItem:
    job_id: str
    direction: str
    status: str
    stage: str
    updated: str


@dataclass(frozen=True)
class JobView:
    job_id: str
    health: HealthStatus
    job_dir: str = ""
    current_stage: str = ""
    failed_stage: str = ""
    completed_stages: tuple[str, ...] = ()
    snapshot_version: int | None = None
    running: bool = False
    worker_status: str = "NOT_STARTED"
    lock_active: bool = False
    has_final_video: bool = False
    final_video_path: str = ""
    actions: JobActionState = field(
        default_factory=lambda: _closed_actions("任务状态未知，已安全禁用操作。")
    )
    row: JobListItem | None = None

    def stage_payload(self) -> dict[str, object]:
        return {
            "current_stage": self.current_stage,
            "failed_stage": self.failed_stage or None,
            "completed_stages": list(self.completed_stages),
        }


@dataclass(frozen=True)
class JobViewModel:
    generation: int
    selected_job_id: str
    health: HealthStatus
    actions: JobActionState
    jobs: tuple[JobView, ...] = ()
    issues: tuple[HealthIssue, ...] = ()
    running_job_id: str = ""

    def selected_job(self) -> JobView | None:
        return next(
            (job for job in self.jobs if job.job_id == self.selected_job_id),
            None,
        )


def newer_view_model(
    current: JobViewModel | None,
    candidate: JobViewModel,
) -> JobViewModel:
    if current is not None and candidate.generation <= current.generation:
        return current
    return candidate


def fail_closed_with_issue(
    model: JobViewModel,
    issue: HealthIssue,
    *,
    health: HealthStatus = HealthStatus.DEGRADED,
) -> JobViewModel:
    """把后台增量读取故障并入快照，并关闭本轮全部危险动作。"""
    guidance = "任务状态不完整或不可确认，已安全禁用操作，请稍后刷新。"
    jobs = tuple(
        replace(
            job,
            health=_merge_health(job.health, health),
            actions=_closed_actions(guidance),
        )
        if not issue.job_id or job.job_id == issue.job_id
        else job
        for job in model.jobs
    )
    return replace(
        model,
        health=_merge_health(model.health, health),
        actions=_closed_actions(guidance),
        jobs=jobs,
        issues=(*model.issues, issue),
    )


class Repository(Protocol):
    def list_jobs(self) -> list[Any]: ...

    def get_job(self, job_id: str) -> Any: ...


def _closed_actions(guidance: str) -> JobActionState:
    return JobActionState(
        can_start=False,
        can_resume=False,
        can_stop=False,
        can_open_video=False,
        guidance=guidance,
    )


def _merge_health(*values: HealthStatus) -> HealthStatus:
    if HealthStatus.UNKNOWN in values:
        return HealthStatus.UNKNOWN
    if HealthStatus.DEGRADED in values:
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY


def _strict_worker_reader(job_dir: Path) -> WorkerRecord | None:
    path = worker_record_path(job_dir)
    if not path.is_file():
        return None
    return WorkerRecord.model_validate_json(path.read_text(encoding="utf-8-sig"))


class JobViewModelBuilder:
    """在后台线程中收集全部慢IO，输出一次性不可变快照。"""

    def __init__(
        self,
        *,
        repository: Repository,
        project_root: str | Path,
        read_text: Callable[[Path], str] = lambda path: path.read_text(
            encoding="utf-8-sig"
        ),
        path_is_file: Callable[[Path], bool] = Path.is_file,
        path_stat: Callable[[Path], Any] = Path.stat,
        worker_reader: Callable[[Path], WorkerRecord | None] = _strict_worker_reader,
        lock_probe: Callable[[Path], bool] = lambda path: lock_is_active(
            path, stale_after=120.0
        ),
        process_probe: Callable[[int], ProcessProbe] = probe_process_identity,
        final_video_probe: Callable[[Path, Path], Path | None] | None = None,
        resume_planner: Callable[[str], Any] | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._repository = repository
        self._project_root = Path(project_root)
        self._read_text = read_text
        self._path_is_file = path_is_file
        self._path_stat = path_stat
        self._worker_reader = worker_reader
        self._lock_probe = lock_probe
        self._process_probe = process_probe
        self._final_video_probe = final_video_probe or self._find_final_video
        self._resume_planner = resume_planner or JobService(repository).plan_resume
        self._logger = logger or logging.getLogger(__name__)

    def collect(
        self,
        generation: int,
        *,
        selected_job_id: str,
        app_running: bool = False,
    ) -> JobViewModel:
        issues: list[HealthIssue] = []
        try:
            statuses = list(self._repository.list_jobs())
        except Exception as error:
            self._record(issues, "repository", error)
            return JobViewModel(
                generation=generation,
                selected_job_id=selected_job_id,
                health=HealthStatus.UNKNOWN,
                actions=_closed_actions(
                    "任务状态暂时无法读取，已安全禁用开始、恢复和停止操作。"
                ),
                issues=tuple(issues),
            )

        jobs = tuple(self._collect_job(item, issues) for item in statuses)
        health = _merge_health(*(job.health for job in jobs))
        running_job_id = next((job.job_id for job in jobs if job.running), "")
        selected = next(
            (job for job in jobs if job.job_id == selected_job_id),
            None,
        )
        if selected_job_id:
            actions = (
                selected.actions
                if selected is not None
                else _closed_actions("所选任务状态尚未加载，已安全禁用操作。")
            )
            actions = constrain_actions_for_running_job(
                actions,
                selected_job_id=selected_job_id,
                running_job_id=running_job_id,
            )
            selected_health = (
                selected.health if selected is not None else HealthStatus.UNKNOWN
            )
        else:
            actions = derive_job_actions(
                existing_job=False,
                app_has_running_job=bool(running_job_id) or app_running,
            )
            selected_health = health
        # 全局健康度用于展示所有任务的聚合状况；动作只由当前选择的健康度
        # fail-closed。未选中的历史任务降级不得关闭健康选中任务。
        if selected_health is not HealthStatus.HEALTHY:
            actions = _closed_actions(
                "任务状态不完整或不可确认，已安全禁用操作，请稍后刷新。"
            )
        return JobViewModel(
            generation=generation,
            selected_job_id=selected_job_id,
            health=health,
            actions=actions,
            jobs=jobs,
            issues=tuple(issues),
            running_job_id=running_job_id,
        )

    def _collect_job(self, status: Any, issues: list[HealthIssue]) -> JobView:
        job_id = str(status.job_id)
        job_dir = Path(status.output_dir)
        output_dir = self._project_root / "outputs" / job_id
        health = HealthStatus.HEALTHY
        snapshot: dict[str, Any] = {}
        snapshot_version: int | None = None

        snapshot_path = job_dir / "status.json"
        try:
            if not self._path_is_file(snapshot_path):
                raise FileNotFoundError("status.json不存在")
            loaded = json.loads(self._read_text(snapshot_path))
            if not isinstance(loaded, dict):
                raise ValueError("status.json根节点不是对象")
            snapshot = loaded
            snapshot_version = int(loaded.get("version", 0))
            if (
                normalized_snapshot_semantics(loaded)
                != normalized_snapshot_semantics(status)
            ):
                raise ValueError("SQLite与状态快照语义不一致")
        except Exception as error:
            health = _merge_health(health, HealthStatus.DEGRADED)
            self._record(issues, "snapshot", error, job_id)

        current_stage = self._stage_value(getattr(status, "current_stage", None))
        failed_stage = self._stage_value(getattr(status, "failed_stage", None))
        completed = tuple(
            self._stage_value(item)
            for item in getattr(status, "completed_stages", ())
        )

        worker: WorkerRecord | Any | None = None
        try:
            worker = self._worker_reader(job_dir)
        except Exception as error:
            health = _merge_health(health, HealthStatus.UNKNOWN)
            self._record(issues, "worker", error, job_id)

        lock_active = False
        try:
            lock_active = self._lock_probe(job_dir / ".autopilot.lock")
        except Exception as error:
            health = _merge_health(health, HealthStatus.UNKNOWN)
            self._record(issues, "lock", error, job_id)
        if lock_active and (
            worker is None or getattr(worker, "finished_at", None) is not None
        ):
            health = _merge_health(health, HealthStatus.UNKNOWN)
            self._record(
                issues,
                "worker",
                RuntimeError("运行锁活跃但没有可确认的活动Worker记录"),
                job_id,
            )

        running = False
        worker_status = "NOT_STARTED"
        if worker is not None:
            worker_status = str(
                getattr(worker, "terminal_status", None) or "RUNNING"
            )
            if getattr(worker, "finished_at", None) is None:
                try:
                    probe = self._process_probe(int(worker.pid))
                    if probe.status is ProcessProbeStatus.UNKNOWN:
                        raise OSError("进程状态不可确认")
                    running = (
                        probe.status is ProcessProbeStatus.RUNNING
                        and process_identity_matches(
                            probe.identity,
                            pid=worker.pid,
                            created_at_ns=worker.process_created_at_ns,
                            executable=worker.process_executable,
                        )
                    )
                    running = running or lock_active
                except Exception as error:
                    health = _merge_health(health, HealthStatus.UNKNOWN)
                    self._record(issues, "process", error, job_id)

        self._check_log_health(job_dir, worker, issues, job_id)
        if any(
            issue.job_id == job_id and issue.source == "log"
            for issue in issues
        ):
            health = _merge_health(health, HealthStatus.DEGRADED)

        final_video: Path | None = None
        try:
            final_video = self._final_video_probe(job_dir, output_dir)
            has_final_video = final_video is not None
        except Exception as error:
            has_final_video = False
            health = _merge_health(health, HealthStatus.DEGRADED)
            self._record(issues, "delivery", error, job_id)

        research_summary = self._research_summary(
            job_dir, failed_stage, issues, job_id
        )
        if any(
            issue.job_id == job_id and issue.source == "research"
            for issue in issues
        ):
            health = _merge_health(health, HealthStatus.DEGRADED)
        try:
            decision = self._resume_planner(job_id)
        except Exception as error:
            decision = None
            health = _merge_health(health, HealthStatus.UNKNOWN)
            self._record(issues, "resume", error, job_id)

        direction = self._direction(job_dir, issues, job_id)
        if any(
            issue.job_id == job_id and issue.source == "direction"
            for issue in issues
        ):
            health = _merge_health(health, HealthStatus.DEGRADED)

        actions = derive_job_actions(
            existing_job=True,
            current_stage=current_stage,
            failed_stage=failed_stage,
            job_is_running=running,
            has_final_video=has_final_video,
            research_failure_summary=research_summary,
            resume_decision=decision,
        )
        if health is not HealthStatus.HEALTHY:
            actions = _closed_actions(
                "任务状态不完整或不可确认，已安全禁用操作，请稍后刷新。"
            )
        row = JobListItem(
            job_id=job_id,
            direction=direction,
            status=self._status_text(current_stage, failed_stage, running),
            stage=failed_stage or current_stage or "-",
            updated=str(
                snapshot.get("updated_at")
                or getattr(status, "updated_at", "-")
                or "-"
            )[:16].replace("T", " "),
        )
        return JobView(
            job_id=job_id,
            health=health,
            job_dir=str(job_dir),
            current_stage=current_stage,
            failed_stage=failed_stage,
            completed_stages=completed,
            snapshot_version=snapshot_version,
            running=running,
            worker_status=worker_status,
            lock_active=lock_active,
            has_final_video=has_final_video,
            final_video_path=str(final_video) if final_video is not None else "",
            actions=actions,
            row=row,
        )

    def _record(
        self,
        issues: list[HealthIssue],
        source: str,
        error: BaseException,
        job_id: str = "",
    ) -> None:
        issues.append(HealthIssue(source, str(error), job_id))
        log_state_exception(
            self._logger,
            event="job_view_model_collect_failed",
            source=source,
            error=error,
            job_id=job_id,
        )

    def _check_log_health(
        self,
        job_dir: Path,
        worker: Any | None,
        issues: list[HealthIssue],
        job_id: str,
    ) -> None:
        candidates = [job_dir / "_work" / "runtime" / "worker.log"]
        if worker is not None and getattr(worker, "log_path", None):
            candidates.insert(0, Path(worker.log_path))
        for path in candidates:
            try:
                if self._path_is_file(path):
                    self._path_stat(path)
                    return
            except Exception as error:
                self._record(issues, "log", error, job_id)
                return

    def _research_summary(
        self,
        job_dir: Path,
        failed_stage: str,
        issues: list[HealthIssue],
        job_id: str,
    ) -> str:
        if failed_stage != "RESEARCHED":
            return ""
        path = job_dir / "research_sources.json"
        try:
            if not self._path_is_file(path):
                return ""
            loaded = json.loads(self._read_text(path))
            if not isinstance(loaded, list):
                raise ValueError("research_sources.json根节点不是数组")
            return summarize_research_failure(
                [item for item in loaded if isinstance(item, dict)]
            )
        except Exception as error:
            self._record(issues, "research", error, job_id)
            return ""

    def _direction(
        self,
        job_dir: Path,
        issues: list[HealthIssue],
        job_id: str,
    ) -> str:
        path = job_dir / "direction.json"
        try:
            if not self._path_is_file(path):
                return "-"
            loaded = json.loads(self._read_text(path))
            text = str(
                loaded.get("series_name") or loaded.get("core_direction") or "-"
            )
            return text if len(text) <= 20 else text[:20] + "..."
        except Exception as error:
            self._record(issues, "direction", error, job_id)
            return "-"

    @staticmethod
    def _stage_value(value: Any) -> str:
        return str(getattr(value, "value", value) or "")

    @staticmethod
    def _status_text(current: str, failed: str, running: bool) -> str:
        if current == "FAILED_NEEDS_ATTENTION":
            return "✗ 需人工处理"
        if current == "FAILED_RETRYABLE":
            return "⚠ 可重试失败"
        if current == "COMPLETED":
            return "✓ 已完成"
        if failed:
            return "✗ 失败/等待"
        if current and current != "INIT":
            return "▶ 进行中" if running else "⚠ 异常中断"
        return "○ 待启动"

    @staticmethod
    def _find_final_video(job_dir: Path, output_dir: Path) -> Path | None:
        for path in (
            output_dir / "最终视频.mp4",
            job_dir / "delivery" / "video.mp4",
        ):
            if path.is_file():
                return path
        return None


class JobViewModelPoller:
    """为每次轮询分配新generation；单次顶层失败不终止后续轮询。"""

    def __init__(
        self,
        builder_factory: Callable[[], JobViewModelBuilder],
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._builder_factory = builder_factory
        self._logger = logger or logging.getLogger(__name__)
        self._generation = 0

    def next(
        self,
        *,
        selected_job_id: str,
        app_running: bool = False,
    ) -> JobViewModel:
        self._generation += 1
        try:
            return self._builder_factory().collect(
                self._generation,
                selected_job_id=selected_job_id,
                app_running=app_running,
            )
        except Exception as error:
            log_state_exception(
                self._logger,
                event="gui_view_model_poll_failed",
                source="poll",
                error=error,
                job_id=selected_job_id,
            )
            return JobViewModel(
                generation=self._generation,
                selected_job_id=selected_job_id,
                health=HealthStatus.UNKNOWN,
                actions=_closed_actions(
                    "任务状态暂时无法读取，已安全禁用操作，后台将自动重试。"
                ),
                issues=(
                    HealthIssue(
                        source="poll",
                        message=str(error),
                        job_id=selected_job_id,
                    ),
                ),
            )
