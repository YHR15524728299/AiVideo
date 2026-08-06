from datetime import date

from aicf.research_policy import FreshnessRequirement
from aicf.source_discovery import (
    BingRSSSearchProvider,
    SourceCandidate,
    SourceDiscovery,
)
from aicf.source_verifier import SourceVerificationError


def test_bing_rss_provider_parses_real_result_shape() -> None:
    xml = b"""<?xml version="1.0"?>
    <rss><channel><item>
      <title>Federal Reserve policy statement</title>
      <link>https://www.federalreserve.gov/newsevents/pressreleases/a.htm</link>
      <pubDate>Wed, 29 Jul 2026 12:00:00 GMT</pubDate>
    </item></channel></rss>"""
    provider = BingRSSSearchProvider(fetcher=lambda _url, _timeout: xml)

    results = provider.search("Federal Reserve policy", limit=5)

    assert results == [
        SourceCandidate(
            url="https://www.federalreserve.gov/newsevents/pressreleases/a.htm",
            title="Federal Reserve policy statement",
            published_at=date(2026, 7, 29),
            source_type="official",
            query="Federal Reserve policy",
        )
    ]


def test_discovery_omits_rejected_duplicate_and_unreachable_candidates() -> None:
    candidates = [
        SourceCandidate("https://example.com/rejected", "Rejected"),
        SourceCandidate("https://example.com/good", "Good"),
        SourceCandidate("https://example.com/good#section", "Duplicate"),
        SourceCandidate("https://example.com/missing", "Missing"),
    ]

    class Provider:
        def search(self, query: str, *, limit: int) -> list[SourceCandidate]:
            return candidates

    def preflight(candidate: SourceCandidate) -> dict[str, object]:
        if "missing" in candidate.url:
            raise SourceVerificationError("URL HTTP 404")
        return {"final_url": candidate.url}

    discovered = SourceDiscovery(
        Provider(),
        preflight=preflight,
    ).discover(
        queries=["test query"],
        freshness=FreshnessRequirement(required=False),
        rejected_urls={"https://example.com/rejected"},
        limit=10,
    )

    assert [item.url for item in discovered.candidates] == [
        "https://example.com/good"
    ]
    assert discovered.rejections == [{
        "url": "https://example.com/missing",
        "category": "PERMANENT_SOURCE_FAILURE",
        "reason": "URL HTTP 404",
    }]


def test_discovery_marks_old_candidate_as_non_core() -> None:
    candidate = SourceCandidate(
        "https://example.com/old",
        "Old",
        published_at=date(2024, 1, 1),
    )

    class Provider:
        def search(self, query: str, *, limit: int) -> list[SourceCandidate]:
            return [candidate]

    discovered = SourceDiscovery(
        Provider(),
        preflight=lambda item: {"final_url": item.url},
    ).discover(
        queries=["current topic"],
        freshness=FreshnessRequirement(
            required=True,
            cutoff_date=date(2025, 8, 6),
        ),
        rejected_urls=set(),
        limit=5,
    )

    assert discovered.candidates[0].core_eligible is False


def test_discovery_uses_preflight_published_date_when_search_has_none() -> None:
    candidate = SourceCandidate("https://example.com/current", "Current")

    class Provider:
        def search(self, query: str, *, limit: int) -> list[SourceCandidate]:
            return [candidate]

    result = SourceDiscovery(
        Provider(),
        preflight=lambda item: {
            "final_url": item.url,
            "published_at": "2026-07-29",
        },
    ).discover(
        queries=["current"],
        freshness=FreshnessRequirement(
            required=True,
            cutoff_date=date(2025, 8, 6),
        ),
        rejected_urls=set(),
        limit=5,
    )

    assert result.candidates[0].published_at == date(2026, 7, 29)
    assert result.candidates[0].core_eligible is True


def test_official_source_detection_rejects_spoofed_domains() -> None:
    assert (
        BingRSSSearchProvider._source_type("https://who.int/report")
        == "official"
    )
    assert (
        BingRSSSearchProvider._source_type("https://who.int.example.org/report")
        == "web"
    )
    assert (
        BingRSSSearchProvider._source_type("https://report.gov.example.com")
        == "web"
    )
