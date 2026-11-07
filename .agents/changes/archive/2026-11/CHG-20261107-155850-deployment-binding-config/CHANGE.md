---
schema: coding-change/v1
id: "CHG-20261107-155850-deployment-binding-config"
title: "服务器监听地址读取TOML并隔离本地运行"
level: L3
status: done
owner: "Codex"
branch: "main"
created: 2026-11-07
updated: 2026-11-07
completion_gate: required
depends_on: []
affected_areas: 
  - "deployment"
  - "config"
affected_paths: 
  - "src/core/config.py"
  - "scripts/run.sh"
  - "config.example.toml"
  - "config.toml"
  - "tests/test_docker_package.py"
  - "tests/test_project_startup.py"
  - "tests/deploy_smoke.py"
  - "README.md"
  - "docs/adp-chat/04_Docker部署与运维.md"
contracts: 
  - "config.toml [deployment] bind_ip/host_port"
data_changes: []
---

# 变更摘要

用户要求服务器 IP/端口在 config.toml 配置并由服务器部署读取；本地运行忽略这两项。变更前 scripts/run.sh 只读取环境变量/成功状态，src/core/config.py 拒绝未知分组。用户后续明确要求两份配置只差 AppKey，config.toml 现有值必须保留；本次一并覆盖。

# 目标、范围与成功标准

- AC1：服务器 [deployment].bind_ip / host_port 自动用于真实 Docker 发布端口。
- AC2：本地 CLI 和直接 Uvicorn 运行忽略部署字段，不改变其启动参数或 ADP 请求。
- AC3：缺少新配置时兼容原环境变量、成功状态和默认 10.102.70.82:4560；显式配置优先于旧状态，环境变量保留覆盖能力。
- AC4：非法 IPv4、端口类型/范围、未知部署字段在停止旧容器前失败；会话与数据保护不变。
- AC5：两份配置同步新增分组且解析后仅 adp.app_key 不同，现有 AppKey、身份、路径和其他值保持；文档说明读取范围、重载和旧镜像升级顺序。
- AC6：真实部署切换、幂等、失败恢复和本地隔离均有证据，完成独立只读 Review。

范围：元数据列出的配置读取、部署入口、现有测试和文档。本地源码安装使用 Python 3.14.7，unittest；服务器脚本 Bash + 镜像 Python。风险 L3：公开配置与部署恢复。规则 Source main=ffd7ac58a52c7561a99bb31580e970debaa2744d；项目基线 main=026ed66cd040e3f020d872b224e14c743026180e，包含已有未提交工作，逐项保护。

非目标：修改 API、容器 8000 端口、Compose 的环境变量入口、依赖/Runtime、会话格式/路径迁移；不连接真实服务器或 ADP，不提交/推送/合并/发布。

# 方案与兼容

采用独立 deployment 分组；本地 load_config 跳过该字典，不将字段放入 ChatConfig；run.sh 内嵌部署解析器负责严格验证及优先级。优先级为非空环境变量 > 显式 TOML 字段 > 已有成功状态 > 旧默认值，按字段解析。端口输出规范化为整数。

备选：将字段加入 ChatConfig 会让应用本地承担无关配置；增加外置 .env 会违背用户集中 TOML 的目标。选独立分组复用既有部署 helper，保持宿主不需要 Python。

旧镜像的 load_config 不识别新分组，真实回滚也无法启动带新分组的配置。因此提供停机前明确兼容提示：先保持旧 TOML 升级到包含新加载器的镜像，再添加 deployment 并对同版本 upgrade。回退到旧镜像需先恢复不含新分组的配置备份。新版本之间失败恢复仍使用旧镜像/旧端口/原会话和当前配置，不伪称自动恢复旧配置。

# Requirement Traceability

| ID | Requirement | Source | Status | Evidence |
| --- | --- | --- | --- | --- |
| R1 | AC1 服务器从 TOML 自动读取绑定地址/端口 | user:deployment-binding#AC1 | satisfied | run.sh deployment_binding/plan；真实 smoke.log 第106行改绑与第127行初次部署 PASS |
| R2 | AC2 本地忽略部署设置 | user:deployment-binding#AC2 | satisfied | core.load_config 不产生部署字段；startup 两项测试实际 CLI 与明确 host/port 的 Uvicorn health200；完整107项退出0 |
| R3 | AC3 优先级与旧配置兼容 | user:deployment-binding#AC3 | satisfied | 生产内嵌解析器测试 TOML覆盖state、env覆盖TOML、按字段缺项回退、旧加载器明确拒绝；真实smoke原配置和显式配置均PASS |
| R4 | AC4 非法配置预停机失败、数据保持 | user:deployment-binding#AC4 | satisfied | 类型/范围/IP/未知字段矩阵；实际非法TOML保留containerID/state/session，故障目标恢复旧实际端口；smoke.exit=0 |
| R5 | AC5 配置值保护和文档同步 | user:deployment-binding#AC5 | satisfied | 两文件仅adp.app_key语义差；删本次尾部增量后原文件SHA精确相等；README/运维说明与124本地链接/4heading检查通过 |
| R6 | AC6 独立 Review 和真实部署工作流 | user:deployment-binding#AC6 | satisfied | 隔离只读 Reviewer /root/deployment_config_review 最终PASS/NO_FINDINGS；107回归、正式Dockerfile构建、Linux非root真实smoke退出0 |

# Validation Matrix

| Layer | Required | Scope / Evidence |
| --- | --- | --- |
| 行为 / Unit / Component | required | 107 unittest / 0失败 / 0跳过 / exit0（e63e09）；含生产内嵌解析器优先级、类型、范围与本地忽略 |
| 接口 / Contract | required | 4项部署解析测试 + 2项真实本地入口测试，Reviewer独立6项退出0；不改变HTTP/ADP/会话schema |
| 集成 / Persistence / Runtime Dependency | required | dist/.validation/deployment-config-smoke.log/.exit=0，真实Linux FS/UID1000调用者/10001应用，端口/state/session断言 |
| 用户 / Workflow Acceptance | required | 公开sh run/upgrade实际切换/幂等/拒绝/恢复，实际本地Uvicorn明确参数health200且退出无监听残留 |
| 跨组件 Golden Path | required | TOML→生产脚本→正式Dockerfile镜像→真实发布/健康/原子状态；smoke.log第106及127行PASS |
| 外部依赖 Probe | not_applicable | 不改变 ADP 接口，不请求服务器/云端；Docker 是本机隔离验证依赖 |
| Build / Package / Runtime | required | docker build --platform linux/amd64 本次scratch镜像 exit0（ae0f1b），实际docker save归档、load/启动；未发布正式版本 |
| Docs / Governance / Other | required | 两TOML原字节保护、数据hash、9文档/124链接/4heading/6JSON-TOML/3sh与11Bash块/AST/diff-check通过；独立Review PASS |

# 计划与执行

1. 写部署解析和本地隔离的真实回归，确认目标 Red。
2. 最小实现忽略分组与部署解析，确认 Green；仅输出低频已选绑定信息。
3. 扩展现有 Linux 部署 smoke 场景，验证配置端口修改、非法配置保护和恢复。
4. 同步两份配置及 README/运维文档；原值和会话、日志保持。
5. 当前 revision 验证、Completion Audit、独立 Review、Ready 检查；按用户既有本地归档授权收尾，明确未提交/未部署。

# Completion Audit

- [x] upstream_re_read：重新核对用户部署配置要求、本地忽略约束及最新两份配置仅AppKey差/保留原值要求。
- [x] change_coverage：AC1 至 AC6 对照实现、真实入口和当前证据全部满足。
- [x] reverse_audit：TOML→实际绑定、本地TOML→明确启动参数、失败→原会话/旧端口/旧镜像均反查到可观察断言。
- [x] unresolved_cleared：独立两阶段Review PASS/NO_FINDINGS_WITHIN_SCOPE；验收无阻塞，ARM64服务器未操作的边界明确。

# 完成证据与状态

## 实施与验证

- 修改仅为 core/config.py 本地跳过 deployment 表、run.sh 解析/计划输出与旧加载器兼容提示，两份配置尾部追加同一分组；扩展现有部署和启动测试，README/运维同步。stop/upgrade通过现有公共入口复用，未改依赖、Dockerfile、API、会话格式和路径。tests/test_api.py 与任务前hash相等，保留既有修改。
- 真实 Red：新增分组遭原 load_config unknown_section；修复后完整命令 `.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s tests -q` 返回0，107 tests/0失败/0跳过/12.298s（e63e09）。独立Reviewer另跑6项目标回归，exit0/6OK/3.314s。
- 真实 Build：正式 Dockerfile，`docker build --platform linux/amd64 --tag adp-deployment-config-test:20261107-bce05a4b --label io.nnit.adp.binding-test=20261107 .`，exit0；image sha256:611ddc2fd9482a5c80c42ff5d0064fc43e6898eb3d1cb0c6005462662f3f6ed4。其实际docker-save测试包用于smoke，并非正式发布。
- 真实 Workflow 命令：`.venv/Scripts/python.exe -B -X utf8 tests/deploy_smoke.py --first dist/.validation/deployment-config-20261107-bce05a4b/v0.0.7 --second dist/.validation/deployment-config-20261107-bce05a4b/v0.0.8 --cli-binary dist/.validation/deployment-config-20261107-bce05a4b/docker --unprivileged`，exit0（7c0409）；完整原始日志 `dist/.validation/deployment-config-smoke.log`，退出码 `deployment-config-smoke.exit=0`。撤销环境覆盖后实际TOML绑定、同版本幂等、非法配置不停止旧容器/不改状态会话、失败目标新端口恢复旧实际端口、独立首次部署TOML、普通用户权限、空格目录/绝对路径、旧包完整性/身份/缺失会话门禁均通过。
- 首次smoke仅因本任务临时checksum fixture被Windows默认写成CRLF，在严格清单门禁拒绝；修正fixture为LF并重新核对tar/hash后重跑通过。保留 `deployment-config-smoke-fixture-red.log`；未放宽产品校验。smoke总预算增加150秒仅对应新增两个既有75秒健康窗口，单阶段超时未放宽。
- 文档/语法：9文档124本地链接、4heading均无错；6JSON/TOML块实际解析；3部署sh与11Bash示例语法；4Python AST；git diff --check退出0（仅既有换行提示）。配置和生成包均实际重新读取/解析。
- 原值保护：追加之前所有字节仍精确相等（原config SHA 0bd58e5832fe73825e6158ae0004f5ea4ad11d795d93e5ad5248af4ed4559173；example 646bd74122be793deaa62b11ed0db86da5cd3e249a930a376e0bb8df0f54930f）。当前两配置解析后仅adp.app_key不同；未打印/备份真实密钥。runtime/session.json 的 c3f9fb9a59f28a8ba3ae5e9ec733837d40fa68c158fd001fad6cd70092e92451、logs/adp_chat.log 的 fea40a9ef88ee4246deeb4f2f990f7f1fdf37f29a48f8bfff22efa9352613053 均保持。
- 当前实现fingerprint：core/config.py d856bb5929e0f028932ee49ba2e6676e740c8d5387972766ab1161d284f28cc1；run.sh 68e74decdb82a0689f982bb5b3297dd1c924e2369d884d23f2c92327a6895986；deploy_smoke.py 975bfefa42543af0d497797c88135d5044a2ca164f71a9a41e13f4d50a7af42b。Build、验收和Review后无实现变化。

## 两阶段 Review 与边界

Reviewer /root/deployment_config_review 为原生上下文隔离只读审查，未参与实现/再委派；canonical要求的独立复核，不是实现任务委派。decision_epoch=1。独立恢复用户原文AC、审查源码/正式文档、实际跑6目标测试、内存核对真实两配置唯一AppKey差及原字节/数据hash、实际读最终smoke原始日志和exit0。最终结论 PASS / NO_FINDINGS_WITHIN_SCOPE，A1需求覆盖与A2工程机制均通过，无阻塞Finding。

Parent重新核对上游原文及最新配置要求，AC1–6与8层Validation Matrix闭合。外部ADP/跨机网络不变，不请求真实云端。证据环境为Windows/Python3.14与本机Linux AMD64 Docker；没有在目标ARM64服务器运行，也未构建正式ARM64发布包。默认打包ARM64保持，用户后续须用新版本号重新打包并上传新脚本。旧v0.0.6首次启用deployment必须按文档两阶段升级；若回退旧加载器须先恢复兼容配置，不能删除session/state。

## 资源清理与收尾

Smoke自行清理容器、Linux fixture与故障镜像。随后逐个核对本任务3标签指向上述scratch ID后删除：adp-deployment-config-test:20261107-bce05a4b、adp_channel_api:v0.0.7/v0.0.8；验证专属Windows临时目录绝对路径在项目dist/.validation内后删除tar/CLI。既有v0.0.5/v0.0.6标签、正式离线包与用户数据保留（823228）；保留原始验证日志。当前源码Review PASS，后续仅Ready与本地归档元数据收尾。


Git：main，用户已有修改保留；没有提交、PR、CI、合并、发布或生产部署授权。真实目标 ARM64 服务器未操作；本机 Docker AMD64 与本地 Windows 的受测边界单独报告。现有会话格式和持久路径不改变；依赖不变。

## 最终本地归档

Ready检查 --require-active-ready 退出0，ok=true，strict_checked=6，errors=[]（c4f9cf）。按本聊天用户明确允许完成Change自行本地归档的授权，移动到本归档目录并标记done；没有借此执行GitHub/提交/推送。原active已不存在，归档已读回，全部R1–R6 satisfied、Completion Audit已勾选。归档不代表发布或生产部署。
