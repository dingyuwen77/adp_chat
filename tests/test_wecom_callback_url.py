"""回调外部URL配置的格式、旧配置兼容和本机模拟边界。"""

import base64
import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from core.config import ChatConfig, load_config
from core.identity import employee_identity
from core.models import ChatError
from wecom_callback_client import main as callback_main


class CallbackURLTests(unittest.TestCase):
    """仅使用虚构配置与内存HTTP，不读取实际凭据或访问公网。"""

    def test_optional_url_loads_http_https_and_preserves_identity(self):
        """旧配置保留默认路由，新地址只改变接收路径而不改变员工会话。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            base = '[adp]\napp_key="fixture"\nuser_id="legacy"\n'
            path.write_text(base, encoding="utf-8")
            legacy = load_config(path)
            self.assertEqual(legacy.callback_url, "")
            self.assertEqual(legacy.callback_path, "/wecom/callback")
            for url, expected in (("http://115.190.218.12:4560/wecom/callback", "/wecom/callback"),
                                  ("https://callback.example/qy/receive", "/qy/receive"),
                                  ("http://[::1]:8000/local/callback", "/local/callback")):
                with self.subTest(url=url):
                    path.write_text(base + '[wecom]\ncallback_url=' + json.dumps(url) + '\n', encoding="utf-8")
                    config = load_config(path)
                    self.assertEqual(config.callback_url, url)
                    self.assertEqual(config.callback_path, expected)
                    self.assertFalse(config.gateway_enabled)
            identity_config = replace(legacy, corp_id="fixture-corp", agent_id=1, adp_application="fixture-app")
            self.assertEqual(employee_identity(identity_config, "YNND"), employee_identity(
                replace(identity_config, callback_url="https://other.example/new/path"), "YNND"))

    def test_invalid_urls_fail_without_echoing_their_content(self):
        """错误地址、凭据查询和保留API路径在启动前失败，错误不回显敏感值。"""
        invalid = ("ftp://host/qy", "http:///qy", "http://host:0/qy", "http://host:70000/qy",
                   "http://host:abc/qy", "http://host/qy，", " http://host/qy", "http://host/qy\n",
                   "http://host/qy?secret=private", "http://host/qy#private", "http://user:private@host/qy",
                   "http://host/api/v1/chat", "http://host/docs", "http://host/healthz", "http://host/openapi.json",
                   "http://host/{variable}", "http://host/a/../qy", "http://host/%71y")
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ChatError) as raised:
                ChatConfig("fixture", "legacy", callback_url=url)
            self.assertEqual(raised.exception.code, "invalid_callback_url")
            self.assertNotIn("private", str(raised.exception))
        with self.assertRaises(ChatError) as raised:
            ChatConfig("fixture", "legacy", callback_url=True)
        self.assertEqual(raised.exception.code, "invalid_type")

    def test_local_client_uses_configured_path_without_targeting_public_host(self):
        """公开URL只贡献路由路径，本机模拟器仍将请求发向显式loopback。"""
        config = ChatConfig("fixture", "legacy", corp_id="fixture-corp", agent_id=1, adp_application="fixture",
            gateway_enabled=True, callback_token="fixturetoken", encoding_aes_key=base64.b64encode(b"k" * 32).decode()[:-1],
            callback_url="http://115.190.218.12:4560/qy/receive", log_enabled=False)
        requests = []

        def respond(request, **_kwargs):
            """仅以内存响应承载客户端校验，不建立任何外部连接。"""
            requests.append(request)
            response = io.BytesIO(b"local-wecom-url-check")
            response.status = 200
            return response

        with patch("wecom_callback_client.load_config", return_value=config), \
                patch("wecom_callback_client.urlopen", side_effect=respond), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(callback_main(["--verify-url"]), 0)
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0].full_url.startswith("http://127.0.0.1:8000/qy/receive?"))
