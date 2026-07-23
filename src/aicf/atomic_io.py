from __future__ import annotations

import os
import time
from pathlib import Path


_IS_WINDOWS = os.name == "nt"
_SHARING_VIOLATION_ERRORS = frozenset({5, 32})
_INITIAL_RETRY_DELAY_SECONDS = 0.01
_MAX_RETRY_SECONDS = 1.0


def atomic_replace(source: str | Path, target: str | Path) -> None:
    deadline = time.monotonic() + _MAX_RETRY_SECONDS
    delay = _INITIAL_RETRY_DELAY_SECONDS
    while True:
        try:
            os.replace(source, target)
            return
        except OSError as error:
            if (
                not _IS_WINDOWS
                or getattr(error, "winerror", None)
                not in _SHARING_VIOLATION_ERRORS
            ):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay *= 2
