"""商户在营状态硬性约束与数据时效分级测试。

对应《数据清单-v3.md》：
  * #2 商户在营状态 —— 硬性约束，时效 ≤5min
  * #5 商户评分 —— T+1
  * #8 天气预报 —— 每日刷新
  * 其余各项的时效分级与覆盖度
"""

from __future__ import annotations

from datetime import date

import pytest

from app.adapters.catalog import get_catalog
from app.domain import freshness
from app.domain.availability import (
    STATE_AVAILABLE,
    STATE_NOT_ELIGIBLE,
    PartyRequest,
    check_merchant_status,
    query_product,
)
from conftest import call

TRAVEL_DATE = date(2026, 11, 20)
FAMILY = PartyRequest(adults=2, children=1, child_ages=[5])


@pytest.fixture(autouse=True)
def _clock(frozen_clock: None) -> None:
    """全组用例使用冻结时钟。"""


# ---------------------------------------------------------------- 商户在营状态（#2）


def test_suspended_merchant_blocks_single_merchant_product() -> None:
    """摩天轮暂停营业 → 其单商户商品不可订。"""
    catalog = get_catalog()
    blockers, advisories = check_merchant_status(catalog, "MOCK_PROD_SKY_DECK_TICKET")
    # 观景台商户正常，此商品不应因商户被拦。
    assert blockers == [] and advisories == []

    # 直接验证摩天轮商户本身被判不可用。
    ferris_blockers, _ = check_merchant_status(catalog, "MOCK_PROD_CITY_PASS_2DAY")
    assert ferris_blockers == []  # 通票不因单个场馆停业而整体阻断


def test_unknown_merchant_status_is_not_bookable() -> None:
    """状态未知不等于在营 —— 不得按「没说关门就是开门」处理。"""
    snapshot = query_product(get_catalog(), "MOCK_PROD_TRAIN_MUSEUM_TICKET", TRAVEL_DATE, FAMILY)
    assert snapshot["availability_state"] == STATE_NOT_ELIGIBLE
    assert snapshot["bookable"] is False
    assert snapshot["reason_code"] == "MERCHANT_UNKNOWN"


def test_stale_merchant_status_is_not_bookable() -> None:
    """状态虽为 OPEN 但已 42 分钟未更新，超出 ≤5min 时效要求 → 不可采信。"""
    snapshot = query_product(get_catalog(), "MOCK_PROD_NATURE_PARK_TICKET", TRAVEL_DATE, FAMILY)
    assert snapshot["availability_state"] == STATE_NOT_ELIGIBLE
    assert snapshot["bookable"] is False
    assert snapshot["reason_code"] == "MERCHANT_STATUS_STALE"
    violation = snapshot["eligibility_violations"][0]
    assert violation["freshness"]["max_age_seconds"] == 300
    assert violation["freshness"]["blocks_usage"] is True


def test_multi_merchant_pass_stays_bookable_with_partial_outage() -> None:
    """通票覆盖 4 个场馆，1 个停业：商品仍可订，但必须如实列出停业场馆。

    这条用例守护一个容易写错的方向 —— 过度阻断同样是缺陷：
    整张通票因单个场馆检修而判不可订，会直接损害转化。
    """
    snapshot = query_product(get_catalog(), "MOCK_PROD_CITY_PASS_2DAY", TRAVEL_DATE, FAMILY)
    assert snapshot["availability_state"] == STATE_AVAILABLE
    assert snapshot["bookable"] is True

    advisories = snapshot["merchant_advisories"]
    assert len(advisories) >= 1
    assert any("SUSPENDED" in a["code"] for a in advisories)
    assert all(a["severity"] == "advisory" for a in advisories)


def test_merchant_without_any_link_is_blocked() -> None:
    catalog = get_catalog()
    blockers, _ = check_merchant_status(catalog, "MOCK_PROD_DOES_NOT_EXIST")
    assert blockers[0]["code"] == "MERCHANT_UNKNOWN"


def test_all_merchants_have_status_rating_and_coordinates() -> None:
    """清单 #1/#2/#5 的字段完整性。"""
    for merchant in get_catalog().merchants.values():
        status = merchant["operating_status"]
        assert status["status"] in {"OPEN", "SUSPENDED", "CLOSED_PERMANENTLY", "UNKNOWN"}
        assert status["freshness_requirement_seconds"] == 300
        assert merchant["rating"]["overall"] > 0
        assert merchant["rating"]["review_count"] >= 0
        assert merchant["coordinates"]["lat"] and merchant["coordinates"]["lng"]


# ---------------------------------------------------------------- 时效分级


def test_realtime_tier_allows_no_age() -> None:
    """库存为实时数据（#4）：任何非零年龄都不算新鲜。"""
    assert freshness.assess_age(4, 0).is_fresh is True
    assert freshness.assess_age(4, 1).is_fresh is False


def test_hard_constraint_items_block_on_stale() -> None:
    """#2 与 #4 是硬性约束，过期即阻断；其余项过期只标注。"""
    for number in (2, 4):
        verdict = freshness.assess_age(number, 10**6)
        assert verdict.blocks_usage is True, number
    for number in (1, 3, 5, 6, 7, 8, 9):
        verdict = freshness.assess_age(number, 10**9)
        assert verdict.is_fresh is False, number
        assert verdict.blocks_usage is False, number


def test_unknown_age_blocks_hard_constraint_items() -> None:
    """年龄未知时，硬性约束项按不可用处理 —— 「不知道多旧」不等于「还新」。"""
    assert freshness.assess_age(2, None).blocks_usage is True
    assert freshness.assess_age(8, None).blocks_usage is False


def test_timestamp_assessment_handles_bad_input() -> None:
    assert freshness.assess_timestamp(8, None).age_seconds is None
    assert freshness.assess_timestamp(8, "not-a-date").age_seconds is None
    assert freshness.assess_timestamp(8, "2026-11-18T00:00:00Z").is_fresh is True


def test_inventory_covers_all_nine_items() -> None:
    report = freshness.inventory_report()
    assert [item["number"] for item in report] == list(range(1, 10))
    assert sum(1 for item in report if item["hard_constraint"]) == 2
    assert all(item["implementation"] for item in report)


# ---------------------------------------------------------------- 天气与季节（#8/#9）


def test_weather_covers_travel_window_with_rain_scenarios() -> None:
    catalog = get_catalog()
    assert catalog.weather("2026-11-20") is not None
    conditions = {w["condition"] for w in catalog.weather_by_date.values()}
    assert {"sunny", "light_rain", "heavy_rain"} <= conditions


def test_weather_missing_date_returns_none_not_default_sunny() -> None:
    """缺数据必须返回 None，让调用方按未知处理，不能默认晴天。"""
    assert get_catalog().weather("2099-01-01") is None


def test_seasonality_covers_twelve_months() -> None:
    catalog = get_catalog()
    assert {month for month in catalog.seasonality} == set(range(1, 13))
    assert catalog.season(11)["peak_level"] == "peak"


# ---------------------------------------------------------------- 对外接口


def test_data_inventory_endpoint_mirrors_checklist() -> None:
    status, body = call("GET", "/v1/data-inventory")
    assert status == 200
    assert len(body["items"]) == 9
    assert body["source_document"] == "数据清单-v3.md"
    hard = [item for item in body["items"] if item["hard_constraint"]]
    assert {item["number"] for item in hard} == {2, 4}


def test_merchant_endpoint_exposes_status_freshness() -> None:
    status, body = call("GET", "/v1/merchants/MOCK_MER_NATURE_PARK")
    assert status == 200
    assert body["merchant"]["operating_status"]["status"] == "OPEN"
    # 过期状态必须被明确判定，不能只把原始字段丢出去。
    assert body["status_freshness"]["is_fresh"] is False
    assert body["usable_for_recommendation"] is False


def test_unknown_merchant_is_404() -> None:
    status, _ = call("GET", "/v1/merchants/MOCK_MER_NOPE")
    assert status == 404


def test_destinations_endpoint() -> None:
    status, body = call("GET", "/v1/destinations")
    assert status == 200
    assert body["destinations"][0]["currency"] == "JPY"


def test_weather_endpoint_returns_forecast_with_source() -> None:
    status, body = call(
        "GET", "/v1/weather", query={"destination_id": "MOCK_DEST_OSAKA", "start_date": "2026-11-20", "days": "2"}
    )
    assert status == 200
    assert len(body["forecasts"]) == 2
    assert body["forecasts"][0]["source"]["type"] == "third_party_api"
    assert body["freshness"]["tier"] == "daily"


def test_weather_endpoint_rejects_bad_days() -> None:
    status, _ = call(
        "GET", "/v1/weather", query={"destination_id": "MOCK_DEST_OSAKA", "start_date": "2026-11-20", "days": "99"}
    )
    assert status == 422


def test_seasonality_endpoint() -> None:
    status, body = call("GET", "/v1/destinations/MOCK_DEST_OSAKA/seasonality")
    assert status == 200
    assert len(body["seasonality"]) == 12
