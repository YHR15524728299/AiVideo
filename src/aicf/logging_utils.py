from __future__ import annotations

import logging
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable


_REDACTED = "***REDACTED***"
_SENSITIVE_QUERY_KEYS = (
    "access_token|api_key|apikey|auth|authorization|"
    "cookie|signature|sig|sign|token"
)
_RATE_LIMIT_LOCK = threading.Lock()
_RATE_LIMITED_EVENTS: dict[tuple[str, str, str], float] = {}


def sanitize_error(value: object) -> str:
    message = str(value)
    for name in (
        "OPENROUTER_API_KEY",
        "JIMENG_TOKEN",
        "JIMENG_COOKIE",
    ):
        secret = os.getenv(name)
        if secret:
            message = message.replace(secret, _REDACTED)
    message = re.sub(
        r"(?i)\bBearer\s+[^\s,;]+",
        f"Bearer {_REDACTED}",
        message,
    )
    message = re.sub(r"\bsk-or-[A-Za-z0-9._-]+", _REDACTED, message)
    message = re.sub(
        rf"(?i)\b({_SENSITIVE_QUERY_KEYS})"
        r"(\s*[:=]\s*)([\"']?)[^&\s,;\"']+\3",
        lambda match: (
            f"{match.group(1)}{match.group(2)}{match.group(3)}"
            f"{_REDACTED}{match.group(3)}"
        ),
        message,
    )
    message = re.sub(
        rf"(?i)([?&]({_SENSITIVE_QUERY_KEYS})=)[^&#\s]+",
        lambda match: f"{match.group(1)}{_REDACTED}",
        message,
    )
    try:
        home = str(Path.home())
        if home:
            message = message.replace(home, "<USER_PATH>")
            message = message.replace(home.replace("\\", "/"), "<USER_PATH>")
    except RuntimeError:
        home = ""
    message = re.sub(
        r"(?i)\b[A-Z]:\\Users\\[^\\\s,;]+(?:\\[^\s,;]+)*",
        "<USER_PATH>",
        message,
    )
    message = re.sub(
        r"(?<![\w.])/(?:home|Users)/[^/\s,;]+(?:/[^\s,;]+)*",
        "<USER_PATH>",
        message,
    )
    return message


def log_state_exception(
    logger: logging.Logger,
    *,
    event: str,
    source: str,
    error: BaseException,
    job_id: str = "",
    interval_seconds: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """结构化且按事件/来源/任务限流记录关键状态读取异常。"""
    key = (event, source, job_id)
    now = clock()
    with _RATE_LIMIT_LOCK:
        previous = _RATE_LIMITED_EVENTS.get(key)
        if previous is not None and now - previous < interval_seconds:
            return False
        _RATE_LIMITED_EVENTS[key] = now
    payload = {
        "event": event,
        "source": source,
        "job_id": job_id or None,
        "error_type": type(error).__name__,
        "error": sanitize_error(error),
    }
    logger.warning(
        "job_state_health %s",
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
    )
    return True


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = sanitize_error(record.getMessage())
        record.args = ()
        return True


def configure_logging(path: str | Path, logger_name: str = "aicf") -> logging.Logger:
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    handler.addFilter(SecretFilter())
    logger.addHandler(handler)
    return logger
