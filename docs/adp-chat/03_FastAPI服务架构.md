# 单用户 FastAPI 服务架构

## 目标和边界

使用现有 ADP HTTP SSE V2 一轮对话能力，提供给可信内网其他服务的同步 HTTP 接口。固定 UserId、VisitorId 和持久 ConversationId，使用一个进程内互斥锁实现**一次仅一轮**。第一版不提供用户管理、会话管理 HTTP 接口、认证、HTTPS 代理、文件上传、WebSocket 或对外 SSE。单次脚本和交互 CLI 使用相同应用服务层。

## 调用链与实现

`HTTP POST → Pydantic 输入校验 → ChatService 实例互斥锁 → ADP chat() → response.completed → ChatResponse`。

ADP 请求固定使用 `Stream: "disable"`，让回复文本完整生成后输出。响应仍使用 SSE（`text/event-stream`）承载状态和消息事件，内部必须继续解析这些事件，确认 `response.completed` 成功后才能返回正式回复；不能直接按普通 JSON 读取上游响应。开关定义见[腾讯 ADP HTTP SSE 文档](https://cloud.tencent.com/document/product/1759/129202)。

现有 `incremental` 只控制文本事件采用追加还是替换格式，`streaming_throttle` 控制流式模式下的回包频率，两者均不用于开启流式文本输出。旧配置继续兼容，对外 HTTP 字段、会话持久化、并发限制和错误处理沿用现有契约。

- 服务生命周期和错误处理在 [src/api/app.py](../../src/api/app.py)；创建 HTTP 服务前完成配置/持久会话初始化，失败则不启动。
- 路由在 [src/api/routes.py](../../src/api/routes.py)，HTTP Schema 在 [src/api/schemas.py](../../src/api/schemas.py)。
- 单用户调用在 [src/application/chat_service.py](../../src/application/chat_service.py)；请求共享同一个服务实例，非阻塞获取实例内的互斥锁，失败返回 409，`finally` 释放。
- 腾讯 ADP 单轮请求和正式回复解析在 [src/adp/client.py](../../src/adp/client.py)；SSE 处理在 [src/adp/sse.py](../../src/adp/sse.py)。
- 单会话存储在 [src/storage/session.py](../../src/storage/session.py)；路径由 `[session].session_file` 指定，相对路径以配置目录为基准，开发模板为 `runtime/session.json`。HTTP 启动时先恢复或原子保存 ID；单次脚本和 CLI 在首次有效消息前初始化。

## HTTP Contract

`POST /api/v1/chat`

输入（只有一个字段）：

```json
{"message":"你好"}
```

成功的响应字段：`text: string`、`conversation_id: string`、`request_id: string`、`record_id: string|null`、`reply_method: int|null`、`reply_contents: array<array<object>>`。回复文本来自正式 `reply` 消息，不包括模型思考和工具消息；非文本内容按消息分组完整保留。

错误结构：

```json
{"error":{"code":"CHAT_BUSY","stage":"service","message":"当前 ADP 会话正在处理请求。","request_id":null}}
```

- `200`：收到 ADP 正式成功完成事件；不表示整个业务问题完成。
- `409`：会话占用，无后台排队。
- `422`：Body 非法或文字为空；不进入 ADP。
- `502`：上游拒绝、断流、协议错误等。
- `503`：请求处理期间发生配置、会话或日志资源异常。若配置或会话在启动初始化时就失败，进程不会正常开始服务，不能期待收到 HTTP 503。
- `504`：ADP 超时，是否已执行未知。
- `500`：未经预期的应用错误，不能将其视为 ADP 成功。

`GET /healthz` 返回 `{"status":"ok"}`，不请求腾讯 ADP，也不逐次检查日志写入和会话文件。配置与会话已在服务启动时初始化；离线部署脚本另行执行日志/目录预检。健康返回 200 不表示 ADP 可用或所有持久化资源始终可写。

## 失败边界

互斥锁只约束**同一个 ChatService 实例**；HTTP 请求共享该实例，本版只允许 `--workers 1`、一个 Docker 实例。CLI 不得在 HTTP 服务运行期间操作同一个 `session.json`，同一进程也不能创建多个服务实例同时操作该文件。进程在 ADP SSE 期间退出，Docker 虽可重启服务并恢复 ConversationId，却无法恢复该次请求，也不能据此证明 ADP 没有执行。不能自动重发可能触发会议 MCP 写操作的超时请求；正式承接写操作前另行设计真正的业务幂等和结果核查。

此接口**无应用鉴权，明文 HTTP**；任何能够访问内网端口的客户端均可使用共享 ADP 身份。部署时必须在服务器网络边界限制调用来源，不允许暴露到公网。
