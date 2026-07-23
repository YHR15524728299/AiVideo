from __future__ import annotations

from aicf.engines.llm_engine import StructuredEngine
from aicf.models.contracts import PackageResult, ReviewResult, ScriptResult


class PackageCopyEngine(StructuredEngine):
    stage = "package"
    result_model = PackageResult
    system_prompt = (
        "你是多平台短视频文案编辑。为指定平台生成克制、准确、可发布的标题、简介和标签。"
        "不得引入脚本与审核结果之外的新事实，不得使用夸张承诺。"
    )

    def package(
        self,
        script: ScriptResult,
        review: ReviewResult,
        platforms: list[str],
    ) -> PackageResult:
        return self.generate(
            {
                "script": script.model_dump(mode="json"),
                "review": review.model_dump(mode="json"),
                "platforms": platforms,
            }
        )
