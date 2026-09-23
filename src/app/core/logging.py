"""结构化 JSON 日志 + 强制脱敏。

CDE 规范 §3：日志为结构化 JSON、保留 7 天，**严禁打印令牌与请求头全量**。
本模块是唯一的日志出口；调用方无法绕过脱敏。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from typing import Any, Mapping

_SERVICE = os.environ.get("SERVICE_NAME", "yuetu-trip-assistant")

# 敏感字段名（大小写不敏感，子串匹配）——命中即整体替换为掩码。
_SENSITIVE_KEYS = (
    "authorization",
    "token",
    "secret",
    "password",
    "passwd",
    "credential",
    "apikey",
    "api_key",
    "x-api-key",
    "cookie",
    "set-cookie",
    "session",
    "idempotency-key",
)

# 兜底：即使字段名未命中，值里出现 Bearer 令牌形态也要打掉。
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-=]+")

_MASK = "***REDACTED***"
_MAX_STR = 512


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in _SENSITIVE_KEYS)


def redact(value: Any, _depth: int = 0) -> Any:
    """递归脱敏：敏感键掩码、Bearer 形态掩码、超长字符串截断。"""
    if _depth > 8:
        return "***DEPTH_LIMIT***"
    if isinstance(value, Mapping):
        return {
            str(k): (_MASK if _is_sensitive(str(k)) else redact(v, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v, _depth + 1) for v in value[:50]]
    if isinstance(value, str):
        cleaned = _BEARER_RE.sub(_MASK, value)
        return cleaned if len(cleaned) <= _MAX_STR else cleaned[:_MAX_STR] + "...<truncated>"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value), _depth + 1)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "service": _SERVICE,
            "message": record.getMessage(),
            "logger": record.name,
        }
        extra = getattr(record, "context", None)
        if isinstance(extra, Mapping):
            payload.update(redact(dict(extra)))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _build_logger() -> logging.Logger:
    logger = logging.getLogger(_SERVICE)
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
    return logger


_LOGGER = _build_logger()

# 请求级上下文，由 handler 在每次调用开始时设置。
_REQUEST_CONTEXT: dict[str, Any] = {}


def bind_request_context(**fields: Any) -> None:
    _REQUEST_CONTEXT.clear()
    _REQUEST_CONTEXT.update(redact(fields))


def _log(level: int, message: str, **fields: Any) -> None:
    merged = {**_REQUEST_CONTEXT, **fields}
    _LOGGER.log(level, message, extra={"context": merged})


def info(message: str, **fields: Any) -> None:
    _log(logging.INFO, message, **fields)


def warn(message: str, **fields: Any) -> None:
    _log(logging.WARNING, message, **fields)


def error(message: str, **fields: Any) -> None:
    _LOGGER.log(
        logging.ERROR,
        message,
        exc_info=fields.pop("exc_info", False),
        extra={"context": {**_REQUEST_CONTEXT, **fields}},
    )


def debug(message: str, **fields: Any) -> None:
    _log(logging.DEBUG, message, **fields)
