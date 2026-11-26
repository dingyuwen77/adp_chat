"""通过正式 HTTP 诊断接口验证应用接线，用替身隔离远端副作用。"""

import base64
import json
import socket
import threading
import time
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from api.app import create_app
from core.config import ChatConfig, load_config
from storage.sqlite_store import read_messages
from wecom.crypto import WeComCrypto


_PATH = "/api/internal/diagnostics/check"
_TOKEN = "diagnostics-fixture-token-32-chars"


class DiagnosticsTests(unittest.TestCase):
    """使用实际配置加载和 FastAPI 生命周期。"""

    def test_configured_endpoint_requires_operator_token(self):
        """已启用诊断时，匿名调用在尝试上游之前被拒绝。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[adp]\napp_key="fixture"\nuser_id="staff"\n'
                '[logging]\nlog_enabled=false\n'
                f'[diagnostics]\ndiagnostic_token="{_TOKEN}"\n',
                encoding="utf-8",
            )
            with patch("api.app.load_config", side_effect=lambda: load_config(path)):
                with TestClient(create_app()) as client:
                    response = client.post(_PATH, json={"target": "adp"})
                    self.assertEqual(response.status_code, 401, response.text)
                    self.assertEqual(client.get("/healthz").json(), {"status": "ok"})

    def setUp(self):
        """隔离会话、数据库和远端 HTTP 请求。"""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.config = ChatConfig(
            "production-placeholder-key", "staff",
            session_file=str(root / "session.json"), log_file=str(root / "diagnostic.log"),
            database_file=str(root / "gateway.sqlite3"), log_answer=False, log_question=False,
            diagnostic_token=_TOKEN, diagnostic_adp_app_key="probe-placeholder-key",
            diagnostic_adp_user_id="adp-probe", diagnostic_wecom_user_id="wecom-probe",
            max_active_requests=4, gateway_enabled=True, notification_mode="wecom",
            gateway_environment="production", corp_id="fixturecorp", agent_id=120,
            adp_application="fixture", wecom_app_secret="wecom-secret-placeholder",
            callback_token="testcallbacktoken", encoding_aes_key=base64.b64encode(b"1" * 32).decode()[:-1],
        )
        self.adp_mode = "complete"
        self.wecom_mode = "accepted"
        self.adp_requests = []
        self.wecom_requests = []
        self.sending = threading.Event()
        self.auth = {"Authorization": "Bearer " + _TOKEN}

    def _upstream(self, request):
        """在 HTTP 传输边界返回厂商协议结构，不替换解析器。"""
        if request.url.host == "wss.lke.cloud.tencent.com":
            payload = json.loads(request.content)
            self.adp_requests.append(payload)
            if self.adp_mode == "dns":
                raise httpx.ConnectError("私密底层错误") from socket.gaierror()
            cid = payload["ConversationId"]
            events = [{"Type": "request_ack", "RequestAck": {"ConversationId": cid, "Status": "success"}}]
            if self.adp_mode == "complete":
                events.append({"Type": "response.completed", "Response": {
                    "ConversationId": cid, "Status": "success",
                    "Messages": [{"Type": "reply", "Contents": [{"Type": "text", "Text": "fixture-only"}]}],
                }})
            return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                                  text="".join(f"data: {json.dumps(event)}\n\n" for event in events))
        if request.url.path == "/cgi-bin/gettoken":
            if self.wecom_mode == "token_rejected":
                return httpx.Response(200, json={"errcode": 40001})
            return httpx.Response(200, json={"errcode": 0, "access_token": "fixture-token", "expires_in": 7200})
        self.assertEqual(request.url.path, "/cgi-bin/message/send")
        self.wecom_requests.append(json.loads(request.content))
        self.sending.set()
        if self.wecom_mode == "read_timeout":
            raise httpx.ReadTimeout("出站响应丢失")
        return httpx.Response(200, json={"errcode": 0})

    def _client(self, config=None):
        """实际启动 FastAPI 和正式出站实现，只替换底层传输。"""
        self.enterContext(patch("api.app.load_config", return_value=config or self.config))
        self.enterContext(patch("httpx.AsyncHTTPTransport", side_effect=lambda **_: httpx.MockTransport(self._upstream)))
        return self.enterContext(TestClient(create_app()))

    def _callback(self, client, code, *, user="wecom-probe", valid=True):
        """通过真实回调路径提交已加密的企微消息。"""
        config = self.config
        crypto = WeComCrypto(config.callback_token, config.encoding_aes_key, config.corp_id)
        timestamp = str(int(time.time()))
        raw = (f"<xml><ToUserName>{config.corp_id}</ToUserName><FromUserName>{user}</FromUserName>"
               f"<AgentID>{config.agent_id}</AgentID><MsgType>text</MsgType><MsgId>700</MsgId>"
               f"<CreateTime>{timestamp}</CreateTime><Content>{code}</Content></xml>").encode()
        cipher = crypto.encrypt(raw)
        signature = crypto.signature(cipher, timestamp, "test") if valid else "0" * 40
        outer = (f"<xml><ToUserName>{config.corp_id}</ToUserName><AgentID>{config.agent_id}</AgentID>"
                 f"<Encrypt>{cipher}</Encrypt></xml>")
        return client.post("/wecom/callback", params={"timestamp": timestamp, "nonce": "test",
                           "msg_signature": signature}, content=outer)

    def test_disabled_and_invalid_check_has_no_outbound_side_effect(self):
        """旧配置兼容，禁用后不会访问渠道。"""
        client = self._client(replace(self.config, diagnostic_token=""))
        self.assertEqual(client.post(_PATH, json={"target": "wecom"}, headers=self.auth).status_code, 404)
        self.assertEqual(self.wecom_requests, [])
        self.assertEqual(self.adp_requests, [])

    def test_adp_checks_actual_sse_and_isolated_identity(self):
        """有正式完成事件才通过，收到确认后的截断保留失败边界。"""
        client = self._client()
        invalid = client.post(_PATH, json={"target": "adp", "message": "caller input"}, headers=self.auth)
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(self.adp_requests, [])
        response = client.post(_PATH, json={"target": "adp"}, headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["status"], result["last_ok"]), ("passed", "adp_reply_parsed"))
        payload = self.adp_requests[0]
        self.assertEqual(payload["RequestId"], result["request_id"])
        self.assertEqual((payload["UserId"], payload["AppKey"]), ("adp-probe", "probe-placeholder-key"))
        self.assertNotIn("fixture-only", response.text)
        self.adp_mode = "incomplete"
        result = client.post(_PATH, json={"target": "adp"}, headers=self.auth).json()
        self.assertEqual((result["status"], result["last_ok"], result["failed_stage"], result["reason"]),
                         ("unknown", "adp_ack", "adp_sse", "incomplete"))
        self.assertNotEqual(payload["ConversationId"], self.adp_requests[-1]["ConversationId"])

    def test_adp_dns_failure_is_observed_without_leaking_exception(self):
        """连接异常中的 DNS 证据和安全排查方向返回给运维。"""
        self.adp_mode = "dns"
        response = self._client().post(_PATH, json={"target": "adp"}, headers=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["last_ok"], result["failed_stage"], result["reason"]),
                         ("adp_request_prepared", "adp_connect_or_response", "dns_error"))
        self.assertIn("DNS", result["hint"])
        self.assertNotIn("私密底层错误", response.text)

    def test_wecom_authenticated_reply_completes_without_adp_or_business_record(self):
        """回发需经过验签，探测与业务存储分离并拒绝重复主动发送。"""
        self.enterContext(patch("application.diagnostics._WECOM_REPLY_SECONDS", 10))
        client = self._client()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(client.post, _PATH, json={"target": "wecom"}, headers=self.auth)
            self.assertTrue(self.sending.wait(5))
            duplicate = client.post(_PATH, json={"target": "wecom"}, headers=self.auth)
            self.assertEqual(duplicate.status_code, 409)
            sent = self.wecom_requests[0]
            self.assertEqual(sent["touser"], "wecom-probe")
            code = sent["text"]["content"]
            self.assertEqual(self._callback(client, code, valid=False).status_code, 403)
            self.assertEqual(self._callback(client, code).status_code, 200)
            result = future.result(timeout=5).json()
        self.assertEqual((result["status"], result["send_status"], result["receive_status"]),
                         ("passed", "passed", "passed"))
        self.assertEqual(result["last_ok"], "wecom_callback_verified")
        self.assertEqual(self._callback(client, code).status_code, 200)
        self.assertEqual(read_messages(self.config), [])
        self.assertEqual(self.adp_requests, [])
        self.assertEqual(len(self.wecom_requests), 1)

    def test_wecom_platform_acceptance_with_no_reply_leaves_receive_unverified(self):
        """平台接受和实际回发分开验收。"""
        with patch("application.diagnostics._WECOM_REPLY_SECONDS", 0):
            result = self._client().post(_PATH, json={"target": "wecom"}, headers=self.auth).json()
        self.assertEqual((result["status"], result["send_status"], result["receive_status"]),
                         ("unverified", "passed", "unverified"))
        self.assertEqual((result["failed_stage"], result["reason"]), ("wecom_callback", "reply_not_observed"))

    def test_wecom_token_rejection_and_lost_send_response_are_distinct(self):
        """已提交但未见响应的发送与 Token 拒绝具有不同边界。"""
        client = self._client()
        self.wecom_mode = "token_rejected"
        result = client.post(_PATH, json={"target": "wecom"}, headers=self.auth).json()
        self.assertEqual((result["failed_stage"], result["send_status"], result["provider_code"]),
                         ("wecom_token", "failed", 40001))
        self.assertEqual(self.wecom_requests, [])
        self.wecom_mode = "read_timeout"
        result = client.post(_PATH, json={"target": "wecom"}, headers=self.auth).json()
        self.assertEqual((result["failed_stage"], result["send_status"], result["reason"]),
                         ("wecom_send", "unknown", "read_timeout"))
        self.assertEqual(len(self.wecom_requests), 1)
