# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Helpers shared by page routes for building template context."""

from __future__ import annotations

import json
from datetime import date
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from app import cache
from app.balances import from_cents
from app.config import local_today
from app.db import get_connection
from app.models import MANUAL_MARK_SOURCE

if TYPE_CHECKING:
    from starlette.requests import Request


def is_admin(request: Request) -> bool:
    """Return True when the signed-in user has the Admin role.

    Args:
        request (Request): Current request.

    Returns:
        bool: True for Admin, False otherwise.
    """
    principal = getattr(request.state, "principal", None)
    return bool(principal is not None and principal.is_admin)


def include_confidential(request: Request) -> bool:
    """Return True when the signed-in user may see confidential documents.

    Only an Admin may; this names the document rule at its call sites.

    Args:
        request (Request): Current request.

    Returns:
        bool: True for Admin, False otherwise.
    """
    return is_admin(request)


async def freshness(dataset: str) -> dict[str, Any]:
    """Return the last-updated time and stale flag for one dataset.

    The freshness time is read once and the stale flag is computed from it.

    Args:
        dataset (str): Dataset name from ``cache.DATASET_TABLES``.

    Returns:
        dict[str, Any]: ``updated`` and ``stale`` template values.
    """
    updated = await cache.last_fetched_at(dataset)
    stale = cache.is_older_than(updated, cache.STALENESS_HOURS[dataset])
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

    Freshness is judged per provider: one provider that stopped reporting makes
    the section show the existing out-of-date label even when others are new.

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
    total = None if row is None or count == 0 else from_cents(int(row["cents"]))
    oldest = str(row["oldest"]) if row is not None and row["oldest"] else None
    as_of_stale = False
    if oldest is not None:
        age = local_today() - date.fromisoformat(oldest)
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


# Plain-English names for balance feeds; anything else is shown from its name.
SOURCE_LABELS: MappingProxyType[str, str] = MappingProxyType(
    {
        "broker_report": "Brokerage report",
        MANUAL_MARK_SOURCE: "Manual entry from a statement",
        "xero_bank": "Bank accounts",
        "crypto_tracker": "Digital currency tracker",
    }
)

# Entity types listed under "person and household"; every other known type is
# listed under "company and trust", and accounts with no cached owner get a
# heading of their own.
# #ASSUME: external resource: the entities backend (llc-manager) reports
# people and households with the types ``individual`` and ``household``. Its
# released type list (llc, corporation, s_corporation, partnership,
# sole_proprietorship, trust, non_profit, other) does not have them yet; they
# arrive with llc-manager PR #105. Until then the people heading stays empty
# and every owner is listed under "company and trust".
# #VERIFY: after llc-manager PR #105 merges, confirm the type strings match
# these two values exactly (they are compared lowercased).
PEOPLE_KINDS = frozenset({"individual", "household"})

# A line needs two points; one day of history is a table row only.
_MIN_CHART_POINTS = 2


def source_label(source: str) -> str:
    """Return a plain-English name for a balance feed.

    Args:
        source (str): Feed name such as ``manual_mark``.

    Returns:
        str: The friendly label for a known feed. For an unknown feed, the
        name with underscores turned into spaces and its first letter
        capitalized.
    """
    known = SOURCE_LABELS.get(source)
    if known is not None:
        return known
    text = source.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def _trend_json(points: list[cache.TrendPoint]) -> str:
    """Encode the trend for the chart script as exact decimal strings.

    Args:
        points (list[cache.TrendPoint]): Daily totals, oldest first.

    Returns:
        str: JSON array of ``{"date", "total"}`` objects.
    """
    return json.dumps(
        [{"date": p.date, "total": str(p.total)} for p in points],
        separators=(",", ":"),
    )


async def balance_breakdown() -> dict[str, Any]:
    """Return template values for the category, entity, trend and source blocks.

    Both Viewers and Admins see every value returned here: the per-owner
    totals and cash account names are the family's own figures, which both
    roles may read (only document confidentiality and the admin pages are
    limited to Admin).

    Returns:
        dict[str, Any]: ``categories``, ``people``, ``companies``,
        ``unknown_owners``, ``trend_rows`` (newest first), ``trend_json``
        (oldest first), ``trend_chart`` (True when at least two days exist),
        ``trend_days``, ``sources_list`` and ``cash_accounts``.
    """
    entities = await cache.get_balance_totals_by_entity()
    trend = await cache.get_daily_totals()
    sources = [
        {
            "label": source_label(src.source),
            "accounts": src.accounts,
            "oldest": src.oldest_as_of,
            "newest": src.newest_as_of,
        }
        for src in await cache.get_balance_sources()
    ]
    return {
        "categories": await cache.get_balance_totals_by_category(),
        "people": [e for e in entities if e.known and e.kind in PEOPLE_KINDS],
        "companies": [e for e in entities if e.known and e.kind not in PEOPLE_KINDS],
        "unknown_owners": [e for e in entities if not e.known],
        "trend_rows": list(reversed(trend)),
        "trend_json": _trend_json(trend),
        "trend_chart": len(trend) >= _MIN_CHART_POINTS,
        "trend_days": cache.TREND_DAYS,
        "sources_list": sources,
        "cash_accounts": await cache.get_cash_accounts(),
    }
