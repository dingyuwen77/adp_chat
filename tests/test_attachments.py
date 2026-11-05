import io
import json
import tempfile
import unittest
from dataclasses import replace
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from adp_chat import ChatConfig, ChatError, chat
from adp_chat.attachments import _credential_request, _make_signed_url
from test_chat import FakeStream, completed


_HOST = "fixture-1250000000.cos.ap-guangzhou.myqcloud.com"
_OBJECT = f"https://{_HOST}/corp/example/sample.txt"
_UPLOAD = f"{_OBJECT}?q-signature=put-test"
_DOWNLOAD = f"{_OBJECT}?q-signature=get-test"
_STORAGE = {
    "Bucket": "fixture-1250000000", "Region": "ap-guangzhou", "UploadPath": "/corp/example/sample.txt",
    "UploadUrl": _UPLOAD, "FileUrl": _DOWNLOAD,
}


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        """为附件流程创建真实的中文路径和仅用于测试的配置。"""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name) / "带 空格"
        folder.mkdir()
        self.path = folder / "文档.TXT"
        self.content = "只存在于附件的校验内容".encode("utf-8")
        self.path.write_bytes(self.content)
        self.config = ChatConfig(
            "dummy-key", "tester", bot_biz_id="test-app", secret_id="test-id", secret_key="test-secret",
        )

    def test_file_uploads_full_path_and_sends_presigned_url_to_v2(self):
        """实际读出嵌套文件，私有上传并在 V2 中提交预签名下载 URL。"""
        operations = []

        def cos_io(request, **kwargs):
            """在边界收集真实请求方法、数据和阶段。"""
            data = request.data.read() if request.data is not None else None
            operations.append((request.get_method(), request.full_url, data, kwargs["stage"]))
            return io.BytesIO(b"")

        with (
            patch("adp_chat.attachments._storage_request", return_value=_STORAGE) as credentials,
            patch("adp_chat.attachments.open_request", side_effect=cos_io),
            patch("adp_chat.sse.open_request", return_value=FakeStream(completed())) as sse,
        ):
            result = chat("提取校验内容", config=self.config, file=self.path)

        credentials.assert_called_once_with(self.config, "txt", False, "credentials")
        self.assertEqual(operations, [
            ("PUT", _UPLOAD, self.content, "upload"), ("GET", _DOWNLOAD, None, "sign"),
        ])
        payload = json.loads(sse.call_args.args[0].data)
        self.assertEqual(payload["Contents"][0], {"Type": "text", "Text": "提取校验内容"})
        self.assertEqual(payload["Contents"][1]["Type"], "file")
        file = payload["Contents"][1]["File"]
        self.assertEqual(file["FileName"], self.path.name)
        self.assertEqual(file["FileSize"], str(len(self.content)))
        self.assertEqual(file["FileType"], "txt")
        self.assertEqual(file["FileUrl"], _DOWNLOAD)
        self.assertIsInstance(file["CreateTime"], int)
        self.assertNotIn("DocId", file)
        self.assertEqual(result.text, "完整回答")

    def test_falls_back_to_download_credentials_for_unreadable_file_url(self):
        """下载地址拒绝访问时，申请下载场景权限并核验替代链接。"""
        alternate = f"{_OBJECT}?q-signature=alternate"
        with (
            patch("adp_chat.attachments._storage_request", side_effect=[_STORAGE, _STORAGE]) as credentials,
            patch("adp_chat.attachments._make_signed_url", return_value=alternate) as signing,
            patch("adp_chat.attachments.open_request", side_effect=[
                io.BytesIO(), ChatError("sign", "http_error", "403", http_status=403), io.BytesIO(),
            ]),
            patch("adp_chat.sse.open_request", return_value=FakeStream(completed())) as sse,
        ):
            chat("问题", config=self.config, file=self.path)

        self.assertEqual(credentials.call_args.args, (self.config, None, False, "sign"))
        self.assertEqual(signing.call_args.args[1:3], (_STORAGE["UploadPath"], "GET"))
        self.assertEqual(json.loads(sse.call_args.args[0].data)["Contents"][1]["File"]["FileUrl"], alternate)

    def test_failed_upload_does_not_start_chat(self):
        """COS 上传失败会中止本轮并保留关联请求 ID。"""
        with (
            patch("adp_chat.attachments._storage_request", return_value=_STORAGE),
            patch("adp_chat.attachments.open_request", side_effect=ChatError("upload", "timeout", "超时")),
            patch("adp_chat.sse.open_request") as sse,
        ):
            with self.assertRaises(ChatError) as caught:
                chat("问题", config=self.config, file=self.path)
        self.assertEqual(caught.exception.stage, "upload")
        self.assertTrue(caught.exception.request_id)
        sse.assert_not_called()

    def test_missing_file_and_public_opt_in_are_checked_before_cloud(self):
        """文件不存在或图片未授权公开时，禁止申请云端凭证。"""
        image_path = self.path.with_suffix(".jpeg")
        image_path.write_bytes(b"test-image")
        with patch("adp_chat.attachments._storage_request") as credentials:
            with self.assertRaises(ChatError) as missing:
                chat("问题", config=self.config, file=self.path.with_name("不存在.txt"))
            with self.assertRaises(ChatError) as disabled:
                chat("问题", config=self.config, image=image_path)
        self.assertEqual(missing.exception.code, "missing_file")
        self.assertEqual(disabled.exception.code, "image_upload_disabled")
        credentials.assert_not_called()

    def test_image_uses_uploaded_object_without_put_signature(self):
        """图片上传显式使用公开场景，提交与上传一致的对象路径。"""
        image_path = self.path.with_suffix(".jpeg")
        image_path.write_bytes(b"test-image")
        url = f"https://{_HOST}/public/%E6%A0%B7%E4%BE%8B.jpeg"
        storage = {**_STORAGE, "UploadUrl": f"{url}?q-signature=put-test"}
        enabled = replace(self.config, allow_public_image_upload=True)
        with (
            patch("adp_chat.attachments._storage_request", return_value=storage) as credentials,
            patch("adp_chat.attachments.open_request", return_value=io.BytesIO()),
            patch("adp_chat.sse.open_request", return_value=FakeStream(completed())) as sse,
        ):
            chat("描述图片", config=enabled, image=image_path)
        credentials.assert_called_once_with(enabled, "jpeg", True, "credentials")
        self.assertEqual(json.loads(sse.call_args.args[0].data)["Contents"][1], {
            "Type": "image", "Image": {"Url": url},
        })

    def test_signed_url_never_logs_credentials_at_debug_level(self):
        """宿主打开 DEBUG 时，签名过程仍保持静默。"""
        storage = {
            **_STORAGE, "ExpiredTime": 1800000120,
            "Credentials": {"TmpSecretId": "test-id", "TmpSecretKey": "test-secret", "Token": "test-token"},
        }
        with patch("time.time", return_value=1800000000), self.assertNoLogs(level="DEBUG"):
            _make_signed_url(storage, storage["UploadPath"], "PUT", self.config)

    @unittest.skipUnless(find_spec("tencentcloud") and find_spec("qcloud_cos"), "未安装附件 SDK")
    def test_sdk_signatures_match_expected_request_and_token(self):
        """使用真实 SDK 与固定时间离线验证 TC3 请求和 COS 签名。"""
        from qcloud_cos import CosConfig, CosS3Client
        from tencentcloud.common import credential
        from tencentcloud.common.common_client import CommonClient
        from tencentcloud.common.http.request import RequestInternal

        params = {"BotBizId": "test-app", "FileType": "txt", "TypeKey": "realtime", "IsPublic": False}
        storage = {
            **_STORAGE, "ExpiredTime": 1800000120,
            "Credentials": {"TmpSecretId": "test-id", "TmpSecretKey": "test-secret", "Token": "test-token"},
        }
        with patch("time.time", return_value=1800000000):
            request = _credential_request(self.config, params)
            sdk = CommonClient("lke", "2023-11-30", credential.Credential("test-id", "test-secret"), "ap-guangzhou")
            reference = RequestInternal("lke.tencentcloudapi.com", "POST", "/")
            sdk._build_req_inter("DescribeStorageCredential", params, reference)
            self.assertEqual(request.get_header("Authorization"), reference.header["Authorization"])
            self.assertEqual(request.data, reference.data.encode())
            url = _make_signed_url(storage, storage["UploadPath"], "GET", self.config)
            cos = CosS3Client(CosConfig(
                Region="ap-guangzhou", SecretId="test-id", SecretKey="test-secret", Token="test-token",
            ))
            for method in ("GET", "PUT"):
                for key in (storage["UploadPath"], "/corp/带 空格/图%.txt"):
                    with self.subTest(method=method, key=key):
                        expected = cos.get_presigned_url(
                            Bucket=storage["Bucket"], Key=key, Method=method,
                            Expired=120, Params={"x-cos-security-token": "test-token"},
                        )
                        actual = _make_signed_url(storage, key, method, self.config)
                        self.assertEqual(actual, expected)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["x-cos-security-token"], ["test-token"])
        # COS SDK 会把签名起点回拨一分钟容忍时钟偏差，终点仍不得越过凭证过期时间。
        self.assertEqual(query["q-sign-time"], ["1799999940;1800000120"])
        self.assertEqual(urlsplit(url).path, "/corp/example/sample.txt")
