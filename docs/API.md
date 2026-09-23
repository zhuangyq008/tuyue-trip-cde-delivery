# 接口契约

> **状态说明**：截至本版本，尚未收到决策人下发的接口规范文件。
> 本文是按需求文档与非功能规范**自拟**的契约，字段名、状态码与路径均已做到内部一致，
> 并把契约层做成了 schema 驱动（`src/app/http/validation.py` + `src/app/api/*`）——
> 规范文件到位后，对齐只需改字段定义，不必改架构。
>
> 规范 §4.3 要求「用例与规范文件逐字对齐」。收到文件后请以该文件为准，
> 并同步更新本文与 `tests/test_http_contract.py`。

- **基地址**：`https://{api-id}.execute-api.{region}.amazonaws.com`
- **Stage**：`$default`，**无路径前缀**，基地址后直接拼接下表路径
- **Content-Type**：请求与响应均为 `application/json`
- **鉴权**：所有路由（含 `/v1/health`）均需 `Authorization: Bearer <token>`

## 1. 鉴权与状态码语义

| 场景 | 状态码 | `error_code` | 由谁产出 |
|---|---|---|---|
| 未带 `Authorization` 头 | `401` | `UNAUTHORIZED` | API Gateway（identitySource 缺失，不调用 Authorizer） |
| 令牌无效 | `401` | `UNAUTHORIZED` | 业务 Lambda（Authorizer 透传结论） |
| 令牌有效但缺少 scope | `403` | `FORBIDDEN` | 业务 Lambda |
| 请求体非合法 JSON / 非对象 | `400` | `BAD_REQUEST` | 业务 Lambda |
| 路径不存在 | `404` | `NOT_FOUND` | 业务 Lambda（应用内路由器） |
| 资源不存在 | `404` | `NOT_FOUND` | 业务 Lambda |
| 方法不支持 | `405` | `METHOD_NOT_ALLOWED` | 业务 Lambda（带 `Allow` 头） |
| 幂等键冲突 | `409` | `IDEMPOTENCY_CONFLICT` | 业务 Lambda |
| **字段语义非法** | `422` | `VALIDATION_FAILED` | **业务 Lambda**（网关模型校验只给 400，拿不到 422） |
| 内部依赖异常 | `502` | `UPSTREAM_ERROR` | 业务 Lambda |
| 未预期异常 | `500` | `INTERNAL_ERROR` | 业务 Lambda（不含堆栈） |

以上为默认模式 `AuthMode=handler_decide`，符合 RFC 7235。
切换到 `AuthMode=authorizer_deny` 后，「令牌无效」改由 Lambda Authorizer 返回 Deny 策略，
网关输出 `403`（响应体为网关默认体）。

### 错误响应体

顶层字段名为 `message`，与 API Gateway 自身生成的 `{"message": "Unauthorized"}` **同名**，
使网关兜底错误与业务错误在字段层面完全一致，调用方无需分辨错误来自哪一层。

```json
{
  "message": "请求参数校验未通过。",
  "error_code": "VALIDATION_FAILED",
  "request_id": "abcd-1234",
  "errors": [
    { "field": "days", "reason": "类型应为整数，实际为 str", "expected": "integer" }
  ]
}
```

### 成功响应体

所有成功响应都附带 `request_id` 与 `delivery_notice`（交付边界声明，规范 §1）。

## 2. 「缺失」与「非法」的边界

这条是理解本 API 的关键。默认行为与唯一例外：

| 情况 | 默认处理 | 理由 |
|---|---|---|
| 必填字段**缺失** | `422` + 逐字段错误 | 安全的行为必须是默认行为 |
| 字段**存在但非法** | `422` + 逐字段错误 | 类型、范围、枚举、日期逻辑错误 |
| 字段**未定义** | `422` | 拼错字段名不应被静默忽略 |

**唯一例外：`POST /v1/plans`。** 该入口的必填字段缺失返回
`200` + `status: "needs_clarification"` + 追问清单——缺信息是正常的对话状态，
不是客户端错误（需求文档 §4.3 P0「缺少关键条件时先追问」）。
该入口的字段**存在但非法**仍然是 `422`。

这个例外在代码里是显式声明的（`Validator(body, missing_as_error=False)`），
不是靠调用方自觉补检查。早先的实现反了过来——默认只记录缺失项、
要求每个入口自己转成错误，结果两个入口漏了，必填数组整个缺失时被当成空数组静默放过。

## 3. 行程方案

### POST /v1/plans

权限：`write`。**始终返回 201**（含重放），用 `replayed` 区分首建与重放——
保证判分脚本重跑时状态码恒定。

请求：

```json
{
  "destination": "大阪",
  "start_date": "2026-11-20",
  "days": 2,
  "party": { "adults": 2, "children": 1, "child_ages": [5] },
  "preferences": {
    "pace": "standard",
    "interests": ["亲子"],
    "budget_cny_per_person": 2000,
    "hotel_region_id": "MOCK_REG_CHUO"
  },
  "options": { "check_availability": true, "include_narrative": true }
}
```

| 字段 | 类型 | 必填 | 约束 |
|---|---|---|---|
| `destination` | string | 是 | 枚举，当前仅 `大阪`（别名 `Osaka`/`osaka`/`OSAKA`） |
| `start_date` | string | 是 | `YYYY-MM-DD`，不早于今日（JST），不晚于今日 +365 天 |
| `days` | integer | 是 | 1–7 |
| `party.adults` | integer | 是 | 0–9 |
| `party.children` | integer | 否 | 0–9，默认 0 |
| `party.child_ages` | array\<integer\> | 条件必填 | 元素 0–17；`children > 0` 时必填，且长度须等于 `children` |
| `preferences.pace` | string | 否 | `relaxed` \| `standard` \| `packed`，默认 `standard` |
| `preferences.interests` | array\<string\> | 否 | 最多 10 项 |
| `preferences.budget_cny_per_person` | integer | 否 | 0–1000000 |
| `preferences.hotel_region_id` | string | 否 | 须为有效 `region_id` |
| `options.check_availability` | boolean | 否 | 默认 `true`；为 `false` 时全部商品的可订状态为 `UNKNOWN` |
| `options.include_narrative` | boolean | 否 | 默认 `true` |

跨字段规则：`adults + children` 须在 1–12 之间。

响应（`201`）主要结构：

```json
{
  "plan_id": "MOCK-PLAN-XXXXXXXXXXXX",
  "status": "planned",
  "replayed": false,
  "created_at": "2026-11-18T01:00:00Z",
  "request_echo": { "...": "回显解析后的请求" },
  "itinerary": {
    "pace_profile": { "name": "standard", "day_start": "09:00", "...": "..." },
    "days": [
      {
        "day_index": 1,
        "date": "2026-11-20",
        "weekday_label": "周五",
        "primary_region_id": "MOCK_REG_CHUO",
        "items": [
          { "type": "transit",    "from_region_name": "...", "duration_minutes": 12, "transit_duration_known": true },
          { "type": "attraction", "poi_id": "...", "start_time": "09:12", "end_time": "11:12",
            "is_indoor": true, "requires_reservation": false,
            "recommend_reason": "推荐依据由结构化字段拼装，不由模型生成",
            "linked_product_ids": ["..."] },
          { "type": "meal", "duration_minutes": 60 },
          { "type": "rest", "note": "低龄儿童（<6 岁）午后休息缓冲" }
        ],
        "weather": { "available": true, "precipitation_probability": 12, "prefers_indoor": false,
                     "freshness": { "tier": "daily", "is_fresh": true, "blocks_usage": false } },
        "weather_advisories": [],
        "totals": { "attraction_count": 3, "transit_minutes": 36, "active_minutes": 520 }
      }
    ],
    "summary": { "total_days": 2, "regions_covered": ["..."], "days_with_weather_data": 2 },
    "excluded_by_merchant_status": [
      { "poi_id": "MOCK_POI_FERRIS_WHEEL", "reason": "经营商户 ... 当前状态为 SUSPENDED。",
        "constraint": "数据清单 #2 商户在营状态（硬性约束，时效 ≤5min）" }
    ]
  },
  "validation": {
    "executable": true,
    "issue_counts": { "blocker": 0, "warning": 2, "unverified": 1 },
    "issues": [
      { "code": "RESERVATION_REQUIRED", "severity": "warning", "message": "...", "suggestion": "..." }
    ],
    "checked_rules": ["商户在营状态（数据清单 #2，硬性约束）", "指定日期闭馆", "..."],
    "statement": "已通过上述规则校验，未发现阻断项；仍以出行前实际公告为准。"
  },
  "product_mapping": { "mapping_method": "explicit_catalog_link", "entries": ["..."], "summary": { "...": "..." } },
  "narrative": { "generated_by": "deterministic_template", "text": "...", "fact_source_note": "..." },
  "booking_handoff": {
    "bookable_entry_count": 3,
    "blocked_entry_count": 4,
    "entries": [{ "binding": { "product_id": "...", "travel_date": "...", "adults": 2, "child_ages": [5] } }],
    "blocked": [{ "availability_state": "SOLD_OUT", "reason": "..." }],
    "constraints": ["本助手不代替用户下单或付款。", "一次可订查询不等于预订成功……"]
  },
  "delivery_notice": { "maturity": "CDE 验证原型，非 production-ready；……" }
}
```

信息不全时（`200`）：

```json
{
  "status": "needs_clarification",
  "missing_fields": ["start_date", "days", "party.adults"],
  "questions": [
    { "field": "start_date", "question": "计划哪天出发？（格式 YYYY-MM-DD）",
      "why": "开放时间、闭馆日与库存都按具体日期判定，不能用「大概下个月」代替。" }
  ],
  "assumed_defaults": { "pace": "standard", "interests": [], "budget_cny_per_person": null, "hotel_region_id": null },
  "privacy_note": "仅收集行程履约必需信息；不收集姓名、证件、手机号与支付信息。"
}
```

### GET /v1/plans/{plan_id}

权限：`read`。不存在或已过期 → `404`。

### POST /v1/plans/{plan_id}/revalidate

权限：`read`。请求体可选：`{ "check_availability": true }`。

存在的意义：商户在营状态（≤5min）与库存（实时）都是时效数据，
**「创建时校验通过」不等于「现在还走得通」**。响应含 `comparison` 块对比两次结论。

## 4. 动态可订查询

### POST /v1/availability/queries

权限：`read`。批量查询，单条商品失败**不会**让整个请求失败。

```json
{
  "items": [
    { "product_id": "MOCK_PROD_AQUARIUM_TICKET", "travel_date": "2026-11-20" },
    { "product_id": "MOCK_PROD_KIDS_SCIENCE_TICKET", "travel_date": "2026-11-20" }
  ],
  "party": { "adults": 2, "children": 1, "child_ages": [5] },
  "use_cache": true
}
```

`items` 最多 20 条。响应中每条结果的结构：

```json
{
  "product_id": "MOCK_PROD_KIDS_SCIENCE_TICKET",
  "availability_state": "UNCONFIRMED",
  "bookable": false,
  "reason_code": "SUPPLIER_TIMEOUT",
  "reason": "供应商查询超时，暂无法确认是否可订：……",
  "checked_at": "2026-11-18T01:00:00Z",
  "expires_at": "2026-11-18T01:02:00Z",
  "snapshot_ttl_seconds": 120,
  "from_cache": false,
  "fact_source": "not_established",
  "disclaimer": "本结果为查询时点的供应商状态快照，不构成锁价或库存保留；……"
}
```

`availability_state == "AVAILABLE"` 时额外含 `price`、`remaining_inventory`、`quote_valid_seconds`。
**非 `AVAILABLE` 的条目不携带 `price`**——不可订就不该有报价出现在展示层。

多商户商品部分场馆停业时，额外含 `merchant_advisories`（`severity: "advisory"`，不影响 `bookable`）。

响应 `summary.invariant` 字段固定声明：
`bookable == true 当且仅当 availability_state == 'AVAILABLE'；其余四态一律不可订。`

### GET /v1/products/{product_id}/availability

权限：`read`。查询串：`travel_date`（必填）、`adults`（必填）、`children`、`child_ages`（逗号分隔，如 `5,8`）。

与批量查询的语义差异：**单资源形态下商品不存在 → `404`**（批量查询则返回 `NOT_ELIGIBLE` 条目）。

## 5. 预订意图

### POST /v1/bookings

权限：`write`。**始终返回 201**，用 `intent_status` 区分结果。

```json
{
  "plan_id": "MOCK-PLAN-XXXXXXXXXXXX",
  "product_id": "MOCK_PROD_AQUARIUM_TICKET",
  "travel_date": "2026-11-20",
  "party": { "adults": 2, "children": 1, "child_ages": [5] },
  "session_id": "MOCK_SESSION_0001"
}
```

可选请求头 `Idempotency-Key`：同键不同内容 → `409`；同键同内容 → 重放原响应并带 `Idempotency-Replayed: true`。

创建意图时**强制绕过缓存**向供应商重新查询（`reverification.bypassed_cache: true`）。

| `intent_status` | 含义 | `checkout` |
|---|---|---|
| `READY_FOR_CHECKOUT` | 重新确认后仍可订 | 含 `handoff_url` 与 `required_reconfirmation` |
| `REJECTED_NOT_BOOKABLE` | 重新确认后不可订 | **`null`**——不可订就不该有下单路径 |

无论哪种状态，响应都含：

```json
{
  "payment":    { "performed_by_assistant": false, "note": "本助手不代替用户下单或付款；……" },
  "guarantees": { "price_locked": false, "inventory_held": false, "booking_confirmed": false }
}
```

### GET /v1/bookings/{booking_id}

权限：`read`。不存在或已过期 → `404`。

## 6. 目录、内容与参考数据

| 路径 | 说明 |
|---|---|
| `GET /v1/catalog` | 目录概览与数据集元信息（含 `data_classification`、`seed`） |
| `GET /v1/products/{id}` | 商品静态资料。**不含实时价格与库存**，响应内 `dynamic_fields_notice` 说明原因与查询方式 |
| `GET /v1/pois/{id}` | 景点资料 + 绑定商品 + 关联内容来源 |
| `GET /v1/content` | 内容检索。查询串 `q`、`poi_id`、`tags`（逗号分隔）、`limit`（1–50） |
| `GET /v1/data-inventory` | 《数据清单-v3.md》9 项覆盖度、时效分级与执行口径 |
| `GET /v1/destinations` | 目的地（清单 #6） |
| `GET /v1/destinations/{id}/seasonality` | 季节气候与事件（清单 #9） |
| `GET /v1/merchants/{id}` | 商户信息 + 在营状态**时效判定结论** + `usable_for_recommendation` |
| `GET /v1/weather` | 天气预报。查询串 `destination_id`、`start_date`（必填）、`days`（1–14） |

`GET /v1/content` 的每条结果必带 `source`、`updated_at`、`freshness`（`fresh`/`stale`/`unknown`）；
过期或冲突时附 `notices` 明确提示，系统**不自行裁决**哪份数据为准。

`GET /v1/weather` 缺数据的日期列在 `missing_dates`，**不用邻近日期或默认晴天填充**。

`poi_id` 过滤条件指向不存在的景点 → `422`（参数非法），而非 `404`（本资源不存在）。

## 7. 漏斗埋点

### POST /v1/events

权限：`write`。返回 `201`。

```json
{
  "session_id": "MOCK_SESSION_0001",
  "events": [
    { "step": "product_click", "plan_id": "...", "product_id": "...", "poi_id": "..." }
  ]
}
```

`step` 枚举（顺序即漏斗层级）：
`guide_view` → `product_impression` → `product_click` → `availability_checked` → `booking_intent` → `payment_success`

隐私约束：字段白名单之外一律 `422`；只接受调用方自带的匿名 `session_id`；全部事件带 TTL。

### GET /v1/funnel

权限：`read`。返回漏斗步骤定义、主指标口径与护栏指标清单。
`primary_metric.baseline` 固定为「待确认」——客户尚未提供基线，原型不填预测值。

## 8. 运维

| 路径 | 说明 |
|---|---|
| `GET /v1/health` | 状态、数据集元信息、能力自描述、当前令牌的 subject 与 scopes |
| `GET /v1/routes` | 全部路由与所需权限；用于确认路径拼接正确（规范 §3.1-4） |

两者**同样需要令牌**。留一个匿名探活端点等于给出一条绕过鉴权的路径，
而「未带令牌是否被拒」是规范 §4.2 的判分必测项。
