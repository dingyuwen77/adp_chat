"""单实例后台转发：获准消息首次执行，armed恢复一律未知，不按ADP配额排队。"""

import asyncio
from logging import DEBUG
from time import perf_counter
from dataclasses import asdict
from functools import partial
from uuid import uuid4
from weakref import WeakValueDictionary

from adp.async_client import unknown_outcome
from core.logging import log_adp_answer, log_writer_metrics
from core.models import ChatError
from wecom.sender import SendOutcome, unknown_send


_DISPATCH_BATCH = 64
_MAINTENANCE_SECONDS = 60


class GatewayWorker:
    """按 SQLite 状态索引和当前执行位调度，出站前以版本事务认领。"""

    def __init__(self, gateway, adp, *, sender=None):
        """依赖由lifespan持有，测试与真实出站使用同一状态机制。"""
        self.gateway, self.adp = gateway, adp
        self.store, self.logger = gateway.store, gateway.logger
        self.events = gateway.events
        self.tasks = {}
        self.send_tasks = {}
        self._sending_users = set()
        # 等待者持有强引用；员工无在途通知后自动释放锁，避免长期员工字典增长。
        self._send_locks = WeakValueDictionary()
        self.sender = sender
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
        await self._purge()
        for column, state in (("send_state", "SEND_ARMED"), ("send_state", "PENDING"),
                              ("adp_state", "ADP_SEND_ARMED"), ("adp_state", "RECEIVED")):
            await self._recover(column, state)
        self._runner = asyncio.create_task(self._dispatch(), name="wecom-dispatch")
        self.wake()

    async def _recover(self, column, state):
        """只读需要恢复的状态。已出站证据先转未知，待提交和待发意图仍可首次执行。"""
        after = 0
        while records := await self.store.recovery_page(column, state, after=after):
            for record in records:
                event_type = "adp" if column == "adp_state" else "notification"
                uncertain = state in {"ADP_SEND_ARMED", "SEND_ARMED"}
                action = "mark_unknown" if uncertain else (
                    "first_submission" if state == "RECEIVED" else "first_notification")
                if state == "ADP_SEND_ARMED":
                    await self._finish(record, unknown_outcome("process_recovery"))
                    self.logger.warning("adp_recovered_unknown message_key=%s request_id=%s",
                                        record["message_key"], record["request_id"])
                elif state == "SEND_ARMED":
                    await self._finish_notification(record, unknown_send(code="process_recovery"))
                self.events.emit("message.recovered", record, outcome_uncertain=uncertain,
                                 attributes={"source": "recovery", "event_type": event_type,
                                             "recovery_action": action})
            after = records[-1]["receive_sequence"]

    async def _dispatch(self):
        """及时发现已获准任务，维护时钟独立于回调唤醒频率。"""
        try:
            maintenance_at = self._loop.time() + _MAINTENANCE_SECONDS
            while not self._stopping and self.failure is None:
                try:
                    async with asyncio.timeout_at(maintenance_at):
                        await self._event.wait()
                except TimeoutError:
                    pass
                self._event.clear()
                if self.failure is not None:
                    return
                await self._schedule_adp()
                await self._schedule_notifications()
                if self._loop.time() >= maintenance_at:
                    more = await self._purge()
                    maintenance_at = self._loop.time() + (1 if more else _MAINTENANCE_SECONDS)
                    if self.logger.isEnabledFor(DEBUG) and not more:
                        checkpoint = await self.store.checkpoint()
                        store_stats = await asyncio.to_thread(self.store.metrics)
                        log_stats = await asyncio.to_thread(log_writer_metrics, self.logger)
                        self.logger.debug("网关存储及日志资源", extra={"log_fields": {
                            **store_stats, **log_stats, **checkpoint, "adp_tasks": len(self.tasks),
                            "send_tasks": len(self.send_tasks),
                        }})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = type(exc).__name__
            self.logger.error("gateway_dispatch_failed code=%s", self.failure)

    async def _schedule_adp(self):
        """分页填充剩余执行位，跳过同进程中已经开始认领的记录。"""
        remaining = self.gateway.config.max_active_requests - len(self.tasks)
        cursor = 0
        while remaining > 0 and self.failure is None:
            limit = min(_DISPATCH_BATCH, remaining)
            records = await self.store.received(limit, after=cursor)
            for record in records:
                key = record["message_key"]
                if key not in self.tasks:
                    task = asyncio.create_task(self._execute(record), name=f"adp-{key[:12]}")
                    self.tasks[key] = task
                    task.add_done_callback(partial(self._adp_done, key))
                    remaining -= 1
            if len(records) < limit:
                return
            cursor = records[-1]["receive_sequence"]

    def _adp_done(self, key, _task):
        """归还执行位，使恢复或同时到达的 RECEIVED 记录继续被发现。"""
        self.tasks.pop(key, None)
        self.wake()

    async def _schedule_notifications(self):
        """空闲发送位只接收每位员工的一条通知，避免同用户阻塞其他员工。"""
        remaining = self.gateway.config.max_send_tasks - len(self.send_tasks)
        if not remaining or self.sender is None or self.failure is not None:
            return
        records = await self.store.pending_sends(remaining, tuple(self._sending_users))
        for record in records:
            key, user = record["message_key"], record["user_id"].casefold()
            self._sending_users.add(user)
            task = asyncio.create_task(self._execute_notification(record), name=f"notify-{key[:12]}")
            self.send_tasks[key] = task
            task.add_done_callback(partial(self._notification_done, key, user))

    def _notification_done(self, key, user, _task):
        """通知结束才归还员工发送位，使下一条可按可靠接收顺序出站。"""
        self.send_tasks.pop(key, None)
        self._sending_users.discard(user)
        self.wake()

    async def _purge(self):
        """清理结果仅诊断，失败仍按既有存储Owner失败关闭，不靠日志恢复。"""
        try:
            removed, more = await self.store.purge_async()
        except ChatError as exc:
            self.events.emit("message.cleanup.failed", error_stage="storage", error_code=exc.code)
            if exc.code == "storage_busy":
                return True
            raise
        self.events.emit("message.cleanup.completed", attributes={"removed": removed})
        return more

    async def _finish(self, record, outcome, *, duration_ms=None):
        """处理结果、一次待发送提示、本地释放同一原子提交，发送层后续独立处理。"""
        return await self.store.update_async(record["message_key"],
            expected_version=record["record_version"], adp_state=outcome.state,
            adp_result=asdict(outcome.result) if outcome.result is not None else None,
            adp_error=outcome.error, notification=outcome.notification,
            notification_kind=outcome.state, send_state="PENDING",
            on_commit=partial(self._after_result, outcome, duration_ms))

    def _after_result(self, outcome, duration_ms, saved):
        """以已提交 ADP 结果发出诊断，即便等待者在提交期间取消。"""
        error = outcome.error or {}
        result = outcome.result
        self.events.emit({"ADP_SUCCEEDED": "adp.completed", "ADP_REJECTED": "adp.rejected"}.get(outcome.state, "adp.unknown"),
            saved, duration_ms=duration_ms, error_code=error.get("provider_code"), error_stage=error.get("code"),
            http_status=error.get("http_status"), provider_request_id=error.get("provider_request_id"),
            provider_trace_id=error.get("provider_trace_id"), record_id=result.record_id if result else None,
            outcome_uncertain=outcome.state == "ADP_UNKNOWN",
            attributes={"reply_method": result.reply_method if result else None})
        if outcome.state == "ADP_SUCCEEDED":
            self.events.emit("reply.persisted", saved, attributes={"reply_chars": len(outcome.notification)})
            if self.gateway.config.log_answer:
                log_adp_answer(self.logger, result, conversation_id=saved["conversation_id"],
                               message_key=saved["message_key"], request_id=saved["request_id"],
                               employee_id=saved["user_id"])
        self.events.emit("message.session.released", saved, outcome_uncertain=outcome.state == "ADP_UNKNOWN",
                         attributes={"release_reason": outcome.state})
        self.logger.info("adp_result_saved message_key=%s request_id=%s adp_state=%s local_session_released=true",
                         saved["message_key"], saved["request_id"], saved["adp_state"])
        self.wake()

    async def _finish_notification(self, record, outcome):
        """只提交独立发送维度，绝不修改ADP结果或重新占用员工。"""
        saved = await self.store.update_async(record["message_key"],
            expected_version=record["record_version"], send_state=outcome.state, send_result=asdict(outcome))
        event = {"SEND_ACCEPTED": "wecom.send.accepted", "SEND_SIMULATED": "wecom.send.simulated",
                 "SEND_FAILED": "wecom.send.failed", "SEND_UNKNOWN": "wecom.send.unknown"}[outcome.state]
        self.events.emit(event, saved, error_code=outcome.provider_code, error_stage=outcome.code,
            outcome_uncertain=outcome.state == "SEND_UNKNOWN",
            attributes={"source": outcome.source, "accepted_parts": outcome.accepted_parts, "total_parts": outcome.total_parts})
        self.logger.info("wecom_send_result message_key=%s send_state=%s source=%s code=%s",
                         saved["message_key"], saved["send_state"], outcome.source, outcome.code)
        return saved

    async def _execute_notification(self, record):
        """完整通知按员工互斥；等待时保持 PENDING，不占用 ADP 会话或发送超时。"""
        user = record["user_id"].casefold()
        lock = self._send_locks.get(user)
        if lock is None:
            lock = asyncio.Lock()
            self._send_locks[user] = lock
        async with lock:
            await self._send_notification_locked(record)

    async def _send_notification_locked(self, record):
        """通知武装先落盘，Token/消息网络或取消未知均不调用ADP或派生新通知。"""
        armed = None
        try:
            plan = SendOutcome("SEND_ARMED", self.sender.source, None, None, None, "armed")
            armed = await self.store.update_async(record["message_key"],
                expected_version=record["record_version"], send_state="SEND_ARMED", send_result=asdict(plan))
            self.logger.info("wecom_send_armed message_key=%s source=%s", armed["message_key"], self.sender.source)
            self.events.emit("wecom.send.started", armed, attempt=1, attributes={"source": self.sender.source})
            outcome = await self.sender.send(armed)
            await self._finish_notification(armed, outcome)
        except asyncio.CancelledError:
            try:
                current = await self.store.get_async(record["message_key"])
                if current is not None and current["send_state"] == "SEND_ARMED":
                    await self._finish_notification(current, unknown_send(code="shutdown"))
            except ChatError:
                self.failure = "storage_unavailable"
            raise
        except ChatError as exc:
            if armed is None and exc.code == "version_conflict":
                self.wake()
                return
            self.failure = exc.code
            self.logger.error("notification_state_failed message_key=%s code=%s", record["message_key"], exc.code)
        except Exception:
            if armed is not None:
                try:
                    await self._finish_notification(armed, unknown_send(code="unexpected_error"))
                except ChatError:
                    self.failure = "storage_unavailable"
            else:
                self.failure = "unexpected_error"

    async def _execute(self, record):
        """武装先落盘，取消/异常也保存UNKNOWN；任何路径都不再次调用ADP。"""
        armed = None
        try:
            armed = await self.store.update_async(record["message_key"],
                expected_version=record["record_version"], adp_state="ADP_SEND_ARMED", request_id=str(uuid4()))
            self.logger.info("adp_send_armed message_key=%s request_id=%s", armed["message_key"], armed["request_id"])
            self.events.emit("adp.submit_armed", armed, attempt=1)
            self.events.emit("adp.started", armed, attempt=1)
            started = perf_counter()
            outcome = await self.adp.request(armed)
            await self._finish(armed, outcome, duration_ms=round((perf_counter() - started) * 1000, 3))
        except asyncio.CancelledError:
            # 异步写入已等待事务结束。读回落盘状态，再决定是否仍需 UNKNOWN。
            try:
                current = await self.store.get_async(record["message_key"])
                if current is not None and current["adp_state"] == "ADP_SEND_ARMED":
                    await self._finish(current, unknown_outcome("shutdown"))
            except ChatError:
                self.failure = "storage_unavailable"
            raise
        except ChatError as exc:
            if armed is None and exc.code == "version_conflict":
                current = await self.store.get_async(record["message_key"])
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
        tasks = list(self.tasks.values()) + list(self.send_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
