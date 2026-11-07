"""显式验证真实 Linux 部署脚本：仅使用临时数据、唯一容器和本机 Docker。"""

import argparse
import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from uuid import uuid4


def docker(*arguments, check=True, timeout=120):
    """保留 Docker 的原始错误，并限制每个验证阶段的执行时间。"""
    result = subprocess.run(["docker", *map(str, arguments)], capture_output=True,
                            text=True, encoding="utf-8", timeout=timeout)
    if check and result.returncode:
        print(result.stderr, file=sys.stderr, flush=True)
        result.check_returncode()
    return result


def inspect_container(name):
    """读取实际容器状态，不把部署日志当作成功证据。"""
    return json.loads(docker("container", "inspect", name).stdout)[0]


def inside(root, first, second):
    """在真实 Linux 文件系统执行公开入口，隔离数据与 Docker 资源名称。"""
    release = root / "release"
    name = os.environ["ADP_CONTAINER_NAME"]
    initial_port = int(os.environ["ADP_HOST_PORT"])
    config = release / "config.toml"
    content = ('[adp]\napp_key="deployment-fixture"\nuser_id="deployment-fixture-user"\n'
               '[session]\nsession_file="../runtime/session.json"\n'
               '[logging]\nlog_file="../log/adp_chat.log"\n')
    config.write_text(content, encoding="utf-8")
    config.chmod(0o640)
    session = root / "runtime/session.json"

    def session_bytes(path=session):
        """以应用身份读取测试会话，不要求宿主调用者读取10001的私有文件。"""
        result = docker("run", "--rm", "--network", "none", "--user", "10001:10001",
                        "--entrypoint", "python", "--mount", f"type=bind,src={path},dst=/fixture/session.json,readonly",
                        "adp_channel_api:" + first, "-c",
                        "from pathlib import Path;import sys;sys.stdout.buffer.write(Path('/fixture/session.json').read_bytes())")
        return result.stdout.encode("utf-8")

    def write_session(data):
        """只修改隔离fixture，用应用身份制造损坏/恢复，不给宿主额外权限。"""
        docker("run", "--rm", "--network", "none", "--user", "10001:10001", "--entrypoint", "python",
               "--mount", f"type=bind,src={session},dst=/fixture/session.json", "adp_channel_api:" + first,
               "-c", "from pathlib import Path;import base64,sys;Path('/fixture/session.json').write_bytes(base64.b64decode(sys.argv[1]))",
               base64.b64encode(data).decode("ascii"))

    def invoke(script, *arguments, success=True):
        """从其他工作目录调用公开脚本，并检查明确的成功或失败退出码。"""
        result = subprocess.run(["sh", str(release / script), *arguments], cwd="/",
                                capture_output=True, text=True, timeout=160)
        print(result.stdout, end="", flush=True)
        print(result.stderr, end="", flush=True)
        assert (result.returncode == 0) == success, (script, result.returncode)
        return result

    def package_fixture(version, command=None):
        """构建唯一测试标签的镜像并生成真实归档，绝不覆盖已有标签。"""
        directory = release / version
        directory.mkdir()
        tag = "adp_channel_api:" + version
        assert docker("image", "inspect", tag, check=False).returncode != 0
        context = root / ("build-" + version)
        context.mkdir()
        recipe = "FROM adp_channel_api:" + first + "\n"
        if command:
            recipe += "CMD " + json.dumps(command) + "\n"
        (context / "Dockerfile").write_text(recipe)
        docker("build", "--tag", tag, "--label", "org.opencontainers.image.version=" + version,
               "--label", "io.nnit.adp.test=" + os.environ["ADP_TEST_ID"], str(context))
        manifest = json.loads((release / first / "manifest.json").read_text())
        image_id = docker("image", "inspect", tag, "--format", "{{.Id}}").stdout.strip()
        archive_name = f"adp_channel_api-{version}-linux-amd64.tar"
        docker("image", "save", "--output", str(directory / archive_name), tag)
        with (directory / archive_name).open("rb") as source:
            archive_hash = hashlib.file_digest(source, "sha256").hexdigest()
        with tarfile.open(directory / archive_name) as saved:
            member = json.load(saved.extractfile("manifest.json"))[0]["Config"]
            with saved.extractfile(member) as source:
                config_id = "sha256:" + hashlib.file_digest(source, "sha256").hexdigest()
        manifest.update(version=version, image=tag, image_id=image_id, image_config_id=config_id,
                        archive=archive_name, archive_sha256=archive_hash)
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        rows = []
        for filename in (archive_name, "manifest.json"):
            with (directory / filename).open("rb") as source:
                rows.append(hashlib.file_digest(source, "sha256").hexdigest() + "  " + filename)
        (directory / "SHA256SUMS").write_text("\n".join(rows) + "\n")
        return manifest

    retired = package_fixture("v0.0.0")
    retired_holder = name + "-retired"
    docker("run", "-d", "--name", retired_holder, "--entrypoint", "sleep", retired["image_id"], "300")
    invoke("run.sh", first)
    assert docker("image", "inspect", retired["image"]).returncode == 0
    docker("container", "rm", "--force", retired_holder)
    original = session_bytes()
    cid = json.loads(original)["conversation_id"]
    assert session.stat().st_uid == 10001
    expected_owner = os.getuid() if os.getuid() else 10001
    assert (root / "runtime").stat().st_uid == (root / "log").stat().st_uid == expected_owner
    state_file = release / ".deploy-state.json"
    assert state_file.stat().st_uid == os.getuid()
    if os.getuid():
        assert (root / "runtime").stat().st_mode & 0o7777 == 0o2770
        assert "1000" in inspect_container(name)["HostConfig"]["GroupAdd"]
        print(f"Non-root deploy process UID/GID={os.getuid()}/{os.getgid()}, Docker access and caller-owned state PASS", flush=True)
    initial_id = inspect_container(name)["Id"]
    print("Initial run: real Linux UID/GID, health and session creation PASS", flush=True)

    # 完整包未传好时必须保持当前容器，而不是先停机再发现错误。
    for version in ("v0.0.9", "v0.0.10"):
        (release / version).mkdir()
    if tuple(map(int, second[1:].split("."))) < (0, 0, 9):
        result = invoke("upgrade.sh", success=False)
        assert "v0.0.10" in result.stderr
        assert inspect_container(name)["Id"] == initial_id
    for version in ("v0.0.9", "v0.0.10"):
        (release / version).rmdir()

    manifest = json.loads((release / second / "manifest.json").read_text())
    archive = release / second / manifest["archive"]
    with archive.open("r+b") as stream:
        saved_byte = stream.read(1)
        stream.seek(0)
        stream.write(bytes([saved_byte[0] ^ 1]))
    try:
        invoke("upgrade.sh", second, success=False)
        assert inspect_container(name)["Id"] == initial_id
    finally:
        with archive.open("r+b") as stream:
            stream.write(saved_byte)

    for altered in (content.replace("../runtime/session.json", "../other/session.json"),
                    content.replace('app_key="deployment-fixture"', 'app_key="different-identity"')):
        config.write_text(altered, encoding="utf-8")
        try:
            invoke("upgrade.sh", second, success=False)
            assert inspect_container(name)["Id"] == initial_id
            assert session_bytes() == original
        finally:
            config.write_text(content, encoding="utf-8")
    held_session = session.with_name("held-session.fixture")
    session.rename(held_session)
    try:
        invoke("upgrade.sh", second, success=False)
        assert inspect_container(name)["Id"] == initial_id and not session.exists()
    finally:
        held_session.rename(session)
    for damaged in (b"not-json", original + b"\n"):
        write_session(damaged)
        try:
            invoke("upgrade.sh", second, success=False)
            assert inspect_container(name)["Id"] == initial_id and session_bytes() == damaged
        finally:
            write_session(original)
    print("Pre-stop protection: numeric selection, checksum, path, identity and missing session PASS", flush=True)

    # 同一文件锁用可观察握手获取，避免以随机 sleep 代替并发验证。
    holder = subprocess.Popen(["bash", "-c",
                               'exec 9>>"$1"; flock 9; echo READY; read -r done',
                               "fixture", str(release / ".deploy.lock")],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "READY"
        invoke("stop.sh", success=False)
        assert inspect_container(name)["State"]["Running"]
    finally:
        holder.stdin.close()
        assert holder.wait(timeout=5) == 1

    invoke("upgrade.sh")
    assert docker("image", "inspect", retired["image"], check=False).returncode != 0
    assert (release / "v0.0.0" / retired["archive"]).is_file()
    print("Old cache: container references protected; unused older tag removed after success, archive retained PASS", flush=True)
    current = inspect_container(name)
    assert current["Config"]["Labels"]["io.nnit.adp.version"] == second
    assert current["Image"] == manifest["image_id"]
    assert session_bytes() == original
    state = json.loads((release / ".deploy-state.json").read_text())
    assert state["conversation_id"] == cid and state["previous_version"] == first
    upgraded_id = current["Id"]
    invoke("run.sh", second)
    assert inspect_container(name)["Id"] == upgraded_id
    # 同一版本仅改变网络参数，也必须替换容器并让成功记录对应真实端口。
    os.environ["ADP_HOST_PORT"] = os.environ["ADP_TEST_ALT_PORT"]
    invoke("upgrade.sh", second)
    changed = inspect_container(name)
    assert changed["Id"] != upgraded_id
    assert changed["HostConfig"]["PortBindings"]["8000/tcp"] == [
        {"HostIp": "127.0.0.1", "HostPort": os.environ["ADP_TEST_ALT_PORT"]}]
    assert json.loads((release / ".deploy-state.json").read_text())["host_port"] == int(os.environ["ADP_TEST_ALT_PORT"])
    assert docker("container", "port", name, "8000/tcp").stdout.strip() == "127.0.0.1:" + os.environ["ADP_TEST_ALT_PORT"]
    del os.environ["ADP_HOST_PORT"]
    invoke("run.sh", second)
    assert inspect_container(name)["Id"] == changed["Id"]
    assert session_bytes() == original
    print("Same-version port update: real bindings, state and persisted idempotency PASS", flush=True)
    invoke("stop.sh")
    assert not inspect_container(name)["State"]["Running"] and session_bytes() == original
    invoke("run.sh", second)
    assert session_bytes() == original
    print("Upgrade, idempotent run, stop and restart: original session bytes and ID PASS", flush=True)

    # 故障镜像只修改临时 Dockerfile 的 CMD，不改产品代码或交付镜像。
    fault = "v9" + uuid4().hex[:10].translate(str.maketrans("abcdef", "123456")) + ".0.0"
    fault_manifest = package_fixture(fault, ["python", "-c", "raise SystemExit(42)"])
    fault_tag = fault_manifest["image"]
    try:
        result = invoke("upgrade.sh", success=False)
        restored = inspect_container(name)
        assert "上一成功版本已恢复" in result.stderr
        assert restored["State"]["Health"]["Status"] == "healthy"
        assert restored["Image"] == manifest["image_id"]
        assert session_bytes() == original
        assert json.loads((release / ".deploy-state.json").read_text())["version"] == second
        print("Failed new CMD: upgrade exit is nonzero, previous image and original session restored PASS", flush=True)
    finally:
        docker("image", "rm", fault_tag, check=False)

    foreign = name + "-foreign"
    docker("run", "-d", "--name", foreign, "--entrypoint", "sleep", manifest["image_id"], "300")
    try:
        result = subprocess.run(["sh", str(release / "stop.sh")], capture_output=True,
                                env={**os.environ, "ADP_CONTAINER_NAME": foreign}, timeout=20)
        assert result.returncode != 0 and inspect_container(foreign)["State"]["Running"]
    finally:
        docker("container", "rm", "--force", foreign)
    print("Container ownership guard PASS; no real ADP request was sent.", flush=True)

    # 撤掉测试环境覆盖，只修改正式 TOML，经公开脚本观察 Docker 绑定和失败恢复。
    saved_binding = {key: os.environ.pop(key) for key in ("ADP_BIND_IP", "ADP_HOST_PORT") if key in os.environ}
    configured_port = initial_port
    configured = content + f'\n[deployment]\nbind_ip="127.0.0.1"\nhost_port={configured_port}\n'
    try:
        config.write_text(configured, encoding="utf-8")
        invoke("upgrade.sh", second)
        configured_container = inspect_container(name)
        assert configured_container["HostConfig"]["PortBindings"]["8000/tcp"] == [
            {"HostIp": "127.0.0.1", "HostPort": str(configured_port)}]
        configured_id = configured_container["Id"]
        invoke("run.sh", second)
        assert inspect_container(name)["Id"] == configured_id
        configured_state = state_file.read_bytes()
        for value in ("true", "0", '"4560"'):
            config.write_text(configured.replace(f"host_port={configured_port}", "host_port=" + value))
            invoke("upgrade.sh", second, success=False)
            assert inspect_container(name)["Id"] == configured_id
            assert state_file.read_bytes() == configured_state and session_bytes() == original
        config.write_text(configured)
        # 故障升级提出新端口时，恢复必须沿用实际旧容器端口而非失败目标。
        alternate = int(os.environ["ADP_TEST_ALT_PORT"])
        config.write_text(configured.replace(f"host_port={configured_port}", f"host_port={alternate}"))
        invoke("upgrade.sh", fault, success=False)
        restored = inspect_container(name)
        assert restored["Image"] == manifest["image_id"]
        assert restored["HostConfig"]["PortBindings"]["8000/tcp"] == [
            {"HostIp": "127.0.0.1", "HostPort": str(configured_port)}]
        assert json.loads(state_file.read_text())["host_port"] == configured_port
        assert session_bytes() == original
        print("TOML deployment: real binding, idempotency, invalid pre-stop protection and rollback port PASS", flush=True)
    finally:
        config.write_text(content)
        os.environ.update(saved_binding)

    # 自定义绝对路径和带空格的 release 根目录，实际挂载后仍需可写和复用。
    invoke("stop.sh")
    docker("container", "rm", name)
    state_file = release / ".deploy-state.json"
    state_bytes = state_file.read_bytes()
    config.write_text(content.replace("../runtime/session.json", "../guarded-new/session.json"))
    release.chmod(0o700)
    try:
        invoke("run.sh", second, success=False)
        assert not (root / "guarded-new/session.json").exists()
        assert session_bytes() == original and state_file.read_bytes() == state_bytes
    finally:
        release.chmod(0o755)
        config.write_text(content)
    state_file.chmod(0o000)
    try:
        invoke("run.sh", second, success=False)
        assert session_bytes() == original
        assert docker("container", "inspect", name, check=False).returncode != 0
    finally:
        state_file.chmod(0o640)
    state_file.write_text("not-json")
    try:
        invoke("run.sh", second, success=False)
        assert session_bytes() == original
    finally:
        state_file.write_bytes(state_bytes)
    held_state = state_file.with_name("held-state.fixture")
    state_file.rename(held_state)
    state_file.symlink_to("missing-state.fixture")
    try:
        invoke("run.sh", second, success=False)
        assert session_bytes() == original
    finally:
        state_file.unlink()
        held_state.rename(state_file)
    print("Private release and unreadable, damaged, dangling state: no new session or path created PASS", flush=True)
    custom = root / "absolute release"
    custom.mkdir()
    custom_name = name + "-absolute"
    for script in ("run.sh", "stop.sh", "upgrade.sh"):
        shutil.copy2(release / script, custom / script)
    shutil.copytree(release / first, custom / first)
    custom_session = root / "custom data/session.json"
    custom_log = root / "custom log/events.log"
    (custom / "config.toml").write_text(
        content.replace('"../runtime/session.json"', json.dumps(str(custom_session)))
        .replace('"../log/adp_chat.log"', json.dumps(str(custom_log)))
        + f'\n[deployment]\nbind_ip="127.0.0.1"\nhost_port={initial_port}\n', encoding="utf-8")
    os.environ["ADP_CONTAINER_NAME"] = custom_name
    custom_binding = {key: os.environ.pop(key) for key in ("ADP_BIND_IP", "ADP_HOST_PORT") if key in os.environ}
    try:
        for entry in ("run.sh", "stop.sh", "run.sh"):
            result = subprocess.run(["sh", str(custom / entry), *([first] if entry == "run.sh" else [])],
                                    capture_output=True, text=True, timeout=160)
            print(result.stderr, end="", flush=True)
            assert result.returncode == 0, result.stdout
            if entry == "run.sh":
                assert custom_session.is_file() and custom_log.is_file()
                assert inspect_container(custom_name)["HostConfig"]["PortBindings"]["8000/tcp"] == [
                    {"HostIp": "127.0.0.1", "HostPort": str(initial_port)}]
                if "custom_bytes" in locals():
                    assert session_bytes(custom_session) == custom_bytes
                custom_bytes = session_bytes(custom_session)
        print("Absolute paths and release directory containing spaces PASS", flush=True)
    finally:
        docker("container", "rm", "--force", custom_name, check=False)
        os.environ["ADP_CONTAINER_NAME"] = name
        os.environ.update(custom_binding)


def main():
    """本机准备并清理唯一 Linux 测试环境，真实执行手动上传后的工作流。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--first", type=Path)
    parser.add_argument("--second", type=Path)
    parser.add_argument("--cli-image", default="docker:29.6.2-cli")
    parser.add_argument("--cli-binary", type=Path)
    parser.add_argument("--inside", type=Path)
    parser.add_argument("--first-version")
    parser.add_argument("--second-version")
    parser.add_argument("--unprivileged", action="store_true", help="用UID/GID1000且有Docker权限的真实Linux进程执行三个脚本")
    arguments = parser.parse_args()
    if arguments.inside:
        inside(arguments.inside, arguments.first_version, arguments.second_version)
        return
    if arguments.first is None or arguments.second is None:
        parser.error("需要 --first dist/v0.0.1 --second dist/v0.0.2")
    roots = [arguments.first.resolve(), arguments.second.resolve()]
    manifests = [json.loads((p / "manifest.json").read_text(encoding="utf-8")) for p in roots]
    assert all(m["platform"] == "linux/amd64" for m in manifests)
    identity = uuid4().hex
    server_root = "/tmp/adp-release-test-" + identity
    controller = "adp-release-controller-" + identity
    cli_container = "adp-cli-copy-" + identity
    service = "adp-release-service-" + identity
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        with socket.socket() as alternate:
            alternate.bind(("127.0.0.1", 0))
            alternate_port = alternate.getsockname()[1]
    created = []
    try:
        if arguments.cli_binary is None:
            docker("create", "--name", cli_container, arguments.cli_image)
            created.append(cli_container)
        docker("create", "--name", controller, "--user", "0:0", "--entrypoint", "python",
               "--mount", "type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock",
               "--volume", server_root + ":" + server_root,
               "--env", "ADP_CONTAINER_NAME=" + service, "--env", "ADP_BIND_IP=127.0.0.1",
               "--env", "ADP_HOST_PORT=" + str(port), "--env", "ADP_TEST_ID=" + identity,
               "--env", "ADP_TEST_ALT_PORT=" + str(alternate_port),
               manifests[0]["image_id"], "-c", "import time; time.sleep(1800)")
        created.append(controller)
        if arguments.cli_binary is not None:
            docker("cp", arguments.cli_binary.resolve(), controller + ":/usr/local/bin/docker")
        else:
            with tempfile.TemporaryDirectory(prefix="adp-linux-cli-") as temporary:
                cli_path = Path(temporary) / "docker"
                docker("cp", cli_container + ":/usr/local/bin/docker", cli_path)
                docker("cp", cli_path, controller + ":/usr/local/bin/docker")
        docker("cp", Path(__file__).resolve(), controller + ":/tmp/deploy_smoke.py")
        docker("start", controller)
        docker("exec", controller, "chmod", "0755", "/usr/local/bin/docker")
        docker("exec", controller, "mkdir", "-p", server_root + "/release")
        source_root = Path(__file__).resolve().parents[1]
        for name in ("run.sh", "stop.sh", "upgrade.sh"):
            docker("cp", source_root / "scripts" / name, controller + ":" + server_root + "/release/" + name)
        for version_root in roots:
            docker("cp", version_root, controller + ":" + server_root + "/release/")
        invocation = ["-u", "/tmp/deploy_smoke.py", "--inside", server_root,
                      "--first-version", manifests[0]["version"], "--second-version", manifests[1]["version"]]
        if arguments.unprivileged:
            prepare = ('import os,pathlib,sys;root=pathlib.Path(sys.argv[1]);'
                       'assert root.parent==pathlib.Path("/tmp") and root.name.startswith("adp-release-test-");'
                       '[(os.chown(p,1000,1000)) for p in [root,*root.rglob("*")]];'
                       'root.chmod(0o700);(root/"release").chmod(0o700)')
            docker("exec", controller, "python", "-c", prepare, server_root)
            launcher = ('import os,sys;os.environ["HOME"]=sys.argv[4];'
                        'os.setgroups([1000,os.stat("/var/run/docker.sock").st_gid]);'
                        'os.setgid(1000);os.setuid(1000);assert os.getuid()==os.getgid()==1000;'
                        'os.execv(sys.executable,[sys.executable,*sys.argv[1:]])')
            invocation = ["-c", launcher, *invocation]
        # 只有测试控制器接入本机 Docker；交付的产品脚本本身不挂载 socket。
        # 新增 TOML 端口切换及故障恢复各有一个75秒健康窗口，保留原阶段预算。
        subprocess.run(["docker", "exec", controller, "python", *invocation], check=True, timeout=510)
    finally:
        for name in (service + "-foreign", service + "-retired", service + "-absolute", service):
            result = docker("container", "inspect", name, check=False)
            if result.returncode == 0:
                docker("container", "rm", "--force", name)
        if controller in created:
            cleanup = (
                'import pathlib,shutil,sys;root=pathlib.Path(sys.argv[1]);'
                'assert root.name==sys.argv[2] and root.parent==pathlib.Path("/tmp");'
                '[(shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink()) for p in root.iterdir()]'
            )
            result = docker("exec", controller, "python", "-c", cleanup, server_root,
                            "adp-release-test-" + identity, check=False)
            if result.returncode:
                print("Fixture cleanup failed:", result.stderr, file=sys.stderr)
        for name in reversed(created):
            docker("container", "rm", "--force", name, check=False)
        fault_images = docker("image", "ls", "--filter", "label=io.nnit.adp.test=" + identity,
                              "--format", "{{.Repository}}:{{.Tag}}", check=False)
        for tag in fault_images.stdout.splitlines():
            if tag.startswith("adp_channel_api:v9") or tag == "adp_channel_api:v0.0.0":
                docker("image", "rm", tag)
        # 控制器只清理挂载内的自有数据；卸载后单独删除已经空的精确宿主目录。
        result = docker("run", "--rm", "--network", "none", "--user", "0:0", "--entrypoint", "python",
                        "--mount", "type=bind,src=/tmp,dst=/host-tmp", manifests[0]["image_id"], "-c",
                        'import os,sys;name=sys.argv[1];assert name=="adp-release-test-"+sys.argv[2];os.rmdir("/host-tmp/"+name)',
                        "adp-release-test-" + identity, identity, check=False)
        if result.returncode:
            raise RuntimeError("Linux fixture directory cleanup failed: " + result.stderr)
        assert docker("container", "ls", "-aq", "--filter", "name=" + identity).stdout.strip() == ""
    print("Deployment smoke passed; test containers, fixture files and fault images removed.")


if __name__ == "__main__":
    main()
