from __future__ import annotations

import hashlib
import ipaddress
import re
import socket
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
        request = Request(
            url,
            headers={
                "User-Agent": "AIContentFactory-SourceVerifier/1.0",
                "Accept": "text/html,text/plain;q=0.9",
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
                content_type = str(response.headers.get("Content-Type", ""))
                if not (
                    content_type.casefold().startswith("text/html")
                    or content_type.casefold().startswith("text/plain")
                ):
                    raise SourceVerificationError(
                        f"URL 响应类型不支持: {content_type or '未知'}"
                    )
                declared_size = response.headers.get("Content-Length")
                if declared_size is not None:
                    try:
                        if int(declared_size) > self.max_response_bytes:
                            raise SourceVerificationError(
                                f"URL 响应过大，限制 {self.max_response_bytes} 字节"
                            )
                    except ValueError:
                        pass
                body = response.read(self.max_response_bytes + 1)
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

        if len(body) > self.max_response_bytes:
            raise SourceVerificationError(
                f"URL 响应过大，限制 {self.max_response_bytes} 字节"
            )
        charset = self._charset_from_content_type(content_type)
        text = body.decode(charset, errors="replace")
        title, visible_text = self._extract_content(text, content_type)
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
            "claim_supported": supported,
        }
        if not supported:
            raise SourceVerificationError(
                "claim 关键词支持度不足"
                f"（匹配 {matched}/{required}）：{claim[:120]}",
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
            url = str(getattr(fact, "source_url", ""))
            key = (url, claim)
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
        }

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
