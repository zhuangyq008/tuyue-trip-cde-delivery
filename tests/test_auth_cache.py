"""令牌缓存与吊销测试。

这组用例来自一次真实的安全评审发现：令牌缓存原本没有 TTL，
导致吊销令牌后已温启动的 Lambda 容器仍会放行旧令牌。
修复加了 5 分钟 TTL，这里把行为钉死 —— 缓存策略是安全属性，不能靠人记得。
"""

from __future__ import annotations

import time

import pytest

from app.core import auth


def test_cache_is_used_within_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """TTL 窗口内不应重复回源。"""
    calls: list[int] = []

    def counting_parse(raw: str) -> list[auth.TokenRecord]:
        calls.append(1)
        return [auth.TokenRecord(token="MOCK_T", subject="s", scopes=frozenset({"read"}))]

    auth.reset_token_cache()
    monkeypatch.setattr(auth, "_TOKENS", [auth.TokenRecord("MOCK_T", "s", frozenset({"read"}))])
    monkeypatch.setattr(auth, "_TOKENS_LOADED_AT", time.monotonic())
    monkeypatch.setattr(auth, "_parse_tokens", counting_parse)

    for _ in range(5):
        assert auth.authenticate("Bearer MOCK_T").authenticated is True
    assert calls == [], "TTL 窗口内不应触发任何重新解析"


def test_cache_expires_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    """超过 TTL 必须重新回源 —— 这是吊销能生效的前提。"""
    auth.reset_token_cache()
    monkeypatch.setattr(auth, "_TOKENS", [auth.TokenRecord("MOCK_OLD", "s", frozenset({"read"}))])
    # 把加载时间推到 TTL 之外。
    monkeypatch.setattr(auth, "_TOKENS_LOADED_AT", time.monotonic() - auth._TOKEN_CACHE_TTL_SECONDS - 1)

    assert auth._cache_is_fresh() is False, "超过 TTL 的缓存必须判为过期"


def test_cache_fresh_check_respects_ttl_boundary(monkeypatch: pytest.MonkeyPatch) -> None:
    auth.reset_token_cache()
    monkeypatch.setattr(auth, "_TOKENS", [])
    monkeypatch.setattr(auth, "_TOKENS_LOADED_AT", time.monotonic() - 1)
    assert auth._cache_is_fresh() is True


def test_missing_secret_id_never_grants_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """取不到密钥时必须拒绝，绝不降级为放行。"""
    from app.core import config

    auth.reset_token_cache()
    monkeypatch.setenv("AUTH_SECRET_ID", "")
    config.reset_config_cache()
    try:
        assert auth.authenticate("Bearer MOCK_ANYTHING").authenticated is False
    finally:
        config.reset_config_cache()
        auth.reset_token_cache()


def test_fetch_failure_is_cached_to_avoid_call_storm(monkeypatch: pytest.MonkeyPatch) -> None:
    """密钥故障期间也要记时间戳，否则每个请求都重打 Secrets Manager。"""
    from app.core import config

    auth.reset_token_cache()
    monkeypatch.setenv("AUTH_SECRET_ID", "")
    config.reset_config_cache()
    try:
        auth._load_tokens()
        assert auth._TOKENS == []
        assert auth._cache_is_fresh() is True, "失败结果也应进入缓存窗口"
    finally:
        config.reset_config_cache()
        auth.reset_token_cache()


# ---------------------------------------------------------------- 令牌解析


def test_malformed_secret_yields_no_tokens() -> None:
    assert auth._parse_tokens("not json") == []
    assert auth._parse_tokens('{"no_tokens_key": 1}') == []
    assert auth._parse_tokens("[]") == []


def test_entries_without_token_are_skipped() -> None:
    records = auth._parse_tokens(
        '{"tokens":[{"subject":"a"},{"token":"","subject":"b"},'
        '{"token":"MOCK_GOOD","subject":"c","scopes":["read","write"]}]}'
    )
    assert len(records) == 1
    assert records[0].token == "MOCK_GOOD"
    assert records[0].scopes == frozenset({"read", "write"})


def test_non_string_scopes_are_dropped() -> None:
    records = auth._parse_tokens('{"tokens":[{"token":"MOCK_X","scopes":["read",5,null]}]}')
    assert records[0].scopes == frozenset({"read"})


# ---------------------------------------------------------------- Bearer 解析


@pytest.mark.parametrize(
    "header,expected",
    [
        ("Bearer MOCK_T", "MOCK_T"),
        ("bearer MOCK_T", "MOCK_T"),
        ("  Bearer   MOCK_T  ", "MOCK_T"),
        ("Basic MOCK_T", None),
        ("MOCK_T", None),
        ("Bearer", None),
        ("Bearer ", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_bearer(header: str | None, expected: str | None) -> None:
    assert auth.extract_bearer(header) == expected
