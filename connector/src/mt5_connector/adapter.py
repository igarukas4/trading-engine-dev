from dataclasses import dataclass
from typing import Protocol, Any
from .models import Identity, AccountSnapshot, ReadSnapshot

class AdapterError(RuntimeError): pass
class MT5ReadAdapter(Protocol):
    def initialize(self, terminal_path: str) -> None: ...
    def account_snapshot(self) -> AccountSnapshot: ...
    def symbol_info(self, symbol: str) -> ReadSnapshot: ...
    def quote(self, symbol: str) -> ReadSnapshot: ...
    def closed_m1(self, symbol: str, count: int) -> ReadSnapshot: ...
    def open_orders(self) -> ReadSnapshot: ...
    def open_positions(self) -> ReadSnapshot: ...

class OfficialMT5Adapter:
    def __init__(self, expected: Identity, account_id: str): self.expected=expected; self.account_id=account_id; self._mt5=None
    def _module(self):
        if self._mt5 is None:
            try: import MetaTrader5 as mt5
            except ImportError as e: raise AdapterError("MetaTrader5 package is required only for live terminal initialization") from e
            self._mt5=mt5
        return self._mt5
    def initialize(self, terminal_path):
        mt5=self._module()
        if not mt5.initialize(path=terminal_path): raise AdapterError("MT5 initialization failed")
        self._verify(mt5.account_info())
    def _verify(self, info):
        if info is None: raise AdapterError("MT5 account_info unavailable")
        login=str(getattr(info,"login", "")); server=str(getattr(info,"server", ""))
        if login != self.expected.external_account_id or server != self.expected.broker_server: raise AdapterError("MT5 account identity mismatch")
    def _info(self):
        mt5=self._module(); info=mt5.account_info(); self._verify(info); return info
    def account_snapshot(self):
        i=self._info(); return AccountSnapshot(self.account_id,self.expected,str(i.login),str(getattr(i,"currency","")),int(getattr(i,"leverage",0)),str(getattr(i,"balance",0)),str(getattr(i,"equity",0)),bool(getattr(i,"trade_allowed",False)),str(getattr(i,"margin_mode","")),tuple(),tuple(),tuple())
    def symbol_info(self,symbol): return ReadSnapshot("symbol_info", self._plain(self._module().symbol_info(symbol)))
    def quote(self,symbol): return ReadSnapshot("quote", self._plain(self._module().symbol_info_tick(symbol)))
    def closed_m1(self,symbol,count):
        rows=self._module().copy_rates_from_pos(symbol,self._module().TIMEFRAME_M1,1,count)
        return ReadSnapshot("candles", {"symbol":symbol,"timeframe":"M1","closed_only":True,"items":[self._plain(x) for x in rows or []]})
    def open_orders(self): return ReadSnapshot("orders", {"items":[self._plain(x) for x in self._module().orders_get() or []]})
    def open_positions(self): return ReadSnapshot("positions", {"items":[self._plain(x) for x in self._module().positions_get() or []]})
    @staticmethod
    def _plain(value):
        if value is None: return None
        if hasattr(value,"_asdict"): return {k: OfficialMT5Adapter._plain(v) for k,v in value._asdict().items()}
        if isinstance(value,(list,tuple)): return [OfficialMT5Adapter._plain(v) for v in value]
        return value
