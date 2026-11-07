"""单实例消息快照：JSON 是事实源，内存占用仅由可靠提交重建。"""

import json
import os
import re
import tempfile
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from threading import RLock

from core.config import ChatConfig
from core.identity import employee_identity
from core.models import ChatError
from wecom.models import InboundMessage


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
                record = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
                self._validate(record, path.stem)
                if record["receive_sequence"] in sequences:
                    raise ValueError("duplicate sequence")
                sequences.add(record["receive_sequence"])
                self._messages[path.stem] = record
                self._sequence = max(self._sequence, record["receive_sequence"])
                if record["adp_state"] == "RECEIVED":
                    conversation = record["conversation_id"]
                    if conversation in self._occupied:
                        raise ValueError("conflicting occupation")
                    self._occupied[conversation] = path.stem
            if len(self._messages) > config.max_unfinished_messages:
                raise ValueError("resource limit")
            self._healthy = True
        except (OSError, ValueError, TypeError, KeyError, ChatError):
            self.close()
            raise _storage_error("initialization_failed") from None

    def _validate(self, record: object, key: str) -> None:
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

    def _write(self, record: dict) -> None:
        """可靠发布单快照；任何提交异常关闭接收，避免磁盘与索引分裂。"""
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.directory,
                                             prefix=".message-", suffix=".tmp", delete=False) as output:
                temporary = Path(output.name)
                json.dump(record, output, ensure_ascii=False, indent=2)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.directory / f"{record['message_key']}.json")
            # POSIX 另同步目录元数据；Windows 的普通目录不支持 os.fsync。
            if os.name != "nt":
                descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        except (OSError, ValueError):
            self._healthy = False
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
                "schema_version": 1, "message_key": key, **asdict(message),
                "receive_sequence": self._sequence + 1,
                "received_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="milliseconds"),
                "conversation_id": identity.conversation_id,
                "adp_state": "REJECTED_USER_BUSY" if busy else "RECEIVED",
                "admission_decision": "USER_BUSY" if busy else "ACCEPTED",
                "request_id": None, "send_state": "PENDING" if busy else "NONE",
                "notification_kind": "USER_BUSY" if busy else None,
                "notification": "上一条消息仍在处理中，请等待本次回答完成后重新发送。" if busy else None,
            }
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
