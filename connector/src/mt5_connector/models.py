from dataclasses import dataclass, asdict
from typing import Any
import json

@dataclass(frozen=True)
class Identity:
    provider: str
    broker_server: str
    external_account_id: str

@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    identity: Identity
    login: str
    currency: str
    leverage: int
    balance: str
    equity: str
    trade_allowed: bool
    margin_mode: str
    orders: tuple[dict[str, Any], ...] = ()
    positions: tuple[dict[str, Any], ...] = ()
    deals: tuple[dict[str, Any], ...] = ()
    def to_dict(self):
        d=asdict(self); d["identity"]=asdict(self.identity); return d
    def to_json(self): return json.dumps(self.to_dict(), sort_keys=True)

@dataclass(frozen=True)
class ReadSnapshot:
    kind: str
    data: dict[str, Any]
    def to_dict(self): return {"kind": self.kind, **self.data}
