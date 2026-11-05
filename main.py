"""分别演示普通问答、图片问答和文件问答。"""

import sys
from pathlib import Path

from adp_chat import ChatConfig, ChatError, ChatResult, chat_until_complete, load_config
from adp_chat.console import read_clarification, show_result


_PROJECT_ROOT = Path(__file__).absolute().parent


def example_text_chat(config: ChatConfig) -> ChatResult:
    """普通问答：仅在 ADP 追问时读取补充答案。"""
    return chat_until_complete(
        "windows", config=config,
        answer_provider=read_clarification, on_result=show_result,
    )


def example_image_chat(config: ChatConfig) -> ChatResult:
    """图片问答：首轮上传图片，必要时在同一会话补充回答。"""
    return chat_until_complete(
        "请描述这张图片",
        config=config,
        image=_PROJECT_ROOT / "examples" / "小楷.jpeg",
        answer_provider=read_clarification, on_result=show_result,
    )


def example_file_chat(config: ChatConfig) -> ChatResult:
    """文件问答：首轮上传文件，必要时在同一会话补充回答。"""
    return chat_until_complete(
        "请总结这首诗",
        config=config,
        file=_PROJECT_ROOT / "examples" / "致橡树.txt",
        answer_provider=read_clarification, on_result=show_result,
    )


def main() -> int:
    """加载项目配置，依次运行已启用的示例并显示各轮回复。"""
    try:
        config = load_config(_PROJECT_ROOT / "config.toml")
        # 每个示例各开一个会话；可按需注释暂时不运行的示例。
        example_text_chat(config)
        # example_image_chat(config)
        # example_file_chat(config)
    except ChatError as error:
        print(error.stage, error.code, error.request_id, str(error), file=sys.stderr)
        return 1
    except EOFError:
        print("已结束，当前追问未作答。", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("\n已中断。", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
