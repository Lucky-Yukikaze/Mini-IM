from __future__ import annotations

import time
import re

from protocol.pb import auth_pb2
from services.auth.session_mgr import SessionManager, SessionRecord


class AuthService:
    DEV_TOKEN_PREFIX = "dev-token:"
    USER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

    def __init__(self, session_mgr: SessionManager | None = None) -> None:
        self.m_session_mgr = session_mgr or SessionManager()

    @staticmethod
    def _resolve_user_id(token: str) -> str:
        if token.startswith(AuthService.DEV_TOKEN_PREFIX):
            user_id = token[len(AuthService.DEV_TOKEN_PREFIX) :].strip()
            if AuthService.USER_ID_PATTERN.fullmatch(user_id):
                return user_id
            return "u-demo"
        if token.startswith("dev-token"):
            return "u-demo"
        return "u-demo"

    def handle_hello(self, hello: auth_pb2.Hello) -> auth_pb2.Welcome:
        user_id = self._resolve_user_id(hello.token)
        session, resumed = self.m_session_mgr.create_or_resume(
            user_id=user_id,
            device_id=hello.device_id,
            resume_session_id=hello.resume_session_id,
            global_cursor=hello.global_cursor,
            last_acked_request_id=hello.last_acked_request_id,
        )

        welcome = auth_pb2.Welcome()
        welcome.session_id = session.session_id
        welcome.user_id = user_id
        welcome.server_time_ms = int(time.time() * 1000)
        welcome.heartbeat_interval_sec = 15
        welcome.global_cursor = session.global_cursor
        welcome.resumable = resumed
        welcome.need_reauth = False
        return welcome

    def touch_session(self, session_id: str) -> None:
        self.m_session_mgr.touch(session_id)

    def get_session(self, session_id: str) -> SessionRecord | None:
        return self.m_session_mgr.get(session_id)
