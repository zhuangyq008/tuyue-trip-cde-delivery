#!/usr/bin/env python3
"""本地演示脚本：大阪亲子两日游端到端走一遍。

不需要部署、不需要 AWS 凭证 —— 用内存版存储替身直接驱动业务逻辑，
把「需求澄清 → 可信检索 → 行程生成 → 约束校验 → 商品映射 → 动态可订 → 预订衔接」
这条链路的每一步输出打成人能读的形式，用于业务汇报现场演示。

刻意把**不可订的场景也演示出来**：演示只给顺利路径，等于把原型讲成成品。

用法：
    python3 scripts/demo.py                      # 完整演示
    python3 scripts/demo.py --section availability   # 只看某一段
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# 演示固定时钟，保证每次演示输出一致（也便于截图复现）。
os.environ.setdefault("FROZEN_NOW", "2026-11-18T01:00:00Z")
os.environ.setdefault("TABLE_NAME", "demo-inmemory")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

TRIP_START = date(2026, 11, 20)


class InMemoryTable:
    """DynamoDB Table 替身，使演示无需任何 AWS 资源。"""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def put_item(self, Item: dict[str, Any], ConditionExpression: str | None = None) -> dict:  # noqa: N803
        key = (Item["PK"], Item["SK"])
        if ConditionExpression == "attribute_not_exists(PK)" and key in self.items:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = dict(Item)
        return {}

    def get_item(self, Key: dict[str, Any], ConsistentRead: bool = False) -> dict:  # noqa: N803
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item else {}


def title(text: str) -> None:
    print(f"\n{'=' * 78}\n{text}\n{'=' * 78}")


def sub(text: str) -> None:
    print(f"\n--- {text} ---")


def main() -> int:
    parser = argparse.ArgumentParser(description="途悦旅行助手本地演示")
    parser.add_argument(
        "--section",
        choices=["clarify", "content", "plan", "validate", "mapping", "availability", "booking", "all"],
        default="all",
    )
    args = parser.parse_args()
    want = args.section

    from app.adapters import store
    from app.adapters.catalog import get_catalog
    from app.domain import clarify, constraints, content, freshness, mapping, narrative
    from app.domain.availability import PartyRequest, query_product
    from app.domain.itinerary import TripRequest, generate

    store._TABLE = InMemoryTable()  # noqa: SLF001 —— 演示脚本刻意注入替身
    catalog = get_catalog()

    title("途悦旅行 AI 旅行助手 — CDE 验证原型演示")
    print("场景：大阪亲子两日游（2 成人 + 1 个 5 岁儿童），出发 2026-11-20")
    print(f"数据集：{catalog.meta['dataset_version']}（{catalog.meta['data_classification']}，seed={catalog.meta['seed']}）")
    print("提醒：本原型为技术验证，非 production-ready；全部数据为合成 mock 数据。")

    # ---------------- 1. 需求澄清 ----------------
    if want in ("clarify", "all"):
        title("第 1 步｜理解需求：信息不全时先追问，不猜")
        result = clarify.assess(
            destination="大阪",
            start_date=None,
            days=None,
            adults=2,
            children=1,
            child_ages=None,
            supplied_preferences={},
        )
        print(f"缺失槽位：{result.missing}")
        for question in result.questions:
            print(f"  · {question['question']}")
            print(f"    为什么要问：{question['why']}")
        print("\n注意：缺信息返回 HTTP 200 + 追问，不是 422 —— 缺信息是正常对话状态，不是客户端错误。")
        print("注意：只问履约必需信息。全程不收集姓名、证件、手机号与支付方式。")

    request = TripRequest(
        destination="大阪",
        start_date=TRIP_START,
        days=2,
        adults=2,
        child_ages=[5],
        pace="standard",
        hotel_region_id="MOCK_REG_CHUO",
    )

    # ---------------- 2. 可信内容检索 ----------------
    if want in ("content", "all"):
        title("第 2 步｜检索可信内容：每条都带来源与时效，过期与冲突必须暴露")
        found = content.search(catalog, query="亲子 室内", limit=5)
        for item in found["results"]:
            flag = {"fresh": "新鲜", "stale": "已过期", "unknown": "时效未知"}[item["freshness"]]
            print(f"\n  [{item['doc_id']}] {item['title']}")
            print(f"    来源：{item['source']['name']}（{item['source']['type']}）")
            print(f"    更新：{item['updated_at'][:10]}　时效：{flag}（{item['age_days']} 天前）")
            for notice in item.get("notices", []):
                print(f"    ⚠ {notice}")
        print(f"\n来源完整性：全部结果均有来源 = {found['provenance_summary']['all_results_have_source']}")
        print(f"过期/时效未知 {found['provenance_summary']['stale_or_unknown_count']} 条，"
              f"数据冲突 {found['provenance_summary']['conflict_count']} 条")
        print("\n注意：动态的价格与库存不来自这些资料，一律走供应商接口。")

    # ---------------- 3. 行程生成 ----------------
    plan = generate(catalog, request)
    if want in ("plan", "all"):
        title("第 3 步｜生成候选行程：区域聚合 + 计入交通/用餐/休息缓冲")
        for day in plan["days"]:
            weather = day["weather"]
            weather_text = (
                f"{weather['condition_label']}　降雨概率 {weather['precipitation_probability']}%　"
                f"{weather['temp_min_c']}-{weather['temp_max_c']}℃"
                if weather["available"]
                else "天气数据缺失（按未知处理，未做推测填充）"
            )
            sub(f"第 {day['day_index']} 天　{day['date']} {day['weekday_label']}　"
                f"{day['primary_region_name']}　｜　{weather_text}")
            for item in day["items"]:
                if item["type"] == "attraction":
                    print(f"  {item['start_time']}-{item['end_time']}  ★ {item['poi_name']}"
                          f"（{item['duration_minutes']} 分钟，{'室内' if item['is_indoor'] else '户外'}）")
                    print(f"                     推荐依据：{item['recommend_reason']}")
                elif item["type"] == "transit":
                    print(f"  {item['start_time']}-{item['end_time']}  → 交通 {item['from_region_name']} "
                          f"→ {item['to_region_name']}（{item['duration_minutes']} 分钟）")
                elif item["type"] == "meal":
                    print(f"  {item['start_time']}-{item['end_time']}    用餐")
                elif item["type"] == "rest":
                    print(f"  {item['start_time']}-{item['end_time']}    休息（{item['note']}）")
                elif item["type"] == "wait":
                    print(f"  {item['start_time']}-{item['end_time']}    自由活动（{item['note']}）")
            totals = day["totals"]
            print(f"  合计：{totals['attraction_count']} 个景点，交通 {totals['transit_minutes']} 分钟，"
                  f"在外 {totals['active_minutes']} 分钟")
            for note in day["weather_advisories"]:
                print(f"  ⚠ {note}")

        excluded = plan["excluded_by_merchant_status"]
        if excluded:
            sub("因「商户在营状态」硬性约束被排除的景点（数据清单 #2，时效 ≤5min）")
            for entry in excluded:
                print(f"  ✗ {entry['poi_name']}（{entry['region_name']}）")
                print(f"     {entry['reason']}")
            print("\n注意：过滤动作是可见的 —— 悄悄少几个景点，业务方无法判断是没得推还是被规则挡了。")

    # ---------------- 4. 约束校验 ----------------
    validation = constraints.validate(catalog, plan, request)
    if want in ("validate", "all"):
        title("第 4 步｜执行约束校验：生成器不负责证明可行，校验器负责抓问题")
        counts = validation["issue_counts"]
        print(f"结论：{'可执行' if validation['executable'] else '走不通'}　"
              f"｜阻断 {counts['blocker']}　提醒 {counts['warning']}　待确认 {counts['unverified']}")
        print(f"\n{validation['statement']}")
        sub(f"已校验的规则（{len(validation['checked_rules'])} 项）")
        print("  " + "、".join(validation["checked_rules"]))
        if validation["issues"]:
            sub("发现的问题")
            for issue in validation["issues"]:
                mark = {"blocker": "■ 阻断", "warning": "▲ 提醒", "unverified": "? 待确认"}[issue["severity"]]
                print(f"  {mark} [{issue['code']}] {issue['message']}")
                if issue.get("suggestion"):
                    print(f"         建议：{issue['suggestion']}")

    # ---------------- 5. 商品映射 ----------------
    product_mapping = mapping.map_plan_products(catalog, plan, request, check_availability=True)
    if want in ("mapping", "all"):
        title("第 5 步｜匹配实际商品：只走目录显式绑定，不用文本相似度")
        print(f"映射方式：{product_mapping['mapping_method']}　—— {product_mapping['mapping_note']}")
        for entry in product_mapping["entries"]:
            sub(f"{entry['poi_name']}　（{entry['travel_date']}）")
            if entry["candidate_count"] == 0:
                print(f"  {entry['note']}")
                continue
            for candidate in entry["candidates"]:
                mark = "✓ 可订" if candidate["bookable"] else "✗ 不可订"
                print(f"  {mark}  {candidate['product_name']}　[{candidate['availability_state']}]")
                print(f"         原因：{candidate['reason']}")
                binding = candidate["binding"]
                print(f"         绑定：商品 {binding['product_id']}｜日期 {binding['travel_date']}"
                      f"｜{binding['adults']} 成人 + 儿童 {binding['child_ages']}")
        summary = product_mapping["summary"]
        print(f"\n汇总：{summary['attractions_with_bookable_product']}/{summary['attraction_count']} 个景点"
              f"在查询时点有可订商品；候选共 {summary['candidate_total']} 条，"
              f"可订 {summary['candidate_bookable']}，不可订 {summary['candidate_not_bookable']}")
        print(f"状态分布：{summary['state_breakdown']}")

    # ---------------- 6. 五态可订语义 ----------------
    if want in ("availability", "all"):
        title("第 6 步｜动态可订查询：五态语义，只有一种可订")
        print("这一段是整个原型的核心 —— 需求文档把「看中了却订不了」列为已发生过的事故形态。\n")
        party = PartyRequest(adults=2, children=1, child_ages=[5])
        showcase = [
            ("MOCK_PROD_AQUARIUM_TICKET", TRIP_START, "正常可售"),
            ("MOCK_PROD_THEMEPARK_EXPRESS", TRIP_START, "供应商明确售罄"),
            ("MOCK_PROD_KIDS_SCIENCE_TICKET", TRIP_START, "供应商查询超时"),
            ("MOCK_PROD_CASTLE_TICKET", TRIP_START, "供应商返回 5xx"),
            ("MOCK_PROD_SKY_DECK_TICKET", TRIP_START, "供应商 200 但字段不全"),
            ("MOCK_PROD_TRAIN_MUSEUM_TICKET", TRIP_START, "商户在营状态未知"),
            ("MOCK_PROD_NATURE_PARK_TICKET", TRIP_START, "商户状态已过期（>5min）"),
            ("MOCK_PROD_BRICK_CENTER_TICKET", TRIP_START + timedelta(days=-1), "预约提前期不足"),
            ("MOCK_PROD_CITY_PASS_2DAY", TRIP_START, "通票：部分场馆停业但仍可售"),
        ]
        print(f"{'场景':<26}{'状态':<15}{'可订':<7}说明")
        print("-" * 110)
        for product_id, travel_date, label in showcase:
            snapshot = query_product(catalog, product_id, travel_date, party, use_cache=False)
            mark = "是" if snapshot["bookable"] else "否"
            print(f"{label:<26}{snapshot['availability_state']:<15}{mark:<7}{snapshot['reason'][:52]}")
            if snapshot.get("merchant_advisories"):
                for advisory in snapshot["merchant_advisories"]:
                    print(f"{'':<48}⚠ {advisory['detail'][:60]}")
            if snapshot.get("price"):
                price = snapshot["price"]
                print(f"{'':<48}报价 {price['total_price']} {price['currency']}"
                      f"（有效至 {snapshot['expires_at']}）")
        print("-" * 110)
        print("不变式：bookable == true 当且仅当 availability_state == 'AVAILABLE'。")
        print("超时、未知、失败、商户不在营 —— 一律不可订，且不携带报价。")

    # ---------------- 7. 预订衔接 ----------------
    if want in ("booking", "all"):
        title("第 7 步｜引导预订：助手不代付、不锁价、不锁库存")
        from app.api.bookings import _build_intent
        from app.api.common import RequestContext

        ctx = RequestContext(
            request_id="demo",
            subject="demo",
            scopes=frozenset({"read", "write"}),
            method="POST",
            path="/v1/bookings",
            path_params={},
            query={},
            headers={},
            body={},
        )
        party = PartyRequest(adults=2, children=1, child_ages=[5])

        for product_id, label in (
            ("MOCK_PROD_AQUARIUM_TICKET", "可订商品"),
            ("MOCK_PROD_THEMEPARK_EXPRESS", "售罄商品"),
        ):
            intent = _build_intent("MOCK-BKG-DEMO", None, product_id, TRIP_START, party, ctx)
            sub(f"{label}：{intent['binding']['product_name']}")
            print(f"  意图状态：{intent['intent_status']}")
            print(f"  重新确认：{intent['reverification']['note']}")
            print(f"  可订：{intent['bookable']}")
            if intent["checkout"]:
                print(f"  预订入口：{intent['checkout']['handoff_url']}")
                print(f"  跳转后须重新确认：{intent['checkout']['required_reconfirmation']}")
            else:
                print("  预订入口：无 —— 不可订就不提供下单路径")
                print(f"  拒绝原因：{intent['rejection']['message']}")
            print(f"  承诺边界：锁价={intent['guarantees']['price_locked']}　"
                  f"库存保留={intent['guarantees']['inventory_held']}　"
                  f"预订成功={intent['guarantees']['booking_confirmed']}")

    # ---------------- 8. 行程解释与数据口径 ----------------
    if want == "all":
        title("第 8 步｜行程解释：模板拼装，大模型默认不介入")
        narrative_block = narrative.build(plan, validation, product_mapping)
        print(f"生成方式：{narrative_block['generated_by']}")
        print(f"事实来源说明：{narrative_block['fact_source_note']}\n")
        print(narrative_block["text"])

        title("数据清单 v3 覆盖度与时效口径")
        print(f"{'#':<4}{'数据项':<34}{'时效':<16}{'硬约束':<8}最大可接受年龄")
        print("-" * 100)
        for item in freshness.inventory_report():
            hard = "是" if item["hard_constraint"] else ""
            name = item["name"][:30]
            print(f"{item['number']:<4}{name:<34}{item['freshness_tier']:<16}{hard:<8}{item['max_age_seconds']}s")
        print("-" * 100)
        print("硬性约束项（#2 商户在营状态、#4 库存）过期或未知 → 对象不可用；")
        print("其余数据项过期 → 标注「待确认」并对外明示，但不阻断行程。")

        title("演示结束")
        print("已验证：可信内容有来源、行程约束可校验、商品映射可追溯、不可订不会被展示成可订、")
        print("        预订衔接不越权承诺、数据时效按清单要求判定。")
        print("未验证：真实供应商接口行为、真实转化率影响、生产环境表现。")
        print("这些需要客户提供接口文档与漏斗数据后另行确认，原型结果不等于商业收益。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
