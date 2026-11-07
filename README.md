# adp_chat：腾讯 ADP 单用户文字对话转发

本项目把 **同一套腾讯 ADP 对话能力**提供成三种运行方式：单次脚本、交互式 CLI 和 Linux Docker 常驻 FastAPI。每次只向 ADP 发送一轮文字；ADP 需要追问时，由调用方下一轮继续发消息。Python 不自动判断整项业务是否结束。

**使用前提**：本地开发使用 Python 3.14；已发布且可访问的腾讯 ADP 应用 AppKey、固定 UserId。离线镜像部署的服务器需要 Linux、Docker Engine、Bash 和允许访问腾讯 ADP 的网络，无需安装 Python、Git 或 Compose。

源码集中在 `src/`，该目录只作为源码根，不是运行模块；内部业务目录采用命名空间组织，不需要空 `__init__.py`。构建与 `adp-chat` 入口由 [pyproject.toml](pyproject.toml) 声明。`main.py`、正式 `config.toml`、`logs/` 和 `runtime/` 均在项目根目录。开发环境通过 `requirements-dev.txt` 安装应用后，单次脚本按源码位置定位根配置；正式镜像安装应用，并通过 `ADP_CONFIG_FILE=/app/config.toml` 绑定外置根配置。两者均不依赖当前工作目录。PyCharm 直接运行 `main.py` 即可输入问题，不需要额外的 `--config` 参数。

## 从哪里开始

| 我想做什么 | 该看哪里                                                                                                    |
| --- |---------------------------------------------------------------------------------------------------------|
| 本地装依赖、配置、PyCharm/CLI 调试 | [docs/adp-chat/04_Docker部署与运维.md](docs/adp-chat/04_Docker部署与运维.md#一先在本地验证三种入口)                          |
| 在 Linux 服务器 `10.102.70.82:4560` 部署 | [docs/adp-chat/04_Docker部署与运维.md](docs/adp-chat/04_Docker部署与运维.md#二linux-服务器部署)                         |
| 从 Windows、Python 或其他服务调用 | [docs/adp-chat/04_Docker部署与运维.md](docs/adp-chat/04_Docker部署与运维.md#三其他电脑如何调用)                           |
| API、会话与并发机制 | [docs/adp-chat/03_FastAPI服务架构.md](docs/adp-chat/03_FastAPI服务架构.md)                                      |
| 可直接运行的 HTTP 调用脚本 | [examples/http_client.py](examples/http_client.py)、[examples/http_client.ps1](examples/http_client.ps1) |

## 本地五分钟上手

在仓库根目录，先安装 Python 3.14（Windows 的 `py -3.14` 仅在该版本已安装时适用）：

```powershell
py -3.14 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
if (-not (Test-Path config.toml)) { Copy-Item config.example.toml config.toml }
```

编辑未纳入 Git 的根目录 `config.toml`，至少填写 `[adp].app_key` 和 `[adp].user_id`；不要将真实密钥提交到仓库。

```powershell
.\.venv\Scripts\python.exe main.py --ask "你好"          # 一轮后退出
.\.venv\Scripts\adp-chat.exe                # 连续输入，/exit 退出
.\.venv\Scripts\python.exe -m uvicorn api.app:app --host 127.0.0.1 --port 8000 --workers 1
```

第三条命令启动本机 HTTP 服务；**它需要保持运行**。在另外一个终端：

```powershell
Invoke-RestMethod http://127.0.0.1:8000/healthz
```

预期返回 `status = ok`（只证明本机 HTTP 存活，不代表腾讯 ADP 可用）。详细的调用与错误示例见运维文档。

## 服务器部署速览

镜像名固定为 `adp_channel_api`，标签为 `vX.Y.Z`。默认目标为服务器对应的 `linux/arm64`（aarch64）。本地安装并启动 Docker Linux 引擎后，只指定新版本即可：

```powershell
.\scripts\build_image.ps1 -Version v0.0.5
```

生成 `dist/v0.0.5/`（镜像 tar、`manifest.json`、`SHA256SUMS`）；已有版本不会覆盖，实际打包时使用尚未占用的新版本号。手动将整个版本目录、`scripts/run.sh`、`scripts/stop.sh`、`scripts/upgrade.sh` 和服务器专用 `config.toml` 上传到服务器 `release/`。服务器配置使用 `session_file="../runtime/session.json"`、`log_file="../log/adp_chat.log"`，路径均相对于 `release/config.toml`。

服务器发布地址在 `release/config.toml` 的独立分组设置：

```toml
[deployment]
bind_ip = "10.102.70.82"
host_port = 4560
```

仅服务器 `run.sh`/`upgrade.sh` 使用这两项；本地单次脚本、交互 CLI、直接 Uvicorn 和源码 Compose 均忽略此分组，本地 Web 监听仍由 `--host`/`--port` 指定。修改服务器 IP/端口后执行 `bash ./upgrade.sh 当前版本号` 重建端口映射，容器内部端口仍为 8000。新分组需要目标镜像和服务器脚本支持；升级前备份旧配置，采用目标版本的新配置后直接升级。回退旧镜像时须恢复与其兼容的配置，详见运维文档。

服务器 `10.102.70.82` 的宿主端口是 **4560**，容器内部端口是 **8000**。从服务器 release 目录运行：

```bash
docker info                 # 当前账户应可直接访问Docker
chmod 640 config.toml        # 保留当前账户拥有配置文件
bash ./run.sh v0.0.5
curl -fsS http://10.102.70.82:4560/healthz
# 下次上传新版本目录后，自动选取数字版本最高的正式目录：
bash ./upgrade.sh
bash ./stop.sh
```

三个脚本可由有 Docker 权限且对 release 有写权限的普通账户直接运行，无需 sudo。缺失的 `log/` 和 `runtime/` 会自动创建；已有会话不会覆盖。配置只读挂载到容器固定路径 `/app/release/config.toml`，无需 `.env`。升级先校验归档、身份和会话，再停旧容器；新容器失败时尝试恢复上一版本。`session.json` 保存会话身份，不保存完整问答正文。完整目录、权限、端口和回滚说明见 [docs/adp-chat/04_Docker部署与运维.md](docs/adp-chat/04_Docker部署与运维.md#二linux-服务器部署)。

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

- `POST /api/v1/chat` 请求为 `{"message":"你好"}`，响应包括 `text`、`conversation_id`、`request_id`、`record_id`、`reply_method` 和 `reply_contents`；接口只在 `response.completed` 成功后一次性返回完整 JSON。
- `[chat].stream` 控制上游 ADP 文本输出：默认 `false` 为非流式，`true` 为流式。修改后重启对应进程；main、CLI 和 FastAPI 都继续等待完整正式结果。当前配置与升级说明见 [docs/adp-chat/04_Docker部署与运维.md](docs/adp-chat/04_Docker部署与运维.md)。
- `GET /healthz` 是轻量健康检查，不调用腾讯 ADP；FastAPI 的交互文档默认在 `/docs`。
- `runtime/session.json` 维持固定 UserId/AppKey 的单个 ConversationId；容器重建不应丢失该文件。三种模式不能**同时**操作同一份会话数据。
- 服务使用一把进程内互斥锁；单实例单 worker，同一时刻只能处理一个 ADP 请求，忙碌的第二条消息立即返回 HTTP **409**。不支持多用户、多会话、上传、队列或自动重试。
- **无 X-API-Key 或 HTTPS**。任何能够到达服务端口的内网程序都可以使用该固定 ADP 身份。必须由服务器网络边界限制访问来源，严禁直接暴露公网。
- 超时、断流、容器退出后不能推断 ADP 或下游 MCP 未执行；涉及订会、改会等写入操作时，不应盲目重试，业务幂等仍需后续验证。

正式对话入口在 [src/adp/client.py](src/adp/client.py)，服务编排在 [src/application/chat_service.py](src/application/chat_service.py)，HTTP 入口在 [src/api/routes.py](src/api/routes.py)，本地 CLI 在 [src/cli/runner.py](src/cli/runner.py)。

## 企业微信网关（阶段 A 身份基础已实现，后续按阶段实施）

当前已提供多员工身份派生和受控跨进程 ADP 验收入口，操作见 [docs/adp-chat/04_Docker部署与运维.md](docs/adp-chat/04_Docker部署与运维.md#16-多员工身份与真实-adp-跨进程验收阶段-a)。单用户 `POST /api/v1/chat` 保持原有行为，企业微信 `GET/POST /wecom/callback` 尚未实现，不能直接将现有内网 URL 填入企业微信后台。后续按 [docs/wecom-integration/03_实施路线图.md](docs/wecom-integration/03_实施路线图.md) 依次开发 B HTTP 回调和可靠 ACK、C 消息调度恢复、D 模拟发送、E JSONL 与本地/Docker HTTP 联调。**A～E 不要求公网 HTTPS/HTTP 地址**；F 才处理企业微信实际可达的回调地址（HTTP/HTTPS）和员工真实收发。HTTP/HTTPS 共用一条 FastAPI 业务路由，未来无需再造另一套网关。

**已确认但尚未实现的网关语义：**不同员工尽可能立即异步转发，不设置人为的 ADP 业务并发上限；同一员工上一轮仍在处理时，新 MsgId 立即拒绝并通知，不排队。ADP 明确执行前拒绝后解释真实错误码、提示手动重试；结果未知时保存证据、通知并释放**本地**会话，不自动重投 ADP。企业微信通知发送状态独立；接口明确接受即认为网关发送完成，不判断已读。上述接收、调度和发送行为属于 B～F，不能由阶段 A 的身份派生或真实 ADP 探测推断已经支持。

架构与边界见 [docs/wecom-integration/01_网关架构设计.md](docs/wecom-integration/01_网关架构设计.md)，日志事件见 [docs/wecom-integration/02_结构化日志事件.md](docs/wecom-integration/02_结构化日志事件.md)。现有 `/api/v1/chat` 无应用鉴权，继续只允许可信内网调用；不得因为后续接收企业微信回调而将其暴露公网。

## 测试

先安装 `requirements-dev.txt`，它同时以 editable 方式安装本项目，供测试和 PyCharm 使用同一组业务模块。源码可直接修改，无需重复安装；新增构建元数据或控制台入口时再安装一次。

```bash
python -B -X utf8 -m unittest discover -s tests -v
```

本地和现有 Windows CI 均安装 `requirements-dev.txt`，覆盖 FastAPI TestClient / HTTPX。**检查 skipped 是否为零，并核实每个跳过原因**。Python 回归通过不等于目标服务器的 Docker、ADP 网络及访问控制已验收。

本机 Docker 镜像可单独验收，测试使用临时配置和会话，不发送 ADP 请求：

```powershell
docker build -t adp-chat:local .
python tests/docker_smoke.py --image adp-chat:local
```

版本化部署的完整 Linux 验收入口为 [tests/deploy_smoke.py](tests/deploy_smoke.py)，使用两个已打包的版本和 Linux Docker CLI，覆盖真实启动、会话复用、升级前保护、失败恢复及自定义路径。运行方法见运维文档。

机器事实见 [config.example.toml](config.example.toml)、[requirements.txt](requirements.txt)、[requirements-dev.txt](requirements-dev.txt)、[compose.yaml](compose.yaml)、[Dockerfile](Dockerfile)。其他专题见 [docs/README.md](docs/README.md)。
