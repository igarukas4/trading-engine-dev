from dataclasses import dataclass
from urllib.parse import urlparse
import json

class ConfigError(ValueError): pass
@dataclass(frozen=True)
class ConnectorConfig:
    account_id: str; provider: str; broker_server: str; external_account_id: str
    key_id: str; secret_ref: str; terminal_path: str; wss_url: str
    journal_path: str; reconnect_initial: float=1.0; reconnect_max: float=30.0
    jitter: float=0.2; execution_disabled: bool=True; local_test: bool=False
    @classmethod
    def from_dict(cls, raw, *, local_test=False):
        required=("account_id","provider","broker_server","external_account_id","key_id","secret_ref","terminal_path","wss_url","journal_path")
        missing=[k for k in required if not raw.get(k)]
        if missing: raise ConfigError("missing configuration: "+", ".join(missing))
        url=raw["wss_url"]
        if urlparse(url).scheme.lower() != "wss" and not (local_test or raw.get("local_test")): raise ConfigError("wss_url must use wss")
        if raw.get("execution_disabled") is not True: raise ConfigError("DEMO connector requires execution_disabled=true")
        if raw.get("reconnect_initial",1)<=0 or raw.get("reconnect_max",30)<raw.get("reconnect_initial",1): raise ConfigError("invalid reconnect settings")
        values={k:raw.get(k, getattr(cls,k,None)) for k in cls.__dataclass_fields__}
        values["local_test"]=local_test or bool(raw.get("local_test"))
        return cls(**values)
    @classmethod
    def from_json(cls,text,**kw): return cls.from_dict(json.loads(text),**kw)
    def identity(self): return {"provider":self.provider,"broker_server":self.broker_server,"external_account_id":self.external_account_id}
