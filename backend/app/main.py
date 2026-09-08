"""Read-only application foundation for the operator System surface."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, TypedDict

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from psycopg import connect

from .broker_accounts import AccountError, AccountRegistry


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
    environment: str = Field(pattern="^(DEMO|LIVE)$")


class ConnectorBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret: str = Field(min_length=32, max_length=512)


def _account_error(error: AccountError) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": error.code, "message": str(error)})


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
        "status": "healthy" if all(value == "healthy" for value in services.values()) else "degraded",
        "version": settings.version,
        "services": services,
        "execution_available": False,
        "trading_enabled": False,
        "message": "Sistem siap untuk pemantauan; konektor broker belum tersedia.",
    }


@app.post("/api/v1/broker-accounts", status_code=status.HTTP_201_CREATED, tags=["broker-accounts"])
def register_broker_account(request: BrokerAccountRegistration) -> dict[str, Any]:
    try:
        account = accounts.register(**request.model_dump())
    except AccountError as error:
        raise _account_error(error) from error
    return {"id": account.id, "provider": account.provider, "broker_server": account.broker_server, "external_account_id": account.external_account_id, "display_name": account.display_name, "environment": account.environment, "lifecycle_status": account.lifecycle_status, "bot_state": account.bot_state, "execution_mode": account.execution_mode, "live_execution_enabled": account.live_execution_enabled}


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


@app.websocket("/ws/v1/connector")
async def connector_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    try:
        hello = await websocket.receive_json()
        required = {"type", "account_id", "provider", "broker_server", "external_account_id", "key_id", "secret", "generation", "session_id"}
        if set(hello) != required or hello["type"] != "hello":
            await websocket.close(code=1008, reason="invalid authenticated handshake")
            return
        account = accounts.accounts.get(hello["account_id"])
        if not account or (hello["provider"], hello["broker_server"], hello["external_account_id"]) != account.identity:
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
