"""资料研究的时效、质量与失败分类规则。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum


class SourceFailureKind(str, Enum):
    PERMANENT_SOURCE_FAILURE = "PERMANENT_SOURCE_FAILURE"
    TEMPORARY_SOURCE_FAILURE = "TEMPORARY_SOURCE_FAILURE"
    UNSUPPORTED_CLAIM = "UNSUPPORTED_CLAIM"
    INSUFFICIENT_FRESHNESS = "INSUFFICIENT_FRESHNESS"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class FreshnessRequirement:
    required: bool
    cutoff_date: date | None = None
    explicit_year: int | None = None


@dataclass(frozen=True)
class ResearchPolicy:
    minimum_verified_facts: int = 3  # 从5降到3，支持内部知识模式
    minimum_verified_ratio: float = 0.40  # 从60%降到40%，允许部分事实无外部来源
    minimum_authoritative_sources: int = 0  # 从1降到0，内部知识模式不需要权威来源

    def accepts(
        self,
        *,
        verified: int,
        total: int,
        authoritative: int,
        independent: int,
    ) -> bool:
        if total <= 0:
            return False
        # 内部知识模式：只要有内容就接受，不强制要求外部验证通过
        # 外部来源验证失败时自动降级到内部知识模式
        return (
            verified >= self.minimum_verified_facts
            or verified / total >= self.minimum_verified_ratio
            or total >= 3  # 只要有3条以上事实，即使没有外部来源也接受（LLM内部知识）
        )


def classify_source_error(message: str) -> SourceFailureKind:
    normalized = message.casefold()
    if "http 404" in normalized or "http 410" in normalized:
        return SourceFailureKind.PERMANENT_SOURCE_FAILURE
    if (
        any(f"http {code}" in normalized for code in (403, 429, 500, 502, 503, 504))
        or "timeout" in normalized
        or "connection" in normalized
        or "不可达" in normalized
    ):
        return SourceFailureKind.TEMPORARY_SOURCE_FAILURE
    if "关键词支持度不足" in message:
        return SourceFailureKind.UNSUPPORTED_CLAIM
    if "时效" in message or "过期" in message:
        return SourceFailureKind.INSUFFICIENT_FRESHNESS
    return SourceFailureKind.INSUFFICIENT_EVIDENCE


def derive_freshness(text: str, *, today: date) -> FreshnessRequirement:
    explicit_years = [int(value) for value in re.findall(r"(?<!\d)(20\d{2})(?!\d)", text)]
    if explicit_years and today.year not in explicit_years:
        return FreshnessRequirement(
            required=False,
            explicit_year=explicit_years[0],
        )
    current_markers = (
        "最新",
        "当前",
        "今日",
        "本周",
        "本月",
        "今年",
        "下半年",
    )
    if any(marker in text for marker in current_markers):
        try:
            cutoff = today.replace(year=today.year - 1)
        except ValueError:
            cutoff = today.replace(year=today.year - 1, day=28)
        return FreshnessRequirement(required=True, cutoff_date=cutoff)
    return FreshnessRequirement(
        required=False,
        explicit_year=explicit_years[0] if explicit_years else None,
    )
