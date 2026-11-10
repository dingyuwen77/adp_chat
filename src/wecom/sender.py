"""统一通知适配器：正式企微协议与模拟证据分开，不调用ADP、不追踪已读。"""

import asyncio
import json
import logging
import ssl
from dataclasses import dataclass
from time import monotonic

import httpx

from core.config import ChatConfig
from core.logging import HTTPQueryFilter
from core.models import ChatError
from .models import is_employee_id
from .images import ImageDownloader, ImageError
from .reply_plan import ImageBlock, TextBlock, reply_plan, text_parts


_BASE = "https://qyapi.weixin.qq.com"


@dataclass(frozen=True)
class SendOutcome:
    """protocol_accepted仅说明协议解码；是否真实接受还必须核对source。"""

    state: str
    source: str
    protocol_accepted: bool | None
    accepted_parts: int | None
    total_parts: int | None
    code: str
    provider_code: int | None = None


def unknown_send(source="recovery", code="interrupted") -> SendOutcome:
    """武装后结果不明保留未知，不把可能已接受的消息变成可自动重发。"""
    return SendOutcome("SEND_UNKNOWN", source, None, None, None, code)


class SimulatedSender:
    """没有外部I/O的测试发送器，唯一成功状态为SEND_SIMULATED。"""

    source = "simulated"

    def __init__(self, config: ChatConfig):
        """只允许显式测试环境，不能冒充正式发送。"""
        if config.gateway_environment != "test":
            raise ChatError("config", "invalid_notification", "模拟发送只允许测试环境。")

    async def send(self, record: dict) -> SendOutcome:
        """统一消费保存提示；不打印员工正文或构造平台接受证据。"""
        if not is_employee_id(record.get("user_id")):
            return SendOutcome("SEND_FAILED", self.source, False, 0, None, "invalid_target")
        count = sum(len(text_parts(block.text)) if isinstance(block, TextBlock) else 1
                    for block in reply_plan(record))
        if not count:
            return SendOutcome("SEND_FAILED", self.source, False, 0, None, "empty_notification")
        return SendOutcome("SEND_SIMULATED", self.source, None, count, count, "simulated")

    async def aclose(self):
        """没有网络资源，但保持统一生命周期。"""


class _ProtocolError(Exception):
    """仅传递安全原因，不包含URL、凭据或供应方原始正文。"""

    def __init__(self, code, *, http_status=None):
        """保留HTTP状态供逐图诊断使用，不附带响应正文。"""
        self.http_status = http_status
        super().__init__(code)


@dataclass
class _ImageAttempt:
    """每个发送任务独立持有当前图片进度，不能用实例字段串扰并发用户。"""

    index: int
    started: float
    stage: str = "capacity_wait"


class WeComSender:
    """正式应用消息接口；注入Fake HTTP的成功只能形成模拟结果。"""

    def __init__(self, config: ChatConfig, *, http_client=None, logger=None):
        """原生出站需production与Secret；D只使用注入的隔离HTTP。"""
        if not config.wecom_app_secret.strip():
            raise ChatError("config", "missing_wecom_secret", "正式发送适配器需要本机应用凭据。")
        self._owned = http_client is None
        if self._owned and config.gateway_environment != "production":
            raise ChatError("config", "real_send_disabled", "测试环境不得使用真实企业微信出站。")
        self.config = config
        self.logger = logger if logger is not None else logging.getLogger(__name__)
        self.source = "wecom_api" if self._owned else "fake_http"
        if http_client is None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_default_certs()
            transport = httpx.AsyncHTTPTransport(verify=context, retries=0,
                limits=httpx.Limits(max_connections=None, max_keepalive_connections=20))
            http_client = httpx.AsyncClient(transport=transport, follow_redirects=False,
                                           trust_env=False, timeout=config.send_timeout_seconds)
        self.http = http_client
        self.images = ImageDownloader(config, http_client=None if self._owned else http_client)
        self._token = None
        self._expires = 0.0
        self._token_lock = asyncio.Lock()
        self._filter = HTTPQueryFilter()
        logging.getLogger("httpx").addFilter(self._filter)

    async def aclose(self):
        """保持请求期间查询脱敏，关闭自有连接后撤销本实例filter。"""
        try:
            await self.images.aclose()
            if self._owned:
                await self.http.aclose()
        finally:
            logging.getLogger("httpx").removeFilter(self._filter)

    async def _json(self, method, path, *, params, payload=None, files=None):
        """有界消费官方JSON响应；HTTP非200或畸形正文不当成功。"""
        async with self.http.stream(method, _BASE + path, params=params, json=payload, files=files,
                                    follow_redirects=False) as response:
            if response.status_code != 200:
                raise _ProtocolError("http_error", http_status=response.status_code)
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 65536:
                    raise _ProtocolError("response_resource_limit")
                body.extend(chunk)
            try:
                data = json.loads(body)
            except (ValueError, UnicodeError):
                raise _ProtocolError("invalid_response") from None
            # 官方素材成功响应可仅含 type/media_id/created_at，错误才保证 errcode。
            if isinstance(data, dict) and path == "/cgi-bin/media/upload" and "errcode" not in data:
                if data.get("type") == "image" and isinstance(data.get("media_id"), str) and data["media_id"]:
                    data["errcode"] = 0
            if not isinstance(data, dict) or type(data.get("errcode")) is not int:
                raise _ProtocolError("invalid_response")
            return data

    async def _access_token(self, *, rejected_token=None):
        """按应用缓存Token；并发发现旧Token失效时只由一个任务刷新。"""
        async with self._token_lock:
            if self._token and monotonic() < self._expires and (rejected_token is None or self._token != rejected_token):
                return self._token
            if rejected_token == self._token:
                self._token, self._expires = None, 0.0
            data = await self._json("GET", "/cgi-bin/gettoken",
                                    params={"corpid": self.config.corp_id, "corpsecret": self.config.wecom_app_secret})
            if data["errcode"] != 0:
                return None, data["errcode"]
            token, seconds = data.get("access_token"), data.get("expires_in")
            if (not isinstance(token, str) or not token or len(token.encode("utf-8")) > 512
                    or any(ord(c) < 32 for c in token) or type(seconds) is not int or not 0 < seconds <= 86400):
                raise _ProtocolError("invalid_token_response")
            self._token = token
            self._expires = monotonic() + seconds - min(30, seconds / 10)
            return token

    def _log_image_failure(self, record, attempt, error, *, fallback=False, uncertain=False):
        """在统一日志中关联单张失败；仅输出受控原因和数字，不写URL/异常正文。"""
        if attempt is None:
            return
        if isinstance(error, ImageError):
            reason = error.code
        elif isinstance(error, (TimeoutError, httpx.TimeoutException)):
            reason = "image_timeout"
        elif isinstance(error, asyncio.CancelledError):
            reason = "image_cancelled"
        elif isinstance(error, _ProtocolError):
            reason = "image_protocol_error"
        elif isinstance(error, (httpx.HTTPError, OSError)):
            reason = "image_network_error"
        else:
            reason = "image_invalid_response"
        http_status = getattr(error, "http_status", None)
        provider_code = getattr(error, "provider_code", None)
        self.logger.warning("图片准备失败，已在原位置降级为链接" if fallback else "图片发送未完成", extra={"log_fields": {
            "message_key": record.get("message_key"), "image_index": attempt.index,
            "error_stage": attempt.stage, "reason": reason,
            "http_status": http_status if type(http_status) is int else None,
            "provider_code": provider_code if type(provider_code) is int else None,
            "elapsed_ms": round((monotonic() - attempt.started) * 1000, 3),
            "image_fallback": fallback, "outcome_uncertain": uncertain}})

    async def _prepare_image(self, block, attempt):
        """素材准备不提交用户消息；失败可以降级，Token明确过期仅纠正一次。"""
        async with self.images.slots:
            attempt.stage = "download"
            body, mime = await self.images.download(block.url)
            try:
                attempt.stage = "token"
                token = await self._access_token()
                for correction in range(2):
                    if isinstance(token, tuple):
                        raise ImageError("image_token_rejected", provider_code=token[1])
                    attempt.stage = "upload"
                    data = await self._json("POST", "/cgi-bin/media/upload",
                        params={"access_token": token, "type": "image"},
                        files={"media": ("image.png" if mime == "image/png" else "image.jpg", body, mime)})
                    if data["errcode"] in {40014, 42001} and correction == 0:
                        attempt.stage = "token"
                        token = await self._access_token(rejected_token=token)
                        continue
                    media_id = data.get("media_id")
                    if (data["errcode"] != 0 or not isinstance(media_id, str) or not media_id
                            or len(media_id) > 1024 or any(ord(c) < 32 for c in media_id)):
                        raise ImageError("image_upload_rejected", provider_code=data["errcode"])
                    return media_id
            finally:
                # 不缓存二进制或 media_id；上传请求退出后释放本图片内存引用。
                del body

    async def send(self, record: dict) -> SendOutcome:
        """仅Token明确失效允许一次纠正；其他发送失败/未知没有自动重发。"""
        accepted, total, uncertain, last_code = 0, None, False, None
        attempt = None
        if not is_employee_id(record.get("user_id")):
            return SendOutcome("SEND_FAILED", self.source, False, 0, None, "invalid_target")
        try:
            blocks = reply_plan(record)
            total = sum(len(text_parts(block.text)) if isinstance(block, TextBlock) else 1 for block in blocks)
            if not total:
                return SendOutcome("SEND_FAILED", self.source, False, 0, None, "empty_notification")
            send_remaining = self.config.send_timeout_seconds
            prepare_remaining = self.config.image_prepare_timeout_seconds
            image_count, degraded = 0, False
            for block in blocks:
                attempt = None
                if isinstance(block, ImageBlock):
                    started = monotonic()
                    image_count += 1
                    attempt = _ImageAttempt(image_count, started)
                    try:
                        if prepare_remaining <= 0:
                            attempt.stage = "prepare_budget"
                            raise ImageError("image_prepare_budget_exhausted")
                        async with asyncio.timeout(min(10, prepare_remaining)):
                            media_id = await self._prepare_image(block, attempt)
                        messages = [("image", {"media_id": media_id})]
                    except (TimeoutError, httpx.HTTPError, OSError, _ProtocolError, ValueError) as exc:
                        self._log_image_failure(record, attempt, exc, fallback=True)
                        attempt = None
                        degraded = True
                        messages = [("text", {"content": part}) for part in text_parts(block.fallback())]
                        total += len(messages) - 1
                    finally:
                        prepare_remaining -= monotonic() - started
                else:
                    messages = [("text", {"content": part}) for part in text_parts(block.text)]
                started = monotonic()
                async with asyncio.timeout(send_remaining):
                    for msgtype, content in messages:
                        if attempt is not None:
                            attempt.stage = "send_token"
                        token = await self._access_token()
                        if isinstance(token, tuple):
                            self._log_image_failure(record, attempt, ImageError("image_token_rejected", provider_code=token[1]))
                            return SendOutcome("SEND_FAILED", self.source, False, accepted, total, "token_rejected", token[1])
                        for correction in range(2):
                            uncertain, last_code = True, None
                            if attempt is not None:
                                attempt.stage = "send"
                            data = await self._json("POST", "/cgi-bin/message/send", params={"access_token": token},
                                payload={"touser": record["user_id"], "msgtype": msgtype, "agentid": self.config.agent_id,
                                         msgtype: content, "safe": 0, "enable_id_trans": 0, "enable_duplicate_check": 0})
                            code = data["errcode"]
                            if code != 0:
                                uncertain, last_code = False, code
                            if code in {40014, 42001} and correction == 0:
                                if attempt is not None:
                                    attempt.stage = "send_token"
                                token = await self._access_token(rejected_token=token)
                                if isinstance(token, tuple):
                                    self._log_image_failure(record, attempt, ImageError("image_token_rejected", provider_code=token[1]))
                                    return SendOutcome("SEND_FAILED", self.source, False, accepted, total, "token_rejected", token[1])
                                continue
                            if code != 0:
                                self._log_image_failure(record, attempt, ImageError("image_send_rejected", provider_code=code))
                                return SendOutcome("SEND_FAILED", self.source, False, accepted, total, "wecom_error", code)
                            for field in ("invaliduser", "unlicenseduser"):
                                if field in data and not isinstance(data[field], str):
                                    raise _ProtocolError("invalid_response")
                                values = data.get(field, "").split("|")
                                if any(value and not is_employee_id(value) for value in values):
                                    raise _ProtocolError("invalid_response")
                                if record["user_id"].casefold() in {value.casefold() for value in values}:
                                    self._log_image_failure(record, attempt, ImageError("image_target_rejected", provider_code=0))
                                    return SendOutcome("SEND_FAILED", self.source, False, accepted, total, "invalid_user", 0)
                            accepted += 1
                            uncertain = False
                            break
                send_remaining -= monotonic() - started
            state = "SEND_ACCEPTED" if self.source == "wecom_api" else "SEND_SIMULATED"
            return SendOutcome(state, self.source, True, accepted, total,
                               "accepted_with_image_fallback" if degraded else "accepted")
        except asyncio.CancelledError as exc:
            self._log_image_failure(record, attempt, exc, uncertain=uncertain)
            raise
        except (TimeoutError, httpx.HTTPError, OSError, _ProtocolError, ValueError) as exc:
            self._log_image_failure(record, attempt, exc, uncertain=uncertain)
            # Token阶段从未提交消息时可证明没有发送；提交后的传输/协议问题均保守未知。
            return SendOutcome("SEND_UNKNOWN" if uncertain else "SEND_FAILED", self.source, None,
                               accepted, total, "transport_or_protocol_error", last_code)


def create_sender(config: ChatConfig, *, logger=None):
    """默认不发送；正式适配器复用网关日志，模拟发送仍使用独立证据来源。"""
    if config.notification_mode == "disabled":
        return None
    return SimulatedSender(config) if config.notification_mode == "simulated" else WeComSender(config, logger=logger)
