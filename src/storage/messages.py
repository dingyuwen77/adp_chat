"""企微消息键和独立的 ADP、通知状态转换约束。"""

import json
from hashlib import sha256
from uuid import UUID

from core.models import ChatError


ACTIVE_ADP = {"RECEIVED", "ADP_SEND_ARMED"}
FINAL_ADP = {"ADP_SUCCEEDED", "ADP_REJECTED", "ADP_UNKNOWN", "REJECTED_USER_BUSY"}
FINAL_SEND = {"SEND_ACCEPTED", "SEND_SIMULATED", "SEND_FAILED", "SEND_UNKNOWN"}
SEND_TRANSITIONS = {
    "NONE": {"PENDING"},
    "PENDING": {"SEND_ARMED", "SEND_SIMULATED"},
    "SEND_ARMED": FINAL_SEND,
}
UPDATE_FIELDS = {
    "adp_state", "request_id", "adp_result", "adp_error", "notification",
    "notification_kind", "send_state", "send_result",
}
BUSY_NOTICE = "上一条消息仍在处理中，请等待本次回答完成后重新发送。"


def message_key(corp_id: str, agent_id: int, msg_id: str) -> str:
    """企业、应用与 MsgId 共同确定跨重启稳定的去重键。"""
    encoded = json.dumps([corp_id, agent_id, msg_id], ensure_ascii=False, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def storage_error(code="unavailable") -> ChatError:
    """只返回稳定的安全分类，避免暴露 SQLite 错误 SQL、路径或消息。"""
    return ChatError("gateway_storage", code, "网关消息状态不可用，请检查数据库、容量或后台处理状态。")


def transition(previous: dict, changes: dict, updated_at: str) -> dict:
    """确认版本内的状态转换及出站证据后生成新记录，发送结果不得改写 ADP 事实。"""
    record = {**previous, **changes, "record_version": previous["record_version"] + 1,
              "updated_at": updated_at}
    before, after = previous["adp_state"], record["adp_state"]
    before_send, send = previous["send_state"], record["send_state"]
    allowed = (before == after or (before == "RECEIVED" and after == "ADP_SEND_ARMED")
               or (before == "ADP_SEND_ARMED" and after in FINAL_ADP - {"REJECTED_USER_BUSY"}))
    if (not allowed or (before == after and before in FINAL_ADP and set(changes) - {"send_state", "send_result"})
            or (previous["request_id"] is not None and record["request_id"] != previous["request_id"])
            or (send != before_send and send not in SEND_TRANSITIONS.get(before_send, set()))):
        raise storage_error("invalid_transition")
    try:
        _validate(record)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise storage_error("invalid_transition") from None
    return record


def _validate(record: dict) -> None:
    """确保活跃、终态、通知和环境证据组合遵守当前处理链路约定。"""
    state, send = record["adp_state"], record["send_state"]
    result, error = record["adp_result"], record["adp_error"]
    request_id = record["request_id"]
    busy = state == "REJECTED_USER_BUSY"
    if state == "RECEIVED" or busy:
        if request_id is not None:
            raise ValueError("request")
    elif not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
        raise ValueError("request")
    if state in ACTIVE_ADP:
        if (send != "NONE" or record["notification"] is not None or record["notification_kind"] is not None
                or result is not None or error is not None):
            raise ValueError("active")
    else:
        if (send not in {"PENDING", "SEND_ARMED"} | FINAL_SEND
                or record["notification_kind"] != ("USER_BUSY" if busy else state)):
            raise ValueError("send")
        if not record["redacted"]:
            if not isinstance(record["notification"], str) or not record["notification"]:
                raise ValueError("notification")
            if state == "ADP_SUCCEEDED" and not isinstance(result, dict):
                raise ValueError("result")
            if state in {"ADP_REJECTED", "ADP_UNKNOWN"} and not isinstance(error, dict):
                raise ValueError("error")
        if busy and (result is not None or error is not None):
            raise ValueError("busy result")
    if result is not None and (
        not isinstance(result, dict) or set(result) != {
            "text", "conversation_id", "request_id", "record_id", "reply_method", "reply_contents"
        } or not isinstance(result["text"], str) or result["conversation_id"] != record["conversation_id"]
        or result["request_id"] != request_id
        or (result["reply_method"] is not None and type(result["reply_method"]) is not int)
        or not isinstance(result["reply_contents"], (list, tuple))
    ):
        raise ValueError("result")
    if error is not None and (not isinstance(error, dict) or not isinstance(error.get("code"), str)
                              or not isinstance(error.get("provider_code"), (str, type(None)))):
        raise ValueError("error")
    _validate_send(record)


def _validate_send(record: dict) -> None:
    """真实接受、模拟成功、未知和部分发送记录使用各自的证据。"""
    send, result = record["send_state"], record["send_result"]
    if send in {"NONE", "PENDING"}:
        if result is not None:
            raise ValueError("unexpected send result")
        return
    fields = {"state", "source", "protocol_accepted", "accepted_parts", "total_parts", "code", "provider_code"}
    if (not isinstance(result, dict) or set(result) != fields or result["state"] != send
            or result["source"] not in {"simulated", "fake_http", "wecom_api", "recovery"}
            or not isinstance(result["code"], str) or not result["code"]
            or (result["protocol_accepted"] is not None and type(result["protocol_accepted"]) is not bool)
            or (result["provider_code"] is not None and type(result["provider_code"]) is not int)):
        raise ValueError("send result")
    accepted, total = result["accepted_parts"], result["total_parts"]
    if (accepted is not None and (type(accepted) is not int or accepted < 0)
            or total is not None and (type(total) is not int or total < 1)
            or accepted is not None and total is not None and accepted > total):
        raise ValueError("parts")
    if record["environment"] == "test" and result["source"] == "wecom_api":
        raise ValueError("test evidence")
    if send == "SEND_ACCEPTED" and (result["source"] != "wecom_api" or result["protocol_accepted"] is not True):
        raise ValueError("acceptance evidence")
    if send == "SEND_SIMULATED" and (record["environment"] != "test" or result["source"] not in {"simulated", "fake_http"}):
        raise ValueError("simulation evidence")
    if send in {"SEND_ACCEPTED", "SEND_SIMULATED"} and (total is None or accepted != total):
        raise ValueError("incomplete acceptance")
