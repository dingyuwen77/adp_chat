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

    def test_explicit_enable_serves_http_and_https_without_token(self):
        """启用后，域名 HTTPS 和内网 IP HTTP 都无需附带令牌。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[adp]\napp_key="fixture"\nuser_id="staff"\n'
                '[logging]\nlog_enabled=false\n'
                '[diagnostics]\ndiagnostics_enabled=true\n',
                encoding="utf-8",
            )
            with patch("api.app.load_config", side_effect=lambda: load_config(path)):
                for url in ("https://adp-admin.example.com", "http://10.102.70.82:3000"):
                    with self.subTest(url=url), TestClient(create_app(), base_url=url) as client:
                        response = client.post(_PATH, json={"target": "adp"})
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertEqual(response.json()["reason"], "adp_unconfigured")
                        self.assertEqual(client.get("/healthz").json(), {"status": "ok"})

    def test_legacy_diagnostic_fields_keep_upgrade_disabled(self):
        """旧凭据和收件人配置可加载，升级后必须显式启用诊断。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[adp]\napp_key="fixture"\nuser_id="staff"\n'
                '[logging]\nlog_enabled=false\n'
                f'[diagnostics]\ndiagnostic_token="{_TOKEN}"\n'
                'diagnostic_adp_app_key="old-app"\ndiagnostic_wecom_user_id="old-recipient"\n',
                encoding="utf-8",
            )
            with patch("api.app.load_config", side_effect=lambda: load_config(path)):
                with TestClient(create_app()) as client:
                    response = client.post(_PATH, json={"target": "adp"}, headers={"Authorization": "Bearer " + _TOKEN})
                    self.assertEqual(response.status_code, 404, response.text)
                    self.assertEqual(client.get("/healthz").json(), {"status": "ok"})

    def setUp(self):
        """隔离会话、数据库和远端 HTTP 请求。"""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.config = ChatConfig(
            "production-placeholder-key", "staff", visitor_id="staff-visitor",
            session_file=str(root / "session.json"), log_file=str(root / "diagnostic.log"),
            database_file=str(root / "gateway.sqlite3"), log_answer=False, log_question=False,
            diagnostics_enabled=True, diagnostic_adp_user_id="adp-probe",
            max_active_requests=4, gateway_enabled=True, notification_mode="wecom",
            gateway_environment="production", corp_id="fixturecorp", agent_id=120,
            adp_application="fixture", wecom_app_secret="wecom-secret-placeholder",
            callback_token="testcallbacktoken", encoding_aes_key=base64.b64encode(b"1" * 32).decode()[:-1],
        )
        self.adp_mode = "complete"
        self.wecom_mode = "accepted"
        self.adp_requests = []
        self.wecom_requests = []
        self.adp_called = threading.Event()
        self.sending = threading.Event()

    def _upstream(self, request):
        """在 HTTP 传输边界返回厂商协议结构，不替换解析器。"""
        if request.url.host == "wss.lke.cloud.tencent.com":
            payload = json.loads(request.content)
            self.adp_requests.append(payload)
            self.adp_called.set()
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

    def test_disabled_check_has_no_outbound_side_effect(self):
        """旧配置兼容，禁用后不会访问渠道。"""
        client = self._client(replace(self.config, diagnostics_enabled=False))
        self.assertEqual(client.post(_PATH, json={"target": "wecom", "user_id": "wecom-probe"}).status_code, 404)
        self.assertEqual(self.wecom_requests, [])
        self.assertEqual(self.adp_requests, [])

    def test_adp_checks_actual_sse_and_isolated_identity(self):
        """有正式完成事件才通过，收到确认后的截断保留失败边界。"""
        client = self._client()
        invalid = client.post(_PATH, json={"target": "adp", "message": "caller input"})
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["error"]["code"], "INVALID_DIAGNOSTIC_REQUEST")
        self.assertEqual(self.adp_requests, [])
        response = client.post(_PATH, json={"target": "adp"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["status"], result["last_ok"]), ("passed", "adp_reply_parsed"))
        payload = self.adp_requests[0]
        self.assertEqual(payload["RequestId"], result["request_id"])
        self.assertEqual(payload["AppKey"], self.config.app_key)
        self.assertEqual((payload["UserId"], payload["VisitorId"]), ("adp-probe", "adp-probe"))
        formal_session = client.app.state.chat_service.initialize()
        self.assertNotEqual(payload["ConversationId"], formal_session)
        self.assertNotIn("fixture-only", response.text)
        self.adp_mode = "incomplete"
        result = client.post(_PATH, json={"target": "adp"}).json()
        self.assertEqual((result["status"], result["last_ok"], result["failed_stage"], result["reason"]),
                         ("unknown", "adp_ack", "adp_sse", "incomplete"))
        self.assertNotEqual(payload["ConversationId"], self.adp_requests[-1]["ConversationId"])
        self.assertEqual(client.app.state.chat_service.initialize(), formal_session)

    def test_adp_does_not_probe_with_a_formal_identity(self):
        """测试身份与正式 UserId 或 VisitorId 重合时，不发送诊断请求。"""
        self.enterContext(patch("httpx.AsyncHTTPTransport", side_effect=lambda **_: httpx.MockTransport(self._upstream)))
        for identity in (self.config.user_id, self.config.visitor_id):
            with self.subTest(identity=identity):
                config = replace(self.config, diagnostic_adp_user_id=identity, gateway_enabled=False, log_enabled=False)
                with patch("api.app.load_config", return_value=config), TestClient(create_app()) as client:
                    response = client.post(_PATH, json={"target": "adp"})
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["reason"], "adp_unconfigured")
        self.assertEqual(self.adp_requests, [])

    def test_diagnostic_request_accepts_only_the_target_and_a_single_wecom_member(self):
        """非法成员或试图指定 ADP 身份时返回安全的 422，且没有出站。"""
        client = self._client()
        invalid_requests = [
            {"target": "wecom"},
            *({"target": "wecom", "user_id": value}
              for value in (None, 123, "", " ", "@all", "one|two", "one,two", "A" * 65)),
            {"target": "wecom", "user_id": "wecom-probe", "message": "caller input"},
            {"target": "adp", "user_id": "caller input"},
            {"target": "adp", "user_id": None},
        ]
        for body in invalid_requests:
            with self.subTest(body=body):
                response = client.post(_PATH, json=body)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["error"]["code"], "INVALID_DIAGNOSTIC_REQUEST")
                self.assertNotIn("caller input", response.text)
        self.assertEqual(self.wecom_requests, [])
        self.assertEqual(self.adp_requests, [])

    def test_adp_dns_failure_is_observed_without_leaking_exception(self):
        """连接异常中的 DNS 证据和安全排查方向返回给运维。"""
        self.adp_mode = "dns"
        response = self._client().post(_PATH, json={"target": "adp"})
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual((result["last_ok"], result["failed_stage"], result["reason"]),
                         ("adp_request_prepared", "adp_connect_or_response", "dns_error"))
        self.assertIn("DNS", result["hint"])
        self.assertNotIn("私密底层错误", response.text)

    def test_adp_diagnostic_identity_is_independent_of_wecom_employee_session(self):
        """正式企微回调与诊断使用同一应用，各自的 ADP 身份和会话独立。"""
        client = self._client(replace(self.config, notification_mode="disabled"))
        self.assertEqual(self._callback(client, "业务问句", user="employee-probe").status_code, 200)
        self.assertTrue(self.adp_called.wait(5))
        employee_request = self.adp_requests[0]
        result = client.post(_PATH, json={"target": "adp"}).json()
        self.assertEqual(result["status"], "passed")
        diagnostic_request = self.adp_requests[1]
        self.assertEqual(employee_request["AppKey"], diagnostic_request["AppKey"])
        self.assertEqual((employee_request["UserId"], employee_request["VisitorId"]),
                         ("employee-probe", "employee-probe"))
        self.assertEqual((diagnostic_request["UserId"], diagnostic_request["VisitorId"]),
                         ("adp-probe", "adp-probe"))
        self.assertNotEqual(employee_request["ConversationId"], diagnostic_request["ConversationId"])

    def test_wecom_authenticated_reply_completes_without_adp_or_business_record(self):
        """回发需经过验签，探测与业务存储分离并拒绝重复主动发送。"""
        self.enterContext(patch("application.diagnostics._WECOM_REPLY_SECONDS", 10))
        client = self._client()
        body = {"target": "wecom", "user_id": "alternate-probe"}
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(client.post, _PATH, json=body)
            self.assertTrue(self.sending.wait(5))
            duplicate = client.post(_PATH, json=body)
            self.assertEqual(duplicate.status_code, 409)
            sent = self.wecom_requests[0]
            self.assertEqual(sent["touser"], body["user_id"])
            code = sent["text"]["content"]
            self.assertEqual(self._callback(client, code, user="alternate-probe", valid=False).status_code, 403)
            self.assertEqual(self._callback(client, code, user="ALTERNATE-PROBE").status_code, 200)
            result = future.result(timeout=5).json()
        self.assertEqual((result["status"], result["send_status"], result["receive_status"]),
                         ("passed", "passed", "passed"))
        self.assertEqual(result["last_ok"], "wecom_callback_verified")
        self.assertEqual(self._callback(client, code, user="alternate-probe").status_code, 200)
        self.assertEqual(read_messages(self.config), [])
        self.assertEqual(self.adp_requests, [])
        self.assertEqual(len(self.wecom_requests), 1)

    def test_wecom_wrong_member_cannot_verify_receive(self):
        """另一名通过验签的成员发回正确测试码，仍不能确认指定成员的收发。"""
        self.enterContext(patch("application.diagnostics._WECOM_REPLY_SECONDS", 2))
        client = self._client()
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(client.post, _PATH, json={"target": "wecom", "user_id": "chosen-member"})
            self.assertTrue(self.sending.wait(5))
            code = self.wecom_requests[0]["text"]["content"]
            self.assertEqual(self._callback(client, code, user="other-member").status_code, 200)
            result = future.result(timeout=5).json()
        self.assertEqual((result["send_status"], result["receive_status"], result["reason"]),
                         ("passed", "unverified", "reply_not_observed"))
        self.assertEqual(read_messages(self.config), [])
        self.assertEqual(self.adp_requests, [])

    def test_disabled_diagnostics_ignores_test_codes_but_accepts_ordinary_text(self):
        """停用后仍拦截完整测试码，相同前缀的普通消息继续持久化。"""
        client = self._client(replace(self.config, diagnostics_enabled=False))
        self.assertEqual(self._callback(client, "ADP-DIAG-" + "f" * 32).status_code, 200)
        content = "ADP-DIAG-今天的排障记录"
        self.assertEqual(self._callback(client, content).status_code, 200)
        self.assertEqual([record["content"] for record in read_messages(self.config)], [content])

    def test_wecom_platform_acceptance_with_no_reply_leaves_receive_unverified(self):
        """平台接受和实际回发分开验收。"""
        with patch("application.diagnostics._WECOM_REPLY_SECONDS", 0):
            result = self._client().post(_PATH, json={"target": "wecom", "user_id": "wecom-probe"}).json()
        self.assertEqual((result["status"], result["send_status"], result["receive_status"]),
                         ("unverified", "passed", "unverified"))
        self.assertEqual((result["failed_stage"], result["reason"]), ("wecom_callback", "reply_not_observed"))

    def test_wecom_token_rejection_and_lost_send_response_are_distinct(self):
        """已提交但未见响应的发送与 Token 拒绝具有不同边界。"""
        client = self._client()
        body = {"target": "wecom", "user_id": "wecom-probe"}
        self.wecom_mode = "token_rejected"
        result = client.post(_PATH, json=body).json()
        self.assertEqual((result["failed_stage"], result["send_status"], result["provider_code"]),
                         ("wecom_token", "failed", 40001))
        self.assertEqual(self.wecom_requests, [])
        self.wecom_mode = "read_timeout"
        result = client.post(_PATH, json=body).json()
        self.assertEqual((result["failed_stage"], result["send_status"], result["reason"]),
                         ("wecom_send", "unknown", "read_timeout"))
        self.assertEqual(len(self.wecom_requests), 1)
