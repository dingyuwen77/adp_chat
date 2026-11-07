"""三种文字入口共用的正式结果展示。"""

import json
import sys

from core.models import ChatResult


def show_result(result: ChatResult) -> None:
    """显示正式文本及非文本内容，保留类型供终端调用者辨认。"""
    if result.text:
        print(result.text)
    nontext = [part for message in result.reply_contents for part in message if part.get("Type") not in ("text", "json_text")]
    for part in nontext:
        print(json.dumps(part, ensure_ascii=False))
    if not result.text and not nontext:
        print("[本轮正式回复没有内容。]", file=sys.stderr)
