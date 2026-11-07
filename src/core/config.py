import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import TIMEOUT_MAX

from .models import ChatError


@dataclass(frozen=True, slots=True)
class ChatConfig:
    """从一份 TOML 读取的不可变配置，可用于多次对话。"""

    app_key: str = field(repr=False)
    user_id: str
    visitor_id: str = ""
    user_name: str = ""
    chat_timeout_seconds: float = 180
    incremental: bool = True
    streaming_throttle: int = 0
    system_role: str = field(default="", repr=False)
    search_network: str = ""
    workflow_status: str = ""
    enable_multi_intent: bool | None = None
    first_question: str = field(default="", repr=False)
    show_preview: bool = True
    interactive: bool = True
    detect_text_clarification: bool = True
    structured_interaction: bool = False
    log_enabled: bool = True
    log_level: str = "INFO"
    log_file: str = "logs/adp_chat.log"
    log_max_bytes: int = 5_242_880
    log_backup_count: int = 3
    log_answer_chars: int = 0
    log_question: bool = False
    session_file: str = "runtime/session.json"

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


_SECTIONS = {
    "adp": {
        "app_key": str, "user_id": str, "visitor_id": str, "user_name": str,
    },
    "chat": {
        "chat_timeout_seconds": (int, float), "incremental": bool,
        "streaming_throttle": int, "system_role": str, "search_network": str,
        "workflow_status": str, "enable_multi_intent": bool,
        "detect_text_clarification": bool,
        "structured_interaction": bool,
    },
    "cli": {"first_question": str, "show_preview": bool, "interactive": bool},
    "session": {"session_file": str},
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
    """读取应用配置；忽略服务器专用部署表及废弃附件表。"""
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
        if name in ("attachments", "deployment") and isinstance(group, dict):
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
    return config
