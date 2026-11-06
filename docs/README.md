# 技术文档索引

本目录按**主题**分类；同目录的 `NN_` 表示建议阅读顺序，不表示创建日期。项目当前运行入口及本地验证见 [README.md](../README.md)。

## ADP 对话（现有代码的可选能力）

1. [docs/adp-chat/01_文字追问接口.md](adp-chat/01_文字追问接口.md)：认识高级文字接口的追问、补答、Widget 和兼容边界。**已实现可选接口**；默认 [main.py](../main.py) 一轮退出，不自动等待补答。
2. [docs/adp-chat/02_结构化业务状态.md](adp-chat/02_结构化业务状态.md)：只有 ADP 应用显式约定业务 JSON 时才使用的状态解析与校验。**客户端可选协议已实现**，但真实云端应用是否按约定输出仍需单独验证。

机器可读契约：[docs/interaction.schema.json](interaction.schema.json)（**保持原文件名和路径，不参与人读文档编号**）。业务说明只解释用法，精确机器约束以该 JSON Schema 为准。

## 企业微信接入（设计中，尚未实现）

1. [docs/wecom-integration/01_网关架构设计.md](wecom-integration/01_网关架构设计.md)：先了解企业微信回调、身份与单会话、文件状态、并发、重试、退出应用后的回复，以及现阶段实现边界。
2. [docs/wecom-integration/02_结构化日志事件.md](wecom-integration/02_结构化日志事件.md)：再阅读结构化 JSONL 日志字段、关联 ID、脱敏、保存策略和未来接入 Cosmos DB 的约束。
3. [docs/wecom-integration/03_实施路线图.md](wecom-integration/03_实施路线图.md)：最后按阶段 A～F 开发，包含阶段依赖、测试与验收标准。

> 注意：设计文档不是已交付的运行功能。已实现能力以当前代码、配置和测试为准；企业微信消息网关、相关持久化及 Cosmos DB 日志采集仍未在本项目代码中实现。
