import asyncio
from contextlib import redirect_stdout
from dataclasses import asdict
from io import StringIO
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "connector/src")
from mt5_connector.config import ConnectorConfig
from mt5_connector.dispatcher import Dispatcher
from mt5_connector.journal import canonical_request_hash, SQLiteJournal
from mt5_connector.main import RuntimeErrorSafe, main, run_runtime
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

    def preflight_facts(self):
        return {"account_id": "account-1", "provider": "MT5", "broker_server": "Demo",
                "external_account_id": "42", "trade_mode": "DEMO", "terminal_connected": True,
                "terminal_trade_allowed": True, "account_trade_allowed": True}

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

    def snapshot(self):
        return {"type": "snapshot", "generation": 0, "execution_epoch": 4,
                "snapshot": {"account_id": "account-1", "identity": {
                    "provider": "MT5", "broker_server": "Demo", "external_account_id": "42"},
                    "environment": "DEMO", "execution_mode": "MANUAL"}}

    def reconciliation_ack(self):
        return {"schema_version": 1, "type": "reconciliation_observed", "message_id": "ack-1",
                "account_id": "account-1", "provider": "MT5", "broker_server": "Demo",
                "external_account_id": "42", "generation": 0, "sequence": 1,
                "execution_epoch": 4, "command_id": None, "idempotency_key": "msg:ack-1",
                "sent_at": "2026-01-01T00:00:00Z", "payload": {
                    "reconciliation_complete": True, "backend_execution_gate": True,
                    "no_unknown_commands": True}}

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
                "external_account_id": "42", "generation": 0, "sequence": 2,
                "execution_epoch": 4, "command_id": "command-1", "idempotency_key": "idem-1",
                "request_hash": request_hash, "sent_at": "2026-01-01T00:00:00Z", "payload": payload,
            }
            transport = FakeTransport([
                self.snapshot(), self.reconciliation_ack(),
                command,
            ])
            adapter = FakeAdapter()
            run_runtime(self.config(Path(tmp) / "journal.sqlite"), lambda: "opaque",
                        adapter=adapter, transport=transport, preflight=True, max_messages=2)
            self.assertEqual([call[0] for call in adapter.calls], ["check", "invoke"])
            self.assertEqual(transport.sent[-1]["type"], "command.result")
            self.assertEqual(transport.sent[-1]["payload"]["state"], "ACCEPTED")
            journal = SQLiteJournal(str(Path(tmp) / "journal.sqlite"), "account-1")
            self.assertEqual(journal.get("command-1").state, "ACCEPTED")
            journal.close()

    def test_unverified_backend_preflight_never_invokes_fake_broker(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = {"symbol": "EURUSD", "sl": "1", "tp": "2"}
            command = {
                "schema_version": 1, "type": "order.submit_market", "message_id": "command-frame",
                "account_id": "account-1", "provider": "MT5", "broker_server": "Demo",
                "external_account_id": "42", "generation": 0, "sequence": 2,
                "execution_epoch": 4, "command_id": "command-1", "idempotency_key": "idem-1",
                "request_hash": canonical_request_hash("order.submit_market", payload),
                "sent_at": "2026-01-01T00:00:00Z", "payload": payload,
            }
            for invalid in (
                {"backend_execution_gate": False},
                {"reconciliation_complete": False},
                {"no_unknown_commands": False},
            ):
                with self.subTest(invalid=invalid):
                    ack = self.reconciliation_ack()
                    ack["payload"].update(invalid)
                    transport = FakeTransport([self.snapshot(), ack, command])
                    adapter = FakeAdapter()
                    run_runtime(self.config(Path(tmp) / (next(iter(invalid)) + ".sqlite")),
                                lambda: "opaque", adapter=adapter, transport=transport,
                                preflight=True, max_messages=2)
                    self.assertEqual(adapter.calls, [])
                    self.assertEqual(transport.sent[-1]["payload"]["code"], "EXECUTION_DISABLED")

    def test_preflight_operator_input_cannot_supply_health_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeAdapter()
            with self.assertRaisesRegex(RuntimeErrorSafe, "PREFLIGHT_REQUIRED"):
                run_runtime(self.config(Path(tmp) / "journal.sqlite"), lambda: "opaque",
                            adapter=adapter, transport=FakeTransport([]),
                            preflight={"demo_account": True})
            self.assertEqual(adapter.calls, [])

    def test_cli_preflight_only_checks_facts_without_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(asdict(self.config(Path(tmp) / "journal.sqlite"))), encoding="utf-8")
            heartbeat = {**self.reconciliation_ack(), "type": "heartbeat_ack",
                         "message_id": "heartbeat-2", "sequence": 2,
                         "idempotency_key": "msg:heartbeat-2", "payload": {}}
            transport = FakeTransport([self.snapshot(), self.reconciliation_ack(), heartbeat])
            adapter = FakeAdapter()
            output = StringIO()
            with redirect_stdout(output):
                main(["--config", str(config_path), "--preflight-only"],
                     secret_provider=lambda: "opaque", transport=transport, adapter=adapter)
            self.assertIn('"preflight": "PASS"', output.getvalue())
            self.assertEqual(adapter.calls, [])

    def test_cli_preflight_only_stops_with_failure_when_backend_gate_is_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(json.dumps(asdict(self.config(Path(tmp) / "journal.sqlite"))), encoding="utf-8")
            ack = self.reconciliation_ack()
            ack["payload"]["backend_execution_gate"] = False
            heartbeat = {**ack, "type": "heartbeat_ack", "message_id": "heartbeat-2",
                         "sequence": 2, "idempotency_key": "msg:heartbeat-2", "payload": {}}
            transport = FakeTransport([self.snapshot(), ack, heartbeat])
            with self.assertRaisesRegex(RuntimeErrorSafe, "PREFLIGHT_FAILED"):
                main(["--config", str(config_path), "--preflight-only"],
                     secret_provider=lambda: "opaque", transport=transport,
                     adapter=FakeAdapter())


if __name__ == "__main__":
    unittest.main()
