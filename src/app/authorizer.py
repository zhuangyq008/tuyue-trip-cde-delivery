"""Lambda Authorizer（HTTP API，REQUEST 型，payload 2.0，IAM policy 格式）。

## 401 与 403 如何区分（规范 §3.1-1 点名的挂测点）

本项目使用 HTTP API，其语义与 REST API 不同，这里写清楚实际链路：

| 场景                       | 谁来拒绝                                  | 状态码 |
|----------------------------|-------------------------------------------|--------|
| 完全未带 Authorization 头  | API Gateway（identitySource 缺失，**不调用**本函数） | 401 |
| 带了头但令牌无效           | 本函数返回 Deny 策略                      | 403 |
| 令牌有效但缺少所需 scope   | 业务 Lambda 内判定                        | 403 |
| 令牌有效且 scope 足够      | 本函数返回 Allow                          | 2xx |

两类拒绝落在不同状态码上，不会「都掉进同一个码」。

`AUTH_MODE=handler_decide` 时本函数一律放行、把认证结论透传给业务 Lambda，
由业务侧返回 401/403 并使用统一响应体。留这个开关是为了应对判分反馈
「错误令牌必须返回 401」的情形 —— 改一个环境变量即可，不动代码（规范 §4.6 可反复重交）。

本函数**不打印任何令牌内容**，只记录判定结果（规范 §3）。
"""

from __future__ import annotations

from typing import Any

from .core import auth
from .core import logging as log
from .core.config import get_config

# 授权缓存关闭（模板中 AuthorizerResultTtlInSeconds=0）：
# 判分期间宁可多付一次 Lambda 调用，也不要因缓存命中导致「换了令牌仍被放行」。


def _policy(effect: str, principal_id: str, method_arn: str, context: dict[str, str]) -> dict[str, Any]:
    return {
        "principalId": principal_id,
        "policyDocument": {
            "Version": "2012-10-17",
            "Statement": [{"Action": "execute-api:Invoke", "Effect": effect, "Resource": method_arn}],
        },
        "context": context,
    }


def handler(event: dict[str, Any], _context: Any = None) -> dict[str, Any]:
    cfg = get_config()
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    method_arn = event.get("routeArn") or event.get("methodArn") or "*"

    result = auth.authenticate(headers.get("authorization"))

    request_ctx = (event.get("requestContext") or {}).get("http") or {}
    log.info(
        "authorizer_decision",
        authenticated=result.authenticated,
        subject=result.subject if result.authenticated else "anonymous",
        reason=result.reason,
        method=request_ctx.get("method"),
        path=request_ctx.get("path"),
        auth_mode=cfg.auth_mode,
    )

    context = {
        "auth_status": "OK" if result.authenticated else result.reason,
        "subject": result.subject,
        # context 的值必须是字符串标量，因此 scope 用逗号连接后由业务侧还原。
        "scopes": ",".join(sorted(result.scopes)),
    }

    if result.authenticated:
        return _policy("Allow", result.subject, method_arn, context)

    if cfg.auth_mode == "handler_decide":
        # 放行到业务 Lambda，由其返回 401 与统一响应体。
        return _policy("Allow", "unauthenticated", method_arn, context)

    return _policy("Deny", "unauthenticated", method_arn, context)
