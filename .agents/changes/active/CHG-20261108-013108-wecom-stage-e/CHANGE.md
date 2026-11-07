---
schema: coding-change/v1
id: "CHG-20261108-013108-wecom-stage-e"
title: "企业微信阶段 E 本地完整集成与容器验收"
level: L3
status: in_progress
owner: "codex"
branch: "feature/wecom-stage-e"
created: 2026-11-08
updated: 2026-11-08
completion_gate: required
depends_on: []
affected_areas: 
  - "gateway"
  - "logging"
affected_paths: 
  - "src/core"
  - "src/application"
  - "src/api"
  - "tests"
  - "compose.yaml"
contracts: 
  - "网关JSONL事件v1及本地启动运行"
data_changes: 
  - "日志仅诊断，消息v3不变"
---

# 变更摘要

补齐正式Roadmap阶段E的JSONL与本地完整验收；复用A～D正式链路、配置和消息v3，不建立第二套计划或状态源。

# 背景、现状与问题

## 背景

用户授权A～E逐阶段研发、独立Review、PR/合并/main-fresh/归档清理；A～D均已完整交付。

## 当前现状

base=a454e164130acb896d934a21c93c79892fe39323，D实现PR18/main147项通过，收尾PR19归档/清理已完成；当前网关仅文本日志，E的完整IDE/容器及真实两轮总验收未执行。

## 问题、根因或约束

现有状态机制已实现，缺少按正式事件契约的跨边界诊断及真实运行环境闭环。日志失败不能改变ACK/状态或重试外部副作用；模拟与真实投递必须隔离。

## 不修改的后果

无法凭仓库文档独立复现完整本地链路，运行、恢复和语义证据不完整。

# 事实与证据

| 证据编号 | 已确认事实 | 来源 / 定位 / 命令 | 支撑的约束或决策 |
| --- | --- | --- | --- |
| E1 | A～D已有身份/签名/快照/异步ADP/独立发送 | PR12～19、当前源码 | 复用既有Worker和Store，不新增状态源 |
| E2 | 正式事件v1要求白名单、北京毫秒时间、关联与轮转 | docs/wecom-integration/02_结构化日志事件.md | 标准logging事件Formatter/Handler |
| E3 | 现有Docker/脚本单worker、外置TOML、持久目录 | Dockerfile/compose.yaml/scripts | 本机隔离配置覆盖，不访问Linux服务器 |

## 推断与待确认

待实测Docker ARM64本地仿真/挂载权限、PyCharm断点和新独立真实ADP上下文；未执行前不声称通过。

# 目标、成功标准与非目标

## 目标

同一正式链路完成加密回调→可靠状态→ADP→模拟发送→可解析脱敏JSONL，并可依文档在Windows/PyCharm/Docker复现和恢复。

## 成功标准

E1～E6及用户明确验收有直接证据；独立深度Review、现有CI、逐阶段PR/merge/main-fresh/归档/清理全满足。

## 范围

统一JSONL/调用点；既有本地测试脚本与Docker运行配置；受控真实ADP/Fake故障/IDE/旧入口与打包验证；现有长期文档及配置同源。

## 非目标

阶段F、真实企微后台/发送/Secret、Linux服务器、生产会议MCP、真实ADP压力、平台提示词变更、Release、生产Migration、CI新增/修改、canonical规则修改。

## 必须保持不变

A身份/跨进程；B认证/先去重/原子BUSY/可靠ACK；C无模型配额/无ADP重试/UNKNOWN释放；D独立通知/模拟证据；旧main/CLI/API与现有依赖版本。

# 约束与意图决策

事件只从业务提交后的明确边界产生，不从文本反推；日志只诊断，不恢复业务。JSONL和旧CLI同标准logging，正文和未知属性永不进入事件。测试环境显式test。Docker linux/arm64默认，Windows本机仿真不等于未来真实目标验收。

# 修改方案与决策依据

## 最小充分方案

先日志行为Red→白名单Formatter/轮转/故障可观察→原Gateway/Worker/API接线→真实HTTP故障/关联→PyCharm/Fake/真实ADP独立跨进程→Docker构建/Compose持久/重启/退出→回归/文档/Review/逐阶段交付。

## 证据到决策

E1支持同一Store/Worker；E2支持metadata白名单与诊断/状态解耦；E3支持既有配置/容器入口和单worker，不新建另一套部署架构。

## 备选方案与取舍

解析文本日志会丢失结构/泄漏正文；直接向Cosmos联网扩大范围并让业务依赖日志；新增DB/队列与既有单实例架构冲突。选择本地标准logging结构化事件。Delegation Value=MUST_SPLIT；Independence Requirement=REQUIRED，仅委派只读Review，单写者实施。

# 需求追溯

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | E1 JSONL可解析/关联/脱敏、BUSY/拒绝/UNKNOWN/释放/发送 | docs/wecom-integration/03_实施路线图.md#AC25 | not_satisfied | 尚未实现验证 |
| R2 | E2 真实HTTP可靠ACK/100员工/Fake30/同用户拒绝 | docs/wecom-integration/03_实施路线图.md#AC26 | not_satisfied | 尚未执行E集成 |
| R3 | E3 PyCharm/Docker恢复、armed不重放/UNKNOWN释放 | docs/wecom-integration/03_实施路线图.md#AC27 | not_satisfied | 尚未执行E运行验证 |
| R4 | E4 原main/CLI/API/配置/会话兼容 | docs/wecom-integration/03_实施路线图.md#AC28 | not_satisfied | 尚未执行E回归 |
| R5 | E5 受控真实ADP跨进程两轮/错误结构及隔离 | docs/wecom-integration/03_实施路线图.md#AC29 | not_satisfied | 尚未执行E真实探测 |
| R6 | E6 仅A～E本地验收，F边界/可复现文档 | docs/wecom-integration/03_实施路线图.md#AC30 | not_satisfied | 文档待同步 |

# 计划改动

core日志与配置、gateway/worker/API调用点、既有本地/容器/真实探测脚本和回归，既有README/架构/日志/Roadmap/运维Owner。

# 验证矩阵

| 验证层 | 是否要求 | 范围 / 证据 |
| --- | --- | --- |
| 行为 / 单元 / 组件 | required | JSONL字段白名单、泄漏、轮转、写失败 |
| 接口 / 契约 | required | 事件v1/旧入口/快照v3兼容 |
| 集成 / 持久化 / 运行依赖 | required | 本机真实HTTP/SSE、文件、强杀/重启及Docker挂载 |
| 用户 / 工作流验收 | required | PyCharm断点/PowerShell客户端/真实ADP密码两轮语义 |
| 跨组件关键路径 | required | 正式入站→状态→ADP→模拟发送→事件 |
| 外部依赖 / 供应方探测 | required | 有界真实ADP与错误结构；最新一手文档 |
| 构建 / 打包 / 运行 | required | wheel、Docker ARM64、本机Compose/退出 |
| 文档 / 治理 / 其他 | required | 配置同源、链接/示例、Ready/Review/Git |

## 验证计划

日志先Red后Green、Fake协议/日志故障/强杀、现有与新增全回归；受控真实ADP跨进程两轮与SIYU隔离，人工实读语义；Docker正式镜像/Compose挂载/重启；PyCharm观察断点；Docs/Ready/独立Review/现有CI/guarded merge/main-fresh。

# 风险、兼容性、迁移与回滚

L3诊断安全与运行验收；JSONL永不包含正文/密钥且失败不驱动重试。消息schema v3不变，事件初版v1。新增配置遵循同TOML并保留实际旧值；无依赖升级。容器/测试资源均限本任务并清理，持久证据保留；回退先停网关、备份状态并恢复前版配置。无生产部署/Migration/Release。

# 文档、依赖、部署与发布影响

同步既有长期Owner，日志从目标变实际，其他未来章节保持准确。无Azure依赖、不增加CI、不使用真实企微Secret。本机Docker仅研发验收，真实Linux/ARM网络及员工接入留F。

# 完成审计

- [ ] upstream_re_read：E1～E6及用户明确要求从正式Owner重读。
- [ ] change_coverage：全部上游完成定义有直接映射。
- [ ] reverse_audit：配置/入站/出站/日志/恢复/运行/旧入口双向核对。
- [ ] unresolved_cleared：全部required实现与验证闭合。

# 完成证据与状态

## 新鲜证据

E-RED：日志专项首次执行exit1/ModuleNotFoundError: core.events；先建立行为失败证据再实现。E-EVENTS：正式GatewayEvents/Formatter/分段Handler已接线，9项日志专项exit0/0.825s；短写后同实例续接尾行的新增Red失败已由失败后新开段修复，再Green。原始runtime/wecom-stage-e-red.log、wecom-stage-e-short-write-red.log、wecom-stage-e-events.log。

E-REGRESSION初轮：Windows/Python3.14.7，unittest discover -s tests -v，155tests/OK27.971s（当时8项新日志专项）；后续新增短写用例已定向Green，最终全回归待执行。

E-HTTP初轮：tests/wecom_dispatch_smoke.py --simulate-notifications --report runtime/wecom-stage-e-http.json exit0，正式独立Uvicorn/HTTP/SSE/Store/Simulator/JSONL。quota0 peak100/no拒绝，quota30 peak30/实际70拒绝，maxACK525.6/876.7ms；ADP及通知强杀恢复旧请求/旧通知各一次。四场景日志909/839/31/32条全部解析，关联101/101/3/3条快照、正文/凭据缺失、无真实accepted。PID12544/1572、10616/19300、26544/13840/25912、15848/13364/18916；全部自有进程/端口/临时数据清理。此后仅日志短写恢复保护增量，正式最终证据待核验。

E-CONFIG：同[logging]新增events_retention_days=14，复用既有log_enabled/level/log_file父目录/log_max_bytes，不另造日志路径来源。实际config仅补该默认字段，所有旧值逐项对比相等，actual/example分组+key一致，无凭据输出。

Source固定5f406b3ec94faa006b7d5f99bf17fc845c5a97f3；阶段E起点为远程最新main a454e164130acb896d934a21c93c79892fe39323，工作区干净。D已完成PR18/19、main147项回归、独立深度/收尾Review、归档和分支清理。

## 未验证内容与剩余风险

E尚未实现/验证。F明确不实施；本机模拟签名与发送不证明真实企微云端认证/员工接收。

## 交付状态

implementation=incomplete；delivery=incomplete；validation=incomplete；main_fresh=incomplete；change_archive=incomplete；requirement_closure=not_applicable（正式Roadmap）；cleanup=incomplete；end_to_end=incomplete。

## 备注

沿用现有Roadmap/Change机制，不新建重复计划/Issue，不改canonical规则。
