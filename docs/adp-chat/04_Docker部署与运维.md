# Docker 部署与内网调用

## 目标

将源码运行在 Linux 服务器 **10.102.70.82**，默认宿主机 TCP 端口 **4560**，容器内 Uvicorn 监听 `0.0.0.0:8000`（一个 worker）。公网、HTTPS、认证和自动幂等恢复不属于本版。服务器 IP/端口为目标配置，**不能代替实际连通性验收**。

正式配置模板见 [config.example.toml](../../config.example.toml)，容器入口见 [Dockerfile](../../Dockerfile) 与 [compose.yaml](../../compose.yaml)。

## 首次准备

在项目根目录：

```bash
mkdir -p deploy runtime logs
cp config.example.toml deploy/config.toml
```

编辑 `deploy/config.toml`，填写 ADP AppKey / UserId，并保留模板的 `../runtime/session.json` 和 `../logs/adp_chat.log` 路径。此文件只读挂载到容器内 `/app/adp_chat/config.toml`，不纳入 Git。请将 `runtime/`、`logs/` 设置为镜像内非 root 用户 UID 10001 可写，并确保 `deploy/config.toml` 对该用户可读；不要简单赋予整个目录 777 权限。

如果从旧部署迁移，先停止旧服务，备份有效的 `runtime/session.json` 并放回同一路径，避免创建新会话。新旧实例不得同时操作相同文件。

Compose 默认使用 `10.102.70.82:4560:8000`，仅在服务器拥有该 IP 时有效。修改绑定网卡/端口时，在项目根目录的未提交 `.env` 写入：

```dotenv
ADP_BIND_IP=10.102.70.82
ADP_HOST_PORT=4560
```

本机调试可设 `ADP_BIND_IP=127.0.0.1`，但此时另一台电脑无法通过服务器内网 IP 访问。

## Docker 启动与检查

```bash
docker compose config
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=100 adp-chat
curl -fsS http://10.102.70.82:4560/healthz
```

`curl /healthz` 只证明本地 HTTP 健康，不证明云端 ADP 可用。容器内单进程运行，`restart: unless-stopped` 管理进程退出后的重启；运行中断的上游请求不会恢复。

停止：

```bash
docker compose down
```

## Windows 电脑调用

先检查 TCP 端口：

```powershell
Test-NetConnection -ComputerName 10.102.70.82 -Port 4560
```

再发一条消息：

```powershell
$body = @{ message = "你好" } | ConvertTo-Json -Compress
$response = Invoke-RestMethod -Uri "http://10.102.70.82:4560/api/v1/chat" -Method Post -ContentType "application/json; charset=utf-8" -Body ([Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 240
$response.text
```

Python 标准库（其他内网服务也可用）：

```python
import json
from urllib.request import Request, urlopen

data = json.dumps({"message": "继续刚才的话题"}, ensure_ascii=False).encode("utf-8")
request = Request("http://10.102.70.82:4560/api/v1/chat", data=data,
                  headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
with urlopen(request, timeout=240) as response:
    print(json.load(response)["text"])
```

此版本没有 `X-API-Key`。由于 HTTP 不加密，请务必使用已确认可信的企业内网，不得通过公网映射发布。

## 备份、升级及回滚

1. 停止容器并确认没有运行中的 ADP 请求；异常终止后不要假设已发出的写操作尚未执行。
2. 备份 `deploy/config.toml`（受保护保存）、`runtime/session.json` 和必要日志，并记录当前应用版本/镜像标识。
3. 获取新的已验证代码，`docker compose up -d --build`。挂载的持久数据不随容器重建删除。
4. 通过 `/healthz`、有界真实 ADP 连续两轮文字和容器重启，核验同一个 ConversationId。
5. 回滚时停止新实例，恢复原镜像/代码，保留状态文件；如果新旧配置中的 ADP AppKey/UserId 不一致，应先确认正确状态版本，不要强制重建会话。

## 排障

- TCP 不通：检查 IP 属于服务器、Compose 映射、路由、云安全组/ACL、Docker 防火墙规则以及端口占用。
- HTTP 409：当前会话已经有一轮请求运行；不会自动排队。不要在等待结果未知时盲重发。
- HTTP 422：输入 JSON 不合法或 message 为空。
- HTTP 502/504：ADP 异常/超时，查看关联请求 ID；504 不保证 ADP 没有接收请求。
- 服务启动失败：检查 TOML 解析、AppKey/UserId、`runtime/` 和 `logs/` 读写权限、损坏的 session.json。
- Health 正常但业务失败：本地健康不等于外部 ADP API 可用。

**上线前网络门禁**：确认服务端口不对公网可达；应由拥有服务器网络权限的人员通过真实防火墙/ACL 和授权的内网、非授权网络测试验证，不能仅凭 IP 为私有地址就断言访问隔离。
