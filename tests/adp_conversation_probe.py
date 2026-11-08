"""受控 ADP 单轮验收入口；默认只展示身份，不访问网络。"""

import argparse
import asyncio
import json
import os
import time
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from urllib.parse import urlsplit

from adp.client import chat
from core.config import ADP_CHAT_URL, load_config
from core.identity import employee_identity
from core.models import ChatError


def gateway_round(config, identity, question, url):
    """正式加密回调只提交一次，再读同目录终态；不得绕验签或重发未知请求。"""
    from core.models import ChatResult
    from storage.messages import message_key, read_snapshot
    from wecom_dispatch_smoke import callback

    target = urlsplit(url)
    if (target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost", "::1"}
            or target.username or target.password or target.query or target.fragment or target.path not in {"", "/"}
            or not config.gateway_enabled or config.notification_mode != "simulated"
            or config.gateway_environment != "test" or config.adp_url != ADP_CHAT_URL):
        raise ChatError("config", "invalid_probe_target", "真实网关探测须本机HTTP、独立test/simulated配置及正式ADP端点。")
    msg_id = str(time.time_ns()) + str(os.getpid())
    key = message_key(config.corp_id, config.agent_id, msg_id)
    snapshot = Path(config.messages_dir) / f"{key}.json"
    acknowledgement = "received"
    try:
        callback(config, url.rstrip("/"), identity.user_id, msg_id, question)
    except AssertionError:
        raise ChatError("probe", "invalid_callback_ack", "回调ACK格式异常，保留原消息核查，不重发。") from None
    except OSError:
        # ACK连接故障可能发生在可靠接收之后；只观察原消息，不再提交。
        acknowledgement = "unknown"
    deadline = time.monotonic() + config.chat_timeout_seconds + config.send_timeout_seconds + 10
    while time.monotonic() < deadline:
        try:
            record = read_snapshot(snapshot)
        except FileNotFoundError:
            time.sleep(0.05)
            continue
        if (record.get("message_key") != key or record.get("user_id") != identity.user_id
                or record.get("conversation_id") != identity.conversation_id or record.get("environment") != "test"):
            raise ChatError("probe", "invalid_probe_evidence", "快照身份/作用域不匹配，停止探测。")
        if record["adp_state"] in {"ADP_SUCCEEDED", "ADP_REJECTED", "ADP_UNKNOWN", "REJECTED_USER_BUSY"}:
            metadata = {"message_key": key, "send_state": record["send_state"], "callback_ack": acknowledgement,
                        "notification_source": (record.get("send_result") or {}).get("source"),
                        "gateway_adp_state": record["adp_state"]}
            if (record["adp_state"] == "ADP_SUCCEEDED" and record["send_state"] == "SEND_SIMULATED"
                    and metadata["notification_source"] == "simulated"):
                result = record["adp_result"]
                if result["conversation_id"] != record["conversation_id"] or result["request_id"] != record["request_id"]:
                    raise ChatError("probe", "invalid_probe_evidence", "ADP关联标识不匹配，停止探测。")
                return ChatResult(**{**result, "reply_contents": tuple(tuple(p for p in m) for m in result["reply_contents"])}), metadata
            if record["send_state"] in {"SEND_SIMULATED", "SEND_FAILED", "SEND_UNKNOWN"}:
                error = record["adp_error"] or {}
                exc = ChatError("chat", error.get("code", record["adp_state"]), "真实网关探测未得到确定成功。",
                    provider_code=error.get("provider_code"), provider_request_id=error.get("provider_request_id"),
                    provider_trace_id=error.get("provider_trace_id"), http_status=error.get("http_status"))
                exc.request_id, exc.conversation_id = record["request_id"], record["conversation_id"]
                exc.gateway_evidence = metadata
                raise exc
        time.sleep(0.05)
    raise ChatError("probe", "gateway_wait_unknown", "观察超时，保留原消息核查，不重发。")


async def async_round(config, identity, question):
    """只探测正式异步协议；不是网关持久化验收，不自动重试。"""
    from adp.async_client import AsyncADPClient

    client = AsyncADPClient(config)
    request_id = str(uuid4())
    try:
        outcome = await client.request({"user_id": identity.user_id, "conversation_id": identity.conversation_id,
                                        "request_id": request_id, "content": question})
        if outcome.state == "ADP_SUCCEEDED":
            return outcome.result
        error = outcome.error or {}
        exc = ChatError("chat", error.get("code", "unknown"), "真实ADP探测没有获得确定成功。",
                        provider_code=error.get("provider_code"), http_status=error.get("http_status"),
                        provider_request_id=error.get("provider_request_id"), provider_trace_id=error.get("provider_trace_id"))
        exc.request_id, exc.conversation_id = request_id, identity.conversation_id
        raise exc
    finally:
        await client.aclose()


def main(argv: list[str] | None = None) -> int:
    """每次进程最多发一轮允许的测试文字，不判断语义通过、不重试。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="使用现有配置来源，默认项目 config.toml")
    parser.add_argument("--user-id", required=True, choices=("YNND", "SIYU"))
    parser.add_argument("--message", choices=("我想改密码", "windows"), default="我想改密码")
    parser.add_argument("--execute-real-adp", action="store_true", help="明确允许本进程的一次真实付费请求")
    parser.add_argument("--output", help="真实请求的本地证据 JSON；必须为尚不存在的文件")
    parser.add_argument("--async-client", action="store_true", help="探测C正式异步HTTPS/SSE；仍需显式真实授权")
    parser.add_argument("--gateway-url", help="E通过本机正式加密回调执行；须显式真实授权和simulated通知")
    args = parser.parse_args(argv)
    if args.execute_real_adp and not args.output:
        parser.error("真实请求必须指定 --output，保留结果并人工审查语义；不可提交原始正文")
    try:
        config = replace(load_config(args.config), log_enabled=False, log_question=False, log_answer_chars=0)
        identity = employee_identity(config, args.user_id)
        if args.execute_real_adp and args.async_client and config.adp_url != ADP_CHAT_URL:
            raise ChatError("config", "invalid_probe_target", "真实探测只允许腾讯正式HTTPS端点。")
        evidence = {
            "schema_version": 1,
            "timestamp": datetime.now(timezone(timedelta(hours=8))).isoformat(),
            "pid": os.getpid(), "adp_application": config.adp_application,
            **asdict(identity), "real_adp_requested": args.execute_real_adp,
            "transport": "gateway_https_sse" if args.gateway_url else "async_https_sse" if args.async_client else "sync_https_sse",
        }
        if not args.execute_real_adp:
            print(json.dumps(evidence, ensure_ascii=False))
            return 0
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先独占创建证据载体，避免文件已存在时仍调用 ADP。失败保留文件，不重投。
        with path.open("x", encoding="utf-8") as output:
            try:
                if args.gateway_url:
                    result, metadata = gateway_round(config, identity, args.message, args.gateway_url)
                    evidence.update(metadata)
                else:
                    result = (asyncio.run(async_round(config, identity, args.message)) if args.async_client else
                              chat(args.message, config=identity.chat_config(config), conversation_id=identity.conversation_id))
                evidence.update(status="response.completed", request_id=result.request_id,
                                record_id=result.record_id, reply_method=result.reply_method,
                                response_text=result.text, semantic_verdict="requires_review")
                code = 0
            except ChatError as exc:
                evidence.update(getattr(exc, "gateway_evidence", {}))
                evidence.update(status="error", stage=exc.stage, code=exc.code,
                                request_id=exc.request_id, provider_code=exc.provider_code,
                                provider_request_id=exc.provider_request_id,
                                provider_trace_id=exc.provider_trace_id)
                code = 1
            json.dump(evidence, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        # 控制台只展示标识和状态；正文留在明确指定的本地文件中供语义审查。
        print(json.dumps({k: v for k, v in evidence.items() if k != "response_text"}, ensure_ascii=False))
        return code
    except ChatError as exc:
        print(json.dumps({"status": "error", "stage": exc.stage, "code": exc.code}))
        return 1
    except (OSError, ValueError):
        print(json.dumps({"status": "error", "code": "probe_evidence_unavailable"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
