# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
# ruff: noqa: TC003
"""Tests for the balance aggregation readers in ``app.cache``.

All entities, accounts and amounts are made up. Money comes back as
``Decimal`` dollars, never a float.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from freezegun import freeze_time

from app import cache

PERSON = "11111111-2222-4333-8444-555555555555"
HOUSEHOLD = "66666666-7777-4888-8999-aaaaaaaaaaaa"
COMPANY = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff"
MISSING = "99999999-9999-4999-8999-999999999999"
NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _balance(  # noqa: PLR0913
    conn: sqlite3.Connection,
    account_id: str,
    cents: int,
    *,
    entity: str | None = PERSON,
    category: str = "Cash",
    source: str = "xero_bank",
    as_of: str = "2026-09-26",
    currency: str = "USD",
    name: str = "Example Account",
    fetched_at: str | None = None,
    reconciled: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO account_balances (account_id, account_name, entity_id, "
        "category, source, value_cents, as_of, fetched_at, currency, "
        "reconciled_through) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            account_id,
            name,
            entity,
            category,
            source,
            cents,
            as_of,
            fetched_at or NOW.isoformat(),
            currency,
            reconciled,
        ),
    )


@pytest.fixture
def db_path(tmp_db_path: Path, portal_env: dict[str, str]) -> Path:
    """A schema with three entities (one person, one household, one company)."""
    del portal_env
    with sqlite3.connect(tmp_db_path) as conn:
        conn.executemany(
            "INSERT INTO entities (id, name, type, fetched_at) VALUES (?, ?, ?, ?)",
            [
                (PERSON, "Example Person", "individual", NOW.isoformat()),
                (HOUSEHOLD, "Example Household", "household", NOW.isoformat()),
                (COMPANY, "Example Holdings LLC", "llc", NOW.isoformat()),
            ],
        )
    return tmp_db_path


async def test_totals_by_category_are_usd_only_in_a_fixed_order(
    db_path: Path,
) -> None:
    """Categories are summed per category, USD only, in the documented order."""
    with sqlite3.connect(db_path) as conn:
        _balance(conn, "pp:a", 100_050, category="Investments")
        _balance(conn, "pp:b", 200_000, category="Investments")
        _balance(conn, "xero:c", 5_025, category="Cash")
        _balance(conn, "crypto:d", 7_777, category="Digital currency")
        _balance(conn, "pp:e", 1_000_000, category="Cash", currency="EUR")
        _balance(conn, "pp:f", 300, category="Mystery")
    groups = await cache.get_balance_totals_by_category()
    assert [(g.name, g.total, g.accounts) for g in groups] == [
        ("Investments", Decimal("3000.50"), 2),
        ("Cash", Decimal("50.25"), 1),
        ("Digital currency", Decimal("77.77"), 1),
        ("Mystery", Decimal("3.00"), 1),
    ]
    assert all(isinstance(g.total, Decimal) for g in groups)


async def test_totals_by_category_empty(db_path: Path) -> None:
    """No balances gives an empty list."""
    del db_path
    assert await cache.get_balance_totals_by_category() == []


async def test_totals_by_entity_use_names_and_kinds(db_path: Path) -> None:
    """Totals are grouped per entity with its cached name and type."""
    with sqlite3.connect(db_path) as conn:
        _balance(conn, "pp:a", 100_000, entity=PERSON)
        _balance(conn, "pp:b", 50_000, entity=PERSON)
        _balance(conn, "xero:c", 900_000, entity=HOUSEHOLD)
        _balance(conn, "xero:d", 10_000, entity=COMPANY)
        _balance(conn, "pp:e", 777_777, entity=PERSON, currency="GBP")
    groups = await cache.get_balance_totals_by_entity()
    assert [(g.name, g.kind, g.total, g.accounts) for g in groups] == [
        ("Example Household", "household", Decimal("9000.00"), 1),
        ("Example Person", "individual", Decimal("1500.00"), 2),
        ("Example Holdings LLC", "llc", Decimal("100.00"), 1),
    ]


async def test_unknown_entities_share_a_plain_label_never_a_uuid(
    db_path: Path,
) -> None:
    """An entity not in the cache, or with no id, is "Unknown entity"."""
    with sqlite3.connect(db_path) as conn:
        _balance(conn, "pp:a", 100, entity=MISSING)
        _balance(conn, "pp:b", 250, entity="another-missing-id")
        _balance(conn, "pp:c", 50, entity=None)
    groups = await cache.get_balance_totals_by_entity()
    assert [(g.name, g.kind, g.total, g.accounts) for g in groups] == [
        ("Unknown entity", None, Decimal("4.00"), 3)
    ]
    assert MISSING not in repr(groups)


async def test_daily_totals_sum_per_date_and_cover_90_days(db_path: Path) -> None:
    """The series is one total per date for the last 90 days, oldest first."""
    today = date(2026, 9, 27)
    first = today - timedelta(days=89)
    before = today - timedelta(days=90)
    rows = [
        (before.isoformat(), "pp:a", 111, "USD"),
        (first.isoformat(), "pp:a", 1_000, "USD"),
        (first.isoformat(), "pp:b", 500, "USD"),
        (today.isoformat(), "pp:a", 2_000, "USD"),
        (today.isoformat(), "pp:b", 99_999, "EUR"),
    ]
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO balances_daily (date, account_id, entity_id, category, "
            "value_cents, as_of, currency) VALUES (?, ?, NULL, 'Cash', ?, ?, ?)",
            [(d, a, c, d, cur) for d, a, c, cur in rows],
        )
    points = await cache.get_daily_totals(today=today)
    assert [(p.date, p.total) for p in points] == [
        (first.isoformat(), Decimal("15.00")),
        (today.isoformat(), Decimal("20.00")),
    ]
    short = await cache.get_daily_totals(days=1, today=today)
    assert [p.date for p in short] == [today.isoformat()]


async def test_daily_totals_default_today_is_the_display_date(
    db_path: Path,
) -> None:
    """Without an explicit date the window ends on today's display date."""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO balances_daily (date, account_id, entity_id, category, "
            "value_cents, as_of, currency) VALUES "
            "('2026-09-27', 'pp:a', NULL, 'Cash', 100, '2026-09-27', 'USD')"
        )
    with freeze_time("2026-09-27 12:00:00"):
        points = await cache.get_daily_totals()
    assert [p.date for p in points] == ["2026-09-27"]
    with freeze_time("2027-01-27 12:00:00"):
        assert await cache.get_daily_totals() == []


async def test_manual_marks_are_oldest_first_with_ages_in_days(
    db_path: Path,
) -> None:
    """Only manual marks are listed, oldest first, with whole-day ages."""
    with sqlite3.connect(db_path) as conn:
        _balance(
            conn,
            "pp:new",
            1,
            source="manual_mark",
            as_of="2026-09-20",
            name="Newer Mark",
            entity=HOUSEHOLD,
        )
        _balance(
            conn,
            "pp:old",
            1,
            source="manual_mark",
            as_of="2026-06-30",
            name="Older Mark",
            entity=MISSING,
        )
        _balance(conn, "pp:feed", 1, source="broker_report", as_of="2020-01-01")
    marks = await cache.get_manual_marks(today=date(2026, 9, 27))
    assert [(m.account_name, m.entity_name, m.as_of, m.age_days) for m in marks] == [
        ("Older Mark", "Unknown entity", "2026-06-30", 89),
        ("Newer Mark", "Example Household", "2026-09-20", 7),
    ]


async def test_manual_marks_age_is_never_negative(db_path: Path) -> None:
    """A mark dated in the future reports zero days, not a negative age."""
    with sqlite3.connect(db_path) as conn:
        _balance(conn, "pp:x", 1, source="manual_mark", as_of="2026-10-05")
    marks = await cache.get_manual_marks(today=date(2026, 9, 27))
    assert marks[0].age_days == 0


async def test_manual_marks_default_today(db_path: Path) -> None:
    """Age is measured from today's display date by default."""
    with sqlite3.connect(db_path) as conn:
        _balance(conn, "pp:x", 1, source="manual_mark", as_of="2026-09-17")
    with freeze_time("2026-09-27 12:00:00"):
        marks = await cache.get_manual_marks()
    assert marks[0].age_days == 10


async def test_sources_list_counts_and_as_of_range(db_path: Path) -> None:
    """Each source shows how many accounts it feeds and its as-of range."""
    with sqlite3.connect(db_path) as conn:
        _balance(conn, "pp:a", 1, source="broker_report", as_of="2026-09-26")
        _balance(conn, "pp:b", 1, source="manual_mark", as_of="2026-08-31")
        _balance(conn, "pp:c", 1, source="manual_mark", as_of="2026-09-15")
        _balance(
            conn, "pp:d", 1, source="broker_report", as_of="2026-09-25", currency="EUR"
        )
    sources = await cache.get_balance_sources()
    assert [
        (s.source, s.accounts, s.oldest_as_of, s.newest_as_of) for s in sources
    ] == [
        ("broker_report", 1, "2026-09-26", "2026-09-26"),
        ("manual_mark", 2, "2026-08-31", "2026-09-15"),
    ]


async def test_cash_accounts_carry_reconciled_through(db_path: Path) -> None:
    """Bank-style accounts list their reconciled-through date when present."""
    with sqlite3.connect(db_path) as conn:
        _balance(
            conn, "xero:a", 12_345, name="Example Operating", reconciled="2026-09-20"
        )
        _balance(conn, "xero:b", 500, name="Example Savings")
        _balance(conn, "pp:c", 9, category="Investments")
        _balance(conn, "xero:d", 9, currency="EUR")
    accounts = await cache.get_cash_accounts()
    assert [(a.name, a.value, a.as_of, a.reconciled_through) for a in accounts] == [
        ("Example Operating", Decimal("123.45"), "2026-09-26", "2026-09-20"),
        ("Example Savings", Decimal("5.00"), "2026-09-26", None),
    ]


async def test_balances_freshness_follows_the_oldest_provider(db_path: Path) -> None:
    """A provider that stops reporting makes the whole section look stale."""
    old = (NOW - timedelta(hours=60)).isoformat()
    with sqlite3.connect(db_path) as conn:
        _balance(conn, "pp:a", 1, fetched_at=NOW.isoformat())
        _balance(conn, "xero:b", 1, fetched_at=old)
    with freeze_time(NOW):
        latest = await cache.last_fetched_at("balances")
        stale = await cache.is_stale("balances", 24)
    assert latest == datetime.fromisoformat(old)
    assert stale is True


async def test_balances_freshness_when_every_provider_is_current(
    db_path: Path,
) -> None:
    """All providers fresh means fresh; no rows means stale."""
    with freeze_time(NOW):
        assert await cache.last_fetched_at("balances") is None
        assert await cache.is_stale("balances", 24) is True
        with sqlite3.connect(db_path) as conn:
            _balance(conn, "pp:a", 1)
            _balance(conn, "xero:b", 1)
        assert await cache.is_stale("balances", 24) is False
