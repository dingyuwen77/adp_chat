---
schema: coding-change/v1
id: "CHG-20261126-121459-server-diagnostics"
title: "部署入口与收发诊断"
level: L3
status: done
owner: "dingyuwen77"
branch: "feat/server-diagnostics"
created: 2026-11-26
updated: 2026-11-26
completion_gate: required
depends_on: []
affected_areas:
  - "diagnostics"
affected_paths:
  - "src/api"
  - "src/adp"
  - "src/wecom/sender.py"
  - "src/application"
  - "src/core"
  - "tests/test_diagnostics.py"
  - "config.example.toml"
  - "docs/adp-chat"
contracts:
  - "POST /api/internal/diagnostics/check"
data_changes: []
---

# 变更摘要

在复用健康入口的基础上，增加一个受保护的 ADP / 企微收发检查。响应给出最后确认阶段、失败阶段、实际原因和排查方向。

# 背景、现状与问题

健康探针检查应用本机和已启用的网关状态。它无法观察 ADP 完成事件和企微收发。发生事故时，现有业务结果还需要足以区分确认、完成及失败的阶段线索。

# 事实与证据

| 编号 | 事实 | 来源 |
| --- | --- | --- |
| E1 | Docker 通过现有健康接口检查应用 | `scripts/run.sh` |
| E2 | 企微正式回调已验证身份和签名 | `src/api/wecom_routes.py` |
| E3 | ADP 正式结果来自成功完成事件；企微发送结果独立保存 | `src/adp/async_client.py`、`src/application/wecom_worker.py` |

# 目标、成功标准与非目标

服务器部署人员能按指南检查外网入口、ADP 收发和企微两向链路。故障报告与业务日志可按请求标识指出证据边界。交付范围至主分支，现场入口和平台配置由部署人员使用其授权验证。

# 约束与意图决策

诊断由可选配置启用，管理 Bearer 令牌独立。ADP 使用隔离测试 AppKey、身份和会话。企微测试回发只接管指定成员的诊断前缀，先通过正规验签。各渠道结果保持各自的判据。诊断不创建业务数据库记录或新增依赖。

# 修改方案与决策依据

复用健康接口、异步 ADP 会话实现、企微发送器、已认证回调及现有业务日志。一次发起，一次返回，通过单轮并发限制控制外部副作用。

## 备选方案与取舍

仅提供网络探针缺少协议收发证据。独立任务提交与状态查询可持久化进度，但增加用户操作和状态维护。采用有时间上限的单接口完成本次需求。

# 需求追溯

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | 提供外网和服务器分层验收 | docs/adp-chat/04_服务器收发排障.md#ac1 | satisfied | 已安装 wheel 的回环 Uvicorn 健康检查及命令复测，目标公网环境按文档现场验收。 |
| R2 | 受保护的 ADP 收发及失败阶段 | docs/adp-chat/04_服务器收发排障.md#ac2 | satisfied | tests/test_diagnostics.py 实际 HTTP 路由、独立身份、SSE 完成、ACK 后断流、DNS 和鉴权回归。 |
| R3 | 验证企微发送接受和真实签名回发 | docs/adp-chat/04_服务器收发排障.md#ac3 | satisfied | tests/test_diagnostics.py 实际生命周期、模拟 API 响应、已签名回发、未回发和超时测试。 |
| R4 | 按失败阶段和原请求标识定位 | docs/adp-chat/04_服务器收发排障.md#ac4 | satisfied | 诊断阶段断言、现有业务日志回归和用户指南命令检查。 |

# 计划改动

代码位于 api、application、adp、wecom 与 core。测试覆盖正式入口及隔离协议。配置示例、运维指南和主导航一起更新。

# 验证矩阵

| 验证层 | 是否要求 | 范围 / 证据 |
| --- | --- | --- |
| 行为 / 单元 / 组件 | required | 现有和新增 unittest 行为回归。 |
| 接口 / 契约 | required | FastAPI 端点、鉴权、输出阶段和并发。 |
| 集成 / 持久化 / 运行依赖 | required | 正式回调、真实临时 SQLite、正式客户端和隔离 HTTP 替身。 |
| 用户 / 工作流验收 | required | 从实际 HTTP 发起两种探测，回放签名回发，核对排障文档。 |
| 跨组件关键路径 | required | 企微发送到正式回调的隔离闭环。 |
| 外部依赖 / 供应方探测 | not_applicable | 本次仓库交付使用隔离协议替身，正式服务器的渠道网络、密钥和测试成员按运维指南现场验证。 |
| 构建 / 打包 / 运行 | required | wheel 从源码成功构建，安装后的实际 Uvicorn 启动。 |
| 文档 / 治理 / 其他 | required | 逐链接检查，Bash 语法和本机请求验证，独立审查及 Git 门禁。 |

# 风险、兼容性、迁移与回滚

旧配置省略诊断分组可以继续加载。诊断仅在显式配置后可用。旧镜像回滚时移除新增配置分组。没有 DB Schema、依赖和部署脚本变更。发送提交后缺少响应时保持未知，诊断无需持久化外部重试。

# 文档、依赖、部署与发布影响

新增用户排障操作文档，链接同步至 README 和部署文档。发布网络按路径放行健康检查和企微回调，诊断只在管理入口开放。现场生产部署不属于本次 Git 交付。

# 完成审计

- [x] upstream_re_read：已按用户确认范围重读文档验收条款。
- [x] change_coverage：外网检查、ADP、企微双向和失败证据均有入口及可执行指南。
- [x] reverse_audit：回查请求到上游、回调到诊断等待、部署文档所指接口及证据范围。
- [x] unresolved_cleared：仓库交付要求已映射验证，真实环境边界明确标注。

# 完成证据与状态

Windows Python 3.14 下执行 unittest discover，256 项通过，无跳过。已构建并安装 wheel，回环 Uvicorn 对照文档命令通过，核验 7 个文档引用、3 个导航、4 个 Bash 块和 TOML 分组。独立审查复核 505324f78b950eceed1bc7a8149ac08aafae43fc，无未解决问题。交付记录见 PR #42。

[PR #42](https://github.com/dingyuwen77/adp_chat/pull/42) 已合并，main 提交 8b738e77c5e7577132017dcd0c70fff20f7fa7ee。最终 PR CI 和 main push CI 均通过。 [PR CI](https://github.com/dingyuwen77/adp_chat/actions/runs/45755833523) · [main CI](https://github.com/dingyuwen77/adp_chat/actions/runs/45756116504)

本机 main 已同步。验收依据保存在仓库排障文档。
