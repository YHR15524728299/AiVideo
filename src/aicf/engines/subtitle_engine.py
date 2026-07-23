from __future__ import annotations

import re
from collections.abc import Mapping


def split_subtitle_text(text: str, max_chars: int = 18) -> list[str]:
    if max_chars < 1:
        raise ValueError("max_chars 必须大于 0")
    normalized = re.sub(r"\s+", "", text).strip()
    if not normalized:
        return []
    phrases = [item for item in re.split(r"(?<=[，。！？；,.!?;])", normalized) if item]
    chunks: list[str] = []
    for phrase in phrases:
        while len(phrase) > max_chars:
            chunks.append(phrase[:max_chars].rstrip("，。！？；,.!?;"))
            phrase = phrase[max_chars:]
        if not phrase:
            continue
        if chunks and len(chunks[-1]) + len(phrase) <= max_chars:
            chunks[-1] += phrase
        else:
            chunks.append(phrase)
    return [chunk.strip("，。！？；,.!?;") for chunk in chunks if chunk.strip("，。！？；,.!?;")]


def _srt_timestamp(seconds: float) -> str:
    milliseconds = round(max(0.0, seconds) * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{whole_seconds:02},{milliseconds:03}"


def build_srt(entries: list[Mapping[str, object]]) -> str:
    blocks: list[str] = []
    for index, entry in enumerate(entries, start=1):
        start = float(entry["start_seconds"])
        end = float(entry["end_seconds"])
        if end <= start:
            raise ValueError("字幕结束时间必须晚于开始时间")
        blocks.append(
            f"{index}\n{_srt_timestamp(start)} --> {_srt_timestamp(end)}\n"
            f"{str(entry['text']).strip()}"
        )
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _ass_timestamp(seconds: float) -> str:
    centiseconds = round(max(0.0, seconds) * 100)
    hours, remainder = divmod(centiseconds, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    whole_seconds, centiseconds = divmod(remainder, 100)
    return f"{hours}:{minutes:02}:{whole_seconds:02}.{centiseconds:02}"


def build_ass(entries: list[Mapping[str, object]]) -> str:
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Alignment, MarginL, MarginR, MarginV, Outline, Shadow
Style: Default,Microsoft YaHei,64,&H00FFFFFF,&H00101010,&H80000000,-1,2,80,80,180,3,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    dialogues: list[str] = []
    for entry in entries:
        start = float(entry["start_seconds"])
        end = float(entry["end_seconds"])
        if end <= start:
            raise ValueError("字幕结束时间必须晚于开始时间")
        text = str(entry["text"]).strip().replace("\n", r"\N").replace(",", "，")
        dialogues.append(
            f"Dialogue: 0,{_ass_timestamp(start)},{_ass_timestamp(end)},"
            f"Default,,0,0,0,,{text}"
        )
    return header + "\n".join(dialogues) + ("\n" if dialogues else "")
