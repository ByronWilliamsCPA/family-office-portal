# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""Tests for the chat balance table (app.chat.balances)."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from decimal import Decimal
from typing import TYPE_CHECKING

from app.balances import format_amount, from_cents
from app.chat.balances import AccountBalance, BalanceTable, read_balance_table
from tests.unit.chat_fakes import seed_balances

if TYPE_CHECKING:
    from pathlib import Path


def test_cents_become_exact_dollars() -> None:
    """Cents become dollars without float drift, with two decimal places."""
    assert from_cents(123456789) == Decimal("1234567.89")
    assert from_cents(-5) == Decimal("-0.05")
    assert str(from_cents(100)) == "1.00"


def test_format_amount() -> None:
    """USD shows a dollar sign; other currencies show their code."""
    assert format_amount(Decimal("1234567.891")) == "$1,234,567.89"
    assert format_amount(Decimal(-12)) == "-$12.00"
    assert format_amount(Decimal("100.5"), "EUR") == "100.50 EUR"


async def test_empty_cache_gives_empty_table(portal_env: dict[str, str]) -> None:
    """With nothing cached the table is empty."""
    del portal_env
    table = await read_balance_table()
    assert table.is_empty()
    assert table.overall_total() is None


async def test_reads_accounts_and_daily_totals(
    portal_env: dict[str, str], tmp_db_path: Path
) -> None:
    """Accounts come with entity names; totals come from the latest day."""
    del portal_env
    seed_balances(tmp_db_path)
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        # An older day must not be counted.
        conn.execute(
            "INSERT INTO balances_daily (date, account_id, category, value_cents, "
            "as_of, currency) VALUES ('2026-09-01', 'acct-1', 'Investments', 1, "
            "'2026-09-01', 'USD')"
        )
        conn.commit()
    table = await read_balance_table()
    names = [(a.name, a.entity, a.amount, a.as_of) for a in table.accounts]
    assert names == [
        ("Main Checking", None, Decimal("2500.00"), "2026-10-01"),
        ("Harbor Brokerage", "Maple Holdings LLC", Decimal("1234567.89"), "2026-09-30"),
    ]
    totals = [(t.label, t.amount, t.as_of) for t in table.totals]
    assert totals == [
        ("All US dollar accounts", Decimal("1237067.89"), "2026-09-30"),
        ("Category Cash", Decimal("2500.00"), "2026-10-01"),
        ("Category Investments", Decimal("1234567.89"), "2026-09-30"),
    ]


async def test_totals_fall_back_to_accounts(
    portal_env: dict[str, str], tmp_db_path: Path
) -> None:
    """Without daily rows, totals are summed from USD accounts only."""
    del portal_env
    seed_balances(tmp_db_path)
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        conn.execute("DELETE FROM balances_daily")
        conn.execute(
            "INSERT INTO account_balances (account_id, account_name, category, "
            "source, value_cents, as_of, fetched_at, currency) VALUES "
            "('acct-3', 'Euro Savings', 'Cash', 'manual', 999, '2026-08-01', "
            "'2026-10-01T00:00:00+00:00', 'EUR')"
        )
        conn.commit()
    table = await read_balance_table()
    assert len(table.accounts) == 3
    totals = [(t.label, t.amount, t.as_of) for t in table.totals]
    assert totals == [
        ("All US dollar accounts", Decimal("1237067.89"), "2026-09-30"),
        ("Category Cash", Decimal("2500.00"), "2026-10-01"),
        ("Category Investments", Decimal("1234567.89"), "2026-09-30"),
    ]


async def test_non_usd_only_has_no_totals(
    portal_env: dict[str, str], tmp_db_path: Path
) -> None:
    """Accounts in other currencies alone give no totals."""
    del portal_env
    with closing(sqlite3.connect(tmp_db_path)) as conn:
        conn.execute(
            "INSERT INTO account_balances (account_id, account_name, category, "
            "source, value_cents, as_of, fetched_at, currency) VALUES "
            "('acct-3', 'Euro Savings', 'Cash', 'manual', 999, '2026-08-01', "
            "'2026-10-01T00:00:00+00:00', 'EUR')"
        )
        conn.commit()
    table = await read_balance_table()
    assert table.totals == ()
    assert not table.is_empty()


def _account(name: str) -> AccountBalance:
    return AccountBalance(
        name=name,
        category="Cash",
        entity=None,
        amount=Decimal(1),
        currency="USD",
        as_of="2026-10-01",
    )


def test_accounts_named_in_matches_whole_names() -> None:
    """Names match case-insensitively as whole word runs."""
    table = BalanceTable(
        accounts=(_account("Brokerage"), _account("Brokerage Two"), _account("Main")),
        totals=(),
    )
    found = table.accounts_named_in("What is in brokerage two?", "")
    assert [a.name for a in found] == ["Brokerage Two"]
    found = table.accounts_named_in("x", "The Brokerage, as of today.")
    assert [a.name for a in found] == ["Brokerage"]
    assert table.accounts_named_in("Mainly nothing") == ()
