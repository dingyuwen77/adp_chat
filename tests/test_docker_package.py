"""通过真实 PowerShell 入口验证版本产物；Docker 构建边界使用受控替身。"""

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch
from pathlib import Path

from core.models import ChatError


ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts/build_image.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
IMAGE_ID = "sha256:" + "a" * 64
CONFIG_CONTENT = b'{"architecture":"arm64","os":"linux"}'
CONFIG_DIGEST = hashlib.sha256(CONFIG_CONTENT).hexdigest()


class DeploymentBindingConfigTests(unittest.TestCase):
    """通过正式内嵌工具的 plan 入口验证部署配置及历史回退。"""

    def setUp(self):
        """使用真实临时 TOML 和状态文件，仅隔离容器执行边界。"""
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.config = root / "config.toml"
        self.state = root / "state.json"
        self.content = ('[adp]\napp_key="fixture-key"\nuser_id="fixture-user"\n'
                        '[session]\nsession_file="../data/session.json"\n'
                        '[logging]\nlog_file="../data/adp_chat.log"\n')
        text = (ROOT / "scripts/run.sh").read_text(encoding="utf-8")
        body = text.split("# BEGIN_DEPLOY_HELPER\n", 1)[1].split("# END_DEPLOY_HELPER", 1)[0]
        body = body.split("\ntry:\n    main()", 1)[0]
        self.namespace = {}
        environment = patch.dict(os.environ, DEPLOY_ROOT="/srv/fixture/release", DEPLOY_VERSION="v1.2.3",
                                 DEPLOY_HAS_CONTAINER="", ADP_BIND_IP="", ADP_HOST_PORT="")
        environment.start()
        self.addCleanup(environment.stop)
        exec(compile(body, "scripts/run.sh", "exec"), self.namespace)
        self.namespace.update(CONFIG=self.config, STATE=self.state)

    def plan(self, extra="", *, saved=None, environment=None):
        """走真实 TOML/状态读取和 plan 输出，返回调用者可见的键值。"""
        self.config.write_text(self.content + extra, encoding="utf-8")
        if saved is not None:
            state = {"schema_version": 1, "release_dir": "/srv/fixture/release", "version": "v1.2.2",
                     "image_id": IMAGE_ID, "session_file": "/srv/fixture/data/session.json",
                     "conversation_id": "fixture-conversation", "session_sha256": "b" * 64, **saved}
            self.state.write_text(json.dumps(state), encoding="utf-8")
        else:
            self.state.unlink(missing_ok=True)
        output = io.StringIO()
        with (patch.dict(os.environ, environment or {}),
              patch.object(self.namespace["sys"], "argv", ["deploy-helper", "plan"]),
              redirect_stdout(output)):
            self.namespace["main"]()
        return dict(line.split("=", 1) for line in output.getvalue().splitlines())

    def test_toml_binding_overrides_saved_state_and_environment_can_override_toml(self):
        """显式配置取代旧部署记录，既有环境变量仍可临时覆盖。"""
        settings = '[deployment]\nbind_ip="127.0.0.2"\nhost_port=5678\n'
        saved = {"bind_ip": "127.0.0.1", "host_port": 4567}
        result = self.plan(settings, saved=saved)
        self.assertEqual((result["BIND_IP"], result["HOST_PORT"]), ("127.0.0.2", "5678"))
        result = self.plan(settings, saved=saved,
                           environment={"ADP_BIND_IP": "127.0.0.3", "ADP_HOST_PORT": "06789"})
        self.assertEqual((result["BIND_IP"], result["HOST_PORT"]), ("127.0.0.3", "6789"))

    def test_omitted_fields_fall_back_independently_to_state_or_original_defaults(self):
        """省略分组/字段保持旧行为，配置一个字段不会重置另一个字段。"""
        result = self.plan()
        self.assertEqual((result["BIND_IP"], result["HOST_PORT"]), ("10.102.70.82", "4560"))
        saved = {"bind_ip": "127.0.0.1", "host_port": 4567}
        result = self.plan(saved=saved)
        self.assertEqual((result["BIND_IP"], result["HOST_PORT"]), ("127.0.0.1", "4567"))
        result = self.plan('[deployment]\nhost_port=5678\n', saved=saved)
        self.assertEqual((result["BIND_IP"], result["HOST_PORT"]), ("127.0.0.1", "5678"))
        result = self.plan('[deployment]\nbind_ip="127.0.0.2"\n', saved=saved)
        self.assertEqual((result["BIND_IP"], result["HOST_PORT"]), ("127.0.0.2", "4567"))

    def test_invalid_deployment_settings_are_rejected_even_with_valid_environment(self):
        """不允许类型混淆、超界端口或未知字段被环境覆盖掩盖。"""
        for fields in ('bind_ip="localhost"', 'bind_ip="::1"', 'bind_ip=""', 'bind_ip=123',
                       'host_port=true', 'host_port="5678"', 'host_port=1.5', 'host_port=0',
                       'host_port=65536', 'host_port=-1', 'unknown=1'):
            with self.subTest(fields=fields):
                with self.assertRaises(self.namespace["DeploymentError"]):
                    self.plan('[deployment]\n' + fields + '\n',
                              environment={"ADP_BIND_IP": "127.0.0.1", "ADP_HOST_PORT": "4567"})
        self.content = 'deployment="invalid-group"\n' + self.content
        with self.assertRaises(self.namespace["DeploymentError"]):
            self.plan()

    def test_old_image_config_rejection_explains_staged_upgrade_before_stopping(self):
        """模拟旧镜像加载器的明确拒绝，部署计划给出保留原配置的升级步骤。"""
        legacy = Mock(side_effect=ChatError("config", "unknown_section", "fixture old reader"))
        with patch.dict(self.namespace, load_config=legacy):
            with self.assertRaisesRegex(self.namespace["DeploymentError"], "先保持不含该分组的原配置"):
                self.plan('[deployment]\nbind_ip="127.0.0.1"\nhost_port=4567\n')


class ServerScriptFormatTests(unittest.TestCase):
    """保护从 Windows 手工上传到 Linux 的脚本格式。"""

    def test_shell_sources_are_utf8_lf_and_embedded_python_parses(self):
        """三个服务器入口真实存在、无 BOM/CRLF，内嵌校验工具可编译。"""
        for name in ("run.sh", "stop.sh", "upgrade.sh"):
            with self.subTest(script=name):
                data = (ROOT / "scripts" / name).read_bytes()
                self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
                self.assertNotIn(b"\r", data)
                self.assertTrue(data.decode("utf-8").startswith("#!/usr/bin/env bash\n"))
        text = (ROOT / "scripts/run.sh").read_text(encoding="utf-8")
        body = text.split("# BEGIN_DEPLOY_HELPER\n", 1)[1].split("# END_DEPLOY_HELPER", 1)[0]
        compile(body, "scripts/run.sh", "exec")

    def test_image_identity_accepts_only_archive_verified_backend_representations(self):
        """同一归档的索引/配置摘要均可用；任意镜像或伪造配置摘要必须拒绝。"""
        text = (ROOT / "scripts/run.sh").read_text(encoding="utf-8")
        body = text.split("# BEGIN_DEPLOY_HELPER\n", 1)[1].split("# END_DEPLOY_HELPER", 1)[0]
        body = body.split("\ntry:\n    main()", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "v1.2.3").mkdir()
            with tarfile.open(root / "v1.2.3/image.tar", "w") as archive:
                for name, data in (("manifest.json", json.dumps([{"Config": CONFIG_DIGEST + ".json"}]).encode()),
                                   (CONFIG_DIGEST + ".json", CONFIG_CONTENT)):
                    member = tarfile.TarInfo(name)
                    member.size = len(data)
                    archive.addfile(member, io.BytesIO(data))
            namespace = {}
            with patch.dict(os.environ, DEPLOY_ROOT=str(root), DEPLOY_VERSION="v1.2.3"):
                exec(compile(body, "scripts/run.sh", "exec"), namespace)
                namespace["Path"] = lambda value: root
                manifest = {"image_id": IMAGE_ID, "image_config_id": "sha256:" + CONFIG_DIGEST,
                            "archive": "image.tar"}
                for identity in (IMAGE_ID, "sha256:" + CONFIG_DIGEST):
                    with patch.dict(os.environ, DEPLOY_IMAGE_ID=identity):
                        namespace["verify_image_identity"](manifest)
                with patch.dict(os.environ, DEPLOY_IMAGE_ID="sha256:" + "c" * 64):
                    with self.assertRaises(namespace["DeploymentError"]):
                        namespace["verify_image_identity"](manifest)
                with patch.dict(os.environ, DEPLOY_IMAGE_ID=IMAGE_ID):
                    with self.assertRaises(namespace["DeploymentError"]):
                        namespace["verify_image_identity"]({**manifest, "image_config_id": "sha256:" + "b" * 64})

    def test_unreadable_state_and_nonregular_session_never_mean_first_deployment(self):
        """权限错误和符号链接不能被 Path.exists 吞成首次部署。"""
        text = (ROOT / "scripts/run.sh").read_text(encoding="utf-8")
        body = text.split("# BEGIN_DEPLOY_HELPER\n", 1)[1].split("# END_DEPLOY_HELPER", 1)[0]
        body = body.split("\ntry:\n    main()", 1)[0]
        namespace = {}
        with patch.dict(os.environ, DEPLOY_ROOT="/fixture", DEPLOY_VERSION="v1.2.3"):
            exec(compile(body, "scripts/run.sh", "exec"), namespace)
        state = Mock()
        namespace["STATE"] = state
        state.exists.return_value = False
        state.lstat.side_effect = PermissionError("fixture inaccessible")
        with self.assertRaises(PermissionError):
            namespace["read_state"]()
        state.lstat.side_effect = FileNotFoundError
        self.assertEqual(namespace["read_state"](), {})
        state.lstat.side_effect = None
        state.lstat.return_value.st_mode = 0o120777
        with self.assertRaises(namespace["DeploymentError"]):
            namespace["read_state"]()
        namespace["Path"] = lambda value: state
        with self.assertRaises(namespace["DeploymentError"]):
            namespace["check_session"](Mock(session_file="/fixture/session.json"), {}, False)
        state.lstat.side_effect = PermissionError("fixture inaccessible")
        with self.assertRaises(PermissionError):
            namespace["check_session"](Mock(session_file="/fixture/session.json"), {}, False)


@unittest.skipUnless(POWERSHELL, "本机没有 PowerShell，无法运行 Windows 打包入口")
class DockerPackageTests(unittest.TestCase):
    """验证调用者可见的成功、拒绝、失败清理及不可覆盖行为。"""

    def setUp(self):
        """在含空格的独立目录准备源码和可解析的 Docker 归档替身。"""
        self.assertTrue(BUILDER.is_file(), "缺少 scripts/build_image.ps1 正式入口")
        temporary = tempfile.TemporaryDirectory(prefix="adp package test ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "scripts").mkdir()
        shutil.copy2(BUILDER, self.root / "scripts/build_image.ps1")
        for name in ("Dockerfile", "requirements.txt", "pyproject.toml", ".dockerignore"):
            shutil.copy2(ROOT / name, self.root / name)
        (self.root / "config.toml").write_text("private-fixture-do-not-export", encoding="utf-8")
        self.fixture = self.root / "fixture.tar"
        self.write_fixture()

    def write_fixture(self, platform="linux/arm64"):
        """准备所选架构的受控配置字节；默认期望独立于脚本文本。"""
        config = CONFIG_CONTENT if platform == "linux/arm64" else b'{"architecture":"amd64","os":"linux"}'
        config_digest = hashlib.sha256(config).hexdigest()
        with tarfile.open(self.fixture, "w") as archive:
            content = json.dumps([{"Config": config_digest + ".json", "RepoTags": [], "Layers": []}]).encode()
            member = tarfile.TarInfo("manifest.json")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
            member = tarfile.TarInfo(config_digest + ".json")
            member.size = len(config)
            archive.addfile(member, io.BytesIO(config))

    def run_builder(self, version="v1.2.3", *, fail="", engine=POWERSHELL, platform=None):
        """调用完整脚本，替换 Docker 进程边界，文件和 tar 验证仍实际运行。"""
        fake = r'''
function global:docker {
    $global:LASTEXITCODE = 0
    $items = @($args)
    if ($items[0] -eq 'info') { Write-Output 'linux'; return }
    if ($items[0] -eq 'build') {
        $position = [Array]::IndexOf($items, '--platform')
        $global:AdpTestArchitecture = $items[$position + 1].Split('/')[1]
        if ($env:ADP_TEST_FAIL -eq 'build') { $global:LASTEXITCODE = 23 }
        return
    }
    if ($items[0] -eq 'image' -and $items[1] -eq 'ls') { return }
    if ($items[0] -eq 'image' -and $items[1] -eq 'inspect') {
        Write-Output ('{"Id":"sha256:' + ('a' * 64) + '","Os":"linux","Architecture":"' + $global:AdpTestArchitecture + '"}')
        return
    }
    if ($items[0] -eq 'image' -and $items[1] -eq 'save') {
        if ($env:ADP_TEST_FAIL -eq 'save') { $global:LASTEXITCODE = 24; return }
        $position = [Array]::IndexOf($items, '--output')
        Copy-Item -LiteralPath $env:ADP_TEST_TAR -Destination $items[$position + 1]
        return
    }
    if ($items[0] -eq 'image' -and $items[1] -in @('tag', 'rm')) { return }
    throw ('Unexpected Docker fixture invocation: ' + ($items -join ' '))
}
$parameters = @{Version = $env:ADP_TEST_VERSION}
if ($env:ADP_TEST_PLATFORM) { $parameters.Platform = $env:ADP_TEST_PLATFORM }
& $env:ADP_TEST_BUILDER @parameters
'''
        self.write_fixture(platform or "linux/arm64")
        encoded = base64.b64encode(fake.encode("utf-16-le")).decode("ascii")
        return subprocess.run(
            [engine, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            env={**os.environ, "ADP_TEST_FAIL": fail, "ADP_TEST_TAR": str(self.fixture),
                 "ADP_TEST_BUILDER": str(self.root / "scripts/build_image.ps1"),
                 "ADP_TEST_VERSION": version, "ADP_TEST_PLATFORM": platform or ""},
            cwd=self.root.parent, capture_output=True, timeout=20,
        )

    def test_versioned_artifacts_and_integrity_from_both_powershell_editions(self):
        """两代 PowerShell 均生成一致的版本、镜像标识和可复核的完整产物。"""
        engines = [POWERSHELL]
        other = shutil.which("pwsh.exe")
        if other and other != POWERSHELL:
            engines.append(other)
        for index, engine in enumerate(engines):
            version = f"v1.2.{index + 3}"
            with self.subTest(engine=engine):
                result = self.run_builder(version, engine=engine)
                self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
                destination = self.root / "dist" / version
                archive = destination / f"adp_channel_api-{version}-linux-arm64.tar"
                manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["version"], version)
                self.assertEqual(manifest["image"], "adp_channel_api:" + version)
                self.assertEqual(manifest["image_id"], IMAGE_ID)
                self.assertEqual(manifest["image_config_id"], "sha256:" + CONFIG_DIGEST)
                self.assertEqual(manifest["platform"], "linux/arm64")
                self.assertTrue(manifest["built_at"].endswith("+08:00"))
                self.assertEqual(manifest["archive_sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
                with tarfile.open(archive) as contents:
                    self.assertEqual(contents.getnames(), ["manifest.json", CONFIG_DIGEST + ".json"])
                sums = (destination / "SHA256SUMS").read_text(encoding="ascii").splitlines()
                self.assertEqual(len(sums), 2)
                for row in sums:
                    digest, filename = row.split("  ", 1)
                    self.assertEqual(hashlib.sha256((destination / filename).read_bytes()).hexdigest(), digest)
                self.assertEqual({p.name for p in destination.iterdir()}, {archive.name, "manifest.json", "SHA256SUMS"})

    def test_explicit_amd64_override_is_preserved(self):
        """默认ARM64之外仍可显式选择AMD64，清单与文件名必须一致。"""
        result = self.run_builder(platform="linux/amd64")
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
        destination = self.root / "dist/v1.2.3"
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["platform"], "linux/amd64")
        self.assertTrue((destination / "adp_channel_api-v1.2.3-linux-amd64.tar").is_file())
        self.assertFalse((destination / "adp_channel_api-v1.2.3-linux-arm64.tar").exists())

    def test_invalid_version_is_rejected_before_creating_dist(self):
        """路径穿越和非正式版本不能进入构建或写入输出目录。"""
        for version in ("../escape", "v1.2.3;bad", "v1.2.3-rc.1", "v01.2.3"):
            with self.subTest(version=version):
                self.assertNotEqual(self.run_builder(version).returncode, 0)
                self.assertFalse((self.root / "dist").exists())

    def test_build_and_save_failure_leave_no_published_version(self):
        """构建或导出失败不发布版本，也不删除既有用户产物。"""
        (self.root / "dist").mkdir()
        marker = self.root / "dist/keep.txt"
        marker.write_bytes(b"keep")
        for failure in ("build", "save"):
            with self.subTest(stage=failure):
                self.assertNotEqual(self.run_builder(fail=failure).returncode, 0)
                self.assertEqual(list((self.root / "dist").iterdir()), [marker])
                self.assertEqual(marker.read_bytes(), b"keep")

    def test_existing_version_is_not_overwritten(self):
        """相同版本第二次打包必须拒绝，原归档与清单字节保持。"""
        self.assertEqual(self.run_builder().returncode, 0)
        destination = self.root / "dist/v1.2.3"
        before = {p.name: p.read_bytes() for p in destination.iterdir()}
        self.assertNotEqual(self.run_builder().returncode, 0)
        self.assertEqual({p.name: p.read_bytes() for p in destination.iterdir()}, before)

