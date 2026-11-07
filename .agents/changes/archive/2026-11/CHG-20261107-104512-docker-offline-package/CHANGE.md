---
schema: coding-change/v1
id: CHG-20261107-104512-docker-offline-package
title: adp_channel_api 版本镜像打包及手动上传后的安全升级
level: L3
status: done
owner: dingyuwen77
branch: main
created: 2026-11-07T10:45:12.1243090+08:00
updated: 2026-11-07T13:13:24+08:00
completion_gate: required
depends_on:
  - CHG-20261107-000020-src-root-layout
affected_areas:
  - 本机版本化镜像构建
  - 服务器手动上传与单实例升级
  - 会话保护和本地归档
affected_paths:
  - scripts
  - tests/test_docker_package.py
  - tests/deploy_smoke.py
  - .gitattributes
  - .dockerignore
  - .gitignore
  - .github/workflows/tests.yml
  - README.md
  - docs/adp-chat/04_Docker部署与运维.md
contracts:
  - scripts/build_image.ps1 -Version
  - scripts/run.sh VERSION
  - scripts/stop.sh
  - scripts/upgrade.sh
  - adp_channel_api version manifest
data_changes: []
---

# 变更摘要

按本聊天最后确认的方案实施本地代码：镜像名称 adp_channel_api；本机 scripts 下提供 build_image.ps1、run.sh、stop.sh、upgrade.sh。用户手动上传后三个脚本、正式 config.toml 和 dist 版本目录。服务器 release 根目录使用固定配置，日志和 runtime 位于项目根，升级保留原会话。没有 .env、自动上传、远程部署、提交或推送。

# 背景、现状与问题

当前 main/026ed66cd040e3f020d872b224e14c743026180e 工作区含已完成的 src 迁移及 PowerShell 客户端修复，全部保护。本次 canonical 已 fetch 核实并锁定 ffd7ac58a52c7561a99bb31580e970debaa2744d。现有镜像 UID/GID10001，FastAPI单worker，API和会话Schema不变。config相对路径以文件目录为基准。load_session已有文件有效时直接返回原ConversationId，身份不符或损坏则拒绝覆盖。

旧草稿中的 .env、deploy/start.sh 和组合发布包已被用户明确替代；未实施这些旧设想。本契约按最新用户原文重新归一 AC1–AC9，不把自身当作上游来源。

# 事实与证据

E1：Dockerfile、compose.yaml声明既有非root身份、端口8000、240秒停止宽限、健康检查及重启策略。
E2：src/core/config.py、src/storage/session.py、src/application/chat_service.py确认配置定位和会话复用机制。
E3：本机PowerShell5.1/7、Python3.14.7、Docker29.6.2 Linux/x86_64、Docker Desktop WSL可用；Docker/WSL访问须宿主执行权限。
E4：工作区原Change与本Change在文档和测试路径重叠；当前主Agent单写，依赖已完成源码迁移，不覆盖其他改动。

# 目标、成功标准与非目标

- AC1：本机 scripts 提供四个脚本，打包仅需指定版本；镜像名为adp_channel_api，稳定版本vX.Y.Z。
- AC2：dist/vX.Y.Z生成镜像tar、manifest.json与SHA256SUMS；不覆盖既有版本，失败不留下伪成功产物，不包含秘密或生产数据。
- AC3：用户自行上传，服务器无需额外上传Compose/.env/解析脚本，也不要求宿主Python；三个sh使用镜像Python读取TOML。
- AC4：固定读取release/config.toml，默认../log和../runtime，父目录持久挂载；缺失目录自动创建并匹配10001，既有文件不覆盖。
- AC5：已有session.json、AppKey/UserId和Schema兼容时原文件字节及ConversationId不变；升级中缺失/损坏/身份或路径漂移必须阻止切换，不重建。
- AC6：版本数字排序，先验证并加载目标镜像再替换，单实例与归属检查、部署锁、健康检查、失败恢复和明确退出码；保留上一成功版本。
- AC7：Windows脚本编码和Linux LF、相关回归、真实Docker产物与Linux升级链、文档同步、独立Review闭环。
- AC8：仅本地修改和隔离验收；不改HTTP/ADP/会话Schema/运行依赖，不连接服务器、不执行真实ADP、Git提交推送合并。
- AC9：用户明确授权主Agent本地归档本任务及其他已完成Change，不依赖GitHub自动归档；此本地收尾不代表Git集成。

# 修改方案与决策依据

PowerShell构建Linux镜像并显式设置版本标签及OCI元数据，导出tar，产生机器清单和校验和，临时目录成功后原子移入dist版本目录。三个Bash入口可用sh执行后自动转入Bash，停止/升级复用run.sh中的统一控制逻辑，不要求上传第四个帮助文件。服务器由镜像自带Python执行内嵌的配置/清单/会话校验；Docker调用和宿主文件操作留在宿主脚本。

使用Docker CLI实现离线启动，复用既有运行参数。先校验候选与原会话，再优雅停旧容器；新容器健康且会话一致后原子记录最近成功部署，失败恢复旧镜像。目录新建设置属主，原目录/配置/会话不递归改写。应用照旧10001运行，健康检查不发送业务请求。

备选：要求服务器Python3.11会增加运行依赖；额外上传Compose/帮助脚本不符合用户上传范围；容器挂载Docker socket作为部署控制器扩大生产权限。选择自包含的三个shell入口加镜像Python校验，测试环境与生产数据隔离。

# 需求追溯

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | 四脚本与版本镜像 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2#AC1 | satisfied | scripts/build_image.ps1、run.sh、stop.sh、upgrade.sh；真实v0.0.3构建日志与manifest |
| R2 | 可靠且无秘密的版本产物 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2#AC2 | satisfied | 公开PowerShell入口正反例、重复与失败清理均通过；三版本tar/SHA256/配置摘要重读通过；Docker smoke证明镜像无正式数据 |
| R3 | 手动上传边界及服务器依赖 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2#AC3 | satisfied | 三个sh自包含，镜像Python解析TOML；Linux仅上传三sh、配置和版本目录后真实执行成功 |
| R4 | 单配置和本地持久目录 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2#AC4 | satisfied | Linux相对/绝对路径、带空格release、缺目录10001创建及真实挂载验收通过；README及运维Owner同步 |
| R5 | 原会话不变及异常保护 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2#AC5 | satisfied | Linux原session字节/CID不变；缺失、坏JSON、身份、路径、状态不可读/坏JSON/悬空链接全部拒绝；根真实数据hash相同 |
| R6 | 升级、归属与失败恢复 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2#AC6 | satisfied | 真实数字版本选择、冷load、锁、归属、端口变更、健康、cache引用保护/成功后清理、故障CMD恢复通过 |
| R7 | 验证、文档及独立Review | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2#AC7 | satisfied | 99项回归/真实Linux/正式build及独立复审PASS；证据见完成记录 |
| R8 | 本地授权与兼容边界 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2#AC8 | satisfied | 运行依赖、HTTP/ADP和会话Schema未改；仅本机隔离数据，未提交/推送/部署/真实ADP；保护三份真实文件hash相同 |
| R9 | 主Agent本地归档已完成Change | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2#AC9 | satisfied | 用户明确本地归档授权；主Agent执行两个已完成Change归档，最终位置与读回见归档记录 |

# 计划改动

1. 建立公开打包入口的目标Red，覆盖产物、版本拒绝、失败与重复保护。
2. 实现脚本和编码/构建排除，使用临时数据完成Linux真实运行、升级和故障恢复。
3. 同步现有运维Owner文档和README；CI只扩展相关路径，不改Job/check身份或安装测试步骤。
4. 执行Completion Audit、隔离只读Review和适用Ready检查；按用户授权本地归档完成Change。

# 验证矩阵

| Layer | Required | Scope / Evidence |
| --- | --- | --- |
| 行为 / Unit / Component | required | PowerShell公开入口的正常产物、非法版本、构建失败、版本重复和清理 |
| 接口 / Contract | required | tar/清单/校验和、三个sh入口、路径与会话检查的固定协议 |
| 集成 / Persistence / Runtime Dependency | required | 真实Linux目录权限、Docker save/load、原会话字节保持、容器归属和部署锁 |
| 用户 / Workflow Acceptance | required | 仅-Version打包；真实Linux环境执行run/stop/upgrade |
| 跨组件 Golden Path | required | 打包产物到真实容器HTTP健康、版本替换和失败恢复 |
| 外部依赖 Probe | not_applicable | 不修改云端接口，不连接服务器或ADP |
| Build / Package / Runtime | required | 正式build/save、归档重读、load、标签和镜像ID、非root启动 |
| Docs / Governance / Other | required | 脚本编码/解析、受影响链接、CI YAML及路径责任、Change/Review/归档 |

# 风险、兼容性、迁移与回滚

HTTP/ADP、应用配置字段、Schema和运行依赖保持。运行脚本读取config.toml原有session_file/log_file，服务器配置移到release时使用../路径；本机默认配置保持。无自动数据Migration。已有会话缺失或身份不符必须fail closed。镜像回退不保证恢复不兼容的数据迁移，当前任务不引入迁移。

默认linux/amd64，支持显式linux/arm64但本机只证明原生架构。服务器需Linux、Bash、Docker和常见coreutils/flock，创建目录需root/sudo，应用仍以10001运行。可选ADP_BIND_IP/ADP_HOST_PORT沿用现有部署默认；测试使用唯一容器名、独立宿主目录和本机回环端口。三个脚本不自动上传或连接服务器。

# 文档、依赖、部署与发布影响

Docs targeted：README和docs/adp-chat/04_Docker部署与运维.md。无应用依赖升级、业务协议或会话Schema变更。CI保留text-regression身份、权限、安装与测试命令，仅将scripts和构建控制文件纳入路径。远程CI未获push授权，不伪称已运行。

# 完成审计

- [x] upstream_re_read：重读最新用户要求及归档授权。
- [x] change_coverage：逐AC1–AC9核对。
- [x] reverse_audit：由产物、服务器入口和原会话反查实现及证据。
- [x] unresolved_cleared：范围内未解决项清零。

# 完成证据与状态

实现与验证已完成，独立Reviewer两项复审PASS；产品无未解决阻塞项。Delegation Value=MUST_SPLIT，Independence Requirement=REQUIRED；主Agent单写，完成后新建原生隔离只读Reviewer，禁止继续委派。decision_epoch=4。用户已授权本地归档完成Change，覆盖通用GitHub自动归档建议；未授权提交、推送、合并或远程部署。

## 本轮验证与证据边界

- 正式构建：PowerShell 5.1运行 `scripts/build_image.ps1 -Version v0.0.3`，exit0；原始日志dist/.validation/build-v0.0.3.log（工具2aed10/467846）。image_id=sha256:e6e56f2ba33242904c67386611977099ddc6da46a08c284e1e3493a4c9f170f4；配置摘要和归档内容实际匹配。先前v0.0.1/2为本轮验证生成的早期版本包，后人工补齐image_config_id与LF清单并重算SHA256SUMS；v0.0.3完全由最终builder生成，未覆盖用户既有版本。
- 完整回归：Windows Python3.14.7，`.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s tests -v`，99 tests、0 failed、0 skipped、8.540s、exit0；dist/.validation/unittest.log，工具a669b7。包含PowerShell5.1/7真实脚本入口测试，Docker进程边界以受控替身隔离；该单测不冒充真实Docker。
- 镜像验收：`tests/docker_smoke.py --image adp_channel_api:v0.0.2`，exit0，dist/.validation/docker-smoke.log/工具2fc265。实际wheel安装、非root、只读配置、可写持久数据、重建会话保持，真实HTTP→service→session→SSE fixture→HTTP200；不发真实ADP请求。
- Linux公开部署：`tests/deploy_smoke.py --first dist/v0.0.2 --second dist/v0.0.3 --cli-binary dist/.deploy-tools/docker`，exit0，dist/.validation/deploy-smoke.log，工具4f6a1a/dce39d。该次先核对v3无容器引用再rm本轮标签，日志13:05:46.624显示加载并Loaded image v0.0.3，证明真实冷load分支。最终run.sh SHA256=c8833f62405192bef0e2f4f3d42a4880c2573b9e6b57bb547c1ee17176430d19，测试copy此修订后运行。真实Linux目录、版本升/重启、会话字节/CID、锁、同名外来容器保护、损坏包/会话/身份/路径/权限/状态保护、同版本改端口与state/绑定一致、新CMD失败后恢复、旧cache引用保护/无引用删除/离线包保留、绝对路径及空格均PASS。所有fixture和测试容器/故障镜像已清理。
- 独立Review发现两项缺陷后返修：同版本改端口短路；Path.exists吞权限错误误判首次部署。修复前隔离snapshot Red：dist/.validation/port-red.log失败于容器ID未更换；state-red.log失败于PermissionError未抛出。最终真实Linux与99项回归均Green。快照在ignored dist内，正式脚本未回退。
- 静态/读回：PS AST、镜像Bash -n、内嵌Python、测试AST、3sh UTF8/LF、两文档相对链接、三版本完整SHA256及tar/config内容均通过（163152/54c815）。全局已安装PyYAML实际解析CI，对比HEAD确认事件、text-regression job/check、权限、超时、Action revisions及最终测试命令保持（1e8062）；当前任务只扩展scripts/构建控制路径。git diff --check exit0，仅既有跨平台行尾提示。
- 真实数据保护：config.toml SHA256=e7e85db6339f2419f5e90d8ce462891de9e92aafecd0b30a850cdbf1b2a31d3e；runtime/session.json=c3f9fb9a59f28a8ba3ae5e9ec733837d40fa68c158fd001fad6cd70092e92451；logs/adp_chat.log=a84a752362ec6f2ca8d0e5c088edd7e2017d1dd754676c4f680a88616dc0c278，均与任务前baseline一致；未读取或输出秘密正文。
- 适用边界：实际环境为Docker29.6.2 Linux/amd64。classic存储的配置ID兼容有归档内容和单测证据，但未在classic daemon实机；arm64、真实服务器网络、真实ADP和远程CI未执行。用户没有授权push/服务器部署，未以本机健康冒充外部业务可用。

## 独立复审与Parent完成审计

原生隔离只读Reviewer /root/release_review，fork_turns=none，未参与实现或改动文件，未再委派。Base/Head=main/026ed66cd040e3f020d872b224e14c743026180e未提交工作区，epoch4，canonical ffd7ac58a52c7561a99bb31580e970debaa2744d。首轮CHANGES_REQUIRED两项：同版本改端口导致state漂移（MEDIUM）、Path.exists吞权限错误导致状态被当首次部署（HIGH）。主Agent返修并取得隔离旧实现Red、最终99项和真实LinuxGreen后，Reviewer实际重读最终run.sh c8833f...、deploy_smoke c644db...、底层日志及断言，独立检查权限错误投影、AST/LF/文档链接，最终PASS：两个finding关闭，AC1–8证据在本机amd64/受控ADP传输范围充分，无其他blocker。AC9本地归档由Parent执行，不把Review当作移动文件证据。

Parent复核实际diff及最终哈希，重新从用户最新需求反查：四脚本→版本归档→手动上传边界→固定配置/真实持久目录→原会话/失败保护→受管单实例→新镜像验证/优雅替换/真实健康与端口/失败恢复→缓存保留和更早版本清理→文档/用户入口。required层均有相称证据；不改公共HTTP/ADP或session Schema、依赖，没有自动数据迁移。两项Reviewer建议已实际关闭；未将真实服务器/Provider/远程CI或classic/arm64未实机范围冒充通过。

本地Git仍main/026ed66...未提交工作区，既有src迁移和用户暂存历史归档原样保留。用户在本聊天明确要求“已完成的change你自己归档”“其他已完成的change你也自己归档”，因此完成态按本地实施/验证/Review闭环收尾后归档，不需要GitHub自动归档或Git集成；未创建commit、push、PR、merge、生产部署、Issue关闭。此用户授权覆盖通用未集成通常不归档的默认建议，不删除治理门禁。

## 最终归档记录

2026-11-07：主Agent按用户明确授权在本地移动完成；实际位置 .agents/changes/archive/2026-11/CHG-20261107-104512-docker-offline-package/CHANGE.md，原active目录已不存在，文档已重新读取且状态done。归档前Ready检查exit0（6d78df，gated=3/strict=3）。本地归档不代表Git提交、合并、远程CI或生产部署。

归档后再次运行正式Ready检查：exit0，active无剩余Change；归档文档、需求追溯及审计均通过机器检查。

收尾清理已验证：专用dist/.deploy-tools/Linux Docker CLI和4个本轮空Temp目录已删除；无adp-release测试容器；本轮故障/退役测试镜像已清理。保留dist/v0.0.1、v0.0.2、v0.0.3离线产物、3个正式版本标签及ignored验证日志/Red快照供追溯。已刷新本地project-context，未改用户已有暂存归档。最终git diff --check exit0，未执行提交、推送或远程操作。
