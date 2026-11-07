import http.client
import json
import re
from collections.abc import Iterator
from urllib.request import Request

from core.models import ChatError
from .transport import open_request


CHAT_URL = "https://wss.lke.cloud.tencent.com/adp/v2/chat"
_LINE_BREAK = re.compile(rb"\r\n|\r|\n")


class SSEDecoder:
    """同步和异步传输共用逐块 UTF-8/SSE 解析，EOF不补造未闭合事件。"""

    def __init__(self):
        """保留跨块CRLF、中文与事件帧的少量解析状态。"""
        self.pending = bytearray()
        self.after_cr = False
        self.first = True
        self.name = ""
        self.data = []

    def feed(self, chunk: bytes):
        """以完整行解码并仅在空行返回事件，兼容BOM和多行data。"""
        start = int(self.after_cr and chunk.startswith(b"\n"))
        for end in _LINE_BREAK.finditer(chunk, start):
            self.pending.extend(chunk[start:end.start()])
            line = self.pending.decode("utf-8-sig" if self.first else "utf-8")
            self.first = False
            self.pending.clear()
            start = end.end()
            if not line:
                if self.data:
                    yield self.name, "\n".join(self.data)
                self.name, self.data = "", []
            else:
                field, _, value = line.partition(":")
                value = value.removeprefix(" ")
                if field == "event":
                    self.name = value.strip()
                elif field == "data":
                    self.data.append(value)
        self.pending.extend(chunk[start:])
        self.after_cr = chunk.endswith(b"\r")


def decode_event(name: str, data: str) -> tuple[str, dict] | None:
    """共用V2 JSON投影；不在传输层判断ADP业务或副作用。"""
    if data == "[DONE]":
        return None
    event = json.loads(data)
    if not isinstance(event, dict):
        raise ChatError("chat", "invalid_event", "SSE 事件的数据结构无效。")
    kind = event.get("Type") or name
    if not isinstance(kind, str):
        raise ChatError("chat", "invalid_event", "SSE 事件类型无效。")
    return kind, event


def _frames(response) -> Iterator[tuple[str, str]]:
    """按空行切分 SSE 事件，丢弃断流时尚未闭合的事件。"""
    decoder = SSEDecoder()
    while chunk := response.read1(65536):
        yield from decoder.feed(chunk)


def stream_events(payload: dict, timeout: float) -> Iterator[tuple[str, dict]]:
    """解析 V2 的 SSE 响应并可靠关闭连接；关闭流式文本后仍使用此协议。"""
    request = Request(
        CHAT_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Accept": "text/event-stream", "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with open_request(request, timeout=timeout, stage="chat") as response:
            content_type = response.headers.get("Content-Type", "").partition(";")[0].strip().lower()
            if content_type != "text/event-stream":
                raise ChatError("chat", "invalid_response", "对话服务没有返回 SSE 流。")
            for name, data in _frames(response):
                decoded = decode_event(name, data)
                if decoded is None:
                    return
                yield decoded
    except (UnicodeError, json.JSONDecodeError):
        raise ChatError("chat", "invalid_event", "SSE 事件不是有效的 UTF-8 JSON。") from None
    except TimeoutError:
        raise ChatError("chat", "timeout", "读取对话流超时，服务端可能已接收该轮。") from None
    except (OSError, http.client.HTTPException):
        raise ChatError("chat", "network_error", "读取对话流时连接中断。") from None
