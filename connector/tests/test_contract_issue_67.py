from collections import deque
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "connector/src")
sys.path.insert(0, "connector/tests")

from mt5_connector.adapter import FakeMT5Adapter
from mt5_connector.config import ConnectorConfig
from mt5_connector.dispatcher import Dispatcher
from mt5_connector.journal import SQLiteJournal, canonical_request_hash
from mt5_connector.protocol import ConnectorProtocol, ProtocolError
from mt5_connector.websocket_client import ConnectorClient

from test_runtime import RAW


class FakeWss:
    def __init__(self, frames):
        self.frames = deque(frames)
        self.sent = []

    async def connect(self):
        return self

    async def send(self, frame):
        self.sent.append(frame)

    async def recv(self):
        return self.frames.popleft()

    async def close(self):
        return None


class Issue67ContractTests(unittest.IsolatedAsyncioTestCase):
    def config(self):
        return ConnectorConfig.from_dict(RAW)

    def command(self, *, message_id="command-1", command_id="order-1",
                idempotency_key="key-1", sequence=1, payload=None):
        payload = payload or {"symbol": "EURUSD", "volume": "0.01", "sl": "1", "tp": "2"}
        return {
            "schema_version": 1, "type": "order.submit_market",
            "message_id": message_id, "account_id": "a1", "provider": "MT5",
            "broker_server": "Demo", "external_account_id": "42",
            "generation": 7, "sequence": sequence, "execution_epoch": 4,
            "command_id": command_id, "idempotency_key": idempotency_key,
            "request_hash": canonical_request_hash("order.submit_market", payload),
            "sent_at": "2026-09-22T00:00:00Z",
            "payload": payload,
        }

    def dispatcher(self, adapter):
        journal = SQLiteJournal(str(Path(self.tmp.name) / "connector.sqlite"), "a1")
        self.addCleanup(journal.close)
        return Dispatcher(journal, adapter, generation=7, execution_epoch=4)

    def protocol_with_dispatcher(self, adapter):
        dispatcher = self.dispatcher(adapter)
        protocol = ConnectorProtocol(self.config(), adapter, dispatcher=dispatcher)
        protocol.accept_snapshot(self.snapshot())
        return protocol, dispatcher

    @staticmethod
    def snapshot():
        return {
            "type": "snapshot", "generation": 7, "execution_epoch": 4,
            "snapshot": {"account_id": "a1"},
        }

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    async def test_command_enabled_wss_session_invokes_fake_adapter_once(self):
        adapter = FakeMT5Adapter()
        dispatcher = self.dispatcher(adapter)
        command = self.command()
        transport = FakeWss([self.snapshot(), command])
        client = ConnectorClient(self.config(), adapter, transport=transport,
                                 dispatcher=dispatcher)

        await client.connect_once("secret", max_messages=1)

        self.assertEqual([kind for kind, _ in adapter.checks], ["order.submit_market"])
        self.assertEqual([kind for kind, _ in adapter.requests], ["order.submit_market"])
        self.assertEqual(len(transport.sent), 2)
        self.assertEqual(transport.sent[1].count("secret"), 0)
        self.assertIn('"state":"ACCEPTED"', transport.sent[1])

    async def test_order_check_rejection_never_invokes_and_replay_is_durable(self):
        adapter = FakeMT5Adapter(check={"retcode": 10016})
        protocol, _dispatcher = self.protocol_with_dispatcher(adapter)

        first = protocol.handle(self.command())
        replay = protocol.handle(self.command(message_id="command-2", sequence=2))

        self.assertEqual(first["payload"]["code"], "ORDER_CHECK_REJECTED")
        self.assertEqual(replay["payload"], first["payload"])
        self.assertEqual(len(adapter.checks), 1)
        self.assertEqual(adapter.requests, [])

    async def test_unknown_is_invoked_once_and_ambiguous_match_reopens(self):
        adapter = FakeMT5Adapter(outcome={"state": "UNKNOWN", "code": "TRANSPORT_AMBIGUOUS"})
        protocol, dispatcher = self.protocol_with_dispatcher(adapter)

        result = protocol.handle(self.command())
        self.assertEqual(result["payload"]["state"], "UNKNOWN")
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(dispatcher.reconcile(
            "order-1", snapshot={"orders": [{"order_id": "order-1"}]},
            source="history", match_status="UNIQUE_MATCH", state="ACCEPTED",
            result={"external_order_id": "order-1"},
        )["state"], "ACCEPTED")
        replay = protocol.handle(self.command(message_id="command-2", sequence=2))
        self.assertEqual(replay["payload"]["state"], "ACCEPTED")
        self.assertEqual(len(adapter.requests), 1)

    async def test_idempotency_key_reuse_is_rejected_without_broker_call(self):
        adapter = FakeMT5Adapter()
        protocol, _dispatcher = self.protocol_with_dispatcher(adapter)
        protocol.handle(self.command())
        with self.assertRaisesRegex(ProtocolError, "IDEMPOTENCY_KEY_REUSED"):
            protocol.handle(self.command(message_id="command-2", sequence=2,
                                         command_id="order-2", idempotency_key="key-1"))
        self.assertEqual(len(adapter.requests), 1)


if __name__ == "__main__":
    unittest.main()
