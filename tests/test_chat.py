import io
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from adp.client import chat
from core.config import ChatConfig, load_config
from core.models import ChatError


def event(kind, **fields):
    """构造符合 V2 格式的 SSE 事件。"""
    return (
        f"event: {kind}\r\ndata: "
        + json.dumps({"Type": kind, **fields}, ensure_ascii=False)
        + "\r\n\r\n"
    ).encode("utf-8")


def completed(text="完整回答"):
    """生成带有工具消息和最终回复的成功事件。"""
    return event(
        "response.completed",
        Response={
            "Status": "success",
            "Messages": [
                {"Type": "thought", "Contents": [{"Type": "text", "Text": "内部消息"}]},
                {"Type": "reply", "Contents": [{"Type": "text", "Text": text}]},
            ],
        },
    )


def response(text, reply_method=1, extra_contents=()):
    """构造保留正式回复方式与原始内容的一轮成功响应。"""
    contents = [{"Type": "text", "Text": text}] if text else []
    return FakeStream(event("response.completed", Response={
        "Status": "success",
        "ExtraInfo": {"ReplyMethod": reply_method},
        "Messages": [{"Type": "reply", "Contents": contents + list(extra_contents)}],
    }))


class FakeStream(io.BytesIO):
    headers = {"Content-Type": "text/event-stream; charset=utf-8"}


class ChatTests(unittest.TestCase):
    def setUp(self):
        """创建每个测试独享的本地配置。"""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config_path = self.root / "config.toml"
        self.config_path.write_text(
            '[adp]\napp_key = "test-key"\nuser_id = "tester"\n'
            '[chat]\nenable_multi_intent = false\n',
            encoding="utf-8",
        )
        self.config = load_config(self.config_path)

    def test_text_calls_reuse_session_and_keep_request_ids_distinct(self):
        """切换输出模式后仍复用会话、生成独立请求并保留完整正式回复。"""
        with patch("adp.sse.open_request", side_effect=[
            FakeStream(completed()), FakeStream(completed("第二轮")),
        ]) as transport:
            first = chat("第一轮", config=self.config)
            second = chat(
                "第二轮", config=replace(self.config, stream=True, streaming_throttle=20),
                conversation_id=first.conversation_id,
            )

        first_request, second_request = [
            json.loads(call.args[0].data) for call in transport.call_args_list
        ]
        self.assertEqual([first_request["Stream"], second_request["Stream"]], ["disable", "enable"])
        self.assertIs(first_request["Incremental"], False)
        self.assertIs(second_request["Incremental"], True)
        self.assertEqual(second_request["StreamingThrottle"], 20)
        self.assertEqual(first.text, "完整回答")
        self.assertEqual(second.text, "第二轮")
        self.assertEqual(first.conversation_id, second.conversation_id)
        self.assertNotEqual(first.request_id, second.request_id)
        self.assertEqual(second_request["ConversationId"], first_request["ConversationId"])
        self.assertEqual(second_request["RequestId"], second.request_id)
        self.assertEqual(second_request["VisitorId"], "tester")
        self.assertEqual(second_request["UserId"], "tester")
        self.assertIs(second_request["EnableMultiIntent"], False)
        self.assertEqual(second_request["Contents"], [{"Type": "text", "Text": "第二轮"}])

    def test_intermediate_text_never_replaces_the_completed_reply(self):
        """中间追加、替换和思考事件都不能代替正式完成结果。"""
        stream = FakeStream(
            b"\xef\xbb\xbf: heartbeat\r\n\r\n"
            + event("text.delta", MessageId="thought", ContentIndex=0, Text="思考")
            + event("message.added", Message={"Type": "reply", "MessageId": "reply"})
            + event("text.delta", MessageId="reply", ContentIndex=0, Text="初稿")
            + event("text.replace", MessageId="reply", ContentIndex=0, Text="修订稿")
            + completed()
        )
        with patch("adp.sse.open_request", return_value=stream):
            result = chat("问题", config=self.config)
        self.assertEqual(result.text, "完整回答")
        self.assertTrue(stream.closed)

    def test_incomplete_or_rejected_streams_raise_with_request_context(self):
        """断流、服务端拒绝和失败终态都返回可诊断的异常。"""
        for data, code in [
            (b"data: [DONE]\n\n", "incomplete"),
            (completed().rstrip(b"\r\n"), "incomplete"),
            (event("error", Error={"Code": 460501, "Message": "test-key"}), "provider_error"),
            (event("request_ack", RequestAck={"Status": "failed"}), "request_rejected"),
            (event("response.completed", Response={"Status": "failed"}), "response_failed"),
        ]:
            with self.subTest(code=code):
                stream = FakeStream(data)
                with patch("adp.sse.open_request", return_value=stream):
                    with self.assertRaises(ChatError) as caught:
                        chat("问题", config=self.config)
                self.assertEqual(caught.exception.stage, "chat")
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.request_id)
                self.assertTrue(caught.exception.conversation_id)
                self.assertNotIn("test-key", str(caught.exception))
                self.assertTrue(stream.closed)

    def test_stream_accepts_cr_line_endings(self):
        """允许使用单独回车符作为 SSE 分隔符。"""
        stream = FakeStream(completed().replace(b"\r\n", b"\r"))
        with patch("adp.sse.open_request", return_value=stream):
            self.assertEqual(chat("问题", config=self.config).text, "完整回答")

    def test_read_errors_close_response_and_keep_request_context(self):
        """两种模式读取超时或断网都关闭连接，并保留本轮关联标识。"""
        for enabled in (False, True):
            for failure, code in ((TimeoutError("private"), "timeout"), (OSError("private"), "network_error")):
                class BrokenStream(FakeStream):
                    """在已建立的响应读取处注入传输错误。"""

                    def read1(self, size):
                        """模拟真实 socket 读取失败。"""
                        raise failure

                stream = BrokenStream()
                with self.subTest(stream=enabled, error=code), patch("adp.sse.open_request", return_value=stream):
                    with self.assertRaises(ChatError) as caught:
                        chat("问题", config=replace(self.config, stream=enabled))
                self.assertEqual(caught.exception.code, code)
                self.assertTrue(caught.exception.request_id)
                self.assertNotIn("private", str(caught.exception))
                self.assertTrue(stream.closed)

    def test_diagnostic_keeps_provider_request_id_and_trace_id_distinct(self):
        """服务端请求和跟踪标识保留各自的字段。"""
        stream = FakeStream(event("error", Error={
            "Code": 460501, "RequestId": "provider-request", "TraceId": "trace-id",
        }))
        with patch("adp.sse.open_request", return_value=stream):
            with self.assertRaises(ChatError) as caught:
                chat("问题", config=self.config)
        self.assertEqual(caught.exception.provider_request_id, "provider-request")
        self.assertEqual(caught.exception.provider_trace_id, "trace-id")

    def test_dedicated_model_concurrency_error_is_actionable_without_retry(self):
        """真实460701错误应直接说明并发限制，同时保留原错误分类与关联ID。"""
        stream = FakeStream(event("error", Error={
            "Code": 460701, "Message": "test-key", "RequestId": "server-request", "TraceId": "server-trace",
        }))
        with patch("adp.sse.open_request", return_value=stream) as network:
            with self.assertRaises(ChatError) as caught:
                chat("我想改密码", config=self.config)
        error = caught.exception
        self.assertEqual(error.code, "provider_error")
        self.assertEqual(error.provider_code, "460701")
        self.assertIn("专属模型", str(error))
        self.assertIn("并发", str(error))
        self.assertIn("460701", str(error))
        self.assertNotIn("test-key", str(error))
        self.assertEqual(error.provider_request_id, "server-request")
        self.assertEqual(network.call_count, 1)
        self.assertTrue(stream.closed)

    def test_invalid_input_does_not_start_network(self):
        """错误问题或会话 ID 在联网前拒绝。"""
        with patch("adp.sse.open_request") as transport:
            for question, kwargs in [
                ("", {}), ("问题", {"conversation_id": "short"}),
            ]:
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(ChatError) as caught:
                        chat(question, config=self.config, **kwargs)
                    self.assertEqual(caught.exception.stage, "input")
        transport.assert_not_called()

    def test_stream_config_and_retired_keys(self):
        """新开关严格加载，旧配置键和废弃分组明确拒绝且不泄露值。"""
        identity = '[adp]\napp_key="test-key"\nuser_id="tester"\n'
        for enabled in (False, True):
            self.config_path.write_text(identity + f'[chat]\nstream={str(enabled).lower()}\n', encoding="utf-8")
            self.assertIs(load_config(self.config_path).stream, enabled)
        self.assertFalse(ChatConfig("test-key", "tester").stream)
        for retired in (
            '[chat]\nincremental=true\n', '[chat]\ndetect_text_clarification=true\n',
            '[chat]\nstructured_interaction=true\n', '[cli]\ninteractive=true\n',
            '[attachments]\nsecret_key="retired-secret"\n',
        ):
            with self.subTest(config=retired.splitlines()[0]):
                self.config_path.write_text(identity + retired, encoding="utf-8")
                with self.assertRaises(ChatError) as caught:
                    load_config(self.config_path)
                self.assertNotIn("retired-secret", str(caught.exception))

    def test_json_and_cards_remain_raw_reply_content(self):
        """只保留原始 JSON 与卡片内容，不恢复旧业务状态判断或自动补答。"""
        text = '{"type":"CLARIFICATION","status":"WAITING_USER","message":"选择城市"}'
        parts = [{"Type": "json_text", "Text": text}, {"Type": "widget", "Widget": {"State": "卡片"}},
                 {"Type": "questionnaire", "Questionnaire": {"Title": "问卷"}}]
        with patch("adp.sse.open_request", return_value=response("", 15, parts)):
            result = chat("问题", config=self.config)
        self.assertEqual(result.text, text)
        self.assertEqual(result.reply_method, 15)
        self.assertEqual(result.reply_contents, (tuple(parts),))

    def test_config_is_typed_and_rejects_unknown_keys_without_echoing_values(self):
        """配置解析器拒绝拼错的键，并隐藏凭据字段。"""
        self.assertNotIn("test-key", repr(self.config))
        self.config_path.write_text(
            '[adp]\napp_key = "test-key"\nuser_id = "tester"\n'
            '[chat]\nmisspelled = "secret-value"\n', encoding="utf-8",
        )
        with self.assertRaises(ChatError) as caught:
            load_config(self.config_path)
        self.assertEqual(caught.exception.stage, "config")
        self.assertNotIn("secret-value", str(caught.exception))

    def test_config_constructor_rejects_nonboolean_options_and_invalid_timeout(self):
        """Python 构造的配置执行与 TOML 相同的安全和超时校验。"""
        for value in ("false", 1):
            with self.subTest(value=value):
                with self.assertRaises(ChatError) as caught:
                    ChatConfig("test-key", "tester", stream=value)
                self.assertEqual(caught.exception.code, "invalid_type")
        with self.assertRaises(ChatError) as caught:
            ChatConfig("test-key", "tester", chat_timeout_seconds=1e100)
        self.assertEqual(caught.exception.code, "invalid_timeout")
        with self.assertRaises(ChatError) as caught:
            load_config("invalid\0path")
        self.assertEqual(caught.exception.code, "unreadable")
