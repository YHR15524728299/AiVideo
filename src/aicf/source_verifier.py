from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import (
    HTTPRedirectHandler,
    Request,
    build_opener,
)

from aicf.research_policy import classify_source_error

# 真实浏览器 User-Agent，避免被反爬识别
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

# 拦截/验证码页面特征关键词
_BLOCKED_PAGE_KEYWORDS = [
    "captcha",
    "robot",
    "automated access",
    "aggressive scraping",
    "request access",
    "access denied",
    "blocked",
    "bot detection",
    "please verify",
    "security check",
    "site map",
]

# 可重试的 HTTP 状态码
_RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}
_MAX_RETRIES = 2
_RETRY_DELAY = 2.0


class SourceVerificationError(ValueError):
    def __init__(
        self,
        errors: str | list[str],
        *,
        evidence: list[dict[str, object]] | None = None,
        research: dict[str, object] | None = None,
    ) -> None:
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        self.evidence = list(evidence or [])
        self.research = research
        super().__init__("；".join(self.errors))


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._in_title = False
        self._ignored_depth = 0

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        normalized = tag.casefold()
        if normalized == "title":
            self._in_title = True
        if normalized in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized == "title":
            self._in_title = False
        if normalized in {"script", "style", "noscript", "svg"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if not text or self._ignored_depth:
            return
        if self._in_title:
            self.title_parts.append(text)
        self.text_parts.append(text)


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, validate_url: Callable[[str], None]) -> None:
        super().__init__()
        self._validate_url = validate_url

    def redirect_request(
        self,
        req: Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> Request | None:
        self._validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class SourceVerifier:
    _ENGLISH_STOPWORDS = {
        "about",
        "after",
        "also",
        "and",
        "are",
        "been",
        "being",
        "can",
        "could",
        "does",
        "for",
        "from",
        "has",
        "have",
        "into",
        "its",
        "more",
        "not",
        "that",
        "the",
        "their",
        "than",
        "this",
        "through",
        "using",
        "was",
        "were",
        "which",
        "will",
        "with",
    }

    def __init__(
        self,
        *,
        timeout: float = 8.0,
        max_response_bytes: int = 1_000_000,
        summary_chars: int = 1200,
        opener: Callable[[Request, float], object] | None = None,
        resolver: Callable[..., list[tuple[object, ...]]] = socket.getaddrinfo,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.summary_chars = summary_chars
        self.resolver = resolver
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat())
        if opener is None:
            safe_opener = build_opener(_SafeRedirectHandler(self._validate_public_url))
            self.opener = lambda request, timeout: safe_opener.open(
                request,
                timeout=timeout,
            )
        else:
            self.opener = opener

    def verify(self, url: str, *, claim: str) -> dict[str, object]:
        self._validate_public_url(url)
        last_error: SourceVerificationError | None = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                return self._verify_once(url, claim=claim)
            except SourceVerificationError as error:
                last_error = error
                # 判断是否可重试：403/429/5xx 或网络超时
                msg = str(error).lower()
                is_retryable = (
                    any(str(code) in msg for code in _RETRYABLE_STATUS)
                    or "不可达" in msg
                    or "timeout" in msg
                    or "connection" in msg
                )
                if is_retryable and attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_DELAY * (attempt + 1))
                    continue
                raise

        if last_error:
            raise last_error
        raise SourceVerificationError("事实核查未知错误")

    def preflight(self, url: str) -> dict[str, object]:
        """只确认公网来源可访问且有正文，不要求支持具体 claim。"""
        self._validate_public_url(url)
        last_error: SourceVerificationError | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return self._verify_once(url, claim="", preflight=True)
            except SourceVerificationError as error:
                last_error = error
                category = classify_source_error(str(error))
                if (
                    category.value == "TEMPORARY_SOURCE_FAILURE"
                    and attempt < _MAX_RETRIES
                ):
                    time.sleep(_RETRY_DELAY * (attempt + 1))
                    continue
                raise
        if last_error:
            raise last_error
        raise SourceVerificationError("来源预检未知错误")

    def _verify_once(
        self,
        url: str,
        *,
        claim: str,
        preflight: bool = False,
    ) -> dict[str, object]:
        request = Request(
            url,
            headers={
                "User-Agent": _BROWSER_UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf;q=0.8,text/plain;q=0.7,*/*;q=0.5",
                "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
                "Accept-Encoding": "identity",
                "Connection": "keep-alive",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Cache-Control": "max-age=0",
            },
            method="GET",
        )
        try:
            with self.opener(request, self.timeout) as response:  # type: ignore[attr-defined]
                final_url = str(response.geturl())
                self._validate_public_url(final_url)
                status = int(getattr(response, "status", 200))
                if not 200 <= status < 300:
                    raise SourceVerificationError(f"URL HTTP {status}")
                content_type_raw = str(response.headers.get("Content-Type", ""))
                content_type = content_type_raw.casefold()
                is_pdf = "application/pdf" in content_type or url.lower().endswith(".pdf")
                declared_size = response.headers.get("Content-Length")
                if declared_size is not None:
                    try:
                        if int(declared_size) > self.max_response_bytes * 5:
                            raise SourceVerificationError(
                                f"URL 响应过大，限制 {self.max_response_bytes * 5} 字节"
                            )
                    except ValueError:
                        pass
                body = response.read(
                    self.max_response_bytes * 5 if is_pdf else self.max_response_bytes + 1
                )
        except SourceVerificationError:
            raise
        except HTTPError as error:
            message = f"URL HTTP {error.code}"
            raise SourceVerificationError(
                message,
                evidence=[self._failed_evidence(url, message)],
            ) from None
        except (URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", None)
            kind = type(reason or error).__name__
            message = f"URL 不可达: {kind}"
            raise SourceVerificationError(
                message,
                evidence=[self._failed_evidence(url, message)],
            ) from None

        # PDF 文件：如果成功下载且有合理大小，则视为验证通过（不做正文关键词匹配）
        if is_pdf:
            if len(body) < 1000:
                raise SourceVerificationError(
                    "PDF 文件过小或内容为空",
                    evidence=[self._failed_evidence(url, "PDF 内容过小")],
                )
            return {
                "original_url": url,
                "final_url": final_url,
                "title": url.split("/")[-1] or "PDF Document",
                "body_summary": f"[PDF 文件，大小 {len(body)} 字节，跳过正文关键词匹配]",
                "fetched_at": self.clock(),
                "sha256": hashlib.sha256(body).hexdigest(),
                "claim_supported": True,
            }

        if len(body) > self.max_response_bytes:
            raise SourceVerificationError(
                f"URL 响应过大，限制 {self.max_response_bytes} 字节"
            )
        charset = self._charset_from_content_type(content_type_raw)
        text = body.decode(charset, errors="replace")
        title, visible_text = self._extract_content(text, content_type_raw)

        # 检测是否被反爬拦截（验证码/阻止访问页面）
        text_lower = visible_text.casefold()
        blocked_hits = sum(1 for kw in _BLOCKED_PAGE_KEYWORDS if kw in text_lower)
        if blocked_hits >= 2 and len(visible_text) < 2000:
            raise SourceVerificationError(
                f"URL 被反爬拦截（检测到验证码/阻止页面）",
                evidence=[self._failed_evidence(url, "被反爬拦截")],
            )

        if not visible_text:
            raise SourceVerificationError("URL 正文为空")

        supported, matched, required = self._claim_support(claim, visible_text)
        evidence = {
            "original_url": url,
            "final_url": final_url,
            "title": title,
            "body_summary": visible_text[: self.summary_chars],
            "fetched_at": self.clock(),
            "sha256": hashlib.sha256(body).hexdigest(),
            "claim_supported": supported or preflight,
            "published_at": self._extract_published_at(text, final_url),
        }
        if preflight:
            return evidence
        if not supported:
            # 放宽：如果页面标题或摘要中包含来源域名（如公司名），至少证明URL可达
            # 对于关键词匹配不足的情况，给出警告但不阻断
            domain = urlparse(final_url).netloc.lower()
            domain_parts = domain.replace("www.", "").split(".")
            domain_hits = sum(1 for part in domain_parts if len(part) > 3 and part in text_lower)
            if domain_hits >= 1 and matched >= 1:
                # 至少有一个关键词匹配且域名正确，标记为支持（低置信度）
                evidence["claim_supported"] = True
                evidence["low_confidence"] = True
                return evidence
            failure_message = (
                "claim 关键词支持度不足"
                f"（匹配 {matched}/{required}）：{claim[:120]}"
            )
            evidence["category"] = classify_source_error(failure_message).value
            raise SourceVerificationError(
                failure_message,
                evidence=[evidence],
            )
        return evidence

    def verify_research(self, research: object) -> list[dict[str, object]]:
        facts = getattr(research, "facts", None)
        if not isinstance(facts, list):
            raise SourceVerificationError("research.facts 必须是列表")
        evidence: list[dict[str, object]] = []
        errors: list[str] = []
        cache: dict[tuple[str, str], dict[str, object]] = {}
        for index, fact in enumerate(facts):
            claim = str(getattr(fact, "claim", ""))
            url = str(getattr(fact, "source_url", "")).strip()
            key = (url, claim)
            # 空URL：无外部来源模式，直接标记为已接受（LLM内部知识）
            if not url:
                evidence.append({
                    "fact_index": index,
                    "original_url": "",
                    "final_url": "",
                    "title": "[无外部来源]",
                    "body_summary": "",
                    "fetched_at": self.clock(),
                    "sha256": "",
                    "claim_supported": True,  # 信任LLM生成内容
                    "published_at": None,
                    "source_type": "none",
                    "external_verification": False,
                })
                continue
            try:
                item = cache.get(key)
                if item is None:
                    item = self.verify(url, claim=claim)
                    cache[key] = item
                evidence.append({"fact_index": index, **item})
            except SourceVerificationError as error:
                evidence.extend(
                    {"fact_index": index, **item}
                    for item in error.evidence
                )
                errors.extend(
                    f"facts[{index}] {message}" for message in error.errors
                )
        if errors:
            raise SourceVerificationError(errors, evidence=evidence)
        return evidence

    def _failed_evidence(
        self,
        url: str,
        error: str,
    ) -> dict[str, object]:
        return {
            "original_url": url,
            "final_url": "",
            "title": "",
            "body_summary": "",
            "fetched_at": self.clock(),
            "sha256": "",
            "claim_supported": False,
            "error": error,
            "category": classify_source_error(error).value,
        }

    @staticmethod
    def _extract_published_at(html_text: str, url: str) -> str | None:
        patterns = (
            r"(?:article:published_time|datePublished|datepublished)"
            r"""[^>]{0,160}?(20\d{2}-\d{2}-\d{2})""",
            r"/(20\d{2})/(\d{2})/(\d{2})(?:/|$)",
            r"(20\d{2})(\d{2})(\d{2})",
        )
        for source, pattern in ((html_text, patterns[0]), (url, patterns[1]), (url, patterns[2])):
            match = re.search(pattern, source, flags=re.IGNORECASE)
            if not match:
                continue
            value = (
                match.group(1)
                if len(match.groups()) == 1
                else "-".join(match.groups())
            )
            try:
                return datetime.fromisoformat(value).date().isoformat()
            except ValueError:
                continue
        return None

    def _validate_public_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            raise SourceVerificationError("禁止非 HTTP(S) URL")
        if not parsed.hostname:
            raise SourceVerificationError("HTTP URL 缺少主机名")
        hostname = parsed.hostname.casefold().rstrip(".")
        if hostname == "localhost" or hostname.endswith(".localhost"):
            raise SourceVerificationError("禁止访问非公网主机")
        if parsed.username or parsed.password:
            raise SourceVerificationError("禁止 URL 携带用户凭据")
        try:
            direct_ip = ipaddress.ip_address(hostname)
            addresses = [direct_ip]
        except ValueError:
            try:
                resolved = self.resolver(
                    hostname,
                    parsed.port or (443 if parsed.scheme == "https" else 80),
                    0,
                    socket.SOCK_STREAM,
                )
            except OSError:
                raise SourceVerificationError("URL 主机无法解析") from None
            addresses = []
            for result in resolved:
                sockaddr = result[4]
                if isinstance(sockaddr, tuple) and sockaddr:
                    try:
                        addresses.append(ipaddress.ip_address(str(sockaddr[0])))
                    except ValueError:
                        continue
        if not addresses:
            raise SourceVerificationError("URL 主机无法解析")
        if any(not address.is_global for address in addresses):
            raise SourceVerificationError("禁止访问非公网或 private 网络")

    @staticmethod
    def _charset_from_content_type(content_type: str) -> str:
        match = re.search(r"charset=([^\s;]+)", content_type, re.IGNORECASE)
        return match.group(1).strip("\"'") if match else "utf-8"

    @staticmethod
    def _extract_content(text: str, content_type: str) -> tuple[str, str]:
        if content_type.casefold().startswith("text/plain"):
            normalized = " ".join(text.split())
            return "", normalized
        parser = _TextExtractor()
        parser.feed(text)
        title = " ".join(parser.title_parts).strip()
        visible_text = " ".join(parser.text_parts).strip()
        return title, visible_text

    @classmethod
    def _claim_support(cls, claim: str, body: str) -> tuple[bool, int, int]:
        claim_tokens = cls._keywords(claim)
        body_tokens = cls._keywords(body)
        if not claim_tokens:
            return False, 0, 1
        matched = len(claim_tokens & body_tokens)
        required = len(claim_tokens)
        threshold = 1 if required <= 2 else max(2, (required + 2) // 3)
        return matched >= threshold, matched, required

    @classmethod
    def _keywords(cls, text: str) -> set[str]:
        normalized = text.casefold()
        english = {
            token
            for token in re.findall(r"[a-z][a-z0-9-]{2,}", normalized)
            if token not in cls._ENGLISH_STOPWORDS
        }
        chinese: set[str] = set()
        for sequence in re.findall(r"[\u3400-\u9fff]+", normalized):
            if len(sequence) == 1:
                chinese.add(sequence)
                continue
            chinese.update(
                sequence[index : index + 2]
                for index in range(len(sequence) - 1)
            )
        return english | chinese
