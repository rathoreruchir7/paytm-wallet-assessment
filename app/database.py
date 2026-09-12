import hashlib
from pathlib import Path

from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.config import Settings


def make_pool(settings: Settings):
    return AsyncConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=settings.pool_size,
        max_waiting=200,
        timeout=settings.pool_timeout_seconds,
        open=False,
        kwargs={
            "autocommit": True,
            "row_factory": dict_row,
            "connect_timeout": settings.connect_timeout_seconds,
            "prepare_threshold": None,
        },
    )


async def transaction_limits(conn, settings: Settings):
    # One network round trip; values are quoted by the driver, never interpolated SQL.
    await conn.execute(
        sql.SQL("""SET TRANSACTION ISOLATION LEVEL READ COMMITTED;
            SET LOCAL lock_timeout = {};
            SET LOCAL statement_timeout = {};
            SET LOCAL idle_in_transaction_session_timeout = '60s';
            SET LOCAL synchronous_commit = on""").format(
            sql.Literal(f"{settings.lock_timeout_ms}ms"), sql.Literal(f"{settings.statement_timeout_ms}ms")
        ),
        prepare=False,
    )


async def migrate(pool):
    async with pool.connection() as conn, conn.transaction():
        # Only migrations use a global advisory lock; transfers never do.
        await conn.execute("SELECT pg_advisory_xact_lock(734950821)")
        await conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            name TEXT PRIMARY KEY, sha256 TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
        for path in sorted((Path(__file__).resolve().parents[1] / "migrations").glob("*.sql")):
            script = path.read_text()
            digest = hashlib.sha256(script.encode()).hexdigest()
            row = await (
                await conn.execute("SELECT sha256 FROM schema_migrations WHERE name = %s", (path.name,))
            ).fetchone()
            if row:
                if row["sha256"] != digest:
                    raise RuntimeError(f"Applied migration changed: {path.name}")
                continue
            await conn.execute(script, prepare=False)
            await conn.execute(
                "INSERT INTO schema_migrations(name, sha256) VALUES (%s,%s)", (path.name, digest)
            )
