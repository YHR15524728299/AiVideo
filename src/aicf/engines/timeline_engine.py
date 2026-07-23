from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AudioSegment:
    audio_segment_id: str
    script_segment_id: str
    text: str
    path: Path


@dataclass(frozen=True)
class TimelineEntry:
    audio_segment_id: str
    script_segment_id: str
    text: str
    path: str
    start_seconds: float
    end_seconds: float
    duration_seconds: float


def probe_wav_duration(path: str | Path) -> float:
    audio_path = Path(path)
    if not audio_path.exists():
        raise FileNotFoundError(audio_path)
    try:
        with wave.open(str(audio_path), "rb") as audio:
            frame_rate = audio.getframerate()
            frame_count = audio.getnframes()
    except (EOFError, wave.Error) as error:
        raise ValueError(f"无效 WAV: {audio_path}") from error
    if frame_rate <= 0 or frame_count <= 0:
        raise ValueError(f"无效 WAV: {audio_path}")
    return frame_count / frame_rate


def build_narration_timeline(segments: list[AudioSegment]) -> list[TimelineEntry]:
    current = 0.0
    entries: list[TimelineEntry] = []
    for segment in segments:
        duration = probe_wav_duration(segment.path)
        end = current + duration
        entries.append(
            TimelineEntry(
                audio_segment_id=segment.audio_segment_id,
                script_segment_id=segment.script_segment_id,
                text=segment.text,
                path=str(segment.path),
                start_seconds=current,
                end_seconds=end,
                duration_seconds=duration,
            )
        )
        current = end
    return entries
