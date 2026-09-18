import asyncio
import json
import ssl
import sys
import unittest

sys.path.insert(0, "connector/src")
from mt5_connector.config import ConnectorConfig
from mt5_connector.protocol import ConnectorProtocol, ProtocolError
from mt5_connector.websocket_client import ConnectorClient, TransportError, WssTransport

from test_runtime import Fake, RAW


class FakeTransport:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []
        self.closed = False

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        if not self.frames:
            raise OSError("connection closed")
        return self.frames.pop(0)

    async def close(self):
        self.closed = True


class WssRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def config(self, **updates):
        return ConnectorConfig.from_dict({**RAW, **updates})

    def test_wss_and_tls_enforcement(self):
        with self.assertRaises(TransportError):
            WssTransport("https://example/ws")
        context = ssl.create_default_context()
        context.check_hostname = False
        with self.assertRaises(TransportError):
            WssTransport("wss://example/ws", ssl_context=context)
        WssTransport("http://fake/ws", local_test=True)

    async def test_fake_handshake_snapshot_heartbeat(self):
        transport = FakeTransport([
            {"type": "snapshot", "generation": 0, "snapshot": {"account_id": "a1"}},
            {"type": "heartbeat_ack", "account_id": "a1", "generation": 0},
        ])
        client = ConnectorClient(self.config(), Fake(), transport=transport)
        await client.connect_once("injected-secret", max_messages=1)
        self.assertEqual(transport.sent[0]["type"], "hello")
        self.assertEqual(transport.sent[0]["generation"], 0)
        self.assertTrue(transport.closed)

    def test_generation_mismatch(self):
        protocol = ConnectorProtocol(self.config(backend_generation=4), Fake())
        protocol.accept_snapshot({"type": "snapshot", "generation": 4,
                                  "snapshot": {"account_id": "a1"}})
        with self.assertRaisesRegex(ProtocolError, "generation mismatch"):
            protocol.handle({"type": "heartbeat_ack", "account_id": "a1", "generation": 5})

    def test_side_effecting_commands_rejected(self):
        protocol = ConnectorProtocol(self.config(), Fake())
        with self.assertRaisesRegex(ProtocolError, "MALFORMED_FRAME"):
            protocol.handle({"type": "order.submit_market", "account_id": "a1",
                             "generation": 0, "command_id": "c1",
                             "idempotency_key": "i1"})

    async def test_secret_not_in_runtime_errors(self):
        secret = "do-not-log-this"
        transport = FakeTransport(["not-json"])
        client = ConnectorClient(self.config(), Fake(), transport=transport)
        with self.assertRaises(ProtocolError) as error:
            await client.connect_once(secret)
        self.assertNotIn(secret, str(error.exception))

    async def test_reconnect_delay_bounds(self):
        client = ConnectorClient(self.config(reconnect_max=2.0, jitter=0.2), Fake(),
                                 transport=FakeTransport(["not-json"]))
        delays = []
        async def sleep(value):
            delays.append(value)
        with self.assertRaises((OSError, ProtocolError)):
            await client.run(lambda: "secret", max_attempts=3, sleep=sleep)
        self.assertEqual(len(delays), 2)
        self.assertTrue(all(0 <= value <= 2.0 for value in delays))


if __name__ == "__main__":
    unittest.main()
