import http.client
import json
import re
from collections.abc import Iterator
from urllib.request import Request

from core.models import ChatError
from .transport import open_request


CHAT_URL = "https://wss.lke.cloud.tencent.com/adp/v2/chat"
_LINE_BREAK = re.compile(rb"\r\n|\r|\n")


def _lines(response) -> Iterator[bytes]:
    """流式读取 LF、CRLF 和 CR 行，并处理跨网络块的 CRLF。"""
    pending = bytearray()
    after_cr = False
    while chunk := response.read1(65536):
        start = int(after_cr and chunk.startswith(b"\n"))
        for end in _LINE_BREAK.finditer(chunk, start):
            pending.extend(chunk[start:end.start()])
            yield bytes(pending)
            pending.clear()
            start = end.end()
        pending.extend(chunk[start:])
        after_cr = chunk.endswith(b"\r")


def _frames(response) -> Iterator[tuple[str, str]]:
    """按空行切分 SSE 事件，丢弃断流时尚未闭合的事件。"""
    name = ""
    lines = []
    for index, raw_line in enumerate(_lines(response)):
        encoding = "utf-8-sig" if index == 0 else "utf-8"
        line = raw_line.decode(encoding)
        if not line:
            if lines:
                yield name, "\n".join(lines)
            name = ""
            lines = []
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            name = value.strip()
        elif field == "data":
            lines.append(value)


def stream_events(payload: dict, timeout: float) -> Iterator[tuple[str, dict]]:
    """发送一轮 V2 对话，交付已解码的事件，退出时关闭 HTTP 响应。"""
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
                if data == "[DONE]":
                    return
                event = json.loads(data)
                if not isinstance(event, dict):
                    raise ChatError("chat", "invalid_event", "SSE 事件的数据结构无效。")
                kind = event.get("Type") or name
                if not isinstance(kind, str):
                    raise ChatError("chat", "invalid_event", "SSE 事件类型无效。")
                yield kind, event
    except (UnicodeError, json.JSONDecodeError):
        raise ChatError("chat", "invalid_event", "SSE 事件不是有效的 UTF-8 JSON。") from None
    except TimeoutError:
        raise ChatError("chat", "timeout", "读取对话流超时，服务端可能已接收该轮。") from None
    except (OSError, http.client.HTTPException):
        raise ChatError("chat", "network_error", "读取对话流时连接中断。") from None
