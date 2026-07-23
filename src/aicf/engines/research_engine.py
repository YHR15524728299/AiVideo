from __future__ import annotations

from typing import Any

from aicf.engines.llm_engine import StructuredEngine
from aicf.models.contracts import DirectionProfile, ResearchResult
from aicf.source_verifier import SourceVerificationError


class ResearchEngine(StructuredEngine):
    stage = "research"
    result_model = ResearchResult
    system_prompt = (
        "你是事实研究员。围绕选题整理可用于脚本的事实；每条事实必须带标题、URL 和"
        "置信度。优先使用政府、标准组织、大学、项目官方文档或厂商官方文档等一手"
        "官方来源。URL 必须是可公开访问的 HTTP(S) 正文页面，claim 必须能由来源"
        "正文中的中文或英文关键词直接支持。无法确认的内容放入 unknowns，禁止编造"
        "精确数字、引文或来源。收到 source_verification_errors 时，必须逐项更换"
        "不可达来源或收窄不受正文支持的 claim。"
    )

    def research(
        self,
        profile: DirectionProfile,
        topic: dict[str, object],
    ) -> ResearchResult:
        return self.generate(
            {"direction_profile": profile.model_dump(mode="json"), "topic": topic}
        )

    def research_verified(
        self,
        profile: DirectionProfile,
        topic: dict[str, object],
        verifier: object,
    ) -> tuple[ResearchResult, list[dict[str, Any]]]:
        original_request = {
            "direction_profile": profile.model_dump(mode="json"),
            "topic": topic,
        }
        payload: dict[str, object] = original_request
        for repair_round in range(3):
            research = self.generate(payload)
            try:
                evidence = verifier.verify_research(research)
                return research, evidence
            except SourceVerificationError as error:
                if repair_round >= 2:
                    error.research = research.model_dump(mode="json")
                    raise
                next_round = repair_round + 1
                payload = {
                    "original_request": original_request,
                    "original_research": research.model_dump(mode="json"),
                    "source_verification_errors": error.errors,
                    "repair_round": next_round,
                }
        raise RuntimeError("来源验证修正流程异常结束")
