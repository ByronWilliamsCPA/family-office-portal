# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Per-security table states for a connected pp-security-master backend.

The account list above it is covered in ``test_portfolio_accounts.py``.

pp-security-master is alpha: a failed refresh leaves the last cached rows in
place, so the page must show them with a "last updated" label and the
out-of-date notice, and must never show an error to a primary user.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient

# With no cached holdings the page says nothing about securities; the only
# empty state is the account list's.
EMPTY_MESSAGE = (
    "There are no investments to show yet. They will appear after the next update."
)
EMPTY_ACCOUNTS = "Your investment accounts will appear here after the next update."


def _seed_holding(path: Path, fetched_at: datetime) -> None:
    """Cache one made-up holding fetched at ``fetched_at``.

    Args:
        path: SQLite file to seed.
        fetched_at: Fetch time stored with the row.
    """
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO holdings (id, security_name, sector, current_value, "
            "allocation_pct, fetched_at) VALUES "
            "('h1', 'Sample Index Fund', 'Diversified', 1000.0, 10.0, ?)",
            (fetched_at.isoformat(),),
        )


async def test_connected_empty_portfolio_says_plainly_there_is_nothing_yet(
    client: AsyncClient,
) -> None:
    """With nothing cached the page explains once, and keeps the freshness line."""
    async with client as ac:
        response = await ac.get("/portfolio")
    assert response.status_code == 200
    assert EMPTY_ACCOUNTS in response.text
    assert EMPTY_MESSAGE not in response.text
    assert "Not updated yet" in response.text
    assert "<table" not in response.text


async def test_fresh_portfolio_shows_rows_and_last_updated(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """Fresh rows show with a last-updated label and no out-of-date notice."""
    _seed_holding(tmp_db_path, datetime.now(timezone.utc))
    async with client as ac:
        response = await ac.get("/portfolio")
    assert "Sample Index Fund" in response.text
    assert "Last updated" in response.text
    assert "This may be out of date." not in response.text
    assert EMPTY_MESSAGE not in response.text


async def test_stale_portfolio_shows_rows_with_out_of_date_notice(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """After a failed refresh the last good rows stay, marked as out of date."""
    _seed_holding(tmp_db_path, datetime.now(timezone.utc) - timedelta(hours=5))
    async with client as ac:
        response = await ac.get("/portfolio")
    assert response.status_code == 200
    assert "Sample Index Fund" in response.text
    assert "Last updated" in response.text
    assert "This may be out of date." in response.text
    assert EMPTY_MESSAGE not in response.text
