"""测试夹具：内存版 DynamoDB Table 替身 + 固定时钟 + 测试令牌。

刻意替换的是**最底层的 Table 对象**而不是 store 模块的函数：
这样 store.py 自己的逻辑（TTL 读侧过期判定、条件写入、payload 序列化）
也一并被测到，而不是被 mock 掉。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

# 环境变量必须在导入 app 之前设置：Config 在首次 get_config() 时快照。
os.environ.setdefault("TABLE_NAME", "yuetu-cde-test")
os.environ.setdefault("AUTH_SECRET_ID", "")
os.environ.setdefault("STAGE", "test")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

TOKEN_RW = "MOCK_TEST_TOKEN_READ_WRITE"
TOKEN_RO = "MOCK_TEST_TOKEN_READ_ONLY"


class ConditionalCheckFailed(Exception):
    """模拟 botocore 的 ConditionalCheckFailedException 形态。"""

    def __init__(self) -> None:
        super().__init__("ConditionalCheckFailedException")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    """DynamoDB Table 的最小可用替身（仅支持本项目实际用到的操作）。"""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}

    def put_item(self, Item: dict[str, Any], ConditionExpression: str | None = None) -> dict[str, Any]:  # noqa: N803
        key = (Item["PK"], Item["SK"])
        if ConditionExpression == "attribute_not_exists(PK)" and key in self.items:
            from botocore.exceptions import ClientError

            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = dict(Item)
        return {}

    def get_item(self, Key: dict[str, Any], ConsistentRead: bool = False) -> dict[str, Any]:  # noqa: N803
        item = self.items.get((Key["PK"], Key["SK"]))
        return {"Item": dict(item)} if item is not None else {}


@pytest.fixture(autouse=True)
def fake_store(monkeypatch: pytest.MonkeyPatch) -> FakeTable:
    from app.adapters import store

    table = FakeTable()
    monkeypatch.setattr(store, "_TABLE", table)
    return table


@pytest.fixture(autouse=True)
def fake_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """注入测试令牌，避免测试触碰 Secrets Manager。

    必须同时设置 `_TOKENS_LOADED_AT`：令牌缓存带 5 分钟 TTL，
    只设 `_TOKENS` 会被判为已过期而回源 Secrets Manager，
    进而把注入的令牌覆盖成空列表 —— 表现为「正确令牌也返回 401」。
    """
    import time

    from app.core import auth

    monkeypatch.setattr(
        auth,
        "_TOKENS",
        [
            auth.TokenRecord(token=TOKEN_RW, subject="MOCK_test_rw", scopes=frozenset({"read", "write"})),
            auth.TokenRecord(token=TOKEN_RO, subject="MOCK_test_ro", scopes=frozenset({"read"})),
        ],
    )
    monkeypatch.setattr(auth, "_TOKENS_LOADED_AT", time.monotonic())


@pytest.fixture
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """冻结到数据集锚定日附近，使日期相关断言稳定。"""
    monkeypatch.setenv("FROZEN_NOW", "2026-11-18T01:00:00Z")


def make_event(
    method: str,
    path: str,
    *,
    body: Any = None,
    token: str | None = TOKEN_RW,
    query: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    authorizer_context: dict[str, str] | None = None,
) -> dict[str, Any]:
    """构造 API Gateway HTTP API payload 2.0 事件。"""
    import json

    request_context: dict[str, Any] = {
        "requestId": "test-request-id",
        "http": {"method": method, "path": path},
    }
    if authorizer_context is not None:
        request_context["authorizer"] = {"lambda": authorizer_context}

    all_headers = {"content-type": "application/json", **(headers or {})}
    if token is not None:
        all_headers["authorization"] = f"Bearer {token}"

    return {
        "version": "2.0",
        "rawPath": path,
        "requestContext": request_context,
        "headers": all_headers,
        "queryStringParameters": query or {},
        "body": json.dumps(body, ensure_ascii=False) if body is not None else None,
        "isBase64Encoded": False,
    }


def call(method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
    """调用 handler 并返回 (状态码, 解析后的 body)。"""
    import json

    from app.handler import handler

    response = handler(make_event(method, path, **kwargs))
    return response["statusCode"], json.loads(response["body"])
