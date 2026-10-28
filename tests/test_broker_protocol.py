from __future__ import annotations

import json
import unittest
import uuid

from ai_debug_display.broker_protocol import (
    BrokerProtocolError, BrokerReply, FRAME_DELIMITER, PIPE_NAME,
    encode_reply, parse_request,
)


def request(action="status", **extra):
    value = {"version": 1, "request_id": str(uuid.uuid4()), "action": action}
    value.update(extra)
    return json.dumps(value, separators=(",", ":"))


class BrokerProtocolTests(unittest.TestCase):
    def test_fixed_only_chat_pipe_and_single_line_framing(self):
        self.assertEqual(PIPE_NAME, r"\\.\pipe\OnlyChatAiDebugDisplay.v1")
        framed = request().encode("utf-8") + FRAME_DELIMITER
        self.assertEqual(parse_request(framed).action, "status")
        with self.assertRaisesRegex(BrokerProtocolError, "message_framing_invalid"):
            parse_request(framed + framed)

    def test_status_has_no_arguments_or_user_paths(self):
        result = parse_request(request())
        self.assertEqual(result.action, "status")
        self.assertIsNone(result.run_id)
        with self.assertRaisesRegex(BrokerProtocolError, "request_fields_invalid"):
            parse_request(request(path="C:/users/file"))

    def test_acquire_requires_user_confirmation_and_guid_run_id(self):
        run_id = str(uuid.uuid4())
        with self.assertRaisesRegex(BrokerProtocolError, "confirmation_required"):
            parse_request(request("acquire", run_id=run_id, confirmed=False))
        result = parse_request(request("acquire", run_id=run_id, confirmed=True))
        self.assertTrue(result.explicitly_confirmed)
        self.assertEqual(result.run_id, run_id)

    def test_run_actions_require_a_run_identity(self):
        for action in ("heartbeat", "release"):
            with self.assertRaisesRegex(BrokerProtocolError, "request_fields_invalid"):
                parse_request(request(action))

    def test_unknown_command_and_unknown_fields_are_rejected(self):
        with self.assertRaisesRegex(BrokerProtocolError, "action_not_allowed"):
            parse_request(request("powershell"))
        with self.assertRaisesRegex(BrokerProtocolError, "request_fields_invalid"):
            parse_request(request("release", run_id=str(uuid.uuid4()), command="Start-VM"))

    def test_duplicate_keys_invalid_json_and_invalid_utf8_are_rejected(self):
        with self.assertRaisesRegex(BrokerProtocolError, "duplicate_json_key"):
            parse_request('{"version":1,"version":1,"request_id":"00000000-0000-0000-0000-000000000000","action":"status"}')
        with self.assertRaisesRegex(BrokerProtocolError, "invalid_json"):
            parse_request(b"{broken")
        with self.assertRaisesRegex(BrokerProtocolError, "invalid_json"):
            parse_request(b"\xff")

    def test_version_and_size_are_bounded(self):
        value = json.loads(request())
        value["version"] = 2
        with self.assertRaisesRegex(BrokerProtocolError, "protocol_version_unsupported"):
            parse_request(json.dumps(value))
        with self.assertRaisesRegex(BrokerProtocolError, "message_size_invalid"):
            parse_request(b"{" + b" " * 4096 + b"}")

    def test_boolean_type_is_not_confused_with_integer_version(self):
        value = json.loads(request())
        value["version"] = True
        with self.assertRaisesRegex(BrokerProtocolError, "protocol_version_unsupported"):
            parse_request(json.dumps(value))

    def test_reply_has_no_secrets_and_rejects_invalid_fields(self):
        reply = BrokerReply(str(uuid.uuid4()), "stopped", None, 0, 300, False)
        payload = encode_reply(reply)
        self.assertTrue(payload.endswith(FRAME_DELIMITER))
        self.assertNotIn(b"secret", payload)
        self.assertEqual(json.loads(payload)["state"], "stopped")
        bad = BrokerReply(reply.request_id, "stopped", None, -1, 300, False)
        with self.assertRaisesRegex(BrokerProtocolError, "reply_idle_invalid"):
            encode_reply(bad)


if __name__ == "__main__":
    unittest.main()
