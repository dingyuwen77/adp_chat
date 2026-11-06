"""交互式 CLI 入口。"""

from .cli.runner import entrypoint


if __name__ == "__main__":
    raise SystemExit(entrypoint())
