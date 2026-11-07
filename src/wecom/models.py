"""已验签并核实企业、应用和员工的普通文字消息。"""

from dataclasses import dataclass
import re


def is_employee_id(value: object) -> bool:
    """遵守官方成员账号格式，首字符字母数字，排除@all与多人分隔符。"""
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_@.\-]{0,63}", value) is not None


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """单条可信入站；模拟密钥产生的身份仅代表测试输入。"""

    corp_id: str
    agent_id: int
    user_id: str
    msg_id: str
    content: str
    create_time: int
