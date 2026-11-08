"""企业微信可靠接收编排，沿同一快照通知后台首次转发。"""

import re

from core.config import ChatConfig
from core.events import GatewayEvents
from storage.messages import MessageStore
from wecom.crypto import WeComCrypto, invalid_callback, parse_xml, xml_field
from wecom.models import InboundMessage, is_employee_id


class WeComGateway:
    """复用已验签身份与单一快照 Owner，后续调度沿同一消息扩展。"""

    def __init__(self, config: ChatConfig, logger):
        """创建加解密和目录独占资源，由 FastAPI lifespan 持有。"""
        self.config = config
        self.logger = logger
        self.crypto = WeComCrypto(config.callback_token, config.encoding_aes_key, config.corp_id)
        self.store = MessageStore(config)
        self.events = GatewayEvents(config)
        self.worker = None

    def decode(self, body: bytes, timestamp: str, nonce: str, signature: str) -> InboundMessage | None:
        """认证内外企业/应用与员工；非文字事件只忽略，不创建聊天任务。"""
        outer = parse_xml(body)
        if (xml_field(outer, "ToUserName") != self.config.corp_id
                or xml_field(outer, "AgentID") != str(self.config.agent_id)):
            raise invalid_callback()
        plaintext = self.crypto.decrypt(xml_field(outer, "Encrypt"), timestamp, nonce, signature)
        inner = parse_xml(plaintext)
        if (xml_field(inner, "ToUserName") != self.config.corp_id
                or xml_field(inner, "AgentID") != str(self.config.agent_id)):
            raise invalid_callback()
        user_id = xml_field(inner, "FromUserName")
        if not is_employee_id(user_id):
            raise invalid_callback()
        if xml_field(inner, "MsgType") != "text":
            return None
        msg_id = xml_field(inner, "MsgId")
        created = xml_field(inner, "CreateTime")
        content = xml_field(inner, "Content")
        if (not re.fullmatch(r"[0-9]{1,32}", msg_id)
                or not re.fullmatch(r"[0-9]{1,20}", created) or not content.strip()):
            raise invalid_callback()
        return InboundMessage(self.config.corp_id, self.config.agent_id, user_id, msg_id, content, int(created))

    def receive(self, message: InboundMessage) -> dict:
        """持久化成功才交回 ACK 决策，不记录员工身份、正文或密文。"""
        if self.worker is not None and self.worker.failure is not None:
            from core.models import ChatError
            raise ChatError("gateway_storage", "dispatcher_unavailable", "后台状态处理不可用。")
        record, created = self.store.put_if_absent(message)
        source = "test_fixture" if self.config.gateway_environment == "test" else "wecom_callback"
        if created:
            self.events.emit("wecom.message.received", record, attributes={"source": source})
            self.events.emit("wecom.message.persisted", record,
                             attributes={"source": source, "admission_decision": record["admission_decision"]})
            if record["adp_state"] == "REJECTED_USER_BUSY":
                self.events.emit("wecom.message.busy_rejected", record, attributes={"source": source})
        else:
            self.events.emit("wecom.message.duplicate", record, attributes={"source": source})
        self.logger.info("wecom_callback_received message_key=%s decision=%s duplicate=%s",
                         record["message_key"], record["adp_state"], not created)
        if created and self.worker is not None:
            self.worker.wake()
        return record

    def close(self) -> None:
        """等待磁盘提交后释放目录，不把关闭变成任务已处理的证据。"""
        self.store.close()
        self.events.close()
