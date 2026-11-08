"""阶段E：结构化日志独立于消息事实源，真实文件与正式Worker联动。"""

import asyncio
import base64
import json
import logging
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

import httpx

from adp.async_client import AsyncADPClient
from application.wecom_gateway import WeComGateway
from application.wecom_worker import GatewayWorker
from core.config import ChatConfig
from core.events import GatewayEvents
from wecom.models import InboundMessage
from wecom.sender import SimulatedSender
from test_wecom_dispatch import answer, frame


def read_events(root):
    """完整JSONL逐行解析；不把损坏行或业务快照当事件证据。"""
    return [json.loads(line) for path in sorted(root.glob("events-*.jsonl"))
            for line in path.read_text(encoding="utf-8").splitlines()]


class EventFileTests(unittest.TestCase):
    """真实临时目录检验Schema、白名单、轮转与故障告警。"""

    def setUp(self):
        """隔离日志及测试凭据，禁止访问根实际配置。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = ChatConfig("private-test-app-key", "legacy", log_file=str(self.root / "human.log"))

    def test_schema_allowlist_and_sensitive_record_fields(self):
        """即使记录带完整正文/身份/未知属性，也只输出规定的安全元数据。"""
        events = GatewayEvents(self.config)
        self.addCleanup(events.close)
        record = {"message_key": "a" * 64, "conversation_id": "b" * 64,
                  "request_id": "123e4567-e89b-42d3-a456-426614174000", "user_id": "YNND",
                  "content": "绝密问题", "notification": "绝密回答", "corp_id": "private-company"}
        events.emit("adp.completed", record, attributes={"reply_chars": 4, "source": "test_fixture",
                    "body": "绝密正文", "token": self.config.app_key}, provider_trace_id="trace-safe")
        output = read_events(self.root)
        self.assertEqual(len(output), 1)
        event = output[0]
        self.assertEqual(event["schema_version"], "adp_chat.log.v1")
        UUID(event["id"])
        self.assertRegex(event["timestamp"], r"\.\d{3}\+08:00$")
        self.assertEqual(event["environment"], "test")
        self.assertEqual(event["message_key"], record["message_key"])
        self.assertEqual(event["request_id"], record["request_id"])
        self.assertEqual(event["provider_trace_id"], "trace-safe")
        self.assertEqual(event["attributes"], {"reply_chars": 4, "source": "test_fixture"})
        self.assertGreater(event["source_line"], 0)
        self.assertEqual(event["source_file"], "test_gateway_events.py")
        text = json.dumps(output, ensure_ascii=False)
        for private in ("YNND", "绝密", self.config.app_key, "private-company"):
            self.assertNotIn(private, text)

    def test_unknown_fields_and_fake_accepted_never_become_success(self):
        """开放文本、未知事件和模拟接受不能污染真实发送成功事件。"""
        events = GatewayEvents(self.config)
        self.addCleanup(events.close)
        events.emit("arbitrary.private-question", attributes={"source": "test_fixture"})
        events.emit("wecom.send.accepted", attributes={"source": "simulated"})
        events.emit("wecom.send.accepted", attributes={"source": "fake_http"})
        events.emit("wecom.send.simulated", attributes={"source": "simulated", "reply_chars": -1,
                                                       "release_reason": "private-answer"})
        output = read_events(self.root)
        self.assertEqual([event["event"] for event in output], ["wecom.send.simulated"])
        self.assertEqual(output[0]["attributes"], {"source": "simulated"})

    def test_size_date_retention_and_partial_old_tail(self):
        """新进程/日期/大小分段不拼接旧崩溃尾行，清理只触及本Owner过期文件。"""
        old = self.root / "events-20000101-123-aaaaaaaaaaaa-0.jsonl"
        old.write_text('{"partial":', encoding="utf-8")
        unrelated = self.root / "events-personal.jsonl"
        unrelated.write_text("保留", encoding="utf-8")
        config = replace(self.config, log_max_bytes=1000, events_retention_days=14)
        events = GatewayEvents(config)
        for _ in range(4):
            events.emit("service.started")
        paths = list(self.root.glob("events-*.jsonl"))
        self.assertFalse(old.exists())
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "保留")
        self.assertGreaterEqual(len(paths), 3)
        count = sum(len(path.read_text(encoding="utf-8").splitlines()) for path in paths if path != unrelated)
        self.assertEqual(count, 4)
        events.close()
        current = next(path for path in paths if path != unrelated)
        with current.open("a", encoding="utf-8") as output:
            output.write('{"incomplete":')
        later = GatewayEvents(config)
        self.addCleanup(later.close)
        later.emit("service.started")
        self.assertEqual(current.read_text(encoding="utf-8").splitlines()[-1], '{"incomplete":')
        self.assertTrue(any(path != current and path.read_text(encoding="utf-8").endswith("\n")
                            for path in self.root.glob("events-*.jsonl") if path != unrelated))
        tomorrow = datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)
        with patch("logging.time.time_ns", return_value=int(tomorrow.timestamp() * 1_000_000_000)):
            later.emit("service.stopped")
        self.assertTrue(any(tomorrow.strftime("%Y%m%d") in path.name for path in self.root.glob("events-*.jsonl")))

    def test_write_failure_is_observable_but_never_raises_into_business(self):
        """磁盘故障只告警/计数，不把成功业务改判为失败或驱动重试。"""
        events = GatewayEvents(self.config)
        self.addCleanup(events.close)
        with patch("core.events._EventHandler._write", side_effect=OSError("private-secret-path")):
            with self.assertLogs("adp_chat.event_alert", level="ERROR") as alert:
                events.emit("service.started")
        self.assertEqual(events.failed_writes, 1)
        self.assertIn("gateway_jsonl_write_failed", alert.output[0])
        self.assertNotIn("private-secret-path", alert.output[0])
        events.emit("service.stopped")
        self.assertEqual([e["event"] for e in read_events(self.root)], ["service.stopped"])

    def test_disabled_log_creates_no_directory(self):
        """沿用现有日志总开关，不给旧CLI配置增加隐性文件副作用。"""
        target = self.root / "absent" / "human.log"
        events = GatewayEvents(replace(self.config, log_enabled=False, log_file=str(target)))
        events.emit("service.started")
        events.close()
        self.assertFalse(target.parent.exists())

    def test_partial_write_failure_opens_new_segment_without_concatenating_tail(self):
        """同进程短写后也隔离末尾半行；恢复日志不得拼坏下一条完整事件。"""
        events = GatewayEvents(self.config)
        self.addCleanup(events.close)
        events.emit("service.started")
        damaged = events.handler.path

        def partial(_record, content):
            """用真实追加模拟短写后磁盘错误，不模拟业务存储或消息结果。"""
            with damaged.open("ab") as output:
                output.write(content[:20])
            raise OSError("short-write")

        with patch.object(events.handler, "_write", side_effect=partial), self.assertLogs("adp_chat.event_alert", level="ERROR"):
            events.emit("service.started")
        events.emit("service.stopped")
        self.assertNotEqual(events.handler.path, damaged)
        self.assertFalse(damaged.read_bytes().endswith(b"\n"))
        self.assertEqual(json.loads(events.handler.path.read_text(encoding="utf-8"))["event"], "service.stopped")


class EventWorkflowTests(unittest.IsolatedAsyncioTestCase):
    """正式接收/持久化/异步ADP/模拟发送的实际事件，不解析人类日志。"""

    def setUp(self):
        """独立测试作用域与真实临时磁盘；无企微或ADP云端调用。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = ChatConfig("fixture-private-key", "legacy", corp_id="wwf483023b734c570a", agent_id=1000053,
            adp_application="wecom-e-events", gateway_enabled=True, callback_token=os.urandom(12).hex(),
            encoding_aes_key=base64.b64encode(os.urandom(32)).decode()[:-1], notification_mode="simulated",
            messages_dir=str(self.root / "messages"), session_file=str(self.root / "legacy.json"),
            log_file=str(self.root / "logs" / "human.log"))
        self.gateway = WeComGateway(self.config, logging.getLogger("e-human"))
        self.addCleanup(self.gateway.close)

    async def test_lifecycle_busy_duplicate_rejected_unknown_release_and_sender(self):
        """完整消息关联/真实Code/发送证据源均来自同一可靠快照与实际结果。"""
        release = asyncio.Event()
        calls = []

        async def handle(request):
            """第一轮等候以产生BUSY；另两用户明确拒绝或断流。"""
            payload = json.loads(request.content)
            calls.append(payload)
            if payload["UserId"] == "YNND":
                await release.wait()
                data = answer(payload, text="仅磁盘保存的私密回答")
            elif payload["UserId"] == "SIYU":
                data = frame("error", Error={"Code": 460031, "Message": "不可记录的供应商正文"})
            else:
                data = frame("request_ack", RequestAck={"Status": "success"})
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=data)

        def receive(user, msg):
            """正式原子接收，不替换状态转换或验收证据。"""
            return self.gateway.receive(InboundMessage(self.config.corp_id, self.config.agent_id, user, msg,
                                                       "不可记录的用户正文", 1))

        async def wait_for(predicate):
            """等待具体终态，超时保留失败。"""
            async with asyncio.timeout(8):
                while not predicate():
                    await asyncio.sleep(0.005)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            worker = GatewayWorker(self.gateway, AsyncADPClient(self.config, http_client=http),
                                   sender=SimulatedSender(self.config))
            await worker.start()
            try:
                first = receive("YNND", "1")
                await wait_for(lambda: len(calls) == 1)
                busy = receive("YNND", "2")
                self.assertEqual(busy["adp_state"], "REJECTED_USER_BUSY")
                receive("YNND", "2")
                second = receive("SIYU", "3")
                third = receive("EMP2", "4")
                release.set()
                records = [first, busy, second, third]
                await wait_for(lambda: all(self.gateway.store.get(r["message_key"])["send_state"] == "SEND_SIMULATED"
                                          for r in records))
            finally:
                await worker.stop()
        events = read_events(self.root / "logs")
        names = {e["event"] for e in events}
        self.assertTrue({"wecom.message.received", "wecom.message.persisted", "wecom.message.duplicate",
                         "wecom.message.busy_rejected", "adp.submit_armed", "adp.started", "adp.completed",
                         "adp.rejected", "adp.unknown", "message.session.released", "reply.persisted",
                         "wecom.send.started", "wecom.send.simulated"} <= names)
        self.assertNotIn("wecom.send.accepted", names)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len({e["id"] for e in events}), len(events))
        for saved in (self.gateway.store.get(r["message_key"]) for r in records):
            linked = [e for e in events if e["message_key"] == saved["message_key"]]
            self.assertTrue(linked)
            self.assertTrue(all(e["conversation_id"] == saved["conversation_id"] for e in linked))
        reject = next(e for e in events if e["event"] == "adp.rejected")
        self.assertEqual(reject["error_code"], "460031")
        unknown = next(e for e in events if e["event"] == "adp.unknown")
        self.assertTrue(unknown["outcome_uncertain"])
        raw = json.dumps(events, ensure_ascii=False)
        for value in ("YNND", "SIYU", "不可记录", "私密回答", self.config.app_key, self.config.callback_token):
            self.assertNotIn(value, raw)

    async def test_log_disk_failure_never_changes_adp_or_notification_execution(self):
        """整个正式工作流日志磁盘失败时仍提交一次结果/释放/模拟发送，不重投。"""
        calls = []

        async def handle(request):
            """单次Fake回答供核对真实调用次数，而非预设成功状态。"""
            payload = json.loads(request.content)
            calls.append(payload)
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, content=answer(payload))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            worker = GatewayWorker(self.gateway, AsyncADPClient(self.config, http_client=http),
                                   sender=SimulatedSender(self.config))
            with patch("core.events._EventHandler._write", side_effect=PermissionError("private")):
                with self.assertLogs("adp_chat.event_alert", level="ERROR"):
                    await worker.start()
                    try:
                        message = InboundMessage(self.config.corp_id, self.config.agent_id, "YNND", "10", "正文", 1)
                        first = self.gateway.receive(message)
                        async with asyncio.timeout(8):
                            while self.gateway.store.get(first["message_key"])["send_state"] != "SEND_SIMULATED":
                                await asyncio.sleep(0.005)
                        self.gateway.receive(message)
                        self.assertEqual(len(calls), 1)
                        self.assertIsNone(worker.failure)
                        self.assertEqual(self.gateway.store.get(first["message_key"])["adp_state"], "ADP_SUCCEEDED")
                        next_message = self.gateway.receive(InboundMessage(self.config.corp_id, self.config.agent_id,
                                                                           "YNND", "11", "下轮", 1))
                        self.assertEqual(next_message["adp_state"], "RECEIVED")
                        self.assertGreater(self.gateway.events.failed_writes, 5)
                    finally:
                        await worker.stop()


class EventCallbackTests(unittest.TestCase):
    """正式HTTP认证拒绝和服务生命周期也输出同一事件结构。"""

    def test_callback_rejection_and_service_lifecycle(self):
        """无效签名不落消息，不记录查询串/密文，正常退出记录停止。"""
        from fastapi.testclient import TestClient
        from api.app import create_app
        from test_wecom_callback import PausedWorker

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = ChatConfig("fake-only-key", "legacy", corp_id="wwf483023b734c570a", agent_id=1000053,
                adp_application="e-callback", gateway_enabled=True, callback_token=os.urandom(12).hex(),
                encoding_aes_key=base64.b64encode(os.urandom(32)).decode()[:-1],
                log_file=str(root / "logs" / "human.log"), messages_dir=str(root / "messages"),
                session_file=str(root / "legacy.json"))
            with patch("api.app.load_config", return_value=config), patch("api.app.GatewayWorker", PausedWorker):
                with TestClient(create_app()) as client:
                    response = client.post("/wecom/callback?timestamp=1&nonce=n&msg_signature=private-signature",
                                           content=b"private-ciphertext")
                    self.assertEqual(response.status_code, 403)
                    self.assertFalse(list((root / "messages").glob("*.json")))
            events = read_events(root / "logs")
            self.assertEqual([e["event"] for e in events],
                             ["service.started", "wecom.callback.rejected", "service.stopped"])
            self.assertTrue(all(e["message_key"] is None for e in events))
            self.assertNotIn("private-", json.dumps(events))
