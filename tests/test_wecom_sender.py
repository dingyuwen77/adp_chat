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
from dataclasses import asdict
from pathlib import Path
from threading import Event
from unittest.mock import patch

import httpx

from adp.async_client import ADPOutcome, unknown_outcome
from application.wecom_gateway import WeComGateway
from application.wecom_worker import GatewayWorker
from core.config import ChatConfig
from core.models import ChatError, ChatResult
from wecom.models import InboundMessage
from wecom.sender import SendOutcome, SimulatedSender, WeComSender, create_sender


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
        """准备协议消费者所需字段；持久化闭环由下方真实Worker测试证明。"""
        return {"user_id": user, "notification": text}

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
                 ({"errcode": 0, "invaliduser": []}, "SEND_UNKNOWN"),
                 ({"errcode": 0, "invaliduser": " ynnd"}, "SEND_UNKNOWN")]
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
            await self.wait_for(lambda: len(notices) == 2
                                and self.gateway.store.get(first["message_key"])["adp_state"] == "ADP_SUCCEEDED")
            self.assertEqual(self.receive(msg="4")["adp_state"], "RECEIVED")
            await self.wait_for(lambda: len(adp_calls) == 3)
            self.assertEqual(len(notices), 2)
            release.set()
            await self.wait_for(lambda: len(notices) == 4)
            self.assertEqual(set((user, kind) for user, kind, _ in notices), {
                ("YNND", "USER_BUSY"), ("YNND", "ADP_SUCCEEDED"), ("SIYU", "ADP_REJECTED"), ("YNND", "ADP_UNKNOWN")})
            self.assertEqual(len(adp_calls), 3)
            await self.wait_for(lambda: self.gateway.store.get(first["message_key"])["send_state"] == "SEND_SIMULATED")
            self.assertEqual(self.gateway.store.get(busy["message_key"])["adp_state"], "REJECTED_USER_BUSY")
            self.assertEqual(self.gateway.store.get(third["message_key"])["adp_state"], "ADP_REJECTED")
        finally:
            release.set()
            await worker.stop()

    async def test_armed_recovery_never_resends_and_pending_recovers_first_send(self):
        """有确定ADP结果的两条通知，只恢复未开始的一条，不再调ADP。"""
        records = [self.receive("YNND", "1"), self.receive("SIYU", "2")]

        class ADP:
            """仅准备两条已完成Fake结果。"""

            async def request(inner, record):
                """每条一次，恢复阶段替换成禁止ADP以证明不重调。"""
                result = ChatResult("独立模拟回答", record["conversation_id"], record["request_id"])
                return ADPOutcome("ADP_SUCCEEDED", result.text, result=result)

        preparing = GatewayWorker(self.gateway, ADP())
        for record in records:
            await preparing._execute(record)
        first = self.gateway.store.get(records[0]["message_key"])
        plan = SendOutcome("SEND_ARMED", "simulated", None, None, 1, "armed")
        self.gateway.store.update(first["message_key"], expected_version=first["record_version"],
                                  send_state="SEND_ARMED", send_result=asdict(plan))
        self.gateway.close()
        self.gateway = WeComGateway(self.config, logging.getLogger("wecom-d-test"))
        self.addCleanup(self.gateway.close)
        sent = []

        class ForbiddenADP:
            """恢复已完成结果不得穿过ADP边界。"""

            async def request(inner, record):
                """即使调用被Worker异常捕获，后续也断言没有调用。"""
                sent.append("unexpected-adp")
                raise AssertionError("不得重调ADP")

        class Sender(SimulatedSender):
            """在真实发送入口记录模拟目标。"""

            async def send(inner, record):
                """只允许第二条未武装通知进入。"""
                sent.append(record["user_id"])
                return await super().send(record)

        worker = GatewayWorker(self.gateway, ForbiddenADP(), sender=Sender(self.config))
        await worker.start()
        try:
            await self.wait_for(lambda: self.gateway.store.get(records[1]["message_key"])["send_state"] == "SEND_SIMULATED")
            self.assertEqual(sent, ["SIYU"])
            unknown = self.gateway.store.get(first["message_key"])
            self.assertEqual((unknown["adp_state"], unknown["send_state"]), ("ADP_SUCCEEDED", "SEND_UNKNOWN"))
            self.assertEqual(self.receive(msg="3")["adp_state"], "RECEIVED")
        finally:
            await worker.stop()

    async def test_failed_and_unknown_send_never_hold_user_or_repeat_adp(self):
        """企业微信故障只更新send维度，保存的ADP结果保持确定成功。"""
        for index, state in enumerate(("SEND_FAILED", "SEND_UNKNOWN"), 1):
            record = self.receive(f"EMP{index}", str(index))
            calls = []

            class ADP:
                """仅计数一次获准ADP。"""

                async def request(inner, current):
                    """正式结果由同Worker保存。"""
                    calls.append((current["msg_id"], current["request_id"]))
                    result = ChatResult("模拟回答", current["conversation_id"], current["request_id"])
                    return ADPOutcome("ADP_SUCCEEDED", result.text, result=result)

            class Sender:
                """模拟HTTP明确失败或响应未知。"""

                source = "fake_http"

                async def send(inner, current):
                    """返回一次结果，不附带第二条通知意图。"""
                    return SendOutcome(state, "fake_http", False if state == "SEND_FAILED" else None,
                                       0, 1, "fixture_error")

            worker = GatewayWorker(self.gateway, ADP(), sender=Sender())
            await worker.start()
            try:
                await self.wait_for(lambda: self.gateway.store.get(record["message_key"])["send_state"] == state)
                self.assertEqual(self.gateway.store.get(record["message_key"])["adp_state"], "ADP_SUCCEEDED")
                self.assertEqual(sum(msg == str(index) for msg, _ in calls), 1)
                self.assertEqual(len({request for _, request in calls}), len(calls))
                self.assertEqual(self.receive(f"EMP{index}", str(100 + index))["adp_state"], "RECEIVED")
                self.assertIsNone(worker.failure)
            finally:
                await worker.stop()

    async def test_test_environment_cannot_enable_native_send_or_forge_acceptance(self):
        """配置和快照共同防止测试成功误变成真实平台接受。"""
        self.assertIsNone(create_sender(replace(self.config, notification_mode="disabled")))
        with self.assertRaises(ChatError):
            WeComSender(replace(self.config, wecom_app_secret=self.secret))
        with self.assertRaises(ChatError):
            replace(self.config, gateway_environment="production")
        original = self.receive()
        busy = self.receive(msg="2")
        forged = SendOutcome("SEND_ACCEPTED", "fake_http", True, 1, 1, "accepted")
        self.gateway.store.update(busy["message_key"], expected_version=1,
            send_state="SEND_ARMED", send_result=asdict(SendOutcome("SEND_ARMED", "simulated", None, None, 1, "armed")))
        current = self.gateway.store.get(busy["message_key"])
        self.assertEqual(current["send_state"], "SEND_ARMED")
        with self.assertRaises(ChatError):
            self.gateway.store.update(current["message_key"], expected_version=current["record_version"],
                                      send_state="SEND_ACCEPTED", send_result=asdict(forged))
        self.assertEqual(self.gateway.store.get(original["message_key"])["adp_state"], "RECEIVED")

    def busy_notice(self):
        """接收Owner生成真实PENDING提示，避免测试自行拼业务终态。"""
        self.receive()
        return self.receive(msg="2")

    async def test_arm_failure_prevents_sender_and_result_failure_never_resends(self):
        """发送前武装失败不出站；出站结果保存失败重启未知且不重复发送。"""
        pending = self.busy_notice()
        calls = []

        class Sender(SimulatedSender):
            """把独立模拟发送作为可观察副作用边界。"""

            async def send(inner, current):
                """调用瞬间重读真实文件，必须已经可靠武装。"""
                from storage.messages import read_snapshot

                path = Path(self.config.messages_dir) / f"{current['message_key']}.json"
                self.assertEqual(read_snapshot(path)["send_state"], "SEND_ARMED")
                calls.append(current["message_key"])
                return await super().send(current)

        worker = GatewayWorker(self.gateway, None, sender=Sender(self.config))
        with patch("storage.messages._publish", side_effect=OSError("disk failure")):
            await worker._execute_notification(pending)
        self.assertEqual(calls, [])
        self.assertIsNotNone(worker.failure)
        self.gateway.close()
        self.gateway = WeComGateway(self.config, logging.getLogger("wecom-d-test"))
        self.addCleanup(self.gateway.close)
        pending = self.gateway.store.get(pending["message_key"])
        self.assertEqual(pending["send_state"], "PENDING")
        original = self.gateway.store._write

        def fail_result(value):
            """武装发布正常，只破坏模拟发送完成后的终态发布。"""
            if value["send_state"] == "SEND_SIMULATED":
                with patch("storage.messages._publish", side_effect=OSError("disk failure")):
                    return original(value)
            return original(value)

        worker = GatewayWorker(self.gateway, None, sender=Sender(self.config))
        with patch.object(self.gateway.store, "_write", side_effect=fail_result):
            await worker._execute_notification(pending)
        self.assertEqual(calls, [pending["message_key"]])
        self.assertEqual(self.gateway.store.get(pending["message_key"])["send_state"], "SEND_ARMED")
        self.gateway.close()
        self.gateway = WeComGateway(self.config, logging.getLogger("wecom-d-test"))
        self.addCleanup(self.gateway.close)
        worker = GatewayWorker(self.gateway, None, sender=Sender(self.config))
        # 保留已获准但尚未执行的消息，以挂起FakeADP只观察旧通知恢复，不篡改快照。
        class HoldingADP:
            """第一条获准消息尚未开始，允许首次请求但与通知重发无关。"""

            async def request(inner, current):
                """一直等待，关闭时由Worker可靠转换UNKNOWN。"""
                await asyncio.Event().wait()

        worker.adp = HoldingADP()
        await worker.start()
        try:
            self.assertEqual(self.gateway.store.get(pending["message_key"])["send_state"], "SEND_UNKNOWN")
            self.assertEqual(calls, [pending["message_key"]])
        finally:
            await worker.stop()

    async def test_cancellation_during_notification_commit_reads_back_terminal_state(self):
        """取消磁盘线程交接：未出站武装保守未知，已保存模拟完成不能覆盖成未知。"""
        for index, boundary in enumerate(("SEND_ARMED", "SEND_SIMULATED"), 1):
            self.receive(f"EMP{index}", str(index * 10))
            notice = self.receive(f"EMP{index}", str(index * 10 + 1))
            entered, release = Event(), Event()
            original = self.gateway.store._write
            sends = []

            def publish(value):
                """真实原子发布后暂停，让取消发生在await结果返回之前。"""
                original(value)
                if value["message_key"] == notice["message_key"] and value["send_state"] == boundary:
                    entered.set()
                    if not release.wait(8):
                        raise AssertionError("未释放测试磁盘屏障")

            class Sender(SimulatedSender):
                """只记录是否越过发送边界。"""

                async def send(inner, current):
                    """返回模拟完成，不能产生真实平台接受。"""
                    sends.append(current["message_key"])
                    return await super().send(current)

            worker = GatewayWorker(self.gateway, None, sender=Sender(self.config))
            with patch.object(self.gateway.store, "_write", side_effect=publish):
                task = asyncio.create_task(worker._execute_notification(notice))
                try:
                    await self.wait_for(entered.is_set)
                    task.cancel()
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
                finally:
                    release.set()
            self.assertIsNone(worker.failure)
            self.assertEqual(len(sends), 0 if boundary == "SEND_ARMED" else 1)
            self.assertEqual(self.gateway.store.get(notice["message_key"])["send_state"],
                             "SEND_UNKNOWN" if boundary == "SEND_ARMED" else "SEND_SIMULATED")

    async def test_refresh_network_failure_after_explicit_token_rejection_is_known_unsent(self):
        """消息明确42001未接受，随后Token GET故障不应伪装消息可能已发送。"""
        gets, posts = [], []

        def handle(request):
            """首次获取有效形式Token，消息过期拒绝后刷新连接失败。"""
            if request.method == "GET":
                gets.append(request)
                if len(gets) > 1:
                    raise httpx.ConnectError("private credential context")
                return httpx.Response(200, json={"errcode": 0, "access_token": self.token, "expires_in": 7200})
            posts.append(request)
            return httpx.Response(200, json={"errcode": 42001})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            sender = WeComSender(replace(self.config, wecom_app_secret=self.secret), http_client=http)
            try:
                outcome = await sender.send(self.pending())
            finally:
                await sender.aclose()
        self.assertEqual((len(gets), len(posts)), (2, 1))
        self.assertEqual((outcome.state, outcome.accepted_parts), ("SEND_FAILED", 0))
        self.assertEqual(outcome.provider_code, 42001)

    async def test_concurrent_expiration_uses_one_refresh_and_bounded_resends(self):
        """多员工共享旧Token失效时缓存按应用刷新，不形成刷新风暴。"""
        gets, posts = [], []

        async def handle(request):
            """异步交接制造并发，平台只拒绝旧Token，不拒绝新Token。"""
            await asyncio.sleep(0)
            if request.method == "GET":
                gets.append(request)
                return httpx.Response(200, json={"errcode": 0, "access_token": self.token + str(len(gets)), "expires_in": 7200})
            posts.append(request)
            return httpx.Response(200, json={"errcode": 42001 if request.url.params["access_token"] == self.token + "1" else 0})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            sender = WeComSender(replace(self.config, wecom_app_secret=self.secret), http_client=http)
            try:
                outcomes = await asyncio.gather(*(sender.send(self.pending(f"EMP{i}")) for i in range(20)))
            finally:
                await sender.aclose()
        self.assertEqual(len(gets), 2)
        self.assertTrue(20 <= len(posts) <= 40)
        self.assertTrue(all(x.state == "SEND_SIMULATED" and x.accepted_parts == 1 for x in outcomes))

    async def test_v2_upgrade_and_environment_guard_keep_pending_evidence(self):
        """C的PENDING兼容升级，但测试状态目录不能在production下静默发送。"""
        from storage.messages import MessageStore, read_snapshot

        pending = self.busy_notice()
        self.gateway.close()
        path = Path(self.config.messages_dir) / f"{pending['message_key']}.json"
        old = {k: v for k, v in read_snapshot(path).items() if k not in {"environment", "send_result"}}
        old["schema_version"] = 2
        path.write_text(json.dumps(old), encoding="utf-8")
        production = replace(self.config, gateway_environment="production", notification_mode="disabled")
        with self.assertRaises(ChatError):
            MessageStore(production)
        self.assertEqual(read_snapshot(path)["schema_version"], 2)
        self.gateway = WeComGateway(self.config, logging.getLogger("wecom-d-test"))
        self.addCleanup(self.gateway.close)
        upgraded = self.gateway.store.get(pending["message_key"])
        self.assertEqual((upgraded["schema_version"], upgraded["environment"], upgraded["send_state"]), (3, "test", "PENDING"))
        self.assertEqual(upgraded["notification"], pending["notification"])
        self.gateway.close()
        with self.assertRaises(ChatError):
            MessageStore(production)

    async def test_native_factory_validates_tls_and_disables_redirects_and_retries(self):
        """只构造/关闭原生工厂不发送；检验真实F路径的证书与零传输重试参数。"""
        transport = httpx.MockTransport(lambda request: (_ for _ in ()).throw(AssertionError("不得发送")))
        config = replace(self.config, gateway_environment="production", notification_mode="wecom", wecom_app_secret=self.secret)
        with patch("wecom.sender.httpx.AsyncHTTPTransport", return_value=transport) as factory:
            sender = WeComSender(config)
            options = factory.call_args.kwargs
            self.assertTrue(options["verify"].check_hostname)
            self.assertIsNone(options["verify"].keylog_filename)
            self.assertEqual(options["retries"], 0)
            self.assertFalse(sender.http.follow_redirects)
            self.assertFalse(sender.http.trust_env)
            await sender.aclose()

    async def test_same_employee_notifications_do_not_interleave_and_other_employee_progresses(self):
        """屏障保持首条回复未结束；同员工第二条须等待，另一员工仍可完成。"""
        class ADP:
            """使用正式 Worker 保存可发送结果，释放员工的 ADP 占用。"""

            async def request(inner, record):
                """不调用真实供应方。"""
                result = ChatResult("有序回答", record["conversation_id"], record["request_id"])
                return ADPOutcome("ADP_SUCCEEDED", result.text, result=result)

        preparing = GatewayWorker(self.gateway, ADP())
        first = self.receive(msg="1001")
        await preparing._execute(first)
        second = self.receive(msg="1002")
        await preparing._execute(second)
        other = self.receive("SIYU", "1003")
        await preparing._execute(other)
        records = [self.gateway.store.get(r["message_key"]) for r in (first, second, other)]
        entered, release, other_done = asyncio.Event(), asyncio.Event(), asyncio.Event()
        order = []

        class Sender(SimulatedSender):
            """通过可控调度观察完整通知的开始与结束。"""

            async def send(inner, record):
                """首条挂起直到另一员工完成，禁止同员工插队。"""
                order.append((record["msg_id"], "start"))
                if record["msg_id"] == "1001":
                    entered.set()
                    await release.wait()
                result = await super().send(record)
                order.append((record["msg_id"], "end"))
                if record["msg_id"] == "1003":
                    other_done.set()
                return result

        worker = GatewayWorker(self.gateway, ADP(), sender=Sender(self.config))
        tasks = [asyncio.create_task(worker._execute_notification(records[0]))]
        try:
            await asyncio.wait_for(entered.wait(), 2)
            tasks.extend(asyncio.create_task(worker._execute_notification(r)) for r in records[1:])
            await asyncio.wait_for(other_done.wait(), 2)
            self.assertNotIn(("1002", "start"), order)
            self.assertEqual(self.gateway.store.get(second["message_key"])["send_state"], "PENDING")
        finally:
            release.set()
            await asyncio.gather(*tasks)
        self.assertLess(order.index(("1001", "end")), order.index(("1002", "start")))

    async def test_reserved_or_multiple_targets_never_reach_http(self):
        """单员工契约禁止@all/多人列表，连Token请求也不应发出。"""
        calls = []

        def handle(request):
            """Fake响应不能让非法群发目标混成成功。"""
            calls.append(request)
            return httpx.Response(200, json={"errcode": 0, "access_token": self.token, "expires_in": 7200})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            sender = WeComSender(replace(self.config, wecom_app_secret=self.secret), http_client=http)
            try:
                for target in ("@all", "@ALL", "YNND|SIYU", "_employee", "a" * 65, None):
                    outcome = await sender.send(self.pending(target))
                    self.assertEqual(outcome.state, "SEND_FAILED")
            finally:
                await sender.aclose()
        self.assertEqual(calls, [])

    async def test_invalid_user_comparison_respects_official_case_insensitive_ids(self):
        """官方账号唯一性忽略大小写；不同大小写的无效目标也不能当成接受。"""
        def handle(request):
            """有效形式Token后返回errcode0但该员工无效。"""
            return httpx.Response(200, json={"errcode": 0, "access_token": self.token, "expires_in": 7200}
                                  if request.method == "GET" else {"errcode": 0, "invaliduser": "ynnd"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            sender = WeComSender(replace(self.config, wecom_app_secret=self.secret), http_client=http)
            try:
                outcome = await sender.send(self.pending("YNND"))
            finally:
                await sender.aclose()
        self.assertEqual(outcome.state, "SEND_FAILED")


if __name__ == "__main__":
    unittest.main()
