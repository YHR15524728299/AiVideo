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


def test_default_research_policy_requires_three_facts_or_forty_percent_or_three_total() -> None:
    """新默认策略：verified>=3 或 ratio>=40% 或 total>=3 即可通过（支持内部知识模式）。"""
    policy = ResearchPolicy()

    # 默认值验证
    assert policy.minimum_verified_facts == 3
    assert policy.minimum_verified_ratio == 0.4
    assert policy.minimum_authoritative_sources == 0

    # 通过条件1：verified >= 3（满足最低验证事实数）
    assert policy.accepts(
        verified=3, total=10, authoritative=0, independent=0
    ) is True
    # 即使 ratio 只有 30%，只要 verified>=3 也通过
    assert policy.accepts(
        verified=3, total=10, authoritative=0, independent=0
    ) is True

    # 通过条件2：verified/total >= 40%（比例达标）
    # verified=2, total=5 → 40% 刚好达标
    assert policy.accepts(
        verified=2, total=5, authoritative=0, independent=0
    ) is True
    # 旧版中 verified=4, total=8 (50%<60%) 不通过，现在 50%>=40% 应该通过
    assert policy.accepts(
        verified=4, total=8, authoritative=0, independent=0
    ) is True

    # 通过条件3：total >= 3（内部知识模式：有3条以上事实即接受，无需外部验证）
    assert policy.accepts(
        verified=0, total=3, authoritative=0, independent=0
    ) is True
    assert policy.accepts(
        verified=1, total=8, authoritative=0, independent=0
    ) is True  # total=8>=3，内部知识模式通过

    # 不通过的情况：total<3 且 verified<3 且 ratio<40%
    assert policy.accepts(
        verified=0, total=2, authoritative=0, independent=0
    ) is False
    assert policy.accepts(
        verified=0, total=1, authoritative=0, independent=0
    ) is False
    # total=0 也不通过
    assert policy.accepts(
        verified=0, total=0, authoritative=0, independent=0
    ) is False
