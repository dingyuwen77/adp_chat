import re
from collections import Counter
from collections.abc import Callable
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from logging import INFO
from time import monotonic
from uuid import uuid4

from .config import ChatConfig
from .clarification import is_text_clarification
from .interaction import parse_interaction
from .logging import conversation_text, diagnostic_logger
from .models import ChatError, ChatResult, ClarificationAnswer, PreviewEvent
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
    """以统一的 V2 字段组装文本、图片或文档请求。"""
    payload = {
        "AppKey": config.app_key,
        "UserId": config.user_id,
        "VisitorId": config.visitor_id or config.user_id,
        "ConversationId": conversation_id,
        "RequestId": request_id,
        "Contents": contents,
        "Stream": "enable",
        "Incremental": config.incremental,
    }
    optional = {
        "UserName": config.user_name, "StreamingThrottle": config.streaming_throttle,
        "SystemRole": config.system_role, "SearchNetwork": config.search_network,
        "WorkflowStatus": config.workflow_status,
    }
    payload.update({key: value for key, value in optional.items() if value})
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


def _result(
    record: dict, conversation_id: str, request_id: str, input_kind: str,
    question: str, config: ChatConfig,
) -> ChatResult:
    """先依据正式标记，再按需保守推断普通智能体回复中的文字追问。"""
    contents = _reply_contents(record)
    messages = []
    questionnaires, widgets = [], []
    for message in contents:
        parts = []
        for content in message:
            kind = content.get("Type")
            if kind in ("text", "json_text"):
                parts.append(content["Text"])
            elif kind == "questionnaire":
                questionnaire = _object(content.get("Questionnaire"))
                pending = _pending_questionnaire(questionnaire)
                if pending is not None:
                    questionnaires.append(pending)
            elif kind == "widget":
                widgets.append(deepcopy(_object(content.get("Widget"))))
        if parts:
            messages.append("".join(parts))
    method = _object(record.get("ExtraInfo")).get("ReplyMethod")
    if method is not None and type(method) is not int:
        raise ChatError("chat", "invalid_event", "回复方式字段无效。")
    text = "\n".join(messages)
    interaction = None
    if config.structured_interaction:
        if text.lstrip().startswith("{") or method != 15 and not questionnaires:
            interaction = parse_interaction(text)
            if interaction.status == "COMPLETED" and (method == 15 or questionnaires):
                raise ChatError("chat", "invalid_interaction", "业务完成状态与 ADP 等待作答状态冲突。")
            text = interaction.message
    source = None
    if interaction is not None:
        source = "interaction" if interaction.status == "WAITING_USER" else None
    elif method == 15:
        source = "reply_method"
    elif questionnaires:
        source = "questionnaire"
    elif not config.structured_interaction and method == 18 and config.detect_text_clarification and is_text_clarification(question, text):
        source = "text_rule"
    return ChatResult(
        text=text, conversation_id=conversation_id,
        request_id=request_id, record_id=_safe_diagnostic(record.get("RecordId")),
        reply_method=method, needs_input=source is not None, clarification_source=source,
        input_kind=input_kind, questionnaires=tuple(questionnaires), widgets=tuple(widgets),
        original_question=question,
        interaction=interaction,
    )


def _pending_questionnaire(questionnaire: dict) -> dict | None:
    """保留尚未填写的题目；已填或部分填写回显不应重复收集同一答案。"""
    questions = questionnaire.get("Questions") or []
    answers = questionnaire.get("Answers") or []
    if not isinstance(questions, list) or not isinstance(answers, list):
        raise ChatError("chat", "invalid_event", "问卷的问题或答案列表无效。")
    answered = set()
    for answer in answers:
        if not isinstance(answer, dict) or not isinstance(answer.get("Question"), str):
            raise ChatError("chat", "invalid_event", "问卷答案字段无效。")
        labels = answer.get("SelectedLabels") or []
        if not isinstance(labels, list) or any(not isinstance(label, str) for label in labels):
            raise ChatError("chat", "invalid_event", "问卷选中项字段无效。")
        if labels:
            answered.add(answer["Question"])
    pending = []
    for question in questions:
        if not isinstance(question, dict) or not isinstance(question.get("Question"), str):
            raise ChatError("chat", "invalid_event", "问卷题目字段无效。")
        if question["Question"] not in answered:
            pending.append(question)
    if not pending:
        return None
    result = deepcopy(questionnaire)
    result["Questions"] = deepcopy(pending)
    return result


def _answer_contents(question: str | ClarificationAnswer, reply_to: ChatResult | None) -> list[dict]:
    """校验文本或 Widget 作答，避免无等待状态时提交卡片动作。"""
    if isinstance(question, str):
        if not question.strip():
            raise ChatError("input", "empty_question", "问题不能为空。")
        return [{"Type": "text", "Text": question}]
    if not isinstance(question, ClarificationAnswer) or reply_to is None or not reply_to.needs_input:
        raise ChatError("input", "invalid_answer", "结构化作答需要传入正在等待回答的 reply_to。")
    if not isinstance(question.text, str) or not isinstance(question.widget_actions, tuple):
        raise ChatError("input", "invalid_answer", "补充答案的文本或动作列表无效。")
    contents = [{"Type": "text", "Text": question.text}] if question.text.strip() else []
    for action in question.widget_actions:
        fields = ("WidgetId", "WidgetRunId", "ActionType", "Payload")
        if not isinstance(action, dict) or any(
            not isinstance(action.get(key), str) or not action[key].strip() for key in fields
        ):
            raise ChatError("input", "invalid_answer", "Widget 动作缺少必需的字符串字段。")
        if not any(
            widget.get("WidgetId") == action["WidgetId"]
            and widget.get("WidgetRunId") == action["WidgetRunId"] for widget in reply_to.widgets
        ):
            raise ChatError("input", "invalid_answer", "Widget 动作不属于本轮追问卡片。")
        contents.append({"Type": "widget_action", "WidgetAction": {key: action[key] for key in fields}})
    if not contents:
        raise ChatError("input", "empty_question", "补充答案不能为空。")
    return contents


def _completed_record(payload: dict, config: ChatConfig, on_preview, logger) -> dict:
    """验证对话终态，并按需交付属于回复消息的预览。"""
    reply_ids = set()
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
            if kind == "request_ack" and _object(event.get("RequestAck")).get("Status") != "success":
                raise ChatError("chat", "request_rejected", "ADP 未接受本轮请求。")
            if kind == "response.completed":
                record = _object(event.get("Response"))
                if record.get("Status") != "success":
                    raise ChatError("chat", "response_failed", "ADP 对话未成功完成。")
                logger.debug("sse_completed conversation_id=%s request_id=%s events=%s", payload["ConversationId"], payload["RequestId"], dict(counts))
                return record
            if on_preview is None:
                continue
            if kind in ("message.added", "message.processing", "message.done"):
                message = _object(event.get("Message"))
                message_id = event.get("MessageId") or message.get("MessageId")
                if message.get("Type") == "reply" and isinstance(message_id, str) and message_id:
                    reply_ids.add(message_id)
            elif kind in ("text.delta", "text.replace"):
                message_id = event.get("MessageId")
                index, text = event.get("ContentIndex"), event.get("Text")
                if (
                    isinstance(message_id, str) and message_id in reply_ids
                    and type(index) is int and index >= 0 and isinstance(text, str)
                ):
                    operation = "delta" if kind == "text.delta" else "replace"
                    on_preview(PreviewEvent(operation, message_id, index, text))
    raise ChatError("chat", "incomplete", "对话流已结束，未收到成功完成事件。")


def chat(
    question: str | ClarificationAnswer,
    *,
    config: ChatConfig,
    file: str | Path | None = None,
    image: str | Path | None = None,
    conversation_id: str | None = None,
    reply_to: ChatResult | None = None,
    on_preview: Callable[[PreviewEvent], None] | None = None,
) -> ChatResult:
    """执行一轮 ADP Multi-Agent 对话，可附带一个本地文档或图片。"""
    if not isinstance(config, ChatConfig):
        raise ChatError("config", "invalid_config", "config 必须是 load_config() 返回的对话配置。")
    if reply_to is not None:
        if not isinstance(reply_to, ChatResult):
            raise ChatError("input", "invalid_reply_to", "reply_to 必须是上一轮 ChatResult。")
        if conversation_id is not None and conversation_id != reply_to.conversation_id:
            raise ChatError("input", "conflicting_conversation", "会话 ID 与 reply_to 不一致。")
        conversation_id = reply_to.conversation_id
    contents = _answer_contents(question, reply_to)
    original_question = question if isinstance(question, str) else question.text
    if reply_to is not None and reply_to.needs_input and reply_to.original_question:
        # 本题经过补答后仍保留首问语境；新问题不会继承上一题的生成任务保护。
        original_question = reply_to.original_question
    if file is not None and image is not None:
        raise ChatError("input", "conflicting_attachment", "一轮只可选择文档或图片。")
    if conversation_id is not None and (
        not isinstance(conversation_id, str) or not _ID_PATTERN.fullmatch(conversation_id)
    ):
        raise ChatError("input", "invalid_conversation_id", "会话 ID 应为 32 至 64 个字母、数字、下划线或横杠。")
    if on_preview is not None and not callable(on_preview):
        raise ChatError("input", "invalid_callback", "on_preview 必须是可调用对象。")

    conversation_id = conversation_id or str(uuid4())
    request_id = str(uuid4())
    callback_error = None

    def notify(event: PreviewEvent) -> None:
        """标记调用方异常，避免写入组件自己的请求 ID。"""
        nonlocal callback_error
        try:
            on_preview(event)
        except ChatError as exc:
            callback_error = exc
            raise

    with diagnostic_logger(config) as logger:
        started = monotonic()
        input_kind = "clarification_answer" if reply_to is not None and reply_to.needs_input else "question"
        attachment_kind = "image" if image is not None else "file" if file is not None else "none"
        logger.info("request_started conversation_id=%s request_id=%s input_kind=%s attachment=%s structured=%s", conversation_id, request_id, input_kind, attachment_kind, config.structured_interaction)
        if logger.isEnabledFor(INFO):
            current_question = question if isinstance(question, str) else question.text
            logger.info("user_question conversation_id=%s request_id=%s question=%s", conversation_id, request_id, conversation_text(config, current_question))
        try:
            if file is not None or image is not None:
                from .attachments import prepare_attachment

                logger.debug("attachment_started conversation_id=%s request_id=%s kind=%s", conversation_id, request_id, attachment_kind)
                contents.append(prepare_attachment(image if image is not None else file, image is not None, config))
                logger.debug("attachment_completed conversation_id=%s request_id=%s kind=%s", conversation_id, request_id, attachment_kind)
            payload = _payload(config, request_id, conversation_id, contents)
            record = _completed_record(payload, config, notify if on_preview is not None else None, logger)
            result = _result(record, conversation_id, request_id, input_kind, original_question, config)
            if logger.isEnabledFor(INFO):
                logger.info("adp_answer conversation_id=%s request_id=%s answer_length=%d answer_truncated=%s answer_preview=%s", conversation_id, request_id, len(result.text), len(result.text) > config.log_answer_chars, conversation_text(config, result.text, limit=config.log_answer_chars))
            logger.info("request_completed conversation_id=%s request_id=%s record_id=%s reply_method=%s needs_input=%s clarification_source=%s business_status=%s elapsed_ms=%.1f", conversation_id, request_id, result.record_id, result.reply_method, result.needs_input, result.clarification_source, result.interaction.status if result.interaction else "unmarked", (monotonic() - started) * 1000)
            return result
        except ChatError as exc:
            if exc is not callback_error:
                exc.conversation_id = conversation_id
                exc.request_id = request_id
                logger.error("request_failed conversation_id=%s request_id=%s stage=%s code=%s http_status=%s provider_code=%s provider_request_id=%s provider_trace_id=%s elapsed_ms=%.1f", conversation_id, request_id, exc.stage, exc.code, exc.http_status, exc.provider_code, exc.provider_request_id, exc.provider_trace_id, (monotonic() - started) * 1000)
            else:
                logger.warning("callback_failed conversation_id=%s request_id=%s", conversation_id, request_id)
            raise
        except Exception:
            logger.warning("request_interrupted conversation_id=%s request_id=%s", conversation_id, request_id)
            raise


def chat_until_complete(
    question: str,
    *,
    config: ChatConfig,
    answer_provider: Callable[[ChatResult], str | ClarificationAnswer],
    on_result: Callable[[ChatResult], None] | None = None,
    file: str | Path | None = None,
    image: str | Path | None = None,
    conversation_id: str | None = None,
    on_preview: Callable[[PreviewEvent], None] | None = None,
) -> ChatResult:
    """按正式状态或保守文字推断收集补充答案；附件仅首轮上传。

    answer_provider 和 on_result 在当前线程执行，异常原样传播，不重试。
    调用方可在回调中实现界面输入、取消或限制追问次数。
    """
    if not callable(answer_provider) or (on_result is not None and not callable(on_result)):
        raise ChatError("input", "invalid_callback", "答案提供器和结果回调必须可调用。")
    result = chat(
        question, config=config, file=file, image=image, conversation_id=conversation_id,
        on_preview=on_preview,
    )
    with diagnostic_logger(config) as logger:
        while True:
            if on_result is not None:
                try:
                    on_result(result)
                except (Exception, KeyboardInterrupt):
                    logger.warning("result_callback_failed conversation_id=%s request_id=%s", result.conversation_id, result.request_id)
                    raise
            if not result.needs_input:
                return result
            logger.debug("answer_requested conversation_id=%s request_id=%s source=%s", result.conversation_id, result.request_id, result.clarification_source)
            try:
                answer = answer_provider(result)
            except (Exception, KeyboardInterrupt):
                logger.warning("answer_callback_failed conversation_id=%s request_id=%s", result.conversation_id, result.request_id)
                raise
            logger.debug("answer_received conversation_id=%s request_id=%s", result.conversation_id, result.request_id)
            result = chat(answer, config=config, reply_to=result, on_preview=on_preview)
