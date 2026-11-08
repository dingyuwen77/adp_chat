"""真实ADP协议投影的离线回放；不读取实际配置、不创建外部网络连接。"""

import json
from pathlib import Path

import httpx


def recorded_cases():
    """读取已脱敏的固定样本，缺失/版本不符直接失败而不退回云端探测。"""
    path = Path(__file__).parent / "fixtures" / "adp_http_sse_recorded.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data["schema"] != "adp-http-sse-recorded/v1":
        raise ValueError("不支持的ADP录制样本版本")
    cases = {case["id"]: case for case in data["cases"]}
    if len(cases) != len(data["cases"]):
        raise ValueError("录制样本ID重复")
    return cases


def recorded_sse(case, payload, *, reply_label=None):
    """绑定本轮关联ID；可用隔离标签替代脱敏正文，仅用于验证投递关联。"""
    bindings = {"{{conversation_id}}": payload["ConversationId"], "{{request_id}}": payload["RequestId"]}

    def bind(value):
        """只替换明确占位，不改变实际事件顺序、状态、Code或ReplyMethod。"""
        if isinstance(value, dict):
            return {key: bind(item) for key, item in value.items()}
        if isinstance(value, list):
            return [bind(item) for item in value]
        if isinstance(value, str):
            if reply_label is not None and value == "[回答正文已脱敏]":
                return reply_label
            return bindings.get(value, value)
        return value

    return b"".join(("event: " + frame["event"] + "\r\ndata: "
        + json.dumps(bind(frame["data"]), ensure_ascii=False) + "\r\n\r\n").encode("utf-8")
        for frame in case["frames"])


class RecordedStream(httpx.AsyncByteStream):
    """按变化字节边界回放真实协议投影，覆盖UTF8与SSE跨块而不依赖外网。"""

    def __init__(self, content):
        """只保存内存字节，生命周期不持有socket或真实凭据。"""
        self.content = content

    async def __aiter__(self):
        """保留全部字节与事件边界，不对错误或末尾补造成功事件。"""
        position, index = 0, 0
        sizes = (1, 2, 7, 64)
        while position < len(self.content):
            size = sizes[index % len(sizes)]
            yield self.content[position:position + size]
            position += size
            index += 1
