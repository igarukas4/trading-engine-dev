import asyncio
import unittest

from backend.app.connector_delivery import ConnectorDeliveryRegistry, DeliveryError


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

    async def test_receive_and_delivery_can_progress_concurrently(self):
        envelope = await self.registry.enqueue(**self.kw, dispatch_sequence=1, command_id="c1", idempotency_key="i1", request_hash="h1")
        receive = asyncio.create_task(asyncio.sleep(0.01, result={"type": "heartbeat"}))
        outbound = asyncio.create_task(self.registry.next_for_session("a", "s1"))
        received, sent = await asyncio.gather(receive, outbound)
        self.assertEqual(received["type"], "heartbeat")
        self.assertEqual(sent.command_id, envelope.command_id)


if __name__ == "__main__":
    unittest.main()
