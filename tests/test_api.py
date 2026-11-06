"""FastAPI 合约回归：CLI 纯标准库环境可明确跳过。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adp_chat.core.config import ChatConfig
from adp_chat.core.models import ChatError, ChatResult

try:
    from fastapi.testclient import TestClient
    from adp_chat.api.app import app
except ImportError:
    TestClient = None
    app = None


@unittest.skipIf(TestClient is None, "测试环境未安装 FastAPI/HTTPX 服务依赖")
class ApiTests(unittest.TestCase):
    """通过实际 ASGI 路由验证输入、输出及故障响应。"""

    def setUp(self):
        """使用真实 lifespan 和临时会话文件。"""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = ChatConfig(
            "fixture-app-key", "only-user", log_enabled=False,
            session_file=str(Path(directory.name) / "session.json"),
        )
        loader = patch("adp_chat.api.app.load_config", return_value=self.config)
        loader.start()
        self.addCleanup(loader.stop)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_health_and_invalid_input(self):
        """健康检查和 422 不发送 ADP。"""
        with patch("adp_chat.application.chat_service.chat") as provider:
            self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})
            for payload in ({}, {"message": "  "}, {"message": 3}, {"message": "ok", "extra": 1}):
                response = self.client.post("/api/v1/chat", json=payload)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["error"]["code"], "INVALID_REQUEST")
        provider.assert_not_called()

    def test_success_preserves_nontext_and_identifiers(self):
        """服务成功时保留正式回复类型与关联标识。"""
        cid = app.state.chat_service.initialize()
        result = ChatResult(
            "完成", cid, "request-1", record_id="record-1", reply_method=18,
            reply_contents=(({"Type": "text", "Text": "完成"}, {"Type": "image", "Image": {"Url": "fixture"}}),),
        )
        with patch("adp_chat.application.chat_service.chat", return_value=result) as provider:
            response = self.client.post("/api/v1/chat", json={"message": "您好"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["conversation_id"], cid)
        self.assertEqual(response.json()["reply_contents"][0][1]["Type"], "image")
        self.assertEqual(provider.call_count, 1)

    def test_unexpected_error_returns_safe_500(self):
        """不可预期异常不把私密正文暴露给调用方。"""
        with patch("adp_chat.application.chat_service.chat", side_effect=RuntimeError("sensitive exception")):
            with TestClient(app, raise_server_exceptions=False) as client:
                response = client.post("/api/v1/chat", json={"message": "你好"})
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error"]["code"], "INTERNAL_ERROR")
        self.assertNotIn("sensitive exception", response.text)

    def test_upstream_error_and_timeout(self):
        """错误和超时不得返回 200，也不得泄露原始异常文本。"""
        for error, status in [
            (ChatError("chat", "timeout", "private body"), 504),
            (ChatError("chat", "network_error", "private body"), 502),
        ]:
            with self.subTest(status=status), patch("adp_chat.application.chat_service.chat", side_effect=error):
                response = self.client.post("/api/v1/chat", json={"message": "你好"})
            self.assertEqual(response.status_code, status)
            self.assertNotIn("private body", response.text)
