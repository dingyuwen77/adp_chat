---
schema: coding-change/v1
id: CHG-20261107-214801-wecom-stage-a
title: 企业微信阶段 A 多用户身份与真实会话续接
level: L3
status: in_progress
owner: codex
branch: feature/wecom-stage-a
created: 2026-11-07T21:48:01+08:00
updated: 2026-11-07T21:48:01+08:00
completion_gate: required
depends_on: []
affected_areas: [core, adp, tests, docs]
affected_paths: [src/core/config.py, src/core/identity.py, tests/test_employee_identity.py, tests/adp_conversation_probe.py, config.example.toml, README.md, docs]
contracts: [wecom-conversation-v1]
data_changes: []
---

# 变更摘要

实现路线图阶段 A：每员工独立 UserId/VisitorId 与稳定 ConversationId，复用现有 ADP 单轮入口并通过真实跨进程探测确认上下文。A～E 是本次顶层目标，按现有路线图逐阶段 PR 交付；B～E 必须等待 A 合并收尾。F 不实施。

# 背景、现状与问题

## 背景

来源为 docs/wecom-integration/03_实施路线图.md 阶段 A 与用户本轮 A～E 授权。

## 当前现状

基线 origin/main=2ba18da14ed35602c074b2cf10f3148e9a8b1026，PR #11 已合并。现有配置固定身份，storage.session 仅单用户；adp.client.chat 已支持传入会话、逐轮 RequestId。没有 Active Change。

## 问题、根因或约束

不能把单用户 JSON 会话用于多员工；短 UserID 不满足 ADP ConversationId 的 32～64 字符限制。固定 ConversationId 仍需平台实际语义验收。

## 不修改的后果

网关后续不能保证员工隔离和跨进程稳定会话。

# 事实与证据

| 证据编号 | 已确认事实 | 来源 / 定位 / 命令 | 支撑的约束或决策 |
| --- | --- | --- | --- |
| E1 | 单轮请求可复用现有 SSE，不自动追问 | src/adp/client.py、src/adp/sse.py | 不建第二套协议 |
| E2 | Python 3.14.7；FastAPI 0.128.2；HTTPX 0.28.1 | .venv 实际版本 | 沿用工具链 |
| E3 | 身份模块不存在，新增身份测试实际失败 | unittest discover -s tests -p test_employee_identity.py -v，exit 1 | Red 已确认 |
| E4 | main 无保护；Rulesets=[]；principal=dingyuwen77 | gh api branches/main、rulesets、user | 仍执行本次规定的门禁 |

## 推断与待确认

真实应用是否保留密码修改上下文尚未验证，属于 A 交付阻塞项；不修改平台配置制造通过。

# 目标、成功标准与非目标

## 目标

员工身份与会话可独立派生，真实 ADP 上下文可复用。

## 成功标准

- [ ] A1 同员工同作用域跨进程会话稳定。
- [ ] A2 不同员工、企业、AgentId、逻辑应用隔离；AppKey 轮换不改会话。
- [ ] A3 旧脚本、CLI、单用户 API/配置兼容。
- [ ] A4 明确固定 ID 不等于永久记忆。
- [ ] A5 YNND 两个独立进程“我想改密码”→“windows”真实上下文与 SIYU 隔离有直接证据。
- [ ] 当前 head 独立 Review/CI、guarded merge、main-fresh 和收尾完成。

## 范围

core 身份函数、同一 TOML 的可选 wecom 身份作用域、受控 Probe、回归及现有架构/路线图/运维说明。

## 非目标

B～F 实现、真实企微发送、服务器部署、会议 MCP 写入、生产压测、Release、Agent_Skills 修改。

## 必须保持不变

chat()、main.py、adp-chat、/api/v1/chat、单用户 session.json 语义和 CI；无 ADP 自动重试。

# 约束与意图决策

| 决策维度 | 当前决定 | 依据 | 影响 |
| --- | --- | --- | --- |
| 范围与负责人边界 | 主 Agent 实现；隔离 Reviewer 只读 | 用户 required 独立 Review | Delegation Value=MUST_SPLIT；Independence Requirement=REQUIRED |
| 接口与契约 | SHA256 编码版本化 JSON 元组 | 架构第 4 节 | 64 小写 hex；UserId=VisitorId=员工 |
| 数据与迁移 | 不建立身份映射文件 | A1/A2 | 旧会话文件无需迁移 |
| 错误与失败语义 | 不完整作用域或非法身份在出站前拒绝 | 可信身份边界 | 不静默退回旧身份 |
| 兼容性 | 新配置可省略，旧入口不采用员工身份 | A3 | 同一配置体系 |
| 部署与回滚 | 无正式部署；旧版回退先移除新增 wecom 分组 | 旧配置严格校验 | 不操作旧会话 |

# 修改方案与决策依据

## 最小充分方案

先测试身份派生、跨进程和正式 payload，再新增 core 身份函数和可选配置；Probe 只调用现有 chat()，关闭正文日志；最后真实两轮、完整兼容回归、文档和交付。

## 证据到决策

| 决策 | 依据证据 | 为什么采用这个方案 |
| --- | --- | --- |
| D1 | E1 | 复用协议与错误/日志 Owner，避免双实现 |
| D2 | E2 | 不升级 Runtime 或依赖 |

## 备选方案与取舍

UserID 原样会话不满足长度；持久映射文件增加写入一致性和恢复负担；采用已批准的确定性 SHA256，使用 JSON 元组消除字段拼接歧义。公开算法上线后不可静默改变。

# 需求追溯

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | 跨进程稳定身份 | docs/wecom-integration/03_实施路线图.md 阶段 A / A1 | not_satisfied | 开发中 |
| R2 | 不同作用域隔离 | docs/wecom-integration/03_实施路线图.md 阶段 A / A2 | not_satisfied | 开发中 |
| R3 | 兼容旧入口 | docs/wecom-integration/03_实施路线图.md 阶段 A / A3 | not_satisfied | 开发中 |
| R4 | 平台记忆边界 | docs/wecom-integration/03_实施路线图.md 阶段 A / A4 | not_satisfied | 开发中 |
| R5 | 真实 ADP 跨进程语义与 SIYU 隔离 | docs/wecom-integration/03_实施路线图.md 阶段 A / A5；user:A～E真实续接验收 | not_satisfied | 开发中 |

# 计划改动

| 文件 / 模块 / 资产 | 计划修改 | 原因 | 对应要求 / 证据 |
| --- | --- | --- | --- |
| src/core/identity.py、src/core/config.py | 身份与配置 | 派生作用域 | R1/R2 |
| tests/test_employee_identity.py、tests/adp_conversation_probe.py | 稳定回归与独立真实单轮进程 | 不以 Mock 代替 Provider | R1/R2/R3/R5 |
| config.example.toml、README.md、docs | 当前能力和复现说明 | 可自行使用 | R3/R4/R5 |

# 验证矩阵

| 验证层 | 是否要求 | 范围 / 证据 |
| --- | --- | --- |
| 行为 / 单元 / 组件 | required | 派生向量、全部身份维度、非法输入 |
| 接口 / 契约 | required | 现有正式 V2 payload 身份、独立 RequestId、原入口回归 |
| 集成 / 持久化 / 运行依赖 | required | 两个独立解释器、TOML 解析；无新增持久状态 |
| 用户 / 工作流验收 | required | Probe 正式命令及真实两轮回复语义 |
| 跨组件关键路径 | required | config→identity→chat→真实 ADP→脱敏证据 |
| 外部依赖 / 供应方探测 | required | 有界 YNND 两轮与 SIYU；不触发会议写入 |
| 构建 / 打包 / 运行 | required | 既有 installed namespace 的新模块可导入；完整回归 |
| 文档 / 治理 / 其他 | required | 文档链接、配置对齐、Change/PR 合法性、独立 Review、CI |

## 验证计划

.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s tests -v；身份 targeted；独立 Probe 进程；canonical ready_check 和 governance_contract；当前 PR head CI 与 merge 后 main CI。A 不改 Docker，镜像验收在 E。

# 风险、兼容性、迁移与回滚

算法和逻辑应用名改变会丢上下文；固定版本向量保护。生产 AppKey 仅从忽略配置读取，不打印。旧配置可省略 wecom，旧 session.json 原样。无依赖升级、数据迁移、生产部署；回滚移除新配置组并退回旧代码。

# 文档、依赖、部署与发布影响

targeted 更新现有架构、路线图和运维 Owner；无新文档主题。配置实际值保留，只补必要新字段，示例安全。无新增依赖、Release、生产部署。

# 完成审计

- [ ] upstream_re_read：重新读取阶段 A 上游要求。
- [ ] change_coverage：逐项覆盖 A1～A5。
- [ ] reverse_audit：配置→身份→正式调用→输出与失败边界。
- [ ] unresolved_cleared：所有 required 结果有直接证据。

# 完成证据与状态

## 新鲜证据

E3：身份测试 Red，1 import error，exit 1；命中缺失 core.identity。Source Mode 本次锁定 Agent_Skills 5f406b3ec94faa006b7d5f99bf17fc845c5a97f3，已 fetch 并核对 origin/main。

## 未验证内容与剩余风险

Green、真实 ADP、独立 Review、PR CI、merge/main-fresh 尚未执行；A 不可声明完成。

## 交付状态

implementation=incomplete；delivery=incomplete；validation=incomplete；main_fresh=incomplete；change_archive=incomplete；requirement_closure=not_applicable（正式 Roadmap，不重复建 Issue）；cleanup=incomplete；end_to_end=incomplete。

## 备注

仅独立 Review 必要委派，不拆实现；已批准 A～E 正常 PR/合并，F 不在授权范围。
