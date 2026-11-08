"""企业应用正式回调路由，本地模拟同样必须验签。"""

import asyncio
import re
import time

from fastapi import APIRouter, HTTPException, Request, Response

from wecom.crypto import invalid_callback


def _gateway(request: Request):
    """默认关闭新网关，不因增加路由改变旧配置的启动行为。"""
    gateway = getattr(request.app.state, "wecom_gateway", None)
    if gateway is None:
        raise HTTPException(404, "企业微信网关未启用。")
    return gateway


def _parameters(request: Request) -> tuple[str, str, str]:
    """拒绝歧义查询和过期重放；Unix 时间戳保持外部协议语义。"""
    values = []
    for name in ("timestamp", "nonce", "msg_signature"):
        found = request.query_params.getlist(name)
        if len(found) != 1:
            raise invalid_callback()
        values.append(found[0])
    timestamp, nonce, signature = values
    if (not re.fullmatch(r"[0-9]{1,20}", timestamp)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", nonce)
            or abs(time.time() - int(timestamp)) > 300):
        raise invalid_callback()
    return timestamp, nonce, signature


async def verify_url(request: Request) -> Response:
    """返回验签解密后的原始 echo，不加 JSON 包装或换行。"""
    gateway = _gateway(request)
    timestamp, nonce, signature = _parameters(request)
    values = request.query_params.getlist("echostr")
    if len(values) != 1 or len(values[0]) > gateway.config.callback_max_bytes:
        raise invalid_callback()
    plaintext = gateway.crypto.decrypt(values[0], timestamp, nonce, signature)
    return Response(plaintext, media_type="text/plain")


async def receive_message(request: Request) -> Response:
    """有界读取、完整认证、原子可靠接收，最后返回空 ACK。"""
    gateway = _gateway(request)
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > gateway.config.callback_max_bytes:
            raise HTTPException(413, "回调请求过大。")
        body.extend(chunk)
    timestamp, nonce, signature = _parameters(request)
    message = gateway.decode(bytes(body), timestamp, nonce, signature)
    if message is not None:
        await asyncio.to_thread(gateway.receive, message)
    return Response(b"", media_type="text/plain")


def create_router(path: str) -> APIRouter:
    """由启动配置注册同一组GET/POST处理器，不派生第二套认证或业务逻辑。"""
    router = APIRouter()
    router.add_api_route(path, verify_url, methods=["GET"])
    router.add_api_route(path, receive_message, methods=["POST"])
    return router
