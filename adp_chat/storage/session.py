"""本地顺序运行的会话标识存储，不保存消息和附件访问凭据。"""

import hashlib
import json
import os
import tempfile
from pathlib import Path
from uuid import UUID, uuid4

from ..core.config import ChatConfig
from ..core.models import ChatError


def load_session(config: ChatConfig, *, new: bool = False) -> str:
    """恢复同应用同用户的会话；显式 new 才允许替换已有状态。

    新 ID 在返回前经同目录临时文件和原子替换落盘。仅支持顺序启动，
    不保证并发进程间的创建或提交顺序。
    """
    path = Path(config.session_file)
    if not path.is_absolute():
        path = Path(__file__).resolve().parents[2] / path
    identity = {
        "user_id": config.user_id,
        "app_key_fingerprint": "sha256:" + hashlib.sha256(config.app_key.encode("utf-8")).hexdigest(),
    }
    if not new:
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            pass
        except UnicodeError:
            raise ChatError("session", "invalid_session", "会话文件必须是有效的 UTF-8 JSON。") from None
        except (OSError, ValueError):
            raise ChatError("session", "unreadable", "无法读取会话文件，请检查路径和权限。") from None
        else:
            try:
                state = json.loads(raw)
                if (
                    not isinstance(state, dict)
                    or set(state) != {"schema_version", "conversation_id", *identity}
                    or type(state["schema_version"]) is not int or state["schema_version"] != 1
                    or not isinstance(state["conversation_id"], str)
                    or str(UUID(state["conversation_id"])) != state["conversation_id"]
                    or not isinstance(state["user_id"], str) or not state["user_id"].strip()
                    or not isinstance(state["app_key_fingerprint"], str)
                    or len(state["app_key_fingerprint"]) != 71
                    or not state["app_key_fingerprint"].startswith("sha256:")
                    or any(c not in "0123456789abcdef" for c in state["app_key_fingerprint"][7:])
                ):
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise ChatError("session", "invalid_session", "会话文件损坏或版本不支持；请保留文件排查，或显式开启新会话。") from None
            if any(state[key] != value for key, value in identity.items()):
                raise ChatError("session", "identity_mismatch", "会话所属应用或用户不匹配；请使用独立会话路径或显式开启新会话。")
            return state["conversation_id"]

    conversation_id = str(uuid4())
    state = {"schema_version": 1, "conversation_id": conversation_id, **identity}
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=".session-", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(state, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except (OSError, ValueError):
        cleanup_failed = False
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                cleanup_failed = True
        # 清理错误不得遮盖保存失败的既有错误分类，也不能静默宣称已清理。
        message = "无法安全保存会话；本轮未发送，请检查会话路径和权限。"
        if cleanup_failed:
            message += "同目录临时文件清理也失败，请检查残留文件。"
        raise ChatError("session", "unwritable", message) from None
    return conversation_id
