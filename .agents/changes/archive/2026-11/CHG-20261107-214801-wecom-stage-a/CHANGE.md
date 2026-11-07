---
schema: coding-change/v1
id: CHG-20261107-214801-wecom-stage-a
title: 企业微信阶段 A 多用户身份与真实会话续接
level: L3
status: done
owner: codex
branch: feature/wecom-stage-a
created: 2026-11-07T21:48:01+08:00
updated: 2026-11-07
completion_gate: required
depends_on: []
affected_areas:
  - "core"
  - "adp"
  - "tests"
  - "docs"
affected_paths:
  - "src/core/config.py"
  - "src/core/identity.py"
  - "tests/test_employee_identity.py"
  - "tests/adp_conversation_probe.py"
  - "config.example.toml"
  - "README.md"
  - "docs"
contracts:
  - "wecom-conversation-v1"
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

真实应用已按受控三次请求核验上下文和用户隔离，见下文 V2。固定标识不保证未来永久记忆；未修改平台配置。

# 目标、成功标准与非目标

## 目标

员工身份与会话可独立派生，真实 ADP 上下文可复用。

## 成功标准

- [x] A1 同员工同作用域跨进程会话稳定。
- [x] A2 不同员工、企业、AgentId、逻辑应用隔离；AppKey 轮换不改会话。
- [x] A3 旧脚本、CLI、单用户 API/配置兼容。
- [x] A4 明确固定 ID 不等于永久记忆。
- [x] A5 YNND 两个独立进程“我想改密码”→“windows”真实上下文与 SIYU 隔离有直接证据。
- [x] 当前 head 独立 Review/CI、guarded merge、main-fresh 和收尾完成。

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
| R1 | 跨进程稳定身份 | docs/wecom-integration/03_实施路线图.md#AC1 | satisfied | 8 项身份/Probe 自动回归及 V2 真实证据，原 85 项兼容回归保持通过 |
| R2 | 不同作用域隔离 | docs/wecom-integration/03_实施路线图.md#AC2 | satisfied | 8 项身份/Probe 自动回归及 V2 真实证据，原 85 项兼容回归保持通过 |
| R3 | 兼容旧入口 | docs/wecom-integration/03_实施路线图.md#AC3 | satisfied | 8 项身份/Probe 自动回归及 V2 真实证据，原 85 项兼容回归保持通过 |
| R4 | 平台记忆边界 | docs/wecom-integration/03_实施路线图.md#AC4 | satisfied | 8 项身份/Probe 自动回归及 V2 真实证据，原 85 项兼容回归保持通过 |
| R5 | 真实 ADP 跨进程语义与 SIYU 隔离 | docs/wecom-integration/03_实施路线图.md#AC5 | satisfied | 8 项身份/Probe 自动回归及 V2 真实证据，原 85 项兼容回归保持通过 |

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

- [x] upstream_re_read：重新读取阶段 A 上游要求。
- [x] change_coverage：逐项覆盖 A1～A5。
- [x] reverse_audit：配置→身份→正式调用→输出与失败边界。
- [x] unresolved_cleared：所有 required 结果有直接证据。

# 完成证据与状态

## 新鲜证据

E3：身份测试 Red，1 import error，exit 1；命中缺失 core.identity。Source Mode 本次锁定 Agent_Skills 5f406b3ec94faa006b7d5f99bf17fc845c5a97f3，已 fetch 并核对 origin/main。

V1：Windows Python 3.14.7，`.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s tests -v`，93 passed，0 failed，0 skipped，exit 0；完整输出 runtime/wecom-stage-a-regression.log（本机忽略，不入 Git）。当前实现复用正式 payload/SSE，未改旧 API、CLI、会话和依赖。四份受影响 Markdown 的本地链接解析无断链；git diff --check 无错误；实际与示例 TOML section/key 相同。

V2：2026-11-07 21:54～21:55 +08:00，真实已发布应用，逻辑名 wecom-local-test，正式端点 https://wss.lke.cloud.tencent.com/adp/v2/chat；AppKey 仅从本机忽略配置读取。依次执行：

1. `.venv/Scripts/python.exe -B -X utf8 tests/adp_conversation_probe.py --user-id YNND --message "我想改密码" --execute-real-adp --output runtime/wecom-stage-a-ynnd-1.json`，pid=18344，RequestId=66400928-c1bc-46cc-96f8-028e152df63c。
2. 第一进程 exit 0 后，新的独立进程执行 `.venv/Scripts/python.exe -B -X utf8 tests/adp_conversation_probe.py --user-id YNND --message windows --execute-real-adp --output runtime/wecom-stage-a-ynnd-2.json`，pid=29428，RequestId=643b3095-e680-49dd-ad89-129cf95b7dcb。
3. `.venv/Scripts/python.exe -B -X utf8 tests/adp_conversation_probe.py --user-id SIYU --message windows --execute-real-adp --output runtime/wecom-stage-a-siyu-1.json`，pid=14680，RequestId=1c7df559-0388-44f1-ad21-f6d6e9557292。

三次 exit 0、正式 response.completed、ReplyMethod=18。YNND 的 UserId=VisitorId=YNND，ConversationId=5798bc0a9a481f33883e4f1bfd6eac5147f41af5293157f731e89ba6d83a7a30 两轮相同；SIYU 的 UserId=VisitorId=SIYU，ConversationId=bc250c9cd373c4400290789d657678a6762405dce2721db522e621d7946cd6fc。

语义审计（主 Agent 实读三份实际返回，不用关键词断言）：YNND 首轮询问要改哪个系统或设备的密码；续轮仅输入 windows，回答按设备情况给出 Windows 密码修改和重置步骤、密码规则，直接延续密码需求。SIYU 没有该前文，同样只输入 windows 时反问具体问题，未继承 YNND 的密码任务。由此支持当前应用的本次跨进程上下文与员工隔离，不能证明永久记忆、知识答案完整性或真实企微认证。实际回复含内部知识 URL，原文仅留忽略目录，本记录只保留脱敏摘要和请求标识。未修改 ADP 平台提示词/Agent，未调用会议写入或企业微信发送。

V3：2026-11-07 核对腾讯官方 SSE 文档 https://cloud.tencent.com/document/product/1759/129202（页面更新 2026-09-18）：腾讯云 V2 端点、UserId/VisitorId、ConversationId 32～64 格式和独立 RequestId 与现有生产请求一致。完整回归验证旧入口契约未改。

## 未验证内容与剩余风险

实现、稳定回归和真实 Provider 语义验证已交付；独立 Review、PR CI、guarded merge、main-fresh 与实现分支清理均完成。PyCharm GUI 未操作；阶段 A 不改容器链路，网关容器留 E 验收。

## 交付状态

implementation=complete；delivery=merged；validation=complete；main_fresh=complete；change_archive=本地归档；requirement_closure=not_applicable（正式 Roadmap，无 Issue close 生命周期）；cleanup=complete（实现分支）；end_to_end=complete。

V4：隔离只读 Reviewer /root/stage_a_review，base=2ba18da，head=4f75e00c937a1138376f8a01d41d250a17231d17，decision_epoch=1；独立 Deep Review 结论 NO_FINDINGS_WITHIN_SCOPE，零阻塞 Findings；Reviewer 实读真实 JSON，复验 8 tests / 0 skipped / exit 0；父 Agent 核实实际 diff 和底层证据。Validation Asset Redundancy=clean。

V5：PR #12 https://github.com/dingyuwen77/adp_chat/pull/12，current-head CI run 42379664979 实跑 93 tests / OK；最终权限与保护回读 principal=dingyuwen77、push/admin=true、main unprotected、rulesets=[]、review threads=[]，base/head 未漂移。REST merge 使用 expected_head_sha=4f75e00c937a1138376f8a01d41d250a17231d17，实际 merge=7f82924fa2ee77da99f831a65f29393de0b25f8d。main-fresh run 42380434855 success（93 tests / OK）；本地 main 与 origin/main 一致，merge tree 与 reviewed head 无内容差异。Closure Audit 重新核对 A1～A5：身份稳定/隔离、兼容、边界文档及本次真实续接均有直接证据；正式 Roadmap 无单独关闭操作。实现远程分支由 GitHub 合并后自动删除，ls-remote 回读确认不存在；已确认祖先关系和 worktree 后删除本地 feature/wecom-stage-a。阶段收尾归档与 Roadmap 的发布由独立文档 PR 完成，不影响既有运行证据。

## 备注

仅独立 Review 必要委派，不拆实现；已批准 A～E 正常 PR/合并，F 不在授权范围。
