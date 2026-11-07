"""阶段 A：身份契约、真实进程派生和既有 ADP 请求复用。"""

import io
import json
import contextlib
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from adp.client import chat
from core.config import ChatConfig, load_config
from core.identity import employee_identity
from core.models import ChatError, ChatResult
from adp_conversation_probe import main as probe_main


class EmployeeIdentityTests(unittest.TestCase):
    """身份作用域完整隔离，不能污染固定用户的旧配置。"""

    def setUp(self):
        """使用独立的虚构 AppKey，企业和用户仅作测试输入。"""
        self.config = ChatConfig(
            "test-key", "legacy-user", visitor_id="legacy-visitor", user_name="旧名称",
            corp_id="wwf483023b734c570a", agent_id=1000053,
            adp_application="wecom-local-test", log_enabled=False,
        )

    def test_scope_is_stable_and_separates_every_identity_dimension(self):
        """员工、企业、应用和逻辑应用任一改变都必须分离会话。"""
        first = employee_identity(self.config, "YNND")
        self.assertEqual(first.user_id, "YNND")
        self.assertEqual(first.visitor_id, "YNND")
        self.assertRegex(first.conversation_id, r"^[0-9a-f]{64}$")
        self.assertEqual(first, employee_identity(self.config, "YNND"))
        variants = [
            employee_identity(self.config, "SIYU"),
            employee_identity(replace(self.config, corp_id="other-corp"), "YNND"),
            employee_identity(replace(self.config, agent_id=1000054), "YNND"),
            employee_identity(replace(self.config, adp_application="another-app"), "YNND"),
        ]
        self.assertEqual(len({first.conversation_id, *(v.conversation_id for v in variants)}), 5)
        self.assertEqual(first, employee_identity(replace(self.config, app_key="rotated-key"), "YNND"))
        self.assertNotEqual(first, employee_identity(self.config, "ynnd"))

    def test_canonical_derivation_vector_and_unambiguous_fields(self):
        """固定协议向量保护后续版本，分隔符不能造成作用域碰撞。"""
        import hashlib

        canonical = b'["adp_chat:wecom:v1","wwf483023b734c570a",1000053,"wecom-local-test","YNND"]'
        self.assertEqual(employee_identity(self.config, "YNND").conversation_id,
                         hashlib.sha256(canonical).hexdigest())
        left = replace(self.config, corp_id="a:b", adp_application="c")
        right = replace(self.config, corp_id="a", adp_application="b:c")
        self.assertNotEqual(employee_identity(left, "YNND"), employee_identity(right, "YNND"))

    def test_cross_process_identity_does_not_need_a_mapping_file(self):
        """两个独立解释器与父进程生成相同标识，其他员工仍隔离。"""
        code = (
            "import json; from core.config import ChatConfig; "
            "from core.identity import employee_identity; "
            "c=ChatConfig('fake','legacy',corp_id='wwf483023b734c570a',"
            "agent_id=1000053,adp_application='wecom-local-test'); "
            "print(json.dumps([employee_identity(c,u).conversation_id for u in ['YNND','SIYU']]))"
        )
        with tempfile.TemporaryDirectory() as directory:
            results = [subprocess.run([sys.executable, "-B", "-c", code], cwd=directory,
                                      capture_output=True, text=True, check=True, timeout=15)
                       for _ in range(2)]
            self.assertEqual(list(Path(directory).iterdir()), [])
        expected = [employee_identity(self.config, user).conversation_id for user in ("YNND", "SIYU")]
        self.assertEqual([json.loads(r.stdout) for r in results], [expected, expected])
        self.assertNotEqual(*expected)

    def test_identity_reuses_formal_chat_without_mutating_legacy_config(self):
        """新身份通过现有 V2 入口，续轮只改变 RequestId 而不改旧配置。"""
        requests = []

        def open_stream(request, **_kwargs):
            """响应回显当前会话，检查实际正式请求的身份与消息。"""
            payload = json.loads(request.data)
            requests.append(payload)
            stream = io.BytesIO(('data: ' + json.dumps({"Type": "response.completed", "Response": {
                "Status": "success", "ConversationId": payload["ConversationId"],
                "Messages": [{"Type": "reply", "Contents": [{"Type": "text", "Text": "测试回复"}]}],
            }}) + '\n\n').encode())
            stream.headers = {"Content-Type": "text/event-stream"}
            return stream

        with patch("adp.sse.open_request", side_effect=open_stream):
            for user, message in (("YNND", "我想改密码"), ("SIYU", "windows"), ("YNND", "windows")):
                identity = employee_identity(self.config, user)
                result = chat(message, config=identity.chat_config(self.config),
                              conversation_id=identity.conversation_id)
                self.assertEqual(result.text, "测试回复")
        self.assertEqual([r["UserId"] for r in requests], ["YNND", "SIYU", "YNND"])
        self.assertEqual([r["VisitorId"] for r in requests], ["YNND", "SIYU", "YNND"])
        self.assertEqual(requests[0]["ConversationId"], requests[2]["ConversationId"])
        self.assertNotEqual(requests[0]["ConversationId"], requests[1]["ConversationId"])
        self.assertEqual(len({r["RequestId"] for r in requests}), 3)
        self.assertTrue(all("UserName" not in r for r in requests))
        self.assertEqual((self.config.user_id, self.config.visitor_id, self.config.user_name),
                         ("legacy-user", "legacy-visitor", "旧名称"))

    def test_invalid_or_partial_identity_fails_before_network(self):
        """缺失作用域和非法用户不可静默退回固定用户。"""
        legacy = ChatConfig("test-key", "legacy-user", log_enabled=False)
        for config, user in ((legacy, "YNND"), (self.config, ""), (self.config, " YNND"),
                             (self.config, "YNND\0"), (self.config, None)):
            with self.subTest(user=user), self.assertRaises(ChatError):
                employee_identity(config, user)
        for fields in ({"corp_id": "corp"}, {"agent_id": True}, {"agent_id": -1},
                       {"adp_application": " bad "}):
            with self.subTest(fields=fields), self.assertRaises(ChatError):
                ChatConfig("test-key", "legacy-user", **fields)

    def test_toml_uses_existing_config_source_and_legacy_remains_valid(self):
        """新增同文件分组，缺省仍兼容全部原入口。"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            base = '[adp]\napp_key="fake"\nuser_id="legacy"\n'
            path.write_text(base, encoding="utf-8")
            self.assertEqual(load_config(path).corp_id, "")
            path.write_text(base + '[wecom]\ncorp_id="corp"\nagent_id=1000053\n'
                            'adp_application="test-app"\n', encoding="utf-8")
            self.assertEqual(employee_identity(load_config(path), "YNND").user_id, "YNND")

    def test_probe_is_offline_by_default_and_real_calls_disable_body_logs(self):
        """Probe 默认零网络，显式调用保留证据且不复制协议或泄露密钥。"""
        with tempfile.TemporaryDirectory() as directory, patch("adp_conversation_probe.load_config", return_value=self.config):
            output = Path(directory) / "evidence.json"
            with patch("adp_conversation_probe.chat") as provider, contextlib.redirect_stdout(io.StringIO()) as terminal:
                self.assertEqual(probe_main(["--user-id", "YNND"]), 0)
                provider.assert_not_called()
                self.assertIs(json.loads(terminal.getvalue())["real_adp_requested"], False)
            reply = ChatResult("测试正文", employee_identity(self.config, "YNND").conversation_id, "r" * 32)
            with patch("adp_conversation_probe.chat", return_value=reply) as provider, contextlib.redirect_stdout(io.StringIO()) as terminal:
                args = ["--user-id", "YNND", "--execute-real-adp", "--output", str(output)]
                self.assertEqual(probe_main(args), 0)
                called_config = provider.call_args.kwargs["config"]
                self.assertFalse(called_config.log_enabled)
                self.assertFalse(called_config.log_question)
                self.assertEqual(called_config.log_answer_chars, 0)
                saved = json.loads(output.read_text(encoding="utf-8"))
                self.assertEqual(saved["response_text"], "测试正文")
                self.assertEqual(saved["semantic_verdict"], "requires_review")
                self.assertNotIn("测试正文", terminal.getvalue())
                self.assertNotIn("test-key", output.read_text(encoding="utf-8"))
                provider.reset_mock()
                self.assertEqual(probe_main(args), 1)
                provider.assert_not_called()

    def test_probe_records_provider_error_once_without_retry(self):
        """上游失败也保存实际 Code，不伪造成功或发第二次请求。"""
        with tempfile.TemporaryDirectory() as directory, patch("adp_conversation_probe.load_config", return_value=self.config):
            output = Path(directory) / "error.json"
            error = ChatError("chat", "provider_error", "安全提示", provider_code="460701")
            with patch("adp_conversation_probe.chat", side_effect=error) as provider, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(probe_main(["--user-id", "SIYU", "--execute-real-adp", "--output", str(output)]), 1)
                self.assertEqual(provider.call_count, 1)
            saved = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual((saved["status"], saved["provider_code"], saved["user_id"]), ("error", "460701", "SIYU"))
