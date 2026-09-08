"""Read-only application foundation for the operator System surface."""

from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal, TypedDict
from uuid import uuid4

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from psycopg import connect

from .broker_accounts import AccountError, AccountRegistry
from .market_data import Candle, PairMapping, QuoteTelemetry, market_data
from .strategies import StrategyConfig, canonical_configs, evaluate_snapshot
from .risk_calendar import (
    AccountSafety,
    ActivationGate,
    CalendarHealth,
    EnrichmentPolicy,
    RiskLimits,
    RiskLimitsStore,
)
from .signals import SignalStore
from .execution import ExecutionError, ExecutionSubstrate


@dataclass(frozen=True)
class Settings:
    app_name: str = "Trading Engine"
    version: str = "0.1.0"
    public_origin: str = "http://localhost:3000"
    database_url: str = ""

    @classmethod
    def from_environment(cls) -> "Settings":
        return cls(
            version=os.getenv("APP_VERSION", cls.version),
            public_origin=os.getenv("PUBLIC_ORIGIN", cls.public_origin),
            database_url=os.getenv("DATABASE_URL", ""),
        )


class ServiceStates(TypedDict):
    api: str
    database: str
    connector: str


class SystemStatus(TypedDict):
    status: str
    version: str
    services: ServiceStates
    execution_available: bool
    trading_enabled: bool
    message: str


settings = Settings.from_environment()
app = FastAPI(title=settings.app_name, version=settings.version, docs_url="/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.public_origin],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["Content-Type"],
)

accounts = AccountRegistry()
strategy_configs: dict[str, tuple[StrategyConfig, ...]] = {}
opportunities: dict[str, list[dict[str, Any]]] = {}
signals = SignalStore()
execution = ExecutionSubstrate()
risk_limits = RiskLimitsStore()
enrichment_policies: dict[tuple[str, str], EnrichmentPolicy] = {}
calendar_health: dict[str, dict[str, CalendarHealth]] = {}
safety = AccountSafety()
activation_gate = ActivationGate()


def _strategy_configs_for(account_id: str) -> tuple[StrategyConfig, ...]:
    return strategy_configs.setdefault(account_id, canonical_configs(account_id))


class BrokerAccountRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=80)
    broker_server: str = Field(min_length=1, max_length=160)
    external_account_id: str = Field(min_length=1, max_length=160)
    display_name: str = Field(min_length=1, max_length=160)
    environment: Literal["DEMO", "LIVE"]


class ConnectorBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret: str = Field(min_length=32, max_length=512)


class CandleIngestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pair: str = Field(min_length=1, max_length=40)
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    tick_volume: int = 0
    real_volume: int = 0
    spread: Decimal = Decimal("0")
    source_revision: str = Field(min_length=1, max_length=80)
    is_closed: bool


class QuoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pair: str = Field(min_length=1, max_length=40)
    bid: Decimal
    ask: Decimal
    observed_at: datetime


class PairMappingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    canonical_code: str = Field(min_length=1, max_length=40)
    broker_symbol: str = Field(min_length=1, max_length=80)
    base_currency: str = Field(default="", max_length=3)
    quote_currency: str = Field(default="", max_length=3)


class StrategyEvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_version_id: str = Field(min_length=1, max_length=200)
    snapshot: dict[str, Any]


class RiskLimitsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_risk_per_trade: Decimal = Decimal("0.005")
    daily_loss_limit: Decimal = Decimal("0.02")
    max_open_positions: int = 3
    max_total_open_risk: Decimal = Decimal("0.02")
    max_currency_exposure: Decimal = Decimal("0.01")
    max_spread_multiple: Decimal = Decimal("2")
    max_slippage_r: Decimal = Decimal("0.15")
    max_volatility_atr_multiple: Decimal = Decimal("2.5")
    baseline_window_sessions: int = Field(default=20, ge=1)
    baseline_minimum_samples: int = Field(default=20, ge=1)
    reason: str = Field(min_length=1, max_length=500)


class CalendarHealthRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    currency: str = Field(min_length=3, max_length=3)
    provider: str = Field(min_length=1, max_length=80)
    observed_at: datetime
    healthy: bool = True
    covered: bool = True
    source_revision: str = Field(min_length=1, max_length=100)


class ActivationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    connector_capabilities: dict[str, bool] = Field(default_factory=dict)
    session_allowed: bool = True
    wti_gate: str = "OPEN"


class EnrichmentPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    required_currencies: tuple[str, ...] = ()
    event_kinds: tuple[str, ...] = ()
    blackout_before_minutes: int = Field(ge=0)
    blackout_after_minutes: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=500)


class SessionChoiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_policy: Literal["ALL_BROKER_OPEN", "LONDON", "NEW_YORK", "LONDON_NEW_YORK_OVERLAP"]
    reason: str = Field(min_length=1, max_length=500)


class SignalEnrichmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    opportunity_id: str = Field(min_length=1, max_length=200)
    market_snapshot_id: str = Field(min_length=1, max_length=200)
    policy_version: int = Field(ge=1)
    entry_zone: dict[str, Any] = Field(default_factory=dict)
    stop_loss: Decimal = Decimal("0")
    take_profit: tuple[Decimal, ...] = ()
    ttl_seconds: int = Field(default=1800, ge=1, le=86400)
    baseline_samples: int = Field(default=0, ge=0)
    daily_loss: Decimal = Decimal("0")
    open_positions: int = Field(default=0, ge=0)
    open_risk: Decimal = Decimal("0")
    requested_risk: Decimal = Decimal("0")
    spread_multiple: Decimal | None = None
    volatility_multiple: Decimal | None = None
    policy_healthy: bool = True
    calendar_blackout: bool = False


class OperatorActionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    idempotency_key: str = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=1, max_length=500)
    confirmed: bool = False
    signal_revision: int = Field(default=1, ge=1)


class ExecuteSignalRequest(OperatorActionRequest):
    order_payload: dict[str, Any] = Field(default_factory=dict)
    risk_amount: Decimal = Decimal("0")


def _account_error(error: AccountError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": error.code, "message": str(error)},
    )


def _strategy_config_for(account_id: str, config_id: str) -> StrategyConfig:
    config = next(
        (item for item in _strategy_configs_for(account_id) if item.id == config_id),
        None,
    )
    if config is None:
        raise HTTPException(status_code=404, detail="StrategyConfig version not found")
    return config


def _risk_limits_payload(limits: RiskLimits) -> dict[str, Any]:
    return {
        key: str(value) if isinstance(value, Decimal) else value
        for key, value in limits.__dict__.items()
    }


def _database_state() -> str:
    if not settings.database_url:
        return "unavailable"
    try:
        with connect(settings.database_url, connect_timeout=2) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        return "healthy"
    except Exception:
        return "unavailable"


def _service_state() -> ServiceStates:
    """Return conservative status values; absence is never treated as healthy."""

    return {"api": "healthy", "database": _database_state(), "connector": "unavailable"}


@app.get("/health/live", tags=["health"])
def live_health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name, "version": settings.version}


@app.get("/api/v1/system/version", tags=["system"])
def system_version() -> dict[str, str]:
    return {"service": settings.app_name, "version": settings.version}


@app.get("/api/v1/system/status", tags=["system"])
def system_status() -> SystemStatus:
    services = _service_state()
    return {
        "status": (
            "healthy"
            if all(value == "healthy" for value in services.values())
            else "degraded"
        ),
        "version": settings.version,
        "services": services,
        "execution_available": False,
        "trading_enabled": False,
        "message": (
            "Sistem siap untuk pemantauan; konektor broker belum tersedia."
        ),
    }


@app.post("/api/v1/broker-accounts", status_code=status.HTTP_201_CREATED, tags=["broker-accounts"])
def register_broker_account(request: BrokerAccountRegistration) -> dict[str, Any]:
    try:
        account = accounts.register(**request.model_dump())
        strategy_configs[account.id] = canonical_configs(account.id)
    except AccountError as error:
        raise _account_error(error) from error
    return {
        "id": account.id,
        "provider": account.provider,
        "broker_server": account.broker_server,
        "external_account_id": account.external_account_id,
        "display_name": account.display_name,
        "environment": account.environment,
        "lifecycle_status": account.lifecycle_status,
        "bot_state": account.bot_state,
        "execution_mode": account.execution_mode,
        "live_execution_enabled": account.live_execution_enabled,
    }


@app.get("/api/v1/broker-accounts/{account_id}/strategy-configs", tags=["strategies"])
def list_strategy_configs(account_id: str) -> dict[str, Any]:
    _require_account(account_id)
    configs = _strategy_configs_for(account_id)
    return {
        "account_id": account_id,
        "configs": [
            {
                "id": config.id,
                "template_key": config.template_key,
                "pair": config.pair,
                "strategy": config.strategy,
                "version": config.version,
                "activation_status": config.activation_status,
            }
            for config in configs
        ],
        "has_more": False,
    }


@app.post("/api/v1/broker-accounts/{account_id}/risk-limits", status_code=status.HTTP_201_CREATED, tags=["risk"])
def create_risk_limits(account_id: str, request: RiskLimitsRequest) -> dict[str, Any]:
    _require_account(account_id)
    version = risk_limits.create(
        RiskLimits(broker_account_id=account_id, **request.model_dump())
    )
    accounts.accounts[account_id].risk_limits_active = True
    return {
        "account_id": account_id,
        "version": version.version,
        "risk_limits": _risk_limits_payload(version),
    }


@app.post("/api/v1/broker-accounts/{account_id}/calendar/health", status_code=status.HTTP_201_CREATED, tags=["calendar"])
def set_calendar_health(account_id: str, request: CalendarHealthRequest) -> dict[str, Any]:
    _require_account(account_id)
    health = CalendarHealth(**request.model_dump())
    previous = calendar_health.setdefault(account_id, {}).get(health.currency)
    calendar_health.setdefault(account_id, {})[health.currency] = health
    fence = None
    health_changed = (
        previous is None
        or previous.healthy != health.healthy
        or previous.covered != health.covered
        or previous.source_revision != health.source_revision
    )
    if health_changed:
        fence = safety.install_fence(account_id)
    return {
        "account_id": account_id,
        **health.__dict__,
        "observed_at": health.observed_at.isoformat(),
        "fence": fence.__dict__ if fence else None,
    }


@app.post("/api/v1/broker-accounts/{account_id}/strategy-configs/{config_id}/enrichment-policies", status_code=status.HTTP_201_CREATED, tags=["strategies"])
def create_enrichment_policy(account_id: str, config_id: str, request: EnrichmentPolicyRequest) -> dict[str, Any]:
    _require_account(account_id)
    _strategy_config_for(account_id, config_id)
    prior = enrichment_policies.get((account_id, config_id))
    policy = EnrichmentPolicy(
        strategy_config_id=config_id,
        broker_account_id=account_id,
        version=(prior.version + 1 if prior else 1),
        required_currencies=request.required_currencies,
        event_kinds=request.event_kinds,
        blackout_before_minutes=request.blackout_before_minutes,
        blackout_after_minutes=request.blackout_after_minutes,
        reason=request.reason,
    )
    enrichment_policies[(account_id, config_id)] = policy
    return {"account_id": account_id, **policy.__dict__}


@app.post("/api/v1/broker-accounts/{account_id}/strategy-configs/{config_id}/session", status_code=status.HTTP_201_CREATED, tags=["strategies"])
def choose_strategy_session(account_id: str, config_id: str, request: SessionChoiceRequest) -> dict[str, Any]:
    """Session choice is a new disabled immutable version, never an in-place edit."""
    _require_account(account_id)
    configs = _strategy_configs_for(account_id)
    config = _strategy_config_for(account_id, config_id)
    version = config.version + 1
    updated = replace(
        config,
        id=f"{config.id.rsplit('-v', 1)[0]}-v{version}",
        version=version,
        session_policy=request.session_policy,
        activation_status="DISABLED",
    )
    strategy_configs[account_id] = configs + (updated,)
    return {
        "account_id": account_id,
        "id": updated.id,
        "version": updated.version,
        "session_policy": updated.session_policy,
        "activation_status": updated.activation_status,
    }


@app.post("/api/v1/broker-accounts/{account_id}/strategy-configs/{config_id}/activate", tags=["strategies"])
def activate_strategy_config(
    account_id: str,
    config_id: str,
    request: ActivationRequest = ActivationRequest(),
) -> dict[str, Any]:
    _require_account(account_id)
    configs = _strategy_configs_for(account_id)
    config = _strategy_config_for(account_id, config_id)
    mapping = market_data.pairs.get(account_id, {}).get(config.pair)
    decision = activation_gate.evaluate(
        account_id=account_id,
        pair=config.pair,
        mapping=mapping,
        connector_capabilities=request.connector_capabilities,
        risk_limits=risk_limits.active(account_id),
        policy=enrichment_policies.get((account_id, config_id)),
        calendar_health=calendar_health.get(account_id, {}),
        session_allowed=request.session_allowed,
        wti_gate=request.wti_gate,
    )
    if not decision.allowed:
        return {
            "account_id": account_id,
            "config_id": config_id,
            "activation_status": "DISABLED",
            "outcome": "ACTIVATION_BLOCKED",
            "reason_codes": list(decision.reason_codes),
        }
    updated = replace(config, activation_status="ACTIVE")
    strategy_configs[account_id] = tuple(updated if item.id == config_id else item for item in configs)
    return {
        "account_id": account_id,
        "config_id": config_id,
        "activation_status": "ACTIVE",
        "outcome": "ACTIVATION_ALLOWED",
        "reason_codes": [],
    }


@app.post("/api/v1/broker-accounts/{account_id}/strategy-evaluations", tags=["strategies"])
def evaluate_strategy(account_id: str, request: StrategyEvaluationRequest) -> dict[str, Any]:
    _require_account(account_id)
    configs = _strategy_configs_for(account_id)
    config = next((item for item in configs if item.id == request.config_version_id), None)
    if config is None:
        raise HTTPException(status_code=404, detail="StrategyConfig version not found")
    result = evaluate_snapshot({**request.snapshot, "account_id": account_id}, config)
    payload = result.as_dict()
    if result.opportunity:
        opportunity = deepcopy(payload["opportunity"])
        opportunity["id"] = f"opportunity-{uuid4()}"
        opportunity["market_snapshot_id"] = str(request.snapshot.get("snapshot_id", ""))
        opportunity["strategy_config_account_id"] = account_id
        opportunity["market_snapshot_account_id"] = account_id
        opportunity["created_at"] = datetime.now(timezone.utc).isoformat()
        opportunities.setdefault(account_id, []).append(opportunity)
        payload["opportunity"] = deepcopy(opportunity)
    return payload


def _signals_for_account(account_id: str) -> list[dict[str, Any]]:
    account_signals = [
        signal.as_dict()
        for signal in signals.signals.values()
        if signal.account_id == account_id
    ]
    return sorted(
        account_signals,
        key=lambda signal: signal["created_at"],
        reverse=True,
    )


def _signals_for_opportunity(account_id: str, opportunity_id: Any) -> list[dict[str, Any]]:
    opportunity_signals = [
        signal.as_dict()
        for signal in signals.signals.values()
        if signal.account_id == account_id
        and signal.opportunity.get("id") == opportunity_id
    ]
    return sorted(
        opportunity_signals,
        key=lambda signal: signal["created_at"],
        reverse=True,
    )


def _policy_is_healthy(account_id: str, policy: EnrichmentPolicy | None) -> bool:
    if policy is None:
        return False

    ttl = policy.freshness_ttl_seconds.get("calendar", 3600)
    account_health = calendar_health.get(account_id, {})
    for currency in policy.required_currencies:
        health = account_health.get(currency)
        if health is None or not health.is_fresh(ttl_seconds=ttl):
            return False
    return True


def _backend_risk_context(
    account_state: str,
    policy: EnrichmentPolicy | None,
    account_id: str,
) -> dict[str, Any]:
    # Risk context is derived from backend-owned account and policy state.
    return {
        "baseline_samples": 0,
        "account_state": account_state,
        "policy_healthy": _policy_is_healthy(account_id, policy),
        "calendar_blackout": False,
    }


@app.get("/api/v1/broker-accounts/{account_id}/opportunities", tags=["strategies"])
def list_opportunities(account_id: str) -> dict[str, Any]:
    _require_account(account_id)
    return {
        "account_id": account_id,
        "opportunities": [
            {
                **item,
                "signals": _signals_for_opportunity(account_id, item.get("id")),
            }
            for item in sorted(
                opportunities.get(account_id, []),
                key=lambda item: item.get("created_at", ""),
                reverse=True,
            )
        ],
        "has_more": False,
    }


@app.post("/api/v1/broker-accounts/{account_id}/signals", status_code=status.HTTP_201_CREATED, tags=["signals"])
def create_signal(account_id: str, request: SignalEnrichmentRequest) -> dict[str, Any]:
    _require_account(account_id)
    opportunity = next(
        (item for item in opportunities.get(account_id, []) if item.get("id") == request.opportunity_id),
        None,
    )
    if opportunity is None:
        raise HTTPException(status_code=404, detail="Opportunity not found")
    if request.market_snapshot_id and request.market_snapshot_id != opportunity.get("market_snapshot_id"):
        raise HTTPException(status_code=409, detail="MarketStateSnapshot does not match Opportunity")
    config = _strategy_config_for(account_id, str(opportunity["strategy_config_version_id"]))
    policy = enrichment_policies.get((account_id, config.id))
    if policy is None or policy.version != request.policy_version:
        raise HTTPException(status_code=409, detail="EnrichmentPolicy version is not account-scoped/current")
    limits = risk_limits.active(account_id)
    account = accounts.accounts[account_id]
    risk_kwargs = _backend_risk_context(account.bot_state, policy, account_id)
    try:
        signal = signals.create(
            account_id=account_id,
            opportunity=opportunity,
            market_snapshot_id=opportunity.get("market_snapshot_id", ""),
            policy_version=request.policy_version,
            ttl=timedelta(seconds=request.ttl_seconds),
            entry_zone=request.entry_zone,
            stop_loss=request.stop_loss,
            take_profit=request.take_profit,
            limits=limits, risk_kwargs=risk_kwargs,
        )
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return signal.as_dict()


@app.get("/api/v1/broker-accounts/{account_id}/signals", tags=["signals"])
def list_signals(account_id: str) -> dict[str, Any]:
    _require_account(account_id)
    return {
        "account_id": account_id,
        "signals": _signals_for_account(account_id),
        "has_more": False,
    }


@app.post("/api/v1/broker-accounts/{account_id}/signals/{signal_id}/revise", status_code=status.HTTP_201_CREATED, tags=["signals"])
def revise_signal(account_id: str, signal_id: str, policy_version: int = 1) -> dict[str, Any]:
    _require_account(account_id)
    try:
        signal = signals.get(signal_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Signal not found") from error
    if signal.account_id != account_id:
        raise HTTPException(status_code=404, detail="Signal not found")
    policy = enrichment_policies.get((account_id, signal.strategy_config_version_id))
    if policy is None or policy.version != policy_version:
        raise HTTPException(status_code=409, detail="EnrichmentPolicy version is not account-scoped/current")
    limits = risk_limits.active(account_id)
    return signals.create_revision(signal_id, policy_version=policy_version,
                                   limits=limits).as_dict()


@app.post("/api/v1/broker-accounts/{account_id}/signals/{signal_id}/approve", tags=["execution"])
def approve_signal(account_id: str, signal_id: str, request: OperatorActionRequest) -> dict[str, Any]:
    _require_account(account_id)
    try:
        signal = signals.get(signal_id)
        if signal.account_id != account_id:
            raise ValueError("SIGNAL_NOT_FOUND")
        command = execution.approve_signal(
            account_id=account_id, signal_id=signal_id, idempotency_key=request.idempotency_key,
            reason=request.reason, confirmed=request.confirmed, signal_revision=signal.revision,
            signal_eligible=(signal.as_dict()["status"] == "ELIGIBLE" and signal.revision == request.signal_revision),
        )
        if command.status == "REJECTED":
            raise ExecutionError(command.rejection_code or "APPROVAL_REJECTED")
        signal = signals.approve(signal_id, account_id=account_id, revision=request.signal_revision)
    except (KeyError, ValueError, ExecutionError) as error:
        code = getattr(error, "code", str(error))
        raise HTTPException(status_code=409, detail={"code": code}) from error
    return {"account_id": account_id, "signal": signal.as_dict(), "command": command.__dict__}


@app.post("/api/v1/broker-accounts/{account_id}/signals/{signal_id}/execute", tags=["execution"])
def execute_signal(account_id: str, signal_id: str, request: ExecuteSignalRequest) -> dict[str, Any]:
    _require_account(account_id)
    try:
        signal = signals.get(signal_id)
        if signal.account_id != account_id:
            raise ValueError("SIGNAL_NOT_FOUND")
        account = accounts.accounts[account_id]
        result = execution.execute_signal(
            account_id=account_id, signal_id=signal_id, idempotency_key=request.idempotency_key,
            reason=request.reason, confirmed=request.confirmed, signal_revision=request.signal_revision,
            risk_approved=signal.risk_assessment.approved, signal_fresh=signal.as_dict()["status"] == "APPROVED",
            fence_safe=execution.account(account_id).exposure_gate == "OPEN",
            account_state=account.bot_state, live_lock=account.connector_healthy and account.connector_bound,
            execution_epoch=execution.account(account_id).execution_epoch,
            order_payload=request.order_payload, risk_amount=str(request.risk_amount),
        )
    except (KeyError, ValueError, ExecutionError) as error:
        code = getattr(error, "code", str(error))
        raise HTTPException(status_code=409, detail={"code": code}) from error
    return {"account_id": account_id, "order": result.order.__dict__, "status": result.order.status}


@app.post("/api/v1/broker-accounts/{account_id}/emergency-stop", tags=["execution"])
def emergency_stop(account_id: str) -> dict[str, Any]:
    _require_account(account_id)
    fence = execution.emergency_stop(account_id)
    return {"account_id": account_id, "fence": fence.__dict__}


@app.post("/api/v1/broker-accounts/{account_id}/close-all", tags=["execution"])
def close_all(account_id: str, request: OperatorActionRequest) -> dict[str, Any]:
    _require_account(account_id)
    try:
        command = execution.close_all(account_id=account_id, idempotency_key=request.idempotency_key,
                                      reason=request.reason, confirmed=request.confirmed)
    except ExecutionError as error:
        raise HTTPException(status_code=409, detail={"code": error.code}) from error
    return {"account_id": account_id, "command": command.__dict__}


@app.post("/api/v1/broker-accounts/{account_id}/connector-binding", tags=["broker-accounts"])
def bind_connector(account_id: str, request: ConnectorBindingRequest) -> dict[str, str]:
    try:
        key_id = accounts.bind_connector(account_id, request.secret)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="BrokerAccount not found") from error
    except AccountError as error:
        raise _account_error(error) from error
    # The secret is accepted only for provisioning and is never returned or logged.
    return {"key_id": key_id}


@app.get("/api/v1/broker-accounts/{account_id}/snapshot", tags=["broker-accounts"])
def broker_account_snapshot(account_id: str) -> dict[str, Any]:
    try:
        return accounts.read_only_snapshot(account_id)
    except KeyError as error:
        raise HTTPException(status_code=404, detail="BrokerAccount not found") from error


def _require_account(account_id: str) -> None:
    if account_id not in accounts.accounts:
        raise HTTPException(status_code=404, detail="BrokerAccount not found")


@app.post("/api/v1/broker-accounts/{account_id}/candles", status_code=status.HTTP_202_ACCEPTED, tags=["markets"])
def ingest_closed_m1(account_id: str, request: CandleIngestRequest) -> dict[str, Any]:
    _require_account(account_id)
    try:
        candle = market_data.ingest_candle(
            account_id,
            Candle(
                account_id=account_id,
                pair=request.pair,
                timeframe="M1",
                open_time=request.open_time,
                close_time=request.close_time,
                open=request.open,
                high=request.high,
                low=request.low,
                close=request.close,
                tick_volume=request.tick_volume,
                real_volume=request.real_volume,
                spread=request.spread,
                source_revision=request.source_revision,
                is_closed=request.is_closed,
            ),
        )
    except AccountError as error:
        raise _account_error(error) from error
    return {"account_id": account_id, "pair": candle.pair, "timeframe": candle.timeframe, "accepted": True}


@app.get("/api/v1/broker-accounts/{account_id}/pairs", tags=["markets"])
def market_pairs(account_id: str) -> dict[str, Any]:
    _require_account(account_id)
    pairs = [mapping.__dict__ for mapping in market_data.pairs.get(account_id, {}).values()]
    return {"account_id": account_id, "pairs": pairs, "has_more": False}


@app.post("/api/v1/broker-accounts/{account_id}/pairs", status_code=status.HTTP_201_CREATED, tags=["markets"])
def register_pair_mapping(account_id: str, request: PairMappingRequest) -> dict[str, Any]:
    _require_account(account_id)
    try:
        mapping = market_data.register_pair(
            account_id,
            PairMapping(account_id=account_id, **request.model_dump()),
        )
    except AccountError as error:
        raise _account_error(error) from error
    return mapping.__dict__


@app.get("/api/v1/broker-accounts/{account_id}/candles", tags=["markets"])
def market_candles(account_id: str, pair: str, timeframe: str = "M15") -> dict[str, Any]:
    _require_account(account_id)
    if timeframe not in {"M1", "M5", "M15", "H1", "H4", "D1"}:
        raise HTTPException(status_code=422, detail="unsupported timeframe")
    candles = market_data.aggregate(account_id, pair, timeframe)  # type: ignore[arg-type]
    return {"account_id": account_id, "pair": pair, "timeframe": timeframe, "candles": [c.__dict__ for c in candles], "has_more": False}


@app.get("/api/v1/broker-accounts/{account_id}/market-state", tags=["markets"])
def market_state(account_id: str, pair: str, timeframe: str = "M15") -> dict[str, Any]:
    _require_account(account_id)
    snapshot = market_data.build_snapshot(account_id, pair, timeframe)  # type: ignore[arg-type]
    return {"account_id": account_id, "pair": pair, "timeframe": timeframe, "snapshot": snapshot.__dict__}


@app.post("/api/v1/broker-accounts/{account_id}/market-data/resync", tags=["markets"])
def resync_market_data(account_id: str, stream: str = "markets") -> dict[str, Any]:
    _require_account(account_id)
    return market_data.resync(account_id, stream)


@app.websocket("/ws/v1/quotes")
async def quote_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        while True:
            message = await websocket.receive_json()
            account_id = message.get("account_id")
            _require_account(account_id)
            quote = QuoteTelemetry(account_id, message["pair"], Decimal(str(message["bid"])), Decimal(str(message["ask"])), datetime.now(timezone.utc))
            market_data.ingest_quote(account_id, quote)
            await websocket.send_json({"type": "quote", "account_id": account_id, "pair": quote.pair, "bid": str(quote.bid), "ask": str(quote.ask), "non_canonical": True})
    except (WebSocketDisconnect, KeyError, HTTPException):
        return


@app.websocket("/ws/v1/connector")
async def connector_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        hello = await websocket.receive_json()
        required = {
            "type",
            "account_id",
            "provider",
            "broker_server",
            "external_account_id",
            "key_id",
            "secret",
            "generation",
            "session_id",
        }
        if set(hello) != required or hello["type"] != "hello":
            await websocket.close(code=1008, reason="invalid authenticated handshake")
            return
        account = accounts.accounts.get(hello["account_id"])
        requested_identity = (
            hello["provider"],
            hello["broker_server"],
            hello["external_account_id"],
        )
        if not account or requested_identity != account.identity:
            await websocket.close(code=1008, reason="WRONG_ACCOUNT")
            return
        try:
            accounts.authenticate(hello["account_id"], hello["key_id"], hello["secret"], hello["generation"])
            accounts.heartbeat(hello["account_id"], hello["generation"], hello["session_id"])
        except AccountError as error:
            await websocket.close(code=1008, reason=error.code)
            return
        await websocket.send_json({"type": "snapshot", "snapshot": accounts.read_only_snapshot(account.id)})
        while True:
            message = await websocket.receive_json()
            if message.get("account_id") != account.id or message.get("generation") != account.connector_generation:
                await websocket.send_json({"type": "error", "code": "STALE_GENERATION" if message.get("account_id") == account.id else "WRONG_ACCOUNT"})
                continue
            if message.get("type") == "heartbeat":
                accounts.heartbeat(account.id, account.connector_generation, message.get("session_id", ""))
                await websocket.send_json({"type": "heartbeat_ack", "account_id": account.id, "generation": account.connector_generation})
            else:
                await websocket.send_json({"type": "error", "code": "READ_ONLY_FOUNDATION"})
    except WebSocketDisconnect:
        return
