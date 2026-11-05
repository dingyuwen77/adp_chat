# 腾讯云 ADP Multi-Agent 对话组件

`adp_chat` 提供单轮函数 `chat()`、自动处理追问的 `chat_until_complete()`，以及终端入口。三者共用文本、图片和文件请求链路。

推荐由 ADP 应用输出独立的业务交互状态，组件严格校验后决定是否等待补答，保留原始 `ReplyMethod`。当前云端尚未配置该契约，因此默认保持兼容：正式澄清/问卷优先，`18` 类回复再用保守文字规则。真实文本联调已验证“18 类追问 → 补充答案 → 正常回复停止”，但有限规则不能保证任意自然语言零误判。

## 安装和配置

在项目根目录使用 Python 3.14 执行。只做文本对话时安装基础包，使用附件时安装带附件依赖的包。

```powershell
python -m pip install .
python -m pip install -c requirements.txt ".[attachments]"
```

首次使用时，将 [config.example.toml](config.example.toml) 复制为 `config.toml`，已有本机配置时直接编辑。

- `[adp]` 填写已发布的 Multi-Agent 应用 AppKey 和本地用户标识 `user_id`。留空的 `visitor_id` 使用 `user_id`。
- `[chat].detect_text_clarification` 默认 `true`，启用 `18` 类文字追问识别；设为 `false` 时仅依据正式协议。已有配置省略此项也会默认启用，不必改动凭据。
- 应用已配置业务 JSON 契约后，设置 `[chat].structured_interaction = true`：严格按业务状态处理，停止文字猜测；缺失或损坏的输出直接报错。[docs/adp-interaction.md](docs/adp-interaction.md) 提供配置步骤、Schema 和节点约束。
- `[attachments]` 填写同一应用的 `bot_biz_id`、地域，以及有权申请 ADP COS 临时凭证的腾讯云 `secret_id` 和 `secret_key`。纯文本聊天可保留这些凭据为空。
- 图片按 ADP 对话 COS 规则上传为公开可读。只有确认图片可以公开时，才把 `allow_public_image_upload` 设为 `true`。文档使用私有上传和临时下载链接。
- 真实凭据保存在本机 `config.toml`。分享项目时使用配置模板。

## 在 Python 中调用

下面三种示例可以分别执行。每个示例从首问开始，检测到正式澄清或明确的文字追问才读取补充答案，收到普通答案便返回。直接运行项目根目录的 [main.py](main.py)：

```powershell
python main.py
```

在 [main.py](main.py) 中，`example_text_chat()`、`example_image_chat()`、`example_file_chat()` 分别对应普通、图片和文件问答。当前 `main()` 只启用了普通问答；图片和文件调用可按需取消注释，每个示例各开一个会话。该脚本基于自身位置读取项目配置和样例，可直接在 PyCharm 中运行。

### 普通问答

```python
from adp_chat import chat_until_complete, load_config
from adp_chat.console import read_clarification, show_result

config = load_config("config.toml")
result = chat_until_complete(
    "你好，请介绍一下自己", config=config,
    answer_provider=read_clarification, on_result=show_result,
)
```

### 图片问答

使用前安装附件依赖，填写 `[attachments]` 凭据，并按前述说明允许公开图片上传。

```python
from adp_chat import chat_until_complete, load_config
from adp_chat.console import read_clarification, show_result

config = load_config("config.toml")
result = chat_until_complete(
    "请描述这张图片",
    config=config,
    image="examples/小楷.jpeg",
    answer_provider=read_clarification, on_result=show_result,
)
```

### 文件问答

使用前安装附件依赖，填写 `[attachments]` 凭据。

```python
from adp_chat import chat_until_complete, load_config
from adp_chat.console import read_clarification, show_result

config = load_config("config.toml")
result = chat_until_complete(
    "请总结这首诗",
    config=config,
    file="examples/致橡树.txt",
    answer_provider=read_clarification, on_result=show_result,
)
```

### 在其他 Python 程序中处理追问

底层 `chat()` 只发起一次请求，不读取终端输入，适合接入 Web、业务函数或后续测试用例。保留上一轮结果，用 `reply_to` 作答；组件复用会话，并自动标记本次输入的用途。附件在首轮传入即可。

```python
from adp_chat import chat, load_config

config = load_config("config.toml")
result = chat("我想了解差旅报销", config=config)
if result.needs_input:
    # 在你的界面展示 result.text / questionnaires / widgets，再获取真实补充答案。
    answer = "我想了解 Concur 系统操作流程"
    result = chat(answer, config=config, reply_to=result)
```

`chat_until_complete()` 用 `answer_provider(result)` 获取真实补充答案，用 `on_result(result)` 交付每轮正式回复；这两个回调都由调用方提供。只有 `needs_input=True` 才调用答案提供器。多次追问会依次作答，附件不会重复上传。回调在调用线程执行，抛错即停止，调用方可据此取消或限制次数。组件不生成补充答案。

`ChatResult` 新增字段：

| 字段 | 含义 |
| --- | --- |
| `needs_input` | 是否检测到正式等待状态或符合保守规则的文字追问；`False` 不代表任意措辞都已被理解 |
| `clarification_source` | `interaction`、`reply_method`、`questionnaire` 或 `text_rule`；未识别到追问时为 `None` |
| `interaction` | 应用明确给出的业务类型、状态、正文、缺失字段和确认要求；兼容模式为 `None` |
| `reply_method` | ADP 返回的正式回复方式，缺失时为 `None` |
| `input_kind` | 本轮发送的是 `question`，或对已识别追问的 `clarification_answer` |
| `original_question` | 本题首问，用于跨补答保留任务语境；不会在默认 `repr` 中输出 |
| `questionnaires` | 尚未作答的问卷题目和选项；已填题目不重复收集 |
| `widgets` | 当前回复中的 Widget 原始数据，供界面或适配器使用 |

`input_kind` 根据 `reply_to.needs_input` 确定。用 `reply_to` 回答追问时保留 `original_question`，避免把翻译任务的正常译文当成新追问；普通新问题会重新设置首问。只传 `conversation_id` 能续接会话，无法恢复此前的追问状态和首问语境。主动延续已完成的回答也可传 `reply_to=result`。同一会话按顺序调用，每轮请求 ID 独立。

终端适配器支持文字作答和问卷单选、多选；问卷答案按“题目：所选内容”汇总为文本提交。自定义 Widget 需根据实际卡片事件填写动作 JSON，终端不渲染卡片。Python 调用方可返回 `ClarificationAnswer(widget_actions=(action,))`；`action` 必须包含当前卡片的 `WidgetId`、`WidgetRunId`、真实的 `ActionType` 和字符串 `Payload`。组件按正式 `widget_action` 结构发送，具体动作语义由卡片定义，尚未经过当前应用的真实 Widget 联调。

上面的独立代码示例使用相对路径，基于调用程序的当前工作目录解析。其他项目可传绝对路径。调用失败会抛出 `ChatError`，按需读取它的 `stage`、`code` 和 `request_id`。

每次单轮调用返回 `ChatResult`，包含本轮正式文本 `text`、会话 ID、请求 ID，以及服务端返回时可用的 `record_id`。省略 `conversation_id` 和 `reply_to` 会新开会话。成功响应可能只有非文本内容，此时 `text` 为空字符串。`chat_until_complete()` 返回最后一轮结果；需要保存整个过程时，在 `on_result` 中收集各轮结果。

可传入 `on_preview` 回调接收 `PreviewEvent`。用 `(message_id, content_index)` 定位片段，`delta` 追加，`replace` 覆盖。回调在调用线程同步执行，最终以 `ChatResult.text` 为准。调用方回调抛错时，流会关闭并将该异常交还调用方。

## 在终端中使用

```powershell
python -m adp_chat
python -m adp_chat --ask "你好"
python -m adp_chat --file ".\examples\致橡树.txt" --ask "请总结这首诗"
python -m adp_chat --image ".\examples\小楷.jpeg" --ask "请描述这张图片"
```

CLI 默认读取当前目录的配置。可用 `--config` 指定其他路径，或用 `--conversation-id` 续接已知会话。交互模式可输入 `/file "路径"` 或 `/image "路径"`，随后输入问题。用 `/new` 开始新会话，用 `/exit` 退出。配置中的 `first_question` 可设置启动时发送的问题。

`--ask` 完成本题及已识别追问后退出。每轮正式文本走标准输出，预览、补充答案提示和错误走标准错误。未识别到追问时不会再为本题读取答案；普通交互模式会继续等待下一道问题。追问时 `/exit` 或输入结束会以非零退出码结束，表示当前题目尚未完成。单题命令发生错误返回非零；普通交互模式遇到其他 `ChatError` 会显示错误并继续等待问题。管道输入输出使用 UTF-8。

PyCharm 新建运行配置时，选择 Python 模块 `adp_chat`，工作目录设为项目根目录。

## 接入和排障

### 文件日志

默认将调试日志写入 `logs/adp_chat.log`，每份约 5 MiB，保留 3 份轮转备份，编码为 UTF-8。相对路径基于配置文件所在目录；直接构造 `ChatConfig` 时基于工作目录。

```toml
[logging]
log_enabled = true
log_level = "DEBUG"
log_file = "logs/adp_chat.log"
log_max_bytes = 5242880
log_backup_count = 3
# 问题完整保留，回答在脱敏后最多保留100个Unicode字符；0关闭回答正文。
log_answer_chars = 100
```

记录格式为 `[北京时间 YYYY-MM-DD HH:mm:ss.SSS 源文件 L行号] [级别] 事件`，文件实际格式示例是 `[2026-11-05 14:00:00.000 client.py L100] [INFO] request_started ...`（仅格式示例）。记录包括会话/请求/服务端关联标识、附件阶段、SSE 事件数量、原始回复方式、判定来源、业务状态、耗时和错误分类。按 `request_id` 或 `conversation_id` 搜索即可串起一轮或整个会话。

日志只写文件，组件不安装控制台日志处理器，也不改变宿主 root logger。每轮 `user_question` 完整记录本次用户问题（包括补答）；`adp_answer` 默认记录回答前100个Unicode字符，同时标明原文长度和是否截断。失败时仍有问题记录，没有成功回答就不伪造回答日志。问答先做已知密钥及签名/令牌URL脱敏，再截取和JSON转义，换行以 `\\n` 等形式保留在同一条物理日志中。

组件不额外记录完整API请求、配置对象或自动生成的附件签名链接；已知本机密钥在格式化出口再次脱敏。正常聊天输出由原有入口决定。日志文件启动时不可写会在请求前报 `logging/unwritable`；运行中磁盘/权限变化导致的写入失败采用尽力记录，不把错误转写控制台，也不触发业务重试。轮转锁只覆盖单个进程，多个进程不应共用同一轮转文件。

### `provider_error` 与并发限制

`provider_error` 表示服务端通过 SSE 返回了错误事件，实际原因查看日志中的 `provider_code`。`460701` 的官方定义为“专属模型超出并发数限制”，代码会直接显示这个错误码及原因。[官方错误码](https://cloud.tencent.com/document/product/1759/129202)

应先等待现有请求结束，核对同一专属模型是否有其他客户端或任务占用，再核查云端并发配额。本地日志不能确定云端实际并发数和占用来源。组件不会自动重试，也不会修改云端配额；改进错误提示不等于已解除云端限制。

### 推荐使用明确业务状态

`ReplyMethod` 表示回复方式，`interaction` 表示业务交互。开启结构化模式后，例如 `18 + CLARIFICATION/WAITING_USER` 会继续取答案；`18 + ANSWER/COMPLETED` 会返回，即使正文含问号或“请上传发票”等操作说明。`CONFIRMATION/WAITING_USER` 单独表达确认要求，组件不默认同意。

此业务契约由应用/工作流生成，本项目只负责校验与消费。客户端不能通过给文字猜测套 JSON 获得确定性。详见 [docs/adp-interaction.md](docs/adp-interaction.md) 和 [docs/interaction.schema.json](docs/interaction.schema.json)。目前只验证了本地契约、模拟 SSE 和入口接线，未配置、发布或验收真实云端的结构化输出。

### 核对 ADP 澄清设置

截至 2026-11-05 查询的 [HTTP SSE V2 文档](https://cloud.tencent.com/document/product/1759/129202)（2026-09-18 更新）说明：`ReplyMethod=15` 是澄清确认回复，`reply` 消息中的 `questionnaire` 用于等待用户作答。这些正式状态优先于本地文字推断；普通卡片、推荐问题和单个问号不会单独触发输入。

当前应用曾以 `18` 返回“想了解报销的哪方面”。现在组件会结合本题首问和整段正式回复，在直接信息请求、选择询问等常见句式中推断等待作答。识别逻辑位于 [adp_chat/clarification.py](adp_chat/clarification.py)，不新增模型或分类请求，`chat()` 仍然只请求一轮。

为减少误判，规则排除引用、代码、FAQ、正常操作步骤、材料要求、可选帮助邀请，以及生成/翻译作品的内容；回复包含独立答案或步骤时采用保守停止。比如“请提供完整报错”可触发，“请上传发票，再提交报销单”“您需要我继续介绍吗”不会触发。不明确、未覆盖的句式可能漏判；当前规则主要覆盖已测试的中文句式和少量英语句式。调用方可查看 `clarification_source` 区分平台状态与本地推断，或关闭文字推断。

对于需要严格状态契约的接入，仍建议在相关 Agent 核对并发布“澄清询问”和“上下文轮数”，用业务问题验证正式标记和上下文。官方文档说明“澄清询问”默认关闭；本轮没有读取控制台，不能确定应用当前开关。详见 [Agent 高级设置](https://cloud.tencent.com/document/product/1759/126992) 和 [澄清询问 Widget](https://cloud.tencent.com/document/product/1759/127034)。

### 请求和附件

公共调用逻辑在 [adp_chat/client.py](adp_chat/client.py)。文档由 [adp_chat/attachments.py](adp_chat/attachments.py) 上传到 ADP COS。组件用单字节范围请求检查临时下载链接，随后把链接交给 Multi-Agent 的 V2 对话接口。图片按公开上传方式把对象 URL 交给 V2 接口。对话只有收到成功的 `response.completed` 才算成功。 [腾讯云 HTTP SSE V2](https://cloud.tencent.com/document/product/1759/129202) · [附件接入指南](https://cloud.tencent.com/document/product/1759/107908)

- `config`、`input` 错误先检查本机配置、文件路径或扩展名。首版接收常用文档和数据文件，以及 JPG、JPEG、PNG、BMP、WebP 图片。文件能否被 Agent 处理还取决于已发布应用配置的工具。 [Multi-Agent 附件能力](https://cloud.tencent.com/document/product/1759/122607)
- `credentials`、`upload`、`sign` 分别对应申请临时凭证、上传对象和取得可读链接。出现 403 时先核对应用、地域和 COS 权限。下载签名的有效期受临时凭证到期时间约束。 [ADP COS 权限指南](https://cloud.tencent.com/document/product/1759/116238)
- `chat` 阶段超时或断流时，服务端可能已接收该轮。记录 `request_id` 和错误阶段，确认状态后再决定是否重发。

离线回归可用 `python -B -X utf8 -m unittest discover -s tests`。若 Windows 沙箱的默认临时目录不可写，可先把当前进程的 `TEMP` 和 `TMP` 指向项目内可写位置。真实工作流验收使用已发布的测试应用和可上传的样例，判断回答是否包含只能从附件获得的信息，再验证“缺少信息 → 追问识别 → 补充回答 → 普通结果停止”。离线测试覆盖本地调用和请求契约，不能替代云端配置、附件权限及真实业务验收。本次范围和证据记录见 [docs/clarification.md](docs/clarification.md)。
