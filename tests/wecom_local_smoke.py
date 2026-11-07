"""本机真实 HTTP 接线验收：独立 Uvicorn 进程、加密客户端和 JSON 快照。"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from wecom_callback_client import initialize_test_keys


def serve(port: int) -> None:
    """复用正式 ASGI 应用，以父进程 stdin EOF/停止信号执行正常 lifespan 关闭。"""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config("api.app:app", host="127.0.0.1", port=port, workers=1))

    def stop_on_input():
        """测试父进程正常结束或异常关闭管道时均请求服务退出。"""
        sys.stdin.readline()
        server.should_exit = True

    threading.Thread(target=stop_on_input, daemon=True).start()
    server.run()


def main(argv=None) -> int:
    """只使用临时测试配置/密钥，验证重启去重与占用并可靠回收自有进程。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    if args.serve:
        serve(args.port)
        return 0
    with tempfile.TemporaryDirectory(prefix="wecom-http-") as directory:
        root = Path(directory)
        config = root / "config.toml"
        config.write_text('[adp]\napp_key="isolated-fake-adp-key"\nuser_id="legacy-test"\n'
                          '[logging]\nlog_enabled=false\n[wecom]\ncorp_id="wwf483023b734c570a"\n'
                          'agent_id=1000053\nadp_application="wecom-http-fixture"\n', encoding="utf-8")
        initialize_test_keys(config)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", args.port))
            port = probe.getsockname()[1]
        base_url = f"http://127.0.0.1:{port}"
        environment = {**os.environ, "ADP_CONFIG_FILE": str(config), "PYTHONDONTWRITEBYTECODE": "1"}
        script = Path(__file__).resolve()
        client = script.with_name("wecom_callback_client.py")
        processes = []
        durations = []

        def invoke(*arguments):
            """调用可复用的正式协议客户端，输出只包含脱敏测试结果。"""
            started = time.monotonic()
            completed = subprocess.run([sys.executable, "-B", "-X", "utf8", str(client), "--config", str(config),
                                        "--url", base_url, *arguments], capture_output=True, text=True,
                                       encoding="utf-8", timeout=10)
            durations.append((time.monotonic() - started) * 1000)
            if completed.returncode:
                raise RuntimeError("正式回调客户端失败，未把此结果当作成功接收")
            return json.loads(completed.stdout)

        for cycle in (1, 2):
            with (root / f"server-{cycle}.log").open("w", encoding="utf-8") as log:
                process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", str(script), "--serve", "--port", str(port)],
                                           stdin=subprocess.PIPE, stdout=log, stderr=log, env=environment)
                processes.append(process.pid)
                try:
                    deadline = time.monotonic() + 10
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError("独立 Uvicorn 启动失败")
                        try:
                            with urlopen(base_url + "/healthz", timeout=0.5) as response:
                                if response.status == 200:
                                    break
                        except (OSError, URLError):
                            if time.monotonic() >= deadline:
                                raise RuntimeError("独立 Uvicorn 未就绪") from None
                            time.sleep(0.05)
                    invoke("--verify-url")
                    invoke("--user-id", "YNND", "--msg-id", "1")
                    if cycle == 1:
                        invoke("--user-id", "YNND", "--msg-id", "2")
                        invoke("--user-id", "SIYU", "--msg-id", "3")
                    snapshots = [json.loads(p.read_text(encoding="utf-8")) for p in (root / "runtime/messages").glob("*.json")]
                    assert len(snapshots) == 3
                    assert sorted(r["adp_state"] for r in snapshots) == ["RECEIVED", "RECEIVED", "REJECTED_USER_BUSY"]
                    assert {r["user_id"] for r in snapshots} == {"YNND", "SIYU"}
                finally:
                    try:
                        process.stdin.write(b"stop\n")
                        process.stdin.flush()
                    except (OSError, BrokenPipeError):
                        pass
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        if os.name == "nt":
                            # venv 启动器可能有 Python 子进程；复用现有测试的自有进程树回收方式。
                            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                                           check=True, capture_output=True, timeout=5)
                        else:
                            process.terminate()
                        process.wait(timeout=5)
                        raise RuntimeError("服务未能正常退出，已回收本任务进程") from None
                    finally:
                        process.stdin.close()
                assert process.returncode == 0
                assert "Application shutdown complete" in (root / f"server-{cycle}.log").read_text(encoding="utf-8")
        with socket.socket() as probe:
            probe.settimeout(0.2)
            assert probe.connect_ex(("127.0.0.1", port)) != 0, "测试监听未释放"
        report = {"status": "passed", "environment": "test", "real_wecom": False, "adp_called": False,
                  "server_processes": processes, "restart_cycles": 2, "snapshots": 3,
                  "max_client_duration_ms": round(max(durations), 1), "cleanup": "processes_exited_and_port_released"}
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            assert json.loads(args.report.read_text(encoding="utf-8")) == report
        print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
