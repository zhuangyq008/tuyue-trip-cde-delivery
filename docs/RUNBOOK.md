# 部署与提交运行手册

面向执行人的操作清单。每一步都标注了**为什么这么做**，便于判分挂测后快速定位。

前置：AWS CLI 已配置（账户 284367710968，region us-east-1）、SAM CLI ≥ 1.150、Python 3.12。

---

## 一、首次部署

```bash
cd /home/ec2-user/yuetu_trip_cde

# 1) 生成 Mock 数据集（固定 seed，可复现）
python3 tools/generate_mock_data.py

# 2) 本地测试必须全绿才继续
python3 -m pytest -q

# 3) 构建并部署
sam build
sam deploy
```

`sam deploy` 使用 `samconfig.toml` 中的默认参数（stack `yuetu-trip-cde-demo`，
`Stage=demo`、`AuthMode=handler_decide`、`EnableLlmNarrative=false`）。

记录输出：

```bash
aws cloudformation describe-stacks --stack-name yuetu-trip-cde-demo \
  --query 'Stacks[0].Outputs' --output table
```

需要的是 `ApiBaseUrl`。

## 二、写入令牌

令牌**不在** CloudFormation 里生成，也不进代码仓库（规范 §3「令牌不落代码」）。
CFN 只创建密钥容器，实际值由脚本本地生成后直接写入 Secrets Manager：

```bash
python3 scripts/rotate_tokens.py --stage demo --print-token
```

输出两个令牌：

- **read+write** —— 提交给判分平台。
- **read only** —— 自检用，验证写操作返回 403（证明 401 与 403 可区分）。

> 令牌只在这一次输出里出现。丢了就重跑脚本换一组，不要试图从代码或日志里找回 ——
> 日志有强制脱敏，找不到是设计如此。

> ⚠ **`--print-token` 的使用禁区。** 本仓库是公开仓库，令牌一旦进入可搜索的历史记录就等于泄露。
> 禁止在以下场景使用该参数：CI 流水线（输出会存进构建日志）、录屏与直播演示、
> 开启了会话记录的终端（`asciinema`、`tmux` logging、`script`）、共享屏幕的会议。
> 需要自动化取用时，改为把令牌写入本地 `.env`（`.gitignore` 已排除 `.env` 与 `*token*.txt`），
> 或直接从 Secrets Manager 控制台读取。

> ⚠ **吊销令牌不是立即生效的。** Lambda 进程内缓存令牌（带 5 分钟 TTL，减少 Secrets Manager 调用）。
> 重跑 `rotate_tokens.py` 后，最长 5 分钟内既有温容器仍可能认旧令牌。
> **紧急吊销**（例如令牌误传到公开仓库）时不要只依赖 TTL 过期，执行强制重新部署创建全新执行环境：
>
> ```bash
> python3 scripts/rotate_tokens.py --stage demo   # 先换掉密钥里的值
> sam build && sam deploy --no-confirm-changeset  # 再强制轮换执行环境
> ```

写入后 Lambda 需要拿到新值。进程内有令牌缓存（减少 Secrets Manager 调用），
所以**执行一次预热让容器轮换**：

```bash
# 触发一次部署使容器全部更新（最可靠）
sam deploy --no-confirm-changeset

# 或等待既有容器自然回收后再自检
```

## 三、提交前自检

**这一步不能跳。** 单元测试证明代码逻辑对，自检脚本证明「这个 URL + 这个令牌，
从外网真的能按规范工作」——两者验证的不是同一件事。

```bash
python3 scripts/preflight_check.py \
  --url https://<api-id>.execute-api.us-east-1.amazonaws.com \
  --token <read+write 令牌> \
  --readonly-token <read only 令牌>
```

脚本逐条覆盖规范 §5 清单：HTTPS 与非内网地址、无令牌/错误令牌被拒、
正确令牌可用、只读令牌写操作 403、404 与 422 可区分、405、错误体字段名一致、
可订不变式、幂等重跑一致、数据集 Mock 标记、9 项数据清单覆盖、连续探活稳定性。

**全绿才提交。** 有 `FAIL` 就先修；有 `WARN` 需人工确认（最常见的是冷启动导致首次响应偏慢）。

### 预热

判分是异步轮询，但首个请求遇上冷启动仍可能拖慢整体。提交前打一轮预热：

```bash
URL=https://<api-id>.execute-api.us-east-1.amazonaws.com
TOKEN=<read+write 令牌>
for i in $(seq 1 5); do
  curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" \
    -H "Authorization: Bearer $TOKEN" "$URL/v1/health"
done
```

第 2 次起应稳定在 200 且耗时明显下降。

## 四、提交

```
URL: https://<api-id>.execute-api.us-east-1.amazonaws.com
TOKEN: <read+write 令牌>
```

URL **不带尾部斜杠、不带 stage 前缀**——HTTP API 用 `$default` stage，
基地址后直接拼接 `/v1/...` 即可访问（规范 §3.1-4 点名的坑）。

### 提交后到判分结束期间

规范 §4.5：**判分窗口内不得部署、改配、换端点。**

- 不要跑 `sam deploy`
- 不要改 CloudFormation 参数
- 不要重跑 `rotate_tokens.py`（会让已提交的令牌失效）
- 业务 Lambda 已用发布别名 `live` 锁定版本，即使误操作部署也不会切换已提交端点的行为——但仍然别赌这个

## 五、挂测后的处理

规范 §4.6：按用例通过率计分，通常容许挂 1–2 条；**重试不清零、取历史最高、失败不倒扣。**
所以策略是：先交一版拿分，再迭代重交。

1. **先看「查看判定依据」的逐用例报告**，不要盲目重提。
2. 按失败用例的类型对症处理：

| 挂测症状 | 大概率原因 | 处理 |
|---|---|---|
| 「错误令牌应返回 403，实际 401」 | 默认 `handler_decide` 按 RFC 7235 给 401 | **改参数即可，不动代码**：`sam deploy --parameter-overrides Stage=demo AuthMode=authorizer_deny`。Authorizer 改为返回 Deny 策略，网关输出 403 |
| 所有带令牌的请求都返回 500，且 Authorizer **没有任何日志** | 网关缺少调用 Authorizer 的权限（Authorizer 从未被调用） | 确认栈内存在 `AuthorizerInvokePermission`（`AWS::Lambda::Permission`）。SAM 不会为 HTTP API 的 Lambda Authorizer 自动创建该权限，必须显式声明 |
| 「参数非法应返回 422，实际 400」 | 请求体连 JSON 都不合法 → 走的是 `BAD_REQUEST` 分支 | 确认用例发送的是合法 JSON；字段级非法一定走 422（校验全在 Lambda 内，不依赖网关模型） |
| 「响应字段名不符」 | 收到了决策人下发的接口规范文件，与本项目自拟契约不一致 | 按该文件改 `src/app/api/*.py` 的响应键名与 `src/app/http/validation.py` 的字段定义，同步更新 `docs/API.md` 与 `tests/test_http_contract.py` |
| 「路径 404」 | URL 拼接问题 | 用 `GET /v1/routes` 拉取真实路由清单核对 |
| 「响应超时」 | 冷启动或并发限流 | 先预热；必要时提高 `ReservedConcurrentExecutions` |
| 「重复调用结果不一致」 | 时间戳类字段在重放时变化 | 检查是否走到了重放分支（响应里 `replayed: true`）；行程与预订意图均为内容寻址 ID，重放返回存储副本 |

3. 改完重新走「二、写入令牌 → 三、自检 → 四、提交」。若令牌未变，可跳过第二步。

### 定位挂测原因的日志入口

```bash
# 业务 Lambda（结构化 JSON，按 request_id 检索）
aws logs tail /aws/lambda/yuetu-trip-demo-api --since 15m --follow

# Authorizer 判定记录（只记结果，不记令牌）
aws logs tail /aws/lambda/yuetu-trip-demo-authorizer --since 15m

# API Gateway 访问日志（不含请求头）
aws logs tail /aws/apigateway/yuetu-trip-demo --since 15m
```

响应体里的 `request_id` 与日志中的 `request_id` 一致，可直接对账。

## 六、验收结束后删栈

规范 §3：验收结束后删栈。

```bash
sam delete --stack-name yuetu-trip-cde-demo --no-prompts
```

DynamoDB 表与日志组的 `DeletionPolicy` 为 `Delete`，会随栈删除。
Secrets Manager 密钥默认有 7–30 天恢复窗口；如需立即删除：

```bash
aws secretsmanager delete-secret --secret-id yuetu-trip-demo-api-tokens \
  --force-delete-without-recovery
```

删栈后确认无残留计费资源：

```bash
aws cloudformation describe-stacks --stack-name yuetu-trip-cde-demo 2>&1 | head -3
# 期望：Stack with id yuetu-trip-cde-demo does not exist
```

## 七、成本说明

判分窗口外基本零成本，全部为按量付费且无常驻资源：

| 资源 | 计费方式 | 空闲成本 |
|---|---|---|
| Lambda（业务 + Authorizer） | 按调用与时长 | 0 |
| API Gateway HTTP API | 按请求数 | 0 |
| DynamoDB On-Demand | 按读写请求 | 0（TTL 自动清理数据） |
| CloudWatch Logs | 按摄入量与存储 | 极低（保留 7 天） |
| Secrets Manager | **按密钥数计月费** | 约 $0.40/月/密钥 |

唯一有固定成本的是 Secrets Manager 密钥。验收结束后按第六节删除即可。

---

## 八、演示界面（CloudFront）

判分交付物是 REST API；界面是给业务汇报用的演示层，放在**独立栈** `yuetu-trip-ui-demo`。
它与 CDE 交付栈完全解耦——删除 UI 栈不影响已提交的 API 端点。

### 部署

```bash
# 前置：CDE 交付栈已部署（脚本从它的 ApiBaseUrl 输出解析 API 源）
python3 scripts/deploy_ui.py
```

脚本会：解析 API 域名 → 创建/更新 CloudFront + 私有 S3 栈 → 上传 `web/index.html`
→ 发起缓存失效 → 打印访问地址。**CloudFront 首次创建通常 10–20 分钟**，属正常。

### 使用

打开输出的 `https://<dist>.cloudfront.net`，在右上角粘贴 **read+write** 令牌后点「连接」。

```bash
# 取令牌
aws secretsmanager get-secret-value --secret-id yuetu-trip-demo-api-tokens \
  --query SecretString --output text | python3 -m json.tool
```

> ⚠ 屏幕共享演示时：令牌输入框是掩码的，但浏览器开发者工具里可见。
> 建议演示前先在非共享窗口连接好，或演示结束后轮换令牌。

### 改了页面之后

```bash
python3 scripts/deploy_ui.py --sync-only   # 重传 + 失效缓存，不动 CloudFront 配置
```

本地改页面时用预览服务更快（不必等缓存失效）：

```bash
python3 scripts/serve_ui.py --port 8080
# 从自己的电脑访问：ssh -L 8080:localhost:8080 ec2-user@<EC2 地址>
```

预览服务与 CloudFront 的路径语义一致（`/` 走页面、`/v1/*` 走 API），
且同样**不持有令牌**，所以本地看到的表现与线上一致——包括未输入令牌时的 401。

### 演示现场建议的讲述顺序

界面刻意把失败路径和顺利路径放得一样显眼。建议按这个顺序走：

1. **先留空「出发日期」点生成** —— 出现「需求澄清」，说明缺信息返回 200 追问而不是报错
2. **目的地改成「冰岛」** —— 出现 422 逐字段错误，说明字段非法与信息缺失是两回事
3. **填完整生成行程** —— 时间轴、天气、开门等候、可执行性校验三档
4. **指出「被商户状态排除的景点」** —— 过滤动作是可见的，不是悄悄少几个
5. **在商品表里点一个可订商品的「创建预订意图」** —— 展示锁价/库存保留/预订成功全为「否」
6. **拉到最下面跑「可订状态实验台」** —— 9 个失败形态一次看完，不变式成立
7. **最后开「数据清单 v3 覆盖度」** —— 与客户团队逐项对表

### 排查

| 症状 | 原因 | 处理 |
|---|---|---|
| 页面能打开但点「连接」报 401 | 令牌无效或刚轮换（缓存 TTL 5 分钟） | 确认令牌来自 Secrets Manager 当前值；刚轮换过则等 5 分钟或 `sam deploy` 强制轮换执行环境 |
| 点「连接」报 403 | 用了只读令牌 | 本页需要 read+write |
| `/v1/*` 返回 403 且响应体不是本项目格式 | CloudFront 把 `Host` 头透传给了 API Gateway | 确认 API 行为用的是 `AllViewerExceptHostHeader` 源请求策略 |
| 改了 HTML 但页面没变 | CloudFront 缓存 | `--sync-only` 重跑；或等失效完成（1–2 分钟） |
| 可订状态看起来不刷新 | `/v1/*` 被缓存了 | 确认 API 行为用的是 `CachingDisabled` 缓存策略 |

### 成本与清理

CloudFront 与 S3 都按量付费，无固定月费；演示级流量成本可忽略。

```bash
python3 scripts/deploy_ui.py --delete
```

脚本会先清空 S3 桶再删栈（桶非空无法随栈删除）。
CloudFront 分配需先禁用再删除，整个过程约 10–15 分钟，属正常。
