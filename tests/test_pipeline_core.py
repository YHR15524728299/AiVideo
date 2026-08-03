from __future__ import annotations

import json
import wave
from pathlib import Path

import pytest

from aicf.engines.clip_planner import (
    allocate_dynamic_scenes,
    choose_generation_duration,
    split_scene_duration,
)
from aicf.engines.subtitle_engine import build_srt, split_subtitle_text
from aicf.engines.timeline_engine import (
    AudioSegment,
    build_narration_timeline,
    probe_wav_duration,
)
from aicf.engines.topic_engine import rank_topics, select_topic
from aicf.providers.jimeng import JimengCapabilities, JimengCliAdapter
from aicf.providers.openrouter import extract_json_object


def _write_silent_wav(path: Path, duration: float, rate: int = 8000) -> None:
    frame_count = round(duration * rate)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(b"\0\0" * frame_count)


@pytest.mark.parametrize(
    ("required", "supported", "expected"),
    [
        (4.1, [5, 10, 15], 5.0),
        (6.7, [5, 10, 15], 10.0),
        (15.0, [5, 10, 15, 20], 15.0),
        (15.1, [5, 10, 15, 20], None),
        (7.0, [5], None),
    ],
)
def test_choose_generation_duration_uses_shortest_legal_value(
    required: float,
    supported: list[float],
    expected: float | None,
) -> None:
    assert choose_generation_duration(required, supported) == expected


def test_split_scene_duration_never_exceeds_hard_limit() -> None:
    assert split_scene_duration(17.0, hard_max_seconds=15.0) == [8.5, 8.5]
    assert split_scene_duration(30.1, hard_max_seconds=15.0) == pytest.approx(
        [10.033333, 10.033333, 10.033333],
        abs=1e-5,
    )


def test_dynamic_allocation_respects_balanced_count_and_coverage() -> None:
    scenes = [
        {"scene_id": f"SC{i:03}", "duration": 5.0, "motion_score": score}
        for i, score in enumerate([0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2], start=1)
    ]

    selected = allocate_dynamic_scenes(
        scenes,
        total_duration=40.0,
        target_ratio=0.40,
        min_clips=3,
        max_clips=5,
    )

    assert [item["scene_id"] for item in selected] == ["SC001", "SC002", "SC003", "SC004"]
    assert sum(item["duration"] for item in selected) >= 16.0


def test_wav_probe_and_narration_timeline_use_real_audio_duration(tmp_path: Path) -> None:
    first = tmp_path / "中文一.wav"
    second = tmp_path / "中文二.wav"
    _write_silent_wav(first, 1.25)
    _write_silent_wav(second, 0.75)

    timeline = build_narration_timeline(
        [
            AudioSegment("AUD001", "SEG001", "第一句", first),
            AudioSegment("AUD002", "SEG002", "第二句", second),
        ]
    )

    assert probe_wav_duration(first) == pytest.approx(1.25)
    assert timeline[0].start_seconds == 0.0
    assert timeline[0].end_seconds == pytest.approx(1.25)
    assert timeline[1].start_seconds == pytest.approx(1.25)
    assert timeline[1].end_seconds == pytest.approx(2.0)


def test_timeline_rejects_missing_or_empty_audio(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        probe_wav_duration(tmp_path / "missing.wav")
    empty = tmp_path / "empty.wav"
    empty.write_bytes(b"")
    with pytest.raises(ValueError, match="无效 WAV"):
        probe_wav_duration(empty)


def test_chinese_subtitles_split_naturally_and_follow_audio_timing() -> None:
    chunks = split_subtitle_text("AI视频不稳定，问题往往不只在模型，而在整个生产流程。", max_chars=12)
    assert all(1 <= len(chunk) <= 12 for chunk in chunks)
    assert "".join(chunks).replace("，", "").replace("。", "") == (
        "AI视频不稳定问题往往不只在模型而在整个生产流程"
    )

    srt = build_srt(
        [
            {
                "text": "第一句话",
                "start_seconds": 0.0,
                "end_seconds": 1.25,
            },
            {
                "text": "第二句话",
                "start_seconds": 1.25,
                "end_seconds": 2.0,
            },
        ]
    )
    assert "00:00:00,000 --> 00:00:01,250" in srt
    assert "00:00:01,250 --> 00:00:02,000" in srt


def test_openrouter_json_extraction_repairs_code_fence_and_surrounding_text() -> None:
    result = extract_json_object('结果如下：```json\n{"passed": true, "scores": {"hook": 90}}\n```')
    assert result == {"passed": True, "scores": {"hook": 90}}

    with pytest.raises(ValueError, match="JSON"):
        extract_json_object("模型没有返回结构化内容")


def test_topic_ranking_penalizes_risk_and_recent_duplicates() -> None:
    topics = [
        {
            "topic_id": "T1",
            "title": "AI视频为什么不稳定",
            "hook": "问题不只在模型",
            "core_claim": "工作流决定稳定性",
            "direction_relevance": 95,
            "hook_strength": 90,
            "visual_potential": 80,
            "novelty": 90,
            "evidence_availability": 80,
            "production_difficulty": 20,
            "fact_risk": 10,
        },
        {
            "topic_id": "T2",
            "title": "AI视频为什么不稳定？",
            "hook": "问题不只在模型",
            "core_claim": "工作流决定稳定性",
            "direction_relevance": 100,
            "hook_strength": 100,
            "visual_potential": 100,
            "novelty": 100,
            "evidence_availability": 100,
            "production_difficulty": 0,
            "fact_risk": 0,
        },
    ]
    ranked = rank_topics(
        topics,
        recent_history=[
            {
                "title": "AI视频为什么不稳定",
                "hook": "问题不只在模型",
                "core_claim": "工作流决定稳定性",
            }
        ],
    )

    assert ranked[0]["topic_id"] == "T1"
    assert ranked[1]["duplicate"] is True
    assert ranked[1]["overall_score"] == 0


def test_explicit_title_prioritizes_direction_fit_and_locks_title() -> None:
    ranked = [
        {
            "topic_id": "T004",
            "title": "美联储的观察清单",
            "direction_relevance": 98,
            "overall_score": 86.5,
            "duplicate": False,
        },
        {
            "topic_id": "T001",
            "title": "美联储鹰鸽大战",
            "direction_relevance": 100,
            "overall_score": 84.6,
            "duplicate": False,
        },
    ]

    selected = select_topic(
        ranked,
        "视频标题必须为《美联储，为什么有人主张加息？2026.7.31》。",
    )

    assert selected["topic_id"] == "T001"
    assert selected["title"] == "美联储，为什么有人主张加息？2026.7.31"


def test_without_explicit_title_keeps_ranked_first_topic() -> None:
    ranked = [
        {"topic_id": "T004", "title": "观察清单", "overall_score": 90},
        {"topic_id": "T001", "title": "政策分歧", "overall_score": 80},
    ]

    assert select_topic(ranked, "制作一条美联储视频")["topic_id"] == "T004"


def test_jimeng_adapter_builds_argument_list_and_never_exceeds_fifteen_seconds(
    tmp_path: Path,
) -> None:
    adapter = JimengCliAdapter(
        executable="dreamina",
        capabilities=JimengCapabilities(
            image_command=[
                "text2image",
                "--prompt",
                "{prompt}",
                "--ratio",
                "{ratio}",
                "--model_version",
                "{model}",
                "--poll",
                "0",
            ],
            video_command=[
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
            ],
            supported_durations=list(range(4, 16)),
        ),
    )
    output = tmp_path / "中文目录" / "镜头.mp4"

    command = adapter.build_video_command("中文 提示词 & 安全", 6.7, output)

    assert command[0] == "dreamina"
    assert command[1] == "text2video"
    assert command[command.index("--duration") + 1] == "7"
    assert command[command.index("--ratio") + 1] == "9:16"
    assert "中文 提示词 & 安全" in command
    assert str(output) not in command
    assert json.dumps(command, ensure_ascii=False)

    with pytest.raises(ValueError, match="4-15"):
        adapter.build_video_command("prompt", 16.0, output)
