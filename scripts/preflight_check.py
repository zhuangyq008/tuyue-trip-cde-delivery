#!/usr/bin/env python3
"""提交前自检（逐条对应《CDE-非功能交付规范》§5 清单）。

对**已部署的公网端点**实测，不是单元测试的替代品：
单元测试证明代码逻辑对，本脚本证明「这个 URL + 这个令牌，从外网真的能按规范工作」。

任何一条 FAIL 都不应提交。WARN 表示需要人工确认。

用法：
    python3 scripts/preflight_check.py --url https://xxx.execute-api.us-east-1.amazonaws.com \\
        --token <完整权限令牌> [--readonly-token <只读令牌>]
"""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

TIMEOUT = 20

# 平台 SSRF 防护会拦截的地址（规范 §4.1）。
BLOCKED_HOST_MARKERS = (
    "127.0.0.1",
    "localhost",
    "169.254.",
    "192.168.",
    "10.",
    "172.16.",
    "172.17.",
    "172.18.",
    "172.19.",
    "172.2",
    "172.30.",
    "172.31.",
)


@dataclass
class Report:
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    warned: list[str] = field(default_factory=list)

    def ok(self, message: str) -> None:
        self.passed.append(message)
        print(f"[PASS] {message}")

    def fail(self, message: str) -> None:
        self.failed.append(message)
        print(f"[FAIL] {message}")

    def warn(self, message: str) -> None:
        self.warned.append(message)
        print(f"[WARN] {message}")

    def check(self, condition: bool, message: str) -> bool:
        (self.ok if condition else self.fail)(message)
        return condition


def request(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], Any]:
    """发起 HTTP 请求。4xx/5xx 不抛异常，按响应返回，便于断言状态码。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    all_headers = {"Content-Type": "application/json", **(headers or {})}
    if token is not None:
        all_headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=data, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            raw = response.read().decode("utf-8")
            return response.status, dict(response.headers), _maybe_json(raw)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return exc.code, dict(exc.headers or {}), _maybe_json(raw)


def _maybe_json(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def main() -> int:
    parser = argparse.ArgumentParser(description="CDE 提交前自检")
    parser.add_argument("--url", required=True, help="API 基地址，不含尾部斜杠")
    parser.add_argument("--token", required=True, help="完整权限令牌")
    parser.add_argument("--readonly-token", default=None, help="只读令牌（用于验证 403）")
    args = parser.parse_args()

    base = args.url.rstrip("/")
    r = Report()

    print("=" * 72)
    print("CDE 提交前自检 —— 对应《CDE-非功能交付规范》§5")
    print("=" * 72)

    # ---------------- §4.1 网络可达性 ----------------
    print("\n--- §4.1 网络可达性 ---")
    parsed = urllib.parse.urlparse(base)
    r.check(parsed.scheme == "https", "使用 HTTPS 协议")
    host = parsed.hostname or ""
    r.check(
        not any(host.startswith(marker) for marker in BLOCKED_HOST_MARKERS),
        f"主机名不属于内网/环回地址段（{host}）",
    )
    r.check(
        parsed.path in ("", "/"),
        f"基地址不含 stage 路径前缀，可直接拼接规范路径（当前 path={parsed.path or '/'}）",
    )

    # ---------------- §4.2 认证 ----------------
    print("\n--- §4.2 认证（判分必测项）---")
    status, _, body = request(f"{base}/v1/health")
    r.check(status in (401, 403), f"未带令牌被拒绝，返回 {status}（不得为 200）")
    r.check(status != 200, "未带令牌时没有返回 200 携错误体")

    status_bad, _, body_bad = request(f"{base}/v1/health", token="MOCK_DEFINITELY_WRONG_TOKEN")
    r.check(status_bad in (401, 403), f"错误令牌被拒绝，返回 {status_bad}")
    if isinstance(body_bad, dict):
        r.check("message" in body_bad, "拒绝响应体包含 message 字段")
        r.check(
            "MOCK_DEFINITELY_WRONG_TOKEN" not in json.dumps(body_bad, ensure_ascii=False),
            "拒绝响应体未回显令牌内容",
        )

    status_ok, headers_ok, body_ok = request(f"{base}/v1/health", token=args.token)
    if not r.check(status_ok == 200, f"正确令牌可正常访问（返回 {status_ok}）"):
        print("\n[ABORT] 正确令牌无法访问，后续用例无法执行。")
        return _summarise(r)

    r.check(
        headers_ok.get("Content-Type", "").startswith("application/json"),
        "Content-Type 为 application/json",
    )
    r.check(isinstance(body_ok, dict), "响应体为合法 JSON 对象")

    if args.readonly_token:
        status_ro, _, _ = request(
            f"{base}/v1/plans",
            method="POST",
            token=args.readonly_token,
            body={"destination": "大阪"},
        )
        r.check(status_ro == 403, f"只读令牌执行写操作返回 403（实际 {status_ro}）")
        # 规范 §3.1-1 的要求是「别让两种拒绝都落到同一个码」，
        # 而不是要求三种拒绝各用一个码。所以这里检查的是：
        # 未带令牌与权限不足必须可区分（401 vs 403），
        # 且全部拒绝码构成的集合不止一个元素。
        codes = {status, status_bad, status_ro}
        r.check(status != status_ro, f"未带令牌（{status}）与权限不足（{status_ro}）状态码可区分")
        r.check(len(codes) >= 2, f"拒绝状态码不止一种（实际 {sorted(codes)}）")
        r.check(status == 401, f"未带令牌返回 401（实际 {status}）")
    else:
        r.warn("未提供 --readonly-token，跳过 403 权限范围用例")

    # ---------------- §4.3 接口契约 ----------------
    print("\n--- §4.3 接口契约一致性 ---")
    status_404, _, body_404 = request(f"{base}/v1/no-such-route", token=args.token)
    r.check(status_404 == 404, f"不存在的路径返回 404（实际 {status_404}）")
    if isinstance(body_404, dict):
        r.check(
            {"message", "error_code", "request_id"} <= set(body_404),
            "404 响应体字段名与业务错误一致（message/error_code/request_id）",
        )

    status_missing, _, _ = request(f"{base}/v1/products/MOCK_PROD_DOES_NOT_EXIST", token=args.token)
    r.check(status_missing == 404, f"不存在的资源返回 404（实际 {status_missing}）")

    start = (date.today() + timedelta(days=30)).isoformat()
    status_422, _, body_422 = request(
        f"{base}/v1/plans",
        method="POST",
        token=args.token,
        body={
            "destination": "大阪",
            "start_date": start,
            "days": "两天",
            "party": {"adults": 2, "children": 0},
        },
    )
    r.check(status_422 == 422, f"参数类型非法返回 422（实际 {status_422}）")
    if isinstance(body_422, dict):
        r.check(bool(body_422.get("errors")), "422 响应体逐字段列出错误原因")
    r.check(status_missing != status_422, "404（资源不存在）与 422（参数非法）可区分")

    status_405, _, _ = request(f"{base}/v1/bookings", token=args.token)
    r.check(status_405 == 405, f"方法不支持返回 405（实际 {status_405}）")

    # ---------------- 业务不变式 ----------------
    print("\n--- 业务不变式：不可订不得展示为可订 ---")
    status_plan, _, plan = request(
        f"{base}/v1/plans",
        method="POST",
        token=args.token,
        body={
            "destination": "大阪",
            "start_date": start,
            "days": 2,
            "party": {"adults": 2, "children": 1, "child_ages": [5]},
            "options": {"check_availability": True},
        },
    )
    if r.check(status_plan == 201, f"行程创建返回 201（实际 {status_plan}）") and isinstance(plan, dict):
        candidates = [
            candidate
            for entry in plan.get("product_mapping", {}).get("entries", [])
            for candidate in entry.get("candidates", [])
        ]
        r.check(bool(candidates), "行程包含商品映射候选")
        violations = [
            c
            for c in candidates
            if c.get("bookable") is not (c.get("availability_state") == "AVAILABLE")
        ]
        r.check(not violations, f"可订不变式成立（{len(candidates)} 条候选全部一致）")
        priced_but_unbookable = [
            c for c in candidates if not c.get("bookable") and (c.get("availability") or {}).get("price")
        ]
        r.check(not priced_but_unbookable, "不可订候选未携带报价")
        r.check(
            plan.get("booking_handoff", {}).get("constraints"),
            "预订衔接明确声明不代下单、不锁价",
        )
        r.check(
            "CDE 验证原型" in plan.get("delivery_notice", {}).get("maturity", ""),
            "响应内嵌交付边界声明",
        )

        plan_id = plan.get("plan_id", "")
        r.check(str(plan_id).startswith("MOCK-PLAN-"), f"资源标识带 MOCK 前缀（{plan_id}）")

        # ---------------- §4.5 幂等 ----------------
        print("\n--- §4.5 幂等（用例会重跑）---")
        status_replay, _, replay = request(
            f"{base}/v1/plans",
            method="POST",
            token=args.token,
            body={
                "destination": "大阪",
                "start_date": start,
                "days": 2,
                "party": {"adults": 2, "children": 1, "child_ages": [5]},
                "options": {"check_availability": True},
            },
        )
        r.check(status_replay == status_plan, f"重复提交状态码一致（{status_plan} → {status_replay}）")
        if isinstance(replay, dict):
            r.check(replay.get("plan_id") == plan_id, "重复提交返回同一 plan_id")
            r.check(
                replay.get("itinerary") == plan.get("itinerary"),
                "重复提交返回完全相同的行程内容",
            )

        status_get, _, fetched = request(f"{base}/v1/plans/{plan_id}", token=args.token)
        r.check(status_get == 200, f"按 ID 读取行程返回 200（实际 {status_get}）")

    # ---------------- 数据合规 ----------------
    print("\n--- §2 数据合规（全 Mock）---")
    status_catalog, _, catalog = request(f"{base}/v1/catalog", token=args.token)
    if r.check(status_catalog == 200, "目录接口可访问") and isinstance(catalog, dict):
        meta = catalog.get("dataset_meta", {})
        r.check(
            meta.get("data_classification") == "SYNTHETIC_MOCK_ONLY",
            "数据集标记为 SYNTHETIC_MOCK_ONLY",
        )
        r.check(meta.get("seed") is not None, "数据集声明固定 seed（可复现）")
        blob = json.dumps(catalog, ensure_ascii=False)
        r.check("MOCK_" in blob, "目录数据带 MOCK_ 前缀，假值一眼可辨")
        for leak in ("@gmail.", "@qq.com", "138001", "身份证"):
            r.check(leak not in blob, f"目录数据不含疑似真实信息片段（{leak}）")

    status_inv, _, inventory = request(f"{base}/v1/data-inventory", token=args.token)
    if r.check(status_inv == 200, "数据清单覆盖度接口可访问") and isinstance(inventory, dict):
        r.check(len(inventory.get("items", [])) == 9, "覆盖《数据清单-v3.md》全部 9 项")
        hard = {item["number"] for item in inventory.get("items", []) if item.get("hard_constraint")}
        r.check(hard == {2, 4}, f"硬性约束项标记正确（{sorted(hard)}）")

    # ---------------- 稳定性 ----------------
    print("\n--- §4.5 可靠性与时序 ---")
    latencies: list[float] = []
    codes: list[int] = []
    for _ in range(5):
        started = time.perf_counter()
        code, _, _ = request(f"{base}/v1/health", token=args.token)
        latencies.append(time.perf_counter() - started)
        codes.append(code)
    r.check(all(code == 200 for code in codes), f"连续 5 次探活全部成功（{codes}）")
    slowest = max(latencies)
    r.check(slowest < 10.0, f"最慢一次响应 {slowest:.2f}s，在判分超时预算内")
    if slowest > 3.0:
        r.warn(f"最慢响应 {slowest:.2f}s 偏高，可能是冷启动；建议先预热再提交")

    return _summarise(r)


def _summarise(r: Report) -> int:
    print("\n" + "=" * 72)
    print(f"通过 {len(r.passed)} 项 / 失败 {len(r.failed)} 项 / 待确认 {len(r.warned)} 项")
    if r.failed:
        print("\n未通过项：")
        for item in r.failed:
            print(f"  - {item}")
        print("\n结论：不满足提交条件，请先修复上述项。")
        return 1
    print("\n结论：§5 自检清单全部通过，可提交。")
    print("提交格式：")
    print("  URL: <本次自检使用的基地址>")
    print("  TOKEN: <完整权限令牌>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
