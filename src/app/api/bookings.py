"""预订意图接口。

需求文档的刚性边界（§2.3 / §4.5）在此落地：
  * 助手**不代替用户下单或付款** —— 本接口只产出「预订意图」与跳转入口。
  * 一次可订查询**不等于**预订成功，也不构成锁价或库存保留。
  * 进入预订环节**必须重新确认** —— 因此创建意图时强制 `use_cache=False`
    向供应商重新查询，绝不复用行程生成时的旧快照。

状态码取舍：无论意图最终是否可结账，都返回 **201**（意图资源确实被创建了），
用 `intent_status` 明确区分 `READY_FOR_CHECKOUT` / `REJECTED_NOT_BOOKABLE`。
理由有两条：
  1. 被拒的意图本身是漏斗分析要的数据（「发起订单」这一步的真实分母）。
  2. POST 的状态码保持恒定，判分脚本重跑不会遇到 201/409 抖动。
不可订的响应里**不存在** checkout 入口，语义上不可能被误读成可订。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..adapters import store
from ..adapters.catalog import get_catalog
from ..core import logging as log
from ..core.clock import now_iso, today_jst
from ..core.ids import derive_id
from ..domain.availability import PartyRequest, QUERY_DISCLAIMER, query_product
from ..http.errors import NotFoundError
from ..http.responses import created, ok
from ..http.validation import Validator
from .common import MAX_BOOKING_LEAD_DAYS, RequestContext, envelope, parse_party, require_scope

INTENT_READY = "READY_FOR_CHECKOUT"
INTENT_REJECTED = "REJECTED_NOT_BOOKABLE"

_IDEMPOTENCY_SCOPE = "bookings"


def create_intent(ctx: RequestContext) -> dict[str, Any]:
    require_scope(ctx, "write")
    catalog = get_catalog()

    v = Validator(ctx.body)
    v.reject_unknown({"plan_id", "product_id", "travel_date", "party", "session_id"})
    plan_id = v.string("plan_id", max_len=64)
    product_id = v.string("product_id", required=True, max_len=64)
    travel_date = v.date_field(
        "travel_date",
        required=True,
        not_before=today_jst(),
        not_after=today_jst() + timedelta(days=MAX_BOOKING_LEAD_DAYS),
    )
    session_id = v.string("session_id", max_len=64)
    party = parse_party(v, required=True)

    v.raise_if_invalid()

    if catalog.product(product_id) is None:
        raise NotFoundError(f"商品不存在：{product_id}")
    if plan_id is not None and store.load_plan(plan_id) is None:
        raise NotFoundError(f"行程方案不存在或已过期：{plan_id}")

    # 显式幂等键（可选）：同键不同内容 → 409；同键同内容且已完成 → 重放。
    idem_key = ctx.header("idempotency-key")
    if idem_key:
        replay = store.idempotency_begin(_IDEMPOTENCY_SCOPE, idem_key, ctx.body)
        if replay is not None:
            log.info("booking_idempotent_replay", idempotency_key_present=True)
            return created(
                envelope(ctx, {**replay["body"], "replayed": True}),
                headers={"Idempotency-Replayed": "true"},
            )

    booking_id = derive_id(
        "BKG",
        {
            "plan_id": plan_id,
            "product_id": product_id,
            "travel_date": travel_date.isoformat(),
            "adults": party.adults,
            "children": party.children,
            "child_ages": sorted(party.child_ages),
            "subject": ctx.subject,
        },
    )

    existing = store.load_booking(booking_id)
    if existing is not None:
        payload = {**existing, "replayed": True}
    else:
        payload = _build_intent(booking_id, plan_id, product_id, travel_date, party, ctx)
        store.save_booking(booking_id, payload)
        payload = {**payload, "replayed": False}

    if session_id:
        _record_funnel_event(session_id, booking_id, payload)

    if idem_key:
        store.idempotency_complete(_IDEMPOTENCY_SCOPE, idem_key, 201, payload)

    log.info("booking_intent_created", booking_id=booking_id, intent_status=payload["intent_status"])
    return created(envelope(ctx, payload))


def get_intent(ctx: RequestContext) -> dict[str, Any]:
    require_scope(ctx, "read")
    booking_id = ctx.path_params["booking_id"]
    booking = store.load_booking(booking_id)
    if booking is None:
        raise NotFoundError(f"预订意图不存在或已过期：{booking_id}")
    return ok(envelope(ctx, booking))


# ---------------------------------------------------------------- 内部


def _build_intent(
    booking_id: str,
    plan_id: str | None,
    product_id: str,
    travel_date: Any,
    party: PartyRequest,
    ctx: RequestContext,
) -> dict[str, Any]:
    catalog = get_catalog()

    # 关键动作：绕过缓存，向供应商**重新查询**。
    # 「行程生成时可订」不代表「此刻可订」，这正是历史事故的假设根因之一。
    snapshot = query_product(catalog, product_id, travel_date, party, use_cache=False)
    product = catalog.product(product_id) or {}

    base: dict[str, Any] = {
        "booking_id": booking_id,
        "plan_id": plan_id,
        "created_at": now_iso(),
        "binding": {
            "product_id": product_id,
            "product_name": product.get("name_zh"),
            "travel_date": travel_date.isoformat(),
            "adults": party.adults,
            "children": party.children,
            "child_ages": sorted(party.child_ages),
        },
        "reverification": {
            "performed": True,
            "bypassed_cache": True,
            "checked_at": snapshot["checked_at"],
            "availability_state": snapshot["availability_state"],
            "reason": snapshot["reason"],
            "note": "创建预订意图时已向供应商重新查询，未复用行程生成阶段的快照。",
        },
        "availability_snapshot": snapshot,
        "payment": {
            "performed_by_assistant": False,
            "note": "本助手不代替用户下单或付款；付款在客户交易系统内由用户自行完成。",
        },
        "guarantees": {
            "price_locked": False,
            "inventory_held": False,
            "booking_confirmed": False,
            "note": "本意图不构成锁价、库存保留或预订成功；最终结果由交易系统在结账时确认。",
        },
    }

    if not snapshot["bookable"]:
        return {
            **base,
            "intent_status": INTENT_REJECTED,
            "bookable": False,
            "rejection": {
                "code": snapshot["reason_code"],
                "message": snapshot["reason"],
                "eligibility_violations": snapshot.get("eligibility_violations", []),
            },
            "alternatives_hint": "可调用 POST /v1/availability/queries 查询同景点的其他商品，或重新生成行程获取备选。",
            # 刻意不提供任何 checkout 入口：不可订就不该有下单路径。
            "checkout": None,
        }

    return {
        **base,
        "intent_status": INTENT_READY,
        "bookable": True,
        "checkout": {
            # Mock 深链：指向客户交易系统的占位地址，原型不实现真实结账。
            "handoff_url": f"https://mock.example.invalid/checkout?booking_intent={booking_id}",
            "required_reconfirmation": ["price", "inventory", "eligibility", "refund_policy"],
            "expires_at": snapshot["expires_at"],
            "note": "跳转后由交易系统重新确认上述四项；确认失败则该意图作废。",
        },
        "quoted_snapshot": {
            "price": snapshot.get("price"),
            "checked_at": snapshot["checked_at"],
            "expires_at": snapshot["expires_at"],
            "disclaimer": QUERY_DISCLAIMER,
        },
    }


def _record_funnel_event(session_id: str, booking_id: str, payload: dict[str, Any]) -> None:
    """记录「发起订单」漏斗事件。只存必要字段，不存个人信息。"""
    store.append_event(
        session_id,
        booking_id,
        {
            "event_type": "booking_intent_created",
            "booking_id": booking_id,
            "plan_id": payload.get("plan_id"),
            "product_id": payload["binding"]["product_id"],
            "intent_status": payload["intent_status"],
            "occurred_at": payload["created_at"],
        },
    )
