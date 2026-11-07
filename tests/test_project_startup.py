"""根目录配置在真实脚本、交互 CLI 和跨工作目录启动中的回归。"""

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
import unittest
from urllib.error import URLError
from urllib.request import urlopen
from pathlib import Path

from core.config import load_config


ROOT = Path(__file__).resolve().parents[1]


class ProjectStartupTests(unittest.TestCase):
    """从已安装应用的正式入口运行，配置和会话使用隔离项目。"""

    def setUp(self):
        """以正式模板建立隔离根配置，使用部署配置入口而不改导入路径。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.outside = Path(temporary.name)
        self.project = self.outside / "project"
        self.project.mkdir()
        shutil.copyfile(ROOT / "main.py", self.project / "main.py")
        template = (ROOT / "config.example.toml").read_text(encoding="utf-8")
        (self.project / "config.toml").write_text(
            template.replace('app_key = ""', 'app_key = "fixture-key"'), encoding="utf-8",
        )
        self.environment = {**os.environ, "ADP_CONFIG_FILE": str(self.project / "config.toml")}

    def test_main_directly_loads_root_config_from_another_working_directory(self):
        """模拟 PyCharm 直接执行文件：配置加载成功后才拒绝空问题。"""
        result = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", str(self.project / "main.py")],
            input="\n", cwd=self.outside, env=self.environment,
            capture_output=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("[input/empty_question]", result.stderr)
        self.assertNotIn("config/", result.stderr)
        self.assertFalse((self.project / "runtime/session.json").exists())

    def test_default_main_restarts_keep_root_session_and_logs(self):
        """不传 --config 跨进程恢复会话；只隔离远端 SSE，真实读写根配置和文件。"""
        harness = self.project / "harness.py"
        harness.write_text(
            'import io,json,runpy,sys\nfrom pathlib import Path\nfrom unittest.mock import patch\n'
            'root=Path(__file__).resolve().parent\n'
            'def respond(request, **kwargs):\n'
            '    """在远端传输边界返回固定正式回复。"""\n'
            '    p=json.loads(request.data)\n'
            '    with (root/"requests.jsonl").open("a",encoding="utf-8") as f:\n'
            '        f.write(json.dumps({k:p[k] for k in ("ConversationId","RequestId")})+"\\n")\n'
            '    data={"Type":"response.completed","Response":{"Status":"success","ConversationId":p["ConversationId"],"Messages":[{"Type":"reply","Contents":[{"Type":"text","Text":"reply"}]}]}}\n'
            '    stream=io.BytesIO(("event: response.completed\\ndata: "+json.dumps(data)+"\\n\\n").encode())\n'
            '    stream.headers={"Content-Type":"text/event-stream"}\n'
            '    return stream\n'
            'sys.argv=[str(root/"main.py")]\n'
            'with patch("adp.sse.open_request",side_effect=respond):\n'
            '    runpy.run_path(sys.argv[0],run_name="__main__")\n',
            encoding="utf-8",
        )
        original_state = None
        for question in ("第一轮", "第二轮"):
            result = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(harness)], input=question + "\n",
                cwd=self.outside, env=self.environment, capture_output=True, encoding="utf-8", timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "reply\n")
            state = (self.project / "runtime/session.json").read_bytes()
            if original_state is not None:
                self.assertEqual(state, original_state)
            original_state = state
        payloads = [json.loads(line) for line in (self.project / "requests.jsonl").read_text().splitlines()]
        self.assertEqual(payloads[0]["ConversationId"], payloads[1]["ConversationId"])
        self.assertNotEqual(payloads[0]["RequestId"], payloads[1]["RequestId"])
        self.assertTrue((self.project / "logs/adp_chat.log").is_file())
        self.assertFalse((self.project / "src/runtime").exists())
        self.assertFalse((self.project / "src/logs").exists())

    def test_installed_console_starts_with_root_config(self):
        """正式控制台入口加载根配置后正常退出，不使用 src 模块入口。"""
        executable = Path(sysconfig.get_path("scripts")) / ("adp-chat.exe" if os.name == "nt" else "adp-chat")
        result = subprocess.run(
            [str(executable)], input="/exit\n", cwd=self.project, env=self.environment,
            capture_output=True, encoding="utf-8", timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("输入文字消息", result.stderr)
        self.assertFalse((self.project / "runtime/session.json").exists())

    def test_local_config_ignores_deployment_fields_instead_of_validating_server_values(self):
        """本地加载器不解析服务器监听参数，也不把它们传进对话配置。"""
        path = self.project / "config.toml"
        self.set_deployment('bind_ip="not-a-local-interface"\nhost_port="server-only"')
        config = load_config(path)
        self.assertFalse(hasattr(config, "bind_ip"))
        self.assertFalse(hasattr(config, "host_port"))
        executable = Path(sysconfig.get_path("scripts")) / ("adp-chat.exe" if os.name == "nt" else "adp-chat")
        result = subprocess.run([str(executable)], input="/exit\n", cwd=self.project, env=self.environment,
                                capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.project / "runtime/session.json").exists())

    def test_local_uvicorn_uses_explicit_host_port_despite_deployment_settings(self):
        """真实本地 HTTP 进程保持启动参数，服务器配置不能改变其监听。"""
        self.set_deployment('bind_ip="server-only"\nhost_port="ignored-locally"')
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, "-B", "-X", "utf8", "-m", "uvicorn", "api.app:app",
             "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
            cwd=self.outside, env=self.environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    self.fail(process.stderr.read().decode("utf-8", errors="replace"))
                try:
                    with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=0.5) as response:
                        self.assertEqual(json.load(response), {"status": "ok"})
                    break
                except (URLError, TimeoutError):
                    time.sleep(0.05)
            else:
                self.fail("本地 Uvicorn 未在指定端口就绪")
        finally:
            if process.poll() is None:
                if os.name == "nt":
                    # venv 启动器可能另起 Python 子进程；父句柄仍有效时回收自有进程树。
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                   check=True, capture_output=True, timeout=5)
                else:
                    process.terminate()
            process.wait(timeout=5)
            process.stderr.close()
        with socket.socket() as probe:
            self.assertNotEqual(probe.connect_ex(("127.0.0.1", port)), 0)

    def set_deployment(self, fields):
        """替换 fixture 的部署表，兼容正式模板是否已经包含该表。"""
        path = self.project / "config.toml"
        content = re.sub(r"(?ms)^\[deployment\]\n.*?(?=^\[|\Z)", "", path.read_text("utf-8"))
        path.write_text(content + "\n[deployment]\n" + fields + "\n", encoding="utf-8")
