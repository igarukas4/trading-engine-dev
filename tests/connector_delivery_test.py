import asyncio
import unittest

from backend.app.connector_delivery import ConnectorDeliveryRegistry, DeliveryError, validate_hello


class ConnectorDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = ConnectorDeliveryRegistry()
        await self.registry.open_session("a", 7, "s1")
        self.kw = dict(
            account_id="a", identity={"provider": "mt5", "broker_server": "demo", "external_account_id": "42"},
            connector_generation=7, execution_epoch=3, type="order.submit_market", payload={"pair": "EURUSD"},
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

    async def test_one_session_and_generation_validation(self):
        with self.assertRaisesRegex(DeliveryError, "SESSION_ALREADY_ACTIVE"):
            await self.registry.open_session("a", 7, "s2")
        with self.assertRaisesRegex(DeliveryError, "STALE_GENERATION"):
            await self.registry.enqueue(**{**self.kw, "connector_generation": 8}, dispatch_sequence=1, command_id="c", idempotency_key="i", request_hash="h")

    async def test_result_states_and_unknown_is_terminal_without_resend(self):
        envelope = await self.registry.enqueue(**self.kw, dispatch_sequence=1, command_id="c1", idempotency_key="i1", request_hash="h1")
        await self.registry.next_for_session("a", "s1")
        await self.registry.mark_sent("a", envelope.command_id, "s1")
        result = {"type": "command_result", "account_id": "a", "connector_generation": 7,
                  "dispatch_sequence": 1, "command_id": "c1", "idempotency_key": "i1", "request_hash": "h1", "result": "UNKNOWN"}
        self.assertEqual(await self.registry.record_result(result), "UNKNOWN")
        self.assertEqual(self.registry.pending_state("a", "c1"), "UNKNOWN")
        with self.assertRaisesRegex(DeliveryError, "STALE_COMMAND_RESULT"):
            await self.registry.record_result(result)
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
        self.assertEqual(await self.registry.record_result(result), "REJECTED")
        second = await self.registry.enqueue(**self.kw, dispatch_sequence=2, command_id="c2", idempotency_key="i2", request_hash="h2")
        await self.registry.next_for_session("a", "s1")
        await self.registry.mark_sent("a", second.command_id, "s1")
        duplicate = {**result, "command_id": "c2", "sequence": 2}
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


if __name__ == "__main__":
    unittest.main()
