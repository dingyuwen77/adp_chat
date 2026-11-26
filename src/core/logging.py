"""独立文件诊断：不安装控制台 Handler，不更改宿主 root logger。"""

import logging
import json
import os
import re
import shutil
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from functools import partial
from logging.handlers import QueueHandler, RotatingFileHandler
from pathlib import Path
from queue import Full, Queue
from sys import getsizeof
from threading import Lock, RLock, Thread
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from .config import ChatConfig
from .models import ChatError


_FILE_LOCK = RLock()
_ACTIVE_WRITERS = {}
_QUEUE_MAX_RECORDS = 16_384
_QUEUE_MAX_BYTES = 64 * 1024 * 1024
_BEIJING = timezone(timedelta(hours=8))
# 先读取完整候选；合法内部标点不截断，同段中的下一个URL另行处理。
_URL = re.compile(r"https?://(?:(?!https?://)[^\s<>\"])+", re.I)
_JSON_STRING = re.compile(r'"(?:[^"\\\x00-\x1f]++|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"')
_PRIVATE_QUERY = re.compile(
    r"(?:^|[?&#;])(?:q-sign[^=]*|q-key-time|q-ak|x-cos-security-token|x-amz-[^=]*|x-goog-[^=]*|"
    r"(?:msg_)?signature|sig|token|access_token|authorization|appkey|corpsecret|encodingaeskey|"
    r"api[_-]?key|secret[_-]?key|credential)=", re.I,
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


def _mask_text(text, secrets, employee=None):
    """清洗文字和 JSON 字符串中的凭据，未脱敏的片段保持原样。"""
    def scrub(value):
        """在解码后的文字中识别 URL、配置凭据及当前员工账号。"""
        value = _URL.sub(_mask_private_url, _mask_secrets(value, secrets))
        if employee:
            value = re.sub(rf"(?<![A-Za-z0-9_@.-]){re.escape(employee)}(?![A-Za-z0-9_@.-])",
                           "[REDACTED]", value, flags=re.IGNORECASE)
        return value

    if "\\" not in text or '"' not in text:
        return scrub(text)
    parts = []
    start = 0
    for match in _JSON_STRING.finditer(text):
        # JSON 字符串和外部正文各处理一次，避免转义后的句号被误当成 URL 的一部分。
        parts.append(scrub(text[start:match.start()]))
        token = match.group()
        decoded = json.loads(token)
        safe = scrub(decoded)
        if safe != decoded:
            token = json.dumps(safe, ensure_ascii=False).encode("utf-8", errors="backslashreplace").decode()
        parts.append(token)
        start = match.end()
    parts.append(scrub(text[start:]))
    return "".join(parts)


def log_conversation(logger, field: str, text: str, *, employee_id=None, **fields) -> None:
    """统一排入完整问答，调用点和关联字段保留，员工 ID 只用于脱敏。"""
    logger.info("记录用户问题" if field == "question" else "记录 ADP 回答",
                extra={"log_fields": {**fields, field: text}, "log_employee": employee_id},
                stacklevel=2)


def log_adp_answer(logger, result, *, employee_id=None, **fields) -> None:
    """记录完整文字及非文本类型，不把媒体负载复制到日志。"""
    if not logger.isEnabledFor(logging.INFO):
        return
    parts = [part.get("Type") for message in result.reply_contents for part in message
             if part.get("Type") not in ("text", "json_text")]
    kinds = {kind if kind in ("image", "audio", "video", "file", "card") else "other" for kind in parts}
    logger.info("记录 ADP 回答", extra={"log_fields": {
        **fields, "answer": result.text, "reply_method": result.reply_method,
        "media_parts": len(parts), "media_types": ",".join(sorted(kinds)) or None,
    }, "log_employee": employee_id}, stacklevel=2)


# 固定前缀保持不变。仅正文使用一个「 | 」分隔说明与键值字段。
_PREFIX = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}) ([^\s\]]+) L(\d+)\] \[([A-Z]+)\] (.*)$")
_PAIR = re.compile(r'([a-z][a-z0-9_]*)=("(?:\\.|[^"\\]++)*"|[^\s]+)')
_FIELD_NAME = re.compile(r"[a-z][a-z0-9_]*\Z")
_TEXT_FIELDS = {"question", "answer", "answer_preview"}
_NUMERIC_FIELDS = {"elapsed_ms", "duration_ms", "http_status", "attempt", "reply_chars",
                   "reply_method", "queue_depth", "removed", "accepted_parts", "total_parts", "image_index",
                   "provider_code", "media_parts"}
_BOOL_FIELDS = {"outcome_uncertain", "duplicate", "stream", "answer_truncated", "local_session_released", "image_fallback"}
# 中文可自由随代码变化；这里仅给既有英文诊断事件提供有意义的默认说明。
_DESCRIPTIONS = {
    "request_started": "开始调用ADP服务",
    "request_completed": "ADP请求完成",
    "request_failed": "ADP请求失败",
    "request_interrupted": "ADP请求被中断",
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


def _encode_field(value, key: str = "") -> str:
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) in (int, float):
        return str(value)
    if not isinstance(value, str):
        value = str(value)
    if key in _TEXT_FIELDS or not value or any(c.isspace() or c in '"\\|=' for c in value):
        return json.dumps(value, ensure_ascii=False).replace("\u0085", "\\u0085").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    return value


def _decode_field(key: str, raw: str):
    if raw.startswith('"'):
        value = json.loads(raw)
        if not isinstance(value, str):
            raise ValueError("日志字段字符串格式无效")
        return value
    if key in _BOOL_FIELDS and raw in ("true", "false", "True", "False"):
        return raw.lower() == "true"
    if key in _NUMERIC_FIELDS or (key.startswith("attr_") and key[5:] in {"reply_chars", "reply_method", "queue_depth", "removed", "accepted_parts", "total_parts"}):
        try:
            return float(raw) if "." in raw else int(raw)
        except ValueError:
            return raw
    return raw


class _Formatter(logging.Formatter):
    """保留原有前缀；仅正文为「自由中文说明 | key=value ...」。"""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        """独立同步 logger 的默认凭据，后台 writer 使用记录附带的配置。"""
        super().__init__()
        self._secrets = tuple(value for value in secrets if value)

    def formatTime(self, record: logging.LogRecord, datefmt=None) -> str:
        """以北京时间和固定三位毫秒提供稳定的本地前缀。"""
        moment = datetime.fromtimestamp(record.created, _BEIJING)
        return moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{moment.microsecond // 1000:03d}"

    def format(self, record: logging.LogRecord) -> str:
        """在输出线程清洗凭据、员工标识和换行，正文保留完整字段。"""
        message = record.getMessage()
        explicit = getattr(record, "log_fields", None)
        if isinstance(explicit, dict):
            description, fields = message, explicit
        else:
            description, fields = _split_legacy_message(message)
        secrets = getattr(record, "log_secrets", self._secrets)
        employee = getattr(record, "log_employee", None)
        description = _mask_secrets(_URL.sub(_mask_private_url, description), secrets)
        description = description.replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")
        description = description.replace("\u0085", "\\u0085").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        description = description.replace(" | ", " \\u007c ")
        safe_fields = {}
        for key, value in fields.items():
            if not _FIELD_NAME.fullmatch(key) or value is None:
                continue
            if isinstance(value, str):
                value = _mask_text(value, secrets, employee if key in _TEXT_FIELDS else None)
            if value not in ("None", "null") or key in _TEXT_FIELDS:
                safe_fields[key] = value
        suffix = (" | " + " ".join(f"{key}={_encode_field(value, key)}"
                                    for key, value in safe_fields.items())) if safe_fields else ""
        return f"[{self.formatTime(record)} {record.filename} L{record.lineno}] [{record.levelname}] {description}{suffix}"


def parse_log_line(line: str) -> dict:
    """将新格式的单行可读日志转为字典；拒绝旧版 record/gateway_event JSON。"""
    match = _PREFIX.fullmatch(line.rstrip("\r\n"))
    if match is None:
        raise ValueError("日志前缀无效")
    timestamp, filename, lineno, level, body = match.groups()
    if body.startswith("gateway_event "):
        raise ValueError("旧版日志不属于方案 B")
    if " | " in body:
        description, fields_text = body.split(" | ", 1)
    else:
        description, fields_text = body, ""
    if fields_text.startswith("record="):
        raise ValueError("旧版日志不属于方案 B")
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
    escapes = {"\\": "\\", "n": "\n", "r": "\r", "u007c": "|",
               "u0085": "\u0085", "u2028": "\u2028", "u2029": "\u2029"}
    description = re.sub(r"\\(u007c|u0085|u2028|u2029|n|r|\\)",
                         lambda match: escapes[match.group(1)], description)
    return {
        "timestamp": datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=_BEIJING).isoformat(timespec="milliseconds"),
        "source_file": filename,
        "source_line": int(lineno),
        "level": level,
        "message": description,
        **fields,
    }


class _FileHandler(RotatingFileHandler):
    """进程内串行轮转。后台唯一 writer 在连续记录之间保持文件打开。"""

    failed_writes = 0
    keep_open = False

    def _open(self):
        """完整问答日志的新文件只授予所属用户读写权限。"""
        return open(self.baseFilename, self.mode, encoding=self.encoding, errors=self.errors,
                    opener=_private_log_open)

    def emit(self, record: logging.LogRecord) -> None:
        """写入与轮转共用锁，避免其他请求占用待重命名的日志文件。"""
        with _FILE_LOCK:
            try:
                super().emit(record)
            finally:
                if not self.keep_open and self.stream is not None:
                    try:
                        self.stream.close()
                    except OSError:
                        pass
                    self.stream = None

    def handleError(self, record: logging.LogRecord) -> None:
        """运行中日志写入失败不转写控制台，也不打断已发出的业务请求。"""
        # 业务调用不因日志写失败而重试；仅增加计数并发出安全告警。
        self.failed_writes += 1
        _alert("diagnostic_log_write_failed", self.failed_writes)


def _private_log_open(path, flags):
    """新增与升级沿用的活动日志都以私有权限保存，轮转继承文件权限。"""
    fd = os.open(path, flags, 0o600)
    if os.name != "nt":
        try:
            if os.fstat(fd).st_mode & 0o077:
                os.fchmod(fd, 0o600)
        except OSError:
            os.close(fd)
            raise
    return fd


def _alert(code, count):
    """大面积写故障按二次幂报警，避免用诊断日志故障放大主链路压力。"""
    if count & (count - 1):
        return
    try:
        logging.getLogger("adp_chat.event_alert").error("%s count=%d", code, count)
    except Exception:
        # 宿主日志失效时，计数仍可从 writer 读回。
        pass


class _BufferedHandler(QueueHandler):
    """有界记录数与正文内存，用一条后台线程执行脱敏、写入和轮转。"""

    def __init__(self, sink):
        """初始化文件 writer 及其所有统计，线程仅在网关启用时创建。"""
        super().__init__(Queue(maxsize=_QUEUE_MAX_RECORDS))
        self.sink = sink
        self._lock = Lock()
        self._accepting = True
        self._pending_bytes = 0
        self._peak = 0
        self._dropped = 0
        self._thread = Thread(target=self._run, name="gateway-log-writer", daemon=True)
        self._thread.start()

    @property
    def failed_writes(self):
        """与网关事件现有计数兼容，专门报告文件写入失败。"""
        return self.sink.failed_writes

    def prepare(self, record):
        """不在业务协程格式化或复制文本，LogRecord 只由后台 writer 消费。"""
        return record

    def enqueue(self, record):
        """写满就记录丢弃次数，ACK 和 ADP 无需等待日志磁盘。"""
        fields = getattr(record, "log_fields", {})
        size = 512 + getsizeof(record.msg) + sum(getsizeof(value) for value in fields.values())
        with self._lock:
            try:
                if not self._accepting or self._pending_bytes + size > _QUEUE_MAX_BYTES:
                    raise Full
                self.queue.put_nowait((record, size))
            except Full:
                self._dropped += 1
                dropped = self._dropped
            else:
                self._pending_bytes += size
                self._peak = max(self._peak, self.queue.qsize())
                return
        _alert("diagnostic_log_queue_full", dropped)

    def _run(self):
        """逐行写出。队列和内存计数在失败时一样释放。"""
        while item := self.queue.get():
            record, size = item
            try:
                self.sink.handle(record)
            except Exception:
                self.sink.handleError(record)
            finally:
                with self._lock:
                    self._pending_bytes -= size

    def metrics(self) -> dict:
        """汇总积压、高水位、写失败和可用磁盘空间。"""
        with self._lock:
            stats = dict(log_queue_depth=self.queue.qsize(), log_queue_peak=self._peak,
                         log_queue_bytes=self._pending_bytes, log_dropped=self._dropped,
                         log_write_failures=self.failed_writes)
        try:
            stats["log_disk_free_bytes"] = shutil.disk_usage(Path(self.sink.baseFilename).parent).free
        except OSError:
            stats["log_disk_free_bytes"] = None
        return stats

    def drain(self):
        """生命周期结束后先关闭入队，再等待已接受的日志全部写出。"""
        with self._lock:
            self._accepting = False
        self.queue.put(None)
        self._thread.join()
        self.sink.close()
        super().close()


def _attach_secrets(secrets, record):
    """共享文件 writer 时每条记录仍使用其原始调用配置脱敏。"""
    record.log_secrets = secrets
    return True


def log_writer_metrics(logger) -> dict:
    """读取后台 writer 的安全计数，禁用日志或独立同步入口返回空统计。"""
    for handler in logger.handlers:
        if isinstance(handler, _BufferedHandler):
            return handler.metrics()
    return {}


@contextmanager
def diagnostic_logger(config: ChatConfig, *, background=False):
    """网关持有长生命周期后台 writer，进程内同路径的同步入口共用该文件所有者。"""
    logger = logging.Logger("adp_chat", level=getattr(logging, config.log_level))
    logger.propagate = False
    logger.disabled = not config.log_enabled
    if not config.log_enabled:
        yield logger
        return
    secret_values = (config.app_key, config.callback_token, config.encoding_aes_key, config.wecom_app_secret,
                     config.diagnostic_token, config.diagnostic_adp_app_key)
    owns_writer = False
    try:
        path = Path(config.log_file).resolve()
        with _FILE_LOCK:
            handler = _ACTIVE_WRITERS.get(path)
            if handler is None:
                path.parent.mkdir(parents=True, exist_ok=True)
                os.close(_private_log_open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY))
                sink = _FileHandler(path, maxBytes=config.log_max_bytes, backupCount=config.log_backup_count,
                                    encoding="utf-8", delay=True)
                sink.setFormatter(_Formatter(secret_values))
                handler = sink
                if background:
                    sink.keep_open = True
                    handler = _BufferedHandler(sink)
                    _ACTIVE_WRITERS[path] = handler
                    owns_writer = True
    except (OSError, ValueError):
        if not background:
            raise ChatError("logging", "unwritable", "无法创建日志文件，请检查 [logging] 的路径和权限。") from None
        _alert("diagnostic_log_unwritable", 1)
        logger.disabled = True
        yield logger
        return
    if isinstance(handler, _BufferedHandler):
        logger.addFilter(partial(_attach_secrets, secret_values))
    logger.addHandler(handler)
    try:
        yield logger
    finally:
        logger.removeHandler(handler)
        if owns_writer:
            handler.drain()
            with _FILE_LOCK:
                _ACTIVE_WRITERS.pop(path)
        elif not isinstance(handler, _BufferedHandler):
            handler.close()
