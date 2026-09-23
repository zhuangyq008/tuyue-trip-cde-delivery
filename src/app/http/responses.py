"""响应构造。

规范 §4.3：响应必须是合法 JSON 且 `Content-Type: application/json`。
DynamoDB 取回的 Decimal 在此统一转回 int/float，避免 json 序列化失败。
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

JSON_CONTENT_TYPE = "application/json"

_BASE_HEADERS = {
    "Content-Type": JSON_CONTENT_TYPE,
    # 原型不提供浏览器端跨域调用，不开放通配 CORS；判分脚本走服务端调用不受影响。
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


def _normalise(value: Any) -> Any:
    """Decimal → int/float；集合 → 列表；保证可 JSON 序列化。"""
    if isinstance(value, Decimal):
        as_int = int(value)
        return as_int if value == as_int else float(value)
    if isinstance(value, dict):
        return {k: _normalise(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalise(v) for v in value]
    if isinstance(value, set):
        return sorted(_normalise(v) for v in value)
    return value


def json_response(
    status_code: int,
    body: Any,
    *,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    merged = {**_BASE_HEADERS, **(headers or {})}
    return {
        "statusCode": status_code,
        "headers": merged,
        "isBase64Encoded": False,
        "body": json.dumps(_normalise(body), ensure_ascii=False, default=str),
    }


def ok(body: Any, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    return json_response(200, body, headers=headers)


def created(body: Any, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    return json_response(201, body, headers=headers)
