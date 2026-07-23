from __future__ import annotations

from aicf.engines.llm_engine import StructuredEngine
from aicf.models.contracts import DirectionProfile, ResearchResult, ScriptResult


class ScriptEngine(StructuredEngine):
    stage = "script"
    result_model = ScriptResult
    system_prompt = (
        "你是竖屏知识短视频编剧。按方向、选题和研究材料写有钩子、解释与结论的脚本。"
        "事实引用使用 research.facts 的零基索引；不得把 unknowns 写成事实。"
    )

    def write(
        self,
        profile: DirectionProfile,
        topic: dict[str, object],
        research: ResearchResult,
    ) -> ScriptResult:
        return self.generate(
            {
                "direction_profile": profile.model_dump(mode="json"),
                "topic": topic,
                "research": research.model_dump(mode="json"),
            }
        )


class ScriptRevisionEngine(StructuredEngine):
    stage = "script_revision"
    result_model = ScriptResult
    system_prompt = (
        "你是竖屏知识短视频修稿编辑。只按修订指令修改脚本，"
        "不得引入研究材料之外的新事实，保留有效内容并返回完整脚本。"
    )

    def revise(
        self,
        profile: DirectionProfile,
        research: ResearchResult,
        script: ScriptResult,
        revision_instructions: list[str],
    ) -> ScriptResult:
        return self.generate(
            {
                "direction_profile": profile.model_dump(mode="json"),
                "research": research.model_dump(mode="json"),
                "script": script.model_dump(mode="json"),
                "revision_instructions": revision_instructions,
            }
        )

    def revise_for_duration(
        self,
        profile: DirectionProfile,
        research: ResearchResult,
        script: ScriptResult,
        *,
        actual_duration_seconds: float,
        min_duration_seconds: float,
        max_duration_seconds: float,
        target_duration_seconds: float,
        suggested_action: str,
    ) -> ScriptResult:
        return self.generate(
            {
                "direction_profile": profile.model_dump(mode="json"),
                "research": research.model_dump(mode="json"),
                "script": script.model_dump(mode="json"),
                "duration_revision": {
                    "actual_duration_seconds": actual_duration_seconds,
                    "min_duration_seconds": min_duration_seconds,
                    "max_duration_seconds": max_duration_seconds,
                    "target_duration_seconds": target_duration_seconds,
                    "suggested_action": suggested_action,
                    "target_ratio": (
                        target_duration_seconds / actual_duration_seconds
                    ),
                },
            }
        )


def render_script_markdown(script: ScriptResult) -> str:
    lines = [f"# {script.title}", "", f"> {script.hook}", ""]
    for segment in script.segments:
        lines.extend(
            [
                f"## {segment.segment_id} · {segment.purpose}",
                "",
                segment.narration,
                "",
                f"- 画面：{segment.visual_brief}",
                f"- 事实引用：{segment.fact_refs or '无'}",
                "",
            ]
        )
    lines.extend(
        [
            "## 收束",
            "",
            script.call_to_action,
            "",
            f"预计时长：{script.estimated_duration_seconds:g} 秒",
            "",
        ]
    )
    return "\n".join(lines)
