"""阶段E显式Docker Compose验收：ARM64正式镜像、Fake ADP、持久目录和恢复。"""

import argparse
import hashlib
import json
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from core.config import load_config
from core.models import ChatResult
from storage.messages import MessageStore, read_snapshot
from wecom.models import InboundMessage
from wecom.sender import SendOutcome
from wecom_dispatch_smoke import callback, event_report, free_port, json_http, wait_for
from wecom_local_setup import create_local_config


def command(arguments, *, check=True):
    """无shell插值，仅调用本脚本持有的Compose项目；失败保留明确输出。"""
    return subprocess.run(arguments, check=check, capture_output=True, text=True, encoding="utf-8", timeout=60)


def run(image):
    """全程只用Fake专属AppKey，清理本任务容器/网络/临时目录并保留脱敏报告。"""
    project_root = Path(__file__).resolve().parents[1]
    runtime = (project_root / "runtime").resolve()
    runtime.mkdir(exist_ok=True)
    project = "wecom-e-" + uuid4().hex[:12]
    platform = json.loads(command(["docker", "image", "inspect", image, "--format", "{{json .}}"] ).stdout)
    assert platform["Os"] == "linux" and platform["Architecture"] == "arm64"
    base = ["docker", "compose", "--project-directory", str(project_root), "-p", project,
            "-f", str(project_root / "compose.yaml"), "-f", str(project_root / "compose.local.yaml")]
    result = {"source": "docker_compose_fake_adp", "image_id": platform["Id"], "platform": "linux/arm64",
              "emulated_on_local_desktop": True, "real_wecom": False, "real_adp": False}
    with tempfile.TemporaryDirectory(prefix="docker-wecom-", dir=runtime) as temporary:
        root = Path(temporary).resolve()
        assert root.is_relative_to(runtime) and root.name.startswith("docker-wecom-")
        config_path = create_local_config(root / "config.toml", scope="wecom-local-docker-" + uuid4().hex[:8])
        config = load_config(config_path)
        for name in ("runtime", "logs"):
            (root / name).mkdir(exist_ok=True)
        port = free_port()
        endpoint = f"http://127.0.0.1:{port}"
        # 临时第三层只覆盖测试资源路径/端口/镜像；业务应用和dev模板保持同一入口。
        override = {"services": {
            "adp-chat": {"image": image, "volumes": [
                {"type": "bind", "source": str(config_path), "target": "/app/config.toml", "read_only": True},
                {"type": "bind", "source": str(root / "runtime"), "target": "/app/runtime"},
                {"type": "bind", "source": str(root / "logs"), "target": "/app/logs"}]},
            "fake-adp": {"image": image, "volumes": [
                {"type": "bind", "source": str(root / "runtime"), "target": "/app/runtime"}]}}}
        # JSON是合法YAML；端口需要Compose的显式替换标签，避免保留任何额外发布地址。
        override_file = root / "compose.override.yaml"
        text = json.dumps(override, ensure_ascii=False)
        # 先用JSON形成正确转义，再以独立YAML块补端口并保留同服务结构。
        override_file.write_text(text, encoding="utf-8")
        port_file = root / "compose.ports.yaml"
        port_file.write_text(f'services:\n  fake-adp:\n    ports: !override\n      - "127.0.0.1:{port}:8000"\n', encoding="utf-8")
        compose = base + ["-f", str(override_file), "-f", str(port_file)]

        def dc(*args, check=True):
            """明确限定当前唯一项目，不执行全局prune或删除其他容器。"""
            return command(compose + list(args), check=check)

        def fake(path="/_test/status", *, release=False):
            """Fake控制面只通过容器exec访问共享loopback，不对宿主发布。"""
            code = ("import json,urllib.request;u='http://127.0.0.1:8765" + path + "';"
                    "r=urllib.request.urlopen(urllib.request.Request(u,data=" + ("b''" if release else "None") + "),timeout=3);"
                    "print(r.read().decode('utf-8'))")
            return json.loads(dc("exec", "-T", "fake-adp", "python", "-c", code).stdout)

        def snapshots():
            """共享读取实际bind快照，Windows读取不阻止文件替换。"""
            return {record["msg_id"]: record for path in Path(config.messages_dir).glob("*.json")
                    if (record := read_snapshot(path))}

        def ready():
            """ARM64本地仿真预设30秒启动预算，状态断言不依赖健康日志猜测。"""
            wait_for(lambda: json_http(endpoint + "/healthz") == {"status": "ok"}, seconds=30)
            wait_for(lambda: dc("exec", "-T", "fake-adp", "python", "-c",
                "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8765/_test/status',timeout=2).close()",
                check=False).returncode == 0, seconds=30)

        try:
            model = json.loads(dc("config", "--format", "json").stdout)
            ports = model["services"]["fake-adp"]["ports"]
            assert len(ports) == 1 and ports[0]["host_ip"] == "127.0.0.1" and int(ports[0]["published"]) == port
            assert not model["services"]["adp-chat"].get("ports")
            dc("up", "-d", "--no-build")
            ready()
            container = dc("ps", "-q", "adp-chat").stdout.strip()
            inspection = json.loads(command(["docker", "inspect", container, "--format", "{{json .}}"] ).stdout)
            assert inspection["Image"] == platform["Id"]
            assert next(m for m in inspection["Mounts"] if m["Destination"] == "/app/config.toml")["RW"] is False
            expected = hashlib.sha256((project_root / "src/core/events.py").read_bytes()).hexdigest()
            probe = ("import os,platform,hashlib;from pathlib import Path;import core.events;from core.config import load_config;"
                     "c=load_config();assert os.getuid()==10001 and os.getgid()==10001;"
                     "assert platform.machine()=='aarch64';assert c.app_key=='fake-local-adp-only';"
                     "assert c.notification_mode=='simulated' and c.gateway_environment=='test';"
                     "assert os.environ['ADP_CONFIG_FILE']=='/app/config.toml';"
                     f"assert hashlib.sha256(Path(core.events.__file__).read_bytes()).hexdigest()=={expected!r};"
                     "print('ARM64 uid/gid10001/config/source: OK')")
            assert "OK" in dc("exec", "-T", "adp-chat", "python", "-c", probe).stdout
            with ThreadPoolExecutor(max_workers=100) as pool:
                durations = list(pool.map(lambda i: callback(config, endpoint, f"EMP{i}", str(i + 1)), range(100)))
            observed = wait_for(lambda: (s if len((s := fake())["calls"]) == 100 else None), seconds=30)
            assert observed["peak"] == 30
            held = next(record for record in snapshots().values()
                        if record["adp_state"] == "ADP_SEND_ARMED" and record["request_id"] not in observed["rejected"])
            callback(config, endpoint, held["user_id"], "1001")
            callback(config, endpoint, "EMP0", "1")
            assert snapshots()["1001"]["adp_state"] == "REJECTED_USER_BUSY"
            fake("/_test/release", release=True)
            finished = wait_for(lambda: (s if len(s := snapshots()) == 101 and
                all(r["send_state"] == "SEND_SIMULATED" for r in s.values()) else None), seconds=30)
            denied = set(observed["rejected"])
            assert len(fake()["calls"]) == 100
            for record in finished.values():
                if record["msg_id"] != "1001":
                    assert record["adp_state"] == ("ADP_REJECTED" if record["request_id"] in denied else "ADP_SUCCEEDED")
            result["concurrency"] = {"calls": 100, "peak": 30, "actual_rejections": len(denied),
                                     "max_ack_ms": round(max(durations), 1), "busy_and_duplicate": True}
            before = {key: r["record_version"] for key, r in finished.items()}
            dc("restart", "-t", "10", "adp-chat")
            ready()
            assert {key: r["record_version"] for key, r in snapshots().items()} == before
            assert len(fake()["calls"]) == 100
            result["graceful_restart_preserved_closed_records"] = True
            # 保持Fake网络Owner存活，只重设后续请求屏障，不把重启误作业务重试。
            assert fake("/_test/hold", release=True) == {"holding_future_requests": True}
            callback(config, endpoint, "YNND", "2001")
            wait_for(lambda: len(fake()["calls"]) == 101, seconds=30)
            first_id = snapshots()["2001"]["request_id"]
            dc("kill", "-s", "SIGKILL", "adp-chat")
            state = json.loads(command(["docker", "inspect", container, "--format", "{{json .State}}"] ).stdout)
            assert state["Status"] == "exited" and state["ExitCode"] == 137
            with MessageStore(config) as store:
                store.put_if_absent(InboundMessage(config.corp_id, config.agent_id, "SIYU", "2002", "本机恢复测试", 1))
                for msg, armed_send in (("2003", True), ("2004", False)):
                    record, _ = store.put_if_absent(InboundMessage(config.corp_id, config.agent_id, "EMP_SEND" + msg,
                                                                  msg, "通知边界注入夹具", 1))
                    armed = store.update(record["message_key"], expected_version=record["record_version"],
                                         adp_state="ADP_SEND_ARMED", request_id=str(uuid4()))
                    outcome = ChatResult("仅测试恢复通知", armed["conversation_id"], armed["request_id"])
                    saved = store.update(armed["message_key"], expected_version=armed["record_version"], adp_state="ADP_SUCCEEDED",
                        adp_result=asdict(outcome), notification=outcome.text, notification_kind="ADP_SUCCEEDED", send_state="PENDING")
                    if armed_send:
                        store.update(saved["message_key"], expected_version=saved["record_version"], send_state="SEND_ARMED",
                            send_result=asdict(SendOutcome("SEND_ARMED", "simulated", None, None, None, "armed")))
            dc("start", "adp-chat")
            ready()
            wait_for(lambda: snapshots()["2001"]["adp_state"] == "ADP_UNKNOWN" and
                snapshots()["2003"]["send_state"] == "SEND_UNKNOWN" and
                snapshots()["2004"]["send_state"] == "SEND_SIMULATED", seconds=30)
            wait_for(lambda: len(fake()["calls"]) == 102, seconds=30)
            callback(config, endpoint, "YNND", "2001")
            callback(config, endpoint, "YNND", "2005")
            wait_for(lambda: len(fake()["calls"]) == 103, seconds=30)
            fake("/_test/release", release=True)
            wait_for(lambda: snapshots()["2002"]["send_state"] == snapshots()["2005"]["send_state"] == "SEND_SIMULATED", seconds=30)
            calls = [json.loads(line) for line in (root / "runtime/fake-calls.jsonl").read_text(encoding="utf-8").splitlines()]
            assert len(calls) == 103 and sum(c["RequestId"] == first_id for c in calls) == 1
            assert [c["UserId"] for c in calls[-3:]] == ["YNND", "SIYU", "YNND"]
            assert calls[-3]["ConversationId"] == calls[-1]["ConversationId"] != calls[-2]["ConversationId"]
            result["recovery"] = {"actual_adp_container_sigkill": True, "old_request_calls": 1,
                "received_first_submission": True, "unknown_released": True,
                "send_armed_injected_state": "SEND_UNKNOWN", "pending_first_notification": "SEND_SIMULATED"}
            # 原单用户API非法输入应在任何ADP调用前拒绝；不拿FakeKey误调正式同步端点。
            try:
                urlopen(Request(endpoint + "/api/v1/chat", data=b'{"message":" "}',
                                headers={"Content-Type": "application/json"}), timeout=3)
            except HTTPError as exc:
                assert exc.code == 422 and json.load(exc)["error"]["code"] == "INVALID_REQUEST"
                exc.close()
            else:
                raise AssertionError("旧API输入契约退化")
            dc("stop", "-t", "10", "adp-chat")
            stopped = json.loads(command(["docker", "inspect", container, "--format", "{{json .State}}"] ).stdout)
            # Uvicorn0.48在完整lifespan关闭后重新抛出SIGTERM，init返回128+15=143。
            # 不把SIGKILL/137、OOM或缺少本次service.stopped当作正常退出。
            assert stopped["ExitCode"] == 143 and stopped["OOMKilled"] is False and not stopped["Error"]
            result["log_events"] = event_report(config, snapshots(), quota=30, crash=True,
                                           notification_crash=False, notifications=False)
            from core.logging import parse_log_line
            events = [document
                      for path in (root / "logs").glob(Path(config.log_file).name + "*")
                      for line in path.read_bytes().splitlines(keepends=True) if line.endswith(b"\\n")
                      for document in [parse_log_line(line.decode("utf-8"))]
                      if "event" in document]
            injected = snapshots()["2003"]["message_key"]
            assert not any(e.get("message_key") == injected and e["event"] in {"adp.started", "wecom.send.started", "wecom.send.simulated"}
                           for e in events)
            latest_start = max(e["timestamp"] for e in events if e["event"] == "service.started")
            assert any(e["event"] == "service.stopped" and e["timestamp"] >= latest_start for e in events)
            result["status"] = "passed"
            result["normal_exit_code"] = stopped["ExitCode"]
            result["sigterm_lifespan_completed"] = True
        except Exception:
            diagnostics = dc("logs", "--no-color", "--tail", "40", check=False)
            print(json.dumps({"failed_project": project,
                "states": [{key: record.get(key) for key in ("msg_id", "adp_state", "send_state", "request_id", "adp_error")}
                           for record in snapshots().values() if int(record["msg_id"]) >= 2000],
                "service_diagnostics": diagnostics.stdout + diagnostics.stderr}, ensure_ascii=False))
            raise
        finally:
            # down只删除唯一project的容器/网络，不删镜像、卷或用户数据；临时目录由其Owner回收。
            cleanup = dc("down", "--timeout", "10", check=False)
            if cleanup.returncode != 0:
                raise AssertionError("本任务Compose清理失败：" + cleanup.stderr)
            assert not command(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project]).stdout.strip()
            assert not command(["docker", "network", "ls", "-q", "--filter", "label=com.docker.compose.project=" + project]).stdout.strip()
    result["cleanup"] = "owned_containers_network_and_temporary_test_directory_removed"
    return result


def main(argv=None):
    """显式指定已构建镜像/报告，不构建、推送、部署或访问真实外部业务。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    result = run(args.image)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert json.loads(args.report.read_text(encoding="utf-8")) == result
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
