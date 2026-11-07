"""阶段 B：正式加密协议、原子接收和真实文件失败边界。"""

import base64
import hashlib
import json
import os
import secrets
import struct
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from xml.sax.saxutils import escape

from fastapi.testclient import TestClient

from api.app import create_app
from core.config import ChatConfig
from core.models import ChatError
from storage.messages import MessageStore, message_key
from wecom.crypto import WeComCrypto


class CallbackTests(unittest.TestCase):
    """从正式 HTTP 边界验证，没有绕过签名的测试路由。"""

    def setUp(self):
        """每个测试独立生成测试密钥和存储，不接企微或真实 ADP。"""
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.key = os.urandom(32)
        self.token = secrets.token_hex(12)
        self.config = ChatConfig(
            "fake-adp-key", "legacy", corp_id="wwf483023b734c570a", agent_id=1000053,
            adp_application="wecom-b-test", gateway_enabled=True,
            callback_token=self.token, encoding_aes_key=base64.b64encode(self.key).decode()[:-1],
            messages_dir=str(self.root / "messages"), session_file=str(self.root / "legacy.json"),
            log_enabled=False,
        )
        self.app = create_app()
        configuration = patch("api.app.load_config", return_value=self.config)
        configuration.start()
        self.addCleanup(configuration.stop)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def encrypted(self, content: bytes, *, receiver: str | None = None) -> str:
        """独立协议夹具只准备密文，防止生产加解密自洽掩盖格式错误。"""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        body = os.urandom(16) + struct.pack("!I", len(content)) + content
        body += (receiver if receiver is not None else self.config.corp_id).encode()
        padding = 32 - len(body) % 32
        encryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).encryptor()
        return base64.b64encode(encryptor.update(body + bytes([padding]) * padding) + encryptor.finalize()).decode()

    def signed(self, ciphertext: str, timestamp: str | None = None) -> dict:
        """以独立 SHA1 夹具生成正式查询参数。"""
        timestamp = timestamp if timestamp is not None else str(int(time.time()))
        nonce = secrets.token_hex(8)
        signature = hashlib.sha1("".join(sorted([self.token, timestamp, nonce, ciphertext])).encode()).hexdigest()
        return {"msg_signature": signature, "timestamp": timestamp, "nonce": nonce}

    def xml(self, msg_id="1", user="YNND", content="我想改密码", kind="text", corp=None, agent=None) -> bytes:
        """准备应用文字或非文字明文，身份只来自已签名的字段。"""
        return (f"<xml><ToUserName>{escape(corp or self.config.corp_id)}</ToUserName>"
                f"<FromUserName>{escape(user)}</FromUserName><CreateTime>{int(time.time())}</CreateTime>"
                f"<MsgType>{kind}</MsgType><Content>{escape(content)}</Content><MsgId>{msg_id}</MsgId>"
                f"<AgentID>{agent or self.config.agent_id}</AgentID></xml>").encode()

    def post(self, plaintext=None, *, ciphertext=None, params=None, outer_corp=None, outer_agent=None):
        """从正式生产回调提交加密 XML，不注入内部接收决定。"""
        ciphertext = ciphertext if ciphertext is not None else self.encrypted(plaintext or self.xml())
        outer = (f"<xml><ToUserName>{outer_corp or self.config.corp_id}</ToUserName>"
                 f"<AgentID>{outer_agent or self.config.agent_id}</AgentID><Encrypt>{ciphertext}</Encrypt></xml>")
        return self.client.post("/wecom/callback", params=params or self.signed(ciphertext),
                                content=outer.encode(), headers={"Content-Type": "application/xml"})

    def stored(self, msg_id):
        """通过正式存储读取快照，与磁盘 JSON 事实对照。"""
        key = message_key(self.config.corp_id, self.config.agent_id, msg_id)
        return self.app.state.wecom_gateway.store.get(key)

    def test_get_returns_exact_decrypted_echo(self):
        """URL 验证没有 JSON 引号、BOM 或额外换行。"""
        ciphertext = self.encrypted(b"url-test-echo")
        response = self.client.get("/wecom/callback", params={**self.signed(ciphertext), "echostr": ciphertext})
        self.assertEqual((response.status_code, response.content), (200, b"url-test-echo"))

    def test_post_is_durable_before_empty_ack_and_duplicate_has_no_new_intent(self):
        """落盘先于 ACK；相同 MsgId 不变成 BUSY，也不增加通知或 ADP 调用。"""
        with patch("adp.sse.open_request") as network:
            first = self.post()
            self.assertEqual((first.status_code, first.content), (200, b""))
            saved = self.stored("1")
            self.assertEqual(saved["adp_state"], "RECEIVED")
            self.assertEqual(saved["send_state"], "NONE")
            key = saved["message_key"]
            disk = self.root / "messages" / f"{key}.json"
            self.assertEqual(json.loads(disk.read_text(encoding="utf-8")), saved)
            self.assertEqual(self.post().status_code, 200)
            self.assertEqual(self.stored("1"), saved)
            network.assert_not_called()

    def test_same_user_race_admits_only_one_and_saves_busy_notification(self):
        """屏障同时提交两个新 MsgId，只一条原子占用，另一条不排队。"""
        barrier = Barrier(2)

        def submit(msg_id):
            """让两个真实 HTTP 调用在接收边界竞争。"""
            barrier.wait(timeout=5)
            return self.post(self.xml(msg_id)).status_code

        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(list(pool.map(submit, ("1", "2"))), [200, 200])
        records = [self.stored(msg_id) for msg_id in ("1", "2")]
        self.assertCountEqual([r["adp_state"] for r in records], ["RECEIVED", "REJECTED_USER_BUSY"])
        busy = next(r for r in records if r["adp_state"] == "REJECTED_USER_BUSY")
        self.assertEqual(busy["send_state"], "PENDING")
        self.assertIn("重新发送", busy["notification"])
        before = busy.copy()
        self.assertEqual(self.post(self.xml(busy["msg_id"])).status_code, 200)
        self.assertEqual(self.stored(busy["msg_id"]), before)

    def test_different_users_are_both_admitted(self):
        """不同员工有各自占用，不使用旧单用户全局锁作为业务限额。"""
        self.assertEqual(self.post(self.xml("1", "YNND")).status_code, 200)
        self.assertEqual(self.post(self.xml("2", "SIYU")).status_code, 200)
        first, second = self.stored("1"), self.stored("2")
        self.assertEqual([r["adp_state"] for r in (first, second)], ["RECEIVED"] * 2)
        self.assertNotEqual(first["conversation_id"], second["conversation_id"])

    def test_signature_receiver_outer_and_inner_identity_are_all_checked(self):
        """每个认证/身份投影都在持久化和 ACK 前拒绝。"""
        ciphertext = self.encrypted(self.xml())
        cases = [
            {"ciphertext": ciphertext, "params": {**self.signed(ciphertext), "msg_signature": "0" * 40}},
            {"ciphertext": self.encrypted(self.xml(), receiver="wrong-corp")},
            {"outer_corp": "wrong-corp"}, {"outer_agent": 42},
            {"plaintext": self.xml(corp="wrong-corp")}, {"plaintext": self.xml(agent=42)},
            {"plaintext": self.xml(user="")}, {"plaintext": self.xml(msg_id="../escape")},
            {"plaintext": self.xml().replace(b"</xml>", b"<FromUserName>SIYU</FromUserName></xml>")},
            {"ciphertext": "not-base64"},
        ]
        for case in cases:
            with self.subTest(case=list(case)):
                self.assertEqual(self.post(**case).status_code, 403)
        self.assertEqual(list((self.root / "messages").glob("*.json")), [])

    def test_stale_duplicate_query_and_malicious_xml_are_rejected(self):
        """过期签名、歧义参数、DTD 与巨量请求不能到达状态写入。"""
        ciphertext = self.encrypted(self.xml())
        self.assertEqual(self.post(ciphertext=ciphertext, params=self.signed(ciphertext, "1")).status_code, 403)
        params = list(self.signed(ciphertext).items()) + [("nonce", "another")]
        self.assertEqual(self.client.post("/wecom/callback", params=params, content=b"<xml/>").status_code, 403)
        evil = b'<!DOCTYPE xml [<!ENTITY x "expansion">]><xml><Encrypt>&x;</Encrypt></xml>'
        self.assertEqual(self.client.post("/wecom/callback", params=self.signed(ciphertext), content=evil).status_code, 403)
        self.assertEqual(self.client.post("/wecom/callback", content=b"x" * 65537).status_code, 413)
        self.assertEqual(list((self.root / "messages").glob("*.json")), [])

    def test_nontext_and_events_are_acknowledged_without_adp_or_occupation(self):
        """非文字业务不冒充聊天消息，也不产生忙碌占用。"""
        with patch("adp.sse.open_request") as network:
            for kind in ("image", "event"):
                self.assertEqual(self.post(self.xml(kind=kind)).status_code, 200)
            network.assert_not_called()
        self.assertEqual(list((self.root / "messages").glob("*.json")), [])

    def test_replace_failure_never_acks_and_receiver_fails_closed(self):
        """原子提交失败不能 ACK，后续接收关闭直到重启，避免内存/磁盘分裂。"""
        with patch("storage.messages.os.replace", side_effect=OSError("disk failure")):
            self.assertEqual(self.post().status_code, 503)
        self.assertEqual(list((self.root / "messages").glob("*.json")), [])
        self.assertEqual(self.post().status_code, 503)

    def test_directory_is_exclusive_and_restart_restores_busy_from_snapshot(self):
        """OS 锁拒绝第二实例；同目录重开从唯一消息事实恢复占用。"""
        self.post()
        with self.assertRaises(ChatError):
            MessageStore(self.config)
        self.app.state.wecom_gateway.store.close()
        with MessageStore(self.config) as store:
            self.assertEqual(store.get(message_key(self.config.corp_id, self.config.agent_id, "1"))["adp_state"], "RECEIVED")
        self.assertTrue((self.root / "messages").is_dir())

    def test_crypto_encrypt_interoperates_with_independent_decoder(self):
        """生产加密只用于模拟输入，协议布局由独立 AES 解码断言。"""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        crypto = WeComCrypto(self.token, self.config.encoding_aes_key, self.config.corp_id)
        ciphertext = crypto.encrypt(b"reference-layout")
        decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).decryptor()
        raw = decryptor.update(base64.b64decode(ciphertext)) + decryptor.finalize()
        self.assertEqual(raw[16:20], struct.pack("!I", len(b"reference-layout")))
        self.assertEqual(raw[20:-raw[-1]], b"reference-layout" + self.config.corp_id.encode())
        self.assertEqual(raw[-raw[-1]:], bytes([raw[-1]]) * raw[-1])
