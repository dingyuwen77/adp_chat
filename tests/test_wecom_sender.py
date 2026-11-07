"""阶段D：正式发送适配器只用Fake HTTP；不连接企微、不把模拟当实际接受。"""

import asyncio
import base64
import io
import json
import logging
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import httpx

from adp.async_client import ADPOutcome, unknown_outcome
from application.wecom_gateway import WeComGateway
from application.wecom_worker import GatewayWorker
from core.config import ChatConfig
from core.models import ChatResult
from wecom.models import InboundMessage
from wecom.sender import SendOutcome, SimulatedSender, WeComSender


class SenderTests(unittest.IsolatedAsyncioTestCase):
    """真实消息快照与正式Worker负责通知，不在测试中代写业务终态。"""

    def setUp(self):
        """所有密钥独立随机生成，不读取或保存真实应用Secret。"""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = ChatConfig("fake-local-adp-only", "legacy", corp_id="wwf483023b734c570a", agent_id=1000053,
            adp_application="wecom-d-fixture", gateway_enabled=True, callback_token=os.urandom(12).hex(),
            encoding_aes_key=base64.b64encode(os.urandom(32)).decode()[:-1],
            messages_dir=str(self.root / "messages"), session_file=str(self.root / "legacy.json"),
            log_enabled=False, gateway_environment="test", notification_mode="simulated")
        self.gateway = WeComGateway(self.config, logging.getLogger("wecom-d-test"))
        self.addCleanup(self.gateway.close)
        self.token = os.urandom(24).hex()
        self.secret = os.urandom(24).hex()

    def receive(self, user="YNND", msg="1"):
        """沿可信接收后的同一Owner进行原子准入，不跳过后续状态。"""
        return self.gateway.receive(InboundMessage(self.config.corp_id, self.config.agent_id, user, msg, "独立模拟文字", 1))

    async def wait_for(self, predicate):
        """以具体持久状态有界等待，不按固定延迟判断成功。"""
        async with asyncio.timeout(8):
            while not predicate():
                await asyncio.sleep(0.005)

    def pending(self, user="YNND", text="模拟回答"):
        """发送器只消费已保存通知；此处保留完整源身份供payload检查。"""
        return {**self.receive(user), "notification": text}

    async def test_payload_token_cache_and_fake_acceptance_remain_simulated(self):
        """同应用共享token，payload只发单一原用户；Fake成功永不标记真实接受。"""
        calls = []

        def handle(request):
            """模拟官方两个HTTPS路径，秘密只在进程内核对。"""
            calls.append(request)
            self.assertEqual(request.url.host, "qyapi.weixin.qq.com")
            if request.url.path == "/cgi-bin/gettoken":
                self.assertEqual(request.url.params["corpid"], self.config.corp_id)
                self.assertEqual(request.url.params["corpsecret"], self.secret)
                return httpx.Response(200, json={"errcode": 0, "access_token": self.token, "expires_in": 7200})
            self.assertEqual(request.url.path, "/cgi-bin/message/send")
            self.assertEqual(request.url.params["access_token"], self.token)
            payload = json.loads(request.content)
            self.assertEqual(payload["agentid"], self.config.agent_id)
            self.assertEqual(payload["msgtype"], "text")
            self.assertNotIn("toparty", payload)
            self.assertNotIn("totag", payload)
            return httpx.Response(200, json={"errcode": 0, "invaliduser": "", "unlicenseduser": ""})

        config = replace(self.config, wecom_app_secret=self.secret)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            sender = WeComSender(config, http_client=http)
            try:
                for user in ("YNND", "SIYU"):
                    result = await sender.send(self.pending(user))
                    self.assertEqual((result.state, result.source), ("SEND_SIMULATED", "fake_http"))
                    self.assertIs(result.protocol_accepted, True)
            finally:
                await sender.aclose()
        self.assertEqual(sum(r.method == "GET" for r in calls), 1)
        self.assertEqual([json.loads(r.content)["touser"] for r in calls if r.method == "POST"], ["YNND", "SIYU"])

    async def test_utf8_long_reply_has_no_silent_platform_truncation(self):
        """逐UTF8字符切片，每片不超过官方2048字节，拼接仍是全部原文。"""
        parts = []

        def handle(request):
            """Fake只确认各片实际收到的内容。"""
            if request.method == "GET":
                return httpx.Response(200, json={"errcode": 0, "access_token": self.token, "expires_in": 7200})
            parts.append(json.loads(request.content)["text"]["content"])
            return httpx.Response(200, json={"errcode": 0})

        text = "汉字🙂" * 1100
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            sender = WeComSender(replace(self.config, wecom_app_secret=self.secret), http_client=http)
            try:
                result = await sender.send(self.pending(text=text))
            finally:
                await sender.aclose()
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(0 < len(p.encode("utf-8")) <= 2048 for p in parts))
        self.assertEqual("".join(parts), text)
        self.assertEqual((result.state, result.accepted_parts), ("SEND_SIMULATED", len(parts)))

    async def test_expired_token_refreshes_only_one_definitively_unaccepted_send(self):
        """42001/40014可证明未接受，仅允许一次刷新重发相同已保存提示。"""
        for error_code in (42001, 40014):
            gets, posts = [], []

            def handle(request):
                """首发Token过期，刷新后成功；没有任何ADP调用。"""
                if request.method == "GET":
                    gets.append(request)
                    return httpx.Response(200, json={"errcode": 0, "access_token": self.token + str(len(gets)), "expires_in": 7200})
                posts.append(json.loads(request.content))
                return httpx.Response(200, json={"errcode": error_code if len(posts) == 1 else 0})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                sender = WeComSender(replace(self.config, wecom_app_secret=self.secret), http_client=http)
                try:
                    outcome = await sender.send(self.pending())
                finally:
                    await sender.aclose()
            self.assertEqual((len(gets), len(posts)), (2, 2))
            self.assertEqual(posts[0], posts[1])
            self.assertEqual(outcome.state, "SEND_SIMULATED")

    async def test_business_errors_and_invalid_targets_are_not_http_success(self):
        """HTTP200、errcode0、目标有效三者须同时成立；畸形响应保守未知。"""
        cases = [({"errcode": 40003}, "SEND_FAILED"), ({"errcode": 60020}, "SEND_FAILED"),
                 ({"errcode": 0, "invaliduser": "OTHER|YNND"}, "SEND_FAILED"),
                 ({"errcode": 0, "unlicenseduser": "YNND"}, "SEND_FAILED"),
                 ({"errcode": False}, "SEND_UNKNOWN"), ({"errcode": "0"}, "SEND_UNKNOWN"),
                 ({"errcode": 0, "invaliduser": []}, "SEND_UNKNOWN")]
        for body, expected in cases:
            with self.subTest(body=body):
                posts = []

                def handle(request):
                    """每个失败只一次发送，不派生失败的失败通知。"""
                    if request.method == "GET":
                        return httpx.Response(200, json={"errcode": 0, "access_token": self.token, "expires_in": 7200})
                    posts.append(request)
                    return httpx.Response(200, json=body)

                async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                    sender = WeComSender(replace(self.config, wecom_app_secret=self.secret), http_client=http)
                    try:
                        outcome = await sender.send(self.pending())
                    finally:
                        await sender.aclose()
                self.assertEqual(outcome.state, expected)
                self.assertEqual(len(posts), 1)

    async def test_network_unknown_is_not_retried_and_url_secrets_never_log(self):
        """发送响应丢失不重发，HTTPX正常信息日志也不得泄露Token或Secret。"""
        posts = []

        def handle(request):
            """获取Token成功，消息POST在可能已接受后超时。"""
            if request.method == "GET":
                return httpx.Response(200, json={"errcode": 0, "access_token": self.token, "expires_in": 7200})
            posts.append(request)
            raise httpx.ReadTimeout("sensitive provider error")

        log = logging.getLogger("httpx")
        previous = log.level
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                sender = WeComSender(replace(self.config, wecom_app_secret=self.secret), http_client=http)
                try:
                    outcome = await sender.send(self.pending())
                finally:
                    await sender.aclose()
        finally:
            log.removeHandler(handler)
            log.setLevel(previous)
        self.assertEqual((outcome.state, len(posts)), ("SEND_UNKNOWN", 1))
        self.assertNotIn(self.secret, output.getvalue())
        self.assertNotIn(self.token, output.getvalue())

    async def test_all_notification_kinds_share_sender_without_holding_adp_session(self):
        """正常/BUSY/入口拒绝/UNKNOWN同一发送器，发送仍挂起时新ADP可进入。"""
        first = self.receive()
        busy = self.receive(msg="2")
        third = self.receive("SIYU", "3")
        adp_calls, notices = [], []
        release = asyncio.Event()

        class ADP:
            """仅Fake一轮结果，绝不代表实际Provider响应。"""

            async def request(inner, record):
                """按测试消息标识安排结果，不给网关增加意图规则。"""
                adp_calls.append(record["message_key"])
                if record["msg_id"] == "3":
                    return ADPOutcome("ADP_REJECTED", "应用入口QPS超限（460031），请手动重试。", error={"code": "provider_error", "provider_code": "460031"})
                if record["msg_id"] == "4":
                    return unknown_outcome("timeout")
                result = ChatResult("YNND模拟回答", record["conversation_id"], record["request_id"])
                return ADPOutcome("ADP_SUCCEEDED", result.text, result=result)

        class Sender:
            """统一模拟出口可被独立挂起，不会占用ADP身份。"""

            source = "simulated"

            async def send(inner, record):
                """仅收集测试通知与原目标，不实际向员工发送。"""
                notices.append((record["user_id"], record["notification_kind"], record["notification"]))
                await release.wait()
                return SendOutcome("SEND_SIMULATED", "simulated", None, 1, 1, "simulated")

        worker = GatewayWorker(self.gateway, ADP(), sender=Sender())
        await worker.start()
        try:
            await self.wait_for(lambda: len(notices) == 3)
            self.assertEqual(self.receive(msg="4")["adp_state"], "RECEIVED")
            await self.wait_for(lambda: len(notices) == 4)
            self.assertEqual(set((user, kind) for user, kind, _ in notices), {
                ("YNND", "USER_BUSY"), ("YNND", "ADP_SUCCEEDED"), ("SIYU", "ADP_REJECTED"), ("YNND", "ADP_UNKNOWN")})
            self.assertEqual(len(adp_calls), 3)
            release.set()
            await self.wait_for(lambda: self.gateway.store.get(first["message_key"])["send_state"] == "SEND_SIMULATED")
            self.assertEqual(self.gateway.store.get(busy["message_key"])["adp_state"], "REJECTED_USER_BUSY")
            self.assertEqual(self.gateway.store.get(third["message_key"])["adp_state"], "ADP_REJECTED")
        finally:
            release.set()
            await worker.stop()


if __name__ == "__main__":
    unittest.main()
