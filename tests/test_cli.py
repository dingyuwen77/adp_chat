"""单次与交互 CLI 的真实入口验证。"""

import contextlib
import io
import json
import os
import subprocess
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cli.runner import main
from test_chat import FakeStream, completed


class CliTests(unittest.TestCase):
    """单轮、会话续接和 CLI 安全边界。"""

    def setUp(self):
        """独立配置及会话数据。"""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.toml"
        self.path.write_text(
            '[adp]\napp_key="fixture-key"\nuser_id="cli-user"\n[logging]\nlog_enabled=false\n',
            encoding="utf-8",
        )

    def test_help(self):
        """模块启动帮助不需要 HTTP 依赖。"""
        executable = Path(sysconfig.get_path("scripts")) / ("adp-chat.exe" if os.name == "nt" else "adp-chat")
        result = subprocess.run(
            [str(executable), "--help"],
            capture_output=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--ask", result.stdout)

    def test_single_turn_success_and_failure(self):
        """一次输入仅触发一次 ADP 请求，断流不伪装成成功。"""
        output, errors = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors),
            patch("adp.sse.open_request", side_effect=[
                FakeStream(completed()), FakeStream(b"data: [DONE]\n\n"),
            ]) as network,
        ):
            self.assertEqual(main(["--config", str(self.path), "--ask", "问题"]), 0)
            self.assertEqual(main(["--config", str(self.path), "--ask", "问题"]), 1)
        self.assertEqual(network.call_count, 2)
        self.assertIn("完整回答", output.getvalue())
        self.assertIn("[chat/incomplete]", errors.getvalue())

    def test_interactive_session_and_new(self):
        """连续提问复用会话，/new 显式开启新会话。"""
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
            patch("builtins.input", side_effect=["第一轮", "第二轮", "/new", "新会话", "/exit"]),
            patch("adp.sse.open_request", side_effect=[FakeStream(completed()) for _ in range(3)]) as network,
        ):
            self.assertEqual(main(["--config", str(self.path)]), 0)
        payloads = [json.loads(call.args[0].data) for call in network.call_args_list]
        self.assertEqual(payloads[0]["ConversationId"], payloads[1]["ConversationId"])
        self.assertNotEqual(payloads[1]["ConversationId"], payloads[2]["ConversationId"])

    def test_file_command_and_flags_rejected(self):
        """附件参数和旧命令不会发送 ADP 请求。"""
        with patch("adp.sse.open_request") as network, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(["--file", "local"])
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(main(["--config", str(self.path), "--ask", "/file private"]), 1)
        network.assert_not_called()

    def test_import_has_no_environment_side_effects(self):
        """核心包导入不修改 root logger 与 TLS 变量。"""
        code = (
            "import logging,os,sys;h=list(logging.getLogger().handlers);"
            "import adp.client,core.config;assert os.environ['SSLKEYLOGFILE']=='fixture';"
            "assert h==logging.getLogger().handlers;"
            "assert 'tencentcloud' not in sys.modules"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            env={**os.environ, "SSLKEYLOGFILE": "fixture"},
            capture_output=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
