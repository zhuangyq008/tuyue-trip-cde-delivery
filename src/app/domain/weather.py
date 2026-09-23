"""天气感知（数据清单 #8）。

用途限定得很死：天气**只影响室内/户外的排布与提示**，
绝不参与可订判定。下雨不会让一张有库存的门票变成不可订，
反过来，天晴也不能替供应商证明有货。

缺数据时按「未知」处理并如实标注，不用邻近日期或默认晴天填充 ——
推测天气会让「已校验行程」这个说法失去意义。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..core.clock import now
from ..adapters.catalog import Catalog
from . import freshness

# 第三方天气 API 的刷新时刻（UTC）。清单 #8 标注「每日刷新」，
# 因此时效应当按「距最近一次刷新多久」判定。
DAILY_REFRESH_HOUR_UTC = 6

# 降雨概率阈值（百分比）：达到该值即建议优先室内。
RAIN_THRESHOLD = 50
# 重度降雨阈值：户外行程给出更强的调整建议。
HEAVY_RAIN_THRESHOLD = 75

# 极端温度提示阈值（摄氏度），亲子行程对高低温更敏感。
HOT_THRESHOLD_C = 33
COLD_THRESHOLD_C = 5


@dataclass(frozen=True)
class DayWeather:
    date: str
    available: bool
    condition: str | None = None
    condition_label: str | None = None
    precipitation_probability: int | None = None
    temp_min_c: int | None = None
    temp_max_c: int | None = None
    # 最近一次刷新时刻。静态数据集只是这个每日接口的**罐装响应体**，
    # 时效要按接口的刷新节奏算，而不是按数据集生成日算 ——
    # 否则任何一份打包进 Lambda 的天气数据都会永久显示「已过期」，
    # 把一个建模错位伪装成真实的过期告警，真正的过期场景反而被噪声淹没。
    refreshed_at: str | None = None
    # 数据集生成时间，仅用于溯源。
    canned_at: str | None = None

    @property
    def prefers_indoor(self) -> bool:
        """是否建议优先室内。数据缺失时返回 False —— 不因未知而擅自改排。"""
        if not self.available or self.precipitation_probability is None:
            return False
        return self.precipitation_probability >= RAIN_THRESHOLD

    @property
    def is_heavy_rain(self) -> bool:
        return (
            self.available
            and self.precipitation_probability is not None
            and self.precipitation_probability >= HEAVY_RAIN_THRESHOLD
        )

    def to_dict(self) -> dict[str, Any]:
        if not self.available:
            return {
                "date": self.date,
                "available": False,
                "note": "该日期缺少天气数据，按未知处理；未做推测填充。",
                "freshness": freshness.assess_age(8, None).to_dict(),
            }
        return {
            "date": self.date,
            "available": True,
            "condition": self.condition,
            "condition_label": self.condition_label,
            "precipitation_probability": self.precipitation_probability,
            "temp_min_c": self.temp_min_c,
            "temp_max_c": self.temp_max_c,
            "prefers_indoor": self.prefers_indoor,
            "refreshed_at": self.refreshed_at,
            "canned_at": self.canned_at,
            "source_note": "静态数据集为每日刷新接口的罐装响应体；时效按接口刷新节奏判定。",
            "freshness": freshness.assess_timestamp(8, self.refreshed_at).to_dict(),
        }


def last_refresh_iso() -> str:
    """最近一次每日刷新的时刻（UTC ISO-8601）。"""
    moment = now()
    boundary = moment.replace(hour=DAILY_REFRESH_HOUR_UTC, minute=0, second=0, microsecond=0)
    if moment < boundary:
        # 当天刷新还没到点，最近一次是前一天。
        boundary = boundary - timedelta(days=1)
    return boundary.strftime("%Y-%m-%dT%H:%M:%SZ")


def for_date(catalog: Catalog, day: str) -> DayWeather:
    forecast = catalog.weather(day)
    if forecast is None:
        return DayWeather(date=day, available=False)
    return DayWeather(
        date=day,
        available=True,
        condition=forecast.get("condition"),
        condition_label=forecast.get("condition_label"),
        precipitation_probability=forecast.get("precipitation_probability"),
        temp_min_c=forecast.get("temp_min_c"),
        temp_max_c=forecast.get("temp_max_c"),
        refreshed_at=last_refresh_iso(),
        canned_at=forecast.get("updated_at"),
    )


def advisories(weather: DayWeather, *, has_young_child: bool) -> list[str]:
    """生成对外提示文案。全部基于已有字段，不含推测。"""
    if not weather.available:
        return ["该日天气数据缺失，户外安排的适宜性待确认。"]

    notes: list[str] = []
    if weather.is_heavy_rain:
        notes.append(
            f"{weather.date} 降雨概率 {weather.precipitation_probability}%，建议以室内场馆为主并预留备选。"
        )
    elif weather.prefers_indoor:
        notes.append(f"{weather.date} 降雨概率 {weather.precipitation_probability}%，建议携带雨具并优先室内项目。")

    if weather.temp_max_c is not None and weather.temp_max_c >= HOT_THRESHOLD_C:
        notes.append(
            f"{weather.date} 最高温 {weather.temp_max_c}℃，"
            + ("同行有低龄儿童，建议缩短户外时段并增加补水休息。" if has_young_child else "建议避开正午户外时段。")
        )
    if weather.temp_min_c is not None and weather.temp_min_c <= COLD_THRESHOLD_C:
        notes.append(f"{weather.date} 最低温 {weather.temp_min_c}℃，建议做好保暖。")

    return notes
