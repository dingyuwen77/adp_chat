"""阶段 C：正式异步客户端、同一快照 Owner 和后台恢复的可重复验收。"""

import asyncio
import base64
import json
import logging
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import httpx

from adp.async_client import AsyncADPClient
from application.wecom_gateway import WeComGateway
from application.wecom_worker import GatewayWorker
from core.config import ChatConfig
from core.models import ChatError
from storage.messages import MessageStore
from wecom.models import InboundMessage


def frame(kind, **fields):
    """使用正式 V2 事件字段，而不借助业务文本猜测执行状态。"""
    return (f"event: {kind}\r\ndata: " + json.dumps({"Type": kind, **fields}, ensure_ascii=False) + "\r\n\r\n").encode()


def answer(payload, *, method=18, text=None):
    """各员工正式 reply 按真实 payload 产生，便于检查串线。"""
    return frame("response.completed", Response={"Status": "success", "ConversationId": payload["ConversationId"],
                 "ExtraInfo": {"ReplyMethod": method}, "Messages": [{"Type": "reply", "Contents": [
                     {"Type": "text", "Text": text or f"回复 {payload['UserId']}"}]}]})


class HeldStream(httpx.AsyncByteStream):
    """把 Fake 名额保持到测试明确释放，避免靠执行速度猜测恰好拒绝数。"""

    def __init__(self, fake, payload):
        """绑定当前请求和共享的释放屏障。"""
        self.fake, self.payload = fake, payload

    async def __aiter__(self):
        """先发成功 ACK，再异步等待，最后逐字节发送含中文的完成帧。"""
        try:
            yield frame("request_ack", RequestAck={"Status": "success", "ConversationId": self.payload["ConversationId"]})
            await self.fake.release.wait()
            for value in answer(self.payload):
                yield bytes([value])
        finally:
            self.fake.active -= 1


class FakeADP:
    """入口名额由 Fake 平台决定，网关不提供配额队列。"""

    def __init__(self, quota=None):
        """None 表示无模型名额限制；30用于平台入口拒绝。"""
        self.quota, self.active, self.peak = quota, 0, 0
        self.calls, self.rejected = [], []
        self.release = asyncio.Event()

    async def handle(self, request):
        """按真实 POST/SSE 协议返回成功或真实入口 QPS Provider Code。"""
        payload = json.loads(request.content)
        self.calls.append(payload)
        if self.quota is not None and self.active >= self.quota:
            self.rejected.append(payload["RequestId"])
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                  content=frame("error", Error={"Code": 460031, "Message": "平台入口拒绝"}))
        self.active += 1
        self.peak = max(self.peak, self.active)
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, stream=HeldStream(self, payload))


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    """真实临时磁盘配合正式 worker，不替换业务状态转换。"""

    def setUp(self):
        """隔离测试身份/密钥和目录，绝不读取真实 AppKey。"""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.config = ChatConfig("fake-c-key", "legacy", corp_id="wwf483023b734c570a", agent_id=1000053,
            adp_application="wecom-c-fixture", gateway_enabled=True, callback_token="testC123",
            encoding_aes_key=base64.b64encode(os.urandom(32)).decode()[:-1],
            messages_dir=str(self.root / "messages"), session_file=str(self.root / "legacy.json"), log_enabled=False)
        self.gateway = WeComGateway(self.config, logging.getLogger("wecom-c-test"))
        self.addCleanup(self.gateway.close)

    def receive(self, user="YNND", msg="1"):
        """沿 B 的同一存储 Owner 可靠接收，不人为选择 ACCEPTED。"""
        return self.gateway.receive(InboundMessage(self.config.corp_id, self.config.agent_id, user, msg, "测试文字", 1))

    async def wait_for(self, predicate):
        """有界等待具体可观察事实，失败不放宽超时或隐藏失败。"""
        async with asyncio.timeout(8):
            while not predicate():
                await asyncio.sleep(0.005)

    async def exercise_concurrency(self, quota):
        """100请求全部到达平台屏障，结果仅按实际拒绝集合逐一核对。"""
        fake = FakeADP(quota)
        records = [self.receive(f"EMP{i}", str(i + 1)) for i in range(100)]
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
            client = AsyncADPClient(self.config, http_client=http)
            worker = GatewayWorker(self.gateway, client)
            await worker.start()
            try:
                await self.wait_for(lambda: len(fake.calls) == 100)
                self.assertEqual(fake.peak, 100 if quota is None else 30)
                self.assertEqual(len({p["RequestId"] for p in fake.calls}), 100)
                self.assertEqual(len({p["ConversationId"] for p in fake.calls}), 100)
                self.assertTrue(all(p["UserId"] == p["VisitorId"] for p in fake.calls))
                fake.release.set()
                await self.wait_for(lambda: all(self.gateway.store.get(r["message_key"])["send_state"] == "PENDING" for r in records))
                for original in records:
                    saved = self.gateway.store.get(original["message_key"])
                    if saved["request_id"] in fake.rejected:
                        self.assertEqual(saved["adp_state"], "ADP_REJECTED")
                        self.assertEqual(saved["adp_error"]["provider_code"], "460031")
                        self.assertIn("手动", saved["notification"])
                    else:
                        self.assertEqual(saved["adp_state"], "ADP_SUCCEEDED")
                        self.assertEqual(saved["notification"], f"回复 {saved['user_id']}")
                    self.assertEqual(self.receive(saved["user_id"], str(1000 + saved["receive_sequence"]))["adp_state"], "RECEIVED")
                self.assertEqual(len(fake.calls), 100)
            finally:
                fake.release.set()
                await worker.stop()

    async def test_100_users_have_no_gateway_model_quota(self):
        """100名员工同时处于Fake未完成请求中，不能被默认100连接队列误导。"""
        await self.exercise_concurrency(None)

    async def test_fake_30_slots_reports_only_actual_entrance_rejections(self):
        """Fake明确应用入口460031拒绝，拒绝集合与网关持久结果严格一致。"""
        await self.exercise_concurrency(30)

    async def test_same_user_busy_and_duplicates_never_enter_adp(self):
        """已接收未完成同用户新消息不延后执行，重复MsgId不生成第二意图。"""
        fake = FakeADP()
        first = self.receive()
        busy = self.receive(msg="2")
        self.assertEqual(busy["adp_state"], "REJECTED_USER_BUSY")
        self.assertEqual(self.receive(), first)
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
            worker = GatewayWorker(self.gateway, AsyncADPClient(self.config, http_client=http))
            await worker.start()
            try:
                await self.wait_for(lambda: len(fake.calls) == 1)
                worker.wake()
                await asyncio.sleep(0.03)
                self.assertEqual(len(fake.calls), 1)
                fake.release.set()
                await self.wait_for(lambda: self.gateway.store.get(first["message_key"])["adp_state"] == "ADP_SUCCEEDED")
                self.assertEqual(self.gateway.store.get(busy["message_key"])["adp_state"], "REJECTED_USER_BUSY")
            finally:
                fake.release.set()
                await worker.stop()

    async def test_armed_is_durable_before_network_and_unknown_releases(self):
        """超时前实际磁盘已有唯一RequestId；UNKNOWN与通知同一提交后可接新消息。"""
        first = self.receive()
        calls = []

        async def timeout(request):
            """网络边界实读快照，然后模拟结果未知而非业务失败。"""
            payload = json.loads(request.content)
            saved = json.loads((self.root / "messages" / f"{first['message_key']}.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["adp_state"], "ADP_SEND_ARMED")
            self.assertEqual(saved["request_id"], payload["RequestId"])
            calls.append(payload)
            raise httpx.ReadTimeout("sensitive provider text")

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as http:
            worker = GatewayWorker(self.gateway, AsyncADPClient(self.config, http_client=http))
            await worker.start()
            try:
                await self.wait_for(lambda: self.gateway.store.get(first["message_key"])["adp_state"] == "ADP_UNKNOWN")
                saved = self.gateway.store.get(first["message_key"])
                self.assertEqual(saved["send_state"], "PENDING")
                self.assertIn("未知", saved["notification"])
                self.assertIn("核查", saved["notification"])
                self.assertNotIn("sensitive", json.dumps(saved))
                self.assertEqual(self.receive(msg="2")["adp_state"], "RECEIVED")
                self.assertEqual(len(calls), 1)
            finally:
                await worker.stop()

    async def test_failed_arm_never_sends_and_failed_result_does_not_fake_release(self):
        """存储发布失败关闭接收；不能在可靠状态缺失时越过网络或释放边界。"""
        self.receive()
        fake = FakeADP()
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
            worker = GatewayWorker(self.gateway, AsyncADPClient(self.config, http_client=http))
            with patch("storage.messages.os.replace", side_effect=OSError("disk full")):
                await worker.start()
                await self.wait_for(lambda: worker.failure is not None)
            self.assertEqual(fake.calls, [])
            with self.assertRaises(ChatError):
                self.receive(msg="2")
            await worker.stop()

    async def test_recovery_executes_received_once_and_never_replays_armed(self):
        """恢复以磁盘武装事实判定，与是否真正发包无关。"""
        first = self.receive()
        second = self.receive("SIYU", "2")
        self.gateway.store.update(first["message_key"], expected_version=first["record_version"],
                                  adp_state="ADP_SEND_ARMED", request_id=str(uuid4()))
        self.gateway.close()
        self.gateway = WeComGateway(self.config, logging.getLogger("wecom-c-test"))
        self.addCleanup(self.gateway.close)
        fake = FakeADP()
        fake.release.set()
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake.handle)) as http:
            worker = GatewayWorker(self.gateway, AsyncADPClient(self.config, http_client=http))
            await worker.start()
            try:
                await self.wait_for(lambda: self.gateway.store.get(second["message_key"])["adp_state"] == "ADP_SUCCEEDED")
                self.assertEqual([p["UserId"] for p in fake.calls], ["SIYU"])
                saved = self.gateway.store.get(first["message_key"])
                self.assertEqual((saved["adp_state"], saved["send_state"]), ("ADP_UNKNOWN", "PENDING"))
                self.assertEqual(self.receive(msg="3")["adp_state"], "RECEIVED")
            finally:
                await worker.stop()

    async def test_actual_provider_code_and_execution_evidence_control_classification(self):
        """无ACK不等于未执行；模型额度/ReplyMethod7不伪造入口拒绝或Code。"""
        record = self.receive()
        record = {**record, "request_id": str(uuid4())}
        cases = [
            (frame("error", Error={"Code": 460031}), "ADP_REJECTED", "460031"),
            (frame("error", Error={"Code": 460701}), "ADP_UNKNOWN", "460701"),
            (frame("error", Error={"Code": 460020}), "ADP_UNKNOWN", "460020"),
            (frame("error", Error={"Code": 999999}), "ADP_UNKNOWN", "999999"),
            (frame("request_ack", RequestAck={"Status": "success"}) + frame("error", Error={"Code": 460031}), "ADP_UNKNOWN", "460031"),
            (frame("response.processing", Response={}) + frame("error", Error={"Code": 460031}), "ADP_UNKNOWN", "460031"),
            (answer({"ConversationId": record["conversation_id"], "UserId": "YNND"}, method=7), "ADP_UNKNOWN", None),
            (b"", "ADP_UNKNOWN", None),
        ]
        for data, state, code in cases:
            with self.subTest(state=state, code=code, data=data[:40]):
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,
                     headers={"Content-Type": "text/event-stream"}, content=data))) as http:
                    outcome = await AsyncADPClient(self.config, http_client=http).request(record)
                self.assertEqual(outcome.state, state)
                self.assertEqual(outcome.error["provider_code"], code)
                self.assertIn("手动" if state == "ADP_REJECTED" else "未知", outcome.notification)

    async def test_version_conflict_invalid_transition_and_pending_purge_preserve_state(self):
        """CAS阻止过时写入；未知/待处理/待通知不能按年龄直接删除。"""
        original = self.receive()
        key = original["message_key"]
        armed = self.gateway.store.update(key, expected_version=original["record_version"],
                                         adp_state="ADP_SEND_ARMED", request_id=str(uuid4()))
        for version, changes in [(original["record_version"], {"adp_state": "RECEIVED"}),
                                 (armed["record_version"], {"adp_state": "RECEIVED"})]:
            with self.assertRaises(ChatError):
                self.gateway.store.update(key, expected_version=version, **changes)
        self.assertEqual(self.gateway.store.get(key), armed)
        self.assertEqual(self.gateway.store.purge(), 0)
        self.assertTrue(self.gateway.store.list_unfinished())


if __name__ == "__main__":
    unittest.main()
