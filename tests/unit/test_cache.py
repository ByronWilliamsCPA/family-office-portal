# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: PLR2004, PLR0913, TC003
"""Unit tests for ``app.cache``: async readers and staleness checker.

Spec contract (``CLAUDE.md`` "Data layer rules" + tech-spec §3, §6):

* Reader functions: ``get_entities``, ``get_holdings``, ``get_positions``,
  ``get_documents``. Each is async; each reads from SQLite via ``aiosqlite``;
  none calls a backend HTTP service.
* ``is_stale(dataset, threshold_hours)`` compares the most recent
  ``fetched_at`` against now and returns True when older than the threshold.
* Staleness thresholds (hours): entities=8, holdings=4, positions=4, documents=24.

Tests use real SQLite (the project tests "use real database operations where
the project already does so in existing tests" -- per the task brief, and
``CLAUDE.md``'s "tests/conftest.py - SQLite fixture DB").
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from freezegun import freeze_time

cache = pytest.importorskip("app.cache")
db = pytest.importorskip("app.db")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _seed_entity(
    path: Path,
    *,
    entity_id: str = "ent-1",
    name: str = "Estate Holdings LLC",
    entity_type: str = "LLC",
    state: str = "WY",
    status: str = "current",
    next_date: str = "2026-12-01",
    fetched_at: str | None = None,
) -> None:
    """Insert one row into the entities table.

    # noqa
    """
    fetched = fetched_at or datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO entities (id, name, type, state, status, next_date, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entity_id, name, entity_type, state, status, next_date, fetched),
        )
        conn.commit()


def _seed_holding(
    path: Path,
    *,
    holding_id: str = "hold-1",
    security_name: str = "Apple Inc.",
    sector: str = "Technology",
    current_value: float = 100_000.0,
    allocation_pct: float = 12.5,
    fetched_at: str | None = None,
) -> None:
    """Insert one row into the holdings table.

    # noqa
    """
    fetched = fetched_at or datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO holdings (id, security_name, sector, current_value, "
            "allocation_pct, fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                holding_id,
                security_name,
                sector,
                current_value,
                allocation_pct,
                fetched,
            ),
        )
        conn.commit()


def _seed_position(
    path: Path,
    *,
    position_id: str = "pos-1",
    asset: str = "BTC",
    quantity: float = 1.5,
    usd_value: float = 90_000.0,
    fetched_at: str | None = None,
) -> None:
    """Insert one row into the positions table.

    # noqa
    """
    fetched = fetched_at or datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO positions (id, asset, quantity, usd_value, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (position_id, asset, quantity, usd_value, fetched),
        )
        conn.commit()


def _seed_document(
    path: Path,
    *,
    doc_id: str = "doc-1",
    name: str = "2025 Tax Return.pdf",
    category: str = "Tax Returns",
    added_at: str = "2026-04-15T00:00:00",
    proxy_url: str = "/documents/doc-1/download",
    fetched_at: str | None = None,
) -> None:
    """Insert one row into the documents table.

    # noqa
    """
    fetched = fetched_at or datetime.now(UTC).isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO documents (id, name, category, added_at, proxy_url, "
            "fetched_at) VALUES (?, ?, ?, ?, ?, ?)",
            (doc_id, name, category, added_at, proxy_url, fetched),
        )
        conn.commit()


@pytest.fixture
def initialized_db(
    tmp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """Create a fresh schema and point the cache module at it via env var.

    # noqa
    """
    monkeypatch.setenv("SQLITE_PATH", str(tmp_db_path))
    db.init_schema(str(tmp_db_path))
    return tmp_db_path


# --------------------------------------------------------------------------- #
# get_entities
# --------------------------------------------------------------------------- #


async def test_get_entities_returns_empty_list_when_no_rows(
    initialized_db: Path,
) -> None:
    """An empty entities table yields no rows.

    # noqa
    """
    rows = await cache.get_entities()
    assert list(rows) == []


async def test_get_entities_returns_seeded_row(initialized_db: Path) -> None:
    """A single seeded entity round-trips through the reader.

    # noqa
    """
    _seed_entity(initialized_db, name="Family Trust", entity_type="Trust")
    rows = list(await cache.get_entities())
    assert len(rows) == 1
    # #ASSUME cache rows are dict-like (sqlite3.Row supports str indexing).
    assert rows[0]["name"] == "Family Trust"


async def test_get_entities_returns_all_seeded_rows(initialized_db: Path) -> None:
    """Multiple seeded entities all appear in the reader output.

    # noqa
    """
    _seed_entity(initialized_db, entity_id="ent-1", name="LLC One")
    _seed_entity(initialized_db, entity_id="ent-2", name="LLC Two")
    rows = list(await cache.get_entities())
    assert len(rows) == 2


# --------------------------------------------------------------------------- #
# get_holdings
# --------------------------------------------------------------------------- #


async def test_get_holdings_returns_empty_list_when_no_rows(
    initialized_db: Path,
) -> None:
    """An empty holdings table yields no rows.

    # noqa
    """
    rows = await cache.get_holdings()
    assert list(rows) == []


async def test_get_holdings_returns_seeded_row(initialized_db: Path) -> None:
    """A single seeded holding round-trips through the reader.

    # noqa
    """
    _seed_holding(initialized_db, security_name="Vanguard Total Stock Market")
    rows = list(await cache.get_holdings())
    assert len(rows) == 1
    assert rows[0]["security_name"] == "Vanguard Total Stock Market"


# --------------------------------------------------------------------------- #
# get_positions
# --------------------------------------------------------------------------- #


async def test_get_positions_returns_empty_list_when_no_rows(
    initialized_db: Path,
) -> None:
    """An empty positions table yields no rows.

    # noqa
    """
    rows = await cache.get_positions()
    assert list(rows) == []


async def test_get_positions_returns_seeded_row(initialized_db: Path) -> None:
    """A single seeded crypto position round-trips through the reader.

    # noqa
    """
    _seed_position(initialized_db, asset="ETH", quantity=10.0, usd_value=30_000.0)
    rows = list(await cache.get_positions())
    assert len(rows) == 1
    assert rows[0]["asset"] == "ETH"


# --------------------------------------------------------------------------- #
# get_performance (portfolio chart data, backs Portfolio section)
# --------------------------------------------------------------------------- #


async def test_get_performance_round_trips_seeded_rows(
    initialized_db: Path,
) -> None:
    """If a ``get_performance`` reader exists, it returns the seeded timeseries.

    The performance table backs the Portfolio section's chart and per
    ``tech-spec.md`` is populated alongside holdings by pp-security-master.
    Phase 1 may expose this either as ``cache.get_performance()`` or fold it
    into ``get_holdings`` -- the test skips if the dedicated reader isn't
    present. #ASSUME

    # noqa
    """
    if not hasattr(cache, "get_performance"):
        pytest.skip(
            "cache.get_performance not exposed; performance data may be "
            "returned alongside holdings -- verify in Phase 1."
        )

    fetched = datetime.now(UTC).isoformat()
    with sqlite3.connect(initialized_db) as conn:
        conn.execute(
            "INSERT INTO performance (date, total_value, benchmark, fetched_at) "
            "VALUES ('2026-05-01', 1000000.0, 950000.0, ?)",
            (fetched,),
        )
        conn.commit()

    rows = list(await cache.get_performance())
    assert len(rows) == 1
    assert rows[0]["date"] == "2026-05-01"


# --------------------------------------------------------------------------- #
# get_documents
# --------------------------------------------------------------------------- #


async def test_get_documents_returns_empty_list_when_no_rows(
    initialized_db: Path,
) -> None:
    """An empty documents table yields no rows.

    # noqa
    """
    rows = await cache.get_documents()
    assert list(rows) == []


async def test_get_documents_returns_seeded_row(initialized_db: Path) -> None:
    """A single seeded document round-trips through the reader.

    # noqa
    """
    _seed_document(initialized_db, name="Trust Agreement.pdf", category="Trusts")
    rows = list(await cache.get_documents())
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# is_stale
# --------------------------------------------------------------------------- #


async def test_is_stale_true_for_empty_dataset(initialized_db: Path) -> None:
    """An empty dataset must be reported as stale (never refreshed).

    # noqa
    """
    assert await cache.is_stale("entities", threshold_hours=8) is True


async def test_is_stale_false_for_fresh_data(initialized_db: Path) -> None:
    """Data fetched moments ago is not stale.

    # noqa
    """
    fresh = datetime.now(UTC).isoformat()
    _seed_entity(initialized_db, fetched_at=fresh)
    assert await cache.is_stale("entities", threshold_hours=8) is False


async def test_is_stale_true_for_data_older_than_threshold(
    initialized_db: Path,
) -> None:
    """Data fetched longer ago than the threshold is reported stale.

    # noqa
    """
    old = (datetime.now(UTC) - timedelta(hours=9)).isoformat()
    _seed_entity(initialized_db, fetched_at=old)
    assert await cache.is_stale("entities", threshold_hours=8) is True


async def test_is_stale_just_under_threshold_is_fresh(
    initialized_db: Path,
) -> None:
    """1 second younger than the threshold is fresh.

    Time is frozen so the comparison is exact -- catches off-by-one
    implementations that use ``>=`` vs ``>``.

    # noqa
    """
    frozen_now = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
    just_under = (frozen_now - timedelta(hours=8) + timedelta(seconds=1)).isoformat()
    _seed_entity(initialized_db, fetched_at=just_under)
    with freeze_time(frozen_now):
        assert await cache.is_stale("entities", threshold_hours=8) is False


async def test_is_stale_just_over_threshold_is_stale(
    initialized_db: Path,
) -> None:
    """1 second older than the threshold is stale.

    Time is frozen so the comparison is exact -- catches off-by-one
    implementations.

    # noqa
    """
    frozen_now = datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC)
    just_over = (frozen_now - timedelta(hours=8) - timedelta(seconds=1)).isoformat()
    _seed_entity(initialized_db, fetched_at=just_over)
    with freeze_time(frozen_now):
        assert await cache.is_stale("entities", threshold_hours=8) is True


async def test_is_stale_uses_most_recent_fetched_at(initialized_db: Path) -> None:
    """Staleness is computed from the most recent fetched_at, not the oldest.

    # noqa
    """
    old = (datetime.now(UTC) - timedelta(hours=10)).isoformat()
    fresh = datetime.now(UTC).isoformat()
    _seed_entity(initialized_db, entity_id="ent-old", fetched_at=old)
    _seed_entity(initialized_db, entity_id="ent-new", fetched_at=fresh)
    assert await cache.is_stale("entities", threshold_hours=8) is False


async def test_is_stale_holdings_threshold(initialized_db: Path) -> None:
    """Holdings threshold per spec is 4 hours.

    # noqa
    """
    five_hours = (datetime.now(UTC) - timedelta(hours=5)).isoformat()
    _seed_holding(initialized_db, fetched_at=five_hours)
    assert await cache.is_stale("holdings", threshold_hours=4) is True


async def test_is_stale_positions_threshold(initialized_db: Path) -> None:
    """Positions threshold per spec is 4 hours.

    # noqa
    """
    fresh = datetime.now(UTC).isoformat()
    _seed_position(initialized_db, fetched_at=fresh)
    assert await cache.is_stale("positions", threshold_hours=4) is False


async def test_is_stale_documents_threshold(initialized_db: Path) -> None:
    """Documents threshold per spec is 24 hours.

    # noqa
    """
    twelve_hours = (datetime.now(UTC) - timedelta(hours=12)).isoformat()
    _seed_document(initialized_db, fetched_at=twelve_hours)
    assert await cache.is_stale("documents", threshold_hours=24) is False
