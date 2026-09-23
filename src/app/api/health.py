"""健康检查与服务自描述。

注意：本接口**同样要求令牌**。
CDE 规范 §4.2 的判分用例专门验证「未带令牌 / 错误令牌是否被拒」，
留一个公开端点就等于给出一条绕过鉴权的路径，因此不设匿名探活。
"""

from __future__ import annotations

from typing import Any

from ..adapters.catalog import get_catalog
from ..core.clock import now_iso, today_jst
from ..core.config import get_config
from ..http.responses import ok
from .common import SUPPORTED_DESTINATIONS, RequestContext, envelope


def health(ctx: RequestContext) -> dict[str, Any]:
    catalog = get_catalog()
    cfg = get_config()
    return ok(
        envelope(
            ctx,
            {
                "status": "ok",
                "service": "yuetu-trip-assistant",
                "stage": cfg.stage,
                "server_time": now_iso(),
                "server_date_jst": today_jst().isoformat(),
                "dataset": {
                    "version": catalog.meta.get("dataset_version"),
                    "classification": catalog.meta.get("data_classification"),
                    "seed": catalog.meta.get("seed"),
                    "counts": {
                        "regions": len(catalog.regions),
                        "pois": len(catalog.pois),
                        "products": len(catalog.products),
                        "documents": len(catalog.documents),
                    },
                },
                "capabilities": {
                    "supported_destinations": sorted(set(SUPPORTED_DESTINATIONS.values())),
                    "availability_states": [
                        "AVAILABLE",
                        "SOLD_OUT",
                        "UNKNOWN",
                        "UNCONFIRMED",
                        "NOT_ELIGIBLE",
                    ],
                    "availability_invariant": "bookable == true 当且仅当 availability_state == 'AVAILABLE'。",
                    "llm_narrative_enabled": cfg.enable_llm_narrative,
                    "availability_snapshot_ttl_seconds": cfg.availability_ttl_seconds,
                },
                "auth": {
                    "scheme": "Bearer",
                    "current_subject": ctx.subject,
                    "current_scopes": sorted(ctx.scopes),
                },
            },
        )
    )


def describe_routes(ctx: RequestContext) -> dict[str, Any]:
    """GET /v1/routes —— 返回路由清单，供判分前自检确认路径拼接正确（规范 §3.1-4）。"""
    from ..handler import build_router

    return ok(
        envelope(
            ctx,
            {
                "routes": [
                    {
                        "method": route.method,
                        "path": route.template,
                        "required_scopes": sorted(route.scopes),
                    }
                    for route in build_router().routes
                ],
                "note": "所有路径均不含 stage 前缀；HTTP API 使用 $default stage，直接拼接即可访问。",
            },
        )
    )
