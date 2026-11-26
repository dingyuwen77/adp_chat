import http.client
import ssl
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

from core.models import ChatError
from core.outbound import network_reason


class _NoRedirect(HTTPRedirectHandler):
    """拒绝携带请求正文或签名的地址跳转。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """把跳转留给调用方按 HTTP 错误处理。"""
        return None


def open_request(request: Request, *, timeout: float, stage: str):
    """打开一个验证证书的 HTTPS 请求，返回由调用方关闭的响应。"""
    # 单独创建 TLS 上下文，避免默认工厂读取宿主的 SSLKEYLOGFILE。
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        context.load_default_certs()
        opener = build_opener(HTTPSHandler(context=context), _NoRedirect())
        return opener.open(request, timeout=timeout)
    except HTTPError as exc:
        status = exc.code
        exc.close()
        raise ChatError(stage, "http_error", f"服务返回 HTTP {status}。", http_status=status,
                        failed_stage="adp_http") from None
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ChatError(stage, "timeout", "等待服务响应超时。", failed_stage="adp_connect_or_response") from None
        reason = network_reason(exc.reason, "network_error") if isinstance(exc.reason, BaseException) else "network_error"
        raise ChatError(stage, "network_error", "无法建立服务连接。",
                        failed_stage="adp_connect_or_response", reason=reason) from None
    except TimeoutError:
        raise ChatError(stage, "timeout", "等待服务响应超时。", failed_stage="adp_connect_or_response") from None
    except ValueError:
        raise ChatError(stage, "invalid_request", "HTTPS 请求地址或请求头无效。", failed_stage="adp_prepare") from None
    except (OSError, http.client.HTTPException) as exc:
        raise ChatError(stage, "network_error", "无法建立服务连接。", failed_stage="adp_connect_or_response",
                        reason=network_reason(exc, "network_error")) from None
