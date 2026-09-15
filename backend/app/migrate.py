"""Apply the ordered SQL migrations before the API accepts traffic."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

from psycopg import connect


def database_url_from_environment() -> str:
    database_url = os.getenv("DATABASE_URL", "")
    if database_url:
        return database_url
    password_file = os.getenv("DATABASE_PASSWORD_FILE", "")
    if not password_file:
        return ""
    password = Path(password_file).read_text(encoding="utf-8").strip()
    return (
        "postgresql://"
        f"{quote(os.getenv('DATABASE_USER', 'trading_engine'), safe='')}:"
        f"{quote(password, safe='')}@"
        f"{os.getenv('DATABASE_HOST', 'postgres')}:"
        f"{os.getenv('DATABASE_PORT', '5432')}/"
        f"{os.getenv('DATABASE_NAME', 'trading_engine')}"
    )


def main() -> None:
    database_url = database_url_from_environment()
    if not database_url:
        raise RuntimeError("DATABASE_URL or DATABASE_* configuration is required for migrations")
    migrations = sorted(Path("/app/migrations").glob("*.sql"))
    if not migrations:
        raise RuntimeError("no migration files found")
    with connect(database_url) as connection:
        for migration in migrations:
            with connection.cursor() as cursor:
                cursor.execute(migration.read_text(encoding="utf-8"))
            connection.commit()


if __name__ == "__main__":
    main()
