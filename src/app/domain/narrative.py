"""行程解释文案（大模型边界层）。

需求文档 §4.4 对大模型的定位：「理解需求、组织候选行程、解释推荐；
**不替代交易事实源**」。本模块是全项目唯一允许接触大模型的地方，
并用两道机制把「模型不得编造动态事实」变成可执行的代码约束：

1. **默认不调用模型**（`ENABLE_LLM_NARRATIVE=false`）。
   模板拼装的文案完全确定，满足规范 §4.5 幂等；判分路径上不引入模型延迟与抖动。

2. 开启模型时走**事实白名单 + 输出校验**：
   提示词只投喂已校验过的结构化事实；生成结果中出现任何
   白名单之外的数字（价格、时间、库存），即判定为幻觉并**丢弃模型输出**、
   回落到模板文案。宁可文案朴素，也不让模型编造动态事实。
"""

from __future__ import annotations

import re
from typing import Any

from ..core import logging as log
from ..core.config import get_config
from .itinerary import ITEM_ATTRACTION, ITEM_MEAL, ITEM_REST, ITEM_TRANSIT, ITEM_WAIT

_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")

# 序号表述（「第 3 天」「第 2 项」）里的数字是排版产物，不是业务事实，可豁免。
# 但豁免必须**限定在序号语境内**：早先的实现是「任何 ≤31 的整数都放过」，
# 那等于给幻觉开了一扇大门 —— 折扣、排队分钟数、人数、小额价格大多落在这个区间，
# 「预计排队 15 分钟」「该商品享 8 折」这类编造会被直接放行。
_ORDINAL_RE = re.compile(r"第\s*\d+\s*(?:天|日|项|步|段|位)")

# 模型只允许「解释」，不允许作出这些承诺。命中即丢弃输出。
_FORBIDDEN_PHRASES = (
    "已锁价",
    "已保留库存",
    "保证可订",
    "一定能订",
    "确保有票",
    "已为您下单",
    "已支付",
    "稳定可订",
)


def build(plan: dict[str, Any], validation: dict[str, Any], mapping: dict[str, Any] | None) -> dict[str, Any]:
    """产出行程解释。返回结构含 `generated_by`，使文案来源可审计。"""
    template_text = _render_template(plan, validation, mapping)
    cfg = get_config()

    if not cfg.enable_llm_narrative:
        return {
            "generated_by": "deterministic_template",
            "text": template_text,
            "fact_source_note": "文案由结构化事实拼装，未经大模型改写；价格与库存均不在文案内断言。",
        }

    allowed_numbers = _collect_allowed_numbers(plan, validation, mapping)
    model_text = _invoke_model(template_text, plan, validation)

    if model_text is None:
        return {
            "generated_by": "deterministic_template",
            "text": template_text,
            "fact_source_note": "大模型调用未成功，已回落到模板文案。",
        }

    violation = _guard(model_text, allowed_numbers)
    if violation is not None:
        log.warn("narrative_guard_rejected", violation=violation)
        return {
            "generated_by": "deterministic_template",
            "text": template_text,
            "fact_source_note": f"大模型输出未通过事实校验（{violation}），已丢弃并回落到模板文案。",
        }

    return {
        "generated_by": "llm_reviewed",
        "text": model_text,
        "fact_source_note": "大模型仅改写表达，输出已通过事实白名单校验；价格与库存仍以供应商查询为准。",
    }


# ---------------------------------------------------------------- 模板


def _render_template(
    plan: dict[str, Any],
    validation: dict[str, Any],
    mapping: dict[str, Any] | None,
) -> str:
    lines: list[str] = []
    summary = plan.get("summary") or {}
    lines.append(
        f"共 {summary.get('total_days', 0)} 天，安排 {summary.get('total_attractions', 0)} 个景点，"
        f"覆盖 {len(summary.get('regions_covered') or [])} 个区域，按区域聚合以减少跨区通勤。"
    )

    for day in plan["days"]:
        header = f"第 {day['day_index']} 天（{day['date']} {day['weekday_label']}）"
        if day.get("primary_region_name"):
            header += f"·{day['primary_region_name']}"
            lines.append(header)
        else:
            lines.append(header + "：当日无可安排景点。")
            continue
        for item in day["items"]:
            if item["type"] == ITEM_ATTRACTION:
                lines.append(
                    f"  {item['start_time']}-{item['end_time']} {item['poi_name']}"
                    f"（{item['duration_minutes']} 分钟）｜推荐依据：{item['recommend_reason']}"
                )
            elif item["type"] == ITEM_TRANSIT:
                lines.append(
                    f"  {item['start_time']}-{item['end_time']} 交通 {item['from_region_name']} → "
                    f"{item['to_region_name']}（{item['duration_minutes']} 分钟）"
                )
            elif item["type"] == ITEM_MEAL:
                lines.append(f"  {item['start_time']}-{item['end_time']} 用餐")
            elif item["type"] == ITEM_REST:
                lines.append(f"  {item['start_time']}-{item['end_time']} 休息缓冲")
            elif item["type"] == ITEM_WAIT:
                lines.append(f"  {item['start_time']}-{item['end_time']} 自由活动（等待开门）")

    counts = validation.get("issue_counts") or {}
    lines.append(
        f"可执行性校验：{'通过' if validation.get('executable') else '未通过'}"
        f"（阻断 {counts.get('blocker', 0)} 项 / 提醒 {counts.get('warning', 0)} 项 / "
        f"待确认 {counts.get('unverified', 0)} 项）。"
    )

    if mapping:
        mapping_summary = mapping.get("summary") or {}
        lines.append(
            f"商品衔接：{mapping_summary.get('attractions_with_bookable_product', 0)}/"
            f"{mapping_summary.get('attraction_count', 0)} 个景点在查询时点有可订商品；"
            f"不可订候选 {mapping_summary.get('candidate_not_bookable', 0)} 条已按实际状态标注。"
        )

    lines.append(
        "说明：可订状态与价格均为查询时点快照，不构成锁价或库存保留；"
        "进入预订时由交易系统重新确认。标记为待确认的事项未经验证，请勿视为已核实。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- 事实白名单


def _collect_allowed_numbers(
    plan: dict[str, Any],
    validation: dict[str, Any],
    mapping: dict[str, Any] | None,
) -> set[str]:
    """收集所有「已校验事实」中出现过的数字串。"""
    allowed: set[str] = set()

    def absorb(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                absorb(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                absorb(item)
        elif isinstance(value, (int, float)):
            allowed.add(str(value))
            allowed.add(str(int(value)) if float(value).is_integer() else str(value))
        elif isinstance(value, str):
            allowed.update(_NUMBER_RE.findall(value))

    absorb(plan)
    absorb(validation)
    if mapping:
        absorb(mapping)
    return allowed


def _guard(text: str, allowed_numbers: set[str]) -> str | None:
    """返回违规描述；`None` 表示通过。"""
    for phrase in _FORBIDDEN_PHRASES:
        if phrase in text:
            return f"出现越权承诺表述「{phrase}」"

    # 先摘掉序号语境，再逐一核对剩余数字。这样「第 3 天」被豁免，
    # 而同样是数字 3 的「排队 3 分钟」仍会被追究。
    scannable = _ORDINAL_RE.sub("第N", text)
    for number in _NUMBER_RE.findall(scannable):
        if number in allowed_numbers:
            continue
        return f"出现事实白名单之外的数字「{number}」"

    return None


# ---------------------------------------------------------------- 模型调用


_PROMPT = """你是旅行行程的解释助手。请把下面的行程事实改写成更顺畅的中文说明。

严格约束：
1. 只允许使用给定事实中已出现的数字，禁止新增或修改任何时间、价格、时长、库存数字。
2. 禁止作出「已锁价」「已保留库存」「保证可订」等承诺。
3. 保留原文中关于「查询时点快照」「待确认」的所有免责与不确定性说明。
4. 不要新增行程项，不要删除任何一天。

行程事实：
{facts}
"""


def _invoke_model(template_text: str, plan: dict[str, Any], validation: dict[str, Any]) -> str | None:
    """调用 Bedrock 改写文案。任何异常都返回 None 交由调用方回落。"""
    cfg = get_config()
    try:
        import boto3
        from botocore.config import Config as BotoConfig

        client = boto3.client(
            "bedrock-runtime",
            region_name=cfg.region,
            config=BotoConfig(retries={"max_attempts": 2, "mode": "standard"}, read_timeout=8),
        )
        response = client.converse(
            modelId=cfg.bedrock_model_id,
            messages=[{"role": "user", "content": [{"text": _PROMPT.format(facts=template_text)}]}],
            inferenceConfig={"maxTokens": 1200, "temperature": 0.0, "topP": 0.9},
        )
        blocks = response["output"]["message"]["content"]
        text = "".join(block.get("text", "") for block in blocks).strip()
        return text or None
    except Exception as exc:  # 模型不可用不应影响主链路
        log.warn("bedrock_invoke_failed", error_type=type(exc).__name__)
        return None
