import contextlib
import io
import json
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from adp_chat import ChatConfig, chat, chat_until_complete
from test_clarification import response


# 正例是直接等待本次用户作答，反例是已回答或只展示/建议，不以问号作判据。
POSITIVE = (
    "好的，报销方面你想了解哪一块呢？比如：费用报销流程与单据要求、Concur 报销操作或报错、公务卡办理与还款、发票与进项税、差旅住宿与餐费标准、讲课费，还是个税与年度汇算？告诉我具体方向，我马上帮你转给财务助手。",
    "请问您想了解差旅费报销的哪方面？例如：报销标准、所需单据与发票要求、还是系统操作流程？",
    "你问的是电脑/账号、报销，还是采购？告诉我涉及的系统和具体要做的操作，我好帮你转过去对应同事。",
    "请提供操作系统和完整错误提示。",
    "为了进一步定位问题，我还需要知道：您使用的是 Windows 还是 macOS？",
    "你使用的是哪个版本的 VPN 客户端？",
    "方便告诉我具体的报错内容吗？",
    "请先补充以下信息：\n1. 使用的操作系统\n2. 完整错误提示",
    "发票是电子发票还是纸质发票？",
    "具体出现了什么错误？",
    "需要您确认一下：是否同意继续查询？",
    "截图有点模糊。请上传清晰的报错截图，我才能继续排查。",
    "还缺少出差日期，请补充后我再给出对应标准。",
    "请问需要查询哪个城市的天气？",
    "您想了解报销标准、发票要求，还是系统操作？",
    "请先选择要查询的业务：IT、财务或采购。",
    "Which operating system are you using, Windows or macOS?",
    "Could you provide the exact error message?",
)

NEGATIVE = (
    "请上传电子发票，再提交报销单。",
    "请提供发票，并发送给财务审核。",
    "您想了解更多细节吗？",
    "请告诉我是否需要其他帮助。",
    "请问还有什么可以帮您？",
    "您需要我继续介绍操作流程吗？",
    "请发送邮件至 help@example.com。",
    "提供发票即可报销。",
    "请提供发票即可完成报销。",
    "报销标准是600元，您想了解哪个方面？",
    "差旅报销请登录 Concur，创建报销单并上传合规发票。",
    "为什么 VPN 无法连接？通常是网络或账号权限问题。请先检查网络。",
    "报销需要哪些材料？需要发票、行程单和审批记录。",
    "你可以输入“为什么？”查询原因。",
    "示例：请提供操作系统和错误信息。",
    "引用原文：\n> 请问您想了解哪方面？",
    '原句是“请提供报错信息”。这是一句请求。',
    '```text\n请提供报错信息。\n```',
    "`请问您使用什么系统？` 是模板内容。",
    "还需要其他帮助吗？",
    "您还有其他问题吗？",
    "如果仍然无法连接，请提供报错信息。",
    "如需进一步帮助，请告诉我具体情况。",
    "欢迎随时告诉我其他需求。",
    "您可以提供截图给 IT 服务台。",
    "请提供发票给财务审核。",
    "请告诉财务您的出差日期。",
    "请确认发票抬头与公司名称一致。",
    "请选择报销类型后提交。",
    "报销标准为每天600元。您还想了解哪个方面？",
    "请问您想了解哪个方面？这是客服接待时常用的话。",
    "请注意，报销时需要提供发票。",
    "无需补充信息，材料齐全后即可提交。",
    "建议你先确认网络是否可用。",
    "你知道为什么吗？因为账号没有开通 VPN 权限。",
    "能提供发票吗？当然可以，电子发票和纸质发票都支持。",
    "流程如下：\n1. 请提供发票。\n2. 请在 Concur 提交。",
    "要查询天气，请先选择城市，再点击查询。",
    "请登录 VPN 后填写账号密码。",
    "如果缺少发票，请告诉我。",
    "我需要先补充材料才能报销。",
    "Please provide the invoice to Finance for approval.",
    "If you need further help, could you provide the error message?",
    "Would you like anything else?",
    "How to submit expenses? Sign into Concur and upload the invoice.",
)


class TextClarificationTests(unittest.TestCase):
    def setUp(self):
        """使用不含真实凭据的配置测试用户可见的判定和续答行为。"""
        self.config = ChatConfig("fixture-key", "text-clarification-test", log_enabled=False)

    def test_direct_text_requests_are_detected(self):
        """识别不依赖问号的直接补充要求和原始真实漏判句式。"""
        for text in POSITIVE:
            with self.subTest(text=text), patch("adp_chat.sse.open_request", return_value=response(text, 18)):
                result = chat("我需要帮助", config=self.config)
                self.assertTrue(result.needs_input, text)
                self.assertEqual(result.clarification_source, "text_rule")

    def test_normal_answers_do_not_call_answer_provider(self):
        """正常答案、引用、步骤和可选邀请均不能导致额外用户输入或请求。"""
        for text in NEGATIVE:
            provider = Mock(side_effect=AssertionError("正常答案不应询问用户"))
            with self.subTest(text=text), patch("adp_chat.sse.open_request", return_value=response(text, 18)) as network:
                result = chat_until_complete("问题", config=self.config, answer_provider=provider)
                self.assertFalse(result.needs_input, text)
                self.assertIsNone(result.clarification_source)
                provider.assert_not_called()
                self.assertEqual(network.call_count, 1)

    def test_generated_question_is_an_answer_to_the_original_task(self):
        """用户要求生成/翻译疑问句时，输出疑问句不意味着向当前用户追问。"""
        for question, text in (
            ("写一个让用户补充城市的问题", "请告诉我需要查询哪个城市？"),
            ("翻译成中文：Could you provide the error message?", "请提供完整的错误提示。"),
            ("请改写这句客服话术：请告诉我你的系统", "方便告诉我你使用什么系统吗？"),
            ("请将这句话转换为礼貌表达：告诉我系统版本", "请告诉我系统版本。"),
            ("请把这两个客服追问列成清单：您用什么系统？请提供错误截图。", "- 您用什么系统？\n- 请提供错误截图。"),
        ):
            with self.subTest(question=question), patch("adp_chat.sse.open_request", return_value=response(text, 18)):
                self.assertFalse(chat(question, config=self.config).needs_input)

    def test_instructions_answer_the_question_without_requesting_a_reply(self):
        """结合用户原始任务区分材料/操作说明与需要当前用户回传的信息。"""
        for question, text in (
            ("报销需要提供什么材料？", "请提供发票。"),
            ("怎么报销？", "请上传电子发票。"),
            ("如何提交工单？", "请上传错误截图。"),
        ):
            with self.subTest(question=question), patch("adp_chat.sse.open_request", return_value=response(text, 18)):
                self.assertFalse(chat(question, config=self.config).needs_input)

    def test_agent_actions_after_collecting_input_are_not_user_instructions(self):
        """后续从句的执行者是助手时，保留真正的信息收集请求。"""
        text = "请提供报错截图，收到截图后我再帮您提交给 IT 排查。"
        with patch("adp_chat.sse.open_request", return_value=response(text, 18)):
            self.assertTrue(chat("VPN 连不上", config=self.config).needs_input)

    def test_task_keywords_in_product_names_do_not_disable_detection(self):
        """模板和翻译出现在故障对象名称时，不等于用户在要求生成作品。"""
        for question in ("报销模板下载失败", "翻译软件连不上"):
            with self.subTest(question=question), patch("adp_chat.sse.open_request", return_value=response("请提供完整的错误提示。", 18)):
                self.assertTrue(chat(question, config=self.config).needs_input)

    def test_instruction_topics_still_allow_missing_information(self):
        """流程主题不应否决业务选择或故障排查中的真正补充请求。"""
        for question, text in (
            ("我想了解报销流程", "请先选择要查询的业务：IT、财务或采购。"),
            ("报销流程走到登录时出错", "请提供完整错误提示。"),
        ):
            with self.subTest(question=question), patch("adp_chat.sse.open_request", return_value=response(text, 18)):
                self.assertTrue(chat(question, config=self.config).needs_input)

    def test_original_task_survives_clarification_answers(self):
        """续轮保留原始翻译任务，不能把正常译文再当成向用户的信息请求。"""
        question = "请翻译这句英文：Could you provide your operating system and the exact error message?"
        for wrapped in (False, True):
            with self.subTest(wrapped=wrapped), patch("adp_chat.sse.open_request", side_effect=[
                response("请问您需要直译还是意译？", 15), response("请提供操作系统和完整错误信息。", 18),
            ]):
                provider = Mock(side_effect=["直译"])
                if wrapped:
                    final = chat_until_complete(question, config=self.config, answer_provider=provider)
                    provider.assert_called_once()
                else:
                    pending = chat(question, config=self.config)
                    final = chat("直译", config=self.config, reply_to=pending)
                self.assertFalse(final.needs_input)
                self.assertEqual(final.original_question, question)

    def test_acknowledgement_does_not_start_another_input_round(self):
        """用户已经明确结束时，可选扩展邀请不是未完成任务的追问。"""
        with patch("adp_chat.sse.open_request", return_value=response("您需要我继续介绍操作流程吗？", 18)):
            self.assertFalse(chat("谢谢，明白了", config=self.config).needs_input)

    def test_text_detection_can_be_disabled_without_disabling_protocol(self):
        """关闭推断仍保留平台明确的澄清标记。"""
        config = replace(self.config, detect_text_clarification=False)
        with patch("adp_chat.sse.open_request", side_effect=[response(POSITIVE[0], 18), response("请补充", 15)]):
            self.assertFalse(chat("问题", config=config).needs_input)
            explicit = chat("问题", config=config)
        self.assertTrue(explicit.needs_input)
        self.assertEqual(explicit.clarification_source, "reply_method")

    def test_three_input_modes_continue_and_do_not_upload_again(self):
        """文字追问对三类输入生效，补答可再次追问，正常结果立即停止。"""
        for attachment in ({}, {"image": "fixture.jpeg"}, {"file": "fixture.txt"}):
            with (
                self.subTest(attachment=attachment),
                patch("adp_chat.attachments.prepare_attachment", return_value={"Type": "fixture"}) as upload,
                patch("adp_chat.sse.open_request", side_effect=[
                    response("请提供操作系统。", 18), response("请补充具体报错。", 18),
                    response("请先重新连接 VPN，再登录系统。", 18),
                ]) as network,
            ):
                results = []
                provider = Mock(side_effect=["Windows", "登录超时"])
                final = chat_until_complete("VPN 连不上", config=self.config, answer_provider=provider, on_result=results.append, **attachment)
            self.assertFalse(final.needs_input)
            self.assertEqual(provider.call_count, 2)
            self.assertEqual(upload.call_count, int(bool(attachment)))
            self.assertEqual([item.input_kind for item in results], ["question", "clarification_answer", "clarification_answer"])
            payloads = [json.loads(call.args[0].data) for call in network.call_args_list]
            self.assertEqual(len({item["ConversationId"] for item in payloads}), 1)
            self.assertEqual(payloads[1]["Contents"], [{"Type": "text", "Text": "Windows"}])
            self.assertEqual(payloads[2]["Contents"], [{"Type": "text", "Text": "登录超时"}])

    def test_text_request_with_display_widget_uses_text_input(self):
        """文本推断不得把同轮展示卡片误当作需要提交的交互卡片。"""
        from adp_chat.cli import main

        with (
            patch("adp_chat.cli.load_config", return_value=self.config),
            patch("builtins.input", side_effect=["Windows"]) as user_input,
            patch("adp_chat.sse.open_request", side_effect=[
                response("请提供操作系统。", 18, [{"Type": "widget", "Widget": {"WidgetId": "display", "WidgetRunId": "run"}}]),
                response("请重新连接 VPN。", 18),
            ]) as network,
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()) as prompts,
        ):
            self.assertEqual(main(["--ask", "VPN 连不上"]), 0)
        user_input.assert_called_once()
        self.assertNotIn("卡片动作 JSON", prompts.getvalue())
        self.assertEqual(network.call_count, 2)
