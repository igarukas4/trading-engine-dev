"""Production composition for the account-scoped connector runtime."""
import argparse
import asyncio
import json
import sqlite3
from urllib.parse import urlparse

from .adapter import AdapterError, OfficialMT5Adapter
from .config import ConnectorConfig
from .dispatcher import Dispatcher
from .journal import JournalError, SQLiteJournal
from .models import Identity
from .preflight import PreflightError, require_preflight, verify_preflight
from .secrets import secret_provider_from_ref
from .websocket_client import ConnectorClient


class RuntimeErrorSafe(RuntimeError):
    """Stable startup error without credential-bearing context."""


def run_runtime(config, secret_provider=None, *, adapter=None, transport=None,
                max_attempts=1, max_messages=1, preflight=None, preflight_only=False):
    execution_enabled = not config.execution_disabled and not preflight_only
    if execution_enabled and secret_provider is not None and not config.local_test:
        raise RuntimeErrorSafe("PROTECTED_SECRET_REQUIRED")
    if execution_enabled and getattr(secret_provider, "read_only_file_provider", False):
        raise RuntimeErrorSafe("PROTECTED_SECRET_REQUIRED")
    if (
        execution_enabled
        and urlparse(config.secret_ref).scheme.lower() == "file"
        and not config.local_test
    ):
        raise RuntimeErrorSafe("PROTECTED_SECRET_REQUIRED")
    if execution_enabled and secret_provider is None and not config.secret_ref.startswith("credential-manager://"):
        raise RuntimeErrorSafe("PROTECTED_SECRET_REQUIRED")
    if secret_provider is None:
        secret_provider = secret_provider_from_ref(config.secret_ref)
    if not callable(secret_provider):
        raise RuntimeErrorSafe("SECRET_PROVIDER_REQUIRED")
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
            execution_epoch=None,
            execution_enabled=False,
        )
        if execution_enabled and dispatcher.blocked:
            # A live WSS session must be allowed to deliver reconciliation.required.
            # Test transports with no pending frames cannot provide that proof.
            if hasattr(transport, "frames") and not getattr(transport, "frames"):
                raise RuntimeErrorSafe("RECONCILIATION_REQUIRED")
        initialize = getattr(adapter, "initialize", None)
        if callable(initialize):
            try:
                initialized = initialize(config.terminal_path)
            except AdapterError as exc:
                raise RuntimeErrorSafe("TERMINAL_UNAVAILABLE") from exc
            if initialized is False:
                raise RuntimeErrorSafe("TERMINAL_UNAVAILABLE")
        client = None

        def approve(snapshot, acknowledgement):
            try:
                epoch = verify_preflight(config, adapter, journal, snapshot, acknowledgement)
            except PreflightError:
                dispatcher.execution_enabled = False
                return
            dispatcher.execution_epoch = epoch
            dispatcher.execution_enabled = execution_enabled
            client.protocol.preflight_ready = True

        client = ConnectorClient(config, adapter, transport=transport,
                                 dispatcher=dispatcher, session_preflight=approve)
        protocol = asyncio.run(client.run(secret_provider, max_attempts=max_attempts,
                                          max_messages=max_messages))
        if preflight_only and not getattr(protocol, "preflight_ready", False):
            raise RuntimeErrorSafe("PREFLIGHT_FAILED")
        return protocol
    except JournalError as exc:
        raise RuntimeErrorSafe(str(exc)) from exc
    except sqlite3.Error as exc:
        raise RuntimeErrorSafe("JOURNAL_UNAVAILABLE") from exc
    except OSError as exc:
        raise RuntimeErrorSafe("JOURNAL_UNAVAILABLE") from exc
    except AdapterError as exc:
        raise RuntimeErrorSafe("TERMINAL_UNAVAILABLE") from exc
    finally:
        if journal is not None:
            journal.close()


def main(argv=None, *, secret_provider=None, transport=None, preflight=None, adapter=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--initialize", action="store_true")
    parser.add_argument("--run", action="store_true",
                        help="run the account-scoped WSS session")
    parser.add_argument("--enable-manual-demo", action="store_true",
                        help="operator opts in to verified MANUAL DEMO command handling")
    parser.add_argument("--preflight-only", action="store_true",
                        help="read-only terminal/WSS/reconciliation check; never dispatch commands")
    args = parser.parse_args(argv)
    with open(args.config, encoding="utf-8") as handle:
        config = ConnectorConfig.from_json(handle.read())
    if not args.preflight_only:
        print(json.dumps({"account_id": config.account_id,
                          "execution_disabled": config.execution_disabled}))
    if args.initialize:
        OfficialMT5Adapter(Identity(config.provider, config.broker_server,
                                    config.external_account_id),
                           config.account_id).initialize(config.terminal_path)
    if args.run or args.preflight_only:
        # A production command-capable CLI may only use the configured
        # protected store. Test seams can inject a provider for preflight-only
        # runs, which never dispatch a broker side effect.
        if (
            args.run
            and secret_provider is not None
            and urlparse(config.secret_ref).scheme.lower() == "file"
            and not config.local_test
        ):
            raise RuntimeErrorSafe("PROTECTED_SECRET_REQUIRED")
        protocol = run_runtime(config, secret_provider, transport=transport, adapter=adapter,
                               preflight=(preflight if preflight is not None else args.enable_manual_demo),
                               preflight_only=args.preflight_only,
                               max_attempts=5 if args.run else 1,
                               max_messages=None if args.run else 2)
        if args.preflight_only:
            print(json.dumps({"account_id": config.account_id,
                              "preflight": "PASS" if getattr(protocol, "preflight_ready", False) else "FAIL"}))


if __name__ == "__main__":
    main()
