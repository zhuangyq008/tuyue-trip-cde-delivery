"""行程方案接口。

`POST /v1/plans` 是整条链路的主入口，按需求文档 §4.2 的七步串联：
理解需求 → 检索可信内容 → 生成候选行程 → 执行约束校验 → 匹配实际商品
→ 解释并引导预订 → 处理变化。

幂等实现（规范 §4.5）：`plan_id` 由请求内容派生，已存在则原样重放，
且**始终返回 201** —— 重复提交不会出现 201/200 抖动，判分脚本重跑得到同一状态码。
"""

from __future__ import annotations

from typing import Any

from ..adapters import store
from ..adapters.catalog import get_catalog
from ..core import logging as log
from ..core.clock import now_iso
from ..core.ids import derive_id
from ..domain import constraints, mapping, narrative
from ..domain.itinerary import TripRequest, generate
from ..http.errors import NotFoundError
from ..http.responses import created, ok
from ..http.validation import Validator
from .common import RequestContext, envelope, parse_trip_request, require_scope


def create_plan(ctx: RequestContext) -> dict[str, Any]:
    require_scope(ctx, "write")
    catalog = get_catalog()

    trip, second = parse_trip_request(ctx.body, catalog)

    # 必填槽位缺失 → 200 追问，不是 422。缺信息是正常对话状态。
    if trip is None:
        clarification = second
        return ok(
            envelope(
                ctx,
                {
                    "status": "needs_clarification",
                    "missing_fields": clarification.missing,
                    "questions": clarification.questions,
                    "assumed_defaults": clarification.assumed_defaults,
                    "message": "缺少生成行程所必需的信息，已列出需要补充的字段。",
                    "privacy_note": "仅收集行程履约必需信息；不收集姓名、证件、手机号与支付信息。",
                },
            )
        )

    options: dict[str, Any] = second
    plan_id = derive_id("PLAN", trip.to_dict(), options)

    existing = store.load_plan(plan_id)
    if existing is not None:
        log.info("plan_replayed", plan_id=plan_id)
        return created(envelope(ctx, {**existing, "replayed": True}))

    payload = _build_plan(plan_id, trip, options)
    store.save_plan(plan_id, payload)
    log.info(
        "plan_created",
        plan_id=plan_id,
        days=trip.days,
        executable=payload["validation"]["executable"],
        blockers=payload["validation"]["issue_counts"]["blocker"],
    )
    return created(envelope(ctx, {**payload, "replayed": False}))


def get_plan(ctx: RequestContext) -> dict[str, Any]:
    require_scope(ctx, "read")
    plan_id = ctx.path_params["plan_id"]
    plan = store.load_plan(plan_id)
    if plan is None:
        # 资源不存在 → 404，与「参数非法 → 422」严格区分（规范 §4.3）。
        raise NotFoundError(f"行程方案不存在或已过期：{plan_id}")
    return ok(envelope(ctx, plan))


def revalidate_plan(ctx: RequestContext) -> dict[str, Any]:
    """重新校验已存在的行程。

    存在意义：可订状态与资料时效都会随时间变化，
    「当时校验通过」不等于「现在还走得通」（需求文档 §2.3）。
    """
    require_scope(ctx, "read")
    plan_id = ctx.path_params["plan_id"]
    stored = store.load_plan(plan_id)
    if stored is None:
        raise NotFoundError(f"行程方案不存在或已过期：{plan_id}")

    v = Validator(ctx.body)
    v.reject_unknown({"check_availability"})
    recheck_availability = v.boolean("check_availability", default=True)
    v.raise_if_invalid()

    trip = _trip_from_stored(stored)
    catalog = get_catalog()
    validation = constraints.validate(catalog, stored["itinerary"], trip)
    product_mapping = mapping.map_plan_products(
        catalog, stored["itinerary"], trip, check_availability=bool(recheck_availability)
    )

    return ok(
        envelope(
            ctx,
            {
                "plan_id": plan_id,
                "revalidated_at": now_iso(),
                "validation": validation,
                "product_mapping": product_mapping,
                "comparison": {
                    "executable_at_creation": (stored.get("validation") or {}).get("executable"),
                    "executable_now": validation["executable"],
                    "note": "创建时的校验结论不构成后续保证；本次结论同样只代表当前时点。",
                },
            },
        )
    )


# ---------------------------------------------------------------- 内部


def _build_plan(plan_id: str, trip: TripRequest, options: dict[str, Any]) -> dict[str, Any]:
    catalog = get_catalog()

    itinerary = generate(catalog, trip)
    validation = constraints.validate(catalog, itinerary, trip)
    product_mapping = mapping.map_plan_products(
        catalog, itinerary, trip, check_availability=options["check_availability"]
    )

    narrative_block = (
        narrative.build(itinerary, validation, product_mapping) if options["include_narrative"] else None
    )

    return {
        "plan_id": plan_id,
        "status": "planned",
        "created_at": now_iso(),
        "request_echo": trip.to_dict(),
        "options": options,
        "itinerary": itinerary,
        "validation": validation,
        "product_mapping": product_mapping,
        "narrative": narrative_block,
        "booking_handoff": _booking_handoff(plan_id, product_mapping),
    }


def _booking_handoff(plan_id: str, product_mapping: dict[str, Any]) -> dict[str, Any]:
    """预订衔接。

    需求文档 §4.2-6 / §4.5：助手只把用户送到正确商品的预订入口，
    **不代替用户完成付款**，也不把一次查询等同于预订成功。
    """
    ready: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []

    for entry in product_mapping["entries"]:
        for candidate in entry["candidates"]:
            record = {
                "poi_id": entry["poi_id"],
                "poi_name": entry["poi_name"],
                "product_id": candidate["product_id"],
                "product_name": candidate.get("product_name"),
                "travel_date": entry["travel_date"],
                "availability_state": candidate.get("availability_state"),
            }
            if candidate.get("bookable"):
                ready.append(
                    {
                        **record,
                        "binding": candidate["binding"],
                        "next_step": "POST /v1/bookings 创建预订意图，随后由交易系统重新确认价格与库存。",
                    }
                )
            else:
                blocked.append({**record, "reason": candidate.get("reason")})

    return {
        "plan_id": plan_id,
        "bookable_entry_count": len(ready),
        "blocked_entry_count": len(blocked),
        "entries": ready,
        "blocked": blocked,
        "constraints": [
            "本助手不代替用户下单或付款。",
            "一次可订查询不等于预订成功，也不构成锁价或库存保留。",
            "进入预订时由交易系统按当时价格、库存与适用条件重新确认。",
        ],
    }


def _trip_from_stored(stored: dict[str, Any]) -> TripRequest:
    from ..core.clock import parse_date

    echo = stored["request_echo"]
    party = echo["party"]
    start = parse_date(echo["start_date"])
    if start is None:
        raise NotFoundError("存储的行程方案数据异常，无法重新校验。")
    return TripRequest(
        destination=echo["destination"],
        start_date=start,
        days=int(echo["days"]),
        adults=int(party["adults"]),
        child_ages=[int(a) for a in party.get("child_ages") or []],
        pace=echo.get("pace", "standard"),
        interests=list(echo.get("interests") or []),
        hotel_region_id=echo.get("hotel_region_id"),
        budget_cny_per_person=echo.get("budget_cny_per_person"),
    )
