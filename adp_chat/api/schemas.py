"""面向内部 HTTP 调用者的固定单轮对话契约。"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ChatRequest(BaseModel):
    """每次只允许提交一条文字消息。"""

    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        """拒绝空白内容，防止无意义的 ADP 请求。"""
        if not value.strip():
            raise ValueError("消息不能为空")
        return value


class ChatResponse(BaseModel):
    """保留正式 ADP 回复的内容类型与关联 ID。"""

    text: str
    conversation_id: str
    request_id: str
    record_id: str | None
    reply_method: int | None
    reply_contents: list[list[dict[str, Any]]]


class ErrorDetail(BaseModel):
    """可安全返回给客户端的错误。"""

    code: str
    message: str
    stage: str
    request_id: str | None = None


class ErrorResponse(BaseModel):
    """统一错误包裹结构。"""

    error: ErrorDetail
