---
schema: coding-change/v1
id: "CHG-20261126-140602-formal-adp-dynamic-wecom"
title: "正式 ADP 与按请求指定企微诊断成员"
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

ADP 诊断连接正式应用，每次使用固定测试身份和新的 ConversationId。企微诊断由请求指定本次接收和回发的成员。

# 背景、现状与问题

既有诊断分别使用另配的 ADP AppKey 和固定的企微成员。独立应用的成功无法验证正式应用，固定成员也限制了排障时的协作。

# 事实与证据

诊断与业务共用出站 HTTP/SSE 实现。正式企微业务使用员工的账号派生 ADP 身份及业务会话。企微回调先验证签名和企业及应用归属，再检查测试码。示例文件在本任务开始前已含用户定制值。

# 目标、成功标准与非目标

诊断复用正式 AppKey，保留单独 UserId 并按次生成新会话。企微命令选择一个成员，先确认平台接受发送，再验证该成员的回发。同步示例与两种免令牌命令，并保留用户的其他示例修改。范围为本地代码和文档验证。

# 约束与意图决策

测试 UserId 由运维配置采用单独保留的账号，避开业务 ADP 及任何企微员工身份。应用自动拦截与固定 UserId 或 VisitorId 重合的配置。正式 ADP 的测试身份或流程须在 ADP 端限制工具写入权限。企微请求复用现有单成员校验，诊断入口由可信网络限制。

# 修改方案与决策依据

ADP 诊断客户端继承共享配置的 AppKey，覆盖诊断身份，独立生成每轮会话。企微在签名验证后，以本轮成员和测试码匹配回发。旧 TOML 字段只作启动兼容，所有实际收件人以命令提供的 user_id 为准。

## 备选方案与取舍

专用 AppKey 可隔离应用权限，但无法验证正式应用。由服务直接查询组织成员目录会引入额外权限和运行依赖。采用正式 AppKey 配合专用测试身份，在配置侧预留身份范围，并通过真实入口测试身份和会话隔离。

# 需求追溯

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | ADP 复用正式 AppKey，使用专用身份与独立会话 | docs/adp-chat/04_服务器收发排障.md#ac2 | satisfied | 定向测试核对实际 HTTP 请求的 AppKey、UserId、VisitorId、新旧会话以及正式员工的 ADP 会话。 |
| R2 | 企微成员由命令指定，验签后核对本轮成员和测试码 | docs/adp-chat/04_服务器收发排障.md#ac3 | satisfied | 实际 FastAPI 入口规定单个有效成员，覆盖正确成员、其他成员、错误签名、停用和迟到回发。 |
| R3 | 更新两种免令牌命令及配置示例并保留其他本地改动 | user:2026-11-26-正式渠道诊断#AC3 | satisfied | HTTPS 和 HTTP 命令用本机回环重放，示例重新解析并逐字节核对诊断段外内容。 |

# 计划改动

修改应用启动、管理路由、诊断回发关联、配置解析及共享日志的旧配置引用。同步配置示例和既有排障指南，更新诊断回归测试。

# 验证矩阵

| 验证层 | 是否要求 | 范围 / 证据 |
| --- | --- | --- |
| 行为 / 单元 / 组件 | required | 新增入口契约先失败后通过。定向 13 项验证请求体、身份、旧配置和回发。 |
| 接口 / 契约 | required | FastAPI 请求体、422 非法输入、404 停用和 409 并发行为经验证。 |
| 集成 / 持久化 / 运行依赖 | required | 真实应用、SQLite、业务回调、出站解析，提供方以 HTTP 层替身隔离。 |
| 用户 / 工作流验收 | required | 文档的 HTTPS 和 HTTP 请求经真实本机 Uvicorn 重放，HTTPS 校验证书。 |
| 跨组件关键路径 | required | 真回调解密验签、发送适配器和 ADP SSE 解析经测试。 |
| 外部依赖 / 供应方探测 | not_applicable | 本地实现范围，现场 ADP 和企业微信验证由部署运维按指南执行。 |
| 构建 / 打包 / 运行 | required | 应用真实 Uvicorn 本机启动，经两种传输访问。构建配置无改动。 |
| 文档 / 治理 / 其他 | required | TOML 解析、示例原值、命令、链接、独立审查和 Ready Check。 |

# 风险、兼容性、迁移与回滚

免令牌诊断开启后，可信管理者可向可见成员发送固定测试码。部署前按指南限制公网转发路径和直连端口。旧版企微命令升级时需加入 user_id，旧配置仍能加载。回滚时使用上一版本对应命令和配置。无数据库迁移。

# 文档、依赖、部署与发布影响

排障文档同步正式应用、专用身份、测试码与两种调用方式。应用运行配置和依赖没有额外修改，示例保留用户原有环境值。本地未执行提交、推送、部署及远程 CI。

# 完成审计

- [x] upstream_re_read：核对用户要求和排障指南验收路径。
- [x] change_coverage：正式 AppKey、专用身份与会话、按请求指定企微成员、两套命令及原示例修改均有对应证据。
- [x] reverse_audit：从请求和签名回调检查对应渠道和业务隔离，从示例命令回放实际入口。
- [x] unresolved_cleared：对本地约定的行为已有验证，真实生产渠道列为现场验证。

# 完成证据与状态

Python 3.14 在用户工作区执行诊断定向测试，13 项通过。工作区全量回归首次运行有三项失败，将旧版源码与等价的合成示例设置组合复现同三项问题，原因是预填 AppKey 和会话及日志相对路径。将当前产品修改放入隔离源码副本，使用干净示例默认值执行项目 CI 的 unittest 完整命令，261 项通过，零跳过。指南 5 个 Bash 代码块、7 个文件链接及两种实际本机回环诊断命令通过。已对最终产品 diff 进行独立审查。用户的示例定制值保持原样。临时测试副本已清理。
独立审查 PASS。基准 HEAD 7c3fb1ce1e912b9fbaecbff1326ab4f31d074282，产品 diff SHA256 2b2caea3a128f4fdf6206c9d16b23186e6a23f41da871b25ed8b16fd091052ff，规则源码 dbae4a011f78ddeaf6e7a0411d3fb40df858fbed。
