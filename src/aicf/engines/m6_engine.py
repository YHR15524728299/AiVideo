from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from aicf.atomic_io import atomic_replace
from aicf.engines.render_engine import probe_media
from aicf.engines.subtitle_engine import build_ass
from aicf.artifact_commit import DirectoryPromoter
from aicf.models.contracts import SUPPORTED_PLATFORMS
from aicf.platform_export import PlatformExporter
from aicf.production_settings import get_resolution
from aicf.providers.tts import FfmpegToolchain


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
PORTRAIT_DELIVERY_PLATFORMS = tuple(
    platform for platform in SUPPORTED_PLATFORMS if platform != "youtube"
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    atomic_replace(temporary, path)


def _run(
    runner: CommandRunner,
    command: list[str],
) -> subprocess.CompletedProcess[str]:
    return runner(
        command,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _ass_cues(path: Path) -> list[tuple[float, float, str]]:
    def seconds(value: str) -> float:
        hour, minute, rest = value.split(":")
        return int(hour) * 3600 + int(minute) * 60 + float(rest)

    cues: list[tuple[float, float, str]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.startswith("Dialogue:"):
            continue
        fields = line.split(",", 9)
        if len(fields) == 10:
            cues.append((seconds(fields[1]), seconds(fields[2]), fields[9].strip()))
    return cues


def _timeline_entries(path: Path) -> list[tuple[float, float, str]]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    raw_entries = value.get("segments", []) if isinstance(value, dict) else value
    if not isinstance(raw_entries, list):
        raise ValueError("timeline 顶层必须是数组或包含 segments 数组")
    entries: list[tuple[float, float, str]] = []
    for index, item in enumerate(raw_entries):
        if not isinstance(item, dict):
            raise ValueError(f"timeline 第 {index + 1} 项必须是对象")
        start = float(item.get("start_seconds", item.get("start", -1)))
        end = float(item.get("end_seconds", item.get("end", -1)))
        if start < 0 or end <= start:
            raise ValueError(f"timeline 第 {index + 1} 项时间无效")
        entries.append((start, end, str(item.get("text", "")).strip()))
    return entries


def _ass_safety(path: Path, *, orientation: str = "portrait") -> dict[str, object]:
    text = path.read_text(encoding="utf-8-sig")
    play_res_x = re.search(r"^PlayResX:\s*(\d+)", text, re.MULTILINE)
    play_res_y = re.search(r"^PlayResY:\s*(\d+)", text, re.MULTILINE)
    style_lines = re.findall(r"^Style:\s*(.+)$", text, re.MULTILINE)
    styles: dict[str, dict[str, int]] = {}
    for line in style_lines:
        fields = [field.strip() for field in line.split(",")]
        if len(fields) < 13:
            continue
        try:
            styles[fields[0]] = {
                "alignment": int(fields[7]),
                "margin_l": int(fields[8]),
                "margin_r": int(fields[9]),
                "margin_v": int(fields[10]),
            }
        except ValueError:
            continue
    used_styles = {
        fields[3].strip()
        for line in text.splitlines()
        if line.startswith("Dialogue:")
        for fields in [line.split(",", 9)]
        if len(fields) == 10
    }
    expected_width, expected_height = get_resolution(orientation)
    if orientation == "landscape":
        # 横屏安全区域：底部居中字幕
        safe_styles = {
            name
            for name, style in styles.items()
            if style["alignment"] == 2
            and style["margin_l"] >= 100
            and style["margin_r"] >= 100
            and 60 <= style["margin_v"] <= 200
        }
    else:
        safe_styles = {
            name
            for name, style in styles.items()
            if style["alignment"] == 2
            and style["margin_l"] >= 60
            and style["margin_r"] >= 60
            and 120 <= style["margin_v"] <= 360
        }
    passed = (
        bool(play_res_x)
        and bool(play_res_y)
        and int(play_res_x.group(1)) == expected_width
        and int(play_res_y.group(1)) == expected_height
        and bool(used_styles)
        and used_styles <= safe_styles
    )
    return {
        "passed": passed,
        "play_res": [
            int(play_res_x.group(1)) if play_res_x else 0,
            int(play_res_y.group(1)) if play_res_y else 0,
        ],
        "used_styles": sorted(used_styles),
        "safe_styles": sorted(safe_styles),
    }


def _artifact_hash(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_delivery_probe(
    probe: object,
    expected_duration_seconds: float,
    *,
    orientation: str,
    expected_resolution: tuple[int, int] | None = None,
) -> None:
    assertion = getattr(probe, "assert_vertical_delivery")
    try:
        if expected_resolution is None:
            assertion(expected_duration_seconds, orientation=orientation)
        else:
            assertion(
                expected_duration_seconds,
                orientation=orientation,
                expected_resolution=expected_resolution,
            )
    except TypeError as error:
        if "unexpected keyword argument 'expected_resolution'" in str(error):
            _assert_delivery_probe(
                probe,
                expected_duration_seconds,
                orientation=orientation,
            )
            return
        if "unexpected keyword argument 'orientation'" not in str(error):
            raise
        assertion(expected_duration_seconds)


class RepairEngine:
    def __init__(
        self,
        toolchain: FfmpegToolchain,
        *,
        renderer: object,
        command_runner: CommandRunner = subprocess.run,
    ) -> None:
        self.toolchain = toolchain
        self.renderer = renderer
        self.command_runner = command_runner

    def repair(
        self,
        *,
        issues: list[object],
        report: dict[str, object],
        master: Path,
        clean: Path,
        subtitles: Path,
        timeline: Path,
        context: dict[str, object],
    ) -> list[str]:
        issue_names = {str(issue) for issue in issues}
        actions: list[str] = []
        needs_rerender = False
        if "blackdetect" in issue_names:
            if self._trim_boundary_black(report, master, clean):
                actions.append("trim_black_edges")
            else:
                needs_rerender = True
                actions.append("rerender_black_frames")
        if issue_names.intersection({"silencedetect", "loudness"}):
            audio = self._required_path(context, "audio_path")
            self._remix_audio(master, clean, audio)
            actions.append("remix_audio")
        if "subtitles" in issue_names:
            entries = _timeline_entries(timeline)
            orientation = str(context.get("orientation", "portrait"))
            subtitles.write_text(
                build_ass(
                    [
                        {"start_seconds": start, "end_seconds": end, "text": text}
                        for start, end, text in entries
                    ],
                    orientation=orientation,
                ),
                encoding="utf-8-sig",
            )
            needs_rerender = True
            actions.append("reburn_subtitles")
        if "ffprobe" in issue_names:
            needs_rerender = True
        if needs_rerender:
            render_kwargs = dict(
                visual_plan_path=self._required_path(context, "visual_plan_path"),
                audio_path=self._required_path(context, "audio_path"),
                subtitle_path=subtitles,
                output_path=master,
                title=str(context.get("title", "")),
            )
            orientation = str(context.get("orientation", "portrait"))
            render_kwargs["orientation"] = orientation
            self.renderer.render_and_validate(**render_kwargs)
            actions.append("rerender_m5")
        return actions

    @staticmethod
    def _required_path(context: dict[str, object], key: str) -> Path:
        value = context.get(key)
        if value is None or not Path(value).is_file():
            raise ValueError(f"修复缺少 {key}")
        return Path(value)

    def _remix_audio(self, master: Path, clean: Path, audio: Path) -> None:
        for video in (master, clean):
            pending = video.with_name(
                f".{video.stem}.remix-{uuid.uuid4().hex}{video.suffix}"
            )
            _run(
                self.command_runner,
                [
                    self.toolchain.ffmpeg,
                    "-y",
                    "-i",
                    str(video),
                    "-i",
                    str(audio),
                    "-map",
                    "0:v:0",
                    "-map",
                    "1:a:0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "aac",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-shortest",
                    str(pending),
                ],
            )
            atomic_replace(pending, video)

    def _trim_boundary_black(
        self,
        report: dict[str, object],
        master: Path,
        clean: Path,
    ) -> bool:
        checks = report.get("checks", {})
        black = checks.get("blackdetect", {}) if isinstance(checks, dict) else {}
        segments = black.get("segments", []) if isinstance(black, dict) else []
        if not isinstance(segments, list) or not segments:
            return False
        duration = 0.0
        ffprobe = checks.get("ffprobe", {}) if isinstance(checks, dict) else {}
        if isinstance(ffprobe, dict):
            master_probe = ffprobe.get("master", {})
            if isinstance(master_probe, dict):
                probe = master_probe.get("probe", {})
                if isinstance(probe, dict):
                    duration = float(probe.get("duration_seconds", 0))
        boundary_only = all(
            float(segment.get("start", 0)) <= 0.15
            or (duration > 0 and float(segment.get("end", 0)) >= duration - 0.15)
            for segment in segments
            if isinstance(segment, dict)
        )
        if not boundary_only or duration <= 0:
            return False
        start = max(
            (
                float(segment["end"])
                for segment in segments
                if isinstance(segment, dict) and float(segment.get("start", 0)) <= 0.15
            ),
            default=0.0,
        )
        end = min(
            (
                float(segment["start"])
                for segment in segments
                if isinstance(segment, dict)
                and float(segment.get("end", 0)) >= duration - 0.15
            ),
            default=duration,
        )
        if end <= start:
            return False
        for video in (master, clean):
            pending = video.with_name(
                f".{video.stem}.trim-{uuid.uuid4().hex}{video.suffix}"
            )
            _run(
                self.command_runner,
                [
                    self.toolchain.ffmpeg,
                    "-y",
                    "-ss",
                    f"{start:.3f}",
                    "-to",
                    f"{end:.3f}",
                    "-i",
                    str(video),
                    "-c",
                    "copy",
                    str(pending),
                ],
            )
            atomic_replace(pending, video)
        return True


class TechnicalQA:
    def __init__(
        self,
        toolchain: FfmpegToolchain,
        command_runner: CommandRunner = subprocess.run,
    ) -> None:
        self.toolchain = toolchain
        self.command_runner = command_runner

    def run(
        self,
        master_path: str | Path,
        clean_path: str | Path,
        subtitle_path: str | Path,
        timeline_path: str | Path,
        *,
        expected_duration_seconds: float,
        orientation: str = "portrait",
    ) -> dict[str, object]:
        master = Path(master_path)
        clean = Path(clean_path)
        subtitles = Path(subtitle_path)
        timeline = Path(timeline_path)
        probes = {
            "master": probe_media(self.toolchain.ffprobe, master, self.command_runner),
            "clean": probe_media(self.toolchain.ffprobe, clean, self.command_runner),
        }
        probe_checks: dict[str, object] = {}
        for name, probe in probes.items():
            errors: list[str] = []
            try:
                _assert_delivery_probe(
                    probe,
                    expected_duration_seconds,
                    orientation=orientation,
                )
            except ValueError as error:
                errors.append(str(error))
            probe_checks[name] = {
                "passed": not errors,
                "issues": errors,
                "probe": asdict(probe),
            }
        duration_delta = abs(
            probes["master"].duration_seconds - probes["clean"].duration_seconds
        )
        ffprobe_passed = (
            all(bool(check["passed"]) for check in probe_checks.values())
            and duration_delta <= 0.15
        )

        black = _run(
            self.command_runner,
            [
                self.toolchain.ffmpeg,
                "-hide_banner",
                "-i",
                str(master),
                "-vf",
                "blackdetect=d=0.50:pix_th=0.10",
                "-an",
                "-f",
                "null",
                "-",
            ],
        )
        black_segments = [
            {"start": float(start), "end": float(end), "duration": float(duration)}
            for start, end, duration in re.findall(
                r"black_start:([\d.]+)\s+black_end:([\d.]+)\s+black_duration:([\d.]+)",
                black.stderr,
            )
        ]
        silence = _run(
            self.command_runner,
            [
                self.toolchain.ffmpeg,
                "-hide_banner",
                "-i",
                str(master),
                "-af",
                "silencedetect=noise=-45dB:d=1.1",
                "-f",
                "null",
                "-",
            ],
        )
        silence_segments = [
            {"start": float(start), "end": float(end), "duration": float(duration)}
            for start, end, duration in re.findall(
                r"silence_start:\s*([\d.]+).*?silence_end:\s*([\d.]+)"
                r"\s*\|\s*silence_duration:\s*([\d.]+)",
                silence.stderr,
                re.DOTALL,
            )
        ]
        loudness = _run(
            self.command_runner,
            [
                self.toolchain.ffmpeg,
                "-hide_banner",
                "-i",
                str(master),
                "-af",
                "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json",
                "-f",
                "null",
                "-",
            ],
        )
        loudness_match = re.search(
            r'\{[^{}]*"input_i"[^{}]*\}',
            loudness.stderr,
            re.DOTALL,
        )
        loudness_data = json.loads(loudness_match.group(0)) if loudness_match else {}
        integrated_lufs = float(loudness_data.get("input_i", "-99"))
        true_peak_db = float(loudness_data.get("input_tp", "99"))
        loudness_ok = -18.0 <= integrated_lufs <= -14.0 and true_peak_db <= -1.0

        entries = _timeline_entries(timeline)
        timeline_overlaps = [
            {"previous_end": entries[index - 1][1], "next_start": entries[index][0]}
            for index in range(1, len(entries))
            if entries[index][0] < entries[index - 1][1] - 0.01
        ]
        timeline_gaps = [
            {"previous_end": entries[index - 1][1], "next_start": entries[index][0]}
            for index in range(1, len(entries))
            if entries[index][0] - entries[index - 1][1] > 0.5
        ]
        if entries and entries[0][0] > 0.5:
            timeline_gaps.insert(0, {"previous_end": 0.0, "next_start": entries[0][0]})
        if entries and expected_duration_seconds - entries[-1][1] > 0.5:
            timeline_gaps.append(
                {
                    "previous_end": entries[-1][1],
                    "next_start": expected_duration_seconds,
                }
            )
        timeline_ok = bool(entries) and not timeline_overlaps and not timeline_gaps

        cues = _ass_cues(subtitles)
        cue_overlaps = [
            {"previous_end": cues[index - 1][1], "next_start": cues[index][0]}
            for index in range(1, len(cues))
            if cues[index][0] < cues[index - 1][1] - 0.01
        ]
        event_count_matches = len(cues) == len(entries)
        event_timings_match = event_count_matches and all(
            abs(cue[0] - entry[0]) <= 0.15
            and abs(cue[1] - entry[1]) <= 0.15
            for cue, entry in zip(cues, entries)
        )
        safety = _ass_safety(subtitles, orientation=orientation)
        subtitle_ok = (
            bool(cues)
            and all(text for _, _, text in cues)
            and not cue_overlaps
            and event_count_matches
            and event_timings_match
            and bool(safety["passed"])
        )
        checks = {
            "ffprobe": {
                "passed": ffprobe_passed,
                **probe_checks,
                "duration_delta_seconds": duration_delta,
            },
            "blackdetect": {"passed": not black_segments, "segments": black_segments},
            "silencedetect": {
                "passed": not silence_segments,
                "segments": silence_segments,
            },
            "loudness": {
                "passed": loudness_ok,
                "integrated_lufs": integrated_lufs,
                "true_peak_db": true_peak_db,
                "lra": float(loudness_data.get("input_lra", 0)),
            },
            "timeline": {
                "passed": timeline_ok,
                "entry_count": len(entries),
                "gaps": timeline_gaps,
                "overlaps": timeline_overlaps,
            },
            "subtitles": {
                "passed": subtitle_ok,
                "cue_count": len(cues),
                "overlaps": cue_overlaps,
                "event_count_matches_timeline": event_count_matches,
                "event_timings_match_timeline": event_timings_match,
                "safe_zone_passed": safety["passed"],
                "style_safety": safety,
            },
        }
        issues = [name for name, check in checks.items() if not check["passed"]]
        return {"passed": not issues, "checks": checks, "issues": issues}


class M6Pipeline:
    def __init__(
        self,
        toolchain: FfmpegToolchain,
        *,
        technical_qa: object | None = None,
        command_runner: CommandRunner = subprocess.run,
        max_repair_rounds: int = 2,
        media_probe: Callable[..., object] = probe_media,
        repair_engine: object | None = None,
        directory_promoter: DirectoryPromoter | None = None,
        platform_exporter: PlatformExporter | None = None,
    ) -> None:
        self.toolchain = toolchain
        self.command_runner = command_runner
        self.technical_qa = technical_qa or TechnicalQA(toolchain, command_runner)
        self.max_repair_rounds = min(2, max(0, max_repair_rounds))
        self.media_probe = media_probe
        self.repair_engine = repair_engine
        self.directory_promoter = directory_promoter or DirectoryPromoter()
        self.platform_exporter = platform_exporter or PlatformExporter(
            toolchain,
            command_runner=command_runner,
        )

    def run(
        self,
        *,
        master_video: str | Path,
        clean_video: str | Path,
        subtitle_path: str | Path,
        timeline_path: str | Path,
        script: dict[str, object],
        package: dict[str, object],
        output_dir: str | Path,
        expected_duration_seconds: float,
        repair_context: dict[str, object] | None = None,
        selected_platforms: tuple[str, ...] | None = None,
        orientation: str = "portrait",
    ) -> dict[str, object]:
        if selected_platforms is not None and not selected_platforms:
            raise ValueError("至少选择一个导出平台")
        master = Path(master_video)
        clean = Path(clean_video)
        subtitles = Path(subtitle_path)
        timeline = Path(timeline_path)
        destination = Path(output_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        working = destination.parent / (
            f".{destination.name}.staging-{uuid.uuid4().hex}"
        )
        if working.exists():
            shutil.rmtree(working)
        working.mkdir(parents=True)
        qa_dir = working / "qa"

        repair_rounds = 0
        repair_attempts: list[dict[str, object]] = []
        repair_verified = False
        # 确保 repair_context 中包含 orientation，供修复引擎使用
        effective_repair_context = dict(repair_context or {})
        effective_repair_context.setdefault("orientation", orientation)
        technical = self.technical_qa.run(
            master,
            clean,
            subtitles,
            timeline,
            expected_duration_seconds=expected_duration_seconds,
            orientation=orientation,
        )
        _write_json(qa_dir / "technical_round_0.json", technical)
        while not technical["passed"] and repair_rounds < self.max_repair_rounds:
            previous_issues = list(technical.get("issues", []))
            if self.repair_engine is None:
                break
            before_hash = _artifact_hash([master, clean, subtitles, timeline])
            actions = self.repair_engine.repair(
                issues=previous_issues,
                report=technical,
                master=master,
                clean=clean,
                subtitles=subtitles,
                timeline=timeline,
                context=effective_repair_context,
            )
            if not actions:
                break
            repair_rounds += 1
            after_hash = _artifact_hash([master, clean, subtitles, timeline])
            hash_changed = before_hash != after_hash
            technical = self.technical_qa.run(
                master,
                clean,
                subtitles,
                timeline,
                expected_duration_seconds=expected_duration_seconds,
                orientation=orientation,
            )
            current_issues = list(technical.get("issues", []))
            issues_disappeared = not set(previous_issues).intersection(current_issues)
            repair_verified = bool(
                hash_changed and issues_disappeared and technical["passed"]
            )
            attempt = {
                "round": repair_rounds,
                "issues_before": previous_issues,
                "issues_after": current_issues,
                "hash_before": before_hash,
                "hash_after": after_hash,
                "hash_changed": hash_changed,
                "issues_disappeared": issues_disappeared,
                "verified": repair_verified,
                "actions": actions,
            }
            repair_attempts.append(attempt)
            _write_json(
                qa_dir / f"repair_round_{repair_rounds}.json",
                attempt,
            )
            _write_json(
                qa_dir / f"technical_round_{repair_rounds}.json",
                technical,
            )

        platforms = (
            ("youtube",)
            if selected_platforms is None and orientation == "landscape"
            else PORTRAIT_DELIVERY_PLATFORMS
            if selected_platforms is None
            else selected_platforms
        )
        content = self._content_qa(script, package, subtitles, platforms)
        _write_json(qa_dir / "content_qa.json", content)
        repair_ok = repair_rounds == 0 or repair_verified
        if not technical["passed"] or not content["passed"] or not repair_ok:
            manifest = {
                "status": "FAILED",
                "repair_status": "FAILED",
                "repair_rounds": repair_rounds,
                "repair_attempts": repair_attempts,
                "issues": list(technical.get("issues", []))
                + list(content.get("issues", [])),
                "recovery_command": "python -m aicf autopilot --job <JOB_ID>",
            }
            _write_json(working / "publish_manifest.json", manifest)
            if destination.exists():
                shutil.rmtree(working)
            else:
                self.directory_promoter.promote(working, destination)
            return manifest

        frame_count = self._create_visual_assets(
            master,
            clean,
            working,
            expected_duration_seconds,
            orientation=orientation,
        )
        shutil.copy2(master, working / "master.mp4")
        shutil.copy2(clean, working / "clean.mp4")
        preview_filename = (
            "preview_960x540.mp4" if orientation == "landscape" else "preview_540x960.mp4"
        )
        self._create_preview(
            master, working / preview_filename, orientation=orientation
        )
        platform_entries = self.platform_exporter.export(
            master,
            working,
            package,
            selected_platforms=platforms,
        )

        manifest = {
            "status": "READY_TO_PUBLISH",
            "repair_status": "AUTO_REPAIRED" if repair_rounds else "NOT_REQUIRED",
            "repair_rounds": repair_rounds,
            "repair_attempts": repair_attempts,
            "technical_qa": "qa/technical_round_%d.json" % repair_rounds,
            "content_qa": "qa/content_qa.json",
            "contact_sheet": "contact_sheet.jpg",
            "contact_sheet_frame_count": frame_count,
            "cover": "cover.jpg",
            "clean_cover": "clean_cover.jpg",
            "clean_video": "clean.mp4",
            "preview": preview_filename,
            "platforms": platform_entries,
            "expected_duration_seconds": expected_duration_seconds,
            "orientation": orientation,
        }
        manifest["files"] = self._file_inventory(working)
        _write_json(working / "publish_manifest.json", manifest)
        self.directory_promoter.promote(working, destination)
        return manifest

    @staticmethod
    def _file_inventory(destination: Path) -> dict[str, dict[str, object]]:
        media_suffixes = {".mp4", ".mov", ".mkv", ".webm"}
        return {
            path.relative_to(destination).as_posix(): {
                "sha256": _sha256_file(path),
                "size_bytes": path.stat().st_size,
                "media": path.suffix.lower() in media_suffixes,
            }
            for path in sorted(destination.rglob("*"))
            if path.is_file() and path.name != "publish_manifest.json"
        }

    def verify_delivery(self, output_dir: str | Path) -> list[str]:
        destination = Path(output_dir)
        manifest_path = destination / "publish_manifest.json"
        if not manifest_path.is_file():
            return ["publish_manifest.json 不存在"]
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            return [f"publish_manifest.json 无法读取: {error}"]
        if not isinstance(manifest, dict) or manifest.get("status") != "READY_TO_PUBLISH":
            return ["publish_manifest.json 状态不是 READY_TO_PUBLISH"]
        files = manifest.get("files")
        if not isinstance(files, dict) or not files:
            return ["publish_manifest.json 未列出交付文件"]
        issues: list[str] = []
        try:
            repair_rounds = int(manifest.get("repair_rounds", 0))
        except (TypeError, ValueError):
            repair_rounds = 0
            issues.append("publish_manifest.json repair_rounds 无效")
        orientation = str(manifest.get("orientation", "portrait"))
        preview_name = (
            "preview_960x540.mp4" if orientation == "landscape" else "preview_540x960.mp4"
        )
        required = {
            "master.mp4",
            "clean.mp4",
            preview_name,
            "cover.jpg",
            "clean_cover.jpg",
            "contact_sheet.jpg",
            "qa/content_qa.json",
            f"qa/technical_round_{repair_rounds}.json",
        }
        platforms = manifest.get("platforms", {})
        selected_platforms = (
            tuple(platforms)
            if isinstance(platforms, dict) and platforms
            else ("youtube",)
            if orientation == "landscape"
            else PORTRAIT_DELIVERY_PLATFORMS
        )
        for platform in selected_platforms:
            required.add(f"{platform}/video.mp4")
            required.add(f"{platform}/publish.md")
        listed = {str(relative) for relative in files}
        for relative in sorted(required - listed):
            issues.append(f"必需交付文件未列入 manifest: {relative}")
        actual = {
            path.relative_to(destination).as_posix()
            for path in destination.rglob("*")
            if path.is_file() and path.name != "publish_manifest.json"
        }
        if actual != listed:
            missing_actual = sorted(listed - actual)
            unlisted_actual = sorted(actual - listed)
            issues.append(
                "实际文件集合与 manifest 不一致"
                f"；缺失={missing_actual}；未列入={unlisted_actual}"
            )
        expected_duration = float(manifest.get("expected_duration_seconds", 0))
        for relative, raw_metadata in files.items():
            metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
            path = destination / str(relative)
            try:
                path.resolve().relative_to(destination.resolve())
            except ValueError:
                issues.append(f"{relative} 路径越界")
                continue
            if not path.is_file():
                issues.append(f"{relative} 不存在")
                continue
            if _sha256_file(path) != metadata.get("sha256"):
                issues.append(f"{relative} SHA256 不匹配")
                continue
            if path.stat().st_size != metadata.get("size_bytes"):
                issues.append(f"{relative} 文件大小不匹配")
            if metadata.get("media"):
                try:
                    probe = self.media_probe(
                        self.toolchain.ffprobe,
                        path,
                        self.command_runner,
                    )
                    if expected_duration > 0:
                        preview_resolution = (
                            (960, 540)
                            if str(relative) == "preview_960x540.mp4"
                            else (540, 960)
                            if str(relative) == "preview_540x960.mp4"
                            else None
                        )
                        _assert_delivery_probe(
                            probe,
                            expected_duration,
                            orientation=orientation,
                            expected_resolution=preview_resolution,
                        )
                except Exception as error:
                    issues.append(f"{relative} 媒体验证失败: {error}")
        return issues

    @staticmethod
    def _content_qa(
        script: dict[str, object],
        package: dict[str, object],
        subtitles: Path,
        selected_platforms: tuple[str, ...],
    ) -> dict[str, object]:
        issues: list[str] = []
        if not str(script.get("title", "")).strip():
            issues.append("脚本缺少标题")
        if not _ass_cues(subtitles):
            issues.append("字幕为空")
        for platform in selected_platforms:
            value = package.get(platform)
            if not isinstance(value, dict):
                issues.append(f"{platform} 缺少发布文案")
                continue
            if not str(value.get("title", "")).strip():
                issues.append(f"{platform} 缺少标题")
            if not str(value.get("description", "")).strip():
                issues.append(f"{platform} 缺少简介")
        return {"passed": not issues, "issues": issues}

    def _create_visual_assets(
        self,
        master: Path,
        clean: Path,
        destination: Path,
        duration: float,
        *,
        orientation: str = "portrait",
    ) -> int:
        sample_rate = 9.0 / max(duration, 0.1)
        frame_analysis = _run(
            self.command_runner,
            [
                self.toolchain.ffmpeg,
                "-hide_banner",
                "-i",
                str(master),
                "-vf",
                f"fps={sample_rate:.8f},blackframe=amount=0:threshold=32",
                "-an",
                "-f",
                "null",
                "-",
            ],
        )
        black_percentages = [
            float(value)
            for value in re.findall(r"\bpblack:\s*([\d.]+)", frame_analysis.stderr)
        ]
        if not 6 <= len(black_percentages) <= 9:
            raise ValueError(
                f"contact sheet 抽帧数量应为 6-9，实际 {len(black_percentages)}"
            )
        if any(value >= 98.0 for value in black_percentages):
            raise ValueError("contact sheet 包含黑帧")
        # contact sheet：竖屏每格 270x480、横屏每格 480x270，保持 3x3 网格
        if orientation == "landscape":
            sheet_filter = f"fps={sample_rate:.8f},scale=480:270,tile=3x3"
            cover_scale = "scale=1920:1080"
        else:
            sheet_filter = f"fps={sample_rate:.8f},scale=270:480,tile=3x3"
            cover_scale = "scale=1080:1920"
        _run(
            self.command_runner,
            [
                self.toolchain.ffmpeg,
                "-y",
                "-i",
                str(master),
                "-vf",
                sheet_filter,
                "-frames:v",
                "1",
                str(destination / "contact_sheet.jpg"),
            ],
        )
        for source, name in ((master, "cover.jpg"), (clean, "clean_cover.jpg")):
            _run(
                self.command_runner,
                [
                    self.toolchain.ffmpeg,
                    "-y",
                    "-ss",
                    f"{min(duration * 0.15, 1.0):.3f}",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-vf",
                    cover_scale,
                    str(destination / name),
                ],
            )
        return len(black_percentages)

    def _create_preview(
        self, master: Path, output: Path, *, orientation: str = "portrait"
    ) -> None:
        scale_filter = "scale=960:540" if orientation == "landscape" else "scale=540:960"
        _run(
            self.command_runner,
            [
                self.toolchain.ffmpeg,
                "-y",
                "-i",
                str(master),
                "-vf",
                scale_filter,
                "-c:v",
                "libx264",
                "-crf",
                "28",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                str(output),
            ],
        )

    @staticmethod
    def _publish_markdown(platform: str, metadata: object) -> str:
        value = metadata if isinstance(metadata, dict) else {}
        hashtags = value.get("hashtags", [])
        hashtag_text = " ".join(
            f"#{tag}" for tag in hashtags if isinstance(tag, str) and tag.strip()
        )
        return (
            f"# {value.get('title', '')}\n\n"
            f"{value.get('description', '')}\n\n"
            f"{hashtag_text}\n\n"
            f"平台：{platform}\n"
        )
