from __future__ import annotations

import logging
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aicf.cli import build_m4_asset_runner, main
from aicf.background_worker import WorkerRecord, write_worker_record
from aicf.database import JobRepository
from aicf.gui import worker_start_command
from aicf.job_service import ResearchResumeStrategy
from aicf.logging_utils import (
    configure_logging,
    log_state_exception,
    sanitize_error,
)
from aicf.m2_runner import M2ContentRunner
from aicf.process_identity import get_process_identity
from aicf.production_settings import ProductionSettings
from aicf.state_machine import FailureKind, ORDERED_STAGES, PipelineStage


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


def test_state_exception_logging_is_structured_and_rate_limited() -> None:
    records: list[tuple[str, str]] = []

    class Logger:
        def warning(self, message: str, payload: str) -> None:
            records.append((message, payload))

    times = iter([10.0, 20.0, 80.0])
    kwargs = {
        "logger": Logger(),
        "event": "test_unique_state_failure",
        "source": "database",
        "error": OSError("db unavailable"),
        "job_id": "JOB-1",
        "clock": lambda: next(times),
    }

    assert log_state_exception(**kwargs) is True
    assert log_state_exception(**kwargs) is False
    assert log_state_exception(**kwargs) is True
    assert len(records) == 2
    payload = json.loads(records[0][1])
    assert payload["event"] == "test_unique_state_failure"
    assert payload["source"] == "database"
    assert payload["error_type"] == "OSError"


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

    class FakeLauncher:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self, job_id: str, _job_dir: Path, **_kwargs: object) -> SimpleNamespace:
            assert job_id == "JOB-中文"
            return SimpleNamespace(
                model_dump_json=lambda **_kwargs: json.dumps(
                    {"job_id": job_id, "pid": 321, "reused": False}
                )
            )

    monkeypatch.setattr("aicf.cli.WorkerLauncher", FakeLauncher)
    assert main(["resume", "--job", "JOB-中文"]) == 0
    resume_output = json.loads(capsys.readouterr().out)
    assert resume_output["pid"] == 321
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


def test_worker_start_rejects_failed_attention_before_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    _failed_attention_job(tmp_path, "JOB-ATTENTION-START")

    class FailIfLaunched:
        def __init__(self, **_kwargs: object) -> None:
            pytest.fail("未确认重开的任务不应构造 WorkerLauncher")

    monkeypatch.setattr("aicf.cli.WorkerLauncher", FailIfLaunched)

    assert main(["worker-start", "--job", "JOB-ATTENTION-START"]) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "START_REJECTED"
    assert "人工确认" in result["reason"]


def test_worker_start_cannot_bypass_auto_reopen_transition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    repo = JobRepository(tmp_path / "data" / "content.db")
    repo.create_job("JOB-TRANSIENT", tmp_path / "data" / "jobs" / "JOB-TRANSIENT")
    repo.start_stage("JOB-TRANSIENT", PipelineStage.DIRECTION_LOADED)
    repo.fail_stage(
        "JOB-TRANSIENT",
        PipelineStage.DIRECTION_LOADED,
        "HTTP 503: service temporarily unavailable",
        retryable=False,
    )

    class FailIfLaunched:
        def __init__(self, **_kwargs: object) -> None:
            pytest.fail("worker-start 不得绕过自动重开状态转换")

    monkeypatch.setattr("aicf.cli.WorkerLauncher", FailIfLaunched)

    assert main(["worker-start", "--job", "JOB-TRANSIENT"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "START_REJECTED"
    assert "人工确认" in result["reason"]


def test_worker_start_reports_auto_reopen_failure_without_launching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    repo = JobRepository(tmp_path / "data" / "content.db")
    job_id = "JOB-REOPEN-ERROR"
    repo.create_job(job_id, tmp_path / "data" / "jobs" / job_id)
    repo.start_stage(job_id, PipelineStage.DIRECTION_LOADED)
    repo.complete_stage(job_id, PipelineStage.DIRECTION_LOADED)
    repo.start_stage(job_id, PipelineStage.DIRECTION_ANALYZED)
    repo.fail_stage(
        job_id,
        PipelineStage.DIRECTION_ANALYZED,
        "HTTP 503",
        retryable=False,
        failure_kind=FailureKind.TRANSIENT_EXTERNAL,
    )

    def fail_reopen(*_args: object, **_kwargs: object) -> object:
        raise OSError("snapshot write failed")

    monkeypatch.setattr(JobRepository, "reopen_failed_attention", fail_reopen)
    monkeypatch.setattr(
        "aicf.cli.WorkerLauncher",
        lambda **_kwargs: pytest.fail("重开失败时不得启动Worker"),
    )

    assert main(["worker-start", "--job", job_id]) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "START_REJECTED"
    assert "snapshot write failed" in result["reason"]


def test_worker_run_returns_cooperative_stop_exit_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = SimpleNamespace(
        get_job=lambda job_id: SimpleNamespace(
            job_id=job_id,
            output_dir=str(tmp_path),
            current_stage=None,
            failed_stage=None,
            failure_kind=FailureKind.UNKNOWN,
            stages={},
            next_resume_command=f"python -m aicf resume --job {job_id}",
        )
    )
    calls: list[tuple[str, Path]] = []

    def stopped_worker(job_id: str, job_dir: Path, **_kwargs: object) -> int:
        calls.append((job_id, job_dir))
        return 130

    monkeypatch.setattr("aicf.cli.repository", lambda: repo)
    monkeypatch.setattr("aicf.cli.run_worker", stopped_worker)

    assert main(["worker-run", "--job", "JOB-STOP"]) == 130
    assert calls == [("JOB-STOP", tmp_path)]


def test_worker_run_second_authorization_guard_rejects_ungranted_strategy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    repo = JobRepository(tmp_path / "data" / "content.db")
    repo.create_job("JOB-GUARD", tmp_path / "data" / "jobs" / "JOB-GUARD")
    monkeypatch.setattr(
        "aicf.cli.run_worker",
        lambda *_args, **_kwargs: pytest.fail("未授权策略不得进入Worker"),
    )

    assert main([
        "worker-run",
        "--job",
        "JOB-GUARD",
        "--research-strategy",
        "RETRY_SOURCES",
    ]) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "START_REJECTED"
    assert "研究策略" in result["reason"]


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


def test_cli_resume_and_worker_start_preserve_research_strategy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    repo = JobRepository(tmp_path / "data" / "content.db")
    repo.create_job("JOB-RESEARCH", tmp_path / "data" / "jobs" / "JOB-RESEARCH")
    repo.start_stage("JOB-RESEARCH", PipelineStage.DIRECTION_LOADED)
    repo.complete_stage("JOB-RESEARCH", PipelineStage.DIRECTION_LOADED)
    repo.start_stage("JOB-RESEARCH", PipelineStage.DIRECTION_ANALYZED)
    repo.complete_stage("JOB-RESEARCH", PipelineStage.DIRECTION_ANALYZED)
    repo.start_stage("JOB-RESEARCH", PipelineStage.TOPICS_GENERATED)
    repo.complete_stage("JOB-RESEARCH", PipelineStage.TOPICS_GENERATED)
    repo.start_stage("JOB-RESEARCH", PipelineStage.TOPIC_SELECTED)
    repo.complete_stage("JOB-RESEARCH", PipelineStage.TOPIC_SELECTED)
    repo.start_stage("JOB-RESEARCH", PipelineStage.RESEARCHED)
    repo.fail_stage(
        "JOB-RESEARCH",
        PipelineStage.RESEARCHED,
        "资料不足",
        retryable=True,
    )
    captured: list[str] = []

    class FakeLauncher:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(
            self,
            job_id: str,
            _job_dir: Path,
            *,
            research_strategy: str | None,
            **_kwargs: object,
        ) -> SimpleNamespace:
            assert job_id == "JOB-RESEARCH"
            captured.append(str(research_strategy))
            return SimpleNamespace(
                model_dump_json=lambda **_kwargs: json.dumps(
                    {"job_id": job_id, "pid": 654, "reused": False}
                )
            )

    monkeypatch.setattr("aicf.cli.WorkerLauncher", FakeLauncher)

    assert main([
        "resume",
        "--job",
        "JOB-RESEARCH",
        "--research-strategy",
        "RETRY_SOURCES",
    ]) == 0
    assert json.loads(capsys.readouterr().out)["pid"] == 654
    assert captured == ["RETRY_SOURCES"]


def test_gui_cli_worker_reaches_m2_with_service_authorized_strategy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    repo = JobRepository(tmp_path / "data" / "content.db")
    job_id = "JOB-GUI-CHAIN"
    job_dir = tmp_path / "data" / "jobs" / job_id
    repo.create_job(job_id, job_dir)
    for stage in (
        PipelineStage.DIRECTION_LOADED,
        PipelineStage.DIRECTION_ANALYZED,
        PipelineStage.TOPICS_GENERATED,
        PipelineStage.TOPIC_SELECTED,
    ):
        repo.start_stage(job_id, stage)
        repo.complete_stage(job_id, stage)
    repo.start_stage(job_id, PipelineStage.RESEARCHED)
    repo.fail_stage(
        job_id,
        PipelineStage.RESEARCHED,
        "资料不足",
        retryable=True,
    )
    reached_m2: list[ResearchResumeStrategy | None] = []

    class InlineAutopilot:
        def __init__(self, strategy: ResearchResumeStrategy | None) -> None:
            self.runner = M2ContentRunner(
                SimpleNamespace(),
                repo,
                tmp_path / "outputs",
                source_verifier=object(),
                source_discovery=SimpleNamespace(),
                research_strategy=strategy,
            )

        def run(self, _job_id: str) -> dict[str, str]:
            reached_m2.append(self.runner.research_strategy)
            return {"status": "READY_TO_PUBLISH"}

    monkeypatch.setattr(
        "aicf.cli.build_autopilot",
        lambda _repo, strategy=None: InlineAutopilot(strategy),
    )
    monkeypatch.setattr(
        "aicf.cli.run_worker",
        lambda worker_job_id, _job_dir, *, run_autopilot: (
            0
            if run_autopilot(worker_job_id)["status"] == "READY_TO_PUBLISH"
            else 1
        ),
    )

    class InlineLauncher:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(
            self,
            worker_job_id: str,
            _job_dir: Path,
            *,
            project_root: Path,
            research_strategy: str | None,
        ) -> SimpleNamespace:
            assert project_root == tmp_path
            argv = ["worker-run", "--job", worker_job_id]
            if research_strategy:
                argv.extend(["--research-strategy", research_strategy])
            assert main(argv) == 0
            return SimpleNamespace(
                model_dump_json=lambda **_kwargs: json.dumps(
                    {"job_id": worker_job_id, "status": "STARTED"}
                )
            )

    monkeypatch.setattr("aicf.cli.WorkerLauncher", InlineLauncher)
    gui_command = worker_start_command(
        job_id,
        ResearchResumeStrategy.RETRY_SOURCES,
    )

    assert main(gui_command[3:]) == 0
    assert reached_m2 == [ResearchResumeStrategy.RETRY_SOURCES]


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
        _path: Path,
        _status: object,
    ) -> None:
        raise OSError("磁盘已满")

    monkeypatch.setattr(
        JobRepository,
        "_write_status_locked",
        staticmethod(fail_snapshot),
    )

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


def _register_current_content_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    job_id: str,
    *,
    strategy: ResearchResumeStrategy | None = None,
    identity_offset: int = 0,
) -> JobRepository:
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    config_path = config_dir / "content_direction.yaml"
    if not config_path.exists():
        config_path.write_text("direction: 测试方向\n", encoding="utf-8")
    repo = JobRepository(tmp_path / "data" / "content.db")
    job_dir = tmp_path / "data" / "jobs" / job_id
    repo.create_job(job_id, job_dir)
    identity = get_process_identity(os.getpid())
    assert identity is not None
    instance_id = "content-worker-instance"
    write_worker_record(
        job_dir,
        WorkerRecord(
            job_id=job_id,
            pid=identity.pid,
            started_at="2026-08-20T00:00:00+00:00",
            log_path=str(job_dir / "_work" / "runtime" / "worker.log"),
            instance_id=instance_id,
            process_created_at_ns=identity.created_at_ns + identity_offset,
            process_executable=identity.executable,
            research_strategy=strategy.value if strategy else None,
            ready=True,
        ),
    )
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("AICF_WORKER_LAUNCHED", "1")
    monkeypatch.setenv("AICF_WORKER_INSTANCE_ID", instance_id)
    return repo


def test_cli_content_run_rejects_external_direct_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("AICF_WORKER_LAUNCHED", raising=False)
    monkeypatch.delenv("AICF_WORKER_INSTANCE_ID", raising=False)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "content_direction.yaml").write_text(
        "direction: 测试方向\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "aicf.cli.build_m2_runner",
        lambda *_args, **_kwargs: pytest.fail("外部直跑不得构造M2 Runner"),
    )

    assert main(["content-run", "--job", "M2-DIRECT"]) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "START_REJECTED"
    assert "WorkerLauncher" in result["reason"]


def test_cli_content_run_rejects_worker_record_identity_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _register_current_content_worker(
        tmp_path,
        monkeypatch,
        "M2-IDENTITY",
        identity_offset=1,
    )
    monkeypatch.setattr(
        "aicf.cli.build_m2_runner",
        lambda *_args, **_kwargs: pytest.fail("身份不匹配不得构造M2 Runner"),
    )

    assert main(["content-run", "--job", "M2-IDENTITY"]) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "START_REJECTED"
    assert "进程身份" in result["reason"]


def test_cli_content_run_requires_recorded_research_strategy_consistency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _register_current_content_worker(
        tmp_path,
        monkeypatch,
        "M2-STRATEGY-MISMATCH",
        strategy=ResearchResumeStrategy.INTERNAL_KNOWLEDGE,
    )
    for stage in ORDERED_STAGES:
        repo.start_stage("M2-STRATEGY-MISMATCH", stage)
        if stage == PipelineStage.RESEARCHED:
            break
        repo.complete_stage("M2-STRATEGY-MISMATCH", stage)
    repo.fail_stage(
        "M2-STRATEGY-MISMATCH",
        PipelineStage.RESEARCHED,
        "资料不足",
        retryable=True,
    )
    monkeypatch.setattr(
        "aicf.cli.build_m2_runner",
        lambda *_args, **_kwargs: pytest.fail("策略不一致不得构造M2 Runner"),
    )

    assert main([
        "content-run",
        "--job",
        "M2-STRATEGY-MISMATCH",
        "--research-strategy",
        ResearchResumeStrategy.RETRY_SOURCES.value,
    ]) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "START_REJECTED"
    assert "研究策略" in result["reason"]


def test_cli_content_run_requires_job_service_second_authorization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _register_current_content_worker(
        tmp_path,
        monkeypatch,
        "M2-SERVICE-GUARD",
    )
    repo.start_stage("M2-SERVICE-GUARD", PipelineStage.DIRECTION_LOADED)
    repo.fail_stage(
        "M2-SERVICE-GUARD",
        PipelineStage.DIRECTION_LOADED,
        "需要人工修复",
        retryable=False,
    )
    monkeypatch.setattr(
        "aicf.cli.build_m2_runner",
        lambda *_args, **_kwargs: pytest.fail("服务层拒绝后不得构造M2 Runner"),
    )

    assert main(["content-run", "--job", "M2-SERVICE-GUARD"]) == 2

    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "START_REJECTED"
    assert "人工确认" in result["reason"]


def test_cli_content_run_loads_direction_yaml_and_runs_authorized_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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

    repo = _register_current_content_worker(
        tmp_path,
        monkeypatch,
        "M2CLI001",
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-secret")
    monkeypatch.setattr(
        "aicf.cli.build_m2_runner",
        lambda job_repository, research_strategy: (
            captured.update(
                repository=job_repository,
                research_strategy=research_strategy,
            )
            or FakeRunner()
        ),
    )

    exit_code = main(["content-run", "--job", "M2CLI001"])

    assert exit_code == 0
    authorized_repository = captured.pop("repository")
    assert isinstance(authorized_repository, JobRepository)
    assert authorized_repository.get_job("M2CLI001").job_id == "M2CLI001"
    assert captured == {
        "job_id": "M2CLI001",
        "direction": "从 YAML 读取的方向",
        "research_strategy": None,
    }
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "ready_to_publish"


def test_cli_autopilot_compatibility_command_starts_worker_through_service(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    calls: list[tuple[str, Path, Path, str | None]] = []

    class FakeLauncher:
        def __init__(self, **kwargs: object) -> None:
            assert callable(kwargs["launch_guard"])

        def start(
            self,
            job_id: str,
            job_dir: Path,
            *,
            project_root: Path,
            research_strategy: str | None,
        ) -> SimpleNamespace:
            calls.append((job_id, job_dir, project_root, research_strategy))
            return SimpleNamespace(
                model_dump_json=lambda **_kwargs: json.dumps(
                    {"job_id": job_id, "pid": 123, "reused": False}
                )
            )

    monkeypatch.setattr("aicf.cli.WorkerLauncher", FakeLauncher)
    monkeypatch.setattr(
        "aicf.cli.build_autopilot",
        lambda *_args, **_kwargs: pytest.fail(
            "兼容命令不得在CLI进程中假运行Autopilot"
        ),
    )

    assert main(["autopilot", "--job", "CLI-FULL-001"]) == 0

    assert calls == [
        (
            "CLI-FULL-001",
            tmp_path / "data" / "jobs" / "CLI-FULL-001",
            tmp_path,
            None,
        )
    ]
    repo = JobRepository(tmp_path / "data" / "content.db")
    assert repo.get_job("CLI-FULL-001").job_id == "CLI-FULL-001"
    assert json.loads(capsys.readouterr().out)["pid"] == 123


def test_cli_resume_compatibility_command_starts_worker_through_service(
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

    class FakeLauncher:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(
            self,
            job_id: str,
            _job_dir: Path,
            **_kwargs: object,
        ) -> SimpleNamespace:
            calls.append(job_id)
            return SimpleNamespace(
                model_dump_json=lambda **_kwargs: json.dumps(
                    {"job_id": job_id, "pid": 456, "reused": False}
                )
            )

    monkeypatch.setattr("aicf.cli.WorkerLauncher", FakeLauncher)
    monkeypatch.setattr(
        "aicf.cli.build_autopilot",
        lambda *_args, **_kwargs: pytest.fail(
            "resume不得在CLI进程中直接运行Autopilot"
        ),
    )

    assert main(["resume", "--job", "CLI-RESUME-001"]) == 0

    assert calls == ["CLI-RESUME-001"]
    assert json.loads(capsys.readouterr().out)["pid"] == 456


def test_cli_retry_rejects_mismatched_stage_without_mutating_or_launching(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    repo = JobRepository(tmp_path / "data" / "content.db")
    job_id = "CLI-RETRY-MISMATCH"
    repo.create_job(job_id, tmp_path / "data" / "jobs" / job_id)
    repo.start_stage(job_id, PipelineStage.DIRECTION_LOADED)
    repo.fail_stage(
        job_id,
        PipelineStage.DIRECTION_LOADED,
        "temporary",
        retryable=True,
    )
    monkeypatch.setattr(
        "aicf.cli.WorkerLauncher",
        lambda **_kwargs: pytest.fail("阶段不匹配时不得启动Worker"),
    )

    assert main([
        "retry",
        "--job",
        job_id,
        "--stage",
        PipelineStage.RENDERED.value,
    ]) == 2

    persisted = repo.get_job(job_id)
    assert persisted.current_stage == PipelineStage.FAILED_RETRYABLE
    assert persisted.failed_stage == PipelineStage.DIRECTION_LOADED
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "START_REJECTED"
    assert PipelineStage.RENDERED.value in result["reason"]
    assert PipelineStage.DIRECTION_LOADED.value in result["reason"]


def test_cli_retry_matching_stage_launches_before_any_state_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AICF_PROJECT_ROOT", str(tmp_path))
    repo = JobRepository(tmp_path / "data" / "content.db")
    job_id = "CLI-RETRY-MATCH"
    repo.create_job(job_id, tmp_path / "data" / "jobs" / job_id)
    repo.start_stage(job_id, PipelineStage.DIRECTION_LOADED)
    repo.fail_stage(
        job_id,
        PipelineStage.DIRECTION_LOADED,
        "temporary",
        retryable=True,
    )
    observed: list[tuple[PipelineStage, PipelineStage | None]] = []

    class FakeLauncher:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def start(self, job_id: str, _job_dir: Path, **_kwargs: object) -> SimpleNamespace:
            current = repo.get_job(job_id)
            observed.append((current.current_stage, current.failed_stage))
            return SimpleNamespace(
                model_dump_json=lambda **_kwargs: json.dumps(
                    {"job_id": job_id, "pid": 789, "reused": False}
                )
            )

    monkeypatch.setattr("aicf.cli.WorkerLauncher", FakeLauncher)

    assert main([
        "retry",
        "--job",
        job_id,
        "--stage",
        PipelineStage.DIRECTION_LOADED.value,
    ]) == 0

    assert observed == [
        (PipelineStage.FAILED_RETRYABLE, PipelineStage.DIRECTION_LOADED)
    ]
    assert json.loads(capsys.readouterr().out)["pid"] == 789
