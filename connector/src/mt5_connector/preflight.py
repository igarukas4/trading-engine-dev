"""Verify operator intent against local terminal and authenticated backend facts."""
from __future__ import annotations

import sqlite3
from typing import Any, Mapping

from .adapter import AdapterError


class PreflightError(RuntimeError):
    """Stable, credential-free preflight failure."""


def require_preflight(operator_approved: Any) -> None:
    if operator_approved is not True:
        raise PreflightError("PREFLIGHT_REQUIRED")


def verify_preflight(config, adapter, journal, snapshot: Mapping[str, Any],
                     acknowledgement: Mapping[str, Any]) -> int:
    """Return the verified epoch; never accept operator-supplied health flags."""
    try:
        expected_identity = config.identity()
        account = snapshot["snapshot"]
        facts = adapter.preflight_facts()
        payload = acknowledgement["payload"]
        epoch = snapshot["execution_epoch"]
        if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
            raise PreflightError("PREFLIGHT_FAILED")
        if (snapshot["generation"] != config.backend_generation
                or acknowledgement["generation"] != config.backend_generation
                or acknowledgement["execution_epoch"] != epoch):
            raise PreflightError("PREFLIGHT_STALE_CONTEXT")
        if (account["account_id"] != config.account_id
                or account["identity"] != expected_identity
                or facts["account_id"] != config.account_id
                or any(facts[field] != value for field, value in expected_identity.items())):
            raise PreflightError("PREFLIGHT_IDENTITY_MISMATCH")
        if account["environment"] != "DEMO" or account["execution_mode"] != "MANUAL":
            raise PreflightError("PREFLIGHT_FAILED")
        if facts["trade_mode"] != "DEMO" or not all(
            facts[field] is True for field in (
                "terminal_connected", "terminal_trade_allowed", "account_trade_allowed"
            )
        ):
            raise PreflightError("PREFLIGHT_FAILED")
        if not all(payload[field] is True for field in (
            "reconciliation_complete", "backend_execution_gate", "no_unknown_commands"
        )) or journal.has_unknown():
            raise PreflightError("PREFLIGHT_FAILED")
        journal.connection.execute("BEGIN IMMEDIATE")
        journal.connection.rollback()
        return epoch
    except PreflightError:
        raise
    except AdapterError as exc:
        # Adapter errors may carry implementation-specific detail. Startup
        # exposes one stable code and never serializes that detail.
        raise PreflightError("PREFLIGHT_FAILED") from exc
    except (AttributeError, KeyError, TypeError, ValueError, sqlite3.Error) as exc:
        raise PreflightError("PREFLIGHT_FAILED") from exc
