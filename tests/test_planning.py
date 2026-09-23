"""行程生成、约束校验、商品映射与叙述层测试。"""

from __future__ import annotations

from datetime import date

import pytest

from app.adapters.catalog import get_catalog
from app.domain import constraints, mapping, narrative
from app.domain.itinerary import TripRequest, generate, merchant_is_usable
from conftest import call


@pytest.fixture(autouse=True)
def _clock(frozen_clock: None) -> None:
    """全组用例使用冻结时钟（2026-11-18）。"""


def family_request(**overrides: object) -> TripRequest:
    defaults: dict = {
        "destination": "大阪",
        "start_date": date(2026, 11, 20),
        "days": 2,
        "adults": 2,
        "child_ages": [5],
        "pace": "standard",
    }
    defaults.update(overrides)
    return TripRequest(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------- 生成


def test_generate_is_deterministic() -> None:
    """同一请求两次生成必须逐字段一致 —— 幂等的前提。"""
    catalog = get_catalog()
    first = generate(catalog, family_request())
    second = generate(catalog, family_request())
    assert first == second


def test_generate_clusters_by_region() -> None:
    """同一天的景点应集中在一个区域内，减少跨区通勤。"""
    plan = generate(get_catalog(), family_request())
    for day in plan["days"]:
        regions = {
            item["region_id"] for item in day["items"] if item["type"] == "attraction"
        }
        assert len(regions) <= 1, day["day_index"]


def test_generate_inserts_meal_and_rest_for_young_child() -> None:
    plan = generate(get_catalog(), family_request(child_ages=[4]))
    day_one = plan["days"][0]
    types = [item["type"] for item in day_one["items"]]
    assert "meal" in types
    assert "rest" in types, "同行低龄儿童应插入午后休息缓冲"


def test_relaxed_pace_limits_attractions() -> None:
    plan = generate(get_catalog(), family_request(pace="relaxed"))
    for day in plan["days"]:
        assert day["totals"]["attraction_count"] <= 2


def test_merchant_exclusions_are_visible() -> None:
    """过滤动作必须可见：不能悄悄少几个景点让业务方无从判断。"""
    plan = generate(get_catalog(), family_request())
    excluded = plan["excluded_by_merchant_status"]
    excluded_ids = {entry["poi_id"] for entry in excluded}

    # 暂停营业、状态未知、状态过期三个场馆都应被排除且列出原因。
    assert "MOCK_POI_FERRIS_WHEEL" in excluded_ids
    assert "MOCK_POI_TRAIN_MUSEUM" in excluded_ids
    assert "MOCK_POI_NATURE_PARK" in excluded_ids
    assert all(entry["reason"] for entry in excluded)
    assert all("数据清单 #2" in entry["constraint"] for entry in excluded)


def test_excluded_pois_never_appear_in_itinerary() -> None:
    plan = generate(get_catalog(), family_request(days=5))
    scheduled = {
        item["poi_id"]
        for day in plan["days"]
        for item in day["items"]
        if item["type"] == "attraction"
    }
    excluded = {entry["poi_id"] for entry in plan["excluded_by_merchant_status"]}
    assert scheduled.isdisjoint(excluded)


def test_merchant_is_usable_reports_reason() -> None:
    catalog = get_catalog()
    usable, reason = merchant_is_usable(catalog, catalog.poi("MOCK_POI_FERRIS_WHEEL"))
    assert usable is False
    assert "SUSPENDED" in (reason or "")

    usable, reason = merchant_is_usable(catalog, catalog.poi("MOCK_POI_AQUARIUM"))
    assert usable is True and reason is None


# ---------------------------------------------------------------- 天气


def test_each_day_carries_weather_block() -> None:
    plan = generate(get_catalog(), family_request())
    for day in plan["days"]:
        assert "weather" in day
        assert day["weather"]["date"] == day["date"]


def test_missing_weather_is_reported_not_guessed() -> None:
    """超出预报范围的日期不得被推测填充。"""
    plan = generate(get_catalog(), family_request(start_date=date(2027, 8, 1)))
    assert plan["days"][0]["weather"]["available"] is False
    assert "未做推测填充" in plan["days"][0]["weather"]["note"]


def test_weather_never_produces_blocker() -> None:
    """天气只影响提示与排布，不能让行程判为走不通。"""
    catalog = get_catalog()
    request = family_request()
    plan = generate(catalog, request)
    result = constraints.validate(catalog, plan, request)
    weather_codes = {
        "OUTDOOR_ON_RAINY_DAY",
        "WEATHER_ADVISORY",
        "WEATHER_DATA_STALE",
        "WEATHER_DATA_UNAVAILABLE",
    }
    for issue in result["issues"]:
        if issue["code"] in weather_codes:
            assert issue["severity"] != "blocker", issue


# ---------------------------------------------------------------- 约束校验


def test_closed_date_is_blocker_with_alternative() -> None:
    """海洋馆 2026-11-25 闭馆：应报阻断并给出同区域当日开放的备选。"""
    catalog = get_catalog()
    request = family_request(start_date=date(2026, 11, 25), days=1, hotel_region_id="MOCK_REG_MINATO")
    plan = generate(catalog, request)
    # 生成器不隐藏问题：即使当日闭馆也会排进去，由校验器抓出来。
    plan["days"][0]["items"] = [
        {
            "type": "attraction",
            "poi_id": "MOCK_POI_AQUARIUM",
            "poi_name": "MOCK_大阪湾海洋馆",
            "region_id": "MOCK_REG_MINATO",
            "region_name": "MOCK_港区",
            "category": "aquarium",
            "is_indoor": True,
            "start_time": "10:00",
            "end_time": "12:30",
            "duration_minutes": 150,
            "opening_hours": catalog.poi("MOCK_POI_AQUARIUM")["opening_hours"],
            "requires_reservation": False,
            "recommend_reason": "test",
            "linked_product_ids": [],
        }
    ]
    result = constraints.validate(catalog, plan, request)
    closed = [i for i in result["issues"] if i["code"] == "POI_CLOSED_ON_DATE"]
    assert closed, "闭馆日必须被识别"
    assert closed[0]["severity"] == "blocker"
    assert "备选" in closed[0]["suggestion"]
    assert result["executable"] is False


def test_closed_weekday_is_detected() -> None:
    """儿童科学馆每周一闭馆；2026-11-23 是周一。"""
    catalog = get_catalog()
    request = family_request(start_date=date(2026, 11, 23), days=1)
    plan = generate(catalog, request)
    plan["days"][0]["items"] = [_attraction_item(catalog, "MOCK_POI_KIDS_SCIENCE", "10:00", "13:00")]
    result = constraints.validate(catalog, plan, request)
    assert any(i["code"] == "POI_CLOSED_ON_WEEKDAY" for i in result["issues"])
    assert result["executable"] is False


def test_last_entry_missed_is_blocker() -> None:
    catalog = get_catalog()
    request = family_request(start_date=date(2026, 11, 20), days=1)
    plan = generate(catalog, request)
    # 城址公园最后入场 16:30，安排 17:00 抵达。
    plan["days"][0]["items"] = [_attraction_item(catalog, "MOCK_POI_CASTLE_PARK", "17:00", "19:00")]
    result = constraints.validate(catalog, plan, request)
    issues = {i["code"] for i in result["issues"]}
    assert "LAST_ENTRY_MISSED" in issues
    assert "EXCEEDS_CLOSING_TIME" in issues
    assert result["executable"] is False


def test_adult_only_party_blocked_from_kids_venue() -> None:
    """积木中心仅接待携带 2-12 岁儿童的家庭，成人团不得入场。"""
    catalog = get_catalog()
    request = family_request(child_ages=[], days=1)
    plan = generate(catalog, request)
    plan["days"][0]["items"] = [_attraction_item(catalog, "MOCK_POI_BRICK_CENTER", "10:00", "12:30")]
    result = constraints.validate(catalog, plan, request)
    assert any(i["code"] == "ADULT_ONLY_PARTY_NOT_ADMITTED" for i in result["issues"])


def test_child_below_min_age_blocked() -> None:
    """积木中心 min_age=2，1 岁幼儿不符合。"""
    catalog = get_catalog()
    request = family_request(child_ages=[1], days=1)
    plan = generate(catalog, request)
    plan["days"][0]["items"] = [_attraction_item(catalog, "MOCK_POI_BRICK_CENTER", "10:00", "12:30")]
    result = constraints.validate(catalog, plan, request)
    assert any(i["code"] == "CHILD_BELOW_MIN_AGE" for i in result["issues"])


def test_height_limit_without_data_is_unverified_not_guessed() -> None:
    """有身高限制但未提供身高：标注待确认，不替用户判断。"""
    catalog = get_catalog()
    request = family_request(child_ages=[5], days=1)
    plan = generate(catalog, request)
    plan["days"][0]["items"] = [_attraction_item(catalog, "MOCK_POI_BRICK_CENTER", "10:00", "12:30")]
    result = constraints.validate(catalog, plan, request)
    height = [i for i in result["issues"] if i["code"] == "HEIGHT_LIMIT_UNVERIFIED"]
    assert height and height[0]["severity"] == "unverified"


def test_stale_content_is_flagged_unverified() -> None:
    """城址公园的参考资料已过期 400 天，必须明确提示。"""
    catalog = get_catalog()
    request = family_request(start_date=date(2026, 11, 20), days=1)
    plan = generate(catalog, request)
    plan["days"][0]["items"] = [_attraction_item(catalog, "MOCK_POI_CASTLE_PARK", "10:00", "12:00")]
    result = constraints.validate(catalog, plan, request)
    stale = [i for i in result["issues"] if i["code"] == "CONTENT_STALE"]
    assert stale and stale[0]["severity"] == "unverified"


def test_merchant_check_is_defense_in_depth() -> None:
    """已存行程重新校验时，商户状态变化必须被抓出（≤5min 时效数据）。"""
    catalog = get_catalog()
    request = family_request(days=1)
    plan = generate(catalog, request)
    plan["days"][0]["items"] = [_attraction_item(catalog, "MOCK_POI_FERRIS_WHEEL", "10:00", "10:40")]
    result = constraints.validate(catalog, plan, request)
    issue = next(i for i in result["issues"] if i["code"] == "MERCHANT_NOT_OPERATIONAL")
    assert issue["severity"] == "blocker"
    assert result["executable"] is False


def test_executable_plan_states_its_limits() -> None:
    """通过校验也不得表述为「保证可行」。"""
    catalog = get_catalog()
    request = family_request()
    plan = generate(catalog, request)
    result = constraints.validate(catalog, plan, request)
    if result["executable"]:
        assert "以出行前实际公告为准" in result["statement"]
    else:
        assert "不得表述为已验证行程" in result["statement"]


# ---------------------------------------------------------------- 商品映射


def test_mapping_uses_explicit_links_not_similarity() -> None:
    catalog = get_catalog()
    request = family_request()
    plan = generate(catalog, request)
    result = mapping.map_plan_products(catalog, plan, request, check_availability=True)
    assert result["mapping_method"] == "explicit_catalog_link"
    assert "不使用文本相似度" in result["mapping_note"]
    for entry in result["entries"]:
        for candidate in entry["candidates"]:
            assert candidate["mapping_method"] == "explicit_catalog_link"


def test_mapping_binds_product_date_and_pax() -> None:
    """推荐必须绑定商品标识 + 日期 + 人数（需求文档 §2.2）。"""
    catalog = get_catalog()
    request = family_request()
    plan = generate(catalog, request)
    result = mapping.map_plan_products(catalog, plan, request, check_availability=True)
    for entry in result["entries"]:
        for candidate in entry["candidates"]:
            if candidate.get("catalog_integrity") != "OK":
                continue
            binding = candidate["binding"]
            assert binding["product_id"] == candidate["product_id"]
            assert binding["travel_date"] == entry["travel_date"]
            assert binding["adults"] == request.adults
            assert binding["child_ages"] == sorted(request.child_ages)


def test_unchecked_availability_is_never_bookable() -> None:
    """未查询就是未知，不得默认为可订。"""
    catalog = get_catalog()
    request = family_request()
    plan = generate(catalog, request)
    result = mapping.map_plan_products(catalog, plan, request, check_availability=False)
    for entry in result["entries"]:
        assert entry["has_bookable_product"] is False
        for candidate in entry["candidates"]:
            assert candidate["bookable"] is False
            assert candidate["availability_state"] == "UNKNOWN"


def test_poi_without_product_is_reported_honestly() -> None:
    """没有绑定商品的景点如实说明，不用相近商品填坑。"""
    catalog = get_catalog()
    request = family_request(days=3, hotel_region_id="MOCK_REG_CHUO")
    plan = generate(catalog, request)
    result = mapping.map_plan_products(catalog, plan, request, check_availability=True)
    no_product = [e for e in result["entries"] if e["candidate_count"] == 0]
    for entry in no_product:
        assert "不提供预订入口" in entry["note"]


# ---------------------------------------------------------------- 叙述层（大模型边界）


def test_narrative_defaults_to_deterministic_template() -> None:
    catalog = get_catalog()
    request = family_request()
    plan = generate(catalog, request)
    validation = constraints.validate(catalog, plan, request)
    result = narrative.build(plan, validation, None)
    assert result["generated_by"] == "deterministic_template"
    assert "未经大模型改写" in result["fact_source_note"]


def test_narrative_guard_rejects_fabricated_numbers() -> None:
    """守卫必须拦下白名单外的数字 —— 模型编造价格是明令禁止的。"""
    allowed = {"150", "2"}
    assert narrative._guard("停留 150 分钟", allowed) is None
    violation = narrative._guard("门票仅需 888 元", allowed)
    assert violation is not None and "888" in violation


def test_narrative_guard_rejects_overreaching_promises() -> None:
    for phrase in ("已锁价", "保证可订", "已为您下单"):
        violation = narrative._guard(f"本商品{phrase}", {"1"})
        assert violation is not None, phrase


def test_narrative_guard_allows_small_ordinals() -> None:
    assert narrative._guard("第 3 天安排", set()) is None


def test_narrative_includes_disclaimer() -> None:
    catalog = get_catalog()
    request = family_request()
    plan = generate(catalog, request)
    validation = constraints.validate(catalog, plan, request)
    result = narrative.build(plan, validation, None)
    assert "不构成锁价或库存保留" in result["text"]


# ---------------------------------------------------------------- 端到端


def test_full_plan_endpoint_returns_all_layers() -> None:
    status, body = call(
        "POST",
        "/v1/plans",
        body={
            "destination": "大阪",
            "start_date": "2026-11-20",
            "days": 2,
            "party": {"adults": 2, "children": 1, "child_ages": [5]},
            "preferences": {"pace": "standard", "hotel_region_id": "MOCK_REG_CHUO"},
            "options": {"check_availability": True, "include_narrative": True},
        },
    )
    assert status == 201
    assert body["status"] == "planned"
    for key in ("itinerary", "validation", "product_mapping", "narrative", "booking_handoff"):
        assert key in body, key
    assert body["booking_handoff"]["constraints"]
    assert body["plan_id"].startswith("MOCK-PLAN-")


def test_clarification_flow_returns_200_with_questions() -> None:
    status, body = call("POST", "/v1/plans", body={"destination": "大阪"})
    assert status == 200
    assert body["status"] == "needs_clarification"
    assert set(body["missing_fields"]) >= {"start_date", "days", "party.adults"}
    assert all(q["question"] and q["why"] for q in body["questions"])
    assert "不收集姓名" in body["privacy_note"]


def test_clarification_asks_for_child_ages_when_children_declared() -> None:
    status, body = call(
        "POST",
        "/v1/plans",
        body={
            "destination": "大阪",
            "start_date": "2026-11-20",
            "days": 2,
            "party": {"adults": 2, "children": 1},
        },
    )
    assert status == 200
    assert "party.child_ages" in body["missing_fields"]


def test_revalidate_compares_against_creation() -> None:
    _, created = call(
        "POST",
        "/v1/plans",
        body={
            "destination": "大阪",
            "start_date": "2026-11-20",
            "days": 2,
            "party": {"adults": 2, "children": 1, "child_ages": [5]},
        },
    )
    status, body = call("POST", f"/v1/plans/{created['plan_id']}/revalidate")
    assert status == 200
    assert body["comparison"]["executable_at_creation"] == created["validation"]["executable"]
    assert "不构成后续保证" in body["comparison"]["note"]


# ---------------------------------------------------------------- 辅助


def _attraction_item(catalog, poi_id: str, start: str, end: str) -> dict:
    poi = catalog.poi(poi_id)
    return {
        "type": "attraction",
        "poi_id": poi_id,
        "poi_name": poi["name_zh"],
        "region_id": poi["region_id"],
        "region_name": poi["region_name"],
        "category": poi["category"],
        "is_indoor": bool(poi.get("indoor")),
        "merchant_id": poi.get("merchant_id"),
        "start_time": start,
        "end_time": end,
        "duration_minutes": 120,
        "opening_hours": poi["opening_hours"],
        "requires_reservation": bool((poi.get("reservation") or {}).get("required")),
        "recommend_reason": "test",
        "linked_product_ids": list(poi.get("linked_product_ids") or []),
    }


# ---------------------------------------------------------------- 评审修复回归


def test_packed_pace_allows_its_own_active_limit() -> None:
    """节奏档位上限必须被真正使用。

    早先 `_check_day_load` 固定读全局配置（600 分钟），
    导致用户明确选 packed（设计容许 690）时，落在 600-690 这个
    本该允许的区间仍被判超限 —— 与 API 自己回显的 pace_profile 自相矛盾。
    """
    catalog = get_catalog()
    request = family_request(pace="packed")
    plan = generate(catalog, request)

    # 构造一个落在 600-690 区间的当日活动时长。
    plan["days"][0]["totals"]["active_minutes"] = 650
    result = constraints.validate(catalog, plan, request)
    exceeded = [i for i in result["issues"] if i["code"] == "DAILY_DURATION_EXCEEDED"]
    assert not exceeded, "packed 档位下 650 分钟应在允许范围内"

    # 超过 packed 自身上限才应报出。
    plan["days"][0]["totals"]["active_minutes"] = 700
    result = constraints.validate(catalog, plan, request)
    assert any(i["code"] == "DAILY_DURATION_EXCEEDED" for i in result["issues"])


def test_relaxed_pace_uses_stricter_limit() -> None:
    """relaxed 档位上限 480，比全局配置的 600 更严，同样必须生效。"""
    catalog = get_catalog()
    request = family_request(pace="relaxed")
    plan = generate(catalog, request)
    plan["days"][0]["totals"]["active_minutes"] = 520
    result = constraints.validate(catalog, plan, request)
    assert any(i["code"] == "DAILY_DURATION_EXCEEDED" for i in result["issues"])


def test_validate_falls_back_when_pace_profile_missing() -> None:
    """历史存量行程没有 pace_profile 字段时回落到全局配置，不应崩溃。"""
    catalog = get_catalog()
    request = family_request()
    plan = generate(catalog, request)
    plan.pop("pace_profile")
    plan["days"][0]["totals"]["active_minutes"] = 700
    result = constraints.validate(catalog, plan, request)
    assert any(i["code"] == "DAILY_DURATION_EXCEEDED" for i in result["issues"])


def test_narrative_guard_no_longer_exempts_all_small_integers() -> None:
    """守卫曾对所有 ≤31 的整数无条件放行，等于给幻觉开门。

    折扣、排队分钟、人数、小额价格大多落在该区间，
    「排队 15 分钟」这类编造必须被拦下。
    """
    allowed = {"150", "2"}
    assert narrative._guard("预计排队 15 分钟", allowed) is not None
    assert narrative._guard("该商品享 8 折", allowed) is not None
    assert narrative._guard("同行 4 人", allowed) is not None


def test_narrative_guard_still_allows_genuine_ordinals() -> None:
    """序号语境内的数字仍应豁免，否则模板文案自己都过不了。"""
    assert narrative._guard("第 3 天安排如下", set()) is None
    assert narrative._guard("第2项、第 10 步", set()) is None


def test_narrative_template_passes_its_own_guard() -> None:
    """模板文案必须能通过守卫 —— 否则开启模型时会永远回落。"""
    catalog = get_catalog()
    request = family_request()
    plan = generate(catalog, request)
    validation = constraints.validate(catalog, plan, request)
    product_mapping = mapping.map_plan_products(catalog, plan, request, check_availability=True)
    text = narrative._render_template(plan, validation, product_mapping)
    allowed = narrative._collect_allowed_numbers(plan, validation, product_mapping)
    assert narrative._guard(text, allowed) is None


def test_generator_waits_for_opening_instead_of_arriving_early() -> None:
    """不应排出「到了但没开门」的行程：生成器把时间推到开门。"""
    catalog = get_catalog()
    request = family_request(days=2, hotel_region_id="MOCK_REG_CHUO")
    plan = generate(catalog, request)

    for day in plan["days"]:
        for item in day["items"]:
            if item["type"] != "attraction":
                continue
            poi = catalog.poi(item["poi_id"])
            open_time = (poi.get("opening_hours") or {}).get("open")
            if open_time:
                assert item["start_time"] >= open_time, f"{item['poi_name']} 早于开门时间入场"

    result = constraints.validate(catalog, plan, request)
    assert not [i for i in result["issues"] if i["code"] == "ARRIVE_BEFORE_OPENING"]


def test_wait_item_explains_the_gap() -> None:
    """等待开门要有显式条目，时间表不留无法解释的空档。"""
    catalog = get_catalog()
    plan = generate(catalog, family_request(days=2, hotel_region_id="MOCK_REG_CHUO"))
    waits = [
        item for day in plan["days"] for item in day["items"] if item["type"] == "wait"
    ]
    for wait in waits:
        assert wait["duration_minutes"] > 0
        assert "开门" in wait["note"]


def test_day_is_truncated_not_silently_clamped() -> None:
    """当日排不下时应显式截断，而非让时间被静默压到 23:59。"""
    catalog = get_catalog()
    plan = generate(catalog, family_request(pace="packed", days=3))
    for day in plan["days"]:
        assert "truncated_by_day_limit" in day["totals"]
        # 没有任何条目的结束时间应停在 23:59 这个截断值上。
        for item in day["items"]:
            if item.get("duration_minutes", 0) > 0:
                assert not (item["start_time"] == "23:59" and item["end_time"] == "23:59")


def test_weather_freshness_follows_daily_refresh_not_dataset_age() -> None:
    """天气是每日刷新的接口，时效按刷新节奏算，不按数据集生成日算。

    否则任何打包进 Lambda 的天气数据都会永久显示「已过期」，
    把建模错位伪装成真实告警，真正的过期场景反而被噪声淹没。
    """
    catalog = get_catalog()
    plan = generate(catalog, family_request())
    for day in plan["days"]:
        weather = day["weather"]
        if not weather["available"]:
            continue
        assert weather["freshness"]["is_fresh"] is True
        assert weather["refreshed_at"], "必须给出最近刷新时刻"
        assert weather["canned_at"], "数据集生成时间保留用于溯源"


def test_weather_stale_flag_no_longer_fires_for_every_day() -> None:
    catalog = get_catalog()
    request = family_request()
    plan = generate(catalog, request)
    result = constraints.validate(catalog, plan, request)
    assert not [i for i in result["issues"] if i["code"] == "WEATHER_DATA_STALE"]
