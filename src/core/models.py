from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class Interaction:
    """应用明确提供的业务状态，独立于 ADP 的回复方式枚举。"""

    type: Literal["ANSWER", "CLARIFICATION", "CONFIRMATION"]
    status: Literal["COMPLETED", "WAITING_USER"]
    message: str
    missing_fields: tuple[str, ...] = ()
    confirmation_required: bool = False


@dataclass(frozen=True, slots=True)
class ChatResult:
    """一轮成功完成的对话及其关联标识。"""

    text: str
    conversation_id: str
    request_id: str
    record_id: str | None = None
    reply_method: int | None = None
    needs_input: bool = False
    input_kind: Literal["question", "clarification_answer"] = "question"
    questionnaires: tuple[dict, ...] = field(default=(), repr=False)
    widgets: tuple[dict, ...] = field(default=(), repr=False)
    clarification_source: Literal["interaction", "reply_method", "questionnaire", "text_rule"] | None = None
    original_question: str = field(default="", repr=False)
    interaction: Interaction | None = None
    reply_contents: tuple[tuple[dict, ...], ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class ClarificationAnswer:
    """对 ADP 追问的作答；自定义 Widget 适配器可提交正式动作字段。"""

    text: str = ""
    widget_actions: tuple[dict, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class PreviewEvent:
    """回复消息一个文本片段的增量或替换。"""

    kind: Literal["delta", "replace"]
    message_id: str
    content_index: int
    text: str


class ChatError(Exception):
    """配置、输入、附件或对话失败，包含可安全展示的排障信息。"""

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
