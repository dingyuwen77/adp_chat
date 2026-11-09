"""公网图片下载：HTTPS、公网 IP 固定连接、逐跳校验且不落盘。"""

import asyncio
import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REPLY_IMAGE_BYTES = 20 * 1024 * 1024
MAX_REPLY_IMAGES = 8
# 图片预算独立于整个进程：为下载缓冲、bytes及HTTP/TLS临时副本按最大图片四倍预约。
# 下载前取得名额，直到上传结束才释放；等待者只持有URL，不先分配图片二进制。
IMAGE_MEMORY_BUDGET_BYTES = 2 * 1024 * 1024 * 1024
IMAGE_PREPARE_RESERVATION_BYTES = 4 * MAX_IMAGE_BYTES
MAX_IMAGE_PREPARATIONS = IMAGE_MEMORY_BUDGET_BYTES // IMAGE_PREPARE_RESERVATION_BYTES


class ImageError(ValueError):
    """只携带安全错误码，不携带图片地址或响应正文。"""


@dataclass
class ImageBudget:
    """每个回复累计消费实际下载字节，失败下载同样计入预算。"""

    remaining: int = MAX_REPLY_IMAGE_BYTES


async def resolve_public(host: str) -> str:
    """所有 DNS 结果都须公网，返回的 IP 用于实际连接，杜绝二次 DNS 解析。"""
    addresses = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ips = [ipaddress.ip_address(item[4][0]) for item in addresses]
    if not ips or any(not ip.is_global or ip.is_multicast for ip in ips):
        raise ImageError("image_address_blocked")
    return str(ips[0])


class ImageDownloader:
    """独立无凭据连接池；2GiB保守预约预算覆盖下载到上传的完整内存生命周期。"""

    def __init__(self, config, *, http_client=None):
        """正式连接关闭代理、自动跳转、重试和连接复用，避免跨 SNI 共用 IP 连接。"""
        self.config = config
        self.slots = asyncio.Semaphore(MAX_IMAGE_PREPARATIONS)
        self._owned = http_client is None
        if http_client is None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.load_default_certs()
            transport = httpx.AsyncHTTPTransport(verify=context, retries=0,
                limits=httpx.Limits(max_connections=MAX_IMAGE_PREPARATIONS, max_keepalive_connections=0))
            http_client = httpx.AsyncClient(transport=transport, follow_redirects=False,
                                           trust_env=False, timeout=10)
        self.http = http_client

    async def aclose(self):
        """注入的测试客户端由调用方关闭。"""
        if self._owned:
            await self.http.aclose()

    async def download(self, url: str, budget: ImageBudget) -> tuple[bytes, str]:
        """最多三次受限跳转，只读取有界原始 JPG/PNG 字节，绝不传送应用凭据。"""
        for hop in range(4):
            try:
                target = urlsplit(url)
                host = target.hostname
                # 默认开放公网来源；显式名单仍可收紧，公网 IP 与资源约束不会因通配而跳过。
                valid = (target.scheme == "https" and host
                         and ("*" in self.config.image_allowed_hosts or host in self.config.image_allowed_hosts)
                         and target.port in (None, 443) and not target.username and not target.password
                         and not target.fragment and not any(c.isspace() or ord(c) < 32 for c in url))
            except ValueError:
                valid = False
            if not valid:
                raise ImageError("image_url_blocked")
            ip = await resolve_public(host)
            # URL 连接到已核验 IP；Host 与 TLS SNI/证书仍使用原域名。
            pinned = httpx.URL(url).copy_with(host=ip)
            # 空Cookie覆盖客户端CookieJar，防止共用公网IP的不同图片域名串用Cookie。
            async with self.http.stream("GET", pinned, headers={"Host": host, "Accept-Encoding": "identity", "Cookie": ""},
                                        extensions={"sni_hostname": host}, follow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("location")
                    if hop == 3 or not location:
                        raise ImageError("image_redirect_limit")
                    url = urljoin(url, location)
                    continue
                mime = response.headers.get("content-type", "").partition(";")[0].lower().strip()
                if (response.status_code != 200 or mime not in {"image/jpeg", "image/png"}
                        or response.headers.get("content-encoding", "identity").lower() != "identity"):
                    raise ImageError("image_response_rejected")
                limit = min(MAX_IMAGE_BYTES, budget.remaining)
                length = response.headers.get("content-length")
                if limit <= 5 or length is not None and (not length.isdigit() or not 5 < int(length) <= limit):
                    raise ImageError("image_resource_limit")
                body = bytearray()
                async for chunk in response.aiter_raw(chunk_size=65536):
                    budget.remaining -= len(chunk)
                    if len(body) + len(chunk) > limit:
                        raise ImageError("image_resource_limit")
                    body.extend(chunk)
                valid_image = (body.startswith(b"\x89PNG\r\n\x1a\n") if mime == "image/png"
                               else body.startswith(b"\xff\xd8\xff"))
                if len(body) <= 5 or not valid_image:
                    raise ImageError("image_format_rejected")
                return bytes(body), mime
        raise ImageError("image_redirect_limit")
