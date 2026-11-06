"""单次 ADP 文字对话入口。"""

from adp_chat.cli.runner import entrypoint, main as run_cli


def main(argv: list[str] | None = None) -> int:
    """发送一轮文字后结束，供脚本和测试调用。"""
    return run_cli(argv, single=True)


if __name__ == "__main__":
    raise SystemExit(entrypoint(single=True))
