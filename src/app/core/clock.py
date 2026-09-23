"""时间源。

统一出口便于测试与演示；`FROZEN_NOW` 环境变量可冻结时钟以获得完全可复现的响应。
所有对外时间戳均为 UTC ISO-8601（带 Z），避免时区歧义。
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone

# 大阪本地时区（JST，全年 UTC+9，无夏令时）——行程时间按当地时间推演。
JST = timezone(timedelta(hours=9))


def now() -> datetime:
    frozen = os.environ.get("FROZEN_NOW", "").strip()
    if frozen:
        try:
            return datetime.fromisoformat(frozen.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def now_epoch() -> int:
    return int(now().timestamp())


def iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_iso() -> str:
    return iso(now())


def today_jst() -> date:
    return now().astimezone(JST).date()


def parse_date(raw: str) -> date | None:
    """严格解析 YYYY-MM-DD；不接受其他格式，避免歧义输入被静默接受。"""
    if not isinstance(raw, str) or len(raw) != 10:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def minutes_to_hhmm(total_minutes: int) -> str:
    total_minutes = max(0, min(total_minutes, 24 * 60 - 1))
    return f"{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def hhmm_to_minutes(value: str) -> int | None:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return None
    try:
        hours, minutes = int(value[:2]), int(value[3:])
    except ValueError:
        return None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        return None
    return hours * 60 + minutes
