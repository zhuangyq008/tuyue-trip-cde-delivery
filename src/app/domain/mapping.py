"""景点 → 可预订商品映射。

需求文档 §2.2 的刚性约束：「推荐绑定明确商品标识、日期、人数和适用条件；
**不把内容相似当作可购买证明**」。

因此映射**只走目录中显式维护的 `linked_product_ids`**，不做文本相似度匹配。
每条映射都带 `mapping_method` 字段，使「这个商品凭什么被推给这个景点」可审计。
景点没有对应商品时如实返回 `has_bookable_product=false`，
绝不用一个相近商品填坑——那正是「看中了却订不了」的成因。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..adapters.catalog import Catalog
from ..core.clock import parse_date
from .availability import (
    NON_BOOKABLE_STATES,
    PartyRequest,
    STATE_AVAILABLE,
    query_product,
)
from .itinerary import ITEM_ATTRACTION, TripRequest

MAPPING_METHOD = "explicit_catalog_link"


def map_plan_products(
    catalog: Catalog,
    plan: dict[str, Any],
    request: TripRequest,
    *,
    check_availability: bool,
) -> dict[str, Any]:
    """为行程中每个景点找出绑定商品，并可选地做动态可订查询。"""
    party = PartyRequest(adults=request.adults, children=request.children, child_ages=list(request.child_ages))

    entries: list[dict[str, Any]] = []
    for day in plan["days"]:
        travel_date = parse_date(day["date"])
        if travel_date is None:
            continue
        for item in day["items"]:
            if item["type"] != ITEM_ATTRACTION:
                continue
            entries.append(
                _map_item(
                    catalog,
                    item,
                    travel_date,
                    party,
                    day_index=day["day_index"],
                    check_availability=check_availability,
                )
            )

    return {
        "mapping_method": MAPPING_METHOD,
        "mapping_note": "映射来自目录显式维护的商品绑定关系，不使用文本相似度推断。",
        "availability_checked": check_availability,
        "entries": entries,
        "summary": _summarise(entries, check_availability),
    }


def _map_item(
    catalog: Catalog,
    item: dict[str, Any],
    travel_date: date,
    party: PartyRequest,
    *,
    day_index: int,
    check_availability: bool,
) -> dict[str, Any]:
    product_ids = list(item.get("linked_product_ids") or [])
    candidates: list[dict[str, Any]] = []

    for product_id in sorted(product_ids):
        product = catalog.product(product_id)
        if product is None:
            # 目录内部引用不一致：如实暴露，不静默跳过。
            candidates.append(
                {
                    "product_id": product_id,
                    "product_name": None,
                    "mapping_method": MAPPING_METHOD,
                    "catalog_integrity": "DANGLING_REFERENCE",
                    "bookable": False,
                    "availability_state": "NOT_ELIGIBLE",
                    "reason": "目录中不存在该商品标识，映射数据需修复。",
                }
            )
            continue

        candidate: dict[str, Any] = {
            "product_id": product_id,
            "product_name": product.get("name_zh"),
            "product_type": product.get("product_type"),
            "supplier": product.get("supplier_name"),
            "mapping_method": MAPPING_METHOD,
            "catalog_integrity": "OK",
            "requires_reservation": bool(product.get("requires_reservation")),
            "refund_policy": product.get("refund_policy"),
            # 参考价仅用于排序展示，明确标注不是报价。
            "price_reference": product.get("price_hint"),
            # 绑定要素齐备才允许进入预订，缺一不可。
            "binding": {
                "product_id": product_id,
                "travel_date": travel_date.isoformat(),
                "adults": party.adults,
                "children": party.children,
                "child_ages": sorted(party.child_ages),
            },
        }

        if check_availability:
            snapshot = query_product(catalog, product_id, travel_date, party)
            candidate["availability"] = snapshot
            candidate["availability_state"] = snapshot["availability_state"]
            candidate["bookable"] = snapshot["bookable"]
            candidate["reason"] = snapshot["reason"]
        else:
            # 未查询就是未知，不允许默认为可订。
            candidate["availability_state"] = "UNKNOWN"
            candidate["bookable"] = False
            candidate["reason"] = "本次未发起动态可订查询，可订状态未知；未查询不等于可订。"

        candidates.append(candidate)

    bookable_candidates = [c for c in candidates if c.get("bookable")]
    return {
        "day_index": day_index,
        "poi_id": item["poi_id"],
        "poi_name": item["poi_name"],
        "travel_date": travel_date.isoformat(),
        "has_bookable_product": bool(bookable_candidates),
        "candidate_count": len(candidates),
        "candidates": candidates,
        "note": (
            "该景点在当前目录中没有绑定任何可预订商品，仅作行程安排，不提供预订入口。"
            if not candidates
            else None
        ),
    }


def _summarise(entries: list[dict[str, Any]], check_availability: bool) -> dict[str, Any]:
    all_candidates = [c for entry in entries for c in entry["candidates"]]
    state_counts: dict[str, int] = {}
    for candidate in all_candidates:
        state = candidate.get("availability_state", "UNKNOWN")
        state_counts[state] = state_counts.get(state, 0) + 1

    pois_with_product = sum(1 for e in entries if e["candidate_count"] > 0)
    pois_bookable = sum(1 for e in entries if e["has_bookable_product"])

    return {
        "attraction_count": len(entries),
        "attractions_with_mapped_product": pois_with_product,
        "attractions_with_bookable_product": pois_bookable,
        "attractions_without_any_product": len(entries) - pois_with_product,
        "candidate_total": len(all_candidates),
        "candidate_bookable": state_counts.get(STATE_AVAILABLE, 0),
        "candidate_not_bookable": sum(state_counts.get(state, 0) for state in NON_BOOKABLE_STATES),
        "state_breakdown": dict(sorted(state_counts.items())),
        "note": (
            "可订判定基于查询时点的供应商快照，具有时效。"
            if check_availability
            else "本次未查询供应商，所有商品的可订状态均为未知。"
        ),
    }
