"""从公开搜索结果发现并预检资料来源。"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, replace
from datetime import date
from email.utils import parsedate_to_datetime
from typing import Callable, Protocol
from urllib.parse import quote_plus, urldefrag, urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree

from aicf.research_policy import (
    FreshnessRequirement,
    SourceFailureKind,
    classify_source_error,
)
from aicf.source_verifier import SourceVerificationError


@dataclass(frozen=True)
class SourceCandidate:
    url: str
    title: str
    published_at: date | None = None
    source_type: str = "web"
    query: str = ""
    core_eligible: bool = True


@dataclass(frozen=True)
class DiscoveryResult:
    candidates: list[SourceCandidate]
    rejections: list[dict[str, str]]


class SearchProvider(Protocol):
    def search(self, query: str, *, limit: int) -> list[SourceCandidate]: ...


def _default_fetcher(url: str, timeout: float) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 AIContentFactory/1.0",
            "Accept": "application/rss+xml,application/xml,text/xml",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read(1_000_000)


class BingRSSSearchProvider:
    def __init__(
        self,
        *,
        fetcher: Callable[[str, float], bytes] = _default_fetcher,
        timeout: float = 8.0,
    ) -> None:
        self.fetcher = fetcher
        self.timeout = timeout

    def search(self, query: str, *, limit: int) -> list[SourceCandidate]:
        url = f"https://www.bing.com/search?format=rss&q={quote_plus(query)}"
        root = ElementTree.fromstring(self.fetcher(url, self.timeout))
        results: list[SourceCandidate] = []
        for item in root.findall(".//item")[:limit]:
            result_url = html.unescape(item.findtext("link", "").strip())
            title = html.unescape(item.findtext("title", "").strip())
            if not result_url or not title:
                continue
            published_at = self._parse_date(item.findtext("pubDate", ""))
            results.append(
                SourceCandidate(
                    url=result_url,
                    title=title,
                    published_at=published_at,
                    source_type=self._source_type(result_url),
                    query=query,
                )
            )
        return results

    @staticmethod
    def _parse_date(value: str) -> date | None:
        if not value.strip():
            return None
        try:
            return parsedate_to_datetime(value).date()
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _source_type(url: str) -> str:
        host = urlparse(url).hostname or ""
        exact_domains = (
            "who.int",
            "imf.org",
            "worldbank.org",
            "bis.org",
            "un.org",
        )
        host = host.casefold().rstrip(".")
        institutional = any(
            host == domain or host.endswith("." + domain)
            for domain in exact_domains
        )
        public_suffix = (
            host.endswith(".gov")
            or ".gov." in host and host.endswith((".gov.cn", ".gov.uk"))
            or host.endswith(".edu")
        )
        return (
            "official"
            if institutional or public_suffix
            else "web"
        )


class SourceDiscovery:
    def __init__(
        self,
        provider: SearchProvider,
        *,
        preflight: Callable[[SourceCandidate], dict[str, object]],
    ) -> None:
        self.provider = provider
        self.preflight = preflight

    def discover(
        self,
        *,
        queries: list[str],
        freshness: FreshnessRequirement,
        rejected_urls: set[str],
        limit: int,
    ) -> DiscoveryResult:
        rejected = {self._canonical(url) for url in rejected_urls}
        seen: set[str] = set()
        accepted: list[SourceCandidate] = []
        rejections: list[dict[str, str]] = []
        per_query = max(limit, 5)
        for query in queries:
            for candidate in self.provider.search(query, limit=per_query):
                canonical = self._canonical(candidate.url)
                if not canonical or canonical in rejected or canonical in seen:
                    continue
                seen.add(canonical)
                try:
                    result = self.preflight(candidate)
                except SourceVerificationError as error:
                    category = classify_source_error(str(error))
                    if category == SourceFailureKind.PERMANENT_SOURCE_FAILURE:
                        rejections.append({
                            "url": canonical,
                            "category": category.value,
                            "reason": str(error),
                        })
                    continue
                final_url = self._canonical(
                    str(result.get("final_url") or canonical)
                )
                if not final_url or final_url in rejected:
                    continue
                published_at = candidate.published_at
                if published_at is None and result.get("published_at"):
                    try:
                        published_at = date.fromisoformat(
                            str(result["published_at"])
                        )
                    except ValueError:
                        published_at = None
                core_eligible = not (
                    freshness.required
                    and freshness.cutoff_date is not None
                    and (
                        published_at is None
                        or published_at < freshness.cutoff_date
                    )
                )
                accepted.append(
                    replace(
                        candidate,
                        url=final_url,
                        published_at=published_at,
                        core_eligible=core_eligible,
                    )
                )
                if len(accepted) >= limit:
                    return DiscoveryResult(accepted, rejections)
        return DiscoveryResult(accepted, rejections)

    @staticmethod
    def _canonical(url: str) -> str:
        clean, _fragment = urldefrag(url.strip())
        return re.sub(r"/$", "", clean)
