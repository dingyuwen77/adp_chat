---
schema: coding-change/v1
id: CHG-20261107-000020-src-root-layout
title: src 源码目录与根配置和持久数据统一
level: L3
status: done
owner: dingyuwen77
branch: main
created: 2026-11-07T00:00:20+08:00
updated: 2026-11-07T13:13:24+08:00
completion_gate: required
depends_on: []
affected_areas:
  - 源码组织
  - 配置和会话
  - CLI和PyCharm
  - Docker
  - 测试与文档
affected_paths:
  - src
  - adp_chat
  - main.py
  - config.example.toml
  - pyproject.toml
  - requirements-dev.txt
  - Dockerfile
  - compose.yaml
  - .github/workflows/tests.yml
  - tests
  - docs
  - README.md
contracts:
  - src source root and installed business packages
  - python main.py
  - adp-chat console entry point
  - POST /api/v1/chat
  - GET /healthz
data_changes:
  - config.toml
  - runtime/session.json
  - logs
---

# 变更摘要

统一源码根、配置与运行数据，消除默认配置读取与数据分流。src仅作为源码根，空初始化文件和src模块入口已移除；正式安装、PyCharm单次入口、Docker API、测试和文档使用同一实现。

# 背景、现状与问题

user:src-root-migration：用户要求单层 src 源码，config.toml、logs、runtime 在项目根目录；同步导入、Docker、测试和文档，PyCharm 可直接运行，实际构建并运行容器。

user:keep-root-session：继续现有根目录 runtime/session.json。user:no-backup-cleanup：不备份，不用的数据可删除。保留根目录正式配置、会话和日志的原有字节；两份配置语义已确认相同，删除旧包内重复配置和不用的日志/会话。

本轮只修改当前项目；不提交/推送/合并、不部署远程生产，不调用真实 ADP/MCP。HTTP 路由、请求响应、ADP 协议、身份指纹、会话 Schema、锁、端口和依赖版本保持。

原默认配置位于包内，而本地根配置存在；相对数据路径随配置位置分流。用户要求统一工程结构并保留选定根会话，因此不能仅重命名目录或修改运行参数。

# 事实与证据

| 编号 | 已确认事实 | 来源 | 决策依据 |
| --- | --- | --- | --- |
| E1 | 基线默认包内配置，本地存在两份同内容配置和不同会话 | 基线core/config.py与本轮不打印秘密的解析/哈希检查 | 统一根配置，按用户选择保留根会话 |
| E2 | Windows Python3.14.7和Docker Linux引擎可用 | 实际基线87项测试与docker version | 可在本机取得直接运行证据 |
| E3 | 最新用户明确src及内部目录不需空初始化文件 | 本聊天用户结构澄清 | 正式安装命名空间业务目录，src不作导入前缀 |

## 推断与待确认

无影响当前实施选择的未决业务或数据语义；未运行环境见完成证据的边界说明。

# 目标、成功标准与非目标

## 目标与成功标准

本轮真实用户聊天是上游来源（chat 01a1b17d-4b3d-7052-ba5b-08da0164391f）。AC1–AC6按用户原有要求及后续明确决定的顺序归一为稳定编号，未增加或改变验收语义；当前Change只记录该映射和证据，不作为自己的来源。当前范围没有Issue/PR平台写入或多人需求Owner迁移，未创建平行需求文件。

- [x] AC1：src 非包源码根、业务包安装与正式入口统一
- [x] AC2：根配置和持久目录，跨 CWD 运行
- [x] AC3：原根配置、会话和日志保留
- [x] AC4：不备份，清除旧包与不用的数据
- [x] AC5：实际镜像构建、容器健康和重建持久化
- [x] AC6：测试、CI 路径、Docker 和当前文档同步

## 范围与非目标

仅当前项目的源码、安装、入口、Docker、测试、CI和相关文档。Git提交/推送/合并、生产部署、真实ADP业务调用不在本轮授权内。

## 必须保持不变

HTTP/ADP协议、会话Schema、运行依赖版本与根正式数据字节保持；不用备份，不覆盖用户暂存的历史Change归档。

# 约束与意图决策

| 维度 | 已确认决定 | 依据 |
| --- | --- | --- |
| 数据 | 继续根会话，保留根配置和日志，删除包内弃用数据，无备份 | 用户明确选择与清理授权 |
| 源码 | src为源码根，业务目录采用namespace，无空初始化文件 | 用户最新工程结构要求 |
| 部署 | 单个应用安装；镜像显式绑定/app/config.toml | 使正式安装与外置数据解耦 |
| 兼容 | Python导入/命令显式迁移，HTTP与数据格式不变 | 当前迁移范围 |

# 修改方案与决策依据

## 最小充分方案

user:engineering-source-root：用户进一步明确 src 只作为源码根目录，不是包，不需要 src/__init__.py 或 src/__main__.py；须从系统交付角度采用工程化结构。

采用 pyproject.toml 声明 src 下的命名空间业务目录和 adp-chat 控制台入口。user:no-directory-initializers：用户进一步要求内部目录也不保留空 __init__.py；已核对原初始化文件仅包含说明并移除，构建使用 namespaces=true 和明确业务目录 include 列表。main.py 保留为 PyCharm 单次入口；开发环境通过 requirements-dev.txt 的 editable install 取得稳定导入，PyCharm 同时将 src 标为 Sources Root。不使用临时 sys.path/PYTHONPATH 或更换 CWD 掩盖安装问题。Docker 正式安装应用，显式 ADP_CONFIG_FILE=/app/config.toml 绑定外置根配置；启动 api.app:app。

只改文件夹或保留 src 为包已不能满足最新要求；src/adp_chat 的额外统一包层非本次用户选择，采用当前 adp/api/application/cli/core/storage 业务包直接从 src 安装。新增 setuptools 固定构建后端，仅承担应用打包；现有运行依赖版本保持。

根目录文件记录 SHA-256 后，建立配置路径 Red，迁移导入、入口、构建元数据和消费者；完成安装、真实脚本/命令/镜像/容器验证，最后核对根数据哈希并清除旧包与弃用数据。不创建备份或兼容旧包。先前 src 包方案的 92 个测试和镜像结果仅为中间阶段，不冒充最终结构证据。

## 证据到决策

E1要求配置和持久化归一；E2支持真实进程和镜像验收；E3决定使用声明式源码发现与控制台入口，不引入临时导入路径修改。

## 备选方案与取舍

只改旧运行参数不能满足src根布局；将src本身当包不能满足用户最新要求；src/adp_chat额外统一包层并非本轮用户选择。采用当前业务目录的namespace发现、正式应用安装和根入口，避免新增重复兼容实现。

# 需求追溯

本轮真实用户聊天是上游来源（chat 01a1b17d-4b3d-7052-ba5b-08da0164391f）。AC1–AC6按用户原有要求及后续明确决定的顺序归一为稳定编号，未增加或改变验收语义；当前Change只记录该映射和证据，不作为自己的来源。当前范围没有Issue/PR平台写入或多人需求Owner迁移，未创建平行需求文件。

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | src 非包源码根、业务包安装与正式入口统一 | user:01a1b17d-4b3d-7052-ba5b-08da0164391f#AC1 | satisfied | 见本记录最终执行证据 |
| R2 | 根配置和持久目录，跨 CWD 运行 | user:01a1b17d-4b3d-7052-ba5b-08da0164391f#AC2 | satisfied | 见本记录最终执行证据 |
| R3 | 原根配置、会话和日志保留 | user:01a1b17d-4b3d-7052-ba5b-08da0164391f#AC3 | satisfied | 见本记录最终执行证据 |
| R4 | 不备份，清除旧包与不用的数据 | user:01a1b17d-4b3d-7052-ba5b-08da0164391f#AC4 | satisfied | 见本记录最终执行证据 |
| R5 | 实际镜像构建、容器健康和重建持久化 | user:01a1b17d-4b3d-7052-ba5b-08da0164391f#AC5 | satisfied | 见本记录最终执行证据 |
| R6 | 测试、CI 路径、Docker 和当前文档同步 | user:01a1b17d-4b3d-7052-ba5b-08da0164391f#AC6 | satisfied | 最终92项回归、Docker/Compose运行、98个文档链接、CI YAML及独立Review |

# 计划改动

- [x] 完成路径 Red/Green、源码迁移和调用点同步。
- [x] 完成根数据保留、旧目录清理核对。
- [x] 完成完整 Python/API 和真实脚本启动验证。
- [x] 完成镜像、容器、重建持久化与镜像内容检查。
- [x] 完成文档/CI/配置检查及独立 Review。

对应资产：src/及main.py统一导入，pyproject.toml与requirements-dev.txt声明安装，Dockerfile/compose.yaml声明运行和挂载，tests及当前文档同步，ignored PyCharm模块配置修正内容根。

# 验证矩阵

| Layer | Required | Scope / Evidence |
| --- | --- | --- |
| 行为 / Unit / Component | required | 现有全套回归和配置路径回归；基线 Windows Python 3.14.7：87 tests、0 failed、0 skipped |
| 接口 / Contract | required | 已安装业务包 consumer、CLI、FastAPI 既有协议与 5 个 API 测试 |
| 集成 / Persistence / Runtime Dependency | required | 真实临时文件和会话恢复、正式数据哈希、Docker 只读配置与可写持久挂载 |
| 用户 / Workflow Acceptance | required | main.py 不附加 --config，从另一 CWD 直接运行并跨进程复用会话 |
| 跨组件 Golden Path | required | 实际镜像到 HTTP 健康、非法输入及容器重建恢复；ADP 用受控 fixture 隔离 |
| 外部依赖 Probe | not_applicable | 未修改云端协议，无须真实 ADP 请求或业务写操作 |
| Build / Package / Runtime | required | Docker Linux 实际 build/run、UID/GID、挂载权限、镜像无配置密钥和旧数据 |
| Docs / Governance / Other | required | 链接、配置、CI、Change Ready 与隔离只读 Review |

## 验证计划

本轮完整命令、被验证版本、退出码及范围见新鲜证据；所有required层均已取得相应直接证据，外部云端事实无本轮独立验证责任。

# 风险、兼容性、迁移与回滚

Python 导入路径迁为 src 下安装的业务包，移除根包聚合导出及 src 模块启动方式，统一 adp-chat 控制台入口，仓库消费者同步，无旧包兼容层。配置字段/HTTP/会话 Schema/依赖保持；根正式数据不重建。Compose 将根配置只读挂载到 /app/config.toml，runtime/logs 保持宿主根目录。测试仅用 fixture 数据；不并行操作正式会话。回退旧代码时须显式指定根配置或对应调整旧镜像挂载；不能用代码回退重置会话。用户已明确不备份并允许删除弃用数据。

# 文档、依赖、部署与发布影响

CI 保留 text-regression Job、事件、check 名称、权限、超时及固定 Action revisions；源码路径迁为 src/**，增加 requirements 文件触发和 requirements-dev.txt 安装，保留确定性 unittest，补齐 API 依赖。不新增 Job/发布/Provider Probe。远程 CI 需要用户另行授权提交和推送，本轮检查 YAML 并运行相同的本地命令。

Docs targeted：README、docs 索引、ADP 架构/运维与引用旧代码位置的企微设计。同步根配置、PyCharm 无参数启动、Docker 挂载、升级/回滚约定，不改业务方案。

新增固定setuptools构建后端，运行依赖不变；镜像正式安装而开发环境editable安装。没有生产发布动作，回退代码时必须保持原根数据并按旧入口约定显式挂载配置，不能重建会话代替回滚。

# 完成审计

- [x] upstream_re_read：重读当前用户要求及根会话、不备份决定。
- [x] change_coverage：逐要求核对实现和证据。
- [x] reverse_audit：从入口、配置、数据和镜像反查消费者。
- [x] unresolved_cleared：无未解决阻塞项。

# 完成证据与状态

## 新鲜证据

- 配置路径 Red：`D:/python314/python.exe -B -X utf8 -m unittest discover -s tests -p test_single_turn.py -v`，17 tests、3 个目标路径断言失败（默认包内配置、根配置缺省持久路径、旧模板 ../）；迁移后同范围17/17通过。随后根据用户源码根要求调整安装与导入，不保留中间 src 包方案。
- 正式安装与单测：`D:/python314/python.exe -m pip install --no-deps -e .` 成功；`D:/python314/python.exe -B -X utf8 -m unittest discover -s tests`，Windows Python 3.14.7，92 tests、0 failed、0 skipped、4.178s、exit0。工具执行 session52654/chunks c9c639+e77522。
- 镜像构建：`docker build --progress=plain -t adp-chat:src-layout-check .`，exit0（session21267/chunks d90ba0+f4c476）；固定运行依赖成功安装，正式 wheel安装成功。最终镜像 Id `sha256:1fb27b87dfed7c3a4c98c9a7a21f33d3978876a6271f42a998d58af8a339bb66`；user=adp，CMD=python -m uvicorn api.app:app --host 0.0.0.0 --port 8000 --workers 1。
- 镜像验收：`D:/python314/python.exe -B -X utf8 tests/docker_smoke.py --image adp-chat:src-layout-check`，exit0（chunk6fe442）。安装模块来自site-packages；镜像不含正式配置/旧包/源码持久数据；UID/GID10001；两次默认容器健康200、非法输入422、配置写入EROFS、日志可写；重建session字节相同；第三次真实HTTP→正式业务→会话→SSE fixture→HTTP200。不请求真实ADP，所有测试容器和临时数据清理。
- 实际Compose：临时唯一project，以实际compose.yaml启动，仅覆盖镜像标签、构建上下文和本机随机端口/fixture目录；`compose config --format json`确认根挂载和只读配置/缺文件不自动建目录；`compose up -d --no-build`、执行正式healthcheck命令、HTTP200、`up -d --no-build --force-recreate`、session字节一致；finally `compose down`，确认无项目容器和网络，exit0（chunk5aca57）。未操作目标服务器。
- 根数据：config.toml、runtime/session.json、logs/adp_chat.log 的SHA-256与开工完全相同。生产load_config解析到本项目root/runtime/session.json和root/logs/adp_chat.log，load_session身份匹配且未改写原文件（chunk5916b5）。旧包及源码内配置/日志/会话已移除，没有备份。
- 实际脚本：同一D:/python314/python.exe，从外部CWD执行真实main.py，不传--config，仅输入空行；在配置加载后得到预期input/empty_question，无配置错误、不发ADP（chunk3d1be2）。PyCharm ignored .iml当前内容根=$PROJECT_DIR$，唯一Sources Root=$PROJECT_DIR$/src；移除了误指父目录与重复标记（chunkc1d070）。未点击PyCharm UI运行按钮，不将文件验证冒充UI实测。
- 静态：AST28文件、98个本地Markdown文件链接、pyproject TOML通过；CI YAML通过PyYAML BaseLoader解析，text-regression Job/contents:read/触发/安装步骤核对通过；git diff --check通过（chunks3d1be2/c1d070）。源码无__init__.py、src/__main__.py或src运行数据，正式安装构建产物清除后导入仍成功。
- CI/Review边界：未获Git提交/推送授权，未触发远程CI，保留Job/check身份并执行同样的本地测试；独立只读Reviewer /root/migration_review 已从本轮需求、工作区和原始工具输出完成复核，结论 PASS / NO_FINDINGS_WITHIN_SCOPE。

## 未验证内容与剩余风险

真实腾讯ADP/业务MCP、目标服务器10.102.70.82的网卡/ACL/跨机访问、远程CI及PyCharm UI按钮未执行。它们不被本地fixture、镜像或同解释器脚本结果冒充；本轮不部署或调用生产业务。

## 交付状态

开工 main/026ed66cd040e3f020d872b224e14c743026180e；Agent_Skills ffd7ac58a52c7561a99bb31580e970debaa2744d。基线 87/87 全部通过，无 skipped；初次沙箱内 TestClient 停住已中断，同 Python 本机权限重跑通过。

用户在任务期间将旧 Active Change 移至 archive 并暂存，此用户修改保留，不认领或覆盖旧记录。新任务当前无其他可见 writer。

Delegation Value=MUST_SPLIT（独立审查隔离价值），Independence Requirement=REQUIRED；实现仅主 Agent 写入，完成后新建原生隔离只读 Reviewer，不允许继续委派。

最新需求 epoch=3；主 Agent 已停止沿用 src 包方案，按正式源码根布局重新安装和验证。

当前 main 工作区修改；未创建分支/提交/推送/PR/合并/生产部署；本 Change 未集成不归档。当前本地实施、验证和独立Review已闭环；未提交或集成，不将ready_for_review冒充合并/部署完成。

## 独立 Review 与完成审计结果

- Reviewer：原生隔离只读 /root/migration_review，未参与实现，未修改文件/数据，未再次委派。Base/HEAD=026ed66cd040e3f020d872b224e14c743026180e，decision epoch=3，canonical规则锁定ffd7ac58a52c7561a99bb31580e970debaa2744d。
- 结论：PASS / NO_FINDINGS_WITHIN_SCOPE，无阻塞Finding，无需代码返修。Reviewer实际经read_thread(includeOutputs=true)定位并读取92项回归、完整Docker构建、增强smoke、Compose及根数据/PyCharm校验的底层输出，独立image inspect绑定最终1fb27b87产物。
- A1/A2：当前用户要求、根会话选择、不备份/清理、src及内部目录无空初始化文件、PyCharm根配置与正式安装交付均覆盖；14个非config业务模块除导入外AST与基线相同，HTTP/ADP协议和会话Schema未改变。
- Parent completion audit：重新按真实用户最新要求逐项核对R1-R6，反查main/console/API→安装导入→根配置→原会话/日志与镜像/挂载→消费者/文档；每个required层有对应实际证据，无未解决范围内阻塞项。
- 生成物清理：本任务editable安装生成的src/adp_chat.egg-info及src字节码缓存已清除；安装环境中的正式metadata和源码映射保留，导入与distribution版本读取仍成功。测试容器/网络/临时fixture无残留，没有创建备份。
- Git：保持main工作区，用户归档的旧Change暂存改动原样保留；本任务未创建分支、提交、PR、推送、合并或部署，不归档本Change，不触发远程CI。

最终镜像已添加文档标签 adp-chat:local，image inspect确认与已验证产物1fb27b87完全相同；保留验证标签，不覆盖其他用户镜像。

## 用户后续授权与本地归档

用户在同一聊天后续明确要求主Agent本地归档所有已完成Change，无需GitHub自行归档；本条覆盖上文当时“未集成不归档”的默认收尾状态。该Change已有92项回归、正式镜像、Compose、独立 /root/migration_review PASS 和完整完成审计。后续部署Change未修改src生产代码/Schema/依赖，在当前工作区重新运行99项（0失败/0跳过）及正式adp_channel_api:v0.0.2镜像验收，安装/入口/只读配置/持久数据/受控完整HTTP路径均通过；新部署入口为原Compose的补充，不撤销既有入口。该两份证据保存在dist/.validation/unittest.log和docker-smoke.log。

据此本地实施、验证与独立Review已完成，状态记done并由主Agent本地归档；没有Git提交、推送、PR、合并、远程CI或生产部署，不把本地归档等同集成或发布。用户之前自行暂存的旧single-user-fastapi归档保持原样。最终归档位置和Ready检查见下方收尾记录。

## 最终归档记录

2026-11-07：主Agent按用户明确授权在本地移动完成；实际位置 .agents/changes/archive/2026-11/CHG-20261107-000020-src-root-layout/CHANGE.md，原active目录已不存在，文档已重新读取且状态done。归档前Ready检查exit0（6d78df，gated=3/strict=3）。本地归档不代表Git提交、合并、远程CI或生产部署。

归档后再次运行正式Ready检查：exit0，active无剩余Change；归档文档、需求追溯及审计均通过机器检查。
