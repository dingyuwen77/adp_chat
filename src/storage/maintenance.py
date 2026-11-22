"""检查 SQLite 消息库或使用 SQLite 在线备份 API 生成独立副本。"""

import argparse
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from time import monotonic

from core.models import ChatError

from .messages import storage_error
from .schema import SCHEMA_VERSION
from .sqlite_store import _open_reader


def has_message_history(path: Path, *, required=False) -> bool:
    """只读检查数据库是否接收过消息。30 天清理后仍保留自增的历史证据。"""
    if not path.is_file():
        if path.exists() or path.is_symlink():
            raise storage_error("invalid_database_path")
        if required:
            raise storage_error("missing_database")
        return False
    try:
        with closing(_open_reader(path)) as reader:
            if reader.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
                raise storage_error("unsupported_schema")
            return bool(reader.execute(
                "SELECT EXISTS(SELECT 1 FROM sqlite_sequence WHERE name = 'gateway_messages' AND seq > 0)"
            ).fetchone()[0])
    except (sqlite3.Error, OSError):
        raise storage_error("read_failed") from None


def check_database(path: Path) -> dict:
    """仅报告完整性和版本，不打印行值或 SQLite 诊断中的正文。"""
    try:
        with closing(_open_reader(path)) as reader:
            version = reader.execute("PRAGMA user_version").fetchone()[0]
            check = reader.execute("PRAGMA integrity_check").fetchall()
            if version != SCHEMA_VERSION or len(check) != 1 or check[0][0] != "ok":
                raise storage_error("integrity_check_failed")
    except (sqlite3.Error, OSError):
        raise storage_error("integrity_check_failed") from None
    return {"integrity": "ok", "schema_version": version, "sqlite_version": sqlite3.sqlite_version}


def backup_database(source: Path, destination: Path, *, timeout_seconds=60) -> dict:
    """由只读源创建一致副本，校验成功后原子发布且禁止覆盖已有备份。"""
    deadline = monotonic() + timeout_seconds

    def progress(_status, _remaining, _total):
        """持续有写入或锁竞争时，限制在线备份等待时间。"""
        if monotonic() >= deadline:
            raise storage_error("backup_timeout")

    temporary = None
    try:
        if destination.exists() or destination.is_symlink() or source.resolve() == destination.resolve():
            raise storage_error("backup_exists")
        fd, name = tempfile.mkstemp(prefix=".gateway-backup-", dir=destination.parent)
        temporary = Path(name)
        os.close(fd)
        with closing(_open_reader(source)) as reader, closing(sqlite3.connect(temporary)) as target:
            reader.backup(target, pages=128, progress=progress, sleep=0.05)
            target.execute("PRAGMA journal_mode = DELETE").fetchone()
        result = check_database(temporary)
        # 同目录硬链接发布拒绝覆盖。SQLite 已关闭目标，副本无需 WAL/SHM。
        os.link(temporary, destination)
        return result
    except (sqlite3.Error, OSError):
        raise storage_error("backup_failed") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    """供本机或容器运维执行完整性检查与在线备份。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    check = commands.add_parser("check")
    check.add_argument("database", type=Path)
    backup = commands.add_parser("backup")
    backup.add_argument("database", type=Path)
    backup.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    try:
        result = (check_database(args.database) if args.operation == "check"
                  else backup_database(args.database, args.destination))
    except ChatError as error:
        parser.exit(1, f"{error.stage}/{error.code}\n")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
