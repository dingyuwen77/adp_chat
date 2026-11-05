---
schema: coding-change/v1
id: CHG-20261105-174700-single-turn-session
title: 纯文字单次转发与跨进程会话续接
level: L3
status: done
owner: Codex
branch: feature/text-session-forwarding
created: 2026-11-05T17:47:00+08:00
updated: 2026-11-05T18:25:37+08:00
completion_gate: required
depends_on: []
affected_areas:
  - "CLI"
  - "Library"
  - "Session"
  - "Docs"
  - "CI"
affected_paths:
  - "main.py"
  - "adp_chat"
  - "tests"
  - "README.md"
  - "config.example.toml"
  - ".gitignore"
  - "docs"
  - "pyproject.toml"
  - ".github/workflows/tests.yml"
contracts:
  - "ChatResult"
  - "ChatConfig"
  - "ADP HTTP SSE V2"
data_changes:
  - "runtime/session.json"
---
# 变更摘要

默认入口改为纯文字单次转发与跨进程会话续接；系统移除附件能力。正式上游需求为 #1，用户最新范围优先于原多模态任务书。本记录在首次实现后建立，未将其伪记为实施前门禁已完成。

# 背景、现状与问题

## 背景

用户要求每次启动只转发一条消息，并在后续运行续接；随后明确只做文字、每项配置注释、不安装 wheel、完成后合并 main。

## 当前现状

基线 main 为 9a2ccd3d7c6887551de6fe89ba163723f7ea3567，旧入口固定问题并自动追问，无持久会话。已有用户问题修改“我想改密码”保留在文字兼容示例。

## 问题、根因或约束

进程局部 UUID 无法跨启动恢复；默认示例自动取补答违背消息转发目标。附件链路已不属于当前范围，保留会增加无需求配置和依赖。

## 不修改的后果

每次启动丢失会话，收到追问后持续读输入，用户需维护无关云端附件配置。

# 事实与证据

| 证据编号 | 已确认事实 | 来源 | 决策 |
| --- | --- | --- | --- |
| E1 | 用户明确纯文字、配置注释、免安装 wheel 与合并 main | #1 | 当前范围 |
| E2 | 旧入口自动追问，无会话文件 | 基线 main.py | 单次入口与会话模块 |
| E3 | 无现有 CI、Issue 或 Change carrier | GitHub 与仓库有界调查 | 最小文字 CI 与稳定 Issue |

## 推断与待确认

真实记忆受 ADP 上下文设置影响；不承诺无限历史或同会话并发提交。

# 目标、成功标准与非目标

## 目标

转发一条文字后退出，下次恢复同一会话。

## 成功标准

按 #1 的 AC1–AC8 验收，本记录不建立第二套成功标准。

## 范围

入口、会话、协议、错误与日志、配置注释、附件退役、文字测试与文档、CI 和交付。

## 非目标

无附件输入、数据库、分布式锁、业务状态机；不安装 wheel，不发布或部署，不改云端配置。

## 必须保持不变

保留文字高级接口、旧文字 CLI、VisitorId fallback、独立 RequestId、SSE 成功终态、日志安全和错误退出。

# 约束与意图决策

当前 decision_epoch=4：用户授权纯文字系统收口、每项配置注释、不安装 wheel 和最终合并 main。只移除已退役输入能力；服务端正式非文本回复仍保留，不将 thought/工具内容当答案。旧 [attachments] 分组兼容忽略；本机失效分组已删除，有效值经解析比对保持。

# 修改方案与决策依据

## 最小充分方案

main 读取本轮文字，关闭业务解析，先 load_session，再 chat 一次，最后 show_result 并退出；session 用同目录临时文件、fsync 和原子替换。错误不自动重试。

## 证据到决策

持久会话修复跨进程 ID 丢失；入口调用次数固定修复自动追问；依赖与配置同步移除避免保留上传残骸。保存与清理双故障先复现再修复，保留 ChatError。

## 备选方案与取舍

手工每次传 ID 无法满足自动恢复；数据库或锁增加本地顺序阶段的复杂度。采用本地最小 JSON；不保留双上传路线或伪装附件文本。

# 需求追溯

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | 文字一次请求，追问退出，空输入不发 | #1 / AC1 | satisfied | test_single_turn 与真实 clarification 单请求退出 0 |
| R2 | 先落盘、真实重启恢复、请求独立 | #1 / AC2 | satisfied | 真实磁盘与双进程回归；first/recall 服务端ID一致 |
| R3 | 身份/损坏/保存/清理失败和不重试 | #1 / AC3 | satisfied | 原子替换及双故障 Red/Green，超时断流保留状态回归 |
| R4 | 服务端一致性及正式回复保留 | #1 / AC4 | satisfied | 两种事件差异测试、reply_contents及thought/tool过滤 |
| R5 | 系统移除附件，保留文字兼容 | #1 / AC5 | satisfied | 删除上传模块、参数、依赖、样例；原文字断言保留 |
| R6 | 配置逐项注释与安全迁移 | #1 / AC6 | satisfied | 模板与本机配置逐项注释，解析比对有效值，废弃凭据不入对象 |
| R7 | 回归与真实文字验收，不安装wheel | #1 / AC7 | satisfied | 79 tests退出0；first/recall/clarification真实进程记录；最新范围后未安装wheel |
| R8 | Review、CI、merge和后置收尾 | #1 / AC8 | not_applicable | 本施工实现阶段只到Ready，后置状态仍由Issue #1持有；必须在合并/main-fresh/归档/cleanup后才可关闭Issue，不以本行声明端到端完成 |

# 计划改动

| 文件 / 模块 / 资产 | 修改 | 对应要求 |
| --- | --- | --- |
| main.py、session.py | 单次入口、持久会话 | R1–R3 |
| client.py、models.py、console.py | 协议一致、回复内容 | R4 |
| config.py、logging.py、cli.py、pyproject.toml | 文字范围、废弃字段/依赖清除 | R5–R6 |
| tests、README、docs、配置模板 | 消费者回归与说明 | R5–R7 |
| .github/workflows/tests.yml | Windows Python 3.14 确定性回归 | R8 |

# 验证矩阵

| Layer | Required | Scope / Evidence |
| --- | --- | --- |
| 行为 / Unit / Component | required | 输入/回复/协议/错误的文字回归 |
| 接口 / Contract | required | 文字消费者、退役参数、旧配置兼容 |
| 集成 / Persistence / Runtime Dependency | required | 真实目录、原子失败、进程重启、不同cwd |
| 用户 / Workflow Acceptance | required | 真实主入口一次输入/输出/退出 |
| 跨组件 Golden Path | required | main→配置/会话→ADP→正式回复 |
| 外部依赖 Probe | required | 独立进程first/recall/clarification |
| Build / Package / Runtime | not_applicable | 用户明确不需要安装wheel；标准库脚本的入口与CI证明当前运行范围 |
| 文档 / 治理 / 其他 | required | 配置注释、链接、Issue/Change/PR门禁、独立Review |

## 验证计划

标准 unittest、真实文字探针、配置/链接/源码检查、独立只读 Review、当前 PR/main CI。

# 风险、兼容性、迁移与回滚

附件 public 参数和配置已按用户授权退役；文字 keyword 参数及高级功能保持。新状态 schema_version=1，不迁移生产数据。顺序运行，不保证并发；上下文保存取决于应用。回滚代码保留本地会话文件；无部署或 Release。

# 文档、依赖、部署与发布影响

README、配置模板、可选业务协议同步为纯文字。上传实现、示例、requirements和可选SDK依赖移除；未升级 Runtime。日志只脱敏当前AppKey并继续处理URL令牌。无发布、部署或云端修改。

# 完成审计

- [x] upstream_re_read：已live重读Issue #1，纯文字/配置注释/免安装wheel/合并main范围无漂移。
- [x] change_coverage：AC1–AC7证据充分，AC8的后置合并与收尾仍由Issue #1保持未勾选，不以Ready替代交付完成。
- [x] reverse_audit：已审入口、配置、Library、状态文件、依赖和CI责任；79 tests及真实文字证据覆盖当前风险。
- [x] unresolved_cleared：独立Reviewer对22937b2无剩余BLOCKING；CI 42009487928成功。仅施工Ready，Issue仍待合并后验收。

# 完成证据与状态

## 新鲜证据

Windows Python 3.14.7，当前任务分支工作区：79 tests，退出0；新增文字范围测试曾出现2个预期失败后修复。保存/清理双故障回归先失败后通过。18:07:52–18:08:10 三个独立真实进程同一服务端会话 b2e9c018-7a33-41ff-8276-3ee23b116fe0，RequestId各不相同，每进程一次请求，编号两次准确，追问正常退出0。

## 未验证内容与剩余风险

长时历史受云端上下文影响；本次不承诺无限历史，不安装wheel、不验收真实Widget业务动作。

## 交付状态

当前任务分支 feature/text-session-forwarding，Issue #1 open；独立Review结论NO_FINDINGS_WITHIN_SCOPE，reviewed head=22937b2f94c82a41d96743a7b1e4a76997b897ec；PR #2 CI 42009487928成功，79 tests。此后仅证据元数据更新；main-fresh和后置收尾仍由平台事实确认。实现PR #2已由REST绑定head合并至3814784ec5551e4a8628d4c23784f612514cb850，main CI42010326002成功（79 tests，1.639s）。项目无原生自动归档，本归档通过独立受审PR进入main，不直接写默认分支；Issue #1仍等待Closure及cleanup。

## 备注

旧多模态实验仅是收窄范围前的诊断背景，不作为本次文字验收；相关临时上传工具在任务清理中删除。Validation Asset Redundancy：文字与高级行为各有独立边界，已移除附件重复分支；CI只保留一个无依赖安装的Windows回归job。
