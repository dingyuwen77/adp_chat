"""在临时 localhost HTTP 服务上验证可交付的 Python 请求示例。"""

import json
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CLIENT = Path(__file__).resolve().parents[1] / "examples/http_client.py"


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path != "/api/v1/chat":
            status, result = 404, {}
        elif body["message"] == "忙碌":
            status, result = 409, {"error": {"code": "CHAT_BUSY", "message": "请稍后"}}
        else:
            status, result = 200, {
                "text": body["message"],
                "conversation_id": "fixture",
                "request_id": "request-1",
                "record_id": None,
            }
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *_args):
        pass


class ExampleClientTests(unittest.TestCase):
    def setUp(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.worker.join(timeout=5)

    def _run(self, message):
        return subprocess.run(
            [sys.executable, str(CLIENT), message, "--url", f"http://127.0.0.1:{self.server.server_port}"],
            capture_output=True, timeout=8,
        )

    def test_unicode_and_correlation_fields(self):
        result = self._run("你好，第二轮")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8"))
        response = json.loads(result.stdout.decode("utf-8"))
        self.assertEqual(response["text"], "你好，第二轮")
        self.assertEqual(response["conversation_id"], "fixture")
        self.assertEqual(response["request_id"], "request-1")

    def test_busy_is_reported_without_retry(self):
        result = self._run("忙碌")
        self.assertEqual(result.returncode, 1)
        self.assertIn("409", result.stderr.decode("utf-8"))
        self.assertIn("CHAT_BUSY", result.stderr.decode("utf-8"))
