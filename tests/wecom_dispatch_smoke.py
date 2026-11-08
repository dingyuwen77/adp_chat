"""C本机全链路：独立正式Uvicorn与Fake ADP进程，100并发及强制退出恢复。"""

import argparse
import base64
import json
import os
import secrets
import statistics
import socket
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from xml.sax.saxutils import escape

from core.config import load_config
from storage.messages import MessageStore, read_snapshot
from wecom.crypto import WeComCrypto
from wecom.models import InboundMessage
from wecom_fake_adp import create_fake_app


def free_port():
    """仅绑定本机临时端口；启动冲突应失败而不连接未知服务。"""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def json_http(url, data=None):
    """测试控制面有界读取，控制路由只存在于独立Fake进程。"""
    with urlopen(Request(url, data=data), timeout=5) as response:
        return json.load(response)


def wait_for(predicate, *, seconds=12):
    """等待明确状态，保留超时失败并进入自有资源回收。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            value = predicate()
            if value:
                return value
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    raise AssertionError("未在限定时间观察到预期本机测试状态")


def callback(config, base, user, msg_id, text="本机确定性测试"):
    """生产Crypto生成协议输入，正式HTTP仍校验全部签名/身份/存储。"""
    crypto = WeComCrypto(config.callback_token, config.encoding_aes_key, config.corp_id)
    timestamp, nonce = str(int(time.time())), secrets.token_hex(8)
    inner = (f"<xml><ToUserName>{config.corp_id}</ToUserName><AgentID>{config.agent_id}</AgentID>"
             f"<FromUserName>{escape(user)}</FromUserName><CreateTime>{timestamp}</CreateTime>"
             f"<MsgType>text</MsgType><MsgId>{msg_id}</MsgId><Content>{escape(text)}</Content></xml>").encode()
    encrypted = crypto.encrypt(inner)
    params = {"timestamp": timestamp, "nonce": nonce, "msg_signature": crypto.signature(encrypted, timestamp, nonce)}
    body = f"<xml><ToUserName>{config.corp_id}</ToUserName><AgentID>{config.agent_id}</AgentID><Encrypt>{encrypted}</Encrypt></xml>".encode()
    started = time.monotonic()
    with urlopen(Request(base + config.callback_path + "?" + urlencode(params), data=body), timeout=5) as response:
        assert response.status == 200 and response.read() == b""
    return (time.monotonic() - started) * 1000


def stop(process, *, force=False):
    """只处理本脚本Popen持有的自有进程树，强杀用于明确崩溃注入。"""
    if process.poll() is not None:
        return
    if force:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], check=True, capture_output=True, timeout=5)
        else:
            process.kill()
    else:
        process.stdin.write(b"stop\n")
        process.stdin.flush()
    try:
        process.wait(timeout=12)
    except subprocess.TimeoutExpired:
        stop(process, force=True)
        raise AssertionError("未正常退出，已回收本脚本进程") from None
    finally:
        if process.stdin is not None and not process.stdin.closed:
            process.stdin.close()
    if not force:
        assert process.returncode == 0


def event_report(config, records, *, quota, crash, notification_crash, notifications):
    """解析统一文本日志中的 gateway_event，核对消息快照关联。"""
    events, incomplete = [], 0
    for path in sorted(Path(config.log_file).parent.glob(Path(config.log_file).name + "*")):
        for line in path.read_bytes().splitlines(keepends=True):
            if not line.endswith(b"\\n"):
                incomplete += 1
                continue
            from core.logging import parse_log_line
            document = parse_log_line(line.decode("utf-8"))
            if "event" not in document:
                continue
            event = {key: value for key, value in document.items() if key != "message" and not key.startswith("attr_")}
            event["attributes"] = {key[5:]: value for key, value in document.items() if key.startswith("attr_")}
            event["id"] = event.pop("event_id")
            event["schema_version"] = "adp_chat.log.v1"
            assert event["schema_version"] == "adp_chat.log.v1" and event["environment"] == "test"
            events.append(event)
    assert len({e["id"] for e in events}) == len(events)
    assert {"service.started", "service.stopped", "adp.submit_armed", "adp.started", "adp.completed",
            "message.session.released", "reply.persisted"} <= {e["event"] for e in events}
    assert not any(e["event"] == "wecom.send.accepted" for e in events)
    for record in records.values():
        linked = [event for event in events if event["message_key"] == record["message_key"]]
        assert linked and all(e["conversation_id"] == record["conversation_id"] for e in linked)
        if record["request_id"] is not None:
            assert any(e["request_id"] == record["request_id"] for e in linked)
        if notifications:
            assert any(e["event"] == ("wecom.send.unknown" if notification_crash and record["msg_id"] == "1"
                                      else "wecom.send.simulated") for e in linked)
    names = {e["event"] for e in events}
    if quota:
        assert "adp.rejected" in names and any(e.get("error_code") == "460031" for e in events)
    if crash:
        assert {"adp.unknown", "message.recovered"} <= names
    if not crash and not notification_crash:
        assert {"wecom.message.busy_rejected", "wecom.message.duplicate", "wecom.message.persisted"} <= names
    raw = json.dumps(events, ensure_ascii=False)
    for private in (config.app_key, config.callback_token, config.encoding_aes_key, "本机确定性测试", '"YNND"', '"SIYU"'):
        assert private not in raw
    return {"parsed_events": len(events), "incomplete_tails_isolated": incomplete,
            "message_links_checked": len(records), "credentials_and_bodies_absent": True}


def serve(args):
    """子进程复用正式应用或独立Fake，stdin触发正常退出。"""
    import uvicorn
    from unittest.mock import patch

    target = create_fake_app(quota=args.quota, hold=True, calls_file=args.calls_file) if args.serve == "fake" else "api.app:app"
    server = uvicorn.Server(uvicorn.Config(target, host="127.0.0.1", port=args.port, workers=1,
                                          log_level="warning", timeout_graceful_shutdown=3))

    def on_stop():
        """父进程退出或管道关闭时亦释放测试服务。"""
        sys.stdin.readline()
        server.should_exit = True

    threading.Thread(target=on_stop, daemon=True).start()
    if args.hold_notifications:
        async def held_simulation(_sender, record):
            """只在本测试子进程阻塞模拟出口，记录已进入发送边界，不是平台接受。"""
            with args.simulation_calls_file.open("a", encoding="utf-8") as output:
                output.write(json.dumps({"message_key": record["message_key"], "source": "simulated"}) + "\n")
                output.flush()
                os.fsync(output.fileno())
            import asyncio

            await asyncio.Event().wait()

        with patch("wecom.sender.SimulatedSender.send", held_simulation):
            server.run()
    else:
        server.run()


def run_case(root, *, quota=0, crash=False, notifications=False, notification_crash=False, users=100):
    """真实TCP/SSE与正式worker验证一套入站状态；测试数据用后清理。"""
    root.mkdir()
    fake_port, gateway_port = free_port(), free_port()
    fake, base = f"http://127.0.0.1:{fake_port}", f"http://127.0.0.1:{gateway_port}"
    key = base64.b64encode(os.urandom(32)).decode()[:-1]
    config_file = root / "config.toml"
    config_file.write_text('[adp]\napp_key="fake-local-adp-only"\nuser_id="legacy-test"\n'
        '[logging]\nlog_enabled=true\nlog_question=false\nlog_answer_chars=0\n[wecom]\ncorp_id="wwf483023b734c570a"\nagent_id=1000053\n'
        f'adp_application="wecom-c-smoke"\ngateway_enabled=true\ncallback_token="{secrets.token_hex(12)}"\n'
        f'encoding_aes_key="{key}"\nadp_url="{fake}/adp/v2/chat"\n'
        f'notification_mode="{"simulated" if notifications else "disabled"}"\ngateway_environment="test"\n', encoding="utf-8")
    config = load_config(config_file)
    processes, handles = [], []
    environment = {**os.environ, "ADP_CONFIG_FILE": str(config_file), "PYTHONDONTWRITEBYTECODE": "1"}

    def launch(kind, port, *, hold_notifications=False):
        """保存自有Popen与日志句柄，只有这些资源可被回收。"""
        log = (root / f"{kind}-{len(processes)}.log").open("w", encoding="utf-8")
        handles.append(log)
        command = [sys.executable, "-B", "-X", "utf8", str(Path(__file__).resolve()),
            "--serve", kind, "--port", str(port), "--quota", str(quota), "--calls-file", str(root / "calls.jsonl"),
            "--simulation-calls-file", str(root / "simulation-calls.jsonl")]
        if hold_notifications:
            command.append("--hold-notifications")
        process = subprocess.Popen(command,
            stdin=subprocess.PIPE, stdout=log, stderr=log, env=environment)
        processes.append(process)
        wait_for(lambda: process.poll() is None and json_http((fake if kind == "fake" else base) + ("/_test/status" if kind == "fake" else "/healthz")))
        return process

    def snapshots():
        """原子JSON快照直接重读，状态不靠日志猜测。"""
        return {r["msg_id"]: r for p in Path(config.messages_dir).glob("*.json")
                if (r := read_snapshot(p))}

    try:
        launch("fake", fake_port)
        if crash or notification_crash:
            # 模拟上一进程只完成可靠接收，尚未武装；启动应执行首次请求。
            with MessageStore(config) as store:
                store.put_if_absent(InboundMessage(config.corp_id, config.agent_id, "YNND", "1", "本机测试", 1))
        gateway = launch("gateway", gateway_port, hold_notifications=notification_crash)
        if not crash and not notification_crash:
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=users) as pool:
                durations = list(pool.map(lambda i: callback(config, base, f"EMP{i}", str(i + 1)), range(users)))
            callback_elapsed = time.monotonic() - started
            observed = wait_for(lambda: (s if len((s := json_http(fake + "/_test/status"))["calls"]) == users else None))
            assert observed["peak"] == min(quota or users, users)
            # 被入口拒绝的快照短暂也可能仍ARMED；只能从平台实际获准集合选择BUSY对象。
            held = next(r for r in snapshots().values() if r["adp_state"] == "ADP_SEND_ARMED"
                        and r["request_id"] not in observed["rejected"])
            callback(config, base, held["user_id"], "1001")
            callback(config, base, "EMP0", "1")
            assert snapshots()["1001"]["adp_state"] == "REJECTED_USER_BUSY"
            assert len(json_http(fake + "/_test/status")["calls"]) == users
            json_http(fake + "/_test/release", b"")
            saved = wait_for(lambda: (s if (s := snapshots()) and all(r["adp_state"] not in {"RECEIVED", "ADP_SEND_ARMED"} for r in s.values()) else None))
            if notifications:
                saved = wait_for(lambda: (s if (s := snapshots()) and all(r["send_state"] == "SEND_SIMULATED" for r in s.values()) else None))
            denied = set(observed["rejected"])
            for r in saved.values():
                if r["msg_id"] == "1001":
                    continue
                assert r["adp_state"] == ("ADP_REJECTED" if r["request_id"] in denied else "ADP_SUCCEEDED")
                assert r["send_state"] == ("SEND_SIMULATED" if notifications else "PENDING")
                if notifications:
                    assert r["send_result"]["source"] == "simulated"
            ordered = sorted(durations)
            result = {"case": f"quota-{quota}", "calls": users, "peak": observed["peak"], "actual_rejections": len(denied),
                      "max_ack_ms": round(max(durations), 1), "ack_p50_ms": round(statistics.median(ordered), 1),
                      "ack_p95_ms": round(ordered[(len(ordered) * 95 + 99) // 100 - 1], 1),
                      "ack_p99_ms": round(ordered[(len(ordered) * 99 + 99) // 100 - 1], 1),
                      "callback_batch_seconds": round(callback_elapsed, 3),
                      "callback_ack_per_second": round(users / callback_elapsed, 1)}
        else:
            wait_for(lambda: len(json_http(fake + "/_test/status")["calls"]) == 1)
            first_id = snapshots()["1"]["request_id"]
            if notification_crash:
                json_http(fake + "/_test/release", b"")
                wait_for(lambda: (root / "simulation-calls.jsonl").exists())
                wait_for(lambda: snapshots()["1"]["send_state"] == "SEND_ARMED")
                assert snapshots()["1"]["adp_state"] == "ADP_SUCCEEDED"
            else:
                assert snapshots()["1"]["adp_state"] == "ADP_SEND_ARMED"
            stop(gateway, force=True)
            with MessageStore(config) as store:
                store.put_if_absent(InboundMessage(config.corp_id, config.agent_id, "SIYU", "2", "本机测试", 1))
            gateway = launch("gateway", gateway_port)
            wait_for(lambda: snapshots()["1"]["send_state"] == "SEND_UNKNOWN" if notification_crash
                     else snapshots()["1"]["adp_state"] == "ADP_UNKNOWN")
            wait_for(lambda: len(json_http(fake + "/_test/status")["calls"]) == 2)
            callback(config, base, "YNND", "1")
            callback(config, base, "YNND", "3")
            wait_for(lambda: len(json_http(fake + "/_test/status")["calls"]) == 3)
            json_http(fake + "/_test/release", b"")
            wait_for(lambda: snapshots()["2"]["adp_state"] == snapshots()["3"]["adp_state"] == "ADP_SUCCEEDED")
            if notifications:
                wait_for(lambda: all(r["send_state"] == ("SEND_UNKNOWN" if notification_crash and r["msg_id"] == "1" else "SEND_SIMULATED")
                                     for r in snapshots().values()))
            calls = [json.loads(line) for line in (root / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
            assert sum(c["RequestId"] == first_id for c in calls) == 1
            assert [c["UserId"] for c in calls] == ["YNND", "SIYU", "YNND"]
            assert calls[0]["ConversationId"] == calls[2]["ConversationId"] != calls[1]["ConversationId"]
            result = {"case": "forced-process-crash", "calls": 3, "old_request_calls": 1,
                      "received_resumed": True, "unknown_released": True}
            if notification_crash:
                attempts = [json.loads(line) for line in (root / "simulation-calls.jsonl").read_text(encoding="utf-8").splitlines()]
                assert len(attempts) == 1 and attempts[0]["source"] == "simulated"
                assert snapshots()["1"]["send_state"] == "SEND_UNKNOWN"
                result = {"case": "forced-notification-crash", "calls": 3, "old_request_calls": 1,
                          "old_notification_attempts": 1, "recovered_send_state": "SEND_UNKNOWN"}
        result["notifications"] = "simulated" if notifications else "disabled"
        result["processes"] = [p.pid for p in processes]
        stop(gateway)
        result["jsonl"] = event_report(config, snapshots(), quota=quota, crash=crash,
                                      notification_crash=notification_crash, notifications=notifications)
        return result
    except Exception:
        # 失败也保留安全的状态诊断；临时目录随后清理，不能吞掉根因只留下超时。
        print(json.dumps({"failed_case": str(root.name), "states": [
            {k: r.get(k) for k in ("msg_id", "adp_state", "send_state", "adp_error", "request_id")}
            for r in snapshots().values() if r["adp_state"] in {"ADP_SEND_ARMED", "RECEIVED", "ADP_UNKNOWN"}],
            "fake_status": json_http(fake + "/_test/status"), "service_diagnostics": [
                line for path in root.rglob("*.log") for line in path.read_text(encoding="utf-8").splitlines()
                if "failed" in line or "unknown" in line]}, ensure_ascii=False))
        raise
    finally:
        for process in reversed(processes):
            stop(process)
        for handle in handles:
            handle.close()
        for port in (gateway_port, fake_port):
            with socket.socket() as sock:
                sock.settimeout(0.2)
                assert sock.connect_ex(("127.0.0.1", port)) != 0


def main(argv=None):
    """一次执行全部确定性场景，可保存脱敏摘要，不调用真实ADP/企业微信。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", choices=["fake", "gateway"], help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--quota", type=int, default=0)
    parser.add_argument("--calls-file", type=Path)
    parser.add_argument("--simulation-calls-file", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--hold-notifications", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--simulate-notifications", action="store_true", help="使用D正式Simulator，并验证发送武装后强杀恢复")
    parser.add_argument("--users", type=int, choices=(50, 100), default=100, help="不同模拟用户数；不访问真实ADP")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.serve:
        serve(args)
        return 0
    with tempfile.TemporaryDirectory(prefix="wecom-dispatch-") as directory:
        root = Path(directory)
        cases = [run_case(root / "unlimited", notifications=args.simulate_notifications, users=args.users),
                 run_case(root / "quota", quota=30, notifications=args.simulate_notifications, users=args.users),
                 run_case(root / "crash", crash=True, notifications=args.simulate_notifications)]
        if args.simulate_notifications:
            cases.append(run_case(root / "notification-crash", notifications=True, notification_crash=True))
    result = {"status": "passed", "source": "fake_adp", "real_wecom": False, "cases": cases,
              "cleanup": "owned_processes_exited_ports_released_temporary_data_removed"}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        assert json.loads(args.report.read_text(encoding="utf-8")) == result
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
