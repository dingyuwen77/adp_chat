"""FastAPI 常驻服务及统一异常边界。"""

from contextlib import asynccontextmanager, ExitStack, AsyncExitStack
import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from application.chat_service import ChatService
from application.wecom_gateway import WeComGateway
from application.wecom_worker import GatewayWorker
from adp.async_client import AsyncADPClient
from wecom.sender import create_sender
from core.config import DEFAULT_CALLBACK_PATH, load_config
from core.logging import CallbackAccessFilter, diagnostic_logger
from core.models import ChatError
from .routes import router
from .wecom_routes import create_router as create_wecom_router


def _configure_callback_routes(app: FastAPI, path: str) -> None:
    """只在接收请求前更换本Owner的路由，支持lifespan重启且不残留旧入口。"""
    if getattr(app.state, "wecom_callback_path", None) == path:
        return
    previous = getattr(app.state, "wecom_callback_routes", ())
    app.router.routes[:] = [route for route in app.router.routes
                           if not any(route is owned for owned in previous)]
    start = len(app.router.routes)
    app.include_router(create_wecom_router(path))
    app.state.wecom_callback_routes = tuple(app.router.routes[start:])
    app.state.wecom_callback_path = path
    # OpenAPI可能在lifespan之前已经生成；必须反映实际接收路径。
    app.openapi_schema = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """在接收 HTTP 请求前加载配置并持久化会话 ID。"""
    config = load_config()
    _configure_callback_routes(app, config.callback_path)
    service = ChatService(config)
    service.initialize()
    app.state.chat_service = service
    app.state.wecom_gateway = None
    with ExitStack() as resources:
        if config.gateway_enabled:
            logger = resources.enter_context(diagnostic_logger(config))
            gateway = WeComGateway(config, logger)
            resources.callback(gateway.close)
            access = logging.getLogger("uvicorn.access")
            access_filter = CallbackAccessFilter()
            access.addFilter(access_filter)
            resources.callback(access.removeFilter, access_filter)
            app.state.wecom_gateway = gateway
            async with AsyncExitStack() as outgoing:
                adp = AsyncADPClient(config)
                outgoing.push_async_callback(adp.aclose)
                sender = create_sender(config, logger=logger)
                if sender is not None:
                    outgoing.push_async_callback(sender.aclose)
                worker = GatewayWorker(gateway, adp, sender=sender)
                outgoing.push_async_callback(worker.stop)
                await worker.start()
                gateway.events.emit("service.started")
                yield
            gateway.events.emit("service.stopped")
        else:
            yield


def _error(status: int, code: str, stage: str, message: str, request_id: str | None = None) -> JSONResponse:
    """确保错误仅包含本服务生成的安全字段。"""
    return JSONResponse(status_code=status, content={"error": {
        "code": code, "stage": stage, "message": message, "request_id": request_id,
    }})


def create_app() -> FastAPI:
    """构造 HTTP 服务，不在导入模块时读取密钥或创建会话。"""
    app = FastAPI(title="ADP 单用户对话服务", lifespan=lifespan)
    app.include_router(router)
    _configure_callback_routes(app, DEFAULT_CALLBACK_PATH)

    @app.exception_handler(ChatError)
    async def chat_error_handler(_request: Request, exc: ChatError) -> JSONResponse:
        """根据失败所属边界返回可靠的 HTTP 状态。"""
        if exc.stage == "service" and exc.code == "busy":
            return _error(409, "CHAT_BUSY", "service", "当前 ADP 会话正在处理请求。")
        if exc.stage == "input":
            return _error(422, "INVALID_REQUEST", "input", "请求内容不合法。")
        if exc.stage == "wecom":
            gateway = getattr(_request.app.state, "wecom_gateway", None)
            if gateway is not None:
                gateway.events.emit("wecom.callback.rejected", error_stage="authentication", error_code=exc.code,
                    attributes={"source": "test_fixture" if gateway.config.gateway_environment == "test" else "wecom_callback"})
            return _error(403, "WECOM_INVALID_CALLBACK", "wecom", "回调认证或消息格式无效。")
        if exc.stage in ("config", "session", "logging", "gateway_storage"):
            return _error(503, "SERVICE_UNAVAILABLE", exc.stage, "服务配置或持久化不可用。")
        if exc.code == "timeout":
            return _error(504, "ADP_TIMEOUT", "chat", "ADP 请求超时，结果可能不确定。", exc.request_id)
        return _error(502, "ADP_ERROR", exc.stage, "ADP 对话未成功完成。", exc.request_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, _exc: RequestValidationError) -> JSONResponse:
        """拦截校验报错，避免在响应中回显请求消息。"""
        return _error(422, "INVALID_REQUEST", "input", "请求必须包含非空文字 message。")

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
        """隐藏意外异常的正文，给调用方稳定的 500 响应。"""
        return _error(500, "INTERNAL_ERROR", "service", "服务内部错误。")

    return app


app = create_app()
