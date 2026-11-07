# SPDX-FileCopyrightText: 2026 Byron Williams
# SPDX-License-Identifier: MIT
"""The Portfolio page lists investment-type accounts from the balance feed.

The list needs no backend: it reads ``account_balances`` only, so it shows
with ``BACKEND_PP_SECURITY_URL`` unset. The per-security table is an extra that
appears only when that backend is connected and has cached holdings. Every
name and amount here is made up.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from httpx import AsyncClient

OWNER = "11111111-2222-4333-8444-555555555555"
EMPTY_ACCOUNTS = "Your investment accounts will appear here after the next update."
NONE_REPORTED = "No investment accounts have been reported yet."
NOT_CONNECTED = "Not connected yet"
OUT_OF_DATE = "This may be out of date."
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# (account id, name, category, source, cents, currency)
ACCOUNTS = [
    ("pp:acct-111", "Example Brokerage", "Investments", "broker_report", 125_000_000),
    ("pp:acct-222", "Example Retirement", "Retirement", "manual_mark", 400_000_000),
    (
        "pp:acct-333",
        "Example Land Partnership",
        "Alternatives",
        "manual_mark",
        50_000_000,
    ),
    (
        "crypto:acct-444",
        "Example Wallet",
        "Digital currency",
        "crypto_tracker",
        7_000_000,
    ),
    ("xero:acct-555", "Example Checking", "Cash", "xero_bank", 900_000_000),
]


def _seed_balances(
    path: Path,
    fetched_at: datetime | None = None,
    *,
    as_of: str | None = None,
    accounts: list[tuple[str, str, str, str, int]] | None = None,
) -> None:
    """Cache made-up account balances.

    Args:
        path: SQLite file to seed.
        fetched_at: Fetch time for every row; now when omitted.
        as_of: As-of date for every row; today when omitted.
        accounts: Rows to insert; the module defaults when omitted.
    """
    fetched = (fetched_at or datetime.now(timezone.utc)).isoformat()
    day = as_of or datetime.now(timezone.utc).date().isoformat()
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO account_balances (account_id, account_name, entity_id, "
            "category, source, value_cents, as_of, fetched_at, currency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'USD')",
            [
                (a[0], a[1], OWNER, a[2], a[3], a[4], day, fetched)
                for a in accounts or ACCOUNTS
            ],
        )


def _now() -> str:
    """Return the current UTC time as ISO text, for a fresh fetch stamp."""
    return datetime.now(timezone.utc).isoformat()


def _seed_holding(path: Path, fetched_at: datetime) -> None:
    """Cache one made-up holding.

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


@pytest.fixture
def no_backend(portal_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> None:
    """Leave the pp-security backend unconfigured, as it is today.

    Args:
        portal_env: Applied first, because it sets the variable removed here.
        monkeypatch: Pytest monkeypatch fixture.
    """
    del portal_env
    monkeypatch.delenv("BACKEND_PP_SECURITY_URL")


@pytest.mark.usefixtures("no_backend")
async def test_account_list_shows_with_no_backend_url(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """The list renders from the balance feed alone, with no not-connected box."""
    _seed_balances(tmp_db_path)
    async with client as ac:
        response = await ac.get("/portfolio")
    assert response.status_code == 200
    for text in ("Example Brokerage", "Example Retirement", "$1,250,000"):
        assert text in response.text
    assert NOT_CONNECTED not in response.text
    assert "Last updated" in response.text
    assert OUT_OF_DATE not in response.text


@pytest.mark.usefixtures("no_backend")
async def test_accounts_are_listed_largest_first(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """The biggest account is first, whatever the order they were delivered in."""
    _seed_balances(tmp_db_path)
    async with client as ac:
        html = (await ac.get("/portfolio")).text
    names = [
        "Example Retirement",
        "Example Brokerage",
        "Example Land Partnership",
        "Example Wallet",
    ]
    positions = [html.index(name) for name in names]
    assert positions == sorted(positions)


@pytest.mark.usefixtures("no_backend")
async def test_cash_is_left_out_and_every_other_category_is_in(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """Cash accounts stay on Finances; the four asset categories are listed."""
    _seed_balances(tmp_db_path)
    async with client as ac:
        html = (await ac.get("/portfolio")).text
    assert "Example Checking" not in html
    assert "$9,000,000" not in html
    for name in (
        "Example Brokerage",
        "Example Retirement",
        "Example Land Partnership",
        "Example Wallet",
    ):
        assert name in html


@pytest.mark.usefixtures("no_backend")
async def test_each_account_shows_its_as_of_date(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """Every row says which day its value is from, in plain words."""
    _seed_balances(tmp_db_path, as_of="2026-09-26")
    async with client as ac:
        html = (await ac.get("/portfolio")).text
    assert html.count("Value from September 26, 2026.") == 4


@pytest.mark.usefixtures("no_backend")
async def test_no_raw_account_or_owner_id_is_shown(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """Account ids and owner ids never appear as text or anywhere in the markup."""
    _seed_balances(tmp_db_path)
    async with client as ac:
        html = (await ac.get("/portfolio")).text
    for raw in ("pp:acct-111", "crypto:acct-444", "xero:acct-555", OWNER):
        assert raw not in html
    assert not UUID_RE.search(html)


@pytest.mark.usefixtures("no_backend")
async def test_stale_provider_keeps_values_with_a_last_updated_label(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """One provider that stopped reporting marks the list out of date, not blank."""
    _seed_balances(tmp_db_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    with closing(sqlite3.connect(tmp_db_path)) as conn, conn:
        conn.execute(
            "UPDATE account_balances SET fetched_at = ? "
            "WHERE account_id LIKE 'crypto:%'",
            (old,),
        )
    async with client as ac:
        response = await ac.get("/portfolio")
    assert response.status_code == 200
    assert "Example Wallet" in response.text
    assert "Example Brokerage" in response.text
    assert "Last updated" in response.text
    assert response.text.count(OUT_OF_DATE) == 1


@pytest.mark.usefixtures("no_backend")
async def test_old_as_of_dates_get_the_may_need_updating_note(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """A value dated long ago is flagged even when it was delivered just now."""
    _seed_balances(tmp_db_path, as_of="2026-01-05")
    async with client as ac:
        html = (await ac.get("/portfolio")).text
    assert "Some values are from January 5, 2026 and may need updating." in html


@pytest.mark.usefixtures("no_backend")
async def test_no_balances_shows_a_friendly_empty_state(client: AsyncClient) -> None:
    """With nothing delivered yet the page explains and shows no table."""
    async with client as ac:
        response = await ac.get("/portfolio")
    assert response.status_code == 200
    assert EMPTY_ACCOUNTS in response.text
    assert "Not updated yet" in response.text
    assert NOT_CONNECTED not in response.text
    assert "<table" not in response.text


@pytest.mark.usefixtures("no_backend")
async def test_only_cash_balances_do_not_promise_an_update(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """Cash-only data was delivered, so the page does not say one is coming."""
    _seed_balances(tmp_db_path, accounts=[ACCOUNTS[-1]])
    async with client as ac:
        html = (await ac.get("/portfolio")).text
    assert NONE_REPORTED in html
    assert EMPTY_ACCOUNTS not in html
    assert "Example Checking" not in html
    assert "Last updated" not in html
    assert "Not updated yet" not in html


@pytest.mark.usefixtures("no_backend")
async def test_stale_cash_provider_does_not_flag_the_portfolio(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """A stopped bank feed says nothing about accounts it does not supply."""
    _seed_balances(tmp_db_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    with closing(sqlite3.connect(tmp_db_path)) as conn, conn:
        conn.execute(
            "UPDATE account_balances SET fetched_at = ? WHERE category = 'Cash'",
            (old,),
        )
    async with client as ac:
        html = (await ac.get("/portfolio")).text
    assert "Example Brokerage" in html
    assert "Last updated" in html
    assert OUT_OF_DATE not in html


@pytest.mark.usefixtures("no_backend")
async def test_stale_non_usd_row_does_not_flag_the_portfolio(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """An old row in another currency is not listed, so it cannot age the list."""
    _seed_balances(tmp_db_path)
    old = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
    with closing(sqlite3.connect(tmp_db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO account_balances (account_id, account_name, entity_id, "
            "category, source, value_cents, as_of, fetched_at, currency) VALUES "
            "('pp:acct-eur', 'Example Euro Fund', ?, 'Investments', "
            "'broker_report', 100, '2026-09-26', ?, 'EUR')",
            (OWNER, old),
        )
    async with client as ac:
        html = (await ac.get("/portfolio")).text
    assert "Example Euro Fund" not in html
    assert OUT_OF_DATE not in html
    assert "1 account in another currency not included." in html


@pytest.mark.usefixtures("no_backend")
async def test_other_currency_accounts_are_counted_in_plain_words(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """Two non-USD asset rows and a non-USD cash row: only the first two count."""
    _seed_balances(tmp_db_path)
    with closing(sqlite3.connect(tmp_db_path)) as conn, conn:
        conn.executemany(
            "INSERT INTO account_balances (account_id, account_name, entity_id, "
            "category, source, value_cents, as_of, fetched_at, currency) VALUES "
            "(?, ?, ?, ?, 'broker_report', 100, '2026-09-26', ?, 'EUR')",
            [
                ("pp:eur-1", "Example Euro Fund", OWNER, "Investments", _now()),
                ("pp:eur-2", "Example Euro IRA", OWNER, "Retirement", _now()),
                ("xero:eur-3", "Example Euro Bank", OWNER, "Cash", _now()),
            ],
        )
    async with client as ac:
        html = (await ac.get("/portfolio")).text
    assert "2 accounts in other currencies not included." in html
    assert "Example Euro Fund" not in html


@pytest.mark.usefixtures("no_backend")
async def test_only_other_currency_accounts_say_so_instead_of_blank(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """Delivered but all in another currency: no list, and the page says why."""
    with closing(sqlite3.connect(tmp_db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO account_balances (account_id, account_name, entity_id, "
            "category, source, value_cents, as_of, fetched_at, currency) VALUES "
            "('pp:eur-1', 'Example Euro Fund', ?, 'Investments', "
            "'broker_report', 100, '2026-09-26', ?, 'EUR')",
            (OWNER, _now()),
        )
    async with client as ac:
        html = (await ac.get("/portfolio")).text
    first = "No investment accounts in US dollars have been reported yet."
    second = "1 account in another currency not included."
    assert NONE_REPORTED not in html
    assert first in html
    assert html.index(first) < html.index(second)
    assert re.search(r'<div class="mt-4">\s*<p[^>]*>1 account in another', html)


@pytest.mark.usefixtures("no_backend")
async def test_a_negative_account_sorts_last_and_shows_a_minus(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """A negative value is listed after the positive ones with a minus sign."""
    _seed_balances(
        tmp_db_path,
        accounts=[
            ("pp:acct-1", "Example Brokerage", "Investments", "broker_report", 100_000),
            (
                "pp:acct-2",
                "Example Margin Fund",
                "Alternatives",
                "manual_mark",
                -50_000,
            ),
        ],
    )
    async with client as ac:
        html = (await ac.get("/portfolio")).text
    assert html.index("Example Brokerage") < html.index("Example Margin Fund")
    assert "-$500" in html


@pytest.mark.usefixtures("no_backend")
async def test_long_account_names_cannot_push_the_value_off_the_card(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """The name may wrap; the value keeps its width."""
    _seed_balances(tmp_db_path)
    async with client as ac:
        html = (await ac.get("/portfolio")).text
    assert '<span class="min-w-0 break-words">Example Brokerage</span>' in html
    assert '<span class="shrink-0">$1,250,000</span>' in html


@pytest.mark.parametrize("url", [None, ""], ids=["unset", "blank"])
async def test_security_table_is_hidden_when_not_connected(
    client: AsyncClient,
    tmp_db_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    url: str | None,
) -> None:
    """Old cached holdings are not shown once the backend is unset or blank."""
    if url is None:
        monkeypatch.delenv("BACKEND_PP_SECURITY_URL")
    else:
        monkeypatch.setenv("BACKEND_PP_SECURITY_URL", url)
    _seed_balances(tmp_db_path)
    _seed_holding(tmp_db_path, datetime.now(timezone.utc))
    async with client as ac:
        html = (await ac.get("/portfolio")).text
    assert "Sample Index Fund" not in html
    assert "<table" not in html
    assert NOT_CONNECTED not in html


async def test_security_table_shows_when_connected_with_holdings(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """Connected with holdings: accounts first, then the security table."""
    _seed_balances(tmp_db_path)
    _seed_holding(tmp_db_path, datetime.now(timezone.utc))
    async with client as ac:
        html = (await ac.get("/portfolio")).text
    assert html.index("Example Brokerage") < html.index("Sample Index Fund")
    assert "Diversified" in html
    assert "$1,000" in html
    assert html.count("Last updated") == 2


async def test_connected_without_holdings_says_nothing_about_securities(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """Connected but empty: no table, no empty-box, no not-connected note."""
    _seed_balances(tmp_db_path)
    async with client as ac:
        html = (await ac.get("/portfolio")).text
    assert "Example Brokerage" in html
    assert "<table" not in html
    assert "no investments to show" not in html
    assert NOT_CONNECTED not in html


async def test_stale_holdings_keep_their_own_out_of_date_label(
    client: AsyncClient, tmp_db_path: Path
) -> None:
    """Fresh accounts plus stale holdings: only the securities are flagged."""
    _seed_balances(tmp_db_path)
    _seed_holding(tmp_db_path, datetime.now(timezone.utc) - timedelta(hours=5))
    async with client as ac:
        html = (await ac.get("/portfolio")).text
    assert "Sample Index Fund" in html
    assert html.count(OUT_OF_DATE) == 1
    assert "Example Brokerage" in html


@pytest.mark.usefixtures("no_backend")
@pytest.mark.parametrize("bad", ["", "not a time"], ids=["empty", "garbage"])
async def test_an_unreadable_fetch_time_marks_the_list_old_not_broken(
    client: AsyncClient, tmp_db_path: Path, bad: str
) -> None:
    """One bad stored time must not fail the page; it reads as not updated."""
    _seed_balances(tmp_db_path)
    with closing(sqlite3.connect(tmp_db_path)) as conn, conn:
        conn.execute(
            "UPDATE account_balances SET fetched_at = ? "
            "WHERE account_id = 'pp:acct-111'",
            (bad,),
        )
    async with client as ac:
        response = await ac.get("/portfolio")
    assert response.status_code == 200
    assert "Example Brokerage" in response.text
    assert "Example Retirement" in response.text
    assert "Not updated yet" in response.text
