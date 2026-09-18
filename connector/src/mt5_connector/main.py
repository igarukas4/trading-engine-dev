"""Safe CLI boundary; secrets can only enter through an injected provider."""
import argparse
import asyncio
import json

from .config import ConnectorConfig
from .adapter import OfficialMT5Adapter
from .models import Identity
from .websocket_client import ConnectorClient


def run_runtime(config, secret_provider, *, adapter=None, transport=None,
                max_attempts=1, max_messages=1):
    if not callable(secret_provider):
        raise TypeError("secret_provider callback is required")
    adapter = adapter or OfficialMT5Adapter(
        Identity(config.provider, config.broker_server, config.external_account_id),
        config.account_id,
    )
    client = ConnectorClient(config, adapter, transport=transport)
    return asyncio.run(client.run(secret_provider, max_attempts=max_attempts,
                                  max_messages=max_messages))


def main(argv=None, *, secret_provider=None, transport=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--run", action="store_true",
                        help="run the injected-provider read-only WSS session")
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
        if secret_provider is None:
            raise RuntimeError("--run requires an injected secret_provider")
        run_runtime(config, secret_provider, transport=transport)


if __name__ == "__main__":
    main()
