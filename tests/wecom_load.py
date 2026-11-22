"""本机真实 HTTP/SSE 负载测试。生成独立配置，只连接 loopback Fake ADP 和模拟通知。"""

import argparse
import asyncio
import base64
import json
import os
import secrets
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import AsyncExitStack, ExitStack, closing
from pathlib import Path

import httpx

from core.config import load_config
from core.logging import log_writer_metrics, parse_log_line
from storage.sqlite_store import read_messages
from wecom_dispatch_smoke import free_port, json_http, signed_callback, stop, wait_for
from wecom_fake_adp import create_fake_app


QUESTION = "合成问题🙂\n排查" * 35 + ' | record=输入字面量 “问题末尾”'
ANSWER = "合成回答🙂\n完整内容" * 140 + "“回答末尾”"
SLOW_PREFIX = "LOAD_SLOW_"
ACK_TIMEOUT_SECONDS = 5


def resource_sample():
    """在 Linux 记录单个网关进程的内存、句柄、CPU 和块 I/O。"""
    cpu = os.times()
    result = {"cpu_seconds": round(cpu.user + cpu.system, 3)}
    if sys.platform.startswith("linux"):
        import resource

        usage = resource.getrusage(resource.RUSAGE_SELF)
        result.update(rss_peak_bytes=usage.ru_maxrss * 1024, open_fds=len(os.listdir("/proc/self/fd")),
                      io_input_blocks=usage.ru_inblock, io_output_blocks=usage.ru_oublock)
    return result


def serve(kind, port):
    """独立子进程启动正式应用或 Fake，测试指标只存在于该临时网关实例。"""
    import uvicorn
    from api.app import create_app

    if kind == "fake":
        app = create_fake_app(hold=True, answer_text=ANSWER, hold_user_prefix=SLOW_PREFIX)
    else:
        app = create_app()
        sampling = {"loop_lag_peak_ms": 0.0, "fd_peak": 0}

        async def sample():
            """按固定间隔观察事件循环延时和连接数变化。"""
            while True:
                start = time.perf_counter()
                await asyncio.sleep(0.05)
                sampling["loop_lag_peak_ms"] = max(sampling["loop_lag_peak_ms"],
                                                   (time.perf_counter() - start - 0.05) * 1000)
                if sys.platform.startswith("linux"):
                    sampling["fd_peak"] = max(sampling["fd_peak"], len(os.listdir("/proc/self/fd")))

        original = app.router.lifespan_context

        async def lifespan(app):
            """在应用生命周期内管理测试采样任务。"""
            async with original(app):
                sampler = asyncio.create_task(sample())
                try:
                    yield
                finally:
                    sampler.cancel()
                    await asyncio.gather(sampler, return_exceptions=True)

        from contextlib import asynccontextmanager
        app.router.lifespan_context = asynccontextmanager(lifespan)

        @app.get("/_test/metrics")
        async def metrics(checkpoint: bool = False):
            """查询业务本身的队列计数，以及采样到的实际进程资源。"""
            gateway = app.state.wecom_gateway
            return {"sqlite_version": sqlite3.sqlite_version, **sampling, **resource_sample(),
                    "store": gateway.store.metrics(), "logging": log_writer_metrics(gateway.logger),
                    "checkpoint": await gateway.store.checkpoint() if checkpoint else None,
                    "adp_tasks": len(gateway.worker.tasks), "send_tasks": len(gateway.worker.send_tasks),
                    "dispatcher_failure": gateway.worker.failure}

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, workers=1,
                                           access_log=False, log_level="warning",
                                           timeout_graceful_shutdown=5))

    def parent_shutdown():
        """父进程关闭 stdin 后退出服务，正常结束时刷出日志及 SQLite 状态。"""
        sys.stdin.readline()
        server.should_exit = True

    threading.Thread(target=parent_shutdown, daemon=True).start()
    server.run()


async def submit_batch(config, gateway, messages):
    """同时建立真实回调连接，分组的客户端连接池只用于消除压测端池扫描瓶颈。"""
    async with AsyncExitStack() as stack:
        group_size = 50
        clients = [
            await stack.enter_async_context(httpx.AsyncClient(
                base_url=gateway, limits=httpx.Limits(max_connections=group_size), trust_env=False,
                timeout=ACK_TIMEOUT_SECONDS,
            ))
            for _ in range((len(messages) + group_size - 1) // group_size)
        ]
        prepared = [signed_callback(config, user, msgid, QUESTION) for user, msgid in messages]
        started = time.perf_counter()

        async def send(index, params, body):
            """记录每条请求收到正式 HTTP 响应的耗时和状态。"""
            begin = time.perf_counter()
            reply = await clients[index // group_size].post(config.callback_path, params=params, content=body)
            return reply.status_code, reply.content, (time.perf_counter() - begin) * 1000

        replies = await asyncio.gather(*(send(i, params, body) for i, (params, body) in enumerate(prepared)))
        return replies, time.perf_counter() - started


def count_states(config, column, state):
    """仅检查确定状态数量，短查询后立即释放 WAL 读快照。"""
    path = config.gateway_database_path
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as reader:
        return reader.execute(f"SELECT count(*) FROM gateway_messages WHERE {column} = ?", (state,)).fetchone()[0]


def run_batch(config, gateway, fake, *, size, number, previous, hold_seconds, mixed=False):
    """在同一套部署上独立验证 ACK、真实 SSE 重叠、发送结果和资源归还。"""
    json_http(fake + "/_test/hold", b"")
    held = size // 2 if mixed else size
    messages = [(f"{SLOW_PREFIX if i < held else 'LOAD_FAST_'}{i}", str(number * 10_000 + i + 1))
                for i in range(size)]
    replies, elapsed = asyncio.run(submit_batch(config, gateway, messages))
    assert all(status == 200 and not body for status, body, _ in replies)
    durations = sorted(t for _, _, t in replies)
    assert durations[-1] <= 1000 * ACK_TIMEOUT_SECONDS, (
        f"users={size} round={number} mixed={mixed} max_ack_ms={durations[-1]:.1f}"
    )
    previous_calls, previous_sent = previous
    in_flight = wait_for(lambda: (state if len((state := json_http(fake + "/_test/status"))["calls"])
                                  == previous_calls + size and state["active"] == held else None), seconds=40)
    assert not in_flight["rejected"]
    assert count_states(config, "adp_state", "ADP_SEND_ARMED") >= held
    assert count_states(config, "send_state", "SEND_SIMULATED") >= previous_sent
    if mixed:
        wait_for(lambda: count_states(config, "send_state", "SEND_SIMULATED") == previous_sent + size - held)
        assert json_http(fake + "/_test/status")["active"] == held
    busy_count = 0
    if size == 1000 and not mixed:
        busy_count = 100
        overload = ("LOAD_OTHER", str(number * 10_000 + 9000))
        competing = [(messages[0][0], str(number * 10_000 + 2000 + i)) for i in range(busy_count)]
        pressure, _ = asyncio.run(submit_batch(config, gateway, [overload, *competing, *([messages[0]] * 100)]))
        assert pressure[0][0] == 503
        assert all(status == 200 and not body for status, body, _ in pressure[1:])
        assert count_states(config, "adp_state", "REJECTED_USER_BUSY") >= busy_count
        assert len(json_http(fake + "/_test/status")["calls"]) == previous_calls + size
    time.sleep(hold_seconds)
    assert json_http(fake + "/_test/status")["active"] == held
    during = json_http(gateway + "/_test/metrics")
    assert during["adp_tasks"] == held

    start = time.perf_counter()
    json_http(fake + "/_test/release", b"")
    completed = previous_sent + size + busy_count
    wait_for(lambda: count_states(config, "send_state", "SEND_SIMULATED") == completed, seconds=50)
    end = wait_for(lambda: (status if not (status := json_http(gateway + "/_test/metrics"))["send_tasks"]
                            and not status["adp_tasks"] else None))
    assert end["dispatcher_failure"] is None and end["store"]["write_errors"] == 0
    assert end["logging"]["log_dropped"] == end["logging"]["log_write_failures"] == 0

    def percentile(p):
        """使用最近秩计算 ACK 百分位，保持小样本可重复。"""
        return round(durations[(len(durations) * p + 99) // 100 - 1], 1)

    summary = {"users": size, "ack": size, "active_sse": held, "completed": completed - previous_sent,
               "busy_rejections": busy_count, "ack_p50_ms": round(statistics.median(durations), 1),
               "ack_p95_ms": percentile(95), "ack_p99_ms": percentile(99), "ack_max_ms": round(durations[-1], 1),
               "ack_batch_seconds": round(elapsed, 3), "drain_seconds": round(time.perf_counter() - start, 3),
               "held": during, "after": end, "mixed": mixed}
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return summary, (previous_calls + size, completed)


def verify_records(config, calls, total, busy):
    """全量核对压测产生的独立测试记录及轮转后的完整问答。"""
    records = read_messages(config)
    assert len(records) == total + busy
    assert len({record["message_key"] for record in records}) == len(records)
    accepted = [record for record in records if record["adp_state"] == "ADP_SUCCEEDED"]
    assert len(accepted) == total and all(record["adp_result"]["text"] == ANSWER for record in accepted)
    assert all(record["send_state"] == "SEND_SIMULATED" and record["send_result"]["source"] == "simulated"
               for record in records)
    assert {record["request_id"] for record in accepted} == {call["RequestId"] for call in calls}
    assert len(calls) == total
    assert len({(call["UserId"], call["ConversationId"]) for call in calls}) == len({call["UserId"] for call in calls})
    log = Path(config.log_file)
    logged_questions, logged_answers = [], []
    paths = sorted(log.parent.glob(log.name + "*"))
    assert len(paths) > 1
    for path in paths:
        with path.open(encoding="utf-8") as source:
            for line in source:
                assert config.app_key not in line and config.callback_token not in line
                assert "LOAD_SLOW_" not in line and "LOAD_FAST_" not in line
                entry = parse_log_line(line)
                if "question" in entry:
                    assert entry["question"] == QUESTION
                    logged_questions.append(entry["message_key"])
                if "answer" in entry:
                    assert entry["answer"] == ANSWER
                    logged_answers.append((entry["message_key"], entry["request_id"]))
    assert sorted(logged_questions) == sorted(record["message_key"] for record in records)
    assert sorted(logged_answers) == sorted((record["message_key"], record["request_id"]) for record in accepted)
    return {"rows": len(records), "questions": len(logged_questions), "answers": len(logged_answers),
            "log_files": len(paths)}


def run(root, args):
    """启动独立本机进程，失败或成功均回收自有进程和测试目录。"""
    fake_port, gateway_port = free_port(), free_port()
    while gateway_port == fake_port:
        gateway_port = free_port()
    fake, gateway = f"http://127.0.0.1:{fake_port}", f"http://127.0.0.1:{gateway_port}"
    path = root / "config.toml"
    token = secrets.token_hex(12)
    aes = base64.b64encode(os.urandom(32)).decode()[:-1]
    path.write_text('[adp]\napp_key="fake-local-adp-only"\nuser_id="fixture"\n'
                    '[logging]\nlog_answer=true\nlog_question=true\nlog_backup_count=40\n'
                    'log_max_bytes=2097152\n[wecom]\ncorp_id="fixture-corp"\nagent_id=1\n'
                    f'adp_application="wecom-local-load"\ngateway_enabled=true\ncallback_token="{token}"\n'
                    f'encoding_aes_key="{aes}"\nadp_url="{fake}/adp/v2/chat"\n'
                    'notification_mode="simulated"\ngateway_environment="test"\n', encoding="utf-8")
    config = load_config(path)
    env = {**os.environ, "ADP_CONFIG_FILE": str(path)}
    with ExitStack() as cleanup:
        for kind, port, ready in [("fake", fake_port, fake + "/_test/status"),
                                  ("gateway", gateway_port, gateway + "/healthz")]:
            log = cleanup.enter_context((root / f"{kind}.log").open("w", encoding="utf-8"))
            process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", __file__, "--serve", kind,
                                        "--port", str(port)], stdin=subprocess.PIPE, stdout=log, stderr=log, env=env)
            cleanup.callback(stop, process)
            wait_for(lambda: process.poll() is None and json_http(ready), seconds=20)
        gateway_process = process
        previous = (0, 0)
        busy = 0
        start = time.perf_counter()
        for number, (size, mixed) in enumerate(
            [(size, False) for size in args.users] + [(max(args.users), False)] * args.repeats
            + [(max(args.users), True)], 1
        ):
            row, previous = run_batch(config, gateway, fake, size=size, number=number, previous=previous,
                                      hold_seconds=args.hold_seconds, mixed=mixed)
            busy += row["busy_rejections"]
        calls = json_http(fake + "/_test/status")["calls"]
        checkpoint = json_http(gateway + "/_test/metrics?checkpoint=true")["checkpoint"]
        stop(gateway_process)
        evidence = verify_records(config, calls, previous[0], busy)
        print(json.dumps({"verified": evidence, "seconds": round(time.perf_counter() - start, 3),
                          "final_passive_checkpoint": checkpoint,
                          "upstream": "local_fake_only", "notifications": "simulated"}, ensure_ascii=False))


def main():
    """只生成压测配置，禁止传入真实配置或远端地址。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, nargs="+", default=[100, 300, 500, 1000])
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--hold-seconds", type=float, default=6)
    parser.add_argument("--serve", choices=["fake", "gateway"], help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve:
        serve(args.serve, args.port)
    else:
        if not all(2 <= size <= 1000 for size in args.users) or not 0 <= args.repeats <= 4 or not 0 <= args.hold_seconds <= 30:
            parser.error("负载用户数须为 2 至 1000，重复轮次 0 至 4，保持时间 0 至 30 秒。")
        with tempfile.TemporaryDirectory(prefix="wecom-load-") as directory:
            run(Path(directory), args)


if __name__ == "__main__":
    main()
