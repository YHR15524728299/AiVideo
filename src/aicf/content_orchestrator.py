from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any, Callable

from aicf.atomic_io import atomic_replace
from aicf.engines.direction_engine import DirectionEngine
from aicf.engines.llm_engine import StructuredClient
from aicf.engines.package_engine import PackageCopyEngine
from aicf.engines.research_engine import ResearchEngine
from aicf.engines.review_engine import ReviewEngine
from aicf.engines.script_engine import ScriptEngine, render_script_markdown
from aicf.m2_promotion import M2PromotionManager
from aicf.models.contracts import SUPPORTED_PLATFORMS


M2_MANAGED_FILES = {
    "direction.json",
    "topic.json",
    "research.json",
    "script.json",
    "script.md",
    "review.json",
    "package.json",
    "publish.json",
    "usage.json",
    "manifest.json",
}


class ContentOrchestrator:
    def __init__(
        self,
        client: StructuredClient,
        output_dir: str | Path,
        *,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.client = client
        self.output_dir = Path(output_dir)
        self._active_output_dir = self.output_dir
        self._promotion = M2PromotionManager(
            self.output_dir,
            M2_MANAGED_FILES,
            fault_injector,
        )
        self._promotion.recover()
        self.direction_engine = DirectionEngine(client)
        self.research_engine = ResearchEngine(client)
        self.script_engine = ScriptEngine(client)
        self.review_engine = ReviewEngine(client)
        self.package_engine = PackageCopyEngine(client)

    def run(
        self,
        *,
        direction: dict[str, object],
        selected_topic: dict[str, object],
    ) -> dict[str, Any]:
        platforms = self._validate_platforms(direction.get("platforms", []))
        run_id = uuid.uuid4().hex
        self.output_dir.parent.mkdir(parents=True, exist_ok=True)
        staging = self.output_dir.parent / (
            f".{self.output_dir.name}.staging-{uuid.uuid4().hex}"
        )
        staging.mkdir()
        self._active_output_dir = staging
        try:
            manifest = self._execute(direction, selected_topic, platforms)
            if manifest["status"] != "ready_to_publish":
                self._archive_run(staging, run_id, manifest, publishable=False)
                return manifest
            self._promotion.promote(staging)
            self._write_run_status(
                run_id,
                status="ready_to_publish",
                publishable=True,
                manifest=manifest,
            )
            return manifest
        except Exception as error:
            shutil.rmtree(staging, ignore_errors=True)
            self._write_run_status(
                run_id,
                status="generation_failed",
                publishable=False,
                error=str(error),
            )
            raise
        finally:
            self._active_output_dir = self.output_dir

    def _execute(
        self,
        direction: dict[str, object],
        selected_topic: dict[str, object],
        platforms: list[str],
    ) -> dict[str, Any]:
        profile = self.direction_engine.analyze(direction)
        self._write_json("direction.json", profile.model_dump(mode="json"))
        self._write_json("topic.json", selected_topic)

        research = self.research_engine.research(profile, selected_topic)
        self._write_json("research.json", research.model_dump(mode="json"))
        script = self.script_engine.write(profile, selected_topic, research)
        self._write_json("script.json", script.model_dump(mode="json"))
        self._write_text("script.md", render_script_markdown(script))
        review = self.review_engine.review(profile, research, script)
        self._write_json("review.json", review.model_dump(mode="json"))

        if not review.passed:
            manifest = {
                "status": "needs_revision",
                "topic_id": selected_topic.get("topic_id"),
                "issues": review.issues,
                "revision_instructions": review.revision_instructions,
                "usage": self._usage(),
                "files": self._file_names(),
            }
            self._write_json("usage.json", manifest["usage"])
            manifest["files"] = self._file_names() + ["manifest.json"]
            self._write_json("manifest.json", manifest)
            return manifest

        package = self.package_engine.package(script, review, platforms)
        package_data = package.model_dump(mode="json", exclude_none=True)
        self._write_json("package.json", package_data)
        publish = {
            "status": "ready_to_publish",
            "topic_id": selected_topic.get("topic_id"),
            "platforms": {
                platform: package_data[platform]
                for platform in platforms
                if platform in package_data
            },
        }
        self._write_json("publish.json", publish)
        usage = self._usage()
        self._write_json("usage.json", usage)
        manifest = {
            "status": "ready_to_publish",
            "topic_id": selected_topic.get("topic_id"),
            "usage": usage,
            "files": self._file_names() + ["manifest.json"],
        }
        self._write_json("manifest.json", manifest)
        return manifest

    @staticmethod
    def _validate_platforms(value: object) -> list[str]:
        if not isinstance(value, list):
            raise ValueError("platforms 必须是列表")
        platforms = [str(item) for item in value]
        actual = set(platforms)
        supported = set(SUPPORTED_PLATFORMS)
        if actual != supported:
            missing = sorted(supported - actual)
            unsupported = sorted(actual - supported)
            details = []
            if missing:
                details.append("缺少平台: " + ", ".join(missing))
            if unsupported:
                details.append("不支持的平台: " + ", ".join(unsupported))
            details.append("platforms 必须精确包含: " + ", ".join(SUPPORTED_PLATFORMS))
            raise ValueError("; ".join(details))
        return platforms

    def _archive_run(
        self,
        staging: Path,
        run_id: str,
        manifest: dict[str, Any],
        *,
        publishable: bool,
    ) -> None:
        run_dir = self.output_dir / "m2_runs" / run_id
        run_dir.parent.mkdir(parents=True, exist_ok=True)
        atomic_replace(staging, run_dir)
        self._write_run_status(
            run_id,
            status=str(manifest["status"]),
            publishable=publishable,
            manifest=manifest,
        )

    def _write_run_status(
        self,
        run_id: str,
        *,
        status: str,
        publishable: bool,
        manifest: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        run_dir = self.output_dir / "m2_runs" / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        value: dict[str, object] = {
            "run_id": run_id,
            "status": status,
            "publishable": publishable,
        }
        if manifest is not None:
            value["manifest"] = manifest
        if error is not None:
            value["error"] = error
        target = run_dir / "run.json"
        temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        atomic_replace(temporary, target)

    def _usage(self) -> dict[str, int]:
        usage = getattr(self.client, "usage", None)
        return {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0)),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0)),
            "total_tokens": int(getattr(usage, "total_tokens", 0)),
        }

    def _file_names(self) -> list[str]:
        return sorted(
            path.name for path in self._active_output_dir.iterdir() if path.is_file()
        )

    def _write_json(self, name: str, value: object) -> None:
        self._write_text(
            name,
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        )

    def _write_text(self, name: str, value: str) -> None:
        target = self._active_output_dir / name
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(value, encoding="utf-8")
        atomic_replace(temporary, target)
