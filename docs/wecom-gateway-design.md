# 企业微信自建应用消息网关：架构设计（待实施）

> **状态：方案设计，尚未实现企业微信服务。** 核对基线：main @ 7acea275d90079e05e25f87b698b5e859d83cd2e（2026-11-06）。正式编码前要重新核对最新 main。  
> 相关：[docs/wecom-roadmap.md](wecom-roadmap.md)（实施计划）、[docs/wecom-log-events.md](wecom-log-events.md)（结构化日志契约）。

## 1. 为什么需要这个服务

目标：企业内部员工在**企业微信自建应用原生消息会话**里发文字，Python 网关负责**验签、可靠接收、转发给 ADP、发送应用消息回复**。ADP 承担自然语言、追问、上下文、Agent/MCP 等业务逻辑。网关**不判断“本题是否完成”**，每次收到员工新消息只调用一次 ADP 单轮对话。

首期硬范围：**一个企业、一个自建应用、一个 ADP 逻辑应用、员工单聊、纯文字、一个运行实例、本地文件状态持久化**。不引入数据库、Redis、独立任务队列产品、H5 自定义聊天 UI 或群聊；附件、微信客服、跨企业身份、会议 MCP 可信授权与 Cosmos DB 实际上传均属后续阶段。

## 2. 当前事实与待实现边界

| 能力 | 已有事实 |
| --- | --- |
| ADP 一轮对话 | [adp_chat/client.py](../adp_chat/client.py) 的 chat() 接受 conversation_id、生成独立 RequestId、等待正式 response.completed；默认 CLI 不做追问完成判断 |
| SSE 网络 | [adp_chat/sse.py](../adp_chat/sse.py)、[adp_chat/transport.py](../adp_chat/transport.py) 为同步阻塞 HTTPS |
| 配置 | [adp_chat/config.py](../adp_chat/config.py)、[config.example.toml](../config.example.toml)：现有 user_id 是静态配置，网关必须用已验签的消息按轮覆盖 |
| 会话 | [adp_chat/session.py](../adp_chat/session.py)：单用户 JSON 会话状态，**不能直接用于多用户并发** |
| 调试 | [main.py](../main.py)、[README.md](../README.md)：继续保留一次运行一轮的 CLI |
| 日志 | [adp_chat/logging.py](../adp_chat/logging.py) 当前为人类可读轮转文件，默认记录完整问题和最多 100 字回复；尚无结构化网关日志 |
| 测试 | [tests/test_single_turn.py](../tests/test_single_turn.py)、[tests/test_logging.py](../tests/test_logging.py)、[.github/workflows/tests.yml](../.github/workflows/tests.yml) |

**目前不存在**企业微信 HTTP 回调、消息存储、主动发送和常驻服务；下面所有服务路径及状态都属于拟实现的契约，不得写成已可运行。

## 3. 整体架构

~~~mermaid
flowchart TD
    U["企业微信员工"] <--> W["企业微信自建应用原生消息"]
    W -->|"HTTPS 加密回调"| R["反向代理 / HTTPS"]
    subgraph G["adp_chat 单实例 Python 网关"]
      A["GET 验证 / POST 回调"]
      V["验签、解密、校验企业与用户"]
      F["文件消息状态：去重与恢复"]
      Q["后台调度：每用户串行，不同用户有限并行"]
      C["复用现有 chat() SSE 客户端"]
      S["企微主动发应用消息"]
      L["统一结构化日志 JSONL"]
      A --> V --> F --> Q --> C
      C --> F
      F --> S
      A -.-> L
      Q -.-> L
      S -.-> L
    end
    R --> A
    A -.->|"可靠落盘后快速 HTTP 200"| W
    C <-->|"AppKey / UserId / ConversationId"| ADP["腾讯 ADP Multi-Agent"]
    S -->|"应用发送接口，不依赖员工在线"| W
    F --- FS[("runtime/messages 目录")]
    L --- LF[("logs/events*.jsonl")]
~~~

**为何必须先写文件、再返回 200：**ADP 一轮最长可能比企微回调的短响应窗口长得多。企微超时会有限重推；若只把消息放到内存、已经返回 200 后进程退出，消息就无法恢复。网关可靠落盘后快速 ACK，后台按并发名额调用 ADP，再主动给员工发回复。

~~~mermaid
sequenceDiagram
    participant U as 员工
    participant W as 企业微信
    participant G as Python 网关
    participant F as 文件状态
    participant A as 腾讯 ADP
    U->>W: 发送文字 (MsgId=M1)
    W->>G: 加密 POST 回调
    G->>G: 验签 / 解密 / 验证企业与员工
    G->>F: 按 MsgId 去重、保存 RECEIVED
    F-->>G: 可靠写入成功
    G-->>W: HTTP 200 空响应
    G->>F: ADP_STARTED
    G->>A: chat(文字, UserId, ConversationId)
    A-->>G: SSE response.completed / reply
    G->>F: REPLY_SAVED
    G->>W: 调用主动应用消息发送
    W-->>G: API 接受发送
    G->>F: SEND_ACCEPTED
    W-->>U: 企微客户端展示回复
~~~

## 4. 身份与唯一会话

1. 回调 GET 验证 URL，POST 验签/解密后核实 CorpID、AgentId、消息类型与 FromUserName。Token + EncodingAESKey 负责企微回调可信性；应用 Secret 用来换取企微 access_token；ADP AppKey 只用于调用 ADP。Secret 均不得进入仓库、业务消息文件或日志。
2. 单企业内部员工场景，ADP UserId **直接取**已验证的企业微信 UserID；兼容当前 ADP V2，VisitorId=UserId。不支持将聊天正文中自称的用户身份传给模型作为鉴权。
3. **每个员工只使用一个 ADP ConversationId**。但企微 UserID 往往短于 ADP V2 要求的 32～64 个字符，不直接原样赋值。建议 `sha256("adp_chat:wecom:v1" + 明确分隔的 CorpID、AgentId、ADP 逻辑应用名、UserID)` 的 64 位小写十六进制串，无须维护 UserID→ConversationId 映射文件。
4. ADP 逻辑应用名是固定配置值，不能使用可轮换的 AppKey；上线后派生算法、字段顺序/分隔和命名空间不可静默改变。测试应证明同用户跨重启 ID 一致、不同用户分离。
5. ConversationId 恒定只确保网关复用同一会话标识，**不保证 ADP 永久记忆全部对话**；记忆由 ADP 应用配置与平台约束决定。
6. 后续 ADP→会议 MCP 的写操作必须另外完成可信服务身份及用户权限校验、业务幂等；不能直接信任模型生成的 UserID。

## 5. 用户退出应用、再次进入会怎样？

- **员工发消息后离开应用**：回调已可靠落盘，ADP_STARTED 任务在服务器继续执行，用户退出界面不影响后台请求。
- **在回复生成之前再次打开**：还没有最终回复，因此不能“取回一个尚未生成的回答”；用户可能只看到原来的消息。网关暂不提供进度查询 API。
- **ADP 完成之后**：网关通过企微 /cgi-bin/message/send 主动向原员工发送应用消息；无需用户一直在线，也不需要员工重新发问。之后在原生应用会话中的可见性由企微客户端/消息保存设置决定。
- **不能保证的内容**：主动发送 API 返回成功只表示企微 API 接受请求，不能推出设备已同步、用户已阅读、所有设备永久可见；必须使用真实企业微信客户端测试“离开→等待→重进、跨设备”。
- **发送失败或结果未知**：回复快照仍在网关的 REPLY_SAVED 或 SEND_UNKNOWN 状态，不再次请求 ADP；需要按错误类别恢复/核查。打开应用不是恢复命令，不应据此自动重放消息。
- **如果将来改为自建 H5 聊天首页**：原生应用会话与 H5 页面不是同一个前端历史系统，届时才需要额外的受身份认证的历史/任务查询 REST API。本期不做。

## 6. 文件方案与 JSONL 比较

消息状态是“这条消息**现在**应做什么”，运行日志是“这条消息**曾经**发生什么”。

| 选型 | 单消息 JSON 快照（本期采用） | 追加式 JSONL 消息事件流（本期不采用） |
| --- | --- | --- |
| 单条 MsgId 查找 | 哈希文件名直接定位 | 需要扫描或额外索引 |
| 状态更新 | 写临时文件并原子替换 | 追加新事件，重启需重放恢复最新状态 |
| 文件异常 | 单条快照独立修复/隔离 | 处理不完整尾行、重放、轮转、压缩 |
| 过期删除 | 按终态删除单文件 | 分段轮转与压缩，不能直接删除中间事件 |
| 换数据库 | 以稳定状态接口迁移 | 仍需事件投影与状态迁移 |

JSONL **更适合日志**。未来数据库迁移的低成本来自稳定的 `put_if_absent/get/update/list_unfinished/purge` 操作语义、固定消息主键和版本化状态，而不是文件扩展名。

**单实例文件状态约束（待实施）：**

- 对普通文字消息，以 `(CorpID, AgentId, MsgId)` 生成稳定的文件名 `message_key`；未含 MsgId 的事件单独处理，不冒充聊天消息。
- 去重检查和首次落盘在单一写入进程内互斥；同目录临时文件、flush/fsync、原子发布后才能 ACK；重复 MsgId 不重投 ADP。
- 保存 schema_version、message_key、受信身份、接收序号、文字正文（未处理期间）、ConversationId、ADP RequestId、处理状态、回复快照（发送之前）以及必要错误。
- 单进程、独占数据目录；Uvicorn 1 个 worker，但进程内部允许有界并发线程。PyCharm 与 Docker **不能同时指向同一 runtime 文件目录**。
- 同一会话按**可靠接收顺序**一次只允许一个 ADP 请求，不同会话并行；企微网络重排的真实消息发送顺序不一定可从 MsgId 恢复。
- 状态恢复前先读取文件并校验结构/版本，文件损坏不可静默当作新消息重入。

### 状态与失败处理

~~~text
RECEIVED → ADP_STARTED → REPLY_SAVED → SEND_STARTED → SEND_ACCEPTED
                ↘ ADP_UNKNOWN                  ↘ SEND_UNKNOWN
明确不可恢复失败可进入 FAILED_FINAL
~~~

`PROCESSING` 口头上方便理解，但正式状态必须区分 **ADP 正在处理、回复已保存、企业微信正在发送**，否则无法正确恢复。

| 失败/重启位置 | 本期最小正确处理 |
| --- | --- |
| 验签失败、企业不匹配 | 拒绝，不调用 ADP |
| 消息落盘失败/磁盘满 | 返回失败，不返回“收到”；企微有限重试仍可能最终丢消息 |
| 相同 MsgId 重复回调 | 回复 200，不生成重复 ADP 任务 |
| RECEIVED 尚未提交 ADP | 重启自动领取 |
| 已开始发出 ADP 请求但 SSE 超时/进程退出 | 保守标记 ADP_UNKNOWN，不盲目重放可能触发会议 MCP 的业务请求 |
| ADP 成功且 REPLY_SAVED | 继续企微发送，不重新请求 ADP |
| 企微发送明确可重试失败 | 有界退避重试；明确永久错误标 FAILED_FINAL |
| SEND_STARTED 后发送结果未知 | SEND_UNKNOWN，不自动重复推送可能已送达的回复 |

当同一用户出现 ADP_UNKNOWN 时，该会话后续消息暂时阻塞，避免不确定的前一轮影响上下文；其他用户不受影响。必须有错误日志、告警/管理员检查入口与明确处置，否则任务可能永久停在 UNKNOWN。对跨企微/ADP/MCP 副作用**不能承诺端到端 exactly-once**。

### 文件自动清理规则（推荐起始值，配置可调整）

- `SEND_ACCEPTED` 进入终态 **7 天后**删除原始问题/回答快照，保留不含正文的去重最小记录，至**首次接收后 30 天**可删除。
- `FAILED_FINAL` 保留可定位失败记录至少 30 天；敏感正文按更短保留策略清理，到期按已授权处置/归档规则删除。
- `RECEIVED`、`ADP_STARTED`、`REPLY_SAVED`、`SEND_STARTED`、`*_UNKNOWN` **绝不按年龄直接删掉**。恢复、确认或人工处置后才能转换到可清理终态；对异常积压设置磁盘使用率、文件量与最老未完成消息告警。
- 在启动和定期间隔执行小批清理；清理前重新检查终态及记录版本，不能误删运行中的任务。需要持久卷与备份，不能靠清理掩盖磁盘写入失败。
- 7/30 天是**推荐配置默认值，不是企业微信官方重试/保存期限**。保存期最终服从企业的数据保护要求。

## 7. 运行、认证与外部准备

- 本地保留 [main.py](../main.py) CLI；拟新增独立的 FastAPI 网关运行入口，PyCharm 可直接启动。模拟企微回调用加密协议夹具；真实回调必须提供企业微信可达的 HTTPS URL。
- Docker Compose 初期仅一个网关容器、一个应用进程；挂载消息文件与日志卷，可复用企业既有 HTTPS 反向代理。禁止在相同存储目录上启动第二实例。参数包括 CorpID、AgentId、应用 Secret、Token、EncodingAESKey、ADP AppKey、ADP 逻辑应用名、最大 ADP 同时请求数、状态目录及清理/日志策略。
- 企微管理员准备 CorpID、AgentId、自建应用 Secret、接收消息 API 设置权限、Token/AES Key、测试员工可见范围、域名和 API 出口可信 IP。
- 应用回调为 GET URL 校验与 POST 加密 XML 消息；回复由应用 Secret 获取 access_token 后调用 **企微应用消息发送 API**。不要混用微信客服 /cgi-bin/kf/ 接口。
- 企业微信主动消息接口接受并非最终到端成功或用户已读，不额外造不存在的“读回执 API”。

## 8. 实施验收与未来切换

交给 Codex 按 [docs/wecom-roadmap.md](wecom-roadmap.md) 分阶段实现；最小真实验收覆盖两个员工隔离、同员工连续追问、重复 MsgId、回调落盘失败、不同用户并发/同用户串行、ADP 与企微发送不同失败边界、进程重启、消息清理、员工退出并返回、PyCharm 和 Docker 实际运行。

后续替换数据库时：先冻结本地消息写入、备份状态、迁移未完成消息与有效去重记录、核主键/版本/序号一致，再切唯一写入 Owner；**不允许旧文件存储与新数据库同时对同一消息竞争处理**。Cosmos DB 主要计划用于结构化日志，见 [docs/wecom-log-events.md](wecom-log-events.md)。

## 9. 外部文档与事实强度

- [腾讯 ADP HTTP SSE V2（腾讯云，2026-09-18）](https://cloud.tencent.com/document/product/1759/129202)——会话 ID 与 UserId / VisitorId 格式。
- [企业微信接收消息概述（开放文档镜像）](https://wdk-docs.github.io/wework-docs/server/basic/message-push/receive-messages-and-events/)——短回调窗口、有限重投、MsgId 排重。实施时需与企业微信开发者中心当前文档比对。
- [企业微信发送应用消息（开放文档镜像）](https://s.apifox.cn/apidoc/docs-site/406014/api-10061358)——主动消息协议；非微信客服接口。
- 当前**没有**覆盖所有企微客户端“退出/重进/多端历史可见性”的一手实测证据，因此把它列为实际联调验收而非协议保证。
