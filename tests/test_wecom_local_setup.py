"""阶段E本机配置夹具：同一TOML加载器、独立目录、不覆盖秘密或真实作用域。"""

import base64
import tempfile
import unittest
from pathlib import Path

from core.config import ADP_CHAT_URL, load_config
from wecom_local_setup import create_local_config


class LocalSetupTests(unittest.TestCase):
    """只使用临时项目和Fake源；绝不读取本机根AppKey。"""

    def setUp(self):
        """复制已跟踪示例形成测试项目，不另定义配置Schema。"""
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.example = Path(__file__).resolve().parents[1] / "config.example.toml"
        (self.root / "config.example.toml").write_bytes(self.example.read_bytes())
        self.output = self.root / "runtime" / "local" / "config.toml"

    def test_fake_is_isolated_valid_and_never_overwrites(self):
        """独立密钥、模拟发送和Fake专属AppKey，不污染根配置。"""
        create_local_config(self.output, scope="wecom-local-e-fixture", project_root=self.root)
        config = load_config(self.output)
        self.assertEqual(config.app_key, "fake-local-adp-only")
        self.assertEqual(config.adp_url, "http://127.0.0.1:8765/adp/v2/chat")
        self.assertEqual(config.notification_mode, "simulated")
        self.assertEqual(config.gateway_environment, "test")
        self.assertEqual(len(base64.b64decode(config.encoding_aes_key + "=")), 32)
        self.assertFalse(config.log_question)
        self.assertEqual(config.log_answer_chars, 0)
        original = self.output.read_bytes()
        with self.assertRaises(FileExistsError):
            create_local_config(self.output, scope="wecom-local-e-fixture", project_root=self.root)
        self.assertEqual(self.output.read_bytes(), original)
        self.assertFalse((self.root / "config.toml").exists())

    def test_real_uses_explicit_source_and_keeps_published_app_settings(self):
        """只复制已选测试应用的凭据/请求选项，回调密钥、目录与scope独立。"""
        source = self.root / "private-source.toml"
        text = self.example.read_text(encoding="utf-8").replace('app_key = ""', 'app_key = "private-fixture-key"')
        text = text.replace('system_role = ""', 'system_role = "published-test-role"')
        source.write_text(text, encoding="utf-8")
        create_local_config(self.output, scope="wecom-local-e-real", real_adp_source=source, project_root=self.root)
        config = load_config(self.output)
        self.assertEqual(config.adp_url, ADP_CHAT_URL)
        self.assertEqual(config.app_key, "private-fixture-key")
        self.assertEqual(config.system_role, "published-test-role")
        self.assertEqual(config.adp_application, "wecom-local-e-real")
        self.assertEqual(config.wecom_app_secret, "")
        self.assertEqual(config.messages_dir, str(self.output.parent / "runtime" / "messages"))
        self.assertEqual(source.read_text(encoding="utf-8"), text)

    def test_output_and_scope_are_bounded_to_ignored_test_area(self):
        """不能把生成的测试/复制凭据写到跟踪目录，也不能复用生产作用域名称。"""
        for output, scope in ((self.root / "config.toml", "wecom-local-e-test"),
                              (self.output, "production-app")):
            with self.subTest(output=output, scope=scope), self.assertRaises(ValueError):
                create_local_config(output, scope=scope, project_root=self.root)
            self.assertFalse(output.exists())
