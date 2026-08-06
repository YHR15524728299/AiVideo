from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from aicf.engines.llm_engine import StructuredEngine
from aicf.models.contracts import DirectionProfile, ResearchResult
from aicf.research_policy import (
    FreshnessRequirement,
    ResearchPolicy,
    SourceFailureKind,
)
from aicf.source_discovery import SourceCandidate
from aicf.source_verifier import SourceVerificationError


class ResearchEngine(StructuredEngine):
    stage = "research"
    result_model = ResearchResult
    system_prompt = (
        "你是事实研究员。围绕选题整理可用于脚本的事实；每条事实必须带标题、URL 和"
        "置信度。优先使用政府、标准组织、大学、项目官方文档或厂商官方文档等一手"
        "官方来源。URL 必须是可公开访问的 HTTP(S) 正文页面，claim 必须能由来源"
        "正文中的中文或英文关键词直接支持。无法确认的内容放入 unknowns，禁止编造"
        "精确数字、引文或来源。当请求包含 source_candidates 时，source_url 必须"
        "逐字选自候选列表，禁止新增、改写或猜测 URL；同时原样填写候选来源的"
        " published_at 和 source_type。收到 source_verification_errors 时，必须"
        "逐项更换不可达来源或收窄不受正文支持的 claim。"
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
        source_candidates: list[SourceCandidate] | None = None,
        freshness: FreshnessRequirement | None = None,
        policy: ResearchPolicy | None = None,
    ) -> tuple[ResearchResult, list[dict[str, Any]]]:
        freshness = freshness or FreshnessRequirement(required=False)
        candidate_payload = [
            {
                "url": candidate.url,
                "title": candidate.title,
                "published_at": (
                    candidate.published_at.isoformat()
                    if candidate.published_at
                    else None
                ),
                "source_type": candidate.source_type,
                "core_eligible": candidate.core_eligible,
            }
            for candidate in source_candidates or []
        ]
        original_request = {
            "direction_profile": profile.model_dump(mode="json"),
            "topic": topic,
            "research_attempt_id": research_attempt_id,
        }
        if source_candidates is not None:
            original_request.update({
                "source_candidates": candidate_payload,
                "freshness_required": freshness.required,
                "cutoff_date": (
                    freshness.cutoff_date.isoformat()
                    if freshness.cutoff_date
                    else None
                ),
            })
        payload: dict[str, object] = original_request
        last_error: SourceVerificationError | None = None
        for repair_round in range(3):
            research = self.generate(payload)
            if source_candidates is not None:
                allowed_urls = {candidate.url for candidate in source_candidates}
                candidate_by_url = {
                    candidate.url: candidate for candidate in source_candidates
                }
                invented_urls = sorted({
                    fact.source_url
                    for fact in research.facts
                    if fact.source_url not in allowed_urls
                })
                if invented_urls:
                    raise SourceVerificationError(
                        "研究结果引用了候选来源之外的 URL："
                        + "、".join(invented_urls),
                    )
                for fact in research.facts:
                    candidate = candidate_by_url[fact.source_url]
                    fact.published_at = candidate.published_at
                    fact.source_type = candidate.source_type
                if freshness.required and freshness.cutoff_date is not None:
                    stale = [
                        fact.source_url
                        for fact in research.facts
                        if (
                            candidate_by_url[fact.source_url].published_at is None
                            or candidate_by_url[fact.source_url].published_at
                            < freshness.cutoff_date
                            or not candidate_by_url[fact.source_url].core_eligible
                        )
                    ]
                    if stale:
                        raise SourceVerificationError(
                            "核心资料时效不足：" + "、".join(stale),
                            evidence=[{
                                "original_url": url,
                                "claim_supported": False,
                                "category": (
                                    SourceFailureKind.INSUFFICIENT_FRESHNESS.value
                                ),
                            } for url in stale],
                        )
            try:
                evidence = verifier.verify_research(research)
                if policy is not None and source_candidates is not None:
                    verified = sum(
                        1 for item in evidence
                        if item.get("claim_supported")
                    )
                    candidate_types = {
                        candidate.url: candidate.source_type
                        for candidate in source_candidates
                    }
                    authoritative = len({
                        fact.source_url
                        for fact in research.facts
                        if candidate_types.get(fact.source_url) == "official"
                    })
                    independent = len({
                        (urlparse(fact.source_url).hostname or "").casefold()
                        for fact in research.facts
                    })
                    if not policy.accepts(
                        verified=verified,
                        total=len(research.facts),
                        authoritative=authoritative,
                        independent=independent,
                    ):
                        raise SourceVerificationError(
                            "资料数量或验证通过比例不足",
                            evidence=evidence + [{
                                "claim_supported": False,
                                "category": (
                                    SourceFailureKind.INSUFFICIENT_EVIDENCE.value
                                ),
                                "verified": verified,
                                "total": len(research.facts),
                                "authoritative": authoritative,
                            }],
                        )
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
                    if (
                        policy is None
                        and verified_count >= max(1, total_count * 0.3)
                    ):
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
