"""Apply the ordered SQL migrations before the API accepts traffic."""

from __future__ import annotations

from collections.abc import Iterable
import os
from pathlib import Path
from urllib.parse import quote


class SchemaCompatibilityError(RuntimeError):
    """The database cannot be safely opened by this image."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def _migration_number(version: str) -> int:
    try:
        return int(version.split("_", 1)[0])
    except (AttributeError, ValueError):
        raise SchemaCompatibilityError(
            "INVALID_MIGRATION_VERSION", f"invalid migration version: {version}"
        ) from None


def validate_schema_compatibility(
    applied_versions: Iterable[str], available_versions: Iterable[str]
) -> None:
    """Reject a newer database before an older image can start.

    Migrations only move forward. The image may apply missing migrations, but
    it may never reinterpret or remove a version already recorded by the
    database.
    """
    applied = set(applied_versions)
    available = set(available_versions)
    unknown = applied - available
    if unknown:
        versions = ", ".join(sorted(unknown))
        raise SchemaCompatibilityError(
            "DATABASE_SCHEMA_NEWER_THAN_IMAGE",
            f"database contains migrations unavailable to this image: {versions}",
        )
    if applied and available:
        applied_head = max(_migration_number(version) for version in applied)
        image_head = max(_migration_number(version) for version in available)
        if applied_head > image_head:
            raise SchemaCompatibilityError(
                "DATABASE_SCHEMA_NEWER_THAN_IMAGE",
                "database schema is newer than the migration set in this image",
            )


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
    from psycopg import connect

    database_url = database_url_from_environment()
    if not database_url:
        raise RuntimeError("DATABASE_URL or DATABASE_* configuration is required for migrations")
    migrations = sorted(Path("/app/migrations").glob("*.sql"))
    if not migrations:
        raise RuntimeError("no migration files found")
    available_versions = {migration.stem for migration in migrations}
    with connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SELECT to_regclass('public.schema_migrations')")
            migration_table_exists = cursor.fetchone()[0] is not None
            if migration_table_exists:
                cursor.execute("SELECT version FROM schema_migrations")
                applied_versions = {row[0] for row in cursor.fetchall()}
            else:
                applied_versions = set()
        validate_schema_compatibility(applied_versions, available_versions)
        for migration in migrations:
            if migration.stem in applied_versions:
                continue
            with connection.cursor() as cursor:
                cursor.execute(migration.read_text(encoding="utf-8"))
            connection.commit()
            applied_versions.add(migration.stem)
        with connection.cursor() as cursor:
            cursor.execute(
                """INSERT INTO application_metadata (key, value)
                   VALUES ('schema_head', %s)
                   ON CONFLICT (key) DO UPDATE
                   SET value = EXCLUDED.value, updated_at = now()""",
                (max(available_versions, key=_migration_number),),
            )
        connection.commit()


if __name__ == "__main__":
    main()
