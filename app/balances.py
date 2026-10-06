# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Synchronous balance storage: replacement, cents conversion, daily snapshots.

These functions take an open SQLite connection and never commit; the caller
runs them inside one transaction under the process-wide write lock
(``app.scheduler``), so a delivery and its snapshot succeed or fail together.

#CRITICAL: financial: amounts are decimal text from the sender. They are
converted with ``Decimal`` and rounded half-even to whole cents; no float
touches a value. #VERIFY: ``tests/unit/test_balances.py`` covers ties and
negative amounts, and no ``float(`` call appears in this module.
"""

from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Decimal
from typing import TYPE_CHECKING

from app.config import local_today
from app.models import provider_of

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Iterable, Sequence

    from app.models import BalanceRow

__all__ = [
    "drop_unreported_from_day",
    "from_cents",
    "local_today",
    "provider_of",
    "replace_balances",
    "snapshot_daily",
    "to_cents",
]

_CENTS_PER_UNIT = Decimal(100)


def to_cents(value: str) -> int:
    """Convert a decimal amount to integer cents, rounding half to even.

    Args:
        value (str): Decimal text such as ``"1234.56"``.

    Returns:
        int: Amount in cents, for example ``123456``.
    """
    cents = (Decimal(value) * _CENTS_PER_UNIT).quantize(
        Decimal(1), rounding=ROUND_HALF_EVEN
    )
    return int(cents)


def from_cents(cents: int) -> Decimal:
    """Convert integer cents back to Decimal dollars with exactly two places.

    This is the one cents-to-dollars helper; every reader that shows money
    uses it, so totals always carry the same exponent.

    Args:
        cents (int): Amount in cents, for example ``123456``.

    Returns:
        Decimal: Amount in dollars, for example ``Decimal("1234.56")``.
    """
    return Decimal(cents).scaleb(-2)


def replace_balances(
    conn: sqlite3.Connection,
    rows: Sequence[BalanceRow],
    fetched_at: str,
) -> int:
    """Replace the stored balances of every provider present in ``rows``.

    A provider with no row in the delivery is left alone, so its last values
    stay stored, with their own ``fetched_at``, rather than vanishing.

    #ASSUME: product: there is no way to retire a provider that has stopped
    reporting. Its last rows stay in the stored balances and the daily
    history until a delivery names that provider again. The balance pages
    judge staleness per provider from ``fetched_at`` (see
    ``app.cache.last_fetched_at``), so a silent provider is labelled out of
    date there. #VERIFY: the owner decides
    whether a retire action (or an expiry age) is wanted before any provider
    is switched off for good.

    #ASSUME: data integrity: an account id's text before the first colon names
    its provider, and one delivery carries all of a provider's rows. If a
    sender ever split one provider across deliveries, the later one would
    drop the earlier rows. #VERIFY: confirm the collector sends every
    provider's rows in one request.

    Args:
        conn (sqlite3.Connection): Open connection inside a transaction.
        rows (Sequence[BalanceRow]): Validated rows from one delivery.
        fetched_at (str): ISO 8601 delivery time stored on each row.

    Returns:
        int: Number of rows stored.
    """
    for provider in sorted({row.provider for row in rows}):
        prefix = f"{provider}:"
        conn.execute(
            "DELETE FROM account_balances WHERE substr(account_id, 1, ?) = ?",
            (len(prefix), prefix),
        )
    conn.executemany(
        "INSERT INTO account_balances (account_id, account_name, entity_id, "
        "category, source, value_cents, as_of, fetched_at, currency, "
        "reconciled_through) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                row.account_id,
                row.account_name,
                row.entity_id,
                row.category,
                row.source,
                to_cents(row.value),
                row.as_of,
                fetched_at,
                row.currency,
                row.reconciled_through,
            )
            for row in rows
        ],
    )
    return len(rows)


def drop_unreported_from_day(
    conn: sqlite3.Connection, day: str, providers: Iterable[str]
) -> int:
    """Remove ``day``'s history rows that a just-applied delivery replaced away.

    Call this after ``replace_balances`` and before ``snapshot_daily``, in the
    same transaction. For each named provider it deletes that day's history
    rows whose account is no longer in ``account_balances``, so a same-day
    account swap does not leave both the old and the new account in the day's
    total. Other providers and every other day are never touched, so history
    for past days is never deleted; only the current day is corrected.

    Args:
        conn (sqlite3.Connection): Open connection inside a transaction.
        day (str): Date being corrected, ``YYYY-MM-DD``.
        providers (Iterable[str]): Providers present in the delivery.

    Returns:
        int: Number of history rows removed.
    """
    removed = 0
    for provider in sorted(set(providers)):
        prefix = f"{provider}:"
        cursor = conn.execute(
            "DELETE FROM balances_daily WHERE date = ? "
            "AND substr(account_id, 1, ?) = ? "
            "AND account_id NOT IN (SELECT account_id FROM account_balances)",
            (day, len(prefix), prefix),
        )
        removed += max(cursor.rowcount, 0)
    return removed


def snapshot_daily(conn: sqlite3.Connection, day: str) -> int:
    """Upsert one history row per account for ``day`` from the current balances.

    A later run on the same day overwrites that day's rows. This function
    never deletes: an account that later disappears keeps its earlier history
    (a same-day swap is corrected by ``drop_unreported_from_day``).

    Args:
        conn (sqlite3.Connection): Open connection inside a transaction.
        day (str): Snapshot date, ``YYYY-MM-DD``.

    Returns:
        int: Number of account rows written for the day.
    """
    cursor = conn.execute(
        "INSERT INTO balances_daily (date, account_id, entity_id, category, "
        "value_cents, as_of, currency) "
        "SELECT ?, account_id, entity_id, category, value_cents, as_of, currency "
        "FROM account_balances WHERE 1 "
        "ON CONFLICT (date, account_id) DO UPDATE SET "
        "entity_id = excluded.entity_id, category = excluded.category, "
        "value_cents = excluded.value_cents, as_of = excluded.as_of, "
        "currency = excluded.currency",
        (day,),
    )
    return max(cursor.rowcount, 0)
