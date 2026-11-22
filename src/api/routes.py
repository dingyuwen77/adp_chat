"""FastAPI HTTP 路由。"""

from fastapi import APIRouter, Request

from application.chat_service import ChatService
from core.models import ChatError
from .schemas import ChatRequest, ChatResponse, ErrorResponse

router = APIRouter()


def _service(request: Request) -> ChatService:
    """读取生命周期初始化的唯一聊天服务。"""
    return request.app.state.chat_service


@router.get("/healthz")
def healthz(request: Request) -> dict[str, str]:
    """探测本机进程和网关持久状态，保持上游调用为零。"""
    gateway = getattr(request.app.state, "wecom_gateway", None)
    if gateway is not None and not gateway.healthy:
        raise ChatError("gateway_storage", "unavailable", "企业微信状态处理不可用。")
    return {"status": "ok"}


@router.post(
    "/api/v1/chat", response_model=ChatResponse,
    responses={status: {"model": ErrorResponse} for status in (409, 422, 502, 503, 504, 500)},
)
def chat_endpoint(body: ChatRequest, request: Request) -> ChatResponse:
    """将文字转发一轮，返回正式 ADP 消息。"""
    result = _service(request).ask(body.message)
    return ChatResponse(
        text=result.text,
        conversation_id=result.conversation_id,
        request_id=result.request_id,
        record_id=result.record_id,
        reply_method=result.reply_method,
        reply_contents=[[dict(part) for part in message] for message in result.reply_contents],
    )
