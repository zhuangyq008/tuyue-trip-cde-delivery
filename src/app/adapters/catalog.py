"""目录仓库：POI / 商品 / 区域 / 交通矩阵 / 内容语料。

数据随 Lambda 包一起分发，进程启动时一次性加载并缓存（跨调用复用）。
这样做的原因：目录是**只读静态数据**，放进 DynamoDB 只会引入一个
「部署后必须灌数、灌数失败则接口全挂」的额外故障点；判分窗口内必须稳定
（规范 §4.5），因此把静态目录做成不可变的包内资源，运行态存储只承载
真正的可变状态（行程、预订意图、幂等记录、埋点、可订快照）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "mock"

INTRA_REGION_FALLBACK_MINUTES = 15
UNKNOWN_TRANSIT_MINUTES = 90  # 缺失区域对：按保守上限处理，宁可判为交通过长


def _load(name: str) -> Any:
    with (_DATA_DIR / f"{name}.json").open(encoding="utf-8") as handle:
        return json.load(handle)


@dataclass(frozen=True)
class Catalog:
    meta: dict[str, Any]
    destinations: dict[str, dict[str, Any]]
    regions: dict[str, dict[str, Any]]
    transit: dict[str, int]
    pois: dict[str, dict[str, Any]]
    merchants: dict[str, dict[str, Any]]
    products: dict[str, dict[str, Any]]
    suppliers: dict[str, dict[str, Any]]
    documents: list[dict[str, Any]]
    seasonality: dict[int, dict[str, Any]]
    products_by_poi: dict[str, list[str]] = field(default_factory=dict)
    docs_by_poi: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    weather_by_date: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ---------- POI ----------

    def poi(self, poi_id: str) -> dict[str, Any] | None:
        return self.pois.get(poi_id)

    def pois_in_region(self, region_id: str) -> list[dict[str, Any]]:
        return [p for p in self.pois.values() if p["region_id"] == region_id]

    def all_pois(self) -> list[dict[str, Any]]:
        # 固定排序，保证行程生成完全确定（规范 §4.5 幂等）。
        return sorted(self.pois.values(), key=lambda p: p["poi_id"])

    # ---------- 商品 ----------

    def product(self, product_id: str) -> dict[str, Any] | None:
        return self.products.get(product_id)

    def products_for_poi(self, poi_id: str) -> list[dict[str, Any]]:
        return [self.products[pid] for pid in self.products_by_poi.get(poi_id, [])]

    # ---------- 交通 ----------

    def transit_minutes(self, from_region: str, to_region: str) -> tuple[int, bool]:
        """返回 (耗时分钟, 是否已知)。未知区域对返回保守上限并标记未知。"""
        if from_region == to_region:
            return self.transit.get(f"{from_region}|{to_region}", INTRA_REGION_FALLBACK_MINUTES), True
        known = self.transit.get(f"{from_region}|{to_region}")
        if known is None:
            return UNKNOWN_TRANSIT_MINUTES, False
        return known, True

    def region_name(self, region_id: str) -> str:
        region = self.regions.get(region_id)
        return region["name_zh"] if region else region_id

    # ---------- 商户（清单 #1/#2/#5）----------

    def merchant(self, merchant_id: str) -> dict[str, Any] | None:
        return self.merchants.get(merchant_id)

    def merchant_for_poi(self, poi_id: str) -> dict[str, Any] | None:
        poi = self.pois.get(poi_id)
        if poi is None:
            return None
        return self.merchants.get(str(poi.get("merchant_id") or ""))

    def merchants_for_product(self, product_id: str) -> list[dict[str, Any]]:
        product = self.products.get(product_id)
        if product is None:
            return []
        return [
            self.merchants[mid]
            for mid in (product.get("merchant_ids") or [])
            if mid in self.merchants
        ]

    # ---------- 目的地与季节（清单 #6/#9）----------

    def destination(self, destination_id: str) -> dict[str, Any] | None:
        return self.destinations.get(destination_id)

    def destination_by_alias(self, name: str) -> dict[str, Any] | None:
        for destination in self.destinations.values():
            if name in (destination.get("aliases") or []) or name == destination.get("name_zh"):
                return destination
        return None

    def season(self, month: int) -> dict[str, Any] | None:
        return self.seasonality.get(month)

    # ---------- 天气（清单 #8）----------

    def weather(self, day: str) -> dict[str, Any] | None:
        """按日期取预报。无数据返回 None —— 调用方须按「未知」处理，不得假设晴天。"""
        return self.weather_by_date.get(day)

    # ---------- 内容 ----------

    def docs_for_poi(self, poi_id: str) -> list[dict[str, Any]]:
        return self.docs_by_poi.get(poi_id, [])


@lru_cache(maxsize=1)
def get_catalog() -> Catalog:
    regions = {r["region_id"]: r for r in _load("regions")}
    pois = {p["poi_id"]: p for p in _load("pois")}
    products = {p["product_id"]: p for p in _load("products")}
    suppliers = {s["supplier_id"]: s for s in _load("suppliers")}
    documents = _load("documents")

    products_by_poi: dict[str, list[str]] = {}
    for product in products.values():
        for poi_id in product["poi_ids"]:
            products_by_poi.setdefault(poi_id, []).append(product["product_id"])
    # 商品映射按 ID 排序，避免因加载顺序导致推荐结果漂移。
    for poi_id in products_by_poi:
        products_by_poi[poi_id].sort()

    docs_by_poi: dict[str, list[dict[str, Any]]] = {}
    for doc in documents:
        docs_by_poi.setdefault(doc["poi_id"], []).append(doc)
    for poi_id in docs_by_poi:
        docs_by_poi[poi_id].sort(key=lambda d: d["doc_id"])

    return Catalog(
        meta=_load("meta"),
        destinations={d["destination_id"]: d for d in _load("destinations")},
        regions=regions,
        transit={k: int(v) for k, v in _load("transit_minutes").items()},
        pois=pois,
        merchants={m["merchant_id"]: m for m in _load("merchants")},
        products=products,
        suppliers=suppliers,
        documents=sorted(documents, key=lambda d: d["doc_id"]),
        seasonality={int(entry["month"]): entry for entry in _load("seasonality")},
        products_by_poi=products_by_poi,
        docs_by_poi=docs_by_poi,
        weather_by_date={forecast["date"]: forecast for forecast in _load("weather")},
    )
