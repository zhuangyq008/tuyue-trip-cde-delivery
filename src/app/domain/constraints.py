"""行程可执行性校验。

需求文档 §4.2-4「执行约束校验」与 §4.3 P0「能发现闭馆、赶不上入场、交通过长、
儿童不适用等问题，并解释调整原因」。

三档严重度，对应三种截然不同的对外表达：
  * `blocker`    —— 行程走不通。`executable=false`，**不得**表述为已验证行程。
  * `warning`    —— 走得通但体验有风险，需明示。
  * `unverified` —— 关键事实无法确认。按需求文档 §2.2 标注「待确认」，
                    **不允许**用模型推测补齐（这正是历史事故的假设根因之一）。

校验器独立于生成器：生成器排出的违规行程必须能在这里被抓出来，
否则「行程可执行率」这个指标就没有意义。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ..adapters.catalog import Catalog
from ..core.clock import hhmm_to_minutes, parse_date, today_jst
from ..core.config import get_config
from .itinerary import ITEM_ATTRACTION, ITEM_MEAL, ITEM_TRANSIT, TripRequest

SEVERITY_BLOCKER = "blocker"
SEVERITY_WARNING = "warning"
SEVERITY_UNVERIFIED = "unverified"


def _issue(
    code: str,
    severity: str,
    message: str,
    *,
    day_index: int | None = None,
    poi_id: str | None = None,
    suggestion: str | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    issue: dict[str, Any] = {"code": code, "severity": severity, "message": message}
    if day_index is not None:
        issue["day_index"] = day_index
    if poi_id is not None:
        issue["poi_id"] = poi_id
    if suggestion is not None:
        issue["suggestion"] = suggestion
    if evidence is not None:
        issue["evidence"] = evidence
    return issue


# ---------------------------------------------------------------- 单项校验


def _check_opening(
    catalog: Catalog,
    day_index: int,
    day: date,
    item: dict[str, Any],
) -> list[dict[str, Any]]:
    """开放时间三件事：当日是否闭馆、是否赶得上最后入场、是否会超出闭馆时间。"""
    issues: list[dict[str, Any]] = []
    poi = catalog.poi(item["poi_id"])
    if poi is None:
        return [
            _issue(
                "POI_NOT_FOUND",
                SEVERITY_BLOCKER,
                f"行程引用的景点 {item['poi_id']} 不在目录中，无法校验开放时间。",
                day_index=day_index,
                poi_id=item["poi_id"],
            )
        ]

    if day.isoformat() in (poi.get("closed_dates") or []):
        issues.append(
            _issue(
                "POI_CLOSED_ON_DATE",
                SEVERITY_BLOCKER,
                f"{poi['name_zh']} 在 {day.isoformat()} 为指定闭馆日，当日无法安排。",
                day_index=day_index,
                poi_id=poi["poi_id"],
                suggestion=_closed_alternative(catalog, poi, day),
                evidence={"closed_dates": poi.get("closed_dates")},
            )
        )
    if day.weekday() in (poi.get("closed_weekdays") or []):
        labels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
        issues.append(
            _issue(
                "POI_CLOSED_ON_WEEKDAY",
                SEVERITY_BLOCKER,
                f"{poi['name_zh']} 每{labels[day.weekday()]}固定闭馆，{day.isoformat()} 当日无法安排。",
                day_index=day_index,
                poi_id=poi["poi_id"],
                suggestion=_closed_alternative(catalog, poi, day),
                evidence={"closed_weekdays": poi.get("closed_weekdays")},
            )
        )

    hours = poi.get("opening_hours") or {}
    open_minutes = hhmm_to_minutes(hours.get("open", "")) if hours.get("open") else None
    close_minutes = hhmm_to_minutes(hours.get("close", "")) if hours.get("close") else None
    last_entry = hhmm_to_minutes(hours.get("last_entry", "")) if hours.get("last_entry") else None
    arrive = hhmm_to_minutes(item["start_time"])
    leave = hhmm_to_minutes(item["end_time"])

    if open_minutes is None or close_minutes is None:
        issues.append(
            _issue(
                "OPENING_HOURS_UNVERIFIED",
                SEVERITY_UNVERIFIED,
                f"{poi['name_zh']} 缺少可用的开放时间数据，无法确认当日可入场，标记为待确认。",
                day_index=day_index,
                poi_id=poi["poi_id"],
            )
        )
        return issues

    if arrive is not None and arrive < open_minutes:
        issues.append(
            _issue(
                "ARRIVE_BEFORE_OPENING",
                SEVERITY_WARNING,
                f"{poi['name_zh']} {hours['open']} 开门，行程安排 {item['start_time']} 抵达，需在门口等候 "
                f"{open_minutes - arrive} 分钟。",
                day_index=day_index,
                poi_id=poi["poi_id"],
                suggestion=f"将抵达时间调整到 {hours['open']} 之后。",
            )
        )

    if last_entry is not None and arrive is not None and arrive > last_entry:
        issues.append(
            _issue(
                "LAST_ENTRY_MISSED",
                SEVERITY_BLOCKER,
                f"{poi['name_zh']} 最后入场时间为 {hours['last_entry']}，行程安排 {item['start_time']} 抵达，"
                "已错过入场。",
                day_index=day_index,
                poi_id=poi["poi_id"],
                suggestion="提前当日出发时间，或将该景点移至次日上午。",
                evidence={"last_entry": hours.get("last_entry"), "planned_arrival": item["start_time"]},
            )
        )

    if leave is not None and leave > close_minutes:
        issues.append(
            _issue(
                "EXCEEDS_CLOSING_TIME",
                SEVERITY_BLOCKER,
                f"{poi['name_zh']} {hours['close']} 闭馆，行程安排停留至 {item['end_time']}，超出闭馆时间 "
                f"{leave - close_minutes} 分钟。",
                day_index=day_index,
                poi_id=poi["poi_id"],
                suggestion="缩短停留时长或提前当日行程。",
                evidence={"close": hours.get("close"), "planned_departure": item["end_time"]},
            )
        )

    return issues


def _check_reservation(
    catalog: Catalog,
    day_index: int,
    day: date,
    item: dict[str, Any],
) -> list[dict[str, Any]]:
    """预约要求与提前期。提前期不够是硬阻断，不是提醒。"""
    poi = catalog.poi(item["poi_id"])
    if poi is None:
        return []
    reservation = poi.get("reservation") or {}
    if not reservation.get("required"):
        return []

    lead_days = int(reservation.get("lead_days") or 0)
    available_lead = (day - today_jst()).days
    if available_lead < lead_days:
        return [
            _issue(
                "RESERVATION_LEAD_TIME_INSUFFICIENT",
                SEVERITY_BLOCKER,
                f"{poi['name_zh']} 需提前 {lead_days} 天预约，距出行仅 {available_lead} 天，当前无法完成预约。",
                day_index=day_index,
                poi_id=poi["poi_id"],
                suggestion="更换为无需预约的同区域场馆，或推迟该景点的安排日期。",
                evidence={"lead_days": lead_days, "days_until_visit": available_lead},
            )
        ]

    return [
        _issue(
            "RESERVATION_REQUIRED",
            SEVERITY_WARNING,
            f"{poi['name_zh']} 需提前预约（提前 {lead_days} 天）。{reservation.get('note') or ''}".strip(),
            day_index=day_index,
            poi_id=poi["poi_id"],
            suggestion="在出行前完成预约，预约结果不由本助手代为确认。",
        )
    ]


def _check_age(
    catalog: Catalog,
    day_index: int,
    item: dict[str, Any],
    request: TripRequest,
) -> list[dict[str, Any]]:
    """适龄与身高限制。身高数据不可得时标注待确认，不替用户判断。"""
    poi = catalog.poi(item["poi_id"])
    if poi is None:
        return []
    policy = poi.get("age_policy") or {}
    issues: list[dict[str, Any]] = []

    min_age = policy.get("min_age")
    max_age = policy.get("max_age")

    if isinstance(min_age, int) and min_age > 0:
        too_young = [age for age in request.child_ages if age < min_age]
        if too_young:
            issues.append(
                _issue(
                    "CHILD_BELOW_MIN_AGE",
                    SEVERITY_BLOCKER,
                    f"{poi['name_zh']} 要求 {min_age} 岁以上入场，同行 {sorted(too_young)} 岁儿童不符合。",
                    day_index=day_index,
                    poi_id=poi["poi_id"],
                    suggestion="更换为无年龄下限的同区域场馆。",
                    evidence={"min_age": min_age, "child_ages": sorted(request.child_ages)},
                )
            )

    if isinstance(max_age, int):
        # 设了年龄上限的场馆（如仅接待儿童）：必须有符合年龄的孩子，成人不得单独入场。
        if request.children == 0:
            issues.append(
                _issue(
                    "ADULT_ONLY_PARTY_NOT_ADMITTED",
                    SEVERITY_BLOCKER,
                    f"{poi['name_zh']} 仅接待携带 {max_age} 岁以下儿童的家庭，当前同行无儿童。",
                    day_index=day_index,
                    poi_id=poi["poi_id"],
                    suggestion="移除该场馆或改为面向成人的同区域景点。",
                    evidence={"max_age": max_age},
                )
            )
        elif not any(age <= max_age for age in request.child_ages):
            issues.append(
                _issue(
                    "CHILD_ABOVE_MAX_AGE",
                    SEVERITY_BLOCKER,
                    f"{poi['name_zh']} 面向 {max_age} 岁以下儿童，同行儿童 {sorted(request.child_ages)} 岁均超出。",
                    day_index=day_index,
                    poi_id=poi["poi_id"],
                    suggestion="更换为无年龄上限的同区域场馆。",
                    evidence={"max_age": max_age, "child_ages": sorted(request.child_ages)},
                )
            )

    min_height = policy.get("min_height_cm")
    if isinstance(min_height, int) and request.children > 0:
        issues.append(
            _issue(
                "HEIGHT_LIMIT_UNVERIFIED",
                SEVERITY_UNVERIFIED,
                f"{poi['name_zh']} 存在 {min_height}cm 身高限制，但请求未提供儿童身高，无法判定是否满足，"
                "标记为待确认。",
                day_index=day_index,
                poi_id=poi["poi_id"],
                suggestion="出行前自行核对儿童身高，或在请求中补充身高信息。",
                evidence={"min_height_cm": min_height},
            )
        )

    adult_required_under = policy.get("adult_required_under_age")
    if isinstance(adult_required_under, int) and request.children > 0 and request.adults == 0:
        issues.append(
            _issue(
                "ADULT_ACCOMPANIMENT_REQUIRED",
                SEVERITY_BLOCKER,
                f"{poi['name_zh']} 要求 {adult_required_under} 岁以下儿童须成人陪同，当前同行成人为 0。",
                day_index=day_index,
                poi_id=poi["poi_id"],
                evidence={"adult_required_under_age": adult_required_under},
            )
        )

    return issues


def _check_merchant(catalog: Catalog, day_index: int, item: dict[str, Any]) -> list[dict[str, Any]]:
    """商户在营状态复核（数据清单 #2）。

    生成器已在选点阶段过滤掉不可用商户，这里再查一遍是**纵深防御**：
    行程可能来自更早的存储（`POST /v1/plans/{id}/revalidate`），
    而商户状态是 ≤5min 时效的数据 —— 当时能用，现在未必。
    """
    from .itinerary import merchant_is_usable

    poi = catalog.poi(item["poi_id"])
    if poi is None:
        return []
    usable, reason = merchant_is_usable(catalog, poi)
    if usable:
        return []
    return [
        _issue(
            "MERCHANT_NOT_OPERATIONAL",
            SEVERITY_BLOCKER,
            f"{poi['name_zh']} 的{reason}该景点当前不可推荐。",
            day_index=day_index,
            poi_id=poi["poi_id"],
            suggestion="移除该景点或改为同区域在营场馆；可调用 GET /v1/merchants/{merchant_id} 核对状态。",
            evidence={"merchant_id": poi.get("merchant_id")},
        )
    ]


def _check_weather(day: dict[str, Any], request: TripRequest) -> list[dict[str, Any]]:
    """天气适宜性（数据清单 #8）。

    天气一律只产出 `warning` 或 `unverified`，**绝不产出 blocker**：
    下雨不会让行程「走不通」，把它判成阻断是过度反应；
    但户外项目扎堆在大雨天又必须说清楚，不能装作没看见。
    """
    issues: list[dict[str, Any]] = []
    day_index = day["day_index"]
    weather_block = day.get("weather") or {}

    if not weather_block.get("available"):
        if any(item["type"] == ITEM_ATTRACTION for item in day["items"]):
            issues.append(
                _issue(
                    "WEATHER_DATA_UNAVAILABLE",
                    SEVERITY_UNVERIFIED,
                    f"第 {day_index} 天（{day['date']}）缺少天气数据，户外安排的适宜性待确认。",
                    day_index=day_index,
                )
            )
        return issues

    verdict = weather_block.get("freshness") or {}
    if verdict.get("is_fresh") is False:
        issues.append(
            _issue(
                "WEATHER_DATA_STALE",
                SEVERITY_UNVERIFIED,
                f"第 {day_index} 天的天气数据已超出每日刷新时效，标记为待确认。",
                day_index=day_index,
                evidence={"freshness": verdict},
            )
        )

    if weather_block.get("prefers_indoor"):
        outdoor = [
            item
            for item in day["items"]
            if item["type"] == ITEM_ATTRACTION and not item.get("is_indoor", True)
        ]
        if outdoor:
            issues.append(
                _issue(
                    "OUTDOOR_ON_RAINY_DAY",
                    SEVERITY_WARNING,
                    f"第 {day_index} 天降雨概率 {weather_block.get('precipitation_probability')}%，"
                    f"仍安排了 {len(outdoor)} 个户外项目。",
                    day_index=day_index,
                    suggestion="替换为同区域室内场馆，或准备雨天备选方案。",
                )
            )

    for note in day.get("weather_advisories") or []:
        issues.append(
            _issue("WEATHER_ADVISORY", SEVERITY_WARNING, note, day_index=day_index)
        )

    return issues


def _check_day_load(
    day: dict[str, Any],
    request: TripRequest,
    active_limit: int,
) -> list[dict[str, Any]]:
    """单日强度：单段交通、当日交通总量、当日活动总时长、是否有用餐时段。

    `active_limit` 由调用方按**行程自身的节奏档位**给出，不是全局配置值。
    早先这里直接读 `cfg.max_daily_active_minutes`（固定 600），
    结果用户明确选了 `packed`（设计容许 690 分钟）时，
    落在 600–690 这个本该允许的区间仍会被判超限 ——
    与 API 自己回显的 `pace_profile.max_active_minutes` 自相矛盾。
    """
    cfg = get_config()
    issues: list[dict[str, Any]] = []
    day_index = day["day_index"]

    for item in day["items"]:
        if item["type"] != ITEM_TRANSIT:
            continue
        if not item.get("transit_duration_known", True):
            issues.append(
                _issue(
                    "TRANSIT_DURATION_UNVERIFIED",
                    SEVERITY_UNVERIFIED,
                    f"{item['from_region_name']} → {item['to_region_name']} 缺少交通耗时数据，"
                    f"已按保守上限 {item['duration_minutes']} 分钟估算，标记为待确认。",
                    day_index=day_index,
                )
            )
            continue
        if item["duration_minutes"] > cfg.max_single_transit_minutes:
            issues.append(
                _issue(
                    "SINGLE_TRANSIT_TOO_LONG",
                    SEVERITY_WARNING,
                    f"{item['from_region_name']} → {item['to_region_name']} 单段交通 "
                    f"{item['duration_minutes']} 分钟，超过 {cfg.max_single_transit_minutes} 分钟阈值。",
                    day_index=day_index,
                    suggestion="将该景点与同区域景点合并到同一天。",
                )
            )

    if day["totals"]["transit_minutes"] > cfg.max_daily_transit_minutes:
        issues.append(
            _issue(
                "DAILY_TRANSIT_EXCEEDED",
                SEVERITY_WARNING,
                f"第 {day_index} 天累计交通 {day['totals']['transit_minutes']} 分钟，"
                f"超过 {cfg.max_daily_transit_minutes} 分钟阈值。",
                day_index=day_index,
                suggestion="减少跨区移动或调整景点分配。",
            )
        )

    if day["totals"]["active_minutes"] > active_limit:
        severity = SEVERITY_BLOCKER if request.has_young_child else SEVERITY_WARNING
        issues.append(
            _issue(
                "DAILY_DURATION_EXCEEDED",
                severity,
                f"第 {day_index} 天在外活动 {day['totals']['active_minutes']} 分钟，超过 {active_limit} 分钟上限"
                + ("，同行有低龄儿童，判定为走不通。" if request.has_young_child else "。"),
                day_index=day_index,
                suggestion="减少当日景点数量或改用 relaxed 节奏。",
            )
        )

    has_attraction = any(item["type"] == ITEM_ATTRACTION for item in day["items"])
    has_meal = any(item["type"] == ITEM_MEAL for item in day["items"])
    if has_attraction and not has_meal:
        issues.append(
            _issue(
                "NO_MEAL_SLOT",
                SEVERITY_WARNING,
                f"第 {day_index} 天未安排用餐时段。",
                day_index=day_index,
                suggestion="在行程中插入至少一段用餐时间。",
            )
        )

    if not has_attraction:
        issues.append(
            _issue(
                "EMPTY_DAY",
                SEVERITY_WARNING,
                f"第 {day_index} 天没有可安排的景点（候选已被年龄或闭馆条件排除）。",
                day_index=day_index,
                suggestion="放宽节奏偏好、调整日期或扩大目的地范围。",
            )
        )

    return issues


def _check_content_freshness(catalog: Catalog, day_index: int, item: dict[str, Any]) -> list[dict[str, Any]]:
    """内容时效与冲突。过期与冲突都只标注，不自行裁决哪份数据为准。"""
    cfg = get_config()
    issues: list[dict[str, Any]] = []
    today = today_jst()

    for doc in catalog.docs_for_poi(item["poi_id"]):
        updated = parse_date(str(doc.get("updated_at", ""))[:10])
        if updated is not None and (today - updated).days > cfg.content_stale_days:
            issues.append(
                _issue(
                    "CONTENT_STALE",
                    SEVERITY_UNVERIFIED,
                    f"{item['poi_name']} 的参考资料《{doc['title']}》最后更新于 {doc['updated_at'][:10]}，"
                    f"已超过 {cfg.content_stale_days} 天，内容可能过期，标记为待确认。",
                    day_index=day_index,
                    poi_id=item["poi_id"],
                    evidence={"doc_id": doc["doc_id"], "updated_at": doc["updated_at"]},
                )
            )
        if doc.get("conflicts_with_poi_hours"):
            issues.append(
                _issue(
                    "CONTENT_CONFLICT",
                    SEVERITY_UNVERIFIED,
                    f"{item['poi_name']} 的商品文案与目的地知识库记录的营业时间不一致，需人工核对，"
                    "本次不自行裁决以哪份为准。",
                    day_index=day_index,
                    poi_id=item["poi_id"],
                    evidence={"doc_id": doc["doc_id"]},
                )
            )

    return issues


# ---------------------------------------------------------------- 备选建议


def _closed_alternative(catalog: Catalog, poi: dict[str, Any], day: date) -> str:
    """闭馆备选：同区域、当日开放、优先室内。

    需求文档 §1.2-3 明确认可「闭馆备选」。备选同样经过开放日校验，
    不给出一个自己也没验过的替代项。
    """
    candidates = [
        other
        for other in catalog.pois_in_region(poi["region_id"])
        if other["poi_id"] != poi["poi_id"]
        and day.isoformat() not in (other.get("closed_dates") or [])
        and day.weekday() not in (other.get("closed_weekdays") or [])
    ]
    if not candidates:
        return "该区域当日无其他开放场馆，建议将此景点调整到其他日期。"
    candidates.sort(key=lambda p: (not p.get("indoor"), -p["kid_fit_score"], p["poi_id"]))
    best = candidates[0]
    return f"同区域当日开放的备选：{best['name_zh']}（{'室内' if best.get('indoor') else '户外'}，亲子评分 {best['kid_fit_score']}/100）。"


# ---------------------------------------------------------------- 入口


def validate(catalog: Catalog, plan: dict[str, Any], request: TripRequest) -> dict[str, Any]:
    """校验整份行程，返回可执行性结论。"""
    issues: list[dict[str, Any]] = []

    # 单日活动时长上限取行程自身的节奏档位；缺该字段时（例如历史存量数据）
    # 回落到全局配置。全局值在此是兜底默认，不是硬性天花板。
    profile_block = plan.get("pace_profile") or {}
    raw_limit = profile_block.get("max_active_minutes")
    active_limit = int(raw_limit) if isinstance(raw_limit, int) else get_config().max_daily_active_minutes

    for day in plan["days"]:
        day_date = parse_date(day["date"])
        if day_date is None:
            issues.append(
                _issue("INVALID_DAY_DATE", SEVERITY_BLOCKER, f"行程第 {day['day_index']} 天日期非法：{day['date']}")
            )
            continue

        for item in day["items"]:
            if item["type"] != ITEM_ATTRACTION:
                continue
            issues.extend(_check_merchant(catalog, day["day_index"], item))
            issues.extend(_check_opening(catalog, day["day_index"], day_date, item))
            issues.extend(_check_reservation(catalog, day["day_index"], day_date, item))
            issues.extend(_check_age(catalog, day["day_index"], item, request))
            issues.extend(_check_content_freshness(catalog, day["day_index"], item))

        issues.extend(_check_day_load(day, request, active_limit))
        issues.extend(_check_weather(day, request))

    counts = {
        SEVERITY_BLOCKER: sum(1 for i in issues if i["severity"] == SEVERITY_BLOCKER),
        SEVERITY_WARNING: sum(1 for i in issues if i["severity"] == SEVERITY_WARNING),
        SEVERITY_UNVERIFIED: sum(1 for i in issues if i["severity"] == SEVERITY_UNVERIFIED),
    }
    executable = counts[SEVERITY_BLOCKER] == 0

    return {
        "executable": executable,
        "issue_counts": counts,
        "issues": issues,
        "checked_rules": [
            "商户在营状态（数据清单 #2，硬性约束）",
            "指定日期闭馆",
            "每周固定闭馆",
            "最后入场时间",
            "闭馆时间超出",
            "预约要求与提前期",
            "儿童年龄下限/上限",
            "成人陪同要求",
            "身高限制（数据缺失时标注待确认）",
            "单段交通耗时",
            "当日交通总量",
            "当日活动总时长（按行程节奏档位判定）",
            "用餐时段",
            "资料时效与冲突",
            "天气适宜性与数据时效（数据清单 #8）",
        ],
        "statement": (
            "已通过上述规则校验，未发现阻断项；仍以出行前实际公告为准。"
            if executable
            else "存在阻断项，本行程当前走不通，不得表述为已验证行程。"
        ),
    }
