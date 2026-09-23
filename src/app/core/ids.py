"""内容寻址 ID 与请求指纹。

设计取舍（对应 CDE 规范 §4.5「幂等：用例会重跑，同一请求重复调用结果须一致」）：
资源 ID **不使用随机 UUID**，而由请求的规范化内容派生。
这样同一份请求体重复 POST 必然命中同一条资源，幂等性由构造保证，
不依赖调用方是否传 Idempotency-Key。

所有 ID 带 `MOCK-` 前缀，满足规范 §2「假值须一眼可辨」。
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

_ALPHABET_TRIM = str.maketrans({"=": None, "+": "", "/": ""})


def canonical_json(payload: Any) -> str:
    """规范化 JSON：键排序、无多余空格，保证同义请求得到同一指纹。"""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def fingerprint(payload: Any) -> str:
    """请求内容的 SHA-256 全长十六进制指纹（用于幂等冲突判定）。"""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _short(seed: str, length: int) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    encoded = base64.b32encode(digest).decode("ascii").translate(_ALPHABET_TRIM)
    return encoded[:length]


def derive_id(prefix: str, *parts: Any) -> str:
    """由业务要素派生稳定 ID，例如 MOCK-PLAN-4NTQK7VZB2XA。"""
    seed = "|".join(canonical_json(part) for part in parts)
    return f"MOCK-{prefix}-{_short(seed, 12)}"


def request_id(event: dict[str, Any]) -> str:
    """优先复用 API Gateway 请求 ID，便于与 CloudWatch 日志对账。"""
    ctx = event.get("requestContext") or {}
    native = ctx.get("requestId")
    if isinstance(native, str) and native:
        return native
    return f"local-{_short(canonical_json(event), 16)}"
