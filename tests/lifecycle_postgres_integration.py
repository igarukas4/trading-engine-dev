"""Opt-in PostgreSQL proof for the account lifecycle public seams.

Run this harness with ``DATABASE_URL`` pointing at an isolated PostgreSQL
database. It applies every repository migration before creating its accounts.
It never initializes MT5 or invokes a broker adapter.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.testclient import TestClient
from psycopg import connect
from psycopg.types.json import Jsonb


ROOT = Path(__file__).resolve().parents[1]


def migrate(database_url: str) -> None:
    """Apply the same ordered migrations used by the backend image."""
    with connect(database_url) as connection:
        for migration in sorted((ROOT / "backend" / "migrations").glob("*.sql")):
            with connection.cursor() as cursor:
                cursor.execute(migration.read_text(encoding="utf-8"))
            connection.commit()


def bind_without_a_secret(account) -> None:
    """Install a fake binding without putting any connector secret in source."""
    from backend.app import main
    from backend.app.broker_accounts import ConnectorBinding

    key_id = f"test-key-{uuid4()}"
    salt_hex = secrets.token_hex(16)
    secret_hash = secrets.token_hex(32)
    binding = ConnectorBinding(
        account.id, *account.identity, key_id, salt_hex, secret_hash,
    )
    with connect(main.settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO connector_bindings
                   (id, broker_account_id, provider, broker_server,
                    external_account_id, key_id, secret_salt, secret_hash)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    str(uuid4()), account.id, *account.identity,
                    key_id, salt_hex, secret_hash,
                ),
            )
    account.connector_bound = True
    main.accounts.bindings[account.id] = binding


def prepare_account(main, external_id: str, *, with_facts: bool):
    from backend.app.market_data import PairMapping
    from backend.app.risk_calendar import RiskLimits

    account = main.accounts.register(
        provider="MT5", broker_server="Broker-Demo",
        external_account_id=external_id, display_name=external_id,
        environment="DEMO",
    )
    bind_without_a_secret(account)
    main.accounts.heartbeat(account.id, account.connector_generation, f"session-{external_id}", lease_seconds=300)
    establish_authoritative_session(main, account)
    main.accounts.mark_reconciled(account.id, datetime.now(timezone.utc).isoformat())
    if with_facts:
        limits = main.risk_limits.create(RiskLimits(account.id))
        main.lifecycle.record_risk_limits(account.id, version=limits.version)
        main.market_data.register_pair(
            account.id, PairMapping(account.id, "EURUSD", f"EURUSD.{external_id}"),
        )
        main.lifecycle.record_pair_mapping(
            account.id, "EURUSD", f"EURUSD.{external_id}", valid=True,
        )
    return account


def establish_authoritative_session(main, account) -> None:
    """Install the fake WSS transport authority used by lifecycle readiness.

    This is intentionally only the ConnectorDeliveryRegistry: no broker or
    MT5 adapter is initialized. The persisted heartbeat/session columns and the
    in-process registry must agree on the exact account generation.
    """
    session_id = f"session-{account.external_account_id}"
    asyncio.run(main.connector_delivery.open_session(
        account.id,
        account.connector_generation,
        session_id,
        identity={
            "provider": account.provider,
            "broker_server": account.broker_server,
            "external_account_id": account.external_account_id,
        },
        execution_epoch=account.execution_epoch,
        reconciliation_required=False,
    ))
    assert main.connector_delivery.active_session(
        account.id, generation=account.connector_generation, session_id=session_id,
    )


def assert_http_forgery_is_rejected(main, account_id: str) -> None:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(
            app=main.app, client=("198.51.100.24", 50000),
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.post(
                f"/api/v1/broker-accounts/{account_id}/lifecycle/enable",
                headers={"X-Authenticated-User": "forged"},
                json={
                    "idempotency_key": "forged-actor",
                    "expected_version": 1,
                    "reason": "integration test",
                },
            )

    assert asyncio.run(request()).status_code == 401


def install_audit_failure(database_url: str) -> None:
    with connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """CREATE OR REPLACE FUNCTION issue72_abort_audit()
                   RETURNS trigger LANGUAGE plpgsql AS $$
                   BEGIN RAISE EXCEPTION 'issue72 rollback probe'; END;
                   $$""",
            )
            cursor.execute("DROP TRIGGER IF EXISTS issue72_abort_audit_trigger ON lifecycle_audit")
            cursor.execute(
                """CREATE TRIGGER issue72_abort_audit_trigger
                   BEFORE INSERT ON lifecycle_audit FOR EACH ROW
                   EXECUTE FUNCTION issue72_abort_audit()""",
            )


def remove_audit_failure(database_url: str) -> None:
    with connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DROP TRIGGER IF EXISTS issue72_abort_audit_trigger ON lifecycle_audit")
            cursor.execute("DROP FUNCTION IF EXISTS issue72_abort_audit()")


def account_row(database_url: str, account_id: str) -> tuple[str, str, int, int]:
    with connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT lifecycle_status, bot_state, version, execution_epoch
                   FROM broker_accounts WHERE id = %s""",
                (account_id,),
            )
            row = cursor.fetchone()
    assert row is not None
    return tuple(row)


def session_row(database_url: str, account_id: str) -> tuple[str | None, int | None]:
    with connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT connector_session_id, connector_session_generation "
                "FROM broker_accounts WHERE id = %s",
                (account_id,),
            )
            row = cursor.fetchone()
    assert row is not None
    return tuple(row)


def run() -> None:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise SystemExit("DATABASE_URL is required for this opt-in integration harness")

    migrate(database_url)
    from backend.app import main
    from backend.app.execution import ExecutionCoordinator
    from backend.app.lifecycle import LifecycleCoordinator
    from backend.app.risk_calendar import RiskAssessment, RiskLimits

    account = prepare_account(main, f"pg-a-{uuid4()}", with_facts=True)
    other = prepare_account(main, f"pg-b-{uuid4()}", with_facts=False)
    with connect(main.settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT version FROM schema_migrations "
                "WHERE version = '016_connector_session_authority'",
            )
            assert cursor.fetchone() == ("016_connector_session_authority",)
    assert session_row(main.settings.database_url, account.id) == (
        f"session-{account.external_account_id}", account.connector_generation,
    )
    client = TestClient(main.app)
    headers = {"X-Authenticated-User": "integration-operator"}
    base = f"/api/v1/broker-accounts/{account.id}/lifecycle"
    assert_http_forgery_is_rejected(main, account.id)

    rejected = client.post(
        f"/api/v1/broker-accounts/{other.id}/lifecycle/enable",
        headers=headers,
        json={"idempotency_key": "missing-facts", "expected_version": 1, "reason": "integration test"},
    )
    assert rejected.status_code == 422
    assert "READINESS:" in rejected.json()["detail"]["code"]
    assert account_row(main.settings.database_url, other.id) == ("DISABLED", "STOPPED", 1, 1)

    enabled_body = main.lifecycle.command(
        account, "enable", idempotency_key="enable-a", expected_version=1,
        reason="integration test", actor="integration-operator", readiness=None,
    )
    assert enabled_body.status == "ACCEPTED"
    replay = main.lifecycle.command(
        account, "enable", idempotency_key="enable-a", expected_version=1,
        reason="integration test", actor="integration-operator", readiness=None,
    )
    assert replay.replayed is True
    assert replay.command_id == enabled_body.command_id

    stale = client.post(
        f"{base}/start", headers=headers,
        json={"idempotency_key": "stale-start", "expected_version": 1, "reason": "integration test"},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "STALE_VERSION"
    started = main.lifecycle.command(
        account, "start", idempotency_key="start-a", expected_version=2,
        reason="integration test", actor="integration-operator", readiness=None,
    )
    assert started.status == "ACCEPTED"
    assert main.execution.orders == {} and main.execution.dispatch_records == {}

    from backend.app.main import connector_reconciliation_acknowledgement

    observation = {
        "complete": True,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "orders": [], "fills": [], "positions": [],
        "history_orders": [], "deals": [],
    }
    gate = connector_reconciliation_acknowledgement(
        account, observation, [], None, main.execution, main.lifecycle, main.accounts,
    )
    assert gate["backend_execution_gate"] is True

    other_limits = main.risk_limits.create(RiskLimits(other.id))
    main.lifecycle.record_risk_limits(other.id, version=other_limits.version)
    from backend.app.market_data import PairMapping
    main.market_data.register_pair(other.id, PairMapping(other.id, "GBPUSD", "GBPUSD.other"))
    main.lifecycle.record_pair_mapping(other.id, "GBPUSD", "GBPUSD.other", valid=True)

    other_context = main.lifecycle.readiness_context(
        other, main.accounts.bindings[other.id], main.execution,
    )
    def enable_other():
        return main.lifecycle.command(
            other, "enable", idempotency_key="concurrent-enable", expected_version=1,
            reason="integration test", actor="integration-operator", readiness=other_context,
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        concurrent = [future.result() for future in (pool.submit(enable_other), pool.submit(enable_other))]
    assert sorted(result.replayed for result in concurrent) == [False, True]
    assert main.lifecycle.readiness_facts(account.id).pair_mappings == {
        "EURUSD": f"EURUSD.{account.external_account_id}"
    }
    assert main.lifecycle.readiness_facts(other.id).pair_mappings == {"GBPUSD": "GBPUSD.other"}

    with connect(main.settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE connector_bindings SET revoked_at = now() WHERE broker_account_id = %s",
                (other.id,),
            )
    main.accounts.bindings[other.id].revoked = True
    try:
        main.lifecycle.command(
            other, "start", idempotency_key="revoked-start", expected_version=2,
            reason="integration test", actor="integration-operator",
            readiness=main.lifecycle.readiness_context(
                other, main.accounts.bindings[other.id], main.execution,
            ),
        )
    except ValueError as error:
        assert "CONNECTOR_BINDING_REVOKED" in str(error)
    else:
        raise AssertionError("revoked connector binding was accepted")

    install_audit_failure(main.settings.database_url)
    before = account_row(main.settings.database_url, account.id)
    try:
        main.lifecycle.command(
            account, "stop", idempotency_key="rollback-stop", expected_version=3,
            reason="integration rollback", actor="integration-operator",
            readiness=main.lifecycle.readiness_context(
                account, main.accounts.bindings[account.id], main.execution,
            ),
        )
    except Exception:
        pass
    else:
        raise AssertionError("audit trigger did not force rollback")
    finally:
        remove_audit_failure(main.settings.database_url)
    assert account_row(main.settings.database_url, account.id) == before
    with connect(main.settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM lifecycle_commands WHERE idempotency_key = %s",
                ("rollback-stop",),
            )
            assert cursor.fetchone()[0] == 0

    now = datetime.now(timezone.utc)
    assessment = RiskAssessment(
        account.id, 1, True, purpose="PRE_ORDER", assessed_at=now,
        valid_until=now + timedelta(seconds=30), signal_revision=1, signal_id="entry",
    )
    queued = main.execution.accept_execution(
        account_id=account.id, signal_id="entry", idempotency_key="entry-fence",
        canonical_hash="entry-fence", execution_epoch=1, risk_assessment=assessment,
        signal_revision=1, order_payload={"symbol": "EURUSD", "volume": "1"}, now=now,
    )
    entry_record = main.execution.prepare_connector_dispatch(
        account.id, queued.order.id,
        identity={"provider": "MT5", "broker_server": "Broker-Demo", "external_account_id": account.external_account_id},
        generation=0,
    )
    source = main.execution.accept_execution(
        account_id=account.id, signal_id="position-source", idempotency_key="position-source",
        canonical_hash="position-source", execution_epoch=1,
        risk_assessment=RiskAssessment(
            account.id, 1, True, purpose="PRE_ORDER", assessed_at=now,
            valid_until=now + timedelta(seconds=30), signal_revision=1, signal_id="position-source",
        ),
        signal_revision=1, order_payload={"symbol": "EURUSD", "volume": "1"}, now=now,
    )
    main.execution.record_fill(
        account.id, source.order.id, "deal-source", "1",
        native_protection_confirmed=True, external_position_id="position-source",
    )
    with connect(main.settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO connector_dispatch_outbox
                   (command_id, broker_account_id, dispatch_sequence,
                    connector_generation, execution_epoch, idempotency_key,
                    request_hash, command_type, payload, state)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'QUEUED')""",
                (
                    entry_record.command_id, account.id, entry_record.dispatch_sequence,
                    entry_record.generation, entry_record.execution_epoch,
                    entry_record.idempotency_key, entry_record.request_hash,
                    entry_record.command_type, Jsonb(entry_record.payload),
                ),
            )
    stopped = main.lifecycle.command(
        account, "stop", idempotency_key="stop-a", expected_version=3,
        reason="integration stop", actor="integration-operator",
        readiness=main.lifecycle.readiness_context(
            account, main.accounts.bindings[account.id], main.execution,
        ),
    )
    assert stopped.status == "ACCEPTED"
    assert entry_record.state == "FENCED"
    assert queued.order.status == "REJECTED" and queued.reservation.status == "RELEASED"
    with connect(main.settings.database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT state FROM connector_dispatch_outbox WHERE command_id = %s",
                (entry_record.command_id,),
            )
            assert cursor.fetchone()[0] == "REJECTED"
            cursor.execute(
                "SELECT status, reason_codes FROM runtime_interlocks "
                "WHERE broker_account_id = %s",
                (account.id,),
            )
            assert cursor.fetchone() == ("BLOCKED", ["LIFECYCLE_STOPPED"])
            cursor.execute(
                "SELECT state -> 'accounts' -> %s ->> 'runtime_interlock', "
                "state -> 'accounts' -> %s -> 'interlock_reasons' "
                "FROM execution_state_snapshots WHERE snapshot_id = 1",
                (account.id, account.id),
            )
            snapshot_interlock = cursor.fetchone()
            assert snapshot_interlock == ("BLOCKED", ["LIFECYCLE_STOPPED"])

    close = main.execution.request_position_close(
        account.id, source.order.id, "0.5", "EURUSD", "integration exit",
        confirmed=True, idempotency_key="close-after-stop",
    )
    exit_record = main.execution.prepare_connector_position_dispatch(
        account.id, close.id,
        identity={"provider": "MT5", "broker_server": "Broker-Demo", "external_account_id": account.external_account_id},
        generation=0,
    )
    assert exit_record.command_type == "position.close" and exit_record.state == "QUEUED"
    after_stop_gate = connector_reconciliation_acknowledgement(
        account, observation, [], None, main.execution, main.lifecycle, main.accounts,
    )
    assert after_stop_gate["backend_execution_gate"] is False

    restarted_accounts = __import__("backend.app.broker_accounts", fromlist=["AccountRegistry"]).AccountRegistry(
        main.settings.database_url,
    )
    restarted = restarted_accounts.accounts[account.id]
    restarted_execution = ExecutionCoordinator(
        database_url=main.settings.database_url,
        account_identity_provider=lambda account_id: restarted_accounts.accounts[account_id].identity,
    )
    restarted_lifecycle = LifecycleCoordinator(
        database_url=main.settings.database_url,
        account_registry=restarted_accounts,
        execution=restarted_execution,
    )
    assert restarted.lifecycle_status == "ENABLED" and restarted.bot_state == "STOPPED"
    assert restarted_execution.runtime_interlock(account.id).status == "BLOCKED"
    assert "LIFECYCLE_STOPPED" in restarted_execution.runtime_interlock(account.id).reasons
    assert restarted_lifecycle.readiness_facts(account.id).risk_limits_version == 1
    assert restarted_lifecycle.readiness_facts(account.id).pair_mappings
    assert not restarted_lifecycle.readiness_context(
        restarted, restarted_accounts.bindings[account.id], restarted_execution,
    ).allowed
    exact = restarted_lifecycle.command(
        restarted, "start", idempotency_key="start-a", expected_version=2,
        reason="integration test", actor="integration-operator", readiness=None,
    )
    assert exact.replayed and exact.status == "ACCEPTED" and exact.bot_state == "RUNNING"
    print("lifecycle PostgreSQL integration passed")


if __name__ == "__main__":
    run()
