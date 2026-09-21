import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, "connector/src")
from mt5_connector.dispatcher import Dispatcher
from mt5_connector.journal import SQLiteJournal, canonical_request_hash


class Adapter:
    def __init__(self, result=None):
        self.result = result or {"retcode": 10009, "external_order_id": "o1"}
        self.calls = []

    def order_check(self, typ, payload):
        self.calls.append(("check", typ))
        return {"retcode": 0}

    def invoke(self, typ, payload):
        self.calls.append(("invoke", typ))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class JournalDispatcherTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "journal.sqlite")

    def tearDown(self):
        self.tmp.cleanup()

    def command(self, command_id="c1", key="k1", sequence=1, payload=None):
        return {
            "account_id": "a1", "command_id": command_id, "idempotency_key": key,
            "generation": 1, "dispatch_sequence": sequence, "execution_epoch": 2,
            "type": "order.submit_market", "payload": payload or {"symbol": "EURUSD", "sl": "1", "tp": "2"},
        }

    def test_durable_lifecycle_and_same_hash_replay(self):
        journal = SQLiteJournal(self.path, "a1")
        adapter = Adapter()
        dispatcher = Dispatcher(journal, adapter)
        command = self.command()
        result = dispatcher.dispatch(command)
        self.assertEqual(result["state"], "ACCEPTED")
        self.assertEqual([call[0] for call in adapter.calls], ["check", "invoke"])
        replay = dispatcher.dispatch(command)
        self.assertEqual(replay["state"], "ACCEPTED")
        self.assertEqual(len(adapter.calls), 2)
        self.assertEqual(journal.get("c1").request_hash, canonical_request_hash(command["type"], command["payload"]))

    def test_timeout_is_unknown_and_fences_later_commands(self):
        journal = SQLiteJournal(self.path, "a1")
        dispatcher = Dispatcher(journal, Adapter({"retcode": 10012}))
        self.assertEqual(dispatcher.dispatch(self.command())["state"], "UNKNOWN")
        later = self.command("c2", "k2", 2)
        self.assertEqual(dispatcher.dispatch(later)["code"], "ACCOUNT_FENCED_UNKNOWN")

    def test_restart_rejects_uninvoked_and_keeps_invoked_unknown(self):
        journal = SQLiteJournal(self.path, "a1")
        journal.receive(command_id="before", generation=1, dispatch_sequence=1, idempotency_key="b",
                         request_hash="h", execution_epoch=1, command_type="order.submit_market", request={})
        journal.receive(command_id="after", generation=1, dispatch_sequence=2, idempotency_key="a",
                         request_hash="h2", execution_epoch=1, command_type="order.submit_market", request={})
        journal.transition("after", state="INVOKING", phase="DISPATCHING", mt5_invoked=True)
        journal.close()
        recovered = SQLiteJournal(self.path, "a1").recover()
        self.assertEqual({row.command_id: row.state for row in recovered}, {"before": "REJECTED", "after": "UNKNOWN"})

    def test_reconciliation_persists_evidence_before_resolution(self):
        journal = SQLiteJournal(self.path, "a1")
        journal.receive(command_id="c1", generation=1, dispatch_sequence=1, idempotency_key="k1",
                         request_hash="h", execution_epoch=1, command_type="order.submit_market", request={})
        journal.transition("c1", state="UNKNOWN", phase="RECONCILING", mt5_invoked=True)
        resolved = journal.resolve_unknown("c1", snapshot={"orders": [{"ticket": "o1"}]},
                                           source="reconcile", match_status="UNIQUE_MATCH",
                                           state="ACCEPTED", result={"external_order_id": "o1"})
        self.assertEqual(resolved.state, "ACCEPTED")
        self.assertEqual(journal.connection.execute("SELECT COUNT(*) FROM connector_observations").fetchone()[0], 1)

    def test_same_idempotency_key_different_hash_never_invokes(self):
        journal = SQLiteJournal(self.path, "a1")
        adapter = Adapter()
        dispatcher = Dispatcher(journal, adapter)
        first = self.command()
        dispatcher.dispatch(first)
        second = self.command("c2", "k1", 2, {"symbol": "GBPUSD", "sl": "1", "tp": "2"})
        self.assertEqual(dispatcher.dispatch(second)["code"], "IDEMPOTENCY_KEY_REUSED")
        self.assertEqual(len(adapter.calls), 2)


if __name__ == "__main__":
    unittest.main()
