"""FastAPI 常驻服务及统一异常边界。"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from application.chat_service import ChatService
from core.config import load_config
from core.models import ChatError
from .routes import router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """在接收 HTTP 请求前加载配置并持久化会话 ID。"""
    service = ChatService(load_config())
    service.initialize()
    app.state.chat_service = service
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

    @app.exception_handler(ChatError)
    async def chat_error_handler(_request: Request, exc: ChatError) -> JSONResponse:
        """根据失败所属边界返回可靠的 HTTP 状态。"""
        if exc.stage == "service" and exc.code == "busy":
            return _error(409, "CHAT_BUSY", "service", "当前 ADP 会话正在处理请求。")
        if exc.stage == "input":
            return _error(422, "INVALID_REQUEST", "input", "请求内容不合法。")
        if exc.stage in ("config", "session", "logging"):
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
