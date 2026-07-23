from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from aicf.cache import FileCache

# 加载环境变量
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=False)

# 默认使用 OpenRouter 免费模型（强制 :free 后缀，优先中文支持好的模型）
DEFAULT_FREE_MODEL = "tencent/hy3:free"


class OpenRouterHTTPError(RuntimeError):
    def __init__(self, status_code: int, summary: str) -> None:
        self.status_code = status_code
        super().__init__(f"OpenRouter HTTP {status_code}: {summary}")


class ModelCatalogVerificationError(ValueError):
    """The live OpenRouter catalog could not prove that the selected model is free."""


@dataclass(frozen=True)
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


@dataclass(frozen=True)
class StructuredResult:
    data: dict[str, object]
    usage: TokenUsage
    cached: bool
    request_id: str | None = None
    model: str | None = None


Transport = Callable[
    [str, dict[str, str], dict[str, object], float],
    dict[str, object],
]
ModelCatalogTransport = Callable[
    [str, dict[str, str], float],
    dict[str, object],
]


def _http_transport(
    url: str,
    headers: dict[str, str],
    body: dict[str, object],
    timeout: float,
) -> dict[str, object]:
    request = Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("OpenRouter 响应顶层必须是对象")
    return payload


def _http_model_catalog_transport(
    url: str,
    headers: dict[str, str],
    timeout: float,
) -> dict[str, object]:
    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("OpenRouter 模型目录响应顶层必须是对象")
    return payload


def extract_json_object(content: str) -> dict[str, object]:
    text = content.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline >= 0 else ""
        if text.endswith("```"):
            text = text[:-3]
    start = text.find("{")
    if start < 0:
        raise ValueError("模型响应中没有 JSON 对象")
    decoder = json.JSONDecoder()
    try:
        value, _ = decoder.raw_decode(text[start:])
    except json.JSONDecodeError as error:
        raise ValueError(f"模型响应 JSON 解析失败: {error.msg}") from error
    if not isinstance(value, dict):
        raise ValueError("模型响应 JSON 顶层必须是对象")
    return value


class OpenRouterClient:
    endpoint = "https://openrouter.ai/api/v1/chat/completions"
    models_endpoint = "https://openrouter.ai/api/v1/models"

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        cache: FileCache | None = None,
        *,
        max_retries: int = 2,
        timeout: float = 60.0,
        transport: Transport = _http_transport,
        model_catalog_transport: ModelCatalogTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        app_name: str = "AI Content Factory",
        site_url: str = "",
        allow_paid_models: bool = False,
    ) -> None:
        resolved_key = api_key or os.getenv("OPENROUTER_API_KEY", "")
        if not resolved_key:
            raise ValueError("OpenRouter API Key 不能为空，请设置 OPENROUTER_API_KEY 环境变量或 .env 文件")
        self.api_key = resolved_key

        resolved_model = model or os.getenv("OPENROUTER_MODEL", DEFAULT_FREE_MODEL)
        if not resolved_model.endswith(":free"):
            raise ValueError(
                f"当前配置仅允许使用免费模型（模型名需以 :free 结尾），当前模型: {resolved_model}"
            )
        self.model = resolved_model
        self.cache = cache
        self.max_retries = max_retries
        self.timeout = timeout
        self.transport = transport
        self.model_catalog_transport = (
            model_catalog_transport or _http_model_catalog_transport
        )
        self.sleep = sleep
        self.app_name = app_name
        self.site_url = site_url
        self.usage = TokenUsage()
        self.logical_calls = 0

    def call_structured(
        self,
        *,
        stage: str,
        system_prompt: str,
        user_payload: dict[str, object],
        json_schema: dict[str, object],
        prompt_version: str = "v1",
    ) -> StructuredResult:
        self._verify_free_model_from_live_catalog()
        cache_inputs = {
            "system_prompt": system_prompt,
            "user_payload": user_payload,
            "json_schema": json_schema,
        }
        key = self.cache.make_key(stage, cache_inputs, self.model, prompt_version)
        cached = self.cache.get(key)
        if cached is not None:
            data = cached.get("data") if isinstance(cached, dict) else None
            if not isinstance(data, dict):
                raise ValueError("OpenRouter 缓存格式无效")
            return StructuredResult(data=data, usage=TokenUsage(), cached=True)

        base_body: dict[str, object] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(user_payload, ensure_ascii=False, sort_keys=True),
                },
            ],
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "X-Title": self.app_name,
        }
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url

        response = self._request_with_fallback(headers, base_body, json_schema)
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices:
            error_detail = response.get("error", {})
            if isinstance(error_detail, dict):
                msg = error_detail.get("message", "")
                code = error_detail.get("code", "")
                raise ValueError(
                    f"OpenRouter 响应缺少 choices（模型: {self.model}，"
                    f"错误码: {code}，错误信息: {msg}）"
                )
            raise ValueError(
                f"OpenRouter 响应缺少 choices（模型: {self.model}，"
                f"原始响应: {json.dumps(response, ensure_ascii=False)[:500]}）"
            )
        first = choices[0]
        message = first.get("message") if isinstance(first, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise ValueError("OpenRouter 响应缺少结构化文本")
        data = extract_json_object(content)
        usage = self._parse_usage(response.get("usage"))
        self.usage = self.usage + usage
        self.logical_calls += 1
        self.cache.set(key, {"data": data, "provider_usage": asdict(usage)})
        return StructuredResult(
            data=data,
            usage=usage,
            cached=False,
            request_id=str(response["id"]) if response.get("id") is not None else None,
            model=str(response["model"]) if response.get("model") is not None else self.model,
        )

    def _verify_free_model_from_live_catalog(self) -> None:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        try:
            payload = self.model_catalog_transport(
                self.models_endpoint,
                headers,
                self.timeout,
            )
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            raise ModelCatalogVerificationError(
                "无法实时验证 OpenRouter 模型是否免费，M2 已阻断"
            ) from error
        models = payload.get("data")
        if not isinstance(models, list):
            raise ModelCatalogVerificationError(
                "OpenRouter 实时模型目录格式无效，M2 已阻断"
            )
        selected = next(
            (
                item
                for item in models
                if isinstance(item, dict) and item.get("id") == self.model
            ),
            None,
        )
        if selected is None:
            raise ModelCatalogVerificationError(
                f"模型 {self.model} 不在实时模型目录中，M2 已阻断"
            )
        pricing = selected.get("pricing")
        if not isinstance(pricing, dict):
            raise ModelCatalogVerificationError(
                f"模型 {self.model} 缺少实时价格，无法证明免费，M2 已阻断"
            )
        required_prices = ("prompt", "completion")
        if any(field not in pricing for field in required_prices):
            raise ModelCatalogVerificationError(
                f"模型 {self.model} 价格字段不完整，无法证明免费，M2 已阻断"
            )
        try:
            prices = [Decimal(str(value)) for value in pricing.values()]
        except (InvalidOperation, ValueError):
            raise ModelCatalogVerificationError(
                f"模型 {self.model} 价格格式无效，无法证明免费，M2 已阻断"
            ) from None
        if not prices or any(price != 0 for price in prices):
            raise ModelCatalogVerificationError(
                f"模型 {self.model} 不是免费模型，M2 已阻断"
            )

    def _request_with_fallback(
        self,
        headers: dict[str, str],
        base_body: dict[str, object],
        json_schema: dict[str, object],
    ) -> dict[str, object]:
        modes = ("json_schema", "json_object", "strict_prompt")
        last_error: HTTPError | None = None
        for index, mode in enumerate(modes):
            body = dict(base_body)
            messages = [dict(message) for message in base_body["messages"]]  # type: ignore[arg-type]
            body["messages"] = messages
            if mode == "json_schema":
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": json_schema,
                }
            elif mode == "json_object":
                body["response_format"] = {"type": "json_object"}
            else:
                schema_text = json.dumps(json_schema.get("schema", {}), ensure_ascii=False)
                messages[0]["content"] = (
                    f"{messages[0]['content']}\n"
                    "只允许输出一个严格 JSON 对象，不得输出 Markdown、解释或额外文本。"
                    f"输出必须符合此 JSON Schema：{schema_text}"
                )
            try:
                return self._request_with_retry(headers, body)
            except HTTPError as error:
                last_error = error
                detail = self._read_http_error(error)
                if index < len(modes) - 1 and self._response_format_unsupported(detail):
                    continue
                raise self._sanitized_http_error(error, detail) from None
        assert last_error is not None
        raise self._sanitized_http_error(last_error, self._read_http_error(last_error))

    @staticmethod
    def _read_http_error(error: HTTPError) -> str:
        try:
            payload = error.read().decode("utf-8", errors="replace")
        except Exception:
            return error.reason if isinstance(error.reason, str) else "请求失败"
        return payload[:4096]

    @staticmethod
    def _response_format_unsupported(detail: str) -> bool:
        normalized = detail.casefold()
        rejected = "unsupported" in normalized or "not supported" in normalized
        return rejected and (
            "response_format" in normalized
            or "json_schema" in normalized
            or "json_object" in normalized
        )

    def _sanitized_http_error(
        self,
        error: HTTPError,
        detail: str,
    ) -> OpenRouterHTTPError:
        summary = "请求被拒绝"
        try:
            payload = json.loads(detail)
            candidate = payload.get("error", {}).get("message")
            if isinstance(candidate, str) and candidate.strip():
                summary = candidate.strip()
        except (json.JSONDecodeError, AttributeError):
            if detail.strip():
                summary = detail.strip()
        secrets = (self.api_key, "secret-header", "secret-query")
        for secret in secrets:
            if secret:
                summary = summary.replace(secret, "[REDACTED]")
        summary = summary.replace("\r", " ").replace("\n", " ")[:240]
        return OpenRouterHTTPError(error.code, summary)

    def _request_with_retry(
        self,
        headers: dict[str, str],
        body: dict[str, object],
    ) -> dict[str, object]:
        for attempt in range(self.max_retries + 1):
            try:
                return self.transport(self.endpoint, headers, body, self.timeout)
            except HTTPError as error:
                retryable = error.code in {408, 409, 429} or error.code >= 500
                if not retryable or attempt >= self.max_retries:
                    raise
            except (URLError, TimeoutError):
                if attempt >= self.max_retries:
                    raise
            self.sleep(float(2**attempt))
        raise RuntimeError("OpenRouter 重试流程异常结束")

    @staticmethod
    def _parse_usage(value: Any) -> TokenUsage:
        source = value if isinstance(value, dict) else {}
        return TokenUsage(
            prompt_tokens=int(source.get("prompt_tokens", 0)),
            completion_tokens=int(source.get("completion_tokens", 0)),
            total_tokens=int(source.get("total_tokens", 0)),
        )
