# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""SQLite connection factory and schema initialization.

Async readers (route handlers) use ``get_connection``; synchronous writers
(APScheduler refresh jobs) use ``connect_sync``. Both set WAL journaling and a
5 second busy timeout so readers never block the single writer.

#CRITICAL: data integrity: WAL allows concurrent async readers alongside one
synchronous writer; two writers would contend on the database lock.
#VERIFY: every refresh write goes through ``app.scheduler._WRITE_LOCK`` and
the app runs as a single process (uvicorn ``--workers 1``).
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import aiosqlite

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS entities (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    type        TEXT,
    state       TEXT,
    agent       TEXT,
    status      TEXT,
    next_date   TEXT,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS holdings (
    id              TEXT PRIMARY KEY,
    security_name   TEXT NOT NULL,
    sector          TEXT,
    current_value   REAL,
    allocation_pct  REAL,
    fetched_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS performance (
    date         TEXT PRIMARY KEY,
    total_value  REAL,
    benchmark    REAL,
    fetched_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id          TEXT PRIMARY KEY,
    asset       TEXT NOT NULL,
    quantity    REAL,
    usd_value   REAL,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    category         TEXT NOT NULL,
    entity_id        TEXT,
    document_type    TEXT,
    document_date    TEXT,
    is_confidential  INTEGER NOT NULL DEFAULT 0,
    added_at         TEXT,
    modified_at      TEXT,
    proxy_url        TEXT,
    fetched_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refresh_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    service  TEXT NOT NULL,
    status   TEXT NOT NULL CHECK (status IN ('success', 'error')),
    message  TEXT,
    rows     INTEGER,
    ran_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_refresh_log_service_ran_at
    ON refresh_log (service, ran_at);

-- Current account-level balance snapshot per account (MVP choice M-1).
-- #CRITICAL: financial: values are stored as integer cents to avoid binary
-- floating point drift in totals. #VERIFY writers round with Decimal before
-- converting to cents.
CREATE TABLE IF NOT EXISTS account_balances (
    account_id    TEXT PRIMARY KEY,
    account_name  TEXT NOT NULL,
    entity_id     TEXT,
    category      TEXT NOT NULL,
    source        TEXT NOT NULL,
    value_cents   INTEGER NOT NULL,
    as_of         TEXT NOT NULL,
    fetched_at    TEXT NOT NULL
);

-- Daily history of account balances (D-16). This table is durable history,
-- not a cache: it must be included in backups.
CREATE TABLE IF NOT EXISTS balances_daily (
    date          TEXT NOT NULL,
    account_id    TEXT NOT NULL,
    entity_id     TEXT,
    category      TEXT NOT NULL,
    value_cents   INTEGER NOT NULL,
    as_of         TEXT NOT NULL,
    PRIMARY KEY (date, account_id)
);
"""


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    """Set WAL journaling and the busy timeout on a synchronous connection.

    Args:
        conn (sqlite3.Connection): Open connection to configure.
    """
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")


def connect_sync(path: str) -> sqlite3.Connection:
    """Open a synchronous connection for scheduler writes.

    Args:
        path (str): SQLite database file path.

    Returns:
        sqlite3.Connection: Connection with WAL and busy timeout applied.
    """
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    _apply_pragmas(conn)
    return conn


# Ordered schema migrations applied after the base schema. Each entry moves
# ``PRAGMA user_version`` to its number. Append new entries; never edit or
# reorder existing ones, because deployed databases have already run them.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
        ALTER TABLE account_balances ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD';
        ALTER TABLE balances_daily ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD';
        """,
    ),
)


def schema_version(conn: sqlite3.Connection) -> int:
    """Return the database's applied migration number.

    Args:
        conn (sqlite3.Connection): Open connection.

    Returns:
        int: ``PRAGMA user_version`` (0 for a database with no migrations).
    """
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def init_schema(path: str) -> None:
    """Create the database, then apply any migrations it has not run yet.

    Safe to call on every startup.

    Args:
        path (str): SQLite database file path.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sync(path)
    try:
        conn.executescript(_SCHEMA)
        current = schema_version(conn)
        for version, script in MIGRATIONS:
            if version <= current:
                continue
            conn.executescript(
                f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;"
            )
        conn.commit()
    finally:
        conn.close()


def sqlite_path() -> str:
    """Return the configured SQLite path from ``SQLITE_PATH``.

    Returns:
        str: Database file path.

    Raises:
        RuntimeError: If ``SQLITE_PATH`` is not set.
    """
    path = os.environ.get("SQLITE_PATH")
    if not path:
        msg = "SQLITE_PATH is not set"
        raise RuntimeError(msg)
    return path


@asynccontextmanager
async def get_connection() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Open an async read connection to the configured database.

    Yields:
        aiosqlite.Connection: Connection with row access by column name.
    """
    conn = await aiosqlite.connect(sqlite_path(), timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        yield conn
    finally:
        await conn.close()
