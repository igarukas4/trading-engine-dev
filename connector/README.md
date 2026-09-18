# MT5 Connector (read-only DEMO)

Run from repository root with `PYTHONPATH=connector/src python -m mt5_connector.main --config config.json`. Initialization is opt-in via `--initialize`; execution remains disabled.

The async WSS runtime is injectable for tests. A live `--run` invocation must receive a protected `secret_provider` callback through the embedding runtime; secrets are never accepted on the command line, stored in config, or logged. Windows Credential Manager integration is intentionally left as the next deployment integration rather than implemented with guessed retrieval.

The runtime validates `wss://` and leaves TLS certificate verification enabled. Local fake transports may be used explicitly with `local_test`, but no live connection is made during import or tests.
