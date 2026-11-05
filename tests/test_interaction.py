import json
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from adp_chat import ChatConfig, ChatError, chat, chat_until_complete
from test_chat import FakeStream, event
from test_clarification import response


def envelope(kind, status, message, fields=(), confirmation=False):
    """构造本项目明确约定的业务状态，不把它当成腾讯云原生字段。"""
    return json.dumps({"schema": "adp-chat.interaction.v1", "interaction": {
        "type": kind, "status": status, "message": message,
        "missing_fields": list(fields), "confirmation_required": confirmation,
    }}, ensure_ascii=False)


class InteractionTests(unittest.TestCase):
    def setUp(self):
        """显式开启应用已约定结构化输出的模式。"""
        self.config = ChatConfig("fixture-key", "interaction-test", structured_interaction=True, log_enabled=False)

    def test_business_status_overrides_question_like_text_and_preserves_method(self):
        """业务声明已回答时，即使正文像追问也不额外询问。"""
        answer = Mock(side_effect=AssertionError("不应取答案"))
        with patch("adp_chat.sse.open_request", return_value=response(envelope("ANSWER", "COMPLETED", "请提供发票。"), 18)):
            result = chat_until_complete("报销需要什么材料", config=self.config, answer_provider=answer)
        self.assertEqual(result.reply_method, 18)
        self.assertEqual(result.text, "请提供发票。")
        self.assertFalse(result.needs_input)
        self.assertEqual(result.interaction.status, "COMPLETED")
        answer.assert_not_called()

    def test_business_clarification_in_json_text_continues_text(self):
        """文字按业务状态补答，18来源不被改写为15，正文无需疑问句。"""
        first = {"Status": "success", "ExtraInfo": {"ReplyMethod": 18}, "Messages": [{"Type": "reply", "Contents": [
            {"Type": "json_text", "Text": envelope("CLARIFICATION", "WAITING_USER", "需要费用类型", ["expense_type"])},
        ]}]}
        with (
            patch("adp_chat.sse.open_request", side_effect=[
                FakeStream(event("response.completed", Response=first)),
                response(envelope("ANSWER", "COMPLETED", "报销说明"), 18),
            ]) as network,
        ):
            seen = []
            final = chat_until_complete("我要报销", config=self.config, answer_provider=lambda _: "差旅", on_result=seen.append)
            self.assertEqual(seen[0].clarification_source, "interaction")
            self.assertEqual(seen[0].interaction.missing_fields, ("expense_type",))
            self.assertEqual([item.reply_method for item in seen], [18, 18])
            self.assertEqual(final.input_kind, "clarification_answer")
            self.assertEqual(final.text, "报销说明")
            payloads = [json.loads(call.args[0].data) for call in network.call_args_list]
            self.assertEqual(payloads[0]["ConversationId"], payloads[1]["ConversationId"])

    def test_confirmation_waits_for_human_and_never_invents_consent(self):
        """操作确认保留独立类型，只向调用方要答案，不默认同意。"""
        with patch("adp_chat.sse.open_request", return_value=response(envelope("CONFIRMATION", "WAITING_USER", "确认继续查询", confirmation=True), 18)):
            result = chat("查询", config=self.config)
        self.assertTrue(result.needs_input)
        self.assertEqual(result.interaction.type, "CONFIRMATION")
        self.assertTrue(result.interaction.confirmation_required)

    def test_missing_malformed_or_inconsistent_status_fails_without_text_guessing(self):
        """结构化模式契约缺失、损坏或自相矛盾时明确失败，避免假报业务完成。"""
        for text in (
            "请提供报错信息。", "{bad json}",
            envelope("ANSWER", "WAITING_USER", "已回答"),
            envelope("CONFIRMATION", "WAITING_USER", "确认", confirmation=False),
            envelope("ANSWER", "COMPLETED", "回答", ["unneeded"]),
            envelope("CLARIFICATION", "COMPLETED", "追问"),
        ):
            with self.subTest(text=text), patch("adp_chat.sse.open_request", return_value=response(text, 18)):
                with self.assertRaises(ChatError) as caught:
                    chat("问题", config=self.config)
                self.assertEqual(caught.exception.code, "invalid_interaction")

    def test_native_questionnaire_remains_supported(self):
        """应用走原生问卷分支时，结构化模式仍可保留并处理原生交互。"""
        with patch("adp_chat.sse.open_request", return_value=response("", 18, [{"Type": "questionnaire", "Questionnaire": {"Questions": [{"Question": "费用类型"}]}}])):
            result = chat("报销", config=self.config)
        self.assertTrue(result.needs_input)
        self.assertEqual(result.clarification_source, "questionnaire")

    def test_legacy_mode_does_not_treat_example_json_as_business_control(self):
        """未声明云端契约的普通应用中，JSON示例保持作品内容。"""
        config = replace(self.config, structured_interaction=False, detect_text_clarification=False)
        text = envelope("CLARIFICATION", "WAITING_USER", "测试示例")
        with patch("adp_chat.sse.open_request", return_value=response(text, 18)):
            result = chat("输出一个JSON示例", config=config)
        self.assertIsNone(result.interaction)
        self.assertFalse(result.needs_input)
        self.assertEqual(result.text, text)
