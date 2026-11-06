"""通过服务器内网 IP 调用 adp_chat FastAPI；只发送一次，不自动重试。"""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_URL = "http://10.102.70.82:4560"


def send_message(base_url: str, message: str, timeout: float = 240) -> dict:
    """向单用户会话提交本轮文字，返回完整 JSON 响应。"""
    data = json.dumps({"message": message}, ensure_ascii=False).encode("utf-8")
    request = Request(
        base_url.rstrip("/") + "/api/v1/chat",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="调用内网 adp_chat FastAPI（不自动重试）")
    parser.add_argument("message", nargs="?", default="你好", help="要发送的一轮文字")
    parser.add_argument("--url", default=DEFAULT_URL, help="服务器 URL，例如 http://10.102.70.82:4560")
    parser.add_argument("--timeout", type=float, default=240, help="客户端等待秒数")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    try:
        result = send_message(args.url, args.message, args.timeout)
    except HTTPError as exc:
        try:
            body = json.load(exc)
            error = body.get("error", {})
            detail = f"{error.get('code', 'HTTP_ERROR')}: {error.get('message', '请求被拒绝')}"
        except (ValueError, AttributeError, TypeError):
            detail = "服务器返回非 JSON 错误响应"
        print(f"HTTP {exc.code}: {detail}", file=sys.stderr)
        return 1
    except (URLError, TimeoutError, OSError) as exc:
        print(f"网络错误或超时：{exc}。请求可能已到达 ADP，请不要盲目重试。", file=sys.stderr)
        return 1
    print(json.dumps({
        "text": result.get("text", ""),
        "conversation_id": result.get("conversation_id"),
        "request_id": result.get("request_id"),
        "record_id": result.get("record_id"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
