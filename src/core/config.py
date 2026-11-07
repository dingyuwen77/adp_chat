import os
import base64
import binascii
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import TIMEOUT_MAX

from .models import ChatError


ADP_CHAT_URL = "https://wss.lke.cloud.tencent.com/adp/v2/chat"


@dataclass(frozen=True, slots=True)
class ChatConfig:
    """从一份 TOML 读取的不可变配置，可用于多次对话。"""

    app_key: str = field(repr=False)
    user_id: str
    visitor_id: str = ""
    user_name: str = ""
    chat_timeout_seconds: float = 180
    stream: bool = False
    streaming_throttle: int = 0
    system_role: str = field(default="", repr=False)
    search_network: str = ""
    workflow_status: str = ""
    enable_multi_intent: bool | None = None
    log_enabled: bool = True
    log_level: str = "INFO"
    log_file: str = "logs/adp_chat.log"
    log_max_bytes: int = 5_242_880
    log_backup_count: int = 3
    log_answer_chars: int = 0
    log_question: bool = False
    session_file: str = "runtime/session.json"
    corp_id: str = ""
    agent_id: int = 0
    adp_application: str = ""
    gateway_enabled: bool = False
    callback_token: str = field(default="", repr=False)
    encoding_aes_key: str = field(default="", repr=False)
    messages_dir: str = "runtime/messages"
    callback_max_bytes: int = 65536
    max_unfinished_messages: int = 2000
    adp_url: str = ADP_CHAT_URL
    adp_response_max_bytes: int = 1_048_576
    notification_mode: str = "disabled"
    gateway_environment: str = "test"
    wecom_app_secret: str = field(default="", repr=False)
    send_timeout_seconds: float = 15

    def __post_init__(self) -> None:
        """统一校验文件加载和 Python 直接构造的配置。"""
        for section, fields in _SECTIONS.items():
            for key, allowed in fields.items():
                value = getattr(self, key)
                if key == "enable_multi_intent" and value is None:
                    continue
                types = allowed if isinstance(allowed, tuple) else (allowed,)
                if type(value) not in types:
                    raise ChatError("config", "invalid_type", f"配置字段 [{section}].{key} 类型错误。")
        if not self.app_key.strip() or not self.user_id.strip():
            raise ChatError("config", "missing_identity", "请填写 [adp] 的 app_key 和 user_id。")
        if not 0 < self.chat_timeout_seconds <= TIMEOUT_MAX:
            raise ChatError("config", "invalid_timeout", "配置字段 chat_timeout_seconds 超出系统支持的超时范围。")
        if self.streaming_throttle < 0:
            raise ChatError("config", "invalid_range", "流式节流超出有效范围。")
        for key in ("search_network", "workflow_status"):
            if getattr(self, key) not in ("", "enable", "disable"):
                raise ChatError("config", "invalid_option", f"配置字段 {key} 的取值无效。")
        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            raise ChatError("config", "invalid_option", "日志级别必须是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL。")
        if not self.log_file.strip() or self.log_max_bytes <= 0 or self.log_backup_count < 1 or self.log_answer_chars < 0:
            raise ChatError("config", "invalid_logging", "日志路径、轮转参数和回答预览长度必须有效。")
        if not self.session_file.strip() or "\0" in self.session_file:
            raise ChatError("config", "invalid_session_path", "会话文件路径不能为空或含有空字符。")
        scope = (self.corp_id, self.agent_id, self.adp_application)
        if any(scope) and (
            not self.corp_id or not self.adp_application or self.agent_id <= 0
            or any(value != value.strip() or any(ord(c) < 32 for c in value)
                   for value in (self.corp_id, self.adp_application))
        ):
            raise ChatError("config", "invalid_wecom_scope", "[wecom] 的企业、应用 ID 和固定 ADP 逻辑应用名必须完整有效。")
        if not self.messages_dir.strip() or "\0" in self.messages_dir or not 1024 <= self.callback_max_bytes <= 1_048_576 or self.max_unfinished_messages < 1:
            raise ChatError("config", "invalid_gateway_resources", "网关目录或物理资源保护配置无效。")
        if self.gateway_enabled:
            try:
                valid_key = len(self.encoding_aes_key) == 43 and len(base64.b64decode(self.encoding_aes_key + "=", validate=True)) == 32
            except (ValueError, binascii.Error):
                valid_key = False
            if (not all(scope) or not valid_key or not self.callback_token.isascii()
                    or not self.callback_token.isalnum() or len(self.callback_token) > 32):
                raise ChatError("config", "invalid_gateway_credentials", "启用网关需要完整身份作用域和有效的回调测试 Token/AES 密钥。")
        from urllib.parse import urlsplit
        try:
            target = urlsplit(self.adp_url)
            port = target.port
        except ValueError:
            raise ChatError("config", "invalid_adp_url", "网关 ADP 地址格式无效。") from None
        if (self.adp_url != ADP_CHAT_URL and (target.scheme not in ("http", "https")
                or target.hostname not in ("127.0.0.1", "localhost", "::1")
                or target.path != "/adp/v2/chat" or target.query or target.fragment
                or target.username is not None or target.password is not None or port == 0)):
            raise ChatError("config", "invalid_adp_url", "网关 ADP 地址只允许正式 HTTPS 端点或本机 Fake 端点。")
        if not 16_384 <= self.adp_response_max_bytes <= 1_048_576:
            raise ChatError("config", "invalid_gateway_resources", "ADP 流物理大小保护须在16KiB至1MiB之间。")
        if (self.notification_mode not in {"disabled", "simulated", "wecom"}
                or self.gateway_environment not in {"test", "production"}
                or (self.notification_mode == "simulated" and self.gateway_environment != "test")
                or not 0 < self.send_timeout_seconds <= TIMEOUT_MAX):
            raise ChatError("config", "invalid_notification", "通知模式、环境或超时配置无效。")


_SECTIONS = {
    "adp": {
        "app_key": str, "user_id": str, "visitor_id": str, "user_name": str,
    },
    "chat": {
        "chat_timeout_seconds": (int, float), "stream": bool,
        "streaming_throttle": int, "system_role": str, "search_network": str,
        "workflow_status": str, "enable_multi_intent": bool,
    },
    "session": {"session_file": str},
    "wecom": {
        "corp_id": str, "agent_id": int, "adp_application": str, "gateway_enabled": bool,
        "callback_token": str, "encoding_aes_key": str, "messages_dir": str,
        "callback_max_bytes": int, "max_unfinished_messages": int,
        "adp_url": str, "adp_response_max_bytes": int,
        "notification_mode": str, "gateway_environment": str, "wecom_app_secret": str,
        "send_timeout_seconds": (int, float),
    },
    "logging": {
        "log_enabled": bool, "log_level": str, "log_file": str,
        "log_max_bytes": int, "log_backup_count": int,
        "log_answer_chars": int, "log_question": bool,
    },
}


def default_config_path() -> Path:
    """开发时定位源码根配置，安装部署时通过显式环境变量绑定外置配置。"""
    configured = os.environ.get("ADP_CONFIG_FILE")
    return Path(configured) if configured else Path(__file__).resolve().parents[2] / "config.toml"


def load_config(path: str | Path | None = None) -> ChatConfig:
    """读取当前应用配置；服务器专用部署表由部署脚本独立校验。"""
    path = Path(path) if path is not None else default_config_path()
    try:
        with path.open("rb") as source:
            data = tomllib.load(source)
    except (UnicodeError, tomllib.TOMLDecodeError):
        raise ChatError("config", "invalid_toml", "配置文件必须是有效的 UTF-8 TOML。") from None
    except (OSError, TypeError, ValueError):
        raise ChatError("config", "unreadable", "无法读取配置文件，请检查路径和权限。") from None

    values = {}
    for name, group in data.items():
        # 部署 IP/端口只由服务器脚本校验，不影响本地入口或进入 ADP 配置。
        if name == "deployment" and isinstance(group, dict):
            continue
        if name not in _SECTIONS or not isinstance(group, dict):
            raise ChatError("config", "unknown_section", "配置包含未知分组或分组格式错误。")
        for key, value in group.items():
            if key not in _SECTIONS[name]:
                raise ChatError("config", "unknown_key", f"配置分组 [{name}] 包含未知字段。")
            values[key] = value

    values.setdefault("app_key", "")
    values.setdefault("user_id", "")
    config = ChatConfig(**values)
    log_path = Path(config.log_file)
    if not log_path.is_absolute():
        config = replace(config, log_file=str(Path(path).absolute().parent / log_path))
    session_path = Path(config.session_file)
    if not session_path.is_absolute():
        config = replace(config, session_file=str(Path(path).absolute().parent / session_path))
    messages_path = Path(config.messages_dir)
    if not messages_path.is_absolute():
        config = replace(config, messages_dir=str(Path(path).absolute().parent / messages_path))
    return config
