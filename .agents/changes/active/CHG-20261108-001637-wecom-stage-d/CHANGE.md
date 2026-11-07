---
schema: coding-change/v1
id: "CHG-20261108-001637-wecom-stage-d"
title: "企业微信阶段 D 统一通知发送与恢复"
level: L3
status: ready_for_review
owner: "codex"
branch: "feature/wecom-stage-d"
created: 2026-11-08
updated: 2026-11-08
completion_gate: required
depends_on: []
affected_areas:
  - "wecom"
  - "storage"
  - "application"
affected_paths:
  - "src/wecom"
  - "src/storage/messages.py"
  - "src/application"
contracts:
  - "通知独立状态与企业微信发送协议"
data_changes:
  - "发送结果和测试环境元数据，兼容旧快照"
---

# 变更摘要

实现正式Roadmap D统一通知：正常/BUSY/限额/UNKNOWN同一发送路径，发送状态独立，不重问ADP；模拟成功严格区分真实API接受。

# 背景、现状与问题



## 背景

A/B/C已逐阶段交付，C通过PR16/17合并main并完成129项main-fresh；用户授权D/E继续，F排除。

## 当前现状

base=a16748b0ba7808ed300893a8dda25b72df26f212。C保存PENDING提示并释放用户，无发送适配器；MessageStore v2已分离ADP/send维度。

## 问题、根因或约束

通知没有执行路径；若把通知与会话锁耦合或重启盲重发，会持续锁人或重复发送/ADP副作用。

## 不修改的后果

ADP结果无法通过本地模拟完成完整通知链路，E无法验收。

# 事实与证据

| 证据编号 | 已确认事实 | 来源 / 定位 / 命令 | 支撑的约束或决策 |
| --- | --- | --- | --- |
| E1 | C释放和PENDING已持久化 | PR16/17与现有源码 | 沿同Store/Worker扩展独立send任务 |
| E2 | 发送POST/单用户/errcode/无效用户/文字2048字节 | 最新企业微信官方path90236，本机HTTPS正文 | 按UTF8切片，不把HTTP200等于接受 |
| E3 | token需按应用缓存/expiry并处理提前失效 | 官方path91039、90313，已联网取原文 | 仅40014/42001明确未接受可一次刷新重发 |

## 推断与待确认

FakeHTTP可以验证协议解释，不能证明真实平台接受。正式Secret/IP/收发权限留F；本次绝不调用真实企微接口。

# 目标、成功标准与非目标



## 目标

各用户的正常/BUSY/限额/UNKNOWN通知不串发；失败/未知不再调用ADP或阻塞用户；崩溃按发送武装边界恢复。

## 成功标准

D1～D5和Token/长文本/无效用户/未知网络/保存故障边界有直接测试；独立Review、每阶段PR/main-fresh/归档清理。

## 范围

正式WeComSender和Simulator、现有Worker独立发送任务、同Owner快照升级与send_result/环境、防泄漏日志、现有配置/文档/测试。

## 非目标

E JSONL/PyCharm/Docker总验收、F真实回调/员工发送/服务器部署；真实Secret保存、ADP平台变更、实际会议写入；CI新增。

## 必须保持不变

A身份；B签名/去重/BUSY/可靠ACK；C零ADP重试/独立并发/armed先保存/UNKNOWN释放；旧CLI/API和当前依赖版本。

# 约束与意图决策

扩展原Worker同一调度事件和Store，发送任务注册表独立于ADP；PENDING→SEND_ARMED先可靠提交，结果未知不重发。默认发送disabled；测试环境显式simulated，FakeHTTP成功只能SEND_SIMULATED；正式发送需未来production模式/Secret。

# 修改方案与决策依据



## 最小充分方案

先失败测试→发送协议与UTF8切片/缓存→同快照send状态/恢复→原Worker/lifespan接线→Fake故障/崩溃与旧回归→文档/独立Review/逐阶段交付。

## 证据到决策

E1支持复用同Worker+Store，不建新队列/状态源；E2/E3决定正式协议边界、Token有限刷新与未知不盲重发。

## 备选方案与取舍

同步发送占用callback或ADP锁会破坏ACK/用户释放；新通知数据库/独立架构超出首期；盲重试可能重复通知。采用同Worker的独立send任务和持久快照。Delegation Value=MUST_SPLIT；Independence Requirement=REQUIRED，只委派只读Review，单写者实现。

# 需求追溯

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- |
| R1 | D1 正常/BUSY/限额/UNKNOWN通知隔离 | docs/wecom-integration/03_实施路线图.md#AC20 | satisfied | 正式统一Worker/发送器、17项协议与发送测试、回调成员拒绝回归、四场景独立HTTP/强杀恢复及完整147项回归，见下方证据 |
| R2 | D2 发送失败未知不重调ADP/不锁用户 | docs/wecom-integration/03_实施路线图.md#AC21 | satisfied | 正式统一Worker/发送器、17项协议与发送测试、回调成员拒绝回归、四场景独立HTTP/强杀恢复及完整147项回归，见下方证据 |
| R3 | D3 业务错误/无效用户/Token过期分类 | docs/wecom-integration/03_实施路线图.md#AC22 | satisfied | 正式统一Worker/发送器、17项协议与发送测试、回调成员拒绝回归、四场景独立HTTP/强杀恢复及完整147项回归，见下方证据 |
| R4 | D4 通知armed崩溃不双发、未开始首次恢复 | docs/wecom-integration/03_实施路线图.md#AC23 | satisfied | 正式统一Worker/发送器、17项协议与发送测试、回调成员拒绝回归、四场景独立HTTP/强杀恢复及完整147项回归，见下方证据 |
| R5 | D5 模拟成功严格不同真实接受 | docs/wecom-integration/03_实施路线图.md#AC24 | satisfied | 正式统一Worker/发送器、17项协议与发送测试、回调成员拒绝回归、四场景独立HTTP/强杀恢复及完整147项回归，见下方证据 |

# 计划改动

wecom sender、MessageStore schema/转换、GatewayWorker独立通知分支、API lifespan资源、config/example/log脱敏；FakeHTTP/恢复/集成测试；现有长期文档Owner。

# 验证矩阵

| 验证层 | 是否要求 | 范围 / 证据 |
| --- | --- | --- |
| 行为 / 单元 / 组件 | required | UTF8/Token/真实errcode和无效用户解释 |
| 接口 / 契约 | required | payload/模拟证据/旧snapshot兼容 |
| 集成 / 持久化 / 运行依赖 | required | 真实文件/武装与崩溃/发布失败/恢复 |
| 用户 / 工作流验收 | required | ADP/忙碌/未知→统一通知且用户可继续 |
| 跨组件关键路径 | required | 同worker/store接线及FakeHTTP |
| 外部依赖 / 供应方探测 | not_applicable | D禁止真实企微调用；只联网读最新官方协议，真实接入留F |
| 构建 / 打包 / 运行 | required | 正式wheel/安装/现有全回归 |
| 文档 / 治理 / 其他 | required | 模式切换/配置同源/链接/Ready/Review/Git |

## 验证计划

定向Red/Green；正常、BUSY、真实Code与UNKNOWN提示同发送器；FakeHTTP Token过期/失效/无效用户/错误/网络未知；武装前后崩溃；完整旧回归、打包、Ready/独立Review/现有CI，合并后main-fresh。

# 风险、兼容性、迁移与回滚

L3外部副作用和schema：先武装，未知不重发；发送不占用ADP。兼容v1/v2到新快照，全部先校验，旧未知出站不能冒充真实发送证据；环境元数据防测试目录被production误用。回退先停网关、备份状态、恢复兼容配置；不让旧代码处理新状态。无生产Migration。

# 文档、依赖、部署与发布影响

现有配置同源增加发送模式/环境/空Secret与超时，旧值保留；无依赖升级。同步正式架构/Roadmap/日志边界/运维和README；真实Secret不落本任务代码/测试/提示词/Git。无Release/部署/新增CI。

# 完成审计

- [x] upstream_re_read：从当前正式D1～D5、架构和最新官方协议独立重读。
- [x] change_coverage：逐项映射D1～D5及全局不变量。
- [x] reverse_audit：已执行提示→arm→Token/发送→结果/恢复→用户状态及旧入口反向审计。
- [x] unresolved_cleared：全部实现/验证required证据完成，Review/Git门禁仍待交付。

# 完成证据与状态



## 新鲜证据

Source锁定5f406b3，本总任务不换版；D-RED：`.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s tests -p test_wecom_sender.py -v` exit1，ModuleNotFoundError: wecom.sender；正式发送实现尚未编写。官方path90236/91039/90313经最新HTTPS原文核实，浏览器工具读取失败后本机HTTPS成功，runtime保留原文。

D-GREEN：Windows/Python3.14.7，`.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s tests -v` exit0，147 passed / 0 failed / 0 skipped，27.486s（原129项+17发送专项+1回调成员边界）。原始runtime/wecom-stage-d-regression.log。D专项17项覆盖单员工payload、Token缓存/20并发一次刷新、40014/42001一次纠正、明确未接受后的Token GET故障、UTF8完整分片、HTTP200业务失败/无效用户/畸形名单、消息网络未知、HTTPX查询脱敏、统一四类通知/用户隔离、发送失败不重问ADP/不锁用户、v2兼容/环境隔离、防Fake伪造接受、武装/结果发布失败及取消交接。C夹具只按已批准v3同步版本/证据源，不删减旧断言。

D-HTTP：`.venv/Scripts/python.exe -B -X utf8 tests/wecom_dispatch_smoke.py --simulate-notifications --report runtime/wecom-stage-d-http.json` exit0。独立正式Uvicorn→真实本机HTTP/SSE Fake→已存提示→正式Simulator：100员工peak100，Fake30名额peak30，本次保持屏障实际70入口拒绝并按真实集合核对；最大ACK412.1/514.5ms。PID22084/14808、26312/10216。ADP强杀组15604/30220/6768，旧RequestId仅一次，UNKNOWN释放/RECEIVED首次续做与SIYU隔离；通知强杀组29048/29884/19580，在SEND_ARMED后已进入模拟发送边界、持久记录一次attempt再强杀，重启SEND_UNKNOWN，旧通知没有再次调用。所有自有进程退出、端口释放、临时目录清理。测试身份/密文不证明云端认证，所有成功通知source=simulated/SEND_SIMULATED，没有真实企微API调用。

D-PROTOCOL：2026-11-08联网获取企业微信官方path90236（消息发送/2048字节/@all/invaliduser等）、91039（应用Token缓存及expires_in/提前失效）、90313（40014/42001/40003等）、90195（成员UserID首字符及唯一性忽略大小写）完整当前正文。浏览工具不可读后本机HTTPS200读取，原文在忽略runtime保留。所有实现决定依原文而非搜索摘要。新发送面发现@all广播风险，先失败单元回归，再共用官方成员格式校验于回调/快照/CLI/发送；无效用户大小写比较先Red后Green。A原值/派生算法未变。另首版Token刷新连接故障误作SEND_UNKNOWN，新增Red后按明确未接受证据修为SEND_FAILED，保留实际42001并不重发。

D-PACKAGE/DOCS：`uv build --wheel --out-dir runtime/wecom-stage-d-package --index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple --no-progress` exit0，zipfile CRC/8个变更正式源码逐字节通过，无依赖版本/lock变化；既有Source任务已核验临时镜像，未改全局源。现有README、索引、架构、日志边界、Roadmap和运维同步，D1～D5原语义保留/AC20～24别名。31个当前Owner本地链接有效，新增/修改函数中文说明AST完整。实际config仅补四个默认字段，全部旧值保留，实际/example section/key相同；无真实Secret/Token写入代码/测试/Git。完整diff whitespace检查通过，CI文件差异为空。新快照v3保留环境/发送证据源；v1/v2先完整校验后按test范围升级，不能把测试目录切production或补造旧接受证据。默认disabled，真实模式未执行。

## 未验证内容与剩余风险

D实现/本地验证完成；独立Review、当前head CI、guarded merge、main-fresh、归档及清理未完成。FakeHTTP不是真实企微；F明确排除；不追踪在线/已读/终端展示。

## 交付状态

implementation=complete；delivery=incomplete；validation=complete；main_fresh=incomplete；change_archive=incomplete；requirement_closure=not_applicable（正式Roadmap）；cleanup=incomplete；end_to_end=incomplete。

## 备注

不修改Agent_Skills canonical，不建重复计划/Issue，不增加远程CI。
