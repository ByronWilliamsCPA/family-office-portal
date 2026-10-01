# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Helpers shared by page routes for building template context."""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app import cache
from app.db import get_connection

if TYPE_CHECKING:
    from starlette.requests import Request


def include_confidential(request: Request) -> bool:
    """Return True when the signed-in user may see confidential documents.

    Args:
        request (Request): Current request.

    Returns:
        bool: True for Admin, False otherwise.
    """
    principal = getattr(request.state, "principal", None)
    return bool(principal is not None and principal.is_admin)


async def freshness(dataset: str) -> dict[str, Any]:
    """Return the last-updated time and stale flag for one dataset.

    Args:
        dataset (str): Dataset name from ``cache.DATASET_TABLES``.

    Returns:
        dict[str, Any]: ``updated`` and ``stale`` template values.
    """
    updated = await cache.last_fetched_at(dataset)
    stale = await cache.is_stale(dataset, cache.STALENESS_HOURS[dataset])
    return {"updated": updated, "stale": stale}


# Values older than this many days are flagged even when the portal fetched
# them recently: manual marks are monthly, so a mark older than about five
# weeks means one was missed.
# #ASSUME: financial: 35 days suits monthly marks and weekly Xero
# reconciliation. #VERIFY with the admin once real sources are connected.
AS_OF_WARN_DAYS = 35


async def balances_summary() -> dict[str, Any]:
    """Return the current USD account total with its fetch and as-of freshness.

    Only USD rows are summed (the balance feed is USD-only for now); other
    currencies are counted so the page can say they are left out.

    Returns:
        dict[str, Any]: ``total_value`` (Decimal dollars, or None when no USD
        balances are cached), ``balances_updated``, ``balances_stale``,
        ``oldest_as_of`` (date text or None), ``as_of_stale`` (bool), and
        ``excluded_currency_count``.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT "
            "SUM(CASE WHEN currency = 'USD' THEN 1 ELSE 0 END) AS n, "
            "SUM(CASE WHEN currency = 'USD' THEN value_cents ELSE 0 END) AS cents, "
            "MIN(CASE WHEN currency = 'USD' THEN as_of END) AS oldest, "
            "SUM(CASE WHEN currency = 'USD' THEN 0 ELSE 1 END) AS other "
            "FROM account_balances"
        )
        row = await cursor.fetchone()
    count = int(row["n"] or 0) if row is not None else 0
    total = (
        None if row is None or count == 0 else Decimal(int(row["cents"])) / Decimal(100)
    )
    oldest = str(row["oldest"]) if row is not None and row["oldest"] else None
    as_of_stale = False
    if oldest is not None:
        age = datetime.now(timezone.utc).date() - date.fromisoformat(oldest)
        as_of_stale = age.days > AS_OF_WARN_DAYS
    state = await freshness("balances")
    return {
        "total_value": total,
        "balances_updated": state["updated"],
        "balances_stale": state["stale"],
        "oldest_as_of": oldest,
        "as_of_stale": as_of_stale,
        "excluded_currency_count": int(row["other"] or 0) if row is not None else 0,
    }
