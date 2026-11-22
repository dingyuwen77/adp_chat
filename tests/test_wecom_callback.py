"""正式加密协议、原子接收和 SQLite 事务失败边界。"""

import base64
import hashlib
import os
import secrets
import sqlite3
import struct
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch
from uuid import uuid4
from xml.sax.saxutils import escape

from fastapi.testclient import TestClient

from api.app import create_app
from core.config import ChatConfig
from core.models import ChatError
from storage.messages import message_key
from storage.sqlite_store import MessageStore, read_messages
from wecom.crypto import WeComCrypto
from wecom.models import InboundMessage
from wecom_callback_client import initialize_test_keys


class PausedWorker:
    """B协议/接收隔离测试固定下游未启动；C另测真实worker且不绕过验签。"""

    def __init__(self, *_args, **_kwargs):
        """不接管任何状态，保留RECEIVED直到接收断言结束。"""

    async def start(self):
        """接收专项没有ADP出站，不读取真实配置。"""

    async def stop(self):
        """没有创建后台任务或外部副作用。"""


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
        worker = patch("api.app.GatewayWorker", PausedWorker)
        worker.start()
        self.addCleanup(worker.stop)
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

    def test_ack_is_visible_from_a_separate_wal_reader(self):
        """回调成功时独立只读连接已能看到完整消息，旧目录中没有新快照。"""
        self.assertEqual(self.post(self.xml(content="完整问题🙂")).status_code, 200)
        database = self.root / "gateway.sqlite3"
        self.assertTrue(database.is_file())
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as reader:
            self.assertEqual(reader.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(reader.execute(
                "SELECT content, adp_state FROM gateway_messages"
            ).fetchall(), [("完整问题🙂", "RECEIVED")])
        self.assertFalse(list((self.root / "messages").glob("*.json")))

    def stored(self, msg_id):
        """通过网关的只读连接查验已提交消息。"""
        key = message_key(self.config.corp_id, self.config.agent_id, msg_id)
        return self.app.state.wecom_gateway.store.get(key)

    def test_get_returns_exact_decrypted_echo(self):
        """URL 验证没有 JSON 引号、BOM 或额外换行。"""
        ciphertext = self.encrypted(b"url-test-echo")
        response = self.client.get("/wecom/callback", params={**self.signed(ciphertext), "echostr": ciphertext})
        self.assertEqual((response.status_code, response.content), (200, b"url-test-echo"))

    def test_configured_callback_path_reuses_protocol_and_replaces_routes_on_restart(self):
        """同一应用重新启动后只保留新路径，GET/POST仍真实验签并可靠接收。"""
        app = create_app()
        app.openapi()  # 预先缓存默认Schema，重配路径后必须失效。
        previous = "/wecom/callback"
        for index, path in enumerate(("/qy/receive", "/enterprise/callback")):
            config = replace(self.config, callback_url="http://115.190.218.12:4560" + path,
                             messages_dir=str(self.root / f"custom-{index}"))
            with patch("api.app.load_config", return_value=config), TestClient(app) as client:
                ciphertext = self.encrypted(b"configured-url-echo")
                params = {**self.signed(ciphertext), "echostr": ciphertext}
                self.assertEqual(client.get(path, params=params).content, b"configured-url-echo")
                self.assertEqual(client.get(previous, params=params).status_code, 404)
                wrong = {**params, "msg_signature": "0" * 40}
                self.assertEqual(client.get(path, params=wrong).status_code, 403)
                encrypted = self.encrypted(self.xml(msg_id=str(80 + index)))
                body = (f"<xml><ToUserName>{config.corp_id}</ToUserName><AgentID>{config.agent_id}</AgentID>"
                        f"<Encrypt>{encrypted}</Encrypt></xml>").encode()
                response = client.post(path, params=self.signed(encrypted), content=body)
                self.assertEqual((response.status_code, response.content), (200, b""))
                key = message_key(config.corp_id, config.agent_id, str(80 + index))
                self.assertEqual(app.state.wecom_gateway.store.get(key)["adp_state"], "RECEIVED")
                self.assertEqual(client.get("/healthz").status_code, 200)
                paths = client.get("/openapi.json").json()["paths"]
                self.assertIn(path, paths)
                self.assertNotIn(previous, paths)
                self.assertEqual(set(paths[path]), {"get", "post"})
            previous = path

    def test_reserved_and_invalid_member_ids_are_rejected_before_storage(self):
        """合法签名不能把@all等非成员目标变成可群发的可信员工。"""
        for user in ("@all", "@ALL", "_member", ".member", "YNND|SIYU"):
            with self.subTest(user=user):
                self.assertEqual(self.post(self.xml(user=user)).status_code, 403)
        self.assertEqual(read_messages(self.config), [])

    def test_post_is_durable_before_empty_ack_and_duplicate_has_no_new_intent(self):
        """落盘先于 ACK；相同 MsgId 不变成 BUSY，也不增加通知或 ADP 调用。"""
        with patch("adp.sse.open_request") as network:
            first = self.post()
            self.assertEqual((first.status_code, first.content), (200, b""))
            saved = self.stored("1")
            self.assertEqual(saved["adp_state"], "RECEIVED")
            self.assertEqual(saved["send_state"], "NONE")
            key = saved["message_key"]
            self.assertEqual(read_messages(self.config, key=key), [saved])
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

    def test_concurrent_duplicate_callbacks_commit_only_one_record(self):
        """相同 MsgId 真正并发时也只产生一次获准状态，没有额外 BUSY 通知。"""
        barrier = Barrier(2)

        def submit(_index):
            """同时通过正式 HTTP 提交同一消息身份。"""
            barrier.wait(timeout=5)
            return self.post().status_code

        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(list(pool.map(submit, range(2))), [200, 200])
        records = read_messages(self.config)
        self.assertEqual(len(records), 1)
        saved = self.stored("1")
        self.assertEqual((saved["adp_state"], saved["send_state"], saved["receive_sequence"]), ("RECEIVED", "NONE", 1))

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
        self.assertEqual(read_messages(self.config), [])

    def test_stale_duplicate_query_and_malicious_xml_are_rejected(self):
        """过期签名、歧义参数、DTD 与巨量请求不能到达状态写入。"""
        ciphertext = self.encrypted(self.xml())
        self.assertEqual(self.post(ciphertext=ciphertext, params=self.signed(ciphertext, "1")).status_code, 403)
        params = list(self.signed(ciphertext).items()) + [("nonce", "another")]
        self.assertEqual(self.client.post("/wecom/callback", params=params, content=b"<xml/>").status_code, 403)
        evil = b'<!DOCTYPE xml [<!ENTITY x "expansion">]><xml><Encrypt>&x;</Encrypt></xml>'
        self.assertEqual(self.client.post("/wecom/callback", params=self.signed(ciphertext), content=evil).status_code, 403)
        self.assertEqual(self.client.post("/wecom/callback", content=b"x" * 65537).status_code, 413)
        self.assertEqual(read_messages(self.config), [])

    def test_nontext_and_events_are_acknowledged_without_adp_or_occupation(self):
        """非文字业务不冒充聊天消息，也不产生忙碌占用。"""
        with patch("adp.sse.open_request") as network:
            for kind in ("image", "event"):
                self.assertEqual(self.post(self.xml(kind=kind)).status_code, 200)
            network.assert_not_called()
        self.assertEqual(read_messages(self.config), [])

    def test_cdata_and_comment_literals_are_not_mistaken_for_real_dtd(self):
        """合法文字中的 XML 示例不属于文档声明，仍需原样可靠接收。"""
        for index, literal in enumerate(("<!DOCTYPE html>", '<!ENTITY x "literal">'), 1):
            plaintext = self.xml(str(index), f"CDATA_USER_{index}", "placeholder")
            plaintext = plaintext.replace(b"<Content>placeholder</Content>",
                                          ("<Content><![CDATA[" + literal + "]]></Content>").encode())
            plaintext = plaintext.replace(b"</xml>", b"<!-- literal <!DOCTYPE example> --></xml>")
            with self.subTest(literal=literal):
                self.assertEqual(self.post(plaintext).status_code, 200)
                self.assertEqual(self.stored(str(index))["content"], literal)
                self.assertEqual(self.stored(str(index))["adp_state"], "RECEIVED")

    def test_sqlite_full_failure_never_acks_and_receiver_fails_closed(self):
        """SQLite 实际触发磁盘配额异常，回调失败且保留之前提交的记录。"""
        self.assertEqual(self.post().status_code, 200)
        store = self.app.state.wecom_gateway.store
        store._writes.submit(store._writer.execute, "PRAGMA wal_checkpoint(TRUNCATE)").result()
        store._writes.submit(store._writer.execute, "PRAGMA max_page_count = 1").result()
        for index in range(2, 12):
            response = self.post(self.xml(str(index), f"EMP{index}", "大" * 10_000))
            if response.status_code == 503:
                break
        self.assertEqual(response.status_code, 503)
        self.assertIsNone(self.stored(str(index)))
        self.assertEqual(self.stored("1")["adp_state"], "RECEIVED")
        self.assertEqual(self.post().status_code, 503)
        self.assertEqual(self.client.get("/healthz").status_code, 503)

    def test_sqlite_write_lock_wait_never_acks(self):
        """外部写锁到期只拒绝本轮。锁释放后同一回调可以可靠接收。"""
        with closing(sqlite3.connect(self.config.gateway_database_path, isolation_level=None)) as lock:
            lock.execute("BEGIN IMMEDIATE")
            self.assertEqual(self.post().status_code, 503)
        self.assertEqual(read_messages(self.config), [])
        self.assertEqual(self.app.state.wecom_gateway.store.metrics()["write_busy"], 1)
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        time.sleep(0.06)
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.stored("1")["adp_state"], "RECEIVED")

    def test_failed_commit_and_unfinished_limit_preserve_reception_contract(self):
        """提交失败不 ACK；物理保护拒绝新消息，已保存重复仍可 ACK。"""
        store = self.app.state.wecom_gateway.store
        with patch.object(store, "_commit", side_effect=sqlite3.OperationalError("sync failure")):
            self.assertEqual(self.post().status_code, 503)
        self.assertEqual(read_messages(self.config), [])
        store.close()
        limited = replace(self.config, max_unfinished_messages=1)
        self.app.state.wecom_gateway.store = MessageStore(limited)
        self.assertEqual(self.post().status_code, 200)
        self.assertEqual(self.post(self.xml("2", "SIYU")).status_code, 503)
        self.assertEqual(self.post().status_code, 200)
        self.assertIsNone(self.stored("2"))

    def test_full_writer_queue_rejects_before_durable_ack(self):
        """暂停真实写事务，待写额度满时新回调返回 503 且没有插入记录。"""
        self.app.state.wecom_gateway.store.close()
        store = MessageStore(replace(self.config, database_write_queue=1))
        self.app.state.wecom_gateway.store = store
        entered, release = Event(), Event()
        receive = store._receive

        def held(message):
            """通过屏障留住首条写操作，正式接收和提交规则不变。"""
            entered.set()
            if not release.wait(5):
                raise AssertionError("未释放写队列回归屏障")
            return receive(message)

        with patch.object(store, "_receive", side_effect=held), ThreadPoolExecutor(1) as pool:
            first = pool.submit(self.post)
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.post(self.xml("2", "SIYU")).status_code, 503)
                self.assertFalse(first.done())
                self.assertEqual(read_messages(self.config), [])
                self.assertEqual(store.metrics()["write_queue_full"], 1)
            finally:
                release.set()
            self.assertEqual(first.result(timeout=3).status_code, 200)
        self.assertIsNone(self.stored("2"))

    def test_queued_callbacks_share_a_commit_and_never_ack_a_failed_batch(self):
        """两条回调积压时共享一次同步提交。整批写入失败时两条都不能 ACK。"""
        store = self.app.state.wecom_gateway.store
        for should_fail in (False, True):
            entered, release = Event(), Event()

            def hold_writer():
                """在事务开始前让两个回调都进入实际待写队列。"""
                entered.set()
                if not release.wait(5):
                    raise AssertionError("未释放批量提交回归屏障")

            blocker = store._writes.submit(hold_writer)
            with (patch.object(store, "_commit", wraps=store._commit,
                               side_effect=OSError("fixture") if should_fail else None) as commit,
                  ThreadPoolExecutor(2) as pool):
                self.assertTrue(entered.wait(3))
                start = 3 if should_fail else 1
                posts = [pool.submit(self.post, self.xml(str(number), f"EMP{number}"))
                         for number in (start, start + 1)]
                try:
                    deadline = time.monotonic() + 3
                    while store.metrics()["write_pending"] != 2 and time.monotonic() < deadline:
                        time.sleep(0.005)
                    self.assertEqual(store.metrics()["write_pending"], 2)
                    self.assertTrue(all(not post.done() for post in posts))
                finally:
                    release.set()
                blocker.result(timeout=3)
                self.assertEqual([post.result(timeout=3).status_code for post in posts],
                                 [503, 503] if should_fail else [200, 200])
                self.assertEqual(commit.call_count, 1)
                self.assertEqual(store.metrics()["write_batch_peak"], 2)
            self.assertEqual(len(read_messages(self.config)), 2)

    def test_receipt_and_versioned_updates_share_one_ordered_transaction(self):
        """状态更新可与新接收共享提交，版本冲突不影响同批其他写入。"""
        self.assertEqual(self.post().status_code, 200)
        store = self.app.state.wecom_gateway.store
        first = self.stored("1")
        entered, release = Event(), Event()

        def hold_writer():
            """等三个操作排好后再由写线程进入正式事务。"""
            entered.set()
            if not release.wait(5):
                raise AssertionError("未释放混合写入回归屏障")

        blocker = store._writes.submit(hold_writer)
        try:
            self.assertTrue(entered.wait(3))
            with patch.object(store, "_commit", wraps=store._commit) as commit:
                key, version = first["message_key"], first["record_version"]
                changes = {"adp_state": "ADP_SEND_ARMED", "request_id": str(uuid4())}
                stale = store._enqueue_write(store._update, (key, version - 1, changes))
                arm = store._enqueue_write(store._update, (key, version, changes))
                receipt = store.submit_receive(InboundMessage(
                    self.config.corp_id, self.config.agent_id, first["user_id"], "2", "排队检查", int(time.time())))
                self.assertFalse(arm.done())
                self.assertIsNone(self.stored("2"))
                release.set()
                blocker.result(timeout=3)
                with self.assertRaises(ChatError) as conflict:
                    stale.result(timeout=3)
                self.assertEqual(conflict.exception.code, "version_conflict")
                self.assertEqual(arm.result(timeout=3)["adp_state"], "ADP_SEND_ARMED")
                self.assertEqual(receipt.result(timeout=3)[0]["adp_state"], "REJECTED_USER_BUSY")
                self.assertEqual(commit.call_count, 1)
        finally:
            release.set()

    def test_online_backup_contains_uncheckpointed_ack_and_reopens(self):
        """在线备份包含还在 WAL 的提交，可用相同网关配置恢复且不覆盖已有副本。"""
        from storage.maintenance import backup_database, check_database, has_message_history

        self.assertEqual(self.post().status_code, 200)
        self.assertGreater(self.app.state.wecom_gateway.store.metrics()["wal_bytes"], 0)
        destination = self.root / "backup" / "gateway.sqlite3"
        destination.parent.mkdir()
        self.assertFalse(has_message_history(destination))
        with self.assertRaises(ChatError) as missing:
            has_message_history(destination, required=True)
        self.assertEqual(missing.exception.code, "missing_database")
        self.assertEqual(backup_database(self.config.gateway_database_path, destination)["integrity"], "ok")
        self.assertEqual(check_database(destination)["schema_version"], 1)
        self.assertTrue(has_message_history(destination, required=True))
        with self.assertRaises(ChatError):
            backup_database(self.config.gateway_database_path, destination)
        backup_config = replace(self.config, database_file=str(destination))
        with MessageStore(backup_config):
            self.assertEqual(read_messages(backup_config), [self.stored("1")])

    def test_failure_after_commit_recovers_without_second_reception(self):
        """提交已生效但调用报错时关闭接收，重启以持久事实去重并恢复占用。"""
        store = self.app.state.wecom_gateway.store
        commit = store._commit

        def commit_then_fail():
            """在真实提交后注入结果交接失败，保留 SQLite 中的原记录。"""
            commit()
            raise sqlite3.OperationalError("commit outcome uncertain")

        with patch.object(store, "_commit", side_effect=commit_then_fail):
            self.assertEqual(self.post().status_code, 503)
        self.assertEqual(len(read_messages(self.config)), 1)
        self.assertEqual(self.post(self.xml("2")).status_code, 503)
        self.app.state.wecom_gateway.store.close()
        self.app.state.wecom_gateway.store = MessageStore(self.config)
        saved = self.stored("1")
        self.assertEqual(self.post(self.xml("1", content="不得替换原内容")).status_code, 200)
        self.assertEqual(self.stored("1"), saved)
        self.assertEqual(self.post(self.xml("2")).status_code, 200)
        self.assertEqual(self.stored("2")["adp_state"], "REJECTED_USER_BUSY")

    def test_corrupt_or_unsupported_database_is_preserved_and_blocks_restart(self):
        """版本或损坏库不能静默当作空库，避免重复接收已处理消息。"""
        self.post()
        self.app.state.wecom_gateway.store.close()
        path = self.config.gateway_database_path
        with closing(sqlite3.connect(path)) as writer:
            writer.execute("PRAGMA user_version = 999")
        with self.assertRaises(ChatError):
            MessageStore(self.config)
        self.assertEqual(len(read_messages(self.config)), 1)
        with closing(sqlite3.connect(path)) as reader:
            self.assertEqual(reader.execute("PRAGMA user_version").fetchone()[0], 999)
        path.write_bytes(b"broken database")
        with self.assertRaises(ChatError):
            MessageStore(self.config)
        self.assertEqual(path.read_bytes(), b"broken database")

    def test_cryptographic_padding_and_claimed_plaintext_length_are_strict(self):
        """有合法签名的畸形加密包也不能通过认证边界。"""
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        for raw in (b"x" * 31 + b"\0", b"x" * 28 + b"\4\4\3\4",
                    b"r" * 16 + struct.pack("!I", 999999) + b"x" * 11 + b"\1"):
            encryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).encryptor()
            ciphertext = base64.b64encode(encryptor.update(raw) + encryptor.finalize()).decode()
            with self.subTest(raw=raw[:4]):
                self.assertEqual(self.post(ciphertext=ciphertext).status_code, 403)

    def test_database_is_exclusive_and_restart_restores_busy(self):
        """OS 锁拒绝第二实例，同一库重开后仍按持久状态判断占用。"""
        self.post()
        with self.assertRaises(ChatError):
            MessageStore(self.config)
        self.app.state.wecom_gateway.store.close()
        with MessageStore(self.config) as store:
            self.assertEqual(store.get(message_key(self.config.corp_id, self.config.agent_id, "1"))["adp_state"], "RECEIVED")
            self.app.state.wecom_gateway.store = store
            self.assertEqual(self.post(self.xml("2")).status_code, 200)
            self.assertEqual(self.stored("2")["adp_state"], "REJECTED_USER_BUSY")
        self.assertTrue(self.config.gateway_database_path.is_file())

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

    def test_test_key_initializer_preserves_existing_configuration_and_refuses_rotation(self):
        """本机初始化仅填空测试密钥，保留既有配置且拒绝再次覆盖。"""
        from core.config import load_config

        path = self.root / "config.toml"
        path.write_text('[adp]\napp_key="existing-fake"\nuser_id="legacy"\n'
                        '[wecom]\ncorp_id="corp"\nagent_id=1000053\nadp_application="local"\n'
                        'messages_dir="keep-this-directory"\n', encoding="utf-8")
        initialize_test_keys(path)
        config = load_config(path)
        self.assertEqual((config.app_key, config.user_id), ("existing-fake", "legacy"))
        self.assertTrue(config.gateway_enabled)
        self.assertEqual(config.messages_dir, str(self.root / "keep-this-directory"))
        self.assertEqual(len(base64.b64decode(config.encoding_aes_key + "=")), 32)
        self.assertNotIn(config.callback_token, repr(config))
        saved = path.read_bytes()
        with self.assertRaises(ValueError):
            initialize_test_keys(path)
        self.assertEqual(path.read_bytes(), saved)

    def test_access_log_drops_signed_query_even_on_encoded_callback_path(self):
        """Uvicorn 原始路径的不同编码不能绕过密文/签名查询串脱敏。"""
        import logging
        from core.logging import CallbackAccessFilter

        record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1,
                                   '%s - "%s %s HTTP/%s" %d',
                                   ("127.0.0.1", "GET", "/%77ecom/callback?msg_signature=hidden&echostr=hidden", "1.1", 200), None)
        self.assertTrue(CallbackAccessFilter().filter(record))
        self.assertNotIn("hidden", record.getMessage())
        self.assertNotIn("?", record.getMessage())
