from __future__ import annotations

import hashlib
from io import BytesIO
from urllib.error import HTTPError

import pytest

from aicf.source_verifier import SourceVerificationError, SourceVerifier


class FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        final_url: str = "https://docs.example.com/final",
        content_type: str = "text/html; charset=utf-8",
    ) -> None:
        self._stream = BytesIO(body)
        self._final_url = final_url
        self.status = 200
        self.headers = {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
        }

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def geturl(self) -> str:
        return self._final_url


def _public_resolver(*args: object) -> list[tuple[object, ...]]:
    return [(None, None, None, None, ("93.184.216.34", 443))]


def test_source_verifier_captures_final_url_title_summary_time_and_sha256() -> None:
    body = (
        b"<html><head><title>Official Workflow Guide</title></head>"
        b"<body><h1>Workflow validation</h1>"
        b"<p>Stage validation reduces rework and keeps production consistent.</p>"
        b"</body></html>"
    )
    verifier = SourceVerifier(
        opener=lambda request, timeout: FakeResponse(body),
        resolver=_public_resolver,
        clock=lambda: "2026-07-20T12:00:00+00:00",
    )

    evidence = verifier.verify(
        "https://docs.example.com/start",
        claim="Stage validation reduces rework",
    )

    assert evidence["original_url"] == "https://docs.example.com/start"
    assert evidence["final_url"] == "https://docs.example.com/final"
    assert evidence["title"] == "Official Workflow Guide"
    assert "Stage validation reduces rework" in evidence["body_summary"]
    assert evidence["fetched_at"] == "2026-07-20T12:00:00+00:00"
    assert evidence["sha256"] == hashlib.sha256(body).hexdigest()
    assert evidence["claim_supported"] is True


def test_source_verifier_preflight_checks_reachability_without_claim_match() -> None:
    body = (
        b'<html><head><title>Completely different title</title>'
        b'<meta property="article:published_time" '
        b'content="2026-07-29T12:00:00Z"></head>'
        b"<body>Reachable public article.</body></html>"
    )
    verifier = SourceVerifier(
        opener=lambda _request, _timeout: FakeResponse(body),
        resolver=_public_resolver,
        clock=lambda: "2026-07-20T12:00:00+00:00",
    )

    evidence = verifier.preflight("https://docs.example.com/start")

    assert evidence["final_url"] == "https://docs.example.com/final"
    assert evidence["claim_supported"] is True
    assert evidence["published_at"] == "2026-07-29"


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.8/private",
        "http://localhost/private",
    ],
)
def test_source_verifier_rejects_file_and_private_network_urls(url: str) -> None:
    verifier = SourceVerifier(
        opener=lambda request, timeout: pytest.fail("禁止的 URL 不应发起 HTTP 请求"),
    )

    with pytest.raises(SourceVerificationError, match="禁止|公网|HTTP"):
        verifier.verify(url, claim="anything")


def test_source_verifier_limits_response_size_before_reading_body() -> None:
    body = b"<html><body>" + (b"x" * 1024) + b"</body></html>"
    verifier = SourceVerifier(
        max_response_bytes=128,
        opener=lambda request, timeout: FakeResponse(body),
        resolver=_public_resolver,
    )

    with pytest.raises(SourceVerificationError, match="响应过大"):
        verifier.verify(
            "https://docs.example.com/large",
            claim="large response",
        )


def test_source_verifier_reports_http_reachability_failure_without_leaking_body() -> None:
    def fail(request: object, timeout: float) -> object:
        raise HTTPError(
            "https://docs.example.com/missing",
            404,
            "Not Found",
            {},
            BytesIO(b"secret response body"),
        )

    verifier = SourceVerifier(opener=fail, resolver=_public_resolver)

    with pytest.raises(SourceVerificationError) as captured:
        verifier.verify(
            "https://docs.example.com/missing",
            claim="missing page",
        )

    assert "HTTP 404" in str(captured.value)
    assert "secret response body" not in str(captured.value)
    assert captured.value.evidence == [{
        "original_url": "https://docs.example.com/missing",
        "final_url": "",
        "title": "",
        "body_summary": "",
        "fetched_at": captured.value.evidence[0]["fetched_at"],
        "sha256": "",
        "claim_supported": False,
        "error": "URL HTTP 404",
        "category": "PERMANENT_SOURCE_FAILURE",
    }]


def test_source_verifier_checks_chinese_and_english_keyword_support() -> None:
    verifier = SourceVerifier(
        opener=lambda request, timeout: FakeResponse(
            "<html><title>官方工作流</title><body>分阶段校验可以降低返工成本。</body></html>".encode(
                "utf-8"
            )
        ),
        resolver=_public_resolver,
    )

    supported = verifier.verify(
        "https://docs.example.com/zh",
        claim="分阶段校验降低返工",
    )
    assert supported["claim_supported"] is True

    with pytest.raises(SourceVerificationError, match="关键词支持度不足") as captured:
        verifier.verify(
            "https://docs.example.com/zh",
            claim="量子计算提高电池容量",
        )

    assert captured.value.evidence[0]["final_url"] == "https://docs.example.com/final"
    assert captured.value.evidence[0]["claim_supported"] is False
    assert captured.value.evidence[0]["category"] == "UNSUPPORTED_CLAIM"
    assert captured.value.evidence[0]["sha256"] == hashlib.sha256(
        "<html><title>官方工作流</title><body>分阶段校验可以降低返工成本。</body></html>".encode(
            "utf-8"
        )
    ).hexdigest()
