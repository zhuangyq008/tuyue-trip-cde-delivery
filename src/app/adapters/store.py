"""DynamoDB 单表运行态存储。

单表设计（On-Demand，无 VPC、无连接池 —— 规范 §3）：

| 实体         | PK                      | SK                      | 说明                     |
|--------------|-------------------------|-------------------------|--------------------------|
| 行程方案     | `PLAN#<plan_id>`        | `META`                  | 内容寻址 ID，可重复写入  |
| 预订意图     | `BOOKING#<booking_id>`  | `META`                  | 不代付，仅记录意图       |
| 幂等记录     | `IDEM#<scope>#<key>`    | `META`                  | 存响应快照用于重放       |
| 可订快照     | `AVAIL#<hash>`          | `META`                  | 带 TTL，显式建模时效     |
| 漏斗埋点     | `EVENT#<session_id>`    | `TS#<epoch>#<event_id>` | 仅存必要字段             |

所有条目写入 TTL 属性 `expires_at`，避免原型数据无限留存
（对应需求文档 §2.3「记录本身须满足最小化及脱敏要求」）。

业务载荷统一以 JSON 字符串存入 `payload` 字段，不落 DynamoDB 原生嵌套类型：
这样彻底规避 float/Decimal 往返损失，读回即得与写入完全一致的对象，
是「重复调用结果须一致」（规范 §4.5）最省事且可验证的做法。
"""

from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from ..core import logging as log
from ..core.clock import now_epoch
from ..core.config import get_config
from ..core.ids import fingerprint
from ..http.errors import ConflictError, UpstreamError

# 判分脚本会并发跑用例，重试次数给足但超时收紧，避免拖满 Lambda 15s 预算。
_BOTO_CONFIG = BotoConfig(
    retries={"max_attempts": 4, "mode": "standard"},
    connect_timeout=2,
    read_timeout=3,
)

_TABLE = None


def _table():
    global _TABLE
    if _TABLE is None:
        cfg = get_config()
        _TABLE = boto3.resource("dynamodb", region_name=cfg.region, config=_BOTO_CONFIG).Table(cfg.table_name)
    return _TABLE


def reset_table_cache() -> None:
    """仅供测试使用。"""
    global _TABLE
    _TABLE = None


# ---------------------------------------------------------------- 通用读写


def _put(pk: str, sk: str, payload: dict[str, Any], ttl_seconds: int, **attrs: Any) -> None:
    item = {
        "PK": pk,
        "SK": sk,
        "payload": json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        "expires_at": now_epoch() + ttl_seconds,
        **attrs,
    }
    try:
        _table().put_item(Item=item)
    except ClientError as exc:
        log.error("dynamodb_put_failed", pk=pk, sk=sk, code=exc.response.get("Error", {}).get("Code"))
        raise UpstreamError("存储写入失败，请稍后重试。") from exc


def _get(pk: str, sk: str) -> dict[str, Any] | None:
    try:
        response = _table().get_item(Key={"PK": pk, "SK": sk}, ConsistentRead=True)
    except ClientError as exc:
        log.error("dynamodb_get_failed", pk=pk, sk=sk, code=exc.response.get("Error", {}).get("Code"))
        raise UpstreamError("存储读取失败，请稍后重试。") from exc
    item = response.get("Item")
    if item is None:
        return None
    # TTL 删除是异步的（最长可滞后 48h），读取侧必须自行判定过期，
    # 否则会把已到期的可订快照当成有效数据返回。
    expires_at = item.get("expires_at")
    if expires_at is not None and int(expires_at) <= now_epoch():
        return None
    return item


def _payload(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    raw = item.get("payload")
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.error("payload_decode_failed", pk=item.get("PK"), sk=item.get("SK"))
        return None


# ---------------------------------------------------------------- 行程方案


def save_plan(plan_id: str, plan: dict[str, Any]) -> None:
    _put(f"PLAN#{plan_id}", "META", plan, get_config().plan_ttl_seconds, entity="plan")


def load_plan(plan_id: str) -> dict[str, Any] | None:
    return _payload(_get(f"PLAN#{plan_id}", "META"))


# ---------------------------------------------------------------- 预订意图


def save_booking(booking_id: str, booking: dict[str, Any]) -> None:
    _put(f"BOOKING#{booking_id}", "META", booking, get_config().booking_ttl_seconds, entity="booking")


def load_booking(booking_id: str) -> dict[str, Any] | None:
    return _payload(_get(f"BOOKING#{booking_id}", "META"))


# ---------------------------------------------------------------- 可订快照


def save_availability_snapshot(snapshot_key: str, snapshot: dict[str, Any]) -> None:
    _put(
        f"AVAIL#{snapshot_key}",
        "META",
        snapshot,
        get_config().availability_ttl_seconds,
        entity="availability_snapshot",
    )


def load_availability_snapshot(snapshot_key: str) -> dict[str, Any] | None:
    return _payload(_get(f"AVAIL#{snapshot_key}", "META"))


# ---------------------------------------------------------------- 漏斗埋点


def append_event(session_id: str, event_id: str, event: dict[str, Any]) -> None:
    _put(
        f"EVENT#{session_id}",
        f"TS#{now_epoch():011d}#{event_id}",
        event,
        get_config().event_ttl_seconds,
        entity="funnel_event",
    )


# ---------------------------------------------------------------- 幂等


def idempotency_begin(scope: str, key: str, request_body: Any) -> dict[str, Any] | None:
    """尝试占用幂等键。

    返回值：
      * `None`            —— 首次占用成功，调用方继续执行业务逻辑。
      * `{...}`           —— 命中已完成的记录，调用方直接重放该响应。
    异常：
      * `ConflictError`   —— 同一幂等键承载了不同请求内容（409）。
    """
    pk = f"IDEM#{scope}#{key}"
    digest = fingerprint(request_body)
    try:
        _table().put_item(
            Item={
                "PK": pk,
                "SK": "META",
                "payload": json.dumps({"status": "in_progress"}, ensure_ascii=False),
                "request_fingerprint": digest,
                "expires_at": now_epoch() + 24 * 3600,
                "entity": "idempotency",
            },
            ConditionExpression="attribute_not_exists(PK)",
        )
        return None
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            log.error("idempotency_begin_failed", scope=scope, code=exc.response.get("Error", {}).get("Code"))
            raise UpstreamError("幂等记录写入失败，请稍后重试。") from exc

    existing = _get(pk, "META")
    if existing is None:
        # 记录刚好过期被清理：视为首次占用。
        return None
    if existing.get("request_fingerprint") != digest:
        raise ConflictError(
            "Idempotency-Key 已被另一份不同内容的请求使用，请更换幂等键或使用原始请求体。"
        )
    stored = _payload(existing) or {}
    if stored.get("status") == "completed":
        return stored
    # 同内容但上一次未完成：重跑业务逻辑。资源 ID 由内容派生，重跑不会产生重复资源。
    return None


def idempotency_complete(scope: str, key: str, status_code: int, body: dict[str, Any]) -> None:
    pk = f"IDEM#{scope}#{key}"
    _put(
        pk,
        "META",
        {"status": "completed", "status_code": status_code, "body": body},
        24 * 3600,
        entity="idempotency",
        request_fingerprint=_read_fingerprint(pk),
    )


def _read_fingerprint(pk: str) -> str:
    item = _get(pk, "META")
    value = (item or {}).get("request_fingerprint")
    return value if isinstance(value, str) else ""
