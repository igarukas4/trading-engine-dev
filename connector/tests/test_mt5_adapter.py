import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "connector/src")

from mt5_connector.adapter import FakeMT5Adapter, OfficialMT5Adapter
from mt5_connector.dispatcher import Dispatcher
from mt5_connector.journal import SQLiteJournal
from mt5_connector.models import Identity


class MT5AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.journal = SQLiteJournal(
            str(Path(self.tmp.name) / "journal.sqlite"), "a1"
        )
        self.addCleanup(self.journal.close)

    def command(self, typ="order.submit_market", payload=None):
        return {
            "account_id": "a1",
            "command_id": "c1",
            "idempotency_key": "k1",
            "dispatch_sequence": 1,
            "type": typ,
            "payload": payload or {
                "symbol": "EURUSD",
                "side": "BUY",
                "volume": "0.01",
                "sl": "1.09",
                "tp": "1.11",
            },
        }

    def test_dispatcher_checks_before_invoke_and_replays(self):
        adapter = FakeMT5Adapter()
        dispatcher = Dispatcher(self.journal, adapter)
        self.assertEqual(dispatcher.dispatch(self.command())["state"], "ACCEPTED")
        self.assertEqual(
            [name for name, _ in adapter.checks], ["order.submit_market"]
        )
        self.assertEqual(len(adapter.requests), 1)
        self.assertEqual(dispatcher.dispatch(self.command())["state"], "ACCEPTED")
        self.assertEqual(len(adapter.requests), 1)

    def test_failed_check_never_invokes(self):
        adapter = FakeMT5Adapter(check={"retcode": 10016})
        result = Dispatcher(self.journal, adapter).dispatch(self.command())
        self.assertEqual(result["state"], "REJECTED")
        self.assertEqual(adapter.requests, [])

    def test_fake_preserves_native_protection_and_exact_close_ticket(self):
        adapter = FakeMT5Adapter(identity=Identity("MT5", "Demo", "42"))
        adapter.positions[7] = {"ticket": "7", "volume": "0.10", "type": 0}
        adapter.close({
            "symbol": "EURUSD",
            "side": "SELL",
            "position_ticket": "7",
            "volume": "0.05",
        })
        self.assertEqual(adapter.requests[-1][1]["position_ticket"], "7")

    def test_official_protection_request_uses_exact_ticket_without_entry_fields(self):
        class Module:
            TRADE_ACTION_SLTP = 6

            def account_info(self):
                return type("Account", (), {"login": 42, "server": "Demo", "trade_allowed": True})()

            def symbol_info(self, symbol):
                return type("Symbol", (), {"visible": True, "digits": 5, "point": 0.00001,
                    "volume_min": 0.01, "volume_max": 10, "volume_step": 0.01,
                    "trade_stops_level": 0, "trade_freeze_level": 0,
                    "filling_mode": 0, "trade_allowed": True})()

            def positions_get(self, ticket):
                return [{"ticket": ticket, "symbol": "EURUSD", "sl": 1.08, "tp": 1.12,
                         "volume": 0.10, "type": 0}]

        adapter = OfficialMT5Adapter(Identity("MT5", "Demo", "42"), "a1")
        adapter._mt5 = Module()
        request = adapter._request("position.modify_protection", {
            "symbol": "EURUSD", "position_ticket": "7", "sl": "1.09000", "tp": "1.11000",
        })
        self.assertEqual(request, {"action": 6, "symbol": "EURUSD", "position": 7,
                                   "sl": 1.09, "tp": 1.11})
        stop_only = adapter._request("position.modify_protection", {
            "symbol": "EURUSD", "position_ticket": "7", "sl": "1.09000", "tp": None,
        })
        self.assertEqual(stop_only["tp"], 1.12)

    def test_unrecognised_retcodes_remain_unknown(self):
        class Module:
            def order_send(self, request):
                return {"retcode": 19999}

        adapter = OfficialMT5Adapter(Identity("MT5", "Demo", "42"), "a1")
        adapter._mt5 = Module()
        result = adapter._send({"action": 1})
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertEqual(result["code"], "UNKNOWN_RETCODE")


if __name__ == "__main__":
    unittest.main()
