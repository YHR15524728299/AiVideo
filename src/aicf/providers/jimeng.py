from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Sequence

import yaml
from PIL import Image, UnidentifiedImageError
from aicf.atomic_io import atomic_replace
from PIL import Image, UnidentifiedImageError

from aicf.engines.clip_planner import choose_generation_duration
from aicf.logging_utils import sanitize_error


@dataclass(frozen=True)
class JimengCapabilities:
    image_command: list[str] = field(default_factory=list)
    video_command: list[str] = field(default_factory=list)
    status_command: list[str] = field(default_factory=list)
    wait_command: list[str] = field(default_factory=list)
    download_command: list[str] = field(default_factory=list)
    validate_command: list[str] = field(default_factory=list)
    supported_durations: list[float] = field(default_factory=list)
    supports_reference_image: bool = False
    supports_first_frame: bool = False
    supports_last_frame: bool = False
    supports_async_task: bool = False


@dataclass(frozen=True)
class GenerationRequest:
    kind: str
    prompt: str
    output_path: Path
    duration_seconds: float | None = None
    model: str | None = None
    ratio: str = "9:16"


@dataclass(frozen=True)
class GenerationResult:
    kind: str
    output_path: Path
    cached: bool = False
    degraded: bool = False
    degradation_reason: str | None = None
    ken_burns_plan_path: Path | None = None
    submit_id: str | None = None


class JimengCliNotFound(RuntimeError):
    pass


class DreaminaProtocolError(RuntimeError):
    pass


class DreaminaTaskFailed(RuntimeError):
    pass


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
Sleep = Callable[[float], None]
Clock = Callable[[], float]

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
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


def parse_jimeng_help(help_text: str) -> JimengCapabilities:
    lowered = help_text.lower()
    has_image = "text2image" in lowered and "--prompt" in lowered and "--ratio" in lowered
    has_video = (
        "text2video" in lowered
        and "--prompt" in lowered
        and "--duration" in lowered
        and "--ratio" in lowered
    )
    has_query = "query_result" in lowered and "--submit_id" in lowered
    has_download = has_query and "--download_dir" in lowered
    return JimengCapabilities(
        image_command=(
            [
                "text2image",
                "--prompt",
                "{prompt}",
                "--ratio",
                "{ratio}",
                "--model_version",
                "{model}",
                "--poll",
                "0",
            ]
            if has_image
            else []
        ),
        video_command=(
            [
                "text2video",
                "--prompt",
                "{prompt}",
                "--duration",
                "{duration}",
                "--ratio",
                "{ratio}",
                "--model_version",
                "{model}",
                "--poll",
                "0",
            ]
            if has_video
            else []
        ),
        status_command=(
            ["query_result", "--submit_id", "{submit_id}"] if has_query else []
        ),
        download_command=(
            [
                "query_result",
                "--submit_id",
                "{submit_id}",
                "--download_dir",
                "{download_dir}",
            ]
            if has_download
            else []
        ),
        supported_durations=[float(value) for value in range(4, 16)]
        if has_video
        else [],
        supports_async_task=has_image and has_video and has_query and has_download,
    )


def _default_candidates() -> list[list[str]]:
    configured = os.getenv("JIMENG_CLI_EXECUTABLE", "").strip()
    candidates: list[list[str]] = [[configured]] if configured else []
    discovered = shutil.which("dreamina")
    if discovered:
        candidates.append([discovered])
    legacy = shutil.which("jimeng")
    if legacy:
        candidates.append([legacy])
    return candidates


def _capability_config(
    command_prefix: Sequence[str],
    capabilities: JimengCapabilities,
) -> dict[str, object]:
    return {
        "provider": "dreamina",
        "command_prefix": list(command_prefix),
        "image_command": capabilities.image_command,
        "video_command": capabilities.video_command,
        "status_command": capabilities.status_command,
        "wait_command": [],
        "download_command": capabilities.download_command,
        "validate_command": [],
        "video": {
            "default_duration_seconds": 5,
            "hard_min_duration_seconds": 4,
            "hard_max_duration_seconds": 15,
            "detected_supported_durations": capabilities.supported_durations,
            "supports_reference_image": False,
            "supports_first_frame": False,
            "supports_last_frame": False,
            "supports_async_task": True,
        },
        "execution": {
            "timeout_seconds": 1800,
            "poll_interval_seconds": 2,
            "max_concurrency": 1,
            "retry_count": 1,
            "cache_enabled": True,
        },
        "detection": {
            "status": "detected",
            "checked_at": date.today().isoformat(),
            "source": "dreamina --help; text2image --help; text2video --help; query_result --help",
            "note": "Dreamina 1.4.11 异步协议：提交、query_result 轮询、download_dir 下载。",
        },
    }


def detect_jimeng_cli(
    candidates: Iterable[Sequence[str]] | None = None,
    *,
    config_path: str | Path | None = None,
    timeout_seconds: float = 5,
    command_runner: CommandRunner = subprocess.run,
) -> JimengCapabilities:
    failures: list[str] = []
    prefixes = candidates if candidates is not None else _default_candidates()
    for raw_prefix in prefixes:
        prefix = [str(token) for token in raw_prefix if str(token)]
        if not prefix:
            continue
        help_parts: list[str] = []
        try:
            for suffix in (
                ["--help"],
                ["text2image", "--help"],
                ["text2video", "--help"],
                ["query_result", "--help"],
            ):
                completed = command_runner(
                    [*prefix, *suffix],
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_seconds,
                )
                help_parts.extend((completed.stdout, completed.stderr))
        except (OSError, subprocess.SubprocessError) as error:
            failures.append(f"{prefix[0]}: {type(error).__name__}")
            continue
        capabilities = parse_jimeng_help("\n".join(help_parts))
        if not capabilities.supports_async_task:
            failures.append(f"{prefix[0]}: --help 缺少 Dreamina 1.4.11 必需能力")
            continue
        if config_path is not None:
            target = Path(config_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".tmp")
            temporary.write_text(
                yaml.safe_dump(
                    _capability_config(prefix, capabilities),
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            atomic_replace(temporary, target)
        return capabilities
    detail = "；".join(failures) if failures else "未配置命令且 PATH 中未发现 dreamina"
    raise JimengCliNotFound(f"未定位到真实 Dreamina CLI，M4 已阻塞：{detail}")


class JimengCliAdapter:
    def __init__(
        self,
        executable: str | Sequence[str],
        capabilities: JimengCapabilities,
        *,
        timeout_seconds: float = 1800,
        poll_interval_seconds: float = 2,
        cache_dir: str | Path | None = None,
        retry_count: int = 1,
        ffprobe_executable: str = "ffprobe",
        command_runner: CommandRunner = subprocess.run,
        sleep: Sleep = time.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        prefix = [executable] if isinstance(executable, str) else list(executable)
        if not prefix or not all(str(token).strip() for token in prefix):
            raise ValueError("Dreamina CLI 可执行文件不能为空")
        if timeout_seconds <= 0:
            raise ValueError("CLI 超时必须大于 0")
        if poll_interval_seconds <= 0:
            raise ValueError("轮询间隔必须大于 0")
        self.command_prefix = [str(token) for token in prefix]
        self.executable = self.command_prefix[0]
        self.capabilities = capabilities
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.retry_count = min(max(0, retry_count), 1)
        self.ffprobe_executable = ffprobe_executable
        self._command_runner = command_runner
        self._sleep = sleep
        self._clock = clock

    @staticmethod
    def _render_template(template: list[str], values: dict[str, str]) -> list[str]:
        return [token.format_map(values) for token in template]

    def _build_command(
        self,
        template: list[str],
        values: dict[str, str],
        capability_name: str,
    ) -> list[str]:
        if not template:
            raise RuntimeError(f"真实 CLI 未探测到 {capability_name} 能力")
        return [*self.command_prefix, *self._render_template(template, values)]

    def _run(
        self,
        command: list[str],
        *,
        idempotent: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        attempts = self.retry_count + 1 if idempotent else 1
        for attempt in range(attempts):
            try:
                return self._command_runner(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                if attempt + 1 >= attempts:
                    raise
        raise AssertionError("unreachable")

    @staticmethod
    def _parse_json_output(completed: subprocess.CompletedProcess[str], name: str) -> dict[str, object]:
        text = completed.stdout.strip()
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = None
            decoder = json.JSONDecoder()
            for offset, character in enumerate(text):
                if character != "{":
                    continue
                try:
                    candidate, _ = decoder.raw_decode(text[offset:])
                except json.JSONDecodeError:
                    continue
                if isinstance(candidate, dict):
                    value = candidate
        if not isinstance(value, dict):
            raise DreaminaProtocolError(f"{name} 未返回 JSON 对象")
        return value

    def _run_json(
        self,
        command: list[str],
        name: str,
        *,
        idempotent: bool = False,
    ) -> dict[str, object]:
        return self._parse_json_output(
            self._run(command, idempotent=idempotent),
            name,
        )

    @staticmethod
    def _find_string(value: object, keys: set[str]) -> str | None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key.lower() in keys and isinstance(nested, str) and nested.strip():
                    return nested.strip()
            for nested in value.values():
                found = JimengCliAdapter._find_string(nested, keys)
                if found:
                    return found
        if isinstance(value, list):
            for nested in value:
                found = JimengCliAdapter._find_string(nested, keys)
                if found:
                    return found
        return None

    @classmethod
    def _submit_id(cls, payload: dict[str, object]) -> str:
        submit_id = cls._find_string(payload, {"submit_id", "submitid"})
        if not submit_id:
            raise DreaminaProtocolError("提交响应缺少 submit_id")
        return submit_id

    @classmethod
    def _task_state(cls, payload: dict[str, object]) -> str:
        state = cls._find_string(payload, {"gen_status", "status", "state"})
        return state.lower() if state else ""

    @classmethod
    def _failure_reason(cls, payload: dict[str, object]) -> str:
        reason = cls._find_string(
            payload,
            {"fail_reason", "failure_reason", "error", "message"},
        ) or "Dreamina 任务失败"
        return sanitize_error(reason)

    @staticmethod
    def _cache_key(
        prompt: str,
        model: str,
        ratio: str,
        duration: float | None,
    ) -> str:
        payload = json.dumps(
            {
                "prompt": prompt,
                "model": model,
                "ratio": ratio,
                "duration": duration,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _cache_path(
        self,
        prompt: str,
        model: str,
        ratio: str,
        duration: float | None,
        suffix: str,
    ) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{self._cache_key(prompt, model, ratio, duration)}{suffix}"

    def _restore_cache(
        self,
        prompt: str,
        model: str,
        ratio: str,
        duration: float | None,
        target: Path,
        *,
        kind: str,
    ) -> bool:
        cached = self._cache_path(prompt, model, ratio, duration, target.suffix)
        if cached is None or not cached.is_file():
            return False
        valid = self.validate_image(cached) if kind == "image" else self.validate_video(cached)
        if not valid:
            cached.unlink(missing_ok=True)
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(cached, target)
        return True

    def _store_cache(
        self,
        prompt: str,
        model: str,
        ratio: str,
        duration: float | None,
        target: Path,
    ) -> None:
        cached = self._cache_path(prompt, model, ratio, duration, target.suffix)
        if cached is not None:
            shutil.copy2(target, cached)

    def build_image_command(
        self,
        prompt: str,
        output_path: str | Path | None = None,
        *,
        model: str = "4.1",
        ratio: str = "9:16",
    ) -> list[str]:
        del output_path
        return self._build_command(
            self.capabilities.image_command,
            {"prompt": prompt, "model": model, "ratio": ratio},
            "text2image",
        )

    def build_video_command(
        self,
        prompt: str,
        required_seconds: float,
        output_path: str | Path | None = None,
        *,
        model: str = "seedance2.0fast",
        ratio: str = "9:16",
    ) -> list[str]:
        del output_path
        duration = self._generation_duration(required_seconds)
        return self._build_command(
            self.capabilities.video_command,
            {
                "prompt": prompt,
                "duration": str(int(duration)),
                "model": model,
                "ratio": ratio,
            },
            "text2video",
        )

    def _generation_duration(self, required_seconds: float) -> float:
        if required_seconds < 4 or required_seconds > 15:
            raise ValueError("Dreamina 视频时长必须位于 4-15 秒")
        duration = choose_generation_duration(
            required_seconds,
            self.capabilities.supported_durations,
        )
        if duration is None:
            raise ValueError(f"无法生成 {required_seconds} 秒单镜头")
        return duration

    def _submit(self, command: list[str]) -> str:
        payload = self._run_json(command, "Dreamina 提交")
        return self._submit_id(payload)

    def _query(self, submit_id: str) -> dict[str, object]:
        command = self._build_command(
            self.capabilities.status_command,
            {"submit_id": submit_id},
            "query_result",
        )
        return self._run_json(command, "query_result", idempotent=True)

    def _poll(self, submit_id: str) -> dict[str, object]:
        deadline = self._clock() + self.timeout_seconds
        while True:
            payload = self._query(submit_id)
            state = self._task_state(payload)
            if state in _SUCCESS_STATES:
                return payload
            if state in _FAILURE_STATES:
                raise DreaminaTaskFailed(self._failure_reason(payload))
            if state not in _PENDING_STATES:
                raise DreaminaProtocolError(f"query_result 返回未知状态：{state or '<空>'}")
            if self._clock() >= deadline:
                raise TimeoutError(f"Dreamina 任务 {submit_id} 轮询超时")
            self._sleep(self.poll_interval_seconds)

    def _download(self, submit_id: str, target: Path, *, kind: str) -> Path:
        download_dir = target.parent / f".{target.stem}.{submit_id}.download"
        shutil.rmtree(download_dir, ignore_errors=True)
        download_dir.mkdir(parents=True, exist_ok=True)
        command = self._build_command(
            self.capabilities.download_command,
            {"submit_id": submit_id, "download_dir": str(download_dir)},
            "query_result --download_dir",
        )
        try:
            payload = self._run_json(
                command,
                "query_result --download_dir",
                idempotent=True,
            )
            state = self._task_state(payload)
            if state in _FAILURE_STATES:
                raise DreaminaTaskFailed(self._failure_reason(payload))
            extensions = _IMAGE_EXTENSIONS if kind == "image" else _VIDEO_EXTENSIONS
            candidates = sorted(
                path
                for path in download_dir.rglob("*")
                if path.is_file() and path.suffix.lower() in extensions
            )
            if not candidates:
                raise DreaminaProtocolError("query_result --download_dir 未下载媒体文件")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            shutil.move(str(candidates[0]), str(target))
            return target
        finally:
            shutil.rmtree(download_dir, ignore_errors=True)

    @staticmethod
    def validate_image(input_path: str | Path) -> bool:
        path = Path(input_path)
        if not path.is_file() or path.stat().st_size == 0:
            return False
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image.load()
                return image.width > 0 and image.height > 0
        except (OSError, ValueError, UnidentifiedImageError):
            return False

    def validate_video(self, input_path: str | Path) -> bool:
        path = Path(input_path)
        if not path.is_file() or path.stat().st_size == 0:
            return False
        command = [
            self.ffprobe_executable,
            "-v",
            "error",
            "-show_streams",
            "-of",
            "json",
            str(path),
        ]
        try:
            payload = self._run_json(command, "ffprobe", idempotent=True)
        except (OSError, subprocess.SubprocessError, DreaminaProtocolError):
            return False
        streams = payload.get("streams")
        return isinstance(streams, list) and any(
            isinstance(stream, dict) and stream.get("codec_type") == "video"
            for stream in streams
        )

    def generate_image(
        self,
        prompt: str,
        output_path: str | Path,
        *,
        model: str = "4.1",
        ratio: str = "9:16",
    ) -> GenerationResult:
        target = Path(output_path)
        if self._restore_cache(prompt, model, ratio, None, target, kind="image"):
            return GenerationResult("image", target, cached=True)
        submit_id = self._submit(
            self.build_image_command(prompt, model=model, ratio=ratio)
        )
        self._poll(submit_id)
        self._download(submit_id, target, kind="image")
        if not self.validate_image(target):
            target.unlink(missing_ok=True)
            raise RuntimeError("Dreamina 下载了无效图片")
        self._store_cache(prompt, model, ratio, None, target)
        return GenerationResult("image", target, submit_id=submit_id)

    def generate_video(
        self,
        prompt: str,
        required_seconds: float,
        output_path: str | Path,
        *,
        model: str = "seedance2.0fast",
        ratio: str = "9:16",
        fallback_to_keyframe: bool = True,
    ) -> GenerationResult:
        target = Path(output_path)
        duration = self._generation_duration(required_seconds)
        if self._restore_cache(
            prompt,
            model,
            ratio,
            duration,
            target,
            kind="video",
        ):
            return GenerationResult("video", target, cached=True)
        try:
            submit_id = self._submit(
                self.build_video_command(
                    prompt,
                    required_seconds,
                    model=model,
                    ratio=ratio,
                )
            )
            self._poll(submit_id)
            self._download(submit_id, target, kind="video")
            if not self.validate_video(target):
                raise RuntimeError("Dreamina 下载了无效视频")
        except (
            OSError,
            subprocess.SubprocessError,
            TimeoutError,
            DreaminaProtocolError,
            DreaminaTaskFailed,
            RuntimeError,
        ) as error:
            target.unlink(missing_ok=True)
            if not fallback_to_keyframe:
                raise
            keyframe = target.with_suffix(".keyframe.png")
            image_result = self.generate_image(
                prompt,
                keyframe,
                model="4.1",
                ratio=ratio,
            )
            plan_path = target.with_suffix(".ken_burns.json")
            plan_path.write_text(
                json.dumps(
                    {
                        "source": str(image_result.output_path),
                        "duration_seconds": required_seconds,
                        "keyframes": [
                            {"at": 0.0, "scale": 1.0, "x": 0.5, "y": 0.5},
                            {
                                "at": required_seconds,
                                "scale": 1.12,
                                "x": 0.52,
                                "y": 0.48,
                            },
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            return GenerationResult(
                "video",
                image_result.output_path,
                cached=image_result.cached,
                degraded=True,
                degradation_reason=f"{type(error).__name__}: {error}",
                ken_burns_plan_path=plan_path,
                submit_id=image_result.submit_id,
            )
        self._store_cache(prompt, model, ratio, duration, target)
        return GenerationResult("video", target, submit_id=submit_id)

    def submit_image(
        self,
        prompt: str,
        *,
        model: str = "4.1",
        ratio: str = "9:16",
    ) -> str:
        return self._submit(
            self.build_image_command(prompt, model=model, ratio=ratio)
        )

    def submit_video(
        self,
        prompt: str,
        required_seconds: float,
        *,
        model: str = "seedance2.0fast",
        ratio: str = "9:16",
    ) -> str:
        return self._submit(
            self.build_video_command(
                prompt,
                required_seconds,
                model=model,
                ratio=ratio,
            )
        )

    def query(self, task_id: str) -> dict[str, object]:
        return self._query(task_id)

    @classmethod
    def failure_reason(cls, payload: dict[str, object]) -> str:
        return cls._failure_reason(payload)

    def status(self, task_id: str) -> dict[str, object]:
        return self._query(task_id)

    def wait(self, task_id: str) -> dict[str, object]:
        return self._poll(task_id)

    def download(
        self,
        task_id: str,
        output_path: str | Path,
        *,
        kind: str | None = None,
    ) -> Path:
        target = Path(output_path)
        resolved_kind = kind or (
            "video" if target.suffix.lower() in _VIDEO_EXTENSIONS else "image"
        )
        return self._download(task_id, target, kind=resolved_kind)

    def validate(self, input_path: str | Path) -> bool:
        path = Path(input_path)
        if path.suffix.lower() in _VIDEO_EXTENSIONS:
            return self.validate_video(path)
        return self.validate_image(path)

    def generate_chain(
        self,
        requests: Iterable[GenerationRequest],
    ) -> list[GenerationResult]:
        results: list[GenerationResult] = []
        for request in requests:
            if request.kind == "image":
                results.append(
                    self.generate_image(
                        request.prompt,
                        request.output_path,
                        model=request.model or "4.1",
                        ratio=request.ratio,
                    )
                )
            elif request.kind == "video":
                if request.duration_seconds is None:
                    raise ValueError("视频请求必须提供 duration_seconds")
                results.append(
                    self.generate_video(
                        request.prompt,
                        request.duration_seconds,
                        request.output_path,
                        model=request.model or "seedance2.0fast",
                        ratio=request.ratio,
                    )
                )
            else:
                raise ValueError(f"不支持的生成类型：{request.kind}")
        return results
