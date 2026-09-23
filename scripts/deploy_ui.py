#!/usr/bin/env python3
"""部署演示界面（CloudFront + S3）。

一条命令走完：解析 API 域名 → 部署/更新 UI 栈 → 上传页面 → 失效缓存 → 打印访问地址。

本脚本**不接触任何令牌**。令牌由使用者在页面里输入，
既不写入 CloudFront 配置，也不写入 CloudFormation 模板或本仓库。

用法：
    python3 scripts/deploy_ui.py                 # 部署或更新
    python3 scripts/deploy_ui.py --sync-only     # 只重传页面并失效缓存（改了 HTML 后用）
    python3 scripts/deploy_ui.py --delete        # 删除 UI 栈（演示结束）
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError, WaiterError

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "template-ui.yaml"
UI_FILE = ROOT / "web" / "index.html"


def stack_outputs(cfn, stack_name: str) -> dict[str, str]:
    try:
        stacks = cfn.describe_stacks(StackName=stack_name)["Stacks"]
    except ClientError:
        return {}
    return {o["OutputKey"]: o["OutputValue"] for o in (stacks[0].get("Outputs") or [])}


def resolve_api_domain(cfn, api_stack: str) -> str:
    outputs = stack_outputs(cfn, api_stack)
    base = outputs.get("ApiBaseUrl", "")
    if not base:
        print(f"[FAIL] 栈 {api_stack} 没有 ApiBaseUrl 输出，请先部署 CDE 交付栈。", file=sys.stderr)
        raise SystemExit(1)
    # 只取主机名：CloudFront 的 Origin DomainName 不接受协议与路径。
    return base.replace("https://", "").replace("http://", "").rstrip("/")


def deploy_stack(cfn, stack_name: str, api_domain: str, price_class: str) -> None:
    body = TEMPLATE.read_text(encoding="utf-8")
    params = [
        {"ParameterKey": "ApiDomainName", "ParameterValue": api_domain},
        {"ParameterKey": "PriceClass", "ParameterValue": price_class},
    ]

    exists = bool(stack_outputs(cfn, stack_name)) or _stack_exists(cfn, stack_name)
    action = "更新" if exists else "创建"
    print(f"[..] {action}栈 {stack_name}（CloudFront 首次创建通常需要 10–20 分钟）")

    try:
        if exists:
            cfn.update_stack(StackName=stack_name, TemplateBody=body, Parameters=params)
            waiter = cfn.get_waiter("stack_update_complete")
        else:
            cfn.create_stack(
                StackName=stack_name,
                TemplateBody=body,
                Parameters=params,
                OnFailure="DELETE",
                Tags=[{"Key": "Project", "Value": "yuetu-trip-cde"}],
            )
            waiter = cfn.get_waiter("stack_create_complete")
    except ClientError as exc:
        if "No updates are to be performed" in str(exc):
            print("[OK] 栈无变更。")
            return
        print(f"[FAIL] {action}栈失败：{exc}", file=sys.stderr)
        raise SystemExit(1)

    try:
        waiter.wait(StackName=stack_name, WaiterConfig={"Delay": 15, "MaxAttempts": 160})
    except WaiterError:
        print(f"[FAIL] 栈 {action}未成功，请查看 CloudFormation 事件。", file=sys.stderr)
        raise SystemExit(1)
    print(f"[OK] 栈{action}完成。")


def _stack_exists(cfn, stack_name: str) -> bool:
    try:
        status = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
        return not status.startswith("DELETE_COMPLETE")
    except ClientError:
        return False


def sync_ui(s3, bucket: str) -> None:
    if not UI_FILE.exists():
        print(f"[FAIL] 界面文件缺失：{UI_FILE}", file=sys.stderr)
        raise SystemExit(1)
    s3.put_object(
        Bucket=bucket,
        Key="index.html",
        Body=UI_FILE.read_bytes(),
        ContentType="text/html; charset=utf-8",
        # 页面本身不缓存，保证改完立刻能看到；API 行为已单独禁用缓存。
        CacheControl="no-cache, must-revalidate",
    )
    size_kb = UI_FILE.stat().st_size / 1024
    print(f"[OK] 已上传 index.html（{size_kb:.1f} KB）到 s3://{bucket}/")


def invalidate(cloudfront, distribution_id: str) -> None:
    response = cloudfront.create_invalidation(
        DistributionId=distribution_id,
        InvalidationBatch={
            "Paths": {"Quantity": 1, "Items": ["/*"]},
            "CallerReference": f"deploy-ui-{int(time.time())}",
        },
    )
    print(f"[OK] 已发起缓存失效 {response['Invalidation']['Id']}（通常 1–2 分钟生效）")


def delete_stack(cfn, s3, stack_name: str) -> None:
    outputs = stack_outputs(cfn, stack_name)
    bucket = outputs.get("UiBucketName")
    if bucket:
        # S3 桶必须先清空才能随栈删除。
        print(f"[..] 清空桶 {bucket}")
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})
    print(f"[..] 删除栈 {stack_name}（CloudFront 分配需先禁用，约 10–15 分钟）")
    cfn.delete_stack(StackName=stack_name)
    try:
        cfn.get_waiter("stack_delete_complete").wait(
            StackName=stack_name, WaiterConfig={"Delay": 15, "MaxAttempts": 160}
        )
        print("[OK] UI 栈已删除。CDE 交付栈未受影响。")
    except WaiterError:
        print("[WARN] 删除仍在进行，请稍后在 CloudFormation 控制台确认。", file=sys.stderr)


def verify(ui_url: str) -> bool:
    """部署后校验：确认 CloudFront 没有篡改 API 的状态码或响应体。

    这组断言是一次真实事故留下的。最初给分配配了
    「403/404 → 200 + /index.html」做单页回落，而 `CustomErrorResponses`
    是分配级的、无法按行为收窄 —— 于是 `/v1/*` 的 403/404 也被改写成 200 携 HTML。
    API 本身完全正确，是边缘配置把它的状态码吃掉了。

    这类缺陷单元测试抓不到（代码没问题），直连 API 自检也抓不到（不经过 CloudFront），
    只有在这一层验证才能发现。
    """
    import urllib.error
    import urllib.request

    def probe(path: str, headers: dict[str, str] | None = None) -> tuple[int, str]:
        request = urllib.request.Request(ui_url + path, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, response.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            return exc.code, (exc.headers or {}).get("Content-Type", "")
        except Exception:
            return 0, ""

    print("\n[..] 部署后校验")
    checks: list[tuple[str, bool, str]] = []

    status, ctype = probe("/")
    checks.append(("界面可访问且为 HTML", status == 200 and "html" in ctype, f"HTTP {status} {ctype}"))

    # 核心断言：未认证的 API 请求绝不能返回 200。
    status, ctype = probe("/v1/health")
    checks.append(("未带令牌的 /v1/health 不是 200", status != 200, f"HTTP {status}"))
    checks.append(("未带令牌的 /v1/health 返回 401", status == 401, f"HTTP {status}"))
    checks.append(("API 错误响应是 JSON 而非 HTML", "html" not in ctype.lower(), ctype or "(无)"))

    status, _ = probe("/v1/health", {"Authorization": "Bearer MOCK_INVALID_TOKEN"})
    checks.append(("错误令牌被拒绝", status in (401, 403), f"HTTP {status}"))

    status, ctype = probe("/v1/definitely-not-a-route", {"Authorization": "Bearer MOCK_INVALID_TOKEN"})
    checks.append(("未知 API 路径不被改写成 200", status != 200, f"HTTP {status}"))

    ok = True
    for name, passed, detail in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name} — {detail}")
        ok = ok and passed

    if not ok:
        print("\n[FAIL] CloudFront 层校验未通过：状态码或响应体被边缘配置篡改。", file=sys.stderr)
        print("       检查 template-ui.yaml 是否配置了 CustomErrorResponses ——", file=sys.stderr)
        print("       它是分配级的，会把 /v1/* 的错误码一起改写。", file=sys.stderr)
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description="部署演示界面")
    parser.add_argument("--stack", default="yuetu-trip-ui-demo", help="UI 栈名称")
    parser.add_argument("--api-stack", default="yuetu-trip-cde-demo", help="CDE 交付栈名称")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--price-class", default="PriceClass_200")
    parser.add_argument("--sync-only", action="store_true", help="只重传页面并失效缓存")
    parser.add_argument("--delete", action="store_true", help="删除 UI 栈")
    args = parser.parse_args()

    cfn = boto3.client("cloudformation", region_name=args.region)
    s3 = boto3.client("s3", region_name=args.region)
    cloudfront = boto3.client("cloudfront")

    if args.delete:
        delete_stack(cfn, s3, args.stack)
        return 0

    if not args.sync_only:
        api_domain = resolve_api_domain(cfn, args.api_stack)
        print(f"[OK] API 源：{api_domain}")
        deploy_stack(cfn, args.stack, api_domain, args.price_class)

    outputs = stack_outputs(cfn, args.stack)
    bucket = outputs.get("UiBucketName")
    distribution_id = outputs.get("DistributionId")
    ui_url = outputs.get("UiUrl")

    if not (bucket and distribution_id and ui_url):
        print(f"[FAIL] 栈 {args.stack} 输出不完整，无法上传。", file=sys.stderr)
        return 1

    sync_ui(s3, bucket)
    invalidate(cloudfront, distribution_id)

    # 缓存失效需要一点时间生效，校验前稍等。
    time.sleep(8)
    verified = verify(ui_url)

    print()
    print("=" * 74)
    print("演示界面已就绪")
    print("=" * 74)
    print(f"  打开：{ui_url}")
    print()
    print("  首次打开需在右上角粘贴 read+write 令牌后点「连接」。")
    print("  取令牌：")
    print("    aws secretsmanager get-secret-value \\")
    print("      --secret-id yuetu-trip-demo-api-tokens \\")
    print("      --query SecretString --output text | python3 -m json.tool")
    print()
    print("  说明：界面与 API 同源（/ 走 S3，/v1/* 走 API Gateway），因此无需 CORS。")
    print("        本栈不持有任何令牌；未输入令牌时页面无法调用任何接口。")
    print()
    print("  演示结束删除：python3 scripts/deploy_ui.py --delete")
    print("=" * 74)
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
