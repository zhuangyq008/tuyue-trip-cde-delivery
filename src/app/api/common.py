"""API 层公用件：交付边界声明、请求上下文、入参解析。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Mapping

from ..adapters.catalog import Catalog
from ..core.clock import today_jst
from ..domain.availability import PartyRequest
from ..domain.itinerary import PACE_PROFILES, TripRequest
from ..http.errors import field_error
from ..http.validation import Validator

# 目录当前仅覆盖大阪。刻意做成白名单枚举：
# 请求了没有供给的目的地就明确报错，而不是返回一个空行程让调用方误以为「没景点可去」。
SUPPORTED_DESTINATIONS: dict[str, str] = {
    "大阪": "大阪",
    "osaka": "大阪",
    "Osaka": "大阪",
    "OSAKA": "大阪",
}

MAX_TRIP_DAYS = 7
MAX_ADULTS = 9
MAX_CHILDREN = 9
MAX_TOTAL_PAX = 12
MAX_CHILD_AGE = 17
MAX_BOOKING_LEAD_DAYS = 365


# CDE 规范 §1：交付边界声明必须出现在交付物首页。
# 同时内嵌进每个 API 响应，避免有人只看接口输出、没看文档就把原型当成品。
DELIVERY_NOTICE: dict[str, Any] = {
    "maturity": "CDE 验证原型，非 production-ready；上生产前由客户走自身测试评估流程。",
    "exclusivity": "CDE 为非排他的联合探索，不含独家约定。",
    "confidentiality": "客户业务细节严格保密。",
    "commitments": "不承诺未经确认的预算、排期与 SLA。",
    "data": "全部数据为合成 mock 数据，未接入任何客户生产系统，不含真实个人或支付信息。",
    "fact_source": "价格、库存、可订状态一律以供应商接口查询为事实源；大模型不作为动态事实来源。",
}


@dataclass(frozen=True)
class RequestContext:
    request_id: str
    subject: str
    scopes: frozenset[str]
    method: str
    path: str
    path_params: Mapping[str, str]
    query: Mapping[str, str]
    headers: Mapping[str, str]
    body: dict[str, Any]

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


def envelope(ctx: RequestContext, payload: dict[str, Any]) -> dict[str, Any]:
    """统一成功响应外壳。"""
    return {**payload, "request_id": ctx.request_id, "delivery_notice": DELIVERY_NOTICE}


# ---------------------------------------------------------------- 入参解析


def parse_trip_request(body: Mapping[str, Any], catalog: Catalog) -> tuple[TripRequest | None, Any]:
    """解析行程请求。

    返回 `(trip_request, clarification)`：
      * 信息齐备 → `(TripRequest, None)`
      * 必填缺失 → `(None, ClarificationResult)` —— 由调用方返回 200 追问
      * 字段非法 → 抛 `ValidationError` —— 422
    """
    from ..domain import clarify

    # 唯一使用 missing_as_error=False 的入口：这里必填字段缺失要走「需求澄清」返回 200，
    # 而不是 422。字段存在但非法仍然是 422。
    v = Validator(body, missing_as_error=False)
    v.reject_unknown({"destination", "start_date", "days", "party", "preferences", "options"})

    raw_destination = v.string("destination", max_len=64)
    start_date = v.date_field(
        "start_date",
        not_before=today_jst(),
        not_after=today_jst() + timedelta(days=MAX_BOOKING_LEAD_DAYS),
    )
    days = v.integer("days", minimum=1, maximum=MAX_TRIP_DAYS)

    party_v = v.nested("party")
    adults = party_v.integer("adults", minimum=0, maximum=MAX_ADULTS)
    children = party_v.integer("children", minimum=0, maximum=MAX_CHILDREN)
    child_ages = party_v.int_list("child_ages", minimum=0, maximum=MAX_CHILD_AGE, max_items=MAX_CHILDREN)
    party_v.reject_unknown({"adults", "children", "child_ages"})
    v.adopt(party_v)

    pref_v = v.nested("preferences")
    pace = pref_v.string("pace", choices=sorted(PACE_PROFILES), default="standard")
    interests_value = pref_v.string_list("interests", max_items=10)
    budget = pref_v.integer("budget_cny_per_person", minimum=0, maximum=1_000_000)
    hotel_region = pref_v.string("hotel_region_id", max_len=64)
    pref_v.reject_unknown({"pace", "interests", "budget_cny_per_person", "hotel_region_id"})
    v.adopt(pref_v)

    opt_v = v.nested("options")
    check_availability = opt_v.boolean("check_availability", default=True)
    include_narrative = opt_v.boolean("include_narrative", default=True)
    opt_v.reject_unknown({"check_availability", "include_narrative"})
    v.adopt(opt_v)

    # 目的地枚举校验：给出明确的 expected 列表，便于调用方一次改对。
    destination: str | None = None
    if raw_destination is not None:
        destination = SUPPORTED_DESTINATIONS.get(raw_destination)
        if destination is None:
            v.errors.append(
                field_error(
                    "destination",
                    f"当前 mock 目录未覆盖该目的地：{raw_destination}",
                    expected="|".join(sorted({val for val in SUPPORTED_DESTINATIONS.values()})),
                )
            )

    if hotel_region is not None and hotel_region not in catalog.regions:
        v.errors.append(
            field_error(
                "preferences.hotel_region_id",
                f"区域标识不存在：{hotel_region}",
                expected="|".join(sorted(catalog.regions)),
            )
        )

    # 人数一致性与总量校验（跨字段规则，放在单字段校验之后）。
    if adults is not None and children is not None and child_ages is not None:
        if len(child_ages) != children:
            v.errors.append(
                field_error(
                    "party.child_ages",
                    f"儿童年龄数量（{len(child_ages)}）与 children（{children}）不一致",
                    expected=f"length == {children}",
                )
            )
        total = adults + children
        if total < 1:
            v.errors.append(field_error("party", "同行人数总计不能为 0", expected="adults + children >= 1"))
        if total > MAX_TOTAL_PAX:
            v.errors.append(
                field_error("party", f"同行人数总计 {total} 超过上限", expected=f"adults + children <= {MAX_TOTAL_PAX}")
            )

    v.raise_if_invalid()

    clarification = clarify.assess(
        destination=destination,
        start_date=start_date,
        days=days,
        adults=adults,
        children=children,
        child_ages=child_ages,
        supplied_preferences={
            "pace": pace,
            "interests": interests_value or [],
            "budget_cny_per_person": budget,
            "hotel_region_id": hotel_region,
        },
    )
    if clarification.needs_clarification:
        return None, clarification

    if destination is None or start_date is None or days is None or adults is None:
        # 走到这里说明澄清判定与必填集合不一致，属于内部逻辑错误而非用户输入问题。
        raise RuntimeError("澄清判定通过但必填槽位仍为空，槽位定义与校验逻辑不一致")

    trip = TripRequest(
        destination=destination,
        start_date=start_date,
        days=days,
        adults=adults,
        child_ages=sorted(child_ages or []),
        pace=pace or "standard",
        interests=sorted(interests_value or []),
        hotel_region_id=hotel_region,
        budget_cny_per_person=float(budget) if budget is not None else None,
    )
    trip_options = {"check_availability": bool(check_availability), "include_narrative": bool(include_narrative)}
    return trip, trip_options


def parse_party(v: Validator, *, required: bool) -> PartyRequest | None:
    """解析可订查询用的同行人结构。"""
    party_v = v.nested("party")
    adults = party_v.integer("adults", required=required, minimum=0, maximum=MAX_ADULTS)
    children = party_v.integer("children", minimum=0, maximum=MAX_CHILDREN, default=0)
    child_ages = party_v.int_list("child_ages", minimum=0, maximum=MAX_CHILD_AGE, max_items=MAX_CHILDREN)
    party_v.reject_unknown({"adults", "children", "child_ages"})
    v.adopt(party_v)

    if adults is None:
        return None

    ages = sorted(child_ages or [])
    declared_children = children if children is not None else 0
    if child_ages is not None and declared_children != len(ages):
        v.errors.append(
            field_error(
                "party.child_ages",
                f"儿童年龄数量（{len(ages)}）与 children（{declared_children}）不一致",
                expected=f"length == {declared_children}",
            )
        )
    if child_ages is None and declared_children > 0:
        v.errors.append(
            field_error("party.child_ages", "声明了儿童人数就必须提供每位儿童的年龄", expected="array<integer>")
        )

    total = adults + declared_children
    if total < 1:
        v.errors.append(field_error("party", "同行人数总计不能为 0", expected="adults + children >= 1"))
    if total > MAX_TOTAL_PAX:
        v.errors.append(
            field_error("party", f"同行人数总计 {total} 超过上限", expected=f"adults + children <= {MAX_TOTAL_PAX}")
        )

    return PartyRequest(adults=adults, children=declared_children, child_ages=ages)


def require_scope(ctx: RequestContext, scope: str) -> None:
    from ..http.errors import ForbiddenError

    if scope and scope not in ctx.scopes:
        raise ForbiddenError(f"当前令牌缺少 `{scope}` 权限范围。")
