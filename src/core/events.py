"""网关事件v1：标准logging写本地JSONL，绝不作为消息恢复事实源。"""

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from threading import RLock
from uuid import uuid4

from .logging import _BEIJING, _mask_secrets


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


class EventFormatter(logging.Formatter):
    """每个事件只生成一次UUID；最终序列化仍按已知凭据脱敏。"""

    def __init__(self, config):
        """身份/密钥不进入LogRecord扩展字段或格式化异常。"""
        super().__init__()
        self.environment = config.gateway_environment
        self.secrets = (config.app_key, config.callback_token, config.encoding_aes_key, config.wecom_app_secret)

    def format(self, record):
        """只格式化受控gateway_event，不使用message/args/异常堆栈。"""
        event = record.gateway_event
        status, level, component = _EVENTS[event["event"]]
        output = {"schema_version": "adp_chat.log.v1", "id": event["id"],
            "timestamp": datetime.fromtimestamp(record.created, _BEIJING).isoformat(timespec="milliseconds"),
            "level": level, "service": "adp_chat", "environment": self.environment,
            "component": component, "event": event["event"], "status": status,
            "message_key": event.get("message_key"), "user_ref": None,
            "conversation_id": event.get("conversation_id"), "request_id": event.get("request_id"),
            "source_file": record.filename, "source_line": record.lineno,
            "attributes": event.get("attributes", {})}
        output.update({key: value for key, value in event.items()
                       if key in _ID_FIELDS | _NUMBER_FIELDS | {"outcome_uncertain"}})

        def redact(value):
            """在JSON编码前递归替换，凭据含引号或转义字符时也不破坏JSON。"""
            if isinstance(value, str):
                return _mask_secrets(value, self.secrets)
            if isinstance(value, dict):
                return {key: redact(item) for key, item in value.items()}
            return value

        return json.dumps(redact(output), ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class _EventHandler(logging.Handler):
    """单进程单网关的日期/大小分段；崩溃尾行留旧段，新实例永不续接旧尾。"""

    def __init__(self, config):
        """沿用现有日志目录/大小，不另建路径配置或修改root logger。"""
        super().__init__()
        import os

        self.directory = Path(config.log_file).absolute().parent
        self.max_bytes = config.log_max_bytes
        self.retention = config.events_retention_days
        self.prefix = f"{os.getpid()}-{uuid4().hex[:12]}"
        self.sequence = 0
        self.path = None
        self.day = None
        self.failed_writes = 0
        self._lock = RLock()

    def _purge(self, day):
        """只删除本Owner命名的过期分段，不触及未知文件或当前诊断目录。"""
        cutoff = datetime.strptime(day, "%Y%m%d").date() - timedelta(days=self.retention)
        for path in self.directory.iterdir():
            match = _SEGMENT.fullmatch(path.name)
            if match is not None and path.is_file():
                try:
                    expired = datetime.strptime(match[1], "%Y%m%d").date() < cutoff
                except ValueError:
                    continue
                if expired:
                    path.unlink()

    def _write(self, record, content):
        """每行单次有界追加并关闭；不重命名活跃段，不把日志fsync当业务提交。"""
        with self._lock:
            day = datetime.fromtimestamp(record.created, _BEIJING).strftime("%Y%m%d")
            self.directory.mkdir(parents=True, exist_ok=True)
            if self.day != day:
                self._purge(day)
            if (self.path is None or self.day != day
                    or (self.path.stat().st_size > 0 and self.path.stat().st_size + len(content) > self.max_bytes)):
                self.path = self.directory / f"events-{day}-{self.prefix}-{self.sequence}.jsonl"
                self.sequence += 1
                with self.path.open("xb"):
                    pass
                self.day = day
            with self.path.open("ab") as output:
                output.write(content)
                output.flush()

    def emit(self, record):
        """写失败以安全计数告警，不传播异常到ACK/ADP/发送或状态恢复。"""
        try:
            self._write(record, (self.format(record) + "\n").encode("utf-8"))
        except (OSError, ValueError, TypeError):
            self.failed_writes += 1
            # 失败可能已写入部分字节；下一次新开分段，绝不续接未知尾行。
            self.path = None
            try:
                logging.getLogger("adp_chat.event_alert").error("gateway_jsonl_write_failed count=%d", self.failed_writes)
            except Exception:
                # 宿主自定义告警Handler也可能失效；保留计数，绝不改变业务执行。
                pass


class GatewayEvents:
    """网关生命周期持有事件logger；所有业务恢复仍只读MessageStore。"""

    def __init__(self, config):
        """沿用日志总开关；使用独立logging.Logger避免修改宿主全局Handler。"""
        self.config = config
        self.logger = logging.Logger("adp_chat.gateway.events", level=getattr(logging, config.log_level))
        self.logger.propagate = False
        self.logger.disabled = not config.log_enabled
        self.handler = _EventHandler(config)
        self.handler.setFormatter(EventFormatter(config))
        self.logger.addHandler(self.handler)

    @property
    def failed_writes(self):
        """供本机排障读取的失败计数，日志本身不成为健康或业务事实源。"""
        return self.handler.failed_writes

    def emit(self, event, record=None, *, attributes=None, **fields):
        """只从明确调用点接收白名单元数据，未知事件/模拟接受直接拒绝记录。"""
        if event not in _EVENTS:
            return
        attributes = _attributes(attributes)
        attributes.setdefault("source", "test_fixture" if self.config.gateway_environment == "test" else "wecom_callback")
        if event == "wecom.send.accepted" and (self.config.gateway_environment != "production"
                or attributes.get("source") != "wecom_api"):
            return
        record = record if isinstance(record, dict) else {}
        data = {"event": event, "id": str(uuid4()), "attributes": attributes}
        for key in ("message_key", "conversation_id"):
            value = record.get(key)
            if isinstance(value, str) and _HASH.fullmatch(value):
                data[key] = value
        data["request_id"] = _identifier(record.get("request_id"))
        for key, value in fields.items():
            if key in _ID_FIELDS:
                data[key] = _identifier(value)
            elif key in _NUMBER_FIELDS and type(value) in (int, float) and 0 <= value < 1e15:
                data[key] = value
            elif key == "outcome_uncertain" and type(value) is bool:
                data[key] = value
        self.logger.log(getattr(logging, _EVENTS[event][1]), "", extra={"gateway_event": data}, stacklevel=2)

    def close(self):
        """仅释放日志资源，关闭不会更改任何消息或重发决定。"""
        self.logger.removeHandler(self.handler)
        self.handler.close()
