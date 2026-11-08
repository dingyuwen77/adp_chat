"""独立文件诊断：不安装控制台 Handler，不更改宿主 root logger。"""

import logging
import json
import re
from uuid import uuid4
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


class _Formatter(logging.Formatter):
    """使用北京时间及统一源码前缀，在最终写入边界脱敏已知凭据。"""

    def __init__(self, secrets: tuple[str, ...]) -> None:
        """只保存本次配置的脱敏值，不把它们写进记录字段。"""
        super().__init__("[%(asctime)s %(filename)s L%(lineno)d] [%(levelname)s] %(message)s")
        self._secrets = tuple(value for value in secrets if value)

    def formatTime(self, record: logging.LogRecord, datefmt=None) -> str:
        """从记录的真实时间转换到 UTC+8，固定显示三位毫秒。"""
        moment = datetime.fromtimestamp(record.created, _BEIJING)
        return moment.strftime("%Y-%m-%d %H:%M:%S.") + f"{moment.microsecond // 1000:03d}"

    def format(self, record: logging.LogRecord) -> str:
        """所有普通日志行使用同一文本前缀与版本化机器字段，供未来逐行采集。"""
        # 保留已有人工可读正文；原始异常/换行不能制造额外的日志行。
        message = record.getMessage()
        message = _mask_secrets(_URL.sub(_mask_private_url, message), self._secrets)
        message = message.replace("\r", "\\r").replace("\n", "\\n")
        message = message.replace("\u0085", "\\u0085").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        timestamp = datetime.fromtimestamp(record.created, _BEIJING).isoformat(timespec="milliseconds")
        fields = {
            "schema_version": "adp_chat.textlog.v1",
            "id": str(uuid4()),
            "timestamp": timestamp,
            "level": record.levelname,
            "service": "adp_chat",
            "source_file": record.filename,
            "source_line": record.lineno,
            "message": message,
            "kind": "diagnostic",
        }
        if message.startswith("gateway_event "):
            try:
                event = json.loads(message[len("gateway_event "):])
            except (ValueError, TypeError):
                event = None
            if isinstance(event, dict) and event.get("schema_version") == "adp_chat.log.v1":
                fields["kind"] = "gateway_event"
                fields["event"] = event
                # 网关事件的持久 ID 在原事件生成时确定，采集时不能再造。
                fields["id"] = event["id"]
        visible = f"[{self.formatTime(record)} {record.filename} L{record.lineno}] [{record.levelname}] {message}"
        # 只有这一份普通文本文件；末尾 record= 是可直接 JSON 解码的机器字段，
        # 不要求解析前面的人类诊断文字。
        return visible + " | record=" + json.dumps(fields, ensure_ascii=False, separators=(",", ":"))


def parse_log_line(line: str) -> dict:
    """将新版普通日志的每一行转换为一个可入库文档；不猜测旧版自由文本。"""
    prefix, separator, payload = line.rstrip("\\r\\n").rpartition(" | record=")
    if not separator or not prefix.startswith("["):
        raise ValueError("不是可解析的 adp_chat.textlog.v1 日志行")
    try:
        document = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ValueError("日志机器字段损坏") from exc
    if not isinstance(document, dict) or document.get("schema_version") != "adp_chat.textlog.v1":
        raise ValueError("日志 Schema 不受支持")
    if not isinstance(document.get("id"), str) or not isinstance(document.get("message"), str):
        raise ValueError("日志标识或正文无效")
    return document


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
