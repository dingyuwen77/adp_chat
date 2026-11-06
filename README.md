# adp_chat · 腾讯 ADP 单用户对话转发

同一套 Python 源码支持**单次脚本、交互式 CLI、单实例 FastAPI**。每条输入只发送一轮 ADP HTTP SSE V2，对话是否需要追问由 ADP 决定；下一条输入继续使用相同 ConversationId。

**范围**：固定一个 ADP 用户与会话，仅支持文字输入。无应用层鉴权、无队列、无自动重试；只允许放在已限制调用方的可信企业内网，勿公开到互联网。当前不提供企业微信回调网关。

## 配置

Python 3.14。复制 [config.example.toml](config.example.toml) 为包内未纳入 Git 的 `adp_chat/config.toml`，填写已发布 ADP 应用的 AppKey 和稳定 UserId。相对日志和会话路径以配置文件所在目录为基准，模板把持久目录指向仓库根 `runtime/` 和 `logs/`。参见 [adp_chat/core/config.py](adp_chat/core/config.py)。

切换应用或用户时，现有 `runtime/session.json` 会进行身份校验；不会悄悄覆盖不匹配或损坏的文件。

## 三种运行方式

```bash
python main.py --ask "你好"             # 一轮后退出
python main.py                         # 从终端读取一条后退出
python -m adp_chat                     # 多轮交互，/exit 退出
python -m uvicorn adp_chat.api.app:app --host 127.0.0.1 --port 8000 --workers 1
```

配置路径可用 `--config` 覆盖；终端 `/new` 或 `--new-session` 可在**停止 HTTP 服务后**人工更换当前会话。不支持 HTTP 会话重置。所有调用入口复用 [adp_chat/application/chat_service.py](adp_chat/application/chat_service.py)，正式 ADP 流处理仍来自 [adp_chat/adp/client.py](adp_chat/adp/client.py)。

### 内网 FastAPI

```http
POST http://10.102.70.82:4560/api/v1/chat
Content-Type: application/json

{"message":"你好"}
```

API 返回 `text`、`conversation_id`、`request_id`、`record_id`、`reply_method` 和完整 `reply_contents`。无认证 Header。忙碌时返回 HTTP 409，输入格式不合法时 422，ADP 错误/超时分别返回 502/504；只有正式成功 `response.completed` 才返回 200。健康检查为 `GET /healthz`，不会调用 ADP。

**重要**：当前进程内锁仅保护一个 Uvicorn worker/实例，且防并发不等于幂等。无法确认结果的 ADP 请求不能自动重试；后续启用会议预订等写操作之前需要下游业务幂等和未知结果处置。

## Docker 部署与验证

执行 `docker compose up -d --build` 前必须准备 `deploy/config.toml`、`runtime/`、`logs/`，并检查目录权限。默认仅绑定服务器内网地址 `10.102.70.82:4560`，可通过 Compose 的 `ADP_BIND_IP`、`ADP_HOST_PORT` 覆盖。**当前代码尚未在目标服务器验收，不将配置示例视为已部署成功。**

完整安装、PowerShell/Python 跨机器调用、网络限制、备份和回滚见 [docs/adp-chat/04_Docker部署与运维.md](docs/adp-chat/04_Docker部署与运维.md)，架构/API 见 [docs/adp-chat/03_FastAPI服务架构.md](docs/adp-chat/03_FastAPI服务架构.md)。

## 测试

```bash
python -B -X utf8 -m unittest discover -s tests
```

测试使用 unittest；FastAPI 的 ASGI 契约测试需要先安装 `requirements-dev.txt`，纯标准库 CLI 环境会明确跳过这些用例。已有 GitHub Actions CI 工作流保持不变，不新增 CI。外部 ADP 真实联网、Docker 目标主机、网络 ACL 和 Windows 跨机验收应按部署文档在实际可达环境执行。

更多背景和企业微信未实施内容见 [docs/README.md](docs/README.md)。
