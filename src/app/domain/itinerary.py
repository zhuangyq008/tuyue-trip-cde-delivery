"""行程生成（区域聚合 + 时间推演）。

需求文档 §4.2-3：「按区域安排景点，计入交通、排队、用餐、休息和合理缓冲；
根据年龄及行动能力调整强度」。

**完全确定性**：不使用随机数，排序键全部显式且唯一（末位以 poi_id 兜底）。
同一份请求任意次生成得到逐字段相同的行程，这是规范 §4.5 幂等的前提。

本模块只负责「排出候选行程」，**不负责断言行程可行** ——
可执行性由 `constraints.py` 在生成后独立校验（需求文档 §4.2-4「执行约束校验」）。
两者刻意分离：生成器可以排出一个违规行程，校验器必须能抓出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..adapters.catalog import Catalog
from ..core.clock import hhmm_to_minutes, minutes_to_hhmm
from . import freshness, weather as weather_mod

ITEM_ATTRACTION = "attraction"
ITEM_TRANSIT = "transit"
ITEM_MEAL = "meal"
ITEM_REST = "rest"
ITEM_WAIT = "wait"

# 一天的时间上限（分钟）。超过即停止当日排点：
# `minutes_to_hhmm` 会把 ≥1440 的值静默截断成 23:59，
# 若不显式拦住，行程里会出现一串 23:59-23:59 的无意义条目。
DAY_END_LIMIT_MINUTES = 24 * 60


@dataclass(frozen=True)
class PaceProfile:
    name: str
    day_start_minutes: int
    max_attractions_per_day: int
    buffer_minutes: int
    max_active_minutes: int


PACE_PROFILES: dict[str, PaceProfile] = {
    "relaxed": PaceProfile("relaxed", hhmm_to_minutes("09:30") or 570, 2, 25, 480),
    "standard": PaceProfile("standard", hhmm_to_minutes("09:00") or 540, 3, 15, 600),
    "packed": PaceProfile("packed", hhmm_to_minutes("08:30") or 510, 4, 10, 690),
}

LUNCH_TARGET_MINUTES = hhmm_to_minutes("12:00") or 720
LUNCH_DURATION_MINUTES = 60
# 低龄儿童午后需要一段休息，否则「排得出来」但「走不下去」。
REST_DURATION_MINUTES = 30
YOUNG_CHILD_AGE_THRESHOLD = 6


@dataclass
class TripRequest:
    destination: str
    start_date: date
    days: int
    adults: int
    child_ages: list[int]
    pace: str = "standard"
    interests: list[str] = field(default_factory=list)
    hotel_region_id: str | None = None
    budget_cny_per_person: float | None = None

    @property
    def children(self) -> int:
        return len(self.child_ages)

    @property
    def total_pax(self) -> int:
        return self.adults + self.children

    @property
    def has_young_child(self) -> bool:
        return any(age < YOUNG_CHILD_AGE_THRESHOLD for age in self.child_ages)

    def trip_dates(self) -> list[date]:
        return [self.start_date + timedelta(days=offset) for offset in range(self.days)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "destination": self.destination,
            "start_date": self.start_date.isoformat(),
            "days": self.days,
            "party": {
                "adults": self.adults,
                "children": self.children,
                "child_ages": sorted(self.child_ages),
            },
            "pace": self.pace,
            "interests": sorted(self.interests),
            "hotel_region_id": self.hotel_region_id,
            "budget_cny_per_person": self.budget_cny_per_person,
        }


# ---------------------------------------------------------------- 打分与选点


def merchant_is_usable(catalog: Catalog, poi: dict[str, Any]) -> tuple[bool, str | None]:
    """POI 的经营商户当前是否可用（数据清单 #2 的硬性约束）。

    清单把该项的用途写成「过滤不可用商户」，所以它必须在**推荐阶段**就生效，
    而不是等到用户点了预订才拦。一个已停业的场馆压根不该出现在行程里。
    """
    merchant = catalog.merchant_for_poi(poi["poi_id"])
    if merchant is None:
        return False, "该景点未关联经营商户，无法确认是否在营。"

    status_block = merchant.get("operating_status") or {}
    status = status_block.get("status")
    age = status_block.get("status_age_seconds")
    verdict = freshness.assess_age(2, age if isinstance(age, int) else None)

    if status != "OPEN":
        return False, f"经营商户 {merchant['name_zh']} 当前状态为 {status}。"
    if verdict.blocks_usage:
        return False, f"经营商户 {merchant['name_zh']} 的在营状态已过期（{age}s > 300s），不可采信。"
    return True, None


def _rating_bonus(catalog: Catalog, poi: dict[str, Any]) -> float:
    """商户评分加权（数据清单 #5，T+1 时效）。

    评分过期则**不加权**而非按旧分加权：过期评分参与排序会让推荐依据失真。
    评分只影响排序，永远不影响可订判定。
    """
    merchant = catalog.merchant_for_poi(poi["poi_id"])
    if merchant is None:
        return 0.0
    rating = merchant.get("rating") or {}
    verdict = freshness.assess_timestamp(5, rating.get("updated_at"))
    if not verdict.is_fresh:
        return 0.0
    overall = rating.get("overall")
    if not isinstance(overall, (int, float)):
        return 0.0
    # 5 分制映射到 0-20 分区间，与亲子/热度项量级可比。
    return round(float(overall) * 4.0, 3)


def score_poi(
    catalog: Catalog,
    poi: dict[str, Any],
    request: TripRequest,
    day_weather: "weather_mod.DayWeather | None" = None,
) -> float:
    """候选景点打分。权重固定，可解释，便于向业务方说明推荐依据。"""
    kid_weight = 0.6 if request.children > 0 else 0.15
    popularity_weight = 1.0 - kid_weight

    score = poi["kid_fit_score"] * kid_weight + poi["popularity_score"] * popularity_weight

    # 商户评分（清单 #5）。
    score += _rating_bonus(catalog, poi)

    # 兴趣标签命中：通过 POI 的 category 与关联文档标签匹配。
    if request.interests:
        hits = sum(1 for tag in request.interests if tag in poi.get("category", ""))
        score += hits * 5

    # 有低龄儿童时，室内场馆更稳（天气与午休都更可控）。
    if request.has_young_child and poi.get("indoor"):
        score += 6

    # 天气（清单 #8）：高降雨概率时室内加权、户外降权。
    # 数据缺失时 prefers_indoor 为 False，即不做任何调整 —— 不因未知而擅自改排。
    if day_weather is not None and day_weather.prefers_indoor:
        score += 12 if poi.get("indoor") else -12

    # 单点时长超过半天的重型项目，在慢节奏下降权。
    if request.pace == "relaxed" and poi["typical_duration_minutes"] > 300:
        score -= 15

    return round(score, 3)


def _poi_is_age_appropriate(poi: dict[str, Any], request: TripRequest) -> bool:
    """选点阶段的粗筛。细粒度年龄校验仍由 constraints.py 负责。"""
    policy = poi.get("age_policy") or {}
    max_age = policy.get("max_age")
    min_age = policy.get("min_age")

    if isinstance(max_age, int):
        # 场馆设了年龄上限（如仅接待儿童）：没有符合年龄的孩子就不选。
        if request.children == 0:
            return False
        if not any(age <= max_age for age in request.child_ages):
            return False
    if isinstance(min_age, int) and request.children > 0:
        if all(age < min_age for age in request.child_ages):
            return False
    return True


def _closed_on(poi: dict[str, Any], day: date) -> bool:
    if day.isoformat() in (poi.get("closed_dates") or []):
        return True
    return day.weekday() in (poi.get("closed_weekdays") or [])


def _rank_regions(catalog: Catalog, request: TripRequest) -> list[tuple[str, float]]:
    """按区域内候选景点的得分聚合排序，实现「区域聚合」减少跨区通勤。"""
    aggregates: dict[str, float] = {}
    for poi in catalog.all_pois():
        if not _poi_is_age_appropriate(poi, request):
            continue
        # 商户不可用的景点不参与区域打分，避免把区域选在一堆停业场馆上。
        if not merchant_is_usable(catalog, poi)[0]:
            continue
        aggregates.setdefault(poi["region_id"], 0.0)
        aggregates[poi["region_id"]] += score_poi(catalog, poi, request)
    # 排序键：得分降序 + region_id 升序（保证唯一确定）。
    return sorted(aggregates.items(), key=lambda kv: (-kv[1], kv[0]))


def _assign_regions(catalog: Catalog, request: TripRequest) -> list[str]:
    """给每一天分配一个主区域。区域数不足时循环复用。"""
    ranked = [region_id for region_id, _ in _rank_regions(catalog, request)]
    if not ranked:
        return []

    # 指定了住宿区域时优先安排该区域，减少首日通勤。
    if request.hotel_region_id and request.hotel_region_id in ranked:
        ranked.remove(request.hotel_region_id)
        ranked.insert(0, request.hotel_region_id)

    return [ranked[index % len(ranked)] for index in range(request.days)]


# ---------------------------------------------------------------- 时间推演


def _build_day(
    catalog: Catalog,
    request: TripRequest,
    day_index: int,
    day: date,
    region_id: str,
    used_poi_ids: set[str],
    profile: PaceProfile,
    day_weather: "weather_mod.DayWeather",
) -> dict[str, Any]:
    """推演一天的时间表。

    刻意**不在此处剔除闭馆景点**：若某天该区域只剩闭馆景点，也照样排进去，
    让 constraints.py 报出 `POI_CLOSED` 并给出备选。
    生成器隐瞒问题，校验器就永远抓不到问题。
    """
    candidates = [
        poi
        for poi in catalog.pois_in_region(region_id)
        if poi["poi_id"] not in used_poi_ids
        and _poi_is_age_appropriate(poi, request)
        # 硬性约束（清单 #2）：商户不在营或状态过期的景点直接不进候选池。
        and merchant_is_usable(catalog, poi)[0]
    ]
    # 优先选当天开放的；全闭馆则退化为按得分选，交给校验器报错。
    open_candidates = [poi for poi in candidates if not _closed_on(poi, day)]
    pool = open_candidates or candidates
    pool.sort(key=lambda poi: (-score_poi(catalog, poi, request, day_weather), poi["poi_id"]))
    selected = pool[: profile.max_attractions_per_day]

    # 同一天内按开门时间升序游览，避免早到闭门。
    selected.sort(key=lambda poi: ((poi["opening_hours"] or {}).get("open", "23:59"), poi["poi_id"]))

    items: list[dict[str, Any]] = []
    cursor = profile.day_start_minutes
    previous_region = request.hotel_region_id or region_id
    lunch_inserted = False
    rest_inserted = False
    truncated = False

    for poi in selected:
        transit_minutes, transit_known = catalog.transit_minutes(previous_region, poi["region_id"])
        items.append(
            {
                "type": ITEM_TRANSIT,
                "start_time": minutes_to_hhmm(cursor),
                "end_time": minutes_to_hhmm(cursor + transit_minutes),
                "duration_minutes": transit_minutes,
                "from_region_id": previous_region,
                "from_region_name": catalog.region_name(previous_region),
                "to_region_id": poi["region_id"],
                "to_region_name": poi["region_name"],
                "transit_duration_known": transit_known,
                "note": "公共交通估算耗时（含换乘步行）" if transit_known else "该区域对缺少交通数据，按保守上限估算，待确认",
            }
        )
        cursor += transit_minutes

        # 早于开门时间抵达时，把时间推到开门，并显式插入等候条目。
        # 不这么做就会排出「09:24 抵达 10:00 才开门的场馆」——
        # 校验器能抓到（ARRIVE_BEFORE_OPENING），但让带小孩的用户在门口干等
        # 36 分钟本身就是不该生成的行程。校验器那条规则保留作纵深防御。
        open_minutes = hhmm_to_minutes((poi.get("opening_hours") or {}).get("open") or "")
        if open_minutes is not None and cursor < open_minutes:
            wait_minutes = open_minutes - cursor
            items.append(
                {
                    "type": ITEM_WAIT,
                    "start_time": minutes_to_hhmm(cursor),
                    "end_time": minutes_to_hhmm(open_minutes),
                    "duration_minutes": wait_minutes,
                    "note": f"{poi['name_zh']} {(poi['opening_hours'] or {}).get('open')} 开门，此前为自由活动时间",
                }
            )
            cursor = open_minutes

        # 午餐：在接近目标时间且尚未安排时插入，避免把用餐挤掉。
        if not lunch_inserted and cursor >= LUNCH_TARGET_MINUTES - 30:
            items.append(_meal_item(cursor))
            cursor += LUNCH_DURATION_MINUTES
            lunch_inserted = True

        duration = int(poi["typical_duration_minutes"])
        if cursor + duration >= DAY_END_LIMIT_MINUTES:
            # 当日时间已排不下这个景点：停止而不是继续累加。
            truncated = True
            break

        items.append(
            {
                "type": ITEM_ATTRACTION,
                "poi_id": poi["poi_id"],
                "poi_name": poi["name_zh"],
                "region_id": poi["region_id"],
                "region_name": poi["region_name"],
                "category": poi["category"],
                "is_indoor": bool(poi.get("indoor")),
                "merchant_id": poi.get("merchant_id"),
                "start_time": minutes_to_hhmm(cursor),
                "end_time": minutes_to_hhmm(cursor + duration),
                "duration_minutes": duration,
                "opening_hours": poi["opening_hours"],
                "requires_reservation": bool((poi.get("reservation") or {}).get("required")),
                "recommend_reason": _reason(poi, request),
                "linked_product_ids": list(poi.get("linked_product_ids") or []),
            }
        )
        cursor += duration + profile.buffer_minutes
        previous_region = poi["region_id"]
        used_poi_ids.add(poi["poi_id"])

        # 低龄儿童午后休息，排在当日第二个景点之后。
        if request.has_young_child and not rest_inserted and len(
            [i for i in items if i["type"] == ITEM_ATTRACTION]
        ) >= 2:
            items.append(_rest_item(cursor))
            cursor += REST_DURATION_MINUTES
            rest_inserted = True

    if not lunch_inserted and items and cursor + LUNCH_DURATION_MINUTES < DAY_END_LIMIT_MINUTES:
        items.append(_meal_item(cursor))
        cursor += LUNCH_DURATION_MINUTES

    attractions = [i for i in items if i["type"] == ITEM_ATTRACTION]
    transit_total = sum(i["duration_minutes"] for i in items if i["type"] == ITEM_TRANSIT)
    active_total = cursor - profile.day_start_minutes

    return {
        "day_index": day_index,
        "date": day.isoformat(),
        "weekday": day.weekday(),
        "weekday_label": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][day.weekday()],
        "primary_region_id": region_id,
        "primary_region_name": catalog.region_name(region_id),
        "items": items,
        "weather": day_weather.to_dict(),
        "weather_advisories": weather_mod.advisories(day_weather, has_young_child=request.has_young_child),
        "totals": {
            "attraction_count": len(attractions),
            "transit_minutes": transit_total,
            "active_minutes": active_total,
            "day_start": minutes_to_hhmm(profile.day_start_minutes),
            "day_end": minutes_to_hhmm(cursor),
            # 当日是否因时间排不下而提前截断（而非静默把时间截到 23:59）。
            "truncated_by_day_limit": truncated,
            "indoor_attraction_count": sum(
                1 for item in attractions if (catalog.poi(item["poi_id"]) or {}).get("indoor")
            ),
        },
    }


def _meal_item(cursor: int) -> dict[str, Any]:
    return {
        "type": ITEM_MEAL,
        "start_time": minutes_to_hhmm(cursor),
        "end_time": minutes_to_hhmm(cursor + LUNCH_DURATION_MINUTES),
        "duration_minutes": LUNCH_DURATION_MINUTES,
        "note": "用餐时段（未绑定具体餐厅商品）",
    }


def _rest_item(cursor: int) -> dict[str, Any]:
    return {
        "type": ITEM_REST,
        "start_time": minutes_to_hhmm(cursor),
        "end_time": minutes_to_hhmm(cursor + REST_DURATION_MINUTES),
        "duration_minutes": REST_DURATION_MINUTES,
        "note": f"低龄儿童（<{YOUNG_CHILD_AGE_THRESHOLD} 岁）午后休息缓冲",
    }


def _reason(poi: dict[str, Any], request: TripRequest) -> str:
    """推荐理由由结构化字段拼装，不由模型生成，避免出现无依据表述。"""
    parts = [f"{poi['region_name']}区域内同行程聚合"]
    if request.children > 0:
        parts.append(f"亲子适配评分 {poi['kid_fit_score']}/100")
    if poi.get("indoor"):
        parts.append("室内场馆，可作雨天备选")
    parts.append(f"建议停留 {poi['typical_duration_minutes']} 分钟")
    return "；".join(parts)


def _merchant_exclusions(catalog: Catalog, request: TripRequest) -> list[dict[str, Any]]:
    """如实列出因商户在营状态而被排除的景点。

    过滤动作必须是**可见的**：悄悄少了几个景点，业务方无法判断
    「是没有可推荐的，还是被规则挡掉了」。
    """
    excluded: list[dict[str, Any]] = []
    for poi in catalog.all_pois():
        usable, reason = merchant_is_usable(catalog, poi)
        if usable:
            continue
        excluded.append(
            {
                "poi_id": poi["poi_id"],
                "poi_name": poi["name_zh"],
                "region_name": poi["region_name"],
                "reason": reason,
                "constraint": "数据清单 #2 商户在营状态（硬性约束，时效 ≤5min）",
            }
        )
    return excluded


# ---------------------------------------------------------------- 入口


def generate(catalog: Catalog, request: TripRequest) -> dict[str, Any]:
    profile = PACE_PROFILES.get(request.pace, PACE_PROFILES["standard"])
    regions = _assign_regions(catalog, request)
    used: set[str] = set()

    days: list[dict[str, Any]] = []
    for index, day in enumerate(request.trip_dates()):
        region_id = regions[index] if index < len(regions) else (regions[0] if regions else "")
        day_weather = weather_mod.for_date(catalog, day.isoformat())
        if not region_id:
            days.append(
                {
                    "day_index": index + 1,
                    "date": day.isoformat(),
                    "weekday": day.weekday(),
                    "weekday_label": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][day.weekday()],
                    "primary_region_id": None,
                    "primary_region_name": None,
                    "items": [],
                    "weather": day_weather.to_dict(),
                    "weather_advisories": [],
                    "totals": {
                        "attraction_count": 0,
                        "transit_minutes": 0,
                        "active_minutes": 0,
                        "day_start": minutes_to_hhmm(profile.day_start_minutes),
                        "day_end": minutes_to_hhmm(profile.day_start_minutes),
                    },
                }
            )
            continue
        days.append(
            _build_day(catalog, request, index + 1, day, region_id, used, profile, day_weather)
        )

    return {
        "pace_profile": {
            "name": profile.name,
            "day_start": minutes_to_hhmm(profile.day_start_minutes),
            "max_attractions_per_day": profile.max_attractions_per_day,
            "buffer_minutes": profile.buffer_minutes,
            "max_active_minutes": profile.max_active_minutes,
        },
        "days": days,
        "summary": {
            "total_days": len(days),
            "total_attractions": sum(d["totals"]["attraction_count"] for d in days),
            "total_transit_minutes": sum(d["totals"]["transit_minutes"] for d in days),
            "regions_covered": sorted({d["primary_region_id"] for d in days if d["primary_region_id"]}),
            "days_with_weather_data": sum(1 for d in days if d["weather"]["available"]),
            "days_preferring_indoor": sum(
                1 for d in days if d["weather"].get("prefers_indoor") is True
            ),
        },
        "excluded_by_merchant_status": _merchant_exclusions(catalog, request),
    }
