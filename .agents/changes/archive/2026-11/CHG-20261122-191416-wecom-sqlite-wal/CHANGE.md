---
schema: coding-change/v1
id: "CHG-20261122-191416-wecom-sqlite-wal"
title: "企微网关 SQLite WAL 与千用户并发"
level: L3
status: done
owner: "Codex"
branch: "fix/wecom-sqlite-arm64"
created: 2026-11-22
updated: 2026-11-22
completion_gate: required
depends_on: []
affected_areas:
  - "gateway"
affected_paths:
  - "src"
  - "scripts"
  - "tests"
  - "docs"
  - "config.example.toml"
  - "Dockerfile"
  - "compose.yaml"
  - "compose.local.yaml"
  - "README.md"
contracts:
  - "企业微信回调及通知"
  - "应用 TOML"
  - "普通文本日志"
data_changes:
  - "gateway_messages"
  - "旧消息 JSON 切换"
---

# 变更摘要

将企微网关的消息状态从逐文件 JSON 改为单机 SQLite WAL。以真实加密回调、SQLite 和本地 Fake ADP 验收不同用户可靠接收、同时在途及完成回复，保留同用户互斥和未知状态恢复。

# 背景、现状与问题

基线 6867ac1e9a9d7fe78addb41b3de53d90ca9a0ddb 在持有全局锁时同步写入并替换消息快照。Worker 每次唤醒枚举消息。历史、处理中、未知都占用同一容量，原同步问答日志会截断，企微链路只记录应答长度。公共入口语义、员工会话派生和 ADP SSE 协议由现有实现及回归确定。

# 修改方案与决策依据

- 保留 JSON 并分片仍需要跨文件管理去重、状态与恢复。PostgreSQL 可以支持多实例，但增加数据库服务。本机单 Worker 和用户确认的 SQLite WAL 适合本期部署约束。
- SQLite 提交包括去重、容量、每会话唯一活跃记录及初始状态。写入线程独占连接，按入队顺序将接收和状态更新合并为最多 32 条的 FULL 提交。读连接独立。Worker 对索引查出的记录按预期状态和版本提交后才出站。事务内无网络调用。
- ADP 连接池组各持有最多 64 个连接，空闲连接位轮转、全局上限按 `max_active_requests` 精确分配。此调整针对千条连接期间的同步连接池扫描开销。
- 使用普通文本格式记录已提交问题和回答，脱敏后保留完整文本。后台队列设上限，独立监测溢出和写失败。
- 保留企微验签、重复 ACK、忙碌通知、通知有序性、ADP 未知不重试和状态保留。沿用单 Worker 与现有 CLI/HTTP 会话文件。
- 启动空库且旧消息文件不参与持久化。生产切换先核查未完成及未知外部操作。数据库有业务写入后原则上前向修复。真实 ADP、正式企微、发版和部署不属于本轮验证。

# 需求追溯

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | WAL 空库、原子接收、去重、同用户互斥、状态机及恢复 | #39 / AC1 | satisfied | Windows 247 项回归与双进程强杀场景，真实 SQLite 批事务、写锁竞争、磁盘配额故障、在线备份复开。 |
| R2 | 完整问题和回答、单行日志及敏感数据清洗、异步轮转 | #39 / AC2 | satisfied | 完整文本、日志权限、签名 URL 和员工脱敏以及队列溢出、轮转与磁盘失败回归。 |
| R3 | 索引调度、有界 ADP、企微、数据库、图片资源，兼容现有协议 | #39 / AC1 | satisfied | 247 项回归验收兼容调用。连接池组覆盖 1、129、1000 的连接上限，取消归还连接位，图片预算及收发容量保护用例均通过。 |
| R4 | 100、300、500、1000 本机负载及持续、故障验证，Fake ADP 和模拟企微 | #39 / AC3 | satisfied | AMD64 持续 278.523 秒，三轮各 1000 条 SSE 同时在途 30 秒且全部模拟发送。各轮最大 ACK 3193.2、3033.4、2350.7 ms，混合轮 3868.6 ms。5200 行、5200 条问题、4900 条答案、27 个日志文件，checkpoint 805/805。双架构修复后 AMD64 再验证千人，最大 ACK 3082.3 ms，混合轮 2586.3 ms。 |
| R5 | 配置、运维、备份恢复、消息库切换与回滚边界 | #39 / AC4 | satisfied | 确定性回切与在线备份复开通过。ARM64 失败记录载入系统 3.46.1，已修复。b3925e8a6f65fa52a6f33c0f438a98638a5093fb 的双架构实际镜像从专用目录加载 3.54.1，持久卷、正常重启、SIGKILL 恢复均通过。 |

# 计划改动

- [x] 核对基线、现有状态机和入口，锁定 Agent_Skills dbae4a011f78ddeaf6e7a0411d3fb40df858fbed。
- [x] 建立可观察行为 Red，生产入口接入 SQLite、日志和调度，完成 Green。最新本机候选 b4bd45c10147a35d0556d72f118f4da787f99605。
- [x] 同步配置、运维脚本与文档，运行回归和隔离负载。Windows 247 项，Linux 专项 73 项，AMD64 Compose 持久运行、故障和 278.523 秒负载通过。
- [x] 完成两次独立审核和合并，已读取对应 PR CI 与远程 main CI。
- [x] 将 5 条验收证据写入 #39 并回读关闭，确认本机主分支和远端任务分支状态。
- [x] 回收测试脚本自有网络、容器、卷、两种架构的专用镜像、已识别的残留测试目录。归档后移除承载它的工作树和两个本地任务分支。

# 验证矩阵

| 验证层 | 是否要求 | 范围 / 证据 |
| --- | --- | --- |
| 行为 / 单元 / 组件 | required | 去重、互斥、事务失败、保留清理、状态恢复及日志文本 |
| 接口 / 契约 | required | 正式加密回调、ACK 错误语义、发送顺序、旧配置 |
| 集成 / 持久化 / 运行依赖 | required | 实际 SQLite WAL、锁竞争、重启和备份恢复 |
| 用户 / 工作流验收 | required | 1000 独立用户同时在途且完成，涵盖重复、忙碌及资源溢出 |
| 跨组件关键路径 | required | 本地进程 HTTP 回调至 SSE 及模拟企微，完整记录与持久结果 |
| 外部依赖 / 供应方探测 | not_applicable | 用户要求本机隔离，Fake ADP 和模拟企微 |
| 构建 / 打包 / 运行 | required | Docker 运行时 SQLite 版本与持久挂载、配置和重启 |
| 文档 / 治理 / 其他 | required | 文档链接、实际配置、独立 Review、CI、main 和本地归档 |

# 风险、兼容性、迁移与回滚

单写连接保持 FULL 同步，按提交结果决定 ACK。恢复根据 ADP 和发送两个状态推进，对已武装操作保留未知证据。容量按未完成状态计算，历史保留到既有清理窗口。旧 JSON 数据不导入且不在本任务操作生产文件。SQLite 在线备份先校验再保留。原始员工身份仅用于路由或存储，日志隐藏身份和已知凭据。

# 完成审计

- [x] upstream_re_read: 重读 live #39 并通过 canonical 合同检查，对照用户引用方案最后的验收、运行限制与迁移决策。
- [x] change_coverage: b4bd45c10147a35d0556d72f118f4da787f99605 已验证网关行为，后续 b3925e8a6f65fa52a6f33c0f438a98638a5093fb 精确覆盖 ARM64 加载，生产应用源码逐文件相同。AC5 按交付阶段执行。
- [x] reverse_audit: 核对入口与状态、完整日志、Schema 和部署路径、普通与回归测试、构建与运维文档；已核对候选 diff、模板 TOML 与本地文档链接。
- [x] unresolved_cleared: 实现范围 R1–R5 已覆盖。本机 Fake 证据不覆盖生产宿主和真实三方容量。Release、生产部署与实际数据操作超出交付范围。

# 完成证据与状态

- 独立审核精确覆盖 b4bd45c10147a35d0556d72f118f4da787f99605 和 b3925e8a6f65fa52a6f33c0f438a98638a5093fb。PR #40 的 CI 45022789636、首次合并 122715901dd4d38c4f1a21ddbe1f15653bde561a 的 CI 45022997662、PR #41 的 CI 45024923567、最终 main 07523417d795d3b61e5d8a470c71716a3b734b40 的 CI 45025337983 均通过。最终 main 文件树 d1e2fccd671fd8ba717648b8b9d5b73da2495b5f 与修复候选一致。现有 `.github/workflows/tests.yml` 与任务基线相同。
- 两轮真实强杀网关分别发生在 ADP 已出站和企微进入模拟通知后，原 RequestId 和通知尝试各一次。Windows 247 项、Linux 73 项回归，以及独立本机回调两次重启均通过。正式生产服务调用无，验证使用 loopback Fake ADP 与模拟企微。
- 修复后 AMD64 镜像 sha256:65ef6490e47569010f622f4e76cd603e8ac325c6e347b2c5e0a70c469c5792ef 与原性能镜像的 SQLite 二进制和网关相关源文件哈希一致。ARM64 镜像 sha256:70c7f9ec35d0af2ed153cea6b273d77b3fd77bc9d7ed0bd07ce66e21b76d59b6 在 Docker Desktop 仿真通过同一 Compose 路径。两者均实际映射 /opt/adp-sqlite/libsqlite3.so.0。
- 本地 main 已快进到同一合并提交。远程任务分支已自动清理。#39 五条 Acceptance 已写入并回读，关闭原因为 completed。目标服务器和真实上游容量应在生产切换前按运维流程独立核验。
