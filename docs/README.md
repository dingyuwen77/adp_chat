# 技术文档阅读索引

本文是文档入口，编号表示**同一目录内推荐阅读顺序**，不是创建日期。根 [README.md](../README.md) 负责快速上手；本文按读者任务组织长期维护知识。全部正式说明均以当前代码、配置和测试为事实源，不将历史 PR 的测试数字当作本轮测试结果。

## 推荐阅读路线

### 首次接手代码

1. [docs/adp-chat/01_FastAPI服务架构.md](adp-chat/01_FastAPI服务架构.md)：单用户脚本、CLI、HTTP 的职责、单轮 ADP 及错误边界。
2. [docs/adp-chat/03_代码维护导航.md](adp-chat/03_代码维护导航.md)：按调用链定位源码、配置、状态文件和回归测试。
3. [docs/wecom-integration/01_网关架构设计.md](wecom-integration/01_网关架构设计.md)：企业微信回调、身份、并发、恢复、发送与安全边界。

### 本地运行与服务器运维

1. [docs/adp-chat/02_Docker部署与运维.md](adp-chat/02_Docker部署与运维.md)：本地 Python/PyCharm、模拟联调、服务器部署、跨机调用、升级、回滚及排障。篇幅较长，按文内章节跳转，不必从头顺读。
2. [examples/http_client.py](../examples/http_client.py) 与 [examples/http_client.ps1](../examples/http_client.ps1)：HTTP 调用示例。
3. [docs/wecom-integration/02_统一日志与采集预留.md](wecom-integration/02_统一日志与采集预留.md)：统一普通文本日志、事件字段、脱敏、轮转及未来 Cosmos DB 采集边界。

### 继续企业微信集成

1. [docs/wecom-integration/03_实施路线图.md](wecom-integration/03_实施路线图.md)：阶段 A～E 已交付证据与 F 真实联调剩余验收。
2. [docs/wecom-integration/01_网关架构设计.md](wecom-integration/01_网关架构设计.md)：理解现有设计和不可盲目重试的边界。
3. [docs/adp-chat/02_Docker部署与运维.md](adp-chat/02_Docker部署与运维.md)：阶段 F 的操作步骤与部署检查。

## 文档职责和维护原则

- 单用户 HTTP Contract 的机器事实在 [src/api/schemas.py](../src/api/schemas.py)、[src/api/routes.py](../src/api/routes.py)；配置字段在 [src/core/config.py](../src/core/config.py)、[config.example.toml](../config.example.toml)。
- 企业微信回调、消息状态、发送及事件日志分别由 [src/api/wecom_routes.py](../src/api/wecom_routes.py)、[src/storage/messages.py](../src/storage/messages.py)、[src/wecom/sender.py](../src/wecom/sender.py)、[src/core/events.py](../src/core/events.py) 负责。
- [docs/wecom-integration/03_实施路线图.md](wecom-integration/03_实施路线图.md) 记录阶段状态与历史验收，不作为当前机器 Contract 的替代品；历史 Change 证据保留在仓库 `.agents/changes/archive/`。
- 修改代码后，先确认哪个文档拥有该事实的解释责任，再更新相应章节和索引；不要把同一字段清单或状态机复制到多篇文档。
- 目录使用小写英文主题名；同目录人工阅读文档使用 `NN_中文主题.md`，从 01 连续；`README.md` 固定名称例外。目录内按阅读依赖排序，不按文件创建时间编号。

## 已确认的范围与限制

当前包含单用户脚本/CLI/FastAPI 和企业微信网关两条运行路径；企业微信 A～E 已交付，F 已有单员工真实收发证据，但多员工真实端到端和公网旧 API 收口等事项仍未全部验收。Cosmos DB 日志上传、附件和群聊不属于当前已实现能力。健康检查成功不等于 ADP 或企业微信真实可用。
