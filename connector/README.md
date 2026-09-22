# MT5 Connector (DEMO/MANUAL, fail-closed)

Run from repository root with `PYTHONPATH=connector/src python -m mt5_connector.main --config config.json --run`. Initialization is opt-in via `--initialize`. Configuration alone never enables side effects: command-capable startup requires an explicit `PreflightEvidence` supplied by the embedding operator, proving DEMO/MANUAL identity, terminal permission, current generation/epoch, writable journal, complete reconciliation, no unresolved `UNKNOWN`, and the backend execution gate.

The runtime owns the account-local SQLite journal and dispatcher. Startup recovers pre-invocation rows as `REJECTED/NOT_INVOKED_AFTER_RESTART` and preserves post-invocation rows as `UNKNOWN`; reconnect/session loss never blindly resends them. Inspect sanitized health and reconcile before new side effects. Stop without deleting the journal while a command is `INVOKING` or `UNKNOWN`.

Provide the secret only through a protected provider. `secret_ref` may use `credential-manager://<target>` (via the platform keyring provider) or an absolute `file://` path protected by Windows ACLs. Secret values are not accepted in CLI arguments/config, logs, errors, fixtures, or URLs. The repository tests use only fake adapters/transports and temporary journals; they never connect to MT5 or a broker.

The async WSS runtime validates `wss://` and keeps TLS certificate verification enabled. Local fake transports may be used explicitly with `local_test`. Human DEMO execution still requires a separate operator approval and preflight; this package does not place a broker order as part of startup or tests.
