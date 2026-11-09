"""从正式加密 HTTP 回调到持久化、ADP 最终结果与图文出站的关键链路。"""

import asyncio
import base64
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from adp.async_client import AsyncADPClient
from api.app import create_app
from core.config import ChatConfig
from storage.messages import message_key, read_snapshot
from wecom.crypto import WeComCrypto
from wecom.sender import WeComSender


class ImageWorkflowTests(unittest.TestCase):
    """仅隔离外部 ADP/企微/图片 HTTP，真实执行回调验签、文件状态及 Worker。"""

    def test_callback_to_ordered_native_reply_and_duplicate_is_not_resent(self):
        """员工提交一次后收到文字/图/文字；相同 MsgId 回调不重复执行或通知。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = ChatConfig("fake-key", "local", corp_id="wwfixture", agent_id=1,
                adp_application="image-workflow", gateway_enabled=True, callback_token="fixturetoken",
                encoding_aes_key=base64.b64encode(os.urandom(32)).decode()[:-1],
                wecom_app_secret="fake-secret", notification_mode="wecom", log_enabled=False,
                messages_dir=str(root / "messages"), session_file=str(root / "session.json"))
            submitted, sent = [], []

            def handle(request):
                """通过正式协议交付最终 reply，思考内容绝不能进入用户通知。"""
                if request.url.path == "/adp/v2/chat":
                    payload = json.loads(request.content)
                    submitted.append(payload)
                    self.assertEqual((payload["Stream"], payload["Incremental"]), ("disable", False))
                    value = {"Type": "response.completed", "Response": {"Status": "success",
                        "ConversationId": payload["ConversationId"], "Messages": [
                            {"Type": "thought", "Contents": [{"Type": "text", "Text": "内部思考"}]},
                            {"Type": "reply", "Contents": [{"Type": "text", "Text":
                                "步骤一![截图](https://lke.cloud.tencent.com/a.png)\n来源与服务台尾部"}]}]}}
                    content = "event: response.completed\ndata: " + json.dumps(value) + "\n\n"
                    return httpx.Response(200, content=content, headers={"Content-Type": "text/event-stream"})
                if request.url.host == "8.8.8.8":
                    return httpx.Response(200, headers={"Content-Type": "image/png"},
                        stream=httpx.ByteStream(b"\x89PNG\r\n\x1a\nfixture"))
                if request.url.path == "/cgi-bin/gettoken":
                    return httpx.Response(200, json={"errcode": 0, "access_token": "fake-token", "expires_in": 7200})
                if request.url.path == "/cgi-bin/media/upload":
                    return httpx.Response(200, json={"type": "image", "media_id": "fake-media", "created_at": 1})
                self.assertEqual(request.url.path, "/cgi-bin/message/send")
                sent.append(json.loads(request.content))
                return httpx.Response(200, json={"errcode": 0})

            http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            app = create_app()
            crypto = WeComCrypto(config.callback_token, config.encoding_aes_key, config.corp_id)
            timestamp, nonce = str(int(time.time())), "fixture"
            xml = (f"<xml><ToUserName>{config.corp_id}</ToUserName><FromUserName>YNND</FromUserName>"
                   f"<CreateTime>{timestamp}</CreateTime><MsgType>text</MsgType><Content>修改密码</Content>"
                   "<MsgId>12345</MsgId><AgentID>1</AgentID></xml>").encode()
            encrypted = crypto.encrypt(xml)
            query = {"timestamp": timestamp, "nonce": nonce,
                     "msg_signature": crypto.signature(encrypted, timestamp, nonce)}
            body = (f"<xml><ToUserName>{config.corp_id}</ToUserName><AgentID>1</AgentID>"
                    f"<Encrypt>{encrypted}</Encrypt></xml>").encode()
            key = message_key(config.corp_id, config.agent_id, "12345")

            async def completed():
                """等待明确磁盘终态，不能用固定延迟代替状态断言。"""
                async with asyncio.timeout(5):
                    while True:
                        record = app.state.wecom_gateway.store.get(key)
                        if record and record["send_state"] == "SEND_SIMULATED":
                            return record
                        await asyncio.sleep(0.005)

            try:
                with patch("api.app.load_config", return_value=config), \
                     patch("api.app.AsyncADPClient", side_effect=lambda c: AsyncADPClient(c, http_client=http)), \
                     patch("api.app.create_sender", side_effect=lambda c: WeComSender(c, http_client=http)), \
                     patch("wecom.images.resolve_public", return_value="8.8.8.8"), TestClient(app) as client:
                    response = client.post("/wecom/callback", params=query, content=body)
                    self.assertEqual((response.status_code, response.content), (200, b""))
                    record = client.portal.call(completed)
                    self.assertEqual([p["msgtype"] for p in sent], ["text", "image", "text"])
                    self.assertEqual(sent[-1]["text"]["content"], "\n来源与服务台尾部")
                    self.assertEqual(record["send_result"]["accepted_parts"], 3)
                    self.assertEqual(record["schema_version"], 3)
                    self.assertEqual(client.post("/wecom/callback", params=query, content=body).status_code, 200)
                    self.assertEqual((len(submitted), len(sent)), (1, 3))
                    self.assertEqual(read_snapshot(root / "messages" / f"{key}.json")["adp_state"], "ADP_SUCCEEDED")
                    self.assertNotIn("内部思考", str(sent))
            finally:
                asyncio.run(http.aclose())


if __name__ == "__main__":
    unittest.main()
