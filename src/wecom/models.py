"""已验签并核实企业、应用和员工的普通文字消息。"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """单条可信入站；模拟密钥产生的身份仅代表测试输入。"""

    corp_id: str
    agent_id: int
    user_id: str
    msg_id: str
    content: str
    create_time: int
