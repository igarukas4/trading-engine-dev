import json, random, time, uuid
from .config import ConnectorConfig

READ_ONLY={"account_snapshot.request","market_snapshot.request","candle_batch.request","reconcile.request"}
SIDE_EFFECTING={"order.submit_market","position.modify_protection","position.close"}
class ProtocolError(ValueError): pass
class ConnectorProtocol:
 def __init__(self,cfg,adapter,transport=None,clock=time.time,random_fn=random.random): self.cfg=cfg; self.adapter=adapter; self.transport=transport; self.clock=clock; self.random=random_fn; self.generation=None; self.sequence=0; self.session_id=str(uuid.uuid4())
 def hello(self,secret):
  if not secret: raise ProtocolError("secret is required")
  return {"type":"hello","account_id":self.cfg.account_id,"provider":self.cfg.provider,"broker_server":self.cfg.broker_server,"external_account_id":self.cfg.external_account_id,"key_id":self.cfg.key_id,"secret":secret,"generation":self.generation or 0,"session_id":self.session_id}
 def accept_snapshot(self,msg):
  if msg.get("type")!="snapshot" or msg.get("snapshot",{}).get("account_id") not in (None,self.cfg.account_id): raise ProtocolError("wrong account snapshot")
  if "generation" in msg: self.generation=msg["generation"]
  return msg["snapshot"]
 def _envelope(self,typ,payload=None,command_id=None):
  self.sequence+=1; mid=str(uuid.uuid4()); return {"schema_version":1,"type":typ,"message_id":mid,"account_id":self.cfg.account_id,"provider":self.cfg.provider,"broker_server":self.cfg.broker_server,"external_account_id":self.cfg.external_account_id,"generation":self.generation or 0,"sequence":self.sequence,"execution_epoch":0,"command_id":command_id,"idempotency_key":"msg:"+mid,"sent_at":"1970-01-01T00:00:00Z","payload":payload or {}}
 def heartbeat(self): return self._envelope("heartbeat",{"terminal_connected":True,"journal_state":"READY"})
 def handle(self,msg):
  if msg.get("account_id") not in (None,self.cfg.account_id): raise ProtocolError("wrong account")
  if self.generation is not None and msg.get("generation",self.generation)!=self.generation: raise ProtocolError("generation mismatch")
  typ=msg.get("type"); cid=msg.get("command_id")
  if typ in SIDE_EFFECTING: return self._result(cid,msg.get("idempotency_key"),"REJECTED","EXECUTION_DISABLED")
  if typ=="heartbeat_ack": return msg
  if typ not in READ_ONLY: raise ProtocolError("unsupported message")
  p=msg.get("payload",{})
  if typ=="account_snapshot.request": data=self.adapter.account_snapshot().to_dict()
  elif typ=="market_snapshot.request": data={s:self.adapter.symbol_info(s).to_dict() for s in p.get("symbols",[])}
  elif typ=="candle_batch.request": data=self.adapter.closed_m1(p["symbol"],p.get("count",200)).to_dict()
  else: data=self.adapter.account_snapshot().to_dict()
  return self._envelope(typ.replace(".request",""),data,cid)
 def _result(self,cid,key,state,code):
  m=self._envelope("command.result",{"state":state,"code":code},cid); m["idempotency_key"]=key or m["idempotency_key"]; return m
 def backoff(self,attempt): return min(self.cfg.reconnect_max,self.cfg.reconnect_initial*(2**attempt))*(1+self.cfg.jitter*(self.random()*2-1))
 def connect_once(self, secret):
  if self.transport is None: raise ProtocolError("transport is required")
  self.transport.send(json.dumps(self.hello(secret), sort_keys=True))
  raw=self.transport.recv()
  msg=json.loads(raw) if isinstance(raw,str) else raw
  return self.accept_snapshot(msg)
 def reconnect_delays(self, attempts):
  return [self.backoff(i) for i in range(attempts)]
