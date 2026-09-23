"""需求澄清（槽位填充）。

需求文档 §4.2-1 / §4.3 P0：「缺少关键条件时先追问，避免收集无关个人信息」。

与 422 的边界（全项目统一口径）：
  * 字段**缺失** → 业务态 `needs_clarification`，返回追问清单，HTTP 200。
    缺信息是正常的对话状态，不是客户端的错误。
  * 字段**存在但非法** → HTTP 422，由 Validator 负责。

只追问履约必需的信息。刻意**不收集**姓名、手机号、证件、支付方式等个人信息 ——
原型阶段没有任何环节需要它们（规范 §2）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# 追问顺序即业务重要性顺序：先定目的地与日期，再定人数，最后才是偏好。
_SLOT_QUESTIONS: dict[str, dict[str, str]] = {
    "destination": {
        "question": "这次想去哪个目的地？",
        "why": "目的地决定可用的景点与商品供给范围。",
    },
    "start_date": {
        "question": "计划哪天出发？（格式 YYYY-MM-DD）",
        "why": "开放时间、闭馆日与库存都按具体日期判定，不能用「大概下个月」代替。",
    },
    "days": {
        "question": "计划玩几天？",
        "why": "决定行程天数与每日强度分配。",
    },
    "party.adults": {
        "question": "同行成人几位？",
        "why": "人数影响商品的成行下限、上限与报价。",
    },
    "party.child_ages": {
        "question": "同行儿童分别几岁？",
        "why": "儿童年龄决定适龄限制、儿童票区间与行程强度，仅问年龄不问身份信息。",
    },
}

# 缺失时使用默认值而非追问的偏好类槽位。
PREFERENCE_DEFAULTS: dict[str, Any] = {
    "pace": "standard",
    "interests": [],
    "budget_cny_per_person": None,
    "hotel_region_id": None,
}


@dataclass
class ClarificationResult:
    missing: list[str]
    questions: list[dict[str, str]]
    assumed_defaults: dict[str, Any]

    @property
    def needs_clarification(self) -> bool:
        return bool(self.missing)


def assess(
    *,
    destination: str | None,
    start_date: Any,
    days: int | None,
    adults: int | None,
    children: int | None,
    child_ages: list[int] | None,
    supplied_preferences: dict[str, Any],
) -> ClarificationResult:
    """判定还缺哪些必填槽位。"""
    missing: list[str] = []

    if not destination:
        missing.append("destination")
    if start_date is None:
        missing.append("start_date")
    if days is None:
        missing.append("days")
    if adults is None:
        missing.append("party.adults")

    # 条件必填：声明了儿童人数就必须给出每个孩子的年龄，
    # 否则适龄校验只能靠猜——而猜就是需求文档明令禁止的行为。
    declared_children = children if isinstance(children, int) else (len(child_ages) if child_ages else 0)
    if declared_children > 0 and not child_ages:
        missing.append("party.child_ages")
    elif child_ages and isinstance(children, int) and len(child_ages) != children:
        # 数量对不上属于语义冲突，交由调用方修正；此处不猜哪个字段是对的。
        missing.append("party.child_ages")

    questions = [
        {
            "field": slot,
            "question": _SLOT_QUESTIONS[slot]["question"],
            "why": _SLOT_QUESTIONS[slot]["why"],
        }
        for slot in missing
        if slot in _SLOT_QUESTIONS
    ]

    assumed = {
        key: supplied_preferences.get(key, default)
        for key, default in PREFERENCE_DEFAULTS.items()
    }

    return ClarificationResult(missing=missing, questions=questions, assumed_defaults=assumed)
