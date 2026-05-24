# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: TC003
"""Unit tests for ``app.db``: SQLite connection factory and schema initialization.

Spec contract (from ``docs/planning/tech-spec.md`` §3 and ``CLAUDE.md``):

* Schema initialization creates the six tables: ``entities``, ``holdings``,
  ``performance``, ``positions``, ``documents``, ``refresh_log``.
* ``PRAGMA journal_mode=WAL`` is set on every connection.
* ``PRAGMA busy_timeout=5000`` is set on every connection.
* Schema initialization is idempotent (safe to call repeatedly).

These tests use ``pytest.importorskip`` so they skip gracefully until Phase 1
implements ``app.db``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

db = pytest.importorskip("app.db")


EXPECTED_TABLES = frozenset(
    {
        "entities",
        "holdings",
        "performance",
        "positions",
        "documents",
        "refresh_log",
    }
)


# --------------------------------------------------------------------------- #
# init_schema
# --------------------------------------------------------------------------- #


def _table_names(path: Path) -> set[str]:
    """Return the set of user table names in the SQLite file at ``path``.

    # noqa
    """
    with sqlite3.connect(path) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    return {row[0] for row in rows}


def test_init_schema_creates_all_required_tables(tmp_db_path: Path) -> None:
    """All six spec-required tables exist after a fresh init.

    # noqa
    """
    db.init_schema(str(tmp_db_path))
    assert EXPECTED_TABLES.issubset(_table_names(tmp_db_path))


def test_init_schema_is_idempotent(tmp_db_path: Path) -> None:
    """Running init twice does not raise and leaves the schema intact.

    # noqa
    """
    db.init_schema(str(tmp_db_path))
    db.init_schema(str(tmp_db_path))
    assert EXPECTED_TABLES.issubset(_table_names(tmp_db_path))


def test_init_schema_creates_database_file(tmp_db_path: Path) -> None:
    """The SQLite file is created if absent.

    # noqa
    """
    assert not tmp_db_path.exists()
    db.init_schema(str(tmp_db_path))
    assert tmp_db_path.exists()


def test_entities_table_has_required_columns(tmp_db_path: Path) -> None:
    """The entities table has all spec-required columns.

    # noqa
    """
    db.init_schema(str(tmp_db_path))
    with sqlite3.connect(tmp_db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(entities)")}
    required = {"id", "name", "type", "state", "status", "next_date", "fetched_at"}
    assert required.issubset(cols)


def test_documents_table_has_required_columns(tmp_db_path: Path) -> None:
    """The documents table has all spec-required columns.

    # noqa
    """
    db.init_schema(str(tmp_db_path))
    with sqlite3.connect(tmp_db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    required = {"id", "name", "category", "added_at", "proxy_url", "fetched_at"}
    assert required.issubset(cols)


def test_refresh_log_table_has_required_columns(tmp_db_path: Path) -> None:
    """The refresh_log table has all spec-required columns.

    # noqa
    """
    db.init_schema(str(tmp_db_path))
    with sqlite3.connect(tmp_db_path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(refresh_log)")}
    required = {"id", "service", "status", "ran_at"}
    assert required.issubset(cols)


# --------------------------------------------------------------------------- #
# Connection pragmas (WAL + busy_timeout)
# --------------------------------------------------------------------------- #


@pytest.fixture
def configured_db(
    tmp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Init the schema and point ``SQLITE_PATH`` at it for ``get_connection()``.

    Without this, ``db.get_connection()`` (which reads ``SQLITE_PATH`` from
    env) would open a different file than ``init_schema`` just populated.

    # noqa
    """
    monkeypatch.setenv("SQLITE_PATH", str(tmp_db_path))
    db.init_schema(str(tmp_db_path))
    return tmp_db_path


async def test_async_connection_uses_wal_journal(configured_db: Path) -> None:
    """Async readers must be on a WAL-mode database (concurrent reads safe).

    # noqa
    """
    del configured_db
    async with db.get_connection() as conn:
        cursor = await conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
    assert row is not None
    assert str(row[0]).lower() == "wal"


async def test_async_connection_sets_busy_timeout(configured_db: Path) -> None:
    """The connection's busy_timeout is at least the spec-required 5000 ms.

    # noqa
    """
    del configured_db
    async with db.get_connection() as conn:
        cursor = await conn.execute("PRAGMA busy_timeout")
        row = await cursor.fetchone()
    assert row is not None
    assert int(row[0]) >= 5000


async def test_async_connection_round_trip_select(configured_db: Path) -> None:
    """A trivial SELECT through the async connection returns the expected row.

    # noqa
    """
    del configured_db
    async with db.get_connection() as conn:
        cursor = await conn.execute("SELECT 1")
        row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 1
