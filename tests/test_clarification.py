import contextlib
import io
import json
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from adp.client import chat, chat_until_complete
from core.config import ChatConfig
from core.models import ChatError, ClarificationAnswer
from test_chat import FakeStream, event


def response(text, reply_method=1, extra_contents=()):
    """构造带有正式回复方式标记的一轮 SSE 响应。"""
    contents = [{"Type": "text", "Text": text}] if text else []
    return FakeStream(event("response.completed", Response={
        "Status": "success",
        "ExtraInfo": {"ReplyMethod": reply_method},
        "Messages": [{"Type": "reply", "Contents": contents + list(extra_contents)}],
    }))


class ClarificationTests(unittest.TestCase):
    def setUp(self):
        """为多轮协议回归准备不含真实凭据的配置。"""
        self.config = ChatConfig("fixture-key", "clarification-user", log_enabled=False)

    def test_normal_answer_does_not_request_user_input_even_with_question_mark(self):
        """普通答案即使含问号也应直接结束，不能误触发追问。"""
        answer = Mock(side_effect=AssertionError("不应询问用户"))
        with patch("adp.sse.open_request", return_value=response("你可以输入“为什么？”查询原因。")):
            result = chat_until_complete("问题", config=self.config, answer_provider=answer)
        self.assertFalse(result.needs_input)
        self.assertEqual(result.input_kind, "question")
        answer.assert_not_called()

    def test_text_resumes_only_when_adp_requests_clarification(self):
        """文字输入按协议追问，续轮复用会话且每轮请求 ID 独立。"""
        supplied = Mock(side_effect=["北京", "明天"])
        results = []
        with (
            patch("adp.sse.open_request", side_effect=[
                response("请补充城市", 15), response("请补充日期", 15), response("最终答案"),
            ]) as transport,
        ):
            result = chat_until_complete(
                "首次问题", config=self.config, answer_provider=supplied,
                on_result=results.append,
            )
        payloads = [json.loads(call.args[0].data) for call in transport.call_args_list]
        self.assertEqual(len({item["ConversationId"] for item in payloads}), 1)
        self.assertEqual(len({item["RequestId"] for item in payloads}), 3)
        self.assertEqual(payloads[1]["Contents"], [{"Type": "text", "Text": "北京"}])
        self.assertEqual(payloads[2]["Contents"], [{"Type": "text", "Text": "明天"}])
        self.assertEqual([item.needs_input for item in results], [True, True, False])
        self.assertEqual([item.input_kind for item in results], [
            "question", "clarification_answer", "clarification_answer",
        ])
        self.assertEqual(result.text, "最终答案")
        self.assertEqual(supplied.call_count, 2)

    def test_questionnaire_without_plain_text_is_exposed_as_pending(self):
        """结构化问卷必须保留其问题和选项，供调用方作答。"""
        questionnaire = {"Title": "补充信息", "Questions": [
            {"Question": "选择城市", "Type": 1, "Required": True, "Options": [
                {"Label": "北京"}, {"Label": "上海"},
            ]},
        ]}
        with patch("adp.sse.open_request", return_value=response("", 18, [
            {"Type": "questionnaire", "Questionnaire": questionnaire},
        ])):
            result = chat("问题", config=self.config)
        self.assertTrue(result.needs_input)
        self.assertEqual(result.questionnaires, (questionnaire,))

    def test_questionnaire_echo_and_output_widget_do_not_prompt(self):
        """已填问卷回显和普通展示卡片都不能再次触发作答。"""
        answer = Mock(side_effect=AssertionError("不应再取答案"))
        questionnaire = {
            "Questions": [{"Question": "城市"}],
            "Answers": [{"Question": "城市", "SelectedLabels": ["北京"]}],
        }
        with patch("adp.sse.open_request", return_value=response("完成", 18, [
            {"Type": "questionnaire", "Questionnaire": questionnaire},
            {"Type": "widget", "Widget": {"WidgetId": "output", "WidgetRunId": "run"}},
        ])):
            result = chat_until_complete("问题", config=self.config, answer_provider=answer)
        self.assertFalse(result.needs_input)
        self.assertEqual(result.questionnaires, ())
        self.assertEqual(result.widgets[0]["WidgetId"], "output")
        answer.assert_not_called()

    def test_questionnaire_console_collects_single_and_multiple_choices(self):
        """终端保留未填题目并校验单选和多选，将选择汇总为文本续答。"""
        from cli.console import read_clarification

        questionnaire = {"Questions": [
            {"Question": "已填题"},
            {"Question": "城市", "Type": 1, "Required": True, "Options": [{"Label": "北京"}, {"Label": "上海"}]},
            {"Question": "日期", "Type": 2, "Required": True, "Options": [{"Label": "今天"}, {"Label": "明天"}]},
        ], "Answers": [{"Question": "已填题", "SelectedLabels": ["原答案"]}]}
        with (
            patch("builtins.input", side_effect=["1,2", "2", "1，2"]),
            contextlib.redirect_stderr(io.StringIO()) as output,
            patch("adp.sse.open_request", side_effect=[
                response("", 18, [{"Type": "questionnaire", "Questionnaire": questionnaire}]),
                response("完成"),
            ]) as transport,
        ):
            result = chat_until_complete("问题", config=self.config, answer_provider=read_clarification)
        payload = json.loads(transport.call_args_list[1].args[0].data)
        self.assertEqual(payload["Contents"], [{"Type": "text", "Text": "城市：上海\n日期：今天、明天"}])
        self.assertNotIn("已填题", output.getvalue())
        self.assertIn("请填写有效选项序号", output.getvalue())
        self.assertFalse(result.needs_input)

    def test_widget_action_preserves_provider_contract(self):
        """调用方提供实际动作名称，组件按 V2 结构提交给原会话。"""
        widget = {"WidgetId": "widget-config", "WidgetRunId": "widget-instance", "View": "{}", "State": "请确认"}
        action = {"WidgetId": "widget-config", "WidgetRunId": "widget-instance", "ActionType": "fixture-action", "Payload": '{"city":"北京"}'}
        with patch("adp.sse.open_request", side_effect=[
            response("", 15, [{"Type": "widget", "Widget": widget}]), response("完成"),
        ]) as transport:
            result = chat_until_complete(
                "问题", config=self.config,
                answer_provider=lambda _: ClarificationAnswer(widget_actions=(action,)),
            )
        payloads = [json.loads(call.args[0].data) for call in transport.call_args_list]
        self.assertEqual(payloads[1]["Contents"], [{"Type": "widget_action", "WidgetAction": action}])
        self.assertEqual(payloads[0]["ConversationId"], payloads[1]["ConversationId"])
        self.assertEqual(result.input_kind, "clarification_answer")

    def test_invalid_continuation_is_rejected_before_network(self):
        """跨会话误答或提交其他卡片动作必须在外部副作用之前失败。"""
        with patch("adp.sse.open_request", return_value=response("补充信息", 15)):
            pending = chat("问题", config=self.config)
        with patch("adp.sse.open_request") as network:
            with self.assertRaises(ChatError) as caught:
                chat("答案", config=self.config, reply_to=pending, conversation_id="a" * 32)
            self.assertEqual(caught.exception.code, "conflicting_conversation")
            with self.assertRaises(ChatError) as caught:
                chat(ClarificationAnswer(widget_actions=({
                    "WidgetId": "wrong", "WidgetRunId": "wrong", "ActionType": "fixture", "Payload": "{}",
                },)), config=self.config, reply_to=pending)
            self.assertEqual(caught.exception.code, "invalid_answer")
        network.assert_not_called()

    def test_callback_cancellation_keeps_exception_and_stops_network(self):
        """调用方取消原样传播，不修改异常、不重试、不再发送补充回答。"""
        error = ChatError("caller", "cancel", "调用方取消")
        error.request_id = "caller-id"
        with patch("adp.sse.open_request", return_value=response("补充城市", 15)) as transport:
            with self.assertRaises(ChatError) as caught:
                chat_until_complete("问题", config=self.config, answer_provider=Mock(side_effect=error))
        self.assertIs(caught.exception, error)
        self.assertEqual(error.request_id, "caller-id")
        self.assertEqual(transport.call_count, 1)

    def test_unmarked_agent_question_is_recognized_as_text_inference(self):
        """新要求覆盖真实漏判句式，推断来源与正式协议状态明确区分。"""
        with patch("adp.sse.open_request", return_value=response("请问想了解报销标准，还是系统流程？", 18)):
            result = chat("我想报销", config=self.config)
        self.assertTrue(result.needs_input)
        self.assertEqual(result.clarification_source, "text_rule")

    def test_questionnaire_takes_priority_over_display_widget(self):
        """同轮存在明确问卷和展示卡片时，按问卷收集而非索取卡片动作。"""
        from cli.console import read_clarification

        questionnaire = {"Questions": [{"Question": "城市", "Required": True}]}
        with (
            patch("adp.sse.open_request", return_value=response("", 18, [
                {"Type": "questionnaire", "Questionnaire": questionnaire},
                {"Type": "widget", "Widget": {"WidgetId": "display", "WidgetRunId": "run"}},
            ])),
            patch("builtins.input", side_effect=["北京", "/exit"]),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            result = chat("问题", config=self.config)
            self.assertEqual(read_clarification(result), "城市：北京")
