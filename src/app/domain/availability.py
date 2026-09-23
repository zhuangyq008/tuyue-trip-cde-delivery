"""动态可订状态机。

**全项目最重要的不变式**：

    bookable is True  ⟺  availability_state == "AVAILABLE"

五种状态中只有一种可订，其余四种一律不可订：

| 状态          | 触发条件                         | 可订 |
|---------------|----------------------------------|------|
| AVAILABLE     | 供应商明确确认可售且资格校验通过 | 是   |
| SOLD_OUT      | 供应商明确确认不可售             | 否   |
| UNKNOWN       | 供应商 200 但业务字段不完整      | 否   |
| UNCONFIRMED   | 查询超时或供应商 5xx             | 否   |
| NOT_ELIGIBLE  | 人数/日期/年龄等硬规则不满足     | 否   |

对应需求文档的刚性约束：
  * §2.2「不将超时、未知或失败状态当作成功」
  * §2.3「可订状态有时效，查询成功不是履约保证」
  * §4.3 P0「售罄、查询超时、未知状态不会被展示成可订」

时效建模：每次查询产出一份带 `checked_at` / `expires_at` 的**快照**并落 TTL 存储。
窗口内的重复查询返回同一份快照（满足规范 §4.5 幂等），窗口外重新向供应商查询。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from ..adapters import store
from ..adapters.catalog import Catalog
from ..adapters.supplier_mock import (
    SupplierQuery,
    SupplierTimeout,
    SupplierUnavailable,
    query_availability,
    supplier_name,
)
from ..core import logging as log
from ..core.clock import iso, now, today_jst
from ..core.config import get_config
from ..core.ids import derive_id, fingerprint
from . import freshness

STATE_AVAILABLE = "AVAILABLE"
STATE_SOLD_OUT = "SOLD_OUT"
STATE_UNKNOWN = "UNKNOWN"
STATE_UNCONFIRMED = "UNCONFIRMED"
STATE_NOT_ELIGIBLE = "NOT_ELIGIBLE"

NON_BOOKABLE_STATES = frozenset({STATE_SOLD_OUT, STATE_UNKNOWN, STATE_UNCONFIRMED, STATE_NOT_ELIGIBLE})

# 固定对外免责口径：任何可订结果都必须携带，禁止在展示层省略。
QUERY_DISCLAIMER = (
    "本结果为查询时点的供应商状态快照，不构成锁价或库存保留；"
    "进入预订时由交易系统按当时价格、库存与适用条件重新确认。"
)


@dataclass
class PartyRequest:
    adults: int
    children: int
    child_ages: list[int] = field(default_factory=list)

    @property
    def total_pax(self) -> int:
        return self.adults + self.children


def snapshot_key(product_id: str, travel_date: date, party: PartyRequest) -> str:
    return fingerprint(
        {
            "product_id": product_id,
            "travel_date": travel_date.isoformat(),
            "adults": party.adults,
            "children": party.children,
            "child_ages": sorted(party.child_ages),
        }
    )


# ---------------------------------------------------------------- 资格校验


def _merchant_unavailable_reason(merchant: dict[str, Any]) -> dict[str, Any] | None:
    """单个商户是否不可用。可用则返回 None。

    两条判定（数据清单 #2，时效要求 ≤5min）：
      * 状态非 OPEN（暂停/永久关闭/未知）→ 不可用
      * 状态虽为 OPEN 但**已超出 5 分钟时效**→ 同样不可用

    第二条是关键：一条 42 分钟前同步的「在营」不能证明商户此刻还在营业。
    把过期数据当成当前事实，正是需求文档 §2.2 列出的根因假设之一。
    """
    status_block = merchant.get("operating_status") or {}
    status = status_block.get("status")
    age_seconds = status_block.get("status_age_seconds")
    verdict = freshness.assess_age(2, age_seconds if isinstance(age_seconds, int) else None)

    if status != "OPEN":
        note = status_block.get("note")
        return {
            "code": f"MERCHANT_{status or 'STATUS_MISSING'}",
            "detail": (
                f"商户 {merchant['name_zh']} 当前状态为 {status}"
                f"{'（' + note + '）' if note else ''}，不满足在营硬性约束。"
            ),
            "merchant_id": merchant["merchant_id"],
            "freshness": verdict.to_dict(),
        }

    if verdict.blocks_usage:
        return {
            "code": "MERCHANT_STATUS_STALE",
            "detail": (
                f"商户 {merchant['name_zh']} 的在营状态已 {age_seconds}s 未更新，"
                "超出 ≤5min 时效要求，不能作为当前在营的依据。"
            ),
            "merchant_id": merchant["merchant_id"],
            "freshness": verdict.to_dict(),
        }

    return None


def check_merchant_status(catalog: Catalog, product_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """商户在营状态硬性约束（数据清单 #2）。

    返回 `(阻断项, 提示项)`。按商品覆盖的商户数量分两种判定：

    * **单商户商品**（门票、酒店、接送）：该商户不可用即阻断。
    * **多商户商品**（周游卡等通票）：仅当**全部**商户都不可用才阻断；
      部分场馆停业时，通票仍可售，把停业场馆作为提示如实列出。

    这个区分是必要的：一张覆盖 4 个场馆的通票，因为其中 1 个场馆检修
    就整张判为不可订，属于过度阻断 —— 既不符合真实售卖规则，
    也与「改善攻略到交易转化」的项目目标相悖。
    """
    merchants = catalog.merchants_for_product(product_id)

    if not merchants:
        return (
            [
                {
                    "code": "MERCHANT_UNKNOWN",
                    "detail": f"商品 {product_id} 未关联任何商户，无法确认经营主体是否在营。",
                }
            ],
            [],
        )

    unavailable = [
        reason for reason in (_merchant_unavailable_reason(m) for m in merchants) if reason is not None
    ]

    if not unavailable:
        return [], []

    # 全部商户都不可用 → 阻断。
    if len(unavailable) == len(merchants):
        return unavailable, []

    # 部分不可用 → 商品仍可售，但必须如实告知哪些场馆当前不可用。
    return [], [
        {
            **reason,
            "severity": "advisory",
            "impact": "该商品覆盖的部分场馆当前不可用，商品本身仍可售；请在展示中如实说明。",
        }
        for reason in unavailable
    ]


def check_eligibility(product: dict[str, Any], travel_date: date, party: PartyRequest) -> list[dict[str, Any]]:
    """向供应商发起查询**之前**的硬规则校验。

    不满足的条目不会去查供应商 —— 既省一次无谓调用，
    也避免「供应商说有货」被误当成「这单能下」。
    """
    violations: list[dict[str, Any]] = []
    rules = product.get("pax_rules") or {}
    date_rules = product.get("date_rules") or {}

    min_pax = rules.get("min_pax")
    max_pax = rules.get("max_pax")
    if isinstance(min_pax, int) and party.total_pax < min_pax:
        violations.append(
            {"code": "PAX_BELOW_MIN", "detail": f"该商品最少 {min_pax} 人成行，当前 {party.total_pax} 人"}
        )
    if isinstance(max_pax, int) and party.total_pax > max_pax:
        violations.append(
            {"code": "PAX_ABOVE_MAX", "detail": f"该商品最多 {max_pax} 人，当前 {party.total_pax} 人"}
        )

    child_range = rules.get("child_age_range")
    free_under = rules.get("infant_free_under")
    if isinstance(child_range, list) and len(child_range) == 2:
        low, high = int(child_range[0]), int(child_range[1])
        for age in sorted(party.child_ages):
            # 低于免票年龄属于「免票婴幼儿」，不算违规。
            if isinstance(free_under, int) and age < free_under:
                continue
            if age < low or age > high:
                violations.append(
                    {
                        "code": "CHILD_AGE_OUT_OF_RANGE",
                        "detail": f"{age} 岁儿童不在该商品儿童票适用区间 {low}-{high} 岁内",
                    }
                )

    today = today_jst()
    days_ahead = (travel_date - today).days
    from_ahead = date_rules.get("bookable_from_days_ahead")
    until_ahead = date_rules.get("bookable_until_days_ahead")
    if isinstance(from_ahead, int) and days_ahead < from_ahead:
        violations.append(
            {
                "code": "LEAD_TIME_TOO_SHORT",
                "detail": f"该商品需至少提前 {from_ahead} 天预订，当前仅提前 {days_ahead} 天",
            }
        )
    if isinstance(until_ahead, int) and days_ahead > until_ahead:
        violations.append(
            {
                "code": "BEYOND_BOOKING_WINDOW",
                "detail": f"该商品最多提前 {until_ahead} 天开放预订，当前提前 {days_ahead} 天",
            }
        )

    return violations


# ---------------------------------------------------------------- 查询编排


def query_product(
    catalog: Catalog,
    product_id: str,
    travel_date: date,
    party: PartyRequest,
    *,
    use_cache: bool = True,
) -> dict[str, Any]:
    """查询单个商品的可订状态，返回对外快照结构。

    商品不存在时返回 NOT_ELIGIBLE 条目而非抛 404：
    批量查询里单条商品拼错不应让整个请求失败。
    路径参数形式的单资源查询由 API 层负责给 404。
    """
    product = catalog.product(product_id)
    if product is None:
        return _snapshot(
            product_id=product_id,
            product_name=None,
            supplier=None,
            travel_date=travel_date,
            party=party,
            state=STATE_NOT_ELIGIBLE,
            reason_code="PRODUCT_NOT_FOUND",
            reason="商品标识不存在于当前目录中。",
            violations=[{"code": "PRODUCT_NOT_FOUND", "detail": f"未找到商品 {product_id}"}],
        )

    key = snapshot_key(product_id, travel_date, party)
    if use_cache:
        cached = store.load_availability_snapshot(key)
        if cached is not None:
            # 返回与首次完全一致的快照（含 checked_at），幂等由此保证。
            return {**cached, "from_cache": True}

    # 商户在营状态先判（硬性约束，数据清单 #2），再判人数/日期规则。
    # 商户已停业时连供应商都不必查 —— 库存有货也不该推给用户。
    merchant_blockers, merchant_advisories = check_merchant_status(catalog, product_id)
    violations = [*merchant_blockers, *check_eligibility(product, travel_date, party)]
    if violations:
        snapshot = _snapshot(
            product_id=product_id,
            product_name=product.get("name_zh"),
            supplier=supplier_name(product),
            travel_date=travel_date,
            party=party,
            state=STATE_NOT_ELIGIBLE,
            reason_code=violations[0]["code"],
            reason=violations[0]["detail"],
            violations=violations,
            advisories=merchant_advisories,
            product=product,
        )
        store.save_availability_snapshot(key, snapshot)
        return snapshot

    try:
        raw = query_availability(product, _to_supplier_query(product_id, travel_date, party))
    except SupplierTimeout as exc:
        snapshot = _snapshot(
            product_id=product_id,
            product_name=product.get("name_zh"),
            supplier=supplier_name(product),
            travel_date=travel_date,
            party=party,
            state=STATE_UNCONFIRMED,
            reason_code="SUPPLIER_TIMEOUT",
            reason=f"供应商查询超时，暂无法确认是否可订：{exc}",
            advisories=merchant_advisories,
            product=product,
        )
        log.warn("supplier_timeout", product_id=product_id, travel_date=travel_date.isoformat())
        store.save_availability_snapshot(key, snapshot)
        return snapshot
    except SupplierUnavailable as exc:
        snapshot = _snapshot(
            product_id=product_id,
            product_name=product.get("name_zh"),
            supplier=supplier_name(product),
            travel_date=travel_date,
            party=party,
            state=STATE_UNCONFIRMED,
            reason_code="SUPPLIER_UNAVAILABLE",
            reason=f"供应商接口异常，暂无法确认是否可订：{exc}",
            advisories=merchant_advisories,
            product=product,
        )
        log.warn("supplier_unavailable", product_id=product_id, travel_date=travel_date.isoformat())
        store.save_availability_snapshot(key, snapshot)
        return snapshot

    snapshot = _interpret(product, raw, travel_date, party, advisories=merchant_advisories)
    store.save_availability_snapshot(key, snapshot)
    return snapshot


def _to_supplier_query(product_id: str, travel_date: date, party: PartyRequest) -> SupplierQuery:
    return SupplierQuery(
        product_id=product_id,
        travel_date=travel_date,
        adults=party.adults,
        children=party.children,
        child_ages=tuple(sorted(party.child_ages)),
    )


def _interpret(
    product: dict[str, Any],
    raw: dict[str, Any],
    travel_date: date,
    party: PartyRequest,
    *,
    advisories: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """解读供应商原始响应。

    白名单式解读：只有明确出现 AVAILABLE **且**价格字段齐备才判为可订。
    任何缺字段、未知枚举值都落到 UNKNOWN，绝不「宽容地」当成可订。
    """
    status = raw.get("status")
    common = {
        "product_id": product["product_id"],
        "product_name": product.get("name_zh"),
        "supplier": supplier_name(product),
        "travel_date": travel_date,
        "party": party,
        "product": product,
        "advisories": advisories,
    }

    if status == STATE_AVAILABLE:
        unit_price = raw.get("unit_price")
        total_price = raw.get("total_price")
        if not isinstance(unit_price, dict) or not isinstance(total_price, (int, float)):
            return _snapshot(
                state=STATE_UNKNOWN,
                reason_code="SUPPLIER_PRICE_MISSING",
                reason="供应商返回可售但价格字段缺失，无法确认可订。",
                **common,
            )
        return _snapshot(
            state=STATE_AVAILABLE,
            reason_code="SUPPLIER_CONFIRMED",
            reason="供应商在查询时点确认可售。",
            price={
                "currency": raw.get("currency", product.get("currency", "CNY")),
                "unit_price": unit_price,
                "total_price": round(float(total_price), 2),
                "billable_pax": raw.get("billable_pax"),
            },
            remaining_inventory=raw.get("remaining_inventory"),
            quote_valid_seconds=raw.get("quote_valid_seconds"),
            **common,
        )

    if status == STATE_SOLD_OUT:
        return _snapshot(
            state=STATE_SOLD_OUT,
            reason_code="SUPPLIER_SOLD_OUT",
            reason=str(raw.get("supplier_message") or "供应商确认该日期不可售。"),
            **common,
        )

    return _snapshot(
        state=STATE_UNKNOWN,
        reason_code="SUPPLIER_RESPONSE_INCOMPLETE",
        reason=str(
            raw.get("supplier_message") or "供应商响应缺少可识别的状态字段，无法确认是否可订。"
        ),
        **common,
    )


def _snapshot(
    *,
    product_id: str,
    product_name: str | None,
    supplier: str | None,
    travel_date: date,
    party: PartyRequest,
    state: str,
    reason_code: str,
    reason: str,
    violations: list[dict[str, Any]] | None = None,
    advisories: list[dict[str, Any]] | None = None,
    price: dict[str, Any] | None = None,
    remaining_inventory: Any = None,
    quote_valid_seconds: Any = None,
    product: dict[str, Any] | None = None,
) -> dict[str, Any]:
    cfg = get_config()
    checked_at = now()
    ttl = cfg.availability_ttl_seconds
    bookable = state == STATE_AVAILABLE

    snapshot: dict[str, Any] = {
        "product_id": product_id,
        "product_name": product_name,
        "supplier": supplier,
        "travel_date": travel_date.isoformat(),
        "party": {
            "adults": party.adults,
            "children": party.children,
            "child_ages": sorted(party.child_ages),
        },
        "availability_state": state,
        # 单一不变式：只有 AVAILABLE 为 True。
        "bookable": bookable,
        "reason_code": reason_code,
        "reason": reason,
        "checked_at": iso(checked_at),
        "expires_at": iso(checked_at + timedelta(seconds=ttl)),
        "snapshot_ttl_seconds": ttl,
        "from_cache": False,
        "fact_source": "supplier_api" if state in {STATE_AVAILABLE, STATE_SOLD_OUT} else "not_established",
        "disclaimer": QUERY_DISCLAIMER,
        "snapshot_id": derive_id("AVL", product_id, travel_date.isoformat(), party.adults, sorted(party.child_ages), state),
    }

    if violations:
        snapshot["eligibility_violations"] = violations
    if advisories:
        # 提示项不影响可订判定，但必须如实呈现，不允许静默丢弃。
        snapshot["merchant_advisories"] = advisories
    if price is not None:
        snapshot["price"] = price
    if remaining_inventory is not None:
        snapshot["remaining_inventory"] = remaining_inventory
    if quote_valid_seconds is not None:
        snapshot["quote_valid_seconds"] = quote_valid_seconds
    if product is not None:
        snapshot["refund_policy"] = product.get("refund_policy")
        snapshot["requires_reservation"] = bool(product.get("requires_reservation"))

    # 用显式检查而非 assert 守护不变式：assert 可能被解释器优化掉，
    # 而这条不变式被违反就是「把不可订展示成可订」——正是需求文档点名要避免的事故形态。
    if snapshot["bookable"] != (snapshot["availability_state"] == STATE_AVAILABLE):
        raise RuntimeError(
            f"可订不变式被破坏：state={snapshot['availability_state']} bookable={snapshot['bookable']}"
        )
    return snapshot
