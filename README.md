# 途悦旅行 AI 旅行助手 — CDE 验证原型

> **交付边界声明**（《CDE-非功能交付规范》§1 要求置于交付物首页）

| 边界 | 口径 |
|---|---|
| **成熟度** | 本交付物是 **CDE 验证原型，非 production-ready**。上生产前由客户走自身测试评估流程。 |
| **排他性** | CDE 是**非排他的联合探索**，不做独家约定。 |
| **保密** | 客户业务细节严格保密，可写入条款。 |
| **承诺** | **不承诺**未经确认的预算、排期、SLA，也不承诺转化率提升或商业收益。 |
| **数据** | 全部为**合成 mock 数据**。未接入客户任何生产系统（含只读连接、只读副本、生产库快照）。不含真实姓名、证件号、手机号、地址、支付信息。 |
| **部署** | 部署在 AWS 侧**自有账户**，不占用客户账户资源。验收结束后删栈。 |

---

## 一、这个原型验证什么

需求文档把业务命门定为「**用户看完攻略不下单**」。因此本原型不追求生成更多攻略内容，
而是验证一条链路能否成立：

```
理解需求 → 检索可信内容 → 生成候选行程 → 执行约束校验
        → 匹配实际商品 → 动态可订查询 → 引导至预订入口
```

并把需求文档 §2.3 列出的刚性边界做成**可被测试证伪的代码约束**：

| 需求文档的边界 | 代码中的落点 | 守护用例 |
|---|---|---|
| 可订状态有时效，查询成功不是履约保证 | 快照带 `checked_at` / `expires_at` / `disclaimer` | `test_ok_product_is_bookable` |
| 超时、未知、失败状态不得当成可订 | `domain/availability.py` 五态状态机 | `test_timeout_is_unconfirmed_not_bookable` 等 5 条 |
| 不编造动态事实 | 大模型默认不介入；介入时走事实白名单校验 | `test_narrative_guard_rejects_fabricated_numbers` |
| 推荐必须绑定商品标识+日期+人数 | `domain/mapping.py` 的 `binding` 块 | `test_mapping_binds_product_date_and_pax` |
| 不把内容相似当作可购买证明 | 只走目录显式 `linked_product_ids` | `test_mapping_uses_explicit_links_not_similarity` |
| 关键事实无法确认时标注「待确认」 | `constraints.py` 的 `unverified` 档 | `test_height_limit_without_data_is_unverified_not_guessed` |
| 不代替用户下单付款、不锁价锁库存 | `bookings.py` 的 `guarantees` / `payment` 块 | `test_full_plan_endpoint_returns_all_layers` |

### 最重要的一条不变式

```
bookable is True  ⟺  availability_state == "AVAILABLE"
```

五种状态只有一种可订。这条不变式在 `_snapshot()` 里用显式检查守护（不用 `assert`，
因为它被违反就等于重演「看中了却订不了」那类事故），并有一条穷举目录全部商品的用例兜底。

| 状态 | 触发条件 | 可订 |
|---|---|---|
| `AVAILABLE` | 供应商明确确认可售且资格校验通过 | ✅ |
| `SOLD_OUT` | 供应商明确确认不可售 | ❌ |
| `UNKNOWN` | 供应商 200 但业务字段不完整 | ❌ |
| `UNCONFIRMED` | 查询超时或供应商 5xx | ❌ |
| `NOT_ELIGIBLE` | 商户不在营 / 人数 / 年龄 / 预订窗口不满足 | ❌ |

## 二、数据清单对齐

《数据清单-v3.md》9 项核心数据全部覆盖。**时效性不是注释，而是判定规则**——
超出时效的数据不得当作当前事实（这正是需求文档 §2.2 的根因假设之一）。

| # | 数据项 | 时效要求 | 硬性约束 | 实现 |
|---|---|---|---|---|
| 1 | 商户信息 | 低频 | | `merchants.json`、`GET /v1/merchants/{id}` |
| 2 | **商户在营状态** | **≤5min** | **是** | 推荐阶段过滤 + 可订查询前置门禁 + 重校验时复核 |
| 3 | 产品信息 | 低频 | | `products.json`；`price_hint` 明确标注为参考价 |
| 4 | **库存与可售日期** | **实时** | **是** | 每次向供应商实查；快照带 TTL 且读侧判过期 |
| 5 | 商户评分 | T+1 | | 排序因子；**过期则不加权**，绝不影响可订判定 |
| 6 | 目的地信息 | 周级 | | `GET /v1/destinations` |
| 7 | POI 信息 | 周级 | | 开放时间由约束校验器做闭馆/最后入场判定 |
| 8 | 天气预报 | 每日 | | 高降雨概率时室内加权；**只产出提示，绝不产出阻断** |
| 9 | 季节气候与事件 | 月级 | | `GET /v1/destinations/{id}/seasonality` |

运行时可通过 `GET /v1/data-inventory` 拉取这张对照表，便于与客户团队逐项对表。

两处刻意的设计取舍，值得单独说明：

1. **硬性约束过期即阻断，软性数据过期只标注。** #2/#4 过期 → 对象不可用；
   评分或天气过期 → 标「待确认」但不拦行程。把天气过期判成阻断是过度反应。
2. **多商户商品不因单个场馆停业而整体阻断。** 一张覆盖 4 个场馆的周游卡，
   其中 1 个检修就整张判不可订属于过度阻断——既不符合真实售卖规则，
   也与「改善转化」的项目目标相悖。此时商品仍可订，停业场馆如实列在 `merchant_advisories`。

## 三、架构

严格按规范 §3 选型，未做替换：

```
Internet ──HTTPS──▶ API Gateway HTTP API（$default stage，无路径前缀）
                        │
                        ├─▶ Lambda Authorizer（REQUEST 型，Bearer 静态令牌）
                        │        └─▶ Secrets Manager（令牌，最小权限只读一个密钥）
                        │
                        └─▶ Lambda（Python 3.12 / arm64，不入 VPC）
                                 ├─▶ 包内 Mock 目录（只读静态数据）
                                 ├─▶ DynamoDB 单表（On-Demand + TTL）
                                 ├─▶ Mock 供应商接口（唯一的价格库存事实源）
                                 └─▶ Bedrock（可选，默认关闭）
```

| 选型 | 非功能理由 |
|---|---|
| HTTP API | 自带公网 HTTPS 域名，`$default` stage 无路径前缀（规避规范 §3.1-4 的路径拼接坑） |
| Lambda 不入 VPC | 规避冷启动与 NAT 开销 |
| `/{proxy+}` 全量转交应用路由 | 404/405 的状态码与响应体由本项目掌控，不出现网关默认错误体字段名不一致（§3.1-3） |
| DynamoDB On-Demand 单表 | 无 VPC、无连接池、无实例管理；判分窗口外零成本 |
| 静态目录放包内而非 DynamoDB | 去掉「部署后必须灌数、灌数失败则全挂」这个故障点；运行态存储只承载可变状态 |
| Lambda 发布别名 `live` | 规范 §4.5 要求判分期间不得换端点；别名把流量锁在确定版本上 |
| 预留并发 20 | 判分脚本并行跑用例时不被账户级并发限流 |

### 401 与 403 如何区分（规范 §3.1-1）

HTTP API 的语义与 REST API 不同（REST API 里 `throw Unauthorized` 得 401，HTTP API 没有这条路径）。
默认模式 `AuthMode=handler_decide` 下的实际链路：

| 场景 | 谁拒绝 | 状态码 |
|---|---|---|
| 完全未带 `Authorization` 头 | API Gateway（identitySource 缺失，**不调用** Authorizer） | **401** |
| 带了头但令牌无效 | Authorizer 放行并透传结论 → 业务 Lambda | **401** |
| 令牌有效但缺少所需 scope | 业务 Lambda | **403** |

选这个默认值有三条理由：符合 RFC 7235（无效凭证 401、凭证有效但越权 403）；
所有拒绝都用同一套响应体字段名（不会混入网关的裸 `{"message":"Forbidden"}`）；
401 与 403 两个码语义清晰不含糊。

鉴权仍是纵深防御：Authorizer 透传的结论缺失时（如本地直调），
业务 Lambda 会**自行重新校验** `Authorization` 头；`handler.py` 在路由分发**之前**
无条件拒绝任何非 `OK` 的认证状态。不存在"context 缺失即放行"的绕过点。

若判分反馈要求「错误令牌必须 403」，把 `AuthMode` 改为 `authorizer_deny` 重新部署即可
——Authorizer 直接返回 Deny 策略，网关输出 403。**改一个参数，不动代码**（规范 §4.6 可反复重交）。

### 幂等如何做到（规范 §4.5）

判分用例会重跑，因此幂等不能靠调用方自觉传 `Idempotency-Key`：

1. **资源 ID 内容寻址**——`plan_id` / `booking_id` 由请求规范化内容的 SHA-256 派生。
   同一份请求体重复 POST 必然命中同一条资源。
2. **POST 状态码恒定**——`/v1/plans` 与 `/v1/bookings` 始终返回 **201**，
   用 `replayed` 字段区分首建与重放。不会出现 201/200 抖动。
3. **可订快照缓存**——同一 (商品, 日期, 人数) 在 TTL 窗口内返回同一份快照（含 `checked_at`），
   既满足幂等，又显式建模「可订状态有时效」。
4. **显式幂等键**——传了 `Idempotency-Key` 时，同键不同内容返回 **409**，同键同内容重放原响应。

## 四、快速开始

```bash
# 1. 生成 Mock 数据集（固定 seed，可复现；评审可自行灌数）
python3 tools/generate_mock_data.py

# 2. 本地测试
python3 -m pytest tests/ -q

# 3. 部署（自有账户）
sam build && sam deploy --guided     # 首次
sam build && sam deploy              # 后续

# 4. 写入令牌（令牌不进模板、不进 CFN 参数、不进代码仓库）
python3 scripts/rotate_tokens.py --stage demo --print-token

# 5. 提交前自检（对已部署端点从外网实测 §5 清单全部条目）
python3 scripts/preflight_check.py --url <ApiBaseUrl> --token <完整令牌> \
    --readonly-token <只读令牌>
```

`preflight_check.py` 全绿才提交。提交格式：

```
URL: https://<api-id>.execute-api.us-east-1.amazonaws.com
TOKEN: <完整权限令牌>
```

## 五、演示界面（CloudFront）

判分的交付物是 REST API；界面是给**业务汇报**用的演示层，部署在**独立栈**里。

```bash
python3 scripts/deploy_ui.py              # 部署（CloudFront 首次创建 10–20 分钟）
python3 scripts/deploy_ui.py --sync-only  # 只改了 HTML 时重传并失效缓存
python3 scripts/deploy_ui.py --delete     # 演示结束删除
python3 scripts/serve_ui.py               # 本地预览（改页面时用，不必等缓存失效）
```

### 为什么是独立栈

1. 判分的交付物应保持原样，不挂一个公开分配；
2. CloudFront 变更要十几分钟，混在一起会拖慢 API 的每次迭代；
3. 演示结束可单独删除，不影响已提交的 API 端点。

### 同源设计：为什么不需要 CORS

一个 CloudFront 分配挂两个源：

| 路径 | 源 | 缓存 |
|---|---|---|
| `/`、`/index.html` | S3（**私有** + OAC，Block Public Access 全开） | CachingOptimized |
| `/v1/*` | API Gateway HTTP API | **CachingDisabled** |

界面与 API 在同一个域名下，浏览器发的就是**同源请求**——不需要为了演示去放宽
已部署 API 的 CORS 配置（`http/responses.py` 至今不下发任何 `Access-Control-Allow-Origin`）。
路径天然不冲突（UI 在根、API 在 `/v1`），所以也**不需要任何 URI 重写函数**。

`/v1/*` 必须禁用缓存：可订状态是实时数据，缓存它就等于重演"把过期状态当成当前事实"。
原始请求头经 `AllViewerExceptHostHeader` 透传（`Authorization` 要过去，`Host` 不能过去
——API Gateway 用 `Host` 做路由）。

### 令牌怎么处理

**UI 栈不持有任何令牌。** 令牌由使用者在页面右上角输入，只存当前标签的
`sessionStorage`，关标签即失效：

- 不写入 CloudFront 配置、不写入 CloudFormation 模板、不写入代码仓库
- 未输入令牌时页面无法调用任何接口；未授权访客只看到一个输入框
- 令牌仍然只有一个权威来源：Secrets Manager

这是刻意的取舍。让 CloudFront 用 `OriginCustomHeaders` 注入令牌能省掉粘贴这一步，
但代价是令牌进入分配配置，且那个公开 URL 会变成**无鉴权的可写 API 入口**。
对一个演示界面来说，多粘一次令牌远比这个代价便宜。

### 界面包含什么

按需求文档的七步链路组织，**失败路径和顺利路径一样显眼**：

- 需求表单 —— 留空必填项可现场演示「需求澄清」（200 追问，不是报错）；目的地选"冰岛"可演示 422
- 统计磁贴 + **可订占比条** + 五态表格
- 行程时间轴（含天气、交通、用餐、休息、开门等候）
- 可执行性校验（阻断/提醒/待确认三档，带修复建议）
- **被商户在营状态排除的景点**（过滤动作可见）
- 商品映射与预订意图（可订的才有按钮；不可订的明确写"无预订入口"）
- **可订状态实验台** —— 一键跑 9 个真实失败形态，验证不变式
- 数据清单 9 项覆盖度对照表
- 完整 API 原始响应（技术评审用）

### 界面的可视化取舍

一开始想用 5 段堆叠条表示五种可订状态。跑配色验证器后否掉了：状态色里
黄（`#fab219`）与橙（`#ec835a`）相邻时**正常视力 ΔE 仅 13.6**（低于 15 底线），
红（`#d03b3b`）与绿（`#0ca30c`）在 deutan 模拟下 **ΔE 4.1**——颜色根本承载不了这个区分。

改成：**头条是二元的**（可订 vs 不可订，两色 CVD ΔE 10.7 / 正常视力 20.2，双模式通过），
五态明细走**表格**。每个状态都是「色点 + 图标 + 文字」三件套，颜色永不单独承载含义。
深色模式是各色针对深色表面重新取步，不是自动反转。

## 六、接口一览

完整契约见 [`docs/API.md`](docs/API.md)。所有路由均需 Bearer 令牌（**包括健康检查**——
留一个匿名端点等于给出一条绕过鉴权的路径）。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/v1/health` | read | 健康检查与能力自描述 |
| GET | `/v1/routes` | read | 路由清单（确认路径拼接正确） |
| POST | `/v1/plans` | write | 生成行程（信息不全时返回追问） |
| GET | `/v1/plans/{plan_id}` | read | 读取行程 |
| POST | `/v1/plans/{plan_id}/revalidate` | read | 重新校验（时效数据会变） |
| POST | `/v1/availability/queries` | read | 批量可订查询（五态） |
| GET | `/v1/products/{id}/availability` | read | 单商品可订查询 |
| GET | `/v1/catalog` | read | 目录概览 |
| GET | `/v1/products/{id}` | read | 商品静态资料（**不含**实时价格库存） |
| GET | `/v1/pois/{id}` | read | 景点资料 + 绑定商品 + 内容来源 |
| GET | `/v1/content` | read | 可信内容检索（带来源与时效标注） |
| GET | `/v1/data-inventory` | read | 数据清单 9 项覆盖度与时效口径 |
| GET | `/v1/destinations` | read | 目的地 |
| GET | `/v1/destinations/{id}/seasonality` | read | 季节气候与事件 |
| GET | `/v1/merchants/{id}` | read | 商户信息 + 在营状态时效判定 |
| GET | `/v1/weather` | read | 天气预报 |
| POST | `/v1/bookings` | write | 创建预订意图（**不代付**） |
| GET | `/v1/bookings/{id}` | read | 读取预订意图 |
| POST | `/v1/events` | write | 漏斗埋点 |
| GET | `/v1/funnel` | read | 漏斗口径定义 |

## 七、代码结构

```
src/app/
├── handler.py            业务 Lambda 入口（路由注册 + 顶层异常兜底）
├── authorizer.py         Lambda Authorizer
├── core/                 配置、日志脱敏、时钟、内容寻址 ID、令牌校验
├── http/                 错误模型、响应构造、字段校验器（422 由此产出）、路由器
├── adapters/             目录加载、DynamoDB 单表、Mock 供应商接口
├── domain/               ★ 业务规则所在
│   ├── availability.py   五态可订状态机 + 商户在营门禁（最核心）
│   ├── constraints.py    行程可执行性校验（blocker/warning/unverified 三档）
│   ├── itinerary.py      区域聚合 + 时间推演（完全确定性）
│   ├── mapping.py        景点→商品显式映射
│   ├── freshness.py      数据时效分级（对齐数据清单）
│   ├── weather.py        天气感知（只影响排布与提示）
│   ├── content.py        带来源与时效的内容检索
│   ├── clarify.py        需求澄清槽位
│   └── narrative.py      大模型边界层（事实白名单 + 输出校验）
├── api/                  路由处理函数
└── data/mock/            Mock 数据集（随包分发）
web/index.html                演示界面（自包含单文件，无 CDN，离线可用）
template-ui.yaml              演示界面基础设施（CloudFront + 私有 S3，独立栈）
tools/generate_mock_data.py   数据生成器（固定 seed + 合规自检）
scripts/preflight_check.py    提交前自检（§5 清单自动化）
scripts/rotate_tokens.py      令牌生成与写入
scripts/deploy_ui.py          部署/更新/删除演示界面
scripts/serve_ui.py           界面本地预览（纯透传，不持有令牌）
scripts/demo.py               命令行端到端演示（不需要 AWS 凭证）
```

### 一个刻意的分层

**生成器不负责证明行程可行，校验器负责抓出问题。**
`itinerary.py` 会把当日闭馆的场馆照样排进行程，由 `constraints.py` 报出 `POI_CLOSED_ON_DATE`
并给出**经过开放日校验的**同区域备选。如果生成器自己把问题藏掉，
「行程可执行率」这个指标就失去意义了。

## 八、首期不包含

与需求文档 §4.5 一致：

- 对转化率提升、商业收益、准确率或交付日期的任何保底承诺
- 未经评估直接上线生产、改造生产客服 Agent、连接生产数据库
- 训练覆盖商品动态信息的「记忆型」模型，或用客户数据训练模型
- 自动替用户下单付款、保证锁价锁库存
- 在未确认核心用户画像前铺开全部城市与客群

## 九、已知限制

诚实列出，避免把原型当成品：

1. **供应商接口是 Mock。** 真实接入需替换 `adapters/supplier_mock.py`（接口签名已按真实 HTTP 供应商形态设计，域层无需改动），并补充限流、重试预算、熔断与对账。
2. **内容检索是确定性关键词打分**，非向量检索。原型目标是验证「有来源、能标时效、冲突可暴露」这套契约；换 OpenSearch / Bedrock Knowledge Base 只需替换 `domain/content.py` 的 `search()`。
3. **目的地仅覆盖大阪**，且 POI/商户/商品均为合成数据，规模远小于真实目录。
4. **漏斗埋点只提供契约**，不产出任何转化率结论——基线与指标口径需客户在自有环境统计并确认。
5. **交通耗时为静态矩阵**，未接入实时路况；缺失区域对按保守上限估算并标注「待确认」。
6. **`timeout` 模式不引入真实等待。** 判分需要可复现，故直接抛超时异常并上报配置的超时时长，语义等价但不浪费 Lambda 时间预算。
7. **令牌吊销有最长 5 分钟的生效延迟。** Lambda 进程内缓存令牌（带 TTL）以减少 Secrets Manager 调用。紧急吊销须配合强制重新部署，详见 [`docs/RUNBOOK.md`](docs/RUNBOOK.md) 第二节。
8. **API Gateway 访问日志不记录客户端 IP。** 为满足规范 §3「严禁打印令牌与请求头全量」，日志格式做了收紧；代价是缺少异地调用溯源数据。生产化时可加回 `$context.identity.sourceIp`（该字段不属敏感信息）。

## 十、后续待客户确认

需求文档 §5.1 列出的补访项中，直接影响本工程下一步的两项：

1. **供应商接口如何判定「可订」，查询与下单之间怎么重查？** —— 决定 Mock 要做到什么精度，以及 `availability_ttl_seconds` 的合理取值。
2. **漏斗当前主要流失在哪一步？** —— 决定演示重点放在行程可信度还是商品衔接。

在这两项有答案前，本原型把可插拔的 Mock 供应商接口和五态语义做扎实——这部分不依赖客户数据。
