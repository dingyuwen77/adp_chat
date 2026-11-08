"""独立文件诊断：不安装控制台 Handler，不更改宿主 root logger。"""

import logging
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from .config import ChatConfig
from .models import ChatError


_FILE_LOCK = RLock()
_BEIJING = timezone(timedelta(hours=8))
# 先读取完整候选；合法内部标点不截断，同段中的下一个URL另行处理。
_URL = re.compile(r"https?://(?:(?!https?://)[^\s<>\"])+", re.I)
_PRIVATE_QUERY = re.compile(
    r"(?:^|[?&#;])(?:q-sign[^=]*|q-key-time|q-ak|x-cos-security-token|x-amz-[^=]*|x-goog-[^=]*|"
    r"signature|sig|token|access_token|authorization|appkey|api[_-]?key|secret[_-]?key|credential)=", re.I,
)


class CallbackAccessFilter(logging.Filter):
    """启用回调时移除 Uvicorn 访问日志的查询串，避免记录签名与密文。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """只处理 Uvicorn 的稳定五参数记录，不更改请求或业务日志。"""
        if isinstance(record.args, tuple) and len(record.args) == 5 and isinstance(record.args[2], str):
            arguments = list(record.args)
            arguments[2] = arguments[2].partition("?")[0]
            record.args = tuple(arguments)
        return True


class HTTPQueryFilter(logging.Filter):
    """发送适配器存活时屏蔽HTTPX日志中的完整查询，Token/Secret不进入日志。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """在logger分发到任意Handler之前处理插值后的URL，不改HTTP请求。"""
        def clean(match):
            """GET查询中的corpsecret和access_token一律不展示。"""
            value = match.group()
            return value.partition("?")[0] + ("?[REDACTED]" if "?" in value else "")

        record.msg = _URL.sub(clean, record.getMessage())
        record.args = ()
        return True


def _prose_suffix(value: str) -> str:
    """保留URI凭据字段之后的中文语句；不解码值，避免把编码的敏感数据当正文。"""
    match = re.search(r"[，。！？；：、]", value)
    if match is None or not value[:match.start()].isascii():
        return ""
    suffix = value[match.start():]
    if _PRIVATE_QUERY.search(unquote_plus(suffix.replace("&amp;", "&"))):
        return ""
    return suffix


def _mask_url_parameters(component: str) -> str:
    """逐字段隐藏敏感值及包含嵌套凭据的值，保留非敏感参数与链接后正文。"""
    parts = []
    for part in component.split("&"):
        key, separator, value = part.partition("=")
        decoded = unquote_plus(part.replace("&amp;", "&"))
        if _PRIVATE_QUERY.search(decoded):
            prefix = key + "=" if separator else ""
            parts.append(prefix + "[REDACTED]" + _prose_suffix(value if separator else part))
        else:
            parts.append(part)
    return "&".join(parts)


def _mask_secrets(text: str, secrets: tuple[str, ...]) -> str:
    """先替换较长凭据，避免一个凭据恰好是另一凭据的前缀。"""
    for secret in sorted({value for value in secrets if value}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    return text


def _mask_private_url(match: re.Match) -> str:
    """移除签名/令牌查询参数与URL用户凭据，保留非敏感定位部分。"""
    try:
        parts = urlsplit(match.group())
        netloc = parts.netloc
        if "@" in netloc:
            netloc = "[REDACTED]@" + netloc.rsplit("@", 1)[1]
        query = _mask_url_parameters(parts.query)
        fragment = _mask_url_parameters(parts.fragment)
        if (netloc, query, fragment) == (parts.netloc, parts.query, parts.fragment):
            return match.group()
        return urlunsplit((parts.scheme, netloc, parts.path, query, fragment))
    except ValueError:
        return "[REDACTED_URL]"


def conversation_text(config: ChatConfig, text: str, *, limit: int | None = None) -> str:
    """完整脱敏后按Unicode字符截取，再转成单行JSON字符串，防止日志行伪造。"""
    text = _mask_secrets(text, (config.app_key, config.callback_token, config.encoding_aes_key, config.wecom_app_secret))
    text = _URL.sub(_mask_private_url, text)
    if limit is not None:
        text = text[:limit]
    encoded = json.dumps(text, ensure_ascii=False)
    for separator in ("\u0085", "\u2028", "\u2029"):
        encoded = encoded.replace(separator, f"\\u{ord(separator):04x}")
    return encoded


# 固定前缀保持不变。仅正文使用一个「 | 」分隔说明与键值字段。
_PREFIX = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) ([^\s\]]+) L(\d+)\] \[([A-Z]+)\] (.*)$")
_PAIR = re.compile(r'([a-z][a-z0-9_]*)=("(?:\\.|[^"\\])*"|[^\s]+)')
_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_NUMERIC_FIELDS = {"elapsed_ms", "duration_ms", "http_status", "attempt", "reply_chars",
                   "reply_method", "queue_depth", "removed", "accepted_parts", "total_parts"}
_BOOL_FIELDS = {"outcome_uncertain", "duplicate", "stream", "answer_truncated", "local_session_released"}
# 中文可自由随代码变化；这里仅给既有英文诊断事件提供有意义的默认说明。
_DESCRIPTIONS = {
    "request_started": "开始调用ADP服务",
    "request_completed": "ADP请求完成",
    "request_failed": "ADP请求失败",
    "request_interrupted": "ADP请求被中断",
    "user_question": "记录用户问题",
    "adp_answer": "记录ADP回复预览",
    "sse_completed": "ADP SSE响应完成",
    "wecom_callback_received": "收到企业微信回调并完成接收处理",
    "adp_recovered_unknown": "恢复时发现ADP执行结果不确定",
    "gateway_dispatch_failed": "网关调度失败",
    "adp_result_saved": "ADP处理结果已持久化",
    "wecom_send_result": "企业微信通知处理结果已保存",
    "wecom_send_armed": "企业微信通知已进入发送准备状态",
    "notification_state_failed": "企业微信通知状态持久化失败",
    "adp_send_armed": "ADP请求已进入发送准备状态",
    "gateway_state_failed": "网关状态持久化失败",
}
# 仅解析具有明确语法的 key=value 字段，不从自由中文正文推断字段。
def _split_legacy_message(message: str) -> tuple[str, dict]:
    head, sep, rest = message.partition(" ")
    if not sep or not re.fullmatch(r"[a-z][a-z0-9_]*", head):
        return message, {}
    fields = {}
    offset = 0
    for match in _PAIR.finditer(rest):
        if rest[offset:match.start()].strip():
            # 不猜测混杂的自由文本，保留全部正文。
            return message, {}
        key, raw = match.groups()
        if key in fields:
            return message, {}
        if raw.startswith('"'):
            try:
                value = json.loads(raw)
            except ValueError:
                return message, {}
        else:
            value = raw
        fields[key] = value
        offset = match.end()
    if not fields or rest[offset:].strip():
        return message, {}
    return _DESCRIPTIONS.get(head, head), fields


def _encode_field(value) -> str:
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (int, float):
        return str(value)
    if not isinstance(value, str):
        value = str(value)
    if not value or any(c.isspace() or c in '"\\|=' for c in value):
        return json.dumps(value, ensure_ascii=False)
    return value


def _decode_field(key: str, raw: str):
    if raw.startswith('"'):
        value = json.loads(raw)
        if not isinstance(value, str):
            raise ValueError("日志字段字符串格式无效")
        return value
    if key in _BOOL_FIELDS and raw in ("true", "false", "True", "False"):
        return raw.lower() == "true"
    if key in _NUMERIC_FIELDS:
        try:
            return float(raw) if "." in raw else int(raw)
        except ValueError:
            return raw
    return raw


class _Formatter(logging.Formatter):
    """保留原有前缀；仅正文为「自由中文说明 | key=value ...」。"""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self._secrets = tuple(value for value in secrets if value)

    def formatTime(self, record: logging.LogRecord, datefmt=None) -> str:
        moment = datetime.fromtimestamp(record.created, _BEIJING)
        return moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{moment.microsecond // 1000:03d}"

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        explicit = getattr(record, "log_fields", None)
        if isinstance(explicit, dict):
            description, fields = message, explicit
        else:
            description, fields = _split_legacy_message(message)
        # 已经由 conversation_text 脱敏的问答字段不再二次修改 JSON 字符串。
        description = _mask_secrets(_URL.sub(_mask_private_url, description), self._secrets)
        description = description.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
        description = description.replace("\u0085", "\\u0085").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        description = description.replace(" | ", " \\u007c ")
        safe_fields = {}
        for key, value in fields.items():
            if not _FIELD_NAME.fullmatch(key) or value is None:
                continue
            if isinstance(value, str):
                value = _mask_secrets(value, self._secrets)
                # 旧问答字段已由 conversation_text 清洗，避免重复 URL 脱敏。
                if key not in ("question", "answer_preview"):
                    value = _URL.sub(_mask_private_url, value)
            safe_fields[key] = value
        suffix = (" | " + " ".join(f"{key}={_encode_field(value)}"
                                    for key, value in safe_fields.items())) if safe_fields else ""
        return f"[{self.formatTime(record)} {record.filename} L{record.lineno}] [{record.levelname}] {description}{suffix}"


def parse_log_line(line: str) -> dict:
    """将新格式的单行可读日志转为字典；拒绝旧版 record/gateway_event JSON。"""
    match = _PREFIX.fullmatch(line.rstrip("\r\n"))
    if match is None:
        raise ValueError("日志前缀无效")
    timestamp, filename, lineno, level, body = match.groups()
    if " | record=" in body or body.startswith("gateway_event "):
        raise ValueError("旧版日志不属于方案 B")
    if " | " in body:
        description, fields_text = body.split(" | ", 1)
    else:
        description, fields_text = body, ""
    fields = {}
    offset = 0
    for field in _PAIR.finditer(fields_text):
        if fields_text[offset:field.start()].strip():
            raise ValueError("日志字段语法无效")
        key, raw = field.groups()
        if key in fields:
            raise ValueError("日志字段重复")
        fields[key] = _decode_field(key, raw)
        offset = field.end()
    if fields_text[offset:].strip() or (fields_text and not fields):
        raise ValueError("日志字段格式无效")
    # 中文说明中的分隔符以 \u007c 表示，不会被误判为字段区。
    description = description.replace("\\u007c", "|").replace("\\n", "\n").replace("\\r", "\r")
    return {
        "timestamp": datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=_BEIJING).isoformat(timespec="milliseconds"),
        "source_file": filename,
        "source_line": int(lineno),
        "level": level,
        "message": description,
        **fields,
    }


class _FileHandler(RotatingFileHandler):
    """进程内串行轮转，每条记录后关闭文件，兼容 Windows 并发请求。"""

    failed_writes = 0

    def emit(self, record: logging.LogRecord) -> None:
        """写入与轮转共用锁，避免其他请求占用待重命名的日志文件。"""
        with _FILE_LOCK:
            try:
                super().emit(record)
            finally:
                if self.stream is not None:
                    try:
                        self.stream.close()
                    except OSError:
                        pass
                    self.stream = None

    def handleError(self, record: logging.LogRecord) -> None:
        """运行中日志写入失败不转写控制台，也不打断已发出的业务请求。"""
        # 业务调用不因日志写失败而重试；仅增加计数并发出安全告警。
        self.failed_writes += 1
        try:
            logging.getLogger("adp_chat.event_alert").error("diagnostic_log_write_failed count=%d", self.failed_writes)
        except Exception:
            pass
        return None


@contextmanager
def diagnostic_logger(config: ChatConfig):
    """为一次操作创建独立文件 logger，结束即释放，不修改全局日志设置。"""
    logger = logging.Logger("adp_chat", level=getattr(logging, config.log_level))
    logger.propagate = False
    logger.disabled = not config.log_enabled
    if not config.log_enabled:
        yield logger
        return
    try:
        path = Path(config.log_file).absolute()
        with _FILE_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8"):
                pass
        handler = _FileHandler(
            path, maxBytes=config.log_max_bytes, backupCount=config.log_backup_count,
            encoding="utf-8", delay=True,
        )
    except (OSError, ValueError):
        raise ChatError("logging", "unwritable", "无法创建日志文件，请检查 [logging] 的路径和权限。") from None
    handler.setFormatter(_Formatter((config.app_key, config.callback_token, config.encoding_aes_key, config.wecom_app_secret)))
    logger.addHandler(handler)
    try:
        yield logger
    finally:
        handler.close()
        logger.removeHandler(handler)
