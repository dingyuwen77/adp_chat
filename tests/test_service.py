"""单用户服务的串行与持久状态回归。"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from application.chat_service import ChatService
from core.config import ChatConfig
from core.models import ChatError, ChatResult


class ServiceTests(unittest.TestCase):
    """并发互斥和会话生命周期。"""

    def setUp(self):
        """准备独立测试会话。"""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.config = ChatConfig(
            "fixture-key", "only-user", log_enabled=False,
            session_file=str(Path(directory.name) / "session.json"),
        )

    def test_session_persistence_and_single_turn(self):
        """跨服务实例复用会话 ID，每条请求只有一轮。"""
        first = ChatService(self.config)
        cid = first.initialize()
        self.assertEqual(ChatService(self.config).initialize(), cid)
        self.assertTrue(Path(self.config.session_file).exists())
        seen = []

        def respond(message, *, config, conversation_id):
            """记录发送参数及独立关联 ID。"""
            seen.append((message, config, conversation_id))
            return ChatResult(message, conversation_id, "request-" + str(len(seen)))

        with patch("application.chat_service.chat", side_effect=respond) as network:
            self.assertEqual(first.ask("一").text, "一")
            self.assertEqual(first.ask("二").text, "二")
        self.assertEqual(network.call_count, 2)
        self.assertEqual({item[2] for item in seen}, {cid})
        self.assertIs(seen[0][1], self.config)

    def test_busy_409_mechanism_and_lock_recovery(self):
        """用屏障控制真实线程交错，确保忙碌时不进入第二轮。"""
        service = ChatService(self.config)
        entered, release = threading.Event(), threading.Event()

        def block(_message, *, config, conversation_id):
            """模拟第一轮网络请求保持占用。"""
            entered.set()
            if not release.wait(5):
                raise AssertionError("测试等待失败")
            raise ChatError("chat", "network_error", "模拟断网")

        errors = []

        def worker():
            """在另一线程启动请求。"""
            try:
                service.ask("第一轮")
            except ChatError as error:
                errors.append(error.code)

        with patch("application.chat_service.chat", side_effect=block) as network:
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(ChatError) as caught:
                    service.ask("第二轮")
                self.assertEqual(caught.exception.code, "busy")
                self.assertEqual(network.call_count, 1)
            finally:
                release.set()
                thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ["network_error"])
        with patch("application.chat_service.chat", return_value=ChatResult("ok", service.initialize(), "new")):
            self.assertEqual(service.ask("第三轮").text, "ok")

    def test_bad_input_has_no_side_effect(self):
        """空消息和附件命令均不触发持久化或网络。"""
        service = ChatService(self.config)
        with patch("application.chat_service.chat") as network:
            for question in (" ", "/file private"):
                with self.assertRaises(ChatError):
                    service.ask(question)
        network.assert_not_called()
        self.assertFalse(Path(self.config.session_file).exists())
