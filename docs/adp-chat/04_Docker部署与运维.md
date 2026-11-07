# Docker 部署、环境准备、调试与内网调用

本指南面向拿到源码、需要自行部署并使用的人。**先在本地验证，再部署到 Linux 服务器，最后从另一台电脑调用**。尚未实际运行的环境步骤不能视为已经验收成功。

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

当前 CLI 由命令行参数和上述交互命令控制；`[cli]` 下的 `first_question`、`show_preview`、`interactive` 仅作为旧配置兼容字段读取，不影响当前入口。单次脚本、交互 CLI 和 HTTP 都关闭高级文字推断及结构化业务解析；直接 Python Library 的高级用法见 [docs/adp-chat/01_文字追问接口.md](01_文字追问接口.md)。

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

## 二、Linux 服务器部署

### 2.1 本机打包与手动上传

镜像名称固定为 `adp_channel_api`，正式标签为 `vX.Y.Z`（三段非负整数，不接受预发布或前导零）。四个源码脚本均保存在本地 [scripts/build_image.ps1](../../scripts/build_image.ps1)、[scripts/run.sh](../../scripts/run.sh)、[scripts/stop.sh](../../scripts/stop.sh)、[scripts/upgrade.sh](../../scripts/upgrade.sh)。

在 Windows 项目根目录，安装并启动 Docker Linux 引擎，然后指定新版本即可：

```powershell
.\scripts\build_image.ps1 -Version v0.0.5
```

默认平台是 `linux/arm64`，对应目标服务器Ubuntu24.04/aarch64。日常只需指定版本号，无需再传Platform。跨架构构建取决于本机builder的目标平台支持；确有其他架构用途时仍可显式覆盖 `-Platform linux/amd64`。脚本兼容 Windows PowerShell 5.1/PowerShell 7；生成 `dist/v0.0.5/adp_channel_api-v0.0.5-linux-arm64.tar`、`manifest.json`、`SHA256SUMS`。清单记录镜像、ID/配置摘要、平台、归档 SHA256、北京时间、源码 revision 和工作区是否有未提交修改。脚本拒绝覆盖同版本目录或已有镜像标签；失败只清理本次临时产物。打包不需要真实配置，构建上下文排除了正式配置、日志、会话和 dist。

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

### 旧镜像首次启用部署配置

新分组需要包含新版配置加载器的镜像以及新版三个服务器脚本。此前已打包的旧镜像（包括原 `v0.0.6`）会拒绝 `[deployment]`；仅更新脚本不能改变旧镜像的加载行为。

1. 用当前源码按未使用的新版本号打包，上传完整版本目录和新版三个 sh 脚本，备份现有服务器配置。
2. 服务器先保持原配置，暂不添加 `[deployment]`，保留 AppKey/UserId、会话和日志路径；执行 `bash ./upgrade.sh 新版本号`。旧配置的环境变量、部署记录和默认值回退仍有效。
3. 新镜像健康后，再在服务器配置添加 `[deployment]`，执行 `bash ./upgrade.sh 同一新版本号` 应用 IP/端口。
4. 需要回退到不支持新分组的旧镜像时，先恢复不含该分组且身份/持久路径相同的配置备份，再显式指定旧版本升级。

脚本会在停机前预检旧镜像与当前配置是否兼容；提前加入新分组可能触发兼容拒绝，此时旧容器保持运行。整个过程不需要新建或删除会话文件。

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

[config.example.toml](../../config.example.toml) 是可提交的配置模板，AppKey留空；本机config.toml保存真实配置并被Git/Docker忽略。本机这两份配置除AppKey外保持一致；服务器release配置还需保留该环境的../runtime、../log等路径，不能用本机根配置整份覆盖。

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

切换顺序是：取得部署锁 → 校验归档/清单和平台 → 必要时加载目标镜像 → 校验固定配置、路径、目录权限和原会话 → 验证旧镜像仍兼容当前配置 → 优雅停止并替换旧容器 → 等待新容器健康 → 在真实容器中复核会话、挂载和镜像 → 原子记录成功。**不会在加载新包前删除上一成功镜像。**

新容器启动/验收失败时，清理本次容器并尝试用原镜像、原端口和原会话恢复上一版本，仍读取**当前 `release/config.toml`**；即使恢复成功，升级命令仍返回非零，需检查失败原因。脚本不保存旧配置副本，也不自动撤销配置修改，所以切换前会先检查旧镜像能否读取当前配置和原会话。配置回退需要人工恢复升级前的受控备份，再重新运行升级入口验收；保持原 AppKey/UserId 和会话路径。若当前配置/会话已不兼容，自动恢复可能失败，会明确报告，不能靠清空会话继续。强制断电、SIGKILL、磁盘故障或外部同时修改数据不属于自动恢复保证范围。

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
