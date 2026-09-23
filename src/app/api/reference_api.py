"""参考数据接口（对齐《数据清单-v3.md》）。

覆盖清单中尚未被行程主链路直接暴露的数据项：
  #1/#2/#5 商户信息、在营状态、评分   → GET /v1/merchants/{merchant_id}
  #6       目的地信息                 → GET /v1/destinations
  #8       天气预报                   → GET /v1/weather
  #9       季节气候与事件             → GET /v1/destinations/{destination_id}/seasonality
  全部 9 项的覆盖度与时效口径         → GET /v1/data-inventory

设计原则：凡是带时效要求的数据项，接口**不只返回原始字段**，
还要返回本系统对其时效的**判定结论**（是否新鲜、是否因过期而不可用）。
只把原始 updated_at 丢给调用方，等于把「这条数据还能不能用」的判断推给它，
而那正是历史事故里出问题的环节。
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ..adapters.catalog import get_catalog
from ..core.clock import now_iso, today_jst
from ..domain import freshness
from ..domain.weather import RAIN_THRESHOLD, last_refresh_iso
from ..http.errors import NotFoundError, field_error
from ..http.responses import ok
from ..http.validation import Validator
from .common import MAX_BOOKING_LEAD_DAYS, RequestContext, envelope, require_scope

MAX_WEATHER_DAYS = 14


def data_inventory(ctx: RequestContext) -> dict[str, Any]:
    """GET /v1/data-inventory —— 与客户团队逐项对表用。"""
    require_scope(ctx, "read")
    catalog = get_catalog()
    return ok(
        envelope(
            ctx,
            {
                "source_document": "数据清单-v3.md",
                "source_version": "v3.0",
                "dataset_version": catalog.meta.get("dataset_version"),
                "items": freshness.inventory_report(),
                "freshness_tiers": {
                    tier: seconds for tier, seconds in sorted(freshness.MAX_AGE_SECONDS.items(), key=lambda kv: kv[1])
                },
                "enforcement": {
                    "hard_constraint_items": [2, 4],
                    "hard_constraint_behaviour": "数据过期或状态未知 → 对象判定为不可用，不得推荐、不得判为可订。",
                    "soft_item_behaviour": "数据过期 → 标记待确认并对外明示，但不阻断行程生成。",
                },
                "coverage_counts": {
                    "destinations": len(catalog.destinations),
                    "regions": len(catalog.regions),
                    "pois": len(catalog.pois),
                    "merchants": len(catalog.merchants),
                    "products": len(catalog.products),
                    "documents": len(catalog.documents),
                    "weather_days": len(catalog.weather_by_date),
                    "seasonality_months": len(catalog.seasonality),
                },
            },
        )
    )


def list_destinations(ctx: RequestContext) -> dict[str, Any]:
    """GET /v1/destinations —— 清单 #6。"""
    require_scope(ctx, "read")
    catalog = get_catalog()
    return ok(
        envelope(
            ctx,
            {
                "destinations": sorted(catalog.destinations.values(), key=lambda d: d["destination_id"]),
                "freshness": freshness.assess_age(6, 0).to_dict(),
            },
        )
    )


def get_merchant(ctx: RequestContext) -> dict[str, Any]:
    """GET /v1/merchants/{merchant_id} —— 清单 #1/#2/#5。"""
    require_scope(ctx, "read")
    merchant_id = ctx.path_params["merchant_id"]
    catalog = get_catalog()
    merchant = catalog.merchant(merchant_id)
    if merchant is None:
        raise NotFoundError(f"商户不存在：{merchant_id}")

    status_block = merchant.get("operating_status") or {}
    age = status_block.get("status_age_seconds")
    status_verdict = freshness.assess_age(2, age if isinstance(age, int) else None)
    rating_verdict = freshness.assess_timestamp(5, (merchant.get("rating") or {}).get("updated_at"))

    # 可推荐 = 状态为 OPEN 且状态数据在时效内。两个条件缺一不可。
    usable = status_block.get("status") == "OPEN" and not status_verdict.blocks_usage

    return ok(
        envelope(
            ctx,
            {
                "merchant": merchant,
                "status_freshness": status_verdict.to_dict(),
                "rating_freshness": rating_verdict.to_dict(),
                "usable_for_recommendation": usable,
                "decision_note": (
                    "商户在营且状态数据在 ≤5min 时效内，可参与推荐。"
                    if usable
                    else "商户状态非在营或状态数据已过期，按硬性约束排除在推荐之外。"
                ),
                "linked_products": [
                    {
                        "product_id": pid,
                        "product_name": (catalog.product(pid) or {}).get("name_zh"),
                    }
                    for pid in merchant.get("product_ids") or []
                ],
            },
        )
    )


def get_weather(ctx: RequestContext) -> dict[str, Any]:
    """GET /v1/weather?destination_id=&start_date=&days= —— 清单 #8。"""
    require_scope(ctx, "read")
    catalog = get_catalog()
    q = dict(ctx.query)

    payload: dict[str, Any] = {
        "destination_id": q.get("destination_id", "MOCK_DEST_OSAKA"),
        "start_date": q.get("start_date"),
        "days": q.get("days"),
    }
    payload = {k: v for k, v in payload.items() if v is not None}

    v = Validator(payload)
    v.reject_unknown({"destination_id", "start_date", "days"})
    destination_id = v.string("destination_id", max_len=64, default="MOCK_DEST_OSAKA")
    start_date = v.date_field(
        "start_date",
        required=True,
        not_before=today_jst(),
        not_after=today_jst() + timedelta(days=MAX_BOOKING_LEAD_DAYS),
    )

    days = 1
    raw_days = payload.get("days")
    if raw_days is not None:
        if not str(raw_days).isdigit():
            v.errors.append(field_error("days", f"不是合法整数：{raw_days!r}", f"1-{MAX_WEATHER_DAYS}"))
        else:
            days = int(raw_days)
            if not 1 <= days <= MAX_WEATHER_DAYS:
                v.errors.append(field_error("days", f"超出范围：{days}", f"1-{MAX_WEATHER_DAYS}"))

    if start_date is None and not any(e["field"] == "start_date" for e in v.errors):
        v.errors.append(field_error("start_date", "缺少必填查询参数", "YYYY-MM-DD"))
    if destination_id is not None and catalog.destination(destination_id) is None:
        v.errors.append(
            field_error("destination_id", f"目的地不存在：{destination_id}", "|".join(sorted(catalog.destinations)))
        )
    v.raise_if_invalid()

    forecasts: list[dict[str, Any]] = []
    missing: list[str] = []
    for offset in range(days):
        day = (start_date + timedelta(days=offset)).isoformat()
        forecast = catalog.weather(day)
        if forecast is None:
            # 缺数据如实报告为 missing，不用邻近日期或默认晴天填充。
            missing.append(day)
            continue
        forecasts.append(
            {
                **forecast,
                "outdoor_advisable": forecast["precipitation_probability"] < RAIN_THRESHOLD,
                "refreshed_at": last_refresh_iso(),
                "canned_at": forecast.get("updated_at"),
            }
        )

    return ok(
        envelope(
            ctx,
            {
                "destination_id": destination_id,
                "queried_at": now_iso(),
                "forecasts": forecasts,
                "missing_dates": missing,
                "rain_threshold_percent": RAIN_THRESHOLD,
                "freshness": freshness.assess_timestamp(
                    8, last_refresh_iso() if forecasts else None
                ).to_dict(),
                "usage_note": (
                    "天气用于调整室内/户外安排与提示，不作为可订判定依据；"
                    "缺数据的日期已列在 missing_dates，不做推测填充。"
                ),
            },
        )
    )


def get_seasonality(ctx: RequestContext) -> dict[str, Any]:
    """GET /v1/destinations/{destination_id}/seasonality —— 清单 #9。"""
    require_scope(ctx, "read")
    catalog = get_catalog()
    destination_id = ctx.path_params["destination_id"]
    if catalog.destination(destination_id) is None:
        raise NotFoundError(f"目的地不存在：{destination_id}")

    return ok(
        envelope(
            ctx,
            {
                "destination_id": destination_id,
                "seasonality": [catalog.seasonality[month] for month in sorted(catalog.seasonality)],
                "freshness": freshness.assess_age(9, 0).to_dict(),
                "usage_note": "淡旺季与节庆用于出行时间建议，不改变任何可订判定。",
            },
        )
    )
