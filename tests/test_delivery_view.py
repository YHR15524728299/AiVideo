from __future__ import annotations

import hashlib
import json
from pathlib import Path

from aicf.database import JobRepository
from aicf.delivery_view import (
    FINAL_FILE_NAMES,
    finalize_user_delivery,
    migrate_legacy_job,
)


def _write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def test_finalize_exposes_exactly_five_user_files(tmp_path: Path) -> None:
    job_dir = tmp_path / "data" / "jobs" / "JOB1"
    delivery = job_dir / "delivery"
    _write(delivery / "youtube" / "video.mp4", b"final")
    _write(delivery / "clean.mp4", b"clean")
    _write(delivery / "cover.jpg", b"cover")
    _write(delivery / "youtube" / "publish.md", "标题".encode())
    (delivery / "publish_manifest.json").write_text(
        json.dumps(
            {
                "status": "READY_TO_PUBLISH",
                "orientation": "landscape",
                "expected_duration_seconds": 10,
                "platforms": {"youtube": {"video": "youtube/video.mp4"}},
            }
        ),
        encoding="utf-8",
    )
    user_dir = tmp_path / "outputs" / "JOB1"
    _write(user_dir / "旧文件.txt", b"remove")

    result = finalize_user_delivery(job_dir, user_dir)

    assert {path.name for path in user_dir.iterdir()} == FINAL_FILE_NAMES
    assert (user_dir / "最终视频.mp4").read_bytes() == b"final"
    assert (user_dir / "无字幕视频.mp4").read_bytes() == b"clean"
    summary = json.loads((user_dir / "验收摘要.json").read_text(encoding="utf-8"))
    assert summary["status"] == "READY_TO_PUBLISH"
    assert summary["final_video_sha256"] == hashlib.sha256(b"final").hexdigest()
    assert result.final_video == user_dir / "最终视频.mp4"


def test_finalize_does_not_copy_rejected_or_technical_files(tmp_path: Path) -> None:
    job_dir = tmp_path / "job"
    delivery = job_dir / "delivery"
    _write(delivery / "youtube" / "video.mp4", b"final")
    _write(delivery / "clean.mp4", b"clean")
    _write(delivery / "cover.jpg", b"cover")
    _write(delivery / "youtube" / "publish.md", b"copy")
    _write(delivery / "rejected_image_slideshow.mp4", b"rejected")
    _write(delivery / "qa" / "technical.json", b"qa")
    (delivery / "publish_manifest.json").write_text(
        '{"status":"READY_TO_PUBLISH","platforms":{"youtube":{}}}',
        encoding="utf-8",
    )
    user_dir = tmp_path / "output"

    finalize_user_delivery(job_dir, user_dir)

    names = {path.name for path in user_dir.iterdir()}
    assert "rejected_image_slideshow.mp4" not in names
    assert "qa" not in names


def test_repository_relocates_internal_job_directory(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "content.db")
    old = tmp_path / "outputs" / "JOB1"
    repository.create_job("JOB1", old)
    new = tmp_path / "data" / "jobs" / "JOB1"
    new.mkdir(parents=True)

    status = repository.relocate_output_dir("JOB1", new)

    assert Path(status.output_dir) == new.resolve()
    assert Path(repository.get_job("JOB1").output_dir) == new.resolve()


def test_migrate_legacy_job_separates_work_and_user_output(tmp_path: Path) -> None:
    repository = JobRepository(tmp_path / "content.db")
    legacy = tmp_path / "outputs" / "JOB1"
    repository.create_job("JOB1", legacy)
    delivery = legacy / "delivery"
    _write(delivery / "youtube" / "video.mp4", b"final")
    _write(delivery / "clean.mp4", b"clean")
    _write(delivery / "cover.jpg", b"cover")
    _write(delivery / "youtube" / "publish.md", b"copy")
    (delivery / "publish_manifest.json").write_text(
        '{"status":"READY_TO_PUBLISH","platforms":{"youtube":{}}}',
        encoding="utf-8",
    )
    internal = tmp_path / "data" / "jobs" / "JOB1"

    migrate_legacy_job(repository, "JOB1", legacy, internal, legacy)

    assert internal.is_dir()
    assert {path.name for path in legacy.iterdir()} == FINAL_FILE_NAMES
    assert Path(repository.get_job("JOB1").output_dir) == internal.resolve()
