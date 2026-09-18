import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, "connector/src")
sys.path.insert(0, "connector/tests")

from mt5_connector.config import ConnectorConfig
from mt5_connector.models import Identity, AccountSnapshot
from mt5_connector.protocol import ConnectorProtocol, ProtocolError

from test_runtime import Fake, RAW


class ContractIssue63Tests(unittest.TestCase):
    def config(self, **updates):
        return ConnectorConfig.from_dict({**RAW, **updates})

    def test_hello_is_exactly_typed_nine_field_frame(self):
        protocol = ConnectorProtocol(self.config(backend_generation=7), Fake())
        hello = protocol.hello("secret-value")
        self.assertEqual(
            set(hello),
            {
                "type", "account_id", "provider", "broker_server",
                "external_account_id", "key_id", "secret", "generation",
                "session_id",
            },
        )
        self.assertEqual(hello["generation"], 7)
        with self.assertRaisesRegex(ProtocolError, "MALFORMED_FRAME"):
            protocol.validate_hello({**hello, "extra": "nope"})

    def test_canonical_envelope_rejects_replay_and_stale_epoch(self):
        protocol = ConnectorProtocol(self.config(backend_generation=7), Fake())
        protocol.accept_snapshot({
            "type": "snapshot", "generation": 7, "execution_epoch": 4,
            "snapshot": {"account_id": "a1"},
        })
        command = {
            "schema_version": 1, "type": "order.submit_market",
            "message_id": "command-message-1", "account_id": "a1",
            "provider": "MT5", "broker_server": "Demo",
            "external_account_id": "42", "generation": 7, "sequence": 1,
            "execution_epoch": 4, "command_id": "command-1",
            "idempotency_key": "manual-1", "request_hash": "hash-1",
            "sent_at": "2026-09-18T00:00:00+00:00", "payload": {},
        }
        with self.assertRaisesRegex(ProtocolError, "MALFORMED_FRAME"):
            protocol.handle({**command, "dispatch_sequence": 1})
        result = protocol.handle(command)
        self.assertEqual(result["payload"]["state"], "REJECTED")
        self.assertEqual(result["request_hash"], "hash-1")
        self.assertNotIn("dispatch_sequence", result)
        self.assertNotIn("connector_generation", result)
        with self.assertRaisesRegex(ProtocolError, "OUT_OF_ORDER_SEQUENCE"):
            protocol.handle({**command, "message_id": "command-message-2"})
        with self.assertRaisesRegex(ProtocolError, "REPLAYED_SEQUENCE"):
            protocol.handle(command)
        stale = protocol.handle({**command, "message_id": "command-message-3", "sequence": 2,
                                  "execution_epoch": 3})
        self.assertEqual(stale["payload"], {"state": "REJECTED", "code": "STALE_EPOCH"})

    def test_duplicate_result_replays_without_new_adapter_work(self):
        protocol = ConnectorProtocol(self.config(backend_generation=7), Fake())
        protocol.accept_snapshot({
            "type": "snapshot", "generation": 7, "execution_epoch": 4,
            "snapshot": {"account_id": "a1"},
        })
        command = {
            "schema_version": 1, "type": "order.submit_market",
            "message_id": "command-message-1", "account_id": "a1",
            "provider": "MT5", "broker_server": "Demo",
            "external_account_id": "42", "generation": 7, "sequence": 1,
            "execution_epoch": 4, "command_id": "command-1",
            "idempotency_key": "manual-1", "request_hash": "hash-1",
            "sent_at": "2026-09-18T00:00:00+00:00", "payload": {},
        }
        first = protocol.handle(command)
        replay = protocol.handle({**command, "message_id": "command-message-2", "sequence": 2})
        self.assertEqual(replay, first)
        with self.assertRaisesRegex(ProtocolError, "IDEMPOTENCY_KEY_REUSED"):
            protocol.handle({**command, "message_id": "command-message-3", "sequence": 3,
                             "request_hash": "different"})

    def test_read_only_command_returns_idempotent_command_result(self):
        protocol = ConnectorProtocol(self.config(backend_generation=7), Fake())
        protocol.accept_snapshot({
            "type": "snapshot", "generation": 7, "execution_epoch": 4,
            "snapshot": {"account_id": "a1"},
        })
        command = {
            "schema_version": 1, "type": "account_snapshot.request",
            "message_id": "snapshot-command-1", "account_id": "a1",
            "provider": "MT5", "broker_server": "Demo",
            "external_account_id": "42", "generation": 7, "sequence": 1,
            "execution_epoch": 4, "command_id": "snapshot-command",
            "idempotency_key": "snapshot-key", "request_hash": "snapshot-hash",
            "sent_at": "2026-09-18T00:00:00+00:00", "payload": {},
        }
        result = protocol.handle(command)
        self.assertEqual(result["type"], "command.result")
        self.assertEqual(result["payload"]["state"], "ACCEPTED")
        self.assertEqual(result["idempotency_key"], "snapshot-key")
        self.assertEqual(result["request_hash"], "snapshot-hash")
        replay = protocol.handle({**command, "message_id": "snapshot-command-2", "sequence": 2})
        self.assertEqual(replay, result)

    def test_candle_request_requires_closed_only_and_valid_payload(self):
        protocol = ConnectorProtocol(self.config(backend_generation=7), Fake())
        protocol.accept_snapshot({
            "type": "snapshot", "generation": 7, "execution_epoch": 4,
            "snapshot": {"account_id": "a1"},
        })
        command = {
            "schema_version": 1, "type": "candle_batch.request",
            "message_id": "candle-command-1", "account_id": "a1",
            "provider": "MT5", "broker_server": "Demo",
            "external_account_id": "42", "generation": 7, "sequence": 1,
            "execution_epoch": 4, "command_id": "candle-command",
            "idempotency_key": "candle-key", "request_hash": "candle-hash",
            "sent_at": "2026-09-18T00:00:00+00:00",
            "payload": {"symbol": "EURUSD", "count": 2},
        }
        with self.assertRaisesRegex(ProtocolError, "MALFORMED_FRAME"):
            protocol.handle(command)
        with self.assertRaisesRegex(ProtocolError, "MALFORMED_FRAME"):
            protocol.handle({
                **command,
                "message_id": "candle-command-2",
                "sequence": 2,
                "payload": {"closed_only": True, "count": 0},
            })

    def test_telemetry_requires_null_command_id(self):
        protocol = ConnectorProtocol(self.config(backend_generation=7), Fake())
        protocol.accept_snapshot({
            "type": "snapshot", "generation": 7,
            "snapshot": {"account_id": "a1"},
        })
        with self.assertRaisesRegex(ProtocolError, "MALFORMED_FRAME"):
            protocol.handle({
                "schema_version": 1, "type": "heartbeat",
                "message_id": "heartbeat-1", "account_id": "a1",
                "provider": "MT5", "broker_server": "Demo",
                "external_account_id": "42", "generation": 7, "sequence": 1,
                "execution_epoch": 0, "command_id": "unexpected",
                "idempotency_key": "msg:heartbeat-1",
                "sent_at": "2026-09-18T00:00:00+00:00", "payload": {},
            })

        protocol = ConnectorProtocol(self.config(), Fake())
        with self.assertRaises(ProtocolError) as error:
            protocol.validate_hello({"type": "hello", "secret": "super-secret"})
        self.assertNotIn("super-secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
