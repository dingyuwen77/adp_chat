"""企业微信可靠接收，提交消息后通知后台首次转发。"""

import asyncio
import re

from core.config import ChatConfig
from core.events import GatewayEvents
from core.logging import log_conversation
from core.models import ChatError
from storage.sqlite_store import MessageStore
from wecom.crypto import WeComCrypto, invalid_callback, parse_xml, xml_field
from wecom.models import InboundMessage, is_employee_id


class WeComGateway:
    """复用已验签身份与 SQLite 消息状态，后续调度沿同一消息执行。"""

    def __init__(self, config: ChatConfig, logger):
        """创建加解密和数据库资源，由 FastAPI lifespan 持有。"""
        self.config = config
        self.logger = logger
        self.crypto = WeComCrypto(config.callback_token, config.encoding_aes_key, config.corp_id)
        self.store = MessageStore(config)
        self.events = GatewayEvents(config, logger)
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

    def _submit(self, message: InboundMessage):
        """调度器失效时关闭新接收，后续由已提交状态恢复。"""
        if self.worker is not None and self.worker.failure is not None:
            raise ChatError("gateway_storage", "dispatcher_unavailable", "后台状态处理不可用。")
        pending = self.store.submit_receive(message)
        pending.add_done_callback(self._after_commit)
        return pending

    def receive(self, message: InboundMessage) -> dict:
        """隔离测试可同步调用，使用相同提交和唤醒路径。"""
        record, _ = self._submit(message).result()
        return record

    async def receive_async(self, message: InboundMessage) -> dict:
        """生产回调等待 SQLite 提交，取消已开始的写入时保留提交后的唤醒。"""
        pending = asyncio.wrap_future(self._submit(message))
        pending.add_done_callback(self._consume_detached)
        # 请求取消不提前释放待写名额，线程队列仍持有这条消息。
        record, _ = await asyncio.shield(pending)
        return record

    @staticmethod
    def _consume_detached(pending) -> None:
        """读取已取消请求遗留的本地提交异常，正常等待方仍能收到同一异常。"""
        if not pending.cancelled():
            pending.exception()

    @property
    def healthy(self) -> bool:
        """只使用本机状态判断能否继续可靠处理，健康探针无需调用上游。"""
        return self.store.available and (self.worker is None or self.worker.failure is None)

    def _after_commit(self, pending) -> None:
        """可靠接收之后记录新问题。即使客户端断开，已提交消息仍触发调度。"""
        if pending.cancelled() or pending.exception() is not None:
            return
        record, created = pending.result()
        source = "test_fixture" if self.config.gateway_environment == "test" else "wecom_callback"
        if created:
            if self.worker is not None:
                self.worker.wake()
            self.events.emit("wecom.message.received", record, attributes={"source": source})
            self.events.emit("wecom.message.persisted", record,
                             attributes={"source": source, "admission_decision": record["admission_decision"]})
            if self.config.log_question:
                log_conversation(self.logger, "question", record["content"],
                                 conversation_id=record["conversation_id"], message_key=record["message_key"],
                                 employee_id=record["user_id"], admission_decision=record["admission_decision"])
            if record["adp_state"] == "REJECTED_USER_BUSY":
                self.events.emit("wecom.message.busy_rejected", record, attributes={"source": source})
        else:
            self.events.emit("wecom.message.duplicate", record, attributes={"source": source})
        self.logger.info("wecom_callback_received message_key=%s decision=%s duplicate=%s",
                         record["message_key"], record["adp_state"], not created)

    def close(self) -> None:
        """等待磁盘提交后释放 SQLite 连接与单实例锁。"""
        self.store.close()
        self.events.close()
