"""Explicit operator evidence required before enabling broker side effects."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


class PreflightError(RuntimeError):
    """Stable, credential-free preflight failure."""


@dataclass(frozen=True)
class PreflightEvidence:
    demo_account: bool
    manual_mode: bool
    terminal_healthy: bool
    trade_allowed: bool
    journal_writable: bool
    generation_current: bool
    epoch_current: bool
    reconciliation_complete: bool
    no_unknown_commands: bool
    backend_execution_gate: bool

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "PreflightEvidence":
        if not isinstance(value, Mapping):
            raise PreflightError("PREFLIGHT_REQUIRED")
        fields = tuple(cls.__dataclass_fields__)
        if any(not isinstance(value.get(field), bool) for field in fields):
            raise PreflightError("PREFLIGHT_INCOMPLETE")
        evidence = cls(**{field: value[field] for field in fields})
        if not all(evidence.__dict__.values()):
            raise PreflightError("PREFLIGHT_FAILED")
        return evidence


def require_preflight(value: PreflightEvidence | Mapping[str, Any] | None) -> PreflightEvidence:
    if isinstance(value, PreflightEvidence):
        if not all(value.__dict__.values()):
            raise PreflightError("PREFLIGHT_FAILED")
        return value
    return PreflightEvidence.from_mapping(value)
