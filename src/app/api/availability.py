"""动态可订查询接口。

需求文档 §4.3 P0「不可订与接口异常处理」的对外出口。
批量查询中单条商品失败**不会**让整个请求失败：失败条目按真实状态返回，
这样调用方能看到「哪条不可订、为什么」，而不是拿到一个 500 后无从下手。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..adapters.catalog import get_catalog
from ..core import logging as log
from ..core.clock import now_iso, today_jst
from ..domain.availability import (
    NON_BOOKABLE_STATES,
    QUERY_DISCLAIMER,
    STATE_AVAILABLE,
    query_product,
)
from ..http.errors import NotFoundError, field_error
from ..http.responses import ok
from ..http.validation import Validator
from .common import MAX_BOOKING_LEAD_DAYS, RequestContext, envelope, parse_party, require_scope

MAX_ITEMS_PER_QUERY = 20


def query(ctx: RequestContext) -> dict[str, Any]:
    """POST /v1/availability/queries —— 批量查询可订状态。"""
    require_scope(ctx, "read")
    catalog = get_catalog()

    v = Validator(ctx.body)
    v.reject_unknown({"items", "party", "use_cache"})
    items = v.object_list("items", required=True, min_items=1, max_items=MAX_ITEMS_PER_QUERY)
    party = parse_party(v, required=True)
    use_cache = v.boolean("use_cache", default=True)

    parsed_items: list[tuple[str, Any]] = []
    for index, raw in enumerate(items or []):
        item_v = Validator(raw, prefix=f"items[{index}].")
        item_v.reject_unknown({"product_id", "travel_date"})
        product_id = item_v.string("product_id", required=True, max_len=64)
        travel_date = item_v.date_field(
            "travel_date",
            required=True,
            not_before=today_jst(),
            not_after=today_jst() + timedelta(days=MAX_BOOKING_LEAD_DAYS),
        )
        # 缺失与非法都已由子校验器记入 errors，此处只负责收集有效项。
        v.adopt(item_v)
        if product_id is not None and travel_date is not None:
            parsed_items.append((product_id, travel_date))

    v.raise_if_invalid()

    results = [
        query_product(catalog, product_id, travel_date, party, use_cache=bool(use_cache))
        for product_id, travel_date in parsed_items
    ]

    log.info(
        "availability_queried",
        item_count=len(results),
        bookable=sum(1 for r in results if r["bookable"]),
    )

    return ok(envelope(ctx, {"queried_at": now_iso(), **_summarise(results)}))


def query_single(ctx: RequestContext) -> dict[str, Any]:
    """GET /v1/products/{product_id}/availability?travel_date=&adults=&child_ages=

    单资源形态：商品不存在时给 404（与批量查询的容错语义刻意不同）。
    """
    require_scope(ctx, "read")
    catalog = get_catalog()
    product_id = ctx.path_params["product_id"]

    if catalog.product(product_id) is None:
        raise NotFoundError(f"商品不存在：{product_id}")

    # 查询串参数统一先转成与 JSON body 同形的结构，复用同一套校验器。
    q = dict(ctx.query)
    child_ages_raw = q.get("child_ages", "").strip()
    child_ages: list[int] = []
    bad_ages: list[str] = []
    if child_ages_raw:
        for token in child_ages_raw.split(","):
            token = token.strip()
            if token.isdigit():
                child_ages.append(int(token))
            else:
                bad_ages.append(token)

    payload = {
        "party": {
            "adults": _as_int(q.get("adults")),
            "children": len(child_ages) if child_ages_raw else _as_int(q.get("children")) or 0,
            "child_ages": child_ages if child_ages_raw else None,
        },
        "travel_date": q.get("travel_date"),
    }
    if payload["party"]["child_ages"] is None:
        payload["party"].pop("child_ages")

    v = Validator(payload)
    travel_date = v.date_field(
        "travel_date",
        required=True,
        not_before=today_jst(),
        not_after=today_jst() + timedelta(days=MAX_BOOKING_LEAD_DAYS),
    )
    party = parse_party(v, required=True)

    for token in bad_ages:
        v.errors.append(field_error("child_ages", f"不是合法的整数年龄：{token!r}", "逗号分隔的整数，如 5,8"))
    v.raise_if_invalid()

    snapshot = query_product(catalog, product_id, travel_date, party)
    return ok(envelope(ctx, {"queried_at": now_iso(), "result": snapshot, "disclaimer": QUERY_DISCLAIMER}))


def _as_int(value: Any) -> int | None:
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _summarise(results: list[dict[str, Any]]) -> dict[str, Any]:
    state_counts: dict[str, int] = {}
    for result in results:
        state_counts[result["availability_state"]] = state_counts.get(result["availability_state"], 0) + 1

    return {
        "results": results,
        "summary": {
            "total": len(results),
            "bookable": state_counts.get(STATE_AVAILABLE, 0),
            "not_bookable": sum(state_counts.get(state, 0) for state in NON_BOOKABLE_STATES),
            "state_breakdown": dict(sorted(state_counts.items())),
            "invariant": "bookable == true 当且仅当 availability_state == 'AVAILABLE'；其余四态一律不可订。",
        },
        "disclaimer": QUERY_DISCLAIMER,
    }
