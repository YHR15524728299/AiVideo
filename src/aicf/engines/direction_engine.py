from __future__ import annotations

from aicf.engines.llm_engine import StructuredEngine
from aicf.models.contracts import DirectionProfile


class DirectionEngine(StructuredEngine):
    stage = "direction"
    result_model = DirectionProfile
    system_prompt = (
        "你是内容方向策略师。仅依据输入分析系列定位、受众、内容边界、差异化、"
        "重复风险和事实风险。必须返回符合 JSON Schema 的对象，不得虚构来源。"
    )

    def analyze(self, direction: dict[str, object]) -> DirectionProfile:
        return self.generate(direction)
