import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from protocol.pb.auth_pb2 import Hello
from services.auth.service import AuthService


class AuthServiceTest(unittest.TestCase):
    def test_handle_hello_creates_new_session(self):
        service = AuthService()
        hello = Hello(token="dev-token", device_id="d1", global_cursor=7)

        welcome = service.handle_hello(hello)

        self.assertTrue(welcome.session_id)
        self.assertEqual("u-demo", welcome.user_id)
        self.assertEqual(7, welcome.global_cursor)
        self.assertFalse(welcome.resumable)
        self.assertFalse(welcome.need_reauth)

    def test_handle_hello_uses_dev_token_user_id(self):
        service = AuthService()
        hello = Hello(token="dev-token:u-alice", device_id="d1", global_cursor=0)

        welcome = service.handle_hello(hello)

        self.assertEqual("u-alice", welcome.user_id)

    def test_handle_hello_resumes_when_session_matches(self):
        service = AuthService()
        first = service.handle_hello(Hello(token="dev-token", device_id="d1", global_cursor=7))
        resumed = service.handle_hello(
            Hello(
                token="dev-token",
                device_id="d1",
                global_cursor=11,
                resume_session_id=first.session_id,
                last_acked_request_id="req-2",
            )
        )

        self.assertEqual(first.session_id, resumed.session_id)
        self.assertEqual(11, resumed.global_cursor)
        self.assertTrue(resumed.resumable)

    def test_handle_hello_falls_back_when_device_differs(self):
        service = AuthService()
        first = service.handle_hello(Hello(token="dev-token", device_id="d1", global_cursor=7))
        resumed = service.handle_hello(
            Hello(token="dev-token", device_id="d2", global_cursor=9, resume_session_id=first.session_id)
        )

        self.assertNotEqual(first.session_id, resumed.session_id)
        self.assertFalse(resumed.resumable)


if __name__ == "__main__":
    unittest.main()
