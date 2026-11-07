---
schema: coding-change/v1
id: "CHG-20261107-231855-wecom-stage-c"
title: "企业微信阶段 C 异步 ADP 与状态恢复"
level: L3
status: in_progress
owner: "codex"
branch: "feature/wecom-stage-c"
created: 2026-11-07
updated: 2026-11-07
completion_gate: required
depends_on: []
affected_areas:
  - "adp"
  - "storage"
  - "application"
affected_paths:
  - "src/adp"
  - "src/storage/messages.py"
  - "src/application"
contracts:
  - "网关异步 SSE 和消息状态机"
data_changes:
  - "兼容升级消息快照与 UNKNOWN 恢复"
---

# 变更摘要

实现正式 Roadmap C：异步 ADP、同源可靠状态转换、UNKNOWN 保存和释放、重启不重投；沿用 B 快照唯一 Owner。

# 背景、现状与问题



## 背景

A/B 已分别经 PR12/13、14/15 完整交付，用户授权按 A→E 持续研发。

## 当前现状

base=780b440a424967c2f12645f5c2394246882faa3f。B 仅接收 RECEIVED/BUSY，原 ADP 为同步阻塞 SSE；httpx0.28.1 当前是开发依赖。

## 问题、根因或约束

可靠 ACK 后没有异步首次执行；没有 armed 持久边界会使崩溃后误重投外部副作用，单用户旧锁不能服务多员工。

## 不修改的后果

不能完成可信入站到 ADP 结果的多用户闭环和恢复。

# 事实与证据

| 证据编号 | 已确认事实 | 来源 / 定位 / 命令 | 支撑的约束或决策 |
| --- | --- | --- | --- |
| E1 | B main-fresh111tests通过 | PR14/main106dfb3，归档PR15 | 复用正式crypto/store |
| E2 | 当前SSE协议含request_ack/error/completed和ReplyMethod7 | 腾讯官方1759/129202（2026-09-18），本机原文 | 分类不靠回复文本 |
| E3 | HTTPX提供AsyncClient/stream且max_connections=None无连接业务排队 | python-httpx.org/async、advanced/resource-limits | 使用既有0.28.1，不升级 |

## 推断与待确认

模型层限额可能发生在业务部分执行后；没有执行前证据时保守 UNKNOWN，不能由缺少 ACK 推断未执行。真实异步客户端与错误协议将受控验证；不压测实际 ADP。

# 目标、成功标准与非目标



## 目标

多员工尽可能立即异步提交；同员工仍一条；可靠完成结果/提示/释放，旧RequestId永不自动重投。

## 成功标准

C1～C8有直接证据；受控真实ADP与确定性Fake故障/并发/实际进程退出恢复，独立Review和逐阶段交付。

## 范围

ADP async/SSE共用协议、MessageStore同Owner版本升级与转换、后台调度/lifespan、安全配置和现有文档/测试。

## 非目标

D发送、E JSONL/Docker总验收、F正式接入；真实ADP压力和会议写入、CI新增、生产部署。

## 必须保持不变

A身份算法；B验签/可靠ACK/先去重和BUSY语义；旧main.py/CLI/API单用户兼容；ADP零重试。

# 约束与意图决策

单实例独占目录；armed先于网络，终态/通知/释放同一JSON提交；物理资源保护不按模型配额排队。版本升级先校验全部历史记录，损坏失败关闭。

# 修改方案与决策依据



## 最小充分方案

先写确定性失败测试→现有SSE纯解析抽取共用/真正异步HTTP→原store扩展转换/恢复→正式lifespan worker→Fake并发/故障/跨进程与受控真实联调→文档/独立Review/交付。

## 证据到决策

E1支持沿同一存储Owner；E2要求实际Code和阶段证据、UNKNOWN而非业务失败；E3支持0.28.1转正式依赖并显式零重试/证书校验。

## 备选方案与取舍

线程包装旧chat会保留阻塞连接且不能证明真异步；数据库/Redis超出首期；全局信号量会改变模型业务配额。采用轻量asyncio任务注册表+现有物理容量保护。Delegation Value=MUST_SPLIT，Independence Requirement=REQUIRED，只委派隔离只读Review，实现单写者。

# 需求追溯

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | C1 100独立用户同时转发 | docs/wecom-integration/03_实施路线图.md#AC12 | not_satisfied | 尚未实现验证 |
| R2 | C2 Fake30名额入口拒绝 | docs/wecom-integration/03_实施路线图.md#AC13 | not_satisfied | 尚未实现验证 |
| R3 | C3 同用户竞态只提交一条 | docs/wecom-integration/03_实施路线图.md#AC14 | not_satisfied | 尚未实现验证 |
| R4 | C4 真实Code与阶段准确分类 | docs/wecom-integration/03_实施路线图.md#AC15 | not_satisfied | 尚未实现验证 |
| R5 | C5 UNKNOWN可靠保存/释放/不重投 | docs/wecom-integration/03_实施路线图.md#AC16 | not_satisfied | 尚未实现验证 |
| R6 | C6 armed进程崩溃恢复 | docs/wecom-integration/03_实施路线图.md#AC17 | not_satisfied | 尚未实现验证 |
| R7 | C7 身份与结果隔离 | docs/wecom-integration/03_实施路线图.md#AC18 | not_satisfied | 尚未实现验证 |
| R8 | C8 旧CLI/API兼容 | docs/wecom-integration/03_实施路线图.md#AC19 | not_satisfied | 尚未实现验证 |

# 计划改动

src/adp、src/storage/messages.py、src/application/wecom_gateway.py/worker、src/api/app.py及回调接线；config/example/requirements/lock；确定性Fake与崩溃测试；现有文档Owner。

# 验证矩阵

| 验证层 | 是否要求 | 范围 / 证据 |
| --- | --- | --- |
| 行为 / 单元 / 组件 | required | SSE、真实Code/阶段、状态转换与安全 |
| 接口 / 契约 | required | 旧CLI/API、新回调和schema升级 |
| 集成 / 持久化 / 运行依赖 | required | 真实文件/故障/进程崩溃和HTTPS/SSE |
| 用户 / 工作流验收 | required | 100用户/30限额/同员工BUSY/UNKNOWN新轮次 |
| 跨组件关键路径 | required | 正式回调→快照→worker→SSE→终态/通知/释放 |
| 外部依赖 / 供应方探测 | required | 受控已发布测试ADP，真实协议 |
| 构建 / 打包 / 运行 | required | httpx正式安装和wheel源码 |
| 文档 / 治理 / 其他 | required | 配置同源、链接、Ready/Review/当前head及main-fresh |

## 验证计划

targeted先Red/Green；完整unittest；Fake100无限额及30限额、故障注入/跨进程退出；有界真实请求；当前head Ready/Review/CI，guarded merge后main-fresh。

# 风险、兼容性、迁移与回滚

L3外部副作用/版本恢复：armed前保存失败不发包，armed后未知不自动重试。兼容升级B v1，未知版本/损坏保留失败关闭；回退先停止网关并保留所有快照，不能用旧版继续处理新状态。不执行生产Migration。

# 文档、依赖、部署与发布影响

现有httpx0.28.1从开发转正式依赖，不升级任何版本；同源config/example新增网关输送配置，实际值保留。更新架构/Roadmap/日志边界/现有运维。无CI新增、Release或生产部署。

# 完成审计

- [ ] upstream_re_read：交付前重读C1～C8。
- [ ] change_coverage：完整映射上游。
- [ ] reverse_audit：回调→arm→网络→结果/通知/释放/恢复。
- [ ] unresolved_cleared：所有required证据完成。

# 完成证据与状态



## 新鲜证据

C-RED：`.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s tests -p test_wecom_dispatch.py -v`，exit1，ModuleNotFoundError: adp.async_client。生产实现尚未编写。Source固定5f406b3ec94faa006b7d5f99bf17fc845c5a97f3，不更改canonical。

## 未验证内容与剩余风险

C尚未实现，D～F仍未开始，外部exactly-once无法由本地网关保证。

## 交付状态

implementation=incomplete；delivery=incomplete；validation=incomplete；main_fresh=incomplete；change_archive=incomplete；requirement_closure=not_applicable（正式Roadmap）；cleanup=incomplete；end_to_end=incomplete。

## 备注

不新建重复计划或Issue，不修改CI或Agent_Skills。

