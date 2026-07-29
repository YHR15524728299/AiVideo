from __future__ import annotations

import hashlib
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable, cast

from aicf.atomic_io import atomic_replace
from aicf.models import VisualPlan
from aicf.production_settings import ProductionSettings


_PENDING_STATES = {
    "pending",
    "queued",
    "queueing",
    "querying",
    "processing",
    "running",
    "generating",
    "waiting",
}
_SUCCESS_STATES = {"success", "succeeded", "completed", "done"}
_FAILURE_STATES = {"failure", "failed", "error", "cancelled", "canceled"}


class M4AssetRunner:
    def __init__(
        self,
        providers: dict[str, object] | object,
        *,
        media_probe: Callable[[Path, str], dict[str, object]],
        pending_timeout_seconds: float = 1800,
        poll_interval_seconds: float = 2,
        asset_cache_dir: str | Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # 兼容旧的单provider传参方式
        if isinstance(providers, dict):
            self.providers = dict(providers)
        else:
            self.providers = {"jimeng": providers}
        self.media_probe = media_probe
        self.pending_timeout_seconds = pending_timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.asset_cache_dir = (
            Path(asset_cache_dir).resolve() if asset_cache_dir is not None else None
        )
        self.clock = clock
        self.sleep = sleep
        self.production_settings = ProductionSettings()
        self._current_provider_key = next(iter(self.providers), "jimeng")

    @property
    def provider(self) -> object:
        """获取当前选中的视频生成适配器。"""
        return self.providers.get(
            self._current_provider_key,
            self.providers.get("jimeng"),
        )

    _PROVIDER_DISPLAY_NAMES = {
        "jimeng": "即梦",
        "kling": "可灵",
    }

    def _provider_display_name(self) -> str:
        return self._PROVIDER_DISPLAY_NAMES.get(
            self._current_provider_key, self._current_provider_key
        )

    def run(
        self,
        visual_plan_path: str | Path,
        *,
        resume: bool = False,
        usage_recorder: Callable[..., object] | None = None,
        budget_guard: Callable[..., object] | None = None,
    ) -> dict[str, object]:
        plan_path = Path(visual_plan_path).resolve()
        plan = VisualPlan.model_validate_json(
            plan_path.read_text(encoding="utf-8-sig")
        )
        root = plan_path.parent
        self.production_settings = ProductionSettings.load_for_job(root)
        # 根据settings选择当前provider
        self._current_provider_key = getattr(
            self.production_settings, "video_provider", "jimeng"
        )
        if self._current_provider_key not in self.providers:
            self._current_provider_key = next(iter(self.providers), "jimeng")
        self._apply_motion_mode(plan)
        tasks_path = root / "assets" / "tasks.json"
        manifest_path = root / "asset_manifest.json"
        tasks_document = self._load_or_create_tasks(tasks_path, plan)
        tasks = cast(list[dict[str, object]], tasks_document["tasks"])

        for shot, task in zip(plan.shots, tasks, strict=True):
            original_target = (root / shot.expected_path).resolve()
            active_kind = str(task.get("active_kind") or shot.asset_type)
            target = (
                original_target.with_suffix(".keyframe.png")
                if task.get("degraded_from") == "video"
                else original_target
            )
            if task["status"] == "completed" and target.is_file():
                try:
                    self._validate_media(target, active_kind)
                except (OSError, ValueError, TypeError):
                    task["status"] = "submitted" if task.get("submit_id") else "new"
                    self._write_json(tasks_path, tasks_document)
                else:
                    if task.get("degraded_from") == "video":
                        shot.asset_type = "image"
                        shot.expected_path = self._relative_path(root, target)
                    continue
            if (
                task.get("status")
                in {"submission_intent", "UNKNOWN_REMOTE_SUBMISSION"}
                and not task.get("submit_id")
            ):
                task["status"] = "UNKNOWN_REMOTE_SUBMISSION"
                self._write_json(tasks_path, tasks_document)
                return {
                    "status": "FAILED_NEEDS_ATTENTION",
                    "shot_id": shot.shot_id,
                    "reason": "UNKNOWN_REMOTE_SUBMISSION",
                    "recovery_command": (
                        "人工核对即梦后台后更新 assets/tasks.json；禁止自动重提"
                    ),
                }
            if not task.get("submit_id") and self._restore_from_cache(
                task,
                target,
                active_kind,
                shot.prompt,
                shot.duration_seconds,
            ):
                task["status"] = "completed"
                task["downloaded_path"] = self._relative_path(root, target)
                task["cache_hit"] = True
                self._write_json(tasks_path, tasks_document)
                continue
            if not task.get("submit_id"):
                self._submit_with_intent(
                    tasks_path,
                    tasks_document,
                    task,
                    active_kind,
                    shot.prompt,
                    shot.duration_seconds,
                    usage_recorder,
                    budget_guard,
                )
            self._record_usage_if_needed(task, usage_recorder)
            self._write_json(tasks_path, tasks_document)

            deadline = self.clock() + self.pending_timeout_seconds
            while True:
                payload = self.provider.query(str(task["submit_id"]))
                state = self._state(payload)
                if state in _SUCCESS_STATES:
                    self._download_validated(
                        str(task["submit_id"]),
                        target,
                        active_kind,
                    )
                    task["status"] = "completed"
                    task["downloaded_path"] = self._relative_path(root, target)
                    task["cache_hit"] = False
                    if task.get("degraded_from") == "video":
                        shot.asset_type = "image"
                        shot.expected_path = self._relative_path(root, target)
                        ken_burns_path = original_target.with_suffix(".ken_burns.json")
                        self._write_ken_burns(
                            ken_burns_path,
                            shot.expected_path,
                            shot.duration_seconds,
                        )
                        task["ken_burns_path"] = self._relative_path(
                            root, ken_burns_path
                        )
                    self._store_in_cache(
                        target,
                        active_kind,
                        shot.prompt,
                        shot.duration_seconds,
                    )
                    self._write_json(tasks_path, tasks_document)
                    break
                if state in _FAILURE_STATES:
                    task["status"] = "failed"
                    self._write_json(tasks_path, tasks_document)
                    reason = self.provider.failure_reason(payload)
                    if (
                        active_kind == shot.asset_type
                        and int(task["attempts"]) < 2
                    ):
                        task["submit_id"] = None
                        self._submit_with_intent(
                            tasks_path,
                            tasks_document,
                            task,
                            active_kind,
                            shot.prompt,
                            shot.duration_seconds,
                            usage_recorder,
                            budget_guard,
                        )
                        continue
                    raise RuntimeError(f"{shot.shot_id} 生成失败：{reason}")
                if state not in _PENDING_STATES:
                    provider_label = self._provider_display_name()
                    raise RuntimeError(
                        f"{shot.shot_id} 返回未知 {provider_label} 状态：{state or '<空>'}"
                    )
                task["status"] = "pending"
                self._write_json(tasks_path, tasks_document)
                if self.clock() >= deadline:
                    return {
                        "status": "WAITING_EXTERNAL",
                        "shot_id": shot.shot_id,
                        "recovery_command": (
                            "python -m aicf asset-run "
                            f'--visual-plan "{plan_path}" --resume'
                        ),
                    }
                self.sleep(self.poll_interval_seconds)

        self._publish_outputs(plan_path, manifest_path, plan, tasks)
        return {
            "status": "COMPLETED",
            "visual_plan_path": str(plan_path),
            "asset_manifest_path": str(manifest_path),
            "tasks_path": str(tasks_path),
        }

    def _submit(self, kind: str, prompt: str, duration_seconds: float) -> str:
        parameters = self._submission_parameters(kind, duration_seconds)
        if kind == "image":
            return str(
                self.provider.submit_image(
                    prompt,
                    model=str(parameters["model"]),
                    ratio=str(parameters["ratio"]),
                    resolution=str(parameters["resolution"]),
                )
            )
        return str(
            self.provider.submit_video(
                prompt,
                duration_seconds,
                model=str(parameters["model"]),
                ratio=str(parameters["ratio"]),
                resolution=str(parameters["resolution"]),
            )
        )

    def _submit_with_intent(
        self,
        tasks_path: Path,
        tasks_document: dict[str, object],
        task: dict[str, object],
        kind: str,
        prompt: str,
        duration_seconds: float,
        usage_recorder: Callable[..., object] | None,
        budget_guard: Callable[..., object] | None,
    ) -> None:
        parameters = self._submission_parameters(kind, duration_seconds)
        usage = self._usage_delta(kind, duration_seconds)
        request_id = uuid.uuid4().hex
        if budget_guard is not None:
            budget_guard(request_id=request_id, **usage)
        task.update(
            {
                "request_id": request_id,
                "submission_parameters": parameters,
                "submit_id": None,
                "status": "submission_intent",
                "active_kind": kind,
                "usage_recorded": False,
            }
        )
        self._write_json(tasks_path, tasks_document)
        submit_id = self._submit(kind, prompt, duration_seconds)
        task["submit_id"] = submit_id
        task["attempts"] = int(task["attempts"]) + 1
        task["status"] = "submitted"
        self._write_json(tasks_path, tasks_document)
        self._record_usage_if_needed(task, usage_recorder)
        self._write_json(tasks_path, tasks_document)

    def _submission_parameters(
        self,
        kind: str,
        duration_seconds: float,
    ) -> dict[str, object]:
        ratio = "16:9" if self.production_settings.orientation == "landscape" else "9:16"
        # 根据当前provider选择模型
        if self._current_provider_key == "kling":
            video_model = getattr(
                self.production_settings, "kling_model", "kling-video-v2_6"
            )
            image_model = "kling-v1"  # 可灵图生图默认模型
        else:
            video_model = self.production_settings.jimeng_model
            image_model = "4.1"
        return {
            "kind": kind,
            "model": (
                image_model
                if kind == "image"
                else video_model
            ),
            "ratio": ratio,
            "resolution": (
                "2k"
                if kind == "image"
                else self.production_settings.video_resolution
            ),
            "duration_seconds": duration_seconds,
        }

    def _apply_motion_mode(self, plan: VisualPlan) -> None:
        """根据 motion_mode 将所有镜头设置为纯图片或纯视频。

        视频模式下，即梦视频要求时长 4-15 秒，短于 4 秒的镜头会被调整为 4 秒。
        """
        desired = self.production_settings.motion_mode  # "image" 或 "video"
        for shot in plan.shots:
            if shot.asset_type == desired:
                if desired == "video" and shot.duration_seconds < 4:
                    shot.duration_seconds = 4.0
                continue
            shot.asset_type = desired
            suffix = ".mp4" if desired == "video" else ".png"
            shot.expected_path = str(
                Path(shot.expected_path).with_suffix(suffix)
            ).replace("\\", "/")
            if desired == "video" and shot.duration_seconds < 4:
                shot.duration_seconds = 4.0
        plan.total_duration_seconds = sum(s.duration_seconds for s in plan.shots)

    def _usage_delta(self, kind: str, duration_seconds: float) -> dict[str, int]:
        prefix = self._current_provider_key
        is_image = kind == "image"
        is_video = kind == "video"
        seconds = int(duration_seconds) if is_video else 0
        result: dict[str, int] = {
            f"{prefix}_images": 1 if is_image else 0,
            f"{prefix}_video_clips": 1 if is_video else 0,
            f"{prefix}_video_seconds_requested": seconds,
        }
        # 同时设置通用字段名，方便跨provider统一统计
        result["images"] = 1 if is_image else 0
        result["video_clips"] = 1 if is_video else 0
        result["video_seconds_requested"] = seconds
        return result

    def _record_usage_if_needed(
        self,
        task: dict[str, object],
        usage_recorder: Callable[..., object] | None,
    ) -> None:
        if (
            usage_recorder is None
            or task.get("usage_recorded")
            or not task.get("submit_id")
            or not task.get("request_id")
        ):
            return
        parameters = cast(dict[str, object], task["submission_parameters"])
        usage_recorder(
            request_id=str(task["request_id"]),
            **self._usage_delta(
                str(parameters["kind"]),
                float(parameters["duration_seconds"]),
            ),
        )
        task["usage_recorded"] = True

    def _cache_paths(
        self,
        kind: str,
        prompt: str,
        duration_seconds: float,
        suffix: str,
    ) -> tuple[Path, Path] | None:
        if self.asset_cache_dir is None:
            return None
        payload = {
            "prompt": prompt,
            **self._submission_parameters(kind, duration_seconds),
        }
        key = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        return (
            self.asset_cache_dir / f"{key}{suffix}",
            self.asset_cache_dir / f"{key}.json",
        )

    def _restore_from_cache(
        self,
        task: dict[str, object],
        target: Path,
        kind: str,
        prompt: str,
        duration_seconds: float,
    ) -> bool:
        paths = self._cache_paths(kind, prompt, duration_seconds, target.suffix)
        if paths is None:
            return False
        source, metadata_path = paths
        if not source.is_file() or not metadata_path.is_file():
            return False
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            expected_hash = str(metadata["sha256"])
            if self._sha256(source) != expected_hash:
                return False
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(
                f".{target.stem}.cache-{uuid.uuid4().hex}{target.suffix}"
            )
            shutil.copy2(source, temporary)
            if self._sha256(temporary) != expected_hash:
                temporary.unlink(missing_ok=True)
                return False
            self._validate_media(temporary, kind)
            atomic_replace(temporary, target)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            if "temporary" in locals():
                temporary.unlink(missing_ok=True)
            return False
        task["cache_key"] = metadata_path.stem
        return True

    def _store_in_cache(
        self,
        target: Path,
        kind: str,
        prompt: str,
        duration_seconds: float,
    ) -> None:
        paths = self._cache_paths(kind, prompt, duration_seconds, target.suffix)
        if paths is None:
            return
        cache_path, metadata_path = paths
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = cache_path.with_suffix(cache_path.suffix + ".tmp")
        shutil.copy2(target, temporary)
        atomic_replace(temporary, cache_path)
        self._write_json(
            metadata_path,
            {
                "sha256": self._sha256(cache_path),
                "media_probe": self._validate_media(cache_path, kind),
            },
        )

    def _download_validated(
        self,
        submit_id: str,
        target: Path,
        kind: str,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(
            f".{target.stem}.download-{uuid.uuid4().hex}{target.suffix}"
        )
        try:
            self.provider.download(submit_id, temporary, kind=kind)
            self._validate_media(temporary, kind)
            atomic_replace(temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _validate_media(self, path: Path, kind: str) -> dict[str, object]:
        if not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f"{kind} 素材为空或不存在: {path.name}")
        probe = self.media_probe(path, kind)
        reported_kind = probe.get("kind")
        if reported_kind is not None and str(reported_kind) != kind:
            raise ValueError(
                f"素材类型不匹配: 期望 {kind}，实际 {reported_kind}"
            )
        for dimension in ("width", "height"):
            if int(probe.get(dimension, 0)) <= 0:
                raise ValueError(f"{kind} 素材缺少有效 {dimension}")
        if kind == "image" and not str(probe.get("format", "")).strip():
            raise ValueError("图片素材格式无效")
        if kind == "video" and float(probe.get("duration_seconds", 0)) <= 0:
            raise ValueError("视频素材时长无效")
        return probe

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _state(payload: dict[str, object]) -> str:
        for key in ("gen_status", "status", "state"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().lower()
        return ""

    def _load_or_create_tasks(
        self,
        tasks_path: Path,
        plan: VisualPlan,
    ) -> dict[str, object]:
        if tasks_path.is_file():
            loaded = json.loads(tasks_path.read_text(encoding="utf-8-sig"))
            if not isinstance(loaded, dict) or not isinstance(loaded.get("tasks"), list):
                raise ValueError("assets/tasks.json 格式无效")
            existing = {
                str(task.get("shot_id")): task
                for task in loaded["tasks"]
                if isinstance(task, dict)
            }
        else:
            existing = {}

        tasks: list[dict[str, object]] = []
        for shot in plan.shots:
            prompt_hash = hashlib.sha256(shot.prompt.encode("utf-8")).hexdigest()
            task = existing.get(shot.shot_id)
            if task is None:
                task = {
                    "shot_id": shot.shot_id,
                    "asset_type": shot.asset_type,
                    "active_kind": shot.asset_type,
                    "prompt_hash": prompt_hash,
                    "submit_id": None,
                    "status": "new",
                    "attempts": 0,
                    "downloaded_path": None,
                    "request_id": None,
                    "submission_parameters": None,
                    "usage_recorded": False,
                }
            elif task.get("prompt_hash") != prompt_hash:
                if task.get("submit_id"):
                    raise ValueError(
                        f"{shot.shot_id} 已有 submit_id，不能更改 prompt 后重新提交"
                    )
                task.update(
                    {
                        "asset_type": shot.asset_type,
                        "active_kind": shot.asset_type,
                        "prompt_hash": prompt_hash,
                        "status": "new",
                        "attempts": 0,
                        "downloaded_path": None,
                        "request_id": None,
                        "submission_parameters": None,
                        "usage_recorded": False,
                    }
                )
            else:
                expected_parameters = self._submission_parameters(
                    shot.asset_type,
                    shot.duration_seconds,
                )
                type_conflict = (
                    not task.get("degraded_from")
                    and (
                        task.get("asset_type") != shot.asset_type
                        or task.get("active_kind") != shot.asset_type
                    )
                )
                saved_parameters = task.get("submission_parameters")
                settings_conflict = (
                    isinstance(saved_parameters, dict)
                    and not task.get("degraded_from")
                    and saved_parameters != expected_parameters
                )
                if type_conflict or settings_conflict:
                    if task.get("submit_id") or task.get("status") != "new":
                        raise ValueError(
                            f"{shot.shot_id} 恢复冲突：素材类型或生产设置已变化"
                        )
                    task.update(
                        {
                            "asset_type": shot.asset_type,
                            "active_kind": shot.asset_type,
                            "submission_parameters": None,
                        }
                    )
            tasks.append(task)
        document: dict[str, object] = {"version": 1, "tasks": tasks}
        self._write_json(tasks_path, document)
        return document

    def _publish_outputs(
        self,
        plan_path: Path,
        manifest_path: Path,
        plan: VisualPlan,
        tasks: list[dict[str, object]],
    ) -> None:
        root = plan_path.parent
        assets: list[dict[str, object]] = []
        for shot, task in zip(plan.shots, tasks, strict=True):
            target = (root / str(task["downloaded_path"])).resolve()
            shot.expected_path = self._relative_path(root, target)
            assets.append(
                {
                    "shot_id": shot.shot_id,
                    "script_segment_id": shot.script_segment_id,
                    "type": shot.asset_type,
                    "prompt": shot.prompt,
                    "expected_path": shot.expected_path,
                    "authoritative_duration_seconds": shot.duration_seconds,
                    "sha256": self._sha256(target),
                    "media_probe": self._validate_media(target, shot.asset_type),
                }
            )
        self._write_json(plan_path, plan.model_dump(mode="json"))
        self._write_json(
            manifest_path,
            {
                "mode": plan.mode,
                "total_duration_seconds": plan.total_duration_seconds,
                "assets": assets,
            },
        )

    @staticmethod
    def _relative_path(root: Path, path: Path) -> str:
        return path.relative_to(root.resolve()).as_posix()

    @classmethod
    def _write_ken_burns(
        cls,
        path: Path,
        source: str,
        duration_seconds: float,
    ) -> None:
        cls._write_json(
            path,
            {
                "source": source,
                "duration_seconds": duration_seconds,
                "keyframes": [
                    {"at": 0.0, "scale": 1.0, "x": 0.5, "y": 0.5},
                    {
                        "at": duration_seconds,
                        "scale": 1.12,
                        "x": 0.52,
                        "y": 0.48,
                    },
                ],
            },
        )

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        atomic_replace(temporary, path)
