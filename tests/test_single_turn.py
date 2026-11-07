"""单次入口、持久会话与 V2 正式回复的回归测试。"""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from adp.client import chat
from core.config import ChatConfig, load_config
from core.models import ChatError
from test_chat import FakeStream, completed, event


ROOT = Path(__file__).resolve().parents[1]


class SingleTurnTests(unittest.TestCase):
    def setUp(self):
        """每个用例独享真实配置和磁盘目录。"""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "config.toml"
        self.path.write_text(
            '[adp]\napp_key="fixture-key"\nuser_id="tester"\n'
            '[chat]\nstructured_interaction=true\n'
            '[logging]\nlog_enabled=false\n', encoding="utf-8",
        )

    def run_main(self, inputs, **patches):
        """通过正式入口送入键盘输入，观察一次请求与退出状态。"""
        import main

        with (
            patch("builtins.input", side_effect=inputs) as keyboard,
            patch("adp.sse.open_request", **patches) as network,
            contextlib.redirect_stdout(io.StringIO()) as output,
            contextlib.redirect_stderr(io.StringIO()) as errors,
        ):
            code = main.main(["--config", str(self.path)])
        return code, output.getvalue(), errors.getvalue(), keyboard, network

    def test_clarification_is_one_request_and_session_is_saved_before_network(self):
        """追问也只展示一次；持久化早于首次网络请求。"""
        def respond(request, **kwargs):
            """在网络边界核对真实磁盘状态，而非只检查调用次数。"""
            state = json.loads((self.root / "runtime/session.json").read_text())
            payload = json.loads(request.data)
            self.assertEqual(state["conversation_id"], payload["ConversationId"])
            self.assertNotIn("fixture-key", json.dumps(state))
            return FakeStream(event("response.completed", Response={
                "ConversationId": payload["ConversationId"], "Status": "success",
                "ExtraInfo": {"ReplyMethod": 15},
                "Messages": [{"Type": "reply", "Contents": [{"Type": "text", "Text": "请补充城市"}]}],
            }))

        code, output, errors, keyboard, network = self.run_main(["查询"], side_effect=respond)
        self.assertEqual(code, 0, errors)
        self.assertEqual(output, "请补充城市\n")
        self.assertEqual(keyboard.call_count, 1)
        self.assertEqual(network.call_count, 1)

    def test_empty_input_does_not_send(self):
        """空输入不创建会话，也不请求 ADP。"""
        code, _, errors, _, network = self.run_main(["   "])
        self.assertEqual(code, 1)
        self.assertIn("empty_question", errors)
        network.assert_not_called()
        self.assertFalse((self.root / "runtime/session.json").exists())

    def test_text_only_interface_rejects_retired_attachment_inputs(self):
        """纯文字范围移除附件参数和命令，不把本地路径意外发给 ADP。"""
        import inspect

        for name in ("file", "image"):
            self.assertNotIn(name, inspect.signature(chat).parameters)
        for command in ('/image "C:\\private.png"', '/file "C:\\private.txt"'):
            code, _, errors, _, network = self.run_main([command])
            self.assertEqual(code, 1)
            self.assertIn("unsupported_command", errors)
            network.assert_not_called()

    def test_legacy_attachment_config_is_ignored_without_retaining_secrets(self):
        """旧本机配置仍可用于文本；已退役上传配置不进入运行时对象。"""
        with self.path.open("a", encoding="utf-8") as destination:
            destination.write('[attachments]\nsecret_key="retired-secret"\nallow_public_image_upload=true\n')
        config = load_config(self.path)
        self.assertFalse(hasattr(config, "secret_key"))
        self.assertNotIn("retired-secret", repr(config))
        code, output, errors, _, network = self.run_main(["问题"], return_value=FakeStream(completed()))
        self.assertEqual(code, 0, errors)
        self.assertEqual(output, "完整回答\n")
        self.assertEqual(len(json.loads(network.call_args.args[0].data)["Contents"]), 1)

    def test_text_beginning_with_slash_remains_a_literal_question(self):
        """路径或其他以斜线开头的文字仍可转发，仅退役附件命令被拒绝。"""
        question = "/var/log 这个目录有什么作用？"
        code, _, errors, _, network = self.run_main([question], return_value=FakeStream(completed()))
        self.assertEqual(code, 0, errors)
        self.assertEqual(json.loads(network.call_args.args[0].data)["Contents"], [{"Type": "text", "Text": question}])

    def test_repeated_text_runs_share_id_with_unique_requests(self):
        """多次文字启动始终复用会话，每轮仅一个文字内容与新请求 ID。"""
        payloads = []
        for line in ("第一轮", "第二轮", "第三轮"):
            code, _, errors, keyboard, network = self.run_main([line], return_value=FakeStream(completed()))
            self.assertEqual(code, 0, errors)
            self.assertEqual(keyboard.call_count, 1)
            payloads.append(json.loads(network.call_args.args[0].data))
        self.assertEqual(len({p["ConversationId"] for p in payloads}), 1)
        self.assertEqual(len({p["RequestId"] for p in payloads}), 3)
        self.assertEqual([p["Contents"] for p in payloads], [[{"Type": "text", "Text": text}] for text in ("第一轮", "第二轮", "第三轮")])

    def test_failure_keeps_session_without_retry(self):
        """断流和请求超时都保留会话与关联 ID，不自动重发。"""
        code, _, errors, _, network = self.run_main(["问题"], return_value=FakeStream(b""))
        self.assertEqual(code, 1)
        self.assertIn("incomplete", errors)
        self.assertEqual(network.call_count, 1)
        original = (self.root / "runtime/session.json").read_bytes()
        error = ChatError("chat", "timeout", "请求超时")
        code, _, errors, _, network = self.run_main(["后续问题"], side_effect=error)
        self.assertEqual(code, 1)
        self.assertIn("chat/timeout", errors)
        self.assertEqual(network.call_count, 1)
        self.assertTrue(error.request_id)
        self.assertEqual(error.conversation_id, json.loads(original)["conversation_id"])
        self.assertEqual((self.root / "runtime/session.json").read_bytes(), original)

    def test_server_ids_are_checked_and_diagnostic_request_id_is_independent(self):
        """两个事件中的会话不一致均拒绝；诊断请求 ID 允许不同。"""
        config = ChatConfig("fixture", "tester", log_enabled=False)
        cid = "1" * 32
        for kind, field in (("request_ack", "RequestAck"), ("response.completed", "Response")):
            for value in ("2" * 32, None, 42, ""):
                with self.subTest(kind=kind, value=value):
                    stream = event(kind, **{field: {"Status": "success", "ConversationId": value}}) + completed()
                    with patch("adp.sse.open_request", return_value=FakeStream(stream)):
                        with self.assertRaises(ChatError) as caught:
                            chat("问题", config=config, conversation_id=cid)
                    self.assertEqual(caught.exception.code, "conversation_mismatch")
                    self.assertEqual(caught.exception.conversation_id, cid)
        stream = event("request_ack", RequestAck={"Status": "success", "ConversationId": cid, "ExtraInfo": {"RequestId": "diagnostic"}}) + completed()
        with patch("adp.sse.open_request", return_value=FakeStream(stream)):
            self.assertEqual(chat("问题", config=config, conversation_id=cid).conversation_id, cid)

    def test_nontext_reply_is_preserved_and_visible(self):
        """保留正式回复内容及顺序，排除 thought 和工具消息。"""
        from cli.console import show_result

        parts = [{"Type": "image", "Image": {"Url": "https://example.com/image.png"}}, {"Type": "e_chart", "EChart": {"data": [1]}}]
        stream = event("response.completed", Response={"Status": "success", "Messages": [
            {"Type": "thought", "Contents": [{"Type": "text", "Text": "内部推理"}]},
            {"Type": "tool", "Contents": [{"Type": "text", "Text": "内部工具"}]},
            {"Type": "reply", "Contents": parts},
        ]})
        with patch("adp.sse.open_request", return_value=FakeStream(stream)):
            result = chat("问题", config=ChatConfig("fixture", "tester", log_enabled=False))
        self.assertEqual(result.reply_contents, (tuple(parts),))
        self.assertEqual(result.text, "")
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as errors:
            show_result(result)
        self.assertIn("image", output.getvalue())
        self.assertIn("e_chart", output.getvalue())
        self.assertNotIn("内部", output.getvalue())
        self.assertNotIn("完成", errors.getvalue())

    def test_default_config_path_is_project_root_from_another_working_directory(self):
        """PyCharm 使用任意工作目录时仍定位项目根目录配置。"""
        from core.config import default_config_path

        original = Path.cwd()
        try:
            os.chdir(self.root)
            self.assertEqual(default_config_path(), ROOT / "config.toml")
        finally:
            os.chdir(original)

    def test_example_config_keeps_persistence_beside_root_config(self):
        """直接复制正式模板后，日志和会话均留在配置所在根目录。"""
        template = (ROOT / "config.example.toml").read_text(encoding="utf-8")
        self.path.write_text(template.replace('app_key = ""', 'app_key = "fixture"'), encoding="utf-8")
        config = load_config(self.path)
        self.assertEqual(Path(config.session_file).resolve(), self.root / "runtime/session.json")
        self.assertEqual(Path(config.log_file).resolve(), self.root / "logs/adp_chat.log")

    def test_example_explicitly_enables_question_logging(self):
        """复制模板后显式启用问题日志，代码默认关闭仍由独立回归保护。"""
        template = (ROOT / "config.example.toml").read_text(encoding="utf-8")
        self.path.write_text(template.replace('app_key = ""', 'app_key = "fixture"'), encoding="utf-8")
        self.assertTrue(load_config(self.path).log_question)

    def test_default_root_config_uses_root_persistence_directories(self):
        """根配置省略路径字段时，使用同级日志和持久会话目录。"""
        from core.config import default_config_path

        config_file = self.root / "config.toml"
        config_file.write_text('[adp]\napp_key="fixture"\nuser_id="tester"\n', encoding="utf-8")
        with patch("core.config.default_config_path", return_value=config_file):
            config = load_config()
        self.assertEqual(Path(config.session_file).resolve(), (self.root / "runtime/session.json").resolve())
        self.assertEqual(Path(config.log_file).resolve(), (self.root / "logs/adp_chat.log").resolve())

    def test_session_identity_corruption_new_and_failed_atomic_write(self):
        """损坏、身份冲突、显式重建和失败原子替换均有明确行为。"""
        from storage.session import load_session

        config = load_config(self.path)
        first = load_session(config)
        self.assertEqual(str(UUID(first)), first)
        self.assertEqual(first, load_session(config))
        state_path = Path(config.session_file)
        original = state_path.read_bytes()
        for changed in (replace(config, app_key="another"), replace(config, user_id="another")):
            with self.assertRaises(ChatError) as caught:
                load_session(changed)
            self.assertEqual(caught.exception.code, "identity_mismatch")
            self.assertEqual(state_path.read_bytes(), original)
        with patch("storage.session.os.replace", side_effect=PermissionError):
            with self.assertRaises(ChatError) as caught:
                load_session(config, new=True)
        self.assertEqual(caught.exception.code, "unwritable")
        self.assertEqual(state_path.read_bytes(), original)
        self.assertEqual(list(state_path.parent.iterdir()), [state_path])
        second = load_session(config, new=True)
        self.assertNotEqual(first, second)
        for damaged in ("{", "[]", '{"schema_version":true}', original.decode().replace(first, "short")):
            state_path.write_bytes(damaged.encode("utf-8"))
            with self.assertRaises(ChatError) as caught:
                load_session(config)
            self.assertEqual(caught.exception.code, "invalid_session")
            self.assertEqual(state_path.read_bytes(), damaged.encode("utf-8"))

    def test_real_process_restarts_from_another_working_directory(self):
        """退出 Python 后重启，仅模拟远端传输，使用真实配置和磁盘。"""
        harness = self.root / "harness.py"
        harness.write_text(
            'import io,json,runpy,sys\nfrom unittest.mock import patch\n'
            'def respond(request, **kwargs):\n'
            '    p=json.loads(request.data)\n'
            '    with open(record,"a") as f: f.write(json.dumps({k:p[k] for k in ("ConversationId","RequestId","Contents")})+"\\n")\n'
            '    data={"Type":"response.completed","Response":{"Status":"success","ConversationId":p["ConversationId"],"Messages":[{"Type":"reply","Contents":[{"Type":"text","Text":"reply"}]}]}}\n'
            '    r=io.BytesIO(("event: response.completed\\ndata: "+json.dumps(data)+"\\n\\n").encode())\n'
            '    r.headers={"Content-Type":"text/event-stream"}\n    return r\n'
            'target,record,config=sys.argv[1:]\nsys.argv=[target,"--config",config]\n'
            'with patch("adp.sse.open_request",side_effect=respond): runpy.run_path(target,run_name="__main__")\n', encoding="utf-8",
        )
        record = self.root / "requests.jsonl"
        for question in ("第一轮", "第二轮"):
            result = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(harness), str(ROOT / "main.py"), str(record), str(self.path)],
                input=question + "\n", cwd=self.root, capture_output=True, encoding="utf-8", timeout=15,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "reply\n")
        payloads = [json.loads(line) for line in record.read_text().splitlines()]
        self.assertEqual(payloads[0]["ConversationId"], payloads[1]["ConversationId"])
        self.assertNotEqual(payloads[0]["RequestId"], payloads[1]["RequestId"])

    def test_save_and_cleanup_failure_keep_chat_error_and_old_session(self):
        """保存与清理同时失败仍返回稳定 ChatError，并保留旧会话。"""
        from storage.session import load_session

        config = load_config(self.path)
        load_session(config)
        state = Path(config.session_file)
        original = state.read_bytes()
        with patch("storage.session.os.replace", side_effect=PermissionError), patch.object(Path, "unlink", side_effect=PermissionError):
            with self.assertRaises(ChatError) as caught:
                load_session(config, new=True)
        self.assertEqual(caught.exception.code, "unwritable")
        self.assertIn("清理", str(caught.exception))
        self.assertEqual(state.read_bytes(), original)

    def test_explicit_new_session_command_has_no_adp_request(self):
        """正式入口的新会话命令立即落盘，不读题也不触发网络。"""
        import main

        with patch("builtins.input") as keyboard, patch("adp.sse.open_request") as network, contextlib.redirect_stderr(io.StringIO()):
            args = ["--config", str(self.path), "--new-session"]
            self.assertEqual(main.main(args), 0)
            first = json.loads((self.root / "runtime/session.json").read_text())["conversation_id"]
            self.assertEqual(main.main(args), 0)
            second = json.loads((self.root / "runtime/session.json").read_text())["conversation_id"]
        self.assertNotEqual(first, second)
        keyboard.assert_not_called()
        network.assert_not_called()

    def test_unreadable_utf8_and_persistence_failure_do_not_start_network(self):
        """损坏编码与落盘失败均在网络前拒绝，不覆盖原状态。"""
        state = self.root / "runtime/session.json"
        state.parent.mkdir()
        state.write_bytes(b"\xff\xfe")
        code, _, errors, _, network = self.run_main(["问题"])
        self.assertEqual(code, 1)
        self.assertIn("invalid_session", errors)
        network.assert_not_called()
        self.assertEqual(state.read_bytes(), b"\xff\xfe")
        state.unlink()
        with patch("storage.session.os.replace", side_effect=PermissionError):
            code, _, errors, _, network = self.run_main(["问题"])
        self.assertEqual(code, 1)
        self.assertIn("unwritable", errors)
        network.assert_not_called()
