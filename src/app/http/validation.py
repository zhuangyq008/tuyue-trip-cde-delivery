"""Lambda 内字段校验器（产出 422）。

为什么全部校验都在 Lambda 内做：API Gateway 的请求模型校验只会返回 400，
拿不到 422（规范 §3.1-2 点名的挂测点）。

默认语义：
  * 字段 **缺失**     → 422 `VALIDATION_FAILED`
  * 字段 **存在但非法** → 422 `VALIDATION_FAILED`，逐字段给出原因
  * 字段 **未定义**    → 422（`reject_unknown`，避免拼错字段名被静默忽略）

唯一的例外是 `POST /v1/plans`：那里必填字段缺失要走「需求澄清」返回 200 追问
（需求文档 §4.3 P0「缺少关键条件时先追问」），因此该入口显式传
`missing_as_error=False`，把缺失项交给 `domain/clarify.py` 处理。

校验器收集全部错误后一次性抛出，避免调用方需要多轮往返才能改对请求。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any, Iterable, Mapping, Sequence

from ..core.clock import hhmm_to_minutes, parse_date
from .errors import BadRequestError, ValidationError, field_error

_MAX_BODY_BYTES = 256 * 1024
_MISSING = object()


def parse_json_body(raw_body: str | None, *, required: bool) -> dict[str, Any]:
    """解析请求体。语法问题 → 400；语义问题留给 Validator → 422。"""
    if raw_body is None or raw_body.strip() == "":
        if required:
            raise BadRequestError("请求体不能为空，需提供 JSON 对象。")
        return {}
    if len(raw_body.encode("utf-8")) > _MAX_BODY_BYTES:
        raise BadRequestError("请求体超过 256KB 上限。")
    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise BadRequestError(f"请求体不是合法 JSON：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）") from exc
    if not isinstance(parsed, dict):
        raise BadRequestError("请求体必须是 JSON 对象，不接受数组或标量。")
    return parsed


class Validator:
    """累积式字段校验器。

    `missing_as_error` 控制「必填字段缺失」的归属，这是本类唯一需要调用方决策的语义：

    * `True`（默认）—— 缺失即记入 `errors`，`raise_if_invalid()` 抛 422。
      绝大多数接口要的是这个行为，因此把它设为**默认**。
    * `False` —— 缺失只记入 `missing`，由调用方转成业务态。
      仅 `POST /v1/plans` 使用：那里缺信息要走「需求澄清」返回 200 追问，
      而不是报错（需求文档 §4.3 P0）。

    为什么默认值这样选：早先的实现让 `required=True` 只写 `missing`，
    要求每个调用方自己补一句「若为 None 则 append 到 errors」。
    结果 `POST /v1/availability/queries` 与 `POST /v1/events` 都漏了这一句，
    整个必填数组缺失时被当成空数组静默放过（返回 200/201 而不是 422）。
    安全的行为必须是默认行为，特例才需要显式声明。
    """

    def __init__(
        self,
        payload: Mapping[str, Any],
        *,
        prefix: str = "",
        missing_as_error: bool = True,
    ) -> None:
        self._payload = payload
        self._prefix = prefix
        self._missing_as_error = missing_as_error
        self.errors: list[dict[str, Any]] = []
        self.missing: list[str] = []

    # ---------- 内部工具 ----------

    def _path(self, field: str) -> str:
        return f"{self._prefix}{field}"

    def _get(self, field: str) -> Any:
        return self._payload.get(field, _MISSING)

    def _fail(self, field: str, reason: str, expected: str | None = None) -> None:
        self.errors.append(field_error(self._path(field), reason, expected=expected))

    def _absent(self, field: str, required: bool, expected: str | None = None) -> None:
        if not required:
            return
        self.missing.append(self._path(field))
        if self._missing_as_error:
            self._fail(field, "缺少必填字段", expected)

    # ---------- 标量 ----------

    def string(
        self,
        field: str,
        *,
        required: bool = False,
        min_len: int = 1,
        max_len: int = 200,
        choices: Sequence[str] | None = None,
        default: str | None = None,
    ) -> str | None:
        value = self._get(field)
        if value is _MISSING or value is None:
            self._absent(field, required, "string")
            return default
        if not isinstance(value, str):
            self._fail(field, f"类型应为字符串，实际为 {type(value).__name__}", "string")
            return default
        stripped = value.strip()
        if len(stripped) < min_len:
            self._fail(field, f"长度不足，至少 {min_len} 个字符", f"minLength={min_len}")
            return default
        if len(stripped) > max_len:
            self._fail(field, f"长度超限，最多 {max_len} 个字符", f"maxLength={max_len}")
            return default
        if choices is not None and stripped not in choices:
            self._fail(field, f"取值不在允许范围内：{stripped}", "|".join(choices))
            return default
        return stripped

    def integer(
        self,
        field: str,
        *,
        required: bool = False,
        minimum: int | None = None,
        maximum: int | None = None,
        default: int | None = None,
    ) -> int | None:
        value = self._get(field)
        if value is _MISSING or value is None:
            self._absent(field, required, "integer")
            return default
        # 显式拒绝 bool：Python 中 bool 是 int 子类，容易漏过。
        if isinstance(value, bool) or not isinstance(value, int):
            self._fail(field, f"类型应为整数，实际为 {type(value).__name__}", "integer")
            return default
        if minimum is not None and value < minimum:
            self._fail(field, f"小于下限 {minimum}", f"minimum={minimum}")
            return default
        if maximum is not None and value > maximum:
            self._fail(field, f"超过上限 {maximum}", f"maximum={maximum}")
            return default
        return value

    def boolean(self, field: str, *, required: bool = False, default: bool | None = None) -> bool | None:
        value = self._get(field)
        if value is _MISSING or value is None:
            self._absent(field, required, "boolean")
            return default
        if not isinstance(value, bool):
            self._fail(field, f"类型应为布尔值，实际为 {type(value).__name__}", "boolean")
            return default
        return value

    def date_field(
        self,
        field: str,
        *,
        required: bool = False,
        not_before: date | None = None,
        not_after: date | None = None,
    ) -> date | None:
        value = self._get(field)
        if value is _MISSING or value is None:
            self._absent(field, required, "YYYY-MM-DD")
            return None
        if not isinstance(value, str):
            self._fail(field, f"类型应为字符串日期，实际为 {type(value).__name__}", "YYYY-MM-DD")
            return None
        parsed = parse_date(value)
        if parsed is None:
            self._fail(field, f"不是合法日期：{value}", "YYYY-MM-DD")
            return None
        if not_before is not None and parsed < not_before:
            self._fail(field, f"早于允许的最早日期 {not_before.isoformat()}", f"minDate={not_before.isoformat()}")
            return None
        if not_after is not None and parsed > not_after:
            self._fail(field, f"晚于允许的最晚日期 {not_after.isoformat()}", f"maxDate={not_after.isoformat()}")
            return None
        return parsed

    def time_field(self, field: str, *, required: bool = False) -> int | None:
        """HH:MM → 当日分钟数。"""
        value = self._get(field)
        if value is _MISSING or value is None:
            self._absent(field, required, "HH:MM")
            return None
        if not isinstance(value, str) or hhmm_to_minutes(value) is None:
            self._fail(field, f"不是合法时间：{value!r}", "HH:MM (24 小时制)")
            return None
        return hhmm_to_minutes(value)

    # ---------- 复合 ----------

    def int_list(
        self,
        field: str,
        *,
        required: bool = False,
        minimum: int | None = None,
        maximum: int | None = None,
        max_items: int = 20,
    ) -> list[int] | None:
        value = self._get(field)
        if value is _MISSING or value is None:
            self._absent(field, required, "array<integer>")
            return None
        if not isinstance(value, list):
            self._fail(field, f"类型应为数组，实际为 {type(value).__name__}", "array<integer>")
            return None
        if len(value) > max_items:
            self._fail(field, f"元素过多，最多 {max_items} 项", f"maxItems={max_items}")
            return None
        result: list[int] = []
        for index, item in enumerate(value):
            if isinstance(item, bool) or not isinstance(item, int):
                self._fail(f"{field}[{index}]", f"类型应为整数，实际为 {type(item).__name__}", "integer")
                continue
            if minimum is not None and item < minimum:
                self._fail(f"{field}[{index}]", f"小于下限 {minimum}", f"minimum={minimum}")
                continue
            if maximum is not None and item > maximum:
                self._fail(f"{field}[{index}]", f"超过上限 {maximum}", f"maximum={maximum}")
                continue
            result.append(item)
        return result

    def string_list(
        self,
        field: str,
        *,
        required: bool = False,
        max_items: int = 20,
        max_len: int = 64,
    ) -> list[str] | None:
        value = self._get(field)
        if value is _MISSING or value is None:
            self._absent(field, required, "array<string>")
            return None
        if not isinstance(value, list):
            self._fail(field, f"类型应为数组，实际为 {type(value).__name__}", "array<string>")
            return None
        if len(value) > max_items:
            self._fail(field, f"元素过多，最多 {max_items} 项", f"maxItems={max_items}")
            return None
        result: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip():
                self._fail(f"{field}[{index}]", "元素应为非空字符串", "string")
                continue
            if len(item.strip()) > max_len:
                self._fail(f"{field}[{index}]", f"长度超限，最多 {max_len} 个字符", f"maxLength={max_len}")
                continue
            result.append(item.strip())
        return result

    def object_list(
        self,
        field: str,
        *,
        required: bool = False,
        min_items: int = 1,
        max_items: int = 50,
    ) -> list[Mapping[str, Any]] | None:
        value = self._get(field)
        if value is _MISSING or value is None:
            self._absent(field, required, "array<object>")
            return None
        if not isinstance(value, list):
            self._fail(field, f"类型应为数组，实际为 {type(value).__name__}", "array<object>")
            return None
        if len(value) < min_items:
            self._fail(field, f"元素过少，至少 {min_items} 项", f"minItems={min_items}")
            return None
        if len(value) > max_items:
            self._fail(field, f"元素过多，最多 {max_items} 项", f"maxItems={max_items}")
            return None
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                self._fail(f"{field}[{index}]", f"类型应为对象，实际为 {type(item).__name__}", "object")
        return [item for item in value if isinstance(item, dict)]

    def nested(self, field: str) -> "Validator":
        """返回子对象校验器；子对象缺失时返回空校验器（按缺失处理，不报错）。"""
        value = self._get(field)
        child_prefix = f"{self._path(field)}."
        if value is _MISSING or value is None:
            return Validator({}, prefix=child_prefix, missing_as_error=self._missing_as_error)
        if not isinstance(value, dict):
            self._fail(field, f"类型应为对象，实际为 {type(value).__name__}", "object")
            return Validator({}, prefix=child_prefix, missing_as_error=self._missing_as_error)
        return Validator(value, prefix=child_prefix, missing_as_error=self._missing_as_error)

    def adopt(self, child: "Validator") -> None:
        """合并子校验器的错误与缺失项。"""
        self.errors.extend(child.errors)
        self.missing.extend(child.missing)

    def reject_unknown(self, allowed: Iterable[str]) -> None:
        """拒绝未声明字段，避免调用方拼错字段名却被静默忽略。"""
        allowed_set = set(allowed)
        for key in self._payload:
            if key not in allowed_set:
                self._fail(key, "未定义的字段", f"allowed={','.join(sorted(allowed_set))}")

    # ---------- 收口 ----------

    def raise_if_invalid(self) -> None:
        if self.errors:
            raise ValidationError("请求参数校验未通过。", errors=self.errors)
