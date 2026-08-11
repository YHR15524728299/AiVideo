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
    
    # 默认提示词（有外部来源时使用）
    system_prompt = (
        "你是严格的内容审稿人。检查方向匹配、钩子、表达、事实证据和安全性。"
        "任何无来源精确数字、虚构引文或与方向冲突都必须判定不通过并给出可执行修订指令。"
    )
    
    # 内部知识模式提示词（无外部来源时使用）
    _internal_knowledge_prompt = (
        "你是严格的内容审稿人。当前为【内部知识模式】，所有研究事实基于LLM内部知识，"
        "无外部URL来源。检查以下内容：\n"
        "1. 方向匹配：内容必须紧扣内容方向，不能跑题\n"
        "2. 钩子效果：开头钩子要吸引人，符合视频类型要求\n"
        "3. 表达流畅：脚本口语化，适合朗读，无晦涩长句\n"
        "4. 逻辑自洽：内容前后一致，无自相矛盾，概念解释清晰\n"
        "5. 安全性：无违规、敏感、虚假误导内容\n"
        "注意：不要检查'无来源精确数字'问题（本模式允许基于常识的合理表述），"
        "不要要求提供外部URL来源。只有当内容与方向严重冲突、逻辑明显混乱、"
        "或存在安全问题时才判定不通过。对于表述不流畅等小问题，尽量判定通过。"
        "给出的修订指令必须具体、可执行。"
    )

    def review(
        self,
        profile: DirectionProfile,
        research: ResearchResult,
        script: ScriptResult,
        *,
        review_attempt_id: str = "legacy",
    ) -> ReviewResult:
        # 检测是否为内部知识模式：所有facts的source_url为空
        has_external_sources = any(
            fact.source_url and fact.source_url.strip()
            for fact in research.facts
        )
        
        # 根据模式选择提示词
        if has_external_sources:
            active_prompt = self.system_prompt
        else:
            active_prompt = self._internal_knowledge_prompt
        
        payload = {
            "direction_profile": profile.model_dump(mode="json"),
            "research": research.model_dump(mode="json"),
            "script": script.model_dump(mode="json"),
            "review_attempt_id": review_attempt_id,
            "internal_knowledge_mode": not has_external_sources,
        }
        
        # 如果是内部知识模式，临时修改system_prompt
        if not has_external_sources:
            original_prompt = self.system_prompt
            self.system_prompt = active_prompt
            try:
                return self.generate(payload)
            finally:
                self.system_prompt = original_prompt
        else:
            return self.generate(payload)
