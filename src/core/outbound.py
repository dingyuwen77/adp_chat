"""传输层可证实的排障阶段，避免将连接失败推断为防火墙拦截。"""

import socket
import ssl
from dataclasses import dataclass


@dataclass(slots=True)
class OutboundProgress:
    """保存本次调用的阶段和安全错误分类，不持有原始异常或请求内容。"""

    stage: str
    last_ok: str = "request_received"
    reason: str | None = None
    http_status: int | None = None


def network_reason(error: BaseException, fallback: str) -> str:
    """仅在异常链提供证据时区分 DNS、TLS 和连接拒绝。"""
    seen = set()
    while error is not None and id(error) not in seen:
        if isinstance(error, socket.gaierror):
            return "dns_error"
        if isinstance(error, ssl.SSLError):
            return "tls_error"
        if isinstance(error, ConnectionRefusedError):
            return "connect_refused"
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return fallback
