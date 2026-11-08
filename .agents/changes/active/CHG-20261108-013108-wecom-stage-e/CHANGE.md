---
schema: coding-change/v1
id: "CHG-20261108-013108-wecom-stage-e"
title: "企业微信阶段 E 本地完整集成与容器验收"
level: L3
status: ready_for_review
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

先日志行为Red→白名单Formatter/轮转/故障可观察→原Gateway/Worker/API接线→真实HTTP故障/关联→CLI/Fake/真实ADP独立跨进程→Docker构建/Compose持久/重启/退出→回归/文档/Review/逐阶段交付。

## 证据到决策

E1支持同一Store/Worker；E2支持metadata白名单与诊断/状态解耦；E3支持既有配置/容器入口和单worker，不新建另一套部署架构。

## 备选方案与取舍

解析文本日志会丢失结构/泄漏正文；直接向Cosmos联网扩大范围并让业务依赖日志；新增DB/队列与既有单实例架构冲突。选择本地标准logging结构化事件。Delegation Value=MUST_SPLIT；Independence Requirement=REQUIRED，仅委派只读Review，单写者实施。

# 需求追溯

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | E1 JSONL可解析/关联/脱敏、BUSY/拒绝/UNKNOWN/释放/发送 | docs/wecom-integration/03_实施路线图.md#AC25 | satisfied | 统一事件v1/9项日志专项、真实HTTP日志关联、磁盘/短写故障、脱敏/轮转/旧尾隔离，164项全回归；详见E-EVENTS/E-FINAL下方证据 |
| R2 | E2 真实HTTP可靠ACK/100员工/Fake30/同用户拒绝 | docs/wecom-integration/03_实施路线图.md#AC26 | satisfied | 正式独立进程50/100员工、无限额/Fake30、实际拒绝集合、BUSY/先去重及强杀，JSONL关联；详见E-HTTP/E-PRESSURE |
| R3 | E3 PyCharm/Docker恢复、armed不重放/UNKNOWN释放 | docs/wecom-integration/03_实施路线图.md#AC27 | satisfied | 用户后续要求无需操作PyCharm，采用同一Python命令行运行/故障注入；Docker ARM64真实SIGKILL、RECEIVED恢复、注入SEND_ARMED/PENDING恢复及UNKNOWN新消息，详见E-DOCKER/E-SCOPE |
| R4 | E4 原main/CLI/API/配置/会话兼容 | docs/wecom-integration/03_实施路线图.md#AC28 | satisfied | 164项回归含旧main/CLI/API/配置；旧镜像独立HTTP→原ChatService/会话→SSE夹具→HTTP200和重建恢复，详见E-LEGACY |
| R5 | E5 受控真实ADP跨进程两轮/错误结构及隔离 | docs/wecom-integration/03_实施路线图.md#AC29 | satisfied | 正式加密回调→真实已发布ADP，两轮YNND网关/客户端跨进程、SIYU隔离；实读语义通过，真实无效AppKey4505004且模拟提示完成，详见E-REAL |
| R6 | E6 仅A～E本地验收，F边界/可复现文档 | docs/wecom-integration/03_实施路线图.md#AC30 | satisfied | 六份当前Owner同步、136本地链接、配置同源、明确CLI/可选IDE/模拟与真实切换/Docker/清理/F检查清单；阶段F未实施 |

# 计划改动

core日志与配置、gateway/worker/API调用点、既有本地/容器/真实探测脚本和回归，既有README/架构/日志/Roadmap/运维Owner。

# 验证矩阵

| 验证层 | 是否要求 | 范围 / 证据 |
| --- | --- | --- |
| 行为 / 单元 / 组件 | required | JSONL字段白名单、泄漏、轮转、写失败 |
| 接口 / 契约 | required | 事件v1/旧入口/快照v3兼容 |
| 集成 / 持久化 / 运行依赖 | required | 本机真实HTTP/SSE、文件、强杀/重启及Docker挂载 |
| 用户 / 工作流验收 | required | CLI真实启动/故障注入、PowerShell客户端/真实ADP密码两轮语义；PyCharm按后续用户要求仅提供配置说明 |
| 跨组件关键路径 | required | 正式入站→状态→ADP→模拟发送→事件 |
| 外部依赖 / 供应方探测 | required | 有界真实ADP与错误结构；最新一手文档 |
| 构建 / 打包 / 运行 | required | wheel、Docker ARM64、本机Compose/退出 |
| 文档 / 治理 / 其他 | required | 配置同源、链接/示例、Ready/Review/Git |

## 验证计划

日志先Red后Green、Fake协议/日志故障/强杀、现有与新增全回归；受控真实ADP跨进程两轮与SIYU隔离，人工实读语义；Docker正式镜像/Compose挂载/重启；PyCharm仅配置说明（后续用户范围）；Docs/Ready/独立Review/现有CI/guarded merge/main-fresh。

# 风险、兼容性、迁移与回滚

L3诊断安全与运行验收；JSONL永不包含正文/密钥且失败不驱动重试。消息schema v3不变，事件初版v1。新增配置遵循同TOML并保留实际旧值；无依赖升级。容器/测试资源均限本任务并清理，持久证据保留；回退先停网关、备份状态并恢复前版配置。无生产部署/Migration/Release。

# 文档、依赖、部署与发布影响

同步既有长期Owner，日志从目标变实际，其他未来章节保持准确。无Azure依赖、不增加CI、不使用真实企微Secret。本机Docker仅研发验收，真实Linux/ARM网络及员工接入留F。

# 完成审计

- [x] upstream_re_read：E1～E6及用户明确要求从正式Owner重读。
- [x] change_coverage：全部上游完成定义有直接映射。
- [x] reverse_audit：配置/入站/出站/日志/恢复/运行/旧入口双向核对。
- [x] unresolved_cleared：全部required实现与验证闭合。

# 完成证据与状态

## 新鲜证据

E-RED：日志专项首次执行exit1/ModuleNotFoundError: core.events；先建立行为失败证据再实现。E-EVENTS：正式GatewayEvents/Formatter/分段Handler已接线，9项日志专项exit0/0.825s；短写后同实例续接尾行的新增Red失败已由失败后新开段修复，再Green。原始runtime/wecom-stage-e-red.log、wecom-stage-e-short-write-red.log、wecom-stage-e-events.log。

E-REGRESSION初轮：Windows/Python3.14.7，unittest discover -s tests -v，155tests/OK27.971s（当时8项新日志专项）；后续新增短写用例已定向Green，最终全回归待执行。

E-HTTP初轮：tests/wecom_dispatch_smoke.py --simulate-notifications --report runtime/wecom-stage-e-http.json exit0，正式独立Uvicorn/HTTP/SSE/Store/Simulator/JSONL。quota0 peak100/no拒绝，quota30 peak30/实际70拒绝，maxACK525.6/876.7ms；ADP及通知强杀恢复旧请求/旧通知各一次。四场景日志909/839/31/32条全部解析，关联101/101/3/3条快照、正文/凭据缺失、无真实accepted。PID12544/1572、10616/19300、26544/13840/25912、15848/13364/18916；全部自有进程/端口/临时数据清理。此后仅日志短写恢复保护增量，正式最终证据待核验。

E-CONFIG：同[logging]新增events_retention_days=14，复用既有log_enabled/level/log_file父目录/log_max_bytes，不另造日志路径来源。实际config仅补该默认字段，所有旧值逐项对比相等，actual/example分组+key一致，无凭据输出。

Source固定5f406b3ec94faa006b7d5f99bf17fc845c5a97f3；阶段E起点为远程最新main a454e164130acb896d934a21c93c79892fe39323，工作区干净。D已完成PR18/19、main147项回归、独立深度/收尾Review、归档和分支清理。

## 未验证内容与剩余风险

E实现/本地验证完成，独立Review/当前head既有CI/合并/main-fresh/归档清理尚未完成。按后续用户范围调整未操作PyCharm GUI，文档保留配置方法；本机ARM64仿真不替代目标环境。UNKNOWN正文/证据保留须按企业策略人工核查，当前不会自动清除未核查未知状态。真实50/100压力未获明确选择，仅Fake已授权并完成，不宣称真实容量。F明确不实施；本机模拟签名与发送不证明真实企微云端认证/员工接收。

## 交付状态

implementation=complete；delivery=incomplete；validation=complete；main_fresh=incomplete；change_archive=incomplete；requirement_closure=not_applicable（正式Roadmap）；cleanup=incomplete；end_to_end=incomplete。

E-SCOPE：用户后续指出“你用pycharm干啥，你用不到这个软件啊”；已停止GUI工作，只进行应用清单读取，窗口读取审批超时，没有发送任何IDE输入或改IDE配置；之后不再操作PyCharm。按用户最新范围以命令行真实启动/测试/故障注入验证相同Python3.14入口，IDE仅给配置说明，不宣称GUI实测。用户再次允许真实ADP，并要求50/100用户压力；Fake压力均已执行，针对可能扩大的真实50/100范围已异步澄清，明确授权到达前不执行真实压力，不影响本任务原有Fake验收。

E-FINAL：Windows/Python3.14.7，`.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s tests -v` exit0，164tests/0fail/0skip/27.077s，原始runtime/wecom-stage-e-regression.log。17项新增：9事件/3配置生成/4网关Probe/1Fake重设屏障。配置生成首次Red为缺失模块，Fake hold首次Red为404，均再Green；Probe扩展属于受控验收资产，未宣称其新增前已建立Red，4项边界测试与真实全链路直接验证。无有效测试删减、依赖升级或CI改动。

E-REAL：本机正式加密回调→FastAPI→v3快照→正式AsyncADPClient/官方HTTPS/SSE→结果保存→正式Simulator→JSONL。独立测试scope wecom-local-e-real-20261108-935579d2，同一个已发布测试应用AppKey仅在忽略TOML本机读取，根配置/平台提示词未改。2026-11-08T09:54+08，YNND首轮客户端pid28264、网关pid31272，RequestId030a73fe-1412-4b88-acdc-f264973ea1cd；首轮完整完成并网关退出后，新网关pid31844/客户端pid32108续轮RequestIdb1663507-c0b6-419a-8e63-99ba09ce9fdf。两轮UserId=VisitorId=YNND、ConversationId8d21a55ae85c478622a0424586ae85a9d42f2d9b397624616afa5427718630e0稳定，独立RequestId。首轮实读为询问哪类系统密码；续轮把windows作为密码类型补充，给出Windows密码修改流程和相关知识来源；主Agent实读完整回复确认语义，不靠固定文本/关键词判定，不评价业务建议正确性或判断业务任务完成。

SIYU客户端pid17492、RequestId95bf11af-bcd4-4c9c-8518-02be530f6ccf，ConversationIda1b106482d7825cffbf4f2481dc2ebb8021d82e512e7c3eb98e5dc6fcdbaa6aa隔离；实读为询问Windows问题的具体方面，未继承YNND密码上下文。三轮正式response.completed/ReplyMethod18，通知均SEND_SIMULATED/source=simulated。另新网关pid20192用无效Fake专属AppKey向官方端点发一次windows，客户端pid8288、RequestId21463f8d-7a99-48fd-b497-fd81c63a73c7，真实SSE Code4505004、TraceId3799685582ab236fba3d34f87146f1d8，ADP_REJECTED及模拟提示完成；负向Probe预期exit1，没有真实限额压力/会议写入。实际共4请求、零自动重试，全部网关正常退出/端口释放。原始回复和配置仅runtime/wecom-stage-e-real，脱敏报告runtime/wecom-stage-e-real-report.json状态passed、semantic_verdict=parent_review_passed；原始内容不提交Git。复现用tests/wecom_local_setup.py和adp_conversation_probe.py --gateway-url，详细命令已入既有运维Owner。

E-DOCKER：`scripts/build_image.ps1 -Version 0.0.9` exit0，linux/arm64，镜像sha256:a504925ad0871014484a2c6da3ff6c06bcb1f95f605d6c498803cc7ad26ff877；本地产物dist/v0.0.9，tar/manifest/SHA256SUMS实体与归档hash82e61b80b00effe106b64042a5d2c1a86f0fd9911f956888909ac127488c6463实读一致。构建revision4d0a14d/dirty=true，之后正式src无变化；5个变更源码在已安装镜像与当前源逐字节hash全部一致。Windows Docker Desktop29.6.2/Compose5.3.1、Linux/amd64引擎本机ARM64仿真，非目标服务器。

`tests/wecom_docker_smoke.py --image adp_channel_api:v0.0.9 --report runtime/wecom-stage-e-docker.json`最终exit0。复用compose.yaml+本机override，同一正式镜像/入口/Worker/Store，UID/GID10001，TOML只读、bind runtime/logs可写；100员工、Fake30峰30/实际70拒绝、BUSY/先去重，最大ACK2768.6ms。正常重启闭合record_version不变；实际ADP SIGKILL/137后旧RequestId仅一次，RECEIVED首次恢复、UNKNOWN释放/新YNND可进入、SIYU隔离。停止后以正式Store注入SEND_ARMED/PENDING通知，重启分别SEND_UNKNOWN/首次SEND_SIMULATED；该注入不冒充通知已进入出口强杀（真实出口强杀由Windows独立HTTP实验覆盖）。876条JSONL解析、106消息关联、正文/凭据无、无真实accepted；最后正常SIGTERM/lifespan完整、无OOM/Error，当前SDK返回143。自有唯一Compose容器/网络及临时目录已全部清理，原始报告/日志保留。

E-DOCKER-RCA：首轮拓扑让网关持有共享网络namespace，强杀重启后存活Fake落在旧loopback，恢复的SIYU请求得到network_error/UNKNOWN，正确未重试。最小真实Docker实验确认初始owner/peer同net:[4026532385]且连接成功；owner重启改net:[4026532453]而peer仍旧namespace、连接111；peer重启重新加入后同namespace/连接0，资源已清理。改为稳定Fake持有namespace，网关加入且启动依赖Fake健康，用Fake专用hold控制未来屏障而不重启Owner。完整第三轮已消除恢复网络症状；Fake本身重建须整体重建测试项目，运维已说明。未放宽ADP或ACK超时/未重投请求。

第二轮已通过并发/恢复，但原测试要求正常退出0与Uvicorn0.48的capture_signals机制不符；官方已安装源码显示正常lifespan后重抛SIGTERM。独立最小ARM64真实容器实验确认Application shutdown complete、exit143/无OOM/Error；测试改为严格143且要求本次最后service.started之后有service.stopped，拒绝SIGKILL137。第三轮完整通过，未修改应用退出语义。原始RCA在runtime/wecom-stage-e-network-namespace.json、wecom-stage-e-sigterm-exit.json及三轮日志。

E-LEGACY：`tests/docker_smoke.py --image adp_channel_api:v0.0.9` exit0：已安装包/数据排除、UID/GID10001、只读配置/可写根数据、两次重建session不变、真实HTTP→原ChatService→SSE夹具→HTTP200；全部测试容器/临时数据清理，无真实ADP请求。旧main/CLI/同步客户端回归亦在164项内，不以无鉴权API暴露公网。

E-PACKAGE/DOCS：正式wheel构建exit0，CRC及5个变更正式模块逐字节通过；无依赖/lock版本变更。六份现有长期Owner同步，本地启动/签名客户端/两员工/BUSY/真Fake切换/50与100压力/JSON快照/JSONL/命令行与可选IDE/Docker/清理及F前置检查完整。136本地链接有效，候选Python函数中文说明AST通过、未提交工作区增量diff whitespace通过；完整base→head检查见后续纠正证据；NUL安全枚举候选文件实读无实际凭据泄漏，actual/example键一致且旧值保留。CI文件差异为空。

E-PRIMARY：2026-11-08联网读取Python3.14 logging.handlers官方完整原文、Docker services官方当前页（2026-11-03）、官方compose-spec/compose-spec默认分支13-merge.md blob1c8cac2448197648c16c7e1ce88e52617dec1543完整原文，确认共享namespace、按target合并卷、ports须!override完整替换且支持版本2.24.4。网页工具连接失败时用本机HTTPS/官方GitHub connector原文，未拿摘要冒充证据。前序已联网核验WeCom及ADP正式协议继续有效。无Azure SDK/Cosmos、正式企微或服务器部署。

E-PRESSURE-50：tests/wecom_dispatch_smoke.py --users 50 --simulate-notifications --report runtime/wecom-stage-e-http-50.json exit0；无限额峰值50、拒绝0，Fake30峰值30、实际拒绝20；ACK P50/P95/P99/max分别96.8/320.7/325.7/325.7ms与86.9/245.4/249.7/249.7ms。批次ACK吞吐138.9/177.0每秒，仅代表回调落盘ACK，不代表ADP模型吞吐。两类实际强杀恢复/未知释放/旧请求和旧通知各一次继续通过。四场景JSONL逐行解析/关联/脱敏通过，全部自有进程/监听/临时数据回收；原始报告/日志在runtime。

E-PRESSURE-100：tests/wecom_dispatch_smoke.py --users 100 --simulate-notifications --report runtime/wecom-stage-e-http-100.json exit0；无限额峰值100、拒绝0，Fake30峰值30、实际拒绝70；ACK P50/P95/P99/max分别403.3/476.3/483.9/488.7ms与341.2/423.1/848.7/853.8ms。批次ACK吞吐182.4/108.8每秒，仅代表回调落盘ACK，不代表ADP模型吞吐。两类实际强杀恢复/未知释放/旧请求和旧通知各一次继续通过。四场景JSONL逐行解析/关联/脱敏通过，全部自有进程/监听/临时数据回收；原始报告/日志在runtime。

Validation Asset Redundancy=clean：新增9日志边界、3配置副作用、4受控Probe边界、1Fake屏障各有独立责任；Docker网关恢复与既有单用户镜像烟测验证不同调用链；50/100复用同一Harness，无第二套业务状态或重复永久框架。

E-GOVERNANCE-CORRECTION：独立Reviewer发现原head bbef4e8795b9a3954fee8c0905380d3138da87d5 的完整base→head diff --check实际exit2，Change frontmatter四个列表键后有行尾空格。此前根侧仅执行工作区增量检查，不能支撑完整diff通过表述；已纠正记录、移除四处空格，并同步验证矩阵中的早期PyCharm断点措辞到用户后续CLI范围。仅Change文档，无源码/测试/产物变化；修后工作区完整base→当前diff检查、Contract与Ready通过，新head仍需定向复核和现有CI。

## 备注

沿用现有Roadmap/Change机制，不新建重复计划/Issue，不改canonical规则。
