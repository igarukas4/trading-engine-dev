"""Read-only application foundation for the operator System surface."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from psycopg import connect


def _read_setting(name: str, default: str = "") -> str:
    file_name = os.getenv(f"{name}_FILE")
    if file_name:
        try:
            return Path(file_name).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return os.getenv(name, default)


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


settings = Settings.from_environment()
app = FastAPI(title=settings.app_name, version=settings.version, docs_url="/docs")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.public_origin],
    allow_credentials=False,
    allow_methods=["GET"],
    allow_headers=["Content-Type"],
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


def _service_state() -> dict[str, str]:
    """Return conservative status values; absence is never treated as healthy."""

    return {"api": "healthy", "database": _database_state(), "connector": "unavailable"}


@app.get("/health/live", tags=["health"])
def live_health() -> dict[str, str]:
    return {"status": "ok", "service": settings.app_name, "version": settings.version}


@app.get("/api/v1/system/version", tags=["system"])
def system_version() -> dict[str, str]:
    return {"service": settings.app_name, "version": settings.version}


@app.get("/api/v1/system/status", tags=["system"])
def system_status() -> dict[str, Any]:
    services = _service_state()
    return {
        "status": "healthy" if all(value == "healthy" for value in services.values()) else "degraded",
        "version": settings.version,
        "services": services,
        "execution_available": False,
        "trading_enabled": False,
        "message": "Sistem siap untuk pemantauan; konektor broker belum tersedia.",
    }
