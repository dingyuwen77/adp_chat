import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adp_chat.cli import main
from test_chat import FakeStream, completed


class CliTests(unittest.TestCase):
    def setUp(self):
        """为命令行测试准备本地配置与独立的输出流。"""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "config.toml"
        self.path.write_text('[adp]\napp_key = "test-key"\nuser_id = "cli-test"\n', encoding="utf-8")
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()

    def test_public_command_help(self):
        """运行真实的 Python 模块入口，确认 help 可以独立启动。"""
        result = subprocess.run(
            [sys.executable, "-B", "-m", "adp_chat", "--help"],
            capture_output=True, encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--conversation-id", result.stdout)
        self.assertIn("腾讯云 ADP Multi-Agent 对话", result.stdout)

    def test_cli_outputs_final_answer_and_returns_nonzero_on_stream_failure(self):
        """CLI 在同一公共链路上区分成功与断流。"""
        with contextlib.redirect_stdout(self.stdout), contextlib.redirect_stderr(self.stderr):
            with patch("adp_chat.sse.open_request", side_effect=[
                FakeStream(completed()), FakeStream(b"data: [DONE]\n\n"),
            ]):
                args = ["--config", str(self.path), "--ask", "问题"]
                self.assertEqual(main(args), 0)
                self.assertEqual(main(args), 1)
        self.assertEqual(self.stdout.getvalue(), "完整回答\n")
        self.assertIn("[chat/incomplete]", self.stderr.getvalue())
        self.assertNotIn("test-key", self.stderr.getvalue())

    def test_interactive_new_changes_session(self):
        """交互模式复用会话，只有 /new 切换会话。"""
        with (
            contextlib.redirect_stdout(self.stdout),
            contextlib.redirect_stderr(self.stderr),
            patch("builtins.input", side_effect=["第一轮", "第二轮", "/new", "新会话", "/exit"]),
            patch("adp_chat.sse.open_request", side_effect=[FakeStream(completed()) for _ in range(3)]) as sse,
        ):
            self.assertEqual(main(["--config", str(self.path)]), 0)
        payloads = [json.loads(call.args[0].data) for call in sse.call_args_list]
        self.assertEqual(payloads[0]["ConversationId"], payloads[1]["ConversationId"])
        self.assertNotEqual(payloads[1]["ConversationId"], payloads[2]["ConversationId"])

    def test_retired_attachment_flags_and_commands_are_rejected(self):
        """旧附件入口明确拒绝，文字 CLI 不会读取或上传本地文件。"""
        with contextlib.redirect_stderr(self.stderr), patch("adp_chat.sse.open_request") as network:
            for flag in ("--file", "--image"):
                with self.assertRaises(SystemExit) as caught:
                    main([flag, "local-path", "--ask", "问题"])
                self.assertEqual(caught.exception.code, 2)
            with patch("builtins.input", side_effect=['/file "local-path"', "/exit"]):
                self.assertEqual(main(["--config", str(self.path)]), 0)
        network.assert_not_called()
        self.assertIn("unsupported_command", self.stderr.getvalue())

    def test_import_preserves_environment_and_root_logging(self):
        """在新进程验证库导入不会配置宿主日志或删除 TLS 环境变量。"""
        code = (
            "import logging, os, sys; "
            "handlers = list(logging.getLogger().handlers); "
            "import adp_chat; "
            "assert os.environ['SSLKEYLOGFILE'] == 'fixture-keylog'; "
            "assert handlers == logging.getLogger().handlers; "
            "assert 'qcloud_cos' not in sys.modules and 'tencentcloud' not in sys.modules"
        )
        result = subprocess.run(
            [sys.executable, "-B", "-c", code],
            env={**os.environ, "SSLKEYLOGFILE": "fixture-keylog"}, capture_output=True,
            encoding="utf-8", timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
