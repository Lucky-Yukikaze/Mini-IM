from __future__ import annotations

import time
import uuid
from dataclasses import dataclass


@dataclass
class SessionRecord:
    session_id: str
    user_id: str
    device_id: str
    global_cursor: int
    last_acked_request_id: str
    updated_at_ms: int


class SessionManager:
    def __init__(self) -> None:
        self.m_sessions: dict[str, SessionRecord] = {}

    def create_or_resume(
        self,
        user_id: str,
        device_id: str,
        resume_session_id: str,
        global_cursor: int,
        last_acked_request_id: str,
    ) -> tuple[SessionRecord, bool]:
        now_ms = int(time.time() * 1000)
        session = self.m_sessions.get(resume_session_id) if resume_session_id else None
        can_resume = bool(
            session
            and session.user_id == user_id
            and session.device_id == device_id
        )

        if can_resume and session is not None:
            session.global_cursor = max(session.global_cursor, int(global_cursor))
            if last_acked_request_id:
                session.last_acked_request_id = last_acked_request_id
            session.updated_at_ms = now_ms
            return session, True

        new_session = SessionRecord(
            session_id=str(uuid.uuid4()),
            user_id=user_id,
            device_id=device_id,
            global_cursor=max(int(global_cursor), 0),
            last_acked_request_id=last_acked_request_id,
            updated_at_ms=now_ms,
        )
        self.m_sessions[new_session.session_id] = new_session
        return new_session, False

    def touch(self, session_id: str) -> None:
        session = self.m_sessions.get(session_id)
        if session is None:
            return
        session.updated_at_ms = int(time.time() * 1000)

    def get(self, session_id: str) -> SessionRecord | None:
        if not session_id:
            return None
        return self.m_sessions.get(session_id)
