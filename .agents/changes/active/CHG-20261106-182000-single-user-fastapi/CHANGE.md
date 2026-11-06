---
schema: coding-change/v1
id: CHG-20261106-182000-single-user-fastapi
title: 单用户 FastAPI 常驻服务与 Docker 内网部署
level: L3
status: ready_for_review
owner: dingyuwen77
branch: feature/single-user-fastapi-service
created: 2026-11-06T18:20:00+08:00
updated: 2026-11-06T19:24:00+08:00
completion_gate: required
depends_on: []
affected_areas: [ADP对话, HTTP API, CLI, Docker, 文档]
affected_paths: [adp_chat, main.py, requirements.txt, requirements-dev.txt, Dockerfile, compose.yaml, tests, docs, README.md, config.example.toml]
contracts: [POST /api/v1/chat, GET /healthz]
data_changes: [runtime/session.json]
---

# 目标与范围

基于当前 main 将固定 UserId + ConversationId 的 ADP 单轮客户端整理成三种运行入口：单次脚本、交互 CLI 和 FastAPI。Docker 通过内网 10.102.70.82:4560 供其他可信客户端调用。

## 非目标与约束

不新增 CI，不实现 HTTPS、X-API-Key、多用户多会话、消息队列、数据库、文件上传、对外 SSE、HTTP 会话重置、Wheel 打包和旧路径兼容。不自动判定业务结束或重试已可能发出的 ADP 请求。只在可信内网部署，生产服务器本次未经授权不执行部署。

# Requirement Traceability

| ID | Source | 要求 | 状态 | Evidence |
| --- | --- | --- | --- | --- |
| R1 | user:20261106-adp-chat | 单次、CLI、HTTP 三入口复用单轮 ADP | satisfied | tests/test_cli.py, tests/test_api.py；已实现，HTTP API 的完整依赖集成另行验收 |
| R2 | user:20261106-adp-chat | ConversationId 文件持久化、配置统一 | satisfied | tests/test_single_turn.py, tests/test_service.py；CI run #13 |
| R3 | user:20261106-adp-chat | 进程锁与忙碌 HTTP 409 | satisfied | tests/test_service.py；隔离 ASGI tests/test_api.py；CI run #13 |
| R4 | user:20261106-adp-chat | Docker 绑定 10.102.70.82:4560、挂载状态 | explicitly_deferred | compose.yaml, Dockerfile；用户于本轮明确将真实服务器构建和端口验收留待自行执行 |
| R5 | user:20261106-adp-chat | 不新增 CI、不加鉴权及 HTTPS | satisfied | .github/workflows/tests.yml unchanged；docs/adp-chat/04_Docker部署与运维.md |
| R6 | user:20261106-adp-chat | 正式回复内容、异常与脱敏 | satisfied | tests/test_chat.py, tests/test_logging.py；CI run #13 |
| R7 | user:20261106-deployment-guide | 源码可用于环境安装、部署、调试和远程调用，有可运行示例 | satisfied | README.md；docs/adp-chat/04_Docker部署与运维.md；examples/http_client.py；examples/http_client.ps1；tests/test_http_client_example.py |
| R8 | user:20261106-delivery | 实机 Docker、完整 FastAPI 依赖、跨机器 ADP/网络验收由用户合并后自行验证 | explicitly_deferred | 本轮用户明确接受尚未现场验证的范围；本记录不代表通过 |

# Validation Matrix

| Layer | Required | Scope / Evidence |
| --- | --- | --- |
| 行为 / Unit / Component | required | 单轮、会话、互斥和异常测试 |
| 接口 / Contract | required | FastAPI 200/409/422/502/504、完整回复 |
| 集成 / Persistence / Runtime Dependency | required | 真实会话 JSON 文件重启恢复、FastAPI lifespan |
| 用户 / Workflow Acceptance | required | 单次脚本和 CLI 已在 Windows 3.14 回归；独立 HTTP 示例已在同一 CI 的本机模拟服务运行；真实 10.102.70.82:4560 消费者用户延期验收 |
| 跨组件 Golden Path | required | 原有 ADP SSE 模拟流+CLI；隔离 FastAPI ASGI 5/5；完整 ADP/HTTP/Docker 实际链路用户延期验收 |
| 外部依赖 Probe | not_applicable | 本阶段只改本地服务边界，真实 ADP 云端需目标环境单独授权验证 |
| Build / Package / Runtime | required | Windows Python 3.14 CI run #13 通过；Docker build/up 与真实挂载已由用户明确批准延期，不得报告为通过 |
| Docs / Governance / Other | required | README、架构和部署文档，以及无新增 CI 的验证 |

# 任务与验证

- [x] 完成目录、入口和配置路径重构。
- [x] 完成 FastAPI 与全局互斥。
- [x] 完成日志安全与错误响应。
- [x] 完成 Docker 构建配置和持久挂载声明；**真实容器构建与挂载运行待用户验收**。
- [x] 完成已有测试范围、文档同步；独立 AI Review 经用户豁免；完整 HTTP/Docker 实机验证明确延期。
- [ ] 合并后核对 main fresh CI / 项目要求的 Change 归档和分支清理。

# Completion Audit

- [x] upstream_re_read：基于本轮用户最终业务范围及最新合并/验证决定重新核对。
- [x] change_coverage：覆盖三种入口、配置/会话、并发、API、端口、部署/调用文档及示例。
- [x] reverse_audit：检查 HTTP→service→ADP、会话路径、Docker UID/GID、运行示例、失败与重启边界，实机未测试已标明。
- [x] unresolved_cleared：实施范围已覆盖；缺少实机验证是用户明确接受的延期，不视作 Green Evidence。

# Git / 交付

开发分支：feature/single-user-fastapi-service。PR、CI、Review、Merge 与 Archive 以当前 GitHub 实际状态为准，不预先标记成功。

## 2026-11-06 独立审查豁免与剩余验收

- 用户已明确豁免**本次独立 AI Review**，记录为 `USER_WAIVED`；不视为独立评审通过，不豁免其他测试、CI、部署或完成门禁。
- 当前实现 revision `67692b623148706d1bc37246548d838cdd9b8203` 的原有 Windows / Python 3.14 CI run #9：85 tests，0 failed，5 skipped，成功。5 个 FastAPI/HTTPX API 用例因测试依赖未安装而跳过，不能据此认定 HTTP 契约已通过。
- 当前会话执行环境安装有 FastAPI 等依赖，但没有项目源码的本地可执行 checkout、Python 3.14 或 Docker 引擎；未取得完整 API 真实测试与 Docker 构建/容器启动的直接证据。远程 Desktop Commander 离线，无法在目标服务器核验 10.102.70.82:4560 的网络边界。
- 此段为前一轮历史状态；用户随后明确要求合并并自行完成真实服务器验收。现有实现/文档与适用 CI 已复核，但额外运行测试仍须作为**明确延期项**对外披露，不能声称通过。

### 本轮补充验证（仅说明实际覆盖范围）

- 在临时隔离环境（Python 3.13.5，FastAPI 0.128.2，HTTPX 0.28.1，Pydantic 2.13.5）运行 `python -B -X utf8 -m unittest discover -s tests -v`，获得 **5 tests, 0 failures, 0 skipped**。
- 该环境中 `adp_chat/api/app.py`、`api/routes.py`、`api/schemas.py`、`application/chat_service.py` 及 `tests/test_api.py` 均逐文件验证与 PR 对应 GitHub Blob SHA 完全一致；但 `core/config.py`、`core/models.py`、`storage/session.py`、`adp/client.py` 用受控桩代替。因此它是 HTTP ASGI/并发入口的隔离验证，**不能替代 Python 3.14 真实全仓 API 集成测试、ADP Provider Probe 或 Docker/Linux 容器运行**。
- 当前执行容器不具备 Docker CLI / daemon，关联的 Remote Desktop Commander 设备离线，无法形成本任务要求的 Docker build/up/健康/持久挂载和服务端网络 ACL 新鲜证据。

## 本轮文档交付与验证后续（用户批准带风险合并）

- 最新代码和示例更新为 `README.md`、`docs/README.md`、`docs/adp-chat/04_Docker部署与运维.md`、`examples/http_client.py`、`examples/http_client.ps1`；明确 Windows / PyCharm Python 3.14 环境、Linux Docker、UID/GID 10001、宿主 IP `10.102.70.82:4560`、HTTP 调用、错误排查、回滚。
- Windows Python 3.14 现有 CI run #13：`87 tests, 0 failures, 5 skipped`，成功。新增本地 HTTP 示例两项成功；5 个跳过仍为缺少 FastAPI/HTTPX 的 TestClient 用例。此前 Python 3.13.5 隔离 ASGI 测试 5/5，使用桩，证据等级不变。
- 用户已明确把**真实 Docker 构建、服务启动、FastAPI 完整依赖测试、目标服务器 10.102.70.82:4560、Windows 远程与网络 ACL 验收**留在合并之后自行完成。因此仅交付源码与操作指南，不提供生产就绪/部署完成/端到端全通过的断言；发现失败时通过新的修复任务处理，不篡改本次测试事实。
- PR #7 独立 AI Review `USER_WAIVED`（用户已授权）；既有 CI 工作流没有修改或新增。
