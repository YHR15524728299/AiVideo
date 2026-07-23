from __future__ import annotations

import json
from pathlib import Path

import pytest

from aicf.m5_runner import M5VisualPlanRunner
from aicf.models import VisualPlan


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def test_visual_plan_uses_audio_timeline_as_authoritative_segment_duration(
    tmp_path: Path,
) -> None:
    script = {
        "title": "AI视频生产",
        "hook": "为什么画面总是不稳定？",
        "call_to_action": "关注我。",
        "estimated_duration_seconds": 99,
        "segments": [
            {
                "segment_id": "SEG001",
                "purpose": "开场冲突",
                "narration": "第一段。",
                "visual_brief": "机械臂快速穿梭，数据流爆发",
            },
            {
                "segment_id": "SEG002",
                "purpose": "解释",
                "narration": "第二段。",
                "visual_brief": "编辑台上的分镜卡片",
            },
            {
                "segment_id": "SEG003",
                "purpose": "案例",
                "narration": "第三段。",
                "visual_brief": "镜头沿城市街道向前推进",
            },
            {
                "segment_id": "SEG004",
                "purpose": "结论",
                "narration": "第四段。",
                "visual_brief": "完整流程图和完成标记",
            },
        ],
    }
    timeline = [
        {
            "script_segment_id": "SEG001",
            "start_seconds": 0.0,
            "end_seconds": 1.0,
            "duration_seconds": 1.0,
        },
        {
            "script_segment_id": "SEG001",
            "start_seconds": 1.2,
            "end_seconds": 2.0,
            "duration_seconds": 0.8,
        },
        {
            "script_segment_id": "SEG002",
            "start_seconds": 2.3,
            "end_seconds": 4.0,
            "duration_seconds": 1.7,
        },
        {
            "script_segment_id": "SEG003",
            "start_seconds": 4.2,
            "end_seconds": 6.5,
            "duration_seconds": 2.3,
        },
        {
            "script_segment_id": "SEG004",
            "start_seconds": 6.8,
            "end_seconds": 8.0,
            "duration_seconds": 1.2,
        },
    ]
    script_path = tmp_path / "最终脚本.json"
    timeline_path = tmp_path / "音频" / "timeline.json"
    timeline_path.parent.mkdir()
    _write_json(script_path, script)
    _write_json(timeline_path, timeline)

    result = M5VisualPlanRunner().run(
        script_path=script_path,
        timeline_path=timeline_path,
        output_dir=tmp_path / "视觉计划",
        mode="balanced",
    )

    plan = VisualPlan.model_validate_json(result.visual_plan_path.read_text("utf-8"))
    assert plan.total_duration_seconds == pytest.approx(8.0)
    assert [shot.duration_seconds for shot in plan.shots] == pytest.approx(
        [2.3, 1.9, 2.6, 1.2]
    )
    assert [shot.start_seconds for shot in plan.shots] == pytest.approx(
        [0.0, 2.3, 4.2, 6.8]
    )
    assert [shot.asset_type for shot in plan.shots] == ["image"] * 4
    assert all(any("\u4e00" <= char <= "\u9fff" for char in shot.prompt) for shot in plan.shots)
    assert all("竖屏9:16" in shot.prompt and "无文字" in shot.prompt for shot in plan.shots)

    manifest = json.loads(result.asset_manifest_path.read_text(encoding="utf-8"))
    assert manifest["mode"] == "balanced"
    assert manifest["total_duration_seconds"] == pytest.approx(8.0)
    assert len(manifest["assets"]) == 4
    assert manifest["assets"][0]["expected_path"].endswith("VIS001.png")
    assert manifest["assets"][1]["expected_path"].endswith("VIS002.png")
    assert manifest["assets"][2]["expected_path"].endswith("VIS003.png")


def test_visual_plan_rejects_script_segment_missing_from_audio_timeline(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "script.json"
    timeline_path = tmp_path / "timeline.json"
    _write_json(
        script_path,
        {
            "title": "缺失测试",
            "hook": "钩子",
            "call_to_action": "行动",
            "estimated_duration_seconds": 10,
            "segments": [
                {
                    "segment_id": "SEG001",
                    "purpose": "解释",
                    "narration": "第一段",
                    "visual_brief": "画面一",
                },
                {
                    "segment_id": "SEG002",
                    "purpose": "结论",
                    "narration": "第二段",
                    "visual_brief": "画面二",
                },
            ],
        },
    )
    _write_json(
        timeline_path,
        [
            {
                "script_segment_id": "SEG001",
                "start_seconds": 0,
                "end_seconds": 2,
                "duration_seconds": 2,
            }
        ],
    )

    with pytest.raises(ValueError, match="SEG002.*音频时间线"):
        M5VisualPlanRunner().run(
            script_path=script_path,
            timeline_path=timeline_path,
            output_dir=tmp_path / "visual",
        )


def test_visual_plan_splits_long_segments_into_stable_continuous_subshots(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "script.json"
    timeline_path = tmp_path / "timeline.json"
    _write_json(
        script_path,
        {
            "title": "长镜头拆分",
            "hook": "钩子",
            "call_to_action": "行动",
            "estimated_duration_seconds": 42,
            "segments": [
                {
                    "segment_id": "SEG001",
                    "purpose": "动态演示",
                    "narration": "第一段",
                    "visual_brief": "机械臂快速穿梭",
                },
                {
                    "segment_id": "SEG002",
                    "purpose": "结论",
                    "narration": "第二段",
                    "visual_brief": "稳定的工作台",
                },
            ],
        },
    )
    _write_json(
        timeline_path,
        [
            {
                "script_segment_id": "SEG001",
                "start_seconds": 0,
                "end_seconds": 31,
                "duration_seconds": 31,
            },
            {
                "script_segment_id": "SEG002",
                "start_seconds": 31,
                "end_seconds": 42,
                "duration_seconds": 11,
            },
        ],
    )

    result = M5VisualPlanRunner().run(
        script_path=script_path,
        timeline_path=timeline_path,
        output_dir=tmp_path / "visual",
    )

    plan = VisualPlan.model_validate_json(result.visual_plan_path.read_text("utf-8"))
    assert [shot.shot_id for shot in plan.shots] == [
        "VIS001_01",
        "VIS001_02",
        "VIS001_03",
        "VIS002",
    ]
    assert [shot.start_seconds for shot in plan.shots] == pytest.approx(
        [0, 31 / 3, 62 / 3, 31]
    )
    assert [shot.duration_seconds for shot in plan.shots] == pytest.approx(
        [31 / 3, 31 / 3, 31 / 3, 11]
    )
    assert sum(shot.duration_seconds for shot in plan.shots[:3]) == pytest.approx(31)
    assert all(4 <= shot.duration_seconds <= 15 for shot in plan.shots)
    assert "连续镜头 1/3" in plan.shots[0].prompt
    assert "连续镜头 2/3" in plan.shots[1].prompt
    assert "连续镜头 3/3" in plan.shots[2].prompt
    assert plan.shots[3].shot_id == "VIS002"


def test_balanced_budget_selects_two_eligible_subshots_and_never_short_video(
    tmp_path: Path,
) -> None:
    script_path = tmp_path / "script.json"
    timeline_path = tmp_path / "timeline.json"
    _write_json(
        script_path,
        {
            "title": "动态预算",
            "hook": "钩子",
            "call_to_action": "行动",
            "estimated_duration_seconds": 24,
            "segments": [
                {
                    "segment_id": "SEG001",
                    "purpose": "快速运动",
                    "narration": "短段",
                    "visual_brief": "快速飞行",
                },
                {
                    "segment_id": "SEG002",
                    "purpose": "快速运动",
                    "narration": "长段",
                    "visual_brief": "机械臂快速穿梭爆发",
                },
            ],
        },
    )
    _write_json(
        timeline_path,
        [
            {
                "script_segment_id": "SEG001",
                "start_seconds": 0,
                "end_seconds": 3,
                "duration_seconds": 3,
            },
            {
                "script_segment_id": "SEG002",
                "start_seconds": 3,
                "end_seconds": 24,
                "duration_seconds": 21,
            },
        ],
    )

    result = M5VisualPlanRunner().run(
        script_path=script_path,
        timeline_path=timeline_path,
        output_dir=tmp_path / "visual",
    )

    plan = VisualPlan.model_validate_json(result.visual_plan_path.read_text("utf-8"))
    assert [shot.shot_id for shot in plan.shots] == [
        "VIS001",
        "VIS002_01",
        "VIS002_02",
    ]
    assert [shot.asset_type for shot in plan.shots] == ["image", "video", "video"]
    assert sum(shot.asset_type == "video" for shot in plan.shots) == 2
    assert all(
        shot.duration_seconds >= 4
        for shot in plan.shots
        if shot.asset_type == "video"
    )
