from dataclasses import dataclass
from urllib.parse import urlparse
import json


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class ConnectorConfig:
    account_id: str
    provider: str
    broker_server: str
    external_account_id: str
    key_id: str
    secret_ref: str
    terminal_path: str
    wss_url: str
    journal_path: str
    reconnect_initial: float = 1.0
    reconnect_max: float = 30.0
    jitter: float = 0.2
    execution_disabled: bool = True
    local_test: bool = False
    backend_generation: int = 0

    @classmethod
    def from_dict(cls, raw, *, local_test=False):
        required = ("account_id", "provider", "broker_server", "external_account_id",
                    "key_id", "secret_ref", "terminal_path", "wss_url", "journal_path")
        missing = [key for key in required if not raw.get(key)]
        if missing:
            raise ConfigError("missing configuration: " + ", ".join(missing))
        local = local_test or bool(raw.get("local_test"))
        if urlparse(raw["wss_url"]).scheme.lower() != "wss" and not local:
            raise ConfigError("wss_url must use wss")
        if raw.get("execution_disabled") is not True:
            raise ConfigError("DEMO connector requires execution_disabled=true")
        initial = raw.get("reconnect_initial", 1.0)
        maximum = raw.get("reconnect_max", 30.0)
        generation = raw.get("backend_generation", raw.get("generation", 0))
        if initial <= 0 or maximum < initial or generation < 0 or not isinstance(generation, int):
            raise ConfigError("invalid reconnect or generation settings")
        values = {key: raw.get(key, getattr(cls, key, None)) for key in cls.__dataclass_fields__}
        values.update(local_test=local, backend_generation=generation)
        return cls(**values)

    @classmethod
    def from_json(cls, text, **kwargs):
        return cls.from_dict(json.loads(text), **kwargs)

    def identity(self):
        return {"provider": self.provider, "broker_server": self.broker_server,
                "external_account_id": self.external_account_id}
