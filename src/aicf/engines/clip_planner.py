from __future__ import annotations

import math
from collections.abc import Iterable, Mapping


def choose_generation_duration(
    required_seconds: float,
    detected_supported_durations: list[float],
    hard_max_seconds: float = 15.0,
) -> float | None:
    if required_seconds <= 0:
        raise ValueError("镜头时长必须大于 0")
    legal = sorted(
        {
            float(duration)
            for duration in detected_supported_durations
            if 0 < float(duration) <= hard_max_seconds
        }
    )
    return next((duration for duration in legal if duration >= required_seconds), None)


def split_scene_duration(
    required_seconds: float,
    hard_max_seconds: float = 15.0,
) -> list[float]:
    if required_seconds <= 0 or hard_max_seconds <= 0:
        raise ValueError("时长必须大于 0")
    part_count = math.ceil(required_seconds / hard_max_seconds)
    part_duration = required_seconds / part_count
    return [part_duration] * part_count


def allocate_dynamic_scenes(
    scenes: Iterable[Mapping[str, object]],
    *,
    total_duration: float,
    target_ratio: float,
    min_clips: int,
    max_clips: int,
) -> list[dict[str, object]]:
    if not 0 <= target_ratio <= 1:
        raise ValueError("动态覆盖率必须位于 0 到 1")
    if not 0 <= min_clips <= max_clips:
        raise ValueError("动态镜头数量边界非法")
    ranked = sorted(
        (dict(scene) for scene in scenes),
        key=lambda scene: float(scene.get("motion_score", 0)),
        reverse=True,
    )
    target_seconds = total_duration * target_ratio
    selected: list[dict[str, object]] = []
    selected_seconds = 0.0
    for scene in ranked:
        if len(selected) >= max_clips:
            break
        if len(selected) >= min_clips and selected_seconds >= target_seconds:
            break
        selected.append(scene)
        selected_seconds += float(scene.get("duration", 0))
    return selected
