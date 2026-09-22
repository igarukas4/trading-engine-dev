"""Production composition for the account-scoped connector runtime."""
import argparse
import asyncio
import json

from .adapter import OfficialMT5Adapter
from .config import ConnectorConfig
from .dispatcher import Dispatcher
from .journal import JournalError, SQLiteJournal
from .models import Identity
from .preflight import PreflightError, require_preflight
from .secrets import secret_provider_from_ref
from .websocket_client import ConnectorClient


class RuntimeErrorSafe(RuntimeError):
    """Stable startup error without credential-bearing context."""


def run_runtime(config, secret_provider=None, *, adapter=None, transport=None,
                max_attempts=1, max_messages=1, preflight=None):
    if secret_provider is None:
        secret_provider = secret_provider_from_ref(config.secret_ref)
    if not callable(secret_provider):
        raise RuntimeErrorSafe("SECRET_PROVIDER_REQUIRED")
    execution_enabled = not config.execution_disabled
    if execution_enabled:
        try:
            require_preflight(preflight)
        except PreflightError as exc:
            raise RuntimeErrorSafe(str(exc)) from exc
    adapter = adapter or OfficialMT5Adapter(
        Identity(config.provider, config.broker_server, config.external_account_id),
        config.account_id,
    )
    journal = None
    try:
        journal = SQLiteJournal(config.journal_path, config.account_id)
        journal.ensure_generation(config.backend_generation)
        dispatcher = Dispatcher(
            journal, adapter, account_id=config.account_id,
            generation=config.backend_generation,
            execution_epoch=(getattr(preflight, "execution_epoch", None)
                              if execution_enabled else None),
            execution_enabled=execution_enabled,
        )
        initialize = getattr(adapter, "initialize", None)
        if callable(initialize):
            initialized = initialize(config.terminal_path)
            if initialized is False:
                raise RuntimeErrorSafe("TERMINAL_UNAVAILABLE")
        client = ConnectorClient(config, adapter, transport=transport,
                                 dispatcher=dispatcher)
        return asyncio.run(client.run(secret_provider, max_attempts=max_attempts,
                                      max_messages=max_messages))
    except (JournalError, OSError) as exc:
        raise RuntimeErrorSafe(str(exc)) from exc
    finally:
        if journal is not None:
            journal.close()


def main(argv=None, *, secret_provider=None, transport=None, preflight=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--run", action="store_true",
                        help="run the account-scoped WSS session")
    args = parser.parse_args(argv)
    with open(args.config, encoding="utf-8") as handle:
        config = ConnectorConfig.from_json(handle.read())
    print(json.dumps({"account_id": config.account_id,
                      "execution_disabled": config.execution_disabled}))
    if args.initialize:
        OfficialMT5Adapter(Identity(config.provider, config.broker_server,
                                    config.external_account_id),
                           config.account_id).initialize(config.terminal_path)
    if args.run:
        run_runtime(config, secret_provider, transport=transport, preflight=preflight)


if __name__ == "__main__":
    main()
