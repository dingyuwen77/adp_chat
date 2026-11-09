"""隔离 HTTP 的图片下载、素材上传与有序发送契约，不发送真实企业微信消息。"""

import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx

from core.config import ChatConfig
from wecom.sender import WeComSender
from wecom.images import ImageBudget, ImageDownloader, ImageError, resolve_public


PNG = b"\x89PNG\r\n\x1a\n" + b"fixture-image"


class ImageSenderTests(unittest.IsolatedAsyncioTestCase):
    """正式 Sender 经 Fake HTTP 观察输出类型、内容和失败边界。"""

    def setUp(self):
        """仅使用测试凭据与明确允许的图片主机。"""
        self.config = ChatConfig("fixture", "local", corp_id="wwtest", agent_id=1,
            adp_application="test", wecom_app_secret="fixture-secret")
        self.record = {"user_id": "YNND", "adp_state": "ADP_SUCCEEDED", "notification": "旧提示",
                       "adp_result": {"reply_contents": [[{"Type": "text", "Text":
                           "步骤一![截图](https://lke.cloud.tencent.com/a.png?sig=private)步骤二"}]]}}
        self.sent, self.uploads, self.downloads = [], [], []

    async def send(self, *, download_status=200, upload_error=0, image_error=None, redirect_host=None):
        """仅替换远端 HTTP/DNS；正式规划、下载校验和发送逻辑均参与。"""
        def handle(request):
            """校验每个生产协议，伪造图片、素材和消息响应。"""
            if request.url.host == "8.8.8.8":
                self.downloads.append(request)
                self.assertIn(request.headers["host"], {"lke.cloud.tencent.com", redirect_host})
                self.assertEqual(request.extensions["sni_hostname"], request.headers["host"])
                self.assertNotIn("authorization", request.headers)
                if redirect_host and request.headers["host"] == "lke.cloud.tencent.com":
                    return httpx.Response(302, headers={"Location": f"https://{redirect_host}/image.png?sig=private"})
                return httpx.Response(download_status, stream=httpx.ByteStream(PNG), headers={"Content-Type": "image/png"})
            if request.url.path == "/cgi-bin/gettoken":
                return httpx.Response(200, json={"errcode": 0, "access_token": "fixture-token", "expires_in": 7200})
            if request.url.path == "/cgi-bin/media/upload":
                self.uploads.append(request)
                self.assertEqual(request.url.params["type"], "image")
                self.assertIn(b'name="media"', request.content)
                self.assertIn(PNG, request.content)
                return httpx.Response(200, json={"errcode": upload_error, "type": "image", "media_id": "fixture-media"})
            self.assertEqual(request.url.path, "/cgi-bin/message/send")
            payload = json.loads(request.content)
            self.sent.append(payload)
            if payload["msgtype"] == "image" and image_error:
                raise image_error
            return httpx.Response(200, json={"errcode": 0})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            with patch("wecom.images.resolve_public", return_value="8.8.8.8"):
                sender = WeComSender(self.config, http_client=http)
                try:
                    return await sender.send(self.record)
                finally:
                    await sender.aclose()

    async def test_native_image_is_between_texts_and_counted(self):
        """实际消息序列为文字/图片/文字，无旧媒体误提示且计数为三。"""
        outcome = await self.send()
        self.assertEqual([p["msgtype"] for p in self.sent], ["text", "image", "text"])
        self.assertEqual([self.sent[0]["text"]["content"], self.sent[2]["text"]["content"]], ["步骤一", "步骤二"])
        self.assertEqual(self.sent[1]["image"], {"media_id": "fixture-media"})
        self.assertEqual((outcome.state, outcome.accepted_parts, outcome.total_parts), ("SEND_SIMULATED", 3, 3))

    async def test_download_or_upload_failure_falls_back_in_place(self):
        """未提交图片消息时，失败只降级一次并继续后文。"""
        for options in ({"download_status": 404}, {"upload_error": 40005}):
            self.sent.clear()
            outcome = await self.send(**options)
            self.assertEqual([p["msgtype"] for p in self.sent], ["text", "text", "text"])
            self.assertIn("图片未能显示", self.sent[1]["text"]["content"])
            self.assertIn("sig=private", self.sent[1]["text"]["content"])
            self.assertEqual(outcome.state, "SEND_SIMULATED")
            self.assertEqual(outcome.code, "accepted_with_image_fallback")

    async def test_default_allows_cross_domain_cdn_and_sends_native_image(self):
        """默认短链跳到未列出的公网 CDN 后仍发送原生图片，不降级为链接。"""
        outcome = await self.send(redirect_host="images.example.net")
        self.assertEqual([p["msgtype"] for p in self.sent], ["text", "image", "text"])
        self.assertEqual([r.headers["host"] for r in self.downloads],
                         ["lke.cloud.tencent.com", "images.example.net"])
        self.assertEqual((outcome.code, len(self.uploads)), ("accepted", 1))

    async def test_image_message_timeout_stops_and_never_falls_back_or_resends(self):
        """提交后超时是 UNKNOWN，不能再补链接或重发图片。"""
        outcome = await self.send(image_error=httpx.ReadTimeout("private URL must not be logged"))
        self.assertEqual((outcome.state, outcome.accepted_parts, outcome.total_parts), ("SEND_UNKNOWN", 1, 3))
        self.assertEqual([p["msgtype"] for p in self.sent], ["text", "image"])

    async def test_disallowed_host_falls_back_without_download(self):
        """显式名单和空名单继续阻止未允许来源，只发送降级链接。"""
        self.record["adp_result"]["reply_contents"][0][0]["Text"] = "![x](https://evil.example/a.png)"
        for hosts in ([], ["lke.cloud.tencent.com"]):
            with self.subTest(hosts=hosts):
                self.config = replace(self.config, image_allowed_hosts=hosts)
                self.sent.clear()
                outcome = await self.send()
                self.assertEqual(self.downloads, [])
                self.assertEqual(outcome.state, "SEND_SIMULATED")
                self.assertIn("图片未能显示", self.sent[0]["text"]["content"])

    async def test_image_count_limit_keeps_order_and_explicit_fallback(self):
        """第九张不再下载，前八张和原位置降级均纳入接受计数。"""
        self.record["adp_result"]["reply_contents"] = [[
            {"Type": "image", "Image": {"Url": f"https://lke.cloud.tencent.com/{i}.png"}}
            for i in range(9)]]
        outcome = await self.send()
        self.assertEqual((len(self.downloads), len(self.uploads)), (8, 8))
        self.assertEqual([p["msgtype"] for p in self.sent], ["image"] * 8 + ["text"])
        self.assertEqual((outcome.accepted_parts, outcome.total_parts), (9, 9))

    async def test_shared_prepare_slots_bound_upload_lifetime_and_allow_text_progress(self):
        """百人请求共享2GiB预约预算：最多51张准备，其余等待且纯文字可继续。"""
        active, peak, downloads = 0, 0, 0
        full, release = asyncio.Event(), asyncio.Event()

        async def handle(request):
            """挂起51个素材上传，未获预算者不能先下载图片。"""
            nonlocal active, peak, downloads
            if request.url.host == "8.8.8.8":
                downloads += 1
                return httpx.Response(200, stream=httpx.ByteStream(PNG), headers={"Content-Type": "image/png"})
            if request.url.path == "/cgi-bin/gettoken":
                return httpx.Response(200, json={"errcode": 0, "access_token": "fixture-token", "expires_in": 7200})
            if request.url.path == "/cgi-bin/media/upload":
                active += 1
                peak = max(peak, active)
                if active == 51:
                    full.set()
                try:
                    await release.wait()
                    return httpx.Response(200, json={"type": "image", "media_id": "fixture-media"})
                finally:
                    active -= 1
            return httpx.Response(200, json={"errcode": 0})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            sender = WeComSender(self.config, http_client=http)
            with patch("wecom.images.resolve_public", return_value="8.8.8.8"):
                tasks = [asyncio.create_task(sender.send({**self.record, "user_id": f"EMP{i}"})) for i in range(100)]
                try:
                    await asyncio.wait_for(full.wait(), 2)
                    outcome = await sender.send({"user_id": "OTHER", "notification": "纯文字"})
                    self.assertEqual(outcome.state, "SEND_SIMULATED")
                    self.assertEqual(downloads, 51)
                finally:
                    release.set()
                    outcomes = await asyncio.gather(*tasks)
                    await sender.aclose()
            self.assertEqual((peak, active, downloads), (51, 0, 100))
            self.assertTrue(all(o.state == "SEND_SIMULATED" for o in outcomes))

    async def test_full_image_budget_times_out_in_place_and_recovers_capacity(self):
        """预算全满时不下载，等待超时降级并保留尾文；释放后新图片正常发送。"""
        sent, downloads, uploads = [], [], []

        def handle(request):
            """仅模拟图片、Token、素材及发送响应，观察正式发送器的结果。"""
            if request.url.host == "8.8.8.8":
                downloads.append(request)
                return httpx.Response(200, headers={"Content-Type": "image/png"}, stream=httpx.ByteStream(PNG))
            if request.url.path == "/cgi-bin/gettoken":
                return httpx.Response(200, json={"errcode": 0, "access_token": "fixture-token", "expires_in": 7200})
            if request.url.path == "/cgi-bin/media/upload":
                uploads.append(request)
                return httpx.Response(200, json={"type": "image", "media_id": "fixture-media"})
            sent.append(json.loads(request.content))
            return httpx.Response(200, json={"errcode": 0})

        config = replace(self.config, image_prepare_timeout_seconds=0.01)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            sender = WeComSender(config, http_client=http)
            held = 0
            try:
                async with asyncio.timeout(1):
                    for _ in range(51):
                        await sender.images.slots.acquire()
                        held += 1
                with patch("wecom.images.resolve_public", return_value="8.8.8.8"):
                    outcome = await sender.send(self.record)
                    self.assertEqual(outcome.code, "accepted_with_image_fallback")
                    self.assertEqual((downloads, uploads), ([], []))
                    self.assertEqual([p["msgtype"] for p in sent], ["text", "text", "text"])
                    self.assertIn("图片未能显示", sent[1]["text"]["content"])
                    self.assertEqual(sent[-1]["text"]["content"], "步骤二")
                    for _ in range(held):
                        sender.images.slots.release()
                    held = 0
                    sent.clear()
                    outcome = await sender.send(self.record)
                    self.assertEqual((outcome.code, len(downloads), len(uploads)), ("accepted", 1, 1))
                    self.assertEqual([p["msgtype"] for p in sent], ["text", "image", "text"])
            finally:
                for _ in range(held):
                    sender.images.slots.release()
                await sender.aclose()

    async def test_prepare_timeout_falls_back_without_image_message(self):
        """图片准备超时尚未提交图片消息，仍能发送降级及尾文。"""
        self.config = replace(self.config, image_prepare_timeout_seconds=0.01)
        self.sent.clear()

        async def blocked(block, budget):
            """无限等待由正式准备预算取消，不靠人为延长超时制造成功。"""
            await asyncio.Event().wait()

        with patch("wecom.images.ImageDownloader.download", side_effect=blocked):
            outcome = await self.send()
        self.assertEqual(outcome.code, "accepted_with_image_fallback")
        self.assertEqual([p["msgtype"] for p in self.sent], ["text", "text", "text"])


class ImageDownloadTests(unittest.IsolatedAsyncioTestCase):
    """下载安全独立于微信接口，用实际 HTTPX 请求断言公网固定连接。"""

    async def test_dns_rejects_any_private_or_mixed_result(self):
        """混合公网/私网 DNS 不能只挑公网结果后继续。"""
        import socket

        loop = asyncio.get_running_loop()
        for ips in (("127.0.0.1",), ("169.254.169.254",), ("::1",), ("8.8.8.8", "10.1.1.1")):
            records = [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM,
                        6, "", (ip, 443)) for ip in ips]
            with patch.object(loop, "getaddrinfo", return_value=records):
                with self.assertRaises(ImageError):
                    await resolve_public("lke.cloud.tencent.com")

    async def test_blocked_urls_and_redirects_do_not_reach_untrusted_destination(self):
        """凭据、私有协议、端口、片段与跨域跳转在发包前被拒绝。"""
        requests = []

        def handle(request):
            """可信入口只返回恶意跳转，不能自动跟随。"""
            requests.append(request)
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle), follow_redirects=True) as http:
            config = ChatConfig("fixture", "local", image_allowed_hosts=["lke.cloud.tencent.com"])
            downloader = ImageDownloader(config, http_client=http)
            with patch("wecom.images.resolve_public", return_value="8.8.8.8"):
                for url in ("file:///tmp/a.png", "http://lke.cloud.tencent.com/a", "https://evil.example/a",
                            "https://user:secret@lke.cloud.tencent.com/a", "https://lke.cloud.tencent.com:8443/a",
                            "https://lke.cloud.tencent.com/a#fragment", "https://127.0.0.1/a"):
                    with self.assertRaises(ImageError):
                        await downloader.download(url, ImageBudget())
                self.assertEqual(requests, [])
                with self.assertRaises(ImageError):
                    await downloader.download("https://lke.cloud.tencent.com/start", ImageBudget())
            self.assertEqual(len(requests), 1)

    async def test_default_allows_arbitrary_public_https_origin(self):
        """任意公网 HTTPS 图片首链均可下载，不要求预先登记域名。"""
        requests = []

        def handle(request):
            """返回 PNG 并记录真实下载器生成的固定 IP 请求。"""
            requests.append(request)
            return httpx.Response(200, headers={"Content-Type": "image/png"}, stream=httpx.ByteStream(PNG))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            downloader = ImageDownloader(ChatConfig("fixture", "local"), http_client=http)
            with patch("wecom.images.resolve_public", return_value="8.8.8.8"):
                result = await downloader.download("https://unlisted.example.org/a.png", ImageBudget())
        self.assertEqual(result, (PNG, "image/png"))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "8.8.8.8")
        self.assertEqual(requests[0].headers["host"], "unlisted.example.org")
        self.assertEqual(requests[0].extensions["sni_hostname"], "unlisted.example.org")

    async def test_open_domains_still_block_private_origins_and_redirects(self):
        """放开来源后仍通过真实 DNS 校验，内部首链与跳转均不能产生内部 HTTP 请求。"""
        import socket

        loop = asyncio.get_running_loop()
        for target, ips in (("127.0.0.1", ("127.0.0.1",)), ("[::1]", ("::1",)),
                            ("10.0.0.1", ("10.0.0.1",)), ("192.168.1.1", ("192.168.1.1",)),
                            ("169.254.169.254", ("169.254.169.254",)),
                            ("internal.example.net", ("10.1.1.1",)),
                            ("mixed.example.net", ("8.8.8.8", "10.1.1.1"))):
            for redirected in (False, True):
                with self.subTest(target=target, redirected=redirected):
                    requests = []

                    def resolve(host, port, **kwargs):
                        """仅短链域名解析为公网，其余输入返回对应内部或混合地址。"""
                        addresses = ("8.8.8.8",) if host == "lke.cloud.tencent.com" else ips
                        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET,
                                 socket.SOCK_STREAM, 6, "", (ip, port)) for ip in addresses]

                    def handle(request):
                        """公网入口返回内部跳转，记录任何不应发生的后续 HTTP 请求。"""
                        requests.append(request)
                        return httpx.Response(302, headers={"Location": f"https://{target}/image.png"})

                    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                        downloader = ImageDownloader(ChatConfig("fixture", "local"), http_client=http)
                        url = "https://lke.cloud.tencent.com/start" if redirected else f"https://{target}/image.png"
                        with patch.object(loop, "getaddrinfo", side_effect=resolve):
                            with self.assertRaisesRegex(ImageError, "image_address_blocked"):
                                await downloader.download(url, ImageBudget())
                    self.assertEqual(len(requests), int(redirected))
                    self.assertTrue(all(r.url.host == "8.8.8.8" for r in requests))

    async def test_each_redirect_resolves_and_pins_again_without_credential_headers(self):
        """第二跳使用新核验 IP，路径查询保持原样，Host/SNI 始终是原域名。"""
        requests = []

        def handle(request):
            """先跳转再返回流式 PNG，模拟 DNS 在两次请求间变更。"""
            requests.append(request)
            if len(requests) == 1:
                return httpx.Response(302, headers={"Location": "/final.png?size=mid|1*2|0.2&sig=x"})
            return httpx.Response(200, stream=httpx.ByteStream(PNG), headers={"Content-Type": "image/png"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            downloader = ImageDownloader(ChatConfig("fixture", "local"), http_client=http)
            with patch("wecom.images.resolve_public", side_effect=("8.8.8.8", "1.1.1.1")) as resolver:
                body, mime = await downloader.download("https://lke.cloud.tencent.com/start", ImageBudget())
            self.assertEqual((body, mime), (PNG, "image/png"))
            self.assertEqual([r.url.host for r in requests], ["8.8.8.8", "1.1.1.1"])
            self.assertEqual(resolver.await_count, 2)
            self.assertEqual(requests[-1].url.params["size"], "mid|1*2|0.2")
            self.assertTrue(all(r.headers["host"] == "lke.cloud.tencent.com" for r in requests))

    async def test_size_format_encoding_and_total_bytes_are_bounded(self):
        """无长度头的超量流、伪 PNG、压缩响应和不支持格式均不能上传。"""
        for headers, body in (({"Content-Type": "image/png"}, PNG * 4),
                              ({"Content-Type": "image/png"}, b"not a png"),
                              ({"Content-Type": "image/gif"}, PNG),
                              ({"Content-Type": "image/png", "Content-Encoding": "gzip"}, PNG),
                              ({"Content-Type": "image/png", "Content-Length": "99999999"}, PNG)):
            def handle(request):
                """模拟未受信任的大小/MIME/编码与真实流体。"""
                return httpx.Response(200, headers=headers, stream=httpx.ByteStream(body))

            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
                downloader = ImageDownloader(ChatConfig("fixture", "local"), http_client=http)
                with patch("wecom.images.resolve_public", return_value="8.8.8.8"):
                    with self.assertRaises(ImageError):
                        await downloader.download("https://lke.cloud.tencent.com/a", ImageBudget(32))

    async def test_image_request_does_not_forward_cookie_jar(self):
        """按IP连接时不能将连接池Cookie带到同IP托管的其他图片域名。"""
        def handle(request):
            """共享IP的旧Cookie不能进入图片GET。"""
            self.assertNotIn("private-image-cookie", request.headers.get("cookie", ""))
            return httpx.Response(200, headers={"Content-Type": "image/png"}, stream=httpx.ByteStream(PNG))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            http.cookies.set("image", "private-image-cookie", domain="8.8.8.8")
            downloader = ImageDownloader(ChatConfig("fixture", "local"), http_client=http)
            with patch("wecom.images.resolve_public", return_value="8.8.8.8"):
                self.assertEqual((await downloader.download("https://lke.cloud.tencent.com/a", ImageBudget()))[0], PNG)

    async def test_config_accepts_full_wildcard_and_preserves_explicit_lists(self):
        """默认及显式完整通配开放公网域名，旧完整名单及空名单仍可使用。"""
        config = ChatConfig("fixture", "local")
        self.assertEqual(config.image_allowed_hosts, ["*"])
        self.assertEqual(replace(config, image_allowed_hosts=["*"]).image_allowed_hosts, ["*"])
        self.assertEqual(replace(config, image_allowed_hosts=["lke.cloud.tencent.com"]).image_allowed_hosts,
                         ["lke.cloud.tencent.com"])

    async def test_config_rejects_partial_wildcards_invalid_types_and_unbounded_timeout(self):
        """不引入子域模式解析；畸形名单和无界超时仍拒绝，空列表继续禁用。"""
        from core.models import ChatError

        config = ChatConfig("fixture", "local")
        self.assertEqual(replace(config, image_allowed_hosts=[]).image_allowed_hosts, [])
        for hosts in (["*.tencent.com"], ["https://lke.cloud.tencent.com"], [None], "lke.cloud.tencent.com"):
            with self.assertRaises(ChatError):
                replace(config, image_allowed_hosts=hosts)
        for seconds in (0, -1, float("inf"), float("nan"), 301):
            with self.assertRaises(ChatError):
                replace(config, image_prepare_timeout_seconds=seconds)


if __name__ == "__main__":
    unittest.main()
