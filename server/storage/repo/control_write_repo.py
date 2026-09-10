"""Commit control-write results with their business changes and sync events."""
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import time

from protocol.pb import message_pb2
from storage.repo.sync_event import StoredSyncEvent
from storage.sqlite.db import MiniImSqliteDb


@dataclass
class ControlWriteResult:
    ack: message_pb2.Ack
    sync_events: list[StoredSyncEvent]


class RetryableWrite(Exception):
    def __init__(self, result: ControlWriteResult):
        super().__init__(result.ack.message)
        self.result = result


class ControlWriteRepo:
    def __init__(self, db: MiniImSqliteDb):
        self.m_db = db

    @staticmethod
    def reject(request_id: str, code: int, message: str) -> ControlWriteResult:
        return ControlWriteResult(message_pb2.Ack(
            request_id=request_id, success=False, code=code, message=message,
            server_time_ms=int(time.time() * 1000)), [])

    def execute(
        self, user_id: str, request_id: str, operation: str, payload: bytes,
        apply: Callable[[], ControlWriteResult],
    ) -> ControlWriteResult:
        if not user_id or not request_id.strip():
            return self.reject(request_id, 400, "control write requires user and request id")
        fingerprint = hashlib.sha256(payload).digest()
        try:
            with self.m_db.transaction() as connection:
                previous = connection.execute(
                    "SELECT operation,fingerprint,ack FROM control_write_results WHERE user_id=? AND request_id=?",
                    (user_id, request_id)).fetchone()
                if previous is not None:
                    if previous["operation"] != operation or bytes(previous["fingerprint"]) != fingerprint:
                        return self.reject(request_id, 409, "request id already belongs to a different write")
                    ack = message_pb2.Ack.FromString(bytes(previous["ack"]))
                    return ControlWriteResult(ack, [])
                legacy = connection.execute(
                    "SELECT intent_fingerprint FROM messages WHERE sender_id=? AND request_id=?",
                    (user_id, request_id)).fetchall()
                if legacy and (len(legacy) != 1 or operation != "send_message"
                               or legacy[0]["intent_fingerprint"] is None
                               or bytes(legacy[0]["intent_fingerprint"]) != fingerprint):
                    return self.reject(request_id, 409, "legacy message request identity unavailable or conflicting")
                result = apply()
                if not result.ack.success and (result.ack.code in (401, 408, 429) or result.ack.code >= 500):
                    # Transient failure is retryable; retain neither changes nor an authoritative result.
                    raise RetryableWrite(result)
                connection.execute(
                    "INSERT INTO control_write_results(user_id,request_id,operation,fingerprint,ack,created_at_ms) "
                    "VALUES(?,?,?,?,?,?)", (user_id, request_id, operation, fingerprint,
                                           result.ack.SerializeToString(), int(time.time() * 1000)))
            return result
        except RetryableWrite as failure:
            return ControlWriteResult(failure.result.ack, [])
