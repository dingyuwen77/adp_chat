from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ChatResult:
    """一轮成功完成的正式回复、原始内容与关联标识。"""

    text: str
    conversation_id: str
    request_id: str
    record_id: str | None = None
    reply_method: int | None = None
    reply_contents: tuple[tuple[dict, ...], ...] = field(default=(), repr=False)


class ChatError(Exception):
    """配置、输入或对话失败，包含可安全展示的排障信息。"""

    def __init__(
        self,
        stage: str,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
        provider_code: str | None = None,
        provider_request_id: str | None = None,
        provider_trace_id: str | None = None,
    ) -> None:
        """记录错误分类和可选的服务端标识。"""
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.http_status = http_status
        self.provider_code = provider_code
        self.provider_request_id = provider_request_id
        self.provider_trace_id = provider_trace_id
        self.conversation_id: str | None = None
        self.request_id: str | None = None
