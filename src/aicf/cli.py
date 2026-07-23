from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Sequence

import yaml
from PIL import Image

from .autopilot import Autopilot
from .cache import FileCache
from .config import load_config
from .database import JobRepository
from .doctor import Doctor
from .engines.m6_engine import M6Pipeline, RepairEngine
from .engines.narration_engine import NarrationPipeline
from .engines.render_engine import FfmpegRenderer, probe_media
from .m4_asset_runner import M4AssetRunner
from .m2_runner import M2ContentRunner
from .m5_runner import M5VisualPlanRunner
from .gui import launch as launch_gui
from .logging_utils import sanitize_error
from .providers.jimeng import JimengCliAdapter, detect_jimeng_cli
from .providers.openrouter import OpenRouterClient
from .providers.tts import (
    EdgeTtsProvider,
    SapiTtsProvider,
    TtsService,
    build_default_tts_service,
    discover_ffmpeg_toolchain,
)
from .state_machine import PipelineStage


def project_root() -> Path:
    configured = os.getenv("AICF_PROJECT_ROOT")
    return Path(configured) if configured else Path.cwd()


def repository() -> JobRepository:
    return JobRepository(project_root() / "data" / "content.db")


def build_narration_pipeline() -> NarrationPipeline:
    toolchain = discover_ffmpeg_toolchain()
    service = TtsService(
        [
            EdgeTtsProvider(ffmpeg_executable=toolchain.ffmpeg),
            SapiTtsProvider(ffmpeg_executable=toolchain.ffmpeg),
        ]
    )
    return NarrationPipeline(service=service, toolchain=toolchain)


def build_renderer() -> FfmpegRenderer:
    return FfmpegRenderer(discover_ffmpeg_toolchain())


def build_m2_runner(
    job_repository: JobRepository | None = None,
) -> M2ContentRunner:
    root = project_root()
    client = OpenRouterClient(cache=FileCache(root / "data" / "openrouter_cache"))
    return M2ContentRunner(
        client,
        job_repository or repository(),
        root / "outputs",
    )


def build_dreamina_adapter() -> JimengCliAdapter:
    root = project_root()
    config_path = root / "config" / "jimeng_cli.yaml"
    candidates = None
    settings: dict[str, object] = {}
    if config_path.is_file():
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            settings = loaded
            configured_prefix = loaded.get("command_prefix")
            if isinstance(configured_prefix, list) and configured_prefix:
                candidates = [[str(token) for token in configured_prefix]]
    capabilities = detect_jimeng_cli(
        candidates,
        config_path=config_path,
        timeout_seconds=30,
    )
    prefix = yaml.safe_load(config_path.read_text(encoding="utf-8"))["command_prefix"]
    execution = settings.get("execution", {})
    if not isinstance(execution, dict):
        execution = {}
    return JimengCliAdapter(
        prefix,
        capabilities,
        timeout_seconds=float(execution.get("timeout_seconds", 1800)),
        poll_interval_seconds=float(execution.get("poll_interval_seconds", 2)),
        retry_count=int(execution.get("retry_count", 1)),
        cache_dir=root / "data" / "dreamina_cache",
    )


def build_m4_asset_runner() -> M4AssetRunner:
    root = project_root()
    adapter = build_dreamina_adapter()
    ffprobe = discover_ffmpeg_toolchain().ffprobe
    config = load_config(root / "config" / "content_direction.yaml")

    def media_probe(path: Path, kind: str) -> dict[str, object]:
        if kind == "image":
            with Image.open(path) as image:
                return {
                    "kind": "image",
                    "format": image.format,
                    "width": image.width,
                    "height": image.height,
                    "size_bytes": path.stat().st_size,
                }
        return {"kind": "video", **asdict(probe_media(ffprobe, path))}

    return M4AssetRunner(
        adapter,
        media_probe=media_probe,
        asset_cache_dir=(
            root / "data" / "dreamina_asset_cache"
            if config.generation_budget.enable_asset_cache
            else None
        ),
    )


def build_autopilot(job_repository: JobRepository) -> Autopilot:
    root = project_root()
    config = load_config(root / "config" / "content_direction.yaml")
    toolchain = discover_ffmpeg_toolchain()
    renderer = FfmpegRenderer(toolchain)
    return Autopilot(
        job_repository,
        content_runner=build_m2_runner(job_repository),
        narration_pipeline=build_narration_pipeline(),
        visual_plan_runner=M5VisualPlanRunner(),
        asset_runner=build_m4_asset_runner(),
        renderer=renderer,
        m6_pipeline=M6Pipeline(
            toolchain,
            repair_engine=RepairEngine(toolchain, renderer=renderer),
        ),
        config=config,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aicf")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor")
    tts_smoke = subparsers.add_parser("tts-smoke")
    tts_smoke.add_argument(
        "--output",
        type=Path,
        default=Path("outputs") / "tts_smoke.wav",
    )
    tts_smoke.add_argument(
        "--text",
        default="AI Content Factory 语音合成冒烟测试。",
    )
    dreamina_smoke = subparsers.add_parser("dreamina-smoke")
    dreamina_smoke.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs") / "DREAMINA_SMOKE",
    )
    dreamina_smoke.add_argument(
        "--prompt",
        default="电影感中国未来城市清晨，金色阳光穿过薄雾，竖屏构图，细节丰富，无文字",
    )
    dreamina_smoke.add_argument("--model", default="4.1")
    asset_run = subparsers.add_parser("asset-run")
    asset_run.add_argument("--visual-plan", required=True, type=Path)
    asset_run.add_argument("--resume", action="store_true")
    batch_synthesize = subparsers.add_parser("batch-synthesize")
    batch_synthesize.add_argument("--script", required=True, type=Path)
    batch_synthesize.add_argument("--output-dir", required=True, type=Path)
    render = subparsers.add_parser("render")
    render.add_argument("--visual-plan", required=True, type=Path)
    render.add_argument("--audio", required=True, type=Path)
    render.add_argument("--subtitles", required=True, type=Path)
    render.add_argument("--output", required=True, type=Path)
    render.add_argument("--title", required=True)
    init_job = subparsers.add_parser("init-job")
    init_job.add_argument("--job", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("--job")
    rebuild_snapshot = subparsers.add_parser("rebuild-snapshot")
    rebuild_snapshot.add_argument("--job", required=True)
    resume = subparsers.add_parser("resume")
    resume.add_argument("--job", required=True)
    reopen = subparsers.add_parser("reopen")
    reopen.add_argument("--job", required=True)
    reopen.add_argument("--confirm-artifacts-fixed", action="store_true")
    reopen.add_argument(
        "--recoverable-reason",
        choices=[
            "credentials_restored",
            "external_service_restored",
            "dependency_restored",
        ],
    )
    retry = subparsers.add_parser("retry")
    retry.add_argument("--job", required=True)
    retry.add_argument("--stage", required=True, choices=[s.value for s in PipelineStage])
    autopilot = subparsers.add_parser("autopilot")
    autopilot.add_argument("--job", required=True)
    content_run = subparsers.add_parser("content-run")
    content_run.add_argument("--job", required=True)
    subparsers.add_parser("ui")
    return parser


def _print_status(status) -> None:
    stage = status.current_stage.value if status.current_stage else "尚未开始"
    print(f"{status.job_id}: {stage}")
    if status.failed_stage:
        print(f"失败阶段: {status.failed_stage.value}")
        print(f"恢复命令: {status.next_resume_command}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        report = Doctor().run()
        print(report.to_text())
        return 0 if report.healthy else 1
    if args.command == "tts-smoke":
        result = build_default_tts_service().synthesize(args.text, args.output)
        print(f"Provider: {result.provider}")
        print(f"音频: {args.output}")
        print(f"元数据: {result.metadata_path}")
        if result.degraded:
            print(f"降级原因: {result.degradation_reason}")
        return 0
    if args.command == "dreamina-smoke":
        target = args.output_dir / "dreamina_smoke.png"
        try:
            result = build_dreamina_adapter().generate_image(
                args.prompt,
                target,
                model=args.model,
                ratio="9:16",
            )
        except Exception as error:
            print(
                json.dumps(
                    {"status": "failed", "error": sanitize_error(error)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        print(f"Dreamina 图片: {result.output_path}")
        print(f"submit_id: {result.submit_id}")
        print(f"比例: 9:16")
        print(f"缓存命中: {'是' if result.cached else '否'}")
        return 0
    if args.command == "asset-run":
        try:
            result = build_m4_asset_runner().run(
                args.visual_plan,
                resume=args.resume,
            )
        except Exception as error:
            result = {"status": "FAILED", "error": sanitize_error(error)}
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "COMPLETED" else 2
    if args.command == "batch-synthesize":
        script = json.loads(args.script.read_text(encoding="utf-8-sig"))
        video = load_config(
            project_root() / "config" / "content_direction.yaml"
        ).video
        result = build_narration_pipeline().batch_synthesize(
            script,
            args.output_dir,
            target_duration_seconds=video.target_duration_seconds,
            min_duration_seconds=video.min_duration_seconds,
            max_duration_seconds=video.max_duration_seconds,
        )
        print(f"分句音频: {len(result.segment_paths)} 个")
        print(f"旁白: {result.voiceover_path}")
        print(f"时间线: {result.timeline_path}")
        print(f"字幕: {result.srt_path}")
        print(f"ASS: {result.ass_path}")
        return 0
    if args.command == "render":
        result, probes = build_renderer().render_and_validate(
            visual_plan_path=args.visual_plan,
            audio_path=args.audio,
            subtitle_path=args.subtitles,
            output_path=args.output,
            title=args.title,
        )
        probe = probes["master"]
        print(f"成片: {result.output_path}")
        print(f"Clean: {result.clean_output_path}")
        print(f"渲染清单: {result.manifest_path}")
        print(
            f"ffprobe: {probe.width}x{probe.height} {probe.fps:g}fps "
            f"{probe.video_codec}/{probe.pixel_format} "
            f"{probe.audio_codec}/{probe.sample_rate}Hz/{probe.channels}ch "
            f"{probe.duration_seconds:.3f}s {probe.size_bytes}bytes"
        )
        return 0
    if args.command == "content-run":
        config = load_config(project_root() / "config" / "content_direction.yaml")
        try:
            result = build_m2_runner().run(args.job, config)
        except Exception as error:
            print(
                json.dumps(
                    {"status": "failed", "error": sanitize_error(error)},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "ready_to_publish" else 1
    repo = repository()
    if args.command == "autopilot":
        try:
            repo.get_job(args.job)
        except KeyError:
            repo.create_job(args.job, project_root() / "outputs" / args.job)
        try:
            result = build_autopilot(repo).run(args.job)
        except FileNotFoundError as error:
            recovery = "powershell -File scripts/doctor.ps1"
            result = {
                "status": "FAILED_NEEDS_ATTENTION",
                "reason": sanitize_error(error),
                "recovery_command": recovery,
            }
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "READY_TO_PUBLISH" else 1
    if args.command == "init-job":
        output_dir = project_root() / "outputs" / args.job
        _print_status(repo.create_job(args.job, output_dir))
        return 0
    if args.command == "status":
        if args.job:
            _print_status(repo.get_job(args.job))
        else:
            for status in repo.list_jobs():
                _print_status(status)
        return 0
    if args.command == "rebuild-snapshot":
        status = repo.rebuild_snapshot(args.job)
        if status.snapshot_dirty:
            print(f"{status.job_id}: status.json 快照重建失败")
            return 1
        print(f"{status.job_id}: status.json 快照已重建（version={status.version}）")
        return 0
    if args.command == "resume":
        status = repo.get_job(args.job)
        if status.current_stage == PipelineStage.FAILED_NEEDS_ATTENTION:
            result = {
                "status": PipelineStage.FAILED_NEEDS_ATTENTION.value,
                "reason": (
                    "该 Job 需要人工确认后才能重开；"
                    "resume 不会执行不可恢复阶段"
                ),
                "recovery_command": status.next_resume_command,
            }
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 2
        result = build_autopilot(repo).run(args.job)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("status") == "READY_TO_PUBLISH" else 1
    if args.command == "reopen":
        try:
            status = repo.reopen_failed_attention(
                args.job,
                artifacts_fixed=args.confirm_artifacts_fixed,
                recoverable_reason=args.recoverable_reason,
            )
        except Exception as error:
            print(
                json.dumps(
                    {
                        "status": "REOPEN_REJECTED",
                        "reason": sanitize_error(error),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 2
        print(
            json.dumps(
                {
                    "status": "REOPENED",
                    "job_id": status.job_id,
                    "stage": (
                        status.current_stage.value
                        if status.current_stage
                        else "尚未开始"
                    ),
                    "next_command": (
                        f"python -m aicf resume --job {status.job_id}"
                    ),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "retry":
        stage = PipelineStage(args.stage)
        repo.start_stage(args.job, stage)
        print(f"{args.job}: 重试 {stage.value}")
        return 0
    if args.command == "ui":
        launch_gui()
        return 0
    return 2
