from datetime import date

import pytest

from aicf.engines.research_engine import ResearchEngine
from aicf.models.contracts import DirectionProfile
from aicf.providers.openrouter import StructuredResult, TokenUsage
from aicf.research_policy import FreshnessRequirement, ResearchPolicy
from aicf.source_discovery import SourceCandidate
from aicf.source_verifier import SourceVerificationError


class Client:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def call_structured(self, **kwargs: object) -> StructuredResult:
        self.calls.append(kwargs)
        return StructuredResult(
            data=self.response,
            usage=TokenUsage(),
            cached=False,
            model="test/model",
        )


class Verifier:
    def __init__(self) -> None:
        self.called = False

    def verify_research(self, research: object) -> list[dict[str, object]]:
        self.called = True
        return [
            {
                "original_url": fact.source_url,
                "final_url": fact.source_url,
                "claim_supported": True,
            }
            for fact in research.facts
        ]


def _profile() -> DirectionProfile:
    return DirectionProfile(
        series_name="全球主线",
        core_direction="解释宏观经济变化",
        audience="中文观众",
        content_goal="提供可验证的财经解释",
        content_pillars=["财经"],
        tone=["专业"],
        visual_style="数据图表",
    )


def _research(urls: list[str]) -> dict[str, object]:
    return {
        "summary": "研究摘要",
        "facts": [
            {
                "claim": f"事实 {index}",
                "source_title": f"来源 {index}",
                "source_url": url,
                "confidence": 0.9,
                "published_at": "2026-07-01",
                "source_type": "official" if index == 1 else "web",
            }
            for index, url in enumerate(urls, start=1)
        ],
        "unknowns": [],
    }


def test_research_rejects_url_outside_discovered_allowlist() -> None:
    client = Client(_research(["https://invented.example.com/article"]))
    verifier = Verifier()

    with pytest.raises(SourceVerificationError, match="候选来源"):
        ResearchEngine(client).research_verified(
            _profile(),
            {"title": "全球主线"},
            verifier,
            research_attempt_id="attempt-1",
            source_candidates=[
                SourceCandidate(
                    "https://official.example.com/article",
                    "Official",
                )
            ],
            freshness=FreshnessRequirement(required=False),
            policy=ResearchPolicy(),
        )

    assert verifier.called is False


def test_research_accepts_five_verified_candidates_with_authoritative_source() -> None:
    urls = [f"https://example.com/{index}" for index in range(5)]
    client = Client(_research(urls))
    verifier = Verifier()
    candidates = [
        SourceCandidate(
            url,
            f"Source {index}",
            published_at=date(2026, 7, 1),
            source_type="official" if index == 0 else "web",
        )
        for index, url in enumerate(urls)
    ]

    research, evidence = ResearchEngine(client).research_verified(
        _profile(),
        {"title": "全球主线"},
        verifier,
        research_attempt_id="attempt-1",
        source_candidates=candidates,
        freshness=FreshnessRequirement(
            required=True,
            cutoff_date=date(2025, 8, 6),
        ),
        policy=ResearchPolicy(),
    )

    assert len(research.facts) == 5
    assert len(evidence) == 5
    request = client.calls[0]["user_payload"]
    assert len(request["source_candidates"]) == 5
    assert request["cutoff_date"] == "2025-08-06"


def test_research_freshness_uses_candidate_date_not_model_claimed_date() -> None:
    url = "https://example.com/old"
    client = Client(_research([url] * 5))
    candidates = [
        SourceCandidate(
            url,
            "Old source",
            published_at=date(2024, 1, 1),
            source_type="official",
            core_eligible=False,
        )
    ]

    with pytest.raises(SourceVerificationError, match="时效不足"):
        ResearchEngine(client).research_verified(
            _profile(),
            {"title": "当前全球主线"},
            Verifier(),
            research_attempt_id="attempt-1",
            source_candidates=candidates,
            freshness=FreshnessRequirement(
                required=True,
                cutoff_date=date(2025, 8, 6),
            ),
            policy=ResearchPolicy(),
        )
