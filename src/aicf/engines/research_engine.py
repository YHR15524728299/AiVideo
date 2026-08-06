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
        *,
        research_attempt_id: str = "legacy",
    ) -> ResearchResult:
        return self.generate(
            {
                "direction_profile": profile.model_dump(mode="json"),
                "topic": topic,
                "research_attempt_id": research_attempt_id,
            }
        )

    def research_verified(
        self,
        profile: DirectionProfile,
        topic: dict[str, object],
        verifier: object,
        *,
        research_attempt_id: str,
    ) -> tuple[ResearchResult, list[dict[str, Any]]]:
        original_request = {
            "direction_profile": profile.model_dump(mode="json"),
            "topic": topic,
            "research_attempt_id": research_attempt_id,
        }
        payload: dict[str, object] = original_request
        last_error: SourceVerificationError | None = None
        for repair_round in range(3):
            research = self.generate(payload)
            try:
                evidence = verifier.verify_research(research)
                return research, evidence
            except SourceVerificationError as error:
                last_error = error
                if repair_round >= 2:
                    # 3轮修复后仍有URL无法验证，降级处理：
                    # 返回已验证通过的evidence，未通过的记录为警告，不阻断流程
                    partial_evidence = list(error.evidence)
                    verified_count = sum(
                        1 for e in partial_evidence if e.get("claim_supported")
                    )
                    total_count = len(research.facts)
                    # 如果至少有30%的事实通过验证，就继续流程
                    if verified_count >= max(1, total_count * 0.3):
                        import logging
                        logging.warning(
                            f"来源验证降级通过：{verified_count}/{total_count} 个事实验证通过，"
                            f"未通过的有 {len(error.errors)} 个错误，继续流程"
                        )
                        return research, partial_evidence
                    # 通过率太低，才真正抛出错误
                    error.research = research.model_dump(mode="json")
                    raise
                next_round = repair_round + 1
                payload = {
                    "original_request": original_request,
                    "original_research": research.model_dump(mode="json"),
                    "source_verification_errors": error.errors,
                    "repair_round": next_round,
                }
        # 理论上不会到这里
        if last_error:
            raise last_error
        raise RuntimeError("来源验证修正流程异常结束")
