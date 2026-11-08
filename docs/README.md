# 技术文档索引

按主题分类；同目录 `NN_` 表示推荐阅读顺序，而不是创建日期。第一次拿到源码，先读 [README.md](../README.md) 的快速上手。

## ADP 单用户对话与服务维护

1. [docs/adp-chat/01_FastAPI服务架构.md](adp-chat/01_FastAPI服务架构.md)：三入口共用的单轮实现、ADP 输出模式、固定 ConversationId、互斥及 HTTP 错误边界。
2. [docs/adp-chat/02_Docker部署与运维.md](adp-chat/02_Docker部署与运维.md)：Python/PyCharm 本地调试、当前配置与迁移、Linux Docker 部署、内网调用、测试、备份和回滚。
3. [docs/adp-chat/03_代码维护导航.md](adp-chat/03_代码维护导航.md)：从入口到 ADP、企业微信和文件状态的源码定位、改动边界及回归入口。

HTTP 请求响应模型以 [src/api/schemas.py](../src/api/schemas.py) 为准；旧高级追问与自定义业务状态实现及其文档已经退役。

## 可执行示例

- [examples/http_client.py](../examples/http_client.py)：Python 标准库请求 `http://10.102.70.82:4560/api/v1/chat`，打印正式结果及关联 ID。
- [examples/http_client.ps1](../examples/http_client.ps1)：Windows PowerShell 远程调用；支持指定服务器地址和本轮文字。

## 企业微信接入（A～E已交付，F真实联调中）

1. [docs/wecom-integration/01_网关架构设计.md](wecom-integration/01_网关架构设计.md)：企业微信回调、身份、同用户忙碌即拒绝、跨用户异步并发、ADP UNKNOWN 释放、独立发送状态。
2. [docs/wecom-integration/02_结构化日志事件.md](wecom-integration/02_结构化日志事件.md)：当前网关JSONL事件、脱敏/轮转/故障边界；未来Cosmos DB仅为日志扩展。
3. [docs/wecom-integration/03_实施路线图.md](wecom-integration/03_实施路线图.md)：A～E 的 HTTP 本地/内网阶段和 F 的真实接入门禁、逐阶段验收。

> 通用单用户 FastAPI 和离线部署脚本已有实现；具体服务器是否通过部署、ADP 连通和跨机访问验收，以该环境的实际结果为准。企业微信加密回调、身份、可靠接收、异步ADP与状态恢复已本地验证；模拟通知与发送状态恢复已验证，正式适配器只有Fake HTTP证据；网关JSONL、本机压力与真实ADP跨进程上下文已验证，Docker完整链路已验证；阶段交付状态见Roadmap；未操作PyCharm GUI，仅保留配置方法。真实企微单员工收发已有现场证据，但阶段 F 尚未完整验收；Cosmos DB 未实施。A～E 的 HTTP 模拟联调不要求公网回调，但不等于企业微信员工实际收发；F 需要额外的外部可达入口与真实应用验收。
