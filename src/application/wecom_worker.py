"""单实例后台转发：获准消息首次执行，armed恢复一律未知，不按ADP配额排队。"""

import asyncio
from dataclasses import asdict
from uuid import uuid4

from adp.async_client import unknown_outcome
from core.models import ChatError


class GatewayWorker:
    """任务注册表只去重进程内调度；唯一事实仍是MessageStore快照。"""

    def __init__(self, gateway, adp):
        """依赖由lifespan持有，测试与真实出站使用同一状态机制。"""
        self.gateway, self.adp = gateway, adp
        self.store, self.logger = gateway.store, gateway.logger
        self.tasks = {}
        self.failure = None
        self._event = asyncio.Event()
        self._loop = None
        self._runner = None
        self._stopping = False
        gateway.worker = self

    def wake(self):
        """接收可能来自磁盘线程，以事件唤醒，不复制消息或建立业务等待队列。"""
        if self._loop is not None and not self._stopping:
            self._loop.call_soon_threadsafe(self._event.set)

    async def start(self):
        """先可靠解决上进程armed记录，之后才允许未武装消息首次执行。"""
        self._loop = asyncio.get_running_loop()
        await asyncio.to_thread(self.store.purge)
        records = await asyncio.to_thread(self.store.list_unfinished)
        for record in records:
            if record["adp_state"] == "ADP_SEND_ARMED":
                await self._finish(record, unknown_outcome("process_recovery"))
                self.logger.warning("adp_recovered_unknown message_key=%s request_id=%s",
                                    record["message_key"], record["request_id"])
        self._runner = asyncio.create_task(self._dispatch(), name="wecom-dispatch")
        self.wake()

    async def _dispatch(self):
        """从可靠快照发现首次工作；每条任务立即异步发起，不加模型并发信号量。"""
        try:
            while not self._stopping:
                try:
                    await asyncio.wait_for(self._event.wait(), timeout=60)
                except TimeoutError:
                    await asyncio.to_thread(self.store.purge)
                self._event.clear()
                records = await asyncio.to_thread(self.store.list_unfinished)
                for record in records:
                    key = record["message_key"]
                    if record["adp_state"] == "RECEIVED" and key not in self.tasks:
                        self.tasks[key] = asyncio.create_task(self._execute(record), name=f"adp-{key[:12]}")
                        self.tasks[key].add_done_callback(lambda _done, key=key: self.tasks.pop(key, None))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = type(exc).__name__
            self.logger.error("gateway_dispatch_failed code=%s", self.failure)

    async def _finish(self, record, outcome):
        """处理结果、一次待发送提示、本地释放同一原子提交，发送层后续独立处理。"""
        saved = await asyncio.to_thread(self.store.update, record["message_key"],
            expected_version=record["record_version"], adp_state=outcome.state,
            adp_result=asdict(outcome.result) if outcome.result is not None else None,
            adp_error=outcome.error, notification=outcome.notification,
            notification_kind=outcome.state, send_state="PENDING")
        self.logger.info("adp_result_saved message_key=%s request_id=%s adp_state=%s local_session_released=true",
                         saved["message_key"], saved["request_id"], saved["adp_state"])
        return saved

    async def _execute(self, record):
        """武装先落盘，取消/异常也保存UNKNOWN；任何路径都不再次调用ADP。"""
        armed = None
        try:
            armed = await asyncio.to_thread(self.store.update, record["message_key"],
                expected_version=record["record_version"], adp_state="ADP_SEND_ARMED", request_id=str(uuid4()))
            self.logger.info("adp_send_armed message_key=%s request_id=%s", armed["message_key"], armed["request_id"])
            outcome = await self.adp.request(armed)
            await self._finish(armed, outcome)
        except asyncio.CancelledError:
            # to_thread取消不会停止提交；先等同一锁读回落盘结果，再决定是否仍需UNKNOWN。
            try:
                current = await asyncio.to_thread(self.store.get, record["message_key"])
                if current is not None and current["adp_state"] == "ADP_SEND_ARMED":
                    await self._finish(current, unknown_outcome("shutdown"))
            except ChatError:
                self.failure = "storage_unavailable"
            raise
        except ChatError as exc:
            if armed is None and exc.code == "version_conflict":
                current = await asyncio.to_thread(self.store.get, record["message_key"])
                if current is not None and current["adp_state"] != "RECEIVED":
                    return
            self.failure = exc.code
            self.logger.error("gateway_state_failed message_key=%s code=%s", record["message_key"], exc.code)
        except Exception:
            if armed is not None:
                try:
                    await self._finish(armed, unknown_outcome("unexpected_error"))
                except ChatError:
                    self.failure = "storage_unavailable"
            else:
                self.failure = "unexpected_error"

    async def stop(self):
        """停止新调度，取消本地网络等待并保存UNKNOWN，再释放连接和目录。"""
        self._stopping = True
        if self._runner is not None:
            self._runner.cancel()
            await asyncio.gather(self._runner, return_exceptions=True)
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
