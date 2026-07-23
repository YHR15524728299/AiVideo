from __future__ import annotations

import json
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from aicf.providers.openrouter import StructuredResult


class StructuredClient(Protocol):
    def call_structured(self, **kwargs: object) -> StructuredResult: ...


ModelT = TypeVar("ModelT", bound=BaseModel)


class StructuredEngine:
    stage: str
    prompt_version = "m2-v1"
    max_validation_repairs = 2
    system_prompt: str
    result_model: type[BaseModel]

    def __init__(self, client: StructuredClient) -> None:
        self.client = client

    def generate(self, payload: dict[str, object]) -> Any:
        schema = self.result_model.model_json_schema()
        schema_request = {
            "name": f"{self.stage}_result",
            "strict": True,
            "schema": schema,
        }
        request_payload = payload
        system_prompt = self.system_prompt
        prompt_version = self.prompt_version

        for repair_round in range(self.max_validation_repairs + 1):
            result = self.client.call_structured(
                stage=self.stage,
                system_prompt=system_prompt,
                user_payload=request_payload,
                json_schema=schema_request,
                prompt_version=prompt_version,
            )
            try:
                return self.result_model.model_validate(result.data)
            except ValidationError as error:
                if repair_round >= self.max_validation_repairs:
                    raise
                next_round = repair_round + 1
                request_payload = {
                    "original_request": payload,
                    "original_response": result.data,
                    "validation_error_summary": json.dumps(
                        error.errors(include_url=False, include_input=False),
                        ensure_ascii=False,
                        default=str,
                    ),
                    "repair_round": next_round,
                }
                system_prompt = (
                    f"{self.system_prompt}\n"
                    "上一份响应未通过本地 Pydantic 校验。请依据原请求、原响应和错误摘要"
                    "修正，只返回完整且符合 JSON Schema 的 JSON 对象。"
                )
                prompt_version = f"{self.prompt_version}-repair-{next_round}"

        raise RuntimeError("结构化响应修正流程异常结束")
