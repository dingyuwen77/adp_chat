import re
import json
from collections import Counter
from contextlib import closing
from copy import deepcopy
from logging import INFO
from time import monotonic
from uuid import uuid4

from core.config import ChatConfig
from core.logging import conversation_text, diagnostic_logger
from core.models import ChatError, ChatResult
from .sse import stream_events


_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,64}")
_DIAGNOSTIC_PATTERN = re.compile(r"[A-Za-z0-9._/ -]{1,128}")


def _safe_diagnostic(value: object) -> str | None:
    """只保留格式正常的服务端错误码和关联 ID。"""
    if isinstance(value, (str, int)) and _DIAGNOSTIC_PATTERN.fullmatch(str(value)):
        return str(value)
    return None


def _provider_error_message(code: str | None) -> str:
    """将已确认的服务端错误码解释为安全提示，不显示未经筛选的原始错误正文。"""
    if code == "460701":
        return "ADP 专属模型超出并发数限制（460701）。请等待现有请求结束后重试，并检查专属模型并发配额。"
    if code is not None and code.isascii() and code.isdigit():
        return f"ADP 返回了对话错误（服务端错误码：{code}）。"
    return "ADP 返回了对话错误。"


def _payload(config: ChatConfig, request_id: str, conversation_id: str, contents: list) -> dict:
    """按配置选择文本输出模式；ADP 始终通过 SSE 交付状态和正式回复。"""
    payload = {
        "AppKey": config.app_key,
        "UserId": config.user_id,
        "VisitorId": config.visitor_id or config.user_id,
        "ConversationId": conversation_id,
        "RequestId": request_id,
        "Contents": contents,
        "Stream": "enable" if config.stream else "disable",
        "Incremental": True,
    }
    optional = {
        "UserName": config.user_name,
        "SystemRole": config.system_role, "SearchNetwork": config.search_network,
        "WorkflowStatus": config.workflow_status,
    }
    payload.update({key: value for key, value in optional.items() if value})
    if config.stream and config.streaming_throttle:
        payload["StreamingThrottle"] = config.streaming_throttle
    if config.enable_multi_intent is not None:
        payload["EnableMultiIntent"] = config.enable_multi_intent
    return payload


def _object(value: object) -> dict:
    """把缺失的事件对象按空对象处理，拒绝结构错误。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ChatError("chat", "invalid_event", "SSE 事件对象结构无效。")
    return value


def _reply_contents(record: dict) -> list[list[dict]]:
    """按消息保留回复内容，排除工具和思考消息。"""
    replies = []
    messages = record.get("Messages")
    if messages is None:
        return []
    if not isinstance(messages, list):
        raise ChatError("chat", "invalid_event", "最终回复的消息列表无效。")
    for item in messages:
        message = _object(item)
        if message.get("Type") != "reply":
            continue
        contents = message.get("Contents")
        if contents is None:
            continue
        if not isinstance(contents, list):
            raise ChatError("chat", "invalid_event", "最终回复的内容列表无效。")
        parts = []
        for item in contents:
            content = _object(item)
            if content.get("Type") in ("text", "json_text"):
                text = content.get("Text")
                if not isinstance(text, str):
                    raise ChatError("chat", "invalid_event", "最终回复的文本字段无效。")
            parts.append(content)
        replies.append(parts)
    return replies


def _result(record: dict, conversation_id: str, request_id: str) -> ChatResult:
    """提取正式 reply 文本并完整保留原始内容，不推断业务完成或自动补答。"""
    contents = _reply_contents(record)
    messages = ["".join(part["Text"] for part in message if part.get("Type") in ("text", "json_text")) for message in contents]
    method = _object(record.get("ExtraInfo")).get("ReplyMethod")
    if method is not None and type(method) is not int:
        raise ChatError("chat", "invalid_event", "回复方式字段无效。")
    return ChatResult(
        text="\n".join(text for text in messages if text), conversation_id=conversation_id,
        request_id=request_id, record_id=_safe_diagnostic(record.get("RecordId")),
        reply_method=method,
        reply_contents=tuple(tuple(deepcopy(part) for part in message) for message in contents),
    )


def _completed_record(payload: dict, config: ChatConfig, logger) -> dict:
    """消费两种文本输出模式的 SSE，仅返回已确认成功的正式终态。"""
    counts = Counter()
    with closing(stream_events(payload, config.chat_timeout_seconds)) as events:
        for kind, event in events:
            known = kind if kind in {
                "error", "request_ack", "response.created", "response.processing", "response.completed",
                "message.added", "message.processing", "message.done", "text.delta", "text.replace",
            } else "other"
            counts[known] += 1
            if kind == "error":
                error = _object(event.get("Error"))
                provider_code = _safe_diagnostic(error.get("Code"))
                raise ChatError(
                    "chat", "provider_error", _provider_error_message(provider_code),
                    provider_code=provider_code,
                    provider_request_id=_safe_diagnostic(error.get("RequestId")),
                    provider_trace_id=_safe_diagnostic(error.get("TraceId")),
                )
            if kind == "request_ack":
                acknowledgement = _object(event.get("RequestAck"))
                _check_conversation(acknowledgement, payload["ConversationId"])
                if acknowledgement.get("Status") != "success":
                    raise ChatError("chat", "request_rejected", "ADP 未接受本轮请求。")
            if kind == "response.completed":
                record = _object(event.get("Response"))
                _check_conversation(record, payload["ConversationId"])
                if record.get("Status") != "success":
                    raise ChatError("chat", "response_failed", "ADP 对话未成功完成。")
                logger.debug("sse_completed conversation_id=%s request_id=%s events=%s", payload["ConversationId"], payload["RequestId"], json.dumps(dict(counts), ensure_ascii=False, separators=(",", ":")))
                return record
    raise ChatError("chat", "incomplete", "对话流已结束，未收到成功完成事件。")


def _check_conversation(record: dict, conversation_id: str) -> None:
    """兼容未返回标识的事件；一旦返回则严格验证，不回显未经信任的值。"""
    if "ConversationId" in record and record["ConversationId"] != conversation_id:
        raise ChatError("chat", "conversation_mismatch", "ADP 返回的会话标识与本轮提交值不一致。")


def chat(
    question: str,
    *,
    config: ChatConfig,
    conversation_id: str | None = None,
) -> ChatResult:
    """按配置请求一轮文字对话，等待正式成功后返回，不自动续答或重试。"""
    if not isinstance(config, ChatConfig):
        raise ChatError("config", "invalid_config", "config 必须是 load_config() 返回的对话配置。")
    if not isinstance(question, str) or not question.strip():
        raise ChatError("input", "empty_question", "问题必须是非空文字。")
    if conversation_id is not None and (
        not isinstance(conversation_id, str) or not _ID_PATTERN.fullmatch(conversation_id)
    ):
        raise ChatError("input", "invalid_conversation_id", "会话 ID 应为 32 至 64 个字母、数字、下划线或横杠。")
    conversation_id = conversation_id or str(uuid4())
    request_id = str(uuid4())
    with diagnostic_logger(config) as logger:
        started = monotonic()
        logger.info("request_started conversation_id=%s request_id=%s stream=%s", conversation_id, request_id, config.stream)
        if logger.isEnabledFor(INFO) and config.log_question:
            logger.info("user_question conversation_id=%s request_id=%s question=%s", conversation_id, request_id, conversation_text(config, question))
        try:
            payload = _payload(config, request_id, conversation_id, [{"Type": "text", "Text": question}])
            record = _completed_record(payload, config, logger)
            result = _result(record, conversation_id, request_id)
            if logger.isEnabledFor(INFO):
                logger.info("adp_answer conversation_id=%s request_id=%s answer_length=%d answer_truncated=%s answer_preview=%s", conversation_id, request_id, len(result.text), len(result.text) > config.log_answer_chars, conversation_text(config, result.text, limit=config.log_answer_chars))
            logger.info("request_completed conversation_id=%s request_id=%s record_id=%s reply_method=%s elapsed_ms=%.1f", conversation_id, request_id, result.record_id, result.reply_method, (monotonic() - started) * 1000)
            return result
        except ChatError as exc:
            exc.conversation_id = conversation_id
            exc.request_id = request_id
            logger.error("request_failed conversation_id=%s request_id=%s stage=%s code=%s http_status=%s provider_code=%s provider_request_id=%s provider_trace_id=%s elapsed_ms=%.1f", conversation_id, request_id, exc.stage, exc.code, exc.http_status, exc.provider_code, exc.provider_request_id, exc.provider_trace_id, (monotonic() - started) * 1000)
            raise
        except Exception:
            logger.warning("request_interrupted conversation_id=%s request_id=%s", conversation_id, request_id)
            raise
