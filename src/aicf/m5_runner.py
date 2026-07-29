from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from aicf.models.contracts import ScriptResult, VisualPlan, VisualShot


@dataclass(frozen=True)
class VisualPlanResult:
    visual_plan_path: Path
    asset_manifest_path: Path


class M5VisualPlanRunner:
    _MOTION_TERMS = (
        "快速",
        "穿梭",
        "爆发",
        "推进",
        "运动",
        "追踪",
        "旋转",
        "飞行",
        "流动",
        "延时",
    )

    def run(
        self,
        *,
        script_path: str | Path,
        timeline_path: str | Path,
        output_dir: str | Path,
        mode: Literal["balanced"] = "balanced",
        orientation: Literal["portrait", "landscape"] = "portrait",
    ) -> VisualPlanResult:
        script = ScriptResult.model_validate(self._read_json(Path(script_path)))
        timeline = self._read_json(Path(timeline_path))
        if not isinstance(timeline, list) or not timeline:
            raise ValueError("音频时间线必须是非空数组")

        timings = self._allocate_segment_timings(script, timeline)
        shot_specs: list[dict[str, object]] = []
        for index, segment in enumerate(script.segments, start=1):
            start, duration = timings[segment.segment_id]
            part_count = max(1, math.ceil(duration / 15))
            part_duration = duration / part_count
            for part_index in range(1, part_count + 1):
                part_start = start + part_duration * (part_index - 1)
                current_duration = (
                    start + duration - part_start
                    if part_index == part_count
                    else part_duration
                )
                shot_specs.append(
                    {
                        "shot_id": (
                            f"VIS{index:03d}"
                            if part_count == 1
                            else f"VIS{index:03d}_{part_index:02d}"
                        ),
                        "segment": segment,
                        "part_index": part_index,
                        "part_count": part_count,
                        "start": part_start,
                        "duration": current_duration,
                        "order": len(shot_specs),
                    }
                )

        eligible = [
            spec for spec in shot_specs if float(spec["duration"]) >= 4
        ]
        ranked = sorted(
            eligible,
            key=lambda spec: (
                self._motion_score(
                    f"{spec['segment'].purpose} {spec['segment'].visual_brief}"
                ),
                -int(spec["order"]),
            ),
            reverse=True,
        )
        dynamic_ids = {
            str(spec["shot_id"]) for spec in ranked[: min(2, len(ranked))]
        }

        shots: list[VisualShot] = []
        for spec in shot_specs:
            segment = spec["segment"]
            shot_id = str(spec["shot_id"])
            asset_type: Literal["image", "video"] = (
                "video" if shot_id in dynamic_ids else "image"
            )
            suffix = ".mp4" if asset_type == "video" else ".png"
            shots.append(
                VisualShot(
                    shot_id=shot_id,
                    script_segment_id=segment.segment_id,
                    asset_type=asset_type,
                    prompt=self._build_prompt(
                        segment.visual_brief,
                        dynamic=asset_type == "video",
                        sequence=(
                            (int(spec["part_index"]), int(spec["part_count"]))
                            if int(spec["part_count"]) > 1
                            else None
                        ),
                        orientation=orientation,
                    ),
                    expected_path=f"assets/{shot_id}{suffix}",
                    start_seconds=float(spec["start"]),
                    duration_seconds=float(spec["duration"]),
                )
            )

        plan = VisualPlan(
            title=script.title,
            mode=mode,
            total_duration_seconds=shots[-1].end_seconds,
            shots=shots,
        )
        root = Path(output_dir)
        root.mkdir(parents=True, exist_ok=True)
        visual_plan_path = root / "visual_plan.json"
        asset_manifest_path = root / "asset_manifest.json"
        self._write_json(visual_plan_path, plan.model_dump(mode="json"))
        self._write_json(
            asset_manifest_path,
            {
                "mode": mode,
                "total_duration_seconds": plan.total_duration_seconds,
                "assets": [
                    {
                        "shot_id": shot.shot_id,
                        "script_segment_id": shot.script_segment_id,
                        "type": shot.asset_type,
                        "prompt": shot.prompt,
                        "expected_path": shot.expected_path,
                        "authoritative_duration_seconds": shot.duration_seconds,
                    }
                    for shot in shots
                ],
            },
        )
        return VisualPlanResult(visual_plan_path, asset_manifest_path)

    @staticmethod
    def _allocate_segment_timings(
        script: ScriptResult,
        timeline: list[object],
    ) -> dict[str, tuple[float, float]]:
        starts: dict[str, float] = {}
        ends: dict[str, float] = {}
        for raw in timeline:
            if not isinstance(raw, dict):
                raise ValueError("音频时间线条目必须是对象")
            segment_id = str(raw.get("script_segment_id", "")).strip()
            if not segment_id:
                raise ValueError("音频时间线缺少 script_segment_id")
            start = float(raw["start_seconds"])
            end = float(raw["end_seconds"])
            if end <= start:
                raise ValueError(f"{segment_id} 的音频时间非法")
            starts[segment_id] = min(starts.get(segment_id, start), start)
            ends[segment_id] = max(ends.get(segment_id, end), end)

        segment_ids = [segment.segment_id for segment in script.segments]
        for segment_id in segment_ids:
            if segment_id not in starts:
                raise ValueError(f"{segment_id} 不存在于音频时间线")
        unexpected = set(starts) - set(segment_ids)
        if unexpected:
            raise ValueError(f"音频时间线包含未知脚本段落: {sorted(unexpected)}")

        result: dict[str, tuple[float, float]] = {}
        for index, segment_id in enumerate(segment_ids):
            start = starts[segment_id]
            end = (
                starts[segment_ids[index + 1]]
                if index + 1 < len(segment_ids)
                else ends[segment_id]
            )
            if end <= start:
                raise ValueError(f"{segment_id} 无法分配有效权威时长")
            result[segment_id] = (start, end - start)
        return result

    @classmethod
    def _motion_score(cls, text: str) -> int:
        return sum(term in text for term in cls._MOTION_TERMS)

    @staticmethod
    def _build_prompt(
        visual_brief: str,
        *,
        dynamic: bool,
        sequence: tuple[int, int] | None = None,
        orientation: str = "portrait",
    ) -> str:
        motion = (
            "连续动态镜头，主体运动明确，镜头运动流畅"
            if dynamic
            else "高细节静态主视觉，主体清晰，适合轻微推近"
        )
        sequence_note = (
            f"；连续镜头 {sequence[0]}/{sequence[1]}，"
            "与同组前后镜头保持主体、场景、光线和运动方向一致"
            if sequence
            else ""
        )
        orientation_text = (
            "横屏16:9，主体位于安全区域，无文字，无水印，无标志。"
            if orientation == "landscape"
            else "竖屏9:16，主体位于安全区域，无文字，无水印，无标志。"
        )
        return (
            f"{visual_brief}{sequence_note}；{motion}；电影级光影，真实材质，层次丰富，"
            f"{orientation_text}"
        )

    @staticmethod
    def _read_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8-sig"))

    @staticmethod
    def _write_json(path: Path, value: object) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
