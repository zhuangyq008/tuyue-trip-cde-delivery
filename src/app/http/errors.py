"""统一错误模型。

对应 CDE 规范 §3.1-3（默认 4xx 响应体字段名不一致会挂测）与
§4.3（404 资源不存在 与 422 参数非法 必须可区分）。

字段名选择说明：顶层使用 `message`，与 API Gateway 自身生成的
`{"message": "Unauthorized"}` / `{"message": "Not Found"}` 保持同名，
使「网关兜底错误」与「Lambda 业务错误」在字段层面完全一致，
判分脚本无需分辨错误由哪一层产生。
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """所有对外错误的基类；handler 顶层统一捕获并序列化。"""

    status_code: int = 500
    error_code: str = "INTERNAL_ERROR"

    def __init__(
        self,
        message: str,
        *,
        errors: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.errors = errors or []
        self.headers = headers or {}

    def to_body(self, request_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "message": self.message,
            "error_code": self.error_code,
            "request_id": request_id,
        }
        if self.errors:
            body["errors"] = self.errors
        return body


class BadRequestError(ApiError):
    """请求无法解析：JSON 语法错误、Content-Type 不符、body 非对象。"""

    status_code = 400
    error_code = "BAD_REQUEST"


class UnauthorizedError(ApiError):
    """缺少令牌或令牌无效。"""

    status_code = 401
    error_code = "UNAUTHORIZED"


class ForbiddenError(ApiError):
    """令牌有效但权限范围不足。"""

    status_code = 403
    error_code = "FORBIDDEN"


class NotFoundError(ApiError):
    """路由不存在，或资源标识不存在。"""

    status_code = 404
    error_code = "NOT_FOUND"


class MethodNotAllowedError(ApiError):
    status_code = 405
    error_code = "METHOD_NOT_ALLOWED"


class ConflictError(ApiError):
    """同一 Idempotency-Key 承载了不同的请求内容。"""

    status_code = 409
    error_code = "IDEMPOTENCY_CONFLICT"


class ValidationError(ApiError):
    """请求可解析，但字段语义非法（类型、范围、枚举、日期逻辑）。

    注意：API Gateway 的请求模型校验只会返回 400，拿不到 422，
    因此本项目的全部字段校验都在 Lambda 内完成（规范 §3.1-2）。
    """

    status_code = 422
    error_code = "VALIDATION_FAILED"


class UpstreamError(ApiError):
    """内部依赖异常；对外不暴露堆栈或依赖细节。"""

    status_code = 502
    error_code = "UPSTREAM_ERROR"


def field_error(field: str, reason: str, expected: str | None = None) -> dict[str, Any]:
    """构造单个字段错误条目。`expected` 描述期望形态，便于调用方一次改对。"""
    item: dict[str, Any] = {"field": field, "reason": reason}
    if expected:
        item["expected"] = expected
    return item
