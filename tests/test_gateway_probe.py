"""受控真实Probe的本机边界测试：这里只写Fake快照，不访问任何外部服务。"""

import base64
import os
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from adp_conversation_probe import gateway_round
from core.config import ChatConfig
from core.identity import employee_identity
from core.models import ChatError, ChatResult
from storage.messages import MessageStore
from wecom.models import InboundMessage
from wecom.sender import SendOutcome


class GatewayProbeTests(unittest.TestCase):
    """Probe只复用既有回调/状态机制，不成为第二条业务调用路径。"""

    def setUp(self):
        """隔离配置、身份和磁盘，不读取真实配置/AppKey。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = ChatConfig("fixture-key", "legacy", corp_id="wwf483023b734c570a", agent_id=1000053,
            adp_application="wecom-local-probe-fixture", gateway_enabled=True, notification_mode="simulated",
            callback_token=os.urandom(12).hex(), encoding_aes_key=base64.b64encode(os.urandom(32)).decode()[:-1],
            messages_dir=str(self.root / "messages"), log_enabled=False)
        self.identity = employee_identity(self.config, "YNND")

    def publish(self, config, _base, user, msg_id, text, *, rejected=False):
        """模拟可信HTTP已经处理完，仍沿正式Store校验/原子状态转换保存。"""
        with MessageStore(config) as store:
            record, _ = store.put_if_absent(InboundMessage(config.corp_id, config.agent_id, user, msg_id, text, 1))
            armed = store.update(record["message_key"], expected_version=record["record_version"],
                                 adp_state="ADP_SEND_ARMED", request_id=str(uuid4()))
            result = ChatResult("仅测试回复", armed["conversation_id"], armed["request_id"])
            saved = store.update(armed["message_key"], expected_version=armed["record_version"],
                adp_state="ADP_REJECTED" if rejected else "ADP_SUCCEEDED",
                adp_result=None if rejected else asdict(result),
                adp_error={"code": "provider_error", "provider_code": "4505004"} if rejected else None,
                notification="测试通知", notification_kind="ADP_REJECTED" if rejected else "ADP_SUCCEEDED", send_state="PENDING")
            sending = store.update(saved["message_key"], expected_version=saved["record_version"], send_state="SEND_ARMED",
                send_result=asdict(SendOutcome("SEND_ARMED", "simulated", None, None, None, "armed")))
            store.update(sending["message_key"], expected_version=sending["record_version"], send_state="SEND_SIMULATED",
                         send_result=asdict(SendOutcome("SEND_SIMULATED", "simulated", None, 1, 1, "simulated")))

    def test_gateway_waits_for_same_snapshot_and_simulated_terminal(self):
        """只提交一个MsgId，结果关联与模拟通知完成后才产生成功证据。"""
        with patch("wecom_dispatch_smoke.callback", side_effect=self.publish) as callback:
            result, evidence = gateway_round(self.config, self.identity, "我想改密码", "http://127.0.0.1:8000")
        self.assertEqual(callback.call_count, 1)
        self.assertEqual(result.conversation_id, self.identity.conversation_id)
        self.assertEqual(evidence["send_state"], "SEND_SIMULATED")
        self.assertEqual(evidence["notification_source"], "simulated")
        self.assertEqual(evidence["callback_ack"], "received")

    def test_lost_ack_only_observes_old_message_without_resubmission(self):
        """ACK丢失可能已处理完成，只读既有快照，不再执行一次回调。"""
        def lost(*args):
            """先真实落盘，再模拟ACK断连，避免把连接异常误作未执行。"""
            self.publish(*args)
            raise OSError("private-url")

        with patch("wecom_dispatch_smoke.callback", side_effect=lost) as callback:
            _, evidence = gateway_round(self.config, self.identity, "windows", "http://127.0.0.1:8000")
        self.assertEqual(callback.call_count, 1)
        self.assertEqual(evidence["callback_ack"], "unknown")

    def test_external_or_non_simulated_target_is_rejected_before_http(self):
        """不能借Probe调用真实企微、外部回调或Fake ADP冒充真实测试。"""
        cases = [(self.config, "http://example.com"),
                 (replace(self.config, notification_mode="disabled"), "http://127.0.0.1:8000"),
                 (replace(self.config, adp_url="http://127.0.0.1:8765/adp/v2/chat"), "http://127.0.0.1:8000")]
        with patch("wecom_dispatch_smoke.callback") as callback:
            for config, url in cases:
                with self.subTest(url=url), self.assertRaises(ChatError):
                    gateway_round(config, self.identity, "windows", url)
        callback.assert_not_called()

    def test_provider_rejection_evidence_keeps_code_and_sender_source(self):
        """明确拒绝先完成模拟提示，再保留真实格式Code，不声称ADP成功。"""
        def reject(*args):
            """确定性夹具模拟4505004，不连接云端。"""
            self.publish(*args, rejected=True)

        with patch("wecom_dispatch_smoke.callback", side_effect=reject) as callback:
            with self.assertRaises(ChatError) as caught:
                gateway_round(self.config, self.identity, "windows", "http://127.0.0.1:8000")
        self.assertEqual(callback.call_count, 1)
        self.assertEqual(caught.exception.provider_code, "4505004")
        self.assertEqual(caught.exception.gateway_evidence["send_state"], "SEND_SIMULATED")
