---
schema: coding-change/v1
id: CHG-20261106-182000-single-user-fastapi
title: 单用户 FastAPI 常驻服务与 Docker 内网部署
level: L3
status: in_progress
owner: dingyuwen77
branch: feature/single-user-fastapi-service
created: 2026-11-06T18:20:00+08:00
updated: 2026-11-06T19:07:00+08:00
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
| R1 | user:20261106-adp-chat | 单次、CLI、HTTP 三入口复用单轮 ADP | not_satisfied | tests/test_cli.py, tests/test_api.py |
| R2 | user:20261106-adp-chat | ConversationId 文件持久化、配置统一 | not_satisfied | tests/test_single_turn.py, tests/test_service.py |
| R3 | user:20261106-adp-chat | 进程锁与忙碌 HTTP 409 | not_satisfied | tests/test_service.py, tests/test_api.py |
| R4 | user:20261106-adp-chat | Docker 绑定 10.102.70.82:4560、挂载状态 | not_satisfied | compose.yaml, Dockerfile |
| R5 | user:20261106-adp-chat | 不新增 CI、不加鉴权及 HTTPS | not_satisfied | .github/workflows/tests.yml, docs/adp-chat/04_Docker部署与运维.md |
| R6 | user:20261106-adp-chat | 正式回复内容、异常与脱敏 | not_satisfied | tests/test_chat.py, tests/test_logging.py |

# Validation Matrix

| Layer | Required | Scope / Evidence |
| --- | --- | --- |
| 行为 / Unit / Component | required | 单轮、会话、互斥和异常测试 |
| 接口 / Contract | required | FastAPI 200/409/422/502/504、完整回复 |
| 集成 / Persistence / Runtime Dependency | required | 真实会话 JSON 文件重启恢复、FastAPI lifespan |
| 用户 / Workflow Acceptance | required | 单次脚本、交互 CLI 和内网 HTTP 消费者 |
| 跨组件 Golden Path | required | 文字提交→SSE→HTTP 结果、配置→Docker |
| 外部依赖 Probe | not_applicable | 本阶段只改本地服务边界，真实 ADP 云端需目标环境单独授权验证 |
| Build / Package / Runtime | required | Python3.14 测试、Docker 构建及启动（工具可用时） |
| Docs / Governance / Other | required | README、架构和部署文档，以及无新增 CI 的验证 |

# 任务与验证

- [ ] 完成目录、入口和配置路径重构。
- [ ] 完成 FastAPI 与全局互斥。
- [ ] 完成日志安全与错误响应。
- [ ] 完成 Docker 和目录挂载。
- [ ] 完成测试、文档同步和 Review。
- [ ] 合并后核对 main fresh CI / 项目要求的 Change 归档和分支清理。

# Completion Audit

- [ ] upstream_re_read：独立重建用户当前要求。
- [ ] change_coverage：逐项核对未遗漏需求。
- [ ] reverse_audit：反向检查调用链、状态、部署和证据层。
- [ ] unresolved_cleared：无未满足的适用需求，剩余现场验证明确报告。

# Git / 交付

开发分支：feature/single-user-fastapi-service。PR、CI、Review、Merge 与 Archive 以当前 GitHub 实际状态为准，不预先标记成功。

## 2026-11-06 独立审查豁免与剩余验收

- 用户已明确豁免**本次独立 AI Review**，记录为 `USER_WAIVED`；不视为独立评审通过，不豁免其他测试、CI、部署或完成门禁。
- 当前实现 revision `67692b623148706d1bc37246548d838cdd9b8203` 的原有 Windows / Python 3.14 CI run #9：85 tests，0 failed，5 skipped，成功。5 个 FastAPI/HTTPX API 用例因测试依赖未安装而跳过，不能据此认定 HTTP 契约已通过。
- 当前会话执行环境安装有 FastAPI 等依赖，但没有项目源码的本地可执行 checkout、Python 3.14 或 Docker 引擎；未取得完整 API 真实测试与 Docker 构建/容器启动的直接证据。远程 Desktop Commander 离线，无法在目标服务器核验 10.102.70.82:4560 的网络边界。
- 当前状态仍为 `in_progress`、完成门禁尚未达到 `ready_for_review`。仅豁免独立 Review 不自动解除缺失的必要验证。

### 本轮补充验证（仅说明实际覆盖范围）

- 在临时隔离环境（Python 3.13.5，FastAPI 0.128.2，HTTPX 0.28.1，Pydantic 2.13.5）运行 `python -B -X utf8 -m unittest discover -s tests -v`，获得 **5 tests, 0 failures, 0 skipped**。
- 该环境中 `adp_chat/api/app.py`、`api/routes.py`、`api/schemas.py`、`application/chat_service.py` 及 `tests/test_api.py` 均逐文件验证与 PR 对应 GitHub Blob SHA 完全一致；但 `core/config.py`、`core/models.py`、`storage/session.py`、`adp/client.py` 用受控桩代替。因此它是 HTTP ASGI/并发入口的隔离验证，**不能替代 Python 3.14 真实全仓 API 集成测试、ADP Provider Probe 或 Docker/Linux 容器运行**。
- 当前执行容器不具备 Docker CLI / daemon，关联的 Remote Desktop Commander 设备离线，无法形成本任务要求的 Docker build/up/健康/持久挂载和服务端网络 ACL 新鲜证据。
