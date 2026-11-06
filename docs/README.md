# 技术文档索引

按主题分类；同目录 `NN_` 表示推荐阅读顺序，而不是创建日期。第一次拿到源码，先读 [README.md](../README.md) 的快速上手。

## ADP 单用户对话

1. [docs/adp-chat/01_文字追问接口.md](adp-chat/01_文字追问接口.md)：可选高级文字追问接口、Widget 和补答的协议能力；三种默认入口都只调用一轮，不自动补答。
2. [docs/adp-chat/02_结构化业务状态.md](adp-chat/02_结构化业务状态.md)：可选结构化业务状态解析；必须由 ADP 应用显式遵守协议才有意义，默认单轮入口不启用。
3. [docs/adp-chat/03_FastAPI服务架构.md](adp-chat/03_FastAPI服务架构.md)：固定 ConversationId、全局锁、HTTP 状态及错误边界。
4. [docs/adp-chat/04_Docker部署与运维.md](adp-chat/04_Docker部署与运维.md)：Python/PyCharm 本地调试、Linux 环境准备、Docker 部署、内网调用、测试、备份和回滚。

已有机器可读协议：[docs/interaction.schema.json](interaction.schema.json)。真正的 HTTP 请求响应模型以 [adp_chat/api/schemas.py](../adp_chat/api/schemas.py) 为准；不要把历史结构化业务协议误当作 FastAPI 输入格式。

## 可执行示例

- [examples/http_client.py](../examples/http_client.py)：Python 标准库请求 `http://10.102.70.82:4560/api/v1/chat`，打印正式结果及关联 ID。
- [examples/http_client.ps1](../examples/http_client.ps1)：Windows PowerShell 远程调用；支持指定服务器地址和本轮文字。

## 企业微信接入（设计中，尚未实现）

1. [docs/wecom-integration/01_网关架构设计.md](wecom-integration/01_网关架构设计.md)：企业微信回调、身份、消息生命周期及待实现边界。
2. [docs/wecom-integration/02_结构化日志事件.md](wecom-integration/02_结构化日志事件.md)：网关 JSONL/Cosmos DB 日志设计，尚非已部署能力。
3. [docs/wecom-integration/03_实施路线图.md](wecom-integration/03_实施路线图.md)：企微网关后续开发阶段。

> 通用单用户 FastAPI 已有源码，但 **Linux 目标服务器的部署和跨网卡验收不应视为已完成**；企微网关、队列、Cosmos DB 等仍是未来设计。
