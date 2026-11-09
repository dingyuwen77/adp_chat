---
schema: coding-change/v1
id: "CHG-20261109-133229-wecom-ordered-images"
title: "企业微信有序图文回复"
level: L3
status: ready_for_review
owner: "Codex"
branch: "feature/wecom-ordered-images"
created: 2026-11-09
updated: 2026-11-09T14:13:57+08:00
completion_gate: required
depends_on: []
affected_areas:
  - "adp"
  - "wecom"
  - "configuration"
  - "docs"
affected_paths:
  - "src/adp/client.py"
  - "src/wecom"
  - "src/application/wecom_worker.py"
  - "src/core/config.py"
  - "tests"
  - "config.example.toml"
  - "README.md"
  - "docs/wecom-integration"
  - ".agents/changes/archive"
  - "docs/adp-chat/02_Docker部署与运维.md"
contracts:
  - "企业微信应用消息与临时素材"
  - "wecom配置"
data_changes: []
---

# 变更摘要

按正式 reply 顺序发送企业微信文字及原生图片，保留来源与尾文，安全有界准备图片并保留发送未知语义。本地 main/CLI 不增加图片显示。

# 背景、现状与问题

基线 main=991b0717e7434fae7ca2978e04dc843bc4c5789f；原 Sender 仅使用 notification 文字，已保留 reply_contents 未用于原生媒体；文字按2048字节硬切，员工通知可能交错。用户要求落实引用会话最终方案并合并远程main。

# 事实与证据

| 证据 | 已确认事实 | 来源 | 决策 |
| --- | --- | --- | --- |
| E1 | 原始有序内容及v3发送状态已存在 | src/adp/client.py、src/storage/messages.py | 复用，不迁移存储 |
| E2 | 发送仅文字、通知并发独立 | src/wecom/sender.py、src/application/wecom_worker.py | 加图文规划与员工互斥 |
| E3 | 当前官方临时素材支持JPG/PNG、10MB | https://developer.work.weixin.qq.com/document/path/90253 本机读取原文 | 修正旧方案2MB口径 |
| E4 | 用户2026-11-09确认既有F真实客户端已验收，并要求保留/api/v1/chat | user:当前补充 | 不删除接口；新增图文实测独立记录 |

推断与未验证：旧示例图片HEAD为404，真实图文客户端显示本任务不执行；公网访问控制无新实测。

# 目标、成功标准与非目标

## 目标
企业微信按最终答案顺序获得文字/原生图片或原位置链接降级。

## 成功标准
以正式 #28 AC1～AC7、AC8a及用户新增AC9为实现/合并前施工验收；AC8b为合并后关闭前交付轴；AC8c为Issue关闭后required收尾，整体端到端完成仍须全部轴完成。

## 范围
规划、下载、上传、发送、员工互斥、配置、测试和针对性文档。

## 非目标
不增加main/CLI图片显示，不改API/ADP会话/v3格式，不升级依赖、不部署或发送真实企微测试消息。

## 必须保持不变
零ADP自动重试，SEND_ARMED恢复UNKNOWN，单员工身份和去重，保留/api/v1/chat及原始API响应。

# 约束与意图决策

| 维度 | 决定 | 依据 |
| --- | --- | --- |
| Owner | 企业微信输出模块负责规划/图片，Worker负责员工串行 | E1/E2及#28 |
| Contract | 新增两个可选wecom字段，旧配置缺省兼容 | config.py及config.example.toml |
| 数据 | 不修改v3快照；原始URL沿用现有保留策略 | E1 |
| 失败 | 准备失败降级；提交后响应未知停止不重发 | #28 AC5 |
| 回滚 | 回退代码及配置新增项，保留状态目录，不部署 | 用户授权边界 |

# 修改方案与决策依据

## 最小充分方案
1. Incremental随stream。2. 正式reply规划为有序块，文字安全分段。3. 图片仅可信HTTPS/公网固定IP连接、逐跳校验、限量内存下载与临时素材上传。4. 复用Sender状态与Token，素材失败原位置链接降级。5. Worker为完整员工通知加互斥。6. 回归、文档、独立审查、现有CI/PR交付。

## 证据到决策
复用E1避免Schema迁移；E2保留发送器错误语义并在正确Owner串行；E3使用10MiB/20MiB/8张/共享4名额。

## 备选方案与取舍
保留纯文字无法达成原生图片目标；mpnews整篇卡片需点击且改变用户体验；选择现有Sender增量扩展，按已确认原生图文方案实施，不另建服务/队列/数据库。

# 需求追溯

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | 非流式Incremental及最终完成机制 | #28 / AC1 | satisfied | src/adp/client.py + test_chat；实现revision4ba7f7d的CI42571409501执行197项/OK，默认disable/false与enable/true均有断言 |
| R2 | 有序图文与尾文来源 | #28 / AC2 | satisfied | reply_plan.py + 规划9项/正式加密HTTP关键链路1项；保序、来源/尾文、结构化与跨文字块图片、代码/转义已验证 |
| R3 | 2048字节和链接完整 | #28 / AC3 | satisfied | test_wecom_reply_plan与test_wecom_sender；2048字节、UTF8重组、自然边界、超长来源链接保留相邻尾文均通过 |
| R4 | 安全有界内存图片准备 | #28 / AC4 | satisfied | 图片13项/OK；公网DNS、逐跳固定IP、MIME/实际字节/累计预算、8张/共享4名额、无Cookie转发；真实TLS成功下载未实测，不夸大Fake证据 |
| R5 | 准备降级与发送未知 | #28 / AC5 | satisfied | test_wecom_images/test_wecom_sender；准备失败原位置降级、图片POST超时停止UNKNOWN、部分计数与既有恢复均通过，v3不变 |
| R6 | 员工通知串行并独立ADP | #28 / AC6 | satisfied | test_wecom_sender屏障先Red复现交错再Green；跨员工继续、等待保持PENDING、ADP占用不延长；取消/武装恢复沿用既有回归 |
| R7 | API/CLI/v3/依赖兼容与文档 | #28 / AC7 | satisfied | CI197项包含原main/CLI/API/v3回归；实际TOML字段/类型及旧值保留核对；146个文档本地链接与源码AST解析通过 |
| R8 | 仅AC8a合并前门禁子阶段：验证与独立审查 | #28 / AC8 | satisfied | 独立Reviewer /root/independent_review 绑定4ba7f7d：F1/F2/F3/F4均关闭、NO_FINDINGS_WITHIN_SCOPE；CI42571409501 Windows Python3.14执行197项/27.435s/OK；AC8b未完成，Issue整体AC8仍未勾 |
| R9 | AC9历史引用修复、语义守恒及全仓校验 | #28 / AC9 | satisfied | 6份归档35处路径修正，34条Source/AC锚点实读存在；所有其他字节/元数据保持；全仓严格Ready随后以真实输出核验，新head独立Review仍为合并前必需门禁 |

AC8b（guarded merge/main-fresh/Closure Audit）属于合并后、Issue关闭前交付轴；AC8c是Issue关闭后required的cleanup/archive轴，不作为关闭的循环前置。Ready不宣称这些已完成，Change保持active直到适用收尾条件满足，end_to_end仍incomplete。

# 计划改动

| 能力 | 文件 | 可观察证据 |
| --- | --- | --- |
| 请求模式 | src/adp/client.py | test_chat |
| 有序规划 | src/wecom/reply_plan.py | test_wecom_reply_plan |
| 下载/发送 | src/wecom/images.py、src/wecom/sender.py | test_wecom_images |
| 通知互斥 | src/application/wecom_worker.py | test_wecom_sender |
| 正式关键链路 | tests/test_wecom_image_workflow.py | 加密回调到真实本地状态和Fake HTTP图文 |
| 配置/文档 | config.example.toml、README.md、docs/wecom-integration、运维文档 | TOML/链接/语义核对 |

# 验证矩阵

| 验证层 | 是否要求 | 范围 / 证据 |
| --- | --- | --- |
| 行为 / 单元 / 组件 | required | 规划、链接边界、畸形文本复杂度、下载资源/安全和发送分类 |
| 接口 / 契约 | required | payload、multipart、图片media_id、旧API/CLI与v3兼容 |
| 集成 / 持久化 / 运行依赖 | required | 真临时文件、员工互斥/跨员工并行、恢复取消 |
| 用户 / 工作流验收 | required | 正式加密HTTP回调→文件/Worker→Fake远端消息序列；真实客户端新图文不在本次执行范围 |
| 跨组件关键路径 | required | test_wecom_image_workflow |
| 外部依赖 / 供应方探测 | not_applicable | 不发送真实企微消息或新增付费ADP请求；已读取官方协议，旧图片404明确记录 |
| 构建 / 打包 / 运行 | not_applicable | 不改构建元数据/入口/依赖，不生成部署产物；正式进程入口由既有回归与HTTP lifespan覆盖 |
| 文档 / 治理 / 其他 | required | TOML值保留、示例/文档链接、Change规范、Issue/PR与CI |

## 验证计划
.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s tests -v；先目标用例再完整既有回归；canonical ready_check/governance_contract；既有Windows Python3.14 PR与main CI。

# 风险、兼容性、迁移与回滚

| 项目 | 结论 | 依据 |
| --- | --- | --- |
| 安全 | 精确allowlist、每跳公网DNS并固定IP、HTTPS/SNI验证、禁代理与自动重试 | images.py |
| 资源 | 每张10MiB、每回复20MiB/8张、共享4名额；发送与准备预算独立累计 | images.py/sender.py |
| 兼容 | main/CLI/API/v3格式及依赖保持；可选配置缺省兼容 | #28 AC7 |
| Migration | 无 | 不改快照Schema |
| 部署 | 未授权，不执行 | 用户仅要求本地代码与远程main |
| 回滚 | 回退代码、移除新增配置键，保留状态 | 旧版本不能解析新增键，不能删除恢复证据 |

# 文档、依赖、部署与发布影响

Docs Impact=targeted：架构解释有序输出/失败/资源；路线图区分用户确认的既有F客户端验收与新图文；运维解释allowlist与预算。新增image_allowed_hosts/image_prepare_timeout_seconds，实际配置值保留并同步示例注释。无依赖/Runtime/存储Schema升级，无Release/Deploy。

# 完成审计

- [x] upstream_re_read：已重新读取live #28与用户最新F客户端/保留API决定，AC8时序已正式澄清且live Contract通过。
- [x] change_coverage：逐AC1～AC7/AC8a核对实现、文档和直接证据；AC8b/c保留在交付轴，不提前宣称完成。
- [x] reverse_audit：正式加密回调→真实文件/Worker→Fake远端图文；反查API/CLI无图片下载，未运行真实终端不写已实测。
- [x] unresolved_cleared：合并前施工项与阻塞Finding已清零，Validation Asset Redundancy=clean；合并后轴仍独立追踪。

# 完成证据与状态

## 新鲜证据
Red：Incremental真/假断言失败；图片规划模块缺失；同员工屏障重现通知交错；独立审查F1尾文丢失/F2畸形图片二次方扫描的两个回归失败。目标修复后规划7、图片12、Sender18与正式加密回调关键链路1均已通过（工作区Python3.14.7，Fake远端）。

## 未验证内容与剩余风险
未执行本次图文真实客户端、生产部署、公网访问控制及真实TLS下载成功验收；既有F文字客户端由用户2026-11-09确认。平台接受不代表终端显示或已读；UNKNOWN快照URL可能长期保留。

## 交付状态
implementation=complete；delivery=pr_ready；validation=complete；main_fresh=incomplete；change_archive=incomplete；requirement_closure=incomplete；cleanup=incomplete；end_to_end=incomplete。AC8b/AC8c在合并后证据取得后继续完成。

## 备注
canonical Source已同步且本任务锁定dbae4a011f78ddeaf6e7a0411d3fb40df858fbed。只有主Agent写入；独立Reviewer只读并有上下文隔离。


## 根因调试与最终审查证据

同步解析的实际风险是重叠候选反复扫描或复制同一输入区间，不能只修首个while。已审计并切断：未闭合括号尾扫、代码正则回溯/区间反复遍历、无效嵌套长切片、共享空白/title逐字符尾扫及连续文字拼接复制；采用一次索引/前进游标/二分区间/list-join。六族复杂度回归同时计入字符读和切片复制；原共享空白n=500先Red255995>35000，最终独立复测4498≤35000；生产函数840000字符约0.1603秒，Reviewer独立约0.1631秒，原文保留。规划9项、图片13项、正式加密回调链路1项在最终实现工作区直接复验通过；CI42571409501绑定4ba7f7d执行197项/27.435秒/OK。后续Change-only更新不改变这些实现边界，最终PR current-head gate仍独立执行。


## Ready检查范围

此前全仓Ready发现34条旧归档Source断链。用户随后明确要求全部修复，因此纳入AC9/decision_epoch=2；6份历史归档仅替换已迁移的文档路径，35处旧引用及34个稳定AC锚点已重新核对，元数据与其他历史字节保持。现在必须执行全仓严格Ready并取得零错误证据，不再将这些错误排除出本任务。新head的Review与CI仍须满足后才合并。


## 用户新增归档修复范围

2026-11-09用户明确要求把旧归档链接错误一并修复，正式来源#28新增AC9。所有原需求、决策、验收证据、Change ID/status/Owner/时间均保留，只修当前Source文档迁移引用；无产品实现变化，不新增永久测试或Workflow，采用原canonical全仓检查、锚点与字节差异作为直接证据。独立Review/current-head CI在新的revision执行，既有实现证据保持。
