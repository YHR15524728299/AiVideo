from __future__ import annotations

from aicf.engines.llm_engine import StructuredEngine
from aicf.models.contracts import (
    DirectionProfile,
    ResearchResult,
    ReviewResult,
    ScriptResult,
)


class ReviewEngine(StructuredEngine):
    stage = "review"
    result_model = ReviewResult
    system_prompt = (
        "你是严格的内容审稿人。检查方向匹配、钩子、表达、事实证据和安全性。"
        "任何无来源精确数字、虚构引文或与方向冲突都必须判定不通过并给出可执行修订指令。"
    )

    def review(
        self,
        profile: DirectionProfile,
        research: ResearchResult,
        script: ScriptResult,
        *,
        review_attempt_id: str = "legacy",
    ) -> ReviewResult:
        return self.generate(
            {
                "direction_profile": profile.model_dump(mode="json"),
                "research": research.model_dump(mode="json"),
                "script": script.model_dump(mode="json"),
                "review_attempt_id": review_attempt_id,
            }
        )
