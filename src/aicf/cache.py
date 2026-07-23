from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from aicf.atomic_io import atomic_replace


class FileCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def make_key(stage: str, inputs: Any, model: str, prompt_version: str) -> str:
        payload = {
            "stage": stage,
            "inputs": inputs,
            "model": model,
            "prompt_version": prompt_version,
        }
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Any | None:
        path = self.root / f"{key}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def set(self, key: str, value: Any) -> Path:
        path = self.root / f"{key}.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        atomic_replace(temporary, path)
        return path
