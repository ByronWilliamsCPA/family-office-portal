# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Helpers shared by page routes for building template context."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app import cache
from app.db import get_connection

if TYPE_CHECKING:
    from starlette.requests import Request


def include_confidential(request: Request) -> bool:
    """Return True when the signed-in user may see confidential documents (D-14).

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


async def balances_summary() -> dict[str, Any]:
    """Return the current account total and its freshness.

    Returns:
        dict[str, Any]: ``total_value`` (dollars or None when no balances are
        cached), ``balances_updated``, and ``balances_stale``.
    """
    async with get_connection() as conn:
        cursor = await conn.execute(
            "SELECT COUNT(*) AS n, SUM(value_cents) AS cents FROM account_balances"
        )
        row = await cursor.fetchone()
    count = int(row["n"]) if row is not None else 0
    total = None if count == 0 or row is None else int(row["cents"]) / 100
    state = await freshness("balances")
    return {
        "total_value": total,
        "balances_updated": state["updated"],
        "balances_stale": state["stale"],
    }
