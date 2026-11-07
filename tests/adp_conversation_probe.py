"""受控 ADP 单轮验收入口；默认只展示身份，不访问网络。"""

import argparse
import json
import os
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from adp.client import chat
from core.config import load_config
from core.identity import employee_identity
from core.models import ChatError


def main(argv: list[str] | None = None) -> int:
    """每次进程最多发一轮允许的测试文字，不判断语义通过、不重试。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="使用现有配置来源，默认项目 config.toml")
    parser.add_argument("--user-id", required=True, choices=("YNND", "SIYU"))
    parser.add_argument("--message", choices=("我想改密码", "windows"), default="我想改密码")
    parser.add_argument("--execute-real-adp", action="store_true", help="明确允许本进程的一次真实付费请求")
    parser.add_argument("--output", help="真实请求的本地证据 JSON；必须为尚不存在的文件")
    args = parser.parse_args(argv)
    if args.execute_real_adp and not args.output:
        parser.error("真实请求必须指定 --output，保留结果并人工审查语义；不可提交原始正文")
    try:
        config = replace(load_config(args.config), log_enabled=False, log_question=False, log_answer_chars=0)
        identity = employee_identity(config, args.user_id)
        evidence = {
            "schema_version": 1,
            "timestamp": datetime.now(timezone(timedelta(hours=8))).isoformat(),
            "pid": os.getpid(), "adp_application": config.adp_application,
            **asdict(identity), "real_adp_requested": args.execute_real_adp,
        }
        if not args.execute_real_adp:
            print(json.dumps(evidence, ensure_ascii=False))
            return 0
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先独占创建证据载体，避免文件已存在时仍调用 ADP。失败保留文件，不重投。
        with path.open("x", encoding="utf-8") as output:
            try:
                result = chat(args.message, config=identity.chat_config(config),
                              conversation_id=identity.conversation_id)
                evidence.update(status="response.completed", request_id=result.request_id,
                                record_id=result.record_id, reply_method=result.reply_method,
                                response_text=result.text, semantic_verdict="requires_review")
                code = 0
            except ChatError as exc:
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
