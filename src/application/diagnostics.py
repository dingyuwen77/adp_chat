"""单次 ADP 和企微收发检查，使用既有客户端及经认证的回调。"""

import asyncio
import re
from dataclasses import dataclass
from uuid import uuid4

from core.outbound import OutboundProgress
from wecom.models import InboundMessage


_PREFIX = "ADP-DIAG-"
_TEST_CODE = re.compile(re.escape(_PREFIX) + r"[0-9a-f]{32}")
_ADP_TEXT = "这是只读连通性测试。请直接回复：连通正常。"
_WECOM_SEND_SECONDS = 15
_WECOM_REPLY_SECONDS = 35
_NETWORK_HINTS = {
    "dns_error": "在同一容器检查 DNS 解析及 DNS 出站策略。",
    "connect_timeout": "检查容器出网、出口防火墙、安全组、路由和对端可达性。",
    "connect_refused": "检查目标端口、出口代理及拒绝连接的网络规则。",
    "tls_error": "检查容器系统时间、CA 证书和出口 TLS 代理。",
}
_STAGE_HINTS = {
    "configuration": "核对正式 ADP 应用、独立测试 UserId 或企微网关及真实发送模式。",
    "adp_connect_or_response": "检查容器出网、代理、防火墙和上游 HTTP 响应。",
    "adp_http": "检查上游 HTTP 状态、网关和 ADP 应用配置。",
    "adp_sse": "用请求 ID 查 ADP 处理及 SSE 回程。若有上游错误码，先核对应用权限、发布状态或配额。",
    "adp_reply": "检查 ADP 本轮正式回复的内容与协议格式。",
    "wecom_token": "检查到企微 API 的出网、应用凭据及可信出口 IP。",
    "wecom_send": "核对企微 API 响应、应用可见范围及测试账号权限。结果未知时先确认实收。",
    "wecom_callback": "请测试账号原样发回测试码，再查企微回调配置、公网转发及验签日志。",
}


@dataclass(slots=True)
class DiagnosticReport:
    """供运维分享的结果，仅包含阶段、分类及安全标识。"""

    diagnostic_id: str
    target: str
    status: str
    last_ok: str
    failed_stage: str | None = None
    reason: str | None = None
    hint: str | None = None
    request_id: str | None = None
    http_status: int | None = None
    provider_code: str | int | None = None
    provider_request_id: str | None = None
    send_status: str | None = None
    receive_status: str | None = None

    def fail(self, stage: str, reason: str, *, status: str = "failed") -> None:
        """用已观察到的阶段给排查方向，并保留不确定结果。"""
        self.status, self.failed_stage, self.reason = status, stage, reason
        self.hint = _NETWORK_HINTS.get(reason) or _STAGE_HINTS.get(stage)


class Diagnostics:
    """单进程只执行一轮主动探测，企微回调在等待期间仍可进入。"""

    def __init__(self, config, logger, *, adp=None, sender=None):
        """借用 lifespan 管理的客户端，不占用正式 ADP 会话。"""
        self.enabled = config.diagnostics_enabled
        self.logger = logger
        self.adp = adp
        self.sender = sender
        self.lock = asyncio.Lock()
        self._expected: tuple[str, str, asyncio.Event] | None = None

    def receive(self, message: InboundMessage) -> bool:
        """消费保留格式的探测码，仅当前指定成员的正确回发可以完成检查。"""
        content = message.content.strip()
        if not _TEST_CODE.fullmatch(content):
            return False
        if self._expected is not None:
            user_id, expected_code, received = self._expected
            if content == expected_code and message.user_id.casefold() == user_id:
                received.set()
        # 迟到、重复和其他成员误发的测试码也不进入业务处理。
        return True

    async def check(self, target: str, user_id: str | None) -> DiagnosticReport:
        """运行一轮已启用、已限并发的检查并记录可关联结果。"""
        report = DiagnosticReport(uuid4().hex, target, "passed", "request_received")
        if target == "adp":
            await self._check_adp(report)
        else:
            await self._check_wecom(report, user_id)
        if self.logger is not None:
            self.logger.info(
                "diagnostic_completed diagnostic_id=%s target=%s status=%s last_ok=%s failed_stage=%s "
                "reason=%s request_id=%s http_status=%s provider_code=%s send_status=%s receive_status=%s",
                report.diagnostic_id, report.target, report.status, report.last_ok, report.failed_stage,
                report.reason, report.request_id, report.http_status, report.provider_code,
                report.send_status, report.receive_status,
            )
        return report

    async def _check_adp(self, report: DiagnosticReport) -> None:
        """用独立身份测试真实 SSE 完成和文字解析。"""
        if self.adp is None:
            report.fail("configuration", "adp_unconfigured")
            return
        report.request_id = str(uuid4())
        outcome = await self.adp.probe(report.request_id, str(uuid4()), _ADP_TEXT)
        if outcome.state == "ADP_SUCCEEDED" and outcome.result and outcome.result.text.strip():
            report.last_ok = "adp_reply_parsed"
            return
        if outcome.state == "ADP_SUCCEEDED":
            report.last_ok = "adp_completed"
            report.fail("adp_reply", "no_text_reply")
            return
        error = outcome.error or {}
        report.last_ok = error.get("last_ok", report.last_ok)
        report.http_status = error.get("http_status")
        report.provider_code = error.get("provider_code")
        report.provider_request_id = error.get("provider_request_id")
        report.fail(error.get("failed_stage", "adp_prepare"), error.get("reason", error.get("code", "unknown")),
                    status="unknown" if outcome.state == "ADP_UNKNOWN" else "failed")

    async def _check_wecom(self, report: DiagnosticReport, user_id: str | None) -> None:
        """先登记唯一回发，再发企微短文字，保留两个方向各自的证据。"""
        report.send_status = report.receive_status = "unverified"
        if user_id is None or getattr(self.sender, "source", None) != "wecom_api":
            report.fail("configuration", "wecom_unconfigured")
            return
        code = _PREFIX + report.diagnostic_id
        received = asyncio.Event()
        self._expected = user_id.casefold(), code, received
        progress = OutboundProgress("wecom_prepare")
        try:
            try:
                async with asyncio.timeout(_WECOM_SEND_SECONDS):
                    sent = await self.sender.send(
                        {"user_id": user_id, "notification": code},
                        progress=progress,
                    )
            except TimeoutError:
                report.last_ok = progress.last_ok
                report.send_status = "unknown" if progress.stage == "wecom_send" else "failed"
                report.fail(progress.stage, "timeout", status=report.send_status)
                return
            if sent.state != "SEND_ACCEPTED":
                report.last_ok = progress.last_ok
                report.http_status = progress.http_status
                report.provider_code = sent.provider_code
                report.send_status = "unknown" if sent.state == "SEND_UNKNOWN" else "failed"
                report.fail(progress.stage, progress.reason or sent.code, status=report.send_status)
                return
            report.send_status = "passed"
            report.last_ok = "wecom_send_accepted"
            try:
                async with asyncio.timeout(_WECOM_REPLY_SECONDS):
                    await received.wait()
            except TimeoutError:
                report.fail("wecom_callback", "reply_not_observed", status="unverified")
        finally:
            self._expected = None
            if received.is_set():
                report.receive_status = "passed"
                report.last_ok = "wecom_callback_verified"
