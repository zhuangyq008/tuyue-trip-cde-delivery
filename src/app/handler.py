"""业务 Lambda 入口（HTTP API payload 2.0，$default 路由全量接管）。

顶层统一兜底所有异常，保证：
  * 响应体字段名恒定（规范 §3.1-3）；
  * 5xx 不泄露堆栈、依赖名或内部路径；
  * 任何未预期异常都记录结构化日志，便于按 request_id 与 CloudWatch 对账。
"""

from __future__ import annotations

import base64
from typing import Any

from .api import availability as availability_api
from .api import bookings as bookings_api
from .api import catalog_api
from .api import events as events_api
from .api import health as health_api
from .api import plans as plans_api
from .api import reference_api
from .api.common import RequestContext
from .core import auth
from .core import logging as log
from .core.config import get_config
from .core.ids import request_id as derive_request_id
from .http.errors import ApiError, ForbiddenError, UnauthorizedError
from .http.responses import json_response
from .http.router import Router
from .http.validation import parse_json_body

_ROUTER: Router | None = None


def build_router() -> Router:
    global _ROUTER
    if _ROUTER is not None:
        return _ROUTER

    router = Router()

    # 运维与自描述
    router.get("/v1/health", health_api.health)
    router.get("/v1/routes", health_api.describe_routes)

    # 行程主链路
    router.post("/v1/plans", plans_api.create_plan, scopes={"write"})
    router.get("/v1/plans/{plan_id}", plans_api.get_plan, scopes={"read"})
    router.post("/v1/plans/{plan_id}/revalidate", plans_api.revalidate_plan, scopes={"read"})

    # 动态可订查询
    router.post("/v1/availability/queries", availability_api.query, scopes={"read"})
    router.get("/v1/products/{product_id}/availability", availability_api.query_single, scopes={"read"})

    # 参考数据（对齐《数据清单-v3.md》）
    router.get("/v1/data-inventory", reference_api.data_inventory, scopes={"read"})
    router.get("/v1/destinations", reference_api.list_destinations, scopes={"read"})
    router.get(
        "/v1/destinations/{destination_id}/seasonality", reference_api.get_seasonality, scopes={"read"}
    )
    router.get("/v1/merchants/{merchant_id}", reference_api.get_merchant, scopes={"read"})
    router.get("/v1/weather", reference_api.get_weather, scopes={"read"})

    # 目录与可信内容
    router.get("/v1/catalog", catalog_api.list_catalog, scopes={"read"})
    router.get("/v1/products/{product_id}", catalog_api.get_product, scopes={"read"})
    router.get("/v1/pois/{poi_id}", catalog_api.get_poi, scopes={"read"})
    router.get("/v1/content", catalog_api.search_content, scopes={"read"})

    # 预订衔接
    router.post("/v1/bookings", bookings_api.create_intent, scopes={"write"})
    router.get("/v1/bookings/{booking_id}", bookings_api.get_intent, scopes={"read"})

    # 漏斗埋点
    router.post("/v1/events", events_api.record, scopes={"write"})
    router.get("/v1/funnel", events_api.funnel_definition, scopes={"read"})

    _ROUTER = router
    return router


def _decode_body(event: dict[str, Any]) -> str | None:
    raw = event.get("body")
    if raw is None:
        return None
    if event.get("isBase64Encoded"):
        try:
            return base64.b64decode(raw).decode("utf-8")
        except Exception:
            # 解码失败按「无法解析的请求体」处理，交由 parse_json_body 给 400。
            return "<undecodable>"
    return raw


def _resolve_auth(event: dict[str, Any]) -> tuple[str, frozenset[str], str]:
    """还原认证结论。

    优先取 Authorizer 透传的 context；context 缺失时（如本地直调）
    回落到自行校验 Authorization 头 —— 纵深防御，不因少一层就变成无鉴权。
    """
    authorizer_ctx = ((event.get("requestContext") or {}).get("authorizer") or {}).get("lambda") or {}
    status = authorizer_ctx.get("auth_status")

    if isinstance(status, str) and status:
        scopes = frozenset(s for s in str(authorizer_ctx.get("scopes", "")).split(",") if s)
        return status, scopes, str(authorizer_ctx.get("subject") or "anonymous")

    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    result = auth.authenticate(headers.get("authorization"))
    return (
        "OK" if result.authenticated else result.reason,
        result.scopes,
        result.subject,
    )


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    req_id = derive_request_id(event)
    http_ctx = (event.get("requestContext") or {}).get("http") or {}
    method = str(http_ctx.get("method") or event.get("httpMethod") or "GET").upper()
    path = str(event.get("rawPath") or http_ctx.get("path") or event.get("path") or "/")

    log.bind_request_context(request_id=req_id, method=method, path=path, stage=get_config().stage)

    try:
        auth_status, scopes, subject = _resolve_auth(event)

        # AUTH_MODE=handler_decide 时，未通过认证的请求会被 Authorizer 放行到这里。
        if auth_status == "MISSING_TOKEN":
            raise UnauthorizedError("缺少 Authorization 请求头，需提供 Bearer 令牌。")
        if auth_status == "INVALID_TOKEN":
            raise UnauthorizedError("Authorization 请求头中的令牌无效。")
        if auth_status != "OK":
            raise ForbiddenError("访问被拒绝。")

        route, path_params = build_router().resolve(method, path)

        if route.scopes and not route.scopes.issubset(scopes):
            missing = sorted(route.scopes - scopes)
            raise ForbiddenError(f"当前令牌缺少所需权限范围：{', '.join(missing)}。")

        body = parse_json_body(_decode_body(event), required=False) if method in {"POST", "PUT", "PATCH"} else {}

        ctx = RequestContext(
            request_id=req_id,
            subject=subject,
            scopes=scopes,
            method=method,
            path=path,
            path_params=path_params,
            query={str(k): str(v) for k, v in (event.get("queryStringParameters") or {}).items()},
            headers={str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()},
            body=body,
        )

        response = route.handler(ctx)
        log.info("request_completed", status_code=response["statusCode"], route=route.template)
        return response

    except ApiError as exc:
        log.info(
            "request_rejected",
            status_code=exc.status_code,
            error_code=exc.error_code,
            field_error_count=len(exc.errors),
        )
        return json_response(exc.status_code, exc.to_body(req_id), headers=exc.headers)

    except Exception as exc:  # 兜底：绝不把堆栈或内部细节返回给调用方
        log.error("unhandled_exception", error_type=type(exc).__name__, exc_info=True)
        return json_response(
            500,
            {
                "message": "服务内部错误，请稍后重试。",
                "error_code": "INTERNAL_ERROR",
                "request_id": req_id,
            },
        )
