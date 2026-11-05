"""单次转发一条消息并退出；下次启动自动续接本地会话。"""

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from adp_chat import ChatConfig, ChatError, ChatResult, chat, chat_until_complete, load_config
from adp_chat.console import read_clarification, show_result
from adp_chat.cli import _show_error
from adp_chat.session import load_session


_PROJECT_ROOT = Path(__file__).absolute().parent


def example_text_chat(config: ChatConfig) -> ChatResult:
    """普通问答：仅在 ADP 追问时读取补充答案。"""
    return chat_until_complete(
        "我想改密码", config=config,
        answer_provider=read_clarification, on_result=show_result,
    )


def main(argv: list[str] | None = None) -> int:
    """读取本轮输入，先保存会话，再调用一次 ADP，不收集追问补答。"""
    parser = argparse.ArgumentParser(description="单次 ADP 消息转发，跨进程恢复会话")
    parser.add_argument("--config", type=Path, default=_PROJECT_ROOT / "config.toml")
    parser.add_argument("--ask", help="本轮问题；省略时从键盘读取")
    parser.add_argument("--new-session", action="store_true", help="显式替换本地会话；可单独执行")
    args = parser.parse_args(argv)
    try:
        config = replace(load_config(args.config), detect_text_clarification=False, structured_interaction=False)
        if args.new_session and args.ask is None:
            conversation_id = load_session(config, new=True)
            print(f"已开启新会话：{conversation_id}", file=sys.stderr)
            return 0
        if args.ask is None:
            print("你（文字问题，或 /new）：", end="", file=sys.stderr, flush=True)
            question = input().strip()
        else:
            question = args.ask.strip()
        if question.lower() == "/new":
            conversation_id = load_session(config, new=True)
            print(f"已开启新会话：{conversation_id}", file=sys.stderr)
            return 0
        if not question:
            raise ChatError("input", "empty_question", "问题不能为空。")
        if question.split(maxsplit=1)[0].lower() in ("/image", "/file"):
            raise ChatError("input", "unsupported_command", "当前只转发文字，不支持附件命令。")
        conversation_id = load_session(config, new=args.new_session)
        result = chat(question, config=config, conversation_id=conversation_id)
        show_result(result)
    except ChatError as error:
        _show_error(error)
        return 1
    except EOFError:
        print("输入结束，本轮未发送。", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and not stream.isatty():
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
