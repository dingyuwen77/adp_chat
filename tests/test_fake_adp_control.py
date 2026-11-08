"""独立Fake服务的屏障控制；不向正式网关增加测试路由。"""

import asyncio
import unittest
from uuid import uuid4

import httpx

from wecom_fake_adp import create_fake_app


class FakeControlTests(unittest.IsolatedAsyncioTestCase):
    """保持同一Fake网络进程即可重新阻塞后续请求，不重放历史请求。"""

    async def test_rearm_hold_keeps_history_and_only_blocks_future_request(self):
        """第一次完成后重新设屏障，历史调用不变，新请求等待显式release。"""
        app = create_fake_app()

        def payload():
            """每轮独立ID，使用Fake唯一允许的专属AppKey。"""
            return {"AppKey": "fake-local-adp-only", "UserId": "YNND", "VisitorId": "YNND",
                    "ConversationId": "a" * 64, "RequestId": str(uuid4()), "Contents": [{"Type": "text", "Text": "夹具"}]}

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
            first = await client.post("/adp/v2/chat", json=payload())
            self.assertIn("response.completed", first.text)
            held = await client.post("/_test/hold")
            self.assertEqual(held.status_code, 200)
            self.assertEqual(held.json(), {"holding_future_requests": True})
            second = asyncio.create_task(client.post("/adp/v2/chat", json=payload()))
            try:
                async with asyncio.timeout(4):
                    while (await client.get("/_test/status")).json()["active"] != 1:
                        await asyncio.sleep(0.005)
                self.assertFalse(second.done())
                self.assertEqual(len((await client.get("/_test/status")).json()["calls"]), 2)
                await client.post("/_test/release")
                self.assertIn("response.completed", (await second).text)
            finally:
                if not second.done():
                    second.cancel()
                    await asyncio.gather(second, return_exceptions=True)
