"""统一的单用户单会话调用入口。"""

from threading import Lock

from adp.client import chat
from core.config import ChatConfig
from core.models import ChatError, ChatResult
from storage.session import load_session


class ChatService:
    """负责顺序提交同一 ADP 会话中的消息。"""

    def __init__(self, config: ChatConfig) -> None:
        """共享当前配置，并创建每个服务实例独有的互斥锁。"""
        self.config = config
        self._conversation_id: str | None = None
        self._lock = Lock()

    def initialize(self) -> str:
        """首次使用前恢复或创建持久会话；损坏状态不可悄悄重置。"""
        if self._conversation_id is None:
            self._conversation_id = load_session(self.config)
        return self._conversation_id

    def reset_session(self) -> str:
        """仅供已停止 HTTP 服务时的本地 CLI 手工换会话。"""
        if not self._lock.acquire(blocking=False):
            raise ChatError("service", "busy", "当前会话正在处理请求。")
        try:
            self._conversation_id = load_session(self.config, new=True)
            return self._conversation_id
        finally:
            self._lock.release()

    def ask(self, message: str) -> ChatResult:
        """拒绝非法输入和并行提交；仅发送一轮，不自动续答或重试。"""
        if not isinstance(message, str) or not message.strip():
            raise ChatError("input", "empty_question", "问题不能为空。")
        message = message.strip()
        if message.split(maxsplit=1)[0].lower() in ("/image", "/file"):
            raise ChatError("input", "unsupported_command", "当前只支持文字消息。")
        if not self._lock.acquire(blocking=False):
            raise ChatError("service", "busy", "当前会话正在处理请求。")
        try:
            return chat(message, config=self.config, conversation_id=self.initialize())
        finally:
            self._lock.release()
