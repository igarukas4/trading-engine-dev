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
from mt5_connector.adapter import AdapterError
from mt5_connector.preflight import PreflightError
from backend.app.connector_delivery import ConnectorDeliveryBridge, ConnectorDeliveryRegistry
from backend.app.execution import ExecutionCoordinator, OrderIntent, OutboxEvent, Position, RiskReservation


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


class RegistryWssTransport:
    """Fake WSS server that owns delivery and result projection end to end."""

    def __init__(self, registry, bridge, snapshot, acknowledgement, account_id, session_id):
        self.registry = registry
        self.bridge = bridge
        self.snapshot = snapshot
        self.acknowledgement = acknowledgement
        self.account_id = account_id
        self.session_id = session_id
        self.frames = [snapshot, acknowledgement]
        self.sent = []

    async def send(self, message):
        frame = json.loads(message)
        self.sent.append(frame)
        if frame.get("type") == "command.result":
            await self.bridge.record_result(
                frame,
                authenticated_account_id=self.account_id,
                session_id=self.session_id,
            )

    async def recv(self):
        if self.frames:
            return self.frames.pop(0)
        envelope = await self.registry.next_for_session(self.account_id, self.session_id)
        await self.bridge.mark_sent(self.account_id, envelope.command_id, self.session_id)
        return envelope.as_message()

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
        result = {"retcode": 10009, "external_order_id": "fake-order",
                  "readback_confirmed": True}
        if typ == "position.modify_protection":
            result.update({
                "protection_confirmed": True,
                "readback_confirmed": True,
                "confirmed_stop": payload.get("sl"),
                "confirmed_take_profit": payload.get("tp"),
            })
        if typ == "position.close":
            # A fake broker must provide the same read-back fact required by
            # the real adapter. The requested volume is intent, not evidence.
            result["filled_volume"] = payload.get("volume")
        return result


class FailingInitializeAdapter(FakeAdapter):
    def initialize(self, terminal_path):
        raise AdapterError("SECRET_MARKER terminal detail")


class FailingPreflightAdapter(FakeAdapter):
    def preflight_facts(self):
        raise AdapterError("SECRET_MARKER account detail")


class AmbiguousAdapter(FakeAdapter):
    def invoke(self, typ, payload):
        self.calls.append(("invoke", typ, payload))
        raise OSError("SECRET_MARKER transport detail")


class TicketCheckingAdapter(FakeAdapter):
    def order_check(self, typ, payload):
        self.calls.append(("check", typ, payload))
        if typ.startswith("position.") and payload.get("position_ticket") != "ticket-1":
            return {"retcode": 10016}
        return {"retcode": 0}


class Issue70RuntimeTests(unittest.TestCase):
    def config(self, path, **updates):
        raw = {
            "account_id": "account-1", "provider": "MT5", "broker_server": "Demo",
            "external_account_id": "42", "key_id": "key-1", "secret_ref": "file:///outside/secret",
            "terminal_path": "C:/terminal64.exe", "wss_url": "wss://backend/ws",
            "journal_path": str(path), "execution_disabled": False,
            "local_test": True,
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

    def command_frame(self, payload=None, *, command_id="command-1", key="idem-1",
                      generation=0, epoch=4, sequence=2, typ="order.submit_market"):
        payload = payload or {"symbol": "EURUSD", "sl": "1", "tp": "2"}
        return {
            "schema_version": 1, "type": typ, "message_id": "command-frame-" + command_id,
            "account_id": "account-1", "provider": "MT5", "broker_server": "Demo",
            "external_account_id": "42", "generation": generation, "sequence": sequence,
            "execution_epoch": epoch, "command_id": command_id, "idempotency_key": key,
            "request_hash": canonical_request_hash(typ, payload),
            "sent_at": "2026-01-01T00:00:00Z", "payload": payload,
        }

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

    def test_production_runtime_composes_position_commands_and_projects_results(self):
        identity = {"provider": "MT5", "broker_server": "Demo", "external_account_id": "42"}
        for command_kind in ("PROTECTION", "CLOSE"):
            with self.subTest(command_kind=command_kind), tempfile.TemporaryDirectory() as tmp:
                coordinator = ExecutionCoordinator()
                coordinator.account("account-1").execution_epoch = 4
                order_id = "order-" + command_kind.lower()
                coordinator.orders[order_id] = OrderIntent(
                    order_id, "account-1", "signal-" + command_kind.lower(),
                    "order-key-" + command_kind.lower(), "order-hash", 1, 1,
                    {"symbol": "EURUSD"}, command_id="order-command-" + command_kind.lower(),
                )
                coordinator.reservations["reservation-" + command_kind.lower()] = RiskReservation(
                    "reservation-" + command_kind.lower(), "account-1", "signal-" + command_kind.lower(),
                )
                coordinator._order_reservations[order_id] = "reservation-" + command_kind.lower()
                coordinator.events["event-" + command_kind.lower()] = OutboxEvent(
                    "event-" + command_kind.lower(), "account-1", order_id, 1,
                )
                coordinator.positions[("account-1", order_id)] = Position(
                    "account-1", order_id, "0.10", "CONFIRMED", pair="EURUSD",
                    direction="LONG", external_position_id="ticket-" + command_kind.lower(),
                )
                if command_kind == "PROTECTION":
                    command = coordinator.request_position_modify_protection(
                        "account-1", order_id, "1.0", "2.0", "EURUSD", "operator",
                        confirmed=True, idempotency_key="key-protection",
                    )
                else:
                    command = coordinator.request_position_close(
                        "account-1", order_id, "0.05", "EURUSD", "operator",
                        confirmed=True, idempotency_key="key-close",
                    )

                registry = ConnectorDeliveryRegistry()
                bridge = ConnectorDeliveryBridge(coordinator, registry)
                asyncio.run(registry.open_session(
                    "account-1", 0, "session-" + command_kind.lower(),
                    identity=identity, execution_epoch=4,
                ))
                record = asyncio.run(bridge.enqueue_position_command(
                    "account-1", command.id, identity=identity, generation=0,
                ))
                envelope = asyncio.run(registry.next_for_session(
                    "account-1", "session-" + command_kind.lower(),
                ))
                asyncio.run(bridge.mark_sent(
                    "account-1", command.id, "session-" + command_kind.lower(),
                ))
                incoming = envelope.as_message()
                incoming["sequence"] = 2
                incoming["message_id"] = "command-frame-" + command_kind.lower()
                transport = FakeTransport([self.snapshot(), self.reconciliation_ack(), incoming])
                adapter = FakeAdapter()
                run_runtime(
                    self.config(Path(tmp) / "journal.sqlite"), lambda: "opaque",
                    adapter=adapter, transport=transport, preflight=True, max_messages=2,
                )
                result = transport.sent[-1]
                asyncio.run(bridge.record_result(
                    result, authenticated_account_id="account-1",
                    session_id="session-" + command_kind.lower(),
                ))
                self.assertEqual(record.command_type, {
                    "PROTECTION": "position.modify_protection", "CLOSE": "position.close",
                }[command_kind])
                self.assertEqual(record.payload["position_ticket"], "ticket-" + command_kind.lower())
                self.assertEqual([item[0] for item in adapter.calls], ["check", "invoke"])
                self.assertEqual(adapter.calls[0][1], record.command_type)
                self.assertEqual(adapter.calls[1][1], record.command_type)
                self.assertEqual(result["payload"]["state"], "ACCEPTED")
                self.assertEqual(command.status, "CONFIRMED")
                if command_kind == "PROTECTION":
                    self.assertEqual(coordinator.position("account-1", order_id).native_stop_loss, "1.0")
                else:
                    self.assertEqual(coordinator.position("account-1", order_id).remaining_volume, "0.05")

    def test_three_commands_cross_registry_wss_sqlite_adapter_and_projection(self):
        identity = {"provider": "MT5", "broker_server": "Demo", "external_account_id": "42"}
        coordinator = ExecutionCoordinator()
        coordinator.account("account-1").execution_epoch = 4
        coordinator.bind_account_identity("account-1", identity)
        coordinator.orders["entry-order"] = OrderIntent(
            "entry-order", "account-1", "entry-signal", "entry-key",
            canonical_request_hash("order.submit_market", {
                "symbol": "EURUSD", "volume": "0.01", "sl": "1", "tp": "2",
            }), 4, 1, {"symbol": "EURUSD", "volume": "0.01", "sl": "1", "tp": "2"},
            command_id="entry-command",
        )
        coordinator.reservations["entry-reservation"] = RiskReservation(
            "entry-reservation", "account-1", "entry-signal",
        )
        coordinator._order_reservations["entry-order"] = "entry-reservation"
        coordinator.events["entry-event"] = OutboxEvent("entry-event", "account-1", "entry-order", 1)
        coordinator.orders["position-order"] = OrderIntent(
            "position-order", "account-1", "position-signal", "position-key", "position-hash",
            4, 0, {"symbol": "EURUSD"}, command_id="position-command-source",
        )
        coordinator.positions[("account-1", "position-order")] = Position(
            "account-1", "position-order", "0.10", "CONFIRMED", pair="EURUSD",
            direction="LONG", external_position_id="ticket-position",
        )
        coordinator.account("account-1").next_dispatch_sequence = 2
        modify = coordinator.request_position_modify_protection(
            "account-1", "position-order", "1.0", "2.0", "EURUSD", "operator",
            confirmed=True, idempotency_key="modify-key",
        )
        close = coordinator.request_position_close(
            "account-1", "position-order", "0.05", "EURUSD", "operator",
            confirmed=True, idempotency_key="close-key",
        )
        registry = ConnectorDeliveryRegistry()
        bridge = ConnectorDeliveryBridge(coordinator, registry)
        session_id = "session-end-to-end"
        asyncio.run(registry.open_session(
            "account-1", 0, session_id, identity=identity, execution_epoch=4,
        ))
        asyncio.run(registry.reserve_sequence("account-1", session_id))
        asyncio.run(bridge.enqueue_order("account-1", "entry-order", identity=identity, generation=0))
        asyncio.run(bridge.enqueue_position_command("account-1", modify.id, identity=identity, generation=0))
        asyncio.run(bridge.enqueue_position_command("account-1", close.id, identity=identity, generation=0))
        transport = RegistryWssTransport(
            registry, bridge, self.snapshot(), self.reconciliation_ack(), "account-1", session_id,
        )
        adapter = FakeAdapter()
        with tempfile.TemporaryDirectory() as tmp:
            run_runtime(
                self.config(Path(tmp) / "journal.sqlite"), lambda: "opaque",
                adapter=adapter, transport=transport, preflight=True, max_messages=4,
            )
        results = [frame for frame in transport.sent if frame.get("type") == "command.result"]
        self.assertEqual(len(results), 3)
        self.assertEqual([frame["payload"]["state"] for frame in results], ["ACCEPTED"] * 3)
        self.assertEqual([item[0] for item in adapter.calls], [
            "check", "invoke", "check", "invoke", "check", "invoke",
        ])
        self.assertEqual(coordinator.orders["entry-order"].status, "SUBMITTED")
        self.assertEqual(modify.status, "CONFIRMED")
        self.assertEqual(close.status, "CONFIRMED")
        self.assertEqual(coordinator.position("account-1", "position-order").remaining_volume, "0.05")

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

    def test_adapter_initialize_failure_is_stable_and_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeErrorSafe, "TERMINAL_UNAVAILABLE") as error:
                run_runtime(self.config(Path(tmp) / "journal.sqlite"), lambda: "opaque",
                            adapter=FailingInitializeAdapter(), transport=FakeTransport([]),
                            preflight=True)
            self.assertNotIn("SECRET_MARKER", str(error.exception))

    def test_adapter_preflight_failure_is_stable_and_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            frames = [self.snapshot(), self.reconciliation_ack()]
            with self.assertRaisesRegex(RuntimeErrorSafe, "PREFLIGHT_FAILED") as error:
                run_runtime(self.config(Path(tmp) / "journal.sqlite"), lambda: "opaque",
                            adapter=FailingPreflightAdapter(), transport=FakeTransport(frames),
                            preflight=True, preflight_only=True, max_messages=1)
            self.assertNotIn("SECRET_MARKER", str(error.exception))

    def test_invalid_journal_path_is_stable_and_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter = FakeAdapter()
            with self.assertRaisesRegex(RuntimeErrorSafe, "JOURNAL_UNAVAILABLE") as error:
                run_runtime(self.config(Path(tmp) / "missing" / "journal.sqlite"), lambda: "opaque",
                            adapter=adapter, transport=FakeTransport([]), preflight=True)
            self.assertNotIn(str(Path(tmp)), str(error.exception))
            self.assertEqual(adapter.calls, [])

    def test_runtime_rejects_stale_identity_generation_and_epoch_without_invoke(self):
        cases = (
            ("identity", {"snapshot": {"account_id": "account-1", "identity": {
                "provider": "MT5", "broker_server": "Other", "external_account_id": "42"},
                "environment": "DEMO", "execution_mode": "MANUAL"}}, 0, 4),
            ("generation", None, 1, 4),
            ("epoch", None, 0, 3),
        )
        for name, snapshot_update, generation, epoch in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                snapshot = self.snapshot()
                if snapshot_update is not None:
                    snapshot.update(snapshot_update)
                if name == "generation":
                    snapshot["generation"] = 1
                ack = self.reconciliation_ack()
                ack["generation"] = generation if name == "generation" else 0
                command = self.command_frame(generation=generation, epoch=epoch)
                adapter = FakeAdapter()
                transport = FakeTransport([snapshot, ack, command])
                run_runtime(self.config(Path(tmp) / "journal.sqlite"), lambda: "opaque",
                            adapter=adapter, transport=transport, preflight=True, max_messages=2)
                self.assertEqual(adapter.calls, [])
                self.assertEqual(transport.sent[-1]["payload"]["state"], "REJECTED")
                if name == "epoch":
                    self.assertEqual(transport.sent[-1]["payload"]["code"], "STALE_EPOCH")

    def test_runtime_rejects_wrong_account_hash_and_ticket_without_side_effect(self):
        cases = ("account", "hash", "ticket")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                if case == "ticket":
                    payload = {
                        "symbol": "EURUSD", "position_ticket": "wrong-ticket",
                        "sl": "1", "tp": "2",
                    }
                    command = self.command_frame(
                        payload=payload, typ="position.modify_protection",
                    )
                    adapter = TicketCheckingAdapter()
                else:
                    command = self.command_frame()
                    adapter = FakeAdapter()
                    if case == "account":
                        command["account_id"] = "foreign-account"
                    else:
                        command["request_hash"] = "wrong-hash"
                transport = FakeTransport([self.snapshot(), self.reconciliation_ack(), command])
                if case == "account":
                    with self.assertRaises(Exception):
                        run_runtime(
                            self.config(Path(tmp) / "journal.sqlite"), lambda: "opaque",
                            adapter=adapter, transport=transport, preflight=True, max_messages=2,
                        )
                elif case == "hash":
                    run_runtime(
                        self.config(Path(tmp) / "journal.sqlite"), lambda: "opaque",
                        adapter=adapter, transport=transport, preflight=True, max_messages=2,
                    )
                    self.assertEqual(transport.sent[-1]["payload"]["code"], "REQUEST_HASH_MISMATCH")
                else:
                    run_runtime(
                        self.config(Path(tmp) / "journal.sqlite"), lambda: "opaque",
                        adapter=adapter, transport=transport, preflight=True, max_messages=2,
                    )
                    self.assertEqual(transport.sent[-1]["payload"]["code"], "ORDER_CHECK_REJECTED")
                self.assertEqual([item[0] for item in adapter.calls], [] if case != "ticket" else ["check"])

    def test_post_invocation_disconnect_stays_unknown_across_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal_path = Path(tmp) / "journal.sqlite"
            adapter = AmbiguousAdapter()
            transport = FakeTransport([self.snapshot(), self.reconciliation_ack(), self.command_frame()])
            run_runtime(self.config(journal_path), lambda: "opaque", adapter=adapter,
                        transport=transport, preflight=True, max_messages=2)
            self.assertEqual([item[0] for item in adapter.calls], ["check", "invoke"])
            self.assertEqual(transport.sent[-1]["payload"]["state"], "UNKNOWN")
            restarted_adapter = FakeAdapter()
            with self.assertRaisesRegex(RuntimeErrorSafe, "RECONCILIATION_REQUIRED") as error:
                run_runtime(self.config(journal_path), lambda: "opaque", adapter=restarted_adapter,
                            transport=FakeTransport([]), preflight=True)
            self.assertNotIn("SECRET_MARKER", str(error.exception))
            self.assertEqual(restarted_adapter.calls, [])

    def test_duplicate_delivery_replays_durable_result_without_second_invoke(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.command_frame()
            duplicate = self.command_frame(sequence=3)
            duplicate["message_id"] = "duplicate-command-frame"
            transport = FakeTransport([self.snapshot(), self.reconciliation_ack(), first, duplicate])
            adapter = FakeAdapter()
            run_runtime(self.config(Path(tmp) / "journal.sqlite"), lambda: "opaque",
                        adapter=adapter, transport=transport, preflight=True, max_messages=3)
            self.assertEqual([item[0] for item in adapter.calls], ["check", "invoke"])
            self.assertEqual(transport.sent[-1]["payload"]["state"], "ACCEPTED")
            self.assertEqual(transport.sent[-1]["command_id"], "command-1")

    def test_idempotency_key_reuse_with_new_hash_never_invokes_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.command_frame()
            changed = self.command_frame(
                payload={"symbol": "EURUSD", "sl": "1.1", "tp": "2"}, sequence=3,
            )
            changed["message_id"] = "changed-command-frame"
            transport = FakeTransport([self.snapshot(), self.reconciliation_ack(), first, changed])
            adapter = FakeAdapter()
            with self.assertRaisesRegex(Exception, "IDEMPOTENCY_KEY_REUSED"):
                run_runtime(self.config(Path(tmp) / "journal.sqlite"), lambda: "opaque",
                            adapter=adapter, transport=transport, preflight=True, max_messages=3)
            self.assertEqual([item[0] for item in adapter.calls], ["check", "invoke"])

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
