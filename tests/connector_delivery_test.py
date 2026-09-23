import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from backend.app.connector_delivery import (
    ConnectorDeliveryBridge,
    ConnectorDeliveryRegistry,
    DeliveryError,
    validate_hello,
)
from backend.app.broker_accounts import AccountRegistry, BrokerAccount
from backend.app.execution import ExecutionCoordinator, ExecutionError, OrderIntent, OutboxEvent, Position, PositionCommand, RiskReservation
from backend.app.lifecycle import LifecycleCoordinator
from connector.src.mt5_connector.journal import canonical_request_hash


class ConnectorDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = ConnectorDeliveryRegistry()
        await self.registry.open_session("a", 7, "s1", execution_gate_open=True)
        self.kw = dict(
            account_id="a", identity={"provider": "mt5", "broker_server": "demo", "external_account_id": "42"},
            connector_generation=7, execution_epoch=3, type="order.submit_market", payload={"pair": "EURUSD"},
        )

    @staticmethod
    def _seed_dispatchable_order(coordinator, *, account_id: str, suffix: str) -> None:
        # Entry tests explicitly opt into the lifecycle-open gate. New
        # ExecutionCoordinator accounts are blocked by default.
        coordinator.set_lifecycle_gate(account_id, True)
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

    def test_disconnect_invalidates_only_matching_authoritative_session(self):
        accounts = AccountRegistry()
        account = accounts.register(
            provider="MT5", broker_server="Demo", external_account_id="42",
            display_name="Disconnect", environment="DEMO",
        )
        account.connector_healthy = True
        account.reconciliation_complete = True
        account.lease_owner = "session-1"
        account.connector_session_id = "session-1"
        account.connector_session_generation = 3
        accounts.close_connector_session(account.id, "stale-session", 3)
        self.assertTrue(account.connector_healthy)
        accounts.close_connector_session(account.id, "session-1", 3)
        self.assertFalse(account.connector_healthy)
        self.assertIsNone(account.connector_session_id)
        self.assertIsNone(account.lease_owner)
        self.assertFalse(account.reconciliation_complete)

    def test_readiness_rejects_stale_authority_facts_and_quarantine(self):
        account = BrokerAccount("MT5", "Demo", "43", "Readiness")
        account.connector_healthy = True
        account.lease_owner = "expired-session"
        account.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        account.reconciliation_complete = False
        execution = ExecutionCoordinator()
        execution.account(account.id).runtime_interlock = "QUARANTINED"
        lifecycle = LifecycleCoordinator(
            active_session_check=lambda account_id, generation, session_id: False,
        )
        readiness = lifecycle.readiness_context(account, None, execution)
        self.assertFalse(readiness.allowed)
        self.assertFalse(readiness.lease_current)
        self.assertFalse(readiness.generation_current)
        self.assertIn("CONNECTOR_LEASE_STALE", readiness.reason_codes)
        self.assertIn("CONNECTOR_GENERATION_STALE", readiness.reason_codes)
        self.assertIn("BROKER_FACTS_STALE", readiness.reason_codes)
        self.assertIn("RUNTIME_INTERLOCK_BLOCKED", readiness.reason_codes)

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

    async def test_execution_gate_requires_active_session_control_ack(self):
        registry = ConnectorDeliveryRegistry()
        await registry.open_session(
            "a", 7, "s1", identity=self.kw["identity"], execution_epoch=3,
            reconciliation_required=False,
        )
        self.assertTrue(registry.active_session("a", generation=7, session_id="s1"))
        self.assertFalse(registry.active_session("a", generation=8, session_id="s1"))
        with self.assertRaisesRegex(DeliveryError, "EXECUTION_GATE_CLOSED"):
            await registry.enqueue(**self.kw, dispatch_sequence=1, command_id="c1", idempotency_key="i1", request_hash="h1")

        update = await registry.publish_execution_gate(
            "a", generation=7, execution_epoch=3, allowed=True,
        )
        self.assertEqual(update.type, "execution_gate.update")
        self.assertFalse(registry.execution_gate("a"))
        await registry.mark_sent("a", update.command_id, "s1")
        waiting = asyncio.create_task(
            registry.wait_for_control_ack("a", update.command_id, timeout=0.5),
        )
        await asyncio.sleep(0)
        self.assertFalse(waiting.done())
        await registry.acknowledge_execution_gate(
            "a", "s1", update.command_id, generation=7, execution_epoch=3,
        )
        await waiting
        self.assertTrue(registry.execution_gate("a"))
        envelope = await registry.enqueue(
            **self.kw, dispatch_sequence=1, command_id="c1", idempotency_key="i1", request_hash="h1",
        )
        self.assertEqual(envelope.execution_epoch, 3)

    async def test_closed_entry_gate_preserves_explicit_reduce_only_delivery(self):
        registry = ConnectorDeliveryRegistry()
        await registry.open_session(
            "a", 7, "s1", identity=self.kw["identity"], execution_epoch=3,
        )
        exit_envelope = await registry.enqueue(
            **{
                **self.kw,
                "type": "position.close",
                "payload": {"position_ticket": "ticket-1", "volume": "0.10"},
            },
            dispatch_sequence=1, command_id="exit-1", idempotency_key="exit-1", request_hash="exit-hash",
        )
        self.assertEqual(exit_envelope.type, "position.close")

    async def test_gate_update_closes_immediately_and_stale_session_cannot_ack(self):
        registry = ConnectorDeliveryRegistry()
        await registry.open_session(
            "a", 7, "s1", identity=self.kw["identity"], execution_epoch=3,
            reconciliation_required=False,
        )
        opening = await registry.publish_execution_gate(
            "a", generation=7, execution_epoch=3, allowed=True,
        )
        await registry.mark_sent("a", opening.command_id, "s1")
        await registry.acknowledge_execution_gate("a", "s1", opening.command_id, generation=7, execution_epoch=3)
        closing = await registry.publish_execution_gate(
            "a", generation=7, execution_epoch=4, allowed=False,
        )
        self.assertFalse(registry.execution_gate("a"))
        with self.assertRaisesRegex(DeliveryError, "SESSION_NOT_ACTIVE"):
            await registry.acknowledge_execution_gate(
                "a", "old-session", closing.command_id, generation=7, execution_epoch=4,
            )
        await registry.mark_sent("a", closing.command_id, "s1")
        await registry.acknowledge_execution_gate("a", "s1", closing.command_id, generation=7, execution_epoch=4)
        self.assertEqual(registry.session_epoch("a"), 4)

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
        update = await registry.publish_execution_gate(
            "a", generation=7, execution_epoch=3, allowed=True,
        )
        await registry.mark_sent("a", update.command_id, "s1")
        await registry.acknowledge_execution_gate(
            "a", "s1", update.command_id, generation=7, execution_epoch=3,
        )
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
            execution_gate_open=True,
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
            execution_gate_open=True,
        )
        with self.assertRaisesRegex(DeliveryError, "WRONG_ACCOUNT"):
            await registry.enqueue(
                **{**self.kw, "identity": {"provider": "mt5", "broker_server": "other", "external_account_id": "42"}},
                dispatch_sequence=1, command_id="c1", idempotency_key="i1", request_hash="h1",
            )
        self.assertIsNone(registry.pending_state("a", "c1"))

        registry = ConnectorDeliveryRegistry()
        await registry.open_session("a", 7, "s1", execution_epoch=4, execution_gate_open=True)
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
            await registry.open_session(account_id, 1, "session-" + suffix, identity=identity, execution_epoch=1, execution_gate_open=True)
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

    async def test_entry_acceptance_without_readback_stays_unknown(self):
        coordinator = ExecutionCoordinator()
        identity = {"provider": "mt5", "broker_server": "demo", "external_account_id": "42"}
        self._seed_dispatchable_order(coordinator, account_id="a", suffix="missing-readback")
        registry = ConnectorDeliveryRegistry()
        bridge = ConnectorDeliveryBridge(coordinator, registry)
        await registry.open_session("a", 1, "session", identity=identity, execution_epoch=1, execution_gate_open=True)
        await bridge.enqueue_order("a", "order-missing-readback", identity=identity, generation=1)
        envelope = await registry.next_for_session("a", "session")
        await bridge.mark_sent("a", envelope.command_id, "session")
        await bridge.record_result({
            **envelope.as_message(), "type": "command.result", "message_id": "result-missing-readback",
            "sequence": 1, "payload": {"state": "ACCEPTED"},
        }, authenticated_account_id="a", session_id="session")
        self.assertEqual(coordinator.orders["order-missing-readback"].status, "UNKNOWN")

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
        await registry.open_session("a", 0, "session", identity=identity, execution_epoch=1, execution_gate_open=True)
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
                "state": "ACCEPTED", "readback_confirmed": True,
                "protection_confirmed": True,
                "confirmed_stop": "1.0", "confirmed_take_profit": "2.0",
            },
        }, authenticated_account_id="a", session_id="session")
        self.assertEqual(modify.status, "CONFIRMED")
        self.assertEqual(coordinator.position("a", "order-position").native_stop_loss, "1.0")
        second = await registry.next_for_session("a", "session")
        await bridge.mark_sent("a", second.command_id, "session")
        await bridge.record_result({
            **second.as_message(), "type": "command.result", "message_id": "result-close",
            "sequence": 2, "payload": {
                "state": "ACCEPTED", "readback_confirmed": True, "filled_volume": "0.05",
            },
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

    async def test_close_acceptance_without_broker_volume_stays_unknown(self):
        coordinator = ExecutionCoordinator()
        identity = {"provider": "mt5", "broker_server": "demo", "external_account_id": "42"}
        self._seed_dispatchable_order(coordinator, account_id="a", suffix="no-volume")
        coordinator.positions[("a", "order-no-volume")] = Position(
            "a", "order-no-volume", "0.10", "CONFIRMED", pair="EURUSD",
            direction="LONG", external_position_id="ticket-no-volume",
        )
        close = coordinator.request_position_close(
            "a", "order-no-volume", "0.05", "EURUSD", "operator", confirmed=True,
        )
        registry = ConnectorDeliveryRegistry()
        bridge = ConnectorDeliveryBridge(coordinator, registry)
        await registry.open_session("a", 0, "session", identity=identity, execution_epoch=1, execution_gate_open=True)
        record = await bridge.enqueue_position_close(
            "a", close.id, identity=identity, generation=0,
        )
        envelope = await registry.next_for_session("a", "session")
        await bridge.mark_sent("a", envelope.command_id, "session")
        await bridge.record_result({
            **envelope.as_message(), "type": "command.result", "message_id": "result-no-volume",
            "sequence": 1, "payload": {"state": "ACCEPTED"},
        }, authenticated_account_id="a", session_id="session")
        self.assertEqual(close.status, "UNKNOWN")
        self.assertEqual(coordinator.position("a", "order-no-volume").remaining_volume, "0.10")

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
            await registry.open_session("a", 1, "session", identity=identity, execution_epoch=1, execution_gate_open=True)
            delivered = await ConnectorDeliveryBridge(restarted, registry).replay_unsent("a")
            self.assertEqual([item.command_id for item in delivered], [record.command_id])

    async def test_stop_commit_cannot_promote_stale_entry_to_sent(self):
        """A committed stop wins over a sender that already dequeued an entry."""
        coordinator = ExecutionCoordinator()
        identity = {"provider": "mt5", "broker_server": "demo", "external_account_id": "42"}
        self._seed_dispatchable_order(coordinator, account_id="a", suffix="race")
        coordinator.account("a").exposure_gate = "OPEN"
        coordinator.account("a").execution_epoch = 1
        registry = ConnectorDeliveryRegistry()
        bridge = ConnectorDeliveryBridge(coordinator, registry)
        await registry.open_session("a", 1, "session", identity=identity, execution_epoch=1, execution_gate_open=True)
        await bridge.enqueue_order("a", "order-race", identity=identity, generation=1)
        envelope = await registry.next_for_session("a", "session")

        # This models the lifecycle transaction having committed before the
        # asynchronous WSS sender calls mark_sent. The entry must never reach
        # SENT, even before the registry's cleanup callback runs.
        coordinator.apply_lifecycle_commit("a", 2, allowed=False, persist=False)
        with self.assertRaisesRegex(DeliveryError, "STALE_EPOCH"):
            await bridge.mark_sent("a", envelope.command_id, "session")
        self.assertNotEqual(registry.pending_state("a", envelope.command_id), "SENT")

        await registry.advance_epoch("a", 2)
        await registry.fence_account("a", 2)
        self.assertEqual(registry.pending_state("a", envelope.command_id), "FENCED")

    async def test_mark_sent_then_stop_before_wire_write_never_emits_entry(self):
        """The writer's second race ordering must fail closed after stop commits."""
        coordinator = ExecutionCoordinator()
        identity = {"provider": "mt5", "broker_server": "demo", "external_account_id": "42"}
        self._seed_dispatchable_order(coordinator, account_id="a", suffix="wire-race")
        coordinator.account("a").exposure_gate = "OPEN"
        registry = ConnectorDeliveryRegistry()
        bridge = ConnectorDeliveryBridge(coordinator, registry)
        await registry.open_session(
            "a", 1, "session", identity=identity, execution_epoch=1,
            execution_gate_open=True,
        )
        await bridge.enqueue_order("a", "order-wire-race", identity=identity, generation=1)
        envelope = await registry.next_for_session("a", "session")
        await bridge.mark_sent("a", envelope.command_id, "session")

        # mark_sent happens before the actual websocket write. A concurrent
        # lifecycle commit must invalidate that already-marked envelope.
        coordinator.apply_lifecycle_commit("a", 2, allowed=False, persist=False)
        with self.assertRaisesRegex(DeliveryError, "STALE_EPOCH"):
            await bridge.ensure_wire_write("a", envelope.command_id, "session")
        self.assertEqual(registry.pending_state("a", envelope.command_id), "FENCED")
        self.assertEqual(coordinator.dispatch_records[envelope.command_id].state, "FENCED")


if __name__ == "__main__":
    unittest.main()
