"""令牌校验。

CDE 规范 §3：令牌存于 Secrets Manager / SSM SecureString，
**不落代码、不落环境变量明文**。本模块是唯一读取令牌的地方。

规范 §4.2：必须启用 Token 鉴权，未带令牌 / 错误令牌必须被拒。
比较使用 `hmac.compare_digest` 做常量时间比较，避免时序侧信道。
"""

from __future__ import annotations

import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

from ..core import logging as log
from ..core.config import get_config

_BOTO_CONFIG = BotoConfig(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=2, read_timeout=3)

# 进程内缓存：减少 Secrets Manager 调用与冷启动开销。
#
# 为什么必须带 TTL：Lambda 容器可能存活数小时。若缓存永不失效，
# 轮换或**吊销**令牌后，已温启动的容器会继续接受旧令牌直到容器被回收 ——
# 对一个即将公开仓库、需要具备应急吊销能力的服务，这个窗口太长。
# 5 分钟是取舍点：足够摊薄密钥调用成本，又把吊销延迟压到可接受范围。
# 紧急吊销仍应配合强制重新部署（见 docs/RUNBOOK.md），不要只依赖 TTL。
_TOKEN_CACHE_TTL_SECONDS = 300

_TOKENS: list["TokenRecord"] | None = None
_TOKENS_LOADED_AT: float = 0.0


@dataclass(frozen=True)
class TokenRecord:
    token: str
    subject: str
    scopes: frozenset[str]


@dataclass(frozen=True)
class AuthResult:
    authenticated: bool
    subject: str
    scopes: frozenset[str]
    # 未通过时的拒绝原因，仅用于日志与对外的粗粒度提示，不回显令牌内容。
    reason: str = ""


ANONYMOUS = AuthResult(authenticated=False, subject="anonymous", scopes=frozenset(), reason="MISSING_TOKEN")


def _cache_is_fresh() -> bool:
    return _TOKENS is not None and (time.monotonic() - _TOKENS_LOADED_AT) < _TOKEN_CACHE_TTL_SECONDS


def _load_tokens() -> list[TokenRecord]:
    global _TOKENS, _TOKENS_LOADED_AT
    if _cache_is_fresh():
        return _TOKENS or []

    cfg = get_config()
    if not cfg.auth_secret_id:
        log.error("auth_secret_not_configured")
        _TOKENS = []
        _TOKENS_LOADED_AT = time.monotonic()
        return _TOKENS

    raw: str | None = None
    try:
        if cfg.auth_secret_id.startswith("/"):
            ssm = boto3.client("ssm", region_name=cfg.region, config=_BOTO_CONFIG)
            raw = ssm.get_parameter(Name=cfg.auth_secret_id, WithDecryption=True)["Parameter"]["Value"]
        else:
            secrets = boto3.client("secretsmanager", region_name=cfg.region, config=_BOTO_CONFIG)
            raw = secrets.get_secret_value(SecretId=cfg.auth_secret_id)["SecretString"]
    except Exception as exc:  # 取不到密钥时一律拒绝访问，绝不降级为放行
        log.error("auth_secret_fetch_failed", error_type=type(exc).__name__)
        _TOKENS = []
        # 失败也记时间戳：否则每个请求都会重打 Secrets Manager，
        # 在密钥故障期间把一次故障放大成一场限流风暴。
        _TOKENS_LOADED_AT = time.monotonic()
        return _TOKENS

    _TOKENS = _parse_tokens(raw)
    _TOKENS_LOADED_AT = time.monotonic()
    log.info("auth_tokens_loaded", token_count=len(_TOKENS), cache_ttl_seconds=_TOKEN_CACHE_TTL_SECONDS)
    return _TOKENS


def _parse_tokens(raw: str) -> list[TokenRecord]:
    try:
        parsed: Any = json.loads(raw)
    except json.JSONDecodeError:
        log.error("auth_secret_malformed")
        return []

    entries = parsed.get("tokens") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        log.error("auth_secret_missing_tokens_array")
        return []

    records: list[TokenRecord] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        token = entry.get("token")
        if not isinstance(token, str) or not token:
            continue
        scopes = entry.get("scopes")
        records.append(
            TokenRecord(
                token=token,
                subject=str(entry.get("subject") or "unknown"),
                scopes=frozenset(s for s in (scopes or []) if isinstance(s, str)),
            )
        )
    return records


def extract_bearer(authorization_header: str | None) -> str | None:
    """从 Authorization 头提取 Bearer 令牌。格式不符返回 None。"""
    if not authorization_header:
        return None
    parts = authorization_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    return token or None


def authenticate(authorization_header: str | None) -> AuthResult:
    token = extract_bearer(authorization_header)
    if token is None:
        return ANONYMOUS

    # 常量时间比较全部候选，避免因提前 return 泄露前缀匹配长度。
    matched: TokenRecord | None = None
    for record in _load_tokens():
        if hmac.compare_digest(record.token, token):
            matched = record

    if matched is None:
        return AuthResult(authenticated=False, subject="anonymous", scopes=frozenset(), reason="INVALID_TOKEN")

    return AuthResult(authenticated=True, subject=matched.subject, scopes=matched.scopes)


def reset_token_cache() -> None:
    """仅供测试使用。"""
    global _TOKENS, _TOKENS_LOADED_AT
    _TOKENS = None
    _TOKENS_LOADED_AT = 0.0
