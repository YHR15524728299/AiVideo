from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest
from pydantic import BaseModel, ValidationError

import aicf.providers.openrouter as openrouter_provider
from aicf.cache import FileCache
from aicf.providers.openrouter import (
    ModelCatalogVerificationError,
    OpenRouterClient,
    OpenRouterHTTPError,
)


class StubTransport:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def __call__(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, object],
        timeout: float,
    ) -> dict[str, object]:
        self.requests.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        assert isinstance(response, dict)
        return response


class StubCatalogTransport:
    def __init__(self, response: object) -> None:
        self.response = response
        self.requests: list[dict[str, object]] = []

    def __call__(
        self,
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> dict[str, object]:
        self.requests.append({"url": url, "headers": headers, "timeout": timeout})
        if isinstance(self.response, Exception):
            raise self.response
        assert isinstance(self.response, dict)
        return self.response


class SequencedCatalogTransport:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.requests = 0

    def __call__(
        self,
        _url: str,
        _headers: dict[str, str],
        _timeout: float,
    ) -> dict[str, object]:
        self.requests += 1
        return self.responses.pop(0)


def _catalog(*models: dict[str, object]) -> dict[str, object]:
    return {"data": list(models)}


def _free_model(model_id: str = "test/model:free") -> dict[str, object]:
    return {
        "id": model_id,
        "pricing": {
            "prompt": "0",
            "completion": "0",
            "request": "0",
        },
    }


@pytest.fixture(autouse=True)
def stub_default_model_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        openrouter_provider,
        "_http_model_catalog_transport",
        lambda _url, _headers, _timeout: _catalog(_free_model()),
    )


def _response(content: str = '{"answer": "ok"}') -> dict[str, object]:
    return {
        "id": "gen-1",
        "model": "test/model:free",
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def test_openrouter_proves_selected_model_is_free_from_live_catalog_before_chat(
    tmp_path: Path,
) -> None:
    chat_transport = StubTransport([_response()])
    catalog_transport = StubCatalogTransport(_catalog(_free_model()))
    client = OpenRouterClient(
        api_key="secret",
        model="test/model:free",
        cache=FileCache(tmp_path / "cache"),
        transport=chat_transport,
        model_catalog_transport=catalog_transport,
        sleep=lambda _: None,
    )

    result = client.call_structured(
        stage="direction",
        system_prompt="只返回 JSON",
        user_payload={"direction": "AI 视频"},
        json_schema={"name": "direction", "schema": {"type": "object"}},
    )

    assert result.data == {"answer": "ok"}
    assert catalog_transport.requests == [{
        "url": "https://openrouter.ai/api/v1/models",
        "headers": {
            "Authorization": "Bearer secret",
            "Accept": "application/json",
        },
        "timeout": 180.0,
    }]
    assert len(chat_transport.requests) == 1


@pytest.mark.parametrize(
    ("catalog_response", "message"),
    [
        (URLError("offline"), "无法实时验证"),
        ({"unexpected": []}, "格式无效"),
        (_catalog(_free_model("other/model:free")), "不在实时模型目录"),
        (
            _catalog({
                "id": "test/model:free",
                "pricing": {"prompt": "0.000001", "completion": "0"},
            }),
            "不是免费模型",
        ),
        (
            _catalog({"id": "test/model:free", "pricing": {"prompt": "0"}}),
            "无法证明",
        ),
    ],
)
def test_openrouter_fails_closed_before_chat_when_catalog_cannot_prove_free(
    tmp_path: Path,
    catalog_response: object,
    message: str,
) -> None:
    chat_transport = StubTransport([_response()])
    client = OpenRouterClient(
        api_key="secret",
        model="test/model:free",
        cache=FileCache(tmp_path / "cache"),
        transport=chat_transport,
        model_catalog_transport=StubCatalogTransport(catalog_response),
        sleep=lambda _: None,
    )

    with pytest.raises(ModelCatalogVerificationError, match=message):
        client.call_structured(
            stage="direction",
            system_prompt="只返回 JSON",
            user_payload={},
            json_schema={"name": "direction", "schema": {"type": "object"}},
        )

    assert chat_transport.requests == []


def test_openrouter_rechecks_live_catalog_even_before_serving_cached_result(
    tmp_path: Path,
) -> None:
    catalog_transport = SequencedCatalogTransport([
        _catalog(_free_model()),
        _catalog({
            "id": "test/model:free",
            "pricing": {"prompt": "0", "completion": "0.1"},
        }),
    ])
    chat_transport = StubTransport([_response()])
    client = OpenRouterClient(
        api_key="secret",
        model="test/model:free",
        cache=FileCache(tmp_path / "cache"),
        transport=chat_transport,
        model_catalog_transport=catalog_transport,
        sleep=lambda _: None,
    )
    arguments = {
        "stage": "direction",
        "system_prompt": "JSON",
        "user_payload": {},
        "json_schema": {"name": "direction", "schema": {"type": "object"}},
    }

    assert client.call_structured(**arguments).cached is False
    with pytest.raises(ModelCatalogVerificationError, match="不是免费模型"):
        client.call_structured(**arguments)

    assert catalog_transport.requests == 2
    assert len(chat_transport.requests) == 1


def test_openrouter_clients_do_not_share_live_catalog_verification(
    tmp_path: Path,
) -> None:
    model = "test/isolation:free"
    first_catalog = StubCatalogTransport(_catalog(_free_model(model)))
    second_catalog = StubCatalogTransport(_catalog({
        "id": model,
        "pricing": {"prompt": "0", "completion": "0.1"},
    }))
    first_chat = StubTransport([_response()])
    second_chat = StubTransport([_response()])
    arguments = {
        "stage": "direction",
        "system_prompt": "JSON",
        "user_payload": {},
        "json_schema": {"name": "direction", "schema": {"type": "object"}},
    }

    first_client = OpenRouterClient(
        api_key="first-secret",
        model=model,
        cache=FileCache(tmp_path / "first-cache"),
        transport=first_chat,
        model_catalog_transport=first_catalog,
        sleep=lambda _: None,
    )
    second_client = OpenRouterClient(
        api_key="second-secret",
        model=model,
        cache=FileCache(tmp_path / "second-cache"),
        transport=second_chat,
        model_catalog_transport=second_catalog,
        sleep=lambda _: None,
    )

    assert first_client.call_structured(**arguments).data == {"answer": "ok"}
    with pytest.raises(ModelCatalogVerificationError, match="不是免费模型"):
        second_client.call_structured(**arguments)

    assert len(first_catalog.requests) == 1
    assert len(second_catalog.requests) == 1
    assert len(first_chat.requests) == 1
    assert second_chat.requests == []


def test_openrouter_structured_call_sends_json_schema_and_tracks_usage(tmp_path: Path) -> None:
    transport = StubTransport([_response()])
    client = OpenRouterClient(
        api_key="secret",
        model="test/model:free",
        cache=FileCache(tmp_path / "cache"),
        transport=transport,
        sleep=lambda _: None,
    )

    result = client.call_structured(
        stage="direction",
        system_prompt="只返回 JSON",
        user_payload={"direction": "AI 视频"},
        json_schema={
            "name": "direction_result",
            "schema": {
                "type": "object",
                "properties": {"answer": {"type": "string"}},
                "required": ["answer"],
            },
        },
        prompt_version="v1",
    )

    assert result.data == {"answer": "ok"}
    assert result.cached is False
    assert result.usage.total_tokens == 18
    request = transport.requests[0]
    assert request["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer secret"
    assert request["body"]["response_format"]["type"] == "json_schema"
    assert client.usage.total_tokens == 18


def test_openrouter_cache_avoids_http_and_cached_usage_is_zero(tmp_path: Path) -> None:
    transport = StubTransport([_response()])
    client = OpenRouterClient(
        api_key="secret",
        model="test/model:free",
        cache=FileCache(tmp_path / "cache"),
        transport=transport,
        sleep=lambda _: None,
    )

    arguments = {
        "stage": "research",
        "system_prompt": "JSON",
        "user_payload": {"topic": "缓存"},
        "json_schema": {"name": "research", "schema": {"type": "object"}},
        "prompt_version": "v1",
    }

    first = client.call_structured(**arguments)
    second = client.call_structured(**arguments)

    assert first.data == second.data
    assert second.cached is True
    assert second.usage.total_tokens == 0
    assert len(transport.requests) == 1
    assert client.usage.total_tokens == 18


def test_openrouter_retries_retryable_http_errors(tmp_path: Path) -> None:
    error = HTTPError("https://openrouter.ai", 429, "rate limited", {}, None)
    transport = StubTransport([error, error, _response()])
    sleeps: list[float] = []
    client = OpenRouterClient(
        api_key="secret",
        model="test/model:free",
        cache=FileCache(tmp_path / "cache"),
        max_retries=2,
        transport=transport,
        sleep=sleeps.append,
    )

    result = client.call_structured(
        stage="script",
        system_prompt="JSON",
        user_payload={"topic": "retry"},
        json_schema={"name": "script", "schema": {"type": "object"}},
    )

    assert result.data == {"answer": "ok"}
    assert len(transport.requests) == 3
    assert sleeps == [1.0, 2.0]


def test_openrouter_rejects_invalid_structured_content_without_caching(tmp_path: Path) -> None:
    transport = StubTransport([_response("not json")])
    cache = FileCache(tmp_path / "cache")
    client = OpenRouterClient(
        api_key="secret",
        model="test/model:free",
        cache=cache,
        transport=transport,
        sleep=lambda _: None,
        max_retries=0,
    )

    with pytest.raises(ValueError, match="JSON"):
        client.call_structured(
            stage="review",
            system_prompt="JSON",
            user_payload={},
            json_schema={"name": "review", "schema": {"type": "object"}},
        )

    assert list((tmp_path / "cache").glob("*.json")) == []


def _http_error(code: int, payload: str) -> HTTPError:
    return HTTPError(
        "https://openrouter.ai/api/v1/chat/completions?api_key=secret-query",
        code,
        "bad request",
        {"Authorization": "Bearer secret-header"},
        BytesIO(payload.encode("utf-8")),
    )


def test_openrouter_never_allows_paid_models_even_with_legacy_override(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="仅允许使用免费模型"):
        OpenRouterClient(
            api_key="secret",
            model="paid/model",
            cache=FileCache(tmp_path / "cache"),
            allow_paid_models=True,
        )


def test_openrouter_falls_back_from_json_schema_to_json_object(tmp_path: Path) -> None:
    unsupported = _http_error(
        400,
        '{"error":{"message":"response_format json_schema is not supported by this model"}}',
    )
    transport = StubTransport([unsupported, _response()])
    client = OpenRouterClient(
        api_key="secret",
        model="test/model:free",
        cache=FileCache(tmp_path / "cache"),
        transport=transport,
        sleep=lambda _: None,
        max_retries=0,
    )

    result = client.call_structured(
        stage="direction",
        system_prompt="只返回 JSON",
        user_payload={"direction": "AI 视频"},
        json_schema={"name": "direction", "schema": {"type": "object"}},
    )

    assert result.data == {"answer": "ok"}
    assert [request["body"]["response_format"]["type"] for request in transport.requests] == [
        "json_schema",
        "json_object",
    ]
    assert client.usage.total_tokens == 18


def test_openrouter_falls_back_to_strict_json_prompt_when_response_format_is_unsupported(
    tmp_path: Path,
) -> None:
    schema_error = _http_error(400, "json_schema unsupported")
    object_error = _http_error(400, "response_format json_object unsupported")
    transport = StubTransport([schema_error, object_error, _response()])
    client = OpenRouterClient(
        api_key="secret",
        model="test/model:free",
        cache=FileCache(tmp_path / "cache"),
        transport=transport,
        sleep=lambda _: None,
        max_retries=0,
    )

    client.call_structured(
        stage="research",
        system_prompt="研究",
        user_payload={},
        json_schema={"name": "research", "schema": {"type": "object"}},
    )

    final_body = transport.requests[-1]["body"]
    assert "response_format" not in final_body
    assert "严格 JSON" in final_body["messages"][0]["content"]


def test_structured_engine_always_performs_local_pydantic_validation(tmp_path: Path) -> None:
    class RequiredResult(BaseModel):
        answer: str

    from aicf.engines.llm_engine import StructuredEngine

    class RequiredEngine(StructuredEngine):
        stage = "required"
        system_prompt = "JSON"
        result_model = RequiredResult

    transport = StubTransport([_response('{"wrong": "field"}')] * 3)
    client = OpenRouterClient(
        api_key="secret",
        model="test/model:free",
        cache=FileCache(tmp_path / "cache"),
        transport=transport,
        sleep=lambda _: None,
        max_retries=0,
    )

    with pytest.raises(ValidationError):
        RequiredEngine(client).generate({})


def test_structured_engine_repairs_validation_error_with_original_response_and_summary() -> None:
    class RequiredResult(BaseModel):
        answer: str

    from aicf.engines.llm_engine import StructuredEngine

    class RequiredEngine(StructuredEngine):
        stage = "required"
        system_prompt = "JSON"
        result_model = RequiredResult

    class RecordingClient:
        def __init__(self) -> None:
            self.responses = [{"wrong": "field"}, {"answer": "fixed"}]
            self.calls: list[dict[str, object]] = []

        def call_structured(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return type("Result", (), {"data": self.responses.pop(0)})()

    client = RecordingClient()

    result = RequiredEngine(client).generate({"input": "value"})

    assert result.answer == "fixed"
    assert [call["stage"] for call in client.calls] == ["required", "required"]
    assert [call["prompt_version"] for call in client.calls] == [
        "m2-v1",
        "m2-v1-repair-1",
    ]
    repair_payload = client.calls[1]["user_payload"]
    assert repair_payload["original_response"] == {"wrong": "field"}
    assert repair_payload["repair_round"] == 1
    assert "answer" in repair_payload["validation_error_summary"]


def test_structured_engine_stops_after_two_validation_repair_rounds() -> None:
    class RequiredResult(BaseModel):
        answer: str

    from aicf.engines.llm_engine import StructuredEngine

    class RequiredEngine(StructuredEngine):
        stage = "required"
        system_prompt = "JSON"
        result_model = RequiredResult

    class RecordingClient:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def call_structured(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return type("Result", (), {"data": {"wrong": "field"}})()

    client = RecordingClient()

    with pytest.raises(ValidationError):
        RequiredEngine(client).generate({})

    assert len(client.calls) == 3
    assert [call["prompt_version"] for call in client.calls] == [
        "m2-v1",
        "m2-v1-repair-1",
        "m2-v1-repair-2",
    ]


def test_structured_engine_compatibly_copies_review_instructions_without_repair() -> None:
    from aicf.engines.llm_engine import StructuredEngine
    from aicf.models.contracts import ReviewResult

    class ReviewTestEngine(StructuredEngine):
        stage = "review"
        system_prompt = "JSON"
        result_model = ReviewResult

    compatible = {
        "passed": False,
        "scores": {
            "direction_fit": 90,
            "hook": 90,
            "clarity": 90,
            "evidence": 40,
            "safety": 95,
        },
        "issues": [],
        "revision_instructions": ["补充问题"],
    }

    class RecordingClient:
        def __init__(self) -> None:
            self.responses = [compatible]
            self.calls: list[dict[str, object]] = []

        def call_structured(self, **kwargs: object) -> object:
            self.calls.append(kwargs)
            return type("Result", (), {"data": self.responses.pop(0)})()

    client = RecordingClient()

    result = ReviewTestEngine(client).generate({})

    assert result.issues == ["补充问题"]
    assert len(client.calls) == 1


def test_openrouter_http_error_exposes_only_sanitized_summary(tmp_path: Path) -> None:
    error = _http_error(
        401,
        '{"error":{"message":"invalid api key sk-or-v1-super-secret-token for bearer secret-header"}}',
    )
    client = OpenRouterClient(
        api_key="sk-or-v1-super-secret-token",
        model="test/model:free",
        cache=FileCache(tmp_path / "cache"),
        transport=StubTransport([error]),
        sleep=lambda _: None,
        max_retries=0,
    )

    with pytest.raises(OpenRouterHTTPError) as captured:
        client.call_structured(
            stage="review",
            system_prompt="JSON",
            user_payload={},
            json_schema={"name": "review", "schema": {"type": "object"}},
        )

    summary = str(captured.value)
    assert "HTTP 401" in summary
    assert "super-secret-token" not in summary
    assert "secret-header" not in summary
    assert "api_key=secret-query" not in summary
