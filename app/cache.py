# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Async SQLite readers used by route handlers, plus the staleness checker.

Page route handlers read only from here; they never call backend services
(ADR-003), except the document file proxy (ADR-007). The balance intake route
is the one handler that writes, through ``app.scheduler``. Every reader returns
``aiosqlite.Row`` objects, which support access by column name in templates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from app.balances import local_today
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


# Provider of an account: the text before the first colon of its id.
_PROVIDER_SQL = (
    "CASE WHEN instr(account_id, ':') > 0 "
    "THEN substr(account_id, 1, instr(account_id, ':') - 1) ELSE '' END"
)

# Order in which categories are listed; anything else follows alphabetically.
CATEGORY_ORDER: tuple[str, ...] = (
    "Investments",
    "Retirement",
    "Cash",
    "Digital currency",
    "Alternatives",
)
UNKNOWN_ENTITY = "Unknown entity"
TREND_DAYS = 90


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


# The one predicate that hides confidential documents from Viewers. Every
# document reader uses it, so the rule cannot drift between queries.
_VIEWER_VISIBLE = "is_confidential = 0"


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
        clauses.append(_VIEWER_VISIBLE)
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
    visible = "" if include_confidential else f" AND {_VIEWER_VISIBLE}"
    # ``visible`` is one of two fixed strings; the ID is bound.
    query = f"SELECT * FROM documents WHERE id = ?{visible}"  # nosec B608
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
    confidential = "" if include_confidential else f" AND {_VIEWER_VISIBLE}"
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

    For ``balances`` this is the oldest of each provider's latest fetch time,
    so one provider that stopped reporting makes the whole section stale.

    Returns:
        datetime | None: Latest fetch time, or None when the dataset is empty.

    Raises:
        ValueError: If ``dataset`` is not a known dataset.
    """
    table = DATASET_TABLES.get(dataset)
    if table is None:
        msg = f"Unknown dataset: {dataset}"
        raise ValueError(msg)
    if dataset == "balances":
        # Balances arrive one provider at a time and a provider that stops
        # reporting keeps its last rows, so the section is as fresh as its
        # least recently updated provider, not its newest row.
        query = (
            "SELECT MIN(latest) AS latest FROM ("  # nosec B608
            f"SELECT MAX(fetched_at) AS latest FROM {table} "
            f"GROUP BY {_PROVIDER_SQL})"
        )
    else:
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


# --------------------------------------------------------------------------- #
# Balance aggregates (USD only; money is Decimal dollars, never a float)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BalanceGroup:
    """A total over a group of accounts.

    Attributes:
        name (str): Category or entity name shown to people.
        total (Decimal): Sum of the group's USD balances, in dollars.
        accounts (int): Number of accounts in the group.
        kind (str | None): Entity type such as ``individual``; None for a
            category or an unknown entity.
    """

    name: str
    total: Decimal
    accounts: int
    kind: str | None = None


@dataclass(frozen=True)
class TrendPoint:
    """The total of all accounts on one day.

    Attributes:
        date (str): Day, ``YYYY-MM-DD``.
        total (Decimal): Sum of that day's USD balances, in dollars.
    """

    date: str
    total: Decimal


@dataclass(frozen=True)
class ManualMark:
    """An account whose value is entered by hand.

    Attributes:
        account_name (str): Account name shown to people.
        entity_name (str): Owner name, or ``Unknown entity``.
        as_of (str): Date the mark is true for, ``YYYY-MM-DD``.
        age_days (int): Whole days from that date to today, never negative.
    """

    account_name: str
    entity_name: str
    as_of: str
    age_days: int


@dataclass(frozen=True)
class BalanceSource:
    """One feed of balances and how current its values are.

    Attributes:
        source (str): Feed name such as ``manual_mark``.
        accounts (int): USD accounts the feed supplies.
        oldest_as_of (str): Oldest as-of date among them.
        newest_as_of (str): Newest as-of date among them.
    """

    source: str
    accounts: int
    oldest_as_of: str
    newest_as_of: str


@dataclass(frozen=True)
class CashAccount:
    """A cash or bank account with its date details.

    Attributes:
        name (str): Account name shown to people.
        value (Decimal): Balance in dollars.
        as_of (str): Date the balance is true for.
        reconciled_through (str | None): Date through which the bank
            statement is matched, when the feed supplies it.
    """

    name: str
    value: Decimal
    as_of: str
    reconciled_through: str | None


def _dollars(cents: object) -> Decimal:
    """Convert integer cents to Decimal dollars with two places.

    Args:
        cents (object): Integer cent amount from SQLite.

    Returns:
        Decimal: Amount in dollars, for example ``Decimal("12.34")``.
    """
    return Decimal(int(str(cents))).scaleb(-2)


def _category_rank(name: str) -> tuple[int, str]:
    try:
        return (CATEGORY_ORDER.index(name), name)
    except ValueError:
        return (len(CATEGORY_ORDER), name)


async def get_balance_totals_by_category() -> list[BalanceGroup]:
    """Return USD totals per category in a fixed, plain order.

    Returns:
        list[BalanceGroup]: One group per category that has balances.
    """
    rows = await _fetch_all(
        "SELECT category, SUM(value_cents) AS cents, COUNT(*) AS accounts "
        "FROM account_balances WHERE currency = 'USD' GROUP BY category"
    )
    groups = [
        BalanceGroup(str(r["category"]), _dollars(r["cents"]), int(r["accounts"]))
        for r in rows
    ]
    return sorted(groups, key=lambda g: _category_rank(g.name))


async def get_balance_totals_by_entity() -> list[BalanceGroup]:
    """Return USD totals per owning entity, largest first.

    Names come from the cached entities table. An entity that is not cached,
    or an account with no entity, is grouped as ``Unknown entity``; an id is
    never used as a label.

    Returns:
        list[BalanceGroup]: One group per entity with balances.
    """
    rows = await _fetch_all(
        "SELECT e.name AS name, e.type AS kind, SUM(b.value_cents) AS cents, "
        "COUNT(*) AS accounts FROM account_balances b "
        "LEFT JOIN entities e ON e.id = b.entity_id "
        "WHERE b.currency = 'USD' GROUP BY e.id"
    )
    groups = [
        BalanceGroup(
            name=str(r["name"]) if r["name"] else UNKNOWN_ENTITY,
            total=_dollars(r["cents"]),
            accounts=int(r["accounts"]),
            kind=str(r["kind"]).lower() if r["kind"] else None,
        )
        for r in rows
    ]
    return sorted(groups, key=lambda g: (-g.total, g.name))


async def get_daily_totals(
    days: int = TREND_DAYS, today: date | None = None
) -> list[TrendPoint]:
    """Return the total of all USD accounts per day for the last ``days`` days.

    Args:
        days (int): Length of the window, ending on ``today``.
        today (date | None): Last day of the window; defaults to today in the
            display time zone.

    Returns:
        list[TrendPoint]: One point per day that has history, oldest first.
    """
    end = today if today is not None else local_today()
    start = end - timedelta(days=days - 1)
    rows = await _fetch_all(
        "SELECT date, SUM(value_cents) AS cents FROM balances_daily "
        "WHERE currency = 'USD' AND date >= ? AND date <= ? "
        "GROUP BY date ORDER BY date",
        (start.isoformat(), end.isoformat()),
    )
    return [TrendPoint(str(r["date"]), _dollars(r["cents"])) for r in rows]


async def get_manual_marks(today: date | None = None) -> list[ManualMark]:
    """Return accounts valued by hand with how old each mark is, oldest first.

    Args:
        today (date | None): Date to measure age from; defaults to today in
            the display time zone.

    Returns:
        list[ManualMark]: One entry per manually marked account.
    """
    now = today if today is not None else local_today()
    rows = await _fetch_all(
        "SELECT b.account_name AS name, e.name AS entity, b.as_of AS as_of "
        "FROM account_balances b LEFT JOIN entities e ON e.id = b.entity_id "
        "WHERE b.source = 'manual_mark' ORDER BY b.as_of, b.account_name"
    )
    return [
        ManualMark(
            account_name=str(r["name"]),
            entity_name=str(r["entity"]) if r["entity"] else UNKNOWN_ENTITY,
            as_of=str(r["as_of"]),
            age_days=max((now - date.fromisoformat(str(r["as_of"]))).days, 0),
        )
        for r in rows
    ]


async def get_balance_sources() -> list[BalanceSource]:
    """Return each balance feed with its accounts and as-of date range.

    Returns:
        list[BalanceSource]: One entry per feed, ordered by feed name.
    """
    rows = await _fetch_all(
        "SELECT source, COUNT(*) AS accounts, MIN(as_of) AS oldest, "
        "MAX(as_of) AS newest FROM account_balances WHERE currency = 'USD' "
        "GROUP BY source ORDER BY source"
    )
    return [
        BalanceSource(
            str(r["source"]), int(r["accounts"]), str(r["oldest"]), str(r["newest"])
        )
        for r in rows
    ]


async def get_cash_accounts() -> list[CashAccount]:
    """Return USD cash accounts, largest first, with reconciled-through dates.

    Returns:
        list[CashAccount]: Cash and bank accounts.
    """
    rows = await _fetch_all(
        "SELECT account_name, value_cents, as_of, reconciled_through "
        "FROM account_balances WHERE category = 'Cash' AND currency = 'USD' "
        "ORDER BY value_cents DESC, account_name"
    )
    return [
        CashAccount(
            name=str(r["account_name"]),
            value=_dollars(r["value_cents"]),
            as_of=str(r["as_of"]),
            reconciled_through=(
                str(r["reconciled_through"]) if r["reconciled_through"] else None
            ),
        )
        for r in rows
    ]
