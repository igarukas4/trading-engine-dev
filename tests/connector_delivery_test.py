import asyncio
import unittest

from backend.app.connector_delivery import (
    ConnectorDeliveryBridge,
    ConnectorDeliveryRegistry,
    DeliveryError,
    validate_hello,
)
from backend.app.execution import ExecutionCoordinator, ExecutionError, OrderIntent, OutboxEvent, Position, PositionCommand, RiskReservation
from connector.src.mt5_connector.journal import canonical_request_hash


class ConnectorDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = ConnectorDeliveryRegistry()
        await self.registry.open_session("a", 7, "s1")
        self.kw = dict(
            account_id="a", identity={"provider": "mt5", "broker_server": "demo", "external_account_id": "42"},
            connector_generation=7, execution_epoch=3, type="order.submit_market", payload={"pair": "EURUSD"},
        )

    @staticmethod
    def _seed_dispatchable_order(coordinator, *, account_id: str, suffix: str) -> None:
        order_id = "order-" + suffix
        command_id = "command-" + suffix
        signal_id = "signal-" + suffix
        reservation_id = "reservation-" + suffix
        coordinator.orders[order_id] = OrderIntent(
            order_id, account_id, signal_id, "idem-" + suffix,
            "hash-" + suffix, 1, 1, {"symbol": "EURUSD"}, command_id=command_id,
        )
        coordinator.reservations[reservation_id] = RiskReservation(
            reservation_id, account_id, signal_id,
        )
        coordinator._order_reservations[order_id] = reservation_id
        coordinator.events["event-" + suffix] = OutboxEvent(
            "event-" + suffix, account_id, order_id, 1,
        )

    async def test_ordering_and_duplicate_pending_suppression(self):
        first = await self.registry.enqueue(**self.kw, dispatch_sequence=1, command_id="c1", idempotency_key="i1", request_hash="h1")
        second = await self.registry.enqueue(**self.kw, dispatch_sequence=2, command_id="c2", idempotency_key="i2", request_hash="h2")
        self.assertEqual([first.command_id, second.command_id], [
            (await self.registry.next_for_session("a", "s1")).command_id,
            (await self.registry.next_for_session("a", "s1")).command_id,
        ])
        with self.assertRaisesRegex(DeliveryError, "DUPLICATE_PENDING_COMMAND"):
            await self.registry.enqueue(**self.kw, dispatch_sequence=3, command_id="c1", idempotency_key="i1", request_hash="h1")
        with self.assertRaisesRegex(DeliveryError, "OUT_OF_ORDER_DISPATCH"):
            await self.registry.enqueue(**self.kw, dispatch_sequence=2, command_id="c3", idempotency_key="i3", request_hash="h3")

    async def test_wire_sequence_is_independent_from_dispatch_ordering(self):
        first = await self.registry.enqueue(**self.kw, dispatch_sequence=1, command_id="c1", idempotency_key="i1", request_hash="h1")
        ack_sequence = await self.registry.reserve_sequence("a", "s1")
        second = await self.registry.enqueue(**self.kw, dispatch_sequence=2, command_id="c2", idempotency_key="i2", request_hash="h2")
        self.assertEqual(first.as_message()["sequence"], 1)
        self.assertEqual(ack_sequence, 2)
        self.assertEqual(second.as_message()["sequence"], 3)
        self.assertEqual(await self.registry.current_sequence("a"), 3)

        with self.assertRaisesRegex(DeliveryError, "SESSION_ALREADY_ACTIVE"):
            await self.registry.open_session("a", 7, "s2")
        with self.assertRaisesRegex(DeliveryError, "STALE_GENERATION"):
            await self.registry.enqueue(**{**self.kw, "connector_generation": 8}, dispatch_sequence=1, command_id="c", idempotency_key="i", request_hash="h")

    async def test_reconciliation_gate_blocks_side_effects_until_observed(self):
        registry = ConnectorDeliveryRegistry()
        await registry.open_session(
            "a", 7, "s1",
            identity={"provider": "mt5", "broker_server": "demo", "external_account_id": "42"},
            execution_epoch=3,
            reconciliation_required=True,
        )
        with self.assertRaisesRegex(DeliveryError, "RECONCILIATION_REQUIRED"):
            await registry.enqueue(**self.kw, dispatch_sequence=1, command_id="c1", idempotency_key="i1", request_hash="h1")
        await registry.mark_reconciled("a", "s1")
        envelope = await registry.enqueue(**self.kw, dispatch_sequence=1, command_id="c1", idempotency_key="i1", request_hash="h1")
        self.assertEqual(envelope.command_id, "c1")

        envelope = await self.registry.enqueue(**self.kw, dispatch_sequence=1, command_id="c1", idempotency_key="i1", request_hash="h1")
        await self.registry.next_for_session("a", "s1")
        await self.registry.mark_sent("a", envelope.command_id, "s1")
        result = {
            **envelope.as_message(),
            "type": "command.result",
            "message_id": "result-1",
            "payload": {"state": "UNKNOWN"},
        }
        self.assertEqual(await self.registry.record_result(result), "UNKNOWN")
        self.assertEqual(self.registry.pending_state("a", "c1"), "UNKNOWN")
        self.assertEqual(await self.registry.record_result({**result, "message_id": "result-2", "sequence": 2}), "UNKNOWN")
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.registry.next_for_session("a", "s1"), 0.01)

    async def test_wire_contract_uses_sequence_and_round_trips_request_hash(self):
        envelope = await self.registry.enqueue(**self.kw, dispatch_sequence=1, command_id="c1", idempotency_key="i1", request_hash="h1")
        message = envelope.as_message()
        self.assertEqual(message["sequence"], 1)
        self.assertNotIn("dispatch_sequence", message)
        self.assertNotIn("connector_generation", message)
        await self.registry.next_for_session("a", "s1")
        await self.registry.mark_sent("a", "c1", "s1")
        self.assertEqual(await self.registry.record_result({
            **message,
            "type": "command.result",
            "message_id": "result-1",
            "sequence": 1,
            "payload": {"state": "REJECTED", "code": "EXECUTION_DISABLED"},
        }), "REJECTED")

    async def test_inbound_sequence_watermark_survives_reconnect(self):
        message = {
            "schema_version": 1, "type": "heartbeat", "message_id": "hb-1",
            "account_id": "a", "provider": "mt5", "broker_server": "demo",
            "external_account_id": "42", "generation": 7, "sequence": 1,
            "execution_epoch": 3, "command_id": None, "idempotency_key": "msg:hb-1",
            "sent_at": "2026-09-18T00:00:00+00:00", "payload": {},
        }
        await self.registry.accept_inbound("a", "s1", message)
        await self.registry.close_session("a", "s1")
        await self.registry.open_session(
            "a", 7, "s2",
            identity={"provider": "mt5", "broker_server": "demo", "external_account_id": "42"},
            execution_epoch=3,
        )
        accepted = await self.registry.accept_inbound(
            "a", "s2", {**message, "message_id": "hb-2", "sequence": 2, "idempotency_key": "msg:hb-2"},
        )
        self.assertEqual(accepted["sequence"], 2)
        with self.assertRaisesRegex(DeliveryError, "MALFORMED_FRAME"):
            await self.registry.accept_inbound("a", "s2", {**message, "connector_generation": 7})
        with self.assertRaisesRegex(DeliveryError, "MALFORMED_FRAME"):
            await self.registry.accept_inbound("a", "s2", {**message, "message_id": "hb-3", "sequence": 3, "generation": True})
        with self.assertRaisesRegex(DeliveryError, "MALFORMED_FRAME"):
            await self.registry.accept_inbound("a", "s2", {**message, "message_id": "hb-3", "sequence": 3, "schema_version": 1.0})
        with self.assertRaisesRegex(DeliveryError, "MALFORMED_FRAME"):
            await self.registry.accept_inbound("a", "s2", {**message, "message_id": "hb-3", "sequence": 3, "schema_version": 2})
        with self.assertRaisesRegex(DeliveryError, "MALFORMED_FRAME"):
            await self.registry.accept_inbound("a", "s2", {**message, "message_id": "hb-3", "sequence": 3, "foo": "nope"})
        with self.assertRaisesRegex(DeliveryError, "MALFORMED_FRAME"):
            await self.registry.accept_inbound("a", "s2", {**message, "message_id": "hb-3", "sequence": 3, "command_id": "unexpected"})
        with self.assertRaisesRegex(DeliveryError, "REPLAYED_SEQUENCE"):
            await self.registry.accept_inbound("a", "s2", {**message, "message_id": "hb-1"})

    async def test_result_requires_canonical_schema_and_rejects_message_replay(self):
        envelope = await self.registry.enqueue(**self.kw, dispatch_sequence=1, command_id="c1", idempotency_key="i1", request_hash="h1")
        await self.registry.next_for_session("a", "s1")
        await self.registry.mark_sent("a", envelope.command_id, "s1")
        result = {
            **envelope.as_message(),
            "type": "command.result",
            "message_id": "result-1",
            "payload": {"state": "REJECTED", "code": "EXECUTION_DISABLED"},
        }
        with self.assertRaisesRegex(DeliveryError, "MALFORMED_FRAME"):
            await self.registry.record_result({k: v for k, v in result.items() if k != "schema_version"})
        with self.assertRaisesRegex(DeliveryError, "MALFORMED_FRAME"):
            await self.registry.record_result({**result, "type": "command_result"})
        with self.assertRaisesRegex(DeliveryError, "MALFORMED_FRAME"):
            await self.registry.record_result({**result, "schema_version": True})
        with self.assertRaisesRegex(DeliveryError, "MALFORMED_FRAME"):
            await self.registry.record_result({**result, "schema_version": 1.0})
        self.assertEqual(await self.registry.record_result(result), "REJECTED")
        with self.assertRaisesRegex(DeliveryError, "MALFORMED_FRAME"):
            await self.registry.record_result({**result, "message_id": "result-malformed", "command_id": {"not": "hashable"}})
        second = await self.registry.enqueue(**self.kw, dispatch_sequence=2, command_id="c2", idempotency_key="i2", request_hash="h2")
        await self.registry.next_for_session("a", "s1")
        await self.registry.mark_sent("a", second.command_id, "s1")
        duplicate = {
            **second.as_message(),
            "type": "command.result",
            "message_id": result["message_id"],
            "payload": {"state": "REJECTED", "code": "EXECUTION_DISABLED"},
        }
        with self.assertRaisesRegex(DeliveryError, "REPLAYED_SEQUENCE"):
            await self.registry.record_result(duplicate)

        hello = {
            "type": "hello", "account_id": "a", "provider": "MT5",
            "broker_server": "demo", "external_account_id": "42", "key_id": "k",
            "secret": "super-secret", "generation": 7, "session_id": "s1",
        }
        self.assertEqual(validate_hello(hello), hello)
        with self.assertRaisesRegex(DeliveryError, "MALFORMED_FRAME") as error:
            validate_hello({**hello, "extra": "nope"})
        self.assertNotIn("super-secret", str(error.exception))

        registry = ConnectorDeliveryRegistry()
        await registry.open_session(
            "a", 7, "s1",
            identity={"provider": "mt5", "broker_server": "demo", "external_account_id": "42"},
            execution_epoch=3,
        )
        with self.assertRaisesRegex(DeliveryError, "WRONG_ACCOUNT"):
            await registry.enqueue(
                **{**self.kw, "identity": {"provider": "mt5", "broker_server": "other", "external_account_id": "42"}},
                dispatch_sequence=1, command_id="c1", idempotency_key="i1", request_hash="h1",
            )
        self.assertIsNone(registry.pending_state("a", "c1"))

        registry = ConnectorDeliveryRegistry()
        await registry.open_session("a", 7, "s1", execution_epoch=4)
        with self.assertRaisesRegex(DeliveryError, "STALE_EPOCH"):
            await registry.enqueue(
                **{**self.kw, "execution_epoch": 3},
                dispatch_sequence=1, command_id="c1", idempotency_key="i1", request_hash="h1",
            )
        self.assertIsNone(registry.pending_state("a", "c1"))
    async def test_bridge_projects_result_and_keeps_accounts_isolated(self):
        coordinator = ExecutionCoordinator()
        identity = {"provider": "mt5", "broker_server": "demo", "external_account_id": "42"}
        for account_id, suffix in (("a", "1"), ("b", "2")):
            self._seed_dispatchable_order(
                coordinator, account_id=account_id, suffix=suffix,
            )

        registry = ConnectorDeliveryRegistry()
        bridge = ConnectorDeliveryBridge(coordinator, registry)
        for account_id, suffix in (("a", "1"), ("b", "2")):
            await registry.open_session(account_id, 1, "session-" + suffix, identity=identity, execution_epoch=1)
            await bridge.enqueue_order(
                account_id, "order-" + suffix, identity=identity, generation=1,
            )

        first = await registry.next_for_session("a", "session-1")
        await bridge.mark_sent("a", first.command_id, "session-1")
        result = {
            **first.as_message(), "type": "command.result", "message_id": "result-a",
            "sequence": 1, "payload": {"state": "UNKNOWN"},
        }
        await bridge.record_result(result, authenticated_account_id="a", session_id="session-1")
        self.assertEqual(coordinator.orders["order-1"].status, "UNKNOWN")
        self.assertEqual(coordinator.orders["order-2"].status, "INTENT")
        self.assertEqual(coordinator.runtime_interlock("a").status, "BLOCKED")
        self.assertEqual(coordinator.runtime_interlock("b").status, "ELIGIBLE")

    async def test_position_commands_use_durable_bridge_and_project_account_locally(self):
        coordinator = ExecutionCoordinator()
        identity = {"provider": "mt5", "broker_server": "demo", "external_account_id": "42"}
        self._seed_dispatchable_order(coordinator, account_id="a", suffix="position")
        coordinator.positions[("a", "order-position")] = Position(
            "a", "order-position", "0.10", "CONFIRMED", pair="EURUSD",
            direction="LONG", external_position_id="ticket-1",
        )
        modify = coordinator.request_position_modify_protection(
            "a", "order-position", "1.0", "2.0", "EURUSD", "operator",
            confirmed=True, idempotency_key="modify-key",
        )
        close = coordinator.request_position_close(
            "a", "order-position", "0.05", "EURUSD", "operator",
            confirmed=True, idempotency_key="close-key",
        )
        registry = ConnectorDeliveryRegistry()
        bridge = ConnectorDeliveryBridge(coordinator, registry)
        await registry.open_session("a", 0, "session", identity=identity, execution_epoch=1)
        modify_record = await bridge.enqueue_position_modify_protection(
            "a", modify.id, identity=identity, generation=0,
        )
        close_record = await bridge.enqueue_position_close(
            "a", close.id, identity=identity, generation=0,
        )
        self.assertEqual(modify_record.command_type, "position.modify_protection")
        self.assertEqual(modify_record.payload["position_ticket"], "ticket-1")
        self.assertEqual(close_record.command_type, "position.close")
        self.assertEqual(close_record.payload["position_ticket"], "ticket-1")
        self.assertEqual(modify_record.request_hash, canonical_request_hash(modify_record.command_type, modify_record.payload))
        self.assertEqual(close_record.request_hash, canonical_request_hash(close_record.command_type, close_record.payload))
        first = await registry.next_for_session("a", "session")
        await bridge.mark_sent("a", first.command_id, "session")
        await bridge.record_result({
            **first.as_message(), "type": "command.result", "message_id": "result-modify",
            "sequence": 1, "payload": {
                "state": "ACCEPTED", "protection_confirmed": True,
                "confirmed_stop": "1.0", "confirmed_take_profit": "2.0",
            },
        }, authenticated_account_id="a", session_id="session")
        self.assertEqual(modify.status, "CONFIRMED")
        self.assertEqual(coordinator.position("a", "order-position").native_stop_loss, "1.0")
        second = await registry.next_for_session("a", "session")
        await bridge.mark_sent("a", second.command_id, "session")
        await bridge.record_result({
            **second.as_message(), "type": "command.result", "message_id": "result-close",
            "sequence": 2, "payload": {"state": "ACCEPTED", "filled_volume": "0.05"},
        }, authenticated_account_id="a", session_id="session")
        self.assertEqual(close.status, "CONFIRMED")
        self.assertEqual(coordinator.position("a", "order-position").remaining_volume, "0.05")

    async def test_position_dispatch_requires_broker_ticket_and_supported_type(self):
        coordinator = ExecutionCoordinator()
        identity = {"provider": "mt5", "broker_server": "demo", "external_account_id": "42"}
        self._seed_dispatchable_order(coordinator, account_id="a", suffix="no-ticket")
        coordinator.positions[("a", "order-no-ticket")] = Position(
            "a", "order-no-ticket", "0.10", "CONFIRMED", pair="EURUSD", direction="LONG",
        )
        close = coordinator.request_position_close(
            "a", "order-no-ticket", "0.05", "EURUSD", "operator", confirmed=True,
        )
        with self.assertRaisesRegex(ExecutionError, "POSITION_TICKET_REQUIRED"):
            coordinator.prepare_connector_position_dispatch("a", close.id, identity=identity, generation=0)
        self.assertNotIn(close.id, coordinator.dispatch_records)
        coordinator.position("a", "order-no-ticket").external_position_id = "ticket-1"
        unsupported = PositionCommand("unsupported", "a", "order-no-ticket", "TRAIL", None)
        coordinator.position_commands.append(unsupported)
        with self.assertRaisesRegex(ExecutionError, "UNSUPPORTED_POSITION_COMMAND"):
            coordinator.prepare_connector_position_dispatch("a", unsupported.id, identity=identity, generation=0)
        self.assertNotIn(unsupported.id, coordinator.dispatch_records)

    async def test_position_dispatch_rejects_identity_values_not_bound_to_broker_account(self):
        actual = {"provider": "mt5", "broker_server": "demo", "external_account_id": "42"}
        coordinator = ExecutionCoordinator(account_identity_provider=lambda _account_id: actual)
        self._seed_dispatchable_order(coordinator, account_id="a", suffix="identity")
        coordinator.positions[("a", "order-identity")] = Position(
            "a", "order-identity", "0.10", "CONFIRMED", pair="EURUSD",
            direction="LONG", external_position_id="ticket-identity",
        )
        command = coordinator.request_position_close(
            "a", "order-identity", "0.05", "EURUSD", "operator", confirmed=True,
        )
        with self.assertRaisesRegex(ExecutionError, "ACCOUNT_IDENTITY_MISMATCH"):
            coordinator.prepare_connector_position_dispatch(
                "a", command.id,
                identity={"provider": "mt5", "broker_server": "other", "external_account_id": "42"},
                generation=0,
            )
        self.assertNotIn(command.id, coordinator.dispatch_records)

    async def test_unsent_dispatch_replays_after_restart(self):
        from tempfile import TemporaryDirectory

        identity = {"provider": "mt5", "broker_server": "demo", "external_account_id": "42"}
        with TemporaryDirectory() as directory:
            coordinator = ExecutionCoordinator(state_path=directory + "/execution.json")
            self._seed_dispatchable_order(
                coordinator, account_id="a", suffix="restart",
            )
            record = coordinator.prepare_connector_dispatch(
                "a", "order-restart", identity=identity, generation=1,
            )
            restarted = ExecutionCoordinator(state_path=directory + "/execution.json")
            registry = ConnectorDeliveryRegistry()
            await registry.open_session("a", 1, "session", identity=identity, execution_epoch=1)
            delivered = await ConnectorDeliveryBridge(restarted, registry).replay_unsent("a")
            self.assertEqual([item.command_id for item in delivered], [record.command_id])


if __name__ == "__main__":
    unittest.main()
