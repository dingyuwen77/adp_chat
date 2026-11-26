"""网关专用真正异步 HTTPS/SSE：一次出站，不自动重试、不推断业务意图。"""

import asyncio
import json
import ssl
from collections import deque
from dataclasses import dataclass, replace

import httpx

from core.config import ChatConfig
from core.identity import employee_identity
from core.models import ChatError, ChatResult
from core.outbound import OutboundProgress, network_reason
from .client import _check_conversation, _object, _payload, _result, _safe_diagnostic
from .sse import SSEDecoder, decode_event


_EXPLANATIONS = {
    "400": "请求参数无效", "4505004": "应用 AppKey 无效", "460001": "访问凭据校验失败",
    "460004": "应用不存在", "460033": "应用不存在或无访问权限", "460048": "应用尚未发布",
    "460026": "应用停用或欠费", "460031": "应用入口 QPS 超限",
    "460011": "应用配置模型 QPM 超限", "460701": "专属模型并发超限",
    "460711": "空间 TPM 超限", "460712": "空间 QPM 超限", "460713": "空间每日 Token 超限",
    "460721": "应用 TPM 超限", "460722": "应用 QPM 超限", "460723": "应用每日 Token 超限",
    "460032": "应用模型余额不足", "460702": "专属模型余额不足",
    "460034": "输入内容过长", "460035": "计算内容过长且已停止",
    "460020": "模型请求超时", "460401": "请求处理超时",
}
# 只有明确的应用入口/鉴权失败可独立证明未进入业务；模型层限额可能发生在工具之后。
_ENTRANCE_REJECTIONS = {"400", "4505004", "460001", "460004", "460033", "460048", "460026", "460031"}
_UNKNOWN = "本轮 ADP 执行结果未知，旧请求可能仍在云端执行。若涉及会议等操作，请先核查结果；核查后可手动发送新消息，网关不会自动重试。"
_CONNECTIONS_PER_POOL = 64
_MAX_IDLE_CONNECTIONS = 100


@dataclass(frozen=True)
class ADPOutcome:
    """同一事务提交的结果、一次通知和本地释放决定。"""

    state: str
    notification: str
    result: ChatResult | None = None
    error: dict | None = None


def unknown_outcome(code="interrupted", *, provider_code=None, **diagnostics) -> ADPOutcome:
    """不把不确定执行表达成业务失败，也不丢失实际Provider Code。"""
    explanation = _EXPLANATIONS.get(provider_code, "ADP 返回错误") if provider_code else ""
    prefix = f"{explanation}（{provider_code}）。" if provider_code else ""
    return ADPOutcome("ADP_UNKNOWN", prefix + _UNKNOWN,
                      error={"code": code, "provider_code": provider_code, **diagnostics})


class AsyncADPClient:
    """lifespan 持有若干有界连接池，按可用连接位分配，无模型名额排队。"""

    def __init__(self, config: ChatConfig, *, http_client=None, connect_timeout: float | None = None):
        """正式路径验证证书/禁跳转/零重试；注入HTTP只用于隔离协议测试。"""
        self.config = config
        self._owned = http_client is None
        capacity = config.max_active_requests
        if self._owned:
            timeout = (httpx.Timeout(config.chat_timeout_seconds, connect=connect_timeout)
                       if connect_timeout is not None else config.chat_timeout_seconds)
            # HTTPX 每次分配都扫描当前连接。限制单池规模并保留原总连接数。
            count = (capacity + _CONNECTIONS_PER_POOL - 1) // _CONNECTIONS_PER_POOL
            slots, extra_slots = divmod(capacity, count)
            idle, extra_idle = divmod(min(capacity, _MAX_IDLE_CONNECTIONS), count)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_default_certs()
            clients = []
            for index in range(count):
                limits = httpx.Limits(max_connections=slots + (index < extra_slots),
                                      max_keepalive_connections=idle + (index < extra_idle))
                transport = httpx.AsyncHTTPTransport(verify=context, retries=0, limits=limits)
                clients.append(httpx.AsyncClient(transport=transport, timeout=timeout,
                                                 follow_redirects=False, trust_env=False))
            self._clients = tuple(clients)
        else:
            self._clients = (http_client,)
            slots, extra_slots = capacity, 0
        # 队列只保存空闲连接位，立即轮转到可用组。没有请求在这里排队。
        self._available = deque(self._clients * slots + self._clients[:extra_slots])

    async def aclose(self):
        """仅关闭本Owner创建的连接，注入测试连接由测试上下文释放。"""
        if self._owned:
            await asyncio.gather(*(client.aclose() for client in self._clients))

    async def request(self, record: dict) -> ADPOutcome:
        """只接受已持久化RequestId的一轮；总超时包含连接、读流及处理。"""
        identity = employee_identity(self.config, record["user_id"])
        if (not record.get("request_id") or record["conversation_id"] != identity.conversation_id):
            raise ChatError("chat", "invalid_request", "网关请求缺少可靠关联标识。")
        config = identity.chat_config(self.config)
        payload = _payload(config, record["request_id"], identity.conversation_id,
                           [{"Type": "text", "Text": record["content"]}])
        return await self._request_payload(payload)

    async def probe(self, request_id: str, conversation_id: str, text: str) -> ADPOutcome:
        """使用调用方隔离的配置和会话，复用正式 HTTPS 和 SSE 路径。"""
        payload = _payload(self.config, request_id, conversation_id, [{"Type": "text", "Text": text}])
        return await self._request_payload(payload)

    async def _request_payload(self, payload: dict) -> ADPOutcome:
        """保持既有结果分类，并附上已观察到的传输和协议阶段。"""
        if not self._available:
            return unknown_outcome("client_capacity")
        client = self._available.popleft()
        progress = OutboundProgress("adp_connect_or_response", "adp_request_prepared")
        try:
            async with asyncio.timeout(self.config.chat_timeout_seconds):
                outcome = await self._consume(client, payload, progress)
        except (TimeoutError, httpx.TimeoutException) as exc:
            if isinstance(exc, httpx.ConnectTimeout):
                progress.reason = "connect_timeout"
            elif isinstance(exc, httpx.ReadTimeout):
                progress.reason = "read_timeout"
            outcome = unknown_outcome("timeout")
        except (httpx.HTTPError, OSError) as exc:
            progress.reason = network_reason(exc, "network_error")
            outcome = unknown_outcome("network_error")
        except (UnicodeError, json.JSONDecodeError, ChatError):
            outcome = unknown_outcome("invalid_response")
        finally:
            self._available.append(client)
        if outcome.error is not None:
            error = {**outcome.error, "last_ok": progress.last_ok, "failed_stage": progress.stage,
                     "reason": progress.reason or outcome.error["code"]}
            return replace(outcome, error=error)
        return outcome

    async def _consume(self, client: httpx.AsyncClient, payload: dict, progress: OutboundProgress) -> ADPOutcome:
        """按官方事件消费阶段证据；缺失ACK本身不是未执行证明。"""
        accepted = False
        execution = False
        rejected_ack = False
        async with client.stream("POST", self.config.adp_url, json=payload,
                                 headers={"Accept": "text/event-stream"}) as response:
            progress.last_ok, progress.stage = "adp_http_response", "adp_http"
            if response.status_code != 200:
                return unknown_outcome("http_error", http_status=response.status_code)
            progress.stage = "adp_sse"
            if response.headers.get("Content-Type", "").partition(";")[0].strip().lower() != "text/event-stream":
                return unknown_outcome("invalid_response")
            decoder, received = SSEDecoder(), 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > self.config.adp_response_max_bytes:
                    return unknown_outcome("response_resource_limit")
                for name, data in decoder.feed(chunk):
                    decoded = decode_event(name, data)
                    if decoded is None:
                        return unknown_outcome("incomplete")
                    kind, event = decoded
                    if kind == "request_ack":
                        ack = _object(event.get("RequestAck"))
                        _check_conversation(ack, payload["ConversationId"])
                        if ack.get("Status") == "success":
                            accepted = True
                            progress.last_ok = "adp_ack"
                        elif ack.get("Status") == "failed":
                            rejected_ack = True
                        else:
                            return unknown_outcome("invalid_ack")
                    elif kind == "error":
                        error = _object(event.get("Error"))
                        code = _safe_diagnostic(error.get("Code"))
                        diagnostics = {"provider_request_id": _safe_diagnostic(error.get("RequestId")),
                                       "provider_trace_id": _safe_diagnostic(error.get("TraceId"))}
                        if not execution and not accepted and (rejected_ack or code in _ENTRANCE_REJECTIONS):
                            detail = _EXPLANATIONS.get(code, "ADP 明确拒绝了请求")
                            suffix = f"（{code}）" if code else ""
                            return ADPOutcome("ADP_REJECTED", f"{detail}{suffix}。本轮未获准执行，请检查原因后手动重试。",
                                              error={"code": "provider_error", "provider_code": code, **diagnostics})
                        return unknown_outcome("provider_error", provider_code=code, **diagnostics)
                    elif kind == "response.completed":
                        value = _object(event.get("Response"))
                        _check_conversation(value, payload["ConversationId"])
                        if rejected_ack or value.get("Status") != "success":
                            return unknown_outcome("response_failed")
                        progress.last_ok, progress.stage = "adp_completed", "adp_reply"
                        result = _result(value, payload["ConversationId"], payload["RequestId"])
                        if result.reply_method == 7:
                            return ADPOutcome("ADP_UNKNOWN", "ADP 返回并发数超限回复。" + _UNKNOWN,
                                              result=result, error={"code": "reply_method_7", "provider_code": None})
                        unsupported = any(p.get("Type") not in ("text", "json_text")
                                          for message in result.reply_contents for p in message)
                        notice = result.text
                        if unsupported:
                            notice += "\n本回复含当前文字渠道无法完整呈现的媒体内容。"
                        if not notice:
                            notice = "ADP 已结束本轮，但没有可由文字渠道呈现的回答。"
                        return ADPOutcome("ADP_SUCCEEDED", notice, result=result)
                    elif kind.startswith(("response.", "message.", "content.", "text.")):
                        execution = True
        if rejected_ack and not accepted and not execution:
            return ADPOutcome("ADP_REJECTED", "ADP 明确未接受本轮请求，请检查原因后手动重试。",
                              error={"code": "request_rejected", "provider_code": None})
        return unknown_outcome("incomplete")
