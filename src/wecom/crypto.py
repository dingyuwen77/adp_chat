"""企业应用 XML 回调协议：认证先于 AES 解密，不记录输入或凭据。"""

import base64
import binascii
import hashlib
import hmac
import os
import re
import struct
from xml.etree import ElementTree

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from core.models import ChatError


def invalid_callback() -> ChatError:
    """外部认证和格式错误统一安全表达，避免泄露解密细节。"""
    return ChatError("wecom", "invalid_callback", "回调认证或消息格式无效。")


def parse_xml(data: bytes) -> ElementTree.Element:
    """只接收有界调用方传入的 UTF-8 无 DTD XML，拒绝字段歧义。"""
    try:
        text = data.decode("utf-8")
        if re.search(r"<!\s*(DOCTYPE|ENTITY)", text, re.I) or "\0" in text:
            raise ValueError
        root = ElementTree.fromstring(text)
        tags = [child.tag for child in root]
        if root.tag != "xml" or len(set(tags)) != len(tags):
            raise ValueError
        return root
    except (UnicodeError, ElementTree.ParseError, ValueError):
        raise invalid_callback() from None


def xml_field(root: ElementTree.Element, name: str) -> str:
    """获取必需的单一字段，拒绝缺失或空值，不回显原文。"""
    element = root.find(name)
    value = element.text if element is not None else None
    if element is None or len(element) or not isinstance(value, str) or not value:
        raise invalid_callback()
    return value


class WeComCrypto:
    """实现官方 SHA1 排序签名与 AES-256-CBC/32 字节填充协议。"""

    def __init__(self, token: str, encoding_aes_key: str, corp_id: str):
        """配置已由同源配置 Owner 校验，密钥仅留进程内。"""
        self._token = token
        self._key = base64.b64decode(encoding_aes_key + "=", validate=True)
        self._receiver = corp_id.encode("utf-8")

    def signature(self, ciphertext: str, timestamp: str, nonce: str) -> str:
        """计算官方签名，供认证和独立本地模拟输入使用。"""
        return hashlib.sha1("".join(sorted((self._token, timestamp, nonce, ciphertext))).encode()).hexdigest()

    def decrypt(self, ciphertext: str, timestamp: str, nonce: str, signature: str) -> bytes:
        """先常量时间验签，再校验密文、全部填充、网络序长度和接收企业。"""
        try:
            if not re.fullmatch(r"[0-9a-f]{40}", signature) or not hmac.compare_digest(
                    signature, self.signature(ciphertext, timestamp, nonce)):
                raise ValueError
            encrypted = base64.b64decode(ciphertext, validate=True)
            if not encrypted or len(encrypted) % 32:
                raise ValueError
            decryptor = Cipher(algorithms.AES(self._key), modes.CBC(self._key[:16])).decryptor()
            raw = decryptor.update(encrypted) + decryptor.finalize()
            padding = raw[-1]
            if not 1 <= padding <= 32 or raw[-padding:] != bytes([padding]) * padding:
                raise ValueError
            raw = raw[:-padding]
            if len(raw) < 20:
                raise ValueError
            length = struct.unpack("!I", raw[16:20])[0]
            if length > len(raw) - 20 or raw[20 + length:] != self._receiver:
                raise ValueError
            return raw[20:20 + length]
        except (ValueError, UnicodeError, binascii.Error, struct.error):
            raise invalid_callback() from None

    def encrypt(self, message: bytes) -> str:
        """为本地协议客户端准备测试密文，不产生任何发送或认证证据。"""
        raw = os.urandom(16) + struct.pack("!I", len(message)) + message + self._receiver
        padding = 32 - len(raw) % 32
        encryptor = Cipher(algorithms.AES(self._key), modes.CBC(self._key[:16])).encryptor()
        encrypted = encryptor.update(raw + bytes([padding]) * padding) + encryptor.finalize()
        return base64.b64encode(encrypted).decode("ascii")
