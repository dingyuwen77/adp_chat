---
schema: coding-change/v1
id: CHG-20261107-143221-readable-session-config
title: 会话文件组读权限与两份配置同步
level: L3
status: done
owner: dingyuwen77
branch: main
created: 2026-11-07T14:32:21+08:00
updated: 2026-11-07T14:46:26+08:00
completion_gate: required
depends_on: []
affected_areas:
  - 会话权限与既有会话恢复
  - 本地配置与问题日志
affected_paths:
  - src/storage/session.py
  - config.toml
  - config.example.toml
  - tests/test_single_turn.py
  - tests/session_permissions_smoke.py
  - docs/adp-chat/04_Docker部署与运维.md
contracts:
  - load_session
  - logging.log_question
data_changes: []
---

# 变更摘要与上游要求

用户明确要求改变session.json权限以允许服务器部署账户查看，在本地config.toml和config.example.toml补充log_question，其他配置对齐且config.toml现有值必须保留。沿用本聊天本地归档授权，无Git提交/推送/服务器连接或生产ADP授权。canonical已fetch并锁定ffd7ac58a52c7561a99bb31580e970debaa2744d，main/base026ed66cd040e3f020d872b224e14c743026180e，保护既有dirty工作区及历史archive。

已实际只输出键名核对两份TOML：共有字段完全相同，仅adp.app_key不同，两份均缺log_question。读取真实值只在本地内存用于前后断言，不打印/存入日志、测试或Change。真实runtime/session.json内容hashc3f9fb9a59f28a8ba3ae5e9ec733837d40fa68c158fd001fad6cd70092e92451、logs/adp_chat.log=fea40a9ef88ee4246deeb4f2f990f7f1fdf37f29a48f8bfff22efa9352613053为本轮保护基线。

# 目标与方案

AC1：新建/显式重建POSIX会话文件640，应用仍10001拥有写权；适用时属组匹配数据目录组，普通部署用户组可直接读取。
AC2：已有有效同身份普通会话文件只修正owned元数据，CID/JSON字节不变；先完成JSON/身份校验再修权限，不以重建绕过错误；非本进程所有的可读文件不强制夺取属主。
AC3：本地两份配置只新增logging.log_question=true，保留所有原有解析值，包括真实app_key；模板AppKey保持空，其他值和字段一致，修正误导注释。
AC4：真实Linux UID10001创建/恢复、UID1000直接读取、组读不可写及错误/原子保存保护有证据；问答日志沿用原实现/脱敏，配置显式启用不改代码默认。
AC5：同步现有运维Owner，完整回归、ARM64镜像及隔离角色验收、独立Review、Ready和本地归档闭环。

备选比较：644会向其他账户开放会话身份；仅手工chmod会在下次创建回到600；改应用UID影响既有数据。选择仅同组可读640、保持UID/内容契约，owned文件可匹配可加入的目录组。Windows不模拟POSIX chmod，保留现有NTFS行为；部署脚本补充目录组机制沿用已验证实现，无新权限服务/ACL依赖。真实服务器由用户上传新镜像后验收。

# 需求追溯

| 编号 | 要求 | 来源 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| R1 | 会话同组可读与应用可写 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2/readable-session-config#AC1 | satisfied | 新ARM64镜像角色测试：应用10001创建640/目录组1000，部署用户1000直接读且不能写PASS |
| R2 | 旧会话不重建且异常失败关闭 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2/readable-session-config#AC2 | satisfied | 旧600仅改元数据，CID/字节保持；无效身份/数据不先开放，foreign owner不夺权，失败保存不覆盖PASS |
| R3 | 两配置补字段并保留现有值 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2/readable-session-config#AC3 | satisfied | 两文件TOML逐原值比对+当前parity通过；逆本轮片段的内存SHA还原原完整配置e7e85...，密钥未打印 |
| R4 | 实际角色/日志/保存回归 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2/readable-session-config#AC4 | satisfied | 正确Red→角色Green、目标18项及完整101项exit0、ARM64真实镜像HTTP/会话恢复PASS |
| R5 | 文档、Review和本地收尾 | user:01a1b3cf-c72e-7022-b6d3-6165f043e5a2/readable-session-config#AC5 | satisfied | 运维Owner同步、隔离只读Reviewer PASS、Ready/本地归档按用户授权执行 |

# 验证矩阵

| Layer | Required | Scope / Evidence |
| --- | --- | --- |
| 行为 / Contract | required | 模板显式问题日志、会话JSON/CID/错误/原子保存不变 |
| 集成 / Persistence / Runtime Dependency | required | 真实Linux角色UID10001与1000/补充组、640元数据、旧600迁移仅权限 |
| 用户 / Workflow Acceptance | required | 部署身份直接读取session、配置只增加字段、问题日志受配置控制 |
| 跨组件 Golden Path | required | 镜像HTTP健康/会话重建复用/受控SSE回复；不请求真实ADP |
| 外部依赖 Probe | not_applicable | 不改云端，不连接服务器 |
| Build / Package / Runtime | required | 默认ARM64新版本真实构建/归档/模拟运行，原角色测试不可用源码冒充 |
| Docs / Governance / Other | required | 文档Owner、秘密保护、TOML前后语义对照、独立Review/Ready/归档 |

# 风险与兼容

不改会话Schema、HTTP/ADP、依赖或默认镜像架构。只调整POSIX owned普通会话文件权限与可加入目录组，既有JSON和CID保留；失败明确ChatError且不发业务。配置启用后未来用户问题会进入脱敏日志，这是本次明确需求；原代码log_question默认false与显式false保护继续。config.toml不入Git或Docker。权限代码需新镜像生效；服务器release配置仍保留其../路径，不能拿本地根配置盲目覆盖。

# 完成审计

- [x] upstream_re_read：重读用户本轮原文和配置值保留要求。
- [x] change_coverage：逐AC反查。
- [x] reverse_audit：文件创建/恢复→应用权限→部署用户直接读取，配置→日志。
- [x] unresolved_cleared：无未解决阻塞。

# 完成证据与状态

实施前契约。只做本地修改和隔离fixture验收；不读取真实会话内容，不请求ADP，不覆盖用户其他工作，不提交/推送。完成后按当前用户既有授权由主Agent本地归档。

## 实际验证与版本

- 配置先行核验：修改前两份解析值只有adp.app_key差异，均缺log_question。写入时在同一进程逐值assert after==before+logging.log_question=True，包含AppKey原值不变；template key仍空；未打印或备份任何真实值。之后只在内存逆转新增开关/注释，完整文件SHA精确还原先前原config.toml的e7e85db6339f2419f5e90d8ce462891de9e92aafecd0b30a850cdbf1b2a31d3e，直接证明所有原值保留；当前两文件除AppKey外一致。忽略的正式config仍不入Git/Docker。
- Red：dist/.validation/session-config-red.log真实模板加载log_question=False而assertTrue失败；session-permission-red.log在正式旧ARM64v0.0.5镜像的真实Linux角色进程中，实际mode600不等于要求640失败（a1aa00）。没有用环境失败冒充目标Red。
- Green：目标SingleTurn18项通过；完整`.venv/Scripts/python.exe -B -X utf8 -m unittest discover -s tests -v`最终101tests/0失败/0跳过/9.197s/UNITTEST_EXIT=0（ce1cfd及后续session结束），日志readable-session-unittest.log及readable-session-unittest.exit=0。第一次完整OK日志外层返回字段未保留，未编造该次退出码，随后仅为补齐该证据缺口重新取得明确0。
- 默认ARM64真实构建：scripts/build_image.ps1仅-Version v0.0.6，exit0（43cd5c），dist/.validation/session-permission-build.log；新包dist/v0.0.6/adp_channel_api-v0.0.6-linux-arm64.tar，manifest/SHA256SUMS均实际读回通过，配置digest与真实tar内容一致且architecturearm64。imageID sha256:867b784ec503464824eee1e61cc26467167e7a01f253cd4981486a99719ca524。未覆盖旧版本，不修改运行依赖或Dockerfile。
- 新角色验收：tests/session_permissions_smoke.py --image adp_channel_api:v0.0.6，exit0（fbd569），dist/.validation/session-permission-green.log。容器内纯Linux文件系统目录owner1000/group1000/mode770且故意不设setgid；正式storage在真实进程10001/主gid10001/补充1000创建文件640并匹配目录组；用户1000直接read_bytes与原始JSON一致，open写拒绝。旧文件600/group10001恢复后CID及字节不变；无效JSON/身份不先改权限；foreign owner文件不夺权；权限设置故障时原子保存不覆盖原会话、无残留temp；显式new后仍可读。无Docker socket/网络/真实数据，测试容器与fixture清理。
- 正式镜像集成：tests/docker_smoke.py --image adp_channel_api:v0.0.6，exit0（ea77ee），readable-session-image-smoke.log。实际安装、UID/GID10001、只读配置/可写持久目录、两次health200/非法输入422/session字节保持、真实HTTP→service→session→受控SSE→HTTP200均PASS，无真实ADP；fixture/容器清理。
- AST、TOML/两文档实际链接、git diff --check通过。最终storage SHA256 c88aee7075ed5c532d3f2ad61c9c551aa0b6c75c7060eaea08919f59cb2b80a1；角色测试faf0ec4a3e8704e07377e04a9e368f8af097db60714f7a6c5d112f7497cab3e9；template6e7237cfa0d436299f06b33fa78d81dbb50a5073e14b550eba450c0a3999518a。真实runtime/session.json及logs/adp_chat.log内容hash与开工基线相同。

## 独立Review与Parent审计

Reviewer /root/readable_session_review原生新建、上下文隔离、只读且未参与实现/再委派。独立从用户本轮原文恢复AC1–5，读取canonical、storage/调用端/角色测试/模板和文档，实际读完整Green及正确Red，程序内存比较两TOML只输出布尔不展示真实值，核对最终storage SHA。结论PASS / NO_FINDINGS_WITHIN_SCOPE，无阻塞Finding。身份/数据校验→owned元数据修复、新临时文件完整flush→640/目录组→fsync→replace与失败cleanup均闭环；原run.sh实际补充目录组机制与用户组读兼容。

Parent逐用户原文、配置原值和file lifecycle反查实现/Evidence，所有AC与required层满足，未把程序默认false改为全局true，只在两份明确配置新增开关。会话schema/HTTP/ADP/依赖/既有CID均不改；Windows不执行POSIX元数据调整。当前ARM64为本机DockerDesktop模拟运行，目标服务器原生、真实ADP及远程CI未运行，不冒充完成；没有push授权，也没有CI配置变化。没有commit/push/PR/merge/生产数据动作。本地完成后沿用用户明确本地归档授权；归档不代表集成或发布。

## 最终归档与清理

主Agent按同聊天明确授权本地移动完成到.archive/2026-11/CHG-20261107-143221-readable-session-config（正式路径为.agents/changes/archive/2026-11/CHG-20261107-143221-readable-session-config/CHANGE.md），原active目录已不存在，文档已读回为done；归档前Ready通过。测试容器/fixture已清理；保留新ARM64 v0.0.6包与ignored原始验证日志供用户上传/追溯。无secret备份、提交、推送或服务器操作。
