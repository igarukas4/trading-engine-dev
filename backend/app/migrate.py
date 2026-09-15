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
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('public.schema_migrations')")
            migration_table_exists = cursor.fetchone()[0] is not None
            if migration_table_exists:
                cursor.execute("SELECT version FROM schema_migrations")
                applied_versions = {row[0] for row in cursor.fetchall()}
            else:
                applied_versions = set()
        for migration in migrations:
            if migration.stem in applied_versions:
                continue
            with connection.cursor() as cursor:
                cursor.execute(migration.read_text(encoding="utf-8"))
            connection.commit()
            applied_versions.add(migration.stem)


if __name__ == "__main__":
    main()
