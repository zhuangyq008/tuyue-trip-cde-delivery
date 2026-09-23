"""可订状态机测试。

这组测试守护全项目最重要的不变式：
    bookable is True  ⟺  availability_state == "AVAILABLE"

需求文档把「把不可订展示成可订」列为已发生过的业务事故形态，
因此五种状态各有一条独立用例，任何一条失败都视为交付阻断。
"""

from __future__ import annotations

from datetime import date

import pytest

from app.adapters.catalog import get_catalog
from app.domain.availability import (
    NON_BOOKABLE_STATES,
    STATE_AVAILABLE,
    STATE_NOT_ELIGIBLE,
    STATE_SOLD_OUT,
    STATE_UNCONFIRMED,
    STATE_UNKNOWN,
    PartyRequest,
    check_eligibility,
    query_product,
)

TRAVEL_DATE = date(2026, 11, 20)
FAMILY = PartyRequest(adults=2, children=1, child_ages=[5])


@pytest.fixture(autouse=True)
def _clock(frozen_clock: None) -> None:
    """全组用例使用冻结时钟，使提前期与日期判定稳定。"""


def test_ok_product_is_bookable() -> None:
    snapshot = query_product(get_catalog(), "MOCK_PROD_AQUARIUM_TICKET", TRAVEL_DATE, FAMILY)
    assert snapshot["availability_state"] == STATE_AVAILABLE
    assert snapshot["bookable"] is True
    assert snapshot["fact_source"] == "supplier_api"
    assert snapshot["price"]["total_price"] > 0
    # 可订也必须携带时效与免责说明，不允许省略。
    assert snapshot["checked_at"] and snapshot["expires_at"]
    assert "不构成锁价" in snapshot["disclaimer"]


def test_sold_out_is_never_bookable() -> None:
    snapshot = query_product(get_catalog(), "MOCK_PROD_THEMEPARK_EXPRESS", TRAVEL_DATE, FAMILY)
    assert snapshot["availability_state"] == STATE_SOLD_OUT
    assert snapshot["bookable"] is False
    assert "price" not in snapshot


def test_timeout_is_unconfirmed_not_bookable() -> None:
    """超时绝不能被当成可订 —— 需求文档 §2.2 明确点名。"""
    snapshot = query_product(get_catalog(), "MOCK_PROD_KIDS_SCIENCE_TICKET", TRAVEL_DATE, FAMILY)
    assert snapshot["availability_state"] == STATE_UNCONFIRMED
    assert snapshot["bookable"] is False
    assert snapshot["reason_code"] == "SUPPLIER_TIMEOUT"
    assert snapshot["fact_source"] == "not_established"


def test_supplier_error_is_unconfirmed_not_bookable() -> None:
    snapshot = query_product(get_catalog(), "MOCK_PROD_CASTLE_TICKET", TRAVEL_DATE, FAMILY)
    assert snapshot["availability_state"] == STATE_UNCONFIRMED
    assert snapshot["bookable"] is False
    assert snapshot["reason_code"] == "SUPPLIER_UNAVAILABLE"


def test_partial_response_is_unknown_not_bookable() -> None:
    """供应商返回 200 但字段不全：不得按「没说不行就是行」处理。"""
    snapshot = query_product(get_catalog(), "MOCK_PROD_SKY_DECK_TICKET", TRAVEL_DATE, FAMILY)
    assert snapshot["availability_state"] == STATE_UNKNOWN
    assert snapshot["bookable"] is False
    assert snapshot["reason_code"] == "SUPPLIER_RESPONSE_INCOMPLETE"


def test_date_based_sold_out() -> None:
    snapshot = query_product(get_catalog(), "MOCK_PROD_THEMEPARK_1DAY", date(2026, 11, 22), FAMILY)
    assert snapshot["availability_state"] == STATE_SOLD_OUT
    assert snapshot["bookable"] is False


def test_blackout_date_is_sold_out() -> None:
    snapshot = query_product(get_catalog(), "MOCK_PROD_AQUARIUM_TICKET", date(2026, 11, 25), FAMILY)
    assert snapshot["availability_state"] == STATE_SOLD_OUT
    assert snapshot["bookable"] is False


def test_unknown_product_is_not_eligible_not_404() -> None:
    """批量查询里单条商品拼错，不应让整个请求失败。"""
    snapshot = query_product(get_catalog(), "MOCK_PROD_DOES_NOT_EXIST", TRAVEL_DATE, FAMILY)
    assert snapshot["availability_state"] == STATE_NOT_ELIGIBLE
    assert snapshot["bookable"] is False
    assert snapshot["reason_code"] == "PRODUCT_NOT_FOUND"


def test_pax_above_max_is_not_eligible() -> None:
    big_party = PartyRequest(adults=9, children=3, child_ages=[4, 6, 8])
    snapshot = query_product(get_catalog(), "MOCK_PROD_THEMEPARK_EXPRESS", TRAVEL_DATE, big_party)
    assert snapshot["availability_state"] == STATE_NOT_ELIGIBLE
    assert snapshot["bookable"] is False
    assert any(v["code"] == "PAX_ABOVE_MAX" for v in snapshot["eligibility_violations"])


def test_lead_time_too_short_is_not_eligible() -> None:
    """需提前 2 天预约的商品，明天出发即不可订。"""
    tomorrow = date(2026, 11, 19)
    snapshot = query_product(get_catalog(), "MOCK_PROD_BRICK_CENTER_TICKET", tomorrow, PartyRequest(2, 1, [5]))
    assert snapshot["availability_state"] == STATE_NOT_ELIGIBLE
    assert any(v["code"] == "LEAD_TIME_TOO_SHORT" for v in snapshot["eligibility_violations"])


def test_infant_below_free_age_does_not_violate_child_range() -> None:
    """免票婴幼儿不应被判成「年龄超出儿童票区间」。"""
    infant_party = PartyRequest(adults=2, children=1, child_ages=[1])
    violations = check_eligibility(
        get_catalog().product("MOCK_PROD_AQUARIUM_TICKET"), TRAVEL_DATE, infant_party
    )
    assert not any(v["code"] == "CHILD_AGE_OUT_OF_RANGE" for v in violations)


def test_snapshot_is_cached_and_identical_within_ttl() -> None:
    """TTL 窗口内重复查询返回同一快照 —— 规范 §4.5 幂等的实现基础。"""
    first = query_product(get_catalog(), "MOCK_PROD_AQUARIUM_TICKET", TRAVEL_DATE, FAMILY)
    second = query_product(get_catalog(), "MOCK_PROD_AQUARIUM_TICKET", TRAVEL_DATE, FAMILY)
    assert second["from_cache"] is True
    assert first["checked_at"] == second["checked_at"]
    assert {k: v for k, v in first.items() if k != "from_cache"} == {
        k: v for k, v in second.items() if k != "from_cache"
    }


def test_bypassing_cache_refetches() -> None:
    query_product(get_catalog(), "MOCK_PROD_AQUARIUM_TICKET", TRAVEL_DATE, FAMILY)
    fresh = query_product(get_catalog(), "MOCK_PROD_AQUARIUM_TICKET", TRAVEL_DATE, FAMILY, use_cache=False)
    assert fresh["from_cache"] is False


def test_invariant_holds_across_every_catalog_product() -> None:
    """穷举目录内所有商品：不变式必须条条成立。"""
    catalog = get_catalog()
    for product_id in sorted(catalog.products):
        snapshot = query_product(catalog, product_id, TRAVEL_DATE, FAMILY, use_cache=False)
        state = snapshot["availability_state"]
        assert snapshot["bookable"] is (state == STATE_AVAILABLE), product_id
        if state in NON_BOOKABLE_STATES:
            assert snapshot["bookable"] is False, product_id
            # 不可订的条目不得携带报价，避免展示层误用。
            assert "price" not in snapshot, product_id
