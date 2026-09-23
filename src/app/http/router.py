"""极简路径路由。

为什么自己做路由而不用 API Gateway 逐条 route：
规范 §3.1-3/-4 指出网关兜底 4xx 的响应体字段名与 stage 路径前缀是常见挂测点。
把 `$default` 路由全量转交 Lambda 后，404 / 405 的状态码与响应体都由本项目掌控，
不会出现「网关吐出 {"message": "Not Found"} 但字段名与规范不一致」的情况。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from .errors import MethodNotAllowedError, NotFoundError

Handler = Callable[..., dict[str, Any]]

_PARAM_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _compile(template: str) -> re.Pattern[str]:
    pattern = _PARAM_RE.sub(lambda m: f"(?P<{m.group(1)}>[^/]+)", template.rstrip("/"))
    return re.compile(f"^{pattern}/?$")


@dataclass(frozen=True)
class Route:
    method: str
    template: str
    handler: Handler
    pattern: re.Pattern[str]
    # 访问该路由所需的权限范围；空集表示仅需有效令牌。
    scopes: frozenset[str]


class Router:
    def __init__(self) -> None:
        self._routes: list[Route] = []

    def add(self, method: str, template: str, handler: Handler, *, scopes: set[str] | None = None) -> None:
        self._routes.append(
            Route(
                method=method.upper(),
                template=template,
                handler=handler,
                pattern=_compile(template),
                scopes=frozenset(scopes or ()),
            )
        )

    def get(self, template: str, handler: Handler, **kw: Any) -> None:
        self.add("GET", template, handler, **kw)

    def post(self, template: str, handler: Handler, **kw: Any) -> None:
        self.add("POST", template, handler, **kw)

    def resolve(self, method: str, path: str) -> tuple[Route, dict[str, str]]:
        """匹配路由。

        路径存在但方法不符 → 405（并带 Allow 头），而非 404，
        这样「资源不存在」与「方法用错」不会混成同一个码。
        """
        normalised = _normalise_path(path)
        allowed: set[str] = set()
        for route in self._routes:
            match = route.pattern.match(normalised)
            if not match:
                continue
            if route.method == method.upper():
                return route, {k: v for k, v in match.groupdict().items()}
            allowed.add(route.method)
        if allowed:
            raise MethodNotAllowedError(
                f"路径 {normalised} 不支持 {method.upper()} 方法。",
                headers={"Allow": ", ".join(sorted(allowed | {"OPTIONS"}))},
            )
        raise NotFoundError(f"路径不存在：{normalised}")

    @property
    def routes(self) -> list[Route]:
        return list(self._routes)


def _normalise_path(path: str) -> str:
    """去掉 stage 前缀与重复斜杠。

    HTTP API 的 `$default` stage 不带路径前缀，但为兼容以
    `/{stage}` 形式调用（或将来切到 REST API），此处主动剥离一层已知 stage 段。
    """
    cleaned = re.sub(r"/+", "/", path or "/")
    for stage_prefix in ("/demo/", "/prod/", "/default/"):
        if cleaned.startswith(stage_prefix):
            cleaned = cleaned[len(stage_prefix) - 1 :]
            break
    return cleaned.rstrip("/") or "/"
