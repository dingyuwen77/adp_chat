"""校验调用方与 ADP 应用明确约定的业务交互协议。"""

import json

from ..core.models import ChatError, Interaction


def _unique_object(pairs: list[tuple]) -> dict:
    """拒绝重复键，防止同一状态在解析时被静默覆盖。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def parse_interaction(text: str) -> Interaction:
    """严格解析本项目业务状态；契约损坏时不退回文字猜测。"""
    try:
        envelope = json.loads(text, object_pairs_hook=_unique_object)
        if not isinstance(envelope, dict) or set(envelope) != {"schema", "interaction"}:
            raise ValueError
        if envelope["schema"] != "adp-chat.interaction.v1":
            raise ValueError
        data = envelope["interaction"]
        fields = {"type", "status", "message", "missing_fields", "confirmation_required"}
        if not isinstance(data, dict) or set(data) != fields:
            raise ValueError
        kind, status, message = data["type"], data["status"], data["message"]
        if not isinstance(kind, str) or kind not in ("ANSWER", "CLARIFICATION", "CONFIRMATION"):
            raise ValueError
        if status != ("COMPLETED" if kind == "ANSWER" else "WAITING_USER"):
            raise ValueError
        if not isinstance(message, str) or not message.strip():
            raise ValueError
        missing = data["missing_fields"]
        if not isinstance(missing, list) or any(not isinstance(item, str) or not item.strip() for item in missing):
            raise ValueError
        if len(set(missing)) != len(missing) or kind != "CLARIFICATION" and missing:
            raise ValueError
        confirm = data["confirmation_required"]
        if type(confirm) is not bool or confirm != (kind == "CONFIRMATION"):
            raise ValueError
        return Interaction(kind, status, message, tuple(missing), confirm)
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ChatError("chat", "invalid_interaction", "ADP 业务交互输出不符合已配置的结构化契约。") from None
