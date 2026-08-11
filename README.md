# LLM Gateway

一个最小但完整的 OpenAI-compatible LLM Gateway。第一阶段支持：

- `POST /v1/chat/completions` 非流式聊天接口
- Bearer API Key 鉴权
- 调用一个 OpenAI-compatible Provider
- 记录请求状态、耗时和 token 使用量
- 统一处理超时、网络错误和上游错误
- 使用 Fake Provider 完成核心流程测试

第二阶段 2.1 已加入：

- 租户和租户级 API Key
- API Key 哈希存储，明文只在创建时显示一次
- SQLite 开发数据库和 PostgreSQL 生产配置
- SQLAlchemy 异步访问和 Alembic 迁移
- 调用状态、token、租户和 API Key 持久化

第二阶段 2.2 已加入：

- 同时注册多个 OpenAI-compatible Provider
- 按模型支持范围选择 Provider
- 按 `priority` 确定首选 Provider
- 支持禁用 Provider 和 `*` 通配兜底
- 将最终选中的 Provider 写入调用记录

第二阶段 2.3 已加入：

- Provider 超时或连接失败后自动故障转移
- 对 408、429、500、502、503、504 执行故障转移
- 参数错误等不可重试错误立即返回
- 使用 `MAX_PROVIDER_ATTEMPTS` 限制上游尝试次数
- 独立记录每一次 Provider 调用及其结果
- 响应头返回最终 Provider 和实际尝试次数

第二阶段 2.4 已加入：

- 支持租户级 `Idempotency-Key`
- 使用数据库唯一约束保证一次执行
- 成功响应和业务错误均可重放
- 相同 Key 配合不同请求体时返回冲突
- 记录只保存幂等 Key 哈希，不保存原始 Key
- 幂等记录按 TTL 过期

第二阶段 2.5 已加入：

- Redis 分布式固定窗口限流
- 按租户和 API Key 隔离计数
- Lua 原子执行计数、过期和 TTL 查询
- 成功、幂等重放和 429 响应均返回限流头
- Redis 不可用时采用 fail-closed 策略

第二阶段 2.6 已加入：

- 按租户、按 UTC 月份管理 token 额度
- Redis Lua 原子预留、结算和释放额度
- 使用真实 `usage.total_tokens` 完成最终结算
- Provider 失败时全额释放预留
- 自动回收进程崩溃留下的过期预留
- 租户额度版本化，修改额度时自动切换 Redis Key

第三阶段 3.1 已加入：

- Transactional Outbox 事件表
- 请求记录和事件在同一数据库事务中提交
- RabbitMQ topic exchange 持久化发布
- 后台批量领取、Publisher Confirm 和指数退避
- 发布进程崩溃后的 stale lock 回收
- 应用停机时优雅结束 Outbox 发布任务

第三阶段 3.2 已加入：

- RabbitMQ 主队列、TTL 重试队列和死信队列
- `event_id` 驱动的 Inbox 幂等消费
- Inbox 和业务审计结果在同一数据库事务中提交
- 固定延迟消息重试和最大重试次数
- 非法消息、事件冲突和耗尽重试消息进入 DLQ
- 消费者连接失败后台重试及优雅停机

第三阶段 3.3 已加入：

- Prometheus `/metrics` 指标端点
- HTTP 请求量与延迟直方图
- Provider 尝试、延迟、故障转移和 token 指标
- 限流、额度、幂等重放指标
- Outbox 发布与消费者重试/DLQ 指标
- 低基数标签约束和敏感标识测试

第三阶段 3.4 已加入：

- 非 root 用户运行的 Gateway Docker 镜像
- PostgreSQL、Redis、RabbitMQ 与 Gateway 一键编排
- 容器启动时自动执行 Alembic 数据库迁移
- 服务健康检查、启动依赖和持久化数据卷
- Prometheus 自动抓取 Gateway 指标
- Grafana 自动配置数据源和 Gateway 概览面板

第三阶段 3.5 已加入：

- k6 并发压力测试场景和自动化阈值判定
- OpenAI-compatible 本地 Mock Provider，压测不消耗真实 API 额度
- 一键启动依赖、创建隔离租户和执行压测的脚本
- 可配置并发数、持续时间、思考时间、P95 和错误率阈值
- 压测覆盖鉴权、PostgreSQL、Redis、Provider、Outbox 和 RabbitMQ 链路

第三阶段 3.6 已加入：

- 单机生产 Docker Compose 覆盖配置
- Caddy HTTPS 入口和内部端口隔离
- Gateway 文件密钥注入、容器硬化、资源限制和日志轮转
- `/health` liveness 与 `/ready` dependency readiness
- 单次数据库迁移、发布预检和自动部署脚本
- 上线、升级、备份、回滚、扩容和故障排查说明

## 运行环境

- Python 3.9+

完整生产部署流程见 [生产部署指南](docs/DEPLOYMENT.md)。

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[test]'
cp .env.example .env
```

编辑 `.env`，填写 `PROVIDER_API_KEY`。开发环境可以直接使用默认 SQLite；
生产环境建议设置：

```env
DATABASE_URL=postgresql+asyncpg://user:password@localhost/llm_gateway
DATABASE_AUTO_CREATE=false
```

## 初始化数据库和 API Key

使用迁移创建数据库结构：

```bash
alembic upgrade head
```

创建租户：

```bash
llm-gateway create-tenant --name "Example tenant"
```

命令会输出 `tenant_id`。使用它创建 API Key：

```bash
llm-gateway create-key \
  --tenant-id '<tenant-id>' \
  --name 'development'
```

创建命令只显示一次完整 API Key。数据库仅保存 Key 的 SHA-256 哈希和短前缀。

## 配置多个 Provider

单 Provider 的 `PROVIDER_*` 配置仍然有效。需要多 Provider 时，在 `.env` 中使用
JSON 数组形式的 `PROVIDERS`：

```env
PROVIDERS=[{"name":"openai","api_key":"sk-...","base_url":"https://api.openai.com/v1","models":["gpt-4.1-mini"],"priority":10,"enabled":true,"timeout_seconds":30},{"name":"secondary","api_key":"...","base_url":"https://provider.example/v1","models":["gpt-4.1-mini","*"],"priority":20,"enabled":true,"timeout_seconds":30}]
```

路由过程是：

1. 排除 `enabled=false` 的 Provider。
2. 保留明确支持请求模型或配置了 `*` 的 Provider。
3. 选择 `priority` 数字最小的 Provider。
4. 优先级相同时按 Provider 名称稳定排序。
5. 没有 Provider 支持模型时返回 `404 model_not_available`。

## 故障转移

配置最大 Provider 尝试次数：

```env
MAX_PROVIDER_ATTEMPTS=2
```

Gateway 按路由优先级依次尝试候选 Provider。以下情况会尝试下一个候选：

- Provider 连接失败
- Provider 调用超时
- 上游返回 408 或 429
- 上游返回 500、502、503 或 504

请求参数错误、认证错误和其他不可重试响应不会切换 Provider。全部候选失败时，
Gateway 返回最后一次上游错误。

每次上游调用都会写入 `provider_attempts` 表；一次客户端请求仍只对应一条
`request_logs` 记录。成功响应包含：

```http
X-Request-ID: <request-id>
X-Provider: secondary
X-Provider-Attempts: 2
```

## 幂等请求

客户端可以为聊天请求携带一个 8～255 字符的幂等键：

```http
Idempotency-Key: chat-operation-0001
```

第一次请求正常执行，并返回：

```http
Idempotency-Replayed: false
```

同一租户使用相同 Key 和相同请求体重试时，Gateway 不会再次调用 Provider，而是
返回之前保存的响应：

```http
Idempotency-Replayed: true
```

如果相同 Key 携带不同请求体，返回：

```text
409 idempotency_conflict
```

如果第一次请求仍在执行，返回：

```text
409 idempotency_in_progress
```

幂等记录默认保留一天，可通过以下配置修改，允许范围为 60 秒到 7 天：

```env
IDEMPOTENCY_TTL_SECONDS=86400
```

幂等范围是租户级，因此不同租户可以安全使用相同的客户端幂等键。

## Redis 限流

启动 Redis 后配置：

```env
REDIS_URL=redis://127.0.0.1:6379/0
RATE_LIMIT_ENABLED=true
RATE_LIMIT_REQUESTS=60
RATE_LIMIT_WINDOW_SECONDS=60
```

如果使用 Docker，可以在开发环境启动一个 Redis 实例：

```bash
docker run --name llm-gateway-redis -p 6379:6379 redis:7-alpine
```

限流键格式为：

```text
rate_limit:{tenant_id}:{api_key_id}
```

Lua 脚本在 Redis 内原子执行 `INCR`、首次请求的 `EXPIRE` 和 `TTL` 查询，
因此多个 Gateway 实例并发处理请求时不会依赖单机内存计数。

允许的响应包含：

```http
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 42
X-RateLimit-Reset: 1786377600
```

超过限制时返回：

```http
HTTP/1.1 429 Too Many Requests
Retry-After: 37
```

限流发生在幂等检查之前，所以幂等重放也会计入请求速率。启用限流后，如果 Redis
不可用，Gateway 返回 `503 rate_limit_unavailable`，不会静默跳过限流。

## Redis token 额度

启用按月 token 额度：

```env
QUOTA_ENABLED=true
QUOTA_DEFAULT_TOKENS=1000000
QUOTA_DEFAULT_COMPLETION_RESERVE_TOKENS=1024
QUOTA_RESERVATION_TTL_SECONDS=300
```

创建租户时可以指定独立额度：

```bash
llm-gateway create-tenant \
  --name "Example tenant" \
  --token-quota-limit 2000000
```

修改已有租户额度：

```bash
llm-gateway set-tenant-quota \
  --tenant-id '<tenant-id>' \
  --tokens 3000000
```

修改额度会递增租户的 `quota_version`，新的请求自动使用新的 Redis 余额键，不需要
扫描或删除旧 Key。

额度键按 UTC 月份和配置版本隔离：

```text
quota:{tenant_id}:{YYYY-MM}:v{quota_version}:balance
quota:{tenant_id}:{YYYY-MM}:v{quota_version}:reservations
quota:{tenant_id}:{YYYY-MM}:v{quota_version}:deadlines
```

一次调用的额度生命周期：

```text
估算消息最大消耗
→ Lua 原子预留
→ 调用 Provider（包括故障转移）
→ 使用 usage.total_tokens 结算
→ 退回预留与实际消耗的差额
```

如果 Provider 调用失败，预留额度会全额释放。进程崩溃导致无法主动释放时，下一次
额度预留会利用 Sorted Set 找出超时记录并自动退款。

启用额度后，如果请求没有提供 `max_tokens`，Gateway 会将
`QUOTA_DEFAULT_COMPLETION_RESERVE_TOKENS` 写入上游请求，确保生成 token 存在上界。

成功响应包含：

```http
X-Quota-Limit: 1000000
X-Quota-Remaining: 998742
X-Quota-Period: 2026-08
```

额度不足返回：

```text
429 quota_exceeded
```

执行顺序为：

```text
鉴权 → Redis 限流 → 幂等检查 → 额度预留 → Provider → 额度结算
```

因此幂等重放不会再次预留或扣减 token 额度。

## Transactional Outbox 与 RabbitMQ

启用后台 Outbox 发布器：

```env
RABBITMQ_URL=amqp://guest:guest@127.0.0.1:5672/
RABBITMQ_EXCHANGE=llm_gateway.events
OUTBOX_PUBLISHER_ENABLED=true
OUTBOX_POLL_INTERVAL_SECONDS=1
OUTBOX_BATCH_SIZE=100
OUTBOX_MAX_RETRY_SECONDS=60
```

当前发布两类事件：

```text
request.completed
request.failed
```

事件示例：

```json
{
  "event_id": "f7a...",
  "event_type": "request.completed",
  "occurred_at": "2026-08-10T20:00:00+00:00",
  "request_id": "8ac...",
  "tenant_id": "tenant-id",
  "api_key_id": "api-key-id",
  "provider_name": "openai",
  "model": "gpt-4.1-mini",
  "status": "succeeded",
  "status_code": 200,
  "prompt_tokens": 20,
  "completion_tokens": 10,
  "total_tokens": 30,
  "duration_ms": 450,
  "error_code": null
}
```

请求完成时，`request_logs` 更新和 `outbox_events` 插入使用同一个数据库事务。后台任务
领取 `pending` 事件，发布到 durable topic exchange，并在 Publisher Confirm 成功后将
事件标记为 `published`。

RabbitMQ 不可用时，业务请求不受影响，事件保留在数据库中并采用指数退避重试：

```text
1s → 2s → 4s → 8s → ... → OUTBOX_MAX_RETRY_SECONDS
```

如果 Publisher 在领取后崩溃，超过锁定时间的 `publishing` 事件会被其他实例重新领取。
因此投递语义是“至少一次”：RabbitMQ 已收到事件但数据库尚未标记成功时，事件可能
再次投递。消费者必须使用 `event_id` 实现幂等，这属于第三阶段 3.2。

## RabbitMQ 幂等消费者、重试与死信

启用审计消费者：

```env
EVENT_CONSUMER_ENABLED=true
RABBITMQ_CONSUMER_QUEUE=llm_gateway.audit
RABBITMQ_RETRY_DELAY_MS=5000
RABBITMQ_MAX_RETRIES=3
```

消费者声明以下拓扑：

```text
llm_gateway.events                 topic exchange
└── llm_gateway.audit             主队列，绑定 request.*

llm_gateway.events.retry           topic exchange
└── llm_gateway.audit.retry       TTL 重试队列
    └── TTL 到期后回到 llm_gateway.events

llm_gateway.events.dead            topic exchange
└── llm_gateway.audit.dead        死信队列
```

消费成功时，以下两个写入使用同一个数据库事务：

```text
inbox_events      记录 event_id 已处理
event_audit_logs  保存审计业务结果
```

如果业务处理失败，整个事务回滚，消息携带递增后的 `x-retry-count` 进入 TTL 重试
队列。达到 `RABBITMQ_MAX_RETRIES` 后进入死信队列。

以下消息不进行普通重试，直接进入死信队列：

- 非 JSON 消息
- 缺少 `event_id` 或 `event_type`
- AMQP `message_id` 与 payload `event_id` 不一致
- AMQP message type 与 payload `event_type` 不一致
- 相同 `event_id` 携带不同 payload

RabbitMQ 可能重复投递，但消费者会先检查 `inbox_events.event_id`。已完成事件直接 ACK，
不会再次写入审计结果。因此系统提供的是：

```text
至少一次消息投递 + 幂等数据库消费 = 业务效果一次
```

## Prometheus 指标

Prometheus 抓取端点：

```text
GET /metrics
```

核心指标：

```text
gateway_http_requests_total
gateway_http_request_duration_seconds
gateway_provider_attempts_total
gateway_provider_duration_seconds
gateway_provider_failovers_total
gateway_tokens_total
gateway_rate_limit_rejections_total
gateway_quota_rejections_total
gateway_idempotency_replays_total
gateway_outbox_published_total
gateway_outbox_publish_failures_total
gateway_consumer_deliveries_total
gateway_consumer_retries_total
gateway_dead_letter_messages_total
```

常用 PromQL 示例：

```promql
# 每秒请求数
sum(rate(gateway_http_requests_total[5m]))

# HTTP P95 延迟
histogram_quantile(
  0.95,
  sum by (le, route) (
    rate(gateway_http_request_duration_seconds_bucket[5m])
  )
)

# Provider P95 延迟
histogram_quantile(
  0.95,
  sum by (le, provider) (
    rate(gateway_provider_duration_seconds_bucket[5m])
  )
)

# Provider 故障转移速率
sum by (from_provider, to_provider, reason) (
  rate(gateway_provider_failovers_total[5m])
)

# 每分钟 token 使用量
sum by (provider, token_type) (
  increase(gateway_tokens_total[1m])
)

# 死信增长
sum(increase(gateway_dead_letter_messages_total[10m]))
```

指标标签只使用受控值，例如：

```text
method
route template
status_code
provider
outcome
error_code
token_type
```

以下高基数或敏感信息禁止作为标签：

```text
request_id
tenant_id
api_key_id
Idempotency-Key
API Key 明文
异常消息文本
```

`/metrics` 当前不要求 Gateway API Key，部署时应只允许 Prometheus 所在的内部网络访问，
不要直接暴露到公网。

## Docker Compose

复制 Docker 环境变量模板，并至少替换 Provider API Key：

```bash
cp .env.docker.example .env
```

构建并在后台启动完整服务：

```bash
docker compose up --build -d
docker compose ps
```

首次启动时 Gateway 会自动执行 `alembic upgrade head`。所有依赖健康后，可访问：

| 服务 | 地址 | 默认凭据 |
| --- | --- | --- |
| Gateway | http://127.0.0.1:8000 | 使用租户 API Key |
| Gateway metrics | http://127.0.0.1:8000/metrics | 无 |
| RabbitMQ Management | http://127.0.0.1:15672 | `gateway` / `.env` 中的密码 |
| Prometheus | http://127.0.0.1:9090 | 无 |
| Grafana | http://127.0.0.1:3000 | `.env` 中的管理员账号和密码 |

容器内创建租户和 Gateway API Key：

```bash
docker compose exec gateway llm-gateway create-tenant --name "Example tenant"
docker compose exec gateway llm-gateway create-key \
  --tenant-id '<tenant-id>' \
  --name 'development'
```

查看日志和停止服务：

```bash
docker compose logs -f gateway
docker compose down
```

`docker compose down` 会保留命名数据卷。只有确认不再需要 PostgreSQL、Redis、
RabbitMQ、Prometheus 和 Grafana 数据时，才使用会删除数据卷的 `docker compose down -v`。
模板中的密码仅供本地开发，部署环境必须通过安全的密钥管理方式替换。

## 启动

```bash
uvicorn app.main:create_app --factory --reload
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
```

聊天请求：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Authorization: Bearer <llmgw-api-key>' \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "gpt-4.1-mini",
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

成功响应会保留 Provider 的 OpenAI-compatible JSON，并在响应头返回
`X-Request-ID`。服务会把请求状态、租户、API Key、耗时和 token 使用量写入
`request_logs` 表，同时输出一条 JSON 日志，例如：

```json
{"request_id":"...","status":"succeeded","status_code":200,"prompt_tokens":8,"completion_tokens":4,"total_tokens":12,"duration_ms":321,"error_code":null}
```

## 测试

```bash
pytest
```

测试不会调用真实 Provider，也不会消耗 API 额度。

## 压力测试

3.5 使用 k6 和独立的 Compose 覆盖文件。压测环境会把 Gateway 指向本地 Mock
Provider，因此不会请求真实模型。默认使用 10 个虚拟用户运行 30 秒：

```bash
./scripts/run-load-test.sh
```

脚本会自动完成以下操作：

1. 构建并启动 Gateway、PostgreSQL、Redis、RabbitMQ、Prometheus 和 Grafana。
2. 启动延迟可控的 Mock Provider。
3. 创建本次压测专用的租户和 API Key。
4. 运行 k6 并根据阈值返回成功或非零退出码。

自定义负载和验收阈值：

```bash
VUS=50 \
DURATION=2m \
THINK_TIME_SECONDS=0.05 \
P95_THRESHOLD='p(95)<800' \
ERROR_THRESHOLD='rate<0.01' \
./scripts/run-load-test.sh
```

常用参数：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `VUS` | `10` | 并发虚拟用户数 |
| `DURATION` | `30s` | 稳定负载持续时间 |
| `THINK_TIME_SECONDS` | `0.1` | 每个虚拟用户两次请求间隔 |
| `P95_THRESHOLD` | `p(95)<1000` | HTTP 延迟 P95 验收条件，毫秒 |
| `ERROR_THRESHOLD` | `rate<0.01` | HTTP 和业务错误率上限 |
| `MOCK_PROVIDER_DELAY_MS` | `20` | Mock Provider 模拟推理延迟 |

建议先用默认参数确认环境，再逐步提升到 50、100、200 VU。每一档至少运行 2～5
分钟，并同时观察 Grafana 的延迟、错误率、Provider、Outbox 和消费者指标。压测结束后：

```bash
docker compose -f compose.yaml -f compose.loadtest.yaml down
```

这会停止容器但保留数据卷。压测会真实写入请求日志、Outbox 和审计表；若要比较不同
版本，应固定 VU、持续时间、Mock 延迟和宿主机资源，并为每次结果记录 Git commit。
