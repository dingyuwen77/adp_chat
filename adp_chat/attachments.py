import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request

from .config import ChatConfig
from .models import ChatError
from .transport import open_request


_IMAGE_TYPES = frozenset({"bmp", "jpeg", "jpg", "png", "webp"})
_DOCUMENT_TYPES = frozenset({"csv", "doc", "docx", "json", "md", "pdf", "ppt", "pptx", "txt", "xls", "xlsx"})
_LKE_HOST = "lke.tencentcloudapi.com"


def _credential_request(config: ChatConfig, params: dict) -> Request:
    """复用腾讯云 SDK 的 TC3 签名函数，并用独立传输避免记录鉴权内容。"""
    from tencentcloud.common.sign import Sign

    body = json.dumps(params).encode("utf-8")
    content_type = "application/json"
    timestamp = int(time.time())
    date = datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d")
    scope = f"{date}/lke/tc3_request"
    algorithm = "TC3-HMAC-SHA256"
    signed_headers = "content-type;host"
    canonical = (
        f"POST\n/\n\ncontent-type:{content_type}\nhost:{_LKE_HOST}\n\n"
        f"{signed_headers}\n{hashlib.sha256(body).hexdigest()}"
    )
    string_to_sign = (
        f"{algorithm}\n{timestamp}\n{scope}\n{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"
    )
    signature = Sign.sign_tc3(config.secret_key, date, "lke", string_to_sign)
    authorization = (
        f"{algorithm} Credential={config.secret_id}/{scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )
    return Request(
        f"https://{_LKE_HOST}/", data=body, method="POST", headers={
            "Authorization": authorization, "Content-Type": content_type,
            "Host": _LKE_HOST, "X-TC-Action": "DescribeStorageCredential",
            "X-TC-Region": config.region, "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": "2023-11-30",
        },
    )


def _storage_request(config: ChatConfig, file_type: str | None, public: bool, stage: str) -> dict:
    """请求对应场景的 ADP 临时凭证，下载场景省略上传专用字段。"""
    params = {"BotBizId": config.bot_biz_id, "IsPublic": public}
    if file_type is not None:
        params.update({"FileType": file_type, "TypeKey": "realtime"})
    try:
        request = _credential_request(config, params)
    except ImportError:
        raise ChatError("config", "missing_sdk", "请安装附件依赖后再使用文件或图片。") from None

    try:
        with open_request(request, timeout=config.attachment_timeout_seconds, stage=stage) as response:
            result = json.loads(response.read(65536))["Response"]
    except (ValueError, KeyError, TypeError):
        raise ChatError(stage, "invalid_credentials", "腾讯云临时凭证响应格式无效。") from None
    except TimeoutError:
        raise ChatError(stage, "timeout", "读取腾讯云临时凭证超时。") from None
    except (OSError, http.client.HTTPException):
        raise ChatError(stage, "network_error", "读取腾讯云临时凭证时连接中断。") from None

    if not isinstance(result, dict):
        raise ChatError(stage, "invalid_credentials", "腾讯云临时凭证响应格式无效。")
    if result.get("Error"):
        error = result["Error"]
        code = error.get("Code") if isinstance(error, dict) else None
        request_id = result.get("RequestId")
        raise ChatError(
            stage, "provider_error", "腾讯云拒绝发放 COS 临时凭证。",
            provider_code=code if isinstance(code, str) and code.replace(".", "").isalnum() else None,
            provider_request_id=request_id if isinstance(request_id, str) and request_id.replace("-", "").isalnum() else None,
        )
    return result


def _object_host(storage: dict) -> str:
    """根据平台返回的存储桶和地域确定 COS 对象域名。"""
    bucket, region = storage.get("Bucket"), storage.get("Region")
    if not all(isinstance(value, str) and value.replace("-", "").isalnum() for value in (bucket, region)):
        raise ChatError("credentials", "invalid_storage", "腾讯云没有返回有效的 COS 桶和地域。")
    return f"{bucket}.cos.{region}.myqcloud.com".lower()


def _cos_url(value: object, host: str, *, signed: bool) -> str | None:
    """只接受预期 COS 域名的 HTTPS 地址，签名地址必须带查询部分。"""
    if not isinstance(value, str) or not value.isascii() or not value.isprintable() or " " in value:
        return None
    try:
        parts = urlsplit(value)
        if (
            parts.scheme == "https" and parts.hostname == host and parts.port in (None, 443)
            and parts.username is None and parts.password is None and not parts.fragment
            and parts.path and (not signed or parts.query)
        ):
            return value
    except ValueError:
        pass
    return None


def _make_signed_url(storage: dict, key: str, method: str, config: ChatConfig) -> str:
    """生成 COS V5 签名链接，绑定对象、方法、域名和临时 Token。"""
    stage = "upload" if method == "PUT" else "sign"
    credentials = storage.get("Credentials")
    expiry = storage.get("ExpiredTime")
    if not isinstance(credentials, dict) or type(expiry) is not int:
        raise ChatError(stage, "invalid_credentials", "腾讯云未返回可用于 COS 签名的临时凭证。")
    fields = [credentials.get(name) for name in ("TmpSecretId", "TmpSecretKey", "Token")]
    if not all(isinstance(value, str) and value for value in fields):
        raise ChatError(stage, "invalid_credentials", "腾讯云临时凭证字段不完整。")
    now = int(time.time())
    end = min(now + config.signed_url_seconds, expiry)
    if end <= now:
        raise ChatError(stage, "expired_credentials", "腾讯云临时凭证已过期。")
    secret_id, secret_key, token = fields
    host = _object_host(storage)
    uri = "/" + key.removeprefix("/")
    token_name = "x-cos-security-token"
    key_time = f"{now - 60};{end}"
    # 按 COS 规范签入 Token 和 Host，签名原文及派生密钥均留在函数内。
    canonical = f"{method.lower()}\n{uri}\n{token_name}={quote(token, safe='')}\nhost={host}\n"
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()
    string_to_sign = f"sha1\n{key_time}\n{digest}\n".encode("utf-8")
    signing_key = hmac.new(secret_key.encode("utf-8"), key_time.encode(), hashlib.sha1).hexdigest()
    signature = hmac.new(signing_key.encode(), string_to_sign, hashlib.sha1).hexdigest()
    params = {
        "q-sign-algorithm": "sha1", "q-ak": secret_id,
        "q-sign-time": key_time, "q-key-time": key_time,
        "q-header-list": "host", "q-url-param-list": token_name,
        "q-signature": signature, token_name: token,
    }
    # 保留对象键中的斜线，并避免 HTTP 客户端归一化键里的点路径。
    encoded_uri = quote(uri, safe="/-_.~").replace("./", ".%2F")
    return f"https://{host}{encoded_uri}?{urlencode(params)}"


def _is_readable(url: str, timeout: float) -> bool:
    """用只读的单字节范围请求核验预签名下载权限。"""
    request = Request(url, headers={"Range": "bytes=0-0"}, method="GET")
    try:
        with open_request(request, timeout=timeout, stage="sign"):
            return True
    except ChatError as exc:
        if exc.http_status in (401, 403, 404):
            return False
        raise


def _file_url(storage: dict, host: str, key: str, config: ChatConfig) -> str:
    """优先使用平台的下载链接，再按下载权限签发并核验。"""
    timeout = config.attachment_timeout_seconds
    supplied = _cos_url(storage.get("FileUrl"), host, signed=True)
    if supplied and _is_readable(supplied, timeout):
        return supplied
    download = _storage_request(config, None, False, "sign")
    if _object_host(download) != host:
        raise ChatError("sign", "storage_mismatch", "下载凭证与上传对象的 COS 桶或地域不一致。")
    signed = _make_signed_url(download, key, "GET", config)
    if not _is_readable(signed, timeout):
        raise ChatError("sign", "unreadable", "签名链接无法读取已上传的文档，请检查 COS 下载权限。")
    return signed


def prepare_attachment(path: str | Path, image: bool, config: ChatConfig) -> dict:
    """上传一个本地附件，并生成适用于 V2 的图片或文件内容。"""
    try:
        path = Path(path).absolute()
    except (OSError, TypeError, ValueError):
        raise ChatError("input", "invalid_path", "本地附件路径无效。") from None
    extension = path.suffix.removeprefix(".").lower()
    if extension not in (_IMAGE_TYPES if image else _DOCUMENT_TYPES):
        raise ChatError("input", "unsupported_type", "附件扩展名与指定的文档或图片类型不匹配。")
    if image and config.allow_public_image_upload is not True:
        raise ChatError("config", "image_upload_disabled", "请在配置中显式允许公开图片上传。")
    if not all(value.strip() for value in (config.bot_biz_id, config.secret_id, config.secret_key, config.region)):
        raise ChatError("config", "missing_credentials", "请在 [attachments] 填写应用 ID、地域和腾讯云凭据。")

    try:
        with path.open("rb") as source:
            details = os.fstat(source.fileno())
            if not stat.S_ISREG(details.st_mode) or details.st_size <= 0:
                raise ChatError("input", "invalid_file", "附件必须是非空的本地普通文件。")
            storage = _storage_request(config, extension, image, "credentials")
            host = _object_host(storage)
            key = storage.get("UploadPath")
            if not isinstance(key, str) or not key:
                raise ChatError("credentials", "missing_object_key", "腾讯云未返回 COS 对象路径。")
            upload_url = _cos_url(storage.get("UploadUrl"), host, signed=True)
            if upload_url is None:
                upload_url = _make_signed_url(storage, key, "PUT", config)
            request = Request(
                upload_url, data=source, method="PUT", headers={
                    "Content-Length": str(details.st_size),
                    "Content-Type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
                },
            )
            with open_request(request, timeout=config.attachment_timeout_seconds, stage="upload"):
                pass
    except FileNotFoundError:
        raise ChatError("input", "missing_file", "找不到指定的本地附件。") from None
    except (OSError, ValueError):
        raise ChatError("input", "unreadable_file", "无法读取本地附件，请检查访问权限。") from None

    if image:
        # 沿用实际上传地址的对象路径和编码，去掉 PUT 签名再交给 ADP 读取。
        url = urlsplit(upload_url)._replace(query="").geturl()
        return {"Type": "image", "Image": {"Url": url}}

    created_at = getattr(details, "st_birthtime_ns", details.st_mtime_ns)
    return {
        "Type": "file",
        "File": {
            "FileName": path.name, "FileSize": str(details.st_size), "FileType": extension,
            "FileUrl": _file_url(storage, host, key, config),
            "CreateTime": created_at // 1_000_000,
        },
    }
