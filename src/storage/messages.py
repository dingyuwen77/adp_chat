"""单实例消息快照：JSON 是事实源，内存占用仅由可靠提交重建。"""

import json
import os
import re
import tempfile
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock
from uuid import UUID

from core.config import ChatConfig
from core.identity import employee_identity
from core.models import ChatError
from wecom.models import InboundMessage


ACTIVE_ADP = {"RECEIVED", "ADP_SEND_ARMED"}
FINAL_ADP = {"ADP_SUCCEEDED", "ADP_REJECTED", "ADP_UNKNOWN", "REJECTED_USER_BUSY"}
FINAL_SEND = {"SEND_ACCEPTED", "SEND_SIMULATED", "SEND_FAILED", "SEND_UNKNOWN"}
_V2_FIELDS = {"record_version", "adp_result", "adp_error", "redacted", "updated_at"}


def message_key(corp_id: str, agent_id: int, msg_id: str) -> str:
    """原始 MsgId 不作路径，企业、应用和消息共同确定唯一文件键。"""
    encoded = json.dumps([corp_id, agent_id, msg_id], ensure_ascii=False, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def _storage_error(code="unavailable") -> ChatError:
    """隐藏操作系统路径/消息正文，只返回可定位的存储分类。"""
    return ChatError("gateway_storage", code, "网关消息状态不可用；保留现场并检查目录、容量或快照。")


def _unique_object(pairs):
    """恢复时拒绝重复 JSON 字段，避免不明确的状态解释。"""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


@contextmanager
def _snapshot_reader(path: Path):
    """Windows显式允许替换已打开快照；持有读句柄仍只看到完整旧版本。"""
    if os.name != "nt":
        with path.open("r", encoding="utf-8") as source:
            yield source
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    create.restype = wintypes.HANDLE
    handle = create(str(path.absolute()), 0x80000000, 7, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, "r", encoding="utf-8") as source:
        yield source


def read_snapshot(path: str | Path) -> dict:
    """供恢复/只读检查共用的有界原子快照读取，不阻止Windows发布新版本。"""
    with _snapshot_reader(Path(path)) as source:
        text = source.read(4_194_305)
    if len(text.encode("utf-8")) > 4_194_304:
        raise ValueError("snapshot too large")
    return json.loads(text, object_pairs_hook=_unique_object)


def _publish(temporary: Path, target: Path) -> None:
    """Windows更新用ReplaceFileW配合共享读取，避免MoveFileEx拒绝已打开目标。"""
    if os.name != "nt" or not target.exists():
        os.replace(temporary, target)
        return
    import ctypes
    from ctypes import wintypes

    replace_file = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    replace_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                            wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p)
    replace_file.restype = wintypes.BOOL
    # flags=0保留目标ACL；失败不回退为覆盖式写入或非原子复制。
    if not replace_file(str(target.absolute()), str(temporary.absolute()), None, 0, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


class MessageStore:
    """目录独占、单写者临界区和同目录原子替换组成接收事务。"""

    def __init__(self, config: ChatConfig):
        """在服务接收消息前校验所有快照，失败不当作空目录继续。"""
        self.config = config
        self.directory = Path(config.messages_dir)
        self._lock = RLock()
        self._handle = None
        self._healthy = False
        self._messages = {}
        self._occupied = {}
        self._sequence = 0
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self._handle = (self.directory / ".gateway.lock").open("a+b")
            self._handle.seek(0, os.SEEK_END)
            if self._handle.tell() == 0:
                self._handle.write(b"\0")
                self._handle.flush()
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            sequences = set()
            for path in self.directory.glob("*.json"):
                if path.is_symlink() or path.stat().st_size > 4_194_304:
                    raise ValueError("invalid snapshot")
                record = read_snapshot(path)
                self._validate(record, path.stem)
                if record["receive_sequence"] in sequences:
                    raise ValueError("duplicate sequence")
                sequences.add(record["receive_sequence"])
                self._messages[path.stem] = record
                self._sequence = max(self._sequence, record["receive_sequence"])
                if record["adp_state"] in ACTIVE_ADP:
                    conversation = record["conversation_id"]
                    if conversation in self._occupied:
                        raise ValueError("conflicting occupation")
                    self._occupied[conversation] = path.stem
            if len(self._messages) > config.max_unfinished_messages:
                raise ValueError("resource limit")
            for temporary in self.directory.glob(".message-*.tmp"):
                match = re.match(r"\.message-([0-9a-f]{64})-", temporary.name)
                # 原快照缺失时不能忽略剩余暂存文件，否则可能把已出站消息当作从未接收。
                if temporary.is_symlink() or match is None or match[1] not in self._messages:
                    raise ValueError("unresolved publication")
                temporary.unlink()
            # 全部历史先校验，再逐条原子升级；中途故障可从v1/v2混合目录安全重开。
            for key, record in list(self._messages.items()):
                if record["schema_version"] == 1:
                    upgraded = {**record, "schema_version": 2, "record_version": 1,
                                "adp_result": None, "adp_error": None, "redacted": False,
                                "updated_at": record["received_at"]}
                    self._validate(upgraded, key)
                    self._write(upgraded)
                    self._messages[key] = upgraded
            self._healthy = True
        except (OSError, ValueError, TypeError, KeyError, ChatError):
            self.close()
            raise _storage_error("initialization_failed") from None

    def _validate_v1(self, record: object, key: str) -> None:
        """恢复边界核对版本、身份、关键类型和合法接收状态。"""
        if not isinstance(record, dict) or type(record.get("schema_version")) is not int or record["schema_version"] != 1:
            raise ValueError("schema")
        required = {"schema_version", "message_key", "corp_id", "agent_id", "user_id", "msg_id", "content",
                    "create_time", "receive_sequence", "received_at", "conversation_id", "adp_state",
                    "request_id", "send_state", "notification", "admission_decision", "notification_kind"}
        if set(record) != required or record["request_id"] is not None:
            raise ValueError("fields")
        for field in ("corp_id", "user_id", "msg_id", "content", "conversation_id", "received_at"):
            if not isinstance(record.get(field), str) or not record[field]:
                raise ValueError("field")
        if (type(record.get("agent_id")) is not int or record["corp_id"] != self.config.corp_id
                or record["agent_id"] != self.config.agent_id
                or not re.fullmatch(r"[A-Za-z0-9_@.\-]{1,64}", record["user_id"])
                or not record["content"].strip()
                or not re.fullmatch(r"[0-9]{1,32}", record["msg_id"])
                or type(record.get("create_time")) is not int or record["create_time"] < 0
                or type(record.get("receive_sequence")) is not int or record["receive_sequence"] < 1
                or record.get("message_key") != key
                or key != message_key(record["corp_id"], record["agent_id"], record["msg_id"])
                or record["conversation_id"] != employee_identity(self.config, record["user_id"]).conversation_id):
            raise ValueError("identity")
        received = datetime.fromisoformat(record["received_at"])
        if received.utcoffset() != timedelta(hours=8):
            raise ValueError("timezone")
        if record.get("adp_state") == "RECEIVED":
            if (record.get("send_state") != "NONE" or record.get("notification") is not None
                    or record["admission_decision"] != "ACCEPTED" or record["notification_kind"] is not None):
                raise ValueError("state")
        elif record.get("adp_state") == "REJECTED_USER_BUSY":
            if (record.get("send_state") != "PENDING" or not isinstance(record.get("notification"), str)
                    or not record["notification"] or record["admission_decision"] != "USER_BUSY"
                    or record["notification_kind"] != "USER_BUSY"):
                raise ValueError("notification")
        else:
            raise ValueError("adp state")

    def _validate(self, record: object, key: str) -> None:
        """v1兼容读取；v2按ADP和发送两个维度严格恢复，不接受歧义组合。"""
        if not isinstance(record, dict) or type(record.get("schema_version")) is not int:
            raise ValueError("schema")
        if record["schema_version"] == 1:
            return self._validate_v1(record, key)
        if record["schema_version"] != 2 or not _V2_FIELDS <= set(record):
            raise ValueError("schema")
        base = {k: v for k, v in record.items() if k not in _V2_FIELDS}
        base.update(schema_version=1, adp_state="RECEIVED", admission_decision="ACCEPTED",
                    request_id=None, send_state="NONE", notification=None, notification_kind=None)
        if record["redacted"] is True:
            base["content"] = "已按保留策略脱敏"
        self._validate_v1(base, key)
        if type(record["record_version"]) is not int or record["record_version"] < 1 or type(record["redacted"]) is not bool:
            raise ValueError("version")
        if not isinstance(record["updated_at"], str) or datetime.fromisoformat(record["updated_at"]).utcoffset() != timedelta(hours=8):
            raise ValueError("timezone")
        state, send = record["adp_state"], record["send_state"]
        if state not in ACTIVE_ADP | FINAL_ADP:
            raise ValueError("state")
        busy = state == "REJECTED_USER_BUSY"
        if record["admission_decision"] != ("USER_BUSY" if busy else "ACCEPTED"):
            raise ValueError("admission")
        if state == "RECEIVED" or busy:
            if record["request_id"] is not None:
                raise ValueError("request")
        else:
            if not isinstance(record["request_id"], str) or str(UUID(record["request_id"])) != record["request_id"]:
                raise ValueError("request")
        if state in ACTIVE_ADP:
            if (send != "NONE" or record["notification"] is not None or record["notification_kind"] is not None
                    or record["adp_result"] is not None or record["adp_error"] is not None or record["redacted"]):
                raise ValueError("active")
        else:
            if send not in {"PENDING", "SEND_ARMED"} | FINAL_SEND or record["notification_kind"] != ("USER_BUSY" if busy else state):
                raise ValueError("send")
            if not record["redacted"] and (not isinstance(record["notification"], str) or not record["notification"]):
                raise ValueError("notification")
            if not record["redacted"]:
                if state == "ADP_SUCCEEDED" and not isinstance(record["adp_result"], dict):
                    raise ValueError("result")
                if state in {"ADP_REJECTED", "ADP_UNKNOWN"} and not isinstance(record["adp_error"], dict):
                    raise ValueError("error")
        if record["redacted"] and (record["content"] is not None or record["notification"] is not None
                or record["adp_result"] is not None or state == "ADP_UNKNOWN" or send != "SEND_ACCEPTED"):
            raise ValueError("redaction")
        if busy and (record["adp_result"] is not None or record["adp_error"] is not None):
            raise ValueError("busy result")
        result = record["adp_result"]
        if result is not None:
            if (not isinstance(result, dict) or set(result) != {"text", "conversation_id", "request_id", "record_id", "reply_method", "reply_contents"}
                    or not isinstance(result["text"], str) or result["conversation_id"] != record["conversation_id"]
                    or result["request_id"] != record["request_id"]
                    or (result["reply_method"] is not None and type(result["reply_method"]) is not int)
                    or not isinstance(result["reply_contents"], (list, tuple))):
                raise ValueError("result")
        error = record["adp_error"]
        if error is not None and (not isinstance(error, dict) or not isinstance(error.get("code"), str)
                or not isinstance(error.get("provider_code"), (str, type(None)))):
            raise ValueError("error")

    def _write(self, record: dict) -> None:
        """可靠发布单快照；任何提交异常关闭接收，避免磁盘与索引分裂。"""
        temporary = None
        target = self.directory / f"{record['message_key']}.json"
        was_present = target.exists()
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=f".message-{record['message_key']}-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump(record, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            _publish(temporary, target)
            # POSIX 另同步目录元数据；Windows 的普通目录不支持 os.fsync。
            if os.name != "nt":
                descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except (OSError, ValueError):
            self._healthy = False
            if was_present and not target.exists():
                # ReplaceFileW的1176/1177可能已移动旧文件；保留唯一完整证据，重启失败关闭待核查。
                raise _storage_error("recovery_required") from None
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    raise _storage_error("cleanup_failed") from None
            raise _storage_error() from None

    def put_if_absent(self, message: InboundMessage) -> tuple[dict, bool]:
        """同一临界区先去重，再决定占用、保存通知意图，成功发布后更新索引。"""
        with self._lock:
            if not self._healthy:
                raise _storage_error()
            key = message_key(message.corp_id, message.agent_id, message.msg_id)
            if key in self._messages:
                return deepcopy(self._messages[key]), False
            if len(self._messages) >= self.config.max_unfinished_messages:
                raise _storage_error("resource_exhausted")
            identity = employee_identity(self.config, message.user_id)
            busy = identity.conversation_id in self._occupied
            record = {
                "schema_version": 2, "record_version": 1, "adp_result": None, "adp_error": None,
                "redacted": False, "message_key": key, **asdict(message),
                "receive_sequence": self._sequence + 1,
                "received_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="milliseconds"),
                "conversation_id": identity.conversation_id,
                "adp_state": "REJECTED_USER_BUSY" if busy else "RECEIVED",
                "admission_decision": "USER_BUSY" if busy else "ACCEPTED",
                "request_id": None, "send_state": "PENDING" if busy else "NONE",
                "notification_kind": "USER_BUSY" if busy else None,
                "notification": "上一条消息仍在处理中，请等待本次回答完成后重新发送。" if busy else None,
            }
            record["updated_at"] = record["received_at"]
            self._validate(record, key)
            self._write(record)
            try:
                self._messages[key] = record
                self._sequence = record["receive_sequence"]
                if not busy:
                    self._occupied[identity.conversation_id] = key
            except BaseException:
                self._healthy = False
                raise
            return deepcopy(record), True

    def get(self, key: str) -> dict | None:
        """读取隔离副本，外部调用方不能绕过可靠提交修改索引。"""
        with self._lock:
            value = self._messages.get(key)
            return deepcopy(value) if value is not None else None

    def update(self, key: str, *, expected_version: int, **changes) -> dict:
        """带版本比较的合法转换，发布结果/通知与释放占用是一个提交。"""
        with self._lock:
            if not self._healthy:
                raise _storage_error()
            previous = self._messages.get(key)
            allowed_fields = {"adp_state", "request_id", "adp_result", "adp_error", "notification",
                              "notification_kind", "send_state"}
            if previous is None or previous["record_version"] != expected_version or not set(changes) <= allowed_fields:
                raise _storage_error("version_conflict")
            record = {**previous, **deepcopy(changes), "record_version": expected_version + 1,
                      "updated_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="milliseconds")}
            before, after = previous["adp_state"], record["adp_state"]
            allowed = before == after or (before == "RECEIVED" and after == "ADP_SEND_ARMED") or (before == "ADP_SEND_ARMED" and after in FINAL_ADP - {"REJECTED_USER_BUSY"})
            sends = {"NONE": {"NONE", "PENDING"}, "PENDING": {"SEND_ARMED", "SEND_SIMULATED"},
                     "SEND_ARMED": {"SEND_ACCEPTED", "SEND_FAILED", "SEND_UNKNOWN"}}
            if (not allowed or (before == after and before in FINAL_ADP and set(changes) - {"send_state"})
                    or (previous["request_id"] is not None and record["request_id"] != previous["request_id"])
                    or (record["send_state"] != previous["send_state"] and record["send_state"] not in sends.get(previous["send_state"], set()))):
                raise _storage_error("invalid_transition")
            try:
                self._validate(record, key)
            except (ValueError, TypeError, KeyError):
                raise _storage_error("invalid_transition") from None
            self._write(record)
            self._messages[key] = record
            if before in ACTIVE_ADP and after in FINAL_ADP:
                self._occupied.pop(record["conversation_id"], None)
            return deepcopy(record)

    def list_unfinished(self) -> list[dict]:
        """返回恢复/核查所需记录；UNKNOWN保留证据但不会占用用户。"""
        with self._lock:
            if not self._healthy:
                raise _storage_error()
            return [deepcopy(r) for r in self._messages.values()
                    if r["adp_state"] in ACTIVE_ADP or r["send_state"] not in FINAL_SEND
                    or r["adp_state"] == "ADP_UNKNOWN" or r["send_state"] == "SEND_UNKNOWN"]

    def purge(self, *, now=None) -> int:
        """只处理可证明闭合的状态；7天脱敏已接受正文，30天后删去重记录。"""
        now = now or datetime.now(timezone(timedelta(hours=8)))
        removed = 0
        with self._lock:
            if not self._healthy or now.utcoffset() is None:
                raise _storage_error()
            for key, old in list(self._messages.items()):
                if (old["adp_state"] in ACTIVE_ADP | {"ADP_UNKNOWN"}
                        or old["send_state"] not in {"SEND_ACCEPTED", "SEND_FAILED"}):
                    continue
                age = now - datetime.fromisoformat(old["received_at"])
                accepted_age = now - datetime.fromisoformat(old["updated_at"])
                if age >= timedelta(days=30) and (old["send_state"] == "SEND_FAILED" or accepted_age >= timedelta(days=7) or old["redacted"]):
                    try:
                        (self.directory / f"{key}.json").unlink()
                    except OSError:
                        self._healthy = False
                        raise _storage_error() from None
                    del self._messages[key]
                    removed += 1
                elif accepted_age >= timedelta(days=7) and old["send_state"] == "SEND_ACCEPTED" and not old["redacted"]:
                    record = {**old, "content": None, "notification": None, "adp_result": None,
                              "redacted": True, "record_version": old["record_version"] + 1}
                    self._validate(record, key)
                    self._write(record)
                    self._messages[key] = record
        return removed

    def close(self) -> None:
        """关闭接收并释放 OS 目录锁，重复关闭安全。"""
        # 取消 await to_thread 不会停止磁盘线程；释放目录前必须等待当前提交结束。
        with self._lock:
            self._healthy = False
            if self._handle is not None:
                self._handle.close()
                self._handle = None

    def __enter__(self):
        """将目录独占资源交给明确的上下文所有者。"""
        return self

    def __exit__(self, *_args):
        """无论调用结果如何都释放本实例资源。"""
        self.close()
