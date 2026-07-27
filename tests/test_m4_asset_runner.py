from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

import pytest
from PIL import Image

from aicf.m4_asset_runner import M4AssetRunner
from aicf.production_settings import ProductionSettings


def _write_plan(path: Path, kinds: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    shots = []
    start_seconds = 0.0
    for index, kind in enumerate(kinds, start=1):
        suffix = ".mp4" if kind == "video" else ".png"
        duration_seconds = 5.0 if kind == "video" else 1.0
        shots.append(
            {
                "shot_id": f"VIS{index:03d}",
                "script_segment_id": f"SEG{index:03d}",
                "asset_type": kind,
                "prompt": f"镜头 {index}，竖屏9:16，无文字",
                "expected_path": f"assets/VIS{index:03d}{suffix}",
                "start_seconds": start_seconds,
                "duration_seconds": duration_seconds,
            }
        )
        start_seconds += duration_seconds
    path.write_text(
        json.dumps(
            {
                "title": "M4 集成测试",
                "mode": "balanced",
                "total_duration_seconds": sum(
                    shot["duration_seconds"] for shot in shots
                ),
                "shots": shots,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


class FakeDreamina:
    def __init__(self, states: dict[str, list[str]] | None = None) -> None:
        self.submissions: list[tuple[str, str, str]] = []
        self.queries: list[str] = []
        self.downloads: list[tuple[str, str]] = []
        self._states = states or {}
        self._query_counts: defaultdict[str, int] = defaultdict(int)

    def submit_image(
        self, prompt: str, *, model: str, ratio: str, resolution: str = "2k"
    ) -> str:
        return self._submit("image", prompt)

    def submit_video(
        self,
        prompt: str,
        required_seconds: float,
        *,
        model: str,
        ratio: str,
        resolution: str = "720p",
    ) -> str:
        assert 4 <= required_seconds <= 15
        return self._submit("video", prompt)

    def _submit(self, kind: str, prompt: str) -> str:
        submit_id = f"{kind}-{len(self.submissions) + 1}"
        self.submissions.append((kind, prompt, submit_id))
        return submit_id

    def query(self, submit_id: str) -> dict[str, object]:
        self.queries.append(submit_id)
        states = self._states.get(submit_id, ["success"])
        index = min(self._query_counts[submit_id], len(states) - 1)
        self._query_counts[submit_id] += 1
        state = states[index]
        return {
            "submit_id": submit_id,
            "gen_status": state,
            "fail_reason": "fake failed" if state == "failed" else None,
        }

    def download(
        self,
        submit_id: str,
        output_path: str | Path,
        *,
        kind: str,
    ) -> Path:
        target = Path(output_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if kind == "image":
            Image.new("RGB", (576, 1024), (20, 40, 80)).save(target)
        else:
            target.write_bytes(f"fake-video:{submit_id}".encode())
        self.downloads.append((submit_id, kind))
        return target

    @staticmethod
    def failure_reason(payload: dict[str, object]) -> str:
        return str(payload.get("fail_reason") or "failed")


class CrashAfterRemoteSubmit(FakeDreamina):
    def _submit(self, kind: str, prompt: str) -> str:
        super()._submit(kind, prompt)
        raise SystemExit("simulated crash after remote submission")


def _probe(path: Path, kind: str) -> dict[str, object]:
    if kind == "image":
        with Image.open(path) as image:
            return {
                "kind": "image",
                "width": image.width,
                "height": image.height,
                "format": image.format,
                "size_bytes": path.stat().st_size,
            }
    return {
        "kind": "video",
        "codec_name": "h264",
        "width": 1080,
        "height": 1920,
        "duration_seconds": 5.0,
        "size_bytes": path.stat().st_size,
    }


def test_processes_all_videos_in_video_mode_and_updates_outputs(
    tmp_path: Path,
) -> None:
    """视频模式下所有镜头都生成为视频（短镜头自动延长到4秒）。"""
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["image", "video", "image", "video", "image"])
    provider = FakeDreamina()

    result = M4AssetRunner(provider, media_probe=_probe).run(plan_path)

    assert result["status"] == "COMPLETED"
    # 视频模式下所有镜头都是视频
    assert [kind for kind, _, _ in provider.submissions] == [
        "video",
        "video",
        "video",
        "video",
        "video",
    ]
    tasks = json.loads(
        (tmp_path / "assets" / "tasks.json").read_text(encoding="utf-8")
    )
    assert [task["status"] for task in tasks["tasks"]] == ["completed"] * 5
    assert all(task["attempts"] == 1 for task in tasks["tasks"])
    assert all(task["submit_id"] for task in tasks["tasks"])
    assert all(task["downloaded_path"] for task in tasks["tasks"])
    assert all(len(task["prompt_hash"]) == 64 for task in tasks["tasks"])

    manifest = json.loads(
        (tmp_path / "asset_manifest.json").read_text(encoding="utf-8")
    )
    assert len(manifest["assets"]) == 5
    for asset in manifest["assets"]:
        assert asset["type"] == "video"
        assert asset["expected_path"].endswith(".mp4")


def test_resume_refreshes_unsubmitted_task_kind_when_visual_plan_changes(
    tmp_path: Path,
) -> None:
    """图片模式下，plan 是 image 但旧 tasks 是 video 时，刷新为 image。"""
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["image"])
    ProductionSettings(motion_mode="image").save_for_job(tmp_path)
    tasks_path = tmp_path / "assets" / "tasks.json"
    tasks_path.parent.mkdir()
    tasks_path.write_text(
        json.dumps(
            {
                "version": 1,
                "tasks": [
                    {
                        "shot_id": "VIS001",
                        "asset_type": "video",
                        "active_kind": "video",
                        "prompt_hash": "stale",
                        "submit_id": None,
                        "status": "new",
                        "attempts": 0,
                        "downloaded_path": None,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    provider = FakeDreamina()

    result = M4AssetRunner(provider, media_probe=_probe).run(plan_path, resume=True)

    assert result["status"] == "COMPLETED"
    assert [kind for kind, _, _ in provider.submissions] == ["image"]
    task = json.loads(tasks_path.read_text(encoding="utf-8"))["tasks"][0]
    assert task["asset_type"] == "image"
    assert task["active_kind"] == "image"


@pytest.mark.parametrize(
    "conflict",
    ["asset_type", "settings"],
)
def test_resume_rejects_remote_task_asset_type_or_settings_conflict(
    tmp_path: Path,
    conflict: str,
) -> None:
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["video"])
    prompt = "镜头 1，竖屏9:16，无文字"
    task = {
        "shot_id": "VIS001",
        "asset_type": "video",
        "active_kind": "video",
        "prompt_hash": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "submit_id": "remote-1",
        "status": "pending",
        "attempts": 1,
        "downloaded_path": None,
        "submission_parameters": {
            "kind": "video",
            "model": "seedance2.0fast",
            "ratio": "9:16",
            "resolution": "720p",
            "duration_seconds": 5.0,
        },
    }
    if conflict == "asset_type":
        task["asset_type"] = "image"
        task["active_kind"] = "image"
    else:
        task["submission_parameters"]["resolution"] = "1080p"
    tasks_path = tmp_path / "assets" / "tasks.json"
    tasks_path.parent.mkdir()
    tasks_path.write_text(
        json.dumps({"version": 1, "tasks": [task]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="恢复冲突"):
        M4AssetRunner(FakeDreamina(), media_probe=_probe).run(
            plan_path,
            resume=True,
        )


def test_pending_timeout_returns_waiting_external_and_resume_never_resubmits(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["image", "video", "image", "video", "image"])
    ProductionSettings(motion_mode="image").save_for_job(tmp_path)
    provider = FakeDreamina({"image-1": ["pending", "success"]})
    runner = M4AssetRunner(
        provider,
        media_probe=_probe,
        pending_timeout_seconds=0,
    )

    waiting = runner.run(plan_path)

    assert waiting["status"] == "WAITING_EXTERNAL"
    assert waiting["recovery_command"].endswith(
        f'asset-run --visual-plan "{plan_path}" --resume'
    )
    assert provider.submissions == [
        ("image", "镜头 1，竖屏9:16，无文字", "image-1")
    ]
    persisted = json.loads(
        (tmp_path / "assets" / "tasks.json").read_text(encoding="utf-8")
    )
    assert persisted["tasks"][0]["submit_id"] == "image-1"
    assert persisted["tasks"][0]["status"] == "pending"

    completed = runner.run(plan_path, resume=True)

    assert completed["status"] == "COMPLETED"
    assert [submit_id for _, _, submit_id in provider.submissions].count("image-1") == 1
    assert len(provider.submissions) == 5


def test_failed_video_retries_once_then_raises_error_no_degradation(
    tmp_path: Path,
) -> None:
    """视频模式下，视频生成失败重试1次后仍失败则抛出异常，不降级为图片。"""
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["video"])
    provider = FakeDreamina(
        {
            "video-1": ["failed"],
            "video-2": ["failed"],
        }
    )

    with pytest.raises(RuntimeError, match="VIS001 生成失败"):
        M4AssetRunner(provider, media_probe=_probe).run(plan_path)

    # 总共提交了2次（1次初始 + 1次重试），没有降级为图片
    assert [kind for kind, _, _ in provider.submissions] == ["video", "video"]
    tasks = json.loads(
        (tmp_path / "assets" / "tasks.json").read_text(encoding="utf-8")
    )
    task = tasks["tasks"][0]
    assert task["attempts"] == 2
    assert task.get("degraded_from") is None


def test_writes_durable_intent_before_submit_with_request_and_model_parameters(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["video"])

    class InspectingProvider(FakeDreamina):
        def submit_video(
            self,
            prompt: str,
            required_seconds: float,
            *,
            model: str,
            ratio: str,
            resolution: str = "720p",
        ) -> str:
            task = json.loads(
                (tmp_path / "assets" / "tasks.json").read_text(encoding="utf-8")
            )["tasks"][0]
            assert task["status"] == "submission_intent"
            assert len(task["request_id"]) == 32
            assert task["prompt_hash"] == hashlib.sha256(prompt.encode()).hexdigest()
            assert task["submission_parameters"] == {
                "kind": "video",
                "model": model,
                "ratio": ratio,
                "resolution": resolution,
                "duration_seconds": required_seconds,
            }
            return super().submit_video(
                prompt,
                required_seconds,
                model=model,
                ratio=ratio,
                resolution=resolution,
            )

    result = M4AssetRunner(InspectingProvider(), media_probe=_probe).run(plan_path)

    assert result["status"] == "COMPLETED"


def test_reads_job_settings_and_passes_model_resolution_and_motion_mode(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["image", "image", "image", "image"])
    plan_value = json.loads(plan_path.read_text(encoding="utf-8"))
    for index, shot in enumerate(plan_value["shots"]):
        shot["start_seconds"] = index * 5.0
        shot["duration_seconds"] = 5.0
        shot["expected_path"] = f"assets/VIS{index + 1:03d}.mp4"
    plan_value["total_duration_seconds"] = 20.0
    plan_path.write_text(json.dumps(plan_value), encoding="utf-8")
    from aicf.production_settings import ProductionSettings

    ProductionSettings(
        jimeng_model="seedance2.0_vip",
        video_resolution="1080p",
        motion_mode="video",
    ).save_for_job(tmp_path)

    class RecordingDreamina(FakeDreamina):
        def __init__(self) -> None:
            super().__init__()
            self.parameters: list[tuple[str, str]] = []

        def submit_video(
            self,
            prompt: str,
            required_seconds: float,
            *,
            model: str,
            ratio: str,
            resolution: str = "720p",
        ) -> str:
            self.parameters.append((model, resolution))
            return super().submit_video(
                prompt,
                required_seconds,
                model=model,
                ratio=ratio,
                resolution=resolution,
            )

    provider = RecordingDreamina()
    M4AssetRunner(provider, media_probe=_probe).run(plan_path)

    assert provider.parameters == [("seedance2.0_vip", "1080p")] * 4


def test_crash_after_remote_submit_is_never_resubmitted_and_needs_attention(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["image"])
    ProductionSettings(motion_mode="image").save_for_job(tmp_path)
    crashing = CrashAfterRemoteSubmit()

    with pytest.raises(SystemExit, match="simulated crash"):
        M4AssetRunner(crashing, media_probe=_probe).run(plan_path)

    persisted = json.loads(
        (tmp_path / "assets" / "tasks.json").read_text(encoding="utf-8")
    )["tasks"][0]
    assert persisted["status"] == "submission_intent"
    assert persisted["submit_id"] is None
    assert len(crashing.submissions) == 1

    replacement = FakeDreamina()
    result = M4AssetRunner(replacement, media_probe=_probe).run(plan_path, resume=True)

    assert result["status"] == "FAILED_NEEDS_ATTENTION"
    assert result["reason"] == "UNKNOWN_REMOTE_SUBMISSION"
    assert replacement.submissions == []
    task = json.loads(
        (tmp_path / "assets" / "tasks.json").read_text(encoding="utf-8")
    )["tasks"][0]
    assert task["status"] == "UNKNOWN_REMOTE_SUBMISSION"

    repeated = M4AssetRunner(replacement, media_probe=_probe).run(
        plan_path,
        resume=True,
    )
    assert repeated["status"] == "FAILED_NEEDS_ATTENTION"
    assert replacement.submissions == []


def test_cross_job_cache_key_includes_generation_parameters_and_verifies_copy(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "shared-cache"
    first_plan = tmp_path / "job-a" / "visual_plan.json"
    second_plan = tmp_path / "job-b" / "visual_plan.json"
    _write_plan(first_plan, ["image"])
    _write_plan(second_plan, ["image"])
    ProductionSettings(motion_mode="image").save_for_job(first_plan.parent)
    ProductionSettings(motion_mode="image").save_for_job(second_plan.parent)
    first_provider = FakeDreamina()

    first = M4AssetRunner(
        first_provider,
        media_probe=_probe,
        asset_cache_dir=cache_dir,
    ).run(first_plan)
    second_provider = FakeDreamina()
    second = M4AssetRunner(
        second_provider,
        media_probe=_probe,
        asset_cache_dir=cache_dir,
    ).run(second_plan)

    assert first["status"] == second["status"] == "COMPLETED"
    assert len(first_provider.submissions) == 1
    assert second_provider.submissions == []
    first_asset = tmp_path / "job-a" / "assets" / "VIS001.png"
    second_asset = tmp_path / "job-b" / "assets" / "VIS001.png"
    assert second_asset.read_bytes() == first_asset.read_bytes()
    second_task = json.loads(
        (tmp_path / "job-b" / "assets" / "tasks.json").read_text(encoding="utf-8")
    )["tasks"][0]
    assert second_task["status"] == "completed"
    assert second_task["cache_hit"] is True
    assert second_task["submit_id"] is None


def test_records_usage_once_for_each_confirmed_new_submission(tmp_path: Path) -> None:
    """图片模式下每个镜头记录一次图片用量。"""
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["image", "image"])
    ProductionSettings(motion_mode="image").save_for_job(tmp_path)
    recorded: list[dict[str, object]] = []

    result = M4AssetRunner(FakeDreamina(), media_probe=_probe).run(
        plan_path,
        usage_recorder=lambda **event: recorded.append(event),
    )

    assert result["status"] == "COMPLETED"
    assert len(recorded) == 2
    for event in recorded:
        assert event["jimeng_images"] == 1
        assert event["jimeng_video_clips"] == 0
        assert event["jimeng_video_seconds_requested"] == 0


def test_download_is_validated_in_temporary_file_before_atomic_publish(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["image"])
    ProductionSettings(motion_mode="image").save_for_job(tmp_path)
    target = tmp_path / "assets" / "VIS001.png"
    downloaded_paths: list[Path] = []

    class InvalidDownload(FakeDreamina):
        def download(
            self,
            submit_id: str,
            output_path: str | Path,
            *,
            kind: str,
        ) -> Path:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"<html>not an image</html>")
            downloaded_paths.append(path)
            return path

    with pytest.raises(Exception):
        M4AssetRunner(InvalidDownload(), media_probe=_probe).run(plan_path)

    assert downloaded_paths
    assert downloaded_paths[0] != target
    assert not target.exists()
    task = json.loads(
        (tmp_path / "assets" / "tasks.json").read_text(encoding="utf-8")
    )["tasks"][0]
    assert task["status"] != "completed"


def test_resume_revalidates_completed_asset_before_accepting_it(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["image"])
    ProductionSettings(motion_mode="image").save_for_job(tmp_path)
    target = tmp_path / "assets" / "VIS001.png"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"corrupt")
    prompt = "镜头 1，竖屏9:16，无文字"
    tasks_path = tmp_path / "assets" / "tasks.json"
    tasks_path.write_text(
        json.dumps(
            {
                "version": 1,
                "tasks": [
                    {
                        "shot_id": "VIS001",
                        "asset_type": "image",
                        "active_kind": "image",
                        "prompt_hash": hashlib.sha256(prompt.encode()).hexdigest(),
                        "submit_id": "image-existing",
                        "status": "completed",
                        "attempts": 1,
                        "downloaded_path": "assets/VIS001.png",
                        "submission_parameters": {
                            "kind": "image",
                            "model": "4.1",
                            "ratio": "9:16",
                            "resolution": "2k",
                            "duration_seconds": 1.0,
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    provider = FakeDreamina({"image-existing": ["success"]})

    result = M4AssetRunner(provider, media_probe=_probe).run(
        plan_path,
        resume=True,
    )

    assert result["status"] == "COMPLETED"
    assert provider.downloads == [("image-existing", "image")]
    with Image.open(target) as image:
        assert image.size == (576, 1024)
