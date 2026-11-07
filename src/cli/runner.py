"""单次脚本与交互 CLI 共用的命令行逻辑。"""

import argparse
import sys
from pathlib import Path

from application.chat_service import ChatService
from core.config import default_config_path, load_config
from core.models import ChatError
from .console import show_result


def _show_error(error: ChatError) -> None:
    """输出稳定的错误分类和安全诊断标识。"""
    suffix = f" request_id={error.request_id}" if error.request_id else ""
    print(f"[{error.stage}/{error.code}] {error}{suffix}", file=sys.stderr)


def _send(service: ChatService, message: str) -> bool:
    """单轮展示正式回复，异常不产生自动重试。"""
    try:
        show_result(service.ask(message))
        return True
    except ChatError as error:
        _show_error(error)
        return False


def main(argv: list[str] | None = None, *, single: bool = False) -> int:
    """根据单次或交互模式读取文字并调用同一应用服务。"""
    parser = argparse.ArgumentParser(description="腾讯 ADP 单用户文字对话")
    parser.add_argument("--config", type=Path, default=default_config_path())
    parser.add_argument("--ask", help="发送一条文字后退出")
    parser.add_argument("--new-session", action="store_true", help="本地手工开启新会话")
    args = parser.parse_args(argv)
    try:
        service = ChatService(load_config(args.config))
        if args.new_session:
            print(f"已开启新会话：{service.reset_session()}", file=sys.stderr)
            if args.ask is None:
                return 0
        if args.ask is not None:
            return 0 if _send(service, args.ask) else 1
        if single:
            print("你：", end="", file=sys.stderr, flush=True)
            return 0 if _send(service, input()) else 1
        print("输入文字消息，/new 开启新会话，/exit 退出。", file=sys.stderr)
        while True:
            print("你：", end="", file=sys.stderr, flush=True)
            value = input().strip()
            if value.lower() in ("/exit", "/quit"):
                return 0
            if value.lower() == "/new":
                print(f"已开启新会话：{service.reset_session()}", file=sys.stderr)
                continue
            if not value:
                continue
            _send(service, value)
    except ChatError as error:
        _show_error(error)
        return 1
    except EOFError:
        return 0
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


def entrypoint(argv: list[str] | None = None, *, single: bool = False) -> int:
    """在重定向输出时使用 UTF-8，再进入命令行主流程。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8")
    return main(argv, single=single)
