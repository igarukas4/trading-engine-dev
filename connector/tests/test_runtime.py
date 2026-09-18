import sys, unittest, json
sys.path.insert(0,"connector/src")
from mt5_connector.config import ConnectorConfig, ConfigError
from mt5_connector.models import Identity, AccountSnapshot, ReadSnapshot
from mt5_connector.adapter import AdapterError, OfficialMT5Adapter
from mt5_connector.protocol import ConnectorProtocol, ProtocolError

RAW={"account_id":"a1","provider":"MT5","broker_server":"Demo","external_account_id":"42","key_id":"k1","secret_ref":"vault://ref","terminal_path":"C:/mt5/terminal64.exe","wss_url":"wss://example/ws","journal_path":"%LOCALAPPDATA%/journal.sqlite","execution_disabled":True}
class Fake:
 def __init__(self): self.calls=[]
 def initialize(self,path): self.calls.append(("initialize",path))
 def account_snapshot(self): return AccountSnapshot("a1",Identity("MT5","Demo","42"),"42","USD",100,"100","101",True,"HEDGING",({"ticket":"1"},),({"ticket":"2"},),())
 def symbol_info(self,s): self.calls.append(("symbol",s)); return ReadSnapshot("symbol_info",{"symbol":s,"digits":5})
 def quote(self,s): return ReadSnapshot("quote",{"symbol":s,"bid":"1.0","ask":"1.1"})
 def closed_m1(self,s,c): return ReadSnapshot("candles",{"items":[{"open":"1"}],"closed_only":True})
 def open_orders(self): return ReadSnapshot("orders",{"items":[]})
 def open_positions(self): return ReadSnapshot("positions",{"items":[]})
class Tests(unittest.TestCase):
 def test_config_and_wss(self):
  cfg=ConnectorConfig.from_dict(RAW); self.assertEqual(cfg.wss_url[:3],"wss"); self.assertEqual(set(ConnectorProtocol(cfg,Fake()).hello("SECRET")),{"type","account_id","provider","broker_server","external_account_id","key_id","secret","generation","session_id"})
  with self.assertRaises(ConfigError): ConnectorConfig.from_dict({**RAW,"wss_url":"https://x"})
 def test_fake_reads_and_serialization(self):
  f=Fake(); s=f.account_snapshot(); self.assertEqual(json.loads(s.to_json())["identity"]["broker_server"],"Demo"); self.assertEqual(f.symbol_info("EURUSD").to_dict()["digits"],5); self.assertEqual(f.quote("EURUSD").data["ask"],"1.1"); self.assertTrue(f.closed_m1("EURUSD",2).data["closed_only"]); self.assertEqual(f.open_orders().kind,"orders"); self.assertEqual(f.open_positions().kind,"positions")
 def test_identity_mismatch_fails_closed(self):
  class Info: login=99; server="Other"
  class Module:
   def account_info(self): return Info()
  a=OfficialMT5Adapter(Identity("MT5","Demo","42"),"a1"); a._mt5=Module()
  with self.assertRaises(AdapterError): a.account_snapshot()
 def test_snapshot_heartbeat_generation_and_disabled(self):
  c=ConnectorProtocol(ConnectorConfig.from_dict(RAW),Fake()); c.accept_snapshot({"type":"snapshot","snapshot":{"account_id":"a1"},"generation":7}); self.assertEqual(c.generation,7); self.assertEqual(c.heartbeat()["type"],"heartbeat"); out=c.handle({"type":"order.submit_market","account_id":"a1","generation":7,"command_id":"x","idempotency_key":"secret-key"}); self.assertEqual(out["payload"],{"state":"REJECTED","code":"EXECUTION_DISABLED"}); self.assertEqual(c.handle({"type":"heartbeat_ack","account_id":"a1","generation":7}),{"type":"heartbeat_ack","account_id":"a1","generation":7})
  with self.assertRaises(ProtocolError): c.handle({"type":"heartbeat_ack","account_id":"a1","generation":8})
 def test_missing_official_package_is_safe(self):
  a=OfficialMT5Adapter(Identity("MT5","Demo","42"),"a1")
  try: a._module()
  except AdapterError as e: self.assertIn("MetaTrader5",str(e))
 def test_backoff_bounded(self):
  c=ConnectorProtocol(ConnectorConfig.from_dict(RAW),Fake(),random_fn=lambda:1); self.assertLessEqual(c.backoff(99),36)
if __name__=="__main__": unittest.main()
