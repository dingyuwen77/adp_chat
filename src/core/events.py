"""网关事件 v1：与普通诊断共用一个 .log；消息快照仍是恢复事实源。"""

import json
import logging
import re
from datetime import datetime
from uuid import uuid4

from .logging import _BEIJING


_EVENTS = {
    "service.started": ("success", "INFO", "service"),
    "service.stopped": ("success", "INFO", "service"),
    "wecom.callback.rejected": ("rejected", "WARNING", "wecom_callback"),
    "wecom.message.received": ("start", "INFO", "wecom_callback"),
    "wecom.message.persisted": ("success", "INFO", "message_store"),
    "wecom.message.duplicate": ("duplicate", "INFO", "wecom_callback"),
    "wecom.message.busy_rejected": ("rejected", "INFO", "wecom_callback"),
    "adp.submit_armed": ("success", "INFO", "adp_client"),
    "adp.started": ("start", "INFO", "adp_client"),
    "adp.completed": ("success", "INFO", "adp_client"),
    "adp.rejected": ("rejected", "WARNING", "adp_client"),
    "adp.failed": ("failed", "ERROR", "adp_client"),
    "adp.unknown": ("unknown", "WARNING", "adp_client"),
    "message.session.released": ("success", "INFO", "message_store"),
    "reply.persisted": ("success", "INFO", "message_store"),
    "wecom.send.started": ("start", "INFO", "wecom_sender"),
    "wecom.send.accepted": ("success", "INFO", "wecom_sender"),
    "wecom.send.simulated": ("success", "INFO", "wecom_sender"),
    "wecom.send.failed": ("failed", "WARNING", "wecom_sender"),
    "wecom.send.unknown": ("unknown", "WARNING", "wecom_sender"),
    "message.recovered": ("success", "WARNING", "message_store"),
    "message.cleanup.completed": ("success", "INFO", "message_store"),
    "message.cleanup.failed": ("failed", "WARNING", "message_store"),
}
_IDENTIFIER = re.compile(r"[A-Za-z0-9_\-:.]{1,128}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_SEGMENT = re.compile(r"events-([0-9]{8})-([0-9]+)-([a-f0-9]{12})-([0-9]+)\.jsonl\Z")
_NUMBER_FIELDS = {"attempt", "duration_ms", "http_status"}
_ID_FIELDS = {"provider_request_id", "provider_trace_id", "record_id", "error_code", "error_stage"}
_COUNTS = {"reply_chars", "reply_method", "queue_depth", "removed", "accepted_parts", "total_parts"}
_OPTIONS = {
    "source": {"test_fixture", "wecom_callback", "simulated", "fake_http", "wecom_api", "recovery"},
    "admission_decision": {"ACCEPTED", "USER_BUSY"},
    "release_reason": {"ADP_SUCCEEDED", "ADP_REJECTED", "ADP_UNKNOWN"},
    "event_type": {"adp", "notification"},
    "recovery_action": {"first_submission", "first_notification", "mark_unknown"},
}


def _identifier(value):
    """只允许有界厂商标识字符，禁止原始异常、URL和问答进入诊断字段。"""
    if type(value) in (str, int) and _IDENTIFIER.fullmatch(str(value)):
        return str(value)
    return None


def _attributes(values):
    """属性按字段及值双白名单收敛，未知类型/负数/任意文本直接丢弃。"""
    output = {}
    if not isinstance(values, dict):
        return output
    for key, value in values.items():
        if key in _COUNTS and type(value) is int and 0 <= value <= 1_000_000_000:
            output[key] = value
        elif key in _OPTIONS and isinstance(value, str) and value in _OPTIONS[key]:
            output[key] = value
    return output


class GatewayEvents:
    """统一业务事件：写入现有普通诊断 .log，不再创建独立 JSONL。

    gateway_event 后的单行 JSON 是稳定机器字段，前面的日志前缀保持人工可读；
    后续 Cosmos 采集可提取该字段而无需解析自由格式的诊断正文。
    """

    def __init__(self, config, logger=None):
        self.config = config
        self._owned_logger = None
        if logger is None:
            from .logging import diagnostic_logger
            self._owned_logger = diagnostic_logger(config)
            logger = self._owned_logger.__enter__()
        self.logger = logger

    @property
    def failed_writes(self):
        """诊断日志 Handler 的写失败次数；不能作为业务提交事实。"""
        return sum(getattr(handler, "failed_writes", 0) for handler in self.logger.handlers)

    def emit(self, event, record=None, *, attributes=None, **fields):
        """单次业务事件、白名单字段和稳定 UUID；不记录正文/原始异常。"""
        if event not in _EVENTS or not self.config.log_enabled:
            return
        attributes = _attributes(attributes)
        attributes.setdefault("source", "test_fixture" if self.config.gateway_environment == "test" else "wecom_callback")
        if event == "wecom.send.accepted" and (self.config.gateway_environment != "production"
                or attributes.get("source") != "wecom_api"):
            return
        record = record if isinstance(record, dict) else {}
        from inspect import currentframe
        caller = currentframe().f_back
        data = {
            "schema_version": "adp_chat.log.v1",
            "id": str(uuid4()),
            "timestamp": datetime.now(_BEIJING).isoformat(timespec="milliseconds"),
            "level": _EVENTS[event][1],
            "service": "adp_chat",
            "environment": self.config.gateway_environment,
            "component": _EVENTS[event][2],
            "event": event,
            "status": _EVENTS[event][0],
            "message_key": record.get("message_key") if isinstance(record.get("message_key"), str)
                and _HASH.fullmatch(record["message_key"]) else None,
            "user_ref": None,
            "conversation_id": record.get("conversation_id") if isinstance(record.get("conversation_id"), str)
                and _HASH.fullmatch(record["conversation_id"]) else None,
            "request_id": _identifier(record.get("request_id")),
            "source_file": caller.f_code.co_filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1],
            "source_line": caller.f_lineno,
            "attributes": attributes,
        }
        del caller
        for key, value in fields.items():
            if key in _ID_FIELDS:
                data[key] = _identifier(value)
            elif key in _NUMBER_FIELDS and type(value) in (int, float) and 0 <= value < 1e15:
                data[key] = value
            elif key == "outcome_uncertain" and type(value) is bool:
                data[key] = value
        # 白名单之后再次脱敏；保留机器字段可供未来采集，避免解析自由文本。
        from .logging import _mask_secrets
        secrets = (self.config.app_key, self.config.callback_token,
                   self.config.encoding_aes_key, self.config.wecom_app_secret)
        def redact(value):
            if isinstance(value, str):
                return _mask_secrets(value, secrets)
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items()}
            return value
        payload = json.dumps(redact(data), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        self.logger.log(getattr(logging, _EVENTS[event][1]), "gateway_event %s", payload, stacklevel=2)

    def close(self):
        """共享 logger 由 lifespan 关闭；独立测试 logger 由自身释放。"""
        if self._owned_logger is not None:
            self._owned_logger.__exit__(None, None, None)
            self._owned_logger = None
