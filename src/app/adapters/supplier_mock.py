"""Mock 供应商接口。

这是**唯一**的价格与库存事实来源（需求文档 §2.3「不编造动态事实」）。
大模型与任何缓存都不得替代本模块回答「能不能订、多少钱」。

真实供应商接口的失败形态在此确定性复现（由数据集 `mock_supplier_behavior` 驱动）：
超时、5xx、字段不完整、明确售罄、按日期售罄。
判分需要可复现，因此**不引入真实 sleep**：`timeout` 模式直接抛出超时异常并
上报配置的超时时长，语义等价但不浪费 Lambda 15s 预算。

接口签名刻意保持与真实 HTTP 供应商一致（入参：商品/日期/人数；出参：原始字典），
后续替换为真实接口时只需替换本文件，域层无需改动。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Any

from ..core.config import get_config


class SupplierTimeout(Exception):
    """供应商在超时预算内未返回。绝不可解读为可订。"""


class SupplierUnavailable(Exception):
    """供应商返回 5xx 或连接失败。绝不可解读为可订。"""


@dataclass(frozen=True)
class SupplierQuery:
    product_id: str
    travel_date: date
    adults: int
    children: int
    child_ages: tuple[int, ...]


def _deterministic_int(seed: str, lower: int, upper: int) -> int:
    """由种子派生稳定整数，保证同一查询每次得到同一库存数字。"""
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    span = upper - lower + 1
    return lower + (int.from_bytes(digest[:4], "big") % span)


def query_availability(product: dict[str, Any], query: SupplierQuery) -> dict[str, Any]:
    """查询供应商可订状态。

    返回**原始供应商响应**（故意保留不完整/异常形态），由 domain 层负责解读。
    异常按真实接口语义抛出，不在此处吞掉。
    """
    behavior = product.get("mock_supplier_behavior") or {}
    mode = behavior.get("mode", "ok")
    date_str = query.travel_date.isoformat()
    cfg = get_config()

    if mode == "timeout":
        raise SupplierTimeout(
            f"供应商 {product['supplier_id']} 在 {cfg.supplier_timeout_ms}ms 内未响应"
        )

    if mode == "error":
        raise SupplierUnavailable(f"供应商 {product['supplier_id']} 返回 503")

    if mode == "sold_out":
        return {
            "status": "SOLD_OUT",
            "product_id": product["product_id"],
            "travel_date": date_str,
            "supplier_message": "MOCK_该日期库存已售完",
        }

    if mode == "partial":
        # 真实世界的常见形态：HTTP 200 但业务字段缺失。
        # 不得按「没说不行就是行」处理。
        return {
            "product_id": product["product_id"],
            "travel_date": date_str,
            "supplier_message": "MOCK_响应字段不完整，缺少 status 与价格",
        }

    if mode == "date_based" and date_str in (behavior.get("sold_out_dates") or []):
        return {
            "status": "SOLD_OUT",
            "product_id": product["product_id"],
            "travel_date": date_str,
            "supplier_message": "MOCK_该日期为售罄日",
        }

    if date_str in ((product.get("date_rules") or {}).get("blackout_dates") or []):
        return {
            "status": "SOLD_OUT",
            "product_id": product["product_id"],
            "travel_date": date_str,
            "supplier_message": "MOCK_该日期不开放销售",
        }

    price_hint = product.get("price_hint") or {}
    seed = f"{product['product_id']}|{date_str}"
    billable_children = _billable_children(product, query)
    unit_adult = float(price_hint.get("adult", 0.0))
    unit_child = float(price_hint.get("child", 0.0))

    return {
        "status": "AVAILABLE",
        "product_id": product["product_id"],
        "travel_date": date_str,
        "currency": product.get("currency", "CNY"),
        "unit_price": {"adult": unit_adult, "child": unit_child},
        "total_price": round(unit_adult * query.adults + unit_child * billable_children, 2),
        "billable_pax": {"adults": query.adults, "children": billable_children},
        "remaining_inventory": _deterministic_int(seed, 3, 40),
        # 供应商侧给出的报价有效期；下游必须把它当作**时效**而非承诺。
        "quote_valid_seconds": 900,
        "supplier_message": "MOCK_可售",
    }


def _billable_children(product: dict[str, Any], query: SupplierQuery) -> int:
    """按商品的免票年龄规则计算需计费的儿童数。"""
    rules = product.get("pax_rules") or {}
    free_under = rules.get("infant_free_under")
    if not isinstance(free_under, int):
        return query.children
    return sum(1 for age in query.child_ages if age >= free_under)


def supplier_name(product: dict[str, Any]) -> str:
    return str(product.get("supplier_name") or product.get("supplier_id") or "MOCK_UNKNOWN_SUPPLIER")
