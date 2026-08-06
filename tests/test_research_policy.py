from datetime import date

from aicf.research_policy import (
    ResearchPolicy,
    SourceFailureKind,
    classify_source_error,
    derive_freshness,
)


def test_classify_source_errors_for_user_recovery() -> None:
    assert (
        classify_source_error("URL HTTP 404")
        == SourceFailureKind.PERMANENT_SOURCE_FAILURE
    )
    assert (
        classify_source_error("URL HTTP 503")
        == SourceFailureKind.TEMPORARY_SOURCE_FAILURE
    )
    assert (
        classify_source_error("claim 关键词支持度不足")
        == SourceFailureKind.UNSUPPORTED_CLAIM
    )


def test_current_direction_requires_sources_from_last_twelve_months() -> None:
    freshness = derive_freshness("下半年全球主线", today=date(2026, 8, 6))

    assert freshness.required is True
    assert freshness.cutoff_date == date(2025, 8, 6)


def test_explicit_historical_year_does_not_force_recent_cutoff() -> None:
    freshness = derive_freshness("复盘 2024 年美联储政策", today=date(2026, 8, 6))

    assert freshness.required is False
    assert freshness.explicit_year == 2024
    assert freshness.cutoff_date is None


def test_default_research_policy_requires_five_facts_and_sixty_percent() -> None:
    policy = ResearchPolicy()

    assert policy.accepts(
        verified=5, total=8, authoritative=1, independent=1
    ) is True
    assert policy.accepts(
        verified=5, total=8, authoritative=0, independent=2
    ) is True
    assert policy.accepts(
        verified=4, total=8, authoritative=1, independent=2
    ) is False
    assert policy.accepts(
        verified=5, total=10, authoritative=1, independent=2
    ) is False
    assert policy.accepts(
        verified=5, total=8, authoritative=0, independent=1
    ) is False
