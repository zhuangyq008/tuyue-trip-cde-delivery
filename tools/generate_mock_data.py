#!/usr/bin/env python3
"""Mock 数据集生成器（CDE 规范 §2）。

设计要点：
  * **零真实客户数据**：全部为合成值，schema 与真实 OTA 结构保持一致
    （字段名、类型、枚举、基数），值全部伪造。
  * **一眼可辨**：标识统一 `MOCK_` 前缀，URL 使用保留域 `.invalid`（永不可解析），
    邮箱 `@example.com`，手机号 `13000000000` 段，订单号 `MOCK-ORD-####`。
  * **可复现**：固定 seed，同一版本脚本产出逐字节一致的数据集。
  * 不含任何真实姓名、证件号、手机号、地址、支付信息。

用法：
    python3 tools/generate_mock_data.py                # 写入默认目录
    python3 tools/generate_mock_data.py --out /tmp/mk  # 自定义输出目录
    python3 tools/generate_mock_data.py --check        # 只校验，不写盘
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any

SEED = 20260923
DEFAULT_OUT = Path(__file__).resolve().parents[1] / "src" / "app" / "data" / "mock"

# 生成基准日：数据集中的相对日期以此为锚，保证可复现。
ANCHOR_DATE = "2026-09-23"

# ---------------------------------------------------------------- 目的地（清单 #6）

DESTINATIONS: list[dict[str, Any]] = [
    {
        "destination_id": "MOCK_DEST_OSAKA",
        "name_zh": "大阪",
        "name_local": "MOCK_Osaka",
        "country_code": "JP",
        "country_name_zh": "MOCK_日本",
        "timezone": "Asia/Tokyo",
        "utc_offset_minutes": 540,
        "currency": "JPY",
        "settlement_currency": "CNY",
        # 坐标为打散后的示意值，不指向任何真实门址。
        "coordinates": {"lat": 34.69, "lng": 135.50, "coordinates_mock": True},
        "aliases": ["大阪", "Osaka", "osaka", "OSAKA"],
    }
]

# ---------------------------------------------------------------- 区域与交通

REGIONS: list[dict[str, Any]] = [
    {
        "region_id": "MOCK_REG_KONOHANA",
        "name_zh": "MOCK_此花区",
        "hub": "MOCK_环球城站",
        "coordinates": {"lat": 34.666, "lng": 135.432},
    },
    {
        "region_id": "MOCK_REG_MINATO",
        "name_zh": "MOCK_港区",
        "hub": "MOCK_大阪港站",
        "coordinates": {"lat": 34.654, "lng": 135.429},
    },
    {
        "region_id": "MOCK_REG_CHUO",
        "name_zh": "MOCK_中央区",
        "hub": "MOCK_难波站",
        "coordinates": {"lat": 34.666, "lng": 135.500},
    },
    {
        "region_id": "MOCK_REG_KITA",
        "name_zh": "MOCK_北区",
        "hub": "MOCK_梅田站",
        "coordinates": {"lat": 34.702, "lng": 135.496},
    },
    {
        "region_id": "MOCK_REG_SUITA",
        "name_zh": "MOCK_吹田市",
        "hub": "MOCK_万博纪念公园站",
        "coordinates": {"lat": 34.807, "lng": 135.533},
    },
]

# 区域间公共交通耗时（分钟，含换乘步行）。无向对称矩阵，缺失对视为不可直达。
TRANSIT_PAIRS: dict[tuple[str, str], int] = {
    ("MOCK_REG_KONOHANA", "MOCK_REG_MINATO"): 18,
    ("MOCK_REG_KONOHANA", "MOCK_REG_CHUO"): 32,
    ("MOCK_REG_KONOHANA", "MOCK_REG_KITA"): 28,
    ("MOCK_REG_KONOHANA", "MOCK_REG_SUITA"): 62,
    ("MOCK_REG_MINATO", "MOCK_REG_CHUO"): 24,
    ("MOCK_REG_MINATO", "MOCK_REG_KITA"): 30,
    ("MOCK_REG_MINATO", "MOCK_REG_SUITA"): 58,
    ("MOCK_REG_CHUO", "MOCK_REG_KITA"): 12,
    ("MOCK_REG_CHUO", "MOCK_REG_SUITA"): 40,
    ("MOCK_REG_KITA", "MOCK_REG_SUITA"): 34,
}

# 同区域内点到点默认耗时。
INTRA_REGION_MINUTES = 12

# ---------------------------------------------------------------- 景点（POI）
# weekday 约定：0=周一 … 6=周日（与 Python date.weekday() 一致）。

POIS: list[dict[str, Any]] = [
    {
        "poi_id": "MOCK_POI_THEMEPARK",
        "name_zh": "MOCK_大阪影视主题乐园",
        "region_id": "MOCK_REG_KONOHANA",
        "category": "theme_park",
        "kid_fit_score": 92,
        "popularity_score": 98,
        "typical_duration_minutes": 420,
        "opening_hours": {"open": "09:00", "close": "20:00", "last_entry": "18:00"},
        "closed_weekdays": [],
        "closed_dates": [],
        "reservation": {"required": True, "lead_days": 1, "note": "MOCK_需提前指定入园日期"},
        "age_policy": {"min_age": 0, "max_age": None, "min_height_cm": None, "adult_required_under_age": 12},
        "indoor": False,
        "linked_product_ids": ["MOCK_PROD_THEMEPARK_1DAY", "MOCK_PROD_THEMEPARK_EXPRESS"],
    },
    {
        "poi_id": "MOCK_POI_AQUARIUM",
        "name_zh": "MOCK_大阪湾海洋馆",
        "region_id": "MOCK_REG_MINATO",
        "category": "aquarium",
        "kid_fit_score": 96,
        "popularity_score": 90,
        "typical_duration_minutes": 150,
        "opening_hours": {"open": "10:00", "close": "20:00", "last_entry": "19:00"},
        "closed_weekdays": [],
        # 设为闭馆日：用于验证「闭馆备选」链路（需求文档 §1.2-3）。
        "closed_dates": ["2026-11-25"],
        "reservation": {"required": False, "lead_days": 0, "note": None},
        "age_policy": {"min_age": 0, "max_age": None, "min_height_cm": None, "adult_required_under_age": 12},
        "indoor": True,
        "linked_product_ids": ["MOCK_PROD_AQUARIUM_TICKET", "MOCK_PROD_CITY_PASS_2DAY"],
    },
    {
        "poi_id": "MOCK_POI_FERRIS_WHEEL",
        "name_zh": "MOCK_港区海景摩天轮",
        "region_id": "MOCK_REG_MINATO",
        "category": "landmark",
        "kid_fit_score": 80,
        "popularity_score": 70,
        "typical_duration_minutes": 40,
        "opening_hours": {"open": "10:00", "close": "22:00", "last_entry": "21:30"},
        "closed_weekdays": [],
        "closed_dates": [],
        "reservation": {"required": False, "lead_days": 0, "note": None},
        "age_policy": {"min_age": 0, "max_age": None, "min_height_cm": None, "adult_required_under_age": 6},
        "indoor": False,
        "linked_product_ids": ["MOCK_PROD_CITY_PASS_2DAY"],
    },
    {
        "poi_id": "MOCK_POI_CASTLE_PARK",
        "name_zh": "MOCK_中央城址公园",
        "region_id": "MOCK_REG_CHUO",
        "category": "historic_site",
        "kid_fit_score": 68,
        "popularity_score": 88,
        "typical_duration_minutes": 120,
        "opening_hours": {"open": "09:00", "close": "17:00", "last_entry": "16:30"},
        "closed_weekdays": [],
        "closed_dates": ["2026-12-28", "2026-12-29"],
        "reservation": {"required": False, "lead_days": 0, "note": None},
        "age_policy": {"min_age": 0, "max_age": None, "min_height_cm": None, "adult_required_under_age": 12},
        "indoor": False,
        "linked_product_ids": ["MOCK_PROD_CASTLE_TICKET", "MOCK_PROD_CITY_PASS_2DAY"],
    },
    {
        "poi_id": "MOCK_POI_KIDS_SCIENCE",
        "name_zh": "MOCK_儿童科学探索馆",
        "region_id": "MOCK_REG_KITA",
        "category": "kids_museum",
        "kid_fit_score": 99,
        "popularity_score": 64,
        "typical_duration_minutes": 180,
        "opening_hours": {"open": "09:30", "close": "17:00", "last_entry": "16:00"},
        # 周一固定闭馆：用于验证按星期的闭馆判定。
        "closed_weekdays": [0],
        "closed_dates": [],
        "reservation": {"required": False, "lead_days": 0, "note": None},
        "age_policy": {"min_age": 0, "max_age": 15, "min_height_cm": None, "adult_required_under_age": 12},
        "indoor": True,
        "linked_product_ids": ["MOCK_PROD_KIDS_SCIENCE_TICKET"],
    },
    {
        "poi_id": "MOCK_POI_SKY_DECK",
        "name_zh": "MOCK_北区空中观景台",
        "region_id": "MOCK_REG_KITA",
        "category": "observation_deck",
        "kid_fit_score": 74,
        "popularity_score": 85,
        "typical_duration_minutes": 90,
        "opening_hours": {"open": "09:30", "close": "22:30", "last_entry": "22:00"},
        "closed_weekdays": [],
        "closed_dates": [],
        "reservation": {"required": False, "lead_days": 0, "note": None},
        "age_policy": {"min_age": 0, "max_age": None, "min_height_cm": None, "adult_required_under_age": 12},
        "indoor": True,
        "linked_product_ids": ["MOCK_PROD_SKY_DECK_TICKET", "MOCK_PROD_CITY_PASS_2DAY"],
    },
    {
        "poi_id": "MOCK_POI_FOOD_STREET",
        "name_zh": "MOCK_中央区小吃街",
        "region_id": "MOCK_REG_CHUO",
        "category": "food_district",
        "kid_fit_score": 82,
        "popularity_score": 94,
        "typical_duration_minutes": 90,
        "opening_hours": {"open": "10:00", "close": "23:00", "last_entry": "22:30"},
        "closed_weekdays": [],
        "closed_dates": [],
        "reservation": {"required": False, "lead_days": 0, "note": None},
        "age_policy": {"min_age": 0, "max_age": None, "min_height_cm": None, "adult_required_under_age": 12},
        "indoor": False,
        "linked_product_ids": [],
    },
    {
        "poi_id": "MOCK_POI_BRICK_CENTER",
        "name_zh": "MOCK_积木探索中心",
        "region_id": "MOCK_REG_MINATO",
        "category": "kids_indoor",
        "kid_fit_score": 94,
        "popularity_score": 60,
        "typical_duration_minutes": 150,
        "opening_hours": {"open": "10:00", "close": "18:00", "last_entry": "16:00"},
        "closed_weekdays": [],
        "closed_dates": [],
        "reservation": {"required": True, "lead_days": 2, "note": "MOCK_需提前 2 天预约时段"},
        # 成人不得单独入场：用于验证成人/儿童双向年龄约束。
        "age_policy": {"min_age": 2, "max_age": 12, "min_height_cm": 85, "adult_required_under_age": 12},
        "indoor": True,
        "linked_product_ids": ["MOCK_PROD_BRICK_CENTER_TICKET"],
    },
    {
        "poi_id": "MOCK_POI_NATURE_PARK",
        "name_zh": "MOCK_吹田生态公园",
        "region_id": "MOCK_REG_SUITA",
        "category": "park",
        "kid_fit_score": 88,
        "popularity_score": 58,
        "typical_duration_minutes": 200,
        "opening_hours": {"open": "09:30", "close": "17:00", "last_entry": "16:00"},
        "closed_weekdays": [2],
        "closed_dates": [],
        "reservation": {"required": False, "lead_days": 0, "note": None},
        "age_policy": {"min_age": 0, "max_age": None, "min_height_cm": None, "adult_required_under_age": 12},
        "indoor": False,
        "linked_product_ids": ["MOCK_PROD_NATURE_PARK_TICKET"],
    },
    {
        "poi_id": "MOCK_POI_TRAIN_MUSEUM",
        "name_zh": "MOCK_铁道模型博物馆",
        "region_id": "MOCK_REG_KITA",
        "category": "museum",
        "kid_fit_score": 90,
        "popularity_score": 55,
        "typical_duration_minutes": 120,
        "opening_hours": {"open": "10:00", "close": "17:30", "last_entry": "16:30"},
        "closed_weekdays": [3],
        "closed_dates": [],
        "reservation": {"required": False, "lead_days": 0, "note": None},
        "age_policy": {"min_age": 0, "max_age": None, "min_height_cm": None, "adult_required_under_age": 12},
        "indoor": True,
        "linked_product_ids": ["MOCK_PROD_TRAIN_MUSEUM_TICKET"],
    },
]

# ---------------------------------------------------------------- 商户（清单 #1/#2/#5）
# 商户与 POI 是**两个实体**：POI 是「去哪玩」，商户是「谁在经营、现在还开不开」。
# 清单把「商户在营状态」标为硬性约束，因此必须独立建模，不能塞进 POI 的营业时间里。
#
# operating_status.status 枚举：
#   OPEN                明确在营
#   SUSPENDED           暂停营业（装修、季节性停业）
#   CLOSED_PERMANENTLY  永久关闭
#   UNKNOWN             状态未知（同步失败/无数据）—— 按不可用处理，不得当成 OPEN

MERCHANT_OVERRIDES: dict[str, dict[str, Any]] = {
    # 暂停营业：验证「硬性约束过滤不可用商户」。
    "MOCK_POI_FERRIS_WHEEL": {
        "operating_status": {"status": "SUSPENDED", "note": "MOCK_设备年检暂停营业", "status_age_seconds": 120},
        "rating": {"overall": 4.1, "review_count": 862, "grade": "B"},
    },
    # 状态未知：验证「未知 ≠ 在营」。
    "MOCK_POI_TRAIN_MUSEUM": {
        "operating_status": {"status": "UNKNOWN", "note": "MOCK_供应链系统同步失败", "status_age_seconds": 90},
        "rating": {"overall": 4.4, "review_count": 311, "grade": "B"},
    },
    # 状态数据过期：时效要求 ≤5min，此条已 42 分钟未更新 → 不得按 OPEN 使用。
    "MOCK_POI_NATURE_PARK": {
        "operating_status": {"status": "OPEN", "note": "MOCK_状态同步滞后", "status_age_seconds": 2520},
        "rating": {"overall": 4.3, "review_count": 527, "grade": "B"},
    },
    "MOCK_POI_THEMEPARK": {
        "operating_status": {"status": "OPEN", "note": None, "status_age_seconds": 45},
        "rating": {"overall": 4.8, "review_count": 20431, "grade": "S"},
    },
    "MOCK_POI_AQUARIUM": {
        "operating_status": {"status": "OPEN", "note": None, "status_age_seconds": 60},
        "rating": {"overall": 4.7, "review_count": 9124, "grade": "A"},
    },
    "MOCK_POI_KIDS_SCIENCE": {
        "operating_status": {"status": "OPEN", "note": None, "status_age_seconds": 75},
        "rating": {"overall": 4.6, "review_count": 1583, "grade": "A"},
    },
    "MOCK_POI_BRICK_CENTER": {
        "operating_status": {"status": "OPEN", "note": None, "status_age_seconds": 30},
        "rating": {"overall": 4.2, "review_count": 640, "grade": "B"},
    },
}

# 非 POI 类商户（住宿、用车）。
STANDALONE_MERCHANTS: list[dict[str, Any]] = [
    {
        "merchant_id": "MOCK_MER_HOTEL_CHUO",
        "name_zh": "MOCK_中央区家庭公寓酒店",
        "category": "hotel",
        "region_id": "MOCK_REG_CHUO",
        "operating_status": {"status": "OPEN", "note": None, "status_age_seconds": 50},
        "rating": {"overall": 4.5, "review_count": 2044, "grade": "A"},
        "business_hours": {"open": "00:00", "close": "23:59", "last_entry": None},
        "product_ids": ["MOCK_PROD_FAMILY_HOTEL_CHUO"],
    },
    {
        "merchant_id": "MOCK_MER_TRANSFER",
        "name_zh": "MOCK_关西接送服务商",
        "category": "transfer",
        "region_id": "MOCK_REG_CHUO",
        "operating_status": {"status": "OPEN", "note": None, "status_age_seconds": 35},
        "rating": {"overall": 4.6, "review_count": 1290, "grade": "A"},
        "business_hours": {"open": "05:00", "close": "23:00", "last_entry": None},
        "product_ids": ["MOCK_PROD_AIRPORT_TRANSFER"],
    },
]

# ---------------------------------------------------------------- 天气（清单 #8）
# 每日刷新的第三方数据。生成 anchor 起 150 天，覆盖可预订窗口内的常用日期。
WEATHER_DAYS = 150

# 天气状况枚举与降雨概率区间（确定性抽取）。
_WEATHER_CONDITIONS: list[tuple[str, str, int, int]] = [
    ("sunny", "MOCK_晴", 0, 15),
    ("cloudy", "MOCK_多云", 10, 35),
    ("light_rain", "MOCK_小雨", 55, 75),
    ("heavy_rain", "MOCK_大雨", 80, 95),
]

# ---------------------------------------------------------------- 季节与事件（清单 #9）

SEASONALITY: list[dict[str, Any]] = [
    {"month": 1, "peak_level": "low", "avg_temp_c": [3, 9], "best_for": ["室内亲子", "购物"], "events": ["MOCK_新年促销周"]},
    {"month": 2, "peak_level": "low", "avg_temp_c": [3, 10], "best_for": ["室内亲子"], "events": []},
    {"month": 3, "peak_level": "shoulder", "avg_temp_c": [6, 14], "best_for": ["户外散步"], "events": ["MOCK_早春花季"]},
    {"month": 4, "peak_level": "peak", "avg_temp_c": [11, 20], "best_for": ["户外亲子", "公园"], "events": ["MOCK_春季花期节"]},
    {"month": 5, "peak_level": "peak", "avg_temp_c": [16, 25], "best_for": ["户外亲子"], "events": ["MOCK_五月长假"]},
    {"month": 6, "peak_level": "shoulder", "avg_temp_c": [20, 28], "best_for": ["室内亲子"], "events": ["MOCK_梅雨季"]},
    {"month": 7, "peak_level": "peak", "avg_temp_c": [24, 33], "best_for": ["水族馆", "室内"], "events": ["MOCK_夏日祭"]},
    {"month": 8, "peak_level": "peak", "avg_temp_c": [25, 34], "best_for": ["室内避暑"], "events": ["MOCK_盂兰盆假期"]},
    {"month": 9, "peak_level": "shoulder", "avg_temp_c": [21, 29], "best_for": ["户外亲子"], "events": ["MOCK_台风多发期"]},
    {"month": 10, "peak_level": "peak", "avg_temp_c": [15, 23], "best_for": ["户外亲子", "主题乐园"], "events": ["MOCK_秋季万圣活动"]},
    {"month": 11, "peak_level": "peak", "avg_temp_c": [10, 18], "best_for": ["户外亲子", "红叶"], "events": ["MOCK_红叶季"]},
    {"month": 12, "peak_level": "shoulder", "avg_temp_c": [5, 12], "best_for": ["室内亲子", "灯光节"], "events": ["MOCK_冬季灯光节"]},
]

# ---------------------------------------------------------------- 供应商

SUPPLIERS: list[dict[str, Any]] = [
    {"supplier_id": "MOCK_SUP_ALPHA", "name_zh": "MOCK_阿尔法票务", "contact_email": "alpha@example.com"},
    {"supplier_id": "MOCK_SUP_BETA", "name_zh": "MOCK_贝塔目的地服务", "contact_email": "beta@example.com"},
    {"supplier_id": "MOCK_SUP_GAMMA", "name_zh": "MOCK_伽马通票联营", "contact_email": "gamma@example.com"},
]

# ---------------------------------------------------------------- 商品
# mock_supplier_behavior.mode 枚举 —— 用于确定性注入供应商侧异常：
#   ok           正常返回可订
#   sold_out     明确售罄
#   timeout      查询超时（→ UNCONFIRMED，绝不可展示为可订）
#   partial      返回字段不完整（→ UNKNOWN）
#   error        供应商 5xx（→ UNCONFIRMED）
#   date_based   由 sold_out_dates / blackout 决定

PRODUCTS: list[dict[str, Any]] = [
    {
        "product_id": "MOCK_PROD_THEMEPARK_1DAY",
        "name_zh": "MOCK_主题乐园一日门票",
        "supplier_id": "MOCK_SUP_ALPHA",
        "poi_ids": ["MOCK_POI_THEMEPARK"],
        "product_type": "attraction_ticket",
        "price_hint": {"adult": 468.0, "child": 318.0},
        "pax_rules": {"min_pax": 1, "max_pax": 9, "child_age_range": [4, 11], "infant_free_under": 4},
        "date_rules": {"bookable_from_days_ahead": 1, "bookable_until_days_ahead": 120, "blackout_dates": []},
        "refund_policy": {"refundable": True, "free_cancel_hours": 72},
        "requires_reservation": True,
        "mock_supplier_behavior": {"mode": "date_based", "sold_out_dates": ["2026-11-22"]},
    },
    {
        "product_id": "MOCK_PROD_THEMEPARK_EXPRESS",
        "name_zh": "MOCK_主题乐园快速通行券",
        "supplier_id": "MOCK_SUP_ALPHA",
        "poi_ids": ["MOCK_POI_THEMEPARK"],
        "product_type": "add_on",
        "price_hint": {"adult": 520.0, "child": 520.0},
        "pax_rules": {"min_pax": 1, "max_pax": 6, "child_age_range": [4, 11], "infant_free_under": 4},
        "date_rules": {"bookable_from_days_ahead": 1, "bookable_until_days_ahead": 60, "blackout_dates": []},
        "refund_policy": {"refundable": False, "free_cancel_hours": 0},
        "requires_reservation": True,
        # 长期售罄：验证「售罄不得展示为可订」。
        "mock_supplier_behavior": {"mode": "sold_out", "sold_out_dates": []},
    },
    {
        "product_id": "MOCK_PROD_AQUARIUM_TICKET",
        "name_zh": "MOCK_海洋馆电子门票",
        "supplier_id": "MOCK_SUP_BETA",
        "poi_ids": ["MOCK_POI_AQUARIUM"],
        "product_type": "attraction_ticket",
        "price_hint": {"adult": 132.0, "child": 66.0},
        "pax_rules": {"min_pax": 1, "max_pax": 10, "child_age_range": [3, 15], "infant_free_under": 3},
        "date_rules": {"bookable_from_days_ahead": 0, "bookable_until_days_ahead": 90, "blackout_dates": ["2026-11-25"]},
        "refund_policy": {"refundable": True, "free_cancel_hours": 24},
        "requires_reservation": False,
        "mock_supplier_behavior": {"mode": "ok", "sold_out_dates": []},
    },
    {
        "product_id": "MOCK_PROD_CITY_PASS_2DAY",
        "name_zh": "MOCK_城市二日周游通票",
        "supplier_id": "MOCK_SUP_GAMMA",
        "poi_ids": ["MOCK_POI_AQUARIUM", "MOCK_POI_FERRIS_WHEEL", "MOCK_POI_CASTLE_PARK", "MOCK_POI_SKY_DECK"],
        "product_type": "pass",
        "price_hint": {"adult": 178.0, "child": 92.0},
        "pax_rules": {"min_pax": 1, "max_pax": 12, "child_age_range": [6, 12], "infant_free_under": 6},
        "date_rules": {"bookable_from_days_ahead": 0, "bookable_until_days_ahead": 180, "blackout_dates": []},
        "refund_policy": {"refundable": True, "free_cancel_hours": 48},
        "requires_reservation": False,
        "mock_supplier_behavior": {"mode": "ok", "sold_out_dates": []},
    },
    {
        "product_id": "MOCK_PROD_CASTLE_TICKET",
        "name_zh": "MOCK_城址天守阁门票",
        "supplier_id": "MOCK_SUP_BETA",
        "poi_ids": ["MOCK_POI_CASTLE_PARK"],
        "product_type": "attraction_ticket",
        "price_hint": {"adult": 32.0, "child": 0.0},
        "pax_rules": {"min_pax": 1, "max_pax": 20, "child_age_range": [6, 15], "infant_free_under": 6},
        "date_rules": {"bookable_from_days_ahead": 0, "bookable_until_days_ahead": 60, "blackout_dates": []},
        "refund_policy": {"refundable": False, "free_cancel_hours": 0},
        "requires_reservation": False,
        # 供应商 5xx：验证「失败 ≠ 可订」。
        # 该商品的商户状态为默认 OPEN 且新鲜，确保请求能走到供应商这一层。
        "mock_supplier_behavior": {"mode": "error", "sold_out_dates": []},
    },
    {
        "product_id": "MOCK_PROD_KIDS_SCIENCE_TICKET",
        "name_zh": "MOCK_儿童科学馆亲子套票",
        "supplier_id": "MOCK_SUP_BETA",
        "poi_ids": ["MOCK_POI_KIDS_SCIENCE"],
        "product_type": "family_package",
        "price_hint": {"adult": 76.0, "child": 38.0},
        "pax_rules": {"min_pax": 2, "max_pax": 6, "child_age_range": [3, 15], "infant_free_under": 3},
        "date_rules": {"bookable_from_days_ahead": 0, "bookable_until_days_ahead": 45, "blackout_dates": []},
        "refund_policy": {"refundable": True, "free_cancel_hours": 24},
        "requires_reservation": False,
        # 查询超时：验证「超时 ≠ 可订」。
        "mock_supplier_behavior": {"mode": "timeout", "sold_out_dates": []},
    },
    {
        "product_id": "MOCK_PROD_SKY_DECK_TICKET",
        "name_zh": "MOCK_空中观景台门票",
        "supplier_id": "MOCK_SUP_GAMMA",
        "poi_ids": ["MOCK_POI_SKY_DECK"],
        "product_type": "attraction_ticket",
        "price_hint": {"adult": 88.0, "child": 44.0},
        "pax_rules": {"min_pax": 1, "max_pax": 10, "child_age_range": [4, 12], "infant_free_under": 4},
        "date_rules": {"bookable_from_days_ahead": 0, "bookable_until_days_ahead": 90, "blackout_dates": []},
        "refund_policy": {"refundable": True, "free_cancel_hours": 12},
        "requires_reservation": False,
        # 返回字段不完整：验证「未知状态 ≠ 可订」。
        "mock_supplier_behavior": {"mode": "partial", "sold_out_dates": []},
    },
    {
        "product_id": "MOCK_PROD_BRICK_CENTER_TICKET",
        "name_zh": "MOCK_积木中心时段票",
        "supplier_id": "MOCK_SUP_ALPHA",
        "poi_ids": ["MOCK_POI_BRICK_CENTER"],
        "product_type": "attraction_ticket",
        "price_hint": {"adult": 118.0, "child": 118.0},
        "pax_rules": {"min_pax": 2, "max_pax": 8, "child_age_range": [2, 12], "infant_free_under": 2},
        "date_rules": {"bookable_from_days_ahead": 2, "bookable_until_days_ahead": 60, "blackout_dates": []},
        "refund_policy": {"refundable": True, "free_cancel_hours": 48},
        "requires_reservation": True,
        "mock_supplier_behavior": {"mode": "ok", "sold_out_dates": []},
    },
    {
        "product_id": "MOCK_PROD_NATURE_PARK_TICKET",
        "name_zh": "MOCK_生态公园入园券",
        "supplier_id": "MOCK_SUP_BETA",
        "poi_ids": ["MOCK_POI_NATURE_PARK"],
        "product_type": "attraction_ticket",
        "price_hint": {"adult": 42.0, "child": 21.0},
        "pax_rules": {"min_pax": 1, "max_pax": 15, "child_age_range": [3, 15], "infant_free_under": 3},
        "date_rules": {"bookable_from_days_ahead": 0, "bookable_until_days_ahead": 90, "blackout_dates": []},
        "refund_policy": {"refundable": True, "free_cancel_hours": 24},
        "requires_reservation": False,
        "mock_supplier_behavior": {"mode": "ok", "sold_out_dates": []},
    },
    {
        "product_id": "MOCK_PROD_TRAIN_MUSEUM_TICKET",
        "name_zh": "MOCK_铁道博物馆门票",
        "supplier_id": "MOCK_SUP_GAMMA",
        "poi_ids": ["MOCK_POI_TRAIN_MUSEUM"],
        "product_type": "attraction_ticket",
        "price_hint": {"adult": 58.0, "child": 29.0},
        "pax_rules": {"min_pax": 1, "max_pax": 10, "child_age_range": [3, 15], "infant_free_under": 3},
        "date_rules": {"bookable_from_days_ahead": 0, "bookable_until_days_ahead": 90, "blackout_dates": []},
        "refund_policy": {"refundable": True, "free_cancel_hours": 24},
        "requires_reservation": False,
        # 供应商侧正常；该商品用于验证**商户在营状态未知**这条硬性约束
        # （其商户 MOCK_MER_TRAIN_MUSEUM 的状态为 UNKNOWN），
        # 因此请求会在商户门禁处被拦下，根本不会到达供应商。
        "mock_supplier_behavior": {"mode": "ok", "sold_out_dates": []},
    },
    {
        "product_id": "MOCK_PROD_FAMILY_HOTEL_CHUO",
        "name_zh": "MOCK_中央区家庭房两晚",
        "supplier_id": "MOCK_SUP_ALPHA",
        "poi_ids": [],
        "product_type": "hotel",
        "price_hint": {"adult": 640.0, "child": 0.0},
        "pax_rules": {"min_pax": 2, "max_pax": 4, "child_age_range": [0, 12], "infant_free_under": 6},
        "date_rules": {"bookable_from_days_ahead": 0, "bookable_until_days_ahead": 300, "blackout_dates": []},
        "refund_policy": {"refundable": True, "free_cancel_hours": 24},
        "requires_reservation": False,
        "mock_supplier_behavior": {"mode": "ok", "sold_out_dates": []},
    },
    {
        "product_id": "MOCK_PROD_AIRPORT_TRANSFER",
        "name_zh": "MOCK_机场往返接送（含儿童安全座椅）",
        "supplier_id": "MOCK_SUP_BETA",
        "poi_ids": [],
        "product_type": "transfer",
        "price_hint": {"adult": 288.0, "child": 0.0},
        "pax_rules": {"min_pax": 1, "max_pax": 6, "child_age_range": [0, 12], "infant_free_under": 2},
        "date_rules": {"bookable_from_days_ahead": 1, "bookable_until_days_ahead": 180, "blackout_dates": []},
        "refund_policy": {"refundable": True, "free_cancel_hours": 24},
        "requires_reservation": True,
        "mock_supplier_behavior": {"mode": "ok", "sold_out_dates": []},
    },
]

# ---------------------------------------------------------------- 内容语料

CONTENT_SEEDS: list[dict[str, Any]] = [
    {
        "poi_id": "MOCK_POI_THEMEPARK",
        "title": "MOCK_主题乐园亲子动线建议",
        "body": "MOCK_合成文案：建议开园即入，先完成儿童区项目，午后安排室内演出避开日晒。"
        "带 4-11 岁儿童建议预留 6-7 小时，园内餐厅需排队。",
        "tags": ["亲子", "户外", "半日以上"],
        "source_type": "merchandise_copy",
        "updated_days_ago": 21,
    },
    {
        "poi_id": "MOCK_POI_AQUARIUM",
        "title": "MOCK_海洋馆参观须知",
        "body": "MOCK_合成文案：单向参观动线，约 2-2.5 小时。婴儿车可推入，馆内设哺乳室。"
        "每年 11 月下旬有 1 天例行检修闭馆。",
        "tags": ["亲子", "室内", "雨天备选"],
        "source_type": "destination_kb",
        "updated_days_ago": 40,
    },
    {
        "poi_id": "MOCK_POI_KIDS_SCIENCE",
        "title": "MOCK_儿童科学馆年龄适配说明",
        "body": "MOCK_合成文案：展项面向 3-15 岁设计，12 岁以下须成人陪同。周一固定闭馆。",
        "tags": ["亲子", "室内", "教育"],
        "source_type": "destination_kb",
        "updated_days_ago": 65,
    },
    {
        "poi_id": "MOCK_POI_BRICK_CENTER",
        "title": "MOCK_积木中心入场限制",
        "body": "MOCK_合成文案：仅接待携带 2-12 岁儿童的家庭，成人不可单独入场；"
        "身高低于 85cm 的幼儿不建议参与主体项目。需提前 2 天预约时段。",
        "tags": ["亲子", "室内", "需预约"],
        "source_type": "merchandise_copy",
        "updated_days_ago": 9,
    },
    {
        "poi_id": "MOCK_POI_CASTLE_PARK",
        "title": "MOCK_城址公园步行强度提示",
        "body": "MOCK_合成文案：园区较大，天守阁内以楼梯为主，推婴儿车不便。"
        "建议控制在 2 小时内，夏季注意补水。",
        "tags": ["户外", "历史"],
        "source_type": "destination_kb",
        # 故意设为过期：验证「资料过期须明确提示」。
        "updated_days_ago": 400,
    },
    {
        "poi_id": "MOCK_POI_SKY_DECK",
        "title": "MOCK_观景台最佳时段",
        "body": "MOCK_合成文案：日落前 40 分钟入场可同时看到白天与夜景。顶层为半露天，有风。",
        "tags": ["室内", "夜景"],
        "source_type": "merchandise_copy",
        "updated_days_ago": 15,
    },
    {
        "poi_id": "MOCK_POI_FOOD_STREET",
        "title": "MOCK_小吃街带娃注意事项",
        "body": "MOCK_合成文案：晚间人流密集，建议 17:00 前抵达，多数摊位不提供儿童座椅。",
        "tags": ["餐饮", "户外"],
        "source_type": "destination_kb",
        "updated_days_ago": 30,
    },
    {
        "poi_id": "MOCK_POI_NATURE_PARK",
        "title": "MOCK_生态公园游玩时长",
        "body": "MOCK_合成文案：全程步行约 3 小时，周三闭园。园内有大型儿童游具区。",
        "tags": ["亲子", "户外"],
        "source_type": "destination_kb",
        "updated_days_ago": 52,
    },
    {
        "poi_id": "MOCK_POI_TRAIN_MUSEUM",
        "title": "MOCK_铁道博物馆动线",
        "body": "MOCK_合成文案：模型演示每小时一场，建议对准场次入场。周四闭馆。",
        "tags": ["亲子", "室内"],
        "source_type": "merchandise_copy",
        "updated_days_ago": 11,
    },
    {
        "poi_id": "MOCK_POI_FERRIS_WHEEL",
        "title": "MOCK_摩天轮运营时间（存在冲突版本）",
        "body": "MOCK_合成文案：本文案记录营业至 21:00，与目的地知识库记录的 22:00 不一致，"
        "属于需人工核对的冲突数据样例。",
        "tags": ["户外", "冲突样例"],
        "source_type": "merchandise_copy",
        "updated_days_ago": 5,
        "conflicts_with_poi_hours": True,
    },
]


def _shift_date(anchor: str, days: int) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(anchor) - timedelta(days=days)).isoformat()


def _forward_date(anchor: str, days: int) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(anchor) + timedelta(days=days)).isoformat()


def _build_merchants(pois: list[dict[str, Any]], rng: random.Random) -> list[dict[str, Any]]:
    """由 POI 派生景区类商户，再并入非 POI 类商户。"""
    merchants: list[dict[str, Any]] = []

    for poi in pois:
        override = MERCHANT_OVERRIDES.get(poi["poi_id"], {})
        status = dict(
            override.get(
                "operating_status",
                {"status": "OPEN", "note": None, "status_age_seconds": rng.randint(20, 200)},
            )
        )
        rating = dict(
            override.get(
                "rating",
                {
                    "overall": round(3.8 + (poi["kid_fit_score"] % 12) / 10.0, 1),
                    "review_count": 200 + poi["popularity_score"] * 7,
                    "grade": "A" if poi["popularity_score"] >= 85 else "B",
                },
            )
        )
        merchants.append(
            {
                "merchant_id": f"MOCK_MER_{poi['poi_id'].removeprefix('MOCK_POI_')}",
                "name_zh": f"{poi['name_zh']}运营方",
                "category": poi["category"],
                "region_id": poi["region_id"],
                "poi_id": poi["poi_id"],
                "address_mock": poi["address_mock"],
                "coordinates": poi["coordinates"],
                "business_hours": poi["opening_hours"],
                "operating_status": {
                    **status,
                    # 时效要求 ≤5min（清单 #2）；status_age_seconds 由消费侧判定是否过期。
                    "freshness_requirement_seconds": 300,
                    "source": "供应链系统",
                },
                "rating": {
                    **rating,
                    # T+1 日级（清单 #5）。
                    "updated_at": f"{_shift_date(ANCHOR_DATE, 1)}T02:00:00Z",
                    "source": "质量运营团队",
                },
                "product_ids": sorted(poi.get("linked_product_ids") or []),
            }
        )

    for extra in STANDALONE_MERCHANTS:
        merchants.append(
            {
                **extra,
                "poi_id": None,
                "address_mock": f"MOCK_中央区示例地址{rng.randint(1, 9)}-{rng.randint(1, 20)}",
                "coordinates": {"lat": 34.666, "lng": 135.500, "coordinates_mock": True},
                "operating_status": {
                    **extra["operating_status"],
                    "freshness_requirement_seconds": 300,
                    "source": "供应链系统",
                },
                "rating": {
                    **extra["rating"],
                    "updated_at": f"{_shift_date(ANCHOR_DATE, 1)}T02:00:00Z",
                    "source": "质量运营团队",
                },
            }
        )

    return sorted(merchants, key=lambda m: m["merchant_id"])


def _build_weather(rng: random.Random) -> list[dict[str, Any]]:
    """逐日天气预报。确定性生成，温度随月份呈季节趋势。"""
    from datetime import date as _date

    forecasts: list[dict[str, Any]] = []
    monthly = {entry["month"]: entry["avg_temp_c"] for entry in SEASONALITY}

    for offset in range(WEATHER_DAYS):
        day_str = _forward_date(ANCHOR_DATE, offset)
        day = _date.fromisoformat(day_str)
        low, high = monthly[day.month]
        jitter = rng.randint(-2, 2)

        key, label, prob_low, prob_high = _WEATHER_CONDITIONS[rng.randrange(len(_WEATHER_CONDITIONS))]
        forecasts.append(
            {
                "destination_id": "MOCK_DEST_OSAKA",
                "date": day_str,
                "condition": key,
                "condition_label": label,
                "temp_min_c": low + jitter,
                "temp_max_c": high + jitter,
                "precipitation_probability": rng.randint(prob_low, prob_high),
                "source": {"name": "MOCK_第三方天气 API", "type": "third_party_api", "refresh": "daily"},
                "updated_at": f"{ANCHOR_DATE}T06:00:00Z",
            }
        )
    return forecasts


def build_dataset() -> dict[str, Any]:
    rng = random.Random(SEED)

    transit: dict[str, int] = {}
    for (a, b), minutes in sorted(TRANSIT_PAIRS.items()):
        transit[f"{a}|{b}"] = minutes
        transit[f"{b}|{a}"] = minutes
    for region in REGIONS:
        transit[f"{region['region_id']}|{region['region_id']}"] = INTRA_REGION_MINUTES

    pois: list[dict[str, Any]] = []
    region_names = {r["region_id"]: r["name_zh"] for r in REGIONS}
    region_coords = {r["region_id"]: r["coordinates"] for r in REGIONS}
    for poi in POIS:
        enriched = dict(poi)
        enriched["region_name"] = region_names[poi["region_id"]]
        enriched["address_mock"] = f"MOCK_{region_names[poi['region_id']][5:]}示例地址{rng.randint(1, 9)}-{rng.randint(1, 20)}"
        enriched["destination_id"] = "MOCK_DEST_OSAKA"
        # 坐标 = 区域中心 + 确定性小偏移；标记 coordinates_mock 以免被当成真实门址。
        base = region_coords[poi["region_id"]]
        enriched["coordinates"] = {
            "lat": round(base["lat"] + rng.randint(-30, 30) / 1000.0, 4),
            "lng": round(base["lng"] + rng.randint(-30, 30) / 1000.0, 4),
            "coordinates_mock": True,
        }
        enriched["merchant_id"] = f"MOCK_MER_{poi['poi_id'].removeprefix('MOCK_POI_')}"
        enriched["data_source"] = {
            "name": "MOCK_目的地知识库",
            "type": "destination_kb",
            "url": f"https://mock.example.invalid/kb/{poi['poi_id'].lower()}",
            "license": "authorized_internal",
            "updated_at": f"{_shift_date(ANCHOR_DATE, rng.randint(5, 90))}T00:00:00Z",
        }
        pois.append(enriched)

    products: list[dict[str, Any]] = []
    supplier_names = {s["supplier_id"]: s["name_zh"] for s in SUPPLIERS}
    poi_index = {p["poi_id"]: p for p in pois}
    poi_to_merchant = {p["poi_id"]: p["merchant_id"] for p in pois}
    for product in PRODUCTS:
        enriched = dict(product)
        enriched["supplier_name"] = supplier_names[product["supplier_id"]]
        enriched["currency"] = "CNY"
        # 清单 #3 要求产品含「时长」：取关联 POI 的建议游览时长，无关联则为 None。
        linked = [poi_id for poi_id in product["poi_ids"] if poi_id in poi_index]
        enriched["duration_minutes"] = (
            poi_index[linked[0]]["typical_duration_minutes"] if linked else None
        )
        enriched["merchant_ids"] = sorted(
            {poi_to_merchant[poi_id] for poi_id in product["poi_ids"] if poi_id in poi_to_merchant}
        ) or _standalone_merchant_ids(product["product_id"])
        enriched["price_hint"] = {
            **product["price_hint"],
            "note": "MOCK_历史参考价，仅用于展示排序；实际报价以供应商实时查询为准。",
        }
        enriched["data_source"] = {
            "name": "MOCK_商品文案库",
            "type": "merchandise_copy",
            "url": f"https://mock.example.invalid/catalog/{product['product_id'].lower()}",
            "license": "authorized_internal",
            "updated_at": f"{_shift_date(ANCHOR_DATE, rng.randint(1, 60))}T00:00:00Z",
        }
        products.append(enriched)

    documents: list[dict[str, Any]] = []
    for index, seed in enumerate(CONTENT_SEEDS, start=1):
        poi = poi_index[seed["poi_id"]]
        documents.append(
            {
                "doc_id": f"MOCK_DOC_{index:04d}",
                "poi_id": seed["poi_id"],
                "destination": "大阪",
                "title": seed["title"],
                "body": seed["body"],
                "tags": seed["tags"],
                "keywords": sorted({poi["name_zh"], poi["category"], *seed["tags"]}),
                "source": {
                    "name": "MOCK_商品文案库" if seed["source_type"] == "merchandise_copy" else "MOCK_目的地知识库",
                    "type": seed["source_type"],
                    "url": f"https://mock.example.invalid/doc/MOCK_DOC_{index:04d}",
                    "license": "authorized_internal",
                },
                "updated_at": f"{_shift_date(ANCHOR_DATE, seed['updated_days_ago'])}T00:00:00Z",
                "conflicts_with_poi_hours": bool(seed.get("conflicts_with_poi_hours", False)),
            }
        )

    merchants = _build_merchants(pois, rng)
    weather = _build_weather(rng)

    return {
        "meta": {
            "dataset_version": "2.0.0",
            "generator": "tools/generate_mock_data.py",
            "seed": SEED,
            "anchor_date": ANCHOR_DATE,
            "data_classification": "SYNTHETIC_MOCK_ONLY",
            "notice": "全部为合成数据，不含任何真实客户数据、真实个人信息或真实支付信息。",
            "weekday_convention": "0=Monday ... 6=Sunday",
            "aligned_data_inventory": "数据清单-v3.md（9 项核心数据项全覆盖）",
        },
        "destinations": DESTINATIONS,
        "regions": REGIONS,
        "transit_minutes": transit,
        "pois": pois,
        "merchants": merchants,
        "suppliers": SUPPLIERS,
        "products": products,
        "documents": documents,
        "weather": weather,
        "seasonality": SEASONALITY,
    }


def _standalone_merchant_ids(product_id: str) -> list[str]:
    """非 POI 类商品（住宿、用车）的商户归属。"""
    return sorted(
        merchant["merchant_id"]
        for merchant in STANDALONE_MERCHANTS
        if product_id in merchant["product_ids"]
    )


# ---------------------------------------------------------------- 自检

_FORBIDDEN_SUBSTRINGS = ("@gmail.", "@qq.com", "@163.com", "+86 1", "http://", "13800", "18888")


def validate_dataset(dataset: dict[str, Any]) -> list[str]:
    """合规自检：任何一条不通过都应阻断交付。"""
    problems: list[str] = []
    blob = json.dumps(dataset, ensure_ascii=False)

    for token in _FORBIDDEN_SUBSTRINGS:
        if token in blob:
            problems.append(f"数据集包含疑似真实/不合规内容片段：{token!r}")

    for poi in dataset["pois"]:
        if not poi["poi_id"].startswith("MOCK_"):
            problems.append(f"POI 标识缺少 MOCK_ 前缀：{poi['poi_id']}")
        if not poi["name_zh"].startswith("MOCK_"):
            problems.append(f"POI 名称缺少 MOCK_ 前缀：{poi['name_zh']}")

    product_ids = {p["product_id"] for p in dataset["products"]}
    for poi in dataset["pois"]:
        for pid in poi["linked_product_ids"]:
            if pid not in product_ids:
                problems.append(f"POI {poi['poi_id']} 引用了不存在的商品 {pid}")

    poi_ids = {p["poi_id"] for p in dataset["pois"]}
    for product in dataset["products"]:
        for pid in product["poi_ids"]:
            if pid not in poi_ids:
                problems.append(f"商品 {product['product_id']} 引用了不存在的 POI {pid}")
        if product["mock_supplier_behavior"]["mode"] not in {
            "ok",
            "sold_out",
            "timeout",
            "partial",
            "error",
            "date_based",
        }:
            problems.append(f"商品 {product['product_id']} 的 mock 行为模式非法")

    for doc in dataset["documents"]:
        if doc["poi_id"] not in poi_ids:
            problems.append(f"文档 {doc['doc_id']} 引用了不存在的 POI {doc['poi_id']}")
        if ".invalid" not in doc["source"]["url"]:
            problems.append(f"文档 {doc['doc_id']} 的来源 URL 未使用保留域 .invalid")

    # 必须覆盖全部可订异常场景，否则「不可订与接口异常处理」无法被验证。
    modes = {p["mock_supplier_behavior"]["mode"] for p in dataset["products"]}
    for required in ("ok", "sold_out", "timeout", "partial", "error"):
        if required not in modes:
            problems.append(f"数据集缺少 {required} 场景商品，无法验证可订状态语义")

    problems.extend(_validate_inventory_coverage(dataset))
    return problems


def _validate_inventory_coverage(dataset: dict[str, Any]) -> list[str]:
    """对齐《数据清单-v3.md》9 项核心数据项的覆盖度自检。"""
    problems: list[str] = []

    # #1/#2/#5 商户信息、在营状态、评分
    merchant_ids = {m["merchant_id"] for m in dataset["merchants"]}
    for merchant in dataset["merchants"]:
        status = merchant.get("operating_status") or {}
        if status.get("status") not in {"OPEN", "SUSPENDED", "CLOSED_PERMANENTLY", "UNKNOWN"}:
            problems.append(f"商户 {merchant['merchant_id']} 的在营状态枚举非法：{status.get('status')}")
        if status.get("freshness_requirement_seconds") != 300:
            problems.append(f"商户 {merchant['merchant_id']} 未声明 ≤5min 的在营状态时效要求")
        rating = merchant.get("rating") or {}
        for key in ("overall", "review_count", "grade", "updated_at"):
            if key not in rating:
                problems.append(f"商户 {merchant['merchant_id']} 评分缺少字段 {key}")
        if not merchant.get("coordinates"):
            problems.append(f"商户 {merchant['merchant_id']} 缺少经纬度")

    # 在营状态必须覆盖「暂停」「未知」「过期」三种不可直接采信的情形，
    # 否则「硬性约束：过滤不可用商户」无法被验证。
    statuses = {(m.get("operating_status") or {}).get("status") for m in dataset["merchants"]}
    for required in ("OPEN", "SUSPENDED", "UNKNOWN"):
        if required not in statuses:
            problems.append(f"数据集缺少在营状态为 {required} 的商户，无法验证硬性约束")
    if not any(
        (m.get("operating_status") or {}).get("status_age_seconds", 0) > 300 for m in dataset["merchants"]
    ):
        problems.append("数据集缺少在营状态已过期（>5min）的商户，无法验证时效判定")

    # #3 产品需含时长
    for product in dataset["products"]:
        if "duration_minutes" not in product:
            problems.append(f"商品 {product['product_id']} 缺少 duration_minutes")
        for mid in product.get("merchant_ids") or []:
            if mid not in merchant_ids:
                problems.append(f"商品 {product['product_id']} 引用了不存在的商户 {mid}")

    # #6 目的地
    destination_ids = {d["destination_id"] for d in dataset["destinations"]}
    for destination in dataset["destinations"]:
        for key in ("name_zh", "country_code", "currency", "coordinates", "timezone"):
            if key not in destination:
                problems.append(f"目的地 {destination['destination_id']} 缺少字段 {key}")

    # #7 POI 需含坐标与游览时长
    for poi in dataset["pois"]:
        if not poi.get("coordinates"):
            problems.append(f"POI {poi['poi_id']} 缺少经纬度")
        if not poi.get("typical_duration_minutes"):
            problems.append(f"POI {poi['poi_id']} 缺少建议游览时长")
        if poi.get("destination_id") not in destination_ids:
            problems.append(f"POI {poi['poi_id']} 的 destination_id 无效")
        if poi.get("merchant_id") not in merchant_ids:
            problems.append(f"POI {poi['poi_id']} 的 merchant_id 无效")

    # #8 天气
    if len(dataset["weather"]) < 30:
        problems.append("天气预报覆盖天数不足 30 天")
    conditions = {w["condition"] for w in dataset["weather"]}
    for required in ("sunny", "light_rain", "heavy_rain"):
        if required not in conditions:
            problems.append(f"天气数据缺少 {required} 场景，无法验证雨天室内备选逻辑")
    for forecast in dataset["weather"]:
        if not 0 <= forecast["precipitation_probability"] <= 100:
            problems.append(f"天气 {forecast['date']} 的降雨概率超出 0-100")
        if forecast["temp_min_c"] > forecast["temp_max_c"]:
            problems.append(f"天气 {forecast['date']} 的最低温高于最高温")

    # #9 季节与事件
    months = {entry["month"] for entry in dataset["seasonality"]}
    if months != set(range(1, 13)):
        problems.append("季节数据未覆盖全部 12 个月")
    for entry in dataset["seasonality"]:
        if entry["peak_level"] not in {"low", "shoulder", "peak"}:
            problems.append(f"{entry['month']} 月的淡旺季枚举非法：{entry['peak_level']}")

    return problems


FILES = (
    "destinations",
    "regions",
    "transit_minutes",
    "pois",
    "merchants",
    "suppliers",
    "products",
    "documents",
    "weather",
    "seasonality",
    "meta",
)


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 CDE Mock 数据集")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出目录")
    parser.add_argument("--check", action="store_true", help="仅自检，不写盘")
    args = parser.parse_args()

    dataset = build_dataset()
    problems = validate_dataset(dataset)
    if problems:
        for problem in problems:
            print(f"[FAIL] {problem}", file=sys.stderr)
        return 1

    if args.check:
        print(f"[OK] 数据集自检通过：{len(dataset['pois'])} 个 POI / {len(dataset['products'])} 个商品 / {len(dataset['documents'])} 篇文档")
        return 0

    args.out.mkdir(parents=True, exist_ok=True)
    for key in FILES:
        path = args.out / f"{key}.json"
        payload = json.dumps(dataset[key], ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path.write_text(payload, encoding="utf-8")
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        print(f"[OK] {path}  sha256:{digest}")

    print(f"[OK] 数据集自检通过，seed={SEED}，可复现。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
