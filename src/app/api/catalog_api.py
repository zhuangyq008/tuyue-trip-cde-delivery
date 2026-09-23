"""目录与内容检索接口（商品、景点、可信内容）。"""

from __future__ import annotations

from typing import Any

from ..adapters.catalog import get_catalog
from ..domain import content
from ..http.errors import NotFoundError, field_error
from ..http.responses import ok
from ..http.validation import Validator
from .common import RequestContext, envelope, require_scope

MAX_SEARCH_LIMIT = 50


def get_product(ctx: RequestContext) -> dict[str, Any]:
    """GET /v1/products/{product_id}

    只返回**静态商品资料**。价格与库存不在此返回 —— 它们是动态事实，
    必须走 /v1/availability 查询（需求文档 §4.4「大模型/静态资料不替代交易事实源」）。
    `price_hint` 字段明确标注为历史参考价，不是报价。
    """
    require_scope(ctx, "read")
    product_id = ctx.path_params["product_id"]
    catalog = get_catalog()
    product = catalog.product(product_id)
    if product is None:
        raise NotFoundError(f"商品不存在：{product_id}")

    # 内部 mock 行为配置不对外暴露，避免调用方据此预测状态而绕过真实查询语义。
    public = {k: val for k, val in product.items() if k != "mock_supplier_behavior"}
    linked_pois = [
        {
            "poi_id": poi_id,
            "poi_name": (catalog.poi(poi_id) or {}).get("name_zh"),
            "region_name": (catalog.poi(poi_id) or {}).get("region_name"),
        }
        for poi_id in product.get("poi_ids") or []
    ]

    return ok(
        envelope(
            ctx,
            {
                "product": public,
                "linked_pois": linked_pois,
                "dynamic_fields_notice": {
                    "excluded": ["实时价格", "实时库存", "当日可订状态"],
                    "reason": "动态事实一律以供应商接口查询为准，不由静态目录或模型提供。",
                    "how_to_query": "POST /v1/availability/queries 或 GET /v1/products/{product_id}/availability",
                },
            },
        )
    )


def get_poi(ctx: RequestContext) -> dict[str, Any]:
    """GET /v1/pois/{poi_id} —— 景点静态资料 + 绑定商品 + 关联内容来源。"""
    require_scope(ctx, "read")
    poi_id = ctx.path_params["poi_id"]
    catalog = get_catalog()
    poi = catalog.poi(poi_id)
    if poi is None:
        raise NotFoundError(f"景点不存在：{poi_id}")

    return ok(
        envelope(
            ctx,
            {
                "poi": poi,
                "mapped_products": [
                    {
                        "product_id": product["product_id"],
                        "product_name": product.get("name_zh"),
                        "product_type": product.get("product_type"),
                        "mapping_method": "explicit_catalog_link",
                    }
                    for product in catalog.products_for_poi(poi_id)
                ],
                "content_sources": [content.to_result(doc) for doc in catalog.docs_for_poi(poi_id)],
            },
        )
    )


def search_content(ctx: RequestContext) -> dict[str, Any]:
    """GET /v1/content?q=&poi_id=&tags=&limit= —— 带来源与时效标注的内容检索。"""
    require_scope(ctx, "read")
    catalog = get_catalog()
    q = dict(ctx.query)

    payload: dict[str, Any] = {
        "q": q.get("q"),
        "poi_id": q.get("poi_id"),
        "tags": [t.strip() for t in (q.get("tags") or "").split(",") if t.strip()] or None,
        "limit": q.get("limit"),
    }
    payload = {k: val for k, val in payload.items() if val is not None}

    v = Validator(payload)
    v.reject_unknown({"q", "poi_id", "tags", "limit"})
    query_text = v.string("q", max_len=128, default="")
    poi_id = v.string("poi_id", max_len=64)
    tags = v.string_list("tags", max_items=10)

    limit = 10
    raw_limit = payload.get("limit")
    if raw_limit is not None:
        if not str(raw_limit).isdigit():
            v.errors.append(field_error("limit", f"不是合法整数：{raw_limit!r}", "1-50 的整数"))
        else:
            limit = int(raw_limit)
            if not 1 <= limit <= MAX_SEARCH_LIMIT:
                v.errors.append(field_error("limit", f"超出范围：{limit}", f"1-{MAX_SEARCH_LIMIT}"))

    if poi_id is not None and catalog.poi(poi_id) is None:
        # 过滤条件指向不存在的资源 → 422（参数非法），而非 404（本资源不存在）。
        v.errors.append(field_error("poi_id", f"景点标识不存在：{poi_id}", "有效的 poi_id"))

    v.raise_if_invalid()

    result = content.search(catalog, query=query_text or "", poi_id=poi_id, tags=tags, limit=limit)
    return ok(envelope(ctx, result))


def list_catalog(ctx: RequestContext) -> dict[str, Any]:
    """GET /v1/catalog —— 供演示与判分前自检使用的目录概览。"""
    require_scope(ctx, "read")
    catalog = get_catalog()
    return ok(
        envelope(
            ctx,
            {
                "dataset_meta": catalog.meta,
                "regions": sorted(catalog.regions.values(), key=lambda r: r["region_id"]),
                "pois": [
                    {
                        "poi_id": poi["poi_id"],
                        "name_zh": poi["name_zh"],
                        "region_id": poi["region_id"],
                        "category": poi["category"],
                        "indoor": poi["indoor"],
                        "kid_fit_score": poi["kid_fit_score"],
                        "closed_weekdays": poi["closed_weekdays"],
                        "closed_dates": poi["closed_dates"],
                    }
                    for poi in catalog.all_pois()
                ],
                "products": [
                    {
                        "product_id": product["product_id"],
                        "name_zh": product["name_zh"],
                        "product_type": product["product_type"],
                        "supplier_name": product["supplier_name"],
                        "poi_ids": product["poi_ids"],
                    }
                    for product in sorted(catalog.products.values(), key=lambda p: p["product_id"])
                ],
                "counts": {
                    "regions": len(catalog.regions),
                    "pois": len(catalog.pois),
                    "products": len(catalog.products),
                    "documents": len(catalog.documents),
                },
            },
        )
    )
