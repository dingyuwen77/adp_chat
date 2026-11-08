"""单实例后台转发：获准消息首次执行，armed恢复一律未知，不按ADP配额排队。"""

import asyncio
from time import perf_counter
from dataclasses import asdict
from uuid import uuid4

from adp.async_client import unknown_outcome
from core.models import ChatError
from wecom.sender import SendOutcome, unknown_send


class GatewayWorker:
    """任务注册表只去重进程内调度；唯一事实仍是MessageStore快照。"""

    def __init__(self, gateway, adp, *, sender=None):
        """依赖由lifespan持有，测试与真实出站使用同一状态机制。"""
        self.gateway, self.adp = gateway, adp
        self.store, self.logger = gateway.store, gateway.logger
        self.events = gateway.events
        self.tasks = {}
        self.send_tasks = {}
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
        records = await asyncio.to_thread(self.store.list_unfinished)
        for record in records:
            if record["adp_state"] == "ADP_SEND_ARMED":
                await self._finish(record, unknown_outcome("process_recovery"))
                self.events.emit("message.recovered", record, outcome_uncertain=True,
                    attributes={"source": "recovery", "event_type": "adp", "recovery_action": "mark_unknown"})
                self.logger.warning("adp_recovered_unknown message_key=%s request_id=%s",
                                    record["message_key"], record["request_id"])
            if record["send_state"] == "SEND_ARMED":
                await self._finish_notification(record, unknown_send(code="process_recovery"))
                self.events.emit("message.recovered", record, outcome_uncertain=True,
                    attributes={"source": "recovery", "event_type": "notification", "recovery_action": "mark_unknown"})
            elif record["send_state"] == "PENDING":
                self.events.emit("message.recovered", record,
                    attributes={"source": "recovery", "event_type": "notification", "recovery_action": "first_notification"})
            if record["adp_state"] == "RECEIVED":
                self.events.emit("message.recovered", record,
                    attributes={"source": "recovery", "event_type": "adp", "recovery_action": "first_submission"})
        self._runner = asyncio.create_task(self._dispatch(), name="wecom-dispatch")
        self.wake()

    async def _dispatch(self):
        """从可靠快照发现首次工作；每条任务立即异步发起，不加模型并发信号量。"""
        try:
            while not self._stopping:
                try:
                    await asyncio.wait_for(self._event.wait(), timeout=60)
                except TimeoutError:
                    await self._purge()
                self._event.clear()
                records = await asyncio.to_thread(self.store.list_unfinished)
                for record in records:
                    key = record["message_key"]
                    if record["adp_state"] == "RECEIVED" and key not in self.tasks:
                        self.tasks[key] = asyncio.create_task(self._execute(record), name=f"adp-{key[:12]}")
                        self.tasks[key].add_done_callback(lambda _done, key=key: self.tasks.pop(key, None))
                    if self.sender is not None and record["send_state"] == "PENDING" and key not in self.send_tasks:
                        self.send_tasks[key] = asyncio.create_task(self._execute_notification(record), name=f"notify-{key[:12]}")
                        self.send_tasks[key].add_done_callback(lambda _done, key=key: self.send_tasks.pop(key, None))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = type(exc).__name__
            self.logger.error("gateway_dispatch_failed code=%s", self.failure)

    async def _purge(self):
        """清理结果仅诊断，失败仍按既有存储Owner失败关闭，不靠日志恢复。"""
        try:
            removed = await asyncio.to_thread(self.store.purge)
        except ChatError as exc:
            self.events.emit("message.cleanup.failed", error_stage="storage", error_code=exc.code)
            raise
        self.events.emit("message.cleanup.completed", attributes={"removed": removed})

    async def _finish(self, record, outcome, *, duration_ms=None):
        """处理结果、一次待发送提示、本地释放同一原子提交，发送层后续独立处理。"""
        saved = await asyncio.to_thread(self.store.update, record["message_key"],
            expected_version=record["record_version"], adp_state=outcome.state,
            adp_result=asdict(outcome.result) if outcome.result is not None else None,
            adp_error=outcome.error, notification=outcome.notification,
            notification_kind=outcome.state, send_state="PENDING")
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
        self.events.emit("message.session.released", saved, outcome_uncertain=outcome.state == "ADP_UNKNOWN",
                         attributes={"release_reason": outcome.state})
        self.logger.info("adp_result_saved message_key=%s request_id=%s adp_state=%s local_session_released=true",
                         saved["message_key"], saved["request_id"], saved["adp_state"])
        self.wake()
        return saved

    async def _finish_notification(self, record, outcome):
        """只提交独立发送维度，绝不修改ADP结果或重新占用员工。"""
        saved = await asyncio.to_thread(self.store.update, record["message_key"],
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
        """通知武装先落盘，Token/消息网络或取消未知均不调用ADP或派生新通知。"""
        armed = None
        try:
            plan = SendOutcome("SEND_ARMED", self.sender.source, None, None, None, "armed")
            armed = await asyncio.to_thread(self.store.update, record["message_key"],
                expected_version=record["record_version"], send_state="SEND_ARMED", send_result=asdict(plan))
            self.logger.info("wecom_send_armed message_key=%s source=%s", armed["message_key"], self.sender.source)
            self.events.emit("wecom.send.started", armed, attempt=1, attributes={"source": self.sender.source})
            outcome = await self.sender.send(armed)
            await self._finish_notification(armed, outcome)
        except asyncio.CancelledError:
            try:
                current = await asyncio.to_thread(self.store.get, record["message_key"])
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
            armed = await asyncio.to_thread(self.store.update, record["message_key"],
                expected_version=record["record_version"], adp_state="ADP_SEND_ARMED", request_id=str(uuid4()))
            self.logger.info("adp_send_armed message_key=%s request_id=%s", armed["message_key"], armed["request_id"])
            self.events.emit("adp.submit_armed", armed, attempt=1)
            self.events.emit("adp.started", armed, attempt=1)
            started = perf_counter()
            outcome = await self.adp.request(armed)
            await self._finish(armed, outcome, duration_ms=round((perf_counter() - started) * 1000, 3))
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
        tasks = list(self.tasks.values()) + list(self.send_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
