"""显式运行镜像验收：python tests/docker_smoke.py --image adp-chat:src-layout-check。"""

import argparse
import json
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from uuid import uuid4


def docker(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    """调用本机 Docker 并保留失败证据，不读取用户配置或操作其他容器。"""
    return subprocess.run(
        ["docker", *arguments], check=check, capture_output=True, text=True,
        encoding="utf-8", timeout=45,
    )


def wait_ready(container: str) -> str:
    """等待实际 HTTP 存活，提前退出的容器立即提供其测试日志。"""
    address = docker("port", container, "8000/tcp").stdout.strip()
    endpoint = "http://" + address
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if docker("inspect", "--format", "{{.State.Running}}", container).stdout.strip() != "true":
            raise AssertionError(docker("logs", container).stdout)
        try:
            with urllib.request.urlopen(endpoint + "/healthz", timeout=1) as response:
                if json.load(response) == {"status": "ok"}:
                    return endpoint
        except (OSError, urllib.error.URLError):
            time.sleep(0.1)
    raise AssertionError("容器未在验收预算内通过 /healthz")


def main() -> None:
    """验证正式镜像安装、非 root 启动、只读配置、可写数据及容器重建恢复。"""
    parser = argparse.ArgumentParser(description="隔离 Docker 镜像运行验收，不访问真实 ADP")
    parser.add_argument("--image", required=True)
    image = parser.parse_args().image
    inspection = (
        "import importlib.metadata as m,os;from pathlib import Path;import api.app;"
        "assert os.getuid()==10001 and os.getgid()==10001;"
        "assert 'site-packages' in api.app.__file__;"
        "assert not any(Path(p).exists() for p in "
        "('/app/config.toml','/app/src/config.toml','/app/src/logs','/app/src/runtime','/app/adp_chat'));"
        "assert not any(str(f).startswith('src/') or str(f).endswith('config.toml') "
        "for f in m.files('adp-chat'));print('installed image and data exclusion: OK')"
    )
    print(docker("run", "--rm", "--entrypoint", "python", image, "-c", inspection).stdout.strip())
    containers = []
    label = "adp-chat.smoke=" + uuid4().hex
    try:
        with tempfile.TemporaryDirectory(prefix="adp-container-smoke-") as directory:
            root = Path(directory)
            for name in ("runtime", "logs"):
                (root / name).mkdir()
            configuration = (
                '[adp]\napp_key="docker-fixture-key"\nuser_id="docker-fixture-user"\n'
                '[session]\nsession_file="runtime/session.json"\n'
                '[logging]\nlog_file="logs/adp_chat.log"\n'
            )
            (root / "config.toml").write_text(configuration, encoding="utf-8")
            previous_state = None
            for iteration in range(2):
                arguments = ["run", "-d", "--label", label, "-p", "127.0.0.1::8000"]
                for name in ("config.toml", "runtime", "logs"):
                    mount = f"type=bind,src={root / name},dst=/app/{name}"
                    arguments.extend(("--mount", mount + (",readonly" if name == "config.toml" else "")))
                container = docker(*arguments, image).stdout.strip()
                containers.append(container)
                endpoint = wait_ready(container)
                request = urllib.request.Request(
                    endpoint + "/api/v1/chat", data=b'{"message":" "}',
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                try:
                    urllib.request.urlopen(request, timeout=3)
                except urllib.error.HTTPError as error:
                    assert error.code == 422
                    assert json.load(error)["error"]["code"] == "INVALID_REQUEST"
                else:
                    raise AssertionError("非法输入未在 ADP 调用之前被拒绝")
                state = (root / "runtime/session.json").read_bytes()
                if previous_state is not None:
                    assert state == previous_state, "容器重建改变了持久会话"
                previous_state = state
                probe = '''import errno,os
from pathlib import Path
from core.config import load_config
from core.logging import diagnostic_logger
assert os.getuid()==10001 and os.getgid()==10001
config=load_config()
assert Path(config.session_file)==Path('/app/runtime/session.json')
assert Path(config.log_file)==Path('/app/logs/adp_chat.log')
try:
    Path('/app/config.toml').open('a')
except OSError as error:
    assert error.errno==errno.EROFS
else:
    raise AssertionError('配置不是只读挂载')
with diagnostic_logger(config) as logger:
    logger.info('docker smoke fixture')
print('UID/GID, read-only config and writable root data: OK')
'''
                print(docker("exec", container, "python", "-c", probe).stdout.strip())
                assert (root / "logs/adp_chat.log").is_file()
                assert (root / "config.toml").read_text(encoding="utf-8") == configuration
                docker("stop", "--time", "5", container)
                docker("rm", container)
                containers.remove(container)
                print(f"container {iteration + 1}: health=200, invalid input=422, session preserved")
            transport_fixture = '''import io,json
from unittest.mock import patch
import uvicorn
def respond(request, **kwargs):
    """仅替换远端 SSE 传输，保留正式请求、解析、会话和 HTTP 链路。"""
    payload=json.loads(request.data)
    data={"Type":"response.completed","Response":{"Status":"success","ConversationId":payload["ConversationId"],"Messages":[{"Type":"reply","Contents":[{"Type":"text","Text":"fixture reply"}]}]}}
    stream=io.BytesIO(('event: response.completed\\ndata: '+json.dumps(data)+'\\n\\n').encode())
    stream.headers={"Content-Type":"text/event-stream"}
    return stream
with patch('adp.sse.open_request',side_effect=respond):
    uvicorn.run('api.app:app',host='0.0.0.0',port=8000,workers=1)
'''
            container = docker(*arguments, image, "python", "-c", transport_fixture).stdout.strip()
            containers.append(container)
            endpoint = wait_ready(container)
            request = urllib.request.Request(
                endpoint + "/api/v1/chat", data=b'{"message":"fixture"}',
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                body = json.load(response)
                assert response.status == 200 and body["text"] == "fixture reply"
                assert body["conversation_id"] == json.loads(previous_state)["conversation_id"]
                assert body["request_id"]
            assert (root / "runtime/session.json").read_bytes() == previous_state
            docker("stop", "--time", "5", container)
            docker("rm", container)
            containers.remove(container)
            print("real HTTP -> service -> persistent session -> SSE fixture -> HTTP 200: OK")
    finally:
        for container in containers:
            docker("rm", "--force", container)
        assert not docker("ps", "-aq", "--filter", "label=" + label).stdout.strip(), "测试容器未释放"
    print("Docker smoke passed; fixture data and containers removed; no ADP request sent.")


if __name__ == "__main__":
    main()
