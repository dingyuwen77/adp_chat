# adp_chat：腾讯 ADP 单用户文字对话转发

本项目把 **同一套腾讯 ADP 对话能力**提供成三种运行方式：单次脚本、交互式 CLI 和 Linux Docker 常驻 FastAPI。每次只向 ADP 发送一轮文字；ADP 需要追问时，由调用方下一轮继续发消息。Python 不自动判断整项业务是否结束。

**使用前提**：Python 3.14；已发布且可访问的腾讯 ADP 应用 AppKey、固定 UserId；在服务器部署时另需 Docker Engine、Docker Compose 插件和允许访问腾讯 ADP 的网络。

## 从哪里开始

| 我想做什么 | 该看哪里 |
| --- | --- |
| 本地装依赖、配置、PyCharm/CLI 调试 | [docs/adp-chat/04_Docker部署与运维.md](docs/adp-chat/04_Docker部署与运维.md#一先在本地验证三种入口) |
| 在 Linux 服务器 `10.102.70.82:4560` 部署 | [docs/adp-chat/04_Docker部署与运维.md](docs/adp-chat/04_Docker部署与运维.md#二linux-服务器部署) |
| 从 Windows、Python 或其他服务调用 | [docs/adp-chat/04_Docker部署与运维.md](docs/adp-chat/04_Docker部署与运维.md#三其他电脑如何调用) |
| API、会话与并发机制 | [docs/adp-chat/03_FastAPI服务架构.md](docs/adp-chat/03_FastAPI服务架构.md) |
| 可直接运行的 HTTP 调用脚本 | [examples/http_client.py](examples/http_client.py)、[examples/http_client.ps1](examples/http_client.ps1) |

## 本地五分钟上手

在仓库根目录，先安装 Python 3.14（Windows 的 `py -3.14` 仅在该版本已安装时适用）：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
Copy-Item config.example.toml adp_chat/config.toml
```

编辑未纳入 Git 的 `adp_chat/config.toml`，至少填写 `[adp].app_key` 和 `[adp].user_id`；不要将真实密钥提交到仓库。

```powershell
.\.venv\Scripts\python.exe main.py --ask "你好"          # 一轮后退出
.\.venv\Scripts\python.exe -m adp_chat                # 连续输入，/exit 退出
.\.venv\Scripts\python.exe -m uvicorn adp_chat.api.app:app --host 127.0.0.1 --port 8000 --workers 1
```

第三条命令启动本机 HTTP 服务；**它需要保持运行**。在另外一个终端：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
```

预期返回 `status = ok`（只证明本机 HTTP 存活，不代表腾讯 ADP 可用）。详细的调用与错误示例见运维文档。

## 服务器部署速览

服务器 `10.102.70.82` 的宿主端口是 **4560**，Docker 容器内部端口是 **8000**。

```bash
git clone git@github.com:dingyuwen77/adp_chat.git
cd adp_chat
mkdir -p deploy runtime logs
cp config.example.toml deploy/config.toml
# 先编辑 deploy/config.toml，再按运维文档设置 UID/GID 10001 的文件权限
docker compose config
docker compose up -d --build
docker compose ps
curl -fsS http://10.102.70.82:4560/healthz
```

**不可跳过**：上述命令只有在服务器安装 Docker、拥有该内网 IP、具备访问权限并已配置真实 AppKey/持久目录权限后才能运行。复制配置后未填写 AppKey 就启动会失败。请先读完整的 [docs/adp-chat/04_Docker部署与运维.md](docs/adp-chat/04_Docker部署与运维.md)，其中包含逐步权限命令、调用和回滚。

从你的 Windows 电脑调用：

```powershell
Test-NetConnection 10.102.70.82 -Port 4560
.\examples\http_client.ps1 -Server "http://10.102.70.82:4560" -Message "你好"
```

使用 Python 3 标准库从任意可信内网客户端调用：

```bash
python examples/http_client.py "你好" --url http://10.102.70.82:4560
```

## 服务行为和限制

- `POST /api/v1/chat` 请求为 `{"message":"你好"}`，响应包括 `text`、`conversation_id`、`request_id`、`record_id`、`reply_method` 和 `reply_contents`；只在 ADP 的 `response.completed` 成功后返回 200。
- `GET /healthz` 是轻量健康检查，不调用腾讯 ADP；FastAPI 的交互文档默认在 `/docs`。
- `runtime/session.json` 维持固定 UserId/AppKey 的单个 ConversationId；容器重建不应丢失该文件。三种模式不能**同时**操作同一份会话数据。
- 服务使用一把进程内互斥锁；单实例单 worker，同一时刻只能处理一个 ADP 请求，忙碌的第二条消息立即返回 HTTP **409**。不支持多用户、多会话、上传、队列或自动重试。
- **无 X-API-Key 或 HTTPS**。任何能够到达服务端口的内网程序都可以使用该固定 ADP 身份。必须由服务器网络边界限制访问来源，严禁直接暴露公网。
- 超时、断流、容器退出后不能推断 ADP 或下游 MCP 未执行；涉及订会、改会等写入操作时，不应盲目重试，业务幂等仍需后续验证。

正式对话入口在 [adp_chat/adp/client.py](adp_chat/adp/client.py)，服务编排在 [adp_chat/application/chat_service.py](adp_chat/application/chat_service.py)，HTTP 入口在 [adp_chat/api/routes.py](adp_chat/api/routes.py)，本地 CLI 在 [adp_chat/cli/runner.py](adp_chat/cli/runner.py)。

## 测试

```bash
python -B -X utf8 -m unittest discover -s tests -v
```

在已安装 `requirements-dev.txt` 的环境运行以覆盖 FastAPI TestClient / HTTPX；**检查 skipped 是否为零，并核实每个跳过原因**。现有 Windows CI 没有安装 FastAPI 测试依赖，不能把其绿色状态理解为 API/Docker 实际验收完成。本次没有增加或修改 CI workflow。

机器事实见 [config.example.toml](config.example.toml)、[requirements.txt](requirements.txt)、[requirements-dev.txt](requirements-dev.txt)、[compose.yaml](compose.yaml)、[Dockerfile](Dockerfile)。其他专题见 [docs/README.md](docs/README.md)。
