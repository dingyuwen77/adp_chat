"""网关事件 v1：与普通诊断共用一个 .log；消息快照仍是恢复事实源。"""

import logging
import re
from uuid import uuid4



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
    """网关业务事件以中文说明与结构化键值字段写入同一普通日志。"""

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
        data = {
            "event_id": str(uuid4()),
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
            "attributes": attributes,
        }
        for key, value in fields.items():
            if key in _ID_FIELDS:
                data[key] = _identifier(value)
            elif key in _NUMBER_FIELDS and type(value) in (int, float) and 0 <= value < 1e15:
                data[key] = value
            elif key == "outcome_uncertain" and type(value) is bool:
                data[key] = value
        # 保留事件白名单与所有合法属性；扁平键值便于人类排障和未来逐行解析。
        from .logging import _mask_secrets
        secrets = (self.config.app_key, self.config.callback_token,
                   self.config.encoding_aes_key, self.config.wecom_app_secret)
        fields_out = {}
        for key, value in data.items():
            if key == "attributes":
                for attr_key, attr_value in value.items():
                    fields_out["attr_" + attr_key] = attr_value
            elif value is not None and key != "user_ref":
                fields_out[key] = _mask_secrets(value, secrets) if isinstance(value, str) else value
        descriptions = {
            "service.started": "服务启动完成", "service.stopped": "服务已停止",
            "wecom.callback.rejected": "企业微信回调认证被拒绝",
            "wecom.message.received": "收到企业微信消息",
            "wecom.message.persisted": "企业微信消息已可靠保存",
            "wecom.message.duplicate": "收到重复消息，已跳过处理",
            "wecom.message.busy_rejected": "当前用户有进行中的请求，本次消息已拒绝",
            "adp.submit_armed": "ADP请求已进入发送准备状态",
            "adp.started": "开始调用ADP服务",
            "adp.completed": "ADP请求已完成",
            "adp.rejected": "ADP明确拒绝本次请求",
            "adp.failed": "ADP请求失败",
            "adp.unknown": "ADP执行结果不确定，禁止自动重试",
            "message.session.released": "会话处理占用已释放",
            "reply.persisted": "回复已持久化",
            "wecom.send.started": "开始发送企业微信通知",
            "wecom.send.accepted": "企业微信接口已接受发送请求",
            "wecom.send.simulated": "企业微信通知已模拟发送",
            "wecom.send.failed": "企业微信通知发送失败",
            "wecom.send.unknown": "企业微信通知发送结果不确定",
            "message.recovered": "重启恢复时处理未完成消息",
            "message.cleanup.completed": "消息清理完成",
            "message.cleanup.failed": "消息清理失败",
        }
        if "duration_ms" in fields_out:
            fields_out["elapsed_ms"] = fields_out.pop("duration_ms")
        self.logger.log(getattr(logging, _EVENTS[event][1]),
                        descriptions[event], extra={"log_fields": fields_out}, stacklevel=2)

    def close(self):
        """共享 logger 由 lifespan 关闭；独立测试 logger 由自身释放。"""
        if self._owned_logger is not None:
            self._owned_logger.__exit__(None, None, None)
            self._owned_logger = None
