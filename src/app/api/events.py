"""漏斗埋点接口。

需求文档 §1.3 的诊断漏斗：
攻略有效访问 → 商品曝光 → 商品点击 → 可订查询成功 → 发起订单 → 支付成功。

原型只承载**结构定义与埋点契约**，不产出任何业务结论：
基线与转化率需由客户在自有环境内统计（§3.2「客户暂不便提供转化率具体数字」）。

隐私约束（§4.3 P0「埋点支持漏斗分析并满足隐私要求」）：
  * 只接受调用方自带的**匿名** session_id，不下发也不关联用户身份。
  * 白名单字段之外一律拒绝，防止个人信息被顺带塞进埋点。
  * 全部事件带 TTL，原型数据不长期留存。
"""

from __future__ import annotations

from typing import Any

from ..adapters import store
from ..core import logging as log
from ..core.clock import now_iso
from ..core.ids import derive_id
from ..http.responses import created, ok
from ..http.validation import Validator
from .common import RequestContext, envelope, require_scope

# 漏斗步骤定义与顺序。顺序即漏斗层级，用于计算各步流失。
FUNNEL_STEPS: list[str] = [
    "guide_view",           # 攻略有效访问
    "product_impression",   # 商品曝光
    "product_click",        # 商品点击
    "availability_checked", # 可订查询成功
    "booking_intent",       # 发起订单
    "payment_success",      # 支付成功
]

# 事件允许携带的字段白名单。刻意不含任何个人信息字段。
_ALLOWED_EVENT_FIELDS = {"step", "plan_id", "product_id", "poi_id", "availability_state", "occurred_at"}


def record(ctx: RequestContext) -> dict[str, Any]:
    require_scope(ctx, "write")

    v = Validator(ctx.body)
    v.reject_unknown({"session_id", "events"})
    session_id = v.string("session_id", required=True, max_len=64)
    events = v.object_list("events", required=True, min_items=1, max_items=50)

    parsed: list[dict[str, Any]] = []
    for index, raw in enumerate(events or []):
        item_v = Validator(raw, prefix=f"events[{index}].")
        item_v.reject_unknown(_ALLOWED_EVENT_FIELDS)
        step = item_v.string("step", required=True, choices=FUNNEL_STEPS)
        plan_id = item_v.string("plan_id", max_len=64)
        product_id = item_v.string("product_id", max_len=64)
        poi_id = item_v.string("poi_id", max_len=64)
        availability_state = item_v.string("availability_state", max_len=32)
        v.adopt(item_v)
        if step is not None:
            parsed.append(
                {
                    "step": step,
                    "step_index": FUNNEL_STEPS.index(step),
                    "plan_id": plan_id,
                    "product_id": product_id,
                    "poi_id": poi_id,
                    "availability_state": availability_state,
                    "occurred_at": now_iso(),
                }
            )

    v.raise_if_invalid()

    accepted: list[str] = []
    for event in parsed:
        event_id = derive_id("EVT", session_id, event["step"], event.get("product_id"), event.get("plan_id"))
        store.append_event(session_id, event_id, event)
        accepted.append(event_id)

    log.info("funnel_events_recorded", has_session_id=True, count=len(accepted))

    return created(
        envelope(
            ctx,
            {
                "session_id": session_id,
                "accepted_count": len(accepted),
                "event_ids": accepted,
                "privacy_note": "仅记录白名单字段与匿名会话标识；不记录姓名、手机号、证件或支付信息。",
                "analysis_note": "原型只提供埋点契约，不产出转化率结论；基线与指标口径由客户在自有环境统计。",
            },
        )
    )


def funnel_definition(ctx: RequestContext) -> dict[str, Any]:
    """GET /v1/funnel —— 对外公开漏斗口径，便于与客户对齐指标定义。"""
    require_scope(ctx, "read")
    return ok(
        envelope(
            ctx,
            {
                "steps": [
                    {"index": index, "step": step, "label": label}
                    for index, (step, label) in enumerate(
                        zip(
                            FUNNEL_STEPS,
                            [
                                "攻略有效访问",
                                "商品曝光",
                                "商品点击",
                                "可订查询成功",
                                "发起订单",
                                "支付成功",
                            ],
                        )
                    )
                ],
                "primary_metric": {
                    "name": "攻略到支付转化率",
                    "definition": "约定归因窗口内完成相关支付的去重用户数 ÷ 同一口径下的攻略有效访问去重用户数。",
                    "baseline": "待确认 —— 客户尚未提供基线数字，本原型不填写预测值。",
                },
                "guardrail_metrics": [
                    "不可订误推荐率",
                    "行程可执行率",
                    "商品匹配有效率",
                    "退款/取消率",
                    "接口失败率",
                    "单次服务成本",
                ],
                "note": "指标口径与阈值均需与客户业务、数据与安全负责人共同确认后方可使用。",
            },
        )
    )
