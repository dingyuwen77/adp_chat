import contextlib
import io
import json
import logging
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import patch

from adp.client import chat
from core.config import ChatConfig, load_config
from core.models import ChatError
from core.logging import diagnostic_logger, log_conversation, log_writer_metrics, parse_log_line
from test_chat import FakeStream, event, response


class LoggingTests(unittest.TestCase):
    def setUp(self):
        """准备可清理的独立日志目录，避免把测试诊断混入用户运行日志。"""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config = ChatConfig(
            "fixture-app-secret", "logging-test",
            log_file=str(self.root / "logs" / "log"), log_level="DEBUG",
            log_question=True, log_answer_chars=100,
        )

    def test_every_text_log_line_can_be_converted_to_one_document(self):
        """普通诊断与网关事件共用可读前缀，正文与字段只有一个分隔符。"""
        from core.events import GatewayEvents
        with diagnostic_logger(self.config) as logger:
            logger.info("请求开始 request_id=abc elapsed_ms=12")
            logger.warning("异常行\\n诊断说明 | 不是字段")
            events = GatewayEvents(self.config, logger)
            events.emit("service.started")
        lines = Path(self.config.log_file).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(line.startswith("[") and "record=" not in line for line in lines))
        records = [parse_log_line(line) for line in lines]
        self.assertEqual(records[2]["event"], "service.started")
        self.assertEqual(records[2]["message"], "服务启动完成")
        self.assertEqual(records[2]["status"], "success")
        self.assertNotIn(" | ", lines[1].split("[WARNING] ", 1)[1])
        self.assertRaises(ValueError, parse_log_line, "[old format] [INFO] arbitrary")

    def test_chinese_message_and_field_roundtrip(self):
        with diagnostic_logger(self.config) as logger:
            logger.info("调用失败：上游响应不完整", extra={"log_fields": {
                "request_id": "abc", "error_code": "E=42", "elapsed_ms": 12.5,
                "reason": "包含 | 引号\\n换行", "outcome_uncertain": True}})
        line = Path(self.config.log_file).read_text(encoding="utf-8").strip()
        doc = parse_log_line(line)
        self.assertEqual(doc["message"], "调用失败：上游响应不完整")
        self.assertEqual(doc["error_code"], "E=42")
        self.assertEqual(doc["elapsed_ms"], 12.5)
        self.assertTrue(doc["outcome_uncertain"])
        self.assertEqual(doc["reason"], "包含 | 引号\\n换行")
        self.assertNotIn("record=", line)

    def test_chat_logs_question_and_answer_only_to_file_and_preserves_host_logging(self):
        """按用户新要求记录问答，保持关联标识、密钥脱敏和控制台隔离。"""
        handlers = list(logging.getLogger().handlers)
        with (
            contextlib.redirect_stdout(io.StringIO()) as stdout,
            contextlib.redirect_stderr(io.StringIO()) as stderr,
            patch("adp.sse.open_request", return_value=response("请提供完整报错。", 18)),
        ):
            result = chat("private-question-sentinel", config=self.config)
        content = Path(self.config.log_file).read_text(encoding="utf-8")
        self.assertIn(result.request_id, content)
        self.assertIn(result.conversation_id, content)
        self.assertIn("stream=False", content)
        self.assertIn("ADP SSE响应完成", content)
        self.assertRegex(content, r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} client\.py L\d+\] \[INFO\]")
        self.assertIn('question="private-question-sentinel"', content)
        self.assertIn('answer="请提供完整报错。"', content)
        for value in (self.config.app_key,):
            self.assertNotIn(value, content)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(handlers, logging.getLogger().handlers)

    def test_full_question_and_full_answer_despite_legacy_preview_limit(self):
        """旧预览配置仍可加载，长问答按单行字段完整往返。"""
        question = "完整问题" + "问🙂" * 160 + "问题末尾"
        answer = "答🙂" * 70 + "回答末尾"
        with patch("adp.sse.open_request", return_value=response(answer, 18)):
            chat(question, config=self.config)
        content = Path(self.config.log_file).read_text(encoding="utf-8")
        records = [parse_log_line(line) for line in content.splitlines()]
        self.assertEqual([record["question"] for record in records if "question" in record], [question])
        self.assertEqual([record["answer"] for record in records if "answer" in record], [answer])

    def test_content_redaction_in_full_answer_and_single_line_escaping(self):
        """长回答中的凭据和 URL 签名仍脱敏，换行无法伪造下一行。"""
        question = f'排查\n[ERROR] forged\r\n{self.config.app_key}\u2028后续说明'
        answer = "答" * 95 + self.config.app_key + " https://example.com/file?q-signature=private-signature&token=private-token 回答末尾"
        with patch("adp.sse.open_request", return_value=response(answer, 18)):
            chat(question, config=self.config)
        content = Path(self.config.log_file).read_text(encoding="utf-8")
        for value in (self.config.app_key, "private-signature", "private-token", "fixtu"):
            self.assertNotIn(value, content)
        self.assertIn("\\n[ERROR] forged\\r\\n", content)
        self.assertIn("\\u2028", content)
        self.assertIn("回答末尾", content)
        self.assertTrue(all(line.startswith("[") for line in content.splitlines()))

    def test_signed_url_in_question_is_redacted_without_losing_question_tail(self):
        """完整问题保留非敏感部分，签名URL参数被移除，日志值仍是有效JSON字符串。"""
        question = '链接 "https://example.com/a?q-signature=private-signature&token=private-token" 无法访问，完整问题末尾'
        with patch("adp.sse.open_request", return_value=response("正常回答", 18)):
            chat(question, config=self.config)
        content = Path(self.config.log_file).read_text(encoding="utf-8")
        line = next(line for line in content.splitlines() if "记录用户问题 | " in line)
        logged = parse_log_line(line)["question"]
        self.assertIn("完整问题末尾", logged)
        self.assertIn("[REDACTED]", logged)
        self.assertNotIn("private-signature", content)
        self.assertNotIn("private-token", content)

    def test_escaped_json_urls_and_config_secrets_are_redacted_without_losing_fields(self):
        """json_text 内的转义属于 JSON 正文，先解码敏感子串再写出单行。"""
        answer = r'{"url":"https:\/\/example.com\/a?label=x\u0026token\u003dencoded-test-token\u3002请保留后文",'
        answer += r'"value":"\u0066ixture-app-secret","tail":"完整中文🙂"}'
        with patch("adp.sse.open_request", return_value=response(answer, 18)):
            chat("读 JSON", config=self.config)
        content = Path(self.config.log_file).read_text(encoding="utf-8")
        entries = [parse_log_line(line) for line in content.splitlines()]
        saved = json.loads(next(entry["answer"] for entry in entries if "answer" in entry))
        self.assertEqual(saved, {"url": "https://example.com/a?label=x&token=[REDACTED]。请保留后文",
                                 "value": "[REDACTED]", "tail": "完整中文🙂"})
        self.assertNotIn("encoded-test-token", content)
        self.assertNotIn(self.config.app_key, content)

    def test_ipv6_signed_url_is_also_redacted(self):
        """URL识别保留IPv6主机的闭合方括号，避免留下未匹配的签名参数。"""
        question = "链接 https://[::1]/file?q-signature=private-signature 无法读取"
        with patch("adp.sse.open_request", return_value=response("正常回答", 18)):
            chat(question, config=self.config)
        content = Path(self.config.log_file).read_text(encoding="utf-8")
        self.assertNotIn("private-signature", content)
        self.assertIn("无法读取", content)

    def test_parentheses_inside_urls_do_not_end_credential_redaction(self):
        """合法路径或查询括号不得提前结束URL匹配并留下后续凭据。"""
        for index, url in enumerate((
            "https://example.com/report(2026).pdf?token=fixture-token",
            "https://example.com/report?label=(x)&q-signature=fixture-signature",
            "https://example.com/a?token=fixture-token&label=(x)&sig=fixture-tail-signature",
        )):
            config = replace(self.config, log_file=str(self.root / f"parentheses-{index}.log"))
            with self.subTest(url=url), patch("adp.sse.open_request", return_value=response("正常回答", 18)):
                chat("链接 " + url + " 问题末尾", config=config)
                content = Path(config.log_file).read_text(encoding="utf-8")
                for secret in ("fixture-token", "fixture-signature", "fixture-tail-signature"):
                    self.assertNotIn(secret, content)
                self.assertIn("问题末尾", content)

    def test_url_punctuation_does_not_hide_later_credentials(self):
        """候选URL不能在合法内部标点处分段，完整检查路径后的query凭据。"""
        variants = [
            f"https://example.com/report{char}2026.pdf?token=fixture-token"
            for char in "!$&'()*+,;="
        ] + [
            "https://example.com/a?label='x'&q-signature=fixture-signature",
            "https://example.com/报表（2026）。pdf?token=fixture-token",
        ]
        for index, url in enumerate(variants):
            config = replace(self.config, log_file=str(self.root / f"punctuation-{index}.log"))
            with self.subTest(url=url), patch("adp.sse.open_request", return_value=response("正常回答", 18)):
                chat("链接 " + url + " 完整问题末尾", config=config)
                content = Path(config.log_file).read_text(encoding="utf-8")
                self.assertNotIn("fixture-token", content)
                self.assertNotIn("fixture-signature", content)
                self.assertIn("完整问题末尾", content)

    def test_url_fragment_tokens_are_redacted_together_with_query(self):
        """URL fragment中的访问令牌同样属于凭据，不因不在query中而遗漏。"""
        question = "链接 https://example.com/#access_token=fixture-fragment-token 读取失败"
        with patch("adp.sse.open_request", return_value=response("正常回答", 18)):
            chat(question, config=self.config)
        content = Path(self.config.log_file).read_text(encoding="utf-8")
        self.assertNotIn("fixture-fragment-token", content)
        self.assertIn("读取失败", content)

    def test_nested_url_credentials_and_public_url_text(self):
        """代理URL值中的签名也脱敏；普通公开URL保持原样，避免无关归一化。"""
        question = (
            "代理 https://example.com/proxy?url=https%3A%2F%2Fstorage.example%2Ffile%3Ftoken%3Dfixture-nested-token "
            "公开 HTTPS://example.com/search?q=ADP 完整问题末尾"
        )
        with patch("adp.sse.open_request", return_value=response("正常回答", 18)):
            chat(question, config=self.config)
        content = Path(self.config.log_file).read_text(encoding="utf-8")
        self.assertNotIn("fixture-nested-token", content)
        self.assertIn("HTTPS://example.com/search?q=ADP", content)
        self.assertIn("完整问题末尾", content)

    def test_chinese_question_after_signed_url_is_preserved(self):
        """只隐藏凭据参数，不删除URL后无空格接续的中文问题或后续链接。"""
        questions = (
            "这个链接 https://example.com/a?token=fixture-token，为什么打不开？请保留这段具体问题。",
            "下载地址是https://example.com/a?q-signature=fixture-signature。请分析权限还是时效问题。",
            "https://example.com/a?token=fixture-token，为什么打不开？另一个https://example.com/b?sig=fixture-signature。请检查。",
        )
        for index, question in enumerate(questions):
            config = replace(self.config, log_file=str(self.root / f"prose-{index}.log"))
            with self.subTest(question=question), patch("adp.sse.open_request", return_value=response("正常回答", 18)):
                chat(question, config=config)
                content = Path(config.log_file).read_text(encoding="utf-8")
                self.assertNotIn("fixture-token", content)
                self.assertNotIn("fixture-signature", content)
                if "为什么" in question:
                    self.assertIn("，为什么打不开？", content)
                if "请保留" in question:
                    self.assertIn("请保留这段具体问题。", content)
                if "请分析" in question:
                    self.assertIn("。请分析权限还是时效问题。", content)
                if "请检查" in question:
                    self.assertIn("。请检查。", content)

    def test_encoded_credential_punctuation_is_not_preserved_as_prose(self):
        """凭据值内部的百分号编码仍属于值，不能解码后误当作中文正文保留。"""
        question = "https://example.com/a?token=fixture%E3%80%82private-token&q-ak=fixture-temporary-id，为什么失败？"
        with patch("adp.sse.open_request", return_value=response("正常回答", 18)):
            chat(question, config=self.config)
        content = Path(self.config.log_file).read_text(encoding="utf-8")
        self.assertNotIn("private-token", content)
        self.assertNotIn("fixture-temporary-id", content)
        self.assertIn("，为什么失败？", content)

    def test_answer_logging_can_be_disabled(self):
        """新字段关闭回答正文时仍记录请求完成，无空回答字段。"""
        config = replace(self.config, log_answer=False)
        with patch("adp.sse.open_request", return_value=response("不应记录的回答正文", 18)):
            chat("完整问题", config=config)
        content = Path(config.log_file).read_text(encoding="utf-8")
        self.assertNotIn("不应记录的回答正文", content)
        self.assertNotIn("answer=", content)
        self.assertIn("ADP请求完成", content)

    def test_rotation_utf8_and_last_defence_secret_redaction(self):
        """日志轮转可重新读取，并在格式化出口再次清除配置中的凭据。"""
        config = replace(self.config, log_max_bytes=256, log_backup_count=2)
        with diagnostic_logger(config) as logger:
            for _ in range(8):
                logger.info("凭据兜底测试 %s", config.app_key)
        paths = list(Path(config.log_file).parent.glob("log*"))
        self.assertGreater(len(paths), 1)
        self.assertLessEqual(len(paths), 3)
        for path in paths:
            content = path.read_text(encoding="utf-8")
            self.assertIn("[REDACTED]", content)
            self.assertNotIn(config.app_key, content)

    def test_background_queue_is_bounded_and_drains_kept_records(self):
        """文件 writer 暂停时，满队列不会阻塞生产者，关闭后已入队的完整行可解析。"""
        from core.logging import _FileHandler

        entered, release = Event(), Event()
        emit = _FileHandler.emit

        def held(sink, record):
            """真实轮转之前挂起写入，固定复现后台积压。"""
            entered.set()
            if not release.wait(5):
                raise AssertionError("未释放日志测试屏障")
            emit(sink, record)

        with (patch("core.logging._QUEUE_MAX_RECORDS", 1),
              patch.object(_FileHandler, "emit", held),
              self.assertLogs("adp_chat.event_alert", level="ERROR") as alerts,
              diagnostic_logger(self.config, background=True) as logger):
            try:
                log_conversation(logger, "question", "员工INPUT_EMP 中文🙂\n记录末尾", employee_id="INPUT_EMP")
                self.assertTrue(entered.wait(3))
                log_conversation(logger, "answer", "完整回答🙂" * 100)
                log_conversation(logger, "answer", "超出队列")
                self.assertEqual(log_writer_metrics(logger)["log_dropped"], 1)
            finally:
                release.set()
        entries = [parse_log_line(line) for line in Path(self.config.log_file).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(entries[0]["question"], "员工[REDACTED] 中文🙂\n记录末尾")
        self.assertEqual(entries[1]["answer"], "完整回答🙂" * 100)
        self.assertIn("diagnostic_log_queue_full", alerts.output[0])

    def test_background_full_text_restricts_legacy_active_log_before_rotation(self):
        """旧日志曾允许组或其他用户读取，新问答只能进入受限文件及其轮转。"""
        config = replace(self.config, log_max_bytes=400, log_backup_count=3)
        path = Path(config.log_file)
        path.parent.mkdir(parents=True)
        path.write_text("legacy\n", encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o644)
        with diagnostic_logger(config, background=True) as logger:
            for _ in range(5):
                log_conversation(logger, "answer", "完整回答🙂" * 100)
        files = list(path.parent.glob(path.name + "*"))
        self.assertGreater(len(files), 1)
        self.assertTrue(all("完整回答" in output.read_text(encoding="utf-8") for output in files))
        if os.name != "nt":
            self.assertTrue(all(output.stat().st_mode & 0o077 == 0 for output in files))

    def test_disabled_logging_does_not_create_file(self):
        """调用方可关闭诊断写入，不产生任何日志目录。"""
        config = replace(self.config, log_enabled=False)
        with patch("adp.sse.open_request", return_value=response("正常答案", 18)):
            chat("问题", config=config)
        self.assertFalse(Path(config.log_file).exists())

    def test_unwritable_destination_fails_before_adp_request(self):
        """日志目标是目录等不可写文件时，在真实请求之前明确报错。"""
        config = replace(self.config, log_file=str(self.root))
        with patch("adp.sse.open_request") as network:
            with self.assertRaises(ChatError) as caught:
                chat("问题", config=config)
        self.assertEqual(caught.exception.stage, "logging")
        network.assert_not_called()

    def test_provider_failure_retains_safe_correlation_without_raw_error(self):
        """服务端失败保留各自关联标识，原始错误消息不写入日志。"""
        with patch("adp.sse.open_request", return_value=FakeStream(event("error", Error={
            "Code": "fixture-code", "RequestId": "server-request", "TraceId": "server-trace", "Message": "private-error-body",
        }))):
            with self.assertRaises(ChatError):
                chat("问题", config=self.config)
        content = Path(self.config.log_file).read_text(encoding="utf-8")
        self.assertIn("provider_request_id=server-request", content)
        self.assertIn("provider_trace_id=server-trace", content)
        self.assertNotIn("private-error-body", content)
        self.assertIn('question="问题"', content)
        self.assertNotIn("adp_answer", content)

    def test_interruption_is_logged_without_exception_body(self):
        """非预期读取异常原样传播，日志保留阶段但不记录异常正文。"""
        error = RuntimeError("private-interruption-body")

        class BrokenStream(FakeStream):
            """模拟传输适配器抛出的非预期异常。"""

            def read1(self, size):
                """在正式读取路径触发中断。"""
                raise error

        stream = BrokenStream()
        with patch("adp.sse.open_request", return_value=stream):
            with self.assertRaises(RuntimeError) as caught:
                chat("问题", config=self.config)
        self.assertIs(caught.exception, error)
        self.assertTrue(stream.closed)
        content = Path(self.config.log_file).read_text(encoding="utf-8")
        self.assertIn("ADP请求被中断", content)
        self.assertNotIn("private-interruption-body", content)

    def test_explicit_privacy_configuration_hides_message_bodies(self):
        """关闭正文后，仍保留普通诊断。"""
        config = replace(self.config, log_question=False, log_answer=False, log_file=str(self.root / "safe.log"))
        with patch("adp.sse.open_request", return_value=response("保密答案", 18)):
            chat("保密问题", config=config)
        content = Path(config.log_file).read_text(encoding="utf-8")
        self.assertNotIn("保密答案", content)
        self.assertNotIn("保密问题", content)
        self.assertIn("ADP请求完成", content)

    def test_config_relative_logging_path_and_type_validation(self):
        """相对日志路径基于配置文件，不因运行目录改变；类型错误仍走配置异常。"""
        path = self.root / "config.toml"
        path.write_text('[adp]\napp_key="fixture"\nuser_id="test"\n[logging]\nlog_file="diagnostics/chat.log"\n', encoding="utf-8")
        self.assertEqual(Path(load_config(path).log_file), self.root / "diagnostics" / "chat.log")
        path.write_text('[adp]\napp_key="fixture"\nuser_id="test"\n[logging]\nlog_file=false\n', encoding="utf-8")
        with self.assertRaises(ChatError) as caught:
            load_config(path)
        self.assertEqual(caught.exception.code, "invalid_type")
