import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from adp_chat import ChatConfig, ChatError, chat, load_config


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
        """同一会话跨轮保持身份和会话 ID，并生成独立的请求 ID。"""
        with patch("adp_chat.adp.sse.open_request", side_effect=[
            FakeStream(completed()), FakeStream(completed("第二轮")),
        ]) as transport:
            first = chat("第一轮", config=self.config)
            second = chat("第二轮", config=self.config, conversation_id=first.conversation_id)

        first_request, second_request = [
            json.loads(call.args[0].data) for call in transport.call_args_list
        ]
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

    def test_stream_preview_filters_messages_and_exposes_replacements(self):
        """预览只交付回复消息，替换事件保留位置和替换语义。"""
        stream = FakeStream(
            b"\xef\xbb\xbf: heartbeat\r\n\r\n"
            + event("text.delta", MessageId="thought", ContentIndex=0, Text="思考")
            + event("message.added", Message={"Type": "reply", "MessageId": "reply"})
            + event("text.delta", MessageId="reply", ContentIndex=0, Text="初稿")
            + event("text.replace", MessageId="reply", ContentIndex=0, Text="修订稿")
            + completed()
        )
        preview = []
        with patch("adp_chat.adp.sse.open_request", return_value=stream):
            result = chat("问题", config=self.config, on_preview=preview.append)

        self.assertEqual([(item.kind, item.text) for item in preview], [
            ("delta", "初稿"), ("replace", "修订稿"),
        ])
        self.assertEqual(preview[-1].message_id, "reply")
        self.assertEqual(preview[-1].content_index, 0)
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
                with patch("adp_chat.adp.sse.open_request", return_value=stream):
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
        with patch("adp_chat.adp.sse.open_request", return_value=stream):
            self.assertEqual(chat("问题", config=self.config).text, "完整回答")

    def test_preview_callback_error_keeps_caller_context_and_closes_stream(self):
        """回调异常原样返还，已打开的 SSE 响应关闭。"""
        stream = FakeStream(
            event("message.added", Message={"Type": "reply", "MessageId": "reply"})
            + event("text.delta", MessageId="reply", ContentIndex=0, Text="预览")
        )
        error = ChatError("caller", "cancelled", "调用方停止")
        error.request_id = "caller-request"

        def stop(_):
            """模拟调用方在收到预览时主动停止。"""
            raise error

        with patch("adp_chat.adp.sse.open_request", return_value=stream):
            with self.assertRaises(ChatError) as caught:
                chat("问题", config=self.config, on_preview=stop)
        self.assertIs(caught.exception, error)
        self.assertEqual(error.request_id, "caller-request")
        self.assertTrue(stream.closed)

    def test_diagnostic_keeps_provider_request_id_and_trace_id_distinct(self):
        """服务端请求和跟踪标识保留各自的字段。"""
        stream = FakeStream(event("error", Error={
            "Code": 460501, "RequestId": "provider-request", "TraceId": "trace-id",
        }))
        with patch("adp_chat.adp.sse.open_request", return_value=stream):
            with self.assertRaises(ChatError) as caught:
                chat("问题", config=self.config)
        self.assertEqual(caught.exception.provider_request_id, "provider-request")
        self.assertEqual(caught.exception.provider_trace_id, "trace-id")

    def test_dedicated_model_concurrency_error_is_actionable_without_retry(self):
        """真实460701错误应直接说明并发限制，同时保留原错误分类与关联ID。"""
        stream = FakeStream(event("error", Error={
            "Code": 460701, "Message": "test-key", "RequestId": "server-request", "TraceId": "server-trace",
        }))
        with patch("adp_chat.adp.sse.open_request", return_value=stream) as network:
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
        with patch("adp_chat.adp.sse.open_request") as transport:
            for question, kwargs in [
                ("", {}), ("问题", {"conversation_id": "short"}),
            ]:
                with self.subTest(kwargs=kwargs):
                    with self.assertRaises(ChatError) as caught:
                        chat(question, config=self.config, **kwargs)
                    self.assertEqual(caught.exception.stage, "input")
        transport.assert_not_called()

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
                    ChatConfig("test-key", "tester", incremental=value)
                self.assertEqual(caught.exception.code, "invalid_type")
        with self.assertRaises(ChatError) as caught:
            ChatConfig("test-key", "tester", chat_timeout_seconds=1e100)
        self.assertEqual(caught.exception.code, "invalid_timeout")
        with self.assertRaises(ChatError) as caught:
            load_config("invalid\0path")
        self.assertEqual(caught.exception.code, "unreadable")
