"""阶段 C：正式异步客户端、同一快照 Owner 和后台恢复的可重复验收。"""

import asyncio
import base64
import contextlib
import io
import json
import logging
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from threading import Event
from unittest.mock import patch
from uuid import uuid4

import httpx

from adp.async_client import AsyncADPClient, unknown_outcome
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
            adp_application="wecom-c-fixture", gateway_enabled=True, callback_token=os.urandom(12).hex(),
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
            with patch("storage.messages._publish", side_effect=OSError("disk full")):
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

    async def test_snapshot_reader_does_not_block_atomic_publication(self):
        """Windows监视读取需共享DELETE权限，不能使正常结果保存变成WinError5。"""
        from storage.messages import _snapshot_reader, read_snapshot

        original = self.receive()
        path = self.root / "messages" / f"{original['message_key']}.json"
        with _snapshot_reader(path) as reader:
            armed = self.gateway.store.update(original["message_key"], expected_version=original["record_version"],
                                             adp_state="ADP_SEND_ARMED", request_id=str(uuid4()))
            self.assertEqual(json.load(reader)["adp_state"], "RECEIVED")
        self.assertEqual(read_snapshot(path)["request_id"], armed["request_id"])

    async def test_stale_dispatch_snapshot_is_not_a_fatal_worker_error(self):
        """扫描副本过时只能输掉CAS，不能把已完成请求误判成全局存储故障。"""
        record = self.receive()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,
            headers={"Content-Type": "text/event-stream"}, content=answer(json.loads(r.content))))) as http:
            worker = GatewayWorker(self.gateway, AsyncADPClient(self.config, http_client=http))
            await worker._execute(record)
            await worker._execute(record)
            self.assertIsNone(worker.failure)
            self.assertEqual(self.gateway.store.get(record["message_key"])["adp_state"], "ADP_SUCCEEDED")

    async def test_result_commit_failure_preserves_armed_and_recovers_unknown_without_call(self):
        """已执行却结果未保存时保留armed，重启只能未知，不再询问ADP。"""
        record = self.receive()
        calls = []

        async def reply(request):
            """只在结果发布阶段注入磁盘错误，武装已成功。"""
            calls.append(json.loads(request.content))
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=answer(calls[-1]))

        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as http:
            worker = GatewayWorker(self.gateway, AsyncADPClient(self.config, http_client=http))
            original = self.gateway.store._write

            def fail_result(value):
                """正式_write的发布异常必须让store失效，不能只丢返回值。"""
                if value["adp_state"] == "ADP_SUCCEEDED":
                    with patch("storage.messages._publish", side_effect=OSError("disk full")):
                        return original(value)
                return original(value)

            with patch.object(self.gateway.store, "_write", side_effect=fail_result):
                await worker._execute(record)
            self.assertIsNotNone(worker.failure)
            self.assertEqual(self.gateway.store.get(record["message_key"])["adp_state"], "ADP_SEND_ARMED")
            self.gateway.close()
            self.gateway = WeComGateway(self.config, logging.getLogger("wecom-c-test"))
            self.addCleanup(self.gateway.close)
            recovering = GatewayWorker(self.gateway, AsyncADPClient(self.config, http_client=http))
            await recovering.start()
            await recovering.stop()
            self.assertEqual(len(calls), 1)
            self.assertEqual(self.gateway.store.get(record["message_key"])["adp_state"], "ADP_UNKNOWN")

    async def test_retention_never_deletes_unknown_or_pending_and_keeps_dedup(self):
        """已接受后7天脱敏、首次接收30天去重；未知不能按年龄丢弃。"""
        record = self.receive()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,
            headers={"Content-Type": "text/event-stream"}, content=answer(json.loads(r.content))))) as http:
            worker = GatewayWorker(self.gateway, AsyncADPClient(self.config, http_client=http))
            await worker._execute(record)
        key = record["message_key"]
        saved = self.gateway.store.get(key)
        for state in ("SEND_ARMED", "SEND_ACCEPTED"):
            saved = self.gateway.store.update(key, expected_version=saved["record_version"], send_state=state)
        now = datetime.fromisoformat(saved["updated_at"])
        self.assertEqual(self.gateway.store.purge(now=now + timedelta(days=8)), 0)
        redacted = self.gateway.store.get(key)
        self.assertIsNone(redacted["content"])
        self.assertIsNone(redacted["notification"])
        self.assertEqual(self.receive(), redacted)
        unknown = self.receive("SIYU", "2")
        armed = self.gateway.store.update(unknown["message_key"], expected_version=1,
                                         adp_state="ADP_SEND_ARMED", request_id=str(uuid4()))
        await worker._finish(armed, unknown_outcome())
        self.assertEqual(self.gateway.store.purge(now=now + timedelta(days=31)), 1)
        self.assertIsNone(self.gateway.store.get(key))
        self.assertEqual(self.gateway.store.get(unknown["message_key"])["adp_state"], "ADP_UNKNOWN")

    async def test_partial_windows_replacement_failure_cannot_erase_execution_boundary(self):
        """ReplaceFileW少数错误可移动原文件；必须保留暂存证据并阻止盲目空目录恢复。"""
        record = self.receive()
        path = self.root / "messages" / f"{record['message_key']}.json"

        def lost_original(source, target):
            """模拟官方1176类故障：原文件已移除，完整替换文件仍在。"""
            target.unlink()
            raise OSError("replacement outcome uncertain")

        with patch("storage.messages._publish", side_effect=lost_original), self.assertRaises(ChatError):
            self.gateway.store.update(record["message_key"], expected_version=1,
                                      adp_state="ADP_SEND_ARMED", request_id=str(uuid4()))
        self.gateway.close()
        self.assertFalse(path.exists())
        self.assertTrue(list(path.parent.glob(".message-*.tmp")))
        with self.assertRaises(ChatError):
            MessageStore(self.config)

    async def test_bad_http_streams_are_unknown_once_and_always_closed(self):
        """HTTP状态、断流、畸形JSON/身份和物理大小保护不自动重试或伪造成功。"""
        record = {**self.receive(), "request_id": str(uuid4())}
        responses = [
            httpx.Response(429, content=b"private upstream body"),
            httpx.Response(200, headers={"Content-Type": "application/json"}, content=b"{}"),
            httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=b"data: invalid\n\n"),
            httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=b"data: {}"),
            httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=frame("request_ack", RequestAck={"Status": "success", "ConversationId": "wrong"})),
            httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=frame("response.completed", Response={"Status": "failed"})),
            httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=b":" + b"a" * 17000),
        ]
        for response in responses:
            calls = []

            def handle(request):
                """只计真实传输调用；失败后任何第二次请求都会被断言发现。"""
                calls.append(request)
                return response

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                outcome = await AsyncADPClient(replace(self.config, adp_response_max_bytes=16384), http_client=http).request(record)
            self.assertEqual(outcome.state, "ADP_UNKNOWN")
            self.assertEqual(len(calls), 1)
            self.assertTrue(response.is_closed)
            self.assertNotIn("private", outcome.notification)

    async def test_factory_does_not_enable_tls_keylog_redirects_retry_or_model_connection_quota(self):
        """真正HTTP工厂的参数有安全/并发意义，不能只靠注入Mock证明。"""
        fake = FakeADP()
        transport = httpx.MockTransport(fake.handle)
        with patch("adp.async_client.httpx.AsyncHTTPTransport", return_value=transport) as factory:
            client = AsyncADPClient(self.config)
            options = factory.call_args.kwargs
            self.assertEqual(options["retries"], 0)
            self.assertIsNone(options["limits"].max_connections)
            self.assertTrue(options["verify"].check_hostname)
            self.assertIsNone(options["verify"].keylog_filename)
            self.assertFalse(client.http.follow_redirects)
            self.assertFalse(client.http.trust_env)
            await client.aclose()

    async def test_v1_upgrade_is_compatible_and_preserves_admission_fact(self):
        """B的历史文件先完整校验再升级，不把已接收记录当作新消息。"""
        record = self.receive()
        self.gateway.close()
        path = self.root / "messages" / f"{record['message_key']}.json"
        legacy = {k: v for k, v in record.items() if k not in {"record_version", "adp_result", "adp_error", "redacted", "updated_at"}}
        legacy["schema_version"] = 1
        path.write_text(json.dumps(legacy), encoding="utf-8")
        self.gateway = WeComGateway(self.config, logging.getLogger("wecom-c-test"))
        self.addCleanup(self.gateway.close)
        upgraded = self.gateway.store.get(record["message_key"])
        self.assertEqual((upgraded["schema_version"], upgraded["adp_state"]), (2, "RECEIVED"))
        self.assertEqual(self.receive(), upgraded)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["schema_version"], 2)

    async def test_shutdown_during_disk_commit_reads_back_before_deciding_unknown(self):
        """取消to_thread不停止发布；武装提交变UNKNOWN，完成提交保持确定成功。"""
        for index, boundary in enumerate(("ADP_SEND_ARMED", "ADP_SUCCEEDED"), 1):
            record = self.receive(f"CANCEL{index}", str(index))
            entered, release = Event(), Event()
            original = self.gateway.store._write
            calls = []

            def publish(value):
                """真实提交后暂停磁盘线程，制造取消与返回值交接的精确窗口。"""
                original(value)
                if value["message_key"] == record["message_key"] and value["adp_state"] == boundary:
                    entered.set()
                    if not release.wait(8):
                        raise AssertionError("测试未释放磁盘提交屏障")

            def respond(request):
                """网络调用计数只允许一次，武装交接取消时应为零次。"""
                calls.append(json.loads(request.content))
                return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=answer(calls[-1]))

            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
                worker = GatewayWorker(self.gateway, AsyncADPClient(self.config, http_client=http))
                with patch.object(self.gateway.store, "_write", side_effect=publish):
                    await worker.start()
                    try:
                        await self.wait_for(entered.is_set)
                        task = worker.tasks[record["message_key"]]
                        stopping = asyncio.create_task(worker.stop())
                        await self.wait_for(lambda: task.cancelling() > 0)
                        release.set()
                        await stopping
                    finally:
                        release.set()
                        await worker.stop()
            self.assertIsNone(worker.failure)
            self.assertEqual(len(calls), 0 if boundary == "ADP_SEND_ARMED" else 1)
            self.assertEqual(self.gateway.store.get(record["message_key"])["adp_state"],
                             "ADP_UNKNOWN" if boundary == "ADP_SEND_ARMED" else "ADP_SUCCEEDED")

    async def test_local_snapshot_inspection_is_read_only_and_hides_body(self):
        """本机状态查看沿共享读取，只展示标识/状态且不调用任何网络。"""
        from wecom_callback_client import main

        self.receive()
        with (patch("wecom_callback_client.load_config", return_value=self.config),
              patch("wecom_callback_client.urlopen") as network,
              contextlib.redirect_stdout(io.StringIO()) as output):
            self.assertEqual(main(["--inspect-state"]), 0)
            network.assert_not_called()
        printed = output.getvalue()
        data = json.loads(printed)
        self.assertEqual(data["records"][0]["adp_state"], "RECEIVED")
        self.assertNotIn("测试文字", printed)
        self.assertNotIn(self.config.callback_token, printed)
        self.assertEqual(self.receive()["adp_state"], "RECEIVED")


if __name__ == "__main__":
    unittest.main()
