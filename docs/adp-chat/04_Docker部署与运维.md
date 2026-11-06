# Docker 部署、环境准备、调试与内网调用

本指南面向拿到源码、需要自行部署并使用的人。**先在本地验证，再部署到 Linux 服务器，最后从另一台电脑调用**。尚未实际运行的环境步骤不能视为已经验收成功。

## 系统怎样工作

```text
Windows / 其他内网服务
    │ HTTP POST（不带 Token）
    ▼
10.102.70.82:4560（Linux 宿主机）
    │ Docker Compose 端口映射
    ▼
容器 0.0.0.0:8000
    │ FastAPI → 单用户 ChatService → ADP HTTP SSE
    ▼
腾讯 ADP 云端返回正式 response.completed
    │
    └──── HTTP JSON 回到调用方
```

宿主端口 **4560** 与容器端口 **8000** 不需要相同。宿主 IP **10.102.70.82** 必须是服务器真实拥有、且其他内网客户端可到达的网卡地址。这一版没有 API Key/HTTPS/代理，仅适用于网络访问已受限制的可信内网。

正式机器配置见 [compose.yaml](../../compose.yaml)、[Dockerfile](../../Dockerfile)、[config.example.toml](../../config.example.toml)。运行代码在 [adp_chat/api/app.py](../../adp_chat/api/app.py) 和 [adp_chat/application/chat_service.py](../../adp_chat/application/chat_service.py)。

## 一、先在本地验证三种入口

### 1.1 Windows 或 PyCharm 环境准备

1. 安装 **Python 3.14**，在 PowerShell 中确认：`py -3.14 --version`。如果这条命令未识别到 3.14，请先安装该版本，不要用旧版本的成功测试冒充正式运行验证。
2. 获取仓库源码并进入包含 [main.py](../../main.py) 的项目根目录。
3. 新建虚拟环境，安装正式运行和开发测试依赖，并复制配置：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item config.example.toml adp_chat/config.toml
```

4. 打开 `adp_chat/config.toml`，至少填写：
   - `[adp].app_key`：腾讯 ADP 已发布应用的 AppKey。
   - `[adp].user_id`：本服务唯一的固定 ADP UserId。
   - `[adp].visitor_id`：可留空，沿用现有单用户客户端处理。
   - `[session].session_file = "../runtime/session.json"` 和 `[logging].log_file = "../logs/adp_chat.log"`：路径相对于配置文件目录。
5. 不要提交真实配置，复制前确认新文件不存在，避免覆盖已有密钥/会话配置。

本地 Python 进程使用包内 `adp_chat/config.toml`；Docker 则将宿主 `deploy/config.toml` **只读挂载到同一容器路径**，因此两种启动方式使用相同的配置解析代码。

### 1.2 单次脚本

```powershell
.\.venv\Scripts\python.exe main.py --ask "你好"
```

这次运行只向 ADP 发送一条文字，收到正式回复后结束进程。也可运行 `python main.py` 先输入一条文字。第二次启动会恢复 `runtime/session.json` 中的 ConversationId；RequestId 则由每轮新建。

### 1.3 交互式 CLI

```powershell
.\.venv\Scripts\python.exe -m adp_chat
```

进入后连续输入文字，每条消息只发送一轮；`/exit` 或 `/quit` 退出，`/new` **人工开启新会话**。不要在 FastAPI 容器仍使用同一个 `runtime/session.json` 时操作 `/new` 或发消息，否则进程内锁无法协调这两个独立进程。

### 1.4 本地 FastAPI 与 PyCharm 调试

在单独终端启动：

```powershell
.\.venv\Scripts\python.exe -m uvicorn adp_chat.api.app:app --host 127.0.0.1 --port 8000 --workers 1
```

这个进程会持续运行，按 Ctrl+C 停止。再打开第二个 PowerShell：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
.\.venv\Scripts\python.exe examples/http_client.py "你好" --url http://127.0.0.1:8000
```

在 PyCharm 中选择 `.venv` 对应的 Python 3.14 解释器，运行/调试 `main.py` 并设置参数 `--ask "你好"`；调试 Web API 时把运行模块设为 `uvicorn`、参数设为 `adp_chat.api.app:app --host 127.0.0.1 --port 8000 --workers 1`，工作目录设为仓库根。可以在 [adp_chat/api/routes.py](../../adp_chat/api/routes.py)、[adp_chat/application/chat_service.py](../../adp_chat/application/chat_service.py) 和 [adp_chat/adp/client.py](../../adp_chat/adp/client.py) 设置断点。

### 1.5 本地测试

```powershell
.\.venv\Scripts\python.exe -B -X utf8 -m unittest discover -s tests -v
```

`requirements-dev.txt` 安装了 FastAPI/HTTPX 测试依赖。请检查测试最后的通过/失败/**跳过**数量；如果 `tests/test_api.py` 因依赖未安装跳过，不可以认为 Web API 已验证。额外可运行：

```powershell
.\.venv\Scripts\python.exe -m py_compile examples/http_client.py
```

## 二、Linux 服务器部署

### 2.1 服务器前提和代码获取

- Linux 服务器真实拥有 IP `10.102.70.82`，允许你从其他内网设备访问 TCP 4560，并允许服务器向腾讯 ADP 访问外部 HTTPS。
- 服务器已安装 Docker Engine、`docker compose` 插件、Git 和 curl，Docker 服务已启动；无 Docker 时按 [Docker Engine 官方安装文档](https://docs.docker.com/engine/install/) 安装，切勿把尚未安装写成已完成。
- 私有 GitHub 仓库需要有效的只读访问权限，例如经过授权的 SSH Deploy Key/SSH 用户凭据；命令中的 SSH 地址不能代替授权。

连接服务器后确认：

```bash
ip -4 addr
docker --version
docker compose version
docker info
```

如 Docker 命令需要 sudo，请使用服务器认可的运维权限方案；不要为了简化操作把普通用户直接加入高权限 Docker 组而忽略安全影响。

在合适的部署目录获取源码：

```bash
git clone git@github.com:dingyuwen77/adp_chat.git
cd adp_chat
git branch --show-current
git rev-parse --short HEAD
```

应该位于 `main`。以下命令都在仓库根目录执行。

### 2.2 配置与持久数据

```bash
mkdir -p deploy runtime logs
cp -n config.example.toml deploy/config.toml
```

检查 `deploy/config.toml` 是真实普通文件，不是目录。使用服务器上的编辑器填写 `[adp].app_key` 与 `[adp].user_id`；其他仍使用模板中的配置。请勿把密钥写进 `compose.yaml`、Git、构建日志或聊天截图。

容器通过 [Dockerfile](../../Dockerfile) 以非 root 用户 **UID=10001、GID=10001** 运行，因此挂载目录必须对该身份可写，配置文件必须可读：

```bash
sudo chown -R 10001:10001 runtime logs
sudo chmod 750 runtime logs
sudo chown root:10001 deploy/config.toml
sudo chmod 640 deploy/config.toml
ls -ld runtime logs
ls -l deploy/config.toml
```

如果服务器策略禁止修改文件属主，应由管理员采用等效的受限 UID/GID、ACL 或容器挂载权限方案；**不要执行 `chmod -R 777`**。修改文件权限前先确认现有文件不属于其他工作负载。

已有本地或旧部署的 `runtime/session.json` 必须在旧服务停机后迁入同一路径，且配置中的 AppKey 与 UserId 保持一致，才能恢复原有上下文。会话损坏或身份不匹配时启动应明确失败，不能悄悄覆盖。

### 2.3 端口绑定与启动

[compose.yaml](../../compose.yaml) 默认发布 `10.102.70.82:4560:8000`，无需改 Python 代码。**仅在 IP 是本机网卡地址且已确认访问来源范围时**启动：

```bash
docker compose config
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=100 adp-chat
curl -fsS http://10.102.70.82:4560/healthz
```

如果服务器实际 IP 不同、4560 已占用，或者只想本机测试，可在项目根目录建立未纳入 Git 的 `.env`，例如：

```dotenv
ADP_BIND_IP=127.0.0.1
ADP_HOST_PORT=4560
```

改成 `127.0.0.1` 后其他电脑**不能**通过服务器内网 IP 连接；需要跨机器调用时应改回本机实际内网地址（例如 `10.102.70.82`）。

第一次启动不要同时启动第二个 FastAPI/CLI 实例操作同一会话文件。`docker compose up -d` 使服务常驻，`restart: unless-stopped` 在 Docker 允许时恢复已退出的容器，但**不会恢复已中断的单次 ADP 网络请求**。

### 2.4 确认进程、挂载和网络

```bash
docker compose ps
docker compose logs -f --tail=100 adp-chat
docker compose port adp-chat 8000
curl -i http://10.102.70.82:4560/healthz
```

另一个终端可核对宿主机 4560 监听状态和实际 IP：

```bash
ip -4 addr
sudo ss -lntp | grep ':4560'
```

`GET /healthz` 返回 `{"status":"ok"}` 仅说明 Web 应用已初始化，并不检查腾讯 ADP。验证业务需要一次真实 POST（见下一节）。如果 Docker 状态 `unhealthy`，查容器日志、`deploy/config.toml` 和挂载权限；`restart: unless-stopped` 不代表 unhealthy 会自动重建。

**安全前提**：本版无身份认证且使用明文 HTTP；请让现有防火墙/网络 ACL 只允许可信内网来源访问 4560，不做公网映射。Docker 发布端口可能不受普通 UFW 规则完全约束，需从授权的来源和非授权网络实际验证，而不能只凭 10.x 地址推断隔离有效。

## 三、其他电脑如何调用

### 3.1 Windows PowerShell

在 Windows PowerShell 中先确认端口连通：

```powershell
Test-NetConnection -ComputerName 10.102.70.82 -Port 4560
```

`TcpTestSucceeded=True` 只证明 TCP 能建连，不代表 ADP 成功。直接用 PowerShell：

```powershell
$body = @{ message = "你好" } | ConvertTo-Json -Compress
$response = Invoke-RestMethod -Uri "http://10.102.70.82:4560/api/v1/chat" -Method Post -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 240
$response.text
$response.conversation_id
$response.request_id
```

仓库另提供可重复使用的 [examples/http_client.ps1](../../examples/http_client.ps1)：

```powershell
.\examples\http_client.ps1 -Server "http://10.102.70.82:4560" -Message "你好"
.\examples\http_client.ps1 -Server "http://10.102.70.82:4560" -Message "继续刚才的话题"
```

如果 PowerShell 执行策略禁止运行脚本，可以直接复制上面的 `Invoke-RestMethod` 示例；不需要为了执行示例而修改全机安全策略。

### 3.2 Python/其他后端服务

运行 [examples/http_client.py](../../examples/http_client.py)，**无需**安装 `adp_chat` 或第三方 HTTP SDK：

```bash
python examples/http_client.py "你好" --url http://10.102.70.82:4560
python examples/http_client.py "继续刚才的话题" --url http://10.102.70.82:4560
```

代码内部使用 Python 标准库 `urllib.request` 发送 JSON POST。脚本输出文本、ConversationId、RequestId 和 RecordId；错误返回非零退出码，**不自动重试**。其他系统可参照源码发起相同 HTTP 请求。

### 3.3 HTTP API 和结果

- **POST** `http://10.102.70.82:4560/api/v1/chat`；`Content-Type: application/json`；请求体 `{"message":"你好"}`。
- 正常响应 HTTP 200，含 `text`、`conversation_id`、`request_id`、`record_id`、`reply_method`、`reply_contents`；精确数据结构由 [adp_chat/api/schemas.py](../../adp_chat/api/schemas.py) 定义。`reply_contents` 保留非文本**回复内容**，但服务端**不支持上传**图片或文件。
- **GET** `http://10.102.70.82:4560/healthz` 检查本地运行状态，不调用 ADP。FastAPI 自动生成的接口文档可在可信内网访问 `/docs`。

一次 POST 只等当前 ADP 的正式回复。若 ADP 在文本里追问，你下次再发消息即可；多个客户端使用的都是**同一个 ConversationId**，没有隔离。

## 四、调试与故障排查

| 现象/状态 | 先检查什么 | 说明 |
| --- | --- | --- |
| TCP 连接不上 | 服务器 IP 是否属于网卡、`docker compose ps`、端口 4560、ACL/路由 | 仅有容器运行不代表内网端口可达 |
| 健康接口失败 | `docker compose logs --tail=100 adp-chat`、配置/会话/挂载权限 | 配置损坏或状态身份不符会阻止正常启动 |
| HTTP 422 | POST 是否合法 JSON，`message` 是否为非空文字 | 不会调用 ADP |
| HTTP 409 / CHAT_BUSY | 是否有另一轮正在处理 | 该请求没有进入 ADP；并发不排队 |
| HTTP 502 / ADP_ERROR | 腾讯 ADP 连接、SSE 完成事件、应用状态 | 不代表整体业务完成 |
| HTTP 504 / ADP_TIMEOUT | 网络链路、当前请求耗时、ADP 运行状态 | **不能断定 ADP 没执行**，不要盲目重发 |
| HTTP 500 | 服务端日志，必要时在本地重现 | 对外返回安全的通用错误 |
| ConversationId 与预期不同 | `runtime/session.json` 和 AppKey/UserId 是否一致 | 不要直接手改 UUID/指纹 |
| 启动报 Permission denied | UID/GID、`deploy/config.toml`、`runtime/`、`logs/` | 不用 777 权限掩盖问题 |
| 容器重建后丢上下文 | 是否保留了挂载的 `runtime/session.json` | 不能把临时容器内文件当持久数据 |

日志默认不记录完整问题/回答或 AppKey；保留关联请求 ID 和异常分类。注意操作系统、代理和其他业务系统可能有独立的访问日志，避免在这些系统额外记录敏感内容。

### 本地/服务器调试顺序

1. 配置和目录权限正常 → 通过 `GET /healthz`。
2. 用 `python main.py --ask "你好"` 验证单轮 ADP 之前，**先停止正在使用同一会话数据的 Docker 实例**。
3. 重启 HTTP 服务，使用 Windows/Python 示例连续调用两轮；检查 ConversationId 一致、RequestId 各不相同。
4. 只在本地测试环境用可控的慢请求验证 409，不要在生产会议预订中制造并发测试副作用。
5. 修改 `config.toml` 后重启服务使配置生效；不支持热加载。

## 五、停止、备份、升级与回滚

停止服务：

```bash
docker compose stop adp-chat
# 不再需要运行容器时：
docker compose down
```

备份前应确保没有进行中的 ADP 请求。使用访问权限受控的目录保存 `deploy/config.toml`（含密钥）、`runtime/session.json`（会话身份）、必要的 `logs/`，并记录旧 Git commit/镜像标识。**不要提交备份到 Git**。

升级（代码检查及真实环境验收仍由部署者执行）：

```bash
git status --short
git branch --show-current
git rev-parse --short HEAD
docker compose stop adp-chat
git fetch origin
git switch main
git pull --ff-only
docker compose up -d --build
docker compose ps
```

若有未提交修改，应先保护现有工作，不能用 `git reset --hard` 清除。升级后按 `/healthz`、真实 POST、再次重启和会话一致性逐项验收；真实会话数据保留在宿主机，不随镜像更新删除。

回滚：先停止新实例，切回记录的旧 Git revision/镜像版本并重新构建启动，保持同一份受保护的持久数据。若 AppKey/UserId 或会话格式发生变化，必须先确认相容性，不允许静默覆盖或重建旧会话。进程中断时，曾向 ADP/会议 MCP 发出的请求结果可能不确定，不能把服务重启当成业务回滚。

## 六、实际交付与未验证范围

代码和部署配置在仓库中，但**服务器 10.102.70.82:4560 的 Docker 构建、外部 ADP 连通、Windows 跨机调用和网络 ACL 限制必须在真实环境重新验证**。当前已有 CI 仅验证其真实安装的依赖范围；对于因缺少 FastAPI/HTTPX 依赖而跳过的 API 测试，应在安装 `requirements-dev.txt` 的环境补跑。使用者自行完成这些步骤后，才能确认部署成功。
