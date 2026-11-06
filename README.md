# ADP 文字消息转发与会话续接

每次运行 [main.py](main.py)，读取一条文字消息，恢复本地会话，调用一次 ADP HTTP SSE V2，显示正式回复后退出。ADP 回复追问或普通答案都按相同流程处理；下次启动时可继续回答，由 ADP 使用历史上下文。

## 配置和运行

使用 Python 3.14 或更新版本。文字运行只用标准库，无第三方运行依赖，不需要安装云 SDK。

将 [config.example.toml](config.example.toml) 复制为本机 `config.toml`。模板中的每个配置项都有用途、取值和适用入口说明。至少填写已发布测试应用的 `app_key` 和稳定的 `user_id`。AppKey 不是腾讯云 SecretKey，不要提交真实配置。

```powershell
python main.py
python main.py --ask "你好"
python main.py --ask "继续刚才的话题"
python main.py --config "C:\配置\config.toml"
```

PyCharm 直接运行脚本即可；默认配置位置基于脚本目录，配置内日志与会话相对路径基于配置文件目录，不依赖 PyCharm 当前工作目录。只输入本轮问题，不会继续读取 ADP 追问的补答。

显式开始新会话：

```powershell
python main.py --new-session
python main.py --new-session --ask "新话题"
```

`--new-session` 单独执行只保存新 ID，不发送请求；键盘输入 `/new` 效果相同。旧会话不会自动清空，只有显式新会话命令替换本地状态；如需保留旧 ID，请先自行备份。

本次已移除图片和文件输入、上传模块、相关参数、配置、依赖及样例。旧 `/image`、`/file` 和 CLI 参数会被拒绝。旧配置中遗留的 `[attachments]` 仅被忽略，不加载其凭据，可删除该分组。

## 会话、回复与失败处理

[adp_chat/session.py](adp_chat/session.py) 默认使用 `runtime/session.json`，只存 `schema_version=1`、UUID `conversation_id`、`user_id` 和 `sha256:` AppKey 指纹。它不保存 AppKey 或消息正文。

同应用同用户恢复同一 ID；首次网络请求前用同目录临时文件、刷新磁盘及原子替换落盘。身份不匹配、文件损坏、版本不支持会明确报错，普通启动不覆盖。切换身份时用独立会话路径或显式新会话命令。

每轮 RequestId 独立生成。空问题不发送；超时、中断、错误终态或会话 ID 不一致时失败，不自动重发，保留本地会话和安全诊断标识。常规错误退出码 1，键盘中断 130；未输入问题便结束标准输入时退出 0，且不发请求。只支持本地顺序启动，不承诺多个进程并发提交同一会话。

正式回复只取 `reply` 消息；thought 和工具消息不显示为答案。成功必须收到成功的 `response.completed`。退出码 0 代表本轮传输成功，不表示业务问题已经完成。服务端若返回 ConversationId，则必须与提交值相同；未返回时保持旧协议兼容。服务端诊断 RequestId 不与客户端 RequestId 强制匹配。

ADP 的上下文轮数会影响历史记忆；复用 ID 不等于无限期保留所有历史。当前协议依据见 [腾讯云 HTTP SSE V2](https://cloud.tencent.com/document/product/1759/129202)。

## Python 单轮接口

```python
from dataclasses import replace
from adp_chat import chat, load_config
from adp_chat.session import load_session

config = replace(load_config("config.toml"),
                 detect_text_clarification=False, structured_interaction=False)
conversation_id = load_session(config)
result = chat("你好", config=config, conversation_id=conversation_id)
print(result.text)
```

[adp_chat/client.py](adp_chat/client.py) 的 `chat()` 始终只发一轮，不读取键盘。省略 `conversation_id` 和 `reply_to` 时仍新建会话；持久化由调用方显式使用 `load_session()`。文本参数、回调和已有文字追问接口保留；附件参数已按当前范围移除。

`ChatResult` 保留 `text`、`conversation_id`、`request_id`、`record_id` 及高级接口状态。新增的 `reply_contents` 按正式消息分组保留内容类型和独立复制的数据；即使服务端返回非文本内容也不会丢弃。终端显示文本和非文本 JSON，不把空文本解释为业务完成。

## 可选的高级文字接口

`chat_until_complete()`、`ClarificationAnswer` 和旧 CLI 为原文字消费者保留。它们根据配置和 `needs_input` 处理补答，**默认 main.py 不调用这一流程**，也不执行文字追问猜测或业务 JSON 完成判定。

```powershell
python -m adp_chat
python -m adp_chat --ask "你好"
python -m adp_chat --conversation-id "已知的有效会话ID" --ask "继续"
```

旧 CLI 默认读取当前目录的配置，`--config` 可指定路径。交互输入 `/new` 换会话、`/exit` 退出。`--ask` 会完成已识别的追问后退出；旧 CLI 不使用本地 session.json 自动恢复，其文字行为保持兼容。追问时输入结束或取消会返回非零。

高级接口的 `answer_provider` 获取真实答案，`on_result` 交付各轮结果；回调抛错即停止，不自动重试。问卷和 Widget 动作继续使用原协议。业务 JSON 模式仅在应用显式配置后开启，见 [docs/adp-chat/02_结构化业务状态.md](docs/adp-chat/02_结构化业务状态.md)；文字追问边界见 [docs/adp-chat/01_文字追问接口.md](docs/adp-chat/01_文字追问接口.md)。

## 日志与验证

[adp_chat/logging.py](adp_chat/logging.py) 只写文件，不修改宿主 root logger。默认 UTF-8 日志位于 `logs/adp_chat.log`，约 5 MiB 轮转，保留 3 份备份。每条带北京时间、毫秒、源码文件行号、级别和会话/请求关联 ID。日志先脱敏 AppKey、URL 签名和令牌，再保留问题全文及最多 100 个 Unicode 字符的回答。`log_answer_chars=0` 只关闭回答正文；要关闭全部问答日志请设 `log_enabled=false`。

启动时日志或会话文件不可写会在对话请求前失败。运行中的日志写入采用尽力记录，不导致业务自动重试。多个进程不应共用同一轮转文件。

```powershell
python -B -X utf8 -m unittest discover -s tests
```

测试覆盖真实临时文件、进程退出再启动、会话身份隔离、原子保存及清理失败、单次转发、协议一致性、旧文字接口和日志脱敏。沙箱默认临时目录不可写时，可把当前进程 TEMP/TMP 指向项目内可写目录。模拟 SSE 只能证明本地行为；真实文本验收需在两个独立进程中先发送唯一编号，再询问该编号。本次不安装 wheel，也不发布或部署。

## 文档索引

全部技术文档按主题分类，推荐阅读顺序和当前能力状态见 [docs/README.md](docs/README.md)。企业微信自建应用接入、消息可靠性、JSONL 日志与 Cosmos DB 均属于**待实施设计**；现有单轮 ADP CLI 行为不变。
