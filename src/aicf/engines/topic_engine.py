from __future__ import annotations

import re
from collections.abc import Mapping

from aicf.engines.llm_engine import StructuredEngine
from aicf.models.contracts import DirectionProfile, TopicCandidate, TopicCandidates


class TopicGenerationEngine(StructuredEngine):
    stage = "topics"
    result_model = TopicCandidates
    system_prompt = (
        "你是短视频选题策划。严格生成 8 到 10 个候选选题，覆盖方向内容支柱与受众问题，"
        "逐项给出 0-100 分。overall_score 可先给估计值，本地将重新排序。"
    )

    def generate_candidates(
        self,
        profile: DirectionProfile,
        *,
        count: int,
    ) -> list[TopicCandidate]:
        result = self.generate(
            {
                "direction_profile": profile.model_dump(mode="json"),
                "candidate_count": max(8, min(10, count)),
            }
        )
        return result.candidates


def _normalized(value: object) -> str:
    return re.sub(r"[\W_]+", "", str(value).casefold(), flags=re.UNICODE)


def _is_duplicate(
    topic: Mapping[str, object],
    recent_history: list[Mapping[str, object]],
) -> bool:
    fields = ("title", "hook", "core_claim")
    signature = tuple(_normalized(topic.get(field, "")) for field in fields)
    return any(
        signature == tuple(_normalized(item.get(field, "")) for field in fields)
        for item in recent_history[-5:]
    )


def rank_topics(
    topics: list[Mapping[str, object]],
    recent_history: list[Mapping[str, object]],
) -> list[dict[str, object]]:
    ranked: list[dict[str, object]] = []
    for source in topics:
        topic = dict(source)
        duplicate = _is_duplicate(topic, recent_history)
        score = (
            float(topic.get("direction_relevance", 0)) * 0.25
            + float(topic.get("hook_strength", 0)) * 0.20
            + float(topic.get("visual_potential", 0)) * 0.20
            + float(topic.get("novelty", 0)) * 0.15
            + float(topic.get("evidence_availability", 0)) * 0.10
            + (100 - float(topic.get("production_difficulty", 100))) * 0.10
            - float(topic.get("fact_risk", 0)) * 0.20
        )
        topic["duplicate"] = duplicate
        topic["overall_score"] = 0.0 if duplicate else round(max(0.0, min(100.0, score)), 2)
        ranked.append(topic)
    return sorted(ranked, key=lambda item: float(item["overall_score"]), reverse=True)
