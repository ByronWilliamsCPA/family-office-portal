# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Async SQLite readers used by route handlers, plus the staleness checker.

Page route handlers read only from here; they never call backend services
(ADR-003), except the document file proxy (ADR-007). The balance intake route
is the one handler that writes, through ``app.scheduler``. Every reader returns
``aiosqlite.Row`` objects, which support access by column name in templates.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from app.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

# Dataset name -> backing table. Also the allowlist that keeps table names out
# of user-controlled input.
DATASET_TABLES: dict[str, str] = {
    "entities": "entities",
    "holdings": "holdings",
    "performance": "performance",
    "positions": "positions",
    "documents": "documents",
    "balances": "account_balances",
}

# Staleness thresholds in hours (tech-spec and CLAUDE.md "Data layer rules").
# #ASSUME: timing: thresholds exceed each refresh cadence in app.scheduler, so
# a single missed run does not mark a section stale.
# #VERIFY: keep these in sync with the job intervals in app.scheduler.
STALENESS_HOURS: dict[str, int] = {
    "entities": 8,
    "holdings": 4,
    "performance": 4,
    "positions": 4,
    "documents": 24,
    "balances": 24,
}


async def _fetch_all(
    query: str,
    params: tuple[object, ...] = (),
) -> list[aiosqlite.Row]:
    """Run a read query and return every row.

    Args:
        query (str): Parameterized SQL.
        params (tuple[object, ...]): Query parameters.

    Returns:
        list[aiosqlite.Row]: Result rows.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(query, params)
        rows = await cursor.fetchall()
    return list(rows)


async def get_entities() -> list[aiosqlite.Row]:
    """Return all cached entities ordered by name.

    Returns:
        list[aiosqlite.Row]: Entity rows.
    """
    return await _fetch_all("SELECT * FROM entities ORDER BY name")


async def get_entity(entity_id: str) -> aiosqlite.Row | None:
    """Return one cached entity.

    Args:
        entity_id (str): Entity identifier.

    Returns:
        aiosqlite.Row | None: The entity row, or None when not cached.
    """
    rows = await _fetch_all("SELECT * FROM entities WHERE id = ?", (entity_id,))
    return rows[0] if rows else None


async def get_holdings() -> list[aiosqlite.Row]:
    """Return cached holdings, largest first.

    Returns:
        list[aiosqlite.Row]: Holding rows.
    """
    return await _fetch_all("SELECT * FROM holdings ORDER BY current_value DESC")


async def get_performance() -> list[aiosqlite.Row]:
    """Return the cached portfolio value timeseries in date order.

    Returns:
        list[aiosqlite.Row]: Performance rows.
    """
    return await _fetch_all("SELECT * FROM performance ORDER BY date")


async def get_positions() -> list[aiosqlite.Row]:
    """Return cached crypto positions, largest first.

    Returns:
        list[aiosqlite.Row]: Position rows.
    """
    return await _fetch_all("SELECT * FROM positions ORDER BY usd_value DESC")


async def get_documents(
    *,
    include_confidential: bool = False,
    entity_id: str | None = None,
) -> list[aiosqlite.Row]:
    """Return cached document metadata.

    Args:
        include_confidential (bool): Include documents flagged confidential.
            Only the Admin role may set this.
        entity_id (str | None): Limit to one entity's documents.

    Returns:
        list[aiosqlite.Row]: Document rows ordered by category then name.
    """
    clauses: list[str] = []
    params: list[object] = []
    if not include_confidential:
        clauses.append("is_confidential = 0")
    if entity_id is not None:
        clauses.append("entity_id = ?")
        params.append(entity_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    # ``where`` is built only from the fixed strings above; values are bound.
    query = f"SELECT * FROM documents{where} ORDER BY category, name"  # nosec B608
    return await _fetch_all(query, tuple(params))


async def get_document(
    document_id: str,
    *,
    include_confidential: bool = False,
) -> aiosqlite.Row | None:
    """Return one cached document the caller may see.

    A confidential document is returned only when ``include_confidential``
    is set, so a Viewer cannot tell it apart from an unknown one.

    Args:
        document_id (str): Document identifier.
        include_confidential (bool): Include documents flagged confidential.
            Only the Admin role may set this.

    Returns:
        aiosqlite.Row | None: The document row, or None when it is unknown
        or hidden from the caller.
    """
    query = (
        "SELECT * FROM documents WHERE id = ?"
        if include_confidential
        else "SELECT * FROM documents WHERE id = ? AND is_confidential = 0"
    )
    rows = await _fetch_all(query, (document_id,))
    return rows[0] if rows else None


async def search_documents(
    query: str,
    *,
    include_confidential: bool = False,
    limit: int = 50,
) -> list[aiosqlite.Row]:
    """Search cached document names (case-insensitive substring).

    Args:
        query (str): Text to find in document names.
        include_confidential (bool): Include documents flagged confidential.
        limit (int): Maximum rows returned.

    Returns:
        list[aiosqlite.Row]: Matching document rows.
    """
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    confidential = "" if include_confidential else " AND is_confidential = 0"
    # ``confidential`` is one of two fixed strings; values are bound.
    query = (
        "SELECT * FROM documents WHERE name LIKE ? ESCAPE '\\'"  # nosec B608
        f"{confidential} ORDER BY name LIMIT ?"
    )
    return await _fetch_all(
        query,
        (f"%{escaped}%", limit),
    )


async def get_refresh_log(limit: int = 200) -> list[aiosqlite.Row]:
    """Return the most recent refresh log rows, newest first.

    Args:
        limit (int): Maximum rows returned.

    Returns:
        list[aiosqlite.Row]: Refresh log rows.
    """
    return await _fetch_all(
        "SELECT * FROM refresh_log ORDER BY id DESC LIMIT ?",
        (limit,),
    )


def _parse_timestamp(value: str) -> datetime:
    """Parse an ISO 8601 timestamp, treating naive values as UTC.

    Args:
        value (str): Timestamp text.

    Returns:
        datetime: Timezone-aware timestamp.
    """
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


async def last_fetched_at(dataset: str) -> datetime | None:
    """Return the most recent ``fetched_at`` for a dataset.

    Args:
        dataset (str): Dataset name from ``DATASET_TABLES``.

    Returns:
        datetime | None: Latest fetch time, or None when the dataset is empty.

    Raises:
        ValueError: If ``dataset`` is not a known dataset.
    """
    table = DATASET_TABLES.get(dataset)
    if table is None:
        msg = f"Unknown dataset: {dataset}"
        raise ValueError(msg)
    # ``table`` comes from the DATASET_TABLES allowlist, never from input.
    query = f"SELECT MAX(fetched_at) AS latest FROM {table}"  # nosec B608
    rows = await _fetch_all(query)
    latest = rows[0]["latest"] if rows else None
    return _parse_timestamp(str(latest)) if latest else None


async def is_stale(dataset: str, threshold_hours: int) -> bool:
    """Report whether a dataset's newest row is older than the threshold.

    An empty dataset is stale.

    Args:
        dataset (str): Dataset name from ``DATASET_TABLES``.
        threshold_hours (int): Maximum acceptable age in hours.

    Returns:
        bool: True when the data is missing or older than the threshold.
    """
    latest = await last_fetched_at(dataset)
    if latest is None:
        return True
    age = datetime.now(timezone.utc) - latest
    return age > timedelta(hours=threshold_hours)
