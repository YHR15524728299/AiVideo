from __future__ import annotations

import os
import tempfile
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


def atomic_write_text(
    target: str | Path,
    content: str,
    *,
    encoding: str = "utf-8",
) -> None:
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        dir=target_path.parent,
        prefix=f".{target_path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(temp_path, target_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
