# Docker 部署、环境准备、调试与内网调用

> 本文描述当前仓库已实现的行为；历史验收数字只代表对应提交与环境，不代替当前生产环境验证。具体字段、配置与执行入口以链接的源码和测试为准。


本指南面向拿到源码、需要自行部署并使用的人。**先在本地验证，再部署到 Linux 服务器，最后从另一台电脑调用**。尚未实际运行的环境步骤不能视为已经验收成功。

## 文内导航

- [本地环境、脚本、CLI、FastAPI 和阶段 A～F 联调](#一先在本地验证三种入口)
- [Linux 服务器部署、权限和镜像](#二linux-服务器部署)
- [跨机 HTTP 调用](#三其他电脑如何调用)
- [故障排查](#四调试与故障排查)
- [升级、备份与回滚](#五停止备份升级与回滚)
- [证据范围与未验证事项](#六实际交付与未验证范围)

> 本指南覆盖两类部署：可信内网单用户 HTTP，以及阶段 F 企业微信回调。单用户接口无鉴权，不得因企业微信回调需要外网可达就直接公开整个 FastAPI 服务。

## 系统怎样工作

```text
Windows / 其他内网服务
    │ HTTP POST（不带 Token）
    ▼
10.102.70.82:4560（Linux 宿主机）
    │ Docker 端口映射
    ▼
容器 0.0.0.0:8000
    │ FastAPI → 单用户 ChatService → ADP HTTP SSE
    ▼
腾讯 ADP 云端返回正式 response.completed
    │
    └──── HTTP JSON 回到调用方
```

宿主端口 **4560** 与容器端口 **8000** 不需要相同。宿主 IP **10.102.70.82** 必须是服务器真实拥有、且其他内网客户端可到达的网卡地址。这一版没有 API Key/HTTPS/代理，仅适用于网络访问已受限制的可信内网。

离线打包入口是 [scripts/build_image.ps1](../../scripts/build_image.ps1)，服务器运行、停止和升级共用 [scripts/run.sh](../../scripts/run.sh)；镜像定义和配置字段见 [Dockerfile](../../Dockerfile)、[config.example.toml](../../config.example.toml)。[compose.yaml](../../compose.yaml) 是保留的源码部署入口。运行代码在 [src/api/app.py](../../src/api/app.py) 和 [src/application/chat_service.py](../../src/application/chat_service.py)。

## 一、先在本地验证三种入口

### 1.1 Windows 或 PyCharm 环境准备

1. 安装 **Python 3.14**，在 PowerShell 中确认：`py -3.14 --version`。如果这条命令未识别到 3.14，请先安装该版本，不要用旧版本的成功测试冒充正式运行验证。
2. 获取仓库源码并进入包含 [main.py](../../main.py) 的项目根目录。
3. 新建虚拟环境，安装运行/测试依赖和本项目，再在缺少配置时复制模板：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (-not (Test-Path config.toml)) { Copy-Item config.example.toml config.toml }
```

`requirements-dev.txt` 中的 `-e .` 会读取 [pyproject.toml](../../pyproject.toml)，将 `src/` 声明为源码根，并安装 `adp-chat` 命令。`src/` 及内部业务目录都不需要空 `__init__.py`，也不通过 `python -m src` 启动。

4. 打开项目根目录 `config.toml`，至少填写：
   - `[adp].app_key`：腾讯 ADP 已发布应用的 AppKey。
   - `[adp].user_id`：本服务唯一的固定 ADP UserId。
   - `[adp].visitor_id`：可留空，沿用现有单用户客户端处理。
   - `[session].session_file = "runtime/session.json"` 和 `[logging].log_file = "logs/adp_chat.log"`：路径相对于配置文件目录。
5. 不要提交真实配置，复制前确认新文件不存在，避免覆盖已有密钥/会话配置。

本地 Python 和源码 Docker Compose 均使用项目根目录 `config.toml`。容器内将该文件只读挂载到 `/app/config.toml`，日志与会话分别挂载到 `/app/logs`、`/app/runtime`；源码目录 `src/` 不存放配置或运行数据。开发环境默认根配置按源码位置定位；镜像安装应用后由 `ADP_CONFIG_FILE=/app/config.toml` 明确绑定根配置。PyCharm 直接运行根目录 `main.py` 时无需传 `--config`，即使工作目录不同也能找到配置。显式指定其他配置文件时，相对日志和会话路径仍以该配置文件的目录为基准。

`[deployment]` 是服务器离线脚本专用分组。本地应用加载器忽略该分组，不使用其中的 IP/端口启动 Web 服务，也不将其传给 ADP；本地 HTTP 仍使用启动命令中的 `--host`、`--port`。源码 Compose 保留自己的环境变量端口配置，不读取这个 TOML 分组。

### 1.2 单次脚本

```powershell
.\.venv\Scripts\python.exe main.py --ask "你好"
```

这次运行只向 ADP 发送一条文字，收到正式回复后结束进程。也可运行 `python main.py` 先输入一条文字。第二次启动会恢复 `runtime/session.json` 中的 ConversationId；RequestId 则由每轮新建。

### 1.3 交互式 CLI

```powershell
.\.venv\Scripts\adp-chat.exe
```

进入后连续输入文字，每条消息只发送一轮；`/exit` 或 `/quit` 退出，`/new` **人工开启新会话**。不要在 FastAPI 容器仍使用同一个 `runtime/session.json` 时操作 `/new` 或发消息，否则进程内锁无法协调这两个独立进程。

当前 CLI 由命令行参数和上述交互命令控制，三种入口均只转发一轮，不自动索取补答或判断业务完成。

上游 ADP 的输出模式在根配置中设置：

```toml
[chat]
stream = false  # false 非流式（默认），true 流式
```

修改后重启 main、CLI 或 FastAPI 进程。此开关控制 ADP 的文本回包方式；三种入口均在 `response.completed` 成功后输出完整正式结果，FastAPI 不因此变成流式 HTTP 接口。`streaming_throttle` 仅在 `stream=true` 时有效，0 使用平台默认回包频率。

**配置迁移：**新版已删除 `[cli]`（`first_question`、`show_preview`、`interactive`）、`[attachments]`，以及 `[chat]` 下的 `incremental`、`detect_text_clarification`、`structured_interaction`；加载器会拒绝这些退役项。将其删除后增加 `stream=false`，其他现用参数按 [config.example.toml](../../config.example.toml) 填写。旧自动追问/业务状态/补答/预览编排接口也已退役，当前单轮入口在 [src/adp/client.py](../../src/adp/client.py)。

服务器升级前保留一份权限受限的旧配置备份，使用新镜像时同步采用新配置。回滚旧镜像时须同时恢复旧配置格式；部署脚本不会自动恢复配置内容，旧镜像不能直接加载新增的 `stream` 字段。现有会话文件无需迁移。本机配置清理不代表服务器已升级。

### 1.4 本地 FastAPI 与 PyCharm 调试

在单独终端启动：

```powershell
.\.venv\Scripts\python.exe -m uvicorn api.app:app --host 127.0.0.1 --port 8000 --workers 1
```

这个进程会持续运行，按 Ctrl+C 停止。再打开第二个 PowerShell：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
.\.venv\Scripts\python.exe examples/http_client.py "你好" --url http://127.0.0.1:8000
```

在 PyCharm 中选择已安装上述依赖的 Python 3.14 解释器（建议使用项目 `.venv`），将 `src/` 标为 Sources Root，运行/调试 `main.py`，脚本参数留空即可在控制台输入问题；需要单次固定输入时设置 `--ask "你好"`；调试 Web API 时把运行模块设为 `uvicorn`、参数设为 `api.app:app --host 127.0.0.1 --port 8000 --workers 1`，工作目录设为仓库根。可以在 [src/api/routes.py](../../src/api/routes.py)、[src/application/chat_service.py](../../src/application/chat_service.py) 和 [src/adp/client.py](../../src/adp/client.py) 设置断点。

### 1.5 本地测试

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -v
```

`requirements-dev.txt` 安装了本项目及 FastAPI/HTTPX 测试依赖。请检查测试最后的通过/失败/**跳过**数量；如果 `tests/test_api.py` 因依赖未安装跳过，不可以认为 Web API 已验证。额外可运行：

```powershell
.\.venv\Scripts\python.exe -m py_compile examples/http_client.py
```

镜像本机验收可使用 [tests/docker_smoke.py](../../tests/docker_smoke.py)：

```powershell
docker build -t adp-chat:local .
python tests/docker_smoke.py --image adp-chat:local
```

该检查只使用临时配置/会话，验证正式镜像安装、启动、只读配置、可写日志、容器重建会话恢复，并清理测试资源；不请求腾讯 ADP。真实云端连通和服务器网络边界仍须在部署环境验证。

### 1.6 多员工身份与真实 ADP 跨进程验收（阶段 A）

身份入口在 [src/core/identity.py](../../src/core/identity.py)，配置仍由 [src/core/config.py](../../src/core/config.py) 从同一份根配置读取。单用户三种入口继续使用 `[adp].user_id` 和旧会话文件；员工 Probe 不读写该文件，也不创建员工映射表。

在本机未纳入 Git 的 `config.toml` 中增加以下作用域，保留现有 AppKey 和其他实际配置：

```toml
[wecom]
corp_id = "wwf483023b734c570a"
agent_id = 1000053
adp_application = "wecom-local-test"
```

三项必须同时填写；完全不用员工身份时，可全部留空/0 或省略整个分组。`adp_application` 表示已发布 ADP 应用的固定逻辑名称，不能填写 AppKey；同一实际应用重启时保持不变，切换实际应用应使用独立逻辑名。AppKey 轮换不改变会话。回退不认识该分组的旧代码前，备份实际配置并移除新增分组，旧 `session.json` 无需迁移。

先检查离线派生，不访问 ADP：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 tests/adp_conversation_probe.py --user-id YNND
.\.venv\Scripts\python.exe -B -X utf8 tests/adp_conversation_probe.py --user-id SIYU
```

再按顺序运行下列命令。每条命令各启动一个 Python 进程并退出；真实模式必须显式指定 `--execute-real-adp`，每个进程最多发一次请求，失败不自动重试。仅使用已授权的测试应用，发生费用的是这三次 ADP 请求，不触发企业微信发送。输出文件必须尚不存在，复验时换新文件名；不要通过删证据来盲目重复未知请求。

```powershell
.\.venv\Scripts\python.exe -B -X utf8 tests/adp_conversation_probe.py --user-id YNND --message "我想改密码" --execute-real-adp --output runtime/probe-ynnd-1.json
.\.venv\Scripts\python.exe -B -X utf8 tests/adp_conversation_probe.py --user-id YNND --message "windows" --execute-real-adp --output runtime/probe-ynnd-2.json
.\.venv\Scripts\python.exe -B -X utf8 tests/adp_conversation_probe.py --user-id SIYU --message "windows" --execute-real-adp --output runtime/probe-siyu-1.json
```

检查 JSON 中两轮 YNND 的 `user_id/visitor_id/conversation_id` 相同、`request_id/pid` 不同，SIYU 的会话不同。随后实际阅读本地 `response_text`：首轮应澄清密码类型，续轮应把 Windows 作为这次密码请求的补充并给出对应指引；SIYU 无该前文时不应继承 YNND 的需求。Probe 不用关键词自动判通过，`semantic_verdict=requires_review` 表示需要人工或 AI 审查。固定 ID 不代表 ADP 永久记忆，实际结果受已发布应用配置及平台约束影响；失败时保留实际响应排查，不改平台生产提示词制造通过。

[tests/adp_conversation_probe.py](../../tests/adp_conversation_probe.py) 复用正式 `chat()`，每轮覆盖员工 UserId/VisitorId，并关闭问答日志。控制台只显示标识/状态，完整回复留在指定本地证据文件，可能包含内部知识或链接；不提交这些文件和真实密钥。可共享的记录只保留请求标识、脱敏语义摘要和结论。稳定回归在 [tests/test_employee_identity.py](../../tests/test_employee_identity.py)，不访问真实 ADP；本阶段未接入企业微信，尚不能通过回调或模拟发送完成全链路。

PyCharm 使用项目 `.venv` 运行上述 Probe 脚本；设置相同脚本参数，在身份函数和正式 ADP `chat()` 设置断点。此处仅提供配置方法，不代表已实际操作 PyCharm GUI；FastAPI 调试方式仍见上一节。

### 1.7 加密回调与可靠接收（阶段 B）

这是本机协议模拟，不连接企业微信后台或发送员工消息。B 已实现正式回调、验签解密、企业/应用/员工校验、MsgId 去重和可靠快照；当前main已接入C异步ADP；先按1.8准备隔离Fake配置和Fake服务，再运行下列协议步骤。BUSY提示统一保存并交给D；默认disabled保持PENDING，显式simulated后闭合为SEND_SIMULATED，ADP终态保存后释放占用；只有上一轮仍处理时才BUSY。

先按1.1安装依赖，在1.8的隔离测试配置中填写身份作用域；下例不修改含真实AppKey的根配置。初始化独立测试密钥：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_callback_client.py --config runtime/wecom-fake/config.toml --init-test-keys
```

这个命令只填充空测试 Token/EncodingAESKey，并将 `[wecom].gateway_enabled` 打开，不显示密钥；已有任何回调凭据或已启用网关时拒绝覆盖，保留原 AppKey、用户、路径和资源值。Windows 用 ReplaceFile 保留原配置 ACL。整个服务仍只从原有 `config.toml` / `ADP_CONFIG_FILE` 读取配置，不另建秘密来源。完整字段和默认值见 [config.example.toml](../../config.example.toml)。

在独立终端启动同一正式应用（单 worker），也可按 1.4 在 PyCharm 运行模块 `uvicorn`：

```powershell
$env:ADP_CONFIG_FILE = (Resolve-Path runtime/wecom-fake/config.toml).Path
.\.venv\Scripts\python.exe -m uvicorn api.app:app --host 127.0.0.1 --port 8000 --workers 1
```

在另一个终端，用 [tests/wecom_callback_client.py](../../tests/wecom_callback_client.py) 调正式 GET/POST：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_callback_client.py --config runtime/wecom-fake/config.toml --verify-url
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_callback_client.py --config runtime/wecom-fake/config.toml --user-id YNND --msg-id 10001 --message "本地第一条"
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_callback_client.py --config runtime/wecom-fake/config.toml --user-id YNND --msg-id 10001 --message "相同 MsgId 的重复回调"
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_callback_client.py --config runtime/wecom-fake/config.toml --user-id YNND --msg-id 10002 --message "同员工忙碌拒绝"
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_callback_client.py --config runtime/wecom-fake/config.toml --user-id SIYU --msg-id 10003 --message "另一员工独立接收"
```

正常输出包含 `source=test_fixture`、`cloud_identity_verified=false`、HTTP 200 和 `expected_ack=true`。GET 原样返回解密 echo；POST 可靠保存后返回空包。相同 MsgId 的第二条只 ACK，原正文/接收决定不改变；新 MsgId 的忙碌拒绝会留下独立快照，SIYU 可独立获准。测试客户端只允许 loopback HTTP 地址，复用正式加密实现，没有跳过签名的服务端接口。

查看状态（以下只显示必要标识和状态，不展示问答正文）：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_callback_client.py --config runtime/wecom-fake/config.toml --inspect-state
```

JSON 是唯一业务状态，文件名是企业/应用/MsgId 的哈希；锁文件只负责目录独占，不是第二套员工状态。两个进程不能同时使用同一 `messages_dir`；PyCharm 与 Docker 的目录也应隔离。损坏或未知版本的快照会阻止启动，保留现场排查，不自动删除或当新消息处理。写入失败后不返回成功 ACK，并关闭后续接收直到重启核验快照；现有去重记录正常时，重复回调无需新写盘。

默认入站上限 65536 字节、最多2000条快照（既有字段max_unfinished_messages，包含待通知、未知和去重保留记录）；这是本地内存/文件保护，触发返回非成功状态，**不是 ADP 模型业务并发名额**。回调时间戳与本机 Unix 时间需在 300 秒以内，旧签名重放拒绝；模拟客户端每次生成当前时间戳。应用日志只记哈希和接收状态，启用回调时访问日志不保存查询串中的签名/密文。当前网关事件与诊断共用普通 `.log`，查看方法见 1.10。

稳定回归与不占用现有目录的真实 HTTP 验收：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -p test_wecom_callback.py -v
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_local_smoke.py --report runtime/wecom-http-result.json
```

[tests/test_wecom_callback.py](../../tests/test_wecom_callback.py) 覆盖坏签名、密文/填充/长度、内外身份、恶意 XML/超大包、真实并发、fsync/replace 失败、损坏快照和配置安全初始化。[tests/wecom_local_smoke.py](../../tests/wecom_local_smoke.py) 自动创建临时测试配置和目录，启动/关闭两个正式 Uvicorn 进程，隔离下游worker，仅验证B的HTTP接线、跨进程去重/占用恢复及端口释放，随后删除自有临时数据；不调用ADP。C全链路另用1.8脚本验证。需要反复试验时优先用这个隔离入口，不手改现有消息快照。正式配置及人工测试快照需按本地数据管理要求备份/清理，不能把“ACK 成功”写成 ADP 完成或真实企微送达。

### 1.8 异步 ADP与恢复（阶段 C）

最短可重复验收（不改现有配置、不调用真实ADP）：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_dispatch_smoke.py --report runtime/wecom-dispatch-result.json
.\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -p test_wecom_dispatch.py -v
```

[tests/wecom_dispatch_smoke.py](../../tests/wecom_dispatch_smoke.py)自动启动独立正式网关和[tests/wecom_fake_adp.py](../../tests/wecom_fake_adp.py)进程，以签名加密回调验证100员工真正同时等待、Fake30入口名额、同员工BUSY及重复回调；强杀自有网关进程后验证RECEIVED首次执行、armed恢复UNKNOWN不重投、同员工新消息可继续。结束核对进程退出和端口释放，删除自有临时配置/数据。30名额测试只核对Fake实际拒绝集合；本次保持30名额至手动屏障释放才观察到70拒绝，这不是实际ADP的固定失败数保证。

手动/PyCharm调试使用**同一TOML体系的独立测试文件**：只在目标不存在时复制[config.example.toml](../../config.example.toml)到`runtime/wecom-fake/config.toml`，保留根真实配置。编辑以下字段，其他通用字段保留示例；不得把真实AppKey复制到Fake文件：

| 字段 | Fake本地值 |
| --- | --- |
| `[adp].app_key` | `fake-local-adp-only`（Fake服务拒绝其他凭据） |
| `[wecom].corp_id` / `agent_id` | `wwf483023b734c570a` / `1000053` |
| `[wecom].adp_application` | `wecom-local-fake`，与真实测试分开 |
| `[wecom].adp_url` | `http://127.0.0.1:8765/adp/v2/chat` |
| `[logging].log_question` / `log_answer_chars` | `false` / `0` |

相对session/messages/log路径都基于该测试配置目录，与根运行数据隔离。然后按1.7的`--config`初始化空测试密钥并启动正式网关；先在另一个终端启动Fake：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_fake_adp.py --port 8765 --quota 30 --hold
# 查看Fake实际接收与拒绝集合；该控制路由只属于Fake，不在正式网关：
Invoke-RestMethod http://127.0.0.1:8765/_test/status
# 两名员工获准后保持未完成，继续发YNND新MsgId可稳定观察BUSY；释放后结果保存：
Invoke-RestMethod -Method Post http://127.0.0.1:8765/_test/release
```

Fake还支持`--mode reject --code 460031`、`disconnect`、`timeout`、`model-error --code 460701`和`reply7`。不加`--hold`时立即结束；只在Fake做高并发/故障。正式网关没有模型限额信号量、配额队列或ADP自动重试；仅有快照容量、1MiB SSE总字节等物理保护。`[wecom].adp_url`只允许腾讯正式HTTPS端点或本机Fake地址，避免凭据跟随重定向或流向任意端点。

真实ADP受控探测沿1.6执行，加`--async-client --config config.toml`即可验证C正式异步HTTPS/SSE；仍需显式`--execute-real-adp`和新输出路径，每个进程最多一次。先停Fake调试网关并移除其环境变量（`Remove-Item Env:ADP_CONFIG_FILE`），使用已发布测试应用、原真实逻辑作用域；不把Fake配置改成真实凭据。C已对SIYU一次安全文字探测取得正式completed，E再做完整跨进程两轮语义验收；不压力测试实际ADP、不改平台提示词。

状态v3（兼容B/C历史快照）在首次网络调用前保存`ADP_SEND_ARMED`和UUID RequestId；终态、一次PENDING提示和本地释放同一原子快照。明确应用入口拒绝保留真实Code并提示手动重试；模型层QPM/TPM/余额/并发错误可能发生在工具之后，缺乏执行前证据时保守UNKNOWN。ReplyMethod7解释并发含义，但不编造Code或宣称未执行。UNKNOWN不是业务失败，也不保证旧云端请求停止；涉及会议须先核查。当前通知模式默认disabled，显式simulated由D处理；发送状态不持续占用员工。

Windows查看活动快照使用上面的`--inspect-state`，它用同Owner的共享删除读取；避免用持有普通文件句柄的编辑器/Get-Content持续轮询活动文件，Windows会阻止替换。更新使用保留ACL的ReplaceFileW，初次发布/Posix使用os.replace，均先flush/fsync；不是绕过写入失败。目录独占/版本/CAS检查，v1先全部校验再兼容升级；未知版本/损坏/原快照缺失但留暂存证据均保留现场并拒绝启动。此类发布故障需核查恢复，不能删除目录后盲重投。正常退出读回取消期间磁盘提交；已保存成功不会改写UNKNOWN，armed不确定结果不重投。

启动及60秒维护执行安全清理：已接受通知7天后清正文、接收30天后删闭合去重记录；待处理、待通知及UNKNOWN不按年龄简单删除，未核查状态与敏感正文需按企业数据策略处理。默认disabled时PENDING不会因时间清除；D模拟发送可闭合，E 阶段历史版本曾使用 JSONL，当前版本已统一普通日志；待通知/UNKNOWN正文仍需按企业策略核查处置，不宣称已自动脱敏。回退旧版先停网关、备份全部状态并恢复兼容配置，不能让旧版继续处理v3的新状态。不要同时运行两个worker或让PyCharm/Docker共享状态目录。

### 1.9 统一通知与正式发送协议隔离测试（阶段 D）

完整本机协议链路仍使用1.8的独立Fake配置，不需要真实企业微信Secret。将测试文件`[wecom].notification_mode="simulated"`、`gateway_environment="test"`后重启同一正式FastAPI；正常回答、BUSY、ADP拒绝和UNKNOWN都走同一Simulator，状态只会是SEND_SIMULATED。默认disabled继续保留PENDING。用1.7的`--inspect-state`查看环境、send_state与send_result.source，不显示正文、Token或Secret。

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -p test_wecom_sender.py -v
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_dispatch_smoke.py --simulate-notifications --report runtime/wecom-send-result.json
```

[tests/test_wecom_sender.py](../../tests/test_wecom_sender.py)通过Fake HTTP验证正式[src/wecom/sender.py](../../src/wecom/sender.py)：应用Token缓存/并发失效刷新、40014/42001一次纠正、2048 UTF8字节完整分片、HTTP200但业务错误、无效/未授权用户（忽略账号大小写）、@all群发/多人目标拒绝、消息网络未知、URL日志脱敏和用户会话独立。Fake HTTP即便解码到errcode0也只能产生SEND_SIMULATED/source=fake_http；protocol_accepted仅证明解码规则。Simulator为source=simulated，不代表任何企业微信API调用。

[tests/wecom_dispatch_smoke.py](../../tests/wecom_dispatch_smoke.py)加上述flag后，真实独立Uvicorn+Fake ADP完成100员工/30名额、加密回调→ADP→已存提示→Simulator；另在SEND_ARMED后的模拟出口挂起并强杀自有网关，重启只记SEND_UNKNOWN，不重新发送旧通知。也保留ADP强杀恢复及独立员工/新消息验收。结束清理自有进程/监听/临时数据，无真实员工消息。

通知状态和ADP状态独立。PENDING可恢复首次发送；SEND_ARMED后的崩溃、断线或响应丢失不能盲重发。明确发送失败或发送未知不再问ADP、不持续锁员工、不生成失败通知的失败通知；磁盘失败仍保留现场并停止不可靠接收。长文字只有全部分片确认才闭合，部分发送失败不重发已接受前片。API接受不表示员工在线、已读或客户端已展示。

**图文回复配置与排障**：正式答案中的图片现在按原始顺序发送为原生图片气泡，本地 main/CLI 保持文字及原始内容输出。`[wecom].image_allowed_hosts` 默认 `["*"]`，允许任意公网 HTTPS 来源及其 CDN 跳转；显式完整域名列表继续收紧来源，空列表继续禁用下载。已有配置若仍为 `["lke.cloud.tencent.com"]`，需改成 `["*"]` 才能放开跨域跳转；省略该字段则使用新默认。内网地址、非 HTTPS 和异常资源仍拒绝。图片共享2GiB保守预约预算，按最大图片及临时副本每张预留40MiB，最多51张同时准备；满额等待计入准备时限，超时原位以链接降级，纯文字不占图片预算。该预算不代表整个服务的RSS硬上限，具体网络与资源边界见架构说明。`image_prepare_timeout_seconds` 为整回复累计下载/上传/等候名额预算，发送预算仍由 `send_timeout_seconds` 独立控制；精确默认值和固定资源限制见 [config.example.toml](../../config.example.toml)。图片不落盘、不永久缓存；失败在原位置以链接降级，日志结果码 `accepted_with_image_fallback` 表示已接受的消息含降级。已提交的消息响应未知仍停止发送，不能因图片没看到就自动重发整条答案。升级需重新打包并重启对应服务；回滚不识别 `*` 的旧代码时，应同时恢复旧的完整域名列表。行为和生命周期 Owner 见 [docs/wecom-integration/01_网关架构设计.md](../wecom-integration/01_网关架构设计.md#有序图文回复)。

升级时在实际配置补充上述两个可选字段，保留已有凭据/身份/路径和取值；不补也使用安全缺省值。回滚旧代码前移除它不认识的新增键，保留 v3 消息目录和 UNKNOWN 证据。既有 F 客户端已由用户于2026-11-09确认通过，本次新图文真实终端显示尚需部署后验收。

v1/v2先校验后兼容升级到v3，保存环境和发送证据源。测试目录不能切成production直接复用；旧记录若已有无法证明的发送终态，拒绝启动而非补造真实接受。已模拟完成的数据用独立测试目录按7/30天策略清理，不能计作真实SEND_ACCEPTED；UNKNOWN保留核查证据且不锁用户。回退必须停网关并备份全部状态，恢复兼容配置，禁止旧代码继续处理新格式。

真实模式 `wecom` 要求 `gateway_environment="production"` 及未提交仓库的应用 Secret。该模式已在阶段 F 的单员工真实联调中使用，**不等于阶段 F 全部验收完成**。新环境接入仍须确认回调归属、可达地址、应用可见范围/权限、可信出口 IP、独立生产状态目录及至少两名真实员工的端到端验收；本机 Fake 成功不能替代这些检查。

### 1.10 A～E完整本地链路、统一日志与Docker

本节复用前述正式应用、配置和状态，不连接真实企业微信。快速自动验收使用临时独立目录；手动调试、容器和真实ADP分别使用不同目录。旧单用户会话与员工消息快照亦分开。

**本机手动启动**（项目根目录、Python3.14及依赖按1.1）：

```powershell
# 仅当目标不存在时生成同一TOML格式的独立Fake配置；已有文件绝不覆盖。
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_local_setup.py --output runtime/wecom-local/config.toml --scope wecom-local-dev-001
# 终端1：Fake只接受fake-local-adp-only，不接受真实AppKey。
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_fake_adp.py --port 8765 --quota 30 --hold
# 终端2：本进程明确选择该TOML，其他入口/根实际配置不改。
$env:ADP_CONFIG_FILE = (Resolve-Path runtime/wecom-local/config.toml).Path
.\.venv\Scripts\python.exe -m uvicorn api.app:app --host 127.0.0.1 --port 8000 --workers 1
```

[tests/wecom_local_setup.py](../../tests/wecom_local_setup.py)生成新的测试Token/AESKey，启用网关及simulated通知，关闭问答正文日志。输出仅限本项目忽略的runtime子目录，scope必须以wecom-local-开头；后续两轮和重启保持同一scope。它不发请求、不复制真实企微Secret，不给运行代码增加另一种配置来源。已有配置应直接复用；需要新测试作用域时明确选新目录和名称，不删除未知请求证据后重跑。

终端3运行1.7的签名客户端，将`--config`改为`runtime/wecom-local/config.toml`：先GET校验，再依次发送YNND/10001、重复10001、YNND/10002、SIYU/10003。Fake保持屏障时，第一条YNND与SIYU均进入ADP，YNND新10002立即BUSY，重复10001没有第二次执行或通知。查看`--inspect-state`后释放：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/_test/status
Invoke-RestMethod -Method Post http://127.0.0.1:8765/_test/release
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_callback_client.py --config runtime/wecom-local/config.toml --inspect-state
# 需要下一次保持屏障时，只影响未来请求，不重放历史：
Invoke-RestMethod -Method Post http://127.0.0.1:8765/_test/hold
```

通知终态应为`SEND_SIMULATED/source=simulated`；它不证明企业微信API接受。根配置或其他终端不会因为当前环境变量改变；服务退出后在该终端执行`Remove-Item Env:ADP_CONFIG_FILE`，避免后续误选配置。Ctrl+C正常关闭两个自有服务，保留手动调试状态；只在确认不再需要核查后清理自己的测试目录，不删除未知或生产状态。

**50/100用户和故障注入**：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_dispatch_smoke.py --users 50 --simulate-notifications --report runtime/wecom-pressure-50.json
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_dispatch_smoke.py --users 100 --simulate-notifications --report runtime/wecom-pressure-100.json
.\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -v
```

脚本启动正式独立HTTP网关和TCP/SSE Fake，分别验证无限额与30名额、同员工BUSY、先去重、ADP实际强杀恢复及模拟发送进入武装边界后的强杀恢复。报告给出实际峰值/拒绝集合、ACK P50/P95/P99/最大值、回调批次耗时/吞吐和日志事件关联/脱敏统计；ACK吞吐不是ADP模型吞吐。屏障保持期间50/100对30名额通常出现20/70入口拒绝，以本次真实集合为准。所有请求仅发一次，脚本只回收自身进程/监听/临时数据。Fake压力证据不等于真实平台容量；真实50/100并发须另有明确授权，不直接把Fake命令改成真实端点。

**日常离线回归**：本次真实ADP响应已经脱敏保存到[tests/fixtures/adp_http_sse_recorded.json](../../tests/fixtures/adp_http_sse_recorded.json)，由[tests/adp_recorded_fixtures.py](../../tests/adp_recorded_fixtures.py)经Mock HTTP传输回放给正式异步客户端。[tests/test_wecom_dispatch.py](../../tests/test_wecom_dispatch.py)验证正常ReplyMethod18、入口QPS拒绝460031、成功ACK及处理事件之后的余额错误460702，并检查持久化、通知、释放、去重与不自动重试。双用户并发及重新打开状态目录后的下一轮，检查身份、稳定ConversationId、独立RequestId与通知关联。运行以下命令即可，不读取真实配置、不请求ADP或企业微信：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -p test_wecom_dispatch.py -v
```

回放保留真实事件顺序、状态、Code、ReplyMethod和可选字段形状；正文及标识已替换。测试中的独立回答标签只证明网关投递关联，不能重新证明云端的上下文理解。普通代码回归使用这组样本、上述Fake50/100及全套单元测试即可；平台协议、应用配置或模型发生变化时，再按变化范围决定是否重新进行受控联调。

2026-11-08经用户明确授权，通过模拟加密回调、正式FastAPI/原生HTTPS客户端和模拟发送器，实际调用54次：4次双用户对话控制、50次不同用户同时请求；每个RequestId只发送一次。50用户结果为18次成功、30次460031入口拒绝、2次处理开始后的460702，后者可靠保存为ADP_UNKNOWN并释放本地会话。SDK在途请求峰值50，不能据此断言平台模型并发容量；回调ACK P50/P95为135.418/183.038ms。所有54次HTTP状态均为200，仍须按SSE事件判定结果。错误码含义核对[腾讯ADP官方HTTP协议](https://cloud.tencent.com/document/product/1759/129202)。

本次两轮之间退出并重新启动网关进程：YNND从“我想改密码”到“windows”，实际回答从追问系统转为Windows密码修改指引；同时SIYU的“windows”被独立理解为一般Windows问题，后续BitLocker问题亦得到对应回复。已逐条阅读实际回答并核对两用户身份及会话标识，未以关键词匹配代替语义验证。完整回复、请求标识、状态和报告仅保留在本机忽略的`runtime/real-adp-validation-20261108-105520-95364d/`，共享样本只保留协议投影和请求标识哈希。出现专属模型余额错误460702后停止100用户批次，因此100用户真实容量未验证；需先由应用管理员核查模型余额和配置，不能靠重试解决。未修改平台提示词或模型配置，通知全部SEND_SIMULATED，未向真实员工发送消息。

**查看消息状态与统一普通日志**：消息事实源仍是配置目录下 `runtime/messages/*.json`，可使用 `--inspect-state` 共享读取。业务事件与诊断现在都写到配置指定的 `[logging].log_file`（例如 `logs/adp_chat.log`），**不再生成独立 `events-*.jsonl`**。旧版本遗留的 JSONL 不自动删除。

```powershell
# 人工排障：查看完整诊断和事件记录（路径以实际测试配置为准）。
Get-Content runtime/wecom-local/logs/adp_chat.log -Tail 100 -Encoding utf8

# 提取业务事件：新格式日志的键值字段统一由 Python 解析器处理。
.\\.venv\\Scripts\\python.exe -c "from pathlib import Path; from core.logging import parse_log_line; p=Path('runtime/wecom-local/logs/adp_chat.log'); [print(e) for line in p.read_text(encoding='utf-8').splitlines() if (e:=parse_log_line(line)).get('event')]"

```

轮转文件也可能包含历史事件，查询完整时间范围需同时读取 `adp_chat.log.1` 等备份。统一日志仍由 `log_max_bytes` 和 `log_backup_count` 控制；原 `events_retention_days` 暂保留为兼容配置，但不再控制文本日志轮转或清理。普通日志保留原前缀，使用中文说明与 ` | ` 键值字段，不再追加 `record` JSON；未来采集器使用 `core.logging.parse_log_line()` 解析**全部新格式日志**，包括普通诊断与网关事件。未来 Cosmos DB 采集器必须处理轮转、未完成尾行、断点、幂等和积压保留；当前没有采集器。日志写失败仅作安全告警/计数，不改变业务 ACK 或重试判断，消息快照写入失败则仍必须阻止成功 ACK。已实现的方案 B 与未来 Cosmos DB 设计见 [docs/logging/01_统一日志与Cosmos_DB接入设计.md](../logging/01_统一日志与Cosmos_DB接入设计.md)。

**命令行与PyCharm**：命令行使用上述同一模块/解释器即可启动和排障。本轮未操作PyCharm GUI，不把配置说明当作GUI实测证据。需要用户自行调试时，新建Python运行配置：解释器`.venv/Scripts/python.exe`，模块`uvicorn`，参数`api.app:app --host 127.0.0.1 --port 8000 --workers 1`，工作目录为仓库根，环境变量`ADP_CONFIG_FILE`填上述测试TOML的绝对路径。先在另一终端启动Fake。可在[src/api/wecom_routes.py](../../src/api/wecom_routes.py)、[src/application/wecom_worker.py](../../src/application/wecom_worker.py)和[src/wecom/sender.py](../../src/wecom/sender.py)设置断点。暂停事件循环可能超过回调ACK窗口，使用独立MsgId并读快照核查；断点恢复后不盲重发未知请求。不启多worker或同时用Docker读写该目录。

**真实ADP跨进程两轮**：先停止Fake网关，生成另一份独立测试TOML，只有这个明确操作复制根实际ADP凭据及chat选项；真实企微Secret始终留空、发送仍simulated，文件只在忽略目录本机保存：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_local_setup.py --output runtime/wecom-real/config.toml --scope wecom-local-real-001 --real-adp-source config.toml
# 终端1：启动正式网关；无需启动Fake。
$env:ADP_CONFIG_FILE = (Resolve-Path runtime/wecom-real/config.toml).Path
.\.venv\Scripts\python.exe -m uvicorn api.app:app --host 127.0.0.1 --port 8000 --workers 1
# 终端2：新输出文件，每次只提交一次加密回调。
.\.venv\Scripts\python.exe -B -X utf8 tests/adp_conversation_probe.py --config runtime/wecom-real/config.toml --gateway-url http://127.0.0.1:8000 --user-id YNND --message "我想改密码" --execute-real-adp --output runtime/real-ynnd-1.json
# 等首轮完成并退出Probe；Ctrl+C停止终端1网关，再用同配置重新启动网关。
.\.venv\Scripts\python.exe -B -X utf8 tests/adp_conversation_probe.py --config runtime/wecom-real/config.toml --gateway-url http://127.0.0.1:8000 --user-id YNND --message "windows" --execute-real-adp --output runtime/real-ynnd-2.json
.\.venv\Scripts\python.exe -B -X utf8 tests/adp_conversation_probe.py --config runtime/wecom-real/config.toml --gateway-url http://127.0.0.1:8000 --user-id SIYU --message "windows" --execute-real-adp --output runtime/real-siyu-1.json
```

Probe默认仍离线；有真实flag和未存在输出文件后才提交，ACK丢失只观察原消息、不重发。成功需要正式ADP终态和模拟通知终态。核对两轮UserId/VisitorId/ConversationId相同、RequestId与进程独立，SIYU会话不同；实读response_text，第二轮必须承接密码上下文，不能以关键词判定。E实测首轮追问系统，第二轮给出Windows密码修改流程；SIYU独立询问Windows问题类型，直接语义核验通过。完整回复可能含内部知识/链接，只留本机；共享证据只记脱敏摘要和标识。测试不评价ADP业务建议是否正确、不修改平台提示词、不请求会议写入。

真实错误结构检查须单独的无有效密钥配置：新建隔离Fake配置，只将其`wecom.adp_url`指向官方端点，保留故意无效的Fake专属AppKey，沿同gateway Probe发一次允许文字。预期exit1并保留实际Provider Code/状态，而不是把负向测试当成功问答；E真实返回4505004，ADP_REJECTED，模拟提示完成。日常回归使用离线样本；新增真实压力请求必须单独明确范围、费用及停止条件，也不盲目重复未知结果。

**Docker Desktop本地Compose**（Linux引擎，本机ARM64仿真）：

```powershell
# 选尚未存在的新版本；例v0.0.9，不覆盖既有目录/标签。
.\scripts\build_image.ps1 -Version v0.0.9
# 自动隔离验收：临时覆盖已有Compose的路径/端口，结束回收自身容器/网络/临时数据。
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_docker_smoke.py --image adp_channel_api:v0.0.9 --report runtime/wecom-docker-result.json
# 旧单用户镜像链路也可单独核验，不请求真实ADP：
.\.venv\Scripts\python.exe -B -X utf8 tests/docker_smoke.py --image adp_channel_api:v0.0.9
```

[tests/wecom_docker_smoke.py](../../tests/wecom_docker_smoke.py)检查实际linux/arm64、UID/GID10001、安装源码、只读TOML和可写bind目录；100员工/Fake30、BUSY/去重、正常重启保留闭合记录；实际SIGKILL后的旧ADP仅一次、RECEIVED首次恢复、UNKNOWN后新消息可进入。另在容器停止后以正式Store注入PENDING/SEND_ARMED边界，验证首次模拟通知/只记SEND_UNKNOWN；通知实际进入出口后强杀的证据由本机HTTP脚本提供，二者不冒充同一实验。JSONL按同一快照检查，退出需lifespan完成/无OOM/无错误；当前Uvicorn0.48会在正常关闭后重抛SIGTERM，容器exit143属此信号退出，不能用日志一句话把SIGKILL/137当正常。

需要保留容器手动测试时：

```powershell
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_local_setup.py --output runtime/wecom-docker/config.toml --scope wecom-local-docker-001
New-Item -ItemType Directory -Force runtime/wecom-docker/runtime,runtime/wecom-docker/logs | Out-Null
docker tag adp_channel_api:v0.0.9 adp-chat:wecom-local
docker compose -p adp-wecom-local -f compose.yaml -f compose.local.yaml config
docker compose -p adp-wecom-local -f compose.yaml -f compose.local.yaml up -d --no-build
Invoke-RestMethod http://127.0.0.1:18000/healthz
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_callback_client.py --config runtime/wecom-docker/config.toml --url http://127.0.0.1:18000 --user-id YNND --msg-id 20001 --message "容器本地测试"
# Fake控制面不发布端口，仅在命名空间内访问：
docker compose -p adp-wecom-local -f compose.yaml -f compose.local.yaml exec -T fake-adp python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8765/_test/status').read().decode())"
docker compose -p adp-wecom-local -f compose.yaml -f compose.local.yaml exec -T fake-adp python -c "import urllib.request;urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:8765/_test/release',data=b'')).close()"
docker compose -p adp-wecom-local -f compose.yaml -f compose.local.yaml restart adp-chat
docker compose -p adp-wecom-local -f compose.yaml -f compose.local.yaml logs --tail 30
docker compose -p adp-wecom-local -f compose.yaml -f compose.local.yaml down
```

生成前确认目录/文件不存在；docker tag前确认目标开发标签属于本任务，避免覆盖他人镜像。手动down保留本机配置/日志/消息；不会清理全局Docker资源，不运行prune。命令在项目根执行。[compose.local.yaml](../../compose.local.yaml)作为[compose.yaml](../../compose.yaml)的本机覆盖，按 [Compose官方合并规则](https://github.com/compose-spec/compose-spec/blob/main/13-merge.md)依赖支持`!override`的Compose（≥2.24.4），显式只发布loopback的18000。Fake持有稳定网络命名空间，网关加入其loopback；网关重启不遗留在旧命名空间的Fake，启动先等Fake健康。Fake自身重建时须一起重建该测试项目；不把这种测试拓扑用到F。配置只读映射/app/config.toml，runtime/logs映射各自测试目录。默认ARM64本地仿真不替代目标Linux/ARM服务器、网络/权限和正式企微真实验收；本节不执行上传或部署。

**阶段 F 检查**（单员工已真实接入，完整验收未关闭）：具体配置、后台操作和当前限制见1.11；保留独立生产状态目录、正式凭据、应用权限、目标机网络/架构及两员工真实验收边界。SEND_ACCEPTED仅能由正式API明确接受产生，不追踪已读；不得将测试目录改为production复用。

### 1.11 阶段F：公网IP与HTTP接入配置

本节按用户选择使用**公网IP和HTTP回调**，不要求新增域名或入站HTTPS。用户已在 CentOS Stream 9/x86_64 自行部署 v0.0.10，后台 GET 保存、YNND 的真实加密 POST/ADP/应用回复和切出后返回查看已通过。多用户在隔离模拟身份下验证，F1～F5完整状态、实际压力数据及未验证项仍以[docs/wecom-integration/02_实施路线图.md](../wecom-integration/02_实施路线图.md)为验收Owner。入站协议选择不改变Python调用ADP及企业微信API的既有HTTPS出站地址。

**先分清三个地址**：

| 地址 | 在哪里配置 | 如何填写 |
| --- | --- | --- |
| 服务宿主监听IP、发布端口 | 实际`config.toml`的`[deployment].bind_ip/host_port` | 选择服务器本机IP及发布端口；公网IP若经云NAT映射，不一定出现在网卡上，可绑定本机网卡IP或`0.0.0.0` |
| 企业微信入站回调URL | 实际`config.toml`的`wecom.callback_url`，再复制到后台“接收消息”的URL | 当前选择`http://115.190.218.12:3000/wecom/callback`；应用启动按其路径注册GET/POST，后台是否接受以保存校验为准 |
| 应用API可信IP | 企业微信应用后台“企业可信IP” | 填调用企业微信API时的固定公网出口；直接出网时可与入口IP相同，经NAT/代理时需核实 |

更换服务器只调整实际部署配置和必要的企业微信后台地址，不因IP不同修改源码。[scripts/run.sh](../../scripts/run.sh)与[scripts/upgrade.sh](../../scripts/upgrade.sh)读取服务器`release/config.toml`；常规部署不需要环境变量。若账户手工设置了`ADP_BIND_IP/ADP_HOST_PORT`，它们会覆盖对应字段，需先核对。源码Compose是另一个显式入口，仍按其环境变量发布端口，不读取TOML的`[deployment]`；本节服务器操作统一使用正式脚本。

**第一步：确认服务器运行条件并打包。** 用户已确认当前是CentOS Stream 9、`x86_64`，对应`linux/amd64`。镜像按CPU架构选择，同一镜像也可用于x86_64的Ubuntu；不需要把现有镜像基础系统改为CentOS。目标机还需确认Docker可用：

```bash
cat /etc/os-release
uname -m
docker info --format '{{.OSType}}/{{.Architecture}}'
```

在Windows项目根目录启动Docker Desktop的Linux引擎，然后运行：

```powershell
.\scripts\build_image.ps1 -Version v0.0.10 -Platform linux/amd64
```

`v0.0.10`是本次示例的新版本号；若目录或镜像标签已存在，换未使用的版本号，不覆盖旧包。脚本默认的`linux/arm64`保持原样，当前x86_64服务器必须显式传`-Platform linux/amd64`。生成`dist/v0.0.10/adp_channel_api-v0.0.10-linux-amd64.tar`、`manifest.json`、`SHA256SUMS`。按2.1把版本目录与源码`scripts/run.sh/stop.sh/upgrade.sh`上传到服务器`release`布局，私下准备`release/config.toml`，然后在该目录执行`bash ./run.sh v0.0.10`。此版本的 linux/amd64 镜像已由用户构建并自行部署；源代码与镜像 ID 的验收绑定见 Roadmap。后续重新打包必须使用未占用的新版本号。

宿主不必另装应用Python，容器内已有Python3.14。启动沿第二节的正式流程，按UID/GID10001准备持久化权限；CentOS还须依据实际SELinux状态检查挂载访问，不能只因操作系统名称相同就声称通过。系统时间需同步，回调验签有时间窗口。

宿主端口监听、安全组、系统防火墙和任何转发须形成完整入站路径。先从外部网络检查HTTP入口，再以后台GET保存校验为准；“服务器可以访问互联网”本身不证明指定端口已允许入站。公网入口只应开放`wecom.callback_url`的已验签路径（当前`/wecom/callback`）：原`/api/v1/chat`没有身份认证，不能把整套FastAPI路由无差别公开。可在现有HTTP反向代理或其他具备路径控制能力的入口收口，入站仍可保持HTTP；代理须保留查询参数和XML请求体，回调不记录完整查询串。不要仅靠启动进程或本机`/healthz`判定真实接入成功。

**第二步：在企业微信后台找到应用身份和权限。** 由有权限的管理员登录[企业微信管理后台](https://work.weixin.qq.com/)，选择相应企业：

1. “我的企业 → 企业信息”查看企业ID，对应`wecom.corp_id`。
2. “应用管理 → 自建 → 目标应用”查看AgentId和Secret，对应`wecom.agent_id`与`wecom.wecom_app_secret`。确认是现有目标应用，不是通讯录同步Secret或其他应用凭据。查看Secret时按后台提示完成管理员验证，私下填写实际配置，不发聊天或提交Git。
3. 在同一应用详情页确认应用启用，并在“可见范围”中加入`YNND`和`SIYU`；在“通讯录 → 成员详情”核对两人的实际UserID及必要接口权限。后台菜单名称以当前页面为准，不创建重复应用。
4. 在应用的“企业可信IP”设置中填写确认过的公网出口IP。这是服务器主动调用发送接口的限制，与回调接收地址分开。参考[官方身份/权限说明](https://developer.work.weixin.qq.com/document/path/90665)及[开发须知与可信IP](https://developer.work.weixin.qq.com/document/path/90664)。

**第三步：准备后台回调密钥并填写实际配置。** 在同一应用的“接收消息 → 设置API接收”（名称以当前后台为准）找到URL、Token和EncodingAESKey。当前目标应用的回调已保存成功。以后修改前仍须确认页面的配置归属，保留当前可用的值，不覆盖其他系统。

- 在`config.toml`填写`callback_url = "http://115.190.218.12:3000/wecom/callback"`，再把完全相同的值复制到后台URL，末尾不要带中文逗号。此配置实际决定应用启动注册的接收路径；留空或省略兼容`/wecom/callback`。更改后重启服务并同步后台，代码不会代替管理员修改后台，也不会自动修改宿主监听或网络转发。
- Token是自行生成并与服务器约定的英文/数字字符串，不超过32位；不是应用Secret，也不是access_token。
- EncodingAESKey使用后台“随机生成”的43位值，将完全相同的值写到服务器；它与本地A～E测试密钥分开。
- 先记下这些值并填好服务器、启动服务，再回后台保存，因为保存会立即发送GET验证。凭据仅保存在受限的实际配置中。[官方回调配置与验签流程](https://developer.work.weixin.qq.com/document/path/90930)是协议依据。

以[config.example.toml](../../config.example.toml)的现有字段为准，两份配置的分组、字段顺序和注释保持一致，只替换实际配置值；不要把以下字段重复追加成第二个`[wecom]`分组：

| 现有字段 | F正式接入时的值或处理 |
| --- | --- |
| `corp_id`、`agent_id` | 填同一目标企业和自建应用的实际标识 |
| `adp_application` | 上线前确定固定的生产逻辑应用名，以后部署、迁移或轮换AppKey时保持稳定；不沿用一次性测试作用域 |
| `gateway_enabled` | `true` |
| `callback_url` | 当前`"http://115.190.218.12:3000/wecom/callback"`，与后台URL一致；不允许凭据、查询、片段或现有API保留路径 |
| `callback_token`、`encoding_aes_key` | 与后台完全一致的正式回调值 |
| `gateway_environment` | `"production"` |
| `notification_mode` | `"wecom"` |
| `wecom_app_secret` | 目标自建应用的实际Secret |
| `messages_dir` | 新的独立生产目录；本机可用`runtime/wecom-production/messages`，服务器按2.1的`session_file="../runtime/session.json"`布局应使用`../runtime/wecom-production/messages`，确保落在正式runtime持久化挂载内 |

ADP继续使用已有`adp.app_key`和正式`wecom.adp_url`；先核查真实测试中出现的460702模型余额错误是否已处理。旧`adp.user_id/visitor_id`服务于原单用户入口，网关会从通过验签的员工消息派生各自身份，不需要给两名员工维护两份配置。发送器自动获取和缓存access_token，无须手动填新字段。正式日志建议`log_level="INFO"`、`log_question=false`、`log_answer_chars=0`。

**第四步：保存回调并完成真实验收。**

1. 按正式部署入口启动单实例、单worker；本机健康检查和目录读写正常后，从外部网络检查入口端口。
2. 回企业微信后台保存URL、Token、EncodingAESKey。成功说明GET验签和解密往返通过；失败先查入口转发、签名参数、密钥一致性和系统时间，不绕过验签。
3. `YNND`在企业微信目标应用中发送“我想改密码”，等待正式回复；下一轮发送“windows”，实读语义验证续接。`SIYU`同时走另一会话，核对不串消息。
4. 继续验收同用户BUSY、重复回调去重、ADP明确拒绝/UNKNOWN与用户释放。真实发送须同时核对`send_result.source="wecom_api"`和目标用户接受结果；`SEND_SIMULATED`不能当作企微成功。错误与无效接收人按[官方应用消息接口](https://developer.work.weixin.qq.com/document/path/90236)核查，不为补发而重新调用ADP。
5. 记录真实F1～F5结果及未验证项，再确定正式可用状态。测试问题不触发生产会议写入，不做未经本次批准的真实压力调用。

**阶段 F 已有的上线检查与低负载复验证据。** 生产入口为宿主 `0.0.0.0:3000→8000`，企业可信 IP 已按实际公网出口配置为 `115.190.218.12`。真实发送错误 `60020` 表示不安全的访问 IP；修正后台可信 IP 后等待生效，再由员工发新消息核对正式接受，不能把旧失败快照改回待发送或重新调用 ADP。API 接受与客户端已读是两个不同事实。

本轮发现公网仍能无凭据访问旧 `POST /api/v1/chat`（无效请求返回 422，没有调用 ADP），与架构的公网边界冲突。**在入口仅允许回调路径与必要健康检查，或为公网旧 API 建立明确鉴权前，F 不能关闭。** 单纯改端口、安全组或绑定 `0.0.0.0` 不能按 HTTP 路径隔离这两个接口；也不能把 `/api/v1/chat` 的固定身份当成员身份。

服务器保持现有单实例，不为了复验重启或制造发送网络故障。多用户模拟应使用独立测试 Token/AESKey、`gateway_environment="test"`、`notification_mode="simulated"` 和全新状态目录/固定测试应用范围；不得往生产回调伪造员工身份，或让模拟账号触发真实企微发送。服务器本轮只运行两个模拟员工的低并发真实 ADP；测试容器无宿主公开端口、上限 0.5 CPU/256 MiB，测试结束后删除。50/100 用户压力及故障注入放在本机，既有日常入口仍为：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
.\.venv\Scripts\python.exe -B -X utf8 tests/wecom_dispatch_smoke.py --users 100 --simulate-notifications --report runtime/wecom-f-offline.json
```

第二条仅使用 Fake ADP，不访问真实腾讯 ADP 或企业微信；本轮在与服务器相同的既有镜像内实际执行了该脚本的 100 用户模式。Docker 完整测试方式沿用1.10，不为 F 新建第二套配置来源。真实 QPS 错误 `460031` 已被现有离线 Fixture 覆盖，普通回归无需真实 API。公网错误验证没有发送合法单用户聊天输入。

本机 Docker Desktop 的 Windows bind 目录在并发替换时出现过读取异常：100 用户 Probe 的观察器曾退出 1，原批次 100 条请求/模拟通知曾经由当时的静态快照与 JSONL 核对闭合（历史证据）；没有重发。单独文件系统实验仅在 bind 目录复现读取错误，Linux `/tmp` 对照正常，具体 errno 与原 FileNotFoundError 不同，不能断言完全相同根因。读取观察失败应保留原请求标识并核查原状态，不能为了让测试通过而重投 ADP、改快照或放宽业务断言；该结果也不代表目标服务器文件系统已被大并发压测。

**后续迁移Ubuntu。** 停止旧实例并取得一致的配置/runtime快照后，迁移实际配置、完整生产消息状态和必要日志；保持企业/应用标识、固定`adp_application`及目录映射语义。核对Ubuntu的CPU架构、Docker、目录权限和网络，再启动唯一新实例。入口或出口改变时分别更新回调URL和可信IP并重新验收；旧新实例不能同时处理同一应用。回退同样先停新实例，保留UNKNOWN与发送状态，不能盲目重投已武装请求。

## 二、Linux 服务器部署

### 2.1 本机打包与手动上传

镜像名称固定为 `adp_channel_api`，正式标签为 `vX.Y.Z`（三段非负整数，不接受预发布或前导零）。四个源码脚本均保存在本地 [scripts/build_image.ps1](../../scripts/build_image.ps1)、[scripts/run.sh](../../scripts/run.sh)、[scripts/stop.sh](../../scripts/stop.sh)、[scripts/upgrade.sh](../../scripts/upgrade.sh)。

在 Windows 项目根目录，安装并启动 Docker Linux 引擎，然后指定新版本即可：

```powershell
.\scripts\build_image.ps1 -Version v0.0.5
```

构建脚本默认平台是 `linux/arm64`，适用于 ARM64 服务器；**不能据此认定当前生产服务器也是 ARM64**。阶段 F 已记录的 CentOS Stream 9/x86_64 服务器需要显式使用 `-Platform linux/amd64`。跨架构构建取决于本机builder的目标平台支持；确有其他架构用途时仍可显式覆盖 `-Platform linux/amd64`。脚本兼容 Windows PowerShell 5.1/PowerShell 7；生成 `dist/v0.0.5/adp_channel_api-v0.0.5-linux-arm64.tar`、`manifest.json`、`SHA256SUMS`。清单记录镜像、ID/配置摘要、平台、归档 SHA256、北京时间、源码 revision 和工作区是否有未提交修改。脚本拒绝覆盖同版本目录或已有镜像标签；失败只清理本次临时产物。打包不需要真实配置，构建上下文排除了正式配置、日志、会话和 dist。

用户手动上传整个版本目录和三个 sh 脚本；正式配置由用户在服务器本地维护。无需上传源码、Compose、`.env` 或第四个帮助脚本。三个 sh 以 LF 保存，可直接 `sh ./run.sh`，内部自动使用 Bash。

服务器建议布局：

```text
adp_channel_api/
├── log/adp_chat.log                  # 服务器真实文件，升级保留
├── runtime/session.json              # 服务器真实文件，升级保留
└── release/
    ├── config.toml                   # 长期复用的正式配置
    ├── run.sh
    ├── stop.sh
    ├── upgrade.sh
    ├── .deploy-state.json            # 验收成功后自动记录，无需手动编辑
    ├── v0.0.5/
    │   ├── adp_channel_api-v0.0.5-linux-arm64.tar
    │   ├── manifest.json
    │   └── SHA256SUMS
    └── v0.0.6/                       # 后续上传的新版本
```

服务器需要已启动的 Linux Docker Engine、Bash 4.3+、`flock`、`sha256sum`、GNU coreutils（含 `install`、`sort -V`）。无需宿主 Python、Git 或 Compose。目标架构须匹配镜像；Docker 与基础工具按 [Docker Engine 官方安装文档](https://docs.docker.com/engine/install/) 和服务器运维规范安装。本流程不自动连接服务器或上传文件。已有AMD64包不能通过改名变成ARM64；例如旧v0.0.4为AMD64时，应使用新版本号重新打包并上传完整目录。

### 2.2 固定配置与持久路径

将 [config.example.toml](../../config.example.toml) 复制为服务器 `release/config.toml`，填写正式 AppKey/UserId，并将对应路径改成：

```toml
[session]
session_file = "../runtime/session.json"

[logging]
log_file = "../log/adp_chat.log"

[deployment]
bind_ip = "10.102.70.82"
host_port = 4560
```

保留模板的其他字段和分组；不要重复创建已有 TOML 分组。相对路径始终以配置所在目录为基准，与执行脚本的工作目录无关。本地开发的模板仍使用 `runtime/`、`logs/`，无需修改本机正式配置。

`bind_ip` 为服务器真实拥有的 IPv4 地址字符串，`host_port` 为 1～65535 的 TOML 整数，不加引号。脚本拒绝未知部署字段、非法地址和端口，且在停旧容器前检查。绑定 `127.0.0.1` 只供服务器本机访问；跨机器调用填写真实内网地址。容器内部保持 `0.0.0.0:8000`。

上述设置把服务器 `runtime/`、`log/` 的父目录分别挂载到容器 `/app/runtime`、`/app/log`；配置始终只读挂载到 `/app/release/config.toml`，并通过 `ADP_CONFIG_FILE` 固定指定。文件实际保存在服务器目录里，容器删除或更换镜像不会删除这些文件。无需 `.env`。

也可在服务器配置中填写 Linux 绝对路径，例如 `/srv/adp-data/runtime/session.json`、`/var/log/adp_channel_api/events.log`；脚本按原绝对目录绑定，容器与宿主路径相同。支持带空格路径；不支持 Windows 路径及含逗号、冒号、换行的挂载路径。日志和会话必须位于专用数据目录，不能覆盖 release 配置目录或容器系统目录。升级期间不能更改已记录的会话路径。

有 Docker 访问权、且对 `release/` 和待建数据目录的父目录有写权限的普通账户可直接运行三个脚本，**无需 sudo，也不需要 sudo bash 授权**。先确认 `docker info` 能成功执行。程序不修改 sudoers、Docker 组或系统授权。

普通账户创建的新目录由本人拥有、属组为其主组，模式为 **2770**：setgid 使目录内新文件继承该组。应用仍以 **UID/GID=10001** 运行，只补充实际绑定配置/数据路径的属组以便读写；宿主不是root不影响容器保持非root。已有目录、日志和会话不递归chown/chmod。仍以root运行时，新建数据目录继续使用10001/750。

会话文件在Linux上以 **640** 保存：容器用户拥有读写权限，数据目录所属组可读，其他账户无权限。对应用自己拥有的普通文件，创建或恢复时将属组匹配到有权使用的数据目录组；同组的部署账户因此可用VS Code或cat直接查看。升级读取旧600会话时，先验证JSON及AppKey/UserId，再只调整元数据，保留原JSON字节和ConversationId。不会递归改其他文件，也不夺取外部属主；Windows沿用NTFS权限行为。

此项需要包含新存储代码的镜像；仅更新sh不能改变旧镜像内的文件创建策略。上传新版本后用upgrade.sh切换即可保留原会话并修权限；旧root专属目录仍需先有目录遍历权限。

正式配置保留运行账户所有权，分组可读即可，不再要求chown给root。例如：

```bash
cd /你的路径/adp_channel_api/release
docker info
chmod 640 config.toml
```

部署锁和状态文件由运行账户管理；状态仅在验收成功后原子更新，模式640，不执行宿主chown。配置与状态分别只读绑定为单文件，不要求把release目录开放给应用用户。解析配置、归档和状态的短生命周期元数据容器使用容器内root，仅有只读挂载、无网络和有限读取能力；主服务与数据预检仍为10001，交付容器不挂载Docker socket。

若旧部署留下当前账户无权写入的root专属锁/目录，需要先完成目录权限交接；脚本会明确拒绝，不自动接管无权写入的文件。这里验证的是普通账户访问已有rootful Docker的流程；rootless Docker/userns-remap的UID映射未实机验证。

已有数据目录权限不兼容时，脚本会在停机前失败，应核实所有权后按服务器规范调整；不要用 777 掩盖问题。首次迁移旧部署时，先停止使用同一会话文件的旧服务，将旧 `session.json` 保留在目标位置，保持 AppKey/UserId 和受支持的会话格式。不要同时启动 CLI、Compose 和本流程操作同一会话。

### 2.3 指定版本运行与停止

默认固定容器名 `adp_channel_api`，模板发布 `10.102.70.82:4560:8000`。实际地址和端口读取服务器配置；下面的访问命令也应替换为实际值。确认服务器真实拥有该地址和端口后执行：

```bash
bash ./run.sh v0.0.5
docker container ls --filter name=adp_channel_api
curl -fsS http://10.102.70.82:4560/healthz
bash ./stop.sh
```

`run.sh` 自动核对两项 SHA256、清单、平台及镜像内容；标签未加载时从离线归档加载。通过校验的不可变镜像 ID 用于实际启动；脚本兼容 classic 配置摘要与 containerd 索引摘要的 ID 表示。仅使用 SHA256 保证完整性，不提供发布者签名验证。

日常修改 `release/config.toml` 中的 `[deployment]` 后，执行 `bash ./upgrade.sh 当前版本号`，脚本会重新建立 Docker 端口映射；同版本、相同配置和健康状态下重复运行不会重建容器。无需另外设置 `.env` 或命令行监听参数。

逐字段读取优先级为：**非空环境变量 `ADP_BIND_IP`/`ADP_HOST_PORT` → TOML 中显式设置的字段 → 最近成功部署记录 → 原默认值 `10.102.70.82:4560`**。省略新分组或只填其中一项时，其他项仍按此规则回退；已填写但非法的 TOML 不会被环境变量掩盖。

原有环境变量仍可用于临时覆盖，例如：

```bash
ADP_BIND_IP=127.0.0.1 ADP_HOST_PORT=4560 bash ./run.sh v0.0.5
```

成功后实际 IP/端口记录在 `.deploy-state.json`。状态只为缺少显式配置的旧文件提供回退；如果 TOML 已指定地址/端口，下次无环境覆盖时会重新使用 TOML 值。不要把 `.deploy-state.json` 当成应用配置编辑或定期删除。

### 旧镜像升级并启用新配置

新分组需要包含新版配置加载器的镜像以及新版三个服务器脚本。此前已打包的旧镜像（包括原 `v0.0.6`）会拒绝 `[deployment]`；仅更新脚本不能改变旧镜像的加载行为。

1. 用当前源码按未使用的新版本号打包，上传完整版本目录和新版三个 sh 脚本，备份现有服务器配置。
2. 服务器采用目标版本支持的配置，可直接添加 `[deployment]`、`[chat].stream` 等目标镜像支持的字段；保留 AppKey/UserId、会话和日志路径，然后执行 `bash ./upgrade.sh 新版本号`。
3. 需要回退到不支持新字段的旧镜像时，先恢复与旧镜像兼容且身份/持久路径相同的配置备份，再显式指定旧版本升级。

脚本在停机前只预检目标镜像的配置、身份、会话和目录权限，旧镜像不认识新字段不再阻止升级。实际启动失败后仍会尝试恢复旧镜像；若旧镜像不支持当前配置，需要人工恢复旧配置。整个过程不需要新建或删除会话文件。

相同版本、配置和健康状态重复运行不会重建容器。其他版本正在运行时，`run.sh` 提示改用升级入口；`stop.sh` 仅停止此 release 根目录管理的固定容器，停止宽限为240秒，保留容器、配置和数据。同名但不属于本目录的容器会被拒绝操作。三个入口共用部署锁，不能并行运行。

### 2.4 验证实际进程、挂载和网络

```bash
docker container inspect adp_channel_api --format '{{.Image}} {{.State.Health.Status}}'
docker container inspect adp_channel_api --format '{{json .Mounts}}'
docker container logs --tail=100 adp_channel_api
docker container port adp_channel_api 8000
curl -i http://10.102.70.82:4560/healthz
```

脚本在真实运行容器中检查健康、镜像 ID、固定配置只读挂载、数据目录、UID/GID 和原会话一致性，通过后才原子记录成功。Docker 设置单 worker、`--init`、`unless-stopped` 和健康检查；健康窗口为75秒。`GET /healthz` 只检查应用初始化，不调用 ADP。业务 POST、服务器出网、跨机调用及网络 ACL 仍由部署环境验收。

本服务无 API 身份认证，使用明文 HTTP；应沿用可信内网访问边界，限制可访问端口的来源。容器常驻或重启不会恢复已经中断的 ADP 请求，也不能据此判断外部写入是否发生。

### 2.5 保留的源码 Compose 入口

已有 [compose.yaml](../../compose.yaml) 仍可用于本机/源码部署：项目根 `config.toml` 绑定 `/app/config.toml`，项目根 `runtime/`、`logs/` 持久化。使用者自行准备10001身份的可写目录，再执行 `docker compose up -d --build`。本节的离线版本流程不使用 Compose；请对同一会话选择一个运行入口。

## 三、其他电脑如何调用

### 3.1 Windows PowerShell

在 Windows PowerShell 中先确认端口连通：

```powershell
Test-NetConnection -ComputerName 10.102.70.82 -Port 4560
```

`TcpTestSucceeded=True` 只证明 TCP 能建连，不代表 ADP 成功。直接用 PowerShell：

```powershell
$body = @{ message = "你好" } | ConvertTo-Json -Compress
$response = Invoke-WebRequest -UseBasicParsing `
    -Uri "http://10.102.70.82:4560/api/v1/chat" `
    -Method Post -ContentType "application/json; charset=utf-8" `
    -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 240
$result = [Text.Encoding]::UTF8.GetString($response.RawContentStream.ToArray()) | ConvertFrom-Json
$result.text
$result.conversation_id
$result.request_id
```

请求正文和响应正文都按 UTF-8 处理。Windows PowerShell 5.1 可能把未声明 charset 的 JSON 响应按其他编码解读；仅设置请求的 `Content-Type` 不能解决响应乱码，因此这里显式解码原始响应字节。脚本不会自动重试。

仓库另提供可重复使用的 [examples/http_client.ps1](../../examples/http_client.ps1)：

```powershell
.\examples\http_client.ps1 -Server "http://10.102.70.82:4560" -Message "你好"
.\examples\http_client.ps1 -Server "http://10.102.70.82:4560" -Message "继续刚才的话题"
```

如果 PowerShell 执行策略禁止运行脚本，可以直接复制上面的 `Invoke-WebRequest` 示例；不需要为了执行示例而修改全机安全策略。

### 3.2 Python/其他后端服务

运行 [examples/http_client.py](../../examples/http_client.py)，**无需**安装服务器源码或第三方 HTTP SDK：

```bash
python examples/http_client.py "你好" --url http://10.102.70.82:4560
python examples/http_client.py "继续刚才的话题" --url http://10.102.70.82:4560
```

代码内部使用 Python 标准库 `urllib.request` 发送 JSON POST。脚本输出文本、ConversationId、RequestId 和 RecordId；错误返回非零退出码，**不自动重试**。其他系统可参照源码发起相同 HTTP 请求。

### 3.3 HTTP API 和结果

- **POST** `http://10.102.70.82:4560/api/v1/chat`；`Content-Type: application/json`；请求体 `{"message":"你好"}`。
- 正常响应 HTTP 200，含 `text`、`conversation_id`、`request_id`、`record_id`、`reply_method`、`reply_contents`；精确数据结构由 [src/api/schemas.py](../../src/api/schemas.py) 定义。`reply_contents` 保留非文本**回复内容**，但服务端**不支持上传**图片或文件。
- **GET** `http://10.102.70.82:4560/healthz` 检查本地运行状态，不调用 ADP。FastAPI 自动生成的接口文档可在可信内网访问 `/docs`。

一次 POST 只等当前 ADP 的正式回复。若 ADP 在文本里追问，你下次再发消息即可；多个客户端使用的都是**同一个 ConversationId**，没有隔离。

## 四、调试与故障排查

| 现象/状态 | 先检查什么 | 说明 |
| --- | --- | --- |
| TCP 连接不上 | 服务器 IP 是否属于网卡、`docker container ls --filter name=adp_channel_api`、端口 4560、ACL/路由 | 仅有容器运行不代表内网端口可达 |
| 健康接口失败 | `docker container logs --tail=100 adp_channel_api`、配置/会话/挂载权限 | 配置损坏或状态身份不符会阻止正常启动 |
| HTTP 422 | POST 是否合法 JSON，`message` 是否为非空文字 | 不会调用 ADP |
| HTTP 409 / CHAT_BUSY | 是否有另一轮正在处理 | 该请求没有进入 ADP；并发不排队 |
| HTTP 502 / ADP_ERROR | 腾讯 ADP 连接、SSE 完成事件、应用状态 | 不代表整体业务完成 |
| HTTP 503 / SERVICE_UNAVAILABLE | 错误中的 `stage`、日志路径和持久化权限 | 请求阶段资源异常；启动阶段配置/会话初始化失败则可能根本无法连接 HTTP |
| HTTP 504 / ADP_TIMEOUT | 网络链路、当前请求耗时、ADP 运行状态 | **不能断定 ADP 没执行**，不要盲目重发 |
| HTTP 500 | 服务端日志，必要时在本地重现 | 对外返回安全的通用错误 |
| ConversationId 与预期不同 | `runtime/session.json` 和 AppKey/UserId 是否一致 | 不要直接手改 UUID/指纹 |
| 升级提示会话路径与最近成功部署不同 | `release/config.toml`、`.deploy-state.json` 的 `session_file`、当前容器的会话路径标签 | 停机前的保护检查；按下文“会话路径不一致时如何排查”核对 |
| run.sh找不到linux-arm64.tar | `uname -m`、版本清单platform及实际文件名 | aarch64服务器需要ARM64镜像，按默认目标重新打包新版本，不能仅改文件名 |
| 启动报 Permission denied | UID/GID、`config.toml`、`runtime/`、`log/`（源码 Compose 为 `logs/`） | 不用 777 权限掩盖问题 |
| 容器重建后丢上下文 | 是否保留了挂载的 `runtime/session.json` | 不能把临时容器内文件当持久数据 |

[config.example.toml](../../config.example.toml) 是可提交的配置模板，AppKey留空；本机config.toml保存真实配置并被Git/Docker忽略。两份配置的分组、配置项、顺序及注释保持一致，凭据、身份、回调URL、部署地址等实际值按环境填写；服务器release配置还需保留该环境的../runtime、../log等路径，不能用本机根配置整份覆盖。

配置模板显式设置 `[logging].log_question=true`，启用后记录后续用户问题；`log_answer_chars` 独立限制回答正文长度。若不记录正文，设置 `log_question=false`、`log_answer_chars=0`。代码在省略这两项时仍默认关闭正文记录；DEBUG级别本身不会开启问题记录。已有日志不回填。AppKey及已知凭据URL保持脱敏，其他业务系统可能还有独立日志。

修改服务器现有release/config.toml的对应项后，执行 `bash ./upgrade.sh 当前版本号` 重新加载配置；保持原AppKey、身份和持久路径。

### 本地/服务器调试顺序

1. 配置和目录权限正常 → 通过 `GET /healthz`。
2. 用 `python main.py --ask "你好"` 验证单轮 ADP 之前，**先停止正在使用同一会话数据的 Docker 实例**。
3. 重启 HTTP 服务，使用 Windows/Python 示例连续调用两轮；检查 ConversationId 一致、RequestId 各不相同。
4. 只在本地测试环境用可控的慢请求验证 409，不要在生产会议预订中制造并发测试副作用。
5. 修改 `config.toml` 后重启服务使配置生效；不支持热加载。

## 五、停止、备份、升级与回滚

每次升级仍由用户上传新版本目录；三个脚本变化时同时更新服务器副本。保留 `release/config.toml`、`runtime/session.json` 和 `log/`，不要将新的模板覆盖到正式配置。停止前应让正在进行的业务请求结束，并将配置、会话和必要日志备份到权限受控的目录，记录 `.deploy-state.json` 与镜像版本。

```bash
cd /你的路径/adp_channel_api/release
bash ./upgrade.sh             # 按数字排序选择最高的 vX.Y.Z 目录
# 也可显式指定目标（包括人工回退）：
bash ./upgrade.sh v0.0.5
```

自动选择按整数版本排序，`v0.0.10` 高于 `v0.0.9`，忽略不合规目录和符号链接；最高目录上传不完整则直接失败，旧容器保持运行，不悄悄退选低版本。自动选择不会降级；显式指定旧版本用于兼容性确认后的人工回退。

切换顺序是：取得部署锁 → 校验归档/清单和平台 → 必要时加载目标镜像 → 用目标镜像校验固定配置、路径、目录权限和原会话 → 优雅停止并替换旧容器 → 等待新容器健康 → 在真实容器中复核会话、挂载和镜像 → 原子记录成功。**不会在加载新包前删除上一成功镜像。**

新容器启动/验收失败时，清理本次容器并尝试用原镜像、原端口和原会话恢复上一版本，仍读取**当前 `release/config.toml`**；即使恢复成功，升级命令仍返回非零，需检查失败原因。脚本只在实际恢复时检查旧镜像能否读取当前配置和原会话，升级开始前不做旧镜像兼容预检。脚本不保存旧配置副本，也不自动撤销配置修改；配置回退需要人工恢复升级前的受控备份，再重新运行升级入口验收，保持原 AppKey/UserId 和会话路径。若当前配置/会话已不兼容，自动恢复可能失败，会明确报告，不能靠清空会话继续。强制断电、SIGKILL、磁盘故障或外部同时修改数据不属于自动恢复保证范围。

升级逻辑由外置的 [scripts/run.sh](../../scripts/run.sh) 提供；仅更新部署脚本时，将最新版 `run.sh` 上传并覆盖服务器 `release/run.sh` 后重新执行升级即可，无需重打应用镜像。

成功后保留当前和上一成功版本的镜像；对更早且完整可核验、没有任何容器引用的版本，仅尝试删除对应镜像缓存标签。失败的缓存清理不会回滚成功部署，不使用全局 prune/强制删除。离线归档目录始终保留，人工回退需要时可重新加载。

`session.json` 保存会话格式、ConversationId、UserId 和 AppKey 指纹，**不保存每轮问题和回答**。升级验证原文件字节及 ConversationId 保持不变，因而继续复用同一个云端会话；ADP 的上下文窗口、历史可用性和应用设置仍决定模型能引用多少历史，不能保证全部历史永久进入上下文。相关服务端机制见 [src/storage/session.py](../../src/storage/session.py)。

已有部署若记录过成功状态或仍有受管容器，升级发现会话缺失、损坏、身份不符或路径变化会在停旧容器前失败。镜像升级沿用原会话路径；当前三个脚本没有迁移已有部署路径的子命令。

### 会话路径不一致时如何排查

报错表示配置解析出的服务器会话路径与最近成功部署记录不一致，不代表 `session.json` 已丢失。常见原因是将本机配置上传覆盖服务器配置：在 `release/config.toml` 中，`runtime/session.json` 指向 `release/runtime/session.json`，而 `../runtime/session.json` 指向 release 的同级 runtime 目录。

1. 在服务器实际 release 目录执行以下只读检查。使用自定义容器名时，将示例中的 `adp_channel_api` 换成实际名称；没有该容器时跳过 inspect，以成功状态和备份为依据。

```bash
cd /你的路径/adp_channel_api/release
pwd -P
grep -nE '^\[session\]|^[[:space:]]*session_file[[:space:]]*=' config.toml
if [ -f .deploy-state.json ]; then
    grep -nE '"(release_dir|session_file)"[[:space:]]*:' .deploy-state.json
fi
docker container inspect adp_channel_api --format '{{index .Config.Labels "io.nnit.adp.session-file"}}'
```

2. 将配置中的 `session_file` 值作为下面命令的参数，从 release 目录计算绝对路径，再与状态文件和容器标签比较。示例使用服务器推荐值：

```bash
realpath -ms -- '../runtime/session.json'
```

3. 如果只是误改了路径，将 `[session].session_file` 恢复为指向最近成功部署位置的值，保留其他身份配置。确认原位置的会话文件仍存在、可读且格式有效后，再执行 `bash ./upgrade.sh 目标版本号`；脚本会继续核对原 ConversationId 和文件哈希。原文件确实缺失时，先从对应备份恢复，不能靠新建会话通过升级。
4. 若确实要搬迁数据目录，需要单独的停机迁移方案，覆盖配置/状态/会话备份、新旧文件哈希与 ConversationId 核对、权限检查，以及受管容器和部署记录的一致切换。在迁移步骤准备并验证前继续沿用原路径。不要通过删除 `.deploy-state.json`、修改会话 UUID/指纹或执行 `--new-session` 绕过检查；这些操作不能完成保留原会话的路径迁移。

路径检查和失败恢复的实现均在 [scripts/run.sh](../../scripts/run.sh)。上述排查不自动修改服务器文件。

## 六、实际交付与未验证范围

代码和部署配置在仓库中，但**服务器 10.102.70.82:4560 的 Docker 构建、外部 ADP 连通、Windows 跨机调用和网络 ACL 限制必须在真实环境重新验证**。现有 Windows CI 已安装 `requirements-dev.txt`，运行全部 Python/API 回归；如果本地因缺少 FastAPI/HTTPX 跳过测试，仍需补齐依赖重跑。Python/API 测试不能替代 Linux 镜像与实际挂载、网络的验证。使用者自行完成这些步骤后，才能确认部署成功。


离线部署回归使用 [tests/deploy_smoke.py](../../tests/deploy_smoke.py)，仅在本机 Docker 的临时 Linux 目录测试，不连接服务器或 ADP：

```powershell
# 本机amd64部署回归需两个显式-Platform linux/amd64生成的版本包，以及Linux Docker CLI（二者任选）：
python tests/deploy_smoke.py --first dist/v0.0.2 --second dist/v0.0.3 --cli-image docker:29.6.2-cli
# 已有独立 Linux CLI 文件时：
python tests/deploy_smoke.py --first dist/v0.0.2 --second dist/v0.0.3 --cli-binary /你的本机路径/docker
```

`--cli-image` 路径需先获取该工具镜像；`--cli-binary` 必须是与测试 Linux 环境匹配的 Docker 客户端。测试控制器临时使用本机 Docker socket 来运行真实服务，交付容器和服务器脚本不挂载 socket。测试覆盖正常启动、重启/升级会话复用、数字版本排序、校验失败、身份/路径漂移、会话缺失、操作锁、容器归属保护、失败恢复和自定义绝对路径。


验证普通账户部署可在上述命令增加 `--unprivileged`：

```powershell
python tests/deploy_smoke.py --first dist/v0.0.2 --second dist/v0.0.3 --cli-image docker:29.6.2-cli --unprivileged
```

测试控制器仅在准备fixture时使用root，实际执行run/stop/upgrade的进程为UID/GID1000，只保留测试Docker socket访问组；配置由该账户以640维护，验证私有release目录、目录创建、调用者拥有状态、容器10001身份、升级/重启/恢复及session字节保持。省略该开关仍回归原root调用方式。权限脚本单独上传更新即可，镜像内应用未改时无需重新打包镜像。


会话权限的隔离Linux角色验收入口为 [tests/session_permissions_smoke.py](../../tests/session_permissions_smoke.py)：

```powershell
python tests/session_permissions_smoke.py --image adp_channel_api:v0.0.6
```

使用镜像内正式存储代码，以应用10001创建/恢复、部署用户1000直接读取，检查640与目录属组、旧600迁移不改JSON/CID、错误身份/数据不先开放权限、外部属主保持及失败原子保存不覆盖旧会话。只用容器内临时fixture，不挂载正式数据、不访问ADP。
