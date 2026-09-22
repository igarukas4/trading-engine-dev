import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "connector/src")
from mt5_connector.config import ConnectorConfig
from mt5_connector.dispatcher import Dispatcher
from mt5_connector.journal import canonical_request_hash, SQLiteJournal
from mt5_connector.main import RuntimeErrorSafe, run_runtime
from mt5_connector.preflight import PreflightError


class FakeTransport:
    def __init__(self, frames):
        self.frames = list(frames)
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        return self.frames.pop(0)

    async def close(self):
        pass


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def order_check(self, typ, payload):
        self.calls.append(("check", typ, payload))
        return {"retcode": 0}

    def invoke(self, typ, payload):
        self.calls.append(("invoke", typ, payload))
        return {"retcode": 10009, "external_order_id": "fake-order"}


class Issue70RuntimeTests(unittest.TestCase):
    def config(self, path, **updates):
        raw = {
            "account_id": "account-1", "provider": "MT5", "broker_server": "Demo",
            "external_account_id": "42", "key_id": "key-1", "secret_ref": "file:///outside/secret",
            "terminal_path": "C:/terminal64.exe", "wss_url": "wss://backend/ws",
            "journal_path": str(path), "execution_disabled": False,
        }
        raw.update(updates)
        return ConnectorConfig.from_dict(raw)

    def preflight(self):
        return {name: True for name in (
            "demo_account", "manual_mode", "terminal_healthy", "trade_allowed",
            "journal_writable", "generation_current", "epoch_current",
            "reconciliation_complete", "no_unknown_commands", "backend_execution_gate",
        )}

    def test_missing_preflight_fails_before_transport_or_adapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeAdapter()
            with self.assertRaisesRegex(RuntimeErrorSafe, "PREFLIGHT_REQUIRED"):
                run_runtime(self.config(Path(tmp) / "journal.sqlite"), lambda: "opaque",
                            adapter=adapter, transport=FakeTransport([]))
            self.assertEqual(adapter.calls, [])

    def test_command_enabled_runtime_uses_durable_dispatcher_and_fake_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"symbol": "EURUSD", "sl": "1", "tp": "2"}
            request_hash = canonical_request_hash("order.submit_market", payload)
            command = {
                "schema_version": 1, "type": "order.submit_market", "message_id": "command-frame",
                "account_id": "account-1", "provider": "MT5", "broker_server": "Demo",
                "external_account_id": "42", "generation": 0, "sequence": 1,
                "execution_epoch": 0, "command_id": "command-1", "idempotency_key": "idem-1",
                "request_hash": request_hash, "sent_at": "2026-01-01T00:00:00Z", "payload": payload,
            }
            transport = FakeTransport([
                {"type": "snapshot", "generation": 0, "snapshot": {"account_id": "account-1"}},
                command,
            ])
            adapter = FakeAdapter()
            run_runtime(self.config(Path(tmp) / "journal.sqlite"), lambda: "opaque",
                        adapter=adapter, transport=transport, preflight=self.preflight())
            self.assertEqual([call[0] for call in adapter.calls], ["check", "invoke"])
            self.assertEqual(transport.sent[-1]["type"], "command.result")
            self.assertEqual(transport.sent[-1]["payload"]["state"], "ACCEPTED")
            journal = SQLiteJournal(str(Path(tmp) / "journal.sqlite"), "account-1")
            self.assertEqual(journal.get("command-1").state, "ACCEPTED")
            journal.close()


if __name__ == "__main__":
    unittest.main()
