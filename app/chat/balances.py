# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""The balance table that chat sends to the model and shows to the user.

Figures come only from the portal's own tables: ``account_balances`` for one
row per account, and ``balances_daily`` for the daily totals. The same
``BalanceTable`` is written into the prompt and used to render any balance
shown next to an answer, so a figure on screen never comes from model text.

Totals are USD only, like the rest of the portal; accounts in other
currencies are listed with their currency and left out of totals, which is
why the overall total is labelled "All US dollar accounts".

Totals come from the latest day in ``balances_daily``, the same daily series
the finances page charts, while Home sums ``account_balances``. The two agree
when the daily snapshot is current; when a snapshot lags, the as-of date on
each figure shows which one is older. ADR-009 records the choice.

#CRITICAL: financial: amounts are integer cents from the database turned into
``Decimal`` dollars; no float arithmetic. #VERIFY:
tests/unit/test_chat_balances.py checks cents to dollars and the totals.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from app.balances import from_cents
from app.db import get_connection

if TYPE_CHECKING:
    import aiosqlite

USD = "USD"
OVERALL_LABEL = "All US dollar accounts"
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class AccountBalance:
    """One account's latest balance.

    Attributes:
        name (str): Account name shown to the user.
        category (str): Account category.
        entity (str | None): Owning entity's name, when known.
        amount (Decimal): Balance in the account's currency.
        currency (str): ISO currency code.
        as_of (str): ISO date the balance is as of.
    """

    name: str
    category: str
    entity: str | None
    amount: Decimal
    currency: str
    as_of: str


@dataclass(frozen=True)
class BalanceTotal:
    """One total the table lists.

    Attributes:
        label (str): What the total covers, for example "All US dollar accounts".
        amount (Decimal): Total in US dollars.
        as_of (str): Date of the oldest value counted in the total.
    """

    label: str
    amount: Decimal
    as_of: str


@dataclass(frozen=True)
class BalanceTable:
    """Every balance and total chat may state.

    Attributes:
        accounts (tuple[AccountBalance, ...]): One row per account.
        totals (tuple[BalanceTotal, ...]): Totals, overall first.
    """

    accounts: tuple[AccountBalance, ...]
    totals: tuple[BalanceTotal, ...]

    def is_empty(self) -> bool:
        """Say whether the table has no rows at all.

        Returns:
            bool: True when there are no accounts and no totals.
        """
        return not self.accounts and not self.totals

    def overall_total(self) -> BalanceTotal | None:
        """Return the all-accounts total, when there is one.

        Returns:
            BalanceTotal | None: The overall total, or None.
        """
        return self.totals[0] if self.totals else None

    def accounts_named_in(self, *texts: str) -> tuple[AccountBalance, ...]:
        """Return the accounts whose full name appears in any of the texts.

        Matching ignores case and punctuation and needs the whole name as a
        run of words. When both "Brokerage" and "Brokerage Two" exist, a text
        that says "Brokerage Two" matches only the longer name; a text that
        says "Brokerage" alone still matches the shorter one.

        Args:
            *texts (str): Question and answer text to look in.

        Returns:
            tuple[AccountBalance, ...]: Matching accounts, in table order.
        """
        haystacks: list[str] = [_words(text) for text in texts]
        keyed = [(_words(a.name).strip(), a) for a in self.accounts]
        matched: set[int] = set()
        # Longest names first; a matched name is blanked out so a shorter
        # name inside it does not match too.
        for words, account in sorted(keyed, key=lambda pair: -len(pair[0])):
            needle = f" {words} "
            if not words or not any(needle in hay for hay in haystacks):
                continue
            matched.add(id(account))
            haystacks = [hay.replace(needle, " | ") for hay in haystacks]
        found = [a for a in self.accounts if id(a) in matched]
        return tuple(found)


def _words(text: str) -> str:
    """Return the text as lowercase words joined by single spaces, padded.

    Args:
        text (str): Any text.

    Returns:
        str: For example ``" harbor brokerage "``.
    """
    found: list[str] = _WORD.findall(text.lower())
    return f" {' '.join(found)} "


def _account(row: aiosqlite.Row) -> AccountBalance:
    entity = row["entity_name"]
    return AccountBalance(
        name=str(row["account_name"]),
        category=str(row["category"]),
        entity=str(entity) if entity else None,
        amount=from_cents(int(row["value_cents"])),
        currency=str(row["currency"]),
        as_of=str(row["as_of"]),
    )


async def _accounts(conn: aiosqlite.Connection) -> tuple[AccountBalance, ...]:
    cursor = await conn.execute(
        "SELECT b.account_name, b.category, b.value_cents, b.currency, b.as_of, "
        "e.name AS entity_name "
        "FROM account_balances AS b LEFT JOIN entities AS e ON e.id = b.entity_id "
        "ORDER BY b.category, b.account_name"
    )
    return tuple(_account(row) for row in await cursor.fetchall())


async def _daily_totals(conn: aiosqlite.Connection) -> tuple[BalanceTotal, ...]:
    """Return the totals for the latest day in ``balances_daily``.

    Args:
        conn (aiosqlite.Connection): Open connection.

    Returns:
        tuple[BalanceTotal, ...]: Overall total, then one per category; empty
        when the daily table has no USD rows.
    """
    cursor = await conn.execute(
        "SELECT MAX(date) AS day FROM balances_daily WHERE currency = ?", (USD,)
    )
    row = await cursor.fetchone()
    day = row["day"] if row is not None else None
    if not day:
        return ()
    cursor = await conn.execute(
        "SELECT category, SUM(value_cents) AS cents, MIN(as_of) AS oldest "
        "FROM balances_daily WHERE date = ? AND currency = ? "
        "GROUP BY category ORDER BY category",
        (day, USD),
    )
    rows = await cursor.fetchall()
    per_category = [
        BalanceTotal(
            label=f"Category {r['category']}",
            amount=from_cents(int(r["cents"])),
            as_of=str(r["oldest"]),
        )
        for r in rows
    ]
    overall = BalanceTotal(
        label=OVERALL_LABEL,
        amount=sum((t.amount for t in per_category), Decimal(0)),
        as_of=min(t.as_of for t in per_category),
    )
    return (overall, *per_category)


def _totals_from_accounts(
    accounts: tuple[AccountBalance, ...],
) -> tuple[BalanceTotal, ...]:
    usd = [a for a in accounts if a.currency == USD]
    if not usd:
        return ()
    categories = sorted({a.category for a in usd})
    per_category = [
        BalanceTotal(
            label=f"Category {name}",
            amount=sum((a.amount for a in usd if a.category == name), Decimal(0)),
            as_of=min(a.as_of for a in usd if a.category == name),
        )
        for name in categories
    ]
    overall = BalanceTotal(
        label=OVERALL_LABEL,
        amount=sum((a.amount for a in usd), Decimal(0)),
        as_of=min(a.as_of for a in usd),
    )
    return (overall, *per_category)


async def read_balance_table() -> BalanceTable:
    """Read the balance table from the portal's SQLite cache.

    Totals come from the latest day in ``balances_daily``; when that table
    has no USD rows yet, they are summed from the account rows instead.

    Returns:
        BalanceTable: Accounts and totals; empty when nothing is cached.
    """
    async with get_connection() as conn:
        accounts = await _accounts(conn)
        totals = await _daily_totals(conn)
    return BalanceTable(
        accounts=accounts, totals=totals or _totals_from_accounts(accounts)
    )
