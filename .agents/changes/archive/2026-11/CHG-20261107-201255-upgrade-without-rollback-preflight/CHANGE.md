---
schema: coding-change/v1
id: "CHG-20261107-201255-upgrade-without-rollback-preflight"
title: "删除升级前旧镜像兼容预检"
level: L3
status: done
owner: "codex"
branch: "main"
created: 2026-11-07T20:12:55+08:00
updated: 2026-11-07
completion_gate: required
depends_on: []
affected_areas:
  - "deployment"
affected_paths:
  - "scripts/run.sh"
  - "tests/test_deploy_upgrade.py"
  - "README.md"
  - "docs/adp-chat/04_Docker部署与运维.md"
contracts: []
data_changes: []
---

# 目标与范围

用户要求“那就把升级脚本去掉回退预检呗”。目标是在目标镜像接受新配置时，旧镜像不认识 chat.stream 不再阻止升级。修改三个入口共用的 scripts/run.sh；保留目标镜像停机前预检、会话与身份保护、配置哈希保护和实际失败后的回退逻辑。不改镜像、应用配置加载器、CLI 参数、持久数据格式或依赖；不执行服务器部署、Git 提交、推送、合并。

# 事实与方案

基线 main 89cfb4c7b3d58d4e86de1cd5eafca7e005980dcc。实际 v0.0.7 镜像读取含 stream=false 的虚构配置返回 config/unknown_key，v0.0.8 接受；省略 stream 时两者接受，v0.0.8 默认 false。run.sh 在目标预检后、停机前又把 TOOL_IMAGE 切到旧镜像执行 preflight，构成已复现的阻断机制。

比较：保留旧预检并分阶段改配置要求额外人工操作，不符合当前决定；删除旧预检、保留目标校验和失败恢复符合请求；自动保存和恢复旧配置会增加敏感配置状态与管理职责，超出范围。采用第二种。Delegation Value=MUST_SPLIT（只读独立完成审查）；Independence Requirement=REQUIRED；实现仅由主 Agent 写入，Reviewer 不参与实现。

# Requirement Traceability

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | 旧镜像拒绝新配置不再阻止升级 | user:删除升级前回退预检 / AC1 | satisfied | test_new_config_can_upgrade_even_when_old_image_rejects_it：旧实现 config/unknown_key；当前 Windows/Linux 均成功切换 v0.0.8 |
| R2 | 目标预检失败不得停旧容器，实际启动失败仍尝试恢复并正确报错 | user:保留现有功能与本轮授权范围 / AC2 | satisfied | 其余三条升级状态回归在 Windows/Linux 均通过，兼容恢复成功仍非零，不兼容恢复明确失败 |
| R3 | 运维说明同步升级顺序、回退限制及只需上传 run.sh | user:工程规则文档同步 / AC3 | satisfied | README 与运维文档已更新，相对链接实际检查全部存在 |

# Validation Matrix

| 验证层 | 是否要求 | 范围 / 证据 |
| --- | --- | --- |
| 行为 / Unit / Component | required | 四种真实 Bash 主流程状态：正常升级、目标预检失败、兼容回退、不兼容回退；Docker/镜像校验边界为受控替身 |
| 接口 / Contract | not_applicable | 不改变命令参数、应用 API、配置字段或数据格式；升级门槛的明确行为变化由主流程回归覆盖 |
| 集成 / Persistence / Runtime Dependency | required | Windows Git Bash 与 Linux Bash 的主流程、临时文件、真实退出 trap；不宣称替身覆盖 Docker 引擎或服务器 |
| 用户 / Workflow Acceptance | required | 正式 main --upgrade 入口的成功版本、旧容器保持、回退结果和非零退出；使用受控容器边界 |
| 跨组件 Golden Path | not_applicable | 本次不改变镜像实现、Docker 命令或挂载参数；真实服务器升级留待用户上传后验收，不把编排替身称作完整部署 |
| 外部依赖 Probe | not_applicable | 不变更或访问 ADP、服务器或第三方接口 |
| Build / Package / Runtime | required | 三个脚本 Bash 语法与 UTF-8/LF 校验；服务器脚本外置，无需重打 v0.0.8 镜像 |
| Docs / Governance / Other | required | targeted 文档、链接、Change Ready 与独立 Review |

# 任务

- [x] 恢复升级、目标预检和实际失败恢复调用链。
- [x] 新建编排回归；旧代码正确失败：3 fail、1 pass，关键错误 config/unknown_key。
- [x] 删除升级前旧镜像预检，保留失败恢复标志与逻辑。
- [x] 同步 README 与运维文档。
- [x] Windows/Linux 目标验证与相关回归。
- [x] 独立 Review 与 Ready 检查。

# 风险、部署与回退

新镜像实际失败时，旧镜像若不支持当前配置，自动回退仍可能失败；原有明确错误报告保留。服务器配置备份由用户控制，回退旧镜像须同时恢复对应旧配置，保持身份和会话路径。不新增自动备份或格式兼容。上线只替换服务器 release/run.sh（配套 stop.sh/upgrade.sh 可保持当前版本）后重试 bash ./upgrade.sh v0.0.8。撤销本次脚本变更可恢复原预检门槛；不迁移、重建或清空会话。

# 文档影响

Docs Impact=targeted。更新 README 部署说明与 docs/adp-chat/04_Docker部署与运维.md，删除受本次变化失效的分阶段升级要求，解释目标预检与失败后恢复的边界。其他专题、应用配置、依赖、镜像和 CI 无变化。

# Completion Audit

- [x] upstream_re_read：重读用户本轮要求及既有功能不变项。
- [x] change_coverage：R1-R3 均有直接实现、文档与测试证据。
- [x] reverse_audit：upgrade.sh → run.sh main → 目标预检 → 切换 → 成功或真实失败恢复，未删除恢复阶段的旧镜像 preflight。
- [x] unresolved_cleared：需求项无 not_satisfied；服务器部署、Git 操作不在授权范围。

# 完成证据与交付状态

Source 规则锁定 5f406b3ec94faa006b7d5f99bf17fc845c5a97f3，已 fetch 核对 main。当前仅本地修改，绿色验证已完成，独立 Review 已完成且无未解决阻塞项；没有提交、PR、CI、合并或服务器部署。用户预存的未跟踪历史归档保持原样。授权范围通过门禁后本地归档，不代表远程部署成功。

## 当前验证证据

- Windows Git Bash：python -B -X utf8 -m unittest discover -s tests -p test_deploy_upgrade.py -v，exit 0，4 pass。
- 相关回归：python -B -X utf8 -m unittest discover -s tests -p test_docker_package.py -v，exit 0，12 pass。
- D:/Git/bin/bash.exe -n scripts/run.sh scripts/stop.sh scripts/upgrade.sh，exit 0；既有格式测试确认 UTF-8 无 BOM、LF 与内嵌 Python 可编译。
- Linux ARM64 Python 3.14 容器（AMD64 主机模拟执行）：同一 test_deploy_upgrade.py，exit 0，4 pass；只挂载该测试与 run.sh，禁用网络、未挂载配置或 Docker socket。
- git diff --check，exit 0；README 与运维文档相对文件链接均存在。
- 被验证 scripts/run.sh SHA256=61773e6ce21b428685e494d896479c82ef6f5e8fc04bb4a9eac5c2e1419c8d38。编排回归隔离 Docker 及镜像内 helper，不宣称服务器或完整 Docker 升级已验收。

## 独立完成审查与收尾

- Reviewer：/root/review_upgrade_preflight，隔离上下文、只读、未参与实现；Base/HEAD 89cfb4c7b3d58d4e86de1cd5eafca7e005980dcc，decision_epoch=1，最终 NO_FINDINGS_WITHIN_SCOPE。
- F1：运维文档切换顺序残留旧预检阶段，MEDIUM / IN_SCOPE / BLOCKING / AUTO_REPAIR；已定点删除并经 Reviewer 复读关闭。该修复仅改变文档，脚本及测试 hash 未变。
- Reviewer 独立执行 Windows 4 项升级回归、12 项相关打包回归、3 个 Bash 语法检查和 diff --check，全部 exit 0；53 个文档相对文件链接存在。
- Ready Check 已通过：gated=7，strict=7。主 Agent 与 Reviewer 的受限首次测试留下的空临时目录均已核对精确路径、创建时间和空目录状态后非递归清理，确认无残留；正常测试 TemporaryDirectory 与 Linux --rm 容器均已回收。
- 本次授权范围为本地实现、验证和文档同步，已满足。没有新增依赖、配置、数据 Migration、Git 分支、提交、推送、PR、CI 或合并，也未修改服务器；预存用户历史归档保留。服务器实际验收由上传新版 release/run.sh 后重新运行 upgrade.sh 完成，不宣称已部署。
