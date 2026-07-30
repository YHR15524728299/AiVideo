from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PIL import Image, UnidentifiedImageError

from aicf.engines.clip_planner import choose_generation_duration
from aicf.logging_utils import sanitize_error

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KlingCliCapabilities:
    """可灵 CLI 能力描述。"""
    cli_path: str = "kling"
    supported_durations: list[float] = field(default_factory=lambda: [5.0, 10.0])
    supported_ratios: list[str] = field(
        default_factory=lambda: ["9:16", "2:3", "3:4", "1:1", "4:3", "3:2", "16:9", "21:9"]
    )
    supports_reference_image: bool = True
    supports_first_frame: bool = True
    supports_last_frame: bool = True
    supports_async_task: bool = True
    default_image_model: str = "kling-image-v2_1"
    default_video_model: str = "kling-v1-6"
    default_video_duration: float = 5.0
    detection_error: str | None = None


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


class KlingCliNotFound(RuntimeError):
    pass


class KlingCliError(RuntimeError):
    pass


class KlingTaskFailed(RuntimeError):
    pass


class KlingConfigError(RuntimeError):
    pass


Sleep = Callable[[float], None]
Clock = Callable[[], float]

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm"}
_PENDING_STATES = {
    "submitted",
    "queued",
    "queueing",
    "processing",
    "running",
    "generating",
    "waiting",
}
_SUCCESS_STATES = {"succeed", "success", "succeeded", "completed", "done", "partial_completed"}
_FAILURE_STATES = {"failed", "failure", "error", "cancelled", "canceled"}


def _default_config_path() -> Path:
    root = Path(os.getenv("AICF_PROJECT_ROOT", Path.cwd()))
    return root / "config" / "kling_cli.yaml"


def _find_kling_cli() -> str | None:
    """查找可灵CLI可执行文件。"""
    # 1. 环境变量指定（用户手动配置）
    env_path = (os.environ.get("KLING_CLI_EXECUTABLE") or "").strip()
    if env_path and Path(env_path).is_file():
        return env_path
    # 2. PATH 中查找
    path = shutil.which("kling")
    if path:
        return path
    # 3. TRAE node 内置路径 / npm 全局路径
    candidates = [
        Path(os.environ.get("APPDATA", "") or "") / "TRAE SOLO CN" / "ModularData" / "ai-agent" / "vm" / "tools" / "node" / "kling.cmd",
        Path(os.environ.get("APPDATA", "") or "") / "npm" / "kling.cmd",
    ]
    for c in candidates:
        try:
            if c.is_file():
                return str(c)
        except Exception:
            pass
    return None


def _build_cli_command(cli_path: str, args: list[str]) -> list[str]:
    """构建 CLI 命令列表，Windows 上 .cmd/.bat 文件需通过 cmd /c 调用。"""
    if os.name == "nt" and cli_path.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", cli_path, *args]
    return [cli_path, *args]


def detect_kling_cli(
    config_path: str | Path | None = None,
    *,
    timeout_seconds: float = 15,
) -> KlingCliCapabilities:
    """检测可灵CLI是否可用，返回能力描述。"""
    cli_path = _find_kling_cli()
    if not cli_path:
        return KlingCliCapabilities(supports_async_task=False, detection_error="未找到可灵CLI可执行文件")

    # 尝试获取模型列表
    detection_error: str | None = None
    try:
        cmd = _build_cli_command(cli_path, ["who_am_i"])
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        if result.returncode != 0:
            stderr = result.stderr.strip()
            stdout = result.stdout.strip()
            msg = stderr or stdout or f"exit code {result.returncode}"
            detection_error = f"who_am_i 返回非零退出码: {sanitize_error(msg)[:300]}"
            logger.warning("可灵CLI who_am_i 失败（cli_path=%s）: %s", cli_path, detection_error)
        else:
            data = json.loads(result.stdout)
            body = data.get("body", data)
            models_info = body.get("availableModels", {})
            # 找默认视频模型
            video_models = models_info.get("text_to_video", {}).get("models", [])
            default_video = "kling-v1-6"
            for m in video_models:
                if "v1-6" in m.get("model", ""):
                    default_video = m["model"]
                    break
                if not default_video or "v2" in m.get("model", ""):
                    default_video = m["model"]
            # 找默认图片模型
            image_models = models_info.get("text_to_image", {}).get("models", [])
            default_image = "kling-image-v2_1"
            for m in image_models:
                if "v2_1" in m.get("model", ""):
                    default_image = m["model"]
                    break
            return KlingCliCapabilities(
                cli_path=cli_path,
                default_image_model=default_image,
                default_video_model=default_video,
                supports_async_task=True,
            )
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as error:
        detection_error = f"who_am_i 执行异常: {type(error).__name__}: {sanitize_error(str(error))[:300]}"
        logger.warning("可灵CLI who_am_i 异常（cli_path=%s）: %s", cli_path, detection_error)

    # CLI存在但who_am_i失败（认证过期/网络问题等），返回cli_path但标记为不可用
    return KlingCliCapabilities(
        cli_path=cli_path,
        supports_async_task=False,
        detection_error=detection_error or "未知检测错误",
    )


class KlingCliAdapter:
    """可灵 CLI 适配器，接口与 JimengCliAdapter 保持一致。"""

    def __init__(
        self,
        cli_path: str | Path,
        capabilities: KlingCliCapabilities | None = None,
        *,
        timeout_seconds: float = 1800,
        poll_interval_seconds: float = 3,
        cache_dir: str | Path | None = None,
        retry_count: int = 2,
        ffprobe_executable: str = "ffprobe",
        sleep: Sleep = time.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self.cli_path = str(cli_path)
        self.capabilities = capabilities or KlingCliCapabilities(cli_path=self.cli_path)
        self.timeout_seconds = timeout_seconds
        self.poll_interval_seconds = poll_interval_seconds
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.retry_count = min(max(0, retry_count), 3)
        self.ffprobe_executable = ffprobe_executable
        self._sleep = sleep
        self._clock = clock

    # ---------- CLI 执行基础方法 ----------

    def _run_cli(self, args: list[str], *, timeout: float | None = None) -> dict[str, Any]:
        """执行kling命令并解析JSON响应。"""
        timeout = timeout if timeout is not None else self.timeout_seconds
        cmd = _build_cli_command(self.cli_path, args)

        last_error: Exception | None = None
        for attempt in range(self.retry_count + 1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                if result.returncode != 0:
                    stderr = result.stderr.strip()
                    stdout = result.stdout.strip()
                    msg = stderr or stdout or f"exit code {result.returncode}"
                    # 尝试解析错误JSON
                    try:
                        err_data = json.loads(stdout) if stdout else {}
                        if isinstance(err_data, dict) and not err_data.get("ok", True):
                            msg = err_data.get("error") or err_data.get("message") or msg
                    except Exception:
                        pass
                    raise KlingCliError(f"可灵CLI错误: {sanitize_error(msg)[:300]}")

                stdout = result.stdout.strip()
                if not stdout:
                    raise KlingCliError("可灵CLI返回空响应")
                data = json.loads(stdout)
                if isinstance(data, dict) and data.get("ok") is False:
                    raise KlingCliError(f"可灵CLI错误: {data.get('error', data.get('message', '未知错误'))}")
                return data.get("body", data)
            except (subprocess.TimeoutExpired, json.JSONDecodeError, KlingCliError) as error:
                last_error = error
                if isinstance(error, KlingCliError) and ("认证" in str(error) or "login" in str(error).lower()):
                    raise
                if attempt >= self.retry_count:
                    raise
            wait = float(min(30, 3 * (2 ** attempt)))
            self._sleep(wait)

        if last_error:
            raise last_error
        raise AssertionError("unreachable")

    @staticmethod
    def _download_file(url: str, output_path: Path, timeout: float = 300, max_retries: int = 3) -> Path:
        """下载文件到指定路径，支持重试（指数退避）。"""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                request = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    with open(output_path, "wb") as f:
                        shutil.copyfileobj(response, f)
                return output_path
            except (urllib.error.URLError, OSError, TimeoutError) as error:
                last_error = error
                if attempt < max_retries - 1:
                    wait = float(2 ** attempt)
                    logger.warning(
                        "下载文件失败（第%d次尝试），%gs后重试: %s",
                        attempt + 1, wait, sanitize_error(str(error))[:200],
                    )
                    time.sleep(wait)
                else:
                    logger.error(
                        "下载文件失败，已达最大重试次数(%d): %s",
                        max_retries, sanitize_error(str(error))[:200],
                    )
        if last_error:
            raise last_error
        raise AssertionError("unreachable")

    # ---------- 缓存 ----------

    @staticmethod
    def _cache_key(
        prompt: str,
        model: str,
        ratio: str,
        duration: float | None,
        *,
        kind: str,
    ) -> str:
        payload = json.dumps(
            {
                "prompt": prompt,
                "model": model,
                "ratio": ratio,
                "duration": duration,
                "kind": kind,
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
        *,
        kind: str,
    ) -> Path | None:
        if self.cache_dir is None:
            return None
        key = self._cache_key(prompt, model, ratio, duration, kind=kind)
        return self.cache_dir / f"{key}{suffix}"

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
        cached = self._cache_path(prompt, model, ratio, duration, target.suffix, kind=kind)
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
        *,
        kind: str,
    ) -> None:
        cached = self._cache_path(prompt, model, ratio, duration, target.suffix, kind=kind)
        if cached is not None:
            shutil.copy2(target, cached)

    # ---------- 任务状态解析 ----------

    @staticmethod
    def _extract_generation_id(payload: dict[str, Any]) -> str:
        """从提交响应中提取 generationId。"""
        gen_id = (
            payload.get("generationId")
            or payload.get("generation_id")
            or payload.get("task_id")
            or payload.get("id")
        )
        if not gen_id and isinstance(payload.get("data"), dict):
            data = payload["data"]
            gen_id = data.get("generationId") or data.get("generation_id") or data.get("task_id") or data.get("id")
        if not gen_id or not isinstance(gen_id, (str, int)):
            raise KlingCliError(f"提交响应缺少 generationId，响应: {json.dumps(payload, ensure_ascii=False)[:300]}")
        return str(gen_id)

    @staticmethod
    def _extract_task_state(payload: dict[str, Any]) -> str:
        """从查询响应中提取任务状态。"""
        state = (
            payload.get("task_status")
            or payload.get("status")
            or payload.get("state")
        )
        if not state and isinstance(payload.get("data"), dict):
            data = payload["data"]
            state = data.get("task_status") or data.get("status") or data.get("state")
        return str(state).lower() if state else ""

    @staticmethod
    def _extract_failure_reason(payload: dict[str, Any]) -> str:
        """从失败响应中提取失败原因。"""
        for key in ("task_status_msg", "error", "message", "fail_reason", "status_msg"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return sanitize_error(val.strip())
        if isinstance(payload.get("data"), dict):
            data = payload["data"]
            for key in ("task_status_msg", "error", "message", "fail_reason", "status_msg"):
                val = data.get(key)
                if isinstance(val, str) and val.strip():
                    return sanitize_error(val.strip())
        return "可灵任务失败"

    @staticmethod
    def _extract_result_urls(payload: dict[str, Any], kind: str) -> list[str]:
        """从成功响应中提取结果文件URL列表。"""
        urls: list[str] = []
        data = payload
        if not any(k in payload for k in ("works", "task_id", "generationId", "videos", "images")):
            data = payload.get("data", payload)
        if not isinstance(data, dict):
            return urls

        works = data.get("works") or []
        if isinstance(works, list):
            for w in works:
                if isinstance(w, dict):
                    # 优先无水印，其次带水印
                    url = w.get("urlWithoutWatermark") or w.get("url")
                    if isinstance(url, str) and url.strip():
                        urls.append(url.strip())
        if not urls:
            # 兼容其他结构
            if kind == "video":
                videos = data.get("videos") or data.get("video") or []
                if isinstance(videos, dict):
                    videos = [videos]
                if isinstance(videos, list):
                    for v in videos:
                        if isinstance(v, dict):
                            url = v.get("url") or v.get("video_url")
                            if isinstance(url, str) and url.strip():
                                urls.append(url.strip())
            else:
                images = data.get("images") or data.get("image") or []
                if isinstance(images, dict):
                    images = [images]
                if isinstance(images, list):
                    for img in images:
                        if isinstance(img, dict):
                            url = img.get("url") or img.get("image_url")
                            if isinstance(url, str) and url.strip():
                                urls.append(url.strip())
        return urls

    # ---------- 公共 API（与 JimengCliAdapter 接口一致） ----------

    def submit_image(
        self,
        prompt: str,
        *,
        model: str | None = None,
        ratio: str = "9:16",
        resolution: str = "2k",
        **kwargs: Any,
    ) -> str:
        """提交文生图任务，返回 generationId。（resolution参数已忽略，保持接口兼容）"""
        del resolution
        resolved_model = model or self.capabilities.default_image_model
        args = [
            "text_to_image",
            "--model", resolved_model,
            "--aspectRatio", ratio,
            prompt,
        ]
        payload = self._run_cli(args)
        return self._extract_generation_id(payload)

    def submit_video(
        self,
        prompt: str,
        required_seconds: float,
        *,
        model: str | None = None,
        ratio: str = "9:16",
        resolution: str = "720p",
        **kwargs: Any,
    ) -> str:
        """提交文生视频任务，返回 generationId。（resolution参数已忽略，可灵由模型决定画质）"""
        del resolution
        resolved_model = model or self.capabilities.default_video_model
        duration = int(self._generation_duration(required_seconds))
        args = [
            "text_to_video",
            "--model", resolved_model,
            "--aspectRatio", ratio,
            "--duration", str(duration),
            prompt,
        ]
        payload = self._run_cli(args)
        return self._extract_generation_id(payload)

    def query(self, generation_id: str, *, kind: str = "video") -> dict[str, Any]:
        """查询任务状态。"""
        args = ["query_tasks", generation_id]
        return self._run_cli(args)

    def wait(self, generation_id: str, *, kind: str = "video") -> dict[str, Any]:
        """轮询等待任务完成，返回最终payload。"""
        deadline = self._clock() + self.timeout_seconds
        last_state = ""
        while True:
            payload = self.query(generation_id, kind=kind)
            state = self._extract_task_state(payload)
            if state and state != last_state:
                last_state = state
            if state in _SUCCESS_STATES:
                return payload
            if state in _FAILURE_STATES:
                raise KlingTaskFailed(self._extract_failure_reason(payload))
            if state and state not in _PENDING_STATES:
                # 未知状态但可能是新状态，继续轮询，直到超时
                pass
            if self._clock() >= deadline:
                raise TimeoutError(f"可灵任务 {generation_id} 轮询超时（{self.timeout_seconds}s）")
            self._sleep(self.poll_interval_seconds)

    def download(
        self,
        generation_id: str,
        output_path: str | Path,
        *,
        kind: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Path:
        """下载任务结果到指定路径。"""
        target = Path(output_path)
        resolved_kind = kind or ("video" if target.suffix.lower() in _VIDEO_EXTENSIONS else "image")

        if payload is None:
            payload = self.wait(generation_id, kind=resolved_kind)

        urls = self._extract_result_urls(payload, resolved_kind)
        if not urls:
            raise KlingCliError(f"可灵任务 {generation_id} 成功但未找到结果URL")

        # 下载第一个结果
        url = urls[0]
        tmp_path = target.with_suffix(target.suffix + ".tmp")
        try:
            self._download_file(url, tmp_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.unlink(missing_ok=True)
            shutil.move(str(tmp_path), str(target))
        finally:
            tmp_path.unlink(missing_ok=True)

        return target

    def failure_reason(self, payload: dict[str, Any]) -> str:
        """从payload中提取失败原因。"""
        # 尝试多种可能的字段名
        for key in ("fail_reason", "failure_reason", "error", "message", "msg", "reason"):
            if key in payload and payload[key]:
                return str(payload[key])
        # 递归查找
        reason = self._find_string(payload, {"fail_reason", "failure_reason", "error", "message", "msg", "reason"})
        return reason or "可灵任务失败"

    @staticmethod
    def _find_string(payload: Any, keys: set[str]) -> str | None:
        """递归在payload中查找指定key的字符串值。"""
        if isinstance(payload, dict):
            for k, v in payload.items():
                if k in keys and isinstance(v, str) and v:
                    return v
            for v in payload.values():
                result = KlingCliAdapter._find_string(v, keys)
                if result:
                    return result
        elif isinstance(payload, list):
            for item in payload:
                result = KlingCliAdapter._find_string(item, keys)
                if result:
                    return result
        return None

    def _generation_duration(self, required_seconds: float) -> float:
        """将所需秒数映射到可灵支持的时长。"""
        durations = self.capabilities.supported_durations
        duration = choose_generation_duration(required_seconds, durations)
        if duration is None:
            if required_seconds <= durations[0]:
                return durations[0]
            return durations[-1]
        return duration

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
            "-v", "error",
            "-show_streams",
            "-of", "json",
            str(path),
        ]
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                return False
            payload = json.loads(result.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
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
        model: str | None = None,
        ratio: str = "9:16",
    ) -> GenerationResult:
        """完整流程：提交→等待→下载图片。"""
        target = Path(output_path)
        resolved_model = model or self.capabilities.default_image_model
        if self._restore_cache(prompt, resolved_model, ratio, None, target, kind="image"):
            return GenerationResult("image", target, cached=True)

        gen_id = self.submit_image(prompt, model=resolved_model, ratio=ratio)
        payload = self.wait(gen_id, kind="image")
        self.download(gen_id, target, kind="image", payload=payload)

        if not self.validate_image(target):
            target.unlink(missing_ok=True)
            raise KlingCliError("可灵下载了无效图片")

        self._store_cache(prompt, resolved_model, ratio, None, target, kind="image")
        return GenerationResult("image", target, submit_id=gen_id)

    def generate_video(
        self,
        prompt: str,
        required_seconds: float,
        output_path: str | Path,
        *,
        model: str | None = None,
        ratio: str = "9:16",
        fallback_to_keyframe: bool = True,
    ) -> GenerationResult:
        """完整流程：提交→等待→下载视频。失败时降级为Ken Burns效果。"""
        target = Path(output_path)
        resolved_model = model or self.capabilities.default_video_model
        duration = self._generation_duration(required_seconds)

        if self._restore_cache(prompt, resolved_model, ratio, duration, target, kind="video"):
            return GenerationResult("video", target, cached=True)

        try:
            gen_id = self.submit_video(prompt, required_seconds, model=resolved_model, ratio=ratio)
            payload = self.wait(gen_id, kind="video")
            self.download(gen_id, target, kind="video", payload=payload)
            if not self.validate_video(target):
                raise KlingCliError("可灵下载了无效视频")
        except (OSError, TimeoutError, KlingCliError, KlingTaskFailed, RuntimeError) as error:
            target.unlink(missing_ok=True)
            if not fallback_to_keyframe:
                raise
            # 降级为Ken Burns效果（首图+缩放平移）
            keyframe = target.with_suffix(".keyframe.png")
            image_result = self.generate_image(prompt, keyframe, ratio=ratio)
            plan_path = target.with_suffix(".ken_burns.json")
            plan_path.write_text(
                json.dumps(
                    {
                        "source": str(image_result.output_path),
                        "duration_seconds": required_seconds,
                        "keyframes": [
                            {"at": 0.0, "scale": 1.0, "x": 0.5, "y": 0.5},
                            {"at": required_seconds, "scale": 1.12, "x": 0.52, "y": 0.48},
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

        self._store_cache(prompt, resolved_model, ratio, duration, target, kind="video")
        return GenerationResult("video", target, submit_id=gen_id)

    def status(self, submit_id: str, *, kind: str = "video") -> dict[str, Any]:
        """alias for query"""
        return self.query(submit_id, kind=kind)

    def validate(self, input_path: str | Path) -> bool:
        path = Path(input_path)
        if path.suffix.lower() in _VIDEO_EXTENSIONS:
            return self.validate_video(path)
        return self.validate_image(path)

    def generate_chain(
        self,
        requests: list[GenerationRequest],
    ) -> list[GenerationResult]:
        results: list[GenerationResult] = []
        for request in requests:
            if request.kind == "image":
                results.append(
                    self.generate_image(
                        request.prompt,
                        request.output_path,
                        model=request.model,
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
                        model=request.model,
                        ratio=request.ratio,
                    )
                )
            else:
                raise ValueError(f"不支持的生成类型：{request.kind}")
        return results


def build_kling_adapter(config_path: str | Path | None = None) -> KlingCliAdapter | None:
    """工厂函数：构建 KlingCliAdapter，如果CLI不可用则返回 None。"""
    capabilities = detect_kling_cli(config_path)
    if not capabilities.supports_async_task or not capabilities.cli_path:
        return None

    root = Path(os.getenv("AICF_PROJECT_ROOT", Path.cwd()))
    return KlingCliAdapter(
        cli_path=capabilities.cli_path,
        capabilities=capabilities,
        timeout_seconds=1800,
        poll_interval_seconds=3,
        retry_count=2,
        cache_dir=root / "data" / "kling_cache",
    )
