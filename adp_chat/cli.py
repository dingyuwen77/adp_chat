import argparse
import io
import sys
from pathlib import Path

from .client import chat_until_complete
from .config import ChatConfig, load_config
from .console import read_clarification, show_result
from .models import ChatError, ChatResult, PreviewEvent


def _show_error(error: ChatError) -> None:
    """将库的结构化错误转换成终端提示。"""
    details = [
        f"request_id={error.request_id}" if error.request_id else "",
        f"provider_code={error.provider_code}" if error.provider_code else "",
        f"provider_request_id={error.provider_request_id}" if error.provider_request_id else "",
        f"provider_trace_id={error.provider_trace_id}" if error.provider_trace_id else "",
    ]
    suffix = " ".join(detail for detail in details if detail)
    print(f"[{error.stage}/{error.code}] {error} {suffix}".rstrip(), file=sys.stderr)


def _turn(
    question: str, config: ChatConfig, conversation_id: str | None, *,
    file: str | None = None, image: str | None = None,
) -> ChatResult:
    """运行公共追问流程，分别显示预览和每轮正式回复。"""
    preview_started = False

    def show(event: PreviewEvent) -> None:
        """在终端标记修订片段，使其区别于追加文本。"""
        nonlocal preview_started
        if not preview_started:
            print("[流式预览]", file=sys.stderr)
            preview_started = True
        if event.kind == "replace":
            print("\n[修订片段] ", end="", file=sys.stderr)
        print(event.text, end="", file=sys.stderr, flush=True)

    def completed(result: ChatResult) -> None:
        """结束本轮预览后显示回复，再由协议决定是否取补充答案。"""
        nonlocal preview_started
        if preview_started:
            print(file=sys.stderr)
            preview_started = False
        show_result(result)

    try:
        return chat_until_complete(
            question, config=config, file=file, image=image, conversation_id=conversation_id,
            on_preview=show if config.show_preview and not config.structured_interaction else None,
            answer_provider=read_clarification, on_result=completed,
        )
    finally:
        if preview_started:
            print(file=sys.stderr)


def _interactive(config: ChatConfig, conversation_id: str | None) -> int:
    """处理终端输入、单轮附件和会话切换。"""
    print("输入问题，或使用 /file 路径、/image 路径、/new、/exit。", file=sys.stderr)
    while True:
        print("你：", end="", file=sys.stderr, flush=True)
        question = input().strip()
        if not question:
            continue
        command, *arguments = question.split(maxsplit=1)
        command = command.lower()
        if question.lower() in ("/exit", "/quit"):
            return 0
        if question.lower() == "/new":
            conversation_id = None
            print("下一轮将开启新会话。", file=sys.stderr)
            continue
        attachment = {}
        if command in ("/file", "/image"):
            if not arguments:
                print("请在命令后输入本地附件路径。", file=sys.stderr)
                continue
            path = arguments[0].strip()
            if len(path) >= 2 and path[0] == path[-1] and path[0] in ("\"", "'"):
                path = path[1:-1]
            print("想问什么：", end="", file=sys.stderr, flush=True)
            question = input().strip()
            attachment[command[1:]] = path
        try:
            result = _turn(question, config, conversation_id, **attachment)
            conversation_id = result.conversation_id
        except ChatError as error:
            _show_error(error)
            if error.code == "clarification_cancelled":
                return 1
            if error.stage == "chat" and conversation_id is None:
                conversation_id = error.conversation_id


def main(argv: list[str] | None = None) -> int:
    """解析命令行参数并执行单次或交互聊天。"""
    parser = argparse.ArgumentParser(description="腾讯云 ADP Multi-Agent 对话")
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="本地 TOML 配置")
    parser.add_argument("--ask", help="完成本题及必要的 ADP 追问后退出")
    parser.add_argument("--conversation-id", help="继续指定会话")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--file", help="本轮文档路径")
    group.add_argument("--image", help="本轮图片路径")
    args = parser.parse_args(argv)
    if (args.file is not None or args.image is not None) and args.ask is None:
        parser.error("--file 或 --image 必须和 --ask 一起使用")
    try:
        config = load_config(args.config)
        if args.ask is not None:
            _turn(args.ask, config, args.conversation_id, file=args.file, image=args.image)
            return 0
        conversation_id = args.conversation_id
        if config.first_question.strip():
            try:
                result = _turn(config.first_question, config, conversation_id)
                conversation_id = result.conversation_id
            except ChatError as error:
                if not config.interactive or error.code == "clarification_cancelled":
                    raise
                _show_error(error)
                if error.stage == "chat" and conversation_id is None:
                    conversation_id = error.conversation_id
        elif not config.interactive:
            raise ChatError("config", "missing_question", "关闭交互时需要提供 --ask 或 first_question。")
        return _interactive(config, conversation_id) if config.interactive else 0
    except ChatError as error:
        _show_error(error)
        return 1
    except EOFError:
        return 0
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130


def entrypoint(argv: list[str] | None = None) -> int:
    """将管道输入输出统一为 UTF-8，再启动终端命令。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper) and not stream.isatty():
            stream.reconfigure(encoding="utf-8")
    return main(argv)
