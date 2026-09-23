"""数据时效分级（对齐《数据清单-v3.md》的「时效性」列）。

客户清单为每一项数据都标了时效要求。时效不是注释，而是**判定规则**：
一条超出时效要求的数据不能当作当前事实使用 —— 这正是需求文档 §2.2 里
「生成内容、知识库或缓存中的价格库存已经过期」这条根因假设的直接防护。

分级定义（tier → 最大可接受数据年龄）：

| tier            | 最大年龄 | 清单对应项                                |
|-----------------|----------|-------------------------------------------|
| realtime        | 0（每次查）| #4 库存与可售日期                         |
| near_realtime   | 5 分钟   | #2 商户在营状态                           |
| daily           | 36 小时  | #8 天气预报                               |
| t_plus_one      | 48 小时  | #5 商户评分                               |
| weekly          | 10 天    | #6 目的地信息、#7 POI 信息                |
| monthly         | 45 天    | #9 季节气候与事件                         |
| low_frequency   | 180 天   | #1 商户信息、#3 产品信息                  |

超时的处理方式按数据项的**约束等级**分流：
  * `hard_constraint` 的数据项超时 → 该对象**不可用**（不得推荐、不得判为可订）。
  * 其他数据项超时 → 标记 `stale`，对外明示「待确认」，但不阻断行程。

这个分流是刻意的：清单把 #2 和 #4 标成硬性约束，所以它们超时必须阻断；
评分或天气过期只影响推荐质量，阻断行程反而是过度反应。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..core.clock import now

TIER_REALTIME = "realtime"
TIER_NEAR_REALTIME = "near_realtime"
TIER_DAILY = "daily"
TIER_T_PLUS_ONE = "t_plus_one"
TIER_WEEKLY = "weekly"
TIER_MONTHLY = "monthly"
TIER_LOW_FREQUENCY = "low_frequency"

MAX_AGE_SECONDS: dict[str, int] = {
    TIER_REALTIME: 0,
    TIER_NEAR_REALTIME: 300,
    TIER_DAILY: 36 * 3600,
    TIER_T_PLUS_ONE: 48 * 3600,
    TIER_WEEKLY: 10 * 24 * 3600,
    TIER_MONTHLY: 45 * 24 * 3600,
    TIER_LOW_FREQUENCY: 180 * 24 * 3600,
}


@dataclass(frozen=True)
class DataItem:
    """清单中的一个数据项。"""

    number: int
    name: str
    purpose: str
    tier: str
    source: str
    hard_constraint: bool
    implementation: str

    @property
    def max_age_seconds(self) -> int:
        return MAX_AGE_SECONDS[self.tier]


# 与《数据清单-v3.md》逐项对齐，编号一致，便于与客户团队对表。
DATA_INVENTORY: tuple[DataItem, ...] = (
    DataItem(
        1,
        "商户信息（名称/类别/地址/经纬度/营业时间）",
        "商户展示、位置筛选",
        TIER_LOW_FREQUENCY,
        "供应链系统",
        False,
        "merchants.json + GET /v1/merchants/{merchant_id}",
    ),
    DataItem(
        2,
        "商户在营状态",
        "硬性约束：过滤不可用商户",
        TIER_NEAR_REALTIME,
        "供应链系统",
        True,
        "availability.check_merchant_status()，非 OPEN 或状态过期一律 NOT_ELIGIBLE",
    ),
    DataItem(
        3,
        "产品信息（名称/类型/价格/时长/取消政策）",
        "产品展示、行程规划",
        TIER_LOW_FREQUENCY,
        "供应链系统",
        False,
        "products.json + GET /v1/products/{product_id}；price_hint 明确标注为参考价",
    ),
    DataItem(
        4,
        "库存与可售日期",
        "硬性约束：确认可售",
        TIER_REALTIME,
        "库存系统",
        True,
        "supplier_mock.query_availability()，每次实查；快照带 TTL 且读侧判过期",
    ),
    DataItem(
        5,
        "商户评分（综合分/评价数/等级）",
        "推荐排序、筛选",
        TIER_T_PLUS_ONE,
        "质量运营团队",
        False,
        "itinerary.score_poi() 中作为排序因子，不作为可订依据",
    ),
    DataItem(
        6,
        "目的地信息（名称/国家/经纬度/货币）",
        "目的地推荐、基础展示",
        TIER_WEEKLY,
        "内容团队",
        False,
        "destinations.json + GET /v1/destinations",
    ),
    DataItem(
        7,
        "POI 信息（名称/类别/坐标/开放时间/游览时长）",
        "行程规划、景点推荐",
        TIER_WEEKLY,
        "内容团队",
        False,
        "pois.json；开放时间由 constraints.py 做闭馆/最后入场校验",
    ),
    DataItem(
        8,
        "天气预报（温度/天气状况/降雨概率）",
        "行程建议、穿搭推荐",
        TIER_DAILY,
        "第三方 API",
        False,
        "weather.json；高降雨概率时室内 POI 加权，并在校验中给出提示",
    ),
    DataItem(
        9,
        "季节气候与事件（最佳月份/节日/淡旺季）",
        "出行时间推荐",
        TIER_MONTHLY,
        "内容团队",
        False,
        "seasonality.json + GET /v1/destinations/{destination_id}/seasonality",
    ),
)

BY_NUMBER: dict[int, DataItem] = {item.number: item for item in DATA_INVENTORY}


# ---------------------------------------------------------------- 判定


@dataclass(frozen=True)
class FreshnessVerdict:
    tier: str
    age_seconds: int | None
    max_age_seconds: int
    is_fresh: bool
    blocks_usage: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "age_seconds": self.age_seconds,
            "max_age_seconds": self.max_age_seconds,
            "is_fresh": self.is_fresh,
            "blocks_usage": self.blocks_usage,
            "note": self.note,
        }


def assess_age(item_number: int, age_seconds: int | None) -> FreshnessVerdict:
    """按数据项编号与数据年龄判定是否可采信。"""
    item = BY_NUMBER[item_number]
    limit = item.max_age_seconds

    if age_seconds is None:
        return FreshnessVerdict(
            tier=item.tier,
            age_seconds=None,
            max_age_seconds=limit,
            is_fresh=False,
            # 年龄未知时，硬性约束项按不可用处理：「不知道多旧」不等于「还新」。
            blocks_usage=item.hard_constraint,
            note=f"「{item.name}」缺少可解析的更新时间，时效未知，标记为待确认。",
        )

    if age_seconds <= limit:
        return FreshnessVerdict(
            tier=item.tier,
            age_seconds=age_seconds,
            max_age_seconds=limit,
            is_fresh=True,
            blocks_usage=False,
            note=f"「{item.name}」数据年龄 {age_seconds}s，满足 {item.tier} 时效要求（≤{limit}s）。",
        )

    return FreshnessVerdict(
        tier=item.tier,
        age_seconds=age_seconds,
        max_age_seconds=limit,
        is_fresh=False,
        blocks_usage=item.hard_constraint,
        note=(
            f"「{item.name}」数据年龄 {age_seconds}s 已超出 {item.tier} 时效要求（≤{limit}s）"
            + ("，按硬性约束判定为不可用。" if item.hard_constraint else "，标记为待确认但不阻断行程。")
        ),
    )


def assess_timestamp(item_number: int, updated_at: str | None) -> FreshnessVerdict:
    """按 ISO-8601 更新时间判定时效。"""
    if not updated_at:
        return assess_age(item_number, None)
    try:
        moment = datetime.fromisoformat(str(updated_at).replace("Z", "+00:00"))
    except ValueError:
        return assess_age(item_number, None)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return assess_age(item_number, max(0, int((now() - moment).total_seconds())))


def inventory_report() -> list[dict[str, Any]]:
    """对外输出覆盖度报告，供与客户团队逐项对表。"""
    return [
        {
            "number": item.number,
            "name": item.name,
            "purpose": item.purpose,
            "freshness_tier": item.tier,
            "max_age_seconds": item.max_age_seconds,
            "source": item.source,
            "hard_constraint": item.hard_constraint,
            "implementation": item.implementation,
        }
        for item in DATA_INVENTORY
    ]
