"""运行时配置。

所有可调项通过环境变量注入，代码中不出现任何令牌、密钥或客户标识。
默认值以「CDE 判分安全」为准：宁可保守，不要出现未定义行为。
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    """不可变配置快照，进程启动时读取一次。"""

    # --- 基础设施 ---
    table_name: str
    auth_secret_id: str
    region: str
    stage: str

    # --- 认证语义 ---
    # handler_decide  : Authorizer 放行并透传认证结论 → 业务 Lambda 返回 401/403（默认）
    #                   符合 RFC 7235：无效凭证 401、凭证有效但越权 403，
    #                   且所有拒绝共用统一响应体字段名。
    # authorizer_deny : Authorizer 返回 Deny → API Gateway 输出 403
    #                   （无效令牌与越权都落在 403；挂测要求如此时一键切换，无需改代码）
    auth_mode: str

    # --- 动态可订查询 ---
    # 可订快照缓存时长：同一 (商品, 日期, 人数) 在窗口内返回同一快照，
    # 既满足 §4.5 幂等，又显式建模「可订状态有时效」。
    availability_ttl_seconds: int
    supplier_timeout_ms: int

    # --- 行程可执行性阈值 ---
    max_single_transit_minutes: int
    max_daily_transit_minutes: int
    max_daily_active_minutes: int
    reservation_lead_days: int
    content_stale_days: int

    # --- 数据留存 ---
    plan_ttl_seconds: int
    booking_ttl_seconds: int
    event_ttl_seconds: int

    # --- 大模型叙述层（默认关闭，保证判分确定性）---
    enable_llm_narrative: bool
    bedrock_model_id: str

    @staticmethod
    def from_env() -> "Config":
        return Config(
            table_name=os.environ.get("TABLE_NAME", "yuetu-cde-demo"),
            auth_secret_id=os.environ.get("AUTH_SECRET_ID", ""),
            region=os.environ.get("AWS_REGION", "us-east-1"),
            stage=os.environ.get("STAGE", "demo"),
            auth_mode=os.environ.get("AUTH_MODE", "handler_decide").strip().lower(),
            availability_ttl_seconds=_env_int("AVAILABILITY_TTL_SECONDS", 120),
            supplier_timeout_ms=_env_int("SUPPLIER_TIMEOUT_MS", 2000),
            max_single_transit_minutes=_env_int("MAX_SINGLE_TRANSIT_MINUTES", 45),
            max_daily_transit_minutes=_env_int("MAX_DAILY_TRANSIT_MINUTES", 150),
            max_daily_active_minutes=_env_int("MAX_DAILY_ACTIVE_MINUTES", 600),
            reservation_lead_days=_env_int("RESERVATION_LEAD_DAYS", 1),
            content_stale_days=_env_int("CONTENT_STALE_DAYS", 180),
            plan_ttl_seconds=_env_int("PLAN_TTL_SECONDS", 7 * 24 * 3600),
            booking_ttl_seconds=_env_int("BOOKING_TTL_SECONDS", 7 * 24 * 3600),
            event_ttl_seconds=_env_int("EVENT_TTL_SECONDS", 7 * 24 * 3600),
            enable_llm_narrative=_env_bool("ENABLE_LLM_NARRATIVE", False),
            bedrock_model_id=os.environ.get("BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-5"),
        )


_CONFIG: Config | None = None


def get_config() -> Config:
    global _CONFIG
    if _CONFIG is None:
        _CONFIG = Config.from_env()
    return _CONFIG


def reset_config_cache() -> None:
    """仅供测试使用：清除进程内配置缓存。"""
    global _CONFIG
    _CONFIG = None
