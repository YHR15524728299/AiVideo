from __future__ import annotations

import json
import re
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from aicf.atomic_io import atomic_replace
from aicf.engines.subtitle_engine import build_ass, build_srt
from aicf.providers.tts import FfmpegToolchain, TtsService


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


class NeedsScriptDurationRevision(RuntimeError):
    """合成音频无法在安全变速范围内满足脚本时长约束。"""

    def __init__(
        self,
        *,
        actual_duration_seconds: float,
        min_duration_seconds: float,
        max_duration_seconds: float,
        target_duration_seconds: float,
        detail: str = "",
    ) -> None:
        self.actual_duration_seconds = float(actual_duration_seconds)
        self.min_duration_seconds = float(min_duration_seconds)
        self.max_duration_seconds = float(max_duration_seconds)
        self.target_duration_seconds = float(target_duration_seconds)
        self.suggested_action = (
            "expand"
            if self.actual_duration_seconds < self.min_duration_seconds
            else "compress"
        )
        self.target_ratio = (
            self.target_duration_seconds / self.actual_duration_seconds
        )
        self.revision_instruction = (
            f"{self.suggested_action} script narration to approximately "
            f"{self.target_ratio:.3f}x its current spoken length; "
            f"actual={self.actual_duration_seconds:.3f}s, "
            f"min={self.min_duration_seconds:.3f}s, "
            f"max={self.max_duration_seconds:.3f}s, "
            f"target={self.target_duration_seconds:.3f}s"
        )
        message = detail.strip() or self.revision_instruction
        super().__init__(message)


@dataclass(frozen=True)
class NarrationResult:
    voiceover_path: Path
    timeline_path: Path
    srt_path: Path
    ass_path: Path
    segment_paths: tuple[Path, ...]


def split_chinese_sentences(text: str) -> list[tuple[str, float]]:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []
    pause_by_mark = {
        "，": 0.18,
        ",": 0.18,
        "；": 0.25,
        ";": 0.25,
        "。": 0.35,
        "！": 0.35,
        "？": 0.35,
        ".": 0.35,
        "!": 0.35,
        "?": 0.35,
    }
    chunks = re.findall(r".+?[，,；;。！？.!?](?=\s*|$)|.+$", normalized)
    return [(chunk.strip(), pause_by_mark.get(chunk.strip()[-1], 0.35)) for chunk in chunks]


def _script_segments(script: Mapping[str, Any] | Any) -> list[Mapping[str, Any]]:
    payload = script.model_dump(mode="json") if hasattr(script, "model_dump") else script
    segments = payload.get("segments") if isinstance(payload, Mapping) else None
    if not isinstance(segments, list) or not segments:
        raise ValueError("script.segments 必须是非空数组")
    return segments


def _wav_details(path: Path) -> tuple[int, int, int, bytes]:
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frames = source.readframes(source.getnframes())
    except (EOFError, wave.Error) as error:
        raise ValueError(f"无效 WAV: {path}") from error
    if (sample_rate, channels, sample_width) != (48_000, 2, 2):
        raise ValueError(
            f"音频参数不合规: {path}，要求 48000Hz/双声道/PCM s16le"
        )
    return sample_rate, channels, sample_width, frames


def _wav_duration(path: Path) -> float:
    sample_rate, channels, sample_width, frames = _wav_details(path)
    return len(frames) / (sample_rate * channels * sample_width)


def _probe_duration(
    ffprobe: str,
    path: Path,
    command_runner: CommandRunner,
) -> float:
    completed = command_runner(
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        duration = float(completed.stdout.strip())
    except ValueError as error:
        raise RuntimeError(f"ffprobe 未返回有效时长: {path}") from error
    if duration <= 0:
        raise RuntimeError(f"ffprobe 返回非正时长: {path}")
    return duration


def _extract_loudnorm_measurement(stderr: str) -> dict[str, str]:
    matches = re.findall(r"\{[\s\S]*?\}", stderr)
    for raw in reversed(matches):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        required = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
        if required.issubset(payload):
            return {key: str(payload[key]) for key in required}
    raise RuntimeError("FFmpeg loudnorm 第一遍未返回测量 JSON")


class NarrationPipeline:
    def __init__(
        self,
        service: TtsService,
        toolchain: FfmpegToolchain,
        command_runner: CommandRunner = subprocess.run,
    ) -> None:
        self.service = service
        self.toolchain = toolchain
        self._command_runner = command_runner

    def batch_synthesize(
        self,
        script: Mapping[str, Any] | Any,
        output_dir: str | Path,
        *,
        target_duration_seconds: float,
        min_duration_seconds: float,
        max_duration_seconds: float,
    ) -> NarrationResult:
        if not (
            0
            < min_duration_seconds
            <= target_duration_seconds
            <= max_duration_seconds
        ):
            raise ValueError("时长约束必须满足 0 < min <= target <= max")
        root = Path(output_dir)
        segments_dir = root / "segments"
        segments_dir.mkdir(parents=True, exist_ok=True)
        for stale in segments_dir.glob("*.wav"):
            stale.unlink()

        sentence_units: list[dict[str, Any]] = []
        for source in _script_segments(script):
            script_segment_id = str(source.get("segment_id", "")).strip()
            narration = str(source.get("narration", "")).strip()
            if not script_segment_id or not narration:
                raise ValueError("每个脚本段必须包含 segment_id 与 narration")
            for text, _pause_seconds in split_chinese_sentences(narration):
                sentence_units.append(
                    {
                        "script_segment_id": script_segment_id,
                        "text": text,
                    }
                )
        if not sentence_units:
            raise ValueError("脚本没有可合成的中文句子")

        target = segments_dir / "AUD001_full_narration.wav"
        full_text = "".join(str(item["text"]) for item in sentence_units)
        result = self.service.synthesize(full_text, target)
        source_duration = _wav_duration(target)
        total_weight = sum(max(1, len(str(item["text"]))) for item in sentence_units)

        work: list[dict[str, Any]] = []
        allocated = 0.0
        for index, item in enumerate(sentence_units):
            duration = (
                source_duration - allocated
                if index == len(sentence_units) - 1
                else source_duration
                * max(1, len(str(item["text"])))
                / total_weight
            )
            allocated += duration
            work.append(
                {
                    **item,
                    "audio_segment_id": f"AUD{index + 1:03}",
                    "path": str(target),
                    "duration_seconds": duration,
                    "pause_after_seconds": 0.0,
                    "provider": result.provider,
                    "degraded": result.degraded,
                }
            )

        timeline: list[dict[str, Any]] = []
        current = 0.0
        for item in work:
            duration = float(item["duration_seconds"])
            entry = dict(item)
            entry["start_seconds"] = current
            entry["end_seconds"] = current + duration
            timeline.append(entry)
            current += duration

        root.mkdir(parents=True, exist_ok=True)
        pre_normalized = root / ".voiceover.pre.wav"
        shutil.copy2(target, pre_normalized)

        voiceover = root / "voiceover.wav"
        measurement_output = root / ".voiceover.measure.wav"
        first_filter = "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json"
        first = self._command_runner(
            [
                self.toolchain.ffmpeg,
                "-y",
                "-hide_banner",
                "-i",
                str(pre_normalized),
                "-af",
                first_filter,
                "-ar",
                "48000",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                str(measurement_output),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        measured = _extract_loudnorm_measurement(first.stderr)
        second_filter = (
            "loudnorm=I=-16:TP=-1.5:LRA=11:linear=true:"
            f"measured_I={measured['input_i']}:"
            f"measured_TP={measured['input_tp']}:"
            f"measured_LRA={measured['input_lra']}:"
            f"measured_thresh={measured['input_thresh']}:"
            f"offset={measured['target_offset']}"
        )
        self._command_runner(
            [
                self.toolchain.ffmpeg,
                "-y",
                "-hide_banner",
                "-i",
                str(pre_normalized),
                "-af",
                second_filter,
                "-ar",
                "48000",
                "-ac",
                "2",
                "-c:a",
                "pcm_s16le",
                str(voiceover),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        pre_normalized.unlink(missing_ok=True)
        measurement_output.unlink(missing_ok=True)

        probed_duration = _probe_duration(
            self.toolchain.ffprobe,
            voiceover,
            self._command_runner,
        )
        timeline_scale = probed_duration / current
        if probed_duration < min_duration_seconds:
            raise NeedsScriptDurationRevision(
                actual_duration_seconds=probed_duration,
                min_duration_seconds=min_duration_seconds,
                max_duration_seconds=max_duration_seconds,
                target_duration_seconds=target_duration_seconds,
                detail=(
                    f"合成音频 {probed_duration:.3f}s 低于最小时长 "
                    f"{min_duration_seconds:.3f}s"
                ),
            )
        if probed_duration > max_duration_seconds:
            target_factor = probed_duration / target_duration_seconds
            if target_factor <= 1.35:
                desired_duration = target_duration_seconds
                atempo = target_factor
            else:
                max_factor = probed_duration / max_duration_seconds
                if max_factor > 1.35:
                    raise NeedsScriptDurationRevision(
                        actual_duration_seconds=probed_duration,
                        min_duration_seconds=min_duration_seconds,
                        max_duration_seconds=max_duration_seconds,
                        target_duration_seconds=target_duration_seconds,
                        detail=(
                            f"所需 atempo={max_factor:.6f} "
                            "超出安全范围 1.0-1.35"
                        ),
                    )
                desired_duration = max_duration_seconds
                atempo = max_factor

            compressed = root / ".voiceover.atempo.wav"
            self._command_runner(
                [
                    self.toolchain.ffmpeg,
                    "-y",
                    "-hide_banner",
                    "-i",
                    str(voiceover),
                    "-af",
                    f"atempo={atempo:.6f}",
                    "-ar",
                    "48000",
                    "-ac",
                    "2",
                    "-c:a",
                    "pcm_s16le",
                    str(compressed),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            atomic_replace(compressed, voiceover)
            final_duration = _probe_duration(
                self.toolchain.ffprobe,
                voiceover,
                self._command_runner,
            )
            if not min_duration_seconds <= final_duration <= max_duration_seconds:
                raise NeedsScriptDurationRevision(
                    actual_duration_seconds=final_duration,
                    min_duration_seconds=min_duration_seconds,
                    max_duration_seconds=max_duration_seconds,
                    target_duration_seconds=target_duration_seconds,
                    detail=(
                        f"变速后真实时长 {final_duration:.3f}s 未落入 "
                        f"{min_duration_seconds:.3f}-"
                        f"{max_duration_seconds:.3f}s"
                    ),
                )
            timeline_scale *= final_duration / probed_duration
            if abs(final_duration - desired_duration) > 0.15:
                raise NeedsScriptDurationRevision(
                    actual_duration_seconds=final_duration,
                    min_duration_seconds=min_duration_seconds,
                    max_duration_seconds=max_duration_seconds,
                    target_duration_seconds=target_duration_seconds,
                    detail=(
                        f"变速后真实时长 {final_duration:.3f}s 偏离目标 "
                        f"{desired_duration:.3f}s"
                    ),
                )

        if timeline_scale != 1.0:
            for item in timeline:
                for key in (
                    "duration_seconds",
                    "pause_after_seconds",
                    "start_seconds",
                    "end_seconds",
                ):
                    item[key] = float(item[key]) * timeline_scale

        timeline_path = root / "timeline.json"
        timeline_path.write_text(
            json.dumps(timeline, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        subtitle_entries = [
            {
                "text": item["text"],
                "start_seconds": item["start_seconds"],
                "end_seconds": item["end_seconds"],
            }
            for item in timeline
        ]
        srt_path = root / "subtitles.srt"
        ass_path = root / "subtitles.ass"
        srt_path.write_text(build_srt(subtitle_entries), encoding="utf-8-sig")
        ass_path.write_text(build_ass(subtitle_entries), encoding="utf-8-sig")
        return NarrationResult(
            voiceover_path=voiceover,
            timeline_path=timeline_path,
            srt_path=srt_path,
            ass_path=ass_path,
            segment_paths=(target,),
        )
