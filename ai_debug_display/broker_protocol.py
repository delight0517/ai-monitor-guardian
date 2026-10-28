"""Strict, content-free JSON protocol for a future local privileged broker.

This module is only a parser/contract. It does not create a pipe, start a task,
or invoke Hyper-V. The transport must apply an explicit Windows user-SID ACL.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import uuid


PROTOCOL_VERSION = 1
PIPE_NAME = r"\\.\pipe\OnlyChatAiDebugDisplay.v1"
FRAME_DELIMITER = b"\n"
MAX_MESSAGE_BYTES = 4096  # UTF-8 JSON body bytes, excluding the one-byte delimiter.
_BASE_FIELDS = {"version", "request_id", "action"}
_RUN_ACTIONS = {"heartbeat", "release"}
_ALLOWED_ACTIONS = {"status", "acquire", *_RUN_ACTIONS}


class BrokerProtocolError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BrokerRequest:
    request_id: str
    action: str
    run_id: str | None = None
    explicitly_confirmed: bool = False


@dataclass(frozen=True)
class BrokerReply:
    request_id: str
    state: str
    run_id: str | None
    idle_seconds: int
    idle_timeout_seconds: int
    display_enabled: bool
    error_code: str | None = None


def _pairs_no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BrokerProtocolError("duplicate_json_key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise BrokerProtocolError("non_finite_number")


def _uuid(value, code):
    if not isinstance(value, str) or len(value) != 36:
        raise BrokerProtocolError(code)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise BrokerProtocolError(code) from None
    if str(parsed) != value.lower():
        raise BrokerProtocolError(code)
    return str(parsed)


def parse_request(payload: bytes | str) -> BrokerRequest:
    if isinstance(payload, str):
        try:
            raw = payload.encode("utf-8", "strict")
        except UnicodeError:
            raise BrokerProtocolError("invalid_utf8") from None
    elif isinstance(payload, bytes):
        raw = payload
    else:
        raise BrokerProtocolError("message_type_invalid")
    if raw.endswith(FRAME_DELIMITER):
        if raw.count(FRAME_DELIMITER) != 1:
            raise BrokerProtocolError("message_framing_invalid")
        raw = raw[:-1]
    elif FRAME_DELIMITER in raw:
        raise BrokerProtocolError("message_framing_invalid")
    if not raw or len(raw) > MAX_MESSAGE_BYTES:
        raise BrokerProtocolError("message_size_invalid")
    try:
        data = json.loads(raw.decode("utf-8", "strict"),
                          object_pairs_hook=_pairs_no_duplicates,
                          parse_constant=_reject_constant)
    except BrokerProtocolError:
        raise
    except (UnicodeError, json.JSONDecodeError):
        raise BrokerProtocolError("invalid_json") from None
    if not isinstance(data, dict):
        raise BrokerProtocolError("request_must_be_object")
    action = data.get("action")
    if not isinstance(action, str) or action not in _ALLOWED_ACTIONS:
        raise BrokerProtocolError("action_not_allowed")
    fields = set(_BASE_FIELDS)
    if action == "acquire":
        fields.update(("run_id", "confirmed"))
    elif action in _RUN_ACTIONS:
        fields.add("run_id")
    if set(data) != fields:
        raise BrokerProtocolError("request_fields_invalid")
    if type(data.get("version")) is not int or data["version"] != PROTOCOL_VERSION:
        raise BrokerProtocolError("protocol_version_unsupported")
    request_id = _uuid(data.get("request_id"), "request_id_invalid")
    run_id = None
    confirmed = False
    if action in {"acquire", *_RUN_ACTIONS}:
        run_id = _uuid(data.get("run_id"), "run_id_invalid")
    if action == "acquire":
        if data.get("confirmed") is not True:
            raise BrokerProtocolError("confirmation_required")
        confirmed = True
    return BrokerRequest(request_id, action, run_id, confirmed)


def encode_reply(reply: BrokerReply) -> bytes:
    _uuid(reply.request_id, "request_id_invalid")
    if reply.run_id is not None:
        _uuid(reply.run_id, "run_id_invalid")
    if reply.state not in {"stopped", "starting", "running", "stopping", "failed"}:
        raise BrokerProtocolError("reply_state_invalid")
    if type(reply.idle_seconds) is not int or reply.idle_seconds < 0:
        raise BrokerProtocolError("reply_idle_invalid")
    if type(reply.idle_timeout_seconds) is not int or not 1 <= reply.idle_timeout_seconds <= 300:
        raise BrokerProtocolError("reply_timeout_invalid")
    if type(reply.display_enabled) is not bool:
        raise BrokerProtocolError("reply_display_state_invalid")
    if reply.error_code is not None and (not isinstance(reply.error_code, str)
                                         or len(reply.error_code) > 80
                                         or not reply.error_code.replace("_", "").isalnum()):
        raise BrokerProtocolError("reply_error_code_invalid")
    body = json.dumps({
        "version": PROTOCOL_VERSION,
        "request_id": reply.request_id,
        "state": reply.state,
        "run_id": reply.run_id,
        "idle_seconds": reply.idle_seconds,
        "idle_timeout_seconds": reply.idle_timeout_seconds,
        "display_enabled": reply.display_enabled,
        "error_code": reply.error_code,
    }, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(body) > MAX_MESSAGE_BYTES:
        raise BrokerProtocolError("reply_size_invalid")
    return body + FRAME_DELIMITER

