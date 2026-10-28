from __future__ import annotations

import importlib.util
import json
import os
import threading
import time
import unittest
import uuid

from ai_debug_display.broker_protocol import (
    PIPE_NAME,
    BrokerReply,
    MAX_MESSAGE_BYTES,
    parse_request,
)
from ai_debug_display.broker_server import (
    AuthenticatedNamedPipeServer,
    BrokerTransportError,
    current_pipe_identity,
)


HAS_WIN32_PIPE = os.name == "nt" and all(
    importlib.util.find_spec(name) is not None
    for name in ("pywintypes", "win32api", "win32con", "win32file", "win32pipe", "win32security")
)


@unittest.skipUnless(HAS_WIN32_PIPE, "Windows broker integration test requires pywin32")
class NamedPipeBrokerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import win32api
        import win32con
        import win32file
        import win32pipe
        cls.win32api = win32api
        cls.win32con = win32con
        cls.win32file = win32file
        cls.win32pipe = win32pipe

    def setUp(self):
        self.user_sid, self.logon_sid = current_pipe_identity()
        self.assertTrue(self.user_sid.startswith("S-"))
        self.assertRegex(self.logon_sid, r"^S-1-5-5-\d+-\d+$")

    def _connect(self, timeout=3.0):
        deadline = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < deadline:
            try:
                return self.win32file.CreateFile(
                    PIPE_NAME,
                    self.win32con.GENERIC_READ | self.win32con.GENERIC_WRITE,
                    0,
                    None,
                    self.win32con.OPEN_EXISTING,
                    0,
                    None,
                )
            except Exception as exc:
                last_error = exc
                time.sleep(0.01)
        self.fail(f"local broker pipe did not accept a client: {getattr(last_error, 'winerror', 'unknown')}")

    @staticmethod
    def _request(action="status", **extra):
        data = {"version": 1, "request_id": str(uuid.uuid4()), "action": action, **extra}
        return json.dumps(data, separators=(",", ":")).encode("utf-8") + b"\n"

    def test_pipe_roundtrip_and_normal_stop_use_fake_handler_only(self):
        seen = []

        def handler(request):
            seen.append(request.action)
            return BrokerReply(request.request_id, "stopped", None, 0, 300, False)

        server = AuthenticatedNamedPipeServer(
            handler, expected_user_sid=self.user_sid, expected_logon_sid=self.logon_sid)
        thread = server.start()
        client = self._connect()
        try:
            time.sleep(0.05)
            try:
                self.win32file.WriteFile(client, self._request())
            except Exception:
                self.fail(f"pipe closed before request (stage={server.stage}, stopped={server._stop.is_set()}, alive={thread.is_alive()}, error={server.last_error_code}, rejection={server.last_rejection_code})")
            try:
                error, response = self.win32file.ReadFile(client, MAX_MESSAGE_BYTES + 2)
            except Exception:
                self.fail(f"broker closed the response pipe (stage={server.stage}, seen={seen}, error={server.last_error_code}, rejection={server.last_rejection_code})")
            self.assertEqual(error, 0)
            self.assertEqual(parse_request(self._request()).action, "status")
            decoded = json.loads(response.decode("utf-8").rstrip("\n"))
            self.assertEqual(decoded["state"], "stopped")
            self.assertEqual(seen, ["status"])
        finally:
            self.win32file.CloseHandle(client)
        self.assertTrue(server.stop())
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertIsNone(server.last_error_code)

    def test_wrong_user_or_logon_identity_never_reaches_handler(self):
        wrong_user_sid = "S-1-5-21-111111111-222222222-333333333-4444"
        called = threading.Event()

        def handler(_request):
            called.set()
            raise AssertionError("unauthorized client reached the operation handler")

        server = AuthenticatedNamedPipeServer(
            handler, expected_user_sid=wrong_user_sid,
            expected_logon_sid=self.logon_sid)
        thread = server.start()
        client = self._connect()
        try:
            self.win32file.WriteFile(client, self._request())
            with self.assertRaises(Exception):
                self.win32file.ReadFile(client, MAX_MESSAGE_BYTES + 2)
        finally:
            self.win32file.CloseHandle(client)
        self.assertFalse(called.is_set())
        self.assertTrue(server.stop())
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertFalse(called.is_set())

    def test_wrong_logon_sid_never_reaches_handler(self):
        wrong_logon_sid = "S-1-5-5-111-222"
        called = threading.Event()
        server = AuthenticatedNamedPipeServer(
            lambda _request: called.set(), expected_user_sid=self.user_sid,
            expected_logon_sid=wrong_logon_sid)
        thread = server.start()
        client = self._connect()
        try:
            self.win32file.WriteFile(client, self._request())
            with self.assertRaises(Exception):
                self.win32file.ReadFile(client, MAX_MESSAGE_BYTES + 2)
        finally:
            self.win32file.CloseHandle(client)
        self.assertFalse(called.is_set())
        self.assertTrue(server.stop())
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_rejects_noncanonical_pipe_and_invalid_logon_sid(self):
        with self.assertRaises(BrokerTransportError):
            AuthenticatedNamedPipeServer(lambda _request: None,
                                         expected_user_sid=self.user_sid,
                                         expected_logon_sid=self.logon_sid,
                                         pipe_name=r"\\.\pipe\other")
        with self.assertRaises(BrokerTransportError):
            AuthenticatedNamedPipeServer(lambda _request: None,
                                         expected_user_sid=self.user_sid,
                                         expected_logon_sid="S-1-1-0")

    def test_stalled_client_is_disconnected_after_bounded_request_deadline(self):
        called = threading.Event()
        server = AuthenticatedNamedPipeServer(
            lambda _request: called.set(), expected_user_sid=self.user_sid,
            expected_logon_sid=self.logon_sid, request_timeout_seconds=0.2)
        thread = server.start()
        client = self._connect()
        try:
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline and server.last_rejection_code != "io_timeout":
                time.sleep(0.01)
            self.assertEqual(server.last_rejection_code, "io_timeout")
            self.assertFalse(called.is_set())
        finally:
            self.win32file.CloseHandle(client)
        self.assertTrue(server.stop())
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_stop_cancels_idle_overlapped_connect_promptly(self):
        server = AuthenticatedNamedPipeServer(
            lambda _request: None, expected_user_sid=self.user_sid,
            expected_logon_sid=self.logon_sid, connect_timeout_seconds=10)
        thread = server.start()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline and server.stage != "connecting":
            time.sleep(0.01)
        self.assertEqual(server.stage, "connecting")
        started = time.monotonic()
        self.assertTrue(server.stop(timeout_seconds=1))
        self.assertLess(time.monotonic() - started, 0.8)
        thread.join(1)
        self.assertFalse(thread.is_alive())

    def test_stop_cancels_stalled_overlapped_read_promptly(self):
        server = AuthenticatedNamedPipeServer(
            lambda _request: None, expected_user_sid=self.user_sid,
            expected_logon_sid=self.logon_sid, request_timeout_seconds=10)
        thread = server.start()
        client = self._connect()
        try:
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline and server.stage != "connected":
                time.sleep(0.01)
            self.assertEqual(server.stage, "connected")
            started = time.monotonic()
            self.assertTrue(server.stop(timeout_seconds=1))
            self.assertLess(time.monotonic() - started, 0.8)
        finally:
            self.win32file.CloseHandle(client)
        thread.join(1)
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
