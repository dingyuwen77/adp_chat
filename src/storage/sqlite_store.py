"""单进程 SQLite WAL 消息状态。一个可写连接与一个只读查询连接分属专用线程。"""

import asyncio
import json
import os
import re
import sqlite3
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from threading import Event, Lock
from time import perf_counter
from typing import NamedTuple

from core.identity import employee_identity
from core.models import ChatError
from wecom.models import InboundMessage, is_employee_id

from .messages import BUSY_NOTICE, UPDATE_FIELDS, message_key, storage_error, transition
from .schema import ACTIVE, RETENTION, SEND_OPEN, UNFINISHED, initialize_schema


_BEIJING = timezone(timedelta(hours=8))
_BUSY_TIMEOUT_SECONDS = 1
_WRITE_BATCH = 32
_MAINTENANCE_BATCH = 512
_JSON_FIELDS = ("adp_result", "adp_error", "send_result")


class _PendingWrite(NamedTuple):
    operation: Callable
    args: tuple
    future: Future
    submitted_at: float


def _timestamp(now=None) -> str:
    """统一按北京时间排序和比较持久记录，保留既有精度。"""
    return (now or datetime.now(_BEIJING)).astimezone(_BEIJING).isoformat(timespec="milliseconds")


def _open_reader(path: Path) -> sqlite3.Connection:
    """只读连接没有写入与 checkpoint 权限，路径缺失时也不创建空库。"""
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=_BUSY_TIMEOUT_SECONDS)
    conn.row_factory = sqlite3.Row
    return conn


def _record(row: sqlite3.Row | None) -> dict | None:
    """把索引列和独立结果字段还原为网关编排使用的记录。"""
    if row is None:
        return None
    result = dict(row)
    result["receive_sequence"] = result.pop("id")
    result.pop("send_user")
    result["create_time"] = int(result["create_time"])
    result["redacted"] = bool(result["redacted"])
    for name in _JSON_FIELDS:
        encoded = result.pop(f"{name}_json")
        result[name] = json.loads(encoded) if encoded is not None else None
    return result


def read_messages(config, *, key: str | None = None) -> list[dict]:
    """供本地查验与测试读取已提交状态，不取得生产网关的独占锁。"""
    try:
        with closing(_open_reader(config.gateway_database_path)) as read:
            if key is not None:
                return [_record(row) for row in read.execute(
                    "SELECT * FROM gateway_messages WHERE message_key = ?", (key,)
                )]
            return [_record(row) for row in read.execute("SELECT * FROM gateway_messages ORDER BY id")]
    except (sqlite3.Error, OSError, ValueError):
        raise storage_error("read_failed") from None


class MessageStore:
    """写线程串行提交短事务，外部任务通过持久状态和版本取得执行权。"""

    def __init__(self, config):
        """持有单实例锁，成功初始化库和两个线程后才对外服务。"""
        self.config = config
        self.path = config.gateway_database_path.resolve()
        self._writer = self._reader = self._lease = None
        self._closed = False
        self._healthy = True
        self._retry_after = 0.0
        self._guard = Lock()
        self._pending_writes = deque()
        self._batch_idle = Event()
        self._batch_idle.set()
        self._stats = dict(write_pending=0, write_pending_peak=0, write_queue_full=0, write_errors=0, write_busy=0,
                           write_batch_peak=0,
                           commits=0, commit_ms_total=0.0, commit_ms_max=0.0,
                           write_wait_ms_total=0.0, write_wait_ms_max=0.0,
                           write_lock_ms_total=0.0, write_lock_ms_max=0.0)
        self._writes = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gateway-db-write")
        self._reads = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gateway-db-read")
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._lock_database()
            self._writes.submit(self._open_writer).result()
            self._reads.submit(self._open_queries).result()
        except (ChatError, sqlite3.Error, OSError, ValueError):
            self.close()
            raise storage_error("initialization_failed") from None

    def _lock_database(self) -> None:
        """保护出站恢复所有权，第二个网关不能把运行中的已武装请求标记为未知。"""
        self._lease = self.path.with_name(self.path.name + ".lock").open("a+b")
        self._lease.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self._lease.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _open_writer(self) -> None:
        """只在专用线程连接，以 WAL 和 FULL 同步取得可靠提交语义。"""
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self._writer = sqlite3.connect(self.path, isolation_level=None, timeout=_BUSY_TIMEOUT_SECONDS)
        self._writer.row_factory = sqlite3.Row
        if self._writer.execute("PRAGMA journal_mode = WAL").fetchone()[0] != "wal":
            raise storage_error("wal_unavailable")
        self._writer.execute("PRAGMA synchronous = FULL")
        self._writer.execute("PRAGMA foreign_keys = ON")
        with self._transaction():
            initialize_schema(self._writer, self.config)

    def _open_queries(self) -> None:
        """查询线程始终使用只读连接，SQLite 写锁只由写线程申请。"""
        self._reader = _open_reader(self.path)

    def _record_timing(self, name: str, started: float) -> None:
        """记录毫秒累计值和峰值，不在高频事务内写诊断日志。"""
        ms = (perf_counter() - started) * 1000
        with self._guard:
            self._stats[f"{name}_ms_total"] += ms
            self._stats[f"{name}_ms_max"] = max(self._stats[f"{name}_ms_max"], ms)

    def _commit(self) -> None:
        """等待 SQLite 返回提交完成后才发布结果，统计包含同步写入的耗时。"""
        start = perf_counter()
        self._writer.commit()
        self._record_timing("commit", start)
        with self._guard:
            self._stats["commits"] += 1

    @contextmanager
    def _transaction(self):
        """已知业务拒绝只回滚当前事务。写盘故障关闭接收，由重启核对持久事实。"""
        if not self._healthy:
            raise storage_error()
        if perf_counter() < self._retry_after:
            raise storage_error("storage_busy")
        try:
            start = perf_counter()
            try:
                self._writer.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                code = getattr(exc, "sqlite_errorcode", 0) & 0xff
                if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise
                # BEGIN 未取得锁，没有任何提交结果待确认。短冷却保护队列中的后续回调。
                self._retry_after = perf_counter() + 0.05
                with self._guard:
                    self._stats["write_busy"] += 1
                raise storage_error("storage_busy") from None
            finally:
                self._record_timing("write_lock", start)
            try:
                yield
                self._commit()
            finally:
                if self._writer.in_transaction:
                    self._writer.rollback()
        except (sqlite3.Error, OSError, json.JSONDecodeError) as exc:
            with self._guard:
                self._healthy = False
                self._stats["write_errors"] += 1
            raise storage_error() from None

    def _count_write(self) -> None:
        """调用方持有 guard 时更新待写额度及高水位。"""
        self._stats["write_pending"] += 1
        self._stats["write_pending_peak"] = max(self._stats["write_pending_peak"], self._stats["write_pending"])

    def _submit_write(self, operation, *args):
        """低频维护使用独立任务，仍由唯一写线程执行。"""
        start = perf_counter()
        with self._guard:
            if self._closed or not self._healthy:
                raise storage_error()
            self._count_write()
            future = self._writes.submit(self._run_write, operation, start, args)
        future.add_done_callback(self._write_completed)
        return future

    def _run_write(self, operation, start, args):
        """区分应用排队与数据库锁等待，用相同写线程执行所有业务写入。"""
        self._record_timing("write_wait", start)
        return operation(*args)

    def _write_completed(self, _future) -> None:
        """已完成和提交前被取消的写入都释放队列额度。"""
        with self._guard:
            self._stats["write_pending"] -= 1

    def _submit_read(self, query, *args):
        """读提交和关闭通过同一短临界区，查询本身在独立连接执行。"""
        with self._guard:
            if self._closed:
                raise storage_error()
            return self._reads.submit(self._run_read, query, args)

    def _run_read(self, query, args):
        """读出完整结果后释放 SQLite 游标，避免持有 WAL 读快照跨越网络。"""
        try:
            return query(self._reader, *args)
        except (sqlite3.Error, OSError, ValueError):
            raise storage_error("read_failed") from None

    def metrics(self) -> dict:
        """提供写队列、提交、锁等待和 WAL 大小，供低频日志及本机负载采样。"""
        with self._guard:
            stats = dict(self._stats)
        try:
            stats["wal_bytes"] = self.path.with_name(self.path.name + "-wal").stat().st_size
        except OSError:
            stats["wal_bytes"] = 0
        return stats

    @property
    def available(self) -> bool:
        """报告是否仍可写入。未开始事务的短暂写锁不改变持久健康。"""
        with self._guard:
            return not self._closed and self._healthy

    def submit_receive(self, message: InboundMessage):
        """有限接收入队，每条回调在同一 FIFO 中等待可靠提交。"""
        return self._enqueue_write(self._receive, (message,), admission=True)

    def _enqueue_write(self, operation, args, *, admission=False):
        """接收遵守待写额度，已接收的状态更新仍可入队，由 Worker 数量限制。"""
        submitted = perf_counter()
        future = Future()
        with self._guard:
            if self._closed or not self._healthy:
                raise storage_error()
            if admission and self._stats["write_pending"] >= self.config.database_write_queue:
                self._stats["write_queue_full"] += 1
                raise storage_error("write_queue_full")
            self._count_write()
            self._pending_writes.append(_PendingWrite(operation, args, future, submitted))
            if self._batch_idle.is_set():
                self._batch_idle.clear()
                self._writes.submit(self._write_batch)
        return future

    def _write_batch(self):
        """立即提交当前待写批次，按入队顺序执行，下一批让出维护机会。"""
        with self._guard:
            batch = [self._pending_writes.popleft() for _ in range(min(_WRITE_BATCH, len(self._pending_writes)))]
            self._stats["write_batch_peak"] = max(self._stats["write_batch_peak"], len(batch))
        active = [write for write in batch if write.future.set_running_or_notify_cancel()]
        results = []
        try:
            if active:
                with self._transaction():
                    for write in active:
                        self._record_timing("write_wait", write.submitted_at)
                        try:
                            results.append((write.operation(*write.args), None))
                        except ChatError as error:
                            # 准入拒绝及版本冲突不会修改行，同批其他写入可以提交。
                            results.append((None, error))
        except ChatError as failure:
            # 共享提交失败时，整批均不得发布成功或获得出站执行权。
            results = [(None, storage_error(failure.code)) for _ in active]
        except Exception:
            with self._guard:
                self._healthy = False
                self._stats["write_errors"] += 1
            results = [(None, storage_error()) for _ in active]
        finally:
            with self._guard:
                self._stats["write_pending"] -= len(batch)
            try:
                for write, (result, error) in zip(active, results, strict=True):
                    if error is None:
                        write.future.set_result(result)
                    else:
                        write.future.set_exception(error)
            finally:
                with self._guard:
                    if self._pending_writes:
                        self._writes.submit(self._write_batch)
                    else:
                        self._batch_idle.set()

    def put_if_absent(self, message: InboundMessage) -> tuple[dict, bool]:
        """同步入口用于隔离测试和离线操作，复用生产接收事务。"""
        return self.submit_receive(message).result()

    def _receive(self, message: InboundMessage) -> tuple[dict, bool]:
        """重复检查、用户互斥、网关容量与入库共同决定可靠回调。"""
        key = message_key(message.corp_id, message.agent_id, message.msg_id)
        found = self._get(self._writer, key)
        if found is not None:
            return found, False
        if (message.corp_id != self.config.corp_id or message.agent_id != self.config.agent_id
                or not is_employee_id(message.user_id) or not message.content.strip()
                or not re.fullmatch(r"[0-9]{1,32}", message.msg_id) or message.create_time < 0):
            raise storage_error("invalid_message")
        identity = employee_identity(self.config, message.user_id)
        busy = self._writer.execute(
            f"SELECT 1 FROM gateway_messages WHERE conversation_id = ? AND {ACTIVE}",
            (identity.conversation_id,),
        ).fetchone() is not None
        if self._writer.execute(f"SELECT count(*) FROM gateway_messages WHERE {UNFINISHED}").fetchone()[0] >= self.config.max_unfinished_messages:
            raise storage_error("resource_exhausted")
        if not busy and self._writer.execute(f"SELECT count(*) FROM gateway_messages WHERE {ACTIVE}").fetchone()[0] >= self.config.max_active_requests:
            raise storage_error("active_capacity")
        now = _timestamp()
        values = {
            "message_key": key, "corp_id": message.corp_id, "agent_id": message.agent_id,
            "user_id": message.user_id, "send_user": message.user_id.casefold(),
            "conversation_id": identity.conversation_id, "msg_id": message.msg_id,
            "create_time": str(message.create_time), "content": message.content,
            "environment": self.config.gateway_environment, "received_at": now, "updated_at": now,
            "adp_state": "REJECTED_USER_BUSY" if busy else "RECEIVED",
            "admission_decision": "USER_BUSY" if busy else "ACCEPTED",
            "send_state": "PENDING" if busy else "NONE", "notification_kind": "USER_BUSY" if busy else None,
            "notification": BUSY_NOTICE if busy else None,
        }
        columns = ", ".join(values)
        placeholders = ", ".join(":" + column for column in values)
        row = self._writer.execute(
            f"INSERT INTO gateway_messages ({columns}) VALUES ({placeholders}) RETURNING *", values
        ).fetchone()
        return _record(row), True

    @staticmethod
    def _get(connection, key: str) -> dict | None:
        """按去重索引读取单条持久事实。"""
        return _record(connection.execute("SELECT * FROM gateway_messages WHERE message_key = ?", (key,)).fetchone())

    def get(self, key: str) -> dict | None:
        """同步读取已提交版本，供现有离线检查及隔离测试使用。"""
        return self._submit_read(self._get, key).result()

    async def get_async(self, key: str) -> dict | None:
        """取消和恢复路径读取已提交版本，不阻塞网关事件循环。"""
        return await asyncio.wrap_future(self._submit_read(self._get, key))

    def _update(self, key, expected_version, changes):
        """提交条件包含原版本与双状态，缓存中的旧任务不能重复取得网络执行权。"""
        previous = self._get(self._writer, key)
        if previous is None or previous["record_version"] != expected_version or not set(changes) <= UPDATE_FIELDS:
            raise storage_error("version_conflict")
        record = transition(previous, changes, _timestamp())
        values = {"record_version": record["record_version"], "updated_at": record["updated_at"]}
        for field, value in changes.items():
            if field in _JSON_FIELDS:
                values[f"{field}_json"] = json.dumps(value, ensure_ascii=False) if value is not None else None
            else:
                values[field] = value
        assignments = ", ".join(f"{column} = ?" for column in values)
        updated = self._writer.execute(
            f"""UPDATE gateway_messages SET {assignments}
                WHERE message_key = ? AND record_version = ? AND adp_state = ? AND send_state = ?""",
            (*values.values(), key, expected_version, previous["adp_state"], previous["send_state"]),
        )
        if updated.rowcount != 1:
            raise storage_error("version_conflict")
        return record

    def update(self, key: str, *, expected_version: int, **changes) -> dict:
        """离线检查与状态回归复用同一版本事务。"""
        return self._enqueue_write(self._update, (key, expected_version, changes)).result()

    @staticmethod
    def _after_update(callback, completed) -> None:
        """可靠提交后的诊断不随请求协程取消，也不在异常事务后报告成功。"""
        if not completed.cancelled() and completed.exception() is None:
            callback(completed.result())

    async def update_async(self, key: str, *, expected_version: int, on_commit=None, **changes) -> dict:
        """只重试未取得写锁的本地状态事务，外部请求仍最多执行一次。"""
        for attempt in range(3):
            try:
                return await self._update_once(key, expected_version, on_commit, changes)
            except ChatError as exc:
                if exc.code != "storage_busy" or attempt == 2:
                    raise
                await asyncio.sleep(0.1)

    async def _update_once(self, key, expected_version, on_commit, changes):
        """取消网络任务前等待已排入的事务结束，随后才能判定是否需要标为未知。"""
        future = self._enqueue_write(self._update, (key, expected_version, changes))
        if on_commit is not None:
            future.add_done_callback(partial(self._after_update, on_commit))
        pending = asyncio.wrap_future(future)
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            try:
                await pending
            except ChatError:
                # 保留取消语义，让调用方按持久事实判断或交给重启恢复。
                pass
            raise

    @staticmethod
    def _by_state(reader, column, state, limit, after):
        """按状态索引及单调接收序号取页，仅允许调用处给出的内部列名。"""
        return [_record(row) for row in reader.execute(
            f"SELECT * FROM gateway_messages WHERE {column} = ? AND id > ? ORDER BY id LIMIT ?",
            (state, after, limit),
        )]

    async def received(self, limit: int, *, after=0) -> list[dict]:
        """读取新 ADP 意图，历史记录不进入调度查询。"""
        return await asyncio.wrap_future(self._submit_read(self._by_state, "adp_state", "RECEIVED", limit, after))

    async def recovery_page(self, column, state, *, after=0, limit=64) -> list[dict]:
        """只分页读取指定恢复状态，避免将全部持久记录加载进内存。"""
        return await asyncio.wrap_future(self._submit_read(self._by_state, column, state, limit, after))

    @staticmethod
    def _pending_sends(reader, limit, sending_users):
        """每位员工只取最早可发送通知，后续通知等前次可靠提交后再竞争执行位。"""
        exclusions = ""
        if sending_users:
            exclusions = f"AND m.send_user NOT IN ({', '.join('?' for _ in sending_users)})"
        return [_record(row) for row in reader.execute(
            f"""SELECT m.* FROM gateway_messages AS m
                WHERE m.send_state = 'PENDING' {exclusions}
                AND NOT EXISTS (
                    SELECT 1 FROM gateway_messages AS other
                    WHERE other.send_user = m.send_user AND {SEND_OPEN}
                      AND (other.id < m.id OR other.send_state = 'SEND_ARMED')
                )
                ORDER BY m.id LIMIT ?""",
            (*sending_users, limit),
        )]

    async def pending_sends(self, limit: int, sending_users: tuple[str, ...]) -> list[dict]:
        """按发送索引选择能立即执行的通知，不为同一员工积累等待 Task。"""
        return await asyncio.wrap_future(self._submit_read(self._pending_sends, limit, sending_users))

    @staticmethod
    def _unfinished(reader):
        """兼容离线检查对未完成和待核查记录的接收顺序。"""
        return [_record(row) for row in reader.execute(
            f"""SELECT * FROM gateway_messages WHERE {UNFINISHED}
                OR adp_state = 'ADP_UNKNOWN' OR send_state = 'SEND_UNKNOWN' ORDER BY id"""
        )]

    def list_unfinished(self) -> list[dict]:
        """只供离线检查定位未完结证据，不用于生产调度。"""
        return self._submit_read(self._unfinished).result()

    def _purge(self, now):
        """按索引有界脱敏和删除，未知及待发消息保留，历史去重仍按原窗口。"""
        now = now or datetime.now(_BEIJING)
        if now.utcoffset() is None:
            raise storage_error("invalid_time")
        week, month = _timestamp(now - timedelta(days=7)), _timestamp(now - timedelta(days=30))
        with self._transaction():
            removed = self._writer.execute(
                f"""DELETE FROM gateway_messages WHERE id IN (
                    SELECT id FROM gateway_messages WHERE {RETENTION} AND received_at <= ?
                      AND (send_state = 'SEND_FAILED' OR updated_at <= ? OR redacted = 1)
                    ORDER BY received_at, id LIMIT ?)""",
                (month, week, _MAINTENANCE_BATCH),
            ).rowcount
            redacted = self._writer.execute(
                f"""UPDATE gateway_messages
                    SET redacted = 1, content = NULL, notification = NULL, adp_result_json = NULL,
                        record_version = record_version + 1
                    WHERE id IN (
                        SELECT id FROM gateway_messages WHERE {RETENTION} AND redacted = 0
                          AND updated_at <= ? AND send_state IN ('SEND_ACCEPTED', 'SEND_SIMULATED')
                        ORDER BY updated_at, id LIMIT ?)""",
                (week, _MAINTENANCE_BATCH),
            ).rowcount
        return removed, redacted

    def purge(self, *, now=None) -> int:
        """为同步维护测试返回删除数，保持与既有调用约定一致。"""
        return self._submit_write(self._purge, now).result()[0]

    async def purge_async(self) -> tuple[int, bool]:
        """一次维护最多处理固定行数，向 Worker 报告是否还有待清理批次。"""
        removed, redacted = await asyncio.wrap_future(self._submit_write(self._purge, None))
        return removed, max(removed, redacted) == _MAINTENANCE_BATCH

    def _checkpoint(self):
        """在唯一写入连接低频观察 checkpoint，不阻塞处于读取中的连接。"""
        start = perf_counter()
        busy, pages, copied = self._writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        return {"wal_checkpoint_ms": round((perf_counter() - start) * 1000, 3),
                "wal_checkpoint_busy": busy, "wal_pages": pages, "wal_checkpointed_pages": copied}

    async def checkpoint(self) -> dict:
        """把诊断 checkpoint 排到同一 writer，保留 SQLite 自带自动 checkpoint。"""
        return await asyncio.wrap_future(self._submit_write(self._checkpoint))

    def close(self) -> None:
        """停止新请求，结束已提交执行的操作后关闭连接并释放进程锁。"""
        with self._guard:
            if self._closed:
                return
            self._closed = True
        self._batch_idle.wait()
        for pool, conn in ((self._reads, self._reader), (self._writes, self._writer)):
            if conn is not None:
                pool.submit(conn.close).result()
            pool.shutdown()
        if self._lease is not None:
            self._lease.close()

    def __enter__(self):
        """供离线使用显式持有唯一写入所有权。"""
        return self

    def __exit__(self, *_args):
        """操作失败同样释放所有权，未提交的记录不会被误认为已接收。"""
        self.close()
