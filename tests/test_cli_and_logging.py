from __future__ import annotations

import logging
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aicf.cli import build_m4_asset_runner, main
from aicf.database import JobRepository
from aicf.logging_utils import configure_logging, sanitize_error
from aicf.production_settings import ProductionSettings
from aicf.state_machine import ORDERED_STAGES, PipelineStage


def test_importing_cli_does_not_replace_process_streams() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "stdout, stderr = sys.stdout, sys.stderr; "
                "import aicf.cli; "
                "assert sys.stdout is stdout; "
                "assert sys.stderr is stderr"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_build_m4_runner_initializes_only_selected_jimeng(
    monkeypatch,
) -> None:
    jimeng = object()
    monkeypatch.setattr(
        "aicf.cli.discover_ffmpeg_toolchain",
        lambda: SimpleNamespace(ffprobe=Path("ffprobe")),
    )
    monkeypatch.setattr(
        "aicf.cli.load_config",
        lambda _path: SimpleNamespace(
            generation_budget=SimpleNamespace(enable_asset_cache=False)
        ),
    )
    monkeypatch.setattr("aicf.cli.build_dreamina_adapter", lambda: jimeng)

    def reject_kling_probe() -> object:
        raise AssertionError("选择即梦时不应探测可灵")

    monkeypatch.setattr("aicf.cli.build_kling_adapter", reject_kling_probe)

    runner = build_m4_asset_runner("jimeng")

    assert runner.providers == {"jimeng": jimeng}


def test_cli_doctor_runs_and_never_prints_secret(
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "top-secret")

    exit_code = main(["doctor"])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "top-secret" not in output
    assert "python: OK" in output


def test_cli_init_job_status_and_resume_use_project_database(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))

    assert main(["init-job", "--job", "JOB-中文"]) == 0
    assert main(["status", "--job", "JOB-中文"]) == 0
    status_output = capsys.readouterr().out
    assert "JOB-中文" in status_output
    assert "尚未开始" in status_output

    class FakeResumeAutopilot:
        def run(self, job_id: str) -> dict[str, object]:
            assert job_id == "JOB-中文"
            return {"status": "READY_TO_PUBLISH"}

    monkeypatch.setattr(
        "aicf.cli.build_autopilot",
        lambda _repository: FakeResumeAutopilot(),
    )
    assert main(["resume", "--job", "JOB-中文"]) == 0
    resume_output = json.loads(capsys.readouterr().out)
    assert resume_output["status"] == "READY_TO_PUBLISH"
    assert (tmp_path / "data" / "jobs" / "JOB-中文" / "status.json").exists()
    assert not (tmp_path / "outputs" / "JOB-中文" / "status.json").exists()


def test_worker_start_rejects_completed_job_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    repo = JobRepository(tmp_path / "data" / "content.db")
    repo.create_job("JOB-DONE", tmp_path / "data" / "jobs" / "JOB-DONE")
    for stage in ORDERED_STAGES:
        repo.start_stage("JOB-DONE", stage)
        repo.complete_stage("JOB-DONE", stage)
        if stage == PipelineStage.COMPLETED:
            break

    class FailIfLaunched:
        def __init__(self, **_kwargs: object) -> None:
            pytest.fail("已完成任务不应构造 WorkerLauncher")

    monkeypatch.setattr("aicf.cli.WorkerLauncher", FailIfLaunched)

    assert main(["worker-start", "--job", "JOB-DONE"]) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ALREADY_COMPLETED"
    assert result["job_id"] == "JOB-DONE"
    assert "新任务ID" in result["next_action"]


def _failed_attention_job(tmp_path: Path, job_id: str) -> JobRepository:
    repo = JobRepository(tmp_path / "data" / "content.db")
    repo.create_job(job_id, tmp_path / "outputs" / job_id)
    repo.start_stage(job_id, PipelineStage.DIRECTION_LOADED)
    repo.fail_stage(
        job_id,
        PipelineStage.DIRECTION_LOADED,
        "需要人工修复",
        retryable=False,
        recovery_command=(
            f"python -m aicf reopen --job {job_id} "
            "--confirm-artifacts-fixed"
        ),
    )
    return repo


def test_resume_refuses_failed_needs_attention_without_running_autopilot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    _failed_attention_job(tmp_path, "JOB-ATTENTION")
    monkeypatch.setattr(
        "aicf.cli.build_autopilot",
        lambda _repo: pytest.fail("resume 不应运行不可恢复 Job"),
    )

    assert main(["resume", "--job", "JOB-ATTENTION"]) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "FAILED_NEEDS_ATTENTION"
    assert result["recovery_command"] == (
        "python -m aicf reopen --job JOB-ATTENTION "
        "--confirm-artifacts-fixed"
    )


def test_reopen_requires_explicit_confirmation_or_recoverable_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    repo = _failed_attention_job(tmp_path, "JOB-REOPEN")

    assert main(["reopen", "--job", "JOB-REOPEN"]) == 2
    assert repo.get_job("JOB-REOPEN").current_stage == (
        PipelineStage.FAILED_NEEDS_ATTENTION
    )
    capsys.readouterr()

    assert (
        main(
            [
                "reopen",
                "--job",
                "JOB-REOPEN",
                "--confirm-artifacts-fixed",
            ]
        )
        == 0
    )
    reopened = repo.get_job("JOB-REOPEN")
    assert reopened.current_stage is None
    assert reopened.failed_stage is None
    assert PipelineStage.DIRECTION_LOADED not in reopened.completed_stages


def test_cli_rebuild_status_snapshot_from_sqlite(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    assert main(["init-job", "--job", "JOB-REBUILD"]) == 0
    snapshot = tmp_path / "data" / "jobs" / "JOB-REBUILD" / "status.json"
    snapshot.unlink()

    assert main(["rebuild-snapshot", "--job", "JOB-REBUILD"]) == 0

    output = capsys.readouterr().out
    assert "快照已重建" in output
    assert snapshot.exists()


def test_cli_does_not_claim_rebuild_success_when_snapshot_write_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    assert main(["init-job", "--job", "JOB-REBUILD-FAIL"]) == 0
    capsys.readouterr()

    def fail_snapshot(
        _repository: JobRepository,
        _status: object,
        **_kwargs: object,
    ) -> bool:
        raise OSError("磁盘已满")

    monkeypatch.setattr(JobRepository, "_write_status", fail_snapshot)

    assert main(["rebuild-snapshot", "--job", "JOB-REBUILD-FAIL"]) == 1

    output = capsys.readouterr().out
    assert "快照已重建" not in output
    assert "快照重建失败" in output


def test_configured_logger_writes_utf8_and_redacts_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "do-not-log")
    log_path = tmp_path / "中文日志" / "app.log"
    logger = configure_logging(log_path, logger_name="aicf.test")

    logger.info("方向：中文；key=%s", "do-not-log")
    for handler in logger.handlers:
        handler.flush()

    content = log_path.read_text(encoding="utf-8")
    assert "方向：中文" in content
    assert "do-not-log" not in content
    assert "***REDACTED***" in content


def test_sanitize_error_redacts_credentials_signatures_and_user_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JIMENG_TOKEN", "env-secret")
    raw = (
        "Bearer bearer-secret sk-or-v1-abcdef "
        "token=plain-token cookie: session-cookie "
        "https://api.example.test/v1?a=1&signature=signed-value&x=2 "
        r"C:\Users\Alice\Videos\private.mp4 "
        "/home/alice/private/output.mp4 env-secret"
    )

    sanitized = sanitize_error(raw)

    for secret in (
        "bearer-secret",
        "sk-or-v1-abcdef",
        "plain-token",
        "session-cookie",
        "signed-value",
        "Alice",
        "/home/alice/private",
        "env-secret",
    ):
        assert secret not in sanitized
    assert sanitized.count("***REDACTED***") >= 6


def test_gui_log_sanitizes_secrets_before_queueing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aicf.gui import AicfGUI

    monkeypatch.setenv("OPENROUTER_API_KEY", "do-not-display")
    queued: list[str] = []
    gui = object.__new__(AicfGUI)
    gui.log_queue = type(
        "QueueStub",
        (),
        {"put": lambda _self, value: queued.append(value)},
    )()

    gui._log("请求失败：Bearer do-not-display")

    assert len(queued) == 1
    assert "do-not-display" not in queued[0]
    assert "***REDACTED***" in queued[0]


def test_cli_error_json_uses_shared_sanitizer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_path = tmp_path / "用户目录" / "secret.txt"

    class FailingAdapter:
        def generate_image(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError(
                f"Bearer cli-secret token=cli-token path={secret_path}"
            )

    monkeypatch.setattr("aicf.cli.build_dreamina_adapter", lambda: FailingAdapter())

    assert main(["dreamina-smoke", "--output-dir", str(tmp_path)]) == 1
    output = capsys.readouterr().out
    assert "cli-secret" not in output
    assert "cli-token" not in output
    assert str(secret_path) not in output
    assert "***REDACTED***" in output


def test_cli_tts_smoke_records_actual_provider_and_degradation(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    output = tmp_path / "smoke.wav"

    class FakeService:
        def synthesize(self, text: str, output_path: Path):
            output_path.write_bytes(b"RIFF-smoke")
            metadata = output_path.with_suffix(".wav.tts.json")
            metadata.write_text("{}", encoding="utf-8")
            return SimpleNamespace(
                provider="windows_sapi",
                degraded=True,
                degradation_reason="edge_tts: RuntimeError: offline",
                metadata_path=metadata,
            )

    monkeypatch.setattr("aicf.cli.build_default_tts_service", lambda: FakeService())

    exit_code = main(
        ["tts-smoke", "--output", str(output), "--text", "真实冒烟测试"]
    )
    rendered = capsys.readouterr().out

    assert exit_code == 0
    assert "Provider: windows_sapi" in rendered
    assert "降级原因: edge_tts: RuntimeError: offline" in rendered
    assert str(output.with_suffix(".wav.tts.json")) in rendered


def test_cli_dreamina_smoke_generates_only_one_9_by_16_image(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    class FakeAdapter:
        def generate_image(
            self,
            prompt: str,
            output_path: Path,
            *,
            model: str,
            ratio: str,
        ):
            captured.update(
                prompt=prompt,
                output_path=output_path,
                model=model,
                ratio=ratio,
            )
            output_path.parent.mkdir(parents=True)
            output_path.write_bytes(b"validated-image")
            return SimpleNamespace(
                output_path=output_path,
                submit_id="smoke-submit-id",
                cached=False,
            )

        def generate_video(self, *_args, **_kwargs):
            raise AssertionError("dreamina-smoke 不得生成视频")

    monkeypatch.setattr("aicf.cli.build_dreamina_adapter", lambda: FakeAdapter())
    output_dir = tmp_path / "中文输出" / "DREAMINA_SMOKE"

    exit_code = main(
        [
            "dreamina-smoke",
            "--output-dir",
            str(output_dir),
            "--prompt",
            "真实竖屏冒烟图",
            "--model",
            "4.1",
        ]
    )

    assert exit_code == 0
    assert captured == {
        "prompt": "真实竖屏冒烟图",
        "output_path": output_dir / "dreamina_smoke.png",
        "model": "4.1",
        "ratio": "9:16",
    }
    rendered = capsys.readouterr().out
    assert "smoke-submit-id" in rendered
    assert "9:16" in rendered


def test_cli_asset_run_forwards_resume_and_reports_waiting_external(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    plan_path = tmp_path / "visual_plan.json"
    plan_path.write_text("{}", encoding="utf-8")
    ProductionSettings(video_provider="kling").save_for_job(tmp_path)
    captured: dict[str, object] = {}

    class FakeRunner:
        def run(self, visual_plan_path: Path, *, resume: bool):
            captured.update(visual_plan_path=visual_plan_path, resume=resume)
            return {
                "status": "WAITING_EXTERNAL",
                "recovery_command": "python -m aicf asset-run --resume",
            }

    def build_runner(provider: str) -> FakeRunner:
        captured["provider"] = provider
        return FakeRunner()

    monkeypatch.setattr("aicf.cli.build_m4_asset_runner", build_runner)

    exit_code = main(
        ["asset-run", "--visual-plan", str(plan_path), "--resume"]
    )

    assert exit_code == 2
    assert captured == {
        "provider": "kling",
        "visual_plan_path": plan_path,
        "resume": True,
    }
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "WAITING_EXTERNAL"


def test_build_dreamina_adapter_allows_slow_windows_cli_help_probe(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "jimeng_cli.yaml").write_text(
        "command_prefix:\n  - C:\\\\工具\\\\dreamina.exe\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_detect(candidates, *, config_path, timeout_seconds):
        captured.update(
            candidates=candidates,
            config_path=config_path,
            timeout_seconds=timeout_seconds,
        )
        return SimpleNamespace()

    class FakeAdapter:
        def __init__(self, *_args, **_kwargs):
            pass

    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr("aicf.cli.detect_jimeng_cli", fake_detect)
    monkeypatch.setattr("aicf.cli.JimengCliAdapter", FakeAdapter)

    from aicf.cli import build_dreamina_adapter

    build_dreamina_adapter()

    assert captured["timeout_seconds"] == 30


def test_cli_content_run_loads_direction_yaml_and_runs_job(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "content_direction.yaml").write_text(
        "direction: 从 YAML 读取的方向\n",
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    class FakeRunner:
        def run(self, job_id: str, config: object) -> dict[str, object]:
            captured["job_id"] = job_id
            captured["direction"] = config.direction
            return {"status": "ready_to_publish", "topic_id": "T001"}

    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret")
    monkeypatch.setattr("aicf.cli.build_m2_runner", lambda: FakeRunner())

    exit_code = main(["content-run", "--job", "M2CLI001"])

    assert exit_code == 0
    assert captured == {
        "job_id": "M2CLI001",
        "direction": "从 YAML 读取的方向",
    }
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ready_to_publish"


def test_cli_autopilot_creates_new_job_and_runs_full_orchestrator(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    calls: list[str] = []

    class FakeAutopilot:
        def run(self, job_id: str) -> dict[str, object]:
            calls.append(job_id)
            return {"status": "READY_TO_PUBLISH"}

    monkeypatch.setattr(
        "aicf.cli.build_autopilot",
        lambda _repository: FakeAutopilot(),
    )

    assert main(["autopilot", "--job", "CLI-FULL-001"]) == 0

    assert calls == ["CLI-FULL-001"]
    repo = JobRepository(tmp_path / "data" / "content.db")
    assert repo.get_job("CLI-FULL-001").job_id == "CLI-FULL-001"
    assert json.loads(capsys.readouterr().out)["status"] == "READY_TO_PUBLISH"


def test_cli_resume_calls_real_autopilot_handlers(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    JobRepository(tmp_path / "data" / "content.db").create_job(
        "CLI-RESUME-001",
        tmp_path / "outputs" / "CLI-RESUME-001",
    )
    calls: list[str] = []

    class FakeAutopilot:
        def run(self, job_id: str) -> dict[str, object]:
            calls.append(job_id)
            return {"status": "READY_TO_PUBLISH"}

    monkeypatch.setattr(
        "aicf.cli.build_autopilot",
        lambda _repository: FakeAutopilot(),
    )

    assert main(["resume", "--job", "CLI-RESUME-001"]) == 0

    assert calls == ["CLI-RESUME-001"]
    assert json.loads(capsys.readouterr().out)["status"] == "READY_TO_PUBLISH"
