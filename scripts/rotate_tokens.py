#!/usr/bin/env python3
"""生成并写入 API 访问令牌。

为什么单独做成脚本、而不是在 CloudFormation 里生成令牌：
  * 令牌不进模板、不进 CFN 参数、不进代码仓库（规范 §3「令牌不落代码」）。
  * CFN 参数即使标 NoEcho 也仍存于栈元数据；本地生成 + 直接写密钥，链路最短。

生成两个令牌，刻意区分权限范围：
  * 完整令牌（read+write）—— 提交给判分使用。
  * 只读令牌（read）    —— 用于自检「写操作被 403 拒绝」，验证 401/403 可区分。

用法：
    python3 scripts/rotate_tokens.py --stage demo
    python3 scripts/rotate_tokens.py --secret-id yuetu-trip-demo-api-tokens --print-token
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys

import boto3
from botocore.exceptions import ClientError

# 令牌长度：32 字节熵，URL 安全，避免标点带来的 header 转义问题。
TOKEN_BYTES = 32
TOKEN_PREFIX = "yuetu"


def new_token(label: str) -> str:
    return f"{TOKEN_PREFIX}_{label}_{secrets.token_urlsafe(TOKEN_BYTES)}"


def build_payload() -> dict:
    return {
        "tokens": [
            {
                "token": new_token("rw"),
                "subject": "cde-scorer",
                "scopes": ["read", "write"],
                "note": "提交给判分平台使用的完整权限令牌",
            },
            {
                "token": new_token("ro"),
                "subject": "cde-readonly",
                "scopes": ["read"],
                "note": "自检用只读令牌，用于验证写操作返回 403",
            },
        ],
        "rotated_at_note": "令牌仅存于 Secrets Manager，不写入代码仓库或交付文档。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成并写入 API 令牌")
    parser.add_argument("--stage", default="demo")
    parser.add_argument("--secret-id", default=None, help="默认取 yuetu-trip-<stage>-api-tokens")
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--print-token",
        action="store_true",
        help="在标准输出打印完整权限令牌（仅在需要提交时使用；注意终端记录风险）",
    )
    args = parser.parse_args()

    secret_id = args.secret_id or f"yuetu-trip-{args.stage}-api-tokens"
    payload = build_payload()

    client = boto3.client("secretsmanager", region_name=args.region)
    try:
        client.put_secret_value(SecretId=secret_id, SecretString=json.dumps(payload, ensure_ascii=False))
    except ClientError as exc:
        print(f"[FAIL] 写入密钥失败：{exc.response.get('Error', {}).get('Code')}", file=sys.stderr)
        return 1

    print(f"[OK] 已写入 {secret_id}，共 {len(payload['tokens'])} 个令牌。")
    print("[OK] Lambda 令牌缓存 TTL 为 5 分钟，新令牌最长 5 分钟内生效。")
    print("[WARN] 紧急吊销（如令牌误传公开仓库）不要只等 TTL：执行 sam deploy 强制轮换执行环境。")

    if args.print_token:
        rw = next(t for t in payload["tokens"] if "write" in t["scopes"])
        ro = next(t for t in payload["tokens"] if "write" not in t["scopes"])
        print("\n--- 提交用（read+write）---")
        print(f"TOKEN: {rw['token']}")
        print("\n--- 自检用（read only，写操作应返回 403）---")
        print(f"TOKEN: {ro['token']}")
    else:
        print("[INFO] 未打印令牌。需要取用时加 --print-token，或从 Secrets Manager 控制台读取。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
