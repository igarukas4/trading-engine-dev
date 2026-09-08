"""Read-only application foundation for the operator System surface."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, TypedDict

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from psycopg import connect

from .broker_accounts import AccountError, AccountRegistry
from .market_data import Candle, PairMapping, QuoteTelemetry, market_data


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


def _account_error(error: AccountError) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": error.code, "message": str(error)},
    )


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
        candle = market_data.ingest_candle(account_id, Candle(account_id, request.pair, "M1", request.open_time, request.close_time,
            request.open, request.high, request.low, request.close, request.tick_volume, request.real_volume,
            request.spread, request.source_revision, request.is_closed))
    except AccountError as error:
        raise HTTPException(status_code=409, detail={"code": error.code, "message": str(error)}) from error
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
        mapping = market_data.register_pair(account_id, PairMapping(account_id, **request.model_dump()))
    except AccountError as error:
        raise HTTPException(status_code=409, detail={"code": error.code, "message": str(error)}) from error
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
