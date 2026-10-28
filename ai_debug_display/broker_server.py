"""Authenticated, single-request Windows named-pipe transport.

The server only transports the bounded broker protocol to an injected handler.
It contains no Hyper-V, process-launch, driver, or display operations.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Callable

from .broker_protocol import (
    PIPE_NAME,
    MAX_MESSAGE_BYTES,
    BrokerProtocolError,
    BrokerReply,
    encode_reply,
    parse_request,
)


_LOGON_SID = re.compile(r"^S-1-5-5-\d+-\d+$", re.IGNORECASE)
_LOGON_GROUP_MASK = 0xC0000000
_ERROR_IO_PENDING = 997
_ERROR_PIPE_CONNECTED = 535
_ERROR_MORE_DATA = 234


def _sid_string(security, sid) -> str:
    return security.ConvertSidToStringSid(sid)


def _win32_error_code(exc) -> int | None:
    code = getattr(exc, "winerror", None)
    if isinstance(code, int):
        return code
    args = getattr(exc, "args", ())
    return args[0] if args and isinstance(args[0], int) else None


class BrokerTransportError(RuntimeError):
    """Stable, content-free broker transport failure."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _win32_modules():
    if __import__("os").name != "nt":
        raise BrokerTransportError("windows_only")
    try:
        import pywintypes
        import win32api
        import win32con
        import win32file
        import win32pipe
        import win32security
    except ImportError:
        raise BrokerTransportError("pywin32_required") from None
    return pywintypes, win32api, win32con, win32file, win32pipe, win32security


def current_pipe_identity() -> tuple[str, str]:
    """Return the current process token's user SID and unique logon SID."""
    pywintypes, win32api, win32con, _file, _pipe, security = _win32_modules()
    token = security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    try:
        user_sid = security.GetTokenInformation(token, security.TokenUser)[0]
        logon_sids = [sid for sid, attrs in security.GetTokenInformation(token, security.TokenGroups)
                      if attrs & _LOGON_GROUP_MASK == _LOGON_GROUP_MASK]
        if len(logon_sids) != 1:
            raise BrokerTransportError("current_logon_sid_unavailable")
        return _sid_string(security, user_sid), _sid_string(security, logon_sids[0])
    except BrokerTransportError:
        raise
    except Exception:
        raise BrokerTransportError("current_identity_unavailable") from None
    finally:
        token.Close()


class AuthenticatedNamedPipeServer:
    """Serve one bounded request per connection on the fixed local pipe.

    Expected client identities must come from a trusted installer/session
    bootstrap. The pipe DACL is explicit; every connected client is separately
    impersonated for identity verification and the thread is reverted before
    application code runs.
    """

    def __init__(self, handler: Callable, *, expected_user_sid: str,
                 expected_logon_sid: str, pipe_name: str = PIPE_NAME,
                 connect_timeout_seconds: float = 1.0,
                 request_timeout_seconds: float = 2.0,
                 reply_timeout_seconds: float = 2.0):
        if pipe_name != PIPE_NAME:
            raise BrokerTransportError("pipe_name_not_allowed")
        if not isinstance(expected_user_sid, str) or not expected_user_sid.startswith("S-"):
            raise BrokerTransportError("expected_user_sid_invalid")
        if not isinstance(expected_logon_sid, str) or not _LOGON_SID.fullmatch(expected_logon_sid):
            raise BrokerTransportError("expected_logon_sid_invalid")
        if not callable(handler):
            raise BrokerTransportError("handler_invalid")
        if any(not isinstance(value, (int, float)) or isinstance(value, bool)
               or not 0.05 <= value <= 10
               for value in (connect_timeout_seconds, request_timeout_seconds,
                             reply_timeout_seconds)):
            raise BrokerTransportError("timeout_out_of_range")
        self._handler = handler
        self._expected_user_sid = expected_user_sid.upper()
        self._expected_logon_sid = expected_logon_sid.upper()
        self._pipe_name = pipe_name
        self._connect_timeout = connect_timeout_seconds
        self._request_timeout = request_timeout_seconds
        self._reply_timeout = reply_timeout_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._active_pipe = None
        self._serve_error: BrokerTransportError | None = None
        self._last_rejection: BrokerTransportError | None = None
        self._stage = "idle"

    @staticmethod
    def _validate_sid(value: str) -> None:
        _pywintypes, _api, _con, _file, _pipe, security = _win32_modules()
        try:
            security.ConvertStringSidToSid(value)
        except Exception:
            raise BrokerTransportError("sid_invalid") from None

    def _security_attributes(self):
        pywintypes, win32api, win32con, _file, _pipe, security = _win32_modules()
        self._validate_sid(self._expected_user_sid)
        self._validate_sid(self._expected_logon_sid)
        service_sid, _ = current_pipe_identity()
        # Protected DACL: broker identity can manage the pipe; only the
        # configured interactive user/logon can open a client endpoint.
        sddl = (f"D:P(A;;GA;;;{service_sid})"
                f"(A;;GRGW;;;{self._expected_user_sid})"
                f"(A;;GRGW;;;{self._expected_logon_sid})")
        descriptor = security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            sddl, security.SDDL_REVISION_1)
        attrs = pywintypes.SECURITY_ATTRIBUTES()
        attrs.SECURITY_DESCRIPTOR = descriptor
        attrs.bInheritHandle = False
        return attrs, descriptor

    def _create_pipe(self):
        pywintypes, _api, _con, _file, pipe, _security = _win32_modules()
        attrs, descriptor = self._security_attributes()
        # PySECURITY_DESCRIPTOR owns its native buffer and is retained through
        # CreateNamedPipe; dropping the Python wrapper releases it safely.
        return pipe.CreateNamedPipe(
            self._pipe_name,
            pipe.PIPE_ACCESS_DUPLEX | _con.FILE_FLAG_OVERLAPPED,
            pipe.PIPE_TYPE_MESSAGE | pipe.PIPE_READMODE_MESSAGE | pipe.PIPE_WAIT
            | pipe.PIPE_REJECT_REMOTE_CLIENTS,
            1,
            MAX_MESSAGE_BYTES + 2,
            MAX_MESSAGE_BYTES + 2,
            1000,
            attrs,
        )

    @staticmethod
    def _new_overlapped():
        pywintypes, _api, _con, _file, _pipe, _security = _win32_modules()
        import win32event
        overlapped = pywintypes.OVERLAPPED()
        overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
        return overlapped

    @staticmethod
    def _await_overlapped(handle, overlapped, timeout_seconds: float,
                          *, stop_event: threading.Event | None = None) -> None:
        _pywintypes, _api, _con, file, _pipe, _security = _win32_modules()
        import win32event
        deadline = time.monotonic() + timeout_seconds
        timed_out = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or (stop_event is not None and stop_event.is_set()):
                timed_out = remaining <= 0
                break
            wait_ms = max(1, min(100, int(remaining * 1000)))
            result = win32event.WaitForSingleObject(overlapped.hEvent, wait_ms)
            if result == win32event.WAIT_OBJECT_0:
                return
        # CancelIo is scoped to this thread, which issued this operation.
        try:
            file.CancelIo(handle)
        except Exception:
            pass
        try:
            file.GetOverlappedResult(handle, overlapped, True)
        except Exception:
            pass
        raise BrokerTransportError("io_timeout" if timed_out else "io_cancelled")

    def _connect_pipe(self, handle) -> None:
        _pywintypes, api, _con, file, pipe, _security = _win32_modules()
        overlapped = self._new_overlapped()
        try:
            try:
                result = pipe.ConnectNamedPipe(handle, overlapped)
            except Exception as exc:
                code = _win32_error_code(exc)
                if code == _ERROR_PIPE_CONNECTED:
                    return
                if code != _ERROR_IO_PENDING:
                    raise BrokerTransportError(
                        f"pipe_connect_failed_{code}" if isinstance(code, int)
                        else "pipe_connect_failed") from None
            else:
                if result in (0, _ERROR_PIPE_CONNECTED):
                    return
                if result not in (None, _ERROR_IO_PENDING):
                    raise BrokerTransportError(f"pipe_connect_failed_result_{result}")
            self._await_overlapped(handle, overlapped, self._connect_timeout,
                                   stop_event=self._stop)
            file.GetOverlappedResult(handle, overlapped, False)
        finally:
            try:
                api.CloseHandle(overlapped.hEvent)
            except Exception:
                pass

    @classmethod
    def _read_message(cls, handle, max_bytes: int, timeout_seconds: float,
                      stop_event: threading.Event | None = None) -> bytes:
        _pywintypes, _api, _con, file, _pipe, _security = _win32_modules()
        buffer = file.AllocateReadBuffer(max_bytes)
        overlapped = cls._new_overlapped()
        try:
            error, data = file.ReadFile(handle, buffer, overlapped)
            if error == _ERROR_IO_PENDING:
                cls._await_overlapped(handle, overlapped, timeout_seconds,
                                      stop_event=stop_event)
                try:
                    count = file.GetOverlappedResult(handle, overlapped, False)
                except Exception as exc:
                    if _win32_error_code(exc) == _ERROR_MORE_DATA:
                        raise BrokerTransportError("request_frame_too_large") from None
                    raise
                return bytes(buffer[:count])
            if error == _ERROR_MORE_DATA:
                raise BrokerTransportError("request_frame_too_large")
            if error != 0:
                raise BrokerTransportError("request_read_failed")
            return bytes(data)
        except BrokerTransportError:
            raise
        except Exception:
            raise BrokerTransportError("request_read_failed") from None
        finally:
            try:
                _api.CloseHandle(overlapped.hEvent)
            except Exception:
                pass

    @classmethod
    def _write_message(cls, handle, payload: bytes, timeout_seconds: float) -> None:
        _pywintypes, _api, _con, file, _pipe, _security = _win32_modules()
        overlapped = cls._new_overlapped()
        try:
            error, _written = file.WriteFile(handle, payload, overlapped)
            if error == _ERROR_IO_PENDING:
                cls._await_overlapped(handle, overlapped, timeout_seconds)
                file.GetOverlappedResult(handle, overlapped, False)
            elif error != 0:
                raise BrokerTransportError("reply_write_failed")
        except BrokerTransportError:
            raise
        except Exception:
            raise BrokerTransportError("reply_write_failed") from None
        finally:
            try:
                _api.CloseHandle(overlapped.hEvent)
            except Exception:
                pass

    @staticmethod
    def _client_identity(pipe_handle) -> tuple[str, str]:
        pywintypes, win32api, win32con, _file, _pipe, security = _win32_modules()
        try:
            security.ImpersonateNamedPipeClient(pipe_handle)
        except Exception as exc:
            code = _win32_error_code(exc)
            raise BrokerTransportError(
                f"client_impersonation_failed_{code}" if isinstance(code, int)
                else "client_impersonation_failed") from None
        token = None
        try:
            token = security.OpenThreadToken(win32api.GetCurrentThread(),
                                             win32con.TOKEN_QUERY, True)
            user_sid = security.GetTokenInformation(token, security.TokenUser)[0]
            logon_sids = [sid for sid, attrs in security.GetTokenInformation(token, security.TokenGroups)
                          if attrs & _LOGON_GROUP_MASK == _LOGON_GROUP_MASK]
            if len(logon_sids) != 1:
                raise BrokerTransportError("client_logon_sid_invalid")
            return (_sid_string(security, user_sid).upper(),
                    _sid_string(security, logon_sids[0]).upper())
        except BrokerTransportError:
            raise
        except Exception:
            raise BrokerTransportError("client_identity_unavailable") from None
        finally:
            if token is not None:
                token.Close()
            try:
                security.RevertToSelf()
            except Exception:
                raise BrokerTransportError("client_token_revert_failed") from None

    def _authorize(self, pipe_handle) -> None:
        user_sid, logon_sid = self._client_identity(pipe_handle)
        if user_sid != self._expected_user_sid or logon_sid != self._expected_logon_sid:
            raise BrokerTransportError("client_identity_denied")

    def _serve_connection(self, pipe_handle) -> None:
        _pywintypes, _api, _con, _file, _pipe, _security = _win32_modules()
        if self._stop.is_set():
            return
        try:
            payload = self._read_message(pipe_handle, MAX_MESSAGE_BYTES + 2,
                                         self._request_timeout, self._stop)
        except BrokerTransportError:
            raise
        except Exception:
            raise BrokerTransportError("request_read_failed") from None
        if len(payload) > MAX_MESSAGE_BYTES + 1:
            # ERROR_MORE_DATA means a single message exceeded the bounded frame.
            raise BrokerTransportError("request_frame_too_large")
        try:
            request = parse_request(payload)
        except BrokerProtocolError as exc:
            raise BrokerTransportError(exc.code) from None
        # ImpersonateNamedPipeClient uses the identity associated with the
        # last message read from this pipe; authorize before dispatching it.
        self._authorize(pipe_handle)
        self._stage = "authorized"
        try:
            self._stage = "dispatching"
            reply = self._handler(request)
        except Exception:
            raise BrokerTransportError("handler_failed") from None
        if not isinstance(reply, BrokerReply) or reply.request_id != request.request_id:
            raise BrokerTransportError("handler_reply_invalid")
        try:
            self._stage = "writing_reply"
            self._write_message(pipe_handle, encode_reply(reply), self._reply_timeout)
        except Exception:
            raise BrokerTransportError("reply_write_failed") from None

    def serve_forever(self) -> None:
        """Block on one connection at a time until stop() wakes the listener."""
        _pywintypes, _api, _con, file, pipe, _security = _win32_modules()
        while not self._stop.is_set():
            handle = None
            try:
                self._stage = "creating_pipe"
                handle = self._create_pipe()
                with self._state_lock:
                    self._active_pipe = handle
                try:
                    self._stage = "connecting"
                    self._connect_pipe(handle)
                except BrokerTransportError as exc:
                    if exc.code == "io_cancelled" and self._stop.is_set():
                        break
                    if exc.code == "io_timeout":
                        continue
                    if self._stop.is_set():
                        break
                    raise
                if not self._stop.is_set():
                    self._stage = "connected"
                    try:
                        self._serve_connection(handle)
                    except BrokerTransportError as exc:
                        self._last_rejection = exc
                        # Refuse malformed/unauthorized connections without
                        # returning details or leaking caller-provided content.
                        if exc.code == "client_token_revert_failed":
                            self._serve_error = exc
                            break
                try:
                    pipe.DisconnectNamedPipe(handle)
                except Exception:
                    pass
            except BrokerTransportError as exc:
                self._last_rejection = exc
                if exc.code == "client_token_revert_failed":
                    self._serve_error = exc
                    break
            except Exception as exc:
                code = _win32_error_code(exc)
                self._serve_error = BrokerTransportError(
                    f"pipe_server_error_{code}" if isinstance(code, int) else "pipe_server_error")
                if not self._stop.is_set():
                    break
            finally:
                if not self._stop.is_set():
                    self._stage = "disconnected"
                with self._state_lock:
                    self._active_pipe = None
                if handle is not None:
                    try:
                        file.CloseHandle(handle)
                    except Exception:
                        pass

    def start(self, *, daemon: bool = True) -> threading.Thread:
        _win32_modules()  # Fail synchronously before creating a background thread.
        with self._state_lock:
            if self._thread is not None and self._thread.is_alive():
                raise BrokerTransportError("server_already_running")
            self._stop.clear()
            self._serve_error = None
            self._last_rejection = None
            self._thread = threading.Thread(target=self.serve_forever,
                                            name="AiDebugDisplayBroker", daemon=daemon)
            self._thread.start()
            return self._thread

    def stop(self, timeout_seconds: float = 3.0) -> bool:
        if not 0.1 <= timeout_seconds <= 10:
            raise ValueError("stop_timeout_out_of_range")
        self._stop.set()
        _pywintypes, _api, con, file, _pipe, _security = _win32_modules()
        thread = self._thread
        if thread is None:
            return True
        end = time.monotonic() + timeout_seconds
        # The server may be between pipe instances when stop is requested.
        # Retry the local wake connection until it observes stop or times out.
        while thread.is_alive() and time.monotonic() < end:
            try:
                wake = file.CreateFile(self._pipe_name, con.GENERIC_READ | con.GENERIC_WRITE,
                                       0, None, con.OPEN_EXISTING, 0, None)
                file.CloseHandle(wake)
            except Exception:
                pass
            thread.join(0.02)
        return not thread.is_alive()

    @property
    def last_error_code(self) -> str | None:
        error = self._serve_error
        return error.code if error else None

    @property
    def last_rejection_code(self) -> str | None:
        error = self._last_rejection
        return error.code if error else None

    @property
    def stage(self) -> str:
        return self._stage
