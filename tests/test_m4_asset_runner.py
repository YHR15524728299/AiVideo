from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from pathlib import Path

import pytest
from PIL import Image

from aicf.m4_asset_runner import M4AssetRunner


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

    def submit_image(self, prompt: str, *, model: str, ratio: str) -> str:
        return self._submit("image", prompt)

    def submit_video(
        self,
        prompt: str,
        required_seconds: float,
        *,
        model: str,
        ratio: str,
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


def test_processes_three_images_and_two_videos_serially_and_updates_outputs(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["image", "video", "image", "video", "image"])
    provider = FakeDreamina()

    result = M4AssetRunner(provider, media_probe=_probe).run(plan_path)

    assert result["status"] == "COMPLETED"
    assert [kind for kind, _, _ in provider.submissions] == [
        "image",
        "video",
        "image",
        "video",
        "image",
    ]
    tasks = json.loads(
        (tmp_path / "assets" / "tasks.json").read_text(encoding="utf-8")
    )
    assert [task["status"] for task in tasks["tasks"]] == ["completed"] * 5
    assert all(task["attempts"] == 1 for task in tasks["tasks"])
    assert all(task["submit_id"] for task in tasks["tasks"])
    assert all(task["downloaded_path"] for task in tasks["tasks"])
    assert all(len(task["prompt_hash"]) == 64 for task in tasks["tasks"])

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    manifest = json.loads(
        (tmp_path / "asset_manifest.json").read_text(encoding="utf-8")
    )
    assert len(manifest["assets"]) == 5
    for shot, asset in zip(plan["shots"], manifest["assets"], strict=True):
        asset_path = (tmp_path / shot["expected_path"]).resolve()
        assert asset_path.is_file()
        assert asset["expected_path"] == shot["expected_path"]
        assert asset["sha256"] == hashlib.sha256(asset_path.read_bytes()).hexdigest()
        assert asset["media_probe"]["size_bytes"] == asset_path.stat().st_size


def test_resume_refreshes_unsubmitted_task_kind_when_visual_plan_changes(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["image"])
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


def test_pending_timeout_returns_waiting_external_and_resume_never_resubmits(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["image", "video", "image", "video", "image"])
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


def test_failed_video_retries_submission_once_then_degrades_to_image_kenburns(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["video"])
    provider = FakeDreamina(
        {
            "video-1": ["failed"],
            "video-2": ["failed"],
            "image-3": ["success"],
        }
    )

    result = M4AssetRunner(provider, media_probe=_probe).run(plan_path)

    assert result["status"] == "COMPLETED"
    assert [kind for kind, _, _ in provider.submissions] == [
        "video",
        "video",
        "image",
    ]
    tasks = json.loads(
        (tmp_path / "assets" / "tasks.json").read_text(encoding="utf-8")
    )
    task = tasks["tasks"][0]
    assert task["attempts"] == 3
    assert task["submit_id"] == "image-3"
    assert task["status"] == "completed"
    assert task["degraded_from"] == "video"
    assert task["ken_burns_path"].endswith(".ken_burns.json")

    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    shot = plan["shots"][0]
    assert shot["asset_type"] == "image"
    assert shot["expected_path"].endswith(".keyframe.png")
    keyframe = tmp_path / shot["expected_path"]
    assert keyframe.is_file()
    ken_burns = json.loads(
        (tmp_path / task["ken_burns_path"]).read_text(encoding="utf-8")
    )
    assert ken_burns["source"] == shot["expected_path"]
    assert ken_burns["duration_seconds"] == 5.0


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
                "duration_seconds": required_seconds,
            }
            return super().submit_video(
                prompt,
                required_seconds,
                model=model,
                ratio=ratio,
            )

    result = M4AssetRunner(InspectingProvider(), media_probe=_probe).run(plan_path)

    assert result["status"] == "COMPLETED"


def test_crash_after_remote_submit_is_never_resubmitted_and_needs_attention(
    tmp_path: Path,
) -> None:
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["image"])
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
    plan_path = tmp_path / "visual_plan.json"
    _write_plan(plan_path, ["image", "video"])
    recorded: list[dict[str, object]] = []

    result = M4AssetRunner(FakeDreamina(), media_probe=_probe).run(
        plan_path,
        usage_recorder=lambda **event: recorded.append(event),
    )

    assert result["status"] == "COMPLETED"
    assert recorded == [
        {
            "request_id": recorded[0]["request_id"],
            "jimeng_images": 1,
            "jimeng_video_clips": 0,
            "jimeng_video_seconds_requested": 0,
        },
        {
            "request_id": recorded[1]["request_id"],
            "jimeng_images": 0,
            "jimeng_video_clips": 1,
            "jimeng_video_seconds_requested": 5,
        },
    ]
