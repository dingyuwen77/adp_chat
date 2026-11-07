"""多员工 ADP 身份；传入值须由调用边界先完成可信认证。"""

import hashlib
import json
from dataclasses import dataclass, replace

from .config import ChatConfig
from .models import ChatError


@dataclass(frozen=True, slots=True)
class EmployeeIdentity:
    """员工的 ADP 身份；固定会话标识不保证平台永久记忆。"""

    user_id: str
    visitor_id: str
    conversation_id: str

    def chat_config(self, config: ChatConfig) -> ChatConfig:
        """每轮覆盖身份而不修改共享配置，也不带入固定用户的姓名。"""
        return replace(config, user_id=self.user_id, visitor_id=self.visitor_id, user_name="")


def employee_identity(config: ChatConfig, user_id: str) -> EmployeeIdentity:
    """按版本化 JSON 元组派生会话，AppKey 轮换不改变同一逻辑应用身份。"""
    if not config.corp_id or not config.agent_id or not config.adp_application:
        raise ChatError("config", "missing_wecom_scope", "请配置 [wecom] 的企业、应用 ID 和固定 ADP 逻辑应用名。")
    if (not isinstance(user_id, str) or not user_id or user_id != user_id.strip()
            or any(ord(c) < 32 for c in user_id)):
        raise ChatError("input", "invalid_employee", "员工 UserID 必须为非空且不含控制字符的准确标识。")
    # 此编码是持久会话契约，字段类型、顺序、编码和命名空间不能静默变化。
    canonical = json.dumps(
        ["adp_chat:wecom:v1", config.corp_id, config.agent_id, config.adp_application, user_id],
        ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")
    return EmployeeIdentity(user_id, user_id, hashlib.sha256(canonical).hexdigest())
