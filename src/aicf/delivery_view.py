from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .artifact_commit import DirectoryPromoter
from .atomic_io import atomic_replace


FINAL_FILE_NAMES = {
    "最终视频.mp4",
    "无字幕视频.mp4",
    "封面.jpg",
    "发布文案.md",
    "验收摘要.json",
}


@dataclass(frozen=True)
class UserDelivery:
    output_dir: Path
    final_video: Path
    clean_video: Path
    cover: Path
    publish_copy: Path
    acceptance_summary: Path


class RelocatableRepository(Protocol):
    def relocate_output_dir(self, job_id: str, output_dir: str | Path): ...


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def finalize_user_delivery(
    job_dir: str | Path,
    output_dir: str | Path,
    *,
    promoter: DirectoryPromoter | None = None,
) -> UserDelivery:
    internal = Path(job_dir)
    delivery = internal / "delivery"
    manifest_path = delivery / "publish_manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"内部发布清单不存在: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if manifest.get("status") != "READY_TO_PUBLISH":
        raise ValueError("内部发布清单尚未达到 READY_TO_PUBLISH")

    platforms = manifest.get("platforms")
    if not isinstance(platforms, dict) or not platforms:
        raise ValueError("内部发布清单没有平台视频")
    platform = "youtube" if "youtube" in platforms else next(iter(platforms))
    platform_entry = platforms[platform]
    if not isinstance(platform_entry, dict):
        raise ValueError(f"{platform} 平台清单无效")
    video_relative = str(
        platform_entry.get("video") or f"{platform}/video.mp4"
    )
    copy_relative = str(
        platform_entry.get("copy") or f"{platform}/publish.md"
    )
    sources = {
        "最终视频.mp4": delivery / video_relative,
        "无字幕视频.mp4": delivery / str(manifest.get("clean_video", "clean.mp4")),
        "封面.jpg": delivery / str(manifest.get("cover", "cover.jpg")),
        "发布文案.md": delivery / copy_relative,
    }
    missing = [str(path) for path in sources.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("用户交付源文件不存在: " + "、".join(missing))

    destination = Path(output_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged = destination.parent / f".{destination.name}.staged-{uuid.uuid4().hex}"
    staged.mkdir(parents=True)
    try:
        for name, source in sources.items():
            shutil.copy2(source, staged / name)
        final_hash = _sha256(staged / "最终视频.mp4")
        summary = {
            "status": "READY_TO_PUBLISH",
            "job_id": internal.name,
            "platform": platform,
            "orientation": manifest.get("orientation"),
            "duration_seconds": manifest.get("expected_duration_seconds"),
            "visual_mode": manifest.get("visual_mode", "generated_media"),
            "final_video_sha256": final_hash,
            "internal_manifest_sha256": _sha256(manifest_path),
            "files": sorted(FINAL_FILE_NAMES),
        }
        (staged / "验收摘要.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (promoter or DirectoryPromoter()).promote(staged, destination)
    except BaseException:
        if staged.exists():
            shutil.rmtree(staged)
        raise

    return UserDelivery(
        output_dir=destination,
        final_video=destination / "最终视频.mp4",
        clean_video=destination / "无字幕视频.mp4",
        cover=destination / "封面.jpg",
        publish_copy=destination / "发布文案.md",
        acceptance_summary=destination / "验收摘要.json",
    )


def migrate_legacy_job(
    repository: RelocatableRepository,
    job_id: str,
    legacy_job_dir: str | Path,
    internal_job_dir: str | Path,
    user_output_dir: str | Path,
) -> UserDelivery:
    legacy = Path(legacy_job_dir)
    internal = Path(internal_job_dir)
    if not legacy.is_dir():
        raise FileNotFoundError(f"旧任务目录不存在: {legacy}")
    if internal.exists():
        raise FileExistsError(f"内部任务目录已存在: {internal}")
    internal.parent.mkdir(parents=True, exist_ok=True)
    atomic_replace(legacy, internal)
    try:
        repository.relocate_output_dir(job_id, internal)
        return finalize_user_delivery(internal, user_output_dir)
    except BaseException:
        if not legacy.exists() and internal.exists():
            atomic_replace(internal, legacy)
            repository.relocate_output_dir(job_id, legacy)
        raise
