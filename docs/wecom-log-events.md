# adp_chat 网关结构化日志事件契约 v1（待实施）

> **本文件定义未来的网关日志事件；当前代码尚未输出此结构。** 架构 Owner：[docs/wecom-gateway-design.md](wecom-gateway-design.md)；实施步骤 Owner：[docs/wecom-roadmap.md](wecom-roadmap.md)。基线：main @ 7acea275d90079e05e25f87b698b5e859d83cd2e。

## 1. 日志解决什么问题，不解决什么问题

日志用于回答：**哪个员工的哪条企微消息**，何时被回调接收、是否去重、何时调用 ADP、等了多久、是否保存了回复、企微是否接受发送请求、在哪一阶段失败以及下一步是否安全重试。

日志**不是**消息任务状态的事实源：即便日志中有 `adp.completed`，也不能替代消息文件中的 `REPLY_SAVED` 判定重启恢复；日志写入失败也不应导致重复执行 ADP 或会议操作。需要业务幂等、投递成功状态，必须查看消息状态文件。

目前 [adp_chat/logging.py](../adp_chat/logging.py) 使用 Python logging 和 RotatingFileHandler，以北京时间人类可读格式记录请求/响应；[adp_chat/client.py](../adp_chat/client.py) 已输出 ConversationId、RequestId、ADP 错误分类及请求耗时。**当前默认会记录问题全文和回答前 100 字**。面向企业微信时应按下述新契约增加网关日志；保留 CLI 原格式与其测试，不静默改变旧消费者行为。

## 2. 一套 logging、一个日志事件结构

建议保留 Python 标准库 `logging` 作为统一入口，网关通过轻量 `emit_event(event_name, ...)` 辅助函数产生事件，使用一个 JSONL Formatter 写入 `logs/events-YYYYMMDD-*.jsonl`。**每行一个完整 JSON 对象，单行追加，便于机器读取和 Cosmos DB 导入**；本地 CLI 继续使用现有文本 Formatter。

不为 Cosmos DB 另写一套并行业务日志，也不把每个 SSE token 当作独立日志事件；目前暂不调用任何 Azure SDK 或联网日志服务。人类排障可使用本地 JSONL 搜索/按 message_key 过滤。

### 2.1 v1 字段及含义

| 字段 | 约束 | 为什么需要 |
| --- | --- | --- |
| `schema_version` | 必填，`adp_chat.log.v1` | 后续格式演进/兼容 |
| `id` | 必填，唯一事件 UUID；写盘和上传后不重新生成 | Cosmos DB 文档唯一 ID、重复导入识别 |
| `timestamp` | 必填，带时区 ISO 8601，北京时间 `+08:00`、毫秒 | 排序与追溯，不能使用无时区字符串 |
| `level` | 必填，DEBUG/INFO/WARNING/ERROR/CRITICAL | 风险分级 |
| `service`、`environment` | 必填，例 `adp_chat` / `dev`、`prod` | 区分来源和环境 |
| `component`、`event` | 必填，稳定组件名和事件名 | 清楚知道发生了什么，而不靠全文搜索 |
| `status` | 必填，`start` / `success` / `duplicate` / `failed` / `unknown` / `skipped` | 事件结果 |
| `message_key` | 回调处理消息时必填，稳定哈希键；服务生命周期事件可 null | 整条企业微信→ADP→企微发送链路的唯一关联入口 |
| `user_ref` | 可选，脱敏/不透明员工引用；不能写员工原文、手机号等个人信息 | 多用户调查与权限受控关联 |
| `conversation_id` | 调用 ADP 时有值 | 关联 ADP 会话（标识符仍按个人信息保护） |
| `request_id` | ADP 请求建立后有值，现有客户端真实 RequestId | 定位当前轮调用 |
| `provider_request_id`、`provider_trace_id`、`record_id` | ADP 返回时可选 | 与厂商侧排障关联，不编造缺失值 |
| `attempt`、`duration_ms` | 发生重试、完成耗时的事件可选；数字且非负 | 重试排查与性能 |
| `http_status`、`error_code`、`error_stage` | 失败时按实际结果填写，不强行补 null 信息 | 区分参数、网络、超时、限流、权限及数据异常 |
| `outcome_uncertain` | 失败/未知时可选布尔值 | 防止把“连接超时”误当“下游业务未执行” |
| `source_file`、`source_line` | 可以从 logging LogRecord 自动取得；必需提供可定位调用点 | 保留现有源码定位能力 |
| `attributes` | 可选小型**白名单**对象 | 仅附加必要统计，如 event_type、reply_chars、queue_depth |

以上是项目建议的**统一结构化日志 Schema**，不是腾讯云/微软官方格式。字段不要因 null 值而虚构身份或返回码。特殊事件允许 null 的关联字段必须明确，整体格式仍是同一 v1。

### 2.2 示例：一轮 ADP 完成（示意，非真实运行结果）

~~~json
{
  "schema_version": "adp_chat.log.v1",
  "id": "b0d0bdd8-20b1-47fe-8b79-57ad2384b3e1",
  "timestamp": "2026-11-06T13:00:03.123+08:00",
  "level": "INFO",
  "service": "adp_chat",
  "environment": "prod",
  "component": "adp_client",
  "event": "adp.completed",
  "status": "success",
  "message_key": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "user_ref": "opaque-user-reference",
  "conversation_id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "request_id": "123e4567-e89b-42d3-a456-426614174000",
  "provider_request_id": null,
  "provider_trace_id": null,
  "record_id": null,
  "attempt": 1,
  "duration_ms": 3123,
  "http_status": null,
  "error_code": null,
  "error_stage": null,
  "outcome_uncertain": false,
  "source_file": "client.py",
  "source_line": 1,
  "attributes": {
    "reply_chars": 42
  }
}
~~~

此示例的 ID 和行号是占位示意。实际值必须来自运行中的关联信息，不能从日志正文臆造。示例可由标准 JSON 解析器独立验证。

## 3. 首期需要的事件目录（约定，待实现）

| `event` | 触发点 | 常见级别 |
| --- | --- | --- |
| `service.started` / `service.stopped` | 服务启动/正常关闭 | INFO |
| `wecom.callback.rejected` | 签名、解密或企业/应用校验失败 | WARNING |
| `wecom.message.received` | 回调身份有效，准备落盘 | INFO |
| `wecom.message.persisted` | 已可靠保存，允许向企微 ACK | INFO |
| `wecom.message.duplicate` | 重复 MsgId，被安全忽略 | INFO |
| `adp.started` | 请求外部 ADP，绑定 RequestId | INFO |
| `adp.completed` | SSE 正式成功完成 | INFO |
| `adp.failed` / `adp.unknown` | 明确失败 / 结果不确定 | WARNING / ERROR |
| `reply.persisted` | 完整回答已持久化 | INFO |
| `wecom.send.started` | 调用主动发送接口 | INFO |
| `wecom.send.accepted` | 企微 API 确认接受发送；不是“已读” | INFO |
| `wecom.send.failed` / `wecom.send.unknown` | 确定失败 / 发送结果未知 | WARNING / ERROR |
| `message.recovered` / `message.blocked` | 重启领取未发任务 / 未知结果阻塞 | WARNING |
| `message.cleanup.completed` / `message.cleanup.failed` | 过期清理统计 / 失败 | INFO / WARNING |

一个异常在真正处理/终止它的 Owner 记录一次，避免上下层重复 ERROR；`request_id` 和 `message_key` 应在生命周期内贯穿，不需要额外造多个意义重叠的 correlation ID。敏感错误只记录白名单诊断字段，堆栈与原始供应商响应不得无保护泄露。

## 4. 安全、容量与保留策略

- **默认不记录**用户原始问题、完整 AI 回答、附件内容、Prompt、Token、AppKey、Secret、EncodingAESKey、Authorization Header、签名 URL 或任意未知属性。
- `user_ref` 使用稳定脱敏标识（例如受密钥保护的哈希）；原始 UserID 仅在消息状态文件按访问权限使用。message_key 可关联消息，禁止拼接未经验证的原始 `MsgId` 作文件路径。
- `attributes` 必须白名单和长度限制，不能把未过滤的 request、response、异常全文直接 dump。
- 生产建议默认 INFO；调试时可临时启 DEBUG，但 DEBUG 仍不可输出用户正文与凭证；SSE 只记录事件计数/耗时等摘要。
- 网关 JSONL 文件按**大小和日期**轮转，建议初始最多 10MiB/片、定期清除已过保留期的文件（示例本地 14 天，须由企业日志政策确定）。崩溃造成的末尾不完整行要由后续采集器安全跳过/隔离，不影响正常消息处理。
- 日志写入失败须可观察、告警且不能当成 ADP/企微业务失败来重试；**消息状态文件写入失败则必须阻止 ACK**。两个存储的故障等级不同。
- 启用日志外传后，对未完成采集/未确认写入的分段保留到恢复或明确的丢失处理；不能执行本地清理后又宣称 Cosmos 记录完整。

## 5. 未来怎样接 Cosmos DB

目标是**修改日志输出/采集边界，不修改所有业务调用点**：

~~~mermaid
flowchart LR
  API["网关 / ADP / 企微发送"] --> PY["统一 Python logging"]
  PY --> FILE["JSONL 分段文件"]
  FILE -.->|"后续阶段：采集器、批量提交、上传检查点"| DB[("Azure Cosmos DB")]
~~~

- 本期仅做日志契约与本地 JSONL，**不配置 Azure SDK/Cosmos 凭证，也不直接从请求热路径逐条写数据库**。
- 后续先确认公司选择 Cosmos DB for NoSQL 还是 MongoDB API，以及实际查询口径、保留期、合规/费用预算；字段映射后再选分区键和 TTL。分区键影响查询成本及迁移，不能提前凭感觉固定。
- for NoSQL 模式可将 `id` 映射为 Cosmos item `id`，配合**稳定分区键**幂等导入，上传记录每段检查点并处理同一事件重复投递。一个 JSONL 行是一个 Cosmos 文档候选，并非原样上传就必然满足容量、分区和安全要求。
- 本地 JSONL 并非 ADP 历史记录、也不保存业务可恢复状态；Cosmos 写入失败时业务仍按本地状态运行，但日志上传落后需有指标与告警。
- 如果主要目标是复杂全文搜索/告警/指标，需另比较 Azure Monitor / Log Analytics；Cosmos DB 适合结构化事件按键查询，但不是免运维的日志分析产品。

官方技术参考：[Azure Cosmos DB 分区键](https://learn.microsoft.com/en-us/azure/cosmos-db/partitioning-overview)、[Cosmos DB TTL](https://learn.microsoft.com/en-us/azure/cosmos-db/time-to-live)。详细实现和验收阶段见 [docs/wecom-roadmap.md](wecom-roadmap.md)。
