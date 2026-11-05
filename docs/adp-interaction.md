# 在 ADP 中明确输出业务状态

默认 [main.py](../main.py) 只转发一条消息，不执行本页业务状态解析，也不需要配置此协议。本页仅适用于显式使用高级 Library 或旧 CLI 的消费者。

本项目保留 ADP 原始 `ReplyMethod`，并提供独立的 `interaction` 契约。`interaction` 是本项目与应用的约定，**不是腾讯云原生字段**。`18` 可以对应信息追问、确认或最终回答；`response.completed` 只代表本轮响应结束。

## 可选高级接口的接入路径

1. 在实际负责收集参数的工作流/Agent 中，明确必填字段、当前已收集值和确认条件。由这个业务节点判定状态，避免客户端根据问句猜测。
2. 在返回给调用方的最终回复节点采用 [docs/interaction.schema.json](interaction.schema.json) 的 JSON 输出。Multi-Agent 有多个 Agent 时，需确保每个面向用户的最终回复分支都满足约定。
3. 发布测试版本，验证实际 SSE 中 `reply` 消息的 `text` 或 `json_text`。组件只解析完整最终回复，不把工具/思考消息当业务输出。
4. 在同一份 `config.toml` 的 `[chat]` 中设置 `structured_interaction = true`。旧 CLI 和直接 Library 调用会尊重该配置；main.py 始终关闭业务解析。高级结构化模式的正常回复必须提供契约；原生 `15` 澄清和未答问卷仍被接受。

腾讯云官方说明 Agent 可选择 JSON 输出并配置 JSON Schema：[Agent 高级设置](https://cloud.tencent.com/document/product/1759/126992)。该说明不等于本项目 Schema 已在你当前控制台配置成功；本轮未修改或发布云端应用。如果控制台只接受部分 Schema 约束，需保留基础字段和枚举，并由客户端进行完整组合校验。

## 输出示例

信息不足时：

```json
{
  "schema": "adp-chat.interaction.v1",
  "interaction": {
    "type": "CLARIFICATION",
    "status": "WAITING_USER",
    "message": "请问要了解差旅报销的哪方面？",
    "missing_fields": ["topic"],
    "confirmation_required": false
  }
}
```

收集完成、返回答案时：

```json
{
  "schema": "adp-chat.interaction.v1",
  "interaction": {
    "type": "ANSWER",
    "status": "COMPLETED",
    "message": "请上传电子发票，再提交报销单。",
    "missing_fields": [],
    "confirmation_required": false
  }
}
```

尽管正文是祈使句，客户端仍按 `ANSWER/COMPLETED` 返回，不索取用户补答。需要用户确认时使用 `CONFIRMATION/WAITING_USER`、`confirmation_required=true` 和空 `missing_fields`；组件只收集调用方提供的回答，不默认同意或执行业务操作。

## 给最终回复节点的约束

可根据实际流程加入以下规则，但提示词本身不等于确定性的状态机：

> 每轮只返回一个符合约定的 JSON 对象，不添加 Markdown 代码围栏。缺少继续任务所必需的信息时，输出 CLARIFICATION / WAITING_USER，并列出本流程真实缺少的字段；需要操作确认时输出 CONFIRMATION / WAITING_USER；已有足够信息并回答当前问题时输出 ANSWER / COMPLETED。建议、FAQ、示例和“是否还需要帮助”仍属于答案内容。missing_fields 只使用本流程真实字段，禁止臆造。用户补答后结合原会话和已收集字段继续判定。

优先通过必填参数和条件分支确定状态，再把节点结果映射到 JSON。只有模型自由判断状态时，仍需验证模型正确性；结构合法不能证明业务语义一定正确。

## 客户端行为与失败处理

- `result.reply_method` 保留原值；显式业务状态在 `result.interaction`，展示正文仍为 `result.text`。
- `WAITING_USER` 才收集答案；`clarification_source="interaction"` 表示业务契约来源，区别于 `text_rule` 推断。
- `structured_interaction=true` 时禁用文字推断。缺字段、类型错误、矛盾组合、重复键、无契约的正常回复等均抛出 `ChatError(code="invalid_interaction")`，不会猜成最终完成。
- `ANSWER/COMPLETED` 与 `15` 或未答问卷冲突时明确报错；同轮不可同时声明业务完成和等待输入。
- 组件暂未定义异步业务任务的 RUNNING/轮询协议。应用只有当前轮完成、追问和确认三个受支持分支，其他状态需另行约定。
- 未配置云端契约时保持 `structured_interaction=false`，可用保守文字识别兼容现有应用；有限规则不能提供任意自然语言零误判保证。

高级文字接口验收需覆盖文字首问、缺少字段、补答后再缺字段、完成、确认、拒绝确认、异常输出和会话延续。客户端单测和构造 SSE 不代表已发布应用已经输出这份契约；默认 main.py 不使用本页判定。
