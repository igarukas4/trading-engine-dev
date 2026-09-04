#!/usr/bin/env python3
"""Executable cross-contract checks for docs/spec/frontend-v0.md.

This validator verifies that every frontend surface is present, its stated
backend contract is backed by backend-v0.md, and the acceptance criteria cover
the selected V0 decisions. It deliberately fails on accidental browser-to-
connector language and non-account-scoped command descriptions.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "docs/spec/frontend-v0.md"
BACKEND = ROOT / "docs/spec/backend-v0.md"
CONTEXT = ROOT / "CONTEXT.md"


def require(text: str, needle: str, source: Path) -> None:
    if needle not in text:
        raise AssertionError(f"{source.relative_to(ROOT)}: missing required contract: {needle}")


def section(text: str, heading: str) -> str:
    match = re.search(rf"^## (?:\d+\. )?{re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match:
        raise AssertionError(f"frontend-v0.md: missing section {heading}")
    return match.group(1)


def main() -> None:
    frontend = FRONTEND.read_text()
    backend = BACKEND.read_text()
    context = CONTEXT.read_text()

    # The six-page product contract is executable as an exact surface set.
    pages = ["Dashboard", "Markets", "Opportunities", "Positions", "Strategies", "System"]
    headings = re.findall(r"^## \d+\. ([A-Za-z]+)$", frontend, re.M)
    missing_pages = [page for page in pages if page not in headings]
    if missing_pages:
        raise AssertionError(f"frontend-v0.md: missing page sections: {missing_pages}")

    # Backend routes/events referenced by the frontend must exist in the
    # canonical backend contract. This prevents a frontend spec inventing an API.
    backend_contracts = [
        "GET /dashboard-summary-snapshot",
        "GET /broker-accounts/{id}/dashboard-snapshot",
        "GET /ws/v1/dashboard",
        "/ws/v1/quotes",
        "GET /broker-accounts/{id}/positions",
        "GET /broker-accounts/{id}/orders",
        "GET /broker-accounts/{id}/fills",
        "GET /broker-accounts/{id}/strategy-configs",
        "POST /broker-accounts/{id}/signals/{signal_id}/approve",
        "POST /broker-accounts/{id}/signals/{signal_id}/execute",
        "POST /broker-accounts/{id}/bot/emergency-stop",
        "POST /global-emergency",
    ]
    for contract in backend_contracts:
        require(backend, contract, BACKEND)

    # Verify UI behavior is tied to the backend's real safety semantics.
    for term in [
        "Idempotency-Key",
        "expected_version",
        "ACCOUNT_CONTEXT_MISMATCH",
        "snapshot.required",
        "replay.complete",
        "UNKNOWN",
        "ATTENTION_REQUIRED",
    ]:
        require(backend, term, BACKEND)
        require(frontend, term, FRONTEND)

    # Domain terms must retain the glossary's canonical account scope wording.
    for term in ["BrokerAccount", "MarketState", "Signal", "RiskAssessment", "Position", "StrategyConfig"]:
        require(context, f"**{term}", CONTEXT)
        require(frontend, term, FRONTEND)

    # Acceptance criteria must be numbered, complete, and cover the decisions
    # locked in the Wayfinder grilling rounds.
    acceptance = section(frontend, "Acceptance criteria")
    numbers = [int(n) for n in re.findall(r"^(\d+)\. ", acceptance, re.M)]
    if numbers != list(range(1, 16)):
        raise AssertionError(f"frontend-v0.md: expected acceptance criteria 1..15, got {numbers}")
    for phrase in [
        "bottom nav",
        "all-account page",
        "KPI drawer",
        "entry-producing actions",
        "newest-first",
        "MANUAL, SEMI_AUTO, and FULL_AUTO",
        "operator confirmation",
        "StrategyConfig` control plane",
        "independent disabled target configuration",
        "chronological AuditEvent/command hub",
        "one primary account-scoped chart plus watchlist",
        "shadcn components",
        "toast/popup",
        "deduplicate by incident",
        "Telegram-handoff preview",
        "exact account/detail context",
        "Idempotency-Key",
        "global emergency acceptance deliberately omits `expected_version`",
        "global `resume-reconcile` supplies the current parent-operation version",
        "snapshot-required",
        "MarketChart",
        "DEMO`/`LIVE",
    ]:
        require(acceptance, phrase, FRONTEND)

    # Guard against scope violations: browser must not speak to the connector
    # and all command-specific sections must retain account scope.
    require(frontend, "Direct broker, MT5, or WebSocket connector access from the browser.", FRONTEND)
    scoped_sections = {
        "Opportunities": "account-scoped",
        "Positions": "account-scoped",
        "Strategies": "per-account",
        "Command interaction contract": "account path",
    }
    for heading, scope_marker in scoped_sections.items():
        require(section(frontend, heading), scope_marker, FRONTEND)

    # Enforce the previously missed edge contracts, not merely their vocabulary.
    ownership = section(frontend, "API ownership map")
    for route in [
        "/broker-accounts/{id}/candles",
        "/broker-accounts/{id}/market-state",
        "/broker-accounts/{id}/opportunities",
        "/broker-accounts/{id}/signals",
        "/broker-accounts/{id}/risk-assessments",
        "/broker-accounts/{id}/positions",
        "/broker-accounts/{id}/orders",
        "/broker-accounts/{id}/fills",
        "/broker-accounts/{id}/audit-events",
        "/broker-accounts/{id}/strategy-configs",
    ]:
        require(ownership, route, FRONTEND)
        require(backend, f"GET {route}", BACKEND)

    command_contract = section(frontend, "Command interaction contract")
    for phrase in [
        "sole V0 global exposure-control acceptance command",
        "deliberately omits `expected_version`",
        "POST /global-emergency-operations/{operation_id}/resume-reconcile",
        "current parent-operation `expected_version`",
        "remains `IN_PROGRESS` until every target converges to `COMPLETED`",
        "ATTENTION_REQUIRED` child remains visibly unresolved",
    ]:
        require(command_contract, phrase, FRONTEND)

    realtime = section(frontend, "Realtime state model")
    for phrase in [
        "system_watermark",
        "system stream reloads `GET /dashboard-summary-snapshot`",
        "account snapshot as a system snapshot",
    ]:
        require(realtime, phrase, FRONTEND)

    print("frontend-v0 spec cross-contract validation: PASS")


if __name__ == "__main__":
    main()
