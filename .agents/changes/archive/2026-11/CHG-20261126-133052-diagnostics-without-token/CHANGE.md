---
schema: coding-change/v1
id: "CHG-20261126-133052-diagnostics-without-token"
title: "诊断免令牌与双入口命令"
level: L3
status: done
owner: "dingyuwen77"
branch: "main"
created: 2026-11-26
updated: 2026-11-26
completion_gate: required
depends_on: []
affected_areas:
  - "diagnostics"
affected_paths:
  - "src/api"
  - "src/application/diagnostics.py"
  - "src/core"
  - "tests/test_diagnostics.py"
  - "config.example.toml"
  - "docs/adp-chat"
contracts:
  - "POST /api/internal/diagnostics/check"
data_changes: []
---

# 变更摘要

诊断接口使用显式开关。启用后的 HTTPS 管理入口与内网 HTTP 可直接提交诊断目标，排障文档提供两套命令。

# 背景、现状与问题

旧接口通过 Bearer 令牌启用和校验。用户要求取消令牌配置与输入，并给出当前内网发布地址 10.102.70.82:3000。

# 事实与证据

FastAPI 的诊断、健康和企微回调有独立路由。一次检查会触发固定目标的 ADP 请求或企微通知，启用后的访问限制由管理代理和内网防火墙承担。

# 目标、成功标准与非目标

通过任一受控入口调用时无需令牌，有效请求仍得到诊断报告。两套命令能按用户给定的地址和实际 HTTPS 管理域名执行。正式服务器的代理、防火墙和部署由现场操作。

# 约束与意图决策

diagnostics_enabled 缺省为 false。旧 diagnostic_token 仅作 TOML 读取兼容，旧配置不会自动启用免令牌诊断。企微回调继续沿既有验签和测试码关联流程。

# 修改方案与决策依据

变更诊断配置、lifespan 和管理路由，并移除令牌比较及示例。排障指南使用当前内网地址，为两个入口分别给出 ADP 与企微命令。

## 备选方案与取舍

删除令牌后直接默认开启会把旧部署的认证边界变成公开调用。用显式开关保留默认关闭状态，并写明开关之前逐个核对公网路由和直连端口限制。

# 需求追溯

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | 内网示例使用当前部署地址，指南包含 HTTPS 和内网 HTTP | docs/adp-chat/04_服务器收发排障.md#ac1 | satisfied | 用户提供端口 3000，文档包含两组地址，本机真实 HTTP 和校验证书的 HTTPS 命令回放通过。 |
| R2 | 已启用的 ADP 诊断无需令牌，旧配置安全兼容 | docs/adp-chat/04_服务器收发排障.md#ac2 | satisfied | HTTP/HTTPS TestClient 不携带令牌获得报告，旧 token 单独存在时返回 404，SSE 回归通过。 |
| R3 | 已启用的企微诊断无需令牌，保留正式回发验签 | docs/adp-chat/04_服务器收发排障.md#ac3 | satisfied | 免令牌诊断与模拟发送、合法和非法签名回发、等待和发送异常测试通过。 |

# 计划改动

诊断配置和匹配代码、管理路由、启动接线、现有日志脱敏配置及定向测试。同步示例和排障指南。

# 验证矩阵

| 验证层 | 是否要求 | 范围 / 证据 |
| --- | --- | --- |
| 行为 / 单元 / 组件 | required | 免令牌、默认关闭及旧配置兼容的行为回归。 |
| 接口 / 契约 | required | 两种 URL 的正常入口、停用 404 与诊断结果。 |
| 集成 / 持久化 / 运行依赖 | required | 正式 FastAPI、企微回调及协议替身运行。 |
| 用户 / 工作流验收 | required | 文档两种命令经过真实本机回环 HTTP/TLS 验证。 |
| 跨组件关键路径 | required | 原企微发送与验签回发通过测试。 |
| 外部依赖 / 供应方探测 | not_applicable | 当前任务只改诊断入口，真实入口和供应方仍走部署后的现场验收。 |
| 构建 / 打包 / 运行 | required | 真实 Uvicorn 回环启动；包配置或容器构建无变更。 |
| 文档 / 治理 / 其他 | required | Bash 语法、文档链接、配置差异、独立审查和 Ready 检查。 |

# 风险、兼容性、迁移与回滚

免令牌入口启用前须限制所有公网转发的诊断路径和直连发布端口。旧配置能启动，诊断默认关闭。回退使用旧镜像对应配置。无数据库迁移或依赖变更。

# 文档、依赖、部署与发布影响

排障指南补 HTTPS 管理域名和当前内网地址。配置示例与本地忽略配置同步为默认关闭。交付范围是当前工作区，现场需部署新版并启用。

# 完成审计

- [x] upstream_re_read：重新核对用户要求与指南验收路径。
- [x] change_coverage：取消令牌、两种入口及当前内网地址均有对应实现。
- [x] reverse_audit：检查开关到路由、两个入口命令到诊断报告及真实回发验证。
- [x] unresolved_cleared：本地要求已有直接证据，现场入口状态按部署边界记录。

# 完成证据与状态

本机 Python 3.14 执行 unittest discover，257 项通过，零跳过。诊断定向 9 项通过。解析指南 5 个 Bash 块、核对 7 个链接，实际 HTTP 和校验证书的 HTTPS 回环请求均成功。独立审查确认最终工作树内容，摘要 7a231f9b520b09b31ce3eb24b3c20df0cfa71593ce860384dffcd00bb1654578。
