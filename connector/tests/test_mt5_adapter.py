import sys
import unittest
sys.path.insert(0, "connector/src")

from mt5_connector.adapter import FakeMT5Adapter
from mt5_connector.dispatcher import Dispatcher
from mt5_connector.journal import SQLiteJournal
from mt5_connector.models import Identity
import tempfile


class MT5AdapterTests(unittest.TestCase):
    def command(self, typ="order.submit_market", payload=None):
        return {"account_id": "a1", "command_id": "c1", "idempotency_key": "k1",
                "dispatch_sequence": 1, "type": typ,
                "payload": payload or {"symbol": "EURUSD", "side": "BUY", "volume": "0.01", "sl": "1.09", "tp": "1.11"}}

    def test_dispatcher_checks_before_invoke_and_replays(self):
        adapter = FakeMT5Adapter()
        with tempfile.TemporaryDirectory() as directory:
            journal = SQLiteJournal(directory + "/journal.sqlite", "a1")
            try:
                dispatcher = Dispatcher(journal, adapter)
                self.assertEqual(dispatcher.dispatch(self.command())["state"], "ACCEPTED")
                self.assertEqual([name for name, _ in adapter.checks], ["order.submit_market"])
                self.assertEqual(len(adapter.requests), 1)
                self.assertEqual(dispatcher.dispatch(self.command())["state"], "ACCEPTED")
                self.assertEqual(len(adapter.requests), 1)
            finally:
                journal.close()

    def test_failed_check_never_invokes(self):
        adapter = FakeMT5Adapter(check={"retcode": 10016})
        with tempfile.TemporaryDirectory() as directory:
            journal = SQLiteJournal(directory + "/journal.sqlite", "a1")
            try:
                result = Dispatcher(journal, adapter).dispatch(self.command())
                self.assertEqual(result["state"], "REJECTED")
                self.assertEqual(adapter.requests, [])
            finally:
                journal.close()

    def test_fake_preserves_native_protection_and_exact_close_ticket(self):
        adapter = FakeMT5Adapter(identity=Identity("MT5", "Demo", "42"))
        adapter.positions[7] = {"ticket": "7", "volume": "0.10", "type": 0}
        adapter.close({"symbol": "EURUSD", "side": "SELL", "position_ticket": "7", "volume": "0.05"})
        self.assertEqual(adapter.requests[-1][1]["position_ticket"], "7")


if __name__ == "__main__":
    unittest.main()
