"""本机协议模拟客户端；复用正式加密实现，不连接企微云端。"""

import argparse
import base64
import json
import os
import re
import secrets
import time
import tomllib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen
from xml.sax.saxutils import escape

from core.config import default_config_path, load_config
from core.models import ChatError
from wecom.crypto import WeComCrypto


def initialize_test_keys(path: Path) -> None:
    """仅填充现有配置中的空测试密钥，不覆盖任何已有凭据或其他值。"""
    text = path.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    section = data.get("wecom", {})
    if (not section.get("corp_id") or not section.get("agent_id") or not section.get("adp_application")
            or section.get("callback_token") or section.get("encoding_aes_key") or section.get("gateway_enabled")):
        raise ValueError("先配置完整 wecom 身份作用域；已有密钥或已启用的网关不自动覆盖")
    values = {
        "gateway_enabled": "true", "callback_token": json.dumps(secrets.token_hex(12)),
        "encoding_aes_key": json.dumps(base64.b64encode(os.urandom(32)).decode()[:-1]),
        "messages_dir": '"runtime/messages"', "callback_max_bytes": "65536", "max_unfinished_messages": "2000",
    }
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "[wecom]")
    end = next((i for i in range(start + 1, len(lines)) if re.match(r"\s*\[", lines[i])), len(lines))
    block = lines[start + 1:end]
    for key, value in values.items():
        matches = [i for i, line in enumerate(block) if re.match(r"\s*" + key + r"\s*=", line)]
        if matches:
            # 只更改明确空凭据和启用位，保留已有资源/路径配置。
            if key in ("gateway_enabled", "callback_token", "encoding_aes_key"):
                block[matches[0]] = f"{key} = {value}"
        else:
            block.append(f"{key} = {value}")
    updated = "\n".join(lines[:start + 1] + block + lines[end:]) + "\n"
    tomllib.loads(updated)
    # 独立临时文件后原子替换，避免部分写入损坏原来的 AppKey 配置。
    import tempfile

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(updated)
            output.flush()
            os.fsync(output.fileno())
        load_config(temporary)
        if os.name == "nt":
            # ReplaceFile 保留原配置 ACL；普通 os.replace 会带入临时文件的权限。
            import ctypes
            from ctypes import wintypes

            replace_file = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
            replace_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                    wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID)
            replace_file.restype = wintypes.BOOL
            if not replace_file(str(path.absolute()), str(temporary.absolute()), None, 0, None, None):
                raise ctypes.WinError(ctypes.get_last_error())
        else:
            import stat

            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
            os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    """只向 loopback 正式回调提交测试密文，可重复 MsgId 验证去重。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--init-test-keys", action="store_true")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--verify-url", action="store_true")
    parser.add_argument("--user-id", default="YNND")
    parser.add_argument("--message", default="本地协议测试")
    parser.add_argument("--msg-id", default=str(time.time_ns()))
    args = parser.parse_args(argv)
    try:
        if args.init_test_keys:
            initialize_test_keys(args.config)
            load_config(args.config)
            print(json.dumps({"test_keys_initialized": True, "secrets_displayed": False}))
            return 0
        config = load_config(args.config)
        if not config.gateway_enabled:
            raise ValueError("请先在现有配置启用测试网关")
        target = urlsplit(args.url)
        if (target.scheme != "http" or target.hostname not in ("127.0.0.1", "localhost", "::1")
                or target.username or target.password or target.query or target.fragment or target.path not in ("", "/")):
            raise ValueError("A～E 模拟客户端仅允许本机 HTTP 根地址")
        if not re.fullmatch(r"[A-Za-z0-9_@.\-]{1,64}", args.user_id) or not re.fullmatch(r"[0-9]{1,32}", args.msg_id):
            raise ValueError("UserID 或 MsgId 格式无效")
        crypto = WeComCrypto(config.callback_token, config.encoding_aes_key, config.corp_id)
        timestamp, nonce = str(int(time.time())), secrets.token_hex(8)
        if args.verify_url:
            plaintext = b"local-wecom-url-check"
        else:
            plaintext = (f"<xml><ToUserName>{escape(config.corp_id)}</ToUserName>"
                         f"<FromUserName>{escape(args.user_id)}</FromUserName><CreateTime>{timestamp}</CreateTime>"
                         f"<MsgType>text</MsgType><Content>{escape(args.message)}</Content>"
                         f"<MsgId>{args.msg_id}</MsgId><AgentID>{config.agent_id}</AgentID></xml>").encode()
        ciphertext = crypto.encrypt(plaintext)
        params = {"timestamp": timestamp, "nonce": nonce, "msg_signature": crypto.signature(ciphertext, timestamp, nonce)}
        data = None
        if args.verify_url:
            params["echostr"] = ciphertext
        else:
            data = (f"<xml><ToUserName>{escape(config.corp_id)}</ToUserName><AgentID>{config.agent_id}</AgentID>"
                    f"<Encrypt>{ciphertext}</Encrypt></xml>").encode()
        request = Request(args.url.rstrip("/") + "/wecom/callback?" + urlencode(params), data=data,
                          headers={"Content-Type": "application/xml"})
        with urlopen(request, timeout=5) as response:
            body = response.read(1024)
            valid = body == (b"local-wecom-url-check" if args.verify_url else b"")
            print(json.dumps({"source": "test_fixture", "cloud_identity_verified": False,
                              "status": response.status, "expected_ack": valid}, ensure_ascii=False))
            return 0 if valid else 1
    except ChatError as exc:
        print(json.dumps({"stage": exc.stage, "code": exc.code}))
    except HTTPError as exc:
        print(json.dumps({"http_status": exc.code, "code": "callback_rejected"}))
        exc.close()
    except (OSError, URLError, ValueError, StopIteration):
        print(json.dumps({"code": "local_callback_test_failed", "secrets_displayed": False}))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
